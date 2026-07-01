"""
Stage 6 — MERGE: combine AI batch result files into the two lookup maps build_final consumes.

  compound_results/cmpd_*.json -> compound_map.json  {raw_compound: {name, is_drug}}
  sample_results/samp_*.json   -> sample_map.json     {raw_title_or_cellvalue: {cell_line, category, drug_treated}}
                                  (expanded from representatives via sample_index.json)

Batch counts are discovered by glob (never hardcoded). Tolerant JSON parsing.
"""
import os
import re
import json
import glob


def load_json_loose(path):
    with open(path, encoding="utf-8") as f:
        txt = f.read().strip()
    try:
        return json.loads(txt)
    except Exception:
        pass
    m = re.search(r"\{.*\}", txt, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def _merge_dir(folder, prefix):
    merged, ok, bad = {}, 0, []
    files = sorted(glob.glob(os.path.join(folder, f"{prefix}_*.json")))
    for p in files:
        d = load_json_loose(p)
        if isinstance(d, dict) and d:
            merged.update(d)
            ok += 1
        else:
            bad.append(os.path.basename(p))
    return merged, ok, len(files), bad


def merge_compounds(P):
    merged, ok, total, bad = _merge_dir(P.compound_results, "cmpd")
    tmp = P.compound_map + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, P.compound_map)
    print(f"  MERGE compounds: {ok}/{total} batches ok -> {len(merged):,} entries"
          + (f"  bad={bad}" if bad else ""))
    return merged


def merge_samples(P):
    reps, ok, total, bad = _merge_dir(P.sample_results, "samp")
    index = {}
    if os.path.exists(P.sample_index):
        with open(P.sample_index, encoding="utf-8") as f:
            index = json.load(f)
    sample_map = {}
    for rep, info in reps.items():
        members = index.get(rep, [rep])
        for raw in members:
            sample_map[raw] = info
    tmp = P.sample_map + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sample_map, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, P.sample_map)
    print(f"  MERGE samples: {ok}/{total} batches ok -> {len(reps):,} reps -> "
          f"{len(sample_map):,} raw entries" + (f"  bad={bad}" if bad else ""))
    return sample_map


def main():
    import argparse
    from pipeline_paths import Paths
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    a = ap.parse_args()
    P = Paths(a.run_dir)
    merge_compounds(P)
    merge_samples(P)


if __name__ == "__main__":
    main()
