"""Harmonizer routes: the wide-table variable editor plus its save/missing/index endpoints."""

from datetime import datetime
from flask import Blueprint, abort, jsonify, request
import csv
import glob
import json
import os
import re
import tempfile
import threading
from studio_lib import DOMAIN_VOCAB, atomic_replace, safe_token
from studio_state import S

bp = Blueprint("harmonizer", __name__)


# ---------------------------------------------------------------- harmonizer tab
# Lets a coder audit every harmonized variable: its uniform label, role, domain,
# veracity, timepoint and polarity.  Variables that share a base name and differ
# only by a numeric index (e.g. ``..._i01`` .. ``..._i06``) are bundled so the
# shared parameters can be set once, while the expanded list allows per-variable
# overrides.  Overrides are written to ``Codings/<Key> - harmonizer - <coder>.json``
# and never touch the harmonized dataset or its sidecar.

_HARM_IDX = re.compile(r"(_i?\d+)+$")

_HARM_FIELDS = ("role", "dv_domain", "veracity", "timepoint", "dv_polarity")

# The master coder's override file is applied last, so a resolved value wins.
HARM_MASTER = "master"

_BAD_KEY = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

def valid_key(key):
    """True for a study key that is a single file-name component.

    Keys come from the URL (``<path:key>``) or a JSON body and are joined onto
    ``Datasets/`` and ``Codings/``; a separator or ``..`` would read or write
    outside the study folder / coder slot."""
    if not isinstance(key, str) or not key or key != key.strip() or len(key) > 240:
        return False
    if key in (".", "..") or key.startswith(".") or key.endswith("."):
        return False
    return not _BAD_KEY.search(key)

def harmonized_paths(key):
    folder = os.path.join(S.DATASETS or "", key)
    return (os.path.join(folder, key + " - harmonized_variables.csv"),
            os.path.join(folder, key + " - harmonized.csv"))

def harmonizer_bundle(name):
    base = _HARM_IDX.sub("", str(name))
    return base or str(name)

def harmonizer_coder_files(key):
    out = []
    for f in glob.glob(os.path.join(S.CODINGS, glob.escape(key) + " - harmonizer - *.json")):
        base = os.path.basename(f)[: -len(".json")]
        coder = base[len(key) + len(" - harmonizer - "):]
        try:
            stamp = datetime.fromtimestamp(os.path.getmtime(f)).isoformat(timespec="seconds")
        except OSError:
            stamp = None
        out.append({"coder": coder, "file": os.path.basename(f), "modified": stamp})
    return sorted(out, key=lambda d: d["coder"].lower())

