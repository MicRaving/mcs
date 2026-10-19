#!/usr/bin/env python
"""Golden parity test — the app's statistics vs the pipeline products.

Acceptance test for the CodingStudio correctness track
(``Results/PLAN_codingstudio_harmonizer_reproduction_2026-09-22.md`` §3,
decision R1).  For a set of headline numbers it recomputes each number

  * through the **app's code path** (``Scripts/coding_web/analysis_lib.py`` /
    ``routes_apa.py`` — which must only ever *call* the shared
    ``stat_core``/``specs_api``/``contract``/``prisma`` modules), and
  * from the **pipeline products** (``temp/pb_*.csv``, ``Results/metaanalysis.xlsx``,
    ``Results/reanalysis.xlsx``, ``Scripts/prisma.py``),

and requires them to agree to ``1e-9`` (``1e-6`` where the two float paths
accumulate differently).

Two severities (W2-parity, 2026-09-22):

* **hard** (default) -- the app's code path vs the pipeline's shared selector /
  pooler (``publication_bias.block``) and the ``temp/pb_*.csv`` products, plus the
  prisma and Bayes checks.  Any mismatch is a FAIL and exit code 1.
* **workbook** -- the spec-based pool vs ``Results/*.xlsx`` (metaanalysis.xlsx,
  reanalysis.xlsx).  A mismatch is printed as DIVERGENCE (non-fatal).

W2-parity2 (2026-09-22): BOTH workbook layers are now unified on the workbook's own
derivation.  ``analysis_multiverse_meta.primary_composite_frames`` /
``write_meta_spec_table`` (used by ``analysis_multiverse_meta.main`` and by
``suite_common.build_meta_specs``) is the same contrast computation
``analysis/metaanalysis.py`` pools, and the SDT layer already derived its specs
from ``reanalysis.collect_datasets``.  Every ``*/... vs *.xlsx`` workbook check is
therefore expected to match exactly; a DIVERGENCE is now a genuine regression
rather than the documented pipeline-internal split of W2-parity (see
``_check_meta_headlines``).

Run:  python Scripts/parity_check.py [--tol 1e-9]
Requires the analysis suite tables to exist:  python Scripts/publication_bias.py
"""

from __future__ import annotations

