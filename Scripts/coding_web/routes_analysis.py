"""Analysis routes: moderators, pooling, meta-regression, subgroup, export, tables and runs."""

from datetime import datetime
from flask import Blueprint, abort, jsonify, request, send_file
import collections
import glob
import math
import os
import re
from analysis_lib import ANALYSIS_RUN_KINDS, ANALYSIS_TABLES, DERIVED_SPECS, SDT_AUXILIARY_SPECS, SUSCEPTIBILITY, _APA_SPEC, _CONTRAST_NAMES, _analysis_files, _analysis_run, _apply_study_filter, _bayes_domain, _bayes_for, _bayes_groups, _bayes_rows, _bayes_sensitivity, _csv_rowcount, _derived_pooled_row, _derived_spec_options, _export_pptx, _export_xlsx, _figure_index, _figures_dir, _filter_args, _human_mod_label, _inputs_fingerprint, _level_cap, _loo_summary, _mod_label, _moderator_catalog, _moderator_figures, _moderator_level_effects, _mods_for_row, _participant_cov_names, _participant_rows_for, _prewarm_analysis, _read_table, _robustness_combined, _spec_options, _spec_rows_for, _specs_stale_flag, _study_moderators, _tables_stale_flag, bf_presentation, spec_estimand, start_background_run
from statslib import BAYES_MU_SD_PRESETS, BAYES_TAU_MAX, BAYES_TAU_PRESETS, _bayes_meta, _bayes_pool, _bayes_tau_within, _chi2_sf, _dl_pool, _mat_inv, _resolve_bayes_priors, _study_level, _t_ppf, _t_sf, _two_level_components, _wls_cluster
from studio_lib import _norm, _values
from studio_state import FROZEN, S

bp = Blueprint("analysis", __name__)


@bp.get("/api/analysis/moderators")
def api_analysis_moderators():
    cat = _moderator_catalog()
    return jsonify({"featured": cat["featured"], "groups": cat["groups"],
                    "module_groups": cat.get("module_groups", []),
                    "moderators": cat["moderators"]})

@bp.get("/api/analysis/bayes")
def api_analysis_bayes():
    layer = (request.args.get("layer") or "meta").strip()
    spec = (request.args.get("spec") or "").strip()
    exclusions = (request.args.get("exclusions") or "on").strip().lower()
    scope = (request.args.get("scope") or "unrestricted").strip()
    rows = _spec_rows_for(layer, spec, exclusions)
    if scope == "restricted":
        rows = [r for r in rows if r["dv_domain"] in SUSCEPTIBILITY]
    rows = _apply_study_filter(rows, *_filter_args(request))
    rows = _bayes_rows(rows)
    if not rows:
        return jsonify({"ok": False, "error": "no contrasts for this selection",
                        "layer": layer, "spec": spec, "exclusions": exclusions})
    mu_sd, tau_prior, tau_sd = _resolve_bayes_priors(request.args)
    pooled = _bayes_pool([r["effect"] for r in rows], [r["var"] for r in rows],
                         mu_sd=mu_sd, tau_sd=tau_sd, tau_prior=tau_prior)
    pooled["n_studies"] = len({r["study_key"] for r in rows})
    # W6-AN: the normal-normal Savage-Dickey BF over thousands of contrasts can
    # overflow to ~1e200; never hand a raw 1e204 to the UI.  These fields add a
    # capped/log10 presentation WITHOUT touching the estimator.
    _prior_note = (f"prior: \u03bc ~ N(0, {mu_sd}\u00b2), "
                   f"\u03c4 ~ {tau_prior}({tau_sd})")
    pooled["bf10_present"] = bf_presentation(pooled.get("bf10"), _prior_note)
    pooled["bf01_tau0_present"] = bf_presentation(pooled.get("bf01_tau0"), _prior_note)
    resp = {
        "ok": True, "model": "bayes", "layer": layer, "spec": spec,
        "exclusions": exclusions, "scope": scope, "pooled": pooled,
        "domain": _bayes_domain(rows, mu_sd, tau_sd, tau_prior),
        "age": _bayes_groups(rows, "Age_Group", mu_sd, tau_sd, tau_prior),
        "intervention": _bayes_groups(rows, "Intervention_Type", mu_sd, tau_sd, tau_prior),
        "data_available": _bayes_groups(rows, "data_available", mu_sd, tau_sd, tau_prior),
        "prior": {"mu_sd": mu_sd, "tau_sd": tau_sd, "tau_prior": tau_prior,
                  "tau_max": BAYES_TAU_MAX},
        "presets": {"mu_sd": list(BAYES_MU_SD_PRESETS),
                    "tau": [{"kind": k, "sd": s} for k, s in BAYES_TAU_PRESETS]},
    }
    # Bayes-appropriate small-study-effect analysis (SE-regression = Bayesian PET
    # intercept + Egger asymmetry slope).  Not meaningful for the within-person SDT
    # discrimination index, so it is reported for the meta layer only.
    if layer != "sdt" and len(rows) >= 4:
        try:
            _se = [math.sqrt(r["var"]) for r in rows]
            _bm = _bayes_meta([r["effect"] for r in rows], [[1.0, s] for s in _se],
                              [r["var"] for r in rows],
                              mu_sd=mu_sd, tau_sd=tau_sd, tau_prior=tau_prior)
        except Exception:
            _bm = None
        _terms = (_bm or {}).get("terms") or []
        if len(_terms) >= 2:
            pet, egg = _terms[0], _terms[1]
            resp["bias"] = {
                "pet": pet["estimate"], "pet_lo": pet["lo"], "pet_hi": pet["hi"],
                "egger_slope": egg["estimate"], "egger_slope_lo": egg["lo"],
                "egger_slope_hi": egg["hi"], "egger_p_pos": egg.get("prob_gt0"),
                "k": len(rows),
            }
    if (request.args.get("sensitivity") or "").strip().lower() in ("1", "true", "yes"):
        resp["sensitivity"] = _bayes_sensitivity(rows)
    return jsonify(resp)

