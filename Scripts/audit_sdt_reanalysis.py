"""W6-SDT: independent audit of the SDT discrimination (d') re-analysis pipeline.

Verifies, end to end and from the raw harmonized files:

1. **Hand reproduction** -- for every ``Discrimination`` row (intervention effect)
   and ``Discrimination_level`` row, the melt + orientation reversal + arm split +
   per-participant d' is recomputed from ``<Key> - harmonized.csv`` +
   ``<Key> - harmonized_variables.csv`` (without calling
   ``reanalysis.collect_datasets``) and compared with the sheet.
2. **Polarity/veracity handling** -- per cell, the true- and false-veracity DVs
   that feed it and their ``dv_polarity``/``veracity``; counts of
   (a) cells whose false side contains a blank-polarity DV (correct-rejection
       side left un-reversed),
   (b) cells whose false side mixes reversed and un-reversed DVs,
   (c) cells fed by a difference/composite DV that still carries a veracity.
3. **Sensitivity** -- the posttest intervention effect pooled under
   (i) the sidecar polarity, (ii) a veracity fallback that reverses blank-polarity
   false-veracity endorsement DVs, (iii) no reversal at all, and (iv) the
   difference-score drop off/on.
4. **Scale/variance** -- that ``1 - x`` is applied only to ``_01`` columns, and
   the branch (per-participant vs aggregate delta-method) behind each cell's
   ``vi``, flagging item-expanded N for studies without a participant id.

Writes ``Results/sdt_reanalysis_audit.csv`` (one row per sheet cell) and prints a
summary.  This script only READS the datasets; it never writes a dataset.
"""
import glob
import math
import os
import re
import sys
from collections import Counter

import numpy as np
import pandas as pd
from scipy.stats import norm

_HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = _HERE
B = os.path.dirname(SCRIPTS)
RESULTS = os.path.join(B, "Results")
ANALYSIS = os.path.join(SCRIPTS, "analysis")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, ANALYSIS)

import reanalysis as RA            # noqa: E402  (helpers; main() is not called)
import suite_common as SC          # noqa: E402
import dv_polarity as DP           # noqa: E402
import domain_restriction as DR    # noqa: E402
import study_exclusions as SE      # noqa: E402

OUT_CSV = os.path.join(RESULTS, "sdt_reanalysis_audit.csv")
EPS = 1e-4
ENDORSEMENT_DOMAINS = {"sharing_intention", "engagement_behavior", "credibility"}
# strict difference / change-score marker (matches reanalysis._DIFF_SCORE_RE)
DIFF_RE = re.compile(r"(_diff|difference|minus|\bnet\b|\bgap\b|d_prime|dprime|signal_detection)",
                     re.I)


def _probit(p, n=None):
    a = np.asarray(p, float)
    if n is not None:
        nn = np.asarray(n, float)
        a = (a * nn + 0.5) / (nn + 1.0)
    return norm.ppf(np.clip(a, EPS, 1 - EPS))


def _dprime(hr, cr, n=None, n_fa=None):
    return _probit(hr, n=n) - _probit(1.0 - cr, n=n if n_fa is None else n_fa)


