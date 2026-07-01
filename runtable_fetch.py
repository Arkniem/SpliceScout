# -*- coding: utf-8 -*-
"""
Deep-dive stage — FETCH: full SRA EXPERIMENT_PACKAGE XML for the selected cell line's studies.

For each GSE: GEO DataSets -> elink -> SRA -> efetch full XML  (cached as <GSE>.full.xml).
ENA/EBI-brokered fallback (e.g. GSE310111): read the GEO SOFT record, discover the ENA study
accession from a sample's SRA relation, fetch that study's full XML, and cache the SOFT record
(<GSE>.soft.txt) so the build step can overlay GEO characteristics.

Ported from the validated GEO_SRA_Metadata_Pipeline 01_fetch_xml.py; parameterized by Paths +
RunReporter and re-runnable (already-cached studies are skipped).
"""
import os
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

import runtable_common as C
from progress import NULL

GEO_SOFT = "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=%s&targ=%s&form=text&view=%s"


def find_ena_study(gse):
    """Read GEO SOFT for the series, return (ena_study_acc, soft_text) or (None, soft_text)."""
    soft = C.http_get(GEO_SOFT % (gse, "all", "quick"))
    m = re.search(r"term=([EDS]RX\d+)", soft)          # any experiment accession in a sample relation
    if not m:
        return None, soft
    xml = C.efetch_sra_xml(m.group(1))                  # robust (history -> id-list fallback)
    st = re.search(r'<STUDY[^>]*accession="([EDS]RP\d+)"', xml)
    return (st.group(1) if st else None), soft


def _atomic_write(path, text):
    """Write `text` to `path` atomically (temp file + os.replace). A partial/failed write -- e.g. an ENOSPC
    'No space left on device' mid-stream -- leaves NO final file, so a re-run RE-FETCHES the study instead of
    caching a truncated/0-byte XML that the '>200 bytes => cached' check would wrongly skip forever."""
    tmp = path + ".part"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)            # atomic on the same filesystem
    except BaseException:
        try:
            os.remove(tmp)               # never leave a partial .part behind
        except OSError:
            pass
        raise


def fetch_study(gse, cache_dir):
    """Cache one study's full SRA XML. Returns (ok, status); ok=False => the caller RETRIES it. Only
    COMPLETE, parseable XML is cached (parsed BEFORE the atomic write), so a failed fetch never poisons
    the cache with a half-written file that a later resume would mistake for done."""
    full_path = os.path.join(cache_dir, gse + ".full.xml")
    if os.path.exists(full_path) and os.path.getsize(full_path) > 200:
        return True, "cached"
    we, qk = C.elink_gds_to_sra(gse)
    if we and qk:                                       # normal GEO->SRA path (QueryKey present == real links)
        xml = C.efetch_sra_full(we, qk)
        if not (xml and xml.lstrip().startswith("<")):  # don't cache an empty/blank response as valid
            return False, "FETCH MISS (empty/non-XML response)"
        n = len(ET.fromstring(xml).findall("EXPERIMENT_PACKAGE"))   # parse FIRST: reject garbage before caching
        _atomic_write(full_path, xml)
        return True, "ok (%d experiments)" % n
    # ENA/EBI-brokered fallback
    ena_study, soft = find_ena_study(gse)
    _atomic_write(os.path.join(cache_dir, gse + ".soft.txt"), soft)
    if not ena_study:
        return False, "NO SRA / NO ENA STUDY FOUND"
    xml = C.efetch_sra_xml(ena_study)
    if not (xml and xml.lstrip().startswith("<")):      # don't cache an empty/blank response as valid
        return False, "ENA %s FETCH MISS (empty/non-XML response)" % ena_study
    n = len(ET.fromstring(xml).findall("EXPERIMENT_PACKAGE"))
    _atomic_write(full_path, xml)
    return True, "ENA %s (%d experiments)" % (ena_study, n)


