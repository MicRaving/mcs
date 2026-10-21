"""The coding scheme library: one place that knows what a scheme is.

A *scheme* is the user-editable description of what a coder records.  It is the
successor of the hardcoded misinformation schema (``Scripts/coding_web/schema.json``,
v3.1) and is deliberately *not* tied to that campaign: a scheme is a list of
blocks, each block holds fields, each field has a type, an editor, a controlled
**role**, and an optional condition.

The module is stdlib-only and lives one level up (``Scripts/``) so the desktop
app, the analysis pipeline and the agent tooling can all import it.

Layout of a scheme document::

    {
      "scheme_version": 3,
      "id": "misinformation",
      "name": "misinformation-interventions",
      "label": "Misinformation Interventions",
      "description": "...",
      "groups":  [{"name": ..., "label": ..., "show_if": ...}, ...],
      "fields":  [ <field>, ... ],          # study / experiment level
      "metrics": {"enabled": bool, "group": str, "label": str,
                  "fields": [ <field>, ... ], "layout": {...}},
      "modules": {<key>: {"label": ..., "fields": [ <field>, ... ], ...}},
      "vocabularies": {...},
      "kinds":    [ {"id": ..., "label": ..., ...}, ... ],
      "rules":    {...},
    }

A *field* is::

    {name, label, group, scope, type, editor, options, columns, role,
     required, derived, moderator, description, instruction, show_if,
     required_if, module, module_options, source, coder, kind}

The **role** is the load-bearing part: the pipeline asks "what is the value of
the ``eligible_primary`` field?" and never asks "what is the value of
``eligible_primary``?"  Field names are user data; roles are the contract.
"""

from __future__ import annotations

import re

#: Scheme format version.  ``3`` = codebook v2 plus the conditions/kinds/rules
#: extensions; ``2`` = QCnext's codebook v2 (accepted on read, upgraded).
SCHEME_VERSION = 3

#: Value types a field may hold.
FIELD_TYPES = ("string", "integer", "number", "boolean", "list", "object", "date")

#: Widgets a field may be edited with.  ``module-select`` is synthetic: it picks
#: the module a record activates.
EDITORS = (
    "text", "textarea", "number", "boolean", "select", "multiselect",
    "date", "json", "objectlist", "kv", "chips", "module-select",
)

#: Where a field lives.
SCOPES = ("study", "metric", "module")

#: Operators a condition may use.
CONDITION_OPS = (
    "in", "not_in", "equals", "not_equals", "not_empty", "empty",
    "gt", "gte", "lt", "lte", "contains",
)

#: Effect-size families a metrics block may offer.
EFFECT_FAMILIES = (
    "descriptives", "t", "F", "r", "yi", "d_g", "odds_ratio", "risk_ratio",
    "beta", "eta2p", "chi2",
)

#: Default group name for the repeatable per-contrast (metric) fields.
DEFAULT_METRICS_GROUP = "DV_Metrics"

#: Default group for a metric field when the scheme does not name one.
DEFAULT_GROUP = "Other"


# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #

#: Roles a study/experiment-level field may carry.
STUDY_ROLES = (
    "study_id", "parent_study_id", "year", "doi", "authors", "title",
    "description", "notes", "publication",
    "sample_n", "n_recruited", "n_reported_per_condition", "n_inference_method",
    "age_mean", "age_sd", "age_reported_as", "age_group",
    "gender_ratio", "gender_reported_as", "covariates",
    "country", "sample_region", "international_sample",
    "weird_western", "weird_educated", "weird_industrialized", "weird_rich",
    "weird_democratic", "setting",
    "design", "design_factors", "factor_levels", "unit_of_analysis",
    "clustering", "control_group_type", "n_arms", "fully_within",
    "intervention_type", "intervention_detail", "intervention_topic",
    "intervention_duration", "modality", "exposure_schedule",
    "pedagogy_type", "instructor_type", "feedback_mechanics",
    "attention_cues", "affective_design", "media_support", "comm_strategies",
    "icap_role", "social_context", "retrieval_practice", "contrasting_cases",
    "concept_labeling", "interleaving", "practice_strategy", "self_efficacy",
    "synthetic", "stimuli", "stimuli_topic",
    "dataset_url", "data_repository", "dataset_availability", "data_available",
    "code_available", "preregistration_status", "preregistration_url",
    "ethics", "consent_reported",
    "fulltext_exclusion_criterion",
    "corresponding_author_name", "corresponding_author_email",
    "corresponding_author_source",
)

