"""Blob store and blob endpoints (INTERFACES §3.6)."""

from __future__ import annotations

import asyncio
import gzip
import hashlib

import pytest
from conftest import push_entries, push_file

from studio_server.blobs import (
    BadRange,
    BlobHashMismatch,
    BlobStore,
    BlobTooLarge,
    gunzip_iter,
    parse_range,
)


def blob_url(pid: str, digest: str) -> str:
    return f"/api/p/{pid}/blobs/{digest}"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def reference(client, headers, pid: str, path: str, digest: str, size: int) -> None:
    """Make *digest* reachable from a readable path (§3.6 GET authorization)."""
    body = push_entries(
        client,
        headers,
        pid,
        [{"path": path, "base_hash": None, "new_hash": digest, "size": size}],
    )
    assert body["results"][0]["status"] == "accepted", body


# ── store unit tests ───────────────────────────────────────────────────


def test_store_layout_and_roundtrip(tmp_path):
    store = BlobStore(tmp_path)
    data = b"hello blob\n"
    digest = sha(data)
    store.write_bytes(digest, data)
    assert store.exists(digest)
    assert store.path(digest) == tmp_path / "blobs" / digest[:2] / digest
    assert store.path(digest).is_file()
    assert store.read_bytes(digest) == data
    assert store.size(digest) == len(data)
    # idempotent
    assert store.write_bytes(digest, data) == len(data)


def test_store_rejects_wrong_hash(tmp_path):
    store = BlobStore(tmp_path)
    with pytest.raises(BlobHashMismatch):
        store.write_bytes(sha(b"other"), b"content")
    assert not store.exists(sha(b"other"))


def test_store_size_limit(tmp_path):
    store = BlobStore(tmp_path, max_bytes=4)
    with pytest.raises(BlobTooLarge):
        store.write_bytes(sha(b"12345"), b"12345")


def test_streaming_write_verifies_and_leaves_no_temp(tmp_path):
    store = BlobStore(tmp_path)
    data = b"streamed" * 1000
    digest = sha(data)

    async def chunks():
        for start in range(0, len(data), 333):
            yield data[start : start + 333]

    size = asyncio.run(store.write_stream(digest, chunks()))
    assert size == len(data)
    assert store.read_bytes(digest) == data
    leftovers = [p for p in (tmp_path / "blobs").rglob(".tmp-*")]
    assert leftovers == []


def test_streaming_write_mismatch_removes_temp(tmp_path):
    store = BlobStore(tmp_path)
    digest = sha(b"expected")

    async def chunks():
        yield b"something else"

    with pytest.raises(BlobHashMismatch):
        asyncio.run(store.write_stream(digest, chunks()))
    assert not store.exists(digest)
    assert [p for p in (tmp_path / "blobs").rglob(".tmp-*")] == []


def test_streaming_write_limit(tmp_path):
    store = BlobStore(tmp_path, max_bytes=3)

    async def chunks():
        yield b"ab"
        yield b"cd"

    with pytest.raises(BlobTooLarge):
        asyncio.run(store.write_stream(sha(b"abcd"), chunks()))
    assert [p for p in (tmp_path / "blobs").rglob(".tmp-*")] == []


def test_gunzip_iter_decodes_incrementally():
    payload = b"x" * 100_000
    squeezed = gzip.compress(payload)
    pieces = [squeezed[i : i + 777] for i in range(0, len(squeezed), 777)]
    assert b"".join(gunzip_iter(pieces)) == payload


def test_parse_range():
    assert parse_range(None, 100) is None
    assert parse_range("bytes=0-9", 100) == (0, 9)
    assert parse_range("bytes=90-", 100) == (90, 99)
    assert parse_range("bytes=-10", 100) == (90, 99)
    assert parse_range("bytes=0-1000", 100) == (0, 99)
    assert parse_range("bytes=0-1,4-5", 100) is None  # multi-range: whole file
    for bad in ("bytes=100-", "bytes=9-4", "bytes=abc", "items=0-1", "bytes=-0"):
        with pytest.raises(BadRange):
            parse_range(bad, 100)


