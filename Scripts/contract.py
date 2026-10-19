"""Canonical, **stdlib-only** coding contracts shared by the pipeline, the
analysis suite and the CodingStudio app (frozen exe bundles this module).

This module is the single home of the rules that were previously mirrored in
``suite_common``, ``studio_lib``, ``analysis_lib``, ``agreement_lib`` and
``B_validate``:

* the fully-within (single-arm, no-control) switch and marker rule;
* the DV-completeness rule (``dv_has_effect``) and the contradiction rule;
* stimulus-veracity / dependence-family / component-side decoders;
* the metacognition (confidence) predicate;
* the canonical timepoint classifier (``classify_timepoint``);
* the polarity sign;
* the per-intervention record resolution (``record_interventions`` /
  ``effective_record``);
* the domain vocabulary (imported from ``domain_restriction``) and the guard
  constants (``DID_ABS_MAX``, ``MIN_K_POOL``).

No numpy / pandas here: the packaged ``CodingStudio.exe`` imports this module
directly.  ``suite_common`` re-exports these names (with dict-taking adapters)
so no existing caller changes.
"""

from __future__ import annotations

import os
import re
import sys

# ---------------------------------------------------------------------------
# fully-within switch (kept byte-for-byte compatible with suite_common)
# ---------------------------------------------------------------------------
FULLY_WITHIN_ENV = "INCLUDE_FULLY_WITHIN"
INCLUDE_FULLY_WITHIN_ENV = FULLY_WITHIN_ENV
FULLY_WITHIN_MARKER_SUFFIX = ".fullywithin"

#: |standardised DiD| implausible above this (coded SD likely a SE) -- quality guard.
DID_ABS_MAX = 3.0
#: Minimum number of effects for a pooled estimate to be reported.
MIN_K_POOL = 2


def _env_truthy(value):
    """True for the conventional affirmative values (1/true/yes/on)."""
    if value is None:
        return False
    return str(value).strip().lower() not in ("", "0", "false", "no", "off", "none")


def include_fully_within():
    """True when fully-within studies are explicitly opted in."""
    return _env_truthy(os.environ.get(FULLY_WITHIN_ENV))


def include_fully_within_cli(argv=None):
    """Honour ``--include-fully-within`` on the command line, then report the switch."""
    argv = sys.argv if argv is None else argv
    if "--include-fully-within" in argv:
        os.environ[FULLY_WITHIN_ENV] = "1"
    return include_fully_within()


def is_fully_within(record):
    """True when one experiment record is a fully-within (no-control) study.

    Primary marker: the explicit boolean field ``Fully_Within`` is ``True``.  A
    documented fallback applies ONLY when that field is absent: the record has a
    single arm (``n_arms == 1``) and EVERY eligible primary DV is a within-person
    pre->post contrast (``contrast_type == 4``).  ``Title`` is never consulted.
    """
    if not isinstance(record, dict):
        return False
    if "Fully_Within" in record:
        return record.get("Fully_Within") is True
    try:
        if int(record.get("n_arms")) != 1:
            return False
    except (TypeError, ValueError):
        return False
    eligible = [dv for dv in (record.get("DV_Metrics") or [])
                if isinstance(dv, dict) and dv.get("eligible_primary") == 1]
    if not eligible:
        return False
    return all(str(dv.get("contrast_type")).strip() == "4" for dv in eligible)


def write_fully_within_marker(path):
    """Record the include/exclude state beside a spec table (cache correctness)."""
    try:
        with open(str(path) + FULLY_WITHIN_MARKER_SUFFIX, "w", encoding="utf-8") as fh:
            fh.write("1" if include_fully_within() else "0")
    except OSError:
        pass


def fully_within_marker_matches(path):
    """True when ``path`` was last written under the current include switch."""
    want = "1" if include_fully_within() else "0"
    try:
        with open(str(path) + FULLY_WITHIN_MARKER_SUFFIX, encoding="utf-8") as fh:
            return fh.read().strip() == want
    except OSError:
        return False


def fully_within_report(n_skipped, what="record(s)"):
    """One consistent transparency line for the fully-within filter."""
    state = "included (opt-in)" if include_fully_within() else "excluded"
    switch = ("INCLUDE_FULLY_WITHIN=1" if include_fully_within()
              else "INCLUDE_FULLY_WITHIN unset")
    return (f"fully-within: {n_skipped} {what} {state} "
            f"[{switch}; pass --include-fully-within to include]")


