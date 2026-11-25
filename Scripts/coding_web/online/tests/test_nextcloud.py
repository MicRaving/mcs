"""Nextcloud mode: the engine's sync scenarios against a real WebDAV share.

The same ``OnlineStore`` / ``Syncer`` / ``Outbox`` / ``ConflictStore`` as the
studio-server tests (``test_sync.py``), but connected through
:class:`online.nextcloud.NextcloudClient`, which speaks WebDAV to a shared
folder.  Nothing in the engine differs - that is the point of the phase.

Two servers run the same scenarios:

* :mod:`online.tests.webdav_fake` - stdlib, atomic conditional PUT (the
  guarantee the lock protocol needs);
* :mod:`online.tests.wsgidav_server` - a real WebDAV implementation, when
  ``temp/nextcloud/venv`` has ``wsgidav``+``cheroot``.  Its ``If-Match`` /
  ``If-None-Match`` are correct but its check-then-write is not atomic, so the
  lock-race assertion is relaxed there (and the finding is printed).

Scenarios (PLAN §17 phase 9): owned edits converge, disjoint edits of a shared
file merge, the same field becomes a conflict, a lock race has one winner, an
expired lock can be taken over, offline edits land on reconnect, and a kill in
the middle of an upload loses nothing.
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from online.store import OnlineStore
from online.tests import wsgidav_server
from online.tests.webdav_fake import WebDAVFake

PASSWORD = "app-password"

MEMBERS = {"members": [
    {"username": "own", "role": "owner", "tags": ["O"], "tasks": []},
    {"username": "a", "role": "coder", "tags": ["A"], "tasks": ["coding", "abstract", "fulltext", "settle"]},
    {"username": "b", "role": "coder", "tags": ["B"], "tasks": ["coding", "abstract", "fulltext", "settle"]},
    {"username": "c", "role": "coder", "tags": ["C"], "tasks": ["coding", "settle"]},
]}


class Crash(BaseException):
    """Simulated process death (not an Exception, so no handler swallows it)."""


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(params=["fake", "wsgidav"])
def backend(request, tmp_path):
    """One WebDAV share per test, on the fake or on a real implementation."""
    kind = request.param
    share = tmp_path / "share"
    share.mkdir()
    (share / "Project").mkdir()
    (share / "Project" / "project.json").write_text('{"scheme": "x"}\n', encoding="utf-8")
    studio = share / ".studio"
    studio.mkdir()
    (studio / "members.json").write_text(json.dumps(MEMBERS), encoding="utf-8")

    if kind == "wsgidav":
        if not wsgidav_server.available():
            pytest.skip("wsgidav/cheroot not installed in temp/nextcloud/venv")
        server = wsgidav_server.WsgiDAVServer(str(share))
        atomic_locks = False           # check-then-create is not atomic in WsgiDAV
        etag_settle = 1.05             # its ETag is mtime(1 s)+size, see the docstring
    else:
        server = WebDAVFake(str(share))
        atomic_locks = True
        etag_settle = 0.0
    url = server.start()

    class Backend:
        name = kind
        root = str(share)
        atomic_conditional_writes = atomic_locks
        settle = etag_settle

        def client(self, username: str, *, clock=None, folder: str = "") -> "NCNode":
            return NCNode(url, str(tmp_path / f"mirror-{username}-{time.monotonic_ns()}"),
                          username, clock=clock, folder=folder, settle=etag_settle)

    be = Backend()
    be.url = url
    be.tmp = tmp_path
    be._state = getattr(server, "state", None)
    try:
        yield be
    finally:
        server.stop()


class NCNode:
    """One desktop client of the shared folder (its own mirror + store)."""

    def __init__(self, url: str, base: str, username: str, *, clock=None, folder: str = "",
                 settle: float = 0.0):
        self.url = url
        self.base = base
        self.username = username
        self.clock = clock
        self.settle = settle
        self.store = self._new_store()
        projects = self.store.connect_nextcloud(url, username, PASSWORD, folder=folder,
                                                webdav_root=url)
        self.pid = projects[0]["id"]
        self.root = self.store.open(self.pid, start=False)

    def _new_store(self) -> OnlineStore:
        store = OnlineStore(base_dir=self.base, debounce=0, autostart=False,
                            use_keyring=False, fsync=False, clock=self.clock or time.time)
        return store

    def restart(self) -> None:
        """Drop the objects and rebuild from disk (a crash + relaunch)."""
        try:
            self.store.client.dav.hook = None
        except Exception:                       # noqa: BLE001
            pass
        self.store = self._new_store()
        projects = self.store.connect_nextcloud(self.url, self.username, PASSWORD,
                                                webdav_root=self.url)
        self.pid = projects[0]["id"]
        self.root = self.store.open(self.pid, start=False)

    # -- shorthands -------------------------------------------------------
    @property
    def syncer(self):
        return self.store.syncer

    @property
    def client(self):
        return self.store.client

    def tick(self) -> bool:
        return self.syncer.tick(force=True)

    def status(self) -> dict:
        return self.store.status()

    def path(self, rel: str) -> str:
        return os.path.join(self.root, *rel.split("/"))

    def write(self, rel: str, data) -> None:
        if isinstance(data, (dict, list)):
            data = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
        elif isinstance(data, str):
            data = data.encode()
        full = self.path(rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        old = None
        try:
            old = os.stat(full).st_mtime_ns
        except FileNotFoundError:
            pass
        if old is not None and self.settle:
            time.sleep(self.settle)      # WsgiDAV's ETag has one-second resolution
        with open(full, "wb") as fh:
            fh.write(data)
        if old is not None and os.stat(full).st_mtime_ns <= old:
            os.utime(full, ns=(old + 1_000_000, old + 1_000_000))

    def read(self, rel: str):
        try:
            with open(self.path(rel), "rb") as fh:
                return fh.read()
        except FileNotFoundError:
            return None

    def read_json(self, rel: str):
        data = self.read(rel)
        return None if data is None else json.loads(data)

    def offline(self, flag: bool = True) -> None:
        self.client.set_offline(flag)

    def exists(self, rel: str) -> bool:
        return os.path.exists(self.path(rel))

    def close(self) -> None:
        try:
            self.store.close()
        except Exception:                       # noqa: BLE001
            pass


def _two(backend):
    a = backend.client("a")
    b = backend.client("b")
    assert a.tick() and b.tick()
    return a, b


def _make_shared(a, b, rel: str, obj) -> None:
    a.write(rel, obj)
    assert a.tick()
    assert b.tick()
    assert b.read_json(rel) == obj


# ---------------------------------------------------------------------------
# 1. owned edits converge
# ---------------------------------------------------------------------------

def test_owned_coding_files_converge(backend):
    a, b = _two(backend)
    try:
        a.write("Codings/K1 - A.json", {"Study_ID": "K1", "coder": "A", "v": 1})
        b.write("Codings/K1 - B.json", {"Study_ID": "K1", "coder": "B", "v": 2})
        assert a.tick() and b.tick()
        assert a.tick() and b.tick()      # a second round: each sees the other's push
        assert b.read_json("Codings/K1 - A.json")["coder"] == "A"
        assert a.read_json("Codings/K1 - B.json")["coder"] == "B"
        # a later edit of an owned file keeps converging
        a.write("Codings/K1 - A.json", {"Study_ID": "K1", "coder": "A", "v": 9})
        assert a.tick()
        assert b.tick()
        assert b.read_json("Codings/K1 - A.json")["v"] == 9
        # the shared tree carries no sync state
        names = {e["name"] for e in a.store.listdir("")}
        assert ".studio" not in names
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------------------
# 2. disjoint edits of a shared file merge
# ---------------------------------------------------------------------------

def test_disjoint_edits_of_a_shared_file_merge(backend):
    a, b = _two(backend)
    try:
        _make_shared(a, b, "Extraction/K1.json", {"Study_ID": "K1", "alpha": 1, "beta": 2})
        a.write("Extraction/K1.json", {"Study_ID": "K1", "alpha": 11, "beta": 2})
        b.write("Extraction/K1.json", {"Study_ID": "K1", "alpha": 1, "beta": 22})
        assert a.tick()                      # lands on the share
        assert b.tick()                      # sees alpha, merges beta -> "merged"
        assert b.status()["conflicts"] == 0
        assert b.read_json("Extraction/K1.json") == {"Study_ID": "K1", "alpha": 11, "beta": 22}
        assert a.tick()
        assert a.read_json("Extraction/K1.json") == {"Study_ID": "K1", "alpha": 11, "beta": 22}
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------------------
# 3. the same field twice -> conflict record + restore + resolution
# ---------------------------------------------------------------------------

def test_a_412_between_stat_and_put_is_merged_not_overwritten(backend):
    """The conditional PUT is the real guard: a race *after* our stat still merges."""
    a, b = _two(backend)
    try:
        rel = "Extraction/K1.json"
        _make_shared(a, b, rel, {"Study_ID": "K1", "alpha": 1, "beta": 2})
        b.write(rel, {"Study_ID": "K1", "alpha": 11, "beta": 2})
        injected = {"done": False}

        def hook(method, path, phase, response=None):
            # someone else writes the moment before b's conditional PUT lands
            if method == "PUT" and path == rel and phase == "before" and not injected["done"]:
                injected["done"] = True
                a.write(rel, {"Study_ID": "K1", "alpha": 1, "beta": 22})
                assert a.tick()

        b.client.dav.hook = hook
        assert b.tick()
        b.client.dav.hook = None
        assert injected["done"]
        assert b.status()["conflicts"] == 0
        # neither side lost its change
        assert b.read_json(rel) == {"Study_ID": "K1", "alpha": 11, "beta": 22}
        assert json.loads(b.client.dav.get(rel)) == {"Study_ID": "K1", "alpha": 11, "beta": 22}
        assert a.tick()
        assert a.read_json(rel) == {"Study_ID": "K1", "alpha": 11, "beta": 22}
    finally:
        a.close()
        b.close()
def test_same_field_becomes_a_conflict_and_can_be_resolved(backend):
    a, b = _two(backend)
    try:
        _make_shared(a, b, "Extraction/K1.json", {"Study_ID": "K1", "verdict": "include"})
        a.write("Extraction/K1.json", {"Study_ID": "K1", "verdict": "exclude"})
        b.write("Extraction/K1.json", {"Study_ID": "K1", "verdict": "maybe"})
        assert a.tick()
        assert b.tick()
        open_conflicts = b.store.conflicts("open")
        assert len(open_conflicts) == 1, open_conflicts
        rec = open_conflicts[0]
        assert rec["path"] == "Extraction/K1.json"
        assert rec["kind"] == "conflict"
        # the local bytes are kept, the server version is restored locally
        assert b.read_json("Extraction/K1.json") == {"Study_ID": "K1", "verdict": "exclude"}
        kept = b.syncer.conflicts.mine_bytes(rec["id"])
        assert json.loads(kept)["verdict"] == "maybe"
        # "mine" becomes the new shared version, based on the current head
        b.store.resolve_conflict(rec["id"], "mine")
        assert b.tick()
        assert a.tick()
        assert a.read_json("Extraction/K1.json") == {"Study_ID": "K1", "verdict": "maybe"}
        assert b.store.conflicts("open") == []
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------------------
# 4. lock race: exactly one winner (atomic conditional create)
# ---------------------------------------------------------------------------

def test_lock_race_has_exactly_one_winner(backend):
    key = "Smith (2020)"
    clients = []
    results: list[tuple[str, str]] = []
    lock = threading.Lock()
    start = threading.Barrier(6)

    def worker(name: str, client):
        try:
            start.wait(timeout=10)
            row = client.acquire_lock(pid, key, f"client-{name}")
            with lock:
                results.append((name, "won"))
            return row
        except Exception as exc:                # noqa: BLE001 - LockedError expected
            with lock:
                results.append((name, type(exc).__name__))

    try:
        from online.nextcloud import NextcloudClient
        for i in range(6):
            c = NextcloudClient(backend.url, f"u{i}", PASSWORD,
                                webdav_root_url=backend.url, allow_insecure_http=True)
            clients.append(c)
        pid = clients[0].project_id
        threads = [threading.Thread(target=worker, args=(f"u{i}", clients[i])) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        winners = [n for n, how in results if how == "won"]
        if backend.atomic_conditional_writes:
            assert len(winners) == 1, results
        else:                                   # WsgiDAV: check-then-write is not atomic
            print(f"[{backend.name}] concurrent lock create -> {len(winners)} winners: {results}")
            assert 1 <= len(winners) <= 6, results
        # whatever happened, exactly one lock file exists and everybody agrees who holds it
        holder = clients[0].locks(pid)
        assert len(holder) == 1, holder
        assert holder[0]["user"] == winners[0]
        # a foreign client is refused with the holder object
        other = next(c for c in clients if c.username != winners[0])
        with pytest.raises(Exception) as exc:
            other.acquire_lock(pid, key, "client-x")
        assert getattr(exc.value, "holder", None), exc.value
    finally:
        for c in clients:
            c.close()


# ---------------------------------------------------------------------------
# 5. an expired lock can be taken over
# ---------------------------------------------------------------------------

def test_expired_lock_can_be_taken_over(backend):
    from online.nextcloud import NextcloudClient, lock_path
    a = NextcloudClient(backend.url, "a", PASSWORD, webdav_root_url=backend.url,
                        allow_insecure_http=True)
    b = NextcloudClient(backend.url, "b", PASSWORD, webdav_root_url=backend.url,
                        allow_insecure_http=True)
    try:
        row = a.acquire_lock(a.project_id, "Smith (2020)", "client-a")
        assert row["user"] == "a"
        # age the lock: an expired lease is what a crashed offline client leaves
        stale = {"key": "Smith (2020)", "user": "a", "display_name": "A",
                 "client_id": "client-a", "acquired": 1.0, "renewed": 1.0,
                 "expires": 2.0, "expires_iso": "1970-01-01T00:00:02Z", "offline": True}
        a.dav.put(lock_path("Smith (2020)"), json.dumps(stale).encode(), content_type="application/json")
        taken = b.acquire_lock(b.project_id, "Smith (2020)", "client-b")
        assert taken["user"] == "b"
        assert taken.get("took_over") is True
        assert [l["user"] for l in b.locks(b.project_id)] == ["b"]
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------------------
# 6. offline edits land on reconnect
# ---------------------------------------------------------------------------

def test_offline_edits_land_on_reconnect(backend):
    a, b = _two(backend)
    try:
        a.offline(True)
        a.write("Codings/K1 - A.json", {"Study_ID": "K1", "made": "offline"})
        assert a.tick() is False
        assert a.status()["mode"] == "offline"
        assert a.status()["pending"] == 1
        assert b.tick()                        # b sees nothing: a never pushed
        assert b.read_json("Codings/K1 - A.json") is None
        # the app kept working offline; the queued version survives a restart
        a.restart()
        a.offline(False)
        assert a.tick() is True
        assert a.status()["pending"] == 0
        assert b.tick()
        assert b.read_json("Codings/K1 - A.json") == {"Study_ID": "K1", "made": "offline"}
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------------------
# 7. a kill in the middle of an upload loses nothing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phase", ["before", "after"])
def test_kill_during_upload_loses_nothing(backend, phase):
    a, b = _two(backend)
    try:
        rel = "Codings/K1 - A.json"
        a.write(rel, {"Study_ID": "K1", "v": 1})

        def hook(method, path, ph, response=None):
            if method == "PUT" and path == rel and ph == phase:
                raise Crash(f"killed {phase} the upload")

        a.client.dav.hook = hook
        with pytest.raises(Crash):
            a.tick()
        a.client.dav.hook = None
        # whatever was in flight, the change is still queued on disk
        assert a.status()["pending"] >= 1
        a.restart()
        assert a.tick() is True
        assert a.status()["pending"] == 0
        assert b.tick()
        assert b.read_json(rel) == {"Study_ID": "K1", "v": 1}
        assert json.loads(a.client.dav.get(rel)) == {"Study_ID": "K1", "v": 1}
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------------------
# freshness: one PROPFIND, only changed files are downloaded
# ---------------------------------------------------------------------------

def test_a_deleted_file_disappears_on_the_next_pull(backend):
    a, b = _two(backend)
    try:
        rel = "Codings/K1 - A.json"
        a.write(rel, {"Study_ID": "K1"})
        assert a.tick()
        assert b.tick()
        assert b.exists(rel)
        os.unlink(a.path(rel))                 # the owner deletes their own file
        assert a.tick()                        # DELETE on the share
        assert not os.path.exists(os.path.join(backend.root, *rel.split("/")))
        assert b.tick()                        # the tombstone reaches the mirror
        assert not b.exists(rel)
        # a plain Nextcloud desktop client deleting a file has the same effect
        with open(os.path.join(backend.root, "Codings", "K2 - A.json"), "w", encoding="utf-8") as fh:
            json.dump({"Study_ID": "K2"}, fh)
        assert b.tick() and b.exists("Codings/K2 - A.json")
        os.unlink(os.path.join(backend.root, "Codings", "K2 - A.json"))
        assert b.tick()
        assert not b.exists("Codings/K2 - A.json")
    finally:
        a.close()
        b.close()
def test_only_changed_files_are_downloaded(backend):
    a, b = _two(backend)
    try:
        _make_shared(a, b, "Extraction/K1.json", {"Study_ID": "K1", "v": 1})
        # an immutable file dropped into the share by a plain Nextcloud sync
        # client (a coder may not write Papers/ - only the owner may)
        papers = os.path.join(backend.root, "Papers")
        os.makedirs(papers, exist_ok=True)
        with open(os.path.join(papers, "K2.pdf"), "wb") as fh:
            fh.write(b"%PDF-1.4 k2\n")
        state = getattr(backend, "_state", None)
        if state is None:
            pytest.skip("this backend does not expose a request log")
        state.gets.clear()
        assert b.tick()                     # the manifest learns it without downloading
        assert "Papers/K2.pdf" not in state.gets, state.gets
        state.gets.clear()
        assert b.tick()                     # nothing changed: nothing downloaded
        assert state.gets == {}, state.gets
        a.write("Extraction/K1.json", {"Study_ID": "K1", "v": 2})
        assert a.tick()
        state.gets.clear()
        assert b.tick()
        assert set(state.gets) == {"Extraction/K1.json"}, state.gets
        assert b.read_json("Extraction/K1.json")["v"] == 2
        assert not os.path.exists(b.path("Papers/K2.pdf"))
        pdf = b.store.ensure_local("Papers/K2.pdf")          # on demand only
        assert open(pdf, "rb").read() == b"%PDF-1.4 k2\n"
    finally:
        a.close()
        b.close()


def test_depth_infinity_refused_falls_back_to_depth_1(tmp_path):
    """A share that refuses PROPFIND Depth: infinity is walked folder by folder."""
    from online.nextcloud import NextcloudClient

    share = tmp_path / "share"
    share.mkdir()
    (share / "Project").mkdir()
    (share / "Project" / "project.json").write_text("{}\n", encoding="utf-8")
    (share / "Codings").mkdir()
    (share / "Codings" / "K1 - A.json").write_text('{"a": 1}\n', encoding="utf-8")
    server = WebDAVFake(str(share), refuse_infinity=True)
    url = server.start()
    try:
        client = NextcloudClient(url, "a", PASSWORD, webdav_root_url=url, allow_insecure_http=True)
        rows = client.list_remote()
        assert set(rows) == {"Project/project.json", "Codings/K1 - A.json"}
        assert server.state.methods.get("PROPFIND", 0) >= 2      # root + per folder
    finally:
        client.close()
        server.stop()


# ---------------------------------------------------------------------------
# status / done / presence: the honour-system surface still answers
# ---------------------------------------------------------------------------

def test_status_reports_nextcloud_best_effort(backend):
    a, b = _two(backend)
    try:
        st = a.status()
        assert st["backend"] == "nextcloud"
        assert st["guarantees"] == "best-effort"
        assert st["mode"] == "online"
        assert a.store.me()["tags"] == ["A"]
        assert a.store.me()["role"] == "coder"
        assert a.store.me()["tasks"]
    finally:
        a.close()
        b.close()


def test_done_marks_and_presence_live_in_studio(backend):
    a, b = _two(backend)
    try:
        a.store.mark_done("K1", "coding", "A")
        assert {"key": "K1", "stage": "coding", "tag": "A"} in a.store.done_list(stage="coding")
        assert any(r["key"] == "K1" for r in b.store.done_list(tag="A"))
        others = b.store.presence(key="K1", tab="coding")
        assert others == [] or all("user" in row for row in others)
        a.store.presence(key="K1", tab="coding")
        seen = b.store.presence(key="K2", tab="fulltext")
        assert any(row.get("user") == "a" for row in seen), seen
        # only honour-system files: everything sits under .studio on the share
        root = os.path.join(backend.root, ".studio")
        assert os.path.isdir(os.path.join(root, "done"))
        assert os.path.isdir(os.path.join(root, "presence"))
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------------------
# the WebDAV transport itself: verbs, throttling, the parallel bound
# ---------------------------------------------------------------------------

def test_webdav_verbs(tmp_path):
    from online.webdav import WebDAV
    server = WebDAVFake(str(tmp_path / "share"))
    url = server.start()
    try:
        dav = WebDAV(url, "anna", "pw", allow_insecure_http=True)
        dav.makedirs("A/B/C")                     # nested MKCOL
        assert dav.exists("A/B/C")
        dav.put("A/B/C/data.bin", b"0123456789")
        assert dav.get("A/B/C/data.bin") == b"0123456789"
        chunk = dav.get("A/B/C/data.bin", byte_range=(2, 4))     # Range GET
        assert chunk == b"234"
        # a file object streams (no whole-file read in the client)
        src = tmp_path / "payload.bin"
        src.write_bytes(b"x" * (3 * 1024 * 1024))
        with open(src, "rb") as fh:
            resp = dav.put("A/B/C/streamed.bin", fh)
        assert resp.status in (201, 204)
        assert dav.stat("A/B/C/streamed.bin").size == 3 * 1024 * 1024
        dav.move("A/B/C/streamed.bin", "A/B/C/moved.bin")
        assert dav.exists("A/B/C/moved.bin") and not dav.exists("A/B/C/streamed.bin")
        etag = dav.stat("A/B/C/moved.bin").etag
        dav.delete("A/B/C/moved.bin", if_match=etag)
        assert not dav.exists("A/B/C/moved.bin")
    finally:
        server.stop()


def test_webdav_refuses_plain_http_to_a_remote_host(tmp_path):
    from online.webdav import WebDAV
    with pytest.raises(ValueError):
        WebDAV("http://cloud.example.org/remote.php/dav/files/a/x", "a", "b")
    # loopback is the documented exception (the local test server)
    WebDAV("http://127.0.0.1:1/x", "a", "b")


def test_rate_limit_is_retried(tmp_path):
    from online.webdav import WebDAV
    server = WebDAVFake(str(tmp_path / "share"), rate_limit_every=3)
    url = server.start()
    try:
        dav = WebDAV(url, "a", "pw", allow_insecure_http=True, retries=5,
                     max_retry_wait=0.01, sleep=lambda _s: None)
        dav.makedirs("dir")
        for i in range(6):
            dav.put(f"dir/f{i}.txt", b"x")
        assert dav.throttled, "the 429s were observed"
        assert len(dav.throttled) >= 2
        assert all(t["status"] == 429 for t in dav.throttled)
    finally:
        server.stop()


def test_parallel_requests_start_at_four_and_stay_bounded(tmp_path):
    from online.webdav import DEFAULT_PARALLEL, MAX_PARALLEL_REQUESTS, WebDAV
    server = WebDAVFake(str(tmp_path / "share"))
    url = server.start()
    try:
        dav = WebDAV(url, "a", "pw", allow_insecure_http=True)
        assert dav.parallel_limit == DEFAULT_PARALLEL
        dav.put("f.txt", b"x")
        active = {"now": 0, "max": 0}
        guard = threading.Lock()
        real = dav._send_once

        def counting(*args, **kw):
            with guard:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
            try:
                time.sleep(0.02)
                return real(*args, **kw)
            finally:
                with guard:
                    active["now"] -= 1

        dav._send_once = counting
        errors: list = []

        def call():
            try:
                dav.propfind("", depth="0")
            except Exception as exc:            # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=call) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not errors, errors
        assert 1 < active["max"] <= MAX_PARALLEL_REQUESTS, active
        assert dav.parallel_limit <= MAX_PARALLEL_REQUESTS
    finally:
        server.stop()


def test_a_throttled_share_gets_the_lower_parallelism_back(tmp_path):
    """429/503 closes the gate again: the owner's share must not see a burst."""
    from online.webdav import DEFAULT_PARALLEL, WebDAV
    server = WebDAVFake(str(tmp_path / "share"), rate_limit_every=5)
    url = server.start()
    try:
        dav = WebDAV(url, "a", "pw", allow_insecure_http=True, retries=4,
                     max_retry_wait=0.01, sleep=lambda _s: None)
        dav.makedirs("dir")
        for i in range(10):
            dav.put(f"dir/f{i}.txt", b"x")
        assert dav.throttled
        assert dav.parallel_limit == DEFAULT_PARALLEL
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# the Flask route the connect dialog calls
# ---------------------------------------------------------------------------

