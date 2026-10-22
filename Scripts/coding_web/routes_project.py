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
    scheme = _read_json(os.path.join(path, "Project", "scheme.json"))
    return {
        "path": path,
        "name": os.path.basename(path) or path,
        "scheme": str(scheme.get("name") or ""),
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


def _project_list(current=None):
    """The project list: one entry per folder."""
    return [_project_entry(path) for path in _recent_paths(current)]


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


def _manifest(name):
    """The manifest of a new project."""
    slug = _slug(name)
    return {
        "id": slug,
        "name": slug,
        "label": name,
        "description": "",
        "created": project_context.now(),
        "scheme": project_context.SCHEME_FILENAME,
        "criteria": project_context.CRITERIA_FILENAME,
    }


def _create_project(root, name, template):
    """Create the skeleton and the three project documents.

    Everything is written *inside* ``root``; nothing outside it is touched.
    """
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

    os.makedirs(root, exist_ok=True)
    for folder in SKELETON_DIRS:
        os.makedirs(os.path.join(root, folder), exist_ok=True)
    paths = project_context.project_paths(root)
    os.makedirs(paths["project_dir"], exist_ok=True)
    os.makedirs(paths["bundles"], exist_ok=True)
    os.makedirs(paths["history"], exist_ok=True)

    project_context.write_scheme(normalised, root)
    project_context.write_criteria(criteria_lib.default_criteria(), root)
    project_context.write_manifest(_manifest(name), root)
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
    if template not in TEMPLATES:
        return _fail("the template must be 'blank' or 'copy'", 400)

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
        _create_project(root, name, template)
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
