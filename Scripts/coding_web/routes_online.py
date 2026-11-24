"""Online-project routes (INTERFACES §4, "Flask routes - exact shapes").

The desktop app talks to the studio server through :class:`online.store.OnlineStore`;
this blueprint is the only place that maps HTTP request/response shapes onto
that façade.  It is registered by ``app.py`` and every route here is inert for a
local folder project (``/api/online/status`` says ``mode: "local"``; done marks
go to ``<root>/.studio/done.json`` so "Finish paper" also works without a
server).

Binding contract (see ``Results/INTERFACES_online_projects.md`` §4):

* JSON only; errors are ``{"error": <code>, "message": ...}`` with 400/401/403/
  409 (+ ``holder``)/503.
* ``POST /api/online/open`` switches ``S.ROOT`` to the project's mirror through
  ``State.set_online`` (which resets every root-unaware cache, audit §1.3);
  ``POST /api/online/close`` returns to the folder the user had open.
* The coder tag of a coding/screening/fulltext/harmonizer request must be one
  of the member's tags; the owner may use any tag (audit §4).  The gate is a
  ``before_app_request`` so it runs before the coding routes parse the payload.
* Admin passthroughs are owner-only and call the server's own endpoints.

``.studio/`` (done marks of a local project, the mirror's sync state) is in the
protocol's excluded class: it is never synced, and the app's scans only glob
known extensions, so it never appears as a study.
"""

from __future__ import annotations

import atexit
import json
import os
import tempfile
import threading
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from online_shared import protocol

from online.errors import (
    AuthError,
    BadRequestError,
    ConflictError,
    ForbiddenError,
    LockedError,
    NotConnectedError,
    NotFoundError,
    OfflineError,
    OnlineError,
    ProtocolMismatch,
    RateLimitedError,
    ServerError,
)
from online.store import OnlineStore

from studio_state import S, log, prefetch_progress, run_analysis_locally

bp = Blueprint("online", __name__)

#: The route prefixes whose request carries a coder tag (audit §4).
CODER_ROUTE_PREFIXES = (
    "/api/save",
    "/api/coding/",
    "/api/summary/",
    "/api/harmonizer/",
    "/api/harmonizer_agreement/",
    "/api/assignments/",
    "/api/screening/",
    "/api/fulltext/",
)

#: Publish batches: the contract's push/upload limit.
PUBLISH_BATCH = 1000
#: Blobs up to this size are read into memory (gzip upload); larger files stream.
SMALL_BLOB_BYTES = 8 * 1024 * 1024

_STORE = None
_STORE_LOCK = threading.RLock()
_LOCAL_DONE_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# store plumbing
# ---------------------------------------------------------------------------

def _app_version():
    try:
        from studio_lib import APP_VERSION

        return APP_VERSION
    except Exception:  # noqa: BLE001
        return ""


