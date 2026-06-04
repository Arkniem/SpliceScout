#!/usr/bin/env bash
# run_pipeline.sh — ONE command to launch the whole automated pipeline, then walk
# away. Runs setup, launches every study (download + convert), and starts the
# self-driving watchdog that recovers failures, re-fetches missing runs, and
# writes PIPELINE_COMPLETE.txt when finished. Run on the LSF submit host.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib.sh"
set -u
sra_require_bsub   # must run on the LSF submit host (not a login node)

echo ">> [1/3] setup"
bash "$HERE/setup.sh" || { echo "setup failed — fix config.sh and retry"; exit 1; }

echo ">> [2/3] launching all studies"
bash "$HERE/run_all.sh"

echo ">> [3/3] starting self-driving watchdog"
LIVE="$(sra_live_names)"
if sra_has_live "${JOB_TAG}_watchdog" "$LIVE"; then
  echo "   watchdog already running — not starting a second one"
else
  sra_qopt
  when=$(date -d "+$WATCHDOG_INTERVAL_MIN min" '+%Y:%m:%d:%H:%M' 2>/dev/null) ||
  when=$(date -v+"${WATCHDOG_INTERVAL_MIN}"M '+%Y:%m:%d:%H:%M' 2>/dev/null)
  bsub -L /bin/bash -n 1 -M 1000 -W 20 -b "$when" -J "${JOB_TAG}_watchdog" \
       -o "$PIPELINE_ROOT/watchdog.out" -e "$PIPELINE_ROOT/watchdog.err" \
       ${QOPT[@]+"${QOPT[@]}"} "$SCRIPTS_DIR/watchdog.sh"
  echo "   watchdog scheduled (first pass ~$WATCHDOG_INTERVAL_MIN min from now)"
fi

cat <<EOF

The pipeline is now running UNATTENDED. Nothing else to do.
  progress : bash $HERE/status.sh
  live log : tail -f $PIPELINE_ROOT/watchdog.log
  finished : $PIPELINE_ROOT/PIPELINE_COMPLETE.txt  (or PIPELINE_STALLED.txt)
EOF
