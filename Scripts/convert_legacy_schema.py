"""Convert the legacy v3.1 coding schema into this project's own scheme.

Why this script exists
----------------------
The campaign's coding form used to be hardcoded in ``Scripts/coding_web/schema.json``
(v3.1): a flat list of field names, a pile of controlled vocabularies and a
handful of lookups that lived in ``static/app.js``.  The new coding scheme
(:mod:`scheme_lib`) is the same information in a general shape - blocks, roles,
conditions, kinds and rules - so that the desktop app, the analysis pipeline and
the agent tooling all read *one* description of what a coder records.

This script is the one-way bridge between the two.  It reads the v3.1 document,
converts it with :mod:`scheme_legacy` and writes

* ``Project/scheme.json``  - the project's scheme, via
  :func:`project_context.write_scheme` (which normalises, validates and writes
  atomically), and
* ``Project/project.json`` - the project manifest, via
  :func:`project_context.write_manifest`.

The conversion is *name preserving*: every field keeps its v3.1 name, so the
``Extraction/<Key>.json`` and ``Codings/<Key> - <Coder>.json`` files stay valid
byte-for-byte.  ``--check`` proves that - it compares the field-name sets,
validates the scheme, checks the role coverage and the block derivation and
re-runs the legacy fallback, so a project without ``Project/scheme.json`` still
yields exactly the same fields.

Usage
-----
    python Scripts/convert_legacy_schema.py [--schema PATH] [--project-root DIR]
                                             [--out PATH] [--manifest-out PATH]
                                             [--check] [--force] [--quiet]
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import project_context as pc  # noqa: E402
import scheme_legacy  # noqa: E402
import scheme_lib as SL  # noqa: E402

#: The folder this script keeps its intermediates in (``temp/<owner>/``).
TEMP_OWNER = "convert_legacy_schema"

#: The manifest keys the project layer expects, in the order it writes them.
MANIFEST_KEYS = ("id", "name", "label", "description", "created",
                 "scheme", "criteria")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def sha256_of(path):
    """The hex SHA-256 digest of a file."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path):
    """Read a JSON document, or stop with a readable message."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise SystemExit("the schema file does not exist: %s" % path)
    except json.JSONDecodeError as exc:
        raise SystemExit("the schema file is not valid JSON (%s): %s" % (path, exc))


def temp_root():
    """The folder this script writes its intermediates to."""
    path = os.path.join(REPO, "temp", TEMP_OWNER)
    os.makedirs(path, exist_ok=True)
    return path


def atomic_write_json(path, payload):
    """Write a JSON document atomically.

    This mirrors :func:`project_context._atomic_write`: the temporary file is
    created in the *target's own* folder, so ``os.replace`` is an atomic
    rename on the same volume (Windows and Nextcloud lock files are the
    reason it matters).  It is only used for a caller-supplied target outside
    the project folder - the project paths go through ``project_context``.
    """
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    handle, tmp = tempfile.mkstemp(suffix=".json", dir=folder)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=1)
            stream.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def relative_or_absolute(path, root):
    """``path`` relative to ``root`` when it lives inside it, else absolute."""
    absolute = os.path.abspath(path)
    base = os.path.abspath(root)
    try:
        inside = os.path.commonpath([absolute, base]) == base
    except ValueError:
        inside = False
    if not inside:
        return absolute.replace(os.sep, "/")
    return os.path.relpath(absolute, base).replace(os.sep, "/")


# --------------------------------------------------------------------------- #
# Repairing the frozen converter's name shadowing
# --------------------------------------------------------------------------- #

def scheme_name_is_shadowed(scheme):
    """True when the scheme is named after one of its own groups.

    That is the fingerprint of a shadowed ``name`` argument in the converter
    (a loop variable rebinding the parameter).  It used to be repaired
    silently; now it is a loud check, because a silent repair hides the bug.
    """
    group_names = {group.get("name") for group in scheme.get("groups") or []}
    return bool(scheme.get("name")) and scheme.get("name") in group_names


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #

def write_scheme_document(document, root, target):
    """Write the scheme to ``target``.

    The project path goes through :func:`project_context.write_scheme`, which
    normalises the document, refuses a scheme with structural errors and
    writes it atomically.  A caller-supplied target gets the same treatment
    through the local atomic writer.
    """
    default = pc.project_paths(root)["scheme"]
    if os.path.abspath(target) == os.path.abspath(default):
        return pc.write_scheme(document, root)
    normalised = SL.normalise_scheme(document)
    problems = [p for p in SL.validate_scheme(normalised) if p["level"] == "error"]
    if problems:
        raise ValueError("the scheme has structural errors: " +
                         "; ".join(p["message"] for p in problems))
    return atomic_write_json(target, normalised)


def write_manifest_document(document, root, target):
    """Write the manifest to ``target`` (the project path by default)."""
    default = pc.project_paths(root)["manifest"]
    if os.path.abspath(target) == os.path.abspath(default):
        return pc.write_manifest(document, root)
    return atomic_write_json(target, document)


def existing_created(manifest_path):
    """The ``created`` stamp of an existing manifest, or ``None``."""
    try:
        with open(manifest_path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    value = document.get("created")
    return value if isinstance(value, str) and value else None


def build_manifest(scheme, legacy, legacy_path, root, created):
    """The project manifest for a converted scheme.

    ``created`` is the stamp the manifest keeps across runs, so a second
    conversion does not rewrite the project's history.
    """
    manifest = {
        "id": scheme.get("id") or "",
        "name": scheme.get("name") or "",
        "label": scheme.get("label") or "",
        "description": scheme.get("description") or "",
        "created": created,
        "scheme": pc.SCHEME_FILENAME,
        "criteria": pc.CRITERIA_FILENAME,
        "source_schema": {
            "path": relative_or_absolute(legacy_path, root),
            "schema_version": str(legacy.get("schema_version") or ""),
            "sha256": sha256_of(legacy_path),
        },
    }
    return {key: manifest[key] for key in MANIFEST_KEYS + ("source_schema",)}


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #

def scheme_field_names(scheme):
    """Every field name of a scheme (study fields, metric fields, modules)."""
    return {field["name"] for field in SL.all_fields(scheme)}


def fallback_scheme(legacy_path):
    """The scheme ``project_context`` yields when the project has no scheme.

    The fallback is exercised on a throw-away project root that holds a copy
    of the legacy schema and no ``Project/scheme.json``, so the real project
    folder is never touched.
    """
    root = tempfile.mkdtemp(prefix="fallback_", dir=temp_root())
    try:
        target = os.path.join(root, "Scripts", "coding_web", "schema.json")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(legacy_path, target)
        return pc.scheme(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_rows(legacy, scheme, legacy_path, stored_scheme_path):
    """Run the verification suite; return one row per check.

    A row is ``{"check", "status", "detail"}`` with the status ``"PASS"``,
    ``"FAIL"`` or ``"SKIP"``.
    """
    rows = []

    # 0 -- the scheme is named after the project, not after one of its groups.
    if scheme_name_is_shadowed(scheme):
        rows.append({
            "check": "the scheme is named after the project",
            "status": "FAIL",
            "detail": "the scheme is named %r, which is one of its own groups - "
                      "the converter's 'name' argument is shadowed"
                      % scheme.get("name"),
        })
    else:
        rows.append({
            "check": "the scheme is named after the project",
            "status": "PASS",
            "detail": "named %r" % scheme.get("name"),
        })

    # 1 -- the conversion is lossless: every legacy field name survives, and
    #      the only names it ADDS are the declared corpus-only fields.
    legacy_names = scheme_legacy.legacy_field_names(legacy)
    converted_names = scheme_field_names(scheme)
    missing = sorted(legacy_names - converted_names)
    added = sorted(converted_names - legacy_names)
    declared_additions = set(scheme_legacy.CORPUS_ONLY_STUDY_FIELDS and
                              [f["name"] for f in scheme_legacy.CORPUS_ONLY_STUDY_FIELDS] or [])
    declared_additions |= set(f["name"] for f in scheme_legacy.CORPUS_ONLY_METRIC_FIELDS)
    undeclared = sorted(set(added) - declared_additions)
    if missing or undeclared:
        detail = ("%d legacy field name(s) missing (%s); %d undeclared field "
                  "name(s) added (%s)" %
                  (len(missing), ", ".join(missing) or "-",
                   len(undeclared), ", ".join(undeclared) or "-"))
        rows.append({"check": "legacy field names preserved", "status": "FAIL",
                     "detail": detail})
    else:
        rows.append({
            "check": "legacy field names preserved",
            "status": "PASS",
            "detail": "%d field names in, %d out, no loss%s" %
                      (len(legacy_names), len(converted_names),
                       ("; %d declared corpus-only field(s) added (%s)"
                        % (len(added), ", ".join(added))) if added else ""),
        })

    # 2 -- the scheme has no structural errors.
    problems = SL.validate_scheme(scheme)
    errors = [p for p in problems if p["level"] == "error"]
    warnings = [p for p in problems if p["level"] == "warning"]
    if errors:
        detail = "; ".join(p["message"] for p in errors[:5])
        if len(errors) > 5:
            detail += " (and %d more)" % (len(errors) - 5)
        rows.append({"check": "scheme validates", "status": "FAIL",
                     "detail": detail})
    else:
        rows.append({
            "check": "scheme validates",
            "status": "PASS",
            "detail": "%d errors, %d warnings" % (len(errors), len(warnings)),
        })

    # 3 -- every study and metric field carries a role.  Module fields may
    #      carry none; their count is reported as information.
    study_without = [f["name"] for f in scheme.get("fields") or []
                     if not f.get("role")]
    metric_without = [f["name"] for f in (scheme.get("metrics") or {}).get("fields") or []
                      if not f.get("role")]
    module_without = [f["name"] for mod in (scheme.get("modules") or {}).values()
                      for f in mod.get("fields") or [] if not f.get("role")]
    if study_without or metric_without:
        rows.append({
            "check": "study and metric fields carry a role",
            "status": "FAIL",
            "detail": "without a role: %s" %
                      ", ".join(study_without + metric_without),
        })
    else:
        rows.append({
            "check": "study and metric fields carry a role",
            "status": "PASS",
            "detail": "%d study and %d metric fields carry a role; "
                      "%d module field(s) carry none (allowed)" %
                      (len(scheme.get("fields") or []),
                       len((scheme.get("metrics") or {}).get("fields") or []),
                       len(module_without)),
        })

    # 4 -- one block per group, per module and (when the scheme has one) for
    #      the repeatable metrics block.
    groups = scheme.get("groups") or []
    modules = scheme.get("modules") or {}
    metrics = scheme.get("metrics") or {}
    blocks = SL.derive_blocks(scheme)
    by_kind = {}
    for block in blocks:
        by_kind[block.get("kind")] = by_kind.get(block.get("kind"), 0) + 1
    expected = {
        "group": len(groups),
        "module": len(modules),
        "metrics": 1 if (metrics.get("enabled") and metrics.get("fields")) else 0,
    }
    if by_kind == expected:
        rows.append({
            "check": "derived blocks match groups + modules",
            "status": "PASS",
            "detail": "%d blocks = %d groups + %d modules%s" %
                      (len(blocks), len(groups), len(modules),
                       " + %d metrics" % expected["metrics"] if expected["metrics"] else ""),
        })
    else:
        rows.append({
            "check": "derived blocks match groups + modules",
            "status": "FAIL",
            "detail": "%d blocks (%s) for %d groups + %d modules%s" %
                      (len(blocks),
                       ", ".join("%s: %d" % (kind, count)
                                 for kind, count in sorted(by_kind.items())),
                       len(groups), len(modules),
                       " + %d metrics" % expected["metrics"] if expected["metrics"] else ""),
        })

    # 5 -- the legacy fallback still yields the same fields.
    fallback_names = scheme_field_names(fallback_scheme(legacy_path))
    if fallback_names == converted_names:
        rows.append({
            "check": "legacy fallback agrees",
            "status": "PASS",
            "detail": "a project without Project/scheme.json yields the same "
                      "%d field names" % len(fallback_names),
        })
    else:
        rows.append({
            "check": "legacy fallback agrees",
            "status": "FAIL",
            "detail": "%d field name(s) differ between the fallback and the "
                      "conversion" % len(fallback_names ^ converted_names),
        })

    # 6 -- the stored scheme still matches the source it was built from.
    if os.path.exists(stored_scheme_path):
        stored = SL.normalise_scheme(read_json(stored_scheme_path))
        if stored == scheme:
            rows.append({
                "check": "stored scheme matches the conversion",
                "status": "PASS",
                "detail": "%s is identical to the conversion" %
                          os.path.basename(stored_scheme_path),
            })
        else:
            rows.append({
                "check": "stored scheme matches the conversion",
                "status": "FAIL",
                "detail": "%s differs from the conversion - re-run with "
                          "--force" % os.path.basename(stored_scheme_path),
            })
    else:
        rows.append({
            "check": "stored scheme matches the conversion",
            "status": "SKIP",
            "detail": "no stored scheme yet (%s)" %
                      os.path.basename(stored_scheme_path),
        })

    return rows


def print_rows(rows, quiet=False):
    """Print the check table."""
    if quiet:
        for row in rows:
            if row["status"] == "FAIL":
                print("FAIL  %s: %s" % (row["check"], row["detail"]))
        return
    width = max([len(row["check"]) for row in rows] or [0])
    print("")
    print("%-6s %-*s  %s" % ("STATUS", width, "CHECK", "DETAIL"))
    print("%-6s %-*s  %s" % ("-" * 6, width, "-" * width, "-" * 6))
    for row in rows:
        print("%-6s %-*s  %s" % (row["status"], width, row["check"], row["detail"]))
    print("")


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #

def build_parser():
    """The argument parser of the converter."""
    parser = argparse.ArgumentParser(
        prog="convert_legacy_schema.py",
        description="Convert the legacy v3.1 coding schema into the "
                    "project's own scheme and manifest.",
    )
    parser.add_argument("--schema", default=None,
                        help="the legacy v3.1 schema to read "
                             "(default: <project-root>/Scripts/coding_web/schema.json)")
    parser.add_argument("--project-root", default=None,
                        help="the project folder (default: the repository root)")
    parser.add_argument("--out", default=None,
                        help="where to write the scheme "
                             "(default: <project-root>/Project/scheme.json)")
    parser.add_argument("--manifest-out", default=None,
                        help="where to write the manifest "
                             "(default: <project-root>/Project/project.json)")
    parser.add_argument("--check", action="store_true",
                        help="verify the conversion is lossless; write nothing")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing scheme")
    parser.add_argument("--quiet", action="store_true",
                        help="print only errors")
    parser.add_argument("--bundle", action="store_true",
                        help="also regenerate the agent bundle "
                             "(Project/bundles/<scheme>/) from the new scheme")
    return parser


def resolve_paths(args):
    """The four paths the converter works with."""
    root = os.path.abspath(args.project_root) if args.project_root else REPO
    paths = pc.project_paths(root)
    schema = os.path.abspath(args.schema) if args.schema else pc.legacy_schema_path(root)
    out = os.path.abspath(args.out) if args.out else paths["scheme"]
    manifest_out = (os.path.abspath(args.manifest_out) if args.manifest_out
                    else paths["manifest"])
    return root, schema, out, manifest_out


def write_bundle_files(scheme, root):
    """Regenerate the agent bundle from the scheme; returns the written paths.

    The bundle is what an external agent reads (``scheme.json``,
    ``json-schema.json``, ``prompt.md``, ``criteria.json``, ``mcp.json``,
    ``openapi.json``), so it must not drift from the scheme.
    """
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "coding_web"))
        import scheme_export
    except Exception as exc:  # pragma: no cover - the exporter is optional
        print("Could not regenerate the agent bundle: %r" % (exc,), file=sys.stderr)
        return []
    try:
        return scheme_export.write_bundle(scheme, root=root)
    except Exception as exc:  # pragma: no cover
        print("Could not regenerate the agent bundle: %r" % (exc,), file=sys.stderr)
        return []


def main(argv=None):
    """Run the converter; returns the process exit code."""
    args = build_parser().parse_args(argv)
    root, schema_path, scheme_target, manifest_target = resolve_paths(args)

    legacy = read_json(schema_path)
    scheme = scheme_legacy.convert_to_normalised(legacy)

    if args.check:
        rows = check_rows(legacy, scheme, schema_path, scheme_target)
        print_rows(rows, quiet=args.quiet)
        failed = [row for row in rows if row["status"] == "FAIL"]
        if failed:
            if not args.quiet:
                print("%d of %d checks FAILED." % (len(failed), len(rows)))
            return 1
        if not args.quiet:
            print("All %d checks PASS." % len(rows))
        return 0

    if scheme_name_is_shadowed(scheme):
        print("The converted scheme is named after one of its own groups (%r) - "
              "the converter's 'name' argument is shadowed." % scheme.get("name"),
              file=sys.stderr)
        print("Nothing was written.", file=sys.stderr)
        return 1

    if os.path.exists(scheme_target) and not args.force:
        print("The scheme already exists: %s" % scheme_target, file=sys.stderr)
        print("Refusing to overwrite it. Re-run with --force to replace it.",
              file=sys.stderr)
        return 1

    rows = check_rows(legacy, scheme, schema_path, scheme_target)
    failed = [row for row in rows if row["status"] == "FAIL"
              and row["check"] != "stored scheme matches the conversion"]
    if failed:
        print_rows(rows, quiet=args.quiet)
        print("The conversion is not lossless - nothing was written.",
              file=sys.stderr)
        return 1

    created = existing_created(manifest_target) or pc.now()
    manifest = build_manifest(scheme, legacy, schema_path, root, created)

    write_scheme_document(scheme, root, scheme_target)
    write_manifest_document(manifest, root, manifest_target)

    bundle_paths = []
    if args.bundle:
        bundle_paths = write_bundle_files(scheme, root)

    if not args.quiet:
        print("Converted %s" % schema_path)
        print("  scheme   %s" % scheme_target)
        print("  manifest %s" % manifest_target)
        print("  fields   %d study, %d metric, %d module" %
              (len(scheme.get("fields") or []),
               len((scheme.get("metrics") or {}).get("fields") or []),
               sum(len(mod.get("fields") or [])
                   for mod in (scheme.get("modules") or {}).values())))
        print("  blocks   %d" % len(SL.derive_blocks(scheme)))
        print("  sha256   %s" % manifest["source_schema"]["sha256"])
        for path in bundle_paths:
            print("  bundle   %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
