# -*- coding: utf-8 -*-
"""Build the combined, harmonized wide dataset across all study datasets.

Per project decision (2026-09-21), in addition to the per-study wide files
`Datasets/<Key>/<Key> - harmonized.csv`, the harmonization step also materialises
ONE combined wide table.  It is written as a **gzip-compressed CSV** so that base R
reads it directly with `read.csv("combined_harmonized.csv.gz")` (no packages) while
staying small enough to sync.  A parquet twin is available with `--parquet` for
`arrow::read_parquet()`.

To stay tractable the table keeps only the columns that appear in at least
`MIN_STUDIES` (default 3) study datasets; the full union is ~19k column names x
2.26M rows and is intentionally not materialised.

  combined_harmonized.csv.gz         one row per participant/session, wide, with a
                                     `study_key` column naming the source dataset
  combined_harmonized_variables.csv  sidecar: column, family, n_studies

The table contains the harmonized IDs plus every `dv_*` (raw + `_01`), `cov_*`
(raw + `_01`) and `flag_*` column that clears the threshold, and any other
harmonized variable (e.g. `condition_raw`, `metacog_*`).

Memory-safe: each study file is streamed in chunks and appended, so no single full
table is held in memory.  Intermediate manifest goes to `temp/`; nothing is written
into `Datasets/`.

Run:  python Scripts/combine_harmonized.py [--min-studies 3] [--parquet] [--limit N]
"""

import argparse
import gzip
import os
import sys
from collections import Counter

import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import tempfile  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DS = os.path.join(ROOT, "Datasets")
RESULTS = os.path.join(ROOT, "Results")
TMP = os.path.join(ROOT, "temp")
os.makedirs(RESULTS, exist_ok=True)
os.makedirs(TMP, exist_ok=True)
tempfile.tempdir = TMP

OUT_GZ = os.path.join(RESULTS, "combined_harmonized.csv.gz")
OUT_PARQUET = os.path.join(RESULTS, "combined_harmonized.parquet")
OUT_SIDECAR = os.path.join(RESULTS, "combined_harmonized_variables.csv")
SKIP = {"temp", "_meta_analyses"}
ID_COLS = ("study_key", "artifact", "pid", "condition")


def col_family(c):
    """Sort/space family of a column: 0=key, 1=ids, 2=dv raw, 3=dv_01, 4=cov raw,
    5=cov_01, 6=flag, 7=other."""
    c = str(c)
    if c == "study_key":
        return 0
    if c in ("artifact", "pid", "condition"):
        return 1
    if c.startswith("dv_"):
        return 3 if c.endswith("_01") else 2
    if c.startswith("cov_"):
        return 5 if c.endswith("_01") else 4
    if c.startswith("flag_"):
        return 6
    return 7


def is_string_col(c):
    fam = col_family(c)
    if fam in (0, 1, 6):        # study_key + ids + flags
        return True
    if fam == 7 and not str(c).startswith("metacog_"):
        return True
    return False


def discover():
    """[(study_key, path, [columns])] for every harmonized wide file."""
    files = []
    for d in sorted(os.listdir(DS)):
        if not os.path.isdir(os.path.join(DS, d)) or d in SKIP or d.startswith("."):
            continue
        f = os.path.join(DS, d, d + " - harmonized.csv")
        if not os.path.isfile(f):
            continue
        try:
            cols = list(pd.read_csv(f, encoding="utf-8-sig", nrows=0).columns)
        except Exception as e:
            print(f"  ! header read failed ({d}): {e}")
            continue
        files.append((d, f, cols))
    return files


def _kept_columns(files, min_studies):
    freq = Counter()
    for _, _, cols in files:
        for c in cols:
            freq[c] += 1
    keep = [c for c in freq if freq[c] >= min_studies]
    if "study_key" not in keep:
        keep.append("study_key")
    keep = sorted(set(keep), key=lambda c: (col_family(c), str(c)))
    return keep, freq


