"""The single home of every estimator used by the pipeline **and** the app.

Moved verbatim out of ``suite_common`` / ``statslib`` / ``metaanalysis`` so no
formula is implemented twice.  ``suite_common`` re-exports every name for
back-compat (dict-taking wrappers where the signature differs).

Decision fixes baked in here (see ``Results/PLAN_codingstudio_harmonizer_reproduction_2026-09-22.md``):
* **D2** — one WLS/CRVE fitter (``wls``); when clusters are supplied the reported
  inference is ``t(G-1)`` with an exchangeable working correlation and the
  model-based ``t(k-2)`` is kept as a ``*_model`` sensitivity column.
* **D3** — ``cluster_robust`` uses the full CR1 small-sample factor
  ``(G/(G-1))*((n-1)/(n-p))`` (p = 1 for the intercept-only pool, so numerically
  identical to the old ``G/(G-1)``; the dead ``(k-1)/(k-1)`` is gone).
* **D4** — ``probit``/``dprime`` support the Hautus (1995) log-linear correction;
  call sites pass the number of trials/items ``n``.
* **D5** — the Bayesian estimators formerly only in the app's ``statslib`` live
  here so ``run_analysis`` can reproduce every app number.
"""
from __future__ import annotations

import math

import numpy as np

try:
    from scipy import stats as _st
except Exception:  # pragma: no cover - scipy present in this environment
    _st = None

import contract as CT  # stdlib-only canonical contracts

polarity_sign = CT.polarity_sign


def num(x):
    try:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return np.nan
        return float(x)
    except (TypeError, ValueError):
        return np.nan


# ---------------------------------------------------------------------------
# probit / d'  (D4: pass n for the Hautus log-linear correction)
# ---------------------------------------------------------------------------
_PROBIT_EPS = 1e-4


def probit(x, n=None):
    """Inverse standard-normal CDF (NaN passes through).

    ``n`` is the number of trials behind a rate: when given, the Hautus (1995)
    log-linear correction ``(x*n + 0.5)/(n + 1)`` is applied before the
    transform, so hit/false-alarm rates of exactly 0 or 1 are not mapped to a
    fixed z regardless of sample size.  Without ``n`` the rate is clipped to
    ``[_PROBIT_EPS, 1-_PROBIT_EPS]``.
    """
    arr = np.asarray(x, float)
    if n is not None:
        nn = np.asarray(n, float)
        arr = (arr * nn + 0.5) / (nn + 1.0)
    arr = np.clip(arr, _PROBIT_EPS, 1.0 - _PROBIT_EPS)
    if _st is None:
        return np.full(arr.shape, np.nan)
    return _st.norm.ppf(arr)


def dprime(hits, false_alarms, n=None):
    """SDT discrimination ``d' = z(HR) - z(FAR)`` (element-wise)."""
    return probit(hits, n=n) - probit(false_alarms, n=n)


# back-compat aliases
_probit = probit
_dprime = dprime


# ---------------------------------------------------------------------------
# one-level pooling
# ---------------------------------------------------------------------------
def dl_random(yi, vi):
    """DerSimonian-Laird random-effects pooled estimate.

    Contract: non-finite / ``vi <= 0`` rows are dropped; ``k == 0`` returns
    NaN estimands; ``k == 1`` yields ``tau2 = 0`` (the single effect).
    """
    yi, vi = np.asarray(yi, float), np.asarray(vi, float)
    m = np.isfinite(yi) & np.isfinite(vi) & (vi > 0)
    yi, vi = yi[m], vi[m]
    k = len(yi)
    if k == 0:
        return dict(k=0, est=np.nan, se=np.nan, lo=np.nan, hi=np.nan,
                    tau2=np.nan, i2=np.nan, q=np.nan)
    w = 1 / vi
    fe = np.sum(w * yi) / np.sum(w)
    q = np.sum(w * (yi - fe) ** 2)
    c = np.sum(w) - np.sum(w ** 2) / np.sum(w)
    tau2 = max(0.0, (q - (k - 1)) / c) if c > 0 else 0.0
    ws = 1 / (vi + tau2)
    est = np.sum(ws * yi) / np.sum(ws)
    se = math.sqrt(1 / np.sum(ws))
    i2 = max(0.0, (q - (k - 1)) / q) * 100 if q > 0 else 0.0
    return dict(k=k, est=est, se=se, lo=est - 1.96 * se, hi=est + 1.96 * se,
                tau2=tau2, i2=i2, q=q)


def fe_pool(yi, vi):
    """Fixed-effect (inverse-variance) pooled estimate."""
    yi, vi = np.asarray(yi, float), np.asarray(vi, float)
    m = np.isfinite(yi) & np.isfinite(vi) & (vi > 0)
    yi, vi = yi[m], vi[m]
    k = len(yi)
    if k == 0:
        return dict(k=0, est=np.nan, se=np.nan, lo=np.nan, hi=np.nan)
    w = 1 / vi
    est = np.sum(w * yi) / np.sum(w)
    se = math.sqrt(1 / np.sum(w))
    return dict(k=k, est=est, se=se, lo=est - 1.96 * se, hi=est + 1.96 * se)


# ---------------------------------------------------------------------------
# WLS / CRVE meta-regression (D2: one fitter, t(G-1) CRVE when clustered)
# ---------------------------------------------------------------------------
def wls(y, x, w, groups=None, rho=0.0):
    """Weighted least squares ``y = a + b*x`` with fixed (known) variances.

    Without ``groups`` the inference is model-based: coefficient covariance
    ``sigma2 * (X'WX)^-1`` with ``sigma2`` the residual variance, t on ``n-2``
    df.  With ``groups`` the inference is cluster-robust (CR1) using an
    exchangeable working correlation ``rho`` within each cluster and t on
    ``G-1`` df; the model-based result is kept as ``*_model`` sensitivity keys.

    Returned keys (back-compat): ``k, a, se_a, t_a, p_a, b, se_b, t_b, p_b, tau2``.
    """
    y = np.asarray(y, float)
    x = np.asarray(x, float)
    w = np.asarray(w, float)
    if groups is None or len(np.asarray(groups)) != len(y):
        keep = np.isfinite(y) & np.isfinite(x) & np.isfinite(w) & (w > 0)
        y, x, w, g = y[keep], x[keep], w[keep], None
    else:
        g = np.asarray([str(v) for v in groups])
        keep = np.isfinite(y) & np.isfinite(x) & np.isfinite(w) & (w > 0)
        y, x, w, g = y[keep], x[keep], w[keep], g[keep]
    k = len(y)
    nan = dict(k=k, a=np.nan, se_a=np.nan, t_a=np.nan, p_a=np.nan, b=np.nan,
               se_b=np.nan, t_b=np.nan, p_b=np.nan, tau2=np.nan,
               se_b_model=np.nan, t_b_model=np.nan, p_b_model=np.nan, df=np.nan)
    if k < 3:
        return nan
    X = np.column_stack([np.ones(k), x])
    XtWX = X.T @ (X * w[:, None])
    try:
        XtWX_inv = np.linalg.inv(XtWX)
    except np.linalg.LinAlgError:
        return nan
    beta = XtWX_inv @ (X.T @ (w * y))
    resid = y - X @ beta
    dof_model = k - 2
    sigma2 = float(resid @ (w * resid)) / dof_model if dof_model > 0 else np.nan
    se_a_model = math.sqrt(max(sigma2 * XtWX_inv[0, 0], 0)) if np.isfinite(sigma2) else np.nan
    se_b_model = math.sqrt(max(sigma2 * XtWX_inv[1, 1], 0)) if np.isfinite(sigma2) else np.nan
    t_a_model = beta[0] / se_a_model if se_a_model and np.isfinite(se_a_model) else np.nan
    t_b_model = beta[1] / se_b_model if se_b_model and np.isfinite(se_b_model) else np.nan
    out = dict(k=k, a=beta[0], b=beta[1], tau2=dl_random(y, 1 / w)["tau2"],
               se_a=se_a_model, t_a=t_a_model,
               p_a=(2 * _st.t.sf(abs(t_a_model), dof_model)
                    if (_st is not None and np.isfinite(t_a_model)) else np.nan),
               se_b=se_b_model, t_b=t_b_model,
               p_b=(2 * _st.t.sf(abs(t_b_model), dof_model)
                    if (_st is not None and np.isfinite(t_b_model)) else np.nan),
               se_b_model=se_b_model, t_b_model=t_b_model,
               p_b_model=(2 * _st.t.sf(abs(t_b_model), dof_model)
                          if (_st is not None and np.isfinite(t_b_model)) else np.nan),
               df=float(dof_model))
    if g is None:
        return out
    # cluster-robust (CR1) sandwich with an exchangeable working correlation
    clusters = list(dict.fromkeys(g))
    G = len(clusters)
    if G < 2:
        return out
    XtWX_c = X.T @ (X * w[:, None])
    try:
        bread = np.linalg.inv(XtWX_c)
    except np.linalg.LinAlgError:
        return out
    meat = np.zeros((2, 2))
    for c in clusters:
        idx = g == c
        xc = X[idx]
        wc = w[idx]
        ec = resid[idx]
        if rho and len(ec) > 1:
            # exchangeable working correlation within the cluster (weighted score)
            v = 1.0 / wc
            a_, b_ = _exchangeable_inverse(v, rho)
            s = np.sqrt(v)
            invs = 1.0 / s
            Dg = a_ * np.sum(invs ** 2) + b_ * (np.sum(invs)) ** 2
            u = (a_ * np.sum(ec / v) + b_ * np.sum(ec / s) * np.sum(invs)) / Dg
            score = u * (xc.T @ wc)  # working-GLS score direction
        else:
            score = xc.T @ (wc * ec)
        meat += np.outer(score, score)
    cov = bread @ meat @ bread
    f = (G / (G - 1.0)) * ((k - 1.0) / (k - 2.0)) if k > 2 else (G / (G - 1.0))
    cov = cov * f
    se_a = math.sqrt(max(cov[0, 0], 0.0))
    se_b = math.sqrt(max(cov[1, 1], 0.0))
    df = G - 1
    tc = float(_st.t.ppf(0.975, df)) if _st is not None else 1.96
    out.update({
        "se_a": se_a, "t_a": beta[0] / se_a if se_a > 0 else np.nan,
        "p_a": (2 * _st.t.sf(abs(beta[0] / se_a), df)
                if (_st is not None and se_a > 0) else np.nan),
        "se_b": se_b, "t_b": beta[1] / se_b if se_b > 0 else np.nan,
        "p_b": (2 * _st.t.sf(abs(beta[1] / se_b), df)
                if (_st is not None and se_b > 0) else np.nan),
        "b_lo": beta[1] - tc * se_b, "b_hi": beta[1] + tc * se_b,
        "n_clusters": G, "df_crve": df,
    })
    return out


