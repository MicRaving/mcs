"""Starting worker processes.

* :class:`SubprocessLauncher` — ``python browser_worker.py`` per worker
  (every platform; each worker imports numpy/pandas/scipy itself).
* :class:`ForkserverLauncher` — Linux: one forkserver process imports the
  heavy third-party modules **and the Flask app** once (``studio_web._preload``,
  with ``studio_state`` pointed at an empty scratch root), then ``gc.freeze()``
  (``studio_web._freeze``), and every worker is a fork of it, so those pages are
  shared copy-on-write.  The child re-points the app at its own project
  (``S.set_online``), which is the desktop's normal re-root.

Both return a :class:`WorkerProcess` once the worker printed/sent its port
(i.e. after its first sync round).  ``spawn`` blocks; the supervisor runs it in
a thread.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

#: Modules imported once in the forkserver and shared copy-on-write by every
#: worker: the Flask app itself (its import graph brings numpy/pandas and the
#: route modules), PyMuPDF (every paper view), lxml/Pillow (dataset + page
#: rendering).  scipy and openpyxl are deliberately **not** here: the routes
#: that need them import them lazily, so a user who never runs an analysis or
#: exports a workbook never pays for them.
#: ``studio_web._preload`` must come after the third-party modules and
#: ``studio_web._freeze`` must be last.
PRELOAD = ["flask", "werkzeug.serving", "numpy", "pandas", "pymupdf",
           "lxml.etree", "PIL.Image", "studio_web._preload", "studio_web._freeze"]


class SpawnError(RuntimeError):
    pass


@dataclass
class WorkerProcess:
    pid: int
    port: int
    handle: object            # Popen | multiprocessing.Process
    log_path: str

    def alive(self) -> bool:
        h = self.handle
        if isinstance(h, subprocess.Popen):
            return h.poll() is None
        try:
            return bool(h.is_alive())
        except Exception:  # noqa: BLE001
            return False

    def terminate(self) -> None:
        try:
            os.kill(self.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass

    def kill(self) -> None:
        h = self.handle
        try:
            if isinstance(h, subprocess.Popen):
                h.kill()
            else:
                h.kill()
        except Exception:  # noqa: BLE001
            try:
                os.kill(self.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except OSError:
                pass

    def wait(self, timeout: float) -> bool:
        h = self.handle
        try:
            if isinstance(h, subprocess.Popen):
                h.wait(timeout)
            else:
                h.join(timeout)
        except subprocess.TimeoutExpired:
            return False
        return not self.alive()


def _log_file(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return open(path, "ab", buffering=0)


class SubprocessLauncher:
    name = "subprocess"

    def __init__(self, worker_script: str, python: str | None = None):
        if not worker_script or not os.path.isfile(worker_script):
            raise SpawnError(f"worker script not found: {worker_script!r}")
        self.worker_script = worker_script
        self.python = python or sys.executable

    def spawn(self, env: dict, log_path: str, timeout: float) -> WorkerProcess:
        full_env = dict(os.environ)
        full_env.update(env)
        log = _log_file(log_path)
        proc = subprocess.Popen(
            [self.python, "-X", "utf8", self.worker_script], env=full_env,
            stdout=subprocess.PIPE, stderr=log, stdin=subprocess.DEVNULL,
            cwd=os.path.dirname(self.worker_script))
        port: list[int] = []
        done = threading.Event()

        def reader():
            for raw in proc.stdout:          # type: ignore[union-attr]
                line = raw.decode("utf-8", "replace")
                if not port and line.startswith("READY "):
                    try:
                        port.append(int(line.split()[1]))
                    except (IndexError, ValueError):
                        pass
                    done.set()
                    continue
                try:
                    log.write(raw)
                except OSError:
                    pass
            done.set()

        threading.Thread(target=reader, daemon=True, name=f"worker-out-{proc.pid}").start()
        if not done.wait(timeout) or not port:
            proc.kill()
            raise SpawnError(f"worker did not start (exit {proc.poll()}); see {log_path}")
        return WorkerProcess(proc.pid, port[0], proc, log_path)


def _fork_entry(env: dict, conn, log_path: str) -> None:       # pragma: no cover - child
    """Runs in a forkserver child: redirect output, become the worker."""
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.environ.update(env)
    from . import ensure_worker_importable

    ensure_worker_importable()
    import browser_worker

    cfg = browser_worker.WorkerConfig.from_env()

    def announce(port):
        conn.send(int(port))
        conn.close()

    browser_worker.serve(cfg, announce)


class ForkserverLauncher:
    name = "forkserver"

    def __init__(self, preload: list[str] | None = None):
        import multiprocessing as mp

        if sys.platform.startswith("win"):
            raise SpawnError("forkserver is not available on Windows")
        from . import ensure_worker_importable

        ensure_worker_importable()
        self.ctx = mp.get_context("forkserver")
        self.ctx.set_forkserver_preload(list(preload or PRELOAD))

    def spawn(self, env: dict, log_path: str, timeout: float) -> WorkerProcess:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        parent, child = self.ctx.Pipe(duplex=False)
        proc = self.ctx.Process(target=_fork_entry, args=(env, child, log_path), daemon=False)
        proc.start()
        child.close()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if parent.poll(1.0):
                try:
                    port = int(parent.recv())
                except (EOFError, OSError, ValueError):
                    break
                return WorkerProcess(proc.pid, port, proc, log_path)
            if not proc.is_alive():
                break
        proc.kill()
        raise SpawnError(f"worker did not start (exit {proc.exitcode}); see {log_path}")


def make_launcher(kind: str, worker_script: str):
    kind = (kind or "auto").lower()
    if kind == "auto":
        kind = "forkserver" if sys.platform.startswith("linux") else "subprocess"
    if kind == "forkserver":
        return ForkserverLauncher()
    return SubprocessLauncher(worker_script)
