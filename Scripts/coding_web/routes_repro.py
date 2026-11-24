"""Reproduction routes: the per-study harmonization mapping + reproduction reports,
and the background runners that (re)generate the wrappers and rebuild the two
campaign workbooks (``harmonization_mapping.xlsx`` and ``reproduction_summary.xlsx``).

Read-only with respect to ``Datasets/``: the overview/detail endpoints read the two
root workbooks, the per-study sidecars / exclusion files / reproduction reports and
the filesystem.  The only writers are the run endpoints, which execute the root
runner scripts (``run_harmonizers.py`` / ``run_reproducers.py``) or the
reproduction-wrapper generator in a daemon thread, streaming their combined stdout
into a pollable status object (the same pattern as the Analysis tab).

Endpoints
---------
GET  /api/repro/overview          one row per study (flags, mapping/DV counts,
                                  reproduction status, dossier file/result counts,
                                  log-present indicator, last-run stamp)
GET  /api/repro/study/<key>       the legacy per-study report (variable mapping,
                                  exclusion criteria, analysis applied,
                                  recalculated-vs-paper, reproduced-vs-not)
GET  /api/repro/study/<key>/dossier  the reproduction dossier (schema v1 JSON;
                                  synthesises a clearly-marked v0 fallback when the
                                  dossier file is absent), enriched with coder edits,
                                  the report markdown, the validation rows and the
                                  staleness verdict
GET  /api/repro/dossier/files/<key>  the file inventory (path, role, bytes,
                                  provenance, read_by); ``?live=1`` rescans the folder
GET  /api/repro/dossier/log/<key>    the run log tail + its resolved path (taken
                                  from the dossier's run.log_path, never re-derived)
POST /api/repro/dossier/edit      narrow coder write: source_location/source_quote,
                                  verdict override + reason, gap note -> the per-study
                                  results CSV + Results/repro_dossier_edits.jsonl
POST /api/repro/run               {kind: generate|harmonizers|reproducers|dossier|study,
                                  only?}
POST /api/repro/run/cancel        kill the running step (whole process tree)
GET  /api/repro/run/status        streamed log + ok/finished/cancelled flags

Coder edits are durable: ``build_repro_dossier.py`` rewrites the flat results CSV
with the machine columns only, so every read replays
``Results/repro_dossier_edits.jsonl`` on top of it.

A filtered run (``only``) makes the pipeline runners rewrite their aggregate
(``harmonization_mapping.xlsx`` / ``reproduction_summary.xlsx`` /
``repro_dossier_index.csv``) with the matching studies ONLY.  The run therefore
backs the aggregate up to ``temp/codingstudio_repro/backup/`` first and merges
the rows of every other study back afterwards (and restores the backup when the
runner left an unreadable file behind).
"""

from collections import deque
from datetime import datetime
from flask import Blueprint, abort, jsonify, request
import atexit
import csv
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

from studio_state import FROZEN, S, log
from studio_lib import atomic_replace

bp = Blueprint("repro", __name__)

HARM_XLSX = "harmonization_mapping.xlsx"
REPRO_XLSX = "reproduction_summary.xlsx"

