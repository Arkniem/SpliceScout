#!/usr/bin/env bash
# =============================================================================
# watchdog.sh -- the self-driving controller for the single AltAnalyze (PSI) job.
# Each pass it:
#   1. (re)schedules the NEXT pass FIRST (survives a mid-pass walltime kill),
#   2. if the PSI table is present and no work job is live -> COMPLETE,
#   3. if the job died with no output -> resubmit it (up to MAX_RESUBMITS) else STALLED,
#   4. else (job still RUNNING) just waits for the next pass.
# Counts only the WORK job (^${JOB_TAG}_job), never itself, so nlive can reach 0.
# This is the LAST stage in the chain (download->STAR->BED->PSI); finalize() kicks nothing downstream.
# =============================================================================
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_psi.sh"
set -u; shopt -s nullglob

LOG="$PIPELINE_ROOT/watchdog.log"
STATE="$PIPELINE_ROOT/.watchdog.state"
ts()  { date '+%Y-%m-%d %H:%M:%S'; }
say() { echo "[$(ts)] $*" >> "$LOG"; }

# Reschedule-first: the NEXT pass is queued at the START of each pass, so a mid-pass walltime kill can
# NEVER break the self-driving chain. finalize() cancels the successor once the stage is actually done.
WATCHDOG_NEXT_JID=""
RESCHED_RC=0
reschedule() {
  local when out
  when=$(date -d "+$WATCHDOG_INTERVAL_MIN min" '+%Y:%m:%d:%H:%M' 2>/dev/null) ||
  when=$(date -v+"${WATCHDOG_INTERVAL_MIN}"M '+%Y:%m:%d:%H:%M' 2>/dev/null)
  psi_qopt
  out=$(bsub -L /bin/bash -n 1 -M 1000 -W 20 -b "$when" -J "${JOB_TAG}_watchdog" \
       -o "$LOG_DIR/watchdog.out" -e "$LOG_DIR/watchdog.err" \
       ${QOPT[@]+"${QOPT[@]}"} "$SCRIPTS_DIR/watchdog.sh" 2>&1)
  RESCHED_RC=$?
  WATCHDOG_NEXT_JID=$(printf '%s' "$out" | psi_jobid)
  say "next pass scheduled for $when (job ${WATCHDOG_NEXT_JID:-?}, rc=$RESCHED_RC)"
}

