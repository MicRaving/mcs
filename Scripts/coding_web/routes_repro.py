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
import json
import os
import shutil
import subprocess
import sys
import threading
import time

from studio_state import FROZEN, S, log
from studio_lib import atomic_replace
try:  # ``Scripts/`` reaches sys.path through studio_state; keep a fallback anyway
    import csvio as CSVIO  # plain or gzip-compressed dataset products
except Exception:  # pragma: no cover - source layout fallback
    sys.path.insert(0, os.path.join(S.ROOT, "Scripts"))
    import csvio as CSVIO

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
    # Already-normalised spellings map to themselves, so a value that went
    # through this table once (or a dossier written with underscores) is stable.
    "not_reproduced": "not_reproduced",
    "not_reproducible": "not_reproducible",
    "not_run": "not_run",
    # repro_dossier_lib._statuses() writes "unknown" when neither machine facts
    # nor a report verdict exist, and the report parser speaks yes/no.  The tab
    # has no "unknown" filter or KPI, so such a study silently vanished from
    # every count except "all"; it has no verdict, i.e. it is "not run".
    "unknown": "not_run",
    "yes": "reproduced",
    "no": "not_reproduced",
}
RUN_KINDS = ("generate", "harmonizers", "reproducers", "dossier", "study")


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
    p = S.ensure_local(_report_path(key))
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
    if S.online and S.store is not None:
        newest, name = None, ""
        for e in S.listdir(folder, recursive=True):
            if e.get("is_dir") or e.get("mtime_ns") is None:
                continue
            if newest is None or e["mtime_ns"] > newest:
                newest = e["mtime_ns"] / 1e9
                name = e["path"]
        return newest, name
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
import re  # noqa: E402

#: SB/MM reliability-route artefacts (``<Key> - harmonized - SB.csv``,
#: ``harmonizer - SB.py``, ``<Key> - MM.json``): not raw data, ignored by the pipeline.
_ROUTE_ARTEFACT = re.compile(r" - (SB|MM)(\.[^.]+)?$", re.IGNORECASE)