# Canonical status vocabulary from run_reproducers.py (machine + documented).
_STATUS_NORM = {
    "reproduced": "reproduced",
    "partial": "partial",
    "not reproduced": "not_reproduced",
    "reported_only": "reported_only",
    "not reproducible": "not_reproducible",
    "crashed": "crashed",
    "not run": "not_run",
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _s(v):
    """Clean string: None/NaN sentinels and blanks -> ""."""
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("nan", "none", "nat", "na", "<na>") else s


def _int(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f == f else None  # NaN guard


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _stamp(path):
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")
    except OSError:
        return None


def _datasets_dir():
    return S.DATASETS or os.path.join(S.ROOT, "Datasets")


# ---------------------------------------------------------------------------
# dossier paths / readers (Results/repro_dossier/, interface §7)
# ---------------------------------------------------------------------------
def _results_dir():
    return os.path.join(S.ROOT, "Results")


def _dossier_dir():
    return os.path.join(_results_dir(), "repro_dossier")


def _index_path():
    return os.path.join(_results_dir(), "repro_dossier_index.csv")


def _validation_path():
    return os.path.join(_results_dir(), "repro_dossier_validation.csv")


def _edits_log_path():
    return os.path.join(_results_dir(), "repro_dossier_edits.jsonl")


def _dossier_path(key):
    return os.path.join(_dossier_dir(), key + ".json")


def _flat_csv_path(key):
    return os.path.join(_dossier_dir(), key + " - results.csv")


def _report_path(key):
    return os.path.join(S.ROOT, "Reproduction", key + ".md")


#: the SB / MM reliability routes are additive and ignored by the pipeline
#: (``parse_reproduction_reports.parse_all``, ``build_repro_dossier.enumerate_keys``).
_ROUTE_SUFFIX = re.compile(r" - (SB|MM)$", re.IGNORECASE)
_ROUTE_ARTEFACT = re.compile(r" - (SB|MM)(\.[^.]+)?$", re.IGNORECASE)
_BAD_KEY_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_key(key):
    """Reject path-traversal / separator keys (and route artefacts) before any join."""
    if not key or key in (".", "..") or len(key) > 240:
        return False
    if key != key.strip() or _BAD_KEY_CHARS.search(key):
        return False
    if key != os.path.basename(key) or os.path.isabs(key):
        return False
    if ".." in key:
        return False
    if _ROUTE_SUFFIX.search(key):
        return False
    return True


def _is_route_artefact(fn):
    """``<Key> - harmonized - SB.csv``, ``harmonizer - SB.py``, ``<Key> - MM.json``..."""
    low = fn.lower()
    if " - sb" not in low and " - mm" not in low:  # fast path (~14k files/overview)
        return False
    stem = os.path.splitext(fn)[0]
    return bool(_ROUTE_SUFFIX.search(stem) or _ROUTE_ARTEFACT.search(fn))


def _resolve_rel(rel):
    """A dossier-recorded relative path -> absolute ("" when blank)."""
    rel = _s(rel)
    if not rel:
        return ""
    if os.path.isabs(rel):
        return rel
    return os.path.join(S.ROOT, rel.replace("/", os.sep))


def _inside_root(path):
    """True when ``path`` resolves inside the data root (no reads elsewhere)."""
    try:
        root = os.path.realpath(S.ROOT)
        return os.path.commonpath([root, os.path.realpath(path)]) == root
    except ValueError:  # different drive
        return False


def _sig(path):
    """(abs path, mtime, size) cache signature; None when the file is absent."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (os.path.abspath(path), st.st_mtime, st.st_size)


# Every cache is keyed by the absolute path + mtime + size, so a re-root
# (POST /api/root) can never serve another root's rows.
_INDEX_CACHE = {"sig": None, "rows": {}}


def _dossier_index():
    """study_key -> index row, mtime-cached (one CSV read per change)."""
    sig = _sig(_index_path())
    if sig is None:
        return {}
    if _INDEX_CACHE["sig"] == sig:
        return _INDEX_CACHE["rows"]
    rows = {}
    for r in _read_csv_rows(sig[0]):
        k = _s(r.get("study_key"))
        if k:
            rows[k] = r
    _INDEX_CACHE.update(sig=sig, rows=rows)
    return rows


_DOSSIER_CACHE = {}


def _load_dossier_cached(key):
    """Dossier JSON, mtime-cached; None when absent/unreadable."""
    sig = _sig(_dossier_path(key))
    if sig is None:
        return None
    cached = _DOSSIER_CACHE.get(sig[0])
    if cached and cached[0] == sig:
        return cached[1]
    try:
        with open(sig[0], encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as e:  # noqa: BLE001
        log(f"repro: unreadable dossier {sig[0]}: {e!r}")
        data = None
    if not isinstance(data, dict):
        data = None
    _DOSSIER_CACHE[sig[0]] = (sig, data)
    return data


_VAL_CACHE = {"sig": "unset", "data": None}


def _validation_summary():
    """validate_repro_dossier.csv -> counts + per-study rows (mtime-cached)."""
    p = _validation_path()
    sig = _sig(p) or (os.path.abspath(p), None, None)
    if _VAL_CACHE["sig"] == sig and _VAL_CACHE["data"] is not None:
        return _VAL_CACHE["data"]
    mtime = sig[1]
    rows = _read_csv_rows(p) if mtime is not None else []
    by_key = {}
    n_err = n_warn = 0
    for r in rows:
        sev = _s(r.get("severity")).upper() or "ERROR"
        if sev == "WARNING":
            n_warn += 1
        else:
            n_err += 1
        by_key.setdefault(_s(r.get("study_key")), []).append({
            "contrast_id": _s(r.get("contrast_id")),
            "check": _s(r.get("check")),
            "detail": _s(r.get("detail")),
            "severity": sev,
        })
    data = {"n_problems": len(rows), "n_error": n_err, "n_warning": n_warn,
            "present": mtime is not None,
            "generated_at": _stamp(p) if mtime is not None else None,
            "by_key": by_key}
    _VAL_CACHE.update(sig=sig, data=data)
    return data


def _read_report(key):
    p = _report_path(key)
    if not os.path.isfile(p):
        return "", ""
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            return fh.read(), os.path.relpath(p, S.ROOT).replace("\\", "/")
    except OSError:
        return "", ""


def _newest_file(key):
    """(mtime, relpath) of the newest file in Datasets/<key>/ (for staleness)."""
    folder = os.path.join(_datasets_dir(), key)
    if not os.path.isdir(folder):
        return None, ""
    newest, name = None, ""
    for root, _dirs, files in os.walk(folder):
        for fn in files:
            if _is_route_artefact(fn):  # SB/MM writes must not mark the dossier stale
                continue
            p = os.path.join(root, fn)
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue
            if newest is None or m > newest:
                newest = m
                name = os.path.relpath(p, S.ROOT).replace("\\", "/")
    return newest, name


def _parse_stamp(s):
    txt = _s(s)
    if not txt:
        return None
    for fmt, cut in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%dT%H:%M:%S", 19),
                     ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(txt[:cut], fmt)
        except ValueError:
            continue
    return None


def _staleness(key, generated_at):
    """Is the dossier older than the newest file in its dataset folder?"""
    g = _parse_stamp(generated_at)
    m, name = _newest_file(key)
    out = {"stale": False, "generated_at": _s(generated_at),
           "newest_source": name, "newest_source_mtime": ""}
    if m is not None:
        out["newest_source_mtime"] = datetime.fromtimestamp(m).isoformat(timespec="seconds")
    if g is not None and m is not None:
        out["stale"] = datetime.fromtimestamp(m) > g
    return out


# ---------------------------------------------------------------------------
# file inventory (dossier snapshot, with a filesystem-scan fallback)
# ---------------------------------------------------------------------------
def _role_of(fn, key):
    """Filename-contract role (mirrors repro_dossier_lib._role_of, plus the SB/MM
    route artefacts, which the pipeline ignores and which are not raw data)."""
    low = fn.lower()
    if _is_route_artefact(fn):
        return "reliability route (SB/MM)"
    if low == "harmonizer.py":
        return "harmonizer"
    if low == key.lower() + " - reproduction.py":
        return "reproduction script"
    if low.endswith(" - repro_results.csv"):
        return "repro results (D10)"
    if low.endswith(" - repro_facts.json"):
        return "repro facts (D10)"
    if low.endswith(" - harmonized.csv") or low.endswith(" - harmonized_variables.csv"):
        return "harmonized (product)"
    if low.endswith(" - prepared.csv"):
        return "prepared (product)"
    if low.endswith(" - variables.csv"):
        return "variables sidecar"
    if low.endswith(" - dv_direction.csv"):
        return "dv_direction sidecar"
    if low.endswith(" - exclusions.csv"):
        return "exclusions sidecar"
    if low.endswith(" - conditions.csv"):
        return "conditions sidecar"
    if low.endswith(" - variable_assignments.csv"):
        return "assignments sidecar"
    if low in ("source.md", "readme.txt", "readme.md"):
        return "provenance"
    if "codebook" in low or "code_book" in low:
        return "codebook"
    if "survey" in low or "instrument" in low or "questionnaire" in low:
        return "survey instrument"
    if low.endswith(".pdf"):
        return "document"
    if low.endswith((".sav", ".dta", ".rds", ".rdata", ".rda", ".xlsx", ".xls", ".csv",
                     ".tsv", ".json", ".txt", ".zip", ".7z")):
        return "raw data"
    return "other"


def _scan_inventory(key):
    """Filesystem file inventory (fallback when no dossier snapshot exists)."""
    folder = os.path.join(_datasets_dir(), key)
    if not os.path.isdir(folder):
        return []
    scripts = []
    for fn in os.listdir(folder):
        if fn.endswith(".py"):
            try:
                with open(os.path.join(folder, fn), encoding="utf-8",
                          errors="replace") as fh:
                    scripts.append((fn, fh.read()))
            except OSError:
                pass
    prov = "SOURCE.md" if os.path.isfile(os.path.join(folder, "SOURCE.md")) else ""
    out = []
    for root, _dirs, files in os.walk(folder):
        for fn in sorted(files):
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, folder).replace("\\", "/")
            try:
                size = os.path.getsize(p)
            except OSError:
                size = None
            read_by = [s for s, txt in scripts if fn in txt]
            out.append({"file": rel, "role": _role_of(fn, key), "size_bytes": size,
                        "provenance": prov, "read_by": "; ".join(read_by)})
    return out


def _with_route_roles(rows):
    """Dossier inventory rows with the SB/MM artefacts re-labelled (the pipeline's
    ``_role_of`` files ``<Key> - harmonized - SB.csv`` as raw data)."""
    out = []
    for r in rows or []:
        fn = os.path.basename(str(r.get("file") or ""))
        if fn and _is_route_artefact(fn):
            r = dict(r, role="reliability route (SB/MM)")
        out.append(r)
    return out


def _file_counts(rows):
    """(n_raw, n_scripts, n_products) from a file inventory (SB/MM excluded)."""
    raw = scripts = products = 0
    for r in rows or []:
        fn = str(r.get("file") or "").lower()
        if _is_route_artefact(os.path.basename(fn)):
            continue
        role = str(r.get("role") or "").lower()
        if fn.endswith(".py"):
            scripts += 1
        elif role == "raw data":
            raw += 1
        elif role.endswith("(product)") or role.endswith("sidecar") or role.endswith("(d10)"):
            products += 1
    return raw, scripts, products


def _folder_facts(folder, key):
    """One ``os.walk`` (scandir) of ``Datasets/<key>/``: lower-cased top-level names
    -> mtime, plus the raw/script/product counts.  Replaces ~20 ``stat`` calls per
    study on the overview (slow on a synced Nextcloud folder)."""
    top = {}
    newest = None
    counts = [0, 0, 0]
    stack = [(folder, True)]
    while stack:
        path, is_top = stack.pop()
        try:
            entries = list(os.scandir(path))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir():
                    stack.append((e.path, False))
                    continue
                if is_top:
                    # DirEntry.stat() is free on Windows (FindFirstFile data)
                    top[e.name.lower()] = e.stat().st_mtime
            except OSError:
                continue
            fn = e.name
            if _is_route_artefact(fn):
                continue
            if is_top and (newest is None or top[fn.lower()] > newest):
                newest = top[fn.lower()]
            role = _role_of(fn, key).lower()
            if fn.lower().endswith(".py"):
                counts[1] += 1
            elif role == "raw data":
                counts[0] += 1
            elif (role.endswith("(product)") or role.endswith("sidecar")
                  or role.endswith("(d10)")):
                counts[2] += 1
    return {"top": top, "newest": newest, "counts": tuple(counts)}


def _listing(folder):
    """lower-cased file name -> mtime for one directory (one scandir); {} if absent."""
    out = {}
    try:
        with os.scandir(folder) as it:
            for e in it:
                try:
                    if e.is_file():
                        out[e.name.lower()] = e.stat().st_mtime
                except OSError:
                    continue
    except OSError:
        pass
    return out


# ---------------------------------------------------------------------------
# per-study results CSV (flat twin) + coder edits
# ---------------------------------------------------------------------------
_NUM_RESULT_COLS = {
    "original_value", "original_ci_low", "original_ci_high", "original_p",
    "original_n1", "original_n2", "reproduced_value", "reproduced_n_used",
    "delta_abs", "delta_rel", "domain_recalc_d", "domain_recalc_n",
    "woe_d_on", "woe_d_off", "woe_delta",
    "woe_n_treat_on", "woe_n_control_on", "woe_n_treat_off", "woe_n_control_off",
}
_INT_RESULT_COLS = {"contrast_type", "eligible_primary"}

#: coder-owned columns the edit endpoint may write (never the machine columns).
_EDIT_COLS = {
    "source_location": "source_location",
    "source_quote": "source_quote",
    "verdict_override": "verdict_override",
    "verdict_override_reason": "verdict_override_reason",
    "gap_note": "coder_gap_note",
}
_EXTRA_COLS = ["verdict_override", "verdict_override_reason", "coder_gap_note",
               "coder_edit_at", "coder"]
_CODER_FIELDS = tuple(_EXTRA_COLS)

#: the row-verdict vocabulary of repro_dossier_lib (``_rows_from_lib_facts``) + PARTIAL.
_VERDICTS = ("REPRODUCED", "reproduced in direction", "PARTIAL", "NOT REPRODUCED",
             "NOT COMPARABLE", "NOT CHECKABLE")
_VERDICT_CANON = {v.lower(): v for v in _VERDICTS}

_edit_lock = threading.Lock()


def _read_csv_table(path):
    """(fieldnames, rows) for a CSV, preserving order; ([], []) when absent."""
    if not os.path.isfile(path):
        return [], []
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rd = csv.DictReader(fh)
            fields = list(rd.fieldnames or [])
            rows = [dict(r) for r in rd]
        return fields, rows
    except Exception:  # noqa: BLE001
        return [], []


def _write_csv_table(path, fields, rows):
    """Atomic CSV write (temp in the same folder + os.replace with retries)."""
    d = os.path.dirname(path)
    tmp = os.path.join(d, ".tmp_repro_edit_" + os.path.basename(path))
    try:
        with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in fields})
        atomic_replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _csv_results(key):
    """Result rows from the flat twin, with numeric columns coerced."""
    out = []
    for r in _read_csv_rows(_flat_csv_path(key)):
        row = {}
        for k, v in r.items():
            if k in _NUM_RESULT_COLS:
                row[k] = _num(v)
            elif k in _INT_RESULT_COLS:
                row[k] = _int(v)
            else:
                row[k] = v
        out.append(row)
    return out


def _flat_results(key):
    return {_s(r.get("contrast_id")): r for r in _read_csv_rows(_flat_csv_path(key))
            if _s(r.get("contrast_id"))}


_EDITS_CACHE = {"sig": None, "by_key": {}}


def _edits_replay():
    """study_key -> contrast_id -> {column: last value} from the edit audit log.

    ``build_repro_dossier.py`` rewrites the flat results CSV with the machine
    columns only, which silently dropped every coder edit; the JSONL audit log is
    the durable record, so it is replayed on read (last write wins)."""
    sig = _sig(_edits_log_path())
    if sig is None:
        return {}
    if _EDITS_CACHE["sig"] == sig:
        return _EDITS_CACHE["by_key"]
    by_key = {}
    try:
        with open(sig[0], encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                k, cid, field = (_s(rec.get("study_key")), _s(rec.get("contrast_id")),
                                 _s(rec.get("field")))
                if not (k and cid and field):
                    continue
                cell = by_key.setdefault(k, {}).setdefault(cid, {})
                new = rec.get("new")
                cell[field] = "" if new is None else str(new)
                cell["coder"] = _s(rec.get("coder"))
                cell["coder_edit_at"] = _s(rec.get("at"))
    except OSError as e:
        log(f"repro: could not read the edit log: {e!r}")
    _EDITS_CACHE.update(sig=sig, by_key=by_key)
    return by_key


def _merge_coder_edits(results, flat, replay=None):
    """Overlay coder-owned fields onto dossier result rows: the flat twin first
    (non-empty values), then the replayed audit log (last value, may clear)."""
    replay = replay or {}
    for r in results:
        cid = _s(r.get("contrast_id"))
        c = flat.get(cid)
        if c:
            for f in ("source_location", "source_quote") + _CODER_FIELDS:
                v = _s(c.get(f))
                if v:
                    r[f] = v
        for f, v in (replay.get(cid) or {}).items():
            r[f] = v


def _last_run_for(key):
    """The durable run-log stamp: the dossier's recorded log path, never a slug."""
    rel = _s(_dossier_index().get(key, {}).get("log_path"))
    if not rel:
        d = _load_dossier_cached(key)
        if d:
            rel = _s((d.get("run") or {}).get("log_path"))
    p = _resolve_rel(rel)
    return _stamp(p) if p and os.path.isfile(p) else None


def _known_key(key):
    """A real study (index / dataset folder / report / extraction), not a bogus path."""
    if not _safe_key(key):
        return False
    if key in _dossier_index():
        return True
    if os.path.isdir(os.path.join(_datasets_dir(), key)):
        return True
    if os.path.isfile(_report_path(key)):
        return True
    if os.path.isfile(os.path.join(S.EXTRACTION, key + ".json")):
        return True
    return False


def _synth_dossier(key):
    """A clearly-marked minimal v0 when Results/repro_dossier/<Key>.json is absent."""
    idx = _dossier_index().get(key, {})
    harmonized = os.path.join(_datasets_dir(), key, key + " - harmonized.csv")
    return {
        "schema_version": 0,
        "synthesized": True,
        "generated_at": _s(idx.get("generated_at")),
        "study_key": key,
        "has_dataset": _s(idx.get("has_dataset")) == "yes" or os.path.isfile(harmonized),
        "fully_within": _is_fully_within(key),
        "file_inventory": _scan_inventory(key),
        "run": {
            "script": "", "command": _s(idx.get("command")), "batch_command": "",
            "log_path": _s(idx.get("log_path")), "exit_code": _int(idx.get("exit_code")),
            "runtime_s": _num(idx.get("runtime_s")), "n_rows": None,
            "status": _s(idx.get("reproduced_status")),
        },
        "status": _s(idx.get("reproduced_status")) or "unknown",
        "machine_status": _s(idx.get("machine_status")),
        "documented_status": _s(idx.get("documented_status")),
        "exclusion_rules": [],
        "exclusion_provenance": {"source": "", "computed": None, "reason": "",
                                 "cumulative": [], "loro": []},
        "results": _csv_results(key),
        "study_gaps": [],
        "preparations": _s(idx.get("preparations")),
        "failures": _s(idx.get("failures")),
        "discrepancy_count": _int(idx.get("discrepancy_count")) or 0,
        "dataset_backed": _s(idx.get("dataset_backed")) == "yes",
    }


# ---------------------------------------------------------------------------
# workbook / intermediate readers (openpyxl; no pandas in the packaged app)
# ---------------------------------------------------------------------------
_XLSX_CACHE = {}


def _cell(v):
    """JSON-safe cell value from openpyxl (datetime -> ISO string)."""
    if v is None:
        return ""
    if isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    return str(v)


def _read_sheet(path, sheet):
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001  (missing file / bad workbook -> empty)
        log(f"repro: could not read {path} [{sheet}]: {e!r}")
        return []
    try:
        if sheet not in wb.sheetnames:
            return []
        it = wb[sheet].iter_rows(values_only=True)
        try:
            header = next(it)
        except StopIteration:
            return []
        cols = [("" if c is None else str(c)) for c in header]
        rows = []
        for vals in it:
            if vals is None:
                continue
            rows.append({cols[i]: _cell(vals[i])
                         for i in range(min(len(cols), len(vals)))})
        return rows
    except Exception as e:  # noqa: BLE001
        log(f"repro: error reading {path} [{sheet}]: {e!r}")
        return []
    finally:
        try:
            wb.close()
        except Exception:  # noqa: BLE001, S110
            pass


def _sheet(path, sheet):
    """Read + mtime-cache one worksheet.  Falls back to the temp intermediates when
    the root workbook is absent, so the tab still works before a run."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    key = (os.path.abspath(path), sheet)
    cached = _XLSX_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    rows = _read_sheet(path, sheet)
    _XLSX_CACHE[key] = (mtime, rows)
    return rows


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def _read_csv_rows(path):
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            return [dict(r) for r in csv.DictReader(fh)]
    except Exception:  # noqa: BLE001
        return []


def _harmonization_summary():
    rows = _sheet(os.path.join(S.ROOT, "Results", HARM_XLSX), "Summary")
    if rows:
        return rows
    rep = _read_json(os.path.join(S.TEMP, "run_harmonizers", "report.json"))
    if isinstance(rep, dict):
        return rep.get("studies") or []
    return []


def _harmonization_mapping():
    rows = _sheet(os.path.join(S.ROOT, "Results", HARM_XLSX), "Mapping")
    if rows:
        return rows
    return _read_csv_rows(os.path.join(S.TEMP, "run_harmonizers", "mapping_flat.csv"))


def _reproduction_rows():
    rows = _sheet(os.path.join(S.ROOT, "Results", REPRO_XLSX), "Reproduction")
    if rows:
        return rows
    agg = _read_json(os.path.join(S.TEMP, "reproduce_aggregate.json"))
    if isinstance(agg, dict):
        return agg.get("reproduction") or []
    return []


def _reproduction_domains():
    rows = _sheet(os.path.join(S.ROOT, "Results", REPRO_XLSX), "Domains")
    if rows:
        return rows
    agg = _read_json(os.path.join(S.TEMP, "reproduce_aggregate.json"))
    if isinstance(agg, dict):
        return agg.get("domains") or []
    return []


# ---------------------------------------------------------------------------
# filesystem / per-study facts
# ---------------------------------------------------------------------------
_FW_CACHE = {}


def _is_fully_within(key, mtime=None):
    """Fully-within (single-arm, no-control) paper?  Mirrors studio_lib.summarise,
    cached by the extraction JSON's path + mtime (``mtime`` may come from a
    directory listing so the overview needs no per-study stat)."""
    path = os.path.abspath(os.path.join(S.EXTRACTION, key + ".json"))
    if mtime is None:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return False
    cached = _FW_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    val = False
    try:
        from studio_lib import is_fully_within, load_extraction
        val = bool(is_fully_within(load_extraction(key)))
    except Exception as e:  # noqa: BLE001  (best-effort; missing/unreadable coding)
        log(f"repro: fully-within check failed for {key!r}: {e!r}")
        val = False
    _FW_CACHE[path] = (mtime, val)
    return val


def _dataset_folders(ds):
    """Datasets/ sub-folders that can be studies (same exclusions as run_reproducers)."""
    out = []
    try:
        with os.scandir(ds) as it:
            for e in it:
                name = e.name
                if (name.startswith(".") or name.startswith("_") or name == "temp"
                        or not e.is_dir()):
                    continue
                out.append(name)
    except OSError:
        pass
    return out


def _is_contract_folder(name, top):
    low = name.lower()
    return ("harmonizer.py" in top or low + " - reproduction.py" in top
            or low + " - harmonized.csv" in top
            or low + " - harmonized_variables.csv" in top)


def _study_keys(facts=None):
    """Union of the Datasets/ folders that are part of the contract and every key
    named in the two workbooks (so a study whose data folder was removed still
    shows its reproduction verdict).  ``facts`` (name -> _folder_facts) avoids a
    second scan when the caller already walked the folders."""
    keys = set()
    ds = _datasets_dir()
    if facts is None:
        facts = {n: {"top": _listing(os.path.join(ds, n))} for n in _dataset_folders(ds)}
    for name, f in facts.items():
        if _is_contract_folder(name, f["top"]):
            keys.add(name)
    for r in _harmonization_summary():
        k = _s(r.get("study_key"))
        if k:
            keys.add(k)
    for r in _reproduction_rows():
        k = _s(r.get("key"))
        if k:
            keys.add(k)
    return sorted(keys, key=lambda s: (s.lower(), s))


def _study_flags(key, ds, top=None, reports=None, extractions=None):
    """Presence flags; the optional listings make it stat-free for the overview."""
    if top is None:
        top = _listing(os.path.join(ds, key))
    low = key.lower()
    if reports is None:
        reports = {low + ".md": 0} if os.path.isfile(_report_path(key)) else {}
    if extractions is None:
        extractions = ({low + ".json": 0}
                       if os.path.isfile(os.path.join(S.EXTRACTION, key + ".json")) else {})
    return {
        "has_harmonizer": "harmonizer.py" in top,
        "has_harmonized": low + " - harmonized.csv" in top,
        "has_sidecar": low + " - harmonized_variables.csv" in top,
        "has_reproducer": low + " - reproduction.py" in top,
        "has_report": low + ".md" in reports,
        "has_extraction": low + ".json" in extractions,
    }


def _log_present(rel, durable_logs):
    """Does a dossier-recorded log path exist?  Uses the Results/repro_logs listing
    for the common case, a stat only for other locations."""
    p = _resolve_rel(rel)
    if not p:
        return "", False
    d, fn = os.path.split(p)
    if os.path.normcase(os.path.abspath(d)) == os.path.normcase(
            os.path.abspath(os.path.join(_results_dir(), "repro_logs"))):
        return p, fn.lower() in durable_logs
    return p, os.path.isfile(p)


def _iso(ts):
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else None


def _repro_overview_row(key, harm_by_key, repro_by_key, ds, ctx):
    f = ctx["facts"].get(key) or {"top": {}, "counts": (0, 0, 0)}
    top = f["top"]
    flags = _study_flags(key, ds, top, ctx["reports"], ctx["extractions"])
    h = harm_by_key.get(key, {})
    r = repro_by_key.get(key, {})
    low = key.lower()

    raw_status = _s(r.get("status"))
    status = _STATUS_NORM.get(raw_status, raw_status or "not_run")
    if not flags["has_reproducer"] and not raw_status:
        status = "not_run"

    # dossier index (Results/repro_dossier_index.csv): the dossier JSON's own status,
    # without parsing 300+ dossier JSONs per overview
    idx = ctx["index"].get(key, {})
    status_source = "workbook" if raw_status else ""
    if not raw_status and _s(idx.get("dataset_backed")) == "yes":
        ds_status = _s(idx.get("reproduced_status"))
        if ds_status and ds_status != "unknown":
            status = _STATUS_NORM.get(ds_status, ds_status)
            status_source = "dossier index"

    n_files_raw, n_files_scripts, n_files_products = f["counts"]
    log_rel = _s(idx.get("log_path"))
    log_abs, log_present = _log_present(log_rel, ctx["durable_logs"])

    dossier_mtime = ctx["dossiers"].get(low + ".json")
    harm_mtime = top.get(low + " - harmonized.csv") or top.get(
        low + " - harmonized_variables.csv")
    log_mtime = None
    if log_present:
        log_mtime = ctx["durable_logs"].get(os.path.basename(log_abs).lower())
        if log_mtime is None:
            try:
                log_mtime = os.path.getmtime(log_abs)
            except OSError:
                log_mtime = None
    # dossier older than the newest top-level file of its dataset folder
    newest = f.get("newest")
    gen = _parse_stamp(idx.get("generated_at"))
    stale = bool(dossier_mtime and newest and gen
                 and datetime.fromtimestamp(newest) > gen)

    n_reproduced = _int(r.get("n_reproduced"))
    n_not_repro = _int(r.get("n_not_reproduced"))
    n_direction = _int(r.get("n_direction"))
    ext_mtime = ctx["extractions"].get(low + ".json")
    return {
        "study_key": key,
        "has_harmonizer": flags["has_harmonizer"],
        "has_harmonized": flags["has_harmonized"],
        "has_sidecar": flags["has_sidecar"],
        "has_reproducer": flags["has_reproducer"],
        "has_report": flags["has_report"],
        "has_extraction": flags["has_extraction"],
        "fully_within": _is_fully_within(key, ext_mtime) if ext_mtime else False,
        # harmonization summary (workbook)
        "mapping_vars": _int(h.get("n_mapping_rows")),
        "n_dv": _int(h.get("n_dv")),
        "n_cov": _int(h.get("n_cov")),
        "n_flag": _int(h.get("n_flag")),
        "n_01_twins": _int(h.get("n_01_twins")),
        "n_higher_is_better": _int(h.get("n_higher_is_better")) or 0,
        "n_higher_is_worse": _int(h.get("n_higher_is_worse")) or 0,
        "harmonizer_status": _s(h.get("status")),
        "harmonized_rows": _int(h.get("rows")),
        "harmonized_cols": _int(h.get("cols")),
        # reproduction summary (workbook)
        "repro_status": raw_status,
        "status": status,
        "status_source": status_source,
        "success": _s(r.get("success")),
        "machine_status": _s(r.get("machine_status")),
        "documented_status": _s(r.get("documented_status")),
        "documented_verdict": _s(r.get("documented_verdict")),
        "script_kind": _s(r.get("script_kind")),
        "n_contrasts": _int(r.get("n_contrasts")),
        "n_reproduced": n_reproduced,
        "n_direction": n_direction,
        "n_not_reproduced": n_not_repro,
        "n_recalc_rows": _int(r.get("n_recalc_rows")),
        "runtime_s": _num(r.get("runtime_s")),
        "returncode": _int(r.get("returncode")),
        "machine_disagrees": _s(r.get("machine_disagrees")),
        # dossier (Results/repro_dossier/) -- result counts + log indicator; the file
        # counts are a live scan of Datasets/<Key>/ (SB/MM route artefacts excluded)
        "dossier_present": dossier_mtime is not None,
        "dossier_generated_at": _s(idx.get("generated_at")),
        "dossier_status": _s(idx.get("reproduced_status")),
        "dossier_stale": stale,
        "n_files_raw": n_files_raw,
        "n_files_scripts": n_files_scripts,
        "n_files_products": n_files_products,
        "n_contrasts_dossier": _int(idx.get("n_contrasts")),
        "n_reproduced_dossier": _int(idx.get("n_reproduced")),
        "n_not_reproduced_dossier": _int(idx.get("n_not_reproduced")),
        "n_gaps_dossier": _int(idx.get("n_gap_rows")),
        "n_validation": len(ctx["validation"].get(key, [])),
        "log_path": log_rel,
        "log_present": log_present,
        "last_run": _iso(log_mtime),
        "harmonized_modified": _iso(harm_mtime),
    }


def _overview_ctx(ds):
    """One directory listing per folder the overview needs."""
    facts = {n: _folder_facts(os.path.join(ds, n), n) for n in _dataset_folders(ds)}
    return {
        "facts": facts,
        "reports": _listing(os.path.join(S.ROOT, "Reproduction")),
        "extractions": _listing(S.EXTRACTION),
        "dossiers": _listing(_dossier_dir()),
        "durable_logs": _listing(os.path.join(_results_dir(), "repro_logs")),
        "index": _dossier_index(),
        "validation": _validation_summary()["by_key"],
    }


# ---------------------------------------------------------------------------
# GET /api/repro/overview
# ---------------------------------------------------------------------------
@bp.get("/api/repro/overview")
def api_repro_overview():
    ds = _datasets_dir()
    harm = _harmonization_summary()
    repro = _reproduction_rows()
    harm_by_key = {_s(r.get("study_key")): r for r in harm if _s(r.get("study_key"))}
    repro_by_key = {_s(r.get("key")): r for r in repro if _s(r.get("key"))}
    ctx = _overview_ctx(ds)

    studies = [_repro_overview_row(k, harm_by_key, repro_by_key, ds, ctx)
               for k in _study_keys(ctx["facts"])]

    def _count(pred):
        return sum(1 for s in studies if pred(s))

    by_status = {}
    for s in studies:
        by_status[s["status"]] = by_status.get(s["status"], 0) + 1

    generator = _generator_script()
    return jsonify({
        "root": S.ROOT,
        "datasets": ds,
        "mapping_xlsx": os.path.isfile(os.path.join(S.ROOT, "Results", HARM_XLSX)),
        "reproduction_xlsx": os.path.isfile(os.path.join(S.ROOT, "Results", REPRO_XLSX)),
        "mapping_available": bool(harm),
        "reproduction_available": bool(repro),
        "reproduction_modified": _stamp(os.path.join(S.ROOT, "Results", REPRO_XLSX)),
        "generator_available": bool(generator),
        "generator_name": os.path.basename(generator) if generator else "",
        "scripts_available": {k: bool(_runner(k)) for k in
                              ("harmonizers", "reproducers", "dossier", "validate")},
        "counts": {
            "studies": len(studies),
            "harmonized": _count(lambda s: s["has_harmonized"]),
            "harmonizers": _count(lambda s: s["has_harmonizer"]),
            "reproducers": _count(lambda s: s["has_reproducer"]),
            "reports": _count(lambda s: s["has_report"]),
            "fully_within": _count(lambda s: s["fully_within"]),
            "reproduced": by_status.get("reproduced", 0),
            "partial": by_status.get("partial", 0),
            "not_reproduced": by_status.get("not_reproduced", 0),
            "not_reproducible": by_status.get("not_reproducible", 0),
            "reported_only": by_status.get("reported_only", 0),
            "crashed": by_status.get("crashed", 0),
            "not_run": by_status.get("not_run", 0),
            "dossiers": _count(lambda s: s["dossier_present"]),
            "stale_dossiers": _count(lambda s: s["dossier_stale"]),
            "logs": _count(lambda s: s["log_present"]),
        },
        "by_status": by_status,
        "dossier": {
            "index_present": os.path.isfile(_index_path()),
            "index_modified": _stamp(_index_path()),
            "n_index_studies": len(ctx["index"]),
            "validation": {k: v for k, v in _validation_summary().items() if k != "by_key"},
        },
        "run": _run_snapshot(with_log=False),
        "studies": studies,
    })


# ---------------------------------------------------------------------------
# GET /api/repro/study/<key>
# ---------------------------------------------------------------------------
def _read_exclusions(key, ds):
    """Rows of ``Datasets/<Key>/<Key> - exclusions.csv`` + presence flags."""
    path = os.path.join(ds, key, key + " - exclusions.csv")
    if not os.path.isfile(path):
        return [], False, False
    rows = _read_csv_rows(path)
    out = []
    for r in rows:
        out.append({
            "flag": _s(r.get("flag")),
            "keep": _s(r.get("keep")),
            "applies_to": _s(r.get("applies_to")),
            "reason": _s(r.get("reason")),
        })
    return out, True, not out


def _mapping_for(key):
    rows = []
    for m in _harmonization_mapping():
        if _s(m.get("study_key")) != key:
            continue
        rows.append({
            "uniform_variable": _s(m.get("uniform_variable")),
            "original_variable": _s(m.get("original_variable")),
            "role": _s(m.get("role")),
            "role_original": _s(m.get("role_original")),
            "dv_domain": _s(m.get("dv_domain")),
            "veracity": _s(m.get("veracity")),
            "timepoint": _s(m.get("timepoint")),
            "dv_polarity": _s(m.get("dv_polarity")),
            "family": _s(m.get("family")),
            "has_01_twin": bool(m.get("has_01_twin")),
            "reversed": bool(m.get("reversed")),
            "coded_uniform": _s(m.get("coded_uniform")),
            "coded_role": _s(m.get("coded_role")),
            "coded_polarity": _s(m.get("coded_polarity")),
            "notes": _s(m.get("notes")),
        })
    return rows


@bp.get("/api/repro/study/<path:key>")
def api_repro_study(key):
    if not _safe_key(key):
        abort(404)
    ds = _datasets_dir()
    folder = os.path.join(ds, key)
    known = _known_key(key) or key in _study_keys()
    if not known:
        abort(404)

    flags = _study_flags(key, ds)
    mapping = _mapping_for(key)
    mapping_counts = {
        "total": len(mapping),
        "dv": sum(1 for m in mapping if m["family"] == "dv"),
        "cov": sum(1 for m in mapping if m["family"] == "cov"),
        "flag": sum(1 for m in mapping if m["family"] == "flag"),
        "metacog": sum(1 for m in mapping if m["family"] == "metacog"),
        "item": sum(1 for m in mapping if m["family"] == "item"),
        "higher_is_better": sum(1 for m in mapping
                                if m["dv_polarity"] == "higher_is_better"),
        "higher_is_worse": sum(1 for m in mapping
                               if m["dv_polarity"] == "higher_is_worse"),
        "polarity_blank": sum(1 for m in mapping
                              if m["family"] == "dv" and not m["dv_polarity"]),
        "reversed": sum(1 for m in mapping if m["reversed"]),
        "twins": sum(1 for m in mapping if m["has_01_twin"]),
    }

    exclusions, exclusions_present, exclusions_header_only = _read_exclusions(key, ds)

    harm_by_key = {_s(r.get("study_key")): r for r in _harmonization_summary()}
    repro_by_key = {_s(r.get("key")): r for r in _reproduction_rows()}
    h = harm_by_key.get(key, {})
    r = repro_by_key.get(key, {})
    summary = {
        "raw_status": _s(r.get("status")),
        "status": _STATUS_NORM.get(_s(r.get("status")), _s(r.get("status")) or "not_run"),
        "success": _s(r.get("success")),
        "machine_status": _s(r.get("machine_status")),
        "documented_status": _s(r.get("documented_status")),
        "documented_verdict": _s(r.get("documented_verdict")),
        "documented_reason": _s(r.get("documented_reason")),
        "script_kind": _s(r.get("script_kind")),
        "exclusions": _s(r.get("exclusions")),
        "analysis_applied": _s(r.get("analysis_applied")),
        "recalc_vs_reported": _s(r.get("recalc_vs_reported")),
        "n_contrasts": _int(r.get("n_contrasts")),
        "n_reproduced": _int(r.get("n_reproduced")),
        "n_direction": _int(r.get("n_direction")),
        "n_not_reproduced": _int(r.get("n_not_reproduced")),
        "n_not_comparable": _int(r.get("n_not_comparable")),
        "n_not_checkable": _int(r.get("n_not_checkable")),
        "n_recalc_rows": _int(r.get("n_recalc_rows")),
        "runtime_s": _num(r.get("runtime_s")),
        "returncode": _int(r.get("returncode")),
        "error": _s(r.get("error")),
        "machine_disagrees": _s(r.get("machine_disagrees")),
        "log_file": _s(r.get("log_file")),
        "present": bool(r),
    }
    harmonizer = {
        "status": _s(h.get("status")),
        "returncode": _int(h.get("returncode")),
        "rows": _int(h.get("rows")),
        "cols": _int(h.get("cols")),
        "n_01_cols": _int(h.get("n_01_cols")),
        "n_mapping_rows": _int(h.get("n_mapping_rows")),
        "n_dv": _int(h.get("n_dv")),
        "n_cov": _int(h.get("n_cov")),
        "n_flag": _int(h.get("n_flag")),
        "n_01_twins": _int(h.get("n_01_twins")),
        "n_reversed": _int(h.get("n_reversed")),
        "stderr_tail": _s(h.get("stderr_tail")),
        "present": bool(h),
    }

    domains = []
    for d in _reproduction_domains():
        if _s(d.get("key")) != key:
            continue
        domains.append({
            "domain": _s(d.get("domain")),
            "result": _s(d.get("result")),
            "contrast": _s(d.get("contrast")),
            "coded_effect": _s(d.get("coded_effect")),
            "recalc_twin": _s(d.get("recalc_twin")),
            "note": _s(d.get("note")),
            "source": _s(d.get("source")),
        })

    report_md, _report_rel = _read_report(key)

    return jsonify({
        "key": key,
        "exists": os.path.isdir(folder) or known,
        "folder_exists": os.path.isdir(folder),
        "flags": flags,
        "fully_within": _is_fully_within(key),
        "mapping": mapping,
        "mapping_counts": mapping_counts,
        "exclusions": exclusions,
        "exclusions_present": exclusions_present,
        "exclusions_header_only": exclusions_header_only,
        "summary": summary,
        "harmonizer": harmonizer,
        "domains": domains,
        "report_available": bool(report_md),
        "report_markdown": report_md,
        "last_run": _last_run_for(key),
        "run": _run_snapshot(with_log=False),
    })


# ---------------------------------------------------------------------------
# GET /api/repro/study/<key>/dossier  (the reproduction dossier)
# ---------------------------------------------------------------------------
@bp.get("/api/repro/study/<path:key>/dossier")
def api_repro_dossier(key):
    if not _safe_key(key):
        abort(404)
    dossier = _load_dossier_cached(key)
    synthesized = False
    if not isinstance(dossier, dict):
        if not _known_key(key):
            abort(404)
        dossier = _synth_dossier(key)
        synthesized = True
    out = dict(dossier)
    out["results"] = [dict(r) for r in (dossier.get("results") or [])]
    out["file_inventory"] = _with_route_roles(dossier.get("file_inventory"))
    replay = _edits_replay().get(key, {})
    _merge_coder_edits(out["results"], _flat_results(key), replay)
    out["n_coder_edits"] = len(replay)
    report_md, report_rel = _read_report(key)
    out["report_markdown"] = report_md
    out["report_path"] = report_rel
    out["report_available"] = bool(report_md)
    out["dossier_path"] = (os.path.relpath(_dossier_path(key), S.ROOT).replace("\\", "/")
                           if os.path.isfile(_dossier_path(key)) else "")
    out["dossier_present"] = not synthesized
    out["synthesized"] = synthesized
    if synthesized:
        out["note"] = ("Dossier JSON absent; a minimal v0 was synthesized from the "
                       "repro_dossier_index and the flat results CSV.")
    idx = _dossier_index().get(key, {})
    out["index"] = {k: _cell(idx.get(k)) for k in (
        "study_key", "generated_at", "reproduced_status", "machine_status",
        "documented_status", "n_contrasts", "n_reproduced", "n_gap_rows",
        "n_exclusion_rules", "n_exclusion_provenance_rules", "n_woe_cells",
        "report_path", "dataset_path", "log_path", "command", "exit_code",
        "runtime_s", "preparations", "discrepancy_count")}
    val = _validation_summary()
    out["validation"] = {k: v for k, v in val.items() if k != "by_key"}
    out["validation_rows"] = val["by_key"].get(key, [])
    out["staleness"] = _staleness(key, out.get("generated_at"))
    out["fully_within"] = bool(out.get("fully_within")) or _is_fully_within(key)
    # the campaign workbook's verdict; it moves on every reproducer run while the
    # dossier only moves when build_repro_dossier.py runs
    wb = next((r for r in _reproduction_rows() if _s(r.get("key")) == key), {})
    wb_status = _s(wb.get("status"))
    out["workbook_status"] = _STATUS_NORM.get(wb_status, wb_status)
    out["status_norm"] = _STATUS_NORM.get(_s(out.get("status")), _s(out.get("status")))
    out["workbook_disagrees"] = bool(wb_status and out["status_norm"]
                                     and out["workbook_status"] != out["status_norm"])
    out["run_commands"] = {
        "study": f'python Scripts/analysis/run_reproducers.py --only "{key}"',
        "dossier": f'python Scripts/build_repro_dossier.py --only "{key}"',
    }
    return jsonify(out)


# ---------------------------------------------------------------------------
# GET /api/repro/dossier/files/<key>  (file inventory)
# ---------------------------------------------------------------------------
@bp.get("/api/repro/dossier/files/<path:key>")
def api_repro_dossier_files(key):
    if not _safe_key(key):
        abort(404)
    live = request.args.get("live", "").lower() in ("1", "true", "yes")
    dossier = None if live else _load_dossier_cached(key)
    if dossier and dossier.get("file_inventory"):
        files = _with_route_roles(dossier["file_inventory"])
        source = "dossier"
    else:
        files = _scan_inventory(key)
        source = "live scan" if live else "filesystem scan (no dossier)"
    return jsonify({"key": key, "files": files, "count": len(files), "source": source,
                    "live": live or source != "dossier"})


# ---------------------------------------------------------------------------
# GET /api/repro/dossier/log/<key>  (run log tail; dossier-recorded path)
# ---------------------------------------------------------------------------
@bp.get("/api/repro/dossier/log/<path:key>")
def api_repro_dossier_log(key):
    if not _safe_key(key):
        abort(404)
    dossier = _load_dossier_cached(key)
    rel = _s((dossier.get("run") or {}).get("log_path")) if dossier else ""
    source = "dossier"
    if not rel:
        rel = _s(_dossier_index().get(key, {}).get("log_path"))
        source = "index"
    p = _resolve_rel(rel)
    reason = "no run log recorded for this study"
    if p and not _inside_root(p):
        reason = "the recorded log path lies outside the data root; not read"
        p = ""
    exists = bool(p and os.path.isfile(p))
    tail, size = "", 0
    if exists:
        size, tail = _tail_file(p, LOG_TAIL_BYTES)
    return jsonify({
        "key": key,
        "log_path": rel,
        "resolved": p if exists else "",
        "exists": exists,
        "bytes": size,
        "truncated": size > LOG_TAIL_BYTES,
        "modified": _stamp(p) if exists else None,
        "tail": tail,
        "source": source,
        "reason": "" if exists else reason,
    })


LOG_TAIL_BYTES = 60000


def _tail_file(path, nbytes):
    """(size, last ``nbytes`` decoded) without reading a huge log into memory."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > nbytes:
                fh.seek(size - nbytes)
            data = fh.read()
    except OSError as e:
        return 0, f"[could not read log: {e}]"
    txt = data.decode("utf-8", errors="replace")
    if size > nbytes:
        nl = txt.find("\n")
        txt = "[... earlier output truncated ...]\n" + (txt[nl + 1:] if nl >= 0 else txt)
    return size, txt


# ---------------------------------------------------------------------------
# POST /api/repro/dossier/edit  (narrow coder-owned write)
# ---------------------------------------------------------------------------
@bp.post("/api/repro/dossier/edit")
def api_repro_dossier_edit():
    if not request.is_json:  # a cross-site text/plain POST must not reach a writer
        return jsonify({"ok": False, "error": "expected an application/json body"}), 415
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "expected a JSON object"}), 400
    key = _s(payload.get("key"))
    cid = _s(payload.get("contrast_id"))
    coder = _s(payload.get("coder"))[:80] or "coder"
    reason = _s(payload.get("reason"))
    if not _safe_key(key) or not cid:
        return jsonify({"ok": False, "error": "key and contrast_id are required"}), 400
    if _run_busy_for_edits():
        return jsonify({"ok": False, "error": "the dossier is being rebuilt; save again "
                                              "when the run has finished"}), 409
    if "verdict_override" in payload:
        v = _s(payload.get("verdict_override"))
        if v and v.lower() not in _VERDICT_CANON:
            return jsonify({"ok": False, "error": f"unknown verdict {v!r}; one of "
                                                  + ", ".join(_VERDICTS)}), 400
        payload = dict(payload, verdict_override=_VERDICT_CANON.get(v.lower(), ""))
    path = _flat_csv_path(key)
    if not os.path.isfile(path):
        return jsonify({"ok": False,
                        "error": "no per-study results CSV for this study"}), 404

    with _edit_lock:
        fields, rows = _read_csv_table(path)
        if not fields:
            return jsonify({"ok": False, "error": "results CSV is unreadable"}), 500
        for c in _EXTRA_COLS:
            if c not in fields:
                fields.append(c)
        target = next((r for r in rows if _s(r.get("contrast_id")) == cid), None)
        if target is None:
            return jsonify({"ok": False,
                            "error": "contrast_id not found in the results CSV"}), 404
        # the effective values: a dossier rebuild drops the coder columns from the
        # CSV, the audit log still holds them
        restored = False
        for col, v in (_edits_replay().get(key, {}).get(cid) or {}).items():
            if _s(target.get(col)) != v:
                target[col] = v
                restored = True

        at = datetime.now().isoformat(timespec="seconds")
        changes = []
        for src, col in _EDIT_COLS.items():
            if src not in payload:
                continue
            new = payload.get(src)
            new = "" if new is None else str(new).strip()
            old = _s(target.get(col))
            if new == old:
                continue
            target[col] = new
            changes.append({"field": col, "old": old, "new": new})
        if reason:
            old = _s(target.get("verdict_override_reason"))
            if reason != old:
                target["verdict_override_reason"] = reason
                changes.append({"field": "verdict_override_reason", "old": old,
                                "new": reason})
        if (_s(target.get("verdict_override"))
                and not _s(target.get("verdict_override_reason"))
                and any(c["field"] == "verdict_override" for c in changes)):
            return jsonify({"ok": False,
                            "error": "a verdict override needs a reason"}), 400
        if not changes:
            if restored:
                _write_csv_table(path, fields, rows)
            return jsonify({"ok": True, "changed": 0, "row": target,
                            "message": "nothing to change"})

        target["coder"] = coder
        target["coder_edit_at"] = at
        try:
            _write_csv_table(path, fields, rows)
        except OSError as e:
            log(f"repro dossier edit: write failed for {key!r}: {e!r}")
            return jsonify({"ok": False, "error": f"could not write the results CSV: {e}"}), 500

        try:
            os.makedirs(os.path.dirname(_edits_log_path()), exist_ok=True)
            with open(_edits_log_path(), "a", encoding="utf-8") as fh:
                for ch in changes:
                    fh.write(json.dumps({
                        "at": at, "study_key": key, "contrast_id": cid,
                        "field": ch["field"], "old": ch["old"], "new": ch["new"],
                        "coder": coder, "reason": reason,
                    }, ensure_ascii=False) + "\n")
        except OSError as e:  # noqa: BLE001
            log(f"repro dossier edit: audit append failed: {e!r}")
            return jsonify({"ok": True, "changed": len(changes), "changes": changes,
                            "row": target,
                            "warning": "saved to the results CSV, but the audit log "
                                       "could not be appended; the edit will be lost "
                                       "on the next dossier rebuild"})
    log(f"repro dossier edit key={key!r} cid={cid!r} "
        f"fields={[c['field'] for c in changes]}")
    return jsonify({"ok": True, "changed": len(changes), "changes": changes,
                    "row": target})


