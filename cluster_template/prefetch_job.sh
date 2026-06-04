#!/usr/bin/env bash
# prefetch_job.sh — LSF job body. Downloads every accession in a study's list.
# Args: $1 = study directory   $2 = list file (relative to the study dir)
# Submitted by sra_submit_prefetch(). Not run by hand.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
set -u
SDIR="$1"
LIST="${2:-SraAccList.txt}"
cd "$SDIR" || { echo "prefetch_job: cannot cd $SDIR" >&2; exit 1; }
sra_load_modules
# Each accession lands in <SDIR>/<ACC>/<ACC>.sra (or .sralite). Non-zero exit on a
# single failed accession is fine: the convert step runs on whatever downloaded
# (ended() dependency) and the watchdog re-fetches anything still missing.
prefetch -O "$SDIR" --option-file "$LIST"
