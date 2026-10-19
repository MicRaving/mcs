# -*- coding: utf-8 -*-
"""Audit every ``Datasets/<Key>/harmonizer.py`` against the harmonizer contract.

Checks (one row per file x clause in ``Results/harmonizer_audit.csv``):

* ``syntax_ok``              -- the file parses.
* ``emits_exclusions``       -- writes ``<Key> - exclusions.csv`` (header-only is
                               fine) from within the harmonizer, not just the
                               rename shim.
* ``varmap_merged``          -- ``_varmap`` merges every ``* - variables.csv``
                               (a single-first-file ``_varmap`` silently loses
                               multi-artifact sidecar metadata).  Absent -> ok.
* ``veracity_of_anchored``   -- ``veracity_of`` uses anchored tokens, so
                               "untrue"/"inaccurate"/"realistic" are not decoded
                               as TRUE.  Absent -> ok.
* ``no_flag_01_twins``       -- a rename shim's ``_01`` branch may not emit a
                               ``flag_*_01`` twin (flag columns have no twin).
* ``no_absolute_paths``      -- no hard-coded ``C:\\``/``/Users/`` data paths.
* ``temp_within_root_temp``  -- any ``tempfile.tempdir`` redirect points at the
                               project ``temp/`` tree.

Run:  python Scripts/audit_harmonizers.py [--out Results/harmonizer_audit.csv]
Exit: 0 (reports findings, does not fail).
"""
import argparse
import ast
import glob
import os
import re
import sys

B = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CLAUSES = ["syntax_ok", "emits_exclusions", "varmap_merged", "veracity_of_anchored",
           "no_flag_01_twins", "no_absolute_paths", "temp_within_root_temp"]

_ABS_PATH = re.compile(r"(^|[^A-Za-z0-9_])[A-Za-z]:[\\/]|/Users/|/home/")


def _key_of(path):
    rel = os.path.relpath(path, os.path.join(B, "Datasets"))
    return rel.split(os.sep)[0]


