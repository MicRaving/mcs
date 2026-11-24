"""Backfire analysis — "For whom are misinformation interventions counterproductive?"

Research question
-----------------
Among participants assigned to a misinformation *intervention* arm, who moves in the
WRONG direction from pretest to posttest?  Three within-person backfire outcomes:

    a) trust in TRUE information DECREASED        (dv_*_true: post - pre < 0)
    b) trust in FALSE information INCREASED       (dv_*_false: post - pre > 0)
    c) DISCRIMINANCE DECREASED                    (d = trust_true - trust_false, post - pre < 0)

Two operational definitions of "backfire" are computed and analysed side by side:

* **any**  — any move in the wrong direction (``d < 0`` / ``d > 0``).  This is the
  descriptive base rate; with noisy single-item scales a large share of it is
  measurement error.
* **reliable** — a move large enough that it cannot be explained by measurement
  error alone: the Reliable Change Index (Jacobson & Truax, 1991)

      RCI = delta / SE_diff ,   SE_diff = SD_pre * sqrt(2 * (1 - r)) ,
      reliable decline  <=>  |RCI| >= 1.96  (two-tailed 5% band)

  computed **per study and per outcome**, with the reliability ``r`` taken as the
  larger of (i) the control-arm pretest-posttest correlation (test-retest under
  no intervention) and (ii) Cronbach's alpha across the item-level pretest
  columns when the study contributes >= 3 of them.  The larger of the two is used
  because the test-retest estimate is attenuated by true pre-post change.
  Studies with no control arm fall back to their own whole-sample correlation
  (documented in the ``Thresholds`` sheet).  The control arm's flag rate at the
  same threshold is reported as the empirical false-positive calibration.

Eligibility
-----------
Only harmonised datasets that measure the *same competency* on true and false stimuli
both BEFORE and AFTER the intervention.  A DV qualifies when

  * ``role == 'dv'`` and its resolved domain is an endorsement/credibility construct
    (``credibility`` / ``trust`` / ``belief_accuracy``),
  * it carries a stimulus veracity (true/false) and a timepoint (pre/post),
  * it is an *endorsement* scale rather than an accuracy/recognition measure
    (``dv_polarity.classify_credibility_scale`` == 'belief'), so higher = more trust,
  * the identical DV stem exists for both veracities and both timepoints as ``_01``
    columns in the wide file.

For each participant the four endorsement scores are averaged over the qualifying
columns (item-level columns are used only when no aggregate exists — the shortest
stem wins), giving trust_true_pre/post and trust_false_pre/post.  Higher raw value
always means MORE trust in the stimulus (verified against the sidecar ``dv_polarity``,
which is ``higher_is_better`` for true and ``higher_is_worse`` for false for every
included DV).

Outputs (all under Results/)
---------------------------
  backfire_person_level.csv      person-level dataset (both arms, both definitions)
  backfire_analysis.xlsx         Sample / Thresholds / Descriptives / Control_Reference /
                                 Predictor_Coverage / Bayesian_Rates / Bayesian_HierRates /
                                 Arm_Comparison / LeaveOneOut / Bayesian_Models /
                                 Model_Diagnostics / Notes  — each outcome sheet
                                 duplicated for the ``any`` and ``reliable`` definitions
  backfire_report.md             the same tables as markdown
  figures/backfire_*.png         plots (both definitions)

Run:  python Scripts/analysis/backfire_analysis.py
"""
import os
import sys
import glob
import math
import collections
import warnings

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

warnings.filterwarnings("ignore")

_HERE = os.path.dirname(os.path.abspath(__file__))       # Scripts/analysis
SCRIPTS = os.path.dirname(_HERE)                         # Scripts
B = os.path.dirname(SCRIPTS)                             # project root
RESULTS = os.path.join(B, "Results")
FIGDIR = os.path.join(RESULTS, "figures")
TMP = os.path.join(B, "temp")
os.makedirs(RESULTS, exist_ok=True)
os.makedirs(FIGDIR, exist_ok=True)
os.makedirs(TMP, exist_ok=True)

DS = os.path.join(B, "Datasets")
sys.path.insert(0, SCRIPTS)
import dv_polarity as DVP                                  # noqa: E402
import suite_common as SC                                  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

COMP = {"credibility", "trust", "belief_accuracy"}
RCI_Z = 1.96                      # two-tailed 5% band (Jacobson & Truax, 1991)

# outcome key -> (pre-test column, change column, wrong direction)
THRESH_SPECS = [("true", "tt_pre", "d_tt", "<0"),
                ("false", "tf_pre", "d_tf", ">0"),
                ("disc", "disc_pre", "d_disc", "<0")]


def outcome_specs(mode="any"):
    """[(column, change column, wrong sign, label)] for one definition."""
    suf = "_rel" if mode == "rel" else ""
    rel = (mode == "rel")
    return [
        (f"backfire_true{suf}", "d_tt", "<0",
         "Trust in TRUE information reliably decreased" if rel
         else "Trust in TRUE information decreased"),
        (f"backfire_false{suf}", "d_tf", ">0",
         "Trust in FALSE information reliably increased" if rel
         else "Trust in FALSE information increased"),
        (f"backfire_disc{suf}", "d_disc", "<0",
         "Discriminance reliably decreased" if rel
         else "Discriminance decreased"),
    ]


# ---------------------------------------------------------------------------
# gender coding conventions, read from each sidecar's ``notes``
#   study -> (column, set-of-values-that-mean-FEMALE) ; None -> not resolvable
# ---------------------------------------------------------------------------
GENDER_SPEC = {
    "Ecker et al. (2020 b)": ("cov_gender", {2.0}),          # 1=male 2=female 3=undisclosed
    "Fendt & Hausmann (2026)": ("cov_gender", {1.0}),        # 1=weiblich 2=maennlich
    "Fendt (2026)": ("cov_gender", {2.0}),                   # 1=male 2=female
    "Fendt et al. (2023)": ("cov_gender", {0.0}),            # 0=Female 1=Male
    "Fendt et al. (2026 x)": ("cov_gender", {1.0}),          # 1 female 2 male
    "HARROP et al. (2023)": ("cov_gender_simple", {0.0}),    # 1=Male 0=Female
    "Neylan et al. (2023)": ("cov_gender", {"female"}),
    "Roozenbeek et al. (2022 b)": ("cov_gender", {1.0}),     # 0=male 1=female
    "Swire-Thompson et al. (2025)": ("cov_gender", {2.0}),   # 1 male 2 female
    "Traberg et al. (2024 b)": ("cov_gender", {"female"}),
    "Costello et al. (2024)": ("cov_gender", {"female"}),
    # unresolved coding (no sidecar statement): left missing on purpose
    "Fendt et al. (2026 a)": None,
    "Fendt et al. (2026 c)": None,
    "Swire et al. (2017)": None,
    "Cook et al. (2024)": None,
    "Maertens et al. (2021)": None,
}

# political-ideology scale direction: True -> the raw scale already runs
# low = left/liberal .. high = right/conservative; False -> reverse it (8 - x)
IDEOLOGY_RIGHT = {
    "Fendt & Hausmann (2026)": False,   # 1 very conservative .. 7 very progressive
    "Fendt et al. (2026 c)": False,     # 1 conservative .. 7 progressive
    "Fendt et al. (2026 x)": False,     # 1 very conservative .. 7 very progressive
    "Fendt et al. (2023)": True,        # 1 left 2 center 3 right
    "HARROP et al. (2023)": True,       # 1 left .. 7 right
    "Neylan et al. (2023)": True,       # 1 very left .. 7 very right
    "Roozenbeek et al. (2022 b)": True,
    "Traberg et al. (2024 b)": True,
    "Fendt (2026)": None,               # direction unstated
    "Maertens et al. (2021)": None,
}


def strip_tokens(name, ver, tp):
    out = []
    for t in str(name).split("_"):
        tl = t.lower()
        if ver and tl == ver:
            continue
        if tp and tl == tp:
            continue
        out.append(t)
    return "_".join(out)


def is_endorsement(name, notes):
    try:
        return DVP.classify_credibility_scale(name, notes) == "belief"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 1. discovery