#: Roles a per-contrast (metric) field may carry.
METRIC_ROLES = (
    "dv_name", "dv_domain", "score_types", "discernment", "polarity",
    "veracity", "scale_type", "scale_points", "scale_min", "scale_max",
    "contrast_type", "contrast_role", "eligible_primary",
    "comparison_label", "group1_label", "group2_label",
    "timepoint_1", "timepoint_2", "posttest_type", "delay_weeks",
    "n1", "n2", "n_total", "m1", "m2", "sd1", "sd2",
    "n1_pre", "m1_pre", "sd1_pre", "n2_pre", "m2_pre", "sd2_pre",
    "n1_post", "m1_post", "sd1_post", "n2_post", "m2_post", "sd2_post",
    "t", "df_num", "df_den", "F", "chi2", "z", "r", "p", "p_censored",
    "yi", "vi", "sei",
    "d", "g", "d_se", "g_se", "d_ci_low", "d_ci_high", "g_ci_low", "g_ci_high",
    "or", "or_ci_low", "or_ci_high", "log_odds",
    "rr", "rr_ci_low", "rr_ci_high",
    "beta", "beta_se", "beta_ci_low", "beta_ci_high",
    "eta2p", "eta2p_ci_low", "eta2p_ci_high",
    "effect_type_reported", "effect_favors", "effect_direction_meaning",
    "conversion_method", "conversion_inputs", "cell_descriptives",
    "missing_reason", "source_location", "source_quote",
)

#: Every recognised role.
ALL_ROLES = frozenset(STUDY_ROLES) | frozenset(METRIC_ROLES)

