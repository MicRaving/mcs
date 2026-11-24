"""Browser worker: one headless CodingStudio backend per (user, project).

Phase 8 (``Results/PLAN_online_only.md`` §4): the browser gateway starts one of
these for every logged-in browser user.  It is the desktop app in online mode,
unchanged, on a server-side mirror of the project:

* the mirror is filled from the user's own (blinding-filtered) manifest; with
  ``STUDIO_LOCAL_BLOBS`` its files are symlinks into the server's read-only
  blob store instead of downloads (``Syncer._local_blob``);
* every write lands in the mirror and is pushed by the ordinary syncer
  (base hash -> merge3 -> conflict -> audit), so desktop and browser users
  share one history;
* blinding, coder tags, tasks and locks are the server's and ``routes_online``'s
  existing checks - nothing here decides visibility.

What this module adds on top of the desktop app:

* a shared-secret check (only the gateway may talk to a worker);
* the browser route policy (``browser_policy.browser_route_allowed``);
* a sync kick after every successful write, ``malloc_trim`` after heavy
  requests, and ``/__worker/health`` / ``/__worker/drain`` for the gateway.

Configuration comes from the environment (the token never touches the disk)::

    STUDIO_SERVER         http://studio:8770        the studio server (internal URL)
    STUDIO_TOKEN          <opaque token>            the user's session token
    STUDIO_PROJECT        <project id>
    STUDIO_HOME           /srv/studio/web/u7        per-user home (mirrors, caches, config)
    STUDIO_LOCAL_BLOBS    /blobs                    optional: link instead of download
    STUDIO_WORKER_SECRET  <random>                  required header value
    STUDIO_SYNC_INTERVAL  5                         seconds between pulls
    STUDIO_PUSH_DEBOUNCE  1                         seconds before a write is pushed

Run: ``python Scripts/coding_web/browser_worker.py [--port 0]``; prints
``READY <port>`` on stdout once the first sync round has finished.
"""

from __future__ import annotations

