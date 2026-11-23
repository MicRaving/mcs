"""In-memory studio server implementing the contract (INTERFACES §3) closely
enough to test the client: seq counter, files/heads, blobs, push rules
(base-hash check, server-side merge via ``online_shared.merge.merge3``,
conflicts), locks with expiry, visibility incl. blinding/done marks, manifest
``since`` + ``visibility_epoch``.

Two deliberate server behaviours the client relies on (reported as contract
clarifications):

* a push entry whose ``new_hash`` already **is** the head is ``accepted``
  without a new seq (idempotent re-push after a client crash);
* a merge whose result equals the head is ``merged`` without a new seq.

Each client gets its own :class:`FakeTransport`, which can be switched
offline (raises ``ConnectionError`` like a refused connection) and carries a
``hook(method, path, phase)`` for fault injection (phase ``"before"`` /
``"after"`` the server handled the request).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import time
import urllib.parse
from dataclasses import dataclass

from online_shared import merge as merge_mod
from online_shared import protocol

from online.client import Response


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class HTTPFail(Exception):
    def __init__(self, status: int, code: str, message: str = "", extra: dict | None = None):
        super().__init__(message or code)
        self.status, self.code, self.message, self.extra = status, code, message or code, extra or {}


@dataclass
class Member:
    role: str
    tags: list
    tasks: list


class FakeServer:
    def __init__(self):
        self.users: dict[str, dict] = {}       # username -> {id, password, display_name, is_admin}
        self.tokens: dict[str, str] = {}       # token -> username
        self.projects: dict[str, dict] = {}
        self.blobs: dict[str, bytes] = {}
        self.clock_offset = 0.0
        self.requests: list[tuple[str, str, str]] = []
        # strict=True: the two clarifications from the module docstring plus a
        # head ``hash`` on conflict/rejected results.  strict=False behaves like
        # the contract text read literally (and like Server/studio_server today):
        # a re-push of the head goes through merge3 and gets a new seq, and
        # conflict/rejected results carry no hash.
        self.strict = True
        self._uid = 0
        self._tok = 0

    # -- setup helpers ------------------------------------------------------------

    def now(self) -> float:
        return time.time() + self.clock_offset

    def add_user(self, username: str, password: str = "pw", is_admin: bool = False) -> int:
        self._uid += 1
        self.users[username] = {"id": self._uid, "username": username, "password": password,
                                "display_name": username.title(), "is_admin": is_admin}
        return self._uid

    def create_project(self, name: str = "P", pid: str = "proj-1", blinding: str = "blinded") -> str:
        self.projects[pid] = {
            "id": pid, "name": name,
            "settings": {"blinding": {s: blinding for s in protocol.STAGES},
                         "lock_ttl_h": 24, "max_offline_lease_h": 72},
            "members": {}, "files": {}, "changes": [], "seq": 0, "conflicts": [], "locks": {},
            "done": {}, "presence": {}, "assignments": [],
        }
        return pid

    def add_member(self, pid: str, username: str, role: str = "coder", tags=(), tasks=()) -> None:
        self.projects[pid]["members"][username] = Member(role, list(tags), list(tasks))

    def put_file(self, pid: str, path: str, data: bytes | None, by: str = "admin") -> int:
        """Server-side write (admin publish); returns the new seq."""
        p = self.projects[pid]
        h = None
        if data is not None:
            h = _sha(data)
            self.blobs[h] = data
        return self._apply(p, path, p["files"].get(path, {}).get("hash"), h, len(data or b""), by, "admin")

    def file_bytes(self, pid: str, path: str) -> bytes | None:
        f = self.projects[pid]["files"].get(path)
        if not f or f["deleted"]:
            return None
        return self.blobs[f["hash"]]

    def seq(self, pid: str) -> int:
        return self.projects[pid]["seq"]

    def changes_for(self, pid: str, path: str) -> list[dict]:
        return [c for c in self.projects[pid]["changes"] if c["path"] == path]

    # -- transport entry point ------------------------------------------------------

    def handle(self, method: str, path: str, query: dict, headers: dict, body: bytes | None,
               stream_to=None) -> Response:
        hdrs = {k.lower(): v for k, v in (headers or {}).items()}
        user = self.tokens.get((hdrs.get("authorization") or "").replace("Bearer ", "", 1))
        self.requests.append((user or "-", method, path))
        try:
            if path != "/api/health" and hdrs.get("x-studio-protocol") != str(protocol.PROTOCOL_VERSION):
                raise HTTPFail(426, "protocol_mismatch")
            if body and hdrs.get("content-encoding") == "gzip":
                body = gzip.decompress(body)
            status, payload = self._route(method, path, query or {}, hdrs, body, user)
        except HTTPFail as f:
            data = {"detail": f.code, "message": f.message}
            data.update(f.extra)
            return Response(f.status, {"content-type": "application/json"}, json.dumps(data).encode())
        if isinstance(payload, bytes):
            if stream_to is not None:
                stream_to.write(payload)
                return Response(status, {"content-type": "application/octet-stream"}, b"")
            return Response(status, {"content-type": "application/octet-stream"}, payload)
        return Response(status, {"content-type": "application/json"}, json.dumps(payload).encode())

    # -- permissions ------------------------------------------------------------------

    def _member(self, pid: str, user: str | None) -> Member:
        if user is None:
            raise HTTPFail(401, "unauthorized")
        p = self.projects.get(pid)
        if p is None:
            raise HTTPFail(404, "not_found")
        if self.users[user]["is_admin"]:
            return Member("owner", list(p["members"].get(user, Member("owner", [], [])).tags), list(protocol.TASKS))
        m = p["members"].get(user)
        if m is None:
            raise HTTPFail(403, "forbidden")
        return m

    def _done(self, p, tags, key, stage) -> bool:
        return any((t, key, stage) in p["done"] for t in tags)

    def can_read(self, p, m: Member, fc) -> bool:
        if fc.kind == "excluded":
            return False
        if m.role == "owner":
            return True
        if fc.kind == "owned":
            if fc.tag in m.tags:
                return True
            if p["settings"]["blinding"].get(fc.stage) == "visible":
                return True
            if fc.stage == "coding" and fc.key:
                return self._done(p, m.tags, fc.key, "coding")
            return False
        return True

    def _lock_ok(self, p, user, key) -> bool:
        lk = p["locks"].get(key)
        return lk is None or lk["user"] == user or lk["expires"] <= self.now()

    def can_write(self, p, m: Member, user, fc) -> tuple[bool, str]:
        if fc.kind == "excluded":
            return False, "excluded"
        if m.role == "owner":
            if fc.kind == "shared" and not self._lock_ok(p, user, fc.key):
                return False, "locked"
            return True, ""
        if fc.kind in ("derived", "admin", "immutable"):
            return False, "forbidden"
        if fc.kind == "owned":
            return (fc.tag in m.tags), "forbidden"
        if fc.kind == "shared":
            if fc.task not in m.tasks:
                return False, "forbidden"
            if not self._lock_ok(p, user, fc.key):
                return False, "locked"
            if fc.task == "settle" and not self._done(p, m.tags, fc.key, "coding"):
                return False, "forbidden"
            return True, ""
        return False, "forbidden"

    def _epoch(self, p, user, m: Member) -> str:
        rows = [(t, k, s, ts) for (t, k, s), ts in p["done"].items() if t in m.tags]
        blob = json.dumps([m.role, sorted(m.tags), sorted(m.tasks), p["settings"]["blinding"],
                           len(rows), max((r[3] for r in rows), default=0)], sort_keys=True)
        return _sha(blob.encode())[:16]

    # -- writes -----------------------------------------------------------------------

    def _apply(self, p, path, base, new, size, user, kind) -> int:
        p["seq"] += 1
        fc = protocol.classify(path)
        p["files"][path] = {"path": path, "hash": new, "size": size if new else 0, "seq": p["seq"],
                            "deleted": new is None, "kind": fc.kind, "key": fc.key, "tag": fc.tag,
                            "updated_by": user}
        p["changes"].append({"seq": p["seq"], "path": path, "base_hash": base, "new_hash": new,
                             "user": user, "kind": kind})
        return p["seq"]

    def _head(self, p, path):
        f = p["files"].get(path)
        return None if (f is None or f["deleted"]) else f

    def _push_entry(self, p, m, user, client_id, e) -> dict:
        path = protocol.normalize_path(e["path"])
        base, new = e.get("base_hash"), e.get("new_hash")
        fc = protocol.classify(path)
        ok, reason = self.can_write(p, m, user, fc)
        if not ok:
            r = {"path": path, "status": "rejected", "reason": reason}
            if self.strict:
                r["hash"] = (self._head(p, path) or {}).get("hash")
            return r
        if new is not None and new not in self.blobs:
            return {"path": path, "status": "rejected", "reason": "missing_blob"}
        head = self._head(p, path)
        head_hash = head["hash"] if head else None
        if self.strict and new == head_hash:                     # idempotent
            return {"path": path, "status": "accepted", "seq": head["seq"] if head else p["seq"], "hash": new}
        if base == head_hash:
            seq = self._apply(p, path, base, new, len(self.blobs[new]) if new else 0, user, "push")
            if fc.kind == "shared" and fc.key in p["locks"] and p["locks"][fc.key]["user"] == user:
                p["locks"][fc.key]["expires"] = self.now() + p["settings"]["lock_ttl_h"] * 3600
            return {"path": path, "status": "accepted", "seq": seq, "hash": new}
        if base and head_hash and new and base in self.blobs:
            res = merge_mod.merge3(path, self.blobs[base], self.blobs[head_hash], self.blobs[new])
            if res.ok:
                mh = _sha(res.merged)
                self.blobs[mh] = res.merged
                if self.strict and mh == head_hash:
                    return {"path": path, "status": "merged", "seq": head["seq"], "hash": mh}
                seq = self._apply(p, path, base, mh, len(res.merged), user, "merge")
                return {"path": path, "status": "merged", "seq": seq, "hash": mh}
        cid = len(p["conflicts"]) + 1
        p["conflicts"].append({"id": cid, "path": path, "base_hash": base, "server_hash": head_hash,
                               "client_hash": new, "user": user, "client_id": client_id,
                               "state": "open", "resolution": None})
        r = {"path": path, "status": "conflict", "conflict_id": cid}
        if self.strict:
            r["hash"] = head_hash
        return r

    # -- router -------------------------------------------------------------------------

    def _route(self, method, path, query, hdrs, body, user):
        js = json.loads(body.decode()) if body and hdrs.get("content-type") == "application/json" else None
        if path == "/api/health":
            return 200, {"ok": True, "version": "fake", "protocol": 1}
        if path == "/api/auth/login" and method == "POST":
            u = self.users.get(js["username"])
            if not u or u["password"] != js["password"]:
                raise HTTPFail(401, "unauthorized")
            self._tok += 1
            tok = f"tok-{self._tok}-{js['username']}"
            self.tokens[tok] = js["username"]
            return 200, {"token": tok, "user": {"id": u["id"], "username": u["username"]}}
        if user is None:
            raise HTTPFail(401, "unauthorized")
        if path == "/api/auth/me":
            return 200, {"user": {"id": self.users[user]["id"], "username": user},
                         "projects": self._projects_of(user)}
        if path == "/api/auth/logout":
            return 200, {"ok": True}
        if path == "/api/projects":
            return 200, {"projects": self._projects_of(user)}
        mt = re.match(r"^/api/p/([^/]+)(/.*)?$", path)
        if not mt:
            raise HTTPFail(404, "not_found")
        pid = urllib.parse.unquote(mt.group(1))
        rest = mt.group(2) or ""
        m = self._member(pid, user)
        p = self.projects[pid]

        if rest == "" and method == "GET":
            return 200, {"id": pid, "name": p["name"], "settings": p["settings"],
                         "me": {"role": m.role, "tags": m.tags, "tasks": m.tasks}, "protocol": 1}
        if rest == "/manifest":
            since = int(query.get("since", 0))
            files = []
            for f in p["files"].values():
                if f["seq"] > since and self.can_read(p, m, protocol.classify(f["path"])):
                    files.append(dict(f))
            files.sort(key=lambda f: f["seq"])
            return 200, {"seq": p["seq"], "visibility_epoch": self._epoch(p, user, m), "files": files}
        if rest == "/blobs/missing":
            return 200, {"missing": [h for h in js["hashes"] if h not in self.blobs]}
        mb = re.match(r"^/blobs/([0-9a-f]{64})$", rest)
        if mb:
            h = mb.group(1)
            if method == "PUT":
                if _sha(body or b"") != h:
                    raise HTTPFail(400, "bad_request", "hash mismatch")
                self.blobs[h] = body or b""
                return 200, {"ok": True}
            if h not in self.blobs:
                raise HTTPFail(404, "not_found")
            readable = any(c["new_hash"] == h and self.can_read(p, m, protocol.classify(c["path"]))
                           for c in p["changes"]) or any(c["client_hash"] == h for c in p["conflicts"])
            if not readable:
                raise HTTPFail(403, "forbidden")
            return 200, self.blobs[h]
        if rest == "/push":
            results = [self._push_entry(p, m, user, js.get("client_id"), e) for e in js["entries"]]
            return 200, {"seq": p["seq"], "results": results}
        if rest == "/conflicts" and method == "GET":
            st = query.get("state")
            rows = [dict(c) for c in p["conflicts"] if (st is None or c["state"] == st)
                    and (m.role == "owner" or c["user"] == user)]
            return 200, {"conflicts": rows}
        mc = re.match(r"^/conflicts/(\d+)/resolve$", rest)
        if mc:
            c = p["conflicts"][int(mc.group(1)) - 1]
            if c["state"] != "open":
                raise HTTPFail(409, "conflict", "already resolved")
            res = js["resolution"]
            out = {"ok": True}
            if res in ("mine", "merged"):
                new = js.get("new_hash")
                if new is not None and new not in self.blobs:
                    raise HTTPFail(400, "bad_request", "missing blob")
                head = self._head(p, c["path"])
                out["seq"] = self._apply(p, c["path"], head["hash"] if head else None, new,
                                         len(self.blobs[new]) if new else 0, user, "resolve")
                out["hash"] = new
            c["state"], c["resolution"] = "resolved", res
            return 200, out
        if rest == "/locks" and method == "GET":
            return 200, [self._lock_row(k, lk) for k, lk in p["locks"].items()]
        ml = re.match(r"^/locks/(.+)$", rest)
        if ml:
            key = urllib.parse.unquote(ml.group(1))
            lk = p["locks"].get(key)
            if method == "POST":
                if m.role != "owner" and not set(m.tasks) & {"settle", "harmonize", "reproduce"}:
                    raise HTTPFail(403, "forbidden")
                if lk and lk["user"] != user and lk["expires"] > self.now():
                    raise HTTPFail(409, "locked", "locked by another user",
                                   {"holder": self._lock_row(key, lk)})
                hours = p["settings"]["lock_ttl_h"]
                offline = bool(js and js.get("offline_hours"))
                if offline:
                    hours = min(js["offline_hours"], p["settings"]["max_offline_lease_h"])
                now = self.now()
                p["locks"][key] = {"user": user, "client_id": (js or {}).get("client_id"),
                                   "acquired": now, "renewed": now, "expires": now + hours * 3600,
                                   "offline": offline}
                return 200, self._lock_row(key, p["locks"][key])
            if method == "DELETE":
                force = str(query.get("force", "")) == "1"
                if lk and lk["user"] != user and not (force and m.role == "owner"):
                    raise HTTPFail(403, "forbidden")
                p["locks"].pop(key, None)
                return 200, {"ok": True}
        if rest == "/presence" and method == "POST":
            p["presence"][js["client_id"]] = {"user": user, "key": js.get("key"), "tab": js.get("tab"),
                                              "ts": self.now()}
            others = [{"user": v["user"], "display_name": v["user"].title(), "key": v["key"], "tab": v["tab"],
                       "ts": v["ts"]} for c, v in p["presence"].items()
                      if c != js["client_id"] and self.now() - v["ts"] < protocol.PRESENCE_TTL_SECS]
            return 200, {"others": others}
        if rest.startswith("/presence/") and method == "DELETE":
            p["presence"].pop(urllib.parse.unquote(rest[len("/presence/"):]), None)
            return 200, {"ok": True}
        if rest == "/done":
            if method == "POST":
                if js["tag"] not in m.tags and m.role != "owner":
                    raise HTTPFail(403, "forbidden")
                p["done"][(js["tag"], js["key"], js["stage"])] = self.now()
                return 200, {"ok": True}
            if method == "GET":
                return 200, {"done": [{"tag": t, "key": k, "stage": s} for (t, k, s) in p["done"]]}
            if method == "DELETE":
                if m.role != "owner":
                    raise HTTPFail(403, "forbidden")
                p["done"].pop((js["tag"], js["key"], js["stage"]), None)
                return 200, {"ok": True}
        if rest == "/assignments":
            rows = [a for a in p["assignments"]
                    if (not query.get("stage") or a["stage"] == query["stage"])
                    and (not query.get("tag") or a["tag"] == query["tag"])]
            return 200, rows
        raise HTTPFail(404, "not_found")

    def _projects_of(self, user):
        out = []
        for pid, p in self.projects.items():
            if user in p["members"] or self.users[user]["is_admin"]:
                role = "owner" if self.users[user]["is_admin"] else p["members"][user].role
                out.append({"id": pid, "name": p["name"], "role": role})
        return out

    def _lock_row(self, key, lk):
        return {"key": key, "user": lk["user"], "display_name": lk["user"].title(),
                "client_id": lk["client_id"], "acquired": lk["acquired"], "renewed": lk["renewed"],
                "expires": lk["expires"], "offline": lk["offline"], "expired": lk["expires"] <= self.now()}


class FakeTransport:
    """One client's connection to the :class:`FakeServer`."""

    def __init__(self, server: FakeServer):
        self.server = server
        self.offline = False
        self.hook = None             # hook(method, path, phase) may raise

    def request(self, method, path, *, query=None, headers=None, body=None, stream_to=None, timeout=None):
        if self.offline:
            raise ConnectionRefusedError("fake: offline")
        if self.hook:
            self.hook(method, path, "before")
        resp = self.server.handle(method, path, query or {}, headers or {}, body, stream_to)
        if self.hook:
            self.hook(method, path, "after")
        return resp
