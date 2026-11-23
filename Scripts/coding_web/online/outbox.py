"""Durable outbox: the journal of local changes waiting to be pushed (§4).

``<mirror>/.studio/outbox.jsonl`` is append-only; every line is one record:

* ``{"op": "put", path, base_hash, new_hash, size, mtime_ns, ts[, order, first_ts]}``
  a local change (``new_hash`` null = delete).  Replay **coalesces per path**:
  the earliest ``base_hash`` is kept, the latest ``new_hash``/stat wins, the
  entry keeps the position of its first change (push order = order of first
  change).  A coalesced entry whose ``new_hash`` equals its ``base_hash`` is a
  no-op and disappears (the user reverted the edit).
* ``{"op": "ack", path, pushed, head}`` - the server took version ``pushed``
  and its head is now ``head`` (``pushed`` again for ``accepted``, the merged
  hash for ``merged``).  If the entry still carries ``pushed`` it is removed;
  if a newer version was queued meanwhile it stays, rebased on ``head``.
* ``{"op": "sent", path, hash}`` - version ``hash`` is being pushed; until the
  ack the outcome is unknown (a lost response, a crash).  Kept with the entry
  through later coalescing so the next pull can recognise our own earlier
  push on the server and rebase on it instead of conflicting with it.
* ``{"op": "rebase", path, base_hash}`` - set an entry's base.
* ``{"op": "drop", path}`` - remove an entry (conflict handled, cancelled).

Every append is flushed and fsync'ed before the call returns, so an entry is
never lost once :meth:`Outbox.add` returned.  A crash in the middle of an
append leaves a torn last line, which replay ignores (and the next
compaction removes).  Compaction rewrites the coalesced view to a temp file,
fsyncs it and ``os.replace``s it over the journal: a crash at any point leaves
either the old or the new journal, both complete.
"""

from __future__ import annotations

import json
import os
import threading
import time

from .mirror import fsync_dir, replace_retry

COMPACT_MIN_LINES = 200


