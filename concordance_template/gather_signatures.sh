#!/usr/bin/env bash
# =============================================================================
# gather_signatures.sh -- collect the per-drug PSI signatures the PSI stage produced into a clean
# "ref" directory (DRUG_SIG_DIR) for the concordance scorer. The scorer reads EVERY PSI.*.txt in the
# ref dir, so we copy ONLY the differential PSI.<drug>_vs_<control>.txt tables (not the per-sample
# master PSI table, not the event_summary). Idempotent; echoes the count gathered.
# Pure bash + nullglob; NEVER `grep -c` (empty on the compute nodes).
# =============================================================================
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
set -u; shopt -s nullglob

mkdir -p "$DRUG_SIG_DIR" 2>/dev/null || true

if [ ! -d "$PSI_EVENTS_DIR" ]; then
  echo "[concord] PSI events dir not found: $PSI_EVENTS_DIR" >&2
  echo 0; exit 0
fi

# Refresh: drop any stale scorer outputs that may be sitting in the ref dir from a prior run, so the
# scorer never re-reads them as "signatures" (they don't match PSI.*, but keep the dir pristine anyway).
rm -f "$DRUG_SIG_DIR"/concordance.txt "$DRUG_SIG_DIR"/overlaps-*-direction.txt 2>/dev/null || true

n=0
for f in "$PSI_EVENTS_DIR"/PSI.*_vs_*.txt; do
  [ -e "$f" ] || continue
  cp -p "$f" "$DRUG_SIG_DIR"/ 2>/dev/null && n=$((n+1))
done
# The PSI stage's post-completion compression GZIPS these tables (any >= 1MB) -> ALSO accept
# PSI.*_vs_*.txt.gz, decompressing each into the ref dir as plain .txt (the scorer reads plain text).
# Skip one already gathered uncompressed above. WITHOUT this the concordance gathers 0 and the watchdog
# waits forever for "PSI" output that is actually present but compressed (hit LIVE on MDS_L 2026-06-24).
for g in "$PSI_EVENTS_DIR"/PSI.*_vs_*.txt.gz; do
  [ -e "$g" ] || continue
  b="$(basename "${g%.gz}")"
  [ -e "$DRUG_SIG_DIR/$b" ] && continue
  gunzip -c "$g" > "$DRUG_SIG_DIR/$b" 2>/dev/null && n=$((n+1))
done
echo "[concord] gathered $n drug signature(s) from $PSI_EVENTS_DIR -> $DRUG_SIG_DIR" >&2
echo "$n"
