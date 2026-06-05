#!/usr/bin/env bash
# lib_star.sh -- shared LSF submit + accounting helpers for the STAR pipeline.
# Sourced AFTER config.sh by submit_all.sh, watchdog.sh, status.sh, setup.sh.

# Fail fast if not on an LSF submit host.
star_require_bsub() {
  command -v bsub >/dev/null 2>&1 && return 0
  echo "ERROR: 'bsub' not found -- run this on the LSF SUBMIT host (e.g. bmiclusterp-head)." >&2
  exit 1
}

# QOPT=(-q QUEUE) if a queue is configured, else empty array.
star_qopt() { QOPT=(); [ -n "$LSF_QUEUE" ] && QOPT=(-q "$LSF_QUEUE"); }

# Parse "Job <12345> is submitted ..." -> 12345
star_jobid() { sed -n 's/.*Job <\([0-9]\{1,\}\)>.*/\1/p'; }

# Snapshot of all live (PEND+RUN) LSF job names, one per line.
star_live_names() { bjobs -noheader -o job_name 2>/dev/null; }

# Is job-name $1 present (exact match) in the newline-separated snapshot $2?
star_has_live() { printf '%s\n' "$2" | grep -qxF "$1"; }

# An LSF-safe, length-capped job name for a (possibly long/odd) sample label.
# Deterministic across passes so skip-if-live keeps matching the same job.
star_jobname() {
  local s="$1"
  s="${s//[^A-Za-z0-9_.-]/_}"               # only LSF-safe characters
  if [ "${#s}" -gt 40 ]; then               # cap length: readable head + stable hash tail
    local h; h="$(printf '%s' "$1" | cksum | cut -d' ' -f1)"
    s="${s:0:31}_${h}"
  fi
  printf '%s_star_%s' "$JOB_TAG" "$s"
}

# THE single "is this sample DONE" predicate -- used by BOTH the job body and the
# watchdog, so they can never disagree: BAM exists AND samtools quickcheck passes.
star_bam_ok() {
  local b="$BAM_OUT/$1.bam"
  [ -s "$b" ] && samtools quickcheck "$b" 2>/dev/null
}

# Submit ONE sample. Args: label fastq1 fastq2(:-NA). Echoes the LSF job id.
star_submit_sample() {
  local label="$1" f1="$2" f2="${3:-NA}" jn
  jn="$(star_jobname "$label")"
  star_qopt
  # If a build-once index job is pending (BUILD_JID, from RESOLVED_INDEX.env), wait for it.
  local DEPW=(); [ -n "${BUILD_JID:-}" ] && DEPW=(-w "done(${BUILD_JID})")
  bsub -L /bin/bash ${QOPT[@]+"${QOPT[@]}"} ${DEPW[@]+"${DEPW[@]}"} \
       -J "$jn" -n "$THREADS" -W "$WALL" -M "$MEM_MB" \
       -R "rusage[mem=${MEM_RUSAGE}] span[hosts=1]" \
       -o "$LOG_DIR/star_${label}.out" -e "$LOG_DIR/star_${label}.err" \
       "$SCRIPTS_DIR/run_star_job.sh" "$label" "$f1" "$f2" | star_jobid
}

# Non-empty rows in the sample list = the fixed-at-launch denominator.
star_expected_count() { grep -cve '^[[:space:]]*$' "$SAMPLE_LIST" 2>/dev/null || echo 0; }

# Rows whose label has a valid BAM (anchored to the list, immune to stray *.bam).
star_done_count() {
  local n=0 label rest
  [ -f "$SAMPLE_LIST" ] || { echo 0; return; }
  while IFS=$'\t' read -r label rest; do
    [ -n "$label" ] || continue
    star_bam_ok "$label" && n=$((n+1))
  done < "$SAMPLE_LIST"
  echo "$n"
}

# Count of live WORK jobs (the star_ prefix), never the watchdog -> can reach 0.
star_live_work_count() {
  printf '%s\n' "$(star_live_names)" | grep -cE "^${JOB_TAG}_star_"
}
