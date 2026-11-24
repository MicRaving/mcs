"""End-to-end test of the online-project feature against the REAL services.

Nothing here is faked: a real ``studio_server`` process on a free port with a
throw-away data directory, real accounts, and two real app instances - the
Flask app from ``Scripts/coding_web/app.py``:

* the **owner** runs in this process (``app.test_client()``).  ``app.py`` builds
  one module-level ``app``/``S`` pair, so only one instance can live in a
  process;
* the **coder** runs as a second app process on another port (the same
  ``app.py`` module, served by ``werkzeug.make_server``), driven over HTTP.

The scenario (one linear story; each test is one step and fails on its own):

 01  owner connects, opens, publishes a synthetic project (papers, PDFs, Text,
     Extraction, a coding file per tag, Screening per tag, a Datasets folder with
     raw data + harmonizer) -> progress + admin shapes
 02  coder connects + opens: sees every paper, PDFs stay in the server cache
     until the paper is opened
 03  blinding: the other tag's coding file is invisible; a save with another
     tag is refused (403)
 04  coder finishes paper K -> its done mark lifts the blinding for K
 05  owner runs the automatic assignment (pairs, 40 % overlap); the coder's
     assignment list matches the plan
 06  admin passthroughs: manual assignment, run list + undo, members, users,
     settings, audit, publish progress, coder sees 403
 07  coder saves its coding file -> owner pulls it
 08  both edit disjoint fields of Extraction/K.json -> merged
 09  both edit the same field -> conflict listed on the coder, resolved
 10  coder takes the lock -> owner's attempt is 409 with the holder, force
     release; `mine` comes from the server (username/user_id), not a name
 11  server goes away: coder finishes paper K2 and edits a file -> pending > 0,
     status offline, done list answers from the queue
 12  server returns: one sync lands both changes, pending 0

Run with::

    $env:STUDIO_E2E="1"; python -m pytest Scripts/coding_web/online/tests/e2e_real_server.py -q -m e2e
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("STUDIO_E2E") != "1",
                       reason="set STUDIO_E2E=1 to run against the real studio server"),
]

HERE = os.path.dirname(os.path.abspath(__file__))
CODING_WEB = os.path.abspath(os.path.join(HERE, "..", ".."))          # Scripts/coding_web
SCRIPTS = os.path.abspath(os.path.join(CODING_WEB, ".."))              # Scripts
REPO = os.path.abspath(os.path.join(SCRIPTS, ".."))                    # repo root
SERVER_DIR = os.path.join(REPO, "Server")
SCRATCH_ROOT = os.path.join(REPO, "temp", "online", "e2e")

for _p in (SCRIPTS, CODING_WEB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from online.mirror import mirror_dir  # noqa: E402

PDF = b"%PDF-1.4\n% synthetic test paper\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
OWNER = ("owner", "ownerpass123")
CODER = ("anna", "annapass123")
OTHER = ("bo", "bopass123")
CODER_TAG = "AN"
OTHER_TAG = "BO"
KEYS = ["Alpha et al. (2020)", "Beta et al. (2021)", "Gamma et al. (2022)",
        "Delta et al. (2023)", "Epsilon et al. (2024)"]
KEY = KEYS[0]          # the paper finished early (done marks, Extraction edits, lock)
KEY2 = KEYS[1]         # the paper finished while the server is down
PATH_TIMEOUT = 20.0


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(fn, timeout: float = PATH_TIMEOUT, interval: float = 0.1, what: str = ""):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            value = fn()
            if value:
                return value
        except Exception as exc:                       # noqa: BLE001 - poll
            last = exc
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what or fn}{': ' + repr(last) if last else ''}")


class Http:
    """A tiny JSON client for one app instance or the server."""

    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def request(self, method: str, path: str, body=None, token: str | None = None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"X-Studio-Protocol": "1", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(self.base + path, method=method, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, self._decode(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, self._decode(exc.read())

    @staticmethod
    def _decode(raw: bytes):
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:                              # an HTML 404 from abort()
            return {"raw": raw.decode("utf-8", "replace")[:400]}

    def ok(self, method: str, path: str, body=None, token: str | None = None):
        status, data = self.request(method, path, body=body, token=token)
        assert 200 <= status < 300, f"{method} {path} -> {status}: {data}"
        return data

    def raw(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=60) as resp:
            return resp.status, resp.read()


class Server:
    """The real ``python -m studio_server`` process."""

    def __init__(self, data_dir: str, log_path: str):
        self.data_dir = data_dir
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None
        self.port = 0

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _env(self):
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env.pop("STUDIO_DATA_DIR", None)
        return env

    def create_admin(self, username: str, password: str) -> None:
        subprocess.run([sys.executable, "-m", "studio_server", "create-admin",
                        "--username", username, "--password", password,
                        "--data", self.data_dir],
                       cwd=SERVER_DIR, env=self._env(), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def start(self, port: int | None = None) -> "Server":
        self.port = int(port or self.port or free_port())
        self.log = open(self.log_path, "ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "studio_server", "serve", "--host", "127.0.0.1",
             "--port", str(self.port), "--data", self.data_dir],
            cwd=SERVER_DIR, env=self._env(), stdout=self.log, stderr=self.log)
        wait_until(lambda: self.health(), what=f"studio_server on {self.base}")
        return self

    def health(self) -> bool:
        try:
            with urllib.request.urlopen(self.base + "/api/health", timeout=2) as resp:
                return resp.status == 200
        except Exception:                              # noqa: BLE001 - not up yet
            return False

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)
            self.proc = None
        log = getattr(self, "log", None)
        if log is not None:
            try:
                log.close()
            except OSError:
                pass
            self.log = None

    def wait_down(self) -> None:
        def gone():
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.3):
                    return False
            except OSError:
                return True
        wait_until(gone, what=f"port {self.port} to close")


class AppInstance:
    """A real Coding Studio app process (``Scripts/coding_web/app.py``)."""

    LAUNCHER = (
        "import os\n"
        "import app as app_module\n"
        "from studio_state import configure, refresh_keys\n"
        "configure(os.environ['E2E_ROOT'])\n"
        "refresh_keys()\n"
        "from werkzeug.serving import make_server\n"
        "srv = make_server('127.0.0.1', int(os.environ['E2E_PORT']), app_module.app, threaded=True)\n"
        "print('ready', flush=True)\n"
        "srv.serve_forever()\n"
    )

    def __init__(self, root: str, mirror_base: str, log_path: str):
        self.root = root
        self.mirror_base = mirror_base
        self.log_path = log_path
        self.port = free_port()
        self.proc: subprocess.Popen | None = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "AppInstance":
        os.makedirs(self.root, exist_ok=True)
        for sub in ("Papers", "Text", "Extraction", "Codings"):
            os.makedirs(os.path.join(self.root, sub), exist_ok=True)
        env = dict(os.environ)
        env.update({
            "E2E_ROOT": self.root,
            "E2E_PORT": str(self.port),
            "PYTHONUNBUFFERED": "1",
            "CODINGSTUDIO_ONLINE_DIR": self.mirror_base,
            "CODINGSTUDIO_NO_KEYRING": "1",
        })
        self.log = open(self.log_path, "ab")
        self.proc = subprocess.Popen([sys.executable, "-c", self.LAUNCHER],
                                     cwd=CODING_WEB, env=env,
                                     stdout=self.log, stderr=self.log)
        http = Http(self.base)
        wait_until(lambda: http.request("GET", "/api/online/status")[0] == 200,
                   timeout=60, what=f"app instance on {self.base}")
        return self

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)
            self.proc = None
        log = getattr(self, "log", None)
        if log is not None:
            try:
                log.close()
            except OSError:
                pass
            self.log = None


def write(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(data, (dict, list)):
        data = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    elif isinstance(data, str):
        data = data.encode("utf-8")
    with open(path, "wb") as fh:
        fh.write(data)


def read_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def record(key: str, **over) -> dict:
    rec = {
        "Study_ID": key.split(" ")[0].upper() + "2020",
        "Title": key,
        "Authors": key.split(" ")[0] + ", A.",
        "Year": 2020,
        "N_Analyzed": 412,
        "Setting": "online",
        "Intervention_Type": ["accuracy prompt"],
        "DV_Metrics": [{
            "dv_name": "sharing_intention_false", "dv_domain": "sharing",
            "dv_polarity": "higher_is_worse", "n1i": 200, "n2i": 212,
            "m1i": 2.31, "m2i": 2.98, "sd1i": 1.05, "sd2i": 1.11,
            "contrast_type": "posttest", "eligible_primary": 1,
        }],
    }
    rec.update(over)
    return rec


def write_records(root: str) -> None:
    """The decision-free records table a blinded coder screens from.

    ``Project/master_records.csv.gz`` carries only the bibliographic columns the
    screening index reads (``compile_master`` writes it in a real project); the
    master workbook stays the owner's.
    """
    import csv
    import gzip

    path = os.path.join(root, "Project", "master_records.csv.gz")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["an", "title", "abstract", "authors", "year", "doi", "source"])
        for i, key in enumerate(KEYS, start=1):
            writer.writerow([f"E2E{i:03d}", key, f"Abstract of {key}. More text.",
                             "Doe, J.", 2020, f"10.1000/e2e{i}", "Journal E2E"])


def build_source(root: str) -> None:
    """The synthetic project the owner publishes (§ the scenario list)."""
    for i, key in enumerate(KEYS):
        write(os.path.join(root, "Text", key + ".txt"), f"Full text of {key}.\nSecond paragraph.\n")
        write(os.path.join(root, "Papers", key + ".pdf"), PDF + f"% {key}\n".encode())
        write(os.path.join(root, "Extraction", key + ".json"),
              [record(key, N_Analyzed=400 + i)])
        write(os.path.join(root, "Codings", f"{key} - {CODER_TAG}.json"),
              [record(key, Coder=CODER_TAG, N_Analyzed=400 + i)])
        write(os.path.join(root, "Codings", f"{key} - {OTHER_TAG}.json"),
              [record(key, Coder=OTHER_TAG, N_Analyzed=400 + i)])
        write(os.path.join(root, "Screening", f"{CODER_TAG}.json"),
              {"coder": CODER_TAG, "decisions": {key: "include"}})
        write(os.path.join(root, "Screening", f"{OTHER_TAG}.json"),
              {"coder": OTHER_TAG, "decisions": {key: "include"}})
        folder = os.path.join(root, "Datasets", key)
        write(os.path.join(folder, f"{key} - raw.csv"), "pid,arm,dv\n1,treat,0.5\n2,ctrl,0.4\n")
        write(os.path.join(folder, "harmonizer.py"),
              "# synthetic harmonizer\nprint('no-op')\n")
    write(os.path.join(root, "Project", "project.json"), {"scheme": "synthetic", "name": "E2E"})
    write_records(root)
    # a derived consensus file: readable by the owner, never by a blinded coder
    write(os.path.join(root, "master_dataset.xlsx"), "PK\u0003\u0004 synthetic master\n")


# ---------------------------------------------------------------------------
# the world: one real server, two real app instances
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def world():
    os.makedirs(SCRATCH_ROOT, exist_ok=True)
    scratch = tempfile.mkdtemp(prefix="run-", dir=SCRATCH_ROOT)
    data_dir = os.path.join(scratch, "server-data")
    source = os.path.join(scratch, "source-project")
    build_source(source)

    server = Server(data_dir, os.path.join(scratch, "server.log"))
    coder_app = AppInstance(os.path.join(scratch, "coder-local"),
                            os.path.join(scratch, "mirrors", "coder"),
                            os.path.join(scratch, "coder-app.log"))

    # --- owner side: the shipping Flask app in this process -----------------
    import app as app_module
    import routes_online
    from online.store import OnlineStore
    from studio_state import S

    previous_root = S.ROOT
    app_module.app.config["TESTING"] = True
    owner_store = OnlineStore(base_dir=os.path.join(scratch, "mirrors", "owner"),
                              use_keyring=False, autostart=False, debounce=0)
    routes_online.set_store(owner_store)
    client = app_module.app.test_client()

    class World:
        pass

    w = World()
    w.scratch = scratch
    w.source = source
    w.server = server
    w.owner_client = client
    w.coder_app = coder_app
    w.coder = Http(coder_app.base)
    w.owner_store = owner_store
    w.project_id = None
    w.owner_mirror = None
    w.coder_mirror = lambda: mirror_dir(server.base, w.project_id, coder_app.mirror_base)
    w.plan = None
    w.run_id = None
    w.coder_records = {}

    try:
        server.create_admin(*OWNER)
        server.start()
        w.base_url = server.base
        http = Http(server.base)
        token = http.ok("POST", "/api/auth/login",
                        {"username": OWNER[0], "password": OWNER[1]})["token"]
        for username, password, display in ((CODER[0], CODER[1], "Anna"),
                                            (OTHER[0], OTHER[1], "Bo")):
            http.ok("POST", "/api/admin/users",
                    {"username": username, "display_name": display, "password": password,
                     "is_admin": False}, token=token)
        project = http.ok("POST", "/api/projects", {"name": "E2E project"}, token=token)
        w.project_id = project["id"]
        users = {u["username"]: u for u in http.ok("GET", "/api/admin/users", token=token)["users"]}
        w.users = users
        http.ok("PUT", f"/api/p/{w.project_id}/members/{users[CODER[0]]['id']}",
                {"role": "coder", "tags": [CODER_TAG],
                 "tasks": ["coding", "settle", "harmonize", "reproduce"]}, token=token)
        http.ok("PUT", f"/api/p/{w.project_id}/members/{users[OTHER[0]]['id']}",
                {"role": "coder", "tags": [OTHER_TAG], "tasks": ["coding", "settle"]},
                token=token)
        w.admin = http
        w.admin_token = token
        coder_app.start()
        yield w
    finally:
        try:
            routes_online.set_store(None)
        except Exception:                          # noqa: BLE001
            pass
        try:
            S.online = False
            S.store = None
            if previous_root and os.path.isdir(previous_root):
                S.configure(previous_root)
        except Exception:                          # noqa: BLE001
            pass
        coder_app.stop()
        server.stop()


def owner_sync(w) -> dict:
    return w.owner_client.post("/api/online/sync").get_json()


def coder_sync(w) -> dict:
    return w.coder.ok("POST", "/api/online/sync", {})


def owner_status(w) -> dict:
    return w.owner_client.get("/api/online/status").get_json()


def coder_status(w) -> dict:
    return w.coder.ok("GET", "/api/online/status")


def owner_path(w, rel: str) -> str:
    return os.path.join(w.owner_mirror, *rel.split("/"))


def coder_path(w, rel: str) -> str:
    return os.path.join(w.coder_mirror(), *rel.split("/"))


# ---------------------------------------------------------------------------
# 01 - owner connects, opens and publishes
# ---------------------------------------------------------------------------

def test_01_owner_publishes_and_admin_shapes(world):
    w = world
    http = Http(w.base_url)

    resp = w.owner_client.post("/api/online/connect",
                               json={"server": w.base_url, "username": OWNER[0],
                                     "password": OWNER[1]})
    assert resp.status_code == 200, (w.base_url, w.server.port, resp.get_json())
    projects = resp.get_json()["projects"]
    assert [p["id"] for p in projects] == [w.project_id]
    opened = w.owner_client.post("/api/online/open", json={"project_id": w.project_id})
    assert opened.status_code == 200, opened.get_json()
    w.owner_mirror = opened.get_json()["root"]
    assert os.path.normcase(w.owner_mirror) == os.path.normcase(
        mirror_dir(w.base_url, w.project_id, os.path.join(w.scratch, "mirrors", "owner")))

    status = owner_status(w)
    assert status["active"] is True and status["me"]["role"] == "owner"
    assert status["settings"]["blinding"] == {"abstract": "blinded", "fulltext": "blinded",
                                             "coding": "blinded"}

    resp = w.owner_client.post("/api/online/admin/publish", json={"source_root": w.source})
    assert resp.status_code == 200, resp.get_json()
    first = resp.get_json()["progress"]
    assert set(("state", "running", "total", "done", "bytes")) <= set(first), first

    progress = wait_until(
        lambda: (lambda p: p if not p["running"] else None)(
            w.owner_client.get("/api/online/admin/publish").get_json()),
        timeout=120, what="publish to finish")
    assert progress["ok"] is True, progress
    assert progress["state"] == "published", progress
    assert progress["done"] == progress["total"], progress
    assert progress["pushed"] >= len(KEYS) * 2, progress
    assert progress["bytes"] > 0 and progress["errors"] == [], progress

    # every synthetic file reached the server
    manifest = w.admin.ok("GET", f"/api/p/{w.project_id}/manifest?since=0",
                          token=w.admin_token)
    paths = {f["path"] for f in manifest["files"]}
    for rel in ("Text/" + KEYS[0] + ".txt", "Papers/" + KEYS[0] + ".pdf",
                "Extraction/" + KEYS[0] + ".json",
                "Codings/" + KEYS[0] + " - " + OTHER_TAG + ".json",
                "Screening/" + CODER_TAG + ".json",
                "Datasets/" + KEYS[0] + "/" + KEYS[0] + " - raw.csv",
                "Datasets/" + KEYS[0] + "/harmonizer.py"):
        assert rel in paths, rel

    # the owner's mirror catches up
    owner_sync(w)
    assert os.path.isfile(owner_path(w, "Extraction/" + KEY + ".json"))

    # admin passthrough shapes (the real server's answers, unchanged + derived)
    members = w.owner_client.get("/api/online/admin/members").get_json()["members"]
    assert {m["username"] for m in members} == {OWNER[0], CODER[0], OTHER[0]}
    assert all({"user_id", "username", "display_name", "role", "tags", "tasks"} <= set(m)
               for m in members)

    users = w.owner_client.get("/api/online/admin/users").get_json()["users"]
    assert {u["username"] for u in users} >= {OWNER[0], CODER[0]}
    assert all({"id", "username", "display_name", "email", "is_admin", "disabled"} <= set(u)
               for u in users)

    settings = w.owner_client.get("/api/online/admin/settings").get_json()["settings"]
    assert settings["blinding"]["coding"] == "blinded" and settings["lock_ttl_h"] == 24

    audit = w.owner_client.get("/api/online/admin/audit?limit=50").get_json()["audit"]
    assert audit and all({"ts", "username", "action", "detail"} <= set(e) for e in audit)
    assert any(e["action"] == "project.create" for e in audit)


# ---------------------------------------------------------------------------
# 02 - the coder connects and sees the papers (PDFs not downloaded yet)
# ---------------------------------------------------------------------------

def test_02_coder_sees_papers_and_pdfs_stay_in_the_cache(world):
    w = world
    projects = w.coder.ok("POST", "/api/online/connect",
                          {"server": w.base_url, "username": CODER[0],
                           "password": CODER[1]})["projects"]
    assert [p["id"] for p in projects] == [w.project_id]
    opened = w.coder.ok("POST", "/api/online/open", {"project_id": w.project_id})
    assert opened["status"]["active"] is True
    assert opened["status"]["me"]["tags"] == [CODER_TAG]

    sync = coder_sync(w)
    assert sync["mode"] == "online"

    papers = w.coder.ok("GET", "/api/papers")["papers"]
    assert {p["key"] for p in papers} == set(KEYS), [p["key"] for p in papers]
    assert all(p["has_pdf"] for p in papers)

    pdf = coder_path(w, "Papers/" + KEY + ".pdf")
    assert not os.path.exists(pdf), "a PDF must not be downloaded before the paper is opened"

    status, body = w.coder.raw("/paper/" + urllib.parse.quote(KEY, safe=""))
    assert status == 200 and body.startswith(b"%PDF-1.4")
    assert os.path.isfile(pdf), "opening the paper fetches the PDF"
    # Text/ is core: it is mirrored already
    assert os.path.isfile(coder_path(w, "Text/" + KEY + ".txt"))


# ---------------------------------------------------------------------------
# 02b - a blinded coder screens from the decision-free records file
# ---------------------------------------------------------------------------

def test_02b_blinded_coder_screens_from_the_records_file(world):
    """The abstract stage is blinded, so the mirror has no master workbook.

    Screening must still work: the tab reads ``Project/master_records.csv.gz``
    (bibliographic rows only, never a verdict), the coder's decision saves to its
    own ``Screening/<tag>.json`` and lands on the owner.
    """
    w = world
    assert not os.path.exists(coder_path(w, "master_dataset.xlsx"))
    assert os.path.isfile(coder_path(w, "Project/master_records.csv.gz"))

    meta = w.coder.ok("GET", "/api/screening/meta?coder=" + CODER_TAG)
    assert meta["available"] is True, meta
    assert meta["source"] == "records"
    assert meta["master"] == "master_records.csv.gz"
    assert meta["total"] == len(KEYS)

    listing = w.coder.ok("GET", "/api/screening/list?coder=" + CODER_TAG
                         + "&filter=all")["rows"]
    expected = [f"E2E{i:03d}" for i in range(1, len(KEYS) + 1)]
    assert [r["i"] for r in listing] == expected
    assert all(r["d"] == 0 for r in listing)          # nothing decided by this tag yet

    # the abstract pane reads the text (the index's side file), and there is no
    # AI verdict to show: those live in a foreign tag's Screening/ file
    abstract = w.coder.ok("GET", "/api/screening/abstract/" + expected[0])
    assert abstract["record"]["abstract"] == f"Abstract of {KEYS[0]}. More text."
    assert abstract["ai_decision"] == ""
    assert abstract["human"] is None

    # the decision saves into the coder's own owned file ...
    saved = w.coder.ok("POST", "/api/screening/decision", {
        "coder": CODER_TAG, "id": expected[0],
        "criteria": {key: {"verdict": "Yes", "certainty": "High", "rationale": ""}
                     for key in ("C1", "C2", "C3", "C4")},
    })
    assert saved["ok"] is True and saved["decision"] == "Include", saved
    own = coder_path(w, "Screening/" + CODER_TAG + ".json")
    assert os.path.isfile(own)
    assert read_json(own)[expected[0]]["decision"] == "Include"

    # ... and reaches the owner through the normal push
    sync = coder_sync(w)
    assert sync["pending"] == 0, sync
    owner_sync(w)
    owner_copy = read_json(owner_path(w, "Screening/" + CODER_TAG + ".json"))
    assert owner_copy[expected[0]]["decision"] == "Include"
    assert owner_copy[expected[0]]["criteria"]["C1"]["verdict"] == "Yes"



def test_03_blinding_hides_the_other_tag_and_the_gate_refuses_it(world):
    w = world
    # a blinded coder's mirror holds no derived consensus file: the master
    # workbook carries every coder's screening/full-text verdicts
    assert os.path.isfile(owner_path(w, "master_dataset.xlsx"))
    assert not os.path.exists(coder_path(w, "master_dataset.xlsx"))
    own = w.coder.ok("GET", "/api/coding/" + urllib.parse.quote(KEY, safe="") + "/" + CODER_TAG)
    assert own["coder"] == CODER_TAG

    status, data = w.coder.request("GET", "/api/coding/" + urllib.parse.quote(KEY, safe="")
                                   + "/" + OTHER_TAG)
    assert status == 404, data
    assert not os.path.exists(coder_path(w, f"Codings/{KEY} - {OTHER_TAG}.json"))

    # a save under another tag is refused before it reaches the coding route
    status, data = w.coder.request("POST", "/api/save",
                                   {"key": KEY, "coder": OTHER_TAG,
                                    "records": [record(KEY)]})
    assert status == 403, data
    assert data["error"] == "forbidden"

    status, data = w.coder.request("POST", "/api/save",
                                   {"key": KEY, "coder": CODER_TAG,
                                    "records": [record(KEY, N_Analyzed=412)]})
    assert status == 200, data


# ---------------------------------------------------------------------------
# 04 - finishing paper K lifts the blinding for K
# ---------------------------------------------------------------------------

def test_04_finish_paper_reveals_the_other_tag_for_that_paper(world):
    w = world
    resp = w.coder.ok("POST", "/api/online/done",
                      {"key": KEY, "stage": "coding", "tag": CODER_TAG})
    assert resp["ok"] is True

    done = w.coder.ok("GET", "/api/online/done?stage=coding&tag=" + CODER_TAG)["done"]
    assert any(r["key"] == KEY and r["tag"] == CODER_TAG and r["stage"] == "coding"
               for r in done)

    sync = coder_sync(w)
    assert sync["mode"] == "online"

    # the epoch changed: the other tag's coding of K is readable now
    visible = w.coder.ok("GET", "/api/coding/" + urllib.parse.quote(KEY, safe="")
                         + "/" + OTHER_TAG)
    assert visible["coder"] == OTHER_TAG
    assert os.path.isfile(coder_path(w, f"Codings/{KEY} - {OTHER_TAG}.json"))
    # ... and only of K
    assert not os.path.exists(coder_path(w, f"Codings/{KEYS[2]} - {OTHER_TAG}.json"))


# ---------------------------------------------------------------------------
# 05 - automatic assignment (pairs, 40 % overlap) reaches the coder
# ---------------------------------------------------------------------------

def test_05_automatic_assignment_pairs_40_percent(world):
    w = world
    body = {"stage": "coding", "keys": KEYS,
            "coders": [{"tag": CODER_TAG, "share": 1}, {"tag": OTHER_TAG, "share": 1}],
            "overlap_pct": 40, "overlap_mode": "pairs", "seed": 424242}
    plan = w.owner_client.post("/api/online/admin/assignments/preview", json=body).get_json()
    assert set(plan) >= {"assignments", "per_coder", "pair_matrix", "params"}
    assert plan["params"]["seed"] == 424242
    assert plan["params"]["n_overlap"] == round(len(KEYS) * 0.4) == 2
    assert plan["pair_matrix"] == {f"{CODER_TAG}|{OTHER_TAG}": 2}
    assert plan["per_coder"] == {CODER_TAG: 4, OTHER_TAG: 3}
    shared = [key for key, tags in plan["assignments"].items() if len(tags) == 2]
    assert len(shared) == 2 and all(sorted(plan["assignments"][k]) ==
                                    sorted([CODER_TAG, OTHER_TAG]) for k in shared)

    commit = w.owner_client.post("/api/online/admin/assignments/commit", json=body).get_json()
    assert commit["run_id"]
    w.run_id = commit["run_id"]
    w.plan = plan

    # the coder's own assignment list == the plan's rows for its tag
    rows = w.coder.ok("GET", "/api/online/assignments?stage=coding")["assignments"]
    mine = {key for key, tags in plan["assignments"].items() if CODER_TAG in tags}
    assert {r["key"] for r in rows} == mine
    assert all(r["tag"] == CODER_TAG and r["stage"] == "coding"
               and r["run_id"] == commit["run_id"] for r in rows)

    runs = w.owner_client.get("/api/online/admin/assignments/runs").get_json()["runs"]
    run = next(r for r in runs if r["run_id"] == commit["run_id"])
    assert run["mode"] == "planned" and run["n_keys"] == len(KEYS)
    assert run["stage"] == "coding" and run["undone"] is False


# ---------------------------------------------------------------------------
# 06 - the rest of the admin passthroughs
# ---------------------------------------------------------------------------

def test_06_admin_passthroughs_manual_undo_users_settings_audit(world):
    w = world

    manual = w.owner_client.post("/api/online/admin/assignments/manual",
                                 json={"stage": "coding", "keys": [KEYS[2]],
                                       "tags": [CODER_TAG]}).get_json()
    assert manual["run_id"]
    rows = w.coder.ok("GET", "/api/online/assignments?stage=coding")["assignments"]
    assert any(r["key"] == KEYS[2] and r["run_id"] == manual["run_id"] for r in rows)

    runs = w.owner_client.get("/api/online/admin/assignments/runs").get_json()["runs"]
    row = next(r for r in runs if r["run_id"] == manual["run_id"])
    assert row["mode"] == "manual" and row["n_keys"] == 1

    assert w.owner_client.delete(
        f"/api/online/admin/assignments/runs/{manual['run_id']}").status_code == 200
    runs = w.owner_client.get("/api/online/admin/assignments/runs").get_json()["runs"]
    assert next(r for r in runs if r["run_id"] == manual["run_id"])["undone"] is True
    rows = w.coder.ok("GET", "/api/online/assignments?stage=coding")["assignments"]
    assert all(r["run_id"] != manual["run_id"] for r in rows)
    # the committed plan is untouched
    assert any(r["run_id"] == w.run_id for r in rows)

    # settings round trip
    updated = w.owner_client.patch("/api/online/admin/settings",
                                   json={"lock_ttl_h": 12}).get_json()["settings"]
    assert updated["lock_ttl_h"] == 12
    restored = w.owner_client.patch("/api/online/admin/settings",
                                    json={"lock_ttl_h": 24}).get_json()["settings"]
    assert restored["lock_ttl_h"] == 24

    # member round trip
    member = w.owner_client.put(
        f"/api/online/admin/members/{w.users[OTHER[0]]['id']}",
        json={"role": "coder", "tags": [OTHER_TAG], "tasks": ["coding", "settle"]}
    ).get_json()["member"]
    assert member["user_id"] == w.users[OTHER[0]]["id"] and member["tags"] == [OTHER_TAG]

    # user create + disable through the passthrough
    created = w.owner_client.post("/api/online/admin/users",
                                  json={"username": "temp_e2e", "display_name": "Temp",
                                        "email": "", "password": "temppass123",
                                        "is_admin": False}).get_json()["user"]
    assert created["username"] == "temp_e2e" and created["disabled"] is False
    disabled = w.owner_client.patch(f"/api/online/admin/users/{created['id']}",
                                    json={"disabled": True}).get_json()["user"]
    assert disabled["disabled"] is True

    # membership add + remove
    added = w.owner_client.put(f"/api/online/admin/members/{created['id']}",
                               json={"role": "viewer", "tags": [], "tasks": []}).get_json()
    assert added["member"]["user_id"] == created["id"]
    assert added["member"]["role"] == "viewer"
    removed = w.owner_client.delete(f"/api/online/admin/members/{created['id']}").get_json()
    assert removed["ok"] is True and removed["user_id"] == created["id"]

    audit = w.owner_client.get("/api/online/admin/audit?limit=200").get_json()["audit"]
    actions = {e["action"] for e in audit}
    # the project audit trail is project-scoped (user.create is a global entry)
    assert {"assignments.commit", "assignments.manual", "assignments.undo",
            "settings.update", "member.set"} <= actions, actions
    assert all(isinstance(e.get("detail"), (dict, type(None))) for e in audit)

    # a coder gets 403 from every admin route
    for method, path, body in (("GET", "/api/online/admin/members", None),
                               ("GET", "/api/online/admin/users", None),
                               ("GET", "/api/online/admin/audit", None),
                               ("GET", "/api/online/admin/assignments/runs", None),
                               ("DELETE", "/api/online/admin/locks/X", None)):
        status, data = w.coder.request(method, path, body)
        assert status == 403 and data["error"] == "forbidden", (path, status, data)


# ---------------------------------------------------------------------------
# 07 - a coder's coding file reaches the owner
# ---------------------------------------------------------------------------

def test_07_coder_coding_reaches_the_owner(world):
    w = world
    records = [record(KEY, N_Analyzed=433, Setting="lab")]
    saved = w.coder.ok("POST", "/api/save", {"key": KEY, "coder": CODER_TAG,
                                            "records": records})
    assert saved["ok"] is True

    sync = coder_sync(w)
    assert sync["pending"] == 0
    owner_sync(w)

    seen = w.owner_client.get("/api/coding/" + urllib.parse.quote(KEY, safe="")
                              + "/" + CODER_TAG).get_json()
    assert seen["records"][0]["N_Analyzed"] == 433
    assert seen["records"][0]["Setting"] == "lab"


# ---------------------------------------------------------------------------
# 08 - disjoint edits of Extraction/K.json merge
# ---------------------------------------------------------------------------

def test_08_disjoint_extraction_edits_merge(world):
    w = world
    rel = "Extraction/" + KEY + ".json"
    assert os.path.isfile(owner_path(w, rel)) and os.path.isfile(coder_path(w, rel))

    mine = read_json(owner_path(w, rel))[0]
    mine["N_Analyzed"] = 500
    write(owner_path(w, rel), [mine])
    theirs = read_json(coder_path(w, rel))[0]
    theirs["Setting"] = "field"
    write(coder_path(w, rel), [theirs])

    owner_sync(w)                          # accepted
    sync = coder_sync(w)                   # merged on the server
    assert sync["conflicts"] == 0
    owner_sync(w)

    a = read_json(owner_path(w, rel))[0]
    b = read_json(coder_path(w, rel))[0]
    assert a["N_Analyzed"] == 500 and a["Setting"] == "field", a
    assert b["N_Analyzed"] == 500 and b["Setting"] == "field", b
    assert read_json(owner_path(w, rel)) == read_json(coder_path(w, rel))


# ---------------------------------------------------------------------------
# 09 - the same field conflicts, and the coder resolves it
# ---------------------------------------------------------------------------

def test_09_same_field_conflict_is_listed_and_resolved(world):
    w = world
    rel = "Extraction/" + KEY + ".json"

    mine = read_json(owner_path(w, rel))[0]
    mine["N_Analyzed"] = 501
    write(owner_path(w, rel), [mine])
    theirs = read_json(coder_path(w, rel))[0]
    theirs["N_Analyzed"] = 999
    write(coder_path(w, rel), [theirs])

    owner_sync(w)                          # 501 is the head now
    sync = coder_sync(w)                   # same field -> conflict
    assert sync["conflicts"] == 1, sync

    conflicts = w.coder.ok("GET", "/api/online/conflicts")["conflicts"]
    assert len(conflicts) == 1
    rec = conflicts[0]
    assert rec["path"] == rel and rec["kind"] == "conflict"

    detail = w.coder.ok("GET", f"/api/online/conflicts/{rec['id']}")
    assert detail["path"] == rel
    assert '"N_Analyzed": 999' in detail["mine_text"], detail["mine_text"]
    assert '"N_Analyzed": 501' in detail["theirs_text"], detail["theirs_text"]

    resolved = w.coder.ok("POST", f"/api/online/conflicts/{rec['id']}/resolve",
                          {"resolution": "mine"})
    assert resolved["ok"] is True
    coder_sync(w)
    owner_sync(w)

    assert w.coder.ok("GET", "/api/online/conflicts")["conflicts"] == []
    assert read_json(owner_path(w, rel))[0]["N_Analyzed"] == 999
    assert read_json(coder_path(w, rel))[0]["N_Analyzed"] == 999


# ---------------------------------------------------------------------------
# 10 - lock identity: mine, 409 with holder, force release
# ---------------------------------------------------------------------------

def test_10_lock_identity_and_force_release(world):
    w = world
    quoted = urllib.parse.quote(KEY, safe="")

    lock = w.coder.ok("POST", f"/api/online/locks/{quoted}", {"offline_hours": 4})["lock"]
    assert lock["key"] == KEY
    assert lock["user"] == CODER[0] and lock["user_id"] == w.users[CODER[0]]["id"]
    assert lock["display_name"] == "Anna" and lock["client_id"]
    assert lock["mine"] is True and lock["offline"] is True and lock["expired"] is False

    coder_locks = w.coder.ok("GET", "/api/online/locks")["locks"]
    assert [r["key"] for r in coder_locks if r["mine"]] == [KEY]

    owner_locks = w.owner_client.get("/api/online/locks").get_json()["locks"]
    row = next(r for r in owner_locks if r["key"] == KEY)
    assert row["mine"] is False
    assert row["user"] == CODER[0] and row["user_id"] == w.users[CODER[0]]["id"]
    assert row["display_name"] == "Anna" and row["client_id"] == lock["client_id"]

    resp = w.owner_client.post(f"/api/online/locks/{quoted}", json={})
    assert resp.status_code == 409, resp.get_json()
    body = resp.get_json()
    assert body["error"] == "locked"
    assert body["holder"]["user"] == CODER[0]
    assert body["holder"]["user_id"] == w.users[CODER[0]]["id"]

    # the admin route is the owner's force release
    assert w.owner_client.delete(f"/api/online/admin/locks/{quoted}").status_code == 200
    assert w.coder.ok("GET", "/api/online/locks")["locks"] == []


# ---------------------------------------------------------------------------
# 11 - offline: finish a paper and edit a file
# ---------------------------------------------------------------------------

def test_11_offline_finish_and_edit_queue_up(world):
    w = world
    rel = f"Codings/{KEY2} - {CODER_TAG}.json"
    assert os.path.isfile(coder_path(w, rel))

    w.server.stop()
    w.server.wait_down()

    resp = w.coder.ok("POST", "/api/online/done",
                      {"key": KEY2, "stage": "coding", "tag": CODER_TAG})
    assert resp["ok"] is True

    records = read_json(coder_path(w, rel))
    records[0]["N_Analyzed"] = 777
    write(coder_path(w, rel), records)

    sync = coder_sync(w)
    assert sync["mode"] == "offline", sync
    assert sync["pending"] >= 2, sync

    status = coder_status(w)
    assert status["mode"] == "offline"
    assert status["pending"] >= 2 and status["done_pending"] == 1

    # the done list answers from the durable queue while the server is away
    done = w.coder.ok("GET", "/api/online/done?stage=coding&tag=" + CODER_TAG)["done"]
    assert any(r["key"] == KEY2 and r.get("pending") for r in done), done

    # ... and the queue is on disk, not only in memory
    state = read_json(os.path.join(w.coder_mirror(), ".studio", "state.json"))
    assert {"key": KEY2, "stage": "coding", "tag": CODER_TAG} in state["done_pending"]


# ---------------------------------------------------------------------------
# 12 - the server returns: both changes land, nothing is pending
# ---------------------------------------------------------------------------

def test_12_reconnect_lands_done_and_edit(world):
    w = world
    w.server.start(w.server.port)
    w.admin = Http(w.base_url)
    w.admin_token = w.admin.ok("POST", "/api/auth/login",
                               {"username": OWNER[0], "password": OWNER[1]})["token"]

    sync = coder_sync(w)
    assert sync["mode"] == "online", sync
    assert sync["pending"] == 0 and sync["done_pending"] == 0, sync

    status = coder_status(w)
    assert status["pending"] == 0 and status["done_pending"] == 0

    owner_sync(w)
    done = w.owner_client.get("/api/online/done?tag=" + CODER_TAG).get_json()["done"]
    assert any(r["key"] == KEY2 and r["tag"] == CODER_TAG and r["stage"] == "coding"
               for r in done), done

    seen = w.owner_client.get("/api/coding/" + urllib.parse.quote(KEY2, safe="")
                              + "/" + CODER_TAG).get_json()
    assert seen["records"][0]["N_Analyzed"] == 777
    assert not any(r.get("pending") for r in done)
