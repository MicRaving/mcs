"""Scheme routes: the active project's coding scheme (read, edit, validate).

A *scheme* is the user-editable description of what a coder records.  It is
read through ``Scripts/project_context.py`` (which project is active?) and
edited through ``Scripts/scheme_lib.py`` (what is a scheme?).

Rules of the road:

* every write route validates the **whole** scheme before it touches the disk,
  so a half-finished edit can never land in ``Project/scheme.json``;
* every failure is answered with a JSON body - never an HTML traceback;
* the scheme is the project's, so re-rooting the app (``POST /api/root``)
  switches the scheme with the project.

The legacy ``/api/schema`` route (in ``routes_core``) still serves the v3.1
shape for the agent routes and the manuscript report; this blueprint serves the
scheme itself.
"""

import json
import os
import sys
import tempfile

from flask import Blueprint, jsonify, request
from werkzeug.exceptions import HTTPException

from studio_state import S

# The shared project layer lives one level up (Scripts/): ``project_context``
# answers "which project is active?" and ``scheme_lib`` knows what a scheme is.
try:
    import project_context  # noqa: E402
    import scheme_lib  # noqa: E402
except Exception:  # pragma: no cover - source layout fallback
    for _d in (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
               os.path.join(S.ROOT, "Scripts")):
        if _d and _d not in sys.path:
            sys.path.insert(0, _d)
    import project_context  # noqa: E402
    import scheme_lib  # noqa: E402

bp = Blueprint("scheme", __name__)


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


def _scheme():
    """The active project's scheme, normalised."""
    return project_context.scheme()


def _editable():
    """The scheme document the edit routes work on.

    A project that carries its own scheme is edited in place; a project
    without one starts from the legacy schema, converted on the fly - the
    first edit promotes it to a real ``Project/scheme.json``.
    """
    return _scheme()


def _problems(scheme):
    """The validator's problem list."""
    try:
        return list(scheme_lib.validate_scheme(scheme))
    except Exception as exc:  # noqa: BLE001 - a broken validator must not 500
        return [{"level": "error", "code": "validator_failed",
                 "message": str(exc), "where": ""}]


def _errors(problems):
    return [p for p in problems if p.get("level") == "error"]


def _save(scheme):
    """Write a scheme.

    Returns ``None`` when the scheme was written, or a ``(response, status)``
    tuple that the caller returns unchanged.
    """
    problems = _problems(scheme)
    if _errors(problems):
        return _fail("the scheme has structural errors", 400, problems=problems)
    try:
        project_context.write_scheme(scheme)
    except ValueError as exc:
        return _fail(str(exc), 400, problems=problems)
    except OSError as exc:
        return _fail(f"could not write the scheme: {exc}", 500, problems=problems)
    return None


def _field_names(scheme):
    """Every field name of the scheme (all scopes)."""
    names = set()
    for field in scheme_lib.all_fields(scheme):
        names.add(field.get("name"))
    for group in scheme.get("groups") or []:
        names.add(group.get("name"))
    return names


def _find_field(scheme, name):
    """``(bucket, index, field)`` of the field called ``name``.

    ``bucket`` is ``"study"``, ``"metric"`` or ``"module:<key>"``.
    """
    for i, field in enumerate(scheme.get("fields") or []):
        if field.get("name") == name:
            return "study", i, field
    metrics = scheme.get("metrics") or {}
    for i, field in enumerate(metrics.get("fields") or []):
        if field.get("name") == name:
            return "metric", i, field
    for key, mod in (scheme.get("modules") or {}).items():
        for i, field in enumerate(mod.get("fields") or []):
            if field.get("name") == name:
                return f"module:{key}", i, field
    return None, None, None


def _module_fields(scheme, key):
    mod = (scheme.get("modules") or {}).get(key)
    return mod.get("fields") if isinstance(mod, dict) else None


# --------------------------------------------------------------------------- #
# project
# --------------------------------------------------------------------------- #

@bp.get("/api/project")
def api_project():
    """The active project: its root, its ``Project/`` folder, its manifest."""
    root = project_context.active_root()
    paths = project_context.project_paths(root)
    scheme = _scheme()
    return jsonify({
        "root": root,
        "project_dir": paths["project_dir"],
        "manifest": project_context.manifest(root) or {},
        "scheme": {
            "id": scheme.get("id"),
            "name": scheme.get("name"),
            "label": scheme.get("label"),
            "version": scheme.get("scheme_version"),
        },
    })


# --------------------------------------------------------------------------- #
# the scheme
# --------------------------------------------------------------------------- #

