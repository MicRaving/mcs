"""Process configuration (INTERFACES §3.1).

Environment (adapted from QCnext ``core/server_config.py``):

* ``STUDIO_DATA_DIR``  — data root, default ``./data``
* ``STUDIO_PUBLIC_URL`` — advertised base URL (unused by the foundation API)
* ``STUDIO_TRUST_PROXY`` — ``1`` ⇒ rate-limit by the first ``X-Forwarded-For``

The data dir layout is ``studio.db`` (SQLite, WAL), ``blobs/`` (content
addressed files) and ``logs/``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DB_NAME = "studio.db"
TOKEN_TTL_SECS = 30 * 24 * 60 * 60  # 30 days, sliding on use (§3.3)

_TRUE = ("1", "true", "yes", "on")


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    """Everything the process reads about its environment, resolved once."""

    data_dir: Path
    public_url: str = ""
    trust_proxy: bool = False
    token_ttl_secs: int = TOKEN_TTL_SECS
    rate_limit_failures: int = 10
    rate_limit_window_secs: int = 10 * 60

    #: Sub-directories of the data root (kept as a field so tests can inspect
    #: them without importing the module-level constants).
    subdirs: tuple[str, ...] = field(default=("blobs", "logs"), repr=False)

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_NAME

    @property
    def blobs_dir(self) -> Path:
        return self.data_dir / "blobs"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for name in self.subdirs:
            (self.data_dir / name).mkdir(parents=True, exist_ok=True)


def load_config(data_dir: str | os.PathLike[str] | None = None) -> Config:
    """Resolve the configuration from the environment (call once at startup)."""
    raw_dir = data_dir or os.environ.get("STUDIO_DATA_DIR") or "./data"
    return Config(
        data_dir=Path(raw_dir).expanduser().resolve(),
        public_url=os.environ.get("STUDIO_PUBLIC_URL", "").strip(),
        trust_proxy=_env_bool("STUDIO_TRUST_PROXY"),
        token_ttl_secs=_env_int("STUDIO_TOKEN_TTL_SECS", TOKEN_TTL_SECS),
        rate_limit_failures=_env_int("STUDIO_RATE_LIMIT_FAILURES", 10),
        rate_limit_window_secs=_env_int("STUDIO_RATE_LIMIT_WINDOW_SECS", 10 * 60),
    )
