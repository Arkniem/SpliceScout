# -*- coding: utf-8 -*-
"""
AltAnalyze splicing (PSI) stage deploy -- the analysis step that runs AFTER BAM->BED on the cluster.

When the analysis module is "bulk_rna_seq" and the cluster is on, this fills the vendored psi_template/
config.sh from the form + the run, ships it to <download_root>/psi, and AUTO-CHAINS it: a launcher job
(psi_launch.sh) waits for the BED stage's PIPELINE_COMPLETE.txt, then runs ./run_psi_pipeline.sh, which
runs ONE AltAnalyze job over the whole BED dir -> a per-sample PSI table (+ a differential dPSI comparison
when a usable 2-group split exists).

AltAnalyze itself is NOT shipped in the bundle (it is multi-GB with its species database). Instead, at
submit time we PROBE the cluster: if AltAnalyze + its database are found at ALTANALYZE_HOME (default:
$PIPELINE_ROOT/altanalyze_home -- set it to an existing cluster install to reuse that), we use them in
place; otherwise, if the user pointed us at a LOCAL AltAnalyze copy, we upload it once to
<psi_root>/altanalyze_home (idempotent). An explicit ALTANALYZE_DB path override is supported
for when the database lives outside the AltAnalyze folder.

The comparison groups are computed from the run: build_psi_bundle writes sample_groups.tsv (BioSample ->
group), and build_groups.sh on the cluster intersects that with the BEDs actually present.

Reuses cluster_deploy's fill_config / SSH transport / diagnose_failure.
"""
import os
import re
import csv
import shutil
import subprocess
from collections import defaultdict, Counter

from progress import NULL
import cluster_deploy
import star_deploy   # reuse detect_organism (organism consistency across stages)
from bed_deploy import organism_to_species
from cellline_match import _slug as _cl_slug

HERE = os.path.dirname(os.path.abspath(__file__))
PSI_TEMPLATE_DIR = os.path.join(HERE, "psi_template")

# config.sh vars we fill (name -> default). Numerics are written unquoted.
PSI_CONFIG_DEFAULTS = {
    "BED_INPUT_DIR": "/data/CHANGE_ME/STAR_beds",
    "PIPELINE_ROOT": "/data/CHANGE_ME/psi",
    "ALTANALYZE_HOME": "",          # "" => $PIPELINE_ROOT/altanalyze_home (portable; set to a cluster install to reuse)
    "ALTANALYZE_DB": "",            # "" => $ALTANALYZE_HOME/AltDatabase ; else an external DB path
    "ORGANISM": "Homo sapiens",
    "SPECIES": "",                  # "" => config.sh derives from ORGANISM (default Hs)
    "PSI_OUT": "",                  # "" => config.sh derives $PIPELINE_ROOT/output
    "EXPNAME": "splicing",
    "RUN_GOELITE": "0",
    "GROUP_KEY_SUFFIX": ".bed",
    "THREADS": 4,
    "MEM_MB": 128000,
    "WALL": "1108:00",   # default -W: queue MAX (66480 min) so the AltAnalyze job never dies to walltime
    "LSF_QUEUE": "",
    "JOB_TAG": "psi",
    "WATCHDOG_INTERVAL_MIN": 30,
    "MAX_RESUBMITS": 2,
    "CLEANUP_TOOLS_WHEN_DONE": "0",
    "COMPRESS_WHEN_DONE": "gzip",   # gzip | xz (LZMA2) | off -- compress kept data after PSI COMPLETE
    "COMPRESS_DIR": "",             # "" => parent of PIPELINE_ROOT (the whole project tree)
    "COMPRESS_MIN_MB": 1,
    "COMPRESS_THREADS": 8,
    "PYTHON_MODULE": "python/2.7.5",
    "SAMTOOLS_MODULE": "samtools",
    "R_MODULE": "R",
}
PSI_NUMERIC = {"THREADS", "MEM_MB", "WATCHDOG_INTERVAL_MIN", "MAX_RESUBMITS", "COMPRESS_MIN_MB", "COMPRESS_THREADS"}

# psi_cfg keys that are NOT config.sh vars (deploy-time only) -- stripped before fill_config.
_DEPLOY_ONLY = ("enabled", "ALTANALYZE_LOCAL")


def _copy_lf(srcf, destf):
    txt = open(srcf, encoding="utf-8", errors="replace").read().replace("\r\n", "\n").replace("\r", "\n")
    with open(destf, "w", encoding="utf-8", newline="\n") as f:
        f.write(txt)


def _resolve_psi_cfg(psi_cfg):
    vals = dict(PSI_CONFIG_DEFAULTS)
    for k in PSI_CONFIG_DEFAULTS:
        if psi_cfg and k in psi_cfg and str(psi_cfg[k]).strip() != "":
            vals[k] = psi_cfg[k]
    return vals


def _set_config_var(cfg_path, name, value):
    """Rewrite one `NAME=...` line in an already-filled config.sh (local file, pre-upload)."""
    txt = open(cfg_path, encoding="utf-8").read()
    rep = f"{name}={cluster_deploy._shval(name, value, PSI_NUMERIC, PSI_CONFIG_DEFAULTS)}"
    txt = re.sub(rf"(?m)^{re.escape(name)}=.*$", lambda m, r=rep: r, txt, count=1)
    with open(cfg_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(txt)


def _find_col(header, aliases):
    low = {h.strip().lower(): h for h in header}
    for a in aliases:
        if a in low:
            return low[a]
    return None


# ---- comparison groups: BioSample -> (group_num, group_label) -----------------------------------
# DEFAULT (Phase A): each distinct drug CONDITION (drug x dose x timepoint, scoped by GSE) becomes its
# own group compared against the pooled control baseline (group 1) -> AltAnalyze writes one
# PSI.<GSE>.<drug>_<dose/time>_vs_control.txt per condition. The control baseline = every sample with NO
# drug ("Not Drug Treated"); "Drug Treated" samples are SUBDIVIDED by condition (vs the old binary which
# lumped all of them into one "drug_treated" group). If per-condition isn't viable (fewer than 2 groups
# with >= 2 samples, e.g. all-singleton conditions or no replicated control), we FALL BACK to the binary
# split so a run never ends up worse off than before. Phase B still overrides via the user `group` column.
_DRUG_LABELMAP = {"not drug treated": (1, "not_drug_treated"), "drug treated": (2, "drug_treated")}

# Original free-text condition columns (the timepoint/duration often lives ONLY here, e.g. MDSL's
# "DB2115-treated 20h"), NOT the canonical drug/dose columns appended by runtable_annotate.
_RAW_TREAT_COLS = ("treatment", "treatments", "agent", "agents", "compound", "compounds",
                   "treated_with", "chemical", "perturbation", "small_molecule", "inhibitor",
                   "stimulus", "drug_treatment")
_TIME_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|d|day|days|min|mins|minute|minutes|"
                      r"wk|week|weeks)\b", re.I)
