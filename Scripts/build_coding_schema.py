"""Snapshot the v3.1 coding scheme into a flat ``schema.json`` for the web coder.

Reads ``coding_schemes/*.yaml`` (common core + intervention modules + the DV
effect-size block) and derives, for every field, the editor kind and any
deterministic level map that can be parsed from the schema description
(``1=Foo, 2=Bar`` -> ``{"1": "Foo", "2": "Bar"}``).

Run:  python Scripts/build_coding_schema.py
Writes: Scripts/coding_web/schema.json
"""

import json
import os
import re
import sys

import yaml

B = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(B, "Scripts", "coding_web", "schema.json")

SCHEME_CANDIDATES = [
    os.path.join(B, "coding_schemes"),
    os.path.join(B, "MisinformationMetaArchived", "coding_schemes"),
    os.path.join(B, "archive", "sandbox", "campaign", "coding_schemes"),
    os.path.join(B, "MisinformationMetaArchived", "sandbox", "campaign", "coding_schemes"),
]

MULTISELECT_VOCABS = {
    "dv_domain": "dv_domains",
    "score_types": "score_types",
    "Intervention_Type": "intervention_types",
    "Technique_Taxonomy": "technique_taxonomy",
    "Attention_Cues": "attention_cues",
    "Media_Support": "media_support",
    "Comm_Strategies": "comm_strategies",
    "Practice_Strategy": "practice_strategy",
    "Intervention_Components": "intervention_components",
}

TEXTAREA_FIELDS = {
    "Study_Description",
    "Rationale_Notes",
    "Intervention_Type_Detail",
    "Intervention_Topic",
    "Sample_Region",
    "Stimuli_Topic",
    "Theory_Basis",
    "Booster_Content",
    "clustering",
    "effect_direction_meaning",
    "conversion_method",
    "missing_metrics_reason",
    "discernment_defined_as",
    "comparison_label",
}

OBJECTLIST_FIELDS = {
    "Covariates": [("covariate", "string"), ("dv_name", "string"), ("r_val", "number")],
    "factor_levels": [("factor", "string"), ("levels", "list")],
    "cell_descriptives": [
        ("group", "string"),
        ("timepoint", "string"),
        ("n", "number"),
        ("mean", "number"),
        ("sd", "number"),
    ],
}

EXTRA_VOCABS = {
    "technique_taxonomy": [
        "emotional_manipulation", "fake_experts", "conspiracy", "polarization",
        "amplification", "trolling", "naturalistic_fallacy", "ad_hominem",
        "false_dichotomy", "coherence_break", "discrediting", "scapegoating",
        "false_balance", "conjunction_fallacy", "impersonation",
    ],
    "attention_cues": [
        "Visual Signaling/Highlighting", "Explicit Ignore Instructions",
        "Physiological/Emotional Focus",
    ],
    "media_support": [
        "Visual aids", "Video", "Audio", "Text", "Interactive/gamified",
        "Model teaching", "Physical materials", "Other",
    ],
    "comm_strategies": [
        "Sketching/narrative", "Verbal labelling", "Interview techniques",
        "Group discussion", "Debate", "Other",
    ],
    "practice_strategy": [
        "Recall tests", "Easy-to-hard sequencing", "Classification/sorting",
        "Compare-and-contrast", "Self-explanation/reflection", "Guided practice",
        "Feedback-based practice", "Media production", "Other",
    ],
    "intervention_components": [
        "Corrective information", "Threat/forewarning", "Refutation",
        "Technique exposure", "Narrative/storytelling", "Discussion/interaction",
        "Emotion induction/regulation", "Source salience", "Instruction",
        "Nudge", "Feedback", "Community messaging", "Platform/exposure reduction",
        "Other",
    ],
}

VOCAB_FOR_FIELD = {
    "effect_size_type_reported": "effect_size_types",
}

NUM_RE = re.compile(r"(\d+)\s*=\s*")

# ---------------------------------------------------------------------------
# Campaign-level schema corrections (2026-09-19, analysis-tab fix).
#
# The YAML scheme is the base; these overrides are applied to whatever base is
# loaded (the YAML scheme when present, otherwise the existing schema.json) so
# the generated artifact is the same either way.  They are kept here, next to
# the generator, because the corrections are campaign conventions rather than
# paper-coding rules.
# ---------------------------------------------------------------------------