#: v3.1 field name -> role.  One-to-one by construction: the scheme validator
#: rejects a name that maps to two roles, so the ``N`` / ``n`` collision class
#: that QCnext's alias table has cannot occur here.
LEGACY_ROLE_ALIASES = {
    # -- study level ------------------------------------------------------- #
    "Study_ID": "study_id",
    "Parent_Study_ID": "parent_study_id",
    "Year": "year",
    "Publication": "publication",
    "DOI": "doi",
    "Authors": "authors",
    "Title": "title",
    "Study_Description": "description",
    "Rationale_Notes": "notes",
    "N_Recruited": "n_recruited",
    "N_Analyzed": "sample_n",
    "N_Reported_Per_Condition": "n_reported_per_condition",
    "N_Inference_Method": "n_inference_method",
    "Age_Mean": "age_mean",
    "Age_SD": "age_sd",
    "Age_Reported_As": "age_reported_as",
    "Age_Group": "age_group",
    "Gender_Ratio": "gender_ratio",
    "Gender_Reported_As": "gender_reported_as",
    "Covariates": "covariates",
    "Country": "country",
    "Sample_Region": "sample_region",
    "International_Sample": "international_sample",
    "WEIRD_Western": "weird_western",
    "WEIRD_Educated": "weird_educated",
    "WEIRD_Industrialized": "weird_industrialized",
    "WEIRD_Rich": "weird_rich",
    "WEIRD_Democratic": "weird_democratic",
    "Setting": "setting",
    "dataset_availability_status": "dataset_availability",
    "data_url": "dataset_url",
    "data_repository": "data_repository",
    "data_available": "data_available",
    "code_available": "code_available",
    "preregistration_status": "preregistration_status",
    "preregistration_url": "preregistration_url",
    "ethics": "ethics",
    "consent_reported": "consent_reported",
    "n_arms": "n_arms",
    "design_factors": "design_factors",
    "factor_levels": "factor_levels",
    "within_between": "design",
    "unit_of_analysis": "unit_of_analysis",
    "clustering": "clustering",
    "control_group_type": "control_group_type",
    "Intervention_Type": "intervention_type",
    "Intervention_Type_Detail": "intervention_detail",
    "Intervention_Topic": "intervention_topic",
    "Intervention_Duration": "intervention_duration",
    "Modality": "modality",
    "Exposure_Schedule": "exposure_schedule",
    "Pedagogy_Type": "pedagogy_type",
    "Instructor_Type": "instructor_type",
    "Feedback_Mechanics": "feedback_mechanics",
    "Attention_Cues": "attention_cues",
    "Affective_Design": "affective_design",
    "Media_Support": "media_support",
    "Comm_Strategies": "comm_strategies",
    "ICAP_Role": "icap_role",
    "Social_Context": "social_context",
    "Retrieval_Practice": "retrieval_practice",
    "Contrasting_Cases": "contrasting_cases",
    "Concept_Labeling": "concept_labeling",
    "Interleaving": "interleaving",
    "Practice_Strategy": "practice_strategy",
    "Self_Efficacy": "self_efficacy",
    "Synthetic": "synthetic",
    "Stimuli": "stimuli",
    "Stimuli_Topic": "stimuli_topic",
    "Fulltext_Exclusion_Criterion": "fulltext_exclusion_criterion",
    "Corresponding_Author_Name": "corresponding_author_name",
    "Corresponding_Author_Email": "corresponding_author_email",
    "Corresponding_Author_Source": "corresponding_author_source",
    # -- metric level ------------------------------------------------------ #
    "dv_name": "dv_name",
    "dv_domain": "dv_domain",
    "score_types": "score_types",
    "discernment_defined_as": "discernment",
    "dv_polarity": "polarity",
    "scale_type": "scale_type",
    "scale_points": "scale_points",
    "scale_min": "scale_min",
    "scale_max": "scale_max",
    "comparison_label": "comparison_label",
    "group1_label": "group1_label",
    "group2_label": "group2_label",
    "contrast_type": "contrast_type",
    "eligible_primary": "eligible_primary",
    "timepoint_1": "timepoint_1",
    "timepoint_2": "timepoint_2",
    "posttest_type": "posttest_type",
    "delay_weeks": "delay_weeks",
    "n1i_pre": "n1_pre", "m1i_pre": "m1_pre", "sd1i_pre": "sd1_pre",
    "n2i_pre": "n2_pre", "m2i_pre": "m2_pre", "sd2i_pre": "sd2_pre",
    "n1i_post": "n1_post", "m1i_post": "m1_post", "sd1i_post": "sd1_post",
    "n2i_post": "n2_post", "m2i_post": "m2_post", "sd2i_post": "sd2_post",
    "n1i": "n1", "m1i": "m1", "sd1i": "sd1",
    "n2i": "n2", "m2i": "m2", "sd2i": "sd2",
    "t_val": "t", "F_val": "F", "df_num": "df_num", "df_den": "df_den",
    "z_val": "z", "chi2_val": "chi2", "r_val": "r", "p_val": "p",
    "p_censored": "p_censored", "n_total": "n_total",
    "beta_val": "beta", "beta_se": "beta_se",
    "log_odds": "log_odds", "odds_ratio": "or",
    "or_ci_low": "or_ci_low", "or_ci_high": "or_ci_high", "risk_ratio": "rr",
    "eta2_p": "eta2p", "eta2_p_ci_low": "eta2p_ci_low",
    "eta2_p_ci_high": "eta2p_ci_high",
    "d_val": "d", "g_val": "g",
    "d_ci_low": "d_ci_low", "d_ci_high": "d_ci_high",
    "g_ci_low": "g_ci_low", "g_ci_high": "g_ci_high",
    "d_se": "d_se", "g_se": "g_se",
    "yi": "yi", "vi": "vi", "sei": "sei",
    "effect_size_type_reported": "effect_type_reported",
    "effect_favors": "effect_favors",
    "effect_direction_meaning": "effect_direction_meaning",
    "conversion_method": "conversion_method",
    "conversion_inputs": "conversion_inputs",
    "cell_descriptives": "cell_descriptives",
    "missing_metrics_reason": "missing_reason",
    "source_location": "source_location",
    "source_quote": "source_quote",
}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def slugify(text, prefix="f_"):
    """``"Sample size (N)"`` -> ``"sample_size_n"``; a digit lead gets ``prefix``."""
    slug = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    if not slug:
        return ""
    if slug[0].isdigit():
        slug = prefix + slug
    return slug


def _as_text(value, default=""):
    if value is None:
        return default
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return default


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def normalise_role(role):
    """Return a known role id, or ``None``."""
    if not isinstance(role, str):
        return None
    role = role.strip()
    return role if role in ALL_ROLES else None


