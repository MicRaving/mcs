"""The agent-facing export of a coding scheme.

A *scheme* (see :mod:`scheme_lib`) is the machine contract a coder - human or
agent - codes against.  This module turns that contract into the artefacts an
agent can consume:

``scheme_to_json_schema(scheme)``
    A JSON Schema (draft 2020-12) for **one coded record**: one property per
    study-level field, a ``$defs.metric`` definition for one per-contrast (DV)
    row, and the metrics array on the record itself.

``scheme_to_prompt(scheme, criteria=None)``
    The agent system prompt in Markdown: the job, the output contract, the
    "no invented numbers" rule, one section per block, the metrics block with
    the accepted role bundles, and the criteria.

``build_bundle(scheme, criteria=None)``
    All of the above plus the MCP server config and a minimal OpenAPI
    description, as one dict.

``write_bundle(scheme, criteria=None, out_dir=None)``
    The same bundle written to disk as five files under
    ``<project root>/Project/bundles/<scheme id>/``.

``bundle_manifest(scheme)``
    A short listing of the bundle's files, for a UI listing.

The module is stdlib-only.  It never writes outside the bundle folder it is
given, and the bundle is reproducible: two runs produce byte-identical files.

**The null convention.**  A coded record carries *every* field of the scheme,
and a field the paper does not state is coded ``null`` - for plain fields and
for controlled ones alike (``dv_polarity``, ``p_censored``, ``code_available``).
Every generated property therefore accepts ``null``, and the enums list it
alongside the option values.  A schema that refused ``null`` would refuse the
campaign's own records.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile

# ``scheme_lib`` lives one level up (``Scripts/``) so the desktop app, the
# analysis pipeline and the agent tooling can all import it.  This module
# lives in ``Scripts/coding_web/``; make the parent importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import scheme_lib as SL  # noqa: E402

#: JSON Schema type for every scheme field type.
JSON_TYPES = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "list": "array",
    "object": "object",
    "date": "string",
}

#: The role bundles that make one *complete* effect.  Each bundle is a head
#: phrase plus the requirement groups that follow it; a group with several
#: roles is an alternative ("d or g").  The roles are the contract - the field
#: names are looked up in the scheme.
EFFECT_BUNDLES = (
    {"id": "descriptives", "head": "full descriptives (",
     "groups": (("n1",), ("m1",), ("sd1",), ("n2",), ("m2",), ("sd2",)), "tail": ")"},
    {"id": "t", "head": "t with ", "groups": (("n1",), ("n2",)), "tail": ""},
    {"id": "F", "head": "F with ",
     "groups": (("df_num",), ("df_den",), ("n1",), ("n2",)), "tail": ""},
    {"id": "r", "head": "r with ", "groups": (("n_total",),), "tail": ""},
    {"id": "yi", "head": "yi with ", "groups": (("vi", "sei"),), "tail": ""},
    {"id": "d_g", "head": "d or g with ", "groups": (("n1",), ("n2",)), "tail": ""},
)

#: The six files a bundle is made of.
BUNDLE_FILES = (
    ("scheme.json", "The coding scheme itself - the machine contract."),
    ("json-schema.json", "JSON Schema (draft 2020-12) for one coded record "
                         "and one metric row."),
    ("prompt.md", "The agent system prompt: the job, the output contract, "
                  "every field, the metrics rules and the criteria."),
    ("criteria.json", "The project's inclusion criteria: the abstract stage "
                      "and the full-text stage, with their pass values."),
    ("mcp.json", "The MCP server configuration for this project."),
    ("openapi.json", "The REST contract of the scheme and criteria routes."),
)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _text(value, default=""):
    """``value`` as a stripped string ("" for ``None``)."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return default


