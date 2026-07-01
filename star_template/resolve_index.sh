#!/usr/bin/env bash
# =============================================================================
# resolve_index.sh -- decide which STAR genome index to use (or build one), ONCE,
# before any alignments are submitted. Writes RESOLVED_INDEX.env (sourced by
# submit_all.sh / watchdog.sh / run_star_job.sh) carrying the resolved GENOME_DIR
# + SJDB_GTF + an optional BUILD_JID. When a build is needed, per-sample STAR jobs
# are submitted with -w done(BUILD_JID) so they wait for the index.
# Resolution order: explicit valid GENOME_DIR -> organism registry -> prior auto
# build (STAR_INDEX_ROOT/<org>) -> build-once via build_star_index.sh.
# =============================================================================
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_star.sh"
source "$HERE/lib_index.sh"
set -u
star_require_bsub

ENVF="$PIPELINE_ROOT/RESOLVED_INDEX.env"
mkdir -p "$PIPELINE_ROOT" "$LOG_DIR"

# Call DIRECTLY (not via $()) so the side-effect globals NEED_BUILD/BUILD_TARGET/SJDB_GTF survive --
# command substitution would run it in a subshell and lose them (the build-once branch below depends on NEED_BUILD).
star_resolve_index
RESOLVED="${RESOLVED_GENOME_DIR:-}"
echo "[resolve] GENOME_DIR -> $RESOLVED"

BUILD_JID=""
if [ "${NEED_BUILD:-0}" -eq 1 ]; then
  if [ -z "${REF_FASTA_URL:-}" ] || [ -z "${REF_GTF_URL:-}" ]; then
    echo "[resolve] ERROR: an index must be built for '${ORGANISM:-?}' but no reference URLs are set." >&2
    echo "[resolve] Fix: add '${ORGANISM:-?}' to star_index_registry.json (reference_urls), point" >&2
    echo "[resolve]      GENOME_DIR at a prebuilt index, or set its path in organisms.<org>.index_dir." >&2
    exit 1
  fi
  star_qopt
  BJ="${JOB_TAG}_staridx_$(star_org_slug "${ORGANISM:-organism}")"
  LIVE="$(star_live_names)"
  if star_has_live "$BJ" "$LIVE"; then
    BUILD_JID="$(bjobs -noheader -o jobid -J "$BJ" 2>/dev/null | head -1)"
    echo "[resolve] a build job ($BJ, id ${BUILD_JID:-?}) is already live -> reusing it"
  elif star_index_valid "$RESOLVED"; then
    echo "[resolve] target index became valid -> no build needed"
  elif ! mkdir "${RESOLVED%/}.buildlock" 2>/dev/null; then
    # CROSS-RUN guard: another run (different JOB_TAG, so star_has_live above can't see it) is already
    # building this EXACT shared index dir -> do NOT submit a 2nd build that would race the same files.
    # build_star_index.sh removes the lock on exit; per-sample STAR jobs that start before the index is
    # ready just fail + get resubmitted, so this never deadlocks even if the lock is briefly stale.
    echo "[resolve] index build already claimed by another run (${RESOLVED%/}.buildlock) -> not duplicating"
  else
    echo "[resolve] submitting build-once job for '${ORGANISM:-?}' -> $RESOLVED"
    BUILD_JID="$(bsub -L /bin/bash -J "$BJ" \
        -n "$BUILD_THREADS" -M "$BUILD_MEM_MB" -W "$BUILD_WALL" \
        -R "rusage[mem=${BUILD_MEM_RUSAGE}] span[hosts=1]" \
        -o "$LOG_DIR/$BJ.out" -e "$LOG_DIR/$BJ.err" \
        ${QOPT[@]+"${QOPT[@]}"} \
        "$SCRIPTS_DIR/build_star_index.sh" "${ORGANISM:-organism}" "$RESOLVED" \
        "$REF_FASTA_URL" "$REF_GTF_URL" "$BUILD_THREADS" "$SJDB_OVERHANG" | star_jobid)"
    echo "[resolve] build job id: ${BUILD_JID:-<none>}"
  fi
fi

{
  echo "GENOME_DIR='$RESOLVED'"
  echo "SJDB_GTF='${SJDB_GTF:-}'"
  echo "BUILD_JID='${BUILD_JID:-}'"
  echo "export GENOME_DIR SJDB_GTF BUILD_JID"
} > "$ENVF"
echo "[resolve] wrote $ENVF"
