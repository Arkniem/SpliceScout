# -*- coding: utf-8 -*-
"""
Universal in-memory data layer for the SpliceScout Assistant.

Loads EVERYTHING a run has collected into one in-memory SQLite database so the assistant can SELECT
across it and chart any of it. Tables exposed (whichever artifacts exist yet):

  studies          ncbi_raw.json          one row per GEO study found (assay, organism, #samples, date…)
  study_protocol   study_protocol.json    extracted lib protocol per study (strategy, selection, instrument)
  samples          structured_samples.jsonl   one row per extracted sample (cell_line, spots, compounds…)
  pipeline_stages  progress.json          live per-stage progress (status, done, total)
  data_funnel      (synthesized)          data-volume funnel: how many studies/samples survive each step
  <each tables/*.csv and runtable/*.csv>  loaded verbatim (all columns TEXT)

JSON-derived tables get real numeric column types (n_geo_samples, spots, done, total, n …) so SQL
math/sorting works without CAST; CSV tables are all TEXT (cast for math). Read-only / stdlib-only.
"""
import csv
import glob
import json
import os
import re
import sqlite3

MAX_ROWS = 200000


def _ident(s):
    s = re.sub(r"[^A-Za-z0-9_]", "_", str(s)).strip("_")
    if not s or s[0].isdigit():
        s = "c_" + s
    return s


def _uniq(cols):
    seen, out = {}, []
    for c in cols:
        seen[c] = seen.get(c, 0) + 1
        out.append(c if seen[c] == 1 else "%s_%d" % (c, seen[c]))
    return out


def _create(con, table, coldefs):
    con.execute('CREATE TABLE "%s" (%s)' % (table, ", ".join('"%s" %s' % (n, ty) for n, ty in coldefs)))


def _insert(con, table, ncols, rows):
    if rows:
        con.executemany('INSERT INTO "%s" VALUES (%s)' % (table, ",".join("?" * ncols)), rows)


def _as_int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


# ----------------------------- JSON-derived tables -----------------------------
def _load_studies(con, run_dir):
    p = os.path.join(run_dir, "ncbi_raw.json")
    if not os.path.exists(p):
        return
    res = (json.load(open(p, encoding="utf-8")) or {}).get("result", {}) or {}
    uids = res.get("uids", []) or []
    cols = [("uid", "TEXT"), ("accession", "TEXT"), ("gse", "TEXT"), ("title", "TEXT"),
            ("organism", "TEXT"), ("entrytype", "TEXT"), ("gdstype", "TEXT"), ("assay", "TEXT"),
            ("is_sequencing", "INTEGER"), ("pdat", "TEXT"), ("year", "INTEGER"),
            ("n_geo_samples", "INTEGER"), ("bioproject", "TEXT"), ("gpl", "TEXT"), ("suppfile", "TEXT")]
    _create(con, "studies", cols)
    out = []
    for u in uids:
        e = res.get(u) or {}
        gdst = (e.get("gdstype") or "")
        low = gdst.lower()
        seq = 1 if "sequencing" in low else 0
        assay = "sequencing" if seq else ("array" if "array" in low else (gdst or "other"))
        pdat = e.get("pdat") or ""
        m = re.match(r"(\d{4})", pdat)
        ns = _as_int(e.get("n_samples"))
        if ns is None:
            ns = len(e.get("samples") or [])
        out.append((str(e.get("uid", u)), e.get("accession", ""), e.get("gse", ""),
                    (e.get("title", "") or "")[:300], e.get("taxon", ""), e.get("entrytype", ""),
                    gdst, assay, seq, pdat, int(m.group(1)) if m else None, ns,
                    e.get("bioproject", ""), e.get("gpl", ""), e.get("suppfile", "")))
    _insert(con, "studies", len(cols), out)


def _load_protocol(con, run_dir):
    p = os.path.join(run_dir, "study_protocol.json")
    if not os.path.exists(p):
        return
    sp = json.load(open(p, encoding="utf-8"))
    if not isinstance(sp, dict):
        return
    cols = [("gse", "TEXT"), ("protocol", "TEXT"), ("strategy", "TEXT"),
            ("selection", "TEXT"), ("instrument", "TEXT")]
    _create(con, "study_protocol", cols)
    out = []
    for gse, v in sp.items():
        if isinstance(v, dict):
            out.append((gse, (v.get("protocol", "") or "")[:1000], v.get("strategy", ""),
                        v.get("selection", ""), v.get("instrument", "")))
        else:
            out.append((gse, str(v)[:1000], "", "", ""))
    _insert(con, "study_protocol", len(cols), out)


