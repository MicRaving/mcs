"""Apply ONLY fully-sourced arithmetic fixes to the campaign coding JSONs.

Companion to ``Scripts/validate_extraction.py``.  The only fix applied is the
sampling variance ``vi`` when it is blank but a reported standard error is
already coded (``sei``, or ``d_se``/``g_se``)::

    vi = se**2

Nothing is guessed: ``yi`` is never derived (its sign is not recoverable), and a
row with a coded ``missing_metrics_reason`` is left untouched.  Rows that need a
paper-level value are left for the human queue produced by the validator.

Safety
------
* ``--dry`` is the default; pass ``--apply`` to write.
* Before a file is modified it is copied once to
  ``archive/extractions_2026-09-22/<Key>.json``.
* Every change is appended to ``Results/extraction_autofix_log.csv``.
* Re-running ``--apply`` is idempotent (no remaining blank ``vi`` with a SE).

Usage::

    python Scripts/apply_extraction_fixes.py            # dry run, prints plan
    python Scripts/apply_extraction_fixes.py --apply
"""
import argparse
import csv
import glob
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTRACT = os.path.join(ROOT, "Extraction")
RESULTS = os.path.join(ROOT, "Results")
ARCHIVE = os.path.join(ROOT, "archive", "extractions_2026-09-22")
LOG = os.path.join(RESULTS, "extraction_autofix_log.csv")
LOG_COLS = ["key", "experiment_id", "dv_name", "field", "old", "new", "rule", "timestamp"]

_RUN_TS = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def blank(v):
    return v is None or (isinstance(v, str) and v.strip() == "")


def as_num(v):
    try:
        if blank(v):
            return None
        f = float(v)
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def detect_indent(text):
    m = re.match(r"\[\r?\n(\s+)\{", text)
    return len(m.group(1)) if m else 2


def se_of(dv):
    """Return (value, field_name) for the first usable reported SE, else (None, '')."""
    for field in ("sei", "d_se", "g_se"):
        v = as_num(dv.get(field))
        if v is not None and v > 0:
            return v, field
    return None, ""


def plan_file(path):
    """Return (data, changes) with arithmetic ``vi`` fixes applied in memory."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    data = json.loads(text)
    if not isinstance(data, list):
        data = [data]
    changes = []
    for o in data:
        if not isinstance(o, dict):
            continue
        exp_id = o.get("Study_ID") or ""
        for dv in (o.get("DV_Metrics") or []):
            if not isinstance(dv, dict):
                continue
            if not blank(dv.get("vi")) or not blank(dv.get("missing_metrics_reason")):
                continue
            se, field = se_of(dv)
            if se is None:
                continue
            new = se * se
            changes.append({
                "key": "", "experiment_id": exp_id,
                "dv_name": dv.get("dv_name") or "",
                "field": "vi", "old": dv.get("vi"),
                "new": new, "rule": "vi=%s**2 (%s=%s)" % (field, field, se),
                "timestamp": _RUN_TS})
            dv["vi"] = new
    return data, changes, detect_indent(text)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (default: dry run)")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(EXTRACT, "*.json")))
    n_files = n_changed_files = n_changes = 0
    pending = []  # (path, data, changes, indent)
    for path in files:
        n_files += 1
        try:
            data, changes, indent = plan_file(path)
        except Exception as e:  # noqa: BLE001
            print("SKIP (parse error) %s: %s" % (os.path.basename(path), e))
            continue
        if not changes:
            continue
        key = os.path.splitext(os.path.basename(path))[0]
        for ch in changes:
            ch["key"] = key
        pending.append((path, data, changes, indent))
        n_changed_files += 1
        n_changes += len(changes)

    print("== extraction arithmetic autofix ==")
    print("files scanned: %d | files needing a fix: %d | vi fixes: %d"
          % (n_files, n_changed_files, n_changes))
    by_field = {}
    for _, _, changes, _ in pending:
        for ch in changes:
            by_field[ch["rule"].split("=")[1].split("**")[0]] = \
                by_field.get(ch["rule"].split("=")[1].split("**")[0], 0) + 1
    for k, v in sorted(by_field.items()):
        print("  from %-6s %d" % (k, v))

    if not args.apply:
        print("\nDRY RUN — no files written. Re-run with --apply to write.")
        for path, _, changes, _ in pending[:10]:
            print("  would fix %s (%d rows)" % (os.path.basename(path), len(changes)))
        if len(pending) > 10:
            print("  ... and %d more files" % (len(pending) - 10))
        return 0

    os.makedirs(RESULTS, exist_ok=True)
    os.makedirs(ARCHIVE, exist_ok=True)
    write_header = not os.path.exists(LOG)
    n_written = 0
    with open(LOG, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LOG_COLS)
        if write_header:
            w.writeheader()
        for path, data, changes, indent in pending:
            key = os.path.splitext(os.path.basename(path))[0]
            backup = os.path.join(ARCHIVE, key + ".json")
            if not os.path.exists(backup):
                shutil.copy2(path, backup)
            with open(path, "w", encoding="utf-8") as out:
                json.dump(data, out, ensure_ascii=False, indent=indent)
                out.write("\n")
            for ch in changes:
                w.writerow(ch)
            n_written += 1
    print("\nAPPLIED: %d files rewritten, %d vi fixes logged to %s"
          % (n_written, n_changes, os.path.relpath(LOG, ROOT)))
    print("originals archived under %s" % os.path.relpath(ARCHIVE, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
