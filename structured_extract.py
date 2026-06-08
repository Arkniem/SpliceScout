"""
Stage 2 — EXTRACT (+ PROTOCOL): per-sample structured metadata from NCBI SRA/BioSample.

For each GEO study: GEO->SRA via elink (esearch fallback), batched efetch of SRA XML,
parse each EXPERIMENT_PACKAGE for {gsm, title, cell_line, source, treatments_raw[], spots}.
ALSO capture the study's library-construction protocol from the first experiment in the same
fetch (LIBRARY_CONSTRUCTION_PROTOCOL / STRATEGY / SELECTION / INSTRUMENT) -> study_protocol.json.

NO AI, NO keyword guessing. Compound dose/control normalization uses the shared normalize_v2
module (single source of truth). Resumable via the done-set.
"""
import os
import re
import json
import time
import html
import threading
import urllib.request
import urllib.parse
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

from normalize_v2 import clean_compound  # single source of truth for dose/control logic
from progress import NULL

COMPOUND_TAGS = {
    "treatment", "treatments", "agent", "agents", "compound", "compounds",
    "drug", "drugs", "drug treatment", "chemical", "chemical compound",
    "treated with", "small molecule", "inhibitor", "perturbation",
    "perturbagen", "stimulus", "stimulation", "treatment agent",
    "drug/compound", "compound treatment", "treatment compound",
}
CELL_TAGS = {"cell line", "cell-line", "cell_line", "cellline", "cell line name",
             "cell_line_name"}
SOURCE_TAGS = {"source_name", "source name", "tissue", "sample type",
               "cell type", "cell-type", "cell_type"}

# Global request pacer shared by the parallel extract workers. NCBI allows ~10 req/s with an API key,
# ~3 req/s without. _throttle() serializes only the request STARTS (it sleeps while holding the lock,
# then releases before the network call), so starts are spaced >= interval globally (rate <= 1/interval)
# while responses overlap across threads — that's what makes the API key's higher rate actually pay off
# WITHOUT ever exceeding NCBI's limit.
_state = {"key": None, "interval": 0.34, "last": 0.0}
_throttle_lock = threading.Lock()


def _configure(ncbi_key):
    _state["key"] = ncbi_key
    # a touch under NCBI's cap (10/s keyed, 3/s keyless) for headroom; _slow_pacer() raises it on a 429
    _state["interval"] = 0.13 if ncbi_key else 0.36
    _state["last"] = 0.0
    _state["throttled_notice"] = False


def _is_429(e):
    return isinstance(e, urllib.error.HTTPError) and e.code == 429


def _retry_after(e, default):
    """Honor NCBI's Retry-After header on a 429 when present, else the caller's backoff."""
    try:
        if isinstance(e, urllib.error.HTTPError):
            ra = e.headers.get("Retry-After")
            if ra:
                return max(default, min(120.0, float(ra)))
    except Exception:
        pass
    return default


def _slow_pacer():
    """On a 429, PERMANENTLY raise the GLOBAL request interval so EVERY parallel worker backs off and the
    rate-limit storm quells itself (not just the one thread that got the 429)."""
    with _throttle_lock:
        _state["interval"] = min(_state["interval"] + 0.05, 1.0)
        if not _state.get("throttled_notice"):
            _state["throttled_notice"] = True
            print(f"  NCBI 429 rate-limit hit -> slowing ALL extract workers (req interval now "
                  f"~{_state['interval']:.2f}s); automatic, the run keeps going.")


def _ksuf():
    return f"&api_key={_state['key']}" if _state["key"] else ""


def _throttle():
    """Block until this thread may START a request, keeping the GLOBAL rate <= 1/interval."""
    with _throttle_lock:
        wait = _state["interval"] - (time.monotonic() - _state["last"])
        if wait > 0:
            time.sleep(wait)
        _state["last"] = time.monotonic()


def fetch(url, retries=8):
    """GET with global pacing + resilient retries. A 429 (NCBI rate cap) is RIDDEN OUT: honor Retry-After,
    back off, and slow EVERY worker (so the parallel storm quells), retrying generously rather than failing
    the study. Non-429 errors get bounded retries. The network call is outside the throttle lock so responses
    still overlap across threads."""
    url += _ksuf()
    attempt = 0
    while True:
        _throttle()                      # global pacing (thread-safe); network call is outside the lock
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            attempt += 1
            if _is_429(e):
                _slow_pacer()                                       # slow ALL workers, not just this one
                if attempt <= 15:
                    time.sleep(_retry_after(e, min(60.0, 1.5 * (2 ** min(attempt, 5)))))
                    continue
                return None                                         # gave up after generous 429 retries
            if attempt >= retries:
                return None
            time.sleep(1.5 * attempt)


