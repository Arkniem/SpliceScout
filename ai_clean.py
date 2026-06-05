"""
Stage 4/5 — AI cleaning via the Anthropic API (replaces the Claude Code Workflow fan-out).

Two passes, each over one vocabulary, written to the same ai_work/*_results/ files that
merge_ai.py consumes:
  - "compounds": raw compound string -> {name (standard generic), is_drug}     (canon + synonym)
  - "samples":   title OR cell-tag value -> {cell_line, category, drug_treated} (title-classify + cell-canon)

Forced single tool-use returns an ARRAY of {raw, ...}; we pivot it into the {raw: {...}} map.
Shared instruction block is prompt-cached. Concurrency-capped, resumable, streamed (no timeouts).
Requires: pip install anthropic ; env ANTHROPIC_API_KEY.
"""
import os
import re
import json
import glob
import asyncio

from progress import NULL
import llm_providers

RATE_RETRY_SECS = 30   # on a provider rate-limit (429), wait this long and re-submit the SAME batch

CATEGORIES = ["Cell line", "Primary cells", "Immune/PBMC", "iPSC/ESC",
              "Organoid", "Tissue", "Patient/Tumor", "Single-cell", "Unknown"]

COMPOUND_INSTRUCTIONS = (
    "You canonicalize drug/compound names from NCBI RNA-seq metadata. You receive a JSON array "
    "of raw compound/treatment strings. For EVERY input string call the emit tool once with a "
    "result object: \n"
    "- raw: the EXACT input string, unchanged.\n"
    "- name: the canonical STANDARD GENERIC / INN name. Lowercase generics; strip salts/hydrates "
    "(imatinib mesylate -> imatinib); expand abbreviations (5FU/5-FU -> fluorouracil, DOX -> "
    "doxorubicin, AraC -> cytarabine, CDDP -> cisplatin, MTX -> methotrexate, ATRA -> tretinoin); "
    "convert trade names to generic (Gleevec -> imatinib, Taxol -> paclitaxel, Velcade -> "
    "bortezomib, Adriamycin -> doxorubicin); unify spellings (Rifampin = rifampicin); fix typos. "
    "For COMBINATIONS ('A + B', 'A and B') standardize each component and join the sorted "
    "components with ' + '. Keep research-tool compounds/codes with no generic as a tidy lowercase "
    "form. Keep herbal / natural-product names as a tidy form. If the value is not a treatment, "
    "set name to the cleaned original.\n"
    "- is_drug: true if it is a real drug / small molecule / compound / biologic / natural-product "
    "treatment; false if it is a control/vehicle (DMSO, PBS, untreated, none, water, mock), a "
    "genetic reagent (siRNA, shRNA, sgRNA, CRISPR, plasmid, overexpression, knockout, "
    "non-targeting), a non-compound label (e.g. 'factors', 'Polyplex'), a bare number, or a "
    "cell-line name.\n"
    "Return one result per input, preserving every exact raw string."
)

SAMPLE_INSTRUCTIONS = (
    "You classify human RNA-seq samples. You receive a JSON array of strings; each is EITHER a full "
    "sample title OR a short 'cell line' field value. For EVERY input string call the emit tool once "
    "with a result object:\n"
    "- raw: the EXACT input string, unchanged.\n"
    "- cell_line: the canonical human cell line name if identifiable (merge formatting variants: "
    "'Hep G2'/'HepG2 cells' -> 'HepG2'; extract from prose: 'colorectal cancer cell line HCT116' -> "
    "'HCT116'; keep genuinely distinct sublines/derivatives). If it is NOT a named cell line, set "
    "cell_line to the SAME value as category (the bucket name).\n"
    "- category: EXACTLY one of " + " | ".join(CATEGORIES) + ". Use 'Cell line' for a real line; "
    "map patient/sample codes (e.g. '14-00613','JNJ001','BRx-50','PDAC060') to 'Patient/Tumor'; "
    "pure numbers / '--' / cryptic plate codes to 'Unknown'.\n"
    "- drug_treated: if the input is a FULL SAMPLE TITLE, 'Drug Treated' when the sample was exposed "
    "to a drug / compound / small molecule / inhibitor; 'Not Drug Treated' for a clear control / "
    "vehicle / untreated / time-only sample or a genetic perturbation (KO/siRNA/shRNA/CRISPR/"
    "overexpression); and 'Undetermined' when the title does NOT make it clear either way (ambiguous, "
    "or no treatment information in the title). If the input is a short cell-line field value (not a "
    "full title), use 'N/A'.\n"
    "Return one result per input, preserving every exact raw string."
)

