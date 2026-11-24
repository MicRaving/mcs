"""Audit the SIGN of every pooled row against the row's own effect_favors.

Two master agents found the same class of bug in different studies:
``Dutta-Powell et al. (2024)`` stored the **absolute** value of the
paper's coefficient, and ``Kaňková & Matthes (2026)`` stored ``d = +b/SD``
for seven rows whose contrast is *control - intervention*.  Both flip the
sign of a pooled effect.

The pipeline orients an effect by the row's polarity
(``contract.polarity_sign``), so the oriented effect is
``yi * polarity_sign``: positive means group 1 is favoured, negative means
group 2 is.  A row whose ``effect_favors`` says ``group1`` while its
oriented effect is negative - or the reverse - is a **sign
inconsistency**: one of the two is wrong, and the pooled number flips
depending on which.

The audit reports the rows; it does not decide which side is wrong, because
only the paper can say.  The rows that carry ``neither``/``na`` or a free
-text value are left alone: a null contrast or a term with no favoured arm
has no sign to check.

Products
--------
``Results/effect_sign_consistency.csv``   the rows that disagree
"""
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")
OUT = os.path.join(RESULTS, "effect_sign_consistency.csv")

#: ``effect_favors`` values that name a side of the contrast
FAVOURS_GROUP = ("group1", "group2")

#: words that mark a label as the intervention or the control arm
TREATMENT_WORDS = ("intervention", "treatment", "experimental", "inoculat",
                    "correct", "fact-check", "prebunk", "debunk")
CONTROL_WORDS = ("control", "placebo", "waitlist", "wait-list", "no-treat",
                 "untreated", "baseline")


def polarity_sign(dv):
    p = dv.get("dv_polarity")
    if p in (0, "0", False, 0.0):
        return -1.0
    return 1.0


def as_float(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:          # NaN
        return None
    return f


def label_role(text):
    """+1 if the label reads as the intervention arm, -1 as the control."""
    low = str(text or "").lower()
    t = any(w in low for w in TREATMENT_WORDS)
    c = any(w in low for w in CONTROL_WORDS)
    if t and not c:
        return 1
    if c and not t:
        return -1
    return 0


def main():
    rows = []
    for fn in sorted(os.listdir(os.path.join(ROOT, "Extraction"))):
        if not fn.endswith(".json"):
            continue
        key = fn[:-5]
        try:
            d = json.load(open(os.path.join(ROOT, "Extraction", fn),
                               encoding="utf-8"))
        except Exception:
            continue
        recs = d if isinstance(d, list) else d.get("experiments", [])
        for i, rec in enumerate(recs):
            for j, dv in enumerate(rec.get("DV_Metrics") or []):
                if not isinstance(dv, dict):
                    continue
                favours = str(dv.get("effect_favors") or "").strip()
                if favours not in FAVOURS_GROUP:
                    continue
                yi = as_float(dv.get("yi"))
                if yi is None or yi == 0:
                    continue
                oriented = yi * polarity_sign(dv)
                said = 1 if favours == "group1" else -1
                if (oriented > 0) == (said > 0):
                    continue
                # the sign the row's own labels imply, where they are
                # unambiguous - a second, independent reading
                role = label_role(dv.get("group1_label"))
                # the paper's own coefficient, when the row carries one:
                # an independent check on the sign of the stored effect
                beta = as_float(dv.get("beta_val"))
                beta_agrees = ""
                if beta is not None and beta != 0 and yi != 0:
                    beta_agrees = "yes" if (beta > 0) == (yi > 0) else "NO"
                rows.append(dict(
                    study_key=key, record=i, dv_index=j,
                    dv_name=str(dv.get("dv_name"))[:90],
                    yi=yi, dv_polarity=dv.get("dv_polarity"),
                    oriented_effect=oriented,
                    effect_favors=favours,
                    group1_label=str(dv.get("group1_label"))[:60],
                    group2_label=str(dv.get("group2_label"))[:60],
                    comparison_label=str(dv.get("comparison_label"))[:80],
                    eligible_primary=dv.get("eligible_primary"),
                    effect_size_type_reported=dv.get(
                        "effect_size_type_reported"),
                    beta_val=dv.get("beta_val"),
                    beta_sign_agrees_with_yi=beta_agrees,
                    group1_is_treatment=role))

    with open(OUT, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else
                           ["study_key"])
        w.writeheader()
        w.writerows(rows)

    keys = {r["study_key"] for r in rows}
    elig = sum(1 for r in rows if str(r["eligible_primary"]) == "1")
    # the strongest signal: the row carries the paper's own coefficient and
    # the stored effect has the OPPOSITE sign to it
    beta_conflict = [r for r in rows
                     if r["beta_sign_agrees_with_yi"] == "NO"]
    print("rows whose effect_favors contradicts the sign of their effect: %d"
          % len(rows))
    print("   over %d study keys; %d of the rows are eligible_primary = 1"
          % (len(keys), elig))
    print("   of these, %d carry the paper's own coefficient and the stored "
          "effect has the opposite sign" % len(beta_conflict))
    print("   (%d of those are eligible_primary = 1)"
          % sum(1 for r in beta_conflict
                if str(r["eligible_primary"]) == "1"))
    print()
    print("the strongest cases (paper coefficient and stored effect "
          "disagree):")
    for r in beta_conflict[:20]:
        print("   %-34s %-34s yi=%-9s beta=%-8s %s"
              % (r["study_key"][:34], r["dv_name"][:34], r["yi"],
                 r["beta_val"], r["effect_favors"]))
    print()
    for k, n in Counter(r["study_key"] for r in rows).most_common(20):
        print("   %-44s %d" % (k[:44], n))
    print("\nwrote", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