def normalise_options(raw):
    """Normalise an option list to ``[{"value": ..., "label": ...}, ...]``.

    Accepts plain strings, numbers and ``{"value":…, "label":…}`` objects.
    """
    out = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, dict):
            value = item.get("value")
            label = _as_text(item.get("label"), None)
            if label is None:
                label = _as_text(value)
            if value is None and not label:
                continue
            out.append({"value": value, "label": label})
        elif isinstance(item, (str, int, float)) and not isinstance(item, bool):
            text = _as_text(item)
            if not text:
                continue
            out.append({"value": item, "label": text})
    return out


def option_values(options):
    """The raw values of a normalised option list."""
    return [o.get("value") for o in (options or [])]


def option_labels(options):
    """``{value: label}`` for a normalised option list."""
    return {o.get("value"): o.get("label") for o in (options or [])}


# --------------------------------------------------------------------------- #
# Conditions
# --------------------------------------------------------------------------- #

def normalise_condition(raw):
    """Normalise a condition to ``{"field", "op", "value"}`` or ``None``.

    A malformed condition never raises; it becomes ``None`` so the caller
    treats the field as always visible (fail-open).
    """
    if not isinstance(raw, dict):
        return None
    field = _as_text(raw.get("field")).strip()
    op = _as_text(raw.get("op")).strip().lower()
    if not field or op not in CONDITION_OPS:
        return None
    value = raw.get("value")
    if op in ("not_empty", "empty"):
        value = None
    elif isinstance(value, (list, tuple)):
        value = [v for v in value]
    elif value is not None and not isinstance(value, (int, float, bool)):
        value = _as_text(value)
    return {"field": field, "op": op, "value": value}


