"""Unit tests: outbox coalescing/replay, scan performance, locks, permissions, tokens."""

from __future__ import annotations

import os
import time

import pytest

from online_shared import protocol

from online.client import ServerClient
from online.errors import AuthError, LockedError, ProtocolMismatch
from online.mirror import Mirror, Permissions
from online.outbox import Outbox
from online.syncer import Syncer
from online.tests.conftest import PID, SERVER_URL
from online.tests.fake_server import FakeTransport
from online import keyring_store


# --- outbox -------------------------------------------------------------------------------

def test_outbox_coalesces_and_replays(tmp_path):
    p = str(tmp_path / "outbox.jsonl")
    ob = Outbox(p)
    ob.add("a", "h0", "h1")
    ob.add("b", None, "x1")
    ob.add("a", "h1", "h2")                      # earliest base kept, latest new wins
    assert [(e["path"], e["base_hash"], e["new_hash"]) for e in ob.entries()] == \
        [("a", "h0", "h2"), ("b", None, "x1")]
    ob.add("b", "x1", None)                      # created then deleted: cancelled
    assert len(ob) == 1
    ob.ack("a", "h1", "h1")                      # an older version acked: rebase only
    assert ob.get("a")["base_hash"] == "h1"
    ob2 = Outbox(p)                              # replay gives the same view
    assert ob2.entries() == ob.entries()
    ob2.add("a", "h1", "h1")                     # reverted to the server version
    assert len(ob2) == 0


def test_outbox_compaction_keeps_order(tmp_path):
    p = str(tmp_path / "outbox.jsonl")
    ob = Outbox(p)
    for i in range(300):
        ob.add(f"f{i % 3}", None, f"h{i}")
    ob.compact()
    assert sum(1 for _ in open(p, "rb")) == 3
    assert [e["path"] for e in Outbox(p).entries()] == ["f0", "f1", "f2"]
    assert Outbox(p).get("f2")["new_hash"] == "h299"


# --- 9. scan performance ---------------------------------------------------------------------

class _NoNet:
    def __getattr__(self, name):
        raise AssertionError("no network expected")


def test_scan_5000_files_second_scan_hashes_nothing(tmp_path):
    """5,000 files as a pull leaves them (heads recorded) plus 20 new local
    files: the first scan hashes only the 20, the second scan nothing.

    (Hashing all 5,000 brand-new files works too, but costs ~30 s on a
    Windows machine with real-time antivirus scanning every first open.)
    """
    root = tmp_path / "m"
    d = root / "Codings"
    d.mkdir(parents=True)
    m = Mirror(str(root), fsync=False)
    m.state["project_id"] = PID
    for i in range(5000):
        data = b'{"k": %d}\n' % i
        rel = f"Codings/K{i:05d} - Z.json"
        p = d / f"K{i:05d} - Z.json"
        p.write_bytes(data)
        if i >= 20:
            st = os.stat(p)
            m.heads[rel] = {"hash": protocol.sha256_bytes(data), "size": st.st_size, "mtime_ns": st.st_mtime_ns}
    (root / "temp").mkdir()
    (root / "temp" / "junk.bin").write_bytes(b"x")                 # excluded
    m.save()
    s = Syncer(m, _NoNet(), Permissions("coder", ("Z",), ("coding",)), debounce=0)
    r1 = s.scan()
    assert r1.files_seen == 5000 and r1.hashed == 20 and len(s.outbox) == 20
    calls = m.hash_calls
    t0 = time.perf_counter()
    r2 = s.scan()
    second = time.perf_counter() - t0
    assert r2.files_seen == 5000 and r2.hashed == 0 and m.hash_calls == calls and r2.changes == []
    assert second < 5.0                        # stat-only (~0.03 s here)
    # a restart (fresh objects from disk) still hashes nothing
    m2 = Mirror(str(root), fsync=False)
    s2 = Syncer(m2, _NoNet(), Permissions("coder", ("Z",), ("coding",)), debounce=0)
    assert s2.scan().hashed == 0
    # one touched file -> one hash
    (root / "Codings" / "K00000 - Z.json").write_bytes(b'{"k": "changed"}\n')
    r3 = s2.scan()
    assert r3.hashed == 1 and len(s2.outbox) == 20
    (root / "Codings" / "K04999 - Z.json").write_bytes(b'{"k": "changed!"}\n')
    r4 = s2.scan()
    assert r4.hashed == 1 and len(s2.outbox) == 21


