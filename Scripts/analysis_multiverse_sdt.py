"""SDT-layer multiverse analysis suite.

Entry point for the signal-detection (SDT) half of `Scripts/ANALYSIS_SUITE_SPEC.md`.
It re-analyses the harmonized datasets (`Datasets/<Key>/<Key> - harmonized.csv` +
`<Key> - harmonized_variables.csv`) under the four specifications of the suite:

    did_all    pretest-corrected (DiD), all misinformation-susceptibility domains
    did_cred   pretest-corrected (DiD), credibility only (sharing fallback)
    post_all   immediate posttest, all misinformation-susceptibility domains
    post_cred  immediate posttest, credibility only (sharing fallback)
    persistence_all   follow-up only, all misinformation-susceptibility domains
    persistence_cred  follow-up only, credibility only (sharing fallback)

The immediate posttest and the follow-up are separate questions and are never pooled
together; pretest rows are used only as the DiD baseline.

CONTRAST DEFINITION (decided 2026-09-17, see `Scripts/ANALYSIS_SUITE_NOTE.md`)
------------------------------------------------------------------------------
The SDT contrast is the **arm-based intervention effect on discrimination**, where a
participant's discrimination is the linear twin of `d' = z(HR) - z(FAR)` on the
oriented 0-1 scores,

    d_i = mean(oriented DV | veracity=true) + mean(oriented DV | veracity=false) - 1
        = HR - FAR

(`HR` = oriented true mean, `CR` = oriented false mean = correct rejection,
`FAR = 1 - CR`), within the study x artifact x domain x timepoint cell:

    post  =  d_IG,post - d_CG,post
    did   =  (d_IG,post - d_IG,pre) - (d_CG,post - d_CG,pre)

with `d` the arm mean of `d_i`, the change scores computed on participants matched by
`pid` (so the study's **actual within-person pre-post correlation** enters the variance),
and the effect standardised by the pooled SD of the analysis sample
(`sd_pool` of the participant-level `d_i` for post, of the participant-level change `Delta`
for did).  Every contrast carries its inputs (`d_ig`, `d_cg`, `sd_pool`, raw effect, `vi`,
`r_used`, n by arm).  `reanalysis.py`'s pooled all-arm discrimination is NOT used as the
effect (it is a discrimination level, not a treatment effect); the two are deliberately
different and the distinction is documented in the note.

Arms are read from the harmonized `condition`/`condition_raw` labels with an explicit,
documented classifier.  A study x artifact without an identifiable control (single-arm
designs, purely numeric arm codes that are not a two-level 0/1 set) is skipped and counted.

ROBUSTNESS
----------
Every specification is run with the author exclusion rules ON and OFF
(`reanalysis.py --no-exclusions` semantics; the rules in `<Key> - exclusions.csv` are
applied to the long data before the participant composites are formed).

POOLING
-------
Contrasts are nested in studies.  Primary: a two-level random-effects model
(effect within study, two-stage DL).  Sensitivity: cluster-robust SEs (study clusters) and
a single-level DL for reference.

PUBLICATION BIAS
----------------
Per spec x exclusions: Egger regression + intercept, PET-PEESE (WLS on SE and SE^2),
trim-and-fill (Duval-Tweedie L0), leave-one-study-out influence.  p-curve/p-uniform is
skipped (optional in the spec) because only a subset of contrasts has usable p-values.
A small-study caveat is recorded for the SDT layer (discrimination is variance-dependent
by construction).

MODERATOR
---------
`data_available` vs `paper_only` is NOT estimated here: by construction every SDT contrast
comes from a study with usable data.  Per the 2026-09-17 decision it belongs to the
reported-effects / paper layer only and is documented as such.

OUTPUTS
-------
    temp/sdt_specs.csv                 contrast-level table (all specs x exclusions)
    reanalysis.xlsx  (new sheets)      Suite_SDT_Contrasts, Suite_SDT_Results,
                                       Suite_SDT_PubBias, Suite_SDT_LOO
    data logs                          temp/sdt_skips.csv (studies/artifacts not analysable)
    figures/*.png|pdf                  forest / funnel / LOO / PET-PEESE / exclusions
    Scripts/ANALYSIS_SUITE_NOTE.md     the SDT section (delimited by markers)

Run:  python Scripts/analysis_multiverse_sdt.py
      python Scripts/analysis_multiverse_sdt.py --no-figures
      python Scripts/analysis_multiverse_sdt.py --no-note
      python Scripts/analysis_multiverse_sdt.py --force-write   # override the 15-min guard
"""
import glob
import math
import os
import re
import sys
import time
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)

import tempfile

B = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, B)
sys.path.insert(0, os.path.join(B, "Scripts"))
sys.path.insert(0, os.path.join(B, "Scripts", "analysis"))

_TMP = os.path.join(B, "temp")
os.makedirs(_TMP, exist_ok=True)
tempfile.tempdir = _TMP

import reanalysis as RA  # noqa: E402  (reuse dl_random / exclusions / meta sidecar)
import domain_restriction as DR  # noqa: E402
import study_exclusions as SE  # noqa: E402
import suite_common as SC  # noqa: E402  (explicit per-study condition mappings)

try:  # console-safe output for study names with non-cp1252 characters
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DS = os.path.join(B, "Datasets")
OUT_XLSX = os.path.join(B, "Results", "reanalysis.xlsx")
# Contract (AGENTS.md): analysis-suite figures live under Results/
FIGDIR = os.path.join(B, "Results", "figures")
SPECS_CSV = os.path.join(_TMP, "sdt_specs.csv")
SKIPS_CSV = os.path.join(_TMP, "sdt_skips.csv")
NOTE = os.path.join(B, "Scripts", "ANALYSIS_SUITE_NOTE.md")

SUSC = list(DR.SUSCEPTIBILITY_DOMAINS)
ALL_SPECS = ("post_all", "post_cred", "persistence_all", "persistence_cred",
             "did_all", "did_cred")
CRED = "credibility"
SHARE = "sharing_intention"
R_DEFAULT = 0.50
R_SENS = (0.30, 0.70)
RECENT_MIN = 0 if os.environ.get("SUITE_FORCE") else 15


# --------------------------------------------------------------------------- helpers
def recently_modified(path, minutes=RECENT_MIN):
    try:
        return (time.time() - os.path.getmtime(path)) < minutes * 60
    except OSError:
        return False


def norm_label(s):
    s = str(s).strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s


# Explicit, documented arm classifier.  Labels are normalised to snake_case and matched on
# token boundaries (so `partisan_content` is NOT a control).  A small secondary comparison set
# is used ONLY when no explicit control label is present; two-level numeric codes {0,1} use the
# documented 0=control / 1=intervention convention.
CTRL_TOKENS = {"control", "ctrl", "cont", "baseline", "placebo", "unassigned"}
CTRL_PREFIXES = ("kontrol",)
CTRL_PHRASES = ("no_intervention", "no_treatment", "no_information", "no_nudge", "no_debunk",
                "no_correction", "no_deliberation", "no_growth", "assessment_only",
                "pure_control", "passive_control", "scrambled_control", "active_control")
SECONDARY_CONTROL = ("misinformation", "misinformation_only", "2048game")
NO_ARM_TOKENS = {"single_arm", "single_survey", "survey1_single_group", "intervention1_all",
                 "within_subjects", "all_inoculation_single_arm", "unassigned", "ambiguous",
                 "nan", "none", "na", "missing", "control_only"}


def _is_control(n):
    toks = n.split("_")
    if any(t in CTRL_TOKENS for t in toks):
        return True
    if n.startswith(CTRL_PREFIXES):
        return True
    return any(n == p or n.startswith(p + "_") or n.endswith("_" + p) for p in CTRL_PHRASES)


def _is_no_arm(n):
    return any(t in NO_ARM_TOKENS for t in n.split("_"))