def get_store():
    """The process-wide store, created on first use."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = OnlineStore(app_version=_app_version())
        return _STORE


def set_store(store):
    """Install *store* (the tests' fake-transport store); returns it."""
    global _STORE
    with _STORE_LOCK:
        _STORE = store
    return store


def current_store():
    """The installed store, or None (used by the coder gate and shutdown)."""
    return _STORE


def shutdown():
    """Close the open online project (presence + state) on app shutdown."""
    store = _STORE
    if store is None:
        return
    try:
        store.close()
    except Exception:  # noqa: BLE001 - shutdown must never raise
        pass


atexit.register(shutdown)


# ---------------------------------------------------------------------------
# JSON errors and the route wrapper
# ---------------------------------------------------------------------------

def _err(code, message="", status=400, **extra):
    body = {"error": code, "message": message or code}
    body.update(extra)
    return jsonify(body), status


def online_route(fn):
    """Run *fn* only with an open online project; translate client errors.

    Each route body takes the store as its first argument.  The mapping is the
    contract's: 401 auth, 403 forbidden, 404 not found, 409 locked (+ holder),
    503 offline, 400 bad request.
    """
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        store = _STORE
        if store is None or not S.online:
            return _err("local", "no online project is open", 400)
        try:
            return fn(store, *args, **kwargs)
        except LockedError as exc:
            return _err("locked", str(exc), 409, holder=exc.holder)
        except NotFoundError as exc:
            return _err("not_found", str(exc), 404)
        except ForbiddenError as exc:
            return _err("forbidden", str(exc), 403)
        except AuthError as exc:
            return _err("unauthorized", str(exc), 401)
        except ProtocolMismatch as exc:
            return _err("protocol_mismatch", str(exc), 426)
        except OfflineError as exc:
            return _err("offline", str(exc), 503)
        except RateLimitedError as exc:
            return _err("rate_limited", str(exc), 429)
        except ConflictError as exc:
            return _err(getattr(exc, "code", "") or "conflict", str(exc), 409)
        except BadRequestError as exc:
            return _err(getattr(exc, "code", "") or "bad_request", str(exc), 400)
        except NotConnectedError as exc:
            return _err("local", str(exc), 400)
        except FileNotFoundError as exc:
            return _err("not_found", str(exc), 404)
        except ValueError as exc:
            return _err("bad_request", str(exc), 400)
        except ServerError as exc:
            return _err(getattr(exc, "code", "") or "server_error", str(exc),
                        getattr(exc, "status", 0) or 502)
        except OnlineError as exc:
            return _err("error", str(exc), 503)

    return wrapper


def _member(store):
    try:
        return store.me() or {}
    except Exception:  # noqa: BLE001
        return {}


def _owner_denied(store):
    me = _member(store)
    if me.get("role") != "owner":
        return _err("forbidden", "only the project owner may do this", 403)
    return None


# ---------------------------------------------------------------------------
# coder identity (audit §4)
# ---------------------------------------------------------------------------

@bp.before_app_request
def guard_coder_tag():
    """Refuse a coder tag outside the member's tags before the route runs.

    Only the routes that take a coder are guarded (a save, a screening/full-text
    decision, a harmonizer or assignment write).  A request without a tag is
    left to the route's own "coder is required" check; the owner may use any
    tag.  Non-online (local) projects keep today's free-text behaviour.
    """
    if not S.online or _STORE is None:
        return None
    path = request.path or ""
    if not any(path.startswith(prefix) for prefix in CODER_ROUTE_PREFIXES):
        return None
    tag = ""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        payload = request.get_json(silent=True)
        if isinstance(payload, dict):
            tag = str(payload.get("coder") or "")
    if not tag:
        tag = str(request.args.get("coder") or "")
    tag = tag.strip()
    if not tag:
        return None
    me = _member(_STORE)
    if me.get("role") == "owner":
        return None
    if tag not in (me.get("tags") or []):
        return _err("forbidden",
                    f"coder tag {tag!r} is not one of your tags ({', '.join(me.get('tags') or [])})",
                    403)
    return None


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def _local_status():
    return {
        "active": False,
        "mode": "local",
        "backend": "local",
        "guarantees": "strong",
        "pending": 0,
        "conflicts": 0,
        "last_sync": None,
        "server": None,
        "user": None,
        "project_id": None,
        "project_name": os.path.basename(S.ROOT) if S.ROOT else None,
        "seq": None,
        "local_only": 0,
        "last_error": None,
        "next_retry_in": None,
        "me": {"role": "owner", "tags": [], "tasks": list(protocol.TASKS)},
        "settings": {},
        "run_analysis_locally": run_analysis_locally(),
        # served through the browser gateway (browser_worker); static/browser.js keys off it
        "browser": bool(getattr(S, "browser", False)),
        "prefetch": prefetch_progress(),
    }


def _online_status(store):
    try:
        status = store.status()
    except Exception as exc:  # noqa: BLE001 - status must always answer
        status = {"mode": "error", "pending": 0, "conflicts": 0, "last_sync": None,
                  "last_error": f"{type(exc).__name__}: {exc}"}
    mirror = getattr(store, "mirror", None)
    state = {}
    if mirror is not None:
        with mirror.lock:
            state = dict(mirror.state)
    payload = dict(status)
    payload.update({
        "active": True,
        "project_name": payload.get("project_name") or state.get("project_name"),
        "seq": payload.get("seq", state.get("seq")),
        "me": state.get("me") or {},
        "settings": state.get("settings") or {},
        "run_analysis_locally": run_analysis_locally(),
        "browser": bool(getattr(S, "browser", False)),
    })
    payload.setdefault("local_only", 0)
    payload.setdefault("last_error", None)
    payload.setdefault("next_retry_in", None)
    payload.setdefault("backend", "server")
    payload.setdefault("guarantees", "strong")
    # the on-demand batch (the AI route codings an agreement view fetches) is
    # reported while it runs, so a long first visit explains itself
    payload["prefetch"] = prefetch_progress()
    return payload


@bp.get("/api/online/status")
def api_status():
    store = _STORE
    if S.online and store is not None:
        return jsonify(_online_status(store))
    return jsonify(_local_status())


# ---------------------------------------------------------------------------
# connect / open / close / logout
# ---------------------------------------------------------------------------

@bp.post("/api/online/connect")
def api_connect():
    payload = request.get_json(force=True, silent=True) or {}
    kind = str(payload.get("kind") or "server").strip().lower()
    username = str(payload.get("username") or "").strip()
    store = get_store()
    if kind in ("nextcloud", "webdav", "nc"):
        return _connect_nextcloud(store, payload, username)
    server = str(payload.get("server") or "").strip()
    password = str(payload.get("password") or "")
    if not server or not username:
        return _err("bad_request", "server and username are required", 400)
    try:
        projects = store.connect(server, username, password)
    except AuthError as exc:
        return _err("unauthorized", str(exc), 401)
    except RateLimitedError as exc:
        return _err("rate_limited", str(exc), 429)
    except OfflineError as exc:
        return _err("offline", str(exc), 503)
    except OnlineError as exc:
        return _err("error", str(exc), 400)
    return jsonify({"projects": projects, "backend": "server", "guarantees": "strong"})


def _connect_nextcloud(store, payload, username):
    """``{kind: "nextcloud", url, username, app_password|password, folder}``.

    The same client and the same engine; the shared folder is the project.  The
    answer carries ``guarantees: "best-effort"`` so the badge can say what this
    mode promises (PLAN §13).
    """
    url = str(payload.get("url") or payload.get("server") or "").strip()
    folder = str(payload.get("folder") or "").strip()
    app_password = str(payload.get("app_password") or payload.get("password") or "")
    webdav_root = str(payload.get("webdav_root") or "").strip() or None
    if not url or not username or not app_password:
        return _err("bad_request", "url, username and app password are required", 400)
    try:
        projects = store.connect_nextcloud(url, username, app_password, folder,
                                           webdav_root=webdav_root)
    except ValueError as exc:                       # bad URL, plain http to a remote host
        return _err("bad_request", str(exc), 400)
    except AuthError as exc:
        return _err("unauthorized", str(exc), 401)
    except RateLimitedError as exc:
        return _err("rate_limited", str(exc), 429)
    except OfflineError as exc:
        return _err("offline", str(exc), 503)
    except ForbiddenError as exc:
        return _err("forbidden", str(exc), 403)
    except NotFoundError as exc:
        return _err("not_found", f"{exc} (check the folder name)", 404)
    except OnlineError as exc:
        return _err("error", str(exc), 400)
    return jsonify({"projects": projects, "backend": "nextcloud", "guarantees": "best-effort"})


@bp.post("/api/online/open")
def api_open():
    payload = request.get_json(force=True, silent=True) or {}
    project_id = str(payload.get("project_id") or "").strip()
    if not project_id:
        return _err("bad_request", "project_id is required", 400)
    store = _STORE
    if store is None or store.client is None:
        return _err("unauthorized", "connect to a server first", 401)
    try:
        root = store.open(project_id, analysis_prefetch=run_analysis_locally())
    except AuthError as exc:
        return _err("unauthorized", str(exc), 401)
    except OfflineError as exc:
        # A mirror that already exists still opens offline (INTERFACES §4).
        return _err("offline", str(exc), 503)
    except OnlineError as exc:
        return _err("error", str(exc), 400)
    S.set_online(store, root)
    log(f"online project opened: {project_id} at {root}")
    return jsonify({"root": S.ROOT, "status": _online_status(store)})


@bp.post("/api/online/close")
def api_close():
    store = _STORE
    if store is not None:
        try:
            store.close()
        except Exception:  # noqa: BLE001 - closing must not fail the request
            pass
    root = S.clear_online()
    log(f"online project closed -> {root}")
    return jsonify({"root": root, "status": _local_status()})


@bp.post("/api/online/logout")
def api_logout():
    store = _STORE
    if store is not None:
        try:
            store.logout()
        except Exception:  # noqa: BLE001
            pass
    root = S.clear_online() if S.online else S.ROOT
    return jsonify({"ok": True, "root": root, "status": _local_status()})


# ---------------------------------------------------------------------------
# sync / presence
# ---------------------------------------------------------------------------

@bp.post("/api/online/sync")
@online_route
def api_sync(store):
    result = store.sync_now()
    # A pull can reveal a paper (Text/ is core, so it lands in the mirror): the
    # key list must follow, because 16 routes refuse a key outside S.KEYS.
    try:
        from studio_state import refresh_keys

        refresh_keys()
    except Exception:  # noqa: BLE001
        pass
    return jsonify(result)


@bp.get("/api/online/presence")
def api_presence():
    store = _STORE
    if not S.online or store is None:
        return jsonify({"others": []})
    key = (request.args.get("key") or "").strip() or None
    tab = (request.args.get("tab") or "").strip() or None
    try:
        others = store.presence(key, tab)
    except OfflineError:
        return jsonify({"others": [], "offline": True})
    except OnlineError as exc:
        return _err("error", str(exc), 502)
    return jsonify({"others": others})


# ---------------------------------------------------------------------------
# locks
# ---------------------------------------------------------------------------

def _username(store) -> str:
    """The logged-in username of the open project (the server's lock identity)."""
    name = getattr(store, "username", None)
    if name:
        return str(name)
    mirror = getattr(store, "mirror", None)
    if mirror is not None:
        with mirror.lock:
            return str(mirror.state.get("username") or "")
    return ""


def _lock_row(store, lock):
    """One server lock plus ``mine``, decided against the logged-in username.

    The server identifies a holder by ``user`` (username) and ``user_id``; the
    UI must not guess identity from a display name (two members can share one).
    ``user``, ``user_id``, ``display_name`` and ``client_id`` pass through as
    the server sent them.
    """
    row = dict(lock or {})
    if not isinstance(row.get("mine"), bool):
        who = _username(store)
        row["mine"] = bool(who) and str(row.get("user") or "") == who
    return row


@bp.get("/api/online/locks")
@online_route
def api_locks(store):
    return jsonify({"locks": [_lock_row(store, lock) for lock in store.locks()]})


@bp.post("/api/online/locks/<path:key>")
@online_route
def api_lock_acquire(store, key):
    payload = request.get_json(force=True, silent=True) or {}
    hours = payload.get("offline_hours")
    try:
        hours = float(hours) if hours not in (None, "") else None
    except (TypeError, ValueError):
        return _err("bad_request", "offline_hours must be a number", 400)
    return jsonify({"lock": _lock_row(store, store.acquire_lock(key, hours))})


@bp.delete("/api/online/locks/<path:key>")
@online_route
def api_lock_release(store, key):
    force = str(request.args.get("force", "")).lower() in ("1", "true", "yes")
    store.release_lock(key, force=force)
    return jsonify({"ok": True, "key": key})


@bp.delete("/api/online/admin/locks/<path:key>")
@online_route
def api_admin_lock_force(store, key):
    denied = _owner_denied(store)
    if denied:
        return denied
    store.release_lock(key, force=True)
    return jsonify({"ok": True, "key": key})


# ---------------------------------------------------------------------------
# done (also for local folder projects: <root>/.studio/done.json)
# ---------------------------------------------------------------------------

def _local_done_path(root=None):
    root = root or S.ROOT
    return os.path.join(root, ".studio", "done.json")


def _local_done_read():
    path = _local_done_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    marks = data.get("marks") if isinstance(data, dict) else data
    out = []
    for row in marks or []:
        if isinstance(row, dict) and row.get("key") and row.get("stage"):
            out.append({"key": str(row["key"]), "stage": str(row["stage"]),
                        "tag": str(row.get("tag") or "local")})
    return out


def _local_done_write(marks):
    path = _local_done_path()
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".done-", suffix=".json", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "marks": marks}, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        tmp = ""
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


