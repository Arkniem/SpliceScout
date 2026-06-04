# -*- coding: utf-8 -*-
"""
Stages 13-14 — CLUSTER HANDOFF.

The deep-dive produced per-study accession lists at runtable/by_study/<GSE>/SraAccList.txt. The user's
LSF download pipeline (vendored into cluster_template/) consumes exactly that layout, configured only
through config.sh.

build_bundle  (stage 'cluster_bundle'): assemble a ready-to-run bundle under runtable/cluster/ —
  the vendored scripts + a config.sh filled from the user's settings + the PER-STUDY by_study/<GSE>/
  folders (each holding only that study's SraAccList.txt; the combined all-runs list is NEVER placed
  in by_study/, so the cluster downloads/converts each study INDEPENDENTLY) + RUN_ON_CLUSTER.txt, all
  zipped to cluster_bundle.zip.

submit_over_ssh (stage 'cluster_submit', autonomous only): ssh mkdir -> scp the bundle -> ssh run
  ./run_pipeline.sh. System ssh/scp (key/agent) by default; paramiko if a password is supplied and
  importable. Non-fatal: any failure leaves the downloadable bundle intact.
"""
import os
import re
import shutil
import subprocess
import zipfile

from progress import NULL

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(HERE, "cluster_template")

# config.sh EDIT-THESE values we fill: name -> default. Numerics are written unquoted.
CONFIG_DEFAULTS = {
    "PIPELINE_ROOT": "/data/CHANGE_ME/A_PROJECT_FOLDER",
    "SCRATCH_DIR": "/scratch/$USER",
    "SRATOOLKIT_MODULE": "sratoolkit/3.0.0",
    "ASPERA_MODULE": "aspera/3.9.1",
    "LSF_QUEUE": "",
    "THREADS": 6,
    "MEM_MB": 32000,
    "WALL": "50:00",
    "PREFETCH_MEM_MB": 132000,
    "WATCHDOG_INTERVAL_MIN": 30,
    "JOB_TAG": "sra",
}
NUMERIC = {"THREADS", "MEM_MB", "PREFETCH_MEM_MB", "WATCHDOG_INTERVAL_MIN"}


# ---------- config.sh generation ----------
def _shval(name, value):
    if name in NUMERIC:
        try:
            return str(int(value))
        except Exception:
            return str(CONFIG_DEFAULTS[name])
    s = str(value).replace('"', '\\"')
    return f'"{s}"'


def fill_config(template_text, vals):
    """Replace each EDIT-THESE assignment in the vendored config.sh with the user's value."""
    out = template_text
    for name, value in vals.items():
        rep = f"{name}={_shval(name, value)}"
        out = re.sub(rf"(?m)^{re.escape(name)}=.*$", lambda m, r=rep: r, out, count=1)
    return out


def _resolve_cfg(cluster_cfg):
    cfg = cluster_cfg or {}
    vals = dict(CONFIG_DEFAULTS)
    for k in CONFIG_DEFAULTS:
        if k in cfg and cfg[k] is not None and str(cfg[k]).strip() != "":
            vals[k] = cfg[k]
    if not str(vals.get("PIPELINE_ROOT", "")).strip():
        vals["PIPELINE_ROOT"] = CONFIG_DEFAULTS["PIPELINE_ROOT"]
    return vals


def _slug(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name)).strip("_") or "cellline"


def _effective_root(cluster_cfg, sel):
    """The form's PIPELINE_ROOT + a per-cell-line subfolder, so each run is ISOLATED — its bundle
    deploys to e.g. /data/mylab/sra/UMUC9 and only that line's studies live there. No merging with
    studies left over from previous runs to the same parent path; non-destructive to other runs."""
    base = _resolve_cfg(cluster_cfg)["PIPELINE_ROOT"].rstrip("/")
    line = _slug((sel or {}).get("canonical", "")) if sel else ""
    return f"{base}/{line}" if line else base


def _read_config_root(P):
    """The PIPELINE_ROOT actually baked into the generated bundle's config.sh (single source of truth
    for where submit_over_ssh uploads + launches)."""
    cfgsh = os.path.join(P.cluster_dir, "config.sh")
    if os.path.exists(cfgsh):
        m = re.search(r'(?m)^PIPELINE_ROOT="?(.*?)"?\s*$', open(cfgsh, encoding="utf-8").read())
        if m and m.group(1).strip():
            return m.group(1).strip()
    return None


