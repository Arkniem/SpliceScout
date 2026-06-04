# SpliceScout: NCBI GEO RNA-seq Pipeline — Complete Handoff

**Read this first in a new chat.** It captures everything needed to run, finish, or rebuild the
pipeline. All source code already exists on disk in `C:\Users\krog5w\.gemini\antigravity\SpliceScout\`
— a new session should `Read` those files for full source; this doc is the map + decisions + status.

---

## 1. What this is

A standalone, parameterized program: **submit an NCBI GEO search query → it produces cleaned,
splicing-amenable, cell-line-grouped compound tables.** It automates a workflow we built by hand:
scrape GEO, pull per-sample structured metadata (cell line / treatment-compound / read counts) +
library-prep protocol from SRA, AI-clean the messy free-text (canonical drug names, recovered cell
lines, drug-treated vs not), filter to splicing-amenable library preps, and emit the tables.

The AI cleaning runs via your choice of **Anthropic (Claude), OpenAI (ChatGPT), or Google Gemini**
(`llm_providers.py` — one `classify()` behind all three; OpenAI + Gemini share the `openai` SDK, with
Gemini pointed at its OpenAI-compatible endpoint). Pick the provider in the UI/CLI; it needs that
provider's API key.

---

## 2. Run it

**Prereqs (one-time):**
```
pip install anthropic openai            # openai SDK also drives Gemini (OpenAI-compat endpoint)
set ANTHROPIC_API_KEY=sk-ant-...        # or OPENAI_API_KEY / GEMINI_API_KEY, per provider; or paste when prompted
# optional: an NCBI E-utilities API key (3 req/s -> 10 req/s)
```

**Web UI (recommended):** double-click **`launch.bat`** — it finds Python (if missing, installs the
**latest** version via winget — it queries `winget search Python.Python` and version-sorts, so it's
never pinned to a specific minor), `pip install -r requirements.txt` (anthropic, openai, openpyxl, paramiko) on first run, then
starts the server + opens the browser. No Node.js anywhere (the UI is pure Python `http.server` + inline
browser JS). Or run `server.py` directly:
```
python server.py                         # serve 127.0.0.1:8765 + auto-open browser
python server.py --port 9000 --no-open   # custom port, no auto-open
```
A single page asks for everything a run needs — GEO query, study scope (ALL vs cap at N),
Anthropic API key (or a "skip AI cleaning" checkbox), optional NCBI key, model, concurrency —
then runs the pipeline in a background thread and shows a **7-stage stepper with a live progress
bar, an ETA, and a streaming log**. When done it lists the output tables for download.
Stdlib-only HTTP server; one run at a time. The Anthropic key is held in memory only (never
written to disk); the NCBI key is stored in `config.json` exactly as the CLI does.

**CLI (`pipeline.py`):**
```
python pipeline.py                       # interactive: prompts for query, cap, keys
python pipeline.py --query "rna-seq[Description] AND human[Organism] AND drug" --cap unlimited --yes
python pipeline.py --cap 25 --skip-ai --yes          # deterministic chain only (no API key)
python pipeline.py --run-dir runs/<existing> --resume   # resume a stopped run
```
Flags: base `--query --cap (int|unlimited) --ncbi-key --provider anthropic|openai|gemini --anthropic-key --openai-key --gemini-key --model --concurrency --run-dir --resume --skip-ai --yes`; deep-dive `--no-deep-dive --pick auto|manual --cell-line NAME --validate-runtable`; cluster `--cluster-mode off|manual|autonomous --cluster-root PATH --ssh-host --ssh-user --ssh-port --ssh-key` (SSH password via `$CLUSTER_SSH_PASSWORD`). Default model `claude-haiku-4-5`; upgrade with `--model claude-sonnet-4-6`.

Output → `runs/<query-slug>_<timestamp>/tables/` (**headline = `ncbi_final_splicing.csv`**) and
`runs/<…>/runtable/` (**deep-dive MAIN output = `SraAccList.txt`** — the SRR list for the best cell
line; + `SraRunTable_<line>.csv`/`.xlsx`, `by_study/<GSE>/SraAccList.txt`).

---

## 3. File inventory (all in SpliceScout\)

| File | Role |
|---|---|
| `server.py` | **Web front end** — stdlib HTTP server; setup form + live progress/ETA dashboard; runs `pipeline.run_pipeline` in a background thread, tees stdout→log, serves output tables |
| `progress.py` | `RunReporter` (thread-safe per-run progress + ETA + log, `snapshot()` for the UI) and `NULL` no-op reporter so stages run standalone unchanged |
| `pipeline.py` | **Orchestrator** — `run_pipeline(cfg, P, reporter)` is the shared stage-DAG (CLI `main()` + the web server both call it); interactive prompts, RunConfig→config.json, resume via pipeline_state.json |
| `fetch_5000_ncbi.py` | Stage 1 — `run(query,max_results,P,ncbi_key)` esearch+esummary GEO → ncbi_raw.json |
| `structured_extract.py` | Stage 2 — `run(P,ncbi_key,cap)` GEO→SRA elink+efetch → per-sample {cell_line,treatments_raw,spots} **+ study_protocol.json in the same pass** |
| `prep_ai.py` | Stage 3 — `build_batches(P)` → compound_batches + sample_batches + sample_index.json |
| `llm_providers.py` | **provider abstraction** — `classify()` for Anthropic (tools) + OpenAI/Gemini (function-calling via the `openai` SDK; Gemini uses its OpenAI-compat endpoint); `make_client`, `MODELS`, `KEY_ENV`, `DEFAULT_MODEL` |
| `ai_clean.py` | Stages 4-5 — `run_pass("compounds"/"samples",P,cfg)` provider-agnostic via `llm_providers`, forced tool/function call, resumable → ai_work/*_results/ |
| `merge_ai.py` | Stage 6 — `merge_compounds(P)`→compound_map.json, `merge_samples(P)`→sample_map.json |
| `build_final.py` | Stage 7 — `build_all(P)` emits all tables + protocol audit + **`cellline_index.json`** (full per-cell-line studies+GSMs, untruncated, for the deep dive) |
| `deepdive_select.py` | Stage 8 — `rank_candidates(P)` / `select(P,canonical)`: pick the best REAL cell line (Sample Type "Cell line") by #compounds→total reads → cellline_selection.json |
| `runtable_common.py` | ported NCBI E-utilities client (validated reconstruction path); `configure(ncbi_key)` |
| `runtable_fetch.py` | Stage 9 — `run(P,sel,ncbi_key)` full SRA XML per selected study → runtable/xml_cache/ (+ ENA fallback) |
| `runtable_build.py` | Stage 10 — `run(P,sel)` byte-exact Run Selector reconstruction → SraRunTable_all.csv + match_candidates.json; `validate()` harness |
| `cellline_match.py` | Stage 11 — AI agent (forced tool) decides which cell-line names ARE the target (A549≈A-549); skip-ai = deterministic norm; hybrid GSM∪value filter → **SraAccList.txt** (MAIN) + filtered CSV |
| `runtable_annotate.py` | Stage 12 — drug/dose/is_control **+ 3-way `drug_treated`** on the filtered table (reuses `normalize_v2`+`compound_map`; dose regex from pipeline B) + `.xlsx` workbook |
| `cluster_deploy.py` | Stages 13-14 — `build_bundle` (fill config.sh + per-study by_study + zip) and `submit_over_ssh` (autonomous upload + launch); reads vendored `cluster_template/` |
| `cluster_template/` | Vendored copy of the user's LSF download pipeline (`SRA_pipeline_template`, 12 files); only `config.sh` is regenerated per run |
| `normalize_v2.py` | shared dose/control normalizer: `clean_compound`, `normalize_compound`, `is_control` |
| `cell_utils.py` | shared `clean_struct_cell`, `extract_cell_line` |
| `pipeline_paths.py` | `Paths(run_dir)` — single source of truth for every file path (incl. the `runtable/` subtree) |

Old per-step scripts (`protocol_scrape.py`, etc.) still exist but are NOT in the DAG. The deep-dive
modules were ported from `C:\Users\krog5w\Downloads\GEO_SRA_Metadata_Pipeline\` (its 01/02/04 +
common.py) — that source folder is the reference; the `--validate-runtable` harness proves the port.

---

## 4. Pipeline DAG (14 stages, each checkpointed in pipeline_state.json)

```
1 fetch          -> ncbi_raw.json, unique_titles.json
2 extract        -> structured_samples.jsonl, structured_done.json, study_protocol.json
3 prep           -> ai_work/{compound_batches,sample_batches}/, sample_index.json
4 ai_compounds   -> ai_work/compound_results/*.json     (skipped with --skip-ai)
5 ai_samples     -> ai_work/sample_results/*.json        (skipped with --skip-ai)
6 merge          -> compound_map.json, sample_map.json
7 build          -> tables/{ncbi_final_splicing,ncbi_final,ncbi_final_truseq}.{csv,md}, ncbi_protocol_audit.csv, cellline_index.json
  --- DEEP DIVE: the single best cell line (skipped with --no-deep-dive) ---
8 select          -> runtable/cellline_selection.json   (auto: top real line by #compounds→reads; or manual pick)
9 runtable_fetch  -> runtable/xml_cache/<GSE>.full.xml  (full SRA XML for the selected line's studies; +ENA fallback)
10 runtable_build -> runtable/SraRunTable_all.csv, match_candidates.json   (byte-exact Run Selector reconstruction)
11 cellline_match -> runtable/SraAccList.txt (MAIN), by_study/<GSE>/SraAccList.txt, SraRunTable_<line>.csv, cellline_match.json
12 runtable_annotate -> drug/dose/is_control + 3-way drug_treated on the filtered CSV + SraRunTable_<line>.xlsx + drug_annotation_review.csv
  --- CLUSTER HANDOFF (skipped unless cluster_mode != off; needs the deep dive) ---
13 cluster_bundle  -> runtable/cluster/ (vendored LSF scripts + filled config.sh + PER-STUDY by_study/<GSE>/) + cluster_bundle.zip
14 cluster_submit  -> (autonomous only) ssh mkdir + scp bundle + run ./run_pipeline.sh on the cluster
```
The two AI passes are independent (no cross-pass ordering). Resumable at stage level
(pipeline_state.json), study level (structured_done.json), and batch level (result files).
Deep-dive stages 8-12 run after build by default; `--no-deep-dive` (or the UI checkbox) skips them,
and they skip gracefully if no real cell line exists.

**The deep dive (stages 8-12) — "best cell line → download-ready run list":**
- **Selection** (`--pick auto|manual`, `--cell-line NAME`): only REAL lines (Sample Type "Cell line",
  so UNRESOLVED/Patient/Organoid buckets are never chosen); auto-ranks by #unique compounds then
  total reads. Manual pauses the run (UI shows the ranked list + POST `/api/select`; CLI prompts).
- **MAIN OUTPUT `SraAccList.txt`** = flat SRR list for the target line's runs, for
  `prefetch --option-file SraAccList.txt` (the user's `SRA_download_scripts` layout).
- **Disambiguation agent** (`cellline_match.py`): the run table for the selected studies holds many
  cell lines written many ways. The agent (tool `emit_cellline_matches`) marks a value as the target
  only if it IS/RESEMBLES it (A549 ≈ A-549 ≈ A 549), excluding e.g. BEAS-2B. Keep rule is a HYBRID
  union: keep a run if its GSM was already classified as the line (deterministic floor from
  `cellline_selection.json`) OR its cell-line value is agent-blessed. `--skip-ai` ⇒ deterministic
  normalized-equality match (lowercase+strip non-alphanumeric) — verified to keep A549 / drop BEAS-2B.
- **Drug annotation** reuses `normalize_v2`+`compound_map` (general), NOT pipeline B's hardcoded VOCAB.

**Three-way drug-treated classification (Drug Treated / Not Drug Treated / Undetermined):**
- A sample that can't be confidently classified is now its OWN **Undetermined** column (previously folded
  into Not Drug Treated). `build_final` already tracked `pending` separately — the cell-line tables
  (`ncbi_final*.csv`, now **14 cols**) + `cellline_index.json` split it out; the AI `emit_samples` enum
  gained `"Undetermined"`; and the deep-dive per-run table gained a 3-way `drug_treated` column
  (`runtable_annotate.drug_treated_label`: real drug → Drug Treated; explicit control / known non-drug
  (is_drug=False) → Not Drug Treated; no/unrecognized treatment → Undetermined).

**Cluster handoff (stages 13-14, `cluster_deploy.py`):**
- Vendored the user's LSF pipeline into `cluster_template/` (12 files). `cluster_mode` ∈
  `off | manual | autonomous` (UI default autonomous). `build_bundle` copies the scripts, writes a
  `config.sh` filled from the form's values (`fill_config` substitutes the EDIT-THESE block), copies the
  deep-dive's **per-study** `by_study/<GSE>/SraAccList.txt` (NEVER the combined list — so the cluster runs
  each study separately), writes `RUN_ON_CLUSTER.txt`, and zips → `cluster_bundle.zip`. `submit_over_ssh`
  (autonomous) uploads via system `ssh`/`scp` (key/agent; paramiko if a password is given) and runs
  `./run_pipeline.sh`; **non-fatal** on failure (bundle stays downloadable). SSH password lives in the
  transient `secrets` arg, never in `config.json`.

**The two AI passes (consolidated from the original 4):**
- **compounds**: raw compound string → `{name (standard generic; abbreviations expanded, salts stripped, trade→generic, synonyms merged, combos sorted), is_drug}`. Tool `emit_compounds`, returns array, pivoted to `{raw:{...}}`.
- **samples**: a full title OR a short cell-line tag value → `{cell_line (canonical line or bucket), category (Cell line|Primary cells|Immune/PBMC|iPSC/ESC|Organoid|Tissue|Patient/Tumor|Single-cell|Unknown), drug_treated (Drug Treated|Not Drug Treated|N/A)}`. Tool `emit_samples`.
Full prompt text is in `ai_clean.py` (COMPOUND_INSTRUCTIONS / SAMPLE_INSTRUCTIONS).

---

## 5. Current status

- ✅ **Multi-provider AI (Anthropic / OpenAI / Gemini)** via `llm_providers.py`. All three clients
  construct; the Anthropic path is unchanged (tools); OpenAI + Gemini use forced function-calling
  through the `openai` SDK (Gemini via its OpenAI-compatible endpoint). The OpenAI/Gemini response
  parsing (tool-call + a JSON-in-content fallback) is unit-verified with a mocked client. The UI
  remembers a separate key per provider. NOT yet run live against real OpenAI/Gemini keys (none
  available) — the Anthropic live behavior is unchanged.
- ✅ **Deterministic chain verified end-to-end** on a fresh 25-study run (`--skip-ai`): fetch → extract (protocol captured in-pass, 22/25 had protocol text) → prep → merge → all 4 tables with correct 13-col schema + working Sample Type + splicing/TruSeq filters. All modules import with `anthropic` installed.
- ✅ **Web front end built + verified** (`server.py` + `progress.py`). Live runs through the UI (cap-3 and cap-15, `skip_ai`) drove the 7-stage stepper, live log, and ETA correctly — at 13% the ETA read ~27s and the cap-15 run finished in 26.2s. Output tables download from the UI. `pipeline.py` was refactored to a shared `run_pipeline(cfg, P, reporter)` so CLI and UI share one DAG; stages take an optional `reporter=NULL` (no behavior change when run standalone/CLI).
- ✅ **3-way "Undetermined" + cluster handoff (stages 13-14) built + verified.** Splicing table now
  reports `treated/not/undet` separately (a cap-25 run that read `not=262` now reads `not=102 undet=160`);
  `cellline_index.json` + the per-run `drug_treated` column carry the third category. The cluster bundle
  was verified end-to-end through the UI (manual mode): `config.sh` filled from the form (`THREADS=8`,
  `JOB_TAG`), `by_study/` holds ONLY per-study folders (no combined list), `cluster_bundle.zip` downloads,
  and `config.json` never stores the SSH password. Autonomous `submit_over_ssh` was verified to fail
  gracefully (non-fatal, bundle intact) against an unreachable host.
- ✅ **Deep dive (stages 8-12) built + verified deterministically.** CLI resume of the cap-25 run auto-selected `SW1783` → fetched its study → reconstructed 30 runs → wrote `SraAccList.txt` (30 SRR) + `by_study/` + filtered CSV + `.xlsx` + drug review. The **`--validate-runtable` harness PASSES** (reconstruction byte-exact vs the official SRP189165 export). **Disambiguation proven**: deep-diving `A549` on GSE267599 (which mixes A549+BEAS-2B) kept exactly the 6 A549 runs and dropped the 6 BEAS-2B. The web UI's **manual-pick pause** was verified live (run paused → `/api/select` → resumed → finished), and `SraAccList.txt`/`.xlsx` download through the server.
- ⏳ **AI stages (4-5) and the AI cell-line matcher (stage 11) NOT yet run live** — they need a real `ANTHROPIC_API_KEY` (none was available in any build session). All AI code is structurally validated (imports, tool schemas, batch I/O, caching, concurrency, per-batch progress hooks) and the deterministic fallbacks are verified, but the actual Claude calls have not executed. The UI collects the key at runtime — paste it in the form (or tick "skip AI cleaning").

### What's left to do (the next chat's job)
1. Set `ANTHROPIC_API_KEY` and run a **small live test**: `python pipeline.py --cap 25 --yes`. Verify `ai_work/compound_results/*.json` and `sample_results/*.json` are `{raw:{...}}` maps, `cost_log.jsonl` shows token usage, and the final splicing table has canonical compound names + recovered cell lines + Sample Type. Confirm splicing table has fewer samples than `ncbi_final.csv` while Smart-seq rows survive.
2. If quality is good, run the real job: `python pipeline.py --query "..." --cap unlimited --yes` (large queries = long scrape + real API cost; Haiku is the cheap default).
3. Optional polish: prompt-cache only fires if the cached system prefix ≥4096 tokens on Haiku (ours is ~700) — so caching may be a no-op (cheap anyway); confirm via cost_log `cache_read`. Consider `--model claude-sonnet-4-6` for the compound/synonym pass if drug-name quality needs it.

---

## 6. Critical design decisions / gotchas (don't break these)

- **GEO→SRA via `elink(dbfrom=gds,db=sra,id=<gds_uid>)`** is the reliable join (bare-accession esearch silently returns 0 for ~92% of studies). esearch is only a fallback.
- **`merge_ai` batch counts are glob-based, never hardcoded** (the original had hardcoded 14/222 that silently dropped data on any other query size).
- **`build_final` writes into the run's `tables/` dir** (the original wrote to a fixed `brain\f4916276…` dir).
- **Single normalizer**: `structured_extract`, `prep_ai`, `build_final` all import `normalize_v2` (don't reintroduce a private copy).
- **Splicing filter** (`build_final.is_splicing_amenable`): KEEP TruSeq/NEBNext/KAPA/total-RNA **and Smart-seq** (full-length); REMOVE 3'-end methods — single-cell/nuclei, plate-seq, 10x/droplet, sci-Plex, and **bulk 3'-tag (QuantSeq/DRUG-seq/3'-DGE/BRB-seq/Tag-Seq)** — detected via protocol text + title regex + the AI Single-cell category. Lexogen *QuantSeq* = remove, but Lexogen *Ribocop/total-RNA* = keep (don't match bare "lexogen").
- **High Count = >40,000,000 reads/sample** (spots). Per cell line we report Max + Total spots.
- **Drug-treated logic (hybrid):** if a sample has a structured `treatment` field → treated iff ≥1 real (non-control, is_drug) compound; else use the AI title classification.
- **Windows file locks:** `build_final` writes to `*_v2.csv` if the target is open in Excel (don't remove that fallback). AI result files are written temp-then-`os.replace`.
- **`_ask` catches EOFError** (the agent shell reports a TTY but has no stdin) — keep that.

---

## 7. Data-cleaning rules preserved (the "why" behind the cleaning)

- **No keyword-guessing of drug names from titles** (that produced junk). Compounds come from the depositor's structured `treatment`/`agent`/`compound` SAMPLE_ATTRIBUTES; cell lines from the `cell line` tag; reads from `spots`.
- **Dose/control normalization** (`normalize_v2`): strips leading/trailing/underscore/paren dose tokens, durations, replicate/condition tags; vehicle/control detection (DMSO/PBS/untreated + residue forms); a complex-value guard so combinations aren't mangled.
- **AI canonicalization** then standardizes names globally (5-FU→fluorouracil, salts, trade→generic, Rifampin=Rifampicin), flags non-drugs (siRNA/shRNA/CRISPR/plasmid/controls) as `is_drug=false`, recovers/merges cell-line names, buckets non-lines into Sample Types, and classifies the no-treatment-field samples as Drug Treated / Not.

---

## 8. To continue in a new chat, say:
"Read `C:\Users\krog5w\.gemini\antigravity\SpliceScout\PIPELINE_HANDOFF.md` and the pipeline source in
that folder, then [run a live `--cap 25` test with my ANTHROPIC_API_KEY / make change X]."

Design reference also at `C:\Users\krog5w\.claude\plans\take-everything-we-have-groovy-salamander.md`.