_TIME_UNIT = {"hour": "h", "hours": "h", "hr": "h", "hrs": "h", "day": "d", "days": "d",
              "minute": "min", "minutes": "min", "mins": "min", "week": "wk", "weeks": "wk"}
MIN_PER_GROUP = 2   # mirrors build_groups.sh: a group needs >= this many samples to be compared

# Technical covariates that drive RNA-seq batch effects -> used to pick the NEAREST control-bearing study
# for a drug-GSE that has no controls of its own (col-alias tuple, weight). Higher weight = more important.
_TECH_FEATS = (
    (("instrument",), 3.0),
    (("libraryselection", "library_selection"), 2.0),
    (("librarylayout", "library_layout"), 2.0),
    (("librarysource", "library_source"), 1.0),
    (("platform",), 1.0),
    (("assay type", "assay_type"), 1.0),
    (("center name", "center_name"), 1.0),
)
_SPOTLEN_ALIASES = ("avgspotlen", "avg_spot_len", "avgspotlength")


def _norm_token(s):
    """Filename/AltAnalyze-safe token: keep alnum + . + - ; everything else -> single '_'."""
    s = re.sub(r"[^0-9A-Za-z.\-]+", "_", str(s or "").strip())
    return re.sub(r"_{2,}", "_", s).strip("_.")


def _time_tokens(text):
    """Distinct duration/timepoint tokens from free text: 'DB2115-treated 20h' -> ['20h']."""
    out = []
    for m in _TIME_RE.finditer(text or ""):
        unit = m.group(2).lower()
        out.append(f"{m.group(1)}{_TIME_UNIT.get(unit, unit)}")
    return list(dict.fromkeys(out))


def _raw_treatment(row, cols):
    for c in cols:
        v = (row.get(c) or "").strip()
        if v:
            return v
    return ""


def _condition_label(gse, drug, dose, raw):
    """'<GSE>.<drug>[_<dose>][_<time>]' (falls back to slug of the raw treatment text when no canonical
    drug). The label IS both the comparison group key and the output filename stem: AltAnalyze writes
    PSI.<label>_vs_<control>.txt."""
    gse_t = _norm_token(gse)
    if drug:
        base = _norm_token(drug)
        variant = [_norm_token(re.sub(r"\s+", "", p)) for p in re.split(r"[;,]", dose or "")]
        variant += _time_tokens(raw)
        parts, low = [base], base.lower()
        for t in variant:
            if t and t.lower() not in low:
                parts.append(t)
                low += "_" + t.lower()
        core = "_".join(p for p in parts if p)
    else:
        core = _norm_token(raw)
    core = core or "treated"
    return (gse_t + "." if gse_t else "") + core


def _collapse_labelmap(per, lm):
    """Collapse {bs: Counter(label)} via a {label.lower(): (num, name)} map (binary default / Phase B).
    Returns (out, counts, dropped)."""
    out, counts, dropped = [], Counter(), []
    for bs, c in per.items():
        known = [(lab, n) for lab, n in c.most_common() if lab.lower() in lm]
        if not known:
            dropped.append((bs, c.most_common(1)[0][0] if c else ""))
            continue
        num, name = lm[known[0][0].lower()]
        out.append((bs, num, name))
        counts[num] += 1
    return out, counts, dropped


def _tech_signature(sample_rows, feat_cols, spot_col):
    """Modal categorical value per feature + mean AvgSpotLen across the given run rows (for NN matching)."""
    sig = {}
    for col, _w in feat_cols:
        c = Counter((r.get(col) or "").strip() for r in sample_rows if (r.get(col) or "").strip())
        sig[col] = c.most_common(1)[0][0] if c else ""
    spots = []
    if spot_col:
        for r in sample_rows:
            try:
                spots.append(float((r.get(spot_col) or "").strip()))
            except ValueError:
                pass
    sig["_spot"] = (sum(spots) / len(spots)) if spots else None
    return sig


def _sig_distance(a, b, feat_cols):
    """Weighted distance between two technical signatures (lower = more similar)."""
    d = 0.0
    for col, w in feat_cols:
        if (a.get(col) or "") != (b.get(col) or ""):
            d += w
    sa, sb = a.get("_spot"), b.get("_spot")
    if sa is not None and sb is not None:
        d += 3.0 * min(1.0, abs(sa - sb) / 50.0)   # read-length within ~50 bp counts as similar
    return d


def _nearest_control_gse(orphan_sig, cand_sigs, feat_cols):
    """cand_sigs: {gse: (sig, n_controls)} -> nearest control-bearing gse (tie: more controls, then id)."""
    best, best_key = None, None
    for gse, (sig, nctrl) in cand_sigs.items():
        key = (_sig_distance(orphan_sig, sig, feat_cols), -nctrl, gse)
        if best_key is None or key < best_key:
            best_key, best = key, gse
    return best


