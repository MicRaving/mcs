"""Motivated reasoning & trust in science as moderators of intervention effects
on trust in true news, trust in false news and discernment.

Two questions
-------------
1. **Census** — how many studies in the harmonised corpus collected a measure of
   motivated reasoning (political orientation, partisanship, conspiracy belief,
   analytic/reflective style, epistemic openness, religiosity, ...) or of trust in
   science?  Reported over the whole corpus and over the subset that also carries
   the true/false endorsement outcomes.

2. **Moderation** — for every study that measures an endorsement DV on BOTH true
   and false stimuli *and* at least one such construct, does the construct moderate

       a) trust in TRUE information      (higher = better)
       b) trust in FALSE information     (higher = worse)
       c) DISCERNMENT = true - false     (higher = better)

   Four complementary models, all with a study random intercept and both the
   outcome and the moderator standardised within study (so coefficients are
   "SD of outcome per SD of the construct"):

     A. between-arm   y_post ~ arm * z_mod        (the intervention effect and its
                                                   moderation by the construct)
     B. within-arm    y_post ~ z_mod              (intervention arm only: who ends
                                                   up better/worse)
     C. change        delta ~ z_mod               (intervention arm, pre/post studies)
     D. backfire      P(reliable decline) ~ z_mod (intervention arm, pre/post studies;
                                                   Bayesian logistic)

Orientation
-----------
Every construct is oriented "higher = more of the construct".  Political ideology
is the only one whose scale direction varies, and it is read from each study's
sidecar ``notes`` ("1 = liberal .. 7 = conservative" vs "1 = conservative .. 7 =
progressive"); studies whose note does not state a direction are dropped from the
conservatism model and listed in the Direction_Audit sheet.  Nothing is invented.

Outputs (Results/)
------------------
  motivated_reasoning_moderation.xlsx   Census / Census_Detail / Person_Sample /
                                        Mod_Level_ArmMod / Mod_Within_Intervention /
                                        Mod_Change / Mod_Backfire / Direction_Audit /
                                        Model_Errors / Notes
  motivated_reasoning_report.md         the same tables as markdown
  mr_person_level.csv                   participant-level data (all outcomes + moderators)
  figures/mr_*.png                      forest plots + census

Run: python Scripts/analysis/motivated_reasoning_moderation.py
"""
import os
import re
import sys
import glob
import math
import tempfile
import collections
import warnings

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

warnings.filterwarnings("ignore")

_HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(_HERE)
B = os.path.dirname(SCRIPTS)
RESULTS = os.path.join(B, "Results")
FIGDIR = os.path.join(RESULTS, "figures")
os.makedirs(FIGDIR, exist_ok=True)
os.makedirs(os.path.join(B, "temp"), exist_ok=True)
tempfile.tempdir = os.path.join(B, "temp")

DS = os.path.join(B, "Datasets")
sys.path.insert(0, SCRIPTS)
import dv_polarity as DVP            # noqa: E402
import suite_common as SC            # noqa: E402
import study_exclusions as SE        # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

COMP = {"credibility", "trust", "belief_accuracy"}
ITEM_RX = re.compile(r"_i\d+$")

# ---------------------------------------------------------------------------
# moderator registry
#   groups   : preference-ordered groups of aggregate column names to average
#   item_base: prefix for the item-level fallback composite
#   orient   : 'more' (higher = more of the construct) or 'ideology'
# ---------------------------------------------------------------------------
MODERATORS = collections.OrderedDict([
    ("conservatism", dict(
        label="Political conservatism", short="conservatism", orient="ideology",
        groups=[["cov_political_ideology"],
                ["cov_political_ideology_general"],
                ["cov_political_ideology_social", "cov_political_ideology_economic"],
                ["cov_political_ideology_numeric", "cov_political_orientation",
                 "cov_political_ideology_orientation", "cov_conservatism"]],
        item_base="cov_political_ideology")),
    ("ideology_extremity", dict(
        label="Ideological extremity (|z|)", short="extr", orient="extremity",
        derived=True,
        groups=[["cov_political_ideology"],
                ["cov_political_ideology_general"],
                ["cov_political_ideology_social", "cov_political_ideology_economic"],
                ["cov_political_ideology_numeric", "cov_political_orientation",
                 "cov_political_ideology_orientation", "cov_conservatism"]],
        item_base="cov_political_ideology")),
    ("conspiracy_belief", dict(
        label="Conspiracy belief", short="conspiracy", orient="more",
        groups=[["cov_conspiracy_belief", "cov_generic_conspiracy_belief_gcb5"]],
        item_base="cov_conspiracy_belief")),
    ("analytic_style", dict(
        label="Analytic / reflective style", short="analytic", orient="more",
        groups=[["cov_analytic_thinking", "cov_crt", "cov_cognitive_reflection",
                 "cov_cognitive_ability_crt", "cov_cognitive_ability"]],
        item_base="cov_analytic_thinking")),
    ("need_for_cognition", dict(
        label="Need for cognition", short="nfc", orient="more",
        groups=[["cov_need_for_cognition"]], item_base="cov_need_for_cognition")),
    ("open_minded", dict(
        label="Actively open-minded thinking", short="aot", orient="more",
        groups=[["cov_actively_open_minded_thinking"]],
        item_base="cov_actively_open_minded_thinking")),
    ("intellectual_humility", dict(
        label="Intellectual humility", short="ih", orient="more",
        groups=[["cov_intellectual_humility"]], item_base="cov_intellectual_humility")),
    ("need_for_closure", dict(
        label="Need for closure / dogmatism", short="nfc2", orient="more",
        groups=[["cov_need_for_closure"], ["cov_dogmatism"]],
        item_base="cov_need_for_closure")),
    ("religiosity", dict(
        label="Religiosity", short="religiosity", orient="more",
        groups=[["cov_religiosity"]], item_base="cov_religiosity")),
    ("trust_in_science", dict(
        label="Trust in science", short="trust_science", orient="more",
        groups=[["cov_trust_in_science", "cov_confidence_in_science"]],
        item_base="cov_trust_in_science")),
])
MODELED = list(MODERATORS)
CENSUS_MODS = [m for m in MODERATORS if not MODERATORS[m].get("derived")]

