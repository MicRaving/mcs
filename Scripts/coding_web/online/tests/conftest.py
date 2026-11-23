"""Fixtures: a fake server with a small project and client "nodes".

A node is one desktop client: its own mirror directory (under ``tmp_path``),
its own :class:`FakeTransport` (which can be switched offline or given a
fault-injection hook) and an :class:`OnlineStore` built with
``autostart=False`` and ``debounce=0`` so tests drive ``tick()`` themselves.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CODING_WEB = os.path.abspath(os.path.join(_HERE, "..", ".."))
_SCRIPTS = os.path.abspath(os.path.join(_CODING_WEB, ".."))
for _p in (_SCRIPTS, _CODING_WEB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from online.store import OnlineStore  # noqa: E402
from online.tests.fake_server import FakeServer, FakeTransport  # noqa: E402

SERVER_URL = "http://studio.test:8770"
PID = "proj-1"


class Crash(BaseException):
    """Simulated process death (not an Exception, so no handler swallows it)."""


class Clock:
    def __init__(self, start: float = 1_000_000.0):
        self.t = start

    def __call__(self) -> float:
        self.t += 0.001
        return self.t

    def advance(self, secs: float) -> None:
        self.t += secs


class Node:
    def __init__(self, server: FakeServer, base: str, username: str, password: str = "pw",
                 fsync: bool = True, clock=None):
        self.server = server
        self.base = base
        self.username = username
        self.password = password
        self.fsync = fsync
        self.clock = clock or Clock()
        self.transport = FakeTransport(server)
        self.store = self._new_store()
        self.root = self.store.open(PID, start=False)

    def _new_store(self) -> OnlineStore:
        st = OnlineStore(base_dir=self.base, transport_factory=lambda _s: self.transport, debounce=0,
                         autostart=False, use_keyring=False, fsync=self.fsync, clock=self.clock)
        st.connect(SERVER_URL, self.username, self.password)
        return st

    def restart(self) -> None:
        """Simulate a crash + restart: abandon the objects, rebuild from disk."""
        self.transport.hook = None
        self.store = self._new_store()
        self.root = self.store.open(PID, start=False)

    @property
    def syncer(self):
        return self.store.syncer

    @property
    def mirror(self):
        return self.store.mirror

    def tick(self) -> bool:
        return self.syncer.tick(force=True)

    def path(self, rel: str) -> str:
        return os.path.join(self.root, *rel.split("/"))

    def write(self, rel: str, data) -> None:
        if isinstance(data, (dict, list)):
            data = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
        elif isinstance(data, str):
            data = data.encode()
        full = self.path(rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        old = None
        try:
            old = os.stat(full).st_mtime_ns
        except FileNotFoundError:
            pass
        with open(full, "wb") as fh:
            fh.write(data)
        if old is not None and os.stat(full).st_mtime_ns <= old:   # coarse clocks: force a new stamp
            os.utime(full, ns=(old + 1_000_000, old + 1_000_000))

    def read(self, rel: str) -> bytes | None:
        try:
            with open(self.path(rel), "rb") as fh:
                return fh.read()
        except FileNotFoundError:
            return None

    def read_json(self, rel: str):
        data = self.read(rel)
        return None if data is None else json.loads(data)

    def exists(self, rel: str) -> bool:
        return os.path.exists(self.path(rel))


def jbytes(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode()


@pytest.fixture(params=["strict", "literal"])
def server(request):
    """Every scenario runs against both server flavours (see FakeServer.strict)."""
    s = FakeServer()
    s.strict = request.param == "strict"
    s.add_user("own")
    s.add_user("a")
    s.add_user("b")
    s.create_project("Demo", PID)
    s.add_member(PID, "own", "owner", ["O"], [])
    s.add_member(PID, "a", "coder", ["A"], ["coding", "settle"])
    s.add_member(PID, "b", "coder", ["B"], ["coding", "settle"])
    s.put_file(PID, "Project/project.json", jbytes({"scheme": "x"}))
    s.put_file(PID, "Text/K1.txt", b"paper text K1\n")
    return s


@pytest.fixture
def make_node(server, tmp_path):
    nodes = []

    def _make(username: str, **kw) -> Node:
        n = Node(server, str(tmp_path / f"client-{username}-{len(nodes)}"), username, **kw)
        nodes.append(n)
        return n

    yield _make
    for n in nodes:
        try:
            n.store.close()
        except Exception:
            pass
