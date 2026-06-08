#!/usr/bin/env bash
# lib_index.sh -- STAR genome-index resolution helpers. Sourced AFTER config.sh + lib_star.sh by
# resolve_index.sh. Resolution order: explicit valid GENOME_DIR -> organism registry -> a previously
# built STAR_INDEX_ROOT/<organism> -> build-once (build_star_index.sh). JSON is parsed with python3
# (already required by the pipeline), so there is no 'jq' dependency.

# A usable STAR index has both SAindex and Genome (the same test setup.sh uses).
star_index_valid() { [ -n "${1:-}" ] && [ -f "$1/SAindex" ] && [ -f "$1/Genome" ]; }

# Lowercase + collapse non-alnum -> "_" (matches the Python detect_organism slug).
star_org_slug() {
  printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9._-]+/_/g; s/^_+//; s/_+$//'
}

# Look up organisms[ORGANISM].index_dir (case-insensitive) in $REGISTRY_FILE.
# Echoes "<index_dir>|<gtf>" (gtf may be empty); nothing if no match.
star_registry_lookup() {
  local py; py="$(command -v python3 || command -v python)"
  [ -n "$py" ] && [ -f "${REGISTRY_FILE:-}" ] || return 1
  "$py" - "$REGISTRY_FILE" "${ORGANISM:-}" <<'PYEOF'
import json, sys
try:
    reg = json.load(open(sys.argv[1], encoding="utf-8")).get("organisms", {})
except Exception:
    sys.exit(0)
key = " ".join(sys.argv[2].lower().split())
m = {" ".join(k.lower().split()): v for k, v in reg.items()}.get(key)
if m and m.get("index_dir"):
    print(m["index_dir"] + "|" + (m.get("gtf") or ""))
PYEOF
}

# Resolve the index. Sets the chosen GENOME_DIR into the global RESOLVED_GENOME_DIR (NOT stdout -- the
# caller must invoke this WITHOUT $(...) so the side-effect globals survive; command substitution runs in
# a subshell and would silently lose NEED_BUILD, leaving the build-once branch dead). Sets NEED_BUILD=1 +
# BUILD_TARGET when a build is needed; may set SJDB_GTF from the registry. Order: explicit -> registry ->
# prior auto build -> build.
star_resolve_index() {
  NEED_BUILD=0; BUILD_TARGET=""; RESOLVED_GENOME_DIR=""
  if star_index_valid "${GENOME_DIR:-}"; then
    echo "[resolve] explicit GENOME_DIR is a valid index: $GENOME_DIR" >&2
    RESOLVED_GENOME_DIR="$GENOME_DIR"; return 0
  fi
  [ -n "${GENOME_DIR:-}" ] && echo "[resolve] GENOME_DIR set but missing SAindex/Genome -> trying registry" >&2
  local hit idir gtf
  hit="$(star_registry_lookup || true)"
  if [ -n "$hit" ]; then
    idir="${hit%%|*}"; gtf="${hit#*|}"
    if star_index_valid "$idir"; then
      echo "[resolve] registry match for '${ORGANISM:-?}': $idir" >&2
      [ -n "$gtf" ] && [ -z "${SJDB_GTF:-}" ] && SJDB_GTF="$gtf"
      RESOLVED_GENOME_DIR="$idir"; return 0
    fi
    echo "[resolve] registry path invalid ($idir) -> checking auto root" >&2
  fi
  local slug auto; slug="$(star_org_slug "${ORGANISM:-}")"; auto="${STAR_INDEX_ROOT%/}/$slug"
  if star_index_valid "$auto"; then
    echo "[resolve] using previously built index: $auto" >&2
    RESOLVED_GENOME_DIR="$auto"; return 0
  fi
  NEED_BUILD=1; BUILD_TARGET="$auto"
  echo "[resolve] no index for '${ORGANISM:-?}' anywhere -> build-once into $auto" >&2
  RESOLVED_GENOME_DIR="$auto"; return 0
}
