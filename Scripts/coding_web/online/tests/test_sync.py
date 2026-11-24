"""End-to-end sync scenarios against the in-memory fake server."""

from __future__ import annotations

import json
import os

import pytest

from online_shared import protocol

from online.errors import OfflineError
from online.tests.conftest import PID, Crash, jbytes


def sha(b: bytes) -> str:
    return protocol.sha256_bytes(b)


# --- 1. two clients, own coding files ------------------------------------------------

def test_two_clients_exchange_own_coding_files(server, make_node):
    server.projects[PID]["settings"]["blinding"]["coding"] = "visible"
    a, b = make_node("a"), make_node("b")
    assert a.tick() and b.tick()
    assert a.read("Text/K1.txt") == b"paper text K1\n"           # core pulled on open

    a.write("Codings/K1 - A.json", {"Records": [{"Study_ID": "K1", "N": 10}]})
    assert a.tick()
    assert server.file_bytes(PID, "Codings/K1 - A.json") == a.read("Codings/K1 - A.json")
    assert a.store.status()["pending"] == 0
    assert b.tick()
    assert b.read("Codings/K1 - A.json") == a.read("Codings/K1 - A.json")

    b.write("Codings/K1 - B.json", {"Records": [{"Study_ID": "K1", "N": 12}]})
    assert b.tick() and a.tick()
    assert a.read_json("Codings/K1 - B.json")["Records"][0]["N"] == 12

    # an edit of an existing file travels too, and a delete
    a.write("Codings/K1 - A.json", {"Records": [{"Study_ID": "K1", "N": 11}]})
    assert a.tick() and b.tick()
    assert b.read_json("Codings/K1 - A.json")["Records"][0]["N"] == 11
    os.unlink(a.path("Codings/K1 - A.json"))
    assert a.tick() and b.tick()
    assert server.file_bytes(PID, "Codings/K1 - A.json") is None
    assert not b.exists("Codings/K1 - A.json")
    assert a.store.status()["mode"] == "online"


# --- 2. offline edits are coalesced and pushed in order ------------------------------

def test_offline_edits_coalesced_and_pushed_in_order(server, make_node):
    a = make_node("a")
    assert a.tick()
    a.transport.offline = True
    seq0 = server.seq(PID)

    a.write("Codings/K1 - A.json", {"v": 1})
    assert a.tick() is False
    st = a.store.status()
    assert st["mode"] == "offline" and st["pending"] == 1
    a.write("Codings/K2 - A.json", {"v": 1})
    a.tick()
    assert a.store.status()["pending"] == 2
    a.write("Codings/K1 - A.json", {"v": 2})                 # second edit of the first file
    a.tick()
    assert a.store.status()["pending"] == 2
    a.write("Codings/K3 - A.json", {"v": 1})
    a.tick()
    assert a.store.status()["pending"] == 3
    intermediate = sha(jbytes({"v": 1}))
    a.write("Codings/K1 - A.json", {"v": 3})
    a.tick()
    assert a.store.status()["pending"] == 3
    entry = a.syncer.outbox.get("Codings/K1 - A.json")
    assert entry["base_hash"] is None and entry["new_hash"] == sha(jbytes({"v": 3}))

    a.transport.offline = False
    assert a.tick()
    assert a.store.status()["pending"] == 0
    changes = server.projects[PID]["changes"]
    mine = [c for c in changes if c["seq"] > seq0]
    assert [c["path"] for c in mine] == ["Codings/K1 - A.json", "Codings/K2 - A.json", "Codings/K3 - A.json"]
    assert mine[0]["new_hash"] == sha(jbytes({"v": 3}))
    assert all(c["new_hash"] != sha(jbytes({"v": 2})) for c in mine)
    assert server.file_bytes(PID, "Codings/K2 - A.json") == jbytes({"v": 1})
    assert sha(jbytes({"v": 2})) not in server.blobs          # superseded offline version never uploaded
    assert intermediate in server.blobs                       # (v1 bytes arrived only as K2's content)
    assert len(server.changes_for(PID, "Codings/K1 - A.json")) == 1
    # pending copies are cleaned up
    assert [n for n in os.listdir(a.mirror.pending_dir) if not n.startswith(".")] == []


