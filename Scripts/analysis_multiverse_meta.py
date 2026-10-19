"""analysis_multiverse_meta.py -- META-LAYER (reported-effects) multiverse analysis suite.

Entry point for the *reported-effects* half of `Scripts/ANALYSIS_SUITE_SPEC.md`.  It reads
`Extraction/*.json`, derives one effect per eligible contrast (`eligible_primary == 1`) for the
four specs, runs them with author exclusions ON (coded effects) and OFF (re-derived from the
harmonized participant data where a contrast can be matched), adds the data-availability
moderator, runs the publication-bias battery, draws the figures and writes:

  temp/meta_specs.csv            tidy per-effect table (spec x exclusions x contrast)
  analysis_suite.xlsx            consolidated results / moderator / publication-bias sheets
  metaanalysis.xlsx              + `Suite_<spec>_<ON|OFF>` sheets (existing sheets untouched)
  figures/*.png|pdf              forest / funnel / LOO / moderator / PET-PEESE graphics
  Scripts/ANALYSIS_SUITE_NOTE.md meta section (regenerated)

The four specifications
------------------------
1. `did_all`   DiD = (post_IG - pre_IG) - (post_CG - pre_CG), standardised by the pooled
               *pretest* SD; all DVs.  Requires a pretest and an immediate posttest.
2. `did_cred`  same, credibility DVs; a study with no credibility contrast falls back to
               sharing_intention (every fallback recorded).
3. `post_all`  posttest only (immediate), all DVs.
4. `post_cred` posttest only, credibility (same sharing fallback).
5. `persistence_all`  follow-up only, all DVs (does the effect persist?).
6. `persistence_cred` follow-up only, credibility (same sharing fallback).

The immediate posttest and the follow-up are separate questions and are never pooled
together; pretest rows are used only as the DiD baseline.

Units & nesting: a contrast is one eligible coded effect; contrasts are nested in studies, so
each pooled result is reported as (a) a one-level DerSimonian-Laird random-effects estimate
(the pre-existing `metaanalysis.py` model), (b) a two-stage (within-study / between-study)
random-effects estimate, and (c) the DL estimate with a cluster-robust (by study) SE.

No invented numbers: every derived effect records its `source` and `conversion_inputs`.  Raw
files are never edited; intermediates go to `temp/`; inputs changed in the last 15 minutes are
skipped (concurrent sessions).

Run:  python Scripts/analysis_multiverse_meta.py
"""
import fnmatch
import glob
import json
import math
import os
import re
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMP = os.path.join(ROOT, "temp")
# Contract (AGENTS.md): analysis-suite figures live under Results/
FIG = os.path.join(ROOT, "Results", "figures")
os.makedirs(TEMP, exist_ok=True)
os.makedirs(FIG, exist_ok=True)

import tempfile  # noqa: E402
tempfile.tempdir = TEMP

sys.path.insert(0, os.path.join(ROOT, "Scripts"))
import domain_restriction as DR  # noqa: E402
import study_exclusions as SE  # noqa: E402
import suite_common as SC  # noqa: E402  (dependence-aware pooling: 3-level + RVE)

try:  # console-safe output for study names with non-cp1252 characters
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

EXTRACT = os.path.join(ROOT, "Extraction")
DATASETS = os.path.join(ROOT, "Datasets")
META_XLSX = os.path.join(ROOT, "Results", "metaanalysis.xlsx")
# Contract (AGENTS.md): the consolidated suite workbook lives under Results/
SUITE_XLSX = os.path.join(ROOT, "Results", "analysis_suite.xlsx")
TIDY_CSV = os.path.join(TEMP, "meta_specs.csv")
NOTE_MD = os.path.join(ROOT, "Scripts", "ANALYSIS_SUITE_NOTE.md")

SKIP_SECONDS = 0 if os.environ.get("SUITE_FORCE") else 15 * 60   # skip inputs changed recently
# Strands removed from the primary pool by the dependent-effects guards:
# confidence/metacognition (separate strand) and non-standardised effect types.
METACOG_DROPPED = []
STD_DROPPED = []
# Study keys with at least one fully-within (single-arm, no-control) record.
# ``load_contrasts`` fills this on every call; ``write_meta_spec_table`` uses it
# to label a fully-within DiD row as the paper's reported pre->post change even
# on the exclusions-OFF path (which for a single-arm record has no control arm to
# re-derive from and therefore keeps the same reported effect).
FW_STUDY_KEYS = set()
# Eligible contrasts refused by the W7-STRUCT omnibus guard (multi-df omnibus /
# contrast_type==11).  ``load_contrasts`` clears and refills it on every call so
# ``metaanalysis.py`` can report the pooled count.
OMNIBUS_SKIPPED = []
SPECS = ["did_all", "did_cred", "post_all", "post_cred",
         "persistence_all", "persistence_cred"]
R_PRIMARY = 0.50
R_SENS = [0.30, 0.50, 0.70]
DID_ABS_MAX = 3.0      # |standardised DiD| implausible above this (coded SD likely a SE)

# ---------------------------------------------------------------------------- helpers
def num(x):
    try:
        if x is None:
            return np.nan
        if isinstance(x, float) and math.isnan(x):
            return np.nan
        return float(x)
    except (TypeError, ValueError):
        return np.nan


def fresh(path, seconds=SKIP_SECONDS):
    """True if the file was modified within `seconds` (concurrent-session guard)."""
    try:
        return (time.time() - os.path.getmtime(path)) < seconds
    except OSError:
        return False


def norm_tp(x):
    return re.sub(r"\s+", "_", str(x).strip().lower()) if x is not None else ""


PRE_TOKENS = {"pre", "pretest", "baseline", "t1", "w1", "pre_t1", "pre_t1_t2_t3_t4", "pre_"
              "baseline_w1", "pre_treatment", "t0", "wave1"}
POST_TOKENS = {"post", "posttest", "t2", "w2", "post_decision", "endline", "first_endline_immediate",
               "post_t4", "post_w2", "t1_immediate", "immediate_post", "post_intervention_1",
               "post-intervention", "post_test", "wave2", "endline_immediate"}
FOLLOW_RE = re.compile(r"follow|_w\d|\bweek|month|\b\d+\s*w\b|\b\d+_?m\b|\b\d+_?d\b|\bday|"
                       r"\d+mo\b|\d+month|\d+week|\d+_week|\d+w\b|\d+m\b", re.I)


def timing(dv):
    """Classify a coded contrast's timepoint: (is_immediate_post, is_followup, spans_pre_post).

    Follow-up detection reads the **timepoint fields** (``timepoint_1``/``timepoint_2``)
    only.  The free-text ``comparison_label`` frequently names the timing of the
    *immediate* posttest ("posttest, 1 week after instruction") or the *intervention
    duration* ("14-week campaign"), so a duration word there must not mark a delayed
    measurement -- only an explicit "follow" in the label does.  An explicit post
    token in the timepoint fields also overrides a bare wave token
    ("post_w2" is the immediate posttest, not a follow-up wave).
    """
    t1, t2 = norm_tp(dv.get("timepoint_1")), norm_tp(dv.get("timepoint_2"))
    tt = (t1 + " " + t2).strip()
    label = str(dv.get("comparison_label") or "").lower()
    pre = any(tok and tok in t1 for tok in PRE_TOKENS) or any(tok and tok in t2 for tok in PRE_TOKENS)
    pre = pre or bool(re.search(r"\bpre\b|baseline|pretest", tt))
    post = (t1 in POST_TOKENS or t2 in POST_TOKENS) or ("post" in t1 or "post" in t2)
    if str(dv.get("posttest_type")) in ("1", "4"):
        post = True
    follow = "follow" in t1 or "follow" in t2 or bool(FOLLOW_RE.search(tt))
    # A wave / duration token in the timepoint fields is a delayed measurement only
    # when the same field does not also carry an explicit post marker ("post_w2").
    if post and "follow" not in tt:
        follow = False
    # The free-text label signals a delayed measurement only via the explicit word.
    follow = follow or ("follow" in label)
    spans = pre and post          # the coded effect itself is a pre->post change / DiD
    return bool(post), bool(follow), bool(spans)


def pol_mult(dv):
    """+1 if higher scores are more desirable, -1 if higher is worse, else +1 (unknown)."""
    p = dv.get("dv_polarity")
    if p in (0, "0", False):
        return -1.0
    return 1.0


def hedges_g(m1, s1, n1, m2, s2, n2):
    if any(np.isnan(x) for x in (m1, s1, n1, m2, s2, n2)) or s1 <= 0 or s2 <= 0 or n1 + n2 <= 2:
        return np.nan, np.nan
    sp = math.sqrt(((n1 - 1) * s1 ** 2 + (n2 - 1) * s2 ** 2) / (n1 + n2 - 2))
    if sp <= 0:
        return np.nan, np.nan
    g = (m1 - m2) / sp * (1 - 3 / (4 * (n1 + n2) - 9))
    v = (n1 + n2) / (n1 * n2) + g * g / (2 * (n1 + n2))
    return g, v


def is_withheld(dv):
    """True when the coding explicitly withheld a derived large effect.

    Rows withheld by the large-effect sanity check (``conversion_inputs
    ['large_effect_verification']``, or a "plausible standardised range" reason) keep their
    reported cell means/SDs in ``cell_descriptives`` for audit, but must **never** be
    re-derived from them: the coded SDs behave like standard errors and the derived
    standardised effect exceeds the plausible range (|effect| > 3).  This mirrors
    ``exact_derive_completion.is_withheld`` and does **not** change the plausibility
    threshold; it only stops an already-withheld value from silently re-entering the pool
    through the ``cell_descriptives`` fallback.  See Scripts/ZHAO_CONTRAST_DERIVATION.md
    and Scripts/LARGE_EFFECTS_DERIVED_CHECK.md.
    """
    ci = dv.get("conversion_inputs")
    if isinstance(ci, dict) and ci.get("large_effect_verification"):
        return True
    reason = str(dv.get("missing_metrics_reason") or "").lower()
    return "plausible standardised range" in reason or "large-effect" in reason


def reported_effect(dv):
    """One coded/reported effect (yi, vi, how) -- identical order to metaanalysis.effect().

    A reported ``yi/vi`` that is a raw coefficient is not used as a standardised
    effect; the derivation falls through to a genuinely standardised coded statistic
    (g/d/t/r/means/SD) when one exists.  A ``conversion_method`` recording that the
    value was standardised keeps it.
    """
    yi, vi = num(dv.get("yi")), num(dv.get("vi"))
    if (not np.isnan(yi) and not np.isnan(vi) and vi > 0
            and SC.source_is_standardised("yi/vi", dv.get("effect_size_type_reported"),
                                          SC.conversion_text(dv))):
        return yi, vi, "yi/vi"
    g = num(dv.get("g_val"))
    if not np.isnan(g):
        n1, n2 = num(dv.get("n1i")), num(dv.get("n2i"))
        if np.isnan(n1) or np.isnan(n2):
            nt = num(dv.get("n_total"))
            n1 = n2 = nt / 2 if not np.isnan(nt) else np.nan
        if not np.isnan(n1) and not np.isnan(n2) and n1 + n2 > 2:
            return g, (n1 + n2) / (n1 * n2) + g * g / (2 * (n1 + n2)), "g"
    d = num(dv.get("d_val"))
    if not np.isnan(d):
        n1, n2 = num(dv.get("n1i")), num(dv.get("n2i"))
        if not np.isnan(n1) and not np.isnan(n2) and n1 + n2 > 2:
            return d, (n1 + n2) / (n1 * n2) + d * d / (2 * (n1 + n2)), "d"
    t = num(dv.get("t_val"))
    n1, n2 = num(dv.get("n1i")), num(dv.get("n2i"))
    if not np.isnan(t) and not np.isnan(n1) and not np.isnan(n2) and n1 + n2 > 2:
        r = t / math.sqrt(t * t + n1 + n2 - 2)
        if abs(r) < 1:
            z = 0.5 * math.log((1 + r) / (1 - r))
            return z, 1 / max(n1 + n2 - 3, 1), "t->z"
    if not np.isnan(t):
        df = num(dv.get("df"))
        if np.isnan(df):
            df = num(dv.get("df1"))
        if not np.isnan(df) and df > 1:
            r = t / math.sqrt(t * t + df)
            if abs(r) < 1:
                z = 0.5 * math.log((1 + r) / (1 - r))
                return z, 1 / max(df - 1, 1), "t->z"
    r = num(dv.get("r_val"))
    n = num(dv.get("n_total"))
    if not np.isnan(r) and not np.isnan(n) and abs(r) < 1:
        z = 0.5 * math.log((1 + r) / (1 - r))
        return z, 1 / max(n - 3, 1), "r->z"
    m1 = num(dv.get("m1i_post", dv.get("m1i")))
    m2 = num(dv.get("m2i_post", dv.get("m2i")))
    s1 = num(dv.get("sd1i_post", dv.get("sd1i")))
    s2 = num(dv.get("sd2i_post", dv.get("sd2i")))
    g, v = hedges_g(m1, s1, n1, m2, s2, n2)
    if not np.isnan(g):
        return g, v, "means/sd"
    return np.nan, np.nan, "none"


