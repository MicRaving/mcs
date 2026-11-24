"""Cell-level comparison of two master workbooks (``master_dataset.xlsx``).

Phase 5 of the online-project plan accepts the derived master only when it
equals today's workbook cell by cell, so this module is the acceptance tool::

    python Scripts/compare_master.py OLD.xlsx NEW.xlsx [--first 20]

It streams both workbooks (``openpyxl`` read-only, one row at a time, so a
30 000-row workbook costs a few MB), matches the sheets by name and the columns
by **header name**, and reports

* the sheet / row / column shape differences (a sheet or column that only one
  side has, a reordered column),
* the value differences on the shared columns, normalising only the things that
  are not data (an empty cell is ``None`` or ``""``, a number is a number,
  ``NaN`` is empty),
* the filled cells of the columns only the new workbook has (the merge adds
  ``Screened_By`` / ``Screened_At``: provenance, reported separately from value
  differences).

Exit code 1 when there is at least one value difference on a shared column.

``content_signature(path)`` hashes every cell value (not the zip bytes, whose
entry timestamps differ between two runs) - the deterministic-compile test uses
it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from datetime import date, datetime

MAX_DETAILS = 5000


# ---------------------------------------------------------------- values
def normalise(value):
    """A cell value as a comparable, hashable form (empty cells become ``""``)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return ""
        return int(value) if value.is_integer() else value
    if isinstance(value, int):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def same(old, new):
    """Whether two normalised values are the same cell content."""
    if old == new:
        return True
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        return math.isclose(float(old), float(new), rel_tol=1e-9, abs_tol=1e-9)
    if isinstance(old, str) and isinstance(new, str):
        return old == new
    return False


def _open_rows(path):
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=False)
    return wb, list(wb.worksheets)


def _header(ws):
    it = ws.iter_rows(values_only=True)
    head = next(it, None) or ()
    return [str(h).strip() if h is not None else "" for h in head]


def compare_master(old_path, new_path):
    """Compare two workbooks; returns a dict (counts, per-sheet detail, differences)."""
    wb_old, sheets_old = _open_rows(old_path)
    wb_new, sheets_new = _open_rows(new_path)
    names_old = [ws.title for ws in sheets_old]
    names_new = [ws.title for ws in sheets_new]
    counts = {
        "sheets_old": len(names_old),
        "sheets_new": len(names_new),
        "sheets_compared": 0,
        "sheets_only_old": [n for n in names_old if n not in names_new],
        "sheets_only_new": [n for n in names_new if n not in names_old],
        "rows_old": 0,
        "rows_new": 0,
        "columns_old": 0,
        "columns_new": 0,
        "columns_added": [],
        "columns_removed": [],
        "columns_reordered": False,
        "cells_compared": 0,
        "value_differences": 0,
        "cells_in_added_columns": 0,
    }
    differences = []
    per_sheet = {}
    try:
        for name in names_old:
            if name not in names_new:
                continue
            ws_old = wb_old[name]
            ws_new = wb_new[name]
            head_old = _header(ws_old)
            head_new = _header(ws_new)
            counts["sheets_compared"] += 1
            shared = [h for h in head_old if h and h in head_new]
            added = [h for h in head_new if h and h not in head_old]
            removed = [h for h in head_old if h and h not in head_new]
            counts["columns_old"] = max(counts["columns_old"], len(head_old))
            counts["columns_new"] = max(counts["columns_new"], len(head_new))
            counts["columns_added"].extend(f"{name}:{h}" for h in added)
            counts["columns_removed"].extend(f"{name}:{h}" for h in removed)
            if [h for h in head_old if h in head_new] != [h for h in head_new if h in head_old]:
                counts["columns_reordered"] = True
            idx_old = {h: i for i, h in enumerate(head_old)}
            idx_new = {h: i for i, h in enumerate(head_new)}

            def cell(row, i):
                return normalise(row[i]) if i is not None and i < len(row) else ""

            it_old = ws_old.iter_rows(values_only=True)
            it_new = ws_new.iter_rows(values_only=True)
            next(it_old, None)
            next(it_new, None)
            rows_old = rows_new = 0
            sheet_diffs = 0
            while True:
                row_old = next(it_old, _SENTINEL)
                row_new = next(it_new, _SENTINEL)
                if row_old is _SENTINEL and row_new is _SENTINEL:
                    break
                if row_old is not _SENTINEL:
                    rows_old += 1
                if row_new is not _SENTINEL:
                    rows_new += 1
                rownum = 1 + max(rows_old, rows_new)
                if row_old is _SENTINEL or row_new is _SENTINEL:
                    counts["value_differences"] += 1
                    sheet_diffs += 1
                    differences.append({"sheet": name, "row": rownum, "column": "(row)",
                                        "old": "" if row_old is _SENTINEL else "(row present)",
                                        "new": "" if row_new is _SENTINEL else "(row present)"})
                    continue
                for head in shared:
                    old_value = cell(row_old, idx_old[head])
                    new_value = cell(row_new, idx_new[head])
                    counts["cells_compared"] += 1
                    if not same(old_value, new_value):
                        counts["value_differences"] += 1
                        sheet_diffs += 1
                        if len(differences) < MAX_DETAILS:
                            differences.append({
                                "sheet": name, "row": rownum, "column": head,
                                "old": old_value, "new": new_value})
                for head in added:
                    if cell(row_new, idx_new[head]) not in (None, ""):
                        counts["cells_in_added_columns"] += 1
            counts["rows_old"] = max(counts["rows_old"], rows_old)
            counts["rows_new"] = max(counts["rows_new"], rows_new)
            per_sheet[name] = {"rows": max(rows_old, rows_new), "differences": sheet_diffs,
                               "columns_added": added, "columns_removed": removed}
    finally:
        wb_old.close()
        wb_new.close()
    return {"old": os.path.abspath(old_path), "new": os.path.abspath(new_path),
            "counts": counts, "sheets": per_sheet, "differences": differences}