# theory-implied sign of each construct's association with the outcomes
ROLES = {"conservatism": "risk", "ideology_extremity": "risk",
         "conspiracy_belief": "risk", "need_for_closure": "risk", "religiosity": "risk",
         "analytic_style": "protective", "need_for_cognition": "protective",
         "open_minded": "protective", "intellectual_humility": "protective",
         "trust_in_science": "protective"}
ROLE_SIGN = {"risk": {"true": -1, "false": +1, "disc": -1},
             "protective": {"true": +1, "false": -1, "disc": +1}}

# constructs reported in the census but not carried into the moderation models
CENSUS_ONLY = collections.OrderedDict([
    ("partisanship", dict(label="Partisanship / party identification",
                          rx=r"^cov_(political_party|party_id|party_identification|"
                             r"political_affiliation)")),
    ("prior_topic_attitudes", dict(
        label="Prior topic attitudes (vaccine / climate / immigration)",
        rx=r"^cov_(vaccine_attitude|climate_attitude|immigration_attitude|prior_belief|"
           r"belief_specific)")),
    ("political_knowledge", dict(label="Political knowledge",
                                 rx=r"^cov_political_knowledge")),
    ("extremism", dict(label="Political extremism",
                       rx=r"^cov_(extremism|political_extremism)")),
    ("group_identity", dict(label="Group / partisan identity",
                            rx=r"^cov_(\w*identity|in_?group|out_?group|partisan_identity)")),
    ("pseudoscience_belief", dict(label="Pseudoscience / unwarranted beliefs",
                                  rx=r"^cov_(pseudoscience_belief|unwarranted_beliefs)")),
    ("science_attitudes", dict(
        label="Science attitudes / deference / efficacy (adjacent to trust in science)",
        rx=r"^cov_(attitudes_toward_science|deference_scientific_authority|science_efficacy|"
           r"prior_knowledge_science|science_literacy|scientific_literacy|neuroscience_literacy)")),
])

OUTCOMES = [("true", "Trust in TRUE news", +1),
            ("false", "Trust in FALSE news", -1),
            ("disc", "Discernment (true - false)", +1)]

# postal-timepoint buckets
TP_PRE = ("pre",)
TP_POST = ("post", "single", "")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def endorsement(uv, notes):
    try:
        return DVP.classify_credibility_scale(uv, notes) == "belief"
    except Exception:
        return False


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


def ideo_direction(note):
    """'no_reverse' | 'reverse' | None (undetermined) for an ideology scale."""
    n = (note or "").lower()
    if re.search(r"\brepublican\b", n) and re.search(r"\bdemocratic?\b", n):
        return None                     # party category list, not a left-right scale
    ml = re.search(r"liberal|left|progressive|democratic", n)
    mr = re.search(r"conservative|right|republican|gop", n)
    if mr and not ml:
        return "no_reverse"
    if ml and not mr:
        return "reverse"
    if not ml and not mr:
        return None
    return "no_reverse" if mr.start() > ml.start() else "reverse"


def _to_num(s):
    return pd.to_numeric(s, errors="coerce")


# ---------------------------------------------------------------------------
# 1. census (sidecar only, whole corpus)
# ---------------------------------------------------------------------------
def _fam_hits(side):
    """families present in one sidecar (dict family -> [names])."""
    cov = side[side["role"].astype(str) == "covariate"]
    names = set(cov["uniform_variable"].astype(str))
    hits = collections.defaultdict(list)
    for fam, spec in MODERATORS.items():
        if spec.get("derived"):
            continue
        base = set(x for g in spec["groups"] for x in g)
        got = [n for n in names if n in base
               or (n.startswith(spec["item_base"]) and ITEM_RX.search(n))]
        if got:
            hits[fam] = got
    for fam, spec in CENSUS_ONLY.items():
        got = [n for n in names if re.search(spec["rx"], n)]
        if got:
            hits[fam] = got
    return hits


def census():
    rows, det = [], []
    for f in sorted(glob.glob(os.path.join(DS, "*", "* - harmonized_variables.csv"))):
        key = os.path.basename(os.path.dirname(f))
        try:
            side = pd.read_csv(f, encoding="utf-8-sig", low_memory=False)
        except Exception:
            continue
        if "role" not in side.columns:
            continue
        hits = _fam_hits(side)
        rows.append({"study_key": key,
                     **{fam: int(fam in hits) for fam in list(MODERATORS) + list(CENSUS_ONLY)}})
        for fam, names in hits.items():
            det.append({"study_key": key, "construct": fam,
                        "variables": ";".join(sorted(names))})
    return pd.DataFrame(rows), pd.DataFrame(det)


# ---------------------------------------------------------------------------
# 2. DV selection per study
# ---------------------------------------------------------------------------
def _dv_cols(side, cols):
    """qualifying endorsement columns by (veracity, timepoint-bucket)."""
    dv = side[side["role"].astype(str) == "dv"]
    out = collections.defaultdict(list)
    for _, r in dv.iterrows():
        if str(r.get("dv_domain") or "").strip().lower() not in COMP:
            continue
        ver = str(r.get("veracity") if pd.notna(r.get("veracity")) else "").strip().lower()
        if ver not in ("true", "false"):
            continue
        uv = str(r["uniform_variable"])
        if uv + "_01" not in cols or not endorsement(uv, r.get("notes")):
            continue
        tp = str(r.get("timepoint") if pd.notna(r.get("timepoint")) else "").strip().lower()
        bucket = "pre" if tp in TP_PRE else ("post" if tp in TP_POST else "other")
        out[(ver, bucket)].append((strip_tokens(uv, ver, tp), uv + "_01"))
    return out


def pick_dv(cand, ver, bucket):
    """Tier-1: a stem present for BOTH veracities (shortest stem).  Tier-2: mean of
    every column of that veracity in the bucket, restricted to one domain."""
    mine = cand.get((ver, bucket), [])
    other = cand.get(("false" if ver == "true" else "true", bucket), [])
    if not mine:
        return [], None
    other_stems = {s for s, _ in other}
    matched = sorted({s for s, _ in mine if s in other_stems},
                     key=lambda s: (s.count("_"), s))
    if matched:
        best = matched[0]
        return [c for s, c in mine if s == best], "stem-matched"
    # tier 2: one domain (use the modal domain of the columns, shortest names first)
    doms = collections.Counter(c.split("_")[1] for _, c in mine)
    dom = doms.most_common(1)[0][0]
    sel = [c for _, c in mine if c.split("_")[1] == dom]
    return sel, ("domain:" + dom)


