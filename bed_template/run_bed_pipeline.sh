#!/usr/bin/env bash
# =============================================================================
# run_bed_pipeline.sh -- THE ONE COMMAND.
#   setup -> build BAM list -> submit all BED jobs -> launch the watchdog.
# After this you can log off: the watchdog resubmits failures on its own and
# writes PIPELINE_COMPLETE.txt (or PIPELINE_STALLED.txt) into PIPELINE_ROOT when
# done. Re-running it later is safe (idempotent): finished BEDs are skipped, the
# list is not rebuilt, and a second watchdog is not started.
# =============================================================================
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_bed.sh"
set -u
bed_require_bsub

# SINGLE-FLIGHT (T2.3/T3.2): STAR's finalize kicks this launcher while it also self-polls, so two
# run_bed_pipeline.sh can start at once and race build_bam_list + submit_all. Hold a lock for the launch;
# a loser exits (the winner is setting the run up).
mkdir -p "$PIPELINE_ROOT" 2>/dev/null || true
if command -v flock >/dev/null 2>&1 && { exec 9>"$PIPELINE_ROOT/.launch.lock"; } 2>/dev/null; then
  flock -n 9 2>/dev/null || { echo "another run_bed_pipeline.sh launch is in progress -- exiting"; exit 0; }
fi

echo ">> [1/4] setup"
bash "$HERE/setup.sh" || { echo "setup failed -- fix config.sh and retry" >&2; exit 1; }

echo ">> [2/4] build BAM list"
bash "$HERE/build_bam_list.sh" || exit 1

echo ">> [3/4] submit BED jobs"
bash "$HERE/submit_all.sh"

echo ">> [4/4] start watchdog"
LIVE="$(bed_live_names)"
if bed_has_live "${JOB_TAG}_watchdog" "$LIVE"; then
  echo "watchdog already running -- not starting a second one"
else
  when=$(date -d "+$WATCHDOG_INTERVAL_MIN min" '+%Y:%m:%d:%H:%M' 2>/dev/null) ||
  when=$(date -v+"${WATCHDOG_INTERVAL_MIN}"M '+%Y:%m:%d:%H:%M' 2>/dev/null)
  bed_qopt
  bsub -L /bin/bash -n 1 -M 1000 -W 20 -b "$when" -J "${JOB_TAG}_watchdog" \
       -o "$LOG_DIR/watchdog.out" -e "$LOG_DIR/watchdog.err" \
       ${QOPT[@]+"${QOPT[@]}"} "$HERE/watchdog.sh" >/dev/null \
    && echo "watchdog scheduled for $when (then every ${WATCHDOG_INTERVAL_MIN} min)"
fi

cat <<EOF

================================================================
  BAM->BED stage is now running UNATTENDED. You can log off.
  Watch:   bash $HERE/status.sh
           tail -f $PIPELINE_ROOT/watchdog.log
  Done when $PIPELINE_ROOT/PIPELINE_COMPLETE.txt appears
  (or PIPELINE_STALLED.txt if some BAMs can't finish).
  BEDs -> $BAM_INPUT_DIR  (<sample>__junction.bed + __exon.bed beside each BAM)
================================================================
EOF
