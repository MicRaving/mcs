"""Publication-bias and robustness analysis for the analysis suite.

Runs, for every specification (``did_all``, ``did_cred``, ``post_all``,
``post_cred``) on both layers (``meta`` = reported effects from
``Extraction/*.json``; ``sdt`` = signal-detection discrimination from the
harmonized data) and for exclusions ON / OFF:

* two-level random-effects model (effects nested in studies) with a
  fixed-effect + study-clustered sandwich SE as sensitivity;
* Egger's regression test / intercept and PET-PEESE (WLS on SE and on variance,
  PEESE preferred when the PET intercept differs from 0);
* trim-and-fill (Duval & Tweedie, L0 estimator) -- imputed studies and adjusted
  estimate;
* leave-one-out influence (contrast- and study-level);
* moderator contrast ``data_available`` vs ``paper_only`` (subgroups plus a
  meta-regression coefficient);
* p-curve right-skew and p-uniform (optional; only where p-values suffice).

Outputs ``Analysis_Suite_Results.xlsx`` in the project root and one CSV per table
in ``temp/``.  The prerequisite spec tables (``temp/meta_specs.csv`` /
``temp/sdt_specs.csv``) are built by ``Scripts/suite_common.py`` if missing.

Run:  python Scripts/publication_bias.py [--force-specs] [--subset name]
"""
import argparse
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import suite_common as SC  # noqa: E402
import specs_api as SPECS_API  # noqa: E402  (ONE spec-row selector)
import contract as CT  # noqa: E402

try:  # console-safe output for study names with non-cp1252 characters
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

B = SC.B
TMP = SC.TMP
# Contract (AGENTS.md): the consolidated publication-bias workbook lives under Results/
OUT_XLSX = os.path.join(B, "Results", "Analysis_Suite_Results.xlsx")

LAYERS = ["meta", "sdt"]
SPECS = ["did_all", "did_cred", "post_all", "post_cred",
         "persistence_all", "persistence_cred",
         "didpost_all", "didpost_cred", "fupost_all", "fupost_cred"]
EXCL = ["on", "off"]
PRIMARY_R = 0.5
MIN_K_EGGER = 3
MIN_K_TF = 4
MIN_N_PCURVE = 10
DOMAINS_REPORTED = ["credibility", "sharing_intention", "technique_recognition"]


def _z_p(est, se):
    if not (np.isfinite(est) and np.isfinite(se) and se > 0) or SC._st is None:
        return np.nan
    return float(2 * SC._st.norm.sf(abs(est) / se))


QUALITY_ABS_MAX = CT.DID_ABS_MAX


def select(specs, layer, spec, exclusions, primary_r=True):
    """Rows of one (layer x spec x exclusions) pool via the shared selector.

    All filters -- r_used==0.5, eligible_primary, exclusions, fully-within and
    the |DiD|<=DID_ABS_MAX quality guard (which also covers the derived
    ``didpost_*`` specs) -- live in :func:`specs_api.select_spec_rows`.
    """
    return SPECS_API.select_spec_rows(specs, layer=layer, spec=spec,
                                      exclusions=exclusions)


def select_raw(specs, layer, spec, exclusions):
    """Same as ``select`` without the meta-DiD quality guard (for counts)."""
    d = specs[(specs["layer"] == layer) & (specs["spec"] == spec)
              & (specs["exclusions"] == exclusions)].copy()
    if layer == "meta" and "r_used" in d.columns:
        d = d[d["r_used"].isna() | np.isclose(d["r_used"], PRIMARY_R)]
    return d[np.isfinite(d["effect"]) & np.isfinite(d["var"]) & (d["var"] > 0)]


