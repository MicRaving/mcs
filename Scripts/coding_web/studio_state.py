"""Shared runtime state for Coding Studio.

The app can be re-pointed at another data root while it is running
(``POST /api/root``), so every derived folder and the paper-key list live on a
single :class:`State` instance, :data:`S`.  Modules read them through ``S``
(``S.TEMP``, ``S.KEYS``, ...), which gives one source of truth and lets a
re-root propagate without re-importing modules.

``configure()`` also decides where temporary files go; the whole campaign keeps
its scratch files under ``<root>/temp``.
"""

import glob
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime

FROZEN = getattr(sys, "frozen", False)
HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE = getattr(sys, "_MEIPASS", HERE)
REPO = os.path.dirname(os.path.dirname(HERE))
EXE_DIR = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else REPO

CONFIG_NAME = "codingstudio.config.json"

#: Project-relative prefixes whose files are fetched on demand in an online
#: project (audit §2: Papers/, Text/, Supplements/, the raw data under
#: Datasets/, and the AI coding routes ``Codings/<Key> - SB|MM|DS.json``, which
#: are on demand since 2026-11-24 - see ``protocol.AI_ROUTE_TAGS``).
#: ``State.ensure_local`` is a no-op for a local project, and a no-op in online
#: mode for a path outside these prefixes (a human coder's ``Codings/`` file is
#: core and therefore always local).
ONLINE_FETCH_PREFIXES = ("Papers/", "Text/", "Supplements/", "Datasets/", "Codings/")

#: Progress of the current/last on-demand download batch
#: (:meth:`State.ensure_many`), read by :func:`prefetch_progress` and shown by
#: the online status so a whole-corpus view can report what it is fetching.
_PREFETCH = {"running": False, "phase": "idle", "label": "", "done": 0,
             "total": 0, "bytes": 0, "missing": 0, "at": 0.0}


def prefetch_progress():
    """``{running, phase, label, done, total, bytes, missing, at}`` of the batch."""
    return dict(_PREFETCH)



def _first_existing(*paths, probe=None):
    for p in paths:
        if not p:
            continue
        check = os.path.join(p, probe) if probe else p
        try:
            if os.path.isfile(check) if probe else os.path.exists(check):
                return p
        except OSError:
            continue
    return paths[-1]


_SRC_WEB = os.path.join( os.path.dirname(EXE_DIR), "Scripts", "coding_web")
STATIC = _first_existing(
    os.path.join(BUNDLE, "static"),
    os.path.join(HERE, "static"),
    os.path.join(EXE_DIR, "Scripts", "coding_web", "static"),
    os.path.join(_SRC_WEB, "static"),
    probe="index.html",
)
#: The legacy (v3.1) schema shipped with the app.  It is the fallback for a
#: project that has no scheme of its own; the project's own scheme is read
#: through ``project_context`` (see :func:`schema_path`).
SCHEMA = _first_existing(
    os.path.join(BUNDLE, "schema.json"),
    os.path.join(HERE, "schema.json"),
    os.path.join(EXE_DIR, "Scripts", "coding_web", "schema.json"),
    os.path.join(_SRC_WEB, "schema.json"),
)

# The shared project layer lives one level up (Scripts/): ``project_context``
# answers "which project is active?".  The app points it at the data root, so
# the scheme, the criteria and the manifest follow a re-root.
try:
    import project_context  # noqa: E402
except Exception:  # pragma: no cover - source layout fallback
    for _d in (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
               os.path.join(HERE, "Scripts")):
        if _d and _d not in sys.path:
            sys.path.insert(0, _d)
    import project_context  # noqa: E402


def schema_path():
    """The scheme document of the active project (the legacy file as fallback)."""
    try:
        path = project_context.scheme_path(project_context.active_scheme_id() or "")
        if os.path.exists(path):
            return path
    except Exception:  # noqa: BLE001
        pass
    return SCHEMA