def load_harmonizer_overrides(key, errors=None):
    """Merged overrides from every coder file, plus a per-coder map.

    Coders are merged alphabetically with the master coder last, so a value the
    master resolved always wins.  An unreadable file is reported in ``errors``
    (when a list is given) instead of disappearing silently."""
    merged, by_coder = {}, {}
    files = sorted(harmonizer_coder_files(key),
                   key=lambda d: (d["coder"].lower() == HARM_MASTER, d["coder"].lower()))
    for info in files:
        try:
            with open(os.path.join(S.CODINGS, info["file"]), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as e:
            if errors is not None:
                errors.append({"file": info["file"], "error": repr(e)})
            continue
        ov = data.get("overrides") if isinstance(data, dict) else None
        if not isinstance(ov, dict):
            if errors is not None and ov is not None:
                errors.append({"file": info["file"], "error": "overrides is not an object"})
            ov = {}
        by_coder[info["coder"]] = ov
        for uv, fields in ov.items():
            if isinstance(fields, dict):
                merged.setdefault(uv, {}).update(fields)
    return merged, by_coder

_BLANKS = {"nan", "none", "nat", "<na>"}

def _cell(row, name):
    v = (row.get(name) or "").strip()
    return "" if v.lower() in _BLANKS else v

def _norm_artifact(a):
    """Mirror of ``variable_assignments._norm_artifact``: blank/all/any -> ``*``."""
    return "*" if (a or "").strip().lower() in ("", "*", "all", "any") else a.strip()

def _raw_key_matches(rows, artifact, original):
    """Indices of the sidecar rows ``variable_assignments._match_indices`` hits.

    The pipeline applies an assignment to the FIRST match only, so a raw column
    that feeds several harmonized variables can patch a different variable than
    the one a coder selected."""
    if not original:
        return []
    a = _norm_artifact(artifact)
    return [i for i, r in enumerate(rows)
            if r["original_variable"] == original
            and (a == "*" or _norm_artifact(r["artifact"]) in (a, "*"))]

_SIDECAR_CACHE = {}

def load_harmonized_variables(key):
    vfile, wide = harmonized_paths(key)
    try:
        mt = os.path.getmtime(vfile)
    except OSError:
        return None
    hit = _SIDECAR_CACHE.get(vfile)
    if hit is not None and hit[0] == mt:
        rows = hit[1]
    else:
        rows = []
        with open(vfile, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                uv = _cell(row, "uniform_variable")
                if not uv:
                    continue
                m = _HARM_IDX.search(uv)
                ver = _cell(row, "veracity")
                rows.append({
                    "uniform_variable": uv,
                    "role": _cell(row, "role"),
                    "dv_domain": _cell(row, "dv_domain"),
                    # pandas writes a bool veracity column as True/False
                    "veracity": ver.lower() if ver.lower() in ("true", "false") else ver,
                    "timepoint": _cell(row, "timepoint"),
                    "dv_polarity": _cell(row, "dv_polarity"),
                    "original_variable": _cell(row, "original_variable"),
                    "artifact": _cell(row, "artifact"),
                    "item_scope": _cell(row, "item_scope"),
                    "notes": _cell(row, "notes"),
                    "bundle": harmonizer_bundle(uv),
                    "index": (m.group(0).lstrip("_") if m else ""),
                })
        by_orig = {}
        for i, r in enumerate(rows):
            if r["original_variable"]:
                by_orig.setdefault(r["original_variable"], []).append(i)
        for i, r in enumerate(rows):
            same = [rows[j] for j in by_orig.get(r["original_variable"], [])]
            hits = _raw_key_matches(same, r["artifact"], r["original_variable"])
            r["raw_key_shared"] = len(hits)
            r["raw_key_target"] = same[hits[0]]["uniform_variable"] if hits else ""
        _SIDECAR_CACHE[vfile] = (mt, rows)
    return {"variables": [dict(r) for r in rows], "has_wide": os.path.isfile(wide)}

def _harmonizer_known(key):
    if not valid_key(key):
        return False
    return key in S.KEYS or os.path.isfile(harmonized_paths(key)[0])

def _harmonizer_context(key):
    """Study context for the harmonizer: the coded experiment metadata and its DV
    rows (plus the coding notes), so a coder can align the harmonized variables
    with the extraction.  Read-only; never written back."""
    from studio_lib import load_extraction
    out = []
    for rec in (load_extraction(key) or []):
        if not isinstance(rec, dict):
            continue
        dvs = []
        for dv in (rec.get("DV_Metrics") or []):
            if not isinstance(dv, dict):
                continue

            def _join(v):
                if isinstance(v, (list, tuple)):
                    return "; ".join(str(x) for x in v if x not in (None, ""))
                return str(v or "")

            dvs.append({
                "dv_name": str(dv.get("dv_name") or "")[:140],
                "dv_domain": _join(dv.get("dv_domain")),
                "score_types": _join(dv.get("score_types")),
                "timepoint": str(dv.get("timepoint_1") or ""),
                "polarity": str(dv.get("dv_polarity") or ""),
                "eligible": dv.get("eligible_primary"),
                "veracity": str(dv.get("veracity") or ""),
            })
        out.append({
            "study_id": str(rec.get("Study_ID") or ""),
            "title": str(rec.get("Title") or "")[:300],
            "description": str(rec.get("Study_Description") or "")[:800],
            "authors": str(rec.get("Authors") or ""),
            "year": rec.get("Year") or "",
            "n_dv": len(dvs),
            "dvs": dvs[:80],
        })
    notes = ""
    notes_path = os.path.join(S.EXTRACTION, key + ".md")
    if os.path.isfile(notes_path):
        try:
            with open(notes_path, encoding="utf-8", errors="replace") as fh:
                notes = fh.read()[:4000]
        except OSError:
            notes = ""
    return {"records": out, "notes": notes}

@bp.get("/api/harmonizer/<path:key>")
def api_harmonizer(key):
    if not _harmonizer_known(key):
        abort(404)
    data = load_harmonized_variables(key)
    coder_errors = []
    overrides, by_coder = load_harmonizer_overrides(key, coder_errors)
    context = _harmonizer_context(key)
    if data is None:
        return jsonify({"key": key, "exists": False, "variables": [], "bundles": [],
                        "coders": harmonizer_coder_files(key), "overrides": {},
                        "by_coder": {}, "coder_errors": coder_errors, "context": context,
                        "options": _harmonizer_options()})
    bundles = {}
    for v in data["variables"]:
        b = bundles.setdefault(v["bundle"], {"bundle": v["bundle"], "role": v["role"],
                                             "variables": []})
        b["variables"].append(v["uniform_variable"])
    bundle_list = sorted(bundles.values(), key=lambda b: (b["role"], b["bundle"].lower()))
    return jsonify({"key": key, "exists": True, "has_wide": data["has_wide"],
                    "variables": data["variables"], "bundles": bundle_list,
                    "coders": harmonizer_coder_files(key), "overrides": overrides,
                    "by_coder": by_coder, "coder_errors": coder_errors, "context": context,
                    "options": _harmonizer_options()})

_HARM_MISSING_LOCK = threading.Lock()

_HARM_MISSING_CACHE = {"mtime": None, "data": None}

# Fields a DV must carry.  ``veracity`` and ``timepoint`` are deliberately NOT
# required: veracity only exists for true/false stimuli (it is decoded from the
# DV name and is blank otherwise), and a blank timepoint is the study's primary
# posttest.  Requiring them flagged ~7.5k of 9.4k DVs as "missing".
_HARM_REQUIRED_DV_FIELDS = ("dv_domain", "dv_polarity")

_SIDECAR_LIST = {"root": None, "at": 0.0, "files": []}
_SIDECAR_LIST_TTL = 5.0

def sidecar_files():
    """[(key, path, mtime)] for every ``Datasets/<Key>/<Key> - harmonized_variables.csv``.

    One ``scandir`` of ``Datasets/`` plus one ``stat`` per study folder (a glob
    lists every folder's contents), reused for a few seconds."""
    import time
    ds = S.DATASETS or ""
    now = time.monotonic()
    with _HARM_MISSING_LOCK:
        if _SIDECAR_LIST["root"] == ds and now - _SIDECAR_LIST["at"] < _SIDECAR_LIST_TTL:
            return list(_SIDECAR_LIST["files"])
    out = []
    try:
        entries = sorted(os.scandir(ds), key=lambda e: e.name) if os.path.isdir(ds) else []
    except OSError:
        entries = []
    for e in entries:
        try:
            if not e.is_dir():
                continue
            p = os.path.join(e.path, e.name + " - harmonized_variables.csv")
            out.append((e.name, p, os.stat(p).st_mtime))
        except OSError:
            continue
    with _HARM_MISSING_LOCK:
        _SIDECAR_LIST.update(root=ds, at=now, files=out)
    return list(out)

def _harmonizer_missing_map():
    """{key: {"missing": n, "dv": n_dv, "dv_missing": n_dv_missing}}.

    A variable is "missing" when a required harmonized field is empty: ``role``
    for every variable, and ``dv_domain`` + ``dv_polarity`` for a DV.  Covariates
    and other roles legitimately omit the DV fields; veracity and timepoint are
    optional for a DV (see ``_HARM_REQUIRED_DV_FIELDS``)."""
    listing = sidecar_files()
    stamp = (S.DATASETS, tuple((k, mt) for k, _p, mt in listing))
    with _HARM_MISSING_LOCK:
        if _HARM_MISSING_CACHE["data"] is not None and _HARM_MISSING_CACHE["mtime"] == stamp:
            return _HARM_MISSING_CACHE["data"]
    out = {}
    for key, f, _mt in listing:
        n_missing = n_dv = n_dv_missing = 0
        try:
            with open(f, encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    if not (row.get("uniform_variable") or "").strip():
                        continue
                    role = (row.get("role") or "").strip()
                    dv_ok = all((row.get(k) or "").strip() for k in _HARM_REQUIRED_DV_FIELDS)
                    if not role:
                        n_missing += 1
                    elif role == "dv" and not dv_ok:
                        n_missing += 1
                    if role == "dv":
                        n_dv += 1
                        if not dv_ok:
                            n_dv_missing += 1
        except Exception:
            continue
        out[key] = {"missing": n_missing, "dv": n_dv, "dv_missing": n_dv_missing}
    with _HARM_MISSING_LOCK:
        _HARM_MISSING_CACHE.update(mtime=stamp, data=out)
    return out

@bp.get("/api/harmonizer/missing")
def api_harmonizer_missing():
    return jsonify({"stats": _harmonizer_missing_map()})

@bp.get("/api/harmonizer/index")
def api_harmonizer_index():
    """Dataset keys that are not part of the paper list (experiment-level artefacts)."""
    harm_files = set()
    try:
        for name in os.listdir(S.CODINGS):
            if " - harmonizer - " in name and name.endswith(".json"):
                harm_files.add(name.split(" - harmonizer - ", 1)[0])
    except OSError:
        pass
    extra = []
    for k, _p, _mt in sidecar_files():
        if k in S.KEYS:
            continue
        extra.append({"key": k, "n_dv": 0, "n_records": 0, "n_coders": 0,
                      "missing_metrics": 0, "needs_reason": 0, "module": None,
                      "has_harmonized": True, "has_extraction": False,
                      "has_harmonizer": k in harm_files})
    return jsonify({"papers": extra})

# Sidecar roles seen in the corpus; the select also keeps any other current value.
_ROLE_OPTIONS = ["dv", "covariate", "flag", "metacog", "condition", "id", "other"]

def _harmonizer_options():
    return {"role": _ROLE_OPTIONS + [""],
            "dv_domain": list(DOMAIN_VOCAB) + [""],
            "veracity": ["true", "false", ""],
            "timepoint": ["pre", "post", "followup", ""],
            "dv_polarity": ["higher_is_better", "higher_is_worse", "ambiguous", ""]}

_ROLE_RE = re.compile(r"^[a-z][a-z0-9_/ -]{0,40}$")
_TIMEPOINT_RE = re.compile(r"^(pre|post|followup)(_[a-z0-9]+)?$")
_UV_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

def field_error(field, value):
    """Why ``value`` is not a valid harmonized ``field`` value ('' when it is).

    Blank is always valid (it means "no value").  The vocabularies are the
    contract's: the merged 9-domain list, true/false veracity, a pre/post/
    followup timepoint and the two orientations (plus ``ambiguous``)."""
    v = "" if value is None else str(value).strip()
    if not v:
        return ""
    if field == "dv_domain" and v not in DOMAIN_VOCAB:
        return f"dv_domain {v!r} is not one of {', '.join(DOMAIN_VOCAB)}"
    if field == "veracity" and v.lower() not in ("true", "false"):
        return f"veracity {v!r} must be true or false"
    if field == "timepoint" and not _TIMEPOINT_RE.match(v):
        return f"timepoint {v!r} must be pre, post or followup"
    if field == "dv_polarity" and v not in ("higher_is_better", "higher_is_worse", "ambiguous"):
        return f"dv_polarity {v!r} must be higher_is_better or higher_is_worse"
    if field == "role" and not _ROLE_RE.match(v.lower()):
        return f"role {v!r} is not a role token"
    if field == "uniform_variable":
        if not _UV_RE.match(v):
            return f"uniform_variable {v!r} may only hold letters, digits and _"
        if v.endswith("_01"):
            return f"uniform_variable {v!r} names a _01 twin; assign the raw variable instead"
    return ""

def write_json_atomic(target, record):
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=S.TEMP)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        atomic_replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

@bp.post("/api/harmonizer/save")
def api_harmonizer_save():
    payload = request.get_json(force=True, silent=True) or {}
    key = payload.get("key")
    coder = payload.get("coder")
    overrides = payload.get("overrides")
    if not _harmonizer_known(key):
        return jsonify({"ok": False, "error": "unknown paper key"}), 400
    if not coder or not str(coder).strip():
        return jsonify({"ok": False, "error": "coder name is required"}), 400
    if not isinstance(overrides, dict):
        return jsonify({"ok": False, "error": "no overrides to save"}), 400
    data = load_harmonized_variables(key)
    known = {v["uniform_variable"] for v in data["variables"]} if data else None
    clean, errors = {}, []
    for uv, fields in overrides.items():
        if not isinstance(fields, dict):
            continue
        if known is not None and str(uv) not in known:
            errors.append(f"{uv}: not a variable of this study's sidecar")
            continue
        keep = {}
        for k, v in fields.items():
            if k not in _HARM_FIELDS or v is None:
                continue
            err = field_error(k, v)
            if err:
                errors.append(f"{uv}: {err}")
                continue
            v = str(v).strip()
            keep[k] = v.lower() if k == "veracity" else v
        if keep:
            clean[str(uv)] = keep
    if errors:
        return jsonify({"ok": False, "error": "invalid overrides", "errors": errors[:50]}), 400
    coder = safe_token(str(coder))
    target = os.path.join(S.CODINGS, f"{key} - harmonizer - {coder}.json")
    existed = os.path.exists(target)
    record = {"key": key, "coder": coder,
              "saved": datetime.now().isoformat(timespec="seconds"), "overrides": clean}
    write_json_atomic(target, record)
    return jsonify({"ok": True, "path": os.path.relpath(target, S.ROOT).replace("\\", "/"),
                    "overwrote": existed, "overrides": len(clean), "records": len(clean)})


# ===========================================================================
# Assignment workspace (plan §4 B3): raw-variable context, the live decoder
# preview and the coder-owned per-study assignment drafts + apply.
# ===========================================================================
from contextlib import contextmanager  # noqa: E402
import io  # noqa: E402
import sys  # noqa: E402

_DRAFT_FIELDS = ("artifact", "original_variable", "source_file", "item_scope",
                 "uniform_variable", "role", "dv_domain", "veracity", "timepoint",
                 "dv_polarity", "slot_fix", "evidence", "note", "status")


def _s(v):
    """Clean string: None/NaN sentinels and blanks -> "" (bool -> true/false)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if type(v).__name__ == "bool_":
        return "true" if bool(v) else "false"
    if v is None:
        return ""
    try:
        import pandas as pd
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return str(v).strip()


def _scripts_dir():
    return os.path.join(S.ROOT, "Scripts")


def _ensure_scripts_path():
    d = _scripts_dir()
    if d and d not in sys.path:
        sys.path.insert(0, d)
    return d


@contextmanager
def _quiet():
    """Silence a module import's stdout (harmonizer_template prints on import)."""
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        yield
    finally:
        sys.stdout = old


def _import_assignments():
    _ensure_scripts_path()
    import variable_assignments as va  # noqa: PLC0415
    return va


def _import_template():
    # The template is written to run from Datasets/<Key>/: on import it points
    # the process-wide ``tempfile.tempdir`` two folders above its own file, which
    # from Scripts/ is OUTSIDE the project.  Keep the app's temp dir.
    _ensure_scripts_path()
    saved = tempfile.tempdir
    try:
        with _quiet():
            import harmonizer_template as ht  # noqa: PLC0415
    finally:
        tempfile.tempdir = saved
    return ht


def _import_contract():
    _ensure_scripts_path()
    import contract as ct  # noqa: PLC0415
    return ct


def _import_dvp():
    _ensure_scripts_path()
    import dv_polarity as dvp  # noqa: PLC0415
    return dvp


def _study_folder(key):
    return os.path.join(S.DATASETS or "", key)


def _known_study(key):
    return valid_key(key) and os.path.isdir(_study_folder(key))


# ------------------------------------------------------------------ decode
@bp.get("/api/harmonizer/decode")
def api_harmonizer_decode():
    """Live decode preview using the harmonizer's own decoders.

    ``domain_of``/``timepoint_of`` come from the per-study harmonizer template
    (``harmonizer_template.py``); veracity is decoded by BOTH the canonical
    ``contract.dv_veracity`` and the template's ``veracity_of`` so a name/field
    disagreement is visible, never silent.  Expected polarity uses
    ``dv_polarity.expected_polarity``.

    ``contract.dv_veracity`` matches its tokens with ``\\b``, and ``_`` is a word
    character, so on a raw uniform name (``dv_credibility_false``) it never
    fires; it is therefore fed the name with ``_`` read as a space.  The sidecar
    timepoint is also mapped to the analysis bucket with
    ``contract.classify_timepoint`` (blank -> ``post_assumed``).

    Optional ``domain`` / ``veracity`` (the form's chosen values) give the
    polarity the rule expects for THOSE values (``polarity_for_fields``), so a
    coder sees when the chosen orientation contradicts the veracity rule.
    """
    name = (request.args.get("name") or "").strip()
    score_types = (request.args.get("score_types") or "").strip()
    notes = (request.args.get("notes") or "").strip()
    original = (request.args.get("original") or "").strip()
    form_domain = (request.args.get("domain") or "").strip()
    form_veracity = (request.args.get("veracity") or "").strip().lower()
    out = {"name": name, "domain": "", "veracity": "", "veracity_contract": "",
           "veracity_template": "", "veracity_agreement": None, "timepoint": "",
           "timepoint_bucket": "", "polarity": "", "polarity_source": "",
           "polarity_for_fields": "", "errors": []}
    if not name:
        return jsonify(out)
    try:
        ht = _import_template()
        out["domain"] = ht.domain_of(name)
        out["veracity_template"] = ht.veracity_of(name)
        out["timepoint"] = ht.timepoint_of(name)
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"template decoders unavailable: {e!r}")
    try:
        ct = _import_contract()
        out["veracity_contract"] = ct.dv_veracity(
            name.replace("_", " "), score_types.split(",") if score_types else None, notes)
        out["timepoint_bucket"] = ct.classify_timepoint(out["timepoint"])
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"contract decoders unavailable: {e!r}")
    vt, vc = out["veracity_template"], out["veracity_contract"]
    if vt or vc:
        out["veracity_agreement"] = (vt == vc)
        out["veracity"] = vc or vt
    try:
        dvp = _import_dvp()
        try:
            import domain_restriction as dr  # noqa: PLC0415
            resolve = dr.resolve_sidecar_domain
        except Exception:  # noqa: BLE001  (the rule still runs on the raw domain)
            def resolve(raw, _uv):
                return raw
        # dv_polarity._sidecar_rules resolves the domain the same way first.
        pol = dvp.expected_polarity(resolve(out["domain"] or "", name), out["veracity"],
                                    name, notes, original)
        out["polarity"] = pol or ""
        out["polarity_source"] = "dv_polarity.expected_polarity"
        if form_domain or form_veracity:
            pf = dvp.expected_polarity(resolve(form_domain or out["domain"] or "", name),
                                       form_veracity or out["veracity"], name, notes, original)
            out["polarity_for_fields"] = pf or ""
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"dv_polarity unavailable: {e!r}")
    return jsonify(out)


