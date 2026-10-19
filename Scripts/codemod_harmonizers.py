# -*- coding: utf-8 -*-
"""Idempotent codemod for the harmonizer contract (see Scripts/audit_harmonizers.py).

Repairs three mechanical, source-level defects across ``Datasets/*/harmonizer.py``:

1. ``veracity_of_anchored`` -- the old fallback
   ``re.search(r"(true|real|...)", s)`` matches inside "inaccurate"/"untrue"/
   "realistic".  The alternation is wrapped with token anchors
   ``(?<![a-z]) ... (?![a-z])``.
2. ``no_flag_01_twins`` -- a rename shim's ``_01`` branch
   ``return _VAR_RENAME[c[:-3]] + '_01'`` can emit a forbidden ``flag_*_01``
   twin.  The branch drops ``_01`` when the rename target is a ``flag_*``.
3. ``emits_exclusions`` -- insert a self-contained
   ``_emit_exclusions_csv()`` helper (header-only when ``EXCLUSIONS`` is empty)
   and call it at the top of ``main()``.

Never touches the nine harmonizers already repaired by hand.  Archives each file
to ``archive/harmonizers_2026-09-22/<Key>/harmonizer.py`` before writing.

Usage::

    python Scripts/codemod_harmonizers.py            # dry run
    python Scripts/codemod_harmonizers.py --apply
"""
import argparse
import ast
import glob
import os
import re
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASETS = os.path.join(ROOT, "Datasets")
ARCHIVE = os.path.join(ROOT, "archive", "harmonizers_2026-09-22")

SKIP = {
    "Adam-Troian et al. (2019)", "Guess et al. (2020)", "Nyhan et al. (2013)",
    "Butler & Swire-Thompson (2026)", "Offer-Westort et al. (2024)",
    "Maertens et al. (2021)", "Cook et al. (2024)",
    "Roozenbeek et al. (2021 b)", "Roozenbeek & van der Linden (2019)",
}

# ---- clause 1: anchor the loose veracity fallback -------------------------- #
_VERACITY_RE = re.compile(
    r're\.search\(r(?P<q>["\'])(?P<body>\('
    r'(?:true|false|real|fake|accurate|inaccurate|manipulative|misleading|nonmanipulative)\b'
    r'[^"\']*\))(?P=q)')


def anchor_veracity(src):
    return _VERACITY_RE.sub(
        lambda m: "re.search(r%s(?<![a-z])%s(?![a-z])%s" % (m.group("q"), m.group("body"), m.group("q")),
        src)


# ---- clause 2: forbid flag_*_01 in the rename shim ------------------------- #
# Two source forms occur: an inline `if ...: return MAP[c[:-3]] + '_01'` and a
# `if ...:\n    return ...` block.  Both are replaced with a properly indented
# block that drops `_01` when the rename target is a `flag_*` column.
_FLAG01_INLINE = re.compile(
    r'(?P<lead>[ \t]*)if\s+(?P<var>\w+)\.endswith\((?P<q>["\'])_01(?P=q)\)\s+and\s+'
    r'(?P=var)\[:-3\]\s+in\s+(?P<map>\w+):[ \t]*return\s+(?P=map)\[(?P=var)\[:-3\]\]\s*'
    r'\+\s*(?P<q2>["\'])_01(?P=q2)')
_FLAG01_BLOCK = re.compile(
    r'(?P<lead>[ \t]*)if\s+(?P<var>\w+)\.endswith\((?P<q>["\'])_01(?P=q)\)\s+and\s+'
    r'(?P=var)\[:-3\]\s+in\s+(?P<map>\w+):\n'
    r'(?P<lead2>[ \t]*)return\s+(?P=map)\[(?P=var)\[:-3\]\]\s*\n?[ \t]*\+\s*'
    r'(?P<q2>["\'])_01(?P=q2)')


def _flag01_block(lead, var, mp):
    return ("%sif %s.endswith('_01') and %s[:-3] in %s:\n"
            "%s    _rflag = %s[%s[:-3]]\n"
            "%s    return _rflag if _rflag.startswith('flag_') else _rflag + '_01'"
            % (lead, var, var, mp, lead, mp, var, lead))


def guard_flag01(src):
    src = _FLAG01_BLOCK.sub(
        lambda m: _flag01_block(m.group("lead"), m.group("var"), m.group("map")), src)
    src = _FLAG01_INLINE.sub(
        lambda m: _flag01_block(m.group("lead"), m.group("var"), m.group("map")), src)
    return src


# ---- clause 3: emit <Key> - exclusions.csv --------------------------------- #
_EXCL_HELPER = '''

def _emit_exclusions_csv():
    """Contract: always write <Key> - exclusions.csv (header-only when none)."""
    import os as _os
    try:
        import pandas as _pd
    except Exception:
        return
    _d = _os.path.dirname(_os.path.abspath(__file__))
    _k = _os.path.basename(_d)
    _rows = globals().get("EXCLUSIONS") or []
    _cols = ["flag", "keep", "applies_to", "reason"]
    try:
        if _rows and all(isinstance(r, dict) for r in _rows):
            _df = _pd.DataFrame(_rows).reindex(columns=_cols)
        elif _rows and all(isinstance(r, (list, tuple)) and len(r) == 4 for r in _rows):
            _df = _pd.DataFrame(_rows, columns=_cols)
        else:
            _df = _pd.DataFrame(columns=_cols)
    except Exception:
        _df = _pd.DataFrame(columns=_cols)
    _df.to_csv(_os.path.join(_d, _k + " - exclusions.csv"), index=False, encoding="utf-8-sig")
'''

_CALL = "_emit_exclusions_csv()"

