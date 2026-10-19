#!/usr/bin/env python
"""run_harmonizers.py — run every study harmonizer and report the variable mapping.

The RUN-HARMONIZERS session script.  It

1. enumerates every ``Datasets/<Study Key>/harmonizer.py`` and runs it with
   ``python -X utf8`` (the same invocation ``reanalysis.py`` uses), capturing
   the return code and stderr; failures are reported, never fatal;
2. for each study reports: key, rows x cols of the wide
   ``<Key> - harmonized.csv``, the number of ``dv_*``/``cov_*`` variables, and
   the full variable mapping from ``<Key> - harmonized_variables.csv``
   (+ ``<Key> - variables.csv`` coded provenance): ``original_variable ->
   uniform_variable``, role, dv_domain, veracity, timepoint and
   ``dv_polarity`` (higher_is_better / higher_is_worse), flagging every
   ``_01`` rescale twin and every reversed (higher_is_worse) variable;
3. writes the machine-readable ``harmonization_mapping.xlsx`` at the root
   (``Mapping`` sheet = one row per uniform variable across all studies;
   ``Summary`` sheet = per-study counts, failures, polarity tallies) plus a
   concise console summary (studies, failures, total variables, polarity
   counts).  All intermediates go to ``temp/run_harmonizers/``.

Usage::

    python run_harmonizers.py                     # run harmonizers, then report
    python run_harmonizers.py --no-run            # report from existing sidecars only
    python run_harmonizers.py --only <substr>     # subset of studies (run + report)

Notes
-----
* Harmonizers regenerate their own outputs (``<Key> - harmonized.csv``,
  ``<Key> - harmonized_variables.csv``, ``<Key> - exclusions.csv``); raw data
  is never overwritten.
* After each run the shared veracity-dependent polarity rule
  (``Scripts/dv_polarity.py``) is re-applied exactly like ``reanalysis.py``
  does, so the reported ``dv_polarity`` is what the SDT layer consumes.
* The wide dimension *rows* is counted with a buffered newline scan of the CSV
  (header excluded), so multi-million-row datasets are not loaded.  The count
  assumes no embedded newlines inside quoted fields, which holds for the
  machine-written numeric wide tables.
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile

import numpy as np  # noqa: F401  (kept for parity with the suite's imports)
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))     # Scripts/analysis
SCRIPTS = os.path.dirname(_HERE)                       # Scripts
B = os.path.dirname(SCRIPTS)                           # project root
RESULTS = os.path.join(B, "Results")
os.makedirs(RESULTS, exist_ok=True)
DS = os.path.join(B, "Datasets")
TMP = os.path.join(B, "temp")
RUN_DIR = os.path.join(TMP, "run_harmonizers")
OUT_XLSX = os.path.join(RESULTS, "harmonization_mapping.xlsx")
DONE_FLAG = os.path.join(TMP, "RUN_HARMONIZERS_DONE.flag")
REPORT_JSON = os.path.join(RUN_DIR, "report.json")
MAPPING_CSV = os.path.join(RUN_DIR, "mapping_flat.csv")
SUMMARY_CSV = os.path.join(RUN_DIR, "summary_flat.csv")

os.makedirs(RUN_DIR, exist_ok=True)
tempfile.tempdir = TMP  # all intermediates stay under temp/

sys.path.insert(0, SCRIPTS)  # for dv_polarity (mirrors reanalysis.py)

try:  # console-safe output for study names with non-cp1252 characters
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: S110, BLE001  (best effort; exotic streams may lack reconfigure)
    pass

MAPPING_COLS = [
    "study_key", "uniform_variable", "original_variable", "role", "role_original",
    "dv_domain", "veracity", "timepoint", "dv_polarity", "family",
    "has_01_twin", "reversed", "coded_uniform", "coded_role", "coded_polarity", "notes",
]
SUMMARY_COLS = [
    "study_key", "status", "returncode", "wide_file", "sidecar_file",
    "rows", "cols", "n_01_cols", "n_mapping_rows", "n_dv", "n_cov", "n_flag",
    "n_metacog", "n_item", "n_other", "n_higher_is_better", "n_higher_is_worse",
    "n_polarity_blank", "n_polarity_other", "n_01_twins", "n_reversed",
    "stdout_tail", "stderr_tail",
]

_BLANK = {"", "nan", "none", "nat", "na", "<na>"}


def _clean(v):
    s = str(v).strip()
    return "" if s.lower() in _BLANK else s


def family_of(name):
    s = str(name)
    if s.startswith("dv_"):
        return "dv"
    if s.startswith("cov_"):
        return "cov"
    if s.startswith("metacog_"):
        return "metacog"
    if s.startswith("flag_"):
        return "flag"
    if s.startswith("item_"):
        return "item"
    return "other"


def read_csv(path):
    return pd.read_csv(path, encoding="utf-8-sig", low_memory=False,
                       dtype=str, keep_default_na=False)


# ---------------------------------------------------------------------------
# discovery / running
# ---------------------------------------------------------------------------
def discover_studies(only=None):
    """Sorted (key, folder) for every Datasets/ subdirectory (honours --only)."""
    out = []
    for d in sorted(glob.glob(os.path.join(DS, "*"))):
        if not os.path.isdir(d):
            continue
        key = os.path.basename(d)
        if only and only.lower() not in key.lower():
            continue
        out.append((key, d))
    return out


def run_harmonizer(folder):
    """Run one harmonizer.py; return (ok, returncode, stdout, stderr)."""
    h = os.path.join(folder, "harmonizer.py")
    if not os.path.isfile(h):
        return False, None, "", ""
    proc = subprocess.run([sys.executable, "-X", "utf8", h],
                          capture_output=True, check=False)
    return (proc.returncode == 0, proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


def _tail(text, n=1):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return " | ".join(lines[-n:]) if lines else ""


# ---------------------------------------------------------------------------
# sidecars / wide dims
# ---------------------------------------------------------------------------
def read_sidecar(folder, key):
    p = os.path.join(folder, key + " - harmonized_variables.csv")
    if not os.path.isfile(p):
        return None
    try:
        return read_csv(p)
    except Exception:  # noqa: BLE001  (best-effort read; caller tolerates None)
        return None


def read_wide_cols(path):
    """Header-only column read (nrows=0 keeps even 4.9M-row files cheap)."""
    if not os.path.isfile(path):
        return None
    try:
        hdr = pd.read_csv(path, nrows=0, encoding="utf-8-sig", low_memory=False)
        return [str(c).strip().lstrip("\ufeff") for c in hdr.columns]
    except Exception:  # noqa: BLE001  (best-effort read; caller tolerates None)
        return None


def count_rows(path):
    """Buffered newline count minus the header line.

    Assumes no embedded newlines in quoted fields (holds for the machine
    written wide tables); a trailing newline terminates the last record and is
    not an extra empty record.
    """
    n = 0
    tail = b""
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            n += chunk.count(b"\n")
            tail = chunk
    if n == 0:
        return 0
    return n if tail[-1:] != b"\n" else n - 1


def load_variables_map(folder, key):
    """original_variable -> {coded_uniform, coded_role, coded_polarity}."""
    vpath = os.path.join(folder, key + " - variables.csv")
    if not os.path.isfile(vpath):
        vpath = os.path.join(folder, "variables.csv")
    if not os.path.isfile(vpath):
        return {}
    try:
        v = read_csv(vpath)
    except Exception:  # noqa: BLE001  (best-effort read; caller tolerates {})
        return {}
    ucol = ("uniform_variable" if "uniform_variable" in v.columns
            else ("uniform_name" if "uniform_name" in v.columns else None))
    if ucol is None or "original_variable" not in v.columns:
        return {}
    out = {}
    for _, r in v.iterrows():
        o = _clean(r.get("original_variable"))
        u = _clean(r.get(ucol))
        if not o or not u:
            continue
        out.setdefault(o, {"coded_uniform": u,
                           "coded_role": _clean(r.get("role")),
                           "coded_polarity": _clean(r.get("dv_polarity"))})
    return out


def build_mapping(key, folder, wide_cols):
    side = read_sidecar(folder, key)
    if side is None:
        return []
    vmap = load_variables_map(folder, key)
    wset = set(wide_cols or [])
    rows = []
    for _, r in side.iterrows():
        uv = _clean(r.get("uniform_variable"))
        if not uv:
            continue
        orig = _clean(r.get("original_variable"))
        pol = _clean(r.get("dv_polarity"))
        coded = vmap.get(orig, {})
        rows.append({
            "study_key": key,
            "uniform_variable": uv,
            "original_variable": orig,
            "role": _clean(r.get("role")),
            "role_original": _clean(r.get("role_original")),
            "dv_domain": _clean(r.get("dv_domain")),
            "veracity": _clean(r.get("veracity")),
            "timepoint": _clean(r.get("timepoint")),
            "dv_polarity": pol,
            "family": family_of(uv),
            "has_01_twin": (uv + "_01") in wset,
            "reversed": pol.lower() == "higher_is_worse",
            "coded_uniform": coded.get("coded_uniform", ""),
            "coded_role": coded.get("coded_role", ""),
            "coded_polarity": coded.get("coded_polarity", ""),
            "notes": _clean(r.get("notes")),
        })
    return rows


def summarize(key, mapping, status, cols, wide_fn, sidecar_fn, stdout, stderr):
    fam = [m["family"] for m in mapping]
    pol = [m["dv_polarity"].lower() for m in mapping if m["family"] == "dv"]
    return {
        "study_key": key,
        "status": status,
        "returncode": None,  # filled by the caller after the subprocess metadata
        "wide_file": wide_fn,
        "sidecar_file": sidecar_fn,
        "rows": None,  # filled by the caller (buffered newline count)
        "cols": None,  # filled by the caller (header width)
        "n_01_cols": sum(1 for c in (cols or []) if c.endswith("_01")),
        "n_mapping_rows": len(mapping),
        "n_dv": fam.count("dv"),
        "n_cov": fam.count("cov"),
        "n_flag": fam.count("flag"),
        "n_metacog": fam.count("metacog"),
        "n_item": fam.count("item"),
        "n_other": fam.count("other"),
        "n_higher_is_better": pol.count("higher_is_better"),
        "n_higher_is_worse": pol.count("higher_is_worse"),
        "n_polarity_blank": pol.count(""),
        "n_polarity_other": sum(1 for p in pol
                                if p not in ("", "higher_is_better", "higher_is_worse")),
        "n_01_twins": sum(1 for m in mapping if m["has_01_twin"]),
        "n_reversed": sum(1 for m in mapping if m["reversed"]),
        "stdout_tail": _tail(stdout),
        "stderr_tail": _tail(stderr),
    }


# ---------------------------------------------------------------------------
# console rendering
# ---------------------------------------------------------------------------
def render_row(sum_row):
    s = sum_row
    status = {"ok": "ok", "failed": "FAIL", "no_output": "skip",
              "no_sidecar": "noside", "no_harmonizer": "none"}.get(s["status"], s["status"])
    dims = (f"{s['rows']:,}x{s['cols']}" if s["rows"] is not None and s["cols"] else "   -   ")
    return (f"[{status:<6}] {s['study_key']:<38} {dims:>11}  "
            f"dv={s['n_dv']:>3} cov={s['n_cov']:>3} flg={s['n_flag']:>3} "
            f"meta={s['n_metacog']:>3}  +{s['n_higher_is_better']:>3} "
            f"-{s['n_higher_is_worse']:>3} \u2205{s['n_polarity_blank']:>3}  "
            f"twins={s['n_01_twins']:>3}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Run harmonizers and report the variable mapping.")
    ap.add_argument("--no-run", action="store_true",
                    help="report from existing sidecars without re-running the harmonizers")
    ap.add_argument("--only", default=None, metavar="SUBSTR",
                    help="only studies whose key contains SUBSTR (case-insensitive)")
    args = ap.parse_args(argv)

    studies = discover_studies(args.only)
    # Only folders that are part of the harmonization contract count as
    # candidates: those with a harmonizer.py and/or an existing sidecar.
    # (Datasets/temp, Datasets/_meta_analyses etc. are not harmonized studies.)
    studies = [(k, d) for k, d in studies
               if os.path.isfile(os.path.join(d, "harmonizer.py"))
               or os.path.isfile(os.path.join(d, k + " - harmonized_variables.csv"))]
    if not studies:
        print("no harmonizer candidates found"
              + (f" matching '{args.only}'" if args.only else ""))
        return 0

    mode = "report-only (--no-run)" if args.no_run else "run"
    print(f"[run_harmonizers] {len(studies)} harmonizers ({mode})")
    print(f"[run_harmonizers] intermediates -> {RUN_DIR}")

    mapping_rows, summary_rows = [], []
    n_ran = n_failed = n_with_sidecar = 0

    import dv_polarity as _dvp  # mirrored from reanalysis.py

    for i, (key, folder) in enumerate(studies, 1):
        sidecar_fn = key + " - harmonized_variables.csv"
        wide_fn = key + " - harmonized.csv"
        sidecar_path = os.path.join(folder, sidecar_fn)
        wide_path = os.path.join(folder, wide_fn)

        has_harmonizer = os.path.isfile(os.path.join(folder, "harmonizer.py"))
        ok, rc, stdout, stderr = True, None, "", ""
        if not args.no_run:
            if has_harmonizer:
                ok, rc, stdout, stderr = run_harmonizer(folder)
                n_ran += 1
                # Veracity-dependent polarity: re-apply the shared rule to the just
                # written sidecar (every regeneration path covered, as in reanalysis.py).
                if ok:
                    try:
                        _dvp.normalize_sidecar_file(sidecar_path)
                        _dvp.normalize_variables_file(
                            os.path.join(folder, key + " - variables.csv"), sidecar_path)
                    except Exception:  # noqa: S110, BLE001  (polarity rule is best-effort)
                        pass
                    # Track-B runner post-hook: coder variable assignments (W1-B).
                    # Runs only when the assignments file exists and the module
                    # imports (defensive: the module may not exist yet).
                    va_path = os.path.join(folder, key + " - variable_assignments.csv")
                    if os.path.isfile(va_path):
                        try:
                            import variable_assignments as _va
                            # apply_files() reads the sidecar+wide, applies the
                            # coder assignments and writes them back atomically
                            # (apply() alone only returns the frames).
                            _va.apply_files(key, folder)
                        except ImportError:
                            pass
                        except Exception:  # noqa: S110, BLE001
                            pass
                else:
                    n_failed += 1
            else:
                ok = False  # sidecar-only folder: report read-only

        has_sidecar = os.path.isfile(sidecar_path)
        has_wide = os.path.isfile(wide_path)

        if args.no_run or ok:
            status = "ok" if (has_sidecar or has_wide) else "no_output"
        elif has_harmonizer:
            status = "failed"
        else:
            status = "no_harmonizer"

        if has_sidecar:
            n_with_sidecar += 1

        wide_cols = read_wide_cols(wide_path) if has_wide else None
        n_rows = count_rows(wide_path) if has_wide else None
        mapping = build_mapping(key, folder, wide_cols)
        mapping_rows.extend(mapping)

        sum_row = summarize(key, mapping, status, wide_cols, wide_fn, sidecar_fn,
                            stdout, stderr)
        sum_row["rows"] = n_rows
        sum_row["cols"] = len(wide_cols) if wide_cols else None
        sum_row["returncode"] = rc
        summary_rows.append(sum_row)
        print(render_row(sum_row), flush=True)

    summary = pd.DataFrame(summary_rows)[SUMMARY_COLS]
    mapping = pd.DataFrame(mapping_rows)[MAPPING_COLS] if mapping_rows else \
        pd.DataFrame(columns=MAPPING_COLS)

    # ---- intermediates (all under temp/) -----------------------------------
    summary.to_csv(SUMMARY_CSV, index=False, encoding="utf-8-sig")
    mapping.to_csv(MAPPING_CSV, index=False, encoding="utf-8-sig")
    with open(REPORT_JSON, "w", encoding="utf-8") as fh:
        json.dump({"mode": mode, "studies": summary.to_dict("records")},
                  fh, ensure_ascii=False, indent=1)

    # ---- machine-readable workbook (root) ----------------------------------
    with pd.ExcelWriter(OUT_XLSX, engine="openpyxl") as w:
        mapping.to_excel(w, sheet_name="Mapping", index=False)
        summary.to_excel(w, sheet_name="Summary", index=False)

    # ---- console aggregate --------------------------------------------------
    tot_dv = int(summary["n_dv"].sum())
    tot_cov = int(summary["n_cov"].sum())
    tot_vars = int(summary["n_mapping_rows"].sum())
    hib = int(summary["n_higher_is_better"].sum())
    hiw = int(summary["n_higher_is_worse"].sum())
    pblank = int(summary["n_polarity_blank"].sum())
    pother = int(summary["n_polarity_other"].sum())
    twins = int(summary["n_01_twins"].sum())
    runs = n_ran if not args.no_run else 0
    print("-" * 78)
    print(f"studies enumerated : {len(studies)}")
    if not args.no_run:
        print(f"harmonizers run    : {runs}   failed: {n_failed}")
    print(f"studies w/ sidecar : {n_with_sidecar}")
    print(f"uniform variables  : {tot_vars:,}   (dv={tot_dv:,} cov={tot_cov:,})")
    print(f"dv polarity        : higher_is_better={hib:,}  higher_is_worse={hiw:,}  "
          f"blank={pblank:,}  other={pother:,}")
    print(f"_01 twins flagged  : {twins:,}")
    print(f"mapping workbook   : {OUT_XLSX}")
    if n_failed:
        print(f"FAILURES: {n_failed}  (see Summary.stderr_tail / {REPORT_JSON})")

    # Only a real run may signal "harmonizers just ran": reanalysis.py relaxes its
    # freshness guard when this flag is recent, and a --no-run report must not
    # masquerade as a fresh harmonization pass.
    if not args.no_run:
        with open(DONE_FLAG, "w", encoding="utf-8") as fh:
            fh.write(f"run_harmonizers done {runs} runs, {n_failed} failed, "
                     f"{tot_vars} variables -> {OUT_XLSX}\n")
    return 1 if n_failed else 0


if __name__ == "__main__":
    sys.exit(main())