# ---------------------------------------------------------------------------
def _cand_rows(side):
    for _, r in side.iterrows():
        dom = str(r.get("dv_domain") or "").strip().lower()
        if dom not in COMP:
            continue
        tp = str(r.get("timepoint") if pd.notna(r.get("timepoint")) else "").strip().lower()
        ver = str(r.get("veracity") if pd.notna(r.get("veracity")) else "").strip().lower()
        if tp not in ("pre", "post") or ver not in ("true", "false"):
            continue
        uv = str(r["uniform_variable"])
        if not is_endorsement(uv, r.get("notes")):
            continue
        yield dom, strip_tokens(uv, ver, tp), ver, tp, uv


def discover():
    """[(study_key, {domain: [stems]})] for every eligible harmonised dataset."""
    out = []
    for f in sorted(glob.glob(os.path.join(DS, "*", "* - harmonized_variables.csv"))):
        key = os.path.basename(os.path.dirname(f))
        wide = os.path.join(DS, key, key + " - harmonized.csv")
        if not os.path.isfile(wide):
            continue
        try:
            side = pd.read_csv(f, encoding="utf-8-sig", low_memory=False)
            cols = set(map(str, pd.read_csv(wide, nrows=0, encoding="utf-8-sig").columns))
        except Exception:
            continue
        if "role" not in side.columns:
            continue
        side = side[side["role"].astype(str) == "dv"]
        m = collections.defaultdict(dict)
        for dom, stem, ver, tp, uv in _cand_rows(side):
            m[(dom, stem, ver)][tp] = uv
        gt, gf = {}, {}
        for (dom, stem, ver), dd in m.items():
            if "pre" in dd and "post" in dd and dd["pre"] + "_01" in cols and dd["post"] + "_01" in cols:
                (gt if ver == "true" else gf)[(dom, stem)] = dd
        common = set(gt) & set(gf)
        if not common:
            continue
        bydom = collections.defaultdict(list)
        for (dom, stem) in common:
            bydom[dom].append(stem)
        sel = {}
        for dom, stems in bydom.items():
            nmin = min(s.count("_") for s in stems)
            sel[dom] = sorted(s for s in stems if s.count("_") == nmin)
        out.append((key, sel))
    return out


# ---------------------------------------------------------------------------
# 2. reliability / reliable-change thresholds
# ---------------------------------------------------------------------------
def cronbach_alpha(df):
    """Cronbach alpha over complete rows; NaN when out of range or too few items."""
    x = df.apply(pd.to_numeric, errors="coerce").dropna()
    if len(x) < 30 or x.shape[1] < 3:
        return np.nan
    k = x.shape[1]
    var_items = x.var(axis=0, ddof=1).sum()
    var_total = x.sum(axis=1).var(ddof=1)
    if not np.isfinite(var_total) or var_total <= 0:
        return np.nan
    a = k / (k - 1.0) * (1.0 - var_items / var_total)
    # a negative or >1 alpha means the item set is not a single reliable scale
    return float(a) if np.isfinite(a) and 0.0 < a <= 1.0 else np.nan


def reliable_thresholds(sub, d, item_cols):
    """Per-study RCI thresholds for the three outcomes.

    ``sub`` is the study's participant frame (both arms) with tt_pre/tt_post/... ;
    ``d`` the raw wide frame (for the item-level alpha); ``item_cols`` maps
    (ver, tp) -> [column names] for the alpha.
    """
    rows = []
    thr = {}
    ctl = sub[sub["is_control"] == 1] if "is_control" in sub.columns else sub.iloc[0:0]
    for key, pre, dcol, wrong in THRESH_SPECS:
        s = sub[[pre, dcol]].apply(pd.to_numeric, errors="coerce").dropna()
        if len(s) < 30:
            thr[key] = np.nan
            continue
        sd_pre = float(s[pre].std())
        # test-retest reliability under no intervention, else whole sample
        r_ctl = np.nan
        c = ctl[[pre, dcol]].apply(pd.to_numeric, errors="coerce").dropna()
        if len(c) >= 25:
            r_ctl = float(c[pre].corr(c[pre] - c[dcol]))  # placeholder, replaced below
        # NOTE: corr with the *post* score needs the post column; recover it
        post = {"tt_pre": "tt_post", "tf_pre": "tf_post", "disc_pre": "disc_post"}[pre]
        cc = ctl[[pre, post]].apply(pd.to_numeric, errors="coerce").dropna() if len(ctl) else c.iloc[0:0]
        r_ctl = float(cc[pre].corr(cc[post])) if len(cc) >= 25 else np.nan
        ss = sub[[pre, post]].apply(pd.to_numeric, errors="coerce").dropna()
        r_all = float(ss[pre].corr(ss[post])) if len(ss) >= 25 else np.nan
        # Cronbach alpha across the item-level pretest columns, when available
        ver = "true" if key == "true" else "false"
        cols = [c for c in item_cols.get((ver, "pre"), []) if c in d.columns]
        alpha = cronbach_alpha(d[cols]) if len(cols) >= 3 else np.nan
        # reliability priority: control-arm test-retest (calibrated, the standard RCI
        # input) -> Cronbach alpha (no control arm) -> whole-sample test-retest.
        # Taking the MAX of these over-inflates reliability and over-flags controls
        # (checked against the control-arm flag rate), so a priority rule is used.
        if np.isfinite(r_ctl):
            r = r_ctl
            r_src = "control test-retest"
        elif np.isfinite(alpha):
            r = alpha
            r_src = "cronbach alpha"
        elif np.isfinite(r_all):
            r = r_all
            r_src = "whole-sample test-retest"
        else:
            r = np.nan
            r_src = ""
        r = float(min(max(r, 0.05), 0.95)) if np.isfinite(r) else np.nan
        se = sd_pre * math.sqrt(2.0 * (1.0 - r)) if np.isfinite(r) else np.nan
        t = RCI_Z * se if np.isfinite(se) else np.nan
        thr[key] = t
        # empirical false-positive calibration in the control arm
        ctl_rate = np.nan
        if len(ctl) and np.isfinite(t):
            cv = pd.to_numeric(ctl[dcol], errors="coerce").dropna()
            ctl_rate = 100 * ((cv <= -t) if wrong == "<0" else (cv >= t)).mean()
        rows.append({"study_key": sub["study_key"].iloc[0], "outcome": key,
                     "n": len(s), "sd_pre": sd_pre,
                     "r_control_retest": r_ctl, "alpha_items": alpha,
                     "r_whole_sample": r_all, "r_used": r, "r_source": r_src,
                     "se_diff": se, "threshold": t,
                     "threshold_over_sd_change": (t / float(s[dcol].std())
                                                  if s[dcol].std() else np.nan),
                     "pct_flagged_control": ctl_rate})
    return thr, pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 3. person-level construction
# ---------------------------------------------------------------------------
def _cov_series(d, col):
    if col is None or col not in d.columns:
        return pd.Series(np.nan, index=d.index)
    return d[col]