def _cells(dv):
    out = []
    for c in (dv.get("cell_descriptives") or []):
        if not isinstance(c, dict):
            continue
        mean = num(c.get("mean"))
        sd = num(c.get("sd"))
        grp = c.get("group")
        tp = c.get("timepoint")
        n = num(c.get("n"))
        if pd.isna(mean) or pd.isna(sd) or grp is None or tp is None:
            continue
        out.append({"group": str(grp), "tp": norm_tp(tp), "mean": mean, "sd": sd, "n": n})
    return out


def _match_group(cell_group, label):
    if not label:
        return 0.0
    a = set(re.findall(r"[a-z0-9]+", str(cell_group).lower()))
    b = set(re.findall(r"[a-z0-9]+", str(label).lower()))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cells_post(dv):
    """Hedges g of intervention vs control at the immediate posttest from cell_descriptives."""
    if is_withheld(dv):
        return np.nan, np.nan, ""
    cells = _cells(dv)
    if not cells:
        return np.nan, np.nan, ""
    tps = {c["tp"] for c in cells}
    post_tps = {t for t in tps if t in POST_TOKENS or "post" in t or "endline" in t}
    if not post_tps:
        return np.nan, np.nan, ""
    post = [c for c in cells if c["tp"] in post_tps]
    groups = sorted({c["group"] for c in post})
    if len(groups) < 2:
        return np.nan, np.nan, ""
    g1, g2 = _pick_arms(groups, dv)
    a = [c for c in post if c["group"] == g1]
    b = [c for c in post if c["group"] == g2]
    if not a or not b:
        return np.nan, np.nan, ""
    a, b = a[0], b[0]
    n1 = a["n"] if not pd.isna(a["n"]) else num(dv.get("n1i"))
    n2 = b["n"] if not pd.isna(b["n"]) else num(dv.get("n2i"))
    g, v = hedges_g(a["mean"], a["sd"], n1, b["mean"], b["sd"], n2)
    if np.isnan(g):
        return np.nan, np.nan, ""
    return pol_mult(dv) * g, v, "cells:post"


def _pick_arms(groups, dv):
    """(group1_intervention, group2_control) from a list of cell group names."""
    if len(groups) == 2:
        g1_lab, g2_lab = dv.get("group1_label"), dv.get("group2_label")
        s1 = [_match_group(x, g1_lab) for x in groups]
        s2 = [_match_group(x, g2_lab) for x in groups]
        # prefer the assignment maximising label agreement; tie -> order of appearance
        best, bi = -1, 0
        for i in range(2):
            score = s1[i] + s2[1 - i]
            if score > best:
                best, bi = score, i
        if best <= 0:
            bi = 0
        return groups[bi], groups[1 - bi]
    g1_lab, g2_lab = str(dv.get("group1_label") or ""), str(dv.get("group2_label") or "")
    s1 = [(x, _match_group(x, g1_lab)) for x in groups]
    s2 = [(x, _match_group(x, g2_lab)) for x in groups]
    s1.sort(key=lambda t: -t[1])
    s2.sort(key=lambda t: -t[1])
    if s1 and s2 and s1[0][0] != s2[0][0]:
        return s1[0][0], s2[0][0]
    return None, None


def _did_from_prepost(m1p, s1p, n1p, m1q, s1q, n1q,
                      m2p, s2p, n2p, m2q, s2q, n2q, r):
    need = [m1p, s1p, m1q, s1q, m2p, s2p, m2q, s2q]
    if any(np.isnan(x) for x in need):
        return np.nan, np.nan, None
    n1 = n1p if not np.isnan(n1p) else n1q
    n2 = n2p if not np.isnan(n2p) else n2q
    if np.isnan(n1) or np.isnan(n2) or n1 < 3 or n2 < 3 or s1p <= 0 or s2p <= 0:
        return np.nan, np.nan, None
    if not (-0.999 < r < 0.999):
        return np.nan, np.nan, None
    sp = math.sqrt(((n1 - 1) * s1p ** 2 + (n2 - 1) * s2p ** 2) / (n1 + n2 - 2))
    if sp <= 0:
        return np.nan, np.nan, None
    sd_c1 = math.sqrt(max(1e-12, s1p ** 2 + s1q ** 2 - 2 * r * s1p * s1q))
    sd_c2 = math.sqrt(max(1e-12, s2p ** 2 + s2q ** 2 - 2 * r * s2p * s2q))
    did = ((m1q - m1p) - (m2q - m2p)) / sp
    var = (sd_c1 ** 2 / n1 + sd_c2 ** 2 / n2) / sp ** 2
    return did, var, {"r": r, "n1": n1, "n2": n2, "sd_pre_pooled": sp,
                      "sd_change_ig": sd_c1, "sd_change_cg": sd_c2}


def did_effect(dv, r):
    """DiD standardised by the pooled pretest SD, and its variance, for one correlation r."""
    mult = pol_mult(dv)
    # (1) explicit pre/post fields
    m1p, s1p, n1p = num(dv.get("m1i_pre")), num(dv.get("sd1i_pre")), num(dv.get("n1i_pre"))
    m2p, s2p, n2p = num(dv.get("m2i_pre")), num(dv.get("sd2i_pre")), num(dv.get("n2i_pre"))
    m1q = num(dv.get("m1i_post", dv.get("m1i")))
    s1q = num(dv.get("sd1i_post", dv.get("sd1i")))
    m2q = num(dv.get("m2i_post", dv.get("m2i")))
    s2q = num(dv.get("sd2i_post", dv.get("sd2i")))
    n1q = num(dv.get("n1i_post", dv.get("n1i")))
    n2q = num(dv.get("n2i_post", dv.get("n2i")))
    did, var, info = _did_from_prepost(m1p, s1p, n1p, m1q, s1q, n1q,
                                       m2p, s2p, n2p, m2q, s2q, n2q, r)
    if not np.isnan(did):
        return mult * did, var, "fields:did", info
    # (2) cell_descriptives with both pre and post cells for both arms
    cells = [] if is_withheld(dv) else _cells(dv)
    if cells:
        tps = {c["tp"] for c in cells}
        pre_tps = {t for t in tps if t in PRE_TOKENS or "pre" in t or "baseline" in t}
        post_tps = {t for t in tps if t in POST_TOKENS or "post" in t or "endline" in t}
        pre_tps, post_tps = pre_tps - post_tps, post_tps - pre_tps
        if pre_tps and post_tps:
            groups = sorted({c["group"] for c in cells})
            g1, g2 = _pick_arms(groups, dv)
            def cell(g, tset):
                for c in cells:
                    if c["group"] == g and c["tp"] in tset:
                        return c
                return None
            a_p, a_q = cell(g1, pre_tps), cell(g1, post_tps)
            b_p, b_q = cell(g2, pre_tps), cell(g2, post_tps)
            if all(x is not None for x in (a_p, a_q, b_p, b_q)):
                n1p = a_p["n"] if not pd.isna(a_p["n"]) else num(dv.get("n1i"))
                n2p = b_p["n"] if not pd.isna(b_p["n"]) else num(dv.get("n2i"))
                n1q = a_q["n"] if not pd.isna(a_q["n"]) else n1p
                n2q = b_q["n"] if not pd.isna(b_q["n"]) else n2p
                did, var, info = _did_from_prepost(
                    a_p["mean"], a_p["sd"], n1p, a_q["mean"], a_q["sd"], n1q,
                    b_p["mean"], b_p["sd"], n2p, b_q["mean"], b_q["sd"], n2q, r)
                if not np.isnan(did):
                    info = dict(info or {}, arm_map=f"{g1}|{g2}")
                    return mult * did, var, "cells:did", info
    return np.nan, np.nan, "", None


def harm_offsets_study(key, grp, spec, r_default=0.5):
    """Exclusions-OFF effects for every contrast of one study, loading the wide file once.

    Returns {(Study_ID, contrast): (yi, vi, source)}; missing entries mean the contrast could
    not be matched to harmonized data (the coded effect is then retained and flagged).
    """
    h = harm(key)
    if not h.ok:
        return {}
    f = os.path.join(h.folder, key + " - harmonized.csv")
    if fresh(f):
        return {}                      # concurrent harmonizer writing this dataset
    try:
        header = list(pd.read_csv(f, nrows=0, encoding="utf-8-sig").columns)
    except Exception:
        return {}
    hdr = set(header)
    idcols = [c for c in ("condition", "condition_raw", "group", "cond") if c in hdr]
    plans, need = [], set(idcols)
    for _, row in grp.iterrows():
        cd = row.to_dict()
        dom = row["dv_domain"]
        # persistence contrasts live at the follow-up timepoint in the sidecar
        m = _match_side(cd, dom, "followup" if spec.startswith("persistence") else "post")
        if not m:
            plans.append((row, None, None, None, 0.0)); continue
        uv_post, score = m
        colq = uv_post + "_01" if uv_post + "_01" in hdr else (uv_post if uv_post in hdr else None)
        colp, uv_pre = None, None
        if spec.startswith("did"):
            base = re.sub(r"_(pre|post)$", "", uv_post)
            for suff in ("_pre", "_Pre", "_PRE"):
                cand = base + suff
                if cand + "_01" in hdr:
                    uv_pre, colp = cand, cand + "_01"; break
                if cand in hdr:
                    uv_pre, colp = cand, cand; break
        plans.append((row, colq, colp, uv_post, score))
        if colq:
            need.add(colq)
        if colp:
            need.add(colp)
    if not (need - set(idcols)):
        return {}
    try:
        wide = pd.read_csv(f, usecols=lambda c: c in need, encoding="utf-8-sig", low_memory=False)
    except Exception:
        return {}
    # condition masks (cached on h)
    base_dv = plans[0][0].to_dict() if plans else {}
    mask_i, mask_c, arm = _arm_masks_from(wide, hdr, idcols, base_dv)
    out = {}
    for row, colq, colp, uv_post, score in plans:
        kkey = (row["Study_ID"], row["contrast"])
        if not colq or colq not in wide.columns or mask_i is None:
            continue
        x = pd.to_numeric(wide[colq], errors="coerce")
        a, b = x[mask_i].dropna(), x[mask_c].dropna()
        if len(a) < 3 or len(b) < 3:
            continue
        side = next((s for s in h.side if s["uv"] == uv_post), {})
        mult = -1.0 if side.get("polarity", "") in ("higher_is_worse", "lower_is_better") else 1.0
        if spec.startswith("did") and colp and colp in wide.columns:
            p = pd.to_numeric(wide[colp], errors="coerce")
            q = x
            def st(mask):
                pp, qq = p[mask], q[mask]
                ok = pp.notna() & qq.notna()
                pp, qq = pp[ok], qq[ok]
                if len(pp) < 3:
                    return None
                rr = np.corrcoef(pp, qq)[0, 1] if pp.std() > 0 and qq.std() > 0 else np.nan
                return dict(n=len(pp), mp=pp.mean(), sp=pp.std(ddof=1), mq=qq.mean(),
                            sq=qq.std(ddof=1), r=rr)
            sa, sb = st(mask_i), st(mask_c)
            if sa and sb and sa["sp"] > 0 and sb["sp"] > 0:
                rs = [x["r"] for x in (sa, sb) if not np.isnan(x["r"])]
                rr = float(np.mean(rs)) if rs else r_default
                y, v, info = _did_from_prepost(sa["mp"], sa["sp"], sa["n"], sa["mq"], sa["sq"], sa["n"],
                                               sb["mp"], sb["sp"], sb["n"], sb["mq"], sb["sq"], sb["n"],
                                               rr if abs(rr) < 0.999 else r_default)
                if not np.isnan(y):
                    out[kkey] = (mult * y, v, f"harm:{uv_pre}->{uv_post}|{arm}|r={rr:.2f}")
                    continue
        g, v = hedges_g(a.mean(), a.std(ddof=1), len(a), b.mean(), b.std(ddof=1), len(b))
        if not np.isnan(g):
            out[kkey] = (mult * g, v, f"harm:{colq}|{arm}|{score:.2f}")
    return out


def _arm_masks_from(wide, hdr, idcols, dv):
    if not idcols:
        return None, None, ""
    cond = wide[idcols[0]].astype(str)
    vals = [v for v in cond.unique() if str(v) and str(v).lower() not in ("nan", "none", "", "<na>")]
    if len(vals) != 2:
        return None, None, ""
    cg = None
    for v in vals:
        if re.search(r"control|placebo|no_?intervention|no_?treatment|business_?as_?usual|"
                     r"assessment_?only|inactive", str(v), re.I):
            cg = v; break
    if cg is None and dv.get("group2_label"):
        cg = max(vals, key=lambda v: _match_group(v, dv.get("group2_label")))
    if cg is None:
        return None, None, ""
    ig = [v for v in vals if v != cg][0]
    return (cond == ig).to_numpy(), (cond == cg).to_numpy(), f"{ig}|{cg}"


