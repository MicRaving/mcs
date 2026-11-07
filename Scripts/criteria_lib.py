"""The criteria model: what a criterion is, and the one inclusion rule.

A **criterion** is the unit the screening stages work with.  It is a flat
mapping with a fixed set of keys::

    {key, label, kind, options, pass_values, exclude_reason, gating,
     help, prompt_text, keywords, order}

* ``key`` - the stable id (``C1`` ... ``C4`` for the abstract stage, plus
  ``irretrievable`` ... ``other`` for the full-text stage).  The existing
  ``Screening/*.json`` files, the ``master_dataset.xlsx`` columns
  (``C1_verdict`` ... ``C4_rationale``) and the PRISMA report are keyed by
  it, so a key never changes.
* ``label`` - the human-readable name of the criterion.
* ``kind`` - ``"verdict"`` (Yes/No/Unsure), ``"classify"`` (pick one of
  ``options``) or ``"number"``.
* ``options`` - the offered values; empty for a number criterion.
* ``pass_values`` - the values that PASS the criterion.  The comparison is
  case-insensitive and an empty list means ``["yes"]``.
* ``exclude_reason`` - the PRISMA label used when the criterion fails.
* ``gating`` - ``False`` makes the criterion **informational**: it is
  reported but never blocks inclusion.
* ``help`` / ``prompt_text`` - the instruction shown to the coder and handed
  to the agent.
* ``keywords`` - optional per-criterion keyword hints (the free-text hint
  list the seeded criteria carry).
* ``order`` - the position of the criterion inside its list.
* ``auto_approve`` - the criterion's **auto-approval rule set**: a plain
  dictionary of named regular expressions.  A rule approves the criterion
  when its expression matches the abstract text; the screening UI then
  pre-fills the criterion's first pass value and says which rule matched.
  The rule set is a dictionary on purpose - the order is the insertion
  order and the names are free text.  An empty or missing rule set means
  "no auto-approval".  A rule whose expression does not compile never
  approves anything; it is reported by :func:`validate_criteria` and
  ignored at match time, so a broken rule can never break the criteria.

The **inclusion rule** lives here and nowhere else
(:func:`evaluate_criteria`): every *gating* criterion must pass, an absent
verdict fails a gating criterion, and a criterion with ``gating = False``
never blocks.  When no criteria are configured the legacy rule applies:
every recorded verdict must be ``"yes"``.

The module is stdlib-only and lives one level up (``Scripts/``), next to
``scheme_lib.py`` and ``project_context.py``, so the desktop app, the analysis
pipeline and the agent tooling all import it as ``criteria_lib`` and get the
very same module object.

A project that carries no ``Project/criteria.json`` still screens with the
seeded criteria: :func:`normalise_criteria` falls back to the built-in lists
when the document carries none (the plan's M3 fallback).
"""

from __future__ import annotations

import re

#: The kinds a criterion may have.
CRITERION_KINDS = ("verdict", "classify", "number")

#: The values a verdict criterion offers.
VERDICT_OPTIONS = ("Yes", "No", "Unsure")

#: The keys of a criterion, in their canonical order.
CRITERION_FIELDS = (
    "key", "label", "kind", "options", "pass_values", "exclude_reason",
    "gating", "help", "prompt_text", "keywords", "order", "auto_approve",
)

#: The stages a project carries criteria for.
CRITERION_STAGES = ("abstract", "fulltext")

#: A rule expression that starts with this prefix is matched case-SENSITIVELY.
#: Every other expression is matched case-insensitively.
CASE_SENSITIVE_PREFIX = "(?-i)"


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _value_text(value):
    """The comparison form of a recorded value.

    ``None`` becomes the empty string, ``True``/``False`` become
    ``"yes"``/``"no"`` and numbers become their string form; everything else
    is stripped text.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    return ""


def _text(value, default=""):
    """A stripped string, or ``default`` for anything that is not text."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    return default


