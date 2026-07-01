#!/usr/bin/env bash
# =============================================================================
# diagnose_job.sh -- SpliceScout CPU diagnostic-AI job (LSF, CPU-only). bsub'd by a
# stage watchdog when it finalizes STALLED. It gathers the stall's context, asks the
# LOCAL CPU LLM (diagnose.py) for ONE whitelisted remediation, EMAILS the diagnosis,
# and -- only when autofix is on, the model is confident, the action is whitelisted,
# and the re-arm budget isn't spent -- APPLIES a SAFE, reversible fix (quarantine a
# named corrupt bed and/or re-arm the watchdog). It NEVER runs arbitrary commands.
# Args: <JOB_TAG> <PIPELINE_ROOT> <SCRIPTS_DIR> <LOG_DIR> <AUTOFIX 0|1> <ALERT_EMAIL>
# =============================================================================
set -u
JOB_TAG="${1:?}"; ROOT="${2:?}"; SCRIPTS="${3:?}"; LOGD="${4:?}"; AUTOFIX="${5:-0}"; EMAIL="${6:-}"
# Optional model overrides may arrive as ARGS (so they reach this bsub'd job without relying on LSF env
# propagation) OR from the ENVIRONMENT (config.sh). An arg, when given, wins.
MODEL_PATH_ARG="${7:-}"; MODEL_DIR_ARG="${8:-}"
[ -n "$MODEL_PATH_ARG" ] && DIAGNOSE_MODEL_PATH="$MODEL_PATH_ARG"
[ -n "$MODEL_DIR_ARG" ]  && DIAGNOSE_MODEL_DIR="$MODEL_DIR_ARG"
AIH="${DIAGNOSE_AI_HOME:-/data/salomonis-archive/LabFiles/SpliceScout_AI}"
ENV="$AIH/env"; PY="$ENV/bin/python"
DDIR="$ROOT/.diagnose"; mkdir -p "$DDIR" 2>/dev/null
CTX="$DDIR/context.txt"; OUT="$DDIR/diagnosis.json"; RB="$DDIR/.rearms"
ts(){ date '+%Y-%m-%d %H:%M:%S'; }
log(){ echo "[$(ts)] [diagnose] $*" >> "$ROOT/EVENTS.log" 2>/dev/null; echo "[diagnose] $*" >&2; }

# ---- resolve the GGUF model ------------------------------------------------------------------------
# Priority: an explicit DIAGNOSE_MODEL_PATH (a specific .gguf) -> a model already cached IN THIS PIPELINE
# DIR (DIAGNOSE_MODEL_DIR, default <root>/.splicescout_ai/models) -> the shared install's models dir. If a
# model is found ONLY in the shared install, COPY ("upload") it into the pipeline-dir cache so every FUTURE
# run pointed at this directory reuses it locally (and it survives the shared install being cleaned).
MDIR="${DIAGNOSE_MODEL_DIR:-}"; [ -n "$MDIR" ] || MDIR="$ROOT/.splicescout_ai/models"
MODEL=""
if [ -n "${DIAGNOSE_MODEL_PATH:-}" ] && [ -s "${DIAGNOSE_MODEL_PATH:-}" ]; then
  MODEL="$DIAGNOSE_MODEL_PATH"
fi
[ -n "$MODEL" ] || MODEL="$(ls -1 "$MDIR/"*.gguf 2>/dev/null | head -1)"
if [ -z "$MODEL" ]; then
  SRC="$(ls -1 "$AIH/models/"*.gguf 2>/dev/null | head -1)"
  if [ -n "$SRC" ]; then
    if mkdir -p "$MDIR" 2>/dev/null && [ -w "$MDIR" ]; then
      DEST="$MDIR/$(basename "$SRC")"; TMP="$MDIR/.$(basename "$SRC").$$.part"
      if cp -f "$SRC" "$TMP" 2>/dev/null && mv -f "$TMP" "$DEST" 2>/dev/null; then
        MODEL="$DEST"; log "uploaded model into pipeline cache ($MDIR) -> future runs here reuse it"
      else
        rm -f "$TMP" 2>/dev/null; MODEL="$SRC"; log "could not cache model in $MDIR -> using shared install copy"
      fi
    else
      MODEL="$SRC"; log "pipeline cache $MDIR not writable -> using shared install copy"
    fi
  fi
fi

[ -n "$MODEL" ] && [ -x "$PY" ] || { log "CPU AI model/env not found (checked DIAGNOSE_MODEL_PATH, $MDIR, $AIH/models; env at $AIH/env) -> skipping diagnosis"; exit 0; }

