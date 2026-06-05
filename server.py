"""
Local web front end for the NCBI GEO RNA-seq pipeline.

    python server.py                 # serve on 127.0.0.1:8765 and open a browser
    python server.py --port 9000     # custom port
    python server.py --no-open       # don't auto-open the browser
    python server.py --host 0.0.0.0  # expose on the LAN (use with care: keys are posted)

A single-page UI collects everything a run needs — the GEO search query, whether to scan
ALL matching studies or cap at N, the Anthropic API key (or skip AI cleaning), an optional
NCBI key, model, and concurrency — then starts the pipeline in a background thread and shows
a live 7-stage progress stepper with a progress bar, an ETA, and a streaming log. When the
run finishes it lists the output tables for download.

Stdlib only (http.server) plus the existing pipeline modules — no web framework needed.
Runs ONE pipeline at a time; the UI blocks starting a second run while one is active.
"""
import argparse
import atexit
import io
import json
import os
import re
import sys
import threading
import traceback
import webbrowser
from dataclasses import asdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from pipeline_paths import Paths
from progress import RunReporter
import pipeline
import llm_providers
import cluster_deploy
import plot_data
import stage_docs

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- shared state: exactly one active run at a time ----
_LOCK = threading.Lock()
_REPORTER = None     # current/last RunReporter
_WORKER = None       # current worker thread
_RUN_DIR = None      # current/last run dir (download root)

# ---- per-instance identity: each launched server claims sra1 / sra2 / sra3 ... ----
# So you can run several instances at once (concurrent projects) and their cluster JOB_TAGs
# never collide. The slot is the lowest free number among LIVE instances (a closed/dead
# instance frees its number), claimed atomically in a cross-process lock directory.
_INSTANCE_SLOT = 1
_INSTANCE_TAG = "sra1"
_INSTANCE_LOCK_PATH = None


def _instances_dir():
    return os.path.join(os.path.expanduser("~"), ".geo_pipeline_instances")


def _pid_alive(pid):
    """True if a process with this PID is currently running (cross-platform, no extra deps)."""
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            k.GetExitCodeProcess(h, ctypes.byref(code))
            k.CloseHandle(h)
            return code.value == 259                # STILL_ACTIVE
        except Exception:
            return True                             # uncertain -> don't steal the slot
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


def _claim_instance_slot():
    """Claim the lowest free sraN slot among actively-running instances. Returns (n, lock_path)."""
    regdir = _instances_dir()
    try:
        os.makedirs(regdir, exist_ok=True)
    except Exception:
        return 1, None
    for n in range(1, 1000):
        path = os.path.join(regdir, f"sra{n}.lock")
        if os.path.exists(path):
            alive = True
            try:
                alive = _pid_alive(json.load(open(path, encoding="utf-8")).get("pid"))
            except Exception:
                alive = False
            if alive:
                continue                 # slot held by a live instance
            try:
                os.remove(path)          # stale (instance died) -> reclaim it
            except Exception:
                continue
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)   # atomic claim (race-safe)
        except FileExistsError:
            continue                     # another instance grabbed it a moment ago
        except Exception:
            return n, None
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "slot": n}, f)
        except Exception:
            pass
        return n, path
    return 1, None


def _release_instance_slot():
    if _INSTANCE_LOCK_PATH:
        try:
            os.remove(_INSTANCE_LOCK_PATH)
        except Exception:
            pass


def _port_in_use(host, port):
    """True if something is already listening on host:port. Probe by CONNECT, not by bind —
    HTTPServer sets allow_reuse_address, and on Windows that lets two sockets share a port, so a
    failed bind can't detect an in-use port; a successful connect always can."""
    import socket
    h = "127.0.0.1" if host in ("0.0.0.0", "") else host
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.connect((h, port))
        return True            # someone is listening here
    except OSError:
        return False
    finally:
        s.close()


def _bind_server(host, start_port, tries=64):
    """Bind the first free port at/after start_port, so every launch gets its own instance."""
    last = None
    for port in range(start_port, start_port + tries):
        if _port_in_use(host, port):
            continue
        try:
            return ThreadingHTTPServer((host, port), Handler), port
        except OSError as e:
            last = e          # lost a race for this port -> try the next
    raise last or OSError("no free port found")


class _Tee(io.TextIOBase):
    """Write-through to the original stream AND to the reporter log (line-buffered)."""

    def __init__(self, orig, reporter):
        self.orig = orig
        self.reporter = reporter
        self._buf = ""

    def write(self, s):
        try:
            self.orig.write(s)
        except Exception:
            pass
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.reporter.log(line)
        return len(s)

    def flush(self):
        try:
            self.orig.flush()
        except Exception:
            pass


def _worker(cfg, P, reporter, secrets=None):
    """Run the pipeline, teeing all stdout/stderr into the live log."""
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = _Tee(old_out, reporter)
    sys.stderr = _Tee(old_err, reporter)
    try:
        pipeline.run_pipeline(cfg, P, reporter=reporter, secrets=secrets)
    except Exception as e:
        reporter.log("ERROR: " + repr(e))
        reporter.log(traceback.format_exc())
        reporter.fail(e)
    finally:
        sys.stdout, sys.stderr = old_out, old_err


def _start_run(body):
    """Validate the posted config, create a run dir, and launch the worker."""
    global _REPORTER, _WORKER, _RUN_DIR
    with _LOCK:
        if _WORKER and _WORKER.is_alive():
            return 409, {"error": "A run is already in progress."}

        query = (body.get("query") or "").strip()
        if not query:
            return 400, {"error": "A search query is required."}

        scope = (body.get("scope") or "capped").strip()
        if scope == "all":
            cap = "unlimited"
        else:
            try:
                cap = int(body.get("cap", 25))
                if cap < 1:
                    raise ValueError
            except Exception:
                return 400, {"error": "Cap must be a positive whole number."}

        skip_ai = bool(body.get("skip_ai"))
        provider = llm_providers.normalize_provider(body.get("provider"))
        api_key = (body.get("api_key") or "").strip()
        ncbi_key = (body.get("ncbi_key") or "").strip() or None
        model = (body.get("model") or "").strip() or llm_providers.DEFAULT_MODEL[provider]
        base_url = (body.get("base_url") or "").strip()   # custom OpenAI-compatible endpoint (optional)
        deep_dive = bool(body.get("deep_dive", True))
        pick_mode = "manual" if (body.get("pick_mode") == "manual") else "auto"
        module = (body.get("module") or "bulk_rna_seq").strip() or "bulk_rna_seq"
        star_in = body.get("star") or {}
        _sk = ("GENOME_DIR", "SJDB_GTF", "STAR_INDEX_ROOT", "ORGANISM", "THREADS", "MEM_MB", "WALL")
        star_cfg = {k: str(star_in.get(k)).strip() for k in _sk if str(star_in.get(k) or "").strip()} or None
        try:
            concurrency = max(1, min(99, int(body.get("concurrency", 8))))
        except Exception:
            concurrency = 8

        if not skip_ai:
            if api_key:
                os.environ[llm_providers.KEY_ENV[provider]] = api_key
            elif not llm_providers.have_key(provider):
                return 400, {"error": f"An API key for {llm_providers.PROVIDER_LABEL[provider]} "
                                      "is required (or tick 'Skip AI cleaning')."}

        # ----- cluster handoff -----
        cluster_mode = (body.get("cluster_mode") or "off").strip()
        if not deep_dive:
            cluster_mode = "off"   # no per-study lists without the deep dive
        cluster_in = body.get("cluster") or {}
        cluster_cfg = None
        secrets = {}
        if cluster_mode != "off":
            if not (cluster_in.get("PIPELINE_ROOT") or "").strip():
                return 400, {"error": "Cluster PIPELINE_ROOT (the path on the cluster) is required."}
            if cluster_mode == "autonomous" and not (
                    (cluster_in.get("ssh_host") or "").strip()
                    and (cluster_in.get("ssh_user") or "").strip()):
                return 400, {"error": "Autonomous mode needs an SSH host and username."}
            keys = ["PIPELINE_ROOT", "SCRATCH_DIR", "LSF_QUEUE", "SRATOOLKIT_MODULE", "ASPERA_MODULE",
                    "THREADS", "MEM_MB", "WALL", "PREFETCH_MEM_MB", "WATCHDOG_INTERVAL_MIN", "JOB_TAG",
                    "ssh_host", "ssh_user", "ssh_port", "ssh_key"]
            cluster_cfg = {k: str(cluster_in.get(k)).strip()
                           for k in keys if str(cluster_in.get(k) or "").strip()}
            cluster_cfg.setdefault("JOB_TAG", _INSTANCE_TAG)   # auto per-instance tag if left blank
            pw = body.get("ssh_password") or ""      # secret -> memory for the run; saved only if remembered
            if pw:
                secrets["ssh_password"] = pw

        _save_settings(body)   # remember these inputs locally for the next launch

        slug = re.sub(r"[^a-z0-9]+", "-", query.lower())[:40].strip("-") or "run"
        # tag the run dir with this instance so concurrent instances never share a run folder
        run_dir = os.path.join("runs", f"{slug}_{datetime.now():%Y%m%d-%H%M%S}_{_INSTANCE_TAG}")
        P = Paths(run_dir).ensure_dirs()
        cfg = pipeline.RunConfig(query=query, cap=cap, ncbi_key=ncbi_key, model=model,
                                 provider=provider, base_url=base_url, module=module,
                                 concurrency=concurrency, run_dir=P.run_dir, skip_ai=skip_ai,
                                 deep_dive=deep_dive, pick_mode=pick_mode,
                                 cluster_mode=cluster_mode, cluster_cfg=cluster_cfg, star_cfg=star_cfg)
        # config.json mirrors the CLI (ncbi_key + cluster_cfg stored; Anthropic key + SSH password NOT)
        json.dump(asdict(cfg), open(P.config, "w", encoding="utf-8"), indent=2)

        reporter = RunReporter(run_dir=P.run_dir)
        _REPORTER = reporter
        _RUN_DIR = P.run_dir
        t = threading.Thread(target=_worker, args=(cfg, P, reporter, secrets), daemon=True)
        _WORKER = t
        t.start()
        return 200, {"ok": True, "run_dir": P.run_dir}


def _status():
    with _LOCK:
        if _REPORTER is None:
            return {"state": "idle"}
        return _REPORTER.snapshot()


# ---- local settings: remember the user's inputs (incl. keys + cluster info) between launches ----
def _settings_path():
    return os.path.join(os.path.expanduser("~"), ".geo_pipeline_settings.json")


def _load_settings():
    try:
        return json.load(open(_settings_path(), encoding="utf-8"))
    except Exception:
        return {}


