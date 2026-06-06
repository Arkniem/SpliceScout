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

# The NEXT pass is queued at the START of each pass (reschedule-first), so a mid-pass walltime kill
# (e.g. bsub blocked on the LSF pending-job threshold) can NEVER break the self-driving chain. We keep
# the successor's job id so finalize() can cancel it once the pipeline is actually done.
WATCHDOG_NEXT_JID=""
reschedule() {
  local when
  when=$(date -d "+$WATCHDOG_INTERVAL_MIN min" '+%Y:%m:%d:%H:%M' 2>/dev/null) ||
  when=$(date -v+"${WATCHDOG_INTERVAL_MIN}"M '+%Y:%m:%d:%H:%M' 2>/dev/null)   # BSD fallback
  sra_qopt
  WATCHDOG_NEXT_JID=$(bsub -L /bin/bash -n 1 -M 1000 -W 20 -b "$when" -J "${JOB_TAG}_watchdog" \
       -o "$PIPELINE_ROOT/watchdog.out" -e "$PIPELINE_ROOT/watchdog.err" \
       ${QOPT[@]+"${QOPT[@]}"} "$SCRIPTS_DIR/watchdog.sh" 2>/dev/null | sra_jobid)
  say "next pass scheduled for $when (job ${WATCHDOG_NEXT_JID:-?})"
}