def cell_dprime(g):
    """Replicate reanalysis._cell_dprime; also report which branch produced vi."""
    ver = g["veracity"].astype(str).str.strip().str.lower()
    t = g.loc[ver == "true", "value_0_1"]
    fa = g.loc[ver == "false", "value_0_1"]
    if not (len(t) and len(fa)):
        return None
    dd = var = None
    n_pid = 0
    if "pid" in g.columns:
        tv = g.loc[ver == "true", ["pid", "value_0_1"]].dropna()
        fv = g.loc[ver == "false", ["pid", "value_0_1"]].dropna()
        if len(tv) and len(fv):
            tm = tv.groupby("pid")["value_0_1"].mean()
            fm = fv.groupby("pid")["value_0_1"].mean()
            nt = tv.groupby("pid").size()
            nf = fv.groupby("pid").size()
            common = tm.index.intersection(fm.index)
            if len(common) >= 2:
                dps = np.array([_dprime(tm[p], fm[p], n=int(nt.get(p, 1)),
                                        n_fa=int(nf.get(p, 1))) for p in common], float)
                dps = dps[np.isfinite(dps)]
                if len(dps) >= 2:
                    dd = float(np.mean(dps))
                    var = float(np.var(dps, ddof=1) / len(dps))
                    n_pid = len(dps)
    branch = "participant" if dd is not None else "aggregate"
    if dd is None:
        hr, cr = float(t.mean()), float(fa.mean())
        dd = _dprime(hr, cr, n=len(t), n_fa=len(fa))
        zhr, zfar = float(_probit(hr)), float(_probit(1.0 - cr))
        phi = lambda z: math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
        v_hr = float(t.var(ddof=1)) / max(len(t), 1)
        v_cr = float(fa.var(ddof=1)) / max(len(fa), 1)
        var = v_hr / (phi(zhr) ** 2) + v_cr / (phi(zfar) ** 2)
        n_pid = int(min(len(t), len(fa)))
    if np.isfinite(dd) and np.isfinite(var) and var > 0:
        return dict(d=dd, vi=var, n_true=len(t), n_false=len(fa), n_pid=n_pid,
                    branch=branch)
    return None


def _blank(pol):
    return str(pol).strip().lower() in ("", "nan", "none", "nat")


def _cell_dv_info(g):
    ver = g["veracity"].astype(str).str.strip().str.lower()
    info = {}
    for side in ("true", "false"):
        sub = g[ver == side]
        names = sorted(sub["dv"].astype(str).unique())
        pol_by_dv = {}
        for n in names:
            p = sub.loc[sub["dv"] == n, "polarity"].iloc[0]
            pol_by_dv[n] = "blank" if _blank(p) else str(p).strip().lower()
        info[f"{side}_dvs"] = ";".join(names)
        info[f"{side}_polarities"] = ";".join(f"{n}={pol_by_dv[n]}" for n in names)
        info[f"n_{side}_dvs"] = len(names)
        info[f"n_{side}_blank_pol"] = int(sum(1 for n in names if pol_by_dv[n] == "blank"))
        info[f"n_{side}_diff_dvs"] = int(sum(1 for n in names if DIFF_RE.search(n)))
    info["cat_a_false_blank_pol"] = int(info["n_false_dvs"] > 0
                                         and info["n_false_blank_pol"] > 0)
    fr = g[ver == "false"]
    if len(fr):
        pols = set(fr["polarity"].astype(str).str.strip().str.lower().map(
            lambda x: "blank" if _blank(x) else x))
        rev_set = pols & {"higher_is_worse", "lower_is_better", "reverse",
                          "reversed", "negative"}
        info["cat_b_false_mixed"] = int(bool(rev_set) and bool(pols - rev_set))
        info["cat_b_false_all_blank"] = int(pols == {"blank"})
    else:
        info["cat_b_false_mixed"] = 0
        info["cat_b_false_all_blank"] = 0
    info["cat_c_false_diff_veracity"] = int(info["n_false_diff_dvs"] > 0)
    info["cat_c_true_diff_veracity"] = int(info["n_true_diff_dvs"] > 0)
    return info