# ------------------------------------------------------------------ extraction loading
def load_contrasts(verbose=True, force=False):
    rows, skipped, did_excluded, missing_dropped = [], [], [], []
    fw_skipped = []
    include_fw = SC.include_fully_within()
    METACOG_DROPPED.clear()
    STD_DROPPED.clear()
    FW_STUDY_KEYS.clear()
    OMNIBUS_SKIPPED.clear()
    for p in sorted(glob.glob(os.path.join(EXTRACT, "*.json"))):
        key = os.path.splitext(os.path.basename(p))[0]
        # Whole-study full-text exclusion agreed by coders in Coding Studio.
        if SE.is_fulltext_excluded(key):
            continue
        # ``force`` bypasses the concurrent-writer freshness guard: the standalone
        # spec-table rebuild (``suite_common.build_meta_specs``) must always read
        # every extraction, exactly as the pre-unification builder did.
        if not force and fresh(p):
            skipped.append(key)
            continue
        try:
            data = json.load(open(p, encoding="utf-8"))
        except Exception as e:
            print("  skip (parse)", key, e)
            continue
        data = data if isinstance(data, list) else [data]
        for o in data:
            if not isinstance(o, dict):
                continue
            if SE.is_excluded(key, o.get("Study_ID")):
                continue                      # non-independent experiment (not an author exclusion)
            # Fully-within (single-arm, no-control) experiments: excluded by default.
            is_fw = SC.is_fully_within(o)
            if is_fw:
                FW_STUDY_KEYS.add(key)
            if not include_fw and is_fw:
                fw_skipped.append({"study_key": key, "Study_ID": o.get("Study_ID")})
                continue
            explicit_iv = SC.record_has_interventions(o)
            for iv, eff, i, dv in SC.effective_dvs_unique(o, key):
                _iid = str(iv.get("Intervention_ID") or "").strip() if explicit_iv else ""
                if not isinstance(dv, dict) or dv.get("eligible_primary") != 1:
                    continue
                # W7-STRUCT omnibus guard: never pool a multi-df omnibus (or a
                # contrast_type==11 model term) as an intervention-vs-control effect.
                if SC.is_omnibus_eligible(dv):
                    SC.log_omnibus_skip(key, o.get("Study_ID"), dv,
                                        where="analysis_multiverse_meta")
                    OMNIBUS_SKIPPED.append({
                        "study_key": key, "Study_ID": o.get("Study_ID"),
                        "dv_name": dv.get("dv_name"), "dv_domain": dv.get("dv_domain"),
                        "contrast_type": dv.get("contrast_type"),
                        "df_num": dv.get("df_num"), "df_den": dv.get("df_den"),
                        "F_val": dv.get("F_val"), "chi2_val": dv.get("chi2_val"),
                        "eta2_p": dv.get("eta2_p")})
                    continue
                # Contradictory rows (a numeric effect AND a non-empty missing_metrics_reason)
                # are excluded from pooling; the coder flagged the effect as unavailable.
                # Rows with a reason but no numeric effect are already unpoolable and are
                # simply counted separately.
                _reason = str(dv.get("missing_metrics_reason") or "").strip()
                _yi = num(dv.get("yi")); _vi = num(dv.get("vi"))
                if _reason and np.isfinite(_yi) and np.isfinite(_vi) and _vi > 0:
                    missing_dropped.append({"study_key": key, "Study_ID": o.get("Study_ID"),
                                            "dv_name": dv.get("dv_name"), "reason": _reason})
                    continue
                dom = DR.resolve_domain(dv.get("dv_domain"), key, dv.get("dv_name"))
                # Confidence / certainty / metacognition is a SEPARATE strand and must
                # never enter the susceptibility primary pool (or the SDT pool).
                if DR.is_non_outcome(dom) or SC.is_metacognition(dom, dv.get("dv_name")):
                    METACOG_DROPPED.append({"study_key": key, "Study_ID": o.get("Study_ID"),
                                            "dv_name": dv.get("dv_name"), "dv_domain": dom})
                    continue
                is_post, is_follow, spans = timing(dv)
                # ---- posttest effect ----
                ry, rv, rh = reported_effect(dv)
                cy, cv, ch = cells_post(dv)
                pol_known = dv.get("dv_polarity") in (0, 1)
                if spans:
                    # A pre->post contrast is NEVER an immediate-posttest effect: only a
                    # genuine posttest cell may enter the post pool.  The reported change
                    # stays available for the DiD specs (prepost_* below).
                    post_y, post_v, post_src = (cy, cv, ch) if not np.isnan(cy) else (np.nan, np.nan, "")
                elif not np.isnan(ry):
                    # orient reported effects so positive = more desirable (section E polarity rule);
                    # the unoriented value is kept in `reported_yi` for audit.
                    post_y, post_v, post_src = pol_mult(dv) * ry, rv, rh
                else:
                    post_y, post_v, post_src = cy, cv, ch
                post_reported = not np.isnan(ry)
                # ---- DiD effect ----
                did = {}
                for r in R_SENS:
                    dy, dvv, dsrc, dinfo = did_effect(dv, r)
                    did[r] = (dy, dvv, dsrc, dinfo)
                # Quality guard: a standardised pre-post change cannot plausibly reach
                # |DiD| > 3.  Extreme values trace to coded SDs that behave like standard
                # errors (e.g. Linegar et al. 2025: pre-SD 0.09 on a 1-10 scale, n>2000).
                # They are excluded from the DiD pools and listed in temp/meta_did_excluded.csv
                # rather than silently corrected.
                if not np.isnan(did[R_PRIMARY][0]) and abs(did[R_PRIMARY][0]) > DID_ABS_MAX:
                    did_excluded.append({
                        "study_key": key, "Study_ID": o.get("Study_ID"),
                        "dv_name": dv.get("dv_name"), "dv_domain": dom,
                        "effect_raw": did[R_PRIMARY][0], "source": did[R_PRIMARY][2],
                        "reason": f"|standardised DiD|>{DID_ABS_MAX} (implausible; coded SD likely a SE)"})
                    for r in R_SENS:
                        did[r] = (np.nan, np.nan, did[r][2], did[r][3])
                # ---- Reported pre-post contrast (the paper already reports the change/DiD) ----
                # Used by the DiD specs only when no DiD could be derived from means/SDs, so a
                # pre->post contrast with a reported effect is not silently dropped.
                if spans and not np.isnan(ry) and not np.isnan(rv) and rv > 0:
                    prepost_y, prepost_v, prepost_src = pol_mult(dv) * ry, rv, f"reported:prepost:{rh}"
                else:
                    prepost_y, prepost_v, prepost_src = np.nan, np.nan, ""
                # Explicit follow-up (persistence) effect: the reported effect at the
                # follow-up timepoint.  The posttest cell helper is NOT used here (it would
                # pick up the immediate posttest), so a follow-up never borrows an immediate
                # posttest cell.
                if is_follow and not np.isnan(ry) and not np.isnan(rv) and rv > 0:
                    follow_y, follow_v, follow_src = pol_mult(dv) * ry, rv, f"reported:followup:{rh}"
                else:
                    follow_y, follow_v, follow_src = np.nan, np.nan, ""
                row = {
                    "study_key": key, "Study_ID": o.get("Study_ID"), "contrast": i + 1,
                    "dv_name": dv.get("dv_name"), "dv_domain": dom,
                    "dv_polarity": dv.get("dv_polarity"),
                    "dv_family": SC.dv_family(dv),
                    "component_side": SC.dv_component_side(dv),
                    "cluster_id": SC.contrast_cluster_id(key, o.get("Study_ID"), dv),
                    "oriented": bool(pol_known),
                    "contrast_type": dv.get("contrast_type"),
                    "comparison": dv.get("comparison_label"),
                    "timepoint_1": dv.get("timepoint_1"), "timepoint_2": dv.get("timepoint_2"),
                    "is_post": is_post, "is_followup": is_follow, "spans_pre_post": spans,
                    "reported_yi": ry, "reported_vi": rv, "reported_from": rh,
                    "reported_type": str(dv.get("effect_size_type_reported") or ""),
                    "conversion_text": SC.conversion_text(dv),
                    "post_yi": post_y, "post_vi": post_v, "post_src": post_src,
                    "followup_yi": follow_y, "followup_vi": follow_v, "followup_src": follow_src,
                    "prepost_yi": prepost_y, "prepost_vi": prepost_v, "prepost_src": prepost_src,
                    "did_yi": did[R_PRIMARY][0], "did_vi": did[R_PRIMARY][1],
                    "did_src": did[R_PRIMARY][2],
                    "did_yi_30": did[0.30][0], "did_vi_30": did[0.30][1],
                    "did_yi_70": did[0.70][0], "did_vi_70": did[0.70][1],
                    "did_inputs": json.dumps(did[R_PRIMARY][3], default=str)
                    if did[R_PRIMARY][3] else "",
                }
                if _iid:
                    row["Intervention_ID"] = _iid
                    row["Intervention_Type"] = eff.get("Intervention_Type")
                rows.append(row)
    df = pd.DataFrame(rows)
    for c in ("did_yi", "did_yi_30", "did_yi_70"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if verbose:
        print(f"  extraction: {len(df)} eligible contrasts"
              f" (skipped {len(skipped)} fresh files; DiD guard dropped {len(did_excluded)}; "
              f"missing_metrics_reason dropped {len(missing_dropped)})")
        print("  " + SC.fully_within_report(len(fw_skipped), "record(s)"))
    return df, skipped, did_excluded, missing_dropped


# --------------------------------------------------- harmonized (exclusions OFF) support
def _tok(s):
    stop = {"the", "a", "an", "of", "to", "and", "or", "in", "for", "on", "with", "vs",
            "versus", "mean", "change", "score", "composite", "index", "total", "avg",
            "average", "pre", "post", "followup"}
    s = re.sub(r"[^a-z0-9]+", " ", str(s).lower())
    return {t for t in s.split() if t and t not in stop and len(t) > 1}


class Harm:
    """Lazily loaded harmonized dataset + sidecar for one study key."""

    def __init__(self, key):
        self.key = key
        self.folder = os.path.join(DATASETS, key)
        self.ok = False
        self.wide = None
        self.side = []
        self.rules = []
        self.armcache = {}
        f = os.path.join(self.folder, key + " - harmonized.csv")
        side = os.path.join(self.folder, key + " - harmonized_variables.csv")
        if not (os.path.isfile(f) and os.path.isfile(side)):
            return
        try:
            self.wide = pd.read_csv(f, encoding="utf-8-sig", low_memory=False)
            s = pd.read_csv(side, encoding="utf-8-sig", low_memory=False)
        except Exception:
            return
        for _, r in s.iterrows():
            uv = str(r["uniform_variable"])
            if not uv.startswith("dv_"):
                continue
            blob = " ".join(str(x) for x in (uv, r.get("original_variable"), r.get("notes"))
                            if pd.notna(x))
            sdom = DR.resolve_sidecar_domain(r.get("dv_domain"), uv)
            if DR.is_non_outcome(sdom):
                continue              # confidence/certainty: separate metacognition strand
            self.side.append({
                "uv": uv, "domain": sdom,
                "veracity": str(r.get("veracity", "") or "").strip().lower(),
                "timepoint": norm_tp(r.get("timepoint")),
                "polarity": str(r.get("dv_polarity", "") or "").strip().lower(),
                "tokens": _tok(blob)})
        ex = os.path.join(self.folder, key + " - exclusions.csv")
        if os.path.isfile(ex):
            try:
                e = pd.read_csv(ex, encoding="utf-8-sig")
                self.rules = [dict(r) for _, r in e.iterrows()]
            except Exception:
                self.rules = []
        self.ok = True


_HARM_CACHE = {}


def harm(key):
    if key not in _HARM_CACHE:
        _HARM_CACHE[key] = Harm(key)
    return _HARM_CACHE[key]


_HAS_DATA = {}


def study_has_data(key):
    if key not in _HAS_DATA:
        _HAS_DATA[key] = os.path.isfile(os.path.join(DATASETS, key, key + " - harmonized.csv"))
    return _HAS_DATA[key]


def _match_side(coded_dv, domain, tp):
    h = harm(coded_dv["study_key"])
    if not h.ok:
        return None
    ct = _tok(coded_dv["dv_name"])
    if not ct:
        return None
    best, bu = 0.0, None
    for v in h.side:
        if v["domain"] != domain:
            continue
        if tp and v["timepoint"] and v["timepoint"] != tp:
            continue
        ov = len(ct & v["tokens"]) / max(1, len(ct))
        if ov > best:
            best, bu = ov, v["uv"]
    return (bu, best) if best >= 0.5 else None


def _arm_masks(h, dv):
    wide = h.wide
    cache_key = str(dv.get("group2_label"))
    if cache_key in h.armcache:
        return h.armcache[cache_key]
    cols = [c for c in ("condition", "condition_raw", "group", "cond") if c in wide.columns]
    if not cols:
        h.armcache[cache_key] = (None, None, "")
        return h.armcache[cache_key]
    cond = wide[cols[0]].astype(str)
    vals = [v for v in cond.unique() if str(v) and str(v).lower() not in ("nan", "none", "", "<na>")]
    if len(vals) != 2:
        h.armcache[cache_key] = (None, None, "")
        return h.armcache[cache_key]
    g2_lab = str(dv.get("group2_label") or "")
    cg = None
    for v in vals:
        if re.search(r"control|placebo|no_?intervention|no_?treatment|business_?as_?usual|"
                     r"assessment_?only|inactive", v, re.I):
            cg = v
            break
    if cg is None and g2_lab:
        cg = max(vals, key=lambda v: _match_group(v, g2_lab))
    if cg is None:
        h.armcache[cache_key] = (None, None, "")
        return h.armcache[cache_key]
    ig = [v for v in vals if v != cg][0]
    h.armcache[cache_key] = ((cond == ig).to_numpy(), (cond == cg).to_numpy(), f"{ig}|{cg}")
    return h.armcache[cache_key]


def harm_post_effect(coded_dv, domain):
    """Re-derive the posttest g (IG vs CG) from harmonized data without exclusions."""
    h = harm(coded_dv["study_key"])
    if not h.ok:
        return np.nan, np.nan, ""
    m = _match_side(coded_dv, domain, "post")
    if not m:
        return np.nan, np.nan, ""
    uv, score = m
    col = uv + "_01" if uv + "_01" in h.wide.columns else uv
    if col not in h.wide.columns:
        return np.nan, np.nan, ""
    mask_i, mask_c, arm = _arm_masks(h, coded_dv)
    if mask_i is None:
        return np.nan, np.nan, ""
    x = pd.to_numeric(h.wide[col], errors="coerce")
    a, b = x[mask_i].dropna(), x[mask_c].dropna()
    if len(a) < 3 or len(b) < 3:
        return np.nan, np.nan, ""
    g, v = hedges_g(a.mean(), a.std(ddof=1), len(a), b.mean(), b.std(ddof=1), len(b))
    side = next((s for s in h.side if s["uv"] == uv), {})
    mult = -1.0 if side.get("polarity", "") in ("higher_is_worse", "lower_is_better") else 1.0
    return mult * g, v, f"harm:{uv}|{arm}|{score:.2f}"


def harm_did_effect(coded_dv, domain, r_default=0.5):
    """Re-derive the DiD from harmonized pre/post variables; actual within-person r if possible."""
    h = harm(coded_dv["study_key"])
    if not h.ok:
        return np.nan, np.nan, ""
    postm = _match_side(coded_dv, domain, "post")
    if not postm:
        return np.nan, np.nan, ""
    uv_post, score = postm
    base = re.sub(r"_(pre|post)$", "", uv_post)
    uv_pre = None
    for suff in ("_pre", "_Pre", "_PRE"):
        cand = base + suff
        if cand in h.wide.columns or cand + "_01" in h.wide.columns:
            uv_pre = cand
            break
    if uv_pre is None:
        return np.nan, np.nan, ""
    colq = uv_post + "_01" if uv_post + "_01" in h.wide.columns else uv_post
    colp = uv_pre + "_01" if uv_pre + "_01" in h.wide.columns else uv_pre
    if colp not in h.wide.columns or colq not in h.wide.columns:
        return np.nan, np.nan, ""
    mask_i, mask_c, arm = _arm_masks(h, coded_dv)
    if mask_i is None:
        return np.nan, np.nan, ""
    p = pd.to_numeric(h.wide[colp], errors="coerce")
    q = pd.to_numeric(h.wide[colq], errors="coerce")
    def stats(mask):
        pp, qq = p[mask], q[mask]
        ok = pp.notna() & qq.notna()
        pp, qq = pp[ok], qq[ok]
        if len(pp) < 3:
            return None
        r = np.corrcoef(pp, qq)[0, 1] if pp.std() > 0 and qq.std() > 0 else np.nan
        return dict(n=len(pp), mp=pp.mean(), sp=pp.std(ddof=1), mq=qq.mean(),
                    sq=qq.std(ddof=1), r=r)
    a, b = stats(mask_i), stats(mask_c)
    if not a or not b or a["sp"] <= 0 or b["sp"] <= 0:
        return np.nan, np.nan, ""
    rs = [x["r"] for x in (a, b) if not np.isnan(x["r"])]
    r = float(np.mean(rs)) if rs else r_default
    did, var, info = _did_from_prepost(a["mp"], a["sp"], a["n"], a["mq"], a["sq"], a["n"],
                                       b["mp"], b["sp"], b["n"], b["mq"], b["sq"], b["n"],
                                       r if abs(r) < 0.999 else r_default)
    if np.isnan(did):
        return np.nan, np.nan, ""
    side = next((s for s in h.side if s["uv"] == uv_post), {})
    mult = -1.0 if side.get("polarity", "") in ("higher_is_worse", "lower_is_better") else 1.0
    return mult * did, var, f"harm:{uv_pre}->{uv_post}|{arm}|r={r:.2f}"


# ------------------------------------------------------------------ pooling machinery
def dl_random(yi, vi):
    yi, vi = np.asarray(yi, float), np.asarray(vi, float)
    m = np.isfinite(yi) & np.isfinite(vi) & (vi > 0)
    yi, vi = yi[m], vi[m]
    k = len(yi)
    if k == 0:
        return dict(k=0, est=np.nan, se=np.nan, lo=np.nan, hi=np.nan, tau2=np.nan, i2=np.nan)
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
                tau2=tau2, i2=i2, _tau2=tau2)