def _uri_slug(text, fallback="scheme"):
    """A URI-safe token for a scheme name."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", _text(text)).strip("-")
    return slug or fallback


def _json_type(values):
    """The JSON type of a list of option values (or ``None`` when mixed)."""
    kinds = set()
    for value in values:
        if isinstance(value, bool):
            kinds.add("boolean")
        elif isinstance(value, int):
            kinds.add("integer")
        elif isinstance(value, float):
            kinds.add("number")
        elif isinstance(value, str):
            kinds.add("string")
        else:
            kinds.add("other")
    if not kinds or kinds == {"other"}:
        return None
    if kinds == {"integer", "number"}:
        return "number"
    if len(kinds) != 1:
        return None
    return kinds.pop()


def _nullable(json_type):
    """The JSON type of a field, plus ``null``.

    The campaign's convention is that *every* field of a record is present and
    a field the paper does not state is coded ``null`` - the corpus does this
    for plain fields (``Sample_Region``) **and** for controlled ones
    (``dv_polarity``, ``p_censored``, ``code_available``).  A schema that
    refused ``null`` would refuse the campaign's own records, so every
    property accepts it.
    """
    if not json_type:
        return "null"
    if isinstance(json_type, list):
        return json_type if "null" in json_type else json_type + ["null"]
    return [json_type, "null"]


def _enum_with_null(values):
    """The option values plus ``null`` (the 'not stated' value)."""
    out = list(values)
    if None not in out:
        out.append(None)
    return out


def _condition_text(condition):
    """``"<field> is <op> <value>"`` - the human reading of a condition."""
    if not isinstance(condition, dict):
        return ""
    field = _text(condition.get("field"))
    op = _text(condition.get("op")).lower()
    value = condition.get("value")
    if isinstance(value, (list, tuple)):
        rendered = ", ".join(_text(v) for v in value if _text(v))
    else:
        rendered = _text(value)
    if not field or not op:
        return ""
    return f"{field} is {op} {rendered}".strip()


def _option_text(field, with_labels=True):
    """``"a, b"`` - the option values of a field (labels in brackets).

    A label is only repeated when the field's own description does not already
    spell it out - most of the scheme's descriptions already carry the
    ``1=meaning`` mapping, and repeating it doubles the prompt for nothing.
    """
    options = field.get("options") or []
    if not options:
        return ""
    labels = SL.option_labels(options)
    description = _text(field.get("description")).lower()
    parts = []
    for option in options:
        value = option.get("value")
        label = labels.get(value)
        label_text = _text(label)
        if (with_labels and label_text and label_text != _text(value)
                and label_text.lower() not in description):
            parts.append(f"{_text(value)} ({label_text})")
        else:
            parts.append(_text(value))
    return ", ".join(parts)


def _field_description(field, notes=()):
    """The description of a field: label, description, instruction, notes."""
    parts = []
    label = _text(field.get("label")) or _text(field.get("name"))
    if label:
        parts.append(label)
    description = _text(field.get("description"))
    instruction = _text(field.get("instruction"))
    if description:
        parts.append(description)
    if instruction and instruction != description:
        parts.append(instruction)
    text = " — ".join(parts)
    for note in notes:
        note = _text(note)
        if not note:
            continue
        text = f"{text} {note}" if text else note
    return text


# --------------------------------------------------------------------------- #
# JSON Schema
# --------------------------------------------------------------------------- #

def _property_for_field(field):
    """The JSON Schema property of one scheme field."""
    field_type = field.get("type") or "string"
    json_type = JSON_TYPES.get(field_type, "string")
    editor = field.get("editor") or ""
    options = field.get("options") or []
    values = SL.option_values(options)
    columns = field.get("columns") or []

    # The accepted values of a CONTROLLED vocabulary are the closed options,
    # the controlled values and the legacy values the pipeline still resolves.
    # A token field with only a *suggestion* vocabulary is free text by
    # construction - the coder UI offers suggestions and accepts anything, and
    # the corpus proves it - so it gets no enum, only a description of the
    # vocabulary.  The JSON Schema therefore enforces exactly what the scheme
    # declares controlled.
    if editor == "chips":
        values = []
    else:
        extra = [v for v in (field.get("controlled_values") or []) if v != ""]
        extra += SL.option_values(field.get("legacy_options") or [])
        # A field name that repeats across modules carries each module's own
        # option list in ``module_options``; a record may hold any of them, so
        # the accepted values are the union.
        for mod_options in (field.get("module_options") or {}).values():
            for option in mod_options or []:
                value = option.get("value") if isinstance(option, dict) else option
                if value is not None:
                    extra.append(value)
        seen = {str(v) for v in values}
        for value in extra:
            if str(value) not in seen:
                seen.add(str(value))
                values = list(values) + [value]

    # A multiselect holds a *list* of option values even when the field's
    # declared type is "string" (the legacy scheme says so for
    # Fulltext_Exclusion_Criterion), so the editor decides the shape.
    is_multi = editor in ("multiselect", "chips") or (
        json_type == "array" and bool(values))

    notes = []
    if field.get("required"):
        notes.append("Required.")
    required_if = _condition_text(field.get("required_if"))
    if required_if:
        notes.append(f"Conditionally required when {required_if}.")
    show_if = _condition_text(field.get("show_if"))
    if show_if:
        notes.append(f"Code only if {show_if}.")
    vocab = [v for v in (field.get("vocab") or []) if v != ""]
    if vocab and not field.get("options"):
        notes.append("Suggested values: " + ", ".join(str(v) for v in vocab)
                     + ". Any other value is accepted.")

    prop = {}
    if columns:
        # An objectlist: an array of objects, one property per column.
        prop["type"] = _nullable("array")
        prop["items"] = {
            "type": "object",
            "properties": {
                column.get("name"): {
                    "type": _nullable(
                        JSON_TYPES.get(column.get("type") or "string", "string"))
                }
                for column in columns if column.get("name")
            },
            "additionalProperties": True,
        }
    elif is_multi:
        # A legacy record may hold a bare string (or an empty one) where a
        # list is expected, so the string shape is accepted alongside the
        # array.  The array's items carry the controlled values.
        prop["type"] = ["array", "null", "string"]
        if values:
            items = {"enum": values}
            item_type = _json_type(values)
            if item_type:
                items["type"] = item_type
            prop["items"] = items
        else:
            prop["items"] = {"type": "string"}
    else:
        prop["type"] = _nullable(json_type)
        if field_type == "date":
            prop["format"] = "date"
        if values:
            prop["enum"] = _enum_with_null(values)

    prop["title"] = _text(field.get("label")) or _text(field.get("name"))
    prop["description"] = _field_description(field, notes)
    if field.get("role"):
        prop["x-role"] = field["role"]
    return prop


def _metric_definition(scheme):
    """The ``$defs.metric`` definition: one per-contrast (DV) row."""
    metrics = scheme.get("metrics") or {}
    metric_fields = metrics.get("fields") or []
    label = _text(metrics.get("label")) or "Metric"
    properties = {}
    required = []
    for field in metric_fields:
        name = _text(field.get("name"))
        if not name:
            continue
        properties[name] = _property_for_field(field)
        if field.get("required"):
            required.append(name)
    definition = {
        "type": "object",
        "title": label,
        "description": (
            "One inferential dependent variable and one group contrast. "
            "Every number must be sourced from the paper; a number that is not "
            "reported is left null and explained in the missing-metrics reason "
            "field."
        ),
        "properties": properties,
    }
    if required:
        definition["required"] = required
    definition["additionalProperties"] = True
    return definition


def scheme_to_json_schema(scheme):
    """The JSON Schema of **one coded record** of ``scheme``.

    The document describes a record as it is stored on disk: one property per
    study-level field, the metrics array (``$ref`` to ``$defs.metric``) and
    ``additionalProperties: true`` for forward compatibility.
    """
    scheme = SL.normalise_scheme(scheme or {})
    label = _text(scheme.get("label")) or _text(scheme.get("name")) or "Coding scheme"
    name = _text(scheme.get("name")) or _text(scheme.get("id")) or "scheme"
    metrics = scheme.get("metrics") or {}
    metrics_group = _text(metrics.get("group")) or SL.DEFAULT_METRICS_GROUP

    properties = {}
    required = []
    for field in scheme.get("fields") or []:
        field_name = _text(field.get("name"))
        if not field_name or field.get("scope") == "metric":
            continue
        properties[field_name] = _property_for_field(field)
        if field.get("required"):
            required.append(field_name)
    # The module fields are record-level too: a record stores the fields of the
    # module its intervention type routes to (Inoculation_Target, Refutation_Type,
    # ...), at the top level of the record.  They are described here so a coded
    # record validates, but they are never in "required": a module field is
    # required only when its module is active, and the schema cannot say that.
    for module in (scheme.get("modules") or {}).values():
        for field in module.get("fields") or []:
            field_name = _text(field.get("name"))
            if not field_name or field_name in properties:
                continue
            properties[field_name] = _property_for_field(field)

    metric_def = _metric_definition(scheme)
    properties[metrics_group] = {
        "type": _nullable("array"),
        "title": _text(metrics.get("label")) or "Effect sizes",
        "description": (
            f"One entry per inferential dependent variable and per reported "
            f"group contrast. The entries follow the '{metrics_group}' "
            f"definition."
        ),
        "items": {"$ref": "#/$defs/metric"},
    }

    document = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"codingstudio://{_uri_slug(name)}/record",
        "title": label,
        "type": "object",
        "properties": properties,
    }
    if required:
        document["required"] = required
    document["additionalProperties"] = True
    document["$defs"] = {"metric": metric_def}
    return document


# --------------------------------------------------------------------------- #
# The agent prompt
# --------------------------------------------------------------------------- #

def _field_line(field, with_role=True, max_description=None):
    """``* **name** — Label (required): description. Options: a, b. [role: x]``."""
    name = _text(field.get("name"))
    label = _text(field.get("label"))
    parts = [f"* **{name}**"]
    if label and label != name:
        parts.append(f" — {label}")
    if field.get("required"):
        parts.append(" (required)")
    required_if = _condition_text(field.get("required_if"))
    if required_if:
        parts.append(f" (required if {required_if})")
    description = _text(field.get("description"))
    instruction = _text(field.get("instruction"))
    if max_description and len(description) > max_description:
        description = description[:max_description].rstrip() + " ..."
    if description or instruction:
        parts.append(": ")
        parts.append(" ".join(part for part in (description, instruction) if part))
    options = _option_text(field)
    if options:
        parts.append(f" Options: {options}.")
    show_if = _condition_text(field.get("show_if"))
    if show_if:
        parts.append(f" Code only if {show_if}.")
    if with_role and field.get("role"):
        parts.append(f" [role: {field['role']}]")
    return "".join(parts)


def _metric_role_names(scheme):
    """``{role: field name}`` for the metric fields of the scheme."""
    out = {}
    for field_name, role in SL.role_map(scheme, "metric").items():
        out.setdefault(role, field_name)
    return out


def _render_bundle(bundle, role_names):
    """One effect bundle as text, e.g. ``"d or g with n1 and n2"``."""
    parts = []
    for group in bundle["groups"]:
        names = [role_names.get(role, role) for role in group]
        parts.append(" or ".join(names) if len(names) > 1 else names[0])
    if not parts:
        return bundle["head"].strip()
    if len(parts) == 1:
        body = parts[0]
    elif len(parts) == 2:
        body = f"{parts[0]} and {parts[1]}"
    else:
        body = ", ".join(parts[:-1]) + f" and {parts[-1]}"
    return f"{bundle['head']}{body}{bundle['tail']}"


def _criteria_stages(criteria):
    """``[(stage name, [criterion, ...]), ...]`` for whatever criteria shape."""
    if not criteria:
        return []
    stages = []
    if isinstance(criteria, dict):
        for key, label in (("abstract", "Abstract screening"),
                           ("fulltext", "Full-text exclusion")):
            items = criteria.get(key)
            if isinstance(items, list) and items:
                stages.append((label, items))
        if not stages:
            items = criteria.get("criteria")
            if isinstance(items, list) and items:
                stages.append(("Criteria", items))
        return stages
    if isinstance(criteria, list):
        return [("Criteria", criteria)]
    return []


def _criterion_value(criterion, *names, default=""):
    """Read a field of a criterion (dict or object)."""
    for name in names:
        if isinstance(criterion, dict):
            if name in criterion and criterion[name] not in (None, ""):
                return criterion[name]
        else:
            value = getattr(criterion, name, None)
            if value not in (None, ""):
                return value
    return default


def _criteria_section(criteria):
    """The criteria part of the prompt."""
    stages = _criteria_stages(criteria)
    if not stages:
        return []
    lines = [
        "## Inclusion criteria",
        "",
        "Every **gating** criterion must pass. A study that fails a gating "
        "criterion is excluded, but it is still coded in full: record the "
        "failing criterion and keep the data you read.",
        "",
    ]
    for stage_label, items in stages:
        lines.append(f"### {stage_label}")
        lines.append("")
        for criterion in items:
            key = _text(_criterion_value(criterion, "key"))
            label = _text(_criterion_value(criterion, "label"))
            prompt_text = _text(_criterion_value(criterion, "prompt_text",
                                                "prompt", "text"))
            gating = bool(_criterion_value(criterion, "gating", default=True))
            pass_values = _criterion_value(criterion, "pass_values", default=[])
            help_text = _text(_criterion_value(criterion, "help"))
            head = f"- **{key}**" if key else "-"
            if label:
                head += f" — {label}"
            if not gating:
                head += " (not gating)"
            lines.append(head)
            if prompt_text:
                lines.append(f"  {prompt_text}")
            if help_text and help_text != prompt_text:
                lines.append(f"  Help: {help_text}")
            if isinstance(pass_values, (list, tuple)) and pass_values:
                lines.append(f"  Pass values: {', '.join(_text(v) for v in pass_values)}.")
        lines.append("")
    return lines


def scheme_to_prompt(scheme, criteria=None, max_description=None):
    """The agent-facing system prompt of ``scheme`` (Markdown).

    The prompt states the job and the output contract, the "no invented
    numbers" rule, one section per block, the metrics block with the accepted
    role bundles, and - when given - the criteria.

    ``max_description`` caps the length of a field's description (the scheme's
    own wording is kept when it is ``None``).  It is a knob for a shorter
    prompt, not a default: the campaign scheme has 246 fields, and a
    field-complete prompt is what the agent needs.
    """
    scheme = SL.normalise_scheme(scheme or {})
    label = _text(scheme.get("label")) or _text(scheme.get("name")) or "Coding scheme"
    prompts = scheme.get("prompts") or {}
    system_text = _text(prompts.get("system"))
    metrics = scheme.get("metrics") or {}
    metrics_group = _text(metrics.get("group")) or SL.DEFAULT_METRICS_GROUP
    metrics_label = _text(metrics.get("label")) or "Effect sizes"
    role_names = _metric_role_names(scheme)
    missing_field = role_names.get("missing_reason", "missing_metrics_reason")

    lines = [f"# {label} — coding prompt", ""]
    if system_text:
        lines.extend([system_text, ""])

    lines.extend([
        "## Job",
        "",
        "You are an expert academic research assistant and data coder. Read "
        "the paper thoroughly and code every independent experiment it reports.",
        "",
        "## Output contract",
        "",
        "Return a **JSON array** with one object per experiment, and nothing "
        "else - no prose, no markdown fence:",
        "",
        "```json",
        "[",
        "  {",
        '    "<study field>": "value",',
        f'    "{metrics_group}": [',
        '      { "<metric field>": "value" }',
        "    ]",
        "  }",
        "]",
        "```",
        "",
        "- one array element per **independent experiment**, not per paper; a "
        "paper with two experiments has two elements;",
        "- every study field of this scheme appears in every record, even when "
        "the paper does not state it - use `null` (or `[]` for a list field) "
        "instead of leaving the key out;",
        f"- `{metrics_group}` holds one entry per inferential dependent variable "
        f"and per reported group contrast;",
        "- use the option values exactly as listed below - the label in "
        "brackets is the meaning of the value, the value itself is what you "
        "write.",
        "",
        "## Rules",
        "",
        "- **No invented numbers.** Every number must be sourced from the paper "
        "(its text, tables or figures). If a number is not reported, leave the "
        f"field `null` and record a non-empty `{missing_field}` that names what "
        "you looked for (the table, figure or paragraph). Never derive, "
        "estimate, or carry over a value that the paper does not report.",
        f"- **Never leave a contrast unexplained.** A DV row without a complete "
        f"effect needs a non-empty `{missing_field}`.",
        "- code what the paper says, not what the design implies; leave a field "
        "empty rather than guessing.",
        "- cite the source of every value in the source-location field of the "
        "DV row (page, table or figure) when the scheme provides one.",
        "",
    ])

    # -- the blocks -------------------------------------------------------- #
    blocks = SL.derive_blocks(scheme)
    group_blocks = [b for b in blocks if b.get("kind") == "group"]
    module_blocks = [b for b in blocks if b.get("kind") == "module"]

    def _block_lines(block):
        out = [f"### {_text(block.get('label')) or _text(block.get('name'))}", ""]
        fields = SL.block_fields(scheme, block)
        if not fields:
            out.append("* (this block has no fields)")
            out.append("")
            return out
        for field in fields:
            out.append(_field_line(field, max_description=max_description))
        out.append("")
        return out

    if group_blocks:
        lines.extend(["## Study fields", ""])
        for block in group_blocks:
            lines.extend(_block_lines(block))

    if module_blocks:
        lines.extend(["## Intervention modules", "",
                      "A module block applies when the record's intervention "
                      "type routes to it. Fill only the block that applies.",
                      ""])
        for block in module_blocks:
            activate = block.get("activate_when") or {}
            values = activate.get("value") or []
            if values:
                lines.append(
                    f"_{_text(block.get('label'))} applies when "
                    f"{_text(activate.get('field'))} is one of: "
                    f"{', '.join(_text(v) for v in values)}._"
                )
                lines.append("")
            lines.extend(_block_lines(block))

    # -- the metrics block ------------------------------------------------- #
    metric_fields = metrics.get("fields") or []
    if metric_fields:
        lines.extend([
            f"## {metrics_label} (`{metrics_group}`)",
            "",
            "One entry per inferential dependent variable (DV) and per "
            "reported group contrast, inside the array of the same name.",
            "",
            f"### {metrics_group}",
            "",
        ])
        for field in metric_fields:
            lines.append(_field_line(field, max_description=max_description))
        lines.append("")

        roles = sorted({role for role in SL.role_map(scheme, "metric").values()})
        if roles:
            lines.append(f"Metric roles in use: {', '.join(roles)}.")
            lines.append("")
        families = [f for f in (scheme.get("effect_families") or [])
                    if f in SL.EFFECT_FAMILIES]
        if families:
            lines.append(f"Preferred effect-size families: {', '.join(families)}.")
            lines.append("")

        bundles = []
        for bundle in EFFECT_BUNDLES:
            known = all(
                any(role in role_names for role in group)
                for group in bundle["groups"]
            )
            if known:
                bundles.append(_render_bundle(bundle, role_names))
        if bundles:
            lines.append("A complete effect is one of these role bundles "
                         "(field names in brackets):")
            lines.append("")
            for rendered in bundles:
                lines.append(f"- {rendered}")
            lines.append("")
        lines.append(
            f"Never leave a contrast without a complete effect set. Leave a "
            f"numeric field `null` when the paper does not report it and "
            f"record why in `{missing_field}` (a non-empty sentence that names "
            f"the table, figure or paragraph you searched)."
        )
        lines.append("")

    # -- the criteria ------------------------------------------------------ #
    lines.extend(_criteria_section(criteria))

    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# The bundle
# --------------------------------------------------------------------------- #

def _project_root(root=None):
    """The active project root (falls back to the folder holding ``Scripts/``)."""
    if root:
        return os.path.abspath(root)
    try:
        import project_context  # noqa: PLC0415 - optional, keeps the import cheap
        return project_context.active_root()
    except Exception:  # pragma: no cover - the fallback is the repo root
        return os.path.dirname(_SCRIPTS)


def _project_criteria():
    """The active project's criteria (best effort - ``None`` when there are none)."""
    try:
        import project_context  # noqa: PLC0415 - optional
        return project_context.criteria()
    except Exception:  # pragma: no cover - the criteria library is optional
        return None


