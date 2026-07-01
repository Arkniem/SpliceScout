#!/usr/bin/env python
# =============================================================================
# diagnose.py -- SpliceScout CPU diagnostic LLM (llama-cpp-python, CPU-only), REASONING mode ON.
# Default model: Google Gemma 4 E4B (it-qat GGUF) with its EXPLICIT thinking channel enabled. Reads a
# stall-context file, lets the model REASON (<|channel>thought ...), then emits ONE WHITELISTED remediation
# as a single JSON object. NO network, NO proxy -- the self-contained fallback for when BOTH the
# deterministic watchdogs AND the regular (proxy) AI cannot resolve a stalled stage.
# Usage: diagnose.py <context_file> <model.gguf> [n_threads]
# Prints: {"cause","action","args","confidence","explanation"}  (action in WHITELIST)
# =============================================================================
import sys
import os
import json
import re

WHITELIST = ["rearm", "quarantine_bed", "bump_walltime", "bump_mem", "none"]

SYSTEM = (
    "You are the diagnostic assistant for SpliceScout, a self-driving GEO RNA-seq splicing pipeline on an LSF "
    "cluster (stages: download -> STAR align -> BAM->BED -> AltAnalyze PSI -> drug concordance). Deterministic "
    "watchdogs already auto-handle KNOWN failures (concurrent-writer BED corruption, RUN-but-frozen deadlocks, "
    "false-drops, walltime/memory kills, corrupt intron BEDs). A stage has STALLED anyway. Reason about the "
    "most likely cause, then choose EXACTLY ONE remediation from this whitelist:\n"
    "  rearm           : clear the STALL and let the (already-hardened) watchdog retry the stage. The safe "
    "default when the cause looks transient (a flaky bjobs snapshot, a one-off node/NFS hiccup) or already-fixed.\n"
    "  quarantine_bed  : a SPECIFIC junction/intron BED is corrupt/truncated and is wedging AltAnalyze. Put "
    "ONLY the BARE sample id from the logs in args (e.g. SAMN12345678) -- no suffix, no path, no extension.\n"
    "  bump_walltime   : the job is being killed by the walltime limit (TERM_RUNLIMIT).\n"
    "  bump_mem        : the job is being killed for memory (TERM_MEMLIMIT / OOM-killer).\n"
    "  none            : no safe automatic action; a human must inspect. Use this whenever you are uncertain.\n"
    "After your reasoning, output ONLY a JSON object as the LAST thing you write (a ```json fence is fine): "
    '{"cause": <short string>, "action": <one whitelist value>, "args": <string, may be empty>, '
    '"confidence": <number 0.0-1.0>, "explanation": <one sentence>}.'
)

# Sample-accession shapes the BED files are keyed by (normalize a quarantine arg the model may decorate,
# e.g. "SAMN53796752_intronBEDFile" -> "SAMN53796752").
_ACCN = re.compile(r"(SAMN|SAMEA|SAMD|SRR|ERR|DRR|SRX|SRS|GSM)[0-9]+")


def _extract_json(txt):
    """Return the LAST balanced {...} object in txt that parses to a dict. The model REASONS first (in a
    <|channel>thought ... block) and emits the JSON answer LAST, so scanning for the final valid object is
    robust to any thinking prose/delimiters before it."""
    objs, depth, start = [], 0, -1
    for i, c in enumerate(txt or ""):
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                objs.append(txt[start:i + 1])
    for cand in reversed(objs):
        try:
            o = json.loads(cand)
            if isinstance(o, dict):
                return o
        except Exception:
            continue
    return None


def _generate(llm, messages):
    """Run the model with EXPLICIT reasoning on. Primary path: render the model's chat template with
    enable_thinking=True (injects the <|think|> channel for Gemma 4) and raw-complete, tokenizing with
    add_bos=False so we don't double the BOS the template already emits. Falls back to the plain chat API
    (which still reasons inline) if the template/formatter path is unavailable. NO json-grammar -- it would
    suppress the thinking; we parse the JSON the model emits after reasoning."""
    try:
        from llama_cpp.llama_chat_format import Jinja2ChatFormatter
        tmpl = dict(llm.metadata).get("tokenizer.chat_template", "") or ""
        if not tmpl:
            raise RuntimeError("no chat_template in model metadata")
        fmt = Jinja2ChatFormatter(template=tmpl, eos_token="<end_of_turn>", bos_token="<bos>",
                                  add_generation_prompt=True)
        prompt = fmt(messages=messages, enable_thinking=True).prompt
        toks = llm.tokenize(prompt.encode("utf-8"), add_bos=False, special=True)
        out = llm.create_completion(prompt=toks, max_tokens=2048, temperature=0.7, top_p=0.95, top_k=64,
                                    stop=["<turn|>", "<end_of_turn>", "<eos>"])
        return out["choices"][0]["text"]
    except Exception:
        out = llm.create_chat_completion(messages=messages, max_tokens=2048,
                                         temperature=0.7, top_p=0.95, top_k=64)
        return out["choices"][0]["message"]["content"]


def main():
    if len(sys.argv) < 3:
        print(json.dumps({"cause": "bad-args", "action": "none", "args": "",
                          "confidence": 0.0, "explanation": "usage: diagnose.py <ctx> <model> [threads]"}))
        return
    ctx_file, model = sys.argv[1], sys.argv[2]
    nthreads = int(sys.argv[3]) if len(sys.argv) > 3 else max(2, (os.cpu_count() or 4))
    try:
        context = open(ctx_file, encoding="utf-8", errors="replace").read()[:12000]
    except Exception as e:
        context = "(could not read context: %s)" % e

    from llama_cpp import Llama
    # n_ctx 8192 is plenty for the ~3k-token context + reasoning + the JSON (Gemma 4 trains at 131072).
    llm = Llama(model_path=model, n_ctx=8192, n_threads=nthreads, verbose=False)

    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": "STALL CONTEXT:\n" + context +
                 "\n\nReason about the cause, then output the JSON remediation LAST."}]
    try:
        txt = _generate(llm, messages)
    except Exception as e:
        print(json.dumps({"cause": "llm-failed", "action": "none", "args": "",
                          "confidence": 0.0, "explanation": str(e)[:200]}))
        return

    obj = _extract_json(txt)
    if not obj:
        obj = {"cause": "parse-failed", "action": "none", "args": "",
               "confidence": 0.0, "explanation": (txt or "")[:200]}
    if obj.get("action") not in WHITELIST:
        obj["action"] = "none"
    if obj.get("action") == "quarantine_bed":
        m = _ACCN.search(str(obj.get("args", "")))
        if m:
            obj["args"] = m.group(0)            # normalize to the bare accession the BED files are keyed by
    obj.setdefault("args", "")
    obj.setdefault("confidence", 0.0)
    obj.setdefault("cause", "")
    obj.setdefault("explanation", "")
    print(json.dumps(obj))


if __name__ == "__main__":
    main()
