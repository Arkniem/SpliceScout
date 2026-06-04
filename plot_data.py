# -*- coding: utf-8 -*-
"""
Build the per-RUN dataset that powers the browser "Plots" tab.

Sourced from the DEEP-DIVED cell line's filtered run table (runtable/SraRunTable_<line>.csv — produced
at stage 11 `cellline_match` and drug-annotated at stage 12 `runtable_annotate`). This means the plots
only ever show the runs of the cell line that was picked, never samples from other cell lines.

Per run: read depth (spots = Bases / AvgSpotLen), avg spot length, bases, plus the drug / dose /
control / drug-treated annotations and library/instrument fields. Study titles come from ncbi_raw.json.
The numeric/categorical field lists are computed from the columns actually present (and only
categoricals that vary), so the UI's dropdowns adapt to the data. Read-only.
"""
import csv
import json
import os
import re

# run-table column -> clean field name
_NUM_COLS = {"AvgSpotLen": "avg_spot_len", "Bases": "bases"}
_CAT_COLS = {
    "GSE_Series": "study", "drug": "drug", "drug_treated": "drug_treated",
    "is_control": "is_control", "dose": "dose", "Instrument": "instrument",
    "LibrarySelection": "library_selection", "Platform": "platform",
    "Assay Type": "assay", "LibraryLayout": "layout",
    "source_name": "source_name", "treatment": "treatment",
}


def _to_int(v):
    try:
        return int(float((v or "").strip()))
    except Exception:
        return None


def _study_titles(P):
    out = {}
    try:
        result = json.load(open(P.raw_json, encoding="utf-8"))["result"]
        for uid in result.get("uids", []):
            item = result.get(uid) or {}
            acc = item.get("accession", "")
            if acc:
                out[acc] = item.get("title", "") or ""
    except Exception:
        pass
    return out


def _canonical(P):
    try:
        return json.load(open(P.cellline_selection, encoding="utf-8")).get("canonical", "") or ""
    except Exception:
        return ""


def _slug(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name)).strip("_") or "cellline"


def _filtered_csv(P):
    """The deep-dived cell line's filtered run table (exists after stage 11), or None."""
    canonical = _canonical(P)
    if not canonical:
        return None
    base = P.runtable_filtered_csv(_slug(canonical))
    for cand in (base, base.replace(".csv", "_v2.csv")):
        if os.path.exists(cand):
            return cand
    return None


def build_plot_data(P):
    """Return {available, cell_line, samples, studies, numeric, categorical}. `samples` are per-RUN
    rows for the picked cell line only; `numeric`/`categorical` list the variables present + varying."""
    src = _filtered_csv(P)
    if not src:
        return {"available": False, "samples": [], "studies": [], "numeric": [], "categorical": []}
    titles = _study_titles(P)
    canonical = _canonical(P)

    rows = list(csv.DictReader(open(src, encoding="utf-8")))
    have = set(rows[0].keys()) if rows else set()
    cat_cols = [c for c in _CAT_COLS if c in have]

    samples, per_study = [], {}
    for r in rows:
        gse = (r.get("GSE_Series") or "").strip()
        bases = _to_int(r.get("Bases"))
        avg = _to_int(r.get("AvgSpotLen"))
        rec = {
            "study": gse, "study_title": titles.get(gse, ""),
            "run": (r.get("Run") or "").strip(),
            "gsm": (r.get("GEO_Accession (exp)") or "").strip(),
            "avg_spot_len": avg, "bases": bases,
            "spots": round(bases / avg) if (bases and avg) else None,
        }
        for col in cat_cols:
            if col != "GSE_Series":
                rec[_CAT_COLS[col]] = (r.get(col) or "").strip()
        samples.append(rec)
        s = per_study.setdefault(gse, {"gse": gse, "title": titles.get(gse, ""), "n_samples": 0})
        s["n_samples"] += 1

    # numeric: read depth (always, computed) + any present source numerics
    numeric = ["spots"] + [_NUM_COLS[c] for c in _NUM_COLS if c in have]
    # categorical: study + any present categorical that actually VARIES (drop constant columns)
    categorical = ["study"]
    for col in cat_cols:
        if col == "GSE_Series":
            continue
        name = _CAT_COLS[col]
        vals = {s.get(name, "") for s in samples}
        vals.discard("")
        if len(vals) > 1:
            categorical.append(name)

    studies = sorted(per_study.values(), key=lambda d: (-d["n_samples"], d["gse"]))
    return {"available": True, "cell_line": canonical, "samples": samples, "studies": studies,
            "numeric": numeric, "categorical": categorical}


def main():
    import argparse
    from pipeline_paths import Paths
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    a = ap.parse_args()
    d = build_plot_data(Paths(a.run_dir))
    print(f"available={d['available']} cell_line={d.get('cell_line')!r} runs={len(d['samples'])} "
          f"studies={len(d['studies'])}")
    print(f"numeric={d['numeric']}  categorical={d['categorical']}")


if __name__ == "__main__":
    main()