def _as_bool(value, default=False):
    """A boolean, accepting ``True``/``False``, ``0``/``1`` and the usual words."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
    return default


def _as_int(value, default=0):
    """An integer, or ``default`` when the value is not a number."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_str_list(value):
    """A list of stripped, non-empty strings."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out = []
    for item in items:
        if isinstance(item, dict):
            item = item.get("value")
            if item is None:
                item = item.get("label")
        text = _value_text(item)
        if text and text not in out:
            out.append(text)
    return out


def _entry_value(entry):
    """The verdict of a recorded entry.

    ``entry`` may be the value itself (``"Yes"``) or the recorded mapping
    (``{"verdict": "Yes", "certainty": "High"}`` or ``{"applies": "Yes"}``).
    The first non-empty of ``applies`` / ``verdict`` / ``value`` /
    ``criterion`` / ``answer`` / ``selection`` wins; an entry without any of
    them has no verdict.
    """
    if isinstance(entry, dict):
        for name in ("applies", "verdict", "value", "criterion", "answer",
                     "selection"):
            if name in entry:
                text = _value_text(entry.get(name))
                if text:
                    return text
        return ""
    return _value_text(entry)


def _as_rule_set(value):
    """The auto-approval rule set of a criterion: ``{name: expression}``.

    The rule set is a plain dictionary: the order is the insertion order
    and the names are free text.  A value that is not a mapping yields an
    empty rule set - :func:`validate_criteria` reports the problem, the
    criteria themselves stay usable.
    """
    if not isinstance(value, dict):
        return {}
    out = {}
    for name, pattern in value.items():
        key = _value_text(name)
        if not key:
            continue
        if isinstance(pattern, str):
            out[key] = pattern
        elif pattern is None:
            out[key] = ""
        else:
            out[key] = _value_text(pattern)
    return out


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #

def normalise_criterion(raw, default_order=0):
    """Normalise one criterion into the canonical shape.

    Besides the model's own shape, the two shapes the campaign's constants
    use are understood:

    * ``{"value": "C1", "label": "No misinformation intervention (C1)"}`` -
      the full-text reason list; ``value`` becomes the key,
    * ``{"label": "C1", "title": "Misinformation intervention",
      "instruction": "..."}`` - the abstract screening list; ``label``
      becomes the key and ``title`` the label.

    Every key of the criterion is filled: a missing ``options`` on a verdict
    becomes the Yes/No/Unsure triple, a missing ``pass_values`` becomes
    ``["Yes"]`` (an explicitly empty list is kept as it is - the rule then
    falls back to ``["yes"]``), and a missing ``gating`` means the criterion
    gates inclusion.
    """
    raw = raw if isinstance(raw, dict) else {}

    key = ""
    for name in ("key", "value", "id", "code"):
        key = _value_text(raw.get(name))
        if key:
            break
    title = _value_text(raw.get("title"))
    label = _value_text(raw.get("label"))
    if not key:
        if title and label:
            # the screening shape: the code sits in "label", the name in "title"
            key, label = label, title
        else:
            key = label or title
            label = label or title
    elif not label:
        label = title or key
    elif title and label == key:
        label = title

    options = _as_str_list(raw.get("options"))
    kind = _value_text(raw.get("kind")).lower()
    if kind not in CRITERION_KINDS:
        lowered = [o.lower() for o in options]
        kind = ("classify" if options and
                lowered != [o.lower() for o in VERDICT_OPTIONS] else "verdict")
    if kind == "verdict" and not options:
        options = list(VERDICT_OPTIONS)
    if kind == "number":
        options = []

    if "pass_values" in raw:
        pass_values = _as_str_list(raw.get("pass_values"))
    else:
        pass_values = ["Yes"]

    prompt_text = _text(raw.get("prompt_text") or raw.get("instruction")
                        or raw.get("text"))
    help_text = _text(raw.get("help") or raw.get("hint")
                      or raw.get("instruction") or prompt_text)

    return {
        "key": key,
        "label": label or key,
        "kind": kind,
        "options": options,
        "pass_values": pass_values,
        "exclude_reason": _text(raw.get("exclude_reason") or raw.get("reason")),
        "gating": _as_bool(raw.get("gating"), True),
        "help": help_text,
        "prompt_text": prompt_text,
        "keywords": _as_str_list(raw.get("keywords")),
        "order": _as_int(raw.get("order"), default_order),
        "auto_approve": _as_rule_set(raw.get("auto_approve")),
    }


def _normalise_list(raw_list):
    """Normalise one stage's list and enforce its order."""
    out = []
    if not isinstance(raw_list, list):
        return out
    for index, entry in enumerate(raw_list):
        criterion = normalise_criterion(entry, index)
        if not criterion["key"]:
            continue
        out.append(criterion)
    out.sort(key=lambda c: c["order"])
    for index, criterion in enumerate(out):
        criterion["order"] = index
    return out