def two_level(yi, vi, study):
    """Two-stage (within-study / between-study) random-effects model."""
    yi, vi, study = np.asarray(yi, float), np.asarray(vi, float), np.asarray(study)
    m = np.isfinite(yi) & np.isfinite(vi) & (vi > 0)
    yi, vi, study = yi[m], vi[m], study[m]
    if len(yi) == 0:
        return dict(k=0, est=np.nan, se=np.nan, lo=np.nan, hi=np.nan, tau_w=np.nan, tau_b=np.nan)
    # level-1 within-study variance component (moment estimator)
    wnum = wden = 0.0
    for g in set(study):
        idx = study == g
        if idx.sum() >= 2:
            yg, vg = yi[idx], vi[idx]
            wg = 1 / vg
            ybar = np.sum(wg * yg) / np.sum(wg)
            qg = np.sum(wg * (yg - ybar) ** 2)
            cg = np.sum(wg) - np.sum(wg ** 2) / np.sum(wg)
            tw = max(0.0, (qg - (idx.sum() - 1)) / cg) if cg > 0 else 0.0
            wnum += tw * (idx.sum() - 1)
            wden += (idx.sum() - 1)
    tau_w = wnum / wden if wden > 0 else 0.0
    # level-1 study means, then DL between-study component
    thetas, vars_, gs = [], [], []
    for g in set(study):
        idx = study == g
        w = 1 / (vi[idx] + tau_w)
        thetas.append(np.sum(w * yi[idx]) / np.sum(w))
        vars_.append(1 / np.sum(w))
        gs.append(g)
    thetas, vars_ = np.asarray(thetas), np.asarray(vars_)
    r = dl_random(thetas, vars_)
    tau_b = r["_tau2"]
    w = 1 / (vars_ + tau_b)
    est = np.sum(w * thetas) / np.sum(w)
    se = math.sqrt(1 / np.sum(w))
    return dict(k=len(yi), est=est, se=se, lo=est - 1.96 * se, hi=est + 1.96 * se,
                tau_w=tau_w, tau_b=tau_b)


def cluster_robust(yi, vi, study, tau2, est):
    yi, vi, study = np.asarray(yi, float), np.asarray(vi, float), np.asarray(study)
    m = np.isfinite(yi) & np.isfinite(vi) & (vi > 0)
    yi, vi, study = yi[m], vi[m], study[m]
    if len(yi) == 0:
        return np.nan
    w = 1 / (vi + tau2)
    e = yi - est
    G = len(set(study))
    num = 0.0
    for g in set(study):
        num += (np.sum(w[study == g] * e[study == g])) ** 2
    den = np.sum(w) ** 2
    if den <= 0 or G < 2:
        return np.nan
    se = math.sqrt(num / den) * math.sqrt(G / (G - 1))
    return se


def pool(yi, vi, study, scope_label, cluster=None):
    r = dl_random(yi, vi)
    out = {"scope": scope_label, **{k: v for k, v in r.items() if not k.startswith("_")}}
    tl = two_level(yi, vi, study)
    out.update({"two_level_est": tl["est"], "two_level_se": tl["se"],
                "two_level_lo": tl["lo"], "two_level_hi": tl["hi"],
                "tau_w": tl["tau_w"], "tau_b": tl["tau_b"]})
    cr = cluster_robust(yi, vi, study, r["_tau2"], r["est"])
    out.update({"cr_se": cr, "cr_lo": r["est"] - 1.96 * cr if not np.isnan(cr) else np.nan,
                "cr_hi": r["est"] + 1.96 * cr if not np.isnan(cr) else np.nan})
    # Dependence-aware pool (DEPENDENT_EFFECTS / Option C): 3-level model and RVE
    # clustered by cluster_id, with the rho = 0.5 / 0.8 sensitivity.  The plain DL
    # estimate above is kept as the comparison column.
    if cluster is not None:
        dep = SC.pool_dependence(np.asarray(yi, float), np.asarray(vi, float),
                                 np.asarray(cluster), np.asarray(study))
        out.update(dep)
    return out


# ------------------------------------------------------------------ spec construction
def cred_assignments(df, mask):
    """Per contrast credibility role within a contrast subset: credibility or sharing fallback."""
    sub = df[mask]
    studies_with_cred = set(sub.loc[sub["dv_domain"] == "credibility", "study_key"])
    role = {}
    for _, r in sub.iterrows():
        if r["dv_domain"] == "credibility":
            role[(r["study_key"], r["Study_ID"], r["contrast"])] = "credibility"
        elif r["dv_domain"] == "sharing_intention" and r["study_key"] not in studies_with_cred:
            role[(r["study_key"], r["Study_ID"], r["contrast"])] = "sharing_fallback"
    return role, studies_with_cred


def build_spec(df, spec):
    """Return a contrast-level frame for one spec, with spec eligibility + cred role."""
    d = df.copy()
    key = list(zip(d["study_key"], d["Study_ID"], d["contrast"]))
    d["_key"] = key
    # A DiD contrast is eligible when a DiD can be derived from pre/post descriptives OR the
    # coded contrast already spans pre->post and reports the change/DiD effect.
    d["_did_any"] = ((d["did_vi"].notna() & (d["did_vi"] > 0) & d["did_yi"].notna())
                     | (d["prepost_vi"].notna() & (d["prepost_vi"] > 0) & d["prepost_yi"].notna()))
    if spec == "post_all":
        d = d[d["is_post"] & (~d["is_followup"])].copy()
        d["_elig"] = d["post_vi"].notna() & (d["post_vi"] > 0) & d["post_yi"].notna()
    elif spec == "persistence_all":
        # follow-up only: never the immediate posttest, never a baseline
        d = d[d["is_followup"]].copy()
        d["_elig"] = (d["followup_vi"].notna() & (d["followup_vi"] > 0)
                      & d["followup_yi"].notna())
    elif spec == "did_all":
        d = d[d["is_post"] & (~d["is_followup"])].copy()
        d["_elig"] = d["_did_any"]
    elif spec in ("post_cred", "did_cred", "persistence_cred"):
        if spec == "persistence_cred":
            d = d[d["is_followup"]].copy()
            d["_elig"] = (d["followup_vi"].notna() & (d["followup_vi"] > 0)
                          & d["followup_yi"].notna())
        else:
            d = d[d["is_post"] & (~d["is_followup"])].copy()
            d["_elig"] = (d["_did_any"] if spec == "did_cred"
                          else d["post_vi"].notna() & (d["post_vi"] > 0) & d["post_yi"].notna())
        role, _ = cred_assignments(d[d["_elig"]], pd.Series(True, index=d[d["_elig"]].index))
        d["cred_role"] = d["_key"].map(role)
        d = d[d["cred_role"].notna()].copy()
    d = d[d["_elig"]].copy()
    if spec.startswith("did"):
        use_derived = d["did_vi"].notna() & (d["did_vi"] > 0) & d["did_yi"].notna()
        d["spec_yi"] = np.where(use_derived, d["did_yi"], d["prepost_yi"])
        d["spec_vi"] = np.where(use_derived, d["did_vi"], d["prepost_vi"])
        d["spec_src"] = np.where(use_derived, d["did_src"], d["prepost_src"])
    elif spec.startswith("persistence"):
        d["spec_yi"], d["spec_vi"], d["spec_src"] = (d["followup_yi"], d["followup_vi"],
                                                     d["followup_src"])
    else:
        d["spec_yi"], d["spec_vi"], d["spec_src"] = d["post_yi"], d["post_vi"], d["post_src"]
    # Standardised effects only (effect-type audit): a raw beta / log-odds /
    # regression coefficient is NOT poolable with d/g/z.  Derived sources
    # (cells/fields/harm) are standardised by construction; a reported yi/vi effect
    # is checked against its coded `effect_size_type_reported`.
    rep = (d["reported_type"] if "reported_type" in d.columns
           else pd.Series([""] * len(d)))
    cv = (d["conversion_text"] if "conversion_text" in d.columns
          else pd.Series([""] * len(d)))
    d["_std"] = [SC.source_is_standardised(s, r, c)
                 for s, r, c in zip(d["spec_src"], rep, cv)]
    for _, r in d[~d["_std"]].iterrows():
        STD_DROPPED.append({"study_key": r.get("study_key"), "Study_ID": r.get("Study_ID"),
                            "dv_name": r.get("dv_name"), "dv_domain": r.get("dv_domain"),
                            "spec": spec, "source": r.get("spec_src"),
                            "effect_type_reported": r.get("reported_type"),
                            "conversion_text": r.get("conversion_text")})
    d = d[d["_std"]].drop(columns=["_std"]).copy()
    d["spec"] = spec
    if "cred_role" not in d:
        d["cred_role"] = ""
    return d


