# -*- coding: utf-8 -*-
"""Attach per-veracity participant-level outcomes to a harmonized dataset.

Some studies' prepared data are trial/item level (one row per participant x item)
and carry an item-veracity column (``item_veracity``: real/fake, TRUE/FALSE, 1/-1).
The generic harmonizer drops that column and emits only an overall composite, so
the SDT layer cannot split true vs false stimuli and the study yields no
discrimination ``d``.

``attach(key, folder, sources)`` re-reads those trial-level sources, computes a
participant-level mean of every ``dv_*`` column separately for the true and the
false trials, and adds:

  * ``dv_<name>_true`` / ``dv_<name>_false`` (and their ``_01`` 0-1 twins) to
    ``<Key> - harmonized.csv`` (broadcast onto the participant's existing rows), and
  * matching rows in ``<Key> - harmonized_variables.csv`` with the ``veracity``
    label and the veracity-dependent ``dv_polarity``.

Idempotent: existing split columns are replaced.
"""

import os

import numpy as np
import pandas as pd


def _domain_of(name, raw=""):
    """Resolve the controlled dv_domain from the uniform name (shared vocabulary)."""
    try:
        import sys
        for p in (os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Scripts"),):
            if p not in sys.path:
                sys.path.insert(0, p)
        import domain_restriction as DR
        return DR.resolve_sidecar_domain(raw, name)
    except Exception:
        s = str(name)
        s = s[3:] if s.startswith("dv_") else s
        return s.split("_", 1)[0] or "other"


def _polarity(domain, veracity, name, notes=""):
    try:
        import sys
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Scripts")
        if p not in sys.path:
            sys.path.insert(0, p)
        import dv_polarity as DP
        return DP.expected_polarity(domain, veracity, name, notes=notes) or ""
    except Exception:
        return ""


def _read(p):
    df = pd.read_csv(p, encoding="utf-8-sig", low_memory=False)
    df.columns = [str(c).strip().lstrip("\ufeff") for c in df.columns]
    return df


def attach(key, folder, sources):
    """sources: list of {path, artifact, pid_col, veracity_col, true_values, false_values}
    (optional 'ren' rename map). Returns (n_new_columns, n_sources_used)."""
    harm = os.path.join(folder, key + " - harmonized.csv")
    side = os.path.join(folder, key + " - harmonized_variables.csv")
    if not (os.path.isfile(harm) and os.path.isfile(side)):
        return 0, 0
    parts = []
    for s in sources:
        if not os.path.isfile(s["path"]):
            continue
        df = _read(s["path"])
        if s.get("ren"):
            df = df.rename(columns={k: s["ren"][k] for k in df.columns if k in s["ren"]})
        pid, vc = s["pid_col"], s["veracity_col"]
        if pid not in df.columns or vc not in df.columns:
            continue
        ver = df[vc].astype(str).str.strip().str.lower()
        tv = [str(v).lower() for v in s["true_values"]]
        fv = [str(v).lower() for v in s["false_values"]]
        dv = [c for c in df.columns if str(c).startswith("dv_")]
        if not dv:
            continue
        for c in dv:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        tm, fm = ver.isin(tv), ver.isin(fv)
        obj = {}
        for c in dv:
            obj[c + "_true"] = df[tm].groupby(pid, dropna=False)[c].mean()
            obj[c + "_false"] = df[fm].groupby(pid, dropna=False)[c].mean()
        part = pd.DataFrame(obj)
        part["artifact"] = s["artifact"]
        parts.append(part.reset_index())
    if not parts:
        return 0, 0
    N = pd.concat(parts, ignore_index=True)
    N = N.drop_duplicates(["pid", "artifact"], keep="first")
    H = pd.read_csv(harm, encoding="utf-8-sig", low_memory=False)
    if "pid" not in H.columns or "artifact" not in H.columns:
        return 0, 0
    new_cols = [c for c in N.columns if c not in ("pid", "artifact")]
    H = H.drop(columns=[c for c in new_cols if c in H.columns], errors="ignore")
    H = H.merge(N, on=["pid", "artifact"], how="left")
    # 0-1 twins for the new split columns (built in one concat; avoids fragmentation)
    twins = {}
    for c in new_cols:
        s = pd.to_numeric(H[c], errors="coerce")
        lo, hi = s.min(), s.max()
        if pd.notna(lo) and pd.notna(hi) and hi > lo:
            twins[str(c) + "_01"] = (s - lo) / (hi - lo)
    if twins:
        H = pd.concat([H, pd.DataFrame(twins, index=H.index)], axis=1)
    H.to_csv(harm, index=False, encoding="utf-8-sig")

    S = pd.read_csv(side, encoding="utf-8-sig", low_memory=False)
    if "uniform_variable" not in S.columns:
        return 0, len(parts)
    # always refresh our split rows so a stale notes/polarity from an earlier run
    # cannot persist (the polarity normalizer reads `notes`).
    _ours = [c for c in new_cols] + [c + "_01" for c in new_cols]
    S = S[~S["uniform_variable"].astype(str).isin(_ours)].copy()
    have = set(S["uniform_variable"].astype(str))
    pol_map = {str(r.get("uniform_variable")): str(r.get("dv_polarity") or "").strip()
               for _, r in S.iterrows()}
    notes_map = {str(r.get("uniform_variable")): str(r.get("notes") or "")
                 for _, r in S.iterrows()} if "notes" in S.columns else {}
    rows = []
    for c in new_cols:
        cs = str(c)
        ver = "true" if cs.endswith("_true") else "false"
        base = cs[:-5] if cs.endswith("_true") else cs[:-6]
        dom = _domain_of(base)
        # inherit the base DV's polarity when it is definitive (e.g. an accuracy
        # measure is higher_is_better for BOTH veracities); otherwise fall back to
        # the veracity-dependent rule (belief/behaviour: true->better, false->worse).
        bp = pol_map.get(base, "")
        note = notes_map.get(base, "")
        pol = bp if bp in ("higher_is_better", "higher_is_worse", "lower_is_better") \
            else _polarity(dom, ver, c, notes=note)
        if c not in have:
            rows.append({"uniform_variable": c, "role": "dv", "dv_domain": dom,
                         "veracity": ver, "timepoint": "", "dv_polarity": pol,
                         "notes": note})
        if c + "_01" not in have:
            rows.append({"uniform_variable": c + "_01", "role": "dv", "dv_domain": dom,
                         "veracity": ver, "timepoint": "", "dv_polarity": pol,
                         "notes": note})
    if rows:
        S = pd.concat([S, pd.DataFrame(rows)], ignore_index=True)
        S = S.drop_duplicates("uniform_variable", keep="first")
        S.to_csv(side, index=False, encoding="utf-8-sig")
    return len(new_cols), len(parts)