# -*- coding: utf-8 -*-
"""Repair ``dv_polarity`` in harmonized sidecars.

For every ``Datasets/<Key>/<Key> - harmonized_variables.csv`` that disagrees with
the shared rule (``audit_polarity.py`` flags ``mismatch``) this:

1. copies the original sidecar to ``archive/sidecar_backup_2026-09-22/<Key>/...``
   (mirroring the dataset path) BEFORE touching it, then
2. calls ``dv_polarity.normalize_sidecar_file`` to rewrite the mismatched rows.

It additionally blanks any out-of-vocabulary stored value (anything other than
``higher_is_better`` / ``higher_is_worse`` / blank, e.g. the legacy
``"ambiguous"``): the classifier returns ``None`` for those (leave-as-coded), so
blank is the correct "not oriented" representation.  ``needs_veracity`` rows are
left untouched -- the stimulus veracity cannot be invented here.

Run:  python Scripts/repair_polarity.py [--dry]
Exit: 0.
"""
import argparse
import glob
import os
import shutil
import sys

B = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(B, "Scripts"))
import dv_polarity as DP  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BACKUP_DIR = os.path.join(B, "archive", "sidecar_backup_2026-09-22")
ALLOWED = {DP.HIGHER, DP.LOWER, ""}


def sidecar_paths():
    return sorted(glob.glob(os.path.join(B, "Datasets", "*", "* - harmonized_variables.csv")))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry", action="store_true",
                    help="report what would change without writing or backing up")
    ap.add_argument("--backup-dir", default=BACKUP_DIR)
    args = ap.parse_args(argv)

    import pandas as pd

    n_files_fixed = 0
    n_rows_norm = 0
    n_rows_invalid = 0
    touched = []
    for p in sidecar_paths():
        study = os.path.basename(os.path.dirname(p))
        try:
            side = pd.read_csv(p, encoding="utf-8-sig", low_memory=False)
        except Exception as e:  # pragma: no cover
            print(f"  [skip] {study}: unreadable ({e!r})")
            continue
        if "uniform_variable" not in side.columns:
            continue
        _, n_changed, _ = DP.normalize_sidecar(side)
        if "dv_polarity" in side.columns:
            cur = side["dv_polarity"].fillna("").astype(str).map(DP._clean)
        else:
            cur = pd.Series([""] * len(side), index=side.index)
        bad = ~cur.isin(ALLOWED)
        n_bad = int(bad.sum())
        if not n_changed and not n_bad:
            continue

        touched.append((study, n_changed, n_bad))
        n_files_fixed += 1
        n_rows_norm += n_changed
        n_rows_invalid += n_bad

        if args.dry:
            print(f"  [dry] {study}: {n_changed} mismatch(es), {n_bad} invalid value(s)")
            continue

        # 1) back up the original BEFORE rewriting (mirror the dataset path).
        dest_dir = os.path.join(args.backup_dir, study)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, os.path.basename(p))
        if not os.path.exists(dest):
            shutil.copy2(p, dest)

        # 2) shared normalization, then blank the out-of-vocabulary values.
        n_done = DP.normalize_sidecar_file(p)
        if n_bad:
            side2 = pd.read_csv(p, encoding="utf-8-sig", low_memory=False)
            cur2 = side2["dv_polarity"].fillna("").astype(str).map(DP._clean)
            bad2 = ~cur2.isin(ALLOWED)
            if bad2.any():
                side2.loc[bad2, "dv_polarity"] = ""
                side2.to_csv(p, index=False, encoding="utf-8-sig")
        print(f"  [fixed] {study}: {n_done} mismatch(es) normalized, {n_bad} invalid value(s) blanked")

    print()
    print(f"repair_polarity: {len(sidecar_paths())} sidecars scanned, {n_files_fixed} "
          f"sidecar(s) {'would be ' if args.dry else ''}repaired")
    print(f"  rows normalized (rule mismatch): {n_rows_norm}")
    print(f"  rows blanked (invalid value):    {n_rows_invalid}")
    if not args.dry and touched:
        print(f"  originals backed up under: {args.backup_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
