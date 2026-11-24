"""Record DS-route retrieval outcomes in ``master_dataset.xlsx``.

After the DS agents have run, each planned paper has either
``Papers/<Key>.pdf`` (retrieved), ``Text/<Key>.txt`` only (full text without a
PDF), or neither (not retrievable).  This script writes that fact back to every
master row of the batch so the PRISMA flow and the retrieval ledger agree with
what is actually on disk.

    python Scripts/apply_ds_retrieval_status.py            # report only
    python Scripts/apply_ds_retrieval_status.py --apply

Columns touched: ``Download_Status``, ``PDF_Filename``.  ``Effective_Verdict`` is
left alone - a record that passes the abstract screen stays "sought for
retrieval" until its full text is actually assessed.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMP = os.path.join(ROOT, "temp", "ds_route")
MASTER = os.path.join(ROOT, "master_dataset.xlsx")
ARCHIVE = os.path.join(ROOT, "archive")
SOURCE = "new_hit_2026-11-04"


def _sheet(wb):
    for name in ("Search", "Sheet1"):
        if name in wb.sheetnames:
            return wb[name]
    return wb.worksheets[0]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    plan = json.load(open(os.path.join(TEMP, "plan.json"), encoding="utf-8"))
    status = {}
    for p in plan:
        key = p["key"]
        pdf = os.path.exists(os.path.join(ROOT, "Papers", f"{key}.pdf"))
        txt = os.path.exists(os.path.join(ROOT, "Text", f"{key}.txt"))
        if pdf:
            status[key] = ("downloaded (DS route)", f"{key}.pdf")
        elif txt:
            status[key] = ("full text only, no PDF (DS route)", None)
        else:
            status[key] = ("not retrievable (DS route)", None)

    counts = {}
    for key, (label, _) in status.items():
        counts[label] = counts.get(label, 0) + 1
    print(json.dumps(counts, indent=1, ensure_ascii=False))

    if not args.apply:
        print("(report only - pass --apply to write the master)")
        return 0

    import openpyxl
    stamp = datetime.datetime.now().strftime("%Y-%m-%d")
    os.makedirs(ARCHIVE, exist_ok=True)
    backup = os.path.join(ARCHIVE, f"master_dataset_pre_ds_status_{stamp}.xlsx")
    if not os.path.exists(backup):
        shutil.copy2(MASTER, backup)

    wb = openpyxl.load_workbook(MASTER)
    ws = _sheet(wb)
    head = [str(c.value).strip() if c.value is not None else "" for c in ws[1]]
    H = {n: i for i, n in enumerate(head) if n}
    an_to_key = {}
    for p in plan:
        for an in p["ans"]:
            an_to_key[str(an)] = p["key"]

    updated = 0
    for r in range(2, ws.max_row + 1):
        src = ws.cell(row=r, column=H["Record_Source"] + 1).value
        if str(src or "") != SOURCE:
            continue
        an = str(ws.cell(row=r, column=H["an"] + 1).value or "").strip()
        key = an_to_key.get(an)
        if not key:
            continue
        label, pdf_name = status[key]
        ws.cell(row=r, column=H["Download_Status"] + 1).value = label
        if pdf_name and not ws.cell(row=r, column=H["PDF_Filename"] + 1).value:
            ws.cell(row=r, column=H["PDF_Filename"] + 1).value = pdf_name
        updated += 1
    wb.save(MASTER)
    wb.close()
    print(f"updated {updated} master rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
