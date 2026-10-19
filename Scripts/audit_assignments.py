# -*- coding: utf-8 -*-
"""Audit coder **variable assignments** against the generated products (i2 §5f).

Checks (one row per study x check in ``Results/assignment_audit.csv``):

* ``parity``          -- every ``confirmed`` assignment is reflected in the
                         sidecar (the acceptance criterion; a mismatch exits 1).
* ``stale_key``       -- an assignment key matches neither a sidecar row nor a
                         wide column (a rename/regeneration orphaned it).
* ``veracity_of``     -- where the uniform name decodes a stimulus veracity it
                         must equal the sidecar ``veracity``.
* ``no_flag_01``      -- no ``flag_*_01`` twin exists in any wide table.
* ``exclusions_flag`` -- every ``exclusions.csv`` ``flag`` exists in the wide.
* ``conditions_cover``-- every ``conditions.csv`` code is an observed
                         ``condition`` / ``condition_raw`` value.

Exit: 1 on a ``parity`` failure, else 0 (other findings are reported only).

Run:  python Scripts/audit_assignments.py
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    import contract as _contract
    HAS_CONTRACT = True
except Exception:  # noqa: BLE001
    _contract = None
    HAS_CONTRACT = False

try:
    import variable_assignments as _va
except Exception:  # noqa: BLE001
    _va = None

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

DS = os.path.join(ROOT, "Datasets")
RESULTS = os.path.join(ROOT, "Results")

CHECKS = ["parity", "stale_key", "veracity_of", "no_flag_01",
          "exclusions_flag", "conditions_cover"]

_BLANK = {"", "nan", "none", "nat", "<na>"}
_FLAG01 = re.compile(r"^flag_.*_01$")


def _s(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if type(v).__name__ == "bool_":
        return "true" if bool(v) else "false"
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    return "" if s.lower() in _BLANK else s


def _norm_code(x):
    """Mirror ``suite_common._norm_condition_code`` (numeric + case/sep fold)."""
    if x is None:
        return ""
    try:
        if pd.isna(x):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(x).strip()
    if s == "":
        return ""
    try:
        f = float(s)
        return str(int(f)) if f == int(f) else str(f)
    except (TypeError, ValueError):
        pass
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _wide_cols(path):
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            return [str(c).strip().lstrip("\ufeff") for c in next(csv.reader(fh))]
    except (OSError, StopIteration):
        return []


def _read(path):
    df = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    df.columns = [str(c).strip().lstrip("\ufeff") for c in df.columns]
    return df


def _observed_conditions(wide_path, cols):
    use = [c for c in cols if c]
    if not use or not os.path.isfile(wide_path):
        return set()
    try:
        d = pd.read_csv(wide_path, encoding="utf-8-sig", low_memory=False,
                        usecols=use)
    except Exception:  # noqa: BLE001
        return set()
    out = set()
    for c in use:
        out |= {_s(v) for v in d[c].tolist() if _s(v)}
    return out

def audit_study(key, folder):
    rows = []
    side_path = os.path.join(folder, f"{key} - harmonized_variables.csv")
    wide_path = os.path.join(folder, f"{key} - harmonized.csv")
    side = _read(side_path) if os.path.isfile(side_path) else pd.DataFrame()
    wide_cols = _wide_cols(wide_path)
    a = _va.load(key, folder) if _va is not None else pd.DataFrame()

    # --- parity / stale-key ------------------------------------------------
    parity_bad, stale, parity_n = [], [], 0
    for _, r in a.iterrows():
        st = _s(r.get("status")).lower()
        if st == "conflict":
            continue
        art = _va._norm_artifact(r.get("artifact")) if _va else "*"
        orig = _s(r.get("original_variable"))
        item = _s(r.get("item_scope"))
        targets = _va._match_indices(side, art, orig, item) if _va and len(side) else []
        if not targets:
            in_wide = orig in wide_cols
            if not in_wide:
                stale.append(f"{art}|{orig}|{item}")
            continue
        if st != "confirmed":
            continue
        parity_n += 1
        for i in targets:
            for f in (_va.LABEL_FIELDS if _va else []):
                want = _s(r.get(f))
                if not want:
                    continue
                if f == "role" and want.lower() in getattr(_va, "NA_ROLES", set()):
                    continue
                got = _s(side.at[i, f]) if f in side.columns else ""
                if got != want:
                    parity_bad.append(f"{orig}:{f} sidecar={got!r} assignment={want!r}")
    rows.append(dict(study_key=key, check="parity", ok=not parity_bad, n=parity_n,
                     detail="; ".join(parity_bad[:6])))
    rows.append(dict(study_key=key, check="stale_key", ok=not stale, n=len(stale),
                     detail="; ".join(stale[:6])))

    # --- veracity_of(uniform) == veracity ----------------------------------
    ver_bad, ver_decodable_blank, ver_n = [], [], 0
    if len(side) and "uniform_variable" in side.columns:
        for _, r in side.iterrows():
            uv = _s(r.get("uniform_variable"))
            if not uv.startswith("dv_"):
                continue
            ver = _s(r.get("veracity")).lower()
            if _contract is not None and hasattr(_contract, "dv_veracity"):
                decoded = _s(_contract.dv_veracity(uv, notes=_s(r.get("notes")))).lower()
            else:
                decoded = ""
            if not decoded:
                continue
            ver_n += 1
            if ver and decoded != ver:
                ver_bad.append(f"{uv}: decoded={decoded} sidecar={ver}")
            elif not ver:
                ver_decodable_blank.append(uv)
    detail = "; ".join(ver_bad[:6])
    if ver_decodable_blank:
        detail += ("; " if detail else "") + \
            f"{len(ver_decodable_blank)} decodable-but-blank (e.g. {ver_decodable_blank[0]})"
    rows.append(dict(study_key=key, check="veracity_of", ok=not ver_bad, n=ver_n,
                     detail=detail))

    # --- no flag_*_01 ------------------------------------------------------
    flag01 = [c for c in wide_cols if _FLAG01.match(str(c))]
    rows.append(dict(study_key=key, check="no_flag_01", ok=not flag01, n=len(flag01),
                     detail="; ".join(flag01[:6])))

    # --- exclusions flags exist in the wide --------------------------------
    excl_path = os.path.join(folder, f"{key} - exclusions.csv")
    miss_flags = []
    if os.path.isfile(excl_path):
        try:
            ex = _read(excl_path)
            for _, r in ex.iterrows():
                fl = _s(r.get("flag"))
                if fl and fl not in wide_cols:
                    miss_flags.append(fl)
        except Exception:  # noqa: BLE001
            pass
    rows.append(dict(study_key=key, check="exclusions_flag", ok=not miss_flags,
                     n=len(miss_flags), detail="; ".join(miss_flags[:6])))

    # --- conditions codes cover observed values ----------------------------
    cond_path = os.path.join(folder, f"{key} - conditions.csv")
    uncovered_codes, uncovered_obs = [], []
    if os.path.isfile(cond_path):
        try:
            cm = _read(cond_path)
            codes = {_s(v) for v in cm.get("code", pd.Series(dtype=str)).tolist() if _s(v)}
            # the pipeline splits arms on `condition`, falling back to
            # `condition_raw`; a conditions.csv code must cover that column.
            primary = ["condition"] if "condition" in wide_cols else \
                (["condition_raw"] if "condition_raw" in wide_cols else [])
            obs_primary = _observed_conditions(wide_path, primary)
            obs_all = _observed_conditions(wide_path, [c for c in ("condition", "condition_raw")
                                                       if c in wide_cols])
            codes_norm = {_norm_code(c) for c in codes}
            obs_all_norm = {_norm_code(v) for v in obs_all}
            obs_primary_norm = {_norm_code(v) for v in obs_primary}
            uncovered_codes = sorted(c for c in codes if _norm_code(c) not in obs_all_norm)
            uncovered_obs = sorted(v for v in obs_primary if _norm_code(v) not in codes_norm)
        except Exception:  # noqa: BLE001
            pass
    det = ""
    if uncovered_codes:
        det += "codes not observed: " + ", ".join(uncovered_codes[:6])
    if uncovered_obs:
        det += ("; " if det else "") + "observed not covered: " + ", ".join(uncovered_obs[:6])
    rows.append(dict(study_key=key, check="conditions_cover",
                     ok=not uncovered_codes and not uncovered_obs, n=len(uncovered_codes),
                     detail=det))
    return rows


def discover():
    out = []
    for name in sorted(os.listdir(DS)):
        folder = os.path.join(DS, name)
        if not os.path.isdir(folder):
            continue
        has_side = os.path.isfile(os.path.join(folder, f"{name} - harmonized_variables.csv"))
        has_assign = os.path.isfile(os.path.join(folder, f"{name} - variable_assignments.csv"))
        if has_side or has_assign:
            out.append((name, folder))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=os.path.join(RESULTS, "assignment_audit.csv"))
    args = ap.parse_args(argv)

    studies = discover()
    rows = []
    for key, folder in studies:
        try:
            rows.extend(audit_study(key, folder))
        except Exception as e:  # noqa: BLE001
            rows.append(dict(study_key=key, check="parity", ok=False, n=0,
                             detail=f"audit error: {e!r}"))
    out = pd.DataFrame(rows, columns=["study_key", "check", "ok", "n", "detail"])
    os.makedirs(RESULTS, exist_ok=True)
    out.to_csv(args.out, index=False, encoding="utf-8-sig")

    n_assign = sum(1 for k, d in studies
                   if os.path.isfile(os.path.join(d, f"{k} - variable_assignments.csv")))
    print(f"audit_assignments: {len(studies)} studies ({n_assign} with assignments) "
          f"-> {args.out}")
    print(f"  {'check':<18} {'ok':>5} {'not ok':>7} {'rows':>7}")
    fail_parity = 0
    for chk in CHECKS:
        sub = out[out["check"] == chk]
        n_ok = int(sub["ok"].sum())
        n_bad = int((~sub["ok"].astype(bool)).sum())
        print(f"  {chk:<18} {n_ok:>5} {n_bad:>7} {int(sub['n'].sum()):>7}")
        if chk == "parity":
            fail_parity = n_bad
        if n_bad:
            for _, r in sub[~sub["ok"].astype(bool)].head(5).iterrows():
                print(f"    ! {r['study_key']}: {r['detail']}")
    if fail_parity:
        print(f"FAIL: {fail_parity} study/studies with a parity mismatch")
        return 1
    print("OK: assignments parity clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