# ------------------------------------------------------------------ exclusions ON/OFF
def apply_exclusions_off(d, spec, r=R_PRIMARY):
    """For each contrast try a harmonized re-derivation (no exclusion filters)."""
    ys, vs, srcs = [], [], []
    for key, grp in d.groupby("study_key", sort=False):
        data_ok = study_has_data(key)
        res = harm_offsets_study(key, grp, spec) if data_ok else {}
        for _, row in grp.iterrows():
            r0 = res.get((row["Study_ID"], row["contrast"]))
            if r0 is not None and not np.isnan(r0[0]):
                ys.append(r0[0]); vs.append(r0[1]); srcs.append(r0[2] + "|no-exclusions")
                continue
            tag = "paper_only" if not data_ok else "harmonized_unmatched"
            ys.append(row["spec_yi"]); vs.append(row["spec_vi"]); srcs.append(tag)
    d = d.copy()
    d["off_yi"], d["off_vi"], d["off_src"] = ys, vs, srcs
    return d


def primary_composite_frames(d0):
    """Canonical primary/secondary split for one spec frame (W2-parity2).

    ``build_spec`` returns the coded frame carrying ``Study_ID`` but **no**
    ``experiment_id``; ``SC.build_primary_composite``'s default grouping key is
    ``(study_key, experiment_id, dv_domain)``, so without the key the experiment
    dimension is silently dropped and a ``separate_true`` / ``separate_false``
    pair is matched ACROSS experiments of the same study.  Normalizing the key
    here (and only here) makes the spec table, the publication-bias tables and
    the ``Results/metaanalysis.xlsx`` workbook pool exactly the same contrasts.

    Returns ``(prim_on, sec_on, log_on, st_on, prim_off, sec_off, log_off, st_off)``
    for the exclusions-ON (coded) and exclusions-OFF (re-derived) effect columns.
    """
    if d0 is None or not len(d0):
        e = pd.DataFrame()
        return e, e, [], {}, e, e, [], {}
    if "experiment_id" not in d0.columns:
        d0 = d0.copy()
        d0["experiment_id"] = d0["Study_ID"]
    prim_on, sec_on, log_on, st_on = SC.build_primary_composite(
        d0, effect_col="spec_yi", var_col="spec_vi", src_col="spec_src")
    prim_off, sec_off, log_off, st_off = SC.build_primary_composite(
        d0, effect_col="off_yi", var_col="off_vi", src_col="off_src")
    return prim_on, sec_on, log_on, st_on, prim_off, sec_off, log_off, st_off


def _metric_of(src):
    s = str(src)
    if s in ("g", "d"):
        return s
    if s in ("t->z", "r->z"):
        return "z"
    if s == "means/sd" or s.startswith("cells:post") or s.startswith("fields:did") \
            or s.startswith("cells:did") or s.startswith("reported:prepost"):
        return "g"
    return "reported"


def _did_basis_of(src):
    s = str(src)
    if s.startswith("fields"):
        return "did_independent_arms"
    if "cells:did" in s:
        return "did_cells"
    if s.startswith("reported:prepost"):
        return "reported_prepost"
    return ""


def _r_of(src):
    s = str(src)
    return R_PRIMARY if (s.startswith("fields") or "cells:did" in s) else np.nan


def _pval(y, v):
    if not (np.isfinite(y) and np.isfinite(v) and v > 0):
        return np.nan
    return math.erfc(abs(y) / math.sqrt(v) / math.sqrt(2.0))


def tidy_long(df, spec, excl_mode, yi_col, vi_col, src_col):
    df = df.reset_index(drop=True)
    out = pd.DataFrame({
        "layer": "meta", "spec": spec, "exclusions": excl_mode,
        "study_key": df["study_key"], "experiment_id": df["Study_ID"],
        "contrast_id": df["study_key"].astype(str) + "|" + df["Study_ID"].astype(str)
        + "|" + df["contrast"].astype(str),
        "contrast": df["contrast"],
        "dv_name": df["dv_name"], "dv_domain": df["dv_domain"], "cred_role": df["cred_role"],
        "dv_polarity": df["dv_polarity"], "oriented": df["oriented"],
        "dv_family": df.get("dv_family", pd.Series([""] * len(df))),
        "component_side": df.get("component_side", pd.Series([""] * len(df))),
        "cluster_id": df.get("cluster_id", pd.Series([""] * len(df))),
        "derived_from_true": df.get("derived_from_true", pd.Series([""] * len(df))),
        "derived_from_false": df.get("derived_from_false", pd.Series([""] * len(df))),
        "effect_type_reported": df.get("reported_type", pd.Series([""] * len(df))),
        "timepoint_1": df["timepoint_1"], "timepoint": df["timepoint_1"],
        "reported_yi": df["reported_yi"], "reported_vi": df["reported_vi"],
        "yi": df[yi_col], "vi": df[vi_col], "var": df[vi_col], "sei": np.sqrt(df[vi_col]),
        "se": np.sqrt(df[vi_col]),
        "metric": df[src_col].map(_metric_of),
        "p_value": [_pval(y, v) for y, v in zip(df[yi_col], df[vi_col])],
        "fallback": np.where(df["cred_role"].astype(str) == "sharing_fallback",
                             "sharing_intention", ""),
        "did_basis": df[src_col].map(_did_basis_of),
        "source": df[src_col],
        "did_r_used": df[src_col].map(_r_of) if spec.startswith("did") else np.nan,
        # ``r_used`` is the canonical selector column (specs_api.select_spec_rows /
        # validate_fully_within read it).  Writing it here (W2-parity2) means both
        # the suite writer and the standalone ``build_meta_specs`` emit the unified
        # schema, and ``ensure_specs`` can detect a pre-unification file.
        "r_used": df[src_col].map(_r_of) if spec.startswith("did") else np.nan,
        "spans_pre_post": df["spans_pre_post"],
        "data_available": df["study_key"].map(study_has_data).astype(int),
        "restricted": df["dv_domain"].isin(DR.SUSCEPTIBILITY_DOMAINS),
        "exclusions_embedded": (~df[src_col].astype(str).str.contains("no-exclusions|harm:")).astype(int),
    })
    # Derived composite rows carry their own contrast_id (original id + |Dk); keep
    # it so a derived row never collides with the component it was built from.
    if "contrast_id" in df.columns:
        out["contrast_id"] = df["contrast_id"].where(
            df["contrast_id"].notna(), out["contrast_id"])
    return out


def iter_spec_frames(coded):
    """Yield the canonical spec frames, one per entry of ``SPECS`` (W2-parity2).

    Each item is ``(spec, d0, prim_on, sec_on, log_on, st_on, on, off,
    sec_off, log_off, st_off)`` where ``on``/``off`` are the tidy spec rows for
    exclusions ON/OFF.  This is the ONE meta-contrast derivation; ``main()``,
    :func:`write_meta_spec_table` and (through ``primary_composite_frames``) the
    ``Results/metaanalysis.xlsx`` workbook all read it, so the spec-based pool
    and the workbook can never drift apart again.
    """
    for spec in SPECS:
        d0 = build_spec(coded, spec)
        if len(d0) == 0:
            continue
        d0 = apply_exclusions_off(d0, spec)
        (prim_on, sec_on, log_on, st_on,
         prim_off, sec_off, log_off, st_off) = primary_composite_frames(d0)
        on = tidy_long(prim_on, spec, "ON", "spec_yi", "spec_vi", "spec_src")
        off = tidy_long(prim_off, spec, "OFF", "off_yi", "off_vi", "off_src")
        yield (spec, d0, prim_on, sec_on, log_on, st_on, on, off,
               sec_off, log_off, st_off)


def write_meta_spec_table(out=None, coded=None):
    """Write the canonical meta spec table to ``out`` (default ``temp/meta_specs.csv``).

    Shared by ``analysis_multiverse_meta.main()`` (same :func:`iter_spec_frames`
    derivation) and ``suite_common.build_meta_specs`` (the standalone
    ``ensure_specs`` rebuild).  Unlike ``main()`` it builds no figures/battery, so
    it is cheap enough to run from ``publication_bias.py``.

    Compatibility (fully-within switch contract): a coded pre->post change is the
    paper's reported DiD, so its ``source`` is labelled ``did:reported`` (the
    same label ``metaanalysis.py``'s fallback uses) and its ``r_used`` is set to
    the primary r; both are recorded in an extra ``r_used`` column alongside
    ``did_r_used``.  This does not touch any effect or variance.
    """
    out = out or TIDY_CSV
    did_excluded, missing_dropped = [], []
    if coded is None:
        coded, _sk, did_excluded, missing_dropped = load_contrasts(verbose=False, force=True)
    tidy_all, components_all, nondep_log, family_stats = [], [], [], []
    for (spec, d0, prim_on, sec_on, log_on, st_on, on, off,
         sec_off, log_off, st_off) in iter_spec_frames(coded):
        for c, mode, ecol, vcol, scol in ((sec_on, "ON", "spec_yi", "spec_vi", "spec_src"),
                                          (sec_off, "OFF", "off_yi", "off_vi", "off_src")):
            if len(c):
                components_all.append(tidy_long(c, spec, mode, ecol, vcol, scol))
        nondep_log += log_on + log_off
        family_stats.append({"spec": spec, **st_on})
        tidy_all += [on, off]
    tidy = pd.concat(tidy_all, ignore_index=True) if tidy_all else pd.DataFrame()
    if len(tidy):
        tidy["r_used"] = tidy.get("did_r_used", pd.Series(np.nan, index=tidy.index))
        rep = tidy["source"].astype(str).str.startswith("reported:prepost")
        tidy.loc[rep, "source"] = "did:reported"
        tidy.loc[rep, "r_used"] = R_PRIMARY
        tidy.loc[rep, "did_r_used"] = R_PRIMARY
        # Fully-within DiD rows: a single-arm record has no control arm, so the
        # exclusions-OFF path cannot re-derive and keeps the same reported pre->post
        # change (tagged harmonized_unmatched/paper_only).  Label it identically so
        # the fully-within switch contract holds on both ON and OFF rows.
        fw_did = (tidy["study_key"].astype(str).isin(FW_STUDY_KEYS)
                  & tidy["spec"].astype(str).str.startswith("did"))
        tidy.loc[fw_did, "source"] = "did:reported"
        tidy.loc[fw_did, "r_used"] = R_PRIMARY
        tidy.loc[fw_did, "did_r_used"] = R_PRIMARY
        tidy = SC.derive_specs(tidy, "meta")
    comp = (pd.concat(components_all, ignore_index=True) if components_all
            else pd.DataFrame())
    if len(comp):
        comp.to_csv(os.path.join(TEMP, "meta_specs_components.csv"),
                    index=False, encoding="utf-8-sig")
    SC.write_nonredundancy_log(nondep_log)
    pd.DataFrame(family_stats).to_csv(
        os.path.join(TEMP, "meta_family_stats.csv"), index=False, encoding="utf-8-sig")
    # Diagnostic artifacts (mirror main()), so a standalone rebuild leaves the
    # same audit trail behind.
    if did_excluded:
        pd.DataFrame(did_excluded, columns=[
            "study_key", "Study_ID", "dv_name", "dv_domain", "effect_raw", "source", "reason"]
        ).to_csv(os.path.join(TEMP, "meta_did_excluded.csv"),
                 index=False, encoding="utf-8-sig")
    if missing_dropped:
        pd.DataFrame(missing_dropped).to_csv(
            os.path.join(TEMP, "meta_missing_reason_dropped.csv"),
            index=False, encoding="utf-8-sig")
    if METACOG_DROPPED:
        pd.DataFrame(METACOG_DROPPED).to_csv(
            os.path.join(TEMP, "meta_metacognition_strand.csv"),
            index=False, encoding="utf-8-sig")
    if STD_DROPPED:
        _sd = pd.DataFrame(STD_DROPPED).drop_duplicates(
            subset=["study_key", "Study_ID", "dv_name", "dv_domain"])
        _sd.to_csv(os.path.join(TEMP, "meta_nonstandardised_dropped.csv"),
                   index=False, encoding="utf-8-sig")
    if len(tidy):
        tidy.to_csv(out, index=False, encoding="utf-8-sig")
        SC.write_fully_within_marker(out)
    return tidy


# ------------------------------------------------------------------ moderator
def moderator_contrast(yi, vi, study, x):
    import statsmodels.api as sm
    yi, vi, study, x = map(np.asarray, (yi, vi, study, x))
    m = np.isfinite(yi) & np.isfinite(vi) & (vi > 0) & np.isfinite(x)
    yi, vi, study, x = yi[m], vi[m], study[m], x[m]
    if len(yi) < 4 or len(set(x)) < 2:
        return None
    r = dl_random(yi, vi)
    w = 1 / (vi + r["tau2"])
    X = sm.add_constant(x.astype(float))
    try:
        fit = sm.WLS(yi, X, weights=w).fit(cov_type="cluster",
                                           cov_kwds={"groups": study})
    except Exception:
        fit = sm.WLS(yi, X, weights=w).fit()
    b, se, p = fit.params[1], fit.bse[1], fit.pvalues[1]
    return dict(coef=b, se=se, lo=b - 1.96 * se, hi=b + 1.96 * se, p=p,
                k=int(len(yi)), n_studies=int(len(set(study))))


