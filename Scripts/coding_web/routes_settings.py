"""Settings routes: the project's AGENTS.md, and the in-app updater.

The Settings view is meant to grow; it starts with the two things that exist -
the documentation an agent reads when it opens the project folder, and the
application's own updates.
"""

import os

from flask import Blueprint, jsonify, request

import project_context
import project_doc
import updater
from studio_state import log

bp = Blueprint("settings", __name__)


def _root():
    try:
        return project_context.active_root()
    except Exception as exc:  # noqa: BLE001
        log(f"settings: no active project ({exc})")
        return ""


def _body():
    data = request.get_json(force=True, silent=True)
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- #
# the project's AGENTS.md
# --------------------------------------------------------------------------- #

@bp.get("/api/settings/project")
def api_settings_project():
    """The project's AGENTS.md, plus the template it would be written from."""
    root = _root()
    path = project_doc.path_for(root) if root else ""
    return jsonify({
        "root": root,
        "path": path,
        "exists": bool(path) and os.path.isfile(path),
        "text": project_doc.read(root) if root else "",
        "template": project_doc.template(root) if root else "",
    })


@bp.post("/api/settings/project")
def api_settings_project_save():
    """Save the AGENTS.md of the open project (nothing outside it is touched)."""
    root = _root()
    if not root or not os.path.isdir(root):
        return jsonify({"ok": False, "error": "no project is open"}), 400
    text = str(_body().get("text") or "")
    try:
        path = project_doc.write(root, text)
    except OSError as exc:
        return jsonify({"ok": False, "error": f"the file could not be written: {exc}"}), 500
    return jsonify({"ok": True, "path": path,
                    "bytes": len(text.encode("utf-8"))})


@bp.post("/api/settings/project/reset")
def api_settings_project_reset():
    """Replace the AGENTS.md with a fresh template for this project."""
    root = _root()
    if not root or not os.path.isdir(root):
        return jsonify({"ok": False, "error": "no project is open"}), 400
    text = project_doc.template(root)
    try:
        path = project_doc.write(root, text)
    except OSError as exc:
        return jsonify({"ok": False, "error": f"the file could not be written: {exc}"}), 500
    return jsonify({"ok": True, "path": path, "text": text})


# --------------------------------------------------------------------------- #
# updates
# --------------------------------------------------------------------------- #

@bp.get("/api/update/state")
def api_update_state():
    """The current version, the last check and any download in progress."""
    updater._maybe_auto_check()
    return jsonify(updater.state())


@bp.post("/api/update/check")
def api_update_check():
    return jsonify(updater.check(force=True))


@bp.post("/api/update/download")
def api_update_download():
    result = updater.download_async()
    return jsonify(result), (200 if result.get("ok") else 400)


@bp.post("/api/update/apply")
def api_update_apply():
    """Install the downloaded build and restart into it."""
    result = updater.apply()
    return jsonify(result), (200 if result.get("ok") else 400)
