#!/usr/bin/env bash
# setup.sh -- preflight the splicing-concordance stage. Advisory (always exits 0): run_concordance_job.sh
# re-validates the hard requirements. Reports the scorer, the AltAnalyze modules on PYTHONPATH, the
# upstream PSI signatures, and the cancer atlases to be scored.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
set -u; shopt -s nullglob

echo "== splicing concordance stage -- setup =="
mkdir -p "$PIPELINE_ROOT" "$LOG_DIR" "$DRUG_SIG_DIR" "$RESULTS_DIR" 2>/dev/null || true

# --- scorer + AltAnalyze python deps ---
if [ -s "$CONCORD_SCRIPT" ]; then
  echo "  scorer        : $CONCORD_SCRIPT"
else
  echo "  ERROR: concordance scorer not found at $CONCORD_SCRIPT"
fi
_ok_deps=1
for m in export.py UI.py unique.py; do
  [ -s "$ALTANALYZE_HOME/$m" ] || { _ok_deps=0; echo "    WARNING: $m not under ALTANALYZE_HOME=$ALTANALYZE_HOME"; }
done
[ "$_ok_deps" = 1 ] && echo "  PYTHONPATH    : $ALTANALYZE_HOME  (export/UI/unique present)"

# --- upstream PSI signatures ---
if [ -d "$PSI_EVENTS_DIR" ]; then
  sigs=("$PSI_EVENTS_DIR"/PSI.*_vs_*.txt "$PSI_EVENTS_DIR"/PSI.*_vs_*.txt.gz); n=${#sigs[@]}   # may be gzipped
  echo "  PSI signatures: $PSI_EVENTS_DIR  ($n drug signatures)"
  [ "$n" -eq 0 ] && echo "    WARNING: no PSI.*_vs_*.txt(.gz) yet (PSI stage may not be finished)"
else
  echo "  WARNING: PSI events dir not found: $PSI_EVENTS_DIR (PSI stage not finished?)"
fi

# --- cancer atlases (queries) ---
if [ -s "$QUERIES_FILE" ]; then
  nq=0
  while IFS=$'\t' read -r qname qdir ckind cpath || [ -n "${qname:-}" ]; do
    case "${qname:-}" in ''|'#'*) continue ;; esac
    nq=$((nq+1))
    if [ -d "$qdir" ]; then
      qf=("$qdir"/PSI.*.txt)
      echo "    atlas '$qname': $qdir  (${#qf[@]} subtype signatures; counts=$ckind)"
    else
      echo "    atlas '$qname': MISSING query dir $qdir"
    fi
  done < "$QUERIES_FILE"
  echo "  atlases to score: $nq"
else
  echo "  ERROR: no queries.tsv shipped -> nothing to score against"
fi

echo "  thresholds    : dPSI>=$DPSI rawp<=$RAWP removeIR=$REMOVE_IR conc<$CONC_THRESHOLD"
echo "  RESULTS_DIR   : $RESULTS_DIR"
echo "  setup done (advisory; run_concordance_job.sh enforces the hard requirements)"
exit 0