# ---------------------------------------------------------------------------
# background runners (mirrors the Analysis tab's start/poll pattern)
# ---------------------------------------------------------------------------
RUN_KINDS = ("generate", "harmonizers", "reproducers", "dossier", "study")
RUN_TIMEOUT_S = 6 * 3600          # wall-clock ceiling per step (the runners have none)
LOG_CAP = 40000                   # chars of streamed output kept for the poller
RUN_LOG_KEEP = 20                 # full app-run logs kept in temp/codingstudio_repro/runs
BACKUP_KEEP = 8                   # aggregate backups kept in temp/codingstudio_repro/backup

_repro_lock = threading.Lock()


class _LogTail:
    """Bounded, thread-safe output buffer (the old worker re-joined the whole
    output on every line: quadratic time, unbounded memory)."""

    def __init__(self, cap=LOG_CAP):
        self.cap = cap
        self._chunks = deque()
        self._n = 0
        self._lock = threading.Lock()

    def append(self, s):
        if not s:
            return
        with self._lock:
            self._chunks.append(s)
            self._n += len(s)
            while self._n - len(self._chunks[0]) >= self.cap and len(self._chunks) > 1:
                self._n -= len(self._chunks.popleft())

    def clear(self):
        with self._lock:
            self._chunks.clear()
            self._n = 0

    def text(self):
        with self._lock:
            return "".join(self._chunks)[-self.cap:]