# ---------------------------------------------------------------------------
# DV completeness (ONE rule shared by B_validate and studio_lib)
# ---------------------------------------------------------------------------
#: The six-descriptives route (n1i,m1i,sd1i,n2i,m2i,sd2i).
_DESCRIPTIVE_FIELDS = ("n1i", "m1i", "sd1i", "n2i", "m2i", "sd2i")


def _present(dv, *keys):
    return all(dv.get(k) is not None for k in keys)


def dv_has_effect(dv):
    """True when one coded ``DV_Metric`` carries a usable numeric effect.

    The ONE completeness rule: descriptives, or any coded fallback route
    (t / F / r / yi+vi / d or g with arm n's / beta with its SE / odds-ratio with
    its CI).  This is the union of ``B_validate.has_fallback`` and the app's
    ``studio_lib.dv_metrics_state`` routes, so the save-time validator and the
    pipeline gate can never disagree again.

    Arm sizes: a route that needs ``n1i``/``n2i`` also accepts a bare ``n_total``
    for a balanced two-arm design (``n1 = n2 = n_total / 2``), matching
    ``stat_core.meta_effect``.  A standardised regression coefficient is usable
    from ``beta_val`` together with its ``beta_se`` (or ``sei``/``vi``/``n_total``).
    """
    if not isinstance(dv, dict):
        return False
    if _present(dv, *_DESCRIPTIVE_FIELDS):
        return True
    n1, n2, nt = dv.get("n1i"), dv.get("n2i"), dv.get("n_total")
    arms = n1 is not None and n2 is not None
    arms_or_total = arms or nt is not None      # balanced design: n1 = n2 = n_total / 2
    if dv.get("t_val") is not None and arms_or_total:
        return True
    if _present(dv, "F_val", "df_num", "df_den", "n1i", "n2i"):
        return True
    if dv.get("F_val") is not None and dv.get("df_num") == 1 and arms_or_total:
        return True
    if _present(dv, "r_val", "n_total"):
        return True
    if dv.get("yi") is not None and (dv.get("vi") is not None or dv.get("sei") is not None):
        return True
    if (dv.get("d_val") is not None or dv.get("g_val") is not None) and arms_or_total:
        return True
    if dv.get("beta_val") is not None and (
            dv.get("beta_se") is not None or dv.get("sei") is not None
            or dv.get("vi") is not None or nt is not None):
        return True
    if _present(dv, "odds_ratio", "or_ci_low", "or_ci_high"):
        return True
    return False


def dv_missing_reason_contradiction(dv):
    """True when a coded DV carries BOTH a numeric effect and a stated absence.

    An eligible contrast with a computable effect *and* a non-empty
    ``missing_metrics_reason`` is contradictory: the coder flagged the effect as
    unavailable while supplying one.  Such rows are excluded from pooling but
    stay visible in the export.
    """
    if not isinstance(dv, dict):
        return False
    reason = str(dv.get("missing_metrics_reason") or "").strip()
    return bool(reason) and dv_has_effect(dv)


# ---------------------------------------------------------------------------
# controlled dv_domain vocabulary (single source: domain_restriction)
# ---------------------------------------------------------------------------
try:  # domain_restriction is stdlib-only, so it bundles with the exe too
    import domain_restriction as _DR  # noqa: E402
    DV_DOMAINS = list(_DR.DV_DOMAINS)
    SUSCEPTIBILITY_DOMAINS = list(_DR.SUSCEPTIBILITY_DOMAINS)
    NON_OUTCOME_DOMAINS = list(_DR.NON_OUTCOME_DOMAINS)
except Exception:  # pragma: no cover - fallback snapshot (kept in sync manually)
    DV_DOMAINS = ["technique_recognition", "credibility", "sharing_intention",
                  "search_behavior", "attitude_policy", "trust",
                  "media_literacy_attitudes", "critical_thinking", "other"]
    SUSCEPTIBILITY_DOMAINS = ["credibility", "sharing_intention", "technique_recognition"]
    NON_OUTCOME_DOMAINS = ["confidence"]


# ---------------------------------------------------------------------------
# stimulus veracity / dependence family / component side
# ---------------------------------------------------------------------------
_TRUE_TOKENS = re.compile(
    r"non[-_ ]?manipulat|nonmanipulat|non[-_ ]?misinformation|"
    r"\btrue\b|\breal\b|\bfactual\b|\baccurate\b|legitimate|genuine", re.I)
_FALSE_TOKENS = re.compile(
    r"\bmanipulat|misinformation|\bfalse\b|\bfake\b|misleading|inaccurate", re.I)
_DIFF_TOKENS = re.compile(r"discern|_diff\b|difference|signal_detection", re.I)

