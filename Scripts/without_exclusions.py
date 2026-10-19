"""Compute the (c) element of the Reproduction reports: the eligible primary posttest
effect WITH author exclusions ON vs OFF.

Workstream C (2026-09-22).

Quantity
--------
The project's participant-level re-derivation is the SDT intervention effect on
discrimination, ``d'_treatment - d'_control`` at the posttest (see
``Scripts/analysis/reanalysis.py``, ``collect_datasets``).  This helper reuses that exact
production code read-only and calls it twice: once with the study's
``<Key> - exclusions.csv`` rules applied (ON) and once with every rule skipped (OFF,
``no_excl=True``).  The delta per study/experiment/domain is the change the author
exclusions make to the reproduced effect.

If the production code cannot be imported (a concurrent workstream may be editing it) or
the computation is impossible for a study, the helper records an explicit
``not_computed: <reason>`` -- it never invents a number.

Outputs (all under ``temp/ws_c/``)
----------------------------------
``without_exclusions.csv``      one row per (study_key, artifact, dv_domain, timepoint)
``without_exclusions.json``     per-study summary: value(s) or an explicit reason

Run:  python Scripts/without_exclusions.py
"""
import json
import os
import sys
import time

import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
TEMP = os.path.join(ROOT, "temp", "ws_c")
os.makedirs(TEMP, exist_ok=True)
DATASETS = os.path.join(ROOT, "Datasets")

OUT_CSV = os.path.join(TEMP, "without_exclusions.csv")
OUT_JSON = os.path.join(TEMP, "without_exclusions.json")
CACHE_ON = os.path.join(TEMP, "_disc_rows_on.csv")
CACHE_OFF = os.path.join(TEMP, "_disc_rows_off.csv")
CACHE_SIG = os.path.join(TEMP, "_disc_rows_sig.json")


def _code_sig():
    """Signature of the production modules the ON/OFF caches depend on.

    ON and OFF must be computed under the SAME code version; concurrent workstreams edit
    ``reanalysis.py``/``suite_common.py``/``stat_core.py``, so a cache built before an edit
    would make an exclusions delta appear where the rules changed nothing.  Any change
    invalidates BOTH caches together.
    """
    import hashlib
    parts = []
    for rel in ("analysis/reanalysis.py", "suite_common.py", "stat_core.py",
                "domain_restriction.py", "study_exclusions.py", "contract.py"):
        p = os.path.join(ROOT, "Scripts", rel)
        if os.path.isfile(p):
            try:
                st = os.stat(p)
                parts.append(f"{rel}:{int(st.st_mtime)}:{st.st_size}")
            except OSError:
                parts.append(f"{rel}:?")
        else:
            parts.append(f"{rel}:absent")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()

# Control / intervention roles used by reanalysis.py (via suite_common).  Kept here only
# as documentation; the actual classification is done inside the imported module.
_SDT_IMPORT_ERROR = None


def _load_sdt():
    """Import the SDT production module read-only; return (module, error_str)."""
    sys.path.insert(0, os.path.join(ROOT, "Scripts", "analysis"))
    try:
        import reanalysis  # noqa: E402
        return reanalysis, None
    except Exception as e:  # pragma: no cover - depends on concurrent edits
        return None, f"{type(e).__name__}: {e}"


def _has_dataset(key):
    return os.path.isdir(os.path.join(DATASETS, key))


def _has_harmonized(key):
    return os.path.isfile(os.path.join(DATASETS, key, key + " - harmonized.csv"))


def _exclusion_rules(key):
    """Return the parsed exclusion rules for a study (list of dicts)."""
    p = os.path.join(DATASETS, key, key + " - exclusions.csv")
    if not os.path.isfile(p):
        return None
    try:
        d = pd.read_csv(p, encoding="utf-8-sig")
    except Exception:
        return None
    return [dict(r) for _, r in d.iterrows()]


def _norm_tp(s):
    """Normalise a timepoint column for the ON/OFF join (NaN/'nan'/'' -> '')."""
    def one(v):
        if v is None or (isinstance(v, float) and v != v):
            return ""
        t = str(v).strip().lower()
        return "" if t in ("nan", "none", "nat", "<na>") else t
    return s.map(one)