def test_background_thread_syncs_and_survives_offline(server, make_node):
    import time as _time
    a = make_node("a")
    a.syncer.interval = 0.05
    a.syncer.start()
    try:
        a.write("Codings/K1 - A.json", {"v": 1})
        a.store.kick()
        deadline = _time.time() + 10
        while server.file_bytes(PID, "Codings/K1 - A.json") is None and _time.time() < deadline:
            _time.sleep(0.02)
        assert server.file_bytes(PID, "Codings/K1 - A.json") == jbytes({"v": 1})
        a.transport.offline = True
        a.store.kick()
        deadline = _time.time() + 10
        while a.store.status()["mode"] != "offline" and _time.time() < deadline:
            _time.sleep(0.02)
        assert a.store.status()["mode"] == "offline"
        a.transport.offline = False
        a.write("Codings/K1 - A.json", {"v": 2})
        a.store.kick()
        deadline = _time.time() + 10
        while server.file_bytes(PID, "Codings/K1 - A.json") != jbytes({"v": 2}) and _time.time() < deadline:
            _time.sleep(0.02)
        assert server.file_bytes(PID, "Codings/K1 - A.json") == jbytes({"v": 2})
    finally:
        a.syncer.stop()
    assert a.syncer._thread is None


def test_backoff_grows_while_offline(server, make_node):
    a = make_node("a")
    a.transport.offline = True
    a.tick()
    b1 = a.syncer.backoff
    a.tick()
    a.tick()
    assert b1 == 5.0 and a.syncer.backoff == 20.0
    for _ in range(10):
        a.tick()
    assert a.syncer.backoff == 300.0
    a.transport.offline = False
    assert a.tick() and a.syncer.backoff == 0


# --- 3. crash safety -------------------------------------------------------------------------

def _crash_on(node, method, suffix, phase):
    def hook(m, path, ph):
        if m == method and path.endswith(suffix) and ph == phase:
            raise Crash(f"{m} {path} {ph}")
    node.transport.hook = hook


def _assert_consistent(node, rel, data):
    assert node.read(rel) == data
    assert node.server.file_bytes(PID, rel) == data
    assert node.mirror.heads[rel]["hash"] == sha(data)
    assert len(node.syncer.outbox) == 0
    assert len(node.server.changes_for(PID, rel)) == 1, "no duplicate seq"


def test_crash_after_blob_upload_before_push(server, make_node):
    a = make_node("a")
    assert a.tick()
    a.write("Codings/K1 - A.json", {"v": 1})
    _crash_on(a, "POST", "/push", "before")
    with pytest.raises(Crash):
        a.tick()
    assert sha(jbytes({"v": 1})) in server.blobs           # uploaded, not pushed
    assert server.changes_for(PID, "Codings/K1 - A.json") == []
    a.restart()
    assert a.store.status()["pending"] == 1
    assert a.tick()
    _assert_consistent(a, "Codings/K1 - A.json", jbytes({"v": 1}))


def test_crash_after_push_before_state_save(server, make_node):
    a = make_node("a")
    assert a.tick()
    a.write("Codings/K1 - A.json", {"v": 1})
    _crash_on(a, "POST", "/push", "after")                  # server applied, client died
    with pytest.raises(Crash):
        a.tick()
    assert server.file_bytes(PID, "Codings/K1 - A.json") == jbytes({"v": 1})
    a.restart()
    pushes_before = sum(1 for r in server.requests if r[2].endswith("/push"))
    assert a.tick()
    _assert_consistent(a, "Codings/K1 - A.json", jbytes({"v": 1}))
    # recovered from the manifest, not by pushing again
    assert sum(1 for r in server.requests if r[2].endswith("/push")) == pushes_before