def block(specs, layer, spec, exclusions):
    d = select(specs, layer, spec, exclusions)
    raw = select_raw(specs, layer, spec, exclusions)
    n_quality = max(0, len(raw) - len(d))
    yi = d["effect"].to_numpy(float)
    vi = d["var"].to_numpy(float)
    study = d["study_key"].astype(str).to_numpy()
    k = len(d)
    nst = len(set(study)) if k else 0
    out = {"layer": layer, "spec": spec, "exclusions": exclusions, "k": k,
           "n_studies": nst, "n_quality_excluded": n_quality}
    if not k:
        return out, d, None
    dl = SC.dl_random(yi, vi)
    two = SC.two_level_re(yi, vi, study)
    clus = SC.cluster_robust(yi, vi, study)
    out.update({
        "dl_est": dl["est"], "dl_se": dl["se"], "dl_lo": dl["lo"], "dl_hi": dl["hi"],
        "dl_tau2": dl["tau2"], "dl_i2": dl["i2"], "dl_p": _z_p(dl["est"], dl["se"]),
        "two_est": two["est"], "two_se": two["se"], "two_lo": two["lo"], "two_hi": two["hi"],
        "two_tau2_between": two["tau2_between"],
        "clus_est": clus["est"], "clus_se": clus["se"], "clus_lo": clus["lo"],
        "clus_hi": clus["hi"], "n_clusters": clus["n_clusters"],
    })
    # Dependence-aware pool (DEPENDENT_EFFECTS / Option C): 3-level model + RVE
    # clustered by cluster_id, rho = 0.5 / 0.8, with the largest-cluster weight
    # share.  The DL estimate above stays as the comparison column.
    cluster = (d["cluster_id"].astype(str).to_numpy()
               if "cluster_id" in d.columns else study)
    dep = SC.pool_dependence(yi, vi, cluster, study)
    for _k in ("three_k", "three_est", "three_se", "three_lo", "three_hi",
               "tau2_cluster", "tau2_study", "three_i2",
               "rve_est", "rve_se", "rve_lo", "rve_hi", "rve_df",
               "rve_rho80_est", "rve_rho80_se", "rve_rho80_lo", "rve_rho80_hi",
               "max_weight_share_dl", "max_weight_cluster_dl",
               "max_weight_share_rve", "max_weight_cluster_rve",
               "max_weight_share_rve_rho80", "n_studies"):
        if _k in dep:
            out[_k] = dep[_k]
    # publication-bias battery
    eg = SC.egger_pet(yi, vi)
    pt = SC.pet_peese(yi, vi)
    out.update({
        "egger_intercept": eg["intercept"], "egger_se": eg["se"], "egger_t": eg["t"],
        "egger_p": eg["p"], "pet": pt["pet"]["intercept"], "pet_p": pt["pet"]["p"],
        # Egger's asymmetry test = slope of effect on SE (the intercept is PET).
        "egger_slope": eg.get("slope"), "egger_slope_se": eg.get("slope_se_est"),
        "egger_slope_t": eg.get("slope_t"), "egger_slope_p": eg.get("slope_p"),
        "peese": pt["peese"]["intercept"], "peese_p": pt["peese"]["p"],
        "pet_peese_prefer": pt["prefer"], "pet_peese_est": pt["estimate"],
        "pet_peese_lo": pt["lo"], "pet_peese_hi": pt["hi"],
        "egger_status": "ok" if k >= MIN_K_EGGER else f"not assessed (k={k}<{MIN_K_EGGER})",
    })
    tf = SC.trim_and_fill(yi, vi)
    out.update({
        "tf_k0": tf["k0"], "tf_side": tf["side"], "tf_obs_est": tf["observed"]["est"],
        "tf_adj_est": tf["adjusted"], "tf_adj_se": tf["adjusted_se"],
        "tf_adj_lo": tf["adjusted_lo"], "tf_adj_hi": tf["adjusted_hi"],
        "tf_status": "ok" if k >= MIN_K_TF else f"not assessed (k={k}<{MIN_K_TF})",
    })
    pc = SC.p_curve(d["p_value"].to_numpy(float) if "p_value" in d.columns else np.array([]))
    pu = SC.p_uniform(yi, vi)
    out.update({
        "pc_n_sig": pc["n_sig"], "pc_n_below_025": pc["n_below_025"],
        "pc_prop_below_025": pc["prop_below_025"], "pc_p_right_skew": pc["p_right_skew"],
        "p_uniform_delta": pu["delta"],
        "pcurve_status": "ok" if pc["n_sig"] >= MIN_N_PCURVE
        else f"not assessed (n_sig={pc['n_sig']}<{MIN_N_PCURVE})",
    })
    # moderator: data_available vs paper_only
    if "data_available" in d.columns:
        mod = pd.to_numeric(d["data_available"], errors="coerce").fillna(0).astype(int)
    else:
        mod = pd.Series(np.zeros(len(d), int), index=d.index)
    levels = sorted(set(mod))
    out["moderator_levels"] = ",".join(str(x) for x in levels)
    out["moderator_status"] = ("ok" if len(levels) > 1
                               else f"not assessed (single level: data_available={levels[0] if levels else 'n/a'})")
    return out, d, mod.to_numpy()