def classify_arms(labels):
    """Return {label: 'control'|'intervention'} for one study x artifact, or None.

    Returns None when the design has no identifiable control (single-arm / numeric codes
    that are not a two-level 0/1 set).  `labels` are the raw distinct condition values.
    """
    labs = [l for l in labels if not (isinstance(l, float) and math.isnan(l))]
    if not labs:
        return None
    norm = {l: norm_label(l) for l in labs}
    usable = [l for l in labs if not _is_no_arm(norm[l])]
    if not usable:
        return None
    ctrl = [l for l in usable if _is_control(norm[l])]
    if not ctrl:
        ctrl = [l for l in usable
                if any(norm[l] == t or norm[l].startswith(t) for t in SECONDARY_CONTROL)]
    # Crossed designs whose labels encode a factor that merely *contains* the word control
    # (e.g. Offer-Westort `H_control_R_accuracy`: control = headline type, R factor =
    # intervention) cannot be arm-classified from the label alone -> skip.
    if any(re.match(r"^h_.*_r_", n) for n in norm.values()):
        return None
    if not ctrl:
        # documented numeric 0/1 convention
        vals = set(norm[l] for l in usable)
        if vals in ({"0", "1"}, {"0.0", "1.0"}, {"0", "1.0"}, {"0.0", "1"}):
            return {l: ("control" if norm[l] in ("0", "0.0") else "intervention") for l in usable}
        return None
    m = {l: ("control" if l in ctrl else "intervention") for l in usable}
    if not any(v == "intervention" for v in m.values()):
        return None
    if not any(v == "control" for v in m.values()):
        return None
    return m


def _numeric_pair(labels):
    """True when the usable labels are exactly a two-level 0/1 numeric set."""
    vals = {norm_label(l) for l in labels
            if not (isinstance(l, float) and math.isnan(l))}
    return vals in ({"0", "1"}, {"0.0", "1.0"}, {"0", "1.0"}, {"0.0", "1"})


def classify_arms_mapped(labels, folder, key, artifact):
    """Return (arm_map, ambiguous, source) for one study x artifact.

    Uses the explicit ``Datasets/<Key>/<Key> - conditions.csv`` mapping when it
    exists (authoritative, ``ambiguous = 0``; ``control``/``active_control`` are
    pooled as the comparison arm, ``other``/unmapped labels are dropped).  Only
    when no mapping exists does it fall back to ``classify_arms``; a fallback
    that used the numeric 0/1 convention is flagged ``ambiguous = 1``.
    ``arm_map`` is None when no identifiable control/intervention pair exists.
    """
    cmap = SC.load_condition_map(folder, key) if folder and key else {}
    if cmap:
        m = {}
        for l in labels:
            if isinstance(l, float) and math.isnan(l):
                continue
            code = SC._norm_condition_code(l)
            role = None
            for art in (str(artifact), "*", ""):
                if (art, code) in cmap:
                    role = cmap[(art, code)]
                    break
            if role in ("control", "active_control"):
                m[l] = "control"
            elif role == "intervention":
                m[l] = "intervention"
            else:
                m[l] = None            # 'other' or unmapped -> not an arm
        if (not any(v == "control" for v in m.values())
                or not any(v == "intervention" for v in m.values())):
            return None, 0, "conditions.csv"
        return m, 0, "conditions.csv"
    am = classify_arms(labels)
    amb = 1 if (am is not None and _numeric_pair(labels)) else 0
    return am, amb, "label-heuristic"


def pick_condition(g):
    """Return the condition Series with the most usable labels (condition, else raw)."""
    best, bestn = None, -1
    for c in ("condition", "condition_raw"):
        if c not in g.columns:
            continue
        s = g[c]
        n = s.notna().sum() - (s.astype(str).str.strip().str.lower() == "nan").sum()
        if n > bestn:
            best, bestn = s, n
    return best


def pooled_sd(a, b):
    a = np.asarray(a, float); b = np.asarray(b, float)
    a = a[np.isfinite(a)]; b = b[np.isfinite(b)]
    na, nb = len(a), len(b)
    if na + nb < 3:
        return np.nan
    sp2 = ((na - 1) * np.var(a, ddof=1) + (nb - 1) * np.var(b, ddof=1)) / (na + nb - 2)
    return math.sqrt(sp2) if sp2 > 0 else np.nan


# --------------------------------------------------------------------------- data build
def build_study(folder, exclusions_on, with_components=False):
    """Return (comp_df, skip_rows) for one study.

    comp_df columns: study_key, artifact, domain, timepoint, arm, pid, comp
    """
    key = os.path.basename(folder)
    skips = []
    wide = pd.read_csv(os.path.join(folder, key + " - harmonized.csv"),
                       encoding="utf-8-sig", low_memory=False)
    meta = RA.meta_for(folder)
    rules = RA.load_exclusions(folder)
    ex_labels = SE.excluded_labels(key)
    if "pid" not in wide.columns:
        wide = wide.assign(pid=wide.index)
    frames = []
    for art, g in wide.groupby("artifact", dropna=False):
        art = "" if pd.isna(art) else str(art)
        if art in ex_labels:
            skips.append(dict(study_key=key, artifact=art, reason="non-independent experiment"))
            continue
        cond = pick_condition(g)
        if cond is None:
            skips.append(dict(study_key=key, artifact=art, reason="no condition column"))
            continue
        labels = list(pd.unique(cond.dropna()))
        arm_map, cond_amb, cond_src = classify_arms_mapped(labels, folder, key, art)
        if arm_map is None:
            skips.append(dict(study_key=key, artifact=art, reason="no identifiable control arm"))
            continue
        g = g.copy()
        g["arm"] = cond.map(lambda v: arm_map.get(v, None))
        dropped_arm = int(g["arm"].isna().sum())
        g = g[g["arm"].notna()]
        if not len(g):
            continue
        dvcols = [c for c in g.columns if str(c).startswith("dv_") and str(c).endswith("_01")]
        if not dvcols:
            skips.append(dict(study_key=key, artifact=art, reason="no dv_*_01 columns"))
            continue
        flagcols = [c for c in g.columns if str(c).startswith("flag_")]
        idcols = ["pid", "arm"] + flagcols
        long = g.melt(id_vars=idcols, value_vars=dvcols, var_name="dv_01", value_name="value_0_1")
        long = long.dropna(subset=["value_0_1"])
        if not len(long):
            continue
        long["dv"] = long["dv_01"].str[:-3]
        m = long["dv"].map(lambda v: meta.get(v, ("", "", "", "")))
        long["domain"] = [x[0] for x in m]
        long["veracity"] = [str(x[1]).strip().lower() for x in m]
        long["timepoint"] = ["" if (x[2] is None or str(x[2]) == "nan") else str(x[2]) for x in m]
        long["polarity"] = [str(x[3]).strip().lower() for x in m]
        # Confidence / certainty / metacognition: separate strand, never the SDT pool.
        _meta_mask = long["domain"].map(lambda d: SC.is_metacognition(d))
        if _meta_mask.any():
            skips.append(dict(study_key=key, artifact=art,
                              reason=f"{int(_meta_mask.sum())} confidence/metacognition rows routed out"))
            long = long[~_meta_mask]
        long = long[long["veracity"].isin(["true", "false"])]
        if not len(long):
            skips.append(dict(study_key=key, artifact=art, reason="no true/false veracity DVs"))
            continue
        long["dv_domain"] = long["domain"]
        rev = long["polarity"].isin(["higher_is_worse", "lower_is_better", "reverse",
                                     "reversed", "negative"])
        long.loc[rev, "value_0_1"] = 1.0 - long.loc[rev, "value_0_1"]
        if exclusions_on and rules:
            long = RA.apply_exclusions(long, rules)
            if not len(long):
                skips.append(dict(study_key=key, artifact=art, reason="empty after exclusions"))
                continue
        piv = (long.groupby(["domain", "timepoint", "arm", "pid", "veracity"])["value_0_1"]
               .mean().unstack("veracity"))
        piv = piv.dropna(subset=[c for c in ("true", "false") if c in piv.columns])
        if not {"true", "false"}.issubset(piv.columns) or not len(piv):
            skips.append(dict(study_key=key, artifact=art, reason="no participant with both veracities"))
            continue
        piv = piv.reset_index()
        # SDT discrimination composite.  The sidecar polarity orients every DV to
        # "higher = correct given its veracity" (true -> hit, false -> correct
        # reject), so the false-veracity value is already a correct-rejection rate
        # (CR), not a false-alarm rate (FAR = 1 - CR).  The linear twin of
        # d' = z(HR) - z(FAR) is therefore HR - FAR = HR + CR - 1, i.e. the SUM of
        # the two oriented means minus 1 -- NOT their difference.  (Using
        # true - false turns the discrimination into true+false-1's complement and
        # was a regression from the veracity-dependent polarity migration; see
        # Scripts/dv_polarity.py and Scripts/REANALYSIS_VS_META_SCALE_NOTE.md.)
        piv["comp"] = piv["true"] + piv["false"] - 1.0
        piv["study_key"] = key
        piv["artifact"] = art
        piv["cond_ambiguous"] = int(cond_amb)
        piv["cond_source"] = cond_src
        if with_components:
            piv["true_score"] = piv["true"]
            piv["fake_score"] = piv["false"]
            piv["criterion"] = -0.5 * (SC._probit(piv["true"].to_numpy(float))
                                       + SC._probit(1.0 - piv["false"].to_numpy(float)))
        if dropped_arm:
            skips.append(dict(study_key=key, artifact=art,
                              reason=f"{dropped_arm} rows with unassignable condition dropped"))
        keep_cols = ["study_key", "artifact", "domain", "timepoint", "arm", "pid", "comp",
                     "cond_ambiguous", "cond_source"]
        if with_components:
            keep_cols += ["true_score", "fake_score", "criterion"]
        frames.append(piv[keep_cols])
    if not frames:
        return pd.DataFrame(columns=["study_key", "artifact", "domain", "timepoint", "arm", "pid",
                                     "comp", "cond_ambiguous", "cond_source"]), skips
    return pd.concat(frames, ignore_index=True), skips


