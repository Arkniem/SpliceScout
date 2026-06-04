"""
NCBI GEO RNA-seq pipeline — submit a query, get cleaned splicing-amenable tables.

End to end: fetch GEO -> extract structured SRA metadata + library protocol -> prep AI batches
-> AI clean (compounds + samples via the Anthropic API) -> merge -> build all tables.

Interactive:   python pipeline.py
Unattended:    python pipeline.py --query "..." --cap 25 --yes
Resume a run:  python pipeline.py --run-dir runs/<existing> --resume
Skip AI (test):python pipeline.py --cap 25 --skip-ai --yes
"""
import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime

from pipeline_paths import Paths
from progress import NULL
import llm_providers
import fetch_5000_ncbi
import structured_extract
import prep_ai
import merge_ai
import build_final

DEFAULT_QUERY = "rna-seq[Description] AND human[Organism] AND drug"
STAGES = ["fetch", "extract", "prep", "ai_compounds", "ai_samples", "merge", "build"]


@dataclass
class RunConfig:
    query: str
    cap: object               # int or "unlimited"
    ncbi_key: object          # str or None
    model: str
    concurrency: int
    run_dir: str
    skip_ai: bool = False
    provider: str = "anthropic"   # AI provider for cleaning: anthropic | openai | gemini
    deep_dive: bool = True     # after build, deep-dive the best cell line into a Run Selector table
    pick_mode: str = "auto"    # "auto" (top real line) or "manual" (UI/CLI picks from the ranked list)
    cluster_mode: str = "off"  # "off" | "manual" (build bundle) | "autonomous" (build + upload/launch)
    cluster_cfg: dict = None   # config.sh values + ssh host/port/user/key (NO password — that's a secret)


def _ask(prompt, default=None, secret=False):
    if not sys.stdin.isatty():
        return default
    suffix = f" [{default}]" if default not in (None, "") else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return val or default