def normalise_criteria(raw):
    """Normalise a criteria document.

    Accepts ``{"version": 1, "abstract": [...], "fulltext": [...]}`` - the
    shape stored in ``Project/criteria.json`` - and a bare list, which is
    read as the abstract list.  Every criterion is normalised, blank entries
    are dropped and each list is ordered by its ``order`` field (which is
    renumbered to ``0..n-1`` so the document is canonical).

    **Fallback.**  A document that carries no criteria at all (a missing
    ``Project/criteria.json``) yields the **seeded** criteria, so a project
    that has not been set up yet still screens with the built-in C1-C4 (the
    plan's M3 fallback).  A list that is present but empty stays empty: an
    editor that removes every criterion means it.
    """
    if isinstance(raw, list):
        return {"version": 1, "abstract": _normalise_list(raw), "fulltext": []}
    doc = raw if isinstance(raw, dict) else {}
    abstract_raw = doc.get("abstract")
    fulltext_raw = doc.get("fulltext")
    if not isinstance(abstract_raw, list) and not isinstance(fulltext_raw, list):
        seed = default_criteria()
        seed["version"] = _as_int(doc.get("version"), 1)
        return seed
    return {
        "version": _as_int(doc.get("version"), 1),
        "abstract": _normalise_list(abstract_raw),
        "fulltext": _normalise_list(fulltext_raw),
    }


# --------------------------------------------------------------------------- #
# The inclusion rule
# --------------------------------------------------------------------------- #

def criterion_passes(criterion, entry):
    """Whether a recorded verdict passes the criterion.

    ``entry`` may be the value itself (``"Yes"``) or the recorded entry
    (``{"verdict": "Yes"}`` / ``{"applies": "Yes"}``).  The comparison is
    case-insensitive and an **absent** verdict fails: an unanswered gating
    criterion is not a pass.
    """
    criterion = criterion if isinstance(criterion, dict) else {}
    value = _entry_value(entry)
    if not value:
        return False
    pass_values = _as_str_list(criterion.get("pass_values")) or ["Yes"]
    return value.lower() in {v.lower() for v in pass_values}


def _stage_list(criteria, stage=None):
    """The criterion list of one stage of a criteria document."""
    if isinstance(criteria, list):
        return criteria
    if not isinstance(criteria, dict):
        return []
    names = []
    if stage:
        names.append(stage)
    names.extend(s for s in CRITERION_STAGES if s not in names)
    for name in names:
        value = criteria.get(name)
        if isinstance(value, list):
            return value
    return []