def _load_samples(con, run_dir):
    p = os.path.join(run_dir, "structured_samples.jsonl")
    if not os.path.exists(p):
        return
    cols = [("study", "TEXT"), ("gsm", "TEXT"), ("title", "TEXT"), ("cell_line", "TEXT"),
            ("source", "TEXT"), ("spots", "INTEGER"), ("n_treatments", "INTEGER"),
            ("n_compounds", "INTEGER"), ("compounds", "TEXT")]
    _create(con, "samples", cols)
    batch, n = [], 0
    with open(p, encoding="utf-8") as f:
        for line in f:
            if n >= MAX_ROWS:
                break
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            comp = r.get("compounds") or []
            tr = r.get("treatments_raw") or []
            comp = comp if isinstance(comp, list) else [comp]
            batch.append((r.get("study", ""), r.get("gsm", ""), (r.get("title", "") or "")[:300],
                          r.get("cell_line", ""), r.get("source", ""), _as_int(r.get("spots")),
                          len(tr) if isinstance(tr, list) else 0, len(comp),
                          ";".join(str(c) for c in comp)))
            n += 1
            if len(batch) >= 2000:
                _insert(con, "samples", len(cols), batch)
                batch = []
    _insert(con, "samples", len(cols), batch)


def _load_stages(con, run_dir):
    p = os.path.join(run_dir, "progress.json")
    if not os.path.exists(p):
        return
    stages = (json.load(open(p, encoding="utf-8")) or {}).get("stages", []) or []
    cols = [("ordinal", "INTEGER"), ("key", "TEXT"), ("label", "TEXT"), ("status", "TEXT"),
            ("done", "INTEGER"), ("total", "INTEGER"), ("fraction", "REAL")]
    _create(con, "pipeline_stages", cols)
    out = [(i + 1, s.get("key", ""), s.get("label", ""), s.get("status", ""),
            _as_int(s.get("done")), _as_int(s.get("total")),
            s.get("fraction") if isinstance(s.get("fraction"), (int, float)) else None)
           for i, s in enumerate(stages)]
    _insert(con, "pipeline_stages", len(cols), out)


# ----------------------------- the data-volume funnel -----------------------------
def _json_dict_len(path):
    try:
        d = json.load(open(path, encoding="utf-8"))
        return len(d) if hasattr(d, "__len__") else None
    except Exception:
        return None


def _csv_rows(path):
    try:
        with open(path, encoding="utf-8", newline="") as f:
            r = csv.reader(f)
            next(r, None)
            return sum(1 for _ in r)
    except Exception:
        return None


def _line_count(path):
    try:
        n = 0
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    n += 1
        return n
    except Exception:
        return None


def _canonical_slug(run_dir):
    try:
        can = json.load(open(os.path.join(run_dir, "runtable", "cellline_selection.json"),
                              encoding="utf-8")).get("canonical", "") or ""
    except Exception:
        return ""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", can).strip("_")


def funnel_rows(run_dir):
    """Ordered data-volume funnel: how much data survives each step. `unit` distinguishes the rising
    study/sample-gathering phase from the falling filter phase (units differ, so each row is labelled).
    Steps with no artifact yet are omitted (the funnel grows as the run progresses)."""
    out = [None]  # box for the running order counter

    def add(phase, step, label, n, unit):
        if n is None:
            return
        out[0] = (out[0] or 0) + 1
        rows.append({"step_order": out[0], "phase": phase, "step": step,
                     "label": label, "n": int(n), "unit": unit})

    rows = []
    out[0] = 0
    n_found = n_seq = samp_found = None
    raw_p = os.path.join(run_dir, "ncbi_raw.json")
    if os.path.exists(raw_p):
        try:
            res = (json.load(open(raw_p, encoding="utf-8")) or {}).get("result", {}) or {}
            uids = res.get("uids", []) or []
            n_found, n_seq, samp_found = len(uids), 0, 0
            for u in uids:
                e = res.get(u) or {}
                if "sequencing" in (e.get("gdstype") or "").lower():
                    n_seq += 1
                ns = _as_int(e.get("n_samples"))
                samp_found += ns if ns is not None else len(e.get("samples") or [])
        except Exception:
            pass
    add("gather", "studies_found", "GEO studies found", n_found, "studies")
    add("gather", "studies_sequencing", "…sequencing (can have SRA)", n_seq, "studies")
    add("gather", "studies_extracted", "Studies extracted",
        _json_dict_len(os.path.join(run_dir, "study_protocol.json")), "studies")
    add("gather", "samples_in_studies", "Samples listed in found studies", samp_found, "samples")
    add("gather", "samples_extracted", "Samples extracted (SRA resolved)",
        _line_count(os.path.join(run_dir, "structured_samples.jsonl")), "samples")
    add("filter", "samples_built", "Samples after AI cleaning",
        _csv_rows(os.path.join(run_dir, "tables", "ncbi_final.csv")), "samples")
    add("filter", "samples_runtable_all", "Samples in deep-dive run table",
        _csv_rows(os.path.join(run_dir, "runtable", "SraRunTable_all.csv")), "samples")
    slug = _canonical_slug(run_dir)
    if slug:
        cl = os.path.join(run_dir, "runtable", "SraRunTable_%s.csv" % slug)
        if not os.path.exists(cl):
            cl = cl.replace(".csv", "_v2.csv")
        add("filter", "samples_cellline", "Samples · picked cell line", _csv_rows(cl), "samples")
    return rows