# back-compat aliases
_wls = wls


def moderator_contrast(yi, vi, study, x):
    """Single-moderator meta-regression contrast (WLS of effect on ``x``)."""
    y = np.asarray(yi, float)
    v = np.asarray(vi, float)
    xx = np.asarray(x, float)
    m = np.isfinite(y) & np.isfinite(v) & (v > 0) & np.isfinite(xx)
    if m.sum() < 3:
        return None
    r = wls(y[m], xx[m], 1.0 / v[m])
    return {"moderator": "moderator", "k": r["k"], "est": r["b"], "se": r["se_b"],
            "lo": r["b"] - 1.96 * r["se_b"], "hi": r["b"] + 1.96 * r["se_b"],
            "t": r["t_b"], "p": r["p_b"]}


# ---------------------------------------------------------------------------
# publication-bias battery
# ---------------------------------------------------------------------------
def egger_pet(yi, vi):
    """Egger's regression test and the PET estimate (WLS of effect on SE)."""
    yi, vi = np.asarray(yi, float), np.asarray(vi, float)
    se = np.sqrt(np.maximum(vi, 0.0))
    r = _wls(yi, se, 1.0 / vi)
    return dict(k=r["k"], intercept=r["a"], se=r["se_a"], t=r["t_a"], p=r["p_a"],
                slope=r["b"], slope_se=r["b"], slope_se_est=r["se_b"], slope_t=r["t_b"],
                slope_p=r["p_b"], p_slope=r["p_b"], se_label="SE")


def peese(yi, vi):
    """PEESE estimate: WLS of effect on the variance (weight 1/vi)."""
    yi, vi = np.asarray(yi, float), np.asarray(vi, float)
    r = _wls(yi, vi, 1.0 / vi)
    return dict(k=r["k"], intercept=r["a"], se=r["se_a"], t=r["t_a"], p=r["p_a"],
                slope_var=r["b"], p_slope=r["p_b"])


def pet_peese(yi, vi):
    """PET-PEESE decision rule (Stanley & Doucouliagos)."""
    pet = egger_pet(yi, vi)
    pe = peese(yi, vi)
    prefer = "PEESE" if (np.isfinite(pet["p"]) and pet["p"] < 0.05) else "PET"
    chosen = pe if prefer == "PEESE" else pet
    return {"pet": pet, "peese": pe, "prefer": prefer,
            "estimate": chosen["intercept"], "se": chosen["se"],
            "lo": chosen["intercept"] - 1.96 * chosen["se"],
            "hi": chosen["intercept"] + 1.96 * chosen["se"]}


def _midranks(a):
    """Average ranks (1..n), ties shared."""
    a = np.asarray(a, float)
    order = np.argsort(a, kind="mergesort")
    ranks = np.empty(len(a), float)
    sa = a[order]
    i = 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and sa[j + 1] == sa[i]:
            j += 1
        ranks[order[i:j + 1]] = 0.5 * (i + j) + 1.0
        i = j + 1
    return ranks


def trim_and_fill(yi, vi, estimator="L0", side="auto"):
    """Duval & Tweedie trim-and-fill (random-effects, rank-based, L0)."""
    yi = np.asarray(yi, float)
    vi = np.asarray(vi, float)
    m = np.isfinite(yi) & np.isfinite(vi) & (vi > 0)
    yi, vi = yi[m], vi[m]
    n = len(yi)
    if n < 4:
        return {"k0": np.nan, "side": side, "adjusted": np.nan, "adjusted_se": np.nan,
                "adjusted_lo": np.nan, "adjusted_hi": np.nan, "n_imputed": 0,
                "observed": dl_random(yi, vi), "note": "too few studies"}

    def run(side_sign):
        order = np.argsort(yi, kind="mergesort")

        def est_on(idx):
            k = len(idx)
            if k < 3:
                return 0
            mu = dl_random(yi[idx], vi[idx])["est"]
            if not np.isfinite(mu):
                return 0
            x = yi[idx] - mu
            ranks = _midranks(np.abs(x))
            T = float(np.sum(ranks[x > 0])) if side_sign > 0 else float(np.sum(ranks[x < 0]))
            l0 = (4 * T - k * (k + 1)) / (2 * k - 1)
            return int(max(0, min(k - 1, round(l0))))

        k0 = est_on(np.arange(n))
        for _ in range(100):
            if k0 <= 0:
                break
            keep = order[:-k0] if side_sign > 0 else order[k0:]
            if len(keep) < 3:
                k0 = 0
                break
            k1 = est_on(keep)
            if k1 <= k0:
                break
            k0 = k1
        if k0 > 0:
            keep = order[:-k0] if side_sign > 0 else order[k0:]
        else:
            keep = np.arange(n)
        if len(keep) < 3:
            keep = np.arange(n)
            k0 = 0
        mu_trim = dl_random(yi[keep], vi[keep])["est"]
        trimmed = np.setdiff1d(np.arange(n), keep)
        total = int(len(trimmed))
        if total > 0:
            imp_y = 2 * mu_trim - yi[trimmed]
            imp_v = vi[trimmed]
            all_y = np.concatenate([yi, imp_y])
            all_v = np.concatenate([vi, imp_v])
        else:
            all_y, all_v = yi, vi
        adj = dl_random(all_y, all_v)
        return dict(k0=total, mu_trim=mu_trim, all_y=all_y, all_v=all_v, adj=adj)

    if side == "auto":
        cand = []
        for s in (+1, -1):
            r = run(s)
            cand.append((r["k0"], s, r))
        cand.sort(key=lambda t: (-t[0], t[1]))
        k0, s, r = cand[0]
        side_used = "left-suppressed(trim right)" if s > 0 else "right-suppressed(trim left)"
    else:
        s = +1 if side == "left" else -1
        r = run(s)
        k0 = r["k0"]
        side_used = side
    return {"k0": k0, "side": side_used, "n_imputed": k0,
            "adjusted": r["adj"]["est"], "adjusted_se": r["adj"]["se"],
            "adjusted_lo": r["adj"]["lo"], "adjusted_hi": r["adj"]["hi"],
            "observed": dl_random(yi, vi)}


