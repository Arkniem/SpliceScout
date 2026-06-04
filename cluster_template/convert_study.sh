#!/usr/bin/env bash
# convert_study.sh — LSF job body (gated on a study's prefetch via ended()).
# Flattens downloaded .sra/.sralite up into the study dir, normalizes .sralite
# -> .sra, then submits one conversion job per accession that still needs one.
# Args: $1 = study dir   $2 = list file (optional; relative to study dir).
#   no list  -> handle ALL accessions found on disk in the study dir
#   list set -> handle ONLY the accessions in that list (used by the re-fetch path)
# Idempotent: skips accessions that already have a .fastq.gz or a live conversion job.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib.sh"
set -u
SDIR="$1"
LIST="${2:-}"
cd "$SDIR" || { echo "convert_study: cannot cd $SDIR" >&2; exit 1; }
shopt -s nullglob

# Determine which accessions to act on.
ACCS=()
if [ -n "$LIST" ] && [ -f "$LIST" ]; then
  while read -r a; do a=$(echo "$a" | tr -d '\r'); [ -n "$a" ] && ACCS+=("$a"); done < "$LIST"
else
  # all accessions present on disk (flattened or in per-accession subdirs)
  for f in *.sra */*.sra *.sralite */*.sralite; do
    [ -e "$f" ] || continue
    b=$(basename "$f"); ACCS+=("${b%.sra*}")
  done
  # de-duplicate
  if [ ${#ACCS[@]} -gt 0 ]; then
    mapfile -t ACCS < <(printf '%s\n' "${ACCS[@]}" | sort -u)
  fi
fi

LIVE="$(sra_live_names)"
n=0
for acc in "${ACCS[@]}"; do
  # 1) flatten this accession's download into the study dir
  for f in "$acc"/*.sra "$acc"/*.sralite "$acc".sralite; do
    [ -e "$f" ] && mv -n "$f" .
  done
  [ -e "$acc.sralite" ] && mv -n "$acc.sralite" "$acc.sra"
  # 2) skip if already converted
  gz=("$acc"*.fastq.gz); [ ${#gz[@]} -gt 0 ] && continue
  # 3) skip if no .sra present (nothing downloaded for it)
  [ -e "$acc.sra" ] || continue
  # 4) skip if a conversion job for it is already live (idempotent)
  sra_has_live "${JOB_TAG}_fqd_${acc}" "$LIVE" && continue
  sra_submit_conversion "$acc" "$SDIR" >/dev/null
  n=$((n+1))
done
echo "convert_study: $(basename "$SDIR") submitted $n conversion job(s)"
