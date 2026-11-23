"""Shared, **stdlib-only** helpers for online projects.

This package is imported by both the desktop client (``Scripts/coding_web``)
and the studio server (``Server/studio_server``), so it must never import
pandas, numpy, fastapi or any other third-party module.

Modules
-------
protocol
    Version/port/timing constants and :func:`classify` — the single place
    that says what a project-relative path *is* (``owned``, ``shared``,
    ``immutable``, ...) and whether the mirror must always carry it.
merge
    :func:`merge3` — the three-way merge used when a push races the server
    head (JSON at field level, text files at line level).

See ``Results/INTERFACES_online_projects.md`` §0-§2 for the binding contract.
"""

from . import merge, protocol
from .protocol import PROTOCOL_VERSION

__all__ = ["merge", "protocol", "PROTOCOL_VERSION"]