def p_curve(pvals):
    """p-curve right-skew test (binomial) for the subset with p < .05."""
    p = np.asarray([x for x in np.asarray(pvals, float) if np.isfinite(x)], float)
    sig = p[p < 0.05]
    n = len(sig)
    klow = int(np.sum(sig < 0.025))
    if n == 0:
        return {"n_sig": 0, "n_below_025": 0, "prop_below_025": np.nan, "p_right_skew": np.nan}
    if _st is not None:
        pb = float(_st.binom.sf(klow - 1, n, 0.5))
    else:
        pb = np.nan
    return {"n_sig": n, "n_below_025": klow, "prop_below_025": klow / n, "p_right_skew": pb}


def p_uniform(yi, vi):
    """p-uniform effect estimate (van Assen, van Aert & Wicherts 2015)."""
    yi = np.asarray(yi, float)
    vi = np.asarray(vi, float)
    m = np.isfinite(yi) & np.isfinite(vi) & (vi > 0)
    yi, vi = yi[m], vi[m]
    if not len(yi) or _st is None:
        return {"delta": np.nan, "n": len(yi)}
    se = np.sqrt(vi)
    p = 2 * _st.norm.sf(np.abs(yi) / se)
    keep = p < 0.05
    y, s = yi[keep], se[keep]
    if len(y) < 3:
        return {"delta": np.nan, "n": len(y)}
    grid = np.linspace(min(-2.0, float(np.min(y)) - 2), max(2.0, float(np.max(y)) + 2), 801)
    best, best_ll = np.nan, -np.inf
    for d in grid:
        prob = (_st.norm.cdf((-1.96 * s - d) / s) + 1 - _st.norm.cdf((1.96 * s - d) / s))
        dens = (_st.norm.pdf((y - d) / s) / s)
        with np.errstate(divide="ignore", invalid="ignore"):
            ll = float(np.sum(np.log(dens) - np.log(np.maximum(prob, 1e-12))))
        if np.isfinite(ll) and ll > best_ll:
            best_ll, best = ll, d
    return {"delta": best, "n": len(y)}


# ---------------------------------------------------------------------------
# two-level / cluster-robust / RVE / three-level dependence-aware pooling
# ---------------------------------------------------------------------------
def two_level_re(yi, vi, study):
    """Two-level random-effects model (effects nested within studies).

    Two-stage implementation: within-study fixed-effect combination, then a
    DerSimonian-Laird combination across studies.
    """
    import pandas as _pd  # local import: stat_core stays importable without pandas use
    df = _pd.DataFrame({"y": yi, "v": vi, "s": np.asarray(study)}).dropna()
    df = df[np.isfinite(df["y"]) & np.isfinite(df["v"]) & (df["v"] > 0)]
    if not len(df):
        return dict(k=0, n_studies=0, est=np.nan, se=np.nan, lo=np.nan, hi=np.nan,
                    tau2_between=np.nan, i2=np.nan)
    st_est, st_var = [], []
    for _, g in df.groupby("s"):
        w = 1 / g["v"].to_numpy(float)
        y = g["y"].to_numpy(float)
        st_est.append(float(np.sum(w * y) / np.sum(w)))
        st_var.append(float(1 / np.sum(w)))
    r = dl_random(st_est, st_var)
    return dict(k=len(df), n_studies=len(st_est), est=r["est"], se=r["se"],
                lo=r["lo"], hi=r["hi"], tau2_between=r["tau2"], i2=r["i2"])


def cluster_robust(yi, vi, study, p=1):
    """Fixed-effect pooled estimate with study-clustered (CR1) sandwich SE.

    D3: full CR1 finite-sample factor ``(G/(G-1))*((n-1)/(n-p))`` (p = 1 for the
    intercept-only pool, so numerically identical to the previous ``G/(G-1)``;
    the dead ``(k-1)/(k-1)`` is gone and the general formula is now exercised).
    """
    import pandas as _pd
    df = _pd.DataFrame({"y": np.asarray(yi, float), "v": np.asarray(vi, float),
                        "s": np.asarray(study)}).dropna()
    df = df[np.isfinite(df["y"]) & np.isfinite(df["v"]) & (df["v"] > 0)]
    k = len(df)
    if k < 2:
        return dict(k=k, n_clusters=0, est=np.nan, se=np.nan, lo=np.nan, hi=np.nan)
    w = 1 / df["v"].to_numpy(float)
    y = df["y"].to_numpy(float)
    est = float(np.sum(w * y) / np.sum(w))
    e = y - est
    clusters = df["s"].astype(str).to_numpy()
    j = len(set(clusters))
    u = np.array([np.sum(w[clusters == c] * e[clusters == c]) for c in set(clusters)])
    bread = np.sum(w)
    var = np.sum(u ** 2) / bread ** 2
    if j > 1 and k > p:
        var *= (j / (j - 1)) * ((k - 1.0) / (k - p))
    se = math.sqrt(max(var, 0))
    return dict(k=k, n_clusters=j, est=est, se=se, lo=est - 1.96 * se, hi=est + 1.96 * se)


def _cluster_seq(clusters):
    """Ordered unique cluster labels (as strings)."""
    return list(dict.fromkeys(str(c) for c in clusters))


def _exchangeable_inverse(v, rho):
    """Return ``(a, b)`` with ``(V^-1)_ij = (a*delta_ij + b)/(s_i s_j)``.

    ``V`` is the exchangeable working covariance ``V_ij = rho*sqrt(v_i v_j)``
    (i != j), ``V_ii = v_i``; ``s_i = sqrt(v_i)``.
    """
    n = len(v)
    if n == 1:
        return 1.0 / float(v[0]), 0.0
    rho = min(max(float(rho), -1.0 / (n - 1) + 1e-9), 1.0 - 1e-9)
    a = 1.0 / (1.0 - rho)
    b = -rho / ((1.0 - rho) * (1.0 + (n - 1) * rho))
    return a, b


def rve_intercept(yi, vi, cluster, rho=0.5):
    """Single-intercept meta-regression with cluster-robust (RVE) sandwich SEs.

    Hedges, Tipton & Johnson (2010): exchangeable working covariance within a
    cluster (``rho``), working-GLS intercept, ``G/(G-1)`` correction and a
    ``t(G-1)`` interval.
    """
    y = np.asarray(yi, float)
    v = np.asarray(vi, float)
    g = np.asarray([str(c) for c in cluster])
    m = np.isfinite(y) & np.isfinite(v) & (v > 0)
    y, v, g = y[m], v[m], g[m]
    k = len(y)
    clusters = _cluster_seq(g)
    G = len(clusters)
    nan = dict(k=k, n_clusters=G, est=np.nan, se=np.nan, lo=np.nan, hi=np.nan,
               max_weight_share=np.nan, max_weight_cluster="", df=np.nan)
    if k < 2 or G < 1:
        return nan
    if G < 2:
        w = 1.0 / v
        est = float(np.sum(w * y) / np.sum(w))
        se = float(math.sqrt(1.0 / np.sum(w)))
        return dict(k=k, n_clusters=1, est=est, se=se, lo=est - 1.96 * se,
                    hi=est + 1.96 * se, max_weight_share=1.0,
                    max_weight_cluster=clusters[0], df=0)
    cw, sy = {}, {}
    den = 0.0
    for c in clusters:
        idx = g == c
        vc, yc = v[idx], y[idx]
        a, b = _exchangeable_inverse(vc, rho)
        if len(vc) == 1:
            Dg = a
            Sy = a * yc[0]
        else:
            s = np.sqrt(vc)
            inv_s = 1.0 / s
            Dg = a * np.sum(inv_s ** 2) + b * (np.sum(inv_s)) ** 2
            Sy = a * np.sum(yc / vc) + b * np.sum(yc / s) * np.sum(inv_s)
        den += Dg
        cw[c] = Dg
        sy[c] = Sy
    est = sum(sy.values()) / den
    meat = 0.0
    for c in clusters:
        idx = g == c
        vc, yc = v[idx], y[idx]
        a, b = _exchangeable_inverse(vc, rho)
        e = yc - est
        if len(vc) == 1:
            ui = a * e[0]
        else:
            s = np.sqrt(vc)
            inv_s = 1.0 / s
            ui = a * np.sum(e / vc) + b * np.sum(e / s) * np.sum(inv_s)
        meat += float(ui) ** 2
    var = meat / den ** 2
    if G > 1:
        var *= G / (G - 1.0)
    se = math.sqrt(max(var, 0.0))
    if _st is not None and G > 1:
        tc = float(_st.t.ppf(0.975, G - 1))
    else:
        tc = 1.96
    shares = {c: cw[c] / den for c in clusters}
    top = max(shares, key=shares.get)
    return dict(k=k, n_clusters=G, est=est, se=se, lo=est - tc * se, hi=est + tc * se,
                max_weight_share=shares[top], max_weight_cluster=top, df=G - 1)


