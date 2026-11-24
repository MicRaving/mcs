"""``NextcloudClient`` - the "Nextcloud without server" backend (PLAN §13).

The same desktop app, the same sync engine (``store``/``syncer``/``mirror``/
``outbox``/``cache``/``conflicts``), a different *backend*: instead of the
studio server's REST API this class speaks WebDAV to one shared folder on a
Nextcloud (or any WebDAV share).  It exposes exactly the method surface
:class:`~.client.ServerClient` exposes, so ``Syncer`` cannot tell the two
apart - which is the whole point: the outbox, the crash safety, the three-way
merge, the cache tiers and the UI are the server-mode code, unchanged.

Layout inside the shared folder (documented here because it is the contract
other clients rely on)::

    <shared folder>/                     plain project tree - unchanged, so a
      Project/ Codings/ Extraction/ ...  Nextcloud desktop-sync user sees
      Papers/ Text/ Datasets/ ...        ordinary files and can open them
      master_dataset.xlsx
    <shared folder>/.studio/             the mode's own state (never synced as
      members.json                       project content: ``.studio`` is in the
      settings.json                      protocol's *excluded* class)
      assignments.json                   admin-written, honour system
      locks/<key>.json                   one file per locked study key
      presence/<client>.json             heartbeat, TTL 300 s
      done/<tag>.json                    "Finish paper" marks, one file per tag
      conflicts/                         reserved (conflict bytes stay in the
                                         client's local .studio/conflicts/)
      nextcloud_index.json               *client-local* mirror of this folder,
                                         written to the mirror, not to Nextcloud
                                         (it lives at <mirror>/.studio/…)

Every file in ``.studio/`` is small JSON written with a conditional PUT, so a
member can read the locks, the presence and the assignments with ordinary
Nextcloud tools.  ``members.json``::

    {"members": [{"username": "anna", "display_name": "Anna",
                  "role": "owner"|"coder"|"viewer",
                  "tags": ["AN"], "tasks": ["coding", "settle"]}]}

``settings.json``::

    {"blinding": {"abstract": "blinded", "fulltext": "blinded", "coding": "blinded"},
     "lock_ttl_h": 24, "max_offline_lease_h": 72}

``locks/<key>.json``::

    {"key": "Smith (2020)", "user": "anna", "display_name": "Anna",
     "client_id": "…", "acquired": 1700000000.0, "renewed": 1700000000.0,
     "expires": 1700086400.0, "expires_iso": "2026-11-15T12:00:00Z", "offline": false}

``done/<tag>.json``::

    {"tag": "AN", "marks": {"Smith (2020)|coding": 1700000000.0}}

Guarantees (PLAN §13, and ``status()`` says so): freshness by ETag comparison,
locks atomic **only where the server evaluates ``If-None-Match: *`` atomically**
(the spike measures it; otherwise best-effort), tag ownership / blinding / roles
are honour system, conflicts merge locally against a base kept in the mirror.

Identity: the Nextcloud username plus an **app password**; WebDAV root
``https://<host>/remote.php/dav/files/<user>/<folder>``; Basic auth over HTTPS
only (``http://`` is refused except for a loopback test server).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import urllib.parse

from online_shared import merge as merge_mod
from online_shared import protocol

from .errors import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    LockedError,
    NotFoundError,
    OfflineError,
    OnlineError,
    ServerError,
)
from .webdav import DepthInfinityRefused, PreconditionFailed, WebDAV

STUDIO = ".studio"
LOCK_DIR = f"{STUDIO}/locks"
PRESENCE_DIR = f"{STUDIO}/presence"
DONE_DIR = f"{STUDIO}/done"
MEMBERS_FILE = f"{STUDIO}/members.json"
SETTINGS_FILE = f"{STUDIO}/settings.json"
ASSIGNMENTS_FILE = f"{STUDIO}/assignments.json"

BACKEND = "nextcloud"
GUARANTEES = "best-effort"

DEFAULT_SETTINGS = {
    "blinding": {stage: "visible" for stage in protocol.STAGES},   # UI-only in this mode
    "lock_ttl_h": protocol.DEFAULT_LOCK_TTL_H,
    "max_offline_lease_h": protocol.DEFAULT_MAX_OFFLINE_LEASE_H,
}

#: A non-core file's version token: Nextcloud has no content hashes, so the
#: server's ETag stands in until somebody actually downloads the bytes.
TOKEN_PREFIX = "nc1-"

MAX_SPOOL_BYTES = 64 * 1024 * 1024      # keep merged/uploaded JSON+CSV in memory-spooled files
MAX_BASE_BYTES = 16 * 1024 * 1024       # a locally kept merge base


# ---------------------------------------------------------------------------
# paths / identity helpers
# ---------------------------------------------------------------------------

def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_file(path: str) -> str:
    return protocol.sha256_file(path)


def _is_content_hash(digest: str | None) -> bool:
    return bool(digest) and len(digest) == 64 and not str(digest).startswith(TOKEN_PREFIX)


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))


def webdav_root(url: str, username: str, folder: str = "") -> str:
    """``https://host`` + *username* + *folder* -> the WebDAV folder URL.

    A URL that already names a WebDAV location (``…/remote.php/dav/…``) is used
    as given; the folder is appended unless the URL already ends with it.
    """
    u = (url or "").strip().rstrip("/")
    if not u:
        raise ValueError("a Nextcloud URL is required")
    if "/remote.php/" in u or u.endswith("/dav"):
        base = u
    else:
        user = urllib.parse.quote(username or "", safe="")
        base = f"{u}/remote.php/dav/files/{user}"
    f = "/".join(urllib.parse.quote(part, safe="")
                 for part in (folder or "").strip("/").split("/") if part)
    if not f:
        return base
    if base.rstrip("/").endswith("/" + f):
        return base
    return f"{base}/{f}"


def origin_of(url: str) -> str:
    """``https://host:port`` of *url* (the "server" key of the mirror)."""
    u = urllib.parse.urlsplit(url if "://" in url else "https://" + url)
    port = f":{u.port}" if u.port else ""
    return f"{u.scheme or 'https'}://{u.hostname or ''}{port}"


