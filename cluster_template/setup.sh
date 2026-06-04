#!/usr/bin/env bash
# setup.sh — one-time prep on the LSF submit host. Verifies dirs/tools, initializes
# SRA Toolkit (vdb-config), checks scratch and your slot cap. Safe to re-run.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
set -u
echo "== directories =="
[ -d "$STUDIES_DIR" ] || { echo "  ERROR: STUDIES_DIR not found: $STUDIES_DIR"; echo "  Create it and put one subdir per study, each with a SraAccList.txt."; exit 1; }
ns=$(find "$STUDIES_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)
nl=$(find "$STUDIES_DIR" -mindepth 2 -maxdepth 2 -name SraAccList.txt 2>/dev/null | wc -l)
echo "  $STUDIES_DIR : $ns study dir(s), $nl with SraAccList.txt"
[ "$nl" -eq 0 ] && echo "  WARNING: no SraAccList.txt found — nothing to do yet."

echo "== tools =="
sra_load_modules
for t in prefetch fasterq-dump bsub bjobs; do
  if command -v "$t" >/dev/null 2>&1; then echo "  OK   $t -> $(command -v "$t")"
  else echo "  MISSING $t  (fix SRATOOLKIT_MODULE/ASPERA_MODULE in config.sh, or ensure it's on PATH)"; fi
done
command -v pigz >/dev/null 2>&1 && echo "  OK   pigz (parallel gzip)" || echo "  note: pigz not found; conversions fall back to gzip"

echo "== SRA Toolkit config (vdb-config) =="
if [ ! -f "$HOME/.ncbi/user-settings.mkfg" ]; then
  mkdir -p "$HOME/.ncbi"
  vdb-config --restore-defaults >/dev/null 2>&1
  grep -q '/LIBS/GUID' "$HOME/.ncbi/user-settings.mkfg" 2>/dev/null || \
    echo "/LIBS/GUID = \"$(cat /proc/sys/kernel/random/uuid 2>/dev/null || echo 11111111-2222-3333-4444-555555555555)\"" >> "$HOME/.ncbi/user-settings.mkfg"
  echo "  initialized $HOME/.ncbi/user-settings.mkfg"
else
  echo "  already configured: $HOME/.ncbi/user-settings.mkfg"
fi

echo "== scratch =="
if [ -n "$SCRATCH_DIR" ]; then
  if mkdir -p "$SCRATCH_DIR" 2>/dev/null && [ -w "$SCRATCH_DIR" ]; then
    echo "  OK   $SCRATCH_DIR ($(df -h "$SCRATCH_DIR" 2>/dev/null | awk 'NR==2{print $4}') free)"
  else
    echo "  WARN $SCRATCH_DIR not writable — conversions will process in-place on the archive"
  fi
else
  echo "  SCRATCH_DIR empty -> processing in-place under PIPELINE_ROOT"
fi

echo "== per-user slot cap (informs THREADS choice) =="
busers "$USER" 2>/dev/null | sed 's/^/  /' || echo "  (busers unavailable)"
echo "setup complete."
