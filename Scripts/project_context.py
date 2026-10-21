"""Which project is active, and what scheme and criteria it carries.

A **project** is a folder: the same layout the campaign already uses
(``Papers/``, ``Text/``, ``Extraction/``, ``Codings/``, ``Screening/``, ...) plus
a ``Project/`` folder that holds the project's own

* ``project.json``  - the manifest (id, name, created, settings),
* ``scheme.json``   - the coding scheme (see :mod:`scheme_lib`),
* ``criteria.json`` - the abstract and full-text exclusion criteria.

This module is the single place that answers "what is the active project?",
so the desktop app, the analysis pipeline and the agent tooling can never
disagree.  It is stdlib-only and lives one level up (``Scripts/``).

The project root is resolved in this order:

1. an explicit override (:func:`set_root`, used by the app when it re-roots),
2. the ``CODING_STUDIO_ROOT`` environment variable,
3. ``codingstudio.config.json`` (the app's saved folder),
4. the repository root (the folder that holds ``Scripts/``).

The scheme falls back to the legacy v3.1 ``schema.json`` when a project has no
``Project/scheme.json`` yet, so a project that has not been converted keeps
working unchanged.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime

import scheme_lib as SL

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

#: The folder inside a project that holds its scheme and criteria.
PROJECT_DIRNAME = "Project"
MANIFEST_FILENAME = "project.json"
SCHEME_FILENAME = "scheme.json"
CRITERIA_FILENAME = "criteria.json"
BUNDLES_DIRNAME = "bundles"

#: The app's saved-folder config (same file the desktop app uses).
CONFIG_NAME = "codingstudio.config.json"

#: The legacy (campaign) schema, used when a project has no scheme yet.
LEGACY_SCHEMA_RELATIVE = os.path.join("Scripts", "coding_web", "schema.json")

_ROOT_OVERRIDE = None

_CACHE = {"scheme": {}, "criteria": {}, "manifest": {}}


# --------------------------------------------------------------------------- #
# Root resolution
# --------------------------------------------------------------------------- #

def _config_path():
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(os.path.abspath(sys.executable)), CONFIG_NAME)
    return os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()),
                        "CodingStudio", CONFIG_NAME)


def _config_root():
    try:
        with open(_config_path(), encoding="utf-8") as fh:
            cfg = json.load(fh)
        if isinstance(cfg, dict):
            return cfg.get("root") or ""
    except Exception:
        pass
    return ""


def default_root():
    """The folder a project lives in when nothing says otherwise."""
    return REPO


def active_root():
    """The active project root."""
    if _ROOT_OVERRIDE:
        return os.path.abspath(_ROOT_OVERRIDE)
    env = os.environ.get("CODING_STUDIO_ROOT")
    if env:
        return os.path.abspath(env)
    cfg = _config_root()
    if cfg:
        return os.path.abspath(cfg)
    return default_root()


def set_root(path):
    """Point the project context at ``path``; returns the resolved root."""
    global _ROOT_OVERRIDE
    _ROOT_OVERRIDE = os.path.abspath(path) if path else None
    invalidate()
    return active_root()


def project_dir(root=None):
    """The ``Project/`` folder of the active project."""
    return os.path.join(root or active_root(), PROJECT_DIRNAME)


def project_paths(root=None):
    """Every path the project layer owns."""
    base = root or active_root()
    pdir = os.path.join(base, PROJECT_DIRNAME)
    return {
        "root": base,
        "project_dir": pdir,
        "manifest": os.path.join(pdir, MANIFEST_FILENAME),
        "scheme": os.path.join(pdir, SCHEME_FILENAME),
        "criteria": os.path.join(pdir, CRITERIA_FILENAME),
        "bundles": os.path.join(pdir, BUNDLES_DIRNAME),
        "history": os.path.join(pdir, "history"),
    }


def legacy_schema_path(root=None):
    """The legacy v3.1 schema of the active project (if it has one)."""
    return os.path.join(root or active_root(), LEGACY_SCHEMA_RELATIVE)


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _cached(bucket, path, loader):
    """Read a JSON document, re-reading it only when the file changed."""
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        stamp = None
    entry = _CACHE[bucket]
    if entry.get("path") == path and entry.get("stamp") == stamp:
        return entry.get("value")
    value = loader(path)
    _CACHE[bucket] = {"path": path, "stamp": stamp, "value": value}
    return value


def manifest(root=None):
    """The project manifest (``{}`` when the project has none yet)."""
    paths = project_paths(root)
    return _cached("manifest", paths["manifest"],
                   lambda p: _read_json(p) or {}) or {}


def raw_scheme(root=None):
    """The scheme document as stored, without normalisation."""
    paths = project_paths(root)
    return _cached("scheme", paths["scheme"], lambda p: _read_json(p)) or {}


def scheme(root=None):
    """The active project scheme, normalised.

    Falls back to the legacy v3.1 ``schema.json`` (converted on the fly) when
    the project has no ``Project/scheme.json`` yet.
    """
    paths = project_paths(root)
    raw = _cached("scheme", paths["scheme"], lambda p: _read_json(p))
    if isinstance(raw, dict) and raw:
        return SL.normalise_scheme(raw)
    legacy = _read_json(legacy_schema_path(root))
    if isinstance(legacy, dict) and legacy:
        import scheme_legacy
        return SL.normalise_scheme(scheme_legacy.convert(legacy))
    return SL.normalise_scheme({})


def criteria(root=None):
    """The active project's criteria, normalised (see ``criteria_lib``)."""
    paths = project_paths(root)
    raw = _cached("criteria", paths["criteria"], lambda p: _read_json(p))
    try:
        import criteria_lib
    except Exception:  # pragma: no cover - criteria_lib is optional
        return {"version": 1, "abstract": [], "fulltext": []}
    return criteria_lib.normalise_criteria(raw or {})


