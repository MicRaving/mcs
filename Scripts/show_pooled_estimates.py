"""Read the pooled estimates out of the canonical workbook.

The numbers the report quotes come from ``Results/metaanalysis.xlsx``;
this prints them so they can be checked without opening the workbook.
"""
import os
import sys

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")
XLSX = os.path.join(RESULTS, "metaanalysis.xlsx")


def main():
    if not os.path.isfile(XLSX):
        print("no workbook:", XLSX)
        return 1
    xl = pd.ExcelFile(XLSX)
    for sheet in ("MetaResults", "MetaResults_restricted"):
        if sheet not in xl.sheet_names:
            continue
        df = xl.parse(sheet)
        print("=" * 74)
        print(sheet, "  (%d rows)" % len(df))
        keep = [c for c in ("question", "scope", "group", "dl_k", "dl_est",
                            "dl_se", "dl_lo", "dl_hi", "dl_tau2", "dl_i2",
                            "max_weight_share_dl", "max_weight_cluster_dl")
                if c in df.columns]
        if not keep:
            keep = list(df.columns)[:8]
        with pd.option_context("display.width", 240, "display.max_columns",
                               20, "display.max_rows", 40):
            print(df[keep].to_string(index=False))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
