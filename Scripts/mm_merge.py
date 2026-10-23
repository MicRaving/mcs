"""Merge / compare the MM (MiniMax) coding route against the canonical route.

Mirrors ``Scripts/sb_merge.py`` for the second MM coding route.  Writes:

  Results/mm_coding_agreement.csv    per study: canonical vs MM agreement
  Results/mm_route_summary.json      headline counts for the MM route

The MM route is an *independent* re-coding of the same corpus, so the numbers
here are inter-rater agreement, not corrections: a row where the two coders
disagree is a point for the adjudication, not an error in either file.

Read-only apart from those two products.

Run:  python Scripts/mm_merge.py
"""
import glob
import json
import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "Results")
os.makedirs(RES, exist_ok=True)

SUF = " - MM.json"


def mm_keys():
    return sorted(os.path.basename(p)[: -len(SUF)]
                  for p in glob.glob(os.path.join(ROOT, "Codings", "*" + SUF)))


def load(p):
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            j = json.load(fh)
    except Exception:  # noqa: BLE001
        return None
    return j.get("experiments", j) if isinstance(j, dict) else j


def stats(recs):
    if recs is None:
        return None
    dv_rows = [d for r in recs if isinstance(r, dict) for d in (r.get("DV_Metrics") or [])]
    elig = sum(1 for d in dv_rows if str(d.get("eligible_primary")) == "1")
    doms = sorted({str(d.get("dv_domain")) for d in dv_rows if d.get("dv_domain")})
    pols = sorted({str(d.get("dv_polarity")) for d in dv_rows if d.get("dv_polarity") is not None})
    names = sorted({str(d.get("dv_name")) for d in dv_rows if d.get("dv_name")})
    crits = sorted({crit(d) for r in recs if isinstance(r, dict)
                    for d in [r] if (crit(d) is not None)})
    return dict(exp=len(recs), dv=len(dv_rows), elig=elig, domains=doms,
                polarity=pols, names=names, criteria=crits)


def crit(rec):
    """The screening criterion of a record, normalised to a tuple."""
    v = rec.get("Fulltext_Exclusion_Criterion")
    if v in (None, "", [], ()):
        return ()
    if isinstance(v, str):
        v = [v]
    return tuple(sorted(str(x) for x in v))


def jaccard(a, b):
    a, b = set(a), set(b)
    return round(len(a & b) / len(a | b), 3) if (a | b) else None


def main():
    keys = mm_keys()
    rows = []
    for k in keys:
        can = stats(load(os.path.join(ROOT, "Extraction", k + ".json")))
        mm = stats(load(os.path.join(ROOT, "Codings", k + SUF)))
        rows.append(dict(
            study_key=k,
            can_exp=can["exp"] if can else None, mm_exp=mm["exp"] if mm else None,
            can_dv=can["dv"] if can else None, mm_dv=mm["dv"] if mm else None,
            can_elig=can["elig"] if can else None, mm_elig=mm["elig"] if mm else None,
            domain_jaccard=jaccard(can["domains"], mm["domains"]) if (can and mm) else None,
            polarity_same=(can["polarity"] == mm["polarity"]) if (can and mm) else None,
            dvname_jaccard=jaccard(can["names"], mm["names"]) if (can and mm) else None,
            criteria_same=(can["criteria"] == mm["criteria"]) if (can and mm) else None,
            exp_delta=(mm["exp"] - can["exp"]) if (can and mm) else None,
            elig_delta=(mm["elig"] - can["elig"]) if (can and mm) else None,
            dv_delta=(mm["dv"] - can["dv"]) if (can and mm) else None,
        ))
    cod = pd.DataFrame(rows)
    if len(cod):
        cod.to_csv(os.path.join(RES, "mm_coding_agreement.csv"),
                   index=False, encoding="utf-8-sig")

    matched = cod if len(cod) else pd.DataFrame()
    summary = {
        "mm_codings": len(cod),
        "canonical_extractions": len(glob.glob(os.path.join(ROOT, "Extraction", "*.json"))),
        "mm_dv_rows": int(cod["mm_dv"].sum()) if len(cod) else 0,
        "mm_eligible_rows": int(cod["mm_elig"].sum()) if len(cod) else 0,
        "canonical_dv_rows": int(cod["can_dv"].sum()) if len(cod) else 0,
        "canonical_eligible_rows": int(cod["can_elig"].sum()) if len(cod) else 0,
        "keys_with_identical_eligible_count": int((cod["elig_delta"] == 0).sum()) if len(cod) else 0,
        "keys_with_identical_exclusion_criterion": int((cod["criteria_same"] == True).sum()) if len(cod) else 0,
        "keys_with_identical_polarity_set": int((cod["polarity_same"] == True).sum()) if len(cod) else 0,
        "mean_dvname_jaccard": round(float(cod["dvname_jaccard"].mean()), 3) if len(cod) else None,
        "mean_domain_jaccard": round(float(cod["domain_jaccard"].mean()), 3) if len(cod) else None,
    }
    json.dump(summary, open(os.path.join(RES, "mm_route_summary.json"), "w", encoding="utf-8"),
              indent=2, ensure_ascii=False)
    print(json.dumps(summary, indent=2))
    if len(cod):
        print(cod.head(8).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