@bp.get("/api/scheme")
def api_scheme():
    """The active project's scheme, normalised."""
    return jsonify(_scheme())


@bp.put("/api/scheme")
def api_put_scheme():
    """Replace the whole scheme.

    The body *is* the scheme document.  It is validated as a whole; a
    structural error is answered with 400 and the problem list, and nothing is
    written.
    """
    doc = _body()
    if not doc:
        return _fail("the request body must be the scheme document", 400)
    try:
        normalised = scheme_lib.normalise_scheme(doc)
    except Exception as exc:  # noqa: BLE001
        return _fail(f"the scheme could not be read: {exc}", 400)
    problems = _problems(normalised)
    if _errors(problems):
        return _fail("the scheme has structural errors", 400, problems=problems)
    try:
        project_context.write_scheme(normalised)
    except ValueError as exc:
        return _fail(str(exc), 400, problems=problems)
    except OSError as exc:
        return _fail(f"could not write the scheme: {exc}", 500, problems=problems)
    return jsonify({"ok": True, "problems": problems, "scheme": normalised})


@bp.get("/api/scheme/blocks")
def api_scheme_blocks():
    """The blocks a coder sees: the groups and the modules, in order."""
    scheme = _scheme()
    return jsonify({"blocks": scheme_lib.derive_blocks(scheme),
                    "groups": scheme.get("groups") or []})


# --------------------------------------------------------------------------- #
# fields
# --------------------------------------------------------------------------- #

def _check_field_spec(spec, scheme):
    """Validate an incoming field spec; returns a list of problem dicts."""
    problems = []

    def add(code, message):
        problems.append({"level": "error", "code": code, "message": message,
                         "where": f"field '{spec.get('name')}'"})

    field_type = spec.get("type")
    if field_type not in (None, ""):
        base = str(field_type).split("|")[0].strip()
        if base not in scheme_lib.FIELD_TYPES:
            add("unknown_type", f"'{field_type}' is not a known field type "
                                f"(known: {', '.join(scheme_lib.FIELD_TYPES)}).")
    editor = spec.get("editor")
    if editor not in (None, ""):
        if str(editor).strip().lower() not in scheme_lib.EDITORS:
            add("unknown_editor", f"'{editor}' is not a known editor "
                                  f"(known: {', '.join(scheme_lib.EDITORS)}).")
    role = spec.get("role")
    if role not in (None, ""):
        if scheme_lib.normalise_role(str(role)) is None:
            add("unknown_role", f"'{role}' is not a known role.")
    kind = spec.get("kind")
    if kind not in (None, ""):
        known = {k.get("id") for k in (scheme.get("kinds") or [])}
        if str(kind) not in known:
            add("unknown_kind", f"'{kind}' is not a known kind.")
    names = _field_names(scheme)
    for key in ("show_if", "required_if"):
        cond = spec.get(key)
        if not isinstance(cond, dict):
            continue
        target = str(cond.get("field") or "").strip()
        if target and target not in names:
            add("unknown_condition_target",
                f"the {key} of '{spec.get('name')}' points at the unknown "
                f"field '{target}'.")
    return problems


