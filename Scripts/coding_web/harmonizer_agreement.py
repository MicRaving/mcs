"""Harmonizer inter-rater agreement — the harmonizer counterpart of ``agreement_lib``.

The extraction layer treats each ``Codings/<Key> - <coder>.json`` as a rater and reports
Gwet's AC1/AC2 plus a per-field conflict list that a master coder resolves.  Harmonizer
coders were already supported as *editors* (``Codings/<Key> - harmonizer - <coder>.json``
override files, see ``routes_harmonizer``) but there was no way to compare them; this module
gives the harmonizer the same three features:

    * ``raters``      -- the canonical sidecar ("LLM/base") plus every coder override file;
    * ``reliability`` -- per-field AC1 over every rated ``uniform_variable``;
    * ``conflicts``   -- per-variable/field rows where the raters disagree;
    * ``resolve``     -- write the chosen value into ``Codings/<Key> - harmonizer - <master>.json``.

A rater's value for a variable/field is the coder's override when set, otherwise the canonical
sidecar value: an override file only encodes *changes*, so "not overridden" means "agrees with
canonical" by construction.  That keeps agreement honest (an untouched variable is unanimous)
while still surfacing every deliberate divergence.
"""
from __future__ import annotations
import os
import json
import tempfile
from datetime import datetime

from studio_state import S
from studio_lib import atomic_replace, safe_token
from routes_harmonizer import (harmonizer_coder_files, load_harmonized_variables,
                               load_harmonizer_overrides, safe_study_key, _HARM_FIELDS)

BASE_RATER = "LLM/base"          # the canonical harmonized sidecar
MASTER_CODER = "master"         # where a resolved value is written
# A field a coder may rate.  `role` is included because a disagreement about whether a
# variable is a dv_/cov_/flag_ role changes every downstream product.
RATED_FIELDS = tuple(_HARM_FIELDS)


