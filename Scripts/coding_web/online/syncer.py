"""The sync engine: scan -> enqueue -> push -> pull (INTERFACES §4).

One :class:`Syncer` per open project.  :meth:`Syncer.tick` runs one complete
round and is serialised by ``sync_lock`` (the background thread, "sync now"
and conflict resolution never overlap); ``mirror.lock`` guards the state dict
and is held only for short in-memory updates, never across a network call.

The manifest is the only authority on what this member may read: a path that
disappears from a **full** manifest (blinding hid another tag's file, the
master workbook, the settlement store or a ``Results/`` agreement product;
membership changed; the server deleted it) is retired - its head, stale and
restore marks are dropped and its bytes are unlinked, so a re-blinded coder
does not keep an old copy.  An unpushed outbox entry, or a local edit of a
still-writable path, is the user's work and survives.

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

import hashlib
import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

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

_HEX = frozenset("0123456789abcdef")


def protocol_is_hash(value: str) -> bool:
    """A lowercase sha256 hex digest (never a path fragment)."""
    return len(value) == 64 and all(c in _HEX for c in value)


BACKOFF_MIN = 5.0
BACKOFF_MAX = 300.0
PUSH_BATCH = 200
#: seconds between two state saves while a batch downloads
SAVE_EVERY = 3.0


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class Syncer:
    SAVE_EVERY = SAVE_EVERY

    def __init__(self, mirror: Mirror, client, permissions: Permissions | None = None, *,
                 outbox: Outbox | None = None, conflicts: ConflictStore | None = None,
                 cache: Cache | None = None, debounce: float = protocol.PUSH_DEBOUNCE_SECS,
                 interval: float = protocol.SYNC_INTERVAL_SECS, clock=time.time,
                 local_blobs: str | None = None):
        self.mirror = mirror
        #: Server-local read-only blob store (browser workers): versions are
        #: installed as symlinks to ``<local_blobs>/<h[:2]>/<h>`` when present.
        self.local_blobs = os.path.abspath(local_blobs) if local_blobs else None
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
        #: optional callback() after the *listing* changed (paths appeared or left),
        #: before any download: the app refreshes its key list / paper list
        self.on_listing = None
        #: bumped whenever the set of remote paths (or their versions) changes
        self.files_rev = 0
        #: parallel downloads per round (WebDAV/the server client caps requests too)
        self.parallel = 4
        try:
            self.parallel = max(1, int(getattr(getattr(client, "dav", None), "max_parallel", 0) or 4))
        except Exception:                   # a test double without the attribute
            pass
        #: a background round downloads for at most this long, then parks the
        #: rest as ``stale`` and lets a push/the next round run (kicked at once)
        self.round_budget = 20.0
        #: study keys whose files jump the download queue (the open paper)
        self.priority_keys: list[str] = []
        self._more_pending = False
        self._tokens_refreshed = False
        self._progress_lock = threading.Lock()
        self.progress = self._idle_progress()
        self._recover()

    # -- download progress -----------------------------------------------------------

    @staticmethod
    def _idle_progress() -> dict:
        return {"phase": "idle", "files_done": 0, "files_total": 0, "bytes_done": 0,
                "bytes_total": 0, "started": None, "rate_bps": 0.0, "eta_s": None,
                "current": None, "failed": 0}

    def download_progress(self) -> dict:
        with self._progress_lock:
            return dict(self.progress)

    def _resolve(self, digest):
        fn = getattr(self.client, "resolve_hash", None)
        if fn is None or not digest:
            return digest
        try:
            return fn(digest) or digest
        except Exception:                     # a resolver may never break a round
            return digest

    def _refresh_tokens(self) -> None:
        """Replace version tokens in ``remote`` that a download already resolved.

        After a crash the state can still hold ``nc1-<etag>`` for a file that
        arrived after the last state save; the client's journal knows its
        content hash.  With the real hash the first scan adopts the file on
        disk instead of taking it for a local creation (which would have cost
        a push and a GET per file).
        """
        self._tokens_refreshed = True
        if getattr(self.client, "resolve_hash", None) is None:
            return
        with self.mirror.lock:
            for r in self.mirror.remote.values():
                h = r.get("hash")
                if h and not protocol_is_hash(h):
                    real = self._resolve(h)
                    if real != h:
                        r["hash"] = real

    def _notify_listing(self) -> None:
        cb = self.on_listing
        if cb is not None:
            try:
                cb()
            except Exception:                 # a UI callback must never break the sync
                pass

    @staticmethod
    def _priority(rel: str) -> int:
        top = rel.split("/", 1)[0]
        if "/" not in rel or top in ("Project", "Screening", "Fulltext"):
            return 0
        return {"Codings": 1, "Extraction": 2, "Text": 3, "Reproduction": 4,
                "Datasets": 5}.get(top, 6)

    def download_many(self, jobs, progress=None, budget: float | None = None,
                      track: bool = True) -> tuple[list[str], bool]:
        """Run downloads ``[(rel, hash, force)]`` in parallel; returns ``(written, interrupted)``.

        At most :attr:`parallel` downloads run at once (the WebDAV client also
        caps requests in flight and honours 429 ``Retry-After``).  The state is
        saved every :data:`SAVE_EVERY` seconds, so a crash or a closed window
        keeps every finished file; :meth:`stop` and *budget* (seconds) end the
        batch early - the paths not reached are parked as ``stale`` and the
        next round continues with them (``interrupted`` is then True).
        ``OfflineError`` parks the rest and is re-raised; the first other
        failure is re-raised after the batch (the round reports it), a path
        the server no longer has is parked.
        """
        jobs = list(dict.fromkeys(jobs))
        if not jobs:
            return [], False
        prio = set(self.priority_keys or ())

        def _key(job):
            rel = job[0]
            fc = self.mirror.classify(rel)
            return (0 if (prio and fc.key in prio) else 1, self._priority(rel), rel)

        jobs.sort(key=_key)
        with self.mirror.lock:
            sizes = {j[0]: int((self.mirror.remote.get(j[0]) or {}).get("size") or 0) for j in jobs}
        t0 = time.monotonic()
        if track:
            with self._progress_lock:
                prev = self.progress if self._more_pending and self.progress.get("phase") != "idle" else None
                base_files = int(prev["files_done"]) if prev else 0
                base_bytes = int(prev["bytes_done"]) if prev else 0
                started = prev["started"] if prev else time.time()
                self.progress = {"phase": "downloading", "files_done": base_files,
                                 "files_total": base_files + len(jobs),
                                 "bytes_done": base_bytes,
                                 "bytes_total": base_bytes + sum(sizes.values()),
                                 "started": started, "rate_bps": 0.0, "eta_s": None,
                                 "current": None, "failed": 0}
            self._more_pending = False
        written: list[str] = []
        offline: BaseException | None = None
        first_error: BaseException | None = None
        stopped = False
        last_save = time.monotonic()
        workers = max(1, min(self.parallel, len(jobs)))
        it = iter(jobs)
        pending: dict = {}

        def _submit(ex) -> bool:
            nonlocal stopped
            if offline is not None:
                return False
            if self._stop.is_set() or (budget is not None and time.monotonic() - t0 > budget):
                stopped = True
                return False
            job = next(it, None)
            if job is None:
                return False
            pending[ex.submit(self._download_into, job[0], job[1], job[2])] = job
            return True

        done_n = 0
        ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="online-dl")
        try:
            for _ in range(workers):
                if not _submit(ex):
                    break
            while pending:
                finished, _ = wait(list(pending), timeout=0.5, return_when=FIRST_COMPLETED)
                for fut in finished:
                    rel, _h, _f = pending.pop(fut)
                    try:
                        if fut.result():
                            written.append(rel)
                    except OfflineError as exc:
                        offline = offline or exc
                        self.mirror.add_stale(rel)
                    except (NotFoundError, FileNotFoundError):
                        self.mirror.add_stale(rel)     # gone meanwhile: the next listing decides
                    except Exception as exc:           # noqa: BLE001 - reported after the batch
                        first_error = first_error or exc
                        self.mirror.add_stale(rel)
                        if track:
                            with self._progress_lock:
                                self.progress["failed"] += 1
                    done_n += 1
                    if track:
                        with self._progress_lock:
                            p = self.progress
                            p["files_done"] += 1
                            p["bytes_done"] += sizes.get(rel, 0)
                            p["current"] = rel
                            secs = max(1e-3, time.time() - float(p["started"] or time.time()))
                            p["rate_bps"] = p["bytes_done"] / secs
                            left_files = p["files_total"] - p["files_done"]
                            per_file = secs / max(1, p["files_done"])
                            left_bytes = max(0, p["bytes_total"] - p["bytes_done"])
                            eta = max(left_files * per_file * 0.5,
                                      left_bytes / p["rate_bps"] if p["rate_bps"] > 0 else 0)
                            p["eta_s"] = round(eta, 1) if left_files else 0.0
                    if progress is not None:
                        try:
                            progress(done_n, len(jobs), rel)
                        except Exception:              # noqa: BLE001
                            pass
                if time.monotonic() - last_save >= self.SAVE_EVERY:
                    self.mirror.save()
                    last_save = time.monotonic()
                while len(pending) < workers and _submit(ex):
                    pass
        finally:
            ex.shutdown(wait=True)
            # the paths never started stay queued for the next round
            rest = list(it)
            for rel, _h, _f in rest:
                self.mirror.add_stale(rel)
            self.mirror.save()
            if track:
                with self._progress_lock:
                    if rest or stopped:
                        self.progress["phase"] = "paused" if self._stop.is_set() else "downloading"
                    else:
                        self.progress = dict(self.progress, phase="idle", current=None, eta_s=0.0)
        interrupted = bool(rest) or stopped
        if interrupted and track:
            self._more_pending = True
        if offline is not None:
            raise offline
        if first_error is not None:
            raise first_error
        return written, interrupted

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
            "download": self.download_progress(),
            "files_rev": self.files_rev,
        }

    # -- one round ---------------------------------------------------------------------------

    def tick(self, force: bool = False) -> bool:
        """Scan, push, pull.  Returns True when the round reached the server.

        Never raises for network trouble: ``OfflineError`` switches the mode
        to ``offline`` (local scanning still happened, so ``pending`` is
        right); auth/protocol errors switch it to ``error``.
        """
        with self.sync_lock:
            if not self._tokens_refreshed:
                self._refresh_tokens()
            try:
                self.scan()
            except OSError as exc:                 # disk trouble: report, keep going
                self.last_error = f"scan failed: {exc}"
            prev = self.mode
            self.mode = "syncing"
            self.last_round = {"ok": False, "pushed": 0, "pulled": 0, "done_sent": 0}
            try:
                pulled = None
                if not self._initial_pull_done or self.outbox.unconfirmed():
                    # start-up, or an earlier push whose outcome is unknown:
                    # learn what the server already has before pushing again
                    pulled = self.pull(budget=self._budget())
                    self._initial_pull_done = True
                # queued "Finish paper" marks go first: they change the caller's
                # visibility_epoch, which the pull below then sees
                self.last_round["done_sent"] = len(self.flush_done())
                self.last_round["pushed"] = len(self.push(force=force) or [])
                if pulled is None or self.last_round["pushed"] or self.last_round["done_sent"]:
                    # (a round that just pulled and sent nothing needs no second
                    # listing - on a 25k-file share that is a whole PROPFIND)
                    pulled = self.pull(budget=self._budget())
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

    def _budget(self) -> float | None:
        """The download budget of one round (``None``: unbounded)."""
        return self.round_budget

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

    def pull(self, progress=None, budget: float | None = None) -> dict:
        """Bring the mirror in line with the manifest.

        *progress* (optional) is called ``progress(done, total, path)`` for
        every candidate the round examines, so a batched on-demand fetch
        (:meth:`OnlineStore.ensure_many`) can report how far it is.

        The listing is applied (and saved) before anything is downloaded; the
        downloads then run in parallel (:meth:`download_many`).  *budget*
        (seconds) bounds them: what is left is parked as ``stale`` and the
        next round - started at once by the background loop - continues.
        """
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
        retired: list[str] = []
        with self.mirror.lock:
            listed = set()
            remote_changed = False
            for f in files:
                rel = protocol.normalize_path(f["path"])
                listed.add(rel)
                if f.get("deleted") or not f.get("hash"):
                    if self.mirror.remote.pop(rel, None) is not None:
                        remote_changed = True
                else:
                    row = {k: f.get(k) for k in ("hash", "size", "kind", "key", "tag", "seq")}
                    old = self.mirror.remote.get(rel)
                    if old is not None and old.get("hash") != row["hash"] \
                            and self._resolve(row["hash"]) == old.get("hash"):
                        row["hash"] = old["hash"]   # a token we already resolved by downloading
                    if old != row:
                        remote_changed = True
                    self.mirror.remote[rel] = row
            if full:
                for rel in [p for p in self.mirror.remote if p not in listed]:
                    self.mirror.remote.pop(rel, None)          # no longer readable
                    retired.append(rel)
            candidates = list(dict.fromkeys(list(listed) + list(self.mirror.state.get("stale") or [])))
            if remote_changed or retired:
                self.files_rev += 1
        if remote_changed or retired:
            # the listing is what the paper list and every listing read: save it
            # (and tell the app) *before* the downloads, so a first open shows
            # the whole project at once and a crash keeps it
            self.mirror.save()
            self._notify_listing()
        # A path we may no longer read (blinding: another tag's files, the master
        # workbook, settlements, Results/ agreement products) must leave the
        # mirror too, exactly like a server deletion - a blinded coder must not
        # keep a copy that arrived before the stage was blinded again.  This runs
        # before the restores so a pending restore of a now-hidden file is dropped,
        # not downloaded (the server would answer 403).
        for rel in retired:
            for path in self._retire(rel):
                touched.append(path)
        self._process_restores()
        jobs: list[tuple[str, str, bool]] = []
        n_cand = len(candidates)
        for _i, rel in enumerate(candidates):
            if self._reconcile(rel, defer=jobs):
                touched.append(rel)
            if progress is not None and (not jobs or jobs[-1][0] != rel):
                try:
                    progress(_i + 1, n_cand, rel)
                except Exception:        # a progress callback may never break a pull
                    pass
        if jobs:
            done, interrupted = self.download_many(jobs, progress=progress, budget=budget)
            touched.extend(done)
            if interrupted:
                # parked: the paths not reached are ``stale`` and the next round
                # (kicked at once) continues; seq still advances because every
                # path of the window is handled (written or parked)
                self._more_pending = True
        with self.mirror.lock:
            self.mirror.state["seq"] = max(int(man.get("seq") or 0), since if not full else 0)
            self.mirror.state["visibility_epoch"] = man.get("visibility_epoch")
            self.mirror.save()
        if any(self.cache.tier(p) in ("lru", "supplement") for p in touched):
            self.cache.enforce()
        if touched:
            self._notify(touched)
        return {"seq": man.get("seq"), "full": full, "files": len(files), "written": touched}

    def _retire(self, rel: str) -> list[str]:
        """Remove a path the server no longer lets this member read.

        Returns the paths whose bytes were removed from the mirror.  An unpushed
        local version of a *writable* file is the user's work: it is kept (the
        push decides) - but every path that blinding hides is one this member
        cannot write anyway (a foreign tag, a derived/admin consensus file), so
        a local-only edit of it is discarded exactly as a server version would
        overwrite it (INTERFACES 4).  The head is dropped, so the next scan does
        not report the vanished file as a deletion.
        """
        fc = self.mirror.classify(rel)
        if fc.kind == "excluded":
            return []
        if rel in self.outbox:
            return []
        full = self.mirror.abspath(rel)
        if not os.path.exists(full):
            with self.mirror.lock:
                self.mirror.drop_head(rel)
                self.mirror.discard_stale(rel)
            return []
        if self.mirror.local_state(rel) == "modified" and self.permissions.can_write(rel, fc):
            return []
        self.mirror.remove_file(rel)
        with self.mirror.lock:
            self.mirror.discard_stale(rel)
            self.mirror.state["local_only"].pop(rel, None)
            (self.mirror.state.get("restore") or {}).pop(rel, None)
            cache = self.mirror.state.get("cache") or {}
            (cache.get("atime") or {}).pop(rel, None)
            (cache.get("evicted") or {}).pop(rel, None)
        self.mirror.save()
        return [rel]

    def _wanted(self, rel: str, fc) -> bool:
        if protocol.is_core(rel, fc):
            return True
        if self.local_blobs:
            # a server-local worker: a link costs no download and no disk, so
            # every readable file is made local (no on-demand fetches later)
            with self.mirror.lock:
                r = self.mirror.remote.get(rel)
            if r is not None and self._local_blob(r.get("hash")) is not None:
                return True
        if self.mirror.is_stale(rel):
            return True                     # an explicit request (preload / analysis)
        if os.path.exists(self.mirror.abspath(rel)):
            return True                                     # cached copy: keep it current
        tier = self.cache.tier(rel)
        if tier == "pinned":
            return not rel.startswith("Supplements/") or bool(self.mirror.state["cache"].get("supplements"))
        return False

    def _reconcile(self, rel: str, defer: list | None = None) -> bool:
        """Bring one path in line with the server; True when the file was written.

        With *defer* (a list) a needed download is appended to it as
        ``(rel, hash, force)`` instead of being run here, so :meth:`pull` can
        run the downloads in parallel (:meth:`download_many`).
        """
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
        if defer is not None:
            defer.append((rel, server_hash, not writable))
            return False
        return self._download_into(rel, server_hash, force=not writable)

    def _download_into(self, rel: str, digest: str, force: bool = False) -> bool:
        """Download *digest* and install it as *rel*.

        Unless *force*, the install is skipped when the local file was edited
        while the download ran (the path stays ``stale`` and is retried).
        """
        before = self.mirror.local_state(rel) if not force else None
        blob = self._local_blob(digest)
        if blob is not None:
            # Server-local worker: link instead of download.  *digest* always
            # comes from this user's own manifest, so visibility stays the
            # server's decision.
            with self.mirror.lock:
                if not force:
                    now = self.mirror.local_state(rel)
                    if now == "modified" and before != "modified" and self.permissions.can_write(rel):
                        self.mirror.add_stale(rel)
                        return False
                try:
                    self.mirror.link_blob(rel, blob, digest)
                except OSError as exc:
                    # no symlink support here: download like a desktop from now on
                    self.local_blobs = None
                    self.last_error = f"blob links disabled: {exc}"
                else:
                    self.mirror.discard_stale(rel)
                    if self.cache.tier(rel) in ("lru", "supplement", "pinned"):
                        self.cache.touch(rel)
                    return True
        tmp = os.path.join(self.mirror.tmp_dir,
                           f"dl-{hashlib.sha1(str(digest).encode()).hexdigest()[:16]}-{threading.get_ident()}")
        try:
            self.client.get_blob(self.pid, digest, tmp)
        except OfflineError:
            self.mirror.add_stale(rel)
            raise
        real = self._resolve(digest)
        if real != digest:
            # a version token (Nextcloud): the download learnt the content hash,
            # which is what the head must carry (the base of the next merge)
            with self.mirror.lock:
                r = self.mirror.remote.get(rel)
                if r is not None and r.get("hash") == digest:
                    r["hash"] = real
            digest = real
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

    def _local_blob(self, digest: str | None) -> str | None:
        """Path of *digest* in the server-local blob store, or None."""
        if not self.local_blobs or not digest or not protocol_is_hash(digest):
            return None
        path = os.path.join(self.local_blobs, digest[:2], digest)
        return path if os.path.isfile(path) else None

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
            if ok and self._more_pending:
                continue                      # a parked backlog: next round at once
            self._wake.wait(self._next_wait(ok))
            self._wake.clear()