# Delete transient clutter after a SUCCESSFUL run. KEEPS .fastq.gz, SraAccList.txt,
# the PIPELINE_COMPLETE.txt report, all scripts, and watchdog.log. Uses find so an
# empty match is harmless (never touches outputs/inputs/scripts).
cleanup_run() {
  [ "${CLEANUP_ON_COMPLETE:-yes}" = "yes" ] || return 0
  local S
  for S in "$STUDIES_DIR"/*/; do
    [ -d "$S" ] || continue
    find "$S" -maxdepth 1 -type f \( -name '*.err' -o -name '*.out' -o -name '*.lsf' \
         -o -name '*.sra.vdbcache' -o -name 'SraAccList_missing.txt' \) -delete 2>/dev/null
    find "$S" -mindepth 1 -maxdepth 1 -type d -empty -delete 2>/dev/null   # empty acc subdirs
  done
  rm -f "$PIPELINE_ROOT/watchdog.out" "$PIPELINE_ROOT/watchdog.err" \
        "$PIPELINE_ROOT/.watchdog.state" "$PIPELINE_ROOT/.watchdog.state.stall" \
        "$PIPELINE_ROOT/job_map.tsv" 2>/dev/null
  say "cleanup: removed transient logs/.lsf/temp files (CLEANUP_ON_COMPLETE=yes)"

  # Optionally remove the pipeline's OWN scripts too, leaving a data-only folder.
  # Guard: only if the scripts live inside PIPELINE_ROOT (never a shared tools dir).
  # Safe to delete the running watchdog.sh — bash keeps executing the open file.
  if [ "${CLEANUP_SCRIPTS_ON_COMPLETE:-yes}" = "yes" ] && [ -n "${SCRIPTS_DIR:-}" ]; then
    case "$SCRIPTS_DIR" in
      "$PIPELINE_ROOT"|"$PIPELINE_ROOT"/*)
        rm -f "$SCRIPTS_DIR"/config.sh "$SCRIPTS_DIR"/lib.sh "$SCRIPTS_DIR"/run_pipeline.sh \
              "$SCRIPTS_DIR"/setup.sh "$SCRIPTS_DIR"/run_all.sh "$SCRIPTS_DIR"/prefetch_job.sh \
              "$SCRIPTS_DIR"/fasterqdump_job.sh "$SCRIPTS_DIR"/convert_study.sh \
              "$SCRIPTS_DIR"/fetch_missing.sh "$SCRIPTS_DIR"/status.sh "$SCRIPTS_DIR"/DOWNLOAD_PIPELINE_GUIDE.md \
              "$SCRIPTS_DIR"/watchdog.sh 2>/dev/null
        say "cleanup: removed pipeline scripts from $SCRIPTS_DIR (re-copy the template to re-run)" ;;
      *)
        say "cleanup: kept scripts ($SCRIPTS_DIR is outside PIPELINE_ROOT - shared install)" ;;
    esac
  fi
}

finalize() {  # $1 = COMPLETE | STALLED
  local status="$1"
  # Done -> cancel the successor queued at pass start (it would only no-op, or fail on a cleaned-up
  # script). Best-effort.
  [ -n "${WATCHDOG_NEXT_JID:-}" ] && bkill "$WATCHDOG_NEXT_JID" >/dev/null 2>&1
  local rep="$PIPELINE_ROOT/PIPELINE_${status}.txt"
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
  # SpliceScout STAR auto-chain: if a STAR launcher is bundled here, kick it NOW so STAR starts the
  # moment the download finishes (COMPLETE or STALLED) instead of waiting for the launcher's own poll
  # cycle. Harmless no-op for plain download runs (no star/ dir). The launcher keeps its self-poll too.
  if [ -f "$PIPELINE_ROOT/star/star_launch.sh" ]; then
    sra_qopt
    bsub -L /bin/bash -n 1 -M 1000 -W 120 -J "${JOB_TAG}_star_launch" \
         -o "$PIPELINE_ROOT/star/launch.out" -e "$PIPELINE_ROOT/star/launch.err" \
         ${QOPT[@]+"${QOPT[@]}"} "$PIPELINE_ROOT/star/star_launch.sh" >/dev/null 2>&1
    say "kicked STAR launcher -> $PIPELINE_ROOT/star/star_launch.sh"
  fi
  [ "$status" = "COMPLETE" ] && cleanup_run   # tidy up only on success, never on STALLED
  say "FINALIZED ($status) -> $rep  (watchdog stopping)"
}

say "=== watchdog pass start ==="
# If a prior pass already finalized, this (already-queued) successor just stops — no work, no reschedule.
if [ -f "$PIPELINE_ROOT/PIPELINE_COMPLETE.txt" ] || [ -f "$PIPELINE_ROOT/PIPELINE_STALLED.txt" ]; then
  say "already finalized -> stop"; exit 0
fi
reschedule                       # queue the NEXT pass FIRST (survives a mid-pass walltime kill)
LIVE="$(sra_live_names)"

# 1) resubmit orphaned / failed conversions
resub=0
for S in */; do
  sdir="$STUDIES_DIR/$(basename "$S")"
  for sra in "$sdir"/*.sra; do
    [ -e "$sra" ] || continue
    acc=$(basename "$sra" .sra)
    # already converted? DROP the now-redundant source .sra. (The per-acc converter deletes it on
    # success, but the bulk convert_study path can leave it behind -> stranded .sra keep nsra>0 forever
    # -> the "zero .sra left" completion gate never passes -> a FALSE STALL despite all data present.)
    if compgen -G "$sdir/$acc.fastq.gz" >/dev/null 2>&1 || compgen -G "$sdir/${acc}_[0-9].fastq.gz" >/dev/null 2>&1; then
      rm -f "$sra" "$sdir/${acc}.sra.vdbcache"; continue
    fi
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
# count only WORK jobs (pf/cs/fqd/…), NOT the watchdog itself, or nlive never hits 0
LIVE2="$(sra_live_names)"; nlive=$(printf '%s\n' "$LIVE2" | grep -cE "^${JOB_TAG}_(pf|cs|fqd|pfm|cm)_")
say "progress: $total_done/$total_exp converted, $nlive live jobs"

# 4) decide: complete / stalled / keep going
# Robust completion: require the count AND zero .sra left AND zero live jobs, so a
# transient over-count (e.g. an NFS listing glitch) can never finalize prematurely.
_sra=("$STUDIES_DIR"/*/*.sra "$STUDIES_DIR"/*/*/*.sra); nsra=${#_sra[@]}  # nullglob-safe
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
# Safety net: a successor is normally queued at pass start; reschedule now if that bsub didn't take.
[ -n "${WATCHDOG_NEXT_JID:-}" ] || reschedule
say "pass end -> next pass queued (job ${WATCHDOG_NEXT_JID:-?})"
