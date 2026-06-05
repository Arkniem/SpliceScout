#!/usr/bin/env bash
# build_sample_list.sh -- generate $SAMPLE_LIST from $FASTQ_INPUT_DIR.
# BUILD-ONCE: if the list already exists it is NOT rebuilt, so the watchdog's
# denominator can never drift mid-run. Delete $SAMPLE_LIST to force a rebuild.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
set -u

mkdir -p "$PIPELINE_ROOT" "$LOG_DIR"

if [ -s "$SAMPLE_LIST" ]; then
  n=$(grep -cve '^[[:space:]]*$' "$SAMPLE_LIST")
  echo "sample list already exists ($n rows): $SAMPLE_LIST"
  echo "  (delete it to force a rebuild)"
  exit 0
fi

PY="$(command -v python3 || command -v python)"
[ -n "$PY" ] || { echo "ERROR: python3 not found" >&2; exit 1; }

rt=()
[ -n "$RUNTABLE" ] && rt=(--runtable "$RUNTABLE")

"$PY" "$HERE/make_sample_list.py" \
    --input-dir "$FASTQ_INPUT_DIR" \
    ${rt[@]+"${rt[@]}"} \
    --out "$SAMPLE_LIST"
rc=$?

n=$(grep -cve '^[[:space:]]*$' "$SAMPLE_LIST" 2>/dev/null || echo 0)
if [ "$rc" -ne 0 ] || [ "$n" -eq 0 ]; then
  echo "ERROR: sample-list build failed or produced 0 rows." >&2
  echo "  check $FASTQ_INPUT_DIR and the .orphans/.unmapped/.mixed reports beside $SAMPLE_LIST" >&2
  exit 1
fi
echo "built $n-sample list -> $SAMPLE_LIST"
