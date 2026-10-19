"""Durable append-only drop log for the analysis pipeline.

Every study/contrast a pipeline stage silently drops used to leave no durable
trace (the meta stage wrote none at all).  This module records one JSON object
per line in ``Results/analysis_log.jsonl`` so a partial run is auditable after
the fact.

API
---
``reset(kind=None)``
    Mark the start of a pipeline run in this process: stamps the run timestamp
    (and an optional pipeline ``kind`` such as ``"metaanalysis"``) that every
    subsequent record carries.  It does **not** truncate the file: the pipeline
    runs each stage as a separate subprocess, so truncating here would erase the
    previous stage's records.  The run timestamp distinguishes runs instead.
    It touches the file so the log exists even when nothing dropped.

``log(kind, **fields)``
    Append one record.  ``kind`` is the drop category (``extraction_skipped``,
    ``am_did_excluded``, ``exclusion_rule_problem``, ...); every other field is
    stored verbatim.  Appends are serialised with a lock so parallel writers
    (e.g. ``run_reproducers --jobs N``) cannot interleave a line.
"""
import datetime
import json
import os
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))       # Scripts/analysis
B = os.path.dirname(os.path.dirname(_HERE))              # project root
RESULTS = os.path.join(B, "Results")
LOG_PATH = os.path.join(RESULTS, "analysis_log.jsonl")

_LOCK = threading.Lock()
_run_ts = None
_run_kind = ""


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def reset(kind=None):
    """Start a new in-process run: set the run timestamp / pipeline kind.

    Never truncates (each stage is a separate process and appends).  Returns the
    run timestamp.
    """
    global _run_ts, _run_kind
    _run_ts = _now()
    _run_kind = kind or ""
    os.makedirs(RESULTS, exist_ok=True)
    if not os.path.exists(LOG_PATH):
        # Touch so consumers can always open the artifact, even on a clean run.
        with open(LOG_PATH, "a", encoding="utf-8"):
            pass
    return _run_ts


def log(kind, **fields):
    """Append one drop record to ``Results/analysis_log.jsonl``."""
    os.makedirs(RESULTS, exist_ok=True)
    rec = {"ts": _run_ts or _now(), "run": _run_kind, "logged_at": _now(),
           "kind": kind}
    rec.update(fields)
    line = json.dumps(rec, ensure_ascii=False, default=str)
    with _LOCK:
        with open(LOG_PATH, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