def test_crash_after_state_save_before_outbox_ack(server, make_node, monkeypatch):
    a = make_node("a")
    assert a.tick()
    a.write("Codings/K1 - A.json", {"v": 1})
    from online.outbox import Outbox
    real_ack = Outbox.ack

    def dying_ack(self, *args, **kw):
        raise Crash("ack")
    monkeypatch.setattr(Outbox, "ack", dying_ack)
    with pytest.raises(Crash):
        a.tick()
    monkeypatch.setattr(Outbox, "ack", real_ack)
    a.restart()
    assert a.store.status()["pending"] == 0                 # recovered at start-up
    assert a.tick()
    _assert_consistent(a, "Codings/K1 - A.json", jbytes({"v": 1}))


def test_crash_after_push_with_concurrent_edit_meanwhile(server, make_node):
    """Server applied v1; before restart the user saved v2: v2 must still arrive."""
    a = make_node("a")
    assert a.tick()
    a.write("Codings/K1 - A.json", {"v": 1})
    _crash_on(a, "POST", "/push", "after")
    with pytest.raises(Crash):
        a.tick()
    a.write("Codings/K1 - A.json", {"v": 2})
    a.restart()
    assert a.tick()
    assert server.file_bytes(PID, "Codings/K1 - A.json") == jbytes({"v": 2})
    assert [c["new_hash"] for c in server.changes_for(PID, "Codings/K1 - A.json")] == \
        [sha(jbytes({"v": 1})), sha(jbytes({"v": 2}))]
    assert len(a.syncer.outbox) == 0


def test_crash_during_outbox_compaction(server, make_node, monkeypatch):
    a = make_node("a")
    a.transport.offline = True
    for i in range(3):
        a.write(f"Codings/K{i} - A.json", {"v": i})
    a.tick()
    assert a.store.status()["pending"] == 3
    import online.outbox as ob

    def dying_replace(src, dst, attempts=20):
        raise Crash("compaction")
    monkeypatch.setattr(ob, "replace_retry", dying_replace)
    with pytest.raises(Crash):
        a.syncer.outbox.compact()
    assert os.path.exists(a.syncer.outbox.path + ".compact")
    monkeypatch.undo()
    # and a torn append on top of it
    with open(a.syncer.outbox.path, "ab") as fh:
        fh.write(b'{"op": "put", "path": "Codings/K9 - A.json", "base_ha')
    a.transport.offline = False
    a.restart()
    assert not os.path.exists(a.syncer.outbox.path + ".compact")
    assert a.store.status()["pending"] == 3
    assert a.tick()
    for i in range(3):
        _assert_consistent(a, f"Codings/K{i} - A.json", jbytes({"v": i}))


# --- 4. concurrent shared edit ---------------------------------------------------------------------

def _settle_setup(server):
    server.projects[PID]["done"][("A", "K", "coding")] = 1.0
    server.projects[PID]["done"][("B", "K", "coding")] = 1.0
    server.put_file(PID, "Extraction/K.json", jbytes({"Records": [{"Study_ID": "K", "x": 1, "y": 1}]}))


