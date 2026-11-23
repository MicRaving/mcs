"""The local mirror of an online project (INTERFACES §4).

Layout::

    <mirror>/                      the project working copy (S.ROOT in online mode)
    <mirror>/.studio/state.json    sync state, written atomically
    <mirror>/.studio/outbox.jsonl  the durable change journal (outbox.py)
    <mirror>/.studio/pending/<h>   content-addressed copies of queued versions
    <mirror>/.studio/conflicts/    local bytes of conflicted/rejected pushes
    <mirror>/.studio/tmp/          download / write staging (same volume => os.replace)
    <mirror>/.studio/token         token fallback when ``keyring`` is missing

``state.json`` keys (contract keys first, then this implementation's own)::

    server, project_id, project_name, username, client_id, seq,
    visibility_epoch, heads: {path: {hash, size, mtime_ns}}, pinned: [keys],
    cache: {...}                                     (see cache.py)
    remote:     {path: {hash, size, kind, key, tag, seq}}   last known server state
    local_only: {path: {hash, size, mtime_ns}}       unwritable local edits (not pushed)
    stale:      [paths]                              core files to (re)download
    restore:    {path: {hash, conflict}}             server version to restore after a
                                                     conflict/rejection (download pending)
    me:         {role, tags, tasks}                  permissions mirrored from GET /api/p/{pid}

``heads[path]`` is the *server version the local file was last synced to*
together with the stat of the local file at that moment; it is the base of
the next push.  ``remote[path]`` is what the server has now (as far as the last
manifest said); a path with a remote entry but no head is known but not
downloaded (on-demand files).

Change detection is a scan (no hooks): stat every file, hash only those whose
``(size, mtime_ns)`` differs from the head.  Writable changed files are hashed
*while* being copied to ``pending/`` so the queued version survives a later
edit of the working file.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field

from online_shared import protocol

STUDIO_DIR = ".studio"
STATE_VERSION = 1
_CHUNK = 1024 * 1024


# --- locations ----------------------------------------------------------------

def default_base_dir() -> str:
    """``%LOCALAPPDATA%/CodingStudio/online`` (or the XDG/macOS equivalent)."""
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    elif sys.platform == "darwin":
        root = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        root = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(root, "CodingStudio", "online")


def server_slug(server: str) -> str:
    """``https://studio.example.org:8770`` -> ``studio.example.org_8770``."""
    u = urllib.parse.urlsplit(server if "://" in server else "http://" + server)
    host = (u.hostname or "server").replace(":", "_")
    port = u.port or (443 if u.scheme == "https" else 80)
    return f"{host}_{port}"


def mirror_dir(server: str, project_id: str, base_dir: str | None = None) -> str:
    return os.path.join(base_dir or default_base_dir(), server_slug(server), project_id)


# --- atomic file helpers -----------------------------------------------------------