def run(P, sel, ncbi_key=None, reporter=NULL, workers=8):
    """Cache full SRA XML for every study of the selected cell line, IN PARALLEL behind the global
    runtable_common pacer. Each study writes its OWN cache file ATOMICALLY, so no write lock is needed and a
    failed write can't poison the cache. Resumable (skips cached). FAILED studies -- a network blip, an NCBI
    rate-limit, or a TRANSIENT disk-full (e.g. you free space MID-RUN) -- are RETRIED for several rounds so
    the step SELF-HEALS instead of silently marking 'done' with studies missing (which a plain resume skips).
    Tunable via env: RUNTABLE_FETCH_ROUNDS (default 4), RUNTABLE_FETCH_RETRY_WAIT seconds (default 30)."""
    C.configure(ncbi_key)
    os.makedirs(P.xml_cache_dir, exist_ok=True)
    studies = sel.get("studies", [])
    total = len(studies)
    reporter.set_total(total)
    n_workers = max(1, min(int(workers or 8), 12))   # usually few studies; the pacer bounds req/s
    rounds = max(1, int(os.environ.get("RUNTABLE_FETCH_ROUNDS", "4")))
    wait_s = max(0, int(os.environ.get("RUNTABLE_FETCH_RETRY_WAIT", "30")))
    reporter.set_detail(f"{total} studies for {sel.get('canonical','?')} · {n_workers} parallel")
    print(f"=== DEEP-DIVE FETCH: {total} studies for {sel.get('canonical','?')!r} -> "
          f"{P.xml_cache_dir} ({n_workers} parallel, up to {rounds} round(s)) ===")

    def handle(gse):
        try:
            ok, status = fetch_study(gse, P.xml_cache_dir)
            return gse, ok, status
        except Exception as e:
            return gse, False, "ERROR: %s" % e

    accounted = set()                       # advance the progress bar at most ONCE per study
    pending = list(studies)
    failed = []
    for rnd in range(1, rounds + 1):
        failed = []
        tag = "" if rnd == 1 else f"[retry {rnd}/{rounds}] "
        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            futs = [ex.submit(handle, gse) for gse in pending]
            for fut in as_completed(futs):
                gse, ok, status = fut.result()
                print(f"  {tag}{gse:<12} {status}")
                if ok:
                    if gse not in accounted:
                        accounted.add(gse); reporter.advance(1)
                    reporter.set_detail(f"{gse}: {status}")
                else:
                    failed.append(gse)
        if not failed:
            break
        if rnd < rounds:
            print(f"  -- {len(failed)} study(ies) failed this round; retrying in {wait_s}s "
                  f"(if it was 'No space left on device', free disk NOW and they will recover) --")
            reporter.set_detail(f"{len(failed)} failed -> retrying ({rnd + 1}/{rounds}) in {wait_s}s")
            if wait_s:
                time.sleep(wait_s)
            pending = failed

    for gse in failed:                      # account the permanently-failed so the bar still completes
        if gse not in accounted:
            accounted.add(gse); reporter.advance(1)
    if failed:
        print(f"  !!! {len(failed)} study(ies) could NOT be fetched after {rounds} round(s): "
              f"{', '.join(failed[:15])}{' ...' if len(failed) > 15 else ''}")
        print("      Their .full.xml is ABSENT (not a poisoned cache), so re-running the fetch retries ONLY "
              "these. If persistent: check disk space / network / the study really exists on SRA.")
        reporter.set_detail(f"cached {total - len(failed)}/{total}; {len(failed)} unfetched -> re-run to retry")
    else:
        reporter.set_detail(f"all {total} studies cached")
    return {"studies": total, "n_failed": len(failed), "failed": failed}


def main():
    import argparse
    import json
    from pipeline_paths import Paths
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--ncbi-key", default=None)
    a = ap.parse_args()
    P = Paths(a.run_dir).ensure_dirs()
    with open(P.cellline_selection, encoding="utf-8") as f:
        sel = json.load(f)
    run(P, sel, a.ncbi_key)


if __name__ == "__main__":
    main()
