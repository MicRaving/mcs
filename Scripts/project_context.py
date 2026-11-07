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

The scheme itself lives in the app's scheme library (``<app dir>/Schemes/<id>/``,
see :func:`library_dirs`); the manifest names it, and the project keeps a
portable copy in ``Project/``.  :func:`scheme_source` documents the resolution
order - library, the project's copy, the legacy v3.1 ``schema.json`` converted
on the fly, nothing - so a project that has not been converted keeps working
unchanged.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import sys
import tempfile
import threading
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# The shared modules sit next to this file.  A caller that imports
# ``project_context`` from elsewhere (the desktop app lives in
# ``Scripts/coding_web/``) may not have ``Scripts/`` on ``sys.path`` yet, and
# the sibling imports below would fail.
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import scheme_lib as SL  # noqa: E402

#: The folder inside a project that holds its manifest.
PROJECT_DIRNAME = "Project"
MANIFEST_FILENAME = "project.json"
SCHEME_FILENAME = "scheme.json"
CRITERIA_FILENAME = "criteria.json"
BUNDLES_DIRNAME = "bundles"
#: The folder beside the CodingStudio app that holds every scheme.
SCHEMES_DIRNAME = "Schemes"
#: The manifest of one scheme (the project's own manifest is "project.json").
SCHEME_MANIFEST_FILENAME = "manifest.json"

#: The app's saved-folder config (same file the desktop app uses).
CONFIG_NAME = "codingstudio.config.json"

#: The legacy (campaign) schema, used when a project has no scheme yet.
LEGACY_SCHEMA_RELATIVE = os.path.join("Scripts", "coding_web", "schema.json")

_ROOT_OVERRIDE = None

#: path -> {stamp, value}, per bucket (see :func:`_cached`).
_CACHE = {"scheme": {}, "criteria": {}, "manifest": {}, "legacy": {}}


# --------------------------------------------------------------------------- #
# Root resolution
# --------------------------------------------------------------------------- #

def _config_path():
    """The app config, in the same places the desktop app writes it.

    Mirrors ``studio_state._config_candidates``: a frozen Windows copy keeps a
    portable file beside the exe, everything else (and a Program Files install,
    where the exe folder is read-only) uses the per-user folder of the
    platform.  Reading only the exe folder lost the saved project on macOS
    (the app bundle) and under Program Files.
    """
    candidates = []
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                                       CONFIG_NAME))
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", tempfile.gettempdir())
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config")
    candidates.append(os.path.join(base, "CodingStudio", CONFIG_NAME))
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[-1]


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
    """The project root: the folder that holds the data.

    The code's own folder is the project when it is one (a source checkout, a
    worktree).  A frozen build lives in ``<project>/CodingStudio/``, so the
    project is found by walking up — the app sits beside ``Papers/``.
    """
    if _looks_like_a_project(REPO):
        return REPO
    cur = REPO
    for _ in range(6):
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
        if _looks_like_a_project(cur):
            return cur
    if getattr(sys, "frozen", False):
        # An installed copy that is not inside a project must not keep data in
        # its own folder: that folder can be read-only (Program Files), sits
        # inside the bundle on macOS, and is replaced by every update.  It
        # starts in a per-user folder instead until a project is opened.
        return user_projects_dir()
    return REPO


def user_projects_dir():
    """The per-user starting folder of an installed copy (created on demand)."""
    home = os.path.expanduser("~")
    docs = os.path.join(home, "Documents")
    path = os.path.join(docs if os.path.isdir(docs) else home, "CodingStudio")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def _looks_like_a_project(path):
    """Whether ``path`` is a project folder (the campaign layout)."""
    if not path:
        return False
    for marker in ("Papers", "Text", "Project", "Extraction"):
        if os.path.isdir(os.path.join(path, marker)):
            return True
    return False


def active_root():
    """The active project root.

    Order: an explicit override, the environment variable, **the code's own
    root when that root is a project**, the app's saved folder, then the
    code's own root.

    The own-root rule comes before the saved folder on purpose: the saved
    folder is a *desktop-app* preference, and a script that lives inside a
    project must use that project, not whatever folder the app last had open.
    Without it a stale saved folder silently points the whole pipeline at
    another project.
    """
    if _ROOT_OVERRIDE:
        return os.path.abspath(_ROOT_OVERRIDE)
    env = os.environ.get("CODING_STUDIO_ROOT")
    if env:
        return os.path.abspath(env)
    if _looks_like_a_project(REPO):
        return REPO
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


