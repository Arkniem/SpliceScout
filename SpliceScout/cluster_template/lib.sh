#!/usr/bin/env bash
# lib.sh — submission helpers shared by the driver scripts. Source AFTER config.sh.
# All jobs are submitted with "-L /bin/bash" so the compute-node login shell sets
# up environment modules; job names are namespaced with $JOB_TAG so resubmission
# is idempotent (a driver can check "is <name> already live?" before submitting).

# Fail early with a clear message if not on an LSF submit host (the #1 setup
# mistake: launching from a login node where bsub isn't available).
sra_require_bsub() {
  command -v bsub >/dev/null 2>&1 && return 0
  echo "ERROR: 'bsub' not found on host '$(hostname)'." >&2
  echo "       Launch the pipeline from your LSF SUBMIT HOST, not the login node." >&2
  echo "       On this cluster that is the head node, e.g.:  ssh bmiclusterp-head" >&2
  exit 1
}

# Set QOPT=(-q QUEUE) if a queue is configured, else empty.
sra_qopt() { QOPT=(); [ -n "$LSF_QUEUE" ] && QOPT=(-q "$LSF_QUEUE"); }

# Read "Job <12345> is submitted ..." on stdin -> print 12345
sra_jobid() { sed -n 's/.*Job <\([0-9]*\)>.*/\1/p'; }

# Snapshot of my live (RUN+PEND) LSF job names, one per line.
sra_live_names() { bjobs -noheader -o job_name 2>/dev/null; }

# Is job name $1 present in the newline-separated snapshot $2 ?
sra_has_live() { printf '%s\n' "$2" | grep -qxF "$1"; }

# Submit a prefetch (download) job. Args: studydir  listfile  [dep_jobid]
# Downloads every accession in <studydir>/<listfile> into <studydir>. Echoes job id.
sra_submit_prefetch() {
  local sdir="$1" list="${2:-SraAccList.txt}" dep="${3:-}"
  sra_qopt; local wopt=(); [ -n "$dep" ] && wopt=(-w "ended($dep)")
  bsub -L /bin/bash -n 1 -M "$PREFETCH_MEM_MB" -W "$WALL" \
       -J "${JOB_TAG}_pf_$(basename "$sdir")" \
       -o "$sdir/prefetch.out" -e "$sdir/prefetch.err" \
       ${QOPT[@]+"${QOPT[@]}"} ${wopt[@]+"${wopt[@]}"} \
       "$SCRIPTS_DIR/prefetch_job.sh" "$sdir" "$list" | sra_jobid
}

# Submit ONE conversion job. Args: accession  studydir   Echoes job id.
sra_submit_conversion() {
  local acc="$1" sdir="$2"
  sra_qopt
  bsub -L /bin/bash -n "$THREADS" -M "$MEM_MB" -W "$WALL" -R "span[hosts=1]" \
       -J "${JOB_TAG}_fqd_${acc}" \
       -o "$sdir/fasterqdump_${acc}.out" -e "$sdir/fasterqdump_${acc}.err" \
       ${QOPT[@]+"${QOPT[@]}"} \
       "$SCRIPTS_DIR/fasterqdump_job.sh" "$acc" "$sdir" | sra_jobid
}

# Submit the flatten+convert step for a study (typically gated on its prefetch).
# Args: studydir  [listfile]  [dep_jobid]
#   listfile empty -> handle ALL .sra in the study; else only those accessions.
# Echoes job id.
sra_submit_convert_study() {
  local sdir="$1" list="${2:-}" dep="${3:-}"
  sra_qopt; local wopt=(); [ -n "$dep" ] && wopt=(-w "ended($dep)")
  bsub -L /bin/bash -n 1 -M 2000 -W 30 \
       -J "${JOB_TAG}_cs_$(basename "$sdir")" \
       -o "$sdir/convert_study.out" -e "$sdir/convert_study.err" \
       ${QOPT[@]+"${QOPT[@]}"} ${wopt[@]+"${wopt[@]}"} \
       "$SCRIPTS_DIR/convert_study.sh" "$sdir" "$list" | sra_jobid
}

# Count accessions in a study that already have a .fastq.gz (single- or paired-end).
# Anchored to SraAccList.txt when present (bounded by the list, immune to stale-NFS
# directory over-counts on compute nodes); falls back to a file scan otherwise.
sra_done_count() {  # arg: studydir
  local sdir="$1" a n=0
  if [ -f "$sdir/SraAccList.txt" ]; then
    while read -r a; do
      a=$(echo "$a" | tr -d '\r'); [ -z "$a" ] && continue
      if compgen -G "$sdir/$a.fastq.gz" >/dev/null 2>&1 || compgen -G "$sdir/${a}_[0-9].fastq.gz" >/dev/null 2>&1; then
        n=$((n+1))
      fi
    done < "$sdir/SraAccList.txt"
    echo "$n"
  else
    ls "$sdir"/*.fastq.gz 2>/dev/null \
      | sed 's#.*/##; s/_[0-9]\.fastq\.gz$//; s/\.fastq\.gz$//' | sort -u | wc -l
  fi
}