def moderator_cols(spec, cols):
    for group in spec["groups"]:
        present = [c for c in group if c in cols]
        if present:
            return present, "aggregate"
    base = spec["item_base"]
    items = [c for c in cols if c.startswith(base) and ITEM_RX.search(c)]
    if items:
        return items, "items"
    return [], None


# ---------------------------------------------------------------------------
# 3. participant-level assembly
# ---------------------------------------------------------------------------
def build():
    frames, sample_rows, direction_rows, errs = [], [], [], []
    full_excluded = {k for (k, exp) in SE.ALL_EXCLUSIONS if exp == "*"
                     or exp == SE.WILDCARD}

    for f in sorted(glob.glob(os.path.join(DS, "*", "* - harmonized_variables.csv"))):
        key = os.path.basename(os.path.dirname(f))
        if key in full_excluded:
            continue
        wide_path = os.path.join(DS, key, key + " - harmonized.csv")
        if not os.path.isfile(wide_path):
            continue
        try:
            side = pd.read_csv(f, encoding="utf-8-sig", low_memory=False)
            wcols = list(pd.read_csv(wide_path, nrows=0, encoding="utf-8-sig").columns)
            wset = set(wcols)
        except Exception as e:
            errs.append({"study_key": key, "stage": "read", "error": str(e)})
            continue
        if "role" not in side.columns:
            continue
        cand = _dv_cols(side, wset)
        if not (cand.get(("true", "post")) or cand.get(("true", "other"))) \
                or not (cand.get(("false", "post")) or cand.get(("false", "other"))):
            continue
        if not cand.get(("true", "post")):
            cand[("true", "post")] = cand.get(("true", "other"), [])
        if not cand.get(("false", "post")):
            cand[("false", "post")] = cand.get(("false", "other"), [])

        sel, tier = {}, {}
        for ver in ("true", "false"):
            for bucket in ("pre", "post"):
                sel[(ver, bucket)], tier[(ver, bucket)] = pick_dv(cand, ver, bucket)
        if not sel[("true", "post")] or not sel[("false", "post")]:
            continue

        # moderator selection
        mod_sel, mod_kind, mod_note = {}, {}, {}
        note_by_var = {str(r["uniform_variable"]): str(r.get("notes") or "")
                       for _, r in side.iterrows()}
        for mkey, spec in MODERATORS.items():
            cs, kind = moderator_cols(spec, wset)
            if not cs:
                continue
            mod_sel[mkey] = cs
            mod_kind[mkey] = kind
            mod_note[mkey] = note_by_var.get(cs[0], "")
        if not mod_sel:
            continue

        # columns to read
        base = [c for c in ("pid", "condition", "condition_raw", "artifact", "study_key")
                if c in wset]
        need = list(dict.fromkeys(
            base + [c for k in sel.values() for c in k]
            + [c for cs in mod_sel.values() for c in cs]))
        try:
            d = pd.read_csv(wide_path, encoding="utf-8-sig", low_memory=False, usecols=need)
        except Exception as e:
            errs.append({"study_key": key, "stage": "load", "error": str(e)})
            continue
        if not len(d):
            continue

        pid = d["pid"] if "pid" in d.columns else pd.Series(np.arange(len(d)), index=d.index)
        art = d["artifact"].astype(str) if "artifact" in d.columns else pd.Series("", index=d.index)
        cond = d["condition"] if "condition" in d.columns else pd.Series(np.nan, index=d.index)

        # arm role per (condition, artifact) pair
        pairs = pd.DataFrame({"c": cond.astype(str), "a": art.astype(str)}).drop_duplicates()
        role_map = {}
        for _, pr in pairs.iterrows():
            try:
                role_map[(pr["c"], pr["a"])] = SC.classify_condition(
                    pr["c"], key, pr["a"], folder=os.path.join(DS, key))[0]
            except Exception:
                role_map[(pr["c"], pr["a"])] = "unknown"
        role = [role_map.get((c, a), "unknown") for c, a in zip(cond.astype(str), art.astype(str))]
        role = pd.Series(role, index=d.index)
        has_int = (role == "intervention").any()
        if has_int:
            is_int = (role == "intervention").astype(int)
            is_ctl = role.isin(SC.COND_CONTROL_ROLES).astype(int)
        else:
            is_int = pd.Series(1, index=d.index)
            is_ctl = pd.Series(0, index=d.index)

        p = pd.DataFrame({"study_key": key, "artifact": art, "pid": pid,
                          "is_intervention": is_int, "is_control": is_ctl})
        for (ver, bucket), cs in sel.items():
            if cs:
                p[f"{ver}_{bucket}"] = d[cs].apply(_to_num).mean(axis=1)
        for mkey, cs in mod_sel.items():
            p[mkey + "_raw"] = d[cs].apply(_to_num).mean(axis=1)

        p = p[(p["is_intervention"] == 1) | (p["is_control"] == 1)]
        if not len(p):
            continue
        # aggregate to one row per (artifact, pid)
        agg = {c: "mean" for c in p.columns
               if c not in ("study_key", "artifact", "pid", "is_intervention", "is_control")}
        agg.update({"is_intervention": "max", "is_control": "max",
                    "study_key": "first", "artifact": "first"})
        p = p.groupby(["artifact", "pid"], as_index=False, dropna=False).agg(agg)
        if not len(p):
            continue
        if "is_intervention" in p and "is_control" in p:
            both = (p["is_intervention"] == 1) & (p["is_control"] == 1)
            if both.any():                       # a pid in both arms: keep intervention
                p.loc[both, "is_control"] = 0

        p["arm_assumed"] = 0 if (p["is_intervention"] == 1).any() else 1
        p["d_tt"] = p["true_post"] - p["true_pre"] if "true_pre" in p else np.nan
        p["d_tf"] = p["false_post"] - p["false_pre"] if "false_pre" in p else np.nan
        p["disc_post"] = p["true_post"] - p["false_post"]
        if "true_pre" in p and "false_pre" in p:
            p["disc_pre"] = p["true_pre"] - p["false_pre"]
            p["d_disc"] = p["disc_post"] - p["disc_pre"]
        else:
            p["disc_pre"] = np.nan
            p["d_disc"] = np.nan

        # orient + z-score every moderator within study
        for mkey, spec in MODERATORS.items():
            col = mkey + "_raw"
            if col not in p:
                continue
            x = p[col].astype(float)
            src = mod_kind.get(mkey, "")
            if spec["orient"] == "ideology":
                dr = ideo_direction(mod_note.get(mkey, ""))
                direction_rows.append({"study_key": key, "moderator": mkey,
                                       "column": ";".join(mod_sel[mkey]),
                                       "direction": dr or "undetermined",
                                       "evidence": mod_note.get(mkey, "")[:160]})
                if dr is None:
                    p[mkey] = np.nan
                    continue
                if dr == "reverse":
                    x = -x
            mu, sd = x.mean(), x.std()
            if sd and np.isfinite(sd) and sd > 0:
                z = (x - mu) / sd
                if spec["orient"] == "extremity":
                    e = z.abs()
                    ez = e.std()
                    p[mkey] = (e - e.mean()) / ez if ez and ez > 0 else np.nan
                else:
                    p[mkey] = z
            else:
                p[mkey] = np.nan

        # reliable-change thresholds (pre/post studies only)
        thr = {}
        for ok, pre, post, wrong in (("true", "true_pre", "true_post", "lt"),
                                     ("false", "false_pre", "false_post", "gt"),
                                     ("disc", "disc_pre", "disc_post", "lt")):
            if pre not in p or p[pre].isna().all():
                thr[ok] = np.nan
                continue
            s = p[[pre, post]].apply(_to_num).dropna()
            if len(s) < 30:
                thr[ok] = np.nan
                continue
            sd_pre = float(s[pre].std())
            ctl = p[(p["is_control"] == 1)][[pre, post]].apply(_to_num).dropna() \
                if (p["is_control"] == 1).any() else s.iloc[0:0]
            r = float(ctl[pre].corr(ctl[post])) if len(ctl) >= 25 else np.nan
            if not np.isfinite(r):
                r = float(s[pre].corr(s[post]))
            r = float(min(max(r, 0.05), 0.95)) if np.isfinite(r) else np.nan
            thr[ok] = 1.96 * sd_pre * math.sqrt(2 * (1 - r)) if np.isfinite(r) else np.nan
        p["thr_true"], p["thr_false"], p["thr_disc"] = thr["true"], thr["false"], thr["disc"]
        for ok, dcol, wrong in (("true", "d_tt", "lt"), ("false", "d_tf", "gt"),
                                ("disc", "d_disc", "lt")):
            t = thr[ok]
            if np.isfinite(t):
                p["bf_" + ok] = ((p[dcol] <= -t) if wrong == "lt" else (p[dcol] >= t)).astype(float)
            else:
                p["bf_" + ok] = np.nan

        p["dv_tier_true"] = tier[("true", "post")]
        p["dv_tier_false"] = tier[("false", "post")]
        p["n_true_cols"] = len(sel[("true", "post")])
        p["n_false_cols"] = len(sel[("false", "post")])
        p["has_prepost"] = 1 if ("true_pre" in p and "false_pre" in p) else 0
        frames.append(p)

        sample_rows.append({
            "study_key": key, "n_intervention": int((p["is_intervention"] == 1).sum()),
            "n_control": int((p["is_control"] == 1).sum()),
            "arm_assumed": int(p["arm_assumed"].iloc[0]),
            "prepost": int(p["has_prepost"].iloc[0]),
            "dv_true": ";".join(sorted({c for _, c in cand.get(("true", "post"), [])}))[:60],
            "dv_tier_true": tier[("true", "post")], "dv_tier_false": tier[("false", "post")],
            "moderators": ";".join(sorted(mod_sel)),
        })

    if not frames:
        raise SystemExit("no eligible datasets")
    pl = pd.concat(frames, ignore_index=True)
    return pl, pd.DataFrame(sample_rows), pd.DataFrame(direction_rows), pd.DataFrame(errs)


