"""Project routes: the recent-projects list and creating a new project.

A **project** is a folder: the campaign layout (``Papers/``, ``Text/``,
``Extraction/``, ``Codings/``, ``Screening/``, ``Datasets/``, ``Results/``,
``temp/``) plus a ``Project/`` folder that holds the project's own manifest,
scheme and criteria (see ``Scripts/project_context.py``).

The Home page needs two things from the back end:

* the **recent projects** - the folders that were opened lately, most recent
  first, with the active project always first;
* **create a project** - a new folder with the full skeleton, a manifest, a
  scheme and the seeded criteria.

The recent list lives in the app config (``codingstudio.config.json``, key
``recent_projects``: absolute paths, most recent first, at most 12); the
helpers that read and write it live in ``studio_state``.

Rules of the road:

* creating a project never touches anything outside the new folder, and it
  refuses to write into a folder that already holds something;
* every failure is answered with a JSON body - never an HTML traceback.
"""

import json
import os
import re
import sys

from flask import Blueprint, jsonify, request
from werkzeug.exceptions import HTTPException

from studio_state import (S, log, recent_project_opened, recent_projects,
                           record_recent_project)

# The shared project layer lives one level up (Scripts/): ``project_context``
# answers "which project is active?", ``scheme_lib`` knows what a scheme is
# and ``criteria_lib`` carries the seeded criteria.
try:
    import criteria_lib  # noqa: E402
    import project_context  # noqa: E402
    import scheme_lib  # noqa: E402
except Exception:  # pragma: no cover - source layout fallback
    for _d in (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
               os.path.join(S.ROOT, "Scripts")):
        if _d and _d not in sys.path:
            sys.path.insert(0, _d)
    import criteria_lib  # noqa: E402
    import project_context  # noqa: E402
    import scheme_lib  # noqa: E402

bp = Blueprint("project", __name__)

# the AGENTS.md a new project carries (the Settings view edits it later)
try:
    import project_doc
except Exception:  # pragma: no cover - source layout fallback
    import sys as _sys

    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import project_doc  # noqa: E402

#: The folders a new project starts with (besides ``Project/``).
SKELETON_DIRS = (
    "Papers",
    "Text",
    "Extraction",
    "Codings",
    "Screening",
    "Datasets",
    "Results",
    "temp",
)

#: The longest project name that is accepted.
MAX_NAME_LENGTH = 80

#: The templates ``POST /api/project/new`` understands.
TEMPLATES = ("blank", "copy")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _body():
    """The request body as a dict (never raises)."""
    data = request.get_json(force=True, silent=True)
    return data if isinstance(data, dict) else {}


def _fail(message, status=400, **extra):
    """A JSON error body."""
    payload = {"ok": False, "error": message}
    payload.update(extra)
    return jsonify(payload), status


def _current_root():
    """The active project root (the app's data root)."""
    root = getattr(S, "ROOT", None) or project_context.active_root()
    return os.path.abspath(root)


