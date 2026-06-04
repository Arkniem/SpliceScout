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
    gz=("$sdir/$acc"*.fastq.gz)
    if [ ${#gz[@]} -eq 0 ] && [ ! -e "$sdir/$acc.sra" ]; then missing+=("$acc"); fi
  done < "$S/SraAccList.txt"
  [ ${#missing[@]} -eq 0 ] && continue

  printf '%s\n' "${missing[@]}" > "$sdir/SraAccList_missing.txt"
  PF=$(sra_submit_prefetch        "$sdir" "SraAccList_missing.txt")
  CS=$(sra_submit_convert_study   "$sdir" "SraAccList_missing.txt" "$PF")
  echo "$name: re-fetch ${#missing[@]} missing  prefetch=$PF convert=$CS"
  total=$((total+${#missing[@]}))
done
echo "=== fetch_missing: queued $total accession(s) ==="