_DIFFERENCE_SCORE_TYPES = {
    "discernment_difference", "proportion_difference", "signal_detection_auc",
    "signal_detection_d_prime", "signal_detection_other", "composite",
}
_COMPONENT_SCORE_TYPES = {
    "separate_true", "separate_false", "separate_manipulative", "separate_nonmanipulative",
}
_COMPONENT_TRUE_TYPES = {"separate_true", "separate_nonmanipulative"}
_COMPONENT_FALSE_TYPES = {"separate_false", "separate_manipulative"}
_DIFFERENCE_DEFINED_RE = re.compile(
    r"discern|\bdiff\b|difference|minus|non[-_ ]?manipulat.*[-−]\s*manipulat|"
    r"true.*[-−]\s*false|d['\u2032]?\b|auc", re.I)

DV_FAMILY_DIFFERENCE = "difference"
DV_FAMILY_COMPONENT = "separate_component"
DV_FAMILY_OTHER = "other"


def _as_token_set(value):
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        return {str(v).strip().lower() for v in value if v is not None and str(v).strip()}
    s = str(value).strip().lower()
    return {s} if s else set()


def dv_veracity(name, score_types=None, notes="", domains=None):
    """Canonical true/false/'' stimulus-veracity label.

    Mapping (see AGENTS.md / harmonizer_template.py):
      * ``separate_true`` / ``separate_nonmanipulative`` -> ``"true"``;
      * ``separate_false`` / ``separate_manipulative``   -> ``"false"``;
      * difference / composite / signal-detection scores -> ``""`` (no veracity);
      * confidence/certainty DVs -> ``""``;
      * otherwise decode the DV-name tokens.
    """
    st = _as_token_set(score_types)
    nm = str(name or "")
    doms = _as_token_set(domains)
    if st & {"discernment_difference", "signal_detection_auc",
             "signal_detection_d_prime", "signal_detection_other"}:
        return ""
    if "confidence" in doms or re.search(r"confidence", nm, re.I):
        return ""
    if _DIFF_TOKENS.search(nm):
        return ""
    if st & _COMPONENT_TRUE_TYPES:
        return "true"
    if st & _COMPONENT_FALSE_TYPES:
        return "false"
    if _TRUE_TOKENS.search(nm):
        return "true"
    if _FALSE_TOKENS.search(nm):
        return "false"
    return ""


def dv_family(name, score_types=None, defined=""):
    """Classify a DV into a dependence family (difference / component / other)."""
    st = _as_token_set(score_types)
    if st & _DIFFERENCE_SCORE_TYPES:
        return DV_FAMILY_DIFFERENCE
    if st & _COMPONENT_SCORE_TYPES:
        return DV_FAMILY_COMPONENT
    if _DIFFERENCE_DEFINED_RE.search(str(defined or "")):
        return DV_FAMILY_DIFFERENCE
    return DV_FAMILY_OTHER


def dv_component_side(name, score_types=None):
    """Return ``'true'``/``'false'`` for a per-veracity component DV, else ``''``."""
    st = _as_token_set(score_types)
    if st & _COMPONENT_TRUE_TYPES:
        return "true"
    if st & _COMPONENT_FALSE_TYPES:
        return "false"
    return ""


_METACOG_RE = re.compile(r"metacog|confidence|certainty", re.I)


def is_metacognition(domain, name=""):
    """True when a DV is a confidence/certainty/metacognition rating.

    ``uncertainty`` is a distinct construct and is not treated as confidence.
    """
    for s in (str(domain or ""), str(name or "")):
        if not s:
            continue
        if re.search(r"uncertain", s, re.I):
            continue
        if _METACOG_RE.search(s):
            return True
    return False


# ---------------------------------------------------------------------------
# canonical timepoint classifier
# ---------------------------------------------------------------------------
POST_EXACT = {
    "post", "posttest", "post_test", "post_decision", "post_training", "post_w2",
    "post_intervention", "post_0_to_7h", "post_verification", "post_intervention_1",
    "post_t4", "post_7h_window", "post (time 2)", "lecture1_post", "immediate",
    "first_endline_immediate", "t1_immediate", "time 1 (immediate)", "endline",
    "post_video", "post_treatment", "post_message",
    "post_intervention_2", "post_attack", "posttreatment", "postpartum_hospital",
    "after_labeling", "label_removed", "endline (3-5 months)", "pei",
}
FOLLOWUP_EXACT = {
    "time 2 (7-day)", "peii", "peiii", "t2_2weeks",
}
PRE_EXACT = {
    "cross_sectional", "cross-sectional", "label_present",
}
T_LABEL_EXACT = {
    "T0": "pre", "T1": "pre", "T2": "post", "T3": "followup", "T4": "followup",
    "t0": "pre", "t1": "pre", "t2": "post", "t3": "post", "t4": "post",
}
TIMEPOINT_STUDY_OVERRIDES = {}