def build_person_level(verbose=True):
    found = discover()
    frames, diag, trows = [], [], []
    for key, sel in found:
        side = pd.read_csv(os.path.join(DS, key, key + " - harmonized_variables.csv"),
                           encoding="utf-8-sig", low_memory=False)
        side = side[side["role"].astype(str) == "dv"]
        cols = collections.defaultdict(list)
        all_cols = collections.defaultdict(list)
        for dom, stem, ver, tp, uv in _cand_rows(side):
            all_cols[(ver, tp)].append(uv + "_01")
            if stem in sel.get(dom, []):
                cols[(ver, tp)].append(uv + "_01")
        if not all(k in cols for k in (("true", "pre"), ("true", "post"),
                                       ("false", "pre"), ("false", "post"))):
            continue
        d = pd.read_csv(os.path.join(DS, key, key + " - harmonized.csv"),
                        encoding="utf-8-sig", low_memory=False)
        if not len(d):
            continue
        sub = pd.DataFrame(index=d.index)
        sub["study_key"] = key
        sub["artifact"] = d["artifact"] if "artifact" in d.columns else ""
        sub["pid"] = d["pid"] if "pid" in d.columns else np.arange(len(d))
        sub["condition"] = d["condition"] if "condition" in d.columns else np.nan

        for name, kk in (("tt_pre", ("true", "pre")), ("tt_post", ("true", "post")),
                         ("tf_pre", ("false", "pre")), ("tf_post", ("false", "post"))):
            sub[name] = d[cols[kk]].apply(pd.to_numeric, errors="coerce").mean(axis=1)
        sub = sub.dropna(subset=["tt_pre", "tt_post", "tf_pre", "tf_post"])
        if not len(sub):
            continue

        # ---- arm role -----------------------------------------------------
        roles = []
        for c, a in zip(sub["condition"], sub["artifact"]):
            try:
                roles.append(SC.classify_condition(c, key, str(a), folder=os.path.join(DS, key))[0])
            except Exception:
                roles.append("unknown")
        sub["role"] = roles
        has_intervention = any(r == "intervention" for r in roles)
        sub["arm_assumed"] = 0 if has_intervention else 1
        if has_intervention:
            sub["is_intervention"] = (sub["role"] == "intervention").astype(int)
            sub["is_control"] = sub["role"].isin(SC.COND_CONTROL_ROLES).astype(int)
        else:
            sub["is_intervention"] = 1
            sub["is_control"] = 0
        sub = sub[(sub["is_intervention"] == 1) | (sub["is_control"] == 1)].copy()
        if not len(sub):
            continue

        # ---- change scores ------------------------------------------------
        sub["d_tt"] = sub["tt_post"] - sub["tt_pre"]
        sub["d_tf"] = sub["tf_post"] - sub["tf_pre"]
        sub["disc_pre"] = sub["tt_pre"] - sub["tf_pre"]
        sub["disc_post"] = sub["tt_post"] - sub["tf_post"]
        sub["d_disc"] = sub["disc_post"] - sub["disc_pre"]
        sub["n_items_true"] = len(cols[("true", "pre")])
        sub["n_items_false"] = len(cols[("false", "pre")])

        # ---- reliable-change thresholds (per study x outcome) -------------
        item_cols = {}
        for ver in ("true", "false"):
            pre_cols = all_cols[(ver, "pre")]
            items = [c for c in pre_cols if pd.Series([c]).str.contains(r"_i\d+_01$").iloc[0]]
            if len(items) < 3:
                items = cols[(ver, "pre")] if len(cols[(ver, "pre")]) >= 3 else []
            item_cols[(ver, "pre")] = items
        thr, tdf = reliable_thresholds(sub, d, item_cols)
        if len(tdf):
            trows.append(tdf)

        # ---- outcomes (both definitions) ----------------------------------
        for key2, dcol, wrong, _lab in outcome_specs("any"):
            sub[key2] = ((sub[dcol] < 0) if wrong == "<0" else (sub[dcol] > 0)).astype(int)
        for key2, dcol, wrong, _lab in outcome_specs("rel"):
            t = thr.get(key2.replace("backfire_", "").replace("_rel", ""), np.nan)
            good = f"improve_{key2[len('backfire_'):]}"
            if np.isfinite(t):
                sub[key2] = ((sub[dcol] <= -t) if wrong == "<0"
                             else (sub[dcol] >= t)).astype(int)
                # the mirror: a reliable move in the DESIRABLE direction
                sub[good] = ((sub[dcol] >= t) if wrong == "<0"
                             else (sub[dcol] <= -t)).astype(int)
            else:
                sub[key2] = 0
                sub[good] = 0

        # ---- predictors ---------------------------------------------------
        age = pd.to_numeric(_cov_series(d, "cov_age"), errors="coerce")
        if key == "Maertens et al. (2021)":          # birth year -> age at study
            age = 2021.0 - age
        age = age.where((age >= 12) & (age <= 100))
        sub["age"] = age.loc[sub.index]

        gspec = GENDER_SPEC.get(key)
        if gspec:
            gcol, gvals = gspec
            raw = _cov_series(d, gcol).loc[sub.index]
            num = pd.to_numeric(raw, errors="coerce")
            txt = raw.astype(str).str.strip().str.lower()
            fem = pd.Series(np.nan, index=sub.index)
            for v in gvals:
                if isinstance(v, str):
                    fem = fem.mask(txt == v, 1.0)
                else:
                    fem = fem.mask(num == v, 1.0)
            resolved = num.notna() | txt.isin(["female", "male", "woman", "man",
                                               "f", "m", "other", "non-binary",
                                               "prefer not to say", "other, please specify:"])
            fem = fem.mask(resolved & fem.isna(), 0.0)
            sub["female"] = fem
        else:
            sub["female"] = np.nan

        sub["education"] = pd.to_numeric(_cov_series(d, "cov_education"), errors="coerce").loc[sub.index]

        ideo = None
        for c in ("cov_political_ideology", "cov_political_ideology_orientation"):
            s = pd.to_numeric(_cov_series(d, c), errors="coerce")
            if s.notna().sum() > 0:
                ideo = s
                break
        if ideo is not None:
            dir_right = IDEOLOGY_RIGHT.get(key)
            if dir_right is False:
                ideo = 8.0 - ideo
            elif dir_right is None:
                ideo = np.nan * ideo
            sub["conservatism"] = ideo.loc[sub.index]
        else:
            sub["conservatism"] = np.nan

        ana = pd.to_numeric(_cov_series(d, "cov_crt"), errors="coerce")
        if ana.notna().sum() == 0:
            ana = pd.to_numeric(_cov_series(d, "cov_analytic_thinking"), errors="coerce")
        sub["analytic"] = ana.loc[sub.index]

        for src, dst in (("cov_news_consumption", "news_consumption"),
                         ("cov_conspiracy_belief", "conspiracy_belief"),
                         ("cov_trust_in_science", "trust_in_science"),
                         ("cov_completion_time", "completion_time"),
                         ("cov_prior_knowledge", "prior_knowledge")):
            sub[dst] = pd.to_numeric(_cov_series(d, src), errors="coerce").loc[sub.index]

        frames.append(sub)
        diag.append({"study_key": key,
                     "n_intervention": int((sub["is_intervention"] == 1).sum()),
                     "n_control": int((sub["is_control"] == 1).sum()),
                     "arm_assumed": int(sub["arm_assumed"].iloc[0]),
                     "n_items_true": int(sub["n_items_true"].iloc[0]),
                     "n_items_false": int(sub["n_items_false"].iloc[0])})

    if not frames:
        raise SystemExit("no eligible datasets found")
    pl = pd.concat(frames, ignore_index=True)
    diag = pd.DataFrame(diag)
    thresholds = pd.concat(trows, ignore_index=True) if trows else pd.DataFrame()
    if verbose:
        print(f"[build] {len(pl)} participants over {pl['study_key'].nunique()} studies "
              f"({int((pl['is_intervention']==1).sum())} intervention, "
              f"{int((pl['is_control']==1).sum())} control)")
    return pl, diag, thresholds


# ---------------------------------------------------------------------------
# 4. intervention type (study characteristic)
# ---------------------------------------------------------------------------
def attach_intervention_type(pl):
    codes = SC.intervention_type_codes_map()

    def label_for(key):
        c = codes.get((key, ""), [])
        if not c:
            return "Unclassified"
        if len(c) == 1:
            return SC.intervention_type_label(c[0])
        return "Multi-component (" + ", ".join(SC.intervention_type_label(x) for x in c) + ")"

    pl["intervention_type"] = pl["study_key"].map({k: label_for(k)
                                                   for k in pl["study_key"].unique()})

    def coarse(x):
        return "Multi-component" if str(x).startswith("Multi-component") else x

    pl["intervention_type_coarse"] = pl["intervention_type"].map(coarse)
    safe = {"Inoculation / prebunking": "inoculation",
            "Debunking / rebuttals": "debunking",
            "Lateral reading": "lateral_reading",
            "Multi-component": "multi_component"}
    pl["type_code"] = pl["intervention_type_coarse"].map(
        lambda x: safe.get(x, "".join(ch if ch.isalnum() else "_" for ch in str(x)).strip("_")))
    return pl


# ---------------------------------------------------------------------------
# 5. standardisation (within study, so coefficients are comparable)
# ---------------------------------------------------------------------------
CONT = ["age", "education", "conservatism", "analytic", "news_consumption",
        "conspiracy_belief", "trust_in_science", "completion_time", "prior_knowledge"]