def subgroup(yi, vi, study, cluster, mask):
    yi, vi, study = np.asarray(yi)[mask], np.asarray(vi)[mask], np.asarray(study)[mask]
    cluster = np.asarray(cluster)[mask]
    return pool(yi, vi, study, "subgroup", cluster)


# ------------------------------------------------------------------ publication bias
def egger(yi, vi):
    import statsmodels.api as sm
    yi, vi = np.asarray(yi, float), np.asarray(vi, float)
    m = np.isfinite(yi) & np.isfinite(vi) & (vi > 0)
    yi, vi = yi[m], vi[m]
    if len(yi) < 3:
        return None
    se = np.sqrt(vi)
    snd = yi / se
    prec = 1 / se
    try:
        fit = sm.WLS(snd, sm.add_constant(prec), weights=1 / vi).fit()
        return dict(k=len(yi), intercept=fit.params[0], se=fit.bse[0],
                    t=fit.tvalues[0], p=fit.pvalues[0],
                    slope=fit.params[1])
    except Exception:
        return None


def pet_peese(yi, vi):
    import statsmodels.api as sm
    yi, vi = np.asarray(yi, float), np.asarray(vi, float)
    m = np.isfinite(yi) & np.isfinite(vi) & (vi > 0)
    yi, vi = yi[m], vi[m]
    if len(yi) < 3:
        return None
    se = np.sqrt(vi)
    pet = sm.WLS(yi, sm.add_constant(se), weights=1 / vi).fit()
    peese = sm.WLS(yi, sm.add_constant(vi), weights=1 / vi).fit()
    return dict(k=len(yi),
                pet_int=pet.params[0], pet_se=pet.bse[0], pet_p=pet.pvalues[0], pet_slope=pet.params[1],
                peese_int=peese.params[0], peese_se=peese.bse[0], peese_p=peese.pvalues[0],
                peese_slope=peese.params[1],
                prefer="PEESE" if pet.pvalues[0] < 0.05 else "PET")


def trim_fill(yi, vi, max_iter=200):
    """Trim-and-fill with the L0-style linear rank estimator (Duval & Tweedie 2000).

    Direction: reflect the effect sizes so the potentially suppressed side is the left
    (missing studies are assumed on the side opposite the pooled estimate).  At each
    iteration the ``k0`` largest effects are trimmed, the random-effects estimate ``theta``
    of the remainder is recomputed, and the linear rank count
    ``L0 = #{kept : y < theta} - (n - k0)/2`` gives the next ``k0``.  On convergence the
    ``k0`` largest observed studies are mirrored about ``theta`` (``y' = 2*theta - y``) and
    the augmented set is re-pooled.  Validated on symmetric (k0 = 0) and truncated
    simulated funnels; a sensitivity analysis, not a definitive correction.
    """
    y = np.asarray(yi, float)
    v = np.asarray(vi, float)
    m = np.isfinite(y) & np.isfinite(v) & (v > 0)
    y, v = y[m], v[m]
    n = len(y)
    if n < 4:
        return None

    orig = dl_random(y, v)["est"]
    reflect = bool(orig < 0)
    z = -y if reflect else y.copy()
    order = np.argsort(z)
    z, vz = z[order], v[order]
    k0 = 0
    for _ in range(max_iter):
        kept = z[:n - k0] if 0 < k0 < n else z
        theta = dl_random(kept, vz[:n - k0] if 0 < k0 < n else vz)["est"]
        if not np.isfinite(theta):
            break
        Ln = int(np.sum(kept < theta))
        k0_new = int(round(Ln - (n - k0) / 2.0))
        k0_new = max(0, min(k0_new, n - 2))
        if k0_new == k0:
            break
        k0 = k0_new
    if k0 > 0:
        theta = dl_random(z[:n - k0], vz[:n - k0])["est"]
        mirror = z[n - k0:]
        zf = np.concatenate([z, 2 * theta - mirror])
        vf = np.concatenate([vz, vz[n - k0:]])
    else:
        theta = dl_random(z, vz)["est"]
        zf, vf = z, vz
    adj_y = -zf if reflect else zf
    return dict(k_obs=int(n), k0=int(k0), orig_est=orig,
                adj_est=dl_random(adj_y, vf)["est"],
                side=("right" if reflect else "left"),
                fill_y=list((-zf if reflect else zf)[n:].astype(float)),
                fill_v=list(np.asarray(vf)[n:].astype(float)),
                all_y=list(np.asarray(adj_y, float)), all_v=list(np.asarray(vf, float)))


def leave_one_out(df, col_y="yi", col_v="vi", by="contrast"):
    """Pooled estimate omitting each contrast (and each study) in turn."""
    rows = []
    y, v, s = df[col_y].to_numpy(float), df[col_v].to_numpy(float), df["study_key"].to_numpy()
    full = dl_random(y, v)
    n = len(df)
    for i in range(n):
        idx = np.arange(n) != i
        r = dl_random(y[idx], v[idx])
        rows.append({"unit": f"{df['study_key'].iloc[i]} #{df['contrast'].iloc[i]}",
                     "study_key": df["study_key"].iloc[i], "omitted_est": r["est"],
                     "omitted_lo": r["lo"], "omitted_hi": r["hi"],
                     "delta": r["est"] - full["est"]})
    st = []
    for g in sorted(set(s)):
        idx = s != g
        r = dl_random(y[idx], v[idx])
        st.append({"unit": g, "omitted_est": r["est"], "omitted_lo": r["lo"],
                   "omitted_hi": r["hi"], "delta": r["est"] - full["est"]})
    loo = pd.DataFrame(rows + st)
    if len(loo):
        lim = 2 * full["se"] if np.isfinite(full["se"]) else np.inf
        loo["influential"] = np.abs(loo["delta"]) > lim
    return full, loo


# ------------------------------------------------------------------ graphics
def _save(fig, name):
    import matplotlib
    matplotlib.use("Agg")
    png = os.path.join(FIG, name + ".png")
    pdf = os.path.join(FIG, name + ".pdf")
    fig.savefig(png, dpi=300, bbox_inches="tight")
    fig.savefig(pdf, bbox_inches="tight")
    import matplotlib.pyplot as plt
    plt.close(fig)


def forest_plot(res, per_domain, title, name):
    import matplotlib.pyplot as plt
    rows = [("overall", res)] + sorted(per_domain.items())
    labels = [r[0] for r in rows]
    est = [r[1]["est"] for r in rows]
    lo = [r[1]["lo"] for r in rows]
    hi = [r[1]["hi"] for r in rows]
    ks = [r[1].get("k", 0) for r in rows]
    fig, ax = plt.subplots(figsize=(7, max(3, 0.35 * len(rows) + 1.2)))
    yy = np.arange(len(rows))[::-1]
    ax.errorbar(est, yy, xerr=[np.array(est) - np.array(lo), np.array(hi) - np.array(est)],
                fmt="o", color="#2166ac", ecolor="#888", capsize=3)
    ax.axvline(0, color="grey", lw=0.8, ls="--")
    for i, yv in enumerate(yy):
        ax.text(0.02, yv, f"k={ks[i]}", transform=ax.get_yaxis_transform(),
                ha="right", va="center", fontsize=8)
    ax.set_yticks(yy)
    ax.set_yticklabels(labels, fontsize=9)
    ax.set_xlabel("effect (95% CI)")
    ax.set_title(title, fontsize=10)
    ax.grid(axis="x", alpha=0.25)
    _save(fig, name)


def funnel_plot(yi, vi, egg, title, name):
    import matplotlib.pyplot as plt
    yi, se = np.asarray(yi, float), np.sqrt(np.asarray(vi, float))
    m = np.isfinite(yi) & np.isfinite(se)
    yi, se = yi[m], se[m]
    r = dl_random(yi, se ** 2)
    fig, ax = plt.subplots(figsize=(6, 5))
    ax.scatter(yi, se, s=14, alpha=0.5, color="#2166ac")
    ax.axvline(r["est"], color="#b2182b", lw=1)
    if len(yi) > 2:
        lo_se = np.linspace(0, max(se) if len(se) else 1, 50)
        ax.plot(r["est"] - 1.96 * np.sqrt(lo_se ** 2 + r["tau2"]), lo_se, color="grey", ls="--", lw=0.8)
        ax.plot(r["est"] + 1.96 * np.sqrt(lo_se ** 2 + r["tau2"]), lo_se, color="grey", ls="--", lw=0.8)
    if egg:
        xs = np.linspace(min(yi), max(yi), 10)
        # Egger line in y/SE space is not directly plottable; draw PET-style line instead
        ax.set_title(f"{title}\nEgger intercept={egg['intercept']:.3f} (p={egg['p']:.3f}); k={r['k']}",
                     fontsize=9)
    else:
        ax.set_title(f"{title}; k={r['k']}", fontsize=9)
    ax.invert_yaxis()
    ax.set_xlabel("effect"); ax.set_ylabel("SE")
    ax.grid(alpha=0.25)
    _save(fig, name)


def petpeese_plot(yi, vi, title, name):
    import matplotlib.pyplot as plt
    yi, se = np.asarray(yi, float), np.sqrt(np.asarray(vi, float))
    m = np.isfinite(yi) & np.isfinite(se)
    yi, se = yi[m], se[m]
    fig, ax = plt.subplots(figsize=(6, 5))
    ax.scatter(se, yi, s=14, alpha=0.5, color="#2166ac")
    if len(se) > 2:
        xs = np.linspace(0, max(se), 50)
        pp = pet_peese(yi, se ** 2)
        if pp:
            ax.plot(xs, pp["pet_int"] + pp["pet_slope"] * xs, color="#d95f02", lw=1.2, label="PET")
            ax.plot(xs, pp["peese_int"] + pp["peese_slope"] * xs ** 2, color="#1b9e77", lw=1.2, label="PEESE")
            ax.legend(fontsize=8)
    ax.axhline(0, color="grey", lw=0.8, ls="--")
    ax.set_xlabel("SE"); ax.set_ylabel("effect")
    ax.set_title(title, fontsize=10)
    ax.grid(alpha=0.25)
    _save(fig, name)


def loo_plot(loo, full, title, name):
    import matplotlib.pyplot as plt
    d = loo.copy()
    if len(d):
        # study-level rows only, capped to the 40 most influential (readability)
        d = d[d["unit"] == d["study_key"]].copy()
        d = d.reindex(d["delta"].abs().sort_values(ascending=False).index).head(40)
        d = d.sort_values("delta")
    fig, ax = plt.subplots(figsize=(7, max(3, 0.25 * len(d) + 1)))
    yy = np.arange(len(d))
    ax.errorbar(d["omitted_est"], yy,
                xerr=[d["omitted_est"] - d["omitted_lo"], d["omitted_hi"] - d["omitted_est"]],
                fmt="o", ms=3, color="#2166ac", ecolor="#bbb")
    ax.axvline(full["est"], color="#b2182b", lw=1, label="full")
    ax.set_yticks([]); ax.set_xlabel("leave-one-study-out pooled estimate")
    ax.set_title(title + " (top-40 studies by |change|)", fontsize=10)
    ax.grid(axis="x", alpha=0.25)
    _save(fig, name)


def moderator_plot(mod, sub_avail, sub_paper, title, name):
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(6, 2.6))
    items = [("data_available", sub_avail), ("paper_only", sub_paper)]
    yy = [1, 0]
    for yv, (lab, r) in zip(yy, items):
        ax.errorbar(r["est"], yv, xerr=[[r["est"] - r["lo"]], [r["hi"] - r["est"]]],
                    fmt="o", color="#2166ac", capsize=3)
        ax.text(0.02, yv, f"k={r['k']}", transform=ax.get_yaxis_transform(),
                ha="right", va="center", fontsize=8)
    ax.axvline(0, color="grey", ls="--", lw=0.8)
    ax.set_yticks(yy); ax.set_yticklabels([i[0] for i in items])
    txt = (f"moderator b={mod['coef']:.3f} [{mod['lo']:.3f}, {mod['hi']:.3f}], p={mod['p']:.3f}"
           if mod else "moderator not estimable")
    ax.set_title(f"{title}\n{txt}", fontsize=9)
    ax.grid(axis="x", alpha=0.25)
    _save(fig, name)


# ------------------------------------------------------------------ outputs
def _nonempty(df, col="note"):
    if df is None or (hasattr(df, "empty") and df.empty):
        return pd.DataFrame({col: ["(none)"]})
    return df


def write_suite_workbook(sheets):
    """Merge our meta sheets into analysis_suite.xlsx, preserving any other session's sheets."""
    existing = {}
    if os.path.isfile(SUITE_XLSX):
        try:
            xl = pd.ExcelFile(SUITE_XLSX)
            for name in xl.sheet_names:
                if name not in sheets:
                    existing[name] = xl.parse(name)
        except Exception:
            existing = {}
    merged = {**existing, **sheets}
    with pd.ExcelWriter(SUITE_XLSX, engine="openpyxl") as xl:
        for name, df in merged.items():
            _nonempty(df).to_excel(xl, sheet_name=name[:31], index=False)
    print(f"  wrote {SUITE_XLSX} ({len(merged)} sheets)")


