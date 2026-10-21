"""Convert the legacy misinformation schema (v3.1) into a project scheme.

The v3.1 document (``Scripts/coding_web/schema.json``) is a flat, campaign-specific
artefact: string groups, a common-field list, a DV-field list, intervention
modules and a pile of controlled vocabularies.  A *scheme* is the general form:
blocks, fields, roles, conditions, kinds and rules.

The conversion is **name-preserving on purpose**.  Every field keeps its v3.1
``name``, because the name is the key a coded record is stored under - the
792 ``Extraction/<Key>.json`` and the 3 148 ``Codings/<Key> - <Coder>.json``
files stay valid byte-for-byte.  What the conversion adds is the *role* of each
field, the block it belongs to, the conditions it may carry, and the policy
rules that used to be hardcoded in the pipeline.

The tables below transcribe the hardcoded lookups that today live in
``Scripts/coding_web/static/app.js`` so that the scheme carries them as data.
"""

from __future__ import annotations

import scheme_lib as SL

#: Per-field PDF-search hints used by coder mode to find a field's evidence in
#: the paper.  Transcribed from ``CODER_LOCATE`` in ``static/app.js``.
CODER_LOCATE = {
    "N_Recruited": ["recruit", "enrolled", "sampled"],
    "N_Analyzed": ["analy", "final sample", "excluded"],
    "Age_Mean": ["mean age", "years old", "age"],
    "Age_Group": ["students", "adults", "age"],
    "Gender_Ratio": ["female", "male", "gender"],
    "Country": ["country", "nationwide"],
    "Setting": ["online", "laboratory", "classroom", "field"],
    "Intervention_Type": ["intervention", "training", "treatment"],
    "Intervention_Duration": ["minutes", "duration", "session"],
    "Modality": ["video", "game"],
    "Stimuli": ["headlines", "claims", "stimuli"],
    "Synthetic": ["synthet", "real"],
    "control_group_type": ["control group", "placebo", "comparison"],
    "within_between": ["between-subjects", "within-subjects"],
    "unit_of_analysis": ["participants", "items"],
    "Attention_Cues": ["attention", "warning"],
}

#: Fields the streamlined coder pass skips (typed by hand).  Transcribed from
#: ``CODER_MANUAL_FIELDS`` in ``static/app.js``.
CODER_MANUAL_FIELDS = {
    "Study_ID", "Parent_Study_ID", "DOI", "Authors", "Title", "Study_Description",
    "Rationale_Notes", "Sample_Region", "Intervention_Topic", "Stimuli_Topic",
    "Theory_Basis", "Booster_Content", "Intervention_Type_Detail",
    "comparison_label", "discernment_defined_as", "conversion_method",
    "effect_direction_meaning", "missing_metrics_reason",
    "Publication", "Year", "N_Reported_Per_Condition", "N_Inference_Method",
    "International_Sample", "WEIRD_Western", "WEIRD_Educated",
    "WEIRD_Industrialized", "WEIRD_Rich", "WEIRD_Democratic",
    "dataset_availability_status", "data_available", "code_available",
    "preregistration_status", "ethics", "consent_reported",
}

#: Metric fields coder mode hides outright.  Transcribed from ``CODER_DV_HIDE``
#: in ``static/app.js``.
CODER_DV_HIDE = {
    "score_types", "discernment_defined_as", "comparison_label", "timepoint_1",
    "timepoint_2", "posttest_type", "delay_weeks", "scale_type", "scale_points",
    "scale_min", "scale_max", "p_censored", "effect_size_type_reported",
    "effect_favors", "effect_direction_meaning", "conversion_method",
}