def evaluate_criteria(criteria, verdicts):
    """The one inclusion rule.

    Every **gating** criterion must pass; an absent verdict fails a gating
    criterion.  A criterion with ``gating = False`` is informational: it is
    reported but never blocks inclusion.  ``reasons`` holds the
    ``exclude_reason`` (else the label, else the key) of the failing
    criteria, in the order of the list.

    ``criteria`` is the list of one stage; a whole criteria document is
    accepted as well (the abstract stage is used).  When no criteria are
    configured the legacy rule applies: every recorded verdict must be
    ``"yes"``.
    """
    entries = _stage_list(criteria)
    verdicts = verdicts if isinstance(verdicts, dict) else {}

    failing = []
    reasons = []
    if not entries:
        # Legacy rule: every recorded verdict must be "yes".
        for key, entry in verdicts.items():
            if _entry_value(entry).lower() in ("yes", "true"):
                continue
            failing.append(key)
            reasons.append(str(key))
        return {"eligible": not failing, "failing": failing, "reasons": reasons}

    for criterion in entries:
        if not criterion.get("gating", True):
            continue
        key = criterion.get("key")
        if criterion_passes(criterion, verdicts.get(key)):
            continue
        failing.append(key)
        reasons.append(criterion.get("exclude_reason")
                       or criterion.get("label") or key)
    return {"eligible": not failing, "failing": failing, "reasons": reasons}


def verdict_levels(criterion):
    """The levels the agreement module scores for one criterion.

    A verdict criterion has the three levels Yes/No/Unsure; a classify or
    number criterion has its own options.
    """
    criterion = criterion if isinstance(criterion, dict) else {}
    if (criterion.get("kind") or "verdict") == "verdict":
        return list(VERDICT_OPTIONS)
    return _as_str_list(criterion.get("options"))


# --------------------------------------------------------------------------- #
# The auto-approval rules
# --------------------------------------------------------------------------- #

def compile_rule(pattern):
    """Compile one rule's expression.

    Returns ``(regex, error)``: the compiled pattern (``None`` when the
    expression does not compile) and the error message (``""`` when it
    does).  The matching is **case-insensitive** unless the expression
    starts with ``(?-i)``, which asks for a case-sensitive match.

    An empty expression is an error: it would match every text and approve
    the criterion everywhere.
    """
    text = "" if pattern is None else str(pattern)
    flags = re.IGNORECASE
    if text.startswith(CASE_SENSITIVE_PREFIX):
        text = text[len(CASE_SENSITIVE_PREFIX):]
        flags = 0
    if not text.strip():
        return None, "the expression is empty"
    try:
        return re.compile(text, flags), ""
    except re.error as exc:
        return None, str(exc)


def compile_rule_set(rules):
    """Compile a whole rule set **once**.

    Returns a list of ``(name, expression, regex, error)`` - one entry per
    rule, in the rule set's own order.  A rule that does not compile
    carries its error and no regex: it is ignored at match time.
    """
    out = []
    if not isinstance(rules, dict):
        return out
    for name, pattern in rules.items():
        expression = "" if pattern is None else str(pattern)
        regex, error = compile_rule(expression)
        out.append((str(name), expression, regex, error))
    return out


def auto_approved(criterion, text):
    """Whether the criterion's auto-approval rules approve ``text``.

    Returns ``(approved, matched_rule_names)``.  ``approved`` is ``True``
    when **any** rule's expression matches anywhere in ``text``; the names
    are the rules that matched, in the rule set's own order.

    A rule whose expression does not compile is ignored (it approves
    nothing) - a broken rule never breaks the criteria.  A criterion
    without a rule set is never approved.
    """
    criterion = criterion if isinstance(criterion, dict) else {}
    rules = criterion.get("auto_approve")
    if not isinstance(rules, dict) or not rules:
        return False, []
    haystack = "" if text is None else str(text)
    matched = []
    for name, _expression, regex, _error in compile_rule_set(rules):
        if regex is None:
            continue
        if regex.search(haystack):
            matched.append(name)
    return bool(matched), matched


def auto_approve_matches(criteria, text):
    """``{key: (approved, [rule names])}`` for a whole criterion list.

    The rule sets of the list are compiled **once** for the call, so
    screening a thousand abstracts compiles them once, not a thousand
    times.  A criterion without rules is reported as not approved.
    """
    entries = _stage_list(criteria)
    haystack = "" if text is None else str(text)
    out = {}
    for criterion in entries:
        key = criterion.get("key") if isinstance(criterion, dict) else None
        compiled = compile_rule_set(
            criterion.get("auto_approve") if isinstance(criterion, dict) else {})
        names = [name for name, _expr, regex, _err in compiled
                 if regex is not None and regex.search(haystack)]
        out[key] = (bool(names), names)
    return out


