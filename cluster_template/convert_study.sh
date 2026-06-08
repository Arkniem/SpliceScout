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

# T4.1: nothing downloaded? (prefetch can fail yet this job still runs -- it's gated on ended(), not
# done().) Early-return with a breadcrumb instead of crashing on a bare empty-array expansion under set -u,
# so the download watchdog's own STALL logic -- not a cryptic 'ACCS[@]: unbound variable' -- decides next.
if [ ${#ACCS[@]} -eq 0 ]; then
  echo "convert_study: $(basename "$SDIR") nothing to convert (no .sra on disk -- prefetch may have downloaded nothing)"
  exit 0
fi

LIVE="$(sra_live_names)"
n=0
for acc in ${ACCS[@]+"${ACCS[@]}"}; do
  # 1) flatten this accession's download into the study dir
  for f in "$acc"/*.sra "$acc"/*.sralite "$acc".sralite; do
    [ -e "$f" ] && mv -n "$f" .
  done
  [ -e "$acc.sralite" ] && mv -n "$acc.sralite" "$acc.sra"
  # 2) skip if already converted. T4.2: ANCHOR the glob to match sra_done_count EXACTLY -- an unanchored
  #    "$acc"*.fastq.gz prefix-matches a sibling (SRR123 vs SRR1234.fastq.gz) and would skip SRR123 forever
  #    while the counter (correctly anchored) never credits it -> a permanent stall + a missing FASTQ.
  if compgen -G "$acc.fastq.gz" >/dev/null 2>&1 || compgen -G "${acc}_[0-9].fastq.gz" >/dev/null 2>&1; then continue; fi
  # 3) skip if no .sra present (nothing downloaded for it)
  [ -e "$acc.sra" ] || continue
  # 4) skip if a conversion job for it is already live (idempotent)
  sra_has_live "${JOB_TAG}_fqd_${acc}" "$LIVE" && continue
  sra_submit_conversion "$acc" "$SDIR" >/dev/null
  n=$((n+1))
done
echo "convert_study: $(basename "$SDIR") submitted $n conversion job(s)"