def three_level_re(yi, vi, cluster, study):
    """Three-level random-effects model (effects within experiment within study).

    Method-of-moments variance components (see the pipeline note).
    """
    y = np.asarray(yi, float)
    v = np.asarray(vi, float)
    g = np.asarray([str(c) for c in cluster])
    s = np.asarray([str(x) for x in study])
    m = np.isfinite(y) & np.isfinite(v) & (v > 0)
    y, v, g, s = y[m], v[m], g[m], s[m]
    k = len(y)
    nan = dict(k=k, n_clusters=0, n_studies=0, est=np.nan, se=np.nan, lo=np.nan,
               hi=np.nan, tau2_cluster=np.nan, tau2_study=np.nan, i2=np.nan)
    if k == 0:
        return nan
    clusters, studies = _cluster_seq(g), _cluster_seq(s)
    num = den = 0.0
    for c in clusters:
        idx = g == c
        if idx.sum() < 2:
            continue
        w = 1.0 / v[idx]
        yc = y[idx]
        W = float(np.sum(w))
        ybar = float(np.sum(w * yc)) / W
        q = float(np.sum(w * (yc - ybar) ** 2))
        cg = W - float(np.sum(w ** 2)) / W
        num += q - (idx.sum() - 1)
        den += cg
    tau2_c = max(0.0, num / den) if den > 0 else 0.0
    thetas, vs = [], []
    for st in studies:
        idx = s == st
        w = 1.0 / (v[idx] + tau2_c)
        thetas.append(float(np.sum(w * y[idx]) / np.sum(w)))
        vs.append(float(1.0 / np.sum(w)))
    r = dl_random(thetas, vs)
    tau2_s = float(r["tau2"]) if np.isfinite(r["tau2"]) else 0.0
    w = 1.0 / (v + tau2_c + tau2_s)
    est = float(np.sum(w * y) / np.sum(w))
    se = float(math.sqrt(1.0 / np.sum(w)))
    return dict(k=k, n_clusters=len(clusters), n_studies=len(studies),
                est=est, se=se, lo=est - 1.96 * se, hi=est + 1.96 * se,
                tau2_cluster=tau2_c, tau2_study=tau2_s, i2=r["i2"])


def max_weight_share(yi, vi, cluster, tau2=0.0):
    """Largest cluster share of inverse-variance weight ``1/(vi + tau2)``."""
    y = np.asarray(yi, float)
    v = np.asarray(vi, float)
    g = np.asarray([str(c) for c in cluster])
    m = np.isfinite(y) & np.isfinite(v) & (v > 0)
    v, g = v[m], g[m]
    if not len(v):
        return np.nan, ""
    w = 1.0 / (v + float(tau2))
    tot = float(np.sum(w))
    if tot <= 0:
        return np.nan, ""
    shares = {c: float(np.sum(w[g == c])) / tot for c in _cluster_seq(g)}
    top = max(shares, key=shares.get)
    return shares[top], top


def pool_dependence(yi, vi, cluster, study, rho_values=(0.5, 0.8)):
    """All dependence-aware pooled estimates for one pool, with the DL comparison."""
    y = np.asarray(yi, float)
    v = np.asarray(vi, float)
    g = np.asarray([str(c) for c in cluster])
    s = np.asarray([str(x) for x in study])
    m = np.isfinite(y) & np.isfinite(v) & (v > 0)
    y, v, g, s = y[m], v[m], g[m], s[m]
    dl = dl_random(y, v)
    tl = three_level_re(y, v, g, s)
    dlshare, dlcl = max_weight_share(y, v, g, tau2=dl["tau2"])
    out = {
        "dl_k": dl["k"], "dl_est": dl["est"], "dl_se": dl["se"], "dl_lo": dl["lo"],
        "dl_hi": dl["hi"], "dl_tau2": dl["tau2"], "dl_i2": dl["i2"],
        "max_weight_share_dl": dlshare, "max_weight_cluster_dl": dlcl,
        "three_k": tl["k"], "three_est": tl["est"], "three_se": tl["se"],
        "three_lo": tl["lo"], "three_hi": tl["hi"], "tau2_cluster": tl["tau2_cluster"],
        "tau2_study": tl["tau2_study"], "three_i2": tl["i2"],
        "n_clusters": tl["n_clusters"], "n_studies": tl["n_studies"],
    }
    for rho in rho_values:
        tag = "rve" if float(rho) == 0.5 else f"rve_rho{int(round(float(rho) * 100))}"
        r = rve_intercept(y, v, g, rho=rho)
        out.update({
            f"{tag}_est": r["est"], f"{tag}_se": r["se"],
            f"{tag}_lo": r["lo"], f"{tag}_hi": r["hi"],
            f"{tag}_df": r["df"], f"{tag}_n_clusters": r["n_clusters"],
            f"max_weight_share_{tag}": r["max_weight_share"],
            f"max_weight_cluster_{tag}": r["max_weight_cluster"],
        })
    return out


def leave_one_out(yi, vi, unit, is_study=False):
    """Leave-one-out influence diagnostics (DL on the reduced set)."""
    import pandas as _pd
    df = _pd.DataFrame({"y": np.asarray(yi, float), "v": np.asarray(vi, float),
                        "u": np.asarray(unit, dtype=object)}).dropna()
    df = df[np.isfinite(df["y"]) & np.isfinite(df["v"]) & (df["v"] > 0)]
    full = dl_random(df["y"], df["v"])
    rows = []
    if is_study:
        groups = [(str(u), np.where(df["u"].astype(str) == str(u))[0]) for u in df["u"].unique()]
    else:
        groups = [(str(u), np.array([i])) for i, u in enumerate(df["u"])]
    for name, idx in groups:
        mask = np.ones(len(df), bool)
        mask[idx] = False
        if mask.sum() < 2:
            continue
        r = dl_random(df["y"].to_numpy()[mask], df["v"].to_numpy()[mask])
        infl = bool(np.isfinite(r["lo"]) and np.isfinite(full["est"]) and
                    ((r["lo"] > full["est"]) or (r["hi"] < full["est"])))
        rows.append({"omitted": name, "k_omitted": int(mask.sum()), "est": r["est"],
                     "se": r["se"], "lo": r["lo"], "hi": r["hi"], "tau2": r["tau2"],
                     "i2": r["i2"], "delta": r["est"] - full["est"], "influential": infl})
    return full, _pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# effect builders (moved from suite_common / metaanalysis)
# ---------------------------------------------------------------------------
_RAW_EFFECT_RE = __import__("re").compile(
    # NOTE: `_` is a WORD character, so the original `\b(beta|odds|...)\b` form
    # never matched the `_`-joined spellings that real codings use
    # (`odds_ratio`, `log_odds`, `logistic_regression`, `unstandardized_beta`,
    # `risk_ratio`, `hazard_ratio`, `mean_difference`, ...).  Match any such form
    # by allowing a leading non-alphanumeric boundary plus the joined variants.
    r"(?<![a-z0-9])(unstandardis|unstandardiz|beta|log[_\s-]*odds|logit|logistic|odds|"
    r"coef|coefficient|regression|b)(?![a-z0-9])",
    __import__("re").I)
# `unstandardised/ized` NEGATES a standardised marker: `standardized_beta` is a
# standardised effect, `unstandardized_beta` is a raw one.  The negation has to be
# tested explicitly because the stem "standardiz" is a substring of both.
_NEGATED_STD_RE = __import__("re").compile(r"unstandardis|unstandardiz", __import__("re").I)
# Reported-type values that carry NO usable scale information.  A bare reported
# `yi`/`vi` with one of these cannot be told apart from a raw-scale effect, so it
# is refused (fail closed) unless the `src` already proves a standardisation.
_BLANK_TYPE = {"other", "none", "n/a", "na", "unclear", "unknown", "not stated", "not stated."}
_STD_MARKER_RE = __import__("re").compile(
    r"\bhedges\b|\bcohen\b|\bsmd\b|\bfisher\b|\bstandardis|\bstandardiz|"
    r"\betasq|\bd\b|\bg\b|\bz\b", __import__("re").I)
