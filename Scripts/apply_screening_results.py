"""Apply abstract-screening verdicts to ``master_dataset.xlsx`` and ``Screening/OG.json``.

The screening agents write one JSON file per batch under
``temp/screening/results/``:

    {"batch": "batch_001.json",
     "results": [
       {"an": "197590479",
        "C1": {"verdict": "Yes", "certainty": "High", "rationale": "..."},
        "C2": {...}, "C3": {...}, "C4": {...},
        "Intervention": {"verdict": "debunking_and_rebuttals",
                         "certainty": "High", "rationale": "..."}}
     ]}

This script validates every record, writes the verdicts into the master's
``C*_verdict``/``C*_certainty``/``C*_rationale`` and ``Intervention_*`` columns,
derives ``Effective_Verdict``, propagates a duplicate's verdict from its
primary, and mirrors everything into ``Screening/OG.json`` (one entry per
master row, keyed by ``an`` or ``row-<excel row>``).

Usage
-----
    python Scripts/apply_screening_results.py --check       # validate only
    python Scripts/apply_screening_results.py --apply       # write both stores
    python Scripts/apply_screening_results.py --apply --source new_hit_2026-11-04
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import shutil
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER = os.path.join(ROOT, "master_dataset.xlsx")
TEMP = os.path.join(ROOT, "temp", "screening")
RESULTS = os.path.join(TEMP, "results")
OG = os.path.join(ROOT, "Screening", "OG.json")
ARCHIVE = os.path.join(ROOT, "archive")

VERDICTS = {"Yes", "No", "Unsure"}
CERTAINTIES = {"High", "Medium", "Low"}
CRITERIA = ["C1", "C2", "C3", "C4"]

#: intervention families the campaign codes (the master's screening vocabulary)
INTERVENTIONS = {
    "none", "generic", "inoculation", "sourcing", "lateral_reading",
    "media_literacy_tips", "fact_checking_training", "debunking_and_rebuttals",
    "community_correction", "credibility_labels", "accuracy_prompts",
    "friction", "social_norms", "platform_exposure_reduction",
    "openmindedness_intervention", "other",
}

RATIONALE_MIN = 3


def norm_verdict(value):
    s = str(value or "").strip().lower()
    return {"yes": "Yes", "no": "No", "unsure": "Unsure"}.get(s, "")


def norm_certainty(value):
    s = str(value or "").strip().lower()
    return {"high": "High", "medium": "Medium", "low": "Low"}.get(s, "")


def norm_intervention(value):
    s = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    return s if s in INTERVENTIONS else ""


def screen_outcome(verdicts):
    """Include / Exclude / Unsure / "" - the same rule the screening tab uses."""
    vs = [norm_verdict(v) for v in verdicts]
    if any(v == "No" for v in vs):
        return "Exclude"
    if vs and all(v == "Yes" for v in vs):
        return "Include"
    if vs and all(vs):
        return "Unsure"
    return ""


def effective_verdict(outcome):
    """The master's Effective_Verdict for a freshly screened row."""
    if outcome == "Exclude":
        return "Abstract excluded"
    if outcome in ("Include", "Unsure"):
        # advances to full text; not yet retrieved or assessed
        return "Not retrieved / not assessed"
    return "Unscreened"


# ---------------------------------------------------------------- results
def load_results(directory=RESULTS):
    """{an: decision} plus a list of problems."""
    decisions, problems, seen = {}, [], {}
    files = sorted(glob.glob(os.path.join(directory, "batch_[0-9][0-9][0-9].json")))
    for path in files:
        name = os.path.basename(path)
        if name.startswith("_"):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                blob = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{name}: unreadable ({exc})")
            continue
        rows = blob.get("results") if isinstance(blob, dict) else blob
        if not isinstance(rows, list):
            problems.append(f"{name}: no 'results' list")
            continue
        for i, rec in enumerate(rows):
            where = f"{name}[{i}]"
            if not isinstance(rec, dict):
                problems.append(f"{where}: not an object")
                continue
            an = str(rec.get("an") or "").strip()
            if not an:
                problems.append(f"{where}: missing 'an'")
                continue
            clean = {"an": an}
            ok = True
            for crit in CRITERIA:
                entry = rec.get(crit)
                if not isinstance(entry, dict):
                    problems.append(f"{where} {an}: missing {crit}")
                    ok = False
                    continue
                verdict = norm_verdict(entry.get("verdict"))
                if not verdict:
                    problems.append(f"{where} {an}: {crit} verdict "
                                    f"{entry.get('verdict')!r} not Yes/No/Unsure")
                    ok = False
                certainty = norm_certainty(entry.get("certainty")) or "Medium"
                rationale = str(entry.get("rationale") or "").strip()
                if len(rationale) < RATIONALE_MIN:
                    problems.append(f"{where} {an}: {crit} rationale too short")
                    ok = False
                clean[crit] = {"verdict": verdict, "certainty": certainty,
                               "rationale": rationale}
            entry = rec.get("Intervention") or {}
            if not isinstance(entry, dict):
                entry = {}
            iv = norm_intervention(entry.get("verdict")) or "none"
            clean["Intervention"] = {
                "verdict": iv,
                "certainty": norm_certainty(entry.get("certainty")) or "Medium",
                "rationale": str(entry.get("rationale") or "").strip(),
            }
            if not ok:
                continue
            if an in seen:
                if seen[an] != clean:
                    problems.append(f"{where} {an}: conflicting duplicate verdict")
                continue
            seen[an] = clean
            decisions[an] = clean
    return decisions, problems, files


