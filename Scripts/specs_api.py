"""The read contract for the spec tables (``temp/meta_specs.csv`` /
``temp/sdt_specs.csv``): ONE place that decides which rows enter a pool.

``select_spec_rows`` applies, together and only here:

* the ``r_used == 0.5`` primary filter for the meta-DiD layer;
* the ``eligible_primary == 1`` filter when the column is present;
* exclusions on/off;
* the fully-within include/exclude switch;
* the ``|DiD| <= DID_ABS_MAX`` quality guard.

Every consumer (``publication_bias.select``, the app's ``analysis_lib`` helpers,
``routes_apa``) calls this so a table's ``k`` and its estimate always come from
the same row set.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import contract as CT  # noqa: E402
import suite_common as SC  # noqa: E402

PRIMARY_R = 0.5

#: What each specification's ``effect`` IS.  Kept next to the selector so the app
#: and the reports can label every synthesis row correctly.
SPEC_EFFECT_DEFINITION = {
    "post_all": "intervention-vs-control effect on discrimination (SDT) / reported "
                "immediate-posttest effect (meta); higher = more desirable",
    "post_cred": "as post_all, credibility-only with the sharing-intention fallback",
    "persistence_all": "follow-up (persistence) analogue of post_all",
    "persistence_cred": "as persistence_all, credibility-only fallback",
    "did_all": "pretest-corrected change: (post_IG-pre_IG)-(post_CG-pre_CG), "
               "standardised by the pooled pretest SD",
    "did_cred": "as did_all, credibility-only fallback",
    "didpost_all": "derived: the DiD row where available else the immediate posttest "
                   "(same question population as post_all)",
    "didpost_cred": "as didpost_all, credibility-only fallback",
    "fupost_all": "derived: the follow-up row where available else the immediate "
                  "posttest (same population as post_all)",
    "fupost_cred": "as fupost_all, credibility-only fallback",
    "discrimination_level_all": "AUXILIARY arm-pooled discrimination LEVEL (not an "
                                "intervention effect); d' = z(HR)-z(FAR)",
    "discrimination_level_cred": "as discrimination_level_all, credibility-only fallback",
    "discrimination_level_persistence_all": "AUXILIARY arm-pooled follow-up level",
    "discrimination_level_persistence_cred": "as above, credibility-only fallback",
}


def _as_frame(rows):
    if isinstance(rows, pd.DataFrame):
        return rows.copy()
    return pd.DataFrame(list(rows))


def select_spec_rows(rows, layer=None, spec=None, exclusions="on",
                     include_fully_within=False):
    """Return the spec rows that may enter one pool (the single selection rule).

    ``rows`` is a spec DataFrame (or list of dicts).  ``exclusions`` in
    ``{"on","off",None}`` (None = both).  The meta-DiD quality guard
    ``|effect| <= DID_ABS_MAX`` is applied to ``did*`` specs; the SDT layer is
    unaffected.
    """
    d = _as_frame(rows)
    if d is None or not len(d):
        return d if d is not None else pd.DataFrame()
    # Accept a raw spec table in either session's vocabulary (the multiverse
    # writers use ``yi``/``vi``/``did_r_used``/``ON``/``OFF``); callers such as
    # ``publication_bias.select`` pass such frames directly.  The mapping mirrors
    # ``suite_common._normalize_specs`` so every consumer sees one schema.
    _ren = {}
    if "effect" not in d.columns and "yi" in d.columns:
        _ren["yi"] = "effect"
    if "var" not in d.columns and "vi" in d.columns:
        _ren["vi"] = "var"
    if "se" not in d.columns and "sei" in d.columns:
        _ren["sei"] = "se"
    if "r_used" not in d.columns and "did_r_used" in d.columns:
        _ren["did_r_used"] = "r_used"
    if "experiment_id" not in d.columns and "Study_ID" in d.columns:
        _ren["Study_ID"] = "experiment_id"
    if "dv_domain" not in d.columns and "domain" in d.columns:
        _ren["domain"] = "dv_domain"
    if "timepoint" not in d.columns and "timepoint_1" in d.columns:
        _ren["timepoint_1"] = "timepoint"
    if _ren:
        d = d.rename(columns=_ren)
    if "exclusions" in d.columns:
        d = d.copy()
        d["exclusions"] = (d["exclusions"].astype(str).str.strip().str.lower()
                           .replace({"nan": "on", "": "on"}))
    if layer is not None and "layer" not in d.columns:
        d = d.copy()
        d["layer"] = str(layer)
    if layer is not None and "layer" in d.columns:
        d = d[d["layer"].astype(str) == str(layer)]
    if spec is not None and "spec" in d.columns:
        d = d[d["spec"].astype(str) == str(spec)]
    if exclusions in ("on", "off") and "exclusions" in d.columns:
        d = d[d["exclusions"].astype(str) == exclusions]
    # fully-within: the spec tables are already built under the switch, but if a
    # marker column is present enforce it here too.
    if not include_fully_within and "fully_within" in d.columns:
        fw = d["fully_within"].fillna(False).astype(bool)
        d = d[~fw]
    # eligible_primary filter (present on some frames)
    if "eligible_primary" in d.columns:
        d = d[pd.to_numeric(d["eligible_primary"], errors="coerce").fillna(0) == 1]
    # meta-DiD r_used primary filter
    lays = set(d["layer"].astype(str)) if "layer" in d.columns else set()
    if (not lays or "meta" in lays) and "r_used" in d.columns:
        ru = pd.to_numeric(d["r_used"], errors="coerce")
        d = d[ru.isna() | np.isclose(ru, PRIMARY_R)]
    eff = pd.to_numeric(d["effect"], errors="coerce") if "effect" in d.columns else pd.Series(dtype=float)
    var = pd.to_numeric(d["var"], errors="coerce") if "var" in d.columns else pd.Series(dtype=float)
    d = d[np.isfinite(eff) & np.isfinite(var) & (var > 0)]
    if "spec" in d.columns:
        specs = d["spec"].astype(str)
        is_did = specs.str.startswith("did")
        e = pd.to_numeric(d["effect"], errors="coerce")
        d = d[~(is_did & (e.abs() > CT.DID_ABS_MAX))]
    return d


def derived_rows(rows, spec):
    """Materialise one derived spec (``didpost_*`` / ``fupost_*``) as row dicts."""
    d = _as_frame(rows)
    if not len(d):
        return []
    layer = str(d["layer"].iloc[0]) if "layer" in d.columns else "meta"
    out = SC.derive_specs(d, layer)
    if "spec" not in out.columns:
        return []
    return out[out["spec"].astype(str) == str(spec)].to_dict("records")


def ensure_specs(force=False):
    """Rebuild the prerequisite spec tables if absent, unusable or stale.

    Thin wrapper over :func:`suite_common.ensure_specs`, which staleness-checks
    BOTH layers against their harmonized inputs and the fully-within marker (H3).
    """
    return SC.ensure_specs(force=force)
