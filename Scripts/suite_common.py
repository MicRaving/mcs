"""Shared definitions, spec builders and publication-bias statistics for the
analysis suite (``Scripts/ANALYSIS_SUITE_SPEC.md``).

Two layers
----------
* ``meta`` — the reported-effects layer: one effect per coded ``DV_Metric`` in
  ``Extraction/*.json`` (derived exactly as in ``metaanalysis.py``).
* ``sdt``  — the signal-detection layer: one discrimination index
  ``d = mean(true) - mean(false)`` per study x artifact x domain x timepoint,
  computed from the harmonized participant-level data (polarity already applied,
  so higher always = more desirable).

Six specifications (section A of the spec, extended 2026-09-17)
-------------------------------------------------------------
``did_all``         DiD, all DVs (pretest-corrected posttest difference)
``did_cred``        DiD, credibility only (fallback: sharing_intention)
``post_all``        immediate-posttest only, all DVs
``post_cred``       immediate-posttest only, credibility (fallback: sharing_intention)
``persistence_all`` follow-up only, all DVs (persistence of the effect)
``persistence_cred``follow-up only, credibility (fallback: sharing_intention)

plus the exclusions-on/off robustness (section B) and the
``data_available`` / ``paper_only`` moderator (section C).

The spec tables written here (``temp/meta_specs.csv`` / ``temp/sdt_specs.csv``)
are the prerequisite consumed by ``publication_bias.py`` and ``figures.py``.
Every derivation is recorded per row (``source``, ``metric``, ``did_basis``,
``r_used``, ``fallback`` ...) so nothing is invented.  All choices are
documented in ``Scripts/ANALYSIS_SUITE_NOTE.md``.
"""
import glob
import json
import math
import os
import re
import fnmatch
import sys
import tempfile
import time

import numpy as np
import pandas as pd

try:
    from scipy import stats as _st
except Exception:  # pragma: no cover - scipy is present in this environment
    _st = None

B = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(B, "temp")
# Contract (AGENTS.md): analysis-suite outputs live under Results/
FIGDIR = os.path.join(B, "Results", "figures")
DS = os.path.join(B, "Datasets")
EXTRACT = os.path.join(B, "Extraction")
os.makedirs(TMP, exist_ok=True)
os.makedirs(FIGDIR, exist_ok=True)
tempfile.tempdir = TMP

sys.path.insert(0, os.path.join(B, "Scripts"))
# The controlled dv_domain vocabulary and the susceptibility family live in
# domain_restriction.py.  2026-09-19: `belief_accuracy` is merged into
# `credibility` (PI decision; see Scripts/DV_DOMAIN_MERGE_LOG.md), so
# DR.SUSCEPTIBILITY_DOMAINS == {credibility, sharing_intention,
# technique_recognition} and DR.resolve_domain / DR.resolve_sidecar_domain map
# any legacy label accordingly.
import domain_restriction as DR  # noqa: E402
import study_exclusions as SE  # noqa: E402
# Single source of every estimator (stat_core) and every stdlib-only coding
# contract (contract).  suite_common re-exports them unchanged for back-compat.
import contract as CT  # noqa: E402
import stat_core as _sc  # noqa: E402

_st = _sc._st  # one scipy handle for the whole module

META_SPECS = os.path.join(TMP, "meta_specs.csv")
SDT_SPECS = os.path.join(TMP, "sdt_specs.csv")
# The SDT-layer session also writes temp/sdt_specs.csv with its own vocabulary
# (no sampling-variance column).  When that file is present but cannot drive a
# publication-bias analysis, our own spec table is built here and used instead,
# leaving the shared file untouched.
SDT_SPECS_FALLBACK = os.path.join(TMP, "sdt_specs_pb.csv")

SPEC_COLS = [
    "layer", "spec", "exclusions", "study_key", "experiment_id", "contrast_id",
    "dv_name", "dv_domain", "veracity", "timepoint", "metric", "effect", "effect_raw",
    "var", "se", "n1", "n2", "n_total", "r_used", "p_value", "p_censored",
    "data_available", "contrast_type", "source", "fallback", "did_basis",
    "exclusions_embedded", "cond_ambiguous", "oriented", "note",
    # Dependence structure (DEPENDENT_EFFECTS / Option C, 2026-09-19): the
    # non-redundancy family of the contrast (difference/composite vs
    # separate_component vs other), the component side for a separate_* row
    # ('true'/'false'), and the cluster identity used by the dependence-aware
    # pooling (study x experiment x control arm x family).
    "dv_family", "component_side", "cluster_id",
    # Reported effect-size type (for the standardised-effects guard and the
    # effect-type composition of the final pool).
    "effect_type_reported",
    # SDT-layer decomposition: the two components of the discrimination index
    # (mean endorsement of true vs false stimuli, 0-1 rescaled), so true score,
    # fake score and discrimination can be reported separately.  Absent (NaN)
    # for the meta layer.
    "true_score", "true_var", "fake_score", "fake_var",
    # Response bias, the SDT criterion c = -0.5 * [z(hit) + z(false alarm)]
    # (hit = true_score, false alarm = 1 - fake_score on the oriented scale).
    "criterion_score", "criterion_var",
]

# ---------------------------------------------------------------------------
# small statistics
# ---------------------------------------------------------------------------


def num(x):
    return _sc.num(x)


# ---------------------------------------------------------------------------
# small statistics  (implementations live in stat_core; these are re-exports)
# ---------------------------------------------------------------------------
_PROBIT_EPS = _sc._PROBIT_EPS


def _probit(x, n=None):
    """Inverse standard-normal CDF (delegates to stat_core.probit; D4 n-pass)."""
    return _sc.probit(x, n=n)


def _dprime(hits, false_alarms, n=None):
    """SDT discrimination ``d' = z(HR) - z(FAR)`` (delegates to stat_core.dprime)."""
    return _sc.dprime(hits, false_alarms, n=n)


def dl_random(yi, vi):
    """DerSimonian-Laird random-effects pooled estimate (stat_core.dl_random)."""
    return _sc.dl_random(yi, vi)


def fe_pool(yi, vi):
    """Fixed-effect pooled estimate (stat_core.fe_pool)."""
    return _sc.fe_pool(yi, vi)


def _wls(y, x, w):
    """WLS ``y = a + b*x`` (stat_core.wls; model-based inference when unclustered)."""
    return _sc.wls(y, x, w)


def wls(y, x, w, groups=None, rho=0.0):
    """WLS/CRVE meta-regression fitter (stat_core.wls; D2)."""
    return _sc.wls(y, x, w, groups=groups, rho=rho)


def moderator_contrast(yi, vi, study, x):
    """Single-moderator meta-regression contrast (stat_core.moderator_contrast)."""
    return _sc.moderator_contrast(yi, vi, study, x)


def egger_pet(yi, vi):
    return _sc.egger_pet(yi, vi)


def peese(yi, vi):
    return _sc.peese(yi, vi)


def pet_peese(yi, vi):
    return _sc.pet_peese(yi, vi)


def _midranks(a):
    return _sc._midranks(a)


def trim_and_fill(yi, vi, estimator="L0", side="auto"):
    return _sc.trim_and_fill(yi, vi, estimator=estimator, side=side)


def two_level_re(yi, vi, study):
    return _sc.two_level_re(yi, vi, study)


def cluster_robust(yi, vi, study, p=1):
    """Fixed-effect pooled estimate with study-clustered CR1 sandwich SE (D3)."""
    return _sc.cluster_robust(yi, vi, study, p=p)


def _cluster_seq(clusters):
    return _sc._cluster_seq(clusters)


def _exchangeable_inverse(v, rho):
    return _sc._exchangeable_inverse(v, rho)


def rve_intercept(yi, vi, cluster, rho=0.5):
    return _sc.rve_intercept(yi, vi, cluster, rho=rho)


def three_level_re(yi, vi, cluster, study):
    return _sc.three_level_re(yi, vi, cluster, study)


def max_weight_share(yi, vi, cluster, tau2=0.0):
    return _sc.max_weight_share(yi, vi, cluster, tau2=tau2)


def pool_dependence(yi, vi, cluster, study, rho_values=(0.5, 0.8)):
    return _sc.pool_dependence(yi, vi, cluster, study, rho_values=rho_values)


def leave_one_out(yi, vi, unit, is_study=False):
    return _sc.leave_one_out(yi, vi, unit, is_study=is_study)


def p_curve(pvals):
    return _sc.p_curve(pvals)


def p_uniform(yi, vi):
    return _sc.p_uniform(yi, vi)


def hedges_g(m1, s1, n1, m2, s2, n2):
    return _sc.hedges_g(m1, s1, n1, m2, s2, n2)


# Polarity sign (0 / "0" / False / 0.0 => higher is worse) -- canonical in contract.
polarity_sign = CT.polarity_sign

POST_EXACT = {
    "post", "posttest", "post_test", "post_decision", "post_training", "post_w2",
    "post_intervention", "post_0_to_7h", "post_verification", "post_intervention_1",
    "post_t4", "post_7h_window", "post (time 2)", "lecture1_post", "immediate",
    "first_endline_immediate", "t1_immediate", "time 1 (immediate)", "endline",
    "post_video", "post_treatment", "post_message",
    # --- added 2026-09-19 (TIMEPOINT-HARDENING).  Each of these previously fell
    # to ``other`` (silently dropped from both the posttest and the persistence
    # pools) and is the immediate posttest of its study. ---
    "post_intervention_2",   # Craig & Vijaykumar (2023): Intervention 2, single session
    "post_attack",           # Hester et al. (2022): immediately after the attack message
    "posttreatment",         # Oswald et al. (2026): Wave-1 posttreatment block
    "postpartum_hospital",   # Natoli (2015): in-hospital breastfeeding posttest
    "after_labeling",        # Leicht et al. (2022): post-labeling engagement period
    "label_removed",         # Leicht et al. (2022): NYT link after the label was removed
    "endline (3-5 months)",  # Tjaden & Gninafon (2022): the study's only posttest (DiD)
    "pei",                   # Edelsztein & Vazquez (2021): first post-evaluation (1 week)
}

#: Labels that are unambiguously a delayed / follow-up measurement.
FOLLOWUP_EXACT = {
    "time 2 (7-day)",        # Pluviano et al. (2017, 2019): 7-day retention test
    "peii",                  # Edelsztein & Vazquez (2021): second post-eval (1 month)
    "peiii",                 # Edelsztein & Vazquez (2021): transfer task (3 months after PEII)
    "t2_2weeks",             # Seffen et al. (2026): 2-week follow-up
}

#: Labels that are unambiguously a pretest / baseline.
PRE_EXACT = {
    "cross_sectional", "cross-sectional",  # Bayrak et al. (2025 a): no timepoint (survey)
    "label_present",         # Leicht et al. (2022): NYT link before the label was removed
}

#: Positional T-labels.  CASE IS SIGNIFICANT and the mapping is resolved from the
#: studies that use them (a ``T2`` is not portable across designs):
#:   * Gidron et al. (2023): T1 = baseline, T2 = immediately post, T3 = 1-week follow-up;
#:   * Stekelenburg et al. (2021): T0 = baseline, T3 = final (4-week) wave;
#:   * Siebert & Siebert (2023): single-session sequence t1 = baseline,
#:     t2 = post-misinformation, t3 = post-retraction, t4 = post-debiasing
#:     (no delay, so t2/t3/t4 are ``post``).
T_LABEL_EXACT = {
    "T0": "pre", "T1": "pre", "T2": "post", "T3": "followup", "T4": "followup",
    "t0": "pre", "t1": "pre", "t2": "post", "t3": "post", "t4": "post",
}

#: Explicit per-study overrides for a label that cannot be resolved from the label
#: alone: ``(study_key, raw label) -> bucket``.  Kept as a documented hook; the
#: corpus resolves its only genuinely context-dependent family (``wave*``) through
#: the coded ``posttest_type`` below, so this table is currently empty.
TIMEPOINT_STUDY_OVERRIDES = {}


def _wave_bucket(dv, label):
    """Resolve a ``wave*`` label for the study that emitted it.

    Wave labels are not portable: Nyhan et al. (2022) use waves 2 and 3 both for
    the *immediate* effect of freshly randomized content (``posttest_type = 1``)
    and for the *delayed* persistence of earlier content (``posttest_type = 2``).
    The coded ``posttest_type`` (1=immediate, 2=delayed, 3=both, 4=cross-sectional,
    5=other; see the coding brief) is therefore authoritative when present.  Without
    it the wave index is used: wave 2 is the immediate posttest (randomization
    happens at wave-2 onset in the corpus) and later waves are follow-ups.
    """
    pt = None
    if isinstance(dv, dict):
        pt = dv.get("posttest_type")
    try:
        pti = int(pt)
    except (TypeError, ValueError):
        pti = None
    if pti == 1:
        return "post"
    if pti in (2, 3):
        return "followup"
    if pti == 4:
        return "pre"
    m = re.search(r"wave\s*([0-9]+)", str(label or ""))
    if m and int(m.group(1)) <= 2:
        return "post"
    return "followup"


