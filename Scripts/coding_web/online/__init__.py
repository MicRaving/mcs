"""Desktop client of online projects (INTERFACES_online_projects.md §4).

Modules:

* ``client``        - HTTP client for the studio server (transport seam for tests)
* ``mirror``        - mirror directory, ``state.json``, working-tree scan
* ``outbox``        - durable, coalescing change journal
* ``syncer``        - scan -> push -> pull, background thread, status
* ``cache``         - core / pinned / LRU / supplements tiers, eviction
* ``conflicts``     - local conflict records
* ``keyring_store`` - token storage
* ``store``         - ``OnlineStore``, the façade the Flask routes use
"""

from __future__ import annotations

import os
import sys

try:                                    # Scripts/ is on sys.path in the app
    import online_shared  # noqa: F401
except ImportError:                     # pragma: no cover - standalone use
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    import online_shared  # noqa: F401