import argparse
import hmac
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
for _p in (HERE, SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SECRET_HEADER = "X-Studio-Worker-Secret"
HEAVY_HEADER = "X-Studio-Heavy"
MUTATING = ("POST", "PUT", "PATCH", "DELETE")


@dataclass
class WorkerConfig:
    server: str
    token: str
    project_id: str
    home: str
    secret: str
    local_blobs: str | None = None
    sync_interval: float = 5.0
    push_debounce: float = 1.0
    host: str = "127.0.0.1"
    port: int = 0

    @classmethod
    def from_env(cls, env=None) -> "WorkerConfig":
        env = os.environ if env is None else env
        missing = [k for k in ("STUDIO_SERVER", "STUDIO_TOKEN", "STUDIO_PROJECT", "STUDIO_HOME",
                               "STUDIO_WORKER_SECRET") if not env.get(k)]
        if missing:
            raise SystemExit(f"browser worker: missing environment {', '.join(missing)}")
        return cls(server=env["STUDIO_SERVER"], token=env["STUDIO_TOKEN"],
                   project_id=env["STUDIO_PROJECT"], home=env["STUDIO_HOME"],
                   secret=env["STUDIO_WORKER_SECRET"],
                   local_blobs=env.get("STUDIO_LOCAL_BLOBS") or None,
                   sync_interval=float(env.get("STUDIO_SYNC_INTERVAL") or 5),
                   push_debounce=float(env.get("STUDIO_PUSH_DEBOUNCE") or 1))


# ---------------------------------------------------------------------------
# environment: a per-user home before the app is imported
# ---------------------------------------------------------------------------

def prepare_home(home: str) -> dict:
    """Point every per-user location of the app at *home* (call before importing ``app``).

    ``studio_state`` configures a root at import time and writes into it
    (``Codings/``, ``temp/`` ...), and the app keeps its config, log and the
    agreement pickle in ``LOCALAPPDATA``/``XDG_*``: all of that must land in
    the user's own directory, never in a shared one.
    """
    home = os.path.abspath(home)
    paths = {
        "HOME": home,
        "LOCALAPPDATA": os.path.join(home, "appdata"),
        "APPDATA": os.path.join(home, "appdata"),
        "XDG_CACHE_HOME": os.path.join(home, "cache"),
        "XDG_CONFIG_HOME": os.path.join(home, "config"),
        "XDG_DATA_HOME": os.path.join(home, "data"),
        "CODING_STUDIO_ROOT": os.path.join(home, "empty-root"),
        "TMPDIR": os.path.join(home, "tmp"),
    }
    for key, value in paths.items():
        if key != "HOME":
            os.makedirs(value, exist_ok=True)
        os.environ[key] = value
    os.environ.pop("CODING_STUDIO_RUN_ANALYSIS", None)
    return paths


# ---------------------------------------------------------------------------
# memory helpers
# ---------------------------------------------------------------------------

def rss_bytes(pid: int | None = None) -> int:
    """Resident set size of *pid* (default: this process); 0 when unknown."""
    pid = pid or os.getpid()
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    try:
        import psutil

        return int(psutil.Process(pid).memory_info().rss)
    except Exception:  # noqa: BLE001
        return 0


def malloc_trim() -> None:
    """Hand freed heap back to the OS (glibc); a no-op elsewhere."""
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# the browser route policy (``browser_policy``, PLAN_online_only §7)
# ---------------------------------------------------------------------------

def check_route(endpoint, method, role) -> tuple[int, dict]:
    """``(200, {})`` when the call may run, else the policy's refusal.

    *role* is the member's project role (``owner``/``coder``/``viewer``).  The
    policy is default-deny; if the module cannot be imported or fails, every
    app route is refused (fail closed) - a worker never runs without it.
    """
    try:
        from browser_policy import denial
    except ImportError:
        return 503, {"error": "desktop_only",
                     "message": "the browser route policy is not installed on this server"}
    try:
        status, body = denial(endpoint, method, role)
    except Exception as exc:  # noqa: BLE001
        return 503, {"error": "desktop_only", "message": f"route policy failed: {type(exc).__name__}"}
    return int(status), dict(body or {})


# ---------------------------------------------------------------------------
# building the worker
# ---------------------------------------------------------------------------

class WorkerState:
    def __init__(self):
        self.inflight = 0
        self.lock = threading.Lock()
        self.ready = False
        self.started = time.time()
        self.requests = 0


def build_worker(cfg: WorkerConfig):
    """Open the user's project and return ``(flask_app, store, state)``.

    Blocks until the first sync round has finished, so the first request
    already sees the user's files.
    """
    prepare_home(cfg.home)
    import app as appmod                       # noqa: E402 - after prepare_home
    import routes_online
    from flask import jsonify, request
    from online.store import OnlineStore
    from studio_lib import APP_VERSION
    from studio_state import S, log

    flask_app = appmod.app
    state = WorkerState()
    store = OnlineStore(base_dir=os.path.join(cfg.home, "mirrors"), use_keyring=False,
                        persist_token=False, interval=cfg.sync_interval,
                        debounce=cfg.push_debounce, autostart=False,
                        app_version=APP_VERSION, local_blobs=cfg.local_blobs)
    store.adopt(cfg.server, cfg.token)
    root = store.open(cfg.project_id, start=False)
    if cfg.local_blobs:
        # links cost no disk: the LRU limit would only evict them pointlessly
        try:
            store.syncer.cache.configure(limit_mb=10 ** 7)
        except Exception:  # noqa: BLE001
            pass
    store.sync_now()                           # first round: link/download the core tier
    routes_online.set_store(store)
    S.browser = True
    flask_app.config["BROWSER_MODE"] = True      # read by browser_policy.install() users
    S.set_online(store, root)

    def _on_change(paths):
        # a paper that appears mid-session must become openable (S.KEYS is
        # otherwise only built at start-up)
        if any(p.startswith(("Text/", "Papers/")) for p in paths):
            try:
                S.refresh_keys()
            except Exception:  # noqa: BLE001
                pass

    store.syncer.on_change = _on_change
    store.syncer.start()
    secret = cfg.secret.encode("utf-8")

    def _guard():
        given = (request.headers.get(SECRET_HEADER) or "").encode("utf-8")
        if not hmac.compare_digest(given, secret):
            return jsonify({"error": "forbidden", "message": "not the gateway"}), 403
        with state.lock:
            state.inflight += 1
            state.requests += 1
        request.environ["studio.counted"] = True
        path = request.path or ""
        if path.startswith("/__worker/"):
            return None
        # the PROJECT member's role ({role, tags, tasks} from GET /api/p/<pid>,
        # refreshed by the syncer when membership changes); unknown -> coder
        role = (store.me() or {}).get("role") if store.mirror is not None else None
        status, body = check_route(request.endpoint, request.method, role or "coder")
        if status != 200:
            return jsonify(body), status
        return None

    # The secret check runs before every other hook (incl. the coder gate).
    flask_app.before_request_funcs.setdefault(None, []).insert(0, _guard)

    @flask_app.after_request
    def _after(response):
        if request.method in MUTATING and response.status_code < 400 \
                and not (request.path or "").startswith("/__worker/"):
            try:
                store.kick()
            except Exception:  # noqa: BLE001
                pass
        if request.headers.get(HEAVY_HEADER):
            response.call_on_close(malloc_trim)
        return response

    @flask_app.teardown_request
    def _done(_exc=None):
        if request.environ.get("studio.counted"):
            with state.lock:
                state.inflight -= 1

    @flask_app.get("/__worker/health")
    def _health():
        try:
            st = store.status()
        except Exception as exc:  # noqa: BLE001
            st = {"mode": "error", "last_error": str(exc)}
        return jsonify({
            "ready": state.ready, "pid": os.getpid(), "rss": rss_bytes(),
            "inflight": max(0, state.inflight - 1), "requests": state.requests,
            "uptime": round(time.time() - state.started, 1),
            "mode": st.get("mode"), "pending": st.get("pending"),
            "conflicts": st.get("conflicts"), "last_error": st.get("last_error"),
            "root": S.ROOT,
        })

    @flask_app.post("/__worker/drain")
    def _drain():
        """Push everything now (before the gateway stops this worker)."""
        try:
            st = store.sync_now()
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(exc)}), 503
        return jsonify({"ok": not st.get("pending"), "pending": st.get("pending"),
                        "mode": st.get("mode")})

    state.ready = True
    log(f"browser worker ready: project {cfg.project_id} at {root}")
    return flask_app, store, state