@bp.post("/api/online/done")
def api_done_mark():
    payload = request.get_json(force=True, silent=True) or {}
    key = str(payload.get("key") or "").strip()
    stage = str(payload.get("stage") or "").strip()
    tag = str(payload.get("tag") or "").strip()
    if not key or not stage:
        return _err("bad_request", "key and stage are required", 400)
    if stage not in protocol.STAGES:
        return _err("bad_request", f"stage must be one of {', '.join(protocol.STAGES)}", 400)
    if S.online and _STORE is not None:
        try:
            _STORE.mark_done(key, stage, tag or None)
        except ValueError as exc:
            return _err("bad_request", str(exc), 400)
        except LockedError as exc:
            return _err("locked", str(exc), 409, holder=exc.holder)
        except ForbiddenError as exc:
            return _err("forbidden", str(exc), 403)
        except OfflineError as exc:
            return _err("offline", str(exc), 503)
        except OnlineError as exc:
            return _err("error", str(exc), 502)
        return jsonify({"ok": True, "key": key, "stage": stage, "tag": tag})
    with _LOCAL_DONE_LOCK:
        marks = _local_done_read()
        row = {"key": key, "stage": stage, "tag": tag or "local"}
        if row not in marks:
            marks.append(row)
            _local_done_write(marks)
    return jsonify({"ok": True, **row})


