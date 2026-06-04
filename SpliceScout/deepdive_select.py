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

from progress import NULL

REAL = "Cell line"


def _load_index(P):
    if not os.path.exists(P.cellline_index):
        return {}
    try:
        return json.load(open(P.cellline_index, encoding="utf-8"))
    except Exception:
        return {}


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
    for r in rank_candidates(P)[:15]:
        print(f"  {r['n_compounds']:>3} cmpd | {r['total_spots']:>14,} reads | "
              f"{r['n_studies']:>2} studies | {r['canonical']}")
    run(P, chosen=a.cell_line)


if __name__ == "__main__":
    main()
