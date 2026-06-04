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
import xml.etree.ElementTree as ET

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


def fetch_study(gse, cache_dir):
    full_path = os.path.join(cache_dir, gse + ".full.xml")
    if os.path.exists(full_path) and os.path.getsize(full_path) > 200:
        return "cached"
    we, qk = C.elink_gds_to_sra(gse)
    if we and qk:                                       # normal GEO->SRA path (QueryKey present == real links)
        xml = C.efetch_sra_full(we, qk)
        open(full_path, "w", encoding="utf-8").write(xml)
        n = len(ET.fromstring(xml).findall("EXPERIMENT_PACKAGE"))
        return "ok (%d experiments)" % n
    # ENA/EBI-brokered fallback
    ena_study, soft = find_ena_study(gse)
    open(os.path.join(cache_dir, gse + ".soft.txt"), "w", encoding="utf-8").write(soft)
    if not ena_study:
        return "NO SRA / NO ENA STUDY FOUND"
    xml = C.efetch_sra_xml(ena_study)
    open(full_path, "w", encoding="utf-8").write(xml)
    n = len(ET.fromstring(xml).findall("EXPERIMENT_PACKAGE"))
    return "ENA %s (%d experiments)" % (ena_study, n)


def run(P, sel, ncbi_key=None, reporter=NULL):
    """Cache full SRA XML for every study of the selected cell line. Resumable (skips cached)."""
    C.configure(ncbi_key)
    os.makedirs(P.xml_cache_dir, exist_ok=True)
    studies = sel.get("studies", [])
    reporter.set_total(len(studies))
    reporter.set_detail(f"{len(studies)} studies for {sel.get('canonical','?')}")
    print(f"=== DEEP-DIVE FETCH: {len(studies)} studies for "
          f"{sel.get('canonical','?')!r} -> {P.xml_cache_dir} ===")
    for i, gse in enumerate(studies, 1):
        try:
            status = fetch_study(gse, P.xml_cache_dir)
        except Exception as e:
            status = "ERROR: %s" % e
        print(f"  [{i}/{len(studies)}] {gse:<12} {status}")
        reporter.advance(1)
        reporter.set_detail(f"{gse}: {status}")
    return {"studies": len(studies)}


def main():
    import argparse
    import json
    from pipeline_paths import Paths
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--ncbi-key", default=None)
    a = ap.parse_args()
    P = Paths(a.run_dir).ensure_dirs()
    sel = json.load(open(P.cellline_selection, encoding="utf-8"))
    run(P, sel, a.ncbi_key)


if __name__ == "__main__":
    main()
