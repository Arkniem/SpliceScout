#!/usr/bin/env bash
# lib_psi.sh -- shared LSF submit + accounting helpers for the AltAnalyze (PSI) stage.
# Sourced AFTER config.sh by run_psi_pipeline.sh, watchdog.sh, run_psi_job.sh, setup.sh, status.sh.
# This stage runs ONE AltAnalyze job over the whole BED dir (not per-sample), so the accounting is
# simpler than BED's: "done" = the PSI output table exists; "live" = the single ${JOB_TAG}_job is queued.

# Fail fast if not on an LSF submit host.
psi_require_bsub() {
  command -v bsub >/dev/null 2>&1 && return 0
  echo "ERROR: 'bsub' not found -- run this on the LSF SUBMIT host (e.g. bmiclusterp-head)." >&2
  exit 1
}

# QOPT=(-q QUEUE) if a queue is configured, else empty array.
psi_qopt() { QOPT=(); [ -n "$LSF_QUEUE" ] && QOPT=(-q "$LSF_QUEUE"); }

# Parse "Job <12345> is submitted ..." -> 12345
psi_jobid() { sed -n 's/.*Job <\([0-9]\{1,\}\)>.*/\1/p'; }

# Snapshot of all live (PEND+RUN) LSF job names, one per line.
psi_live_names() { bjobs -noheader -o job_name 2>/dev/null; }
psi_snapshot()   { bjobs -noheader -o job_name 2>/dev/null; }

# Is job-name $1 present (exact match) in the newline-separated snapshot $2?
psi_has_live() { printf '%s\n' "$2" | grep -qxF "$1"; }

# The single work job's LSF name (the "_job" infix tells it apart from the watchdog).
psi_jobname() { printf '%s_job' "$JOB_TAG"; }

# Targeted, fail-CLOSED liveness re-check for the work job before an expensive resubmit.
psi_job_is_live() { bjobs -noheader -o stat -J "$(psi_jobname)" 2>/dev/null | grep -qE 'RUN|PEND'; }

# THE single "is the PSI analysis finished" predicate -- used by the job body AND the watchdog so they can
# never disagree. AltAnalyze's RNASeq splicing deliverable is the per-sample PSI event-annotation table
# under <PSI_OUT>/AltResults/AlternativeOutput/ (written whether or not a group comparison runs). Counted
# with PURE-BASH globbing (nullglob), NOT ls|grep -c: on this cluster's COMPUTE NODES `grep -c` returns an
# EMPTY count (see lib_bed.sh) which would wedge the watchdog's integer gate.
psi_done() {
  shopt -s nullglob
  local a
  a=("$PSI_OUT"/AltResults/AlternativeOutput/*EventAnnotation*)
  [ "${#a[@]}" -gt 0 ] && return 0
  a=("$PSI_OUT"/AltResults/AlternativeOutput/*PSI*.txt)   # fallback: any PSI table emitted
  [ "${#a[@]}" -gt 0 ]
}

# Count ${JOB_TAG}_job WORK-job names in a bjobs snapshot string ($1) with a PURE-BASH loop -- NOT
# `grep -c` (its count output is empty on the compute nodes). The watchdog passes its already-captured
# snapshot so the count matches the same bjobs read it gated on. (At most 1 for this single-job stage.)
psi_count_work() {
  local n=0 _jn
  while IFS= read -r _jn; do
    case "$_jn" in ${JOB_TAG}_job) n=$((n+1)) ;; esac
  done <<< "${1:-}"
  echo "$n"
}
psi_live_work_count() { psi_count_work "$(psi_live_names)"; }

# Submit the ONE AltAnalyze job. Echoes the LSF job id.
psi_submit_job() {
  psi_qopt
  bsub -L /bin/bash ${QOPT[@]+"${QOPT[@]}"} \
       -J "$(psi_jobname)" -n "$THREADS" -W "$WALL" -M "$MEM_MB" \
       -R "span[hosts=1]" \
       -o "$LOG_DIR/psi_job.out" -e "$LOG_DIR/psi_job.err" \
       "$SCRIPTS_DIR/run_psi_job.sh" | psi_jobid
}

# Exactly-once finalization claim via an atomic mkdir (NFS-safe where flock degrades). First caller -> 0.
psi_finalize_once() { mkdir "$PIPELINE_ROOT/.finalized.lock" 2>/dev/null; }

# Wake the watchdog NOW instead of waiting up to WATCHDOG_INTERVAL_MIN for its next timed pass. Called by
# run_psi_job.sh after AltAnalyze finishes and the PSI table is present. Gated on THIS job ending so the
# woken pass sees nlive==0 and FINALIZES. PURE ACCELERATOR -- the timed poll stays the fallback.
#   * EXACTLY 1 live work job (this still-RUN job): nlive==0 means bjobs FAILED -> do NOT nudge.
#   * flock(-n): only one nudge; spurious/early nudges are absorbed by the already-finalized guard.
psi_nudge_watchdog() {
  local who="${1:-?}"
  command -v bsub  >/dev/null 2>&1 || return 0
  command -v flock >/dev/null 2>&1 || return 0
  [ -f "$PIPELINE_ROOT/PIPELINE_COMPLETE.txt" ] && return 0
  [ -f "$PIPELINE_ROOT/PIPELINE_STALLED.txt"  ] && return 0
  exec 9>"$PIPELINE_ROOT/.nudge.lock" 2>/dev/null || return 0
  flock -n 9 2>/dev/null || return 0
  local nlive; nlive="$(psi_live_work_count 2>/dev/null)"; nlive="${nlive:-0}"
  [ "$nlive" -eq 1 ] || return 0
  psi_qopt
  local DEPW=(); [ -n "${LSB_JOBID:-}" ] && DEPW=(-w "ended(${LSB_JOBID})")
  bsub -L /bin/bash -n 1 -M 1000 -W 20 -J "${JOB_TAG}_watchdog" \
       ${DEPW[@]+"${DEPW[@]}"} \
       -o "$LOG_DIR/watchdog.out" -e "$LOG_DIR/watchdog.err" \
       ${QOPT[@]+"${QOPT[@]}"} "$SCRIPTS_DIR/watchdog.sh" >/dev/null 2>&1
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] nudge: '$who' finished -> watchdog queued on ended(${LSB_JOBID:-?})" \
       >> "$PIPELINE_ROOT/watchdog.log" 2>/dev/null || true
}