def append_meta_workbook(sheets):
    """Add/replace our `Suite_*` sheets in metaanalysis.xlsx, preserving every other sheet.

    We own only the `Suite_*` sheets, so they are always refreshed (the 15-minute guard would
    otherwise pin them to a stale run); the write is skipped if the workbook cannot be opened.
    """
    from openpyxl import load_workbook
    from openpyxl.utils.dataframe import dataframe_to_rows
    if not os.path.isfile(META_XLSX):
        print("  (metaanalysis.xlsx absent; skipped suite sheets)")
        return
    try:
        wb = load_workbook(META_XLSX)
    except Exception as e:
        print("  (metaanalysis.xlsx busy/unreadable; skipped suite sheets:", e, ")")
        return
    for name, df in sheets.items():
        nm = name[:31]
        if nm in wb.sheetnames:
            del wb[nm]
        ws = wb.create_sheet(nm)
        for row in dataframe_to_rows(_nonempty(df), index=False, header=True):
            ws.append(row)
    try:
        wb.save(META_XLSX)
        print("  refreshed suite sheets in", META_XLSX)
    except Exception as e:
        print("  (metaanalysis.xlsx not writable; skipped suite sheets:", e, ")")


def md_table(df):
    """Markdown pipe table without a tabulate dependency."""
    if df is None or df.empty:
        return "_(none)_"
    d = df.copy()
    for c in d.columns:
        d[c] = d[c].map(lambda v: f"{v:.4f}" if isinstance(v, (float, np.floating))
                        and np.isfinite(v) else str(v))
    cols = [str(c) for c in d.columns]
    lines = ["| " + " | ".join(cols) + " |",
             "| " + " | ".join("---" for _ in cols) + " |"]
    for _, r in d.iterrows():
        lines.append("| " + " | ".join(str(r[c]) for c in d.columns) + " |")
    return "\n".join(lines)


def main():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: F401

    SC.include_fully_within_cli()
    SC.announce_fully_within("meta-layer multiverse")
    print("== meta-layer multiverse ==")
    coded, skipped, did_excluded, missing_dropped = load_contrasts()
    if len(coded) == 0:
        print("no coded contrasts; aborting")
        return 1
    if did_excluded:
        pd.DataFrame(did_excluded, columns=[
            "study_key", "Study_ID", "dv_name", "dv_domain", "effect_raw", "source", "reason"]
        ).to_csv(os.path.join(TEMP, "meta_did_excluded.csv"), index=False, encoding="utf-8-sig")
        print(f"  DiD guard: {len(did_excluded)} contrasts excluded -> temp/meta_did_excluded.csv")
    if missing_dropped:
        pd.DataFrame(missing_dropped).to_csv(
            os.path.join(TEMP, "meta_missing_reason_dropped.csv"),
            index=False, encoding="utf-8-sig")
        print(f"  missing_metrics_reason: {len(missing_dropped)} eligible contrasts excluded from pooling")
    if METACOG_DROPPED:
        pd.DataFrame(METACOG_DROPPED).to_csv(
            os.path.join(TEMP, "meta_metacognition_strand.csv"),
            index=False, encoding="utf-8-sig")
        print(f"  confidence/metacognition: {len(METACOG_DROPPED)} eligible contrasts "
              f"routed to the separate strand")

    results_rows, moderator_rows, egger_rows, pp_rows, tf_rows, cov_rows = [], [], [], [], [], []
    tidy_all, loo_all = [], []
    components_all, nondep_log, family_stats = [], [], []
    spec_frames = {}

    # PI-corrected family rule (DEPENDENT_EFFECTS / Option C): PRIMARY = the
    # composite (constructed from the coded separate_true/separate_false pair
    # whenever both are available, else the directly reported composite);
    # SECONDARY = the separate true/false strand.  A composite is never pooled
    # with its components.  ON and OFF are built separately because they use
    # different effect columns.  ``iter_spec_frames`` is the shared canonical
    # derivation (W2-parity2).
    for (spec, d0, prim_on, sec_on, log_on, st_on, on, off,
         sec_off, log_off, st_off) in iter_spec_frames(coded):
        for c, mode, ecol, vcol, scol in ((sec_on, "ON", "spec_yi", "spec_vi", "spec_src"),
                                          (sec_off, "OFF", "off_yi", "off_vi", "off_src")):
            if len(c):
                components_all.append(tidy_long(c, spec, mode, ecol, vcol, scol))
        nondep_log += log_on + log_off
        family_stats.append({"spec": spec, **st_on})
        spec_frames[spec] = (d0, on, off)
        tidy_all += [on, off]

        for mode, tab, ycol, vcol in (("ON", on, "yi", "vi"), ("OFF", off, "yi", "vi")):
            for scope, mask in (("unrestricted", pd.Series(True, index=tab.index)),
                                ("restricted", tab["restricted"])):
                sub = tab[mask]
                if len(sub) == 0:
                    continue
                r = pool(sub[ycol], sub[vcol], sub["study_key"], scope, sub.get("cluster_id"))
                row = {"layer": "meta", "spec": spec, "exclusions": mode, "scope": scope,
                       **r}
                results_rows.append(row)
            # moderator + publication bias on the spec's full (unrestricted) set
            tab2 = tab.copy()
            tab2["moderator"] = tab2["data_available"].astype(int)
            if tab2["moderator"].nunique() > 1:
                mod = moderator_contrast(tab2[ycol], tab2[vcol], tab2["study_key"], tab2["moderator"])
                sa = subgroup(tab2[ycol], tab2[vcol], tab2["study_key"], tab2["cluster_id"],
                              tab2["moderator"].to_numpy() == 1)
                sp = subgroup(tab2[ycol], tab2[vcol], tab2["study_key"], tab2["cluster_id"],
                              tab2["moderator"].to_numpy() == 0)
                if mod:
                    moderator_rows.append({"layer": "meta", "spec": spec, "exclusions": mode, **mod,
                                           "data_available_est": sa["est"], "data_available_k": sa["k"],
                                           "paper_only_est": sp["est"], "paper_only_k": sp["k"]})
                if mode == "ON":
                    moderator_plot(mod, sa, sp, f"{spec} (meta, exclusions ON)",
                                   f"moderator_{spec}")
            yv = pd.to_numeric(tab2[ycol], errors="coerce")
            vv = pd.to_numeric(tab2[vcol], errors="coerce")
            eg = egger(yv, vv)
            if eg:
                egger_rows.append({"layer": "meta", "spec": spec, "exclusions": mode, **eg})
            pp = pet_peese(yv, vv)
            if pp:
                pp_rows.append({"layer": "meta", "spec": spec, "exclusions": mode, **pp})
                if mode == "ON":
                    petpeese_plot(yv, vv, f"PET-PEESE: {spec} (meta, ON)", f"petpeese_{spec}")
            if mode == "ON":
                try:
                    tf = trim_fill(yv, vv)
                except Exception as e:
                    print("    trim-fill failed", spec, e)
                    tf = None
                if tf:
                    tf_rows.append({"layer": "meta", "spec": spec, "exclusions": mode,
                                    "k_obs": tf["k_obs"], "k0_missing": tf["k0"],
                                    "side": tf["side"], "orig_est": tf["orig_est"],
                                    "adjusted_est": tf["adj_est"]})
                full, loo = leave_one_out(tab2)
                loo.insert(0, "spec", spec)
                loo.insert(0, "exclusions", mode)
                loo.insert(0, "layer", "meta")
                loo_all.append(loo)
                loo_plot(loo, full, f"Leave-one-out: {spec} (meta, ON)", f"loo_{spec}")
                # per-domain forest
                per_dom = {}
                for dom, g in tab2.groupby("dv_domain"):
                    if len(g):
                        per_dom[f"domain:{dom}"] = dl_random(pd.to_numeric(g[ycol], errors="coerce"),
                                                             pd.to_numeric(g[vcol], errors="coerce"))
                fullr = dl_random(yv, vv)
                forest_plot(fullr, per_dom, f"Forest: {spec} (meta, exclusions ON)", f"forest_{spec}")
                funnel_plot(yv, vv, eg, f"Funnel: {spec} (meta, ON)", f"funnel_{spec}")
            if mode == "OFF":
                # exclusions ON vs OFF forest
                pass

        # exclusions ON vs OFF forest
        fig, ax = plt.subplots(figsize=(6, 2.4))
        for i, (lab, tab, yc, vc) in enumerate([("ON", on, "yi", "vi"),
                                                ("OFF", off, "yi", "vi")]):
            yy = pd.to_numeric(tab[yc], errors="coerce")
            vv_ = pd.to_numeric(tab[vc], errors="coerce")
            r = dl_random(yy, vv_)
            ax.errorbar(r["est"], i, xerr=[[r["est"] - r["lo"]], [r["hi"] - r["est"]]],
                        fmt="o", color="#2166ac", capsize=3)
            ax.text(r["hi"], i, f" k={r['k']}", va="center", fontsize=8)
        ax.axvline(0, color="grey", ls="--", lw=0.8)
        ax.set_yticks([0, 1]); ax.set_yticklabels(["OFF", "ON"])
        ax.set_title(f"{spec}: exclusions ON vs OFF (meta)", fontsize=10)
        ax.grid(axis="x", alpha=0.25)
        _save(fig, f"exclusions_onoff_{spec}")

        # exclusions coverage
        cov = off.groupby("source").size().reset_index(name="n")
        cov.insert(0, "exclusions", "OFF"); cov.insert(0, "spec", spec); cov.insert(0, "layer", "meta")
        cov_rows.append(cov)

    # Components sensitivity/specificity strand (never pooled with the primary
    # composites) and the suppressed-contrast audit.
    components = (pd.concat(components_all, ignore_index=True) if components_all
                  else pd.DataFrame())
    if len(components):
        components.to_csv(os.path.join(TEMP, "meta_specs_components.csv"),
                          index=False, encoding="utf-8-sig")
    SC.write_nonredundancy_log(nondep_log)
    pd.DataFrame(family_stats).to_csv(os.path.join(TEMP, "meta_family_stats.csv"),
                                      index=False, encoding="utf-8-sig")
    print(f"  family rule: {sum(s.get('derived_composite_pairs', 0) for s in family_stats)} "
          f"derived composites; components strand rows: {len(components)}")
    if STD_DROPPED:
        _sd = pd.DataFrame(STD_DROPPED).drop_duplicates(
            subset=["study_key", "Study_ID", "dv_name", "dv_domain"])
        _sd.to_csv(os.path.join(TEMP, "meta_nonstandardised_dropped.csv"),
                   index=False, encoding="utf-8-sig")
        print(f"  standardised-effects guard: {len(_sd)} distinct contrasts dropped "
              f"(raw beta/log-odds/non-standardised)")

    tidy = pd.concat(tidy_all, ignore_index=True)
    # Materialise the derived `all` specifications (didpost_* / fupost_*) so the
    # consolidated workbook / figures have them (same rule as the coding app).
    tidy = SC.derive_specs(tidy, "meta")
    tidy.to_csv(TIDY_CSV, index=False, encoding="utf-8-sig")
    SC.write_fully_within_marker(TIDY_CSV)
    print(f"  wrote {TIDY_CSV} ({len(tidy)} rows)")

    results = pd.DataFrame(results_rows)
    moderator = pd.DataFrame(moderator_rows)
    egger_df = pd.DataFrame(egger_rows)
    pp_df = pd.DataFrame(pp_rows)
    tf_df = pd.DataFrame(tf_rows)
    cov_df = pd.concat(cov_rows, ignore_index=True) if cov_rows else pd.DataFrame()
    loo_df = pd.concat(loo_all, ignore_index=True) if loo_all else pd.DataFrame()

    # side-by-side forest of all specs (immediate posttest, persistence, DiD)
    import matplotlib.pyplot as plt
    specs_present = [s for s in SPECS if s in spec_frames]
    fig, ax = plt.subplots(figsize=(6.5, max(2.6, 0.45 * len(specs_present) + 1.0)))
    for i, spec in enumerate(specs_present):
        on = spec_frames[spec][1]
        r = dl_random(on["yi"], on["vi"])
        ax.errorbar(r["est"], i, xerr=[[r["est"] - r["lo"]], [r["hi"] - r["est"]]],
                    fmt="o", color="#2166ac", capsize=3)
        ax.text(r["hi"], i, f" k={r['k']}", va="center", fontsize=8)
    ax.axvline(0, color="grey", ls="--", lw=0.8)
    ax.set_yticks(range(len(specs_present)))
    ax.set_yticklabels(specs_present)
    ax.set_title("All specs, exclusions ON (meta layer)", fontsize=10)
    ax.grid(axis="x", alpha=0.25)
    _save(fig, "forest_four_specs")

    # domain forest (susceptibility domains)
    fig, ax = plt.subplots(figsize=(6, 3))
    post = spec_frames.get("post_all", (None, None, None))[1]
    used = 0
    if post is not None:
        for dom in DR.SUSCEPTIBILITY_DOMAINS:
            g = post[post["dv_domain"] == dom]
            if len(g):
                r = dl_random(g["yi"], g["vi"])
                ax.errorbar(r["est"], used, xerr=[[r["est"] - r["lo"]], [r["hi"] - r["est"]]],
                            fmt="o", color="#2166ac", capsize=3)
                ax.text(r["hi"], used, f" {dom} k={r['k']}", va="center", fontsize=8)
                used += 1
    ax.axvline(0, color="grey", ls="--", lw=0.8)
    ax.set_yticks(range(max(1, used))); ax.set_yticklabels([])
    ax.set_title("Sensitivity domains, post_all (meta, ON)", fontsize=10)
    ax.grid(axis="x", alpha=0.25)
    _save(fig, "forest_domains")

    sheets = {
        "Specs_Summary": results,
        "Effects": tidy,
        "Moderator": moderator,
        "Egger": egger_df,
        "PET_PEESE": pp_df,
        "TrimFill": tf_df,
        "LeaveOneOut": loo_df[loo_df.get("influential", False) == True]
        if len(loo_df) and "influential" in loo_df else loo_df,
        "Exclusions_Coverage": cov_df,
        "Components_Strand": components,
        "Family_Stats": pd.DataFrame(family_stats),
        "Metacognition_Strand": pd.DataFrame(METACOG_DROPPED),
        "Nonstandardised_Dropped": pd.DataFrame(STD_DROPPED),
    }
    # r sensitivity (did specs)
    r_rows = []
    for spec in ("did_all", "did_cred"):
        if spec not in spec_frames:
            continue
        d0 = spec_frames[spec][0]
        for r in R_SENS:
            ycol = f"did_yi_{int(r*100)}" if r != R_PRIMARY else "did_yi"
            vcol = f"did_vi_{int(r*100)}" if r != R_PRIMARY else "did_vi"
            # derived DiD at this r, else the reported pre-post effect (r-independent)
            dy = d0[ycol].where(d0[ycol].notna(), d0["prepost_yi"])
            dv = d0[vcol].where(d0[vcol].notna(), d0["prepost_vi"])
            dd = d0[(dy.notna()) & (dv > 0)].copy()
            dd["_y"], dd["_v"] = dy[(dy.notna()) & (dv > 0)], dv[(dy.notna()) & (dv > 0)]
            if "cred_role" in dd:
                dd = dd[dd["cred_role"].notna()]
            if len(dd):
                # same family rule as the main pool: composite primary, components secondary
                dd, _, _, _ = SC.build_primary_composite(dd, effect_col="_y", var_col="_v")
            if len(dd):
                rr = pool(dd["_y"], dd["_v"], dd["study_key"], "unrestricted",
                          dd.get("cluster_id"))
                r_rows.append({"layer": "meta", "spec": spec, "r": r,
                               "k": rr["k"], "est": rr["est"], "lo": rr["lo"], "hi": rr["hi"],
                               "tau2": rr["tau2"], "i2": rr["i2"]})
    sheets["r_sensitivity"] = pd.DataFrame(r_rows)

    # credibility fallbacks table
    fb = []
    for spec in SPECS:
        if spec not in spec_frames:
            continue
        on = spec_frames[spec][1]
        if "cred_role" not in on or not on["cred_role"].isin(["sharing_fallback"]).any():
            continue
        for _, r in on[on["cred_role"] == "sharing_fallback"].iterrows():
            fb.append({"spec": spec, "study_key": r["study_key"], "Study_ID": r["experiment_id"],
                       "contrast": r["contrast"], "dv_name": r["dv_name"],
                       "dv_domain": r["dv_domain"], "role": "sharing_fallback"})
    sheets["Credibility_Fallbacks"] = pd.DataFrame(fb)

    write_suite_workbook(sheets)
    append_meta_workbook({f"Suite_{k}": v for k, v in sheets.items()
                          if k in ("Specs_Summary", "Effects")})

    write_note(results, moderator, egger_df, pp_df, tf_df, cov_df, tidy, skipped, did_excluded)
    print("\n== results (meta) ==")
    print(results.to_string(index=False))
    print("\nmoderator:")
    print(moderator.to_string(index=False) if len(moderator) else "  (none)")
    print("\nnote:", NOTE_MD)
    return 0


