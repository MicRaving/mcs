"""First sync through Nextcloud folder archives (the WebDAV ``GET`` on a collection).

The owner's real share (2026-11-25) needed 20-25 minutes for 6.9k core files /
359 MB: ~5-8 files/s, i.e. request latency, not bandwidth.  A Nextcloud >= 31
answers a collection ``GET`` with a tar/zip archive (``Accept`` header, optional
``files`` filter); one request then carries a whole batch.

What these tests pin:

* a bulk first sync produces **the same mirror** as the per-file path (bytes,
  heads, size, index hashes) - the archive is an optimisation, not a second
  code path for identity;
* only the paths the manifest asked for are written: on-demand PDFs, AI-route
  codings and raw data travelling in a stream (or named by a malicious member)
  are skipped, and nothing is ever written outside the mirror;
* a share without the extension (older Nextcloud, WsgiDAV, plain WebDAV) falls
  back to per-file GETs, and the negative capability is cached in the mirror;
* an interrupted archive resumes: what arrived is recorded exactly like a GET,
  the rest is re-fetched by the normal path, and a reopen downloads nothing
  that is already on disk.
"""

from __future__ import annotations

import hashlib
import os

import pytest

from online import ncarchive
from online.nextcloud import TOKEN_PREFIX
from online.store import OnlineStore
from online.tests import nc_benchmark as nb
from online.tests.webdav_fake import WebDAVFake
from online_shared import protocol


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def make_share(tmp_path):
    def _make(keys: int = 8, **fake_kw):
        root = tmp_path / "share"
        info = nb.build_share(str(root), keys=keys, size_scale=0.005, extra_scale=0.01)
        fake = WebDAVFake(str(root), fast_etags=True, **fake_kw)
        url = fake.start()
        return {"root": str(root), "info": info, "fake": fake, "url": url, "tmp": tmp_path}
    return _make


def _store(share, base, *, bulk: bool = True):
    st = OnlineStore(base_dir=str(base), use_keyring=False, autostart=False,
                     debounce=0, fsync=False)
    pid = st.connect_nextcloud(share["url"], nb.USER, nb.PASSWORD, "",
                               webdav_root=share["url"])[0]["id"]
    st.client.bulk_enabled = bulk
    st.open(pid, start=False)
    return st, pid


def _heads(mirror) -> dict:
    with mirror.lock:
        return {rel: (h.get("hash"), h.get("size")) for rel, h in mirror.heads.items()}


def _files(root: str) -> dict:
    out = {}
    for r, _d, fs in os.walk(root):
        for f in fs:
            path = os.path.join(r, f)
            rel = os.path.relpath(path, root).replace("\\", "/")
            if rel.startswith(".studio/"):
                continue
            with open(path, "rb") as fh:
                out[rel] = fh.read()
    return out


def _index_hashes(store) -> dict:
    with store.client._guard:
        return {rel: (e.get("hash"), e.get("size"))
                for rel, e in store.client._index["files"].items()}


def _core(share) -> set:
    root = share["root"]
    out = set()
    for r, _d, fs in os.walk(root):
        rel = os.path.relpath(r, root).replace("\\", "/")
        if rel.startswith(".studio"):
            continue
        for f in fs:
            path = f if rel == "." else rel + "/" + f
            if protocol.is_core(path):
                out.add(path)
    return out


def _served(fake) -> dict:
    with fake.state.lock:
        return {p: n for p, n in fake.state.served.items() if not p.startswith(".studio")}


def _assert_delivered_once(served: dict, core: set) -> None:
    """Every core file arrived; at most the probe's file was read twice.

    The capability probe fetches one small file (its filtered archive) and the
    batch that file belongs to normally *installs* those bytes instead of
    fetching them again.  When that batch is not reached in the round (a
    budget, an interruption), the file comes through the per-file path - the
    one duplicate allowed here.
    """
    assert core <= set(served)
    assert max(served.values()) <= 2
    dups = {p: n for p, n in served.items() if n > 1}
    assert len(dups) <= 1, ("duplicates", sorted(dups)[:6])


