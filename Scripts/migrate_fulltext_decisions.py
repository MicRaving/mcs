"""Migrate the full-text decisions out of ``master_dataset.xlsx`` into ``Fulltext/<Tag>.json``.

The plan makes the master workbook a **derived** product: every decision a coder
makes lives in an owned per-tag file (``Screening/<Tag>.json`` for abstracts,
``Fulltext/<Tag>.json`` for full texts).  The full-text decisions of the current
project still live in four workbook columns
(``Fulltext_Verdict``, ``Fulltext_Exclusion_Criterion``, ``Fulltext_Reason``,
``Effective_Verdict``); this one-off script moves them into one decision file and
they never touch the workbook again.

**Tag.**  The workbook has no coder column for the full-text stage, so the
decisions carry no tag.  They are attributed to the owner's canonical tag
(``MF`` - the human tag that also holds the canonical ``Codings/<Key> - MF.json``
codings and ``Screening/MF.json``); pass ``--tag`` to use another one.  This is
recorded per decision as ``"source": "master_dataset.xlsx row <n>"`` and the
documented tag is printed by every run.

**Decision shape** (the app's own full-text decision record, plus what the
workbook knew about the row)::

    {"<uid or study key>": {
        "exclude": true,                     # Fulltext_Verdict == "Exclude"
        "criterion": "C1",                   # first of the eight criteria, else "other"
        "criterion_label": "No misinformation intervention (C1)",   # verbatim
        "note": "<Fulltext_Reason verbatim>",
        "saved_at": "<the master file's mtime>",
        "effective": "Exclude",              # the row's Effective_Verdict (curated stage)
        "source": "master_dataset.xlsx row 123"}}

A record that reaches the full-text stage with ``Include`` is migrated too
(``exclude: false``): the compiler needs it to keep the record at the full-text
stage (``Effective_Verdict = Include``) instead of the abstract stage.

Dry run by default; ``--apply`` writes atomically and **refuses to overwrite an
existing non-empty decision file** (neither ``Fulltext/<Tag>.json`` nor a legacy
``Screening/fulltext - <Tag>.json``), so a second run can never destroy a
coder's decisions.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.join(_HERE, "coding_web")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import fulltext_lib as FL              # noqa: E402  (the eight criteria + normaliser)
import screening_lib as SL             # noqa: E402  (the row identity of the merge)
from studio_lib import atomic_replace  # noqa: E402

#: The owner's canonical tag: the workbook names no full-text coder, and this is
#: the human tag that carries the canonical codings and abstract screening.
OWNER_TAG = "MF"

VERDICT_COLUMN = "Fulltext_Verdict"
CRITERION_COLUMN = "Fulltext_Exclusion_Criterion"
NOTE_COLUMN = "Fulltext_Reason"
EFFECTIVE_COLUMN = "Effective_Verdict"


def _saved_at(master):
    """A deterministic stamp for the migrated decisions: the workbook's mtime."""
    return datetime.fromtimestamp(os.path.getmtime(master)).isoformat(timespec="seconds")


def _criterion_code(label):
    """The first of the eight criteria a label names (``"other"`` when none does)."""
    for code in FL.normalize_criterion(label):
        if code in FL.FIELD_CRITERIA_VALUES:
            return code
    text = str(label or "").strip().lower()
    for crit in FL.FULLTEXT_CRITERIA:
        if text and crit["label"].lower() == text and crit["value"] in FL.FIELD_CRITERIA_VALUES:
            return crit["value"]
    return "other"


