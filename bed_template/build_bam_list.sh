#!/usr/bin/env bash
# build_bam_list.sh -- generate $BAM_LIST (one row per *.bam) from $BAM_INPUT_DIR.
# BUILD-ONCE: if the list already exists it is NOT rebuilt, so the watchdog's
# denominator can never drift mid-run. Delete $BAM_LIST to force a rebuild (e.g.
# after STAR publishes more BAMs).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_bed.sh"   # for bed_expected_count (pure-bash row count; grep -c is unreliable on compute nodes)
set -u; shopt -s nullglob

mkdir -p "$PIPELINE_ROOT" "$LOG_DIR"

if [ -s "$BAM_LIST" ]; then
  n=$(bed_expected_count)
  echo "BAM list already exists ($n rows): $BAM_LIST"
  echo "  (delete it to force a rebuild)"
  exit 0
fi

# Scan TOP-LEVEL *.bam (not the bed/ subdir). label = basename without .bam.
{
  for b in "$BAM_INPUT_DIR"/*.bam; do
    [ -e "$b" ] || continue
    label="$(basename "$b" .bam)"
    [ -n "$label" ] || continue
    printf '%s\n' "$label"
  done | sort -u
} > "$BAM_LIST.tmp"
mv -f "$BAM_LIST.tmp" "$BAM_LIST"     # atomic publish (never a half-written list)

n=$(bed_expected_count)
if [ "$n" -eq 0 ]; then
  echo "ERROR: 0 *.bam found under $BAM_INPUT_DIR" >&2
  exit 1
fi
echo "built $n-BAM list -> $BAM_LIST"
