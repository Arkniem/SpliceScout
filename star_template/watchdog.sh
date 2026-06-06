#!/usr/bin/env bash
# =============================================================================
# watchdog.sh -- the self-driving controller. Each pass it:
#   1. resubmits any sample with no valid BAM and no live job,
#   2. counts progress (done / expected) over the FIXED sample list,
#   3. declares COMPLETE (all done, no live work jobs) or STALLED (no forward
#      progress for MAX_STALL_PASSES idle passes), else
#   4. reschedules itself WATCHDOG_INTERVAL_MIN into the future via `bsub -b`.
# Counts only WORK jobs (^${JOB_TAG}_star_), never itself, so nlive can reach 0.
# Started by run_star_pipeline.sh; needs no babysitting.
# =============================================================================
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_star.sh"
[ -f "$PIPELINE_ROOT/RESOLVED_INDEX.env" ] && source "$PIPELINE_ROOT/RESOLVED_INDEX.env"  # resolved GENOME_DIR + BUILD_JID
set -u; shopt -s nullglob
star_load_modules                       # samtools, for quickcheck

LOG="$PIPELINE_ROOT/watchdog.log"
STATE="$PIPELINE_ROOT/.watchdog.state"
ts()  { date '+%Y-%m-%d %H:%M:%S'; }
say() { echo "[$(ts)] $*" >> "$LOG"; }

# Reschedule-first: the NEXT pass is queued at the START of each pass, so a mid-pass walltime kill
# (e.g. bsub blocked on the LSF pending-job threshold) can NEVER break the self-driving chain.
# finalize() cancels the successor once the pipeline is actually done.
WATCHDOG_NEXT_JID=""
reschedule() {
  local when
  when=$(date -d "+$WATCHDOG_INTERVAL_MIN min" '+%Y:%m:%d:%H:%M' 2>/dev/null) ||
  when=$(date -v+"${WATCHDOG_INTERVAL_MIN}"M '+%Y:%m:%d:%H:%M' 2>/dev/null)
  star_qopt
  WATCHDOG_NEXT_JID=$(bsub -L /bin/bash -n 1 -M 1000 -W 20 -b "$when" -J "${JOB_TAG}_watchdog" \
       -o "$LOG_DIR/watchdog.out" -e "$LOG_DIR/watchdog.err" \
       ${QOPT[@]+"${QOPT[@]}"} "$SCRIPTS_DIR/watchdog.sh" 2>/dev/null | star_jobid)
  say "next pass scheduled for $when (job ${WATCHDOG_NEXT_JID:-?})"
}

finalize() {                            # $1 = COMPLETE | STALLED
  local status="$1" label rest
  local rep="$PIPELINE_ROOT/PIPELINE_${status}.txt"   # SEPARATE line: bash 4.2 + set -u can't see $status declared in the SAME `local`
  [ -n "${WATCHDOG_NEXT_JID:-}" ] && bkill "$WATCHDOG_NEXT_JID" >/dev/null 2>&1   # cancel queued successor
  {
    echo "STAR alignment pipeline $status at $(ts)"
    echo "Aligned: $done_n / $exp_n samples"
    echo "BAM dir: $BAM_OUT  ($(du -sh "$BAM_OUT" 2>/dev/null | cut -f1) used)"
    echo
    echo "Per-sample:"
    while IFS=$'\t' read -r label rest; do
      [ -n "$label" ] || continue
      if star_bam_ok "$label"; then printf '  %-44s OK\n' "$label"
      else printf '  %-44s MISSING/INVALID\n' "$label"; fi
    done < "$SAMPLE_LIST"
    if [ "$status" = "STALLED" ]; then
      echo
      echo "Samples with no valid BAM after repeated resubmits -- inspect"
      echo "$LOG_DIR/star_<label>.err (likely a bad/truncated FASTQ, OOM, or"
      echo "persistent scratch shortage):"
      while IFS=$'\t' read -r label rest; do
        [ -n "$label" ] || continue
        star_bam_ok "$label" || echo "  $label"
      done < "$SAMPLE_LIST"
    fi
  } > "$rep"
  say "FINALIZED ($status) -> $rep  (watchdog stopping)"
}

say "=== watchdog pass start ==="
if [ -f "$PIPELINE_ROOT/PIPELINE_COMPLETE.txt" ] || [ -f "$PIPELINE_ROOT/PIPELINE_STALLED.txt" ]; then
  say "already finalized -> stop"; exit 0
fi
[ -f "$SAMPLE_LIST" ] || { say "no sample list at $SAMPLE_LIST -- stopping"; exit 1; }
reschedule                       # queue the NEXT pass FIRST (survives a mid-pass walltime kill)
LIVE="$(star_live_names)"

# 1) resubmit missing/invalid samples that have no live job
resub=0
while IFS=$'\t' read -r label f1 f2; do
  [ -n "$label" ] || continue
  star_bam_ok "$label" && continue
  star_has_live "$(star_jobname "$label")" "$LIVE" && continue
  star_submit_sample "$label" "$f1" "${f2:-NA}" >/dev/null && resub=$((resub+1))
done < "$SAMPLE_LIST"
[ "$resub" -gt 0 ] && say "resubmitted $resub missing/invalid sample(s)"

# 2) progress accounting (denominator FIXED at launch)
exp_n=$(star_expected_count)
done_n=$(star_done_count)
nlive=$(star_live_work_count)
say "progress: $done_n/$exp_n aligned ; $nlive live work job(s) ; $resub resubmitted this pass"

# 3) completion: every sample done AND no live work job
if [ "$done_n" -ge "$exp_n" ] && [ "$nlive" -eq 0 ]; then
  finalize "COMPLETE"; exit 0
fi

# 4) stall: no live jobs AND no forward progress for MAX_STALL_PASSES passes.
#    (Gated on NO PROGRESS, not "nothing resubmitted" -- a genuinely-bad sample
#    is resubmitted every idle pass, so this is what makes it converge instead
#    of looping forever.)
if [ "$nlive" -eq 0 ]; then
  prev=$(cat "$STATE" 2>/dev/null || echo -1)
  stall=0
  [ "$done_n" = "$prev" ] && stall=$(( $(cat "$STATE.stall" 2>/dev/null || echo 0) + 1 ))
  echo "$done_n" > "$STATE"; echo "$stall" > "$STATE.stall"
  if [ "$stall" -ge "$MAX_STALL_PASSES" ]; then finalize "STALLED"; exit 0; fi
else
  echo "$done_n" > "$STATE"; echo 0 > "$STATE.stall"
fi

# Safety net: a successor is normally queued at pass start; reschedule now if that bsub didn't take.
[ -n "${WATCHDOG_NEXT_JID:-}" ] || reschedule
say "pass end -> next pass queued (job ${WATCHDOG_NEXT_JID:-?})"