def loo_rows(specs, layer, spec, exclusions):
    d = select(specs, layer, spec, exclusions)
    rows = []
    if len(d) < 3:
        return rows
    yi = d["effect"].to_numpy(float)
    vi = d["var"].to_numpy(float)
    for unit_type, unit, is_study in (("contrast", d["contrast_id"].astype(str).to_numpy(), False),
                                      ("study", d["study_key"].astype(str).to_numpy(), True)):
        full, loo = SC.leave_one_out(yi, vi, unit, is_study=is_study)
        for _, r in loo.iterrows():
            rows.append({"layer": layer, "spec": spec, "exclusions": exclusions,
                         "unit_type": unit_type, "omitted": r["omitted"],
                         "k_omitted": int(r["k_omitted"]), "full_est": full["est"],
                         "est": r["est"], "lo": r["lo"], "hi": r["hi"],
                         "delta": r["delta"], "influential": int(r["influential"])})
    return rows


def domain_rows(specs, layer, spec, exclusions):
    d = select(specs, layer, spec, exclusions)
    rows = []
    if not len(d):
        return rows
    for dom, g in d.groupby("dv_domain"):
        r = SC.dl_random(g["effect"], g["var"])
        rows.append({"layer": layer, "spec": spec, "exclusions": exclusions,
                     "dv_domain": dom, "k": r["k"], "n_studies": g["study_key"].nunique(),
                     "est": r["est"], "se": r["se"], "lo": r["lo"], "hi": r["hi"],
                     "tau2": r["tau2"], "i2": r["i2"],
                     "reported_domain": int(dom in DOMAINS_REPORTED)})
    return rows


def sdt_component_rows(specs, layer, spec, exclusions):
    """SDT-layer decomposition: pool true score, fake score and response bias
    separately (the meta-layer publication-bias battery is not meaningful for an
    SDT discrimination index, so it is replaced by this table).  Discrimination
    itself is the pooled overall effect and is not repeated here."""
    if layer != "sdt":
        return []
    d = select(specs, layer, spec, exclusions)
    rows = []
    if not len(d):
        return rows
    for label, ecol, vcol in (("True score", "true_score", "true_var"),
                              ("Fake score", "fake_score", "fake_var"),
                              ("Response bias (criterion c)", "criterion_score", "criterion_var")):
        if ecol not in d.columns or vcol not in d.columns:
            continue
        y = pd.to_numeric(d[ecol], errors="coerce").to_numpy(float)
        v = pd.to_numeric(d[vcol], errors="coerce").to_numpy(float)
        m = np.isfinite(y) & np.isfinite(v) & (v > 0)
        if not m.any():
            continue
        r = SC.dl_random(y[m], v[m])
        rows.append({"layer": layer, "spec": spec, "exclusions": exclusions,
                     "component": label, "k": r["k"],
                     "n_studies": len(set(d["study_key"].to_numpy()[m])),
                     "est": r["est"], "se": r["se"], "lo": r["lo"], "hi": r["hi"],
                     "tau2": r["tau2"], "i2": r["i2"]})
    return rows


