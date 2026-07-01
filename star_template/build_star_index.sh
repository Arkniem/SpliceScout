#!/usr/bin/env bash
# =============================================================================
# build_star_index.sh -- BUILD-ONCE STAR genome index (LSF job body). Heavy:
# ~1-2 h, tens of GB RAM (GRCh38 needs ~32-40 GB). Submitted by resolve_index.sh
# only when no usable index exists for the organism.
# Args: <organism> <target_index_dir> <fasta_url> <gtf_url> <threads> <sjdbOverhang>
# Idempotent: skips if a valid index is already present; only marks success after
# STAR genomeGenerate AND a validity check pass.
# =============================================================================
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_star.sh"
source "$HERE/lib_index.sh"
set -u
star_load_modules

ORG="${1:-organism}"; IDX="$2"; FAURL="$3"; GTFURL="$4"; THR="${5:-${BUILD_THREADS:-16}}"; OH="${6:-100}"
MARK="$IDX/.star_index_done"
trap 'rmdir "${IDX%/}.buildlock" 2>/dev/null' EXIT   # release the cross-run build lock resolve_index.sh took

command -v STAR >/dev/null 2>&1 || { echo "[buildidx] STAR not on PATH" >&2; exit 1; }
if star_index_valid "$IDX"; then
  echo "[buildidx] $ORG: a valid index already exists at $IDX -> skip"; touch "$MARK" 2>/dev/null || true; exit 0
fi
[ -n "$FAURL" ] || { echo "[buildidx] no FASTA URL for '$ORG'" >&2; exit 1; }
[ -n "$GTFURL" ] || { echo "[buildidx] no GTF URL for '$ORG'" >&2; exit 1; }

WORKBASE="${SCRATCH:-$(dirname "$BAM_OUT")}"
mkdir -p "$WORKBASE" "$IDX" || { echo "[buildidx] cannot mkdir workspace/index" >&2; exit 1; }
WORK="$WORKBASE/staridx_$(star_org_slug "$ORG")_${LSB_JOBID:-$$}"
mkdir -p "$WORK" || exit 1
trap 'rm -rf "$WORK"' EXIT

fetch() {                          # url -> echoes local uncompressed path
  local url="$1" gz out
  gz="$WORK/$(basename "$url")"
  if command -v curl >/dev/null 2>&1; then
    curl -fSL --retry 4 -o "$gz" "$url" || { echo "[buildidx] download failed: $url" >&2; return 1; }
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$gz" "$url" || { echo "[buildidx] download failed: $url" >&2; return 1; }
  else
    echo "[buildidx] neither curl nor wget available" >&2; return 1
  fi
  case "$gz" in
    *.gz) gunzip -f "$gz"; out="${gz%.gz}" ;;
    *)    out="$gz" ;;
  esac
  printf '%s' "$out"
}

echo "[buildidx] $ORG: downloading reference FASTA + GTF"
FA="$(fetch "$FAURL")" || exit 1
GTF="$(fetch "$GTFURL")" || exit 1

echo "[buildidx] $ORG: STAR genomeGenerate -> $IDX (threads=$THR, sjdbOverhang=$OH)"
STAR --runMode genomeGenerate --runThreadN "$THR" \
     --genomeDir "$IDX" \
     --genomeFastaFiles "$FA" \
     --sjdbGTFfile "$GTF" \
     --sjdbOverhang "$OH" \
     --outTmpDir "$WORK/_STARtmp" \
     ${STAR_GENOME_EXTRA_ARGS:-}
RC=$?
if [ "$RC" -ne 0 ] || ! star_index_valid "$IDX"; then
  echo "[buildidx] $ORG: genomeGenerate FAILED (rc=$RC) -- index NOT marked valid (safe to resubmit)" >&2
  exit 1
fi
{ echo "organism=$ORG"; echo "fasta=$FAURL"; echo "gtf=$GTFURL"; echo "overhang=$OH"; } > "$MARK"
echo "[buildidx] $ORG: index built -> $IDX"
