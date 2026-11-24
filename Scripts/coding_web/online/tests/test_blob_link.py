"""Server-local blob linking (browser workers, PLAN_online_only §4.4).

A worker on the server links mirror files to the read-only blob store instead
of downloading them.  These tests pin the rules:

* linked files are what the scanner sees (no phantom deletions, no pushes);
* an app write (atomic replace) swaps the link for a real file and is pushed;
* a later server version is linked again;
* only hashes from the user's own manifest are ever linked (blinding);
* without symlink support the syncer silently downloads like a desktop.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile

import pytest

from online.tests.conftest import PID, jbytes


def _symlinks_supported() -> bool:
    d = tempfile.mkdtemp()
    try:
        src = os.path.join(d, "a")
        open(src, "w").close()
        os.symlink(src, os.path.join(d, "b"))
        return True
    except (OSError, NotImplementedError):
        return False


needs_symlinks = pytest.mark.skipif(not _symlinks_supported(),
                                    reason="this OS user cannot create symlinks")


def blob_dir(server, base) -> str:
    """Materialise the fake server's blobs as ``<dir>/<h[:2]>/<h>`` (read-only files)."""
    d = os.path.join(base, "blobs")
    for h, data in server.blobs.items():
        p = os.path.join(d, h[:2], h)
        if os.path.exists(p):
            continue
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)
        os.chmod(p, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return d


def atomic_write(path: str, data: bytes) -> None:
    """What the app does (``studio_lib.atomic_replace``): temp file + replace."""
    tmp = path + ".tmp-test"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def linked_node(server, make_node, tmp_path, username="a"):
    d = blob_dir(server, str(tmp_path))
    node = make_node(username)
    node.syncer.local_blobs = d
    return node, d


@needs_symlinks
def test_core_files_are_links_into_the_blob_store(server, make_node, tmp_path):
    server.put_file(PID, "Codings/K1 - A.json", jbytes([{"x": 1}]))
    node, d = linked_node(server, make_node, tmp_path)
    assert node.tick()
    p = node.path("Text/K1.txt")
    assert os.path.islink(p)
    assert os.path.realpath(p).startswith(os.path.realpath(d))
    assert node.read("Text/K1.txt") == b"paper text K1\n"
    # the scanner sees the linked files as the server versions: nothing to push
    res = node.syncer.scan()
    assert res.changes == [] and len(node.syncer.outbox) == 0
    assert not res.missing


@needs_symlinks
def test_app_write_replaces_the_link_and_is_pushed_then_relinked(server, make_node, tmp_path):
    rel = "Codings/K1 - A.json"
    server.put_file(PID, rel, jbytes([{"x": 1}]))
    node, _d = linked_node(server, make_node, tmp_path)
    assert node.tick()
    assert os.path.islink(node.path(rel))
    blob_before = os.path.realpath(node.path(rel))
    with open(blob_before, "rb") as fh:
        original = fh.read()

    atomic_write(node.path(rel), jbytes([{"x": 2}]))
    assert not os.path.islink(node.path(rel))
    assert node.tick()
    assert server.file_bytes(PID, rel) == jbytes([{"x": 2}])
    with open(blob_before, "rb") as fh:              # the old blob is untouched
        assert fh.read() == original

    # the owner changes it again on the server -> the worker links the new version
    server.put_file(PID, rel, jbytes([{"x": 3}]), by="own")
    blob_dir(server, str(tmp_path))
    assert node.tick()
    assert os.path.islink(node.path(rel))
    assert node.read_json(rel) == [{"x": 3}]


@needs_symlinks
def test_only_the_users_own_manifest_is_linked(server, make_node, tmp_path):
    server.put_file(PID, "Codings/K1 - B.json", jbytes([{"secret": 1}]))
    node, _d = linked_node(server, make_node, tmp_path, "a")      # tag A, blinded
    assert node.tick()
    assert not node.exists("Codings/K1 - B.json")
    assert node.syncer.fetch("Text/K1.txt")
    with pytest.raises(FileNotFoundError):
        node.syncer.fetch("Codings/K1 - B.json")


@needs_symlinks
@pytest.mark.skipif(sys.platform.startswith("win") or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="file permissions do not stop this user")
def test_an_in_place_write_cannot_alter_a_blob(server, make_node, tmp_path):
    node, _d = linked_node(server, make_node, tmp_path)
    assert node.tick()
    with pytest.raises(PermissionError):
        with open(node.path("Text/K1.txt"), "wb") as fh:
            fh.write(b"tampered")
    assert node.read("Text/K1.txt") == b"paper text K1\n"


def test_without_symlink_support_files_are_downloaded(server, make_node, tmp_path, monkeypatch):
    node, _d = linked_node(server, make_node, tmp_path)

    def refuse(*_a, **_k):
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(node.mirror, "link_blob", refuse)
    assert node.tick()
    p = node.path("Text/K1.txt")
    assert os.path.isfile(p) and not os.path.islink(p)
    assert node.read("Text/K1.txt") == b"paper text K1\n"
    assert node.syncer.local_blobs is None              # switched off after the first refusal


def test_local_blob_lookup_rejects_non_hashes(server, make_node, tmp_path):
    node, d = linked_node(server, make_node, tmp_path)
    assert node.syncer._local_blob("../../etc/passwd") is None
    assert node.syncer._local_blob("ab" * 32) is None                 # not in the store
    some = next(iter(server.blobs))
    assert node.syncer._local_blob(some) == os.path.join(d, some[:2], some)