def moderator_rows(specs, layer, spec, exclusions):
    d = select(specs, layer, spec, exclusions)
    rows = []
    if not len(d):
        return rows
    mod = pd.to_numeric(d["data_available"], errors="coerce").fillna(0).astype(int)
    levels = sorted(set(mod))
    for lev in levels:
        g = d[mod == lev]
        r = SC.dl_random(g["effect"], g["var"])
        rows.append({"layer": layer, "spec": spec, "exclusions": exclusions,
                     "moderator": "Primary data availability", "level": int(lev),
                     "level_label": "Primary data available" if lev == 1 else "Paper only",
                     "k": r["k"], "n_studies": g["study_key"].nunique(),
                     "est": r["est"], "se": r["se"], "lo": r["lo"], "hi": r["hi"]})
    if len(levels) > 1:
        # D2: ONE inference convention -- t(G-1) with an exchangeable working
        # correlation (clustered by cluster_id), model-based t(k-2) kept as a
        # sensitivity column.
        groups = (d["cluster_id"].astype(str).to_numpy() if "cluster_id" in d.columns
                  else d["study_key"].astype(str).to_numpy())
        res = SC.wls(d["effect"].to_numpy(float), mod.to_numpy(float),
                     1 / d["var"].to_numpy(float), groups=groups, rho=0.0)
        lo = res.get("b_lo", res["b"] - 1.96 * res["se_b"])
        hi = res.get("b_hi", res["b"] + 1.96 * res["se_b"])
        rows.append({"layer": layer, "spec": spec, "exclusions": exclusions,
                     "moderator": "Primary data availability", "level": np.nan,
                     "level_label": "Difference (meta-regression)", "k": res["k"],
                     "n_studies": d["study_key"].nunique(),
                     "est": res["b"], "se": res["se_b"], "lo": lo,
                     "hi": hi, "t": res["t_b"], "p": res["p_b"],
                     "df": res.get("df_crve", res.get("df")),
                     "n_clusters": res.get("n_clusters"),
                     "se_model": res.get("se_b_model"),
                     "t_model": res.get("t_b_model"),
                     "p_model": res.get("p_b_model")})
    return rows


def r_sensitivity(specs):
    rows = []
    meta = specs[(specs["layer"] == "meta") & (specs["spec"].isin(["did_all", "did_cred"]))]
    for (spec, excl, r), g in meta.groupby(["spec", "exclusions", "r_used"], dropna=False):
        g = g[g["effect"].abs() <= QUALITY_ABS_MAX]
        g = g[np.isfinite(g["effect"]) & np.isfinite(g["var"]) & (g["var"] > 0)]
        if not len(g):
            continue
        rres = SC.dl_random(g["effect"], g["var"])
        rows.append({"layer": "meta", "spec": spec, "exclusions": excl, "r_used": r,
                     "k": rres["k"], "n_studies": g["study_key"].nunique(),
                     "est": rres["est"], "se": rres["se"], "lo": rres["lo"],
                     "hi": rres["hi"], "tau2": rres["tau2"], "i2": rres["i2"]})
    return rows


def exclusions_rows(pooled):
    rows = []
    for layer in LAYERS:
        for spec in SPECS:
            on = pooled[(pooled["layer"] == layer) & (pooled["spec"] == spec) & (pooled["exclusions"] == "on")]
            off = pooled[(pooled["layer"] == layer) & (pooled["spec"] == spec) & (pooled["exclusions"] == "off")]
            if not len(on) and not len(off):
                continue
            o = on.iloc[0] if len(on) else None
            f = off.iloc[0] if len(off) else None
            rows.append({
                "layer": layer, "spec": spec,
                "on_k": int(o["k"]) if o is not None else 0,
                "on_est": o["dl_est"] if o is not None else np.nan,
                "on_lo": o["dl_lo"] if o is not None else np.nan,
                "on_hi": o["dl_hi"] if o is not None else np.nan,
                "off_k": int(f["k"]) if f is not None else 0,
                "off_est": f["dl_est"] if f is not None else np.nan,
                "off_lo": f["dl_lo"] if f is not None else np.nan,
                "off_hi": f["dl_hi"] if f is not None else np.nan,
                "delta_off_minus_on": (f["dl_est"] - o["dl_est"]) if (o is not None and f is not None) else np.nan,
                "note": ("meta layer: coded effects embed the author sample "
                         "(no coded->harmonized DV crosswalk); identical by construction"
                         if layer == "meta" else "SDT layer: exclusion flags re-applied"),
            })
    return rows


