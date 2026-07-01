#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""rank_concordance.py -- turn splicingConcordance_advanced.py's concordance.txt into a human-readable,
RANKED reversal-candidate report. Pure text parsing (no AltAnalyze deps), python 2.7 AND 3 compatible.

concordance.txt cell format (from the scorer): "<cor>|<anti>:<N>" per (drug row x subtype column), or
"0.5|0.5" when the overlap is below the scorer's floor. cor in [0,1]: 1 = the drug MIMICS the subtype's
splicing (bad), 0 = the drug REVERSES it (therapeutic). N = overlapping splicing events.

Candidates = cor < THRESHOLD, ranked by N (overlapping events) then patient count. Optional patient
counts per subtype come from --counts (a subtype<TAB>patients tsv, e.g. the AML Leucegene table) or
--mergedresult (an OncoSplice cluster membership matrix; count = members per R*-V* cluster).
"""
from __future__ import print_function
import sys, re, getopt


def parse_concordance(path):
    f = open(path)
    try:
        lines = f.read().splitlines()
    finally:
        f.close()
    if not lines:
        return [], []
    subs = lines[0].split('\t')[1:]
    rows = []
    for ln in lines[1:]:
        if not ln.strip():
            continue
        p = ln.split('\t')
        drug = p[0]
        for sub, cell in zip(subs, p[1:]):
            if ':' not in cell or '|' not in cell:
                continue
            try:
                cor = float(cell.split('|')[0])
                rest = cell.split('|')[1]
                anti = float(rest.split(':')[0])
                n = int(rest.split(':')[1])
            except (ValueError, IndexError):
                continue
            rows.append((drug, sub, cor, anti, n))
    return subs, rows


def clean_name(name, kind):
    s = name
    s = re.sub(r'^PSI\.', '', s)
    s = re.sub(r'^Leucegene\.', '', s)
    if kind == 'drug':
        s = re.sub(r'_vs_.*$', '', s)
    else:
        s = re.sub(r'_vs_Others$', '', s)
        s = re.sub(r'_vs_.*$', '', s)
    return s


_RV = re.compile(r'R\d+[-_]?V\d+', re.I)


def rv_token(s):
    m = _RV.search(s or '')
    if not m:
        return None
    return m.group(0).upper().replace('_', '-')


def load_counts_tsv(path):
    d = {}
    f = open(path)
    try:
        for i, ln in enumerate(f):
            t = ln.rstrip('\n').split('\t')
            if len(t) < 2:
                continue
            if i == 0 and not t[1].strip().replace('.', '', 1).isdigit():
                continue   # header row
            try:
                d[t[0].strip()] = int(float(t[1]))
            except ValueError:
                pass
    finally:
        f.close()
    return d


def load_counts_mergedresult(path):
    # rows = samples, cols = clusters (e.g. "LUAD_DT:R1-V23") with 0/1 (or float) membership.
    f = open(path)
    try:
        hdr = f.readline().rstrip('\n').split('\t')
        counts = [0] * len(hdr)
        for ln in f:
            t = ln.rstrip('\n').split('\t')
            for j in range(1, min(len(t), len(hdr))):
                try:
                    if float(t[j]) >= 0.5:
                        counts[j] += 1
                except ValueError:
                    pass
    finally:
        f.close()
    d = {}
    for j in range(1, len(hdr)):
        if hdr[j].strip():
            d[hdr[j].strip()] = counts[j]
    return d


def build_lookup(counts):
    ci = {}
    rv = {}
    for k, v in counts.items():
        ci[k.lower()] = v
        tok = rv_token(k)
        if tok:
            rv[tok] = v
    return ci, rv


def lookup_count(sub, counts, ci, rv):
    if sub in counts:
        return counts[sub]
    if sub.lower() in ci:
        return ci[sub.lower()]
    tok = rv_token(sub)
    if tok and tok in rv:
        return rv[tok]
    return None


def fmt_table(title, items):
    out = [title, "  %-26s %-34s %6s %8s %9s" % ("drug", "cancer subtype", "conc", "N_events", "patients")]
    for d, s, cor, n, pt in items:
        out.append("  %-26s %-34s %6.2f %8d %9s" % (d, s, cor, n, ("-" if pt is None else str(pt))))
    if len(items) == 0:
        out.append("  (none)")
    return "\n".join(out)


def main():
    opts, _ = getopt.getopt(sys.argv[1:], '', ['concordance=', 'atlas=', 'threshold=', 'out=',
                                               'counts=', 'mergedresult='])
    concordance = atlas = out = None
    threshold = 0.3
    counts_tsv = merged = None
    for o, a in opts:
        if o == '--concordance':
            concordance = a
        elif o == '--atlas':
            atlas = a
        elif o == '--threshold':
            threshold = float(a)
        elif o == '--out':
            out = a
        elif o == '--counts':
            counts_tsv = a
        elif o == '--mergedresult':
            merged = a
    if not concordance or not out:
        print("usage: rank_concordance.py --concordance F --out F [--atlas NAME --threshold 0.3 "
              "--counts tsv | --mergedresult matrix]")
        sys.exit(2)
    atlas = atlas or 'atlas'

    counts = {}
    csrc = "none"
    try:
        if counts_tsv:
            counts = load_counts_tsv(counts_tsv); csrc = "tsv:%s" % counts_tsv
        elif merged:
            counts = load_counts_mergedresult(merged); csrc = "mergedresult:%s" % merged
    except Exception as e:
        sys.stderr.write("rank_concordance: could not load counts (%s) -> ranking without patient counts\n" % e)
        counts = {}
    ci, rv = build_lookup(counts)

    _subs, rows = parse_concordance(concordance)
    enriched = []
    for drug, sub, cor, anti, n in rows:
        dn = clean_name(drug, 'drug')
        sn = clean_name(sub, 'subtype')
        pt = lookup_count(sn, counts, ci, rv) if counts else None
        enriched.append((dn, sn, cor, n, pt))

    reversal = sorted([r for r in enriched if r[2] < threshold],
                      key=lambda r: (-r[3], -(r[4] or 0)))
    mimic = sorted([r for r in enriched if r[2] > 0.7],
                   key=lambda r: (-r[3], -(r[4] or 0)))[:12]

    body = (
        "%s concordance ranking (drug splicing signature vs cancer subtypes)\n" % atlas +
        "concordance: 1 = drug MIMICS the subtype (bad), 0 = drug REVERSES it (therapeutic). "
        "< %.2f = candidate.\n" % threshold +
        "N_events = overlapping splicing events; patients = subtype cohort size (%s).\n\n" % csrc +
        fmt_table("=== REVERSAL CANDIDATES (concordance < %.2f), ranked by overlapping events ===" % threshold,
                  reversal) +
        "\n\n" +
        fmt_table("=== MIMIC / AVOID (concordance > 0.70) ===", mimic) + "\n"
    )
    try:
        f = open(out, 'w')
        try:
            f.write(body)
        finally:
            f.close()
    except Exception as e:
        # was uncaught -> a write failure (unwritable dir / disk full) crashed the ranker with a bare
        # traceback and left no ranked output + no clear reason. Log distinctly and fail visibly instead.
        sys.stderr.write("rank_concordance: FAILED to write ranked output %s (%s)\n" % (out, e))
        sys.exit(1)
    print("rank_concordance: %d cells (%d reversal candidates) -> %s" % (len(enriched), len(reversal), out))


if __name__ == '__main__':
    main()
