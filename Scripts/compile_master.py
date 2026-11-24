"""Compile ``master_dataset.xlsx`` from the base records + per-coder decisions.

The master screening workbook is a **derived** product (plan §11): nobody writes
it by hand any more.  It is rebuilt, deterministically, from

* the **base** ``Project/master_base.xlsx`` - the record list and the
  bibliographic columns (title, abstract, DOI, database export fields, duplicate
  bookkeeping, curation notes, ...).  The base carries the master sheet's header
  **with every decision column present but empty**, so the compiled workbook
  keeps today's column order; ``--init-base`` produces it from the current
  workbook by blanking exactly the cells a decision file owns.
* every ``Screening/<Tag>.json`` - the abstract screening decisions, one file
  per coder tag (written by the screening tab);
* every ``Fulltext/<Tag>.json`` - the full-text decisions, one file per coder
  tag (written by the full-text tab; legacy ``Screening/fulltext - <Tag>.json``
  files are read too, the new file winning per key).

The abstract merge is **the app's own merge core**
(``screening_lib.screening_merge_plan`` / ``apply_screening_decision``): a
non-empty decision value overwrites the cell, a blank one leaves it alone, and
``Screened_By`` / ``Screened_At`` record the coder whose decision the row shows.
Files are applied oldest-first (by the newest ``saved_at`` in the file, then by
tag), so a later coder pass supersedes an earlier one.

The full-text layer follows the **union rule** of
``Scripts/homogenize_fulltext_exclusions.py``: a record any coder excludes is
excluded in the master (``Fulltext_Verdict = Exclude``, the criterion label, the
note, ``Effective_Verdict = Exclude``); a record a coder includes gets
``Include``; a record nobody decided stays at the abstract stage
(``Effective_Verdict = Abstract excluded``).  The coded corpus' own
``Fulltext_Exclusion_Criterion`` field is untouched - the union of the two
sources stays the definition (``prisma_report.coded_fulltext_excluded``).

A full-text decision is keyed by the **master record id** (``an``, or
``row-<n>``, or ``<an>#2`` for a repeated accession number) when it is about one
record, and by the **study key** otherwise (``Smith et al. (2021)``): the app
decides per study, so the compiler resolves the key to a row by DOI, then PDF
filename stem, then title (``Extraction/<Key>.json`` supplies the DOI/title).
A decision may carry ``"effective"`` (a curated stage such as ``"Abstract
excluded"`` on a record that carries a full-text criterion); when absent the
rule above decides.

Memory and speed: the base is read **once** in ``openpyxl`` read-only mode and
the workbook written in write-only mode, so peak RSS stays far below the plan's
600 MB budget (``summary["peak_rss_mb"]``).  Pass ``trace_memory=True`` (CLI
``--trace-memory``) to also record a ``tracemalloc`` peak - accurate per
allocation, but several times slower.

CLI::

    python Scripts/compile_master.py                      # rebuild ./master_dataset.xlsx
    python Scripts/compile_master.py --out temp/m.xlsx    # somewhere else
    python Scripts/compile_master.py --init-base          # Project/master_base.xlsx from the workbook
    python Scripts/compile_master.py --check              # compile + cell diff, exit 1 on a difference
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import tempfile
import time
import tracemalloc
from collections import defaultdict
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.join(_HERE, "coding_web")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import fulltext_lib as FL              # noqa: E402  (criteria labels + Extraction index)
import screening_lib as SL             # noqa: E402  (the merge core)
from studio_lib import atomic_replace  # noqa: E402

MASTER_NAME = "master_dataset.xlsx"
BASE_REL = os.path.join("Project", "master_base.xlsx")

#: A decision column is a column the merge (abstract) or the full-text layer
#: writes: ``<criterion>_verdict`` / ``_certainty`` / ``_rationale`` (C1..C4,
#: ``Intervention``, and any criterion added later), the two provenance columns,
#: and the four full-text columns.
_DECISION_SUFFIXES = ("_verdict", "_certainty", "_rationale")
_DECISION_NAMES = frozenset({"screened_by", "screened_at",
                             "fulltext_exclusion_criterion", "fulltext_reason"})
FULLTEXT_COLUMNS = ("Fulltext_Verdict", "Fulltext_Exclusion_Criterion",
                    "Fulltext_Reason", "Effective_Verdict")
ABSTRACT_ONLY_STAGE = "Abstract excluded"
INCLUDE_STAGE = "Include"
EXCLUDE_STAGE = "Exclude"


# ---------------------------------------------------------------- paths / mode
def master_path(root):
    return os.path.join(root, MASTER_NAME)


def base_path(root):
    return os.path.join(root, BASE_REL)


def enabled(root):
    """Whether the project builds its master from decision files (a base exists)."""
    return os.path.isfile(base_path(root))


def is_decision_column(name):
    """Whether the master column *name* is owned by a decision file."""
    n = SL._norm(name)
    if not n:
        return False
    return n.endswith(_DECISION_SUFFIXES) or n in _DECISION_NAMES


def decision_columns(header):
    return [str(h) for h in header if is_decision_column(h)]


# ---------------------------------------------------------------- decisions
def _read_json(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def _newest_saved(decisions):
    newest = ""
    for record in decisions.values():
        if isinstance(record, dict):
            stamp = str(record.get("saved_at") or "")
            if stamp > newest:
                newest = stamp
    return newest


def _tag_of(path, prefix=""):
    stem = os.path.splitext(os.path.basename(path))[0]
    return stem[len(prefix):] if prefix and stem.startswith(prefix) else stem


def screening_decisions(root):
    """``[(tag, {uid: record})]`` in application order (oldest file first).

    Only ``Screening/<Tag>.json`` files are abstract decisions; the full-text
    files of that folder (``fulltext_agreed.json``, ``fulltext - <Tag>.json``)
    and the non-JSON sources are skipped.
    """
    out, errors = [], []
    folder = os.path.join(root, "Screening")
    for path in sorted(glob.glob(os.path.join(folder, "*.json"))):
        name = os.path.basename(path)
        if name == "fulltext_agreed.json" or name.startswith("fulltext - "):
            continue
        try:
            decisions = _read_json(path)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop a compile
            errors.append(f"{name}: {exc}")
            continue
        if decisions:
            out.append((_tag_of(path), decisions))
    out.sort(key=lambda kv: (_newest_saved(kv[1]), kv[0]))
    return out, errors


def fulltext_decisions(root):
    """``[(tag, {key: decision})]`` in application order (oldest file first).

    ``Fulltext/<Tag>.json`` is canonical; a legacy
    ``Screening/fulltext - <Tag>.json`` is merged underneath it, so a key the
    new file carries wins.
    """
    merged, errors = {}, {}
    legacy = os.path.join(root, "Screening")
    for path in sorted(glob.glob(os.path.join(legacy, "fulltext - *.json"))):
        tag = _tag_of(path, "fulltext - ")
        try:
            merged.setdefault(tag, {}).update(_read_json(path))
        except Exception as exc:  # noqa: BLE001
            errors.setdefault(tag, str(exc))
    folder = os.path.join(root, "Fulltext")
    for path in sorted(glob.glob(os.path.join(folder, "*.json"))):
        tag = _tag_of(path)
        try:
            merged.setdefault(tag, {}).update(_read_json(path))
        except Exception as exc:  # noqa: BLE001
            errors.setdefault(tag, str(exc))
    out = [(tag, decisions) for tag, decisions in merged.items() if decisions]
    out.sort(key=lambda kv: (_newest_saved(kv[1]), kv[0]))
    return out, sorted(errors.items())


def _fulltext_index(files):
    """``{record id | study key: [(order, tag, decision)]}`` in application order."""
    index = defaultdict(list)
    for order, (tag, decisions) in enumerate(files):
        for key in sorted(decisions, key=str):
            decision = decisions[key]
            if isinstance(decision, dict):
                index[str(key).strip()].append((order, tag, decision))
    return index


def _study_index(root):
    """How a row is recognised as a study: ``Extraction/<Key>.json``'s DOI/title."""
    info = FL._extraction_info(os.path.join(root, "Extraction"))
    by_doi, by_title = {}, {}
    for key, (doi, title) in info.items():
        if doi:
            by_doi.setdefault(doi, key)
        if title:
            by_title.setdefault(title, key)
    return {"by_doi": by_doi, "by_title": by_title, "keys": set(info)}


