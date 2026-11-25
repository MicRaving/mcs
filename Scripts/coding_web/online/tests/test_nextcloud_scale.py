"""Nextcloud mode at scale - regression tests for the owner report of 2026-11-25.

"Opening took very long / seemed stuck", "after reopening it started from
scratch", "papers or data missing".  Root causes (see
``Results/PHASE9b_nextcloud_scale.md``):

1. the listing (``NextcloudClient.manifest``) downloaded every core file, one
   request at a time, before the engine saw a single path - nothing was listed
   for 46 minutes on the owner's share;
2. nothing of that work was saved until the listing ended, so a closed window
   restarted it from zero;
3. the desktop app built its paper keys when the project opened, before any
   listing existed, and never again (until "Sync now").

These tests pin the fixes on a synthetic share in the real project's shape
(``nc_benchmark.build_share``), scaled down.
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from online.store import OnlineStore
from online.tests import nc_benchmark as nb
from online.tests.webdav_fake import WebDAVFake
from online_shared import protocol


@pytest.fixture
def share(tmp_path):
    info = nb.build_share(str(tmp_path / "share"), keys=12, size_scale=0.01, extra_scale=0.03)
    fake = WebDAVFake(str(tmp_path / "share"), fast_etags=True)
    url = fake.start()
    yield {"info": info, "fake": fake, "url": url, "tmp": tmp_path}
    fake.stop()


def _store(base, **kw) -> OnlineStore:
    kw.setdefault("autostart", False)
    return OnlineStore(base_dir=str(base), use_keyring=False, debounce=0, fsync=False, **kw)


def _open(share, base, **kw):
    st = _store(base, **kw)
    pid = st.connect_nextcloud(share["url"], nb.USER, nb.PASSWORD, "", webdav_root=share["url"])[0]["id"]
    st.open(pid, start=kw.get("autostart", False))
    return st, pid


def _project_gets(fake) -> dict:
    with fake.state.lock:
        return {p: n for p, n in fake.state.gets.items() if not p.startswith(".studio")}


def _core_paths(info_paths) -> set:
    return {p for p in info_paths if protocol.is_core(p)}


def _listed(share) -> set:
    out = set()
    root = os.path.join(str(share["tmp"]), "share")
    for r, _d, fs in os.walk(root):
        rel = os.path.relpath(r, root).replace("\\", "/")
        if rel.startswith(".studio"):
            continue
        for f in fs:
            out.add(f if rel == "." else rel + "/" + f)
    return out


# ---------------------------------------------------------------------------

def test_listing_downloads_nothing_and_names_every_file(share):
    """The manifest is one PROPFIND - no GET of a project file."""
    st, pid = _open(share, share["tmp"] / "c1")
    share["fake"].resets()
    man = st.client.manifest(pid, 0)
    assert len(man["files"]) == share["info"]["files"]
    assert _project_gets(share["fake"]) == {}
    # not-yet-downloaded files carry a version token
    assert all(f["hash"].startswith("nc1-") for f in man["files"])
    st.close()


def test_first_round_lists_before_downloading_and_resolves_hashes(share):
    st, _pid = _open(share, share["tmp"] / "c1")
    seen = {}

    def _on_listing():
        # the listing is applied (and saved) before the first download
        seen.setdefault("remote", len(st.mirror.remote))
        seen.setdefault("heads", len(st.mirror.heads))
        with open(st.mirror.state_path, encoding="utf-8") as fh:
            seen.setdefault("saved_remote", len(json.load(fh)["remote"]))

    st.syncer.on_listing = _on_listing
    assert st.sync_now()["mode"] == "online"
    info = share["info"]
    assert seen["remote"] == info["files"] and seen["saved_remote"] == info["files"]
    assert seen["heads"] == 0
    core = _core_paths(_listed(share))
    assert core <= set(st.mirror.heads)
    # heads carry real content hashes (the base of a later merge), never tokens
    for rel in core:
        h = st.mirror.heads[rel]["hash"]
        assert len(h) == 64 and not h.startswith("nc1-")
        assert st.mirror.remote[rel]["hash"] == h
    # every core file downloaded exactly once, nothing else
    gets = _project_gets(share["fake"])
    assert set(gets) == core and set(gets.values()) == {1}
    st.close()


def test_downloads_run_in_parallel_but_never_more_than_four(share):
    st, _pid = _open(share, share["tmp"] / "c1")
    fake = share["fake"]
    fake.state.latency = (0.01, 0.02)
    active = {"now": 0, "max": 0}
    lock = threading.Lock()
    dav = st.client.dav
    real = dav._send_once

    def counting(*a, **kw):
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        try:
            return real(*a, **kw)
        finally:
            with lock:
                active["now"] -= 1

    dav._send_once = counting
    took = {}
    real_many = st.syncer.download_many

    def timed_many(*a, **kw):
        t = time.perf_counter()
        try:
            return real_many(*a, **kw)
        finally:
            took["s"] = took.get("s", 0.0) + time.perf_counter() - t

    st.syncer.download_many = timed_many
    st.sync_now()
    assert 1 < active["max"] <= 4, active
    n = len(_core_paths(_listed(share)))
    # one at a time would need at least n x the mean latency (15 ms)
    assert took["s"] < n * 0.015, (took, n)
    st.close()


def test_a_killed_first_sync_resumes_without_downloading_again(share, monkeypatch):
    """Worst case: the process dies after the listing was saved and before any
    head was - only the files on disk and the index journal survive."""
    base = share["tmp"] / "c1"
    st, _pid = _open(share, base)
    info = share["info"]
    core = sorted(_core_paths(_listed(share)))
    stop_after = len(core) // 2
    fake = share["fake"]
    real_save = st.mirror.save
    calls = {"n": 0}

    def save_listing_only():
        calls["n"] += 1
        if calls["n"] <= 1:
            real_save()                       # the listing save before the downloads

    monkeypatch.setattr(st.mirror, "save", save_listing_only)
    hook_state = {"gets": 0}

    class Crash(BaseException):
        pass

    def hook(method, path, phase, resp=None):
        if method == "GET" and phase == "after" and not path.startswith(".studio"):
            hook_state["gets"] += 1
            if hook_state["gets"] > stop_after:
                raise Crash()

    st.client.dav.hook = hook
    st.syncer.parallel = 1                     # deterministic: exactly stop_after files land
    with pytest.raises(Crash):
        st.sync_now()
    on_disk = {p for p in core if os.path.exists(st.mirror.abspath(p))}
    assert 0 < len(on_disk) < len(core)
    with open(st.mirror.state_path, encoding="utf-8") as fh:
        saved = json.load(fh)
    assert len(saved["heads"]) == 0 and len(saved["remote"]) == info["files"]
    # "restart": new objects, nothing of the old process
    fake.resets()
    st2, _ = _open(share, base)
    st2.sync_now()
    gets = _project_gets(fake)
    again = sorted(set(gets) & on_disk)
    assert again == [], f"downloaded again although on disk: {again[:5]}"
    assert set(core) <= set(st2.mirror.heads)
    assert len(st2.syncer.outbox) == 0          # nothing was mistaken for a local edit
    st2.close()


def test_closing_mid_download_keeps_what_arrived(share):
    """A closed window (``store.close`` -> ``syncer.stop``) saves and parks the rest."""
    fake = share["fake"]
    fake.state.latency = (0.02, 0.03)
    base = share["tmp"] / "c1"
    st, _pid = _open(share, base, autostart=True)
    core = _core_paths(_listed(share))
    deadline = time.time() + 30
    while time.time() < deadline and len(st.mirror.heads) < len(core) // 3:
        time.sleep(0.02)
    st.close()
    with open(os.path.join(str(base), os.listdir(str(base))[0],
                           os.listdir(os.path.join(str(base), os.listdir(str(base))[0]))[0],
                           ".studio", "state.json"), encoding="utf-8") as fh:
        saved = json.load(fh)
    kept = {p for p in saved["heads"] if p in core}
    assert kept, "the heads of the finished downloads were saved on close"
    fake.state.latency = None
    fake.resets()
    st2, _ = _open(share, base)
    st2.sync_now()
    assert not (set(_project_gets(fake)) & kept)
    assert core <= set(st2.mirror.heads)
    st2.close()


def test_reopen_and_no_change_round_download_nothing(share):
    base = share["tmp"] / "c1"
    st, _pid = _open(share, base)
    st.sync_now()
    st.presence(None, None)                    # verifies the folder-ETag propagation
    st.presence(None, None)
    assert st.client._index.get("etag_propagates") is True
    fake = share["fake"]
    fake.resets()
    st.sync_now()
    with fake.state.lock:
        methods = dict(fake.state.methods)
    assert methods.get("GET", 0) == 0
    # one Depth: 1 listing of the top level, no Depth: infinity
    assert methods.get("PROPFIND", 0) == 1, methods
    st.close()
    fake.resets()
    st2, _ = _open(share, base)
    st2.sync_now()
    assert _project_gets(fake) == {}
    st2.close()


def test_incremental_listing_sees_a_deep_change_and_a_new_file(share):
    base = share["tmp"] / "c1"
    st, _pid = _open(share, base)
    st.sync_now()
    st.presence(None, None)
    st.presence(None, None)
    root = os.path.join(str(share["tmp"]), "share")
    key = share["info"]["keys"][3]
    rel = f"Extraction/{key}.json"
    with open(os.path.join(root, *rel.split("/")), "wb") as fh:
        fh.write(b'{"changed": true}\n')
    new = f"Text/Brand new (2026).txt"
    with open(os.path.join(root, *new.split("/")), "wb") as fh:
        fh.write(b"new paper\n")
    share["fake"].state.bump()                 # a write by another client
    share["fake"].resets()
    st.sync_now()
    assert open(st.mirror.abspath(rel), "rb").read() == b'{"changed": true}\n'
    assert os.path.exists(st.mirror.abspath(new))
    gets = _project_gets(share["fake"])
    assert set(gets) == {rel, new}
    with share["fake"].state.lock:
        infinity = [p for (_u, m, p) in share["fake"].state.requests if m == "PROPFIND"]
    # the top level and the two changed folders - never the whole share
    assert "/" in infinity or "" in [p.strip("/") for p in infinity]
    assert len(infinity) <= 3, infinity
    st.close()


def test_on_demand_read_of_a_core_file_before_the_background_reached_it(share):
    st, _pid = _open(share, share["tmp"] / "c1")
    st.syncer.round_budget = 0.0               # list, then park every download
    st.sync_now()
    rel = f"Extraction/{share['info']['keys'][-1]}.json"
    assert not os.path.exists(st.mirror.abspath(rel))
    path = st.ensure_local(rel)
    assert os.path.exists(path)
    head = st.mirror.heads[rel]["hash"]
    assert len(head) == 64
    # the paper's whole core set jumps the queue
    key = share["info"]["keys"][-2]
    n = st.ensure_key(key)
    assert n >= 1
    assert os.path.exists(st.mirror.abspath(f"Text/{key}.txt"))
    st.close()


def test_progress_is_reported_while_the_core_tier_downloads(share):
    st, _pid = _open(share, share["tmp"] / "c1")
    share["fake"].state.latency = (0.005, 0.01)
    samples = []
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            samples.append(st.status().get("download") or {})
            time.sleep(0.01)

    t = threading.Thread(target=poll)
    t.start()
    try:
        st.sync_now()
    finally:
        stop.set()
        t.join()
    busy = [s for s in samples if s.get("phase") == "downloading" and s.get("files_total")]
    assert busy, "no progress was visible during the download"
    s = busy[len(busy) // 2]
    assert 0 <= s["files_done"] <= s["files_total"] and s["bytes_total"] > 0
    assert st.status()["download"]["phase"] == "idle"
    st.close()


def test_a_budgeted_round_parks_the_rest_and_the_next_round_continues(share):
    st, _pid = _open(share, share["tmp"] / "c1")
    share["fake"].state.latency = (0.01, 0.01)
    st.syncer.round_budget = 0.05
    st.sync_now()
    core = _core_paths(_listed(share))
    first = len([p for p in st.mirror.heads if p in core])
    assert first < len(core)
    assert st.syncer._more_pending
    assert st.mirror.state["seq"] > 0          # the window advanced: the rest is ``stale``
    st.syncer.round_budget = 60
    st.sync_now()
    assert core <= set(st.mirror.heads)
    assert not _project_gets(share["fake"]) or max(_project_gets(share["fake"]).values()) == 1
    st.close()


def test_the_index_is_not_rewritten_per_file(share):
    st, _pid = _open(share, share["tmp"] / "c1")
    st.sync_now()
    n_core = len(_core_paths(_listed(share)))
    # the snapshot is written per listing, the journal carries the downloads
    assert st.client.stats["index_saves"] <= 3, st.client.stats
    assert st.client.stats["downloads"] == n_core
    st.close()


def test_the_session_spool_is_cleaned_on_open(share):
    base = share["tmp"] / "c1"
    st, pid = _open(share, base)
    spool = os.path.join(st.mirror.studio, "nc-spool")
    with open(os.path.join(spool, "leftover"), "wb") as fh:
        fh.write(b"x" * 100)
    st.close()
    st2, _ = _open(share, base)
    assert os.listdir(spool) == []
    st2.close()


def test_index_journal_replays_after_a_torn_last_line(share, tmp_path):
    base = share["tmp"] / "c1"
    st, _pid = _open(share, base)
    st.sync_now()
    client = st.client
    mirror = st.mirror
    rel = next(iter(client._index["files"]))
    entry = dict(client._index["files"][rel], hash="f" * 64)
    st.close()                                    # compacts the index, drops the journal
    # a crash after one journalled change, mid-way through the next append
    with open(client._journal_path, "ab") as fh:
        fh.write((json.dumps({"p": rel, "e": entry, "s": 1}) + "\n").encode())
        fh.write(b'{"p": "torn')
    from online.nextcloud import NextcloudClient
    c2 = NextcloudClient(share["url"], nb.USER, nb.PASSWORD, "", webdav_root_url=share["url"],
                         allow_insecure_http=True)
    c2.bind_mirror(mirror)
    assert c2._index["files"][rel]["hash"] == "f" * 64
    assert c2._path_for_token("f" * 64) == rel
