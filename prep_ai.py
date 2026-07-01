"""
Stage 3 — PREP: build the two AI batch sets from the extracted data.

Pass 1 (compounds): distinct cleaned compound strings -> compound_batches/cmpd_*.json
Pass 2 (samples):   distinct "cell descriptors" needing AI -> sample_batches/samp_*.json
  = (every distinct structured `cell line` tag value, for canonicalization)
  UNION (sample titles for samples that lack a usable cell tag OR lack a treatment field).
  Titles are deduped by a normalized key; sample_index.json maps each representative
  descriptor -> the raw titles/tag-values it stands for (so merge expands results back).
"""
import os
import re
import json

from normalize_v2 import clean_compound
from cell_utils import clean_struct_cell
from progress import NULL

CMPD_PER = 250
SAMP_PER = 200


def title_key(t):
    """Normalize a title for dedup: drop species/assay suffix, brackets, rep/dose tags."""
    k = t.split(";")[0].strip()
    k = re.sub(r"[\[\(][^\]\)]*[\]\)]\s*$", "", k)
    k = re.sub(r"[\s_,\-]+(?:rep(?:licate)?|r|n|donor|batch|set|run|lane)\s*\d+\s*$", "", k, flags=re.I)
    k = re.sub(r"[\s_,\-]+\d+\.?\d*\s*(?:[unmpµμ]m|[unmpµμ]?g/?m?l?|%|nM|uM)\s*$", "", k, flags=re.I)
    k = re.sub(r"[\s_,\-]+\d+\s*$", "", k)
    return re.sub(r"\s+", " ", k).strip().lower() or t.strip().lower()


def chunk_write(folder, prefix, items, per):
    os.makedirs(folder, exist_ok=True)
    n = 0
    for i in range(0, len(items), per):
        with open(os.path.join(folder, f"{prefix}_{n:03d}.json"), "w", encoding="utf-8") as f:
            json.dump(items[i:i + per], f, ensure_ascii=False)
        n += 1
    return n


def build_batches(P, reporter=NULL):
    """Build compound + sample batch inputs and sample_index.json."""
    reporter.set_detail("building AI batches…")

    if not os.path.exists(P.raw_json):
        raise FileNotFoundError(
            f"PREP: raw metadata JSON missing: {P.raw_json} "
            f"(produced by the FETCH stage)")
    if not os.path.exists(P.samples_jsonl):
        raise FileNotFoundError(
            f"PREP: structured samples JSONL missing: {P.samples_jsonl} "
            f"(produced by the EXTRACT stage)")

    try:
        with open(P.raw_json, encoding="utf-8") as f:
            raw = json.load(f)
    except (ValueError, OSError) as e:
        raise ValueError(
            f"PREP: corrupt raw metadata JSON: {P.raw_json} "
            f"(produced by the FETCH stage) — {e}")
    result = raw.get("result")
    if not isinstance(result, dict):
        raise ValueError(
            f"PREP: raw metadata JSON has no usable 'result': {P.raw_json} "
            f"(produced by the FETCH stage)")

    struct = []
    with open(P.samples_jsonl, encoding="utf-8") as f:
        for l in f:
            if not l.strip():
                continue
            try:
                struct.append(json.loads(l))
            except ValueError:
                # Skip a malformed/torn (e.g. trailing) line rather than crash the stage.
                continue
    struct_by_gsm = {r["gsm"]: r for r in struct if r.get("gsm")}

    # ---- Pass 1: distinct cleaned compounds ----
    compounds = set()
    for r in struct:
        for v in r.get("treatments_raw", []):
            c = clean_compound(v)
            if c:
                compounds.add(c)
    n_cmpd = chunk_write(P.compound_batches, "cmpd", sorted(compounds), CMPD_PER)

    # ---- Pass 2: cell descriptors needing AI ----
    cell_values = set()          # raw structured cell-line tag values to canonicalize
    title_groups = {}            # title_key -> [raw titles]
    for u in result.get("uids", []):
        for s in (result.get(u) or {}).get("samples", []):
            gsm = s.get("accession", "")
            title = s.get("title", "")
            sd = struct_by_gsm.get(gsm)
            cell_tag = (sd or {}).get("cell_line", "")
            has_cell = bool(clean_struct_cell(cell_tag))
            has_treat = bool(sd and sd.get("treatments_raw"))
            if cell_tag:
                cell_values.add(cell_tag)
            if (not has_cell) or (not has_treat):
                title_groups.setdefault(title_key(title), []).append(title)

    sample_index, descriptors = {}, []
    for v in sorted(cell_values):
        sample_index[v] = [v]
        descriptors.append(v)
    for raws in title_groups.values():
        uniq = sorted(set(raws))
        rep = min(uniq, key=len)
        sample_index.setdefault(rep, [])
        sample_index[rep] = sorted(set(sample_index[rep]) | set(uniq))
        descriptors.append(rep)

    descriptors = sorted(set(descriptors))

    if not compounds and not descriptors:
        raise ValueError(
            f"PREP: no compounds and no sample descriptors built from "
            f"{P.samples_jsonl} / {P.raw_json} "
            f"(structured-samples={len(struct)}, result-uids={len(result.get('uids', []))}) "
            f"— refusing to write 0 batches, which would make the AI/MERGE stages a no-op")

    with open(P.sample_index, "w", encoding="utf-8") as f:
        json.dump(sample_index, f, ensure_ascii=False)
    n_samp = chunk_write(P.sample_batches, "samp", descriptors, SAMP_PER)

    print(f"  PREP: compounds={len(compounds)} ({n_cmpd} batches) | "
          f"sample-descriptors={len(descriptors)} ({n_samp} batches) "
          f"[cell-values={len(cell_values)}, title-groups={len(title_groups)}]")
    reporter.set_detail(f"{len(compounds)} compounds ({n_cmpd} batches), "
                        f"{len(descriptors)} sample descriptors ({n_samp} batches)")
    return {"compound_batches": n_cmpd, "sample_batches": n_samp}


def main():
    import argparse
    from pipeline_paths import Paths
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    a = ap.parse_args()
    build_batches(Paths(a.run_dir).ensure_dirs())


if __name__ == "__main__":
    main()