META_START = "<!-- META-SECTION:START -->"
META_END = "<!-- META-SECTION:END -->"


def write_note(results, moderator, egger_df, pp_df, tf_df, cov_df, tidy, skipped, did_excluded):
    lines = []
    lines.append(f"_Generated {time.strftime('%Y-%m-%d %H:%M')} by "
                 "`Scripts/analysis_multiverse_meta.py`._\n")
    lines.append("This is the **meta (reported-effects)** section of `ANALYSIS_SUITE_SPEC.md`; the "
                 "signal-detection (SDT) section is produced by the SDT-layer session.\n")
    lines.append("## Definitions\n")
    lines.append("- **Unit**: one eligible coded contrast (`eligible_primary == 1`); contrasts are "
                 "nested in studies.\n")
    lines.append("- **post_all / post_cred**: the immediate-post contrast (follow-ups excluded). "
                 "The effect is the coded/reported effect, or a Hedges' g derived from posttest "
                 "cell means when the coded effect is missing.\n")
    lines.append("- **persistence_all / persistence_cred**: follow-up only. The same effect rule "
                 "as the posttest, applied at the follow-up timepoint. Never pooled with the "
                 "immediate posttest.\n")
    lines.append(f"- **did_all / did_cred**: `(post_IG - pre_IG) - (post_CG - pre_CG)`, "
                 f"standardised by the pooled pretest SD, requiring a pretest and an immediate "
                 f"posttest. Pre-post correlation r = {R_PRIMARY:.2f} primary; "
                 f"r = 0.30 / 0.70 sensitivity (see `r_sensitivity`).\n")
    lines.append("- **Baselines**: pretest rows are never outcomes; they enter only the DiD.\n")
    lines.append("- **Polarity**: every effect is oriented so positive = more desirable, using the "
                 "coded `dv_polarity` (1 = higher is better, 0 = higher is worse); reported effects "
                 "are multiplied by the sign and derived-from-descriptives effects are already "
                 "oriented. The unoriented reported value is kept in `reported_yi`; contrasts with "
                 "unknown polarity are flagged `oriented = 0`.\n")
    lines.append("- **Models (dependence-aware, Option C)**: every pooled cell reports the "
                 "plain one-level DerSimonian–Laird estimate (`dl_*`, the previous model, kept as "
                 "the comparison column), a **three-level** random-effects model (`three_*`: effect "
                 "within experiment x sample within study, with `tau2_cluster` and `tau2_study`), "
                 "and **robust variance estimation** (`rve_*`) with cluster-robust SEs clustered by "
                 "`cluster_id` (study x experiment x shared control arm x DV family), at rho = 0.5 "
                 "and rho = 0.8 (`rve_rho80_*`), mirroring the DiD r sensitivity. "
                 "`max_weight_share_dl` / `max_weight_share_rve` report the inverse-variance weight "
                 "share of the largest contributing experiment/cluster per spec x domain, so a "
                 "single large experiment (e.g. Biddlestone et al. 2026) can be shown not to "
                 "dominate.\n")
    lines.append("- **Family rule (primary composite / secondary components)**: the meta layer and "
                 "the SDT re-analysis are separate products and are never mixed. In the meta layer "
                 "the primary pool is the **composite**: when a cell codes both `separate_true` and "
                 "`separate_false` effects the composite is constructed from them as "
                 "`effect_true_oriented + effect_false_oriented` (variance = sum of the two "
                 "variances; the positive within-person covariance is unavailable, so this is "
                 "conservative), and a directly reported composite in the same cell is demoted; "
                 "when the pair is unavailable the directly reported composite is used; `other` "
                 "DVs always stay primary. Every `separate_*` effect is routed to the "
                 "`Components_Strand` sheet (sensitivity/specificity), where the Biddlestone "
                 "non-manipulative backfire stays visible, and is never pooled with a composite. "
                 "The suppressed-contrast audit is `Scripts/NONREDUNDANCY_SUPPRESSED.csv`.\n")
    lines.append(f"- **Skipped** {len(skipped)} extraction files changed in the last 15 min.\n")
    lines.append(f"- **DiD quality guard**: {len(did_excluded)} contrasts with "
                 f"|standardised DiD| > {DID_ABS_MAX} were dropped (coded SDs behaving like SEs) "
                 f"and listed in `temp/meta_did_excluded.csv`.\n")
    lines.append("## Effects per spec (meta layer)\n")
    if len(results):
        cols = ["spec", "exclusions", "scope", "k", "est", "lo", "hi", "i2",
                "two_level_est", "two_level_lo", "two_level_hi", "cr_se", "cr_lo", "cr_hi"]
        lines.append(md_table(results[[c for c in cols if c in results]]))
    lines.append("\n## Credibility fallbacks (sharing_intention used where no credibility contrast)\n")
    fb = [r for r in tidy.to_dict("records") if r.get("cred_role") == "sharing_fallback"]
    lines.append(f"Fallback contrasts: **{len(fb)}** across "
                 f"{len(set(r['study_key'] for r in fb))} studies.\n")
    lines.append("## Exclusions ON vs OFF\n")
    if len(cov_df):
        cov = cov_df.copy()
        cov["category"] = cov["source"].astype(str).map(
            lambda s: "harmonized_rederived" if s.startswith("harm:") else s)
        cov_agg = (cov.groupby(["spec", "exclusions", "category"], as_index=False)["n"].sum())
        red = int(cov.loc[cov["category"] == "harmonized_rederived", "n"].sum())
        lines.append(f"- Harmonized re-derivations (no exclusion filters): **{red}** contrast-runs.\n"
                     f"- `paper_only`: no usable data, coded effect retained (author sample embedded).\n"
                     f"- `harmonized_unmatched`: data exist but no confident DV match, coded effect "
                     f"retained.\n")
        lines.append(md_table(cov_agg))
    lines.append("\n## Moderator: data_available vs paper_only\n")
    lines.append(md_table(moderator) if len(moderator) else "(not estimable)")
    lines.append("\n## Publication bias\n")
    lines.append("Egger:\n")
    lines.append(md_table(egger_df) if len(egger_df) else "(none)")
    lines.append("\nPET-PEESE:\n")
    lines.append(md_table(pp_df) if len(pp_df) else "(none)")
    lines.append("\nTrim-and-fill (L0-style linear rank estimator):\n")
    lines.append(md_table(tf_df) if len(tf_df) else "(none)")
    lines.append("\n## Limitations (meta layer)\n")
    lines.append("- **did_cred** is near-empty: credibility judgements are usually measured "
                 "post-exposure only, so very few contrasts have a pretest + immediate posttest. "
                 "The spec is reported with its actual k and the sharing_intention fallback.\n")
    lines.append("- **Exclusions OFF** only changes effects that could be confidently matched to "
                 "a harmonized participant-level variable; all other effects keep the coded value "
                 "and are flagged `paper_only`/`harmonized_unmatched`.\n")
    lines.append("- The publication-bias tests inherit the mixed effect-metric pool of "
                 "`metaanalysis.py` (Hedges g, Fisher z, log-odds, raw betas); on `did_all` the "
                 "derived DiD values can be extreme because some papers report implausibly small "
                 "pretest SDs. PET-PEESE/VOC per-metric is left as a future refinement.\n")
    lines.append("- Trim-and-fill uses an L0-style linear rank estimator (validated on simulated "
                 "symmetric/truncated funnels), not a specific software build; k0 is reported as a "
                 "sensitivity.\n")
    lines.append("\n## Outputs\n")
    lines.append("- `temp/meta_specs.csv` — tidy per-effect table (spec x exclusions x contrast).\n"
                 "- `temp/meta_did_excluded.csv` — contrasts dropped by the DiD quality guard.\n"
                 "- `analysis_suite.xlsx` — Specs_Summary, Effects, Moderator, Egger, PET_PEESE, "
                 "TrimFill, LeaveOneOut, Exclusions_Coverage, r_sensitivity, Credibility_Fallbacks.\n"
                 "- `metaanalysis.xlsx` — `Suite_*` sheets (existing sheets untouched).\n"
                 "- `figures/*.png|pdf` — forest / funnel / LOO / moderator / PET-PEESE / "
                 "exclusions-on-off / four-spec / domains.\n")
    lines.append("## Reproduce\n```\npython Scripts/analysis_multiverse_meta.py\n```\n")
    body = META_START + "\n## Meta (reported-effects) layer\n\n" + "\n".join(lines) + "\n" + META_END
    if os.path.isfile(NOTE_MD):
        old = open(NOTE_MD, encoding="utf-8").read()
        if META_START in old and META_END in old:
            pre, rest = old.split(META_START, 1)
            _, post = rest.split(META_END, 1)
            if "analysis_multiverse_meta.py" in pre or "Meta (reported-effects) layer" in pre:
                pre = ""                       # drop an earlier unmarked meta duplicate
            new = ((pre.rstrip() + "\n\n") if pre.strip() else "") + body + post
        elif re.search(r"signal.?detection|\bsdt\b", old, re.I) \
                and "analysis_multiverse_meta.py" not in old:
            new = old.rstrip() + "\n\n" + body + "\n"   # keep another session's SDT section
        else:
            new = "# ANALYSIS SUITE NOTE\n\n" + body + "\n"
    else:
        new = "# ANALYSIS SUITE NOTE\n\n" + body + "\n"
    open(NOTE_MD, "w", encoding="utf-8").write(new)
    print("  wrote", NOTE_MD)


if __name__ == "__main__":
    sys.exit(main())
