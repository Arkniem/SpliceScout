#!/usr/bin/env bash
# status.sh -- one-shot progress read for the AltAnalyze (PSI) stage.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_psi.sh"
set -u; shopt -s nullglob

echo "== AltAnalyze splicing (PSI) -- $JOB_TAG =="
echo "  BED input : $BED_INPUT_DIR"
beds=("$BED_INPUT_DIR"/*__junction.bed); echo "  junction BEDs present : ${#beds[@]}"

if [ -s "$GROUPS_FILE" ] && [ -s "$COMPS_FILE" ]; then
  ng=0; while IFS= read -r _l; do case "$_l" in (*[![:space:]]*) ng=$((ng+1)) ;; esac; done < "$GROUPS_FILE"
  echo "  comparison: $ng grouped samples ; comps -> $(tr '\n' ';' < "$COMPS_FILE")"
else
  echo "  comparison: groupless (per-sample PSI only)"
fi

if psi_done; then echo "  PSI output: PRESENT ($PSI_OUT/AltResults/AlternativeOutput)"; else echo "  PSI output: not yet"; fi

echo "  markers:"
for m in PIPELINE_COMPLETE PIPELINE_STALLED PIPELINE_INCOMPLETE_UPSTREAM PIPELINE_LAUNCH_TIMEOUT PIPELINE_ORPHANED; do
  [ -f "$PIPELINE_ROOT/$m.txt" ] && echo "    $m"
done

echo "  live jobs (this run):"
bjobs -noheader -o "job_name stat" 2>/dev/null | grep "^${JOB_TAG}_" | awk '{print $2}' | sort | uniq -c | sed 's/^/    /'
echo "  AltAnalyze job RUNNING: $(bjobs -noheader -o 'job_name stat' 2>/dev/null | grep -E "^${JOB_TAG}_job.*RUN" | wc -l)"

echo "  watchdog tail:"
tail -n 15 "$PIPELINE_ROOT/watchdog.log" 2>/dev/null | sed 's/^/    /'