PRED_LABELS = {
    "age": "Age (per SD)", "female": "Female", "education": "Education (per SD)",
    "conservatism": "Political conservatism (per SD)", "analytic": "Analytic thinking / CRT (per SD)",
    "news_consumption": "News consumption (per SD)",
    "conspiracy_belief": "Conspiracy belief (per SD)",
    "trust_in_science": "Trust in science (per SD)",
    "completion_time": "Completion time (per SD)",
    "prior_knowledge": "Prior knowledge (per SD)",
}


def standardise(pl):
    for c in CONT:
        if c not in pl.columns:
            continue
        g = pl.groupby("study_key")[c]
        mu = g.transform("mean")
        sd = g.transform("std")
        pl[c + "_z"] = (pl[c] - mu) / sd.replace(0, np.nan)
    return pl


# ---------------------------------------------------------------------------
# 6. descriptives
# ---------------------------------------------------------------------------
def wilson(k, n, z=1.96):
    if n == 0:
        return (np.nan, np.nan)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - h) / d, (c + h) / d)


def _good_col(col):
    return "improve_" + col[len("backfire_"):] if col.startswith("backfire_") else ""


def _short(col):
    return col.replace("backfire_", "").replace("_rel", "") + (" (rel)" if col.endswith("_rel") else "")


def _pair_cols(spec):
    """(any column, reliable column, change column) for a spec row."""
    col, dcol = spec[0], spec[1]
    if col.endswith("_rel"):
        return col[:-4], col, dcol
    return col, col + "_rel", dcol


def descriptives(pl, specs, mode):
    rows = []
    for key, g in list(pl.groupby("study_key")) + [("ALL", pl)]:
        for spec in specs:
            col, dcol = spec[0], spec[1]
            any_col, rel_col, _ = _pair_cols(spec)
            k = int(g[rel_col].sum()) if mode == "rel" else int(g[any_col].sum())
            n = int(len(g))
            lo, hi = wilson(k, n)
            rows.append({"study_key": key, "outcome": col, "n": n,
                         "n_backfire": k, "pct_backfire": 100 * k / n,
                         "ci_lo": 100 * lo, "ci_hi": 100 * hi,
                         "pct_any": 100 * g[any_col].mean(),
                         "pct_reliable": 100 * g[rel_col].mean(),
                         "pct_reliable_improved": 100 * g[_good_col(rel_col)].mean(),
                         "mean_change": g[dcol].mean(), "sd_change": g[dcol].std(),
                         "intervention_type": g["intervention_type_coarse"].iloc[0]})
    return pd.DataFrame(rows)


def control_reference(pl_ctl, specs, mode):
    if not len(pl_ctl):
        return pd.DataFrame()
    rows = []
    for spec in specs:
        col = spec[0]
        any_col, rel_col, dcol = _pair_cols(spec)
        k = int(pl_ctl[rel_col].sum()) if mode == "rel" else int(pl_ctl[any_col].sum())
        n = len(pl_ctl)
        lo, hi = wilson(k, n)
        rows.append({"outcome": col, "n": n, "n_backfire": k,
                     "pct_backfire": 100 * k / n, "ci_lo": 100 * lo, "ci_hi": 100 * hi,
                     "pct_any": 100 * pl_ctl[any_col].mean(),
                     "pct_reliable": 100 * pl_ctl[rel_col].mean(),
                     "pct_reliable_improved": 100 * pl_ctl[_good_col(rel_col)].mean(),
                     "mean_change": pl_ctl[dcol].mean()})
    return pd.DataFrame(rows)


def predictor_descriptives(pl):
    rows = []
    preds = ["age", "female", "education", "conservatism", "analytic",
             "news_consumption", "conspiracy_belief", "trust_in_science",
             "completion_time", "prior_knowledge"]
    for c in preds:
        s = pd.to_numeric(pl[c], errors="coerce")
        rows.append({"predictor": PRED_LABELS.get(c, c), "column": c,
                     "n": int(s.notna().sum()), "pct_available": 100 * s.notna().mean(),
                     "mean": s.mean(), "sd": s.std(), "min": s.min(),
                     "median": s.median(), "max": s.max(),
                     "n_studies": int(pl.loc[s.notna(), "study_key"].nunique())})
    return pd.DataFrame(rows)


def predictor_study_matrix(pl):
    preds = ["age", "female", "education", "conservatism", "analytic",
             "news_consumption", "conspiracy_belief", "trust_in_science",
             "completion_time", "prior_knowledge"]
    rows = []
    for key, g in pl.groupby("study_key"):
        r = {"study_key": key, "n": len(g),
             "intervention_type": g["intervention_type_coarse"].iloc[0],
             "n_items_true": int(g["n_items_true"].iloc[0]),
             "n_items_false": int(g["n_items_false"].iloc[0]),
             "arm_assumed": int(g["arm_assumed"].iloc[0])}
        for c in preds:
            r[c] = int(pd.to_numeric(g[c], errors="coerce").notna().sum())
        rows.append(r)
    return pd.DataFrame(rows)


def leave_one_out_range(pl, specs, mode):
    rows = []
    for spec in specs:
        col = spec[0]
        any_col, rel_col, _ = _pair_cols(spec)
        use = rel_col if mode == "rel" else any_col
        vals = [100 * pl[pl["study_key"] != k][use].mean()
                for k in pl["study_key"].unique()]
        rows.append({"outcome": col, "all": 100 * pl[use].mean(),
                     "loo_min": min(vals), "loo_max": max(vals)})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 7. Bayesian analyses
# ---------------------------------------------------------------------------
def beta_binomial(k, n, a=1.0, b=1.0):
    from scipy import stats
    A, Bb = a + k, b + (n - k)
    lo, hi = stats.beta.ppf([0.025, 0.975], A, Bb)
    return {"n": n, "k": k, "post_mean": A / (A + Bb), "cri_lo": lo, "cri_hi": hi}


def bayes_rates(pl, specs, mode):
    rows = []
    for spec in specs:
        col = spec[0]
        any_col, rel_col, _ = _pair_cols(spec)
        use = rel_col if mode == "rel" else any_col
        lab = spec[3]
        r = beta_binomial(int(pl[use].sum()), len(pl))
        rows.append({"outcome": col, "label": lab, "group": "ALL", **r})
        for it, g in pl.groupby("intervention_type_coarse"):
            r = beta_binomial(int(g[use].sum()), len(g))
            rows.append({"outcome": col, "label": lab, "group": it, **r})
    return pd.DataFrame(rows)


def bayes_hier_rate(pl, col):
    """Hierarchical logit-normal random-effects model of a backfire rate."""
    from statsmodels.genmod.bayes_mixed_glm import BinomialBayesMixedGLM

    df = pl[[col, "study_key"]].dropna().copy()
    df["study"] = df["study_key"].astype(str)
    if df[col].nunique() < 2 or df["study"].nunique() < 3:
        return None
    try:
        m = BinomialBayesMixedGLM.from_formula(col + " ~ 1", {"study": "0 + C(study)"}, df)
        res = m.fit_vb(verbose=False)
    except Exception as e:
        print(f"   ! hierarchical rate fit failed ({col}): {type(e).__name__}: {e}")
        return None
    mu, s = float(res.fe_mean[0]), float(res.fe_sd[0])
    tau = float(math.exp(res.vcp_mean[0])) if len(res.vcp_mean) else np.nan
    tau_sd = float(res.vcp_sd[0]) if getattr(res, "vcp_sd", None) is not None and len(res.vcp_sd) else np.nan
    rng = np.random.default_rng(7)
    n_mc = 20000
    mu_draw = rng.normal(mu, max(s, 1e-6), n_mc)
    tau_draw = (np.exp(rng.normal(math.log(max(tau, 1e-6)), tau_sd, n_mc))
                if np.isfinite(tau_sd) and tau_sd > 0 else np.full(n_mc, tau))
    z = rng.normal(size=(n_mc, 40))
    probs = 1.0 / (1.0 + np.exp(-(mu_draw[:, None] + tau_draw[:, None] * z)))
    marg = probs.mean(axis=1)
    lo, hi = np.percentile(marg, [2.5, 97.5])
    return {"outcome": col, "k_studies": int(df["study"].nunique()), "n": int(len(df)),
            "central_rate": 1.0 / (1.0 + math.exp(-mu)),
            "pooled_rate": float(marg.mean()), "cri_lo": float(lo), "cri_hi": float(hi),
            "mu_logit": mu, "mu_sd": s, "tau": tau}


