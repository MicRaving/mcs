"""Cache tiers of the mirror (PLAN §10, INTERFACES §4).

=========  ==============================================================  ==========
tier       content                                                         eviction
=========  ==============================================================  ==========
core       ``protocol.is_core`` (Project/, Codings/, Extraction/, ...)      never
pinned     every file of a pinned key: assigned keys + "take offline"      never
lru        any other downloaded file (PDFs, raw data, Results ...)         LRU, ``limit_mb``
supplement ``Supplements/`` - like lru, but never pre-loaded unless the    LRU
           ``supplements`` switch is on
=========  ==============================================================  ==========

A file with an unpushed change (outbox entry or a local-only edit) is never
evicted.  Eviction forgets the head **before** deleting the file, so the
scanner never sees "a head without its file" for an evicted file (which it
would otherwise report as a deletion); a crash between the two steps leaves a
file without a head, which the next scan re-adopts because its hash equals
the server version.

``state["cache"]``::

    {limit_mb, supplements: bool, assigned: [keys], atime: {path: epoch},
     evicted: {path: epoch}}
"""

from __future__ import annotations

import os
import time

from online_shared import protocol

MB = 1024 * 1024


class Cache:
    def __init__(self, mirror, outbox, clock=time.time):
        self.mirror = mirror
        self.outbox = outbox
        self.clock = clock
        with mirror.lock:
            c = mirror.state.setdefault("cache", {})
            c.setdefault("limit_mb", protocol.DEFAULT_CACHE_MB)
            c.setdefault("supplements", False)
            c.setdefault("assigned", [])
            c.setdefault("atime", {})
            c.setdefault("evicted", {})
            mirror.state.setdefault("pinned", [])

    @property
    def _c(self) -> dict:
        return self.mirror.state["cache"]

    # -- settings ----------------------------------------------------------------

    def settings(self) -> dict:
        with self.mirror.lock:
            return {"limit_mb": self._c["limit_mb"], "supplements": bool(self._c["supplements"]),
                    "pinned": sorted(self.mirror.state["pinned"]), "assigned": sorted(self._c["assigned"]),
                    "usage": self.usage()}

    def configure(self, limit_mb: float | None = None, supplements: bool | None = None) -> None:
        with self.mirror.lock:
            if limit_mb is not None:
                self._c["limit_mb"] = max(0, float(limit_mb))
            if supplements is not None:
                self._c["supplements"] = bool(supplements)

    def pin(self, keys) -> None:
        with self.mirror.lock:
            cur = self.mirror.state["pinned"]
            for k in keys:
                if k not in cur:
                    cur.append(k)

    def unpin(self, keys) -> None:
        with self.mirror.lock:
            self.mirror.state["pinned"] = [k for k in self.mirror.state["pinned"] if k not in set(keys)]

    def set_assigned(self, keys) -> None:
        with self.mirror.lock:
            self._c["assigned"] = sorted(set(keys))

    def pinned_keys(self) -> set[str]:
        with self.mirror.lock:
            return set(self.mirror.state["pinned"]) | set(self._c["assigned"])

    # -- tiers -------------------------------------------------------------------

    def tier(self, rel: str) -> str:
        fc = self.mirror.classify(rel)
        if protocol.is_core(rel, fc):
            return "core"
        if fc.key and fc.key in self.pinned_keys():
            return "pinned"
        if rel.startswith("Supplements/"):
            return "supplement"
        return "lru"

    def protected(self, rel: str) -> bool:
        if self.tier(rel) in ("core", "pinned"):
            return True
        if rel in self.outbox:
            return True
        with self.mirror.lock:
            return rel in self.mirror.state["local_only"]

    def touch(self, rel: str) -> None:
        with self.mirror.lock:
            self._c["atime"][rel] = self.clock()
            self._c["evicted"].pop(rel, None)

    def usage(self) -> dict:
        out = {"core": 0, "pinned": 0, "lru": 0, "supplement": 0}
        with self.mirror.lock:
            heads = dict(self.mirror.heads)
        for rel, h in heads.items():
            out[self.tier(rel)] += max(0, h.get("size") or 0)
        out["limit"] = int(self._c["limit_mb"] * MB)
        out["evictable"] = out["lru"] + out["supplement"]
        return out

    # -- eviction ------------------------------------------------------------------

    def evictable(self) -> list[tuple[float, str, int]]:
        """``[(atime, path, size)]`` of evictable downloaded files, oldest first."""
        with self.mirror.lock:
            heads = dict(self.mirror.heads)
            atime = dict(self._c["atime"])
        rows = []
        for rel, h in heads.items():
            if self.protected(rel):
                continue
            rows.append((atime.get(rel, 0.0), rel, max(0, h.get("size") or 0)))
        rows.sort()
        return rows

    def evict(self, rel: str) -> bool:
        """Evict one downloaded file; refuses protected or locally modified files."""
        if self.protected(rel):
            return False
        if self.mirror.local_state(rel) == "modified":
            return False
        with self.mirror.lock:
            self.mirror.heads.pop(rel, None)
            self._c["atime"].pop(rel, None)
            self._c["evicted"][rel] = self.clock()
            self.mirror.save()
        try:
            os.unlink(self.mirror.abspath(rel))
        except FileNotFoundError:
            pass
        return True

    def enforce(self, limit_bytes: int | None = None) -> list[str]:
        """Evict least-recently-used files until the evictable tiers fit the limit."""
        limit = int(self._c["limit_mb"] * MB) if limit_bytes is None else limit_bytes
        rows = self.evictable()
        total = sum(r[2] for r in rows)
        evicted = []
        for _atime, rel, size in rows:
            if total <= limit:
                break
            if self.evict(rel):
                evicted.append(rel)
                total -= size
        return evicted

    def clear(self) -> list[str]:
        return self.enforce(0)

    def preload_candidates(self, keys=None) -> list[str]:
        """Server files of *keys* (default: the pinned keys) that are not local yet."""
        keys = set(keys) if keys is not None else self.pinned_keys()
        with self.mirror.lock:
            remote = dict(self.mirror.remote)
            heads = self.mirror.heads
            supplements = bool(self._c["supplements"])
            out = []
            for rel, r in remote.items():
                if r.get("deleted"):
                    continue
                fc = self.mirror.classify(rel)
                if fc.kind == "excluded" or fc.key not in keys:
                    continue
                if rel.startswith("Supplements/") and not supplements:
                    continue
                h = heads.get(rel)
                if h and h["hash"] == r.get("hash"):
                    continue
                out.append(rel)
        return sorted(out)