# ------------------------------------------------------------------ rawvar
def _header_of_csv(path):
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            return [str(c).strip().lstrip("\ufeff") for c in next(csv.reader(fh))]
    except (OSError, StopIteration):
        return []


def _series_from_csv(path, var, want_pid=False):
    import pandas as pd
    hdr = _header_of_csv(path)
    if var not in hdr:
        return None, None, None
    usecols = [var] + ([c for c in ("pid", "condition", "condition_raw")
                        if want_pid and c in hdr] if want_pid else [])
    df = pd.read_csv(path, usecols=usecols, encoding="utf-8-sig", low_memory=False)
    return df[var], (df if want_pid else None), None


def _series_from_spss(path, var):
    import pyreadstat
    reader = pyreadstat.read_dta if path.lower().endswith(".dta") else pyreadstat.read_sav
    df, meta = reader(path, usecols=[var])
    if var not in df.columns:
        return None, None, None, None
    labels = {}
    try:
        labels = dict((meta.variable_value_labels or {}).get(var) or {})
    except Exception:  # noqa: BLE001
        labels = {}
    vlabel = ""
    try:
        vlabel = (meta.column_names_to_labels or {}).get(var) or ""
    except Exception:  # noqa: BLE001
        vlabel = ""
    return df[var], labels, vlabel, meta