def mcp_config(scheme, root=None):
    """The MCP server configuration of the project."""
    project_root = _project_root(root)
    return {
        "mcpServers": {
            "codingstudio": {
                "command": "python",
                "args": ["-m", "Scripts.coding_web.mcp_server",
                         "--root", project_root],
            }
        }
    }


def openapi_document(scheme):
    """A minimal OpenAPI description of the scheme and criteria routes."""
    scheme = SL.normalise_scheme(scheme or {})
    label = _text(scheme.get("label")) or _text(scheme.get("name")) or "Coding scheme"
    json_schema = scheme_to_json_schema(scheme)

    def _ok(summary):
        return {
            "summary": summary,
            "responses": {"200": {"description": "OK"}},
        }

    paths = {
        "/api/project": {
            "get": _ok("The active project: id, name, path and scheme summary."),
        },
        "/api/scheme": {
            "get": _ok("The active coding scheme, normalised."),
            "put": _ok("Replace the coding scheme (whole document, validated)."),
        },
        "/api/scheme/fields": {
            "get": _ok("The scheme's fields, one entry per field."),
            "post": _ok("Add or update one field, by name."),
        },
        "/api/criteria": {
            "get": _ok("The abstract and full-text exclusion criteria."),
            "put": _ok("Replace the abstract and full-text criteria."),
        },
    }

    record = {
        "type": "object",
        "title": json_schema.get("title"),
        "description": (
            "One coded record: one experiment, its study-level fields and its "
            "metrics array."
        ),
        "properties": json_schema.get("properties") or {},
        "additionalProperties": True,
    }
    if json_schema.get("required"):
        record["required"] = json_schema["required"]

    return {
        "openapi": "3.0.3",
        "info": {
            "title": f"{label} - coding API",
            "version": "1.0.0",
            "description": (
                "The scheme and criteria routes of the Coding Studio app. The "
                "record and metric schemas mirror the agent bundle's "
                "json-schema.json."
            ),
        },
        "paths": paths,
        "components": {
            "schemas": {
                "Record": record,
                "Metric": json_schema.get("$defs", {}).get("metric", {}),
            }
        },
    }


