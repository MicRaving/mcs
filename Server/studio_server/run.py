"""``python -m studio_server run`` — ONE CodingStudio server product.

The sync server always runs (uvicorn, one process).  With
``STUDIO_BROWSER_MODE=1`` the browser gateway (``studio_web``: login cookie,
project picker, one worker per user) is started as a supervised child of this
process, so the gateway, its forkserver and the workers live in this
container's cgroup and its memory limit.  With the switch off the gateway is
never started and **never imported** — the browser stack then costs nothing
(no module, no thread, no child); this process itself binds the browser port
and answers ``/app/`` with the "switched off" page (:mod:`.browser`).

Why the sync server is the parent and not a second child: the product must
keep the 52 MB sync-server footprint, and a supervising PID 1 would add a
process.  The duties of PID 1 are therefore done here:

* SIGTERM/SIGINT stop uvicorn gracefully and are forwarded to the gateway's
  process group (its workers included) before this process exits;
* orphaned grandchildren (a gateway that crashed while its forkserver/workers
  still ran) are reaped by the monitor thread;
* a crashed gateway is restarted with exponential backoff (1 s … 30 s);
* if the sync server itself cannot start, the exit code is non-zero, so the
  container runtime restarts it.

Environment (besides :mod:`studio_server.config`):

* ``STUDIO_BROWSER_MODE``  ``1`` starts browser mode (default ``0``)
* ``STUDIO_BROWSER_PORT``  the browser port (default ``8780``)
* ``STUDIO_BROWSER_HOST``  the gateway's bind address (default ``0.0.0.0``)
* ``STUDIO_GATEWAY_LOG_LEVEL``  the gateway's uvicorn log level (default ``info``)
* ``STUDIO_SHUTDOWN_SECS``  how long a graceful stop may take (default ``30``)
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time

from . import browser
from .config import load_config

log = logging.getLogger("studio_server.run")

#: gateway restart delays (seconds); the last value repeats
BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0)
#: a gateway that stayed up this long resets the backoff
HEALTHY_AFTER = 120.0


def _listen(host: str, port: int) -> socket.socket:
    """A bound, listening socket for uvicorn's ``sockets=`` argument."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(2048)
    sock.set_inheritable(True)
    return sock


class GatewayProcess:
    """The browser gateway as a supervised child process (one gateway)."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        python: str | None = None,
        cwd: str | None = None,
        log_level: str = "info",
        shutdown_secs: float = 30.0,
    ):
        self.host = host
        self.port = int(port)
        self.python = python or sys.executable
        self.cwd = cwd
        self.log_level = log_level
        self.shutdown_secs = float(shutdown_secs)
        self.proc: subprocess.Popen | None = None
        self.pgid: int | None = None
        self.started_at = 0.0
        self.failures = 0
        self.restarts = 0
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None

    # -- command / environment ------------------------------------------------

    def command(self) -> list[str]:
        return [
            self.python, "-X", "utf8", "-m", "studio_web", "serve",
            "--host", self.host, "--port", str(self.port), "--log-level", self.log_level,
        ]

    def child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> bool:
        if self._stop.is_set():
            return False
        try:
            self.proc = subprocess.Popen(
                self.command(),
                cwd=self.cwd,
                env=self.child_env(),
                start_new_session=(os.name == "posix"),
                # the gateway logs go to this container's stdout/stderr
                stdin=subprocess.DEVNULL,
            )
        except OSError as exc:
            self.proc = None
            log.error("browser gateway could not be started: %s", exc)
            return False
        self.started_at = time.monotonic()
        self.pgid = self.proc.pid if os.name == "posix" else None
        log.info("browser gateway started (pid %s) on %s:%s",
                 self.proc.pid, self.host, self.port)
        if self._monitor is None:
            self._monitor = threading.Thread(
                target=self._monitor_loop, name="gateway-supervisor", daemon=True)
            self._monitor.start()
        return True

    def _monitor_loop(self) -> None:
        while not self._stop.wait(0.5):
            proc = self.proc
            if proc is None:
                continue
            self._reap_orphans(proc.pid)
            rc = proc.poll()
            if rc is None:
                continue
            self.proc = None
            uptime = time.monotonic() - self.started_at
            if uptime >= HEALTHY_AFTER:
                self.failures = 0
            if self._stop.is_set():
                log.info("browser gateway exited (%s) during shutdown", rc)
                self._stop_group()
                return
            # a crashed gateway leaves its forkserver/workers running; they
            # would keep syncing forever and be invisible to the new gateway
            self._stop_group()
            delay = BACKOFF[min(self.failures, len(BACKOFF) - 1)]
            self.failures += 1
            self.restarts += 1
            log.error("browser gateway exited (%s) after %.0fs; restarting in %.0fs",
                      rc, uptime, delay)
            if self._stop.wait(delay):
                return
            self.start()

    def _reap_orphans(self, gateway_pid: int) -> None:
        """Reap children that are not the gateway (workers of a crash).

        ``waitid(... WNOWAIT)`` peeks without collecting, so the gateway's own
        status stays available to :meth:`Popen.poll` (which needs the exit code).
        """
        if not hasattr(os, "waitid") or os.name != "posix":
            return
        while True:
            try:
                info = os.waitid(os.P_ALL, 0,
                                 os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                return
            if info is None or not getattr(info, "si_pid", None):
                return
            pid = int(info.si_pid)
            if pid == gateway_pid:
                return
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return

    def stop(self) -> None:
        self._stop.set()
        proc = self.proc
        if proc is not None and proc.poll() is None:
            log.info("stopping the browser gateway (pid %s)", proc.pid)
            self._signal(proc, signal.SIGTERM)
            deadline = time.monotonic() + self.shutdown_secs
            while time.monotonic() < deadline and proc.poll() is None:
                time.sleep(0.1)
            if proc.poll() is None:
                log.warning("browser gateway did not stop in %.0fs; killing it",
                            self.shutdown_secs)
                self._signal(proc, signal.SIGKILL)
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    pass
        self._stop_group()
        self.proc = None
        self.pgid = None

    def _stop_group(self, grace: float = 10.0) -> None:
        """Make sure no process of the gateway's group survives it."""
        pgid = self.pgid
        if not pgid or os.name != "posix":
            return
        try:
            os.killpg(pgid, 0)                 # any members left?
        except (ProcessLookupError, PermissionError):
            self.pgid = None
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            self.pgid = None
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                self.pgid = None
                return
            time.sleep(0.1)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.pgid = None

    @staticmethod
    def _signal(proc: subprocess.Popen, sig: int) -> None:
        """Signal the gateway's whole process group (its workers too)."""
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), sig)
            else:
                proc.send_signal(sig)
        except (OSError, ProcessLookupError):
            pass

    # -- health/reporting -----------------------------------------------------

    def status(self) -> dict:
        return {
            "pid": self.proc.pid if self.proc and self.proc.poll() is None else None,
            "restarts": self.restarts,
            "port": self.port,
        }