def _value_text(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    return ""


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _as_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def eval_condition(condition, values):
    """Evaluate a condition against a ``{field_name: value}`` mapping.

    Fail-open: a missing reference, an unusable value or a malformed condition
    evaluates to ``True`` (visible).  A condition must never hide data.
    """
    cond = normalise_condition(condition)
    if cond is None:
        return True
    raw = (values or {}).get(cond["field"])
    op = cond["op"]
    if raw is None:
        # The reference field carries no value: cannot decide -> show.
        return True
    if op == "not_empty":
        if isinstance(raw, (list, tuple, set, dict)):
            return len(raw) > 0
        return _value_text(raw) != ""
    if op == "empty":
        if isinstance(raw, (list, tuple, set, dict)):
            return len(raw) == 0
        return _value_text(raw) == ""
    if op in ("gt", "gte", "lt", "lte"):
        left = _as_number(raw)
        right = _as_number(cond.get("value"))
        if left is None or right is None:
            return True
        if op == "gt":
            return left > right
        if op == "gte":
            return left >= right
        if op == "lt":
            return left < right
        return left <= right
    texts = [t.lower() for t in (_value_text(v) for v in _as_list(raw)) if t != ""]
    if op == "in":
        wanted = {_value_text(v).lower() for v in _as_list(cond.get("value"))}
        return any(t in wanted for t in texts)
    if op == "not_in":
        wanted = {_value_text(v).lower() for v in _as_list(cond.get("value"))}
        return not any(t in wanted for t in texts)
    if op == "equals":
        wanted = {_value_text(cond.get("value")).lower()}
        return any(t in wanted for t in texts)
    if op == "not_equals":
        wanted = {_value_text(cond.get("value")).lower()}
        return not any(t in wanted for t in texts)
    if op == "contains":
        needle = _value_text(cond.get("value")).lower()
        if not needle:
            return True
        return any(needle in t for t in texts)
    return True


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #

def normalise_groups(raw):
    """Normalise the block list to ``[{"name", "label", "show_if"}, ...]``."""
    out = []
    if not isinstance(raw, list):
        return out
    for group in raw:
        if isinstance(group, dict):
            name = _as_text(group.get("name")).strip()
            if not name:
                continue
            out.append({
                "name": name,
                "label": _as_text(group.get("label"), name) or name,
                "description": _as_text(group.get("description")),
                "show_if": normalise_condition(group.get("show_if")),
            })
        elif isinstance(group, str) and group.strip():
            name = group.strip()
            out.append({"name": name, "label": name, "description": "",
                        "show_if": None})
    return out


def normalise_columns(raw):
    """Normalise an objectlist's sub-columns to ``[{"name", "type"}, ...]``."""
    out = []
    if not isinstance(raw, list):
        return out
    for col in raw:
        if not isinstance(col, dict):
            continue
        name = _as_text(col.get("name")).strip()
        if not name:
            continue
        ctype = _as_text(col.get("type"), "string") or "string"
        out.append({"name": name, "type": ctype})
    return out


def normalise_kind(raw):
    """Normalise a user-defined variable type (``kind``)."""
    if not isinstance(raw, dict):
        return None
    kind_id = slugify(_as_text(raw.get("id") or raw.get("label")))
    if not kind_id:
        return None
    return {
        "id": kind_id,
        "label": _as_text(raw.get("label"), kind_id) or kind_id,
        "base_type": _as_text(raw.get("base_type"), "string") or "string",
        "editor": _as_text(raw.get("editor"), "text") or "text",
        "description": _as_text(raw.get("description")),
        "options": normalise_options(raw.get("options")),
        "columns": normalise_columns(raw.get("columns")),
        "role": normalise_role(raw.get("role")),
    }


def normalise_field(raw, fallback_group=DEFAULT_GROUP, scope="study"):
    """Normalise one field, filling every key the rest of the code relies on."""
    if not isinstance(raw, dict):
        return None
    name = _as_text(raw.get("name")).strip()
    if not name:
        return None
    label = _as_text(raw.get("label"), name) or name
    field_type = _as_text(raw.get("type"), "string") or "string"
    if field_type not in FIELD_TYPES:
        base = field_type.split("|")[0].strip()
        field_type = base if base in FIELD_TYPES else "string"
    editor = _as_text(raw.get("editor")).strip().lower()
    if editor not in EDITORS:
        editor = default_editor(field_type)
    options = normalise_options(raw.get("options"))
    vocab = [v for v in (_as_text(v) for v in (raw.get("vocab") or [])) if v]
    if not options and vocab and editor in ("multiselect", "chips"):
        options = normalise_options(vocab)
    role = normalise_role(raw.get("role"))
    if role is None:
        role = LEGACY_ROLE_ALIASES.get(name)
    field = {
        "name": name,
        "label": label,
        "group": _as_text(raw.get("group"), fallback_group) or fallback_group,
        "scope": scope,
        "type": field_type,
        "editor": editor,
        "options": options,
        "vocab": vocab,
        "columns": normalise_columns(raw.get("columns")),
        "role": role,
        "required": _as_bool(raw.get("required")),
        "derived": _as_bool(raw.get("derived")),
        "moderator": _as_bool(raw.get("moderator"), True),
        "description": _as_text(raw.get("description")),
        "instruction": _as_text(raw.get("instruction")),
        "show_if": normalise_condition(raw.get("show_if")),
        "required_if": normalise_condition(raw.get("required_if")),
        "module": _as_text(raw.get("module")) or None,
        "module_options": raw.get("module_options") if isinstance(raw.get("module_options"), dict) else {},
        "kind": _as_text(raw.get("kind")) or None,
        "source": raw.get("source") if isinstance(raw.get("source"), dict) else {},
        "coder": raw.get("coder") if isinstance(raw.get("coder"), dict) else {},
    }
    return field


def default_editor(field_type):
    """The widget a type gets when the scheme does not name one."""
    return {
        "integer": "number", "number": "number", "boolean": "boolean",
        "list": "multiselect", "object": "json", "date": "date",
    }.get(field_type, "text")


def normalise_scheme(raw):
    """Upgrade any accepted scheme document to the canonical shape.

    Accepts this module's own output, QCnext's codebook v2 and the legacy
    v3.1 ``schema.json`` (whose ``common_fields``/``dv_fields``/``modules``
    layout is recognised by its keys).
    """
    book = raw if isinstance(raw, dict) else {}

    groups = normalise_groups(book.get("groups"))

    fields = []
    for entry in book.get("fields") or []:
        field = normalise_field(entry, groups[0]["name"] if groups else "Study", "study")
        if field:
            fields.append(field)
    # QCnext v1 alias.
    if not fields:
        for entry in book.get("study_fields") or []:
            field = normalise_field(entry, groups[0]["name"] if groups else "Study", "study")
            if field:
                fields.append(field)

    metrics_raw = book.get("metrics") if isinstance(book.get("metrics"), dict) else {}
    metrics_group = _as_text(metrics_raw.get("group"), DEFAULT_METRICS_GROUP) or DEFAULT_METRICS_GROUP
    metrics_label = _as_text(metrics_raw.get("label"), "Effect sizes") or "Effect sizes"
    metric_fields = []
    for entry in metrics_raw.get("fields") or []:
        field = normalise_field(entry, metrics_group, "metric")
        if field:
            metric_fields.append(field)

    modules = {}
    for key, mod in (book.get("modules") or {}).items():
        if not isinstance(mod, dict):
            continue
        mod_fields = []
        for entry in mod.get("fields") or []:
            field = normalise_field(entry, _as_text(mod.get("label"), key) or key, "module")
            if field:
                field["module"] = field.get("module") or key
                mod_fields.append(field)
        modules[str(key)] = {
            "key": str(key),
            "label": _as_text(mod.get("label"), str(key)) or str(key),
            "family": _as_text(mod.get("family")) or None,
            "description": _as_text(mod.get("description")),
            "intervention_types": [t for t in (mod.get("intervention_types") or []) if t],
            "show_if": normalise_condition(mod.get("show_if")),
            "fields": mod_fields,
        }

    kinds = []
    for entry in book.get("kinds") or []:
        kind = normalise_kind(entry)
        if kind:
            kinds.append(kind)

    layout = metrics_raw.get("layout") if isinstance(metrics_raw.get("layout"), dict) else {}

    scheme = {
        "scheme_version": SCHEME_VERSION,
        "id": _as_text(book.get("id"), "scheme") or "scheme",
        "name": _as_text(book.get("name"), "scheme") or "scheme",
        "label": _as_text(book.get("label")) or _as_text(book.get("name"), "scheme"),
        "description": _as_text(book.get("description")),
        "prompts": book.get("prompts") if isinstance(book.get("prompts"), dict) else {},
        "groups": groups,
        "fields": fields,
        "metrics": {
            "enabled": _as_bool(metrics_raw.get("enabled"), bool(metric_fields)),
            "group": metrics_group,
            "label": metrics_label,
            "fields": metric_fields,
            "layout": layout,
        },
        "modules": modules,
        "intervention_type_modules": (
            {str(k): [str(m) for m in v] for k, v in
             (book.get("intervention_type_modules") or {}).items() if isinstance(v, list)}
        ),
        "vocabularies": book.get("vocabularies") if isinstance(book.get("vocabularies"), dict) else {},
        "effect_families": [f for f in (book.get("effect_families") or []) if f in EFFECT_FAMILIES],
        "kinds": kinds,
        "rules": book.get("rules") if isinstance(book.get("rules"), dict) else {},
    }
    return apply_kinds(scheme)


def apply_kinds(scheme):
    """Let a field inherit from the kind it names (and fill what it lacks)."""
    kinds = {k["id"]: k for k in (scheme.get("kinds") or [])}
    if not kinds:
        return scheme
    for bucket in (scheme.get("fields") or [],
                   (scheme.get("metrics") or {}).get("fields") or []):
        for field in bucket:
            kind = kinds.get(field.get("kind"))
            if not kind:
                continue
            field["type"] = field.get("type") or kind.get("base_type")
            field["editor"] = field.get("editor") or kind.get("editor")
            if not field.get("options") and kind.get("options"):
                field["options"] = list(kind["options"])
            if not field.get("columns") and kind.get("columns"):
                field["columns"] = list(kind["columns"])
            if field.get("role") is None and kind.get("role"):
                field["role"] = kind["role"]
    return scheme


# --------------------------------------------------------------------------- #
# Blocks
# --------------------------------------------------------------------------- #

def derive_blocks(scheme):
    """The ordered list of blocks a coder sees.

    A block is a group (a section of the form) or a module (a set of fields
    that only apply to some records).  Each block carries its fields, its
    condition and - for modules - the intervention types that activate it.
    """
    scheme = scheme or {}
    groups = scheme.get("groups") or []
    fields = scheme.get("fields") or []
    modules = scheme.get("modules") or {}
    itm = scheme.get("intervention_type_modules") or {}

    blocks = []
    for group in groups:
        names = [f["name"] for f in fields
                 if f.get("group") == group["name"] and f.get("scope") == "study"]
        blocks.append({
            "id": "group:" + group["name"],
            "kind": "group",
            "name": group["name"],
            "label": group.get("label") or group["name"],
            "description": group.get("description") or "",
            "show_if": group.get("show_if"),
            "activate_when": None,
            "fields": names,
        })
    for key, mod in modules.items():
        blocks.append({
            "id": "module:" + key,
            "kind": "module",
            "name": key,
            "label": mod.get("label") or key,
            "description": mod.get("description") or "",
            "show_if": mod.get("show_if"),
            "activate_when": {
                "field": "Intervention_Type",
                "op": "in",
                "value": sorted({t for types in itm.values() if key in types
                                 for t in types}),
            },
            "fields": [f["name"] for f in (mod.get("fields") or [])],
        })
    return blocks


def block_fields(scheme, block):
    """The field objects of a block, in scheme order."""
    wanted = set(block.get("fields") or [])
    out = []
    for field in (scheme.get("fields") or []):
        if field["name"] in wanted:
            out.append(field)
    for mod in (scheme.get("modules") or {}).values():
        for field in (mod.get("fields") or []):
            if field["name"] in wanted:
                out.append(field)
    for field in ((scheme.get("metrics") or {}).get("fields") or []):
        if field["name"] in wanted:
            out.append(field)
    return out


# --------------------------------------------------------------------------- #
# Visibility
# --------------------------------------------------------------------------- #

def effective_values(record, overrides=None):
    """The record's values with an intervention's overrides applied."""
    values = dict(record or {})
    for key, value in (overrides or {}).items():
        values[key] = value
    return values


def field_visible(field, values):
    """Whether a field's control is shown, given the coded values."""
    return eval_condition((field or {}).get("show_if"), values)


def field_required(field, values):
    """Whether a field is required: always, or when its condition holds."""
    if (field or {}).get("required"):
        return True
    return eval_condition((field or {}).get("required_if"), values)


def block_visible(block, values):
    """Whether a block is shown, given the coded values."""
    return eval_condition((block or {}).get("show_if"), values)


def block_active(block, values):
    """Whether a module block applies to this record.

    A module activates when the record's intervention type routes to it.  A
    block without an activation rule is always active.
    """
    activate = (block or {}).get("activate_when")
    if not activate:
        return True
    cond = normalise_condition(activate)
    if cond is None:
        return True
    raw = (values or {}).get(cond["field"])
    if raw is None:
        return False
    return eval_condition(cond, values)


# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #

def role_map(scheme, scope=None):
    """``{field_name: role}`` for every field of the scheme.

    ``scope`` restricts the map to ``"study"`` or ``"metric"`` fields; by
    default both are merged, with the metric scope winning a name clash.
    """
    out = {}
    if scope in (None, "study"):
        for field in (scheme or {}).get("fields") or []:
            if field.get("role"):
                out[field["name"]] = field["role"]
    if scope in (None, "metric"):
        for field in ((scheme or {}).get("metrics") or {}).get("fields") or []:
            if field.get("role"):
                out[field["name"]] = field["role"]
    return out


def role_inverse(scheme):
    """``{role: field_name}`` - the first field carrying each role."""
    out = {}
    for name, role in role_map(scheme).items():
        out.setdefault(role, name)
    return out


def field_by_role(scheme, role, scope=None):
    """The field object carrying ``role``, or ``None``."""
    for name, found in role_map(scheme, scope).items():
        if found == role:
            for field in all_fields(scheme):
                if field["name"] == name:
                    return field
    return None


def all_fields(scheme):
    """Every field of the scheme: study fields, then metric fields."""
    out = list((scheme or {}).get("fields") or [])
    out.extend(((scheme or {}).get("metrics") or {}).get("fields") or [])
    for mod in ((scheme or {}).get("modules") or {}).values():
        out.extend(mod.get("fields") or [])
    return out


def value_by_role(values, roles, role):
    """The value of the field carrying ``role`` inside ``values``."""
    name = (roles or {}).get(role)
    if not name:
        return None
    return (values or {}).get(name)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def validate_scheme(scheme):
    """Check a scheme for structural problems.

    Returns a list of ``{"level", "code", "message", "where"}`` entries;
    ``level`` is ``"error"`` or ``"warning"``.
    """
    problems = []

    def add(level, code, message, where=""):
        problems.append({"level": level, "code": code, "message": message, "where": where})

    if not isinstance(scheme, dict):
        add("error", "not_a_mapping", "The scheme is not a JSON object.")
        return problems

    groups = scheme.get("groups") or []
    group_names = {g["name"] for g in groups}
    fields = all_fields(scheme)

    seen_names = {}
    role_owners = {}
    for field in fields:
        name = field["name"]
        where = f"field '{name}'"
        if name in seen_names:
            add("error", "duplicate_field",
                f"Field '{name}' is defined more than once.", where)
        seen_names[name] = True
        if field.get("role"):
            owner = role_owners.get(field["role"])
            if owner and owner != name:
                add("error", "role_collision",
                    f"Role '{field['role']}' is claimed by both '{owner}' and '{name}'.",
                    where)
            role_owners.setdefault(field["role"], name)
        if field.get("scope") == "study" and field.get("group") not in group_names:
            add("warning", "unknown_group",
                f"Field '{name}' points at the unknown group "
                f"'{field.get('group')}'.", where)
        if field.get("editor") in ("select", "multiselect") and not field.get("options"):
            add("warning", "no_options",
                f"Field '{name}' uses the '{field.get('editor')}' editor but has no options.",
                where)
        values = [o.get("value") for o in field.get("options") or []]
        if len(values) != len(set(map(str, values))):
            add("error", "duplicate_option",
                f"Field '{name}' has duplicate option values.", where)

    # conditions
    condition_targets = []
    for field in fields:
        for key in ("show_if", "required_if"):
            cond = field.get(key)
            if not cond:
                continue
            where = f"field '{field['name']}'"
            if cond["field"] not in seen_names:
                add("error", "unknown_condition_target",
                    f"The {key} of '{field['name']}' points at the unknown field "
                    f"'{cond['field']}'.", where)
            condition_targets.append((field["name"], key, cond))
    for group in groups:
        cond = group.get("show_if")
        if cond and cond["field"] not in seen_names:
            add("error", "unknown_condition_target",
                f"The show_if of group '{group['name']}' points at the unknown field "
                f"'{cond['field']}'.", f"group '{group['name']}'")

    # visibility cycles: a field whose visibility depends on a hidden field
    visible_of = {f["name"]: (f.get("show_if") or {}).get("field") for f in fields}
    for name, _key, _cond in condition_targets:
        seen = set()
        cursor = name
        while cursor and cursor in visible_of:
            if cursor in seen:
                add("error", "condition_cycle",
                    f"The conditions of '{name}' form a cycle.", f"field '{name}'")
                break
            seen.add(cursor)
            cursor = visible_of.get(cursor)

    # kinds
    kind_ids = set()
    for kind in scheme.get("kinds") or []:
        if kind["id"] in kind_ids:
            add("error", "duplicate_kind", f"Kind '{kind['id']}' is defined twice.",
                f"kind '{kind['id']}'")
        kind_ids.add(kind["id"])
        if kind.get("base_type") not in FIELD_TYPES:
            add("error", "bad_kind_type",
                f"Kind '{kind['id']}' has the unknown type '{kind.get('base_type')}'.",
                f"kind '{kind['id']}'")
    for field in fields:
        kind = field.get("kind")
        if kind and kind not in kind_ids:
            add("error", "unknown_kind",
                f"Field '{field['name']}' uses the unknown kind '{kind}'.",
                f"field '{field['name']}'")

    # groups with no fields
    used = {f.get("group") for f in scheme.get("fields") or []}
    for group in groups:
        if group["name"] not in used:
            add("warning", "empty_group",
                f"Group '{group['name']}' holds no fields.", f"group '{group['name']}'")

    # modules
    for key, mod in (scheme.get("modules") or {}).items():
        for field in mod.get("fields") or []:
            if field.get("group") and field["group"] != key:
                add("warning", "unknown_group",
                    f"Module field '{field['name']}' points at the group "
                    f"'{field['group']}' instead of its module '{key}'.",
                    f"field '{field['name']}'")

    # metrics
    metrics = scheme.get("metrics") or {}
    metrics_group = metrics.get("group")
    for field in metrics.get("fields") or []:
        if not field.get("role"):
            add("warning", "no_role",
                f"Metric field '{field['name']}' carries no role, so the analysis "
                "cannot address it by role.", f"field '{field['name']}'")
        if metrics_group and field.get("group") != metrics_group:
            add("warning", "unknown_group",
                f"Metric field '{field['name']}' points at the group "
                f"'{field.get('group')}' instead of the metrics group "
                f"'{metrics_group}'.", f"field '{field['name']}'")
    if metrics.get("enabled") and not metrics.get("fields"):
        add("warning", "empty_metrics",
            "The metrics block is enabled but holds no fields.", "metrics")

    return problems
