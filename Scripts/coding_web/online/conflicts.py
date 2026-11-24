"""Local conflict records (``<mirror>/.studio/conflicts/``).

When a push comes back ``conflict`` or ``rejected``, the local bytes are kept
under ``.studio/conflicts/<id>/<basename>`` (``<id>`` is the server's conflict
id, or ``rej-<n>`` for a rejection, which has no server record), the server
version is restored into the working tree, and a record is appended to
``.studio/conflicts/index.json``::

    {id, server_id, path, kind: conflict|rejected, reason, base_hash,
     mine_hash, server_hash, files: [relative paths under .studio/],
     ts, state: open|resolved, resolution}

Nothing is ever deleted here: a resolved record keeps its files.

Resolution (done by :meth:`syncer.Syncer.resolve_conflict`):

* ``theirs`` - close it (server conflicts: ``resolve theirs`` on the server).
* ``mine``   - the saved local bytes become the new server version, written
  with base = current server head (server conflicts: the resolve endpoint;
  rejections: re-queued as an ordinary push).
* ``merged`` - like ``mine`` with caller-supplied bytes.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

from .mirror import atomic_write_json, atomic_write_bytes

RESOLUTIONS = ("mine", "theirs", "merged")


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ConflictStore:
    def __init__(self, conflicts_dir: str, fsync: bool = True):
        self.dir = conflicts_dir
        self.index_path = os.path.join(conflicts_dir, "index.json")
        self.fsync = fsync
        self.lock = threading.RLock()
        os.makedirs(conflicts_dir, exist_ok=True)
        self._records = self._load()

    def _load(self) -> list[dict]:
        try:
            with open(self.index_path, "rb") as fh:
                data = json.loads(fh.read().decode("utf-8"))
            return data if isinstance(data, list) else []
        except FileNotFoundError:
            return []
        except (ValueError, UnicodeDecodeError):
            os.replace(self.index_path, self.index_path + f".corrupt-{int(time.time())}")
            return []

    def _save(self) -> None:
        atomic_write_json(self.index_path, self._records, fsync=self.fsync)

    # -- queries ---------------------------------------------------------------

    def list(self, state: str | None = "open") -> list[dict]:
        with self.lock:
            return [dict(r) for r in self._records if state is None or r.get("state") == state]

    def get(self, cid) -> dict | None:
        with self.lock:
            for r in self._records:
                if str(r["id"]) == str(cid):
                    return dict(r)
        return None

    def open_count(self) -> int:
        return len(self.list("open"))

    def find_open(self, path: str) -> list[dict]:
        return [r for r in self.list("open") if r["path"] == path]

    # -- writes ------------------------------------------------------------------

    def _next_rej_id(self) -> str:
        n = 0
        for r in self._records:
            m = re.fullmatch(r"rej-(\d+)", str(r["id"]))
            if m:
                n = max(n, int(m.group(1)))
        return f"rej-{n + 1}"

    def record(self, *, path: str, kind: str, server_id=None, reason: str | None = None,
               base_hash: str | None = None, mine_hash: str | None = None,
               server_hash: str | None = None, versions: list[tuple[str, bytes]] = ()) -> dict:
        """Store the local bytes (*versions*: ``[(suffix, data)]``) and the record.

        The bytes are written (fsync'ed) **before** the index, so a crash can
        leave an orphan folder but never a record without its bytes.
        """
        with self.lock:
            cid = str(server_id) if server_id is not None else self._next_rej_id()
            existing = self.get(cid)
            if existing is not None:
                return existing                     # replayed after a crash
            folder = os.path.join(self.dir, re.sub(r"[^A-Za-z0-9_.-]", "_", cid))
            os.makedirs(folder, exist_ok=True)
            base = path.rsplit("/", 1)[-1]
            files = []
            for suffix, data in versions:
                name = base + suffix
                atomic_write_bytes(os.path.join(folder, name), data, fsync=self.fsync)
                files.append(os.path.relpath(os.path.join(folder, name), os.path.dirname(self.dir)).replace("\\", "/"))
            rec = {"id": cid, "server_id": server_id, "path": path, "kind": kind, "reason": reason,
                   "base_hash": base_hash, "mine_hash": mine_hash, "server_hash": server_hash,
                   "files": files, "ts": _utc(), "state": "open", "resolution": None}
            self._records.append(rec)
            self._save()
            return dict(rec)

    def mine_bytes(self, cid) -> bytes | None:
        """The local version saved with conflict *cid* (the first saved file)."""
        rec = self.get(cid)
        if not rec or not rec.get("files"):
            return None
        with open(os.path.join(os.path.dirname(self.dir), *rec["files"][0].split("/")), "rb") as fh:
            return fh.read()

    def mark_resolved(self, cid, resolution: str) -> dict | None:
        with self.lock:
            for r in self._records:
                if str(r["id"]) == str(cid):
                    r["state"] = "resolved"
                    r["resolution"] = resolution
                    r["resolved_ts"] = _utc()
                    self._save()
                    return dict(r)
        return None