@bp.get("/api/analysis/pooled")
def api_analysis_pooled():
    """Overall (pooled) analysis for the current selection, optionally restricted
    to a subgroup of studies (filter_name / filter_value)."""
    layer = (request.args.get("layer") or "meta").strip()
    spec = (request.args.get("spec") or "").strip()
    exclusions = (request.args.get("exclusions") or "on").strip().lower()
    scope = (request.args.get("scope") or "unrestricted").strip()
    fname, fvalue = _filter_args(request)
    rows = _spec_rows_for(layer, spec, exclusions)
    if scope == "restricted":
        rows = [r for r in rows if r["dv_domain"] in SUSCEPTIBILITY]
    rows = _apply_study_filter(rows, fname, fvalue)
    rows = _bayes_rows(rows)
    if not rows:
        return jsonify({"ok": False, "error": "no contrasts for this selection",
                        "filter": {"name": fname, "value": fvalue}})
    y = [r["effect"] for r in rows]
    v = [r["var"] for r in rows]
    groups = [r["study_key"] for r in rows]
    mu_sd, tau_prior, tau_sd = _resolve_bayes_priors(request.args)
    dl = _dl_pool(y, v)
    comp = _two_level_components(y, v, groups)
    st_y, st_v, _st_X, _st_ids = _study_level(y, [[1.0] for _ in y], v, groups)
    two = _dl_pool(st_y, st_v)
    comp.update({"est": two["est"], "se": two["se"], "lo": two["lo"], "hi": two["hi"]})
    fit = _wls_cluster(y, [[1.0] for _ in y], [1.0 / vi for vi in v], groups)
    bayes = _bayes_pool(y, v, mu_sd=mu_sd, tau_sd=tau_sd, tau_prior=tau_prior)
    if bayes:
        bayes["n_studies"] = len(set(groups))
    return jsonify({
        "ok": True, "k": len(rows), "n_studies": len(set(groups)),
        "dl": dl,
        "two_level": comp,
        "cluster": ({"est": fit["beta"][0], "se": fit["se"][0],
                     "n_studies": fit["n_studies"]} if fit else None),
        "bayes": bayes,
        "filter": {"name": fname, "value": fvalue},
        "layer": layer, "spec": spec, "exclusions": exclusions, "scope": scope,
        # C1: label every synthesis row with its estimand so the front end never
        # presents the arm-pooled discrimination level as the intervention effect.
        # W6-AN: the label is now LAYER-SPECIFIC — meta = reported standardised
        # effect (Hedges g), sdt = discrimination difference d' = z(HR) - z(FAR).
        "spec_label": _APA_SPEC.get(spec, spec),
        "estimand": spec_estimand(layer, spec),
        "auxiliary": spec in SDT_AUXILIARY_SPECS,
    })

