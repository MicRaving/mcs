"""Validate `<Key> - exclusions.csv` rules: is each `keep` predicate really a KEEP predicate?

`suite_common._apply_dv_exclusions` drops a cell when its rule's `keep` expression is FALSE, so
the `keep` column must be true for participants the authors RETAINED.  A rule written as
`flag_x == 1` where 1 means "failed the check" therefore keeps exactly the wrong people.

This validator evaluates every rule against the study's wide table and reports:
  * the operator and the retained fraction;
  * INVERSION SUSPICION when the flag name or the reason carries a failure token
    (fail/trap/inattent/drop/exclude/speeder/incomplete) AND the expression keeps the
    flag==1 side;
  * NO-OP when a rule retains 100 % of rows, and EMPTY when it retains none.

Run:  python Scripts/validate_exclusions.py [--json]
Exit code 1 when any INVERSION SUSPICION is found.
"""
from __future__ import annotations
import os, re, csv, glob, json, sys
import pandas as pd

ROOT = os.path.abspath(os.path.dirname(__file__))
B = os.path.dirname(ROOT)
FAIL_TOK = re.compile(r"fail|trap|inattent|speeder|incomplete|drop|exclude|did not|not pass",
                      re.I)


def evaluate(expr: str, flag: pd.Series):
    """Return (retained_mask, error) for a rule expression on one flag column."""
    m = re.match(r"^\s*([\w\.\-]+)\s*(==|!=|>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)\s*$", str(expr or ""))
    if not m:
        return None, f"unparsed expression {expr!r}"
    col, op, val = m.group(1), m.group(2), float(m.group(3))
    s = pd.to_numeric(flag.get(col), errors="coerce")
    if s is None:
        return None, f"flag {col!r} not in the wide table"
    ops = {"==": s.eq(val), "!=": s.ne(val), ">=": s.ge(val), "<=": s.le(val),
           ">": s.gt(val), "<": s.lt(val)}
    return ops[op].fillna(False), None


def main(argv):
    rows = []
    for p in sorted(glob.glob(os.path.join(B, "Datasets", "*", "* - exclusions.csv"))):
        key = os.path.basename(os.path.dirname(p))
        if " - SB" in key:
            continue
        wide_p = os.path.join(os.path.dirname(p), f"{key} - harmonized.csv")
        if not os.path.isfile(wide_p):
            continue
        try:
            ex = pd.read_csv(p, encoding="utf-8-sig")
            wide = pd.read_csv(wide_p, encoding="utf-8-sig")
        except Exception as e:
            rows.append({"key": key, "rule": "", "issue": f"unreadable: {type(e).__name__}"})
            continue
        if not len(ex) or not len(wide):
            continue
        for i, r in ex.iterrows():
            expr = str(r.get("keep") or "")
            reason = str(r.get("reason") or "")
            flag = str(r.get("flag") or "")
            mask, err = evaluate(expr, wide)
            rec = {"key": key, "flag": flag, "keep_expr": expr,
                   "reason": reason[:90], "retained_pct": None, "issue": ""}
            if err:
                rec["issue"] = err
            else:
                share = 100.0 * float(mask.mean())
                rec["retained_pct"] = round(share, 1)
                if share >= 99.9:
                    rec["issue"] = "NO-OP (retains 100%)"
                elif share <= 0.1:
                    rec["issue"] = "EMPTY (retains 0%)"
                elif FAIL_TOK.search(flag + " " + reason) and re.search(r"==\s*1\b", expr):
                    rec["issue"] = "INVERSION SUSPICION (keeps the flag==1 side of a failure check)"
            rows.append(rec)

    out = pd.DataFrame(rows)
    dest = os.path.join(B, "Results", "exclusions_validation.csv")
    out.to_csv(dest, index=False, encoding="utf-8-sig")
    bad = out[out["issue"].str.startswith("INVERSION")]
    print(f"rules checked: {len(out)} | INVERSION SUSPICION: {len(bad)}"
          f" | NO-OP: {int(out['issue'].str.startswith('NO-OP').sum())}"
          f" | EMPTY: {int(out['issue'].str.startswith('EMPTY').sum())}")
    if len(bad):
        print(bad[["key", "flag", "keep_expr", "retained_pct", "reason"]].to_string(index=False))
    print("wrote", os.path.relpath(dest, B))
    if "--json" in argv:
        print(json.dumps(rows[:20], indent=1)[:1200])
    return 1 if len(bad) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
