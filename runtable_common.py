# -*- coding: utf-8 -*-
"""
NCBI E-utilities client for the deep-dive Run Selector reconstruction.

Ported verbatim from the validated GEO_SRA_Metadata_Pipeline `common.py`, with one change:
the NCBI API key is set via `configure(ncbi_key)` (using the key the main pipeline already
has) instead of an environment variable. Kept SEPARATE from the main pipeline's fetch client
because the Run Selector reconstruction is validated byte-for-byte against this exact code path.
"""
import re
import time
import json
import threading
import urllib.request
import urllib.parse
import urllib.error

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"

_state = {"key": "", "interval": 0.34, "last": 0.0, "throttled_notice": False}
_throttle_lock = threading.Lock()   # global pacer so parallel callers stay within NCBI's rate


def configure(ncbi_key):
    """Set the NCBI API key (raises the rate limit 3/s -> 10/s) for this process."""
    _state["key"] = (ncbi_key or "").strip()
    # a touch under the cap (10/s keyed, 3/s keyless) for headroom; _slow_pacer() raises it on a 429
    _state["interval"] = 0.13 if _state["key"] else 0.36
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
    """On a 429, PERMANENTLY raise the GLOBAL request interval so EVERY parallel caller backs off."""
    with _throttle_lock:
        _state["interval"] = min(_state["interval"] + 0.05, 1.0)
        if not _state.get("throttled_notice"):
            _state["throttled_notice"] = True
            print(f"  NCBI 429 rate-limit hit -> slowing all deep-dive fetches (interval now "
                  f"~{_state['interval']:.2f}s); automatic, the run keeps going.")


def _throttle():
    """Block until the caller may START a request, keeping the GLOBAL rate <= 1/interval (thread-safe),
    so parallel deep-dive fetches never exceed NCBI's limit."""
    with _throttle_lock:
        dt = time.time() - _state["last"]
        if dt < _state["interval"]:
            time.sleep(_state["interval"] - dt)
        _state["last"] = time.time()


def http_get(url, tries=8, timeout=180):
    """GET with throttling + retries. Appends api_key for eutils URLs. A 429 (NCBI rate cap) is RIDDEN OUT
    (honor Retry-After, back off, slow ALL callers) and retried generously before it counts against `tries`;
    other errors get `tries` bounded attempts. Raises the last error only if it ultimately can't fetch."""
    key = _state["key"]
    if key and "eutils.ncbi.nlm.nih.gov" in url and "api_key=" not in url:
        url += ("&" if "?" in url else "?") + "api_key=" + key
    last = None
    i = 0
    rl = 0
    while True:
        _throttle()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "geo-sra-pipeline/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            if _is_429(e):
                _slow_pacer()
                rl += 1
                if rl <= 15:
                    time.sleep(_retry_after(e, min(60.0, 1.5 * (2 ** min(rl, 5)))))
                    continue
                raise last
            i += 1
            if i >= tries:
                raise last
            time.sleep(1.5 * i)


def esearch_history(db, term):
    """esearch with usehistory -> (WebEnv, query_key, count)."""
    x = http_get(EUTILS + "esearch.fcgi?db=%s&term=%s&usehistory=y" % (db, urllib.parse.quote(term)))
    we = re.search(r"<WebEnv>(.*?)</WebEnv>", x)
    qk = re.search(r"<QueryKey>(.*?)</QueryKey>", x)
    cnt = re.search(r"<Count>(\d+)</Count>", x)
    return (we.group(1) if we else None,
            qk.group(1) if qk else None,
            int(cnt.group(1)) if cnt else 0)


def gds_uid(gse):
    """GEO Series accession -> GEO DataSets UID. UID = 200000000 + numeric part of GSE."""
    return 200000000 + int(gse[3:])


def elink_gds_to_sra(gse):
    """Link a GSE to its SRA records via GEO DataSets. Returns (WebEnv, query_key) or (None, None)."""
    resp = http_get(EUTILS + "elink.fcgi?dbfrom=gds&db=sra&id=%d&cmd=neighbor_history" % gds_uid(gse))
    we = re.search(r"<WebEnv>(.*?)</WebEnv>", resp)
    qk = re.search(r"<QueryKey>(.*?)</QueryKey>", resp)
    return (we.group(1) if we else None, qk.group(1) if qk else None)


def efetch_sra_full(webenv, query_key, retmax=10000):
    """Fetch full SRA EXPERIMENT_PACKAGE_SET XML for a history set."""
    return http_get(EUTILS + "efetch.fcgi?db=sra&query_key=%s&WebEnv=%s&rettype=full&retmode=xml&retmax=%d"
                    % (query_key, webenv, retmax))


def esearch_idlist(db, term, retmax=10000):
    """Return the list of UIDs for a term (robust fallback to history)."""
    js = http_get(EUTILS + "esearch.fcgi?db=%s&term=%s&retmode=json&retmax=%d"
                  % (db, urllib.parse.quote(term), retmax))
    try:
        return json.loads(js).get("esearchresult", {}).get("idlist", [])
    except Exception:
        return []


def efetch_sra_xml(term):
    """Full SRA XML for a study/accession term. History efetch first, then UID-list fallback
    (NCBI's history efetch returns intermittent HTTP 500s)."""
    we, qk, _ = esearch_history("sra", term)
    if we:
        try:
            return efetch_sra_full(we, qk)
        except Exception:
            pass
    ids = esearch_idlist("sra", term)
    if not ids:
        raise RuntimeError("no SRA records for %r" % term)
    return http_get(EUTILS + "efetch.fcgi?db=sra&id=%s&rettype=full&retmode=xml" % ",".join(ids))


# ---- tiny XML helpers ----
def txt(el):
    return (el.text or "").strip() if el is not None else ""


def ext_id(idents, ns):
    if idents is None:
        return ""
    for e in idents.findall("EXTERNAL_ID"):
        if e.attrib.get("namespace", "").lower() == ns.lower():
            return txt(e)
    return ""
