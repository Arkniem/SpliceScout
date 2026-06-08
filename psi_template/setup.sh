#!/usr/bin/env bash
# setup.sh -- preflight the AltAnalyze (PSI) stage + resolve the species database.
# Advisory (always exits 0): run_psi_job.sh re-validates the hard requirements. The one ACTION here is
# symlinking an external ALTANALYZE_DB into $ALTANALYZE_HOME/AltDatabase when needed.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
set -u; shopt -s nullglob

echo "== AltAnalyze splicing (PSI) stage -- setup =="
mkdir -p "$PIPELINE_ROOT" "$LOG_DIR" "$PSI_OUT" 2>/dev/null || true

# --- BED input ---
if [ -d "$BED_INPUT_DIR" ]; then
  beds=("$BED_INPUT_DIR"/*__junction.bed); n=${#beds[@]}
  echo "  BED_INPUT_DIR : $BED_INPUT_DIR  ($n junction BEDs)"
  [ "$n" -eq 0 ] && echo "    WARNING: no *__junction.bed here yet (BED stage may not be finished)"
else
  echo "  ERROR: BED_INPUT_DIR not found: $BED_INPUT_DIR"
fi

# --- AltAnalyze program ---
if [ -s "$ALTANALYZE_HOME/AltAnalyze.py" ]; then
  echo "  ALTANALYZE_HOME: $ALTANALYZE_HOME"
else
  echo "  ERROR: AltAnalyze.py not found at $ALTANALYZE_HOME"
fi

# --- species database: AltAnalyze looks for AltDatabase beside AltAnalyze.py ---
DBROOT="$ALTANALYZE_HOME/AltDatabase"
_dbfiles=("$DBROOT"/*)
if [ -d "$DBROOT" ] && [ "${#_dbfiles[@]}" -gt 0 ]; then
  echo "  AltDatabase   : $DBROOT"
elif [ -n "$ALTANALYZE_DB" ] && [ -d "$ALTANALYZE_DB" ] && [ "$ALTANALYZE_DB" != "$DBROOT" ]; then
  if ln -s "$ALTANALYZE_DB" "$DBROOT" 2>/dev/null; then
    echo "  AltDatabase   : symlinked $DBROOT -> $ALTANALYZE_DB"
  else
    echo "  WARNING: no AltDatabase at $DBROOT and could not symlink ALTANALYZE_DB=$ALTANALYZE_DB"
    echo "           ($ALTANALYZE_HOME may be read-only; provide a writable ALTANALYZE_HOME or a co-located DB)"
  fi
else
  echo "  WARNING: no AltDatabase at $DBROOT and no usable ALTANALYZE_DB override -- AltAnalyze will fail"
fi

# --- comparison plan (groups are built later by build_groups.sh) ---
if [ -s "$SAMPLE_GROUPS" ]; then
  ng=0; while IFS= read -r _l; do case "$_l" in (*[![:space:]]*) ng=$((ng+1)) ;; esac; done < "$SAMPLE_GROUPS"
  echo "  sample_groups : $SAMPLE_GROUPS  ($ng mapped samples; build_groups.sh intersects with present BEDs)"
else
  echo "  sample_groups : (none shipped) -> groupless PSI table"
fi

echo "  SPECIES       : $SPECIES  (organism $ORGANISM)"
echo "  PSI_OUT       : $PSI_OUT"
echo "  setup done (advisory; run_psi_job.sh enforces the hard requirements)"
exit 0
