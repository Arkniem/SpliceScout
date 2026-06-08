#!/usr/bin/env bash
# setup.sh -- one-time preflight. Safe to re-run. Read-only except mkdir of the
# output/scratch dirs. Exits non-zero if anything required is wrong.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_star.sh"
set -u; shopt -s nullglob

ok=1
echo "== STAR alignment pipeline -- setup =="

# --- FASTQ input ---
if [ -d "$FASTQ_INPUT_DIR" ]; then
  nfq=$(find "$FASTQ_INPUT_DIR" -type f \( -name '*.fastq.gz' -o -name '*.fq.gz' \) 2>/dev/null | wc -l)
  echo "  FASTQ_INPUT_DIR : $FASTQ_INPUT_DIR  ($nfq fastq.gz files)"
  [ "$nfq" -eq 0 ] && { echo "    WARNING: no *.fastq.gz / *.fq.gz found under here"; ok=0; }
else
  echo "  ERROR: FASTQ_INPUT_DIR not found: $FASTQ_INPUT_DIR"; ok=0
fi

# --- genome index ---
if [ -f "$GENOME_DIR/SAindex" ] && [ -f "$GENOME_DIR/Genome" ]; then
  echo "  GENOME_DIR      : $GENOME_DIR  (looks like a STAR index)"
  if [ -z "$SJDB_GTF" ]; then
    baked=$(awk '$1=="sjdbGTFfile"{print $2}' "$GENOME_DIR/genomeParameters.txt" 2>/dev/null)
    if [ -n "$baked" ] && [ "$baked" != "-" ]; then
      echo "    index already has a GTF baked in ($baked) -> leaving SJDB_GTF empty is correct"
    else
      echo "    NOTE: index reports no baked-in GTF; set SJDB_GTF for splice-aware alignment"
    fi
  else
    echo "    SJDB_GTF set -> will re-supply junctions at align time (overhang $SJDB_OVERHANG)"
  fi
else
  echo "  NOTE: GENOME_DIR is not a prebuilt index ($GENOME_DIR)"
  echo "        -> will resolve by ORGANISM='$ORGANISM' (registry -> prior build -> build-once). No action needed."
fi

# --- runtable (optional) ---
PY="$(command -v python3 || command -v python)"
if [ -n "$RUNTABLE" ]; then
  if [ -f "$RUNTABLE" ]; then
    echo "  RUNTABLE        : $RUNTABLE"
    "$PY" "$HERE/make_sample_list.py" --inspect-runtable --runtable "$RUNTABLE" 2>&1 | sed 's/^/    /' \
      || { echo "    ERROR: runtable unreadable / missing Run+BioSample columns"; ok=0; }
  else
    echo "  ERROR: RUNTABLE not found: $RUNTABLE"; ok=0
  fi
else
  echo "  RUNTABLE        : (none) -> one BAM per run"
fi

# --- tools ---
star_load_modules
for t in STAR samtools bsub bjobs; do
  if command -v "$t" >/dev/null 2>&1; then echo "  OK   $t -> $(command -v "$t")"
  else echo "  MISSING $t"; ok=0; fi
done
if [ -n "$PY" ]; then echo "  OK   python3 -> $PY ($("$PY" --version 2>&1))"
else echo "  MISSING python3"; ok=0; fi

# --- workspace + output ---
if [ -n "$SCRATCH" ] && mkdir -p "$SCRATCH" 2>/dev/null && [ -w "$SCRATCH" ]; then
  echo "  SCRATCH         : $SCRATCH  ($(df -h "$SCRATCH" 2>/dev/null | awk 'NR==2{print $4}') free)"
else
  echo "  WARNING: SCRATCH '$SCRATCH' not writable -- jobs fall back to BAM_OUT volume / direct reads"
fi
# Test writability BY ACTION (touch a probe), NOT `[ -w ]`: on the lab NFS from COMPUTE NODES,
# `[ -w dir ]` returns false even where mkdir/touch actually SUCCEED -- this falsely failed setup
# ("BAM_OUT not writable") and wedged the STAR launcher in a retry loop.
if mkdir -p "$BAM_OUT" "$LOG_DIR" 2>/dev/null && touch "$BAM_OUT/.wtest.$$" 2>/dev/null; then
  rm -f "$BAM_OUT/.wtest.$$" 2>/dev/null
  echo "  BAM_OUT         : $BAM_OUT  ($(df -h "$BAM_OUT" 2>/dev/null | awk 'NR==2{print $4}') free)"
else
  echo "  ERROR: BAM_OUT not writable (touch probe failed): $BAM_OUT"; ok=0
fi

# --- memory sanity ---
sort_mb=$(( SORT_RAM / 1000000 ))
[ "$sort_mb" -ge "$MEM_MB" ] && echo "  WARNING: SORT_RAM (~${sort_mb}MB) >= MEM_MB (${MEM_MB}MB) -- STAR may be OOM-killed; lower SORT_RAM"

# --- slot cap (informational) ---
command -v busers >/dev/null 2>&1 && busers "$USER" 2>/dev/null | sed 's/^/  busers: /'

if [ "$ok" -eq 1 ]; then echo "== setup OK =="; exit 0
else echo "== setup found problems (see above) -- fix config.sh =="; exit 1; fi