@bp.get("/api/online/done")
def api_done_list():
    stage = (request.args.get("stage") or "").strip() or None
    tag = (request.args.get("tag") or "").strip() or None
    if S.online and _STORE is not None:
        try:
            rows = _STORE.done_list(stage=stage, tag=tag)
        except OfflineError as exc:
            return _err("offline", str(exc), 503)
        except OnlineError as exc:
            return _err("error", str(exc), 502)
    else:
        with _LOCAL_DONE_LOCK:
            rows = _local_done_read()
        if stage:
            rows = [r for r in rows if r["stage"] == stage]
        if tag:
            rows = [r for r in rows if r["tag"] == tag]
    return jsonify({"done": rows})


# ---------------------------------------------------------------------------
# assignments / conflicts
# ---------------------------------------------------------------------------

@bp.get("/api/online/assignments")
@online_route
def api_assignments(store):
    stage = (request.args.get("stage") or "").strip() or None
    tag = (request.args.get("tag") or "").strip() or None
    return jsonify({"assignments": store.assignments(stage=stage, tag=tag)})


@bp.get("/api/online/conflicts")
@online_route
def api_conflicts(store):
    return jsonify({"conflicts": store.conflicts("open")})


def _text(data):
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


@bp.get("/api/online/conflicts/<cid>")
@online_route
def api_conflict_detail(store, cid):
    syncer = store.syncer
    record = syncer.conflicts.get(cid) if syncer is not None else None
    if record is None:
        return _err("not_found", f"no conflict {cid}", 404)
    client, mirror, _s = store._require()
    mine = theirs = base = b""
    try:
        mine = syncer.conflicts.mine_bytes(cid) or b""
    except OSError:
        mine = b""
    for digest, target in ((record.get("server_hash"), "theirs"),
                           (record.get("base_hash"), "base")):
        if not digest:
            continue
        try:
            data = client.get_blob_bytes(store.pid, digest)
        except OnlineError:
            data = b""
        if target == "theirs":
            theirs = data
        else:
            base = data
    return jsonify({"id": record.get("id"), "path": record.get("path"),
                    "kind": record.get("kind"), "reason": record.get("reason"),
                    "mine_text": _text(mine), "theirs_text": _text(theirs),
                    "base_text": _text(base)})