def classify_timepoint(tp, dv=None, study_key=None):
    """Map a coded ``timepoint_1``/``timepoint_2`` to a routing bucket.

    Thin adapter over :func:`contract.classify_timepoint` (the canonical
    classifier); the vocabulary and the ``wave*`` resolution are unchanged.
    """
    return CT.classify_timepoint(tp, dv=dv, study_key=study_key)


# Pre-fix classifier, kept ONLY so the self-check can report the before/after
# bucket distribution and every label reassigned.  Do not use in the pipeline.
_LEGACY_POST_EXACT = frozenset({
    "post", "posttest", "post_test", "post_decision", "post_training", "post_w2",
    "post_intervention", "post_0_to_7h", "post_verification", "post_intervention_1",
    "post_t4", "post_7h_window", "post (time 2)", "lecture1_post", "immediate",
    "first_endline_immediate", "t1_immediate", "time 1 (immediate)", "endline",
    "post_video", "post_treatment", "post_message",
})


def _legacy_classify_timepoint(tp):
    """The pre-2026-09-19 classifier, for the self-check's before/after report."""
    if tp is None or str(tp).strip() in ("", "none", "nan", "None"):
        return "post_assumed"
    s = str(tp).strip().lower()
    if s in _LEGACY_POST_EXACT:
        return "post"
    if "follow" in s or s.startswith("fu") or "wave" in s:
        return "followup"
    if ("pre" in s or "baseline" in s or "pretreat" in s or "before" in s
            or s in ("base", "cross_sectional")):
        return "pre"
    if "change" in s or "minus" in s:
        return "change"
    return "other"


#: Explicit allow-list of labels that may legitimately remain ``other``; every
#: entry states a reason.  Empty: every corpus label is resolved to a bucket.
TIMEPOINT_OTHER_ALLOWLIST = {}


def audit_timepoint_classification(verbose=True):
    """Self-check: classify every coded timepoint over ``Extraction/*.json``.

    Prints the legacy-vs-current bucket distribution and every label reassigned,
    and raises ``AssertionError`` when any label still routes to ``other`` and is
    not in ``TIMEPOINT_OTHER_ALLOWLIST``.  Returns a summary dict.  It does not
    build the spec tables or touch any dataset, so it can be run while the
    ``Extraction`` layer is being edited::

        python Scripts/suite_common.py --timepoints
    """
    from collections import Counter, defaultdict

    before, after = Counter(), Counter()
    reassigned = defaultdict(Counter)
    other = []
    n_labels = 0
    for p in sorted(glob.glob(os.path.join(EXTRACT, "*.json"))):
        key = os.path.splitext(os.path.basename(p))[0]
        try:
            data = json.load(open(p, encoding="utf-8"))
        except Exception as e:  # a partially written file during an edit
            if verbose:
                print("skip", key, e)
            continue
        data = data if isinstance(data, list) else [data]
        for o in data:
            if not isinstance(o, dict):
                continue
            for dv in o.get("DV_Metrics") or []:
                if not isinstance(dv, dict):
                    continue
                for slot in ("timepoint_1", "timepoint_2"):
                    tpv = dv.get(slot)
                    b0 = _legacy_classify_timepoint(tpv)
                    b1 = classify_timepoint(tpv, dv=dv, study_key=key)
                    before[b0] += 1
                    after[b1] += 1
                    n_labels += 1
                    if b1 != b0:
                        reassigned[(str(tpv), b0, b1)][key] += 1
                    if b1 == "other":
                        other.append((key, str(tpv)))

    other_labels = sorted({lab for _, lab in other})
    unexplained = [lab for lab in other_labels if lab not in TIMEPOINT_OTHER_ALLOWLIST]

    if verbose:
        print(f"timepoint classification self-check: {n_labels} coded slots, "
              f"{len(other)} rows still 'other'")
        print("\n  bucket        before   after")
        for b in ("post", "post_assumed", "pre", "followup", "change", "other"):
            print(f"  {b:<12s} {before.get(b, 0):7d} {after.get(b, 0):7d}")
        print("\n  reassigned labels (raw -> from -> to  xN  [studies]):")
        for (lab, b0, b1), studies in sorted(
                reassigned.items(), key=lambda kv: (-sum(kv[1].values()), kv[0][0])):
            n = sum(studies.values())
            st = ", ".join(f"{k} x{v}" for k, v in sorted(studies.items()))
            print(f"    {lab!r}: {b0} -> {b1}  x{n}  [{st}]")
        if other_labels:
            print("\n  labels still 'other':")
            for lab in other_labels:
                reason = TIMEPOINT_OTHER_ALLOWLIST.get(lab, "UNEXPLAINED")
                print(f"    {lab!r}: {reason}")
        else:
            print("\n  no label routes to 'other'.")

    summary = {
        "n_slots": n_labels, "before": dict(before), "after": dict(after),
        "reassigned": {f"{lab}|{b0}->{b1}": sum(v.values())
                       for (lab, b0, b1), v in reassigned.items()},
        "other_labels": other_labels, "unexplained_other": unexplained,
    }
    if unexplained:
        raise AssertionError(
            "timepoint labels silently routed to 'other': "
            + ", ".join(repr(x) for x in unexplained)
            + " -- resolve them or add a reason to TIMEPOINT_OTHER_ALLOWLIST")
    return summary


_TRUE_TOKENS = re.compile(
    r"non[-_ ]?manipulat|nonmanipulat|non[-_ ]?misinformation|"
    r"\btrue\b|\breal\b|\bfactual\b|\baccurate\b|legitimate|genuine", re.I)
_FALSE_TOKENS = re.compile(
    r"\bmanipulat|misinformation|\bfalse\b|\bfake\b|misleading|inaccurate", re.I)
_DIFF_TOKENS = re.compile(r"discern|_diff\b|difference|signal_detection", re.I)


def dv_veracity(dv):
    """Canonical true/false/'' stimulus-veracity label for one coded ``DV_Metric``.

    Thin adapter over :func:`contract.dv_veracity` (the single decoder also used
    by the app).  A difference/composite has no stimulus veracity and returns
    ``""``; a confidence DV returns ``""``.
    """
    return CT.dv_veracity(dv.get("dv_name") if isinstance(dv, dict) else "",
                          (dv.get("score_types") if isinstance(dv, dict) else None),
                          "", (dv.get("dv_domain") if isinstance(dv, dict) else None))


# ---------------------------------------------------------------------------
# Dependence families and cluster identity  (DEPENDENT_EFFECTS / Option C)
# ---------------------------------------------------------------------------
# A DV whose score combines true and false (or manipulative and non-manipulative)
# stimuli into ONE index is a *composite*; the per-veracity DVs are its
# *components*.  The composite is an exact linear combination of the components
# (D = A - B), so pooling the composite together with its components inflates k
# and the apparent precision without adding independent information.  The
# non-redundancy guard (see ``nonredundancy_guard``) keeps exactly one family
# per (spec x exclusions x study x experiment x domain) cell.

DV_FAMILY_DIFFERENCE = "difference"          # difference / composite / signal-detection
DV_FAMILY_COMPONENT = "separate_component"   # separate true/false (or manip/non-manip)
DV_FAMILY_OTHER = "other"

# schema v3.0 ``score_types`` tokens that identify a composite.
_DIFFERENCE_SCORE_TYPES = {
    "discernment_difference", "proportion_difference", "signal_detection_auc",
    "signal_detection_d_prime", "signal_detection_other", "composite",
}
# ``score_types`` tokens that identify a separate stimulus-veracity component.
_COMPONENT_SCORE_TYPES = {
    "separate_true", "separate_false", "separate_manipulative", "separate_nonmanipulative",
}
# free-text ``discernment_defined_as`` fallback for a difference/composite.
_DIFFERENCE_DEFINED_RE = re.compile(
    r"discern|\bdiff\b|difference|minus|non[-_ ]?manipulat.*[-−]\s*manipulat|"
    r"true.*[-−]\s*false|d['\u2032]?\b|auc", re.I)


_COMPONENT_TRUE_TYPES = {"separate_true", "separate_nonmanipulative"}
_COMPONENT_FALSE_TYPES = {"separate_false", "separate_manipulative"}


def dv_component_side(dv):
    """Return ``'true'``/``'false'`` for a per-veracity component DV, else ``''``.

    Thin adapter over :func:`contract.dv_component_side`.
    """
    return CT.dv_component_side(dv.get("dv_name") if isinstance(dv, dict) else "",
                                (dv.get("score_types") if isinstance(dv, dict) else None))


# Metacognition / confidence is a separate strand (AGENTS.md / DR.NON_OUTCOME_DOMAINS):
# confidence and certainty ratings are NOT misinformation outcomes and must never enter
# the susceptibility primary pool or the SDT pool.
_METACOG_RE = re.compile(r"metacog|confidence|certainty", re.I)


def is_metacognition(domain, dv_name=""):
    """True when a DV is a confidence/certainty/metacognition rating.

    Thin adapter over :func:`contract.is_metacognition`.
    """
    return CT.is_metacognition(domain, dv_name)


# Effect types that are NOT on a standardised (d/g/z) scale and must not be pooled
# with standardised effects.  The coded `yi`/`vi` slot is the standardised effect
# input; a reported raw beta / log-odds / regression coefficient is not.
_RAW_EFFECT_RE = re.compile(
    r"\b(beta|log|logit|logistic|odds|or|coef|coefficient|regression|b)\b", re.I)
# Explicit markers that the reported value was converted to a standardised scale.
_STD_MARKER_RE = re.compile(
    r"\bhedges\b|\bcohen\b|\bsmd\b|\bfisher\b|\bstandardis|\bstandardiz|"
    r"\betasq|\bd\b|\bg\b|\bz\b", re.I)


def is_standardised_metric(metric):
    """True when a derived effect metric is on a standardised (d/g/z) scale."""
    return _sc.is_standardised_metric(metric)


def conversion_text(dv):
    """Concatenated free-text conversion metadata of one coded DV."""
    return _sc.conversion_text(dv)


def source_is_standardised(src, reported_type="", conv_text=""):
    """True when the pooled effect behind ``src`` is standardised.

    Thin adapter over :func:`stat_core.source_is_standardised`.
    """
    return _sc.source_is_standardised(src, reported_type, conv_text)


def dv_family(dv):
    """Classify one coded ``DV_Metric`` into a dependence family.

    Thin adapter over :func:`contract.dv_family` (difference / separate_component /
    other, from the explicit ``score_types`` tokens then ``discernment_defined_as``).
    """
    return CT.dv_family(dv.get("dv_name") if isinstance(dv, dict) else "",
                        (dv.get("score_types") if isinstance(dv, dict) else None),
                        (dv.get("discernment_defined_as") if isinstance(dv, dict) else ""))


def _norm_arm_label(x):
    """Normalise a control-arm label to a stable token ('control_messi_football_video')."""
    if x is None:
        return ""
    if isinstance(x, (list, tuple, set)):
        x = " ".join(str(v) for v in x if v is not None)
    s = str(x).strip()
    if s == "" or s.lower() in ("nan", "none", "<na>"):
        return ""
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


_ARM_SPLIT_RE = re.compile(r"\bvs\.?\b|\bversus\b|\bcompared\s+(?:to|with)\b", re.I)


def control_arm_identity(dv):
    """Derive the shared control/comparison arm identity of one coded contrast.

    The schema has no dedicated control-arm field, so the identity is taken
    from the contrast/group labels, in order:
      1. ``group2_label`` (the comparison arm in the coded contrast);
      2. the text after the last ``vs`` / ``versus`` / ``compared to`` in
         ``comparison_label``;
      3. the literal ``"unknown"``.

    The value is normalised to a lowercase token (parentheticals kept, because
    they usually distinguish two control videos, e.g. ``Control (Messi football
    video)``).  This is the "shared control arm" key of the cluster identity:
    several intervention arms that are each contrasted against the *same*
    control thus receive the *same* control-arm token.
    """
    lab = dv.get("group2_label")
    if not _norm_arm_label(lab):
        comp = str(dv.get("comparison_label") or "")
        parts = _ARM_SPLIT_RE.split(comp)
        if len(parts) >= 2:
            lab = parts[-1]
    tok = _norm_arm_label(lab)
    return tok or "unknown"


def contrast_cluster_id(study_key, experiment_id, dv, family=None, control_arm=None):
    """Cluster identity of one meta-layer contrast: study x experiment x shared control arm.

    Effects sharing all three are non-independent (same sample, same control arm,
    possibly different outcome families) and are clustered together by the RVE /
    3-level models.  Multi-arm studies that contrast several intervention arms
    against one shared control therefore land in the *same* cluster (Option C)
    instead of being counted as independent.  ``dv_family`` is tracked as its own
    column and does NOT enter the cluster key: since the non-redundancy guard has
    already routed components out of the primary pool, the composite and any
    ``other`` outcomes in a cell are measured on the same participants and must be
    clustered together (splitting by family would understate their dependence).
    ``family``/``control_arm`` remain accepted for backward compatibility.
    """
    ctrl = control_arm if control_arm is not None else control_arm_identity(dv)
    return "|".join([str(study_key), str(experiment_id), str(ctrl)])