_repro_run = {"running": False, "kind": None, "ok": None, "started": None,
              "finished": None, "error": "", "only": "", "cancelled": False,
              "steps": [], "step": None, "step_index": None, "warnings": [],
              "notes": [], "log_file": ""}
_run_log = _LogTail()
_run_proc = {"proc": None, "cancel": False}


def _run_snapshot(with_log=True):
    with _repro_lock:
        out = {k: (list(v) if isinstance(v, list) else v) for k, v in _repro_run.items()}
    if with_log:
        out["log"] = _run_log.text()
    return out


def _run_busy_for_edits():
    """A dossier rebuild rewrites the flat results CSVs the edit endpoint writes."""
    with _repro_lock:
        return bool(_repro_run["running"] and _repro_run["kind"] in ("dossier", "study"))


def _interpreter():
    if not FROZEN:
        return sys.executable
    for name in ("python", "python3", "py"):
        exe = shutil.which(name)
        if exe:
            return exe
    return None


def _generator_script(root=None):
    """The wrapper generator; the Scripts/ copy wins over a scratch copy in temp/."""
    root = root or S.ROOT
    for p in (os.path.join(root, "Scripts", "generate_repro_wrappers.py"),
              os.path.join(root, "generate_repro_wrappers.py"),
              os.path.join(root, "temp", "generate_repro_wrappers.py")):
        if os.path.isfile(p):
            return p
    return None