# The analysis tab's Sample group is restricted to these fields; other fields
# that used to share group ``Sample`` are coding bookkeeping and are moved to
# ``Sample detail`` so they no longer render under Sample.
SAMPLE_GROUP = "Sample"
SAMPLE_DETAIL_GROUP = "Sample detail"
SAMPLE_GROUP_WHITELIST = {
    "N_Recruited", "N_Analyzed", "Age_Mean", "Age_Group", "Country",
    "International_Sample", "WEIRD_Western", "WEIRD_Educated",
    "WEIRD_Industrialized", "WEIRD_Rich", "WEIRD_Democratic", "Setting",
    "Gender_Ratio",
}

# Attention_Cues is an intervention-specific cue: render it in the
# intervention-specific group, not under Instruction & Learning.
ATTENTION_CUES_GROUP = "Intervention"

# Publication_Type + Publication_Category are merged into ONE ``Publication``
# moderator.  ``paper`` is split into peer-reviewed vs unreviewed.  This exact
# controlled vocabulary is mirrored in analysis_lib._PUBLICATION_VOCAB and
# published in Scripts/ANALYSIS_TAB_FIX.md so the data layer and the app agree.
PUBLICATION_VOCAB = [
    ("paper_peer_reviewed", "Paper (peer-reviewed)"),
    ("paper_unreviewed", "Paper (unreviewed)"),
    ("thesis", "Thesis"),
    ("dissertation", "Dissertation"),
    ("preprint", "Preprint"),
    ("working_paper", "Working paper"),
    ("conference_paper", "Conference paper"),
    ("unpublished_manuscript", "Unpublished manuscript"),
    ("report", "Report"),
]
PUBLICATION_VOCAB_KEY = "publication_types"

# contrast_type levels 8-13 live in the controlled vocabulary but were missing
# from the field option list, so the analysis tab showed raw numbers 8/10.
CONTRAST_TYPE_LABELS = {
    8: "Misinformation vs no misinformation",
    9: "Informative message vs control",
    10: "Factorial interaction",
    11: "Omnibus model term",
    12: "Moderator main effect",
    13: "Non-intervention factor",
}

# ---------------------------------------------------------------------------
# 2026-09-20 INTEGRITY session (FINAL_CURATION_AUDIT.md §4).
#
# (a) Retired labels.  The intervention-type and dv-domain vocabularies were
#     merged/cleaned on 2026-09-17/19 (see Scripts/domain_restriction.py,
#     Scripts/DV_DOMAIN_MERGE_LOG.md), but the generated schema still offered
#     the eliminated labels as selectable options.  They are filtered here so a
#     build from either the YAML scheme or the schema.json fallback can never
#     re-expose them.  The retired dv-domain labels remain resolvable at
#     analysis time (Scripts/domain_restriction.py OFF_VOCAB_MAP) for any
#     legacy coded value.
# (b) Added score_types.  Five descriptive score types actually used by coders
#     were missing from the controlled vocabulary.  They are ADDED (not
#     remapped to an existing value) so coder intent is preserved; downstream
#     code already recognises `signal_detection_other` as an SDT token.
# ---------------------------------------------------------------------------
RETIRED_INTERVENTION_TYPES = ("generic", "none")
RETIRED_DV_DOMAINS = ("belief_accuracy", "engagement_behavior", "affect", "confidence")
SCORE_TYPE_ADDITIONS = (
    "count",
    "single_item",
    "behavioral_intention",
    "change_score",
    "signal_detection_other",
)

# ---------------------------------------------------------------------------
# 2026-09-22 schema v3.1 (decision D8) — every coded variable carries its
# provenance.  ``source_location`` names where the statistic lives in the paper
# (``"Table 2, p. 7"`` | ``"Fig. 1"`` | ``"Sect. 3.2"`` | ``""``); ``source_quote``
# is the verbatim statistic from the paper.  Both are OPTIONAL in ``B_validate``
# and are checked for coverage in ``Scripts/validate_extraction.py`` (problem
# codes MISSING_SOURCE_LOCATION / MISSING_SOURCE_QUOTE / SOURCE_QUOTE_NOT_FOUND).
# The fields are added here so a rebuild from either the YAML scheme or the
# existing ``schema.json`` fallback always carries them.
# ---------------------------------------------------------------------------
SCHEMA_VERSION = "3.1"
SOURCE_FIELDS = [
    {
        "name": "source_location",
        "label": "Source Location",
        "group": "Other",
        "type": "string",
        "base_type": "string",
        "description": ("Where the statistic is reported in the paper, e.g. "
                        "\"Table 2, p. 7\", \"Fig. 1\", \"Sect. 3.2\"; empty "
                        "string when truly absent."),
        "required": False,
        "derived": False,
        "module": None,
        "editor": "text",
    },
    {
        "name": "source_quote",
        "label": "Source Quote",
        "group": "Other",
        "type": "string",
        "base_type": "string",
        "description": ("Verbatim statistic from the paper, e.g. "
                        "\"d = 0.31, 95% CI [0.12, 0.50]\"; must be findable "
                        "in Text/<Key>.txt after whitespace normalisation."),
        "required": False,
        "derived": False,
        "module": None,
        "editor": "text",
    },
]


