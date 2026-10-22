"""validate_scheme.py - the acceptance gate for a project's coding scheme.

A *scheme* is the user-editable description of what a coder records
(``Scripts/scheme_lib.py``).  This script asks the seven questions that decide
whether a scheme may be trusted by the desktop app and by the analysis
pipeline, and writes the answer to ``Results/``:

  C1  the scheme loads and normalises (no exception);
  C2  ``scheme_lib.validate_scheme`` reports no errors (warnings are listed but
      do not fail the run);
  C3  role integrity: every role is claimed by at most one field, and no field
      name claims two roles (the ``N``/``n`` collision class);
  C4  corpus conformance: every key in ``Extraction/*.json`` and
      ``Codings/*.json`` is a scheme field or a structural key.  A corpus key
      the scheme does not declare is a WARNING (the corpus carries legacy
      fields; that must not fail the build).  A scheme field that never occurs
      in the corpus is an INFO;
  C5  legacy fallback: the legacy ``Scripts/coding_web/schema.json`` still
      converts to the same set of field names, so the fallback path cannot
      silently change the coding form;
  C6  criteria: every criterion key that occurs in ``Screening/*.json`` is
      declared by ``Project/criteria.json`` (a missing key would drop a
      verdict);
  C7  the derived block list is non-empty and every block field exists.

Usage
  python Scripts/validate_scheme.py [--project-root DIR] [--no-corpus] [--json]
                                   [--out-dir Results]

  --project-root  the project folder (default: the project this script belongs
                  to - the folder that holds ``Scripts/``).  The gate does not
                  follow ``project_context.active_root()``: that resolver
                  honours the desktop app's saved folder, so a running app
                  could redirect the pipeline gate at another project;
  --no-corpus     skip the corpus scan (C4) and the screening scan (C6) - the
                  fast mode the pipeline gate uses;
  --json          print the report as JSON on stdout;
  --out-dir       where the products go (default: ``Results`` inside the
                  project).

Products (always written, even when the run fails)
  Results/scheme_validation.csv         one row per check;
  Results/scheme_validation.json        the same plus a summary;
  Results/scheme_validation_summary.md  the human-readable summary.

Exit code: 0 when no check reports a FAIL, 1 otherwise.

The CSV carries one row per check (C1..C7).  A check may add detail rows whose
``check_id`` carries a suffix (``C4.unused``); the canonical rows are always
present.  The script only reads the corpus folders and only writes into the
output directory.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import project_context as PC  # noqa: E402
import scheme_legacy as SLEG  # noqa: E402
import scheme_lib as SL  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover - older interpreters
    pass

#: Record keys that carry structure rather than a coded value.
STRUCTURAL_KEYS = ("DV_Metrics", "Interventions", "source_spans")

#: Screening keys that are not inclusion criteria.  The abstract screening
#: files store the intervention-type classification next to the C1-C4
#: verdicts; it is not one of the inclusion criteria (the app's screening
#: writer drops it, and ``agreement_lib`` scores C1..C4 only), so it is
#: treated as structure here - otherwise every project would fail C6 over a
#: verdict the pipeline already ignores.
SCREENING_STRUCTURAL_KEYS = ("Intervention",)

#: The corpus folders C4 scans.
CORPUS_DIRS = ("Extraction", "Codings")

#: The screening folder C6 scans.
SCREENING_DIR = "Screening"

#: Product file names.
CSV_NAME = "scheme_validation.csv"
JSON_NAME = "scheme_validation.json"
MD_NAME = "scheme_validation_summary.md"

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
INFO = "INFO"

#: How many offenders the corpus report names.
TOP_OFFENDERS = 25


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _row(check_id, level, message, detail=""):
    """One report row."""
    return {"check_id": check_id, "level": level, "message": message,
            "detail": str(detail or "")}


def _rel(path, root):
    """``path`` relative to ``root`` when it is inside it, else the path."""
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return path


def _read_json(path):
    """``(document, error)`` - the error is ``None`` on success."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _scheme_stats(scheme):
    """A one-line census of a scheme."""
    return (f"{len(scheme.get('fields') or [])} study fields, "
            f"{len((scheme.get('metrics') or {}).get('fields') or [])} metric fields, "
            f"{len(scheme.get('groups') or [])} groups, "
            f"{len(scheme.get('modules') or {})} modules, "
            f"{len(SL.all_fields(scheme))} fields in total")


