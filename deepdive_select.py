# -*- coding: utf-8 -*-
"""
Deep-dive stage — SELECT: choose the single best cell line to deep-dive.

Reads cellline_index.json (written by build_final from the splicing pass — full studies + GSMs,
untruncated). Considers ONLY real cell lines (Sample Type == "Cell line"), so fallback buckets like
UNRESOLVED / Patient-Tumor / Organoid are never chosen. Ranks by:
    1. # unique compounds (desc)        <- drug diversity
    2. total reads / total_spots (desc) <- 'most and best data'
    3. name (stable tiebreak)

Auto mode picks rank 1; manual mode lets the caller pass a `chosen` canonical (the UI/CLI picks
from `rank_candidates`). Writes cellline_selection.json and returns the full selection dict (or None
when no real cell line exists).
"""
import json
import os
import re

from progress import NULL

REAL = "Cell line"


def _load_index(P):
    if not os.path.exists(P.cellline_index):
        return {}
    try:
        return json.load(open(P.cellline_index, encoding="utf-8"))
    except Exception:
        return {}


# ---- pre-select consolidation: merge cell-line NAME variants so a line split across spellings
# (A549 / A-549 / "A549 cells") isn't under-counted and mis-ranked. Deterministic, no AI. ----
_CELL_SUFFIX = re.compile(r"[\s_\-]*\b(?:cell\s*lines?|cells?)\s*$", re.I)


def _merge_key(name):
    """Normalized clustering key: lowercase, drop a trailing 'cell line(s)'/'cells', strip
    non-alphanumeric. So A549 / A-549 / A 549 / 'A549 cells' / 'A549 cell line' all -> 'a549'."""
    s = _CELL_SUFFIX.sub("", (name or "").strip().lower())
    return re.sub(r"[^a-z0-9]+", "", s)


def _merge_entries(entries):
    """Union the per-cell-line aggregates (counts, studies, GSMs, compounds, ...) of several rows."""
    out = {"sample_type": entries[0].get("sample_type", REAL),
           "compounds": set(), "studies": set(), "total": 0, "treated": 0, "not": 0,
           "undetermined": 0, "total_spots": 0, "max_spots": 0,
           "gsms": set(), "gsms_by_study": {}, "raw_tags": {}, "uids_by_study": {}}
    for e in entries:
        out["compounds"].update(e.get("compounds", []))
        out["studies"].update(e.get("studies", []))
        for k in ("total", "treated", "not", "undetermined", "total_spots"):
            out[k] += e.get(k, 0)
        out["max_spots"] = max(out["max_spots"], e.get("max_spots", 0))
        out["gsms"].update(e.get("gsms", []))
        for g, v in (e.get("gsms_by_study") or {}).items():
            out["gsms_by_study"].setdefault(g, set()).update(v)
        for t, c in (e.get("raw_tags") or {}).items():
            out["raw_tags"][t] = out["raw_tags"].get(t, 0) + c
        out["uids_by_study"].update(e.get("uids_by_study") or {})
    out["compounds"] = sorted(out["compounds"])
    out["studies"] = sorted(out["studies"])
    out["gsms"] = sorted(out["gsms"])
    out["gsms_by_study"] = {g: sorted(v) for g, v in out["gsms_by_study"].items()}
    return out


