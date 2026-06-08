#!/usr/bin/env bash
# lib.sh — submission helpers shared by the driver scripts. Source AFTER config.sh.
# All jobs are submitted with "-L /bin/bash" so the compute-node login shell sets
# up environment modules; job names are namespaced with $JOB_TAG so resubmission
# is idempotent (a driver can check "is <name> already live?" before submitting).

# Fail early with a clear message if not on an LSF submit host (the #1 setup
# mistake: launching from a login node where bsub isn't available).
sra_require_bsub() {
  command -v bsub >/dev/null 2>&1 && return 0
  echo "ERROR: 'bsub' not found on host '$(hostname)'." >&2
  echo "       Launch the pipeline from your LSF SUBMIT HOST, not the login node." >&2
  echo "       On this cluster that is the head node, e.g.:  ssh bmiclusterp-head" >&2
  exit 1
}

# Set QOPT=(-q QUEUE) if a queue is configured, else empty.
sra_qopt() { QOPT=(); [ -n "$LSF_QUEUE" ] && QOPT=(-q "$LSF_QUEUE"); }

# Read "Job <12345> is submitted ..." on stdin -> print 12345
sra_jobid() { sed -n 's/.*Job <\([0-9]*\)>.*/\1/p'; }

# Snapshot of my live (RUN+PEND) LSF job names, one per line.
sra_live_names() { bjobs -noheader -o job_name 2>/dev/null; }

# Is job name $1 present in the newline-separated snapshot $2 ?
sra_has_live() { printf '%s\n' "$2" | grep -qxF "$1"; }

# ---- reliability helpers (reinforcement pass; mirror lib_star.sh) ------------------------------
# bjobs snapshot of live job NAMES. CAPTURE ITS rc AT THE CALL SITE: LIVE="$(sra_snapshot)"; SRA_SNAP_RC=$?
# (command substitution is a SUBSHELL -- a global set inside is lost; read $? in the parent). rc!=0 => skip.
sra_snapshot() { bjobs -noheader -o job_name 2>/dev/null; }

# Targeted, fail-CLOSED liveness re-check for ONE job name before an expensive resubmit (a 2nd query a
# partial bulk snapshot can't fool). Live -> 0 (skip the resubmit).
sra_job_is_live() { bjobs -noheader -o stat -J "$1" 2>/dev/null | grep -qE 'RUN|PEND'; }

# Exactly-once finalization claim via atomic mkdir (NFS-safe). First caller -> 0.
sra_finalize_once() { mkdir "$PIPELINE_ROOT/.finalized.lock" 2>/dev/null; }

# Stable hash of the live WORK-job name SET (pf/cs/fqd/pfm/cm) for no-churn stall detection. Snapshot=$1.
sra_live_work_hash() { printf '%s\n' "$1" | grep -E "^${JOB_TAG}_(pf|cs|fqd|pfm|cm)_" | sort | cksum | cut -d' ' -f1; }

# Submit a prefetch (download) job. Args: studydir  listfile  [dep_jobid]
# Downloads every accession in <studydir>/<listfile> into <studydir>. Echoes job id.
sra_submit_prefetch() {
  local sdir="$1" list="${2:-SraAccList.txt}" dep="${3:-}"
  sra_qopt; local wopt=(); [ -n "$dep" ] && wopt=(-w "ended($dep)")
  bsub -L /bin/bash -n 1 -M "$PREFETCH_MEM_MB" -W "$WALL" \
       -J "${JOB_TAG}_pf_$(basename "$sdir")" \
       -o "$sdir/prefetch.out" -e "$sdir/prefetch.err" \
       ${QOPT[@]+"${QOPT[@]}"} ${wopt[@]+"${wopt[@]}"} \
       "$SCRIPTS_DIR/prefetch_job.sh" "$sdir" "$list" | sra_jobid
}

# Submit ONE conversion job. Args: accession  studydir   Echoes job id.
sra_submit_conversion() {
  local acc="$1" sdir="$2"
  sra_qopt
  bsub -L /bin/bash -n "$THREADS" -M "$MEM_MB" -W "$WALL" -R "span[hosts=1]" \
       -J "${JOB_TAG}_fqd_${acc}" \
       -o "$sdir/fasterqdump_${acc}.out" -e "$sdir/fasterqdump_${acc}.err" \
       ${QOPT[@]+"${QOPT[@]}"} \
       "$SCRIPTS_DIR/fasterqdump_job.sh" "$acc" "$sdir" | sra_jobid
}

# Submit the flatten+convert step for a study (typically gated on its prefetch).
# Args: studydir  [listfile]  [dep_jobid]
#   listfile empty -> handle ALL .sra in the study; else only those accessions.
# Echoes job id.
sra_submit_convert_study() {
  local sdir="$1" list="${2:-}" dep="${3:-}"
  sra_qopt; local wopt=(); [ -n "$dep" ] && wopt=(-w "ended($dep)")
  bsub -L /bin/bash -n 1 -M 2000 -W 30 \
       -J "${JOB_TAG}_cs_$(basename "$sdir")" \
       -o "$sdir/convert_study.out" -e "$sdir/convert_study.err" \
       ${QOPT[@]+"${QOPT[@]}"} ${wopt[@]+"${wopt[@]}"} \
       "$SCRIPTS_DIR/convert_study.sh" "$sdir" "$list" | sra_jobid
}