_RUNNERS = {
    "harmonizers": ("Scripts", "analysis", "run_harmonizers.py"),
    "reproducers": ("Scripts", "analysis", "run_reproducers.py"),
    "dossier": ("Scripts", "build_repro_dossier.py"),
    "validate": ("Scripts", "validate_repro_dossier.py"),
}


def _runner(name, root=None):
    p = os.path.join(root or S.ROOT, *_RUNNERS[name])
    return p if os.path.isfile(p) else None


def _only_matcher(runner, only):
    """The runner's own ``--only`` rule: run_harmonizers matches one substring,
    run_reproducers / build_repro_dossier split on commas (case-insensitive)."""
    if runner == "harmonizers":
        o = only.lower()
        return lambda k: o in str(k).lower()
    parts = [p.strip().lower() for p in only.split(",") if p.strip()]
    return lambda k: any(p in str(k).lower() for p in parts)


#: aggregates a filtered run truncates: runner -> [(path parts, {sheet: key column})]
_AGGREGATES = {
    "harmonizers": [(("Results", HARM_XLSX), {"Mapping": "study_key",
                                              "Summary": "study_key"})],
    "reproducers": [(("Results", REPRO_XLSX), {"Reproduction": "key", "Domains": "key"})],
    "dossier": [(("Results", "repro_dossier_index.csv"), {"": "study_key"})],
}