# --------------------------------------------------------------------------- contrasts
def _post_compute(comp, domain, tp):
    sub = comp[(comp["domain"] == domain) & (comp["timepoint"] == tp)]
    ig = sub[sub["arm"] == "intervention"]["comp"].to_numpy(float)
    cg = sub[sub["arm"] == "control"]["comp"].to_numpy(float)
    if len(ig) < 2 or len(cg) < 2:
        return None
    sdp = pooled_sd(ig, cg)
    raw = float(np.mean(ig) - np.mean(cg))
    var = np.var(ig, ddof=1) / len(ig) + np.var(cg, ddof=1) / len(cg)
    if not np.isfinite(sdp) or sdp <= 0 or not np.isfinite(var) or var <= 0:
        return None
    out = dict(d_ig=float(np.mean(ig)), d_cg=float(np.mean(cg)), n_ig=len(ig), n_cg=len(cg),
               effect_raw=raw, sd_pool=sdp, effect=raw / sdp, vi=var / sdp ** 2,
               r_used=np.nan, n_paired_ig=len(ig), n_paired_cg=len(cg))
    # SDT decomposition as arm contrasts (IG - CG) when the participant-level
    # components are available, so the app's true/fake/criterion moderation also
    # targets the *intervention effect* on the components, not a pooled level.
    for col, key in (("true_score", "true_score"), ("fake_score", "fake_score"),
                     ("criterion", "criterion_score")):
        if col not in sub.columns:
            continue
        a = sub[sub["arm"] == "intervention"][col].to_numpy(float)
        b = sub[sub["arm"] == "control"][col].to_numpy(float)
        if len(a) >= 2 and len(b) >= 2:
            out[key] = float(np.mean(a) - np.mean(b))
            out[key.replace("_score", "_var")] = float(
                np.var(a, ddof=1) / len(a) + np.var(b, ddof=1) / len(b))
    return out


def _did_compute(comp, domain):
    pre = comp[(comp["domain"] == domain) & (comp["timepoint"] == "pre")]
    post = comp[(comp["domain"] == domain) & (comp["timepoint"] == "post")]
    if not len(pre) or not len(post):
        return None
    comp_cols = [c for c in ("comp", "true_score", "fake_score", "criterion")
                 if c in pre.columns and c in post.columns]
    out = {}
    for arm in ("intervention", "control"):
        a = pre[pre["arm"] == arm][["pid"] + comp_cols].rename(
            columns={c: "pre_" + c for c in comp_cols})
        b = post[post["arm"] == arm][["pid"] + comp_cols].rename(
            columns={c: "post_" + c for c in comp_cols})
        mg = a.merge(b, on="pid", how="inner")
        if len(mg) < 2:
            return None
        for c in comp_cols:
            mg["delta_" + c] = mg["post_" + c] - mg["pre_" + c]
        out[arm] = mg
    ig, cg = out["intervention"], out["control"]
    sdp = pooled_sd(ig["delta_comp"], cg["delta_comp"])
    raw = float(ig["delta_comp"].mean() - cg["delta_comp"].mean())
    var = np.var(ig["delta_comp"], ddof=1) / len(ig) + np.var(cg["delta_comp"], ddof=1) / len(cg)
    if not np.isfinite(sdp) or sdp <= 0 or not np.isfinite(var) or var <= 0:
        return None
    # actual within-person pre-post correlation (pooled over arms)
    rs = []
    for mg in (ig, cg):
        if len(mg) >= 3 and mg["pre_comp"].std(ddof=1) > 0 and mg["post_comp"].std(ddof=1) > 0:
            rs.append(float(np.corrcoef(mg["pre_comp"], mg["post_comp"])[0, 1]))
    r_used = float(np.mean(rs)) if rs else R_DEFAULT
    res = dict(d_ig=float(ig["delta_comp"].mean()), d_cg=float(cg["delta_comp"].mean()),
               n_ig=len(ig), n_cg=len(cg), effect_raw=raw, sd_pool=sdp,
               effect=raw / sdp, vi=var / sdp ** 2, r_used=r_used,
               n_paired_ig=len(ig), n_paired_cg=len(cg),
               d_ig_post=float(ig["post_comp"].mean()), d_cg_post=float(cg["post_comp"].mean()),
               d_ig_pre=float(ig["pre_comp"].mean()), d_cg_pre=float(cg["pre_comp"].mean()))
    # Same decomposition for the pretest-corrected contrast: the arm difference in
    # the per-participant pre->post change of each component (IG - CG).
    for col, key in (("true_score", "true_score"), ("fake_score", "fake_score"),
                     ("criterion", "criterion_score")):
        dc = "delta_" + col
        if dc not in ig.columns:
            continue
        a = ig[dc].to_numpy(float)
        b = cg[dc].to_numpy(float)
        if len(a) >= 2 and len(b) >= 2:
            res[key] = float(np.mean(a) - np.mean(b))
            res[key.replace("_score", "_var")] = float(
                np.var(a, ddof=1) / len(a) + np.var(b, ddof=1) / len(b))
    return res


def _domain_post_tp(sub, domain):
    tps = set(sub[sub["domain"] == domain]["timepoint"])
    if "post" in tps:
        return "post"
    if "" in tps:
        return ""
    return None


def _domain_followup_tp(sub, domain):
    tps = set(sub[sub["domain"] == domain]["timepoint"])
    for cand in ("followup", "follow-up", "follow_up"):
        if cand in tps:
            return cand
    return None


def _domain_has_did(sub, domain):
    tps = set(sub[sub["domain"] == domain]["timepoint"])
    return "pre" in tps and "post" in tps