#: The DV card layout.  Transcribed from ``DESCRIPTIVE_GROUPS`` /
#: ``STAT_FAMILIES`` / ``DV_HEAD_GROUPS`` / ``DV_EXTRA_GROUPS`` in
#: ``static/app.js``; in the scheme this is data, not code.
DV_LAYOUT = {
    "head": [
        {"title": "Completeness", "names": ["missing_metrics_reason"]},
        {"title": "Variable", "names": [
            "dv_name", "dv_domain", "score_types", "discernment_defined_as",
            "dv_polarity", "scale_type", "scale_points", "scale_min", "scale_max"]},
        {"title": "Contrast & groups", "names": [
            "comparison_label", "group1_label", "group2_label", "contrast_type",
            "eligible_primary", "timepoint_1", "timepoint_2", "posttest_type",
            "delay_weeks"]},
    ],
    "families": {
        "descriptives": {
            "label": "Descriptives - n, M, SD",
            "groups": [
                {"title": "Descriptives - tested timepoint",
                 "names": ["n1i", "m1i", "sd1i", "n2i", "m2i", "sd2i"]},
                {"title": "Descriptives - pre",
                 "names": ["n1i_pre", "m1i_pre", "sd1i_pre", "n2i_pre", "m2i_pre", "sd2i_pre"]},
                {"title": "Descriptives - post",
                 "names": ["n1i_post", "m1i_post", "sd1i_post", "n2i_post", "m2i_post", "sd2i_post"]},
            ],
        },
        "t": {"label": "t statistic",
              "groups": [{"title": "t test", "names": ["t_val", "df_den", "n1i", "n2i"]}]},
        "F": {"label": "F statistic",
              "groups": [{"title": "F test", "names": ["F_val", "df_num", "df_den", "n1i", "n2i"]}]},
        "r": {"label": "Correlation r",
              "groups": [{"title": "Correlation", "names": ["r_val", "n_total"]}]},
        "yi": {"label": "Effect size yi + variance",
               "groups": [{"title": "Effect size (yi)", "names": ["yi", "vi", "sei"]}]},
        "d_g": {"label": "Cohen's d / Hedges' g",
                "groups": [{"title": "Standardised mean difference",
                            "names": ["d_val", "g_val", "d_ci_low", "d_ci_high",
                                      "g_ci_low", "g_ci_high", "d_se", "g_se", "n1i", "n2i"]}]},
        "odds_ratio": {"label": "Odds ratio / log-odds",
                       "groups": [{"title": "Odds & risk",
                                   "names": ["log_odds", "odds_ratio", "or_ci_low",
                                             "or_ci_high", "risk_ratio", "n1i", "n2i"]}]},
        "beta": {"label": "Regression beta (+ SE)",
                 "groups": [{"title": "Regression",
                             "names": ["beta_val", "beta_se", "z_val", "n_total"]}]},
        "eta2p": {"label": "Partial eta squared",
                  "groups": [{"title": "ANOVA effect size",
                              "names": ["eta2_p", "eta2_p_ci_low", "eta2_p_ci_high",
                                        "F_val", "df_num", "df_den"]}]},
        "chi2": {"label": "p-value / chi-square",
                 "groups": [{"title": "Significance",
                             "names": ["chi2_val", "p_val", "p_censored"]}]},
    },
    "extra": [
        {"title": "Effect size metadata",
         "names": ["effect_size_type_reported", "effect_favors",
                   "effect_direction_meaning", "conversion_method"]},
        {"title": "Conversion detail", "names": ["conversion_inputs", "cell_descriptives"]},
    ],
}

#: The harmonization sidecar slots, transcribed from
#: ``Scripts/harmonization_coverage.py`` and ``Scripts/coding_web/routes_coverage.py``.
HARMONIZATION_SLOTS = [
    {"id": "S1", "name": "id", "weight": 2.0},
    {"id": "S2", "name": "condition_col", "weight": 5.0},
    {"id": "S3", "name": "arm_roles", "weight": 4.0},
    {"id": "S4", "name": "dv_pairs", "weight": 2.0},
    {"id": "S5", "name": "dv_domain", "weight": 4.0},
    {"id": "S6", "name": "veracity", "weight": 5.0},
    {"id": "S6cell", "name": "veracity_cell", "weight": 5.0},
    {"id": "S7", "name": "timepoint", "weight": 1.0},
    {"id": "S8", "name": "dv_polarity", "weight": 3.0},
    {"id": "S9", "name": "exclusions", "weight": 2.0},
    {"id": "S10", "name": "covariates", "weight": 1.0},
    {"id": "S11", "name": "provenance", "weight": 1.0},
]

#: The fields whose vocabulary is a CONTROLLED vocabulary even though the
#: control is a token field: the values are closed, the pipeline resolves
#: anything else.  Their vocabulary moves to ``controlled_values`` so a
#: validation contract can use it without turning a suggestion list into an
#: enumeration.  The other token fields stay open - the corpus types prose
#: into them on purpose (``Media_Support`` -> "Images", ``Audience`` -> a
#: whole sentence).
CONTROLLED_VOCAB_FIELDS = (
    "Intervention_Type",     # the module routing depends on it
    "dv_domain",             # the pipeline maps anything else to "other"
    "score_types",           # the pipeline matches the score type by token
)