def _run_plan(kind, only, root):
    """[(label, runner, argv-without-interpreter, soft)] or (None, error)."""
    def step(label, runner, extra=(), soft=False):
        script = _runner(runner, root)
        if not script:
            raise LookupError(f"missing runner: {'/'.join(_RUNNERS[runner])}")
        return (label, runner, [script] + list(extra), soft)

    # ``--only=VALUE``: a value that starts with "-" must not be read as a flag
    flt = [f"--only={only}"] if only else []
    try:
        if kind == "generate":
            gen = _generator_script(root)
            if not gen:
                return None, ("the reproduction-script generator is not available "
                              "(expected Scripts/generate_repro_wrappers.py)")
            return [("generate wrappers", "generate", [gen, "--root", root], False)], None
        if kind == "harmonizers":
            return [step("run harmonizers", "harmonizers", flt)], None
        if kind == "reproducers":
            return [step("run reproducers", "reproducers", flt)], None
        if kind == "dossier":
            return [step("build dossier", "dossier", flt),
                    step("validate dossier", "validate", soft=True)], None
        if kind == "study":
            return [step("run reproducer", "reproducers", flt),
                    step("build dossier", "dossier", flt),
                    step("validate dossier", "validate", soft=True)], None
    except LookupError as e:
        return None, str(e)
    return None, "unknown run kind"