finalize() {                            # $1 = COMPLETE | STALLED
  local status="$1"
  local rep="$PIPELINE_ROOT/PIPELINE_${status}.txt"   # SEPARATE line (bash 4.2 + set -u)
  psi_finalize_once || { say "finalize already claimed by a concurrent pass -> skip"; return 0; }
  # cancel ALL queued watchdog successors except THIS job
  local _self _wj
  _self="${LSB_JOBID:-}"
  for _wj in $(bjobs -noheader -o jobid -J "${JOB_TAG}_watchdog" 2>/dev/null); do
    [ "$_wj" = "$_self" ] && continue
    bkill "$_wj" >/dev/null 2>&1
  done
  # PARTIAL if the upstream BED run was incomplete (launcher dropped the marker). PSI still ran on whatever
  # BEDs were present; we just report it honestly and skip the optional uploaded-toolkit cleanup.
  local partial=0
  { [ "$status" = "STALLED" ] || [ -f "$PIPELINE_ROOT/PIPELINE_INCOMPLETE_UPSTREAM.txt" ]; } && partial=1
  local ng=0
  [ -s "$GROUPS_FILE" ] && while IFS= read -r _l; do case "$_l" in (*[![:space:]]*) ng=$((ng+1)) ;; esac; done < "$GROUPS_FILE"
  {
    echo "AltAnalyze splicing (PSI) stage $status at $(ts)"
    [ "$partial" = "1" ] && echo "*** PARTIAL: upstream BED incomplete and/or this stage STALLED ***"
    if psi_done; then echo "PSI table: PRESENT -> $PSI_OUT/AltResults/AlternativeOutput"
    else echo "PSI table: MISSING (AltAnalyze did not produce output)"; fi
    if [ -s "$GROUPS_FILE" ] && [ -s "$COMPS_FILE" ]; then
      echo "Comparison: $ng grouped samples ; comps -> $(tr '\n' ';' < "$COMPS_FILE")"
    else
      echo "Comparison: groupless (per-sample PSI only -- no usable 2-group split)"
    fi
    echo "BED input: $BED_INPUT_DIR"
    if [ "$status" = "STALLED" ]; then
      echo
      echo "AltAnalyze did not finish after $(cat "$STATE.resub" 2>/dev/null || echo 0) resubmit(s)."
      echo "Inspect $LOG_DIR/psi_job.err (likely a missing species DB, a bad BED, or an R/python problem)."
    fi
  } > "$rep"
  # optional cleanup: ONLY remove an UPLOADED altanalyze_home (one that lives UNDER PIPELINE_ROOT), and only
  # on a clean COMPLETE. NEVER touches a found-on-cluster ALTANALYZE_HOME (e.g. the lab install).
  if [ "$status" = "COMPLETE" ] && [ "$partial" = "0" ] && [ "${CLEANUP_TOOLS_WHEN_DONE:-0}" = "1" ]; then
    case "$ALTANALYZE_HOME" in
      "$PIPELINE_ROOT"/*) [ "$ALTANALYZE_HOME" != "/" ] && rm -rf "$ALTANALYZE_HOME" 2>/dev/null \
                            && say "cleanup: removed uploaded AltAnalyze home $ALTANALYZE_HOME (kept PSI results)" ;;
      *) say "cleanup: ALTANALYZE_HOME is external ($ALTANALYZE_HOME) -> left untouched" ;;
    esac
  fi
  say "FINALIZED ($status; partial=$partial) -> $rep  (watchdog stopping)"
}

say "=== watchdog pass start ==="
if [ -f "$PIPELINE_ROOT/PIPELINE_COMPLETE.txt" ] || [ -f "$PIPELINE_ROOT/PIPELINE_STALLED.txt" ]; then
  say "already finalized -> stop"; exit 0
fi
# Reclaim a STALE finalize lock (no marker exists => a prior finalize died before writing one).
[ -d "$PIPELINE_ROOT/.finalized.lock" ] && rmdir "$PIPELINE_ROOT/.finalized.lock" 2>/dev/null

# SINGLE-FLIGHT: only ONE pass at a time (a nudged + a timed successor would otherwise double-submit).
exec 8>"$PIPELINE_ROOT/.watchdog.run.lock" 2>/dev/null || true
if command -v flock >/dev/null 2>&1; then
  flock -n 8 2>/dev/null || { say "another watchdog pass holds the run lock -> exit"; exit 0; }
fi

reschedule                       # queue the NEXT pass FIRST (survives a mid-pass walltime kill)

# ABSOLUTE BACKSTOP (bjobs-independent): cap by pass-count AND wall-clock so a permanently-PENDING job or a
# persistently-broken bjobs can never loop forever with no human signal.
_now=$(date +%s)
[ -f "$STATE.firstpass" ] || echo "$_now" > "$STATE.firstpass"
_first=$(cat "$STATE.firstpass" 2>/dev/null || echo "$_now")
_passes=$(( $(cat "$STATE.passes" 2>/dev/null || echo 0) + 1 )); echo "$_passes" > "$STATE.passes"
if [ "$_passes" -ge "${ABSOLUTE_MAX_PASSES:-960}" ] || [ "$(( _now - _first ))" -ge "$(( ${MAX_WALL_HOURS:-336} * 3600 ))" ]; then
  say "BACKSTOP: passes=$_passes wall=$(( _now - _first ))s exceeded the cap -> STALLED"
  finalize "STALLED"; exit 0
fi

# SNAPSHOT WITH RC: bjobs FAILED or EMPTY -> unreliable -> skip the pass. (This running watchdog always
# appears in bjobs, so a truly empty list means bjobs failed.)
LIVE="$(psi_snapshot)"; PSI_SNAP_RC=$?
if [ "$PSI_SNAP_RC" -ne 0 ] || [ -z "$(printf '%s' "$LIVE" | tr -d '[:space:]')" ]; then
  say "WARNING: bjobs failed/empty (rc=$PSI_SNAP_RC) -- skipping this pass"
  exit 0
fi
nlive=$(psi_count_work "$LIVE")     # 0 or 1 (pure-bash; grep -c is empty on compute nodes)
done=0; psi_done && done=1
say "progress: psi_done=$done ; nlive=$nlive ; resubmits=$(cat "$STATE.resub" 2>/dev/null || echo 0)"

# 1) completion: PSI table present AND no live work job
if [ "$done" -eq 1 ] && [ "$nlive" -eq 0 ]; then
  finalize "COMPLETE"; exit 0
fi

# 2) the job died with no output -> resubmit (bounded), else STALLED
if [ "$done" -eq 0 ] && [ "$nlive" -eq 0 ] && ! psi_job_is_live; then
  nresub=$(cat "$STATE.resub" 2>/dev/null || echo 0)
  if [ "$nresub" -ge "${MAX_RESUBMITS:-2}" ]; then
    say "AltAnalyze not done and resubmit budget (${MAX_RESUBMITS:-2}) exhausted -> STALLED"
    finalize "STALLED"; exit 0
  fi
  beds=("$BED_INPUT_DIR"/*__junction.bed)
  if [ "${#beds[@]}" -eq 0 ]; then
    say "no junction BEDs present yet -- waiting (no resubmit)"
  elif psi_submit_job >/dev/null; then
    nresub=$((nresub+1)); echo "$nresub" > "$STATE.resub"
    say "resubmitted the AltAnalyze job (attempt $nresub/${MAX_RESUBMITS:-2})"
  fi
fi

# Safety net: re-arm ONLY if the pass-start reschedule actually FAILED with no successor id.
if [ "${RESCHED_RC:-0}" -ne 0 ] && [ -z "${WATCHDOG_NEXT_JID:-}" ]; then
  reschedule
  if [ "${RESCHED_RC:-0}" -ne 0 ] && [ -z "${WATCHDOG_NEXT_JID:-}" ]; then
    say "ORPHAN: could not queue a successor twice (PEND cap?) -- chain may stop"
    { echo "PSI watchdog could not reschedule at $(ts): bsub failed twice (likely the LSF pending-job cap)."
      echo "Re-arm manually:  bsub -L /bin/bash -n 1 -M 1000 -W 20 -J ${JOB_TAG}_watchdog $SCRIPTS_DIR/watchdog.sh"
    } > "$PIPELINE_ROOT/PIPELINE_ORPHANED.txt"
  fi
fi
say "pass end -> next pass queued (job ${WATCHDOG_NEXT_JID:-?})"