def inventory_rows(specs, pooled):
    rows = []
    for layer in LAYERS:
        for spec in SPECS:
            for excl in EXCL:
                d = select(specs, layer, spec, excl)
                raw = select_raw(specs, layer, spec, excl)
                nq = max(0, len(raw) - len(d))
                p = pooled[(pooled["layer"] == layer) & (pooled["spec"] == spec) & (pooled["exclusions"] == excl)]
                k = len(d)
                if not k:
                    rows.append({"layer": layer, "spec": spec, "exclusions": excl, "k": 0,
                                 "n_studies": 0, "n_data_available": 0, "n_paper_only": 0,
                                 "n_fallback_sharing": 0, "n_quality_excluded": nq,
                                 "egger_status": "no contrasts",
                                 "tf_status": "no contrasts", "pcurve_status": "no contrasts",
                                 "moderator_status": "no contrasts"})
                    continue
                da = int((pd.to_numeric(d["data_available"], errors="coerce").fillna(0) == 1).sum())
                fb = int((d["fallback"].astype(str) == "sharing_intention").sum())
                pr = p.iloc[0]
                rows.append({
                    "layer": layer, "spec": spec, "exclusions": excl, "k": k,
                    "n_studies": d["study_key"].nunique(),
                    "n_data_available": da, "n_paper_only": k - da,
                    "n_fallback_sharing": fb, "n_quality_excluded": nq,
                    "egger_status": pr["egger_status"], "tf_status": pr["tf_status"],
                    "pcurve_status": pr["pcurve_status"], "moderator_status": pr["moderator_status"],
                })
    return rows


def metric_rows(specs, layer, spec, exclusions):
    """Pooled estimate per effect-size metric family (robustness for the mixed
    reported-effects scale)."""
    d = select(specs, layer, spec, exclusions)
    rows = []
    if not len(d):
        return rows
    for metric, g in d.groupby("metric"):
        r = SC.dl_random(g["effect"], g["var"])
        rows.append({"layer": layer, "spec": spec, "exclusions": exclusions,
                     "metric": metric, "k": r["k"], "n_studies": g["study_key"].nunique(),
                     "est": r["est"], "se": r["se"], "lo": r["lo"], "hi": r["hi"],
                     "tau2": r["tau2"], "i2": r["i2"]})
    return rows


def intervention_rows(specs, layer, spec, exclusions):
    """Pooled effect per intervention type (meta layer; paper-ready labels).

    Multi-type experiments contribute to every type in their list (membership),
    except a contrast bound to one intervention (``intervention_id``), which
    contributes only to that intervention's type.  The overall "All interventions"
    row is pooled from the un-exploded frame so a contrast is never double-counted.
    """
    d0 = select(specs, layer, spec, exclusions)
    if not len(d0) or layer != "meta":
        return []
    d = SC.explode_intervention_types(d0, layer)
    rows = []
    for itype, g in d.groupby("intervention_type"):
        r = SC.dl_random(g["effect"], g["var"])
        rows.append({"layer": layer, "spec": spec, "exclusions": exclusions,
                     "intervention_type": itype, "k": r["k"],
                     "n_studies": g["study_key"].nunique(), "est": r["est"],
                     "se": r["se"], "lo": r["lo"], "hi": r["hi"],
                     "tau2": r["tau2"], "i2": r["i2"]})
    r = SC.dl_random(d0["effect"], d0["var"])
    rows.append({"layer": layer, "spec": spec, "exclusions": exclusions,
                 "intervention_type": "All interventions", "k": r["k"],
                 "n_studies": d0["study_key"].nunique(), "est": r["est"],
                 "se": r["se"], "lo": r["lo"], "hi": r["hi"],
                 "tau2": r["tau2"], "i2": r["i2"]})
    return rows


