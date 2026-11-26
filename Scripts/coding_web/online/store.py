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
                 timeout: float = 30.0, persist_token: bool = True, local_blobs: str | None = None):
        self.base_dir = base_dir
        #: False (browser worker): the token lives in memory only - no keyring,
        #: no ``.studio/token`` fallback file on the server's disk.
        self.persist_token = persist_token
        #: A server-local, read-only blob directory (``<dir>/<h[:2]>/<h>``):
        #: when set, the syncer installs server versions as symlinks into it
        #: instead of downloading them (browser workers on the server, PLAN_online_only §4.4).
        self.local_blobs = local_blobs
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
        #: callback() when the project's listing changed (installed on every
        #: syncer :meth:`open` creates; the app refreshes its paper keys)
        self.on_listing = None

    # -- connection ------------------------------------------------------------------

    def _new_client(self, server: str, token: str | None = None) -> ServerClient:
        transport = self.transport_factory(server) if self.transport_factory else None
        return ServerClient(server, token=token, timeout=self.timeout, transport=transport)

    # -- which backend? ----------------------------------------------------------------

    def connect_nextcloud(self, url: str, username: str, app_password: str, folder: str = "",
                          *, webdav_root: str | None = None,
                          allow_insecure_http: bool | None = None,
                          auth_user: str | None = None, login: str | None = None,
                          invite_id: str | None = None) -> list[dict]:
        """Connect to a shared Nextcloud folder over WebDAV (PLAN §13).

        The same store and the same sync engine as :meth:`connect`; the client
        is :class:`~.nextcloud.NextcloudClient` instead of ``ServerClient``.
        *url* is the Nextcloud base (``https://cloud.example.org``), *folder*
        the path inside the user's files (``Coding/meta``); *webdav_root* skips
        that derivation for a plain WebDAV share (the local test server).

        *login* ``"link"`` is a **share-link** project (D11): *username* is then
        the app identity from the owner's invitation, *auth_user* is
        ``anonymous`` (Nextcloud's user for ``/public.php/dav/…``), the
        password is the link's own password and *webdav_root* the link's public
        root.  Returns ``[{id, name, role}]`` like :meth:`connect`.
        """
        from .nextcloud import NextcloudClient

        if allow_insecure_http is None:
            from .webdav import is_loopback
            allow_insecure_http = is_loopback(url)
        client = NextcloudClient(url, username, app_password, folder,
                                 webdav_root_url=webdav_root, timeout=self.timeout,
                                 allow_insecure_http=bool(allow_insecure_http),
                                 app_version=self.app_version, auth_user=auth_user,
                                 login_mode=login, invite_id=invite_id)
        data = client.login(username, app_password)
        projects = client.projects()
        with self.lock:
            self.client, self.server, self.username = client, client.server_url, username
            self.user = data.get("user")
            self.projects_cache = list(projects)
        keyring_store.save_token(client.server_url, username, client.token or "",
                                 fallback_dir=None, use_keyring=self.use_keyring)
        return list(self.projects_cache)

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
        if self.persist_token:
            keyring_store.save_token(server, username, client.token or "", fallback_dir=None,
                                     use_keyring=self.use_keyring)
        return list(self.projects_cache)

    def adopt(self, server: str, token: str, username: str | None = None) -> dict:
        """Use an already issued *token* (the browser gateway's session).

        No login, nothing stored: the caller owns the token's lifetime.
        Validates it with ``GET /api/auth/me`` and returns that answer
        (``AuthError`` when the token is not valid).
        """
        server = server.rstrip("/")
        client = self._new_client(server, token)
        me = client.me()
        user = me.get("user") or {}
        with self.lock:
            self.client, self.server = client, server
            self.username = username or user.get("username")
            self.user = user
            self.projects_cache = list(me.get("projects") or [])
        return me

    def resume(self, server: str, username: str, project_id: str | None = None) -> bool:
        """Re-use a stored token (keyring, else the mirror's token file).

        Studio-server mode only: a Nextcloud mirror is resumed by calling
        :meth:`connect_nextcloud` again with the same folder (the app itself
        does not call this yet - it opens through ``/api/online/open``).

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

    def resume_connection(self, kind: str, server: str, username: str, *, project_id: str | None = None,
                          url: str | None = None, folder: str = "", webdav_root: str | None = None,
                          secret: str | None = None, login: str | None = None,
                          invite_id: str | None = None) -> dict:
        """Reconnect a saved connection with its stored secret (no dialog).

        *kind* ``"server"``: the token from the keyring (else the mirror's
        token file) is reused; ``"nextcloud"``: the app password (or, for a
        share-link project, the link password).  The secret is validated once;
        an unreachable server is fine (the mirror opens offline), a refused one
        raises ``AuthError`` - the caller then shows the connect dialog
        prefilled, password empty.  ``NotConnectedError`` when no secret is
        stored at all.

        ``login="link"`` restores a **share-link** coder: Basic auth as
        ``anonymous`` with the link password, the project identity from the
        invitation.  Returns ``{kind, server, username, project_id, offline}``.
        """
        kind = "nextcloud" if str(kind or "").lower() in ("nextcloud", "webdav", "nc") else "server"
        server = (server or "").rstrip("/")
        fallback = (os.path.join(mirror_dir(server, project_id, self.base_dir), ".studio")
                    if project_id else None)
        if secret is None:
            secret = keyring_store.load_token(server, username, fallback, use_keyring=self.use_keyring)
        if not secret:
            raise NotConnectedError("no stored credentials for this connection")
        offline = False
        if kind == "nextcloud":
            from .nextcloud import NextcloudClient
            from .webdav import is_loopback

            link = str(login or "").lower() == "link"
            client = NextcloudClient(url or server, username, secret, folder,
                                     webdav_root_url=webdav_root, timeout=self.timeout,
                                     allow_insecure_http=is_loopback(url or server),
                                     app_version=self.app_version,
                                     login_mode="link" if link else None,
                                     invite_id=invite_id)
            try:
                client.login(username, secret)
            except OfflineError:
                offline = True
            user = {"id": username, "username": username, "display_name": username,
                    "login": "link" if link else "account"}
            projects = [{"id": client.project_id, "name": client._folder_name(), "role": ""}]
            server = client.server_url
        else:
            client = self._new_client(server, secret)
            user, projects = None, []
            try:
                me = client.me()
                user, projects = me.get("user"), list(me.get("projects") or [])
            except OfflineError:
                offline = True
        with self.lock:
            self.client, self.server, self.username = client, server, username
            self.user = user
            self.projects_cache = projects
        return {"kind": kind, "server": server, "username": username,
                "project_id": project_id or (projects[0]["id"] if kind == "nextcloud" else None),
                "offline": offline}

    def forget_secret(self, server: str, username: str, project_id: str | None = None) -> None:
        """Remove the stored token / app password of one connection."""
        server = (server or "").rstrip("/")
        fallback = (os.path.join(mirror_dir(server, project_id, self.base_dir), ".studio")
                    if project_id else None)
        keyring_store.delete_token(server, username, fallback, use_keyring=self.use_keyring)

    def connection_info(self) -> dict:
        """What a saved connection needs to reconnect later (never the secret)."""
        with self.lock:
            client, mirror = self.client, self.mirror
            server, username = self.server, self.username
        info = {"kind": getattr(client, "backend", "server") if client is not None else None,
                "server": server, "username": username}
        if info["kind"] == "nextcloud":
            info["folder"] = getattr(client, "folder", "") or ""
            info["url"] = getattr(client, "url", None) or server
            info["login"] = getattr(client, "login_mode", "account") or "account"
            info["invite_id"] = getattr(client, "invite_id", None)
            root = getattr(client, "root_url", "")
            default = None
            if info["login"] == "account":
                try:
                    from .nextcloud import webdav_root as _root
                    default = _root(info["url"], username or "", info["folder"]).rstrip("/")
                except Exception:
                    pass
            info["webdav_root"] = root if root and root != default else None
        else:
            info["url"] = server
        if mirror is not None:
            with mirror.lock:
                info["project_id"] = mirror.state.get("project_id")
                info["project_name"] = mirror.state.get("project_name")
        return info

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

    def open(self, project_id: str, start: bool | None = None,
             analysis_prefetch: bool = False) -> str:
        """Open (create) the mirror of *project_id*; returns its path.

        Works offline when the mirror already exists (permissions come from
        the last known ``me``).  The background sync starts unless
        ``start=False`` (or the store was built with ``autostart=False``).
        ``analysis_prefetch`` queues every ``Datasets/`` product a local
        analysis run reads (the "run analysis locally" switch).
        """
        client = self._require_client()
        self.close()
        path = mirror_dir(self.server, project_id, self.base_dir)
        mirror = Mirror(path, fsync=self.fsync)
        bind = getattr(client, "bind_mirror", None)
        if bind is not None:                 # Nextcloud backend: index + merge bases
            try:
                bind(mirror)
            except OSError:
                pass
        with mirror.lock:
            st = mirror.state
            if st.get("username") and st["username"] != self.username and st.get("project_id"):
                outbox_path = os.path.join(mirror.studio, "outbox.jsonl")
                queued = bool(st.get("done_pending"))
                if queued or (os.path.exists(outbox_path) and os.path.getsize(outbox_path) > 0):
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
        if client.token and self.persist_token:
            keyring_store.save_token(self.server, self.username, client.token, fallback_dir=mirror.studio,
                                     use_keyring=self.use_keyring)
        syncer = Syncer(mirror, client, debounce=self.debounce, interval=self.interval, clock=self.clock,
                        local_blobs=self.local_blobs)
        syncer.on_listing = self.on_listing
        with self.lock:
            self.mirror, self.syncer = mirror, syncer
        try:
            self.refresh_assignments()
        except OnlineError:
            pass
        if analysis_prefetch:
            try:
                self.prefetch_analysis()
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
        closer = getattr(client, "close", None)
        if mirror and closer is not None:
            try:
                closer()                     # Nextcloud: compact the index, drop connections
            except Exception:
                pass

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
            client = self.client
        backend = getattr(client, "backend", "server") if client is not None else "local"
        guarantees = getattr(client, "guarantees", "strong") if client is not None else "strong"
        login = getattr(client, "login_mode", "account") if client is not None else None
        if syncer is None:
            return {"mode": "disconnected", "pending": 0, "conflicts": 0, "last_sync": None,
                    "server": self.server, "user": self.username,
                    "backend": backend, "guarantees": guarantees, "login": login,
                    "readonly": False, "readonly_reason": None, "link_error": None}
        out = syncer.status()
        out["backend"] = backend
        out["guarantees"] = guarantees
        out["login"] = login
        out["readonly"] = False
        out["readonly_reason"] = None
        out["link_error"] = None
        access = getattr(client, "access", None)
        if login == "link" and callable(access):
            try:
                state = access()
            except OnlineError:
                state = {}
            out["readonly"] = bool(state.get("readonly"))
            out["readonly_reason"] = state.get("reason")
            out["readonly_message"] = state.get("message")
            health = getattr(client, "link_health", None)
            if out["mode"] in ("offline", "error") and callable(health):
                # "your Wi-Fi is down" and "the owner deleted the link" both look
                # like a failed round; only one of them needs a new invitation
                try:
                    link = health()
                except Exception:               # noqa: BLE001 - status must answer
                    link = {"ok": None}
                if link.get("ok") is False:
                    out["link_error"] = link.get("message")
                    if out["mode"] == "error":
                        out["last_error"] = link.get("message")
        return out

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

    def ensure_many(self, relpaths, progress=None) -> dict:
        """Make a **batch** of on-demand paths local in one sync round.

        The bulk counterpart of :meth:`ensure_local`, for a view that reads a
        whole corpus (the agreement / IRR tab opens every
        ``Codings/<Key> - <coder>.json``, including the on-demand AI routes).
        The paths are queued as ``stale`` and downloaded by a single
        ``Syncer.pull`` - one manifest request and one state save for the whole
        batch instead of a ``fetch``/state-save per file - so a crash mid-batch
        resumes on the next tick.  ``progress(done, total, path)`` is called as
        the batch advances.

        Returns ``{total, needed, fetched, missing, offline, bytes}``;
        ``missing`` lists the paths that are still absent afterwards (offline,
        or unknown to the server), so the caller can report them.
        """
        _client, mirror, syncer = self._require()
        rels: list[str] = []
        for raw in relpaths or ():
            rel = protocol.normalize_path(str(raw or ""))
            if rel and rel not in rels:
                rels.append(rel)
        needed = []
        for rel in rels:
            with mirror.lock:
                r = mirror.remote.get(rel)
                head = mirror.heads.get(rel)
            if r is None or not r.get("hash") or r.get("deleted"):
                continue                      # unknown to the server: reported missing
            if head and head.get("hash") == r.get("hash") and os.path.exists(mirror.abspath(rel)):
                continue                      # already current
            needed.append(rel)
        out = {"total": len(rels), "needed": len(needed), "fetched": 0,
               "missing": [], "offline": False, "bytes": 0}
        if needed:
            pending = set(needed)
            with mirror.lock:
                for rel in needed:
                    mirror.add_stale(rel)     # survives a crash: the next round fetches it
                mirror.save()

            def _report(done_n, total_n, rel):
                if rel not in pending:
                    return
                pending.discard(rel)
                if progress is not None:
                    try:
                        progress(len(needed) - len(pending), len(needed), rel)
                    except Exception:        # a progress callback may never break a fetch
                        pass

            # Parallel downloads of exactly these paths, without the sync lock:
            # a whole-corpus view must not wait behind a long background round
            # (the first sync of a large project).  ``_download_into`` is atomic
            # per file, so a path the background round fetches too is harmless.
            jobs: list = []
            try:
                for rel in needed:
                    # the engine's own rules decide (an unpushed edit, a local
                    # modification of a writable file are never overwritten)
                    syncer._reconcile(rel, defer=jobs)
                syncer.download_many(jobs, progress=_report, track=False)
            except OfflineError:
                out["offline"] = True
            except OnlineError:
                pass                          # a refused batch: the loop below reports it
            except NotConnectedError:
                pass
        for rel in rels:
            full = mirror.abspath(rel)
            if os.path.exists(full):
                out["fetched"] += 1
                try:
                    out["bytes"] += os.path.getsize(full)
                except OSError:
                    pass
            else:
                out["missing"].append(rel)
        if needed:
            mirror.save()
        return out

    def ensure_key(self, key: str) -> int:
        """Make the **core** files of study *key* local now; returns how many were fetched.

        While the first sync of a large project is still downloading the core
        tier in the background, the paper the user opens must not wait for its
        turn: its files jump the queue (``priority_keys``) and the missing ones
        are fetched right here.  A no-op once they are local.
        """
        _c, mirror, syncer = self._require()
        if not key:
            return 0
        if key not in syncer.priority_keys:
            syncer.priority_keys = ([key] + [k for k in syncer.priority_keys if k != key])[:8]
        paths = self._key_paths().get(key) or ()
        jobs: list = []
        for rel in paths:
            if os.path.exists(mirror.abspath(rel)):
                continue
            syncer._reconcile(rel, defer=jobs)
        if not jobs:
            return 0
        try:
            written, _ = syncer.download_many(jobs, track=False)
        except OnlineError:
            return 0
        return len(written)

    def _key_paths(self) -> dict:
        """``{key: [core paths]}`` from the manifest (cached per listing revision)."""
        _c, mirror, syncer = self._require()
        stamp = (id(mirror), syncer.files_rev, len(mirror.remote))
        cached = getattr(self, "_key_paths_cache", None)
        if cached and cached[0] == stamp:
            return cached[1]
        out: dict[str, list[str]] = {}
        with mirror.lock:
            items = list(mirror.remote.items())
        for rel, r in items:
            k = r.get("key")
            if not k:
                fc = mirror.classify(rel)
                k = fc.key
            if k and protocol.is_core(rel, mirror.classify(rel)):
                out.setdefault(k, []).append(rel)
        self._key_paths_cache = (stamp, out)
        return out

    #: How long a manifest view may be reused when the state stamp is unchanged.
    #: ``/api/papers`` lists ``Papers/`` and ``Supplements/`` once per paper; a
    #: fresh copy of the whole manifest per call cost O(papers x files) - tens of
    #: seconds on the real project.  A listing that is <= 2 s old is fine for
    #: every caller (names and sizes of a folder).
    LISTING_TTL = 2.0

    def _known_files(self) -> dict[str, dict]:
        """``{path: {hash, size, local}}`` of manifest ∪ heads (a shared, cached view - do not mutate)."""
        return self._views()[0]

    def _views(self):
        _c, mirror, _s = self._require()
        with mirror.lock:
            stamp = (id(mirror), mirror.state.get("seq"), mirror.state.get("visibility_epoch"),
                     len(mirror.remote), len(mirror.heads))
            cached = getattr(self, "_listing_cache", None)
            now = time.monotonic()
            if cached and cached[0] == stamp and now - cached[1] < self.LISTING_TTL:
                return cached[2], cached[3]
            out = {p: {"hash": r.get("hash"), "size": r.get("size") or 0, "local": False}
                   for p, r in mirror.remote.items()}
            for p, h in mirror.heads.items():
                e = out.setdefault(p, {"hash": h["hash"], "size": h["size"]})
                e["local"] = True
        # per-folder index: "Papers" -> {"x.pdf": meta, "sub": {"dir": True}}
        by_dir: dict[str, dict[str, dict]] = {}
        for p, meta in out.items():
            parent, _sep, name = p.rpartition("/")
            by_dir.setdefault(parent, {})[name] = meta
            while parent:
                grand, _sep, dname = parent.rpartition("/")
                children = by_dir.setdefault(grand, {})
                if dname in children:
                    break
                children[dname] = {"dir": True}
                parent = grand
        self._listing_cache = (stamp, time.monotonic(), out, by_dir)
        return out, by_dir

    def knows_file(self, relpath: str) -> bool:
        """O(1): is *relpath* a file of the server manifest or a synced head?

        ``S.exists`` asks this once per paper while building ``/api/papers``;
        answering it from :meth:`listdir` copied the whole manifest per call
        (O(papers x files): minutes on an 860-paper project).
        """
        _c, mirror, _s = self._require()
        rel = protocol.normalize_path(relpath)
        with mirror.lock:
            r = mirror.remote.get(rel)
            if r is not None and not r.get("deleted") and r.get("hash"):
                return True
            return rel in mirror.heads

    def listdir(self, prefix: str = "", recursive: bool = False) -> list[dict]:
        """Entries under *prefix* known from the server manifest, the heads
        and the disk (so files not downloaded are listed too).

        Each entry: ``{name, path, is_dir, size, hash, local}``.
        """
        _c, mirror, _s = self._require()
        prefix = protocol.normalize_path(prefix).strip("/")
        pre = prefix + "/" if prefix else ""
        d = mirror.abspath(prefix) if prefix else mirror.root
        if not recursive:
            return self._listdir_flat(mirror, prefix, pre, d)
        known = dict(self._known_files())
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

    def _listdir_flat(self, mirror, prefix: str, pre: str, d: str) -> list[dict]:
        """One folder: its manifest children (from the cached index) ∪ the disk."""
        children = self._views()[1].get(prefix, {})
        on_disk: dict[str, tuple[bool, int | None]] = {}       # name -> (is_dir, size)
        if os.path.isdir(d):
            for e in os.scandir(d):
                try:
                    if e.is_file():
                        on_disk[e.name] = (False, e.stat().st_size)
                    elif e.is_dir():
                        on_disk[e.name] = (True, None)
                except OSError:
                    continue
        out: dict[str, dict] = {}
        for name in set(children) | set(on_disk):
            path = pre + name
            meta = children.get(name)
            disk = on_disk.get(name)
            is_dir = bool(meta.get("dir")) if meta is not None else bool(disk and disk[0])
            if mirror.classify(path + "/" if is_dir else path).kind == "excluded":
                continue
            if is_dir:
                out[name] = {"name": name, "path": path, "is_dir": True, "size": None, "hash": None,
                             "local": bool(disk and disk[0])}
            else:
                size = meta.get("size") if meta is not None else (disk[1] if disk else None)
                out[name] = {"name": name, "path": path, "is_dir": False, "size": size,
                             "hash": meta.get("hash") if meta is not None else None,
                             "local": bool(disk and not disk[0])}
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
        """Mark a paper finished - online, or queued when the server is away.

        The mark is written to ``state.done_pending`` (atomic) *before* the
        server call, so it survives a crash and an offline period; the next
        successful round sends it before the manifest pull (see
        :meth:`Syncer.flush_done`).  ``POST /done`` is an upsert per
        (tag, key, stage), so a re-send is harmless.
        """
        client, mirror, syncer = self._require()
        if tag is None:
            tags = list((mirror.state.get("me") or {}).get("tags") or [])
            if len(tags) != 1:
                raise ValueError("tag is required when the member has several tags")
            tag = tags[0]
        pid = mirror.state["project_id"]
        syncer.queue_done(key, stage, tag)
        try:
            res = client.mark_done(pid, key, stage, tag)
        except OfflineError as exc:
            # queued: the badge shows the paper as finished now, the server
            # learns about it on the next successful round
            syncer.note_offline(exc)
            return {"ok": True, "queued": True, "key": key, "stage": stage,
                    "tag": tag, "mode": "offline"}
        except OnlineError:
            syncer.drop_done(key, stage, tag)      # refused: it will never land
            raise
        syncer.drop_done(key, stage, tag)
        syncer.kick()               # the visibility epoch changed: full manifest next round
        return res

    def presence(self, key: str | None = None, tab: str | None = None) -> list[dict]:
        client, mirror, _s = self._require()
        return client.presence(mirror.state["project_id"], mirror.state["client_id"], key, tab, self.app_version)

    def assignments(self, stage: str | None = None, tag: str | None = None) -> list[dict]:
        client, mirror, _s = self._require()
        return client.assignments(mirror.state["project_id"], stage=stage, tag=tag)

    def done_list(self, stage: str | None = None, tag: str | None = None) -> list[dict]:
        """Done marks of the project: the server's plus the ones still queued.

        A queued mark is part of the answer immediately (the Finish button must
        show the paper as finished offline); it carries ``pending: True`` until
        the next successful round confirms it.  When the server cannot be
        reached the queued marks are the whole answer instead of an error.
        """
        client, mirror, _syncer = self._require()
        pid = mirror.state["project_id"]
        try:
            rows = list(client.done_list(pid, tag=tag, stage=stage))
        except OfflineError:
            rows = []
        seen = {(r.get("key"), r.get("stage"), r.get("tag")) for r in rows}
        with mirror.lock:
            pending = [dict(r) for r in (mirror.state.get("done_pending") or [])]
        for row in pending:
            if stage and row.get("stage") != stage:
                continue
            if tag and row.get("tag") != tag:
                continue
            if (row.get("key"), row.get("stage"), row.get("tag")) in seen:
                continue
            rows.append({**row, "pending": True})
        return rows

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

    def prefetch_analysis(self, blocking: bool = False) -> list[str]:
        """Fetch the ``Datasets/`` products a local analysis run reads.

        Those CSVs are on-demand in the core tier (INTERFACES 4), so the "run
        analysis locally" switch has to ask for them: non-blocking queues them
        for the next pull (``stale``), blocking fetches them now (used right
        before the pipeline starts).  Returns the paths involved.
        """
        _c, mirror, syncer = self._require()
        todo = syncer.cache.analysis_candidates()
        if not blocking:
            for rel in todo:
                mirror.add_stale(rel)
            mirror.save()
            syncer.kick()
            return todo
        done = []
        for rel in todo:
            syncer.fetch(rel)
            done.append(rel)
        return done

    def analysis_download(self) -> dict:
        """``{files, pending, bytes, local_bytes}`` of the local analysis inputs."""
        return self._require()[2].cache.analysis_usage()