# --------------------------------------------------------------------------- #
# C1 - the scheme loads
# --------------------------------------------------------------------------- #

def check_c1(root):
    """C1: load and normalise the project's scheme.

    Returns ``(row, scheme, source)``.  ``scheme`` is ``None`` when the
    project has no usable scheme at all.
    """
    paths = PC.project_paths(root)
    scheme_file = PC.scheme_path(PC.active_scheme_id(root) or "")
    legacy_file = PC.legacy_schema_path(root)

    if os.path.isfile(scheme_file):
        raw, err = _read_json(scheme_file)
        if err:
            return (_row("C1", FAIL,
                         "Project/scheme.json cannot be read",
                         f"{_rel(scheme_file, root)}: {err}"), None, scheme_file)
        if not isinstance(raw, dict) or not raw:
            return (_row("C1", FAIL,
                         "Project/scheme.json is empty or not a JSON object",
                         _rel(scheme_file, root)), None, scheme_file)
        try:
            scheme = SL.normalise_scheme(raw)
        except Exception as exc:  # noqa: BLE001 - the gate must survive anything
            return (_row("C1", FAIL,
                         "Project/scheme.json does not normalise",
                         f"{_rel(scheme_file, root)}: {type(exc).__name__}: {exc}"),
                    None, scheme_file)
        return (_row("C1", PASS,
                     f"the scheme loads from Project/scheme.json "
                     f"({_scheme_stats(scheme)})",
                     _rel(scheme_file, root)), scheme, scheme_file)

    if not os.path.isfile(legacy_file):
        return (_row("C1", FAIL,
                     "the project has no scheme",
                     f"neither {_rel(scheme_file, root)} nor the legacy fallback "
                     f"{_rel(legacy_file, root)} exists"), None, None)

    raw, err = _read_json(legacy_file)
    if err:
        return (_row("C1", FAIL, "the legacy schema cannot be read",
                     f"{_rel(legacy_file, root)}: {err}"), None, legacy_file)
    try:
        scheme = SLEG.convert_to_normalised(raw)
    except Exception as exc:  # noqa: BLE001
        return (_row("C1", FAIL, "the legacy schema does not convert into a scheme",
                     f"{_rel(legacy_file, root)}: {type(exc).__name__}: {exc}"),
                None, legacy_file)
    return (_row("C1", PASS,
                 f"the scheme loads from the legacy fallback "
                 f"({_scheme_stats(scheme)})",
                 f"{_rel(legacy_file, root)} (the project has no "
                 f"{_rel(scheme_file, root)})"), scheme, legacy_file)


# --------------------------------------------------------------------------- #
# C2 - structural validation
# --------------------------------------------------------------------------- #

def check_c2(scheme):
    """C2: ``scheme_lib.validate_scheme`` reports no errors."""
    problems = SL.validate_scheme(scheme)
    errors = [p for p in problems if p.get("level") == "error"]
    warnings = [p for p in problems if p.get("level") == "warning"]
    if errors:
        detail = "\n".join(
            f"[{p.get('code')}] {p.get('message')}"
            + (f"  ({p.get('where')})" if p.get("where") else "")
            for p in errors)
        return [_row("C2", FAIL,
                     f"scheme_lib.validate_scheme reports {len(errors)} error(s)",
                     detail)]
    if warnings:
        detail = "\n".join(
            f"[{p.get('code')}] {p.get('message')}"
            + (f"  ({p.get('where')})" if p.get("where") else "")
            for p in warnings)
        return [_row("C2", WARN,
                     f"scheme_lib.validate_scheme reports 0 errors and "
                     f"{len(warnings)} warning(s)",
                     detail)]
    return [_row("C2", PASS,
                 "scheme_lib.validate_scheme reports 0 errors and 0 warnings",
                 "")]


# --------------------------------------------------------------------------- #
# C3 - role integrity
# --------------------------------------------------------------------------- #