def test_concurrent_shared_edit_merges_and_conflicts(server, make_node):
    _settle_setup(server)
    a, b = make_node("a"), make_node("b")
    assert a.tick() and b.tick()
    rel = "Extraction/K.json"

    # disjoint fields -> merged
    a.write(rel, {"Records": [{"Study_ID": "K", "x": 2, "y": 1}]})
    b.write(rel, {"Records": [{"Study_ID": "K", "x": 1, "y": 2}]})
    assert a.tick() and b.tick()
    merged = {"Records": [{"Study_ID": "K", "x": 2, "y": 2}]}
    assert b.read_json(rel) == merged
    assert server.changes_for(PID, rel)[-1]["kind"] == "merge"
    assert a.tick()
    assert a.read(rel) == b.read(rel) == server.file_bytes(PID, rel)
    assert len(b.syncer.outbox) == 0

    # same field -> conflict; local bytes kept, server head restored
    a.write(rel, {"Records": [{"Study_ID": "K", "x": 3, "y": 2}]})
    b.write(rel, {"Records": [{"Study_ID": "K", "x": 4, "y": 2}]})
    mine = b.read(rel)
    assert a.tick() and b.tick()
    st = b.store.status()
    assert st["conflicts"] == 1 and st["pending"] == 0
    assert b.read_json(rel)["Records"][0]["x"] == 3                    # server version restored
    rec = b.store.conflicts()[0]
    assert rec["path"] == rel and rec["kind"] == "conflict" and rec["mine_hash"] == sha(mine)
    assert b.syncer.conflicts.mine_bytes(rec["id"]) == mine            # nothing lost
    assert server.file_bytes(PID, rel) == a.read(rel)

    b.store.resolve_conflict(rec["id"], "mine")
    assert server.file_bytes(PID, rel) == mine
    assert b.read(rel) == mine
    assert b.store.status()["conflicts"] == 0
    assert b.tick() and a.tick()
    assert a.read(rel) == mine
    assert len(b.syncer.outbox) == 0
    assert server.projects[PID]["conflicts"][0]["state"] == "resolved"


def test_resolve_theirs_and_merged(server, make_node):
    _settle_setup(server)
    a, b = make_node("a"), make_node("b")
    assert a.tick() and b.tick()
    rel = "Extraction/K.json"
    for x in (5, 6):
        a.write(rel, {"Records": [{"Study_ID": "K", "x": x, "y": 1}]})
        b.write(rel, {"Records": [{"Study_ID": "K", "x": x + 10, "y": 1}]})
        assert a.tick() and b.tick()
    recs = b.store.conflicts()
    assert len(recs) == 2
    b.store.resolve_conflict(recs[0]["id"], "theirs")
    merged = jbytes({"Records": [{"Study_ID": "K", "x": 99, "y": 1}]})
    b.store.resolve_conflict(recs[1]["id"], "merged", merged)
    assert server.file_bytes(PID, rel) == merged == b.read(rel)
    assert b.store.status()["conflicts"] == 0


# --- 5. unwritable local edits stay local ---------------------------------------------------------

def test_unwritable_local_changes_are_not_pushed(server, make_node):
    # every stage visible: the app recomputes these derived files, and a coder
    # may see them (a blinded stage hides master_dataset.xlsx from the mirror)
    for stage in protocol.STAGES:
        server.projects[PID]["settings"]["blinding"][stage] = "visible"
    server.put_file(PID, "Screening/fulltext_agreed.json", jbytes({"agreed": 1}))
    server.put_file(PID, "master_dataset.xlsx", b"PK\x03\x04 master v1")
    server.put_file(PID, "Codings/K - B.json", jbytes({"by": "B"}))
    a = make_node("a")
    assert a.tick()
    seq0 = server.seq(PID)
    for rel, data in (("Screening/fulltext_agreed.json", jbytes({"agreed": "local"})),
                      ("master_dataset.xlsx", b"PK\x03\x04 rewritten locally"),
                      ("Codings/K - B.json", jbytes({"by": "A!"}))):
        a.write(rel, data)
    assert a.tick()
    st = a.store.status()
    assert st["pending"] == 0 and st["local_only"] == 3
    assert server.seq(PID) == seq0
    assert not any(r[2].endswith("/push") for r in server.requests if r[0] == "a")
    # the local edit survives until the server version changes ...
    assert a.read("master_dataset.xlsx") == b"PK\x03\x04 rewritten locally"
    assert a.tick() and a.mirror.hash_calls >= 0
    # ... and is overwritten by the next server version
    server.put_file(PID, "Screening/fulltext_agreed.json", jbytes({"agreed": 2}))
    server.put_file(PID, "master_dataset.xlsx", b"PK\x03\x04 master v2")
    server.put_file(PID, "Codings/K - B.json", jbytes({"by": "B2"}))
    assert a.tick()
    assert a.read_json("Screening/fulltext_agreed.json") == {"agreed": 2}
    assert a.read("master_dataset.xlsx") == b"PK\x03\x04 master v2"
    assert a.read_json("Codings/K - B.json") == {"by": "B2"}
    assert a.store.status()["local_only"] == 0