# --------------------------------------------------------------------------- #
# Convenience
# --------------------------------------------------------------------------- #

def role_map(root=None, scope=None):
    """``{field_name: role}`` for the active scheme."""
    return SL.role_map(scheme(root), scope)


def value_by_role(values, role, root=None, scope=None):
    """The value of the field carrying ``role`` inside ``values``."""
    return SL.value_by_role(values, role_map(root, scope), role)


def blocks(root=None):
    """The derived block list of the active scheme."""
    return SL.derive_blocks(scheme(root))


def vocabularies(root=None):
    """The active scheme's controlled vocabularies."""
    return scheme(root).get("vocabularies") or {}


def vocabulary(name, root=None, default=None):
    """One controlled vocabulary of the active scheme."""
    value = vocabularies(root).get(name)
    return default if value is None else value


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #

def _atomic_write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    invalidate()
    return path


def write_manifest(doc, root=None):
    """Write ``Project/project.json``."""
    return _atomic_write(project_paths(root)["manifest"], doc)


def write_scheme(doc, root=None):
    """Write ``Project/scheme.json`` after validating it."""
    normalised = SL.normalise_scheme(doc)
    problems = [p for p in SL.validate_scheme(normalised) if p["level"] == "error"]
    if problems:
        raise ValueError("the scheme has structural errors: " +
                         "; ".join(p["message"] for p in problems))
    return _atomic_write(project_paths(root)["scheme"], normalised)


def write_criteria(doc, root=None):
    """Write ``Project/criteria.json`` after normalising it."""
    import criteria_lib
    normalised = criteria_lib.normalise_criteria(doc)
    return _atomic_write(project_paths(root)["criteria"], normalised)


def invalidate():
    """Drop the cached project documents (call after writing or re-rooting)."""
    for bucket in _CACHE.values():
        bucket.clear()


def now():
    """An ISO timestamp, for manifests and history entries."""
    return datetime.now().isoformat(timespec="seconds")