# Opt-in switch that restores the pre-2026-09-27 `beta/n` behaviour (a raw
# coefficient given the Fisher-z variance 1/(n-3)).  OFF by default -- see #2 in
# Scripts/SB_SHARED_CODE_TRAPS.md.  Use only to reproduce the pre-fix numbers.
_ALLOW_BETA_N_VARIANCE = __import__("os").environ.get(
    "ALLOW_BETA_N_VARIANCE", "").strip().lower() in ("1", "true", "yes", "on")


def is_standardised_metric(metric):
    """True when a derived effect metric is on a standardised (d/g/z) scale."""
    m = str(metric or "").strip().lower()
    return m not in ("", "beta", "none")


def conversion_text(dv):
    """Concatenated free-text conversion metadata of one coded DV."""
    return " ".join(str(dv.get(k) or "") for k in ("conversion_method", "conversion_inputs"))


def source_is_standardised(src, reported_type="", conv_text=""):
    """True when the pooled effect behind ``src`` is standardised."""
    s = str(src or "").strip().lower()
    if s in ("g", "d", "t->z", "r->z"):
        return True
    if any(tok in s for tok in ("t->z", "r->z", "means/sd")):
        return True
    if s in ("paper_only", "harmonized_unmatched"):
        return True
    if s.startswith(("cells:", "fields:", "harm:", "means/sd", "derived")):
        return True
    r = str(reported_type or "")
    if _RAW_EFFECT_RE.search(r):
        # A raw reported type is REFUSED unless the type itself also carries a
        # genuine standardised marker, and never when that marker is negated.
        # The conversion free text deliberately NO LONGER rescues the row: a note
        # that merely mentioned "d"/"g"/"z"/"standardised" was re-admitting ~1,734
        # raw-`beta` rows (SB_SHARED_CODE_TRAPS.md #3/#4).
        if _NEGATED_STD_RE.search(r) or not _STD_MARKER_RE.search(r):
            return False
        return True
    if str(r).strip().lower() in _BLANK_TYPE:
        # FAIL CLOSED on an explicitly generic type ("other"/"none"/...): a bare
        # reported `yi` on a raw scale (0-100 accuracy, minutes, a log-odds) must
        # not pool beside d/g/z.  A genuinely empty `reported_type` is left open
        # on purpose -- many codings place a *reported* standardised effect in
        # `yi` without restating its type, and refusing those would drop valid
        # rows; the derived families already returned above.
        return False
    return True


def first_num(dv, *keys):
    """First non-null numeric value among ``keys`` (present-but-null falls through)."""
    for k in keys:
        v = dv.get(k)
        if v is None:
            continue
        n = num(v)
        if not np.isnan(n):
            return n
    return np.nan


def _clamp_r(r):
    return max(min(r, 0.999999), -0.999999)


def meta_effect(dv):
    """Return (yi, vi, metric, how) for one coded DV contrast."""
    yi, vi = num(dv.get("yi")), num(dv.get("vi"))
    reported = str(dv.get("effect_size_type_reported") or "").lower()
    if (not np.isnan(yi) and not np.isnan(vi) and vi > 0
            and source_is_standardised("yi/vi", reported, conversion_text(dv))):
        metric = "beta" if any(k in reported for k in
                               ("beta", "log", "odds", "or", "coef", "b")) else "yi"
        return yi, vi, metric, "yi/vi"
    g = num(dv.get("g_val"))
    if not np.isnan(g):
        n1, n2 = num(dv.get("n1i")), num(dv.get("n2i"))
        if np.isnan(n1) or np.isnan(n2):
            nt = num(dv.get("n_total"))
            n1 = n2 = nt / 2 if not np.isnan(nt) else np.nan
        if not np.isnan(n1) and not np.isnan(n2) and n1 + n2 > 2:
            vi = (n1 + n2) / (n1 * n2) + g * g / (2 * (n1 + n2))
            return g, vi, "g", "g"
    d = num(dv.get("d_val"))
    if not np.isnan(d):
        n1, n2 = num(dv.get("n1i")), num(dv.get("n2i"))
        if np.isnan(n1) or np.isnan(n2):
            nt = num(dv.get("n_total"))
            n1 = n2 = nt / 2 if not np.isnan(nt) else np.nan
        if not np.isnan(n1) and not np.isnan(n2) and n1 + n2 > 2:
            vi = (n1 + n2) / (n1 * n2) + d * d / (2 * (n1 + n2))
            return d, vi, "d", "d"
    b = num(dv.get("beta_val"))
    if not np.isnan(b):
        se = num(dv.get("beta_se"))
        if np.isnan(se):
            se = num(dv.get("sei"))
        if not np.isnan(se) and se > 0:
            return b, se * se, "beta", "beta/se"
        v = num(dv.get("vi"))
        if not np.isnan(v) and v > 0:
            return b, v, "beta", "beta/vi"
        nt = num(dv.get("n_total"))
        if not np.isnan(nt) and nt > 3 and _ALLOW_BETA_N_VARIANCE:
            # DELIBERATELY DISABLED (2026-09-27, SB_SHARED_CODE_TRAPS.md #2).
            # `1/(n-3)` is the FISHER-Z variance: correct for the r->z and t->z
            # branches, but dimensionally wrong for a RAW regression coefficient,
            # whose sampling variance depends on the residual SD and the predictor
            # scale, not on n alone.  175 eligible rows across 13 studies were
            # pooling with an implied SE of 0.0015-0.21, i.e. near-dominant weight
            # in a random-effects model.  A row carrying only `beta + n` now falls
            # through to the no-route case, where `route_skips` records it loudly
            # instead of quietly pooling a fabricated variance.
            # Set ALLOW_BETA_N_VARIANCE=1 only to reproduce the pre-fix numbers.
            return b, 1.0 / max(nt - 3, 1), "beta", "beta/n"
    t = num(dv.get("t_val"))
    n1, n2 = num(dv.get("n1i")), num(dv.get("n2i"))
    if np.isnan(n1) or np.isnan(n2):
        nt = num(dv.get("n_total"))
        n1 = n2 = nt / 2 if not np.isnan(nt) else np.nan
    if not np.isnan(t):
        if not np.isnan(n1) and not np.isnan(n2) and n1 + n2 > 2:
            r = _clamp_r(t / math.sqrt(t * t + n1 + n2 - 2))
            z = 0.5 * math.log((1 + r) / (1 - r))
            return z, 1 / max(n1 + n2 - 3, 1), "z", "t->z"
        df = num(dv.get("df"))
        if np.isnan(df):
            df = num(dv.get("df1"))
        if not np.isnan(df) and df > 1:
            r = _clamp_r(t / math.sqrt(t * t + df))
            z = 0.5 * math.log((1 + r) / (1 - r))
            return z, 1 / max(df - 1, 1), "z", "t->z"
    r = num(dv.get("r_val"))
    n = num(dv.get("n_total"))
    if not np.isnan(r) and not np.isnan(n) and abs(r) < 1:
        z = 0.5 * math.log((1 + r) / (1 - r))
        return z, 1 / max(n - 3, 1), "z", "r->z"
    m1, m2 = first_num(dv, "m1i_post", "m1i"), first_num(dv, "m2i_post", "m2i")
    s1, s2 = first_num(dv, "sd1i_post", "sd1i"), first_num(dv, "sd2i_post", "sd2i")
    if not any(np.isnan(v) for v in (m1, m2, s1, s2, n1, n2)) and s1 > 0 and s2 > 0 and n1 + n2 > 2:
        sp = math.sqrt(((n1 - 1) * s1 ** 2 + (n2 - 1) * s2 ** 2) / (n1 + n2 - 2))
        g = (m1 - m2) / sp * (1 - 3 / (4 * (n1 + n2) - 9))
        vi = (n1 + n2) / (n1 * n2) + g * g / (2 * (n1 + n2))
        return g, vi, "g", "means/sd"
    return np.nan, np.nan, "none", "none"


def reported_effect(dv):
    """One coded/reported effect (yi, vi, how) -- identical order to ``meta_effect``."""
    yi, vi, _metric, how = meta_effect(dv)
    return yi, vi, how


def hedges_g(m1, s1, n1, m2, s2, n2):
    """Hedges' g with its sampling variance from means/SDs."""
    if any(np.isnan(x) for x in (m1, s1, n1, m2, s2, n2)) or s1 <= 0 or s2 <= 0 or n1 + n2 <= 2:
        return np.nan, np.nan
    sp = math.sqrt(((n1 - 1) * s1 ** 2 + (n2 - 1) * s2 ** 2) / (n1 + n2 - 2))
    if sp <= 0:
        return np.nan, np.nan
    g = (m1 - m2) / sp * (1 - 3 / (4 * (n1 + n2) - 9))
    v = (n1 + n2) / (n1 * n2) + g * g / (2 * (n1 + n2))
    return g, v