def test_owner_pushes_derived_files(server, make_node):
    own = make_node("own")
    assert own.tick()
    own.write("master_dataset.xlsx", b"PK owner build")
    assert own.tick()
    assert server.file_bytes(PID, "master_dataset.xlsx") == b"PK owner build"


# --- 6. on-demand files and the LRU cache --------------------------------------------------------

def test_on_demand_download_and_offline(server, make_node):
    server.put_file(PID, "Papers/K1.pdf", b"%PDF-1.7 K1")
    server.put_file(PID, "Papers/K2.pdf", b"%PDF-1.7 K2")
    a = make_node("a")
    assert a.tick()
    assert not a.exists("Papers/K1.pdf")                         # non-core: metadata only
    names = [e["name"] for e in a.store.listdir("Papers")]
    assert names == ["K1.pdf", "K2.pdf"]
    assert {"K1", "K2"} <= a.store.list_keys()
    p = a.store.ensure_local("Papers/K1.pdf")
    assert open(p, "rb").read() == b"%PDF-1.7 K1"
    a.transport.offline = True
    assert a.store.ensure_local("Papers/K1.pdf") == p               # local copy works offline
    with pytest.raises(OfflineError):
        a.store.ensure_local("Papers/K2.pdf")
    with pytest.raises(FileNotFoundError):
        a.store.ensure_local("Papers/nope.pdf")


def test_lru_eviction_respects_pins_core_and_is_not_a_delete(server, make_node):
    for k in ("P1", "P2", "P3", "PIN"):
        server.put_file(PID, f"Papers/{k}.pdf", b"%PDF" + k.encode() * 300)
    own = make_node("own")                                        # owner: could push deletes
    assert own.tick()
    own.store.pin(["PIN"])
    assert own.tick()
    assert own.exists("Papers/PIN.pdf")                           # pinned: downloaded by the pull
    size = len(b"%PDF" + b"P1" * 300)
    own.store.set_cache_settings(limit_mb=(2.5 * size) / (1024 * 1024))
    for k in ("P1", "P2", "P3"):
        own.store.ensure_local(f"Papers/{k}.pdf")
    assert not own.exists("Papers/P1.pdf")                        # oldest evicted
    assert own.exists("Papers/P2.pdf") and own.exists("Papers/P3.pdf")
    assert own.exists("Papers/PIN.pdf") and own.exists("Project/project.json")
    seq0 = server.seq(PID)
    assert own.tick()
    assert server.seq(PID) == seq0                                # eviction is not a deletion
    assert server.file_bytes(PID, "Papers/P1.pdf") is not None
    assert "P1" in own.store.list_keys()
    own.store.ensure_local("Papers/P1.pdf")                       # comes back on demand
    assert own.exists("Papers/P1.pdf")
    usage = own.store.cache_settings()["usage"]
    assert usage["lru"] <= 2.5 * size and usage["pinned"] == len(b"%PDF" + b"PIN" * 300)
    own.store.clear_cache()
    assert own.exists("Papers/PIN.pdf") and not own.exists("Papers/P3.pdf")


# --- 7. visibility epoch ---------------------------------------------------------------------------

def test_done_mark_changes_epoch_and_reveals_hidden_file(server, make_node):
    server.put_file(PID, "Codings/K1 - B.json", jbytes({"by": "B"}))
    a = make_node("a")
    assert a.tick()
    assert not a.exists("Codings/K1 - B.json")
    assert "Codings/K1 - B.json" not in a.mirror.remote
    epoch = a.mirror.state["visibility_epoch"]
    a.store.mark_done("K1", "coding")
    assert a.tick()
    assert a.mirror.state["visibility_epoch"] != epoch
    assert a.read_json("Codings/K1 - B.json") == {"by": "B"}