def test_flask_connect_route_opens_a_nextcloud_project(tmp_path):
    """``POST /api/online/connect {kind: "nextcloud"}`` -> open -> status."""
    import app as app_module
    import routes_online
    from studio_state import S

    share = tmp_path / "share"
    (share / "Project").mkdir(parents=True)
    (share / "Project" / "project.json").write_text('{"scheme": "x"}\n', encoding="utf-8")
    (share / ".studio").mkdir()
    (share / ".studio" / "members.json").write_text(json.dumps(MEMBERS), encoding="utf-8")
    server = WebDAVFake(str(share))
    url = server.start()
    original = S.ROOT
    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    try:
        r = client.post("/api/online/connect", json={
            "kind": "nextcloud", "url": url, "username": "a",
            "app_password": PASSWORD, "folder": "", "webdav_root": url})
        assert r.status_code == 200, r.get_json()
        data = r.get_json()
        assert data["backend"] == "nextcloud"
        assert data["guarantees"] == "best-effort"
        assert len(data["projects"]) == 1
        pid = data["projects"][0]["id"]
        r = client.post("/api/online/open", json={"project_id": pid})
        assert r.status_code == 200, r.get_json()
        st = client.get("/api/online/status").get_json()
        assert st["active"] is True
        assert st["backend"] == "nextcloud"
        assert st["guarantees"] == "best-effort"
        # a plain http server URL is refused before any credential is used
        r = client.post("/api/online/connect", json={
            "kind": "nextcloud", "url": "http://cloud.example.org", "username": "a",
            "app_password": "x", "folder": "Coding"})
        assert r.status_code == 400, r.get_json()
    finally:
        try:
            client.post("/api/online/close", json={})
        except Exception:                       # noqa: BLE001
            pass
        routes_online.set_store(None)
        S.online = False
        S.store = None
        server.stop()
        if original and os.path.isdir(original):
            S.configure(original)
