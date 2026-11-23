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
from datetime import datetime

FROZEN = getattr(sys, "frozen", False)
HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE = getattr(sys, "_MEIPASS", HERE)
REPO = os.path.dirname(os.path.dirname(HERE))
EXE_DIR = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else REPO

CONFIG_NAME = "codingstudio.config.json"

#: Project-relative prefixes whose files are fetched on demand in an online
#: project (audit §2: Papers/, Text/, Supplements/ and the raw data under
#: Datasets/).  ``State.ensure_local`` is a no-op for a local project.
ONLINE_FETCH_PREFIXES = ("Papers/", "Text/", "Supplements/", "Datasets/")



def _first_existing(*paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return paths[-1]


STATIC = _first_existing(os.path.join(BUNDLE, "static"), os.path.join(HERE, "static"))
#: The legacy (v3.1) schema shipped with the app.  It is the fallback for a
#: project that has no scheme of its own; the project's own scheme is read
#: through ``project_context`` (see :func:`schema_path`).
SCHEMA = _first_existing(os.path.join(BUNDLE, "schema.json"), os.path.join(HERE, "schema.json"))

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
    remembered folder).
    """
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
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
        return True
    except OSError:
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
    _clear_mapping("screening_lib", "_SCREEN_INDEX")
    _clear_mapping("screening_lib", "_DECISIONS_CACHE")
    _clear_mapping("fulltext_lib", "_MAP_CACHE")
    _clear_mapping("routes_harmonizer", "_HARM_MISSING_CACHE", {"mtime": None, "data": None})
    _clear_mapping("routes_apa", "_APA_PRISMA_CACHE", {"mtime": None, "data": None})
    _clear_mapping("routes_project", "_SCREEN_CACHE", {"stamp": None, "ids": 0})
    _clear_mapping("routes_repro", "_INTERP_CACHE")

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
        ``Datasets/``.  For a local folder project it is a **no-op**: the same
        path comes back.  For an online project it asks the store's syncer to
        fetch the file when it is missing or outdated (blocking), and returns
        the path unchanged when that is not possible (a genuinely missing file,
        or an offline mirror without a copy) so the caller's existing error
        path still applies.
        """
        raw = str(path) if path is not None else ""
        if not self.online or self.store is None or not self.ROOT:
            return raw
        rel = self.relpath(raw)
        if rel is None or rel.startswith("../") or not rel.startswith(ONLINE_FETCH_PREFIXES):
            return raw
        try:
            return self.store.ensure_local(rel) or raw
        except Exception:  # noqa: BLE001 - OfflineError/FileNotFoundError: keep the old behaviour
            return raw

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
            os.makedirs(self.CODINGS, exist_ok=True)
            os.makedirs(self.TEMP, exist_ok=True)
            os.makedirs(self.SCREENING, exist_ok=True)
        self.PDFIMG = os.path.join(self.TEMP, "pdfimg")
        try:
            os.makedirs(self.PDFIMG, exist_ok=True)
        except OSError:
            self.PDFIMG = self.TEMP
        tempfile.tempdir = self.TEMP
        self.ROOT = root
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
