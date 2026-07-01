"""Shared cell-line helpers used by prep_ai.py and build_final.py.

- clean_struct_cell: validate/clean a structured `cell line` tag value (reject junk,
  tissue/cell-type descriptions without a code, overly long values).
- extract_cell_line: regex fallback to pull a cell line (or coarse bucket) from a free-text
  sample title when no structured tag and no AI result is available.
"""
import re

BAD_CELL = {"no", "none", "n/a", "na", "not applicable", "-", "missing", "control",
            "wild type", "wt", "parental", "untreated", "", "."}


def clean_struct_cell(val):
    """Return a usable cell-line name from a structured tag value, or None."""
    if not val:
        return None
    v = val.split(",")[0].strip()  # pooled multi-line value -> take first
    if v.lower() in BAD_CELL or len(v) > 40:
        return None
    # A verbose description names the line as a parenthetical CODE or a "<CODE> cell line" phrase, e.g.
    # "MDS-derived cell line (MDS-L)" / "the MDS-L cell line" -> extract the CODE. WITHOUT this, a line whose
    # code has NO DIGIT (e.g. MDS-L) is wrongly rejected by the tissue/descriptor heuristic below (it contains
    # the word "cell" and has no digit), so prep_ai falls back to classifying the TITLE and the AI mis-reads
    # the experimental condition as the cell line (hit LIVE: GSE61052's 23 MDS-L samples were bucketed as
    # P95H/Delta_8aa/Unknown, so the whole study dropped out of the MDS-L selection).
    m = (re.search(r'cell\s*lines?\s*\(\s*([A-Za-z0-9][\w.\-/ ]{0,24}?)\s*\)', v, re.I)
         or re.search(r'(?:^|\bthe\s+)([A-Za-z0-9][\w.\-/]{1,24})\s+cell\s*lines?\b', v, re.I))
    if m:
        code = m.group(1).strip()
        if re.search(r'[A-Za-z]', code) and code.lower() not in BAD_CELL:
            return code
    # looks like a cell *type* / tissue / patient descriptor (no alphanumeric code) -> not a line
    if re.search(r'\bcells?\b|tissue|biopsy|tumou?r|patient|donor|pbmc|organoid', v, re.I) \
            and not re.search(r'\d', v):
        return None
    return v


def extract_cell_line(title, study_acc=""):
    """Best-effort cell line / bucket from a free-text title (last-resort fallback)."""
    t = title.strip()
    if re.search(r'\bA549\b', t) or t.startswith("A549_"):
        return "A549"
    m = re.match(r'QHM_(\w+?)_', t)
    if m:
        return m.group(1)
    if re.match(r'(HepG2|Hepg2),?\s', t, re.I) or re.search(r'\b[Hh]ep[Gg]2\b', t):
        return "HepG2"
    m = re.match(r'^([\w\-\.]+)\s+cells?[,\s]', t, re.I)
    if m and len(m.group(1)) > 2:
        return m.group(1)
    known = [
        ("MCF-7", r'\bMCF[\-]?7\b'), ("PC9", r'\bPC9\b'), ("A375", r'\bA375\b'),
        ("K562", r'\bK562\b'), ("HCT116", r'\bHCT116\b'), ("HCC827", r'\bHCC827\b'),
        ("MDA-MB-231", r'\bMDA[\-_]MB[\-_]231\b'), ("SW480", r'\bSW480\b'),
        ("OVCAR3", r'\bOVCAR3\b'), ("SW1783", r'\bSW1783\b'), ("HT29", r'\bHT29\b'),
        ("UMUC9", r'\bUMUC9\b'), ("5637", r'\b5637\b'), ("HT1197", r'\bHT1197\b'),
        ("H460", r'\bH460\b'), ("H2228", r'\bH2228\b'), ("DIPGXIII", r'\bDIPGXIII\b'),
        ("iPSC-CM", r'\biPSC[\-]?CM\b'), ("RPE1", r'\bRPE1\b'),
        ("HEK293T", r'\b[Hh][Ee][Kk]293T?\b'), ("BEAS-2B", r'\bBEAS[\-]?2B\b'),
        ("HepaRG", r'\bHepaRG\b'), ("MOLM-13", r'\bMOLM[\-]?13\b'),
        ("OCI-AML3", r'\bOCI[\-]AML3\b'), ("TF-1", r'\bTF[\-]?1\b'),
        ("AGS", r'\bAGS\b'), ("Saos2", r'\bSaos2\b'), ("143B", r'\b143B\b'),
        ("MCF-12A", r'\bMCF[\-]?12A\b'), ("MEC1", r'\bMEC1\b'),
        ("SKMEL5", r'\bSKMEL5\b'), ("Jurkat", r'\bJurkat\b'),
        ("LUHMES", r'\bLUHMES\b'), ("U937", r'\bU937\b'), ("THP-1", r'\bTHP[\-]?1\b'),
        ("RPMI8226", r'\bRPMI[\-]?8226\b'), ("SH-SY5Y", r'\bSH[\-]?SY5Y\b'),
        ("Caco-2", r'\bCaco[\-]?2\b'), ("LNCaP", r'\bLNCaP\b'), ("22Rv1", r'\b22Rv1\b'),
        ("PANC-1", r'\bPANC[\-]?1\b'), ("MiaPaCa-2", r'\bMiaPaCa\b'),
        ("BT-549", r'\bBT[\-]?549\b'), ("T47D", r'\bT47D\b'),
    ]
    for name, pat in known:
        if re.search(pat, t):
            return name
    if re.search(r'TCGA|tumor tissue|normal adjacent', t, re.I):
        return "TCGA_TISSUE"
    if re.search(r'organoid', t, re.I):
        return "ORGANOID"
    if re.search(r'primary.*hepatocyte', t, re.I):
        return "PRIMARY_HEPATOCYTE"
    if re.search(r'\bwhole blood\b', t, re.I):
        return "WHOLE_BLOOD"
    if re.search(r'\bpatient\b|\bdonor\b', t, re.I):
        return "PATIENT_SAMPLE"
    if re.search(r'\biPSC\b', t):
        return "iPSC"
    return "UNRESOLVED"