def _build_default_groups(rows, header):
    """PURE core of the default grouping (no I/O; unit-testable). Subdivide 'Drug Treated' samples into
    per-CONDITION groups (drug x dose/time, GSE-scoped) vs control. Baseline:
      * single control-study  -> ONE pooled "control" group (build_groups does all-vs-control; no comps spec)
      * multi control-study   -> PER-GSE control groups + an explicit `comps` list pairing each condition to
                                 its OWN study's controls; a drug-GSE with no controls borrows the NEAREST
                                 control-bearing study (technical-signature nearest-neighbor).
    Falls back to the binary drug-vs-not split when per-condition isn't viable. Returns
    {out, groups, mode, comparisons, baseline, comps?, orphans?} or None."""
    bs_col = _find_col(header, ("biosample", "bio_sample", "biosample_accession", "sample", "sample_accession"))
    dt_col = _find_col(header, ("drug_treated",))
    if not bs_col or not dt_col:
        return None
    drug_col = _find_col(header, ("drug",))
    dose_col = _find_col(header, ("dose",))
    gse_col = _find_col(header, ("gse_series", "gse", "gse_accession (exp)", "gse_accession"))
    raw_cols = [c for c in _RAW_TREAT_COLS if c in header]
    feat_cols = [(_find_col(header, al), w) for al, w in _TECH_FEATS]
    feat_cols = [(c, w) for c, w in feat_cols if c]
    spot_col = _find_col(header, _SPOTLEN_ALIASES)

    per_dt = defaultdict(Counter)       # bs -> Counter(drug_treated label)
    per_label = defaultdict(Counter)    # bs -> Counter(condition label)  (treated only)
    per_gse = defaultdict(Counter)      # bs -> Counter(GSE)
    ctrl_rows_by_gse = defaultdict(list)   # gse -> control run rows (for candidate signatures)
    treat_rows_by_gse = defaultdict(list)  # gse -> treated run rows (for orphan signatures)
    for r in rows:
        bs = (r.get(bs_col) or "").strip()
        if not bs:
            continue
        dt = (r.get(dt_col) or "").strip()
        gse = ((r.get(gse_col) or "").strip()) if gse_col else ""
        if dt:
            per_dt[bs][dt] += 1
        if gse:
            per_gse[bs][gse] += 1
        if dt.lower() == "drug treated":
            per_label[bs][_condition_label(gse, (r.get(drug_col) or "") if drug_col else "",
                                           (r.get(dose_col) or "") if dose_col else "",
                                           _raw_treatment(r, raw_cols))] += 1
            treat_rows_by_gse[gse].append(r)
        elif dt.lower() == "not drug treated":
            ctrl_rows_by_gse[gse].append(r)

    # collapse runs -> BioSample (majority vote); classify treated vs control vs undetermined(drop)
    treated, controls, dropped = {}, [], []
    bs_gse = {}
    for bs in per_dt:
        bs_gse[bs] = per_gse[bs].most_common(1)[0][0] if per_gse[bs] else ""
        chosen = next((lab.lower() for lab, _n in per_dt[bs].most_common()
                       if lab.lower() in ("drug treated", "not drug treated")), None)
        if chosen is None:
            dropped.append(bs)
        elif chosen == "not drug treated":
            controls.append(bs)
        else:
            treated[bs] = per_label[bs].most_common(1)[0][0] if per_label[bs] else "treated"

    control_by_gse = defaultdict(list)
    for bs in controls:
        control_by_gse[bs_gse.get(bs, "")].append(bs)
    cond_by_label = defaultdict(list)
    cond_gse = {}
    for bs, lab in treated.items():
        cond_by_label[lab].append(bs)
        cond_gse[lab] = bs_gse.get(bs, "")
    usable_ctrl_gses = {g for g, b in control_by_gse.items() if g and len(b) >= MIN_PER_GROUP}

    # -------- multi-study: PER-GSE controls + nearest-neighbor for control-less drug studies ----------
    if len(usable_ctrl_gses) >= 2:
        res = _pergse_groups(control_by_gse, usable_ctrl_gses, cond_by_label, cond_gse,
                             ctrl_rows_by_gse, treat_rows_by_gse, feat_cols, spot_col, dropped)
        if res:
            return res
        print("  PSI BUNDLE: per-GSE matching produced no usable comparison -> trying pooled control")

    # -------- single control-study (or fallback): ONE pooled control baseline, all-vs-control ----------
    labels = sorted(set(treated.values()), key=lambda s: (s.lower(), s))
    lab_num = {lab: i for i, lab in enumerate(labels, start=2)}
    out, counts = [], Counter()
    for bs in controls:
        out.append((bs, 1, "control")); counts[1] += 1
    for bs, lab in treated.items():
        out.append((bs, lab_num[lab], lab)); counts[lab_num[lab]] += 1
    qualifying = [n for n, c in counts.items() if c >= MIN_PER_GROUP]
    if counts.get(1, 0) >= MIN_PER_GROUP and len(qualifying) >= 2:
        comparisons = [lab for lab in labels if counts[lab_num[lab]] >= MIN_PER_GROUP]
        thin = [lab for lab in labels if counts[lab_num[lab]] < MIN_PER_GROUP]
        if thin:
            print(f"  PSI BUNDLE: {len(thin)} condition(s) with < {MIN_PER_GROUP} samples dropped (no reps)")
        if dropped:
            print(f"  PSI BUNDLE: dropped {len(dropped)} Undetermined sample(s) (no drug call)")
        return {"out": out, "groups": dict(counts), "mode": "per_condition",
                "comparisons": comparisons, "baseline": "control"}

    # -------- FALLBACK: binary drug-vs-not (never worse than the old behavior) ----------
    print("  PSI BUNDLE: per-condition split not viable -> binary drug_treated vs not_drug_treated")
    bout, bcounts, _bd = _collapse_labelmap(per_dt, _DRUG_LABELMAP)
    if not bout:
        return None
    return {"out": bout, "groups": dict(bcounts), "mode": "binary",
            "comparisons": ["drug_treated"] if bcounts.get(2) else [], "baseline": "not_drug_treated"}