def ensure_source_fields(payload):
    """Append the D8 ``source_location``/``source_quote`` DV fields when missing."""
    dv = payload.setdefault("dv_fields", [])
    names = {f.get("name") for f in dv}
    for field in SOURCE_FIELDS:
        if field["name"] not in names:
            dv.append(dict(field))
    payload["schema_version"] = SCHEMA_VERSION
    return payload


def _drop_retired(text, tokens):
    """Remove retired labels from a prose vocabulary list, tidying separators."""
    out = str(text or "")
    for tok in tokens:
        out = re.sub(r"\b" + re.escape(tok) + r"\b", "", out)
    out = re.sub(r"\s*,\s*,+", ",", out)
    out = re.sub(r",\s*\.", ".", out)
    out = re.sub(r"\s{2,}", " ", out)
    return out.strip()


def apply_campaign_overrides(payload):
    """Apply the analysis-tab schema corrections to a built payload in place."""
    common = payload.get("common_fields") or []
    dv = payload.get("dv_fields") or []

    # Corresponding author (name and address in SEPARATE fields) — the contact a
    # reader, reviewer or meta-analyst can reach about the paper.  Two fields,
    # not one blob: the name is searchable metadata, the address is a string
    # whose shape varies by publisher and is often absent from the deposit.
    if not any(f.get("name") == "Corresponding_Author_Name" for f in common):
        common.append({
            "name": "Corresponding_Author_Name",
            "label": "Corresponding author (name)",
            "group": "Metadata & Identification",
            "type": "string|null",
            "base_type": "string",
            "description": ("Name of the author the paper directs correspondence to, "
                            "e.g. \"Jane Roe\". Empty when the paper names no "
                            "corresponding author. Recorded separately from the "
                            "address in 'Corresponding_Author_Email'."),
            "required": False,
            "derived": False,
            "module": None,
            "editor": "text",
        })
    if not any(f.get("name") == "Corresponding_Author_Email" for f in common):
        common.append({
            "name": "Corresponding_Author_Email",
            "label": "Corresponding author (email)",
            "group": "Metadata & Identification",
            "type": "string|null",
            "base_type": "string",
            "description": ("E-mail address of the corresponding author, as printed "
                            "in the paper. Empty when no address is printed or none "
                            "could be resolved to the corresponding author."),
            "required": False,
            "derived": False,
            "module": None,
            "editor": "text",
        })
    if not any(f.get("name") == "Corresponding_Author_Source" for f in common):
        common.append({
            "name": "Corresponding_Author_Source",
            "label": "Corresponding author (source)",
            "group": "Metadata & Identification",
            "type": "string|null",
            "base_type": "string",
            "description": ("Where the corresponding author was read from, e.g. "
                            "\"pdf: correspondence note, p. 1\" or "
                            "\"openalex: corresponding_author_ids\"."),
            "required": False,
            "derived": False,
            "module": None,
            "editor": "text",
        })

    # Full-text inclusion criteria (C1-C4): recorded on EVERY coding, so a study that fails an
    # inclusion criterion is still fully coded / harmonized / reproduced as normal (see AGENTS.md).
    if not any(f.get("name") == "Fulltext_Exclusion_Criterion" for f in common):
        common.append({
            "name": "Fulltext_Exclusion_Criterion",
            "label": "Full-text exclusion criterion (C1-C4)",
            "group": "Metadata & Identification",
            "type": "multiselect",
            "base_type": "string",
            "description": ("Leave EMPTY when the study meets every inclusion criterion. Otherwise "
                            "list the unmet criterion/criteria: C1 = No misinformation intervention; "
                            "C2 = No control/comparison group; C3 = Not English; C4 = Not a primary "
                            "experimental study. A study that fails a criterion is STILL fully coded, "
                            "harmonized and reproduced as normal."),
            "required": False,
            "derived": False,
            "module": None,
            "editor": "multiselect",
            "options": [
                {"value": "C1", "label": "No misinformation intervention (C1)"},
                {"value": "C2", "label": "No control/comparison group (C2)"},
                {"value": "C3", "label": "Not English (C3)"},
                {"value": "C4", "label": "Not a primary experimental study (C4)"},
                # Three non-criterion buckets the screening layer also uses, so the coding and
                # the screening workbook share ONE canonical vocabulary (2026-10-02):
                {"value": "insufficient reporting",
                 "label": "insufficient reporting (study ran, but no eligible contrast is reported)"},
                {"value": "fulltext irretrievable",
                 "label": "fulltext irretrievable (no obtainable copy)"},
                {"value": "duplicate of an included study",
                 "label": "duplicate of an included study (non-independent report)"},
            ],
        })

    # The fallback source (Scripts/coding_web/schema.json) ALREADY contains this field, so the
    # append above is skipped on a rebuild -- re-assert the canonical vocabulary onto whichever
    # instance exists, otherwise the rebuild silently keeps the old option list.
    for _f in common:
        if _f.get("name") == "Fulltext_Exclusion_Criterion":
            _f["options"] = [
                {"value": "C1", "label": "No misinformation intervention (C1)"},
                {"value": "C2", "label": "No control/comparison group (C2)"},
                {"value": "C3", "label": "Not English (C3)"},
                {"value": "C4", "label": "Not a primary experimental study (C4)"},
                {"value": "insufficient reporting",
                 "label": "insufficient reporting (study ran, but no eligible contrast is reported)"},
                {"value": "fulltext irretrievable",
                 "label": "fulltext irretrievable (no obtainable copy)"},
                {"value": "duplicate of an included study",
                 "label": "duplicate of an included study (non-independent report)"},
            ]

    # Sample group whitelist + Attention_Cues group.
    for f in common:
        name = f.get("name")
        if name == "Attention_Cues":
            f["group"] = ATTENTION_CUES_GROUP
        if f.get("group") == SAMPLE_GROUP and name not in SAMPLE_GROUP_WHITELIST:
            f["group"] = SAMPLE_DETAIL_GROUP

    # contrast_type: complete the option list from the controlled vocabulary.
    for f in common + dv:
        if f.get("name") != "contrast_type":
            continue
        opts = {}
        for o in f.get("options") or []:
            try:
                opts[int(o["value"])] = str(o["label"])
            except (TypeError, ValueError, KeyError):
                continue
        for v, label in CONTRAST_TYPE_LABELS.items():
            opts.setdefault(v, label)
        f["options"] = [{"value": k, "label": opts[k]} for k in sorted(opts)]
        f["editor"] = "select"
        desc = str(f.get("description") or "")
        if "8=" not in desc:
            extra = ", ".join(f"{v}={label}" for v, label in sorted(CONTRAST_TYPE_LABELS.items()))
            f["description"] = desc.rstrip(". ") + ", " + extra + "."

    # Merged Publication field (single moderator; Publication_Type/Category are
    # excluded from the moderator catalogue in analysis_lib).
    pub = next((f for f in common if f.get("name") == "Publication"), None)
    if pub is None:
        pub = {
            "name": "Publication",
            "label": "Publication",
            "group": "Metadata & Identification",
            "type": "string|null",
            "base_type": "string",
            "description": ("Merged publication form (one moderator). Controlled "
                            "vocabulary: " + ", ".join(v for v, _ in PUBLICATION_VOCAB) +
                            ". Splits `paper` into peer-reviewed vs unreviewed."),
            "required": False,
            "derived": False,
            "module": None,
            "editor": "select",
        }
        idx = next((i for i, f in enumerate(common) if f.get("name") == "Year"), None)
        common.insert(idx + 1 if idx is not None else 0, pub)
    pub["options"] = [{"value": v, "label": label} for v, label in PUBLICATION_VOCAB]

    voc = payload.setdefault("vocabularies", {})
    voc[PUBLICATION_VOCAB_KEY] = [v for v, _ in PUBLICATION_VOCAB]
    voc["publication_type_labels"] = {v: label for v, label in PUBLICATION_VOCAB}

    # --- retired intervention types (generic, none) --------------------
    itypes = [t for t in (voc.get("intervention_types") or [])
              if t not in RETIRED_INTERVENTION_TYPES]
    voc["intervention_types"] = itypes
    voc["intervention_types_requiring_detail"] = [
        t for t in (voc.get("intervention_types_requiring_detail") or [])
        if t not in RETIRED_INTERVENTION_TYPES
    ]
    itm = payload.get("intervention_type_modules") or {}
    for _t in RETIRED_INTERVENTION_TYPES:
        itm.pop(_t, None)
    # The `generic` MODULE is kept: it is the routing container for the
    # still-valid `other` type (55 `other`-only records carry its fields), but
    # the retired `generic` type is dropped from its type list.
    for _mod in (payload.get("modules") or {}).values():
        if _mod.get("intervention_types"):
            _mod["intervention_types"] = [
                t for t in _mod["intervention_types"]
                if t not in RETIRED_INTERVENTION_TYPES
            ]

    # --- retired dv domains --------------------------------------------
    ddom = [d for d in (voc.get("dv_domains") or [])
            if d not in RETIRED_DV_DOMAINS]
    voc["dv_domains"] = ddom

    # --- added score_types ---------------------------------------------
    stypes = list(voc.get("score_types") or [])
    for _v in SCORE_TYPE_ADDITIONS:
        if _v not in stypes:
            stypes.append(_v)
    voc["score_types"] = stypes

    # --- propagate to the field option lists / prose --------------------
    for f in common:
        if f.get("name") == "Intervention_Type":
            f["vocab"] = list(itypes)
            if "options" in f:
                f["options"] = [{"value": t, "label": t} for t in itypes]
            if f.get("description"):
                f["description"] = _drop_retired(
                    f["description"], RETIRED_INTERVENTION_TYPES)
    for f in common + dv:
        if f.get("name") == "dv_domain" and "vocab" in f:
            f["vocab"] = list(ddom)
        if f.get("name") == "score_types":
            if "vocab" in f:
                f["vocab"] = list(stypes)
            f["description"] = ("Controlled multi-label list: " +
                                ", ".join(stypes) + ".")

    # ------------------------------------------------------------------
    # Follow-up moderator-collapse vocabularies (2026-09-19, PI-approved).
    # Media_Support and Intervention_Components are collapsed to controlled
    # sets; Technique_Taxonomy gains two genuine labels.  The values are
    # mirrored from EXTRA_VOCABS so the fallback (schema.json) build stays in
    # step with a YAML build.
    # ------------------------------------------------------------------
    voc["media_support"] = list(EXTRA_VOCABS["media_support"])
    voc["intervention_components"] = list(EXTRA_VOCABS["intervention_components"])
    voc["technique_taxonomy"] = list(EXTRA_VOCABS["technique_taxonomy"])
    for f in common:
        if f.get("name") == "Media_Support":
            f["vocab"] = list(EXTRA_VOCABS["media_support"])
        if f.get("name") == "Technique_Taxonomy":
            f["vocab"] = list(EXTRA_VOCABS["technique_taxonomy"])
    for _mod in (payload.get("modules") or {}).values():
        for f in (_mod.get("fields") or []):
            if f.get("name") == "Intervention_Components":
                f["vocab"] = list(EXTRA_VOCABS["intervention_components"])
            if f.get("name") == "Technique_Taxonomy":
                f["vocab"] = list(EXTRA_VOCABS["technique_taxonomy"])

    # ------------------------------------------------------------------
    # Module-scoped numeric enum fields share one NAME but carry a different
    # controlled vocabulary per intervention module (e.g. Correction_Source is
    # 1-7 in debunking_and_rebuttals but 1-5 in warning_and_fact_checking_labels).
    # Record every per-module option list so a flattened consumer can name each
    # code instead of showing a bare number (see DATA_QUALITY_PASS.md item 1).
    # ------------------------------------------------------------------
    module_levels = {}
    for _mname, _mod in (payload.get("modules") or {}).items():
        for f in (_mod.get("fields") or []):
            _n = f.get("name")
            if not _n:
                continue
            opts = f.get("options") or []
            if not opts:
                continue
            module_levels.setdefault(_n, {})[_mname] = {
                str(o.get("value")): o.get("label") for o in opts
            }
    # keep only genuinely module-scoped (declared in >1 module) fields
    voc["module_field_levels"] = {
        k: v for k, v in module_levels.items() if len(v) > 1
    }

    # Keep the group order list consistent with the field groups actually used.
    groups = payload.setdefault("groups", [])
    if SAMPLE_DETAIL_GROUP not in groups:
        if SAMPLE_GROUP in groups:
            groups.insert(groups.index(SAMPLE_GROUP) + 1, SAMPLE_DETAIL_GROUP)
        else:
            groups.append(SAMPLE_DETAIL_GROUP)
    for g in (SAMPLE_DETAIL_GROUP, ATTENTION_CUES_GROUP):
        if g not in groups:
            groups.append(g)

    # Schema v3.1 (D8): every coded variable carries its provenance.
    ensure_source_fields(payload)
    return payload


