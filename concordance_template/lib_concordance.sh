#!/usr/bin/env bash
# lib_concordance.sh -- shared LSF submit + accounting helpers for the splicing-concordance stage.
# Sourced AFTER config.sh by run_concordance_pipeline.sh, watchdog.sh, run_concordance_job.sh, status.sh.
# This stage runs ONE concordance job over the gathered drug signatures, so accounting is simple:
# "done" = at least one per-atlas concordance.txt exists; "live" = the single ${JOB_TAG}_job is queued.

# Fail fast if not on an LSF submit host.
concord_require_bsub() {
  command -v bsub >/dev/null 2>&1 && return 0
  echo "ERROR: 'bsub' not found -- run this on the LSF SUBMIT host (e.g. bmiclusterp-head)." >&2
  exit 1
}

# QOPT=(-q QUEUE) if a queue is configured, else empty array.
concord_qopt() { QOPT=(); [ -n "$LSF_QUEUE" ] && QOPT=(-q "$LSF_QUEUE"); }

# Parse "Job <12345> is submitted ..." -> 12345
concord_jobid() { sed -n 's/.*Job <\([0-9]\{1,\}\)>.*/\1/p'; }

# Snapshot of all live (PEND+RUN) LSF job names, one per line.
concord_live_names() { bjobs -noheader -o job_name 2>/dev/null; }
concord_snapshot()   { bjobs -noheader -o job_name 2>/dev/null; }

# Is job-name $1 present (exact match) in the newline-separated snapshot $2?
concord_has_live() { printf '%s\n' "$2" | grep -qxF "$1"; }

# The single work job's LSF name (the "_job" infix tells it apart from the watchdog).
concord_jobname() { printf '%s_job' "$JOB_TAG"; }

# Targeted, fail-CLOSED liveness re-check for the work job before an expensive resubmit.
concord_job_is_live() { bjobs -noheader -o stat -J "$(concord_jobname)" 2>/dev/null | grep -qE 'RUN|PEND'; }

# THE single "is the concordance finished" predicate -- used by the job body AND the watchdog so they can
# never disagree. The deliverable is one concordance.txt per scored atlas under RESULTS_DIR/<atlas>/.
# Counted with PURE-BASH globbing (nullglob), NOT ls|grep -c (empty on compute nodes -- see lib_bed.sh).
concord_done() {
  shopt -s nullglob
  local a
  a=("$RESULTS_DIR"/*/concordance.txt)
  [ "${#a[@]}" -gt 0 ]
}

# Count ${JOB_TAG}_job WORK-job names in a bjobs snapshot string ($1) with a PURE-BASH loop -- NOT
# `grep -c` (its count output is empty on the compute nodes). At most 1 for this single-job stage.
concord_count_work() {
  local n=0 _jn
  while IFS= read -r _jn; do
    case "$_jn" in ${JOB_TAG}_job) n=$((n+1)) ;; esac
  done <<< "${1:-}"
  echo "$n"
}
concord_live_work_count() { concord_count_work "$(concord_live_names)"; }

# Submit the ONE concordance job. Echoes the LSF job id.
concord_submit_job() {
  concord_qopt
  bsub -L /bin/bash ${QOPT[@]+"${QOPT[@]}"} \
       -J "$(concord_jobname)" -n "$THREADS" -W "$WALL" -M "$MEM_MB" \
       -R "span[hosts=1]" \
       -o "$LOG_DIR/concord_job.out" -e "$LOG_DIR/concord_job.err" \
       "$SCRIPTS_DIR/run_concordance_job.sh" | concord_jobid
}

# Exactly-once finalization claim via an atomic mkdir (NFS-safe where flock degrades). First caller -> 0.
concord_finalize_once() { mkdir "$PIPELINE_ROOT/.finalized.lock" 2>/dev/null; }

# Wake the watchdog NOW instead of waiting up to WATCHDOG_INTERVAL_MIN for its next timed pass. Called by
# run_concordance_job.sh after the concordance results are present. Gated on THIS job ending so the woken
# pass sees nlive==0 and FINALIZES. PURE ACCELERATOR -- the timed poll stays the fallback.
concord_nudge_watchdog() {
  local who="${1:-?}"
  command -v bsub  >/dev/null 2>&1 || return 0
  command -v flock >/dev/null 2>&1 || return 0
  [ -f "$PIPELINE_ROOT/PIPELINE_COMPLETE.txt" ] && return 0
  [ -f "$PIPELINE_ROOT/PIPELINE_STALLED.txt"  ] && return 0
  exec 9>"$PIPELINE_ROOT/.nudge.lock" 2>/dev/null || return 0
  flock -n 9 2>/dev/null || return 0
  local nlive; nlive="$(concord_live_work_count 2>/dev/null)"; nlive="${nlive:-0}"
  [ "$nlive" -eq 1 ] || return 0
  concord_qopt
  local DEPW=(); [ -n "${LSB_JOBID:-}" ] && DEPW=(-w "ended(${LSB_JOBID})")
  bsub -L /bin/bash -n 1 -M 1000 -W 20 -J "${JOB_TAG}_watchdog" \
       ${DEPW[@]+"${DEPW[@]}"} \
       -o "$LOG_DIR/watchdog.out" -e "$LOG_DIR/watchdog.err" \
       ${QOPT[@]+"${QOPT[@]}"} "$SCRIPTS_DIR/watchdog.sh" >/dev/null 2>&1
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] nudge: '$who' finished -> watchdog queued on ended(${LSB_JOBID:-?})" \
       >> "$PIPELINE_ROOT/watchdog.log" 2>/dev/null || true
}