def _pergse_groups(control_by_gse, usable_ctrl_gses, cond_by_label, cond_gse,
                   ctrl_rows_by_gse, treat_rows_by_gse, feat_cols, spot_col, dropped):
    """Multi-study path: per-GSE control groups + explicit matched comps (nearest-neighbor control study
    for drug-GSEs that lack their own controls). Returns the result dict, or None if no usable comp."""
    ctrl_label = {g: f"{_norm_token(g)}.control" for g in usable_ctrl_gses}
    # number ALL groups (control + condition) by sorted label
    all_labels = sorted(set(ctrl_label.values()) | set(cond_by_label.keys()), key=lambda s: (s.lower(), s))
    lab_num = {lab: i for i, lab in enumerate(all_labels, start=1)}

    out, counts = [], Counter()
    for g in usable_ctrl_gses:
        for bs in control_by_gse[g]:
            out.append((bs, lab_num[ctrl_label[g]], ctrl_label[g])); counts[lab_num[ctrl_label[g]]] += 1
    for lab, bslist in cond_by_label.items():
        for bs in bslist:
            out.append((bs, lab_num[lab], lab)); counts[lab_num[lab]] += 1

    cand_sigs = {g: (_tech_signature(ctrl_rows_by_gse.get(g, []), feat_cols, spot_col),
                     len(control_by_gse[g])) for g in usable_ctrl_gses}
    orphan_base = {}   # orphan gse -> chosen nearest control gse (cached per gse)
    comps, comparisons, orphans, thin = [], [], [], []
    for lab in sorted(cond_by_label, key=lambda s: (s.lower(), s)):
        if len(cond_by_label[lab]) < MIN_PER_GROUP:
            thin.append(lab)
            continue
        gse = cond_gse[lab]
        if gse in usable_ctrl_gses:
            base = ctrl_label[gse]
        else:
            if gse not in orphan_base:
                osig = _tech_signature(treat_rows_by_gse.get(gse, []), feat_cols, spot_col)
                orphan_base[gse] = _nearest_control_gse(osig, cand_sigs, feat_cols)
            nn = orphan_base[gse]
            if not nn:
                continue
            base = ctrl_label[nn]
            orphans.append((lab, gse, nn))
        comps.append((lab_num[lab], lab_num[base]))
        comparisons.append(lab)
    if not comps:
        return None
    if thin:
        print(f"  PSI BUNDLE: {len(thin)} condition(s) with < {MIN_PER_GROUP} samples dropped (no reps)")
    if dropped:
        print(f"  PSI BUNDLE: dropped {len(dropped)} Undetermined sample(s) (no drug call)")
    for lab, gse, nn in orphans:
        print(f"  PSI BUNDLE: {gse} has no own controls -> nearest-neighbor baseline {nn} for '{lab}'")
    n_ctrl = len(usable_ctrl_gses)
    print(f"  PSI BUNDLE: per-GSE matched comparisons: {len(comps)} (across {n_ctrl} control studies, "
          f"{len(orphan_base)} borrowed via NN)")
    return {"out": out, "groups": dict(counts), "mode": "per_condition_pergse",
            "comparisons": comparisons, "baseline": "per-GSE", "comps": comps, "orphans": orphans}


def _write_sample_groups(P, sel, dest, group_col=None, labelmap=None):
    """Write sample_groups.tsv (BioSample<TAB>group_num<TAB>label) from the annotated run table.
    DEFAULT = per-condition (drug x dose/time) vs control, binary fallback (see _build_default_groups).
    Phase B (group_col + labelmap) overrides with the user `group` column. Returns
    {n, groups:{num:count}, mode, comparisons} or None if no usable table."""
    if not sel:
        return None
    slug = _cl_slug(sel.get("canonical", "cellline"))
    src = None
    for cand in (P.runtable_filtered_csv(slug), P.runtable_filtered_csv(slug).replace(".csv", "_v2.csv")):
        if os.path.exists(cand):
            src = cand
            break
    if not src:
        return None
    try:
        rows = list(csv.DictReader(open(src, encoding="utf-8")))
    except Exception:
        return None
    if not rows:
        return None
    header = list(rows[0].keys())
    bs_col = _find_col(header, ("biosample", "bio_sample", "biosample_accession", "sample", "sample_accession"))
    if not bs_col:
        return None

    if group_col and labelmap:
        # Phase B: arbitrary user labels already numbered 1..N in `labelmap` (label -> (num, name))
        if group_col not in header:
            return None
        per = defaultdict(Counter)
        for r in rows:
            bs = (r.get(bs_col) or "").strip()
            val = (r.get(group_col) or "").strip()
            if bs and val:
                per[bs][val] += 1
        lm = {k.lower(): v for k, v in labelmap.items()}
        out, counts, dropped = _collapse_labelmap(per, lm)
        if dropped:
            labs = sorted({lab for _bs, lab in dropped if lab})
            print(f"  !! PSI BUNDLE WARNING: DROPPED {len(dropped)} sample(s) with unmapped group label(s) "
                  f"{labs} (not in {sorted(lm.keys())}) -> EXCLUDED from sample_groups.tsv")
        if not out:
            return None
        baseline = next((n for _l, (num, n) in lm.items() if num == 1), "control")
        res = {"out": out, "groups": dict(counts), "mode": "phaseB",
               "comparisons": [n for _l, (num, n) in lm.items() if num != 1], "baseline": baseline}
    else:
        res = _build_default_groups(rows, header)
        if not res or not res["out"]:
            return None

    with open(dest, "w", encoding="utf-8", newline="\n") as f:
        for bs, num, name in sorted(res["out"], key=lambda t: (t[1], t[0])):
            f.write(f"{bs}\t{num}\t{name}\n")
    # explicit matched comps (multi-study per-GSE path) -> sibling sample_comps.tsv; build_groups honors it
    comps_path = os.path.join(os.path.dirname(dest), "sample_comps.tsv")
    comps = res.get("comps")
    if comps:
        with open(comps_path, "w", encoding="utf-8", newline="\n") as f:
            for exp_num, base_num in sorted(comps):
                f.write(f"{exp_num}\t{base_num}\n")
    elif os.path.exists(comps_path):
        os.remove(comps_path)   # stale spec would wrongly force matched-comps mode on the cluster
    return {"n": len(res["out"]), "groups": res["groups"], "mode": res["mode"],
            "comparisons": res.get("comparisons", []), "baseline": res.get("baseline", "control"),
            "comps": len(comps) if comps else 0, "orphans": res.get("orphans", [])}


