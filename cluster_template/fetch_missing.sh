#!/usr/bin/env bash
# fetch_missing.sh — targeted recovery. For each study, finds accessions that are
# MISSING (in SraAccList.txt but have neither a .fastq.gz nor an .sra on disk),
# writes a per-study SraAccList_missing.txt, and prefetches+converts ONLY those.
# Never re-downloads runs already on disk and never double-submits (skips studies
# that already have a live re-fetch). Safe to run repeatedly (the watchdog calls it).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib.sh"
set -u
sra_require_bsub   # must run on the LSF submit host (not a login node)
cd "$STUDIES_DIR" || { echo "fetch_missing: STUDIES_DIR not found" >&2; exit 1; }
shopt -s nullglob

LIVE="$(sra_live_names)"
total=0
for S in */; do
  name=$(basename "$S")
  [ -f "$S/SraAccList.txt" ] || continue
  sdir="$STUDIES_DIR/$name"
  # already re-fetching this study? skip.
  sra_has_live "${JOB_TAG}_pf_${name}"  "$LIVE" && continue
  sra_has_live "${JOB_TAG}_cs_${name}"  "$LIVE" && continue

  missing=()
  while read -r acc; do
    acc=$(echo "$acc" | tr -d '\r'); [ -z "$acc" ] && continue
    sra_is_dropped "$acc" && continue                    # already gave up on this accession
    gz=("$sdir/$acc"*.fastq.gz)
    if [ ${#gz[@]} -eq 0 ] && [ ! -e "$sdir/$acc.sra" ]; then
      # still missing AND this study has no live re-fetch (checked above) = the last download FAILED ->
      # count it, and DROP after MAX_FAILS so an undeliverable accession can't be re-fetched forever.
      _n=$(sra_bump_attempt "$acc")
      if [ "$_n" -gt "${MAX_FAILS:-3}" ]; then
        sra_drop_acc "$acc" "$sdir" download
        echo "  dropped $acc after $((_n - 1)) failed downloads -> logged to dropped_accessions.txt"
        continue
      fi
      missing+=("$acc")
    fi
  done < "$S/SraAccList.txt"
  [ ${#missing[@]} -eq 0 ] && continue

  printf '%s\n' "${missing[@]}" > "$sdir/SraAccList_missing.txt"
  # FAIL-FAST: if the queue is full the helper returns 124 -> STOP this pass (don't hang on each blocked bsub).
  # The final count line still prints; the next watchdog pass resumes the re-fetch where this left off.
  PF=$(sra_submit_prefetch "$sdir" "SraAccList_missing.txt"); _pfrc=$?
  if [ "$_pfrc" -eq 124 ] || [ -z "$PF" ]; then
    echo "  submit blocked (queue full) -> stopping fetch_missing this pass"; break
  fi
  CS=$(sra_submit_convert_study   "$sdir" "SraAccList_missing.txt" "$PF")
  echo "$name: re-fetch ${#missing[@]} missing  prefetch=$PF convert=$CS"
  total=$((total+${#missing[@]}))
done
echo "=== fetch_missing: queued $total accession(s) ==="