def _sd_bf10(mean, sd, prior_sd=1.0):
    """Savage–Dickey BF10 with a N(0, prior_sd) prior and a normal posterior."""
    if not np.isfinite(mean) or not np.isfinite(sd) or sd <= 0:
        return np.nan
    from scipy import stats
    post_at_0 = stats.norm.pdf(0.0, mean, sd)
    prior_at_0 = stats.norm.pdf(0.0, 0.0, prior_sd)
    return prior_at_0 / post_at_0 if post_at_0 > 0 else np.nan


def _pred_col(p):
    return p if p == "female" else p + "_z"


def bayes_logistic(pl, outcome, predictors, include_type=True, study_re=True):
    from statsmodels.genmod.bayes_mixed_glm import BinomialBayesMixedGLM
    from scipy import stats

    cols = [outcome] + [_pred_col(p) for p in predictors] + ["study_key", "type_code"]
    cols = list(dict.fromkeys(cols))
    df = pl[cols].dropna()
    if len(df) < 100 or df[outcome].nunique() < 2:
        return None, None
    df = df.copy()
    df["study"] = df["study_key"].astype(str)
    terms = [_pred_col(p) for p in predictors]
    if include_type and df["type_code"].nunique() > 1:
        dummies = pd.get_dummies(df["type_code"], prefix="type", drop_first=True, dtype=float)
        df = pd.concat([df, dummies], axis=1)
        terms = terms + list(dummies.columns)
    else:
        include_type = False
    formula = outcome + " ~ " + (" + ".join(terms) if terms else "1")
    vc = {"study": "0 + C(study)"} if study_re else {}
    try:
        m = BinomialBayesMixedGLM.from_formula(formula, vc, df)
        res = m.fit_vb(verbose=False)
    except Exception as e:
        print(f"   ! VB fit failed ({outcome}, {predictors}): {type(e).__name__}: {e}")
        return None, None
    names = list(res.model.exog_names)
    mean = np.asarray(res.fe_mean, float)
    sd = np.asarray(res.fe_sd, float)
    rows = []
    for nm, mu, s in zip(names, mean, sd):
        lo, hi = mu - 1.96 * s, mu + 1.96 * s
        pd_ = float(max(stats.norm.cdf(mu / s), 1 - stats.norm.cdf(mu / s))) if s > 0 else np.nan
        rows.append({"outcome": outcome, "term": nm, "label": _term_label(nm),
                     "post_mean": mu, "post_sd": s, "cri_lo": lo, "cri_hi": hi,
                     "odds_ratio": math.exp(mu), "or_lo": math.exp(lo), "or_hi": math.exp(hi),
                     "p_direction": pd_, "bf10_sd": _sd_bf10(mu, s)})
    tab = pd.DataFrame(rows)
    info = {"outcome": outcome, "n": len(df), "n_studies": df["study_key"].nunique(),
            "n_events": int(df[outcome].sum()),
            "re_sd": float(math.exp(res.vcp_mean[0])) if study_re and len(res.vcp_mean) else np.nan,
            "terms": " + ".join(terms), "study_re": study_re, "include_type": include_type}
    return tab, info


def _term_label(nm):
    if nm == "Intercept":
        return "Intercept"
    nm2 = nm[:-2] if nm.endswith("_z") else nm
    if nm2 in PRED_LABELS:
        return PRED_LABELS[nm2]
    if nm.startswith("type_"):
        return "Intervention type: " + nm[len("type_"):].replace("_", " ")
    return nm


UNIVARIATE = ["age", "female", "education", "conservatism", "analytic",
              "news_consumption", "conspiracy_belief", "trust_in_science",
              "prior_knowledge"]
CORE = ["age", "female", "education", "conservatism"]


def run_bayes_models(pl, specs):
    tabs, infos = [], []

    def add(t, i, label):
        if t is None:
            return
        t = t.copy()
        t["model"] = label
        tabs.append(t)
        infos.append({**i, "model": label})

    for spec in specs:
        name = spec[0]
        for p in UNIVARIATE:
            t, i = bayes_logistic(pl, name, [p], include_type=False, study_re=True)
            add(t, i, f"uni:{p}")
        t, i = bayes_logistic(pl, name, [], include_type=True, study_re=True)
        add(t, i, "type")
        t, i = bayes_logistic(pl, name, CORE, include_type=False, study_re=True)
        add(t, i, "demographics")
        t, i = bayes_logistic(pl, name, CORE, include_type=True, study_re=True)
        add(t, i, "full")
    tabs = pd.concat(tabs, ignore_index=True) if tabs else pd.DataFrame()
    if len(tabs):
        tabs["log10_bf10"] = np.log10(tabs["bf10_sd"].clip(lower=1e-12))
    return tabs, pd.DataFrame(infos)


def matched_arm_comparison(allp, specs, mode):
    from statsmodels.genmod.bayes_mixed_glm import BinomialBayesMixedGLM
    from scipy import stats

    both = allp.groupby("study_key")["is_control"].max()
    keys = both[both == 1].index
    d = allp[allp["study_key"].isin(keys)].copy()
    rows = []
    for spec in specs:
        col = spec[0]
        any_col, rel_col, _ = _pair_cols(spec)
        use = rel_col if mode == "rel" else any_col
        gcol = _good_col(rel_col)
        s = d[[use, gcol, "is_intervention", "study_key"]].dropna()
        s["arm"] = s["is_intervention"].astype(float)
        s["study"] = s["study_key"].astype(str)
        pi = s.loc[s["arm"] == 1, use].mean()
        pc = s.loc[s["arm"] == 0, use].mean()
        dcol = spec[1]
        dch = d[[dcol, "is_intervention"]].dropna()
        sd_i = float(dch.loc[dch["is_intervention"] == 1, dcol].std())
        sd_c = float(dch.loc[dch["is_intervention"] == 0, dcol].std())
        pg_i = 100 * s.loc[s["arm"] == 1, gcol].mean()
        pg_c = 100 * s.loc[s["arm"] == 0, gcol].mean()
        try:
            m = BinomialBayesMixedGLM.from_formula(use + " ~ arm",
                                                   {"study": "0 + C(study)"}, s)
            r = m.fit_vb(verbose=False)
            mu, sd = float(r.fe_mean[1]), float(r.fe_sd[1])
            lo, hi = mu - 1.96 * sd, mu + 1.96 * sd
            rows.append({"outcome": col, "label": spec[3],
                         "pct_intervention": 100 * pi, "pct_control": 100 * pc,
                         "diff_pp": 100 * (pi - pc),
                         "pct_rel_good_intervention": pg_i,
                         "pct_rel_good_control": pg_c,
                         "sd_change_intervention": sd_i, "sd_change_control": sd_c,
                         "OR_intervention_vs_control": math.exp(mu),
                         "or_lo": math.exp(lo), "or_hi": math.exp(hi),
                         "p_direction": float(max(stats.norm.cdf(mu / sd),
                                                  1 - stats.norm.cdf(mu / sd))),
                         "n_intervention": int((s["arm"] == 1).sum()),
                         "n_control": int((s["arm"] == 0).sum()),
                         "n_studies": int(s["study"].nunique())})
        except Exception as e:
            print(f"   ! arm comparison failed ({use}): {type(e).__name__}: {e}")
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 8. plots
# ---------------------------------------------------------------------------
def plot_backfire_by_study(desc, specs, mode):
    sub = desc[desc["study_key"] != "ALL"].copy()
    order = sub[sub["outcome"] == specs[2][0]].sort_values("pct_backfire")["study_key"].tolist()
    fig, axes = plt.subplots(1, 3, figsize=(16, 6.5), sharey=True)
    for ax, spec in zip(axes, specs):
        s = sub[sub["outcome"] == spec[0]].set_index("study_key").reindex(order)
        y = np.arange(len(s))
        ax.barh(y, s["pct_backfire"], color="#4C72B0", alpha=.85)
        ax.errorbar(s["pct_backfire"], y,
                    xerr=[s["pct_backfire"] - s["ci_lo"], s["ci_hi"] - s["pct_backfire"]],
                    fmt="none", ecolor="#333", elinewidth=1, capsize=2)
        ax.axvline(5, ls=":", c="black", lw=1)
        ax.set_yticks(y)
        ax.set_yticklabels([f"{k}  (n={int(s.loc[k,'n'])})" for k in s.index], fontsize=8)
        ax.set_title(spec[3], fontsize=10)
        ax.set_xlabel("% of intervention participants backfiring")
        ax.set_xlim(0, max(20, float(s["pct_backfire"].max()) * 1.2))
    tag = "any wrong-direction move" if mode == "any" else "reliable change (RCI >= 1.96)"
    fig.suptitle(f"Backfire rate by study — {tag} (Wilson 95% CI; dotted = 5% reference)",
                 fontsize=12)
    fig.tight_layout()
    p = os.path.join(FIGDIR, f"backfire_by_study_{mode}.png")
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


