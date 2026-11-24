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
from datetime import datetime

from studio_lib import safe_token
from studio_state import S
from routes_harmonizer import (HARM_MASTER, field_error, harmonizer_coder_files,
                               load_harmonized_variables, load_harmonizer_overrides,
                               valid_key, write_json_atomic, _HARM_FIELDS)

BASE_RATER = "LLM/base"          # the canonical harmonized sidecar
MASTER_CODER = HARM_MASTER      # where a resolved value is written (applied last)
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

    Delegates to ``agreement_lib.gwet_ac`` (nominal weights) so the harmonizer and
    the extraction layer report the same statistic: AC1's chance agreement is
    ``sum(pi_k (1 - pi_k)) / (q - 1)``, not the Fleiss/Scott ``sum(pi_k^2)``.
    Blank entries are "not rated"; a unit nobody rated is dropped.
    """
    from agreement_lib import gwet_ac
    units = [[v for v in vals if v != ""] for vals in units]
    units = [u for u in units if u]
    cats = sorted({v for u in units for v in u})
    if not cats:
        return None
    if len(cats) == 1:
        return 1.0 if any(len(u) >= 2 for u in units) else None
    res = gwet_ac(units, cats)
    return None if res is None else res.get("coeff")


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
    return {"available": True, "coders": coders, "raters": raters_for(key),
            "fields": fields, "variables": len(variables)}


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
    if not valid_key(key):
        return {"ok": False, "error": "invalid study key"}
    hv = load_harmonized_variables(key)
    if not hv:
        return {"ok": False, "error": "no harmonized_variables sidecar for this study"}
    if uniform_variable not in {v["uniform_variable"] for v in hv["variables"]}:
        return {"ok": False, "error": f"{uniform_variable!r} is not a variable of this study"}
    value = "" if value is None else str(value).strip()
    err = field_error(field, value)
    if err:
        return {"ok": False, "error": err}
    if field == "veracity":
        value = value.lower()
    coder = safe_token(coder)
    path = os.path.join(S.CODINGS, f"{key} - harmonizer - {coder}.json")
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh) or {}
        except (OSError, ValueError) as e:
            # never replace a file we could not read: that would drop its overrides
            return {"ok": False, "error": f"{os.path.basename(path)} is unreadable: {e!r}"}
    if not isinstance(data, dict) or not isinstance(data.get("overrides", {}), dict):
        return {"ok": False, "error": f"{os.path.basename(path)} has an unexpected shape"}
    ov = data.setdefault("overrides", {})
    ov.setdefault(uniform_variable, {})[field] = value
    data["coder"] = coder
    data["key"] = key
    data["updated"] = datetime.now().isoformat(timespec="seconds")
    write_json_atomic(path, data)
    return {"ok": True, "file": os.path.basename(path), "overrides": len(ov)}


# --------------------------------------------------------------------------- HTTP API
# Same three-endpoint shape as the extraction agreement routes
# (/api/agreement/reliability|conflicts|resolve), so the SPA can reuse its agreement panel.
from flask import Blueprint, jsonify, request  # noqa: E402

bp = Blueprint("harmonizer_agreement", __name__)


@bp.get("/api/harmonizer_agreement/reliability")
def api_harmonizer_reliability():
    key = (request.args.get("key") or "").strip()
    if not valid_key(key):
        return jsonify({"ok": False, "error": "valid key required"}), 400
    return jsonify({"ok": True, "key": key, **reliability(key)})


@bp.get("/api/harmonizer_agreement/conflicts")
def api_harmonizer_conflicts():
    key = (request.args.get("key") or "").strip()
    if not valid_key(key):
        return jsonify({"ok": False, "error": "valid key required"}), 400
    fields = [f.strip() for f in (request.args.get("fields") or "").split(",")
              if f.strip() in RATED_FIELDS]
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