def first_pass_value(criterion):
    """The value a criterion passes with - its first pass value.

    An empty ``pass_values`` means ``["Yes"]``, exactly as the inclusion
    rule reads it.
    """
    criterion = criterion if isinstance(criterion, dict) else {}
    values = _as_str_list(criterion.get("pass_values")) or ["Yes"]
    return values[0]


def validate_criteria(criteria, stage=None):
    """The problems of a criteria document.

    Every problem is a mapping ``{level, code, message, where}``:

    * ``level`` - ``"error"`` for a structural problem (the document
      cannot be used as it is) and ``"warning"`` for a rule that does not
      compile.  A broken rule is a **warning** on purpose: the criteria
      stay usable, the rule is simply ignored at match time, and the
      editor shows the error next to the rule.
    * ``code`` - a stable machine-readable id.
    * ``where`` - the path of the offending item, e.g.
      ``abstract[C1].auto_approve.has rct``.
    """
    problems = []

    def add(level, code, message, where=""):
        problems.append({"level": level, "code": code, "message": message,
                         "where": where})

    if isinstance(criteria, list):
        doc = {"abstract": criteria}
    elif isinstance(criteria, dict):
        doc = criteria
    else:
        add("error", "not_a_document",
            "the criteria must be a mapping with the stages "
            "'abstract' and 'fulltext'")
        return problems

    stages = [stage] if stage else list(CRITERION_STAGES)
    for name in stages:
        entries = doc.get(name)
        if entries is None:
            continue
        if not isinstance(entries, list):
            add("error", "stage_not_a_list",
                f"the '{name}' criteria must be a list", name)
            continue
        seen = {}
        for index, entry in enumerate(entries):
            where = f"{name}[{index}]"
            if not isinstance(entry, dict):
                add("error", "criterion_not_a_mapping",
                    "a criterion must be a mapping", where)
                continue
            key = _value_text(entry.get("key"))
            if not key:
                add("error", "criterion_without_key",
                    "a criterion needs a key", where)
            else:
                where = f"{name}[{key}]"
                if key in seen:
                    add("error", "duplicate_key",
                        f"the key '{key}' is used twice in the '{name}' stage",
                        where)
                seen[key] = index
            rules = entry.get("auto_approve")
            if rules is None:
                continue
            if not isinstance(rules, dict):
                add("error", "auto_approve_not_a_mapping",
                    "the auto-approval rule set must be a mapping of "
                    "name -> expression", f"{where}.auto_approve")
                continue
            for rule_name, expression in rules.items():
                rule_where = f"{where}.auto_approve.{rule_name}"
                if not isinstance(expression, str):
                    add("error", "auto_approve_rule_not_text",
                        "a rule's expression must be text", rule_where)
                    continue
                regex, error = compile_rule(expression)
                if regex is None:
                    if error == "the expression is empty":
                        add("warning", "auto_approve_rule_empty",
                            f"the rule '{rule_name}' has an empty expression; "
                            "it approves nothing until it is filled in",
                            rule_where)
                    else:
                        add("warning", "auto_approve_rule_invalid",
                            f"the rule '{rule_name}' does not compile ({error}); "
                            "it is ignored until it is fixed", rule_where)
    return problems


def criteria_by_key(criteria, stage="abstract"):
    """``{key: criterion}`` for one stage of a criteria document."""
    out = {}
    for criterion in _stage_list(criteria, stage):
        key = criterion.get("key") if isinstance(criterion, dict) else None
        if key:
            out[key] = criterion
    return out


# --------------------------------------------------------------------------- #
# The seed
# --------------------------------------------------------------------------- #

