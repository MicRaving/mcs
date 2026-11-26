"""Worker registry and lifecycle (PLAN_online_only §6.2).

One worker per ``(user_id, project_id)``.  Policies, evaluated every
``monitor_secs`` and right after each proxied request:

* **idle**: no request for ``idle_secs`` and none in flight -> drain, stop.
* **soft cap**: RSS above ``soft_cap_mb`` with nothing in flight -> drain,
  stop; the next request starts a fresh one (its caches on disk keep it warm).
  This returns the allocator high-water a long-lived process keeps.
* **hard cap**: RSS above ``hard_cap_mb`` -> stop even mid-request (the
  request gets an error; the mirror and outbox make that lossless).
* **pressure**: summed RSS above ``pressure_mb`` -> stop idle workers, least
  recently used first.

Draining means ``POST /__worker/drain`` (one blocking sync round) before the
SIGTERM; a worker whose push cannot reach the server is not stopped for
idleness (its outbox is on disk either way).

The heavy lane is an ``asyncio.Semaphore`` with ``heavy_slots`` slots; see
:mod:`.heavy` for what goes through it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from dataclasses import dataclass, field

import httpx

from .config import MB, WebConfig
from .launcher import SpawnError, WorkerProcess, make_launcher

log = logging.getLogger("studio_web")

SECRET_HEADER = "X-Studio-Worker-Secret"


def rss_bytes(pid: int) -> int:
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


def pss_bytes(pid: int) -> int:
    """Proportional set size (Linux): shared pages split between sharers."""
    try:
        with open(f"/proc/{pid}/smaps_rollup", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("Pss:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return rss_bytes(pid)


@dataclass
class Worker:
    user_id: int
    project_id: str
    token: str
    secret: str
    home: str
    proc: WorkerProcess | None = None
    inflight: int = 0
    last_used: float = field(default_factory=time.monotonic)
    started: float = field(default_factory=time.monotonic)
    rss: int = 0
    warm: set = field(default_factory=set)
    spawn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: the running stop (drain + terminate), if any: a new process for this
    #: (user, project) must never start while the old one still owns the mirror
    stop_task: asyncio.Task | None = None

    @property
    def stopping(self) -> bool:
        return self.stop_task is not None and not self.stop_task.done()

    @property
    def key(self) -> tuple[int, str]:
        return (self.user_id, self.project_id)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.proc.port}" if self.proc else ""

    def alive(self) -> bool:
        return self.proc is not None and self.proc.alive()


class Supervisor:
    def __init__(self, cfg: WebConfig, launcher=None):
        self.cfg = cfg
        self.launcher = launcher if launcher is not None else make_launcher(cfg.launcher, cfg.worker_script)
        self.workers: dict[tuple[int, str], Worker] = {}
        self.lane = asyncio.Semaphore(cfg.heavy_slots)
        self.lane_waiting = 0
        #: admission for the max_workers cap (the count check and the eviction
        #: must not interleave between two users starting at the same moment)
        self._admit = asyncio.Lock()
        self.events: list[dict] = []        # recent lifecycle events (health/metrics)
        self._monitor: asyncio.Task | None = None
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0))

    # -- paths -------------------------------------------------------------------------

    def home_of(self, user_id: int) -> str:
        return os.path.join(os.path.abspath(self.cfg.data_dir), f"u{int(user_id)}")

    def _event(self, kind: str, w: Worker, **extra) -> None:
        row = {"ts": round(time.time(), 1), "event": kind, "user": w.user_id,
               "project": w.project_id, **extra}
        self.events.append(row)
        del self.events[:-200]
        log.info("worker %s %s", kind, row)

    # -- spawn ------------------------------------------------------------------------------

    def _env(self, w: Worker) -> dict:
        env = {
            "STUDIO_SERVER": self.cfg.server,
            "STUDIO_TOKEN": w.token,
            "STUDIO_PROJECT": w.project_id,
            "STUDIO_HOME": w.home,
            "STUDIO_WORKER_SECRET": w.secret,
            "STUDIO_SYNC_INTERVAL": str(self.cfg.sync_interval),
            "STUDIO_PUSH_DEBOUNCE": str(self.cfg.push_debounce),
            "MALLOC_ARENA_MAX": "2",
            "PYTHONUNBUFFERED": "1",
        }
        if self.cfg.local_blobs:
            env["STUDIO_LOCAL_BLOBS"] = self.cfg.local_blobs
        return env

    async def get(self, user_id: int, project_id: str, token: str) -> Worker:
        """The running worker of (user, project), started if needed."""
        key = (int(user_id), str(project_id))
        w = self.workers.get(key)
        if w is None:
            w = Worker(int(user_id), str(project_id), token, secrets.token_urlsafe(32),
                       self.home_of(user_id))
            self.workers[key] = w
        w.last_used = time.monotonic()
        if w.alive() and not w.stopping:
            return w
        async with w.spawn_lock:
            # one process per mirror: wait for a stop in progress to finish
            while w.stopping:
                await asyncio.shield(w.stop_task)
            if w.alive():
                return w
            await self._make_room(w)
            w.token = token or w.token
            w.secret = secrets.token_urlsafe(32)
            w.warm = set()
            os.makedirs(w.home, exist_ok=True)
            t0 = time.monotonic()
            proc = await asyncio.to_thread(self.launcher.spawn, self._env(w),
                                           os.path.join(w.home, "worker.log"), self.cfg.spawn_timeout)
            w.proc = proc
            w.started = time.monotonic()
            w.rss = rss_bytes(proc.pid)
            self._event("started", w, pid=proc.pid, secs=round(time.monotonic() - t0, 2),
                        launcher=getattr(self.launcher, "name", "?"))
        return w

    async def _make_room(self, w: Worker) -> None:
        """Keep the number of live workers under ``cfg.max_workers``.

        At the cap, the least recently used **idle** worker is stopped (its
        mirror and outbox make that lossless; the next request restarts it).
        When every slot is busy the request is refused instead of letting the
        cgroup OOM-kill a running worker.  ``max_workers <= 0`` disables the cap.
        """
        limit = int(getattr(self.cfg, "max_workers", 0) or 0)
        if limit <= 0:
            return
        async with self._admit:
            others = [o for o in self.workers.values()
                      if o is not w and (o.alive() or o.stopping or o.spawn_lock.locked())]
            if len(others) < limit:
                return
            idle = sorted((o for o in others
                           if o.alive() and not o.inflight and not o.stopping),
                          key=lambda o: o.last_used)
            if not idle:
                raise SpawnError(
                    f"all {limit} browser workers are busy; try again in a moment")
            victim = idle[0]
            self._event("max_workers", victim, limit=limit)
            await self.stop(victim, "max_workers")

    def is_starting(self, user_id: int, project_id: str) -> bool:
        w = self.workers.get((int(user_id), str(project_id)))
        return bool(w and w.spawn_lock.locked())

    # -- stop -----------------------------------------------------------------------------------

    async def drain(self, w: Worker) -> dict:
        if not w.alive():
            return {"ok": True, "pending": 0}
        try:
            r = await self.http.post(w.base_url + "/__worker/drain", headers={SECRET_HEADER: w.secret},
                                     timeout=self.cfg.drain_timeout)
            return r.json()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    async def stop(self, w: Worker, reason: str, drain: bool = True) -> None:
        """Drain (optional) and stop *w*'s process; concurrent calls share one stop."""
        if w.stopping:
            await asyncio.shield(w.stop_task)
            return
        if w.proc is None:
            return
        w.stop_task = asyncio.ensure_future(self._stop(w, w.proc, reason, drain))
        await asyncio.shield(w.stop_task)

    async def _stop(self, w: Worker, proc: WorkerProcess, reason: str, drain: bool) -> None:
        try:
            result = await self.drain(w) if drain and proc.alive() else {}
            proc.terminate()
            if not await asyncio.to_thread(proc.wait, 20.0):
                proc.kill()
                await asyncio.to_thread(proc.wait, 5.0)
            self._event("stopped", w, reason=reason, rss_mb=round(w.rss / MB),
                        pending=result.get("pending"))
        finally:
            if w.proc is proc:
                w.proc = None

    async def stop_user(self, user_id: int, reason: str = "logout") -> None:
        for w in [w for w in self.workers.values() if w.user_id == int(user_id)]:
            await self.stop(w, reason)

    async def stop_all(self, reason: str = "shutdown") -> None:
        await asyncio.gather(*(self.stop(w, reason) for w in list(self.workers.values())),
                             return_exceptions=True)

    # -- policies ---------------------------------------------------------------------------------

    def sample(self) -> int:
        """Refresh each worker's memory figure; returns the sum.

        On Linux this is the PSS (forkserver children share the preloaded
        pages; RSS would count them once per worker), elsewhere the RSS.
        """
        total = 0
        for w in self.workers.values():
            if w.alive():
                w.rss = pss_bytes(w.proc.pid)
                total += w.rss
            else:
                w.rss = 0
        return total

    async def enforce(self) -> None:
        """One pass of the lifecycle policies (also called after each request)."""
        cfg = self.cfg
        now = time.monotonic()
        total = self.sample()
        for w in list(self.workers.values()):
            if w.proc is not None and not w.alive() and not w.stopping:
                self._event("exited", w)
                w.proc = None
                continue
            if not w.alive() or w.stopping:
                continue
            if w.rss > cfg.hard_cap_mb * MB:
                self._event("hard_cap", w, rss_mb=round(w.rss / MB))
                asyncio.create_task(self.stop(w, "hard_cap", drain=w.inflight == 0))
                continue
            if w.inflight:
                continue
            if w.rss > cfg.soft_cap_mb * MB:
                asyncio.create_task(self.stop(w, "soft_cap"))
                continue
            if now - w.last_used > cfg.idle_secs:
                asyncio.create_task(self.stop(w, "idle"))
        if total > cfg.pressure_mb * MB:
            idle = sorted((w for w in self.workers.values() if w.alive() and not w.inflight
                           and not w.stopping), key=lambda w: w.last_used)
            for w in idle:
                if total <= cfg.pressure_mb * MB:
                    break
                total -= w.rss
                asyncio.create_task(self.stop(w, "pressure"))

    async def _monitor_loop(self) -> None:
        while True:
            try:
                await self.enforce()
            except Exception:  # noqa: BLE001 - the monitor must never die
                log.exception("monitor pass failed")
            await asyncio.sleep(self.cfg.monitor_secs)

    def start(self) -> None:
        if self._monitor is None:
            self._monitor = asyncio.create_task(self._monitor_loop())

    async def close(self) -> None:
        if self._monitor is not None:
            self._monitor.cancel()
            self._monitor = None
        await self.stop_all()
        await self.http.aclose()

    # -- metrics ------------------------------------------------------------------------------------

    def snapshot(self, detail: bool = True) -> dict:
        total = self.sample()
        out = {"workers": len([w for w in self.workers.values() if w.alive()]),
               "rss_mb": round(total / MB, 1),
               "lane": {"slots": self.cfg.heavy_slots, "waiting": self.lane_waiting},
               "launcher": getattr(self.launcher, "name", "?"),
               "limits": {"max_workers": self.cfg.max_workers,
                          "mem_limit_mb": self.cfg.mem_limit_mb,
                          "pressure_mb": self.cfg.pressure_mb}}
        if detail:
            out["detail"] = [{
                "user": w.user_id, "project": w.project_id, "pid": w.proc.pid if w.proc else None,
                "alive": w.alive(), "rss_mb": round(w.rss / MB, 1),
                "pss_mb": round(pss_bytes(w.proc.pid) / MB, 1) if w.alive() else 0,
                "inflight": w.inflight, "idle_s": round(time.monotonic() - w.last_used, 1),
                "warm": sorted(w.warm)} for w in self.workers.values()]
            out["events"] = self.events[-50:]
        return out


__all__ = ["SECRET_HEADER", "SpawnError", "Supervisor", "Worker", "pss_bytes", "rss_bytes"]
