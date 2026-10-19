"""Merge / compare the Space Bunny (SB) coding route against the canonical route.

Reads whatever SB artefacts exist and writes:
  Results/sb_coding_agreement.csv          per study: canonical vs SB extraction agreement
  Results/sb_harmonization_agreement.csv   per study: canonical vs SB wide table shape + DV overlap
  Results/sb_route_summary.json            headline counts (codings, harmonizers, reproductions)

Read-only apart from those three products.

Run:  python Scripts/sb_merge.py

Importing this module is side-effect free: the three products are written only when the
script is run as ``__main__``.  Previously the merge ran at import time, so anything that
merely imported this module to reuse ``stats``/``verdict``/``jaccard`` silently rewrote
``Results/`` — a subagent checking that the agreement readers accept a coding did exactly
that and refreshed the products as a side effect.
"""
import glob
import json
import os
import re
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "Scripts"))
try:
    import contract as CT
except Exception:
    CT = None

RES = os.path.join(ROOT, "Results")
os.makedirs(RES, exist_ok=True)


def sb_keys():
    suf = " - SB.json"
    return sorted(os.path.basename(p)[:-len(suf)]
                  for p in glob.glob(os.path.join(ROOT, "Codings", "*" + suf)))


def load(p):
    try:
        j = json.load(open(p, encoding="utf-8"))
    except Exception:
        return None
    return j.get("experiments", j) if isinstance(j, dict) else j


def stats(recs):
    if recs is None:
        return None
    n_dv = sum(len(r.get("DV_Metrics") or []) for r in recs)
    elig = sum(sum(1 for d in (r.get("DV_Metrics") or []) if str(d.get("eligible_primary")) == "1")
               for r in recs)
    doms = sorted({str(d.get("dv_domain")) for r in recs for d in (r.get("DV_Metrics") or [])
                   if d.get("dv_domain")})
    pols = {str(d.get("dv_polarity")) for r in recs for d in (r.get("DV_Metrics") or [])
            if d.get("dv_polarity") is not None}
    names = sorted({str(d.get("dv_name")) for r in recs for d in (r.get("DV_Metrics") or [])})
    return dict(exp=len(recs), dv=n_dv, elig=elig, domains=doms, polarity=sorted(pols), names=names)


def verdict(p):
    if not os.path.isfile(p):
        return None
    t = open(p, encoding="utf-8", errors="replace").read(3000)
    m = re.search(r"reproduced:\s*\**\s*(not reproducible|yes|partial|no)\b", t, re.I)
    return m.group(1).lower() if m else "(unparsed)"


def jaccard(a, b):
    a, b = set(a), set(b)
    return round(len(a & b) / len(a | b), 3) if (a | b) else None


def main():
    keys = sb_keys()
    rows = []
    for k in keys:
        can = stats(load(os.path.join(ROOT, "Extraction", k + ".json")))
        sb = stats(load(os.path.join(ROOT, "Codings", k + " - SB.json")))
        rows.append(dict(
            study_key=k,
            can_exp=can["exp"] if can else None, sb_exp=sb["exp"] if sb else None,
            can_dv=can["dv"] if can else None, sb_dv=sb["dv"] if sb else None,
            can_elig=can["elig"] if can else None, sb_elig=sb["elig"] if sb else None,
            domain_jaccard=jaccard(can["domains"], sb["domains"]) if (can and sb) else None,
            polarity_same=(can["polarity"] == sb["polarity"]) if (can and sb) else None,
            dvname_jaccard=jaccard(can["names"], sb["names"]) if (can and sb) else None,
            exp_delta=(sb["exp"] - can["exp"]) if (can and sb) else None,
            elig_delta=(sb["elig"] - can["elig"]) if (can and sb) else None,
            sb_valid=(CT.dv_has_effect is not None),   # placeholder; B_validate is the real check
        ))
    cod = pd.DataFrame(rows)
    if len(cod):
        cod.to_csv(os.path.join(RES, "sb_coding_agreement.csv"), index=False, encoding="utf-8-sig")

    # harmonized-shape agreement
    hrows = []

    def shape(f):
        if not os.path.isfile(f):
            return (None, None, set())
        h = pd.read_csv(f, encoding="utf-8-sig", nrows=0)
        cols = list(h.columns)
        return (sum(1 for _ in open(f, encoding="utf-8-sig")) - 1, len(cols),
                {c for c in cols if c.startswith("dv_") and c.endswith("_01")})

    for p in glob.glob(os.path.join(ROOT, "Datasets", "*", "* - harmonized - SB.csv")):
        key = os.path.basename(os.path.dirname(p))
        can = os.path.join(ROOT, "Datasets", key, key + " - harmonized.csv")
        cr, cc, cdv = shape(can)
        sr, sc, sdv = shape(p)
        hrows.append(dict(study_key=key, can_rows=cr, sb_rows=sr, can_cols=cc, sb_cols=sc,
                          dv01_jaccard=jaccard(cdv, sdv),
                          sb_repro_verdict=verdict(os.path.join(ROOT, "Reproduction", key + " - SB.md"))))
    harm = pd.DataFrame(hrows)
    if len(harm):
        harm.to_csv(os.path.join(RES, "sb_harmonization_agreement.csv"), index=False, encoding="utf-8-sig")

    summary = {"sb_codings": len(cod), "sb_harmonizers": len(harm),
               "sb_reproductions": len([p for p in glob.glob(os.path.join(ROOT, "Reproduction", "*.md"))
                                        if p.endswith(" - SB.md")]),
               "canonical_extractions": len(glob.glob(os.path.join(ROOT, "Extraction", "*.json")))}
    json.dump(summary, open(os.path.join(RES, "sb_route_summary.json"), "w", encoding="utf-8"),
              indent=2, ensure_ascii=False)
    print(json.dumps(summary, indent=2))
    if len(cod):
        print(cod.head(8).to_string(index=False))


if __name__ == "__main__":
    main()
