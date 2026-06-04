# -*- coding: utf-8 -*-
"""
Deep-dive stage — CELL-LINE MATCH + FILTER (the disambiguation agent).

The reconstructed run table for the selected studies contains runs from MANY cell lines, and the
same line is written many ways across studies (A549 / A-549 / A 549 / "A549 cells"). This stage
decides which cell-line-name values are the target line and keeps only those runs.

Hybrid keep rule (maximize recall without widening past the target line):
  keep a run if  (a) its GSM was already classified as the target line by the main pipeline
                     (deterministic floor, always available from cellline_selection.json), OR
                 (b) its cell-line value was blessed by the agent (recall for spelling variants).

The agent (Anthropic API, forced tool-use, prompt-cached) includes a value ONLY if it is, or
clearly resembles, the target line; unrelated lines (BEAS-2B) are excluded. When skip_ai, a
deterministic normalized-equality fallback is used (lowercase + strip non-alphanumeric), which
still merges A549 / A-549 / A 549.

MAIN OUTPUT: SraAccList.txt — the flat list of SRR run accessions for the kept (target-line) runs,
consumable by `prefetch --option-file SraAccList.txt`. Written both combined and per-study
(by_study/<GSE>/SraAccList.txt), matching the user's SRA_download_scripts layout.
"""
import os
import re
import csv
import json
import asyncio

from progress import NULL
from runtable_build import order_columns, strip_rep
from build_final import _safe_open
import llm_providers

INSTRUCTIONS = (
    "You decide which cell-line-name strings refer to the SAME human cell line as a given target.\n"
    "You receive a JSON object with `target` (the canonical target line), `target_aliases` (other "
    "spellings already known to be that line), and `candidates` (an array of cell-line-name values "
    "seen in SRA metadata, each with the column(s) it came from and a run count).\n"
    "For EVERY candidate, call the emit tool once with a result object:\n"
    "- value: the EXACT candidate string, unchanged.\n"
    "- matches: true ONLY IF the value IS, or clearly RESEMBLES, the target line — i.e. the same line "
    "written with different formatting/spacing/case, a trailing 'cells'/'cell line', or a same-root "
    "tagged form (e.g. for target 'A549': 'A549', 'A-549', 'A 549', 'A549 cells', 'A549-GFP' all "
    "match). Set matches=false for a clearly DIFFERENT line (e.g. 'BEAS-2B' when the target is "
    "'A549'). When genuinely unsure whether two names are the same line, return false.\n"
    "- canonical: your normalized name for the value (used only for logging).\n"
    "- reason: a few words (e.g. 'hyphenation variant', 'different line').\n"
    "Return one result per candidate, preserving every exact value string."
)

TOOL = {
    "name": "emit_cellline_matches",
    "description": "Return, for every candidate cell-line value, whether it is the target line.",
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
                    "required": ["value", "matches", "canonical", "reason"],
                    "properties": {
                        "value": {"type": "string"},
                        "matches": {"type": "boolean"},
                        "canonical": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            }
        },
    },
}


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _deterministic(target, aliases, candidate_values):
    """Normalized-equality match: A549 == A-549 == A 549 == 'A549 cells' (-> a549)."""
    targets = {_norm(target)} | {_norm(a) for a in aliases}
    targets.discard("")
    out = {}
    for v in candidate_values:
        nv = _norm(v)
        hit = nv in targets or any(nv == t for t in targets)
        out[v] = {"matches": bool(hit), "canonical": target if hit else v,
                  "reason": "normalized match" if hit else "no normalized match"}
    return out


async def _classify_async(payload, ai_cfg, cost_log):
    ai_cfg = ai_cfg or {}
    provider = llm_providers.normalize_provider(ai_cfg.get("provider", "anthropic"))
    model = llm_providers.resolve_model(provider, ai_cfg.get("model"))
    user = {"target": payload.get("target", ""),
            "target_aliases": payload.get("target_aliases", []),
            "candidates": [c["value"] if isinstance(c, dict) else c
                           for c in payload.get("candidates", [])]}
    client = llm_providers.make_client(provider, ai_cfg.get("max_retries", 8))
    try:
        results, usage = await llm_providers.classify(
            client, provider, model, INSTRUCTIONS, user, TOOL, ai_cfg.get("max_tokens", 8000))
    finally:
        await llm_providers.close_client(client)

    out = {}
    for r in results:
        if isinstance(r, dict) and r.get("value"):
            out[r["value"]] = {"matches": bool(r.get("matches")),
                               "canonical": r.get("canonical") or "",
                               "reason": r.get("reason") or ""}
    try:
        with open(cost_log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"pass": "cmatch", "provider": provider, "model": model,
                                "n_in": len(user["candidates"]), "n_out": len(out),
                                "input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"],
                                "cache_read": usage["cache_read"], "cache_creation": usage["cache_creation"]}) + "\n")
    except Exception:
        pass
    return out


