# -*- coding: utf-8 -*-
"""
Deep-dive stage — ANNOTATE: add drug / dose / is_control to the FILTERED run table, then build a
filterable Excel workbook.

Drug/control use the MAIN pipeline's general canonicalization (normalize_v2 + compound_map.json) so
this generalizes to any query — NOT the second pipeline's hardcoded VOCAB (specific to its 48-study
set). Dose uses the second pipeline's general DOSE regex (the one piece worth keeping). Writes a
drug_annotation_review.csv audit and a per-cell-line .xlsx (openpyxl; skipped gracefully if absent).
"""
import os
import re
import csv
import json

from normalize_v2 import clean_compound, is_control as nv_is_control
from build_final import _safe_open
from cellline_match import _slug
from progress import NULL

# treatment-ish columns (run-table attribute tags), in priority order
TREATMENT_COLS = ["treatment", "treatments", "agent", "agents", "compound", "compounds",
                  "drug", "drugs", "drug_treatment", "treated_with", "chemical",
                  "perturbation", "small_molecule", "inhibitor", "stimulus"]

DOSE = re.compile(r"(\d+(?:\.\d+)?)\s*(nM|µM|μM|uM|mM|ng/?mL|µg/?mL|μg/?mL|ug/?mL|mg/?mL|%|Gy)\b", re.I)


def parse_dose(text):
    out = "; ".join(dict.fromkeys("%s %s" % (m.group(1), m.group(2)) for m in DOSE.finditer(text or "")))
    m = re.search(r"(water|ethanol)\s+extract", text or "", re.I)
    if m:
        out = (out + " " if out else "") + m.group(1).lower() + " extract"
    return out.strip()


def _disp(name):
    return " + ".join((p.strip()[:1].upper() + p.strip()[1:])
                      for p in str(name).split("+") if p.strip())


def canon_drug(raw, compound_map):
    """Canonical generic drug name (AI map when present), or '' for controls / non-drugs."""
    c = clean_compound(raw)               # None for vehicle/control/empty
    if not c:
        return ""
    info = compound_map.get(c)
    if info is None:
        return _disp(c)
    if not info.get("is_drug", True):
        return ""
    return _disp(info.get("name") or c)


def drug_treated_label(raw, drug, compound_map):
    """3-way per-run classification: Drug Treated / Not Drug Treated / Undetermined.

    Real drug -> treated; explicit control or a recognized non-drug perturbation (is_drug=False,
    e.g. siRNA/KO) -> not treated; no treatment value or a present-but-unrecognized one -> undetermined.
    """
    if drug:
        return "Drug Treated"
    if not raw:
        return "Undetermined"
    if nv_is_control(raw):
        return "Not Drug Treated"
    c = clean_compound(raw)
    info = compound_map.get(c) if c else None
    if info is not None and not info.get("is_drug", True):
        return "Not Drug Treated"     # known non-drug perturbation
    return "Undetermined"             # present but can't be classified (e.g. skip-AI, novel term)


def _treatment_value(row, cols_present):
    for c in cols_present:
        v = (row.get(c) or "").strip()
        if v:
            return v
    return ""


def _find_filtered_csv(P, slug):
    for cand in (P.runtable_filtered_csv(slug), P.runtable_filtered_csv(slug).replace(".csv", "_v2.csv")):
        if os.path.exists(cand):
            return cand
    return None


