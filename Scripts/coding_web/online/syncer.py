"""The sync engine: scan -> enqueue -> push -> pull (INTERFACES §4).

One :class:`Syncer` per open project.  :meth:`Syncer.tick` runs one complete
round and is serialised by ``sync_lock`` (the background thread, "sync now"
and conflict resolution never overlap); ``mirror.lock`` guards the state dict
and is held only for short in-memory updates, never across a network call.

Crash safety - the invariants every step keeps:

* A local change is in the outbox (fsync'ed) before anything is sent; its
  bytes are in ``pending/<hash>`` before the outbox line is written.
* An outbox entry is removed (``ack``) only **after** ``state.json`` records
  the new head.  A crash between the two leaves an entry whose ``new_hash``
  equals the head: :meth:`_recover` acks it at start-up.  A crash after the
  server applied a push but before the state was saved leaves the entry with
  the old head; the first pull after start-up sees the server already has
  ``new_hash`` and acks it without pushing again (no duplicate seq), and the
  server treats a re-push of its current head as an idempotent ``accepted``.
  Every pushed version is journalled as ``sent`` first; while any entry has
  an unconfirmed ``sent`` version the round pulls before it pushes, so a
  lost response followed by another local edit rebases the new version on
  our own landed push instead of conflicting with it.
* ``state.seq`` only advances after every file of the manifest window has
  been handled (downloaded, deliberately skipped, or parked in ``stale``).
* A conflict's local bytes are written under ``.studio/conflicts/`` before
  the outbox entry is dropped and before the server version is restored.
* A "Finish paper" mark made while offline lives in ``state.done_pending``
  (written atomically with the rest of ``state.json``) and is sent at the start
  of the next round that reaches the server - **before** the manifest pull, so
  the caller sees the ``visibility_epoch`` change the mark causes in the same
  round.  The server's ``POST /done`` is an upsert per (project, tag, key,
  stage), so a re-send after a crash between the call and the state write is
  harmless.
"""

from __future__ import annotations

import os
import threading
import time

from online_shared import merge as merge_mod
from online_shared import protocol

from .cache import Cache
from .conflicts import ConflictStore
from .errors import (
    AuthError,
    BadRequestError,
    ForbiddenError,
    LockedError,
    NotFoundError,
    OfflineError,
    OnlineError,
    ProtocolMismatch,
)
from .mirror import Mirror, Permissions, atomic_write_bytes
from .outbox import Outbox