def _norm(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return "|".join(sorted(str(x).strip() for x in v if str(x).strip()))
    return str(v).strip()


def raters_for(key):
    """[{coder, file, modified, is_base}] -- canonical first, then every coder file."""
    out = [{"coder": BASE_RATER, "file": None, "modified": None, "is_base": True}]
    for info in harmonizer_coder_files(key):
        out.append({"coder": info["coder"], "file": info["file"],
                    "modified": info["modified"], "is_base": False})
    return out


def _ratings(key):
    """(variables, {coder: {uv: {field: value}}}) with the canonical fallback applied.

    Uses ``load_harmonized_variables`` for the canonical sidecar and
    ``load_harmonizer_overrides`` for the coder deltas; a coder's effective rating for a
    variable/field is the override if present else the canonical value.
    """
    hv = load_harmonized_variables(key)
    if not hv:
        return None, None
    variables = hv["variables"]
    _merged, by_coder = load_harmonizer_overrides(key)
    ratings = {BASE_RATER: {}}
    for row in variables:
        uv = row["uniform_variable"]
        ratings[BASE_RATER][uv] = {f: _norm(row.get(f)) for f in RATED_FIELDS}
    for coder, ov in by_coder.items():
        ratings[coder] = {}
        for row in variables:
            uv = row["uniform_variable"]
            base = ratings[BASE_RATER][uv]
            delta = ov.get(uv) if isinstance(ov, dict) else None
            ratings[coder][uv] = {
                f: (_norm(delta.get(f)) if isinstance(delta, dict) and f in delta
                    else base[f])
                for f in RATED_FIELDS
            }
    return variables, ratings


def _gwet_ac1(units):
    """Gwet's AC1 over a list of rating vectors (one vector per unit, one entry per rater).

    Mirrors ``agreement_lib.gwet_ac`` for the nominal case so the harmonizer and the
    extraction layer report the same statistic.  Blank entries are "not rated" and are
    dropped per unit; a unit with < 2 ratings is skipped.
    """
    # One implementation of AC1: this used to compute its own chance agreement
    # as sum(p_k^2) (Scott's pi / Fleiss' kappa), not Gwet's
    # sum(p_k(1 - p_k)) / (q - 1), so the two tabs disagreed on the same data.
    from agreement_lib import gwet_ac
    cases = [[v for v in vals if v != ""] for vals in units]
    cases = [c for c in cases if len(c) >= 2]
    if not cases:
        return None
    cats = sorted({v for c in cases for v in c}, key=str)
    if len(cats) <= 1:
        return 1.0
    res = gwet_ac(cases, cats)
    return res["coeff"] if res else None


def reliability(key):
    """Per-field AC1 + coverage, over every rated uniform_variable."""
    variables, ratings = _ratings(key)
    if not variables:
        return {"available": False, "reason": "no harmonized_variables sidecar", "fields": []}
    coders = list(ratings)
    fields = []
    for f in RATED_FIELDS:
        units, rated = [], 0
        for row in variables:
            uv = row["uniform_variable"]
            vals = [ratings[c][uv][f] for c in coders]
            if any(v != "" for v in vals):
                rated += 1
            units.append(vals)
        ac1 = _gwet_ac1(units)
        fields.append({"field": f, "ac1": ac1, "rated": rated,
                       "variables": len(variables)})
    return {"available": True, "coders": coders, "fields": fields,
            "variables": len(variables)}


def conflicts(key, fields=None):
    """Every variable/field where the raters do not agree, with each rater's value."""
    variables, ratings = _ratings(key)
    if not variables:
        return {"available": False, "conflicts": []}
    coders = list(ratings)
    fields = fields or RATED_FIELDS
    out = []
    for row in variables:
        uv = row["uniform_variable"]
        for f in fields:
            vals = {c: ratings[c][uv][f] for c in coders}
            distinct = {v for v in vals.values() if v != ""}
            if len(distinct) > 1:
                out.append({"uniform_variable": uv, "field": f, "values": vals,
                            "bundle": row.get("bundle", ""), "role": row.get("role", "")})
    return {"available": True, "coders": coders, "conflicts": out,
            "n_conflicts": len(out)}


def resolve(key, uniform_variable, field, value, coder=MASTER_CODER):
    """Write one resolved value into the master coder's override file (never the sidecar)."""
    if field not in RATED_FIELDS:
        return {"ok": False, "error": f"unknown field {field!r}"}
    # Both the key and the coder name come from the request body and form the
    # file name: refuse a traversal key and sanitise the coder the same way the
    # harmonizer save does, so the write can only land in Codings/.
    if not safe_study_key(key):
        return {"ok": False, "error": "invalid study key"}
    coder = safe_token(coder)
    path = os.path.join(S.CODINGS, f"{key} - harmonizer - {coder}.json")
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh) or {}
        except Exception:
            data = {}
    ov = data.setdefault("overrides", {})
    ov.setdefault(uniform_variable, {})[field] = value
    data["coder"] = coder
    data["key"] = key
    data["updated"] = datetime.now().isoformat(timespec="seconds")
    # temp file + atomic replace: a crash mid-write must not truncate the
    # master's override file (every earlier resolution lives in it).
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=S.TEMP or None)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
        atomic_replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return {"ok": True, "file": os.path.basename(path), "overrides": len(ov)}


# --------------------------------------------------------------------------- HTTP API
# Same three-endpoint shape as the extraction agreement routes
# (/api/agreement/reliability|conflicts|resolve), so the SPA can reuse its agreement panel.
from flask import Blueprint, jsonify, request  # noqa: E402

bp = Blueprint("harmonizer_agreement", __name__)


@bp.get("/api/harmonizer_agreement/reliability")
def api_harmonizer_reliability():
    key = (request.args.get("key") or "").strip()
    if not key or not safe_study_key(key):
        return jsonify({"ok": False, "error": "a valid study key is required"}), 400
    return jsonify({"ok": True, "key": key, **reliability(key)})


@bp.get("/api/harmonizer_agreement/conflicts")
def api_harmonizer_conflicts():
    key = (request.args.get("key") or "").strip()
    if not key or not safe_study_key(key):
        return jsonify({"ok": False, "error": "a valid study key is required"}), 400
    fields = [f for f in (request.args.get("fields") or "").split(",") if f.strip()]
    return jsonify({"ok": True, "key": key, **conflicts(key, fields or None)})


@bp.post("/api/harmonizer_agreement/resolve")
def api_harmonizer_resolve():
    body = request.get_json(silent=True) or {}
    key = str(body.get("key") or "").strip()
    uv = str(body.get("uniform_variable") or "").strip()
    field = str(body.get("field") or "").strip()
    if not (key and uv and field):
        return jsonify({"ok": False, "error": "key, uniform_variable and field are required"}), 400
    # `value` may legitimately be "" (clear the cell), so it is read with .get, not `or`.
    res = resolve(key, uv, field, body.get("value"), str(body.get("coder") or MASTER_CODER))
    return jsonify({"ok": bool(res.get("ok")), "key": key, **res}), (200 if res.get("ok") else 400)