def find_scheme_dir():
    """Return the coding_schemes directory, or None when it is unavailable.

    The archived YAML scheme is frequently moved out of the working tree during
    housekeeping.  When it cannot be found the generator falls back to the
    existing ``schema.json`` as its base (see ``main``) so the app can still be
    rebuilt with the campaign overrides applied.
    """
    for cand in SCHEME_CANDIDATES:
        if os.path.isdir(cand) and os.path.exists(os.path.join(cand, "common.yaml")):
            return cand
    return None


NULL_CLAUSE_RE = re.compile(r"\.?\s*Null\s+(?:if|when)\s+[^.]*\.?", re.IGNORECASE)
SEMI_CLAUSE_RE = re.compile(r",?\s*null\s*=\s*[^,;.]+", re.IGNORECASE)


def _clean_label(text):
    """Trim prose scaffolding so dropdown options read as plain level names."""
    text = SEMI_CLAUSE_RE.sub("", text)
    text = NULL_CLAUSE_RE.sub("", text)
    text = text.strip()
    if text.count("(") > text.count(")"):
        text = text[: text.rindex("(")].strip()
    text = text.strip(" ,;.")
    while text.endswith(")") and text.count(")") > text.count("("):
        text = text[:-1].strip(" ,;.")
    return text.strip()