@bp.post("/api/online/conflicts/<cid>/resolve")
@online_route
def api_conflict_resolve(store, cid):
    payload = request.get_json(force=True, silent=True) or {}
    resolution = str(payload.get("resolution") or "").strip()
    if resolution not in ("mine", "theirs", "merged"):
        return _err("bad_request", "resolution must be mine, theirs or merged", 400)
    merged = payload.get("merged_text")
    merged_bytes = merged.encode("utf-8") if isinstance(merged, str) else None
    if resolution == "merged" and merged_bytes is None:
        return _err("bad_request", "merged_text is required for a merged resolution", 400)
    try:
        record = store.resolve_conflict(cid, resolution, merged_bytes)
    except KeyError:
        return _err("not_found", f"no conflict {cid}", 404)
    return jsonify({"ok": True, "conflict": record})


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

def _cache_payload(settings):
    usage = settings.get("usage") or {}
    used = sum(int(usage.get(k) or 0) for k in ("core", "pinned", "lru", "supplement"))
    payload = {"limit_mb": settings.get("limit_mb"),
               "used_mb": round(used / (1024 * 1024), 1),
               "supplements": bool(settings.get("supplements")),
               "pinned": settings.get("pinned") or [],
               "assigned": settings.get("assigned") or [],
               "run_analysis_locally": run_analysis_locally(),
               "usage": usage}
    # The Datasets products a local analysis reads are on-demand in the core
    # tier, so the switch's download is reported here (MB, still missing).
    store = _STORE
    if store is not None:
        try:
            analysis = store.analysis_download()
        except Exception:  # noqa: BLE001 - the cache panel must still answer
            analysis = None
        if analysis is not None:
            payload["analysis"] = {
                "files": analysis["files"],
                "pending": analysis["pending"],
                "mb": round(analysis["bytes"] / (1024 * 1024), 1),
                "local_mb": round(analysis["local_bytes"] / (1024 * 1024), 1),
            }
    return payload


@bp.get("/api/online/cache")
def api_cache_get():
    if not S.online or _STORE is None:
        return jsonify({"limit_mb": protocol.DEFAULT_CACHE_MB, "used_mb": 0,
                        "supplements": False, "pinned": [], "assigned": [], "usage": {}})
    try:
        return jsonify(_cache_payload(_STORE.cache_settings()))
    except OfflineError as exc:
        return _err("offline", str(exc), 503)
    except OnlineError as exc:
        return _err("error", str(exc), 502)


