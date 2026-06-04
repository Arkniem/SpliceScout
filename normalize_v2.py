"""Improved structural normalizer for treatment-field values (no drug-name guessing).
Collapses dose / duration / replicate / condition tokens (underscore-, space-, comma-,
or paren-delimited) so the same drug at multiple doses counts once. Keeps synonym
parens like (GDC-0941). Used by build_final.py."""
import re

NUM = r"\d+\.?\d*"
UNIT = (r"(?:[unmpµμ]m|[unmpµμ]g/?m?l?|mg/?m?l?|ng/?m?l?|g/ml|microg/?m?l?|"
        r"microm(?:olar)?|nanomolar|millimolar|mg/kg|percent|%)")

CONTROL_VALUES = {
    "dmso", "vehicle", "control", "untreated", "none", "no treatment", "no drug",
    "pbs", "water", "mock", "ctrl", "na", "n/a", "missing", "not applicable",
    "unstimulated", "untreated control", "no", "-", "--", "vehicle control",
    "negative control", "ethanol", "etoh", "wild type", "wt", "parental",
}


# multi-clause / combination markers -> value is a sentence, not a single compound.
# For these we strip ONLY safe trailing tokens (no greedy mid-string removal) and
# otherwise leave the text for AI Pass A, so we never mangle a drug name.
_COMPLEX = re.compile(r"(\s\+\s|\sand\s|\swith\b|\sor\s|followed|transfect|"
                      r"cocultur|co-cultur|supplement|grown in|:|;|/kg|non-demult)", re.I)


def normalize_compound(val):
    v = (val or "").strip().strip(",;")
    complex_val = bool(_COMPLEX.search(v)) or len(v) > 48

    # trailing dose inside parens, e.g. '(100 mg/kg P.O.)'  (synonym parens kept)
    v = re.sub(r"\s*\(\s*" + NUM + r"\s*" + UNIT + r"[^)]*\)\s*$", "", v, flags=re.I)
    # trailing replicate / condition / batch tags (repeat a few times)
    for _ in range(4):
        v = re.sub(r"[_,\s]+(?:rep|replicate|con|cond|condition|batch|set|donor|day)"
                   r"\s*\d+\b\.?$", "", v, flags=re.I)
    # trailing descriptor words
    v = re.sub(r"[\s_]+(?:drug|treated|treatment|exposure)\b\.?\s*$", "", v, flags=re.I)

    if complex_val:
        # only strip a dose sitting at the very end; never eat mid-string text
        v = re.sub(r"[_,\s]+" + NUM + r"[_\s]*" + UNIT + r"\s*$", "", v, flags=re.I)
        return v.strip().strip(",;_").strip()

    # simple single-compound value: full normalization
    v = re.sub(r"[_,\s]+" + NUM + r"[_\s]*" + UNIT + r"\b.*$", "", v, flags=re.I)
    v = re.sub(r"^" + NUM + r"[_\s]*" + UNIT + r"[_\s]+", "", v, flags=re.I)
    v = re.sub(r"^" + NUM + r"\s*(?:h|hr|hrs|hours|d|day|days)[_\s]+", "", v, flags=re.I)
    return v.strip().strip(",;_").strip()


def is_control(val):
    v = (val or "").strip().lower()
    if v in CONTROL_VALUES:
        return True
    residue = v
    for pat in (r"dimethyl\s*sulfoxide", r"\bdmso\b", r"\bv/v\b", r"\bvehicle\b",
                r"\bethanol\b", r"\betoh\b", r"\bpbs\b", r"\bwater\b", r"\bcontrol\b",
                r"\bincubation\b", r"\d+\.?\d*\s*%", r"\d+\.?\d*\s*h(?:rs?|ours?)?\b",
                r"\(.*?\)", r"[\(\),/.%\s]+"):
        residue = re.sub(pat, "", residue, flags=re.I)
    return residue == ""


def clean_compound(val):
    """Return canonical-ish name, or None if it's a control / empty."""
    n = normalize_compound(val)
    if not n or is_control(n):
        return None
    return n
# Signed Nicholas Krol
