"""Resolve Study_ID collisions inside the MM coding route.

Why
---
``Study_ID`` is the join key of the IRR merge and of the cross-coder dedup.  The
canonical extraction disambiguates a study key that shares its first-author
surname and year with another key by suffixing the key letter
(``Bayrak2025a_Exp1``) or by using both authors' surnames
(``RamasubramanianOliver2007_Exp1``).  Coding agents, working from the paper
alone, derive ``<Surname><Year>_ExpN`` and therefore reuse one identifier for
several different study keys — e.g. ``Zhang2026_Exp1`` for both ``Zhang & Kim
(2026)`` and ``Zhang et al. (2026 a)``.  A reused id silently pairs the wrong
records, so it has to be fixed before the merge.

What it does
------------
Only the keys that actually take part in a collision are touched.  For each of
those keys the record ids are mapped onto the canonical extraction's ids for
the *same* study key:

* same number of records  -> positional map (canonical[i]);
* different number        -> keep the MM record's own ``_ExpN`` suffix and swap
  in the canonical prefix, so the file stays internally consistent and the
  extra (or missing) record keeps a unique id.

A mapping is skipped when it would not remove the collision, and the script
refuses to write anything if the rewrite would leave an id shared between two
study keys, or would introduce an id that does not exist in the canonical
extraction.  Nothing but the ``Study_ID`` string is ever changed: no DV value,
no span, no other field.

Usage
-----
    python Scripts/mm_align_study_ids.py            # dry run, prints the plan
    python Scripts/mm_align_study_ids.py --apply    # rewrite the JSON files
    python Scripts/mm_align_study_ids.py --report   # write Results/mm_study_id_alignment.csv

Read-only apart from the ``--apply`` rewrite and the ``--report`` CSV.
"""
import argparse
import csv
import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODINGS = os.path.join(ROOT, "Codings")
EXTRACTION = os.path.join(ROOT, "Extraction")
RES = os.path.join(ROOT, "Results")

SUFFIX_RE = re.compile(r"_(Exp\d+|Pretest|Study\d+)$")


def load(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("experiments", data)
    return data if isinstance(data, list) else []


def prefix_of(study_id):
    """'Zhang2026_Exp1' -> 'Zhang2026' (the part before the record suffix)."""
    return SUFFIX_RE.sub("", study_id or "")


def suffix_of(study_id):
    """'Zhang2026_Exp1' -> '_Exp1' (the record suffix, underscore included)."""
    m = SUFFIX_RE.search(study_id or "")
    return ("_" + m.group(1)) if m else ""


def canonical_ids(key):
    path = os.path.join(EXTRACTION, key + ".json")
    if not os.path.isfile(path):
        return []
    return [r.get("Study_ID") for r in load(path) if isinstance(r, dict)]


def all_ids():
    """{study_id: {keys}} over every canonical extraction."""
    owners = {}
    for path in glob.glob(os.path.join(EXTRACTION, "*.json")):
        key = os.path.basename(path)[:-5]
        for r in load(path):
            sid = r.get("Study_ID") if isinstance(r, dict) else None
            if sid:
                owners.setdefault(sid, set()).add(key)
    return owners


def build_plan():
    files = {}
    for path in sorted(glob.glob(os.path.join(CODINGS, "* - MM.json"))):
        key = os.path.basename(path)[: -len(" - MM.json")]
        files[key] = load(path)

    # ids currently used by the MM route, and by which keys
    mm_owners = {}
    for key, recs in files.items():
        for rec in recs:
            sid = rec.get("Study_ID") if isinstance(rec, dict) else None
            if sid:
                mm_owners.setdefault(sid, set()).add(key)

    colliding = {sid for sid, keys in mm_owners.items() if len(keys) > 1}
    if not colliding:
        return {}, files, mm_owners

    plan = {}
    for key, recs in files.items():
        cur = [r.get("Study_ID") for r in recs if isinstance(r, dict)]
        if not any(sid in colliding for sid in cur):
            continue
        canon = [c for c in canonical_ids(key) if c]
        if not canon:
            continue
        canon_prefix = prefix_of(canon[0])

        pairs = []
        if len(canon) == len(cur):
            pairs = list(zip(cur, canon))
        else:
            for old in cur:
                sfx = suffix_of(old)
                pairs.append((old, canon_prefix + sfx) if sfx else (old, old))

        changed = [(o, n) for o, n in pairs if o != n]
        if changed:
            plan[key] = changed
    return plan, files, mm_owners


def simulate(plan, mm_owners):
    """Owners after the rewrite; also report ids that would be new/unknown."""
    owners = {sid: set(keys) for sid, keys in mm_owners.items()}
    for key, pairs in plan.items():
        mapping = dict(pairs)
        for sid, keys in list(owners.items()):
            if key in keys:
                owners[sid] = (keys - {key}) or {key}
        for old, new in pairs:
            if old != new:
                owners.setdefault(new, set()).add(key)
    return {sid: sorted(k) for sid, k in owners.items() if len(k) > 1}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="rewrite the JSON files")
    ap.add_argument("--report", action="store_true", help="write Results/mm_study_id_alignment.csv")
    args = ap.parse_args()

    plan, files, mm_owners = build_plan()
    before = {sid: sorted(k) for sid, k in mm_owners.items() if len(k) > 1}
    after = simulate(plan, mm_owners)
    canon_all = all_ids()

    rows = []
    for key in sorted(plan):
        for old, new in plan[key]:
            rows.append(dict(study_key=key, old_study_id=old, new_study_id=new))

    print("colliding ids before: %d" % len(before))
    for sid, keys in sorted(before.items()):
        print("   %-30s %s" % (sid, keys))
    print("\n%d file(s), %d id(s) to realign" % (len(plan), len(rows)))
    for key in sorted(plan):
        for old, new in plan[key]:
            print("%-42s %-30s -> %s" % (key, old, new))

    if after:
        print("\nREFUSING: ids still shared after the rewrite")
        for sid, keys in sorted(after.items()):
            print("   %-30s %s" % (sid, keys))
        return 1

    # A new id must stay inside the namespace of its own study key: its prefix
    # has to be a prefix the canonical extraction uses for that key.  (A file may
    # hold one more record than the canonical extraction, e.g. an extra
    # pre-test; that record keeps its own suffix and borrows only the prefix.)
    canon_prefixes = {}
    for key in plan:
        canon_prefixes[key] = {prefix_of(c) for c in canonical_ids(key) if c}
    unknown = sorted({row["new_study_id"] for row in rows
                      if prefix_of(row["new_study_id"]) not in
                      canon_prefixes.get(row["study_key"], set())})
    if unknown:
        print("\nREFUSING: %d target id(s) do not exist in the canonical extraction"
              % len(unknown))
        for sid in unknown:
            print("   ", sid)
        return 1

    print("\nno id remains shared between study keys")

    if args.report and rows:
        os.makedirs(RES, exist_ok=True)
        out = os.path.join(RES, "mm_study_id_alignment.csv")
        with open(out, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["study_key", "old_study_id", "new_study_id"])
            w.writeheader()
            w.writerows(rows)
        print("wrote", out)

    if args.apply:
        for key, pairs in plan.items():
            path = os.path.join(CODINGS, key + " - MM.json")
            recs = files[key]
            mapping = dict(pairs)
            changed = 0
            for rec in recs:
                if not isinstance(rec, dict):
                    continue
                old = rec.get("Study_ID")
                new = mapping.get(old)
                if new and new != old:
                    rec["Study_ID"] = new
                    changed += 1
            if not changed:
                continue
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(recs, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            print("rewrote %s (%d id(s))" % (key, changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