def app_dir():
    """The folder the CodingStudio app itself lives in.

    A frozen build sits in its own folder (``<project>/CodingStudio/`` or an
    install folder such as ``Program Files``); the source run sits in
    ``Scripts/coding_web/``.  The schemes live beside the app, in a
    ``Schemes/`` subfolder - the app carries its vocabulary, the project
    carries the data.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    # the source run: the app is Scripts/coding_web/app.py, a subfolder of
    # this module's own folder
    candidate = os.path.join(HERE, "coding_web")
    if os.path.exists(os.path.join(candidate, "app.py")):
        return candidate
    return HERE


# --------------------------------------------------------------------------- #
# The scheme library
# --------------------------------------------------------------------------- #
#
# The library is not one folder but an ordered list of them, because an
# installed app usually cannot write beside its own exe (``Program Files``):
#
# 1. ``<app dir>/Schemes``           - when the app can write there (a source
#                                       run, a portable copy);
# 2. ``%LOCALAPPDATA%/CodingStudio/Schemes`` - the user's own library, where
#                                       imported and edited schemes land when
#                                       the app folder is read-only;
# 3. ``<app dir>/Schemes``           - read-only (an installed app);
# 4. ``<_MEIPASS>/Schemes``          - the copy PyInstaller bundles inside
#                                       ``_internal/``.
#
# A scheme id is looked up in that order, so an edited copy in the user's
# library shadows the read-only one the installer shipped.

#: What a scheme id may look like: it becomes a folder name, so it must never
#: carry a path separator, a drive, or a ``..``.
_SCHEME_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

_WRITABLE = {}


def is_valid_scheme_id(scheme_id):
    """Whether ``scheme_id`` is safe to use as a folder name."""
    sid = str(scheme_id or "")
    return bool(_SCHEME_ID_RE.match(sid)) and ".." not in sid


def _check_scheme_id(scheme_id):
    sid = str(scheme_id or "")
    if not is_valid_scheme_id(sid):
        raise ValueError(f"'{sid}' is not a usable scheme id (letters, digits, "
                         "'.', '_' and '-' only, no path)")
    return sid


def slug_scheme_id(text, fallback="scheme"):
    """A scheme id built from a name or label."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower()).strip("-")
    slug = slug[:60].strip("-")
    return slug if is_valid_scheme_id(slug) else fallback


def _can_write(folder):
    """Whether files can really be created in ``folder`` (probed once).

    ``os.access`` says yes for ``Program Files`` on Windows (it only checks the
    read-only attribute), so the probe creates and removes a file.
    """
    key = os.path.normcase(os.path.abspath(folder))
    if key in _WRITABLE:
        return _WRITABLE[key]
    probe_dir = folder
    while probe_dir and not os.path.isdir(probe_dir):
        parent = os.path.dirname(probe_dir)
        if parent == probe_dir:
            break
        probe_dir = parent
    ok = False
    try:
        fd, tmp = tempfile.mkstemp(prefix=".write-probe-", dir=probe_dir)
        os.close(fd)
        os.unlink(tmp)
        ok = True
    except OSError:
        ok = False
    _WRITABLE[key] = ok
    return ok


def _beside_schemes_dir():
    return os.path.join(app_dir(), SCHEMES_DIRNAME)


def user_schemes_dir():
    """The user's own scheme library (always writable)."""
    override = os.environ.get("CODING_STUDIO_SCHEMES")
    if override:
        return os.path.abspath(override)
    base = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "CodingStudio", SCHEMES_DIRNAME)


def _bundled_schemes_dir():
    bundle = getattr(sys, "_MEIPASS", "")
    return os.path.join(bundle, SCHEMES_DIRNAME) if bundle else ""


def writable_schemes_dir():
    """Where a new, imported or edited scheme is written."""
    beside = _beside_schemes_dir()
    if _can_write(beside):
        return beside
    return user_schemes_dir()