def _series_from_rds(path, var):
    import pyreadr
    obj = pyreadr.read_r(path)
    for _name, df in (obj or {}).items():
        if hasattr(df, "columns") and var in df.columns:
            return df[var], None, None
    return None, None, None


def _series_from_xlsx(path, var):
    try:
        import openpyxl
    except Exception:  # noqa: BLE001
        return None, None, None
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            rows = ws.iter_rows(values_only=True)
            hdr = None
            for row in rows:
                if row and any(c not in (None, "") for c in row):
                    hdr = [str(c).strip() if c is not None else "" for c in row]
                    break
            if not hdr or var not in hdr:
                continue
            idx = hdr.index(var)
            vals = []
            for row in rows:
                if row and idx < len(row):
                    vals.append(row[idx])
            import pandas as pd
            return pd.Series(vals, name=var), None, ws.title
    finally:
        try:
            wb.close()
        except Exception:  # noqa: BLE001
            pass
    return None, None, None


def _labels_from_spss(path, names):
    """Metadata-only value labels / variable label for the first matching name."""
    try:
        import pyreadstat
    except Exception:  # noqa: BLE001
        return {}, ""
    reader = pyreadstat.read_dta if path.lower().endswith(".dta") else pyreadstat.read_sav
    try:
        _df, meta = reader(path, metadataonly=True)
    except Exception:  # noqa: BLE001
        return {}, ""
    for nm in names:
        try:
            labels = dict((meta.variable_value_labels or {}).get(nm) or {})
        except Exception:  # noqa: BLE001
            labels = {}
        vlabel = ""
        try:
            vlabel = (meta.column_names_to_labels or {}).get(nm) or ""
        except Exception:  # noqa: BLE001
            vlabel = ""
        if labels or vlabel:
            return labels, vlabel
    return {}, ""


def _stats_and_examples(series):
    import pandas as pd
    s = pd.Series(series)
    n = int(len(s))
    nn = s.dropna()
    numeric = pd.to_numeric(nn, errors="coerce")
    num = numeric.dropna()
    stats = {
        "n": n,
        "missing": int(s.isna().sum()),
        "n_unique": int(nn.nunique()) if len(nn) else 0,
        "min": (float(num.min()) if len(num) else None),
        "max": (float(num.max()) if len(num) else None),
        "mean": (round(float(num.mean()), 4) if len(num) else None),
        "numeric": bool(len(num)),
    }
    top = []
    try:
        vc = nn.astype(str).value_counts().head(12)
        top = [{"value": str(k), "n": int(v)} for k, v in vc.items()]
    except Exception:  # noqa: BLE001
        top = []
    return stats, top


def _preview_columns(header, var, uniform, limit=12):
    """Up to ``limit`` informative columns: the target var/uniform column plus
    pid/condition when present, then the first columns in file order.  Never
    dumps hundreds of columns of a wide harmonized table."""
    present = set(header)
    lower = {str(c).lower(): c for c in header}
    important = []
    for nm in (var, uniform):
        if nm and nm in present and nm not in important:
            important.append(nm)
    for nm in ("pid", "condition", "condition_raw"):
        c = lower.get(nm)
        if c and c not in important:
            important.append(c)
    sel = list(important)
    for c in header:
        if len(sel) >= limit:
            break
        if c not in sel:
            sel.append(c)
    sel = sel[:limit]
    order = {c: i for i, c in enumerate(header)}
    sel.sort(key=lambda c: order.get(c, 1 << 30))
    return sel


def _preview_highlight(header, var, uniform):
    for nm in (var, uniform):
        if nm and nm in header:
            return nm
    return ""


def _preview_rows(df, cols, n_limit):
    """Python-native string rows (JSON-safe; numpy scalars never leak)."""
    out = []
    for row in df[cols].itertuples(index=False, name=None):
        out.append(["" if _isna(v) else _s(v) for v in row])
        if len(out) >= n_limit:
            break
    return out


def _preview_csv(path, kind, var, uniform, n_limit=10):
    import pandas as pd
    header = _header_of_csv(path)
    if not header:
        return None
    cols = _preview_columns(header, var, uniform)
    if not cols:
        return None
    n_rows = _csv_data_rows(path)
    df = pd.read_csv(path, usecols=cols, encoding="utf-8-sig", low_memory=False,
                     nrows=n_limit)
    return {"file": os.path.basename(path), "kind": kind,
            "n_rows": n_rows, "n_cols": len(header), "columns": cols,
            "rows": _preview_rows(df, cols, n_limit),
            "highlight": _preview_highlight(header, var, uniform)}


_ROWCOUNT_CACHE = {}

def _csv_data_rows(path):
    """Data-row count via streaming (header excluded); never loads the frame.
    Cached per file revision: the preview asks for it on every target click."""
    try:
        stamp = (os.path.getmtime(path), os.path.getsize(path))
    except OSError:
        return 0
    hit = _ROWCOUNT_CACHE.get(path)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    n = -1
    try:
        with open(path, encoding="utf-8-sig", newline="", errors="replace") as fh:
            for _row in csv.reader(fh):
                n += 1
    except OSError:
        return 0
    _ROWCOUNT_CACHE[path] = (stamp, max(n, 0))
    return max(n, 0)


