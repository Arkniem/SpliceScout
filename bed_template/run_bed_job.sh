#!/usr/bin/env bash
# =============================================================================
# run_bed_job.sh -- AltAnalyze junction+exon BED extraction for ONE BAM (LSF job body).
# Sources config.sh for all settings. Hardened:
#   * idempotent: skips if both BEDs already exist & are non-empty (bed_done)
#   * indexes the BAM only if a .bai is missing (STAR usually already indexed it)
#   * runs the VENDORED AltAnalyze scripts (altanalyze/) -> no AltAnalyze install needed;
#     only the stock python/2.7.5 (pysam) + samtools modules
#   * writes <sample>__junction.bed (+ __exon.bed and/or __intronJunction.bed per BED_MODE) NEXT TO the BAM (tool-forced)
#   * verifies both BEDs landed (else exits non-zero so the watchdog resubmits)
# Usage: run_bed_job.sh <LABEL>     (LABEL = BAM basename without .bam)
# =============================================================================
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_bed.sh"
set -u
: "${BED_OUT_DIR:=$(dirname "$BAM_INPUT_DIR")/STAR_beds}"   # robust if an older deployed config.sh predates BED_OUT_DIR

LABEL="$1"
BAM="$BAM_INPUT_DIR/$LABEL.bam"

bed_load_modules
command -v samtools >/dev/null 2>&1 || { echo "[bed] $LABEL: samtools not on PATH" >&2; exit 1; }
command -v python   >/dev/null 2>&1 || { echo "[bed] $LABEL: python (2.7) not on PATH" >&2; exit 1; }
mkdir -p "$PIPELINE_ROOT" "$LOG_DIR" || { echo "[bed] $LABEL: cannot mkdir state dirs" >&2; exit 1; }

[ -s "$BAM" ]      || { echo "[bed] $LABEL: BAM missing/empty: $BAM" >&2; exit 1; }
[ -s "$EXON_REF" ] || { echo "[bed] $LABEL: exon reference missing: $EXON_REF" >&2; exit 1; }

# idempotent: both BEDs already present & non-empty -> skip
if bed_done "$LABEL"; then echo "[bed] $LABEL: BEDs present -> skip"; exit 0; fi

JB="$ALTANALYZE_DIR/import_scripts/BAMtoJunctionBED.py"
EB="$ALTANALYZE_DIR/import_scripts/BAMtoExonBED.py"
[ -s "$JB" ] && [ -s "$EB" ] || { echo "[bed] $LABEL: vendored AltAnalyze scripts missing under $ALTANALYZE_DIR" >&2; exit 1; }

# 1) index only if missing (STAR already indexes on publish; accept .bam.bai OR .bai)
if [ ! -s "$BAM.bai" ] && [ ! -s "${BAM%.bam}.bai" ]; then
  echo "[bed] $LABEL: samtools index"
  samtools index "$BAM" || { echo "[bed] $LABEL: samtools index FAILED" >&2; exit 1; }
fi

# PYTHONPATH lets the vendored scripts find export/unique (siblings) + the import_scripts package.
export PYTHONPATH="$ALTANALYZE_DIR${PYTHONPATH:+:$PYTHONPATH}"

# toolkit re-check (T1.3): a racing finalize cleanup is now gated on no-live-jobs, but re-verify the
# vendored scripts + exon ref right before use anyway and fail DISTINCTLY (rc=3) if they vanished mid-job.
[ -s "$JB" ] && [ -s "$EB" ] && [ -s "$EXON_REF" ] || {
  echo "[bed] $LABEL: vendored AltAnalyze toolkit/exon-ref vanished mid-job (cleanup race?) -- aborting (rc=3)" >&2; exit 3; }

# 2) junction BED  (species flag is --species)
echo "[bed] $LABEL: BAMtoJunctionBED (species=$SPECIES)"
python "$JB" --i "$BAM" --species "$SPECIES" --r "$EXON_REF" \
  || { echo "[bed] $LABEL: BAMtoJunctionBED FAILED" >&2; exit 1; }

