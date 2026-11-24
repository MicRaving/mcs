"""studio-server — the CodingStudio online-project sync server.

Implements the server foundation of ``Results/INTERFACES_online_projects.md``
(§3.1 process/config, §3.2 SQLite schema, §3.3 auth, §3.4 admin/projects,
§3.10 errors).  The auth/token/project-registry layer is adapted from the
private QCnext project (https://github.com/MicRaving/qcnext,
``backend/src/qualcoder_api``), rewritten on stdlib ``sqlite3`` with plain
``def`` endpoints instead of SQLAlchemy/async sessions; passkeys are dropped.

Import shim (§0.1): the shared protocol module lives in ``Scripts/online_shared``
in a dev checkout and at ``/app/online_shared`` in the Docker image.  If it is
not importable yet, ``<repo>/Scripts`` is appended to ``sys.path`` once; when it
is still absent the protocol constants fall back to their contract values so
the server can be developed and tested on its own.
"""

from __future__ import annotations

import sys
from pathlib import Path

SERVER_NAME = "studio-server"
SERVER_VERSION = "0.1.0"

# Contract fallbacks (INTERFACES §1).  When ``online_shared`` exists its values
# win — this module is the only place that decides.
_FALLBACK_PROTOCOL_VERSION = 1
_FALLBACK_STAGES = ("abstract", "fulltext", "coding")
_FALLBACK_TASKS = ("abstract", "fulltext", "coding", "settle", "harmonize", "reproduce")
_FALLBACK_ROLES = ("owner", "coder", "viewer")


def _load_shared_protocol():
    """Import ``online_shared.protocol``, adding ``<repo>/Scripts`` if needed."""
    try:
        import online_shared.protocol as protocol  # type: ignore[import-not-found]

        return protocol
    except ImportError:
        scripts = Path(__file__).resolve().parents[2] / "Scripts"
        if scripts.is_dir() and str(scripts) not in sys.path:
            sys.path.append(str(scripts))
        try:
            import online_shared.protocol as protocol  # type: ignore[import-not-found]

            return protocol
        except ImportError:
            return None


_shared = _load_shared_protocol()

PROTOCOL_VERSION = int(getattr(_shared, "PROTOCOL_VERSION", _FALLBACK_PROTOCOL_VERSION))
STAGES = tuple(getattr(_shared, "STAGES", _FALLBACK_STAGES))
TASKS = tuple(getattr(_shared, "TASKS", _FALLBACK_TASKS))
ROLES = tuple(getattr(_shared, "ROLES", _FALLBACK_ROLES))

#: Every task an owner may perform (owner ⇒ all tasks, §3.4).
ALL_TASKS = TASKS
#: Wildcard tag returned for owners; ``role == "owner"`` is the real test.
ALL_TAGS = ("*",)

__all__ = [
    "ALL_TAGS",
    "ALL_TASKS",
    "PROTOCOL_VERSION",
    "ROLES",
    "SERVER_NAME",
    "SERVER_VERSION",
    "STAGES",
    "TASKS",
]
