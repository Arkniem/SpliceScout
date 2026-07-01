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

# CURATED bedDir: AltAnalyze merges files by sample via the `__<suffix>.bed` double-underscore convention --
# RNASeq.py importBEDFile strips BOTH `__junction.bed` AND `__intronJunction.bed` to the SAME `<sample>.bed`,
# so the two beds for one sample become ONE column, NOT duplicates. We must include BOTH:
#   *__junction.bed        -- exon-exon junctions
#   *__intronJunction.bed  -- intron-retention junctions (e.g. ENSG..:I13.1-E14.1). REQUIRED: omitting it makes
#                             EVERY intron-retention event silently vanish from the PSI table (the file the
#                             annotated `I<n>.<m>` reads live in -- they are NOT in __junction.bed).
# We still EXCLUDE *__exon.bed: an 8-sample A549 smoke left junction PSI byte-identical with/without it
# (89616/89616 cells) while ~doubling import -- it only adds exon-coverage events, not these IR junctions.
# Symlink into a private dir so the quarantine preflight moves SYMLINKS, never the real BED under STAR_beds.
JXN_DIR="$PIPELINE_ROOT/junction_beds"
mkdir -p "$JXN_DIR" || { echo "[psi] cannot mkdir $JXN_DIR" >&2; exit 1; }
find "$JXN_DIR" -maxdepth 1 -name '*.bed' -type l -delete 2>/dev/null || true   # refresh stale links
_nj=0; _ni=0
for _j in "$BED_INPUT_DIR"/*__junction.bed; do
  [ -e "$_j" ] || continue
  ln -sf "$_j" "$JXN_DIR/$(basename "$_j")" && _nj=$((_nj+1))
done
for _i in "$BED_INPUT_DIR"/*__intronJunction.bed; do          # intron-retention junctions -- AA merges into same sample
  [ -e "$_i" ] || continue
  ln -sf "$_i" "$JXN_DIR/$(basename "$_i")" && _ni=$((_ni+1))
done
[ "$_nj" -gt 0 ] || { echo "[psi] no *__junction.bed under $BED_INPUT_DIR" >&2; exit 1; }
echo "[psi] bedDir: $JXN_DIR ($_nj junction + $_ni intronJunction BEDs; exon excluded)"
BED_INPUT_DIR="$JXN_DIR"; export BED_INPUT_DIR PSI_BEDDIR="$JXN_DIR"   # all downstream + build_groups use this

# PRE-FLIGHT: quarantine any truncated/corrupt BED so AltAnalyze can't wedge on it (its bad-line handler is
# broken). If anything was dropped, rebuild groups on the clean set so groups.txt has no dangling samples.
_q="$(psi_check_beds)"
if [ "${_q:-0}" -gt 0 ] 2>/dev/null; then
  echo "[psi] preflight dropped $_q truncated sample(s) -> rebuilding groups on the clean set"
  # LOG the rebuild instead of swallowing it (`|| true` hid a build_groups failure -> stale groups.txt that
  # then silently mis-grouped or groupless-failed the PSI run with no trace).
  if ! bash "$SCRIPTS_DIR/build_groups.sh" >"$LOG_DIR/build_groups.out" 2>&1; then
    echo "[psi] WARNING: build_groups.sh FAILED after preflight quarantine -- groups.txt may be stale (see $LOG_DIR/build_groups.out)" >&2
  fi
fi

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
  # AltAnalyze's RNASeq workflow is NOT groupless-capable -- with no groups.<expname>.txt/comps it exits at
  # the splicing step ("No groups or comps files found ... exiting"). Fail loudly so the watchdog STALLS with
  # a clear cause instead of burning resubmits on a doomed run. (SpliceScout always assigns groups upstream.)
  echo "[psi] ERROR: no groups/comps at $GROUPS_FILE -- AltAnalyze cannot run groupless. Ship a sample_groups.tsv." >&2
  exit 1
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
