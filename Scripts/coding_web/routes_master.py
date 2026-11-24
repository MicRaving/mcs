"""Master-workbook routes: rebuild the derived workbook and report its status.

``master_dataset.xlsx`` is a derived product (plan §11).  These two routes are
the app's handle on its compiler; the decisions themselves are saved by the
screening/full-text routes, which call :func:`master_job.schedule` so the
workbook (and therefore the PRISMA numbers) follows the decision files.

* ``GET  /api/master/status``  - whether this project compiles, the debounce,
  the last run (ok, seconds, row count, peak memory), pending flag.
* ``POST /api/master/compile`` - request a rebuild (debounced background); with
  ``{"wait": true}`` it blocks and returns the compile summary instead.
"""

from flask import Blueprint, jsonify, request

import master_job

bp = Blueprint("master", __name__)


@bp.get("/api/master/status")
def api_master_status():
    return jsonify(master_job.status())


@bp.post("/api/master/compile")
def api_master_compile():
    payload = request.get_json(force=True, silent=True) or {}
    if not master_job.enabled():
        return jsonify({"ok": False, "enabled": False,
                        "error": "this project does not use a derived master "
                                 "(no Project/master_base.xlsx, or not your build to run)"}), 409
    if payload.get("wait"):
        result = master_job.compile_now()
        if not result.get("ok"):
            return jsonify(result), 500
        return jsonify(result)
    master_job.schedule(payload.get("reason") or "manual", delay=0)
    return jsonify({"ok": True, "scheduled": True, "status": master_job.status()})