def _wave_bucket(dv, label):
    """Resolve a ``wave*`` label for the study that emitted it (posttest_type)."""
    pt = dv.get("posttest_type") if isinstance(dv, dict) else None
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


def classify_timepoint(label, dv=None, study_key=None):
    """Map a coded ``timepoint_1``/``timepoint_2`` to a routing bucket.

    Returns ``post`` / ``pre`` / ``followup`` / ``change`` / ``post_assumed`` /
    ``other`` (the vocabulary consumed by ``metaanalysis.py`` and the suite).
    ``dv`` (the coded contrast) and ``study_key`` only refine genuinely
    ambiguous labels -- currently the ``wave*`` family via ``posttest_type``.
    """
    if label is None:
        return "post_assumed"
    raw = str(label).strip()
    if raw == "" or raw.lower() in ("none", "nan", "nat", "<na>"):
        return "post_assumed"
    s = raw.lower()
    if raw in T_LABEL_EXACT:
        return T_LABEL_EXACT[raw]
    if study_key is not None:
        ov = TIMEPOINT_STUDY_OVERRIDES.get((str(study_key), raw))
        if ov:
            return ov
    if s in POST_EXACT:
        return "post"
    if s in FOLLOWUP_EXACT:
        return "followup"
    if s in PRE_EXACT:
        return "pre"
    if "wave" in s:
        return _wave_bucket(dv, s)
    if "follow" in s or s.startswith("fu"):
        return "followup"
    if ("pre" in s or "baseline" in s or "pretreat" in s or "before" in s
            or s in ("base", "cross_sectional")):
        return "pre"
    if "change" in s or "minus" in s:
        return "change"
    return "other"


# ---------------------------------------------------------------------------
# polarity sign (0 / "0" / False / 0.0 => higher is worse)
# ---------------------------------------------------------------------------
def polarity_sign(dv):
    """-1 when higher scores are worse, else +1."""
    if isinstance(dv, dict):
        p = dv.get("dv_polarity")
    else:
        p = dv
    if p in (0, "0", False, 0.0):
        return -1.0
    return 1.0


# ---------------------------------------------------------------------------
# per-intervention record resolution (PER_INTERVENTION_MODEL.md)
# ---------------------------------------------------------------------------
INTERVENTIONS_KEY = "Interventions"


def _intervention_entries(record):
    if not isinstance(record, dict):
        return []
    ivs = record.get(INTERVENTIONS_KEY)
    if isinstance(ivs, list):
        return [iv for iv in ivs if isinstance(iv, dict)]
    return []


def record_has_interventions(record):
    """True when the record carries an explicit (non-empty) ``Interventions`` list."""
    return bool(_intervention_entries(record))


def record_interventions(record):
    """One entry per intervention of an experiment record.

    Explicit ``Interventions`` entries are returned as-is.  Otherwise a single
    implicit intervention is synthesised from the record's own parent coding, so
    a record with no ``Interventions`` key stays one analysis unit.
    """
    entries = _intervention_entries(record)
    if entries:
        return entries
    return [{
        "Intervention_ID": "",
        "Intervention_Type": (record.get("Intervention_Type")
                              if isinstance(record, dict) else None),
        "label": "",
        "overrides": {},
        "implicit": True,
    }]


def effective_record(record, intervention_id=None):
    """Parent coding of ``record`` deep-merged with one intervention's overrides.

    ``intervention_id`` may be an intervention dict, an ``Intervention_ID``
    string, or ``None``.  ``None`` (or an undeclared id) returns the parent
    record unchanged.
    """
    if not isinstance(record, dict):
        return {}
    iv = intervention_id
    if isinstance(iv, str):
        iv = next((x for x in _intervention_entries(record)
                   if str(x.get("Intervention_ID")) == iv), None)
    if not isinstance(iv, dict):
        return dict(record)
    eff = dict(record)
    iid = iv.get("Intervention_ID")
    if iid not in (None, ""):
        eff["Intervention_ID"] = iid
    if iv.get("Intervention_Type") is not None:
        eff["Intervention_Type"] = iv.get("Intervention_Type")
    if iv.get("label") not in (None, ""):
        eff["_intervention_label"] = iv.get("label")
    ov = iv.get("overrides")
    if isinstance(ov, dict):
        for key, val in ov.items():
            eff[key] = val
    return eff