def _preview_spss(path, kind, var, uniform, n_limit=10):
    import pyreadstat
    reader = pyreadstat.read_dta if path.lower().endswith(".dta") else pyreadstat.read_sav
    df, meta = reader(path, row_limit=n_limit)
    header = [str(c) for c in df.columns]
    cols = _preview_columns(header, var, uniform)
    if not cols:
        return None
    try:
        n_rows = int(getattr(meta, "number_rows", 0) or 0)
    except (TypeError, ValueError):
        n_rows = 0
    if n_rows <= 0:
        n_rows = int(df.shape[0])
    try:
        n_cols = int(getattr(meta, "number_columns", 0) or 0)
    except (TypeError, ValueError):
        n_cols = 0
    if n_cols <= 0:
        n_cols = len(header)
    return {"file": os.path.basename(path), "kind": kind,
            "n_rows": n_rows, "n_cols": n_cols, "columns": cols,
            "rows": _preview_rows(df, cols, n_limit),
            "highlight": _preview_highlight(header, var, uniform)}


def _preview_xlsx(path, kind, var, uniform, n_limit=10):
    try:
        import openpyxl
    except Exception:  # noqa: BLE001
        return None
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            it = ws.iter_rows(values_only=True)
            header = None
            for row in it:
                if row and any(c not in (None, "") for c in row):
                    header = ["" if c is None else str(c).strip() for c in row]
                    break
            if not header:
                continue
            cols = _preview_columns(header, var, uniform)
            if not cols:
                continue
            idxs = [header.index(c) for c in cols]
            rows = []
            for row in it:
                if row is None:
                    break
                rows.append(["" if i >= len(row) or row[i] is None else _s(row[i])
                             for i in idxs])
                if len(rows) >= n_limit:
                    break
            try:
                n_rows = int(ws.max_row or len(rows))
            except (TypeError, ValueError):
                n_rows = len(rows)
            return {"file": os.path.basename(path), "kind": kind,
                    "n_rows": n_rows, "n_cols": len(header), "columns": cols,
                    "rows": rows, "highlight": _preview_highlight(header, var, uniform)}
    finally:
        try:
            wb.close()
        except Exception:  # noqa: BLE001
            pass
    return None


def _dataset_preview(key, folder, var, uniform, artifact):
    """A small read-only preview of the study's dataset (first ~10 rows, up to
    ~12 informative columns).  Prefers the requested ``prepared`` artifact, then
    any prepared CSV, then the harmonized wide table, then the raw source.
    Returns ``None`` when nothing is readable — the UI degrades gracefully."""
    cands = []
    csvs = sorted(glob.glob(os.path.join(folder, f"{key} - prepared*.csv")))
    if artifact:
        preferred = os.path.join(folder, f"{key} - prepared {artifact}.csv")
        if os.path.isfile(preferred):
            cands.append((preferred, "prepared"))
    for c in csvs:
        if all(c != p for p, _ in cands):
            cands.append((c, "prepared"))
    harm = os.path.join(folder, f"{key} - harmonized.csv")
    if os.path.isfile(harm):
        cands.append((harm, "harmonized"))
    srcs = _rawvar_sources(folder, key)
    for ext_key, kind in (("csv", "csv"), ("sav", "sav"), ("dta", "dta"),
                          ("xlsx", "xlsx")):
        for base in srcs.get(ext_key, []):
            p = os.path.join(folder, base)
            if all(p != q for q, _ in cands):
                cands.append((p, kind))
    for path, kind in cands:
        low = path.lower()
        try:
            if low.endswith(".csv"):
                got = _preview_csv(path, kind, var, uniform)
            elif low.endswith((".sav", ".dta")):
                got = _preview_spss(path, kind, var, uniform)
            elif low.endswith((".xlsx", ".xls")):
                got = _preview_xlsx(path, kind, var, uniform)
            else:
                got = None
        except Exception:  # noqa: BLE001  (never block an assignment)
            got = None
        if got and got.get("columns"):
            return got
    return None


def _rawvar_sources(folder, key):
    out = {"csv": [], "sav": [], "dta": [], "rds": [], "xlsx": []}
    for f in sorted(glob.glob(os.path.join(folder, "*"))):
        base = os.path.basename(f)
        low = base.lower()
        if not os.path.isfile(f):
            continue
        if low.endswith(".csv"):
            out["csv"].append(base)
        elif low.endswith(".sav"):
            out["sav"].append(base)
        elif low.endswith(".dta"):
            out["dta"].append(base)
        elif low.endswith((".rds", ".rdata")):
            out["rds"].append(base)
        elif low.endswith((".xlsx", ".xls")):
            out["xlsx"].append(base)
    return out


def _snippets(key, names):
    """Small paper/coding-notes snippets mentioning the variable name."""
    out = []
    targets = [n for n in names if n and len(str(n)) >= 3]
    if not targets:
        return out
    sources = [(os.path.join(S.TEXT, key + ".txt"), "paper text"),
               (os.path.join(S.EXTRACTION, key + ".md"), "coding notes")]
    for path, label in sources:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        low = text.lower()
        for name in targets:
            i = low.find(str(name).lower())
            if i < 0:
                continue
            a, b = max(0, i - 160), min(len(text), i + 220)
            out.append({"source": label, "text": text[a:b].replace("\n", " ").strip()})
            if len(out) >= 6:
                return out
    return out


