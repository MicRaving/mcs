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
from datetime import datetime

FROZEN = getattr(sys, "frozen", False)
HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE = getattr(sys, "_MEIPASS", HERE)
REPO = os.path.dirname(os.path.dirname(HERE))
EXE_DIR = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else REPO

CONFIG_NAME = "codingstudio.config.json"


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
        path = project_context.project_paths()["scheme"]
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
    out = []
    if FROZEN:
        out.append(os.path.join(EXE_DIR, CONFIG_NAME))
    out.append(os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()),
                            "CodingStudio", CONFIG_NAME))
    return out


def config_path():
    for c in _config_candidates():
        if os.path.exists(c):
            return c
    return _config_candidates()[0]


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
    """Packaged: the folder the executable sits in. Source: the repo root above Scripts/coding_web."""
    return EXE_DIR if FROZEN else REPO


def has_data(root):
    return os.path.isdir(os.path.join(root, "Papers")) or os.path.isdir(os.path.join(root, "Text"))


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
        # folders that were opened lately, most recent first.
        try:
            record_recent_project(root)
        except Exception:  # noqa: BLE001 - the recent list is a convenience
            pass
        return root

    def paper_keys(self):
        keys = {}
        for f in glob.glob(os.path.join(self.TEXT, "*.txt")):
            keys[os.path.splitext(os.path.basename(f))[0]] = True
        for f in glob.glob(os.path.join(self.PAPERS, "*.pdf")):
            keys.setdefault(os.path.splitext(os.path.basename(f))[0], False)
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