# ---------------------------------------------------------------- workbook
def _sheet(wb):
    for name in ("Search", "Sheet1"):
        if name in wb.sheetnames:
            return wb[name]
    return wb.worksheets[0]


def _match(src, source, prefix):
    return src.startswith(source) if prefix else src == source


def apply_to_master(decisions, source, dry_run=False, prefix=False):
    """Write the verdicts into the workbook; return a small report."""
    import openpyxl

    if not dry_run:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d")
        os.makedirs(ARCHIVE, exist_ok=True)
        backup = os.path.join(ARCHIVE, f"master_dataset_pre_apply_{stamp}.xlsx")
        if not os.path.exists(backup):
            shutil.copy2(MASTER, backup)

    wb = openpyxl.load_workbook(MASTER)
    ws = _sheet(wb)
    head = [str(c.value).strip() if c.value is not None else "" for c in ws[1]]
    H = {name: i for i, name in enumerate(head) if name}
    col = {name: H[name] + 1 for name in H}

    def val(row, name):
        i = H.get(name)
        if i is None:
            return None
        return ws.cell(row=row, column=i + 1).value

    # rows of interest: the newly merged batch (and anything still unscreened)
    targets = []
    for r in range(2, ws.max_row + 1):
        src = str(val(r, "Record_Source") or "")
        if source and not _match(src, source, prefix):
            continue
        targets.append(r)

    applied = inherited = still = 0
    # pass 1: direct decisions
    for r in targets:
        an = str(val(r, "an") or "").strip()
        dec = decisions.get(an)
        if not dec:
            continue
        for crit in CRITERIA:
            ws.cell(row=r, column=col[f"{crit}_verdict"]).value = dec[crit]["verdict"]
            ws.cell(row=r, column=col[f"{crit}_certainty"]).value = dec[crit]["certainty"]
            ws.cell(row=r, column=col[f"{crit}_rationale"]).value = dec[crit]["rationale"]
        iv = dec["Intervention"]
        ws.cell(row=r, column=col["Intervention_verdict"]).value = iv["verdict"]
        ws.cell(row=r, column=col["Intervention_certainty"]).value = iv["certainty"]
        ws.cell(row=r, column=col["Intervention_rationale"]).value = iv["rationale"]
        outcome = screen_outcome([dec[c]["verdict"] for c in CRITERIA])
        ws.cell(row=r, column=col["Effective_Verdict"]).value = effective_verdict(outcome)
        applied += 1

    # pass 2: duplicates inherit their primary's verdict
    an_row = {}
    for r in range(2, ws.max_row + 1):
        an = str(val(r, "an") or "").strip()
        if an and an not in an_row:
            an_row[an] = r
    for r in targets:
        if str(val(r, "an") or "").strip() in decisions:
            continue
        if str(val(r, "Duplicate") or "").strip().lower() != "duplicate":
            continue
        primary_an = str(val(r, "Duplicate_Of") or "").strip()
        pr = an_row.get(primary_an)
        if not pr:
            continue
        for crit in CRITERIA:
            for suffix in ("verdict", "certainty", "rationale"):
                name = f"{crit}_{suffix}"
                ws.cell(row=r, column=col[name]).value = val(pr, name)
        for suffix in ("verdict", "certainty", "rationale"):
            name = f"Intervention_{suffix}"
            ws.cell(row=r, column=col[name]).value = val(pr, name)
        ws.cell(row=r, column=col["Effective_Verdict"]).value = val(pr, "Effective_Verdict")
        inherited += 1

    for r in targets:
        if str(val(r, "Effective_Verdict") or "").strip() in ("", "Unscreened"):
            still += 1

    if not dry_run:
        wb.save(MASTER)
    wb.close()
    return {"targets": len(targets), "applied": applied,
            "inherited": inherited, "still_unscreened": still}