@bp.post("/api/online/cache")
@online_route
def api_cache_set(store):
    payload = request.get_json(force=True, silent=True) or {}
    try:
        limit = float(payload["limit_mb"]) if payload.get("limit_mb") not in (None, "") else None
    except (TypeError, ValueError):
        return _err("bad_request", "limit_mb must be a number", 400)
    supplements = payload.get("supplements")
    settings = store.set_cache_settings(
        limit_mb=limit, supplements=bool(supplements) if supplements is not None else None)
    return jsonify(_cache_payload(settings))


@bp.post("/api/online/cache/pin")
@online_route
def api_cache_pin(store):
    keys = _keys_payload()
    if keys is None:
        return _err("bad_request", "keys must be a list of study keys", 400)
    store.pin(keys)
    return jsonify(_cache_payload(store.cache_settings()))


@bp.post("/api/online/cache/unpin")
@online_route
def api_cache_unpin(store):
    keys = _keys_payload()
    if keys is None:
        return _err("bad_request", "keys must be a list of study keys", 400)
    store.unpin(keys)
    return jsonify(_cache_payload(store.cache_settings()))


def _keys_payload():
    payload = request.get_json(force=True, silent=True) or {}
    keys = payload.get("keys")
    if not isinstance(keys, list):
        return None
    return [str(k) for k in keys if str(k).strip()]


@bp.post("/api/online/cache/clear")
@online_route
def api_cache_clear(store):
    cleared = store.clear_cache()
    return jsonify({"ok": True, "cleared": cleared, **_cache_payload(store.cache_settings())})


@bp.post("/api/online/cache/preload")
@online_route
def api_cache_preload(store):
    payload = request.get_json(force=True, silent=True) or {}
    keys = payload.get("keys")
    if keys is not None and not isinstance(keys, list):
        return _err("bad_request", "keys must be a list of study keys", 400)
    files = store.preload([str(k) for k in keys] if keys else None)
    return jsonify({"ok": True, "files": files, "n": len(files)})


# ---------------------------------------------------------------------------
# admin passthrough (owner only)
# ---------------------------------------------------------------------------

@bp.get("/api/online/admin/members")
@online_route
def api_admin_members(store):
    denied = _owner_denied(store)
    if denied:
        return denied
    return jsonify({"members": store.client.members(store.pid)})


@bp.put("/api/online/admin/members/<int:uid>")
@online_route
def api_admin_member_put(store, uid):
    denied = _owner_denied(store)
    if denied:
        return denied
    payload = request.get_json(force=True, silent=True) or {}
    return jsonify(store.client.put_member(store.pid, uid, payload) or {})


@bp.delete("/api/online/admin/members/<int:uid>")
@online_route
def api_admin_member_delete(store, uid):
    denied = _owner_denied(store)
    if denied:
        return denied
    store.client.delete_member(store.pid, uid)
    return jsonify({"ok": True, "user_id": uid})


@bp.get("/api/online/admin/settings")
@online_route
def api_admin_settings_get(store):
    denied = _owner_denied(store)
    if denied:
        return denied
    info = store.client.project(store.pid)
    return jsonify({"settings": info.get("settings") or {}})


@bp.patch("/api/online/admin/settings")
@online_route
def api_admin_settings_patch(store):
    denied = _owner_denied(store)
    if denied:
        return denied
    payload = request.get_json(force=True, silent=True) or {}
    if not isinstance(payload, dict) or not payload:
        return _err("bad_request", "a settings object is required", 400)
    settings = store.client.update_settings(store.pid, payload)
    return jsonify({"settings": settings})


@bp.post("/api/online/admin/assignments/<action>")
@online_route
def api_admin_assignments(store, action):
    denied = _owner_denied(store)
    if denied:
        return denied
    payload = request.get_json(force=True, silent=True) or {}
    if action == "preview":
        return jsonify(store.client.assignment_preview(store.pid, payload))
    if action == "commit":
        return jsonify(store.client.assignment_commit(store.pid, payload))
    if action == "manual":
        return jsonify(store.client.assignment_manual(store.pid, payload))
    return _err("not_found", f"unknown assignment action {action!r}", 404)


def _run_row(run):
    """One assignment run in the shape the admin panel renders.

    The server sends ``{run_id, stage, params, created_by, ts, undone}`` and
    keeps the mode and the paper count inside ``params``; the panel needs both
    at the top level, so they are derived here (the originals stay untouched).
    """
    row = dict(run or {})
    params = row.get("params") if isinstance(row.get("params"), dict) else {}
    if not row.get("mode"):
        row["mode"] = str(params.get("mode") or "")
    n = params.get("n_keys")
    if n is None and isinstance(params.get("keys"), int):
        n = params["keys"]
    if row.get("n_keys") is None and n is not None:
        row["n_keys"] = int(n)
    return row