def build_cells(study_key, folder):
    """Replicate reanalysis.collect_datasets for one study (no collect_datasets call)."""
    meta = RA.meta_for(folder)
    f = os.path.join(folder, study_key + " - harmonized.csv")
    try:
        d = pd.read_csv(f, encoding="utf-8-sig", low_memory=False)
    except Exception:
        return [], []
    if "condition" not in d.columns and "condition_raw" in d.columns:
        d = d.rename(columns={"condition_raw": "condition"})
    dvcols = [c for c in d.columns if str(c).startswith("dv_") and str(c).endswith("_01")]
    dvcols = [c for c in dvcols
              if not (DR.is_non_outcome(meta.get(str(c)[:-3], ("", "", "", ""))[0])
                      or SC.is_metacognition(meta.get(str(c)[:-3], ("", "", "", ""))[0],
                                             str(c)[:-3]))]
    if not dvcols:
        return [], []
    idcols = [c for c in ("study_key", "artifact", "pid", "condition") if c in d.columns]
    flagcols = [c for c in d.columns if str(c).startswith("flag_")]
    idcols = idcols + [c for c in flagcols if c not in idcols]
    long = d.melt(id_vars=idcols, value_vars=dvcols, var_name="dv_01", value_name="value_0_1")
    long = long.dropna(subset=["value_0_1"])
    if not len(long):
        return [], []
    if "artifact" not in long.columns:
        long["artifact"] = ""
    long["dv"] = long["dv_01"].str[:-3]
    long["dv_domain"] = long["dv"].map(lambda v: meta.get(v, ("", "", "", ""))[0])
    long["veracity"] = long["dv"].map(lambda v: meta.get(v, ("", "", "", ""))[1])
    long["timepoint"] = long["dv"].map(lambda v: meta.get(v, ("", "", "", ""))[2])
    long["polarity"] = long["dv"].map(lambda v: meta.get(v, ("", "", "", ""))[3])
    # same defensive difference-score drop as reanalysis.collect_datasets
    if RA._DROP_MISLABELED_DIFF:
        _ver_tf = long["veracity"].astype(str).str.strip().str.lower().isin(["true", "false"])
        _diff = long["dv"].astype(str).map(lambda v: bool(DIFF_RE.search(v)))
        long = long[~(_ver_tf & _diff)]
    long = long[~long["dv_domain"].isin(DR.NON_OUTCOME_DOMAINS)]
    if not len(long):
        return [], []
    ex_labels = SE.excluded_labels(study_key)
    if ex_labels and "artifact" in long.columns:
        long = long[~long["artifact"].astype(str).isin(ex_labels)]
        if not len(long):
            return [], []
    rev = long["polarity"].astype(str).str.strip().str.lower().isin(
        ["higher_is_worse", "lower_is_better", "reverse", "reversed", "negative"])
    long = long.copy()
    long.loc[rev, "value_0_1"] = 1.0 - long.loc[rev, "value_0_1"]
    long = RA.apply_exclusions(long, RA.load_exclusions(folder), study=study_key)
    if "condition" in long.columns:
        cache = {}

        def role(c, a):
            kk = (str(a), str(c))
            if kk not in cache:
                try:
                    cache[kk] = SC.classify_condition(c, study_key, str(a), folder=folder)[0]
                except Exception:
                    cache[kk] = "unknown"
            return cache[kk]

        art = long["artifact"] if "artifact" in long.columns else pd.Series("", index=long.index)
        long["_role"] = [role(c, a) for c, a in zip(long["condition"], art)]
    else:
        long["_role"] = "missing"

    effect_cells, level_cells = [], []
    for (key, art, dom, tp), g in long.groupby(
            ["study_key", "artifact", "dv_domain", "timepoint"], dropna=False):
        info = _cell_dv_info(g)
        lvl = cell_dprime(g)
        if lvl is not None:
            level_cells.append(dict(study_key=key, artifact=art, dv_domain=dom,
                                    timepoint=tp, hand_d=lvl["d"], hand_vi=lvl["vi"],
                                    branch=lvl["branch"], **info))
        gi = g[g["_role"] == "intervention"]
        gc = g[g["_role"].isin(SC.COND_CONTROL_ROLES)]
        ri = cell_dprime(gi)
        rc = cell_dprime(gc)
        if ri is None or rc is None:
            continue
        effect_cells.append(dict(study_key=key, artifact=art, dv_domain=dom, timepoint=tp,
                                 hand_d=ri["d"] - rc["d"], hand_vi=ri["vi"] + rc["vi"],
                                 hand_n_treat=ri["n_pid"], hand_n_control=rc["n_pid"],
                                 branch=("participant" if ri["branch"] == rc["branch"] == "participant"
                                         else "aggregate"),
                                 **info))
    return effect_cells, level_cells