# Count accessions in a study that already have a .fastq.gz (single- or paired-end).
# Anchored to SraAccList.txt when present (bounded by the list, immune to stale-NFS
# directory over-counts on compute nodes); falls back to a file scan otherwise.
sra_done_count() {  # arg: studydir
  local sdir="$1" a n=0
  if [ -f "$sdir/SraAccList.txt" ]; then
    while read -r a; do
      a=$(echo "$a" | tr -d '\r'); [ -z "$a" ] && continue
      if compgen -G "$sdir/$a.fastq.gz" >/dev/null 2>&1 || compgen -G "$sdir/${a}_[0-9].fastq.gz" >/dev/null 2>&1; then
        n=$((n+1))
      fi
    done < "$sdir/SraAccList.txt"
    echo "$n"
  else
    ls "$sdir"/*.fastq.gz 2>/dev/null \
      | sed 's#.*/##; s/_[0-9]\.fastq\.gz$//; s/\.fastq\.gz$//' | sort -u | wc -l
  fi
}

# Count ${JOB_TAG}_(pf|cs|fqd|pfm|cm)_* WORK-job names in a bjobs snapshot string ($1) with a PURE-BASH
# loop -- NOT `grep -c` (on this cluster's COMPUTE NODES grep -c returns an EMPTY count even though
# matching works; that wedged the watchdog completion gate on the BED stage, LIVE 2026-06-07).
sra_count_work() {
  local n=0 _jn
  while IFS= read -r _jn; do
    case "$_jn" in
      ${JOB_TAG}_pf_*|${JOB_TAG}_cs_*|${JOB_TAG}_fqd_*|${JOB_TAG}_pfm_*|${JOB_TAG}_cm_*) n=$((n+1)) ;;
    esac
  done <<< "${1:-}"
  echo "$n"
}

# Count non-blank lines in a file with a PURE-BASH loop (replaces the unreliable `grep -c .`).
sra_count_nonblank() {
  local n=0 _l
  [ -f "${1:-}" ] || { echo 0; return; }
  while IFS= read -r _l || [ -n "$_l" ]; do
    case "$_l" in (*[![:space:]]*) n=$((n+1)) ;; esac
  done < "$1"
  echo "$n"
}

# Count of live WORK jobs (prefetch/convert/fasterqdump + the re-fetch variants), NEVER the watchdog
# itself -> can reach 0. Mirrors star_live_work_count / bed_live_work_count.
sra_live_work_count() { sra_count_work "$(sra_live_names)"; }

# Wake the watchdog NOW instead of waiting up to WATCHDOG_INTERVAL_MIN for its next timed pass. Called by
# fasterqdump_job.sh right after the LAST conversion publishes its .fastq.gz + drops the source .sra. If
# this is the LAST live work job, it queues a watchdog pass GATED ON THIS JOB ENDING (-w ended($LSB_JOBID))
# so finalize lands within seconds of the final conversion instead of up to a full poll interval later.
# PURE ACCELERATOR -- the timed 30-min poll stays the fallback, so a last job that dies WITHOUT nudging is
# still finalized by the poll. Mirrors star_nudge_watchdog / bed_nudge_watchdog (incl. the runaway fix):
#   * EXACTLY 1 (this still-RUN job), NOT <=1 -- nlive==0 means bjobs failed/overloaded -> do NOT nudge
#     (a spurious 0 under load is what once spawned ~1000 watchdogs). flock(-n): only ONE nudger if several
#     finish together. Spurious/early nudges are absorbed by the already-finalized guard + reschedule-first.
sra_nudge_watchdog() {
  local who="${1:-?}"
  command -v bsub  >/dev/null 2>&1 || return 0
  command -v flock >/dev/null 2>&1 || return 0
  [ -f "$PIPELINE_ROOT/PIPELINE_COMPLETE.txt" ] && return 0
  [ -f "$PIPELINE_ROOT/PIPELINE_STALLED.txt"  ] && return 0
  exec 9>"$PIPELINE_ROOT/.nudge.lock" 2>/dev/null || return 0
  flock -n 9 2>/dev/null || return 0                 # another finisher is already nudging -> skip
  local nlive; nlive="$(sra_live_work_count 2>/dev/null)"; nlive="${nlive:-0}"
  [ "$nlive" -eq 1 ] || return 0
  sra_qopt
  local DEPW=(); [ -n "${LSB_JOBID:-}" ] && DEPW=(-w "ended(${LSB_JOBID})")
  bsub -L /bin/bash -n 1 -M 1000 -W 20 -J "${JOB_TAG}_watchdog" \
       ${DEPW[@]+"${DEPW[@]}"} \
       -o "$PIPELINE_ROOT/watchdog.out" -e "$PIPELINE_ROOT/watchdog.err" \
       ${QOPT[@]+"${QOPT[@]}"} "$SCRIPTS_DIR/watchdog.sh" >/dev/null 2>&1
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] nudge: '$who' last live work job -> watchdog queued on ended(${LSB_JOBID:-?})" \
       >> "$PIPELINE_ROOT/watchdog.log" 2>/dev/null || true
}