def intervention_membership_rows(specs, layer, spec, exclusions):
    """Per-intervention membership rows (PER_INTERVENTION_MODEL.md).

    Explodes each contrast to the interventions it belongs to (``intervention_id``
    then ``intervention_type``); empty unless some record carries ``Interventions``,
    so single-type records reproduce the legacy output exactly.
    """
    d0 = select(specs, layer, spec, exclusions)
    if not len(d0) or layer != "meta" or "intervention_id" not in d0.columns:
        return []
    d = d0[d0["intervention_id"].astype(str).str.strip() != ""]
    if not len(d):
        return []
    rows = []
    for (iid, itype), g in d.groupby(["intervention_id", "intervention_type"]):
        r = SC.dl_random(g["effect"], g["var"])
        rows.append({"layer": layer, "spec": spec, "exclusions": exclusions,
                     "intervention_id": iid,
                     "intervention_type": SC.intervention_type_label(itype),
                     "k": r["k"], "n_studies": g["study_key"].nunique(),
                     "est": r["est"], "se": r["se"], "lo": r["lo"], "hi": r["hi"],
                     "tau2": r["tau2"], "i2": r["i2"]})
    return rows


def sdt_available_sensitivity(specs):
    """Option (a) sensitivity: meta layer pooled with all reported contrasts vs
    restricted to primary-data-available (``data_available == 1``) studies.

    The DEFAULT primary remains the mixed pool (option b): restricting the meta
    layer to SDT-available studies would bias the corpus, so it is reported only
    as this clearly-named sensitivity (see ``--sdt-available-only``).
    """
    rows = []
    for spec in SPECS:
        for excl in EXCL:
            d = select(specs, "meta", spec, excl)
            if not len(d):
                continue
            da = pd.to_numeric(d.get("data_available"), errors="coerce").fillna(0).astype(int)
            for label, mask in (("all_reported_contrasts", np.ones(len(d), bool)),
                                ("sdt_available_only", (da == 1).to_numpy())):
                g = d[mask]
                if not len(g):
                    continue
                cluster = (g["cluster_id"].astype(str).to_numpy()
                           if "cluster_id" in g.columns else g["study_key"].astype(str).to_numpy())
                dep = SC.pool_dependence(g["effect"], g["var"], cluster, g["study_key"])
                rows.append({"layer": "meta", "spec": spec, "exclusions": excl,
                             "subset": label, "k": dep["dl_k"], "n_studies": dep["n_studies"],
                             "dl_est": dep["dl_est"], "three_est": dep["three_est"],
                             "rve_est": dep["rve_est"], "rve_lo": dep["rve_lo"],
                             "rve_hi": dep["rve_hi"],
                             "max_weight_share_rve": dep["max_weight_share_rve"]})
    return rows