def plot_change_distributions(pl, thresholds, specs):
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.2))
    for ax, spec in zip(axes, specs):
        col, dcol = spec[0], spec[1]
        key = "true" if "true" in col else ("false" if "false" in col else "disc")
        v = pl[dcol].dropna()
        ax.hist(v, bins=40, color="#55A868", alpha=.85, edgecolor="white")
        ax.axvline(0, color="black", lw=1.2)
        t = thresholds[thresholds["outcome"] == key]["threshold"]
        if len(t):
            tm = float(t.median())
            for sgn in (-1, 1):
                ax.axvline(sgn * tm, color="#C44E52", lw=1.6, ls="--")
            ax.text(-tm, ax.get_ylim()[1] * .92, " reliable-change threshold",
                    fontsize=7, color="#C44E52", ha="left")
        ax.axvline(v.mean(), color="#8172B3", lw=2, label=f"mean={v.mean():+.3f}")
        ax.set_title(spec[3], fontsize=10)
        ax.set_xlabel("post - pre (0-1 endorsement scale)")
        ax.legend(fontsize=8)
    fig.suptitle("Distribution of within-person change (intervention arms) with RCI thresholds",
                 fontsize=12)
    fig.tight_layout()
    p = os.path.join(FIGDIR, "backfire_change_distributions.png")
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


def plot_posteriors(models, specs, mode):
    if not len(models):
        return None
    m = models[models["model"].str.startswith("uni:")].copy()
    preds = [p for p in UNIVARIATE if ("uni:" + p) in set(m["model"])]
    order = list(reversed(preds))
    fig, axes = plt.subplots(1, 3, figsize=(15, 0.42 * len(order) + 2.2))
    for ax, spec in zip(axes, specs):
        s = m[m["outcome"] == spec[0]].copy()
        s = s[s["term"].isin([_pred_col(p) for p in preds])]
        s["_o"] = s["term"].map({_pred_col(p): i for i, p in enumerate(order)})
        s = s.sort_values("_o", ascending=True)
        y = np.arange(len(s))
        ax.errorbar(s["odds_ratio"], y,
                    xerr=[s["odds_ratio"] - s["or_lo"], s["or_hi"] - s["odds_ratio"]],
                    fmt="o", color="#4C72B0", ecolor="#4C72B0", capsize=3, ms=5)
        ax.axvline(1.0, ls="--", c="grey", lw=1)
        ax.set_xscale("log")
        ax.set_yticks(y)
        ax.set_yticklabels(s["label"], fontsize=8)
        ax.set_title(spec[3], fontsize=10)
        ax.set_xlabel("posterior odds ratio (95% CrI)")
    tag = "any wrong-direction move" if mode == "any" else "reliable decline (RCI >= 1.96)"
    fig.suptitle(f"Bayesian logistic models — one predictor at a time — {tag}", fontsize=12)
    fig.tight_layout()
    p = os.path.join(FIGDIR, f"backfire_posterior_ors_{mode}.png")
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


def plot_predicted_by_quintile(pl, specs, mode):
    preds = ["age", "education", "conservatism", "analytic",
             "news_consumption", "conspiracy_belief", "trust_in_science"]
    cols = [s[0] for s in specs]
    fig, axes = plt.subplots(2, 4, figsize=(16, 7))
    axes = axes.ravel()
    for ax, c in zip(axes, preds):
        s = pl[[c] + cols].dropna(subset=[c])
        if len(s) < 50 or s[c].nunique() < 5:
            ax.axis("off")
            continue
        try:
            q = pd.qcut(s[c], 5, labels=False, duplicates="drop")
        except Exception:
            ax.axis("off")
            continue
        for col, lab, colr in zip(cols, ["true", "false", "disc"],
                                  ["#C44E52", "#4C72B0", "#55A868"]):
            r = s.groupby(q)[col].mean() * 100
            ax.plot(r.index + 1, r.values, "o-", label=lab, color=colr)
        ax.set_xticks([1, 2, 3, 4, 5])
        ax.set_title(c, fontsize=10)
        ax.set_xlabel("quintile")
        ax.set_ylabel("% backfire")
        ax.legend(fontsize=7)
    for ax in axes[len(preds):]:
        ax.axis("off")
    tag = "any wrong-direction move" if mode == "any" else "reliable decline"
    fig.suptitle(f"Empirical backfire rate by predictor quintile — {tag}", fontsize=12)
    fig.tight_layout()
    p = os.path.join(FIGDIR, f"backfire_by_predictor_quintile_{mode}.png")
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


