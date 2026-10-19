"""REPORT-ONLY polarity divergence between the two pooled layers.

The campaign deliberately keeps two independent polarity sources:

  * ``Extraction/<Key>.json`` -> ``DV_Metrics[].dv_polarity``
        consumed by ``metaanalysis.py`` (the reported-effect pool);
  * ``Datasets/<Key>/<Key> - harmonized_variables.csv`` -> ``dv_polarity``
        consumed by ``reanalysis.py`` (the SDT discrimination pool).

They are **not** required to agree: the first is the paper-level DV orientation, the second is
the harmonized per-variable orientation (and the SDT layer re-derives its own per-item polarity
from the response columns).  A divergence is therefore legitimate -- but an *accidental* one is
currently indistinguishable from a considered one, because no gate compares the two layers.

This script reports every divergence for human review and NEVER fails the build.

    python Scripts/polarity_divergence.py            # write Results/polarity_divergence.csv
    python Scripts/polarity_divergence.py --summary  # counts only

Comparison unit: study x dv_domain x veracity.  Names are matched loosely (normalised token
overlap) only to make the report readable; the verdict rests on the domain+veracity cell so it
does not depend on name spelling.
"""
from __future__ import annotations
import os, re, csv, glob, json, sys, unicodedata
from collections import defaultdict

B = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(B, "Results", "polarity_divergence.csv")
POLS = {"higher_is_better": 1, "higher_is_worse": 0, "1": 1, "0": 0}


def norm(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def toks(s):
    return set(w for w in norm(s).split() if len(w) > 2)


def pol(v):
    v = str(v or "").strip()
    return POLS.get(v, POLS.get(v.lower(), None))


def coded_rows(key):
    """{domain: {veracity: {polarity, names}}} from the coding JSON."""
    p = os.path.join(B, "Extraction", key + ".json")
    if not os.path.isfile(p):
        return {}
    try:
        j = json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}
    out = defaultdict(lambda: defaultdict(lambda: {"pols": set(), "names": []}))
    for rec in (j if isinstance(j, list) else [j]):
        if not isinstance(rec, dict):
            continue
        for dv in (rec.get("DV_Metrics") or []):
            if not isinstance(dv, dict):
                continue
            dom = dv.get("dv_domain")
            dom = dom[0] if isinstance(dom, list) and dom else (dom or "")
            ver = str(dv.get("veracity") or "").strip().lower()
            ver = ver if ver in ("true", "false") else ""
            pv = pol(dv.get("dv_polarity"))
            cell = out[str(dom).strip()][ver]
            if pv is not None:
                cell["pols"].add(pv)
            if dv.get("dv_name"):
                cell["names"].append(str(dv["dv_name"])[:80])
    return out


def sidecar_rows(key):
    p = os.path.join(B, "Datasets", key, f"{key} - harmonized_variables.csv")
    if not os.path.isfile(p):
        return {}
    out = defaultdict(lambda: defaultdict(lambda: {"pols": set(), "names": []}))
    with open(p, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            uv = (r.get("uniform_variable") or "").strip()
            if not uv.startswith("dv_"):
                continue
            dom = (r.get("dv_domain") or "").strip()
            ver = (r.get("veracity") or "").strip().lower()
            ver = ver if ver in ("true", "false") else ""
            pv = pol(r.get("dv_polarity"))
            cell = out[dom][ver]
            if pv is not None:
                cell["pols"].add(pv)
            cell["names"].append(uv)
    return out


def main(argv):
    keys = sorted(os.path.basename(p)[:-5] for p in glob.glob(os.path.join(B, "Extraction", "*.json")))
    rows, n_both, n_div = [], 0, 0
    for key in keys:
        c = coded_rows(key)
        s = sidecar_rows(key)
        if not c or not s:
            continue
        n_both += 1
        for dom in sorted(set(c) & set(s)):
            for ver in sorted(set(c[dom]) | set(s[dom])):
                cc = c.get(dom, {}).get(ver) or {"pols": set(), "names": []}
                ss = s.get(dom, {}).get(ver) or {"pols": set(), "names": []}
                if not cc["pols"] or not ss["pols"]:
                    continue                     # one side silent -> not a divergence
                if cc["pols"] == ss["pols"]:
                    continue
                n_div += 1
                rows.append({
                    "study_key": key,
                    "dv_domain": dom,
                    "veracity": ver or "(blank)",
                    "coding_polarity": "|".join(sorted({str(p) for p in cc["pols"]})),
                    "sidecar_polarity": "|".join(sorted({str(p) for p in ss["pols"]})),
                    "meta_layer_uses": "coding",
                    "sdt_layer_uses": "sidecar",
                    "coded_dv_names": "; ".join(cc["names"][:3]),
                    "harmonized_variables": "; ".join(ss["names"][:4]),
                    "n_harmonized_vars": len(ss["names"]),
                })

    with open(OUT, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else
                           ["study_key", "dv_domain", "veracity", "coding_polarity",
                            "sidecar_polarity", "meta_layer_uses", "sdt_layer_uses",
                            "coded_dv_names", "harmonized_variables", "n_harmonized_vars"])
        w.writeheader()
        w.writerows(rows)

    print(f"studies with both layers : {n_both}")
    print(f"divergent cells          : {n_div}  (REPORT ONLY -- never fails the build)")
    if argv and "--summary" in argv:
        return 0
    print(f"wrote {os.path.relpath(OUT, B)}")
    for r in rows[:12]:
        print(f"   {r['study_key']} | {r['dv_domain']} | ver={r['veracity']} | "
              f"coding={r['coding_polarity']} vs sidecar={r['sidecar_polarity']} "
              f"({r['n_harmonized_vars']} vars)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