def sdt_cluster_id(study_key, artifact, family=None):
    """Cluster identity of one SDT (discrimination) contrast: study x artifact.

    Every SDT contrast is a true-vs-false discrimination composite, so the
    experiment/artifact already identifies the shared participant sample.  Accepts
    scalars or pandas Series (vectorised), so a whole column can be passed.
    """
    try:
        return study_key.astype(str) + "|" + artifact.astype(str)
    except AttributeError:
        return "|".join([str(study_key), str(artifact)])


# ---------------------------------------------------------------------------
# Omnibus guard (W7-STRUCT)
# ---------------------------------------------------------------------------
# A multi-df omnibus F/chi2 (or a ``contrast_type == 11`` model term) is NOT an
# intervention-vs-control contrast: it compares >2 cells at once, so its effect
# cannot be signed or pooled as a two-arm standardised mean difference.  The
# systematic extraction defect coded such a statistic as the
# ``eligible_primary == 1`` effect (or stored a ``d`` derived from it); the meta
# layer then pooled it.  ``is_omnibus_eligible`` is the ONE predicate shared by
# the effect builder (here and ``metaanalysis.effect``), the spec-table derivation
# (``analysis_multiverse_meta``) and ``validate_extraction``: when it is true the
# builder REFUSES to derive an effect.  The predicate mirrors
# ``validate_extraction.is_omnibus_eligible`` exactly so the work queue and the
# guard can never disagree.
_EFFECT_METRIC_FIELDS = ("yi", "vi", "d_val", "g_val", "t_val", "r_val", "z_val",
                         "beta_val", "odds_ratio")
_OMNIBUS_METRIC_FIELDS = ("F_val", "chi2_val", "eta2_p")
_PAIRWISE_EST_EXACT = {"d", "g", "d_val", "g_val", "cohen_d", "cohens_d", "hedges_g"}
_PAIRWISE_EST_TOKENS = ("cohen", "hedges", "smd", "standardis", "standardiz")
#: Dedup set for the durable ``omnibus_eligible_skipped`` log (one record per contrast/run).
_OMNIBUS_SKIP_LOGGED = set()


def _nonblank(v):
    return v is not None and not (isinstance(v, str) and v.strip() == "")


def dv_has_descriptives(dv):
    """True when the DV carries usable per-condition means + SDs + arm n's.

    Mirrors ``validate_extraction.has_descriptives``: a single stray ``n1i`` (arm
    flow) does not count; a non-empty ``cell_descriptives`` list does.
    """
    if not isinstance(dv, dict):
        return False
    if dv.get("cell_descriptives"):
        return True
    for pre, post in (("m1i", "m1i_post"), ("sd1i", "sd1i_post"),
                      ("m2i", "m2i_post"), ("sd2i", "sd2i_post")):
        if not _nonblank(dv.get(pre)) and not _nonblank(dv.get(post)):
            return False
    if not _nonblank(dv.get("n1i")) and not _nonblank(dv.get("n1i_post")):
        return False
    if not _nonblank(dv.get("n2i")) and not _nonblank(dv.get("n2i_post")):
        return False
    return True


def _is_omnibus_style_effect(dv):
    """True when ``effect_size_type_reported`` is omnibus-style (not a reported d/g)."""
    est = str(dv.get("effect_size_type_reported") or "").strip().lower()
    if any(t in est for t in _PAIRWISE_EST_TOKENS):
        return False
    return est not in _PAIRWISE_EST_EXACT


def has_multidf_omnibus(dv):
    """True when a multi-df F/chi2/eta2 omnibus statistic is coded."""
    if not isinstance(dv, dict):
        return False
    dfn = num(dv.get("df_num"))
    if dfn is None or not (dfn > 1):
        return False
    return any(_nonblank(dv.get(k)) for k in _OMNIBUS_METRIC_FIELDS)


def is_omnibus_eligible(dv):
    """Guard predicate: refuse to pool this row as an intervention effect.

    True for an ``eligible_primary == 1`` row whose only usable evidence for an
    intervention-vs-control contrast is a multi-df omnibus (or which is a
    ``contrast_type == 11`` model term), with no per-condition descriptives and
    no reported pairwise d/g.  See the module note above.
    """
    if not isinstance(dv, dict) or dv.get("eligible_primary") != 1:
        return False
    if str(dv.get("contrast_type") or "").strip() == "11":
        return True
    if not has_multidf_omnibus(dv):
        return False
    if dv_has_descriptives(dv):
        return False
    if not _is_omnibus_style_effect(dv):
        return False
    return True


def log_omnibus_skip(study_key, experiment_id, dv, where=""):
    """Append one durable ``omnibus_eligible_skipped`` record (deduped in-process).

    ``Results/analysis_log.jsonl`` is the durable audit trail; the dedup set means
    the several call sites in one run record each contrast exactly once.
    """
    dv = dv if isinstance(dv, dict) else {}
    sig = (str(study_key or ""), str(experiment_id or ""),
           str(dv.get("dv_name") or ""), str(dv.get("contrast_type") or ""),
           str(dv.get("df_num") or ""),
           str(dv.get("F_val") or dv.get("chi2_val") or dv.get("eta2_p") or ""))
    if sig in _OMNIBUS_SKIP_LOGGED:
        return False
    _OMNIBUS_SKIP_LOGGED.add(sig)
    try:
        _adir = os.path.join(B, "Scripts", "analysis")
        if _adir not in sys.path:
            sys.path.insert(0, _adir)
        import analysis_log as AL
        metric = ("F" if _nonblank(dv.get("F_val"))
                  else "chi2" if _nonblank(dv.get("chi2_val")) else "eta2")
        AL.log("omnibus_eligible_skipped", study=study_key, experiment=experiment_id,
               dv_name=dv.get("dv_name"), contrast_type=dv.get("contrast_type"),
               df_num=dv.get("df_num"), df_den=dv.get("df_den"), metric=metric,
               eligible_primary=dv.get("eligible_primary"), where=where)
    except Exception:
        pass
    return True


def meta_effect(dv):
    """Return (yi, vi, metric, how) for one coded DV contrast.

    Thin adapter over :func:`stat_core.meta_effect` (identical derivation), with
    the W7-STRUCT omnibus guard: an ``eligible_primary == 1`` row whose only
    evidence is a multi-df omnibus (or a ``contrast_type == 11`` term) is refused
    (NaN), so it can never be pooled as an intervention effect.
    """
    if is_omnibus_eligible(dv):
        return np.nan, np.nan, "none", ""
    return _sc.meta_effect(dv)


def _meta_did(dv, r=0.5):
    """DiD from a coded DV: (post_IG - pre_IG) - (post_CG - pre_CG).

    Thin adapter over :func:`stat_core.meta_did`.
    """
    return _sc.meta_did(dv, r=r)


def _data_available_map():
    """study_key (lower) -> 1 if a harmonized participant dataset exists."""
    out = {}
    for f in glob.glob(os.path.join(DS, "*", "* - harmonized.csv")):
        key = os.path.basename(os.path.dirname(f))
        out[key.lower()] = 1
    return out


# ---------------------------------------------------------------------------
# Per-intervention model (Scripts/PER_INTERVENTION_MODEL.md, ratified 2026-09-20)
# ---------------------------------------------------------------------------
# An experiment record carries its *parent* (default) coding in the usual fields
# and may additionally carry an ``Interventions`` list (NOT a schema field; it
# must be preserved by every read/write path).  Each entry is a distinct
# misinformation intervention nested in the experiment:
#
#     {"Intervention_ID": "<Study_ID>_I1",
#      "Intervention_Type": "friction",          # always a single type
#      "label": "optional human label",
#      "overrides": {"<field>": <value>, ...}}   # ONLY fields that differ
#
# ``record_interventions`` / ``effective_record`` are the single resolution path
# used by every analysis script, so intervention-level overrides are honoured and
# a record with no ``Interventions`` key stays a single analysis unit (backward
# compatibility: its outputs are reproduced exactly).
INTERVENTIONS_KEY = "Interventions"


def _intervention_entries(record):
    """The record's explicit ``Interventions`` entries (dicts only), else ``[]``."""
    return CT._intervention_entries(record)


def record_has_interventions(record):
    """True when the record carries an explicit (non-empty) ``Interventions`` list."""
    return CT.record_has_interventions(record)


def record_interventions(record):
    """One entry per intervention of an experiment record (contract)."""
    return CT.record_interventions(record)


def effective_record(record, intervention=None):
    """Parent coding of ``record`` deep-merged with one intervention's overrides.

    Thin adapter over :func:`contract.effective_record` (the canonical resolver).
    """
    return CT.effective_record(record, intervention)


def intervention_id_of(dv):
    """Binding id of a coded DV row: its ``Intervention_ID`` or '' (shared)."""
    if isinstance(dv, dict):
        return str(dv.get("Intervention_ID") or "").strip()
    return ""


def dv_interventions(record, dv):
    """The interventions a coded DV contrast is evaluated under.

    A DV bound to an ``Intervention_ID`` belongs to that intervention only; an
    unbound DV is shared by every intervention of the experiment (one implicit
    intervention for an un-migrated record).  A bound DV whose id is not declared
    falls back to the full intervention list so it can never be silently dropped.
    """
    ivs = record_interventions(record)
    if not _intervention_entries(record):
        return ivs
    iid = intervention_id_of(dv)
    if not iid:
        return ivs
    hits = [iv for iv in ivs if str(iv.get("Intervention_ID") or "").strip() == iid]
    return hits or ivs


def effective_dvs(record):
    """``[(intervention, effective_record, dv_index, dv)]`` for one experiment.

    The experiment is expanded to one effective record per intervention; each
    coded DV is yielded once per intervention it belongs to (bound -> that one;
    unbound -> every intervention).  A record without ``Interventions`` yields a
    single implicit unit carrying the parent coding and every DV once, so the
    legacy single-unit output is unchanged.  ``dv_index`` is the 0-based position
    in the effective record's ``DV_Metrics`` (the stable contrast number).
    """
    explicit = bool(_intervention_entries(record))
    out = []
    for iv in record_interventions(record):
        eff = effective_record(record, iv)
        dvs = eff.get("DV_Metrics") or []
        if not isinstance(dvs, list):
            continue
        iid = str(iv.get("Intervention_ID") or "").strip()
        for i, dv in enumerate(dvs):
            if not isinstance(dv, dict):
                continue
            if explicit:
                bind = intervention_id_of(dv)
                if bind and bind != iid:
                    continue
            out.append((iv, eff, i, dv))
    return out


def effective_dvs_unique(record, study_key=None):
    """``effective_dvs``, but a shared (unbound) contrast counts once per experiment.

    Shared-primary-DV 'count once' rule (``UNBOUND_DROP_REPORT.md`` §3, applied in the
    primary-pool builders): an **unbound** DV row (no ``Intervention_ID``) describes an
    experiment-level contrast that cannot be attributed to a single arm.  Expanding it
    once per arm (the default ``effective_dvs`` behaviour, which the per-arm moderator
    analyses still need) would pool the same evidence once per arm in the primary pool.
    Here every unbound row is yielded only on its **first** intervention, keyed by
    ``(study_key, Study_ID, dv_index)``; unbound ``eligible_primary == 0`` rows are
    de-duplicated the same way so the exported contrast sheets do not repeat them either.

    The single retained row keeps an **empty** ``Intervention_ID`` (experiment-level) and
    the record-level ``Intervention_Type`` membership (the union over the experiment's
    interventions), so a multi-type experiment still contributes under every type to the
    intervention-type subgroup.  Bound DVs and single-unit records are returned unchanged.
    """
    explicit = bool(_intervention_entries(record))
    if not explicit:
        yield from effective_dvs(record)
        return
    sid = str((record or {}).get("Study_ID") or "")
    skey = str(study_key or "")
    seen = set()
    for iv, eff, i, dv in effective_dvs(record):
        dv_bind = intervention_id_of(dv) if isinstance(dv, dict) else ""
        if dv_bind:
            yield iv, eff, i, dv
            continue
        k = (skey, sid, i)
        if k in seen:
            continue
        seen.add(k)
        # Experiment-level shared row: keep the union type membership, drop the arm id.
        codes = intervention_type_codes_effective(record, dv)
        shared_iv = {"Intervention_ID": "", "Intervention_Type": codes, "overrides": {}}
        shared_eff = dict(eff)
        shared_eff["Intervention_Type"] = codes
        shared_eff.pop("Intervention_ID", None)
        yield shared_iv, shared_eff, i, dv