def _role_of(fn, key):
    """Filename-contract role (mirrors repro_dossier_lib._role_of, plus the SB/MM
    route artefacts)."""
    low = fn.lower()
    if low.endswith(".gz"):          # a gzip-compressed product keeps its role
        low = low[:-3]
    if _ROUTE_ARTEFACT.search(fn[:-3] if fn.lower().endswith(".gz") else fn):
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
    if S.online and S.store is not None:
        entries = [e for e in S.listdir(folder, recursive=True) if not e.get("is_dir")]
        scripts = []
        for e in entries:
            if e["path"].lower().endswith(".py") and e.get("local"):
                try:
                    with open(e["path"], encoding="utf-8", errors="replace") as fh:
                        scripts.append((e["name"], fh.read()))
                except OSError:
                    pass
        prov = "SOURCE.md" if any(e["name"].lower() == "source.md" for e in entries) else ""
        out = []
        for e in entries:
            read_by = [s for s, txt in scripts if e["name"] in txt]
            out.append({"file": e["path"][len("Datasets/" + key + "/"):] if e["path"].startswith("Datasets/" + key + "/") else e["name"],
                        "role": _role_of(e["name"], key), "size_bytes": e.get("size"),
                        "provenance": prov, "read_by": "; ".join(read_by)})
        return out
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
    harmonized = CSVIO.resolve(os.path.join(_datasets_dir(), key, key + " - harmonized.csv"))
    return {
        "schema_version": 0,
        "synthesized": True,
        "generated_at": _s(idx.get("generated_at")),
        "study_key": key,
        "has_dataset": _s(idx.get("has_dataset")) == "yes" or S.has_file(harmonized),
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
    for name in (S.listdir(ds) if S.online and S.store is not None else []):
        if not name.get("is_dir") or name["name"].startswith(".") \
                or name["name"] in ("temp", "_meta_analyses"):
            continue
        files = {e["name"] for e in S.listdir(os.path.join(ds, name["name"]))
                 if not e.get("is_dir")}
        n = name["name"]
        if ("harmonizer.py" in files or n + " - reproduction.py" in files
                or n + " - harmonized.csv" in files
                or n + " - harmonized.csv.gz" in files
                or n + " - harmonized_variables.csv" in files):
            keys.add(n)
    if not (S.online and S.store is not None) and os.path.isdir(ds):
        for name in os.listdir(ds):
            folder = os.path.join(ds, name)
            if (not os.path.isdir(folder) or name.startswith(".")
                    or name in ("temp", "_meta_analyses")):
                continue
            if (os.path.isfile(os.path.join(folder, "harmonizer.py"))
                    or os.path.isfile(os.path.join(folder, name + " - reproduction.py"))
                    or os.path.isfile(os.path.join(folder, name + " - harmonized.csv"))
                    or os.path.isfile(os.path.join(folder, name + " - harmonized.csv.gz"))
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
        "has_harmonizer": S.has_file(os.path.join(folder, "harmonizer.py")),
        "has_harmonized": S.has_file(os.path.join(folder, key + " - harmonized.csv"))
        or S.has_file(os.path.join(folder, key + " - harmonized.csv.gz")),
        "has_sidecar": S.has_file(
            os.path.join(folder, key + " - harmonized_variables.csv")),
        "has_reproducer": S.has_file(
            os.path.join(folder, key + " - reproduction.py")),
        "has_report": S.has_file(os.path.join(S.ROOT, "Reproduction", key + ".md")),
        "has_extraction": os.path.isfile(os.path.join(S.EXTRACTION, key + ".json")),
    }



# ---------------------------------------------------------------------------
# per-study reproduction facts (Datasets/<Key>/<Key> - repro_facts.json)
# ---------------------------------------------------------------------------
# The workbook is the campaign's summary; these files are what it summarises.
# The field names below are the workbook's column names, so a facts dict can
# stand in for a workbook row wherever one is missing.

_FACTS_CACHE = {}


def _facts_row(key, ds):
    """One study's reproduction facts, shaped like a workbook row (or None)."""
    path = S.ensure_local(os.path.join(ds, key, key + " - repro_facts.json"))
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    hit = _FACTS_CACHE.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    data = _read_json(path)
    row = None
    if isinstance(data, dict) and _s(data.get("status")):
        row = {
            "key": key,
            "status": _s(data.get("status")),
            "success": _s(data.get("success")),
            "machine_status": _s(data.get("machine_status")),
            "documented_status": _s(data.get("documented_status")),
            "documented_verdict": _s(data.get("documented_verdict")),
            "script_kind": _s(data.get("script_kind")),
            "n_contrasts": _int(data.get("n_coded")),
            "n_reproduced": _int(data.get("n_reproduced")),
            "n_direction": _int(data.get("n_direction")),
            "n_not_reproduced": _int(data.get("n_not_reproduced")),
            "runtime_s": _num(data.get("runtime")),
            "returncode": _int(data.get("rc")),
            "machine_disagrees": _s(data.get("machine_disagrees")),
        }
    _FACTS_CACHE[key] = (mtime, row)
    return row


def _repro_overview_row(key, harm_by_key, repro_by_key, ds):
    flags = _study_flags(key, ds)
    h = harm_by_key.get(key, {})
    r = repro_by_key.get(key, {})
    # dossier (Results/repro_dossier/) -- consulted before the bare report
    idx = _dossier_index().get(key, {})
    dossier = _load_dossier_cached(key)

    # Status precedence: workbook row -> the study's repro_facts.json -> the
    # dossier -> "reported_only" when only Reproduction/<Key>.md exists.  The
    # report placeholder used to be filled in *before* the dossier was looked at,
    # so a study with a report never showed its dossier verdict.
    source = "workbook" if r else ""
    facts = None
    if not r:
        facts = _facts_row(key, ds)
        if facts:
            r, source = facts, "facts"
    raw_status = _s(r.get("status"))
    if not raw_status and dossier and _s(dossier.get("status")):
        raw_status, source = _s(dossier.get("status")), "dossier"
    if not raw_status and flags["has_report"]:
        # documented in Reproduction/<Key>.md, never machine-checked
        raw_status, source = "reported_only", "report"
    status = _STATUS_NORM.get(raw_status.lower(), raw_status.lower() or "not_run")

    inv = (dossier or {}).get("file_inventory") or []
    n_files_raw, n_files_scripts, n_files_products = _file_counts(inv)
    log_rel = _s(idx.get("log_path"))
    if not log_rel and dossier:
        log_rel = _s((dossier.get("run") or {}).get("log_path"))
    log_abs = _resolve_rel(log_rel)
    log_present = bool(log_abs and os.path.isfile(log_abs))

    sidecar = os.path.join(ds, key, key + " - harmonized_variables.csv")
    harmonized = CSVIO.resolve(os.path.join(ds, key, key + " - harmonized.csv"))

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
        "status_source": source,
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

    gen_ok, gen_name = _generator_available()
    # Only a packaged app has to look for a Python; from source it is this one.
    py_exe, py_note = _interpreter()
    return jsonify({
        "root": S.ROOT,
        "datasets": ds,
        "mapping_xlsx": os.path.isfile(os.path.join(S.ROOT, "Results", HARM_XLSX)),
        "reproduction_xlsx": os.path.isfile(os.path.join(S.ROOT, "Results", REPRO_XLSX)),
        "mapping_available": bool(harm),
        "reproduction_available": bool(repro),
        "generator_available": gen_ok,
        "generator_name": gen_name,
        "python_available": bool(py_exe),
        "python_note": py_note,
        "scripts_available": {
            "harmonizers": os.path.isfile(os.path.join(S.ROOT, "Scripts", "analysis", "run_harmonizers.py")),
            "reproducers": os.path.isfile(os.path.join(S.ROOT, "Scripts", "analysis", "run_reproducers.py")),
            "dossier": bool(_runner("dossier")),
            "validate": bool(_runner("validate")),
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
    path = S.ensure_local(os.path.join(ds, key, key + " - exclusions.csv"))
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
    # ".." would pass the isdir() test below (Datasets/.. is the project root).
    if not _safe_key(key):
        abort(400)
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
    # Same fallback as the overview: without the workbook, the study's own
    # repro_facts.json carries the verdict (else the detail said "not run" while
    # the overview row said "reproduced").
    r = repro_by_key.get(key) or _facts_row(key, ds) or {}
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
    if _run_busy_for_edits():
        # a dossier rebuild rewrites the flat results CSV this edit appends to
        return jsonify({"ok": False, "error": "the dossier is being rebuilt; save again "
                                              "when the run has finished"}), 409
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

#: Wall-clock ceiling per step (the runner scripts themselves have none).
RUN_TIMEOUT_S = 6 * 3600
#: Characters of streamed output kept in memory for the poller (the full output
#: goes to temp/codingstudio_repro/runs/<stamp> <kind>.log).
LOG_CAP = 40000
RUN_LOG_KEEP = 20       # full run logs kept in temp/codingstudio_repro/runs
BACKUP_KEEP = 8         # aggregate backups kept in temp/codingstudio_repro/backup


class _LogTail:
    """Bounded, thread-safe output buffer for the poller."""

    def __init__(self, cap=LOG_CAP):
        from collections import deque

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
            while len(self._chunks) > 1 and self._n - len(self._chunks[0]) >= self.cap:
                self._n -= len(self._chunks.popleft())

    def clear(self):
        with self._lock:
            self._chunks.clear()
            self._n = 0

    def text(self):
        with self._lock:
            return "".join(self._chunks)[-self.cap:]


_repro_run = {"running": False, "kind": None, "ok": None, "started": None,
              "finished": None, "error": "", "only": "", "cached": False,
              "cancelled": False, "steps": [], "step": None, "step_index": None,
              "warnings": [], "notes": [], "log_file": ""}
_run_log = _LogTail()
_run_proc = {"proc": None, "cancel": False}


def _run_snapshot():
    with _repro_lock:
        out = {k: (list(v) if isinstance(v, list) else v) for k, v in _repro_run.items()}
    out["log"] = _run_log.text()
    return out


def _run_busy_for_edits():
    """A dossier rebuild rewrites the flat results CSVs the edit endpoint writes."""
    with _repro_lock:
        return bool(_repro_run["running"] and _repro_run["kind"] in ("dossier", "study"))


_INTERP_CACHE = {"at": 0.0, "value": (None, "")}
_INTERP_TTL = 300  # seconds; a Python installed while the app runs is found again


def _probe_python(exe):
    """True when ``exe`` is a real Python that can import what the runners need.

    The Windows "App execution alias" ``python.exe`` in WindowsApps is a stub
    that opens the Store instead of running anything, and a bare system Python
    without pandas would crash every harmonizer; both fail this probe."""
    try:
        r = subprocess.run([exe, "-c", "import pandas, numpy"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
                           creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                                          if os.name == "nt" else 0))
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _interpreter():
    """(python executable or None, human-readable note).

    From source the app's own interpreter runs the runners.  A packaged app has
    no Python of its own (``sys.executable`` is the app), so it looks for one:
    ``CODING_STUDIO_PYTHON`` first, then ``py`` / ``python`` / ``python3`` on
    PATH, keeping the first that can import pandas.  The result is cached for a
    few minutes because the probe starts a process."""
    if not FROZEN:
        return sys.executable, ""
    now = time.time()
    if _INTERP_CACHE["value"][0] and now - _INTERP_CACHE["at"] < _INTERP_TTL:
        return _INTERP_CACHE["value"]
    if not _INTERP_CACHE["value"][0] and now - _INTERP_CACHE["at"] < 30:
        return _INTERP_CACHE["value"]  # do not re-probe on every overview poll
    candidates = []
    env_py = os.environ.get("CODING_STUDIO_PYTHON", "").strip()
    if env_py:
        candidates.append(env_py)
    for name in ("py", "python", "python3"):
        exe = shutil.which(name)
        if exe and exe not in candidates:
            candidates.append(exe)
    value = (None, ("no Python with pandas found: the packaged app cannot run the "
                    "harmonizers/reproducers itself. Install Python with pandas "
                    "(or set CODING_STUDIO_PYTHON to its python executable) and reopen "
                    "this tab."))
    for exe in candidates:
        if _probe_python(exe):
            value = (exe, f"using the system Python at {exe}")
            break
    _INTERP_CACHE.update(at=now, value=value)
    return value


def _generator_module():
    """The wrapper generator shipped with the app (``Scripts/generate_repro_wrappers.py``).

    It is imported, not executed: it needs only the standard library, so it runs
    in-process in the worker thread and works in a packaged app that has no
    Python interpreter.  ``Scripts/`` is on ``sys.path`` (studio_state puts it
    there for ``project_context``); a frozen build bundles the module because the
    spec's ``pathex`` includes ``Scripts/`` and this is a static import."""
    try:
        import generate_repro_wrappers  # noqa: PLC0415
        return generate_repro_wrappers
    except Exception as e:  # noqa: BLE001 - fall back to a script on disk
        log(f"repro: generator module not importable: {e}")
        return None


def _generator_script():
    """A generator script on disk, for an app build that predates the bundled
    module: the project's own ``Scripts/`` copy, then the legacy ``temp/`` one."""
    for p in (os.path.join(S.ROOT, "Scripts", "generate_repro_wrappers.py"),
              os.path.join(S.TEMP, "generate_repro_wrappers.py")):
        if os.path.isfile(p):
            return p
    return None


def _generator_available():
    """(available, label) for the overview's Generate button."""
    if _generator_module() is not None:
        return True, "generate_repro_wrappers.py (built in)"
    gen = _generator_script()
    if gen:
        return True, os.path.basename(gen)
    return False, ""


def _run_generator_inprocess(mod, only, emit):
    """Run the bundled generator in this (worker) thread; returns the exit code."""
    emit(f"$ generate_repro_wrappers --root {S.ROOT}"
         + (f" --only {only}" if only else "") + "\n")
    try:
        return mod.generate(S.ROOT, only=only or None, out=lambda line: emit(line + "\n"))
    except Exception as e:  # noqa: BLE001 - report, never kill the server thread
        emit(f"! generator failed: {type(e).__name__}: {e}\n")
        return 1


#: The runner scripts a step can run (relative to the project root).
_RUNNERS = {
    "harmonizers": ("Scripts", "analysis", "run_harmonizers.py"),
    "reproducers": ("Scripts", "analysis", "run_reproducers.py"),
    "dossier": ("Scripts", "build_repro_dossier.py"),
    "validate": ("Scripts", "validate_repro_dossier.py"),
}


def _runner(name):
    p = os.path.join(S.ROOT, *_RUNNERS[name])
    return p if os.path.isfile(p) else None


def _run_plan(kind, only):
    """``([(label, runner, argv-without-interpreter, soft)], error)``.

    ``soft`` steps (the dossier validator) may fail without failing the run;
    their exit code is reported as a warning.
    """
    def step(label, runner, extra=(), soft=False):
        script = _runner(runner)
        if not script:
            raise LookupError(f"missing runner: {'/'.join(_RUNNERS[runner])}")
        return (label, runner, [script] + list(extra), soft)

    # "--only=<v>" so a filter that starts with "-" is not read as an option
    flt = ["--only=" + only] if only else []
    try:
        if kind == "generate":
            gen = _generator_script()
            if not gen:
                return None, ("the reproduction-script generator is not available "
                              "(Scripts/generate_repro_wrappers.py is missing)")
            args = [gen, "--root", S.ROOT]
            # The legacy temp/ copy predates --only; passing it would be an argparse error.
            if only and os.path.dirname(gen) != S.TEMP:
                args += ["--only=" + only]
            return [("generate wrappers", "generate", args, False)], None
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
        # the packaged app's own interpreter settings must not leak into a system Python
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
            import signal  # noqa: PLC0415

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


def _kill_on_exit():
    _kill_tree(_run_proc.get("proc"))


import atexit  # noqa: E402

atexit.register(_kill_on_exit)


# ------------------------------------------------------------------ aggregates
#: The Results summaries a runner rewrites, and the column that names the study
#: in each sheet (``""`` for a CSV).  A filtered run rebuilds them for the
#: matching studies only; the worker backs them up first and merges the rows of
#: every study outside the filter back in afterwards.
_AGGREGATES = {
    "harmonizers": [(("Results", HARM_XLSX), {"Mapping": "study_key", "Summary": "study_key"})],
    "reproducers": [(("Results", REPRO_XLSX), {"Reproduction": "key", "Domains": "key"})],
    "dossier": [(("Results", "repro_dossier_index.csv"), {"": "study_key"})],
}


def _only_matcher(runner, only):
    """The runner's own ``--only`` rule: run_harmonizers matches one substring,
    run_reproducers / build_repro_dossier split on commas (case-insensitive)."""
    if runner == "harmonizers":
        o = only.lower()
        return lambda k: o in str(k).lower()
    parts = [p.strip().lower() for p in only.split(",") if p.strip()]
    return lambda k: any(p in str(k).lower() for p in parts)


def _file_sig(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


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


def _backup_aggregates(runner, stamp):
    """Copy the aggregates this runner rewrites: ``[(path, backup, sig_before, sheets)]``."""
    out = []
    for parts, sheets in _AGGREGATES.get(runner, []):
        path = os.path.join(S.ROOT, *parts)
        sig = _file_sig(path)
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
            import openpyxl  # noqa: PLC0415

            openpyxl.load_workbook(path, read_only=True).close()
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
    import openpyxl  # noqa: PLC0415

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
    tmp = os.path.join(S.TEMP, os.path.basename(path) + ".merge.tmp.xlsx")
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
    tmp = os.path.join(S.TEMP, os.path.basename(path) + ".merge.tmp")
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
        if not backup or _file_sig(path) == sig_before:
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
            notes.append(f"{name}: merged back {n} row(s) of the studies outside the filter")
        except Exception as e:  # noqa: BLE001
            try:
                shutil.copy2(backup, path)
                notes.append(f"{name}: merge failed ({e!r}) -> restored the full pre-run "
                             "backup (the filtered results are in the log)")
            except OSError as e2:
                notes.append(f"{name}: merge failed ({e!r}) and the restore failed "
                             f"({e2}); backup at {backup}")
    for n in notes:
        emit(f"NOTE: {n}\n")
    return notes


# ------------------------------------------------------------------ worker
def _run_step(cmd, cwd, env, emit, timeout_s):
    """Run one step; returns ``(rc, timed_out)``.  Output streams into ``emit``."""
    popen_kw = {"stdin": subprocess.DEVNULL}
    if os.name == "nt":
        # A windowed (console-less) parent must not pop a cmd window for the child.
        popen_kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    else:
        popen_kw["start_new_session"] = True     # own process group -> killpg on cancel
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", bufsize=1, **popen_kw)
    with _repro_lock:
        _run_proc["proc"] = proc
        cancel_now = _run_proc["cancel"]
    if cancel_now:                               # cancelled while the step was starting
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


def _repro_worker(kind, only):
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
        _repro_steps(kind, only, stamp, emit, done)
    finally:
        if log_fh is not None:
            try:
                log_fh.close()
            except OSError:
                pass
        _prune(runs_dir, RUN_LOG_KEEP)


def _repro_steps(kind, only, stamp, emit, done):
    state = _repro_run
    if kind == "generate":
        if only and S.online:
            failed = S.ensure_study_local(only)
            if failed:
                emit(f"! {len(failed)} file(s) of {only} could not be downloaded; "
                     "generation needs the study's dataset folder\n")
                done(False, "dataset not fully downloaded")
                return
        mod = _generator_module()
        if mod is not None:
            with _repro_lock:
                state.update(steps=["generate wrappers"], step="generate wrappers",
                             step_index=0)
            rc = _run_generator_inprocess(mod, only, emit)
            emit(f"[exit {rc}]\n")
            log(f"repro run kind=generate (in-process) only={only!r} rc={rc}")
            done(rc == 0, "" if rc == 0 else f"generator exited {rc}")
            return

    exe, note = _interpreter()
    if exe is None:
        emit("! " + note + "\n")
        done(False, "no Python with pandas found (packaged app)")
        return
    if note:
        emit(note + "\n")

    plan, err = _run_plan(kind, only)
    if err:
        emit("! " + err + "\n")
        done(False, err)
        return

    # An online project's mirror holds only the core slice of a dataset: a
    # harmonizer/reproducer reads every file of Datasets/<Key>/, so fetch that
    # whole folder before the subprocess starts (audit §8).
    if S.online and only and kind in ("harmonizers", "reproducers", "study"):
        emit(f"fetching the dataset of {only}...\n")
        failed = S.ensure_study_local(only)
        if failed:
            emit(f"! {len(failed)} file(s) could not be downloaded: "
                 + ", ".join(failed[:5]) + (" ..." if len(failed) > 5 else "") + "\n")
            done(False, "dataset not fully downloaded")
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
        shown = " ".join([os.path.basename(cmd[0]), "-X", "utf8", os.path.basename(args[0])]
                         + [f'"{a}"' if " " in a else a for a in args[1:]])
        emit(f"\n=== [{i + 1}/{len(plan)}] {label} ===\n$ {shown}\n")
        backups = []
        try:
            backups = _backup_aggregates(runner, f"{stamp}-{i}")
        except OSError as e:
            emit(f"! could not back up the aggregate ({e})\n")
            if only:
                # without the backup a filtered run would truncate the summary
                done(False, f"backup failed: {e}")
                return
        t0 = time.time()
        try:
            rc, timed_out = _run_step(cmd, S.ROOT, env, emit, RUN_TIMEOUT_S)
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


def start_repro_run(kind, only=""):
    """Start a run in a daemon thread.  Returns "running" when one is already in
    progress, else "started"."""
    with _repro_lock:
        if _repro_run["running"]:
            return "running"
        _repro_run.update(running=True, kind=kind, ok=None, error="", only=only or "",
                          cached=False, cancelled=False, steps=[], step=None,
                          step_index=None, warnings=[], notes=[], log_file="",
                          started=datetime.now().isoformat(timespec="seconds"),
                          finished=None)
        _run_proc.update(proc=None, cancel=False)
        _run_log.clear()
    threading.Thread(target=_repro_worker_guarded, args=(kind, only or ""), daemon=True,
                     name=f"repro-run-{kind}").start()
    return "started"


def _repro_worker_guarded(kind, only):
    """Any unexpected exception must still clear ``running``; otherwise every later
    run is refused with 409 until the app restarts."""
    try:
        _repro_worker(kind, only)
    except Exception as e:  # noqa: BLE001
        log(f"repro run kind={kind} crashed: {e}")
        _run_log.append(f"! run crashed: {e}\n")
        with _repro_lock:
            _repro_run.update(ok=False, error=f"run crashed: {e}", running=False,
                              finished=datetime.now().isoformat(timespec="seconds"))


_ONLY_OK = re.compile(r"^[^\x00-\x1f]{1,240}$")


@bp.post("/api/repro/run")
def api_repro_run():
    # JSON only: a cross-site form/text POST to the local app must not start a run
    if not request.is_json:
        return jsonify({"ok": False, "error": "expected an application/json body"}), 415
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "expected a JSON object"}), 400
    kind = str(payload.get("kind") or "harmonizers").strip()
    only = str(payload.get("only") or "").strip()
    if kind not in RUN_KINDS:
        return jsonify({"ok": False, "error": "unknown run kind"}), 400
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
    return jsonify(_run_snapshot())
