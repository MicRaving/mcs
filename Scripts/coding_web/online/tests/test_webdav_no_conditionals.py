"""A share that ignores If-Match / If-None-Match (PowerFolder, i.e. LRZ Sync+Share).

Measured on syncandshare.lrz.de (2026-11-26): 8 concurrent ``If-None-Match: *``
creates all answered 201, and a PUT with a stale ``If-Match`` was accepted.
The client detects that once per share and then checks the condition itself
right before each write; a lock write is re-read after a settle delay so a
race still has exactly one winner.
"""

from __future__ import annotations

import json
import threading

import pytest

from online.nextcloud import NextcloudClient, lock_path
from online.tests.webdav_fake import WebDAVFake
from online.webdav import PreconditionFailed

PASSWORD = "app-password"
MEMBERS = {"members": [
    {"username": f"u{i}", "role": "coder", "tags": [f"T{i}"], "tasks": ["coding", "settle"]}
    for i in range(6)
] + [{"username": "own", "role": "owner", "tags": ["O"], "tasks": []}]}


@pytest.fixture
def share(tmp_path):
    root = tmp_path / "share"
    (root / ".studio").mkdir(parents=True)
    (root / ".studio" / "members.json").write_text(json.dumps(MEMBERS), encoding="utf-8")
    fake = WebDAVFake(str(root))
    fake.state.ignore_conditionals = True
    url = fake.start()
    try:
        yield url
    finally:
        fake.stop()


def _client(url, user):
    c = NextcloudClient(url, user, PASSWORD, webdav_root_url=url, allow_insecure_http=True)
    c.lock_settle_s = 0.3
    return c


def test_the_probe_detects_ignored_conditionals_and_leaves_nothing(share):
    c = _client(share, "u0")
    try:
        c._ensure_conditionals()
        assert c._index["conditional_writes"] is False
        assert c.dav.emulate_conditionals is True
        rows = c.dav.propfind(".studio/presence", depth="1")
        assert not [p for p in rows if "condprobe" in p.path]
    finally:
        c.close()


def test_emulated_conditionals_refuse_stale_and_existing(share):
    c = _client(share, "u0")
    try:
        c.dav.emulate_conditionals = True
        c.dav.put("x.json", b"1")
        old = c.dav.stat("x.json").etag
        with pytest.raises(PreconditionFailed):
            c.dav.put("x.json", b"2", if_none_match=True)
        c.dav.put("x.json", b"22")                       # somebody else writes
        with pytest.raises(PreconditionFailed):
            c.dav.put("x.json", b"3", if_match=old)
        with pytest.raises(PreconditionFailed):
            c.dav.delete("x.json", if_match=old)
        assert c.dav.get("x.json") == b"22"
        c.dav.put("x.json", b"4", if_match=c.dav.stat("x.json").etag)
        assert c.dav.get("x.json") == b"4"
    finally:
        c.close()


def test_no_folder_etags_reuse_the_listing_and_keep_own_writes(share):
    """PowerFolder collections carry no ETag: full listings are spaced out,
    and our own writes in between are not mistaken for remote changes."""
    c = _client(share, "u0")
    try:
        pid = c.project_id
        c.dav.makedirs("Extraction")
        c.dav.put("Extraction/A.json", b'{"a": 1}')
        c.manifest(pid)                                   # full listing (loads the index)
        c._index["etag_propagates"] = False               # what PowerFolder answers
        full = c.stats["propfind_infinity"]
        c._last_full_s = 5.0                              # as if the share were big
        # our own new file between full listings
        resp = c.dav.put("Extraction/B.json", b'{"b": 1}')
        c._record_write("Extraction/B.json", "b" * 64, resp.etag or c._etag_of("Extraction/B.json"), 8)
        m = c.manifest(pid, since=0)
        assert c.stats["propfind_infinity"] == full       # reused, no new full listing
        rows = {f["path"]: f for f in m["files"]}
        assert rows["Extraction/B.json"]["deleted"] is False
        assert rows["Extraction/B.json"]["hash"] == "b" * 64
        # our own deletion is not resurrected either
        c.dav.delete("Extraction/A.json")
        c._drop_index("Extraction/A.json")
        m = c.manifest(pid, since=0)
        assert "Extraction/A.json" not in {f["path"] for f in m["files"] if not f["deleted"]}
        # once the gap is over, the next round lists fully again
        c._last_full_s = 0.0
        c.manifest(pid)
        assert c.stats["propfind_infinity"] == full + 1
    finally:
        c.close()


def test_lock_race_has_one_winner_without_server_conditionals(share):
    clients = [_client(share, f"u{i}") for i in range(6)]
    for c in clients:
        c._ensure_conditionals()
    pid = clients[0].project_id
    results: list = []
    guard = threading.Lock()
    barrier = threading.Barrier(len(clients))

    def worker(c):
        barrier.wait(timeout=10)
        try:
            c.acquire_lock(pid, "Smith (2020)", f"client-{c.username}")
            how = "won"
        except Exception as exc:                          # noqa: BLE001 - LockedError expected
            how = type(exc).__name__
        with guard:
            results.append((c.username, how))

    try:
        threads = [threading.Thread(target=worker, args=(c,)) for c in clients]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        winners = [u for u, how in results if how == "won"]
        assert len(winners) == 1, results
        held = json.loads(clients[0].dav.get(lock_path("Smith (2020)")))
        assert held["user"] == winners[0]
    finally:
        for c in clients:
            c.close()