#: Values that are accepted on read but never offered to a coder: the
#: vocabularies the campaign retired but the pipeline still resolves.
#:
#: * ``dv_domain`` - the 2026-09-19 merge folded ``engagement_behavior`` into
#:   ``sharing_intention``, ``confidence`` and ``affect`` into ``other`` and
#:   ``belief_accuracy`` into ``credibility`` (see ``Scripts/domain_restriction.py``,
#:   whose ``OFF_VOCAB_MAP`` still decodes them).  The live vocabulary never
#:   offers them; a coded record may still carry them.
#: * ``effect_size_type_reported`` - the pipeline matches the reported
#:   estimator by token (``suite_common._PAIRWISE_EST_EXACT`` /
#:   ``_PAIRWISE_EST_TOKENS``), so the corpus carries t, F, chi2, log_odds and
#:   the d/g spellings beside the eleven the scheme offers.
LEGACY_OPTIONS = {
    "dv_domain": ["engagement_behavior", "confidence", "affect", "belief_accuracy"],
    "effect_size_type_reported": [
        "t", "F", "chi2", "log_odds", "means_sd",
        "cohen_d", "cohens_d", "hedges_g", "smd", "d_val", "g_val",
    ],
}

#: Fields the CORPUS carries that the legacy v3.1 schema never declared.
#: They are added to the scheme because the pipeline reads them (or because a
#: contrast's provenance needs them); the remaining undeclared corpus keys are
#: legacy fields the campaign never retired and are recorded as a finding
#: rather than blessed as contract.
#:
#: * ``Fully_Within`` - the single-arm marker; ``Scripts/contract.py``
#:   ``is_fully_within`` reads it by name and it decides whether a study is in
#:   every pool.
#: * ``source_span`` - the PDF rectangle of a contrast's source quote, next to
#:   the record-level ``source_spans``; it is per-contrast, so it is a metric
#:   field.
#:
#: The other legacy spellings are ALIASES of a field the scheme already has,
#: not new fields - two fields cannot share a role, so the canonical name wins
#: and the alternative spelling is accepted on read.
CORPUS_ONLY_STUDY_FIELDS = [
    {"name": "Fully_Within", "label": "Fully within (single arm)",
     "group": "Open Science & Design", "type": "boolean", "editor": "boolean",
     "role": "fully_within",
     "description": "True when the experiment has no control arm, so it is "
                    "excluded from every pooled product by default."},
]

CORPUS_ONLY_METRIC_FIELDS = [
    {"name": "source_span", "label": "Source span (PDF rectangle)",
     "group": "DV_Metrics", "type": "object", "editor": "json",
     "role": "source_span",
     "description": "The page and the normalised rectangles of the passage the "
                    "source quote was taken from."},
]

#: Legacy spellings the corpus carries next to the canonical field.
FIELD_ALIASES = {
    "Rationale_Notes": ["notes"],
    "Publication": ["Publication_Type", "Publication_Category"],
}

