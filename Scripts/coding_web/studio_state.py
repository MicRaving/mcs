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
SCHEMA = _first_existing(os.path.join(BUNDLE, "schema.json"), os.path.join(HERE, "schema.json"))


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
        explicit = root or os.environ.get("CODING_STUDIO_ROOT") or load_config().get("root")
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