def enum_from_description(desc):
    """Parse ``1=Foo, 2=Bar`` style enumerations out of a field description."""
    if not desc:
        return None
    matches = list(NUM_RE.finditer(desc))
    if len(matches) < 2:
        return None
    levels = {}
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(desc)
        label = _clean_label(desc[start:end])
        if label:
            levels[m.group(1)] = label
    return levels if len(levels) >= 2 else None


def base_type(t):
    return (t or "string").split("|")[0].strip()


def editor_for(field, vocab, description):
    name = field["name"]
    bt = base_type(field.get("type"))
    if name in VOCAB_FOR_FIELD:
        return "select"
    if name in OBJECTLIST_FIELDS:
        return "objectlist"
    if name == "Gender_Ratio":
        return "kv"
    if name == "conversion_inputs":
        return "json"
    if bt in ("integer", "number"):
        if enum_from_description(description):
            return "select"
        return "number"
    if bt == "list":
        return "multiselect" if name in MULTISELECT_VOCABS else "chips"
    if bt == "object":
        return "json"
    return "textarea" if name in TEXTAREA_FIELDS else "text"


def make_field(raw, vocabularies, module=None):
    name = raw.get("name")
    desc = (raw.get("description") or "").strip()
    field = {
        "name": name,
        "label": raw.get("label") or name,
        "group": raw.get("group") or "Other",
        "type": raw.get("type") or "string",
        "base_type": base_type(raw.get("type")),
        "description": desc,
        "required": bool(raw.get("required")),
        "derived": bool(raw.get("derived")),
        "module": module,
    }
    field["editor"] = editor_for(field, vocabularies, desc)
    enum = enum_from_description(desc)
    if field["editor"] == "select" and name in VOCAB_FOR_FIELD:
        options = vocabularies.get(VOCAB_FOR_FIELD[name], [])
        field["options"] = [{"value": v, "label": v} for v in options]
    elif field["editor"] == "select" and enum:
        items = sorted(enum.items(), key=lambda kv: int(kv[0]))
        field["options"] = [{"value": int(k), "label": v} for k, v in items]
    elif enum and field["editor"] in ("multiselect", "chips", "number", "text"):
        field["hint_levels"] = {k: v for k, v in enum.items()}
    if field["editor"] == "multiselect":
        key = MULTISELECT_VOCABS.get(name)
        field["vocab"] = vocabularies.get(key, []) if key else []
    if name in OBJECTLIST_FIELDS:
        field["columns"] = [{"name": c, "type": t} for c, t in OBJECTLIST_FIELDS[name]]
    return field


