"""Wave-wide verification of the MM coding route.

Runs the two gates over every ``Codings/<Key> - MM.json`` and writes
``Results/mm_coding_audit.csv`` (one row per study key) plus a console summary:

  * ``Scripts/B_validate.py``      -> schema completeness
  * ``Scripts/mark_source.py --verify --fields`` -> every DV has a marked span and
    every non-empty record field has a ``source_spans`` entry

Also re-checks the two invariants the analysis layer depends on:

  * no ``Study_ID`` is shared by two different study keys (the IRR join key);
  * no ``Fulltext_Exclusion_Criterion`` carries a value outside C1-C4.

Read-only: the script only writes its own audit CSV.
"""
import csv
import glob
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODINGS = os.path.join(ROOT, "Codings")
RES = os.path.join(ROOT, "Results")
PY = sys.executable


def run(cmd):
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           timeout=900, encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return 1, repr(exc)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def mark_counts(out):
    """Pull the counters out of mark_source's JSON report."""
    try:
        start = out.index("{")
        data = json.loads(out[start:])
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def main():
    paths = sorted(glob.glob(os.path.join(CODINGS, "* - MM.json")))
    rows = []
    tot_dv = tot_fields = tot_problems = 0
    failed = []
    sid_owners = {}

    for path in paths:
        key = os.path.basename(path)[: -len(" - MM.json")]
        rc_b, out_b = run([PY, os.path.join(ROOT, "Scripts", "B_validate.py"), path])
        rc_m, out_m = run([PY, os.path.join(ROOT, "Scripts", "mark_source.py"),
                           "--verify", path, "--fields"])
        mc = mark_counts(out_m)
        dv = int(mc.get("dv") or 0)
        fields = int(mc.get("fields") or 0)
        problems = int(mc.get("problems") or 0)
        tot_dv += dv
        tot_fields += fields
        tot_problems += problems
        ok = (rc_b == 0) and (rc_m == 0) and problems == 0
        if not ok:
            failed.append((key, rc_b, rc_m, problems))
        rows.append(dict(
            study_key=key, ok=int(ok), b_validate_rc=rc_b, mark_rc=rc_m,
            dv_rows=dv, dv_with_quote=int(mc.get("with_quote") or 0),
            dv_with_span=int(mc.get("with_span") or 0),
            fields=fields, fields_with_span=int(mc.get("fields_with_span") or 0),
            problems=problems,
        ))
        # Study_ID uniqueness (the IRR join key) and exclusion-value validity
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            data = []
        recs = data if isinstance(data, list) else data.get("experiments", [])
        for rec in recs:
            if not isinstance(rec, dict):
                continue
            sid = rec.get("Study_ID")
            if sid:
                sid_owners.setdefault(sid, set()).add(key)
            crit = rec.get("Fulltext_Exclusion_Criterion")
            if crit in (None, "", [], ()):
                continue
            for c in (crit if isinstance(crit, list) else [crit]):
                if str(c) not in ("C1", "C2", "C3", "C4"):
                    rows[-1]["bad_exclusion"] = str(c)

    collisions = {sid: sorted(ks) for sid, ks in sid_owners.items() if len(ks) > 1}

    os.makedirs(RES, exist_ok=True)
    out_path = os.path.join(RES, "mm_coding_audit.csv")
    fields_out = ["study_key", "ok", "b_validate_rc", "mark_rc", "dv_rows",
                   "dv_with_quote", "dv_with_span", "fields",
                   "fields_with_span", "problems", "bad_exclusion"]
    with open(out_path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields_out, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    print("MM wave verification")
    print("  files            : %d" % len(rows))
    print("  DV rows          : %d" % tot_dv)
    print("  record fields    : %d" % tot_fields)
    print("  mark problems    : %d" % tot_problems)
    print("  files failing    : %d" % len(failed))
    for key, rb, rm, pr in failed[:25]:
        print("     %-40s B_validate=%s mark=%s problems=%s" % (key, rb, rm, pr))
    print("  Study_ID collisions: %d" % len(collisions))
    for sid, ks in sorted(collisions.items()):
        print("     %-28s %s" % (sid, ks))
    bad = [r for r in rows if r.get("bad_exclusion")]
    print("  invalid exclusion values: %d" % len(bad))
    print("wrote", out_path)
    return 1 if (failed or collisions or bad) else 0


if __name__ == "__main__":
    sys.exit(main())