@bp.post("/api/scheme/fields")
def api_scheme_fields():
    """Add a field, or update the field that already carries the name.

    Body: the field itself (``name``, ``label``, ``group``, ``scope``,
    ``type``, ``editor``, ``options``, ``role``, ``required``, ``moderator``,
    ``description``, ``instruction``, ``show_if``, ``required_if``,
    ``module``, ``columns``, ``kind``).  The scope decides where the field
    lands: ``study`` (default), ``metric`` or ``module`` (with ``module``
    naming the module).
    """
    spec = _body()
    name = str(spec.get("name") or "").strip()
    if not name:
        return _fail("the field needs a name", 400)
    spec = dict(spec)
    spec["name"] = name

    scheme = _editable()
    bucket, index, existing = _find_field(scheme, name)

    scope = str(spec.get("scope") or "").strip().lower()
    if not scope:
        if bucket:
            scope = "metric" if bucket == "metric" else (
                "module" if bucket.startswith("module:") else "study")
        elif spec.get("module"):
            scope = "module"
        else:
            scope = "study"
    if scope not in ("study", "metric", "module"):
        return _fail(f"'{scope}' is not a known scope "
                     "(study, metric, module)", 400)

    module_key = str(spec.get("module") or "").strip()
    if scope == "module":
        if not module_key and bucket and bucket.startswith("module:"):
            module_key = bucket.split(":", 1)[1]
        if not module_key:
            return _fail("a module field needs the module it belongs to", 400)
        if module_key not in (scheme.get("modules") or {}):
            return _fail(f"the module '{module_key}' does not exist", 400)

    problems = _check_field_spec(spec, scheme)
    if problems:
        return _fail("the field is not valid", 400, problems=problems)

    # Merge onto the existing field so a partial body updates instead of
    # wiping the field.
    if existing is not None:
        merged = dict(existing)
        for key, value in spec.items():
            if value is None:
                continue
            merged[key] = value
        merged["name"] = name
        raw = merged
    else:
        raw = dict(spec)
        raw["name"] = name

    fallback_group = (scheme.get("groups") or [{}])[0].get("name") if scheme.get("groups") else "Other"
    if scope == "metric":
        fallback_group = (scheme.get("metrics") or {}).get("group") or scheme_lib.DEFAULT_METRICS_GROUP
    elif scope == "module":
        fallback_group = module_key
    field = scheme_lib.normalise_field(raw, fallback_group, scope)
    if field is None:
        return _fail("the field could not be read", 400)
    if scope == "module":
        field["module"] = module_key
        field["group"] = module_key

    if scope == "metric":
        metrics = scheme.setdefault("metrics", {})
        fields = metrics.setdefault("fields", [])
        if bucket == "metric":
            fields[index] = field
        else:
            fields.append(field)
    elif scope == "module":
        fields = _module_fields(scheme, module_key)
        if bucket == f"module:{module_key}":
            fields[index] = field
        else:
            fields.append(field)
    else:
        fields = scheme.setdefault("fields", [])
        if bucket == "study":
            fields[index] = field
        else:
            fields.append(field)

    failure = _save(scheme)
    if failure:
        return failure
    return jsonify({"ok": True, "field": field, "scope": scope,
                    "bucket": bucket or scope})


@bp.delete("/api/scheme/fields/<path:name>")
def api_scheme_field_delete(name):
    """Remove a field (study, metric or module scope)."""
    scheme = _editable()
    bucket, index, field = _find_field(scheme, name)
    if field is None:
        return _fail(f"there is no field called '{name}'", 404)
    if bucket == "study":
        (scheme.get("fields") or []).pop(index)
    elif bucket == "metric":
        (scheme.get("metrics") or {}).get("fields", []).pop(index)
    else:
        key = bucket.split(":", 1)[1]
        fields = _module_fields(scheme, key) or []
        fields.pop(index)
    failure = _save(scheme)
    if failure:
        return failure
    return jsonify({"ok": True, "removed": name, "bucket": bucket,
                    "field": field})


# --------------------------------------------------------------------------- #
# group blocks
# --------------------------------------------------------------------------- #