def build_bundle(scheme, criteria=None, root=None, max_description=None):
    """The whole agent bundle as one dict.

    Keys: ``scheme``, ``json-schema``, ``prompt``, ``criteria``, ``mcp`` and
    ``openapi``.  When ``criteria`` is ``None`` the active project's criteria
    are used (an empty list of criteria simply leaves the criteria section out
    of the prompt).
    """
    scheme = SL.normalise_scheme(scheme or {})
    if criteria is None:
        criteria = _project_criteria()
    return {
        "scheme": scheme,
        "json-schema": scheme_to_json_schema(scheme),
        "prompt": scheme_to_prompt(scheme, criteria, max_description=max_description),
        "criteria": criteria or {},
        "mcp": mcp_config(scheme, root),
        "openapi": openapi_document(scheme),
    }


def bundle_manifest(scheme):
    """``[{"name", "description"}, ...]`` - the bundle's files, for a UI."""
    scheme = SL.normalise_scheme(scheme or {})
    label = _text(scheme.get("label")) or _text(scheme.get("name")) or "Coding scheme"
    out = []
    for name, description in BUNDLE_FILES:
        if name == "prompt.md":
            description = f"{description} ({label})"
        out.append({"name": name, "description": description})
    return out


def default_bundle_dir(scheme, root=None):
    """``<project root>/Project/bundles/<scheme id>/``."""
    scheme = SL.normalise_scheme(scheme or {})
    scheme_id = _uri_slug(scheme.get("id") or scheme.get("name") or "scheme")
    base = _project_root(root)
    try:
        import project_context  # noqa: PLC0415 - optional
        bundles = project_context.project_paths(base).get("bundles")
    except Exception:  # pragma: no cover - the fallback is the same layout
        bundles = os.path.join(base, "Project", "bundles")
    return os.path.join(bundles, scheme_id)


