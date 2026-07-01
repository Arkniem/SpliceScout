#!/usr/bin/env bash
# =============================================================================
# run_concordance_pipeline.sh -- THE ONE COMMAND.
#   setup -> submit the concordance job -> watchdog.
# After this you can log off: the watchdog resubmits a failed run and writes
# PIPELINE_COMPLETE.txt (or PIPELINE_STALLED.txt) into PIPELINE_ROOT when done.
# Re-running it later is safe (idempotent): present results are not recomputed,
# a live job is not duplicated, and a second watchdog is not started.
# =============================================================================
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_concordance.sh"
set -u
concord_require_bsub

# SINGLE-FLIGHT: the launcher kicks this while it also self-polls, so two can start at once. Hold a lock;
# the loser exits (the winner is setting the run up).
mkdir -p "$PIPELINE_ROOT" "$LOG_DIR" "$RESULTS_DIR" 2>/dev/null || true
if command -v flock >/dev/null 2>&1 && { exec 9>"$PIPELINE_ROOT/.launch.lock"; } 2>/dev/null; then
  flock -n 9 2>/dev/null || { echo "another run_concordance_pipeline.sh launch is in progress -- exiting"; exit 0; }
fi

echo ">> [1/2] setup"
bash "$HERE/setup.sh" || true                 # advisory; run_concordance_job.sh enforces hard requirements

echo ">> [2/2] submit concordance + start watchdog"
LIVE="$(concord_live_names)"
if concord_done; then
  echo "concordance output already present -> nothing to submit"
elif concord_has_live "$(concord_jobname)" "$LIVE"; then
  echo "concordance job already live -- not resubmitting"
else
  concord_submit_job >/dev/null && echo "concordance job submitted ($(concord_jobname))"
fi

if concord_has_live "${JOB_TAG}_watchdog" "$LIVE"; then
  echo "watchdog already running -- not starting a second one"
else
  when=$(date -d "+$WATCHDOG_INTERVAL_MIN min" '+%Y:%m:%d:%H:%M' 2>/dev/null) ||
  when=$(date -v+"${WATCHDOG_INTERVAL_MIN}"M '+%Y:%m:%d:%H:%M' 2>/dev/null)
  concord_qopt
  bsub -L /bin/bash -n 1 -M 1000 -W 20 -b "$when" -J "${JOB_TAG}_watchdog" \
       -o "$LOG_DIR/watchdog.out" -e "$LOG_DIR/watchdog.err" \
       ${QOPT[@]+"${QOPT[@]}"} "$HERE/watchdog.sh" >/dev/null \
    && echo "watchdog scheduled for $when (then every ${WATCHDOG_INTERVAL_MIN} min)"
fi

cat <<EOF

================================================================
  Splicing concordance stage is now running UNATTENDED. You can log off.
  Watch:   bash $HERE/status.sh
           tail -f $PIPELINE_ROOT/watchdog.log
  Done when $PIPELINE_ROOT/PIPELINE_COMPLETE.txt appears
  (or PIPELINE_STALLED.txt if it can't finish).
  Results -> $RESULTS_DIR/<atlas>/  (concordance.txt + ranked_concordance_summary.txt)
================================================================
EOF