def contrasts_for(comp):
    """Yield one dict per (spec, study, artifact) contrast.
    Returns list of rows and a list of fallback/skip notes.
    """
    rows, notes = [], []
    keys = comp[["study_key", "artifact"]].drop_duplicates()
    for _, k in keys.iterrows():
        study, art = k["study_key"], k["artifact"]
        sub = comp[(comp["study_key"] == study) & (comp["artifact"] == art)]
        if not len(sub):
            continue
        doms = set(sub["domain"]) & set(SUSC)
        # ---- post specs -------------------------------------------------------
        for dom in sorted(doms):
            tp = _domain_post_tp(sub, dom)
            if tp is None:
                continue
            c = _post_compute(sub, dom, tp)
            if c:
                rows.append(dict(study_key=study, artifact=art, spec="post_all",
                                 domain=dom, timepoint=tp or "(blank=post)", **c))
        cdom = CRED if CRED in doms else (SHARE if SHARE in doms else None)
        if cdom and _domain_post_tp(sub, cdom) is not None:
            c = _post_compute(sub, cdom, _domain_post_tp(sub, cdom))
            if c:
                fb = cdom != CRED
                rows.append(dict(study_key=study, artifact=art, spec="post_cred",
                                 domain=cdom, timepoint=_domain_post_tp(sub, cdom) or "(blank=post)",
                                 fallback=fb, **c))
                if fb:
                    notes.append(dict(study_key=study, artifact=art, spec="post_cred",
                                      note="sharing_intention fallback (no credibility DV)"))
        # ---- persistence (follow-up only) specs -------------------------------
        for dom in sorted(doms):
            tp = _domain_followup_tp(sub, dom)
            if tp is None:
                continue
            c = _post_compute(sub, dom, tp)
            if c:
                rows.append(dict(study_key=study, artifact=art, spec="persistence_all",
                                 domain=dom, timepoint=tp, **c))
        if cdom and _domain_followup_tp(sub, cdom) is not None:
            tp = _domain_followup_tp(sub, cdom)
            c = _post_compute(sub, cdom, tp)
            if c:
                fb = cdom != CRED
                rows.append(dict(study_key=study, artifact=art, spec="persistence_cred",
                                 domain=cdom, timepoint=tp, fallback=fb, **c))
                if fb:
                    notes.append(dict(study_key=study, artifact=art, spec="persistence_cred",
                                      note="sharing_intention fallback (no credibility DV)"))
        # ---- did specs --------------------------------------------------------
        for dom in sorted(doms):
            if not _domain_has_did(sub, dom):
                continue
            c = _did_compute(sub, dom)
            if c:
                rows.append(dict(study_key=study, artifact=art, spec="did_all",
                                 domain=dom, timepoint="pre->post", **c))
        if cdom and _domain_has_did(sub, cdom):
            c = _did_compute(sub, cdom)
            if c:
                fb = cdom != CRED
                rows.append(dict(study_key=study, artifact=art, spec="did_cred",
                                 domain=cdom, timepoint="pre->post", fallback=fb, **c))
                if fb:
                    notes.append(dict(study_key=study, artifact=art, spec="did_cred",
                                      note="sharing_intention fallback (no credibility DV)"))
    flags = {}
    if len(comp) and "cond_ambiguous" in comp.columns:
        for (st, ar), gg in comp.groupby(["study_key", "artifact"]):
            flags[(st, ar)] = int(gg["cond_ambiguous"].max())
    for r in rows:
        r.setdefault("fallback", False)
        r["cond_ambiguous"] = flags.get((r["study_key"], r["artifact"]), 0)
        # SDT layer is ALWAYS a true-vs-false discrimination composite (never a
        # separate component score); the cluster identity is study x artifact.
        r["dv_family"] = SC.DV_FAMILY_DIFFERENCE
        r["cluster_id"] = SC.sdt_cluster_id(r["study_key"], r["artifact"])
    return rows, notes


# --------------------------------------------------------------------------- pooling
def two_level(yi, vi, study):
    y = np.asarray(yi, float); v = np.asarray(vi, float); s = np.asarray(study)
    m = np.isfinite(y) & np.isfinite(v) & (v > 0)
    y, v, s = y[m], v[m], s[m]
    if not len(y):
        return dict(k=0, k_studies=0, est=np.nan, se=np.nan, lo=np.nan, hi=np.nan,
                    tau2_within=np.nan, tau2_between=np.nan, i2=np.nan)
    ests, vs, tw = [], [], []
    for st in pd.unique(s):
        mm = s == st
        r = RA.dl_random(y[mm], v[mm])
        if r["k"] == 0:
            continue
        ests.append(r["est"]); vs.append(r["se"] ** 2); tw.append(r["tau2"])
    r2 = RA.dl_random(ests, vs)
    return dict(k=len(y), k_studies=len(ests), est=r2["est"], se=r2["se"], lo=r2["lo"],
                hi=r2["hi"], tau2_within=float(np.mean(tw)) if tw else np.nan,
                tau2_between=r2["tau2"], i2=r2["i2"])


def cluster_robust(yi, vi, study):
    y = np.asarray(yi, float); v = np.asarray(vi, float); s = np.asarray(study)
    m = np.isfinite(y) & np.isfinite(v) & (v > 0)
    y, v, s = y[m], v[m], s[m]
    if not len(y):
        return dict(k=0, k_studies=0, est=np.nan, se=np.nan, lo=np.nan, hi=np.nan)
    w = 1 / v
    est = float(np.sum(w * y) / np.sum(w))
    resid = y - est
    meat = 0.0
    for st in pd.unique(s):
        mm = s == st
        meat += float(np.sum(w[mm] * resid[mm])) ** 2
    G = len(pd.unique(s))
    if G < 2:
        return dict(k=len(y), k_studies=G, est=est, se=np.nan, lo=np.nan, hi=np.nan)
    var = meat / (np.sum(w) ** 2)
    var *= G / (G - 1)
    se = math.sqrt(var)
    return dict(k=len(y), k_studies=G, est=est, se=se, lo=est - 1.96 * se, hi=est + 1.96 * se)


def study_level(yi, vi, study):
    """Stage-1 within-study DL pool (one effect per study) for publication-bias methods."""
    y = np.asarray(yi, float); v = np.asarray(vi, float); s = np.asarray(study)
    m = np.isfinite(y) & np.isfinite(v) & (v > 0)
    y, v, s = y[m], v[m], s[m]
    out = []
    for st in pd.unique(s):
        mm = s == st
        r = RA.dl_random(y[mm], v[mm])
        if r["k"]:
            out.append(dict(study_key=st, yi=r["est"], vi=r["se"] ** 2, k=r["k"]))
    return pd.DataFrame(out)


# --------------------------------------------------------------------------- pub bias
def _wls(x, y, w):
    x = np.asarray(x, float); y = np.asarray(y, float); w = np.asarray(w, float)
    m = np.isfinite(x) & np.isfinite(y) & np.isfinite(w) & (w > 0)
    x, y, w = x[m], y[m], w[m]
    n = len(x)
    if n < 3 or np.var(x) == 0:
        return None
    X = np.column_stack([np.ones(n), x])
    W = np.diag(w)
    XtW = X.T @ W
    beta = np.linalg.solve(XtW @ X, XtW @ y)
    resid = y - X @ beta
    dof = n - 2
    sigma2 = float(resid @ W @ resid) / dof if dof > 0 else np.nan
    cov = sigma2 * np.linalg.inv(XtW @ X)
    se = math.sqrt(cov[0, 0]) if cov[0, 0] > 0 else np.nan
    return dict(intercept=float(beta[0]), slope=float(beta[1]), se=se,
                p=(2 * (1 - _norm_cdf(abs(beta[0] / se))) if se and se > 0 else np.nan), n=n)


def _norm_cdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def egger(y, se):
    m = np.isfinite(y) & np.isfinite(se) & (se > 0)
    y, se = y[m], se[m]
    if len(y) < 3:
        return None
    z = y / se
    x = 1.0 / se
    return _wls(x, z, x)


def pet_peese(y, se):
    m = np.isfinite(y) & np.isfinite(se) & (se > 0)
    y, se = y[m], se[m]
    pet = _wls(se, y, 1.0 / se ** 2) if len(y) >= 3 else None
    peese = _wls(se ** 2, y, 1.0 / se ** 2) if len(y) >= 3 else None
    return pet, peese


def _rank_ordinal(a):
    """R-style rank(ties.method='first'): 1-based ranks in order of appearance."""
    order = np.argsort(a, kind="stable")
    r = np.empty(len(a), float)
    r[order] = np.arange(1, len(a) + 1)
    return r