# ---- self-rescheduling launcher (waits on the BED stage's PIPELINE_COMPLETE.txt) ----------------
def _psi_launch_sh(bam_out_root, psi_root, psi_tag, check_min=30, max_wait_hours=336):
    """SELF-RESCHEDULING launcher (mirrors bed_launch.sh). Polls the BED stage's marker
    (<BAM_OUT>/bed/PIPELINE_COMPLETE.txt); when present, runs ./run_psi_pipeline.sh, else re-queues
    itself. Lives entirely on the cluster, so SpliceScout can be CLOSED right after upload."""
    bo = bam_out_root.rstrip("/")
    pr = psi_root.rstrip("/")
    return (
        "#!/usr/bin/env bash\n"
        "# psi_launch.sh -- generated by SpliceScout. Self-rescheduling: checks if BAM->BED finished; if so\n"
        "# launches AltAnalyze (PSI), else re-queues itself. Runs as short LSF jobs.\n"
        'HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
        f"BAM_OUT={cluster_deploy.shq(bo)}\n"
        f"PSI_ROOT={cluster_deploy.shq(pr)}\n"
        f"CHECK_MIN={int(check_min)}\n"
        f"MAX_WAIT_HOURS={int(max_wait_hours)}\n"
        f"JT={cluster_deploy.shq(psi_tag)}\n"
        "command -v bsub >/dev/null 2>&1 || { echo '[psi_launch] no bsub here' >&2; exit 0; }\n"
        "# PSI already finalized -> nothing to do\n"
        'if [ -f "$PSI_ROOT/PIPELINE_COMPLETE.txt" ] || [ -f "$PSI_ROOT/PIPELINE_STALLED.txt" ]; then\n'
        '  echo "[psi_launch] PSI already finalized -> stop"; exit 0\n'
        "fi\n"
        "# BED finished (or stalled -> PSI on whatever BEDs exist) -> TRY to launch. On failure, fall through\n"
        "# to reschedule + retry (run_psi_pipeline.sh is idempotent). Stop only on success.\n"
        'if [ -f "$BAM_OUT/bed/PIPELINE_COMPLETE.txt" ] || [ -f "$BAM_OUT/bed/PIPELINE_STALLED.txt" ]; then\n'
        "  # BED STALLED (not COMPLETE) -> this PSI run is PARTIAL (ran on a subset of samples): mark it.\n"
        '  if [ ! -f "$BAM_OUT/bed/PIPELINE_COMPLETE.txt" ] && [ -f "$BAM_OUT/bed/PIPELINE_STALLED.txt" ]; then\n'
        '    mkdir -p "$PSI_ROOT" 2>/dev/null\n'
        '    echo "upstream BED STALLED at $(date) -- not all samples had BEDs." \\\n'
        '      > "$PSI_ROOT/PIPELINE_INCOMPLETE_UPSTREAM.txt" 2>/dev/null\n'
        "  fi\n"
        '  echo "[psi_launch] BED finished -> launching AltAnalyze (PSI) pipeline"\n'
        '  if bash "$HERE/run_psi_pipeline.sh"; then\n'
        '    echo "[psi_launch] PSI pipeline launched -> stop"; exit 0\n'
        "  fi\n"
        '  echo "[psi_launch] run_psi_pipeline.sh FAILED -> will retry in $CHECK_MIN min" >&2\n'
        "fi\n"
        "# Bounded wait: abort only if past MAX_WAIT_HOURS AND BED's watchdog.log is stale (dead chain).\n"
        'STAMP="$HERE/.launch_first_seen"\n'
        '[ -f "$STAMP" ] || date +%s > "$STAMP" 2>/dev/null\n'
        'now=$(date +%s); first=$(cat "$STAMP" 2>/dev/null || echo "$now")\n'
        'upwd="$BAM_OUT/bed/watchdog.log"; up_age=999999999\n'
        '[ -f "$upwd" ] && up_age=$(( now - $(stat -c %Y "$upwd" 2>/dev/null || echo "$now") ))\n'
        'if [ "$(( now - first ))" -gt "$(( MAX_WAIT_HOURS * 3600 ))" ] && [ "$up_age" -gt "$(( CHECK_MIN * 180 ))" ]; then\n'
        '  mkdir -p "$PSI_ROOT" 2>/dev/null\n'
        '  echo "PSI launcher gave up at $(date): BED never finalized and its watchdog.log went stale (>${MAX_WAIT_HOURS}h)." \\\n'
        '    > "$PSI_ROOT/PIPELINE_LAUNCH_TIMEOUT.txt" 2>/dev/null\n'
        '  echo "[psi_launch] upstream dead -> giving up (PIPELINE_LAUNCH_TIMEOUT.txt written)" >&2; exit 0\n'
        "fi\n"
        "when=$(date -d \"+$CHECK_MIN min\" '+%Y:%m:%d:%H:%M' 2>/dev/null) || "
        "when=$(date -v+\"${CHECK_MIN}\"M '+%Y:%m:%d:%H:%M' 2>/dev/null)\n"
        'bsub -L /bin/bash -n 1 -M 1000 -W 66480 -b "$when" -J "${JT}_launch" \\\n'
        '     -o "$PSI_ROOT/launch.out" -e "$PSI_ROOT/launch.err" \\\n'
        '     "$HERE/psi_launch.sh" >/dev/null 2>&1\n'
        'echo "[psi_launch] not done / will retry -> next check scheduled for $when"\n'
    )