# ---------------------------------------------------------------------------
# 4. models
# ---------------------------------------------------------------------------
def _z_within(df, col):
    g = df.groupby("study_key")[col]
    mu = g.transform("mean")
    sd = g.transform("std")
    return (df[col] - mu) / sd.replace(0, np.nan)


def _std_global(s):
    """Standardise an outcome once, across the pooled sample, so the study random
    intercept stays non-degenerate (coefficients remain SD-of-outcome per SD)."""
    s = pd.to_numeric(s, errors="coerce").astype(float)
    sd = s.std()
    return (s - s.mean()) / sd if sd and np.isfinite(sd) and sd > 0 else s * 0.0


def _prep(pl, mkey, outcome):
    """rows with the outcome, the moderator, both arms present."""
    ycol = {"true": "true_post", "false": "false_post", "disc": "disc_post"}[outcome]
    d = pl[["study_key", "is_intervention", "is_control", mkey, ycol]].dropna().copy()
    d["y"] = _std_global(d[ycol])
    d["z"] = d[mkey]                       # already z-scored within study
    return d


def _fit_mixedlm(formula, d):
    """MixedLM with the optimiser noise silenced; returns (model|None, converged)."""
    import statsmodels.formula.api as smf
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            m = smf.mixedlm(formula, d, groups=d["study_key"]).fit(reml=True)
        except Exception:
            return None, False
    return m, bool(getattr(m, "converged", True))