# --- 7b. "Finish paper" while offline ---------------------------------------------------------------

def test_done_mark_offline_is_durable_and_sent_before_the_pull(server, make_node):
    server.put_file(PID, "Codings/K1 - B.json", jbytes({"by": "B"}))
    a = make_node("a")
    assert a.tick()
    assert not a.exists("Codings/K1 - B.json")

    a.transport.offline = True
    res = a.store.mark_done("K1", "coding")
    assert res.get("ok") is True, res
    st = a.store.status()
    assert st["mode"] in ("online", "offline") and st["done_pending"] == 1 and st["pending"] == 1
    # the mark is on disk (state.json), not only in memory
    state = json.load(open(os.path.join(a.root, ".studio", "state.json"), encoding="utf-8"))
    assert state["done_pending"] == [{"key": "K1", "stage": "coding", "tag": "A"}]
    assert ("A", "K1", "coding") not in server.projects[PID]["done"]

    # done_list answers from the queue while the server is away
    rows = a.store.done_list(stage="coding", tag="A")
    assert any(r["key"] == "K1" and r.get("pending") for r in rows)

    # a round while still offline reports offline and keeps the mark
    assert a.tick() is False
    assert a.store.status()["mode"] == "offline" and a.store.status()["done_pending"] == 1

    a.transport.offline = False
    epoch = a.mirror.state["visibility_epoch"]
    # ONE round: the mark goes out first, so the epoch change (and B's hidden
    # file) is part of the same round's pull
    assert a.tick()
    assert ("A", "K1", "coding") in server.projects[PID]["done"]
    assert a.mirror.state["visibility_epoch"] != epoch
    assert a.read_json("Codings/K1 - B.json") == {"by": "B"}
    st = a.store.status()
    assert st["done_pending"] == 0 and st["pending"] == 0
    assert a.store.done_list(stage="coding", tag="A")[0].get("pending") is None

    # idempotent: a re-send (crash between call and state write) changes nothing
    a.syncer.queue_done("K1", "coding", "A")
    assert a.tick()
    assert list(server.projects[PID]["done"]) == [("A", "K1", "coding")]
    assert a.store.status()["done_pending"] == 0


def test_queued_done_mark_survives_a_restart(server, make_node):
    a = make_node("a")
    assert a.tick()
    a.transport.offline = True
    a.store.mark_done("K1", "coding")
    assert a.store.status()["done_pending"] == 1

    a.transport.offline = False
    a.restart()                                   # crash + reopen from disk
    assert a.store.status()["done_pending"] == 1
    assert a.tick()
    assert ("A", "K1", "coding") in server.projects[PID]["done"]
    assert a.store.status()["done_pending"] == 0 and a.store.status()["pending"] == 0


def test_done_mark_the_server_refuses_is_dropped_not_retried(server, make_node):
    a = make_node("a")
    assert a.tick()
    # a stale queue entry (e.g. the tag was removed while the client was away)
    a.syncer.queue_done("K1", "coding", "ZZZ")
    assert a.tick()
    status = a.store.status()
    assert status["done_pending"] == 0 and status["mode"] == "online"
    assert "done mark dropped" in (status["last_error"] or "")
    assert not any(tag == "ZZZ" for (tag, _key, _stage) in server.projects[PID]["done"])


# --- 8. merged result while the user edited again --------------------------------------------------