def check_c3(scheme):
    """C3: every role is claimed by at most one field name."""
    owners = {}
    for field in SL.all_fields(scheme):
        role = field.get("role")
        if not role:
            continue
        owners.setdefault(role, []).append((field.get("name"), field.get("scope")))
    # A role claimed by two *different* field names is the collision class;
    # the same name twice is a duplicate field (C2 reports that).
    shared = {role: names for role, names in owners.items()
              if len({n for n, _ in names}) > 1}

    # The same field name in two scopes with two different roles is the other
    # half of the collision class: ``role_map`` would silently keep one of them.
    by_name = {}
    for field in SL.all_fields(scheme):
        role = field.get("role")
        if not role:
            continue
        by_name.setdefault(field.get("name"), set()).add(role)
    split = {name: sorted(roles) for name, roles in by_name.items() if len(roles) > 1}

    if shared or split:
        detail = []
        for role, names in sorted(shared.items()):
            detail.append(f"role '{role}' is claimed by "
                          + ", ".join(f"'{n}' ({s})" for n, s in names))
        for name, roles in sorted(split.items()):
            detail.append(f"field '{name}' claims several roles: " + ", ".join(roles))
        return [_row("C3", FAIL,
                     f"role integrity broken: {len(shared)} shared role(s), "
                     f"{len(split)} split field name(s)",
                     "\n".join(detail))]

    # The inverse map must be a true inverse of the forward map.
    forward = SL.role_map(scheme)
    inverse = SL.role_inverse(scheme)
    if len(inverse) != len(forward):
        return [_row("C3", FAIL,
                     "role_map and role_inverse disagree",
                     f"{len(forward)} field->role entries but only "
                     f"{len(inverse)} distinct roles")]

    return [_row("C3", PASS,
                 f"every role is claimed by at most one field "
                 f"({len(forward)} roles in use)",
                 "")]


# --------------------------------------------------------------------------- #
# C4 - corpus conformance
# --------------------------------------------------------------------------- #

def scan_corpus(root):
    """Read the corpus once and census every key it carries.

    Returns a dict with the key counters, the first study key each key was
    seen in, the counters of the ``Interventions`` entries, and the scan
    statistics.
    """
    counter = Counter()
    first = {}
    inner = Counter()
    files = records = dv_rows = interventions = 0
    failures = []
    started = time.perf_counter()

    for folder in CORPUS_DIRS:
        directory = os.path.join(root, folder)
        if not os.path.isdir(directory):
            continue
        for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
            key = os.path.splitext(os.path.basename(path))[0]
            data, err = _read_json(path)
            if err:
                failures.append(f"{_rel(path, root)}: {err}")
                continue
            files += 1
            if not isinstance(data, list):
                data = [data]
            for rec in data:
                if not isinstance(rec, dict):
                    continue
                records += 1
                for name in rec:
                    counter[name] += 1
                    first.setdefault(name, key)
                for dv in (rec.get("DV_Metrics") or []):
                    if not isinstance(dv, dict):
                        continue
                    dv_rows += 1
                    for name in dv:
                        counter[name] += 1
                        first.setdefault(name, key)
                for iv in (rec.get("Interventions") or []):
                    if isinstance(iv, dict):
                        interventions += 1
                        for name in iv:
                            inner[name] += 1

    return {"keys": counter, "first": first, "intervention_keys": inner,
            "files": files, "records": records, "dv_rows": dv_rows,
            "interventions": interventions, "failures": failures,
            "elapsed_s": time.perf_counter() - started}


def check_c4(root, scheme):
    """C4: the corpus conforms to the scheme."""
    census = scan_corpus(root)
    scheme_names = {f["name"] for f in SL.all_fields(scheme)}
    known = scheme_names | set(STRUCTURAL_KEYS)

    unknown = [(name, n) for name, n in census["keys"].items() if name not in known]
    unknown.sort(key=lambda pair: (-pair[1], pair[0]))
    unused = sorted(scheme_names - set(census["keys"]))

    rows = []
    detail = []
    if census["failures"]:
        detail.append(f"{len(census['failures'])} file(s) could not be read:")
        detail.extend("  " + f for f in census["failures"][:10])
    detail.append(
        f"scanned {census['files']} file(s), {census['records']} record(s), "
        f"{census['dv_rows']} DV row(s) in {census['elapsed_s']:.1f}s; "
        f"{len(census['keys'])} distinct key(s), {len(scheme_names)} scheme field(s)")
    if unknown:
        detail.append(f"{len(unknown)} corpus key(s) are not scheme fields "
                      f"(structural keys {', '.join(STRUCTURAL_KEYS)} excluded):")
        for name, n in unknown[:TOP_OFFENDERS]:
            detail.append(f"  {name}: {n} (first: {census['first'].get(name, '?')})")
        if len(unknown) > TOP_OFFENDERS:
            detail.append(f"  ... and {len(unknown) - TOP_OFFENDERS} more "
                          f"(only the {TOP_OFFENDERS} most frequent are listed)")
        rows.append(_row("C4", WARN,
                          f"{len(unknown)} corpus key(s) are not declared by the scheme",
                          "\n".join(detail)))
    else:
        rows.append(_row("C4", PASS,
                          "every corpus key is a scheme field or a structural key",
                          "\n".join(detail)))

    if unused:
        rows.append(_row("C4.unused", INFO,
                         f"{len(unused)} scheme field(s) never occur in the corpus",
                         ", ".join(unused)))
    else:
        rows.append(_row("C4.unused", INFO,
                         "every scheme field occurs in the corpus", ""))

    if census["intervention_keys"]:
        top = ", ".join(f"{name} ({n})" for name, n
                        in census["intervention_keys"].most_common(TOP_OFFENDERS))
        rows.append(_row("C4.interventions", INFO,
                         f"{census['interventions']} Intervention(s) entry/entries carry "
                         f"{len(census['intervention_keys'])} inner key(s) "
                         f"(not part of the conformance pool)",
                         top))
    return rows