def _find_func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def check_file(path):
    """Return {clause: (ok, detail)} for one harmonizer."""
    res = {c: (True, "") for c in CLAUSES}
    try:
        src = open(path, encoding="utf-8", errors="replace").read()
    except OSError as e:
        for c in CLAUSES:
            res[c] = (False, f"unreadable: {e!r}")
        return res
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        res["syntax_ok"] = (False, f"SyntaxError: {e}")
        for c in CLAUSES[1:]:
            res[c] = (False, "not parsed")
        return res

    # emits_exclusions.csv: a to_csv whose first argument names the file.
    # Two wrinkles this clause has to absorb:
    #   1. The SB route puts its suffix between the product name and the extension
    #      ("<Key> - exclusions - SB.csv"), so match the two tokens separately
    #      instead of the literal substring "exclusions.csv".
    #   2. Many harmonizers build the path once into a local and pass the name
    #      (to_csv(excl_path)), which names nothing in the call itself.  Collect
    #      such bindings and accept them too.
    def _flat(node):
        return ast.unparse(node).replace("'", "").replace('"', "")

    excl_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if value is None:
            continue
        s = _flat(value)
        if "exclusions" in s and ".csv" in s:
            for t in targets:
                for nm in ast.walk(t):
                    if isinstance(nm, ast.Name):
                        excl_names.add(nm.id)

    emits = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "to_csv" and node.args:
            arg = node.args[0]
            target = _flat(arg)
            if "exclusions" in target and ".csv" in target:
                emits = True
                break
            if isinstance(arg, ast.Name) and arg.id in excl_names:
                emits = True
                break
    res["emits_exclusions"] = (emits, "" if emits else "no to_csv(...exclusions.csv)")

    # varmap_merged (only meaningful when the folder has >1 variable-map artifact)
    fn = _find_func(tree, "_varmap")
    if fn is None:
        res["varmap_merged"] = (True, "no _varmap")
    else:
        seg = ast.get_source_segment(src, fn) or ""
        merged = ("concat" in seg) or ("append(" in seg)
        if merged:
            res["varmap_merged"] = (True, "")
        else:
            folder = os.path.dirname(path)
            n_art = len(glob.glob(os.path.join(folder, "* - variables*.csv"))) \
                + (1 if os.path.exists(os.path.join(folder, "variables.csv")) else 0)
            if n_art <= 1:
                res["varmap_merged"] = (True, "single-artifact; first-file _varmap is equivalent")
            else:
                res["varmap_merged"] = (False, "single-first-file _varmap on %d artifacts" % n_art)

    # veracity_of_anchored: anchored tokens only.  The old standard body had
    # anchored *explicit* lines but a loose substring fallback
    # (`re.search(r"(true|real|...)", s)` / `"true" in s`), which is what made
    # "untrue"/"inaccurate"/"realistic" read as TRUE.  Flag that fallback.
    fn = _find_func(tree, "veracity_of")
    if fn is None:
        res["veracity_of_anchored"] = (True, "no veracity_of")
    else:
        seg = ast.get_source_segment(src, fn) or ""
        loose = bool(re.search(
            r"""["'](?:true|false|real|fake|accurate|inaccurate|manipulative|misleading|"""
            r"""nonmanipulative)["']\s+in\s+""", seg))
        underscore_anchored = False
        for m in re.finditer(r'r"([^"]*)"', seg):
            pat = m.group(1)
            if re.match(r"\s*\(\s*(?:true|false|real|fake|accurate|inaccurate|"
                        r"manipulative|misleading|nonmanipulative)\b", pat, re.I):
                loose = True
                break
            if re.match(r"\s*_\(\s*(?:true|false|real|fake|accurate|inaccurate|"
                        r"manipulative|misleading|nonmanipulative)\b", pat, re.I) \
                    or re.match(r"\s*_(?:true|false|real|fake|accurate|inaccurate|"
                                r"manipulative|misleading|nonmanipulative)\b", pat, re.I):
                underscore_anchored = True
        marker = bool(re.search(r"\(\?<!\[a-z\]\)", seg)) or ("(?<![a-z])" in seg) \
            or ("(^|_)" in seg) or ("_true_" in seg) or underscore_anchored
        anchored = (not loose) and marker
        detail = ""
        if loose:
            detail = "loose substring fallback tokens"
        elif not marker:
            detail = "no anchoring marker"
        res["veracity_of_anchored"] = (anchored, detail)

    # no_flag_01_twins: only a real risk when the rename map sends a variable to
    # flag_* AND the `_01` branch does not forbid the flag_*_01 twin.
    rename_maps = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            for tgt, val in zip(node.targets, [node.value]):
                if isinstance(tgt, ast.Name):
                    vals = [ast.literal_eval(v) for v in val.values
                            if isinstance(v, ast.Constant) and isinstance(v.value, str)]
                    rename_maps[tgt.id] = vals
    maps_to_flag = any(any(str(v).startswith("flag_") for v in vals)
                       for vals in rename_maps.values())
    bad_ren = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and "ren" in node.name.lower():
            seg = ast.get_source_segment(src, node) or ""
            if "_01" in seg and re.search(r"\+\s*['\"]_01['\"]|['\"]_01['\"]\s*\)", seg):
                guarded = ("startswith" in seg and "flag_" in seg) or "forbid_flag01" in seg
                if not guarded:
                    bad_ren.append(node.name)
    if not maps_to_flag:
        res["no_flag_01_twins"] = (True, "no cov_->flag_ rename")
    else:
        res["no_flag_01_twins"] = (not bad_ren,
                                   "" if not bad_ren else f"{bad_ren} may emit flag_*_01")

    # no_absolute_paths (ignore docstring/comment text only partially; code lines)
    hits = []
    for i, line in enumerate(src.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        m = _ABS_PATH.search(line)
        if m:
            hits.append(i)
    res["no_absolute_paths"] = (not hits, "" if not hits else f"lines {hits[:5]}")

    # temp_within_root_temp: any `tempfile.tempdir = <expr>` must point at temp/.
    # Resolve simple module-level name aliases (e.g. `_TMP = os.path.join(..., "temp")`).
    name_expr = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            name_expr[node.targets[0].id] = node.value

    def _resolves_to_temp(expr):
        seen = set()
        while True:
            if "temp" in ast.unparse(expr).lower():
                return True
            if isinstance(expr, ast.Name) and expr.id in name_expr and expr.id not in seen:
                seen.add(expr.id)
                expr = name_expr[expr.id]
                continue
            return False

    temp_assign = []
    tf_names = {"tempfile"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "tempfile":
                    tf_names.add(a.asname or "tempfile")
        if isinstance(node, ast.ImportFrom) and node.module == "tempfile":
            for a in node.names:
                tf_names.add(a.asname or a.name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Attribute) and tgt.attr == "tempdir" \
                        and isinstance(tgt.value, ast.Name) and tgt.value.id in tf_names:
                    temp_assign.append(node.value)
    uses_tempfile = any(n in src for n in tf_names)
    if not temp_assign:
        ok = not uses_tempfile
        res["temp_within_root_temp"] = (ok, "" if ok else "uses tempfile without redirect")
    else:
        ok = all(_resolves_to_temp(v) for v in temp_assign)
        res["temp_within_root_temp"] = (ok, "" if ok else
                                        f"tempdir={[ast.unparse(v) for v in temp_assign]}")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=os.path.join(B, "Results", "harmonizer_audit.csv"))
    args = ap.parse_args(argv)

    import pandas as pd

    paths = sorted(glob.glob(os.path.join(B, "Datasets", "**", "harmonizer.py"),
                             recursive=True))
    rows = []
    for p in paths:
        key = _key_of(p)
        for clause, (ok, detail) in check_file(p).items():
            rows.append(dict(key=key, clause=clause, ok=bool(ok), detail=detail))
    out = pd.DataFrame(rows, columns=["key", "clause", "ok", "detail"])
    out.to_csv(args.out, index=False, encoding="utf-8-sig")

    print(f"audit_harmonizers: {len(paths)} harmonizer.py files -> {args.out}")
    print(f"  {'clause':<24} {'ok':>5} {'not ok':>7}")
    summary = {}
    for clause in CLAUSES:
        sub = out[out["clause"] == clause]
        n_ok = int(sub["ok"].sum())
        n_bad = int((~sub["ok"].astype(bool)).sum())
        summary[clause] = (n_ok, n_bad)
        print(f"  {clause:<24} {n_ok:>5} {n_bad:>7}")
    # non-ok by key, for the clauses that are actionable
    for clause in CLAUSES:
        sub = out[(out["clause"] == clause) & (~out["ok"].astype(bool))]
        if not sub.empty:
            print(f"  ! {clause}: {len(sub)} file(s): "
                  + ", ".join(sorted(sub['key'].head(12))))

    # summary counts as a small side table
    summ = pd.DataFrame([{"clause": c, "ok": summary[c][0], "not_ok": summary[c][1]}
                         for c in CLAUSES])
    summ_path = os.path.splitext(args.out)[0] + "_summary.csv"
    summ.to_csv(summ_path, index=False, encoding="utf-8-sig")
    print(f"  clause summary -> {summ_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
