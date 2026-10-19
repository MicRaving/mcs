"""Participant-level SDT moderation specs -> ``temp/sdt_participant_specs.csv``.

For every harmonized dataset the SDT discrimination index (and its true-score /
fake-score / response-bias components) is computed **per participant** exactly as
in ``analysis_multiverse_sdt.build_study`` and then aggregated **within each level
of every participant-level covariate** (the ``cov_*`` / ``role = covariate``
columns of the primary datasets).

The output is the same vocabulary as ``temp/sdt_specs.csv`` plus two columns,
``participant_cov`` and ``participant_level``; the coding app exposes the
covariates as a "Participant-level" moderator category for the re-analysis layer
and swaps to these rows when such a moderator is selected.

Raw covariates are resolved to the controlled vocabulary in
``Scripts/covariate_restriction.py`` before the per-level aggregation: aliases of
the same construct (age, ideology, analytic thinking, gender, education, ...) are
merged, so evidence split across names like ``cov_politics`` / ``cov_ideology`` /
``cov_political_ideology`` is pooled under one canonical covariate, and
non-substantive columns (attention/quality checks, completion time, survey
weights, condition/arm codes, order/counterbalance, bookkeeping) are dropped.
Within one study the exact canonical column wins over its aliases, so a derived
continuous ``cov_age`` is never averaged with its own age-band dummies.

Units policy: raw covariate units are inconsistent across datasets (``cov_age``
is years in some datasets, a z-score in others, a birth year in others), so
continuous participant moderators are analysed on the harmonizer's **within-study
0-1 rescale** (``<name>_01``); the raw-unit column is never pooled.  A birth-year
age alias is flipped (``1 - value``) so higher still means older.  See
``Scripts/COVARIATE_VOCABULARY.md``.

Inputs are read-only; the output goes to ``temp/``.
"""
import collections
import glob
import math
import os
import re
import sys

import numpy as np
import pandas as pd

B = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, B)
sys.path.insert(0, os.path.join(B, "Scripts"))
import suite_common as SC  # noqa: E402
import analysis_multiverse_sdt as AMS  # noqa: E402
import covariate_restriction as CR  # noqa: E402

DS = os.path.join(B, "Datasets")
OUT = os.path.join(B, "temp", "sdt_participant_specs.csv")
MIN_STUDIES = 3
MIN_N = 4
MAX_LEVELS = 4


def _covariate_series(wide, side, allcols):
    """{canonical_covariate: Series} from the wide table.

    Raw covariate names are resolved to the controlled vocabulary
    (``Scripts/covariate_restriction.py``): aliases of the same construct are
    merged under one canonical name, non-substantive columns are dropped, and the
    0-1 within-study rescale is preferred so a covariate is comparable across
    studies even when the raw units differ (e.g. ``cov_age`` is years in some
    datasets, a z-score in others; see PARTICIPANT_MODERATORS_ASSESSMENT.md and
    COVARIATE_VOCABULARY.md).

    Within one study the exact canonical column wins over its aliases, so a
    derived continuous ``cov_age`` is never averaged with its own age-band
    dummies; aliases at the same priority (e.g. ``..._social`` + ``..._economic``
    ideology subscales, or a construct's item set) are averaged into one series.
    An age alias stored as a birth year is flipped (``1 - value``) so higher
    still means older.
    """
    groups = {}
    for _, r in side.iterrows():
        uv = str(r.get("uniform_variable") or "").strip()
        role = str(r.get("role") or "").strip().lower()
        if not uv or not (role in ("covariate", "cov") or uv.startswith("cov_")):
            continue
        if CR.is_excluded_cov(uv):
            continue
        b = re.sub(r"_i\d+$", "", uv)
        can = CR.canonical_cov(b)
        if not can:
            continue
        col = uv + "_01" if uv + "_01" in allcols else (uv if uv in allcols else None)
        if not col:
            continue
        groups.setdefault(can, []).append((0 if b == can else 1, uv, col))
    out = {}
    for can, items in groups.items():
        tier = min(t for t, _uv, _col in items)
        parts = []
        for t, uv, col in items:
            if t != tier:
                continue
            try:
                s = pd.to_numeric(wide[col], errors="coerce")
            except Exception:
                continue
            if CR.is_reversed_cov(uv) and col == uv + "_01":
                s = 1.0 - s
            parts.append(s)
        if not parts:
            continue
        s = pd.concat(parts, axis=1).mean(axis=1, skipna=True)
        if s.notna().sum() >= MIN_N:
            out[can] = s
    return out


_CATEGORICAL_COVS = ("cov_gender", "cov_sex", "cov_sex_female", "cov_female")


def _numeric(series):
    num = pd.to_numeric(series, errors="coerce")
    return num if num.notna().sum() >= 6 else None