# --------------------------------------------------------------------------- #
# C5 - the legacy fallback
# --------------------------------------------------------------------------- #

def check_c5(root, scheme):
    """C5: the legacy schema still converts to the same field names."""
    legacy_file = PC.legacy_schema_path(root)
    if not os.path.isfile(legacy_file):
        return [_row("C5", INFO,
                     "skipped: the project has no legacy schema to fall back to",
                     _rel(legacy_file, root))]

    legacy, err = _read_json(legacy_file)
    if err:
        return [_row("C5", FAIL, "the legacy schema cannot be read",
                     f"{_rel(legacy_file, root)}: {err}")]
    try:
        converted = SLEG.convert_to_normalised(legacy)
    except Exception as exc:  # noqa: BLE001
        return [_row("C5", FAIL, "the legacy schema does not convert into a scheme",
                     f"{_rel(legacy_file, root)}: {type(exc).__name__}: {exc}")]

    converted_names = {f["name"] for f in SL.all_fields(converted)}
    declared = SLEG.legacy_field_names(legacy)
    dropped = sorted(declared - converted_names)
    if dropped:
        return [_row("C5", FAIL,
                     f"the legacy conversion drops {len(dropped)} field name(s)",
                     "the fallback would change the coding form:\n  "
                     + "\n  ".join(dropped))]

    project_file = PC.scheme_path(PC.active_scheme_id(root) or "")
    project_names = {f["name"] for f in SL.all_fields(scheme)}
    if not os.path.isfile(project_file):
        return [_row("C5", PASS,
                     f"the legacy conversion is lossless "
                     f"({len(converted_names)} field names); it is the active scheme",
                     f"{_rel(legacy_file, root)}; the project has no "
                     f"{_rel(project_file, root)}")]

    only_legacy = sorted(converted_names - project_names)
    only_project = sorted(project_names - converted_names)
    if only_legacy or only_project:
        detail = []
        if only_legacy:
            detail.append("only the legacy conversion declares them ("
                          + ", ".join(only_legacy) + ")")
        if only_project:
            detail.append("only Project/scheme.json declares them ("
                          + ", ".join(only_project) + ")")
        return [_row("C5", FAIL,
                     "the legacy fallback and Project/scheme.json declare "
                     "different field names",
                     "the fallback path would silently change the coding form:\n  "
                     + "\n  ".join(detail))]
    return [_row("C5", PASS,
                 f"the legacy conversion matches Project/scheme.json "
                 f"({len(converted_names)} field names)",
                 _rel(legacy_file, root))]


# --------------------------------------------------------------------------- #
# C6 - the criteria
# --------------------------------------------------------------------------- #

def _criterion_entries(doc, stage):
    """The criterion entries of one stage of a criteria document."""
    entries = None
    if isinstance(doc, dict):
        entries = doc.get(stage)
        if entries is None and stage == "abstract":
            entries = doc.get("criteria")
    elif isinstance(doc, list):
        entries = doc
    if isinstance(entries, dict):
        out = []
        for key, value in entries.items():
            entry = dict(value) if isinstance(value, dict) else {}
            entry.setdefault("key", key)
            out.append(entry)
        return out
    if isinstance(entries, list):
        return [e for e in entries if isinstance(e, dict)]
    return []


def _criterion_key(entry):
    """The key of one criterion entry."""
    for field in ("key", "value", "id", "code", "label"):
        value = entry.get(field)
        if value:
            return str(value).strip()
    return ""