def model_A(pl, mkey, outcome):
    """y_post ~ arm * z  (between-arm moderation)."""
    d = _prep(pl, mkey, outcome)
    d = d[d.groupby("study_key")["is_control"].transform("max") == 1]
    d = d.dropna(subset=["y", "z"])
    if d["study_key"].nunique() < 3 or d["is_intervention"].nunique() < 2:
        return None
    d = d.rename(columns={"is_intervention": "arm"})
    m, conv = _fit_mixedlm("y ~ arm * z", d)
    if m is None:
        return None
    ci = m.conf_int()
    def row(term, lab, kind):
        return dict(term=term, label=lab, kind=kind,
                    estimate=float(m.params[term]), se=float(m.bse[term]),
                    ci_lo=float(ci.loc[term, 0]), ci_hi=float(ci.loc[term, 1]),
                    p=float(m.pvalues[term]))
    out = [row("arm", "Intervention effect at mean moderator", "arm_effect"),
           row("z", "Moderator slope in the control arm", "control_slope"),
           row("arm:z", "Moderation of the intervention effect", "interaction")]
    return dict(rows=out, n=int(m.nobs), n_studies=int(d["study_key"].nunique()),
                n_intervention=int((d["arm"] == 1).sum()),
                n_control=int((d["arm"] == 0).sum()), converged=conv)


def model_B(pl, mkey, outcome):
    """y_post ~ z, intervention arm only."""
    d = _prep(pl, mkey, outcome)
    d = d[d["is_intervention"] == 1].dropna(subset=["y", "z"])
    if d["study_key"].nunique() < 3:
        return None
    m, conv = _fit_mixedlm("y ~ z", d)
    if m is None:
        return None
    ci = m.conf_int()
    return dict(n=int(m.nobs), n_studies=int(d["study_key"].nunique()),
                estimate=float(m.params["z"]), se=float(m.bse["z"]),
                ci_lo=float(ci.loc["z", 0]), ci_hi=float(ci.loc["z", 1]),
                p=float(m.pvalues["z"]), converged=conv)


def model_C(pl, mkey, outcome):
    """delta (pre->post) ~ z, intervention arm only, pre/post studies."""
    dcol = {"true": "d_tt", "false": "d_tf", "disc": "d_disc"}[outcome]
    d = pl[["study_key", "is_intervention", mkey, dcol]].dropna().copy()
    d = d[d["is_intervention"] == 1]
    if not len(d) or d["study_key"].nunique() < 3:
        return None
    d["y"] = _std_global(d[dcol])
    d["z"] = d[mkey]
    d = d.dropna(subset=["y", "z"])
    m, conv = _fit_mixedlm("y ~ z", d)
    if m is None:
        return None
    ci = m.conf_int()
    return dict(n=int(m.nobs), n_studies=int(d["study_key"].nunique()),
                estimate=float(m.params["z"]), se=float(m.bse["z"]),
                ci_lo=float(ci.loc["z", 0]), ci_hi=float(ci.loc["z", 1]),
                p=float(m.pvalues["z"]), converged=conv)


def model_D(pl, mkey, outcome):
    """P(reliable decline) ~ z, intervention arm only (Bayesian logistic)."""
    from statsmodels.genmod.bayes_mixed_glm import BinomialBayesMixedGLM
    from scipy import stats
    bcol = "bf_" + outcome
    d = pl[["study_key", "is_intervention", mkey, bcol]].dropna().copy()
    d = d[d["is_intervention"] == 1]
    if not len(d) or d["study_key"].nunique() < 3 or d[bcol].nunique() < 2:
        return None
    d["z"] = d[mkey]
    d = d.dropna(subset=["z"])
    d["study"] = d["study_key"].astype(str)
    if d[bcol].sum() < 10:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            m = BinomialBayesMixedGLM.from_formula(
                f"{bcol} ~ z", {"study": "0 + C(study)"}, d)
            r = m.fit_vb(verbose=False)
    except Exception:
        return None
    mu, sd = float(r.fe_mean[1]), float(r.fe_sd[1])
    lo, hi = mu - 1.96 * sd, mu + 1.96 * sd
    bf = math.nan
    try:
        post0 = stats.norm.pdf(0, mu, sd)
        prior0 = stats.norm.pdf(0, 0, 1)
        bf = prior0 / post0 if post0 > 0 else math.nan
    except Exception:
        pass
    return dict(n=int(len(d)), n_studies=int(d["study"].nunique()),
                n_events=int(d[bcol].sum()), pct_events=100 * float(d[bcol].mean()),
                logit=mu, se=sd, odds_ratio=math.exp(mu),
                or_lo=math.exp(lo), or_hi=math.exp(hi),
                p_direction=float(max(stats.norm.cdf(mu / sd), 1 - stats.norm.cdf(mu / sd)))
                if sd > 0 else math.nan,
                log10_bf10=(math.log10(bf) if np.isfinite(bf) and bf > 0 else math.nan))


# ---------------------------------------------------------------------------
# 5. figures
# ---------------------------------------------------------------------------
def sign_audit(pl):
    """Per-study sign of the construct-outcome association inside the intervention
    arm, with the share of studies matching the theoretically expected direction
    (higher construct -> more false-news trust, less true-news trust, less
    discernment).  A study whose sign opposes its peers is either genuine
    heterogeneity or a mis-oriented harmonised column."""
    expect = {"true": -1, "false": +1, "disc": -1}
    rows = []
    for m in MODELED:
        if m not in pl.columns:
            continue
        role = ROLES.get(m)
        if not role:
            continue
        for ok, olab, _ in OUTCOMES:
            exp = ROLE_SIGN[role][ok]
            ycol = {"true": "true_post", "false": "false_post", "disc": "disc_post"}[ok]
            d = pl[pl["is_intervention"] == 1][["study_key", m, ycol]].dropna()
            for k, g in d.groupby("study_key"):
                if len(g) < 30 or g[m].nunique() < 3:
                    continue
                r = float(g[m].corr(g[ycol]))
                rows.append({"moderator": MODERATORS[m]["label"], "construct_role": role,
                             "outcome": olab,
                             "study_key": k, "n": int(len(g)), "r": r,
                             "expected_sign": exp,
                             "matches_expected": int(np.sign(r) == exp)})
    d = pd.DataFrame(rows)
    if not len(d):
        return d, d
    summ = (d.groupby(["moderator", "construct_role", "outcome"])
             .agg(n_studies=("study_key", "nunique"),
                  pct_matching_expected=("matches_expected", lambda s: round(100 * s.mean(), 1)),
                  median_r=("r", lambda s: round(float(s.median()), 3)))
             .reset_index())
    return d, summ


