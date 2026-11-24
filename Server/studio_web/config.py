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
    heavy_slots: int = 1
    monitor_secs: float = 5.0
    spawn_timeout: float = 900.0
    drain_timeout: float = 60.0
    auth_ttl: float = 60.0
    sync_interval: float = 5.0
    push_debounce: float = 1.0
    cookie_name: str = "studio_session"
    cookie_secure: bool = True
    #: e.g. "https://meta.home.mfendt.de"; empty = compare Origin with Host
    public_origin: str = ""

    @classmethod
    def from_env(cls, env=None) -> "WebConfig":
        env = os.environ if env is None else env
        return cls(
            data_dir=env.get("STUDIO_WEB_DATA", "./web-data"),
            server=env.get("STUDIO_WEB_SERVER", "http://127.0.0.1:8770").rstrip("/"),
            local_blobs=env.get("STUDIO_WEB_LOCAL_BLOBS") or None,
            worker_script=env.get("STUDIO_WEB_WORKER_SCRIPT") or default_worker_script(),
            launcher=env.get("STUDIO_WEB_LAUNCHER", "auto"),
            idle_secs=_f(env, "STUDIO_WEB_IDLE_SECS", 900),
            soft_cap_mb=_f(env, "STUDIO_WEB_SOFT_MB", 550),
            hard_cap_mb=_f(env, "STUDIO_WEB_HARD_MB", 1100),
            pressure_mb=_f(env, "STUDIO_WEB_PRESSURE_MB", 1450),
            heavy_slots=max(1, int(_f(env, "STUDIO_WEB_HEAVY_SLOTS", 1))),
            monitor_secs=_f(env, "STUDIO_WEB_MONITOR_SECS", 5),
            spawn_timeout=_f(env, "STUDIO_WEB_SPAWN_TIMEOUT", 900),
            drain_timeout=_f(env, "STUDIO_WEB_DRAIN_TIMEOUT", 60),
            auth_ttl=_f(env, "STUDIO_WEB_AUTH_TTL", 60),
            sync_interval=_f(env, "STUDIO_WEB_SYNC_INTERVAL", 5),
            push_debounce=_f(env, "STUDIO_WEB_PUSH_DEBOUNCE", 1),
            cookie_secure=env.get("STUDIO_WEB_COOKIE_SECURE", "1") != "0",
            public_origin=(env.get("STUDIO_WEB_PUBLIC_ORIGIN") or "").rstrip("/"),
        )

    def ensure_dirs(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