def library_dirs():
    """The library folders, in lookup order (only the ones that exist)."""
    beside = _beside_schemes_dir()
    order = []
    if _can_write(beside):
        order.append(beside)
    order.append(user_schemes_dir())
    order.append(beside)
    order.append(_bundled_schemes_dir())
    out, seen = [], set()
    for folder in order:
        if not folder:
            continue
        key = os.path.normcase(os.path.abspath(folder))
        if key in seen or not os.path.isdir(folder):
            continue
        seen.add(key)
        out.append(folder)
    return out


def schemes_dir():
    """The primary scheme folder: the one new schemes are written to."""
    return writable_schemes_dir()


def scheme_dir(scheme_id):
    """The folder of one scheme (the first library folder that has it).

    A scheme the library does not have yet resolves to the writable folder,
    so a writer creates it there.  An unsafe id raises ``ValueError``.
    """
    sid = str(scheme_id or "")
    if not sid:
        return schemes_dir()
    _check_scheme_id(sid)
    for folder in library_dirs():
        candidate = os.path.join(folder, sid)
        if os.path.isfile(os.path.join(candidate, SCHEME_FILENAME)):
            return candidate
    return os.path.join(writable_schemes_dir(), sid)


def scheme_path(scheme_id):
    """The scheme document of one scheme."""
    return os.path.join(scheme_dir(scheme_id), SCHEME_FILENAME)


def criteria_path(scheme_id):
    """The criteria document of one scheme."""
    return os.path.join(scheme_dir(scheme_id), CRITERIA_FILENAME)


def scheme_manifest_path(scheme_id):
    """The manifest of one scheme."""
    return os.path.join(scheme_dir(scheme_id), SCHEME_MANIFEST_FILENAME)


def scheme_installed(scheme_id):
    """Whether the library carries a scheme with this id."""
    if not is_valid_scheme_id(scheme_id):
        return False
    return os.path.isfile(scheme_path(scheme_id))


def schemes():
    """Every scheme the app carries (the first copy of an id wins).

    Each entry: ``{id, name, label, description, path, default, exists,
    writable}``.
    """
    out = []
    seen = set()
    writable = os.path.normcase(os.path.abspath(writable_schemes_dir()))
    for root in library_dirs():
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            folder = os.path.join(root, name)
            if name in seen or not os.path.isdir(folder) or not is_valid_scheme_id(name):
                continue
            if not os.path.isfile(os.path.join(folder, SCHEME_FILENAME)):
                continue
            seen.add(name)
            # the scheme's own manifest is "manifest.json"; "project.json" is
            # read only as a fallback for folders written by an older build
            info = (_read_json(os.path.join(folder, SCHEME_MANIFEST_FILENAME))
                    or _read_json(os.path.join(folder, MANIFEST_FILENAME)) or {})
            if not isinstance(info, dict):
                info = {}
            out.append({
                "id": name,
                "name": info.get("name") or name,
                "label": info.get("label") or info.get("name") or name,
                "description": info.get("description") or "",
                "path": folder,
                "default": bool(info.get("default")),
                "exists": True,
                "writable": os.path.normcase(os.path.abspath(root)) == writable,
            })
    return out


def default_scheme_id():
    """The scheme the app marks as its default (``""`` when none)."""
    for entry in schemes():
        if entry.get("default"):
            return entry["id"]
    return ""


def active_scheme_id(root=None):
    """The scheme id the active project's manifest names (``""`` when none).

    The app's default scheme is NOT used here: a project that names no scheme
    shows the scheme it carries (``Project/scheme.json``), or its legacy
    schema, or nothing - never a scheme it never chose.
    """
    named = str((manifest(root) or {}).get("scheme") or "")
    return named if is_valid_scheme_id(named) else ""


def project_paths(root=None):
    """Every path the project layer owns.

    The scheme and the criteria live beside the app, in
    ``<app dir>/Schemes/<id>/``; the project keeps its manifest (which names
    the scheme) and a portable COPY of the scheme and the criteria, so the
    folder can be opened on another machine.
    """
    base = root or active_root()
    pdir = os.path.join(base, PROJECT_DIRNAME)
    return {
        "root": base,
        "project_dir": pdir,
        "manifest": os.path.join(pdir, MANIFEST_FILENAME),
        "scheme": os.path.join(pdir, SCHEME_FILENAME),
        "criteria": os.path.join(pdir, CRITERIA_FILENAME),
        "history": os.path.join(pdir, "history"),
    }