def _child_env():
    env = dict(os.environ)
    if FROZEN:
        # the packaged app's own interpreter settings must not leak into a
        # system Python
        for k in list(env):
            if k in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP") or k.startswith("_PYI"):
                env.pop(k, None)
        env.pop("_MEIPASS2", None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPYCACHEPREFIX"] = os.path.join(S.TEMP, "pycache")
    return env


def _kill_tree(proc):
    """Terminate ``proc`` and every child it spawned (run_reproducers starts one
    Python per study; killing only the parent would orphan the running study)."""
    if proc is None or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        else:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
    except Exception as e:  # noqa: BLE001
        log(f"repro run: tree kill failed ({e!r}); killing the parent only")
    try:
        if proc.poll() is None:
            proc.kill()
    except OSError:
        pass


@atexit.register
def _kill_on_exit():
    _kill_tree(_run_proc.get("proc"))


# ------------------------------------------------------------------ aggregates
def _backup_dir():
    return os.path.join(S.TEMP, "codingstudio_repro", "backup")


def _prune(folder, keep):
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return
    for n in names[:-keep] if len(names) > keep else []:
        p = os.path.join(folder, n)
        try:
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
        except OSError:
            pass


def _backup_aggregates(runner, root, stamp):
    """Copy the aggregates this runner rewrites; [(path, backup, sig_before, sheets)]."""
    out = []
    for parts, sheets in _AGGREGATES.get(runner, []):
        path = os.path.join(root, *parts)
        sig = _sig(path)
        if sig is None:
            out.append((path, "", None, sheets))
            continue
        dst_dir = os.path.join(_backup_dir(), stamp)
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, os.path.basename(path))
        shutil.copy2(path, dst)
        out.append((path, dst, sig, sheets))
    _prune(_backup_dir(), BACKUP_KEEP)
    return out


def _readable(path):
    try:
        if path.lower().endswith(".xlsx"):
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True)
            wb.close()
        else:
            with open(path, encoding="utf-8-sig", newline="") as fh:
                next(csv.reader(fh))
        return True
    except Exception:  # noqa: BLE001
        return False


def _merge_rows(header, new_rows, old_header, old_rows, keycol, matches):
    """New rows + the old rows of every study the filter did not touch, aligned on
    the new header and ordered by key (stable, so per-study row order survives)."""
    if keycol not in header or keycol not in old_header:
        return new_rows, 0
    pos = {h: i for i, h in enumerate(old_header)}
    oki = pos[keycol]
    kept = []
    for r in old_rows:
        k = r[oki] if oki < len(r) else None
        if k is None or str(k).strip() == "" or matches(str(k)):
            continue
        kept.append([r[pos[h]] if h in pos and pos[h] < len(r) else None for h in header])
    ki = header.index(keycol)
    rows = list(new_rows) + kept
    rows.sort(key=lambda r: str(r[ki] if ki < len(r) and r[ki] is not None else "").lower())
    return rows, len(kept)


def _merge_xlsx(path, backup, sheets, matches):
    import openpyxl
    new = openpyxl.load_workbook(path, read_only=True, data_only=True)
    old = openpyxl.load_workbook(backup, read_only=True, data_only=True)
    try:
        out = openpyxl.Workbook(write_only=True)
        n_kept = 0
        for name in new.sheetnames:
            rows = [list(r) for r in new[name].iter_rows(values_only=True)]
            header = [("" if c is None else str(c)) for c in (rows[0] if rows else [])]
            body = rows[1:]
            keycol = sheets.get(name)
            if keycol and name in old.sheetnames:
                orows = [list(r) for r in old[name].iter_rows(values_only=True)]
                if orows:
                    oh = [("" if c is None else str(c)) for c in orows[0]]
                    body, k = _merge_rows(header, body, oh, orows[1:], keycol, matches)
                    n_kept += k
            ws = out.create_sheet(name)
            ws.append(header)
            for r in body:
                ws.append(r)
    finally:
        new.close()
        old.close()
    tmp = path + ".codingstudio_merge.tmp"
    out.save(tmp)
    atomic_replace(tmp, path)
    return n_kept