def _continuous_levels(source, num):
    """Participant subgroups for a continuous covariate.

    Quartile bins, each labelled with the bin's mean value **on the within-study
    0-1 rescale** (raw units are not comparable across datasets), so the moderator
    is fitted continuously rather than as a factor.
    """
    source = pd.to_numeric(source, errors="coerce")
    if source.notna().sum() < 6:
        source = num
    valid = source.dropna()
    if len(valid) >= 6 and valid.nunique() >= 4:
        try:
            bins = pd.qcut(source, 4, duplicates="drop")
        except (ValueError, TypeError):
            bins = None
        if bins is not None and getattr(bins, "cat", None) is not None:
            out = []
            for cat in bins.cat.categories:
                mask = (bins == cat).fillna(False).to_numpy()
                if mask.any():
                    out.append((f"{float(source[mask].mean()):.3g}", mask))
            if len(out) >= 2:
                return out
    med = float(source.median())
    lo = (source <= med).fillna(False).to_numpy()
    hi = (source > med).fillna(False).to_numpy()
    return [(f"{float(source[lo].mean()):.3g}" if lo.any() else "low", lo),
            (f"{float(source[hi].mean()):.3g}" if hi.any() else "high", hi)]


def _levels(series, cov="", continuous=False):
    """[(label, mask)] with a scheme consistent across studies.

    Numeric covariates are CONTINUOUS (quartile bins labelled with the bin's mean
    value on the 0-1 rescale) so age and other scalable covariates are not
    collapsed to a 2-level factor.  Sex/gender is ``male``/``non-male``; other
    categorical covariates keep their normalised observed levels (top
    ``MAX_LEVELS``).
    """
    num = _numeric(series)
    if cov in _CATEGORICAL_COVS:
        if num is not None:
            male = (num > 0.5).fillna(False).to_numpy()
        else:
            v = series.astype(str).str.strip().str.lower()
            male = v.isin(["male", "m", "man", "men", "1", "1.0", "true"]).to_numpy()
        non = (~male) & series.notna().to_numpy()
        return [("male", male), ("non-male", non)]
    if num is not None:
        if continuous:
            return _continuous_levels(series, num)
        med = float(num.median())
        return [("low", (num <= med).fillna(False).to_numpy()),
                ("high", (num > med).fillna(False).to_numpy())]
    v = series.astype(str).str.strip().str.lower()
    counts = v.where(series.notna()).value_counts()
    lvls = [x for x in counts.index if str(x) not in ("", "nan", "none")][:MAX_LEVELS]
    return [(str(lv), (v == str(lv)).to_numpy()) for lv in lvls]


def _stats(vals):
    vals = vals[np.isfinite(vals)]
    n = len(vals)
    if n < MIN_N:
        return None
    eff = float(vals.mean())
    var = float(vals.var(ddof=1) / n)
    if not (np.isfinite(eff) and np.isfinite(var) and var > 0):
        return None
    return n, eff, var


def _mean_var(vals):
    vals = vals[np.isfinite(vals)]
    n = len(vals)
    if n < 1:
        return (np.nan, np.nan)
    return (float(vals.mean()), float(vals.var(ddof=1) / n) if n > 1 else np.nan)


def _row(spec, tag, key, art, dom, tp, metric, n, eff, var, ts, fs, cs, cov, lvl, kind, note):
    return dict(
        layer="sdt", spec=spec, exclusions=tag, study_key=key, experiment_id=str(art),
        contrast_id=f"{key}|{art}|{dom}|{tp}", dv_name=f"discrimination[{dom}]",
        dv_domain=dom, veracity="true_vs_false", timepoint=tp, metric=metric,
        effect=eff, effect_raw=eff, var=var, se=math.sqrt(var),
        n1=np.nan, n2=np.nan, n_total=n, r_used=np.nan,
        p_value=(float(2 * SC._st.norm.sf(abs(eff) / math.sqrt(var))) if SC._st is not None else np.nan),
        p_censored=0, data_available=1, contrast_type="", source="sdt_participant_covariate",
        fallback="", did_basis="", exclusions_embedded=0, cond_ambiguous=0, oriented=1,
        true_score=ts[0], true_var=ts[1], fake_score=fs[0], fake_var=fs[1],
        criterion_score=cs[0], criterion_var=cs[1], note=note,
        participant_cov=cov, participant_level=lvl, participant_kind=kind)


