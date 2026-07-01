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

# Clear STALE terminal markers from a PRIOR run in this REUSED per-instance folder. Without this, a re-run
# into a folder that previously reached PIPELINE_COMPLETE makes the watchdog's first pass see "already
# finalized", run the COMPLETE cleanup (which DELETES the freshly-uploaded scripts), and stop — so the new
# run never self-drives. We're explicitly (re)launching, so any prior terminal state is stale; the watchdog
# re-writes the right marker when this run actually finishes.
for _m in PIPELINE_COMPLETE.txt PIPELINE_STALLED.txt PIPELINE_COMPLETE_PARTIAL.txt \
          PIPELINE_INCOMPLETE_UPSTREAM.txt PIPELINE_ORPHANED.txt PIPELINE_LAUNCH_TIMEOUT.txt; do
  rm -f "$PIPELINE_ROOT/$_m" 2>/dev/null
done
rmdir "$PIPELINE_ROOT/.finalized.lock" 2>/dev/null

echo ">> [2/3] launching the submitter + watchdog as a detached LSF job (so this hand-off returns immediately)"
sra_qopt
if sra_has_live "${JOB_TAG}_launch" "$(sra_live_names)"; then
  echo "   launcher already running — not starting a second one"
else
  # DETACH the launcher bsub with setsid so this hand-off (the upload ssh) returns NOW. Under a saturated
  # per-user pending-job quota (e.g. a concurrent heavy run), `bsub` BLOCKS on "Pending job threshold reached.
  # Retrying in 60s" — inline, that hangs the upload ssh until the head node resets it ("stuck on upload, timed
  # out"). setsid puts the bsub in its OWN session and the redirected fds release the ssh channel, so the ssh
  # closes immediately while the bsub keeps retrying in the background (setsid survives the disconnect — plain
  # nohup does NOT on this cluster) until a slot frees and the launcher job is accepted. Status -> launch.out.
  setsid bsub -L /bin/bash -n 1 -M 2000 -W 1108:00 -J "${JOB_TAG}_launch" \
         -o "$PIPELINE_ROOT/launch.out" -e "$PIPELINE_ROOT/launch.err" \
         ${QOPT[@]+"${QOPT[@]}"} "$SCRIPTS_DIR/launch_all.sh" \
         </dev/null >>"$PIPELINE_ROOT/launch.out" 2>&1 &
  echo "   launcher bsub detached (retries through the pending-job threshold in the background; see launch.out)"
fi

cat <<EOF

The pipeline is now running UNATTENDED. Nothing else to do.
  progress : bash $HERE/status.sh
  live log : tail -f $PIPELINE_ROOT/watchdog.log
  launcher : $PIPELINE_ROOT/launch.out   (submission + watchdog-arm log)
  finished : $PIPELINE_ROOT/PIPELINE_COMPLETE.txt  (or PIPELINE_STALLED.txt)
EOF