def trimfill(y, se):
    """Duval-Tweedie trim-and-fill (L0 estimator), ported from metafor::trimfill.rma.uni.

    Side is chosen from the sign of the Egger-type slope of effect on sqrt(vi); the data are
    flipped for a right-side trim, the L0 estimator iterates on the truncated fixed-effect
    centre, and the k0 most extreme studies are mirrored about that centre.  The adjusted
    estimate is the DL random-effects estimate on the filled data.
    """
    y = np.asarray(y, float); se = np.asarray(se, float)
    m = np.isfinite(y) & np.isfinite(se) & (se > 0)
    y, se = y[m], se[m]
    n = len(y)
    if n < 3:
        return None
    v = se ** 2
    w = 1.0 / v
    fit = _wls(np.sqrt(v), y, w)
    if fit and np.isfinite(fit["slope"]):
        side = "right" if fit["slope"] < 0 else "left"
    else:
        dev = y - np.sum(w * y) / np.sum(w)
        side = "right" if abs(dev.max()) >= abs(dev.min()) else "left"
    yf = -y.copy() if side == "right" else y.copy()
    ix = np.argsort(yf, kind="stable")
    yf = yf[ix]; vs = v[ix]; ws = w[ix]
    k0_sav, k0, it = -1, 0, 0
    while abs(k0 - k0_sav) > 0:
        k0_sav = k0
        it += 1
        if it > 100:
            break
        yt = yf[:n - k0] if k0 else yf
        wt = ws[:n - k0] if k0 else ws
        beta = float(np.sum(wt * yt) / np.sum(wt))
        yc = yf - beta
        rcs = np.sign(yc) * _rank_ordinal(np.abs(yc))
        Sr = float(np.sum(rcs[rcs > 0]))
        k0 = max(0, int(round((4 * Sr - n * (n + 1)) / (2 * n - 1))))
    # fill (mirror of the k0 most extreme, about the truncated centre)
    yc2 = (-1.0 * (yc - beta)) if side == "right" else (yc - beta)
    fill = -1.0 * yc2[(n - k0):] if k0 else np.array([])
    y_all = np.concatenate([y, fill])
    v_all = np.concatenate([v, vs[(n - k0):]]) if k0 else v
    r = RA.dl_random(y_all, v_all)
    return dict(k0=int(k0), adjusted=r["est"], lo=r["lo"], hi=r["hi"],
                k_adj=len(y_all), centre=beta, side=side, iters=it)


# --------------------------------------------------------------------------- per-spec run
def run_spec(df, spec, exclusions):
    d = df[(df["spec"] == spec) & (df["exclusions"] == exclusions)].copy()
    if not len(d):
        return None, None
    results = []
    for method, fn in (("two_level", lambda yy, vv, ss, cc: two_level(yy, vv, ss)),
                       ("cluster_robust", lambda yy, vv, ss, cc: cluster_robust(yy, vv, ss)),
                       ("dl_single", lambda yy, vv, ss, cc: RA.dl_random(yy, vv)),
                       ("three_level", lambda yy, vv, ss, cc: SC.three_level_re(yy, vv, cc, ss)),
                       ("rve_rho50", lambda yy, vv, ss, cc: SC.rve_intercept(yy, vv, cc, rho=0.5)),
                       ("rve_rho80", lambda yy, vv, ss, cc: SC.rve_intercept(yy, vv, cc, rho=0.8))):
        r = fn(d["effect"].to_numpy(float), d["vi"].to_numpy(float),
               d["study_key"].to_numpy(), d["cluster_id"].to_numpy())
        results.append(dict(spec=spec, exclusions=exclusions, group="ALL", method=method, **r))
        for dom in SUSC:
            g = d[d["domain"] == dom]
            if not len(g):
                continue
            r = fn(g["effect"].to_numpy(float), g["vi"].to_numpy(float),
                   g["study_key"].to_numpy(), g["cluster_id"].to_numpy())
            results.append(dict(spec=spec, exclusions=exclusions, group=f"domain: {dom}",
                                method=method, **r))
    res = pd.DataFrame(results)
    # publication bias on study-level effects
    sl = study_level(d["effect"], d["vi"], d["study_key"])
    pub_cols = ["spec", "exclusions", "method", "n", "est", "se", "p", "added", "lo", "hi"]
    loo_cols = ["spec", "exclusions", "omitted_study", "est", "se", "lo", "hi", "delta", "influential"]
    pub, loo = [], []
    if len(sl) >= 3:
        eg = egger(sl["yi"].to_numpy(), np.sqrt(sl["vi"].to_numpy()))
        pet, peese = pet_peese(sl["yi"].to_numpy(), np.sqrt(sl["vi"].to_numpy()))
        tf = trimfill(sl["yi"].to_numpy(), np.sqrt(sl["vi"].to_numpy()))
        for name, r in (("egger", eg), ("pet", pet), ("peese", peese)):
            if r:
                pub.append(dict(spec=spec, exclusions=exclusions, method=name,
                                n=r.get("n", len(sl)), est=r.get("intercept"),
                                se=r.get("se"), p=r.get("p"), added=np.nan,
                                lo=r["intercept"] - 1.96 * r["se"] if r.get("se") else np.nan,
                                hi=r["intercept"] + 1.96 * r["se"] if r.get("se") else np.nan))
        if tf:
            pub.append(dict(spec=spec, exclusions=exclusions, method="trimfill",
                            n=len(sl), est=tf["adjusted"], se=np.nan, p=np.nan,
                            added=tf["k0"], lo=tf["lo"], hi=tf["hi"]))
    full = two_level(d["effect"], d["vi"], d["study_key"])
    for st in pd.unique(d["study_key"]):
        gg = d[d["study_key"] != st]
        if not len(gg):
            continue
        rr = two_level(gg["effect"], gg["vi"], gg["study_key"])
        infl = bool(np.isfinite(rr["lo"]) and np.isfinite(rr["hi"]) and
                    not (rr["lo"] <= full["est"] <= rr["hi"]))
        loo.append(dict(spec=spec, exclusions=exclusions, omitted_study=st,
                        est=rr["est"], se=rr["se"], lo=rr["lo"], hi=rr["hi"],
                        delta=rr["est"] - full["est"], influential=int(infl)))
    return res, (pd.DataFrame(pub, columns=pub_cols),
                 pd.DataFrame(loo, columns=loo_cols), sl)


def dependence_rows(contrasts):
    """Per spec x exclusions x group: DL vs three-level vs RVE (rho 0.5/0.8) + weight share.

    The SDT layer is always a true-vs-false discrimination contrast, so the DV family
    is a single ``difference`` family and the cluster is study x artifact.
    """
    rows = []
    for spec in ALL_SPECS:
        for ex in ("ON", "OFF"):
            d = contrasts[(contrasts["spec"] == spec) & (contrasts["exclusions"] == ex)]
            if not len(d):
                continue
            groups = [("ALL", d)]
            for dom in SUSC:
                g = d[d["domain"] == dom]
                if len(g):
                    groups.append((f"domain: {dom}", g))
            for label, g in groups:
                dep = SC.pool_dependence(g["effect"], g["vi"], g["cluster_id"],
                                         g["study_key"])
                rows.append(dict(spec=spec, exclusions=ex, group=label, **dep))
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- figures
def _figdir():
    os.makedirs(FIGDIR, exist_ok=True)


def _save(fig, stem):
    fig.savefig(os.path.join(FIGDIR, stem + ".png"), dpi=300, bbox_inches="tight")
    fig.savefig(os.path.join(FIGDIR, stem + ".pdf"), bbox_inches="tight")
    import matplotlib.pyplot as plt
    plt.close(fig)


def forest(ax, df, label_col, est_col="est", lo_col="lo", hi_col="hi", title=""):
    ds = df.dropna(subset=[est_col]).iloc[::-1].reset_index(drop=True)
    ypos = np.arange(len(ds))
    xerr = np.vstack([ds[est_col] - ds[lo_col], ds[hi_col] - ds[est_col]])
    ax.errorbar(ds[est_col], ypos, xerr=xerr, fmt="o", color="#1f3b73", ms=3, lw=1)
    ax.axvline(0, color="grey", lw=0.8, ls="--")
    ax.set_yticks(ypos)
    ax.set_yticklabels(ds[label_col].astype(str), fontsize=6)
    ax.set_title(title, fontsize=8)
    ax.tick_params(axis="x", labelsize=7)


