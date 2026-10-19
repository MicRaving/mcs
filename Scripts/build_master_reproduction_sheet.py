"""Add/replace the ``Reproduction`` sheet in ``master_dataset.xlsx``.

Workstream W1-C (2026-09-22).  The sheet is now a **projection of the machine-generated
dossier index** ``Results/repro_dossier_index.csv`` (interface §7 / plan §C4) instead of a
re-parse of the reports, so:

* ``original_results`` / ``reproduced_results`` carry real numeric summaries
  (``yi=0.0165; ...``), not heading-name flags;
* the old ``n_reproduced`` participant-vs-contrast collision is gone: the participant N is
  ``n_participants_reproduced`` and the contrast count is ``n_contrasts_reproduced``.

The existing ``Search`` sheet is preserved exactly: the workbook is opened with openpyxl,
only the ``Reproduction`` sheet is removed/recreated, and the result is written to a temp
file and atomically moved over the original.  The original workbook is backed up first.

Run:  python Scripts/build_master_reproduction_sheet.py
"""
import os
import shutil
import sys
import time

import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
from safe_replace import safe_replace  # noqa: E402

MASTER = os.path.join(ROOT, "master_dataset.xlsx")
RESULTS = os.path.join(ROOT, "Results")
TEMP = os.path.join(ROOT, "temp", "ws_c")
BACKUP = os.path.join(ROOT, "archive", "ws_c_backup_2026-09-22")
INDEX_CSV = os.path.join(RESULTS, "repro_dossier_index.csv")

os.makedirs(TEMP, exist_ok=True)
os.makedirs(BACKUP, exist_ok=True)

# Projection of the index (real values; collision-free names).
COLUMNS = [
    "study_key", "has_dataset", "report_present", "reproduced_status",
    "original_results", "reproduced_results", "results_without_exclusions",
    "woe_d_on", "woe_d_off", "woe_delta",
    "woe_n_treat_on", "woe_n_control_on", "woe_n_treat_off", "woe_n_control_off",
    "without_exclusions_source", "preparations", "failures",
    "n_contrasts", "n_original", "n_participants_reproduced", "n_contrasts_reproduced",
    "n_reproduced_numeric", "n_domain_recalc_numeric", "n_woe_cells", "n_woe_full",
    "n_gap_rows", "n_without_exclusions", "discrepancy_count",
    "report_path", "dataset_path", "log_path", "command", "exit_code",
    "notes", "generated_at",
]


def _load_index():
    if not os.path.isfile(INDEX_CSV):
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(INDEX_CSV, encoding="utf-8-sig")


def build():
    idx = _load_index()
    if idx.empty:
        print("  ! repro_dossier_index.csv missing/empty; run "
              "python Scripts/build_repro_dossier.py first")
    # index column -> master column (real numbers, collision-free names)
    PROJ = {
        "study_key": "study_key", "has_dataset": "has_dataset",
        "report_present": "report_present", "reproduced_status": "reproduced_status",
        "original_results": "original_results", "reproduced_results": "reproduced_results",
        "results_without_exclusions": "results_without_exclusions",
        "woe_d_on": "woe_d_on", "woe_d_off": "woe_d_off", "woe_delta": "woe_delta",
        "woe_n_treat_on": "woe_n_treat_on", "woe_n_control_on": "woe_n_control_on",
        "woe_n_treat_off": "woe_n_treat_off", "woe_n_control_off": "woe_n_control_off",
        "without_exclusions_source": "without_exclusions_source",
        "preparations": "preparations", "failures": "failures",
        "n_contrasts": "n_contrasts", "n_original": "n_original",
        "n_participants_reproduced": "n_participants_reproduced",
        "n_reproduced": "n_contrasts_reproduced",
        "n_reproduced_numeric": "n_reproduced_numeric",
        "n_domain_recalc_numeric": "n_domain_recalc_numeric",
        "n_woe_cells": "n_woe_cells", "n_woe_full": "n_woe_full",
        "n_gap_rows": "n_gap_rows",
        "n_without_exclusions": "n_without_exclusions",
        "discrepancy_count": "discrepancy_count",
        "report_path": "report_path", "dataset_path": "dataset_path",
        "log_path": "log_path", "command": "command", "exit_code": "exit_code",
        "notes": "notes", "generated_at": "generated_at",
    }
    out = pd.DataFrame()
    for src, dst in PROJ.items():
        out[dst] = idx[src] if src in idx.columns else ""
    out = out.sort_values("study_key", key=lambda s: s.str.lower())
    out.to_csv(os.path.join(TEMP, "master_reproduction_sheet.csv"), index=False,
               encoding="utf-8-sig")
    out.to_excel(os.path.join(RESULTS, "master_reproduction_sheet.xlsx"),
                 sheet_name="Reproduction", index=False)
    print(f"  built Reproduction sheet: {len(out)} rows x {len(out.columns)} cols "
          f"(projection of repro_dossier_index.csv)")
    if len(out):
        print(f"  has_dataset: {out['has_dataset'].value_counts().to_dict()}")
        print(f"  reproduced_status: {out['reproduced_status'].value_counts().to_dict()}")
    return out


def write_workbook(df):
    import openpyxl
    bkp = os.path.join(BACKUP, "master_dataset.xlsx")
    if not os.path.exists(bkp):
        shutil.copy2(MASTER, bkp)
        print(f"  backed up master_dataset.xlsx -> {os.path.relpath(bkp, ROOT)}")
    else:
        print(f"  backup already exists: {os.path.relpath(bkp, ROOT)}")

    wb = openpyxl.load_workbook(MASTER)
    if "Reproduction" in wb.sheetnames:
        del wb["Reproduction"]
    ws = wb.create_sheet("Reproduction")
    ws.append(list(df.columns))
    for rec in df.itertuples(index=False):
        ws.append([None if (isinstance(v, float) and pd.isna(v)) else v for v in rec])

    tmp = os.path.join(TEMP, "master_dataset.new.xlsx")
    wb.save(tmp)
    wb.close()
    # Nextcloud keeps master_dataset.xlsx as a reparse-point placeholder that can reject an
    # atomic rename; fall back to an in-place byte copy (the placeholder opens for write).
    try:
        safe_replace(tmp, MASTER)
    except PermissionError:
        ok = False
        for i in range(6):
            try:
                shutil.copyfile(tmp, MASTER)
                ok = True
                break
            except PermissionError:
                time.sleep(0.5 * (i + 1))
        if not ok:
            raise
    print(f"  wrote {os.path.relpath(MASTER, ROOT)} (Reproduction sheet added/replaced)")


def verify():
    import openpyxl
    wb = openpyxl.load_workbook(MASTER, read_only=True)
    print("  verify sheetnames:", wb.sheetnames)
    for s in wb.sheetnames:
        print(f"    {s}: max_row={wb[s].max_row} max_col={wb[s].max_column}")
    wb.close()


if __name__ == "__main__":
    df = build()
    write_workbook(df)
    verify()