def _save_settings(body):
    """Persist form inputs locally so they prefill next launch. NOTE: plaintext on this machine —
    includes the Anthropic key, NCBI key, and any SSH password, at ~/.geo_pipeline_settings.json."""
    keep = {k: body[k] for k in
            ("query", "scope", "cap", "skip_ai", "provider", "api_keys", "ncbi_key", "model",
             "base_url", "module", "concurrency", "pick_mode", "cluster_mode", "ssh_password") if k in body}
    if isinstance(body.get("cluster"), dict):
        keep["cluster"] = body["cluster"]
    if isinstance(body.get("star"), dict):
        keep["star"] = body["star"]
    try:
        with open(_settings_path(), "w", encoding="utf-8") as f:
            json.dump(keep, f, indent=2)
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "GeoPipeline/1.0"

    def log_message(self, *args):
        pass  # quiet: 1s status polling would flood the console

    # -- helpers --
    def _send_json(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, html):
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, name):
        with _LOCK:
            run_dir = _RUN_DIR
        if not run_dir:
            return self._send_json(404, {"error": "no run yet"})
        base = os.path.basename(name)
        path = None
        for sub in ("tables", "runtable"):       # search both output dirs by basename
            cand = os.path.join(run_dir, sub, base)
            if base and os.path.isfile(cand):
                path = cand
                break
        if not path:
            return self._send_json(404, {"error": "file not found"})
        ctype = ("text/csv" if base.endswith(".csv")
                 else "text/markdown" if base.endswith(".md")
                 else "text/plain" if base.endswith(".txt")
                 else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                 if base.endswith(".xlsx")
                 else "application/zip" if base.endswith(".zip")
                 else "application/json" if base.endswith(".json")
                 else "application/octet-stream")
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{base}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_static(self, relpath, ctype):
        """Serve a file from the project dir (vendored assets) — separate from run-dir downloads."""
        path = os.path.join(HERE, relpath)
        if not os.path.isfile(path):
            return self._send_json(404, {"error": "not found"})
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _send_readme(self):
        """Render USER_GUIDE.md as a minimal dark page (preformatted; no markdown deps)."""
        try:
            md = open(os.path.join(HERE, "USER_GUIDE.md"), encoding="utf-8").read()
        except Exception:
            md = "USER_GUIDE.md not found."
        html = ("<!DOCTYPE html><html><head><meta charset='utf-8'><title>SpliceScout — User Guide</title>"
                "<style>body{background:#0f1420;color:#e7ecf5;font:14px/1.6 ui-monospace,Consolas,"
                "monospace;max-width:920px;margin:0 auto;padding:32px 22px}a{color:#5b8cff}"
                "pre{white-space:pre-wrap;word-wrap:break-word}</style></head><body><pre>"
                + _html_attr(md) + "</pre></body></html>")
        self._send_html(html)

    # -- routes --
    def do_GET(self):
        route = urlparse(self.path)
        if route.path in ("/", "/index.html"):
            return self._send_html(_page())
        if route.path == "/api/status":
            return self._send_json(200, _status())
        if route.path == "/api/settings":
            return self._send_json(200, _load_settings())
        if route.path == "/api/file":
            q = parse_qs(route.query)
            return self._send_file(q.get("name", [""])[0])
        if route.path == "/plotly.js":
            return self._send_static("vendor/plotly.min.js", "application/javascript; charset=utf-8")
        if route.path == "/readme":
            return self._send_readme()
        if route.path == "/api/plotdata":
            with _LOCK:
                run_dir = _RUN_DIR
            if not run_dir:
                return self._send_json(200, {"available": False, "samples": [], "studies": []})
            return self._send_json(200, plot_data.build_plot_data(Paths(run_dir)))
        if route.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        self._send_json(404, {"error": "not found"})

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def do_POST(self):
        route = urlparse(self.path)
        if route.path == "/api/start":
            try:
                body = self._read_body()
            except Exception:
                return self._send_json(400, {"error": "invalid JSON"})
            code, obj = _start_run(body)
            return self._send_json(code, obj)
        if route.path == "/api/select":
            try:
                body = self._read_body()
            except Exception:
                return self._send_json(400, {"error": "invalid JSON"})
            with _LOCK:
                rep = _REPORTER
            cell_line = (body.get("cell_line") or "").strip() or None   # None => auto-pick best
            ok = rep.provide_selection(cell_line) if rep else False
            return self._send_json(200 if ok else 409,
                                   {"ok": True, "cell_line": cell_line} if ok
                                   else {"error": "not awaiting a selection"})
        if route.path == "/api/cluster_retry":
            try:
                body = self._read_body()
            except Exception:
                return self._send_json(400, {"error": "invalid JSON"})
            with _LOCK:
                rep = _REPORTER
            if not rep:
                return self._send_json(409, {"error": "no active run"})
            if (body.get("action") or "").strip() == "cancel":
                ok = rep.provide_cluster_fix({"action": "cancel"})
            else:
                cin = body.get("cluster") or {}
                keys = ["PIPELINE_ROOT", "ssh_host", "ssh_user", "ssh_port", "ssh_key"]
                cluster = {k: str(cin.get(k)).strip() for k in keys if str(cin.get(k) or "").strip()}
                fix = {"cluster": cluster}
                if "ssh_password" in body:           # present (even empty) => set or clear the password
                    fix["ssh_password"] = body.get("ssh_password") or ""
                ok = rep.provide_cluster_fix(fix)
            return self._send_json(200 if ok else 409,
                                   {"ok": True} if ok else {"error": "not awaiting a cluster fix"})
        if route.path == "/api/ai_retry":
            try:
                body = self._read_body()
            except Exception:
                return self._send_json(400, {"error": "invalid JSON"})
            with _LOCK:
                rep = _REPORTER
            if not rep:
                return self._send_json(409, {"error": "no active run"})
            if (body.get("action") or "").strip() == "skip_ai":
                ok = rep.provide_ai_fix({"action": "skip_ai"})
            else:
                fix = {}
                for k in ("provider", "model"):
                    v = str(body.get(k) or "").strip()
                    if v:
                        fix[k] = v
                if body.get("api_key"):       # secret -> transient only, never written to config.json
                    fix["api_key"] = body.get("api_key")
                ok = rep.provide_ai_fix(fix)
            return self._send_json(200 if ok else 409,
                                   {"ok": True} if ok else {"error": "not awaiting an AI fix"})
        if route.path == "/api/cluster_status":
            try:
                body = self._read_body()
            except Exception:
                return self._send_json(400, {"error": "invalid JSON"})
            # Works even with NO active run (after a server restart): take SSH creds + JOB_TAG from the
            # active run's config.json if present, else the saved settings + this instance's tag. The
            # probe then DISCOVERS the cluster root from this instance's <JOB_TAG>_* jobs.
            with _LOCK:
                run_dir = _RUN_DIR
            cfg, fallback_root = {}, ""
            if run_dir:
                P = Paths(run_dir)
                try:
                    cfg = (json.load(open(P.config, encoding="utf-8")).get("cluster_cfg")) or {}
                except Exception:
                    cfg = {}
                fallback_root = cluster_deploy._read_config_root(P) or ""
            settings = _load_settings()
            sc = settings.get("cluster") or {}
            host = (cfg.get("ssh_host") or sc.get("ssh_host") or "").strip()
            user = (cfg.get("ssh_user") or sc.get("ssh_user") or "").strip()
            port = str(cfg.get("ssh_port") or sc.get("ssh_port") or "22").strip() or "22"
            keyfile = (cfg.get("ssh_key") or sc.get("ssh_key") or "").strip()
            password = (body.get("ssh_password") or settings.get("ssh_password") or "").strip()
            # use THIS instance's own tag — NOT the shared saved-settings JOB_TAG (one settings file
            # across instances), which made e.g. an sra3 instance read sra2's jobs.
            # Scope strictly to THIS instance's tag: the UI sends its INSTANCE_TAG, and we never fall
            # back to a bare "sra" (which would prefix-match other instances' jobs on the cluster).
            job_tag = (body.get("job_tag") or cfg.get("JOB_TAG") or _INSTANCE_TAG or "").strip()
            if not job_tag:
                return self._send_json(400, {"error": "instance job tag not set — cannot scope the status check"})
            if not fallback_root:
                fallback_root = (sc.get("PIPELINE_ROOT") or "").strip()
            status = cluster_deploy.remote_status(host, user, port, keyfile, password, job_tag, fallback_root)
            # if this run is the Bulk RNA-seq (STAR) module, also report STAR alignment progress
            run_module = ""
            if run_dir:
                try:
                    run_module = json.load(open(Paths(run_dir).config, encoding="utf-8")).get("module") or ""
                except Exception:
                    run_module = ""
            if run_module == "bulk_rna_seq" and status.get("ok"):
                bam_out = (fallback_root.rstrip("/") + "/STAR_bams") if fallback_root else ""
                try:
                    star = cluster_deploy.remote_star_status(host, user, port, keyfile, password,
                                                             f"{job_tag}_star", bam_out)
                    if star and star.get("ok"):
                        status["star"] = star
                except Exception:
                    pass
            return self._send_json(200, status)
        self._send_json(404, {"error": "not found"})


def _page():
    llm_cfg = {
        "models": llm_providers.MODELS,
        "labels": llm_providers.PROVIDER_LABEL,
        "keyHint": {
            "anthropic": "Anthropic key (sk-ant-…) — console.anthropic.com",
            "openai": "OpenAI key (sk-…) — platform.openai.com/api-keys",
            "gemini": "Gemini key (AI…) — aistudio.google.com/apikey",
        },
    }
    return (PAGE.replace("__DEFAULT_QUERY__", _html_attr(pipeline.DEFAULT_QUERY))
                .replace("__LLM_CONFIG__", json.dumps(llm_cfg))
                .replace("__INSTANCE_TAG__", _html_attr(_INSTANCE_TAG))
                .replace("__INSTANCE_SLOT__", str(_INSTANCE_SLOT or 1))
                .replace("__STAGE_DOCS__", json.dumps(stage_docs.STAGE_DOCS)))


def _html_attr(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def main():
    global _INSTANCE_SLOT, _INSTANCE_TAG, _INSTANCE_LOCK_PATH
    ap = argparse.ArgumentParser(description="Web front end for the GEO RNA-seq pipeline")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="don't auto-open a browser")
    a = ap.parse_args()

    # serve relative to this script so runs/ lands next to the pipeline code
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    # claim this instance's slot -> sra1 / sra2 / sra3 ... (released on exit; dead slots reclaimed)
    _INSTANCE_SLOT, _INSTANCE_LOCK_PATH = _claim_instance_slot()
    _INSTANCE_TAG = f"sra{_INSTANCE_SLOT}"
    atexit.register(_release_instance_slot)

    # auto-pick a free port so EVERY launch starts its own instance (run concurrent projects)
    httpd, port = _bind_server(a.host, a.port)
    url = f"http://{a.host if a.host != '0.0.0.0' else '127.0.0.1'}:{port}/"
    print(f"GEO RNA-seq pipeline UI  (instance #{_INSTANCE_SLOT}, cluster JOB_TAG \"{_INSTANCE_TAG}\")  ->  {url}")
    if port != a.port:
        print(f"   (port {a.port} was busy -> using {port})")
    print("   Launch this again any time to run a concurrent project — each instance gets its own")
    print("   port and its own cluster JOB_TAG (sra1, sra2, sra3, ...).")
    print("Press Ctrl+C to stop.")
    if not a.no_open:
        try:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down.")
        httpd.shutdown()
    finally:
        _release_instance_slot()