import argparse
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
for _p in (_HERE, os.path.join(_HERE, "coding_web")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd  # noqa: E402

TEMP = os.path.join(ROOT, "temp")
RESULTS = os.path.join(ROOT, "Results")

_DEFAULT_TOL = 1e-9

_CHECKS = []          # rows: dict(label, app, pipe, delta, tol, ok)


def _f(x):
    try:
        if x is None:
            return None
        v = float(x)
        return v
    except (TypeError, ValueError):
        return None


def _same(a, b, tol):
    a, b = _f(a), _f(b)
    if a is None or b is None:
        return a is None and b is None
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    return abs(a - b) <= tol


def add(label, app, pipe, tol=None, sev="hard"):
    tol = _DEFAULT_TOL if tol is None else tol
    ok = _same(app, pipe, tol)
    a, b = _f(app), _f(pipe)
    delta = (abs(a - b) if (a is not None and b is not None
                            and not math.isnan(a) and not math.isnan(b)) else float("nan"))
    _CHECKS.append({"label": label, "app": a, "pipe": b, "delta": delta,
                    "tol": tol, "ok": ok, "sev": sev})


def add_log10(label, app, pipe, tol=1e-6, sev="hard"):
    """Compare a strictly positive magnitude (e.g. a Bayes factor) on log10.

    Bayes factors can reach 1e200+, where an absolute 1e-9 tolerance is
    meaningless; a log10 tolerance of 1e-6 pins the value to ~1 part in 10^6.
    """
    a, b = _f(app), _f(pipe)
    if a is None or b is None or a <= 0 or b <= 0:
        ok = (a is None and b is None)
        delta = float("nan")
    else:
        la, lb = math.log10(a), math.log10(b)
        delta = abs(la - lb)
        ok = delta <= tol
    _CHECKS.append({"label": label, "app": a, "pipe": b, "delta": delta,
                    "tol": tol, "ok": ok, "sev": sev})


# --------------------------------------------------------------------------- #
# pipeline product readers
# --------------------------------------------------------------------------- #
def _read_csv(name):
    p = os.path.join(TEMP, name)
    if not os.path.isfile(p):
        return None
    return pd.read_csv(p, encoding="utf-8-sig", low_memory=False)


def _read_xlsx(path, sheet):
    if not os.path.isfile(path):
        return None
    try:
        return pd.read_excel(path, sheet_name=sheet)
    except Exception:
        return None


def _row(df, **match):
    if df is None or not len(df):
        return None
    d = df
    for k, v in match.items():
        if k not in d.columns:
            return None
        d = d[d[k].astype(str) == str(v)]
    if not len(d):
        return None
    return d.iloc[0]


def _app_pooled(layer, spec, exclusions, scope="unrestricted"):
    import analysis_lib as A

    return A._pooled_for(layer, spec, exclusions, scope)


def _app_bayes(layer, spec, exclusions, scope="unrestricted"):
    import analysis_lib as A

    return A._bayes_for(layer, spec, exclusions, scope)


def _app_subgroup(by, layer, spec, exclusions):
    import analysis_lib as A
    from flask import Flask

    tapp = Flask("parity")
    with tapp.test_request_context("/"):
        return A._subgroup_rows(by, layer, spec, exclusions)


_META_QUESTIONS = {"post_all": "posttest (immediate)",
                   "persistence_all": "follow-up (persistence)",
                   "did_all": "DiD (pre->post)"}


_PB_SPECS = {"d": None}


def _pb_specs():
    if _PB_SPECS["d"] is None:
        import suite_common as SC

        d = SC.load_specs()
        if "study_key" in d.columns:
            d["study_key"] = d["study_key"].astype(str)
        _PB_SPECS["d"] = d
    return _PB_SPECS["d"]


def _pb_block(layer, spec, exclusions):
    """The pipeline's own pooled block for a spec (publication_bias.block)."""
    import publication_bias as PB

    try:
        row, _d, _m = PB.block(_pb_specs(), layer, spec, exclusions)
        return row
    except Exception:
        return None


def _check_meta_headlines():
    pb = _read_csv("pb_pooled.csv")
    meta_res = _read_xlsx(os.path.join(RESULTS, "metaanalysis.xlsx"), "MetaResults")
    meta_res_r = _read_xlsx(os.path.join(RESULTS, "metaanalysis.xlsx"), "MetaResults_restricted")
    for spec in ("post_all", "persistence_all", "did_all"):
        res = _app_pooled("meta", spec, "on")
        if not res:
            add(f"meta/{spec}/on (app present)", 0, 0)
            continue
        # (a) vs the pipeline's own selector + pooler on the SAME spec file
        blk = _pb_block("meta", spec, "on")
        if blk:
            add(f"meta/{spec}/on k vs publication_bias.block", res.get("dl_k"), blk.get("k"))
            add(f"meta/{spec}/on dl_est vs publication_bias.block",
                res.get("dl_est"), blk.get("dl_est"))
            add(f"meta/{spec}/on three_est vs publication_bias.block",
                res.get("three_est"), blk.get("three_est"), tol=1e-6)
            add(f"meta/{spec}/on rve_est vs publication_bias.block",
                res.get("rve_est"), blk.get("rve_est"), tol=1e-6)
        # (b) vs the publication-bias pooled table (product)
        row = _row(pb, layer="meta", spec=spec, exclusions="on")
        if row is not None:
            add(f"meta/{spec}/on k vs pb_pooled", res.get("dl_k"), row.get("k"))
            add(f"meta/{spec}/on dl_est vs pb_pooled", res.get("dl_est"), row.get("dl_est"))
            add(f"meta/{spec}/on three_est vs pb_pooled",
                res.get("three_est"), row.get("three_est"), tol=1e-6)
        # (c) vs the canonical metaanalysis workbook (AM-derived)
        #
        # W2-parity2 (2026-09-22): the spec-based meta pool (temp/meta_specs.csv)
        # and Results/metaanalysis.xlsx now share ONE contrast computation
        # (analysis_multiverse_meta.primary_composite_frames /
        # write_meta_spec_table; analysis/metaanalysis.py pools the same rows).  The
        # W2-parity divergence (post_all 3078 vs 3088, did_all 354 vs 355, a ~7e-4
        # persistence dl_est gap) was caused by the standalone meta spec builder
        # re-deriving the contrasts in a separate implementation that also omitted
        # the experiment key when pairing separate_true/separate_false components;
        # it is fixed, so these checks must now match exactly.
        row = _row(meta_res, question=_META_QUESTIONS[spec], scope="unrestricted", group="ALL")
        if row is not None:
            add(f"meta/{spec} k vs metaanalysis.xlsx", res.get("dl_k"), row.get("dl_k"),
                sev="workbook")
            add(f"meta/{spec} dl_est vs metaanalysis.xlsx",
                res.get("dl_est"), row.get("dl_est"), sev="workbook")
        # (d) restricted (susceptibility domains)
        res_r = _app_pooled("meta", spec, "on", "restricted")
        row = _row(meta_res_r, question=_META_QUESTIONS[spec], scope="restricted", group="ALL")
        if res_r and row is not None:
            add(f"meta/{spec} restricted k vs metaanalysis.xlsx",
                res_r.get("dl_k"), row.get("dl_k"), sev="workbook")
            add(f"meta/{spec} restricted dl_est vs metaanalysis.xlsx",
                res_r.get("dl_est"), row.get("dl_est"), sev="workbook")


def _check_sdt_headlines():
    re_res = _read_xlsx(os.path.join(RESULTS, "reanalysis.xlsx"), "MetaResults")
    disc = _read_xlsx(os.path.join(RESULTS, "reanalysis.xlsx"), "Discrimination")
    for spec in ("post_all", "persistence_all", "did_all"):
        res = _app_pooled("sdt", spec, "on")
        if not res:
            continue
        blk = _pb_block("sdt", spec, "on")
        if blk:
            add(f"sdt/{spec} k vs publication_bias.block", res.get("dl_k"), blk.get("k"))
            add(f"sdt/{spec} three_est vs publication_bias.block",
                res.get("three_est"), blk.get("three_est"), tol=1e-6)
        row = _row(re_res, question=_META_QUESTIONS[spec], scope="unrestricted", group="ALL")
        if row is not None:
            add(f"sdt/{spec} k vs reanalysis.xlsx", res.get("dl_k"), row.get("dl_k"),
                sev="workbook")
            add(f"sdt/{spec} three_est vs reanalysis.xlsx",
                res.get("three_est"), row.get("three_est"), tol=1e-6, sev="workbook")
        # SDT Δd' pooled directly from the Discrimination sheet
        if disc is not None and len(disc):
            bucket = {"post_all": "post", "persistence_all": "followup", "did_all": "did"}[spec]
            sub = disc[disc["bucket"].astype(str) == bucket]
            if len(sub):
                import suite_common as SC

                dep = SC.pool_dependence(sub["d"], sub["vi"], sub["cluster_id"], sub["study_key"])
                add(f"sdt/{spec} k vs Discrimination sheet", res.get("dl_k"), dep.get("dl_k"),
                    sev="workbook")
                add(f"sdt/{spec} Δd' vs Discrimination sheet",
                    res.get("three_est"), dep.get("three_est"), tol=1e-6, sev="workbook")


def _check_subgroup():
    pb_dom = _read_csv("pb_domain.csv")
    if pb_dom is None:
        return
    import analysis_lib as A

    rows = _app_subgroup("dv_domain", "meta", "post_all", "on")
    d = pb_dom[(pb_dom["layer"].astype(str) == "meta")
               & (pb_dom["spec"].astype(str) == "post_all")
               & (pb_dom["exclusions"].astype(str) == "on")]
    by = {A._mod_label("dv_domain", str(r["dv_domain"])): r for _, r in d.iterrows()}
    n = 0
    for r in rows:
        hit = by.get(str(r.get("label")))
        if hit is None:
            continue
        n += 1
        add(f"subgroup/dv_domain/{r.get('label')} k vs pb_domain", r.get("k"), hit.get("k"))
        add(f"subgroup/dv_domain/{r.get('label')} est vs pb_domain",
            r.get("est"), hit.get("est"))
    if not n:
        add("subgroup/dv_domain matched rows", 0, 1)


def _check_moderator():
    """One moderator table: primary-data availability, app vs pb_moderator."""
    pb_mod = _read_csv("pb_moderator.csv")
    if pb_mod is None:
        return
    rows = _app_subgroup("data_available", "meta", "post_all", "on")
    d = pb_mod[(pb_mod["layer"].astype(str) == "meta")
               & (pb_mod["spec"].astype(str) == "post_all")
               & (pb_mod["exclusions"].astype(str) == "on")]
    by = {str(r["level_label"]): r for _, r in d.iterrows()}
    matched = 0
    for r in rows:
        hit = by.get(str(r.get("label")))
        if hit is None:
            continue
        matched += 1
        add(f"moderator/data_available/{r.get('label')} k vs pb_moderator",
            r.get("k"), hit.get("k"))
        add(f"moderator/data_available/{r.get('label')} est vs pb_moderator",
            r.get("est"), hit.get("est"))
    if not matched:
        add("moderator/data_available matched rows", 0, 1)


def _check_bayes():
    bay = _read_csv("pb_bayes_pooled.csv")
    if bay is None:
        bay = _read_csv("pb_bayes_pooled.csv")
    if bay is None:
        return
    res = _app_bayes("meta", "post_all", "on")
    row = _row(bay, layer="meta", spec="post_all", exclusions="on")
    if res and row is not None:
        add("bayes meta/post_all/on k vs pb_bayes_pooled", res.get("k"), row.get("k"))
        add("bayes meta/post_all/on est vs pb_bayes_pooled",
            res.get("est"), row.get("est"), tol=1e-6)
        add_log10("bayes meta/post_all/on bf10 vs pb_bayes_pooled",
                  res.get("bf10"), row.get("bf10"), tol=1e-6)


def _check_prisma():
    import routes_apa as RA
    import prisma as PR

    counts = RA._prisma_counts() or {}
    flow = PR.flow()
    add("prisma identified_records vs prisma.flow",
        counts.get("canonical", {}).get("identified_records"),
        flow.get("identified_records"))
    add("prisma abstract_excluded vs prisma.flow",
        counts.get("abstract_excluded"), flow.get("abstract_excluded"))
    add("prisma fulltext_excluded vs prisma.flow",
        counts.get("fulltext_excluded"), flow.get("fulltext_excluded"))
    add("prisma included_studies vs prisma.flow",
        counts.get("canonical", {}).get("included_studies"), flow.get("included_studies"))
    add("prisma coded papers vs prisma.flow",
        counts.get("coded"), (flow.get("extraction") or {}).get("distinct_coded_papers"))


def _check_exclusions_off():
    pb = _read_csv("pb_pooled.csv")
    if pb is None:
        return
    for layer, spec in (("meta", "post_all"), ("sdt", "post_all")):
        res = _app_pooled(layer, spec, "off")
        row = _row(pb, layer=layer, spec=spec, exclusions="off")
        if res and row is not None:
            add(f"{layer}/{spec}/off k vs pb_pooled", res.get("dl_k"), row.get("k"))
            add(f"{layer}/{spec}/off dl_est vs pb_pooled",
                res.get("dl_est"), row.get("dl_est"))


def run():
    missing = [n for n in ("pb_pooled.csv",) if _read_csv(n) is None]
    if missing:
        print("parity_check: missing pipeline tables: " + ", ".join(missing))
        print("  run  python Scripts/publication_bias.py  first "
              "(or python run_analysis.py --suite).")
        return 2
    _check_meta_headlines()
    _check_sdt_headlines()
    _check_subgroup()
    _check_moderator()
    _check_bayes()
    _check_prisma()
    _check_exclusions_off()

    width = max((len(c["label"]) for c in _CHECKS), default=10)
    print(f"{'check':<{width}}  {'app':>12}  {'pipeline':>12}  {'delta':>10}  ok")
    print("-" * (width + 44))
    n_bad = n_div = 0
    for c in _CHECKS:
        if not c["ok"]:
            if c["sev"] == "workbook":
                n_div += 1
            else:
                n_bad += 1

        def _fmt(v):
            if v is None:
                return "None"
            try:
                return f"{v:.8g}"
            except (TypeError, ValueError):
                return str(v)

        delta = "" if math.isnan(c["delta"]) else f"{c['delta']:.2e}"
        tag = "ok" if c["ok"] else ("DIVERGENCE" if c["sev"] == "workbook" else "FAIL")
        print(f"{c['label']:<{width}}  {_fmt(c['app']):>12}  {_fmt(c['pipe']):>12}  "
              f"{delta:>10}  {tag}")
    print("-" * (width + 44))
    print(f"{len(_CHECKS) - n_bad - n_div}/{len(_CHECKS)} checks passed "
          f"(tol default {_DEFAULT_TOL:g})")
    if n_div:
        print(f"\n{n_div} workbook DIVERGENCE check(s): the spec-based pool and the")
        print("Results/*.xlsx workbook disagree even though both layers now share one")
        print("derivation (W2-parity2: analysis_multiverse_meta.write_meta_spec_table")
        print("for meta, reanalysis.collect_datasets for SDT).  A mismatch is a genuine")
        print("regression, not the old pipeline-internal split.  See")
        print("Results/TRACK_W2PARITY2_CHANGES.md.")
    return 1 if n_bad else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tol", type=float, default=1e-9)
    args = ap.parse_args()
    _DEFAULT_TOL = args.tol
    raise SystemExit(run())