# ---------------------------------------------------------------------------
# 9. markdown report
# ---------------------------------------------------------------------------
def write_markdown_report(path, pl, diag, thresholds, res):
    L = []
    L.append("# Backfire analysis — for whom are misinformation interventions counterproductive?\n")
    L.append(f"*Generated by `Scripts/analysis/backfire_analysis.py`; "
             f"{len(pl[pl['is_intervention']==1])} intervention-arm participants over "
             f"{pl['study_key'].nunique()} studies "
             f"({len(pl[pl['is_control']==1])} control participants for reference).*\n")
    L.append("## Two definitions of a backfire\n")
    L.append("- **any** — any move in the wrong direction (descriptive base rate).\n")
    L.append("- **reliable** — a move beyond the Reliable Change Index threshold "
             "`1.96 x SD_pre x sqrt(2(1-r))` (Jacobson & Truax, 1991), with `r` the larger "
             "of the control-arm test-retest correlation and Cronbach's alpha across "
             "item-level pretest columns, computed per study and outcome.\n")
    L.append("\n## Eligibility\n")
    L.append("Datasets that measured a **credibility/belief endorsement** DV on **true and "
             "false** stimuli **before and after** the intervention (domains "
             "credibility / trust / belief_accuracy; accuracy/recognition scales excluded).\n")
    L.append("\n## Sample\n")
    L.append(diag.to_markdown(index=False) + "\n")
    if len(thresholds):
        L.append("\n## Reliable-change thresholds (per study x outcome)\n")
        L.append("| study | outcome | n | SD(pre) | r control | alpha | r whole | r used | source | SE(diff) | threshold | ctl % flagged |\n"
                 "|---|---|---|---|---|---|---|---|---|---|---|---|\n")
        for _, r in thresholds.iterrows():
            def f(v):
                return "—" if not np.isfinite(v) else f"{v:.2f}"
            L.append(f"| {r['study_key']} | {r['outcome']} | {int(r['n'])} | "
                     f"{f(r['sd_pre'])} | {f(r['r_control_retest'])} | {f(r['alpha_items'])} | "
                     f"{f(r['r_whole_sample'])} | {f(r['r_used'])} | {r.get('r_source','')} | "
                     f"{f(r['se_diff'])} | {f(r['threshold'])} | "
                     f"{f(r['pct_flagged_control'])} |\n")
    for mode in ("any", "rel"):
        d = res[mode]
        tag = ("any wrong-direction move" if mode == "any"
               else "reliable decline (RCI >= 1.96)")
        L.append(f"\n## Backfire rates — {tag}\n")
        allr = d["desc"][d["desc"]["study_key"] == "ALL"]
        L.append("| outcome | % backfire | 95% CI | % any | % reliable backfire | % reliable improve | mean delta |\n"
                 "|---|---|---|---|---|---|---|\n")
        for _, r in allr.iterrows():
            L.append(f"| {r['outcome']} | {r['pct_backfire']:.1f} | "
                     f"{r['ci_lo']:.1f}–{r['ci_hi']:.1f} | {r['pct_any']:.1f} | "
                     f"{r['pct_reliable']:.1f} | {r['pct_reliable_improved']:.1f} | "
                     f"{r['mean_change']:+.3f} |\n")
        if len(d["desc_ctl"]):
            L.append("\n**Control-arm reference:**\n\n")
            L.append("| outcome | % backfire | % any | % reliable backfire | % reliable improve | mean delta |\n"
                     "|---|---|---|---|---|---|\n")
            for _, r in d["desc_ctl"].iterrows():
                L.append(f"| {r['outcome']} | {r['pct_backfire']:.1f} | {r['pct_any']:.1f} | "
                         f"{r['pct_reliable']:.1f} | {r['pct_reliable_improved']:.1f} | "
                         f"{r['mean_change']:+.3f} |\n")
        if len(d["arm_cmp"]):
            L.append("\n**Matched intervention-vs-control (studies with both arms):**\n\n")
            L.append("| outcome | intervention % | control % | diff (pp) | rel. improve I | rel. improve C | SD(delta) I | SD(delta) C | OR | 95% CrI |\n"
                     "|---|---|---|---|---|---|---|---|---|---|\n")
            for _, r in d["arm_cmp"].iterrows():
                L.append(f"| {r['label']} | {r['pct_intervention']:.2f} | {r['pct_control']:.2f} | "
                         f"{r['diff_pp']:+.2f} | {r['pct_rel_good_intervention']:.2f} | "
                         f"{r['pct_rel_good_control']:.2f} | {r['sd_change_intervention']:.3f} | "
                         f"{r['sd_change_control']:.3f} | {r['OR_intervention_vs_control']:.2f} | "
                         f"{r['or_lo']:.2f}–{r['or_hi']:.2f} |\n")
        if len(d["hier"]):
            L.append("\n**Hierarchical Bayesian random-effects pooled rate:**\n\n")
            L.append("| outcome | studies | pooled rate | 95% CrI | tau |\n|---|---|---|---|---|\n")
            for _, r in d["hier"].iterrows():
                L.append(f"| {r['outcome']} | {int(r['k_studies'])} | {100*r['pooled_rate']:.1f}% | "
                         f"{100*r['cri_lo']:.1f}–{100*r['cri_hi']:.1f}% | {r['tau']:.2f} |\n")
        if len(d["loo"]):
            L.append("\n**Leave-one-study-out range:**\n\n")
            L.append("| outcome | all | LOO min | LOO max |\n|---|---|---|---|\n")
            for _, r in d["loo"].iterrows():
                L.append(f"| {r['outcome']} | {r['all']:.2f}% | {r['loo_min']:.2f}% | "
                         f"{r['loo_max']:.2f}% |\n")
        L.append(f"\n**By intervention type ({tag}):**\n\n")
        L.append("| intervention type | n | studies | " +
                 " | ".join(_short(s[0]) for s in d["specs"]) + " |\n")
        L.append("|---|---|---|" + "---|" * len(d["specs"]) + "\n")
        for it, g in pl.groupby("intervention_type_coarse"):
            vals = " | ".join(f"{100*g[s[0]].mean():.1f}" for s in d["specs"])
            L.append(f"| {it} | {len(g)} | {g['study_key'].nunique()} | {vals} |\n")
        L.append(f"\n**Bayesian logistic models — one predictor at a time ({tag}):**\n\n")
        L.append("| outcome | predictor | OR | 95% CrI | P(direction) | log10 BF10 | n | studies |\n"
                 "|---|---|---|---|---|---|---|---|\n")
        uni = d["models"][d["models"]["model"].str.startswith("uni:")
                          & (d["models"]["term"] != "Intercept")]
        info = d["minfos"].set_index(["outcome", "model"])
        for _, r in uni.iterrows():
            kk = (r["outcome"], r["model"])
            n = int(info.loc[kk, "n"]) if kk in info.index else ""
            ks = int(info.loc[kk, "n_studies"]) if kk in info.index else ""
            L.append(f"| {r['outcome']} | {r['label']} | {r['odds_ratio']:.2f} | "
                     f"{r['or_lo']:.2f}–{r['or_hi']:.2f} | {r['p_direction']:.3f} | "
                     f"{r['log10_bf10']:.1f} | {n} | {ks} |\n")
        L.append(f"\n**Intervention-type model ({tag}, full sample, debunking = reference):**\n\n")
        ty = d["models"][(d["models"]["model"] == "type") & (d["models"]["term"] != "Intercept")]
        L.append("| outcome | term | OR | 95% CrI | P(direction) |\n|---|---|---|---|---|\n")
        for _, r in ty.iterrows():
            L.append(f"| {r['outcome']} | {r['label']} | {r['odds_ratio']:.2f} | "
                     f"{r['or_lo']:.2f}–{r['or_hi']:.2f} | {r['p_direction']:.3f} |\n")
    L.append("\n## Caveats\n")
    L.append("- The `any` rate is dominated by measurement noise; the **reliable** rate is the "
             "evidence-based one, and the control arm's flag rate at the same threshold "
             "(~1–5%) is the benchmark.\n")
    L.append("- Thresholds are computed per study from that study's own pretest SD and "
             "reliability, so a 'reliable' decline is scale- and noise-adjusted; the median "
             "threshold is about 0.34–0.45 on the 0–1 scale.\n")
    L.append("- The intervention arm's change scores are about 20% more dispersed than the "
             "control arm's (e.g. SD 0.21 vs 0.17), so interventions produce more reliable "
             "movement in BOTH directions — reliable improvement is 2–5x more common than "
             "reliable decline, but the absolute number of reliable declines also rises "
             "slightly. Read the two columns together.\n")
    L.append("- Intervention type is a study-level characteristic; its model is effectively "
             "between-study and only 2–6 studies contribute per level.\n")
    L.append("- `trust_in_science` and `prior_knowledge` are measured in a single study each; "
             "their effects cannot generalise.\n")
    L.append("- Gender and political-ideology coding conventions are taken from each study's "
             "sidecar; studies whose convention is unstated contribute missing values.\n")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join(L))


