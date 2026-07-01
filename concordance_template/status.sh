#!/usr/bin/env bash
# status.sh -- one-shot progress read for the splicing-concordance stage.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_concordance.sh"
set -u; shopt -s nullglob

echo "== splicing concordance -- $JOB_TAG =="
sigs=("$DRUG_SIG_DIR"/PSI.*_vs_*.txt); echo "  drug signatures gathered : ${#sigs[@]}"
if [ -s "$QUERIES_FILE" ]; then
  nq=0; while IFS=$'\t' read -r a b c d || [ -n "${a:-}" ]; do case "${a:-}" in ''|'#'*) ;; *) nq=$((nq+1));; esac; done < "$QUERIES_FILE"
  echo "  cancer atlases to score  : $nq"
fi

echo "  per-atlas results:"
for d in "$RESULTS_DIR"/*/; do
  [ -d "$d" ] || continue
  nm="$(basename "$d")"
  if [ -s "$d/concordance.txt" ]; then echo "    $nm: concordance.txt PRESENT$( [ -s "$d/ranked_concordance_summary.txt" ] && echo ' + ranked summary' )"; else echo "    $nm: (pending)"; fi
done

echo "  markers:"
for m in PIPELINE_COMPLETE PIPELINE_STALLED PIPELINE_INCOMPLETE_UPSTREAM PIPELINE_LAUNCH_TIMEOUT PIPELINE_ORPHANED; do
  [ -f "$PIPELINE_ROOT/$m.txt" ] && echo "    $m"
done

echo "  live jobs (this run):"
bjobs -noheader -o "job_name stat" 2>/dev/null | grep "^${JOB_TAG}_" | awk '{print $2}' | sort | uniq -c | sed 's/^/    /'

echo "  watchdog tail:"
tail -n 15 "$PIPELINE_ROOT/watchdog.log" 2>/dev/null | sed 's/^/    /'