def intervention_type_codes_effective(record, dv=None):
    """Effective ``Intervention_Type`` code(s) of one contrast.

    Returns a list of raw codes: the effective type of the intervention the DV
    is bound to, or the union over the experiment's interventions for an unbound
    DV / a record read without a DV context.  Legacy records return their parent
    ``Intervention_Type`` unchanged.
    """
    ivs = dv_interventions(record, dv) if dv is not None else record_interventions(record)
    codes = []
    for iv in ivs:
        eff = effective_record(record, iv)
        for c in intervention_type_codes(eff.get("Intervention_Type")):
            if c not in codes:
                codes.append(c)
    return codes


# ---------------------------------------------------------------------------
# intervention-type vocabulary (paper-ready labels)
# ---------------------------------------------------------------------------

INTERVENTION_TYPE_LABELS = {
    "accuracy_nudge": "Accuracy prompt",
    "accuracy_prompts": "Accuracy prompt",
    "community_correction": "Community correction",
    "correction": "Correction",
    "credibility_labels": "Credibility labels",
    "debunking": "Debunking",
    "debunking_and_rebuttals": "Debunking / rebuttals",
    "fact_checking_training": "Fact-checking training",
    "friction": "Friction",
    "generic": "Generic / umbrella",
    "inoculation": "Inoculation / prebunking",
    "lateral_reading": "Lateral reading",
    "media_literacy": "Media literacy",
    "media_literacy_tips": "Media-literacy tips",
    "mix": "Multi-component",
    "none": "No intervention",
    "openmindedness_intervention": "Open-mindedness",
    "other": "Other",
    "platform_exposure_reduction": "Platform exposure reduction",
    "prebunking_warning": "Prebunking warning",
    "social_norms": "Social norms",
    "source_credibility_labels": "Source-credibility labels",
    "sourcing": "Sourcing",
    "verification": "Verification",
    "warning_and_fact_checking_labels": "Warning / fact-check labels",
}
INTERVENTION_TYPE_UNKNOWN = "Unclassified"


def intervention_type_codes(value):
    """Normalise a (possibly list-valued) Intervention_Type to a list of codes."""
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip() for x in value if x is not None and str(x).strip()]
    c = str(value or "").strip()
    return [c] if c else []


def intervention_type_label(code):
    """Paper-ready label; list-valued types are joined for display."""
    if isinstance(code, (list, tuple, set)):
        labs = []
        for c in code:
            lab = intervention_type_label(c)
            if lab not in labs:
                labs.append(lab)
        return ", ".join(labs) if labs else INTERVENTION_TYPE_UNKNOWN
    c = str(code or "").strip()
    if not c:
        return INTERVENTION_TYPE_UNKNOWN
    return INTERVENTION_TYPE_LABELS.get(c, c.replace("_", " ").strip().capitalize())


def _iter_extraction_records():
    """Yield ``(study_key, record)`` over ``Extraction/*.json`` (parse-skip safe)."""
    for p in sorted(glob.glob(os.path.join(EXTRACT, "*.json"))):
        key = os.path.splitext(os.path.basename(p))[0]
        try:
            data = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        data = data if isinstance(data, list) else [data]
        for o in data:
            if isinstance(o, dict):
                yield key, o


# ---------------------------------------------------------------------------
# Fully-within studies (single arm, no no-misinformation control)
# ---------------------------------------------------------------------------
# Some experiments report ONLY within-subject pre->post effects: a single arm
# (``n_arms == 1``) with no no-misinformation-intervention control, so every
# eligible primary DV is a within-person contrast (``contrast_type == 4``).  The
# PI ratified those within-person effects as valid but decided they must be
# EXCLUDED from every pooled analysis BY DEFAULT and included only on explicit
# opt-in (``INCLUDE_FULLY_WITHIN=1`` env var or ``--include-fully-within``).
#
# The marker is the dedicated record field ``Fully_Within``.  The coded ``Title``
# is NEVER inspected (the historical ``[fw] `` prefix has been stripped).
FULLY_WITHIN_ENV = CT.FULLY_WITHIN_ENV
FULLY_WITHIN_MARKER_SUFFIX = CT.FULLY_WITHIN_MARKER_SUFFIX


def _env_truthy(value):
    """True for the conventional affirmative values (1/true/yes/on)."""
    return CT._env_truthy(value)


def include_fully_within():
    """True when fully-within studies are explicitly opted in (contract)."""
    return CT.include_fully_within()


def include_fully_within_cli(argv=None):
    """Honour ``--include-fully-within`` on the command line, then report the switch."""
    return CT.include_fully_within_cli(argv)


def is_fully_within(record):
    """True when one experiment record is a fully-within (no-control) study.

    Thin adapter over :func:`contract.is_fully_within` (the canonical rule).
    """
    return CT.is_fully_within(record)


_STUDY_FW_CACHE = {}


def study_fully_within(study_key):
    """True when EVERY experiment record of a study key is fully-within.

    Dataset-level twin of ``is_fully_within`` used by the participant-level
    scripts, which iterate ``Datasets/<Key>/`` folders and have no record at hand.
    """
    key = str(study_key)
    if key in _STUDY_FW_CACHE:
        return _STUDY_FW_CACHE[key]
    out = False
    try:
        with open(os.path.join(EXTRACT, key + ".json"), encoding="utf-8") as fh:
            data = json.load(fh)
        recs = [o for o in (data if isinstance(data, list) else [data]) if isinstance(o, dict)]
        out = bool(recs) and all(is_fully_within(o) for o in recs)
    except Exception:
        out = False
    _STUDY_FW_CACHE[key] = out
    return out


def fully_within_report(n_skipped, what="record(s)"):
    """One consistent transparency line for the fully-within filter."""
    state = "included (opt-in)" if include_fully_within() else "excluded"
    switch = ("INCLUDE_FULLY_WITHIN=1" if include_fully_within()
              else "INCLUDE_FULLY_WITHIN unset")
    return (f"fully-within: {n_skipped} {what} {state} "
            f"[{switch}; pass --include-fully-within to include]")


def announce_fully_within(context=""):
    """Print one transparent start-of-run line stating the fully-within switch.

    Every consumer (headless script or app subprocess) calls this once at the top
    of a run, so the switch state is visible even when no fully-within record is
    encountered.  The vocabulary is identical everywhere:
    ``EXCLUDE (default)`` unless ``INCLUDE_FULLY_WITHIN`` is truthy, in which case
    ``INCLUDE (opt-in)``.  Returns the resolved boolean.
    """
    on = include_fully_within()
    env = os.environ.get(FULLY_WITHIN_ENV)
    tag = (f"{FULLY_WITHIN_ENV}={env}" if env not in (None, "")
           else f"{FULLY_WITHIN_ENV} unset")
    state = "INCLUDE (opt-in)" if on else "EXCLUDE (default)"
    where = f" {context}:" if context else ":"
    print(f"fully-within switch{where} {state} [{tag}]; "
          f"pass --include-fully-within or set {FULLY_WITHIN_ENV}=1 to include")
    return on


def write_fully_within_marker(path):
    """Record the include/exclude state beside a spec table (contract)."""
    return CT.write_fully_within_marker(path)


def fully_within_marker_matches(path):
    """True when ``path`` was last written under the current include switch (contract)."""
    return CT.fully_within_marker_matches(path)


def intervention_type_map():
    """{(study_key, Study_ID or '')} -> paper-ready intervention-type label.

    Read through the per-intervention model: the label is the union of the
    *effective* ``Intervention_Type`` of every intervention of the record, so
    overrides are honoured.  Legacy records (no ``Interventions``) resolve to
    their parent list exactly as before.
    """
    out = {}
    for key, o in _iter_extraction_records():
        codes = intervention_type_codes_effective(o)
        lab = intervention_type_label(codes)
        out[(key, str(o.get("Study_ID") or ""))] = lab
        out.setdefault((key, ""), lab)
    return out


def intervention_type_codes_map():
    """{(study_key, Study_ID or '')} -> list of *effective* Intervention_Type codes.

    Read through the per-intervention model (union of the interventions'
    effective types); legacy records return their parent list unchanged.
    """
    out = {}
    for key, o in _iter_extraction_records():
        codes = intervention_type_codes_effective(o)
        out[(key, str(o.get("Study_ID") or ""))] = codes
        out.setdefault((key, ""), codes)
    return out


def attach_intervention_type(df, layer):
    """Add an ``intervention_type`` column (meta layer only; blank elsewhere).

    A spec row that already carries an effective ``intervention_type`` (written by
    the per-intervention spec builder) is labelled from it; otherwise the value is
    resolved through the per-intervention type maps (legacy behaviour).
    """
    df = df.copy()
    if layer != "meta" or not len(df):
        df["intervention_type"] = ""
        return df
    m = intervention_type_map()

    def _one(sk, ex, own):
        own = str(own or "").strip()
        if own:
            return intervention_type_label(own.split(";")[0].strip())
        return m.get((str(sk), str(ex)), m.get((str(sk), ""), INTERVENTION_TYPE_UNKNOWN))

    own = (df["intervention_type"].astype(str).to_numpy()
           if "intervention_type" in df.columns else [""] * len(df))
    df["intervention_type"] = [
        _one(sk, ex, ow)
        for sk, ex, ow in zip(df["study_key"].astype(str),
                              df.get("experiment_id", pd.Series([""] * len(df))).astype(str),
                              own)
    ]
    return df


def explode_intervention_types(df, layer):
    """One row per intervention type (membership) for subgroup pooling.

    A contrast whose effective ``Intervention_Type`` is ``[inoculation, friction]``
    contributes to both groups; a contrast bound to one intervention contributes
    only to that intervention's type.  A spec row carrying an effective
    ``intervention_type`` uses it; otherwise the record's type map is consulted
    (legacy).  Meta layer only; other layers get a blank ``intervention_type``.
    """
    out = df.copy()
    if layer != "meta" or not len(out):
        out["intervention_type"] = ""
        return out
    codes_map = intervention_type_codes_map()
    has_own = "intervention_type" in out.columns
    rows = []
    for _, r in out.iterrows():
        d = r.to_dict()
        codes = None
        if has_own:
            own = str(r.get("intervention_type") or "").strip()
            if own:
                codes = [c.strip() for c in own.split(";") if c.strip()]
        if codes is None:
            codes = (codes_map.get((str(r.get("study_key")), str(r.get("experiment_id", ""))))
                     or codes_map.get((str(r.get("study_key")), "")) or [])
        if not codes:
            rows.append(dict(d, intervention_type=INTERVENTION_TYPE_UNKNOWN))
        else:
            for c in codes:
                rows.append(dict(d, intervention_type=intervention_type_label(c)))
    return pd.DataFrame(rows)


def _cred_fallback(df, spec_prefix):
    """Given a frame of candidate contrasts, apply the credibility-only rule.

    A study contributes only its credibility contrasts if it has any; otherwise
    its sharing_intention contrasts are used and every such row is tagged
    ``fallback = 'sharing_intention'`` (section A).
    """
    out = []
    for key, g in df.groupby("study_key"):
        c = g[g["dv_domain"] == "credibility"]
        if len(c):
            out.append(c)
        else:
            s = g[g["dv_domain"] == "sharing_intention"].copy()
            s["fallback"] = "sharing_intention"
            if len(s):
                out.append(s)
    if not out:
        return df.iloc[0:0].copy()
    return pd.concat(out, ignore_index=True)


DID_ABS_MAX = 3.0


DERIVED_SPEC_NAMES = ("didpost_all", "didpost_cred", "fupost_all", "fupost_cred")


def _derived_key(r, layer, kind):
    """Outcome identity used to decide which immediate rows a base row supersedes."""
    if kind == "did" and layer == "meta":
        return (str(r.get("study_key")), str(r.get("experiment_id", "")),
                str(r.get("contrast_id", "")))
    if layer == "sdt":
        # SDT rows already carry the artifact in ``experiment_id`` and the
        # outcome domain in ``dv_domain``; ``dv_name`` differs between the
        # immediate (``discrimination[dom]``) and corrected/follow-up
        # (``discrimination_change[dom]`` / follow-up) measurements of the same
        # outcome, so keying on it would keep both and double-count.
        return (str(r.get("study_key")), str(r.get("experiment_id", "")),
                str(r.get("dv_domain") or ""))
    return (str(r.get("study_key")), str(r.get("experiment_id", "")),
            str(r.get("dv_name") or r.get("dv_domain") or ""))


def _derive_one(df, layer, primary, secondary, new_spec, kind):
    out = []
    for _, g in df.groupby("exclusions", dropna=False):
        sec = g[g["spec"] == secondary]
        if not len(sec):
            continue
        pop = set(sec["study_key"].astype(str))
        base = g[(g["spec"] == primary) & (g["study_key"].astype(str).isin(pop))]
        superseded = {_derived_key(r, layer, kind) for _, r in base.iterrows()}
        rest = sec[[_derived_key(r, layer, kind) not in superseded
                    for _, r in sec.iterrows()]]
        d = pd.concat([base, rest], ignore_index=True)
        d["spec"] = new_spec
        out.append(d)
    return pd.concat(out, ignore_index=True) if out else df.iloc[0:0].copy()


