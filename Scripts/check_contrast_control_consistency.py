"""Check that ``contrast_type`` agrees with the record's own ``control_group_type``.

The master pass found the same self-contradiction twice: a record codes
``contrast_type = 1`` (intervention vs a no-treatment control) while its own
``control_group_type`` says the comparison arm was a placebo
(``Celio & Lisman (2014)``) or another active format (``Dugas et al. (2024)``).
The contrast label then says "no treatment" about a comparison that received
something.

This script finds every record where the two disagree, so the class can be
fixed in one pass rather than one key at a time.

Products
--------
``Results/contrast_control_consistency.csv``
"""
import csv
import json
import os
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")
OUT = os.path.join(RESULTS, "contrast_control_consistency.csv")

#: control_group_type -> what it means (from the schema's option list).
#:
#: The schema's own description is
#: "1=No-treatment/Passive, 2=Placebo (unrelated content),
#: 3=Active (alternative intervention), 4=Waitlist, 5=Misinformation-only,
#: 6=Other."  A **waitlist** control (4) is untreated *at the time of
#: measurement* - it receives the intervention after the posttest - so a
#: row that says "intervention vs no-treatment control" against a
#: waitlist is consistent, not a contradiction.  An earlier version of
#: this checker read 4 as "untreated but misinformation-exposed", which
#: flagged every waitlist contrast as a contradiction.
CONTROL_MEANING = {
    1: "no-treatment / passive control",
    2: "placebo / attention control",
    3: "active (alternative intervention) control",
    4: "waitlist control (untreated at measurement)",
    5: "misinformation-only (no intervention) control",
    6: "other",
}

#: the control types that ARE untreated at the time of measurement, so a
#: row may call its contrast "intervention vs no-treatment control"
UNTREATED_AT_MEASUREMENT = (1, 4)

#: contrast_type -> what it says the comparison was
CONTRAST_MEANING = {
    1: "intervention vs no-treatment control",
    2: "treatment vs active/placebo control",
    3: "pre-exposure / prebunking comparison",
    4: "within-arm or dose-response",
    5: "no-intervention comparison",
    7: "non-contemporaneous control (e.g. follow-up vs post-test)",
    10: "factorial interaction",
    11: "omnibus model term",
    13: "non-intervention factor",
}


def main():
    rows = []
    for p in sorted(os.listdir(os.path.join(ROOT, "Extraction"))):
        if not p.endswith(".json"):
            continue
        key = p[:-5]
        path = os.path.join(ROOT, "Extraction", p)
        try:
            d = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        recs = d if isinstance(d, list) else d.get("experiments", [])
        for i, rec in enumerate(recs):
            cgt = rec.get("control_group_type")
            try:
                cgt = int(cgt)
            except Exception:
                cgt = None
            if cgt is None:
                continue
            # The rows of a record do not all have to use the record's
            # control arm: a record can carry a second contrast between
            # two *active* arms.  The record's `control_group_type` only
            # speaks about the row whose arms ARE the record's arms, so
            # the check compares the arm PAIR, not the label.
            arm_pairs_of_1 = set()
            for dv in rec.get("DV_Metrics") or []:
                if not isinstance(dv, dict):
                    continue
                try:
                    ct1 = int(dv.get("contrast_type"))
                except Exception:
                    continue
                if ct1 == 1:
                    arm_pairs_of_1.add((
                        str(dv.get("group1_label") or "").strip().lower(),
                        str(dv.get("group2_label") or "").strip().lower()))
            for j, dv in enumerate(rec.get("DV_Metrics") or []):
                if not isinstance(dv, dict):
                    continue
                try:
                    ct = int(dv.get("contrast_type"))
                except Exception:
                    continue
                pair = (str(dv.get("group1_label") or "").strip().lower(),
                        str(dv.get("group2_label") or "").strip().lower())
                # contrast_type 1 claims a no-treatment comparison
                if ct == 1 and cgt not in UNTREATED_AT_MEASUREMENT:
                    rows.append(dict(
                        study_key=key, record=i, dv_index=j,
                        dv_name=str(dv.get("dv_name"))[:70],
                        contrast_type=ct,
                        contrast_says=CONTRAST_MEANING.get(ct, ""),
                        control_group_type=cgt,
                        control_is=CONTROL_MEANING.get(cgt, ""),
                        eligible_primary=dv.get("eligible_primary")))
                # contrast_type 2 claims an active/placebo comparison
                elif ct == 2 and cgt == 1:
                    # only a contradiction if this row uses the SAME arms
                    # as the record's no-treatment contrast; a row that
                    # contrasts two other arms is not governed by the
                    # record's control_group_type at all
                    if pair in arm_pairs_of_1:
                        rows.append(dict(
                            study_key=key, record=i, dv_index=j,
                            dv_name=str(dv.get("dv_name"))[:70],
                            contrast_type=ct,
                            contrast_says=CONTRAST_MEANING.get(ct, ""),
                            control_group_type=cgt,
                            control_is=CONTROL_MEANING.get(cgt, ""),
                            eligible_primary=dv.get("eligible_primary")))

    with open(OUT, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "study_key", "record", "dv_index", "dv_name", "contrast_type",
            "contrast_says", "control_group_type", "control_is",
            "eligible_primary"])
        w.writeheader()
        w.writerows(rows)

    keys = {r["study_key"] for r in rows}
    elig = sum(1 for r in rows if str(r["eligible_primary"]) == "1")
    print("contrast_type / control_group_type contradictions: %d rows in %d keys"
          % (len(rows), len(keys)))
    print("of which the row is eligible_primary = 1: %d" % elig)
    print()
    for k, n in Counter(r["study_key"] for r in rows).most_common(12):
        print("   %-44s %d" % (k[:44], n))
    print("\nwrote", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
