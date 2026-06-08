#!/usr/bin/env bash
# submit_all.sh -- submit one BED job per BAM row in $BAM_LIST.
# Idempotent: skips BAMs that already have both BEDs or a live job, so it is safe
# to re-run to mop up failures.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_bed.sh"
set -u; shopt -s nullglob
bed_require_bsub

[ -f "$BAM_LIST" ] || { echo "no BAM list ($BAM_LIST) -- run build_bam_list.sh first" >&2; exit 1; }
mkdir -p "$PIPELINE_ROOT" "$LOG_DIR"
LIVE="$(bed_live_names)"

n=0; skip=0
while IFS=$'\t' read -r LABEL rest; do
  [ -n "${LABEL:-}" ] || continue
  if bed_done "$LABEL"; then skip=$((skip+1)); continue; fi
  if bed_has_live "$(bed_jobname "$LABEL")" "$LIVE"; then skip=$((skip+1)); continue; fi
  bed_submit_sample "$LABEL" >/dev/null
  n=$((n+1))
done < "$BAM_LIST"

echo "submitted $n BED job(s); skipped $skip (already done or in-flight); BEDs -> $BAM_INPUT_DIR"