@bp.post("/api/analysis/moderate")
def api_analysis_moderate():
    payload = request.get_json(force=True, silent=True) or {}
    layer = (payload.get("layer") or "meta").strip()
    spec = (payload.get("spec") or "").strip()
    exclusions = (payload.get("exclusions") or "on").strip().lower()
    scope = (payload.get("scope") or "unrestricted").strip()
    cat = _moderator_catalog()
    kind_of = cat["kinds"]
    moderators = [m for m in (payload.get("moderators") or []) if m in kind_of]
    interactions = [(a, b) for a, b in (payload.get("interactions") or [])
                    if a in moderators and b in moderators and a != b]
    # SDT layer: the re-analysis can target the discrimination index (default)
    # or one of its decomposition components.
    outcome = (payload.get("outcome") or "effect").strip()
    ycol, vcol = {
        "true_score": ("true_score", "true_var"),
        "fake_score": ("fake_score", "fake_var"),
        "criterion": ("criterion_score", "criterion_var"),
    }.get(outcome, ("effect", "var"))
    model = (payload.get("model") or "freq").strip().lower()

    pcov_set = _participant_cov_names()
    pcovs = [m for m in moderators if m in pcov_set]
    if len(pcovs) > 1:
        return jsonify({"ok": False, "error": "select one participant-level moderator at a time"}), 200
    if pcovs:
        rows = _participant_rows_for(pcovs[0], layer, spec, exclusions,
                                     include_fully_within=payload)
    else:
        rows = _spec_rows_for(layer, spec, exclusions, include_fully_within=payload)
    if scope == "restricted":
        rows = [r for r in rows if r["dv_domain"] in SUSCEPTIBILITY]
    _fname = (payload.get("filter_name") or "").strip()
    _fval = (payload.get("filter_value") or "").strip()
    if _fname and _fval:
        rows = _apply_study_filter(rows, _fname, _fval)
    # Per-intervention model (PER_INTERVENTION_MODEL.md): resolve each effect row's
    # moderators against its own intervention's effective coding, so an arm-level
    # override is honoured instead of being flattened to the study coding.
    data = []
    # A multi-valued moderator cell (e.g. Intervention_Type =
    # ["inoculation", "friction"]) used to be set to None and the study dropped
    # from the meta-regression.  Instead, substitute the study's FIRST value so a
    # multi-intervention study enters the model under its primary (first) label.
    # The data layer orders each study's list with the most prominent/most
    # specific label first, so vals[0] is that primary label.
    multi_valued = collections.defaultdict(set)   # moderator -> {study_key}
    for r in rows:
        m = _mods_for_row(r)
        rec = dict(r)
        for name in moderators:
            if name in _CONTRAST_NAMES:
                # contrast-level moderator: read from the effect row, so it can
                # vary within a study (DV type, veracity, contrast type).
                rec[name] = _norm(r.get(name))
                continue
            if name in pcov_set:
                # participant-level covariate: the row already carries the level
                rec[name] = _norm(r.get("participant_level"))
                continue
            vals = _values(m.get(name))
            if vals:
                # Single values pass through unchanged; a multi-valued cell is
                # substituted with its first value (see comment above) so it is
                # never dropped. Track it for the panel disclosure below.
                rec[name] = vals[0]
                if len(vals) > 1:
                    multi_valued[name].add(r["study_key"])
            else:
                rec[name] = ""
        data.append(rec)

    def valid(rec):
        for name in moderators:
            v = rec.get(name)
            if v in (None, ""):
                return False
            kind = kind_of.get(name)
            if kind == "continuous":
                try:
                    float(v)
                except (TypeError, ValueError):
                    return False
        return True

    kept = [r for r in data if valid(r)]
    if ycol != "effect":
        kept = [r for r in kept if isinstance(r.get(ycol), float)
                and isinstance(r.get(vcol), float) and r[vcol] > 0]
    dropped = len(data) - len(kept)
    # Per-moderator pictures are independent of the meta-regression fit, so they
    # are produced (and returned) even when the model itself cannot be fitted.
    level_effects = _moderator_level_effects(kept, moderators, kind_of, model)
    mod_figures = _moderator_figures(kept, moderators, kind_of, level_effects)
    if len(kept) < max(3, len(moderators) + 2):
        return jsonify({"ok": False, "error": f"too few contrasts ({len(kept)}) after joining "
                        f"moderators; {dropped} dropped", "k": len(kept), "dropped": dropped,
                        "figures": mod_figures, "outcome": outcome}), 200

    # Disclose the first-value substitution to the panel.  Report only the
    # selected moderators where a multi-valued study actually entered the fit,
    # with the number of contributing studies.
    substitutions = []
    for name in moderators:
        keys = multi_valued.get(name)
        if not keys:
            continue
        n = len({rec["study_key"] for rec in kept if rec["study_key"] in keys})
        if n:
            substitutions.append({
                "moderator": name,
                "n_studies": n,
                "note": "multi-valued studies assigned their first value "
                        "(primary/first label)",
            })

    terms, cols = ["Intercept"], [[1.0] * len(kept)]
    base_cols = []  # (moderator, term, vector)
    ref_of, cat_names = {}, []
    for name in moderators:
        vals = [_norm(r.get(name)) for r in kept]
        kind = kind_of.get(name, "categorical")
        if kind == "continuous":
            xs = [float(r.get(name)) for r in kept]
            mu = sum(xs) / len(xs)
            vec = [x - mu for x in xs]
            base_cols.append((name, name, vec))
        else:
            counts = collections.Counter(v for v in vals if v != "")
            if len(counts) < 2:
                continue
            ranked = [lv for lv, _ in counts.most_common()]
            ref = ranked[0]
            ref_of[name] = ref
            cat_names.append(name)
            shown = ranked[1:]
            cap = _level_cap(name, len(ranked))
            if cap is not None:
                shown = shown[:cap]
            shown_set = set(shown)
            for lv in shown:
                vec = [1.0 if v == lv else 0.0 for v in vals]
                base_cols.append((name, f"{name} = {_mod_label(name, lv)}", vec))
            rest = [lv for lv in ranked[1:] if lv not in shown_set]
            if rest:
                rest_set = set(rest)
                vec = [1.0 if v in rest_set else 0.0 for v in vals]
                base_cols.append((name, f"{name} = Other ({len(rest)} levels)", vec))
    for name, term, vec in base_cols:
        terms.append(term); cols.append(vec)
    k_by_term = {t: int(sum(vec)) for _, t, vec in base_cols}
    if cat_names:
        k_by_term["Intercept"] = sum(
            1 for r in kept if all(_norm(r.get(n)) == ref_of[n] for n in cat_names))
    else:
        k_by_term["Intercept"] = len(kept)
    for a, b in interactions:
        acols = [c for c in base_cols if c[0] == a]
        bcols = [c for c in base_cols if c[0] == b]
        for _, ta, va in acols:
            for _, tb, vb in bcols:
                term = f"{ta} × {tb}"
                vec = [x * y for x, y in zip(va, vb)]
                terms.append(term)
                cols.append(vec)
                k_by_term[term] = int(sum(vec))

    # Drop columns that make the design singular (e.g. an interaction that is
    # collinear with a main effect because a level occurs in one group only).
    keep, dropped_terms = [0], []
    for j in range(1, len(cols)):
        cand = keep + [j]
        M = [[sum(cols[a][i] * cols[b][i] for i in range(len(cols[a]))) for b in cand]
             for a in cand]
        if _mat_inv(M) is not None:
            keep.append(j)
    dropped_terms = [terms[j] for j in range(len(terms)) if j not in keep]
    cols = [cols[j] for j in keep]
    terms = [terms[j] for j in keep]

    X = [[cols[c][i] for c in range(len(cols))] for i in range(len(kept))]
    y = [r[ycol] for r in kept]
    v = [r[vcol] for r in kept]
    groups = [r["study_key"] for r in kept]

    # Two-level model: effects are nested in studies.  Stage 1 combines the
    # effects within each study (fixed effect); stage 2 runs the moderator
    # meta-regression across studies.  Heterogeneity is reported at both levels.
    comp = _two_level_components(y, v, groups)
    if comp["k"] < max(3, len(terms) + 2):
        return jsonify({"ok": False, "error": f"too few studies ({comp['k']}) for a "
                        f"{len(terms)}-term two-level model", "k": len(kept),
                        "n_studies": comp["k"], "figures": mod_figures,
                        "outcome": outcome}), 200
    st_y, st_v, st_X, st_ids = _study_level(y, X, v, groups)

    # R² among τ: proportional reduction in the method-of-moments residual
    # between-study variance vs the intercept-only model (same weights).
    _w_re = [1.0 / (vi + comp["tau2_between"]) for vi in st_v]
    _fit_re = _wls_cluster(st_y, st_X, _w_re, st_ids)
    r2_tau = None
    if _fit_re:
        _sw = sum(_w_re)
        _c = _sw - sum(x * x for x in _w_re) / _sw if _sw > 0 else 0.0
        if _c > 0:
            _q_res = sum(_w_re[i] * _fit_re["resid"][i] ** 2 for i in range(len(_w_re)))
            _tau2_res = max(0.0, (_q_res - (len(_w_re) - len(terms))) / _c)
            _ybar = sum(wi * yi for wi, yi in zip(_w_re, st_y)) / _sw
            _q_nul = sum(wi * (yi - _ybar) ** 2 for wi, yi in zip(_w_re, st_y))
            _tau2_nul = max(0.0, (_q_nul - (len(_w_re) - 1)) / _c)
            if _tau2_nul > 0:
                r2_tau = 1.0 - _tau2_res / _tau2_nul

    if model == "bayes":
        mu_sd, tau_prior, tau_sd = _resolve_bayes_priors(payload)
        bfit = _bayes_meta(st_y, st_X, st_v, mu_sd=mu_sd, tau_sd=tau_sd, tau_prior=tau_prior)
        if bfit is None:
            return jsonify({"ok": False, "error": "Bayesian fit failed (singular design?)",
                            "figures": mod_figures}), 200
        tw = _bayes_tau_within(y, v, groups, tau_sd=tau_sd, tau_prior=tau_prior)
        beta_mean = [t["estimate"] for t in bfit["terms"]]
        w2 = [1.0 / (vi + max(bfit["tau2"], 0.0)) for vi in st_v]
        sw = sum(w2)
        ybar = sum(wi * yi for wi, yi in zip(w2, st_y)) / sw if sw else 0.0
        rss = sum(w2[i] * (st_y[i] - sum(st_X[i][a] * beta_mean[a] for a in range(len(terms)))) ** 2
                  for i in range(len(st_y)))
        tss = sum(wi * (yi - ybar) ** 2 for wi, yi in zip(w2, st_y))
        r2 = 1.0 - rss / tss if tss > 0 else float("nan")
        coefs = []
        for a, term in enumerate(terms):
            t = bfit["terms"][a]
            coefs.append({"term": term, "k": k_by_term.get(term),
                          "estimate": t["estimate"], "se": t["se"],
                          "lo": t["lo"], "hi": t["hi"],
                          "z": (t["estimate"] / t["se"] if t["se"] > 0 else float("nan")),
                          "p": t["p"], "prob_gt0": t["prob_gt0"], "bf10": t["bf10"],
                          "kind": "intercept" if a == 0 else ("interaction" if " × " in term else "moderator")})
        return jsonify({"ok": True, "model": "bayes", "two_level": True,
                        "k": len(kept), "n_studies": comp["n_studies"], "dropped": dropped,
                        "r2": r2, "r2_tau": r2_tau,
                        "tau_between": bfit["tau"], "tau_within": (tw["tau"] if tw else None),
                        "tau_between_lo": bfit["tau_lo"], "tau_between_hi": bfit["tau_hi"],
                        "tau_within_lo": (tw["lo"] if tw else None),
                        "tau_within_hi": (tw["hi"] if tw else None),
                        "tau2": bfit["tau2"],
                        "i2_between": comp["i2_between"], "i2_within": comp["i2_within"],
                        "terms": coefs, "dropped_terms": dropped_terms,
                        "levels": level_effects, "figures": mod_figures,
                        "substitutions": substitutions,
                        "prior": {"mu_sd": mu_sd, "tau_sd": tau_sd, "tau_prior": tau_prior},
                        "layer": layer, "spec": spec, "exclusions": exclusions,
                        "scope": scope, "moderators": moderators, "interactions": interactions,
                        "outcome": outcome})
    # Frequentist two-stage fit: study-level meta-regression with the
    # between-study variance in the weights and CR1 SEs clustered by study.
    w2 = [1.0 / (vi + comp["tau2_between"]) for vi in st_v]
    fit = _wls_cluster(st_y, st_X, w2, st_ids)
    if fit is None:
        return jsonify({"ok": False, "error": "design matrix is singular; drop a moderator",
                        "figures": mod_figures}), 200
    beta, se = fit["beta"], fit["se"]
    # Cluster-robust inference uses a t(G-1) reference (G = number of clusters),
    # not the normal (D2/H4): few-cluster models are not anti-conservative.  There
    # is no normal-approximation fallback — if scipy is unavailable the app fails
    # loud instead of silently reporting a narrower interval.
    gdf = max(1, int(fit.get("n_studies") or 1) - 1)
    tcrit = _t_ppf(0.975, gdf)
    coefs = []
    for a, term in enumerate(terms):
        tval = beta[a] / se[a] if se[a] > 0 else float("nan")
        sf = _t_sf(abs(tval), gdf) if math.isfinite(tval) else None
        pval = (2.0 * sf if sf is not None else float("nan"))
        lo = beta[a] - tcrit * se[a] if tcrit is not None else float("nan")
        hi = beta[a] + tcrit * se[a] if tcrit is not None else float("nan")
        coefs.append({"term": term, "k": k_by_term.get(term),
                      "estimate": beta[a], "se": se[a],
                      "lo": lo, "hi": hi,
                      "z": tval, "p": pval, "df": gdf,
                      "kind": "intercept" if a == 0 else ("interaction" if " × " in term else "moderator")})
    # QM: Wald test on the non-intercept terms using the cluster-robust
    # covariance (matching the reported robust SEs, not the model-based one).
    qm = qm_df = qm_p = None
    if len(terms) > 1:
        sub = list(range(1, len(terms)))
        V = [[fit["cov"][a][b] for b in sub] for a in sub]
        Vi = _mat_inv(V)
        if Vi is not None:
            b = [beta[a] for a in sub]
            qm = sum(b[i] * Vi[i][j] * b[j] for i in range(len(b)) for j in range(len(b)))
            qm_df = len(b)
            qm_p = _chi2_sf(qm, qm_df)
    sw = sum(w2)
    ybar = sum(wi * yi for wi, yi in zip(w2, st_y)) / sw
    rss = sum(wi * e * e for wi, e in zip(w2, fit["resid"]))
    tss = sum(wi * (yi - ybar) ** 2 for wi, yi in zip(w2, st_y))
    r2 = 1.0 - rss / tss if tss > 0 else float("nan")
    return jsonify({"ok": True, "model": "freq", "two_level": True,
                    "k": len(kept), "n_studies": comp["n_studies"], "dropped": dropped,
                    "tau2": comp["tau2_between"] + comp["tau2_within"],
                    "tau_between": math.sqrt(max(comp["tau2_between"], 0.0)),
                    "tau_within": math.sqrt(max(comp["tau2_within"], 0.0)),
                    "tau2_between": comp["tau2_between"], "tau2_within": comp["tau2_within"],
                    "i2_between": comp["i2_between"], "i2_within": comp["i2_within"],
                    "r2": r2, "r2_tau": r2_tau, "qm": qm, "qm_df": qm_df, "qm_p": qm_p,
                    "terms": coefs, "dropped_terms": dropped_terms,
                    "levels": level_effects, "figures": mod_figures,
                    "substitutions": substitutions,
                    "layer": layer, "spec": spec, "exclusions": exclusions,
                    "scope": scope, "moderators": moderators, "interactions": interactions,
                    "outcome": outcome})

