"""Promote validated DS codings into the canonical ``Extraction/`` route.

The DS route writes ``Codings/<Key> - DS.json``.  These papers are new to the
corpus, so the DS coding is also the canonical coding; this script copies it to
``Extraction/<Key>.json`` (and the notes to ``Extraction/<Key>.md``) **only when
the coding validates** and the canonical file does not already exist.

    python Scripts/promote_ds_codings.py            # dry run: report only
    python Scripts/promote_ds_codings.py --apply    # copy
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMP = os.path.join(ROOT, "temp", "ds_route")
CODINGS = os.path.join(ROOT, "Codings")
EXTRACTION = os.path.join(ROOT, "Extraction")


def validate(path):
    r = subprocess.run([sys.executable, os.path.join(ROOT, "Scripts", "B_validate.py"), path],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode == 0, (r.stdout or "") + (r.stderr or "")


def validate_spans(path):
    r = subprocess.run([sys.executable, os.path.join(ROOT, "Scripts", "mark_source.py"),
                        "--verify", path, "--fields"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode == 0, (r.stdout or "") + (r.stderr or "")


def full_text_available(key):
    """A real full text: a PDF, or a text file long enough to be an article."""
    if os.path.exists(os.path.join(ROOT, "Papers", f"{key}.pdf")):
        return True, "pdf"
    tp = os.path.join(ROOT, "Text", f"{key}.txt")
    if os.path.exists(tp) and os.path.getsize(tp) >= 20000:
        return True, "text"
    return False, "abstract only" if os.path.exists(tp) else "no full text"


def exclusion_ok(path):
    try:
        recs = json.load(open(path, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False, "unreadable"
    allowed = {"C1", "C2", "C3", "C4"}
    for rec in (recs if isinstance(recs, list) else [recs]):
        val = rec.get("Fulltext_Exclusion_Criterion")
        if val in (None, "", []):
            continue
        vals = val if isinstance(val, list) else [val]
        bad = [v for v in vals if str(v).strip() not in allowed]
        if bad:
            return False, f"invalid Fulltext_Exclusion_Criterion {bad}"
    return True, ""


def mark_master_included(keys):
    """Set Fulltext_Verdict / Effective_Verdict = Include for the promoted papers."""
    import openpyxl
    by_key = {p["key"]: p["ans"] for p in json.load(
        open(os.path.join(TEMP, "plan.json"), encoding="utf-8"))}
    ans = {str(a) for k in keys for a in by_key.get(k, [])}
    if not ans:
        return 0
    master = os.path.join(ROOT, "master_dataset.xlsx")
    wb = openpyxl.load_workbook(master)
    ws = wb["Sheet1"] if "Sheet1" in wb.sheetnames else wb.worksheets[0]
    head = [str(c.value).strip() if c.value is not None else "" for c in ws[1]]
    H = {n: i for i, n in enumerate(head) if n}
    n = 0
    for r in range(2, ws.max_row + 1):
        an = str(ws.cell(row=r, column=H["an"] + 1).value or "").strip()
        if an in ans:
            ws.cell(row=r, column=H["Fulltext_Verdict"] + 1).value = "Include"
            ws.cell(row=r, column=H["Effective_Verdict"] + 1).value = "Include"
            n += 1
    wb.save(master)
    wb.close()
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    plan = json.load(open(os.path.join(TEMP, "plan.json"), encoding="utf-8"))
    promoted, skipped, failed = [], [], []
    for p in plan:
        key = p["key"]
        src = os.path.join(CODINGS, f"{key} - DS.json")
        dst = os.path.join(EXTRACTION, f"{key}.json")
        if not os.path.exists(src):
            skipped.append((key, "no DS coding"))
            continue
        ok, out = validate(src)
        if not ok:
            failed.append((key, out.strip().splitlines()[:3]))
            continue
        ok2, out2 = validate_spans(src)
        if not ok2:
            failed.append((key, ["source spans: " + out2.strip().splitlines()[-1][:160]]))
            continue
        ok3, why3 = exclusion_ok(src)
        if not ok3:
            failed.append((key, [why3]))
            continue
        ok4, why4 = full_text_available(key)
        if not ok4:
            failed.append((key, [f"not promoted: {why4}"]))
            continue
        if os.path.exists(dst):
            skipped.append((key, "Extraction already exists"))
            continue
        if args.apply:
            shutil.copy2(src, dst)
            md_src = os.path.join(CODINGS, f"{key} - DS.md")
            md_dst = os.path.join(EXTRACTION, f"{key}.md")
            if os.path.exists(md_src) and not os.path.exists(md_dst):
                shutil.copy2(md_src, md_dst)
        promoted.append(key)

    print(f"promoted: {len(promoted)}")
    for k in promoted:
        print("  +", k)
    if skipped:
        print(f"skipped: {len(skipped)}")
        for k, why in skipped:
            print("  -", k, "|", why)
    if failed:
        print(f"FAILED validation: {len(failed)}")
        for k, why in failed:
            print("  !", k, "|", why)
    if not args.apply:
        print("\n(dry run - pass --apply to copy)")
    else:
        n = mark_master_included(promoted)
        print(f"master rows marked Include: {n}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
