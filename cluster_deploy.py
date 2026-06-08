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


# A "safe expandable" config value: a pure path whose only `$` uses are allowlisted shell vars
# ($USER/$HOME/$SCRATCH/$TMPDIR) and which contains NO other shell-active char. Such values keep
# expansion (e.g. SCRATCH_DIR="/scratch/$USER"); everything else is escaped to an inert literal.
_SAFE_EXPAND_RE = re.compile(r'^(?:[A-Za-z0-9_./:+-]|\$(?:USER|HOME|SCRATCH|SCRATCHDIR|TMPDIR)\b)*$')


def shq(value):
    """POSIX single-quote a value for safe interpolation into a REMOTE SHELL COMMAND line.
    Closes shell injection: $, `, $(...), ;, |, &, spaces, embedded quotes are all inert."""
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


# ---------- config.sh generation ----------
def _shval(name, value, numeric=NUMERIC, defaults=CONFIG_DEFAULTS):
    if name in numeric:
        try:
            return str(int(value))
        except Exception:
            return str(defaults.get(name, value))
    s = str(value)
    # Keep DOUBLE quotes (so every config.sh reader stays unchanged), but neutralize every
    # shell-active character inside them UNLESS the value is a trusted pure path that legitimately
    # relies on an allowlisted $VAR (then we leave $ alone so it still expands when sourced).
    if "$" in s and _SAFE_EXPAND_RE.match(s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    return f'"{s}"'


def fill_config(template_text, vals, numeric=NUMERIC, defaults=CONFIG_DEFAULTS):
    """Replace each EDIT-THESE assignment in the vendored config.sh with the user's value.
    `numeric`/`defaults` let other templates (e.g. the STAR config.sh) reuse this with their own var
    sets; the download call sites keep the module defaults, so their output is unchanged."""
    out = template_text
    for name, value in vals.items():
        rep = f"{name}={_shval(name, value, numeric, defaults)}"
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
class _SubmitError(RuntimeError):
    """Carries the full ssh/scp/remote output so diagnose_failure can read it."""
    def __init__(self, message, output=""):
        super().__init__(message)
        self.output = output or message


# ---------- log understanding: turn ssh/scp/remote output into a diagnosis ----------
def diagnose_failure(text, reason=""):
    """Read the ssh/scp/remote log and classify WHY the cluster upload failed.

    Returns {category, title, detail, suspect_fields} so the caller can tell the user what
    likely went wrong and which inputs to correct — then retry just the upload, no full rerun.
    suspect_fields use the same keys as the form/cluster_cfg:
    ssh_host | ssh_port | ssh_user | ssh_key | ssh_password | PIPELINE_ROOT.
    """
    t = ((text or "") + " " + (reason or "")).lower()

    def D(category, title, detail, fields):
        return {"category": category, "title": title, "detail": detail, "suspect_fields": fields}

    if "missing host/user" in t:
        return D("missing_credentials", "No SSH host/username given",
                 "Autonomous upload needs an SSH host and username — enter them to upload now.",
                 ["ssh_host", "ssh_user"])
    if any(s in t for s in ("could not resolve", "name or service not known", "nodename nor servname",
                            "name does not resolve", "temporary failure in name resolution")):
        return D("dns", "Host name could not be resolved",
                 "The SSH host didn't resolve. Check for a typo, use the cluster's real submit/login "
                 "host, and connect to the VPN if it's an internal host.", ["ssh_host"])
    if "no route to host" in t or "network is unreachable" in t:
        return D("network", "No network route to the host",
                 "The host is unreachable from here — usually a VPN/firewall issue or a wrong host.",
                 ["ssh_host"])
    if "connection refused" in t or "unable to connect to port" in t:
        return D("refused", "Connection refused",
                 "Nothing is accepting SSH at that host/port. Check the port (22 by default) and that "
                 "you're using the SSH-enabled host.", ["ssh_host", "ssh_port"])
    if "bad port" in t or "invalid port" in t:
        return D("port", "Invalid SSH port",
                 "The SSH port isn't a valid number (22 is the default).", ["ssh_port"])
    if any(s in t for s in ("timed out", "timeout", "error reading ssh protocol banner")):
        return D("timeout", "Connection timed out",
                 "Couldn't reach the host before timing out — usually VPN/firewall, or a wrong host/port.",
                 ["ssh_host", "ssh_port"])
    if "host key verification failed" in t or "remote host identification has changed" in t:
        return D("hostkey", "Host key verification failed",
                 "The host's SSH key changed or isn't trusted. Remove the stale entry for this host "
                 "from ~/.ssh/known_hosts, then retry.", [])
    if any(s in t for s in ("no such identity", "identity file", "could not load key",
                            "error loading key", "not accessible")):
        return D("keyfile", "Private key file problem",
                 "The private key path looks wrong or unreadable. Fix the key file path, or leave it "
                 "blank to use your SSH agent / a password.", ["ssh_key"])
    if "command not found" in t and ("bsub" in t or "run_pipeline" in t or "sra" in t):
        return D("wrong_submit_host", "Cluster tools not on PATH (wrong host?)",
                 "The job scheduler (e.g. bsub) wasn't found — you may be on the login node instead of "
                 "the LSF submit host. Use the submit host (e.g. bmiclusterp-head).", ["ssh_host"])
    if "mkdir" in t and ("permission denied" in t or "read-only" in t or "cannot create" in t):
        return D("root_perms", "Can't create the cluster folder",
                 "Couldn't create PIPELINE_ROOT on the cluster (no write permission or bad path). "
                 "Point it at a directory you can write to.", ["PIPELINE_ROOT"])
    if any(s in t for s in ("permission denied (publickey", "permission denied, please try again",
                            "authentication failed", "too many authentication failures",
                            "no authentication methods", "permission denied (")):
        return D("auth", "Authentication failed",
                 "The username/key/password was rejected. Check the username and either fix the key "
                 "file or provide the account password.", ["ssh_user", "ssh_key", "ssh_password"])
    if "needs paramiko" in t or ("paramiko" in t and "install" in t):
        return D("paramiko", "Password auth needs paramiko",
                 "Password login needs the 'paramiko' package (pip install paramiko), or switch to an "
                 "SSH key / agent and leave the password blank.", ["ssh_password", "ssh_key"])
    if "quota exceeded" in t or "no space left" in t or "disk quota" in t:
        return D("space", "Out of space / quota on the cluster",
                 "The cluster is out of disk or over quota at that path. Use a different PIPELINE_ROOT.",
                 ["PIPELINE_ROOT"])
    if "no such file or directory" in t and "mkdir" not in t:
        return D("root_path", "Path not found on the cluster",
                 "A path didn't exist on the cluster. Check PIPELINE_ROOT (and the private key path).",
                 ["PIPELINE_ROOT", "ssh_key"])
    if (any(s in t for s in ("winerror 2", "cannot find the file", "not recognized as",
                             "[errno 2]")) and "mkdir" not in t):
        return D("local_ssh", "ssh/scp not available locally",
                 "The local 'ssh'/'scp' command wasn't found. Install an OpenSSH client, or just use "
                 "the downloadable bundle instead.", [])
    return D("unknown", "Cluster upload failed",
             "Couldn't complete the upload — review the log line above, then correct the host, "
             "username, port, key, password, or PIPELINE_ROOT and retry.",
             ["ssh_host", "ssh_user", "ssh_port", "ssh_key", "ssh_password", "PIPELINE_ROOT"])


def _run(argv, timeout=180):
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    out = (p.stdout or "") + (p.stderr or "")
    if out.strip():
        print("    " + out.strip().replace("\n", "\n    "))
    if p.returncode != 0:
        raise _SubmitError(f"{os.path.basename(argv[0])} exit {p.returncode}: {out.strip()[:400]}", out)
    return out


def _submit_systemssh(P, host, port, user, keyfile, root, reporter, src_dir=None,
                      launch_cmd="./run_pipeline.sh"):
    src_dir = src_dir or P.cluster_dir
    target = f"{user}@{host}"
    common = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20"]
    ssh = ["ssh", "-p", str(port)] + common + (["-i", keyfile] if keyfile else [])
    scp = ["scp", "-r", "-P", str(port)] + common + (["-i", keyfile] if keyfile else [])
    _run(ssh + [target, f"mkdir -p {shq(root)}"])
    items = [os.path.join(src_dir, n) for n in sorted(os.listdir(src_dir))]
    _run(scp + items + [f"{target}:{root}/"], timeout=600)
    out = _run(ssh + [target, f"cd {shq(root)} && chmod +x *.sh && {launch_cmd}"], timeout=600)
    print(f"  CLUSTER SUBMIT: launched on {target}:{root}")
    reporter.set_detail(f"launched on {host}")
    return {"submitted": True, "host": host, "root": root, "output": out[-2000:]}


def _submit_paramiko(P, host, port, user, password, keyfile, root, reporter, src_dir=None,
                     launch_cmd="./run_pipeline.sh"):
    src_dir = src_dir or P.cluster_dir
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
            raise _SubmitError(f"remote exit {rc}: {out.strip()[:400]}", out)
        return out

    try:
        ex(f"mkdir -p {shq(root)}")
        sftp = cli.open_sftp()
        for base, _dirs, files in os.walk(src_dir):
            rel = os.path.relpath(base, src_dir)
            rdir = root if rel == "." else f"{root}/" + rel.replace(os.sep, "/")
            try:
                sftp.mkdir(rdir)
            except Exception:
                pass
            for fn in files:
                sftp.put(os.path.join(base, fn), f"{rdir}/{fn}")
        sftp.close()
        out = ex(f"cd {shq(root)} && chmod +x *.sh && {launch_cmd}")
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
        print("  CLUSTER SUBMIT: missing SSH host/user -> can't upload yet (bundle still downloadable)")
        reporter.set_detail("need SSH host/user")
        return {"submitted": False, "reason": "missing host/user",
                "diagnosis": diagnose_failure("missing host/user")}
    if not os.path.isdir(P.cluster_dir):
        print("  CLUSTER SUBMIT: no bundle to upload -> skipped")
        return {"submitted": False, "reason": "no bundle",
                "diagnosis": diagnose_failure("", "no bundle")}

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
        output = getattr(e, "output", "") or str(e)
        diag = diagnose_failure(output, str(e))
        print(f"  CLUSTER SUBMIT FAILED: {e}")
        print(f"  -> diagnosis: {diag['title']} — {diag['detail']}")
        if diag["suspect_fields"]:
            print(f"  -> likely wrong: {', '.join(diag['suspect_fields'])}")
        print("  -> the bundle is still downloadable; follow RUN_ON_CLUSTER.txt to run it manually.")
        reporter.set_detail(f"submit failed: {diag['title']}")
        return {"submitted": False, "reason": str(e), "diagnosis": diag}


# ---------- on-demand cluster status (read status.sh + watchdog.log over SSH) ----------
def _ssh_capture_systemssh(host, port, user, keyfile, command, timeout=60):
    target = f"{user}@{host}"
    common = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20"]
    ssh = ["ssh", "-p", str(port)] + common + (["-i", keyfile] if keyfile else [])
    p = subprocess.run(ssh + [target, command], capture_output=True, text=True, timeout=timeout)
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0 and not (p.stdout or "").strip():
        raise _SubmitError(f"ssh exit {p.returncode}: {out.strip()[:400]}", out)
    return p.stdout or ""


def _ssh_capture_paramiko(host, port, user, password, keyfile, command, timeout=60):
    import paramiko
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = {"hostname": host, "port": int(port), "username": user, "timeout": 20}
    if password:
        kw["password"] = password
    if keyfile:
        kw["key_filename"] = keyfile
    cli.connect(**kw)
    try:
        _in, _out, _err = cli.exec_command(command, timeout=timeout)
        out = _out.read().decode("utf-8", "replace")
        err = _err.read().decode("utf-8", "replace")
    finally:
        cli.close()
    return out or err


def _eta_from_watchdog(wd_text, overall):
    """ETA (seconds) from timestamped 'progress: done/exp' lines in watchdog.log; None if unknown.
    Uses the conversion RATE between the first and last progress line, so the cluster's timezone is
    irrelevant — only the time DIFFERENCE matters."""
    if not wd_text or not overall or not overall.get("exp"):
        return None
    import calendar
    pts = []
    for m in re.finditer(
            r"\[(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})\][^\n]*progress:\s*(\d+)\s*/\s*(\d+)",
            wd_text):
        t = calendar.timegm(tuple(int(m.group(i)) for i in range(1, 7)) + (0, 0, 0))
        pts.append((t, int(m.group(7))))
    if len(pts) < 2:
        return None
    (t0, d0), (t1, d1) = pts[0], pts[-1]
    remaining = overall["exp"] - overall["done"]
    if remaining <= 0:
        return 0
    if t1 <= t0 or d1 <= d0:
        return None
    rate = (d1 - d0) / (t1 - t0)                      # runs per second
    return int(remaining / rate) if rate > 0 else None


def parse_status(text):
    """Parse the remote progress probe (a ROOT line + STUDY lines + ---META--- + ---WATCHDOG--- tail)
    into a dict. Per study: DOWNLOADED (a `.sra` exists in a per-accession subdir or flat, or it's
    already converted) and CONVERTED (a `.fastq.gz` exists) — so a study mid-download doesn't read as
    0. Unit-testable (no SSH)."""
    head, _, wd = text.partition("---WATCHDOG---")
    body, _, meta = head.partition("---META---")
    rm = re.search(r"(?m)^ROOT\s+(\S.*?)\s*$", body)
    root = rm.group(1).strip() if rm else ""
    per_study = []
    for m in re.finditer(r"(?m)^STUDY\s+(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$", body):
        per_study.append({"gse": m.group(1), "exp": int(m.group(2)),
                          "downloaded": int(m.group(3)), "converted": int(m.group(4))})
    overall = None
    if per_study:
        exp = sum(s["exp"] for s in per_study)
        dl = sum(s["downloaded"] for s in per_study)
        cv = sum(s["converted"] for s in per_study)
        overall = {"exp": exp, "downloaded": dl, "converted": cv,
                   "pct": round(100 * cv / exp, 1) if exp else 0.0,
                   "dl_pct": round(100 * dl / exp, 1) if exp else 0.0}
    lm = re.search(r"(?m)^LIVE\s+(\d+)", meta)
    eta_base = {"exp": overall["exp"], "done": overall["converted"]} if overall else None
    return {"overall": overall, "per_study": per_study, "root": root,
            "live_jobs": int(lm.group(1)) if lm else None,
            "complete": "COMPLETE" in meta, "stalled": "STALLED" in meta,
            "partial": "PARTIAL" in meta, "incomplete_upstream": "UPSTREAMPARTIAL" in meta,
            "orphaned": "ORPHANED" in meta,
            "eta_seconds": _eta_from_watchdog(wd, eta_base), "raw": text[-4000:]}


# Self-discovering progress probe: find the pipeline root from this instance's <TAG>_* LSF jobs (their
# bsub CWD), so status works even after the SpliceScout server restarted and the local run dir is gone
# (the user's manual ssh diagnostic does the same). Then count per study against each SraAccList.txt.
# DOWNLOADED = accessions with a `.sra` in a per-accession subdir (`<GSE>/<acc>/<acc>.sra`, prefetch's
# layout) OR flat, OR already converted; CONVERTED = `.fastq.gz`. (raw string -> literal globs.)
_STATUS_PROBE = r'''TAG=%TAG%; FB=%FB%
jobcwd() { bjobs -l "$1" 2>/dev/null | tr -d '\n' | sed -n 's/.*CWD <\([^>]*\)>.*/\1/p' | tr -d ' '; }
# studies dir = a pf/cs job's bsub CWD (that IS <root>/by_study), or dirname of an fqd job's CWD
SD=""
for pat in "${TAG}_pf_*" "${TAG}_cs_*" "${TAG}_fqd_*"; do
  JID=$(bjobs -noheader -o jobid -J "$pat" 2>/dev/null | head -1); [ -n "$JID" ] || continue
  C=$(jobcwd "$JID"); [ -n "$C" ] || continue
  case "$pat" in "${TAG}_fqd_*") C=$(dirname "$C") ;; esac
  SD="$C"; [ -d "$SD" ] && break
done
# root = watchdog's CWD, else dirname of the studies dir, else the caller's fallback
ROOT=""
WID=$(bjobs -noheader -o jobid -J "${TAG}_watchdog" 2>/dev/null | head -1)
[ -n "$WID" ] && ROOT=$(jobcwd "$WID")
[ -z "$ROOT" ] && [ -n "$SD" ] && ROOT=$(dirname "$SD")
[ -z "$ROOT" ] && ROOT="$FB"
[ -n "$SD" ] && [ -d "$SD" ] || SD="$ROOT/by_study"
echo "ROOT $ROOT"
echo "SDIR $SD"
if [ -d "$SD" ]; then
  cd "$SD"
  for d in */; do
    g=${d%/}; L="$g/SraAccList.txt"; [ -f "$L" ] || continue
    exp=$(grep -c . "$L")
    sra=$(ls "$g"/*/*.sra "$g"/*.sra "$g"/*/*.sralite "$g"/*.sralite 2>/dev/null | sed 's#.*/##; s/\.sralite$//; s/\.sra$//' | sort -u | grep -c .)
    cv=$(ls "$g"/*.fastq.gz 2>/dev/null | sed 's#.*/##; s/_[0-9]\.fastq\.gz$//; s/\.fastq\.gz$//' | sort -u | grep -c .)
    echo "STUDY $g $exp $((sra+cv)) $cv"
  done
fi
echo "---META---"
[ -n "$ROOT" ] && [ -f "$ROOT/PIPELINE_COMPLETE.txt" ] && echo COMPLETE
[ -n "$ROOT" ] && [ -f "$ROOT/PIPELINE_STALLED.txt" ] && echo STALLED
[ -n "$ROOT" ] && [ -f "$ROOT/PIPELINE_COMPLETE_PARTIAL.txt" ] && echo PARTIAL
[ -n "$ROOT" ] && [ -f "$ROOT/PIPELINE_INCOMPLETE_UPSTREAM.txt" ] && echo UPSTREAMPARTIAL
[ -n "$ROOT" ] && [ -f "$ROOT/PIPELINE_ORPHANED.txt" ] && echo ORPHANED
echo "LIVE $(bjobs -noheader -o stat -J "${TAG}_*" 2>/dev/null | grep -cE 'RUN|PEND')"
echo "---WATCHDOG---"
[ -n "$ROOT" ] && tail -n 80 "$ROOT/watchdog.log" 2>/dev/null
true'''


def remote_status(host, user, port, keyfile, password, job_tag, fallback_root=""):
    """SSH to the LSF submit host, DISCOVER the pipeline root from this instance's `<job_tag>_*` jobs
    (works even after a server restart with no local run dir), then count per study DOWNLOADED (.sra in
    a per-accession subdir or flat, or already converted) and CONVERTED (.fastq.gz). Falls back to
    `fallback_root` if no jobs are live. Non-fatal (returns {"ok": False, ...} on SSH error)."""
    host = (host or "").strip()
    user = (user or "").strip()
    if not host or not user:
        return {"ok": False, "error": "missing SSH host/user",
                "diagnosis": diagnose_failure("missing host/user")}
    port = str(port or "22").strip() or "22"
    keyfile = (keyfile or "").strip()
    cmd = (_STATUS_PROBE.replace("%TAG%", shq(job_tag or "sra"))
                        .replace("%FB%", shq(fallback_root or "")))
    try:
        if password:
            try:
                import paramiko  # noqa: F401
            except Exception:
                raise _SubmitError("password auth needs paramiko (pip install paramiko) or use an SSH key", "")
            text = _ssh_capture_paramiko(host, port, user, password, keyfile, cmd)
        else:
            text = _ssh_capture_systemssh(host, port, user, keyfile, cmd)
    except Exception as e:
        output = getattr(e, "output", "") or str(e)
        return {"ok": False, "error": str(e), "diagnosis": diagnose_failure(output, str(e))}
    parsed = parse_status(text)
    parsed.update({"ok": True, "host": host, "job_tag": job_tag})
    return parsed


# ---------- STAR alignment progress (the analysis stage after the download) ----------
# Given the STAR JOB_TAG (download tag + "_star") + its BAM_OUT, count aligned BAMs vs the sample list,
# note whether the launcher is still PENDing (waiting on the download) and whether a genome-index build
# is running, plus COMPLETE/STALLED + the watchdog tail (for an ETA). (raw string -> literal globs.)
_STAR_STATUS_PROBE = r'''TAG=%TAG%; BO=%BO%
echo "BAMOUT $BO"
if [ -d "$BO" ]; then
  L="$BO/sample_list.tsv"
  exp=0; [ -f "$L" ] && exp=$(grep -cve '^[[:space:]]*$' "$L")
  done=$(ls "$BO"/*.bam 2>/dev/null | grep -c .)
  echo "COUNT $exp $done"
fi
echo "---META---"
[ -n "$BO" ] && [ -f "$BO/PIPELINE_COMPLETE.txt" ] && echo COMPLETE
[ -n "$BO" ] && [ -f "$BO/PIPELINE_STALLED.txt" ] && echo STALLED
[ -n "$BO" ] && [ -f "$BO/PIPELINE_COMPLETE_PARTIAL.txt" ] && echo PARTIAL
[ -n "$BO" ] && [ -f "$BO/PIPELINE_INCOMPLETE_UPSTREAM.txt" ] && echo UPSTREAMPARTIAL
[ -n "$BO" ] && [ -f "$BO/PIPELINE_ORPHANED.txt" ] && echo ORPHANED
[ -n "$BO" ] && [ -f "$BO/star/PIPELINE_LAUNCH_TIMEOUT.txt" ] && echo LAUNCHTIMEOUT
echo "LIVE $(bjobs -noheader -o stat -J "${TAG}_*" 2>/dev/null | grep -cE 'RUN|PEND')"
echo "LAUNCHPEND $(bjobs -noheader -o stat -J "${TAG}_launch" 2>/dev/null | grep -c PEND)"
echo "BUILD $(bjobs -noheader -o stat -J "${TAG}_staridx_*" 2>/dev/null | grep -cE 'RUN|PEND')"
echo "---WATCHDOG---"
[ -n "$BO" ] && tail -n 40 "$BO/watchdog.log" 2>/dev/null
true'''


def parse_star_status(text):
    """Parse the STAR progress probe into a dict (unit-testable, no SSH)."""
    head, _, wd = text.partition("---WATCHDOG---")
    body, _, meta = head.partition("---META---")
    overall = None
    m = re.search(r"(?m)^COUNT\s+(\d+)\s+(\d+)", body)
    if m:
        exp, done = int(m.group(1)), int(m.group(2))
        overall = {"exp": exp, "done": done, "pct": round(100 * done / exp, 1) if exp else 0.0}
    lm = re.search(r"(?m)^LIVE\s+(\d+)", meta)
    lp = re.search(r"(?m)^LAUNCHPEND\s+(\d+)", meta)
    bd = re.search(r"(?m)^BUILD\s+(\d+)", meta)
    eta = _eta_from_watchdog(wd, {"exp": overall["exp"], "done": overall["done"]}) if overall else None
    return {"overall": overall, "live_jobs": int(lm.group(1)) if lm else None,
            "launch_pending": bool(lp and int(lp.group(1)) > 0),
            "building": bool(bd and int(bd.group(1)) > 0),
            "complete": "COMPLETE" in meta, "stalled": "STALLED" in meta,
            "partial": "PARTIAL" in meta, "incomplete_upstream": "UPSTREAMPARTIAL" in meta,
            "orphaned": "ORPHANED" in meta, "launch_timeout": "LAUNCHTIMEOUT" in meta,
            "eta_seconds": eta, "raw": text[-2000:]}


def remote_star_status(host, user, port, keyfile, password, star_job_tag, bam_out=""):
    """SSH to the submit host and report STAR alignment progress for `star_job_tag` (= '<dlTag>_star').
    Non-fatal (returns {"ok": False, ...} on SSH error)."""
    host = (host or "").strip(); user = (user or "").strip()
    if not host or not user:
        return {"ok": False, "error": "missing SSH host/user"}
    port = str(port or "22").strip() or "22"
    cmd = (_STAR_STATUS_PROBE.replace("%TAG%", shq(star_job_tag or "sra_star"))
                             .replace("%BO%", shq(bam_out or "")))
    try:
        if password:
            try:
                import paramiko  # noqa: F401
            except Exception:
                raise _SubmitError("password auth needs paramiko", "")
            text = _ssh_capture_paramiko(host, port, user, password, keyfile, cmd)
        else:
            text = _ssh_capture_systemssh(host, port, user, keyfile, cmd)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    parsed = parse_star_status(text)
    parsed.update({"ok": True, "job_tag": star_job_tag})
    return parsed


# ---------- BAM->BED progress (the AltAnalyze junction/exon stage after STAR) ----------
# Given the BED JOB_TAG ('<dlTag>_star_bed') + STAR's BAM_OUT, count complete junction+exon BED PAIRS vs
# *.bam, note whether the launcher is still PENDing (waiting on STAR), plus COMPLETE/STALLED + the bed/
# watchdog tail (for an ETA). BED state lives in $BO/bed/. (raw string -> literal globs.)
_BED_STATUS_PROBE = r'''TAG=%TAG%; BO=%BO%; MODE=%MODE%
BEDDIR="$(dirname "$BO")/STAR_beds"
echo "BAMOUT $BO"
if [ -d "$BO" ]; then
  if [ -f "$BO/bed/bam_list.tsv" ]; then exp=$(grep -cve '^[[:space:]]*$' "$BO/bed/bam_list.tsv" 2>/dev/null); else exp=$(ls "$BO"/*.bam 2>/dev/null | grep -c .); fi
  done=$(ls "$BEDDIR"/*__junction.bed 2>/dev/null | while read -r j; do
           s="${j%__junction.bed}"; [ -s "$j" ] || continue
           case "$MODE" in
             exon) [ -s "${s}__exon.bed" ] && echo 1 ;;
             both) [ -s "${s}__exon.bed" ] && [ -e "${s}__intronJunction.bed" ] && echo 1 ;;
             *)    [ -e "${s}__intronJunction.bed" ] && echo 1 ;;
           esac
         done | grep -c .)
  echo "COUNT $exp $done"
fi
echo "---META---"
[ -n "$BO" ] && [ -f "$BO/bed/PIPELINE_COMPLETE.txt" ] && echo COMPLETE
[ -n "$BO" ] && [ -f "$BO/bed/PIPELINE_STALLED.txt" ] && echo STALLED
[ -n "$BO" ] && [ -f "$BO/bed/PIPELINE_COMPLETE_PARTIAL.txt" ] && echo PARTIAL
[ -n "$BO" ] && [ -f "$BO/bed/PIPELINE_INCOMPLETE_UPSTREAM.txt" ] && echo UPSTREAMPARTIAL
[ -n "$BO" ] && [ -f "$BO/bed/PIPELINE_ORPHANED.txt" ] && echo ORPHANED
[ -n "$BO" ] && [ -f "$BO/bed/PIPELINE_LAUNCH_TIMEOUT.txt" ] && echo LAUNCHTIMEOUT
echo "LIVE $(bjobs -noheader -o stat -J "${TAG}_*" 2>/dev/null | grep -cE 'RUN|PEND')"
echo "LAUNCHPEND $(bjobs -noheader -o stat -J "${TAG}_launch" 2>/dev/null | grep -c PEND)"
echo "---WATCHDOG---"
[ -n "$BO" ] && tail -n 40 "$BO/bed/watchdog.log" 2>/dev/null
true'''


def parse_bed_status(text):
    """Parse the BED progress probe into a dict (unit-testable, no SSH)."""
    head, _, wd = text.partition("---WATCHDOG---")
    body, _, meta = head.partition("---META---")
    overall = None
    m = re.search(r"(?m)^COUNT\s+(\d+)\s+(\d+)", body)
    if m:
        exp, done = int(m.group(1)), int(m.group(2))
        overall = {"exp": exp, "done": done, "pct": round(100 * done / exp, 1) if exp else 0.0}
    lm = re.search(r"(?m)^LIVE\s+(\d+)", meta)
    lp = re.search(r"(?m)^LAUNCHPEND\s+(\d+)", meta)
    eta = _eta_from_watchdog(wd, {"exp": overall["exp"], "done": overall["done"]}) if overall else None
    return {"overall": overall, "live_jobs": int(lm.group(1)) if lm else None,
            "launch_pending": bool(lp and int(lp.group(1)) > 0),
            "complete": "COMPLETE" in meta, "stalled": "STALLED" in meta,
            "partial": "PARTIAL" in meta, "incomplete_upstream": "UPSTREAMPARTIAL" in meta,
            "orphaned": "ORPHANED" in meta, "launch_timeout": "LAUNCHTIMEOUT" in meta,
            "eta_seconds": eta, "raw": text[-2000:]}


def remote_bed_status(host, user, port, keyfile, password, bed_job_tag, bam_out="", bed_mode="intron"):
    """SSH to the submit host and report BAM->BED progress for `bed_job_tag` (= '<dlTag>_star_bed').
    `bed_mode` (intron|exon|both) selects which BED files count as 'done' (matches lib_bed.sh:bed_done).
    Non-fatal (returns {"ok": False, ...} on SSH error)."""
    host = (host or "").strip(); user = (user or "").strip()
    if not host or not user:
        return {"ok": False, "error": "missing SSH host/user"}
    port = str(port or "22").strip() or "22"
    _mode = (bed_mode or "intron").strip().lower()
    if _mode not in ("intron", "exon", "both"):
        _mode = "intron"
    cmd = (_BED_STATUS_PROBE.replace("%TAG%", shq(bed_job_tag or "sra_star_bed"))
                            .replace("%BO%", shq(bam_out or ""))
                            .replace("%MODE%", shq(_mode)))
    try:
        if password:
            try:
                import paramiko  # noqa: F401
            except Exception:
                raise _SubmitError("password auth needs paramiko", "")
            text = _ssh_capture_paramiko(host, port, user, password, keyfile, cmd)
        else:
            text = _ssh_capture_systemssh(host, port, user, keyfile, cmd)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    parsed = parse_bed_status(text)
    parsed.update({"ok": True, "job_tag": bed_job_tag})
    return parsed


# ---------- AltAnalyze splicing (PSI) progress (the stage after BAM->BED) ----------
# Given the PSI JOB_TAG ('<dlTag>_psi') + its psi_root (= <download_root>/psi), note whether the PSI table
# was produced, whether the launcher is still PENDing (waiting on BED), plus COMPLETE/STALLED + the psi
# watchdog tail. PSI is a single AltAnalyze job, so there is no exp/done count -- just present-or-not.
_PSI_STATUS_PROBE = r'''TAG=%TAG%; PR=%PR%
echo "PSIROOT $PR"
if [ -n "$PR" ]; then
  if ls "$PR"/output/AltResults/AlternativeOutput/*EventAnnotation* >/dev/null 2>&1; then echo "PSITABLE 1"; else echo "PSITABLE 0"; fi
fi
echo "---META---"
[ -n "$PR" ] && [ -f "$PR/PIPELINE_COMPLETE.txt" ] && echo COMPLETE
[ -n "$PR" ] && [ -f "$PR/PIPELINE_STALLED.txt" ] && echo STALLED
[ -n "$PR" ] && [ -f "$PR/PIPELINE_INCOMPLETE_UPSTREAM.txt" ] && echo UPSTREAMPARTIAL
[ -n "$PR" ] && [ -f "$PR/PIPELINE_ORPHANED.txt" ] && echo ORPHANED
[ -n "$PR" ] && [ -f "$PR/PIPELINE_LAUNCH_TIMEOUT.txt" ] && echo LAUNCHTIMEOUT
echo "LIVE $(bjobs -noheader -o stat -J "${TAG}_*" 2>/dev/null | grep -cE 'RUN|PEND')"
echo "LAUNCHPEND $(bjobs -noheader -o stat -J "${TAG}_launch" 2>/dev/null | grep -c PEND)"
echo "JOBRUN $(bjobs -noheader -o stat -J "${TAG}_job" 2>/dev/null | grep -c RUN)"
echo "---WATCHDOG---"
[ -n "$PR" ] && tail -n 40 "$PR/watchdog.log" 2>/dev/null
true'''


def parse_psi_status(text):
    """Parse the PSI progress probe into a dict (unit-testable, no SSH)."""
    head, _, wd = text.partition("---WATCHDOG---")
    body, _, meta = head.partition("---META---")
    table = bool(re.search(r"(?m)^PSITABLE\s+1", body))
    lm = re.search(r"(?m)^LIVE\s+(\d+)", meta)
    lp = re.search(r"(?m)^LAUNCHPEND\s+(\d+)", meta)
    jr = re.search(r"(?m)^JOBRUN\s+(\d+)", meta)
    return {"psi_table": table,
            "live_jobs": int(lm.group(1)) if lm else None,
            "launch_pending": bool(lp and int(lp.group(1)) > 0),
            "job_running": bool(jr and int(jr.group(1)) > 0),
            "complete": "COMPLETE" in meta, "stalled": "STALLED" in meta,
            "incomplete_upstream": "UPSTREAMPARTIAL" in meta,
            "orphaned": "ORPHANED" in meta, "launch_timeout": "LAUNCHTIMEOUT" in meta,
            "raw": text[-2000:]}


def remote_psi_status(host, user, port, keyfile, password, psi_job_tag, psi_root=""):
    """SSH to the submit host and report AltAnalyze (PSI) progress for `psi_job_tag` (= '<dlTag>_psi').
    Non-fatal (returns {"ok": False, ...} on SSH error)."""
    host = (host or "").strip(); user = (user or "").strip()
    if not host or not user:
        return {"ok": False, "error": "missing SSH host/user"}
    port = str(port or "22").strip() or "22"
    cmd = (_PSI_STATUS_PROBE.replace("%TAG%", shq(psi_job_tag or "sra_psi"))
                            .replace("%PR%", shq(psi_root or "")))
    try:
        if password:
            try:
                import paramiko  # noqa: F401
            except Exception:
                raise _SubmitError("password auth needs paramiko", "")
            text = _ssh_capture_paramiko(host, port, user, password, keyfile, cmd)
        else:
            text = _ssh_capture_systemssh(host, port, user, keyfile, cmd)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    parsed = parse_psi_status(text)
    parsed.update({"ok": True, "job_tag": psi_job_tag})
    return parsed


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