class _Sentinel:
    def __repr__(self):  # pragma: no cover - debugging aid
        return "<end of rows>"


_SENTINEL = _Sentinel()


def print_differences(diff, limit=20):
    """Print the first *limit* differences, then a one-line summary."""
    print(f"compared {diff['counts']['cells_compared']} cell(s) on "
          f"{diff['counts']['sheets_compared']} sheet(s): "
          f"{diff['counts']['value_differences']} value difference(s)")
    if diff["counts"]["columns_added"]:
        print(f"columns only in the new workbook: {diff['counts']['columns_added']} "
              f"({diff['counts']['cells_in_added_columns']} filled cell(s))")
    if diff["counts"]["columns_removed"]:
        print(f"columns only in the old workbook: {diff['counts']['columns_removed']}")
    if diff["counts"]["sheets_only_old"] or diff["counts"]["sheets_only_new"]:
        print(f"sheets only in the old: {diff['counts']['sheets_only_old']}; "
              f"only in the new: {diff['counts']['sheets_only_new']}")
    for n, item in enumerate(diff["differences"][:limit], start=1):
        print(f"  {n:2d}. {item['sheet']} row {item['row']} [{item['column']}]: "
              f"{_short(item['old'])} -> {_short(item['new'])}")
    if diff["counts"]["value_differences"] > limit:
        print(f"  ... {diff['counts']['value_differences'] - limit} more")


def _short(value, width=90):
    text = str(value)
    text = text.replace("\n", "\\n")
    return text if len(text) <= width else text[:width - 1] + "…"


def content_signature(path):
    """SHA-256 over every sheet name and cell value (not the zip bytes)."""
    wb, sheets = _open_rows(path)
    digest = hashlib.sha256()
    try:
        for ws in sheets:
            digest.update(ws.title.encode("utf-8"))
            digest.update(b"\x00")
            for row in ws.iter_rows(values_only=True):
                digest.update(repr(tuple(normalise(v) for v in row)).encode("utf-8"))
                digest.update(b"\n")
    finally:
        wb.close()
    return digest.hexdigest()


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("old", help="the reference workbook (today's master_dataset.xlsx)")
    parser.add_argument("new", help="the compiled workbook")
    parser.add_argument("--first", type=int, default=20, help="differences to print (default 20)")
    parser.add_argument("--json", default=None, help="write the full diff as JSON")
    args = parser.parse_args(argv)
    diff = compare_master(args.old, args.new)
    print_differences(diff, limit=args.first)
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(diff, fh, ensure_ascii=False, indent=1, default=str)
        print(f"diff written to {args.json}")
    return 0 if diff["counts"]["value_differences"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
