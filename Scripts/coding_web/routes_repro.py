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
                                  provenance, read_by)
GET  /api/repro/dossier/log/<key>    the run log tail + its resolved path (taken
                                  from the dossier's run.log_path, never re-derived)
POST /api/repro/dossier/edit      narrow coder write: source_location/source_quote,
                                  verdict override + reason, gap note -> the per-study
                                  results CSV + Results/repro_dossier_edits.jsonl
POST /api/repro/run               {kind: generate|harmonizers|reproducers, only?}
GET  /api/repro/run/status        streamed log + ok/finished flags
"""

from datetime import datetime
from flask import Blueprint, abort, jsonify, request
import csv
import glob
import json
import os
import shutil
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
RUN_KINDS = ("generate", "harmonizers", "reproducers")


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


def _safe_key(key):
    """Reject path-traversal / separator keys before any filesystem join."""
    if not key or key in (".", ".."):
        return False
    if key != os.path.basename(key) or os.path.isabs(key):
        return False
    if ".." in key:
        return False
    return True


def _resolve_rel(rel):
    """A dossier-recorded relative path -> absolute ("" when blank)."""
    rel = _s(rel)
    if not rel:
        return ""
    if os.path.isabs(rel):
        return rel
    return os.path.join(S.ROOT, rel.replace("/", os.sep))


_INDEX_CACHE = {"mtime": None, "rows": {}}


def _dossier_index():
    """study_key -> index row, mtime-cached (one CSV read per change)."""
    p = _index_path()
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        return {}
    if _INDEX_CACHE["mtime"] == mtime:
        return _INDEX_CACHE["rows"]
    rows = {}
    for r in _read_csv_rows(p):
        k = _s(r.get("study_key"))
        if k:
            rows[k] = r
    _INDEX_CACHE.update(mtime=mtime, rows=rows)
    return rows


_DOSSIER_CACHE = {}


def _load_dossier_cached(key):
    """Dossier JSON, mtime-cached; None when absent/unreadable."""
    p = _dossier_path(key)
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        return None
    cached = _DOSSIER_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001
        data = None
    _DOSSIER_CACHE[key] = (mtime, data)
    return data


_VAL_CACHE = {"mtime": None, "data": None}


def _validation_summary():
    """validate_repro_dossier.csv -> counts + per-study rows (mtime-cached)."""
    p = _validation_path()
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        mtime = None
    if _VAL_CACHE["mtime"] == mtime and _VAL_CACHE["data"] is not None:
        return _VAL_CACHE["data"]
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
            "by_key": by_key}
    _VAL_CACHE.update(mtime=mtime, data=data)
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
    """Filename-contract role (mirrors repro_dossier_lib._role_of)."""
    low = fn.lower()
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
            prov = "SOURCE.md" if os.path.isfile(os.path.join(folder, "SOURCE.md")) else ""
            out.append({"file": rel, "role": _role_of(fn, key), "size_bytes": size,
                        "provenance": prov, "read_by": "; ".join(read_by)})
    return out


def _file_counts(rows):
    """(n_raw, n_scripts, n_products) from a file inventory."""
    raw = scripts = products = 0
    for r in rows or []:
        fn = str(r.get("file") or "").lower()
        role = str(r.get("role") or "").lower()
        if fn.endswith(".py"):
            scripts += 1
        elif role == "raw data":
            raw += 1
        elif role.endswith("(product)") or role.endswith("sidecar") or role.endswith("(d10)"):
            products += 1
    return raw, scripts, products


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
    with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    atomic_replace(tmp, path)


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


def _merge_coder_edits(results, flat):
    """Overlay coder-owned fields from the flat twin onto dossier result rows."""
    for r in results:
        c = flat.get(_s(r.get("contrast_id")))
        if not c:
            continue
        for f in ("source_location", "source_quote") + _CODER_FIELDS:
            v = _s(c.get(f))
            if v:
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


def _is_fully_within(key):
    """Fully-within (single-arm, no-control) paper?  Mirrors studio_lib.summarise,
    cached by the extraction JSON's mtime."""
    path = os.path.join(S.EXTRACTION, key + ".json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return False
    cached = _FW_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    val = False
    try:
        from studio_lib import is_fully_within, load_extraction
        val = bool(is_fully_within(load_extraction(key)))
    except Exception:  # noqa: BLE001  (best-effort; missing/unreadable coding)
        val = False
    _FW_CACHE[key] = (mtime, val)
    return val


def _study_keys():
    """Union of the Datasets/ folders that are part of the contract and every key
    named in the two workbooks (so a study whose data folder was removed still
    shows its reproduction verdict)."""
    keys = set()
    ds = _datasets_dir()
    if os.path.isdir(ds):
        for name in os.listdir(ds):
            folder = os.path.join(ds, name)
            if (not os.path.isdir(folder) or name.startswith(".")
                    or name in ("temp", "_meta_analyses")):
                continue
            if (os.path.isfile(os.path.join(folder, "harmonizer.py"))
                    or os.path.isfile(os.path.join(folder, name + " - reproduction.py"))
                    or os.path.isfile(os.path.join(folder, name + " - harmonized.csv"))
                    or os.path.isfile(os.path.join(folder,
                                                    name + " - harmonized_variables.csv"))):
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


def _study_flags(key, ds):
    folder = os.path.join(ds, key)
    return {
        "has_harmonizer": os.path.isfile(os.path.join(folder, "harmonizer.py")),
        "has_harmonized": os.path.isfile(os.path.join(folder, key + " - harmonized.csv")),
        "has_sidecar": os.path.isfile(
            os.path.join(folder, key + " - harmonized_variables.csv")),
        "has_reproducer": os.path.isfile(
            os.path.join(folder, key + " - reproduction.py")),
        "has_report": os.path.isfile(os.path.join(S.ROOT, "Reproduction", key + ".md")),
        "has_extraction": os.path.isfile(os.path.join(S.EXTRACTION, key + ".json")),
    }


def _repro_overview_row(key, harm_by_key, repro_by_key, ds):
    flags = _study_flags(key, ds)
    h = harm_by_key.get(key, {})
    r = repro_by_key.get(key, {})

    raw_status = _s(r.get("status"))
    status = _STATUS_NORM.get(raw_status, raw_status or "not_run")
    if not flags["has_reproducer"] and not raw_status:
        status = "not_run"

    # dossier (Results/repro_dossier/) -- the reproduction source of truth
    idx = _dossier_index().get(key, {})
    dossier = _load_dossier_cached(key)
    if not raw_status and dossier:
        ds_status = _s(dossier.get("status"))
        if ds_status:
            status = _STATUS_NORM.get(ds_status, ds_status)

    inv = (dossier or {}).get("file_inventory") or []
    n_files_raw, n_files_scripts, n_files_products = _file_counts(inv)
    log_rel = _s(idx.get("log_path"))
    if not log_rel and dossier:
        log_rel = _s((dossier.get("run") or {}).get("log_path"))
    log_abs = _resolve_rel(log_rel)
    log_present = bool(log_abs and os.path.isfile(log_abs))

    sidecar = os.path.join(ds, key, key + " - harmonized_variables.csv")
    harmonized = os.path.join(ds, key, key + " - harmonized.csv")

    n_reproduced = _int(r.get("n_reproduced"))
    n_not_repro = _int(r.get("n_not_reproduced"))
    n_direction = _int(r.get("n_direction"))
    return {
        "study_key": key,
        "has_harmonizer": flags["has_harmonizer"],
        "has_harmonized": flags["has_harmonized"],
        "has_sidecar": flags["has_sidecar"],
        "has_reproducer": flags["has_reproducer"],
        "has_report": flags["has_report"],
        "has_extraction": flags["has_extraction"],
        "fully_within": _is_fully_within(key),
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
        # dossier (Results/repro_dossier/) -- file/result counts + log indicator
        "dossier_present": os.path.isfile(_dossier_path(key)),
        "dossier_generated_at": _s(idx.get("generated_at")),
        "n_files_raw": n_files_raw,
        "n_files_scripts": n_files_scripts,
        "n_files_products": n_files_products,
        "n_contrasts_dossier": _int(idx.get("n_contrasts")),
        "n_reproduced_dossier": _int(idx.get("n_reproduced")),
        "n_gaps_dossier": _int(idx.get("n_gap_rows")),
        "log_path": log_rel,
        "log_present": log_present,
        "last_run": _stamp(log_abs) if log_present else None,
        "harmonized_modified": _stamp(harmonized) or _stamp(sidecar),
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

    studies = [_repro_overview_row(k, harm_by_key, repro_by_key, ds)
               for k in _study_keys()]

    def _count(pred):
        return sum(1 for s in studies if pred(s))

    generator = _generator_script()
    return jsonify({
        "root": S.ROOT,
        "datasets": ds,
        "mapping_xlsx": os.path.isfile(os.path.join(S.ROOT, "Results", HARM_XLSX)),
        "reproduction_xlsx": os.path.isfile(os.path.join(S.ROOT, "Results", REPRO_XLSX)),
        "mapping_available": bool(harm),
        "reproduction_available": bool(repro),
        "generator_available": bool(generator),
        "generator_name": os.path.basename(generator) if generator else "",
        "scripts_available": {
            "harmonizers": os.path.isfile(os.path.join(S.ROOT, "Scripts", "analysis", "run_harmonizers.py")),
            "reproducers": os.path.isfile(os.path.join(S.ROOT, "Scripts", "analysis", "run_reproducers.py")),
        },
        "counts": {
            "studies": len(studies),
            "harmonized": _count(lambda s: s["has_harmonized"]),
            "harmonizers": _count(lambda s: s["has_harmonizer"]),
            "reproducers": _count(lambda s: s["has_reproducer"]),
            "reports": _count(lambda s: s["has_report"]),
            "fully_within": _count(lambda s: s["fully_within"]),
            "reproduced": _count(lambda s: s["status"] == "reproduced"),
            "partial": _count(lambda s: s["status"] == "partial"),
            "not_reproduced": _count(lambda s: s["status"] == "not_reproduced"),
            "not_reproducible": _count(lambda s: s["status"] == "not_reproducible"),
            "reported_only": _count(lambda s: s["status"] == "reported_only"),
            "crashed": _count(lambda s: s["status"] == "crashed"),
            "not_run": _count(lambda s: s["status"] == "not_run"),
            "dossiers": _count(lambda s: s["dossier_present"]),
            "logs": _count(lambda s: s["log_present"]),
        },
        "dossier": {
            "index_present": os.path.isfile(_index_path()),
            "n_index_studies": len(_dossier_index()),
            "validation": {k: v for k, v in _validation_summary().items() if k != "by_key"},
        },
        "run": {k: v for k, v in _repro_run.items() if k != "log"},
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
    ds = _datasets_dir()
    known = key in _study_keys()
    folder = os.path.join(ds, key)
    if not known and not os.path.isdir(folder):
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

    report_path = os.path.join(S.ROOT, "Reproduction", key + ".md")
    report_md = ""
    if os.path.isfile(report_path):
        try:
            with open(report_path, encoding="utf-8", errors="replace") as fh:
                report_md = fh.read()
        except OSError:
            report_md = ""

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
        "run": {k: v for k, v in _repro_run.items() if k != "log"},
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
    flat = _flat_results(key)
    if flat:
        _merge_coder_edits(out["results"], flat)
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
    return jsonify(out)


# ---------------------------------------------------------------------------
# GET /api/repro/dossier/files/<key>  (file inventory)
# ---------------------------------------------------------------------------
@bp.get("/api/repro/dossier/files/<path:key>")
def api_repro_dossier_files(key):
    if not _safe_key(key):
        abort(404)
    dossier = _load_dossier_cached(key)
    if dossier and dossier.get("file_inventory"):
        files = dossier["file_inventory"]
        source = "dossier"
    else:
        files = _scan_inventory(key)
        source = "filesystem scan (no dossier)"
    return jsonify({"key": key, "files": files, "count": len(files), "source": source})


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
    exists = bool(p and os.path.isfile(p))
    tail, size = "", 0
    if exists:
        try:
            size = os.path.getsize(p)
            with open(p, encoding="utf-8", errors="replace") as fh:
                tail = fh.read()[-20000:]
        except OSError as e:  # noqa: BLE001
            tail = f"[could not read log: {e}]"
    return jsonify({
        "key": key,
        "log_path": rel,
        "resolved": p if exists else "",
        "exists": exists,
        "bytes": size,
        "tail": tail,
        "source": source,
        "reason": "" if exists else "no run log recorded for this study",
    })


# ---------------------------------------------------------------------------
# POST /api/repro/dossier/edit  (narrow coder-owned write)
# ---------------------------------------------------------------------------
@bp.post("/api/repro/dossier/edit")
def api_repro_dossier_edit():
    payload = request.get_json(force=True, silent=True) or {}
    key = _s(payload.get("key"))
    cid = _s(payload.get("contrast_id"))
    coder = _s(payload.get("coder")) or "coder"
    reason = _s(payload.get("reason"))
    if not _safe_key(key) or not cid:
        return jsonify({"ok": False, "error": "key and contrast_id are required"}), 400
    path = _flat_csv_path(key)
    if not os.path.isfile(path):
        return jsonify({"ok": False,
                        "error": "no per-study results CSV for this study"}), 404
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
            changes.append({"field": "verdict_override_reason", "old": old, "new": reason})
    if not changes:
        return jsonify({"ok": True, "changed": 0, "row": target,
                        "message": "nothing to change"})

    target["coder"] = coder
    target["coder_edit_at"] = at
    _write_csv_table(path, fields, rows)

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
    log(f"repro dossier edit key={key!r} cid={cid!r} "
        f"fields={[c['field'] for c in changes]}")
    return jsonify({"ok": True, "changed": len(changes), "changes": changes,
                    "row": target})


# ---------------------------------------------------------------------------
# background runners (mirrors the Analysis tab's start/poll pattern)
# ---------------------------------------------------------------------------
_repro_lock = threading.Lock()

_repro_run = {"running": False, "kind": None, "log": "", "ok": None,
              "started": None, "finished": None, "error": "", "only": "",
              "cached": False}


def _interpreter():
    if not FROZEN:
        return sys.executable
    for name in ("python", "python3", "py"):
        exe = shutil.which(name)
        if exe:
            return exe
    return None


def _generator_script():
    for p in (os.path.join(S.TEMP, "generate_repro_wrappers.py"),
              os.path.join(S.ROOT, "Scripts", "generate_repro_wrappers.py"),
              os.path.join(S.ROOT, "generate_repro_wrappers.py")):
        if os.path.isfile(p):
            return p
    return None


def _run_plan(kind, only):
    """(argv-without-interpreter, cwd, error) for a run kind."""
    if kind == "generate":
        gen = _generator_script()
        if not gen:
            return None, None, ("the reproduction-script generator is not available "
                                "(expected temp/generate_repro_wrappers.py)")
        return [gen], S.ROOT, None
    if kind == "harmonizers":
        script = os.path.join(S.ROOT, "Scripts", "analysis", "run_harmonizers.py")
    elif kind == "reproducers":
        script = os.path.join(S.ROOT, "Scripts", "analysis", "run_reproducers.py")
    else:
        return None, None, "unknown run kind"
    if not os.path.isfile(script):
        return None, None, f"missing runner: {os.path.basename(script)}"
    args = [script]
    if only and kind in ("harmonizers", "reproducers"):
        args += ["--only", only]
    return args, S.ROOT, None


def _repro_worker(kind, only):
    state = _repro_run
    lines = []

    def emit(line):
        lines.append(line)
        state["log"] = "".join(lines)[-20000:]

    def done(ok, error=""):
        state.update(ok=ok, error=error)
        state["running"] = False
        state["finished"] = datetime.now().isoformat(timespec="seconds")

    exe = _interpreter()
    if exe is None:
        emit("! no Python interpreter found (packaged app); run the runner scripts "
             "from a Python environment\n")
        done(False, "no Python interpreter found")
        return

    args, cwd, err = _run_plan(kind, only)
    if err:
        emit("! " + err + "\n")
        done(False, err)
        return

    cmd = [exe, "-X", "utf8"] + args
    shown = " ".join([os.path.basename(cmd[0])] + [os.path.basename(a) if i == 0 else a
                                                   for i, a in enumerate(cmd[1:])])
    emit(f"$ {shown}\n")
    if only and kind in ("harmonizers", "reproducers"):
        book = HARM_XLSX if kind == "harmonizers" else REPRO_XLSX
        emit(f"NOTE: a filtered run rebuilds {book} for the matching studies only; "
             "run without a filter to restore the full summary.\n")

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPYCACHEPREFIX"] = os.path.join(S.TEMP, "pycache")
    try:
        popen_kw = {"stdin": subprocess.DEVNULL}
        if os.name == "nt":
            # A windowed (console-less) parent must not pop a cmd window for the child.
            popen_kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", bufsize=1, **popen_kw)
    except OSError as e:
        emit(f"! could not start: {e}\n")
        done(False, str(e))
        return
    try:
        for line in proc.stdout:
            emit(line)
    finally:
        if proc.stdout:
            proc.stdout.close()
    rc = proc.wait()
    emit(f"[exit {rc}]\n")
    log(f"repro run kind={kind} only={only!r} rc={rc}")
    done(rc == 0, "" if rc == 0 else f"runner exited {rc}")


def start_repro_run(kind, only=""):
    """Start a runner in a daemon thread.  Returns "running" when one is already in
    progress, else "started"."""
    with _repro_lock:
        if _repro_run["running"]:
            return "running"
        _repro_run.update(running=True, kind=kind, log="", ok=None, error="",
                          only=only or "", cached=False,
                          started=datetime.now().isoformat(timespec="seconds"),
                          finished=None)
    threading.Thread(target=_repro_worker, args=(kind, only or ""), daemon=True).start()
    return "started"


@bp.post("/api/repro/run")
def api_repro_run():
    payload = request.get_json(force=True, silent=True) or {}
    kind = (payload.get("kind") or "harmonizers").strip()
    only = (payload.get("only") or "").strip()
    if kind not in RUN_KINDS:
        return jsonify({"ok": False, "error": "unknown run kind"}), 400
    status = start_repro_run(kind, only)
    if status == "running":
        return jsonify({"ok": False, "error": "a run is already in progress"}), 409
    return jsonify({"ok": True, "running": True, "kind": kind, "only": only})


@bp.get("/api/repro/run/status")
def api_repro_run_status():
    return jsonify(_repro_run)
