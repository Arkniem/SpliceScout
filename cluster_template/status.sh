#!/usr/bin/env bash
# status.sh — one-shot progress report (optional; the pipeline self-monitors).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib.sh"
set -u
cd "$STUDIES_DIR" || { echo "STUDIES_DIR not found: $STUDIES_DIR"; exit 1; }
shopt -s nullglob

echo "===== ${JOB_TAG} pipeline @ $(date) ====="
exp=0; dn=0; inc=""
for S in */; do
  [ -f "$S/SraAccList.txt" ] || continue
  a=$(sra_count_nonblank "$S/SraAccList.txt"); g=$(sra_done_count "$STUDIES_DIR/$(basename "$S")")
  exp=$((exp+a)); dn=$((dn+g))
  [ "$g" -lt "$a" ] && inc="$inc  $(printf '%-22s %s/%s' "$(basename "$S")" "$g" "$a")"$'\n'
done
echo "converted: $dn / $exp runs"

echo "--- live jobs (this pipeline) ---"
bjobs -noheader -o "job_name stat" 2>/dev/null | grep "^${JOB_TAG}_" | awk '{print $2}' | sort | uniq -c | sed 's/^/  /'
echo "  conversions RUNNING: $(bjobs -noheader -o 'job_name stat' 2>/dev/null | grep "^${JOB_TAG}_fqd_" | grep RUN | wc -l)"
mapfile -t SL < <(bjobs -noheader -o "stat slots" -u "$USER" 2>/dev/null | awk '$1=="RUN"{print $2}')
t=0; for s in ${SL[@]+"${SL[@]}"}; do t=$((t+s)); done; echo "  my RUN slots in use: $t"   # +-guard: empty array under set -u

echo "--- REAL conversion failures (EXIT, excluding owner-cancellations) ---"
f=$(bjobs -a -noheader -o "jobid job_name stat exit_reason delimiter='|'" 2>/dev/null \
     | awk -F'|' '$2 ~ /^'"${JOB_TAG}"'_fqd_/ && $3=="EXIT" && $4 !~ /TERM_OWNER/{print "  "$1" "$2" ("$4")"}')
[ -n "$f" ] && echo "$f" | head -20 || echo "  none"

echo "--- disk ---"; df -h "$PIPELINE_ROOT" 2>/dev/null | awk 'NR==2{print "  "$4" free ("$5" used)"}'
echo "--- incomplete studies (converted/expected) ---"
[ -n "$inc" ] && printf '%s' "$inc" || echo "  (none — all complete)"

[ -f "$PIPELINE_ROOT/PIPELINE_COMPLETE.txt" ] && echo ">>> PIPELINE COMPLETE — see $PIPELINE_ROOT/PIPELINE_COMPLETE.txt"
[ -f "$PIPELINE_ROOT/PIPELINE_STALLED.txt" ]  && echo ">>> watchdog STOPPED (stalled) — see $PIPELINE_ROOT/PIPELINE_STALLED.txt"