# ---- clause 4: merge _varmap for multi-artifact studies -------------------- #
_MERGED_VARMAP = '''def _varmap():
    """Merge every `<...> - variables.csv` / `variables.csv` in the folder.

    The old version returned the first match only; multi-artifact studies then
    silently lost the sidecar metadata for DVs defined in other variable maps.
    First occurrence of a uniform variable wins.
    """
    frames, seen = [], set()
    for p in _find("* - variables.csv") + _find("variables.csv"):
        v = _read(p)
        if "uniform_variable" not in v.columns and "uniform_name" in v.columns:
            v = v.rename(columns={"uniform_name": "uniform_variable"})
        if "uniform_variable" in v.columns:
            v = v[~v["uniform_variable"].astype(str).isin(seen)].copy()
            seen |= set(v["uniform_variable"].astype(str))
        frames.append(v)
    if not frames:
        return None
    import pandas as _pd
    return _pd.concat(frames, ignore_index=True)'''


def _count_artifacts(key):
    import glob as _g
    d = os.path.join(DATASETS, key)
    return len(_g.glob(os.path.join(d, "* - variables*.csv"))) + \
        (1 if os.path.exists(os.path.join(d, "variables.csv")) else 0)


def merge_varmap(src):
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    fn = _find(tree, "_varmap")
    if fn is None:
        return src
    seg = ast.get_source_segment(src, fn) or ""
    if ("concat" in seg) or ("_find(" not in seg) or ("_read(" not in seg):
        return src
    return src.replace(seg, _MERGED_VARMAP, 1)


def _find(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    return None


def add_exclusions_emitter(src):
    """Return (new_src, changed).

    Pure text insertion (no line-number arithmetic): the helper is placed
    immediately before ``def main``, and the call is placed as the first
    statement of ``main`` (after a leading docstring when present).
    """
    if "_emit_exclusions_csv" in src:
        return src, False
    if re.search(r"to_csv\([^)]*exclusions\.csv", src):
        return src, False
    m = re.search(r"(?m)^def main\(", src)
    if not m:
        return src, False
    helper = _EXCL_HELPER.strip("\n") + "\n\n"
    if "_emit_exclusions_csv" not in src:
        src = src[:m.start()] + helper + src[m.start():]
    m2 = re.search(r"(?m)^(def main\([^\n]*\):)\n", src)
    if not m2:
        return src, False
    after = src[m2.end():]
    mi = re.match(r"([ \t]+)", after)
    indent = mi.group(1) if mi else "    "
    call = indent + _CALL + "\n"
    # skip an immediate docstring so the call does not precede it
    dm = re.match(r'[ \t]+("""|\'\'\')(?:.|\n)*?\1[ \t]*\n', after)
    if dm:
        src = src[:m2.end()] + after[:dm.end()] + call + after[dm.end():]
    else:
        src = src[:m2.end()] + call + after
    return src, True


def transform(src, key=None):
    n1 = n2 = n3 = n4 = 0
    new = anchor_veracity(src)
    if new != src:
        n1 = len(_VERACITY_RE.findall(src))
        src = new
    new = guard_flag01(src)
    if new != src:
        n2 = len(_FLAG01_INLINE.findall(src)) + len(_FLAG01_BLOCK.findall(src))
        src = new
    src, did = add_exclusions_emitter(src)
    n3 = 1 if did else 0
    if key and _count_artifacts(key) > 1:
        new = merge_varmap(src)
        if new != src:
            n4 = 1
            src = new
    return src, n1, n2, n3, n4


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    paths = sorted(glob.glob(os.path.join(DATASETS, "**", "harmonizer.py"), recursive=True))
    tot = [0, 0, 0, 0]
    changed_files = []
    skipped = []
    for p in paths:
        key = os.path.relpath(p, DATASETS).split(os.sep)[0]
        if key in SKIP:
            continue
        try:
            src = open(p, encoding="utf-8").read()
        except OSError as e:
            skipped.append((key, "unreadable %r" % e))
            continue
        try:
            new, n1, n2, n3, n4 = transform(src, key)
        except Exception as e:  # noqa: BLE001
            skipped.append((key, "transform error %r" % e))
            continue
        if (n1, n2, n3, n4) == (0, 0, 0, 0):
            continue
        try:
            ast.parse(new)
        except SyntaxError as e:
            skipped.append((key, "post-edit SyntaxError %s" % e))
            continue
        changed_files.append((key, p, new, n1, n2, n3, n4))
        tot[0] += n1
        tot[1] += n2
        tot[2] += n3
        tot[3] += n4

    print("== harmonizer codemod ==")
    print("files to change: %d | veracity anchors: %d | flag_01 guards: %d | "
          "exclusions emitters: %d | varmap merges: %d"
          % (len(changed_files), tot[0], tot[1], tot[2], tot[3]))
    if skipped:
        print("skipped: %d" % len(skipped))
        for k, why in skipped[:20]:
            print("   ", k, "-", why)
    if not args.apply:
        print("\nDRY RUN — no files written. Re-run with --apply.")
        for k, _, _, n1, n2, n3, n4 in changed_files[:12]:
            print("   %s (veracity %d, flag01 %d, excl %d, varmap %d)" % (k, n1, n2, n3, n4))
        if len(changed_files) > 12:
            print("   ... and %d more" % (len(changed_files) - 12))
        return 0

    for key, p, new, *_ in changed_files:
        dst_dir = os.path.join(ARCHIVE, key)
        os.makedirs(dst_dir, exist_ok=True)
        backup = os.path.join(dst_dir, "harmonizer.py")
        if not os.path.exists(backup):
            shutil.copy2(p, backup)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(new)
    print("\nAPPLIED: %d files rewritten (originals in %s)"
          % (len(changed_files), os.path.relpath(ARCHIVE, ROOT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