@bp.get("/api/analysis/subgroup")
def api_analysis_subgroup():
    """Pooled effects per level of one moderator (used by the Summary tab)."""
    by = (request.args.get("by") or "Age_Group").strip()
    layer = (request.args.get("layer") or "meta").strip()
    spec = (request.args.get("spec") or "").strip()
    exclusions = (request.args.get("exclusions") or "on").strip().lower()
    scope = (request.args.get("scope") or "unrestricted").strip()
    model = (request.args.get("model") or "freq").strip().lower()
    cat = _moderator_catalog()
    if by not in cat["kinds"]:
        return jsonify({"by": by, "rows": [], "error": "unknown moderator"})
    rows = _spec_rows_for(layer, spec, exclusions)
    if scope == "restricted":
        rows = [r for r in rows if r["dv_domain"] in SUSCEPTIBILITY]
    rows = _apply_study_filter(rows, *_filter_args(request))
    groups = {}
    if by in _CONTRAST_NAMES:
        for r in rows:
            v = _norm(r.get(by))
            if v:
                groups.setdefault(v, []).append(r)
    else:
        # per-intervention effective moderators, matching the level tables
        for r in rows:
            for v in _values(_mods_for_row(r).get(by)):
                if v in (None, ""):
                    continue
                groups.setdefault(_norm(v), []).append(r)
    out = []
    for v, gg in groups.items():
        y = [r["effect"] for r in gg]
        vv = [r["var"] for r in gg]
        if len(y) < 2:
            continue
        if model == "bayes":
            res = _bayes_pool(y, vv)
            if not res:
                continue
            out.append({"level": v, "label": _mod_label(by, v), "k": res["k"],
                        "n_studies": len({r["study_key"] for r in gg}),
                        "est": res["est"], "lo": res["lo"], "hi": res["hi"],
                        "tau": res["tau"], "p_gt0": res["p_gt0"]})
        else:
            res = _dl_pool(y, vv)
            out.append({"level": v, "label": _mod_label(by, v), "k": res["k"],
                        "n_studies": len({r["study_key"] for r in gg}),
                        "est": res["est"], "lo": res["lo"], "hi": res["hi"], "i2": res["i2"]})
    out.sort(key=lambda x: -x["k"])
    label = next((m["label"] for m in cat["moderators"] if m["name"] == by), by)
    return jsonify({"by": by, "label": label, "layer": layer, "spec": spec,
                    "scope": scope, "model": model, "rows": out})

