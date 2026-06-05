#!/usr/bin/env bash
# =============================================================================
# run_star_pipeline.sh -- THE ONE COMMAND.
#   setup -> build sample list -> submit all STAR jobs -> launch the watchdog.
# After this you can log off: the watchdog resubmits failures on its own and
# writes PIPELINE_COMPLETE.txt (or PIPELINE_STALLED.txt) into BAM_OUT when done.
# Re-running it later is safe (idempotent): finished BAMs are skipped, the list
# is not rebuilt, and a second watchdog is not started.
# =============================================================================
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_star.sh"
set -u
star_require_bsub

echo ">> [1/5] setup"
bash "$HERE/setup.sh" || { echo "setup failed -- fix config.sh and retry" >&2; exit 1; }

echo ">> [2/5] resolve genome index"
bash "$HERE/resolve_index.sh" || { echo "index resolution failed -- see message above" >&2; exit 1; }

echo ">> [3/5] build sample list"
bash "$HERE/build_sample_list.sh" || exit 1

echo ">> [4/5] submit STAR jobs"
bash "$HERE/submit_all.sh"

echo ">> [5/5] start watchdog"
LIVE="$(star_live_names)"
if star_has_live "${JOB_TAG}_watchdog" "$LIVE"; then
  echo "watchdog already running -- not starting a second one"
else
  when=$(date -d "+$WATCHDOG_INTERVAL_MIN min" '+%Y:%m:%d:%H:%M' 2>/dev/null) ||
  when=$(date -v+"${WATCHDOG_INTERVAL_MIN}"M '+%Y:%m:%d:%H:%M' 2>/dev/null)
  star_qopt
  bsub -L /bin/bash -n 1 -M 1000 -W 20 -b "$when" -J "${JOB_TAG}_watchdog" \
       -o "$LOG_DIR/watchdog.out" -e "$LOG_DIR/watchdog.err" \
       ${QOPT[@]+"${QOPT[@]}"} "$HERE/watchdog.sh" >/dev/null \
    && echo "watchdog scheduled for $when (then every ${WATCHDOG_INTERVAL_MIN} min)"
fi

cat <<EOF

================================================================
  Pipeline is now running UNATTENDED. You can log off.
  Watch:   bash $HERE/status.sh
           tail -f $PIPELINE_ROOT/watchdog.log
  Done when $PIPELINE_ROOT/PIPELINE_COMPLETE.txt appears
  (or PIPELINE_STALLED.txt if some samples can't finish).
  BAMs -> $BAM_OUT      (logs + SJ.out.tab in $LOG_DIR)
================================================================
EOF