# 1) gather a compact context for the model (caps keep the prompt small)
{
  echo "STAGE: $JOB_TAG    ROOT: $ROOT    TIME: $(ts)"
  echo "=== PIPELINE_STALLED.txt ==="; head -40 "$ROOT/PIPELINE_STALLED.txt" 2>/dev/null
  echo "=== EVENTS.log (tail) ==="; tail -60 "$ROOT/EVENTS.log" 2>/dev/null
  echo "=== watchdog.log (tail) ==="; tail -40 "$ROOT/watchdog.log" 2>/dev/null
  echo "=== recent job .err tails ==="
  for f in $(ls -1t "$LOGD"/*.err 2>/dev/null | head -3); do echo "--- $(basename "$f") ---"; tail -25 "$f" 2>/dev/null; done
  echo "=== recent job .out tails ==="
  for f in $(ls -1t "$LOGD"/*.out 2>/dev/null | head -2); do echo "--- $(basename "$f") ---"; tail -20 "$f" 2>/dev/null; done
  echo "=== live ${JOB_TAG}_* jobs ==="; bjobs -noheader -o 'jobid stat job_name run_time' -J "${JOB_TAG}_*" 2>/dev/null | head -20
} > "$CTX" 2>/dev/null

# 2) run the local CPU LLM
log "running CPU LLM diagnosis with $(basename "$MODEL")"
source /etc/profile.d/modules.sh 2>/dev/null || true; module load anaconda3 2>/dev/null || true
# thread count = the LSF slot allocation (so we don't oversubscribe the node's full core count on a -n8 job)
NTH="${LSB_DJOB_NUMPROC:-$(nproc 2>/dev/null || echo 4)}"
"$PY" "$AIH/scripts/diagnose.py" "$CTX" "$MODEL" "$NTH" > "$OUT" 2>"$DDIR/diagnose.pyerr" \
  || { log "LLM call FAILED (see $DDIR/diagnose.pyerr)"; exit 0; }

_get(){ "$PY" -c "import json;print(json.load(open('$OUT')).get('$1',''))" 2>/dev/null; }
CAUSE="$(_get cause)"; ACTION="$(_get action)"; ARGS="$(_get args)"; CONF="$(_get confidence)"; EXPL="$(_get explanation)"
[ -n "$ACTION" ] || ACTION="none"
log "diagnosis: cause='$CAUSE' action='$ACTION' args='$ARGS' confidence=$CONF"

# 3) EMAIL the diagnosis (always, if an address is configured)
if [ -n "$EMAIL" ] && command -v mail >/dev/null 2>&1; then
  printf 'SpliceScout CPU-AI diagnosis\n\nStage: %s\nRoot:  %s\nTime:  %s\n\n  cause:      %s\n  action:     %s %s\n  confidence: %s\n  why:        %s\n\nautofix=%s. The deterministic watchdogs handle known failures; this is the local CPU model for the rest.\n' \
    "$JOB_TAG" "$ROOT" "$(ts)" "$CAUSE" "$ACTION" "$ARGS" "$CONF" "$EXPL" "$AUTOFIX" \
    | mail -s "SpliceScout AI diagnosis [$JOB_TAG]: $ACTION" "$EMAIL" 2>/dev/null && log "emailed diagnosis -> $EMAIL"
fi

# 4) AUTOFIX -- only whitelisted, only confident (>=0.6), only within the re-arm budget. All reversible.
n="$(cat "$RB" 2>/dev/null || echo 0)"
_confident="$("$PY" -c "print(1 if float('${CONF:-0}' or 0)>=0.6 else 0)" 2>/dev/null || echo 0)"
if [ "$AUTOFIX" != "1" ]; then
  log "autofix OFF -> emailed recommendation only (set DIAGNOSE_AUTOFIX=1 to let it act)"; exit 0
fi
if [ "$_confident" != "1" ] || [ "${n:-0}" -ge "${DIAGNOSE_MAX_REARMS:-2}" ]; then
  log "autofix skipped (confidence $CONF<0.6 or re-arm budget ${n:-0}/${DIAGNOSE_MAX_REARMS:-2} spent) -> email only"; exit 0
fi

_rearm=0
case "$ACTION" in
  quarantine_bed)
    S="$(printf '%s' "$ARGS" | tr -cd 'A-Za-z0-9._-')"
    if [ -n "$S" ]; then
      BEDOUT="$(dirname "$ROOT")/STAR_beds"
      Q="$ROOT/quarantined_beds"; mkdir -p "$Q" 2>/dev/null
      moved=0
      for suf in __junction.bed __intronJunction.bed __exon.bed; do
        for d in "$BEDOUT" "$ROOT/junction_beds" "$ROOT/STAR_beds"; do
          [ -e "$d/${S}${suf}" ] && mv -f "$d/${S}${suf}" "$Q/" 2>/dev/null && moved=1
        done
      done
      [ "$moved" = 1 ] && log "AUTOFIX: quarantined bed(s) for $S -> $Q" || log "AUTOFIX: no bed found to quarantine for '$S'"
    fi
    _rearm=1 ;;
  rearm) _rearm=1 ;;
  bump_walltime|bump_mem)
    log "AUTOFIX: '$ACTION' recommended, but the deterministic self-heal already escalates -W/-M on TERM_* -> emailing only, not duplicating" ;;
  *) log "AUTOFIX: '$ACTION' is not in the auto-apply set -> email only" ;;
esac

if [ "$_rearm" = "1" ]; then
  echo $((n+1)) > "$RB"
  rm -f "$ROOT/PIPELINE_STALLED.txt" 2>/dev/null
  rmdir "$ROOT/.finalized.lock" 2>/dev/null
  if bsub -L /bin/bash -n 1 -M 1000 -W 20 -J "${JOB_TAG}_watchdog" \
        -o "$LOGD/watchdog.out" -e "$LOGD/watchdog.err" "$SCRIPTS/watchdog.sh" >/dev/null 2>&1; then
    log "AUTOFIX: re-armed the $JOB_TAG watchdog (re-arm $((n+1))/${DIAGNOSE_MAX_REARMS:-2})"
  else
    log "AUTOFIX: re-arm bsub FAILED -- the stage stays STALLED for a human"
  fi
fi
exit 0