COMPOUND_TOOL = {
    "name": "emit_compounds",
    "description": "Return the canonicalization for every input compound string.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["results"],
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["raw", "name", "is_drug"],
                    "properties": {
                        "raw": {"type": "string"},
                        "name": {"type": "string"},
                        "is_drug": {"type": "boolean"},
                    },
                },
            }
        },
    },
}

SAMPLE_TOOL = {
    "name": "emit_samples",
    "description": "Return cell line, category, and drug-treated status for every input string.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["results"],
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["raw", "cell_line", "category", "drug_treated"],
                    "properties": {
                        "raw": {"type": "string"},
                        "cell_line": {"type": "string"},
                        "category": {"type": "string", "enum": CATEGORIES},
                        "drug_treated": {"type": "string",
                                         "enum": ["Drug Treated", "Not Drug Treated",
                                                  "Undetermined", "N/A"]},
                    },
                },
            }
        },
    },
}

PASSES = {
    "compounds": dict(in_dir="compound_batches", out_dir="compound_results", prefix="cmpd",
                      instr=COMPOUND_INSTRUCTIONS, tool=COMPOUND_TOOL,
                      pivot=lambda r: {"name": r["name"], "is_drug": r["is_drug"]}),
    "samples": dict(in_dir="sample_batches", out_dir="sample_results", prefix="samp",
                    instr=SAMPLE_INSTRUCTIONS, tool=SAMPLE_TOOL,
                    pivot=lambda r: {"cell_line": r["cell_line"], "category": r["category"],
                                     "drug_treated": r["drug_treated"]}),
}


def _result_ok(path):
    if not os.path.exists(path):
        return False
    try:
        return bool(json.load(open(path, encoding="utf-8")))
    except Exception:
        return False


def _atomic_write(path, obj):
    tmp = path + ".tmp"
    json.dump(obj, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, path)


async def _classify_batch(client, provider, spec, in_path, out_path, model, max_tokens, cost_log):
    items = json.load(open(in_path, encoding="utf-8"))
    results, usage = await llm_providers.classify(
        client, provider, model, spec["instr"], items, spec["tool"], max_tokens)
    if not results:
        raise RuntimeError(f"no output for {os.path.basename(in_path)}")
    out = {}
    for r in results:
        if isinstance(r, dict) and r.get("raw"):
            try:
                out[r["raw"]] = spec["pivot"](r)
            except Exception:
                pass        # skip a malformed result rather than failing the whole batch
    if not out:
        raise RuntimeError(f"empty/invalid output for {os.path.basename(in_path)}")
    _atomic_write(out_path, out)
    with open(cost_log, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "pass": spec["prefix"], "batch": os.path.basename(in_path),
            "provider": provider, "model": model,
            "n_in": len(items), "n_out": len(out),
            "input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"],
            "cache_read": usage["cache_read"], "cache_creation": usage["cache_creation"],
        }) + "\n")
    return len(out)


