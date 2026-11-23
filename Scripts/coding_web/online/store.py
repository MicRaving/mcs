"""``OnlineStore`` - the façade the app (``routes_online``) talks to.

Typical use::

    store = OnlineStore()
    projects = store.connect("https://studio.example.org", "anna", "secret")
    root = store.open(projects[0]["id"])     # the mirror; the app sets S.ROOT = root
    store.status()                           # {mode, pending, conflicts, last_sync, server, user, ...}
    path = store.ensure_local("Papers/Smith (2020).pdf")
    store.acquire_lock("Smith (2020)")
    ...
    store.close()

Thread safety: ``self.lock`` (an ``RLock``) guards the store's own fields
(client, mirror, syncer); the sync state has its own lock in
:class:`~.mirror.Mirror`.  Network calls run outside ``self.lock``.
"""

from __future__ import annotations

import os
import threading
import time

from online_shared import protocol

from . import keyring_store
from .client import ServerClient
from .errors import NotConnectedError, OfflineError, OnlineError
from .mirror import Mirror, mirror_dir
from .syncer import Syncer


class OnlineStore:
    def __init__(self, base_dir: str | None = None, *, transport_factory=None, app_version: str = "",
                 debounce: float = protocol.PUSH_DEBOUNCE_SECS, interval: float = protocol.SYNC_INTERVAL_SECS,
                 fsync: bool = True, use_keyring: bool = True, autostart: bool = True, clock=time.time,
                 timeout: float = 30.0):
        self.base_dir = base_dir
        self.transport_factory = transport_factory
        self.app_version = app_version
        self.debounce = debounce
        self.interval = interval
        self.fsync = fsync
        self.use_keyring = use_keyring
        self.autostart = autostart
        self.clock = clock
        self.timeout = timeout
        self.lock = threading.RLock()
        self.client: ServerClient | None = None
        self.server: str | None = None
        self.username: str | None = None
        self.user: dict | None = None
        self.projects_cache: list[dict] = []
        self.mirror: Mirror | None = None
        self.syncer: Syncer | None = None

    # -- connection ------------------------------------------------------------------

    def _new_client(self, server: str, token: str | None = None) -> ServerClient:
        transport = self.transport_factory(server) if self.transport_factory else None
        return ServerClient(server, token=token, timeout=self.timeout, transport=transport)

    def connect(self, server: str, username: str, password: str) -> list[dict]:
        """Log in; returns ``[{id, name, role}]``."""
        server = server.rstrip("/")
        client = self._new_client(server)
        data = client.login(username, password)
        me = client.me()
        with self.lock:
            self.client, self.server, self.username = client, server, username
            self.user = data.get("user") or me.get("user")
            self.projects_cache = list(me.get("projects") or [])
        keyring_store.save_token(server, username, client.token or "", fallback_dir=None,
                                 use_keyring=self.use_keyring)
        return list(self.projects_cache)

    def resume(self, server: str, username: str, project_id: str | None = None) -> bool:
        """Re-use a stored token (keyring, else the mirror's token file).

        Returns True when a token was found.  The token is validated lazily:
        offline is fine (the mirror opens from local state), an invalid token
        surfaces as ``mode: error`` / ``AuthError`` on the next call.
        """
        server = server.rstrip("/")
        fallback = os.path.join(mirror_dir(server, project_id, self.base_dir), ".studio") if project_id else None
        token = keyring_store.load_token(server, username, fallback, use_keyring=self.use_keyring)
        if not token:
            return False
        with self.lock:
            self.client = self._new_client(server, token)
            self.server, self.username = server, username
        try:
            me = self.client.me()
            with self.lock:
                self.user = me.get("user")
                self.projects_cache = list(me.get("projects") or [])
        except OfflineError:
            pass
        return True

    def projects(self) -> list[dict]:
        client = self._require_client()
        try:
            self.projects_cache = client.projects()
        except OfflineError:
            pass
        return list(self.projects_cache)

    def logout(self) -> None:
        self.close()
        with self.lock:
            client, server, username = self.client, self.server, self.username
            self.client = None
        if client:
            try:
                client.logout()
            except OnlineError:
                pass
        if server and username:
            keyring_store.delete_token(server, username, use_keyring=self.use_keyring)

    # -- project -----------------------------------------------------------------------

    def open(self, project_id: str, start: bool | None = None) -> str:
        """Open (create) the mirror of *project_id*; returns its path.

        Works offline when the mirror already exists (permissions come from
        the last known ``me``).  The background sync starts unless
        ``start=False`` (or the store was built with ``autostart=False``).
        """
        client = self._require_client()
        self.close()
        path = mirror_dir(self.server, project_id, self.base_dir)
        mirror = Mirror(path, fsync=self.fsync)
        with mirror.lock:
            st = mirror.state
            if st.get("username") and st["username"] != self.username and st.get("project_id"):
                outbox_path = os.path.join(mirror.studio, "outbox.jsonl")
                if os.path.exists(outbox_path) and os.path.getsize(outbox_path) > 0:
                    raise OnlineError(
                        f"this mirror holds unpushed changes of {st['username']!r}; "
                        "log in as that user to push them first")
            st["server"], st["project_id"], st["username"] = self.server, project_id, self.username
        info = None
        try:
            info = client.project(project_id)
        except OfflineError:
            if not mirror.state.get("me"):
                raise
        if info is not None:
            with mirror.lock:
                mirror.state["project_name"] = info.get("name")
                mirror.state["settings"] = info.get("settings") or {}
                mirror.state["me"] = info.get("me") or {}
        mirror.save()
        if client.token:
            keyring_store.save_token(self.server, self.username, client.token, fallback_dir=mirror.studio,
                                     use_keyring=self.use_keyring)
        syncer = Syncer(mirror, client, debounce=self.debounce, interval=self.interval, clock=self.clock)
        with self.lock:
            self.mirror, self.syncer = mirror, syncer
        try:
            self.refresh_assignments()
        except OnlineError:
            pass
        if self.autostart if start is None else start:
            syncer.start()
        return mirror.root

    def close(self) -> None:
        with self.lock:
            syncer, mirror, client = self.syncer, self.mirror, self.client
            self.syncer = None
            self.mirror = None
        if syncer:
            syncer.stop()
            try:
                client.presence_close(mirror.state["project_id"], mirror.state["client_id"])
            except Exception:
                pass
        if mirror:
            mirror.save()

    @property
    def root(self) -> str | None:
        return self.mirror.root if self.mirror else None

    def _require_client(self) -> ServerClient:
        with self.lock:
            if self.client is None:
                raise NotConnectedError("not connected to a server")
            return self.client

    def _require(self) -> tuple[ServerClient, Mirror, Syncer]:
        with self.lock:
            if self.client is None or self.mirror is None or self.syncer is None:
                raise NotConnectedError("no online project is open")
            return self.client, self.mirror, self.syncer

    @property
    def pid(self) -> str:
        return self._require()[1].state["project_id"]

    def me(self) -> dict:
        return dict(self._require()[1].state.get("me") or {})

    # -- sync ----------------------------------------------------------------------------

    def status(self) -> dict:
        with self.lock:
            syncer = self.syncer
        if syncer is None:
            return {"mode": "disconnected", "pending": 0, "conflicts": 0, "last_sync": None,
                    "server": self.server, "user": self.username}
        return syncer.status()

    def kick(self) -> None:
        self._require()[2].kick()

    def sync_now(self) -> dict:
        """One blocking round that ignores the push debounce."""
        syncer = self._require()[2]
        syncer.tick(force=True)
        return syncer.status()

    # -- files ---------------------------------------------------------------------------

    def ensure_local(self, relpath: str) -> str:
        """Absolute path of *relpath*, downloading it if missing/outdated.

        Raises ``OfflineError`` when it is absent and the server is
        unreachable, ``FileNotFoundError`` when the server does not know it.
        """
        return self._require()[2].fetch(relpath)

    def _known_files(self) -> dict[str, dict]:
        _c, mirror, _s = self._require()
        with mirror.lock:
            out = {p: {"hash": r.get("hash"), "size": r.get("size") or 0, "local": False}
                   for p, r in mirror.remote.items()}
            for p, h in mirror.heads.items():
                e = out.setdefault(p, {"hash": h["hash"], "size": h["size"]})
                e["local"] = True
        return out

    def listdir(self, prefix: str = "", recursive: bool = False) -> list[dict]:
        """Entries under *prefix* known from the server manifest, the heads
        and the disk (so files not downloaded are listed too).

        Each entry: ``{name, path, is_dir, size, hash, local}``.
        """
        _c, mirror, _s = self._require()
        prefix = protocol.normalize_path(prefix).strip("/")
        pre = prefix + "/" if prefix else ""
        known = self._known_files()
        d = mirror.abspath(prefix) if prefix else mirror.root
        if os.path.isdir(d):
            if recursive:
                for root, dirs, files in os.walk(d):
                    rel_root = os.path.relpath(root, mirror.root).replace("\\", "/")
                    rel_root = "" if rel_root == "." else rel_root + "/"
                    dirs[:] = [x for x in dirs if mirror.classify(rel_root + x + "/").kind != "excluded"]
                    for f in files:
                        known.setdefault(rel_root + f, {"hash": None, "size": None, "local": True})
            else:
                for e in os.scandir(d):
                    if e.is_file():
                        known.setdefault(pre + e.name, {"hash": None, "size": e.stat().st_size, "local": True})
                    elif e.is_dir() and mirror.classify(pre + e.name + "/").kind != "excluded":
                        known.setdefault(pre + e.name + "/", {"dir": True})
        out: dict[str, dict] = {}
        for p, meta in known.items():
            if not p.startswith(pre) or mirror.classify(p.rstrip("/")).kind == "excluded":
                continue
            rest = p[len(pre):]
            if not rest:
                continue
            if not recursive and "/" in rest.rstrip("/"):
                name = rest.split("/", 1)[0]
                out.setdefault(name, {"name": name, "path": pre + name, "is_dir": True,
                                      "size": None, "hash": None, "local": os.path.isdir(os.path.join(d, name))})
                continue
            if meta.get("dir"):
                name = rest.rstrip("/")
                out.setdefault(name, {"name": name, "path": pre + name, "is_dir": True, "size": None,
                                      "hash": None, "local": True})
                continue
            local = os.path.exists(mirror.abspath(p))
            out[rest] = {"name": rest.rsplit("/", 1)[-1], "path": p, "is_dir": False,
                         "size": meta.get("size"), "hash": meta.get("hash"), "local": local}
        return sorted(out.values(), key=lambda e: e["path"])

    def list_keys(self) -> set[str]:
        """Study keys the app's ``S.KEYS`` would hold if every file were local:
        stems of ``Text/*.txt`` and ``Papers/*.pdf`` (manifest ∪ disk)."""
        keys = set()
        for e in self.listdir("Text") + self.listdir("Papers"):
            if e["is_dir"]:
                continue
            name = e["name"]
            if (e["path"].startswith("Text/") and name.endswith(".txt")) or \
                    (e["path"].startswith("Papers/") and name.endswith(".pdf")):
                keys.add(name.rsplit(".", 1)[0])
        return keys

    # -- locks / presence / done / assignments -------------------------------------------------

    def acquire_lock(self, key: str, offline_hours: float | None = None) -> dict:
        client, mirror, syncer = self._require()
        res = client.acquire_lock(mirror.state["project_id"], key, mirror.state["client_id"], offline_hours)
        if offline_hours:
            self.pin([key])
        return res

    def release_lock(self, key: str, force: bool = False) -> None:
        client, mirror, _s = self._require()
        client.release_lock(mirror.state["project_id"], key, force=force)

    def locks(self) -> list[dict]:
        client, mirror, _s = self._require()
        return client.locks(mirror.state["project_id"])

    def mark_done(self, key: str, stage: str, tag: str | None = None) -> dict:
        client, mirror, syncer = self._require()
        if tag is None:
            tags = list((mirror.state.get("me") or {}).get("tags") or [])
            if len(tags) != 1:
                raise ValueError("tag is required when the member has several tags")
            tag = tags[0]
        res = client.mark_done(mirror.state["project_id"], key, stage, tag)
        syncer.kick()               # the visibility epoch changed: full manifest next round
        return res

    def presence(self, key: str | None = None, tab: str | None = None) -> list[dict]:
        client, mirror, _s = self._require()
        return client.presence(mirror.state["project_id"], mirror.state["client_id"], key, tab, self.app_version)

    def assignments(self, stage: str | None = None, tag: str | None = None) -> list[dict]:
        client, mirror, _s = self._require()
        return client.assignments(mirror.state["project_id"], stage=stage, tag=tag)

    def done_list(self, stage: str | None = None, tag: str | None = None) -> list[dict]:
        """The member's (or a tag's) done marks from the server."""
        client, mirror, _s = self._require()
        return client.done_list(mirror.state["project_id"], tag=tag, stage=stage)

    def refresh_assignments(self) -> set[str]:
        """Pin the keys assigned to any of my tags (cache tier "pinned")."""
        client, mirror, syncer = self._require()
        tags = set((mirror.state.get("me") or {}).get("tags") or [])
        rows = client.assignments(mirror.state["project_id"])
        keys = {r["key"] for r in rows if not tags or r.get("tag") in tags}
        syncer.cache.set_assigned(keys)
        self._queue_preload(keys)
        mirror.save()
        return keys

    # -- conflicts --------------------------------------------------------------------------------

    def conflicts(self, state: str | None = "open") -> list[dict]:
        _c, _m, syncer = self._require()
        if state == "open":
            return syncer.refresh_conflicts()
        return syncer.conflicts.list(state)

    def resolve_conflict(self, conflict_id, resolution: str, merged_bytes: bytes | None = None) -> dict:
        _c, _m, syncer = self._require()
        res = syncer.resolve_conflict(conflict_id, resolution, merged_bytes)
        syncer.kick()
        return res

    # -- cache --------------------------------------------------------------------------------------

    def _queue_preload(self, keys=None) -> None:
        """Mark the server files of *keys* for download by the next pull."""
        _c, mirror, syncer = self._require()
        for rel in syncer.cache.preload_candidates(keys):
            mirror.add_stale(rel)

    def pin(self, keys) -> None:
        """Pin (take offline) *keys*: their files are downloaded by the next
        round and never evicted."""
        _c, mirror, syncer = self._require()
        syncer.cache.pin(list(keys))
        self._queue_preload(list(keys))
        mirror.save()
        syncer.kick()

    def unpin(self, keys) -> None:
        _c, mirror, syncer = self._require()
        syncer.cache.unpin(list(keys))
        mirror.save()

    def cache_settings(self) -> dict:
        return self._require()[2].cache.settings()

    def set_cache_settings(self, limit_mb: float | None = None, supplements: bool | None = None) -> dict:
        _c, mirror, syncer = self._require()
        syncer.cache.configure(limit_mb=limit_mb, supplements=supplements)
        mirror.save()
        if limit_mb is not None:
            syncer.cache.enforce()
        return syncer.cache.settings()

    def clear_cache(self) -> list[str]:
        return self._require()[2].cache.clear()

    def preload(self, keys=None) -> list[str]:
        """Download the files of *keys* (default: pinned + assigned keys)."""
        _c, _m, syncer = self._require()
        done = []
        for rel in syncer.cache.preload_candidates(keys):
            syncer.fetch(rel)
            done.append(rel)
        return done