#: The abstract screening criteria, transcribed from ``SCREENING_CRITERIA``
#: in ``Scripts/coding_web/screening_lib.py``.
_ABSTRACT_SEED = [
    {
        "key": "C1",
        "label": "Misinformation intervention",
        "kind": "verdict",
        "options": ["Yes", "No", "Unsure"],
        "pass_values": ["Yes"],
        "exclude_reason": "No misinformation intervention (C1)",
        "gating": True,
        "help": "Does the study feature an intervention aimed at reducing "
                "susceptibility to misinformation (inoculation/prebunking, "
                "debunking, media literacy, source-credibility or "
                "lateral-reading training, accuracy nudges, social norms, "
                "platform exposure reduction)?",
        "prompt_text": "Does the study feature an intervention aimed at reducing "
                       "susceptibility to misinformation (inoculation/prebunking, "
                       "debunking, media literacy, source-credibility or "
                       "lateral-reading training, accuracy nudges, social norms, "
                       "platform exposure reduction)?",
        "keywords": [
            "misinformation", "disinformation", "fake news", "fake-news",
            "conspiracy", "conspirac", "false", "falsely", "misleading",
            "misperception", "rumor", "rumour", "hoax", "propaganda",
            "post-truth", "deepfake", "manipulat", "debunk", "prebunk",
            "inoculat", "media literacy", "fact-check", "fact check",
            "correction", "vaccine", "anti-vax", "climate change",
            "lateral reading", "source credibility", "accuracy", "truth",
            "credibility", "headline",
        ],
    },
    {
        "key": "C2",
        "label": "Control group",
        "kind": "verdict",
        "options": ["Yes", "No", "Unsure"],
        "pass_values": ["Yes"],
        "exclude_reason": "No control/comparison group (C2)",
        "gating": True,
        "help": "Does the study include a control, placebo, or comparison group "
                "that does NOT receive the misinformation intervention?",
        "prompt_text": "Does the study include a control, placebo, or comparison "
                       "group that does NOT receive the misinformation "
                       "intervention?",
        "keywords": [
            "control group", "control condition", "control", "comparison",
            "compared", "placebo", "wait-list", "waitlist", "wait list",
            "baseline", "no intervention", "no treatment", "treatment as usual",
            "business as usual", "sham", "randomi", "assigned", "condition",
            "intervention group", "experimental group",
        ],
    },
    {
        "key": "C3",
        "label": "Language (English)",
        "kind": "verdict",
        "options": ["Yes", "No", "Unsure"],
        "pass_values": ["Yes"],
        "exclude_reason": "Not English (C3)",
        "gating": True,
        "help": "Is the paper in English?",
        "prompt_text": "Is the paper in English?",
        "keywords": [
            "N =", "N=", "n =", "n=", "N:", "n:", "participants", "respondents",
            "subjects", "sample", "recruited", "surveyed", "students", "adults",
            "MTurk", "Prolific", "we surveyed", "we recruited", "sample size",
        ],
    },
    {
        "key": "C4",
        "label": "Experimental (primary quantitative study)",
        "kind": "verdict",
        "options": ["Yes", "No", "Unsure"],
        "pass_values": ["Yes"],
        "exclude_reason": "Not a primary experimental study (C4)",
        "gating": True,
        "help": "Is this a primary empirical study with a quantitative "
                "experimental or quasi-experimental design?",
        "prompt_text": "Is this a primary empirical study with a quantitative "
                       "experimental or quasi-experimental design?",
        "keywords": [
            "experiment", "experimental", "randomized", "randomised",
            "randomly assigned", "trial", "field experiment", "online experiment",
            "survey experiment", "study", "studies", "quasi-experimental",
            "pretest", "posttest", "pre-test", "post-test", "between-subjects",
            "within-subjects", "ANOVA", "regression", "we conducted", "we ran",
        ],
    },
]