def _declared_keys(root, doc):
    """The criterion keys the project's criteria file declares, per stage.

    The normalised criteria (via ``project_context.criteria()``) are
    preferred; when the criteria model is not installed the document is read
    directly.
    """
    declared = {}
    normalised = None
    try:
        normalised = PC.criteria(root)
    except Exception:  # noqa: BLE001 - the criteria model is optional
        normalised = None
    for stage in ("abstract", "fulltext"):
        keys = []
        if isinstance(normalised, dict):
            for entry in normalised.get(stage) or []:
                if isinstance(entry, dict) and entry.get("key"):
                    keys.append(str(entry["key"]).strip())
        if not keys:
            for entry in _criterion_entries(doc, stage):
                key = _criterion_key(entry)
                if key:
                    keys.append(key)
        declared[stage] = keys
    return declared


def scan_screening(root):
    """The criterion keys the screening files carry.

    Abstract screening stores a ``criteria`` mapping per record; the full-text
    screening stores a single ``criterion`` value per record.
    """
    directory = os.path.join(root, SCREENING_DIR)
    abstract = Counter()
    fulltext = Counter()
    first = {}
    files = 0
    failures = []
    started = time.perf_counter()

    if os.path.isdir(directory):
        for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
            name = os.path.basename(path)
            data, err = _read_json(path)
            if err:
                failures.append(f"{name}: {err}")
                continue
            files += 1
            records = data.values() if isinstance(data, dict) else (
                data if isinstance(data, list) else [])
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                crits = rec.get("criteria")
                if isinstance(crits, dict):
                    for key in crits:
                        abstract[str(key)] += 1
                        first.setdefault(str(key), name)
                crit = rec.get("criterion")
                if isinstance(crit, str) and crit.strip():
                    crit = crit.strip()
                    fulltext[crit] += 1
                    first.setdefault(crit, name)

    return {"abstract": abstract, "fulltext": fulltext, "first": first,
            "files": files, "failures": failures,
            "elapsed_s": time.perf_counter() - started}


def check_c6(root):
    """C6: every criterion key in the corpus is declared by the criteria file."""
    criteria_file = PC.criteria_path(PC.active_scheme_id(root) or "")
    if not os.path.isfile(criteria_file):
        return [_row("C6", INFO,
                     "skipped: the project has no Project/criteria.json",
                     _rel(criteria_file, root))]

    doc, err = _read_json(criteria_file)
    if err:
        return [_row("C6", FAIL, "Project/criteria.json cannot be read",
                     f"{_rel(criteria_file, root)}: {err}")]

    declared = _declared_keys(root, doc)
    duplicates = []
    declared_sets = {}
    for stage, keys in declared.items():
        seen = set()
        for key in keys:
            if key in seen:
                duplicates.append(f"{stage}:{key}")
            seen.add(key)
        declared_sets[stage] = seen

    corpus = scan_screening(root)
    structural = set(SCREENING_STRUCTURAL_KEYS)
    missing = {}
    for stage in ("abstract", "fulltext"):
        gap = sorted(set(corpus[stage]) - declared_sets[stage] - structural)
        if gap:
            missing[stage] = gap

    detail = [f"criteria: {len(declared_sets['abstract'])} abstract key(s), "
              f"{len(declared_sets['fulltext'])} full-text key(s); "
              f"screening: {corpus['files']} file(s) scanned in "
              f"{corpus['elapsed_s']:.1f}s, "
              f"{len(corpus['abstract'])} abstract key(s), "
              f"{len(corpus['fulltext'])} full-text key(s)"]
    if corpus["failures"]:
        detail.append("files that could not be read: " + "; ".join(corpus["failures"][:5]))
    treated = sorted(structural & (set(corpus["abstract"]) | set(corpus["fulltext"])))
    if treated:
        detail.append("treated as structure, not as criteria: " + ", ".join(treated))
    if duplicates:
        detail.append("duplicate declared key(s): " + ", ".join(sorted(set(duplicates))))
    for stage, gap in missing.items():
        detail.append(f"{stage} criterion key(s) in {SCREENING_DIR}/ but not declared: "
                      + ", ".join(f"{k} (first: {corpus['first'].get(k, '?')})"
                                  for k in gap))

    if duplicates or missing:
        return [_row("C6", FAIL,
                     "the criteria file does not cover the screening corpus"
                     + (f" ({len(duplicates)} duplicate key(s))" if duplicates else ""),
                     "\n".join(detail))]
    return [_row("C6", PASS,
                 "every criterion key in the screening corpus is declared, and "
                 "every declared key is unique",
                 "\n".join(detail))]


