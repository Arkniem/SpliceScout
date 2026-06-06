#!/usr/bin/env bash
# =============================================================================
# run_star_job.sh -- STAR 2-pass alignment for ONE sample (LSF job body).
# Sources config.sh for all settings. Hardened:
#   * space-aware workspace: node-local $TMPDIR > $SCRATCH > BAM_OUT volume
#   * stages FASTQs locally with cp + gzip -t retry (beats flaky NFS reads); if
#     there isn't room it reads direct and relies on idempotent resubmit
#   * single-end safe (never passes "NA" to STAR)
#   * idempotent: skips if a valid BAM already exists (samtools quickcheck)
#   * publishes a quickcheck-verified BAM, plus SJ.out.tab + Log.final.out to logs/
#   * drops --sjdbGTFfile unless SJDB_GTF is set (index usually already has it)
# Usage: run_star_job.sh <LABEL> <FASTQ1[,..]> <FASTQ2[,..]|NA>
# =============================================================================
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_star.sh"
[ -f "$PIPELINE_ROOT/RESOLVED_INDEX.env" ] && source "$PIPELINE_ROOT/RESOLVED_INDEX.env"  # resolved GENOME_DIR + SJDB_GTF
set -u

SAMPLE="$1"; FASTQ1="$2"; FASTQ2="${3:-NA}"
MIN_WORK_GB="${MIN_WORK_GB:-20}"
STAGE_HEADROOM_GB="${STAGE_HEADROOM_GB:-25}"
STAGE_RETRIES="${STAGE_RETRIES:-4}"

star_load_modules
command -v STAR     >/dev/null 2>&1 || { echo "[star] $SAMPLE: STAR not on PATH" >&2; exit 1; }
command -v samtools >/dev/null 2>&1 || { echo "[star] $SAMPLE: samtools not on PATH" >&2; exit 1; }
mkdir -p "$BAM_OUT" "$LOG_DIR" || { echo "[star] $SAMPLE: cannot mkdir output dirs" >&2; exit 1; }

if star_bam_ok "$SAMPLE"; then echo "[star] $SAMPLE: BAM present & valid -> skip"; exit 0; fi

avail_gb() {                       # available GB on the fs holding $1 (0 if df fails)
  ls -d "$1" >/dev/null 2>&1        # warm a possibly-cold NFS automount before df reads it
  df -P --block-size=1G "$1" 2>/dev/null | awk 'NR==2{print $4+0}'
}
# Real writability test BY ACTION. `[ -w ]` is unreliable on this NFS from compute nodes -- it can
# report not-writable on a dir where mkdir/touch actually succeed (the original "no workspace" + the
# first fix both died on this). So we just try to create a probe dir.
can_write() { local d="$1/.wtest.$$"; mkdir -p "$d" 2>/dev/null && { rmdir "$d" 2>/dev/null; return 0; }; return 1; }

# Workspace for staging + STAR 2-pass temp. Prefer a FAST dir (node-local TMPDIR, then SCRATCH) when
# it has room; otherwise fall back to a dedicated folder ON THE PIPELINE_ROOT VOLUME (where the BAMs
# already land, so it's guaranteed usable). The PIPELINE_ROOT fallback is taken even if df is flaky --
# a genuinely-full volume just fails staging and a resubmit retries -- so a full /scratch (or a flaky
# df / lying [ -w ]) can never block alignment.
WORK_FALLBACK="${STAR_WORK_DIR:-$PIPELINE_ROOT/_workspace}"
WORKBASE=""; WORKBASE_AVAIL=0
for cand in "${TMPDIR:-}" "$SCRATCH" "$WORK_FALLBACK"; do
  [ -n "$cand" ] || continue
  mkdir -p "$cand" 2>/dev/null || continue
  can_write "$cand" || continue
  a=$(avail_gb "$cand"); [ -n "$a" ] || a=0
  if [ "$a" -ge "$MIN_WORK_GB" ]; then WORKBASE="$cand"; WORKBASE_AVAIL="$a"; break; fi
done
# Guaranteed last resort: the PIPELINE_ROOT volume, used whenever it's writable-by-action (ignore df).
if [ -z "$WORKBASE" ] && mkdir -p "$WORK_FALLBACK" 2>/dev/null && can_write "$WORK_FALLBACK"; then
  WORKBASE="$WORK_FALLBACK"; WORKBASE_AVAIL="$(avail_gb "$WORK_FALLBACK")"; WORKBASE_AVAIL="${WORKBASE_AVAIL:-0}"
  echo "[star] $SAMPLE: no fast workspace >=${MIN_WORK_GB}G free; using PIPELINE_ROOT volume -> $WORK_FALLBACK (${WORKBASE_AVAIL}G)" >&2
fi
[ -n "$WORKBASE" ] || { echo "[star] $SAMPLE: no usable workspace ($WORK_FALLBACK not creatable)" >&2; exit 1; }
# Staging (copying the FASTQs into the workspace) only pays off on genuinely FAST local disk
# (node-local TMPDIR / SCRATCH). The PIPELINE_ROOT fallback is the SAME NFS volume as the by_study
# FASTQs, so copying there is pure waste -> mark it "not fast" and read the FASTQs in place instead.
WORKBASE_FAST=0; [ "$WORKBASE" != "$WORK_FALLBACK" ] && WORKBASE_FAST=1

WORK="$WORKBASE/star_${SAMPLE}_${LSB_JOBID:-$$}"
mkdir -p "$WORK" || { echo "[star] $SAMPLE: cannot mkdir WORK=$WORK" >&2; exit 1; }
trap 'rm -rf "$WORK"' EXIT
echo "[star] $SAMPLE: workspace=$WORK (${WORKBASE_AVAIL}G free)"