def parse_protocol(xml):
    """Study-level library-prep info from the first experiment in the XML."""
    def g(tag):
        m = re.findall(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
        return html.unescape(m[0]).strip() if m else ""
    return {
        "protocol": g("LIBRARY_CONSTRUCTION_PROTOCOL")[:600],
        "strategy": g("LIBRARY_STRATEGY"),
        "selection": g("LIBRARY_SELECTION"),
        "instrument": g("INSTRUMENT_MODEL"),
    }


def parse_samples(xml, study):
    """Yield one per-sample dict per EXPERIMENT_PACKAGE."""
    for p in re.split(r"<EXPERIMENT_PACKAGE>", xml)[1:]:
        gsm_m = re.search(r"\b(GSM\d+)\b", p)
        gsm = gsm_m.group(1) if gsm_m else ""
        title = ""
        for t in re.findall(r"<TITLE>(.*?)</TITLE>", p, re.S):
            t = html.unescape(t).strip()
            if t.startswith("GSM"):
                title = re.sub(r"^GSM\d+:\s*", "", t)
                break
        if not title:
            titles = re.findall(r"<TITLE>(.*?)</TITLE>", p, re.S)
            if titles:
                title = html.unescape(titles[-1]).strip()

        cell, source, treats = "", "", []
        for tag, val in re.findall(r"<TAG>(.*?)</TAG>\s*<VALUE>(.*?)</VALUE>", p, re.S):
            tg = html.unescape(tag).strip().lower()
            vl = html.unescape(val).strip()
            if not vl or vl.lower() in ("missing", "not applicable", "n/a", "na"):
                continue
            if tg in CELL_TAGS and not cell:
                cell = vl
            if tg in SOURCE_TAGS and not source:
                source = vl
            if tg in COMPOUND_TAGS:
                treats.append(vl)
        spots_list = [int(s) for s in re.findall(r'spots="(\d+)"', p)]
        spots = spots_list[0] if spots_list else 0

        comps = sorted({c for v in treats for c in [clean_compound(v)] if c})
        yield {
            "study": study, "gsm": gsm, "title": title,
            "cell_line": cell, "source": source, "treatments_raw": sorted(set(treats)),
            "compounds": comps, "spots": spots,
        }


def sra_ids_for_study(uid, acc):
    """SRA experiment UIDs for a GEO study: elink (reliable) then esearch fallback."""
    e = fetch(f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi"
              f"?dbfrom=gds&db=sra&id={uid}&retmode=json")
    if e is not None:
        try:
            ls = json.loads(e).get("linksets", [])
            if ls and ls[0].get("linksetdbs"):
                ids = ls[0]["linksetdbs"][0].get("links", [])
                if ids:
                    return ids
        except Exception:
            pass
    s = fetch(f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
              f"?db=sra&term={urllib.parse.quote(acc)}&retmax=2000&retmode=json")
    if s is None:
        return None
    try:
        return json.loads(s)["esearchresult"]["idlist"]
    except Exception:
        return None


def process_study(uid, acc):
    """Return (rows, protocol_dict_or_None) for one study, or (None, None) on fetch failure."""
    ids = sra_ids_for_study(uid, acc)
    if ids is None:
        return None, None
    rows, protocol = [], None
    for i in range(0, len(ids), 50):
        batch = ",".join(ids[i:i + 50])
        xml = fetch(f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=sra&id={batch}")
        if not xml:
            continue
        if protocol is None:  # capture protocol from the first successful batch
            protocol = parse_protocol(xml)
        rows.extend(parse_samples(xml, acc))
    return rows, (protocol or {"protocol": "", "strategy": "", "selection": "", "instrument": ""})


def _checkpoint(P, done, protocols):
    """Persist the resume checkpoint (done-set + protocols). Called every N studies + at the end —
    batching keeps these growing O(n) JSON dumps off the per-study hot path on big runs. Safe because
    structured_samples.jsonl is keyed by GSM downstream (build_final), so a crash that re-fetches a few
    not-yet-checkpointed studies just rewrites the same rows (deduped), never corrupts the output."""
    json.dump(sorted(done), open(P.done_set, "w", encoding="utf-8"))
    json.dump(protocols, open(P.study_protocol, "w", encoding="utf-8"), ensure_ascii=False)


def run(P, ncbi_key=None, cap=None, reporter=NULL, workers=8):
    """Stage 2 entry. Writes P.samples_jsonl, P.done_set, P.study_protocol. Resumable.

    Studies are fetched in PARALLEL (ThreadPoolExecutor) behind the global _throttle() pacer, so the
    NCBI API key's higher rate (10/s vs 3/s) actually turns into speed — the pacer caps request STARTS
    to NCBI's limit while responses overlap, so we never exceed the limit. `workers` is capped to a safe
    ceiling; the rate limiter (not the thread count) is what bounds the request rate. Resume-safe: the
    done-set + protocols are checkpointed after each completed study under a write lock."""
    _configure(ncbi_key)
    with open(P.raw_json, "r", encoding="utf-8") as f:
        result = json.load(f)["result"]
    uids = result["uids"]
    pairs = [(u, result[u].get("accession")) for u in uids]
    if cap:
        pairs = pairs[:cap]

    done = set()
    if os.path.exists(P.done_set):
        try:
            done = set(json.load(open(P.done_set, "r", encoding="utf-8")))
        except Exception:
            done = set()
    protocols = {}
    if os.path.exists(P.study_protocol):
        try:
            protocols = json.load(open(P.study_protocol, "r", encoding="utf-8"))
        except Exception:
            protocols = {}

    # progress: total = all studies in scope; pre-credit ones already done / unaddressable
    reporter.set_total(len(pairs))
    skipped = sum(1 for (_, acc) in pairs if (not acc) or (acc in done))
    if skipped:
        reporter.advance(skipped)
    todo = [(uid, acc) for (uid, acc) in pairs if acc and acc not in done]
    n_workers = max(1, min(int(workers or 8), 24))   # rate limiter caps req/s regardless of thread count
    reporter.set_detail(f"{len(todo)} studies to fetch · {n_workers} parallel · "
                        f"{'keyed ~10/s' if ncbi_key else 'keyless ~3/s'}")
    print(f"=== EXTRACT+PROTOCOL: {len(pairs)} studies (done already: {len(done)}; "
          f"{len(todo)} to fetch, {n_workers} parallel) ===")

    jf = open(P.samples_jsonl, "a", encoding="utf-8")
    write_lock = threading.Lock()
    counters = {"processed": 0, "failed": 0, "seen": 0}

    def handle(uid, acc):
        rows, protocol = process_study(uid, acc)
        return acc, rows, protocol

    try:
        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            futs = [ex.submit(handle, uid, acc) for (uid, acc) in todo]
            for fut in as_completed(futs):
                try:
                    acc, rows, protocol = fut.result()
                except Exception as e:
                    counters["failed"] += 1
                    reporter.advance(1)
                    reporter.set_detail(f"fetch error: {str(e)[:80]}")
                    continue
                if rows is None:
                    counters["failed"] += 1
                    print(f"  {acc} FETCH FAILED (will retry next run)")
                    reporter.advance(1)
                    reporter.set_detail(f"{acc}: fetch failed (will retry)")
                    continue
                with write_lock:
                    for r in rows:
                        jf.write(json.dumps(r, ensure_ascii=False) + "\n")
                    jf.flush()
                    protocols[acc] = protocol
                    done.add(acc)
                    counters["processed"] += 1
                    counters["seen"] += 1
                    seen = counters["seen"]
                    if seen % 25 == 0:                 # periodic checkpoint (final one in finally)
                        _checkpoint(P, done, protocols)
                reporter.advance(1)
                reporter.set_detail(f"{acc} (+{len(rows)} samples)")
                if seen % 20 == 0:
                    print(f"  [{seen}/{len(todo)}] {acc}  (+{len(rows)} samples)")
    finally:
        _checkpoint(P, done, protocols)               # always persist final progress
        jf.close()
    print(f"  Done. Processed {counters['processed']} new studies "
          f"({counters['failed']} fetch failures). Total: {len(done)}.")
    return {"studies_done": len(done)}


def main():
    import argparse
    from pipeline_paths import Paths
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--ncbi-key", default=None)
    ap.add_argument("--cap", type=int, default=None)
    a = ap.parse_args()
    run(Paths(a.run_dir).ensure_dirs(), a.ncbi_key, a.cap)


if __name__ == "__main__":
    main()
