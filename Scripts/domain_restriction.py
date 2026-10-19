"""Controlled `dv_domain` vocabulary, off-vocabulary resolution, and the
misinformation-susceptibility domain restriction shared by `metaanalysis.py`
(effect-size layer) and `reanalysis.py` (signal-detection layer).

Why this module exists
----------------------
The coded `dv_domain` field is a controlled multi-label list (schema v3.0,
`MisinformationMetaArchived/coding_schemes/schema.yaml` lines 101-117 and
`coding_schemes/common.yaml` lines 399-405).  A small number of records carry
either a blank domain or an off-vocabulary label, and the harmonized sidecars
(`<Key> - harmonized_variables.csv`) carry blank domains for a few DV rows.
Resolution is applied at analysis time through this single, documented mapping
so that every run of `metaanalysis.py` / `reanalysis.py` reproduces the same
classification and the same restricted estimate.

2026-09-19 DV-domain reclassification
-------------------------------------
The catch-all `other` domain had grown to ~500 pooled posttest contrasts.  The
PI directed that every `DV_Metrics` row resolving to `other` be reassessed and
given its correct controlled domain, keeping `other` only for genuinely
unclassifiable outcomes.  The reassessment (`Scripts/dv_domain_reclassify.py`,
mapping table in `Scripts/DV_DOMAIN_RECLASSIFICATION.md`) edited the coded
`dv_domain` field in `Extraction/*.json` directly (62 files, every edited file
backed up to `temp/domain_backup/`), so the controlled label now travels with
the coding.  This module therefore still passes controlled labels through
unchanged and only resolves whatever legacy labels and blank domains remain;
the `OFF_VOCAB_MAP` entries below (e.g. `affect -> other`) continue to classify
the residual legacy rows.  `other` posttest k fell 486 -> 333 and the
non-susceptibility domains absorbed the rest (attitude_policy +84, credibility
+34, media_literacy_attitudes +15, search_behavior +10, ...); the restricted
susceptibility posttest pool rose 2107 -> 2144 (credibility +34,
sharing_intention +3).

Resolution rules (applied in order)
-----------------------------------
1. Controlled labels pass through unchanged.
2. Off-vocabulary labels are mapped via `OFF_VOCAB_MAP` (evidence documented
   per key below).
3. Blank domains are resolved via `BLANK_RESOLUTION` (keyed by study key and
   DV name) or, for a sidecar row, via the `dv_<domain>_...` prefix of the
   uniform variable name.
4. Anything still unresolved falls back to the controlled catch-all `other`.

The first controlled label in a multi-label list is treated as the primary
domain, exactly as the pre-existing `metaanalysis.py` grouping did
(``dom[0]``), so the unrestricted all-domain per-domain rows stay comparable.

Restriction
-----------
`SUSCEPTIBILITY_DOMAINS` is the misinformation-susceptibility family:
credibility, sharing_intention, technique_recognition.  `is_susceptibility(domain)`
gates the restricted pooled model.

All choices here are recorded in `Scripts/DOMAIN_RESTRICTION_NOTE.md`; the
2026-09-19 `belief_accuracy` -> `credibility` merge is recorded in
`Scripts/DV_DOMAIN_MERGE_LOG.md`.
"""

import os
import re

# Pre-merge controlled vocabulary, schema v3.0 (schema.yaml dv_domains).
# 2026-09-19: `confidence` is not a misinformation outcome and `affect` is not a
# distinct outcome; both fold into `other`, and `engagement_behavior` folds into
# `sharing_intention` (see OFF_VOCAB_MAP).  This snapshot is retained ONLY so
# `DV_NO_MERGE=1` can reproduce the pre-merge baseline for the before/after
# report; the live vocabulary (`DV_DOMAINS`) never offers dropped labels.
_PRE_MERGE_DV_DOMAINS = [
    "technique_recognition",
    "credibility",
    "belief_accuracy",
    "sharing_intention",
    "search_behavior",
    "attitude_policy",
    "trust",
    "media_literacy_attitudes",
    "critical_thinking",
    "other",
]

# ---------------------------------------------------------------------------
# Domain merge (PI decision 2026-09-19): `belief_accuracy` is merged INTO
# `credibility` (both true/false veracities), so the credibility pool absorbs all
# former belief_accuracy DVs and the susceptibility family becomes
# {credibility, sharing_intention, technique_recognition}.  `belief_accuracy` is
# therefore removed from the controlled vocabulary and from the susceptibility
# family, and any legacy coded/sidecar label is mapped by `OFF_VOCAB_MAP`.  Set
# `DV_NO_MERGE=1` to reproduce the pre-merge vocabulary (before/after report).
# ---------------------------------------------------------------------------
BELIEF_ACCURACY_MERGED = os.environ.get("DV_NO_MERGE") != "1"
DV_DOMAINS = [
    d for d in _PRE_MERGE_DV_DOMAINS
    if not (BELIEF_ACCURACY_MERGED and d == "belief_accuracy")
]
# Domains recognised when resolving a legacy coded/sidecar label.  Includes the
# pre-merge label so a legacy value can still be decoded and then merged.
_RESOLVABLE_DOMAINS = list(_PRE_MERGE_DV_DOMAINS)