def build_combined(out_gz=OUT_GZ, out_sidecar=OUT_SIDECAR, min_studies=3,
                   chunksize=50000, limit=None, also_parquet=False,
                   out_parquet=OUT_PARQUET):
    """Materialise the combined wide dataset (gzip CSV); return a summary dict."""
    files = discover()
    if limit:
        files = files[:limit]
    if not files:
        raise SystemExit("no harmonized datasets found")

    keep, freq = _kept_columns(files, min_studies)

    writer = None
    if also_parquet:
        import pyarrow as pa
        import pyarrow.parquet as pq
        schema = pa.schema([(c, pa.string() if is_string_col(c) else pa.float64())
                            for c in keep])
        try:
            writer = pq.ParquetWriter(out_parquet, schema, compression="zstd")
        except Exception:
            writer = pq.ParquetWriter(out_parquet, schema, compression="snappy")

    gz_fh = gzip.open(out_gz, "wt", encoding="utf-8-sig", newline="")
    first = True
    manifest = []
    total_rows = 0
    try:
        for key, path, cols in files:
            avail = [c for c in keep if c in set(cols)]
            rows = 0
            try:
                reader = pd.read_csv(path, encoding="utf-8-sig",
                                     usecols=lambda c, _a=set(avail): c in _a,
                                     chunksize=chunksize, low_memory=False)
            except Exception as e:
                print(f"  ! read failed ({key}): {e}")
                continue
            for part in reader:
                part = part.reindex(columns=keep)
                part["study_key"] = key
                part.to_csv(gz_fh, header=first, index=False, na_rep="")
                first = False
                if writer is not None:
                    for c in keep:
                        if is_string_col(c):
                            part[c] = part[c].astype("string")
                        else:
                            part[c] = pd.to_numeric(part[c], errors="coerce")
                    tbl = pa.Table.from_pandas(part[keep], preserve_index=False)
                    writer.write_table(tbl.cast(schema))
                rows += len(part)
            total_rows += rows
            manifest.append({"study_key": key, "rows": rows, "cols_kept": len(avail)})
            print(f"  + {key[:56]:<56} rows={rows:<8} cols={len(avail)}")
    finally:
        gz_fh.close()
        if writer is not None:
            writer.close()

    # sidecar: one row per kept column with the number of studies that contain it
    side = pd.DataFrame({"column": keep,
                         "family": [col_family(c) for c in keep],
                         "n_studies": [freq[c] for c in keep]})
    side.to_csv(out_sidecar, index=False, encoding="utf-8-sig")
    pd.DataFrame(manifest).to_csv(
        os.path.join(TMP, "combined_harmonized_manifest.csv"),
        index=False, encoding="utf-8-sig")

    summary = {"studies": len(files), "rows": total_rows, "columns": len(keep),
               "min_studies": min_studies, "gz": out_gz,
               "parquet": out_parquet if also_parquet else None, "sidecar": out_sidecar}
    print(f"combined dataset: {total_rows:,} rows x {len(keep)} cols "
          f"(columns in >= {min_studies} studies) -> {out_gz}")
    return summary


def build_if_stale(min_studies=3, also_parquet=False):
    """Rebuild only when the combined file is missing or older than any input.

    Keeps the automatic post-harmonizer call cheap (the full build takes minutes).
    """
    files = discover()
    if not files:
        return None
    newest = max(os.path.getmtime(p) for _, p, _ in files)
    if os.path.isfile(OUT_GZ) and os.path.getmtime(OUT_GZ) >= newest:
        print(f"combined dataset is current ({os.path.basename(OUT_GZ)})")
        return None
    return build_combined(min_studies=min_studies, also_parquet=also_parquet)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--min-studies", type=int, default=3,
                    help="keep only columns present in at least this many studies")
    ap.add_argument("--chunksize", type=int, default=50000)
    ap.add_argument("--limit", type=int, default=None,
                    help="only the first N study files (testing)")
    ap.add_argument("--parquet", action="store_true",
                    help="also write the parquet twin (arrow::read_parquet)")
    ap.add_argument("--out", default=OUT_GZ, help="gzip CSV output path")
    args = ap.parse_args()
    build_combined(out_gz=args.out, min_studies=args.min_studies,
                   chunksize=args.chunksize, limit=args.limit,
                   also_parquet=args.parquet)


if __name__ == "__main__":
    main()