async def _run_pass_async(pass_name, P, cfg, reporter=NULL):
    spec = PASSES[pass_name]
    provider = llm_providers.normalize_provider(cfg.get("provider", "anthropic"))
    model = llm_providers.resolve_model(provider, cfg.get("model"))
    in_dir = os.path.join(P.work_dir, spec["in_dir"])
    out_dir = os.path.join(P.work_dir, spec["out_dir"])
    os.makedirs(out_dir, exist_ok=True)
    batches = sorted(glob.glob(os.path.join(in_dir, f'{spec["prefix"]}_*.json')))
    todo = [b for b in batches
            if not _result_ok(os.path.join(out_dir, os.path.basename(b)))]
    print(f"[AI:{pass_name}] {len(batches)} batches, {len(todo)} to do "
          f"(provider={provider}, model={model}, concurrency={cfg['concurrency']})")
    # progress: total = all batches; pre-credit any already-complete (resume)
    reporter.set_total(len(batches))
    if len(batches) - len(todo):
        reporter.advance(len(batches) - len(todo))
    reporter.set_detail(f"{len(todo)} of {len(batches)} batches to process")
    if not todo:
        return {"done": len(batches), "ran": 0}

    client = llm_providers.make_client(provider, cfg.get("max_retries", 8),
                                       base_url=cfg.get("base_url"))
    sem = asyncio.Semaphore(cfg["concurrency"])
    completed = [len(batches) - len(todo)]

    async def worker(b):
        async with sem:
            out_path = os.path.join(out_dir, os.path.basename(b))
            result = 0
            attempt = 0
            while True:
                try:
                    result = await _classify_batch(client, provider, spec, b, out_path, model,
                                                   cfg.get("max_tokens", 16000), P.cost_log)
                    break
                except Exception as e:
                    if llm_providers.classify_ai_error(e).get("category") == "rate":
                        # transient throttle -> just re-submit later; don't count it as a failure
                        print(f"  ~ {os.path.basename(b)} rate limited; re-submitting in {RATE_RETRY_SECS}s…")
                        reporter.set_detail(f"rate limited — re-submitting batch in {RATE_RETRY_SECS}s…")
                        await asyncio.sleep(RATE_RETRY_SECS)
                        continue
                    attempt += 1
                    if attempt >= 2:
                        print(f"  ! {os.path.basename(b)} failed: {e}")
                        result = 0
                        break
                    await asyncio.sleep(2)
            completed[0] += 1
            reporter.advance(1)
            reporter.set_detail(f"batch {completed[0]}/{len(batches)}")
            return result

    # warm the prompt cache: run the first batch alone, then fan out the rest
    await worker(todo[0])
    if len(todo) > 1:
        await asyncio.gather(*(worker(b) for b in todo[1:]))
    await llm_providers.close_client(client)
    print(f"[AI:{pass_name}] complete.")
    return {"done": len(batches), "ran": len(todo)}


def run_pass(pass_name, P, cfg, reporter=NULL):
    """Sync entry point used by the orchestrator."""
    return asyncio.run(_run_pass_async(pass_name, P, cfg, reporter))


async def _preflight_async(provider, model, base_url=None):
    client = llm_providers.make_client(provider, max_retries=2, timeout=25, base_url=base_url)
    try:
        results, _ = await llm_providers.classify(
            client, provider, model, COMPOUND_INSTRUCTIONS, ["aspirin"], COMPOUND_TOOL, max_tokens=200)
    finally:
        await llm_providers.close_client(client)
    if not results:
        raise RuntimeError("the model returned no structured output "
                           "(it may not support tool/function calling)")


def preflight(cfg):
    """Validate provider + model + key with ONE tiny live call before the batch fan-out.

    Returns None if AI cleaning is usable, else the raised exception (classify it with
    llm_providers.classify_ai_error). A valid-but-throttled provider still passes — a 1-item call is
    fast even when 250-item batches are slow — so this only trips on a real misconfig (bad key/model).
    """
    provider = llm_providers.normalize_provider(cfg.get("provider", "anthropic"))
    model = llm_providers.resolve_model(provider, cfg.get("model"))
    try:
        asyncio.run(_preflight_async(provider, model, cfg.get("base_url")))
        return None
    except Exception as e:
        return e


def main():
    import argparse
    from pipeline_paths import Paths
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--pass", dest="pass_name", required=True, choices=list(PASSES))
    ap.add_argument("--provider", default="anthropic", choices=list(llm_providers.PROVIDERS))
    ap.add_argument("--model", default=None)
    ap.add_argument("--concurrency", type=int, default=8)
    a = ap.parse_args()
    P = Paths(a.run_dir).ensure_dirs()
    run_pass(a.pass_name, P, {"provider": a.provider, "model": a.model, "concurrency": a.concurrency})


if __name__ == "__main__":
    main()