_BASE_SUSCEPTIBILITY = ["belief_accuracy", "credibility", "sharing_intention",
                        "technique_recognition"]
# Misinformation-susceptibility family used by the restricted pooled model.
SUSCEPTIBILITY_DOMAINS = [
    d for d in _BASE_SUSCEPTIBILITY
    if not (BELIEF_ACCURACY_MERGED and d == "belief_accuracy")
]


def merge_domain(dom):
    """Apply the belief_accuracy -> credibility merge (idempotent)."""
    if BELIEF_ACCURACY_MERGED and str(dom) == "belief_accuracy":
        return "credibility"
    return str(dom)

# Non-outcome labels.  `confidence`/`certainty` ratings belong to the SEPARATE confidence &
# metacognition strand (see Scripts/METACOGNITION_ASSESSMENT.md); they are NOT a misinformation
# outcome and must never be pooled by metaanalysis.py / reanalysis.py -- not as a domain and not
# folded into `other`.  They are given their own label so they stay identifiable and are dropped
# from every pooled model.
NON_OUTCOME_DOMAINS = ["confidence"]

# A uniform variable name that is a confidence or certainty rating (token-matched, so
# `uncertainty` / `uncertain` is NOT mistaken for `certainty`).  Used to catch confidence DVs
# that were coded under an outcome domain (e.g. `dv_belief_accuracy_confidence_*`).
_CONF_TOKEN = re.compile(r"(^|[_\-])(confidence|certainty|conf)([_\-]|$)", re.I)
_UNCERTAIN = re.compile(r"uncertain", re.I)


def is_confidence_name(name):
    """True if `name` is a confidence/certainty rating variable (not `uncertainty`)."""
    s = str(name)
    return bool(_CONF_TOKEN.search(s)) and not _UNCERTAIN.search(s)


def is_non_outcome(domain):
    """True for a label that must be kept out of every pooled outcome model."""
    return str(domain) in NON_OUTCOME_DOMAINS

# Off-vocabulary / legacy dv_domain label -> controlled dv_domain.
# Evidence for each mapping is in Scripts/DOMAIN_RESTRICTION_NOTE.md.
# 2026-09-19: after the `other` reclassification (Scripts/
# DV_DOMAIN_RECLASSIFICATION.md) the coded Extraction labels are controlled, so
# these entries only classify the residual legacy labels that were deliberately
# kept (e.g. the remaining `affect` rows, which are genuinely unclassifiable
# emotion measures).  The 2026-09-19 reclassification did not change these
# mappings.
#   social_norms       -> other : the 2026-09-17 vocabulary change merged
#        social_norms / behavior_choice / echo_chamber / information_overload /
#        general_transfer into `other`; legacy harmonized sidecars that still
#        carry the old label resolve here.
#   behavioral_choice  -> other : `behavioral_choice` is a *score type*
#        (schema.yaml score_types line 132), not a DV domain.  The same study
#        (Allred & Bolton 2024) codes its other consumption/behaviour DVs
#        ("Donation to lab-cultured-meat charity", "Homeopathic product
#        purchase intentions") as `other`; the corpus likewise places
#        gift-card choice (Ahmad 2024) and EV purchase intent (Calvillo 2026)
#        under `other`.  Caple (2019) lists it alongside `attitude_policy`
#        and keeps its primary label `attitude_policy`.
OFF_VOCAB_MAP = {
    "social_norms": "other",
    "behavioral_choice": "other",
    "echo_chamber": "other",
    "information_overload": "other",
    "general_transfer": "other",
    # 2026-09-19: `confidence` is not a misinformation outcome and, per the
    # metacognition strand, must NOT be pooled even as `other`; it keeps its own
    # non-outcome label so it stays identifiable and is dropped from every model.
    "confidence": "confidence",
    # 2026-09-19: affective reactions are not a separate outcome; `affect`
    # folds into `other`.
    "affect": "other",
    # 2026-09-19: engagement with content is treated as an expression of sharing
    # intention, so `engagement_behavior` folds into `sharing_intention`.
    "engagement_behavior": "sharing_intention",
}

# 2026-09-19 (PI decision): `belief_accuracy` is merged into `credibility`:
# belief accuracy is treated as source/message credibility.  It is mapped here so
# any legacy coded record or harmonized sidecar that still carries the old label
# resolves to `credibility`; with `DV_NO_MERGE=1` the mapping is disabled and the
# pre-merge label resolves as itself (before/after report).
if BELIEF_ACCURACY_MERGED:
    OFF_VOCAB_MAP["belief_accuracy"] = "credibility"

