#!/usr/bin/env bash
# fasterqdump_job.sh — LSF job body. Converts ONE .sra to .fastq.gz:
#   extract + compress on SCRATCH_DIR (fast/large), publish the final .fastq.gz
#   to the study dir (permanent archive), verify byte-for-byte, then delete the
#   source .sra. Single-end (<acc>.fastq) and paired-end (<acc>_1/_2.fastq) safe.
# Args: $1 = accession   $2 = study directory
# Submitted by sra_submit_conversion(). Not run by hand.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
set -u
SAMPLE="$1"
SDIR="$2"
cd "$SDIR" || { echo "[fqd] cannot cd $SDIR" >&2; exit 1; }
sra_load_modules
shopt -s nullglob

INPUT="$SDIR/${SAMPLE}.sra"
[ -e "$INPUT" ] || { echo "[fqd] $SAMPLE: no source .sra at $INPUT" >&2; exit 1; }

# ---- choose a working dir: configured scratch if usable, else in-place --------
WORK="$SDIR"
if [ -n "$SCRATCH_DIR" ]; then
  mkdir -p "$SCRATCH_DIR" 2>/dev/null
  avail=$(df -P "$SCRATCH_DIR" 2>/dev/null | awk 'NR==2{print int($4/1048576)}')
  if [ -n "$avail" ] && [ "$avail" -ge 60 ] && [ -w "$SCRATCH_DIR" ]; then
    WORK="$SCRATCH_DIR"
  else
    echo "[fqd] $SAMPLE: scratch '$SCRATCH_DIR' unusable (avail=${avail:-?}GB) -> in-place" >&2
  fi
fi
LOCAL="$WORK/fqd_${SAMPLE}_${LSB_JOBID:-$$}"
mkdir -p "$LOCAL" || { echo "[fqd] $SAMPLE: cannot mkdir $LOCAL" >&2; exit 1; }

# ---- 1) extract onto the working dir -----------------------------------------
fasterq-dump --threads "$THREADS" --split-files --temp "$LOCAL/tmp" -O "$LOCAL" "$INPUT"
RC=$?

FQ=("$LOCAL/${SAMPLE}"*.fastq)
if [ "$RC" -ne 0 ] || [ ${#FQ[@]} -eq 0 ]; then
  echo "[fqd] $SAMPLE: fasterq-dump FAILED (rc=$RC) or produced no FASTQ -> KEEPING source" >&2
  rm -rf "$LOCAL"; exit 1
fi

# ---- 2) compress on the working dir ------------------------------------------
if command -v pigz >/dev/null 2>&1; then
  pigz -p "$THREADS" "$LOCAL/${SAMPLE}"*.fastq
else
  gzip "$LOCAL/${SAMPLE}"*.fastq
fi
GZ=("$LOCAL/${SAMPLE}"*.fastq.gz)
if [ ${#GZ[@]} -eq 0 ]; then
  echo "[fqd] $SAMPLE: compression produced no .gz -> KEEPING source" >&2
  rm -rf "$LOCAL"; exit 1
fi

# ---- 3) publish to the archive, verify, then delete the source ---------------
cp -p "$LOCAL/${SAMPLE}"*.fastq.gz "$SDIR/"
ok=1
for g in "$LOCAL/${SAMPLE}"*.fastq.gz; do
  bn=$(basename "$g")
  if [ ! -f "$SDIR/$bn" ] || [ "$(stat -c%s "$g")" != "$(stat -c%s "$SDIR/$bn")" ]; then ok=0; fi
done
if [ "$ok" -eq 1 ]; then
  echo "[fqd] $SAMPLE: ${#GZ[@]} .fastq.gz published to archive OK -> deleting source"
  rm -rf "$LOCAL"
  rm -f  "$INPUT"
  rm -rf "$SDIR/$SAMPLE"
  rm -f  "$SDIR/${SAMPLE}.sra.vdbcache"
else
  echo "[fqd] $SAMPLE: archive verify FAILED -> KEEPING source (scratch left at $LOCAL)" >&2
  exit 1
fi