def _load_funnel(con, run_dir):
    rows = funnel_rows(run_dir)
    cols = [("step_order", "INTEGER"), ("phase", "TEXT"), ("step", "TEXT"),
            ("label", "TEXT"), ("n", "INTEGER"), ("unit", "TEXT")]
    _create(con, "data_funnel", cols)
    _insert(con, "data_funnel", len(cols),
            [(r["step_order"], r["phase"], r["step"], r["label"], r["n"], r["unit"]) for r in rows])


# ----------------------------- CSV tables -----------------------------
def _load_csv(con, path, table):
    with open(path, encoding="utf-8", newline="") as f:
        rdr = csv.reader(f)
        header = next(rdr, None)
        if not header:
            return False
        cols = _uniq([_ident(h) for h in header])
        _create(con, table, [(c, "TEXT") for c in cols])
        batch, n = [], 0
        for row in rdr:
            if n >= MAX_ROWS:
                break
            batch.append((row + [""] * len(cols))[:len(cols)])
            n += 1
            if len(batch) >= 2000:
                _insert(con, table, len(cols), batch)
                batch = []
        _insert(con, table, len(cols), batch)
    return True


# ----------------------------- public API -----------------------------
def build_db(run_dir):
    """Build the in-memory DB of every artifact in run_dir. Returns (connection, meta) where meta is
    [{name, rows, columns:[...]}]. Each loader is best-effort — a bad artifact won't sink the rest."""
    con = sqlite3.connect(":memory:")
    for fn in (_load_studies, _load_protocol, _load_samples, _load_stages, _load_funnel):
        try:
            fn(con, run_dir)
        except Exception:
            pass
    for path in sorted(glob.glob(os.path.join(run_dir, "tables", "*.csv"))
                       + glob.glob(os.path.join(run_dir, "runtable", "*.csv"))):
        t = _ident(os.path.splitext(os.path.basename(path))[0])
        try:
            _load_csv(con, path, t)
        except Exception:
            pass
    meta = []
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
        try:
            cnt = con.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
            cols = [c[1] for c in con.execute('PRAGMA table_info("%s")' % t).fetchall()]
        except Exception:
            cnt, cols = 0, []
        meta.append({"name": t, "rows": cnt, "columns": cols})
    return con, meta


def query(run_dir, sql, max_rows=2000):
    """Run one SQL statement over the run's DB. Caller is responsible for the SELECT-only guard.
    Returns {cols, rows:[dict], tables:[name], error}."""
    con, meta = build_db(run_dir)
    tables = [m["name"] for m in meta]
    try:
        cur = con.execute(sql)
        cols = [c[0] for c in cur.description] if cur.description else []
        rows = cur.fetchmany(max_rows)
        return {"cols": cols, "rows": [dict(zip(cols, r)) for r in rows], "tables": tables, "error": None}
    except Exception as e:
        return {"cols": [], "rows": [], "tables": tables, "error": "SQL error: %s" % e}
    finally:
        con.close()


def inventory(run_dir):
    """What's queryable/chartable in this run: every table + columns + row count, and the funnel."""
    con, meta = build_db(run_dir)
    con.close()
    return {"tables": meta, "funnel_steps": funnel_rows(run_dir),
            "note": "Query any table with run_sql; chart any of it with make_chart (sql=<SELECT…> or "
                    "source=<table>|funnel). studies/samples/study_protocol/pipeline_stages/data_funnel "
                    "are always present once their artifact exists; tables/* and runtable/* appear later."}