def derive_specs(df, layer):
    """Materialise the derived ``all`` specifications (``didpost_*`` / ``fupost_*``).

    Same rule as the coding app: the study population is the immediate
    specification's (``post_*``), so a study is never added merely because it has
    a follow-up or a pretest correction; within that population the
    corrected/delayed measurement supersedes the immediate row for the same
    outcome.  Materialising them here means every downstream table (pooled,
    publication-bias battery, per-domain, moderator, ...) and figure is produced
    for the ``all`` options too.
    """
    if not len(df) or "exclusions" not in df.columns:
        return df
    add = []
    for cred in (False, True):
        sec = "post_cred" if cred else "post_all"
        did = "did_cred" if cred else "did_all"
        per = "persistence_cred" if cred else "persistence_all"
        add.append(_derive_one(df, layer, did, sec,
                               "didpost_cred" if cred else "didpost_all", "did"))
        add.append(_derive_one(df, layer, per, sec,
                               "fupost_cred" if cred else "fupost_all", "fu"))
    add = [a for a in add if len(a)]
    return pd.concat([df] + add, ignore_index=True) if add else df


# Non-redundancy guard log (DEPENDENT_EFFECTS / Option C).  NOTE (audit F9): this is
# written under Scripts/ rather than Results/; it has multiple writers via this shared
# helper (metaanalysis, analysis_multiverse_meta, suite_common) and the path is also
# printed by analysis_multiverse_meta.py, so it was deliberately left here this pass.
NONREDUNDANCY_LOG = os.path.join(B, "Scripts", "NONREDUNDANCY_SUPPRESSED.csv")
COMPONENTS_SPEC = os.path.join(TMP, "meta_specs_components.csv")


def write_nonredundancy_log(rows, path=None):
    """Atomically write the suppressed-contrast audit CSV (one row per contrast)."""
    path = path or NONREDUNDANCY_LOG
    cols = ["study_key", "experiment_id", "dv_domain", "spec", "exclusions",
            "dv_name", "contrast_id", "family", "rule", "strand"]
    d = pd.DataFrame(rows)
    for c in cols:
        if c not in d.columns:
            d[c] = ""
    d = d[cols]
    tmp = path + ".tmp"
    d.to_csv(tmp, index=False, encoding="utf-8-sig")
    # Windows/Nextcloud can briefly hold a lock on the target (sync client, Excel,
    # antivirus); retry the atomic replace and never crash the pipeline on a lock.
    for _attempt in range(6):
        try:
            os.replace(tmp, path)
            return d
        except PermissionError:
            if _attempt == 5:
                print(f"  ! could not replace {os.path.basename(path)} (locked); "
                      f"left {os.path.basename(tmp)}")
                return d
            time.sleep(0.5 * (_attempt + 1))
    return d


def nonredundancy_guard(df, family_col="dv_family",
                        key_cols=("study_key", "experiment_id", "dv_domain"),
                        prefer=DV_FAMILY_DIFFERENCE):
    """Keep exactly one dependence family per (spec x exclusions x study x experiment x domain).

    When a difference/composite and its ``separate_*`` components are both
    ``eligible_primary == 1`` in the same cell, the composite is retained for the
    primary pool and every component row is moved to the returned *suppressed*
    frame (the sensitivity/specificity strand), so the two families are never
    mixed in one model.  A ``family_kept`` column records the retained family.

    Returns ``(kept, suppressed)``; both keep all original columns.  The guard is
    a no-op when ``dv_family`` is absent or the cell has only one family.
    """
    if df is None or not len(df) or family_col not in df.columns:
        out = df.copy() if df is not None else pd.DataFrame()
        if len(out) and "family_kept" not in out.columns:
            out["family_kept"] = ""
        return out, (df.iloc[0:0].copy() if df is not None else pd.DataFrame())
    keys = list(key_cols)
    for c in ("spec", "exclusions"):
        if c in df.columns and c not in keys:
            keys.append(c)
    d = df.copy()
    d["_keep"] = True
    d["family_kept"] = ""
    fam = d[family_col].fillna("").astype(str)
    for _key, g in d.groupby(keys, dropna=False, sort=False):
        fams = set(fam.loc[g.index])
        if (DV_FAMILY_DIFFERENCE in fams) and (DV_FAMILY_COMPONENT in fams):
            drop_idx = g.index[fam.loc[g.index] == DV_FAMILY_COMPONENT]
            d.loc[g.index, "family_kept"] = "primary_" + prefer
            d.loc[drop_idx, "_keep"] = False
        elif DV_FAMILY_DIFFERENCE in fams:
            d.loc[g.index, "family_kept"] = "primary_" + DV_FAMILY_DIFFERENCE
        elif DV_FAMILY_COMPONENT in fams:
            d.loc[g.index, "family_kept"] = "primary_" + DV_FAMILY_COMPONENT
        else:
            d.loc[g.index, "family_kept"] = "primary_" + DV_FAMILY_OTHER
    kept = d[d["_keep"]].drop(columns=["_keep"]).copy()
    supp = d[~d["_keep"]].drop(columns=["_keep"]).copy()
    return kept, supp


_PAIR_STRIP_RE = re.compile(
    r"non[-_ ]?manipulat|manipulat|non[-_ ]?misinformation|misinformation|"
    r"\btrue\b|\bfalse\b|\bfake\b|\breal\b|\bgenuine\b|\bfactual\b|"
    r"\bmainstream\b|\baccurate\b|\baccuracy\b|\bcorrect\b|\bcorrectly\b|"
    r"\bmanipulativeness\b|\bmanipulation\b", re.I)


def _component_pair_tokens(name):
    """Token set of a component DV name with the true/false descriptor removed.

    Used to pair a ``separate_true`` with its corresponding ``separate_false``
    (same video / stimulus / item set) so the derived difference composite is
    built from matching pairs rather than an arbitrary cross-product.
    """
    s = _PAIR_STRIP_RE.sub(" ", str(name or "").lower())
    return {w for w in re.findall(r"[a-z0-9]+", s) if len(w) > 2}


def _pair_components(t_rows, f_rows):
    """Greedy 1:1 pairing of true and false component rows by token similarity.

    Returns a list of ``(true_index, false_index)`` positions into the supplied
    lists.  Each true row is matched to its most similar unused false row
    (Jaccard of descriptor-stripped name tokens); when no positive similarity is
    available but exactly one false row remains, the leftovers are paired in
    order.  Unpaired rows stay in the secondary strand.
    """
    pairs = []
    used_f = set()
    for ti, tr in enumerate(t_rows):
        tt = tr["tokens"]
        best, bj = 0.0, None
        for fj, fr in enumerate(f_rows):
            if fj in used_f:
                continue
            ft = fr["tokens"]
            if not tt and not ft:
                score = 1.0
            else:
                union = tt | ft
                score = len(tt & ft) / len(union) if union else 0.0
            if score > best:
                best, bj = score, fj
        if (bj is None or best <= 0.0) and len(f_rows) == 1 and 0 not in used_f:
            bj, best = 0, 1.0
        if bj is not None:
            pairs.append((ti, bj))
            used_f.add(bj)
    return pairs


def build_primary_composite(df, effect_col="effect", var_col="var", src_col=None,
                            group_cols=("study_key", "experiment_id", "dv_domain"),
                            family_col="dv_family", side_col="component_side"):
    """Route a contrast frame into a PRIMARY composite pool and a SECONDARY components strand.

    PI-corrected family rule (DEPENDENT_EFFECTS / Option C, 2026-09-19):

    * **Primary question = the composite** (discernment difference / SDT d, higher =
      better).  When a cell has BOTH coded ``separate_true`` and ``separate_false``
      effects, the composite is **constructed from the components** as
      ``effect = effect_true_oriented + effect_false_oriented`` and
      ``var = var_true + var_false`` (conservative: the positive within-person
      covariance is not available, so the sum of variances is used).  A directly
      reported composite in the same cell is demoted to the secondary strand, so a
      composite is never pooled together with its components.
    * **Fallback**: when the separate pair is unavailable, a directly reported
      composite is used as the primary.
    * **Secondary question = the separate true and separate false scores**: every
      ``separate_*`` row (paired or not) is returned in the secondary frame, and is
      never mixed into a primary model.
    * ``other``-family DVs (generic accuracy, single-item outcomes, ...) always stay
      in the primary pool; they are not components of a veracity difference.

    Returns ``(primary, secondary, log, stats)``.  ``log`` is a list of
    suppressed/derouted contrast records; ``stats`` counts the derived composites.
    """
    if df is None or not len(df):
        return (df.copy() if df is not None else pd.DataFrame()), pd.DataFrame(), [], {}
    d = df.copy().reset_index(drop=True)
    for c in (family_col, side_col):
        if c not in d.columns:
            d[c] = "" if c == side_col else DV_FAMILY_OTHER
    if "contrast_id" not in d.columns:
        exp = d["experiment_id"] if "experiment_id" in d.columns else d.get(
            "Study_ID", pd.Series([""] * len(d)))
        con = d["contrast"] if "contrast" in d.columns else pd.Series(np.arange(len(d)))
        d["contrast_id"] = (d["study_key"].astype(str) + "|"
                            + pd.Series(exp).astype(str) + "|" + pd.Series(con).astype(str))
    keys = [c for c in group_cols if c in d.columns]
    for c in ("spec", "exclusions", "r_used", "timepoint"):
        if c in d.columns and c not in keys:
            keys.append(c)
    derived_rows, log = [], []
    primary_labels, secondary_labels = [], []
    stats = {"derived_composite_pairs": 0, "cells_with_derived": 0,
             "cells_with_pair": 0, "cells": 0}

    def rec(row, rule):
        log.append({"study_key": row.get("study_key"), "experiment_id": row.get("experiment_id"),
                    "dv_domain": row.get("dv_domain"), "spec": row.get("spec"),
                    "exclusions": row.get("exclusions"), "dv_name": row.get("dv_name"),
                    "contrast_id": row.get("contrast_id"), "family": row.get(family_col),
                    "rule": rule, "strand": "components_sensitivity"})

    grouped = list(d.groupby(keys, dropna=False, sort=False)) if keys else [((), d)]
    for _key, g in grouped:
        stats["cells"] += 1
        fam = g[family_col].fillna("").astype(str).to_numpy()
        side = g[side_col].fillna("").astype(str).to_numpy()
        is_t = (fam == DV_FAMILY_COMPONENT) & (side == "true")
        is_f = (fam == DV_FAMILY_COMPONENT) & (side == "false")
        is_d = fam == DV_FAMILY_DIFFERENCE
        is_o = ~(is_t | is_f | is_d)
        glab = g.index.to_numpy()
        t_lab, f_lab, d_lab = list(glab[is_t]), list(glab[is_f]), list(glab[is_d])
        name_ok = "dv_name" in g.columns
        t_tok = [_component_pair_tokens(g.loc[l, "dv_name"]) if name_ok else set() for l in t_lab]
        f_tok = [_component_pair_tokens(g.loc[l, "dv_name"]) if name_ok else set() for l in f_lab]
        derived_here = False
        if t_lab and f_lab:
            stats["cells_with_pair"] += 1
            used_f = set()
            for k, tl in enumerate(t_lab):
                tt = t_tok[k]
                best, bm = 0.0, None
                for m, _fl in enumerate(f_lab):
                    if m in used_f:
                        continue
                    ft = f_tok[m]
                    union = tt | ft
                    score = len(tt & ft) / len(union) if union else 1.0
                    if score > best:
                        best, bm = score, m
                if (bm is None or best <= 0.0) and len(f_lab) == 1 and 0 not in used_f:
                    bm = 0
                if bm is None:
                    continue
                fl = f_lab[bm]
                used_f.add(bm)
                yt = num(d.loc[tl, effect_col]) if effect_col in d.columns else np.nan
                yf = num(d.loc[fl, effect_col]) if effect_col in d.columns else np.nan
                vt = num(d.loc[tl, var_col]) if var_col in d.columns else np.nan
                vf = num(d.loc[fl, var_col]) if var_col in d.columns else np.nan
                if not (np.isfinite(yt) and np.isfinite(yf)
                        and np.isfinite(vt) and np.isfinite(vf) and vt > 0 and vf > 0):
                    continue
                base = d.loc[tl].copy()
                base[effect_col] = float(yt) + float(yf)
                base[var_col] = float(vt) + float(vf)
                base[family_col] = DV_FAMILY_DIFFERENCE
                base[side_col] = ""
                if "se" in d.columns:
                    base["se"] = math.sqrt(float(vt) + float(vf))
                if "dv_polarity" in d.columns:
                    base["dv_polarity"] = 1
                if "oriented" in d.columns:
                    base["oriented"] = 1
                if "veracity" in d.columns:
                    base["veracity"] = ""
                stats["derived_composite_pairs"] += 1
                base["contrast_id"] = (str(base.get("contrast_id", ""))
                                       + f"|D{stats['derived_composite_pairs']}")
                base["dv_name"] = ("derived composite (true+false): "
                                   + str(d.loc[tl, "dv_name"])[:80] + " + "
                                   + str(d.loc[fl, "dv_name"])[:80])
                base["derived_from_true"] = str(d.loc[tl, "contrast_id"])
                base["derived_from_false"] = str(d.loc[fl, "contrast_id"])
                if src_col and src_col in d.columns:
                    base[src_col] = "derived:components"
                if "source" in d.columns:
                    base["source"] = "derived:components"
                derived_rows.append(base)
                derived_here = True
        if derived_here:
            stats["cells_with_derived"] += 1
            primary_labels += list(glab[is_o])
            for l in t_lab + f_lab:
                secondary_labels.append(l)
                rec(d.loc[l], "components_moved_to_secondary")
            for l in d_lab:
                secondary_labels.append(l)
                rec(d.loc[l], "reported_composite_demoted_to_secondary")
        elif d_lab:
            primary_labels += list(glab[is_o]) + d_lab
            for l in t_lab + f_lab:
                secondary_labels.append(l)
                rec(d.loc[l], "components_moved_to_secondary")
        else:
            primary_labels += list(glab[is_o])
            for l in t_lab + f_lab:
                secondary_labels.append(l)
                rec(d.loc[l], "components_moved_to_secondary")
    prim = d.loc[primary_labels].copy()
    if derived_rows:
        prim = pd.concat([prim, pd.DataFrame(derived_rows)], ignore_index=True)
    sec = d.loc[sorted(set(secondary_labels))].copy()
    return prim, sec, log, stats