def legacy_schema_path(root=None):
    """The legacy v3.1 schema of the active project (if it has one)."""
    return os.path.join(root or active_root(), LEGACY_SCHEMA_RELATIVE)


def is_legacy_schema(doc):
    """Whether ``doc`` is a legacy v3.1 ``schema.json`` (not a scheme)."""
    return isinstance(doc, dict) and (
        "common_fields" in doc or "dv_fields" in doc) and "fields" not in doc


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _stamp(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _cached(bucket, path, loader):
    """Read a JSON document, re-reading it only when the file changed.

    Each bucket keeps one entry per path, so alternating between two
    projects (or two schemes) does not thrash the cache.
    """
    stamp = _stamp(path)
    entries = _CACHE.setdefault(bucket, {})
    entry = entries.get(path)
    if entry is not None and entry.get("stamp") == stamp:
        return entry.get("value")
    value = loader(path)
    entries[path] = {"stamp": stamp, "value": value}
    return value


def manifest(root=None):
    """The project manifest (``{}`` when the project has none yet)."""
    paths = project_paths(root)
    doc = _cached("manifest", paths["manifest"], lambda p: _read_json(p) or {})
    return doc if isinstance(doc, dict) else {}


def scheme_source(root=None):
    """Where the active project's scheme comes from.

    Resolution order:

    1. the scheme id the manifest names, from the app's library;
    2. the copy the project carries (``Project/scheme.json``) - and when the
       manifest names an id the library lacks, the copy is installed into
       the library first, so a project opened on another machine brings its
       scheme along;
    3. a legacy v3.1 ``Scripts/coding_web/schema.json`` inside the project,
       converted on the fly;
    4. nothing: an empty scheme, with a message saying so.

    Returns ``{kind, scheme_id, path, message, named}``; ``kind`` is
    ``library``, ``project``, ``legacy`` or ``empty``.
    """
    base = os.path.abspath(root or active_root())
    paths = project_paths(base)
    raw_named = str((manifest(base) or {}).get("scheme") or "")
    sid = raw_named if is_valid_scheme_id(raw_named) else ""
    notes = []
    if raw_named and not sid:
        notes.append(f"The project names the scheme '{raw_named}', which is "
                     "not a usable scheme id.")

    if sid and scheme_installed(sid):
        path = scheme_path(sid)
        return {"kind": "library", "scheme_id": sid, "path": path,
                "named": raw_named,
                "message": f"The project uses the scheme '{sid}' from the "
                           f"app's scheme library ({os.path.dirname(path)})."}

    if os.path.isfile(paths["scheme"]):
        if sid:
            try:
                install_from_project(sid, base)
            except (OSError, ValueError) as exc:
                notes.append(f"The project's copy could not be installed into "
                             f"the library: {exc}")
            else:
                path = scheme_path(sid)
                return {"kind": "library", "scheme_id": sid, "path": path,
                        "named": raw_named, "installed_from_project": True,
                        "message": f"The scheme '{sid}' was missing from the "
                                   "app's library and was installed from the "
                                   "copy this project carries."}
        notes.append("The scheme is read from the copy this project carries "
                     f"({paths['scheme']}).")
        return {"kind": "project", "scheme_id": sid, "path": paths["scheme"],
                "named": raw_named, "message": " ".join(notes)}

    if sid:
        notes.append(f"The project names the scheme '{sid}', but neither the "
                     "app's library nor the project carries it.")
    legacy = legacy_schema_path(base)
    if os.path.isfile(legacy):
        notes.append("The scheme is converted from the project's legacy v3.1 "
                     f"schema ({legacy}). Saving it installs it in the library "
                     "and assigns it to the project.")
        return {"kind": "legacy", "scheme_id": "", "path": legacy,
                "named": raw_named, "message": " ".join(notes)}

    notes.append("This project has no scheme yet. Load one from a file or "
                 "pick an installed scheme.")
    return {"kind": "empty", "scheme_id": "", "path": "", "named": raw_named,
            "message": " ".join(notes)}


def _legacy_converted(path):
    doc = _read_json(path)
    if not isinstance(doc, dict) or not doc:
        return None
    import scheme_legacy
    return scheme_legacy.convert(doc)


def raw_scheme(root=None, scheme_id=None):
    """The scheme document as stored, without normalisation.

    A legacy project yields the converted (not yet normalised) document.
    """
    if scheme_id:
        if not scheme_installed(scheme_id):
            return {}
        doc = _cached("scheme", scheme_path(scheme_id), _read_json)
        return copy.deepcopy(doc) if isinstance(doc, dict) else {}
    src = scheme_source(root)
    if src["kind"] in ("library", "project"):
        doc = _cached("scheme", src["path"], _read_json)
    elif src["kind"] == "legacy":
        doc = _cached("legacy", src["path"], _legacy_converted)
    else:
        doc = None
    return copy.deepcopy(doc) if isinstance(doc, dict) else {}


def scheme(root=None, scheme_id=None):
    """The active project's scheme, normalised (see :func:`scheme_source`).

    The answer is a fresh copy: a caller may edit it without touching the
    cache.
    """
    raw = raw_scheme(root, scheme_id)
    if raw and is_legacy_schema(raw):
        import scheme_legacy
        raw = scheme_legacy.convert(raw)
    return SL.normalise_scheme(raw or {})


def criteria(root=None, scheme_id=None):
    """The active scheme's criteria, normalised (see ``criteria_lib``).

    Same order as the scheme: the library's criteria, then the project's own
    copy; a legacy project gets the campaign's seeded criteria (the legacy
    schema IS the campaign's).
    """
    try:
        import criteria_lib
    except Exception:  # pragma: no cover - criteria_lib is optional
        return {"version": 1, "abstract": [], "fulltext": []}
    raw = None
    if scheme_id:
        if is_valid_scheme_id(scheme_id):
            raw = _cached("criteria", criteria_path(scheme_id), _read_json)
        return criteria_lib.normalise_criteria(copy.deepcopy(raw) or {})
    base = os.path.abspath(root or active_root())
    src = scheme_source(base)
    if src["kind"] == "library":
        raw = _cached("criteria", criteria_path(src["scheme_id"]), _read_json)
    if not raw:
        raw = _cached("criteria", project_paths(base)["criteria"], _read_json)
    if not raw and src["kind"] == "legacy":
        return criteria_lib.default_criteria()
    return criteria_lib.normalise_criteria(copy.deepcopy(raw) or {})


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

#: Serialises every read-modify-write of a scheme, its criteria or a project
#: manifest.  The app serves requests on several threads; two field edits
#: that both read the scheme, change it and write it back would otherwise
#: lose one of the edits.
WRITE_LOCK = threading.RLock()


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


#: How many scheme revisions the history keeps.
HISTORY_LIMIT = 20


def _snapshot(path, folder):
    """Keep the previous version of a document under ``folder``.

    The stamp carries microseconds, so two saves within one second keep two
    snapshots instead of the second overwriting the first.
    """
    if not os.path.exists(path):
        return ""
    stem = os.path.basename(path).replace(".json", "")
    try:
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = os.path.join(folder, stem + "-" + stamp + ".json")
        with open(path, encoding="utf-8") as src, open(target, "w", encoding="utf-8") as dst:
            dst.write(src.read())
        # keep the history short; "<stem>-" so "scheme" never prunes the
        # snapshots of a document whose name merely starts with "scheme"
        entries = sorted(
            (os.path.join(folder, name) for name in os.listdir(folder)
             if name.startswith(stem + "-") and name.endswith(".json")),
            key=lambda p: os.path.getmtime(p), reverse=True)
        for stale in entries[HISTORY_LIMIT:]:
            try:
                os.unlink(stale)
            except OSError:
                pass
        return target
    except OSError:
        return ""


def _writable_scheme_dir(scheme_id):
    """The scheme's folder in the writable library, copying it there first
    when the only copy is read-only (an installed app)."""
    sid = _check_scheme_id(scheme_id)
    current = scheme_dir(sid)
    target = os.path.join(writable_schemes_dir(), sid)
    if os.path.normcase(os.path.abspath(current)) == os.path.normcase(os.path.abspath(target)):
        return target
    if os.path.isdir(current) and not os.path.isdir(target):
        shutil.copytree(current, target, ignore=shutil.ignore_patterns("history"))
    os.makedirs(target, exist_ok=True)
    return target


def write_manifest(doc, root=None):
    """Write ``Project/project.json``."""
    return _atomic_write(project_paths(root)["manifest"], doc)


def _validated(doc):
    normalised = SL.normalise_scheme(doc)
    problems = [p for p in SL.validate_scheme(normalised) if p["level"] == "error"]
    if problems:
        raise ValueError("the scheme has structural errors: " +
                         "; ".join(p["message"] for p in problems))
    return normalised


def _mirror_to_project(scheme_id, root=None):
    """Copy the library's scheme and criteria into the project's
    ``Project/`` folder, when the project uses that scheme.

    The copy makes the project portable: opened on a machine whose library
    lacks the scheme, the project installs it from the copy.
    """
    base = os.path.abspath(root or active_root())
    if not scheme_id or active_scheme_id(base) != scheme_id:
        return []
    paths = project_paths(base)
    written = []
    with WRITE_LOCK:
        for src, dst in ((scheme_path(scheme_id), paths["scheme"]),
                         (criteria_path(scheme_id), paths["criteria"])):
            doc = _read_json(src)
            if isinstance(doc, dict):
                written.append(_atomic_write(dst, doc))
    return written


def write_scheme(doc, scheme_id=None, root=None):
    """Write a scheme (after validating it) into ``Schemes/<id>/scheme.json``.

    The previous version is kept under the scheme's ``history/`` so a scheme
    edit can be undone and the history records how the scheme evolved.  When
    the project uses the scheme, its ``Project/`` copy is refreshed too.
    """
    sid = str(scheme_id or active_scheme_id(root) or "")
    if not sid:
        raise ValueError("no scheme to write: name one")
    normalised = _validated(doc)
    with WRITE_LOCK:
        folder = _writable_scheme_dir(sid)
        path = os.path.join(folder, SCHEME_FILENAME)
        _snapshot(path, os.path.join(folder, "history"))
        _atomic_write(path, normalised)
        _mirror_to_project(sid, root)
    return path


def write_scheme_manifest(doc, scheme_id=None):
    """Write ``Schemes/<id>/manifest.json``."""
    sid = str(scheme_id or "")
    if not sid:
        raise ValueError("no scheme to write: name one")
    with WRITE_LOCK:
        folder = _writable_scheme_dir(sid)
        return _atomic_write(os.path.join(folder, SCHEME_MANIFEST_FILENAME), doc)


def write_criteria(doc, scheme_id=None, root=None):
    """Write a scheme's criteria (after normalising them)."""
    import criteria_lib
    sid = str(scheme_id or active_scheme_id(root) or "")
    if not sid:
        raise ValueError("no criteria to write: name a scheme")
    normalised = criteria_lib.normalise_criteria(doc)
    with WRITE_LOCK:
        folder = _writable_scheme_dir(sid)
        path = os.path.join(folder, CRITERIA_FILENAME)
        _snapshot(path, os.path.join(folder, "history"))
        _atomic_write(path, normalised)
        _mirror_to_project(sid, root)
    return path


def unique_scheme_id(base):
    """``base``, or ``base-2``, ``base-3`` ... - the first id not installed."""
    base = slug_scheme_id(base)
    if not scheme_installed(base) and not os.path.isdir(scheme_dir(base)):
        return base
    for n in range(2, 1000):
        candidate = f"{base[:70]}-{n}"
        if not scheme_installed(candidate) and not os.path.isdir(scheme_dir(candidate)):
            return candidate
    raise ValueError("no free scheme id")


def install_scheme(doc, criteria_doc=None, scheme_id=None, overwrite=False,
                   source=""):
    """Install a scheme (and its criteria) into the library; returns the id.

    ``doc`` may be a scheme or a legacy v3.1 ``schema.json``.  An id that is
    already installed with a DIFFERENT scheme raises ``FileExistsError``
    unless ``overwrite``; the same scheme is simply reused.
    """
    if is_legacy_schema(doc):
        import scheme_legacy
        doc = scheme_legacy.convert(doc)
    normalised = _validated(doc)
    sid = _check_scheme_id(scheme_id or slug_scheme_id(
        normalised.get("id") or normalised.get("name") or normalised.get("label")))
    normalised["id"] = sid
    with WRITE_LOCK:
        if scheme_installed(sid) and not overwrite:
            current = SL.normalise_scheme(_read_json(scheme_path(sid)) or {})
            current["id"] = sid
            same = current == normalised
            if same and criteria_doc is not None:
                import criteria_lib
                stored = _read_json(criteria_path(sid))
                same = (criteria_lib.normalise_criteria(stored or {})
                        == criteria_lib.normalise_criteria(criteria_doc or {}))
            if not same:
                raise FileExistsError(sid)
            return sid
        write_scheme(normalised, scheme_id=sid)
        if criteria_doc is not None or not os.path.isfile(criteria_path(sid)):
            write_criteria(criteria_doc or {}, scheme_id=sid)
        info = _read_json(scheme_manifest_path(sid))
        info = info if isinstance(info, dict) else {}
        info.update({
            "id": sid,
            "name": normalised.get("name") or sid,
            "label": normalised.get("label") or normalised.get("name") or sid,
            "description": normalised.get("description") or "",
        })
        info.setdefault("created", now())
        info["installed"] = now()
        if source:
            info["installed_from"] = str(source)
        write_scheme_manifest(info, sid)
    return sid


def install_from_project(scheme_id, root=None):
    """Install the scheme a project carries (``Project/scheme.json``)."""
    paths = project_paths(root)
    doc = _read_json(paths["scheme"])
    if not isinstance(doc, dict) or not doc:
        raise ValueError("the project carries no scheme")
    crit = _read_json(paths["criteria"])
    return install_scheme(doc, crit if isinstance(crit, dict) else None,
                          scheme_id=scheme_id, source=paths["scheme"])


def assign_scheme(scheme_id, root=None):
    """Make the project use the installed scheme ``scheme_id``.

    Writes ``Project/project.json`` (creating ``Project/`` when needed) and
    the project's portable copy of the scheme and the criteria.
    """
    sid = _check_scheme_id(scheme_id)
    if not scheme_installed(sid):
        raise ValueError(f"the scheme '{sid}' is not installed")
    base = os.path.abspath(root or active_root())
    with WRITE_LOCK:
        doc = dict(manifest(base) or {})
        if not doc:
            name = os.path.basename(base.rstrip("\\/")) or "project"
            doc = {"id": slug_scheme_id(name, "project"), "name": name,
                   "label": name, "description": "", "created": now()}
        previous = doc.get("scheme")
        doc["scheme"] = sid
        doc["criteria"] = sid
        if previous and previous != sid:
            doc["previous_scheme"] = previous
        doc["scheme_assigned"] = now()
        write_manifest(doc, base)
        _mirror_to_project(sid, base)
    return sid


def save_active_scheme(doc, root=None):
    """Write the active project's scheme, wherever it came from.

    A project that already names a scheme writes it.  A project that shows
    a legacy schema or its own copy without naming a scheme gets a new
    library scheme (a fresh id, never someone else's) and is assigned to it.
    """
    base = os.path.abspath(root or active_root())
    with WRITE_LOCK:
        sid = active_scheme_id(base)
        if sid:
            return write_scheme(doc, scheme_id=sid, root=base), sid
        normalised = _validated(doc)
        crit = criteria(base)
        sid = unique_scheme_id(normalised.get("id") or normalised.get("name")
                               or os.path.basename(base))
        install_scheme(normalised, crit, scheme_id=sid, source=base)
        assign_scheme(sid, base)
        return scheme_path(sid), sid


def save_active_criteria(doc, root=None):
    """Write the active project's criteria (assigning a scheme first when the
    project names none - see :func:`save_active_scheme`)."""
    base = os.path.abspath(root or active_root())
    with WRITE_LOCK:
        if not active_scheme_id(base):
            save_active_scheme(scheme(base), base)
        return write_criteria(doc, root=base)


def invalidate():
    """Drop the cached project documents (call after writing or re-rooting)."""
    for bucket in _CACHE.values():
        bucket.clear()


def now():
    """An ISO timestamp, for manifests and history entries."""
    return datetime.now().isoformat(timespec="seconds")