# ---------- stage 13: build the bundle ----------
def _write_instructions(P, vals, n_studies, n_acc):
    root = vals["PIPELINE_ROOT"]
    txt = (
        "GEO -> SRA cluster download bundle\n"
        "==================================\n"
        f"{n_studies} studies, {n_acc} run accessions, organized one folder per study under by_study/.\n\n"
        "Run on your LSF cluster:\n"
        f"  1. Put this bundle on the cluster at:  {root}\n"
        f"       unzip cluster_bundle.zip -d {root}\n"
        f"     (or:  scp -r cluster/*  <user>@<host>:{root}/ )\n"
        "  2. On the LSF submit host:\n"
        f"       cd {root}\n"
        "       chmod +x *.sh\n"
        "       ./run_pipeline.sh\n"
        f"  3. Watch:  ./status.sh   (or  tail -f {root}/watchdog.log )\n"
        f"     Done when  {root}/PIPELINE_COMPLETE.txt  appears.\n\n"
        f"This run is isolated in its own per-cell-line folder ({root}), so it never mixes with the\n"
        "studies from any other run that targets the same parent path.\n\n"
        f"config.sh is pre-filled (PIPELINE_ROOT={root}, THREADS={vals['THREADS']}, "
        f"MEM_MB={vals['MEM_MB']}, WALL={vals['WALL']}). Edit it if your cluster's module names / "
        "queue / limits differ. Each study under by_study/ is downloaded and converted INDEPENDENTLY "
        "(per-study prefetch jobs, per-accession fasterq-dump) -> per-study *.fastq.gz.\n"
    )
    with open(os.path.join(P.cluster_dir, "RUN_ON_CLUSTER.txt"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(txt)


def _zip_dir(src_dir, zip_path):
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(src_dir):
            for fn in files:
                fp = os.path.join(root, fn)
                z.write(fp, os.path.relpath(fp, src_dir))   # bundle contents at the zip root


def build_bundle(P, sel, cluster_cfg, reporter=NULL):
    """Assemble runtable/cluster/ (+ zip). Returns a summary dict."""
    if not os.path.isdir(TEMPLATE_DIR):
        print(f"  CLUSTER BUNDLE: vendored template missing at {TEMPLATE_DIR} -> skipping")
        return None
    reporter.set_detail("assembling cluster bundle…")
    # clean rebuild
    if os.path.isdir(P.cluster_dir):
        shutil.rmtree(P.cluster_dir, ignore_errors=True)
    os.makedirs(P.cluster_dir, exist_ok=True)

    # vendored scripts (everything except config.sh, which we generate) -> normalize to LF so the
    # bundle runs on the cluster even if a vendored file picked up CRLF on Windows (\r breaks bash)
    for name in sorted(os.listdir(TEMPLATE_DIR)):
        srcf = os.path.join(TEMPLATE_DIR, name)
        if os.path.isfile(srcf) and name != "config.sh":
            txt = open(srcf, encoding="utf-8", errors="replace").read().replace("\r\n", "\n").replace("\r", "\n")
            with open(os.path.join(P.cluster_dir, name), "w", encoding="utf-8", newline="\n") as f:
                f.write(txt)

    # filled config.sh (LF newlines — it runs on the cluster). PIPELINE_ROOT gets a per-cell-line
    # subfolder so this run is isolated and never mixes with studies from previous runs.
    vals = _resolve_cfg(cluster_cfg)
    vals["PIPELINE_ROOT"] = _effective_root(cluster_cfg, sel)
    template_text = open(os.path.join(TEMPLATE_DIR, "config.sh"), encoding="utf-8").read()
    with open(os.path.join(P.cluster_dir, "config.sh"), "w", encoding="utf-8", newline="\n") as f:
        f.write(fill_config(template_text, vals))

    # PER-STUDY accession lists only (never the combined list)
    bydir = os.path.join(P.cluster_dir, "by_study")
    os.makedirs(bydir, exist_ok=True)
    n_studies = n_acc = 0
    if os.path.isdir(P.by_study_dir):
        for gse in sorted(os.listdir(P.by_study_dir)):
            src_list = os.path.join(P.by_study_dir, gse, "SraAccList.txt")
            if os.path.isfile(src_list):
                os.makedirs(os.path.join(bydir, gse), exist_ok=True)
                # normalize to LF, one accession per line
                accs = [ln.strip() for ln in open(src_list, encoding="utf-8") if ln.strip()]
                with open(os.path.join(bydir, gse, "SraAccList.txt"), "w",
                          encoding="utf-8", newline="\n") as f:
                    f.write("\n".join(accs) + ("\n" if accs else ""))
                n_studies += 1
                n_acc += len(accs)

    _write_instructions(P, vals, n_studies, n_acc)
    _zip_dir(P.cluster_dir, P.cluster_bundle_zip)
    print(f"  CLUSTER BUNDLE: {n_studies} studies, {n_acc} accessions (split per study) "
          f"-> {os.path.basename(P.cluster_bundle_zip)}")
    reporter.set_detail(f"{n_studies} studies / {n_acc} accessions zipped")
    return {"n_studies": n_studies, "n_accessions": n_acc, "pipeline_root": vals["PIPELINE_ROOT"]}


# ---------- stage 14: autonomous SSH submit ----------
def _run(argv, timeout=180):
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    out = (p.stdout or "") + (p.stderr or "")
    if out.strip():
        print("    " + out.strip().replace("\n", "\n    "))
    if p.returncode != 0:
        raise RuntimeError(f"{os.path.basename(argv[0])} exit {p.returncode}: {out.strip()[:300]}")
    return out


def _submit_systemssh(P, host, port, user, keyfile, root, reporter):
    target = f"{user}@{host}"
    common = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20"]
    ssh = ["ssh", "-p", str(port)] + common + (["-i", keyfile] if keyfile else [])
    scp = ["scp", "-r", "-P", str(port)] + common + (["-i", keyfile] if keyfile else [])
    _run(ssh + [target, f"mkdir -p '{root}'"])
    items = [os.path.join(P.cluster_dir, n) for n in sorted(os.listdir(P.cluster_dir))]
    _run(scp + items + [f"{target}:{root}/"], timeout=600)
    out = _run(ssh + [target, f"cd '{root}' && chmod +x *.sh && ./run_pipeline.sh"], timeout=600)
    print(f"  CLUSTER SUBMIT: launched on {target}:{root}")
    reporter.set_detail(f"launched on {host}")
    return {"submitted": True, "host": host, "root": root, "output": out[-2000:]}


def _submit_paramiko(P, host, port, user, password, keyfile, root, reporter):
    import paramiko
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = {"hostname": host, "port": int(port), "username": user, "timeout": 20}
    if password:
        kw["password"] = password
    if keyfile:
        kw["key_filename"] = keyfile
    cli.connect(**kw)

    def ex(cmd, timeout=600):
        _in, _out, _err = cli.exec_command(cmd, timeout=timeout)
        out = _out.read().decode("utf-8", "replace") + _err.read().decode("utf-8", "replace")
        rc = _out.channel.recv_exit_status()
        if out.strip():
            print("    " + out.strip().replace("\n", "\n    "))
        if rc != 0:
            raise RuntimeError(f"remote exit {rc}: {out.strip()[:300]}")
        return out

    try:
        ex(f"mkdir -p '{root}'")
        sftp = cli.open_sftp()
        for base, _dirs, files in os.walk(P.cluster_dir):
            rel = os.path.relpath(base, P.cluster_dir)
            rdir = root if rel == "." else f"{root}/" + rel.replace(os.sep, "/")
            try:
                sftp.mkdir(rdir)
            except Exception:
                pass
            for fn in files:
                sftp.put(os.path.join(base, fn), f"{rdir}/{fn}")
        sftp.close()
        out = ex(f"cd '{root}' && chmod +x *.sh && ./run_pipeline.sh")
    finally:
        cli.close()
    print(f"  CLUSTER SUBMIT: launched on {user}@{host}:{root}")
    reporter.set_detail(f"launched on {host}")
    return {"submitted": True, "host": host, "root": root, "output": out[-2000:]}


def submit_over_ssh(P, cluster_cfg, secrets, reporter=NULL):
    """Upload the bundle and launch ./run_pipeline.sh on the cluster. Non-fatal on failure."""
    cfg = cluster_cfg or {}
    secrets = secrets or {}
    host = (cfg.get("ssh_host") or "").strip()
    user = (cfg.get("ssh_user") or "").strip()
    port = str(cfg.get("ssh_port") or "22").strip() or "22"
    keyfile = (cfg.get("ssh_key") or "").strip()
    password = secrets.get("ssh_password") or ""
    # upload to exactly the PIPELINE_ROOT the bundle's config.sh declares (the per-cell-line subfolder)
    root = _read_config_root(P) or _resolve_cfg(cfg)["PIPELINE_ROOT"]

    if not host or not user:
        print("  CLUSTER SUBMIT: missing SSH host/user -> skipped (bundle still downloadable)")
        reporter.set_detail("missing SSH host/user — skipped")
        return {"submitted": False, "reason": "missing host/user"}
    if not os.path.isdir(P.cluster_dir):
        print("  CLUSTER SUBMIT: no bundle to upload -> skipped")
        return {"submitted": False, "reason": "no bundle"}

    print(f"=== CLUSTER SUBMIT: {user}@{host}:{port} -> {root} ===")
    try:
        if password:
            try:
                import paramiko  # noqa: F401
            except Exception:
                raise RuntimeError("password auth needs paramiko (pip install paramiko) "
                                   "or use an SSH key / agent instead")
            return _submit_paramiko(P, host, port, user, password, keyfile, root, reporter)
        return _submit_systemssh(P, host, port, user, keyfile, root, reporter)
    except Exception as e:
        print(f"  CLUSTER SUBMIT FAILED: {e}")
        print("  -> the bundle is still downloadable; follow RUN_ON_CLUSTER.txt to run it manually.")
        reporter.set_detail(f"submit failed: {e}")
        return {"submitted": False, "reason": str(e)}


def main():
    import argparse
    import json
    from pipeline_paths import Paths
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--pipeline-root", default=None)
    a = ap.parse_args()
    P = Paths(a.run_dir).ensure_dirs()
    sel = json.load(open(P.cellline_selection, encoding="utf-8")) if os.path.exists(P.cellline_selection) else {}
    cfg = {"PIPELINE_ROOT": a.pipeline_root} if a.pipeline_root else {}
    print(build_bundle(P, sel, cfg))


if __name__ == "__main__":
    main()