def compute():
    rea, err = _load_sdt()
    if rea is None:
        result = {"_import_error": err}
        with open(OUT_JSON, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=1)
        print(f"  ! cannot import Scripts/analysis/reanalysis.py read-only: {err}")
        return None, None, err

    # Bypass the concurrent-write freshness guard so a just-run harmonizer pass does not
    # silently empty the result.
    rea._FORCE = True

    # Invalidate BOTH caches when the production code changed since they were built, so the
    # ON/OFF pair is always computed under one code version (no phantom exclusions delta).
    sig = _code_sig()
    stored = ""
    if os.path.isfile(CACHE_SIG):
        try:
            with open(CACHE_SIG, encoding="utf-8") as fh:
                stored = json.load(fh).get("sig", "")
        except Exception:
            stored = ""
    if stored != sig and (os.path.isfile(CACHE_ON) or os.path.isfile(CACHE_OFF)):
        print(f"  production code changed since the ON/OFF caches were built "
              f"({stored[:8]} -> {sig[:8]}); recomputing both")
        for p in (CACHE_ON, CACHE_OFF):
            try:
                os.remove(p)
            except OSError:
                pass

    t0 = time.time()
    if os.path.isfile(CACHE_ON):
        don = pd.read_csv(CACHE_ON, encoding="utf-8-sig")
        print(f"  using cached exclusions ON ({len(don)} cells)")
    else:
        print("  computing exclusions ON ...")
        on = rea.collect_datasets(no_excl=False, write_catalog=False)
        don = pd.DataFrame(on["disc_rows"])
        don.to_csv(CACHE_ON, index=False, encoding="utf-8-sig")
        print(f"    ON: {len(don)} disc cells in {time.time()-t0:.1f}s")
    t1 = time.time()
    if os.path.isfile(CACHE_OFF):
        doff = pd.read_csv(CACHE_OFF, encoding="utf-8-sig")
        print(f"  using cached exclusions OFF ({len(doff)} cells)")
    else:
        print("  computing exclusions OFF ...")
        off = rea.collect_datasets(no_excl=True, write_catalog=False)
        doff = pd.DataFrame(off["disc_rows"])
        doff.to_csv(CACHE_OFF, index=False, encoding="utf-8-sig")
        print(f"    OFF: {len(doff)} disc cells in {time.time()-t1:.1f}s")
    try:
        with open(CACHE_SIG, "w", encoding="utf-8") as fh:
            json.dump({"sig": sig, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}, fh)
    except OSError:
        pass

    if len(don):
        don["bucket"] = don["timepoint"].map(rea.time_bucket)
    else:
        don["bucket"] = pd.Series(dtype=str)
    if len(doff):
        doff["bucket"] = doff["timepoint"].map(rea.time_bucket)
    else:
        doff["bucket"] = pd.Series(dtype=str)

    keys = ["study_key", "artifact", "dv_domain", "timepoint"]
    # posttest cells only (the eligible primary posttest effect)
    cols = keys + ["d", "vi", "n_treat", "n_control"]
    pon = don[don["bucket"] == "post"][cols].copy()
    poff = doff[doff["bucket"] == "post"][cols].copy()
    # Join on a NORMALISED timepoint: the ON and OFF long frames can carry the same cell as
    # float NaN vs the literal string 'nan' (or a stale cache), and an outer merge on the raw
    # value then splits one cell into two half-rows (d_on in one, d_off in the other).  Treat
    # every blank/'nan' label as the empty string so the ON/OFF halves always rejoin.
    pon["tp_key"] = _norm_tp(pon["timepoint"])
    poff["tp_key"] = _norm_tp(poff["timepoint"])
    m = pon.merge(poff, on=["study_key", "artifact", "dv_domain", "tp_key"], how="outer",
                  suffixes=("_on", "_off"))
    m["timepoint"] = m["timepoint_on"].where(m["timepoint_on"].notna(), m["timepoint_off"])
    m = m.drop(columns=["timepoint_on", "timepoint_off", "tp_key"])
    m["delta"] = m["d_off"] - m["d_on"]
    m.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    # ---- per-study summary -----------------------------------------------------
    summary = {}
    for key in sorted(set(don["study_key"]) | set(doff["study_key"])):
        sub = m[m["study_key"] == key]
        sub_post = sub[sub["d_on"].notna() | sub["d_off"].notna()]
        rules = _exclusion_rules(key)
        n_rules = len(rules) if rules is not None else 0
        if len(sub_post) == 0:
            continue
        cells = []
        for _, r in sub_post.iterrows():
            cells.append({
                "artifact": r["artifact"], "dv_domain": r["dv_domain"],
                "timepoint": r["timepoint"],
                "d_on": None if pd.isna(r["d_on"]) else round(float(r["d_on"]), 4),
                "d_off": None if pd.isna(r["d_off"]) else round(float(r["d_off"]), 4),
                "delta": None if pd.isna(r["delta"]) else round(float(r["delta"]), 4),
                "n_treat_on": None if pd.isna(r.get("n_treat_on")) else int(r["n_treat_on"]),
                "n_control_on": None if pd.isna(r.get("n_control_on")) else int(r["n_control_on"]),
                "n_treat_off": None if pd.isna(r.get("n_treat_off")) else int(r["n_treat_off"]),
                "n_control_off": None if pd.isna(r.get("n_control_off")) else int(r["n_control_off"]),
            })
        summary[key] = {"computed": True, "n_posttest_cells": len(cells),
                        "n_exclusion_rules": n_rules, "cells": cells,
                        "on_source": "Scripts/analysis/reanalysis.py collect_datasets(no_excl=False)",
                        "off_source": "Scripts/analysis/reanalysis.py collect_datasets(no_excl=True)"}

    with open(OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    print(f"  wrote {len(summary)} study summaries -> {os.path.relpath(OUT_JSON, ROOT)}")

    # ---- per-rule exclusion provenance (C2: cumulative + leave-one-rule-out) ----
    try:
        prov = compute_rule_provenance(force=False)
        print(f"  exclusion provenance: {len(prov)} rule-study entries -> "
              f"{os.path.relpath(PROV_JSON, ROOT)}")
    except Exception as e:  # never break the ON/OFF product on a provenance failure
        print(f"  ! exclusion provenance failed: {type(e).__name__}: {e}")
    return summary, m, None


# ============================================================================
# Per-rule exclusion provenance
# ============================================================================
PROV_JSON = os.path.join(TEMP, "exclusion_provenance.json")
PROV_CSV = os.path.join(TEMP, "exclusion_provenance.csv")

_ROLE_CACHE = {}


def _rule_json(r):
    return {"flag": r.get("flag"), "keep": r.get("keep"),
            "applies_to": list(r.get("applies_to") or []),
            "reason": r.get("reason", "")}


def _prep_long(rea, SC, DR, SE, folder, key):
    """Melt a study's ``dv_*_01`` columns exactly as ``collect_datasets`` does.

    Read-only reuse of the production helpers so the exclusion provenance is
    computed on the same long frame (same DV filter, polarity orientation and
    arm roles) as the ON/OFF engine.
    """
    f = os.path.join(folder, key + " - harmonized.csv")
    if not os.path.isfile(f):
        return None
    try:
        if SE.is_fulltext_excluded(key):
            return None
        if not SC.include_fully_within() and SC.study_fully_within(key):
            return None
    except Exception:
        pass
    meta = rea.meta_for(folder)
    try:
        d = pd.read_csv(f, encoding="utf-8-sig", low_memory=False)
    except Exception:
        return None
    if "condition" not in d.columns and "condition_raw" in d.columns:
        d = d.rename(columns={"condition_raw": "condition"})
    dvcols = [c for c in d.columns if str(c).startswith("dv_") and str(c).endswith("_01")]
    dvcols = [c for c in dvcols
              if not (DR.is_non_outcome(meta.get(str(c)[:-3], ("", "", "", ""))[0])
                      or SC.is_metacognition(meta.get(str(c)[:-3], ("", "", "", ""))[0],
                                             str(c)[:-3]))]
    if not dvcols:
        return None
    idcols = [c for c in ("study_key", "artifact", "pid", "condition") if c in d.columns]
    flagcols = [c for c in d.columns if str(c).startswith("flag_")]
    idcols = idcols + [c for c in flagcols if c not in idcols]
    long = d.melt(id_vars=idcols, value_vars=dvcols, var_name="dv_01", value_name="value_0_1")
    long = long.dropna(subset=["value_0_1"])
    if not len(long):
        return None
    if "artifact" not in long.columns:
        long["artifact"] = ""
    long["dv"] = long["dv_01"].str[:-3]
    long["dv_domain"] = long["dv"].map(lambda v: meta.get(v, ("", "", "", ""))[0])
    long["veracity"] = long["dv"].map(lambda v: meta.get(v, ("", "", "", ""))[1])
    long["timepoint"] = long["dv"].map(lambda v: meta.get(v, ("", "", "", ""))[2])
    long["polarity"] = long["dv"].map(lambda v: meta.get(v, ("", "", "", ""))[3])
    long = long[~long["dv_domain"].isin(DR.NON_OUTCOME_DOMAINS)]
    if not len(long):
        return None
    try:
        ex_labels = SE.excluded_labels(key)
    except Exception:
        ex_labels = set()
    if ex_labels and "artifact" in long.columns:
        long = long[~long["artifact"].astype(str).isin(ex_labels)]
        if not len(long):
            return None
    rev = long["polarity"].astype(str).str.strip().str.lower().isin(
        ["higher_is_worse", "lower_is_better", "reverse", "reversed", "negative"])
    long = long.copy()
    long.loc[rev, "value_0_1"] = 1.0 - long.loc[rev, "value_0_1"]
    if "condition" in long.columns:
        _role_cache = {}

        def _cond_role(c, a):
            kk = (str(a), str(c))
            if kk not in _role_cache:
                try:
                    _role_cache[kk] = SC.classify_condition(c, key, str(a), folder=folder)[0]
                except Exception:
                    _role_cache[kk] = "unknown"
            return _role_cache[kk]

        art_series = (long["artifact"] if "artifact" in long.columns
                      else pd.Series("", index=long.index))
        long["_role"] = [_cond_role(c, a) for c, a in zip(long["condition"], art_series)]
    else:
        long["_role"] = "missing"
    return long


def _cells(rea, SC, long):
    """{(artifact, dv_domain, timepoint): {d, n_treat, n_control}} for the arm effect."""
    out = {}
    if long is None or not len(long):
        return out
    for (art, dom, tp), g in long.groupby(["artifact", "dv_domain", "timepoint"],
                                          dropna=False):
        ri = rea._cell_dprime(g[g["_role"] == "intervention"])
        rc = rea._cell_dprime(g[g["_role"].isin(SC.COND_CONTROL_ROLES)])
        if ri is None or rc is None:
            continue
        out[(str(art), str(dom), str(tp))] = {
            "d": ri["d"] - rc["d"], "n_treat": ri["n_pid"], "n_control": rc["n_pid"]}
    return out


def _pidset(long):
    if long is None or "pid" not in long.columns:
        return None
    return set(long["pid"].dropna().astype(str))


def _diff_cells(before, after):
    """Participants removed going before -> after (per result cell)."""
    out = []
    for k, av in before.items():
        bv = after.get(k, {"d": None, "n_treat": 0, "n_control": 0})
        nt = int(av["n_treat"] - bv["n_treat"])
        nc = int(av["n_control"] - bv["n_control"])
        out.append({
            "artifact": k[0], "dv_domain": k[1], "timepoint": k[2],
            "n_treat_removed": nt, "n_control_removed": nc, "n_removed": nt + nc,
            "d_before": av["d"], "d_after": bv["d"],
            "d_delta": (av["d"] - bv["d"]) if (av["d"] is not None and bv["d"] is not None)
            else None})
    out.sort(key=lambda r: (r["artifact"], r["dv_domain"], r["timepoint"]))
    return out


def compute_rule_provenance(force=False):
    """Per study: each exclusion rule's participants removed overall and per cell.

    Cumulative (rule j on top of rules 1..j-1) and leave-one-rule-out (rule j
    dropped from the full set), on the same long frame as the ON/OFF engine.
    Cached in ``temp/ws_c/exclusion_provenance.json``; nothing is invented -- a
    study with no computable frame records an explicit reason.
    """
    if os.path.isfile(PROV_JSON) and not force:
        try:
            with open(PROV_JSON, encoding="utf-8") as fh:
                cached = json.load(fh)
            if cached.get("_sig") == _code_sig():
                return cached
        except Exception:
            pass
    rea, err = _load_sdt()
    if rea is None:
        return {}
    rea._FORCE = True
    try:
        rea._LOG_ACTIVE = False
    except Exception:
        pass
    import suite_common as SC            # noqa: E402
    import domain_restriction as DR      # noqa: E402
    import study_exclusions as SE        # noqa: E402

    out = {}
    rows = []
    for key in sorted(os.listdir(DATASETS)):
        folder = os.path.join(DATASETS, key)
        if not os.path.isdir(folder):
            continue
        rules = rea.load_exclusions(folder)
        if not rules:
            continue
        long = _prep_long(rea, SC, DR, SE, folder, key)
        if long is None:
            out[key] = {"study_key": key, "n_rules": len(rules),
                        "rules": [_rule_json(r) for r in rules], "computed": False,
                        "reason": "no eligible participant-level long frame "
                                  "(no harmonized data / no control arm / fully-within)"}
            continue
        base_cells = _cells(rea, SC, long)
        base_pids = _pidset(long)
        prov = {"study_key": key, "n_rules": len(rules),
                "rules": [_rule_json(r) for r in rules],
                "computed": bool(base_cells), "cumulative": [], "loro": [],
                "source": "reanalysis helpers (read-only) on the harmonized long frame"}
        if not base_cells:
            prov["reason"] = "no eligible posttest SDT cell (no true/false veracity / no control)"
        # cumulative
        prev, prev_cells, prev_pids = long, base_cells, base_pids
        for j, r in enumerate(rules):
            cur = rea.apply_exclusions(prev, [r], study=key)
            cur_cells = _cells(rea, SC, cur)
            cur_pids = _pidset(cur)
            removed = _diff_cells(prev_cells, cur_cells)
            entry = {"order": j + 1, "rule": _rule_json(r), "cells": removed,
                     "n_removed_cell_sum": sum(c["n_removed"] for c in removed),
                     "n_treat_removed": sum(c["n_treat_removed"] for c in removed),
                     "n_control_removed": sum(c["n_control_removed"] for c in removed),
                     "n_participants_removed": (len(prev_pids - cur_pids)
                                                if (prev_pids is not None and cur_pids is not None)
                                                else None)}
            prov["cumulative"].append(entry)
            rows.append({"study_key": key, "mode": "cumulative", "order": j + 1,
                         "flag": r.get("flag"), "keep": r.get("keep"),
                         "applies_to": ";".join(r.get("applies_to") or []),
                         "n_participants_removed": entry["n_participants_removed"],
                         "n_removed_cell_sum": entry["n_removed_cell_sum"],
                         "n_treat_removed": entry["n_treat_removed"],
                         "n_control_removed": entry["n_control_removed"]})
            prev, prev_cells, prev_pids = cur, cur_cells, cur_pids
        # leave-one-rule-out
        for j, r in enumerate(rules):
            others = [x for i, x in enumerate(rules) if i != j]
            cur = rea.apply_exclusions(long, others, study=key)
            cur_cells = _cells(rea, SC, cur)
            cur_pids = _pidset(cur)
            removed = _diff_cells(cur_cells, base_cells)      # without_j -> all rules
            entry = {"order": j + 1, "rule": _rule_json(r), "cells": removed,
                     "n_removed_cell_sum": sum(c["n_removed"] for c in removed),
                     "n_treat_removed": sum(c["n_treat_removed"] for c in removed),
                     "n_control_removed": sum(c["n_control_removed"] for c in removed),
                     "n_participants_removed": (len(cur_pids - base_pids)
                                                if (cur_pids is not None and base_pids is not None)
                                                else None)}
            prov["loro"].append(entry)
            rows.append({"study_key": key, "mode": "loro", "order": j + 1,
                         "flag": r.get("flag"), "keep": r.get("keep"),
                         "applies_to": ";".join(r.get("applies_to") or []),
                         "n_participants_removed": entry["n_participants_removed"],
                         "n_removed_cell_sum": entry["n_removed_cell_sum"],
                         "n_treat_removed": entry["n_treat_removed"],
                         "n_control_removed": entry["n_control_removed"]})
        out[key] = prov
    out["_sig"] = _code_sig()
    with open(PROV_JSON, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    if rows:
        pd.DataFrame(rows).to_csv(PROV_CSV, index=False, encoding="utf-8-sig")
    return out



# ============================================================================
# Report updating (tasks 3-6 of the workstream brief)
# ============================================================================
import re
import shutil
import unicodedata

REPRO = os.path.join(ROOT, "Reproduction")
EXTRACTION = os.path.join(ROOT, "Extraction")
RESULTS = os.path.join(ROOT, "Results")
BACKUP = os.path.join(ROOT, "archive", "ws_c_backup_2026-09-22", "Reproduction")
AUDIT_CSV = os.path.join(RESULTS, "reproduction_report_audit.csv")

STUB_KEYS = {
    "Alsaad & AlDossary (2024)", "Cameron et al. (2026)", "Cook et al. (2024)",
    "Malik et al. (2022)", "McGrew (2020)", "McKinney et al. (2025)",
    "Nygren et al. (2021)", "Pimentel (2025)",
    "Schubatzky & Haagen-Schützenhöfer (2022)", "Zarzosa & Ruvalcaba (2024)",
}
NOTRUN_KEYS = {
    "Bago et al. (2022)", "Cook et al. (2017)", "Ecker et al. (2023)",
    "Ecker et al. (2023 b)", "Jamieson et al. (2025)", "Linegar et al. (2025)",
    "McPhedran et al. (2023)", "Orosz et al. (2023)", "Paynter et al. (2019)",
    "Reynolds & Estrada-Reynolds (2026)", "Saling et al. (2024)",
    "Siebert & Siebert (2023)", "Siebert & Siebert (2024)",
    "Somoray & Miller (2023)", "Tay et al. (2022)", "Udry & Barber (2025)",
    "van Huijstee et al. (2026)", "Xue et al. (2024)",
}
NO_DATA_STUBS = STUB_KEYS - {"Cook et al. (2024)"}

# (e)-section heading starts, used to place the new (c) section before failures/notes
E_HEAD_RE = re.compile(
    r"^##\s+(Discrepancies|Notes|To resolve|Dataset status|Column decode|"
    r"Non-independence|Non-independent|Discrepancies / blockers)\b", re.I)
C_HEAD_RE = re.compile(r"^##\s+(?:\(c\)\s+)?Results without exclusions\b", re.I | re.M)
ANY_H2_RE = re.compile(r"^##\s+")

AVAIL_LABEL = {1: "open URL", 2: "upon request", 3: "not available", 4: "not stated"}


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _backup(path):
    os.makedirs(BACKUP, exist_ok=True)
    dst = os.path.join(BACKUP, os.path.basename(path))
    if not os.path.exists(dst):
        shutil.copy2(path, dst)


def _canon_key(k):
    """Normalise a study key for case/unicode-insensitive matching."""
    k = unicodedata.normalize("NFKC", str(k))
    k = k.replace("\u2010", "-").replace("\u2011", "-").replace("\u2013", "-")
    k = k.replace("\u2014", "-").replace("\u2019", "'")
    return re.sub(r"\s+", " ", k).strip().casefold()


def _canon_h1_key(core):
    """Title-case an ALL-CAPS surname and normalise fancy hyphens in an H1 key.

    Only the pre-parenthesis region is touched, so study tags such as ``(LEAIND)`` or
    ``(LR23)`` are never altered.
    """
    core = core.replace("\u2010", "-").replace("\u2011", "-").replace("\u2019", "'")
    i = core.find("(")
    head, tail = (core[:i], core[i:]) if i != -1 else (core, "")

    def fix_word(m):
        w = m.group(0)
        return w.title() if w.isupper() and len(w) > 2 else w
    head = re.sub(r"\b[A-Z][A-Z'\-]+\b", fix_word, head)
    return head + tail


def _load_c_summary():
    if not os.path.isfile(OUT_JSON):
        return {}
    with open(OUT_JSON, encoding="utf-8") as fh:
        return json.load(fh)


def _load_repro_summary():
    try:
        d = pd.read_excel(os.path.join(RESULTS, "reproduction_summary.xlsx"),
                          sheet_name="Reproduction")
        return {str(r["key"]): r for _, r in d.iterrows()}
    except Exception:
        return {}


def _extraction_experiments(key):
    p = os.path.join(EXTRACTION, key + ".json")
    if not os.path.isfile(p):
        return []
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return []


def _canonical_study_key(key):
    """Map a report filename stem to the canonical study key (title-case H1 text)."""
    return _canon_h1_key(key)


def _avail_text(exps):
    if not exps:
        return "not stated"
    e = exps[0]
    st = e.get("dataset_availability_status")
    lab = AVAIL_LABEL.get(st, "not stated")
    url = e.get("data_url")
    repo = e.get("data_repository")
    bits = [lab]
    if repo and str(repo).lower() not in ("none", "nan"):
        r = str(repo).strip()
        if r.casefold() != lab.casefold():
            bits.append(r)
    if url and str(url).lower() not in ("none", "nan"):
        bits.append(str(url))
    return " — ".join(bits)


def _parse_md_within(key):
    """Parse the 'Within effects coded' table from Extraction/<Key>.md."""
    p = os.path.join(EXTRACTION, key + ".md")
    if not os.path.isfile(p):
        return []
    lines = _read(p).splitlines()
    rows, started = [], False
    for ln in lines:
        s = ln.strip()
        if s.lower().startswith("## within effects"):
            started = True
            continue
        if started:
            if s.startswith("##"):
                break
            if s.startswith("|"):
                cells = [c.strip() for c in s.strip("|").split("|")]
                if cells and not set("".join(cells)) <= set("-: "):
                    if cells[0].lower() not in ("dv", "outcome"):
                        rows.append(cells)
    return rows


def _stat_str(dm):
    for k in ("yi", "d_val", "g_val", "t_val", "F_val", "r_val", "z_val", "beta_val",
              "log_odds", "odds_ratio", "eta2_p"):
        v = dm.get(k)
        if v is not None:
            return f"{k}={round(float(v), 4) if isinstance(v, float) else v}"
    if dm.get("m1i") is not None:
        return (f"m1={dm.get('m1i')} (SD {dm.get('sd1i')}) vs m2={dm.get('m2i')} "
                f"(SD {dm.get('sd2i')})")
    return "no numeric statistic coded"


def _eligible_rows(key, limit=12):
    rows = []
    for e in _extraction_experiments(key):
        for dm in e.get("DV_Metrics", []):
            if dm.get("eligible_primary") == 1:
                rows.append((e.get("Study_ID"), dm.get("dv_name"),
                             dm.get("comparison_label"), _stat_str(dm)))
    return rows, len(rows)


def _raw_files(key):
    folder = os.path.join(DATASETS, key)
    if not os.path.isdir(folder):
        return []
    skip_markers = (" - prepared", " - harmonized", " - variables", " - dv_direction",
                    " - exclusions", " - conditions", " - codebook")
    out = []
    for root, _dirs, files in os.walk(folder):
        for fn in sorted(files):
            low = fn.lower()
            if low in ("harmonizer.py", "source.md", "readme.txt") or low.endswith(".md"):
                continue
            if low.endswith("- reproduction.py") or low.endswith(".pdf"):
                continue
            if any(m in low for m in skip_markers):
                continue
            if low.endswith((".csv", ".xlsx", ".xls", ".dta", ".sav", ".rda", ".rdata",
                             ".txt", ".tsv", ".json")):
                out.append(os.path.relpath(os.path.join(root, fn), folder).replace("\\", "/"))
    return sorted(out)


def _clean_reason(text):
    """Strip dangling `reported_effects.*` / `sandbox/prepared/` references."""
    text = re.sub(r"\s*\(see `?reported_effects\.(?:md|csv)`?\)", "", str(text))
    text = re.sub(r"`?reported_effects\.(?:md|csv)`?", "the coded effects", text)
    text = text.replace("sandbox/prepared/", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _c_block(key, csum, has_data, harmonized, fully_within):
    """Return (kind, markdown) for the 'Results without exclusions' section body."""
    info = csum.get(key)
    if info and info.get("computed") and info.get("n_posttest_cells"):
        cells = info["cells"]
        lines = [
            "Signal-detection re-analysis of the harmonized participant data "
            "(`d'_treatment - d'_control` at posttest; "
            "`Scripts/analysis/reanalysis.py`). \"Without exclusions\" reruns the identical "
            "computation with every `<Key> - exclusions.csv` rule skipped. Workstream C, "
            "2026-09-22; raw values in `temp/ws_c/without_exclusions.csv`.",
            "",
            "| Experiment | Domain | d' with exclusions | d' without exclusions | Delta | "
            "n treat/control (ON -> OFF) |",
            "|---|---|---:|---:|---:|---|",
        ]
        for c in cells:
            def _f(x):
                return "—" if x is None else f"{x:+.4f}"
            nt = (f"{c['n_treat_on']}/{c['n_control_on']} -> "
                  f"{c['n_treat_off']}/{c['n_control_off']}")
            lines.append(f"| {c['artifact']} | {c['dv_domain']} | {_f(c['d_on'])} | "
                         f"{_f(c['d_off'])} | {_f(c['delta'])} | {nt} |")
        lines.append("")
        if info.get("n_exclusion_rules", 0) == 0:
            lines.append(f"`{key} - exclusions.csv` is header-only (the study has no author "
                         "exclusions), so the two columns are identical by construction "
                         "(delta 0).")
        else:
            all_zero = all(abs((c.get("delta") or 0.0)) < 1e-9 for c in cells)
            if all_zero:
                lines.append(f"The study declares {info['n_exclusion_rules']} author "
                             "exclusion rule(s), but they leave this posttest cell "
                             "unchanged (delta 0) — no participant contributing to the cell "
                             "is dropped by the rules that apply to these DVs.")
            else:
                lines.append(f"The study applies {info['n_exclusion_rules']} author exclusion "
                             "rule(s) (see the Exclusions section above); the delta is the "
                             "change those rules make to the reproduced posttest effect.")
        return "computed", "\n".join(lines)

    if not has_data:
        return "not_applicable", ("not_applicable (no public dataset — no exclusions exist "
                                  "to remove).")
    if not harmonized:
        return "not_computed", ("not_computed: participant data exist but the dataset is not "
                                "harmonized, so the SDT re-derivation cannot be run.")
    if fully_within:
        return "not_computed", ("not_computed: no control arm (fully-within study), so the "
                                "intervention-vs-control SDT effect is undefined.")
    return "not_computed", ("not_computed: no eligible posttest SDT contrast (the harmonized "
                            "DVs carry no true/false stimulus veracity, so no discrimination "
                            "index can be formed).")


def _insert_c(text, body):
    section = "## Results without exclusions\n\n" + body.strip() + "\n"
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if E_HEAD_RE.match(ln):
            return "\n".join(lines[:i]) + "\n" + section + "\n" + "\n".join(lines[i:]) + "\n"
    # no (e) heading: append before a trailing '## Reproduce' if any, else at end
    for i, ln in enumerate(lines):
        if re.match(r"^##\s+Reproduce\b", ln, re.I):
            return "\n".join(lines[:i]) + "\n" + section + "\n" + "\n".join(lines[i:]) + "\n"
    return text.rstrip() + "\n\n" + section


def _c_section_only(key, body):
    return "## (c) Results without exclusions\n\n" + body.strip() + "\n"


def _rewrite_no_data_stub(key, csum, repro):
    exps = _extraction_experiments(key)
    avail = _avail_text(exps)
    nrec = exps[0].get("N_Recruited") if exps else None
    nana = exps[0].get("N_Analyzed") if exps else None
    rows = _parse_md_within(key)
    dv_lines = ["| DV | Domain | n | Effect (yi) | SE | Source statistic |",
                "|---|---|---:|---:|---:|---|"]
    for r in rows:
        if len(r) >= 6:
            dv_lines.append("| " + " | ".join(r[:6]) + " |")
    if len(dv_lines) == 2:
        for e in exps:
            for dm in e.get("DV_Metrics", []):
                if dm.get("eligible_primary") == 1:
                    dv_lines.append(
                        f"| {dm.get('dv_name')} | {dm.get('dv_domain')} | "
                        f"{dm.get('n_total') or dm.get('n1i') or ''} | "
                        f"{_stat_str(dm)} | | |")
    reason = (f"no public participant-level dataset ({avail}); this is a fully-within "
              f"single-arm study (no no-misinformation-intervention control), so the "
              f"eligible effects are within-subject pre->post changes only and nothing "
              f"was recomputed from participant data.")
    ckind, cbody = _c_block(key, csum, has_data=False, harmonized=False, fully_within=True)
    txt = f"""# Reproduction report - {_canonical_study_key(key)}

<!-- REPRO_DONE -->
**reproduced:** no
**reason:** {reason}

## (a) Original results
Fully-within study (single-arm pre->post); only the within-subject effects on
misinformation-susceptibility outcomes are coded (`contrast_type = 4`); between-arm effects
are deliberately excluded. N recruited = {nrec}, N analysed = {nana}. Coded eligible-primary
contrasts (`Extraction/{key}.json`):

{chr(10).join(dv_lines)}

## (b) Reproduced results
not run — no public participant-level dataset ({avail}). Nothing was fabricated; the within
effects are coded from the published summary statistics only.

{_c_section_only(key, cbody)}
## (d) Preparations to derive the dataset from raw data
not applicable — no raw participant-level data were obtained, so there is no raw -> dataset
derivation to document. Declared data availability: {avail}. See
`Datasets/{key}/SOURCE.md`.

## (e) Failures / discrepancies
- No public participant-level dataset ({avail}); the reproduction cannot be run from
  participant data and only the coded summary statistics enter the meta-analysis.
- Fully-within design: even with participant data, the between-arm SDT re-analysis is not
  defined (no control arm).
"""
    return txt


def _rewrite_data_stub(key, csum, repro):
    """Cook et al. (2024): a stub report for a study that DOES have harmonized data."""
    row = repro.get(key) if repro else None
    exps = _extraction_experiments(key)
    avail = _avail_text(exps)
    rows = _parse_md_within(key)
    dv_lines = ["| DV | Domain | n | Effect (yi) | SE | Source statistic |",
                "|---|---|---:|---:|---:|---|"]
    for r in rows:
        if len(r) >= 6:
            dv_lines.append("| " + " | ".join(r[:6]) + " |")
    recalc = str(row.get("recalc_vs_reported", "")) if row is not None else ""
    n_rep = int(row.get("n_reproduced", 0)) if row is not None else 0
    n_dir = int(row.get("n_direction", 0)) if row is not None else 0
    n_not = int(row.get("n_not_reproduced", 0)) if row is not None else 0
    n_c = int(row.get("n_contrasts", 0)) if row is not None else 0
    fully = bool(exps and exps[0].get("Fully_Within"))
    ckind, cbody = _c_block(key, csum, has_data=True, harmonized=True, fully_within=fully)
    txt = f"""# Reproduction report - {_canonical_study_key(key)}

<!-- REPRO_DONE -->
**reproduced:** partial
**reason:** public OSF data ({avail}) were harmonized and the pre/post credibility
discrimination recomputes from the participant data, but the paper's exact model was not
re-run; of {n_c} coded eligible-primary (within-subject pre->post) contrasts, {n_rep}
reproduced exactly, {n_dir} matched in direction only and {n_not} did not reproduce.

## (a) Original results
Fully-within study (single-arm pre->post). Coded eligible-primary contrasts
(`Extraction/{key}.json`):

{chr(10).join(dv_lines)}

## (b) Reproduced results
Partial — from the harmonized participant data (`Datasets/{key}/{key} - harmonized.csv`):
{recalc}. {n_c} coded contrasts: {n_rep} reproduced, {n_dir} direction-only,
{n_not} not reproduced.

{_c_section_only(key, cbody)}
## (d) Preparations to derive the dataset from raw data
Public OSF deposit ({avail}) -> `Datasets/{key}/CUV_Ghana_pilot.csv` ->
`{key} - prepared.csv` -> `{key} - harmonized.csv` (see `Datasets/{key}/harmonizer.py` and
`Datasets/{key}/SOURCE.md`).

## (e) Failures / discrepancies
- The paper's exact within-subject model was not re-run; the harmonized participant data
  reproduce the pre/post discrimination but not every coded within-subject contrast
  ({n_not}/{n_c} not reproduced).
- Fully-within design: no control arm, so the between-arm SDT intervention effect is not
  defined.
"""
    return txt


def _rewrite_notrun(key, csum, repro, fully_within, harmonized):
    row = repro.get(key) if repro else None
    exps = _extraction_experiments(key)
    avail = _avail_text(exps)
    elig, n_elig = _eligible_rows(key)
    lines = ["| Experiment | DV | Contrast | Reported statistic |",
             "|---|---|---|---|"]
    for eid, dv, cmp_, st in elig[:12]:
        lines.append(f"| {eid} | {dv} | {cmp_} | {st} |")
    if n_elig > 12:
        lines.append(f"| … | … | ({n_elig - 12} more) | … |")
    if n_elig == 0:
        lines = ["none coded (no eligible_primary = 1 contrast in "
                 f"`Extraction/{key}.json`)."]
    reason = _clean_reason((row.get("documented_reason") if row is not None else None) or
                           "no author analysis/wrangling script was recovered, so the "
                           "paper's exact model was not re-run; the coded PDF-extracted "
                           "effects enter the meta-analysis instead.")
    reason = re.sub(r"##.*", "", reason).strip().rstrip(".")
    raw = _raw_files(key)
    raw_txt = ", ".join(f"`{r}`" for r in raw[:8]) if raw else "none recorded"
    ckind, cbody = _c_block(key, csum, has_data=(harmonized or bool(_raw_files(key))),
                            harmonized=harmonized, fully_within=fully_within)
    txt = f"""# Reproduction report - {_canonical_study_key(key)}

<!-- REPRO_DONE -->
**reproduced:** no
**reason:** {reason}.

## (a) Original results (coded eligible-primary effects)
The paper's eligible-primary effects are coded in `Extraction/{key}.json`; they could not be
recomputed from the public data. {n_elig} coded eligible-primary contrast(s):

{chr(10).join(lines)}

## (b) Reproduced results
not run — {reason}. The coded PDF-extracted effects enter the meta-analysis instead.

{_c_section_only(key, cbody)}
## (d) Preparations to derive the dataset from raw data
Raw source file(s): {raw_txt}. Derived to `{key} - prepared.csv` and
`{key} - harmonized.csv` by `Datasets/{key}/harmonizer.py` (declared availability: {avail}).
See `Datasets/{key}/SOURCE.md`.

## (e) Failures / discrepancies
- {reason}. The harmonized participant dataset exists but the author's analysis script was
  not recovered, so the paper's exact model could not be re-run; the reproduction is limited
  to the signal-detection re-derivation in the section above.
"""
    return txt


def _fix_fendt_b(text):
    """Fendt et al. (2026 b): one file, two experiments; fix nested H1s."""
    text = re.sub(r"^# Fendt et al\. \(2026 b\) [—-] reproduction report \(two experiments\)\s*$",
                  "# Reproduction report - Fendt et al. (2026 b)", text, count=1, flags=re.M)
    text = re.sub(r"^# Reproduction report [—-] FendtFriction_Exp1\s*$",
                  "*Coded key: `Fendt2026_Friction_Exp1`*", text, count=1, flags=re.M)
    text = re.sub(r"^# Reproduction report [—-] FendtFriction_Exp2\s*$",
                  "*Coded key: `Fendt2026_Friction_Exp2`*", text, count=1, flags=re.M)
    return text


def _fix_pluviano(text):
    i = text.find("\n## Reproduce")
    if i != -1:
        text = text[:i].rstrip() + "\n"
    return text


def _ab_missing_blocks(key, csum, repro, has_a, has_b):
    """Build (a)/(b) sections for a report whose structural heading is absent."""
    blocks = []
    if not has_a:
        elig, n_elig = _eligible_rows(key)
        if n_elig:
            lines = ["| Experiment | DV | Contrast | Reported statistic |",
                     "|---|---|---|---|"]
            for eid, dv, cmp_, st in elig[:8]:
                lines.append(f"| {eid} | {dv} | {cmp_} | {st} |")
            if n_elig > 8:
                lines.append(f"| … | … | ({n_elig - 8} more) | … |")
            body = (f"The paper's eligible-primary effects are coded in "
                    f"`Extraction/{key}.json` ({n_elig} contrast(s)):\n\n"
                    + "\n".join(lines))
        else:
            body = (f"No eligible-primary contrast is coded for this study "
                    f"(`Extraction/{key}.json`).")
        blocks.append("## (a) Original results\n\n" + body)
    if not has_b:
        row = repro.get(key) if repro else None
        status = str(row.get("status", "")).strip().lower() if row is not None else ""
        recalc = _clean_reason(row.get("recalc_vs_reported")) if row is not None else ""
        reason = _clean_reason(row.get("documented_reason")) if row is not None else ""
        reason = re.sub(r"##.*", "", reason).strip().rstrip(".")
        if status in ("reproduced", "partial") and recalc:
            body = f"Reproduced (status: {status}): {recalc}."
        elif status in ("reproduced", "partial"):
            body = (f"Reproduced (status: {status}); see the reproduction details in the "
                    "body of this report.")
        elif reason:
            body = f"not reproduced — {reason}."
        else:
            body = ("not reproduced — no participant-level reproduction is documented for "
                    "this study.")
        blocks.append("## (b) Reproduced results\n\n" + body)
    return blocks


def add_missing_ab(dry_run=False):
    """Insert explicit (a)/(b) sections where the structural heading is absent."""
    import parse_reproduction_reports as P
    csum = _load_c_summary()
    repro = _load_repro_summary()
    audit = {r["key"]: r for r in P.parse_all(include_text=False)}
    n = 0
    for fn in sorted(os.listdir(REPRO)):
        if not fn.endswith(".md") or fn.startswith("_BATCH"):
            continue
        key = os.path.splitext(fn)[0]
        path = os.path.join(REPRO, fn)
        a = audit.get(key)
        if a is None:
            continue
        has_a = bool(a["has_a_original"])
        has_b = bool(a["has_b_reproduced"])
        if has_a and has_b:
            continue
        text = _read(path)
        blocks = _ab_missing_blocks(key, csum, repro, has_a, has_b)
        if not blocks:
            continue
        m = C_HEAD_RE.search(text)
        if not m:
            continue
        insert = "\n".join(blocks) + "\n\n"
        text = text[:m.start()] + insert + text[m.start():]
        if not dry_run:
            _backup(path)
            _write(path, text)
        n += 1
    print(f"  {'[dry-run] ' if dry_run else ''}added (a)/(b) sections to {n} reports")


def add_missing_d(dry_run=False):
    """Insert an explicit (d) preparations section where the heading is absent."""
    import parse_reproduction_reports as P
    audit = {r["key"]: r for r in P.parse_all(include_text=False)}
    n = 0
    for fn in sorted(os.listdir(REPRO)):
        if not fn.endswith(".md") or fn.startswith("_BATCH"):
            continue
        key = os.path.splitext(fn)[0]
        a = audit.get(key)
        if a is None or a["has_d_preparations"]:
            continue
        path = os.path.join(REPRO, fn)
        text = _read(path)
        m = C_HEAD_RE.search(text)
        if not m:
            continue
        exps = _extraction_experiments(key)
        avail = _avail_text(exps)
        raw = _raw_files(key)
        raw_txt = ", ".join(f"`{r}`" for r in raw[:8]) if raw else "none recorded"
        harm = os.path.isfile(os.path.join(DATASETS, key, key + " - harmonized.csv"))
        if harm:
            body = (f"Raw source file(s): {raw_txt}. Derived to `{key} - prepared.csv` and "
                    f"`{key} - harmonized.csv` by `Datasets/{key}/harmonizer.py` "
                    f"(declared availability: {avail}). See `Datasets/{key}/SOURCE.md`.")
        elif raw:
            body = (f"Raw source file(s): {raw_txt}. The dataset is not harmonized "
                    f"(declared availability: {avail}). See `Datasets/{key}/SOURCE.md`.")
        else:
            body = ("not applicable — no raw participant-level data were obtained "
                    f"(declared availability: {avail}). See `Datasets/{key}/SOURCE.md`.")
        insert = "## (d) Preparations to derive the dataset from raw data\n\n" + body + "\n\n"
        text = text[:m.start()] + insert + text[m.start():]
        if not dry_run:
            _backup(path)
            _write(path, text)
        n += 1
    print(f"  {'[dry-run] ' if dry_run else ''}added (d) sections to {n} reports")


def _fix_h1(text, stem):
    """Canonicalise the first H1 study key (ALL-CAPS surname, fancy hyphens)."""
    m = re.match(r"^#\s+(.*)$", text, re.M)
    if not m:
        return text
    h1 = m.group(1)
    new = _canon_h1_key(h1)
    if new != h1:
        return text[:m.start(1)] + new + text[m.end(1):]
    return text


def _status_after_apply():
    """Re-run the parser and print the missing-element summary."""
    import parse_reproduction_reports as P
    recs = P.parse_all(include_text=False)
    df = pd.DataFrame(recs)
    df.to_csv(AUDIT_CSV, index=False, encoding="utf-8-sig")
    print("\nAFTER apply:")
    for col, label in (("has_a_original", "(a)"), ("has_b_reproduced", "(b)"),
                       ("has_c_without_exclusions", "(c)"), ("has_d_preparations", "(d)"),
                       ("has_e_failures", "(e)")):
        miss = df[~df[col]]
        print(f"  {label}: {int(df[col].sum())}/{len(df)} present; {len(miss)} missing")
        if len(miss):
            print("      " + ", ".join(miss["key"].tolist()))
    print(f"  dangling refs remaining: {int((df['dangling_refs'] != '').sum())}")
    return df


def apply_reports(dry_run=False):
    csum = _load_c_summary()
    if not csum:
        print("  ! temp/ws_c/without_exclusions.json missing; run compute() first")
        return
    repro = _load_repro_summary()
    n_changed = n_c = 0
    kinds = {"computed": 0, "not_computed": 0, "not_applicable": 0}
    for fn in sorted(os.listdir(REPRO)):
        if not fn.endswith(".md") or fn.startswith("_BATCH"):
            continue
        key = os.path.splitext(fn)[0]
        path = os.path.join(REPRO, fn)
        text = _read(path)
        orig = text
        has_data = os.path.isfile(os.path.join(DATASETS, key, key + " - harmonized.csv"))
        harmonized = has_data
        if not has_data:
            # case/unicode-insensitive fallback to a dataset folder
            for d in os.listdir(DATASETS):
                if _canon_key(d) == _canon_key(key):
                    harmonized = os.path.isfile(os.path.join(DATASETS, d, d + " - harmonized.csv"))
                    has_data = bool(_raw_files(d)) or harmonized
                    break
        exps = _extraction_experiments(key)
        fully_within = bool(exps and all(e.get("Fully_Within") for e in exps))

        if key in STUB_KEYS:
            text = _rewrite_no_data_stub(key, csum, repro) if key in NO_DATA_STUBS \
                else _rewrite_data_stub(key, csum, repro)
        elif key in NOTRUN_KEYS:
            text = _rewrite_notrun(key, csum, repro, fully_within, harmonized)
        elif C_HEAD_RE.search(text) is None:
            _, cbody = _c_block(key, csum, has_data=has_data, harmonized=harmonized,
                                fully_within=fully_within)
            text = _insert_c(text, cbody)
            n_c += 1

        # task 6 specials
        if key == "Fendt et al. (2026 b)":
            text = _fix_fendt_b(text)
        if key == "Fendt et al. (2026 x)":
            text = re.sub(r"^#\s+.*$",
                          "# Reproduction report - Fendt et al. (2026 x) (Morality)",
                          text, count=1, flags=re.M)
        if key == "Pluviano et al. (2022)":
            text = _fix_pluviano(text)
        text = _fix_h1(text, key)

        if text != orig:
            if not dry_run:
                _backup(path)
                _write(path, text)
            n_changed += 1
            # tally (c) kind for the audit
            m = re.search(r"## Results without exclusions\s*\n+\s*(.*)", text)
            body = m.group(1)[:40].lower() if m else ""
            if "not_applicable" in body:
                kinds["not_applicable"] += 1
            elif "not_computed" in body:
                kinds["not_computed"] += 1
            else:
                kinds["computed"] += 1
    print(f"  {'[dry-run] ' if dry_run else ''}changed {n_changed} reports "
          f"({n_c} got a new (c) section)")
    print(f"  (c) kinds among changed: {kinds}")
    if not dry_run:
        add_missing_ab(dry_run=False)
        add_missing_d(dry_run=False)
        _status_after_apply()


def split_fendt_b(dry_run=False):
    """Attempt to split the Fendt b report when a separate Extraction key exists."""
    key = "Fendt et al. (2026 b)"
    p2 = os.path.join(EXTRACTION, "Fendt et al. (2026 b2).json")
    if not os.path.isfile(p2):
        print("  Fendt et al. (2026 b): no separate Extraction key "
              "(`Fendt et al. (2026 b2).json`); keeping one file with two H1-labelled "
              "experiment sections.")
        return False
    print("  Fendt et al. (2026 b): separate Extraction key found; split not implemented "
          "for an unknown schema — left as one file.")
    return False


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply-reports", action="store_true",
                    help="rewrite the Reproduction/*.md reports (tasks 3-6)")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --apply-reports: do not write, only report changes")
    args = ap.parse_args()
    if args.apply_reports:
        apply_reports(dry_run=args.dry_run)
        split_fendt_b(dry_run=args.dry_run)
    else:
        compute()