def project_id_for(root_url: str) -> str:
    """A stable, filesystem-safe project id for one shared folder."""
    return "nc-" + hashlib.sha1(root_url.strip().rstrip("/").encode("utf-8")).hexdigest()[:16]


_SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ._()&',+-")


def safe_name(text: str, limit: int = 80) -> str:
    """A WebDAV/Windows-safe file name that stays deterministic for *text*.

    Two different study keys can never collide: the readable prefix is capped
    and a short hash of the full key is appended.
    """
    slug = "".join(c if c in _SAFE else "_" for c in str(text)).strip(" .") or "x"
    return f"{slug[:limit]}-{hashlib.sha1(str(text).encode('utf-8')).hexdigest()[:10]}"


def lock_path(key: str) -> str:
    return f"{LOCK_DIR}/{safe_name(key)}.json"


def presence_path(client_id: str) -> str:
    return f"{PRESENCE_DIR}/{safe_name(client_id, 40)}.json"


def done_path(tag: str) -> str:
    return f"{DONE_DIR}/{safe_name(tag, 40)}.json"


def _json_bytes(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


def _parse_json(data: bytes | None, default):
    if not data:
        return default
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return default


def _content_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return {".json": "application/json", ".csv": "text/csv", ".md": "text/markdown",
            ".txt": "text/plain", ".pdf": "application/pdf",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            }.get(ext, "application/octet-stream")


def _atomic_write(path: str, data: bytes) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".nc-", suffix=".part", dir=d)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        tmp = ""
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# the backend
# ---------------------------------------------------------------------------