@bp.get("/api/analysis/export")
def api_analysis_export():
    fmt = (request.args.get("fmt") or "xlsx").lower()
    layer = (request.args.get("layer") or "meta").strip()
    spec = (request.args.get("spec") or "post_all").strip()
    exclusions = (request.args.get("exclusions") or "on").strip().lower()
    scope = (request.args.get("scope") or "unrestricted").strip()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        if fmt == "pptx":
            path = _export_pptx(layer, spec, exclusions, scope, stamp)
            mime = ("application/vnd.openxmlformats-officedocument."
                    "presentationml.presentation")
        else:
            path = _export_xlsx(layer, spec, exclusions, scope, stamp)
            mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return send_file(path, mimetype=mime, as_attachment=True,
                     download_name=os.path.basename(path))

@bp.get("/api/analysis/overview")
def api_analysis_overview():
    _prewarm_analysis()
    temp, xlsx, _figdir, scripts = _analysis_files()
    tables = []
    for key, fname, label in ANALYSIS_TABLES:
        p = os.path.join(temp, fname)
        if os.path.exists(p):
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(p)).isoformat(timespec="seconds")
            except OSError:
                mtime = None
            tables.append({"key": key, "label": label, "rows": _csv_rowcount(p),
                           "modified": mtime})
    if tables:
        tables.append({"key": "pooled_bayes", "label": "Pooled (Bayesian)",
                       "rows": 24, "modified": None, "synthetic": True})
    figs = _figure_index()
    note = os.path.join(S.ROOT, "Scripts", "ANALYSIS_SUITE_NOTE.md")
    spec_opts = _spec_options(temp)
    return jsonify({
        "root": S.ROOT,
        "available": bool(tables),
        "xlsx": os.path.exists(xlsx),
        "xlsx_name": os.path.basename(xlsx),
        "tables": tables,
        "n_figures": len({f["stem"] for f in figs}),
        "figures": figs,
        "specs": spec_opts + _derived_spec_options(spec_opts),
        "run": {k: v for k, v in _analysis_run.items() if k != "log"},
        "scripts_available": all(os.path.exists(os.path.join(scripts, s))
                                 for s in ANALYSIS_RUN_KINDS["all"]),
        "note_available": os.path.exists(note),
        "frozen": FROZEN,
        "specs_stale": _specs_stale_flag(),
        "stale": _tables_stale_flag(),
        "stale_token": _inputs_fingerprint(),
    })