# Blank domain resolved by (study_key, dv_name).  Both entries are
# non-quantitative placeholder rows with eligible_primary = 0 and no effect.
BLANK_RESOLUTION = {
    # Fendt et al. (2026 a): the two already-labelled rows in the same
    # experiment are `belief_accuracy` (true/fake news accuracy); these are the
    # separate-true / separate-fake components of the same accuracy outcome.
    ("Fendt et al. (2026 a)", "N_POST_Diff"): "belief_accuracy",
    ("Fendt et al. (2026 a)", "POST_True"): "belief_accuracy",
    ("Fendt et al. (2026 a)", "POST_Fake"): "belief_accuracy",
    # Cartiff (2023) Exp1: qualitative think-aloud validation, no quantitative
    # outcome ("not coded"), eligible_primary = 0.
    ("Cartiff (2023)", "not coded - qualitative think-aloud validation (no quantitative outcome)"): "other",
    # Zimmerman (2021): abstract-only dissertation summary, no numeric results,
    # eligible_primary = 0.
    ("Zimmerman (2021)", "Search-harm outcomes (multiple offline/online studies; no numeric results in abstract)"): "other",
}

# Exact DV-name fallback (used only when the (study_key, dv_name) pair is absent).
BLANK_NAME_RESOLUTION = {
    "POST_True": "belief_accuracy",
    "POST_Fake": "belief_accuracy",
    "N_POST_Diff": "belief_accuracy",
}


def _as_list(raw):
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        return [str(x) for x in raw if x is not None and str(x).strip() != ""]
    s = str(raw).strip()
    return [s] if s else []


def resolve_domain(raw, study_key="", dv_name=""):
    """Return one controlled dv_domain for a coded `DV_Metrics` record.

    Confidence/certainty records return the non-outcome label `confidence` (never `other`),
    so metaanalysis.py can drop them from every pooled model.
    """
    for v in _as_list(raw):
        v = OFF_VOCAB_MAP.get(v, v)
        if v in NON_OUTCOME_DOMAINS:
            return v
        if v in _RESOLVABLE_DOMAINS:
            return merge_domain(v)
    # Confidence/certainty DVs stay a separate strand even when coded under an outcome domain.
    if is_confidence_name(dv_name):
        return "confidence"
    key = (str(study_key), str(dv_name))
    if key in BLANK_RESOLUTION:
        return merge_domain(BLANK_RESOLUTION[key])
    if str(dv_name) in BLANK_NAME_RESOLUTION:
        return merge_domain(BLANK_NAME_RESOLUTION[str(dv_name)])
    return "other"


def resolve_sidecar_domain(raw, uniform_variable):
    """Return one controlled dv_domain for a harmonized sidecar DV row.

    The harmonizer naming convention is `dv_<domain>[_<veracity>]...`, so a
    blank sidecar domain can be recovered from the longest controlled-domain
    prefix of the uniform variable (matched on token boundaries).  Verified
    against the sidecar `notes` and the study's coding JSON; see note.

    Confidence/certainty variables return the non-outcome label `confidence`
    (never `other`), taking precedence over any coded outcome domain, so that
    reanalysis.py excludes them from every pooled model.
    """
    if is_confidence_name(uniform_variable):
        return "confidence"
    vals = _as_list(raw)
    for v in vals:
        v = OFF_VOCAB_MAP.get(v, v)
        if v in NON_OUTCOME_DOMAINS:
            return v
        if v in _RESOLVABLE_DOMAINS:
            return merge_domain(v)
    u = str(uniform_variable)
    if u.startswith("dv_"):
        u = u[3:]
    for dom in sorted(_RESOLVABLE_DOMAINS, key=len, reverse=True):
        if u == dom or u.startswith(dom + "_"):
            return merge_domain(dom)
    return "other"


def is_susceptibility(domain):
    return str(domain) in SUSCEPTIBILITY_DOMAINS


if __name__ == "__main__":
    # Self-check: report the resolution for the known problem records.
    for k, dvn, raw in [
        ("Abroms et al. (2024)", "Intention to encourage others to vaccinate", ["social_norms"]),
        ("Allred & Bolton (2024)", "Number of homeopathic products chosen", ["behavioral_choice"]),
        ("Caple (2019)", "GMO behavior/intentions change", ["attitude_policy", "behavioral_choice"]),
        ("Fendt et al. (2026 a)", "POST_True", None),
        ("Fendt et al. (2026 a)", "POST_Fake", None),
        ("Cartiff (2023)", "not coded - qualitative think-aloud validation (no quantitative outcome)", []),
        ("Zimmerman (2021)", "Search-harm outcomes (multiple offline/online studies; no numeric results in abstract)", []),
        ("__check__", "confidence outcome", ["confidence"]),
        ("__check__", "affect outcome", ["affect"]),
        ("__check__", "engagement outcome", ["engagement_behavior"]),
    ]:
        print(k, "|", dvn[:50], "|", raw, "->", resolve_domain(raw, k, dvn))
    for u in ["dv_attitude_policy_registration_support", "dv_other_policy_knowledge_option_i1",
              "dv_affect_happy", "dv_belief_accuracy_true_i01"]:
        print(u, "->", resolve_sidecar_domain("", u))