def meta_did(dv, r=0.5):
    """DiD from a coded DV: (post_IG - pre_IG) - (post_CG - pre_CG)."""
    def arm(pref_m, pref_s, pref_n, postf_m, postf_s, postf_n):
        return (num(dv.get(pref_m)), num(dv.get(pref_s)), num(dv.get(pref_n)),
                num(dv.get(postf_m)), num(dv.get(postf_s)), num(dv.get(postf_n)))

    a1 = arm("m1i_pre", "sd1i_pre", "n1i_pre", "m1i_post", "sd1i_post", "n1i_post")
    a2 = arm("m2i_pre", "sd2i_pre", "n2i_pre", "m2i_post", "sd2i_post", "n2i_post")
    src = "coded m/sd pre-post"
    if any(np.isnan(v) for v in a1 + a2):
        cells = dv.get("cell_descriptives") or []
        rec = {}
        for c in cells:
            if not isinstance(c, dict):
                continue
            g = str(c.get("group") or "").strip()
            tp = str(c.get("timepoint") or "").strip().lower()
            mm, ss, nn = num(c.get("mean", c.get("m"))), num(c.get("sd")), num(c.get("n"))
            if g and tp and not np.isnan(mm):
                rec.setdefault(g, {})[tp] = (mm, ss, nn)
        g1, g2 = str(dv.get("group1_label") or "").strip(), str(dv.get("group2_label") or "").strip()
        if len(rec) >= 2:
            def find(lab):
                if lab in rec:
                    return lab
                for k in rec:
                    if lab and (k.lower().startswith(lab.lower()) or lab.lower().startswith(k.lower())):
                        return k
                return None
            k1, k2 = find(g1), find(g2)
            if k1 is None or k2 is None:
                keys = list(rec)
                k1, k2 = keys[0], keys[1]

            def pick(grp):
                pre = post = None
                for tp, val in rec.get(grp, {}).items():
                    if CT.classify_timepoint(tp) == "pre":
                        pre = val
                    elif CT.classify_timepoint(tp) == "post":
                        post = val
                return pre, post
            p1, q1 = pick(k1)
            p2, q2 = pick(k2)
            if p1 and q1 and p2 and q2:
                a1 = (p1[0], p1[1], p1[2], q1[0], q1[1], q1[2])
                a2 = (p2[0], p2[1], p2[2], q2[0], q2[1], q2[2])
                src = "cell_descriptives"
    m1pre, s1pre, n1, m1post, s1post, _ = a1
    m2pre, s2pre, n2, m2post, s2post, _ = a2
    if any(np.isnan(v) for v in (m1pre, s1pre, m1post, s1post, m2pre, s2pre, m2post, s2post)):
        return None
    if any(v <= 0 for v in (s1pre, s1post, s2pre, s2post)):
        return None
    if np.isnan(n1) or np.isnan(n2):
        nt = num(dv.get("n_total")) or num(dv.get("n1i_post")) or num(dv.get("n2i_post"))
        if np.isnan(n1) and np.isnan(n2) and not np.isnan(nt):
            n1 = n2 = nt / 2
        elif np.isnan(n1):
            n1 = n2
        elif np.isnan(n2):
            n2 = n1
    if np.isnan(n1) or np.isnan(n2) or n1 < 2 or n2 < 2:
        return None
    dc1 = m1post - m1pre
    dc2 = m2post - m2pre
    sp = math.sqrt(((n1 - 1) * s1pre ** 2 + (n2 - 1) * s2pre ** 2) / (n1 + n2 - 2))
    if sp <= 0:
        return None
    # Baseline-imbalance guard (2026-09-27, SB_SHARED_CODE_TRAPS.md #10).
    # The r=0.5 default is a WITHIN-PERSON pre/post correlation.  When the arms
    # are already separated at baseline the change-score route is unreliable, and
    # r=0.5 can make the derived SE far smaller than the authors' own (Wineburg et
    # al. (2022): 0.099 vs the paper's 0.44 -> ~20x the authors' weight).  Above a
    # 0.25 pooled-SD baseline gap we take the conservative r=0 variance -- which is
    # never smaller than any r>0 -- and flag the row so a reviewer can see that the
    # assumption changed.  Will (2002) derived r=0.494 from its own paired t, so
    # the 0.5 default is fine for balanced arms and is left untouched there.
    baseline_gap = abs(m1pre - m2pre) / sp
    imbalanced = baseline_gap > 0.25
    r_used = 0.0 if imbalanced else r
    v1 = max((s1pre ** 2 + s1post ** 2 - 2 * r_used * s1pre * s1post) / n1, 1e-12)
    v2 = max((s2pre ** 2 + s2post ** 2 - 2 * r_used * s2pre * s2post) / n2, 1e-12)
    d = (dc1 - dc2) / sp
    var = (v1 + v2) / sp ** 2
    return dict(effect=d, var=var, n1=n1, n2=n2, r=r_used, source=src,
                baseline_gap=baseline_gap, baseline_imbalance=bool(imbalanced))


# back-compat alias
_meta_did = meta_did