def build_meta_specs(r_values=(0.3, 0.5, 0.7)):
    """Build the meta-layer spec table and write ``temp/meta_specs.csv``.

    W2-parity2 (2026-09-22): the rows now come from the SAME contrast derivation
    as ``Results/metaanalysis.xlsx`` -- ``analysis_multiverse_meta``'s
    ``build_spec`` + ``apply_exclusions_off`` + canonical
    ``primary_composite_frames`` (the function ``analysis/metaanalysis.py`` also
    pools).  The spec-based pool (the app and ``publication_bias``) and the
    workbook are therefore bit-identical.  Previously this builder re-derived the
    meta contrasts in a separate implementation (``post_all`` k=3223, ``did_all``
    k=513 vs the workbook's 3088 / 355), which was the last pipeline split the
    parity test flagged.

    ``r_values`` is kept for signature compatibility.  The canonical table
    carries the r = 0.5 primary DiD; the r = 0.30 / 0.70 sensitivity is reported
    in the suite workbook's ``r_sensitivity`` sheet (``analysis_multiverse_meta``).

    The import is lazy because ``analysis_multiverse_meta`` imports this module.
    """
    import analysis_multiverse_meta as AM   # lazy import: avoid a module cycle
    return AM.write_meta_spec_table(out=META_SPECS)


# ---------------------------------------------------------------------------
# SDT layer spec builder
# ---------------------------------------------------------------------------

CTRL_RE = re.compile(
    r"control|no[_ -]?intervention|no[_ -]?treatment|placebo|wait[_ -]?list|"
    r"no[_ -]?growth|neutral|misinformation[_ -]?only|single[_ -]?survey|"
    r"unassigned|active[_ -]?control|comparison|deliberation[_ -]?control|"
    r"control[_ -]?deliberation|control[_ -]?other|control[_ -]?t0|"
    # Conservative extension (2026-09-21): unambiguous no-intervention labels the
    # original pattern missed (e.g. Nyhan `no_correction`, Brugman `no_debunk`,
    # Collier `no_quiz`, Ecker2020c `NR`, Adamus `kontrol`, Pillai `ctrl`).
    # NOTE: `baseline` is deliberately NOT included -- it is a treatment factor level
    # in Lande-Brenner et al. (2026) (`thinkfre_baseline`) and would contaminate the
    # control arm; ambiguous cases belong in a study's `<Key> - conditions.csv`.
    r"no[_ -]?correction|no[_ -]?debunk|no[_ -]?quiz|no[_ -]?retraction|"
    r"(?<![a-z])ctrl(?![a-z])|(?<![a-z])kontrol(?![a-z])|(?<![a-z])nr(?![a-z])|"
    r"non[_ -]?misleading|(?<![a-z])sham(?![a-z])|untreated", re.I)

NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?$")

# Explicit per-study arm mappings: Datasets/<Key>/<Key> - conditions.csv
# (columns: code, role, label, evidence, artifact; role in
# control|active_control|intervention|other, artifact '*' = all artifacts).
# When one is present it is authoritative and the numeric convention below is
# NEVER used; a code absent from the mapping is reported as unknown/ambiguous
# rather than guessed.
CONDITION_MAP_CACHE = {}


def _norm_condition_code(x):
    """Normalise a condition code for mapping lookup.

    Numeric codes collapse to their canonical integer/float form ('1', '1.0', 1 ->
    '1'); string codes collapse to a lower-case, underscore-separated form so that
    'Control', 'control ' and 'no intervention' match their map/data counterparts
    (case- and separator-insensitive).
    """
    if x is None:
        return ""
    if isinstance(x, float) and math.isnan(x):
        return ""
    s = str(x).strip()
    if s == "":
        return ""
    try:
        f = float(s)
    except (TypeError, ValueError):
        f = None
    if f is not None:
        return str(int(f)) if f == int(f) else str(f)
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def load_condition_map(folder, key):
    """Load ``<Key> - conditions.csv`` -> {(artifact, code): role}, cached.

    Returns an empty dict when the file is absent, so callers fall back to the
    legacy heuristic (still flagged).  ``artifact == '*'`` is a wildcard row.
    """
    cache_key = (os.path.abspath(folder), key)
    if cache_key in CONDITION_MAP_CACHE:
        return CONDITION_MAP_CACHE[cache_key]
    out = {}
    p = os.path.join(folder, key + " - conditions.csv")
    if os.path.isfile(p):
        try:
            d = pd.read_csv(p, encoding="utf-8-sig", low_memory=False)
        except Exception:
            d = None
        if d is not None and {"code", "role"}.issubset(set(str(c) for c in d.columns)):
            for _, r in d.iterrows():
                code = _norm_condition_code(r.get("code"))
                role = str(r.get("role", "")).strip().lower()
                art = str(r.get("artifact", "*")).strip() if "artifact" in d.columns else "*"
                if art.lower() in ("", "nan", "none"):
                    art = "*"
                if code and role and role != "nan":
                    out[(art, code)] = role
    CONDITION_MAP_CACHE[cache_key] = out
    return out


def classify_condition(cond, study_key=None, artifact="*", folder=None):
    """Return (role, ambiguous).  role in {control, active_control, intervention,
    other, missing, unknown}.

    Preferred: the study's explicit ``Datasets/<Key>/<Key> - conditions.csv``
    mapping (``load_condition_map``).  When a mapping exists it is authoritative
    and ``ambiguous = 0`` for every mapped code; a code that is not in the
    mapping is ``unknown``/``1`` rather than guessed.  A blank/absent condition is
    a **missing value, not an ambiguous code**, so it is reported
    ``missing``/``0``.  Without a mapping the legacy numeric convention
    (0=control, non-zero=intervention) is used and flagged ``ambiguous = 1``.
    """
    s = _norm_condition_code(cond)
    missing = s == "" or s.lower() in ("nan", "none")
    if study_key:
        folder = folder or os.path.join(DS, study_key)
        cmap = load_condition_map(folder, study_key)
        if cmap:
            for art in (str(artifact), "*", ""):
                if (art, s) in cmap:
                    return cmap[(art, s)], 0
            # a mapping exists; a code it does not cover is not guessed
            return ("missing", 0) if missing else ("unknown", 1)
    if missing:
        # a blank/absent condition is a missing value, not an ambiguous code
        return "missing", 0
    s2 = str(cond).strip()
    if s2 == "" or s2.lower() in ("nan", "none"):
        return "unknown", 1
    if CTRL_RE.search(s2):
        return "control", 0
    if NUMERIC_RE.match(s2):
        # 0 = control, non-zero = intervention (documented convention)
        return ("control" if float(s2) == 0 else "intervention"), 1
    return "intervention", 0


# Roles pooled together as the comparison arm in the arm-based SDT contrasts.
COND_CONTROL_ROLES = ("control", "active_control")


def _load_excl_rules(folder, key):
    """Same rules as reanalysis.py: list of (flag, keep, applies_to_list)."""
    p = os.path.join(folder, key + " - exclusions.csv")
    if not os.path.isfile(p):
        return []
    try:
        d = pd.read_csv(p, encoding="utf-8-sig")
    except Exception:
        return []
    rules = []
    for _, r in d.iterrows():
        flag = str(r.get("flag", "")).strip()
        keep = str(r.get("keep", "")).strip()
        app = str(r.get("applies_to", "*")).strip() or "*"
        if flag and keep and flag.lower() != "nan":
            rules.append({"flag": flag, "keep": keep,
                          "applies_to": [x.strip() for x in app.split(";") if x.strip()]})
    return rules


def _keep_mask(s, expr):
    s = pd.to_numeric(s, errors="coerce")
    m = re.match(r"^(==|!=|>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)$", expr)
    if m:
        op, v = m.group(1), float(m.group(2))
        mask = {"==": s == v, "!=": s != v, ">=": s >= v, "<=": s <= v,
                ">": s > v, "<": s < v}[op]
        # Exclude only participants whose flag is DEFINED and fails the condition.
        # A missing flag means the check was not administered (e.g. another
        # artifact in the same study), so the row is retained; otherwise a rule
        # defined for one artifact silently wipes out the others.
        return mask | s.isna()
    if expr.lower() == "notna":
        return s.notna()
    return pd.Series(True, index=s.index)


def _apply_dv_exclusions(values, dvs, domains, flag_mask):
    """NaN-out excluded (participant, DV) cells for rules applying to a DV.

    ``values`` is a participants x DVs array; ``flag_mask`` a boolean array
    participants x flags.  A cell is dropped when the rule applies to its DV
    (glob / '*' / domain:) and the participant fails the flag condition.
    """
    for rule in flag_mask:
        app_dv = rule["applies_dv"]
        for j, dv in enumerate(dvs):
            if not app_dv(dv, domains[j]):
                continue
            bad = ~rule["keep_mask"]
            values[bad, j] = np.nan
    return values


#: `source` marker of the reanalysis-consistent SDT arm contrast.  The app's
#: ``analysis_lib._sdt_spec_path`` and :func:`sdt_spec_path` prefer a spec table
#: carrying it over the multiverse suite's standardised linear twin.
SDT_ARM_CONTRAST_SOURCE = "sdt_participant_delta_arm_contrast"
SDT_LEVEL_SOURCE = "sdt_participant_mean"

_SDT_COMP_COLS = ("true_score", "true_var", "fake_score", "fake_var",
                  "criterion_score", "criterion_var")


def _analysis_module(name):
    """Import a module from ``Scripts/analysis`` (lazy; avoids the import cycle)."""
    adir = os.path.join(B, "Scripts", "analysis")
    if adir not in sys.path:
        sys.path.insert(0, adir)
    import importlib
    return importlib.import_module(name)


def _reanalysis_sdt_frames(no_excl):
    """SDT cell frames exactly as ``reanalysis.collect_datasets`` builds them.

    W2-parity: the SDT spec rows are generated FROM reanalysis' own cell
    computation (import sharing) rather than re-derived, so
    ``SC.pool_dependence(spec.effect, spec.var, spec.cluster_id, spec.study_key)``
    reproduces the pooled ``Discrimination`` sheet / ``MetaResults`` rows
    bit-for-bit.  The reanalysis module globals are pinned for the duration of the
    call and restored afterwards, so calling this from the pipeline never changes
    reanalysis' own run.

    Returns ``(disc, did, level, res)`` where ``disc`` is the per
    (study x artifact x domain x timepoint) intervention-vs-control Δd′ frame with
    a ``bucket`` column, ``did`` is ``reanalysis.build_did(disc)`` and ``level`` is
    the arm-pooled discrimination level (auxiliary).
    """
    RA = _analysis_module("reanalysis")
    saved = (RA._FORCE, RA._LOG_ACTIVE, RA._NO_REVERSAL)
    RA._FORCE = True            # no concurrent-writer freshness skips
    RA._LOG_ACTIVE = False      # never duplicate the durable drop log
    RA._NO_REVERSAL = False     # apply the veracity polarity reversal
    try:
        res = RA.collect_datasets(bool(no_excl), write_catalog=False)
        disc = RA._make_disc(res["disc_rows"])
        did = RA.build_did(disc)
        level = RA._make_disc(res["level_rows"])
    finally:
        RA._FORCE, RA._LOG_ACTIVE, RA._NO_REVERSAL = saved
    return disc, did, level, res