def _key(df):
    df = df.copy()
    for c in ("study_key", "artifact", "dv_domain"):
        df[c] = df[c].astype(str)

    def _tp(x):
        if pd.isna(x):
            return "<blank>"
        s = str(x).strip().lower()
        return "<blank>" if s in ("", "nan", "none", "nat") else str(x)

    df["_tp"] = df["timepoint"].map(_tp)
    return df


def compare(hand, sheet, kind):
    h, s = _key(hand), _key(sheet)
    merged = h.merge(s, on=["study_key", "artifact", "dv_domain", "_tp"],
                     how="outer", suffixes=("_hand", "_sheet"), indicator=True)
    both = merged[merged["_merge"] == "both"].copy()
    both["d_diff"] = (both["hand_d"] - both["d"]).abs()
    both["vi_rel"] = (both["hand_vi"] - both["vi"]).abs() / both["vi"].replace(0, np.nan)
    mism = both[(both["d_diff"] > 1e-6) | (both["vi_rel"] > 1e-6)]
    print(f"  {kind}: hand={len(h)} sheet={len(s)} matched={len(both)} "
          f"only_hand={int((merged['_merge'] == 'left_only').sum())} "
          f"only_sheet={int((merged['_merge'] == 'right_only').sum())} "
          f"value_mismatch={len(mism)}")
    if len(mism):
        print("    mismatch sample:")
        print(mism[["study_key", "dv_domain", "artifact", "_tp", "hand_d", "d",
                    "hand_vi", "vi"]].head(20).to_string())
    return both


def cat_counts(df, label):
    if not len(df):
        print(f"  {label}: empty")
        return
    print(f"  {label}: {len(df)} cells")
    for key, desc in (("cat_a_false_blank_pol",
                       "(a) false side has a blank-polarity DV"),
                      ("cat_b_false_mixed",
                       "(b) false side mixes reversed/un-reversed"),
                      ("cat_c_false_diff_veracity",
                       "(c) false side has a difference-score DV w/ veracity")):
        sel = df[df[key] == 1]
        print(f"    {desc:52s}: {len(sel)} cells ({sel['study_key'].nunique()} studies)")
        if len(sel):
            ex = sorted(sel["study_key"].unique())[:6]
            print("        e.g. " + ", ".join(ex))
    print(f"    false side all-blank polarity                        : "
          f"{int((df['cat_b_false_all_blank'] == 1).sum())} cells")


def pool_sens(mode, no_rev=False, fallback=False, drop_diff=True):
    RA._NO_REVERSAL = no_rev
    RA._VERACITY_FALLBACK = fallback
    RA._DROP_MISLABELED_DIFF = drop_diff
    RA._LOG_ACTIVE = False
    RA._FORCE = True
    d = RA.collect_datasets(no_excl=False, write_catalog=False)
    disc = RA._make_disc(d["disc_rows"])
    post = disc[disc["bucket"] == "post"] if len(disc) else disc
    r = SC.pool_dependence(post["d"], post["vi"], post["cluster_id"], post["study_key"])
    return dict(mode=mode, k=len(post), n_studies=post["study_key"].nunique(),
                dl=r["dl_est"], dl_lo=r["dl_lo"], dl_hi=r["dl_hi"],
                three=r["three_est"], three_lo=r["three_lo"], three_hi=r["three_hi"],
                rve=r["rve_est"], rve_lo=r["rve_lo"], rve_hi=r["rve_hi"])