@bp.get("/api/online/admin/assignments/runs")
@online_route
def api_admin_assignment_runs(store):
    denied = _owner_denied(store)
    if denied:
        return denied
    runs = [_run_row(run) for run in store.client.assignment_runs(store.pid)]
    return jsonify({"runs": runs})


@bp.delete("/api/online/admin/assignments/runs/<run_id>")
@online_route
def api_admin_assignment_run_delete(store, run_id):
    denied = _owner_denied(store)
    if denied:
        return denied
    store.client.assignment_run_delete(store.pid, run_id)
    return jsonify({"ok": True, "run_id": run_id})


@bp.get("/api/online/admin/users")
@online_route
def api_admin_users(store):
    denied = _owner_denied(store)
    if denied:
        return denied
    return jsonify({"users": store.client.admin_users()})


@bp.post("/api/online/admin/users")
@online_route
def api_admin_user_create(store):
    denied = _owner_denied(store)
    if denied:
        return denied
    payload = request.get_json(force=True, silent=True) or {}
    return jsonify(store.client.admin_create_user(payload) or {})


@bp.patch("/api/online/admin/users/<int:uid>")
@online_route
def api_admin_user_patch(store, uid):
    denied = _owner_denied(store)
    if denied:
        return denied
    payload = request.get_json(force=True, silent=True) or {}
    return jsonify(store.client.admin_patch_user(uid, payload) or {})


@bp.get("/api/online/admin/audit")
@online_route
def api_admin_audit(store):
    denied = _owner_denied(store)
    if denied:
        return denied
    try:
        limit = int(request.args.get("limit") or 100)
    except (TypeError, ValueError):
        limit = 100
    return jsonify({"audit": store.client.audit(store.pid, limit=limit)})


# ---------------------------------------------------------------------------
# admin publish: push a local folder into the open server project
# ---------------------------------------------------------------------------
#
# The source folder is the user's own checkout (12 GB for the real corpus), so
# nothing is copied into the mirror: each file is hashed in place, its blob is
# uploaded only if the server lacks it (``blobs/missing``), and the entries are
# pushed in batches of <= 1000.  Running it again resumes: a blob that already
# arrived is skipped and a head that already matches is not pushed.

_PUBLISH_LOCK = threading.Lock()
_PUBLISH = {
    "running": False, "ok": None, "source_root": None, "started": None,
    "finished": None, "total": 0, "hashed": 0, "uploaded": 0, "pushed": 0,
    "skipped": 0, "conflicts": 0, "bytes": 0, "errors": [], "message": "",
}


def _publish_state(**changes):
    with _PUBLISH_LOCK:
        _PUBLISH.update(changes)
        return dict(_PUBLISH)


def _publish_phase(snap):
    if snap.get("running"):
        return "running"
    if snap.get("ok") is True:
        return "published"
    if snap.get("ok") is False:
        return "failed"
    if snap.get("finished"):
        return "cancelled"
    return "idle"


def _publish_snapshot():
    """The progress object ``admin_online.js`` renders.

    Beyond the worker's counters it carries ``state`` (running|published|
    failed|cancelled|idle), ``done`` (files processed) and ``bytes``, so the
    panel needs no guesswork about which counter means progress.
    """
    with _PUBLISH_LOCK:
        out = dict(_PUBLISH)
        out["errors"] = list(_PUBLISH["errors"])
    out["state"] = _publish_phase(out)
    out["done"] = int(out.get("hashed") or 0)
    out["bytes"] = int(out.get("bytes") or 0)
    return out


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _publish_files(source_root):
    """``[(relpath, abspath)]`` of every non-excluded file under *source_root*."""
    out = []
    for dirpath, dirnames, names in os.walk(source_root):
        rel_dir = os.path.relpath(dirpath, source_root).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""
        prefix = rel_dir + "/" if rel_dir else ""
        dirnames[:] = sorted(d for d in dirnames
                             if protocol.classify(prefix + d + "/").kind != "excluded")
        for name in sorted(names):
            rel = prefix + name
            if protocol.classify(rel).kind == "excluded":
                continue
            out.append((rel, os.path.join(dirpath, name)))
    return out


