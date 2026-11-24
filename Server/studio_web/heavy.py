"""Which app requests are memory-heavy (PLAN_online_only §2.2, §6.1).

A *family* is a group of routes that build the same corpus-wide structure.
Only the first request of a family in a worker's life pays the peak (the
structure is then cached in the worker), so the gateway sends a family through
the heavy lane until it has completed once in that worker.  Routes listed in
``ALWAYS`` rebuild their result every time (workbook merges, exports).
"""

from __future__ import annotations

#: (family, method, path prefix) — measured peak above ~100 MB on the real project
FAMILIES: tuple[tuple[str, str, str], ...] = (
    ("screening", "GET", "/api/screening/meta"),
    ("screening", "GET", "/api/screening/list"),
    ("screening", "GET", "/api/screening/abstract/"),
    ("screening", "GET", "/api/agreement/abstract/"),
    ("agreement", "GET", "/api/agreement/reliability"),
    ("agreement", "GET", "/api/agreement/conflicts"),
    ("agreement", "GET", "/api/agreement/settlements"),
    ("summary", "GET", "/api/summary/"),
    ("prisma", "GET", "/api/analysis/prisma"),
    ("analysis", "GET", "/api/analysis/overview"),
    ("analysis", "GET", "/api/analysis/pooled"),
    ("analysis", "GET", "/api/analysis/subgroup"),
    ("analysis", "GET", "/api/analysis/table"),
    ("analysis", "GET", "/api/analysis/bayes"),
    ("project", "GET", "/api/project/summary"),
    ("coverage", "GET", "/api/coverage"),
    ("repro", "GET", "/api/repro/overview"),
    ("harmonizer", "GET", "/api/harmonizer/index"),
    ("harmonizer", "GET", "/api/harmonizer/missing"),
)

#: always through the lane (no lasting cache, large transient peak)
ALWAYS: tuple[tuple[str, str], ...] = (
    ("POST", "/api/screening/merge"),
    ("POST", "/api/fulltext/merge"),
    ("GET", "/api/analysis/export"),
    ("POST", "/api/analysis/apa"),
)


def classify(method: str, path: str) -> tuple[bool, str | None]:
    """``(heavy, family)`` for an app request (*path* relative to the worker)."""
    method = (method or "GET").upper()
    for m, prefix in ALWAYS:
        if method == m and path.startswith(prefix):
            return True, None
    for family, m, prefix in FAMILIES:
        if method == m and path.startswith(prefix):
            return True, family
    return False, None