# ── endpoints ──────────────────────────────────────────────────────────


def test_upload_get_roundtrip(client, project, admin_headers):
    pid = project["id"]
    data = b"binary \x00 content"
    digest = sha(data)
    uploaded = client.put(blob_url(pid, digest), content=data, headers=admin_headers)
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json() == {"hash": digest, "size": len(data)}

    # idempotent re-upload
    again = client.put(blob_url(pid, digest), content=data, headers=admin_headers)
    assert again.status_code == 200

    reference(client, admin_headers, pid, "Extraction/Blob test.json", digest, len(data))

    fetched = client.get(blob_url(pid, digest), headers=admin_headers)
    assert fetched.status_code == 200
    assert fetched.content == data
    assert fetched.headers["content-length"] == str(len(data))
    assert fetched.headers["accept-ranges"] == "bytes"


def test_missing_endpoint(client, project, admin_headers):
    pid = project["id"]
    present = b"present"
    digest = sha(present)
    client.put(blob_url(pid, digest), content=present, headers=admin_headers)
    absent = sha(b"absent")
    response = client.post(
        f"/api/p/{pid}/blobs/missing",
        json={"hashes": [digest, absent, "not-a-hash"]},
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["missing"] == [absent, "not-a-hash"]


def test_upload_hash_mismatch_is_400(client, project, admin_headers):
    pid = project["id"]
    response = client.put(
        blob_url(pid, sha(b"expected")), content=b"actual", headers=admin_headers
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "bad_request"


def test_upload_invalid_hash_is_400(client, project, admin_headers):
    response = client.put(
        f"/api/p/{project['id']}/blobs/not-a-hash", content=b"x", headers=admin_headers
    )
    assert response.status_code == 400


def test_upload_gzip_body_is_decoded(client, project, admin_headers):
    pid = project["id"]
    data = b"gzip me " * 2000
    digest = sha(data)
    response = client.put(
        blob_url(pid, digest),
        content=gzip.compress(data),
        headers={**admin_headers, "Content-Encoding": "gzip"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["size"] == len(data)
    reference(client, admin_headers, pid, "Extraction/Gzip test.json", digest, len(data))
    fetched = client.get(blob_url(pid, digest), headers=admin_headers)
    assert fetched.content == data


def test_upload_bad_gzip_is_400(client, project, admin_headers):
    pid = project["id"]
    response = client.put(
        blob_url(pid, sha(b"x")),
        content=b"not gzip",
        headers={**admin_headers, "Content-Encoding": "gzip"},
    )
    assert response.status_code == 400


def test_upload_obeys_configurable_limit(client, project, admin_headers, monkeypatch):
    monkeypatch.setenv("STUDIO_MAX_BLOB_MB", "0.0001")  # ~105 bytes
    data = b"x" * 500
    response = client.put(
        blob_url(project["id"], sha(data)), content=data, headers=admin_headers
    )
    assert response.status_code == 413


def test_range_download(client, project, admin_headers):
    pid = project["id"]
    data = bytes(range(256)) * 8
    digest = sha(data)
    client.put(blob_url(pid, digest), content=data, headers=admin_headers)
    reference(client, admin_headers, pid, "Extraction/Range test.json", digest, len(data))

    part = client.get(blob_url(pid, digest), headers={**admin_headers, "Range": "bytes=10-19"})
    assert part.status_code == 206
    assert part.content == data[10:20]
    assert part.headers["content-range"] == f"bytes 10-19/{len(data)}"
    assert part.headers["content-length"] == "10"

    tail = client.get(blob_url(pid, digest), headers={**admin_headers, "Range": "bytes=-5"})
    assert tail.status_code == 206
    assert tail.content == data[-5:]

    open_ended = client.get(
        blob_url(pid, digest), headers={**admin_headers, "Range": "bytes=250-"}
    )
    assert open_ended.status_code == 206
    assert open_ended.content == data[250:]

    unsatisfiable = client.get(
        blob_url(pid, digest), headers={**admin_headers, "Range": "bytes=99999-"}
    )
    assert unsatisfiable.status_code == 416
    assert unsatisfiable.headers["content-range"] == f"bytes */{len(data)}"


def test_gzip_download_only_for_large_files_and_never_with_range(
    client, project, admin_headers
):
    pid = project["id"]
    big = b"compress me " * 1000
    big_digest = sha(big)
    client.put(blob_url(pid, big_digest), content=big, headers=admin_headers)
    reference(client, admin_headers, pid, "Extraction/Gzip download.json", big_digest, len(big))

    response = client.get(
        blob_url(pid, big_digest), headers={**admin_headers, "Accept-Encoding": "gzip"}
    )
    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "gzip"
    body = response.content
    if body[:2] == b"\x1f\x8b":  # httpx may already have decoded it
        body = gzip.decompress(body)
    assert body == big

    # Range + gzip are not combined
    ranged = client.get(
        blob_url(pid, big_digest),
        headers={**admin_headers, "Accept-Encoding": "gzip", "Range": "bytes=0-9"},
    )
    assert ranged.status_code == 206
    assert ranged.headers.get("content-encoding") != "gzip"
    assert ranged.content == big[:10]

    # small blobs are not compressed
    small = b"tiny"
    small_digest = sha(small)
    client.put(blob_url(pid, small_digest), content=small, headers=admin_headers)
    reference(client, admin_headers, pid, "Extraction/Gzip small.json", small_digest, len(small))
    tiny = client.get(
        blob_url(pid, small_digest), headers={**admin_headers, "Accept-Encoding": "gzip"}
    )
    assert tiny.headers.get("content-encoding") is None


def test_blob_get_requires_a_readable_reference(client, project, cfg, admin_headers, coder):
    from conftest import CODER_PASSWORD, CODER_USERNAME, join_project

    pid = project["id"]
    anna = join_project(
        client, admin_headers, pid, coder, CODER_USERNAME, CODER_PASSWORD,
        tags=["Anna"], tasks=["coding"],
    )
    owner_result = push_file(
        client, admin_headers, pid, "Codings/K1 - Anna.json", b'{"coded": true}'
    )
    digest = owner_result["hash"]

    # Anna may read her own coding file, so she may read its blob
    assert client.get(blob_url(pid, digest), headers=anna).status_code == 200
    # the owner always may
    assert client.get(blob_url(pid, digest), headers=admin_headers).status_code == 200

    # a blob referenced only by another tag's file is forbidden to Anna
    other = push_file(
        client, admin_headers, pid, "Codings/K2 - Bob.json", b'{"coded": false}'
    )
    blocked = client.get(blob_url(pid, other["hash"]), headers=anna)
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "forbidden"

    # ... but becomes readable once Anna marks the key done (past versions too)
    done = client.post(
        f"/api/p/{pid}/done",
        json={"key": "K2", "stage": "coding", "tag": "Anna"},
        headers=anna,
    )
    assert done.status_code == 200
    assert client.get(blob_url(pid, other["hash"]), headers=anna).status_code == 200

    # a blob nobody references is not readable, even to the owner
    orphan = b"orphan"
    orphan_digest = sha(orphan)
    client.put(blob_url(pid, orphan_digest), content=orphan, headers=admin_headers)
    assert (
        client.get(blob_url(pid, orphan_digest), headers=admin_headers).status_code
        == 403
    )


def test_blob_get_unknown_is_404(client, project, admin_headers):
    response = client.get(blob_url(project["id"], sha(b"nope")), headers=admin_headers)
    assert response.status_code == 404
    assert response.json()["detail"] == "not_found"


def test_blob_endpoints_require_auth(client, project):
    response = client.get(blob_url(project["id"], sha(b"x")))
    assert response.status_code == 401