def _publish_worker(store, source_root):
    errors = []
    try:
        files = _publish_files(source_root)
        _publish_state(total=len(files), hashed=0, uploaded=0, pushed=0,
                       skipped=0, conflicts=0, bytes=0, errors=[], ok=None,
                       message=f"hashing {len(files)} files")
        by_hash = {}
        entries = []
        known = {}
        try:
            known = store._known_files()
        except Exception:  # noqa: BLE001 - publish can still create files
            known = {}
        for rel, full in files:
            with _PUBLISH_LOCK:
                if not _PUBLISH["running"]:
                    raise RuntimeError("publish cancelled")
            try:
                digest = protocol.sha256_file(full)
                size = os.path.getsize(full)
            except OSError as exc:
                errors.append(f"{rel}: {exc}")
                continue
            by_hash.setdefault(digest, full)
            head = (known.get(rel) or {}).get("hash")
            if head == digest:
                with _PUBLISH_LOCK:
                    _PUBLISH["skipped"] += 1
                    _PUBLISH["hashed"] += 1
                    _PUBLISH["bytes"] += size
                continue
            entries.append({"path": rel, "base_hash": head, "new_hash": digest, "size": size})
            with _PUBLISH_LOCK:
                _PUBLISH["hashed"] += 1
                _PUBLISH["bytes"] += size
        _publish_state(message=f"uploading blobs for {len(entries)} files")
        client, mirror, _syncer = store._require()
        pid = store.pid
        client_id = mirror.state.get("client_id")
        for i in range(0, len(entries), PUBLISH_BATCH):
            batch = entries[i:i + PUBLISH_BATCH]
            wanted = sorted({e["new_hash"] for e in batch})
            missing = set(client.blobs_missing(pid, wanted))
            for digest in sorted(missing):
                full = by_hash.get(digest)
                if not full:
                    continue
                client.put_blob_file(pid, digest, full)
                with _PUBLISH_LOCK:
                    _PUBLISH["uploaded"] += 1
        _publish_state(message=f"pushing {len(entries)} entries")
        for i in range(0, len(entries), PUBLISH_BATCH):
            batch = entries[i:i + PUBLISH_BATCH]
            resp = client.push(pid, client_id, batch) or {}
            for result in resp.get("results") or []:
                status = result.get("status")
                if status in ("accepted", "merged"):
                    with _PUBLISH_LOCK:
                        _PUBLISH["pushed"] += 1
                elif status == "conflict":
                    with _PUBLISH_LOCK:
                        _PUBLISH["conflicts"] += 1
                    errors.append(f"{result.get('path')}: conflict")
                elif status == "rejected":
                    errors.append(f"{result.get('path')}: rejected ({result.get('reason')})")
            with _PUBLISH_LOCK:
                _PUBLISH["message"] = (f"pushed {_PUBLISH['pushed']} of {len(entries)}")
        ok = not errors
        _publish_state(ok=ok, message=("published" if ok else f"{len(errors)} problem(s)"),
                       errors=errors[:50])
        try:
            store.kick()
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001 - the worker must report, not crash
        errors.append(f"{type(exc).__name__}: {exc}")
        _publish_state(ok=False, errors=errors[:50], message=str(exc))
    finally:
        _publish_state(running=False, finished=_now_iso())


@bp.post("/api/online/admin/publish")
@online_route
def api_admin_publish(store):
    denied = _owner_denied(store)
    if denied:
        return denied
    payload = request.get_json(force=True, silent=True) or {}
    source = str(payload.get("source_root") or "").strip().strip('"')
    if not source or not os.path.isdir(source):
        return _err("bad_request", "source_root must be an existing folder", 400)
    source = os.path.abspath(source)
    with _PUBLISH_LOCK:
        if _PUBLISH["running"]:
            return _err("conflict", "a publish is already running", 409)
        _PUBLISH.update(running=True, ok=None, source_root=source, started=_now_iso(),
                        finished=None, total=0, hashed=0, uploaded=0, pushed=0,
                        skipped=0, conflicts=0, bytes=0, errors=[], message="starting")
    worker = threading.Thread(target=_publish_worker, args=(store, source),
                              name="online-publish", daemon=True)
    worker.start()
    return jsonify({"ok": True, "started": True, "source_root": source,
                    "progress": _publish_snapshot()})


@bp.get("/api/online/admin/publish")
@online_route
def api_admin_publish_progress(store):
    denied = _owner_denied(store)
    if denied:
        return denied
    return jsonify(_publish_snapshot())


@bp.post("/api/online/admin/publish/cancel")
@online_route
def api_admin_publish_cancel(store):
    denied = _owner_denied(store)
    if denied:
        return denied
    _publish_state(running=False, message="cancelling")
    return jsonify({"ok": True})