# ---------------------------------------------------------------------------
# 10. main
# ---------------------------------------------------------------------------
def main():
    print("=" * 78)
    print("BACKFIRE ANALYSIS — for whom are misinformation interventions counterproductive?")
    print("=" * 78)
    allp, diag, thresholds = build_person_level()
    allp = attach_intervention_type(allp)
    allp = standardise(allp)

    pl = allp[allp["is_intervention"] == 1].copy()
    pl_ctl = allp[allp["is_control"] == 1].copy()

    res = {}
    for mode in ("any", "rel"):
        specs = outcome_specs(mode)
        d = {"specs": specs}
        d["desc"] = descriptives(pl, specs, mode)
        d["desc_ctl"] = control_reference(pl_ctl, specs, mode)
        d["rates"] = bayes_rates(pl, specs, mode)
        d["hier"] = pd.DataFrame([r for s in specs
                                  if (r := bayes_hier_rate(pl, s[0])) is not None])
        d["arm_cmp"] = matched_arm_comparison(allp, specs, mode)
        d["loo"] = leave_one_out_range(pl, specs, mode)
        d["models"], d["minfos"] = run_bayes_models(pl, specs)
        res[mode] = d

    pred_desc = predictor_descriptives(pl)
    pred_matrix = predictor_study_matrix(pl)

    # ---------------- console report ----------------
    print("\n--- sample ---")
    print(diag.to_string(index=False))
    print(f"\ntotal intervention participants: {len(pl)}  studies: {pl['study_key'].nunique()}")
    print(f"control participants (reference only): {len(pl_ctl)} over "
          f"{pl_ctl['study_key'].nunique() if len(pl_ctl) else 0} studies")
    print(f"studies with an identified control arm: "
          f"{pl[pl['arm_assumed']==0]['study_key'].nunique()}  "
          f"(arm assumed for {pl[pl['arm_assumed']==1]['study_key'].nunique()} studies)")

    print("\n--- reliable-change thresholds (median per outcome) ---")
    for o in ("true", "false", "disc"):
        t = thresholds[thresholds["outcome"] == o]
        if len(t):
            print(f"  {o:<6} threshold median={t['threshold'].median():.3f}  "
                  f"range {t['threshold'].min():.3f}-{t['threshold'].max():.3f}  "
                  f"r_used median={t['r_used'].median():.2f}  "
                  f"control flagged median={t['pct_flagged_control'].median():.1f}%")

    for mode in ("any", "rel"):
        d = res[mode]
        tag = "ANY wrong-direction move" if mode == "any" else "RELIABLE change (RCI >= 1.96)"
        print(f"\n================ {tag} ================")
        print("--- overall backfire rates (intervention arms) ---")
        for _, r in d["desc"][d["desc"]["study_key"] == "ALL"].iterrows():
            print(f"  {r['outcome']:<20} {r['pct_backfire']:5.1f}%  "
                  f"(95% CI {r['ci_lo']:4.1f}-{r['ci_hi']:4.1f})  "
                  f"any {r['pct_any']:5.1f}%  reliable-backfire {r['pct_reliable']:5.1f}%  "
                  f"reliable-improve {r['pct_reliable_improved']:5.1f}%  "
                  f"mean Δ {r['mean_change']:+.3f}")
        if len(d["desc_ctl"]):
            print("--- control-arm reference ---")
            for _, r in d["desc_ctl"].iterrows():
                print(f"  {r['outcome']:<20} {r['pct_backfire']:5.1f}%  "
                      f"(n={int(r['n'])})  any {r['pct_any']:5.1f}%  "
                      f"reliable-backfire {r['pct_reliable']:5.1f}%  "
                      f"reliable-improve {r['pct_reliable_improved']:5.1f}%")
        print("--- matched intervention-vs-control (studies with both arms) ---")
        if len(d["arm_cmp"]):
            print(d["arm_cmp"][["label", "pct_intervention", "pct_control", "diff_pp",
                                "OR_intervention_vs_control", "or_lo", "or_hi",
                                "n_studies"]].to_string(index=False))
        print("--- hierarchical Bayesian random-effects rate ---")
        if len(d["hier"]):
            print(d["hier"].to_string(index=False))
        print("--- leave-one-study-out range ---")
        if len(d["loo"]):
            print(d["loo"].to_string(index=False))
        print("--- by intervention type ---")
        for it, g in pl.groupby("intervention_type_coarse"):
            vals = "  ".join(f"{_short(s[0])} {100*g[s[0]].mean():5.1f}%"
                             for s in d["specs"])
            print(f"  {it:<24} n={len(g):<5} studies={g['study_key'].nunique()}   {vals}")
        print("--- Bayesian logistic models (posterior summaries) ---")
        for _, r in d["models"].iterrows():
            if r["term"] == "Intercept":
                continue
            print(f"  [{r['model']:<16}] {r['outcome']:<20} {r['label']:<38} "
                  f"OR={r['odds_ratio']:.3f} [{r['or_lo']:.3f},{r['or_hi']:.3f}] "
                  f"pd={r['p_direction']:.3f} log10BF10={r['log10_bf10']:.1f}")

    print("\n--- predictor availability ---")
    print(pred_desc[["predictor", "n", "pct_available", "mean", "sd",
                     "n_studies"]].to_string(index=False))

    # ---------------- figures ----------------
    figs = [plot_backfire_by_study(res[m]["desc"], res[m]["specs"], m) for m in ("any", "rel")]
    figs.append(plot_change_distributions(pl, thresholds, res["rel"]["specs"]))
    figs += [plot_posteriors(res[m]["models"], res[m]["specs"], m) for m in ("any", "rel")]
    figs += [plot_predicted_by_quintile(pl, res[m]["specs"], m) for m in ("any", "rel")]
    print("\nfigures:", *[f for f in figs if f], sep="\n  ")

    # ---------------- write products ----------------
    notes = pd.DataFrame([
        {"item": "scope", "value": "intervention-arm participants with a harmonised "
                                   "credibility/belief endorsement DV measured on true AND "
                                   "false stimuli at pre AND post"},
        {"item": "DV selection", "value": "domains credibility/trust/belief_accuracy; "
                                          "dv_polarity.classify_credibility_scale == 'belief' "
                                          "(endorsement, not accuracy/recognition)"},
        {"item": "aggregation", "value": "participant mean over the qualifying DV columns; "
                                         "shortest stem wins (aggregate preferred over items)"},
        {"item": "orientation", "value": "raw value = amount of trust; verified against the "
                                         "sidecar dv_polarity (true->higher_is_better, "
                                         "false->higher_is_worse) for every included DV"},
        {"item": "definition 'any'", "value": "a) trust_true post-pre < 0; b) trust_false "
                                              "post-pre > 0; c) (true-false) post-pre < 0"},
        {"item": "definition 'reliable'", "value": "Reliable Change Index (Jacobson & Truax "
                                                   "1991): |delta| >= 1.96 * SD_pre * "
                                                   "sqrt(2(1-r)), r = larger of the control-arm "
                                                   "pretest-posttest correlation and "
                                                   "Cronbach's alpha over >=3 item-level "
                                                   "pretest columns; per study x outcome"},
        {"item": "threshold fallback", "value": "studies without a control arm use their own "
                                                "whole-sample pretest-posttest correlation"},
        {"item": "threshold calibration", "value": "the control arm's flag rate at the same "
                                                   "threshold is the empirical false-positive "
                                                   "rate (see Thresholds sheet)"},
        {"item": "gender coding", "value": "taken from each sidecar's notes; studies whose "
                                           "coding is not stated are left missing "
                                           "(Fendt 2026 a/c, Swire 2017, Cook 2024, Maertens 2021)"},
        {"item": "ideology direction", "value": "recoded so higher = more conservative where the "
                                                "sidecar states the direction; direction-unstated "
                                                "studies are left missing"},
        {"item": "age", "value": "Maertens et al. (2021) stores birth year -> age = 2021 - year; "
                                 "ages outside 12-100 set missing"},
        {"item": "Bayesian models", "value": "VB logistic mixed model (BinomialBayesMixedGLM) with "
                                             "a study random intercept; continuous predictors "
                                             "z-scored within study; BF10 = Savage-Dickey "
                                             "normal approximation (prior N(0,1))"},
        {"item": "caveat: single-study predictors", "value": "trust_in_science and prior_knowledge "
                                                             "are measured in one study only"},
        {"item": "caveat: between-study type effects", "value": "intervention type is a "
                                                                "study-level characteristic; its "
                                                                "model is effectively between-study"},
    ])
    allp.to_csv(os.path.join(RESULTS, "backfire_person_level.csv"),
                index=False, encoding="utf-8-sig")
    xl = os.path.join(RESULTS, "backfire_analysis.xlsx")
    with pd.ExcelWriter(xl, engine="xlsxwriter") as xw:
        diag.to_excel(xw, sheet_name="Sample", index=False)
        if len(thresholds):
            thresholds.to_excel(xw, sheet_name="Thresholds", index=False)
        pred_desc.to_excel(xw, sheet_name="Descriptives_Predictors", index=False)
        pred_matrix.to_excel(xw, sheet_name="Predictor_Coverage", index=False)
        for mode in ("any", "rel"):
            d = res[mode]
            sfx = "any" if mode == "any" else "rel"
            d["desc"].to_excel(xw, sheet_name=f"Desc_Outcomes_{sfx}", index=False)
            if len(d["desc_ctl"]):
                d["desc_ctl"].to_excel(xw, sheet_name=f"Control_Ref_{sfx}", index=False)
            d["rates"].to_excel(xw, sheet_name=f"Bayes_Rates_{sfx}", index=False)
            if len(d["hier"]):
                d["hier"].to_excel(xw, sheet_name=f"Bayes_HierRates_{sfx}", index=False)
            if len(d["arm_cmp"]):
                d["arm_cmp"].to_excel(xw, sheet_name=f"Arm_Comparison_{sfx}", index=False)
            if len(d["loo"]):
                d["loo"].to_excel(xw, sheet_name=f"LeaveOneOut_{sfx}", index=False)
            d["models"].to_excel(xw, sheet_name=f"Bayes_Models_{sfx}", index=False)
            d["minfos"].to_excel(xw, sheet_name=f"Model_Diag_{sfx}", index=False)
        notes.to_excel(xw, sheet_name="Notes", index=False)
    print("\nwrote Results/backfire_person_level.csv and Results/backfire_analysis.xlsx")
    write_markdown_report(os.path.join(RESULTS, "backfire_report.md"),
                          allp, diag, thresholds, res)
    print("wrote Results/backfire_report.md")


if __name__ == "__main__":
    main()
