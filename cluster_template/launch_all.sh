#!/usr/bin/env bash
# launch_all.sh — the SLOW launch work (submit every study, then arm the watchdog), run as its OWN LSF job
# so run_pipeline.sh's ssh hand-off returns IMMEDIATELY instead of blocking until all studies are submitted.
# WHY: on a big run (hundreds of studies / tens of thousands of accessions) every `bsub` blocks on the
# cluster's pending-job threshold ("Retrying in 60 seconds…"), so submitting inline can take far longer than
# the launching ssh's timeout — which then KILLS run_pipeline before it ever arms the watchdog, leaving the
# work jobs orphaned. Detaching into this bsub'd job (a compute node, generous walltime) fixes that: the
# submit hand-off returns in seconds, and this job submits everything + arms the watchdog at the cluster's pace.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib.sh"
set -u
sra_require_bsub

# Submit all studies in the BACKGROUND so THIS job can act as a TEMPORARY watchdog while it submits. On a big
# run the submission can take >1h under the pending-job threshold, and until it finishes the real watchdog
# isn't armed — so a conversion that dies mid-submit would sit stranded with nothing to resubmit it. While
# run_all is alive we periodically run a HEAL-ONLY watchdog pass (resubmit dead conversions only; no refetch/
# finalize/reschedule) to keep progress moving. Poll every 30s so the real watchdog arms promptly when done.
echo ">> launcher: submitting all studies in the background (temp-watchdog active during submission)"
bash "$HERE/run_all.sh" &
RUN_ALL_PID=$!
_heal_every=$(( ${LAUNCH_HEAL_INTERVAL_SECS:-600} )); _since=0
while kill -0 "$RUN_ALL_PID" 2>/dev/null; do
  sleep 30
  kill -0 "$RUN_ALL_PID" 2>/dev/null || break
  _since=$(( _since + 30 ))
  if [ "$_since" -ge "$_heal_every" ]; then
    _since=0
    echo ">> launcher: interim HEAL-ONLY watchdog pass (submission still in progress)"
    SRA_WATCHDOG_HEAL_ONLY=1 bash "$HERE/watchdog.sh" >/dev/null 2>&1 || true
  fi
done
wait "$RUN_ALL_PID" 2>/dev/null

echo ">> launcher: submission complete — arming the self-driving watchdog"
LIVE="$(sra_live_names)"
if sra_has_live "${JOB_TAG}_watchdog" "$LIVE"; then
  echo "   watchdog already running — not starting a second one"
else
  sra_qopt
  bsub -L /bin/bash -n 1 -M 1000 -W "$(( ${WATCHDOG_INTERVAL_MIN:-30} - 5 ))" -J "${JOB_TAG}_watchdog" \
       -o "$PIPELINE_ROOT/watchdog.out" -e "$PIPELINE_ROOT/watchdog.err" \
       ${QOPT[@]+"${QOPT[@]}"} "$SCRIPTS_DIR/watchdog.sh" \
    && echo "   watchdog scheduled" \
    || echo "   watchdog bsub FAILED — the run will not self-drive (investigate)"
fi
echo ">> launcher: done (studies submitted; watchdog armed). This job now exits."