def _row_study_key(index, doi, title, stem):
    """The study key of a master row (DOI, then PDF filename stem, then title)."""
    ndoi = FL._norm_doi(doi)
    if ndoi and ndoi in index["by_doi"]:
        return index["by_doi"][ndoi]
    if stem and stem in index["keys"]:
        return stem
    ntitle = FL._norm(title)
    if ntitle and ntitle in index["by_title"]:
        return index["by_title"][ntitle]
    return ""


def _fulltext_cells(entries):
    """The four full-text cells of one row from its decisions (union rule).

    The **verdict** is the union's: any coder's exclusion excludes the row, and
    an inclusion never overrides it.  The criterion label, note and stage come
    from the newest decision that produced that verdict (entries are in
    application order), so the cell shows what the latest coder recorded - the
    same "later file wins" rule as the abstract merge.  Returns ``None`` when the
    row has no decision.
    """
    if not entries:
        return None
    excluded = [(tag, dec) for tag, dec in entries if dec.get("exclude")]
    if excluded:
        _tag, dec = excluded[-1]
        code = str(dec.get("criterion") or "other").strip() or "other"
        label = (str(dec.get("criterion_label") or "").strip()
                 or FL._CRIT_LABEL.get(code) or code)
        note = str(dec.get("note") or "").strip()
        effective = str(dec.get("effective") or "").strip() or EXCLUDE_STAGE
        return (EXCLUDE_STAGE, label, note, effective)
    _tag, dec = entries[-1]
    note = str(dec.get("note") or "").strip()
    effective = str(dec.get("effective") or "").strip() or INCLUDE_STAGE
    return (INCLUDE_STAGE, "", note, effective)