def make_figures(specs_df, results_df, pub_df, loo_df, contrasts):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    _figdir()
    # 1) forest per spec x exclusions (includes the persistence specs)
    for spec in ALL_SPECS:
        for ex in ("ON", "OFF"):
            r = results_df[(results_df["spec"] == spec) & (results_df["exclusions"] == ex)
                           & (results_df["method"] == "two_level")]
            if not len(r):
                continue
            fig, ax = plt.subplots(figsize=(6.5, max(1.5, 0.32 * len(r) + 1.2)))
            est = r["est"].iloc[0]
            forest(ax, r, "group", title=f"SDT {spec} — exclusions {ex} — "
                   f"k={int(r['k'].iloc[0])}, pooled={est:.3f}")
            _save(fig, f"forest_sdt_{spec}_excl{ex}")
    # 2) side-by-side all specs (posttest, persistence, DiD)
    n_specs = len(ALL_SPECS)
    fig, axes = plt.subplots((n_specs + 1) // 2, 2, figsize=(11, 2.6 * ((n_specs + 1) // 2) + 1))
    for ax, spec in zip(axes.ravel(), ALL_SPECS):
        r = results_df[(results_df["spec"] == spec) & (results_df["exclusions"] == "ON")
                       & (results_df["method"] == "two_level")]
        if len(r):
            forest(ax, r, "group", title=f"{spec}: pooled={r['est'].iloc[0]:.3f} (k={int(r['k'].iloc[0])})")
        else:
            ax.axis("off")
    for ax in axes.ravel()[n_specs:]:
        ax.axis("off")
    fig.suptitle("SDT layer — all specifications (exclusions ON)", fontsize=10)
    _save(fig, "forest_sdt_specs")
    # 3) funnel per spec
    for spec in ALL_SPECS:
        d = contrasts[(contrasts["spec"] == spec) & (contrasts["exclusions"] == "ON")]
        if len(d) < 3:
            continue
        se = np.sqrt(d["vi"]); yi = d["effect"]
        fig, ax = plt.subplots(figsize=(5.5, 4.5))
        ax.scatter(yi, se, s=8, color="#1f3b73")
        pooled = RA.dl_random(yi, d["vi"])
        ax.axvline(pooled["est"], color="crimson", lw=1)
        if np.isfinite(pooled["est"]):
            xxx = np.linspace(yi.min() - 0.2, yi.max() + 0.2, 50)
            ax.plot(xxx, np.sqrt(((xxx - pooled["est"]) / 1.96) ** 2), color="grey", lw=0.8)
        eg = pub_df[(pub_df["spec"] == spec) & (pub_df["exclusions"] == "ON")
                    & (pub_df["method"] == "egger")]
        ttl = f"SDT funnel {spec} (ON), k={len(d)}"
        if len(eg):
            ttl += f"\nEgger intercept={eg['est'].iloc[0]:.2f} (p={eg['p'].iloc[0]:.3f})"
        ax.set_xlabel("standardised effect"); ax.set_ylabel("SE"); ax.set_title(ttl, fontsize=8)
        _save(fig, f"funnel_sdt_{spec}")
    # 4) LOO per spec
    for spec in ALL_SPECS:
        d = loo_df[(loo_df["spec"] == spec) & (loo_df["exclusions"] == "ON")]
        if not len(d):
            continue
        fig, ax = plt.subplots(figsize=(6.5, max(1.5, 0.32 * len(d) + 1.2)))
        forest(ax, d, "omitted_study", title=f"SDT leave-one-study-out {spec} (ON)")
        _save(fig, f"loo_sdt_{spec}")
    # 5) PET-PEESE per spec
    for spec in ALL_SPECS:
        d = contrasts[(contrasts["spec"] == spec) & (contrasts["exclusions"] == "ON")]
        if len(d) < 3:
            continue
        se = np.sqrt(d["vi"]); yi = d["effect"]
        fig, ax = plt.subplots(figsize=(5.5, 4.5))
        ax.scatter(se, yi, s=8, color="#1f3b73")
        xs = np.linspace(0, se.max() * 1.05, 50)
        pet = pub_df[(pub_df["spec"] == spec) & (pub_df["exclusions"] == "ON") & (pub_df["method"] == "pet")]
        peese = pub_df[(pub_df["spec"] == spec) & (pub_df["exclusions"] == "ON") & (pub_df["method"] == "peese")]
        if len(pet):
            b0, b1 = pet["est"].iloc[0], np.nan
            # PET slope re-fit for the line
            rr = _wls(se, yi, 1 / se ** 2)
            ax.plot(xs, rr["intercept"] + rr["slope"] * xs, color="darkorange", lw=1, label="PET")
        if len(peese):
            rr = _wls(se ** 2, yi, 1 / se ** 2)
            ax.plot(xs, rr["intercept"] + rr["slope"] * xs ** 2, color="seagreen", lw=1, label="PEESE")
        ax.axhline(0, color="grey", lw=0.6, ls="--")
        ax.set_xlabel("SE"); ax.set_ylabel("effect"); ax.legend(fontsize=7)
        ax.set_title(f"SDT PET-PEESE {spec} (ON)", fontsize=8)
        _save(fig, f"petpeese_sdt_{spec}")
    # 6) exclusions ON vs OFF forest
    for spec in ALL_SPECS:
        r = results_df[(results_df["spec"] == spec) & (results_df["method"] == "two_level")]
        if not len(r):
            continue
        r = r.copy(); r["grp"] = r["exclusions"] + " | " + r["group"]
        fig, ax = plt.subplots(figsize=(6.5, max(1.5, 0.3 * len(r) + 1.2)))
        forest(ax, r, "grp", title=f"SDT {spec}: exclusions ON vs OFF")
        _save(fig, f"excl_onoff_{spec}")
    # 7) four-domain forest (post_all, ON)
    d = results_df[(results_df["spec"] == "post_all") & (results_df["exclusions"] == "ON")
                   & (results_df["method"] == "two_level")
                   & (results_df["group"].str.startswith("domain:"))]
    if len(d):
        fig, ax = plt.subplots(figsize=(6.5, 2.6))
        forest(ax, d, "group", title="SDT domain forest (post_all, ON)")
        _save(fig, "domain_forest_sdt")


# --------------------------------------------------------------------------- note
NOTE_START = "<!-- SDT-SUITE:START -->"
NOTE_END = "<!-- SDT-SUITE:END -->"


def write_note(contrasts, results, pub_df, skips, fallback_notes):
    lines = [NOTE_START, "", "## SDT layer — multiverse analysis suite", "",
             "_Generated by `Scripts/analysis_multiverse_sdt.py`._", ""]
    lines += ["### Contrast definition", "",
              "Arm-based intervention effect on discrimination. A participant's discrimination is the",
              "linear twin of the signal-detection index,",
              "`d_i = mean(oriented DV | veracity=true) + mean(oriented DV | veracity=false) - 1`",
              "`= HR - FAR` within the study x artifact x domain x timepoint cell (polarity rule",
              "applied: `higher_is_worse` -> `1 - x`, so the false-veracity mean is the correct-",
              "rejection rate `CR` and `FAR = 1 - CR`). Arm means `d_arm = mean(d_i)`. Then",
              "",
              "* `post = d_IG,post - d_CG,post`",
              "* `did  = (d_IG,post - d_IG,pre) - (d_CG,post - d_CG,pre)`, change scores on",
              "  participants matched by `pid` (so the empirical within-person pre-post correlation",
              "  enters the variance; the value is recorded per contrast as `r_used`).",
              "",
              "Effects are standardised by the pooled SD of the analysis sample (`sd_pool` of the",
              "participant-level `d_i` for post, of the participant-level change for did). This is",
              "deliberately different from `reanalysis.py`'s pooled all-arm discrimination, which is",
              "a discrimination level rather than a treatment effect.", "",
              "The 2026-09-17 decision was arm-based (Definition A); the earlier `PUBBIAS-GRAPHICS`",
              "section's SDT rows used a pooled-over-arms `post` and should be regenerated from",
              "`temp/sdt_specs.csv`, which this script now writes in the shared `suite_common`",
              "schema.", ""]
    lines += ["### Arms and coverage", "",
              "Arms are read first from the study's explicit `Datasets/<Key>/<Key> - conditions.csv`",
              "mapping (columns `code, role, label, evidence, artifact`; role in",
              "control|active_control|intervention|other). That mapping is authoritative and never",
              "flagged: `control`/`active_control` form the comparison arm, `intervention` the",
              "treatment arm, `other`/unmapped codes are dropped. Only when a study has no mapping",
              "does the legacy label heuristic run (explicit control labels containing",
              "control/ctrl/kontrol/placebo/baseline/no_*/assessment_only/...; a secondary set",
              "`misinformation`, `misinformation_only`, `2048game`); a purely numeric two-level",
              "`0/1` set then uses the documented 0=control/1=intervention convention, and every",
              "such contrast carries `cond_ambiguous = 1`. Single-arm designs are skipped and",
              "listed in `temp/sdt_skips.csv`.", ""]
    sk = pd.DataFrame(skips)
    if len(sk):
        vc = sk["reason"].value_counts()
        lines += [f"Studies/artifacts skipped: **{len(sk)}** rows in `temp/sdt_skips.csv`.", "",
                  "| reason | rows |", "|---|---:|"]
        for r, n in vc.items():
            lines.append(f"| {str(r)[:110]} | {int(n)} |")
        lines += [""]
    lines += ["### Contrasts per specification (exclusions ON)", ""]
    lines += ["| spec | contrasts | studies | median r_used | fallbacks |", "|---|---:|---:|---:|---:|"]
    for spec in ALL_SPECS:
        d = contrasts[(contrasts["spec"] == spec) & (contrasts["exclusions"] == "ON")]
        if len(d):
            med = d["r_used"].median()
            med_s = "–" if not np.isfinite(med) else f"{med:.3f}"
            lines.append(f"| {spec} | {len(d)} | {d['study_key'].nunique()} | "
                         f"{med_s} | {int(d['fallback'].sum())} |")
        else:
            lines.append(f"| {spec} | 0 | 0 | – | – |")
    lines += ["", "### Pooled results (two-level RE, exclusions ON vs OFF)", "",
              "| spec | excl | group | k | k studies | est | 95% CI | tau2_between |",
              "|---|---|---|---:|---:|---:|---|---:|"]
    rt = results[results["method"] == "two_level"]
    for _, r in rt.iterrows():
        lines.append(f"| {r['spec']} | {r['exclusions']} | {r['group']} | {int(r['k'])} | "
                     f"{int(r['k_studies'])} | {r['est']:.3f} | "
                     f"[{r['lo']:.3f}, {r['hi']:.3f}] | {r['tau2_between']:.4f} |")
    lines += ["", "### Publication bias (study-level, exclusions ON)", "",
              "| spec | method | est | 95% CI | p | added k |", "|---|---|---:|---|---:|---:|"]
    pb = pub_df[pub_df["exclusions"] == "ON"]
    for _, r in pb.iterrows():
        p = "" if not np.isfinite(r["p"]) else f"{r['p']:.3f}"
        added = "" if not np.isfinite(r["added"]) else int(r["added"])
        ci = ("" if not (np.isfinite(r["lo"]) and np.isfinite(r["hi"]))
              else f"[{r['lo']:.3f}, {r['hi']:.3f}]")
        lines.append(f"| {r['spec']} | {r['method']} | {r['est']:.3f} | {ci} | {p} | {added} |")
    lines += ["",
              "**Small-study caveat (SDT).** Discrimination is variance-dependent by construction",
              "(it subtracts two condition means), so funnel asymmetry / Egger intercepts are",
              "expected to be partly mechanical; PET-PEESE and trim-and-fill are reported as",
              "sensitivity checks, not as clean tests of publication bias.", "",
              "### Sensitivity and omitted methods", "",
              "* **Cluster-robust SE (study clusters)** is reported in `Suite_SDT_Results`",
              "  (`method = cluster_robust`) alongside the two-level estimate. It is wider because",
              "  contrasts within a study share a sample; for `post_all` the cluster-robust CI",
              "  includes 0 while the two-level CI does not, so the study-level evidence is weaker",
              "  than the nominal contrast-level precision suggests.",
              "* **Leave-one-study-out** is in `Suite_SDT_LOO`; under exclusions ON no omitted",
              "  study's LOO CI excluded the full-sample estimate for any spec (no single",
              "  influential study), consistent with the wide between-study heterogeneity.",
              "* **Pre-post r**: the SDT DiD uses each study's actual within-person correlation,",
              "  recorded per contrast (`r_used`; medians in the table above). The r = 0.30 / 0.70",
              "  sensitivity is not applicable to this layer (it is a meta-layer device for studies",
              "  without raw data).",
              "* **p-curve / p-uniform** (optional in the spec) is skipped: the discrimination index",
              "  is a variance-dependent composite and only a subset of contrasts has a meaningful",
              "  p-value; it is not reported as a bias test here.",
              "* **Immediate post vs follow-up**: the three questions are separate and are never",
              "  pooled. `post_*` uses the immediate posttest (follow-ups excluded); `persistence_*`",
              "  uses follow-up only; `did_*` needs `pre` and `post`. Pretest rows are used only as",
              "  the DiD baseline and are never outcomes.", "",
              "**Moderator.** `data_available` vs `paper_only` is NOT estimated in the SDT layer:",
              "every SDT contrast necessarily comes from a study with usable participant data, so",
              "the subgroup contrast is degenerate here. By the 2026-09-17 decision the moderator",
              "belongs to the reported-effects / paper layer only.", ""]
    if fallback_notes:
        uniq, seen = [], set()
        for n in fallback_notes:
            k = (n["study_key"], n["artifact"], n["spec"], n["note"])
            if k not in seen:
                seen.add(k); uniq.append(n)
        lines += ["### Fallback notes (credibility -> sharing_intention)", ""]
        for n in uniq[:300]:
            lines.append(f"- {n['study_key']} / {n['artifact']} / {n['spec']}: {n['note']}")
        lines += ["", f"Total fallback contrasts: {len(uniq)} (recorded per contrast in "
                      "`temp/sdt_specs.csv` via `fallback = sharing_intention`).", ""]
    lines += ["### Outputs", "",
              "* `temp/sdt_specs.csv` — contrast-level table in the shared `suite_common`",
              "  vocabulary (`layer`, `effect`/`var`, `on`/`off`) plus the audit columns",
              "  (`d_ig`, `d_cg`, `sd_pool`, `r_used`, `fallback`).",
              "* `temp/sdt_specs_multiverse.csv` — the same rows in the native SDT vocabulary.",
              "* `reanalysis.xlsx` — sheets `Suite_SDT_Contrasts`, `Suite_SDT_Results`,",
              "  `Suite_SDT_PubBias`, `Suite_SDT_LOO` (existing sheets untouched).",
              "* `figures/forest_sdt_*`, `funnel_sdt_*`, `loo_sdt_*`, `petpeese_sdt_*`,",
              "  `excl_onoff_*`, `forest_sdt_specs`, `domain_forest_sdt` (PNG 300 dpi + PDF).",
              "* `temp/sdt_skips.csv` — studies/artifacts not analysable and why.", "",
              NOTE_END]
    block = "\n".join(lines)
    if os.path.isfile(NOTE):
        txt = open(NOTE, encoding="utf-8").read()
        if NOTE_START in txt and NOTE_END in txt:
            pre = txt.split(NOTE_START)[0]
            post = txt.split(NOTE_END)[-1]
            txt = pre + block + post
        else:
            txt = (txt.rstrip() + "\n\n" + block + "\n")
    else:
        txt = ("# ANALYSIS SUITE NOTE — meta-analysis + SDT\n\n"
               "This file is maintained by the analysis-suite entry points. The meta-layer",
               "section is owned by the reported-effects session; the SDT section below is owned",
               "by `Scripts/analysis_multiverse_sdt.py`.\n\n" + block + "\n")
    with open(NOTE, "w", encoding="utf-8") as fh:
        fh.write(txt)


def _p_two_sided(effect, se):
    e = np.asarray(effect, float); s = np.asarray(se, float)
    z = np.divide(e, s, out=np.full_like(e, np.nan), where=(np.isfinite(s) & (s > 0)))
    return 2.0 * (1.0 - np.vectorize(_norm_cdf)(np.abs(z)))


def to_canonical(contrasts):
    """Return the SDT table in the shared `suite_common.SPEC_COLS` vocabulary.

    The richer internal columns are kept alongside the canonical ones so the same file
    is both human-auditable (`d_ig`, `d_cg`, `sd_pool`, ...) and consumable by
    `Scripts/suite_common.py` -> `publication_bias.py` / `figures.py`
    (`effect`/`var`, lower-case `on`/`off`, `dv_domain`, `experiment_id`).
    """
    d = contrasts.copy()
    d["experiment_id"] = d["artifact"]
    d["dv_domain"] = d["domain"]
    d["var"] = d["vi"]
    d["se"] = np.sqrt(pd.to_numeric(d["vi"], errors="coerce"))
    d["n1"] = d["n_ig"]; d["n2"] = d["n_cg"]
    d["n_total"] = d["n_ig"] + d["n_cg"]
    d["p_value"] = _p_two_sided(d["effect"], d["se"])
    d["p_censored"] = 0
    d["layer"] = "sdt"
    d["metric"] = "d_sdt"
    d["contrast_id"] = (d["study_key"].astype(str) + "|" + d["artifact"].astype(str)
                        + "|" + d["spec"].astype(str) + "|" + d["exclusions"].astype(str))
    d["dv_name"] = "discrimination[" + d["domain"].astype(str) + "]"
    d["veracity"] = "true_vs_false"
    d["data_available"] = 1
    d["contrast_type"] = ""
    d["source"] = "sdt_arm_contrast"
    d["did_basis"] = np.where(d["spec"].str.startswith("did"), "did_IG_minus_CG_delta_d", "")
    d["exclusions_embedded"] = 0
    if "cond_ambiguous" in d.columns:
        d["cond_ambiguous"] = pd.to_numeric(d["cond_ambiguous"], errors="coerce").fillna(0).astype(int)
    else:
        d["cond_ambiguous"] = 0
    d["oriented"] = 1
    d["exclusions"] = d["exclusions"].map({"ON": "on", "OFF": "off"}).fillna(d["exclusions"])
    d["fallback"] = d["fallback"].map({True: "sharing_intention", False: ""}).fillna(d["fallback"])
    d["note"] = np.where(d["spec"].str.endswith("_cred") & (d["fallback"] == "sharing_intention"),
                         "sharing_intention fallback (no credibility DV)", "")
    order = ["layer", "spec", "exclusions", "study_key", "experiment_id", "contrast_id",
             "dv_name", "dv_domain", "veracity", "timepoint", "metric", "effect",
             "effect_raw", "var", "se", "n1", "n2", "n_total", "r_used", "p_value",
             "p_censored", "data_available", "contrast_type", "source", "fallback",
             "did_basis", "exclusions_embedded", "cond_ambiguous", "oriented", "note",
             "dv_family", "cluster_id", "component_side",
             "artifact", "domain", "d_ig", "d_cg", "sd_pool", "vi", "n_ig", "n_cg",
             "n_paired_ig", "n_paired_cg",
             "true_score", "true_var", "fake_score", "fake_var",
             "criterion_score", "criterion_var"]
    if "component_side" not in d.columns:
        d["component_side"] = ""
    return d[[c for c in order if c in d.columns]]


# --------------------------------------------------------------------------- writing
def write_sheets(path, sheets, log, force=False):
    recent = os.path.isfile(path) and recently_modified(path) and not force
    target = path
    if recent:
        target = os.path.join(_TMP, "reanalysis_sdt_%s.xlsx" % time.strftime("%Y%m%d_%H%M%S"))
        log.append(f"reanalysis.xlsx modified in the last {RECENT_MIN} min; wrote {target} instead")
    try:
        mode = "a" if os.path.isfile(target) else "w"
        with pd.ExcelWriter(target, engine="openpyxl", mode=mode,
                            if_sheet_exists="replace" if mode == "a" else None) as xl:
            for name, df in sheets.items():
                df.to_excel(xl, sheet_name=name, index=False)
    except TypeError:
        with pd.ExcelWriter(target, engine="openpyxl", mode=mode) as xl:
            for name, df in sheets.items():
                df.to_excel(xl, sheet_name=name, index=False)
    return target


# --------------------------------------------------------------------------- main
def main():
    t0 = time.time()
    SC.include_fully_within_cli()
    SC.announce_fully_within("SDT multiverse")
    do_fig = "--no-figures" not in sys.argv
    do_note = "--no-note" not in sys.argv
    limit = None
    out_xlsx = OUT_XLSX
    for i, a in enumerate(sys.argv):
        if a == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])
        if a == "--out" and i + 1 < len(sys.argv):
            out_xlsx = sys.argv[i + 1]
    folders = sorted(glob.glob(os.path.join(DS, "*")))
    if limit:
        folders = folders[:limit]
    print(f"SDT multiverse suite: scanning {len(folders)} dataset folders ...")
    all_comp, skips, fallbacks = [], [], []
    fw_skipped = []
    n_studies = 0
    for folder in folders:
        key = os.path.basename(folder)
        # Fully-within (single-arm, no-control) datasets: excluded by default.
        if not SC.include_fully_within() and SC.study_fully_within(key):
            fw_skipped.append(key)
            continue
        hpath = os.path.join(folder, key + " - harmonized.csv")
        if not os.path.isfile(hpath):
            continue
        if recently_modified(hpath):
            skips.append(dict(study_key=key, artifact="", reason="harmonized file modified in last 15 min (concurrent)"))
            continue
        try:
            comp, sk = build_study(folder, exclusions_on=True, with_components=True)
        except Exception as e:  # noqa: BLE001
            skips.append(dict(study_key=key, artifact="", reason=f"error: {type(e).__name__}: {e}"))
            continue
        skips.extend(sk)
        if len(comp):
            all_comp.append(comp)
            n_studies += 1
    if not all_comp:
        print("no analysable datasets")
        return 1
    comp = pd.concat(all_comp, ignore_index=True)
    print(f"studies analysed: {n_studies} | participant-composites: {len(comp):,}")

    rows_on, notes_on = contrasts_for(comp)
    # exclusions OFF
    all_comp_off, _, _ = [], None, None
    off_folders = folders
    for folder in off_folders:
        key = os.path.basename(folder)
        if not SC.include_fully_within() and SC.study_fully_within(key):
            continue
        hpath = os.path.join(folder, key + " - harmonized.csv")
        if not os.path.isfile(hpath) or recently_modified(hpath):
            continue
        try:
            c, _ = build_study(folder, exclusions_on=False, with_components=True)
        except Exception:
            continue
        if len(c):
            all_comp_off.append(c)
    comp_off = pd.concat(all_comp_off, ignore_index=True) if all_comp_off else comp.iloc[0:0]
    rows_off, notes_off = contrasts_for(comp_off)

    for ex, rows in (("ON", rows_on), ("OFF", rows_off)):
        for r in rows:
            r["exclusions"] = ex
    contrasts = pd.DataFrame(rows_on + rows_off)
    fallbacks = notes_on + notes_off
    canon = to_canonical(contrasts)
    canon.to_csv(SPECS_CSV, index=False, encoding="utf-8-sig")
    SC.write_fully_within_marker(SPECS_CSV)
    contrasts.to_csv(os.path.join(_TMP, "sdt_specs_multiverse.csv"), index=False, encoding="utf-8-sig")
    pd.DataFrame(skips).to_csv(SKIPS_CSV, index=False, encoding="utf-8-sig")
    print("  " + SC.fully_within_report(len(fw_skipped), "dataset(s)"))
    print(f"contrasts: {len(contrasts)} (ON={len(rows_on)}, OFF={len(rows_off)}) -> {SPECS_CSV}")
    if not len(contrasts):
        print("no computable SDT contrasts")
        return 1

    results, pubs, loos = [], [], []
    for spec in ALL_SPECS:
        for ex in ("ON", "OFF"):
            res, extra = run_spec(contrasts, spec, ex)
            if res is None:
                continue
            results.append(res)
            if extra is not None:
                pubs.append(extra[0]); loos.append(extra[1])
    results_df = pd.concat(results, ignore_index=True) if results else pd.DataFrame()
    pub_df = pd.concat(pubs, ignore_index=True) if pubs else pd.DataFrame()
    loo_df = pd.concat(loos, ignore_index=True) if loos else pd.DataFrame()

    sheets = {
        "Suite_SDT_Contrasts": contrasts,
        "Suite_SDT_Results": results_df,
        "Suite_SDT_Dependence": dependence_rows(contrasts),
        "Suite_SDT_PubBias": pub_df,
        "Suite_SDT_LOO": loo_df,
    }
    log = []
    target = write_sheets(out_xlsx, sheets, log, force=("--force-write" in sys.argv))
    for msg in log:
        print("NOTE:", msg)
    print("wrote sheets ->", target)

    if do_fig and len(results_df):
        try:
            make_figures(contrasts, results_df, pub_df, loo_df, contrasts)
            print("figures ->", FIGDIR)
        except Exception as e:  # noqa: BLE001
            print("figure generation failed:", type(e).__name__, e)
    if do_note and len(results_df):
        write_note(contrasts, results_df, pub_df, skips, fallbacks)
        print("note ->", NOTE)
    print(f"done in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