# ---------------------------------------------------------------------------
# Bayesian estimators (D5: moved from Scripts/coding_web/statslib.py)
# ---------------------------------------------------------------------------
def _mat_inv(A):
    n = len(A)
    M = [list(row) + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            return None
        M[col], M[piv] = M[piv], M[col]
        pv = M[col][col]
        M[col] = [v / pv for v in M[col]]
        for r in range(n):
            if r != col and M[r][col] != 0.0:
                f = M[r][col]
                M[r] = [a - f * b for a, b in zip(M[r], M[col])]
    return [row[n:] for row in M]


def _wls_cluster(y, X, w, groups):
    """WLS with a cluster-robust (CR1) sandwich -- the app's moderator fitter."""
    n, p = len(y), len(X[0])
    XtWX = [[sum(w[i] * X[i][a] * X[i][b] for i in range(n)) for b in range(p)] for a in range(p)]
    XtWy = [sum(w[i] * X[i][a] * y[i] for i in range(n)) for a in range(p)]
    inv = _mat_inv(XtWX)
    if inv is None:
        return None
    beta = [sum(inv[a][b] * XtWy[b] for b in range(p)) for a in range(p)]
    resid = [y[i] - sum(X[i][a] * beta[a] for a in range(p)) for i in range(n)]
    by_group = {}
    for i, g in enumerate(groups):
        by_group.setdefault(g, []).append(i)
    meat = [[0.0] * p for _ in range(p)]
    for idxs in by_group.values():
        s = [sum(w[i] * X[i][a] * resid[i] for i in idxs) for a in range(p)]
        for a in range(p):
            for b in range(p):
                meat[a][b] += s[a] * s[b]
    tmp = [[sum(meat[k][l] * inv[l][b] for l in range(p)) for b in range(p)] for k in range(p)]
    cov = [[sum(inv[a][k] * tmp[k][b] for k in range(p)) for b in range(p)] for a in range(p)]
    G = len(by_group)
    if G > 1 and n > p:
        f = (G / (G - 1.0)) * ((n - 1.0) / (n - p))
        cov = [[v * f for v in row] for row in cov]
    se = [math.sqrt(max(cov[a][a], 0.0)) for a in range(p)]
    return {"beta": beta, "cov": cov, "inv": inv, "se": se, "resid": resid,
            "n_studies": G, "n": n, "p": p}


def _dl_tau2(y, v):
    if not y:
        return 0.0
    w = [1.0 / x for x in v]
    sw = sum(w)
    fe = sum(wi * yi for wi, yi in zip(w, y)) / sw
    q = sum(wi * (yi - fe) ** 2 for wi, yi in zip(w, y))
    c = sw - sum(wi * wi for wi in w) / sw
    k = len(y)
    return max(0.0, (q - (k - 1)) / c) if c > 0 else 0.0


def _chi2_sf(x, df):
    try:
        from scipy import stats
        return float(stats.chi2.sf(x, df))
    except Exception:
        return None


def _t_sf(x, df):
    try:
        from scipy import stats
        return float(stats.t.sf(x, df))
    except Exception:
        return None


def _t_ppf(q, df):
    try:
        from scipy import stats
        return float(stats.t.ppf(q, df))
    except Exception:
        return None


def _dl_pool(y, v):
    """DerSimonian-Laird pooled estimate (list-based app twin of ``dl_random``)."""
    r = dl_random(np.asarray(y, float), np.asarray(v, float))
    return {"k": r["k"], "est": r["est"], "se": r["se"], "lo": r["lo"], "hi": r["hi"],
            "tau2": r["tau2"], "i2": r["i2"]}


def _study_blocks(groups):
    by = {}
    for i, g in enumerate(groups):
        by.setdefault(g, []).append(i)
    return by


def _two_level_components(y, v, groups):
    """Method-of-moments tau^2_between / tau^2_within (+ per-level I^2)."""
    by = _study_blocks(groups)
    q_w = c_w = 0.0
    df_w = 0
    st_y, st_v = [], []
    for idxs in by.values():
        ws = {i: 1.0 / v[i] for i in idxs}
        sw = sum(ws.values())
        ybar = sum(ws[i] * y[i] for i in idxs) / sw
        st_y.append(ybar)
        st_v.append(1.0 / sw)
        q_w += sum(ws[i] * (y[i] - ybar) ** 2 for i in idxs)
        c_w += sw - sum(wi * wi for wi in ws.values()) / sw
        df_w += max(0, len(idxs) - 1)
    tau2_w = max(0.0, (q_w - df_w) / c_w) if c_w > 0 else 0.0
    k = len(st_y)
    w = [1.0 / x for x in st_v]
    sw = sum(w)
    fe = sum(wi * yi for wi, yi in zip(w, st_y)) / sw if sw else 0.0
    q_b = sum(wi * (yi - fe) ** 2 for wi, yi in zip(w, st_y))
    c_b = sw - sum(wi * wi for wi in w) / sw
    tau2_b = max(0.0, (q_b - (k - 1)) / c_b) if c_b > 0 else 0.0
    sv = sorted(v)
    nv = len(sv)
    vbar = (sv[nv // 2] if nv % 2 else 0.5 * (sv[nv // 2 - 1] + sv[nv // 2])) if nv else 0.0
    total = vbar + tau2_w + tau2_b
    shares = {
        "sampling": (vbar / total) if total > 0 else 0.0,
        "within": (tau2_w / total) if total > 0 else 0.0,
        "between": (tau2_b / total) if total > 0 else 0.0,
    }
    return {
        "k": k, "n_studies": k,
        "tau2_between": tau2_b, "tau2_within": tau2_w,
        "sigma2_between": tau2_b, "tau2_within_effects": tau2_w,
        "vbar": vbar, "v_total": total, "shares": shares,
        "i2_sampling": shares["sampling"] * 100,
        "i2_within_alloc": shares["within"] * 100,
        "i2_between_alloc": shares["between"] * 100,
        "q_between": q_b, "df_between": max(0, k - 1),
        "q_within": q_w, "df_within": df_w,
        "i2_between": (max(0.0, (q_b - (k - 1)) / q_b) * 100 if q_b > 0 else 0.0),
        "i2_within": (max(0.0, (q_w - df_w) / q_w) * 100 if q_w > 0 else 0.0),
    }


def _study_level(y, X, v, groups):
    """Stage-1 fixed-effect combination of effects (and study-level moderators)."""
    by = _study_blocks(groups)
    st_y, st_v, st_X, st_ids = [], [], [], []
    p = len(X[0]) if X else 0
    for g, idxs in by.items():
        ws = {i: 1.0 / v[i] for i in idxs}
        sw = sum(ws.values())
        st_y.append(sum(ws[i] * y[i] for i in idxs) / sw)
        st_v.append(1.0 / sw)
        st_X.append([sum(ws[i] * X[i][a] for i in idxs) / sw for a in range(p)])
        st_ids.append(g)
    return st_y, st_v, st_X, st_ids


def bayes_tau_within(y, v, groups, grid_n=201, tau_sd=None, tau_prior=None):
    """Posterior of the within-study SD from the per-study heterogeneity Q."""
    if tau_sd is None:
        tau_sd = BAYES_TAU_SD
    if tau_prior is None:
        tau_prior = "half-normal"
    stt = []
    for idxs in _study_blocks(groups).values():
        if len(idxs) < 2:
            continue
        ws = {i: 1.0 / v[i] for i in idxs}
        sw = sum(ws.values())
        ybar = sum(ws[i] * y[i] for i in idxs) / sw
        q = sum(ws[i] * (y[i] - ybar) ** 2 for i in idxs)
        c = sw - sum(wi * wi for wi in ws.values()) / sw
        stt.append((len(idxs) - 1, q, c))
    if not stt:
        return None

    def loglik(tau):
        t2 = tau * tau
        ll = 0.0
        for nu, q, c in stt:
            s = 1.0 + c * t2 / nu
            x = q / s
            ll += (-math.log(s) + (nu / 2.0 - 1.0) * math.log(max(x, 1e-300))
                   - x / 2.0 - (nu / 2.0) * math.log(2.0) - math.lgamma(nu / 2.0))
        return ll

    grid = _bayes_tau_grid(grid_n)
    logpost = [loglik(t) + _tau_log_prior(t, tau_sd, tau_prior) for t in grid]
    w = _bayes_log_weights(logpost)
    mean = sum(wi * t for wi, t in zip(w, grid))
    cum = 0.0
    lo = hi = None
    for wi, t in zip(w, grid):
        cum += wi
        if lo is None and cum >= 0.025:
            lo = t
        if hi is None and cum >= 0.975:
            hi = t
    return {"tau": mean, "lo": lo, "hi": hi, "tau2": mean * mean, "n_studies": len(stt)}


BAYES_MU_SD = 10.0
BAYES_TAU_SD = 0.5
BAYES_TAU_MAX = 3.0
BAYES_MU_SD_PRESETS = (1.0, 5.0, 10.0, 100.0)
BAYES_TAU_PRIOR_KINDS = ("half-normal", "half-cauchy")
BAYES_TAU_PRESETS = (("half-normal", 0.25), ("half-normal", 0.5), ("half-normal", 1.0),
                     ("half-cauchy", 0.5), ("half-cauchy", 1.0))


def _tau_log_prior(tau, sd, kind):
    if kind == "half-cauchy":
        return -math.log1p((tau / sd) ** 2)
    return -0.5 * (tau / sd) ** 2


def _tau_prior_density0(sd, kind):
    if kind == "half-cauchy":
        return 2.0 / (math.pi * sd)
    return 2.0 / (sd * math.sqrt(2.0 * math.pi))


def _resolve_bayes_priors(payload_or_args):
    """Pick the (mu_sd, tau_prior, tau_sd) from the request, clamped to presets."""
    def g(k, default=None):
        try:
            return payload_or_args.get(k)
        except AttributeError:
            return default
    try:
        mu_sd = float(g("mu_sd", BAYES_MU_SD))
    except (TypeError, ValueError):
        mu_sd = BAYES_MU_SD
    if not any(abs(mu_sd - p) < 1e-9 for p in BAYES_MU_SD_PRESETS):
        mu_sd = BAYES_MU_SD
    kind = str(g("tau_prior", "half-normal") or "half-normal").strip().lower()
    if kind not in BAYES_TAU_PRIOR_KINDS:
        kind = "half-normal"
    try:
        tau_sd = float(g("tau_sd", BAYES_TAU_SD))
    except (TypeError, ValueError):
        tau_sd = BAYES_TAU_SD
    if not any(k == kind and abs(tau_sd - s) < 1e-9 for k, s in BAYES_TAU_PRESETS):
        kind, tau_sd = "half-normal", BAYES_TAU_SD
    return mu_sd, kind, tau_sd


def _bayes_tau_grid(n):
    """tau grid on [0, BAYES_TAU_MAX]; note the truncation at BAYES_TAU_MAX."""
    return [BAYES_TAU_MAX * i / (n - 1) for i in range(n)]


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _chol_logdet(A):
    """log det(A) for a symmetric positive-definite matrix (pure Python)."""
    n = len(A)
    L = [[0.0] * n for _ in range(n)]
    ld = 0.0
    for i in range(n):
        for j in range(i + 1):
            s = sum(L[i][k] * L[j][k] for k in range(j))
            if i == j:
                val = A[i][i] - s
                if val <= 0:
                    return None
                L[i][j] = math.sqrt(val)
                ld += math.log(L[i][j])
            else:
                if L[j][j] == 0:
                    return None
                L[i][j] = (A[i][j] - s) / L[j][j]
    return 2.0 * ld


def _mixture_summary(weights, means, sds, taus):
    """Posterior mean/SD, 95% CrI, P(>0) and tau posterior for a normal mixture."""
    pm = sum(w * m for w, m in zip(weights, means))
    pv = sum(w * (s * s + m * m) for w, m, s in zip(weights, means, sds)) - pm * pm
    psd = math.sqrt(max(pv, 0.0))
    lo_b = min(m - 10 * s for m, s in zip(means, sds)) - 1.0
    hi_b = max(m + 10 * s for m, s in zip(means, sds)) + 1.0

    def cdf(x):
        return sum(w * _norm_cdf((x - m) / s) for w, m, s in zip(weights, means, sds))

    def quantile(q):
        lo, hi = lo_b, hi_b
        for _ in range(60):
            mid = 0.5 * (lo + hi)
            if cdf(mid) < q:
                lo = mid
            else:
                hi = mid
        return 0.5 * (lo + hi)

    p_gt0 = 1.0 - cdf(0.0)
    tau = sum(w * t for w, t in zip(weights, taus))
    tau2 = sum(w * t * t for w, t in zip(weights, taus))
    cum = 0.0
    tlo = thi = None
    for w, t in zip(weights, taus):
        cum += w
        if tlo is None and cum >= 0.025:
            tlo = t
        if thi is None and cum >= 0.975:
            thi = t
    return {"mean": pm, "sd": psd, "lo": quantile(0.025), "hi": quantile(0.975),
            "p_gt0": p_gt0, "tau": tau, "tau2": tau2, "tau_lo": tlo, "tau_hi": thi}


def _bayes_log_weights(logpost):
    m = max(logpost)
    w = [math.exp(x - m) for x in logpost]
    sw = sum(w)
    return [x / sw for x in w]


def bayes_pool(y, v, grid_n=401, mu_sd=BAYES_MU_SD, tau_sd=BAYES_TAU_SD,
               tau_prior="half-normal"):
    """Bayesian random-effects pooled estimate (posterior mean + 95% CrI)."""
    y = [float(a) for a in y]
    v = [float(a) for a in v]
    keep = [i for i in range(len(y)) if math.isfinite(y[i]) and math.isfinite(v[i]) and v[i] > 0]
    y = [y[i] for i in keep]
    v = [v[i] for i in keep]
    k = len(y)
    if k == 0:
        return None
    s2 = mu_sd * mu_sd
    grid = _bayes_tau_grid(grid_n)
    logpost, means, sds = [], [], []
    for tau in grid:
        t2 = tau * tau
        A = B = C = logdet = 0.0
        for i in range(k):
            d = v[i] + t2
            inv = 1.0 / d
            A += inv
            B += y[i] * inv
            C += y[i] * y[i] * inv
            logdet += math.log(d)
        logdet += math.log1p(s2 * A)
        quad = C - s2 * B * B / (1.0 + s2 * A)
        ll = -0.5 * (k * math.log(2.0 * math.pi) + logdet + quad)
        logpost.append(ll + _tau_log_prior(tau, tau_sd, tau_prior))
        prec = A + 1.0 / s2
        means.append(B / prec)
        sds.append(math.sqrt(1.0 / prec))
    w = _bayes_log_weights(logpost)
    s = _mixture_summary(w, means, sds, grid)
    prior_mu0 = 1.0 / (mu_sd * math.sqrt(2.0 * math.pi))
    post_mu0 = sum(w * math.exp(-0.5 * ((0.0 - m) / sd) ** 2) / (sd * math.sqrt(2.0 * math.pi))
                   for w, m, sd in zip(w, means, sds))
    dtau = grid[1] - grid[0] if len(grid) > 1 else 1.0
    mx = max(logpost)
    raw = [math.exp(x - mx) for x in logpost]
    z = dtau * (0.5 * raw[0] + sum(raw[1:-1]) + 0.5 * raw[-1])
    post_tau0 = raw[0] / z if z > 0 else 0.0
    prior_tau0 = _tau_prior_density0(tau_sd, tau_prior)
    return {"k": k, "est": s["mean"], "sd": s["sd"], "se": s["sd"],
            "lo": s["lo"], "hi": s["hi"], "p_gt0": s["p_gt0"],
            "p": 2.0 * min(s["p_gt0"], 1.0 - s["p_gt0"]),
            "tau": s["tau"], "tau2": s["tau2"], "tau_lo": s["tau_lo"], "tau_hi": s["tau_hi"],
            "bf10": (prior_mu0 / post_mu0) if post_mu0 > 0 else float("inf"),
            "bf01_tau0": (post_tau0 / prior_tau0) if prior_tau0 > 0 else None,
            "model": "bayes", "prior_mu_sd": mu_sd, "prior_tau_sd": tau_sd,
            "tau_prior": tau_prior,
            "tau_max": BAYES_TAU_MAX, "grid_n": grid_n}


def bayes_meta(y, X, v, grid_n=161, mu_sd=BAYES_MU_SD, tau_sd=BAYES_TAU_SD,
               tau_prior="half-normal"):
    """Bayesian random-effects meta-regression; posterior mean + 95% CrI per term."""
    k = len(y)
    p = len(X[0]) if X else 0
    if k == 0 or p == 0:
        return None
    s2 = mu_sd * mu_sd
    p0 = 1.0 / s2
    used = []
    for tau in _bayes_tau_grid(grid_n):
        t2 = tau * tau
        XtWX = [[0.0] * p for _ in range(p)]
        XtWy = [0.0] * p
        logdetD = yy = 0.0
        for i in range(k):
            inv = 1.0 / (v[i] + t2)
            logdetD += math.log(v[i] + t2)
            yy += y[i] * y[i] * inv
            xi = X[i]
            for a in range(p):
                xa = xi[a]
                if xa == 0.0:
                    continue
                wxa = inv * xa
                XtWy[a] += wxa * y[i]
                for b in range(a, p):
                    XtWX[a][b] += wxa * xi[b]
        for a in range(p):
            for b in range(a):
                XtWX[a][b] = XtWX[b][a]
            XtWX[a][a] += p0
        ldM = _chol_logdet(XtWX)
        invM = _mat_inv(XtWX)
        if ldM is None or invM is None:
            continue
        beta = [sum(invM[a][b] * XtWy[b] for b in range(p)) for a in range(p)]
        quad = yy - sum(XtWy[a] * beta[a] for a in range(p))
        logdet = logdetD + p * math.log(s2) + ldM
        ll = -0.5 * (k * math.log(2.0 * math.pi) + logdet + quad)
        used.append((tau, beta, invM, ll + _tau_log_prior(tau, tau_sd, tau_prior)))
    if not used:
        return None
    w = _bayes_log_weights([u[3] for u in used])
    taus = [u[0] for u in used]
    terms = []
    prior_b0 = 1.0 / (mu_sd * math.sqrt(2.0 * math.pi))
    for a in range(p):
        means = [u[1][a] for u in used]
        sds = [math.sqrt(max(u[2][a][a], 0.0)) for u in used]
        s = _mixture_summary(w, means, sds, taus)
        post_b0 = sum(wi * math.exp(-0.5 * ((0.0 - m) / sd) ** 2) / (sd * math.sqrt(2.0 * math.pi))
                      for wi, m, sd in zip(w, means, sds))
        terms.append({"estimate": s["mean"], "se": s["sd"], "lo": s["lo"], "hi": s["hi"],
                      "prob_gt0": s["p_gt0"],
                      "bf10": (prior_b0 / post_b0) if post_b0 > 0 else float("inf"),
                      "p": 2.0 * min(s["p_gt0"], 1.0 - s["p_gt0"])})
    tau_m = sum(w[j] * used[j][0] for j in range(len(used)))
    tau2_m = sum(w[j] * used[j][0] * used[j][0] for j in range(len(used)))
    cum = 0.0
    tlo = thi = None
    for wi, t in zip(w, taus):
        cum += wi
        if tlo is None and cum >= 0.025:
            tlo = t
        if thi is None and cum >= 0.975:
            thi = t
    return {"terms": terms, "k": k, "p": p, "tau": tau_m, "tau2": tau2_m,
            "tau_lo": tlo, "tau_hi": thi, "model": "bayes",
            "prior_mu_sd": mu_sd, "prior_tau_sd": tau_sd, "tau_prior": tau_prior,
            "grid_n": grid_n}


# underscore aliases used by the app's statslib import surface
_bayes_pool = bayes_pool
_bayes_meta = bayes_meta
_bayes_tau_within = bayes_tau_within
_bayes_tau_grid = _bayes_tau_grid