def test_merged_while_user_edited_again_requeues_on_merged_hash(server, make_node):
    _settle_setup(server)
    a, b = make_node("a"), make_node("b")
    assert a.tick() and b.tick()
    rel = "Extraction/K.json"
    a.write(rel, {"Records": [{"Study_ID": "K", "x": 2, "y": 1}]})
    assert a.tick()
    b.write(rel, {"Records": [{"Study_ID": "K", "x": 1, "y": 2}]})

    def edit_meanwhile(method, path, phase):
        if path.endswith("/push") and phase == "after":
            b.transport.hook = None
            b.write(rel, {"Records": [{"Study_ID": "K", "x": 1, "y": 2, "z": 5}]})
    b.transport.hook = edit_meanwhile
    assert b.tick()
    merged_hash = sha(jbytes({"Records": [{"Study_ID": "K", "x": 2, "y": 2}]}))
    assert server.projects[PID]["files"][rel]["hash"] == merged_hash
    e = b.syncer.outbox.get(rel)
    assert e is not None and e["base_hash"] == merged_hash
    assert b.read_json(rel) == {"Records": [{"Study_ID": "K", "x": 2, "y": 2, "z": 5}]}
    assert b.tick()
    assert json.loads(server.file_bytes(PID, rel)) == {"Records": [{"Study_ID": "K", "x": 2, "y": 2, "z": 5}]}
    assert len(b.syncer.outbox) == 0 and b.store.status()["conflicts"] == 0
    assert a.tick()
    assert a.read(rel) == server.file_bytes(PID, rel)


def test_rejected_locked_push_keeps_bytes_and_restores(server, make_node):
    _settle_setup(server)
    a, b = make_node("a"), make_node("b")
    assert a.tick() and b.tick()
    a.store.acquire_lock("K")
    rel = "Extraction/K.json"
    b.write(rel, {"Records": [{"Study_ID": "K", "x": 7, "y": 1}]})
    mine = b.read(rel)
    assert b.tick()
    rec = b.store.conflicts()[0]
    assert rec["kind"] == "rejected" and rec["reason"] == "locked"
    assert b.read(rel) == server.file_bytes(PID, rel)
    a.store.release_lock("K")
    b.store.resolve_conflict(rec["id"], "mine")                    # re-queued as a push
    assert b.tick()
    assert server.file_bytes(PID, rel) == mine


# --- 9. blinding hides a derived consensus file and the mirror lets it go ------------

def test_blinding_removes_a_newly_hidden_derived_file(server, make_node):
    """A file readable now but hidden by the next blinding change leaves the
    mirror, exactly like a server deletion - and comes back when it is visible."""
    for stage in protocol.STAGES:
        server.projects[PID]["settings"]["blinding"][stage] = "visible"
    server.put_file(PID, "master_dataset.xlsx", b"PK\x03\x04 master v1")
    server.put_file(PID, "Results/sb_coding_agreement.csv", b"key,kappa\nK1,0.8\n")
    server.put_file(PID, "Results/metaanalysis.xlsx", b"PK\x03\x04 meta")
    a = make_node("a")
    assert a.tick()
    # master_dataset.xlsx is core; Results/ is pulled only when opened
    assert a.exists("master_dataset.xlsx")
    for rel in ("Results/sb_coding_agreement.csv", "Results/metaanalysis.xlsx"):
        a.store.ensure_local(rel)
        assert a.exists(rel), rel

    # the owner blinds the screening and coding stages again
    server.projects[PID]["settings"]["blinding"]["abstract"] = "blinded"
    server.projects[PID]["settings"]["blinding"]["fulltext"] = "blinded"
    server.projects[PID]["settings"]["blinding"]["coding"] = "blinded"
    assert a.tick()

    assert not a.exists("master_dataset.xlsx")
    assert not a.exists("Results/sb_coding_agreement.csv")
    # a plain analysis product is not a verdict: it stays
    assert a.exists("Results/metaanalysis.xlsx")
    # the head is dropped too, so the removal is not queued as a deletion
    assert "master_dataset.xlsx" not in a.mirror.heads
    assert "master_dataset.xlsx" not in a.mirror.remote
    assert a.store.status()["pending"] == 0
    assert server.file_bytes(PID, "master_dataset.xlsx") is not None

    # visible again: the next epoch change brings it back
    for stage in protocol.STAGES:
        server.projects[PID]["settings"]["blinding"][stage] = "visible"
    assert a.tick()
    assert a.read("master_dataset.xlsx") == b"PK\x03\x04 master v1"
    assert a.store.status()["pending"] == 0