class Outbox:
    def __init__(self, path: str, fsync: bool = True, clock=time.time):
        self.path = path
        self.fsync = fsync
        self.clock = clock
        self.lock = threading.RLock()
        self._entries: dict[str, dict] = {}
        self._order = 0
        self._lines = 0
        self._load()

    # -- replay -------------------------------------------------------------

    def _load(self) -> None:
        tmp = self.path + ".compact"
        if os.path.exists(tmp):           # crash during compaction before the replace
            os.unlink(tmp)
        try:
            with open(self.path, "rb") as fh:
                raw = fh.read()
        except FileNotFoundError:
            return
        chunks = raw.split(b"\n")
        torn = chunks[-1] != b""          # last line without its newline: torn append
        bad = torn
        for chunk in chunks[:-1]:
            if not chunk.strip():
                continue
            try:
                rec = json.loads(chunk.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                bad = True
                continue
            self._apply(rec)
            self._lines += 1
        if bad:
            self.compact()

    def _apply(self, rec: dict) -> None:
        op = rec.get("op")
        path = rec.get("path")
        if not path:
            return
        e = self._entries.get(path)
        if op == "put":
            ts = rec.get("ts") or 0.0
            if e is None:
                order = rec.get("order")
                if order is None:
                    self._order += 1
                    order = self._order
                else:
                    self._order = max(self._order, int(order))
                e = {"path": path, "base_hash": rec.get("base_hash"), "order": order,
                     "first_ts": rec.get("first_ts", ts), "sent": list(rec.get("sent") or [])}
                self._entries[path] = e
            e["new_hash"] = rec.get("new_hash")
            e["size"] = rec.get("size", 0)
            e["mtime_ns"] = rec.get("mtime_ns", 0)
            e["last_ts"] = ts
            if e["new_hash"] == e["base_hash"]:
                del self._entries[path]
        elif op == "ack":
            if e is None:
                return
            if e.get("new_hash") == rec.get("pushed"):
                del self._entries[path]
            else:
                e["base_hash"] = rec.get("head")
                e["sent"] = []
                if e["new_hash"] == e["base_hash"]:
                    del self._entries[path]
        elif op == "sent":
            if e is not None and rec.get("hash") not in e["sent"]:
                e["sent"].append(rec.get("hash"))
        elif op == "rebase":
            if e is not None:
                e["base_hash"] = rec.get("base_hash")
                e["sent"] = []
                if e["new_hash"] == e["base_hash"]:
                    del self._entries[path]
        elif op == "drop":
            self._entries.pop(path, None)

    # -- journal writes -------------------------------------------------------

    def _append(self, recs: list[dict]) -> None:
        data = b"".join(json.dumps(r, ensure_ascii=False).encode("utf-8") + b"\n" for r in recs)
        with open(self.path, "ab") as fh:
            fh.write(data)
            fh.flush()
            if self.fsync:
                os.fsync(fh.fileno())
        for r in recs:
            self._apply(r)
        self._lines += len(recs)

    def add(self, path: str, base_hash: str | None, new_hash: str | None,
            size: int = 0, mtime_ns: int = 0) -> None:
        self.add_many([(path, base_hash, new_hash, size, mtime_ns)])

    def add_many(self, items) -> None:
        """Queue several changes with a single fsync."""
        now = self.clock()
        recs = [{"op": "put", "path": p, "base_hash": b, "new_hash": n, "size": s, "mtime_ns": m, "ts": now}
                for (p, b, n, s, m) in items]
        if recs:
            with self.lock:
                self._append(recs)

    def ack(self, path: str, pushed: str | None, head: str | None) -> None:
        with self.lock:
            self._append([{"op": "ack", "path": path, "pushed": pushed, "head": head}])
            self.maybe_compact()

    def mark_sent(self, entries: list[dict]) -> None:
        """Journal that these versions are about to be pushed (outcome unknown
        until acked).  After a crash or a lost response the first pull checks
        whether the server already has one of them."""
        recs = [{"op": "sent", "path": e["path"], "hash": e["new_hash"]} for e in entries]
        if recs:
            with self.lock:
                self._append(recs)

    def unconfirmed(self) -> bool:
        with self.lock:
            return any(e.get("sent") for e in self._entries.values())

    def rebase(self, path: str, base_hash: str | None) -> None:
        with self.lock:
            self._append([{"op": "rebase", "path": path, "base_hash": base_hash}])

    def drop(self, path: str) -> None:
        with self.lock:
            self._append([{"op": "drop", "path": path}])
            self.maybe_compact()

    # -- views --------------------------------------------------------------------

    def get(self, path: str) -> dict | None:
        with self.lock:
            e = self._entries.get(path)
            return dict(e) if e else None

    def entries(self) -> list[dict]:
        with self.lock:
            return [dict(e) for e in sorted(self._entries.values(), key=lambda e: e["order"])]

    def view(self) -> dict[str, dict]:
        with self.lock:
            return {p: dict(e) for p, e in self._entries.items()}

    def ready(self, debounce: float, now: float | None = None) -> list[dict]:
        """Entries whose last change is at least *debounce* seconds old, in push order."""
        now = self.clock() if now is None else now
        return [e for e in self.entries() if now - (e.get("last_ts") or 0) >= debounce]

    def __len__(self) -> int:
        with self.lock:
            return len(self._entries)

    def __contains__(self, path: str) -> bool:
        with self.lock:
            return path in self._entries

    def referenced_hashes(self) -> set[str]:
        with self.lock:
            return {e["new_hash"] for e in self._entries.values() if e.get("new_hash")}

    # -- compaction ------------------------------------------------------------------

    def maybe_compact(self) -> None:
        with self.lock:
            if self._lines >= COMPACT_MIN_LINES and self._lines > 4 * max(1, len(self._entries)):
                self.compact()

    def compact(self) -> None:
        """Rewrite the journal as one ``put`` per live entry (atomic replace)."""
        with self.lock:
            recs = [{"op": "put", "path": e["path"], "base_hash": e["base_hash"], "new_hash": e["new_hash"],
                     "size": e.get("size", 0), "mtime_ns": e.get("mtime_ns", 0), "ts": e.get("last_ts", 0),
                     "first_ts": e.get("first_ts", 0), "order": e["order"], "sent": e.get("sent") or []}
                    for e in sorted(self._entries.values(), key=lambda e: e["order"])]
            d = os.path.dirname(self.path) or "."
            tmp = self.path + ".compact"
            with open(tmp, "wb") as fh:
                for r in recs:
                    fh.write(json.dumps(r, ensure_ascii=False).encode("utf-8") + b"\n")
                fh.flush()
                if self.fsync:
                    os.fsync(fh.fileno())
            replace_retry(tmp, self.path)
            if self.fsync:
                fsync_dir(d)
            self._lines = len(recs)

