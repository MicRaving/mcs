"""Correct a fact the paper states but the canonical route mis-codes.

``Abascal (2023)`` carries ``Gender_Ratio = {"female": 63, "male": 37}``.
The paper (Ch. 2, Results) states:

    "The median age of participants was 28 years (IQR 22-39), 63% (N=998)
     identified as female, and 37% spoke an indigenous language at home
     (28% spoke Kaqchikel and 9% K'iche)."

The 37 is the share speaking an indigenous language, not a male share.  The
paper reports no male percentage for the analysed sample (n=1572); the
35% / 65% male-female split it does report belongs to a different
subsample (17 interviews).  Both the canonical and the SB route carry the
misread; the MM route carries the 17-interview subsample.

The strictly sourced value is therefore ``{"female": 63}`` — the one share
the paper states for the analysed sample.  Asserting ``male: 37`` would put
an unsourced number in the coding.

Run:  python Scripts/fix_abascal_gender_ratio.py
"""
import json
import os
import shutil
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = "Abascal (2023)"
NEW = {"female": 63}
SOURCE = ("Ch. 2, Results p. 46: \"The median age of participants was 28 years "
          "(IQR 22-39), 63% (N=998) identified as female, and 37% spoke an "
          "indigenous language at home (28% spoke Kaqchikel and 9% "
          "K'iche).\" The 37% is the indigenous-language share, not a male "
          "share; the paper reports no male percentage for the analysed "
          "sample (n=1572). The 35%/65% male-female split it does report "
          "belongs to a different subsample (17 interviews).")


def main():
    path = os.path.join(ROOT, "Extraction", KEY + ".json")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    recs = data if isinstance(data, list) else data.get("experiments", [])
    if not recs:
        print("no records in", path, file=sys.stderr)
        return 1
    before = recs[0].get("Gender_Ratio")
    if before == NEW:
        print("already correct: %s" % (NEW,))
        return 0

    stamp = datetime.now().strftime("%Y-%m-%d")
    bak_dir = os.path.join(ROOT, "archive", "extraction_fact_fixes_" + stamp)
    os.makedirs(bak_dir, exist_ok=True)
    shutil.copy2(path, os.path.join(bak_dir, KEY + ".json"))

    for rec in recs:
        rec["Gender_Ratio"] = dict(NEW)
        note = rec.get("Rationale_Notes")
        line = ("GENDER_RATIO CORRECTION (%s): %r -> %r. %s"
                % (stamp, before, NEW, SOURCE))
        rec["Rationale_Notes"] = (note + "\n" + line) if note else line

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print("%s: Gender_Ratio %r -> %r" % (KEY, before, NEW))
    print("backup:", os.path.join(bak_dir, KEY + ".json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