def _merge_csv(path, backup, sheets, matches):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    with open(backup, encoding="utf-8-sig", newline="") as fh:
        orows = list(csv.reader(fh))
    if not rows or not orows:
        return 0
    body, n_kept = _merge_rows(rows[0], rows[1:], orows[0], orows[1:],
                               sheets.get(""), matches)
    tmp = path + ".codingstudio_merge.tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(rows[0])
        for r in body:
            w.writerow(["" if v is None else v for v in r])
    atomic_replace(tmp, path)
    return n_kept


def _settle_aggregates(backups, runner, only, emit):
    """After a step: restore an aggregate the runner left unreadable, and merge the
    untouched studies back into one a filtered run truncated."""
    notes = []
    for path, backup, sig_before, sheets in backups:
        name = os.path.basename(path)
        if not backup or _sig(path) == sig_before:
            continue
        if not os.path.isfile(path) or not _readable(path):
            try:
                shutil.copy2(backup, path)
                notes.append(f"{name}: unreadable after the run -> restored the backup")
            except OSError as e:
                notes.append(f"{name}: unreadable and the restore failed ({e}); "
                             f"backup at {backup}")
            continue
        if not only:
            continue
        try:
            fn = _merge_xlsx if path.lower().endswith(".xlsx") else _merge_csv
            n = fn(path, backup, sheets, _only_matcher(runner, only))
            notes.append(f"{name}: merged back {n} row(s) of the studies outside "
                         f"the filter")
        except Exception as e:  # noqa: BLE001
            try:
                shutil.copy2(backup, path)
                notes.append(f"{name}: merge failed ({e!r}) -> restored the full "
                             "pre-run backup (the filtered results are in the log)")
            except OSError as e2:
                notes.append(f"{name}: merge failed ({e!r}) and the restore failed "
                             f"({e2}); backup at {backup}")
    for n in notes:
        emit(f"NOTE: {n}\n")
    return notes


# ------------------------------------------------------------------ worker
def _run_step(cmd, cwd, env, emit, timeout_s):
    """Run one step; returns (rc, timed_out).  Output streams into ``emit``."""
    popen_kw = {"stdin": subprocess.DEVNULL}
    if os.name == "nt":
        # A windowed (console-less) parent must not pop a cmd window for the child.
        popen_kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    else:
        popen_kw["start_new_session"] = True  # own process group -> killpg on cancel
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", bufsize=1, **popen_kw)
    with _repro_lock:
        _run_proc["proc"] = proc
        cancel_now = _run_proc["cancel"]
    if cancel_now:  # cancelled between the steps' start and here
        _kill_tree(proc)
    timed_out = {"v": False}

    def _expire():
        timed_out["v"] = True
        _kill_tree(proc)

    timer = threading.Timer(timeout_s, _expire)
    timer.daemon = True
    timer.start()
    try:
        while True:
            # bounded readline: a child printing one huge line cannot exhaust memory
            chunk = proc.stdout.readline(8192)
            if not chunk:
                break
            emit(chunk)
    finally:
        timer.cancel()
        try:
            proc.stdout.close()
        except OSError:
            pass
    rc = proc.wait()
    with _repro_lock:
        _run_proc["proc"] = None
    return rc, timed_out["v"]


def _repro_worker(kind, only, root):
    state = _repro_run
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    runs_dir = os.path.join(S.TEMP, "codingstudio_repro", "runs")
    log_fh = None
    try:
        os.makedirs(runs_dir, exist_ok=True)
        log_file = os.path.join(runs_dir, f"{stamp} {kind}.log")
        log_fh = open(log_file, "w", encoding="utf-8", newline="\n")
        with _repro_lock:
            state["log_file"] = log_file
    except OSError as e:
        log(f"repro run: could not open the run log: {e!r}")

    def emit(text):
        _run_log.append(text)
        if log_fh is not None:
            try:
                log_fh.write(text)
                log_fh.flush()
            except (OSError, ValueError):
                pass

    def done(ok, error=""):
        with _repro_lock:
            state.update(ok=ok, error=error, running=False, step=None,
                         finished=datetime.now().isoformat(timespec="seconds"))

    try:
        exe = _interpreter()
        if exe is None:
            emit("! no Python interpreter found (packaged app); run the runner scripts "
                 "from a Python environment\n")
            done(False, "no Python interpreter found")
            return
        plan, err = _run_plan(kind, only, root)
        if err:
            emit("! " + err + "\n")
            done(False, err)
            return
        with _repro_lock:
            state["steps"] = [p[0] for p in plan]
        env = _child_env()
        for i, (label, runner, args, soft) in enumerate(plan):
            with _repro_lock:
                if _run_proc["cancel"]:
                    break
                state.update(step=label, step_index=i)
            cmd = [exe, "-X", "utf8"] + args
            shown = " ".join([os.path.basename(cmd[0]), "-X", "utf8",
                              os.path.basename(args[0])]
                             + [f'"{a}"' if " " in a else a for a in args[1:]])
            emit(f"\n=== [{i + 1}/{len(plan)}] {label} ===\n$ {shown}\n")
            backups = []
            try:
                backups = _backup_aggregates(runner, root, f"{stamp}-{i}")
            except OSError as e:
                emit(f"! could not back up the aggregate ({e}); a filtered run is "
                     "not safe without it - step skipped\n")
                if only:
                    done(False, f"backup failed: {e}")
                    return
            t0 = time.time()
            try:
                rc, timed_out = _run_step(cmd, root, env, emit, RUN_TIMEOUT_S)
            except OSError as e:
                emit(f"! could not start: {e}\n")
                done(False, str(e))
                return
            emit(f"[exit {rc} after {time.time() - t0:.1f}s]\n")
            log(f"repro run kind={kind} step={label!r} only={only!r} rc={rc}")
            notes = _settle_aggregates(backups, runner, only, emit)
            with _repro_lock:
                state["notes"].extend(notes)
                cancelled = _run_proc["cancel"]
            if cancelled:
                with _repro_lock:
                    state["cancelled"] = True
                emit("! cancelled by the operator\n")
                done(False, f"cancelled during '{label}'")
                return
            if timed_out:
                done(False, f"'{label}' exceeded {RUN_TIMEOUT_S}s and was killed")
                return
            if rc != 0:
                if soft:
                    with _repro_lock:
                        state["warnings"].append(f"{label} exited {rc}")
                    continue
                done(False, f"'{label}' exited {rc}")
                return
        with _repro_lock:
            cancelled = _run_proc["cancel"]
            if cancelled:
                state["cancelled"] = True
        done(not cancelled, "cancelled" if cancelled else "")
    except Exception as e:  # noqa: BLE001  (never leave the tab stuck on "running")
        log(f"repro run crashed: {e!r}")
        emit(f"! internal error: {e!r}\n")
        done(False, f"internal error: {e}")
    finally:
        if log_fh is not None:
            try:
                log_fh.close()
            except OSError:
                pass
        _prune(runs_dir, RUN_LOG_KEEP)


def start_repro_run(kind, only=""):
    """Start a run in a daemon thread.  Returns "running" when one is already in
    progress, else "started"."""
    with _repro_lock:
        if _repro_run["running"]:
            return "running"
        _repro_run.update(running=True, kind=kind, ok=None, error="", only=only or "",
                          cancelled=False, steps=[], step=None, step_index=None,
                          warnings=[], notes=[], log_file="",
                          started=datetime.now().isoformat(timespec="seconds"),
                          finished=None)
        _run_proc.update(proc=None, cancel=False)
        _run_log.clear()
    threading.Thread(target=_repro_worker, args=(kind, only or "", S.ROOT),
                     daemon=True, name=f"repro-run-{kind}").start()
    return "started"


_ONLY_OK = re.compile(r"^[^\x00-\x1f]{1,240}$")


@bp.post("/api/repro/run")
def api_repro_run():
    if not request.is_json:  # a cross-site text/plain POST must not start a run
        return jsonify({"ok": False, "error": "expected an application/json body"}), 415
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "expected a JSON object"}), 400
    kind = str(payload.get("kind") or "harmonizers").strip()
    only = str(payload.get("only") or "").strip()
    if kind not in RUN_KINDS:
        return jsonify({"ok": False, "error": "unknown run kind"}), 400
    if kind == "generate":
        only = ""
    if only and not _ONLY_OK.match(only):
        return jsonify({"ok": False, "error": "invalid study filter"}), 400
    if kind == "study" and not (_safe_key(only) and _known_key(only)):
        return jsonify({"ok": False, "error": "kind 'study' needs a known study key"}), 400
    status = start_repro_run(kind, only)
    if status == "running":
        return jsonify({"ok": False, "error": "a run is already in progress"}), 409
    return jsonify({"ok": True, "running": True, "kind": kind, "only": only})


@bp.post("/api/repro/run/cancel")
def api_repro_run_cancel():
    if not request.is_json:
        return jsonify({"ok": False, "error": "expected an application/json body"}), 415
    with _repro_lock:
        if not _repro_run["running"]:
            return jsonify({"ok": False, "error": "no run in progress"}), 409
        _run_proc["cancel"] = True
        proc = _run_proc["proc"]
    _kill_tree(proc)
    log("repro run: cancel requested")
    return jsonify({"ok": True, "cancelling": True})


@bp.get("/api/repro/run/status")
def api_repro_run_status():
    return jsonify(_run_snapshot(with_log=True))