@bp.get("/api/rawvar/<path:key>")
def api_rawvar(key):
    """Read-only context for one raw variable (labels, stats, examples, snippets).

    Best-effort and **never blocks an assignment**: a missing/unreadable source
    yields an empty payload plus a ``degraded`` reason, not an error.
    """
    if not _known_study(key):
        abort(404)
    var = (request.args.get("var") or "").strip()
    artifact = (request.args.get("artifact") or "").strip()
    folder = _study_folder(key)
    out = {"key": key, "var": var, "artifact": artifact, "found": False,
           "source": None, "value_labels": [], "variable_label": "",
           "stats": None, "top_values": [], "examples": [], "example_cols": [],
           "original_values": "", "mapped_values": "", "snippets": [],
           "dataset_preview": None,
           "codebook_files": [], "sources": _rawvar_sources(folder, key),
           "degraded": []}

    # variables.csv context (original_values / mapped_values / notes)
    var_path = os.path.join(folder, f"{key} - variables.csv")
    if os.path.isfile(var_path):
        try:
            import pandas as pd
            v = pd.read_csv(var_path, encoding="utf-8-sig", low_memory=False)
            v.columns = [str(c).strip().lstrip("\ufeff") for c in v.columns]
            ucol = "uniform_variable" if "uniform_variable" in v.columns else (
                "uniform_name" if "uniform_name" in v.columns else None)
            hit = v[v["original_variable"].astype(str) == var]
            if len(hit):
                r = hit.iloc[0]
                out["original_values"] = str(r.get("original_values") or "")[:2000]
                out["mapped_values"] = str(r.get("mapped_values") or "")[:2000]
                if ucol:
                    out["uniform_variable"] = str(r.get(ucol) or "")
        except Exception as e:  # noqa: BLE001
            out["degraded"].append(f"variables.csv unreadable: {e!r}")
    else:
        out["degraded"].append("no variables.csv for this study")

    uniform = (request.args.get("uniform") or "").strip()

    # Dataset preview (item 2): a small read-only view of the prepared/harmonized
    # table so the context pane shows the DATASET, not just one column.  Built
    # before the "no variable" early return so study-scope targets get it too;
    # absent/unreadable sources degrade to None and never block an assignment.
    try:
        out["dataset_preview"] = _dataset_preview(
            key, folder, var, uniform or out.get("uniform_variable", ""), artifact)
    except Exception as e:  # noqa: BLE001
        out["dataset_preview"] = None
        out["degraded"].append(f"dataset preview failed: {e!r}")

    if not var:
        out["degraded"].append("no variable requested")
        return jsonify(out)

    names = [n for n in (var, uniform) if n]
    series = None
    # 1) prepared CSVs (prefer the requested artifact)
    csvs = sorted(glob.glob(os.path.join(folder, f"{key} - prepared*.csv")))
    ordered = []
    if artifact:
        for c in csvs:
            if os.path.basename(c) in (f"{key} - prepared {artifact}.csv",):
                ordered.insert(0, c)
    ordered += [c for c in csvs if c not in ordered]
    for c in ordered:
        for nm in names:
            try:
                s, extra, _ = _series_from_csv(c, nm, want_pid=True)
            except Exception as e:  # noqa: BLE001
                out["degraded"].append(f"{os.path.basename(c)} unreadable: {e!r}")
                break
            if s is not None:
                series = s
                out["found"] = True
                out["matched_name"] = nm
                out["source"] = {"file": os.path.basename(c), "kind": "csv"}
                if extra is not None:
                    ex = extra.head(10)
                    out["example_cols"] = [str(x) for x in ex.columns]
                    out["examples"] = [
                        {str(k): _py(val) for k, val in row.items()}
                        for _, row in ex.iterrows()]
                break
        if series is not None:
            break

    # 2) SPSS / Stata
    if series is None:
        for c in (glob.glob(os.path.join(folder, "*.sav"))
                  + glob.glob(os.path.join(folder, "*.dta"))):
            for nm in names:
                try:
                    s, labels, vlabel, _meta = _series_from_spss(c, nm)
                except Exception as e:  # noqa: BLE001
                    out["degraded"].append(f"{os.path.basename(c)} unreadable: {e!r}")
                    break
                if s is not None:
                    series = s
                    out["found"] = True
                    out["matched_name"] = nm
                    out["source"] = {"file": os.path.basename(c),
                                     "kind": "sav" if c.lower().endswith(".sav") else "dta"}
                    out["variable_label"] = vlabel or ""
                    out["value_labels"] = [{"value": str(k), "label": str(v)}
                                           for k, v in (labels or {}).items()]
                    break
            if series is not None:
                break

    # 3) RDS
    if series is None:
        for c in (glob.glob(os.path.join(folder, "*.rds"))
                  + glob.glob(os.path.join(folder, "*.RData"))
                  + glob.glob(os.path.join(folder, "*.rdata"))):
            for nm in names:
                try:
                    s, _l, _v = _series_from_rds(c, nm)
                except Exception as e:  # noqa: BLE001
                    out["degraded"].append(f"{os.path.basename(c)} unreadable: {e!r}")
                    break
                if s is not None:
                    series = s
                    out["found"] = True
                    out["matched_name"] = nm
                    out["source"] = {"file": os.path.basename(c), "kind": "rds"}
                    break
            if series is not None:
                break

    # 4) XLSX
    if series is None:
        for c in (glob.glob(os.path.join(folder, "*.xlsx"))
                  + glob.glob(os.path.join(folder, "*.xls"))):
            for nm in names:
                try:
                    s, _l, sheet = _series_from_xlsx(c, nm)
                except Exception as e:  # noqa: BLE001
                    out["degraded"].append(f"{os.path.basename(c)} unreadable: {e!r}")
                    break
                if s is not None:
                    series = s
                    out["found"] = True
                    out["matched_name"] = nm
                    out["source"] = {"file": os.path.basename(c), "kind": "xlsx",
                                     "sheet": sheet}
                    break
            if series is not None:
                break

    if series is None:
        out["degraded"].append("raw column not found in any source file")
    else:
        # value labels often live only in the SPSS/Stata file while the series
        # was read from the (already renamed) prepared CSV -- fetch them from
        # metadata cheaply when still missing.
        if not out["value_labels"]:
            for c in (glob.glob(os.path.join(folder, "*.sav"))
                      + glob.glob(os.path.join(folder, "*.dta"))):
                labels, vlabel = _labels_from_spss(c, names)
                if labels or vlabel:
                    out["value_labels"] = [{"value": str(k), "label": str(v)}
                                           for k, v in labels.items()]
                    out["variable_label"] = vlabel or out["variable_label"]
                    if not out.get("label_source"):
                        out["label_source"] = os.path.basename(c)
                    break
        try:
            stats, top = _stats_and_examples(series)
            out["stats"] = stats
            out["top_values"] = top
            if not out["examples"]:
                import pandas as pd
                nn = pd.Series(series).dropna().head(10)
                out["examples"] = [{"row": _py(i), "value": _py(v)}
                                   for i, v in nn.items()]
                out["example_cols"] = ["row", "value"]
        except Exception as e:  # noqa: BLE001
            out["degraded"].append(f"stats failed: {e!r}")

    # codebook / paper snippets
    try:
        out["codebook_files"] = [os.path.basename(f) for f in sorted(glob.glob(
            os.path.join(folder, "*")))
            if re.search(r"codebook|survey|questionnaire|instrument|readme",
                         os.path.basename(f), re.I)]
        out["snippets"] = _snippets(key, [var, out.get("uniform_variable", "")])
    except Exception as e:  # noqa: BLE001
        out["degraded"].append(f"snippets failed: {e!r}")
    return jsonify(out)


def _isna(v):
    try:
        import pandas as pd
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return v is None


def _py(v):
    """A JSON-safe Python scalar (numpy ints / timestamps do not serialise)."""
    if _isna(v):
        return None
    if isinstance(v, (bool, int, float, str)):
        return v
    item = getattr(v, "item", None)
    if callable(item):
        try:
            v = item()
        except (TypeError, ValueError):
            pass
        if isinstance(v, (bool, int, float, str)):
            return v
    return str(v)


# --------------------------------------------------------------- assignments
# One lock for every write to a study's assignment file + harmonized products:
# write_rows is a read-modify-write and apply_files rewrites the sidecar/wide,
# so two concurrent saves would otherwise lose rows.
_ASSIGN_LOCK = threading.Lock()

_ROLE_PREFIX = {"dv": "dv_", "covariate": "cov_", "flag": "flag_"}


def _draft_path(key, coder):
    return os.path.join(S.CODINGS, f"{key} - assignments - {safe_token(coder)}.json")