# ---------------------------------------------------------------- measurement
def _peak_rss_mb():
    """The process' peak resident set size in MB (``None`` when unavailable)."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class _Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            counters = _Counters()
            counters.cb = ctypes.sizeof(counters)
            query = getattr(psapi, "GetProcessMemoryInfo", None) or getattr(
                kernel32, "K32GetProcessMemoryInfo")
            query.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Counters), wintypes.DWORD]
            query.restype = wintypes.BOOL
            if query(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return round(counters.PeakWorkingSetSize / (1024 * 1024), 1)
        except Exception:  # noqa: BLE001 - measurement only
            pass
        return None
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports KiB, macOS bytes.
        return round(peak / 1024.0, 1) if sys.platform != "darwin" else round(peak / (1024.0 * 1024.0), 1)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- compile
def compile_master(root, out_path=None, *, base=None, trace_memory=False):
    """Rebuild the master workbook from the base and the decision files.

    *root* is the project folder; *out_path* defaults to
    ``<root>/master_dataset.xlsx``; *base* defaults to
    ``<root>/Project/master_base.xlsx`` (pass another workbook to compile a
    different base); *trace_memory* additionally records a ``tracemalloc`` peak
    (accurate, but several times slower).  Returns a summary dict (rows,
    columns, files, matched and unmatched decisions, peak memory, seconds).
    """
    import openpyxl
    from openpyxl import Workbook

    root = os.path.abspath(root)
    out_path = os.path.abspath(out_path or master_path(root))
    base = os.path.abspath(base or base_path(root))
    if not os.path.isfile(base):
        raise FileNotFoundError(
            f"the master base {base} does not exist; create it once with "
            f"`python Scripts/compile_master.py --root {root} --init-base`")

    started = time.time()
    tracing = bool(trace_memory) and not tracemalloc.is_tracing()
    if tracing:
        tracemalloc.start()
    baseline = tracemalloc.get_traced_memory()[1] if tracemalloc.is_tracing() else 0

    screening, screening_errors = screening_decisions(root)
    fulltext, fulltext_errors = fulltext_decisions(root)
    ft_index = _fulltext_index(fulltext)
    studies = _study_index(root)

    rows = screened = excluded = included = ft_disagreements = 0
    matched_ft = set()
    wb_in = openpyxl.load_workbook(base, read_only=True, data_only=False)
    wb_out = Workbook(write_only=True)
    # Deterministic metadata: two compiles of the same inputs carry the same
    # document properties (the zip archive still stamps its own mtime).
    wb_out.properties.creator = "CodingStudio compile_master"
    wb_out.properties.title = MASTER_NAME
    wb_out.properties.created = datetime(2000, 1, 1)
    wb_out.properties.modified = datetime(2000, 1, 1)
    os.makedirs(os.path.join(root, "temp"), exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".xlsx", dir=os.path.join(root, "temp"))
    os.close(fd)
    try:
        ws_in, header_row = SL._pick_sheet(wb_in)
        sheet = ws_in.title or "Sheet1"
        it = ws_in.iter_rows(values_only=True)
        above = [list(next(it, None) or ()) for _ in range(header_row)]
        header = [str(h).strip() if h is not None else "" for h in (next(it, None) or ())]
        normed = {SL._norm(h): i for i, h in reversed(list(enumerate(header))) if h}

        def column(name):
            i = normed.get(SL._norm(name))
            if i is None:
                i = len(header)
                header.append(name)
                normed[SL._norm(name)] = i
            return i

        # Full-text columns first, so the merge's provenance columns stay last.
        ft_columns = {name: column(name) for name in FULLTEXT_COLUMNS}
        # The base header decides which criteria columns exist: the compiler writes
        # the criteria the decision files carry and the base has a column for.  It
        # does not consult the active scheme, so a compile of another project's
        # base can never add a spurious criterion column.
        extra_keys = {key for _tag, decisions in screening
                      for record in decisions.values() if isinstance(record, dict)
                      for key in ((record.get("criteria") or {}))}
        plan = SL.screening_merge_plan(header, [], extra_keys)
        header = plan["header"]
        dropped_criteria = sorted(extra_keys - set(plan["target"]))
        cols = plan["cols"]
        pdf_column = normed.get(SL._norm("PDF_Filename"))

        ws_out = wb_out.create_sheet(title=sheet)
        for row in above:
            ws_out.append(row)
        ws_out.append(header)
        seen = {}
        for rownum, row in enumerate(it, start=header_row + 2):
            vals = list(row) + [None] * (len(header) - len(row))
            if not any(v not in (None, "") for v in row):
                ws_out.append(vals)
                continue
            rows += 1
            uid = SL.screening_row_uid(row, rownum, seen, plan["cols"])
            for tag, decisions in screening:
                decision = SL.screening_decision_for(decisions, uid, rownum)
                if SL.apply_screening_decision(vals, decision, tag, plan):
                    screened += 1
            pdf = str(SL._cell(row, pdf_column) or "") if pdf_column is not None else ""
            stem = os.path.splitext(os.path.basename(pdf))[0] if pdf else ""
            study = _row_study_key(studies, SL._cell(row, cols.get("doi")),
                                   SL._cell(row, cols.get("title")), stem)
            entries = list(ft_index.get(uid, ()))
            if study and study != uid:
                entries += ft_index.get(study, ())
            ordered = [(tag, dec) for _o, tag, dec in sorted(entries, key=lambda e: e[0])]
            cells = _fulltext_cells(ordered)
            if len({tag for tag, _d in ordered}) > 1:
                if (any(d.get("exclude") for _t, d in ordered)
                        and any(not d.get("exclude") for _t, d in ordered)):
                    ft_disagreements += 1
            if entries:
                matched_ft.add(uid)
                if study:
                    matched_ft.add(study)
            if cells is None:
                vals[ft_columns["Effective_Verdict"]] = ABSTRACT_ONLY_STAGE
            else:
                verdict, label, note, effective = cells
                vals[ft_columns["Fulltext_Verdict"]] = verdict
                vals[ft_columns["Fulltext_Exclusion_Criterion"]] = label or None
                vals[ft_columns["Fulltext_Reason"]] = note or None
                vals[ft_columns["Effective_Verdict"]] = effective
                if verdict == EXCLUDE_STAGE:
                    excluded += 1
                else:
                    included += 1
            ws_out.append(vals)
        wb_out.save(tmp)
        atomic_replace(tmp, out_path)
    finally:
        try:
            wb_in.close()
        except Exception:  # noqa: BLE001
            pass
        if os.path.exists(tmp):
            os.remove(tmp)

    peak_trace = None
    if tracemalloc.is_tracing():
        peak_trace = tracemalloc.get_traced_memory()[1]
        if tracing:
            tracemalloc.stop()
    summary = {
        "out": out_path,
        "base": base,
        "sheet": sheet,
        "rows": rows,
        "columns": len(header),
        "decision_columns": decision_columns(header),
        "screening": {
            "files": [tag for tag, _ in screening],
            "rows_matched": screened,
            "unmatched": sum(len(d) for _t, d in screening) - screened,
            "dropped_criteria": dropped_criteria,
            "errors": screening_errors,
        },
        "fulltext": {
            "files": [tag for tag, _ in fulltext],
            "counts": {tag: len(decisions) for tag, decisions in fulltext},
            "rows_decided": excluded + included,
            "rows_excluded": excluded,
            "rows_included": included,
            "rows_with_disagreement": ft_disagreements,
            "unmatched": sorted(set(ft_index) - matched_ft),
            "errors": [f"{tag}: {msg}" for tag, msg in fulltext_errors],
        },
        "seconds": round(time.time() - started, 2),
        "peak_tracemalloc_mb": (round((peak_trace - baseline) / (1024 * 1024), 1)
                                if peak_trace is not None else None),
        "peak_rss_mb": _peak_rss_mb(),
    }
    return summary


# ---------------------------------------------------------------- base
def _scan_base(path):
    """One streaming pass over a workbook: sheet, header, rows above it."""
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=False)
    try:
        ws, header_row = SL._pick_sheet(wb)
        it = ws.iter_rows(values_only=True)
        above = [list(next(it, None) or ()) for _ in range(header_row)]
        header = [str(h).strip() if h is not None else "" for h in (next(it, None) or ())]
        sheet = ws.title or "Sheet1"
    finally:
        wb.close()
    return {"sheet": sheet, "header_row": header_row, "above": above, "header": header}


def initialise_base(root, *, source=None, force=False):
    """Write ``<root>/Project/master_base.xlsx``: the workbook, decisions blanked.

    Every decision column (see :func:`is_decision_column`) is emptied; the values
    that leave the workbook are exactly the ones a decision file now owns.  The
    file is only written when it does not exist (or *force*), never over a base
    that is already in use.
    """
    import openpyxl
    from openpyxl import Workbook

    root = os.path.abspath(root)
    source = os.path.abspath(source or master_path(root))
    out = base_path(root)
    if not os.path.isfile(source):
        raise FileNotFoundError(f"the workbook {source} does not exist")
    if os.path.exists(out) and os.path.getsize(out) > 0 and not force:
        raise FileExistsError(f"{out} already exists (use --force to replace it)")

    scan = _scan_base(source)
    drop = decision_columns(scan["header"])
    drop_idx = {i for i, name in enumerate(scan["header"]) if is_decision_column(name)}
    blanked = 0
    wb_in = openpyxl.load_workbook(source, read_only=True, data_only=False)
    wb_out = Workbook(write_only=True)
    wb_out.properties.creator = "CodingStudio compile_master"
    wb_out.properties.title = "master_base"
    wb_out.properties.created = datetime(2000, 1, 1)
    wb_out.properties.modified = datetime(2000, 1, 1)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    os.makedirs(os.path.join(root, "temp"), exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".xlsx", dir=os.path.join(root, "temp"))
    os.close(fd)
    try:
        ws_in = wb_in[scan["sheet"]]
        ws_out = wb_out.create_sheet(title=scan["sheet"])
        for row in scan["above"]:
            ws_out.append(row)
        ws_out.append(scan["header"])
        it = ws_in.iter_rows(values_only=True)
        for _ in range(scan["header_row"] + 1):
            next(it, None)
        for row in it:
            vals = list(row)
            for i in drop_idx:
                if i < len(vals) and vals[i] not in (None, ""):
                    blanked += 1
                    vals[i] = None
            ws_out.append(vals)
        wb_out.save(tmp)
        atomic_replace(tmp, out)
    finally:
        try:
            wb_in.close()
        except Exception:  # noqa: BLE001
            pass
        if os.path.exists(tmp):
            os.remove(tmp)
    return {"base": out, "source": source, "sheet": scan["sheet"],
            "columns": len(scan["header"]), "decision_columns": drop,
            "blanked_cells": blanked}


# ---------------------------------------------------------------- CLI
def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=os.path.dirname(_HERE),
                        help="project folder (default: this checkout)")
    parser.add_argument("--out", default=None,
                        help="output workbook (default: <root>/master_dataset.xlsx)")
    parser.add_argument("--base", default=None,
                        help="base workbook (default: <root>/Project/master_base.xlsx)")
    parser.add_argument("--init-base", action="store_true",
                        help="write <root>/Project/master_base.xlsx from the current workbook")
    parser.add_argument("--from", dest="source", default=None,
                        help="workbook --init-base reads (default: <root>/master_dataset.xlsx)")
    parser.add_argument("--force", action="store_true",
                        help="replace an existing (non-empty) base")
    parser.add_argument("--check", action="store_true",
                        help="compile to a temp file and diff it against the current workbook")
    parser.add_argument("--against", default=None,
                        help="workbook --check compares with (default: the output path)")
    parser.add_argument("--first", type=int, default=20,
                        help="how many differing cells --check lists (default 20)")
    parser.add_argument("--trace-memory", action="store_true",
                        help="also record a tracemalloc peak (accurate, slower)")
    args = parser.parse_args(argv)

    if args.init_base:
        result = initialise_base(args.root, source=args.source, force=args.force)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.check:
        import compare_master

        out = args.out or os.path.join(os.path.join(args.root, "temp"), "master_check.xlsx")
        summary = compile_master(args.root, out, base=args.base,
                                 trace_memory=args.trace_memory)
        against = args.against or master_path(args.root)
        diff = compare_master.compare_master(against, out)
        summary = {k: v for k, v in summary.items() if k != "decision_columns"}
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        print(json.dumps(diff["counts"], indent=2, ensure_ascii=False))
        compare_master.print_differences(diff, limit=args.first)
        return 0 if diff["counts"]["value_differences"] == 0 else 1

    summary = compile_master(args.root, args.out, base=args.base,
                            trace_memory=args.trace_memory)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
