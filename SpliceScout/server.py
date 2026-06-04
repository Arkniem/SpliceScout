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

# ---- shared state: exactly one active run at a time ----
_LOCK = threading.Lock()
_REPORTER = None     # current/last RunReporter
_WORKER = None       # current worker thread
_RUN_DIR = None      # current/last run dir (download root)


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
        deep_dive = bool(body.get("deep_dive", True))
        pick_mode = "manual" if (body.get("pick_mode") == "manual") else "auto"
        try:
            concurrency = max(1, min(32, int(body.get("concurrency", 8))))
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
            pw = body.get("ssh_password") or ""      # secret -> memory for the run; saved only if remembered
            if pw:
                secrets["ssh_password"] = pw

        _save_settings(body)   # remember these inputs locally for the next launch

        slug = re.sub(r"[^a-z0-9]+", "-", query.lower())[:40].strip("-") or "run"
        run_dir = os.path.join("runs", f"{slug}_{datetime.now():%Y%m%d-%H%M%S}")
        P = Paths(run_dir).ensure_dirs()
        cfg = pipeline.RunConfig(query=query, cap=cap, ncbi_key=ncbi_key, model=model,
                                 provider=provider, concurrency=concurrency, run_dir=P.run_dir,
                                 skip_ai=skip_ai, deep_dive=deep_dive, pick_mode=pick_mode,
                                 cluster_mode=cluster_mode, cluster_cfg=cluster_cfg)
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
             "concurrency", "pick_mode", "cluster_mode", "ssh_password") if k in body}
    if isinstance(body.get("cluster"), dict):
        keep["cluster"] = body["cluster"]
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
                .replace("__LLM_CONFIG__", json.dumps(llm_cfg)))


def _html_attr(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def main():
    ap = argparse.ArgumentParser(description="Web front end for the GEO RNA-seq pipeline")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="don't auto-open a browser")
    a = ap.parse_args()

    # serve relative to this script so runs/ lands next to the pipeline code
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    httpd = ThreadingHTTPServer((a.host, a.port), Handler)
    url = f"http://{a.host if a.host != '0.0.0.0' else '127.0.0.1'}:{a.port}/"
    print(f"GEO RNA-seq pipeline UI -> {url}")
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
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>GEO RNA-seq Pipeline</h1>
    <p>Submit an NCBI GEO search and get cleaned, splicing-amenable, cell-line-grouped compound tables.</p>
  </header>

  <!-- SETUP -->
  <section id="setup" class="card">
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
        <label class="check"><input type="checkbox" id="skip"> <span>Skip AI cleaning — run the deterministic stages only (no key needed). Tables will lack canonical drug names &amp; recovered cell lines.</span></label>
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
              <input type="text" id="cljob" placeholder="JOB_TAG (sra)" autocomplete="off">
            </div>
          </details>
        </div>
      </label>

      <details class="adv">
        <summary>Advanced options</summary>
        <label class="fld">
          <span class="lbl">AI concurrency</span>
          <input type="number" id="conc" min="1" max="32" value="8">
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
}
providerEl.addEventListener('change', syncProvider);
akeyEl.addEventListener('input', ()=>{ savedKeys[providerEl.value] = akeyEl.value; });
syncProvider();

function syncPick(){
  const manual = document.querySelector('input[name=pick][value=manual]').checked;
  $('#lblManual').classList.toggle('sel', manual); $('#lblAuto').classList.toggle('sel', !manual);
}
$('#pickmode').addEventListener('change', syncPick); syncPick();

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
    concurrency: $('#conc').value,
    deep_dive: true,
    pick_mode: document.querySelector('input[name=pick]:checked').value,
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
       + '<div class="sbody"><div class="slabel">'+(i+1)+'. '+esc(st.label)+(st.status==='skipped'?' — skipped':'')+'</div>'
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
  renderSelect(s);

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
    clline = '<div class="banner pick" style="margin-bottom:14px">Cluster bundle ready'
      + (cl.n_studies!=null ? ' · '+cl.n_studies+' studies (each run separately)' : '')
      + (cl.pipeline_root ? ' · PIPELINE_ROOT <code>'+esc(cl.pipeline_root)+'</code>' : '')
      + ' — grab <b>cluster_bundle.zip</b> below (or it was uploaded &amp; launched autonomously; see the log).</div>';
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
  setVal('ncbi', s.ncbi_key); setVal('conc', s.concurrency);
  if(s.pick_mode){ const r=document.querySelector('input[name=pick][value="'+s.pick_mode+'"]'); if(r) r.checked=true; }
  if(s.cluster_mode){ const r=document.querySelector('input[name=clmode][value="'+s.cluster_mode+'"]'); if(r) r.checked=true; }
  const c=s.cluster||{};
  setVal('clroot', c.PIPELINE_ROOT); setVal('clscratch', c.SCRATCH_DIR); setVal('clqueue', c.LSF_QUEUE);
  setVal('cltool', c.SRATOOLKIT_MODULE); setVal('claspera', c.ASPERA_MODULE);
  setVal('clthreads', c.THREADS); setVal('clmem', c.MEM_MB); setVal('clwall', c.WALL);
  setVal('clpfmem', c.PREFETCH_MEM_MB); setVal('cljob', c.JOB_TAG);
  setVal('sshhost', c.ssh_host); setVal('sshuser', c.ssh_user); setVal('sshport', c.ssh_port); setVal('sshkey', c.ssh_key);
  setVal('sshpass', s.ssh_password);
  syncScope(); syncPick(); syncClusterMode();
}

// resume an in-flight (or finished) run if the page is reloaded
(async function init(){
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