#: The full-text exclusion reasons, transcribed from ``FULLTEXT_CRITERIA`` in
#: ``Scripts/coding_web/fulltext_lib.py``.
_FULLTEXT_VALUES = [
    ("C1", "No misinformation intervention (C1)"),
    ("C2", "No control/comparison group (C2)"),
    ("C3", "Not English (C3)"),
    ("C4", "Not a primary experimental study (C4)"),
    ("irretrievable", "Fulltext irretrievable"),
    ("no_contrast", "No eligible contrast reported"),
    ("duplicate", "Duplicate/non-independent report of an included study"),
    ("observational", "No experimental intervention (observational)"),
    ("domain_mismatch",
     "Outcome/intervention outside misinformation susceptibility"),
    ("other", "Other (specify)"),
]


#: The abstract criteria that carry **no** auto-approval rules.  C3 is the
#: language check: the screening tab decides it with its English detector,
#: so a keyword rule would approve a non-English paper.
_NO_AUTO_APPROVE = ("C3",)


def _rules_from_keywords(keywords):
    """The auto-approval rules a keyword list becomes.

    The seeded criteria carry the keyword hints the screening tab used to
    hardcode (``CRIT_KEYWORDS`` in ``static/screening.js``).  Each keyword
    becomes a rule of its own: the name is the keyword, the expression is
    the keyword read as a regular expression.  The matching is
    case-insensitive, so a rule matches exactly the text the old substring
    check matched - the seeded rules keep the old behaviour, and the
    criteria editor is where they are tightened.
    """
    out = {}
    for word in keywords or []:
        name = str(word).strip()
        if not name or name in out:
            continue
        out[name] = name
    return out


def default_abstract_criteria():
    """The seeded abstract-screening criteria (the four inclusion criteria).

    Transcribed from ``SCREENING_CRITERIA`` in
    ``Scripts/coding_web/screening_lib.py``: the same keys (``C1`` ...
    ``C4``), the same titles, the same instruction text (as ``prompt_text``
    *and* ``help``) and the same Yes/No/Unsure options.  The
    ``exclude_reason`` values are the PRISMA labels of
    ``Scripts/prisma_report.py`` (``CRITERION_REASON``).

    The keyword hints are the ``CRIT_KEYWORDS`` of
    ``Scripts/coding_web/static/screening.js``; each of them also becomes
    an auto-approval rule, so the screening tab's pre-fill is driven by
    the scheme's criteria and not by a hardcoded table any more.
    """
    out = []
    for index, entry in enumerate(_ABSTRACT_SEED):
        criterion = normalise_criterion(entry, index)
        if criterion["key"] not in _NO_AUTO_APPROVE:
            criterion["auto_approve"] = _rules_from_keywords(criterion["keywords"])
        out.append(criterion)
    return out


def default_fulltext_criteria():
    """The seeded full-text exclusion reasons.

    Transcribed from ``FULLTEXT_CRITERIA`` in
    ``Scripts/coding_web/fulltext_lib.py``: the same values (``C1`` ...
    ``C4`` plus ``irretrievable``, ``no_contrast``, ``duplicate``,
    ``observational``, ``domain_mismatch``, ``other``), the same labels and
    the same order.

    The full-text stage records **one** reason per study, so these criteria
    are a reason vocabulary and not a conjunction: every entry is a
    ``classify`` criterion whose options are the ten reason codes, and none
    of them gates inclusion on its own (``gating`` is ``False``).  The
    PRISMA label of the reason the coder picked is that criterion's
    ``exclude_reason``.

    The full-text stage has no auto-approval rules: the abstract text is
    not what the full-text decision is made on.
    """
    codes = [value for value, _label in _FULLTEXT_VALUES]
    out = []
    for index, (value, label) in enumerate(_FULLTEXT_VALUES):
        out.append(normalise_criterion({
            "key": value,
            "label": label,
            "kind": "classify",
            "options": list(codes),
            "pass_values": [],
            "exclude_reason": label,
            "gating": False,
            "help": label,
            "prompt_text": label,
            "keywords": [],
        }, index))
    return out


def default_criteria():
    """The seeded criteria document (both stages, version 1)."""
    return {
        "version": 1,
        "abstract": default_abstract_criteria(),
        "fulltext": default_fulltext_criteria(),
    }