# 3) exon / intron-retention BED via BAMtoExonBED (species flag is --s -- NOT --species).
#    BED_MODE picks the pass(es). chr naming (BAM '1' vs ref 'chr1') is auto-reconciled by
#    the tool, so the vendored chr-prefixed ref is correct in every mode.
#      intron (default): NO flag -> default intronRetentionOnly=True -> <sample>__intronJunction.bed
#      exon            : --intronRetentionOnly False                 -> <sample>__exon.bed
#      both            : exon pass FIRST, then the plain intron pass LAST (both rewrite
#                        __intronJunction.bed; the default-True pass is authoritative -> runs last)
echo "[bed] $LABEL: BAMtoExonBED (species=$SPECIES, mode=${BED_MODE:-intron})"
case "${BED_MODE:-intron}" in
  exon)
    python "$EB" --i "$BAM" --r "$EXON_REF" --s "$SPECIES" --intronRetentionOnly False \
      || { echo "[bed] $LABEL: BAMtoExonBED (exon) FAILED" >&2; exit 1; } ;;
  both)
    python "$EB" --i "$BAM" --r "$EXON_REF" --s "$SPECIES" --intronRetentionOnly False \
      || { echo "[bed] $LABEL: BAMtoExonBED (exon) FAILED" >&2; exit 1; }
    python "$EB" --i "$BAM" --r "$EXON_REF" --s "$SPECIES" \
      || { echo "[bed] $LABEL: BAMtoExonBED (intron) FAILED" >&2; exit 1; } ;;
  *)
    python "$EB" --i "$BAM" --r "$EXON_REF" --s "$SPECIES" \
      || { echo "[bed] $LABEL: BAMtoExonBED (intron) FAILED" >&2; exit 1; } ;;
esac

# 3b) ATOMIC PUBLISH (T3.1): the AltAnalyze scripts wrote the BEDs incrementally into the BAM folder
#     (staging); a mid-write LSF/NFS kill leaves a truncated-but-nonempty .bed that a bare [ -s ] would
#     wrongly accept as DONE. VALIDATE the content-bearing outputs BEFORE the (atomic, same-volume) mv
#     into BED_OUT_DIR, so the watchdog never publishes a truncated BED. __intronJunction.bed is
#     intentionally allowed to be empty (hard-gated junctions), so it is published as-is (-e).
_stage="$BAM_INPUT_DIR/${LABEL}"
bed_file_ok "${_stage}__junction.bed" || {
  echo "[bed] $LABEL: __junction.bed missing/truncated -- discarding (safe to resubmit)" >&2; rm -f "${_stage}__junction.bed"; exit 1; }
case "${BED_MODE:-intron}" in
  exon|both)
    bed_file_ok "${_stage}__exon.bed" || {
      echo "[bed] $LABEL: __exon.bed missing/truncated -- discarding (safe to resubmit)" >&2; rm -f "${_stage}__exon.bed"; exit 1; } ;;
esac
# STAR_beds is a sibling of STAR_bams on the SAME volume, so mv is an atomic rename. Keeps the BAM folder clean.
mkdir -p "$BED_OUT_DIR" || { echo "[bed] $LABEL: cannot mkdir BED_OUT_DIR=$BED_OUT_DIR" >&2; exit 1; }
for _suf in __junction.bed __exon.bed __intronJunction.bed; do
  [ -e "$BAM_INPUT_DIR/${LABEL}${_suf}" ] && mv -f "$BAM_INPUT_DIR/${LABEL}${_suf}" "$BED_OUT_DIR/${LABEL}${_suf}"
done

# 4) verify both BEDs landed & are non-empty (same predicate the watchdog uses)
if ! bed_done "$LABEL"; then
  echo "[bed] $LABEL: conversion ran but a BED is missing/empty -- not done (safe to resubmit)" >&2
  exit 1
fi
echo "[bed] $LABEL: complete (mode=${BED_MODE:-intron}) -> BEDs in $BED_OUT_DIR"

# Optionally free the STAR_bams volume: the BAM is no longer needed once its BEDs are made + verified.
# Default OFF -- BAMs are the primary alignment output; re-making BEDs after this needs re-alignment.
if [ "${DELETE_BAM_AFTER_BED:-0}" = "1" ]; then
  # PARTIAL-run guard (T1.1/T1.2): if the upstream finished incomplete, the launcher dropped an
  # INCOMPLETE_UPSTREAM marker in the BED root -> KEEP the BAM (a partial run must stay re-runnable; the
  # BAM is the last recoverable tier once FASTQ/.sra are gone).
  if [ -f "$PIPELINE_ROOT/PIPELINE_INCOMPLETE_UPSTREAM.txt" ]; then
    echo "[bed] $LABEL: run is PARTIAL (upstream incomplete) -> KEEPING BAM (deletion disabled this run)" >&2
  else
    rm -f "$BAM" "$BAM.bai" "${BAM%.bam}.bai" 2>/dev/null && echo "[bed] $LABEL: deleted BAM $BAM (BEDs are made)"
  fi
fi

# 5) wake the watchdog if this was the last live work job (pure accelerator; poll is the fallback)
bed_nudge_watchdog "$LABEL" || true
