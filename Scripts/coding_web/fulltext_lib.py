"""Full-text exclusion decided by the coders.

Each coder records, for a study, whether the full text must be excluded and why.
Decisions live one file per coder tag in ``Fulltext/<Tag>.json`` (owned class:
only that coder writes it; see ``Scripts/online_shared/protocol.py``).  A legacy
``Screening/fulltext - <Tag>.json`` is still read, so an existing project keeps
working; a save always writes the ``Fulltext/`` file.

When ``AGREE_MIN`` distinct coders agree on exclusion the study is listed in
``Screening/fulltext_agreed.json`` (derived); the coding tab hides it and the
analysis pipeline drops it.  In a *compiled* project (``Project/master_base.xlsx``
exists, see ``Scripts/compile_master.py``) the master workbook is no longer
written here: the decisions are the source and the compiler builds the workbook
from them (the union rule of ``Scripts/homogenize_fulltext_exclusions.py``).  In
a legacy project the newly-agreed studies are still marked in
``master_dataset.xlsx``.

The canonical agreed list is also what ``Scripts/study_exclusions.py`` reads so the
exclusion reaches ``metaanalysis.py`` / ``reanalysis.py`` / ``publication_bias.py``.
"""
from datetime import datetime
import glob
import json
import os
import re
import shutil
import tempfile
import threading

from studio_lib import atomic_replace, safe_token
from studio_state import S, log, on_reroot

# Controlled exclusion criteria (mirrors the values used in master_dataset.xlsx).
FULLTEXT_CRITERIA = [
    {"value": "C1", "label": "No misinformation intervention (C1)"},
    {"value": "C2", "label": "No control/comparison group (C2)"},
    {"value": "C3", "label": "Not English (C3)"},
    {"value": "C4", "label": "Not a primary experimental study (C4)"},
    {"value": "irretrievable", "label": "Fulltext irretrievable"},
    {"value": "no_contrast", "label": "No eligible contrast reported"},
    {"value": "duplicate", "label": "Duplicate/non-independent report of an included study"},
    {"value": "observational", "label": "No experimental intervention (observational)"},
    {"value": "domain_mismatch", "label": "Outcome/intervention outside misinformation susceptibility"},
    {"value": "other", "label": "Other (specify)"},
]
_CRIT_LABEL = {c["value"]: c["label"] for c in FULLTEXT_CRITERIA}

# The CODING field's criterion vocabulary (owner decision 2026-11-19: "the eight
# fulltext exclusion criteria").  The screening layer keeps its own ten classify
# options (criteria.json); this is the canonical set the coding field records.
# ``observational`` folds into C4 (not a primary experimental study); ``other``
# is an administrative catch-all, never a recorded criterion.
FIELD_CRITERIA = [
    {"value": "C1", "label": "No misinformation intervention (C1)",
     "short": "C1 no intervention"},
    {"value": "C2", "label": "No control/comparison group (C2)",
     "short": "C2 no control"},
    {"value": "C3", "label": "Not English (C3)", "short": "C3 not English"},
    {"value": "C4", "label": "Not a primary experimental study (C4)",
     "short": "C4 not experimental"},
    {"value": "irretrievable", "label": "Fulltext irretrievable",
     "short": "irretrievable"},
    {"value": "no_contrast", "label": "No eligible contrast reported",
     "short": "no eligible contrast"},
    {"value": "duplicate",
     "label": "Duplicate/non-independent report of an included study",
     "short": "duplicate report"},
    {"value": "domain_mismatch",
     "label": "Outcome/intervention outside misinformation susceptibility",
     "short": "outside scope"},
]
FIELD_CRITERIA_VALUES = frozenset(c["value"] for c in FIELD_CRITERIA)

# Master / screening label variants that mean one of the eight criteria.
_CRIT_ALIAS = {
    "insufficient reporting": "no_contrast",
    "study ran but no eligible contrast is reported": "no_contrast",
    "fulltext irretrievable": "irretrievable",
    "no obtainable copy": "irretrievable",
    "duplicate of an included study": "duplicate",
    "non-independent report": "duplicate",
    "no experimental intervention (observational)": "C4",
    "not a primary experimental study": "C4",
    "outcome/intervention outside misinformation susceptibility": "domain_mismatch",
}


def normalize_criterion(text):
    """Canonical criterion value(s) for a recorded label, alias or value.

    Returns a list of ``FIELD_CRITERIA`` values (C1..C4 tokens are extracted
    too, so a combined master label yields every criterion it names), or an
    empty list when nothing maps - the caller must then leave the field blank
    and record an anomaly ("never invent a criterion").
    """
    s = str(text or "").strip()
    if not s or s == "(unspecified)":
        return []
    out = []
    for code in ("C1", "C2", "C3", "C4"):
        if code.lower() in s.lower():
            out.append(code)
    if out:
        return out
    low = s.lower()
    for alias, value in _CRIT_ALIAS.items():
        if alias in low:
            out.append(value)
    if out:
        return out
    for c in FULLTEXT_CRITERIA:
        if c["value"] in FIELD_CRITERIA_VALUES and c["label"].lower() == low:
            out.append(c["value"])
    return out