def _atomic_write(path, text):
    """Write ``text`` to ``path`` atomically (same-directory temp file)."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    handle, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=folder)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def _dump_json(payload):
    """A stable JSON rendering (same input, same bytes)."""
    return json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=False) + "\n"


def write_bundle(scheme, criteria=None, out_dir=None, root=None,
                 max_description=None):
    """Write the agent bundle to disk; returns the written paths.

    The five files (``scheme.json``, ``json-schema.json``, ``prompt.md``,
    ``mcp.json``, ``openapi.json``) are written atomically and are identical
    on every run.
    """
    scheme = SL.normalise_scheme(scheme or {})
    bundle = build_bundle(scheme, criteria, root=root,
                          max_description=max_description)
    folder = os.path.abspath(out_dir or default_bundle_dir(scheme, root))

    written = []
    for name, _description in BUNDLE_FILES:
        if name == "prompt.md":
            text = bundle["prompt"]
        elif name == "criteria.json":
            text = _dump_json(bundle.get("criteria") or {})
        elif name == "mcp.json":
            text = _dump_json(bundle["mcp"])
        elif name == "openapi.json":
            text = _dump_json(bundle["openapi"])
        elif name == "json-schema.json":
            text = _dump_json(bundle["json-schema"])
        else:
            text = _dump_json(bundle["scheme"])
        written.append(_atomic_write(os.path.join(folder, name), text))
    return written