def find_data_root(start, max_up=6):
    """Locate the folder holding ``Papers/`` / ``Text/`` / ``Extraction/``."""
    cur = os.path.abspath(start)
    for _ in range(max_up + 1):
        if os.path.isdir(os.path.join(cur, "Papers")) or os.path.isdir(os.path.join(cur, "Text")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def _config_candidates():
    """Where the app config lives, per platform.

    Windows keeps the portable file next to the exe when it is frozen, and
    falls back to %LOCALAPPDATA%.  macOS and Linux use the platform's own
    place: an app folder inside a bundle or a Flatpak is read-only, so the
    config cannot live beside the executable there.
    """
    out = []
    if FROZEN and sys.platform == "win32":
        out.append(os.path.join(EXE_DIR, CONFIG_NAME))
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", tempfile.gettempdir())
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~/Library/Application Support"))
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config")
    out.append(os.path.join(base, "CodingStudio", CONFIG_NAME))
    return out


def config_path():
    """The existing config, else the first candidate whose folder is writable.

    A copy installed under Program Files cannot write beside its exe; picking
    that path anyway made every save fail silently (no recent projects, no
    remembered folder).  ``CODINGSTUDIO_CONFIG`` names the file outright (a
    test run, a portable checkout).
    """
    override = (os.environ.get("CODINGSTUDIO_CONFIG") or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    candidates = _config_candidates()
    for c in candidates:
        if os.path.exists(c):
            return c
    for c in candidates[:-1]:
        if _can_write_in(os.path.dirname(c)):
            return c
    return candidates[-1]


def _can_write_in(folder):
    """Whether a file can really be created in ``folder``.

    ``os.access`` on Windows only reads the read-only attribute, not the ACL,
    so it calls Program Files writable; a probe file is the honest test.
    """
    probe = os.path.join(folder, f".codingstudio-probe-{os.getpid()}")
    try:
        with open(probe, "w", encoding="utf-8"):
            pass
        os.remove(probe)
        return True
    except OSError:
        return False


def load_config():
    try:
        with open(config_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(updates):
    path = config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        cfg = load_config()
        cfg.update(updates)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        log(f"save_config failed: {exc!r}")
        return False


#: The app config key that holds the "run the analysis locally" switch.
RUN_ANALYSIS_KEY = "run_analysis_locally"


def run_analysis_locally():
    """Whether the analysis pipeline may run on this machine.

    An online project normally runs its analysis on the studio server: the
    pipeline needs every ``Extraction/`` and ``Datasets/*/- harmonized`` file,
    which a mirror does not hold.  The app config's switch (or the
    ``CODING_STUDIO_RUN_ANALYSIS=1`` environment variable) opts back in for a
    user who took the whole project offline.
    """
    if os.environ.get("CODING_STUDIO_RUN_ANALYSIS") == "1":
        return True
    try:
        return bool(load_config().get(RUN_ANALYSIS_KEY))
    except Exception:  # noqa: BLE001
        return False


#: The app config key that holds the recently opened project folders.
RECENT_PROJECTS_KEY = "recent_projects"
#: The app config key that holds the opening time of every recent project.
RECENT_OPENED_KEY = "recent_projects_opened"
#: How many projects the recent list keeps.
RECENT_PROJECTS_LIMIT = 12


def recent_projects():
    """The recently opened project folders, most recent first.

    The list lives in the app config (``codingstudio.config.json``) under
    ``recent_projects``: absolute paths, most recent first, at most
    :data:`RECENT_PROJECTS_LIMIT` entries.  The opening time of each folder
    lives beside it under ``recent_projects_opened`` (an ISO stamp), so the
    Home page can show when a project was last used.

    Windows paths are compared case-insensitively, so the same folder never
    appears twice under two spellings.
    """
    raw = load_config().get(RECENT_PROJECTS_KEY)
    out = []
    if not isinstance(raw, list):
        return out
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            continue
        path = os.path.abspath(entry.strip())
        if not any(os.path.normcase(path) == os.path.normcase(known)
                   for known in out):
            out.append(path)
    return out


def recent_project_opened(path):
    """The ISO stamp of the last opening of ``path`` (``""`` when unknown)."""
    if not path:
        return ""
    stamps = load_config().get(RECENT_OPENED_KEY)
    if not isinstance(stamps, dict):
        return ""
    target = os.path.normcase(os.path.abspath(path))
    for key, value in stamps.items():
        if (isinstance(key, str) and isinstance(value, str)
                and os.path.normcase(key) == target):
            return value
    return ""


def record_recent_project(path):
    """Record ``path`` as recently opened; returns the new recent list.

    The folder moves to the front, duplicates are dropped and the list keeps
    at most :data:`RECENT_PROJECTS_LIMIT` entries.
    """
    if not path:
        return recent_projects()
    path = os.path.abspath(path)
    target = os.path.normcase(path)
    paths = [p for p in recent_projects() if os.path.normcase(p) != target]
    paths.insert(0, path)
    paths = paths[:RECENT_PROJECTS_LIMIT]
    stamps = {}
    raw_stamps = load_config().get(RECENT_OPENED_KEY)
    kept = {os.path.normcase(p) for p in paths}
    if isinstance(raw_stamps, dict):
        stamps = {k: v for k, v in raw_stamps.items()
                  if isinstance(k, str) and isinstance(v, str)
                  and os.path.normcase(k) in kept}
    stamps[path] = datetime.now().isoformat(timespec="seconds")
    save_config({RECENT_PROJECTS_KEY: paths, RECENT_OPENED_KEY: stamps})
    return paths


#: The app config key that holds the saved online connections (no secrets).
ONLINE_CONNECTIONS_KEY = "online_connections"
#: The app config key that says what the app had open last:
#: ``{"kind": "online", "id": <connection id>}`` or ``{"kind": "local"}``.
LAST_PROJECT_KEY = "last_project"
#: The fields a saved connection keeps (never a password, token or app password:
#: those live in the OS keyring, see ``online/keyring_store.py``).
ONLINE_CONNECTION_FIELDS = ("id", "kind", "url", "server", "username", "folder", "webdav_root",
                            "project_id", "project_name", "last_opened", "login", "invite_id")
_SECRET_WORDS = ("password", "token", "secret")


def connection_id(kind, server, username, project_id):
    """A stable id of one saved connection (kind + server + user + project)."""
    import hashlib

    raw = "|".join(str(x or "") for x in (kind, (server or "").rstrip("/").lower(),
                                          username, project_id))
    return "c-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def online_connections():
    """The saved online connections, most recently opened first."""
    raw = load_config().get(ONLINE_CONNECTIONS_KEY)
    out = []
    if not isinstance(raw, list):
        return out
    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        out.append({k: entry.get(k) for k in ONLINE_CONNECTION_FIELDS if k in entry})
    out.sort(key=lambda e: str(e.get("last_opened") or ""), reverse=True)
    return out


def online_connection(conn_id):
    for entry in online_connections():
        if entry.get("id") == conn_id:
            return entry
    return None


def record_online_connection(conn):
    """Remember *conn* (moves it to the front, marks it the last project).

    Only the fields of :data:`ONLINE_CONNECTION_FIELDS` are kept; a key that
    looks like a secret is refused outright, so a caller can never put a
    password into the plain-text config by mistake.
    """
    if any(any(w in str(k).lower() for w in _SECRET_WORDS) for k in conn):
        raise ValueError("a saved connection never holds a secret")
    entry = {k: conn.get(k) for k in ONLINE_CONNECTION_FIELDS if conn.get(k) is not None}
    if not entry.get("id"):
        entry["id"] = connection_id(entry.get("kind"), entry.get("server"), entry.get("username"),
                                    entry.get("project_id"))
    entry["last_opened"] = datetime.now().isoformat(timespec="seconds")
    rest = [e for e in online_connections() if e.get("id") != entry["id"]]
    conns = ([entry] + rest)[:RECENT_PROJECTS_LIMIT]
    save_config({ONLINE_CONNECTIONS_KEY: conns,
                 LAST_PROJECT_KEY: {"kind": "online", "id": entry["id"]}})
    return entry


def forget_online_connection(conn_id):
    """Drop one saved connection; returns it (or None)."""
    conns = online_connections()
    gone = next((e for e in conns if e.get("id") == conn_id), None)
    updates = {ONLINE_CONNECTIONS_KEY: [e for e in conns if e.get("id") != conn_id]}
    last = load_config().get(LAST_PROJECT_KEY)
    if isinstance(last, dict) and last.get("id") == conn_id:
        updates[LAST_PROJECT_KEY] = {"kind": "local"}
    save_config(updates)
    return gone


def last_project():
    """``{"kind": "online", "id": ...}`` / ``{"kind": "local"}`` (default local)."""
    last = load_config().get(LAST_PROJECT_KEY)
    return dict(last) if isinstance(last, dict) and last.get("kind") else {"kind": "local"}


def set_last_project_local():
    save_config({LAST_PROJECT_KEY: {"kind": "local"}})


def default_root():
    """Source: the repo root above Scripts/coding_web.  Packaged: the project
    the app sits in, else a per-user folder (never the app's own folder, which
    an update replaces)."""
    if not FROZEN:
        return REPO
    up = find_data_root(EXE_DIR)
    if up:
        return up
    try:
        return project_context.user_projects_dir()
    except Exception:  # noqa: BLE001
        return EXE_DIR


def has_data(root):
    return os.path.isdir(os.path.join(root, "Papers")) or os.path.isdir(os.path.join(root, "Text"))


def _probe_writable(folder):
    """Raise ``OSError`` unless a real file can be created in ``folder``.

    ``os.access`` lies on a dehydrated cloud placeholder, so the only reliable
    test is to write and remove a probe file."""
    fd, probe = tempfile.mkstemp(suffix=".wtest", dir=folder)
    try:
        os.write(fd, b"ok")
    finally:
        os.close(fd)
        try:
            os.remove(probe)
        except OSError:
            pass


def _local_fallback_dir(name):
    """A writable scratch folder outside the synced project tree."""
    base = os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(),
                        "CodingStudio", name)
    os.makedirs(base, exist_ok=True)
    _probe_writable(base)
    return base


#: Callbacks run after the data root is re-pointed (``POST /api/root``), so a
#: module can drop caches that hold data of the previous root.  Register with
#: :func:`on_reroot`; each hook gets ``(old_root, new_root)``.
_REROOT_HOOKS = []


def on_reroot(fn):
    """Register ``fn(old_root, new_root)`` to run after every re-root (decorator-friendly)."""
    if fn not in _REROOT_HOOKS:
        _REROOT_HOOKS.append(fn)
    return fn


def _run_reroot_hooks(old, new):
    for fn in list(_REROOT_HOOKS):
        try:
            fn(old, new)
        except Exception as exc:  # noqa: BLE001 - one module's cache must not block a re-root
            log(f"re-root hook {getattr(fn, '__qualname__', fn)!r} failed: {exc!r}")


def _clear_mapping(module_name, attr, keys=None):
    """Reset one module-level cache without importing the module.

    A cache that belongs to a module the running app has not imported yet is
    not stale (it does not exist yet), so ``sys.modules`` is the right place to
    look: importing every cache owner on a root switch would cost seconds and
    could drag in pandas for a project that never uses it.
    """
    module = sys.modules.get(module_name)
    if module is None:
        return
    obj = getattr(module, attr, None)
    if obj is None:
        return
    try:
        if keys is None:
            obj.clear()
        elif isinstance(obj, dict):
            for key, value in keys.items():
                obj[key] = value
    except Exception:  # noqa: BLE001 - a cache reset may never fail a re-root
        pass


def reset_root_caches(temp_dir=None):
    """Invalidate every cache the root switch in audit §1.3 leaves behind.

    The existing root switch reset only ``studio_lib._CODING_INDEX`` and the
    agreement layer.  These are the remaining root-unaware caches: the master
    workbook map (``fulltext_lib``), the harmonization overview
    (``routes_harmonizer``), the PRISMA counts (``routes_apa``), the analysis
    caches (incl. ``ANALYSIS_CACHE``, an import-time constant) and the Home
    view's screening count (``routes_project``).  Path-keyed caches (PDF pages,
    repro dossiers) need no reset, but the ones keyed by a bare mtime do.
    """
    _clear_mapping("studio_lib", "_CODING_INDEX", {"at": 0.0, "data": None})
    _clear_mapping("studio_lib", "_SCHEMA_CACHE", {"obj": None})
    _clear_mapping("studio_lib", "_DOMAIN_CACHE", {"stamp": None, "value": None})
    _clear_mapping("studio_lib", "_pdf_pages")
    _clear_mapping("studio_lib", "_pdf_text")
    _clear_mapping("studio_lib", "_pdf_size")
    _clear_mapping("studio_lib", "_pdf_search")
    _clear_mapping("studio_lib", "_pdf_spans")
    _clear_mapping("studio_cache", "_INDEX", {"root": None, "stamps": None, "papers": None})
    # Shaped caches are reset to their template, never emptied: their readers
    # index the keys directly (an emptied _SCREEN_INDEX raised KeyError 'rows').
    _clear_mapping("screening_lib", "_SCREEN_INDEX",
                   {"stamp": None, "rows": None, "by_id": None, "info": None})
    _clear_mapping("screening_lib", "_DECISIONS_CACHE")
    # the text side file belongs to the old root's temp folder: release it so
    # the new root opens its own (and Windows can delete the old one)
    _screening = sys.modules.get("screening_lib")
    if _screening is not None:
        try:
            _screening._close_text_db()
        except Exception:  # noqa: BLE001 - a re-root may never fail over a cache
            pass
    _clear_mapping("fulltext_lib", "_MAP_CACHE", {"mtime": None, "map": None, "info": None})
    _clear_mapping("routes_harmonizer", "_HARM_MISSING_CACHE", {"mtime": None, "data": None})
    _clear_mapping("routes_apa", "_APA_PRISMA_CACHE", {"mtime": None, "data": None})
    _clear_mapping("routes_project", "_SCREEN_CACHE", {"stamp": None, "ids": 0})
    _clear_mapping("routes_repro", "_INTERP_CACHE", {"at": 0.0, "value": (None, "")})

    agreement = sys.modules.get("agreement_lib")
    if agreement is not None:
        try:
            agreement._invalidate_agreement()
        except Exception:  # noqa: BLE001
            pass

    analysis = sys.modules.get("analysis_lib")
    if analysis is not None:
        try:
            analysis._invalidate_analysis_caches()
        except Exception:  # noqa: BLE001
            pass
        # ANALYSIS_CACHE is computed at import time from the then-current
        # S.TEMP (audit §1.3 / risk 9): recompute it for the new root.
        if temp_dir:
            try:
                analysis.ANALYSIS_CACHE = os.path.join(temp_dir, ".analysis_cache.json")
            except Exception:  # noqa: BLE001
                pass
        _clear_mapping("analysis_lib", "_NEWEST_INPUT_CACHE", {"at": 0.0, "mtime": 0.0})
        _clear_mapping("analysis_lib", "_FIGDIR_CACHE", {"at": 0.0, "dir": None})


class State:
    """Mutable paths and derived data, resolved by :meth:`configure`."""

    def __init__(self):
        self.ROOT = None
        self.PAPERS = None
        self.TEXT = None
        self.EXTRACTION = None
        self.CODINGS = None
        self.TEMP = None
        self.PDFIMG = None
        self.DATASETS = None
        self.SCREENING = None
        self.FULLTEXT = None
        self.SUPPLEMENTS = None
        self.KEYS = set()
        # --- online project (routes_online) -------------------------------
        #: True while the app root is an online project's mirror.
        self.online = False
        #: The ``OnlineStore`` of the open online project (None for a local
        #: folder project).  ``ensure_local``/``listdir`` use it.
        self.store = None
        #: The local folder the app returns to on ``/api/online/close``.
        self._local_root_before_online = None
        #: Extra cache resets registered by modules that learn a root late.
        self._root_hooks = []
        self._lock = threading.RLock()
        #: ``(root, prefix, suffix) -> (stamp, at, entries)`` for :meth:`names`.
        self._names_cache = {}
        #: Safety net only: the entry's *stamp* (manifest seq/epoch + folder
        #: mtime) is the real freshness test, so a long TTL costs no correctness.
        self.NAMES_TTL = 300.0

    # -- online project ----------------------------------------------------

    def set_online(self, store, root):
        """Point the app at an online project's mirror and remember where to go back."""
        with self._lock:
            if not self.online:
                self._local_root_before_online = self.ROOT
            self.online = True
            self.store = store
        self.configure(root)
        self.refresh_keys()
        return self.ROOT

    def clear_online(self):
        """Leave the online project and re-point the app at the local folder."""
        with self._lock:
            self.online = False
            self.store = None
            root = self._local_root_before_online
            self._local_root_before_online = None
        if root and os.path.isdir(root):
            self.configure(root)
        else:
            self.configure(default_root())
        self.refresh_keys()
        return self.ROOT

    def register_root_hook(self, hook):
        """Register ``hook(root)`` to run after every root switch (idempotent)."""
        with self._lock:
            if hook not in self._root_hooks:
                self._root_hooks.append(hook)

    def relpath(self, path):
        """Project-relative POSIX path of *path* (None when outside the root)."""
        if not path:
            return None
        root = self.ROOT
        if not root:
            return None
        raw = str(path)
        full = os.path.abspath(raw if os.path.isabs(raw) else
                               os.path.join(root, *raw.replace("\\", "/").split("/")))
        root_n = os.path.normcase(root)
        full_n = os.path.normcase(full)
        if full_n != root_n and not full_n.startswith(root_n + os.sep):
            return None
        return os.path.relpath(full, root).replace("\\", "/")

    def abspath(self, relpath):
        """Absolute path of a project-relative path (``relpath`` may be absolute)."""
        raw = str(relpath)
        if os.path.isabs(raw):
            return os.path.abspath(raw)
        return os.path.abspath(os.path.join(self.ROOT, *raw.replace("\\", "/").split("/")))

    def ensure_local(self, path):
        """A local absolute path for *path*, downloading it in an online project.

        This is the helper the audit's read sites call before opening a file of
        ``Papers/``, ``Text/``, ``Supplements/`` or the raw data under
        ``Datasets/`` - and, since the AI coding routes are on demand
        (``protocol.AI_ROUTE_TAGS``), before opening one ``Codings/<Key> -
        SB|MM|DS.json``.  For a local folder project it is a **no-op**: the same
        path comes back.  For an online project it asks the store's syncer to
        fetch the file when it is missing or outdated (blocking), and returns
        the path unchanged when that is not possible (a genuinely missing file,
        or an offline mirror without a copy) so the caller's existing error
        path still applies.  A whole-corpus reader uses :meth:`ensure_many`.
        """
        raw = str(path) if path is not None else ""
        if not self.online or self.store is None or not self.ROOT:
            return raw
        rel = self.relpath(raw)
        if rel is None or rel.startswith("../"):
            return raw
        if not rel.startswith(ONLINE_FETCH_PREFIXES):
            # A core file (Extraction/, Reproduction/, Screening/ ...) is local
            # once the first sync finished; until then it may not be here yet,
            # so a read of a missing one fetches it at once instead of failing
            # (the background download reaches it later anyway).
            if os.path.exists(self.abspath(rel)):
                return raw
            knows = getattr(self.store, "knows_file", None)
            try:
                if knows is None or not knows(rel):
                    return raw
            except Exception:  # noqa: BLE001
                return raw
        try:
            return self.store.ensure_local(rel) or raw
        except Exception:  # noqa: BLE001 - OfflineError/FileNotFoundError: keep the old behaviour
            return raw

    def names(self, prefix="", suffix=""):
        """The files directly in one folder: ``[{name, path, size, hash, local, mtime_ns}]``.

        In an online project the **manifest is the authority**, so a file that
        is known but not downloaded still appears - that is what lets
        ``n_coders`` and the coder lists count an on-demand AI route
        (``Codings/<Key> - SB.json``) without fetching it (INTERFACES 4).
        A local project (or an offline listing that failed) scans the disk.

        The answer is cached per (root, folder, suffix), keyed on the manifest's
        seq/epoch and the folder's mtime: a whole-corpus view asks once per
        paper, and re-listing a folder of thousands of names 826 times cost
        ~26 s (see ``agreement_lib``).  A new or deleted file changes both
        stamps, so the cache never hides a change.
        """
        prefix = str(prefix or "").strip("/").replace("\\", "/")
        if os.path.isabs(prefix):
            prefix = self.relpath(prefix) or ""
        suffix = str(suffix or "").lower()
        stamp = self._folder_stamp(prefix)
        key = (self.ROOT, prefix, suffix)
        now = time.monotonic()
        with self._lock:
            cached = self._names_cache.get(key)
            if cached is not None and cached[0] == stamp and now - cached[1] < self.NAMES_TTL:
                return cached[2]        # a shared, cached view - do not mutate
        out = []
        listed = False
        if self.online and self.store is not None:
            try:
                for e in self.store.listdir(prefix):
                    if e.get("is_dir"):
                        continue
                    if suffix and not e["name"].lower().endswith(suffix):
                        continue
                    local = bool(e.get("local"))
                    out.append({"name": e["name"], "path": e["path"],
                                "size": e.get("size"), "hash": e.get("hash"),
                                "local": local,
                                "mtime_ns": self._mtime_ns(e["path"]) if local else None})
                listed = True
            except Exception:  # noqa: BLE001 - offline: fall back to the disk
                out = []
        if not listed:
            folder = self.abspath(prefix) if prefix else self.ROOT
            try:
                with os.scandir(folder) as it:
                    for e in it:
                        try:
                            if not e.is_file():
                                continue
                            if suffix and not e.name.lower().endswith(suffix):
                                continue
                            st = e.stat()
                        except OSError:
                            continue
                        out.append({"name": e.name, "path": prefix + "/" + e.name if prefix else e.name,
                                    "size": st.st_size, "hash": None, "local": True,
                                    "mtime_ns": st.st_mtime_ns})
            except OSError:
                out = []
        out.sort(key=lambda e: e["name"].lower())
        with self._lock:
            self._names_cache[key] = (stamp, now, out)
        return out

    def _mtime_ns(self, relpath):
        try:
            return os.stat(self.abspath(relpath)).st_mtime_ns
        except OSError:
            return None

    def _folder_stamp(self, prefix):
        """A cheap "did this folder's names change?" stamp (manifest + disk)."""
        stamp = [self.online]
        if self.online and self.store is not None:
            mirror = getattr(self.store, "mirror", None)
            state = getattr(mirror, "state", None)
            try:
                with mirror.lock:
                    stamp.append(state.get("seq"))
                    stamp.append(state.get("visibility_epoch"))
            except Exception:  # noqa: BLE001 - a missing state only defeats the cache
                stamp.append(None)
        try:
            stamp.append(os.stat(self.abspath(prefix) if prefix else self.ROOT).st_mtime_ns)
        except OSError:
            stamp.append(None)
        return tuple(stamp)

    def ensure_many(self, paths, progress=None, label=""):
        """Fetch a **batch** of on-demand files in one sync round (online only).

        The whole-corpus counterpart of :meth:`ensure_local`: a view that scores
        every coder's file (the agreement / IRR tab reads every
        ``Codings/<Key> - <coder>.json``) asks for all of them at once instead
        of issuing one blocking request per file inside its page loop.  The
        files are queued as ``stale`` and downloaded by a single
        ``Syncer.pull`` (one manifest request, one state save; a crash mid-batch
        resumes on the next tick), and ``progress(done, total, path)`` is
        reported per file together with :func:`prefetch_progress`.

        For a local project nothing is fetched.  ``missing`` names the paths
        that are still absent afterwards (offline, or unknown to the server), so
        the caller can say so instead of quietly computing a different number.

        Returns ``{total, needed, fetched, missing, offline, bytes}``.
        """
        rels = []
        for raw in paths or ():
            text = str(raw or "")
            rel = (self.relpath(text) or "") if os.path.isabs(text) else text.strip("/")
            rel = rel.replace("\\", "/")
            if rel and rel not in rels:
                rels.append(rel)
        result = {"total": len(rels), "needed": 0, "fetched": 0, "missing": [],
                  "offline": False, "bytes": 0}
        if not rels:
            return result
        if not self.online or self.store is None:
            return result
        batch = getattr(self.store, "ensure_many", None)
        if batch is None:                       # an older store: one at a time
            for rel in rels:
                try:
                    self.store.ensure_local(rel)
                except Exception:  # noqa: BLE001
                    result["missing"].append(rel)
            return result
        total = len(rels)
        done = {"n": 0}
        _PREFETCH.update(running=True, phase="fetching", label=str(label or ""),
                         done=0, total=total, bytes=0, missing=0, at=time.time())

        def _report(done_n, _total, path):
            done["n"] = max(done["n"], int(done_n))
            _PREFETCH.update(done=done["n"], total=total, at=time.time())
            if progress is not None:
                try:
                    progress(done["n"], total, path)
                except Exception:  # noqa: BLE001 - progress may never fail a view
                    pass

        try:
            result = batch(rels, progress=_report)
        except Exception as exc:  # noqa: BLE001 - offline / server refusal
            if type(exc).__name__ == "NotConnectedError" or not self.online:
                # the project was closed meanwhile (the window was closed while
                # a view prefetched): nothing to fetch and nothing to report
                result = {"total": total, "needed": 0, "fetched": 0, "missing": [],
                          "offline": False, "bytes": 0, "closed": True}
            else:
                log(f"prefetch {label or 'files'} failed: {exc!r}")
                result = {"total": total, "needed": total, "fetched": 0,
                          "missing": list(rels), "offline": True, "bytes": 0}
        finally:
            _PREFETCH.update(running=False, phase="done",
                             done=min(done["n"], total), total=total,
                             bytes=int(result.get("bytes") or 0),
                             missing=len(result.get("missing") or []),
                             at=time.time())
        return result

    def exists(self, path):
        """Whether *path* exists locally **or** in the online project's manifest."""
        raw = str(path) if path is not None else ""
        if not raw:
            return False
        full = self.abspath(raw)
        if os.path.exists(full):
            return True
        if not self.online or self.store is None:
            return False
        rel = self.relpath(raw)
        if rel is None:
            return False
        knows = getattr(self.store, "knows_file", None)
        if knows is not None:
            try:
                return bool(knows(rel))
            except Exception:  # noqa: BLE001 - fall back to the listing below
                pass
        parent, _, name = rel.rpartition("/")
        try:
            return any(e["name"] == name and not e["is_dir"]
                       for e in self.store.listdir(parent))
        except Exception:  # noqa: BLE001 - listings are best effort
            return False

    def listdir(self, prefix="", recursive=False):
        """Entries of one folder, from the manifest **and** the disk.

        Each entry: ``{name, path, is_dir, size, local, mtime_ns}``.  In an
        online project the store's listing is the authority (so a file that is
        known but not downloaded still appears); a local stat fills in the
        mtime the cache stamps need.  For a local project this is an ordinary
        ``scandir`` (recursive on request).
        """
        prefix = str(prefix or "").strip("/").replace("\\", "/")
        if os.path.isabs(prefix):
            prefix = self.relpath(prefix) or ""
        if self.online and self.store is not None:
            try:
                entries = self.store.listdir(prefix, recursive=recursive)
            except Exception:  # noqa: BLE001 - offline: fall back to the disk
                entries = None
            if entries is not None:
                for e in entries:
                    try:
                        e["mtime_ns"] = os.stat(self.abspath(e["path"])).st_mtime_ns
                    except OSError:
                        e["mtime_ns"] = None
                return entries
        folder = self.abspath(prefix) if prefix else self.ROOT
        out = []
        if recursive:
            for dirpath, dirnames, names in os.walk(folder):
                rel_dir = os.path.relpath(dirpath, folder).replace("\\", "/")
                rel_dir = "" if rel_dir == "." else rel_dir + "/"
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                for name in sorted(names):
                    try:
                        st = os.stat(os.path.join(dirpath, name))
                    except OSError:
                        continue
                    out.append({"name": name, "path": prefix + "/" + rel_dir + name
                                if prefix else rel_dir + name,
                                "is_dir": False, "size": st.st_size, "local": True,
                                "mtime_ns": st.st_mtime_ns})
            return sorted(out, key=lambda e: e["path"])
        try:
            with os.scandir(folder) as it:
                for e in it:
                    try:
                        is_dir = e.is_dir()
                        st = e.stat()
                    except OSError:
                        continue
                    # `.studio/` (done marks, sync state) and other dot-folders
                    # are app-local, never study material (see AGENTS contract).
                    if is_dir and e.name.startswith("."):
                        continue
                    out.append({"name": e.name,
                                "path": (prefix + "/" if prefix else "") + e.name,
                                "is_dir": is_dir, "size": None if is_dir else st.st_size,
                                "local": True, "mtime_ns": st.st_mtime_ns})
        except OSError:
            return []
        return sorted(out, key=lambda e: e["path"])

    def ensure_tree_local(self, prefix):
        """Download every file under *prefix* (no-op for a local project).

        Used before a subprocess that reads a whole folder (a harmonizer or a
        reproducer reads every file of ``Datasets/<Key>/``).  Returns the list
        of project-relative paths that could not be made local.
        """
        if not self.online or self.store is None:
            return []
        prefix = str(prefix or "").strip("/").replace("\\", "/")
        if os.path.isabs(prefix):
            prefix = self.relpath(prefix) or ""
        try:
            entries = self.store.listdir(prefix, recursive=True)
        except Exception:  # noqa: BLE001 - offline: the caller decides
            return []
        failed = []
        for e in entries:
            if e.get("is_dir"):
                continue
            try:
                self.store.ensure_local(e["path"])
            except Exception:  # noqa: BLE001
                failed.append(e["path"])
        return failed

    def ensure_study_local(self, key):
        """Everything a per-study subprocess reads: ``Datasets/<key>/`` + text/paper."""
        failed = self.ensure_tree_local("Datasets/" + key)
        for rel in ("Text/" + key + ".txt", "Papers/" + key + ".pdf"):
            try:
                self.ensure_local(rel)
            except Exception:  # noqa: BLE001 - optional
                pass
        return failed

    def has_file(self, relpath):
        """``os.path.isfile`` that also answers from the manifest in online mode."""
        return self.exists(relpath) and not os.path.isdir(self.abspath(relpath))

    def configure(self, root=None):

        """Point the app at a data root (papers, text, extractions, codings)."""
        # The project context resolves the root (an explicit override, the
        # environment, the code's own root when that root is a project, then
        # the app's saved folder).  The app asks it rather than resolving a
        # root of its own, so the app and the analysis pipeline can never
        # disagree about which project is active.
        explicit = root or os.environ.get("CODING_STUDIO_ROOT")
        if not explicit:
            try:
                explicit = project_context.active_root()
            except Exception:  # pragma: no cover - the context is optional here
                explicit = load_config().get("root")
        root = explicit or default_root()
        if not explicit and not has_data(root):
            up = find_data_root(root)  # tolerate being placed in a sub-folder
            if up:
                root = up
        root = os.path.abspath(root)
        self.PAPERS = os.path.join(root, "Papers")
        self.TEXT = os.path.join(root, "Text")
        self.EXTRACTION = os.path.join(root, "Extraction")
        self.CODINGS = os.path.join(root, "Codings")
        self.TEMP = os.path.join(root, "temp")
        self.DATASETS = os.path.join(root, "Datasets")
        self.SCREENING = os.path.join(root, "Screening")
        self.FULLTEXT = os.path.join(root, "Fulltext")
        self.SUPPLEMENTS = os.path.join(root, "Supplements")
        try:
            os.makedirs(self.CODINGS, exist_ok=True)
            os.makedirs(self.TEMP, exist_ok=True)
            os.makedirs(self.SCREENING, exist_ok=True)
        except OSError:
            fallback = os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()),
                                    "CodingStudio")
            self.CODINGS = os.path.join(fallback, "Codings")
            self.TEMP = os.path.join(fallback, "temp")
            self.SCREENING = os.path.join(fallback, "Screening")
            self.FULLTEXT = os.path.join(fallback, "Fulltext")
            os.makedirs(self.CODINGS, exist_ok=True)
            os.makedirs(self.TEMP, exist_ok=True)
            os.makedirs(self.SCREENING, exist_ok=True)
        self.PDFIMG = os.path.join(self.TEMP, "pdfimg")
        try:
            os.makedirs(self.PDFIMG, exist_ok=True)
            _probe_writable(self.PDFIMG)
        except OSError:
            # A synced folder (Nextcloud/Dropbox) can hold temp/ as a dehydrated
            # placeholder where creating a file fails with EINVAL.  That made
            # every rendered PDF page come back blank; keep the images in the
            # local (never-synced) application folder instead.
            try:
                self.PDFIMG = _local_fallback_dir("pdfimg")
            except OSError:
                self.PDFIMG = self.TEMP
        tempfile.tempdir = self.TEMP
        previous, self.ROOT = self.ROOT, root
        with self._lock:
            self._names_cache.clear()
        # The project context follows the app: the scheme, the criteria and the
        # manifest are read from the active root, so a re-root (POST /api/root)
        # switches the scheme with the project.
        try:
            project_context.set_root(root)
        except Exception:  # noqa: BLE001 - never fail a re-root over the context
            pass
        # Opening a project is what makes it "recent": the Home page lists the
        # folders that were opened lately, most recent first.  An online
        # project's mirror is not a folder the user chose, so it is not
        # recorded (closing the online project must not add it to the list).
        if not self.online:
            try:
                record_recent_project(root)
            except Exception:  # noqa: BLE001 - the recent list is a convenience
                pass
        # The root switch must reset every root-unaware cache (audit §1.3):
        # without it a mirror inherited the previous project's summaries for a
        # key both projects share.
        try:
            reset_root_caches(self.TEMP)
        except Exception:  # noqa: BLE001 - a re-root must never fail over a cache
            pass
        for hook in list(self._root_hooks):
            try:
                hook(root)
            except Exception:  # noqa: BLE001
                pass
        # module-level ``@on_reroot`` hooks (agreement_lib, studio_lib, ...)
        if previous is not None:
            _run_reroot_hooks(previous, root)
        return root

    def paper_keys(self):
        keys = {}
        for f in glob.glob(os.path.join(self.TEXT, "*.txt")):
            keys[os.path.splitext(os.path.basename(f))[0]] = True
        for f in glob.glob(os.path.join(self.PAPERS, "*.pdf")):
            keys.setdefault(os.path.splitext(os.path.basename(f))[0], False)
        # A paper that is known from the online project's manifest but not yet
        # downloaded must still be listed (audit §9.1): otherwise a study with
        # neither a local Text/ nor a local PDF disappears from /api/papers.
        if self.online and self.store is not None:
            try:
                for key in self.store.list_keys():
                    keys.setdefault(key, False)
            except Exception:  # noqa: BLE001 - listings are best effort
                pass
        return sorted(keys, key=lambda s: (s.lower(), s))

    def refresh_keys(self):
        self.KEYS = set(self.paper_keys())
        return self.KEYS


S = State()
S.configure()


def configure(root=None):
    return S.configure(root)


def paper_keys():
    return S.paper_keys()


def refresh_keys():
    return S.refresh_keys()


def log(message):
    """Append a line to <root>/temp/CodingStudio.log (never raises)."""
    try:
        os.makedirs(S.TEMP, exist_ok=True)
        with open(os.path.join(S.TEMP, "CodingStudio.log"), "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')}  {message}\n")
    except Exception:
        pass
