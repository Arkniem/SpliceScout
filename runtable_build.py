# -*- coding: utf-8 -*-
"""
Deep-dive stage — BUILD: reconstruct the SRA Run Selector "Metadata" (SraRunTable) format from the
cached full XML, for the selected cell line's studies.

The reconstruction logic (format_package_runs / order_columns / geo_soft_map / parse_study / the
--validate harness) is ported VERBATIM from the validated GEO_SRA_Metadata_Pipeline
02_build_sra_run_table.py (proven byte-for-byte against SRP189165). Only IO is parameterized by
Paths, and `run()` emits:
  * P.runtable_all_csv     — every run for the selected studies (audit / pre-filter)
  * P.match_candidates     — distinct cell-line-ish values for the AI matcher (with run counts)

Usage:  python runtable_build.py --run-dir <dir>   # build from cellline_selection.json
        python runtable_build.py --validate        # prove the format matches the official export
"""
import os
import re
import csv
import io
import json
import xml.etree.ElementTree as ET

import runtable_common as C
from progress import NULL


def iso_date_only(s):
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s or "")
    return m.group(1) + "T00:00:00Z" if m else ""


def iso_minute(s):
    m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})", s or "")
    return "%sT%s:%s:00Z" % (m.group(1), m.group(2), m.group(3)) if m else ""


def clean_tag(t):                       # Run Selector normalizes attribute tags: whitespace -> underscore
    return re.sub(r"\s+", "_", (t or "").strip())


def strip_rep(g):                       # GSMs never carry a _r1/_R2 run-replicate suffix
    return re.sub(r"_[rR]\d+$", "", g or "")


def format_package_runs(p, gse):
    exp = p.find("EXPERIMENT"); study = p.find("STUDY"); sample = p.find("SAMPLE"); subm = p.find("SUBMISSION")
    srx = exp.attrib.get("accession", "") if exp is not None else ""
    center = ""
    for src in (exp, subm, study):
        if src is not None and src.attrib.get("center_name"):
            center = src.attrib.get("center_name"); break
    if not center and subm is not None:
        center = subm.attrib.get("broker_name", "")

    assay = source = selection = layout = platform = instrument = libname = ""
    if exp is not None:
        ld = exp.find("DESIGN/LIBRARY_DESCRIPTOR")
        if ld is not None:
            assay = C.txt(ld.find("LIBRARY_STRATEGY")); source = C.txt(ld.find("LIBRARY_SOURCE"))
            selection = C.txt(ld.find("LIBRARY_SELECTION")); libname = C.txt(ld.find("LIBRARY_NAME"))
            ll = ld.find("LIBRARY_LAYOUT")
            if ll is not None and len(ll): layout = ll[0].tag
        pl = exp.find("PLATFORM")
        if pl is not None and len(pl):
            platform = pl[0].tag; instrument = C.txt(pl[0].find("INSTRUMENT_MODEL"))

    srp = study.attrib.get("accession", "") if study is not None else ""
    bioproj = C.ext_id(study.find("IDENTIFIERS"), "BioProject") if study is not None else ""

    gsm_exp = ""
    if exp is not None:
        gsm_exp = C.ext_id(exp.find("IDENTIFIERS"), "GEO")
        if not gsm_exp:
            for ea in exp.findall("EXPERIMENT_ATTRIBUTES/EXPERIMENT_ATTRIBUTE"):
                if C.txt(ea.find("TAG")).lower() in ("geo accession", "geo_accession"):
                    gsm_exp = C.txt(ea.find("VALUE"))
    gsm_samp = biosample = organism = sample_name = ""
    attrs = {}
    if sample is not None:
        sids = sample.find("IDENTIFIERS")
        biosample = C.ext_id(sids, "BioSample"); gsm_samp = C.ext_id(sids, "GEO")
        sample_name = strip_rep(sample.attrib.get("alias", "") or gsm_samp)
        sn = sample.find("SAMPLE_NAME")
        if sn is not None: organism = C.txt(sn.find("SCIENTIFIC_NAME"))
        sa = sample.find("SAMPLE_ATTRIBUTES")
        if sa is not None:
            for a in sa.findall("SAMPLE_ATTRIBUTE"):
                tg = clean_tag(C.txt(a.find("TAG"))); vl = C.txt(a.find("VALUE"))
                if not tg or tg.lower() in ("ena-spot-count", "ena-base-count", "ena_first_public", "ena_last_update"):
                    continue
                attrs[tg] = vl
    gsm = strip_rep(gsm_exp or gsm_samp)

    rset = p.find("RUN_SET")
    for run in (rset.findall("RUN") if rset is not None else []):
        spots = run.attrib.get("total_spots", ""); bases = run.attrib.get("total_bases", "")
        avglen = ""
        try:
            if spots and bases and int(spots) > 0: avglen = round(int(bases) / int(spots))
        except Exception: pass
        fts, provs, regs = set(), set(), set()
        for cf in run.findall("CloudFiles/CloudFile"):
            ft = cf.attrib.get("filetype", ""); pr = cf.attrib.get("provider", ""); lo = cf.attrib.get("location", "")
            if ft: fts.add("sra" if ft == "run" else ft)
            if pr: provs.add(pr)
            if lo: regs.add(lo)
        if run.attrib.get("is_public", "true") == "true" and fts:
            provs.add("ncbi"); regs.add("ncbi.public")
        create_dt = ver = ""
        sfs = run.findall("SRAFiles/SRAFile")            # walk the subtree ONCE (was up to twice per run)
        norm = next((sf for sf in sfs if sf.attrib.get("semantic_name") == "SRA Normalized"), None)
        if norm is None:
            norm = sfs[0] if sfs else None
        if norm is not None:
            create_dt = iso_minute(norm.attrib.get("date", "")); ver = norm.attrib.get("version", "")
        if not create_dt: create_dt = iso_minute(run.attrib.get("published", ""))

        row = {"Run": run.attrib.get("accession", ""), "Assay Type": assay, "AvgSpotLen": avglen,
               "Bases": bases, "BioProject": bioproj, "BioSample": biosample,
               "Bytes": run.attrib.get("size", ""), "Center Name": center,
               "Consent": "public" if run.attrib.get("is_public", "true") == "true" else "restricted",
               "DATASTORE filetype": ",".join(sorted(fts)), "DATASTORE provider": ",".join(sorted(provs)),
               "DATASTORE region": ",".join(sorted(regs)), "Experiment": srx,
               "GEO_Accession (exp)": gsm, "Instrument": instrument, "Library Name": libname,
               "LibraryLayout": layout,
               "LibrarySelection": selection, "LibrarySource": source, "Organism": organism,
               "Platform": platform, "ReleaseDate": iso_date_only(run.attrib.get("published", "")),
               "create_date": create_dt, "version": ver, "Sample Name": sample_name,
               "SRA Study": srp, "GSE_Series": gse}
        row.update(attrs)
        yield row


