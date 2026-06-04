#!/usr/bin/env bash
# run_all.sh — launch the pipeline for every study: submit a prefetch (download)
# job and a dependent flatten+convert job per study. Idempotent: skips studies
# already complete or that already have a live prefetch/convert job, so it is
# safe to re-run. Run on the LSF submit host (where bsub works).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib.sh"
set -u
sra_require_bsub   # must run on the LSF submit host (not a login node)
cd "$STUDIES_DIR" || { echo "run_all: STUDIES_DIR not found: $STUDIES_DIR" >&2; exit 1; }

LIVE="$(sra_live_names)"
launched=0
for S in */; do
  name=$(basename "$S")
  [ -f "$S/SraAccList.txt" ] || { echo "skip $name (no SraAccList.txt)"; continue; }
  sdir="$STUDIES_DIR/$name"

  # idempotent skips
  nacc=$(grep -c . "$S/SraAccList.txt")
  ngz=$(sra_done_count "$sdir")
  nsra=$(ls "$sdir"/*.sra 2>/dev/null | wc -l)
  if [ "$ngz" -ge "$nacc" ] && [ "$nsra" -eq 0 ]; then
    echo "skip $name (complete: $ngz/$nacc)"; continue
  fi
  if sra_has_live "${JOB_TAG}_pf_${name}" "$LIVE" || sra_has_live "${JOB_TAG}_cs_${name}" "$LIVE"; then
    echo "skip $name (already in progress)"; continue
  fi

  PF=$(sra_submit_prefetch "$sdir" "SraAccList.txt")
  CS=$(sra_submit_convert_study "$sdir" "" "$PF")
  echo "launched $name : prefetch=$PF convert=$CS"
  launched=$((launched+1))
done
echo "=== run_all: launched $launched study/studies ==="