def _norm_tp(x):
    """Normalise a raw timepoint for the component-merge key (blank stays blank)."""
    s = "" if x is None else str(x).strip()
    if s.lower() in ("nan", "none", "<na>"):
        return ""
    if s == "(blank=post)":
        return ""
    return s


def _is_arm_contrast_file(path):
    """True when ``path`` is a usable spec table carrying the arm-contrast marker."""
    if not _specs_usable(path):
        return False
    try:
        hdr = pd.read_csv(path, encoding="utf-8-sig", nrows=0)
        cols = {str(c).strip() for c in hdr.columns}
        if "source" not in cols:
            return False
        src = pd.read_csv(path, encoding="utf-8-sig", usecols=["source"], low_memory=False)
        vals = set(src["source"].astype(str).str.strip())
    except Exception:
        return False
    return SDT_ARM_CONTRAST_SOURCE in vals


def sdt_spec_path():
    """The SDT spec file the app and the pipeline must pool.

    Preference order:

    1. the reanalysis-consistent arm contrast (``temp/sdt_specs_pb.csv``, written
       by :func:`build_sdt_specs` with ``source == SDT_ARM_CONTRAST_SOURCE``);
    2. a table carrying the SDT decomposition columns;
    3. the canonical ``temp/sdt_specs.csv``.

    This mirrors ``analysis_lib._sdt_spec_path`` exactly so the app, the
    publication-bias tables and ``reanalysis.xlsx`` all pool one estimand.
    """
    canon, fallback = SDT_SPECS, SDT_SPECS_FALLBACK
    if _is_arm_contrast_file(fallback) and not _is_arm_contrast_file(canon):
        return fallback
    if not _specs_usable(canon) and _specs_usable(fallback):
        return fallback
    if (not _specs_has_cols(canon, SDT_COMPONENT_COLS)
            and _specs_has_cols(fallback, SDT_COMPONENT_COLS)):
        return fallback
    return canon


def _sdt_component_lookup(path=None):
    """Component columns keyed by cell, taken from the canonical SDT table.

    The arm-contrast pool does not itself carry the hit/false-alarm/criterion
    decomposition; the canonical table written by ``analysis_multiverse_sdt`` does.
    The components are auxiliary (they never enter the pooled effect), so a
    missing/stale canonical table simply yields blank components.
    """
    p = path or SDT_SPECS
    if not os.path.isfile(p):
        return {}
    try:
        d = pd.read_csv(p, encoding="utf-8-sig", low_memory=False)
    except Exception:
        return {}
    if not {"true_score", "fake_score", "criterion_score"}.issubset(set(d.columns)):
        return {}
    out = {}
    for _, r in d.iterrows():
        key = (str(r.get("study_key")), str(r.get("experiment_id")),
               str(r.get("dv_domain")), _norm_tp(r.get("timepoint")),
               str(r.get("exclusions") or "").strip().lower())
        if key in out:
            continue
        out[key] = {c: r.get(c) for c in _SDT_COMP_COLS}
    return out


def _datasets_with_condition():
    """Study keys whose harmonized table carries an arm column (``condition``/``condition_raw``).

    reanalysis assigns ``_role = "missing"`` to a dataset with neither column and
    still emits an arm-pooled discrimination *level* for it.  The historical spec
    builder required an arm column before processing a dataset, so the auxiliary
    ``discrimination_level_*`` rows follow the same gate here (the arm-contrast
    rows already require identifiable arms).  This keeps the fully-within opt-in
    row count identical to the default build (a condition-less fully-within study
    contributes no spec row either way).
    """
    out = set()
    for folder in sorted(glob.glob(os.path.join(DS, "*"))):
        key = os.path.basename(folder)
        f = os.path.join(folder, key + " - harmonized.csv")
        if not os.path.isfile(f):
            continue
        try:
            hdr = pd.read_csv(f, encoding="utf-8-sig", nrows=0)
        except Exception:
            continue
        cols = {str(c).strip().lstrip("\ufeff") for c in hdr.columns}
        if cols & {"condition", "condition_raw"}:
            out.add(key)
    return out


def _sdt_condition_audit():
    """Distinct (artifact, condition) arm classification per dataset (light pass)."""
    rows = []
    for folder in sorted(glob.glob(os.path.join(DS, "*"))):
        key = os.path.basename(folder)
        if SE.is_fulltext_excluded(key):
            continue
        f = os.path.join(folder, key + " - harmonized.csv")
        if not os.path.isfile(f):
            continue
        try:
            header = pd.read_csv(f, encoding="utf-8-sig", nrows=0)
        except Exception:
            continue
        cols = [str(c).strip().lstrip("\ufeff") for c in header.columns]
        if "condition" not in cols:
            continue
        use = ["condition"] + (["artifact"] if "artifact" in cols else [])
        try:
            d = pd.read_csv(f, encoding="utf-8-sig", low_memory=False, usecols=use)
        except Exception:
            continue
        art = (d["artifact"].astype(str).to_numpy() if "artifact" in d.columns
               else np.array([""] * len(d)))
        cond = d["condition"].map(lambda x: "" if pd.isna(x) else str(x).strip())
        seen = set()
        for c, a in zip(cond, art):
            if (c, a) in seen:
                continue
            seen.add((c, a))
            role, amb = classify_condition(c, key, a, folder=folder)
            rows.append({"study_key": key, "artifact": a, "condition": c,
                         "role": role, "ambiguous": amb,
                         "n": int(((cond == c) & (art == a)).sum())})
    return rows


def _sdt_effect_row(r, spec, excl, comps, *, metric, source, note,
                    did_basis="", is_did=False, is_level=False):
    """One SDT spec row from a reanalysis cell (``d``/``vi``/``cluster_id``)."""
    sk = str(r.get("study_key"))
    art = str(r.get("artifact"))
    dom = str(r.get("dv_domain") or "")
    tp = _norm_tp(r.get("timepoint"))
    eff = num(r.get("d"))
    vi = num(r.get("vi"))
    se = math.sqrt(vi) if (np.isfinite(vi) and vi > 0) else np.nan
    pv = (float(2 * _st.norm.sf(abs(eff) / se))
          if (_st is not None and np.isfinite(eff) and np.isfinite(se) and se > 0)
          else np.nan)
    if is_level:
        n1 = n2 = np.nan
        n_total = num(r.get("n_true")) + num(r.get("n_false"))
    elif is_did:
        n1 = n2 = n_total = np.nan
    else:
        n1, n2 = num(r.get("n_treat")), num(r.get("n_control"))
        n_total = (n1 + n2) if (np.isfinite(n1) and np.isfinite(n2)) else np.nan
    comp = comps.get((sk, art, dom, tp, excl), {})
    row = {
        "layer": "sdt", "spec": spec, "exclusions": excl, "study_key": sk,
        "experiment_id": art,
        "contrast_id": f"{sk}|{art}|{dom}|{tp}" + ("|level" if is_level else ""),
        "dv_name": (f"discrimination_level[{dom}]" if is_level
                    else f"discrimination_change[{dom}]" if is_did
                    else f"discrimination[{dom}]"),
        "dv_domain": dom, "veracity": "true_vs_false",
        "dv_family": DV_FAMILY_DIFFERENCE, "component_side": "",
        "cluster_id": str(r.get("cluster_id") or sdt_cluster_id(sk, art)),
        "timepoint": tp, "metric": metric,
        "effect": eff, "effect_raw": eff, "var": vi, "se": se,
        "n1": n1, "n2": n2, "n_total": n_total, "r_used": np.nan,
        "p_value": pv, "p_censored": 0, "data_available": 1,
        "contrast_type": "", "source": source, "fallback": "",
        "did_basis": did_basis, "exclusions_embedded": 0, "cond_ambiguous": 0,
        "oriented": 1, "note": note, "effect_type_reported": "",
    }
    # The components (true score / fake score / response bias c) are the
    # arm contrast computed on the reanalysis path and carried on the cell row; the
    # historical merge (``comps``) is kept only as a fallback for cells that do not
    # carry them (e.g. an auxiliary level row, which has no arm contrast).
    for c in _SDT_COMP_COLS:
        if is_level:
            row[c] = np.nan
            continue
        rv = num(r.get(c)) if hasattr(r, "get") else np.nan
        row[c] = rv if np.isfinite(rv) else (num(comp.get(c)) if comp else np.nan)
    return row


def _sdt_spec_rows(disc, did, level, excl, comps):
    """All SDT spec rows (post / persistence / did / auxiliary level) for one switch."""
    rows = []
    for _, r in disc.iterrows():
        bucket = str(r.get("bucket") or "")
        if bucket == "post":
            rows.append(_sdt_effect_row(
                r, "post_all", excl, comps, metric="d_sdt",
                source=SDT_ARM_CONTRAST_SOURCE,
                note="intervention-vs-control delta d'"))
        elif bucket == "followup":
            rows.append(_sdt_effect_row(
                r, "persistence_all", excl, comps, metric="d_sdt",
                source=SDT_ARM_CONTRAST_SOURCE,
                note="intervention-vs-control delta d' (follow-up)"))
    for _, r in did.iterrows():
        rows.append(_sdt_effect_row(
            r, "did_all", excl, comps, metric="d_sdt_did",
            source=SDT_ARM_CONTRAST_SOURCE, is_did=True,
            did_basis="did_IG_minus_CG_delta_d",
            note="pretest-corrected intervention-vs-control delta d'"))
    for _, r in level.iterrows():
        bucket = str(r.get("bucket") or "")
        if bucket == "post":
            rows.append(_sdt_effect_row(
                r, "discrimination_level_all", excl, comps, metric="d_sdt_level",
                source=SDT_LEVEL_SOURCE, is_level=True,
                note="AUXILIARY arm-pooled discrimination level (not the effect)"))
        elif bucket == "followup":
            rows.append(_sdt_effect_row(
                r, "discrimination_level_persistence_all", excl, comps,
                metric="d_sdt_level", source=SDT_LEVEL_SOURCE, is_level=True,
                note="AUXILIARY arm-pooled follow-up level (not the effect)"))
    return rows


def build_sdt_specs(datasets_limit=None, out=None):
    """Build the SDT-layer specifications -> ``temp/sdt_specs.csv`` (+ PB table).

    W2-parity rebuild (2026-09-22).  The rows are no longer re-derived from the
    participant-level melt; they are generated **from ``reanalysis.collect_datasets``
    itself**, the one code path that produces ``Results/reanalysis.xlsx``:

    * ``post_all`` / ``persistence_all`` = the intervention-vs-control Δd′ cell
      (``d'_treatment - d'_control``) at the immediate posttest / follow-up;
    * ``did_all`` = ``reanalysis.build_did(disc)`` (pretest-corrected change in the
      arm contrast, independent pre/post samples);
    * ``discrimination_level_*`` = the auxiliary arm-pooled level.

    Exclusions ON/OFF are re-collected per switch exactly as reanalysis does, and
    the fully-within switch is honoured by reanalysis' own guard.  Because the cells
    are byte-identical to the ``Discrimination`` sheet, ``pool_dependence`` over a
    spec pool reproduces the ``reanalysis.xlsx`` headline (and the app) exactly.

    The true/fake/criterion decomposition is merged in from the canonical table
    written by ``analysis_multiverse_sdt`` (auxiliary; never the pooled effect).
    """
    comps = _sdt_component_lookup()
    audit = _sdt_condition_audit()
    rows = []
    # The skipped-dataset list is the study-level fully-within inventory (every
    # Datasets/<Key> folder whose study is all-fully-within), whether or not it has
    # a harmonized file -- this is what ``validate_fully_within`` pins (default
    # build skips all fully-within studies; opt-in skips none).  reanalysis only
    # reports the dataset-backed ones, so it cannot drive this list.
    fw_skipped = []
    if not include_fully_within():
        for _folder in sorted(glob.glob(os.path.join(DS, "*"))):
            _key = os.path.basename(_folder)
            if SE.is_fulltext_excluded(_key):
                continue
            if study_fully_within(_key):
                fw_skipped.append(_key)
    no_control_cells = 0
    no_control_studies = set()
    cond_studies = _datasets_with_condition()
    for excl_on in (True, False):
        excl = "on" if excl_on else "off"
        disc, did, level, res = _reanalysis_sdt_frames(no_excl=not excl_on)
        if len(level) and "study_key" in level.columns:
            # auxiliary levels follow the historical arm-column gate (see
            # ``_datasets_with_condition``)
            level = level[level["study_key"].astype(str).isin(cond_studies)]
        rows += _sdt_spec_rows(disc, did, level, excl, comps)
        no_control_cells += int(res.get("no_control_cells") or 0)
        no_control_studies.update(res.get("no_control_studies") or [])

    df = pd.DataFrame(rows)
    if len(df):
        for src, dst in (("post_all", "post_cred"),
                         ("did_all", "did_cred"),
                         ("persistence_all", "persistence_cred"),
                         ("discrimination_level_all", "discrimination_level_cred"),
                         ("discrimination_level_persistence_all",
                          "discrimination_level_persistence_cred")):
            sub = df[df["spec"] == src].copy()
            if not len(sub):
                continue
            sub["fallback"] = ""
            cred = _cred_fallback(sub, src)
            cred["spec"] = dst
            df = pd.concat([df, cred], ignore_index=True)
        for c in SPEC_COLS:
            if c not in df.columns:
                df[c] = ""
        df = df[SPEC_COLS]
    df = derive_specs(df, "sdt")
    target = out or SDT_SPECS
    df.to_csv(target, index=False, encoding="utf-8-sig")
    write_fully_within_marker(target)
    pd.DataFrame(audit).to_csv(os.path.join(TMP, "sdt_condition_audit.csv"),
                               index=False, encoding="utf-8-sig")
    pd.DataFrame({"study_key": fw_skipped}).to_csv(
        os.path.join(TMP, "sdt_fully_within_skipped.csv"), index=False, encoding="utf-8-sig")
    pd.DataFrame([{"no_control_cells": no_control_cells,
                   "no_control_studies": len(no_control_studies),
                   "n_spec_rows": int(len(df))}]).to_csv(
        os.path.join(TMP, "sdt_build_audit.csv"), index=False, encoding="utf-8-sig")
    print(fully_within_report(len(fw_skipped), "dataset(s)"))
    return df