def test_a_local_edit_of_a_newly_hidden_file_is_discarded(server, make_node):
    """The app rewrites master_dataset.xlsx locally during a screening merge; a
    coder's copy is local-only anyway, and blinding then takes it away."""
    for stage in protocol.STAGES:
        server.projects[PID]["settings"]["blinding"][stage] = "visible"
    server.put_file(PID, "master_dataset.xlsx", b"PK\x03\x04 master v1")
    a = make_node("a")
    assert a.tick()
    a.write("master_dataset.xlsx", b"PK\x03\x04 rewritten locally")
    assert a.tick()
    assert a.store.status()["local_only"] == 1
    assert a.store.status()["pending"] == 0

    for stage in protocol.STAGES:
        server.projects[PID]["settings"]["blinding"][stage] = "blinded"
    assert a.tick()
    assert not a.exists("master_dataset.xlsx")
    assert a.store.status()["local_only"] == 0
    assert server.file_bytes(PID, "master_dataset.xlsx") == b"PK\x03\x04 master v1"


# --- 10. Datasets products are on demand --------------------------------------------

def test_dataset_products_are_on_demand_and_the_sidecars_are_core(server, make_node):
    key = "K1"
    server.put_file(PID, f"Datasets/{key}/{key} - variables.csv", b"uniform_variable\nuv1\n")
    server.put_file(PID, f"Datasets/{key}/{key} - harmonized_variables.csv", b"uniform_variable\nuv1\n")
    server.put_file(PID, f"Datasets/{key}/{key} - harmonized.csv", b"pid,uv1\n1,0.5\n2,0.7\n")
    server.put_file(PID, f"Datasets/{key}/{key} - prepared.csv", b"pid,raw\n1,2\n")
    server.put_file(PID, f"Datasets/{key}/harmonizer.py", b"# no-op\n")
    a = make_node("a")
    assert a.tick()

    # the small metadata sidecars and the code are core ...
    for rel in (f"Datasets/{key}/{key} - variables.csv",
                f"Datasets/{key}/{key} - harmonized_variables.csv",
                f"Datasets/{key}/harmonizer.py"):
        assert a.exists(rel), rel
    # ... the data products are only listed until opened
    data = (f"Datasets/{key}/{key} - harmonized.csv",
            f"Datasets/{key}/{key} - prepared.csv")
    for rel in data:
        assert not a.exists(rel), rel
        assert rel in a.mirror.remote
    assert {e["name"] for e in a.store.listdir(f"Datasets/{key}")} >= {
        f"{key} - harmonized.csv", f"{key} - prepared.csv", "harmonizer.py"}

    path = a.store.ensure_local(data[0])
    assert open(path, "rb").read() == b"pid,uv1\n1,0.5\n2,0.7\n"
    assert a.exists(data[0])
    # an offline mirror keeps the fetched copy usable
    a.transport.offline = True
    assert a.store.ensure_local(data[0]) == path


def test_analysis_prefetch_downloads_the_dataset_products(server, make_node):
    key = "K1"
    server.put_file(PID, f"Datasets/{key}/{key} - harmonized.csv", b"pid,dv\n1,0.5\n")
    server.put_file(PID, f"Datasets/{key}/{key} - prepared.csv", b"pid,raw\n1,2\n")
    a = make_node("a")
    assert a.tick()
    usage = a.store.analysis_download()
    assert usage["files"] == 2 and usage["pending"] == 2 and usage["bytes"] > 0

    queued = a.store.prefetch_analysis()
    assert sorted(queued) == sorted([f"Datasets/{key}/{key} - harmonized.csv",
                                     f"Datasets/{key}/{key} - prepared.csv"])
    assert a.tick()
    assert a.exists(f"Datasets/{key}/{key} - harmonized.csv")
    assert a.exists(f"Datasets/{key}/{key} - prepared.csv")
    usage = a.store.analysis_download()
    assert usage["pending"] == 0 and usage["local_bytes"] == usage["bytes"]