inputs_gb() {
  local total=0 f s arr all="$1,$2"
  IFS=',' read -ra arr <<< "$all"
  for f in ${arr[@]+"${arr[@]}"}; do
    [ "$f" = "NA" ] && continue; [ -e "$f" ] || continue
    s=$(stat -c%s "$f" 2>/dev/null || echo 0); total=$((total + s))
  done
  echo $((total / 1073741824 + 1))
}
NEED=$(inputs_gb "$FASTQ1" "$FASTQ2")
DO_STAGE=0
[ "$WORKBASE_FAST" -eq 1 ] && [ "$WORKBASE_AVAIL" -ge "$((NEED + STAGE_HEADROOM_GB))" ] && DO_STAGE=1

stage() {                          # stage one .gz; retry until cp ok AND gzip -t ok
  local src="$1" dst i=0
  dst="$WORK/$(basename "$src")"
  while [ "$i" -lt "$STAGE_RETRIES" ]; do
    i=$((i+1))
    if cp -f "$src" "$dst" 2>/dev/null && gzip -t "$dst" 2>/dev/null; then
      printf '%s' "$dst"; return 0
    fi
    echo "[star] $SAMPLE: stage try $i/$STAGE_RETRIES failed for $src -- retrying" >&2
    rm -f "$dst"
  done
  echo "[star] $SAMPLE: FAILED to stage $src after $STAGE_RETRIES tries" >&2
  return 1
}
stage_list() {                     # comma-list -> comma-list of local paths
  local input="$1" out="" f lp arr
  IFS=',' read -ra arr <<< "$input"
  for f in ${arr[@]+"${arr[@]}"}; do
    lp=$(stage "$f") || return 1
    out="${out:+$out,}$lp"
  done
  printf '%s' "$out"
}

if [ "$DO_STAGE" -eq 1 ]; then
  echo "[star] $SAMPLE: staging ~${NEED}G to workspace"
  R1=$(stage_list "$FASTQ1") || { echo "[star] $SAMPLE: R1 staging failed" >&2; exit 1; }
  if [ "$FASTQ2" != "NA" ] && [ -n "$FASTQ2" ]; then
    R2=$(stage_list "$FASTQ2") || { echo "[star] $SAMPLE: R2 staging failed" >&2; exit 1; }
  else R2="NA"; fi
else
  echo "[star] $SAMPLE: not enough room to stage (~$((NEED + STAGE_HEADROOM_GB))G needed, ${WORKBASE_AVAIL}G free) -- reading direct; a resubmit retries any flaky read"
  R1="$FASTQ1"; R2="$FASTQ2"
fi
READS="$R1"
[ "$R2" != "NA" ] && [ -n "$R2" ] && READS="$R1 $R2"

# conditional splice-junction GTF (default: index already has it -> don't re-supply)
SJDB=()
[ -n "$SJDB_GTF" ] && SJDB=(--sjdbGTFfile "$SJDB_GTF" --sjdbOverhang "$SJDB_OVERHANG")

echo "[star] $SAMPLE: aligning (threads=$THREADS, sortRAM=$SORT_RAM)"
cd "$WORK" || exit 1
STAR --runThreadN "$THREADS" \
     --genomeDir "$GENOME_DIR" \
     --readFilesIn $READS \
     --readFilesCommand gunzip -c \
     --outFileNamePrefix "$WORK/${SAMPLE}_" \
     --outSAMtype BAM SortedByCoordinate \
     --outSAMunmapped Within \
     --outSAMattributes NH HI NM MD AS XS \
     --outSAMstrandField intronMotif \
     --twopassMode Basic \
     --limitBAMsortRAM "$SORT_RAM" \
     --outFilterMultimapScoreRange 1 \
     --outFilterMultimapNmax 20 \
     --outFilterMismatchNmax 10 \
     --outFilterMatchNminOverLread 0.33 \
     --outFilterScoreMinOverLread 0.33 \
     --alignIntronMax 500000 \
     --alignMatesGapMax 1000000 \
     --alignSJDBoverhangMin 1 \
     ${SJDB[@]+"${SJDB[@]}"} \
     $STAR_EXTRA_ARGS
RC=$?

BAM="$WORK/${SAMPLE}_Aligned.sortedByCoord.out.bam"
if [ "$RC" -ne 0 ] || [ ! -s "$BAM" ]; then
  echo "[star] $SAMPLE: STAR FAILED (rc=$RC) -- BAM not published (safe to resubmit)" >&2
  exit 1
fi

# publish + verify the BAM
cp -f "$BAM" "$BAM_OUT/$SAMPLE.bam"
if ! samtools quickcheck "$BAM_OUT/$SAMPLE.bam" 2>/dev/null; then
  echo "[star] $SAMPLE: published BAM failed quickcheck -- retrying copy" >&2
  cp -f "$BAM" "$BAM_OUT/$SAMPLE.bam"
  samtools quickcheck "$BAM_OUT/$SAMPLE.bam" 2>/dev/null || { echo "[star] $SAMPLE: BAM publish FAILED" >&2; exit 1; }
fi
samtools index "$BAM_OUT/$SAMPLE.bam" 2>/dev/null || true

# keep splice junctions + alignment-QC log next to the BAMs (in logs/)
[ -f "$WORK/${SAMPLE}_SJ.out.tab" ]    && cp -f "$WORK/${SAMPLE}_SJ.out.tab"    "$LOG_DIR/${SAMPLE}.SJ.out.tab"    2>/dev/null || true
[ -f "$WORK/${SAMPLE}_Log.final.out" ] && cp -f "$WORK/${SAMPLE}_Log.final.out" "$LOG_DIR/${SAMPLE}.Log.final.out" 2>/dev/null || true

echo "[star] $SAMPLE: complete -> $BAM_OUT/$SAMPLE.bam"
