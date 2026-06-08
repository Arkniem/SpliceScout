#!/usr/bin/env bash
# =============================================================================
# build_groups.sh -- construct AltAnalyze groups.txt + comps.txt from the shipped
# sample_groups.tsv (BioSample<TAB>group_num<TAB>group_label) INTERSECTED with the
# *__junction.bed files actually present in BED_INPUT_DIR (so samples that failed
# upstream are excluded). If fewer than 2 groups end up with enough present members,
# NO groups/comps are written and AltAnalyze runs GROUPLESS (per-sample PSI only).
# Pure bash + nullglob; NEVER `grep -c` (empty on the compute nodes -- see lib_bed.sh).
# =============================================================================
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
set -u; shopt -s nullglob

MIN_PER_GROUP=2     # a COMPARED group needs at least this many present samples (else no usable stats)

mkdir -p "$PIPELINE_ROOT" "$LOG_DIR" 2>/dev/null || true
rm -f "$GROUPS_FILE" "$COMPS_FILE" 2>/dev/null

[ -s "$SAMPLE_GROUPS" ] || { echo "[psi] no sample_groups.tsv shipped -> GROUPLESS PSI (all present BEDs)"; exit 0; }
[ -d "$BED_INPUT_DIR" ] || { echo "[psi] BED_INPUT_DIR missing: $BED_INPUT_DIR -> GROUPLESS" >&2; exit 0; }

# pass 1: candidate rows for samples whose junction BED is present; tally per-group counts in a temp file.
tmp_g="$PIPELINE_ROOT/.groups.tmp.$$"; : > "$tmp_g"
counts="$PIPELINE_ROOT/.gcount.$$";    : > "$counts"
present=0; absent=0
while IFS=$'\t' read -r bs gnum glabel || [ -n "${bs:-}" ]; do
  case "${bs:-}" in ''|'#'*) continue ;; esac
  [ -n "${gnum:-}" ] || continue
  if [ -e "$BED_INPUT_DIR/${bs}__junction.bed" ]; then
    printf '%s%s\t%s\t%s\n' "$bs" "$GROUP_KEY_SUFFIX" "$gnum" "${glabel:-group$gnum}" >> "$tmp_g"
    printf '%s\n' "$gnum" >> "$counts"
    present=$((present+1))
  else
    absent=$((absent+1))
  fi
done < "$SAMPLE_GROUPS"

# distinct group numbers with >= MIN_PER_GROUP present members (pure-bash tally; no grep -c)
ok_groups=""
for g in $(sort -u "$counts" 2>/dev/null); do
  c=0; while IFS= read -r x; do [ "$x" = "$g" ] && c=$((c+1)); done < "$counts"
  [ "$c" -ge "$MIN_PER_GROUP" ] && ok_groups="$ok_groups $g"
done
rm -f "$counts" 2>/dev/null

nok=0; for _g in $ok_groups; do nok=$((nok+1)); done
if [ "$nok" -lt 2 ]; then
  rm -f "$tmp_g" 2>/dev/null
  echo "[psi] only $nok group(s) with >= $MIN_PER_GROUP present samples (of $present present, $absent absent BED) -> GROUPLESS PSI"
  exit 0
fi

# keep only rows whose group qualified
: > "$GROUPS_FILE"
while IFS=$'\t' read -r key gnum glabel; do
  for g in $ok_groups; do
    if [ "$g" = "$gnum" ]; then printf '%s\t%s\t%s\n' "$key" "$gnum" "$glabel" >> "$GROUPS_FILE"; break; fi
  done
done < "$tmp_g"
rm -f "$tmp_g" 2>/dev/null

# comps.txt: compare every qualifying group against the LOWEST group number (the control/baseline).
base=""
for g in $(printf '%s\n' $ok_groups | sort -n); do [ -z "$base" ] && base="$g"; done
: > "$COMPS_FILE"
for g in $(printf '%s\n' $ok_groups | sort -n); do
  [ "$g" = "$base" ] && continue
  printf '%s\t%s\n' "$g" "$base" >> "$COMPS_FILE"
done

ng=0; while IFS= read -r _l; do case "$_l" in (*[![:space:]]*) ng=$((ng+1)) ;; esac; done < "$GROUPS_FILE"
echo "[psi] groups.txt: $ng samples across $nok groups ($absent shipped samples had no BED) ; comps -> $(tr '\n' ';' < "$COMPS_FILE")"