def extract(master_path):
    """Read a workbook and return ``(decisions, summary)`` - no writes."""
    import openpyxl

    wb = openpyxl.load_workbook(master_path, read_only=True, data_only=True)
    try:
        ws, header_row = SL._pick_sheet(wb)
        it = ws.iter_rows(values_only=True)
        for _ in range(header_row + 1):
            head = next(it, None) or ()
        header = [str(h).strip() if h is not None else "" for h in head]
        cols, _crit = SL._locate_columns(header)
        by_name = {SL._norm(h): i for i, h in enumerate(header)}

        def cell(row, name):
            i = by_name.get(SL._norm(name))
            return SL._cell(row, i) if i is not None else ""

        stamp = _saved_at(master_path)
        seen = {}
        decisions = {}
        verdicts = Counter()
        effective = Counter()
        for rownum, row in enumerate(it, start=header_row + 2):
            if not any(v not in (None, "") for v in row):
                continue
            verdict = str(cell(row, VERDICT_COLUMN) or "").strip()
            if not verdict:
                continue
            uid = SL.screening_row_uid(row, rownum, seen, cols)
            label = str(cell(row, CRITERION_COLUMN) or "").strip()
            note = str(cell(row, NOTE_COLUMN) or "")
            stage = str(cell(row, EFFECTIVE_COLUMN) or "").strip()
            decisions[uid] = {
                "exclude": verdict.lower() == "exclude",
                "criterion": _criterion_code(label) if verdict.lower() == "exclude" else "",
                "criterion_label": label,
                "note": note,
                "saved_at": stamp,
                "effective": stage,
                "source": f"master_dataset.xlsx row {rownum}",
            }
            verdicts[verdict] += 1
            effective[stage] += 1
    finally:
        wb.close()
    return decisions, {"sheet": ws.title, "verdicts": dict(verdicts),
                       "effective": dict(effective), "saved_at": stamp}


def plan(root, master=None, out_dir=None, tag=OWNER_TAG):
    """Everything the run would do, computed without writing anything."""
    root = os.path.abspath(root)
    master = os.path.abspath(master or os.path.join(root, "master_dataset.xlsx"))
    out_dir = os.path.abspath(out_dir or os.path.join(root, "Fulltext"))
    tag = str(tag or "").strip() or OWNER_TAG
    target = os.path.join(out_dir, f"{FL.safe_token(tag)}.json")
    legacy = os.path.join(root, "Screening", f"fulltext - {FL.safe_token(tag)}.json")
    blockers = []
    for path in (target, legacy):
        if os.path.exists(path) and os.path.getsize(path) > 0:
            blockers.append(path)
    decisions, summary = extract(master)
    summary.update({"root": root, "master": master, "tag": tag, "target": target,
                    "legacy": legacy, "records": len(decisions),
                    "blockers": blockers})
    return decisions, summary


def write(decisions, target):
    os.makedirs(os.path.dirname(target), exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=os.path.dirname(target))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(decisions, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.write("\n")
        atomic_replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return target


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=os.path.dirname(_HERE), help="project folder")
    parser.add_argument("--master", default=None,
                        help="workbook to read (default: <root>/master_dataset.xlsx)")
    parser.add_argument("--out", default=None,
                        help="decision folder (default: <root>/Fulltext)")
    parser.add_argument("--tag", default=OWNER_TAG,
                        help=f"tag the untagged decisions are attributed to (default: {OWNER_TAG})")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.master or os.path.join(args.root, "master_dataset.xlsx")):
        print("master_dataset.xlsx not found; nothing to migrate")
        return 2
    decisions, summary = plan(args.root, args.master, args.out, args.tag)
    print(f"master      : {summary['master']} (sheet '{summary['sheet']}')")
    print(f"tag         : {summary['tag']}   "
          f"(the workbook names no full-text coder; documented owner tag)")
    print(f"target      : {summary['target']}")
    print(f"records     : {summary['records']}  {summary['verdicts']}  "
          f"effective={summary['effective']}")
    print(f"saved_at    : {summary['saved_at']}  (the workbook's mtime; older than any new save)")
    sample = list(decisions.items())[:2]
    for key, value in sample:
        print(f"  e.g. {key}: {json.dumps(value, ensure_ascii=False)[:160]}")
    if summary["blockers"]:
        print("REFUSING: a non-empty decision file already exists:")
        for path in summary["blockers"]:
            print(f"  - {path}")
        print("Move it away first if it is obsolete; an existing decision file is never overwritten.")
        return 1
    if not args.apply:
        print("\nDRY RUN - nothing written. Re-run with --apply to write the decision file.")
        return 0
    write(decisions, summary["target"])
    print(f"\nwrote {summary['records']} decision(s) -> {summary['target']}")
    print("Next: create the base and check the compiled workbook equals the current one:")
    print("  python Scripts/compile_master.py --init-base")
    print("  python Scripts/compile_master.py --check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