def _normalize_specs(df, layer_hint):
    """Coerce a spec table to the canonical ``SPEC_COLS`` regardless of which
    session built it.

    The meta-layer session writes the same prerequisite file with a slightly
    different vocabulary (``yi``/``vi`` for the effect and variance,
    ``ON``/``OFF`` for exclusions); those columns are mapped here so
    ``publication_bias.py`` and ``figures.py`` accept either schema.
    """
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    ren = {}
    if "effect" not in df.columns and "yi" in df.columns:
        ren["yi"] = "effect"
    if "var" not in df.columns and "vi" in df.columns:
        ren["vi"] = "var"
    if "study_key" not in df.columns and "study" in df.columns:
        ren["study"] = "study_key"
    if "se" not in df.columns and "sei" in df.columns:
        ren["sei"] = "se"
    if "r_used" not in df.columns and "did_r_used" in df.columns:
        ren["did_r_used"] = "r_used"
    if "experiment_id" not in df.columns and "Study_ID" in df.columns:
        ren["Study_ID"] = "experiment_id"
    if "timepoint" not in df.columns and "timepoint_1" in df.columns:
        ren["timepoint_1"] = "timepoint"
    if "dv_domain" not in df.columns and "domain" in df.columns:
        ren["domain"] = "dv_domain"
    df = df.rename(columns=ren)
    if "layer" not in df.columns:
        df["layer"] = layer_hint
    for c in ("spec", "exclusions", "study_key", "dv_domain", "metric", "fallback",
              "did_basis", "note", "source", "timepoint", "veracity", "contrast_type"):
        if c not in df.columns:
            df[c] = ""
    if "exclusions" in df.columns:
        df["exclusions"] = (df["exclusions"].astype(str).str.strip().str.lower()
                            .replace({"nan": "on", "": "on"}))
    if "effect" not in df.columns:
        df["effect"] = np.nan
    if "var" not in df.columns:
        df["var"] = np.nan
    if "se" not in df.columns:
        df["se"] = np.nan
    # reconcile variance and SE: prefer the variance, fall back to se^2
    _v = pd.to_numeric(df["var"], errors="coerce")
    _s = pd.to_numeric(df["se"], errors="coerce")
    _s = _s.where(_s.notna(), np.sqrt(_v.where(_v >= 0)))
    _v = _v.where(_v.notna(), _s ** 2)
    df["var"], df["se"] = _v, _s
    if "contrast_id" not in df.columns:
        df["contrast_id"] = (df["study_key"].astype(str) + "|"
                             + np.arange(len(df)).astype(str))
    if "p_value" not in df.columns:
        df["p_value"] = np.nan
    if "p_censored" not in df.columns:
        df["p_censored"] = np.nan
    if "data_available" not in df.columns:
        df["data_available"] = 1
    if "r_used" not in df.columns:
        df["r_used"] = np.nan
    if "oriented" not in df.columns:
        df["oriented"] = 1
    if "exclusions_embedded" not in df.columns:
        df["exclusions_embedded"] = 1
    if "cond_ambiguous" not in df.columns:
        df["cond_ambiguous"] = 0
    if "experiment_id" not in df.columns:
        df["experiment_id"] = ""
    if "dv_name" not in df.columns:
        df["dv_name"] = ""
    # Dependence structure defaults for spec tables built by another session.
    if "dv_family" not in df.columns:
        df["dv_family"] = DV_FAMILY_OTHER
    if "component_side" not in df.columns:
        df["component_side"] = ""
    if "effect_type_reported" not in df.columns:
        df["effect_type_reported"] = ""
    if "cluster_id" not in df.columns:
        df["cluster_id"] = (df["study_key"].astype(str) + "|"
                            + df["experiment_id"].astype(str) + "|unknown|"
                            + df["dv_family"].astype(str))
    if "metric" in df.columns:
        df["metric"] = df["metric"].fillna("").astype(str)
        df.loc[df["metric"].str.strip() == "", "metric"] = "reported"
    return df


def load_specs(layer=None):
    frames = []
    if layer in (None, "meta"):
        d = pd.read_csv(META_SPECS, encoding="utf-8-sig", low_memory=False)
        frames.append(_normalize_specs(d, "meta"))
    if layer in (None, "sdt"):
        # The SDT pool MUST be the reanalysis-consistent arm contrast (see
        # ``sdt_spec_path``), so the app, the publication-bias tables and
        # ``reanalysis.xlsx`` all report one estimand (W2-parity).
        path = sdt_spec_path()
        d = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
        frames.append(_normalize_specs(d, "sdt"))
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=SPEC_COLS)
    for c in ("effect", "var", "se", "p_value", "n1", "n2", "n_total", "r_used"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    # p-values are optional in the prerequisite tables; fill any missing ones
    # with two-sided normal p-values derived from effect/SE (used only for the
    # optional p-curve / p-uniform summaries).
    if {"effect", "se", "p_value"}.issubset(df.columns):
        with np.errstate(invalid="ignore", divide="ignore"):
            derived = 2 * _st.norm.sf((df["effect"].abs()
                                       / df["se"].replace(0, np.nan)).to_numpy(float))
            df["p_value"] = pd.to_numeric(df["p_value"], errors="coerce").where(
                pd.to_numeric(df["p_value"], errors="coerce").notna(), derived)
    for c in ("fallback", "did_basis", "note", "source", "metric"):
        if c in df.columns:
            df[c] = df[c].fillna("").astype(str)
    return df


def _specs_usable(path):
    """True when a spec file exists and can drive the publication-bias battery.

    Requires an effect and a sampling variance (a variance column, or an SE that
    can be squared)."""
    if not os.path.isfile(path) or os.path.getsize(path) < 100:
        return False
    try:
        hdr = pd.read_csv(path, encoding="utf-8-sig", nrows=0)
        cols = {str(c).strip() for c in hdr.columns}
    except Exception:
        return False
    has_effect = "effect" in cols or "yi" in cols
    has_var = "var" in cols or "vi" in cols or "se" in cols or "sei" in cols
    return has_effect and has_var and "spec" in cols


def _specs_has_cols(path, cols):
    """True when ``path`` exists and its header contains every column in ``cols``."""
    if not os.path.isfile(path) or os.path.getsize(path) < 100:
        return False
    try:
        hdr = pd.read_csv(path, encoding="utf-8-sig", nrows=0)
    except Exception:
        return False
    have = {str(c).strip() for c in hdr.columns}
    return all(c in have for c in cols)


# The SDT-layer decomposition columns (true score / fake score / response bias).
SDT_COMPONENT_COLS = ("true_score", "fake_score", "criterion_score")


def _newest_spec_input_mtime(layer="meta"):
    """Newest mtime among the inputs that drive a layer's spec table.

    The meta layer is derived from ``Extraction/*.json`` only; the SDT layer is
    derived from the harmonized datasets.  Keeping the two separate means running
    the harmonizers (which rewrites ``Datasets/*``) does not mark the meta spec
    table stale, so ``publication_bias.py`` never silently rebuilds the meta pool
    with a different derivation.
    """
    newest = 0.0
    pats = ([os.path.join(EXTRACT, "*.json")] if layer == "meta"
            else [os.path.join(DS, "*", "* - harmonized.csv")])
    for pat in pats:
        for f in glob.glob(pat):
            try:
                newest = max(newest, os.path.getmtime(f))
            except OSError:
                pass
    return newest


def _specs_stale(path, layer="meta"):
    """True when the spec file predates the newest input for its layer.

    The spec tables are a cache of the coded extractions / harmonized data;
    without this check a table built before a recoding keeps driving the analysis
    suite with the old timepoints/effects (e.g. an immediate posttest still filed
    as a follow-up).

    W2-parity2: a pre-unification meta table (no canonical ``r_used`` column,
    built by the retired standalone derivation) is always stale, so the app's
    freshness badge and ``ensure_specs`` both migrate it to the workbook-consistent
    table.
    """
    try:
        if layer == "meta" and not _specs_has_cols(path, ("r_used",)):
            return True
        return os.path.getmtime(path) < _newest_spec_input_mtime(layer)
    except OSError:
        return True


def ensure_specs(force=False):
    """Build the prerequisite spec tables if absent, unusable, or stale.

    The meta-/SDT-layer sessions write the same prerequisite files with their own
    vocabularies.  A file that carries an effect *and* a variance is accepted as
    is (``load_specs`` normalises it).  When the shared SDT file cannot drive the
    battery, our own SDT spec table is written to ``temp/sdt_specs_pb.csv`` and
    used instead, so the shared file is never overwritten.
    """
    built = []
    meta_stale = _specs_stale(META_SPECS, "meta") if os.path.exists(META_SPECS) else False
    # The fully-within include/exclude switch is recorded beside the table; a
    # mismatch means the table was built under the other switch and must be rebuilt.
    fw_meta_mismatch = (os.path.exists(META_SPECS)
                        and not fully_within_marker_matches(META_SPECS))
    # W2-parity2 schema migration: pre-unification meta tables carry ``did_r_used``
    # but no canonical ``r_used`` column and were derived by a separate code path.
    # Rebuild such a file in place so the spec-based pool and metaanalysis.xlsx
    # agree (the builder is now the workbook's own derivation).
    meta_schema_old = (os.path.exists(META_SPECS)
                       and not _specs_has_cols(META_SPECS, ("r_used",)))
    if force or not _specs_usable(META_SPECS) or meta_stale or fw_meta_mismatch or meta_schema_old:
        why = ("forced" if force else "pre-unification schema" if meta_schema_old
               else "stale" if meta_stale
               else "fully-within switch changed" if fw_meta_mismatch else "missing")
        print(f"building {META_SPECS} ({why})")
        build_meta_specs()
        built.append("meta")
    # --- SDT: the app + publication bias pool the reanalysis-consistent arm
    # contrast (temp/sdt_specs_pb.csv), NOT the multiverse suite's standardised
    # linear twin (temp/sdt_specs.csv).  Always keep the PB table current (W2-parity).
    pb_present = os.path.exists(SDT_SPECS_FALLBACK)
    fw_pb_mismatch = pb_present and not fully_within_marker_matches(SDT_SPECS_FALLBACK)
    pb_stale = (not _is_arm_contrast_file(SDT_SPECS_FALLBACK)
                or fw_pb_mismatch
                or (pb_present and _specs_stale(SDT_SPECS_FALLBACK, "sdt")))
    if force or pb_stale:
        why = ("forced" if force else "stale" if pb_present else "missing")
        print(f"building {SDT_SPECS_FALLBACK} ({why}; reanalysis-consistent SDT arm contrast)")
        build_sdt_specs(out=SDT_SPECS_FALLBACK)
        built.append("sdt_pb")
    # The canonical file is kept for consumers that read it directly (it is
    # normally written by analysis_multiverse_sdt); build it only when unusable.
    if force or not _specs_usable(SDT_SPECS):
        print("building", SDT_SPECS)
        build_sdt_specs()
        built.append("sdt")
    return built


if __name__ == "__main__":
    if "--timepoints" in sys.argv:
        # direct classifier unit check; does not build the spec tables
        audit_timepoint_classification()
        sys.exit(0)
    force = "--force" in sys.argv
    built = ensure_specs(force=force)
    for layer in ("meta", "sdt"):
        d = load_specs(layer)
        print(f"\n== {layer}: {len(d)} rows ==")
        if len(d):
            print(d.groupby(["spec", "exclusions"]).size().to_string())
            if "fallback" in d.columns:
                print("fallbacks:", (d["fallback"] == "sharing_intention").sum())