AGREE_MIN = 2

_MARK_LOCK = threading.Lock()
_MARK_STATE = {"running": False, "last": None, "marked": [], "error": "", "finished": None}

_MAP_CACHE = {"mtime": None, "map": None, "info": None}

# Serialises every read-modify-write of a coder file and of the agreed list,
# so two quick saves (or two coders saving at once) cannot lose one.
_DECISIONS_LOCK = threading.RLock()


def criteria():
    return {"criteria": FULLTEXT_CRITERIA, "agree_min": AGREE_MIN}


# ---------------------------------------------------------------- per-coder store
def fulltext_dir():
    """The folder of the per-tag decision files (``Fulltext/``, created on save)."""
    return getattr(S, "FULLTEXT", None) or os.path.join(S.ROOT, "Fulltext")


def decision_path(coder):
    """The canonical decision file of one coder tag."""
    return os.path.join(fulltext_dir(), f"{safe_token(coder)}.json")


def legacy_decision_path(coder):
    """The pre-`Fulltext/` decision file (read-only fallback)."""
    return os.path.join(S.SCREENING, f"fulltext - {safe_token(coder)}.json")


def load_decisions(coder, strict=False):
    """The coder's decisions; *strict* raises instead of returning ``{}`` for
    an unreadable file, so a writer never replaces a file it could not read.

    The canonical ``Fulltext/<Tag>.json`` wins over a legacy
    ``Screening/fulltext - <Tag>.json`` per key; both are read so a project that
    has not migrated yet keeps its decisions.
    """
    if not coder or not str(coder).strip():
        return {}
    merged = {}
    for path in (legacy_decision_path(coder), decision_path(coder)):
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            if strict:
                raise ValueError(f"{os.path.basename(path)} cannot be read ({exc}); fix or move "
                                 "it before recording more decisions") from exc
            continue
        if not isinstance(data, dict):
            if strict:
                raise ValueError(f"{os.path.basename(path)} does not hold a JSON object")
            continue
        merged.update(data)
    return merged