def run(args: argparse.Namespace) -> int:
    """Run the product; returns the process exit code."""
    import uvicorn

    from .app import create_app

    cfg = load_config(args.data)
    cfg.ensure_dirs()

    if args.log_level:
        logging.basicConfig(
            level=args.log_level.upper(),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )

    app = create_app(cfg)
    sockets = [_listen(args.host, args.port)]
    # the browser side always talks to this process, never to a public name
    os.environ.setdefault("STUDIO_WEB_SERVER", f"http://127.0.0.1:{args.port}")

    gateway: GatewayProcess | None = None
    if cfg.browser_mode:
        gateway = GatewayProcess(
            os.environ.get("STUDIO_BROWSER_HOST", "0.0.0.0"),
            cfg.browser_port,
            cwd=os.getcwd(),
            log_level=os.environ.get("STUDIO_GATEWAY_LOG_LEVEL", "info"),
            shutdown_secs=float(os.environ.get("STUDIO_SHUTDOWN_SECS", "30") or 30),
        )
        gateway.start()
    else:
        # same reverse-proxy config either way: the browser port answers here
        try:
            sockets.append(_listen(args.host, cfg.browser_port))
        except OSError as exc:
            log.warning("browser port %s is not available (%s); /app/ will not answer",
                        cfg.browser_port, exc)

    log.info("studio-server on http://%s:%s (data: %s, browser mode: %s)",
             args.host, args.port, cfg.data_dir, "on" if cfg.browser_mode else "off")
    if len(sockets) > 1:
        log.info("browser port %s answers /app/ (gateway switched off)",
                 cfg.browser_port)

    server = uvicorn.Server(uvicorn.Config(
        app, host=args.host, port=args.port, workers=1,
        log_level=args.log_level or "info", proxy_headers=False,
    ))
    try:
        server.run(sockets=sockets)
    finally:
        if gateway is not None:
            gateway.stop()
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass

    if not server.started:
        log.error("the sync server did not start; exiting non-zero so the runtime restarts it")
        return 1
    return 0


def build_parser(sub=None) -> argparse.ArgumentParser:
    ap = sub or argparse.ArgumentParser(prog="python -m studio_server run")
    if sub is None:
        ap.description = "the CodingStudio server (sync server + optional browser mode)"
    ap.add_argument("--host", default=os.environ.get("STUDIO_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("STUDIO_PORT", "8770") or 8770))
    ap.add_argument("--data", default=None,
                    help="data dir (default $STUDIO_DATA_DIR or ./data)")
    ap.add_argument("--log-level", default=os.environ.get("STUDIO_LOG_LEVEL", "info"))
    ap.set_defaults(func=run)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


__all__ = ["BACKOFF", "GatewayProcess", "build_parser", "main", "run"]