def _slug(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "cellline"


def _write_acc_lists(P, keep_rows):
    """MAIN OUTPUT: combined SraAccList.txt + per-study by_study/<GSE>/SraAccList.txt."""
    combined = sorted({(r.get("Run") or "").strip() for r in keep_rows if (r.get("Run") or "").strip()})
    with open(_safe_open(P.sra_acc_list), "w", encoding="utf-8") as f:
        f.write("\n".join(combined) + ("\n" if combined else ""))
    by_study = {}
    for r in keep_rows:
        run_acc = (r.get("Run") or "").strip()
        gse = (r.get("GSE_Series") or "").strip() or "unknown"
        if run_acc:
            by_study.setdefault(gse, set()).add(run_acc)
    for gse, runs in by_study.items():
        d = os.path.join(P.by_study_dir, gse)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "SraAccList.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(runs)) + "\n")
    return combined, by_study


def run(P, sel, ai_cfg=None, skip_ai=False, reporter=NULL):
    """Match cell-line names, filter the run table to the target line, emit SraAccList + filtered CSV."""
    if not os.path.exists(P.match_candidates) or not os.path.exists(P.runtable_all_csv):
        print("  CMATCH: missing inputs (no candidates / run table) -> skipping")
        return None
    payload = json.load(open(P.match_candidates, encoding="utf-8"))
    all_rows = list(csv.DictReader(open(P.runtable_all_csv, encoding="utf-8")))
    cand_values = [c["value"] if isinstance(c, dict) else c for c in payload.get("candidates", [])]
    target = payload.get("target", sel.get("canonical", ""))
    aliases = payload.get("target_aliases", [])
    cl_cols = payload.get("cellline_columns", [])

    reporter.set_total(1)
    if skip_ai or not cand_values:
        match = _deterministic(target, aliases, cand_values)
        mode = "deterministic"
    else:
        try:
            match = asyncio.run(_classify_async(payload, ai_cfg or {"model": "claude-haiku-4-5"}, P.cost_log))
            mode = "ai"
            # any candidate the model didn't return -> deterministic backstop
            for v in cand_values:
                if v not in match:
                    match[v] = _deterministic(target, aliases, [v])[v]
        except Exception as e:
            print(f"  CMATCH: AI match failed ({e}) -> deterministic fallback")
            match = _deterministic(target, aliases, cand_values)
            mode = "deterministic(fallback)"
    reporter.advance(1)

    json.dump({"target": target, "mode": mode, "matches": match},
              open(P.cellline_match, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # hybrid keep: GSM floor (already classified) OR agent-blessed cell-line value
    keep_gsms = {strip_rep(g) for g in sel.get("gsms", [])}
    match_vals = {v for v, info in match.items() if info.get("matches")}

    def is_target(r):
        if strip_rep((r.get("GEO_Accession (exp)") or "")) in keep_gsms:
            return True
        return any((r.get(c) or "").strip() in match_vals for c in cl_cols)

    keep_rows = [r for r in all_rows if is_target(r)]

    # filtered run table (same column rules as the validated reconstruction)
    keys = set()
    for r in keep_rows:
        keys.update(r)
    cols = order_columns(keys, add_gse=True) if keys else order_columns(set(), add_gse=True)
    slug = _slug(target)
    out_csv = _safe_open(P.runtable_filtered_csv(slug))
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore"); w.writeheader()
        for r in keep_rows:
            w.writerow(r)

    combined, by_study = _write_acc_lists(P, keep_rows)

    matched_names = sorted(match_vals)
    print(f"  CMATCH ({mode}): target={target!r} | matched values={matched_names} | "
          f"kept {len(keep_rows)}/{len(all_rows)} runs across {len(by_study)} studies")
    print(f"  MAIN OUTPUT -> {P.sra_acc_list} ({len(combined)} run accessions)")
    reporter.set_detail(f"{len(combined)} runs kept ({mode}); matched: {', '.join(matched_names) or 'GSM-only'}")
    return {"target": target, "slug": slug, "mode": mode,
            "n_runs_all": len(all_rows), "n_runs_kept": len(keep_rows),
            "n_accessions": len(combined), "n_studies": len(by_study),
            "matched_values": matched_names, "filtered_csv": out_csv}


def main():
    import argparse
    from pipeline_paths import Paths
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--provider", default="anthropic", choices=list(llm_providers.PROVIDERS))
    ap.add_argument("--model", default=None)
    ap.add_argument("--skip-ai", action="store_true")
    a = ap.parse_args()
    P = Paths(a.run_dir).ensure_dirs()
    sel = json.load(open(P.cellline_selection, encoding="utf-8"))
    run(P, sel, {"provider": a.provider, "model": a.model}, a.skip_ai)


if __name__ == "__main__":
    main()