def _read_draft(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _clean_assignment(a):
    if not isinstance(a, dict):
        return None
    row = {k: ("" if a.get(k) is None else str(a.get(k)).strip())
           for k in _DRAFT_FIELDS}
    if not row["original_variable"]:
        return None
    if row["status"] not in ("proposed", "confirmed", "conflict"):
        row["status"] = "proposed"
    if row["veracity"].lower() in ("true", "false"):
        row["veracity"] = row["veracity"].lower()
    return row


def _draft_rows(key):
    """All drafts for ``key`` as (coder, row) pairs, plus the merged view."""
    out, by_coder = [], {}
    for f in glob.glob(os.path.join(S.CODINGS, glob.escape(key) + " - assignments - *.json")):
        base = os.path.basename(f)[: -len(".json")]
        coder = base[len(key) + len(" - assignments - "):]
        data = _read_draft(f)
        if not data:
            continue
        rows = [r for r in (_clean_assignment(a) for a in (data.get("assignments") or []))
                if r]
        by_coder[coder] = rows
        out.extend({"coder": coder, **r} for r in rows)
    return out, by_coder


@contextmanager
def _assignment_env(va):
    """Point the module's audit log and archive at the configured data root.

    ``variable_assignments`` derives ``Results/variable_assignment_log.jsonl``
    and its archive folder from its OWN file location, so with the app re-rooted
    (another data root, a test fixture) every save would log into, and archive
    into, the source checkout.  The module reads two environment overrides;
    they are set only for the write and only when not already set."""
    same = (os.path.normcase(os.path.abspath(S.ROOT))
            == os.path.normcase(os.path.abspath(getattr(va, "ROOT", S.ROOT))))
    want = {
        "VARIABLE_ASSIGNMENT_LOG": os.path.join(S.ROOT, "Results",
                                                "variable_assignment_log.jsonl"),
        "VARIABLE_ASSIGNMENT_ARCHIVE": os.path.join(
            S.ROOT, "archive", os.path.basename(getattr(va, "ARCHIVE_DIR", "ws_b3_backup"))),
    }
    set_now = [] if same else [k for k in want if k not in os.environ]
    for k in set_now:
        os.environ[k] = want[k]
    try:
        yield
    finally:
        for k in set_now:
            os.environ.pop(k, None)


def _safe_member(folder, name):
    """``name`` as a file directly under ``folder`` (no absolute path, no ``..``)."""
    if not name or os.path.isabs(name) or ":" in name:
        return None
    full = os.path.normpath(os.path.join(folder, name))
    root = os.path.normpath(folder)
    if os.path.commonpath([full, root]) != root or not os.path.isfile(full):
        return None
    return full


def _validate_rows(key, folder, va, rows, targets):
    """``(errors, warnings)`` for incoming rows, checked against the contract
    and against what ``variable_assignments.apply`` will actually do with them.

    * every label is in the contract's vocabulary and a ``dv``/``covariate``/
      ``flag`` role carries the matching ``dv_``/``cov_``/``flag_`` name;
    * a split row names a source file inside the study folder and both value lists;
    * ``apply`` patches only the FIRST sidecar row of a raw key, so a row whose
      raw column feeds several harmonized variables is refused when the coder
      selected another one of them (it would silently label the wrong variable);
    * a ``proposed`` value over a non-blank label is logged as a conflict and
      never applied -- the coder is told so before saving."""
    data = load_harmonized_variables(key)
    side = data["variables"] if data else []
    errors, warnings = [], []
    for r, target in zip(rows, targets):
        label = r["original_variable"]
        role = r["role"].lower()
        is_na = role in va.NA_ROLES
        if va._is_split_row(r):
            if not _safe_member(folder, r["source_file"]):
                errors.append(f"{label}: split source file {r['source_file']!r} is not a "
                              "file in this study's dataset folder")
            spec = va._parse_kv(r["note"])
            spec.update(va._parse_kv(r["evidence"]))
            if not (va._as_list(spec.get("true_values")) and va._as_list(spec.get("false_values"))):
                errors.append(f"{label}: a split needs both true_values and false_values")
            continue
        for f in ("uniform_variable", "role", "dv_domain", "veracity", "timepoint", "dv_polarity"):
            if f == "role" and is_na:
                continue
            err = field_error(f, r[f])
            if err:
                errors.append(f"{label}: {err}")
        uv = r["uniform_variable"]
        prefix = _ROLE_PREFIX.get(role)
        if uv and prefix and not uv.startswith(prefix):
            errors.append(f"{label}: role {role!r} needs a {prefix}* uniform name, not {uv!r}")
        hits = _raw_key_matches(side, r["artifact"], r["original_variable"])
        if len(hits) > 1:
            names = [side[i]["uniform_variable"] for i in hits]
            first = names[0]
            if target and target != first and target in names:
                errors.append(
                    f"{label}: this raw column feeds {len(hits)} harmonized variables "
                    f"({', '.join(names[:6])}{', ...' if len(names) > 6 else ''}); an "
                    f"assignment is applied to the first one ({first}) only, so it cannot "
                    f"label {target} -- fix it in the study's harmonizer instead")
                continue
            warnings.append(f"{label}: feeds {len(hits)} harmonized variables; the "
                            f"assignment is applied to {first} only")
        if not hits:
            if not (uv and role and not is_na):
                warnings.append(f"{label}: no sidecar row uses this raw column and the row "
                                "names no uniform_variable + role, so the dataset is unchanged")
            continue
        cur = side[hits[0]]
        if r["status"] == "confirmed":
            continue
        for f in ("uniform_variable", "role", "dv_domain", "veracity", "timepoint", "dv_polarity"):
            v, c = r[f], cur.get(f, "")
            if f == "role" and is_na:
                continue
            if v and c and v.lower() != c.lower():
                warnings.append(f"{label}: {f} {c!r} -> {v!r} is only proposed; it is logged "
                                "as a conflict, not applied (set the decision to confirmed "
                                "to override)")
    return errors, warnings


def _file_stamp(path):
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def _backup_products(key, folder):
    """Copy the harmonized products ``apply_files`` may rewrite in place.

    The sidecar is always copied (it is small).  The wide table is copied only
    when the study has no ``harmonizer.py``: otherwise a re-run regenerates it,
    while a sidecar-only study could never get its original back."""
    import shutil
    paths = [os.path.join(folder, f"{key} - harmonized_variables.csv")]
    if not os.path.isfile(os.path.join(folder, "harmonizer.py")):
        paths.append(os.path.join(folder, f"{key} - harmonized.csv"))
    out = []
    tmpdir = os.path.join(S.TEMP, "codingstudio_assign_backup")
    os.makedirs(tmpdir, exist_ok=True)
    for p in paths:
        if not os.path.isfile(p):
            continue
        fd, tmp = tempfile.mkstemp(suffix=".csv", dir=tmpdir)
        os.close(fd)
        shutil.copy2(p, tmp)
        out.append((p, tmp, _file_stamp(p)))
    return out


def _settle_backups(key, backups):
    """Archive the pre-apply copy of every product that changed; drop the rest."""
    import shutil
    archived = []
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    adir = os.path.join(S.ROOT, "archive",
                        "_codingstudio_assign_backup_" + datetime.now().strftime("%Y-%m-%d"), key)
    for src, tmp, before in backups:
        try:
            if _file_stamp(src) != before:
                os.makedirs(adir, exist_ok=True)
                base, ext = os.path.splitext(os.path.basename(src))
                dst = os.path.join(adir, f"{base} {stamp}{ext}")
                shutil.move(tmp, dst)
                archived.append(os.path.relpath(dst, S.ROOT).replace("\\", "/"))
        except OSError:
            pass
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return archived


def _write_and_apply(key, folder, va, out_rows, do_apply):
    """Upsert the rows, then (optionally) apply the file to the harmonized products.

    Returns ``(n_total, records, archived, error)``; ``error`` names the failing
    step.  Serialised by ``_ASSIGN_LOCK``."""
    with _ASSIGN_LOCK, _assignment_env(va):
        try:
            n_total = va.write_rows(key, folder, out_rows)
        except Exception as e:  # noqa: BLE001
            return None, [], [], f"write_rows failed: {e!r}"
        from routes_coverage import invalidate_study  # noqa: PLC0415  (circular at import)
        invalidate_study(key)
        if not do_apply:
            return n_total, [], [], None
        backups = []
        try:
            backups = _backup_products(key, folder)
            records = va.apply_files(key, folder)
        except Exception as e:  # noqa: BLE001
            return n_total, [], _settle_backups(key, backups), f"apply_files failed: {e!r}"
        return n_total, records, _settle_backups(key, backups), None


def _action_rows(records):
    return [{"action": r.get("action"), "original_variable": r.get("original_variable"),
             "uniform_variable": r.get("uniform_variable"),
             "field": r.get("field"), "old": r.get("old"), "new": r.get("new"),
             "status": r.get("status"), "note": r.get("note")}
            for r in records]


def _assignment_rows(va, rows, coder, stamp):
    out = []
    for r in rows:
        row = {c: "" for c in va.ASSIGN_COLS}
        for f in _DRAFT_FIELDS:
            if f in row:
                row[f] = r.get(f, "")
        row["coder"] = coder or r.get("coder", "")
        row["decided_at"] = stamp
        if not row.get("status"):
            row["status"] = "proposed"
        out.append(row)
    return out


@bp.get("/api/assignments/<path:key>")
def api_assignments(key):
    if not _known_study(key):
        abort(404)
    merged, by_coder = _draft_rows(key)
    error = None
    try:
        va = _import_assignments()
        saved = va.load(key, _study_folder(key))
        applied = [{c: _s(v) for c, v in r.items()} for _, r in saved.iterrows()]
    except Exception as e:  # noqa: BLE001
        applied = []
        error = f"variable_assignments unavailable: {e!r}"
    return jsonify({"key": key, "drafts": merged, "by_coder": by_coder,
                    "applied": applied, "applied_path": f"{key} - variable_assignments.csv",
                    "fields": list(_DRAFT_FIELDS), "error": error})


@bp.post("/api/assignments/<path:key>")
def api_assignments_save(key):
    """Save coder assignments **directly into the canonical dataset file**.

    Accepts ``{"assignments": [...], "coder"?: str, "apply_to_dataset"?: bool}``.
    The coder name is OPTIONAL — a blank name is recorded under the neutral
    label ``coder``.  A row may carry ``target_uniform`` (the sidecar variable
    the coder selected; not stored) so a raw column shared by several variables
    is refused instead of labelling the wrong one.  Rows are validated first
    (``_validate_rows``): any error rejects the whole save, warnings are returned.
    Every row is then upserted into
    ``Datasets/<Key>/<Key> - variable_assignments.csv`` via
    ``variable_assignments.write_rows`` and, unless ``apply_to_dataset`` is
    false, applied to the harmonized sidecar/wide with ``apply_files`` (which
    fills blanks / confirmed overrides / split rows and appends the per-field
    audit trail to ``Results/variable_assignment_log.jsonl``).  The pre-apply
    sidecar is archived when it changes.  A per-coder draft copy is kept in
    ``Codings/<Key> - assignments - <coder>.json`` for audit/back-compat.
    """
    if not _known_study(key):
        return jsonify({"ok": False, "error": "unknown study key"}), 404
    payload = request.get_json(force=True, silent=True) or {}
    coder = safe_token(str(payload.get("coder") or "").strip() or "coder")
    incoming = payload.get("assignments")
    do_apply = payload.get("apply_to_dataset")
    do_apply = True if do_apply is None else bool(do_apply)
    if not isinstance(incoming, list):
        return jsonify({"ok": False, "error": "assignments must be a list"}), 400
    clean, targets = [], []
    for a in incoming:
        r = _clean_assignment(a)
        if not r:
            continue
        if r["status"] == "conflict":      # only apply() stamps a conflict
            r["status"] = "proposed"
        clean.append(r)
        targets.append(str(a.get("target_uniform") or "").strip())
    if not clean:
        return jsonify({"ok": False, "error": "no assignments to save"}), 400

    try:
        va = _import_assignments()
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"variable_assignments unavailable: {e!r}"}), 500

    folder = _study_folder(key)
    errors, warnings = _validate_rows(key, folder, va, clean, targets)
    if errors:
        return jsonify({"ok": False, "error": "; ".join(errors[:3]) + (
            f" (+{len(errors) - 3} more)" if len(errors) > 3 else ""),
            "errors": errors, "warnings": warnings}), 400

    stamp = datetime.now().isoformat(timespec="seconds")
    out_rows = _assignment_rows(va, clean, coder, stamp)
    n_total, records, archived, error = _write_and_apply(key, folder, va, out_rows, do_apply)
    if error:
        body = {"ok": False, "error": error, "backup": archived}
        if n_total is not None:
            body["rows_written"] = n_total
        return jsonify(body), 500

    # keep the per-coder draft copy in sync (audit trail / back-compat).
    # The canonical dataset write above is the contract, so a draft-write problem
    # is reported but never fails the save.
    draft_path = None
    draft_error = None
    try:
        target = _draft_path(key, coder)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        existing = _read_draft(target) or {"key": key, "coder": coder, "assignments": []}
        old = [r for r in (_clean_assignment(a) for a in (existing.get("assignments") or [])) if r]
        incoming_keys = {(r["artifact"], r["original_variable"], r["item_scope"]) for r in clean}
        keep = [r for r in old
                if (r["artifact"], r["original_variable"], r["item_scope"]) not in incoming_keys]
        merged = keep + clean
        write_json_atomic(target, {"key": key, "coder": coder, "saved": stamp,
                                   "assignments": merged})
        merged_total = len(merged)
        draft_path = os.path.relpath(target, S.ROOT).replace("\\", "/")
    except Exception as e:  # noqa: BLE001
        merged_total = len(clean)
        draft_error = repr(e)

    actions = _action_rows(records)
    return jsonify({"ok": True, "coder": coder, "count": len(clean), "total": merged_total,
                    "rows_written": n_total, "applied_to_dataset": do_apply,
                    "changes": sum(1 for a in actions
                                   if a["action"] in ("fill", "override", "add_row", "split")),
                    "conflicts": sum(1 for a in actions if a["action"] == "conflict"),
                    "records": actions, "warnings": warnings, "backup": archived,
                    "path": f"{key} - variable_assignments.csv",
                    "draft_path": draft_path, "draft_error": draft_error})