def forest_matrix(mat, path, title, xlabel, logx=False):
    """mat: dict outcome -> list of (label, est, lo, hi, sig)"""
    outs = [o for o, _l, _s in OUTCOMES if o in mat]
    fig, axes = plt.subplots(1, len(outs), figsize=(5.4 * len(outs), 5.6))
    if len(outs) == 1:
        axes = [axes]
    for ax, o in zip(axes, outs):
        rows = mat[o]
        labs = [r[0] for r in rows]
        ys = np.arange(len(rows))[::-1]
        for y, (lab, est, lo, hi, sig) in zip(ys, rows):
            col = "#C44E52" if sig and est > (1 if logx else 0) else (
                "#4C72B0" if sig else "#8d8d8d")
            ax.plot([lo, hi], [y, y], color=col, lw=1.8)
            ax.plot([est], [y], "o", color=col, ms=6)
            ax.text(hi, y, f" {est:.2f}", va="center", fontsize=7, color="#555")
        ax.set_yticks(ys)
        ax.set_yticklabels(labs, fontsize=8.5)
        ax.axvline(1 if logx else 0, ls="--", c="#444", lw=1)
        if logx:
            ax.set_xscale("log")
        lbl = dict(OUTCOMES2)[o]
        ax.set_title(lbl, fontsize=10)
        ax.set_xlabel(xlabel)
    fig.suptitle(title, fontsize=12)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


OUTCOMES2 = [(k, v) for k, v, _ in OUTCOMES]


def plot_census(cen):
    fams = CENSUS_MODS + list(CENSUS_ONLY)
    labels = [MODERATORS[m]["label"] if m in MODERATORS else CENSUS_ONLY[m]["label"]
              for m in fams]
    tot = [int(cen[m].sum()) for m in fams]
    tt = [int((cen[m] == 1).sum()) for m in fams]      # placeholder (same corpus)
    order = np.argsort(tot)
    fig, ax = plt.subplots(figsize=(9, 5.4))
    y = np.arange(len(fams))
    ax.barh(y, [tot[i] for i in order], color="#4C72B0", alpha=.9)
    for yy, i in zip(y, order):
        ax.text(tot[i] + 1, yy, str(tot[i]), va="center", fontsize=8)
    ax.set_yticks(y)
    ax.set_yticklabels([labels[i] for i in order], fontsize=8.5)
    ax.set_xlabel("number of harmonised studies collecting the construct")
    ax.set_title(f"Motivated-reasoning / trust-in-science measures in the corpus "
                 f"({cen['study_key'].nunique()} harmonised datasets)", fontsize=11)
    fig.tight_layout()
    p = os.path.join(FIGDIR, "mr_census.png")
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


# ---------------------------------------------------------------------------
# 6. report
# ---------------------------------------------------------------------------
def markdown(path, cen, det, sample, lvl, within, change, backfire, direction, errs, theta,
             sign_summ=None):
    L = []
    L.append("# Motivated reasoning and trust in science as moderators\n")
    L.append("*Generated by `Scripts/analysis/motivated_reasoning_moderation.py`.*\n")
    L.append("\n## 1. How many studies collected these measures?\n")
    L.append(f"The corpus holds **{cen['study_key'].nunique()} harmonised datasets**. "
             f"Constructs collected (a study counts once per construct):\n\n")
    L.append("| construct | studies | of which measure true+false endorsement |\n|---|---|---|\n")
    dvs = set(sample["study_key"])
    for m in CENSUS_MODS + list(CENSUS_ONLY):
        lab = MODERATORS[m]["label"] if m in MODERATORS else CENSUS_ONLY[m]["label"]
        L.append(f"| {lab} | {int(cen[m].sum())} | "
                 f"{int(cen.loc[cen['study_key'].isin(dvs), m].sum())} |\n")
    L.append(f"\nThe **moderation analysis** uses the {sample['study_key'].nunique()} studies "
             f"that carry a true/false endorsement pair *and* at least one modelled "
             f"construct: {int(sample['n_intervention'].sum()):,} intervention and "
             f"{int(sample['n_control'].sum()):,} control participants.\n")
    L.append("\n### Headline\n")
    if len(lvl):
        x = lvl[(lvl["kind"] == "interaction") & (lvl["p"] < 0.05)]
        L.append("**Moderation of the intervention effect (model A, p < .05).** The signed "
                 "interaction is the change in the intervention's effect on the outcome per SD "
                 "of the construct; for *false-news trust* a positive value is unfavourable, "
                 "for *true-news trust* and *discernment* a negative value is unfavourable.\n\n")
        for _, r in x.iterrows():
            L.append(f"- {r['outcome']}: **{r['moderator']}** "
                     f"(b = {r['estimate']:+.3f}, 95% CI {r['ci_lo']:+.3f} to {r['ci_hi']:+.3f}, "
                     f"{int(r['n_studies'])} studies)\n")
    if len(backfire):
        x = backfire[(backfire["or_lo"] > 1) | (backfire["or_hi"] < 1)]
        L.append("\n**Who reliably declines (model D, 95% CrI excludes 1).** Odds ratio per SD "
                 "of the construct for a reliable wrong-direction move.\n\n")
        for _, r in x.iterrows():
            L.append(f"- {r['outcome']}: **{r['moderator']}** "
                     f"(OR = {r['odds_ratio']:.2f}, 95% CrI {r['or_lo']:.2f}–{r['or_hi']:.2f}, "
                     f"log10 BF10 = {r['log10_bf10']:.1f}, {int(r['n_studies'])} studies)\n")
    L.append("\n### Participant-level reliability of each construct\n\n")
    L.append(theta.to_markdown(index=False))
    L.append("\n\n## 2. Moderating role\n")
    L.append("All models carry a study random intercept; the moderator is standardised within "
             "study and the outcome is standardised across the pooled sample, so a coefficient "
             "is *SD of the outcome per SD of the construct* (model D: odds ratio per SD).  "
             "Model A needs both arms; B, C and D run on the intervention arm.\n")
    for name, tab, note in (("A. Between-arm moderation  `y_post ~ arm * z`", lvl,
                             "`interaction` = how much the intervention effect on the outcome "
                             "changes per SD of the construct."),
                            ("B. Within the intervention arm  `y_post ~ z`", within, ""),
                            ("C. Pre→post change (intervention arm)  `delta ~ z`", change, ""),
                            ("D. Reliable decline (intervention arm)  `logit ~ z`", backfire, "")):
        L.append(f"\n### Model {name}\n")
        if note:
            L.append(note + "\n")
        L.append("\n" + tab.to_markdown(index=False) + "\n")
    if sign_summ is not None and len(sign_summ):
        L.append("\n## Sign audit (within the intervention arm)\n")
        L.append("_Is each study's construct–outcome association in the theoretically "
                 "expected direction?_ A study below 100% is either genuine heterogeneity or "
                 "a mis-oriented harmonised column (both attenuate the pooled estimate).\n\n")
        L.append(sign_summ.to_markdown(index=False))
    L.append("\n\n## Orientation audit (political ideology)\n")
    L.append("Direction read from each sidecar's notes; unknowable directions are dropped "
             "from the directional conservatism row and retained only through the "
             "direction-free extremity variant.\n\n")
    L.append(direction.to_markdown(index=False) if len(direction) else "_none_")
    if len(errs):
        L.append("\n\n## Model / read errors\n\n" + errs.to_markdown(index=False))
    L.append("\n\n## Caveats\n")
    L.append("- Most of the pooled data are **posttest-only** (27 of the pre/post-or-single "
             "studies measure at post only), so models A and B rest on far more participants "
             "than C and D, which need a pretest.\n")
    L.append("- Model A is the causal one (randomisation × moderator); B and C are within-arm "
             "and descriptive of *who improves or ends up worse*, not of a randomised effect.\n")
    L.append("- Political conservatism is the only construct whose scale direction varies; it "
             "is oriented from the sidecar notes and studies with an unstated direction are "
             "excluded from that row (and listed above).  A direction-free "
             "**ideological-extremity** variant (|z| of the same scale) is reported alongside "
             "it, so those studies are not lost.\n")
    L.append("- `trust_in_science`, `conspiracy_belief`, `intellectual_humility`, "
             "`need_for_closure` and the epistemic-openness measures are present in few "
             "studies, so their estimates are between-study and cannot generalise.\n")
    L.append("- The sign audit shows where a construct's within-study association runs against "
             "its theoretical direction, which points to mis-oriented or reversed harmonised "
             "columns.  Two examples worth knowing: `conspiracy_belief` matches the expected "
             "direction in 82% of studies on false-news trust and discernment but only 36% on "
             "true-news trust (median r ≈ 0), and `trust_in_science` matches on true-news trust "
             "in 100% of studies but only 25% on false-news trust (median r = +0.10, i.e. higher "
             "trust in science goes with *more* false-news endorsement).  The two largest "
             "opposite-sign conspiracy studies (`Biddlestone et al. (2025 b)`, "
             "`Linegar et al. (2025)`) are flagged in the Sign_Audit sheet; reversed columns "
             "attenuate the pooled estimates rather than inflate them.\n")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join(L))


