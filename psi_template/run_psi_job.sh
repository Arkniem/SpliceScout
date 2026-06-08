#!/usr/bin/env bash
# =============================================================================
# run_psi_job.sh -- the SINGLE AltAnalyze splicing (PSI) job (LSF job body).
# Runs ONE AltAnalyze pass over the whole BED dir -> per-sample PSI table, plus a
# differential (dPSI) comparison when groups.txt/comps.txt exist. Sources config.sh.
#   * idempotent: skips if the PSI table is already present (psi_done)
#   * verifies the PSI table landed (else exits non-zero so the watchdog resubmits)
# =============================================================================
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_psi.sh"
set -u; shopt -s nullglob

psi_load_modules
command -v python >/dev/null 2>&1 || { echo "[psi] python (2.7) not on PATH" >&2; exit 1; }
mkdir -p "$PIPELINE_ROOT" "$LOG_DIR" "$PSI_OUT" || { echo "[psi] cannot mkdir state/output dirs" >&2; exit 1; }

AA="$ALTANALYZE_HOME/AltAnalyze.py"
[ -s "$AA" ] || { echo "[psi] AltAnalyze.py not found at $AA (deployer should have resolved/uploaded it)" >&2; exit 1; }

# need at least one junction BED to analyze
beds=("$BED_INPUT_DIR"/*__junction.bed)
[ "${#beds[@]}" -gt 0 ] || { echo "[psi] no *__junction.bed under $BED_INPUT_DIR" >&2; exit 1; }

# idempotent: PSI table already there -> skip
if psi_done; then echo "[psi] PSI output already present -> skip"; exit 0; fi

# differential-comparison args (build_groups.sh wrote these only if a usable 2-group split exists)
GOPT=()
GO_FLAG=(--runGOElite no)
if [ -s "$GROUPS_FILE" ] && [ -s "$COMPS_FILE" ]; then
  GOPT=(--groupdir "$GROUPS_FILE" --compdir "$COMPS_FILE")
  [ "${RUN_GOELITE:-0}" = "1" ] && GO_FLAG=(--runGOElite yes --GEelitefold 1.5 --GEeliteptype rawp)
  echo "[psi] differential comparison: groups=$GROUPS_FILE comps=$COMPS_FILE"
else
  echo "[psi] groupless run (no usable 2-group split) -> per-sample PSI table only"
fi

echo "[psi] AltAnalyze: species=$SPECIES bedDir=$BED_INPUT_DIR out=$PSI_OUT expname=$EXPNAME (${#beds[@]} junction BEDs)"
python "$AA" --species "$SPECIES" --platform RNASeq \
    --bedDir "$BED_INPUT_DIR" --output "$PSI_OUT" --expname "$EXPNAME" \
    --multiProcessing yes \
    ${GOPT[@]+"${GOPT[@]}"} "${GO_FLAG[@]}" \
  || { echo "[psi] AltAnalyze FAILED (rc=$?)" >&2; exit 1; }

if ! psi_done; then
  echo "[psi] AltAnalyze ran but no PSI table under $PSI_OUT/AltResults/AlternativeOutput -- not done (safe to resubmit)" >&2
  exit 1
fi
echo "[psi] complete -> PSI results in $PSI_OUT/AltResults"

# wake the watchdog now (pure accelerator; the timed poll is the fallback)
psi_nudge_watchdog "altanalyze" || true