# ----------------------------------------------------------------------------
# Single-page UI (no f-string: literal { } below belong to CSS/JS)
# ----------------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GEO RNA-seq Pipeline</title>
<style>
  :root{
    --bg:#0f1420; --panel:#171d2b; --panel2:#1e2435; --line:#2b3450;
    --txt:#e7ecf5; --mut:#9aa6c0; --accent:#5b8cff; --accent2:#7c5bff;
    --ok:#37c98b; --warn:#f0b429; --err:#ff5d6c; --chip:#222b42;
  }
  *{box-sizing:border-box}
  body{margin:0;background:linear-gradient(180deg,#0d111b,#0f1420 240px);color:var(--txt);
    font:15px/1.5 ui-sans-serif,system-ui,"Segoe UI",Roboto,Arial,sans-serif}
  .wrap{max-width:860px;margin:0 auto;padding:32px 20px 80px}
  header h1{margin:0 0 4px;font-size:24px;letter-spacing:.2px}
  .ibadge{display:inline-block;font-size:12px;font-weight:600;color:var(--accent);
    background:rgba(91,140,255,.12);border:1px solid var(--line);border-radius:20px;
    padding:3px 10px;margin-left:8px;vertical-align:middle;letter-spacing:0}
  header p{margin:0 0 24px;color:var(--mut)}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;
    padding:22px 22px;margin-bottom:18px;box-shadow:0 8px 30px rgba(0,0,0,.25)}
  label.fld{display:block;margin:0 0 18px}
  .fld>span.lbl{display:block;font-weight:600;margin-bottom:6px}
  .hint{color:var(--mut);font-size:13px;margin-top:5px}
  input[type=text],input[type=number],input[type=password],textarea,select{
    width:100%;background:var(--panel2);color:var(--txt);border:1px solid var(--line);
    border-radius:9px;padding:10px 12px;font:inherit;outline:none}
  input:focus,textarea:focus,select:focus{border-color:var(--accent);
    box-shadow:0 0 0 3px rgba(91,140,255,.18)}
  textarea{resize:vertical;min-height:62px}
  .row{display:flex;gap:12px;flex-wrap:wrap}
  .row>*{flex:1;min-width:180px}
  .scope{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .scope label{display:flex;gap:7px;align-items:center;cursor:pointer;
    background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:9px 12px}
  .scope label.sel{border-color:var(--accent);box-shadow:0 0 0 2px rgba(91,140,255,.18)}
  .scope input[type=number]{width:110px;flex:none}
  .chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
  .chip{background:var(--chip);border:1px solid var(--line);border-radius:20px;
    padding:5px 11px;font-size:12.5px;color:var(--mut);cursor:pointer}
  .chip:hover{color:var(--txt);border-color:var(--accent)}
  .check{display:flex;gap:9px;align-items:flex-start;cursor:pointer;margin-top:2px}
  .check input{margin-top:3px}
  details.adv{margin-top:4px;border-top:1px dashed var(--line);padding-top:14px}
  details.adv summary{cursor:pointer;color:var(--mut);font-weight:600;margin-bottom:14px}
  button.primary{background:linear-gradient(90deg,var(--accent),var(--accent2));color:#fff;
    border:none;border-radius:10px;padding:12px 22px;font:600 15px/1 inherit;cursor:pointer}
  button.primary:disabled{opacity:.55;cursor:not-allowed}
  button.ghost{background:var(--panel2);color:var(--txt);border:1px solid var(--line);
    border-radius:9px;padding:9px 16px;font:600 14px/1 inherit;cursor:pointer}
  .err{background:rgba(255,93,108,.12);border:1px solid var(--err);color:#ffd2d6;
    border-radius:9px;padding:11px 13px;margin-top:12px;font-size:14px}
  /* progress view */
  .meta{display:flex;gap:18px;flex-wrap:wrap;color:var(--mut);font-size:13px;margin-bottom:6px}
  .meta b{color:var(--txt);font-weight:600}
  .obar{height:12px;background:var(--panel2);border-radius:8px;overflow:hidden;border:1px solid var(--line)}
  .obar>span{display:block;height:100%;width:0;border-radius:8px;
    background:linear-gradient(90deg,var(--accent),var(--accent2));transition:width .5s ease}
  .ohead{display:flex;justify-content:space-between;align-items:baseline;margin:16px 0 7px}
  .ohead .pct{font-size:22px;font-weight:700}
  .ohead .eta{color:var(--mut);font-size:13px}
  .stages{margin-top:18px;display:flex;flex-direction:column;gap:2px}
  .stage{display:flex;gap:13px;padding:11px 8px;border-radius:9px}
  .stage.active{background:var(--panel2)}
  .sicon{width:24px;text-align:center;font-size:16px;flex:none;color:var(--mut);padding-top:1px}
  .stage.done .sicon{color:var(--ok)} .stage.active .sicon{color:var(--accent)}
  .stage.skipped .sicon{color:#5a6b8c} .stage.skipped{opacity:.5}
  .sbody{flex:1;min-width:0}
  .slabel{font-weight:600}
  .stage.pending .slabel,.stage.skipped .slabel{color:var(--mut);font-weight:500}
  .scount{font-size:12.5px;color:var(--mut);margin-top:5px}
  .sdetail{font-size:12.5px;color:var(--mut);margin-top:3px;white-space:nowrap;
    overflow:hidden;text-overflow:ellipsis}
  .sbar{height:7px;background:#0d1220;border-radius:5px;overflow:hidden;margin-top:7px;border:1px solid var(--line)}
  .sbar>span{display:block;height:100%;background:var(--accent);width:0;transition:width .5s ease}
  .sbar.indet>span{width:35%;background:linear-gradient(90deg,transparent,var(--accent),transparent);
    animation:slide 1.1s linear infinite}
  @keyframes slide{0%{margin-left:-40%}100%{margin-left:100%}}
  .spin{display:inline-block;animation:spin 1s linear infinite}
  @keyframes spin{100%{transform:rotate(360deg)}}
  .logwrap{margin-top:18px}
  .logwrap h3{margin:0 0 7px;font-size:13px;color:var(--mut);font-weight:600;text-transform:uppercase;letter-spacing:.5px}
  .log{background:#0a0e17;border:1px solid var(--line);border-radius:9px;padding:11px 13px;
    height:230px;overflow:auto;font:12.5px/1.55 ui-monospace,"Cascadia Code",Consolas,monospace;
    color:#bcd0f0;white-space:pre-wrap;word-break:break-word}
  .log .t{color:#56627d}
  .banner{border-radius:10px;padding:14px 16px;margin-bottom:16px;font-weight:600}
  .banner.ok{background:rgba(55,201,139,.12);border:1px solid var(--ok);color:#bff3da}
  .banner.err{background:rgba(255,93,108,.12);border:1px solid var(--err);color:#ffd2d6}
  .banner.pick{background:rgba(91,140,255,.12);border:1px solid var(--accent);color:#cfe0ff}
  .stats{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
  .stat{background:var(--panel2);border:1px solid var(--line);border-radius:11px;padding:13px 16px;flex:1;min-width:130px}
  .stat .n{font-size:24px;font-weight:700}
  .stat .k{color:var(--mut);font-size:12.5px;margin-top:2px}
  .files{display:flex;flex-direction:column;gap:8px}
  .file{display:flex;justify-content:space-between;align-items:center;gap:12px;
    background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:10px 13px}
  .file.head{border-color:var(--accent);box-shadow:0 0 0 2px rgba(91,140,255,.15)}
  .file .nm{font-family:ui-monospace,Consolas,monospace;font-size:13px}
  .file .sz{color:var(--mut);font-size:12px}
  a.dl{background:linear-gradient(90deg,var(--accent),var(--accent2));color:#fff;text-decoration:none;
    border-radius:8px;padding:7px 14px;font-size:13px;font-weight:600}
  .tag{font-size:11px;background:var(--accent);color:#fff;border-radius:5px;padding:2px 7px;margin-left:8px;vertical-align:middle}
  .mutfoot{color:var(--mut);font-size:12px;margin-top:18px;word-break:break-all}
  /* tabs */
  .tabs{display:flex;gap:6px;margin:6px 0 14px}
  .tabs button{background:var(--panel2);color:var(--mut);border:1px solid var(--line);
    border-radius:10px;padding:8px 18px;font:600 14px/1 inherit;cursor:pointer}
  .tabs button.on{color:#fff;border-color:transparent;background:linear-gradient(90deg,var(--accent),var(--accent2))}
  .tabs button[hidden]{display:none}
  /* step-doc modal */
  .modal{position:fixed;inset:0;background:rgba(6,9,16,.66);display:flex;align-items:center;
    justify-content:center;z-index:50;padding:20px}
  .modal[hidden]{display:none}
  .modal .box{background:var(--panel);border:1px solid var(--line);border-radius:14px;
    max-width:600px;width:100%;padding:22px 24px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
  .modal h2{margin:0 0 12px;font-size:18px}
  .modal #docBody p{margin:0 0 10px}
  .modal .docmeta{margin-top:10px;font-size:12.5px;color:var(--mut);font-family:ui-monospace,Consolas,monospace;
    word-break:break-word}
  .modal .x{float:right;cursor:pointer;color:var(--mut);font-size:22px;line-height:1;border:none;background:none}
  .slabel.doc{cursor:pointer} .slabel .qm{color:var(--accent);font-size:12px;opacity:.6}
  .slabel.doc:hover .qm{opacity:1}
  /* plots */
  .psec{margin:18px 0 7px;font-size:12.5px;color:var(--mut);font-weight:600;text-transform:uppercase;letter-spacing:.5px}
  .studylist{display:flex;flex-direction:column;gap:5px;max-height:280px;overflow:auto;margin-bottom:6px}
  .studyitem{display:flex;gap:10px;align-items:baseline;background:var(--panel2);border:1px solid var(--line);
    border-radius:8px;padding:8px 12px;cursor:pointer}
  .studyitem:hover{border-color:var(--accent)}
  .studyitem.sel{border-color:var(--accent);box-shadow:0 0 0 2px rgba(91,140,255,.18)}
  .studyitem .snm{font-family:ui-monospace,Consolas,monospace;font-size:13px;flex:none}
  .studyitem .sttl{flex:1;min-width:0;color:var(--mut);font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .studyitem .sn{color:var(--mut);font-size:12px;flex:none}
  .chart{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:6px;margin-bottom:10px;min-height:120px}
  /* page footer */
  .pagefoot{color:var(--mut);font-size:12.5px;margin-top:28px;padding-top:14px;border-top:1px solid var(--line)}
  .pagefoot a{color:var(--accent);text-decoration:none} .pagefoot a:hover{text-decoration:underline}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>GEO RNA-seq Pipeline <span class="ibadge" title="This instance's number and its cluster JOB_TAG. Launch the program again for a concurrent project — it gets the next free tag (sra1, sra2, ...).">instance __INSTANCE_SLOT__ &middot; JOB_TAG __INSTANCE_TAG__</span></h1>
    <p>Submit an NCBI GEO search and get cleaned, splicing-amenable, cell-line-grouped compound tables.</p>
  </header>

  <div class="tabs" id="tabs">
    <button id="tabRun" class="on" type="button">Run</button>
    <button id="tabPlots" type="button" hidden>Plots</button>
  </div>

  <div id="runtab">
  <!-- SETUP -->
  <section id="setup" class="card">
    <div id="clusterCheck" hidden style="margin-bottom:16px;padding:12px 14px;border:1px solid var(--line);border-radius:10px;background:var(--panel2)">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
        <span class="hint" style="margin:0">Cluster jobs from a previous launch of this instance still running? Check without starting a new run.</span>
        <button type="button" class="ghost" id="clCheckBtn">Check cluster status</button>
      </div>
      <div id="clusterstatus2" style="margin-top:10px"></div>
    </div>
    <form id="form">
      <label class="fld">
        <span class="lbl">NCBI GEO search query</span>
        <textarea id="query" spellcheck="false">__DEFAULT_QUERY__</textarea>
        <div class="hint">Entrez syntax, e.g. fields like <code>[Description]</code>, <code>[Organism]</code>. This is sent to GEO's <code>gds</code> database.</div>
        <div class="chips" id="examples">
          <span class="chip" data-q="rna-seq[Description] AND human[Organism] AND drug">human + drug</span>
          <span class="chip" data-q="rna-seq[Description] AND human[Organism] AND cancer AND treatment">cancer treatment</span>
          <span class="chip" data-q="rna-seq[Description] AND human[Organism] AND inhibitor">inhibitor</span>
        </div>
      </label>

      <label class="fld">
        <span class="lbl">How many studies?</span>
        <div class="scope" id="scope">
          <label id="lblCap" class="sel"><input type="radio" name="scope" value="capped" checked> Cap at
            <input type="number" id="cap" min="1" value="25"> studies</label>
          <label id="lblAll"><input type="radio" name="scope" value="all"> All matching studies</label>
        </div>
        <div class="hint">Extraction fetches SRA metadata per study (rate-limited), so more studies = longer runs and more API cost. Start small (25) to validate, then scale up.</div>
      </label>

      <label class="fld">
        <span class="lbl">AI provider &amp; key</span>
        <div class="row">
          <select id="provider" style="max-width:240px">
            <option value="anthropic">Anthropic (Claude)</option>
            <option value="openai">OpenAI (ChatGPT)</option>
            <option value="gemini">Google Gemini</option>
          </select>
          <input type="text" id="model" list="modellist" autocomplete="off" spellcheck="false"
                 placeholder="model name (editable — type any model)">
          <datalist id="modellist"></datalist>
        </div>
        <input type="password" id="akey" autocomplete="off" placeholder="API key" style="margin-top:10px">
        <div class="hint" id="keyhint">Used for the AI cleaning passes (drug-name canonicalization + sample classification).</div>
        <div id="baseurlrow" style="margin-top:10px;display:none">
          <input type="text" id="baseurl" autocomplete="off" spellcheck="false" style="width:100%"
                 placeholder="Custom OpenAI-compatible base URL (optional)">
          <div class="hint">Point the OpenAI provider at a custom OpenAI-compatible endpoint — a MiMo/Qwen host, a local vLLM/LM-Studio server, or OpenRouter <code>https://openrouter.ai/api/v1</code>. Blank = <code>api.openai.com</code>. The API key above is sent to this endpoint.</div>
        </div>
        <label class="check"><input type="checkbox" id="skip"> <span>Skip AI cleaning — run the deterministic stages only (no key needed). Tables will lack canonical drug names &amp; recovered cell lines.</span></label>
      </label>

      <label class="fld">
        <span class="lbl">Analysis module</span>
        <div class="hint" style="margin-bottom:9px">Sets the library-prep filter (which protocols pass the headline table) and the downstream analysis pipeline. More modules (single-cell, etc.) coming.</div>
        <div class="scope" id="modulesel">
          <label id="lblBulkRna" class="sel"><input type="radio" name="module" value="bulk_rna_seq" checked> Bulk RNA-seq (STAR)</label>
        </div>
        <div class="hint">Keeps full-length / splicing-amenable protocols (drops 10x, single-cell, 3'-tag) and, on the cluster, aligns the downloaded reads with STAR.</div>
        <div id="starfields">
          <input type="text" id="star_genome" autocomplete="off" spellcheck="false" style="width:100%;margin-top:8px"
                 placeholder="STAR genome index dir on the cluster (GENOME_DIR) — blank = resolve/build by organism">
          <div class="hint">If blank or not a real index, an index is resolved by organism: the registry (<code>star_index_registry.json</code>) &rarr; a previously built index &rarr; a one-time build job. Fill the registry with your GRCh38 index path to skip the build. Used only when the cluster is on.</div>
          <details class="adv" style="margin-top:8px"><summary>STAR options</summary>
            <div class="row" style="margin-top:8px">
              <input type="text" id="star_org" placeholder="Organism (blank = auto-detect; default Homo sapiens)" autocomplete="off">
              <input type="text" id="star_gtf" placeholder="GTF path (optional)" autocomplete="off">
            </div>
            <input type="text" id="star_indexroot" placeholder="STAR_INDEX_ROOT (where a build-once index is written)" autocomplete="off" style="width:100%;margin-top:8px">
            <div class="row" style="margin-top:8px">
              <input type="number" id="star_threads" placeholder="threads (6)" min="1" max="32">
              <input type="number" id="star_mem" placeholder="mem MB (64000)" min="1000">
              <input type="text" id="star_wall" placeholder="wall (24:00)">
            </div>
          </details>
        </div>
      </label>

      <label class="fld">
        <span class="lbl">Cell-line metadata deep-dive</span>
        <div class="hint" style="margin-bottom:9px">The best cell line is taken through the full SRA Run Selector metadata + a download-ready <code>SraAccList.txt</code> (plus a filtered per-run table &amp; Excel workbook).</div>
        <div class="scope" id="pickmode">
          <label id="lblAuto" class="sel"><input type="radio" name="pick" value="auto" checked> Auto-pick best cell line</label>
          <label id="lblManual"><input type="radio" name="pick" value="manual"> Let me choose after the scan</label>
        </div>
        <div class="hint">Auto ranks real cell lines by # unique compounds, then total reads. Manual pauses the run after the scan so you can pick from the ranked list.</div>
      </label>

      <label class="fld" id="clusterFld">
        <span class="lbl">Run on a cluster (download &amp; convert the reads)</span>
        <div class="scope" id="clmode">
          <label id="lblAuton" class="sel"><input type="radio" name="clmode" value="autonomous" checked> Autonomous (upload &amp; launch)</label>
          <label id="lblManualDl"><input type="radio" name="clmode" value="manual"> Download a bundle</label>
          <label id="lblClOff"><input type="radio" name="clmode" value="off"> Off</label>
        </div>
        <div class="hint">Hands the per-study accession lists to your LSF download pipeline (each study downloaded/converted separately). <b>Autonomous</b> uploads the bundle to your cluster and runs <code>./run_pipeline.sh</code>; <b>Download</b> gives you a ready-to-run zip. Each run lands in its own <b>per-cell-line subfolder</b> under PIPELINE_ROOT (e.g. <code>…/UMUC9</code>), so runs never mix.</div>
        <div id="clfields">
          <input type="text" id="clroot" placeholder="PIPELINE_ROOT — absolute path on the cluster, e.g. /data/mylab/sra" style="margin-top:10px" autocomplete="off">
          <div id="sshwrap">
            <div class="row" style="margin-top:10px">
              <input type="text" id="sshhost" placeholder="SSH host (login.hpc.edu)" autocomplete="off">
              <input type="text" id="sshuser" placeholder="SSH username" autocomplete="off">
              <input type="number" id="sshport" value="22" title="SSH port — 22 is the standard SSH port" style="max-width:110px">
            </div>
            <div class="hint">The number on the right is the <b>SSH port</b> (22 is the standard default — only change it if your cluster uses a custom one).</div>
            <div class="row" style="margin-top:10px">
              <input type="text" id="sshkey" placeholder="private key file (optional — else SSH agent/default)" autocomplete="off">
              <input type="password" id="sshpass" placeholder="password (optional; key/agent preferred)" autocomplete="off">
            </div>
          </div>
          <details class="adv" style="margin-top:12px">
            <summary>Advanced cluster settings (config.sh)</summary>
            <div class="row">
              <input type="text" id="clscratch" placeholder="SCRATCH_DIR (/scratch/$USER)" autocomplete="off">
              <input type="text" id="clqueue" placeholder="LSF_QUEUE (blank = default)" autocomplete="off">
            </div>
            <div class="row" style="margin-top:10px">
              <input type="text" id="cltool" placeholder="sratoolkit module (sratoolkit/3.0.0)" autocomplete="off">
              <input type="text" id="claspera" placeholder="aspera module (aspera/3.9.1)" autocomplete="off">
            </div>
            <div class="row" style="margin-top:10px">
              <input type="number" id="clthreads" placeholder="THREADS (6)">
              <input type="number" id="clmem" placeholder="MEM_MB (32000)">
              <input type="text" id="clwall" placeholder="WALL (50:00)">
              <input type="number" id="clpfmem" placeholder="PREFETCH_MEM_MB (132000)">
              <input type="text" id="cljob" value="__INSTANCE_TAG__" placeholder="JOB_TAG (auto per instance)" title="LSF job-name prefix. Auto-set to this instance's tag (sra1, sra2, ...) so concurrent projects' cluster jobs never collide. Edit if you want a custom tag." autocomplete="off">
            </div>
          </details>
        </div>
      </label>

      <details class="adv">
        <summary>Advanced options</summary>
        <label class="fld">
          <span class="lbl">AI concurrency</span>
          <input type="number" id="conc" min="1" max="99" value="8">
          <div class="hint">Parallel AI requests (1–99). Higher = faster; a rate-limited key may 429 (which now pauses cleanly via the AI-fix prompt).</div>
        </label>
        <label class="fld" style="margin-bottom:0">
          <span class="lbl">NCBI E-utilities API key (optional)</span>
          <input type="password" id="ncbi" autocomplete="off" placeholder="optional — raises rate limit 3→10 req/s">
          <div class="hint">Speeds up the fetch + extract stages. Leave blank to run keyless.</div>
        </label>
      </details>

      <div class="hint" style="margin-bottom:12px">Your entries (including API keys &amp; cluster info) are saved locally on this machine (<code>~/.geo_pipeline_settings.json</code>) so they auto-fill next time.</div>
      <button type="submit" class="primary" id="start">Start pipeline</button>
      <div class="err" id="formErr" hidden></div>
    </form>
  </section>

  <!-- RUN -->
  <section id="run" class="card" hidden>
    <div id="banner"></div>
    <div class="meta" id="meta"></div>
    <div class="ohead">
      <span class="pct" id="opct">0%</span>
      <span class="eta" id="oeta"></span>
    </div>
    <div class="obar"><span id="obar"></span></div>
    <div class="stages" id="stages"></div>

    <div id="selectpanel" hidden></div>
    <div id="aifix" hidden></div>
    <div id="clusterfix" hidden></div>
    <div id="results" hidden></div>

    <div class="logwrap">
      <h3>Live log</h3>
      <div class="log" id="log"></div>
    </div>

    <div style="margin-top:18px;display:flex;gap:10px">
      <button class="ghost" id="newrun" hidden>Start another run</button>
    </div>
    <div class="mutfoot" id="rundir"></div>
  </section>
  </div><!-- /runtab -->

  <!-- PLOTS -->
  <section id="plots" class="card" hidden>
    <div id="plotsbody"><div class="hint">Loading…</div></div>
  </section>

  <footer class="pagefoot">
    <a href="/readme" target="_blank">&#128214; User Guide</a> &nbsp;&middot;&nbsp; SpliceScout &nbsp;&middot;&nbsp; instance __INSTANCE_TAG__
  </footer>
</div>

<!-- step documentation modal -->
<div id="docmodal" class="modal" hidden>
  <div class="box">
    <button class="x" type="button" id="docClose">&times;</button>
    <h2 id="docTitle"></h2>
    <div id="docBody"></div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
const esc = s => String(s==null?'':s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function fmtDur(s){
  if(s==null) return '—'; s=Math.round(s);
  if(s<60) return s+'s';
  const m=Math.floor(s/60), sec=s%60;
  if(m<60) return m+'m '+(sec<10?'0':'')+sec+'s';
  const h=Math.floor(m/60); return h+'h '+(m%60)+'m';
}
function fmtSize(b){ if(b==null) return ''; if(b<1024) return b+' B'; if(b<1048576) return (b/1024).toFixed(1)+' KB'; return (b/1048576).toFixed(1)+' MB'; }

// ---- setup interactions ----
const lblCap=$('#lblCap'), lblAll=$('#lblAll'), capEl=$('#cap');
function syncScope(){
  const all = $('input[name=scope][value=all]').checked;
  lblAll.classList.toggle('sel', all); lblCap.classList.toggle('sel', !all);
  capEl.disabled = all;
}
$('#scope').addEventListener('change', syncScope); syncScope();
document.querySelectorAll('#examples .chip').forEach(c =>
  c.addEventListener('click', ()=>{ $('#query').value = c.dataset.q; }));
const skipEl=$('#skip'), akeyEl=$('#akey');
skipEl.addEventListener('change', ()=>{ akeyEl.disabled = skipEl.checked; akeyEl.style.opacity = skipEl.checked?.5:1; });

// AI provider + model + per-provider key memory
const LLM = __LLM_CONFIG__;
const STAGE_DOCS = __STAGE_DOCS__;
const INSTANCE_TAG = "__INSTANCE_TAG__";   // this server instance's cluster JOB_TAG (sra1/sra2/...)
const providerEl=$('#provider'), modelEl=$('#model'), keyhintEl=$('#keyhint');
let savedKeys = {};
function rebuildModels(p){
  const opts = (LLM.models[p]||[]);
  $('#modellist').innerHTML = opts.map(m=>'<option value="'+m+'"></option>').join('');
}
function syncProvider(){
  const p = providerEl.value;
  rebuildModels(p);
  modelEl.value = (LLM.models[p]||[''])[0] || '';   // default to the provider's first model (editable)
  akeyEl.value = savedKeys[p] || '';
  akeyEl.placeholder = LLM.keyHint[p] || 'API key';
  keyhintEl.textContent = 'Used for the AI cleaning passes. ' + (LLM.keyHint[p]||'');
  const br=$('#baseurlrow'); if(br) br.style.display = (p==='openai') ? '' : 'none';  // custom endpoint = openai-format
}
providerEl.addEventListener('change', syncProvider);
akeyEl.addEventListener('input', ()=>{ savedKeys[providerEl.value] = akeyEl.value; });
syncProvider();

function syncPick(){
  const manual = document.querySelector('input[name=pick][value=manual]').checked;
  $('#lblManual').classList.toggle('sel', manual); $('#lblAuto').classList.toggle('sel', !manual);
}
$('#pickmode').addEventListener('change', syncPick); syncPick();
function syncModule(){
  const m = (document.querySelector('input[name=module]:checked')||{}).value || 'bulk_rna_seq';
  const b=$('#lblBulkRna'); if(b) b.classList.toggle('sel', m==='bulk_rna_seq');
}
$('#modulesel').addEventListener('change', syncModule); syncModule();

// cluster section
function syncClusterMode(){
  const m = document.querySelector('input[name=clmode]:checked').value;
  $('#lblAuton').classList.toggle('sel', m==='autonomous');
  $('#lblManualDl').classList.toggle('sel', m==='manual');
  $('#lblClOff').classList.toggle('sel', m==='off');
  $('#clfields').style.display = (m==='off') ? 'none' : '';
  $('#sshwrap').style.display = (m==='autonomous') ? '' : 'none';
}
$('#clmode').addEventListener('change', syncClusterMode);
syncClusterMode();

// ---- start ----
$('#form').addEventListener('submit', async e=>{
  e.preventDefault();
  const btn=$('#start'), err=$('#formErr');
  btn.disabled=true; err.hidden=true;
  const body={
    query: $('#query').value,
    scope: $('input[name=scope]:checked').value,
    cap: capEl.value,
    skip_ai: skipEl.checked,
    provider: providerEl.value,
    api_key: akeyEl.value,
    api_keys: savedKeys,
    ncbi_key: $('#ncbi').value,
    model: modelEl.value,
    base_url: (($('#baseurl')||{}).value || ''),
    concurrency: $('#conc').value,
    deep_dive: true,
    pick_mode: document.querySelector('input[name=pick]:checked').value,
    module: (document.querySelector('input[name=module]:checked')||{}).value || 'bulk_rna_seq',
    star: { GENOME_DIR:(($('#star_genome')||{}).value||''), ORGANISM:(($('#star_org')||{}).value||''),
            SJDB_GTF:(($('#star_gtf')||{}).value||''), STAR_INDEX_ROOT:(($('#star_indexroot')||{}).value||''),
            THREADS:(($('#star_threads')||{}).value||''), MEM_MB:(($('#star_mem')||{}).value||''),
            WALL:(($('#star_wall')||{}).value||'') },
    cluster_mode: document.querySelector('input[name=clmode]:checked').value,
    cluster: {
      PIPELINE_ROOT: $('#clroot').value, SCRATCH_DIR: $('#clscratch').value,
      LSF_QUEUE: $('#clqueue').value, SRATOOLKIT_MODULE: $('#cltool').value,
      ASPERA_MODULE: $('#claspera').value, THREADS: $('#clthreads').value,
      MEM_MB: $('#clmem').value, WALL: $('#clwall').value,
      PREFETCH_MEM_MB: $('#clpfmem').value, JOB_TAG: $('#cljob').value,
      ssh_host: $('#sshhost').value, ssh_user: $('#sshuser').value,
      ssh_port: $('#sshport').value, ssh_key: $('#sshkey').value,
    },
    ssh_password: $('#sshpass').value,
  };
  try{
    const r = await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const j = await r.json();
    if(!r.ok) throw new Error(j.error||'Could not start the run.');
    $('#setup').hidden=true; $('#run').hidden=false;
    $('#results').hidden=true; $('#newrun').hidden=true; $('#banner').innerHTML='';
    startPolling();
  }catch(ex){ err.textContent=ex.message; err.hidden=false; btn.disabled=false; }
});

$('#newrun').addEventListener('click', ()=>{
  $('#run').hidden=true; $('#setup').hidden=false; $('#start').disabled=false;
});

// ---- polling + render ----
let polling=false;
function startPolling(){ if(polling) return; polling=true; tick(); }
async function tick(){
  try{
    const r = await fetch('/api/status');
    const s = await r.json();
    render(s);
    if(s.state==='done' || s.state==='error'){ polling=false; return; }
  }catch(ex){ /* transient; keep polling */ }
  if(polling) setTimeout(tick, 1000);
}

function stageRow(st, i){
  const icon = st.status==='done' ? '✓'
    : st.status==='skipped' ? '⊘'
    : st.status==='active' ? '<span class="spin">◐</span>' : '○';
  let mid='';
  if(st.status==='active'){
    if(st.total){
      const pct=Math.min(100, st.done/st.total*100);
      mid = '<div class="sbar"><span style="width:'+pct.toFixed(1)+'%"></span></div>'
          + '<div class="scount">'+st.done+' / '+st.total
          + (st.eta!=null ? ' • ~'+fmtDur(st.eta)+' left' : '') + '</div>';
    } else {
      mid = '<div class="sbar indet"><span></span></div>';
    }
  } else if(st.status==='done' && st.elapsed!=null){
    mid = '<div class="scount">done in '+fmtDur(st.elapsed)+'</div>';
  }
  const detail = (st.detail && st.status==='active') ? '<div class="sdetail">'+esc(st.detail)+'</div>' : '';
  return '<div class="stage '+st.status+'"><div class="sicon">'+icon+'</div>'
       + '<div class="sbody"><div class="slabel doc" data-doc="'+esc(st.key)+'" title="What happens in this step?">'
       + (i+1)+'. '+esc(st.label)+(st.status==='skipped'?' — skipped':'')+' <span class="qm">&#9432;</span></div>'
       + mid + detail + '</div></div>';
}

let lastLogLen=0;
function render(s){
  const m=s.meta||{};
  $('#meta').innerHTML =
      'Query: <b>'+esc(m.query||'')+'</b>'
    + ' &nbsp;·&nbsp; Studies: <b>'+esc(m.cap==='unlimited'||m.cap==null?'all':m.cap)+'</b>'
    + ' &nbsp;·&nbsp; '+(m.skip_ai?'<b>AI cleaning skipped</b>':'AI: <b>'+esc((m.provider||'')+(m.model?' / '+m.model:''))+'</b>');

  const pct = s.state==='done' ? 100 : Math.round((s.overall_fraction||0)*100);
  $('#obar').style.width = pct+'%';
  $('#opct').textContent = pct+'%';
  let eta='';
  if(s.state==='running'){
    eta = 'Elapsed '+fmtDur(s.elapsed) + (s.overall_eta!=null ? '  ·  ~'+fmtDur(s.overall_eta)+' remaining' : '  ·  estimating…');
  } else if(s.state==='done'){ eta = 'Completed in '+fmtDur(s.elapsed); }
  else if(s.state==='error'){ eta = 'Stopped after '+fmtDur(s.elapsed); }
  $('#oeta').textContent = eta;

  $('#stages').innerHTML = (s.stages||[]).map(stageRow).join('');
  $('#stages').querySelectorAll('.slabel.doc').forEach(el=>el.onclick=()=>openDoc(el.dataset.doc));
  maybeRevealPlots(s);
  renderSelect(s);
  renderAiFix(s);
  renderClusterFix(s);

  // log (append-aware autoscroll)
  const logEl=$('#log');
  const atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
  if((s.log||[]).length !== lastLogLen){
    logEl.innerHTML = (s.log||[]).map(l => '<span class="t">'+fmtDur(l.t).padStart(6,' ')+'</span>  '+esc(l.text)).join('\n');
    lastLogLen = (s.log||[]).length;
    if(atBottom) logEl.scrollTop = logEl.scrollHeight;
  }

  $('#rundir').textContent = m.run_dir ? ('run dir: '+m.run_dir) : '';

  // banners + results
  const banner=$('#banner');
  if(s.state==='done'){
    banner.className='banner ok'; banner.textContent='✓ Pipeline complete — tables ready below.';
    renderResults(s); $('#newrun').hidden=false; $('#start').disabled=false;
  } else if(s.state==='error'){
    banner.className='banner err'; banner.textContent='✗ Pipeline stopped: '+esc(s.error||'unknown error')+' — see the log.';
    $('#newrun').hidden=false; $('#start').disabled=false;
  } else { banner.className=''; banner.textContent=''; }
}

function renderResults(s){
  const box=$('#results'); const r=s.result||{}; const sp=r.splicing||{}, all=r.all||{}; const dd=r.deep_dive||null;
  let stats='<div class="stats">'
    + stat(sp.samples, 'splicing-amenable samples')
    + stat(sp.cell_lines, 'cell lines / buckets')
    + stat(sp.compounds, 'unique compounds')
    + (dd ? stat(dd.n_accessions, 'SRA runs · '+esc(dd.canonical||'top line')) : stat(all.samples, 'samples (all protocols)'))
    + '</div>';
  let ddline='';
  if(dd){
    ddline = '<div class="banner pick" style="margin-bottom:14px">Deep-dived <b>'+esc(dd.canonical||'')+'</b>'
      + (dd.n_studies!=null ? ' · '+dd.n_studies+' studies' : '')
      + (dd.n_accessions!=null ? ' · '+dd.n_accessions+' run accessions' : '')
      + (dd.matched_values&&dd.matched_values.length ? ' · matched: '+dd.matched_values.map(esc).join(', ') : '')
      + (dd.match_mode ? ' ('+esc(dd.match_mode)+')' : '') + '</div>';
  }
  const cl=r.cluster||null;
  let clline='';
  if(cl){
    const uploaded = (cl.mode==='autonomous' && cl.submitted);
    let tail;
    if(uploaded){
      tail = ' — uploaded &amp; launched on the cluster'
           + (cl.host ? ' (<code>'+esc(cl.host)+'</code>)' : '')
           + '; watch progress in the log (or <code>status.sh</code> on the cluster).';
    } else if(cl.mode==='autonomous'){
      tail = ' — autonomous upload did <b>not</b> complete; grab <b>cluster_bundle.zip</b> below and '
           + 'run it manually (see the log / RUN_ON_CLUSTER.txt).';
    } else {
      tail = ' — grab <b>cluster_bundle.zip</b> below to run it on your cluster '
           + '(see RUN_ON_CLUSTER.txt inside).';
    }
    clline = '<div class="banner '+(uploaded?'ok':'pick')+'" style="margin-bottom:14px">Cluster bundle ready'
      + (cl.n_studies!=null ? ' · '+cl.n_studies+' studies (each run separately)' : '')
      + (cl.pipeline_root ? ' · PIPELINE_ROOT <code>'+esc(cl.pipeline_root)+'</code>' : '')
      + tail + '</div>';
  }
  // featured (SraAccList, cluster zip) first, then headline, then the rest
  const files=(s.files||[]).slice().sort((a,b)=> (b.featured?2:b.headline?1:0)-(a.featured?2:a.headline?1:0));
  const filesHtml=files.map(f =>
    '<div class="file'+(f.headline||f.featured?' head':'')+'">'
    + '<span><span class="nm">'+esc(f.name)+'</span>'
    + (f.featured?'<span class="tag">MAIN OUTPUT</span>':f.headline?'<span class="tag">HEADLINE</span>':'')
    + ' <span class="sz">'+fmtSize(f.size)+'</span></span>'
    + '<a class="dl" href="/api/file?name='+encodeURIComponent(f.name)+'">Download</a></div>').join('');
  box.innerHTML = ddline + clline + stats + '<div class="files">'+filesHtml+'</div>';
  if(cl && cl.mode==='autonomous' && cl.submitted){
    box.innerHTML += '<div style="margin-top:14px"><button class="ghost" id="clstatBtn">Check cluster status</button>'
      + ' <span class="hint">poll the cluster for download/convert progress + ETA</span></div>'
      + '<div id="clusterstatus" style="margin-top:12px"></div>';
    const b=$('#clstatBtn'); if(b) b.onclick=()=>fetchClusterStatus('clusterstatus');
  }
  box.hidden=false;
}
function stat(n, k){ return '<div class="stat"><div class="n">'+(n==null?'—':Number(n).toLocaleString())+'</div><div class="k">'+esc(k)+'</div></div>'; }

// manual cell-line pick: render the ranked candidates when the run pauses for a choice
function renderSelect(s){
  const box=$('#selectpanel');
  if(!s.awaiting_selection){ box.hidden=true; box.innerHTML=''; return; }
  const cands=s.candidates||[];
  box.innerHTML =
    '<div class="banner pick">Pick the cell line to deep-dive — ranked by # unique compounds, then total reads:</div>'
    + '<div class="files">'
    + cands.slice(0,20).map(c =>
        '<div class="file"><span><span class="nm">'+esc(c.canonical)+'</span> <span class="sz">'
        + c.n_compounds+' compounds · '+Number(c.total_spots||0).toLocaleString()+' reads · '
        + c.n_studies+' studies</span></span>'
        + '<button class="dl pk" data-cl="'+encodeURIComponent(c.canonical)+'">Deep-dive</button></div>').join('')
    + '</div>'
    + '<button class="ghost" id="autopick" style="margin-top:10px">Auto-pick the best (top of list)</button>';
  box.hidden=false;
  box.querySelectorAll('button.pk').forEach(b => b.onclick = () => postSelect(decodeURIComponent(b.dataset.cl)));
  const ap=$('#autopick'); if(ap) ap.onclick = () => postSelect(null);
}
async function postSelect(cell_line){
  $('#selectpanel').innerHTML = '<div class="banner pick">Starting deep-dive'
    + (cell_line ? ' for '+esc(cell_line) : '') + '…</div>';
  try{ await fetch('/api/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cell_line})}); }catch(e){}
}

// cluster upload failed -> show the diagnosis + a prefilled form to fix and retry just the upload
function renderClusterFix(s){
  const box=$('#clusterfix');
  if(!s.awaiting_cluster_fix || !s.cluster_prompt){ box.hidden=true; box.dataset.built=''; box.innerHTML=''; return; }
  if(box.dataset.built==='1') return;          // don't clobber what the user is typing each poll
  const d=s.cluster_prompt.diagnosis||{}, c=s.cluster_prompt.current||{};
  const sus=new Set(d.suspect_fields||[]);
  const fld=(id,key,label,type)=>
    '<label class="fld" style="margin-bottom:10px"><span class="lbl" style="font-size:13px">'+esc(label)
    +(sus.has(key)?' <span class="tag" style="background:var(--warn);color:#1b1300">check this</span>':'')
    +'</span><input id="'+id+'" type="'+(type||'text')+'" value="'+esc(c[key]||'')+'" autocomplete="off"></label>';
  box.innerHTML =
      '<div class="banner err">Cluster upload failed — '+esc(d.title||'unknown')+'</div>'
    + '<div class="hint" style="margin:-6px 0 14px">'+esc(d.detail||'')
    + ' The rest of the pipeline is done — fix the details below and retry just the upload.</div>'
    + fld('cf_host','ssh_host','SSH host')
    + '<div class="row">'+fld('cf_user','ssh_user','SSH username')+fld('cf_port','ssh_port','SSH port')+'</div>'
    + fld('cf_key','ssh_key','Private key file (optional)')
    + fld('cf_pass','ssh_password','SSH password (optional; blank = key/agent)','password')
    + fld('cf_root','PIPELINE_ROOT','PIPELINE_ROOT (cluster path)')
    + '<div style="display:flex;gap:10px;margin-top:4px">'
    + '<button class="primary" id="cfRetry">Retry upload</button>'
    + '<button class="ghost" id="cfSkip">Skip — I\'ll run it manually</button></div>';
  box.hidden=false; box.dataset.built='1';
  $('#cfRetry').onclick=()=>postClusterRetry(false);
  $('#cfSkip').onclick=()=>postClusterRetry(true);
}
async function postClusterRetry(cancel){
  const box=$('#clusterfix');
  let body;
  if(cancel){ body={action:'cancel'}; }
  else{
    body={cluster:{
      ssh_host:$('#cf_host').value, ssh_user:$('#cf_user').value, ssh_port:$('#cf_port').value,
      ssh_key:$('#cf_key').value, PIPELINE_ROOT:$('#cf_root').value },
      ssh_password:$('#cf_pass').value};
  }
  box.innerHTML='<div class="banner pick">'+(cancel?'Skipping cluster upload…':'Retrying cluster upload…')+'</div>';
  box.dataset.built='1';
  try{ await fetch('/api/cluster_retry',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); }catch(e){}
}

// AI preflight failed (bad provider / model / key) -> fix the info or turn AI off, then continue
function renderAiFix(s){
  const box=$('#aifix');
  if(!s.awaiting_ai_fix || !s.ai_prompt){ box.hidden=true; box.dataset.built=''; box.innerHTML=''; return; }
  if(box.dataset.built==='1') return;          // don't clobber what the user is typing each poll
  const d=s.ai_prompt.diagnosis||{}, c=s.ai_prompt.current||{};
  box.innerHTML =
      '<div class="banner err">AI cleaning can\'t start — '+esc(d.title||'unknown')+'</div>'
    + '<div class="hint" style="margin:-6px 0 14px">'+esc(d.detail||'')
    + ' Fix the details below and retry, or turn AI off to run the deterministic pipeline.</div>'
    + '<label class="fld" style="margin-bottom:10px"><span class="lbl" style="font-size:13px">Provider</span>'
    + '<select id="af_prov"><option value="anthropic">Anthropic (Claude)</option>'
    + '<option value="openai">OpenAI (ChatGPT)</option><option value="gemini">Google Gemini</option></select></label>'
    + '<label class="fld" style="margin-bottom:10px"><span class="lbl" style="font-size:13px">Model</span>'
    + '<input id="af_model" type="text" value="'+esc(c.model||'')+'" autocomplete="off"></label>'
    + '<label class="fld" style="margin-bottom:10px"><span class="lbl" style="font-size:13px">API key (blank = keep current)</span>'
    + '<input id="af_key" type="password" value="" autocomplete="off"></label>'
    + '<div style="display:flex;gap:10px;margin-top:4px">'
    + '<button class="primary" id="afRetry">Retry AI</button>'
    + '<button class="ghost" id="afSkip">Turn off AI — run without it</button></div>';
  box.hidden=false; box.dataset.built='1';
  const sel=$('#af_prov'); if(sel && c.provider) sel.value=c.provider;
  $('#afRetry').onclick=()=>postAiRetry(false);
  $('#afSkip').onclick=()=>postAiRetry(true);
}
async function postAiRetry(skip){
  const box=$('#aifix');
  const body = skip ? {action:'skip_ai'}
    : {provider:$('#af_prov').value, model:$('#af_model').value, api_key:$('#af_key').value};
  box.innerHTML='<div class="banner pick">'+(skip?'Turning off AI…':'Retrying AI…')+'</div>';
  box.dataset.built='1';
  try{ await fetch('/api/ai_retry',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); }catch(e){}
}

// ---- step-doc modal (click a pipeline step to read what it does) ----
function openDoc(key){
  const d=STAGE_DOCS[key]; if(!d) return;
  $('#docTitle').textContent=d.title||key;
  $('#docBody').innerHTML = '<p>'+esc(d.what||'')+'</p>'
    + (d.inputs?'<div class="docmeta"><b>Inputs:</b> '+esc(d.inputs)+'</div>':'')
    + (d.outputs?'<div class="docmeta"><b>Outputs:</b> '+esc(d.outputs)+'</div>':'');
  $('#docmodal').hidden=false;
}
function closeDoc(){ $('#docmodal').hidden=true; }

// ---- tabs (Run | Plots) ----
function showTab(t){
  $('#runtab').hidden = (t==='plots');
  $('#plots').hidden  = (t!=='plots');
  $('#tabRun').classList.toggle('on', t!=='plots');
  $('#tabPlots').classList.toggle('on', t==='plots');
  if(t==='plots') openPlots();
}
function maybeRevealPlots(s){
  const ready = s && (s.state==='done'
    || (s.stages||[]).some(st=>(st.key==='cellline_match'||st.key==='runtable_annotate') && st.status==='done'));
  if(ready) $('#tabPlots').hidden=false;
}

// ---- Plots tab (Plotly, vendored at /plotly.js; loaded lazily on first open) ----
let PLOTDATA=null, _plotlyStarted=false;
function ensurePlotly(){
  return new Promise((resolve,reject)=>{
    if(window.Plotly) return resolve();
    if(_plotlyStarted){ const i=setInterval(()=>{ if(window.Plotly){clearInterval(i);resolve();} },80); return; }
    _plotlyStarted=true;
    const sc=document.createElement('script'); sc.src='/plotly.js';
    sc.onload=()=>resolve(); sc.onerror=()=>reject(new Error('could not load the plotting library'));
    document.head.appendChild(sc);
  });
}
async function loadPlotData(force){
  if(PLOTDATA && !force) return PLOTDATA;
  const d = await (await fetch('/api/plotdata')).json();
  PLOTDATA = (d && d.available) ? d : null;
  return PLOTDATA;
}
async function openPlots(){
  const host=$('#plotsbody');
  try{
    const d = await loadPlotData(false);
    if(!d){ host.innerHTML='<div class="hint">No plot data yet — plots use the deep-dived cell line\'s runs, available after the <b>match cell-line names</b> step. Reopen this tab then.</div>'; return; }
    await ensurePlotly();
    buildPlotsUI(d);
  }catch(ex){ host.innerHTML='<div class="err">Plots unavailable: '+esc(ex.message)+'</div>'; }
}
const LABELS={spots:'read depth (spots)',avg_spot_len:'avg spot length (bp)',bases:'bases',
  study:'study',drug:'drug',drug_treated:'drug treated',is_control:'control',dose:'dose',
  instrument:'instrument',library_selection:'library selection',platform:'platform',assay:'assay',
  layout:'library layout',source_name:'source name',treatment:'treatment'};
const lab=v=>LABELS[v]||v;
const PCONF={displayModeBar:false,responsive:true};
function buildPlotsUI(d){
  const studies=d.studies||[];
  $('#plotsbody').innerHTML =
      '<div style="display:flex;justify-content:space-between;align-items:baseline">'
    + '<h3 class="psec" style="margin-top:0">'+esc(d.cell_line||'cell line')+' &mdash; studies ('+studies.length+' · '+d.samples.length+' runs)</h3>'
    + '<button class="ghost" id="plReload" style="padding:6px 12px">&#8635; Reload</button></div>'
    + '<div class="hint" style="margin-bottom:8px">Only the picked cell line\'s runs are shown. Click a study to chart read depth and spot length per run.</div>'
    + '<div class="studylist" id="studylist">'
    + studies.map(st=>'<div class="studyitem" data-gse="'+esc(st.gse)+'">'
        + '<span class="snm">'+esc(st.gse)+'</span>'
        + '<span class="sttl">'+esc(st.title||'')+'</span>'
        + '<span class="sn">'+st.n_samples+' runs</span></div>').join('')
    + '</div><div id="studycharts"></div>' + customPlotterHTML(d);
  $('#studylist').querySelectorAll('.studyitem').forEach(el=>el.onclick=()=>{
    $('#studylist').querySelectorAll('.studyitem').forEach(x=>x.classList.remove('sel'));
    el.classList.add('sel'); renderStudyCharts(el.dataset.gse);
  });
  const rl=$('#plReload'); if(rl) rl.onclick=async()=>{ await loadPlotData(true); buildPlotsUI(PLOTDATA); };
  bindCustomPlotter(d);
  if(!window._plResize){ window._plResize=true; window.addEventListener('resize', ()=>{
    document.querySelectorAll('.chart').forEach(c=>{ if(c.data && window.Plotly) Plotly.Plots.resize(c); }); }); }
}
function plotLayout(title, extra){
  return Object.assign({ title:{text:title,font:{color:'#e7ecf5',size:14}},
    paper_bgcolor:'rgba(0,0,0,0)', plot_bgcolor:'rgba(0,0,0,0)', font:{color:'#9aa6c0'},
    autosize:true, height:360, margin:{l:90,r:20,t:42,b:46},
    legend:{font:{color:'#9aa6c0'},orientation:'h',y:-0.18},
    xaxis:{gridcolor:'#2b3450',zerolinecolor:'#2b3450',automargin:true},
    yaxis:{gridcolor:'#2b3450',zerolinecolor:'#2b3450',automargin:true} }, extra||{});
}
function iqrBox(id, vals, labels, title){
  if(!vals.filter(v=>v!=null).length){ $('#'+id).innerHTML='<div class="hint">No data.</div>'; return; }
  Plotly.newPlot(id, [{ x:vals, type:'box', boxpoints:'all', jitter:0.5, pointpos:0, orientation:'h',
    marker:{color:'#5b8cff',size:5,opacity:.55}, line:{color:'#7c5bff'}, fillcolor:'rgba(91,140,255,.12)',
    text:labels, hovertemplate:'%{text}<br>%{x}<extra></extra>', name:'' }],
    plotLayout(title,{height:210,yaxis:{showticklabels:false,gridcolor:'#2b3450',zerolinecolor:'#2b3450'}}), PCONF);
}
function renderStudyCharts(gse){
  const rows=(PLOTDATA.samples||[]).filter(s=>s.study===gse);
  $('#studycharts').innerHTML =
      '<h3 class="psec">'+esc(gse)+' &mdash; read depth per run ('+rows.length+')</h3><div id="chartDepth" class="chart"></div>'
    + '<h3 class="psec">'+esc(gse)+' &mdash; spot length (avg read length) per run</h3><div id="chartLen" class="chart"></div>';
  iqrBox('chartDepth', rows.map(r=>r.spots), rows.map(r=>r.run), 'Read depth (spots)');
  const len=rows.filter(r=>r.avg_spot_len!=null);
  if(len.length) iqrBox('chartLen', len.map(r=>r.avg_spot_len), len.map(r=>r.run), 'Avg spot length (bp)');
  else $('#chartLen').innerHTML='<div class="hint">No spot-length data for this study.</div>';
}
function customPlotterHTML(d){
  const allvars=d.categorical.concat(d.numeric);
  const opt=(arr,sel)=>arr.map(v=>'<option value="'+v+'"'+(v===sel?' selected':'')+'>'+esc(lab(v))+'</option>').join('');
  const defX=d.categorical.includes('drug_treated')?'drug_treated':(d.categorical[0]||'study');
  const defY=d.numeric.includes('spots')?'spots':(d.numeric[0]||'spots');
  return '<h3 class="psec">Custom plot</h3>'
   +'<div class="hint" style="margin-bottom:8px">'+d.samples.length+' runs of '+esc(d.cell_line||'the cell line')+'. Pick variables and a chart type.</div>'
   +'<div class="row">'
   +'<label class="fld" style="margin:0"><span class="lbl" style="font-size:12px">Chart type</span><select id="cpType">'
     +'<option value="box">Box (IQR + dots)</option><option value="violin">Violin</option>'
     +'<option value="bar">Bar (count)</option><option value="scatter">Scatter</option>'
     +'<option value="histogram">Histogram</option><option value="heatmap">Heatmap (counts)</option>'
     +'<option value="density">2D density</option></select></label>'
   +'<label class="fld" style="margin:0"><span class="lbl" style="font-size:12px">X / group</span><select id="cpX">'+opt(allvars,defX)+'</select></label>'
   +'<label class="fld" style="margin:0"><span class="lbl" style="font-size:12px">Y (numeric)</span><select id="cpY">'+opt(d.numeric,defY)+'</select></label>'
   +'<label class="fld" style="margin:0"><span class="lbl" style="font-size:12px">Color / 2nd</span><select id="cpColor"><option value="">none</option>'+opt(d.categorical,'')+'</select></label>'
   +'</div><div class="hint" id="cpHint" style="margin-bottom:8px"></div>'
   +'<button class="ghost" id="cpPlot" style="margin-bottom:10px">Plot</button><div id="customchart" class="chart"></div>';
}
function bindCustomPlotter(d){
  const b=$('#cpPlot'); if(!b) return;
  const upd=()=>{ const t=$('#cpType').value;
    const need={box:'Y by X (split by Color)',violin:'Y by X (split by Color)',bar:'run count of X (stacked by Color)',
      scatter:'X vs Y (colored by Color)',histogram:'distribution of Y (split by Color)',
      heatmap:'run counts of X × Color',density:'2D density of X (numeric) vs Y'};
    $('#cpHint').textContent=need[t]||''; };
  $('#cpType').onchange=upd; upd();
  b.onclick=()=>renderCustom(d); renderCustom(d);
}
function renderCustom(d){
  const type=$('#cpType').value, x=$('#cpX').value, y=$('#cpY').value, color=$('#cpColor').value;
  const S=d.samples;
  const groups = color ? [...new Set(S.map(r=>r[color]))].filter(g=>g!=='').sort() : [null];
  const pick=g=> color ? S.filter(r=>r[color]===g) : S;
  const H=h=>({height:h});
  let traces=[];
  if(type==='heatmap'){
    const c2 = color || (d.categorical.find(v=>v!==x) || x);
    const xs=[...new Set(S.map(r=>String(r[x])))].sort(), ys=[...new Set(S.map(r=>String(r[c2])))].sort();
    const z=ys.map(yy=>xs.map(xx=>S.filter(r=>String(r[x])===xx && String(r[c2])===yy).length));
    Plotly.newPlot('customchart',[{type:'heatmap',x:xs,y:ys,z:z,colorscale:'Blues',
      hovertemplate:lab(x)+'=%{x}<br>'+lab(c2)+'=%{y}<br>%{z} runs<extra></extra>'}],
      plotLayout('run counts: '+lab(x)+' × '+lab(c2), H(Math.max(280, 70+30*ys.length))), PCONF); return;
  }
  if(type==='density'){
    Plotly.newPlot('customchart',[{type:'histogram2d',x:S.map(r=>r[x]),y:S.map(r=>r[y]),colorscale:'Blues'}],
      plotLayout(lab(y)+' vs '+lab(x)+' (density)', H(420)), PCONF); return;
  }
  if(type==='bar'){
    const cats=[...new Set(S.map(r=>String(r[x])))].sort();
    groups.forEach(g=>{ const rows=pick(g);
      traces.push({type:'bar', x:cats, y:cats.map(c=>rows.filter(r=>String(r[x])===c).length), name:g==null?'runs':String(g)}); });
    Plotly.newPlot('customchart',traces,plotLayout('run count by '+lab(x),Object.assign({barmode:'stack'},H(380))),PCONF); return;
  }
  if(type==='histogram'){
    groups.forEach(g=>traces.push({type:'histogram', x:pick(g).map(r=>r[y]).filter(v=>v!=null), name:g==null?lab(y):String(g), opacity:.7}));
    Plotly.newPlot('customchart',traces,plotLayout(lab(y)+' distribution',Object.assign({barmode:'overlay'},H(380))),PCONF); return;
  }
  if(type==='scatter'){
    groups.forEach(g=>{ const rows=pick(g);
      traces.push({type:'scatter',mode:'markers', x:rows.map(r=>r[x]), y:rows.map(r=>r[y]), text:rows.map(r=>r.run),
        name:g==null?'':String(g), marker:{size:6,opacity:.6}}); });
    Plotly.newPlot('customchart',traces,plotLayout(lab(y)+' vs '+lab(x),H(420)),PCONF); return;
  }
  // box / violin — HORIZONTAL: Y(numeric) distribution by X(category), split by Color
  const xs=[...new Set(S.map(r=>String(r[x])))];
  const h=Math.max(300, 50+28*Math.max(xs.length, xs.length*groups.length));
  groups.forEach(g=>{ const rows=pick(g);
    traces.push(Object.assign({type:type, x:rows.map(r=>r[y]), y:rows.map(r=>String(r[x])), orientation:'h',
      text:rows.map(r=>r.run), name:g==null?'':String(g)},
      type==='box'?{boxpoints:'all',jitter:.4,pointpos:0,marker:{size:4,opacity:.5}}:{points:'all'})); });
  Plotly.newPlot('customchart',traces,plotLayout(lab(y)+' by '+lab(x),Object.assign({boxmode:'group',violinmode:'group'},H(h))),PCONF);
}

// ---- on-demand cluster status (poll the cluster after an autonomous launch) ----
let clstatTimer=null;   // single self-rescheduling auto-refresh timer (every 2 min)

// least-squares slope (runs/sec) over [{t,c}] points; null if <2 points
function clstatSlope(pts){
  const n=pts.length; if(n<2) return null;
  let st=0,sc=0,stt=0,stc=0;
  for(const p of pts){ st+=p.t; sc+=p.c; stt+=p.t*p.t; stc+=p.t*p.c; }
  const den=n*stt-st*st; if(den===0) return null;
  return (n*stc-st*sc)/den;
}
// record this check's converted count (persisted, keyed by job+root) so the ETA refines with every
// check. Resets if a new run reuses the same tag (total changes, or converted regresses).
function clstatRecord(d){
  const ov=d.overall||{}; if(ov.exp==null||ov.converted==null) return null;
  const key='clstat_hist_'+(d.job_tag||'def')+'_'+(d.root||'');
  let hist=[]; try{ hist=JSON.parse(localStorage.getItem(key)||'[]'); }catch(e){ hist=[]; }
  const last=hist.length?hist[hist.length-1]:null;
  if(last && (ov.exp!==last.e || ov.converted<last.c)) hist=[];     // new run on same tag -> reset
  const now=Math.floor(Date.now()/1000);
  if(!hist.length || now-hist[hist.length-1].t>=5) hist.push({t:now,c:ov.converted,e:ov.exp});
  if(hist.length>300) hist=hist.slice(-300);
  try{ localStorage.setItem(key, JSON.stringify(hist)); }catch(e){}
  return hist;
}
// ETA seconds to finish converting. Primary = rate observed across YOUR checks (refines each check);
// first-check fallback = the cluster watchdog's own logged rate.
function clstatEta(d, hist){
  const ov=d.overall||{};
  if(hist && hist.length>=2 && ov.exp!=null){
    const t0=hist[0].t, r=clstatSlope(hist.map(p=>({t:p.t-t0,c:p.c})));
    if(r && r>0){ const rem=ov.exp-ov.converted;
      return {sec: rem<=0?0:Math.round(rem/r), src:'your '+hist.length+' checks'}; }
  }
  if(d.eta_seconds!=null) return {sec:d.eta_seconds, src:'cluster log'};
  return {sec:null, src:null};
}
function clstatSchedule(panelId, d){
  if(clstatTimer){ clearTimeout(clstatTimer); clstatTimer=null; }
  if(d && (d.complete || d.stalled)) return;                         // finished/stalled -> stop
  clstatTimer=setTimeout(()=>fetchClusterStatus(panelId, true), 120000);   // every 2 min
}
async function fetchClusterStatus(panelId, auto){
  panelId = panelId || 'clusterstatus';
  const host=$('#'+panelId);
  if(!host){ if(clstatTimer){ clearTimeout(clstatTimer); clstatTimer=null; } return; }   // panel gone
  if(!auto) host.innerHTML='<div class="hint">Checking the cluster…</div>';
  try{
    const d = await (await fetch('/api/cluster_status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_tag:INSTANCE_TAG})})).json();
    if(!d.ok){
      if(!auto) host.innerHTML='<div class="err">'+esc((d.diagnosis&&d.diagnosis.title)||d.error||'Could not reach the cluster')
          +(d.diagnosis&&d.diagnosis.detail?' — '+esc(d.diagnosis.detail):'')+'</div>';
      clstatSchedule(panelId, null);   // transient error -> keep retrying every 2 min
      return;
    }
    renderClusterStatus(d, panelId);
    clstatSchedule(panelId, d);
  }catch(ex){
    if(!auto) host.innerHTML='<div class="err">'+esc(ex.message)+'</div>';
    clstatSchedule(panelId, null);
  }
}
function renderClusterStatus(d, panelId){
  panelId = panelId || 'clusterstatus';
  const host=$('#'+panelId), ov=d.overall||{};
  const hist=clstatRecord(d), eta=clstatEta(d, hist);   // refine the ETA from every check
  const tag = d.job_tag ? ' ('+esc(d.job_tag)+')' : '';
  let h='<div class="banner '+(d.complete?'ok':(d.stalled?'err':'pick'))+'">'
    + (d.complete?'✓ Cluster pipeline complete':(d.stalled?'⚠ Watchdog stopped (stalled)':'Cluster progress'))+tag
    + (ov.exp!=null?' &mdash; '+ov.converted+' converted &middot; '+ov.downloaded+' downloaded / '+ov.exp+' runs ('+ov.pct+'%)':'')
    + (d.live_jobs!=null?' &middot; '+d.live_jobs+' active jobs':'')
    + (eta.sec!=null&&!d.complete?' &middot; ~'+fmtDur(eta.sec)+' left to convert':'') + '</div>';
  if(d.star){
    const s=d.star, so=s.overall||{};
    const phase = s.complete ? '✓ STAR alignment complete'
      : s.stalled ? '⚠ STAR stalled'
      : s.launch_pending ? 'STAR queued — waiting for the download to finish'
      : s.building ? 'STAR — building genome index…'
      : 'STAR aligning';
    h += '<div class="banner '+(s.complete?'ok':(s.stalled?'err':'pick'))+'" style="margin-top:6px">'
       + phase + (so.exp?' &mdash; '+so.done+' / '+so.exp+' BAMs ('+so.pct+'%)':'')
       + (s.live_jobs!=null?' &middot; '+s.live_jobs+' jobs':'')
       + (s.eta_seconds!=null&&!s.complete?' &middot; ~'+fmtDur(s.eta_seconds)+' left':'') + '</div>';
  }
  if(ov.exp){ h+='<div class="obar" title="solid = converted, faint = downloaded" style="margin-bottom:4px;position:relative">'
    + '<span style="width:'+(ov.dl_pct||0)+'%;opacity:.30"></span>'
    + '<span style="width:'+(ov.pct||0)+'%;position:absolute;left:1px;top:0"></span></div>'
    + '<div class="hint" style="margin-bottom:12px;font-size:11.5px">solid = converted, faint = downloaded</div>'; }
  else if(d.live_jobs){ h+='<div class="hint" style="margin-bottom:8px">'+d.live_jobs+' active job(s) found'+tag
    +', but no per-study counts'+(d.root?' under <code>'+esc(d.root)+'</code>':'')+' — see the probe output below.</div>'; }
  else { h+='<div class="hint" style="margin-bottom:8px">No active jobs found for this instance'+tag+'. '
    + (d.root?'Checked <code>'+esc(d.root)+'</code>.':'Start a cluster run first.')+'</div>'; }
  const ps=(d.per_study||[]).slice().sort((a,b)=>(a.converted/Math.max(1,a.exp))-(b.converted/Math.max(1,b.exp)));
  if(ps.length){ h+='<div class="hint" style="margin-bottom:6px">Per study (downloaded · converted / total):</div>'
    + '<div class="files" style="max-height:240px;overflow:auto">'
    + ps.map(s=>'<div class="file"><span class="nm">'+esc(s.gse)+'</span><span class="sz">'
        + s.downloaded+' dl &middot; '+s.converted+' conv / '+s.exp+'</span></div>').join('')+'</div>'; }
  if(d.root){ h+='<div class="mutfoot" style="margin-top:8px">root: '+esc(d.root)+'</div>'; }
  if(d.raw){ h+='<details style="margin-top:8px"><summary class="hint" style="cursor:pointer">probe output</summary>'
    + '<pre style="white-space:pre-wrap;font-size:11px;color:#9aa6c0;max-height:220px;overflow:auto;background:#0a0e17;border:1px solid var(--line);border-radius:8px;padding:8px;margin-top:6px">'+esc(d.raw)+'</pre></details>'; }
  h+='<div class="hint" style="margin-top:10px;font-size:11.5px">'
    + (d.complete?'Pipeline complete — auto-refresh stopped.'
       :(d.stalled?'Watchdog stalled — auto-refresh stopped.'
         :'Auto-refreshing every 2 min.'+(eta.sec==null?' ETA appears once conversions progress.':'')))
    + ' Last checked '+esc(new Date().toLocaleTimeString())
    + (eta.src&&eta.sec!=null&&!d.complete?' &middot; ETA from '+esc(eta.src):'') + '</div>';
  h+='<button class="ghost" id="clstatRefresh_'+panelId+'" style="margin-top:8px">Refresh now</button>';
  host.innerHTML=h; const b=$('#clstatRefresh_'+panelId); if(b) b.onclick=()=>fetchClusterStatus(panelId);
}

// prefill the form from locally-saved settings (keys + cluster info remembered between launches)
function setVal(id, v){ const el=$('#'+id); if(el && v!=null && v!=='') el.value=v; }
function applySettings(s){
  if(!s || !Object.keys(s).length) return;
  setVal('query', s.query); setVal('cap', s.cap);
  if(s.scope){ const r=document.querySelector('input[name=scope][value="'+s.scope+'"]'); if(r) r.checked=true; }
  if(s.skip_ai!=null){ skipEl.checked=!!s.skip_ai; akeyEl.disabled=skipEl.checked; akeyEl.style.opacity=skipEl.checked?.5:1; }
  if(s.provider){ providerEl.value = s.provider; }
  savedKeys = Object.assign({}, s.api_keys || {});
  syncProvider();   // rebuild model suggestions + fill the key for the chosen provider
  if(s.model) modelEl.value = s.model;   // restore any saved model (editable — custom strings too)
  setVal('baseurl', s.base_url);         // restore a saved custom OpenAI-compatible endpoint
  setVal('ncbi', s.ncbi_key); setVal('conc', s.concurrency);
  if(s.pick_mode){ const r=document.querySelector('input[name=pick][value="'+s.pick_mode+'"]'); if(r) r.checked=true; }
  if(s.module){ const r=document.querySelector('input[name=module][value="'+s.module+'"]'); if(r) r.checked=true; }
  if(s.cluster_mode){ const r=document.querySelector('input[name=clmode][value="'+s.cluster_mode+'"]'); if(r) r.checked=true; }
  const c=s.cluster||{};
  setVal('clroot', c.PIPELINE_ROOT); setVal('clscratch', c.SCRATCH_DIR); setVal('clqueue', c.LSF_QUEUE);
  setVal('cltool', c.SRATOOLKIT_MODULE); setVal('claspera', c.ASPERA_MODULE);
  setVal('clthreads', c.THREADS); setVal('clmem', c.MEM_MB); setVal('clwall', c.WALL);
  setVal('clpfmem', c.PREFETCH_MEM_MB);   // JOB_TAG is auto per-instance (sra1, sra2, ...) — keep the instance default, don't restore a saved one
  setVal('sshhost', c.ssh_host); setVal('sshuser', c.ssh_user); setVal('sshport', c.ssh_port); setVal('sshkey', c.ssh_key);
  setVal('sshpass', s.ssh_password);
  const st=s.star||{};
  setVal('star_genome', st.GENOME_DIR); setVal('star_org', st.ORGANISM); setVal('star_gtf', st.SJDB_GTF);
  setVal('star_indexroot', st.STAR_INDEX_ROOT); setVal('star_threads', st.THREADS);
  setVal('star_mem', st.MEM_MB); setVal('star_wall', st.WALL);
  if(s.cluster && (s.cluster.ssh_host||'').trim()) $('#clusterCheck').hidden=false;  // enable after-restart status check
  syncScope(); syncPick(); syncModule(); syncClusterMode();
}

// resume an in-flight (or finished) run if the page is reloaded
(async function init(){
  $('#tabRun').onclick=()=>showTab('run'); $('#tabPlots').onclick=()=>showTab('plots');
  $('#clCheckBtn').onclick=()=>fetchClusterStatus('clusterstatus2');
  $('#docClose').onclick=closeDoc;
  $('#docmodal').onclick=(e)=>{ if(e.target.id==='docmodal') closeDoc(); };
  document.addEventListener('keydown',e=>{ if(e.key==='Escape') closeDoc(); });
  try{ applySettings(await (await fetch('/api/settings')).json()); }catch(ex){}
  try{
    const s = await (await fetch('/api/status')).json();
    if(s.state && s.state!=='idle'){
      $('#setup').hidden=true; $('#run').hidden=false;
      render(s);
      if(s.state==='running') startPolling();
    }
  }catch(ex){}
})();
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