# ---------------------------------------------------------------- OG.json
def apply_to_og(source, dry_run=False, prefix=False):
    """Mirror the master's screening columns into Screening/OG.json.

    Only the rows of the merged batch (``Record_Source`` prefix) are written;
    every pre-existing entry is left exactly as it was.
    """
    import openpyxl

    wb = openpyxl.load_workbook(MASTER, read_only=True, data_only=True)
    try:
        ws = _sheet(wb)
        it = ws.iter_rows(values_only=True)
        head = [str(h).strip() if h is not None else "" for h in (next(it, None) or ())]
        H = {name: i for i, name in enumerate(head) if name}
        rows = [list(r) for r in it]
    finally:
        wb.close()

    def cell(row, name):
        i = H.get(name)
        if i is None or i >= len(row):
            return ""
        v = row[i]
        return "" if v is None else (v if isinstance(v, (int, float)) else str(v).strip())

    existing = {}
    if os.path.exists(OG):
        try:
            with open(OG, encoding="utf-8") as fh:
                existing = json.load(fh)
        except Exception:  # noqa: BLE001
            existing = {}
    if not isinstance(existing, dict):
        existing = {}

    now = datetime.datetime.now().isoformat(timespec="seconds")
    written = 0

    # Assign every row its key first, with the same rule the screening app uses
    # (the accession, else row-<excel row>; a repeated accession gets row-<n>).
    # Building this over ALL rows keeps a new duplicate from stealing the key of
    # the existing record it duplicates.
    keys, seen = [], set()
    for n, row in enumerate(rows, start=2):
        an = str(cell(row, "an") or "").strip()
        key = an if an else f"row-{n}"
        if key in seen:
            key = f"row-{n}"
        seen.add(key)
        keys.append(key)

    for n, (row, key) in enumerate(zip(rows, keys), start=2):
        src = str(cell(row, "Record_Source") or "")
        if source and not _match(src, source, prefix):
            continue
        an = str(cell(row, "an") or "").strip()

        crits = {}
        for crit in CRITERIA:
            crits[crit] = {
                "verdict": str(cell(row, f"{crit}_verdict") or "").strip(),
                "certainty": str(cell(row, f"{crit}_certainty") or "").strip(),
                "rationale": str(cell(row, f"{crit}_rationale") or "").strip(),
            }
        crits["Intervention"] = {
            "verdict": str(cell(row, "Intervention_verdict") or "").strip(),
            "certainty": str(cell(row, "Intervention_certainty") or "").strip(),
            "rationale": str(cell(row, "Intervention_rationale") or "").strip(),
        }
        outcome = screen_outcome([crits[c]["verdict"] for c in CRITERIA])
        prev = existing.get(key) or {}
        entry = {
            "an": an,
            "coder": "OG",
            "criteria": crits,
            "decision": outcome,
            "id": key,
            "saved_at": prev.get("saved_at") or now,
        }
        if prev.get("criteria") != crits:
            entry["saved_at"] = now
        existing[key] = entry
        written += 1

    if not dry_run:
        tmp = os.path.join(TEMP, "OG.json.tmp")
        os.makedirs(TEMP, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(existing, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.write("\n")
        shutil.move(tmp, OG)
    return {"entries": written, "file_entries": len(existing)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--source", default="new_hit_2026-11-04",
                    help="Record_Source value of the rows to fill")
    ap.add_argument("--prefix", action="store_true",
                    help="treat --source as a prefix instead of an exact value")
    ap.add_argument("--results", default=RESULTS)
    args = ap.parse_args(argv)

    decisions, problems, files = load_results(args.results)
    print(f"result files: {len(files)}; valid decisions: {len(decisions)}")
    if problems:
        print(f"problems: {len(problems)}")
        for p in problems[:60]:
            print("  !", p)
        if len(problems) > 60:
            print(f"  ... {len(problems) - 60} more")
    if args.check or not args.apply:
        return 1 if problems else 0

    report = apply_to_master(decisions, args.source, dry_run=args.dry_run,
                             prefix=args.prefix)
    print("master:", json.dumps(report))
    og = apply_to_og(args.source, dry_run=args.dry_run, prefix=args.prefix)
    print("OG.json:", json.dumps(og))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