def _load_state(P):
    if os.path.exists(P.state):
        try:
            return json.load(open(P.state, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(P, state):
    json.dump(state, open(P.state, "w", encoding="utf-8"), indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query")
    ap.add_argument("--cap", help="integer or 'unlimited'")
    ap.add_argument("--ncbi-key", default=None)
    ap.add_argument("--provider", choices=list(llm_providers.PROVIDERS), default="anthropic",
                    help="AI provider for cleaning: anthropic | openai | gemini")
    ap.add_argument("--anthropic-key", default=None)
    ap.add_argument("--openai-key", default=None)
    ap.add_argument("--gemini-key", default=None)
    ap.add_argument("--model", default=None, help="provider-specific model (else the provider default)")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--run-dir", default=None)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--skip-ai", action="store_true", help="run deterministic stages only")
    ap.add_argument("--no-deep-dive", action="store_true", help="skip the cell-line metadata deep dive")
    ap.add_argument("--pick", choices=["auto", "manual"], default="auto",
                    help="deep-dive cell-line pick: auto (top real line) or manual (prompt)")
    ap.add_argument("--cell-line", default=None, help="deep-dive a specific cell line by name")
    ap.add_argument("--validate-runtable", action="store_true",
                    help="prove the Run Selector reconstruction matches the official export, then exit")
    ap.add_argument("--cluster-mode", choices=["off", "manual", "autonomous"], default="off",
                    help="cluster handoff: off | manual (build bundle) | autonomous (build + upload/launch)")
    ap.add_argument("--cluster-root", default=None, help="PIPELINE_ROOT path on the cluster")
    ap.add_argument("--ssh-host", default=None)
    ap.add_argument("--ssh-user", default=None)
    ap.add_argument("--ssh-port", default="22")
    ap.add_argument("--ssh-key", default=None, help="private key file (else SSH agent / default key)")
    ap.add_argument("--yes", action="store_true", help="don't prompt for confirmations")
    a = ap.parse_args()

    if a.validate_runtable:
        import runtable_build
        runtable_build.validate()
        return

    # ----- resume an existing run -----
    if a.run_dir and a.resume:
        P = Paths(a.run_dir).ensure_dirs()
        cfg = RunConfig(**json.load(open(P.config, encoding="utf-8")))
        if a.skip_ai:
            cfg.skip_ai = True
        if a.provider != "anthropic":
            cfg.provider = llm_providers.normalize_provider(a.provider)
        if a.model:
            cfg.model = a.model
        if a.no_deep_dive:
            cfg.deep_dive = False
        if a.pick != "auto" or a.cell_line:
            cfg.pick_mode = "manual"
        if a.cluster_mode != "off":
            cfg.cluster_mode = a.cluster_mode
        cc = _cluster_cfg_from_args(a)
        if cc:
            cfg.cluster_cfg = {**(cfg.cluster_cfg or {}), **cc}
        print(f"Resuming run {P.run_dir}")
    else:
        # ----- gather config (args, then interactive prompts) -----
        query = a.query or _ask("Search query (NCBI GEO)", DEFAULT_QUERY)
        cap_raw = a.cap or _ask("Study cap (number or 'unlimited')", "unlimited")
        cap = "unlimited" if str(cap_raw).lower().startswith("u") else int(cap_raw)
        ncbi_key = a.ncbi_key or _ask("NCBI E-utilities API key (blank = keyless)", "") or None
        provider = llm_providers.normalize_provider(a.provider)
        model = a.model or _ask(f"{provider} model", llm_providers.DEFAULT_MODEL[provider])
        slug = re.sub(r"[^a-z0-9]+", "-", query.lower())[:40].strip("-") or "run"
        run_dir = a.run_dir or os.path.join("runs", f"{slug}_{datetime.now():%Y%m%d-%H%M%S}")
        P = Paths(run_dir).ensure_dirs()
        pick_mode = "manual" if (a.pick == "manual" or a.cell_line) else "auto"
        cfg = RunConfig(query=query, cap=cap, ncbi_key=ncbi_key, model=model, provider=provider,
                        concurrency=a.concurrency, run_dir=P.run_dir, skip_ai=a.skip_ai,
                        deep_dive=not a.no_deep_dive, pick_mode=pick_mode,
                        cluster_mode=a.cluster_mode, cluster_cfg=_cluster_cfg_from_args(a))
        json.dump(asdict(cfg), open(P.config, "w", encoding="utf-8"), indent=2)

    # provider API key into env (CLI: prompt if missing)
    for flag, env in (("anthropic_key", "ANTHROPIC_API_KEY"), ("openai_key", "OPENAI_API_KEY"),
                      ("gemini_key", "GEMINI_API_KEY")):
        if getattr(a, flag):
            os.environ[env] = getattr(a, flag)
    if not cfg.skip_ai and not llm_providers.have_key(cfg.provider):
        envname = llm_providers.KEY_ENV[cfg.provider]
        k = _ask(f"{envname} (required for AI cleaning with {cfg.provider}; blank to skip AI)", "")
        if k:
            os.environ[envname] = k
        else:
            cfg.skip_ai = True
            print(f"** No {cfg.provider} key -> running deterministic stages only (--skip-ai). **")

    select_fn = None
    if cfg.deep_dive and cfg.pick_mode == "manual":
        if a.cell_line:
            select_fn = lambda ranked, _n=a.cell_line: _n
        else:
            select_fn = _console_pick

    secrets = {}
    if os.environ.get("CLUSTER_SSH_PASSWORD"):
        secrets["ssh_password"] = os.environ["CLUSTER_SSH_PASSWORD"]

    run_pipeline(cfg, P, select_fn=select_fn, secrets=secrets)


def _cluster_cfg_from_args(a):
    """Collect cluster/ssh settings from CLI args into a cluster_cfg dict (or None)."""
    c = {}
    if a.cluster_root:
        c["PIPELINE_ROOT"] = a.cluster_root
    if a.ssh_host:
        c["ssh_host"] = a.ssh_host
    if a.ssh_user:
        c["ssh_user"] = a.ssh_user
    if a.ssh_port and str(a.ssh_port) != "22":
        c["ssh_port"] = a.ssh_port
    if a.ssh_key:
        c["ssh_key"] = a.ssh_key
    return c or None


def _console_pick(ranked):
    """CLI manual pick: print the ranked real cell lines and read a choice (TTY only)."""
    if not sys.stdin.isatty() or not ranked:
        return None
    print("\nReal cell lines (ranked by # unique compounds, then total reads):")
    for i, r in enumerate(ranked[:20], 1):
        print(f"  {i:>2}. {r['canonical']:<18} {r['n_compounds']} compounds | "
              f"{r['total_spots']:,} reads | {r['n_studies']} studies")
    try:
        ans = input("Pick a number or name (Enter = #1): ").strip()
    except EOFError:
        return None
    if not ans:
        return ranked[0]["canonical"]
    if ans.isdigit() and 1 <= int(ans) <= len(ranked):
        return ranked[int(ans) - 1]["canonical"]
    for r in ranked:
        if r["canonical"].lower() == ans.lower():
            return r["canonical"]
    return ranked[0]["canonical"]


def _list_outputs(P):
    """Enumerate the tables/ + runtable/ outputs for the UI (name, dir, size, flags)."""
    out = []

    def add(dirpath, dirkey, headline_name=None, featured_name=None):
        if os.path.isdir(dirpath):
            for name in sorted(os.listdir(dirpath)):
                fp = os.path.join(dirpath, name)
                if os.path.isfile(fp):
                    out.append({"name": name, "size": os.path.getsize(fp), "dir": dirkey,
                                "headline": name == headline_name,
                                "featured": name == featured_name})
    add(P.tables_dir, "tables", headline_name="ncbi_final_splicing.csv")
    add(P.runtable_dir, "runtable", featured_name="SraAccList.txt")
    for o in out:                       # the cluster bundle is also a featured deliverable
        if o["name"] == "cluster_bundle.zip":
            o["featured"] = True
    return out


def _deep_summary(P):
    """Compact deep-dive summary for the UI (robust to resume — read from files)."""
    d = {}
    if os.path.exists(P.cellline_selection):
        try:
            sel = json.load(open(P.cellline_selection, encoding="utf-8"))
            d.update({"canonical": sel.get("canonical"), "n_studies": len(sel.get("studies", [])),
                      "n_compounds": len(sel.get("compounds", [])), "total_spots": sel.get("total_spots", 0)})
        except Exception:
            pass
    if os.path.exists(P.cellline_match):
        try:
            m = json.load(open(P.cellline_match, encoding="utf-8"))
            d["match_mode"] = m.get("mode")
            d["matched_values"] = sorted(v for v, i in m.get("matches", {}).items() if i.get("matches"))
        except Exception:
            pass
    if os.path.exists(P.sra_acc_list):
        try:
            d["n_accessions"] = sum(1 for line in open(P.sra_acc_list, encoding="utf-8") if line.strip())
        except Exception:
            pass
    return d or None


def _cluster_summary(P):
    """Compact cluster-handoff summary for the UI (read from files; robust to resume)."""
    if not os.path.exists(P.cluster_bundle_zip):
        return None
    out = {"bundle": True, "zip": os.path.basename(P.cluster_bundle_zip)}
    cfgsh = os.path.join(P.cluster_dir, "config.sh")
    if os.path.exists(cfgsh):
        m = re.search(r'(?m)^PIPELINE_ROOT="?(.*?)"?\s*$', open(cfgsh, encoding="utf-8").read())
        if m:
            out["pipeline_root"] = m.group(1)
    bydir = os.path.join(P.cluster_dir, "by_study")
    if os.path.isdir(bydir):
        out["n_studies"] = sum(1 for g in os.listdir(bydir) if os.path.isdir(os.path.join(bydir, g)))
    return out


def run_pipeline(cfg, P, reporter=NULL, select_fn=None, secrets=None):
    """Execute the full pipeline (7 base + 5 deep-dive + 2 cluster stages) for a run dir.

    Single source of truth shared by the CLI (`main`) and the web server (`server.py`).
    Assumes the Anthropic key is already in the environment (or cfg.skip_ai is set).
    `reporter` is a progress.RunReporter (or the NULL no-op) driving the front-end
    stepper / ETA / live log. `select_fn(ranked)->canonical|None` lets a caller supply the
    manual cell-line pick (CLI prompt); the server uses reporter.await_selection instead.
    `secrets` carries transient, never-persisted values (e.g. {"ssh_password": ...}).
    Resumable via pipeline_state.json exactly as before.
    """
    secrets = secrets or {}
    state = _load_state(P)
    cap_int = None if cfg.cap == "unlimited" else int(cfg.cap)
    fetch_max = 100000 if cfg.cap == "unlimited" else int(cfg.cap)
    ai_cfg = {"provider": cfg.provider, "model": cfg.model, "concurrency": cfg.concurrency,
              "max_retries": 8, "max_tokens": 16000}

    reporter.set_meta(query=cfg.query, cap=cfg.cap, model=cfg.model, provider=cfg.provider,
                      skip_ai=cfg.skip_ai, run_dir=P.run_dir)
    if cfg.skip_ai:
        reporter.skip_stage("ai_compounds")
        reporter.skip_stage("ai_samples")

    def done(stage):
        return state.get(stage) == "done"

    def mark(stage):
        state[stage] = "done"
        _save_state(P, state)

    def begin(key):
        """Begin a stage; return True if it still needs to run (not already done)."""
        reporter.begin_stage(key)
        return not done(key)

    print(f"\n=== PIPELINE run_dir={P.run_dir} ===")
    print(f"query={cfg.query!r} cap={cfg.cap} model={cfg.model} skip_ai={cfg.skip_ai}\n")

    # 1 FETCH
    if begin("fetch"):
        fetch_5000_ncbi.run(cfg.query, fetch_max, P, cfg.ncbi_key, reporter=reporter)
        mark("fetch")
    reporter.complete_stage("fetch")

    # 2 EXTRACT + PROTOCOL
    if begin("extract"):
        structured_extract.run(P, cfg.ncbi_key, cap_int, reporter=reporter)
        mark("extract")
    reporter.complete_stage("extract")

    # 3 PREP AI BATCHES
    if begin("prep"):
        prep_ai.build_batches(P, reporter=reporter)
        mark("prep")
    reporter.complete_stage("prep")

    # 4/5 AI PASSES
    if cfg.skip_ai:
        print("[AI] skipped")
    else:
        if begin("ai_compounds"):
            import ai_clean
            ai_clean.run_pass("compounds", P, ai_cfg, reporter=reporter)
            mark("ai_compounds")
        reporter.complete_stage("ai_compounds")
        if begin("ai_samples"):
            import ai_clean
            ai_clean.run_pass("samples", P, ai_cfg, reporter=reporter)
            mark("ai_samples")
        reporter.complete_stage("ai_samples")

    # 6 MERGE
    if begin("merge"):
        merge_ai.merge_compounds(P)
        merge_ai.merge_samples(P)
        mark("merge")
    reporter.complete_stage("merge")

    # 7 BUILD (always re-runs; idempotent — rewrites the tables + cellline_index.json)
    reporter.begin_stage("build")
    summary = build_final.build_all(P)
    mark("build")
    reporter.complete_stage("build")

    print(f"\n=== Tables in {P.tables_dir} ===  Headline: ncbi_final_splicing.csv")

    # 8-14 DEEP DIVE (+ cluster handoff): best cell line -> Run Selector metadata + SraAccList
    _DEEP = ("runtable_fetch", "runtable_build", "cellline_match", "runtable_annotate")
    _CLUSTER = ("cluster_bundle", "cluster_submit")
    if not cfg.deep_dive:
        for k in ("select",) + _DEEP + _CLUSTER:
            reporter.skip_stage(k)
    else:
        import deepdive_select
        deep = None
        if begin("select"):
            ranked = deepdive_select.rank_candidates(P)
            if not ranked:
                print("[deep-dive] no real cell line in the splicing table -> skipping deep dive")
            else:
                chosen = None
                if cfg.pick_mode == "manual":
                    picker = select_fn or reporter.await_selection
                    chosen = picker(ranked)
                deep = deepdive_select.run(P, reporter, chosen=chosen)
            mark("select")
        else:
            # resume: reuse the prior selection if present, else re-select from the current index
            if os.path.exists(P.cellline_selection):
                deep = json.load(open(P.cellline_selection, encoding="utf-8"))
            else:
                deep = deepdive_select.run(P, reporter)
        reporter.complete_stage("select")

        if deep is None:
            for k in _DEEP + _CLUSTER:
                reporter.skip_stage(k)
        else:
            if begin("runtable_fetch"):
                import runtable_fetch
                runtable_fetch.run(P, deep, cfg.ncbi_key, reporter=reporter)
                mark("runtable_fetch")
            reporter.complete_stage("runtable_fetch")

            if begin("runtable_build"):
                import runtable_build
                runtable_build.run(P, deep, reporter=reporter)
                mark("runtable_build")
            reporter.complete_stage("runtable_build")

            if begin("cellline_match"):
                import cellline_match
                cellline_match.run(P, deep, ai_cfg, cfg.skip_ai, reporter=reporter)
                mark("cellline_match")
            reporter.complete_stage("cellline_match")

            if begin("runtable_annotate"):
                import runtable_annotate
                runtable_annotate.run(P, deep, reporter=reporter)
                mark("runtable_annotate")
            reporter.complete_stage("runtable_annotate")

            # 13-14 CLUSTER HANDOFF (gated on cluster_mode)
            if cfg.cluster_mode == "off":
                for k in _CLUSTER:
                    reporter.skip_stage(k)
            else:
                import cluster_deploy
                if begin("cluster_bundle"):
                    cluster_deploy.build_bundle(P, deep, cfg.cluster_cfg, reporter=reporter)
                    mark("cluster_bundle")
                reporter.complete_stage("cluster_bundle")

                if cfg.cluster_mode == "autonomous":
                    if begin("cluster_submit"):
                        cluster_deploy.submit_over_ssh(P, cfg.cluster_cfg, secrets, reporter=reporter)
                        mark("cluster_submit")
                    reporter.complete_stage("cluster_submit")
                else:
                    reporter.skip_stage("cluster_submit")

    deep_dive = _deep_summary(P)
    if deep_dive:
        summary = dict(summary or {}, deep_dive=deep_dive)
        print(f"\n=== DEEP DIVE: {deep_dive.get('canonical')} -> {P.sra_acc_list} "
              f"({deep_dive.get('n_accessions', 0)} run accessions) ===")
    cluster = _cluster_summary(P)
    if cluster:
        summary = dict(summary or {}, cluster=cluster)
        print(f"=== CLUSTER BUNDLE -> {P.cluster_bundle_zip} (root {cluster.get('pipeline_root')}) ===")

    print(f"\n=== DONE. run_dir={P.run_dir} ===")
    files = _list_outputs(P)
    reporter.finish(result=summary, files=files)
    return {"summary": summary, "files": files, "run_dir": P.run_dir}


if __name__ == "__main__":
    main()