# --------------------------------------------------------------------------- #
# C7 - the derived blocks
# --------------------------------------------------------------------------- #

def check_c7(scheme):
    """C7: the derived block list is non-empty and every block field exists."""
    blocks = SL.derive_blocks(scheme)
    if not blocks:
        return [_row("C7", FAIL, "the derived block list is empty",
                     "the scheme declares no group and no module block")]

    names = {f["name"] for f in SL.all_fields(scheme)}
    missing = []
    empty = []
    for block in blocks:
        fields = block.get("fields") or []
        if not fields:
            empty.append(block.get("id"))
        for name in fields:
            if name not in names:
                missing.append(f"{block.get('id')} -> '{name}'")

    if missing:
        return [_row("C7", FAIL,
                     f"{len(missing)} block field(s) are not declared by the scheme",
                     "\n".join(missing))]

    kinds = Counter(b.get("kind") for b in blocks)
    detail = f"{len(blocks)} block(s): " + ", ".join(
        f"{n} {kind} block(s)" for kind, n in sorted(kinds.items()))
    if empty:
        detail += f"; empty block(s): {', '.join(str(b) for b in empty)}"
    return [_row("C7", PASS,
                 f"the derived block list holds {len(blocks)} block(s) and every "
                 f"block field exists",
                 detail)]


# --------------------------------------------------------------------------- #
# Products
# --------------------------------------------------------------------------- #

def write_csv(path, rows):
    """Write the check rows as CSV."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["check_id", "level", "message", "detail"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_json(path, doc):
    """Write the report as JSON."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def write_summary(path, doc, rows):
    """Write the human-readable summary."""
    summary = doc["summary"]
    out = []
    out.append(f"# Scheme validation - {doc['generated_at']}")
    out.append("")
    out.append(f"- **Project:** `{doc['project_root']}`")
    out.append(f"- **Scheme:** `{doc['scheme_source']}`")
    out.append(f"- **Result:** {'FAIL' if summary['errors'] else 'PASS'} - "
               f"{summary['errors']} error(s), {summary['warnings']} warning(s), "
               f"{summary['infos']} info(s)")
    out.append(f"- **Runtime:** {summary['elapsed_s']:.1f} s")
    if doc.get("note"):
        out.append(f"- **Note:** {doc['note']}")
    out.append("")
    out.append("## Checks")
    out.append("")
    out.append("| check | level | what it asks | result |")
    out.append("|---|---|---|---|")
    asked = {
        "C1": "the scheme loads and normalises",
        "C2": "scheme_lib.validate_scheme reports no errors",
        "C3": "every role is claimed by at most one field",
        "C4": "every corpus key is a scheme field or a structural key",
        "C5": "the legacy fallback converts to the same field names",
        "C6": "every screening criterion key is declared by the criteria file",
        "C7": "the derived blocks are non-empty and reference real fields",
    }
    for row in rows:
        if row["check_id"] in asked:
            out.append(f"| {row['check_id']} | {row['level']} | "
                       f"{asked[row['check_id']]} | {_md(row['message'])} |")
    out.append("")

    detail_rows = [r for r in rows if r["check_id"] not in asked]
    if detail_rows:
        out.append("## Detail rows")
        out.append("")
        for row in detail_rows:
            out.append(f"- **{row['check_id']} ({row['level']})** - "
                       f"{_md(row['message'])}")
            if row["detail"]:
                out.append("")
                out.append("  ```")
                for line in str(row["detail"]).splitlines():
                    out.append("  " + line)
                out.append("  ```")
        out.append("")

    out.append("## What to do about each WARN")
    out.append("")
    warns = [r for r in rows if r["level"] == WARN]
    if not warns:
        out.append("No warnings. Nothing to do.")
    else:
        out.append("A WARN never fails the build, but each one is a decision to make:")
        out.append("")
        for row in warns:
            out.append(f"### {row['check_id']} - {row['message']}")
            out.append("")
            out.append(_advice(row))
            out.append("")
    out.append("## Full detail")
    out.append("")
    for row in rows:
        out.append(f"### {row['check_id']} ({row['level']}) - {row['message']}")
        out.append("")
        if row["detail"]:
            out.append("```")
            out.append(str(row["detail"]))
            out.append("```")
        out.append("")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out).rstrip() + "\n")