def geo_soft_map(soft_path):
    """Parse a cached GEO SOFT file -> {experiment_accession: {'gsm':..., 'chars':{tag:val}}} for ENA overlay."""
    out = {}
    if not os.path.exists(soft_path):
        return out
    soft = open(soft_path, encoding="utf-8", errors="replace").read()
    for b in re.split(r"(?m)^\^SAMPLE = ", soft)[1:]:
        gsm = b.splitlines()[0].strip(); chars = {}; srx = ""
        for line in b.splitlines():
            if line.startswith("!Sample_source_name_ch1 ="):
                chars["source_name"] = line.split("=", 1)[1].strip()
            elif line.startswith("!Sample_characteristics_ch1 ="):
                v = line.split("=", 1)[1].strip()
                if ":" in v:
                    tg, vl = v.split(":", 1); chars[clean_tag(tg)] = vl.strip()
            else:
                m = re.search(r"term=([EDS]RX\d+)", line)
                if m: srx = m.group(1)
        if srx: out[srx] = {"gsm": gsm, "chars": chars}
    return out


STANDARD = ["Run", "Assay Type", "AvgSpotLen", "Bases", "BioProject", "BioSample", "Bytes", "Center Name", "Consent",
            "DATASTORE filetype", "DATASTORE provider", "DATASTORE region", "Experiment", "GEO_Accession (exp)",
            "Instrument", "LibraryLayout", "LibrarySelection", "LibrarySource", "Organism", "Platform", "ReleaseDate",
            "create_date", "version", "Sample Name", "SRA Study"]


def order_columns(keys, add_gse=True):
    keys = set(keys); keys.discard("Run")
    if not add_gse: keys.discard("GSE_Series")
    rest = sorted(keys, key=lambda s: s.lower())
    for k in ("create_date", "version"):
        if k in rest: rest.remove(k)
    out = ["Run"]
    for k in rest:
        out.append(k)
        if k == "ReleaseDate": out += ["create_date", "version"]
    if add_gse and "GSE_Series" in keys:
        out.remove("GSE_Series"); out.append("GSE_Series")
    return out