@bp.post("/api/scheme/blocks")
def api_scheme_blocks_edit():
    """Add, rename, delete, reorder a group block, or set its condition.

    Body: ``{"action": "add"|"rename"|"delete"|"move"|"condition", "name": ...,
    "label": ..., "show_if": ..., "delta": ..., "target": ...}``.
    """
    body = _body()
    action = str(body.get("action") or "").strip().lower()
    name = str(body.get("name") or "").strip()
    if action not in ("add", "rename", "delete", "move", "condition"):
        return _fail("action must be one of add, rename, delete, move, condition",
                     400)
    if not name:
        return _fail("the block needs a name", 400)

    scheme = _editable()
    groups = scheme.setdefault("groups", [])
    index = next((i for i, g in enumerate(groups)
                  if g.get("name") == name), None)

    if action == "add":
        if index is not None:
            return _fail(f"the group '{name}' already exists", 400)
        label = str(body.get("label") or name).strip() or name
        group = {"name": name, "label": label, "description": "",
                 "show_if": None}
        if body.get("show_if") is not None:
            group["show_if"] = scheme_lib.normalise_condition(body.get("show_if"))
        groups.append(group)
    elif action == "rename":
        if index is None:
            return _fail(f"there is no group called '{name}'", 404)
        target = str(body.get("target") or body.get("new_name") or "").strip()
        if not target:
            return _fail("a rename needs the new name (target)", 400)
        if target != name and any(g.get("name") == target for g in groups):
            return _fail(f"the group '{target}' already exists", 400)
        groups[index]["name"] = target
        if body.get("label") is not None:
            groups[index]["label"] = str(body.get("label")).strip() or target
        # Re-point the fields that lived in the renamed group.
        for field in scheme.get("fields") or []:
            if field.get("group") == name:
                field["group"] = target
    elif action == "delete":
        if index is None:
            return _fail(f"there is no group called '{name}'", 404)
        if len(groups) <= 1:
            return _fail("the last group cannot be deleted", 400)
        groups.pop(index)
        # The fields of the deleted group move to the first remaining one.
        fallback = groups[0].get("name")
        for field in scheme.get("fields") or []:
            if field.get("group") == name:
                field["group"] = fallback
    elif action == "move":
        if index is None:
            return _fail(f"there is no group called '{name}'", 404)
        target_index = body.get("target")
        delta = body.get("delta")
        if isinstance(target_index, int) and not isinstance(target_index, bool):
            new_index = max(0, min(len(groups) - 1, target_index))
        else:
            try:
                step = int(delta)
            except (TypeError, ValueError):
                return _fail("a move needs a delta (or a target index)", 400)
            new_index = max(0, min(len(groups) - 1, index + step))
        group = groups.pop(index)
        groups.insert(new_index, group)
    else:  # condition
        if index is None:
            return _fail(f"there is no group called '{name}'", 404)
        show_if = body.get("show_if")
        if show_if in (None, "", {}):
            groups[index]["show_if"] = None
        else:
            if not isinstance(show_if, dict):
                return _fail("show_if must be a condition "
                             "({field, op, value})", 400)
            condition = scheme_lib.normalise_condition(show_if)
            if condition is None:
                return _fail("show_if is not a usable condition "
                             "(field, op, value)", 400)
            target = condition["field"]
            if target not in _field_names(scheme):
                return _fail(f"the show_if points at the unknown field "
                             f"'{target}'", 400)
            groups[index]["show_if"] = condition

    failure = _save(scheme)
    if failure:
        return failure
    return jsonify({"ok": True, "groups": scheme.get("groups") or [],
                    "blocks": scheme_lib.derive_blocks(scheme)})


# --------------------------------------------------------------------------- #
# kinds
# --------------------------------------------------------------------------- #

@bp.post("/api/scheme/kinds")
def api_scheme_kinds():
    """Add a kind (a user-defined variable type) or update the one with that id."""
    body = _body()
    kind = scheme_lib.normalise_kind(body)
    if kind is None:
        return _fail("the kind needs an id (or a label to build one from)", 400)
    if kind.get("base_type") not in scheme_lib.FIELD_TYPES:
        return _fail(f"'{kind.get('base_type')}' is not a known field type", 400)
    if kind.get("editor") not in scheme_lib.EDITORS:
        return _fail(f"'{kind.get('editor')}' is not a known editor", 400)
    scheme = _editable()
    kinds = scheme.setdefault("kinds", [])
    index = next((i for i, k in enumerate(kinds)
                  if k.get("id") == kind["id"]), None)
    if index is None:
        kinds.append(kind)
    else:
        kinds[index] = kind
    failure = _save(scheme)
    if failure:
        return failure
    return jsonify({"ok": True, "kind": kind})


@bp.delete("/api/scheme/kinds/<path:id>")
def api_scheme_kind_delete(id):
    """Remove a kind (the fields that use it keep their name but lose the kind)."""
    scheme = _editable()
    kinds = scheme.get("kinds") or []
    index = next((i for i, k in enumerate(kinds) if k.get("id") == id), None)
    if index is None:
        return _fail(f"there is no kind called '{id}'", 404)
    kinds.pop(index)
    for field in scheme_lib.all_fields(scheme):
        if field.get("kind") == id:
            field["kind"] = None
    failure = _save(scheme)
    if failure:
        return failure
    return jsonify({"ok": True, "removed": id})


# --------------------------------------------------------------------------- #
# roles and vocabularies
# --------------------------------------------------------------------------- #

@bp.get("/api/scheme/roles")
def api_scheme_roles():
    """The roles a field may carry, and which field carries each of them."""
    scheme = _scheme()
    inverse = scheme_lib.role_inverse(scheme)
    return jsonify({
        "study": sorted(scheme_lib.STUDY_ROLES),
        "metric": sorted(scheme_lib.METRIC_ROLES),
        "all": sorted(scheme_lib.ALL_ROLES),
        "fields": inverse,
    })


@bp.get("/api/scheme/vocabularies")
def api_scheme_vocabularies():
    """The scheme's controlled vocabularies."""
    return jsonify(project_context.vocabularies() or {})


