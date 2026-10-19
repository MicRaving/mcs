"""One PRISMA counter — the single implementation used by the pipeline *and* the
CodingStudio app (interface §4 of ``Results/INTERFACES_codingstudio_rework.md``).

This is a thin facade over ``Scripts/prisma_report.py`` (``report_ids`` /
``report_stages`` / ``prisma_flow``); it exists so the app never re-implements
PRISMA counting again (audit finding H6).  Import it instead of duplicating the
DOI/title folding and reason precedence:

    import prisma
    counts = prisma.counts()          # {"flow": ..., "stages": ..., "n_ids": ...}
    flow = prisma.flow()              # the PRISMA flow dict

Run standalone for a JSON dump:  ``python Scripts/prisma.py``.
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import prisma_report as _pr  # noqa: E402


def counts(master=None) -> dict:
    """Return ``{"flow", "stages", "n_ids"}`` from the canonical counter."""
    df = _pr.load_master() if master is None else master
    return {
        "flow": _pr.prisma_flow(df),
        "stages": _pr.report_stages(df),
        "n_ids": len(_pr.report_ids(df)),
    }


def flow(master=None) -> dict:
    df = _pr.load_master() if master is None else master
    return _pr.prisma_flow(df)


def main() -> int:
    print(json.dumps(counts(), indent=2, default=str, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