def parse_study(gse, cache_dir):
    """Return list of run-rows for one GSE from its cached XML (+ GEO overlay if ENA-brokered)."""
    xmlf = os.path.join(cache_dir, gse + ".full.xml")
    if not os.path.exists(xmlf):
        return []
    try:
        with open(xmlf, encoding="utf-8", errors="replace") as f:
            root = ET.fromstring(C.clean_sra_xml(f.read()))           # strip any NCBI error pages injected mid-stream
    except Exception as e:                                            # malformed/truncated XML -> log + skip, don't crash the build
        print(f"  WARNING: {gse} XML parse failed ({e}); skipping study")
        return []
    soft = geo_soft_map(os.path.join(cache_dir, gse + ".soft.txt"))   # empty unless ENA-brokered
    rows = []
    for p in root.findall("EXPERIMENT_PACKAGE"):
        for row in format_package_runs(p, gse):
            if soft:
                g = soft.get(row.get("Experiment", ""))
                if g:
                    for k in [k for k in list(row) if k not in STANDARD and k != "GSE_Series"]:
                        del row[k]                                  # drop sparse ENA attrs
                    row["GEO_Accession (exp)"] = g["gsm"]; row["Sample Name"] = g["gsm"]
                    row.update(g["chars"])                          # overlay GEO characteristics
            rows.append(row)
    return rows


# columns that may carry a cell-line value (after clean_tag normalization)
CELLLINE_COL_RE = re.compile(r"cell[_ ]?line|cell[_ ]?type", re.I)


def cellline_columns(cols):
    """Pick the columns whose values are candidate cell-line names."""
    out = [c for c in cols if CELLLINE_COL_RE.search(c)]
    if "source_name" in cols and "source_name" not in out:
        out.append("source_name")
    return out


def _xml_size(cache_dir, gse):
    """Byte size of a study's cached XML (0 if absent); used to parse the biggest studies FIRST."""
    try:
        return os.path.getsize(os.path.join(cache_dir, gse + ".full.xml"))
    except OSError:
        return 0