# ---------------------------------------------------------------------------
# 7. main
# ---------------------------------------------------------------------------
def main():
    print("=" * 78)
    print("MOTIVATED REASONING / TRUST IN SCIENCE — moderators of intervention effects")
    print("=" * 78)

    cen, det = census()
    print(f"\n[census] {cen['study_key'].nunique()} harmonised datasets")
    for m in CENSUS_MODS + list(CENSUS_ONLY):
        lab = MODERATORS[m]["label"] if m in MODERATORS else CENSUS_ONLY[m]["label"]
        print(f"   {lab:<58} {int(cen[m].sum()):>4} studies")

    pl, sample, direction, errs = build()
    print(f"\n[build] {len(pl)} participant-rows over {pl['study_key'].nunique()} studies "
          f"({int(pl['is_intervention'].sum())} intervention, "
          f"{int(pl['is_control'].sum())} control); "
          f"{sample['prepost'].sum()} studies with a pretest")
    pl.to_csv(os.path.join(RESULTS, "mr_person_level.csv"), index=False, encoding="utf-8-sig")

    # construct reliability table
    theta_rows = []
    for m in MODELED:
        if m not in pl.columns:
            continue
        x = pd.to_numeric(pl[m], errors="coerce")
        s = pl.loc[x.notna(), "study_key"].nunique()
        theta_rows.append({"moderator": MODERATORS[m]["label"], "n": int(x.notna().sum()),
                           "pct_available": round(100 * x.notna().mean(), 1),
                           "n_studies": int(s),
                           "n_intervention_studies":
                               int(pl.loc[x.notna() & (pl["is_intervention"] == 1),
                                          "study_key"].nunique())})
    theta = pd.DataFrame(theta_rows)

    # models
    lvl_rows, within_rows, change_rows, back_rows, m_err = [], [], [], [], []
    for m in MODELED:
        if m not in pl.columns or pl[m].notna().sum() < 50:
            continue
        for ok, olab, favourable in OUTCOMES:
            rA = model_A(pl, m, ok)
            if rA:
                for r in rA["rows"]:
                    lvl_rows.append({"moderator": MODERATORS[m]["label"], "outcome": olab,
                                     **r, "n": rA["n"], "n_studies": rA["n_studies"],
                                     "n_intervention": rA["n_intervention"],
                                     "n_control": rA["n_control"],
                                     "converged": rA["converged"]})
            else:
                m_err.append({"model": "A", "moderator": m, "outcome": ok, "reason": "not fit"})
            rB = model_B(pl, m, ok)
            if rB:
                within_rows.append({"moderator": MODERATORS[m]["label"], "outcome": olab,
                                    **rB})
            rC = model_C(pl, m, ok)
            if rC:
                change_rows.append({"moderator": MODERATORS[m]["label"], "outcome": olab, **rC})
            rD = model_D(pl, m, ok)
            if rD:
                back_rows.append({"moderator": MODERATORS[m]["label"], "outcome": olab, **rD})
    lvl = pd.DataFrame(lvl_rows)
    within = pd.DataFrame(within_rows)
    change = pd.DataFrame(change_rows)
    back = pd.DataFrame(back_rows)

    sign_tab, sign_summ = sign_audit(pl)

    # console summary
    print("\n--- A. moderation of the intervention effect (interaction, per SD) ---")
    if len(lvl):
        x = lvl[lvl["kind"] == "interaction"][
            ["outcome", "moderator", "estimate", "ci_lo", "ci_hi", "p", "n_studies"]]
        with pd.option_context("display.width", 200):
            print(x.round(3).to_string(index=False))
    print("\n--- D. reliable-decline moderation (OR per SD) ---")
    if len(back):
        x = back[["outcome", "moderator", "odds_ratio", "or_lo", "or_hi",
                  "p_direction", "log10_bf10", "n_events", "n_studies"]]
        with pd.option_context("display.width", 200):
            print(x.round(3).to_string(index=False))

    # figures
    def matrix(df, col, kind=None, logx=False, loc="ci_lo", hic="ci_hi"):
        mat = collections.OrderedDict()
        src = df if kind is None else df[df["kind"] == kind]
        for ok, olab, _ in OUTCOMES:
            rows = []
            sub = src[src["outcome"] == olab]
            for _, r in sub.iterrows():
                lo, hi = r[loc], r[hic]
                sig = (lo > (1 if logx else 0)) or (hi < (1 if logx else 0))
                rows.append((r["moderator"], r[col], lo, hi, sig))
            if rows:
                mat[ok] = sorted(rows, key=lambda t: t[1])
        return mat

    figs = [plot_census(cen)]
    if len(lvl):
        figs.append(forest_matrix(matrix(lvl, "estimate", "interaction"),
                                  os.path.join(FIGDIR, "mr_A_arm_moderation.png"),
                                  "A. Moderation of the intervention effect",
                                  "interaction (SD of outcome per SD of construct)"))
    if len(within):
        figs.append(forest_matrix(matrix(within, "estimate"),
                                  os.path.join(FIGDIR, "mr_B_within_intervention.png"),
                                  "B. Outcome vs construct, intervention arm",
                                  "slope (SD per SD)"))
    if len(change):
        figs.append(forest_matrix(matrix(change, "estimate"),
                                  os.path.join(FIGDIR, "mr_C_change.png"),
                                  "C. Pre→post change vs construct, intervention arm",
                                  "slope (SD per SD)"))
    if len(back):
        figs.append(forest_matrix(matrix(back, "odds_ratio", logx=True,
                                          loc="or_lo", hic="or_hi"),
                                  os.path.join(FIGDIR, "mr_D_reliable_decline.png"),
                                  "D. Reliable decline vs construct, intervention arm",
                                  "odds ratio per SD (log scale)", logx=True))

    # workbook
    xl = os.path.join(RESULTS, "motivated_reasoning_moderation.xlsx")
    with pd.ExcelWriter(xl, engine="xlsxwriter") as xw:
        cen.to_excel(xw, sheet_name="Census", index=False)
        det.to_excel(xw, sheet_name="Census_Detail", index=False)
        theta.to_excel(xw, sheet_name="Construct_Availability", index=False)
        sample.to_excel(xw, sheet_name="Person_Sample", index=False)
        lvl.to_excel(xw, sheet_name="Mod_Level_ArmMod", index=False)
        within.to_excel(xw, sheet_name="Mod_Within_Intervention", index=False)
        change.to_excel(xw, sheet_name="Mod_Change", index=False)
        back.to_excel(xw, sheet_name="Mod_Backfire", index=False)
        if len(sign_summ):
            sign_summ.to_excel(xw, sheet_name="Sign_Consistency", index=False)
            sign_tab.to_excel(xw, sheet_name="Sign_Audit", index=False)
        direction.to_excel(xw, sheet_name="Direction_Audit", index=False)
        if len(errs):
            errs.to_excel(xw, sheet_name="Read_Errors", index=False)
        if m_err:
            pd.DataFrame(m_err).to_excel(xw, sheet_name="Model_Errors", index=False)
        notes = pd.DataFrame([
            {"item": "scope", "value": "studies with an endorsement DV on BOTH true and "
                                       "false stimuli (domains credibility/trust/"
                                       "belief_accuracy; classify_credibility_scale=='belief') "
                                       "and >=1 motivated-reasoning / trust-in-science covariate"},
            {"item": "DV composite", "value": "tier 1 = stem matched across veracities "
                                              "(shortest stem); tier 2 = all columns of one "
                                              "domain for that veracity (reported per study)"},
            {"item": "timepoint", "value": "post = 'post'/'single'/unspecified; pre = 'pre'; "
                                           "followup used only when no post exists"},
            {"item": "orientation", "value": "all constructs higher = more; political ideology "
                                             "orientation read from sidecar notes, unknown "
                                             "directions dropped (Direction_Audit); a "
                                             "direction-free ideological-extremity variant "
                                             "(|z| of the ideology scale) is analysed in "
                                             "parallel so those studies still contribute"},
            {"item": "standardisation", "value": "outcome and moderator z-scored within study, so "
                                                 "coefficients are SD-per-SD"},
            {"item": "model A", "value": "y_post ~ arm * z + (1|study), studies with both arms"},
            {"item": "models B/C/D", "value": "intervention arm only; C/D need a pretest "
                                              "(pre/post studies)"},
            {"item": "model D", "value": "Bayesian logistic (VB) of reliable decline, RCI "
                                         "threshold per study x outcome; BF10 via Savage-Dickey"},
            {"item": "exclusions", "value": "studies excluded wholesale by "
                                            "Scripts/study_exclusions.py (experiment '*') are "
                                            "dropped"},
        ])
        notes.to_excel(xw, sheet_name="Notes", index=False)
    print(f"\nwrote {os.path.relpath(xl, B)}")
    markdown(os.path.join(RESULTS, "motivated_reasoning_report.md"),
             cen, det, sample, lvl, within, change, back, direction, errs, theta,
             sign_summ=sign_summ)
    print("wrote Results/motivated_reasoning_report.md")
    print("figures:", *[os.path.relpath(f, B) for f in figs if f], sep="\n  ")


if __name__ == "__main__":
    main()