#: The policy that used to be hardcoded in the pipeline, now data.
DEFAULT_RULES = {
    # A multi-df omnibus cannot be signed and is never a two-arm effect.
    "omnibus_exclusions": {"contrast_type_codes": ["11"]},
    # A single-arm, no-control experiment is excluded from every pool by default.
    "fully_within": {"enabled": True, "contrast_type_codes": ["4"]},
    # The three questions that are never pooled together.
    "timepoints": {
        "immediate": ["post"],
        "followup": ["followup"],
        "did": ["pre", "post"],
    },
    "polarity_values": ["higher_is_better", "higher_is_worse", "ambiguous"],
    "veracity_values": ["true", "false", "true_vs_false"],
    # The pipeline's own timepoint labels (Scripts/contract.py classify_timepoint):
    # it routes a DV to "change", "post_assumed" or "other" when the label is
    # not one of the three canonical buckets.
    "timepoint_values": ["pre", "post", "followup", "change", "post_assumed", "other"],

    # -- the score-type families ------------------------------------------- #
    # Which score_types tokens mark a difference/composite measure and which
    # mark a separate stimulus-veracity component.  These sets exist in
    # Scripts/contract.py and Scripts/suite_common.py; they must end up here
    # only once, because two copies are two answers.
    "score_type_families": {
        "difference": [
            "discernment_difference", "proportion_difference",
            "signal_detection_auc", "signal_detection_d_prime",
            "signal_detection_other", "composite",
        ],
        "component": [
            "separate_true", "separate_false",
            "separate_manipulative", "separate_nonmanipulative",
        ],
        "component_true": ["separate_true", "separate_nonmanipulative"],
        "component_false": ["separate_false", "separate_manipulative"],
    },

    # -- the domain policy -------------------------------------------------- #
    # A legacy label that resolves to a controlled domain
    # (Scripts/domain_restriction.py OFF_VOCAB_MAP; the evidence for each
    # mapping is in Scripts/DOMAIN_RESTRICTION_NOTE.md).
    "domain_aliases": {
        "social_norms": "other",
        "behavioral_choice": "other",
        "echo_chamber": "other",
        "information_overload": "other",
        "general_transfer": "other",
        "confidence": "confidence",
        "affect": "other",
        "engagement_behavior": "sharing_intention",
    },
    # A label merged into another one at coding time (the 2026-09-19 PI
    # decision); the legacy label stays decodable.
    "domain_merges": {"belief_accuracy": "credibility"},
    # A domain that must never be pooled, not even as "other".
    "non_outcome_domains": ["confidence"],
    # The misinformation-susceptibility family: the restricted pooled model.
    "susceptibility_domains": [
        "credibility", "sharing_intention", "technique_recognition",
    ],
    # The domains whose polarity depends on the stimulus veracity
    # (Scripts/dv_polarity.py): behaviour, credibility and the technique
    # recognition family, which mixes two construct families.
    "veracity_dependent_domains": {
        "behaviour": ["sharing_intention", "engagement_behavior"],
        "credibility": ["credibility", "belief_accuracy"],
        "technique_recognition": ["technique_recognition"],
    },

    # -- what a complete coding needs --------------------------------------- #
    # One list, read by B_validate.py and by the app's save-time validator.
    "required_fields": [
        "Study_ID", "Year", "Authors", "Title", "Study_Description",
        "Intervention_Type", "DV_Metrics",
    ],
    # The descriptive fields that make a contrast poolable on its own.
    "descriptive_fields": ["n1i", "m1i", "sd1i", "n2i", "m2i", "sd2i"],
}


def _vocabularies(legacy):
    """The v3.1 vocabularies plus the ones the pipeline used to duplicate."""
    vocabs = dict(legacy.get("vocabularies") or {})
    vocabs.setdefault("susceptibility_domains",
                      ["credibility", "sharing_intention", "technique_recognition"])
    vocabs.setdefault("non_outcome_domains", ["confidence"])
    vocabs.setdefault("veracity_dependent_domains",
                      ["sharing_intention", "engagement_behavior", "credibility",
                       "belief_accuracy", "technique_recognition"])
    vocabs.setdefault("harmonization_slots", HARMONIZATION_SLOTS)
    return vocabs


def _rules(legacy):
    rules = dict(DEFAULT_RULES)
    rules["harmonization_slots"] = HARMONIZATION_SLOTS
    return rules


def _decorate(field, scope):
    """Attach the coder-mode flags, the PDF-search hints and the legacy values."""
    name = field.get("name")
    if scope == "metric":
        field["coder"] = {"auto": True, "hidden": name in CODER_DV_HIDE}
    else:
        manual = name in CODER_MANUAL_FIELDS
        field["coder"] = {"auto": not manual, "hidden": False}
    locate = CODER_LOCATE.get(name)
    if locate:
        field["source"] = {"locate": list(locate)}
    legacy = LEGACY_OPTIONS.get(name)
    if legacy:
        field["legacy_options"] = [{"value": v, "label": v} for v in legacy]
    if name in CONTROLLED_VOCAB_FIELDS:
        controlled = list(field.get("vocab") or [])
        for option in field.get("options") or []:
            value = option.get("value")
            if value is not None and value not in controlled:
                controlled.append(value)
        field["controlled_values"] = controlled
    return field