def main():
    xl = os.path.join(RESULTS, "reanalysis.xlsx")
    disc_sheet = pd.read_excel(xl, sheet_name="Discrimination")
    lvl_sheet = pd.read_excel(xl, sheet_name="Discrimination_level")

    all_effect, all_level, no_pid = [], [], []
    for folder in sorted(glob.glob(os.path.join(B, "Datasets", "*"))):
        key = os.path.basename(folder)
        if not os.path.isfile(os.path.join(folder, key + " - harmonized.csv")):
            continue
        if SE.is_fulltext_excluded(key):
            continue
        if not SC.include_fully_within() and SC.study_fully_within(key):
            continue
        e, l = build_cells(key, folder)
        if not e and not l:
            continue
        try:
            cols = pd.read_csv(os.path.join(folder, key + " - harmonized.csv"),
                               encoding="utf-8-sig", nrows=0).columns
            if "pid" not in cols:
                no_pid.append(key)
        except Exception:
            pass
        all_effect += e
        all_level += l
    eff = pd.DataFrame(all_effect)
    lvl = pd.DataFrame(all_level)

    print("HAND REPRODUCTION vs sheet")
    compare(eff, disc_sheet, "Discrimination (intervention effect)")
    compare(lvl, lvl_sheet, "Discrimination_level (arm-pooled)")

    print("\nPOLARITY/VERACITY CATEGORIES")
    cat_counts(eff, "Discrimination")
    cat_counts(lvl, "Discrimination_level")

    print("\nSENSITIVITY (posttest intervention effect)")
    sens = []
    for mode, no_rev, fb, dd in (
            ("sidecar (current, diff-drop ON)", False, False, True),
            ("diff-drop OFF (pre-fix)", False, False, False),
            ("veracity fallback", False, True, True),
            ("naive (no reversal)", True, False, True)):
        r = pool_sens(mode, no_rev, fb, dd)
        sens.append(r)
        print(f"  {mode:34s} DL={r['dl']:.6f} [{r['dl_lo']:.6f},{r['dl_hi']:.6f}] "
              f"| 3-level={r['three']:.6f} | RVE={r['rve']:.6f} | k={r['k']} "
              f"studies={r['n_studies']}")

    print("\nVARIANCE CHECK")
    print("  reversal applied only to `_01` (0-1) columns: yes "
          "(melt is restricted to dv_*_01; reanalysis.collect_datasets L516).")
    if len(eff):
        br = eff["branch"].value_counts().to_dict()
        print(f"  effect cells by vi branch: {br}")
        agg = eff[eff["branch"] == "aggregate"]
        print(f"    aggregate-branch cells: {len(agg)} ({agg['study_key'].nunique()} studies)")
        print(f"    of which studies without a pid column: "
              f"{len(set(agg['study_key']) & set(no_pid))} studies "
              f"({', '.join(sorted(set(agg['study_key']) & set(no_pid))[:6])})")

    # ---- write CSV -------------------------------------------------------------
    out = _key(eff).merge(
        _key(disc_sheet).rename(columns={"d": "sheet_d", "vi": "sheet_vi",
                                         "n_true": "sheet_n_true", "n_false": "sheet_n_false",
                                         "n_treat": "sheet_n_treat",
                                         "n_control": "sheet_n_control"})[
            ["study_key", "artifact", "dv_domain", "_tp", "sheet_d", "sheet_vi",
             "sheet_n_true", "sheet_n_false", "sheet_n_treat", "sheet_n_control"]],
        on=["study_key", "artifact", "dv_domain", "_tp"], how="left")
    out["d_agree"] = (out["hand_d"] - out["sheet_d"]).abs() < 1e-6
    out["vi_agree"] = (out["hand_vi"] - out["sheet_vi"]).abs() < 1e-9
    out = out.drop(columns=["_tp"])
    out.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(f"\nwrote {OUT_CSV} ({len(out)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
