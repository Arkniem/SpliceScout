# -*- coding: utf-8 -*-
"""
AltAnalyze splicing (PSI) stage deploy -- the analysis step that runs AFTER BAM->BED on the cluster.

When the analysis module is "bulk_rna_seq" and the cluster is on, this fills the vendored psi_template/
config.sh from the form + the run, ships it to <download_root>/psi, and AUTO-CHAINS it: a launcher job
(psi_launch.sh) waits for the BED stage's PIPELINE_COMPLETE.txt, then runs ./run_psi_pipeline.sh, which
runs ONE AltAnalyze job over the whole BED dir -> a per-sample PSI table (+ a differential dPSI comparison
when a usable 2-group split exists).

AltAnalyze itself is NOT shipped in the bundle (it is multi-GB with its species database). Instead, at
submit time we PROBE the cluster: if AltAnalyze + its database are found at ALTANALYZE_HOME (default the
lab install), we use them in place; otherwise, if the user pointed us at a LOCAL AltAnalyze copy, we upload
it once to <psi_root>/altanalyze_home (idempotent). An explicit ALTANALYZE_DB path override is supported
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
    "ALTANALYZE_HOME": "/data/salomonis2/software/AltAnalyze-91/AltAnalyze",
    "ALTANALYZE_DB": "",            # "" => $ALTANALYZE_HOME/AltDatabase ; else an external DB path
    "ORGANISM": "Homo sapiens",
    "SPECIES": "",                  # "" => config.sh derives from ORGANISM (default Hs)
    "PSI_OUT": "",                  # "" => config.sh derives $PIPELINE_ROOT/output
    "EXPNAME": "splicing",
    "RUN_GOELITE": "0",
    "GROUP_KEY_SUFFIX": ".bed",
    "THREADS": 4,
    "MEM_MB": 128000,
    "WALL": "10:00",
    "LSF_QUEUE": "",
    "JOB_TAG": "psi",
    "WATCHDOG_INTERVAL_MIN": 30,
    "MAX_RESUBMITS": 2,
    "CLEANUP_TOOLS_WHEN_DONE": "0",
    "PYTHON_MODULE": "python/2.7.5",
    "SAMTOOLS_MODULE": "samtools",
    "R_MODULE": "R",
}
PSI_NUMERIC = {"THREADS", "MEM_MB", "WATCHDOG_INTERVAL_MIN", "MAX_RESUBMITS"}

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
# Phase A default: collapse the annotated run table's per-run `drug_treated` column to one label per
# BioSample (the BED stem), Not Drug Treated=group 1 (baseline), Drug Treated=group 2; Undetermined
# dropped. Phase B replaces this with the user-defined `group` column (see group_assign.py).
_DRUG_LABELMAP = {"not drug treated": (1, "not_drug_treated"), "drug treated": (2, "drug_treated")}


def _write_sample_groups(P, sel, dest, group_col=None, labelmap=None):
    """Write sample_groups.tsv (BioSample<TAB>group_num<TAB>label) from the annotated run table.
    Returns a dict {n, groups:{num:count}} or None if no usable table/column. group_col/labelmap let
    Phase B pass the user `group` column; default = drug_treated."""
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
    val_col = group_col or _find_col(header, ("drug_treated",))
    if not bs_col or not val_col or val_col not in header:
        return None

    # collapse runs -> BioSample (majority vote of the per-run label; ties/unknown dropped)
    per = defaultdict(Counter)
    for r in rows:
        bs = (r.get(bs_col) or "").strip()
        val = (r.get(val_col) or "").strip()
        if bs and val:
            per[bs][val] += 1

    out = []
    counts = Counter()
    if group_col and labelmap:
        # Phase B: arbitrary user labels already numbered 1..N in `labelmap` (label -> (num, name))
        lm = {k.lower(): v for k, v in labelmap.items()}
    else:
        lm = _DRUG_LABELMAP
    for bs, c in per.items():
        known = [(lab, n) for lab, n in c.most_common() if lab.lower() in lm]
        if not known:
            continue
        num, name = lm[known[0][0].lower()]
        out.append((bs, num, name))
        counts[num] += 1
    if not out:
        return None
    with open(dest, "w", encoding="utf-8", newline="\n") as f:
        for bs, num, name in sorted(out):
            f.write(f"{bs}\t{num}\t{name}\n")
    return {"n": len(out), "groups": dict(counts)}


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
        'bsub -L /bin/bash -n 1 -M 1000 -W 120 -b "$when" -J "${JT}_launch" \\\n'
        '     -o "$PSI_ROOT/launch.out" -e "$PSI_ROOT/launch.err" \\\n'
        '     "$HERE/psi_launch.sh" >/dev/null 2>&1\n'
        'echo "[psi_launch] not done / will retry -> next check scheduled for $when"\n'
    )


def _write_psi_instructions(P, vals, bam_out_root, psi_root, groups_info):
    gtxt = ("default treated-vs-control (from the run table)" if groups_info
            else "groupless (no usable run-table split shipped) -> per-sample PSI table only")
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

    template = open(os.path.join(PSI_TEMPLATE_DIR, "config.sh"), encoding="utf-8").read()
    with open(os.path.join(P.psi_dir, "config.sh"), "w", encoding="utf-8", newline="\n") as f:
        f.write(cluster_deploy.fill_config(template, vals, numeric=PSI_NUMERIC, defaults=PSI_CONFIG_DEFAULTS))
    with open(os.path.join(P.psi_dir, "psi_launch.sh"), "w", encoding="utf-8", newline="\n") as f:
        f.write(_psi_launch_sh(bam_out_root, psi_root, vals["JOB_TAG"]))

    # comparison groups: BioSample -> group (default drug_treated; Phase B passes the user `group` column)
    groups_info = _write_sample_groups(P, sel, os.path.join(P.psi_dir, "sample_groups.tsv"),
                                       group_col=group_col, labelmap=labelmap)
    if groups_info:
        print(f"  PSI BUNDLE: sample_groups.tsv -> {groups_info['n']} samples, groups={groups_info['groups']}")
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
            for base, _dirs, files in os.walk(local_dir):
                rel = os.path.relpath(base, local_dir)
                rdir = dest_home if rel == "." else f"{dest_home}/" + rel.replace(os.sep, "/")
                _i, _o, _e = cli.exec_command(f"mkdir -p {shq(rdir)}"); _o.channel.recv_exit_status()
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
    alt_home = _read_cfg_var(cfg_path, "ALTANALYZE_HOME") or PSI_CONFIG_DEFAULTS["ALTANALYZE_HOME"]
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
    launch = (
        f"bsub -L /bin/bash -n 1 -M 1000 -W 120 -J {shq(psi_tag + '_launch')} "
        f"-o {shq(psi_root + '/launch.out')} -e {shq(psi_root + '/launch.err')} {shq(psi_root + '/psi_launch.sh')}"
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