def save_decisions(coder, decisions):
    folder = fulltext_dir()
    os.makedirs(folder, exist_ok=True)
    path = decision_path(coder)
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=S.TEMP)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(decisions, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.write("\n")
        atomic_replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path


def all_decisions():
    """{study_key: {coder: decision}} across every coder file (new + legacy)."""
    out = {}
    tags = []
    for path in glob.glob(os.path.join(fulltext_dir(), "*.json")):
        tags.append(os.path.basename(path)[: -len(".json")])
    for path in glob.glob(os.path.join(S.SCREENING, "fulltext - *.json")):
        base = os.path.basename(path)[: -len(".json")]
        tags.append(base[len("fulltext - "):])
    for coder in dict.fromkeys(tags):          # one pass per tag (both files merge)
        for key, dec in load_decisions(coder).items():
            if isinstance(dec, dict):
                out.setdefault(str(key), {})[coder] = dec
    return out


def save_decision(coder, key, exclude, criterion, note):
    """Store one coder's decision and refresh the agreed list."""
    coder = str(coder or "").strip()
    key = str(key or "").strip()
    if not coder:
        raise ValueError("coder name is required")
    if not key:
        raise ValueError("study key is required")
    with _DECISIONS_LOCK:
        decisions = load_decisions(coder, strict=True)
        _store_decision(decisions, key, exclude, criterion, note)
        save_decisions(coder, decisions)
        return recompute_agreed()


def _store_decision(decisions, key, exclude, criterion, note):
    if exclude:
        crit = str(criterion or "other").strip() or "other"
        decisions[key] = {
            "exclude": True,
            "criterion": crit,
            "criterion_label": _CRIT_LABEL.get(crit, crit),
            "note": str(note or "").strip(),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
    else:
        decisions[key] = {
            "exclude": False, "criterion": "", "criterion_label": "",
            "note": str(note or "").strip(),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }


def compute_agreed():
    """Studies excluded by >= AGREE_MIN distinct coders (majority criterion)."""
    per = all_decisions()
    agreed = {}
    for key, by_coder in per.items():
        excl = {c: d for c, d in by_coder.items() if d.get("exclude")}
        if len(excl) < AGREE_MIN:
            continue
        counts = {}
        for d in excl.values():
            counts[d.get("criterion") or "other"] = counts.get(d.get("criterion") or "other", 0) + 1
        crit = max(counts, key=counts.get)
        notes = [d.get("note") for d in excl.values() if d.get("note")]
        agreed[key] = {
            "criterion": crit,
            "criterion_label": _CRIT_LABEL.get(crit, crit),
            "coders": sorted(excl.keys()),
            "notes": notes,
            "n_exclude": len(excl),
        }
    return agreed


def agreed_path():
    return os.path.join(S.SCREENING, "fulltext_agreed.json")


def load_agreed():
    path = agreed_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        log(f"fulltext_agreed.json unreadable: {exc!r}")
        return {}
    return data if isinstance(data, dict) else {}


def save_agreed(agreed):
    os.makedirs(S.SCREENING, exist_ok=True)
    path = agreed_path()
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=S.TEMP)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(agreed, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.write("\n")
        atomic_replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path


def recompute_agreed(mark=True):
    """Recompute the agreed list and mark newly-agreed studies in the master."""
    with _DECISIONS_LOCK:
        agreed = compute_agreed()
        previous = load_agreed()
        now = datetime.now().isoformat(timespec="seconds")
        merged = {}
        for key, info in agreed.items():
            prior = previous.get(key) or {}
            merged[key] = dict(info)
            merged[key]["marked_at"] = prior.get("marked_at") or now
            merged[key]["master"] = prior.get("master") or ""
        save_agreed(merged)
    newly = [k for k in merged if not (previous.get(k) or {}).get("master")]
    if mark and newly:
        start_marking(newly)
    return merged


def start_marking(keys):
    with _MARK_LOCK:
        if _MARK_STATE["running"]:
            return False
        _MARK_STATE.update(running=True, error="", finished=None, marked=[])
    threading.Thread(target=_mark_worker, args=(list(keys),), daemon=True).start()
    return True


def mark_status():
    return {k: v for k, v in _MARK_STATE.items()}


# ---------------------------------------------------------------- master workbook
def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _norm_doi(s):
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", str(s or "").strip().lower())


def _extraction_info(folder=None):
    info = {}
    for path in glob.glob(os.path.join(folder or S.EXTRACTION, "*.json")):
        key = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        rec = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
        if isinstance(rec, dict):
            info[key] = (_norm_doi(rec.get("DOI")), _norm(rec.get("Title")))
    return info


def master_mapping(master_path=None, extraction_dir=None):
    """{study_key: (row_index)} into a master workbook (DOI, then filename, then title).

    *master_path*/*extraction_dir* default to the app's project (``S.ROOT``); the
    compiler (``Scripts/compile_master.py``) passes its own base and
    ``Extraction`` folder, so a compile never depends on the app being
    configured for the same root.  When the workbook is not readable (a blinded
    coder's mirror does not get it) the decision-free records file is read
    instead: same rows, same row numbers, no verdict columns - the mapping only
    needs DOI/title/filename, so it is unchanged.
    """
    import screening_lib as SL      # the workbook/records table reader

    master = master_path or os.path.join(S.ROOT, "master_dataset.xlsx")
    if not os.path.exists(master):
        records = os.path.join(S.ROOT, SL.RECORDS_REL)
        if not os.path.exists(records):
            return {}
        master = records
    key = (os.path.abspath(master), int(os.path.getmtime(master)))
    if _MAP_CACHE["map"] is not None and _MAP_CACHE["mtime"] == key:
        return _MAP_CACHE["map"]
    info = _extraction_info(extraction_dir)
    by_doi, by_title, by_fn = {}, {}, {}
    _title, header_row, head, it, close = SL._table_source(master)
    try:
        H = {h: i for i, h in enumerate(head)}

        def cell(row, name):
            i = H.get(name)
            if i is None or i >= len(row) or row[i] is None:
                return ""
            return row[i]

        for idx, row in enumerate(it, start=header_row + 2):
            doi = _norm_doi(cell(row, "doi"))
            title = _norm(cell(row, "title"))
            fn = str(cell(row, "PDF_Filename") or "").strip()
            stem = os.path.splitext(os.path.basename(fn))[0] if fn else ""
            if doi:
                by_doi.setdefault(doi, idx)
            if title:
                by_title.setdefault(title, idx)
            if stem:
                by_fn.setdefault(stem, idx)
    finally:
        try:
            close()
        except Exception:  # noqa: BLE001 - closing a reader never fails the mapping
            pass
    mapping = {}
    for study, (doi, title) in info.items():
        row = None
        if doi and doi in by_doi:
            row = by_doi[doi]
        elif study in by_fn:
            row = by_fn[study]
        elif title and title in by_title:
            row = by_title[title]
        if row is not None:
            mapping[study] = row
    _MAP_CACHE.update(mtime=key, map=mapping, info=info)
    return mapping


def _mark_worker(keys):
    state = _MARK_STATE
    try:
        _mark_master(keys)
    except Exception as exc:  # noqa: BLE001
        state.update(error=str(exc))
        log(f"fulltext mark failed: {exc}")
    finally:
        state.update(running=False, finished=datetime.now().isoformat(timespec="seconds"))


def master_is_derived():
    """Whether this project builds its master from decision files (plan §11).

    True when ``Project/master_base.xlsx`` exists, or when the compiler's
    decision-free records file does: a blinded coder's mirror does not receive
    the base (it carries the curated stages), only the records file, and the
    master is derived there just the same.
    """
    try:
        import compile_master

        if compile_master.enabled(S.ROOT):
            return True
        return os.path.isfile(os.path.join(S.ROOT, compile_master.RECORDS_REL))
    except Exception:  # noqa: BLE001 - a legacy project must keep marking the workbook
        return False


def request_compile(reason="fulltext"):
    """Ask the app to rebuild the derived master (debounced, background)."""
    try:
        import master_job

        return master_job.schedule(reason)
    except Exception as exc:  # noqa: BLE001 - the decision is saved either way
        log(f"fulltext: master recompile not scheduled: {exc}")
        return False


def _mark_master(keys):
    import openpyxl

    master = os.path.join(S.ROOT, "master_dataset.xlsx")
    agreed = load_agreed()
    marked, missing = [], []
    if master_is_derived():
        # The derived master is rebuilt from the decision files; there is no
        # cell to write here (and writing one would be overwritten anyway).
        # Checked before the workbook: a coder's mirror does not receive it.
        with _DECISIONS_LOCK:
            cur = load_agreed()
            now = datetime.now().isoformat(timespec="seconds")
            for key in keys:
                if key in cur:
                    cur[key]["master"] = "derived"
                    cur[key]["marked_at"] = now
            save_agreed(cur)
        _MARK_STATE.update(marked=[], derived=True,
                           note="the master is derived from the decision files; recompile scheduled")
        request_compile("fulltext")
        log(f"fulltext decisions recorded for {len(keys)} study/studies (derived master)")
        return
    mapping = master_mapping()
    if not os.path.exists(master):
        _MARK_STATE.update(error="master_dataset.xlsx not found")
        return
    backup_dir = os.path.join(S.ROOT, "archive", "coding_app_ui_backups",
                              "fulltext_merge_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(backup_dir, exist_ok=True)
    shutil.copy2(master, os.path.join(backup_dir, os.path.basename(master)))

    # The screening merge rewrites the same workbook: hold the shared lock so
    # neither replaces the file with a copy that lacks the other's changes.
    from screening_lib import MASTER_WRITE_LOCK
    with MASTER_WRITE_LOCK:
        _mark_master_locked(openpyxl, master, keys, agreed, mapping, marked, missing)
    # record marking outcome on the agreed entries
    with _DECISIONS_LOCK:
        cur = load_agreed()
        now = datetime.now().isoformat(timespec="seconds")
        for key in keys:
            if key in cur:
                cur[key]["master"] = "marked" if key in marked else "not_found"
                cur[key]["marked_at"] = now
        save_agreed(cur)
    _MAP_CACHE.update(mtime=None, map=None)
    _MARK_STATE.update(marked=marked)
    log(f"fulltext merge marked={len(marked)} missing={len(missing)}")


def _mark_master_locked(openpyxl, master, keys, agreed, mapping, marked, missing):
    wb = openpyxl.load_workbook(master)
    try:
        ws = wb.worksheets[0]
        head = [str(c.value).strip() if c.value is not None else "" for c in ws[1]]
        # 1-based column indexes, matched case-/space-insensitively
        H = {}
        for i, h in enumerate(head):
            H.setdefault(_norm(h), i + 1)

        def setcell(row, name, value):
            col = H.get(_norm(name))
            if col is not None:
                ws.cell(row=row, column=col, value=value)

        for key in keys:
            info = agreed.get(key) or {}
            row = mapping.get(key)
            if not row:
                missing.append(key)
                continue
            setcell(row, "Fulltext_Verdict", "Exclude")
            setcell(row, "Fulltext_Exclusion_Criterion", info.get("criterion_label") or "Coder agreement")
            note = "; ".join(info.get("notes") or []) or f"Excluded by coders: {', '.join(info.get('coders') or [])}"
            setcell(row, "Fulltext_Reason", note)
            if _norm("Effective_Verdict") in H:
                setcell(row, "Effective_Verdict", "Exclude")
            marked.append(key)
        tmp = os.path.join(S.TEMP, "master_dataset.fulltext.xlsx")
        wb.save(tmp)
        wb.close()
        atomic_replace(tmp, master)
    except Exception:
        wb.close()
        raise