def _write_psi_instructions(P, vals, bam_out_root, psi_root, groups_info):
    if not groups_info:
        gtxt = "groupless (no usable run-table split shipped) -> per-sample PSI table only"
    elif groups_info.get("mode") == "per_condition":
        base = groups_info.get("baseline", "control")
        comps = groups_info.get("comparisons", [])
        gtxt = (f"per-condition: {len(comps)} comparison(s) vs {base} -> "
                + ", ".join(f"PSI.{c}_vs_{base}.txt" for c in comps[:8])
                + (" ..." if len(comps) > 8 else ""))
    else:
        gtxt = f"{groups_info.get('mode', 'binary')} treated-vs-control (from the run table)"
    txt = (
        "AltAnalyze splicing (PSI) bundle (Bulk RNA-seq module)\n"
        "=====================================================\n"
        f"Reads BEDs from : {vals['BED_INPUT_DIR']}\n"
        f"Writes PSI to   : {vals['PIPELINE_ROOT']}/output/AltResults\n"
        f"AltAnalyze      : {vals['ALTANALYZE_HOME']}  (found-on-cluster or uploaded; DB {vals['ALTANALYZE_DB'] or '(beside AltAnalyze)'})\n"
        f"Species         : {vals['SPECIES']}  (organism {vals['ORGANISM']})\n"
        f"Comparison      : {gtxt}\n\n"
        "Autonomous mode launches this automatically AFTER BAM->BED finishes. To run it manually:\n"
        f"  cd {psi_root.rstrip('/')}\n"
        "  chmod +x *.sh\n"
        "  ./run_psi_pipeline.sh\n"
        "Watch:  bash status.sh   (or tail -f <psi_root>/watchdog.log)\n"
        "Done when <psi_root>/PIPELINE_COMPLETE.txt appears.\n"
    )
    with open(os.path.join(P.psi_dir, "RUN_PSI_ON_CLUSTER.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write(txt)


def build_psi_bundle(P, sel, bam_out_root, psi_cfg, download_job_tag="sra", reporter=NULL,
                     group_col=None, labelmap=None):
    """Assemble runtable/psi/ (filled config.sh + scripts + launcher + sample_groups.tsv), zip it, return a
    summary. AltAnalyze itself is resolved/uploaded separately by submit_psi_over_ssh. Returns None if the
    template is missing."""
    if not os.path.isdir(PSI_TEMPLATE_DIR):
        print(f"  PSI BUNDLE: vendored template missing at {PSI_TEMPLATE_DIR} -> skipping")
        return None
    reporter.set_detail("assembling AltAnalyze (PSI) bundle…")
    if os.path.isdir(P.psi_dir):
        shutil.rmtree(P.psi_dir, ignore_errors=True)
    os.makedirs(P.psi_dir, exist_ok=True)

    # top-level scripts (LF), everything except config.sh (generated)
    for name in sorted(os.listdir(PSI_TEMPLATE_DIR)):
        srcf = os.path.join(PSI_TEMPLATE_DIR, name)
        if os.path.isfile(srcf) and name != "config.sh":
            _copy_lf(srcf, os.path.join(P.psi_dir, name))

    organism = star_deploy.detect_organism(P, sel, (psi_cfg or {}).get("ORGANISM"))
    bo = bam_out_root.rstrip("/")
    download_root = os.path.dirname(bo)              # .../STAR_bams -> the per-cell-line run root
    psi_root = f"{download_root}/psi"
    bed_out_dir = f"{download_root}/STAR_beds"       # BED stage's default BED_OUT_DIR

    vals = _resolve_psi_cfg(psi_cfg)
    vals["BED_INPUT_DIR"] = (psi_cfg or {}).get("BED_INPUT_DIR") or bed_out_dir
    vals["PIPELINE_ROOT"] = psi_root
    vals["ORGANISM"] = organism
    vals["SPECIES"] = (psi_cfg or {}).get("SPECIES") or organism_to_species(organism)
    vals["JOB_TAG"] = f"{(download_job_tag or 'sra')}_psi"
    if not str(vals.get("EXPNAME") or "").strip():
        vals["EXPNAME"] = _cl_slug(sel.get("canonical", "splicing")) if sel else "splicing"

    # strip deploy-only keys before writing config.sh
    for k in _DEPLOY_ONLY:
        vals.pop(k, None)

    vals["ALERT_EMAIL"] = vals.get("ALERT_EMAIL") or cluster_deploy._alert_email()   # cluster-side email
    cluster_deploy.bake_diagnose_model(vals)                                         # optional diagnose-AI model path / cache dir
    template = open(os.path.join(PSI_TEMPLATE_DIR, "config.sh"), encoding="utf-8").read()
    with open(os.path.join(P.psi_dir, "config.sh"), "w", encoding="utf-8", newline="\n") as f:
        f.write(cluster_deploy.fill_config(template, vals, numeric=PSI_NUMERIC, defaults=PSI_CONFIG_DEFAULTS))
    with open(os.path.join(P.psi_dir, "psi_launch.sh"), "w", encoding="utf-8", newline="\n") as f:
        f.write(_psi_launch_sh(bam_out_root, psi_root, vals["JOB_TAG"]))

    # comparison groups: BioSample -> group (default drug_treated; Phase B passes the user `group` column)
    groups_info = _write_sample_groups(P, sel, os.path.join(P.psi_dir, "sample_groups.tsv"),
                                       group_col=group_col, labelmap=labelmap)
    if groups_info:
        ncomp = len(groups_info.get("comparisons", []))
        norph = len(groups_info.get("orphans", []))
        print(f"  PSI BUNDLE: sample_groups.tsv -> {groups_info['n']} samples, "
              f"mode={groups_info.get('mode')}, {len(groups_info['groups'])} groups, {ncomp} comparison(s)"
              + (f", {norph} via nearest-neighbor control" if norph else ""))
        if groups_info.get("mode") in ("per_condition", "binary"):
            base = groups_info.get("baseline", "control")
            for comp in groups_info.get("comparisons", [])[:12]:
                print(f"               -> PSI.{comp}_vs_{base}.txt")
            if ncomp > 12:
                print(f"               -> ... (+{ncomp - 12} more)")
    else:
        print("  PSI BUNDLE: no usable run-table grouping -> GROUPLESS PSI (per-sample table only)")

    _write_psi_instructions(P, vals, bam_out_root, psi_root, groups_info)
    cluster_deploy._zip_dir(P.psi_dir, P.psi_bundle_zip)
    print(f"  PSI BUNDLE: species={vals['SPECIES']!r} (organism={organism!r}) tag={vals['JOB_TAG']} "
          f"-> {os.path.basename(P.psi_bundle_zip)}")
    reporter.set_detail(f"PSI bundle ready (species {vals['SPECIES']})")
    return {"bed_input": vals["BED_INPUT_DIR"], "psi_root": psi_root, "job_tag": vals["JOB_TAG"],
            "species": vals["SPECIES"], "organism": organism, "altanalyze_home": vals["ALTANALYZE_HOME"],
            "altanalyze_db": vals["ALTANALYZE_DB"], "grouped": bool(groups_info),
            "altanalyze_local": (psi_cfg or {}).get("ALTANALYZE_LOCAL", "")}


# ---- cluster-side AltAnalyze resolution: find-or-upload -----------------------------------------
def _probe_altanalyze(host, port, user, keyfile, password, alt_home, alt_db):
    """Return True iff AltAnalyze.py AND a database are present on the cluster. Non-fatal -> False on error."""
    db = alt_db.strip() if alt_db else ""
    shq = cluster_deploy.shq
    if db:
        dbtest = f'{{ [ -d {shq(db)} ] || [ -d {shq(alt_home + "/AltDatabase")} ]; }}'
    else:
        dbtest = f'[ -d {shq(alt_home + "/AltDatabase")} ]'
    cmd = f'if [ -s {shq(alt_home + "/AltAnalyze.py")} ] && {dbtest}; then echo PSI_FOUND; else echo PSI_MISSING; fi'
    try:
        if password:
            out = cluster_deploy._ssh_capture_paramiko(host, port, user, password, keyfile, cmd)
        else:
            out = cluster_deploy._ssh_capture_systemssh(host, port, user, keyfile, cmd)
    except Exception as e:
        print(f"  PSI SUBMIT: AltAnalyze probe failed ({e}) -> assuming not found")
        return False
    return "PSI_FOUND" in (out or "")


def _upload_altanalyze(host, port, user, keyfile, password, local_dir, dest_home, reporter):
    """Upload a LOCAL AltAnalyze directory to <dest_home> on the cluster (only when not found there).
    Idempotent: skip if the remote AltAnalyze.py already exists with the same size."""
    local_aa = os.path.join(local_dir, "AltAnalyze.py")
    if not os.path.isfile(local_aa):
        print(f"  PSI SUBMIT: ALTANALYZE_LOCAL has no AltAnalyze.py ({local_aa}) -> cannot upload")
        return False
    shq = cluster_deploy.shq
    reporter.set_detail("uploading AltAnalyze to the cluster (one-time)…")
    if password:
        import paramiko
        cli = paramiko.SSHClient(); cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = {"hostname": host, "port": int(port), "username": user, "timeout": 20}
        if password:
            kw["password"] = password
        if keyfile:
            kw["key_filename"] = keyfile
        cli.connect(**kw)
        try:
            sftp = cli.open_sftp()
            sftp.get_channel().settimeout(600)          # don't hang forever on a stalled SFTP transfer
            for base, _dirs, files in os.walk(local_dir):
                rel = os.path.relpath(base, local_dir)
                rdir = dest_home if rel == "." else f"{dest_home}/" + rel.replace(os.sep, "/")
                _i, _o, _e = cli.exec_command(f"mkdir -p {shq(rdir)}")
                _o.channel.settimeout(60)               # don't block indefinitely on recv_exit_status
                rc = _o.channel.recv_exit_status()
                if rc != 0:
                    err = (_e.read().decode("utf-8", "replace").strip() if _e else "")
                    raise RuntimeError(f"mkdir -p {rdir} failed (rc={rc}): {err}")
                for fn in files:
                    sftp.put(os.path.join(base, fn), f"{rdir}/{fn}")
            sftp.close()
        finally:
            cli.close()
    else:
        target = f"{user}@{host}"
        common = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20"]
        ssh = ["ssh", "-p", str(port)] + common + (["-i", keyfile] if keyfile else [])
        scp = ["scp", "-r", "-P", str(port)] + common + (["-i", keyfile] if keyfile else [])
        cluster_deploy._run(ssh + [target, f"mkdir -p {shq(dest_home)}"])
        items = [os.path.join(local_dir, n) for n in sorted(os.listdir(local_dir))]
        cluster_deploy._run(scp + items + [f"{target}:{dest_home}/"], timeout=3600)
    print(f"  PSI SUBMIT: uploaded local AltAnalyze -> {dest_home}")
    return True


def submit_psi_over_ssh(P, cluster_cfg, secrets, bam_out_root, reporter=NULL, prior_skipped=False):
    """Upload the PSI bundle + resolve AltAnalyze (find-on-cluster or upload) and submit the auto-chain
    launcher (waits on BED). Non-fatal. prior_skipped=True (phase-range START at PSI): BED was skipped, so
    pre-create <BAM_OUT>/bed/PIPELINE_COMPLETE.txt so PSI runs immediately on existing BEDs."""
    cfg = cluster_cfg or {}
    secrets = secrets or {}
    host = (cfg.get("ssh_host") or "").strip()
    user = (cfg.get("ssh_user") or "").strip()
    port = str(cfg.get("ssh_port") or "22").strip() or "22"
    keyfile = (cfg.get("ssh_key") or "").strip()
    password = secrets.get("ssh_password") or ""
    dl_tag = (cfg.get("JOB_TAG") or "sra").strip() or "sra"
    psi_tag = f"{dl_tag}_psi"
    bo = bam_out_root.rstrip("/")
    psi_root = f"{os.path.dirname(bo)}/psi"

    if not host or not user:
        print("  PSI SUBMIT: missing SSH host/user -> bundle still downloadable")
        return {"submitted": False, "reason": "missing host/user",
                "diagnosis": cluster_deploy.diagnose_failure("missing host/user")}
    if not os.path.isdir(P.psi_dir):
        return {"submitted": False, "reason": "no bundle",
                "diagnosis": cluster_deploy.diagnose_failure("", "no bundle")}

    cfg_path = os.path.join(P.psi_dir, "config.sh")
    alt_home = (_read_cfg_var(cfg_path, "ALTANALYZE_HOME") or "").strip()
    if not alt_home:                                    # portable default: under the pipeline root (psi_root)
        alt_home = f"{psi_root}/altanalyze_home"
    alt_db = _read_cfg_var(cfg_path, "ALTANALYZE_DB") or ""
    local_dir = (cfg.get("ALTANALYZE_LOCAL") or "").strip()

    # resolve AltAnalyze BEFORE uploading the bundle, so config.sh ships with the correct ALTANALYZE_HOME.
    found = _probe_altanalyze(host, port, user, keyfile, password, alt_home, alt_db)
    if found:
        print(f"  PSI SUBMIT: AltAnalyze found on cluster at {alt_home} -> using in place (no upload)")
    elif local_dir and os.path.isdir(local_dir):
        dest_home = f"{psi_root}/altanalyze_home"
        try:
            if _upload_altanalyze(host, port, user, keyfile, password, local_dir, dest_home, reporter):
                _set_config_var(cfg_path, "ALTANALYZE_HOME", dest_home)
                alt_home = dest_home
        except Exception as up_err:
            print(f"  PSI SUBMIT: AltAnalyze upload failed ({up_err}) -> stage will report it / retry")
    else:
        print(f"  PSI SUBMIT: AltAnalyze NOT found at {alt_home} and no ALTANALYZE_LOCAL provided -> the "
              "stage's setup.sh will flag it (set ALTANALYZE_HOME to a cluster install, or point "
              "ALTANALYZE_LOCAL at a local copy to upload).")

    shq = cluster_deploy.shq
    # DETACH the launcher bsub (setsid, backgrounded in a subshell so only the bsub is async) so the deploy
    # ssh returns immediately instead of hanging on "Pending job threshold reached. Retrying in 60s" under a
    # saturated pending-job quota. See star_deploy.submit_star_over_ssh for the full rationale.
    _lo = shq(psi_root + '/launch.out')
    launch = (
        f"( setsid bsub -L /bin/bash -n 1 -M 1000 -W 66480 -J {shq(psi_tag + '_launch')} "
        f"-o {_lo} -e {shq(psi_root + '/launch.err')} {shq(psi_root + '/psi_launch.sh')} "
        f"</dev/null >>{_lo} 2>&1 & )"
    )
    if prior_skipped:
        # BED was phase-range skipped -> the launcher polls <BAM_OUT>/bed/PIPELINE_COMPLETE.txt which no BED
        # run will write. Pre-create it so PSI runs on the BEDs already present -- but ONLY if BED never ran
        # here (no marker AND no watchdog.log). If a BED stage is RUNNING/finished (watchdog.log exists),
        # touching its completion marker would HALT it + run PSI on partial BEDs, so we leave PSI to wait.
        _bm = bo + "/bed/PIPELINE_COMPLETE.txt"; _bl = bo + "/bed/watchdog.log"
        launch = (f"if [ ! -f {shq(_bm)} ] && [ ! -f {shq(_bl)} ]; then mkdir -p {shq(bo + '/bed')} && "
                  f"touch {shq(_bm)}; fi; " + launch)
        print(f"  PSI SUBMIT: BED phase-skipped -> pre-touch {bo}/bed/PIPELINE_COMPLETE.txt ONLY if no BED ran there "
              "(else PSI waits for the running/finished BED)")
    print(f"=== PSI SUBMIT: {user}@{host}:{port} -> {psi_root} ===")
    try:
        if password:
            try:
                import paramiko  # noqa: F401
            except Exception:
                raise RuntimeError("password auth needs paramiko (pip install paramiko) or use an SSH key")
            res = cluster_deploy._submit_paramiko(P, host, port, user, password, keyfile, psi_root,
                                                  reporter, src_dir=P.psi_dir, launch_cmd=launch)
        else:
            res = cluster_deploy._submit_systemssh(P, host, port, user, keyfile, psi_root,
                                                   reporter, src_dir=P.psi_dir, launch_cmd=launch)
        print(f"  PSI SUBMIT: self-rescheduling launcher armed on {host} — AltAnalyze starts on the cluster "
              "when BAM->BED finishes (safe to close SpliceScout now)")
        res = dict(res or {}); res["altanalyze_home"] = alt_home; res["altanalyze_found"] = found
        return res
    except Exception as e:
        output = getattr(e, "output", "") or str(e)
        diag = cluster_deploy.diagnose_failure(output, str(e))
        print(f"  PSI SUBMIT FAILED: {e}  -> {diag['title']}")
        print("  -> the PSI bundle is still downloadable (RUN_PSI_ON_CLUSTER.txt).")
        reporter.set_detail(f"PSI submit failed: {diag['title']}")
        return {"submitted": False, "reason": str(e), "diagnosis": diag}


def _read_cfg_var(cfg_path, name):
    try:
        m = re.search(rf'(?m)^{re.escape(name)}="?(.*?)"?\s*$', open(cfg_path, encoding="utf-8").read())
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return ""
