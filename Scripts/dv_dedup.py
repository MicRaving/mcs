# -*- coding: utf-8 -*-
"""Drop redundant nested outcomes so a composite and its sub-items are not
double-weighted in the SDT layer.

Problem
-------
A domain's tagged DVs often include a COMPOSITE and its component sub-items, all
carrying the same veracity (e.g. ``dv_belief_accuracy_false`` together with
``dv_belief_accuracy_false_prodemocrat`` / ``..._prorepublican``, or
``dv_technique_recognition_false`` with ``..._false_fakeexpert_i01`` etc.).  The
composite is (approximately) the mean of the sub-items, so feeding both into
``d = mean(true) - mean(false)`` counts the sub-items twice.

``repro_lib.build_recalc`` already swallows an item ``X_iNN`` when the exact
composite ``X`` exists, but that misses (a) nested names ``X_sub_iNN`` and (b) the
plain ``X_sub`` case, and ``reanalysis.py`` has no such rule at all.  This module
applies one deterministic rule at sidecar-read time, so both pipelines agree:

    within one (dv_domain, veracity, timepoint) cell, keep the SHORTEST tagged
    ``dv_*`` name (the composite) and clear ``veracity`` on every longer name that
    extends it with ``_...`` (the redundant sub-items).

Clearing ``veracity`` (rather than deleting the column) keeps every data column in
the harmonized file for other uses; it only removes the sub-item from the SDT
true/false split.  Raw data and the harmonized wide file are untouched.

The rule is intentionally conservative and deterministic.  A handful of
same-prefix pairs may be genuinely distinct facets rather than composite/sub-item;
keeping the shortest is the same choice the reproduction engine already makes for
``X_iNN``, and clearing the remainder is documented here rather than silently.
"""


def prune_sidecar(side):
    """Mutate/return a sidecar DataFrame, clearing ``veracity`` on nested sub-items.

    Returns ``(side, pruned)`` where ``pruned`` is a sorted list of
    ``(nested_variable, composite_kept)``.  Safe on a DataFrame missing columns.
    """
    if side is None:
        return side, []
    need = {"uniform_variable", "dv_domain", "veracity", "timepoint"}
    if not need.issubset(set(map(str, side.columns))):
        return side, []
    uv = side["uniform_variable"].astype(str)
    ver = side["veracity"].fillna("").astype(str).str.strip().str.lower()
    dom = side["dv_domain"].fillna("").astype(str).str.strip()
    tp = side["timepoint"].fillna("").astype(str).str.strip()
    tagged = uv.str.startswith("dv_") & ~uv.str.endswith("_01") & ver.isin(["true", "false"])

    groups = {}
    for i in side.index[tagged]:
        groups.setdefault((dom[i], ver[i], tp[i]), []).append(uv[i])

    prune = {}
    for names in groups.values():
        names = sorted(set(names))
        for b in names:
            for a in names:
                if a != b and b.startswith(a + "_"):
                    if b not in prune or len(a) < len(prune[b]):
                        prune[b] = a
    if prune:
        m = uv.isin(sorted(prune))
        side = side.copy()
        side.loc[m, "veracity"] = ""
    return side, sorted(prune.items())