def serve(cfg: WorkerConfig, announce) -> None:
    """Build, listen, call ``announce(port)``, serve until SIGTERM/SIGINT."""
    flask_app, store, _state = build_worker(cfg)
    from werkzeug.serving import make_server

    import logging

    logging.getLogger("werkzeug").disabled = True
    srv = make_server(cfg.host, cfg.port, flask_app, threaded=True)
    stopping = threading.Event()

    def _stop(*_a):
        if stopping.is_set():
            return
        stopping.set()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    try:
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
    except ValueError:            # not the main thread (tests)
        pass
    announce(srv.server_port)
    try:
        srv.serve_forever()
    finally:
        try:
            store.sync_now()
        except Exception:  # noqa: BLE001
            pass
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass


def run_from_env() -> None:
    """Entry point of a worker process (subprocess or forkserver child)."""
    cfg = WorkerConfig.from_env()

    def announce(port):
        sys.stdout.write(f"READY {port}\n")
        sys.stdout.flush()

    serve(cfg, announce)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="CodingStudio browser worker")
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args(argv)
    if args.port is not None:
        os.environ["STUDIO_WORKER_PORT"] = str(args.port)
    cfg = WorkerConfig.from_env()
    cfg.port = int(os.environ.get("STUDIO_WORKER_PORT") or 0)

    def announce(port):
        sys.stdout.write(f"READY {port}\n")
        sys.stdout.flush()

    serve(cfg, announce)


if __name__ == "__main__":
    main()


__all__ = ["WorkerConfig", "build_worker", "check_route", "malloc_trim", "prepare_home",
           "rss_bytes", "run_from_env", "serve", "SECRET_HEADER", "HEAVY_HEADER"]