class NextcloudClient:
    """The studio client contract implemented on top of one WebDAV folder."""

    #: ``status()`` reports these so the badge can say what the mode promises.
    backend = BACKEND
    guarantees = GUARANTEES

    # -- construction ------------------------------------------------------------

    def __init__(self, url: str, username: str, app_password: str, folder: str = "",
                 *, webdav_root_url: str | None = None, timeout: float = 30.0,
                 allow_insecure_http: bool = False, clock=time.time, dav: WebDAV | None = None,
                 app_version: str = ""):
        self.username = username or ""
        self.display_name = self.username
        self._password = app_password or ""
        self.token = self._password            # the store saves this in the keyring
        self.server_url = origin_of(url)
        self.folder = (folder or "").strip("/")
        self.root_url = (webdav_root_url or webdav_root(url, self.username, self.folder)).rstrip("/")
        self.project_id = project_id_for(self.root_url)
        self.app_version = app_version or ""
        self.clock = clock
        self.timeout = timeout
        self.dav = dav or WebDAV(self.root_url, self.username, self._password,
                                 timeout=timeout, allow_insecure_http=allow_insecure_http)
        self._guard = threading.RLock()
        self._projects: dict[str, dict] = {}
        self._mirror = None
        self._index: dict = {"version": 1, "seq": 0, "files": {}}
        self._index_loaded = False
        self._project_cache: dict | None = None
        self._project_cache_at = 0.0
        self._member_cache: dict | None = None
        self._member_cache_at = 0.0
        self._spool: dict[str, str] = {}
        self._spool_dir = ""
        self._base_dir = ""
        if self.folder:
            self._projects[self.project_id] = {"root": self.folder, "name": self._folder_name()}

    def __repr__(self) -> str:                      # never leak the app password
        return f"NextcloudClient({self.root_url!r}, user={self.username!r})"

    def _folder_name(self) -> str:
        return self.folder.rsplit("/", 1)[-1] if self.folder else (self.dav.host or "Nextcloud")

    # -- wiring -------------------------------------------------------------------

    def bind_mirror(self, mirror) -> None:
        """Give the client the local mirror: index, merge bases and the spool.

        Called by :meth:`online.store.OnlineStore.open` right after the mirror
        exists.  Everything the client keeps for itself lives under
        ``<mirror>/.studio/`` so it survives a restart.
        """
        self._mirror = mirror
        studio = mirror.studio
        self._index_path = os.path.join(studio, "nextcloud_index.json")
        self._base_dir = os.path.join(studio, "nc-base")
        self._spool_dir = os.path.join(studio, "nc-spool")
        os.makedirs(self._base_dir, exist_ok=True)
        os.makedirs(self._spool_dir, exist_ok=True)
        self._index_loaded = False
        self._index = {"version": 1, "seq": 0, "files": {}}
        self._load_index()

    def set_offline(self, offline: bool = True) -> None:
        """Test/output seam: pretend the network is down (no request leaves)."""
        self.dav.offline = bool(offline)

    def close(self) -> None:
        with self._guard:
            self._save_index()

    # -- index (the client's own view of the folder) --------------------------------

    def _load_index(self) -> None:
        with self._guard:
            if self._index_loaded:
                return
            self._index_loaded = True
            path = getattr(self, "_index_path", "")
            data = None
            if path and os.path.exists(path):
                try:
                    with open(path, "rb") as fh:
                        data = json.loads(fh.read().decode("utf-8"))
                except (OSError, ValueError, UnicodeDecodeError):
                    data = None
            if isinstance(data, dict) and isinstance(data.get("files"), dict):
                self._index = {"version": 1, "seq": int(data.get("seq") or 0),
                               "files": {str(k): dict(v) for k, v in data["files"].items()
                                         if isinstance(v, dict)}}
            else:
                self._index = {"version": 1, "seq": 0, "files": {}}

    def _save_index(self) -> None:
        path = getattr(self, "_index_path", "")
        if not path:
            return
        with self._guard:
            payload = {"version": 1, "seq": int(self._index.get("seq") or 0),
                       "files": self._index.get("files") or {}}
            _atomic_write(path, _json_bytes(payload))

    def _index_files(self) -> dict:
        self._load_index()
        return self._index["files"]

    def _bump_seq(self) -> int:
        with self._guard:
            seq = int(self._index.get("seq") or 0) + 1
            self._index["seq"] = seq
            return seq

    def _now_seq(self) -> int:
        with self._guard:
            return int(self._index.get("seq") or 0)

    # -- spool: bytes we hold for a hash -------------------------------------------

    def _spool_path(self, digest: str) -> str:
        if not self._spool_dir:
            self._spool_dir = os.path.join(tempfile.gettempdir(), f"nc-spool-{os.getpid()}")
        return os.path.join(self._spool_dir, digest.replace(":", "_"))

    def _spool_bytes(self, digest: str, data: bytes, path: str | None = None) -> str:
        """Keep *data* readable under *digest* (session spool + merge base)."""
        target = self._spool_path(digest)
        try:
            if not os.path.exists(target):
                _atomic_write(target, data)
            self._spool[digest] = target
        except OSError:
            pass
        if path and protocol.classify(path).kind in ("shared", "owned") and len(data) <= MAX_BASE_BYTES:
            self._keep_base(digest, data)
        return target

    def _base_path(self, digest: str) -> str:
        return os.path.join(self._base_dir, digest) if self._base_dir else ""

    def _keep_base(self, digest: str, data: bytes) -> None:
        """Persist the last synced bytes of a shared file: the merge base.

        The studio server keeps every blob; a Nextcloud folder keeps only the
        current version, so the base of a three-way merge has to live in the
        mirror (PLAN §13: "same merge, base kept locally").
        """
        if not self._base_dir or not _is_content_hash(digest):
            return
        target = self._base_path(digest)
        if os.path.exists(target):
            return
        try:
            _atomic_write(target, data)
        except OSError:
            pass

    def _version_bytes(self, digest: str | None, path: str | None = None) -> bytes | None:
        if not digest:
            return None
        p = self._spool.get(digest)
        if p:
            try:
                with open(p, "rb") as fh:
                    return fh.read()
            except OSError:
                pass
        bp = self._base_path(digest)
        if bp and os.path.exists(bp):
            try:
                with open(bp, "rb") as fh:
                    return fh.read()
            except OSError:
                pass
        if self._mirror is not None:
            try:
                return self._mirror.read_version(digest, path)
            except OSError:
                return None
        return None

    def _path_for_token(self, digest: str) -> str | None:
        for rel, entry in self._index_files().items():
            if entry.get("hash") == digest:
                return rel
        return None

    # -- auth / identity --------------------------------------------------------------

    def login(self, username: str | None = None, password: str | None = None) -> dict:
        """Verify the credentials and the folder with one PROPFIND."""
        if username and username != self.username:
            raise ValueError("this client is bound to one Nextcloud user")
        self.dav.stat("")                   # raises AuthError/NotFoundError
        return {"token": self._password,
                "user": {"id": self.username, "username": self.username,
                         "display_name": self.display_name}}

    def me(self) -> dict:
        user = {"id": self.username, "username": self.username,
                "display_name": self.display_name}
        return {"user": user, "projects": self.projects()}

    def logout(self) -> None:
        self.token = None

    def projects(self) -> list[dict]:
        """Exactly one project: the WebDAV folder this client points at."""
        me = self._member()
        return [{"id": self.project_id, "name": self._folder_name(),
                 "role": me.get("role") or "owner"}]

    def _resolve(self, pid: str) -> dict:
        """The folder row of *pid* (one folder per client)."""
        return self._projects.get(pid) or {"root": self.folder, "name": self._folder_name()}

    def _use(self, pid: str) -> None:
        """One folder per client: *pid* must be this client's project."""
        if pid and pid != self.project_id:
            raise NotFoundError(f"this Nextcloud share holds the project {self.project_id!r}",
                                404, "not_found")

    # -- project (members / settings - honour system) -----------------------------------

    def project(self, pid: str) -> dict:
        self._use(pid)
        now = self.clock()
        with self._guard:
            if self._project_cache is not None and now - self._project_cache_at < 5.0:
                return dict(self._project_cache)
        info = {
            "id": pid,
            "name": self._projects.get(pid, {}).get("name") or self._folder_name(),
            "settings": self._settings(),
            "me": self._member(),
            "protocol": protocol.PROTOCOL_VERSION,
            "backend": BACKEND,
            "guarantees": GUARANTEES,
            "folder": self.folder or "/",
        }
        with self._guard:
            self._project_cache = info
            self._project_cache_at = now
        return dict(info)

    def _invalidate_project(self) -> None:
        with self._guard:
            self._project_cache = None
            self._member_cache = None

    def _read_json(self, path: str, default=None):
        try:
            data = self.dav.get(path)
        except NotFoundError:
            return default
        return _parse_json(data, default)

    def _settings(self) -> dict:
        raw = self._read_json(SETTINGS_FILE, {}) or {}
        out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_SETTINGS.items()}
        if isinstance(raw, dict):
            if isinstance(raw.get("blinding"), dict):
                out["blinding"].update({k: str(v) for k, v in raw["blinding"].items()})
            for key in ("lock_ttl_h", "max_offline_lease_h"):
                try:
                    if raw.get(key) is not None:
                        out[key] = float(raw[key])
                except (TypeError, ValueError):
                    pass
            for key, value in raw.items():
                if key not in out:
                    out[key] = value
        return out

    def _member(self) -> dict:
        now = self.clock()
        with self._guard:
            if self._member_cache is not None and now - self._member_cache_at < 5.0:
                return dict(self._member_cache)
        row = self._read_member()
        with self._guard:
            self._member_cache = row
            self._member_cache_at = now
        return dict(row)

    def _read_member(self) -> dict:
        raw = self._read_json(MEMBERS_FILE, None)
        rows = raw.get("members") if isinstance(raw, dict) else raw
        if isinstance(rows, dict):
            rows = [dict(v, username=k) for k, v in rows.items()]
        rows = [r for r in (rows or []) if isinstance(r, dict)]
        want = self.username.lower()
        for row in rows:
            name = str(row.get("username") or row.get("user") or "").lower()
            if name and name == want:
                return {"role": str(row.get("role") or "coder"),
                        "tags": [str(t) for t in (row.get("tags") or [])],
                        "tasks": [str(t) for t in (row.get("tasks") or protocol.TASKS)],
                        "display_name": str(row.get("display_name") or self.display_name),
                        "user": self.username}
        # No member list, or this user is not on it: the folder is a shared
        # drive, so the file system decides.  Honour system = full owner rights,
        # flagged so the UI can say "not listed in .studio/members.json".
        return {"role": "owner", "tags": [self.username] if self.username else [],
                "tasks": list(protocol.TASKS), "display_name": self.display_name,
                "user": self.username, "unlisted": bool(rows)}

    def _can_write(self, fc: protocol.FileClass) -> bool:
        if fc.kind == "excluded":
            return False
        me = self._member()
        if me.get("role") == "owner":
            return True
        if me.get("role") == "viewer":
            return False
        if fc.kind == "owned":
            return fc.tag in (me.get("tags") or [])
        if fc.kind == "shared":
            return fc.task in (me.get("tasks") or [])
        return False

    # -- manifest (freshness by ETag) -----------------------------------------------------

    def list_remote(self) -> dict[str, object]:
        """``{rel: Prop}`` of every non-excluded file in the folder.

        One ``PROPFIND Depth: infinity``; servers that refuse it (university
        shares often do) are walked with ``Depth: 1`` per folder instead.
        """
        try:
            rows = self.dav.propfind("", depth="infinity")
        except DepthInfinityRefused:
            rows = self._walk_depth1()
        out: dict[str, object] = {}
        for prop in rows:
            if prop.is_dir or not prop.path:
                continue
            if protocol.classify(prop.path).kind == "excluded":
                continue
            out[prop.path] = prop
        return out

    def _walk_depth1(self) -> list:
        rows: list = []
        queue = [""]
        while queue:
            folder = queue.pop(0)
            try:
                children = self.dav.propfind(folder, depth="1")
            except NotFoundError:
                continue
            for prop in children:
                if prop.path == folder or not prop.path:
                    continue
                if prop.is_dir:
                    if protocol.classify(prop.path + "/").kind != "excluded":
                        queue.append(prop.path)
                    continue
                rows.append(prop)
        return rows

    def manifest(self, pid: str, since: int = 0) -> dict:
        self._use(pid)
        since = int(since or 0)
        listing = self.list_remote()
        seq = 0
        out: list[dict] = []
        with self._guard:
            files = self._index_files()
            seq = int(self._index.get("seq") or 0)
            for rel, prop in listing.items():
                entry = files.get(rel)
                if entry and self._unchanged(entry, prop):
                    pass
                else:
                    entry = self._refresh_entry(rel, prop, entry)
                    if entry.get("seq") is None:
                        seq += 1
                        entry["seq"] = seq
                    files[rel] = entry
                if since == 0 or int(entry.get("seq") or 0) > since:
                    fc = protocol.classify(rel)
                    out.append({"path": rel, "hash": entry["hash"], "size": entry.get("size") or 0,
                                "kind": fc.kind, "key": fc.key, "tag": fc.tag,
                                "deleted": False, "seq": entry.get("seq")})
            for rel in [p for p in files if p not in listing]:
                files.pop(rel)
                seq += 1
                # also on a full pull: a deletion must propagate, otherwise a
                # stale local copy would be pushed back as a new file
                out.append({"path": rel, "hash": None, "size": 0, "deleted": True, "seq": seq})
            self._index["seq"] = seq
            self._save_index()
        return {"seq": seq, "visibility_epoch": BACKEND, "files": out}

    @staticmethod
    def _unchanged(entry: dict, prop) -> bool:
        if entry.get("etag") and prop.etag:
            return entry["etag"] == prop.etag and int(entry.get("size") or 0) == int(prop.size or 0)
        return (prop.etag is None and entry.get("etag") is None
                and int(entry.get("size") or 0) == int(prop.size or 0)
                and entry.get("lm") == prop.last_modified)

    def _refresh_entry(self, rel: str, prop, old: dict | None) -> dict:
        """Recompute one path's version token after the server reported a change."""
        fc = protocol.classify(rel)
        etag = prop.etag
        if protocol.is_core(rel, fc):
            data = self.dav.get(rel)          # the only way to learn the content hash
            digest = _sha_bytes(data or b"")
            self._spool_bytes(digest, data or b"", rel)
            self._keep_base(digest, data or b"")
            if old and old.get("hash") == digest:
                return {"hash": digest, "size": len(data or b""), "etag": etag,
                        "lm": prop.last_modified, "seq": old.get("seq")}
            return {"hash": digest, "size": len(data or b""), "etag": etag,
                    "lm": prop.last_modified, "seq": None}
        # Large / on-demand classes: never downloaded to learn a hash.  The
        # server's ETag is the version token until somebody calls ensure_local.
        token = TOKEN_PREFIX + (etag or f"{prop.size}-{prop.last_modified or 0}")
        return {"hash": token, "size": prop.size, "etag": etag,
                "lm": prop.last_modified, "seq": old.get("seq") if old and old.get("hash") == token else None}

    # -- blobs: the engine uploads before it pushes, downloads after ---------------------

    def blobs_missing(self, pid: str, hashes: list[str]) -> list[str]:
        return [h for h in hashes if self._version_bytes(h) is None]

    def put_blob(self, pid: str, digest: str, data: bytes, compress: bool = True) -> None:
        self._spool_bytes(digest, data)

    def put_blob_file(self, pid: str, digest: str, path: str, compress: bool = True,
                      small_bytes: int = 8 * 1024 * 1024) -> None:
        size = os.path.getsize(path)
        if size > MAX_SPOOL_BYTES:
            return
        with open(path, "rb") as fh:
            self._spool_bytes(digest, fh.read())

    def get_blob(self, pid: str, digest: str, dest_path: str) -> str:
        data = self._version_bytes(digest)
        if data is None:
            rel = self._path_for_token(digest)
            if rel is None:
                raise NotFoundError(f"no version {str(digest)[:16]} on the share", 404, "not_found")
            d = os.path.dirname(dest_path) or "."
            os.makedirs(d, exist_ok=True)
            self.dav.get(rel, dest=dest_path)
            if _is_content_hash(digest) and _sha_file(dest_path) != digest:
                raise ServerError(f"version {str(digest)[:12]} arrived corrupted", 200, "bad_blob")
            return dest_path
        _atomic_write(dest_path, data)
        if _is_content_hash(digest) and _sha_bytes(data) != digest:
            raise ServerError(f"version {str(digest)[:12]} arrived corrupted", 200, "bad_blob")
        return dest_path

    def get_blob_bytes(self, pid: str, digest: str) -> bytes:
        data = self._version_bytes(digest)
        if data is not None:
            return data
        rel = self._path_for_token(digest)
        if rel is None:
            raise NotFoundError(f"no version {str(digest)[:16]} on the share", 404, "not_found")
        data = self.dav.get(rel) or b""
        if _is_content_hash(digest) and _sha_bytes(data) != digest:
            raise ServerError(f"version {str(digest)[:12]} arrived corrupted", 200, "bad_blob")
        return data

    # -- push --------------------------------------------------------------------------

    def push(self, pid: str, client_id: str, entries: list[dict]) -> dict:
        self._use(pid)
        results = [self._push_entry(e, client_id) for e in entries]
        self._save_index()
        return {"seq": self._now_seq(), "results": results}

    def _push_entry(self, e: dict, client_id: str) -> dict:
        path = protocol.normalize_path(e.get("path") or "")
        base, new = e.get("base_hash"), e.get("new_hash")
        fc = protocol.classify(path)
        if fc.kind == "excluded":
            return {"path": path, "status": "rejected", "reason": "excluded"}
        if not self._can_write(fc):
            return {"path": path, "status": "rejected", "reason": "forbidden",
                    "hash": self._current_hash(path)}
        if fc.kind == "shared" and fc.key:
            holder = self._lock_holder(fc.key)
            if holder is not None and holder.get("user") != self.username:
                return {"path": path, "status": "rejected", "reason": "locked",
                        "hash": self._current_hash(path)}
        try:
            return self._push_once(path, fc, base, new, client_id)
        except PreconditionFailed:
            # the head moved between our stat/read and our PUT: retry **with the
            # original base**, so the other side's change is merged instead of
            # being overwritten (rebasing on the new head would drop it)
            try:
                return self._push_once(path, fc, base, new, client_id, merge_only=True)
            except PreconditionFailed:
                return {"path": path, "status": "conflict", "reason": "precondition_failed",
                        "hash": self._current_hash(path), "conflict_id": None}

    def _push_once(self, path: str, fc, base: str | None, new: str | None, client_id: str,
                   merge_only: bool = False) -> dict:
        cur = self._stat(path)
        idx = self._index_files().get(path)
        if new is None:
            return self._push_delete(path, fc, base, cur, idx, merge_only=merge_only)
        ours = self._version_bytes(new, path)
        if ours is None:
            return {"path": path, "status": "rejected", "reason": "missing_blob"}

        if cur is None and not merge_only:                 # create
            self._ensure_parent(path)
            try:
                resp = self.dav.put(path, ours, if_none_match=True, content_type=_content_type(path))
            except PreconditionFailed:
                cur = self._stat(path)                    # created meanwhile: merge below
            else:
                self._record_write(path, new, resp.etag or self._etag_of(path), len(ours))
                return {"path": path, "status": "accepted", "hash": new, "seq": self._now_seq()}

        if cur is not None and not merge_only and self._base_is_head(idx, base, cur):
            try:
                resp = self.dav.put(path, ours, if_match=cur.etag, content_type=_content_type(path))
            except PreconditionFailed:
                cur = self._stat(path)                    # raced: merge below
            else:
                self._record_write(path, new, resp.etag or self._etag_of(path), len(ours))
                return {"path": path, "status": "accepted", "hash": new, "seq": self._now_seq()}

        if cur is None:                                   # it vanished under us
            return {"path": path, "status": "conflict", "reason": "vanished", "hash": None,
                    "conflict_id": None}

        theirs = self.dav.get(path) or b""
        theirs_hash = _sha_bytes(theirs)
        self._spool_bytes(theirs_hash, theirs, path)
        if theirs_hash == new or theirs == ours:
            self._record_write(path, new, cur.etag, len(ours))
            return {"path": path, "status": "accepted", "hash": new, "seq": self._now_seq()}

        base_bytes = self._version_bytes(base, path) if base else None
        merged = None
        if base_bytes is not None:
            merged = merge_mod.merge3(path, base_bytes, ours, theirs)
        if merged is not None and merged.ok and merged.merged is not None:
            data = merged.merged
            digest = _sha_bytes(data)
            self._spool_bytes(digest, data, path)
            try:
                resp = self.dav.put(path, data, if_match=cur.etag, content_type=_content_type(path))
            except PreconditionFailed:
                return {"path": path, "status": "conflict", "reason": "precondition_failed",
                        "hash": theirs_hash, "conflict_id": None}
            self._record_write(path, digest, resp.etag or self._etag_of(path), len(data))
            return {"path": path, "status": "merged", "hash": digest, "seq": self._now_seq()}

        # unresolvable: a local conflict record (the engine's own conflict UI)
        return {"path": path, "status": "conflict", "reason": "merge_conflict",
                "hash": theirs_hash, "conflict_id": None}

    def _push_delete(self, path: str, fc, base: str | None, cur, idx: dict | None,
                     merge_only: bool = False) -> dict:
        if cur is None:
            self._drop_index(path)
            return {"path": path, "status": "accepted", "hash": None, "seq": self._now_seq()}
        if not merge_only and self._base_is_head(idx, base, cur):
            try:
                self.dav.delete(path, if_match=cur.etag)
            except NotFoundError:
                pass
            except PreconditionFailed:
                return {"path": path, "status": "conflict", "reason": "precondition_failed",
                        "hash": self._current_hash(path), "conflict_id": None}
            self._drop_index(path)
            return {"path": path, "status": "accepted", "hash": None, "seq": self._now_seq()}
        theirs = self.dav.get(path) or b""
        theirs_hash = _sha_bytes(theirs)
        self._spool_bytes(theirs_hash, theirs, path)
        if base and theirs_hash == base:                  # content unchanged: delete it
            try:
                self.dav.delete(path, if_match=cur.etag)
            except NotFoundError:
                pass
            except PreconditionFailed:
                return {"path": path, "status": "conflict", "reason": "precondition_failed",
                        "hash": self._current_hash(path), "conflict_id": None}
            self._drop_index(path)
            return {"path": path, "status": "accepted", "hash": None, "seq": self._now_seq()}
        return {"path": path, "status": "conflict", "reason": "deleted_locally_changed_remotely",
                "hash": theirs_hash, "conflict_id": None}

    @staticmethod
    def _base_is_head(idx: dict | None, base: str | None, cur) -> bool:
        if not idx or base is None or not cur or not cur.etag:
            return False
        return idx.get("hash") == base and idx.get("etag") == cur.etag

    def _ensure_parent(self, path: str) -> None:
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        if parent:
            try:
                self.dav.makedirs(parent)
            except OnlineError:
                pass

    def _stat(self, path: str):
        try:
            return self.dav.stat(path)
        except NotFoundError:
            return None

    def _etag_of(self, path: str) -> str | None:
        prop = self._stat(path)
        return prop.etag if prop else None

    def _current_hash(self, path: str) -> str | None:
        try:
            data = self.dav.get(path)
        except NotFoundError:
            return None
        if data is None:
            return None
        digest = _sha_bytes(data)
        self._spool_bytes(digest, data, path)
        return digest

    def _record_write(self, path: str, digest: str, etag: str | None, size: int) -> None:
        with self._guard:
            seq = self._bump_seq()
            self._index_files()[path] = {"hash": digest, "size": int(size or 0),
                                         "etag": etag, "lm": None, "seq": seq}

    def _drop_index(self, path: str) -> None:
        with self._guard:
            self._index_files().pop(path, None)
            self._bump_seq()

    # -- conflicts (local records only; nothing to list on the share) --------------------

    def conflicts(self, pid: str, state: str | None = "open") -> list[dict]:
        return []

    def resolve_conflict(self, pid: str, conflict_id, resolution: str, new_hash: str | None = None) -> dict:
        raise BadRequestError("Nextcloud mode resolves conflicts locally", 400, "bad_request")

    # -- locks ----------------------------------------------------------------------------

    def _lock_row(self, lock: dict) -> dict:
        row = dict(lock)
        try:
            expires = float(row.get("expires") or 0)
        except (TypeError, ValueError):
            expires = 0.0
        row["expires"] = expires
        row["expired"] = expires <= self.clock()
        return row

    def _lock_holder(self, key: str) -> dict | None:
        lock = self._read_json(lock_path(key), None)
        if not isinstance(lock, dict):
            return None
        row = self._lock_row(lock)
        return None if row["expired"] else row

    def locks(self, pid: str) -> list[dict]:
        self._use(pid)
        rows: list[dict] = []
        try:
            entries = self.dav.propfind(LOCK_DIR, depth="1")
        except NotFoundError:
            return []
        for prop in entries:
            if prop.is_dir or not prop.path.endswith(".json"):
                continue
            lock = self._read_json(prop.path, None)
            if isinstance(lock, dict) and lock.get("key"):
                rows.append(self._lock_row(lock))
        return rows

    def acquire_lock(self, pid: str, key: str, client_id: str,
                     offline_hours: float | None = None) -> dict:
        self._use(pid)
        me = self._member()
        if me.get("role") != "owner" and not set(me.get("tasks") or []) & {"settle", "harmonize", "reproduce"}:
            raise ForbiddenError("this member has no task that writes a shared file", 403, "forbidden")
        settings = self._settings()
        hours = float(settings.get("lock_ttl_h") or protocol.DEFAULT_LOCK_TTL_H)
        offline = bool(offline_hours)
        if offline:
            hours = min(float(offline_hours), float(settings.get("max_offline_lease_h")
                                                   or protocol.DEFAULT_MAX_OFFLINE_LEASE_H))
        path = lock_path(key)
        for _attempt in range(4):
            now = self.clock()
            row = {"key": str(key), "user": self.username,
                   "display_name": me.get("display_name") or self.display_name,
                   "client_id": client_id, "acquired": now, "renewed": now,
                   "expires": now + hours * 3600.0, "expires_iso": _iso(now + hours * 3600.0),
                   "offline": offline}
            try:
                self.dav.makedirs(LOCK_DIR)
                resp = self.dav.put(path, _json_bytes(row), if_none_match=True,
                                    content_type="application/json")
                row["etag"] = resp.etag
                return self._lock_row(row)
            except ConflictError:
                continue                                  # parent created meanwhile
            except PreconditionFailed:
                current = self._read_lock_file(path)
                if current is None:
                    continue                              # released meanwhile: retry create
                lock, etag = current
                holder = str(lock.get("user") or "")
                if holder == self.username:
                    row["acquired"] = lock.get("acquired") or now
                    resp = self.dav.put(path, _json_bytes(row), if_match=etag,
                                        content_type="application/json")
                    row["etag"] = resp.etag
                    return self._lock_row(row)
                if self._lock_row(lock)["expired"]:
                    # takeover of an expired lock: conditional on the very file we read
                    row["took_over"] = True
                    resp = self.dav.put(path, _json_bytes(row), if_match=etag,
                                        content_type="application/json")
                    row["etag"] = resp.etag
                    return self._lock_row(row)
                raise LockedError(f"{key} is locked by {holder}", 409, "locked",
                                  {"holder": self._lock_row(lock)},
                                  holder=self._lock_row(lock))
        raise ServerError("could not acquire the lock on the share", 409, "locked")

    def _read_lock_file(self, path: str) -> tuple[dict, str | None] | None:
        try:
            resp = self.dav.request("GET", path)
        except NotFoundError:
            return None
        lock = _parse_json(resp.body, None)
        if not isinstance(lock, dict):
            return None
        return lock, resp.etag or (self._etag_of(path))

    def release_lock(self, pid: str, key: str, force: bool = False) -> None:
        self._use(pid)
        path = lock_path(key)
        current = self._read_lock_file(path)
        if current is None:
            return
        lock, etag = current
        if str(lock.get("user") or "") != self.username and not force:
            raise ForbiddenError("this lock belongs to another member", 403, "forbidden")
        try:
            self.dav.delete(path, if_match=etag)
        except NotFoundError:
            pass
        except PreconditionFailed:
            raise ConflictError("the lock changed while releasing it", 409, "conflict")

    # -- presence --------------------------------------------------------------------

    def presence(self, pid: str, client_id: str, key: str | None = None, tab: str | None = None,
                 app_version: str = "") -> list[dict]:
        self._use(pid)
        now = self.clock()
        path = presence_path(client_id)
        row = {"client_id": client_id, "user": self.username,
               "display_name": self.display_name, "key": key, "tab": tab,
               "app_version": app_version or self.app_version, "ts": now, "ts_iso": _iso(now)}
        try:
            self.dav.makedirs(PRESENCE_DIR)
            self.dav.put(path, _json_bytes(row), content_type="application/json")
        except NotFoundError:
            pass
        out: list[dict] = []
        try:
            entries = self.dav.propfind(PRESENCE_DIR, depth="1")
        except NotFoundError:
            return out
        for prop in entries:
            if prop.is_dir or not prop.path.endswith(".json"):
                continue
            other = self._read_json(prop.path, None)
            if not isinstance(other, dict):
                continue
            if other.get("client_id") == client_id:
                continue
            try:
                ts = float(other.get("ts") or 0)
            except (TypeError, ValueError):
                continue
            if now - ts >= protocol.PRESENCE_TTL_SECS:
                continue
            out.append({"user": other.get("user"), "display_name": other.get("display_name"),
                        "key": other.get("key"), "tab": other.get("tab"), "ts": ts})
        return out

    def presence_close(self, pid: str, client_id: str) -> None:
        try:
            self.dav.delete(presence_path(client_id))
        except (NotFoundError, OnlineError):
            pass

    # -- done marks ---------------------------------------------------------------------

    def mark_done(self, pid: str, key: str, stage: str, tag: str) -> dict:
        self._use(pid)
        if stage not in protocol.STAGES:
            raise BadRequestError(f"stage must be one of {', '.join(protocol.STAGES)}", 400, "bad_request")
        me = self._member()
        if tag not in (me.get("tags") or []) and me.get("role") != "owner":
            raise ForbiddenError(f"tag {tag!r} is not one of your tags", 403, "forbidden")
        path = done_path(tag)
        for _attempt in range(4):
            try:
                resp = self.dav.request("GET", path)
                marks = _parse_json(resp.body, None)
                etag = resp.etag or self._etag_of(path)
            except NotFoundError:
                marks, etag = None, None
            if not isinstance(marks, dict):
                marks = {"tag": tag, "marks": {}}
            marks.setdefault("marks", {})[f"{key}|{stage}"] = self.clock()
            marks["tag"] = tag
            try:
                self.dav.makedirs(DONE_DIR)
                if etag:
                    self.dav.put(path, _json_bytes(marks), if_match=etag, content_type="application/json")
                else:
                    self.dav.put(path, _json_bytes(marks), if_none_match=True, content_type="application/json")
                return {"ok": True, "key": key, "stage": stage, "tag": tag}
            except PreconditionFailed:
                continue
            except ConflictError:
                continue
        raise ConflictError("could not write the done mark", 409, "conflict")

    def done_list(self, pid: str, tag: str | None = None, stage: str | None = None) -> list[dict]:
        self._use(pid)
        try:
            entries = self.dav.propfind(DONE_DIR, depth="1")
        except NotFoundError:
            return []
        rows: list[dict] = []
        for prop in entries:
            if prop.is_dir or not prop.path.endswith(".json"):
                continue
            doc = self._read_json(prop.path, None)
            if not isinstance(doc, dict):
                continue
            row_tag = str(doc.get("tag") or "")
            if tag and row_tag != tag:
                continue
            for name in (doc.get("marks") or {}):
                key, _, st = str(name).partition("|")
                if not key or not st:
                    continue
                if stage and st != stage:
                    continue
                rows.append({"key": key, "stage": st, "tag": row_tag})
        return rows

    def reopen_done(self, pid: str, key: str, stage: str, tag: str) -> None:
        self._use(pid)
        if self._member().get("role") != "owner":
            raise ForbiddenError("only the owner may reopen a paper", 403, "forbidden")
        path = done_path(tag)
        for _attempt in range(4):
            try:
                resp = self.dav.request("GET", path)
            except NotFoundError:
                return
            doc = _parse_json(resp.body, None)
            if not isinstance(doc, dict) or not isinstance(doc.get("marks"), dict):
                return
            doc["marks"].pop(f"{key}|{stage}", None)
            try:
                self.dav.put(path, _json_bytes(doc), if_match=resp.etag or self._etag_of(path),
                             content_type="application/json")
                return
            except PreconditionFailed:
                continue

    # -- assignments (admin-written file, honour system) ---------------------------------

    def _assignments_doc(self) -> dict:
        doc = self._read_json(ASSIGNMENTS_FILE, None)
        if not isinstance(doc, dict):
            doc = {}
        doc.setdefault("rows", [])
        doc.setdefault("runs", [])
        return doc

    def assignments(self, pid: str, stage: str | None = None, tag: str | None = None) -> list[dict]:
        self._use(pid)
        done_runs = {r.get("run_id") for r in self._assignments_doc().get("runs") or []
                     if r.get("undone")}
        rows = []
        for row in self._assignments_doc().get("rows") or []:
            if not isinstance(row, dict) or row.get("run_id") in done_runs:
                continue
            if stage and row.get("stage") != stage:
                continue
            if tag and row.get("tag") != tag:
                continue
            rows.append(dict(row))
        return rows

    def _write_assignments(self, doc: dict) -> None:
        for _attempt in range(4):
            try:
                resp = self.dav.request("GET", ASSIGNMENTS_FILE)
                etag = resp.etag or self._etag_of(ASSIGNMENTS_FILE)
            except NotFoundError:
                etag = None
            try:
                self.dav.makedirs(STUDIO)
                if etag:
                    self.dav.put(ASSIGNMENTS_FILE, _json_bytes(doc), if_match=etag,
                                 content_type="application/json")
                else:
                    self.dav.put(ASSIGNMENTS_FILE, _json_bytes(doc), if_none_match=True,
                                 content_type="application/json")
                return
            except (PreconditionFailed, ConflictError):
                continue
        raise ConflictError("could not write the assignments file", 409, "conflict")

    def assignment_preview(self, pid: str, payload: dict) -> dict:
        raise BadRequestError(
            "planning an assignment run needs the studio server; in Nextcloud mode "
            "write .studio/assignments.json or use the manual assignment", 400, "bad_request")

    def _manual_assignment(self, pid: str, payload: dict) -> dict:
        stage = str(payload.get("stage") or "coding")
        keys = [str(k) for k in (payload.get("keys") or []) if str(k).strip()]
        tags = [str(t) for t in (payload.get("tags") or []) if str(t).strip()]
        if not keys or not tags:
            raise BadRequestError("keys and tags are required", 400, "bad_request")
        import uuid
        run_id = str(payload.get("run_id") or uuid.uuid4().hex)
        doc = self._assignments_doc()
        doc["runs"].append({"run_id": run_id, "stage": stage, "ts": _iso(self.clock()),
                            "params": {"mode": "manual", "n_keys": len(keys)}, "undone": False})
        for key in keys:
            for tag in tags:
                doc["rows"].append({"key": key, "tag": tag, "stage": stage, "run_id": run_id})
        self._write_assignments(doc)
        return {"run_id": run_id, "assignments": {k: tags for k in keys},
                "per_coder": {t: len(keys) for t in tags}}

    def assignment_manual(self, pid: str, payload: dict) -> dict:
        self._use(pid)
        return self._manual_assignment(pid, payload)

    def assignment_commit(self, pid: str, payload: dict) -> dict:
        self._use(pid)
        return self._manual_assignment(pid, payload)

    def assignment_runs(self, pid: str) -> list[dict]:
        self._use(pid)
        return [dict(r) for r in self._assignments_doc().get("runs") or [] if isinstance(r, dict)]

    def assignment_run_delete(self, pid: str, run_id: str) -> None:
        self._use(pid)
        doc = self._assignments_doc()
        for run in doc.get("runs") or []:
            if str(run.get("run_id")) == str(run_id):
                run["undone"] = True
        self._write_assignments(doc)

    # -- members / settings / users (owner, honour system) --------------------------------

    def members(self, pid: str) -> list[dict]:
        self._use(pid)
        raw = self._read_json(MEMBERS_FILE, None)
        rows = raw.get("members") if isinstance(raw, dict) else raw
        if isinstance(rows, dict):
            rows = [dict(v, username=k) for k, v in rows.items()]
        out = []
        for i, row in enumerate(rows or [], start=1):
            if not isinstance(row, dict):
                continue
            out.append({"user_id": i, "username": row.get("username"),
                        "display_name": row.get("display_name") or row.get("username"),
                        "role": row.get("role") or "coder",
                        "tags": list(row.get("tags") or []),
                        "tasks": list(row.get("tasks") or [])})
        return out

    def _write_members(self, rows: list[dict]) -> None:
        for _attempt in range(4):
            try:
                resp = self.dav.request("GET", MEMBERS_FILE)
                etag = resp.etag or self._etag_of(MEMBERS_FILE)
            except NotFoundError:
                etag = None
            body = _json_bytes({"members": rows})
            try:
                self.dav.makedirs(STUDIO)
                if etag:
                    self.dav.put(MEMBERS_FILE, body, if_match=etag, content_type="application/json")
                else:
                    self.dav.put(MEMBERS_FILE, body, if_none_match=True, content_type="application/json")
                self._invalidate_project()
                return
            except (PreconditionFailed, ConflictError):
                continue
        raise ConflictError("could not write members.json", 409, "conflict")

    def put_member(self, pid: str, user_id, payload: dict) -> dict:
        self._use(pid)
        rows = self.members(pid)
        idx = int(user_id) - 1
        row = dict(rows[idx]) if 0 <= idx < len(rows) else {"username": str(payload.get("username") or "")}
        row.update({k: payload[k] for k in ("role", "tags", "tasks", "display_name", "username")
                    if k in payload})
        if 0 <= idx < len(rows):
            rows[idx] = row
        else:
            rows.append(row)
        self._write_members(rows)
        return row

    def delete_member(self, pid: str, user_id) -> None:
        self._use(pid)
        rows = self.members(pid)
        idx = int(user_id) - 1
        if 0 <= idx < len(rows):
            rows.pop(idx)
            self._write_members(rows)

    def update_settings(self, pid: str, patch: dict) -> dict:
        self._use(pid)
        current = self._settings()
        for key, value in (patch or {}).items():
            if key == "blinding" and isinstance(value, dict):
                current["blinding"].update({k: str(v) for k, v in value.items()})
            else:
                current[key] = value
        for _attempt in range(4):
            try:
                resp = self.dav.request("GET", SETTINGS_FILE)
                etag = resp.etag or self._etag_of(SETTINGS_FILE)
            except NotFoundError:
                etag = None
            try:
                self.dav.makedirs(STUDIO)
                if etag:
                    self.dav.put(SETTINGS_FILE, _json_bytes(current), if_match=etag,
                                 content_type="application/json")
                else:
                    self.dav.put(SETTINGS_FILE, _json_bytes(current), if_none_match=True,
                                 content_type="application/json")
                self._invalidate_project()
                return current
            except (PreconditionFailed, ConflictError):
                continue
        raise ConflictError("could not write settings.json", 409, "conflict")

    def audit(self, pid: str, limit: int = 100) -> list[dict]:
        return []

    def admin_users(self) -> list[dict]:
        return [{"id": 1, "username": self.username, "display_name": self.display_name,
                 "is_admin": self._member().get("role") == "owner"}]