def fsync_dir(path: str) -> None:
    """fsync a directory entry (POSIX only; a no-op on Windows)."""
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def replace_retry(src: str, dst: str, attempts: int = 20) -> None:
    """``os.replace`` with a short retry: on Windows a reader (antivirus, the
    app) can hold the target open for a moment."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.05)


def atomic_write_bytes(path: str, data: bytes, tmp_dir: str | None = None, fsync: bool = True) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".w-", suffix=".tmp", dir=tmp_dir or d)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            if fsync:
                os.fsync(fh.fileno())
        replace_retry(tmp, path)
        tmp = ""
        if fsync:
            fsync_dir(d)
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def atomic_write_json(path: str, obj, fsync: bool = True) -> None:
    atomic_write_bytes(path, json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8"),
                       fsync=fsync)


# --- permissions (client mirror of §3.5 can_write) -------------------------------

@dataclass
class Permissions:
    """What this member may write, mirrored from ``GET /api/p/{pid}`` ``me``.

    Locks and done marks are *not* checked here - the server re-checks them
    and answers ``rejected/locked|forbidden``; the client-side rule only keeps
    edits the user can never push (derived files, other tags) local-only.
    """

    role: str = "viewer"
    tags: tuple = ()
    tasks: tuple = ()

    @classmethod
    def from_me(cls, me: dict | None) -> "Permissions":
        me = me or {}
        return cls(role=me.get("role") or "viewer", tags=tuple(me.get("tags") or ()),
                   tasks=tuple(me.get("tasks") or ()))

    def as_dict(self) -> dict:
        return {"role": self.role, "tags": list(self.tags), "tasks": list(self.tasks)}

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"

    def can_write(self, path: str, fc: protocol.FileClass | None = None) -> bool:
        fc = fc or protocol.classify(path)
        if fc.kind == "excluded":
            return False
        if self.is_owner:
            return True
        if self.role == "viewer":
            return False
        if fc.kind == "owned":
            return fc.tag in self.tags
        if fc.kind == "shared":
            return fc.task in self.tasks
        return False          # derived / admin / immutable: owner only


# --- scan results ---------------------------------------------------------------------

@dataclass
class Candidate:
    """A local change the syncer should queue (``new_hash None`` = delete)."""

    path: str
    base_hash: str | None
    new_hash: str | None
    size: int = 0
    mtime_ns: int = 0


@dataclass
class ScanResult:
    changes: list = field(default_factory=list)       # [Candidate] writable edits/creates/deletes
    local_only: list = field(default_factory=list)    # paths changed but not writable
    missing: list = field(default_factory=list)       # head without file, not a deletion
    files_seen: int = 0
    hashed: int = 0
    state_changed: bool = False                     # heads/local_only touched: save
    seconds: float = 0.0


# --- the mirror ----------------------------------------------------------------------------

class Mirror:
    """A mirror directory and its ``.studio/state.json``.

    ``lock`` (an ``RLock``) guards ``state``; every mutation of it happens
    under the lock, and :meth:`save` writes a consistent snapshot.
    """

    def __init__(self, root: str, fsync: bool = True):
        self.root = os.path.abspath(root)
        self.studio = os.path.join(self.root, STUDIO_DIR)
        self.pending_dir = os.path.join(self.studio, "pending")
        self.tmp_dir = os.path.join(self.studio, "tmp")
        self.conflicts_dir = os.path.join(self.studio, "conflicts")
        self.state_path = os.path.join(self.studio, "state.json")
        self.fsync = fsync
        self.lock = threading.RLock()
        self.hash_calls = 0
        self._fc_cache: dict[str, protocol.FileClass] = {}
        for d in (self.studio, self.pending_dir, self.tmp_dir, self.conflicts_dir):
            os.makedirs(d, exist_ok=True)
        self._clean_tmp()
        self.state = self._load()

    # -- state -----------------------------------------------------------------

    @staticmethod
    def _default_state() -> dict:
        return {
            "version": STATE_VERSION,
            "server": None, "project_id": None, "project_name": None, "username": None,
            "client_id": uuid.uuid4().hex,
            "seq": 0, "visibility_epoch": None,
            "heads": {}, "remote": {}, "local_only": {}, "stale": [], "restore": {},
            "pinned": [], "cache": {}, "me": {},
        }

    def _load(self) -> dict:
        state = self._default_state()
        try:
            with open(self.state_path, "rb") as fh:
                loaded = json.loads(fh.read().decode("utf-8"))
            if isinstance(loaded, dict):
                state.update(loaded)
        except FileNotFoundError:
            pass
        except (ValueError, UnicodeDecodeError):
            # A torn state file cannot happen with atomic replace; if it does
            # anyway keep it for inspection and start from an empty state: the
            # next full manifest re-adopts every file whose hash still matches.
            os.replace(self.state_path, self.state_path + f".corrupt-{int(time.time())}")
        return state

    def save(self) -> None:
        with self.lock:
            atomic_write_json(self.state_path, self.state, fsync=self.fsync)

    def _clean_tmp(self) -> None:
        for name in os.listdir(self.tmp_dir):
            try:
                os.unlink(os.path.join(self.tmp_dir, name))
            except OSError:
                pass
        for name in os.listdir(self.pending_dir):
            if name.startswith(".w-") or name.startswith(".p-"):
                try:
                    os.unlink(os.path.join(self.pending_dir, name))
                except OSError:
                    pass

    # -- paths -----------------------------------------------------------------

    def abspath(self, rel: str) -> str:
        rel = protocol.normalize_path(rel)
        full = os.path.abspath(os.path.join(self.root, *rel.split("/")))
        if not (full == self.root or full.startswith(self.root + os.sep)):
            raise ValueError(f"path escapes the mirror: {rel!r}")
        return full

    def classify(self, rel: str) -> protocol.FileClass:
        fc = self._fc_cache.get(rel)
        if fc is None:
            fc = protocol.classify(rel)
            if len(self._fc_cache) < 500_000:
                self._fc_cache[rel] = fc
        return fc

    # -- heads ---------------------------------------------------------------------

    @property
    def heads(self) -> dict:
        return self.state["heads"]

    @property
    def remote(self) -> dict:
        return self.state["remote"]

    def head_hash(self, rel: str) -> str | None:
        h = self.heads.get(rel)
        return h["hash"] if h else None

    def set_head(self, rel: str, digest: str, size: int | None = None, mtime_ns: int | None = None) -> None:
        """Record that the local file *rel* is server version *digest*.

        Without an explicit stat the current file is stat'ed; a missing file
        gets ``-1`` so the next scan looks at it again.
        """
        with self.lock:
            if size is None or mtime_ns is None:
                try:
                    st = os.stat(self.abspath(rel))
                    size, mtime_ns = st.st_size, st.st_mtime_ns
                except FileNotFoundError:
                    size, mtime_ns = -1, -1
            self.heads[rel] = {"hash": digest, "size": int(size), "mtime_ns": int(mtime_ns)}
            self.state["local_only"].pop(rel, None)

    def drop_head(self, rel: str) -> None:
        with self.lock:
            self.heads.pop(rel, None)
            self.state["local_only"].pop(rel, None)

    def add_stale(self, rel: str) -> None:
        with self.lock:
            if rel not in self.state["stale"]:
                self.state["stale"].append(rel)

    def discard_stale(self, rel: str) -> None:
        with self.lock:
            try:
                self.state["stale"].remove(rel)
            except ValueError:
                pass

    def local_state(self, rel: str) -> str:
        """``absent`` | ``clean`` (matches the head) | ``modified`` | ``untracked``.

        Stat-only fast path; a stat mismatch is confirmed by hashing, so a
        touched-but-identical file counts as clean (and its head stat is
        refreshed).
        """
        full = self.abspath(rel)
        try:
            st = os.stat(full)
        except FileNotFoundError:
            return "absent"
        with self.lock:
            head = self.heads.get(rel)
        if head is None:
            return "untracked"
        if head["size"] == st.st_size and head["mtime_ns"] == st.st_mtime_ns:
            return "clean"
        digest = self.hash_file(full)
        if digest == head["hash"]:
            self.set_head(rel, digest, st.st_size, st.st_mtime_ns)
            return "clean"
        return "modified"

    # -- hashing / pending copies ---------------------------------------------------------

    def hash_file(self, full: str) -> str:
        self.hash_calls += 1
        return protocol.sha256_file(full)

    def pending_path(self, digest: str) -> str:
        return os.path.join(self.pending_dir, digest)

    def has_pending(self, digest: str) -> bool:
        return os.path.exists(self.pending_path(digest))

    def copy_to_pending(self, full: str) -> tuple[str, int, str]:
        """Copy *full* into ``pending/`` while hashing it (one read).

        Returns ``(hash, size, tmp_path)``; :meth:`commit_pending` moves the
        temp file to ``pending/<hash>``, :meth:`discard_tmp` drops it.
        """
        self.hash_calls += 1
        h = hashlib.sha256()
        size = 0
        fd, tmp = tempfile.mkstemp(prefix=".p-", dir=self.pending_dir)
        try:
            with os.fdopen(fd, "wb") as out, open(full, "rb") as src:
                while True:
                    chunk = src.read(_CHUNK)
                    if not chunk:
                        break
                    h.update(chunk)
                    out.write(chunk)
                    size += len(chunk)
                out.flush()
                if self.fsync:
                    os.fsync(out.fileno())
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return h.hexdigest(), size, tmp

    def commit_pending(self, tmp: str, digest: str) -> None:
        dst = self.pending_path(digest)
        if os.path.exists(dst):
            os.unlink(tmp)
        else:
            replace_retry(tmp, dst)

    @staticmethod
    def discard_tmp(tmp: str | None) -> None:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)

    def put_pending_bytes(self, data: bytes) -> str:
        digest = protocol.sha256_bytes(data)
        if not self.has_pending(digest):
            atomic_write_bytes(self.pending_path(digest), data, fsync=self.fsync)
        return digest

    def read_version(self, digest: str, rel: str | None = None) -> bytes | None:
        """Bytes of version *digest*: from ``pending/`` or, when it still
        matches, from the working file."""
        p = self.pending_path(digest)
        try:
            with open(p, "rb") as fh:
                return fh.read()
        except FileNotFoundError:
            pass
        if rel:
            try:
                with open(self.abspath(rel), "rb") as fh:
                    data = fh.read()
                if protocol.sha256_bytes(data) == digest:
                    return data
            except FileNotFoundError:
                pass
        return None

    def remove_pending(self, digest: str | None) -> None:
        if digest:
            try:
                os.unlink(self.pending_path(digest))
            except FileNotFoundError:
                pass

    # -- writing server versions into the working tree -----------------------------------

    def install_file(self, rel: str, src_tmp: str, digest: str) -> None:
        """Move a verified download *src_tmp* to *rel* and record the head."""
        full = self.abspath(rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        replace_retry(src_tmp, full)
        st = os.stat(full)
        self.set_head(rel, digest, st.st_size, st.st_mtime_ns)

    def write_version(self, rel: str, data: bytes, digest: str | None = None) -> None:
        digest = digest or protocol.sha256_bytes(data)
        full = self.abspath(rel)
        atomic_write_bytes(full, data, tmp_dir=self.tmp_dir, fsync=self.fsync)
        st = os.stat(full)
        self.set_head(rel, digest, st.st_size, st.st_mtime_ns)

    def remove_file(self, rel: str) -> None:
        try:
            os.unlink(self.abspath(rel))
        except FileNotFoundError:
            pass
        self.drop_head(rel)

    # -- scanning --------------------------------------------------------------------------

    def walk(self) -> dict[str, os.stat_result]:
        """Every non-excluded file of the working tree, ``{rel: stat}``."""
        out: dict[str, os.stat_result] = {}
        stack = [("", self.root)]
        while stack:
            prefix, d = stack.pop()
            try:
                it = os.scandir(d)
            except (FileNotFoundError, NotADirectoryError, PermissionError):
                continue
            with it:
                for e in it:
                    rel = prefix + e.name
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if self.classify(rel + "/").kind != "excluded":
                                stack.append((rel + "/", e.path))
                            continue
                        if not e.is_file(follow_symlinks=False):
                            continue
                        if self.classify(rel).kind == "excluded":
                            continue
                        out[rel] = e.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
        return out

    def scan(self, perms: Permissions, pending: dict | None = None) -> ScanResult:
        """Compare the working tree with the heads; see the module docstring.

        *pending* is the outbox's ``{path: entry}`` view (entries carry the
        ``size``/``mtime_ns`` of the queued version, so a queued file that has
        not changed since is not hashed again).  State mutations (stat refresh,
        adopting files that equal the server version, ``local_only``) are
        applied here under the lock; queueing is the caller's job.
        """
        t0 = time.perf_counter()
        hashed0 = self.hash_calls
        pending = pending or {}
        res = ScanResult()
        files = self.walk()
        res.files_seen = len(files)

        with self.lock:
            heads = dict(self.heads)
            local_only = dict(self.state["local_only"])
            restore = dict(self.state.get("restore") or {})

        to_check: list[tuple[str, os.stat_result]] = []
        for rel, st in files.items():
            if rel in restore:
                continue            # waits for the server version (conflict restore)
            head = heads.get(rel)
            if head and head["size"] == st.st_size and head["mtime_ns"] == st.st_mtime_ns:
                continue
            lo = local_only.get(rel)
            if lo and lo["size"] == st.st_size and lo["mtime_ns"] == st.st_mtime_ns:
                continue
            pe = pending.get(rel)
            if pe and pe.get("new_hash") and pe.get("size") == st.st_size and pe.get("mtime_ns") == st.st_mtime_ns:
                continue
            to_check.append((rel, st))

        for rel, st in to_check:
            fc = self.classify(rel)
            writable = perms.can_write(rel, fc)
            full = os.path.join(self.root, *rel.split("/"))
            tmp = None
            try:
                if writable:
                    digest, size, tmp = self.copy_to_pending(full)
                    st2 = os.stat(full)
                    if st2.st_size != size or st2.st_mtime_ns != st.st_mtime_ns:
                        # written while we read it: look again next scan
                        self.discard_tmp(tmp)
                        continue
                else:
                    digest = self.hash_file(full)
            except FileNotFoundError:
                self.discard_tmp(tmp)
                continue
            with self.lock:
                head = self.heads.get(rel)
                remote = self.remote.get(rel)
                if head and digest == head["hash"]:
                    head["size"], head["mtime_ns"] = st.st_size, st.st_mtime_ns
                    self.state["local_only"].pop(rel, None)
                    self.discard_tmp(tmp)
                    if rel in pending:          # reverted to the synced version
                        res.changes.append(Candidate(rel, head["hash"], digest, st.st_size, st.st_mtime_ns))
                    continue
                if head is None and remote and not remote.get("deleted") and remote.get("hash") == digest \
                        and rel not in pending:
                    # the file already is the server version (first open, evicted+restored ...)
                    self.heads[rel] = {"hash": digest, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
                    self.discard_tmp(tmp)
                    continue
                if writable:
                    self.commit_pending(tmp, digest)
                    res.changes.append(Candidate(rel, head["hash"] if head else None, digest,
                                                 st.st_size, st.st_mtime_ns))
                else:
                    self.state["local_only"][rel] = {"hash": digest, "size": st.st_size,
                                                     "mtime_ns": st.st_mtime_ns}
                    res.local_only.append(rel)

        # heads whose file vanished
        for rel, head in heads.items():
            if rel in files or rel in restore:
                continue
            fc = self.classify(rel)
            if fc.kind == "excluded":
                continue
            pe = pending.get(rel)
            if pe is not None and pe.get("new_hash") is None:
                continue                                   # deletion already queued
            if fc.kind in ("owned", "shared") and perms.can_write(rel, fc):
                res.changes.append(Candidate(rel, head["hash"], None, 0, 0))
            else:
                # not a deletion (evicted, or not ours to delete): forget the
                # head; core files are downloaded again by the next pull.
                with self.lock:
                    self.heads.pop(rel, None)
                    self.state["local_only"].pop(rel, None)
                    if protocol.is_core(rel, fc) and rel in self.remote:
                        self.add_stale(rel)
                res.missing.append(rel)

        # created-then-deleted before it was ever pushed: cancel the creation
        for rel, pe in pending.items():
            if rel in files or rel in heads or pe.get("new_hash") is None:
                continue
            res.changes.append(Candidate(rel, pe.get("base_hash"), None, 0, 0))

        res.hashed = self.hash_calls - hashed0
        res.state_changed = bool(res.hashed or res.missing)
        res.seconds = time.perf_counter() - t0
        return res