BACKOFF_MIN = 5.0
BACKOFF_MAX = 300.0
PUSH_BATCH = 200


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class Syncer:
    def __init__(self, mirror: Mirror, client, permissions: Permissions | None = None, *,
                 outbox: Outbox | None = None, conflicts: ConflictStore | None = None,
                 cache: Cache | None = None, debounce: float = protocol.PUSH_DEBOUNCE_SECS,
                 interval: float = protocol.SYNC_INTERVAL_SECS, clock=time.time):
        self.mirror = mirror
        self.client = client
        self.clock = clock
        self.debounce = debounce
        self.interval = interval
        self.outbox = outbox or Outbox(os.path.join(mirror.studio, "outbox.jsonl"), fsync=mirror.fsync, clock=clock)
        self.conflicts = conflicts or ConflictStore(mirror.conflicts_dir, fsync=mirror.fsync)
        self.cache = cache or Cache(mirror, self.outbox, clock=clock)
        if permissions is None:
            permissions = Permissions.from_me(mirror.state.get("me"))
        self.permissions = permissions

        self.sync_lock = threading.RLock()
        self.mode = "offline"
        self.last_sync: float | None = None
        self.last_error: str | None = None
        self.backoff = 0.0
        self.next_retry: float | None = None
        self._initial_pull_done = False
        self.last_round = {"ok": False, "pushed": 0, "pulled": 0, "done_sent": 0}
        self.dropped_done: list[str] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.on_change = None            # optional callback(paths) after files were written
        self._recover()

    # -- identity -------------------------------------------------------------------

    @property
    def pid(self) -> str:
        return self.mirror.state["project_id"]

    @property
    def client_id(self) -> str:
        return self.mirror.state["client_id"]

    def set_permissions(self, me: dict) -> None:
        with self.mirror.lock:
            self.mirror.state["me"] = dict(me or {})
        self.permissions = Permissions.from_me(me)

    def refresh_permissions(self) -> dict:
        info = self.client.project(self.pid)
        with self.mirror.lock:
            self.mirror.state["project_name"] = info.get("name", self.mirror.state.get("project_name"))
            self.mirror.state["settings"] = info.get("settings") or {}
        self.set_permissions(info.get("me") or {})
        return info

    # -- start-up recovery ----------------------------------------------------------------

    def _recover(self) -> None:
        """Repair what a crash can leave behind (see the module docstring)."""
        with self.mirror.lock:
            heads = dict(self.mirror.heads)
        for e in self.outbox.entries():
            head = heads.get(e["path"])
            if (e["new_hash"] is None and head is None) or (head and e["new_hash"] == head["hash"]):
                self.outbox.ack(e["path"], e["new_hash"], e["new_hash"])
        self._gc_pending()

    def _gc_pending(self) -> None:
        keep = self.outbox.referenced_hashes()
        try:
            names = os.listdir(self.mirror.pending_dir)
        except FileNotFoundError:
            return
        for name in names:
            if name.startswith("."):
                continue
            if name not in keep:
                self.mirror.remove_pending(name)

    # -- status ------------------------------------------------------------------------------

    def status(self) -> dict:
        st = self.mirror.state
        with self.mirror.lock:
            local_only = len(st.get("local_only") or {})
            seq = st.get("seq")
            done_pending = len(st.get("done_pending") or [])
        return {
            "mode": self.mode,
            # a queued "Finish paper" mark is a pending change too
            "pending": len(self.outbox) + done_pending,
            "done_pending": done_pending,
            "pushed": int(self.last_round.get("pushed") or 0),
            "pulled": int(self.last_round.get("pulled") or 0),
            "conflicts": self.conflicts.open_count(),
            "last_sync": _iso(self.last_sync),
            "server": st.get("server"),
            "user": st.get("username"),
            "project_id": st.get("project_id"),
            "project_name": st.get("project_name"),
            "seq": seq,
            "local_only": local_only,
            "last_error": self.last_error,
            "next_retry_in": max(0.0, self.next_retry - self.clock()) if self.next_retry else None,
        }

    # -- one round ---------------------------------------------------------------------------

    def tick(self, force: bool = False) -> bool:
        """Scan, push, pull.  Returns True when the round reached the server.

        Never raises for network trouble: ``OfflineError`` switches the mode
        to ``offline`` (local scanning still happened, so ``pending`` is
        right); auth/protocol errors switch it to ``error``.
        """
        with self.sync_lock:
            try:
                self.scan()
            except OSError as exc:                 # disk trouble: report, keep going
                self.last_error = f"scan failed: {exc}"
            prev = self.mode
            self.mode = "syncing"
            self.last_round = {"ok": False, "pushed": 0, "pulled": 0, "done_sent": 0}
            try:
                if not self._initial_pull_done or self.outbox.unconfirmed():
                    # start-up, or an earlier push whose outcome is unknown:
                    # learn what the server already has before pushing again
                    self.pull()
                    self._initial_pull_done = True
                # queued "Finish paper" marks go first: they change the caller's
                # visibility_epoch, which the pull below then sees
                self.last_round["done_sent"] = len(self.flush_done())
                self.last_round["pushed"] = len(self.push(force=force) or [])
                pulled = self.pull()
                self.last_round["pulled"] = len((pulled or {}).get("written") or [])
            except OfflineError as exc:
                self.mode = "offline"
                self.last_error = str(exc)
                self.backoff = BACKOFF_MIN if self.backoff <= 0 else min(self.backoff * 2, BACKOFF_MAX)
                self.next_retry = self.clock() + self.backoff
                return False
            except (AuthError, ProtocolMismatch) as exc:
                self.mode = "error"
                self.last_error = f"{type(exc).__name__}: {exc}"
                return False
            except OnlineError as exc:
                self.mode = "error"
                self.last_error = f"{type(exc).__name__}: {exc}"
                return False
            except BaseException:
                self.mode = prev if prev != "syncing" else "error"
                raise
            self.mode = "online"
            self.last_error = None
            if self.dropped_done:
                self.last_error = "; ".join(self.dropped_done[:3])
            self.backoff = 0.0
            self.next_retry = None
            self.last_sync = self.clock()
            self.last_round["ok"] = True
            return True

    # -- scan / enqueue --------------------------------------------------------------------------

    def scan(self):
        res = self.mirror.scan(self.permissions, self.outbox.view())
        if res.changes:
            self.outbox.add_many([(c.path, c.base_hash, c.new_hash, c.size, c.mtime_ns) for c in res.changes])
        if res.state_changed or res.changes:
            self.mirror.save()
        return res

    # -- done marks queued for the next round -----------------------------------------------

    def done_pending(self) -> list[dict]:
        """The "Finish paper" marks that have not reached the server yet."""
        with self.mirror.lock:
            return [dict(row) for row in (self.mirror.state.get("done_pending") or [])]

    def queue_done(self, key: str, stage: str, tag: str) -> None:
        """Persist one done mark in ``state.done_pending`` (atomically).

        Called *before* the server call, so a crash can lose neither the mark
        nor the fact that it was requested.
        """
        row = {"key": str(key), "stage": str(stage), "tag": str(tag)}
        with self.mirror.lock:
            pending = self.mirror.state.setdefault("done_pending", [])
            if row not in pending:
                pending.append(row)
                self.mirror.save()

    def drop_done(self, key: str, stage: str, tag: str) -> None:
        row = {"key": str(key), "stage": str(stage), "tag": str(tag)}
        with self.mirror.lock:
            pending = list(self.mirror.state.get("done_pending") or [])
            kept = [r for r in pending if r != row]
            if len(kept) != len(pending):
                self.mirror.state["done_pending"] = kept
                self.mirror.save()

    def flush_done(self) -> list[dict]:
        """Send every queued mark.  An unreachable server leaves them queued.

        A mark the server refuses outright (a tag that is no longer the
        member's, a bad stage) can never succeed, so it is dropped instead of
        blocking the queue forever; the reason is kept in ``dropped_done``
        (and therefore in the next ``status().last_error``) instead of
        vanishing with the round.
        """
        sent: list[dict] = []
        self.dropped_done = []
        for row in self.done_pending():
            try:
                self.client.mark_done(self.pid, row["key"], row["stage"], row["tag"])
            except OfflineError:
                raise
            except (ForbiddenError, LockedError, BadRequestError, NotFoundError) as exc:
                self.dropped_done.append(f"done mark dropped ({row['key']}): {exc}")
                self.drop_done(row["key"], row["stage"], row["tag"])
                continue
            self.drop_done(row["key"], row["stage"], row["tag"])
            sent.append(row)
        return sent

    # -- push ----------------------------------------------------------------------------------------

    def push(self, force: bool = False) -> list[dict]:
        entries = self.outbox.ready(0 if force else self.debounce)
        results: list[dict] = []
        for i in range(0, len(entries), PUSH_BATCH):
            results.extend(self._push_batch(entries[i:i + PUSH_BATCH]))
        return results

    def _push_batch(self, entries: list[dict]) -> list[dict]:
        # 1. blobs: make sure the server has every queued version
        sendable = []
        need: dict[str, dict] = {}
        for e in entries:
            if e["new_hash"]:
                need.setdefault(e["new_hash"], e)
        missing = set(self.client.blobs_missing(self.pid, sorted(need))) if need else set()
        lost: set[str] = set()
        for digest in sorted(missing):
            data = self.mirror.read_version(digest, need[digest]["path"])
            if data is None:
                lost.add(digest)
                continue
            self.client.put_blob(self.pid, digest, data)
        for e in entries:
            if e["new_hash"] in lost:
                self._requeue_current(e["path"])     # the queued version vanished: queue what is on disk
            else:
                sendable.append(e)
        if not sendable:
            return []

        # 2. push
        self.outbox.mark_sent(sendable)
        resp = self.client.push(self.pid, self.client_id, [
            {"path": e["path"], "base_hash": e["base_hash"], "new_hash": e["new_hash"], "size": e.get("size", 0)}
            for e in sendable])
        by_path = {r.get("path"): r for r in (resp or {}).get("results", [])}

        # 3a. everything that needs no network: accepted, conflicts, rejections
        later: list[tuple[dict, dict]] = []
        for e in sendable:
            r = by_path.get(e["path"])
            if r is None:
                continue                                   # server skipped it: retry next tick
            status = r.get("status")
            if status == "accepted":
                self._on_accepted(e, r)
            elif status == "merged":
                later.append((e, r))
            elif status in ("conflict", "rejected"):
                reason = r.get("reason")
                if status == "rejected" and reason == "missing_blob":
                    continue                               # uploaded again next tick
                if status == "rejected" and reason == "excluded":
                    self.outbox.drop(e["path"])
                    continue
                self._on_conflict(e, r, status)
        self.mirror.save()
        # 3b. downloads: merged results, restores
        for e, r in later:
            self._on_merged(e, r)
        self._process_restores()
        self._gc_pending()
        return list(by_path.values())

    def _requeue_current(self, rel: str) -> None:
        full = self.mirror.abspath(rel)
        head = self.mirror.head_hash(rel)
        if os.path.exists(full):
            digest, size, tmp = self.mirror.copy_to_pending(full)
            self.mirror.commit_pending(tmp, digest)
            st = os.stat(full)
            self.outbox.add(rel, head, digest, size, st.st_mtime_ns)
        else:
            self.outbox.add(rel, head, None, 0, 0)

    def _remote_set(self, rel: str, digest: str | None, size: int = 0, seq: int | None = None) -> None:
        with self.mirror.lock:
            if digest is None:
                self.mirror.remote.pop(rel, None)
                return
            fc = self.mirror.classify(rel)
            r = self.mirror.remote.get(rel) or {}
            r.update({"hash": digest, "size": size, "kind": fc.kind, "key": fc.key, "tag": fc.tag})
            if seq is not None:
                r["seq"] = seq
            self.mirror.remote[rel] = r

    def _on_accepted(self, e: dict, r: dict) -> None:
        rel = e["path"]
        new = e["new_hash"]
        with self.mirror.lock:
            if new is None:
                self.mirror.drop_head(rel)
            else:
                self.mirror.set_head(rel, new, e.get("size", 0), e.get("mtime_ns", 0))
            self._remote_set(rel, new, e.get("size", 0), r.get("seq"))
            self.mirror.save()                     # head first ...
        self.outbox.ack(rel, new, new)             # ... then the outbox

    def _on_merged(self, e: dict, r: dict) -> None:
        """The server merged our version with a concurrent one into ``r.hash``."""
        rel = e["path"]
        pushed = e["new_hash"]
        merged_hash = r.get("hash")
        merged = self.client.get_blob_bytes(self.pid, merged_hash)     # before touching anything
        base_bytes = self.mirror.read_version(pushed, None) if pushed else None
        full = self.mirror.abspath(rel)
        requeue: tuple | None = None
        with self.mirror.lock:
            try:
                st = os.stat(full)
                unchanged = st.st_size == e.get("size") and st.st_mtime_ns == e.get("mtime_ns")
            except FileNotFoundError:
                st, unchanged = None, False
            if unchanged:
                self.mirror.write_version(rel, merged, merged_hash)
            elif st is None:
                # deleted locally meanwhile: the next scan queues the delete on top of the merge
                self.mirror.set_head(rel, merged_hash, -1, -1)
            else:
                # the user edited the file again while the push was in flight
                with open(full, "rb") as fh:
                    current = fh.read()
                cur_hash = protocol.sha256_bytes(current)
                if cur_hash == merged_hash:
                    self.mirror.set_head(rel, merged_hash, st.st_size, st.st_mtime_ns)
                else:
                    m = merge_mod.merge3(rel, base_bytes, current, merged) if base_bytes is not None else None
                    if m is not None and m.ok:
                        # fold the concurrent change into the user's newer version
                        self.mirror.write_version(rel, m.merged)
                        self.mirror.set_head(rel, merged_hash, -1, -1)
                        digest = self.mirror.put_pending_bytes(m.merged)
                        st2 = os.stat(full)
                        requeue = (merged_hash, digest, st2.st_size, st2.st_mtime_ns)
                    else:
                        # cannot fold it here: keep the user's bytes, push them on
                        # the base they were derived from so the server decides
                        self.mirror.set_head(rel, merged_hash, -1, -1)
                        digest = self.mirror.put_pending_bytes(current)
                        requeue = (pushed, digest, st.st_size, st.st_mtime_ns)
            self._remote_set(rel, merged_hash, len(merged), r.get("seq"))
            self.mirror.save()
        self.outbox.ack(rel, pushed, merged_hash)
        if requeue is not None:
            base, digest, size, mtime = requeue
            self.outbox.add(rel, base, digest, size, mtime)
            if self.outbox.get(rel) and self.outbox.get(rel)["base_hash"] != base:
                self.outbox.rebase(rel, base)
        self._notify([rel])

    def _on_conflict(self, e: dict, r: dict, status: str) -> None:
        rel = e["path"]
        mine = self.mirror.read_version(e["new_hash"], rel) if e["new_hash"] else None
        versions = []
        if mine is not None:
            versions.append(("", mine))
        try:
            with open(self.mirror.abspath(rel), "rb") as fh:
                current = fh.read()
            if mine is None or current != mine:
                versions.append((".latest" if mine is not None else "", current))
        except FileNotFoundError:
            pass
        if "hash" in r:
            server_hash = r.get("hash")
        else:
            with self.mirror.lock:
                rr = self.mirror.remote.get(rel)
                server_hash = rr.get("hash") if rr else None
        self.conflicts.record(
            path=rel, kind="conflict" if status == "conflict" else "rejected",
            server_id=r.get("conflict_id") if status == "conflict" else None,
            reason=r.get("reason"), base_hash=e["base_hash"], mine_hash=e["new_hash"],
            server_hash=server_hash, versions=versions)
        with self.mirror.lock:
            self.mirror.state.setdefault("restore", {})[rel] = {"hash": server_hash}
            self.mirror.save()
        self.outbox.drop(rel)

    def _process_restores(self) -> None:
        with self.mirror.lock:
            todo = dict(self.mirror.state.get("restore") or {})
        for rel, info in todo.items():
            digest = info.get("hash")
            if digest:
                self._download_into(rel, digest, force=True)
            else:
                self.mirror.remove_file(rel)
            with self.mirror.lock:
                self.mirror.state["restore"].pop(rel, None)
                self.mirror.save()
            self._notify([rel])

    # -- pull ----------------------------------------------------------------------------------------

    def pull(self) -> dict:
        with self.mirror.lock:
            since = int(self.mirror.state.get("seq") or 0)
            epoch = self.mirror.state.get("visibility_epoch")
        man = self.client.manifest(self.pid, since)
        full = since == 0
        if man.get("visibility_epoch") != epoch and not full:
            # the readable set may have grown (done mark, tags, blinding): start over
            man = self.client.manifest(self.pid, 0)
            full = True
        if man.get("visibility_epoch") != epoch:
            try:
                self.refresh_permissions()
            except OnlineError:
                pass
        files = man.get("files") or []
        touched: list[str] = []
        with self.mirror.lock:
            listed = set()
            for f in files:
                rel = protocol.normalize_path(f["path"])
                listed.add(rel)
                if f.get("deleted") or not f.get("hash"):
                    self.mirror.remote.pop(rel, None)
                else:
                    self.mirror.remote[rel] = {k: f.get(k) for k in ("hash", "size", "kind", "key", "tag", "seq")}
            if full:
                for rel in [p for p in self.mirror.remote if p not in listed]:
                    self.mirror.remote.pop(rel, None)          # no longer readable
            candidates = list(dict.fromkeys(list(listed) + list(self.mirror.state.get("stale") or [])))
        self._process_restores()
        for rel in candidates:
            if self._reconcile(rel):
                touched.append(rel)
        with self.mirror.lock:
            self.mirror.state["seq"] = max(int(man.get("seq") or 0), since if not full else 0)
            self.mirror.state["visibility_epoch"] = man.get("visibility_epoch")
            self.mirror.save()
        if any(self.cache.tier(p) in ("lru", "supplement") for p in touched):
            self.cache.enforce()
        if touched:
            self._notify(touched)
        return {"seq": man.get("seq"), "full": full, "files": len(files), "written": touched}

    def _wanted(self, rel: str, fc) -> bool:
        if protocol.is_core(rel, fc):
            return True
        if os.path.exists(self.mirror.abspath(rel)):
            return True                                     # cached copy: keep it current
        tier = self.cache.tier(rel)
        if tier == "pinned":
            return not rel.startswith("Supplements/") or bool(self.mirror.state["cache"].get("supplements"))
        return False

    def _reconcile(self, rel: str) -> bool:
        """Bring one path in line with the server; True when the file was written."""
        fc = self.mirror.classify(rel)
        if fc.kind == "excluded":
            self.mirror.discard_stale(rel)
            return False
        with self.mirror.lock:
            r = self.mirror.remote.get(rel)
            head = self.mirror.heads.get(rel)
            if rel in (self.mirror.state.get("restore") or {}):
                return False
        server_hash = r.get("hash") if r else None

        pe = self.outbox.get(rel)
        if pe is not None:
            if pe["new_hash"] == server_hash:
                # our push already landed (crash before the ack): adopt it, no re-push
                with self.mirror.lock:
                    if server_hash is None:
                        self.mirror.drop_head(rel)
                    else:
                        self.mirror.set_head(rel, server_hash, pe.get("size", 0), pe.get("mtime_ns", 0))
                    self.mirror.save()
                self.outbox.ack(rel, pe["new_hash"], pe["new_hash"])
            elif server_hash in (pe.get("sent") or []) and pe["base_hash"] != server_hash:
                # an earlier version of ours landed, the user edited again since:
                # the newer version now builds on it (no conflict with ourselves)
                with self.mirror.lock:
                    if server_hash is None:
                        self.mirror.drop_head(rel)
                    else:
                        self.mirror.set_head(rel, server_hash, -1, -1)
                    self.mirror.save()
                self.outbox.rebase(rel, server_hash)
            self.mirror.discard_stale(rel)
            return False                                    # the push resolves everything else

        writable = self.permissions.can_write(rel, fc)
        if server_hash is None:                             # deleted on the server / unknown
            if head is not None:
                ls = self.mirror.local_state(rel)
                if ls in ("clean", "absent") or not writable:
                    self.mirror.remove_file(rel)
                    self.mirror.discard_stale(rel)
                    return True
                self.mirror.add_stale(rel)                  # local edit: the push decides
                return False
            self.mirror.discard_stale(rel)
            return False

        if head is not None and head["hash"] == server_hash and os.path.exists(self.mirror.abspath(rel)):
            self.mirror.discard_stale(rel)
            return False
        if not self._wanted(rel, fc):
            self.mirror.discard_stale(rel)
            return False
        ls = self.mirror.local_state(rel)
        if ls == "untracked":
            if self.mirror.hash_file(self.mirror.abspath(rel)) == server_hash:
                self.mirror.set_head(rel, server_hash)
                self.mirror.discard_stale(rel)
                return False
            if writable:
                self.mirror.add_stale(rel)                  # the scan queues it; the push decides
                return False
        elif ls == "modified" and writable:
            self.mirror.add_stale(rel)
            return False
        # absent, clean-but-outdated, or an unwritable local edit (local-only): overwrite
        return self._download_into(rel, server_hash, force=not writable)

    def _download_into(self, rel: str, digest: str, force: bool = False) -> bool:
        """Download *digest* and install it as *rel*.

        Unless *force*, the install is skipped when the local file was edited
        while the download ran (the path stays ``stale`` and is retried).
        """
        before = self.mirror.local_state(rel) if not force else None
        tmp = os.path.join(self.mirror.tmp_dir, f"dl-{digest[:16]}-{threading.get_ident()}")
        try:
            self.client.get_blob(self.pid, digest, tmp)
        except OfflineError:
            self.mirror.add_stale(rel)
            raise
        with self.mirror.lock:
            if not force:
                now = self.mirror.local_state(rel)
                if now == "modified" and before != "modified" and self.permissions.can_write(rel):
                    os.unlink(tmp)
                    self.mirror.add_stale(rel)
                    return False
            self.mirror.install_file(rel, tmp, digest)
            self.mirror.discard_stale(rel)
            if self.cache.tier(rel) in ("lru", "supplement", "pinned"):
                self.cache.touch(rel)
        return True

    def fetch(self, rel: str) -> str:
        """Make sure *rel* is local and current (on-demand download)."""
        rel = protocol.normalize_path(rel)
        full = self.mirror.abspath(rel)
        with self.mirror.lock:
            r = self.mirror.remote.get(rel)
            head = self.mirror.heads.get(rel)
        exists = os.path.exists(full)
        if r is None:
            if exists:
                self.cache.touch(rel)
                return full
            raise FileNotFoundError(rel)
        if exists and head and head["hash"] == r["hash"]:
            self.cache.touch(rel)
            return full
        if exists and rel in self.outbox:
            return full                                     # our own unpushed version
        try:
            self._download_into(rel, r["hash"], force=not exists)
        except OfflineError:
            if exists:
                return full                                 # outdated, but usable offline
            raise
        with self.mirror.lock:
            self.mirror.save()
        if self.cache.tier(rel) in ("lru", "supplement"):
            self.cache.enforce()
        return full

    def _notify(self, paths: list[str]) -> None:
        cb = self.on_change
        if cb is not None:
            try:
                cb(list(paths))
            except Exception:       # a UI callback must never break the sync
                pass

    # -- conflicts --------------------------------------------------------------------------------------

    def refresh_conflicts(self) -> list[dict]:
        """Close local records of server conflicts that are resolved on the server."""
        try:
            open_ids = {str(c.get("id")) for c in self.client.conflicts(self.pid, "open")}
        except OnlineError:
            return self.conflicts.list("open")
        for rec in self.conflicts.list("open"):
            if rec["kind"] == "conflict" and rec.get("server_id") is not None \
                    and str(rec["server_id"]) not in open_ids:
                self.conflicts.mark_resolved(rec["id"], "server")
        return self.conflicts.list("open")

    def resolve_conflict(self, cid, resolution: str, merged_bytes: bytes | None = None) -> dict:
        if resolution not in ("mine", "theirs", "merged"):
            raise ValueError(f"unknown resolution {resolution!r}")
        with self.sync_lock:
            rec = self.conflicts.get(cid)
            if rec is None:
                raise KeyError(cid)
            rel = rec["path"]
            server_conflict = rec["kind"] == "conflict" and rec.get("server_id") is not None
            if resolution == "theirs":
                if server_conflict:
                    self.client.resolve_conflict(self.pid, rec["server_id"], "theirs")
                return self.conflicts.mark_resolved(cid, "theirs")

            if resolution == "merged":
                if merged_bytes is None:
                    raise ValueError("merged resolution needs merged_bytes")
                data: bytes | None = merged_bytes
            else:
                data = self.conflicts.mine_bytes(cid) if rec.get("mine_hash") else None
            digest = self.mirror.put_pending_bytes(data) if data is not None else None

            if server_conflict:
                if digest and self.client.blobs_missing(self.pid, [digest]):
                    self.client.put_blob(self.pid, digest, data)
                res = self.client.resolve_conflict(self.pid, rec["server_id"], resolution, new_hash=digest) or {}
                with self.mirror.lock:
                    if data is None:
                        self.mirror.remove_file(rel)
                        self._remote_set(rel, None)
                    else:
                        self.mirror.write_version(rel, data, digest)
                        self._remote_set(rel, digest, len(data), res.get("seq"))
                    self.mirror.save()
                self.mirror.remove_pending(digest)
            else:
                # a rejection has no server record: queue it as an ordinary push
                with self.mirror.lock:
                    base = self.mirror.head_hash(rel)
                    if data is None:
                        self.mirror.remove_file(rel)
                        self.outbox.add(rel, base, None, 0, 0)
                    else:
                        full = self.mirror.abspath(rel)
                        atomic_write_bytes(full, data, tmp_dir=self.mirror.tmp_dir, fsync=self.mirror.fsync)
                        if base:
                            self.mirror.set_head(rel, base, -1, -1)
                        st = os.stat(full)
                        self.outbox.add(rel, base, digest, st.st_size, st.st_mtime_ns)
                    self.mirror.save()
            self._notify([rel])
            return self.conflicts.mark_resolved(cid, resolution)

    # -- background thread ---------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="online-sync", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout)
        self._thread = None

    def kick(self) -> None:
        """Run the next round now (scan + push + pull)."""
        self._wake.set()

    def note_offline(self, exc: BaseException) -> None:
        """Record an unreachable server outside a round (a queued done mark).

        Same state :meth:`tick` sets on ``OfflineError``: the badge flips to
        offline at once instead of waiting for the next scheduled round.
        """
        self.mode = "offline"
        self.last_error = str(exc)
        self.backoff = BACKOFF_MIN if self.backoff <= 0 else min(self.backoff * 2, BACKOFF_MAX)
        self.next_retry = self.clock() + self.backoff

    def _next_wait(self, ok: bool) -> float:
        if not ok and self.mode == "offline":
            return self.backoff or BACKOFF_MIN
        if not ok:
            return max(self.interval, 60.0)
        wait = self.interval
        entries = self.outbox.entries()
        if entries:
            soonest = min(e.get("last_ts") or 0 for e in entries) + self.debounce - self.clock()
            wait = min(wait, max(0.5, soonest))
        return wait

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                ok = self.tick()
            except Exception as exc:          # never let the thread die
                self.mode = "error"
                self.last_error = f"{type(exc).__name__}: {exc}"
                ok = False
            if self._stop.is_set():
                break
            self._wake.wait(self._next_wait(ok))
            self._wake.clear()
