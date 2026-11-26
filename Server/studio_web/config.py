"""Gateway settings, from the environment (``STUDIO_WEB_*``)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import default_worker_script

MB = 1024 * 1024


def _f(env, name, default):
    try:
        return float(env.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def cgroup_memory_limit_mb() -> int:
    """The container's memory limit in MB, or 0 when there is none / no cgroup.

    Reads cgroup v2 (``memory.max``) then v1 (``memory.limit_in_bytes``).  The
    supervisor scales its worker cap, pressure ceiling and caps to this, so the
    same image behaves correctly in a 512 MB container and in a 4 GB one.
    """
    for path in ("/sys/fs/cgroup/memory.max",
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open(path, encoding="ascii") as fh:
                raw = fh.read().strip()
        except OSError:
            continue
        if not raw or raw == "max":
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        # "unlimited" is a huge number (often 2**63-1 or 2**64-1)
        if value <= 0 or value >= (1 << 60):
            continue
        return max(1, value // MB)
    return 0


def scale_to_limit(limit_mb: int) -> dict:
    """Worker/reaping policy for a container of *limit_mb* (0 = unknown).

    The defaults (a 1.5 GB browser container) are kept when the limit is
    unknown.  On a tight box the same rules apply earlier, so a small container
    recycles and reaps workers instead of being OOM-killed.
    """
    if limit_mb <= 0:
        return {"pressure": 1450.0, "soft": 550.0, "hard": 1100.0,
                "idle": 900.0, "max_workers": 4}
    pressure = max(64.0, float(limit_mb - max(64, int(limit_mb * 0.15))))
    soft = float(max(128, min(550, int(limit_mb * 0.40))))
    hard = float(max(soft + 64, min(1100, int(limit_mb * 0.72))))
    if limit_mb >= 1400:
        idle = 900.0
    elif limit_mb >= 1024:
        idle = 600.0
    elif limit_mb >= 768:
        idle = 300.0
    else:
        idle = 120.0
    # one worker per ~384 MB after the gateway/forkserver reserve (192 MB)
    max_workers = max(1, min(8, (limit_mb - 192) // 384))
    return {"pressure": pressure, "soft": soft, "hard": hard,
            "idle": idle, "max_workers": max_workers}


@dataclass
class WebConfig:
    #: per-user homes (mirrors, caches, logs): ``<data>/u<user_id>/``
    data_dir: str = "./web-data"
    #: the studio server as the gateway and the workers reach it (internal URL)
    server: str = "http://127.0.0.1:8770"
    #: server-local read-only blob store; workers link instead of download
    local_blobs: str | None = None
    worker_script: str = field(default_factory=default_worker_script)
    #: "auto" (forkserver on Linux, else subprocess) | "subprocess" | "forkserver"
    launcher: str = "auto"
    idle_secs: float = 900.0
    soft_cap_mb: float = 550.0
    hard_cap_mb: float = 1100.0
    pressure_mb: float = 1450.0
    #: the container's memory limit the figures were derived from (0 = unknown)
    mem_limit_mb: int = 0
    #: concurrent (user, project) workers; the least recently used idle one is
    #: evicted when a new user needs a slot and the box has no room left
    max_workers: int = 4
    heavy_slots: int = 1
    monitor_secs: float = 5.0
    spawn_timeout: float = 900.0
    drain_timeout: float = 60.0
    auth_ttl: float = 60.0
    sync_interval: float = 5.0
    push_debounce: float = 1.0
    cookie_name: str = "studio_session"
    cookie_secure: bool = True
    #: lifetime of the session cookie; the server's token has the same sliding
    #: 30-day expiry, so a shorter cookie only asks the user to sign in again
    session_days: float = 30.0
    #: e.g. "https://meta.home.mfendt.de"; empty = compare Origin with Host
    public_origin: str = ""

    @classmethod
    def from_env(cls, env=None) -> "WebConfig":
        env = os.environ if env is None else env
        limit = int(_f(env, "STUDIO_MEM_LIMIT_MB", 0)) or cgroup_memory_limit_mb()
        auto = scale_to_limit(limit)
        return cls(
            data_dir=env.get("STUDIO_WEB_DATA", "./web-data"),
            server=env.get("STUDIO_WEB_SERVER", "http://127.0.0.1:8770").rstrip("/"),
            local_blobs=env.get("STUDIO_WEB_LOCAL_BLOBS") or None,
            worker_script=env.get("STUDIO_WEB_WORKER_SCRIPT") or default_worker_script(),
            launcher=env.get("STUDIO_WEB_LAUNCHER", "auto"),
            idle_secs=_f(env, "STUDIO_WEB_IDLE_SECS", auto["idle"]),
            soft_cap_mb=_f(env, "STUDIO_WEB_SOFT_MB", auto["soft"]),
            hard_cap_mb=_f(env, "STUDIO_WEB_HARD_MB", auto["hard"]),
            pressure_mb=_f(env, "STUDIO_WEB_PRESSURE_MB", auto["pressure"]),
            mem_limit_mb=limit,
            max_workers=max(1, int(_f(env, "STUDIO_WEB_MAX_WORKERS", auto["max_workers"]))),
            heavy_slots=max(1, int(_f(env, "STUDIO_WEB_HEAVY_SLOTS", 1))),
            monitor_secs=_f(env, "STUDIO_WEB_MONITOR_SECS", 5),
            spawn_timeout=_f(env, "STUDIO_WEB_SPAWN_TIMEOUT", 900),
            drain_timeout=_f(env, "STUDIO_WEB_DRAIN_TIMEOUT", 60),
            auth_ttl=_f(env, "STUDIO_WEB_AUTH_TTL", 60),
            sync_interval=_f(env, "STUDIO_WEB_SYNC_INTERVAL", 5),
            push_debounce=_f(env, "STUDIO_WEB_PUSH_DEBOUNCE", 1),
            cookie_secure=env.get("STUDIO_WEB_COOKIE_SECURE", "1") != "0",
            session_days=_f(env, "STUDIO_WEB_SESSION_DAYS", 30),
            public_origin=(env.get("STUDIO_WEB_PUBLIC_ORIGIN") or "").rstrip("/"),
        )

    def ensure_dirs(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