@bp.get("/api/analysis/table")
def api_analysis_table():
    name = (request.args.get("name") or "publication_bias").strip()
    if name == "robustness":
        layer = (request.args.get("layer") or "meta").strip()
        spec = (request.args.get("spec") or "").strip()
        excl = (request.args.get("exclusions") or "on").strip().lower()
        cols, rows = _robustness_combined(layer, spec, excl)
        return jsonify({"name": name, "label": "Robustness & bias", "columns": cols,
                        "rows": rows, "total": len(rows), "truncated": False})
    if name == "loo_summary":
        layer = (request.args.get("layer") or "meta").strip()
        spec = (request.args.get("spec") or "").strip()
        excl = (request.args.get("exclusions") or "on").strip().lower()
        cols, rows = _loo_summary(layer, spec, excl)
        return jsonify({"name": name, "label": "Leave-one-out summary", "columns": cols,
                        "rows": rows, "total": len(rows), "truncated": False})
    if name == "pooled_bayes":
        layer = (request.args.get("layer") or "meta").strip()
        spec = (request.args.get("spec") or "").strip()
        excl = (request.args.get("exclusions") or "on").strip().lower()
        res = _bayes_for(layer, spec, excl)
        cols = ["layer", "spec", "exclusions", "k", "n_studies", "bayes_est", "bayes_lo",
                "bayes_hi", "bayes_tau", "bayes_p_gt0"]
        rows = []
        if res:
            rows.append({"layer": layer, "spec": spec, "exclusions": excl, "k": res["k"],
                         "n_studies": res["n_studies"], "bayes_est": res["est"],
                         "bayes_lo": res["lo"], "bayes_hi": res["hi"], "bayes_tau": res["tau"],
                         "bayes_p_gt0": res["p_gt0"]})
        return jsonify({"name": name, "label": "Pooled (Bayesian)", "columns": cols,
                        "rows": rows, "total": len(rows), "truncated": False})
    entry = next((t for t in ANALYSIS_TABLES if t[0] == name), None)
    if not entry:
        abort(404)
    path = os.path.join(S.ROOT, "temp", entry[1])
    if name == "condition_audit":
        # Only the conditions whose arm role could not be resolved are of
        # interest; the resolved ones are the common case and just add noise.
        if not os.path.exists(path):
            return jsonify({"name": name, "label": entry[2], "columns": [], "rows": [],
                            "total": 0, "truncated": False})
        cols, rows = _read_table(path, limit=None)
        rows = [r for r in rows
                if str(r.get("ambiguous", "")).strip().lower() in ("1", "1.0", "true", "yes")]
        role_lab = {"control": "Control", "active_control": "Active control",
                    "intervention": "Intervention", "unknown": "Unknown",
                    "missing": "Missing (no condition)", "other": "Other"}
        for r in rows:
            if "condition" in r:
                r["condition"] = _human_mod_label(r["condition"])
            if "role" in r:
                r["role"] = role_lab.get(str(r["role"]).strip().lower(), r["role"])
        return jsonify({"name": name, "label": entry[2], "columns": cols, "rows": rows,
                        "total": len(rows), "truncated": False,
                        "note": "Only conditions whose arm role could not be resolved."})
    if name == "specs_excluded":
        # Quality guard: if there is a single exclusion criterion, state it once
        # as a note and drop the constant reason/sensitivity columns (and the
        # r-sensitivity duplicates) so the table stays short.
        if not os.path.exists(path):
            return jsonify({"name": name, "label": entry[2], "columns": [], "rows": [],
                            "total": 0, "truncated": False})
        cols, rows = _read_table(path, limit=None)
        reasons = sorted({str(r.get("reason") or "").strip() for r in rows
                          if str(r.get("reason") or "").strip()})
        if len(reasons) <= 1:
            drop = {"reason", "r_used", "source", "n1", "n2"}
            keep = [c for c in cols if c not in drop]
            seen, dedup = set(), []
            for r in rows:
                key = tuple(r.get(c) for c in keep)
                if key in seen:
                    continue
                seen.add(key)
                dedup.append(r)
            return jsonify({"name": name, "label": entry[2], "columns": keep, "rows": dedup,
                            "total": len(dedup), "truncated": False,
                            "note": (reasons[0] if reasons else "") +
                                    " (the only quality-guard criterion applied)."})
        return jsonify({"name": name, "label": entry[2], "columns": cols, "rows": rows,
                        "total": len(rows), "truncated": False})
    if not os.path.exists(path):
        return jsonify({"name": name, "columns": [], "rows": [], "total": 0, "truncated": False})
    try:
        limit = max(1, min(5000, int(request.args.get("limit", 2000))))
    except (TypeError, ValueError):
        limit = 2000
    cols, rows = _read_table(path, limit=None)
    layer = (request.args.get("layer") or "").strip()
    spec = (request.args.get("spec") or "").strip()
    excl = (request.args.get("exclusions") or "").strip()

    def keep(r):
        if layer and "layer" in r and r.get("layer") != layer:
            return False
        if spec and "spec" in r and r.get("spec") != spec:
            return False
        if excl and "exclusions" in r and r.get("exclusions") not in ("", None) \
                and r.get("exclusions") != excl:
            return False
        return True
    filtered = [r for r in rows if keep(r)] if (layer or spec or excl) else rows
    if name == "pooled" and layer and spec in DERIVED_SPECS:
        row = _derived_pooled_row(cols, layer, spec, excl or "on")
        if row:
            return jsonify({"name": name, "label": entry[2], "columns": cols,
                            "rows": [row], "total": 1, "truncated": False})
    total = len(filtered)
    return jsonify({"name": name, "label": entry[2], "columns": cols,
                    "rows": filtered[:limit], "total": total, "truncated": total > limit})