def run(P, sel, reporter=NULL):
    """Reconstruct all runs for the selected studies; write the all-runs CSV + match candidates.

    Parsing the cached SRA XML (tens of GB; individual studies can exceed 150 MB) is CPU-bound DOM work, so
    studies are parsed in PARALLEL across PROCESSES -- threads can't help under the GIL. The worker count
    SCALES TO THE MACHINE (one per core minus one, capped) so a 4-core laptop and a many-core server both
    behave; set env RUNTABLE_BUILD_WORKERS to tune (lower it on a low-RAM box; =1 forces the sequential path)."""
    studies = sel.get("studies", [])
    total = len(studies)
    reporter.set_total(total)
    cache_dir = P.xml_cache_dir

    cpu = os.cpu_count() or 1
    try:
        env_workers = int(os.environ.get("RUNTABLE_BUILD_WORKERS", "0"))
    except ValueError:
        env_workers = 0
    # one worker per core, leave one for the OS; cap so a many-core box doesn't hold dozens of multi-GB DOM
    # trees at once -- on the 150 MB studies RAM is the limit, not cores.
    workers = env_workers if env_workers > 0 else max(1, min(cpu - 1, 12))
    workers = max(1, min(workers, total))                            # never more workers than studies

    parsed = {}                                                      # gse -> rows (filled below, possibly out of order)
    if workers == 1 or total <= 2:                                  # single core / tiny job: skip the pool overhead
        for gse in studies:
            parsed[gse] = parse_study(gse, cache_dir)
            print(f"  {gse:<12} {len(parsed[gse])} runs")
            reporter.advance(1)
            reporter.set_detail(f"{gse}: {len(parsed[gse])} runs")
    else:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        order = sorted(studies, key=lambda g: _xml_size(cache_dir, g), reverse=True)   # biggest first
        print(f"  parsing {total} studies across {workers} process(es) ({cpu} core(s) detected)")
        try:
            with ProcessPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(parse_study, gse, cache_dir): gse for gse in order}
                done = 0
                for fut in as_completed(futs):
                    gse = futs[fut]
                    try:
                        rows = fut.result()
                    except Exception as e:                          # one unparseable study must not kill the build
                        print(f"  WARNING: {gse} parse failed in worker ({e}); skipping study")
                        rows = []
                    parsed[gse] = rows
                    done += 1
                    print(f"  [{done}/{total}] {gse:<12} {len(rows)} runs")
                    reporter.advance(1)
                    reporter.set_detail(f"{gse}: {len(rows)} runs")
        except Exception as e:                                      # restricted env without a usable process pool
            print(f"  WARNING: parallel parse unavailable ({e}); falling back to single process")
            for gse in studies:
                if gse not in parsed:
                    parsed[gse] = parse_study(gse, cache_dir)
                    reporter.advance(1)

    all_rows = []                                                    # concat in ORIGINAL order -> deterministic CSV
    for gse in studies:
        all_rows.extend(parsed.get(gse, []))

    if not all_rows:                                                  # header-only runtable -> nothing downstream can match
        print("  !!! WARNING: ZERO run rows extracted across all studies -- runtable will be header-only "
              "(check cached XML / study selection); downstream matching will find nothing.")

    keys = set()
    for r in all_rows:
        keys.update(r)
    cols = order_columns(keys, add_gse=True)
    os.makedirs(P.runtable_dir, exist_ok=True)
    with open(P.runtable_all_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore"); w.writeheader()
        for r in all_rows:
            w.writerow(r)

    # candidate cell-line values for the AI matcher
    cl_cols = cellline_columns(cols)
    counts, col_seen = {}, {}
    for r in all_rows:
        for c in cl_cols:
            v = (r.get(c) or "").strip()
            if v:
                counts[v] = counts.get(v, 0) + 1
                col_seen.setdefault(v, set()).add(c)
    candidates = [{"value": v, "columns": sorted(col_seen[v]), "n_runs": counts[v]}
                  for v in sorted(counts, key=lambda x: -counts[x])]
    payload = {"target": sel.get("canonical", ""),
               "target_aliases": sorted(sel.get("raw_tags", {}).keys()),
               "cellline_columns": cl_cols,
               "candidates": candidates}
    with open(P.match_candidates, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"  RUNTABLE: {len(all_rows)} runs, {len(cols)} cols -> {os.path.basename(P.runtable_all_csv)}"
          f" | {len(candidates)} distinct cell-line values")
    reporter.set_detail(f"{len(all_rows)} runs, {len(candidates)} cell-line values")
    return {"n_runs": len(all_rows), "n_studies": len(studies), "n_candidates": len(candidates)}


# ----- validation harness: prove the reconstruction matches the real Run Selector export -----
EXPECTED = (
 'Run,Assay Type,AvgSpotLen,Bases,BioProject,BioSample,Bytes,cell_type,Center Name,Consent,'
 'DATASTORE filetype,DATASTORE provider,DATASTORE region,Experiment,GEO_Accession (exp),Instrument,'
 'LibraryLayout,LibrarySelection,LibrarySource,Organism,Platform,ReleaseDate,create_date,version,'
 'Sample Name,source_name,SRA Study,treatment\n'
 'SRR8769935,RNA-Seq,180,2300409360,PRJNA528639,SAMN11233512,1570064261,DBTRG,GEO,public,'
 '"run.zq,sra","gs,ncbi,s3","gs.us-east1,ncbi.public,s3.us-east-1",SRX5559996,GSM3683686,'
 'Illumina HiSeq 2000,PAIRED,cDNA,TRANSCRIPTOMIC,Homo sapiens,ILLUMINA,2019-03-25T00:00:00Z,'
 '2019-03-22T13:26:00Z,1,GSM3683686,DBTRG-05MG,SRP189165,1mM dbcAMP_0h')


def validate():
    we, qk, _ = C.esearch_history("sra", "SRP189165")
    root = ET.fromstring(C.efetch_sra_full(we, qk))
    gen = {}
    for p in root.findall("EXPERIMENT_PACKAGE"):
        for r in format_package_runs(p, "SRP189165"):
            gen[r["Run"]] = r
    exp = list(csv.DictReader(io.StringIO(EXPECTED)))[0]
    run_acc = exp["Run"]; ok = True
    for col, want in exp.items():
        got = str(gen.get(run_acc, {}).get(col, ""))
        if got != want:
            print("MISMATCH [%s] want=%r got=%r" % (col, want, got)); ok = False
    print("VALIDATION:", "PASS - reconstruction matches the official SraRunTable" if ok else "FAIL")
    return ok


def main():
    import sys
    import argparse
    from pipeline_paths import Paths
    if "--validate" in sys.argv:
        validate(); return
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    a = ap.parse_args()
    P = Paths(a.run_dir).ensure_dirs()
    sel = json.load(open(P.cellline_selection, encoding="utf-8"))
    run(P, sel)


if __name__ == "__main__":
    main()