def consolidate(P, reporter=NULL):
    """Merge cell-line NAME variants in cellline_index.json BEFORE ranking/selection — deterministic
    (same Sample Type + same normalized key). Picks the dominant spelling (most samples) as canonical,
    unions the aggregates, and records the other spellings as 'aliases' (reused by cellline_match for
    run-table recall). Rewrites cellline_index.json in place + writes cellline_merge.json. Idempotent.
    Returns the merge map {canonical: [original names...]}."""
    index = _load_index(P)
    if not index:
        return {}
    groups = {}
    for name, d in index.items():
        groups.setdefault((d.get("sample_type") or "", _merge_key(name)), []).append(name)

    merged, merge_map, n_groups = {}, {}, 0
    for (stype, key), names in groups.items():
        if not key:                       # no alphanumerics to key on -> never collapse; keep each
            for n in names:
                merged[n], merge_map[n] = index[n], [n]
            continue
        canonical = sorted(names, key=lambda n: (-index[n].get("total", 0), len(n), n))[0]
        agg = _merge_entries([index[n] for n in names])
        alias_pool = set(names)
        for n in names:                   # keep any prior aliases too -> idempotent on re-run
            alias_pool.update(index[n].get("aliases") or [])
        agg["aliases"] = sorted(alias_pool - {canonical})
        merged[canonical], merge_map[canonical] = agg, sorted(names)
        if len(names) > 1:
            n_groups += 1

    json.dump(merged, open(P.cellline_index, "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(merge_map, open(P.cellline_merge, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if len(index) != len(merged):
        print(f"  CONSOLIDATE: merged cell-line name variants {len(index)} -> {len(merged)} "
              f"({n_groups} group(s) merged)")
        reporter.set_detail(f"merged cell-line name variants: {len(index)} -> {len(merged)}")
    else:
        print(f"  CONSOLIDATE: {len(index)} cell-line names, no spelling variants to merge")
    return merge_map


def rank_candidates(P):
    """Return real cell lines ranked (compounds desc, total_spots desc, name). UI/CLI choose from this."""
    index = _load_index(P)
    rows = []
    for canonical, d in index.items():
        if d.get("sample_type") != REAL:
            continue
        rows.append({
            "canonical": canonical,
            "sample_type": d.get("sample_type"),
            "n_compounds": len(d.get("compounds", [])),
            "compounds_preview": ", ".join(sorted(d.get("compounds", []))[:8]),
            "n_studies": len(d.get("studies", [])),
            "studies": d.get("studies", []),
            "total": d.get("total", 0),
            "treated": d.get("treated", 0),
            "total_spots": d.get("total_spots", 0),
            "max_spots": d.get("max_spots", 0),
        })
    rows.sort(key=lambda r: (-r["n_compounds"], -r["total_spots"], r["canonical"]))
    return rows


def select(P, canonical, reporter=NULL):
    """Write cellline_selection.json for `canonical` (pulled from the index). Returns the sel dict."""
    index = _load_index(P)
    d = index.get(canonical)
    if d is None:
        return None
    sel = {
        "canonical": canonical,
        "sample_type": d.get("sample_type"),
        "studies": d.get("studies", []),
        "gsms": d.get("gsms", []),
        "gsms_by_study": d.get("gsms_by_study", {}),
        "raw_tags": d.get("raw_tags", {}),
        "compounds": d.get("compounds", []),
        "total": d.get("total", 0),
        "treated": d.get("treated", 0),
        "total_spots": d.get("total_spots", 0),
        "max_spots": d.get("max_spots", 0),
        "aliases": d.get("aliases", []),   # other spellings merged into this line (-> cellline_match)
    }
    os.makedirs(P.runtable_dir, exist_ok=True)
    json.dump(sel, open(P.cellline_selection, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    reporter.set_detail(f"{canonical}: {sel['total']} samples, {len(sel['studies'])} studies, "
                        f"{sel['total_spots']:,} reads")
    print(f"  SELECT: {canonical} | {len(sel['compounds'])} compounds | "
          f"{len(sel['studies'])} studies | {sel['total']} samples | {sel['total_spots']:,} reads")
    return sel


def run(P, reporter=NULL, chosen=None):
    """Rank real cell lines; pick `chosen` (if valid) else rank 1; persist + return sel (or None)."""
    ranked = rank_candidates(P)
    if not ranked:
        print("  SELECT: no real cell line found (only non-cell-line buckets) -> skipping deep dive")
        reporter.set_detail("no real cell line found")
        return None
    canonical = None
    if chosen:
        valid = {r["canonical"] for r in ranked}
        canonical = chosen if chosen in valid else None
        if canonical is None:
            print(f"  SELECT: requested {chosen!r} not among real cell lines -> using top-ranked")
    if canonical is None:
        canonical = ranked[0]["canonical"]
    return select(P, canonical, reporter)


def main():
    import argparse
    from pipeline_paths import Paths
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--cell-line", default=None, help="pick a specific canonical line (else rank 1)")
    a = ap.parse_args()
    P = Paths(a.run_dir).ensure_dirs()
    consolidate(P)
    for r in rank_candidates(P)[:15]:
        print(f"  {r['n_compounds']:>3} cmpd | {r['total_spots']:>14,} reads | "
              f"{r['n_studies']:>2} studies | {r['canonical']}")
    run(P, chosen=a.cell_line)


if __name__ == "__main__":
    main()
