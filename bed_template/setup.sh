#!/usr/bin/env bash
# setup.sh -- one-time preflight. Safe to re-run. Read-only except mkdir of the
# state dirs. Exits non-zero if anything required is wrong.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_bed.sh"
set -u; shopt -s nullglob

ok=1
echo "== BAM->BED (AltAnalyze junction/exon) stage -- setup =="

# --- BAM input ---
if [ -d "$BAM_INPUT_DIR" ]; then
  nbam=$(ls "$BAM_INPUT_DIR"/*.bam 2>/dev/null | wc -l)   # wc -l, not grep -c (empty on compute nodes)
  echo "  BAM_INPUT_DIR   : $BAM_INPUT_DIR  ($nbam BAMs)"
  [ "$nbam" -eq 0 ] && { echo "    WARNING: no *.bam found here yet"; ok=0; }
else
  echo "  ERROR: BAM_INPUT_DIR not found: $BAM_INPUT_DIR"; ok=0
fi

# --- vendored AltAnalyze toolkit (all-in-one; no cluster install needed) ---
JB="$ALTANALYZE_DIR/import_scripts/BAMtoJunctionBED.py"
EB="$ALTANALYZE_DIR/import_scripts/BAMtoExonBED.py"
if [ -s "$JB" ] && [ -s "$EB" ]; then
  echo "  ALTANALYZE_DIR  : $ALTANALYZE_DIR  (vendored BAMto*BED.py present)"
else
  echo "  ERROR: vendored AltAnalyze scripts missing under $ALTANALYZE_DIR/import_scripts/"; ok=0
fi

# --- exon reference (resolved from ORGANISM -> SPECIES) ---
echo "  ORGANISM/SPECIES: '$ORGANISM' -> $SPECIES"
if [ -s "$EXON_REF" ]; then
  echo "  EXON_REF        : $EXON_REF"
else
  echo "  ERROR: exon reference missing/empty: $EXON_REF"
  echo "         (check SPECIES='$SPECIES' has refs/<SP>/<SP>_Ensembl_exon.txt vendored)"; ok=0
fi

# --- tools ---
bed_load_modules
for t in samtools bsub bjobs python; do
  if command -v "$t" >/dev/null 2>&1; then echo "  OK   $t -> $(command -v "$t")"
  else echo "  MISSING $t"; ok=0; fi
done
# python MUST be 2.7 AND pysam importable (the AltAnalyze import scripts are py2 + need pysam)
if command -v python >/dev/null 2>&1; then
  pv="$(python --version 2>&1)"
  case "$pv" in
    *"Python 2.7"*) echo "  OK   $pv" ;;
    *) echo "  WARNING: '$pv' -- the AltAnalyze import scripts need Python 2.7 (set PYTHON_MODULE)"; ok=0 ;;
  esac
  if python -c "import pysam" >/dev/null 2>&1; then echo "  OK   python can import pysam"
  else echo "  ERROR: this python cannot 'import pysam' (the BAM reader) -- check PYTHON_MODULE='$PYTHON_MODULE'"; ok=0; fi
fi

# --- state dirs + writability (BEDs land beside the BAMs) ---
if mkdir -p "$PIPELINE_ROOT" "$LOG_DIR" 2>/dev/null; then
  echo "  PIPELINE_ROOT   : $PIPELINE_ROOT  ($(df -h "$PIPELINE_ROOT" 2>/dev/null | awk 'NR==2{print $4}') free)"
else
  echo "  ERROR: cannot create state dir: $PIPELINE_ROOT"; ok=0
fi
wt="$BAM_INPUT_DIR/.bedwtest.$$"
if mkdir -p "$wt" 2>/dev/null; then rmdir "$wt" 2>/dev/null; echo "  OK   BAM_INPUT_DIR is writable (BEDs land beside the BAMs)"
else echo "  ERROR: BAM_INPUT_DIR not writable -- BEDs cannot be written: $BAM_INPUT_DIR"; ok=0; fi

# --- slot cap (informational) ---
command -v busers >/dev/null 2>&1 && busers "$USER" 2>/dev/null | sed 's/^/  busers: /'

if [ "$ok" -eq 1 ]; then echo "== setup OK =="; exit 0
else echo "== setup found problems (see above) -- fix config.sh =="; exit 1; fi
