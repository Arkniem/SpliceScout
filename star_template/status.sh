#!/usr/bin/env bash
# status.sh -- one-shot progress report. The pipeline self-monitors regardless;
# this is just for a human peeking in.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_star.sh"
set -u; shopt -s nullglob
star_load_modules

if [ -f "$SAMPLE_LIST" ]; then
  echo "aligned: $(star_done_count) / $(star_expected_count) samples"
else
  echo "no sample list yet ($SAMPLE_LIST)"
fi

echo "live jobs (this run):"
bjobs -noheader -o "job_name stat" 2>/dev/null | grep "^${JOB_TAG}_" | awk '{print $2}' | sort | uniq -c | sed 's/^/  /'
echo "  STAR running: $(bjobs -noheader -o 'job_name stat' 2>/dev/null | grep -E "^${JOB_TAG}_star_.*RUN" | wc -l)"

echo "recent failures (EXIT, excluding cancellations):"
f=$(bjobs -a -noheader -o "jobid job_name stat exit_reason delimiter='|'" 2>/dev/null \
     | awk -F'|' '$2 ~ /^'"${JOB_TAG}"'_star_/ && $3=="EXIT" && $4 !~ /TERM_OWNER/{print "  "$1" "$2" ("$4")"}')
[ -n "$f" ] && echo "$f" | head -20 || echo "  none"

echo "disk:"
df -h "$BAM_OUT" 2>/dev/null | awk 'NR==2{print "  BAM_OUT: "$4" free ("$5" used)"}'
df -h "$SCRATCH" 2>/dev/null | awk 'NR==2{print "  SCRATCH: "$4" free ("$5" used)"}'

[ -f "$PIPELINE_ROOT/PIPELINE_COMPLETE.txt" ] && echo ">>> COMPLETE -- see $PIPELINE_ROOT/PIPELINE_COMPLETE.txt"
[ -f "$PIPELINE_ROOT/PIPELINE_STALLED.txt" ]  && echo ">>> STALLED  -- see $PIPELINE_ROOT/PIPELINE_STALLED.txt"