@bp.post("/api/scheme/validate")
def api_scheme_validate():
    """Validate a scheme (the stored one by default) and report the problems."""
    body = _body()
    doc = body.get("scheme")
    if doc is None:
        scheme = _scheme()
    else:
        try:
            scheme = scheme_lib.normalise_scheme(doc)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"the scheme could not be read: {exc}", 400)
    problems = _problems(scheme)
    return jsonify({"ok": not _errors(problems), "problems": problems,
                    "error_count": len(_errors(problems)),
                    "warning_count": len(problems) - len(_errors(problems))})


@bp.get("/api/scheme/bundle")
def api_scheme_bundle():
    """The agent bundle (the coder-facing description of the scheme)."""
    try:
        from scheme_export import build_bundle
    except ImportError as exc:
        return _fail("the agent-bundle exporter (scheme_export.py) is not "
                     "available yet", 501, detail=str(exc))
    scheme = _scheme()
    try:
        try:
            bundle = build_bundle(scheme)
        except TypeError:
            bundle = build_bundle()
    except Exception as exc:  # noqa: BLE001
        return _fail(f"the bundle could not be built: {exc}", 500)
    if isinstance(bundle, (dict, list)):
        return jsonify(bundle)
    return jsonify({"ok": True, "bundle": bundle})


# --------------------------------------------------------------------------- #
# criteria
# --------------------------------------------------------------------------- #

def _criteria_file():
    """The raw ``Project/criteria.json`` of the active project."""
    path = project_context.criteria_path(project_context.active_scheme_id() or "")
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _normalise_criteria(doc):
    """Normalise a criteria document (fallback while ``criteria_lib`` is absent)."""
    doc = doc if isinstance(doc, dict) else {}
    out = {"version": 1, "abstract": [], "fulltext": []}
    try:
        out["version"] = int(doc.get("version") or 1)
    except (TypeError, ValueError):
        out["version"] = 1
    for key in ("abstract", "fulltext"):
        items = doc.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, str):
                item = {"label": item}
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or item.get("criterion")
                        or item.get("text") or "").strip()
            if not label:
                continue
            out[key].append({
                "label": label,
                "code": str(item.get("code") or "").strip(),
                "description": str(item.get("description") or "").strip(),
            })
    return out


def _criteria():
    """The active project's criteria, normalised."""
    try:
        doc = project_context.criteria()
    except Exception:  # noqa: BLE001
        doc = None
    if isinstance(doc, dict) and (doc.get("abstract") or doc.get("fulltext")):
        return doc
    raw = _criteria_file()
    if raw:
        return _normalise_criteria(raw)
    return doc if isinstance(doc, dict) else {"version": 1, "abstract": [],
                                              "fulltext": []}


@bp.get("/api/criteria")
def api_criteria():
    """The project's abstract and full-text exclusion criteria."""
    return jsonify(_criteria())


@bp.put("/api/criteria")
def api_put_criteria():
    """Replace the project's criteria."""
    doc = _body()
    if not doc:
        return _fail("the request body must be the criteria document", 400)
    try:
        project_context.write_criteria(doc)
        criteria = project_context.criteria()
    except ImportError:
        # ``criteria_lib`` is not available yet: normalise and write it here.
        criteria = _normalise_criteria(doc)
        path = project_context.criteria_path(project_context.active_scheme_id() or "")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(suffix=".json",
                                        dir=os.path.dirname(path))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(criteria, fh, ensure_ascii=False, indent=1)
                fh.write("\n")
            from studio_lib import atomic_replace

            atomic_replace(tmp, path)
        except OSError as exc:
            return _fail(f"could not write the criteria: {exc}", 500)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"could not write the criteria: {exc}", 500)
    except ValueError as exc:
        return _fail(str(exc), 400)
    except OSError as exc:
        return _fail(f"could not write the criteria: {exc}", 500)
    return jsonify({"ok": True, "criteria": criteria})


# --------------------------------------------------------------------------- #
# errors: always JSON, never an HTML traceback
# --------------------------------------------------------------------------- #

@bp.errorhandler(Exception)
def api_scheme_error(exc):
    """Answer every failure with JSON - and keep the traceback in the log."""
    if isinstance(exc, HTTPException):
        if exc.code and exc.code >= 500:
            _log_traceback(exc)
        return jsonify({"ok": False, "error": exc.description or exc.name}), exc.code
    _log_traceback(exc)
    return jsonify({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}), 500


def _log_traceback(exc):
    """Write the traceback of a failed request to the app log (never raises)."""
    try:
        import traceback

        from studio_state import log

        log("scheme request failed:\n" + "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:  # noqa: BLE001
        pass