# ---------------------------------------------------------------------------
# equivalence with the per-file path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("archive", ["tar", "zip"])
def test_first_sync_through_an_archive_equals_the_per_file_result(make_share, tmp_path, archive):
    share = make_share(archive=archive)
    try:
        a, _ = _store(share, tmp_path / "a", bulk=False)
        assert a.sync_now()["mode"] == "online"
        a_heads, a_files, a_index = _heads(a.mirror), _files(a.mirror.root), _index_hashes(a)
        a_core = _core(share)
        assert a_core <= set(a_heads)
        a.close()
        assert share["fake"].state.archive_requests == []      # the per-file path

        share["fake"].resets()
        b, _ = _store(share, tmp_path / "b", bulk=True)
        assert b.sync_now()["mode"] == "online"
        b_heads, b_files, b_index = _heads(b.mirror), _files(b.mirror.root), _index_hashes(b)
        requests = list(share["fake"].state.archive_requests)
        stats = dict(b.client.stats)
        served = _served(share["fake"])
        b.close()
    finally:
        share["fake"].stop()

    assert b_heads == a_heads
    assert b_files == a_files
    assert b_index == a_index
    # it really went through the archive: one request carried many files
    assert requests, stats
    assert all(r["kind"] == archive for r in requests)
    assert stats["archive_files"] >= len(a_core) - 1
    assert stats["archive_requests"] < len(a_core) / 2
    # and every core file was delivered exactly once (the probe's file included)
    assert set(served) >= a_core
    _assert_delivered_once(served, a_core)


# ---------------------------------------------------------------------------
# only what was asked for
# ---------------------------------------------------------------------------

def test_a_bulk_first_sync_writes_no_on_demand_file(make_share, tmp_path):
    share = make_share()
    try:
        st, _ = _store(share, tmp_path / "m")
        st.sync_now()
        written = set(_files(st.mirror.root))
        core = _core(share)
        assert core <= written
        # nothing on demand: papers, supplements, analysis products, raw data,
        # the AI reliability codings
        assert not {p for p in written if not protocol.is_core(p)}
        served = _served(share["fake"])
        assert not {p for p in served if not protocol.is_core(p)}
        st.close()
    finally:
        share["fake"].stop()


def test_traversal_and_foreign_archive_members_are_ignored(make_share, tmp_path):
    extra = [("../escape.txt", b"escaped\n"),
             ("/absolute.txt", b"absolute\n"),
             ("C:/windows.txt", b"drive\n"),
             ("Text/Not in the manifest.txt", b"on demand\n")]
    share = make_share(archive_extra=extra)
    try:
        st, _ = _store(share, tmp_path / "m")
        st.sync_now()
        root = st.mirror.root
        for name in ("escape.txt", "absolute.txt", "windows.txt"):
            assert not os.path.exists(os.path.join(os.path.dirname(root), name))
        assert not os.path.exists(os.path.join(tmp_path, "escape.txt"))
        assert not os.path.exists(os.path.join(root, "Text", "Not in the manifest.txt"))
        # the legitimate files still arrived
        assert _core(share) <= set(_files(root))
        st.close()
    finally:
        share["fake"].stop()


def test_a_file_the_server_flags_as_unreadable_is_fetched_again(make_share, tmp_path):
    """``missing_files.json`` wins over the (zero-padded) member: a single GET runs."""
    rel = "Text/Study0003 et al. (2003).txt"
    share = make_share(archive_missing=[rel])
    try:
        with open(os.path.join(share["root"], *rel.split("/")), "rb") as fh:
            real = fh.read()
        st, _ = _store(share, tmp_path / "m")
        st.syncer.round_budget = 120
        st.sync_now()
        digest = hashlib.sha256(real).hexdigest()
        assert _heads(st.mirror)[rel][0] == digest
        with open(st.mirror.abspath(rel), "rb") as fh:
            assert fh.read() == real
        assert st.client.stats["archive_missing"] >= 1
        assert _served(share["fake"])[rel] == 2       # archive, then the real GET
        st.close()
    finally:
        share["fake"].stop()