def convert(legacy, scheme_id="misinformation", name="misinformation-interventions",
            label="Misinformation Interventions (v3.1)"):
    """Convert a v3.1 ``schema.json`` document into a scheme document.

    The result is a plain dict in the canonical shape; it is *not* normalised
    yet - call :func:`scheme_lib.normalise_scheme` on the result.
    """
    legacy = legacy if isinstance(legacy, dict) else {}

    # v3.1 puts the 76 metric fields in a group called "Other" and the module
    # fields in "Intervention module (<key>)".  In a scheme those are not
    # groups: the metrics have their own block and the module fields belong to
    # their module.  Only the groups that really hold study fields survive.
    study_groups = []
    for raw in legacy.get("common_fields") or []:
        group_name = raw.get("group")
        if group_name and group_name not in study_groups:
            study_groups.append(group_name)
    groups = [{"name": g, "label": g} for g in study_groups]

    fields = []
    seen = {}
    for raw in legacy.get("common_fields") or []:
        field = dict(raw)
        field["scope"] = "study"
        fields.append(_decorate(field, "study"))
        seen[field.get("name")] = field
    for raw in CORPUS_ONLY_STUDY_FIELDS:
        field = dict(raw)
        field["scope"] = "study"
        fields.append(_decorate(field, "study"))
        seen[field.get("name")] = field

    modules = {}
    for key, mod in (legacy.get("modules") or {}).items():
        mod_fields = []
        for raw in (mod or {}).get("fields") or []:
            field = dict(raw)
            field["scope"] = "module"
            field["module"] = key
            field["group"] = key
            name_ = field.get("name")
            owner = seen.get(name_)
            if owner is not None:
                # A field name that repeats across modules keeps its first
                # occurrence; the later option lists ride along in
                # ``module_options`` so a reader can still offer the right
                # levels for the module in play.
                options = field.get("options")
                if options:
                    owner.setdefault("module_options", {}).setdefault(key, options)
                continue
            seen[name_] = field
            mod_fields.append(_decorate(field, "module"))
        modules[key] = {
            "key": key,
            "label": (mod or {}).get("label") or key,
            "family": (mod or {}).get("family"),
            "description": (mod or {}).get("description") or "",
            "intervention_types": (mod or {}).get("intervention_types") or [],
            "fields": mod_fields,
        }

    metric_fields = []
    for raw in legacy.get("dv_fields") or []:
        field = dict(raw)
        field["scope"] = "metric"
        field["group"] = "DV_Metrics"
        metric_fields.append(_decorate(field, "metric"))
    for raw in CORPUS_ONLY_METRIC_FIELDS:
        field = dict(raw)
        field["scope"] = "metric"
        metric_fields.append(_decorate(field, "metric"))

    return {
        "scheme_version": SL.SCHEME_VERSION,
        "id": scheme_id,
        "name": name,
        "label": label,
        "description": (
            "Coding scheme for the misinformation-intervention meta-analysis: "
            "study metadata, sample, open-science and design, the intervention "
            "modules, and the per-contrast effect sizes."),
        "groups": groups,
        "fields": fields,
        "metrics": {
            "enabled": bool(metric_fields),
            "group": "DV_Metrics",
            "label": "Effect sizes",
            "fields": metric_fields,
            "layout": DV_LAYOUT,
        },
        "modules": modules,
        "intervention_type_modules": legacy.get("intervention_type_modules") or {},
        "vocabularies": _vocabularies(legacy),
        "field_aliases": {k: list(v) for k, v in FIELD_ALIASES.items()},
        "effect_families": list(SL.EFFECT_FAMILIES),
        "kinds": [],
        "structures": {
            # The record's structural list: the intervention arms.  The
            # pipeline addresses the arm id by name (contract.py
            # INTERVENTIONS_KEY), so the scheme declares the arm's members.
            "Interventions": {
                "kind": "list",
                "label": "Intervention arms",
                "description": "One entry per arm of the experiment; each arm "
                               "carries its own type and may override the "
                               "record's field values.",
                "item_fields": [
                    {"name": "Intervention_ID", "label": "Arm id", "type": "string",
                     "role": "intervention_id"},
                    {"name": "Intervention_Type", "label": "Arm type", "type": "list",
                     "role": "intervention_type"},
                    {"name": "label", "label": "Arm label", "type": "string"},
                    {"name": "overrides", "label": "Field overrides", "type": "object"},
                ],
            },
        },
        "rules": _rules(legacy),
    }


def convert_to_normalised(legacy, **kwargs):
    """Convenience: :func:`convert` followed by :func:`scheme_lib.normalise_scheme`."""
    return SL.normalise_scheme(convert(legacy, **kwargs))


def legacy_field_names(legacy):
    """Every field name the v3.1 document declares (for a lossless check)."""
    names = set()
    for raw in legacy.get("common_fields") or []:
        if raw.get("name"):
            names.add(raw["name"])
    for raw in legacy.get("dv_fields") or []:
        if raw.get("name"):
            names.add(raw["name"])
    for mod in (legacy.get("modules") or {}).values():
        for raw in (mod or {}).get("fields") or []:
            if raw.get("name"):
                names.add(raw["name"])
    return names