def _md(text):
    """A cell-safe rendering of a message."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def _advice(row):
    """What a reader should do about a warning."""
    if row["check_id"] == "C4":
        return ("The corpus carries keys the scheme does not declare.  Either\n"
                "declare them in the scheme (when they are real coded fields) or\n"
                "leave them: the coding form renders unknown record keys in its\n"
                "\"Additional fields (not in schema)\" section, so they are not\n"
                "lost.  The list names the key, how often it occurs and the study\n"
                "key it was first seen in - the ones with a high count are usually\n"
                "a field the scheme should declare.")
    if row["check_id"] == "C2":
        return ("scheme_lib.validate_scheme raised a warning.  Each one is listed\n"
                "in the detail column with the field it belongs to.  Warnings do\n"
                "not fail the build; fix them when they point at a real problem\n"
                "(a field pointing at an unknown block, a select field without\n"
                "options, a metric field without a role).")
    return ("Look at the detail column: it names the offending item.  This\n"
            "warning does not fail the build, but it should be resolved or\n"
            "explicitly accepted before the scheme is published.")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def run(root, scan=True):
    """Run every check; returns ``(rows, scheme, source, elapsed_s)``."""
    started = time.perf_counter()
    rows = []

    row, scheme, source = check_c1(root)
    rows.append(row)
    if scheme is None:
        for cid, what in (("C2", "structural validation"),
                          ("C3", "role integrity"),
                          ("C4", "corpus conformance"),
                          ("C5", "legacy fallback"),
                          ("C6", "criteria coverage"),
                          ("C7", "derived blocks")):
            rows.append(_row(cid, INFO,
                             f"not run: the scheme could not be loaded ({what})",
                             ""))
        return rows, None, source, time.perf_counter() - started

    rows.extend(check_c2(scheme))
    rows.extend(check_c3(scheme))
    if scan:
        rows.extend(check_c4(root, scheme))
    else:
        rows.append(_row("C4", INFO, "skipped: the corpus scan was not requested",
                         "(--no-corpus)"))
    rows.extend(check_c5(root, scheme))
    if scan:
        rows.extend(check_c6(root))
    else:
        rows.append(_row("C6", INFO, "skipped: the screening scan was not requested",
                         "(--no-corpus)"))
    rows.extend(check_c7(scheme))
    rows.extend(check_c8(scheme))
    return rows, scheme, source, time.perf_counter() - started


def _resolve_root(args):
    """The project the gate validates, and a note about the app's project.

    The gate belongs to the pipeline, so it validates the project it ships
    with - the folder that holds ``Scripts/`` - unless ``--project-root``
    says otherwise.  It deliberately does **not** follow
    ``project_context.active_root()``: that resolver honours the desktop
    app's saved folder (``CODING_STUDIO_ROOT``), so a running app could
    silently redirect the pipeline gate at another project.
    """
    if args.project_root:
        return os.path.abspath(args.project_root), None
    root = os.path.abspath(PC.default_root())
    note = None
    try:
        active = os.path.abspath(PC.active_root())
    except Exception:  # noqa: BLE001 - the resolver is best effort
        active = ""
    if active and active != root:
        note = (f"the app's active project is {active}; the gate validated {root} "
                f"(pass --project-root to validate another project)")
    return root, note


def check_c8(scheme):
    """C8: the legacy view of the scheme is the legacy document, field for field.

    The coder form, the agreement tab and the analysis tab read the *legacy
    view* of the scheme (``studio_lib._schema()``).  If that view drifts from
    the v3.1 document, the form renders different controls, the blank values
    differ (the ``|null`` marker decides whether an empty string is stored as
    ``null``) and the agreement tab scores a different field list.

    So the view must be identical to the legacy document, field for field.
    """
    legacy_path = PC.legacy_schema_path()
    if not legacy_path or not os.path.exists(legacy_path):
        return [_row("C8", INFO,
                     "the project has no legacy schema.json, so there is no "
                     "legacy view to compare the scheme against",
                     legacy_path or "")]
    try:
        with open(legacy_path, encoding="utf-8") as fh:
            legacy = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return [_row("C8", FAIL, "the legacy schema.json could not be read",
                     repr(exc))]

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "coding_web"))
    try:
        import studio_lib
        view = studio_lib.legacy_schema_view(scheme)
        studio_lib._restore_legacy_field_detail(view, legacy)
    except Exception as exc:  # noqa: BLE001
        return [_row("C8", FAIL, "the legacy view could not be rendered",
                     repr(exc))]

    problems = []
    for bucket in ("common_fields", "dv_fields"):
        old = {f.get("name"): f for f in (legacy.get(bucket) or [])}
        new = {f.get("name"): f for f in (view.get(bucket) or [])}
        for name in sorted(set(old) | set(new)):
            if name not in old:
                problems.append(f"{bucket}: '{name}' is in the view but not in "
                                "the legacy document")
                continue
            if name not in new:
                problems.append(f"{bucket}: '{name}' is MISSING from the view")
                continue
            o, n = old[name], new[name]
            for prop in ("label", "group", "type", "base_type", "description",
                         "required", "derived", "module", "editor"):
                if str(o.get(prop)) != str(n.get(prop)):
                    problems.append(f"{bucket}.{name}: {prop} "
                                    f"{o.get(prop)!r} -> {n.get(prop)!r}")
            oo = [(x.get("value"), x.get("label")) for x in (o.get("options") or [])]
            no = [(x.get("value"), x.get("label")) for x in (n.get("options") or [])]
            if oo != no:
                problems.append(f"{bucket}.{name}: options {oo!r} -> {no!r}")
            if (o.get("vocab") or []) != (n.get("vocab") or []):
                problems.append(f"{bucket}.{name}: vocab differs")
    if list(legacy.get("groups") or []) != list(view.get("groups") or []):
        problems.append("the group list differs")
    if sorted((legacy.get("modules") or {})) != sorted((view.get("modules") or {})):
        problems.append("the module list differs")

    if problems:
        return [_row("C8", FAIL,
                     f"the legacy view differs from the legacy document in "
                     f"{len(problems)} place(s)",
                     "\n".join(problems[:25]))]
    return [_row("C8", PASS,
                 "the legacy view is field-for-field identical to the legacy "
                 "document (the coder form renders exactly what it did before)",
                 legacy_path)]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project-root", default=None,
                    help="the project folder (default: the project this script "
                         "belongs to)")
    ap.add_argument("--no-corpus", action="store_true",
                    help="skip the corpus scan (C4) and the screening scan (C6)")
    ap.add_argument("--json", action="store_true",
                    help="print the report as JSON on stdout")
    ap.add_argument("--out-dir", default="Results",
                    help="where the products go (default: Results)")
    args = ap.parse_args(argv)

    root, note = _resolve_root(args)
    PC.set_root(root)
    out_dir = args.out_dir if os.path.isabs(args.out_dir) else os.path.join(root, args.out_dir)

    rows, scheme, source, elapsed = run(root, scan=not args.no_corpus)

    errors = sum(1 for r in rows if r["level"] == FAIL)
    warnings = sum(1 for r in rows if r["level"] == WARN)
    infos = sum(1 for r in rows if r["level"] == INFO)
    passes = sum(1 for r in rows if r["level"] == PASS)

    doc = {
        "generated_at": PC.now(),
        "project_root": root,
        "scheme_source": _rel(source, root) if source else "none",
        "corpus_scanned": not args.no_corpus,
        "note": note,
        "checks": rows,
        "summary": {
            "errors": errors,
            "warnings": warnings,
            "infos": infos,
            "passes": passes,
            "elapsed_s": round(elapsed, 3),
        },
    }

    csv_path = os.path.join(out_dir, CSV_NAME)
    json_path = os.path.join(out_dir, JSON_NAME)
    md_path = os.path.join(out_dir, MD_NAME)
    write_csv(csv_path, rows)
    write_json(json_path, doc)
    write_summary(md_path, doc, rows)

    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        print(f"validate_scheme: {root}")
        if note:
            print(f"  note: {note}")
        print(f"  scheme: {doc['scheme_source']}"
              + (f" ({_scheme_stats(scheme)})" if scheme else ""))
        print("")
        for row in rows:
            print(f"  {row['check_id']:<16} {row['level']:<5} {row['message']}")
        print("")
        print(f"scheme validation {'FAIL' if errors else 'PASS'} "
              f"({errors} error(s), {warnings} warning(s), {infos} info(s)) "
              f"in {elapsed:.1f}s")
        print(f"products: {csv_path}, {json_path}, {md_path}")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