# --- 10. locks ---------------------------------------------------------------------------------

def test_lock_conflict_maps_to_locked_error(server, make_node):
    a, b = make_node("a"), make_node("b")
    lk = a.store.acquire_lock("K1")
    assert lk["user"] == "a"
    assert a.store.acquire_lock("K1")["user"] == "a"                # renew
    with pytest.raises(LockedError) as ei:
        b.store.acquire_lock("K1")
    assert ei.value.status == 409 and ei.value.holder["user"] == "a"
    assert ei.value.holder["display_name"] == "A"
    assert [l["key"] for l in b.store.locks()] == ["K1"]
    a.store.release_lock("K1")
    assert b.store.acquire_lock("K1")["user"] == "b"
    # an expired lock can be taken over
    server.clock_offset = 25 * 3600
    assert a.store.acquire_lock("K1")["user"] == "a"


def test_offline_lease_pins_key(server, make_node):
    a = make_node("a")
    lk = a.store.acquire_lock("K1", offline_hours=500)
    assert lk["offline"] and lk["expires"] - lk["acquired"] == 72 * 3600
    assert "K1" in a.store.cache_settings()["pinned"]


# --- client error mapping / protocol --------------------------------------------------------

def test_client_errors_and_headers(server):
    t = FakeTransport(server)
    c = ServerClient(SERVER_URL, transport=t)
    with pytest.raises(AuthError):
        c.login("a", "wrong")
    c.login("a", "pw")
    assert c.token and c.token not in repr(c)
    assert c.project(PID)["me"]["tags"] == ["A"]
    c2 = ServerClient(SERVER_URL, token=c.token, transport=t)
    c2._headers = lambda extra=None: {"Authorization": f"Bearer {c.token}", "X-Studio-Protocol": "9"}
    with pytest.raises(ProtocolMismatch):
        c2.project(PID)


def test_permissions_mirror_can_write():
    coder = Permissions("coder", ("A",), ("coding", "harmonize"))
    assert coder.can_write("Codings/K - A.json")
    assert not coder.can_write("Codings/K - B.json")
    assert coder.can_write("Screening/fulltext - A.json")
    assert not coder.can_write("Screening/fulltext_agreed.json")
    assert not coder.can_write("master_dataset.xlsx")
    assert not coder.can_write("Extraction/K.json")                 # no settle task
    assert coder.can_write("Datasets/K/K - variable_assignments.csv")
    owner = Permissions("owner", (), ())
    assert owner.can_write("master_dataset.xlsx") and not owner.can_write("temp/x.json")
    assert not Permissions("viewer", ("A",), ("coding",)).can_write("Codings/K - A.json")


def test_token_file_fallback(tmp_path):
    d = str(tmp_path / ".studio")
    assert keyring_store.save_token(SERVER_URL, "a", "secret", d, use_keyring=False) == "file"
    assert keyring_store.load_token(SERVER_URL, "a", d, use_keyring=False) == "secret"
    assert keyring_store.load_token(SERVER_URL, "b", d, use_keyring=False) is None
    keyring_store.delete_token(SERVER_URL, "a", d, use_keyring=False)
    assert not os.path.exists(os.path.join(d, "token"))


def test_state_json_and_status_shape(server, make_node):
    a = make_node("a")
    assert a.tick()
    st = a.store.status()
    for k in ("mode", "pending", "conflicts", "last_sync", "server", "user"):
        assert k in st
    assert st["user"] == "a" and st["server"] == SERVER_URL and st["mode"] == "online"
    import json
    state = json.load(open(os.path.join(a.root, ".studio", "state.json"), encoding="utf-8"))
    for k in ("server", "project_id", "project_name", "username", "client_id", "seq",
              "visibility_epoch", "heads", "pinned", "cache"):
        assert k in state
    assert state["heads"]["Text/K1.txt"]["hash"]
