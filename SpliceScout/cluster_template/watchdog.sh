#!/usr/bin/env bash
# watchdog.sh — the self-driving controller. Each pass it:
#   1. resubmits orphaned/failed conversions (.sra on disk, no .fastq.gz, no live job)
#   2. re-fetches any still-missing accessions (idempotent fetch_missing)
#   3. accounts progress; if everything is converted -> writes PIPELINE_COMPLETE.txt
#      and STOPS. If it has made no progress for several idle passes (accessions
#      that SRA can't deliver) -> writes PIPELINE_STALLED.txt and STOPS.
#   4. otherwise re-submits ITSELF to run again in WATCHDOG_INTERVAL_MIN minutes.
# Start it once (run_pipeline.sh does). After that it needs no human attention.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib.sh"
set -u
cd "$STUDIES_DIR" || exit 1
LOG="$PIPELINE_ROOT/watchdog.log"
STATE="$PIPELINE_ROOT/.watchdog.state"
ts() { date '+%Y-%m-%d %H:%M:%S'; }
say() { echo "[$(ts)] $*" >> "$LOG"; }
shopt -s nullglob

reschedule() {
  local when
  when=$(date -d "+$WATCHDOG_INTERVAL_MIN min" '+%Y:%m:%d:%H:%M' 2>/dev/null) ||
  when=$(date -v+"${WATCHDOG_INTERVAL_MIN}"M '+%Y:%m:%d:%H:%M' 2>/dev/null)   # BSD fallback
  sra_qopt
  bsub -L /bin/bash -n 1 -M 1000 -W 20 -b "$when" -J "${JOB_TAG}_watchdog" \
       -o "$PIPELINE_ROOT/watchdog.out" -e "$PIPELINE_ROOT/watchdog.err" \
       ${QOPT[@]+"${QOPT[@]}"} "$SCRIPTS_DIR/watchdog.sh" >/dev/null 2>&1
  say "next pass scheduled for $when"
}

finalize() {  # $1 = COMPLETE | STALLED
  local status="$1" rep="$PIPELINE_ROOT/PIPELINE_${status}.txt"
  {
    echo "Pipeline $status at $(ts)"
    echo "Converted: $total_done / $total_exp runs"
    echo "Dataset size: $(du -sh "$STUDIES_DIR" 2>/dev/null | cut -f1)"
    echo
    echo "Per-study (converted/expected):"
    for S in "$STUDIES_DIR"/*/; do
      [ -f "$S/SraAccList.txt" ] || continue
      local a g; a=$(grep -c . "$S/SraAccList.txt"); g=$(sra_done_count "$S")
      local mark=""; [ "$g" -lt "$a" ] && mark="   <-- INCOMPLETE"
      printf "  %-22s %s/%s%s\n" "$(basename "$S")" "$g" "$a" "$mark"
    done
    if [ "$status" = "STALLED" ]; then
      echo; echo "Accessions with NO .fastq.gz after repeated re-fetch (likely withdrawn/"
      echo "restricted on SRA — verify manually):"
      for S in "$STUDIES_DIR"/*/; do
        [ -f "$S/SraAccList.txt" ] || continue
        while read -r acc; do acc=$(echo "$acc"|tr -d '\r'); [ -z "$acc" ] && continue
          compgen -G "$S$acc.fastq.gz" >/dev/null 2>&1 || compgen -G "$S${acc}_[0-9].fastq.gz" >/dev/null 2>&1 || echo "  $(basename "$S")  $acc"
        done < "$S/SraAccList.txt"
      done
    fi
  } > "$rep"
  say "FINALIZED ($status) -> $rep  (watchdog stopping)"
}

say "=== watchdog pass start ==="
LIVE="$(sra_live_names)"

# 1) resubmit orphaned / failed conversions
resub=0
for S in */; do
  sdir="$STUDIES_DIR/$(basename "$S")"
  for sra in "$sdir"/*.sra; do
    [ -e "$sra" ] || continue
    acc=$(basename "$sra" .sra)
    { compgen -G "$sdir/$acc.fastq.gz" >/dev/null 2>&1 || compgen -G "$sdir/${acc}_[0-9].fastq.gz" >/dev/null 2>&1; } && continue
    sra_has_live "${JOB_TAG}_fqd_${acc}" "$LIVE" && continue
    sra_submit_conversion "$acc" "$sdir" >/dev/null && resub=$((resub+1))
  done
done
[ "$resub" -gt 0 ] && say "resubmitted $resub orphaned/failed conversion(s)"

# 2) re-fetch still-missing accessions (idempotent)
miss_out=$(bash "$SCRIPTS_DIR/fetch_missing.sh" 2>/dev/null | tail -1)
say "fetch_missing -> ${miss_out:-none}"

# 3) progress accounting
total_done=0; total_exp=0
for S in */; do
  [ -f "$S/SraAccList.txt" ] || continue
  total_exp=$((total_exp + $(grep -c . "$S/SraAccList.txt")))
  total_done=$((total_done + $(sra_done_count "$STUDIES_DIR/$(basename "$S")")))
done
LIVE2="$(sra_live_names)"; nlive=$(printf '%s\n' "$LIVE2" | grep -c "^${JOB_TAG}_")
say "progress: $total_done/$total_exp converted, $nlive live jobs"

# 4) decide: complete / stalled / keep going
# Robust completion: require the count AND zero .sra left AND zero live jobs, so a
# transient over-count (e.g. an NFS listing glitch) can never finalize prematurely.
nsra=$(ls "$STUDIES_DIR"/*/*.sra 2>/dev/null | wc -l)
if [ "$total_done" -ge "$total_exp" ] && [ "$nsra" -eq 0 ] && [ "$nlive" -eq 0 ]; then
  finalize "COMPLETE"; exit 0
fi

queued_new=0; printf '%s' "$miss_out" | grep -qE 'queued [1-9]' && queued_new=1
if [ "$nlive" -eq 0 ] && [ "$resub" -eq 0 ] && [ "$queued_new" -eq 0 ]; then
  prev=$(cat "$STATE" 2>/dev/null || echo -1)
  stall=0; [ "$total_done" = "$prev" ] && stall=$(( $(cat "$STATE.stall" 2>/dev/null || echo 0) + 1 ))
  echo "$total_done" > "$STATE"; echo "$stall" > "$STATE.stall"
  if [ "$stall" -ge 2 ]; then finalize "STALLED"; exit 0; fi
else
  echo "$total_done" > "$STATE"; echo 0 > "$STATE.stall"
fi
reschedule