def _build_from_scheme(scheme):
    """Build the flat schema payload from the YAML coding scheme."""
    with open(os.path.join(scheme, "schema.yaml"), encoding="utf-8") as fh:
        root = yaml.safe_load(fh)
    with open(os.path.join(scheme, "common.yaml"), encoding="utf-8") as fh:
        common = yaml.safe_load(fh)

    vocabularies = dict(root.get("controlled_vocabularies") or {})
    vocabularies.update(EXTRA_VOCABS)
    vocabularies["modules"] = [m for m in (root.get("files", {}).get("modules") or {}) if m != "none"]
    vocabularies["effect_size_types"] = [
        "d", "g", "eta2p", "beta", "odds_ratio", "r", "means", "none", "dprime", "c", "other",
    ]
    vocabularies["contrast_types"] = {str(k): v for k, v in
                                      (vocabularies.get("contrast_types") or {}).items()}

    # 2026-09-19 (PI decision): `belief_accuracy` is merged into `credibility`, so
    # it must never be offered as a selectable dv_domain.  The archived schema.yaml
    # still lists the legacy label, so it is filtered out here (see
    # Scripts/domain_restriction.py and Scripts/DV_DOMAIN_MERGE_LOG.md).
    MERGED_DOMAIN_LABELS = {"belief_accuracy": "credibility"}
    _doms = vocabularies.get("dv_domains")
    if isinstance(_doms, list):
        vocabularies["dv_domains"] = [d for d in _doms if d not in MERGED_DOMAIN_LABELS]

    common_fields = [make_field(f, vocabularies) for f in common.get("study_variables", [])]

    modules = {}
    for name, filename in (root.get("files", {}).get("modules") or {}).items():
        if not filename:
            continue
        path = os.path.join(scheme, filename)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        modules[name] = {
            "label": doc.get("label", name),
            "family": doc.get("intervention_family"),
            "intervention_types": doc.get("intervention_types") or [],
            "description": (doc.get("description") or "").strip(),
            "fields": [make_field(f, vocabularies, module=name)
                       for f in (doc.get("study_variables") or [])],
        }

    dv_fields = []
    for raw in (common.get("effect_sizes") or {}).get("fields", []):
        f = make_field(raw, vocabularies)
        f["group"] = f.get("group") or "DV"
        dv_fields.append(f)
    # Keep the selectable vocabulary and its description consistent with the merge.
    for _f in dv_fields:
        if _f.get("name") == "dv_domain" and _f.get("description"):
            _d = _f["description"]
            for _old in MERGED_DOMAIN_LABELS:
                _d = _d.replace(_old + ", ", "").replace(", " + _old, "")
            _f["description"] = _d

    groups = []
    for f in common_fields + dv_fields:
        if f["group"] not in groups:
            groups.append(f["group"])

    return {
        "schema_version": root.get("schema_version", "3.0"),
        "generated_from": os.path.relpath(scheme, B).replace("\\", "/"),
        "groups": groups,
        "common_fields": common_fields,
        "dv_fields": dv_fields,
        "modules": modules,
        "intervention_type_modules": root.get("intervention_type_modules") or {},
        "vocabularies": vocabularies,
    }


def main():
    scheme = find_scheme_dir()
    if scheme:
        payload = _build_from_scheme(scheme)
    else:
        # The YAML scheme may be archived out of the working tree.  Fall back to
        # the existing generated schema.json so the build stays reproducible and
        # the campaign overrides are still applied.
        try:
            with open(OUT, encoding="utf-8") as fh:
                payload = json.load(fh)
        except FileNotFoundError:
            raise SystemExit("coding_schemes directory not found and no schema.json to update")
        print(f"coding_schemes not found; regenerating from {OUT}")

    payload = apply_campaign_overrides(payload)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)

    common_fields = payload.get("common_fields") or []
    dv_fields = payload.get("dv_fields") or []
    modules = payload.get("modules") or {}
    total = len(common_fields) + len(dv_fields) + sum(len(m.get("fields") or []) for m in modules.values())
    print(f"wrote {OUT}")
    print(f"  common={len(common_fields)} dv={len(dv_fields)} modules={len(modules)} "
          f"module_fields={total - len(common_fields) - len(dv_fields)}")
    print(f"  source={scheme or 'schema.json (fallback) + campaign overrides'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
