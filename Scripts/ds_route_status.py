"""Status of the DS coding route: one row per planned paper.

    python Scripts/ds_route_status.py            # table
    python Scripts/ds_route_status.py --json     # machine-readable

Reads ``temp/ds_route/plan.json`` and reports, per paper, whether the paper was
retrieved, what was deposited, whether the coding exists and validates, how many
DVs carry a source span, and whether the reproduction/harmonization ran.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMP = os.path.join(ROOT, "temp", "ds_route")


def _exists(*parts):
    return os.path.exists(os.path.join(ROOT, *parts))


def _repro_verdict(path):
    try:
        head = open(path, encoding="utf-8", errors="replace").read(4000)
    except Exception:  # noqa: BLE001
        return ""
    m = re.search(r"\*\*reproduced:\*\*\s*(\w+)", head, re.I)
    return m.group(1).lower() if m else ""


def _coding_stats(path):
    try:
        recs = json.load(open(path, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(recs, list):
        return None
    dvs = [dv for r in recs for dv in (r.get("DV_Metrics") or [])]
    return {
        "experiments": len(recs),
        "dvs": len(dvs),
        "dvs_with_span": sum(1 for d in dvs if d.get("source_span")
                             or (d.get("source_quote")
                                 and str(d.get("source_location") or "").strip().lower().startswith("text"))),
        "dvs_with_quote": sum(1 for d in dvs if d.get("source_quote")),
        "eligible_primary": sum(1 for d in dvs if d.get("eligible_primary") == 1),
        "dvs_with_effect": sum(
            1 for d in dvs
            if any(d.get(k) is not None for k in
                   ("m1i", "t_val", "F_val", "r_val", "d_val", "g_val", "yi",
                    "beta_val", "odds_ratio"))),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--validate", action="store_true", help="run B_validate on each coding")
    args = ap.parse_args(argv)

    plan = json.load(open(os.path.join(TEMP, "plan.json"), encoding="utf-8"))
    rows = []
    for p in plan:
        key = p["key"]
        pdf = _exists("Papers", f"{key}.pdf")
        txt = _exists("Text", f"{key}.txt")
        sup = bool(glob.glob(os.path.join(ROOT, "Supplements", f"{key} Supplement*")))
        ds_dir = os.path.join(ROOT, "Datasets", key)
        data_files = []
        if os.path.isdir(ds_dir):
            data_files = [f for f in os.listdir(ds_dir)
                          if f.lower().endswith((".csv", ".sav", ".dta", ".xlsx", ".xls",
                                                 ".rds", ".rdata", ".json", ".zip"))]
        coding = os.path.join(ROOT, "Codings", f"{key} - DS.json")
        stats = _coding_stats(coding) if os.path.exists(coding) else None
        valid = None
        if args.validate and stats:
            r = subprocess.run([sys.executable, os.path.join(ROOT, "Scripts", "B_validate.py"),
                                coding], capture_output=True, text=True)
            valid = r.returncode == 0
        repro = os.path.join(ROOT, "Reproduction", f"{key}.md")
        harmonized = _exists("Datasets", key, f"{key} - harmonized.csv")
        row = {
            "key": key,
            "pdf": pdf, "text": txt, "supplement": sup,
            "data_files": len(data_files),
            "coding": bool(stats), "valid": valid,
            "experiments": (stats or {}).get("experiments"),
            "dvs": (stats or {}).get("dvs"),
            "dvs_with_span": (stats or {}).get("dvs_with_span"),
            "eligible_primary": (stats or {}).get("eligible_primary"),
            "dvs_with_effect": (stats or {}).get("dvs_with_effect"),
            "repro": os.path.exists(repro),
            "verdict": _repro_verdict(repro) if os.path.exists(repro) else "",
            "harmonized": harmonized,
        }
        rows.append(row)

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0

    print(f"{'key':<34}{'pdf':>4}{'txt':>4}{'sup':>4}{'data':>5}{'code':>5}"
          f"{'exp':>4}{'dvs':>5}{'span':>5}{'elig':>5}{'repro':>6}{'harm':>5}")
    for r in rows:
        print(f"{r['key']:<34}"
              f"{'Y' if r['pdf'] else '-':>4}"
              f"{'Y' if r['text'] else '-':>4}"
              f"{'Y' if r['supplement'] else '-':>4}"
              f"{r['data_files']:>5}"
              f"{'Y' if r['coding'] else '-':>5}"
              f"{r['experiments'] or 0:>4}"
              f"{r['dvs'] or 0:>5}"
              f"{r['dvs_with_span'] or 0:>5}"
              f"{r['eligible_primary'] or 0:>5}"
              f"{(r['verdict'] or ('Y' if r['repro'] else '-')):>6}"
              f"{'Y' if r['harmonized'] else '-':>5}")

    n = len(rows)
    print(f"\n{n} papers | retrieved {sum(1 for r in rows if r['pdf'] or r['text'])}"
          f" | coded {sum(1 for r in rows if r['coding'])}"
          f" | reproduced {sum(1 for r in rows if r['repro'])}"
          f" | harmonized {sum(1 for r in rows if r['harmonized'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