@bp.post("/api/assignments/<path:key>/apply")
def api_assignments_apply(key):
    """Apply already-saved drafts to ``<Key> - variable_assignments.csv`` and
    (unless ``apply_to_dataset`` is false) regenerate the sidecar/wide.

    Kept for backwards compatibility; the assignment workspace now saves through
    ``POST /api/assignments/<key>`` in one sweep."""
    if not _known_study(key):
        return jsonify({"ok": False, "error": "unknown study key"}), 404
    payload = request.get_json(force=True, silent=True) or {}
    coder = str(payload.get("coder") or "").strip()
    do_apply = payload.get("apply_to_dataset")
    do_apply = True if do_apply is None else bool(do_apply)
    folder = _study_folder(key)
    merged, by_coder = _draft_rows(key)
    rows = (by_coder.get(coder) or []) if coder else list(merged)
    if not rows:
        return jsonify({"ok": False, "error": "no assignment drafts to apply"}), 400

    try:
        va = _import_assignments()
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"variable_assignments unavailable: {e!r}"}), 500

    errors, warnings = _validate_rows(key, folder, va, rows, [""] * len(rows))
    if errors:
        return jsonify({"ok": False, "error": "; ".join(errors[:3]), "errors": errors,
                        "warnings": warnings}), 400
    stamp = datetime.now().isoformat(timespec="seconds")
    out_rows = _assignment_rows(va, rows, coder, stamp)
    n_total, records, archived, error = _write_and_apply(key, folder, va, out_rows, do_apply)
    if error:
        body = {"ok": False, "error": error, "backup": archived}
        if n_total is not None:
            body["rows_written"] = n_total
        return jsonify(body), 500
    actions = _action_rows(records)
    return jsonify({"ok": True, "rows_written": n_total, "applied_to_dataset": do_apply,
                    "changes": len(actions), "records": actions, "warnings": warnings,
                    "backup": archived, "path": f"{key} - variable_assignments.csv"})
