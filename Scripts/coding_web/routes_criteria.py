"""Criteria routes: the inclusion criteria of the active scheme.

The criteria are the abstract-screening and full-text-exclusion rules of
the project.  They live in their own document next to the scheme
(``<app dir>/Schemes/<id>/criteria.json``) and they are read through
``Scripts/project_context.py`` (which project is active?) and edited
through ``Scripts/criteria_lib.py`` (what is a criterion?).

**What this blueprint serves.**  ``routes_scheme.py`` already owns
``GET /api/criteria`` and ``PUT /api/criteria``; those are not duplicated
here.  What was missing is the *check* the criteria editor needs: a way
to ask "what is wrong with this document?" before it is written.  That is
``/api/criteria/validate``:

    GET  /api/criteria/validate   -> the problems of the stored criteria
    POST /api/criteria/validate   -> the problems of the posted document
                                    (body: {"criteria": {…}})

The answer is always ``{"ok": bool, "problems": [...], "error_count": n,
"warning_count": n, "criteria": {…}}``; a problem is
``{level, code, message, where}``.

The auto-approval rules of a criterion are part of the criterion, so they
travel with it: ``GET /api/criteria`` hands out
``auto_approve: {"<rule name>": "<expression>"}`` for every criterion that
has rules.  A rule whose expression does not compile is a **warning**, not
an error: the criteria stay usable and the rule is simply ignored at
match time (``criteria_lib.auto_approved``), so a broken rule can never
break the criteria.
"""

import os
import sys

from flask import Blueprint, jsonify, request

from studio_state import S

# The shared project layer lives one level up (Scripts/): ``project_context``
# answers "which project is active?" and ``criteria_lib`` knows what a
# criterion is.
try:
    import criteria_lib  # noqa: E402
    import project_context  # noqa: E402
except Exception:  # pragma: no cover - source layout fallback
    for _d in (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
               os.path.join(S.ROOT, "Scripts")):
        if _d and _d not in sys.path:
            sys.path.insert(0, _d)
    import criteria_lib  # noqa: E402
    import project_context  # noqa: E402

bp = Blueprint("criteria", __name__)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _body():
    """The request body as a dict (never raises)."""
    data = request.get_json(force=True, silent=True)
    return data if isinstance(data, dict) else {}


def _fail(message, status=400, **extra):
    """A JSON error body."""
    payload = {"ok": False, "error": message}
    payload.update(extra)
    return jsonify(payload), status


def _stored_criteria():
    """The active scheme's criteria, normalised."""
    try:
        doc = project_context.criteria()
    except Exception:  # noqa: BLE001 - a broken project must not 500
        doc = None
    if isinstance(doc, dict):
        return doc
    return {"version": 1, "abstract": [], "fulltext": []}


def _problems(doc):
    """The validator's problem list for a criteria document."""
    try:
        return list(criteria_lib.validate_criteria(doc))
    except Exception as exc:  # noqa: BLE001 - a broken validator must not 500
        return [{"level": "error", "code": "validator_failed",
                 "message": str(exc), "where": ""}]


def _errors(problems):
    return [p for p in problems if p.get("level") == "error"]


# --------------------------------------------------------------------------- #
# the routes
# --------------------------------------------------------------------------- #

@bp.route("/api/criteria/validate", methods=["GET", "POST"])
def api_criteria_validate():
    """The problems of a criteria document.

    **POST** with ``{"criteria": {…}}`` checks the posted document - the
    criteria editor does this before it writes.  **GET** (or a POST
    without a body) checks the criteria the active scheme carries now.
    """
    body = _body()
    posted = body.get("criteria")
    if posted is None and "abstract" in body:
        # a bare document is accepted too
        posted = body
    if posted is None:
        doc = _stored_criteria()
    else:
        try:
            doc = criteria_lib.normalise_criteria(posted)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"the criteria could not be read: {exc}", 400)
    problems = _problems(doc)
    errors = _errors(problems)
    return jsonify({
        "ok": not errors,
        "problems": problems,
        "error_count": len(errors),
        "warning_count": len(problems) - len(errors),
        "criteria": doc,
    })