def _read_json(path):
    """A JSON document, or ``{}`` when it is missing or broken."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _paper_count(root):
    """How many papers (``Papers/*.pdf``) the folder holds."""
    try:
        with os.scandir(os.path.join(root, "Papers")) as entries:
            return sum(1 for entry in entries
                       if entry.is_file() and entry.name.lower().endswith(".pdf"))
    except OSError:
        return 0


def _slug(text):
    """A filesystem- and id-safe slug of a project name."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower()).strip("-")
    return slug or "project"


def _project_entry(path):
    """One entry of the project list."""
    path = os.path.abspath(path)
    # the project's portable copy of its scheme names it; a project without
    # one still names its scheme id in the manifest
    scheme = _read_json(os.path.join(path, "Project", "scheme.json"))
    manifest = _read_json(os.path.join(path, "Project", "project.json"))
    return {
        "path": path,
        "name": os.path.basename(path) or path,
        "scheme": str(scheme.get("name") or manifest.get("scheme") or ""),
        "scheme_id": str(manifest.get("scheme") or ""),
        "label": str(scheme.get("label") or ""),
        "papers": _paper_count(path),
        "last_opened": recent_project_opened(path),
        "exists": os.path.exists(path),
    }


def _recent_paths(current=None):
    """The folders the list shows: the active project first, then the recents.

    The active project is always the first entry (the contract of
    ``GET /api/projects``); the recents follow, most recently opened first.
    A folder that was just recorded is the first of the recents.
    """
    paths = []
    if current:
        current = os.path.abspath(current)
        paths.append(current)
    known = {os.path.normcase(current)} if current else set()
    for path in recent_projects():
        key = os.path.normcase(path)
        if key in known:
            continue
        known.add(key)
        paths.append(path)
    return paths


def _online_entries():
    """The saved online projects as list rows (``kind: "online"``)."""
    try:
        from studio_state import online_connections
        from routes_online import _ACTIVE, _public
    except Exception:  # noqa: BLE001 - a build without the online layer
        return []
    active = _ACTIVE.get("id") if getattr(S, "online", False) else None
    out = []
    for conn in online_connections():
        pub = _public(conn) or {}
        out.append({
            "kind": "online",
            "id": pub.get("id"),
            "path": "online:" + str(pub.get("id") or ""),
            "name": pub.get("project_name") or pub.get("folder") or pub.get("project_id") or "Online project",
            "backend": pub.get("backend"),
            "login": pub.get("login") or "account",
            "host": pub.get("host"),
            "username": pub.get("username"),
            "folder": pub.get("folder") or "",
            "last_opened": pub.get("last_opened") or "",
            "current": pub.get("id") == active,
            "exists": True,
            "papers": None,
            "scheme": "", "scheme_id": "", "label": "",
        })
    return out


def _project_list(current=None):
    """The project list: the folders (``kind: "local"``) and the saved online
    projects (``kind: "online"``), most recently opened first.

    While an online project is open its mirror is the app's root; it is shown
    as the online row (marked current), never as a folder.
    """
    online = _online_entries()
    locals_ = []
    for path in _recent_paths(None if getattr(S, "online", False) else current):
        entry = _project_entry(path)
        entry["kind"] = "local"
        locals_.append(entry)
    if getattr(S, "online", False):
        cur = [e for e in online if e["current"]]
        rest = sorted(locals_ + [e for e in online if not e["current"]],
                      key=lambda e: str(e.get("last_opened") or ""), reverse=True)
        return cur + rest
    if not online:
        return locals_
    first, rest = locals_[:1], locals_[1:]
    # interleave by opening time, keeping the folders' own order among themselves
    merged, i = [], 0
    for entry in online:
        stamp = str(entry.get("last_opened") or "")
        while i < len(rest) and str(rest[i].get("last_opened") or "") >= stamp:
            merged.append(rest[i])
            i += 1
        merged.append(entry)
    return first + merged + rest[i:]


# --------------------------------------------------------------------------- #
# the recent projects
# --------------------------------------------------------------------------- #

@bp.get("/api/projects")
def api_projects():
    """The recent projects, most recently opened first.

    The active project is always the first entry.  The list comes from the
    app config (``recent_projects``), so it survives a restart.
    """
    current = _current_root()
    if getattr(S, "online", False):
        try:
            from routes_online import _ACTIVE
            if _ACTIVE.get("id"):
                current = "online:" + _ACTIVE["id"]
        except Exception:  # noqa: BLE001
            pass
    return jsonify({"current": current, "projects": _project_list(current)})


@bp.post("/api/project/recent")
def api_project_recent():
    """Record a folder as recently opened.

    The folder moves to the front of the recent list, duplicates are dropped
    and the list keeps at most 12 entries.  The answer carries the same list
    ``GET /api/projects`` serves: the active project first, then the recents
    (the folder just recorded is the first of them).
    """
    body = _body()
    path = str(body.get("path") or "").strip().strip('"')
    if not path:
        return _fail("a project folder is needed", 400)
    path = os.path.abspath(os.path.expanduser(path))
    if getattr(S, "online", False) and os.path.normcase(path) == os.path.normcase(S.ROOT or ""):
        # an online project's mirror is not a folder the user chose: its row
        # is the saved connection
        current = _current_root()
        return jsonify({"ok": True, "projects": _project_list(current)})
    try:
        record_recent_project(path)
    except OSError as exc:
        return _fail(f"the recent list could not be written: {exc}", 500)
    current = _current_root()
    return jsonify({"ok": True, "projects": _project_list(current)})


# --------------------------------------------------------------------------- #
# creating a project
# --------------------------------------------------------------------------- #

def _check_name(name):
    """Why the name cannot be used (``""`` when it is fine)."""
    if not name:
        return "the project needs a name"
    if len(name) > MAX_NAME_LENGTH:
        return (f"the project name is too long "
                f"({len(name)} characters, at most {MAX_NAME_LENGTH})")
    if "/" in name or "\\" in name:
        return "the project name must not contain a path separator"
    if ":" in name:
        return "the project name must not contain a colon"
    if name in (".", ".."):
        return "the project name must not be '.' or '..'"
    return ""


def _blank_scheme(name):
    """An empty scheme: no fields, one group called "Study", no metrics."""
    slug = _slug(name)
    return {
        "scheme_version": scheme_lib.SCHEME_VERSION,
        "id": slug,
        "name": slug,
        "label": name,
        "description": "",
        "groups": [{"name": "Study", "label": "Study", "description": "",
                    "show_if": None}],
        "fields": [],
        "metrics": {"enabled": False, "group": scheme_lib.DEFAULT_METRICS_GROUP,
                    "label": "Effect sizes", "fields": [], "layout": {}},
        "modules": {},
        "vocabularies": {},
        "kinds": [],
        "rules": {},
    }


def _copied_scheme():
    """The active project's scheme, as it is stored.

    A project that carries no ``Project/scheme.json`` yet falls back to the
    normalised scheme (the legacy v3.1 schema, converted on the fly), so
    "copy" always copies something.
    """
    raw = project_context.raw_scheme()
    if isinstance(raw, dict) and raw:
        return raw
    return project_context.scheme()


def _scheme_for(template, name):
    """The scheme a new project starts with."""
    if template == "copy":
        return _copied_scheme()
    return _blank_scheme(name)


def _manifest(name, scheme_id):
    """The manifest of a new project: it names its scheme by library id."""
    slug = _slug(name)
    return {
        "id": slug,
        "name": slug,
        "label": name,
        "description": "",
        "created": project_context.now(),
        "scheme": scheme_id,
        "criteria": scheme_id,
    }


def _create_project(root, name, template, scheme_id=""):
    """Create the skeleton, install the scheme and write the manifest.

    ``scheme_id`` names an installed scheme the project shares; otherwise the
    template decides: ``copy`` installs an independent copy of the active
    project's scheme and criteria, ``blank`` an empty scheme.  The project
    folder only receives its skeleton, its manifest and the portable copy of
    the scheme; the scheme itself goes to the app's library.
    """
    if scheme_id:
        if not project_context.scheme_installed(scheme_id):
            raise ValueError(f"the scheme '{scheme_id}' is not installed")
        sid = scheme_id
        normalised = project_context.scheme(scheme_id=sid)
    else:
        scheme = _scheme_for(template, name)
        try:
            normalised = scheme_lib.normalise_scheme(scheme)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"the scheme could not be read: {exc}") from exc
        problems = [p for p in scheme_lib.validate_scheme(normalised)
                    if p.get("level") == "error"]
        if problems:
            raise ValueError("the scheme has structural errors: " +
                             "; ".join(p.get("message", "") for p in problems))
        crit = (project_context.criteria() if template == "copy"
                else criteria_lib.default_criteria())
        # a fresh id: the new project must never edit another project's scheme
        sid = project_context.unique_scheme_id(_slug(name))
        normalised["id"] = sid
        if template == "blank":
            normalised["name"] = sid

    os.makedirs(root, exist_ok=True)
    for folder in SKELETON_DIRS:
        os.makedirs(os.path.join(root, folder), exist_ok=True)
    paths = project_context.project_paths(root)
    os.makedirs(paths["project_dir"], exist_ok=True)

    if not scheme_id:
        project_context.install_scheme(normalised, crit, scheme_id=sid,
                                       source=f"new project {root}")
    project_context.write_manifest(_manifest(name, sid), root)
    project_context.assign_scheme(sid, root)
    # the project's own AGENTS.md: what an agent (or a new colleague) reads
    # before touching the folder.  It is a normal file the Settings view
    # edits afterwards - created once, never rewritten behind the user.
    try:
        project_doc.ensure(root, name)
    except OSError as exc:  # a read-only parent must not fail the project
        log(f"the project's AGENTS.md could not be written: {exc}")
    return normalised


@bp.post("/api/project/new")
def api_project_new():
    """Create a new project.

    Body: ``{"name": "My study", "parent": "<folder>", "template": "blank"}``.

    ``parent`` defaults to the folder of the active project, ``template`` is
    ``"blank"`` (an empty scheme) or ``"copy"`` (the active project's
    scheme).  The new folder is never switched to - the front end asks for
    that separately.
    """
    body = _body()
    name = str(body.get("name") or "").strip()
    problem = _check_name(name)
    if problem:
        return _fail(problem, 400)

    template = str(body.get("template") or "blank").strip().lower()
    scheme_id = ""
    if template.startswith("scheme:"):
        # "scheme:<id>": the new project uses an installed scheme as it is
        scheme_id = template.split(":", 1)[1].strip()
        template = "scheme"
    scheme_id = str(body.get("scheme_id") or scheme_id).strip()
    if scheme_id:
        if not project_context.is_valid_scheme_id(scheme_id):
            return _fail(f"'{scheme_id}' is not a usable scheme id", 400)
        if not project_context.scheme_installed(scheme_id):
            return _fail(f"the scheme '{scheme_id}' is not installed", 400)
    elif template not in TEMPLATES:
        return _fail("the template must be 'blank', 'copy' or "
                     "'scheme:<installed id>'", 400)

    parent = str(body.get("parent") or "").strip().strip('"')
    if not parent:
        parent = os.path.dirname(_current_root())
    parent = os.path.abspath(os.path.expanduser(parent))
    if not os.path.isdir(parent):
        return _fail(f"the folder '{parent}' does not exist", 400)

    root = os.path.join(parent, name)
    if os.path.exists(root):
        if not os.path.isdir(root) or os.listdir(root):
            return _fail("there is already something there", 400)

    try:
        with project_context.WRITE_LOCK:
            _create_project(root, name, template, scheme_id)
    except ValueError as exc:
        return _fail(str(exc), 400)
    except OSError as exc:
        return _fail(f"the project could not be created: {exc}", 400)

    try:
        record_recent_project(root)
    except OSError as exc:
        log(f"the recent list could not be written: {exc}")

    return jsonify({"ok": True, "root": root, "project": _project_entry(root)})


# --------------------------------------------------------------------------- #
# the Home view's project metrics
# --------------------------------------------------------------------------- #

def _files_in(folder, *suffixes):
    """The file names in ``folder`` that end with one of ``suffixes``.

    In an online project the folder's manifest supplies the names, so a file
    that is not downloaded yet is still counted (audit §2 listings).
    """
    if S.online and S.store is not None:
        rel = S.relpath(folder)
        if rel is not None:
            return [e["name"] for e in S.listdir(rel)
                    if not e.get("is_dir")
                    and any(e["name"].lower().endswith(s) for s in suffixes)]
    out = []
    try:
        with os.scandir(folder) as it:
            for entry in it:
                if not entry.is_file():
                    continue
                low = entry.name.lower()
                if any(low.endswith(s) for s in suffixes):
                    out.append(entry.name)
    except OSError:
        return []
    return out


def _coder_names(names):
    """The coder initials from ``Codings/<Key> - <Coder>.json``."""
    coders = set()
    for name in names:
        stem = name[:-5] if name.lower().endswith(".json") else name
        head, sep, tail = stem.rpartition(" - ")
        if sep and tail:
            coders.add(tail.strip())
    return coders


def _harmonized_datasets(folder):
    """Datasets that hold a ``<Key> - harmonized.csv`` (or its ``.gz`` twin)."""
    count = 0
    for name in _dataset_dirs(folder):
        d = os.path.join(folder, name)
        if _files_in(d, "- harmonized.csv") or _files_in(d, "- harmonized.csv.gz"):
            count += 1
    return count


# The screening files are the only ones that must be read; a 24 MB decisions
# file would otherwise be parsed on every visit to the Home view.
_SCREEN_CACHE = {"stamp": None, "ids": 0}


def _screened_abstracts(folder):
    """How many abstracts carry at least one verdict, across every screener."""
    names = _files_in(folder, ".json")
    stamp = []
    for name in sorted(names):
        path = os.path.join(folder, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        stamp.append((name, st.st_size, int(st.st_mtime)))
    stamp = tuple(stamp)
    if _SCREEN_CACHE["stamp"] == stamp and _SCREEN_CACHE["stamp"] is not None:
        return _SCREEN_CACHE["ids"]

    ids = set()
    for name, _size, _mtime in stamp:
        path = os.path.join(folder, name)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            ids.update(str(k) for k in data.keys())
    _SCREEN_CACHE["stamp"] = stamp
    _SCREEN_CACHE["ids"] = len(ids)
    return len(ids)


def _reproduction_reports(folder):
    """Reproduction reports, without the parallel SB/MM reliability routes."""
    names = _files_in(folder, ".md")
    return sum(1 for n in names if not n[:-3].endswith((" - SB", " - MM")))


@bp.get("/api/project/summary")
def api_project_summary():
    """The Home view's metrics for the active project.

    Every number is a file count, so the answer arrives in milliseconds even
    while the paper list is still being read.  No project open means zeros,
    which the view shows as a dash.
    """
    root = _current_root()
    metrics = {
        "papers": 0, "texts": 0, "extractions": 0, "codings": 0, "coders": 0,
        "screened": 0, "datasets": 0, "harmonized": 0, "reproductions": 0,
    }
    if root and os.path.isdir(root):
        papers_dir = os.path.join(root, "Papers")
        text_dir = os.path.join(root, "Text")
        extraction_dir = os.path.join(root, "Extraction")
        codings_dir = os.path.join(root, "Codings")
        datasets_dir = os.path.join(root, "Datasets")
        repro_dir = os.path.join(root, "Reproduction")
        screening_dir = os.path.join(root, "Screening")

        coding_names = _files_in(codings_dir, ".json")
        metrics["papers"] = len(_files_in(papers_dir, ".pdf"))
        metrics["texts"] = len(_files_in(text_dir, ".txt"))
        metrics["extractions"] = len(_files_in(extraction_dir, ".json"))
        metrics["codings"] = len(coding_names)
        metrics["coders"] = len(_coder_names(coding_names))
        metrics["screened"] = _screened_abstracts(screening_dir)
        metrics["datasets"] = len(_dataset_dirs(datasets_dir))
        metrics["harmonized"] = _harmonized_datasets(datasets_dir)
        metrics["reproductions"] = _reproduction_reports(repro_dir)

    # A folder that holds no Papers/ is not a project yet (a fresh folder, or
    # the app started somewhere else): the Home view shows a dash, not zeroes.
    is_project = bool(root) and os.path.isdir(os.path.join(root, "Papers"))
    return jsonify({
        "ok": True,
        "project": {"path": root or "", "name": os.path.basename(root) if root else "",
                    "is_project": is_project},
        "metrics": metrics,
    })


def _dataset_dirs(folder):
    """The per-study folders in Datasets/ (``temp`` and ``_``-prefixed
    bookkeeping folders are not studies)."""
    if S.online and S.store is not None:
        rel = S.relpath(folder)
        if rel is not None:
            return [e["name"] for e in S.listdir(rel)
                    if e.get("is_dir") and e["name"].lower() != "temp"
                    and not e["name"].startswith("_")]
    try:
        with os.scandir(folder) as it:
            return [e.name for e in it
                    if e.is_dir() and e.name.lower() != "temp"
                    and not e.name.startswith("_")]
    except OSError:
        return []


# --------------------------------------------------------------------------- #
# errors: always JSON, never an HTML traceback
# --------------------------------------------------------------------------- #

@bp.errorhandler(Exception)
def api_project_error(exc):
    """Answer every failure with JSON - and keep the traceback in the log."""
    if isinstance(exc, HTTPException):
        if exc.code and exc.code >= 500:
            _log_traceback(exc)
        return jsonify({"ok": False, "error": exc.description or exc.name}), exc.code
    _log_traceback(exc)
    return jsonify({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}), 500


def _log_traceback(exc):
    """Write the traceback of a failed request to the app log (never raises)."""
    try:
        import traceback

        log("project request failed:\n" + "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:  # noqa: BLE001
        pass