def main():
    SC.include_fully_within_cli()
    SC.announce_fully_within("SDT participant specs")
    rows = []
    cov_studies = collections.defaultdict(set)
    fw_skipped = []
    for folder in sorted(glob.glob(os.path.join(DS, "*"))):
        key = os.path.basename(folder)
        # Fully-within (single-arm, no-control) datasets: excluded by default.
        if not SC.include_fully_within() and SC.study_fully_within(key):
            fw_skipped.append(key)
            continue
        f = os.path.join(folder, key + " - harmonized.csv")
        vfile = os.path.join(folder, key + " - harmonized_variables.csv")
        if not (os.path.isfile(f) and os.path.isfile(vfile)):
            continue
        try:
            side = pd.read_csv(vfile, encoding="utf-8-sig", low_memory=False)
            wide = pd.read_csv(f, encoding="utf-8-sig", low_memory=False)
        except Exception:
            continue
        allcols = [str(c).strip().lstrip("\ufeff") for c in wide.columns]
        if "pid" not in wide.columns:
            wide = wide.assign(pid=wide.index)
        covs = _covariate_series(wide, side, allcols)
        if not covs:
            continue
        pidstr = wide["pid"].astype(str).to_numpy()
        cov_map = {}
        kinds = {}
        for b, series in covs.items():
            continuous = _numeric(series) is not None and b not in _CATEGORICAL_COVS
            lv = _levels(series, b, continuous=continuous)
            if len(lv) < 2:
                continue
            m = {}
            for label, mask in lv:
                for p in pd.Series(pidstr[mask]).unique():
                    m.setdefault(p, label)
            if len(set(m.values())) >= 2:
                cov_map[b] = m
                kinds[b] = "continuous" if continuous else "categorical"
                cov_studies[b].add(key)
        if not cov_map:
            continue
        for excl_on, tag in ((True, "on"), (False, "off")):
            try:
                c, _ = AMS.build_study(folder, excl_on, with_components=True)
            except Exception:
                continue
            if c is None or not len(c):
                continue
            c = c.copy()
            c["pid"] = c["pid"].astype(str)
            for b, mapping in cov_map.items():
                c["_lvl"] = c["pid"].map(mapping)
                for lvl in sorted({v for v in c["_lvl"] if isinstance(v, str)}):
                    sub = c[c["_lvl"] == lvl]
                    if not len(sub):
                        continue
                    for (art, dom, tp), g in sub.groupby(["artifact", "domain", "timepoint"], dropna=False):
                        cls = SC.classify_timepoint(tp)
                        spec = ("post_all" if cls in ("post", "post_assumed")
                                else "persistence_all" if cls == "followup" else None)
                        if spec is None:
                            continue
                        st = _stats(g["comp"].to_numpy(float))
                        if not st:
                            continue
                        n, eff, var = st
                        rows.append(_row(spec, tag, key, art, dom, tp, "d_sdt", n, eff, var,
                                         _mean_var(g["true_score"].to_numpy(float)),
                                         _mean_var(g["fake_score"].to_numpy(float)),
                                         _mean_var(g["criterion"].to_numpy(float)),
                                         b, lvl, kinds[b], f"{b}={lvl} (pooled over arms)"))
                    for dom, g in sub.groupby("domain", dropna=False):
                        cls = g["timepoint"].map(lambda t: SC.classify_timepoint(t))
                        pre = g[cls == "pre"]
                        post = g[cls.isin(("post", "post_assumed"))]
                        if not len(pre) or not len(post):
                            continue
                        j = post.merge(pre, on=["artifact", "pid", "arm"], suffixes=("_po", "_pr"))
                        for art, gg in j.groupby("artifact", dropna=False):
                            d = gg["comp_po"].to_numpy(float) - gg["comp_pr"].to_numpy(float)
                            r = gg["arm"].astype(str).str.lower()
                            ig = np.isfinite(d) & r.eq("intervention").to_numpy()
                            cg = np.isfinite(d) & r.isin(SC.COND_CONTROL_ROLES).to_numpy()
                            if ig.sum() < 2 or cg.sum() < 2:
                                continue
                            eff = float(np.nanmean(d[ig])) - float(np.nanmean(d[cg]))
                            var = (float(np.nanvar(d[ig], ddof=1) / ig.sum())
                                   + float(np.nanvar(d[cg], ddof=1) / cg.sum()))
                            if not (np.isfinite(eff) and np.isfinite(var) and var > 0):
                                continue

                            def cc(col):
                                a = gg[f"{col}_po"].to_numpy(float) - gg[f"{col}_pr"].to_numpy(float)
                                return (float(np.nanmean(a[ig]) - np.nanmean(a[cg])),
                                        float(np.nanvar(a[ig], ddof=1) / ig.sum()
                                              + np.nanvar(a[cg], ddof=1) / cg.sum()))
                            rows.append(_row("did_all", tag, key, art, dom, "post-pre",
                                             "d_sdt_did", int(ig.sum() + cg.sum()), eff, var,
                                             cc("true_score"), cc("fake_score"), cc("criterion"),
                                             b, lvl, kinds[b], f"{b}={lvl} (IG-CG delta)"))

    df = pd.DataFrame(rows)
    if not len(df):
        print("no participant-level rows")
        print(SC.fully_within_report(len(fw_skipped), "dataset(s)"))
        df.to_csv(OUT, index=False, encoding="utf-8-sig")
        return
    for src, dst in (("post_all", "post_cred"), ("did_all", "did_cred"),
                     ("persistence_all", "persistence_cred")):
        sub = df[df["spec"] == src].copy()
        if not len(sub):
            continue
        sub["fallback"] = ""
        cred = SC._cred_fallback(sub, src)
        cred["spec"] = dst
        df = pd.concat([df, cred], ignore_index=True)
    df = SC.derive_specs(df, "sdt")
    df.to_csv(OUT, index=False, encoding="utf-8-sig")
    print(SC.fully_within_report(len(fw_skipped), "dataset(s)"))
    print(f"wrote {OUT}: {len(df)} rows, {df['participant_cov'].nunique()} covariates, "
          f"{df['study_key'].nunique()} studies")


if __name__ == "__main__":
    main()