def bayes_rows(specs):
    """Bayesian random-effects pooled estimate per (layer x spec x exclusions).

    D5: reproduces the app's Bayesian layer from the pipeline so every app number
    is reproducible.  The tau grid is truncated at ``BAYES_TAU_MAX`` (disclosed
    in the ``tau_max`` column).

    Also reports the heterogeneity Bayes factor (``bf01_tau0``: evidence for
    tau = 0) and a Bayes-appropriate small-study-effect analysis: a Bayesian
    meta-regression of the effect on its standard error, whose intercept is the
    Bayesian PET estimate and whose slope is the Bayesian Egger asymmetry slope
    (with the posterior probability that the slope is positive).
    """
    rows = []
    for layer in LAYERS:
        for spec in SPECS:
            for excl in EXCL:
                d = select(specs, layer, spec, excl)
                if not len(d):
                    continue
                y = d["effect"].to_numpy(float)
                v = d["var"].to_numpy(float)
                r = SC._sc.bayes_pool(y, v)
                if r is None:
                    continue
                # ---- Bayes-appropriate small-study-effect analysis --------------
                # X = [1, SE]: the intercept is the Bayesian PET bias-adjusted
                # estimate and the slope the Bayesian Egger asymmetry test.  On a
                # within-person SDT discrimination index this is not meaningful; the
                # columns are left blank for the SDT layer (mirrors the frequentist
                # battery, which is replaced by the true/fake/criterion components).
                pet_int = pet_lo = pet_hi = egg_slope = egg_lo = egg_hi = egg_ppos = np.nan
                if layer != "sdt" and len(y) >= 4:
                    se = np.sqrt(v)
                    try:
                        bm = SC._sc.bayes_meta(list(y), [[1.0, float(s)] for s in se], list(v))
                    except Exception:
                        bm = None
                    terms = (bm or {}).get("terms") or []
                    if len(terms) >= 2:
                        pet_int, pet_lo, pet_hi = (terms[0]["estimate"], terms[0]["lo"],
                                                   terms[0]["hi"])
                        egg_slope = terms[1]["estimate"]
                        egg_lo, egg_hi = terms[1]["lo"], terms[1]["hi"]
                        egg_ppos = terms[1].get("prob_gt0")
                rows.append({
                    "layer": layer, "spec": spec, "exclusions": excl, "k": r["k"],
                    "est": r["est"], "se": r["se"], "lo": r["lo"], "hi": r["hi"],
                    "p": r["p"], "tau": r["tau"], "tau2": r["tau2"],
                    "tau_lo": r["tau_lo"], "tau_hi": r["tau_hi"],
                    "bf10": r["bf10"], "bf01_tau0": r.get("bf01_tau0"),
                    "prior_mu_sd": r["prior_mu_sd"],
                    "prior_tau_sd": r["prior_tau_sd"], "tau_prior": r["tau_prior"],
                    "tau_max": r["tau_max"], "grid_n": r["grid_n"],
                    "bayes_pet_intercept": pet_int, "bayes_pet_lo": pet_lo,
                    "bayes_pet_hi": pet_hi, "bayes_egger_slope": egg_slope,
                    "bayes_egger_slope_lo": egg_lo, "bayes_egger_slope_hi": egg_hi,
                    "bayes_egger_slope_p_pos": egg_ppos})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force-specs", action="store_true")
    ap.add_argument("--include-fully-within", action="store_true",
                    help="Include fully-within (single-arm, no-control) studies "
                         "in the pooled specifications (default: excluded).")
    ap.add_argument("--sdt-available-only", action="store_true",
                    help="Option (a) sensitivity: restrict the META layer to "
                         "primary-data-available studies (NOT the default mixed pool).")
    args = ap.parse_args()
    if args.include_fully_within:
        os.environ["INCLUDE_FULLY_WITHIN"] = "1"
    SC.announce_fully_within("publication bias")
    SC.ensure_specs(force=args.force_specs)
    specs = SC.load_specs()
    specs["study_key"] = specs["study_key"].astype(str)
    out_xlsx = OUT_XLSX
    if args.sdt_available_only:
        keep = (specs["layer"] != "meta") | (
            pd.to_numeric(specs.get("data_available"), errors="coerce").fillna(0) == 1)
        specs = specs[keep].copy()
        out_xlsx = OUT_XLSX.replace(".xlsx", "_SDTonly.xlsx")
        print("option (a): meta layer restricted to data_available == 1 ->", out_xlsx)

    pooled, loo, dom, mod = [], [], [], []
    metric = []
    itype = []
    ivmem = []
    sdtc = []
    for layer in LAYERS:
        for spec in SPECS:
            for excl in EXCL:
                row, d, m = block(specs, layer, spec, excl)
                pooled.append(row)
                loo += loo_rows(specs, layer, spec, excl)
                dom += domain_rows(specs, layer, spec, excl)
                mod += moderator_rows(specs, layer, spec, excl)
                metric += metric_rows(specs, layer, spec, excl)
                itype += intervention_rows(specs, layer, spec, excl)
                ivmem += intervention_membership_rows(specs, layer, spec, excl)
                sdtc += sdt_component_rows(specs, layer, spec, excl)

    pooled = pd.DataFrame(pooled)
    loo = pd.DataFrame(loo)
    dom = pd.DataFrame(dom)
    mod = pd.DataFrame(mod)
    rsens = pd.DataFrame(r_sensitivity(specs))
    excl_tbl = pd.DataFrame(exclusions_rows(pooled))
    inv = pd.DataFrame(inventory_rows(specs, pooled))
    metric = pd.DataFrame(metric)
    itype = pd.DataFrame(itype)
    ivmem = pd.DataFrame(ivmem)
    sdtc = pd.DataFrame(sdtc)
    bay = pd.DataFrame(bayes_rows(specs))
    if len(bay):
        bay.to_csv(os.path.join(B, "Results", "bayes_pooled.csv"),
                   index=False, encoding="utf-8-sig")

    # a tidy publication-bias-only view
    pb = pooled[["layer", "spec", "exclusions", "k", "n_studies", "n_quality_excluded",
                 "egger_intercept", "egger_se", "egger_t", "egger_p",
                 "egger_slope", "egger_slope_se", "egger_slope_t", "egger_slope_p",
                 "pet", "pet_p", "peese", "peese_p", "pet_peese_prefer",
                 "pet_peese_est", "pet_peese_lo", "pet_peese_hi", "egger_status",
                 "tf_k0", "tf_side", "tf_obs_est", "tf_adj_est", "tf_adj_se",
                 "tf_adj_lo", "tf_adj_hi", "tf_status",
                 "pc_n_sig", "pc_n_below_025", "pc_prop_below_025", "pc_p_right_skew",
                 "p_uniform_delta", "pcurve_status"]].copy()

    tables = {
        "Pooled": pooled,
        "Publication_Bias": pb,
        "Moderator": mod,
        "LeaveOneOut": loo,
        "Domain": dom,
        "Metric_Family": metric,
        "Intervention_Type": itype,
        "Intervention_Membership": ivmem,
        "Exclusions_OnOff": excl_tbl,
        "r_Sensitivity": rsens,
        "Spec_Inventory": inv,
        "SDT_Components": sdtc,
        "Bayes_Pooled": bay,
        "Sensitivity_SDT_Available": pd.DataFrame(sdt_available_sensitivity(specs)),
    }
    for name, df in tables.items():
        df.to_csv(os.path.join(TMP, f"pb_{name.lower()}.csv"), index=False, encoding="utf-8-sig")

    with pd.ExcelWriter(out_xlsx, engine="openpyxl") as xl:
        for name, df in tables.items():
            df.to_excel(xl, sheet_name=name[:31], index=False)

    print(f"wrote {out_xlsx}")
    for name, df in tables.items():
        print(f"  {name:20s} {len(df):6d} rows")
    # concise assessment summary
    print("\nPublication-bias assessability:")
    merged = inv.merge(pb[["layer", "spec", "exclusions", "tf_k0", "tf_adj_est"]],
                       on=["layer", "spec", "exclusions"], how="left")
    for _, r in merged.iterrows():
        if not r["k"]:
            continue
        issues = []
        if r["egger_status"] != "ok":
            issues.append(r["egger_status"])
        if r["tf_status"] != "ok":
            issues.append(r["tf_status"])
        if r["pcurve_status"] != "ok":
            issues.append(r["pcurve_status"])
        if r["moderator_status"] != "ok":
            issues.append(r["moderator_status"])
        print(f"  {r['layer']:4s} {r['spec']:9s} excl={r['exclusions']:3s} k={int(r['k']):4d} "
              + ("; ".join(issues) if issues else "all assessed"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