@bp.get("/api/analysis/figure/<path:name>")
def api_analysis_figure(name):
    base = os.path.basename(name)
    if os.path.splitext(base)[1].lower() not in (".png", ".pdf"):
        abort(404)
    path = os.path.join(_figures_dir(), base)
    if not os.path.isfile(path):
        abort(404)
    resp = send_file(path, conditional=True)
    resp.headers["Cache-Control"] = "no-store"
    return resp

@bp.get("/api/analysis/note")
def api_analysis_note():
    """The whole analysis-suite note, split into sections with freshness.

    The note has several writers (meta / publication-bias / SDT) and older blocks
    can silently contradict the live tables, so each section is returned with its
    ``_Generated`` timestamp and a stale flag compared against the newest
    prerequisite file.
    """
    path = os.path.join(S.ROOT, "Scripts", "ANALYSIS_SUITE_NOTE.md")
    if not os.path.exists(path):
        return jsonify({"available": False, "markdown": "", "sections": []})
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    # newest prerequisite (tables/specs/results) so we can flag stale note blocks
    newest = 0.0
    for pat in (os.path.join(S.TEMP, "pb_*.csv"), os.path.join(S.TEMP, "meta_specs.csv"),
                os.path.join(S.TEMP, "sdt_specs*.csv"),
                os.path.join(S.ROOT, "Results", "Analysis_Suite_Results.xlsx")):
        for f in glob.glob(pat):
            try:
                newest = max(newest, os.path.getmtime(f))
            except OSError:
                pass
    tables_at = (datetime.fromtimestamp(newest).isoformat(timespec="minutes")
                 if newest else "")

    gen_re = re.compile(r"_Generated\s+(\d{4}-\d\d-\d\d[ T]\d\d:\d\d)([^_]*)_")
    lines = text.splitlines()
    sections = []
    cur = {"title": "Analysis suite note", "level": 1, "lines": []}
    for ln in lines:
        m = re.match(r"^(#{1,3})\s+(.*)$", ln)
        if m:
            if cur["lines"] or cur["title"] != "Analysis suite note":
                sections.append(cur)
            cur = {"title": m.group(2).strip(), "level": len(m.group(1)), "lines": []}
        else:
            cur["lines"].append(ln)
    sections.append(cur)
    out = []
    for s in sections:
        body = "\n".join(s["lines"]).strip()
        m = gen_re.search(body)
        generated = m.group(1).replace("T", " ") if m else ""
        writer = (m.group(2).strip() if m and m.group(2) else "")
        stale = False
        if generated and newest:
            try:
                gts = datetime.strptime(generated, "%Y-%m-%d %H:%M").timestamp()
                stale = gts < newest - 60
            except ValueError:
                stale = False
        if not body and not generated:
            continue
        out.append({"title": s["title"], "level": s["level"], "markdown": body,
                    "generated": generated, "writer": writer, "stale": stale})
    return jsonify({"available": True, "markdown": text.strip(), "tables_at": tables_at,
                    "sections": out,
                    "stale_sections": sum(1 for s in out if s["stale"])})

@bp.post("/api/analysis/run")
def api_analysis_run():
    payload = request.get_json(force=True, silent=True) or {}
    kind = (payload.get("kind") or "essential").strip()
    force = bool(payload.get("force"))
    if kind not in ANALYSIS_RUN_KINDS:
        return jsonify({"ok": False, "error": "unknown run kind"}), 400
    status = start_background_run(kind, force, payload.get("fully_within"))
    if status == "running":
        return jsonify({"ok": False, "error": "a run is already in progress"}), 409
    if status == "cached":
        return jsonify({"ok": True, "cached": True, "kind": kind})
    return jsonify({"ok": True, "running": True, "kind": kind})

@bp.get("/api/analysis/run/status")
def api_analysis_run_status():
    return jsonify(_analysis_run)
