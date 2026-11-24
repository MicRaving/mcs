"""Check that ``Rationale_Notes`` still agrees with the record it describes.

``Rationale_Notes`` is prose written when a record was coded.  When a later
pass changes a field, the prose does not change with it, and the record ends
up contradicting itself: the note says "routed to fact_checking_training"
while ``Intervention_Type`` now lists three types.

This script finds that drift mechanically.  For every record it looks for
``FIELD = VALUE`` statements in the notes where ``FIELD`` is a field of that
same record, and checks the stated value against the record's actual value.

Products
--------
``Results/rationale_consistency.csv``   one row per mismatch
"""
import csv
import json
import glob
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")
OUT = os.path.join(RESULTS, "rationale_consistency.csv")

#: "Field = value" / "Field: value" in prose
PAT = re.compile(r"\b([A-Z][A-Za-z_0-9]{2,})\s*[=:]\s*([^\n.;]{1,60})")

#: fields whose value is a code list; the prose usually names one code
LIST_FIELDS = {"Intervention_Type", "dv_domain", "score_types",
               "Media_Support", "Comm_Strategies", "Practice_Strategy",
               "Attention_Cues", "Retrieval_Practice", "Contrasting_Cases",
               "Concept_Labeling", "Interleaving", "Feedback_Mechanics",
               "ICAP_Role", "Social_Context", "Affective_Design",
               "Self_Efficacy", "Setting", "Country", "Stimuli_Topic",
               "Stimuli", "Technique_Taxonomy", "Intervention_Components"}

#: phrases that introduce a routing statement
ROUTING = re.compile(r"rout(?:ed|ing)|coded as|coded to|assigned to", re.I)


def values_of(rec, field):
    v = rec.get(field)
    if v is None:
        return set()
    if isinstance(v, (list, tuple)):
        return {str(x).strip().lower() for x in v}
    return {str(v).strip().lower()}


def codes_in(text):
    """The snake_case codes mentioned in a prose fragment."""
    return {m.group(0).lower() for m in re.finditer(r"\b[a-z][a-z0-9_]{3,}\b", text)}


def tidy(s):
    """Strip the punctuation prose wraps a value in."""
    s = (s or "").strip().strip("'\"`")
    s = s.rstrip(").,;:")
    return s.strip().lower()


#: prose words that describe a value rather than being one
PROSE_WORDS = {"mix", "mixed", "multiple", "both", "combo", "list", "several",
               "various", "and", "or", "the", "a", "an", "other", "unknown",
               "tbc", "none", "n/a", "not", "reported", "unspecified"}


def leading_value(fragment):
    """The value a prose fragment starts with.

    ``"2 (Guided Inquiry) although the tablet arm ..."`` -> ``"2"``.
    ``"debunking): the anti-ESB component ..."``          -> ``"debunking"``.
    """
    frag = (fragment or "").strip()
    frag = re.split(r"[\s(\[]", frag, 1)[0]
    frag = frag.rstrip(").,;:'\"").strip()
    return frag.lower()


def agrees(stated, actual):
    """Does the stated value agree with the record's value?

    Conservative on purpose: a value that is a prefix of the record's value
    (``debunking`` vs ``debunking_and_rebuttals``) agrees, because the prose
    may abbreviate; only a value that shares nothing with the record's value
    is a mismatch.
    """
    st = tidy(stated)
    if not st or st in PROSE_WORDS:
        return True
    # a numeric field cannot hold a symbolic reference ("N_Recruited =
    # n_analyzed" is a statement about equality, not a value)
    if all(re.fullmatch(r"-?\d+(\.\d+)?", a) for a in actual) and \
            not re.fullmatch(r"-?\d+(\.\d+)?", st):
        return True
    for a in actual:
        if st == a:
            return True
        if st in a or a in st:
            return True
        if re.fullmatch(r"-?\d+(\.\d+)?", st):
            m = re.match(r"-?\d+(\.\d+)?", a)
            if m and m.group(0) == st:
                return True
    return False


def main():
    rows = []
    for p in sorted(glob.glob(os.path.join(ROOT, "Extraction", "*.json"))):
        key = os.path.basename(p)[:-5]
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        recs = d if isinstance(d, list) else d.get("experiments", [])
        for i, rec in enumerate(recs):
            notes = rec.get("Rationale_Notes")
            if not notes or not isinstance(notes, str):
                continue
            for m in PAT.finditer(notes):
                field = m.group(1)
                if field not in rec or field == "Rationale_Notes":
                    continue
                actual = values_of(rec, field)
                if not actual:
                    continue
                stated = leading_value(m.group(2))
                if not stated or stated in PROSE_WORDS:
                    continue
                if not agrees(stated, actual):
                    rows.append(dict(
                        study_key=key, record=i, field=field,
                        stated=stated[:120],
                        actual="; ".join(sorted(actual))[:160],
                        kind="prose states a value the record no longer has"))

    with open(OUT, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["study_key", "record", "field",
                                           "stated", "actual", "kind"])
        w.writeheader()
        w.writerows(rows)

    print("rationale/record mismatches: %d" % len(rows))
    by_key = Counter(r["study_key"] for r in rows) if rows else {}
    for k, n in by_key.most_common(15):
        print("   %-44s %d" % (k[:44], n))
    print("wrote", OUT)
    return 0


from collections import Counter  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