def make_workbook(src_csv, out_xlsx):
    """Filterable workbook (All runs + Study summary). Ported from 04_make_workbook.py."""
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter
    except Exception as e:
        print(f"  WORKBOOK: openpyxl unavailable ({e}) -> skipping .xlsx")
        return None
    NUMERIC = {"AvgSpotLen", "Bases", "Bytes", "Spots", "TaxID", "version"}
    rows = list(csv.reader(open(src_csv, encoding="utf-8")))
    if not rows:
        return None
    hdr, data = rows[0], rows[1:]
    numidx = {i for i, h in enumerate(hdr) if h in NUMERIC}
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "All runs"
    ws.append(hdr)
    for r in data:
        ws.append([int(v) if i in numidx and v.strip().lstrip("-").isdigit() else v
                   for i, v in enumerate(r)])
    gi = hdr.index("GSE_Series") if "GSE_Series" in hdr else None
    ws2 = wb.create_sheet("Study summary")
    if gi is not None:
        cols = {c: hdr.index(c) for c in ("SRA Study", "BioProject", "Organism", "Assay Type",
                                          "Platform", "Instrument") if c in hdr}
        ws2.append(["GSE"] + list(cols) + ["# Runs"])
        from collections import OrderedDict, Counter
        seen = OrderedDict(); cnt = Counter()
        for r in data:
            g = r[gi]; cnt[g] += 1
            if g not in seen:
                seen[g] = [r[cols[c]] for c in cols]
        for g, vals in seen.items():
            ws2.append([g] + vals + [cnt[g]])
    fill = PatternFill("solid", fgColor="1F4E78"); font = Font(bold=True, color="FFFFFF")
    for sh in (ws, ws2):
        for c in sh[1]:
            c.fill = fill; c.font = font
        sh.freeze_panes = "B2"
        if sh.dimensions and sh.max_row > 1:
            sh.auto_filter.ref = sh.dimensions
    for i, h in enumerate(hdr, 1):
        ws.column_dimensions[get_column_letter(i)].width = min(max(len(h) + 2, 11), 32)
    try:
        wb.save(out_xlsx)
    except PermissionError:
        out_xlsx = out_xlsx.replace(".xlsx", "_v2.xlsx")
        wb.save(out_xlsx)
        print(f"  ** workbook locked -> wrote {os.path.basename(out_xlsx)} **")
    print(f"  WORKBOOK -> {out_xlsx} ({len(data)} runs)")
    return out_xlsx


def run(P, sel, reporter=NULL):
    """Annotate the filtered run table (drug/dose/is_control), write review + workbook."""
    slug = _slug(sel.get("canonical", "cellline"))
    src = _find_filtered_csv(P, slug)
    if not src:
        print("  ANNOTATE: no filtered run table found -> skipping")
        return None
    compound_map = {}
    if os.path.exists(P.compound_map):
        try:
            compound_map = json.load(open(P.compound_map, encoding="utf-8"))
        except Exception:
            compound_map = {}

    rows = list(csv.DictReader(open(src, encoding="utf-8")))
    base_cols = list(rows[0].keys()) if rows else []
    cols_present = [c for c in TREATMENT_COLS if c in base_cols]
    extra = ("drug", "dose", "is_control", "drug_treated")
    fields = [c for c in base_cols if c not in extra] + list(extra)

    review = {}
    counter = {"yes": 0, "no": 0}
    dt_counter = {"Drug Treated": 0, "Not Drug Treated": 0, "Undetermined": 0}
    for r in rows:
        raw = _treatment_value(r, cols_present)
        drug = canon_drug(raw, compound_map)
        dose = parse_dose(raw)
        # a real drug => not a control; else 'yes' only for explicit vehicle/negative controls
        # (a non-drug perturbation such as siRNA leaves drug='' but is_control='no').
        ctrl = "yes" if (not drug and raw and nv_is_control(raw)) else "no"
        dt = drug_treated_label(raw, drug, compound_map)   # 3-way Drug Treated/Not/Undetermined
        r["drug"], r["dose"], r["is_control"], r["drug_treated"] = drug, dose, ctrl, dt
        counter[ctrl] = counter.get(ctrl, 0) + 1
        dt_counter[dt] = dt_counter.get(dt, 0) + 1
        _, _, _, _, n = review.get(raw, (None, None, None, None, 0))
        review[raw] = (drug, dose, ctrl, dt, n + 1)
    reporter.set_detail(f"{len(rows)} runs annotated")

    out = _safe_open(src)  # rewrite in place (same file), _v2 fallback if locked
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore"); w.writeheader()
        for r in rows:
            w.writerow(r)
    with open(_safe_open(P.runtable_drug_review), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["original_condition", "drug", "dose", "is_control", "drug_treated", "n_runs"])
        for cond, (drug, dose, ctrl, dt, n) in sorted(review.items()):
            w.writerow([cond, drug, dose, ctrl, dt, n])

    xlsx = make_workbook(out, P.runtable_workbook(slug))
    print(f"  ANNOTATE: {len(rows)} runs | drug_treated={dt_counter} | is_control={counter} | "
          f"review -> {os.path.basename(P.runtable_drug_review)}")
    return {"n_runs": len(rows), "is_control": counter, "drug_treated": dt_counter,
            "workbook": bool(xlsx), "annotated_csv": out}


def main():
    import argparse
    from pipeline_paths import Paths
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    a = ap.parse_args()
    P = Paths(a.run_dir).ensure_dirs()
    sel = json.load(open(P.cellline_selection, encoding="utf-8"))
    run(P, sel)


if __name__ == "__main__":
    main()