def test_member_rel_refuses_anything_but_a_wanted_path():
    wanted = {"Text/A.txt", "Datasets/K/K - variables.csv"}
    assert ncarchive.member_rel("Text/A.txt", wanted) == "Text/A.txt"
    # a full-folder archive names its root: that one component may be dropped
    assert ncarchive.member_rel("Text/Text/A.txt", wanted) == "Text/A.txt"
    # Nextcloud names filtered members "/<path relative to the collection>"
    assert ncarchive.member_rel("/Text/A.txt", wanted) == "Text/A.txt"
    for bad in ("../Text/A.txt", "Text/../../A.txt", "/etc/passwd", "//Text/B.txt",
                "/../Text/A.txt", "C:/Text/A.txt", "Text/A.txt\x00", "", ".", "/", "Text/B.txt"):
        assert ncarchive.member_rel(bad, wanted) is None, bad


# ---------------------------------------------------------------------------
# fallback and cache
# ---------------------------------------------------------------------------

def test_a_share_without_archive_support_falls_back_and_caches_it(make_share, tmp_path):
    share = make_share(archive=False)
    try:
        st, _ = _store(share, tmp_path / "m")
        st.sync_now()
        core = _core(share)
        assert core <= set(_heads(st.mirror))
        assert share["fake"].state.archive_requests == []
        assert st.client.stats["archive_requests"] == 0
        assert st.client._index["archive"]["formats"] == []
        probes = st.client.stats["archive_probes"]
        # cached: no second probe in this process ...
        st.client.archive_formats()
        assert st.client.stats["archive_probes"] == probes
        st.close()
        # ... and in the next one (it lives in the mirror's index)
        st2, _ = _store(share, tmp_path / "m")
        assert st2.client.archive_formats() == []
        assert st2.client.stats["archive_probes"] == 0
        st2.close()
    finally:
        share["fake"].stop()


def test_a_capable_share_is_detected_with_one_small_probe(make_share, tmp_path):
    share = make_share()
    try:
        st, _ = _store(share, tmp_path / "m")
        st.client.manifest(st.mirror.state["project_id"], 0)
        assert st.client.archive_formats() in (["tar"], ["zip"])
        assert st.client.stats["archive_probes"] == 1
        assert st.client._index["archive"]["probe"]
        st.close()
    finally:
        share["fake"].stop()


# ---------------------------------------------------------------------------
# interruption
# ---------------------------------------------------------------------------

def test_an_interrupted_archive_resumes_without_downloading_again(make_share, tmp_path):
    share = make_share(archive_abort_after=3000)
    try:
        st, _ = _store(share, tmp_path / "m")
        st.syncer.round_budget = 120        # the interruption, not the round budget
        for _ in range(8):
            st.sync_now()                   # a truncated stream parks and retries
            if not st.mirror.state.get("stale"):
                break
        # the truncated stream was abandoned and the normal path delivered the rest
        assert st.client.stats["archive_fallbacks"] >= 1
        core = _core(share)
        assert core <= set(_heads(st.mirror))
        for rel, (digest, _size) in _heads(st.mirror).items():
            assert len(digest) == 64 and not digest.startswith(TOKEN_PREFIX)
        on_disk = set(_files(st.mirror.root))
        st.close()

        # a reopen against a healthy share downloads nothing that is already there
        share["fake"].state.archive_abort_after = 0
        share["fake"].resets()
        st2, _ = _store(share, tmp_path / "m")
        st2.syncer.round_budget = 120
        for _ in range(4):
            st2.sync_now()
            if not st2.mirror.state.get("stale"):
                break
        again = set(_served(share["fake"])) & on_disk
        assert again == set(), sorted(again)[:5]
        assert core <= set(_heads(st2.mirror))
        st2.close()
    finally:
        share["fake"].stop()


def test_a_paused_bulk_round_leaves_the_rest_to_the_next_one(make_share, tmp_path):
    share = make_share()
    try:
        st, _ = _store(share, tmp_path / "m")
        st.syncer.round_budget = 0.0            # list, then stop before any request
        st.sync_now()
        assert st.mirror.state["stale"]
        assert st.syncer._more_pending
        st.syncer.round_budget = 120
        st.sync_now()
        assert _core(share) <= set(_heads(st.mirror))
        _assert_delivered_once(_served(share["fake"]), _core(share))
        st.close()
    finally:
        share["fake"].stop()
