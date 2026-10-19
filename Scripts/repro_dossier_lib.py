# -*- coding: utf-8 -*-
"""Shared engine for the machine-generated per-study reproduction dossier.

Track C / workstream W1-C.  One row per coded eligible-primary contrast, merging (in
priority order):

  1. ``Extraction/<Key>.json`` ``DV_Metrics``            -> the paper's ORIGINAL statistic
  2. ``Datasets|temp/<Key> - repro_results.csv``          -> reproduced values (bespoke)
  3. ``Datasets|temp/<Key> - repro_facts.json``           -> reproduced values (wrappers)
  4. ``Results/reproduction_summary.xlsx`` ``Domains``    -> reproduced values (cached)
  5. the ``Reproduction/<Key>.md`` markdown table         -> reproduced values (prose)
  6. ``temp/ws_c/without_exclusions.json`` + ``exclusion_provenance.json``
                                                          -> without-exclusions + per-rule drops

Every numeric value carries a provenance pointer (``extraction_ref`` /
``source_location`` / ``reproduced_source`` / ``exclusions_applied_json``); anything a
study genuinely cannot provide becomes an explicit reason in ``gaps_json`` -- never an
invented number.

The module owns no writing; ``build_repro_dossier.py`` serialises it.
"""
import json
import os
import re
import sys
import unicodedata

try:
    import pandas as pd
except Exception:                      # pragma: no cover
    pd = None

_HERE = os.path.dirname(os.path.abspath(__file__))     # Scripts
ROOT = os.path.dirname(_HERE)
SCRIPTS = _HERE
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
RESULTS = os.path.join(ROOT, "Results")
DATASETS = os.path.join(ROOT, "Datasets")
EXTRACTION = os.path.join(ROOT, "Extraction")
REPRO = os.path.join(ROOT, "Reproduction")
TEMP = os.path.join(ROOT, "temp")
WS_C = os.path.join(TEMP, "ws_c")
DOSSIER_DIR = os.path.join(RESULTS, "repro_dossier")
DURABLE_LOGS = os.path.join(RESULTS, "repro_logs")

# the runner lives in Scripts/analysis; make its helpers (md table, safe_slug) importable
_ANALYSIS = os.path.join(SCRIPTS, "analysis")
if _ANALYSIS not in sys.path:
    sys.path.insert(0, _ANALYSIS)

SCHEMA_VERSION = 1

# The metric label attached to ``domain_recalc_*`` columns: these are the domain-level
# mean(true) - mean(false) recalculations from the facts dict, NOT the coded contrast effect.
DOMAIN_RECALC_METRIC = "domain mean_diff (mean_true-mean_false), not the coded effect"

# Exact interface §7 column order.
RESULT_COLUMNS = [
    "contrast_id", "study_id", "dv_name", "dv_domain", "contrast_type",
    "eligible_primary",
    "original_value", "original_metric", "original_ci_low", "original_ci_high",
    "original_p", "original_n1", "original_n2",
    "source_location", "source_quote", "extraction_ref",
    "reproduced_value", "reproduced_metric", "reproduced_estimand",
    "reproduced_n_used", "delta_abs", "delta_rel", "verdict", "verdict_basis",
    "reproduced_source",
    "domain_recalc_d", "domain_recalc_metric", "domain_recalc_n",
    "without_exclusions_estimand", "woe_d_on", "woe_d_off", "woe_delta",
    "woe_n_treat_on", "woe_n_control_on", "woe_n_treat_off", "woe_n_control_off",
    "exclusions_applied_json", "how", "gaps_json",
]

INDEX_COLUMNS = [
    "study_key", "generated_at", "schema_version", "dataset_backed", "has_dataset",
    "report_present", "dossier_path",
    "reproduced_status", "machine_status", "documented_status",
    "n_contrasts", "n_original", "n_reproduced", "n_reproduced_numeric",
    "n_domain_recalc_numeric", "n_reproduced_side_numeric",
    "n_participants_reproduced",
    "n_not_reproduced", "n_not_comparable", "n_not_checkable", "n_gap_rows",
    "numeric_pair_studies_marker",
    "n_woe_cells", "n_woe_full", "n_without_exclusions", "without_exclusions_source",
    "woe_d_on", "woe_d_off", "woe_delta",
    "woe_n_treat_on", "woe_n_control_on", "woe_n_treat_off", "woe_n_control_off",
    "n_exclusion_rules", "n_exclusion_provenance_rules", "exclusion_provenance_source",
    "original_results", "reproduced_results", "results_without_exclusions",
    "preparations", "failures", "discrepancy_count",
    "n_paper_contrasts", "report_path", "dataset_path", "log_path", "command",
    "exit_code", "runtime_s", "notes",
]

# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def num(x):
    """Robust float: accepts numbers and strings like ``'yi=0.32'`` / ``'0.32'``."""
    if x is None:
        return None
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        try:
            f = float(x)
            return f if f == f and abs(f) != float("inf") else None
        except Exception:
            return None
    s = str(x).strip()
    if s == "" or s.lower() in ("nan", "none", "na", "n/a", "<na>", "nat"):
        return None
    m = _NUM_RE.search(s)
    if not m:
        return None
    try:
        f = float(m.group(0))
        return f if f == f and abs(f) != float("inf") else None
    except Exception:
        return None


def first_num(x):
    """Leading number only (so prose like 'I2=74%' does not yield 74)."""
    if x is None:
        return None
    m = re.match(r"\s*[*_`]*\s*[≈~]?\s*([-−]?\d+(?:\.\d+)?)", str(x))
    if not m:
        return num(x)
    try:
        return float(m.group(1).replace("−", "-"))
    except Exception:
        return None


_STRICT_NUM_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


def strict_num(x):
    """Float only when the WHOLE value is one number -- never a number inside prose.

    ``num`` deliberately mines the first number out of ``'yi=0.32'``; that is wrong for a
    reproduced value: ``'F(2,562) = 4.33'`` must NOT become ``2.0`` and ``'−0.146'`` must keep
    its (unicode) minus sign.  Returns ``None`` for anything that is not a single number, so a
    cross-estimand string can never masquerade as a reproduced effect.
    """
    if x is None:
        return None
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        try:
            f = float(x)
            return f if f == f and abs(f) != float("inf") else None
        except Exception:
            return None
    s = str(x).strip()
    s = s.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    s = s.strip("`*_ ").strip()
    if not _STRICT_NUM_RE.match(s):
        return None
    try:
        return float(s)
    except Exception:
        return None


def norm_key(k):
    k = unicodedata.normalize("NFKC", str(k))
    for a, b in (("\u2010", "-"), ("\u2011", "-"), ("\u2013", "-"),
                 ("\u2014", "-"), ("\u2019", "'")):
        k = k.replace(a, b)
    return re.sub(r"\s+", " ", k).strip().casefold()


def _tokens(s):
    return {w for w in re.findall(r"[a-z0-9]+", str(s).lower()) if len(w) > 2}


def _sim(a, b):
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def contrast_id(key, study_id, dv_name, comparison_label):
    return f"{key}|{study_id}|{dv_name}|{comparison_label}"


def _join_domain(v):
    if isinstance(v, (list, tuple)):
        return ";".join(str(x) for x in v if str(x).strip())
    return "" if v is None else str(v)


def _estimand_label(metric):
    """Honest estimand label for a reproduced value.

    Only an explicitly signal-detection metric is labelled ``d'_treatment - d'_control``; any
    other metric is reported verbatim.  The old code guessed "d (…)" == SDT, which mislabelled
    every survey-standardised effect.
    """
    m = str(metric or "")
    low = m.lower()
    if "d'" in low or "dprime" in low or "d\u2032" in low or "discrimination" in low:
        return "d'_treatment - d'_control"
    return m


# ---------------------------------------------------------------------------
# 1. Originals -- Extraction DV_Metrics (eligible primary)
# ---------------------------------------------------------------------------
_ORIG_METRICS = ["yi", "d_val", "g_val", "t_val", "F_val", "r_val", "z_val",
                 "beta_val", "log_odds", "odds_ratio", "eta2_p"]


def load_experiments(key):
    p = os.path.join(EXTRACTION, key + ".json")
    if not os.path.isfile(p):
        return []
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    return [e for e in (data or []) if isinstance(e, dict)]


def _pick_original(dv):
    """(value, metric, ci_low, ci_high, p) for one coded DV_Metrics row."""
    yi = num(dv.get("yi"))
    vi = num(dv.get("vi"))
    if yi is not None:
        lo = hi = None
        if vi is not None and vi > 0:
            import math
            lo, hi = yi - 1.96 * math.sqrt(vi), yi + 1.96 * math.sqrt(vi)
        return yi, "yi", lo, hi, _pval(dv)
    for m in _ORIG_METRICS[1:]:
        v = num(dv.get(m))
        if v is not None:
            return v, m, None, None, _pval(dv)
    m1, m2 = num(dv.get("m1i")), num(dv.get("m2i"))
    if m1 is not None and m2 is not None:
        return m1 - m2, "mean_diff (m1-m2)", None, None, _pval(dv)
    if m1 is not None:
        return m1, "m1", None, None, _pval(dv)
    return None, "", None, None, _pval(dv)


def _pval(dv):
    for k in ("p_val", "p_value", "pval", "p"):
        v = num(dv.get(k))
        if v is not None:
            return v
    return None


def extract_originals(key):
    """One dict per eligible-primary DV_Metrics row (the authoritative contrast set)."""
    out = []
    for ei, e in enumerate(load_experiments(key)):
        sid = str(e.get("Study_ID") or "").strip()
        for di, dv in enumerate(e.get("DV_Metrics") or []):
            if not isinstance(dv, dict):
                continue
            if int(num(dv.get("eligible_primary")) or 0) != 1:
                continue
            val, metric, lo, hi, p = _pick_original(dv)
            dv_name = str(dv.get("dv_name") or "").strip()
            cmp_label = str(dv.get("comparison_label") or "").strip()
            gaps = []
            if val is None:
                gaps.append("original_metric_missing")
            src_loc = str(dv.get("source_location") or "").strip()
            src_quote = str(dv.get("source_quote") or "").strip()
            if not src_loc:
                gaps.append("missing_source_location")
            if not src_quote:
                gaps.append("missing_source_quote")
            out.append({
                "study_id": sid,
                "dv_name": dv_name,
                "comparison_label": cmp_label,
                "dv_domain": _join_domain(dv.get("dv_domain")),
                "contrast_type": dv.get("contrast_type"),
                "eligible_primary": 1,
                "original_value": val, "original_metric": metric,
                "original_ci_low": lo, "original_ci_high": hi, "original_p": p,
                "original_n1": num(dv.get("n1i")), "original_n2": num(dv.get("n2i")),
                "source_location": src_loc, "source_quote": src_quote,
                "extraction_ref": f"Extraction/{key}.json#{sid}/DV_Metrics[{di}]",
                "gaps": gaps,
                "_label": dv_name,
                "_sim_source": dv_name,
                "_timepoint": dv.get("timepoint_1") or dv.get("timepoint_2"),
            })
    return out


# ---------------------------------------------------------------------------
# 2-5. Reproduced candidates (priority order)
# ---------------------------------------------------------------------------
def _durable_or_temp(key, suffix):
    for base in (os.path.join(DATASETS, key), TEMP):
        p = os.path.join(base, key + " - " + suffix)
        if os.path.isfile(p):
            return p
    return ""


_EFFECT_COL_RE = re.compile(r"^(reproduced|recovered|recalc|replicated)(.*)$", re.I)


def _is_effect_col(c):
    """True when a reproduced column is an effect-size estimate (not a t/p/F/SE/N).

    ``reproduced`` / ``recovered_estimate`` / ``reproduced_d`` / ``reproduced_b_fe`` are
    effects; ``reproduced_p`` / ``reproduced_t`` / ``reproduced_F`` / ``reproduced_se`` are
    not -- differencing a test statistic against a coded effect is a cross-estimand error.
    """
    m = _EFFECT_COL_RE.match(str(c).strip())
    if not m:
        return False
    core = m.group(2).lstrip("_").strip().lower()
    if core in ("", "d", "g", "b", "beta", "r", "z", "eta2p", "eta2_p", "estimate",
                "coef", "coefficient", "effect"):
        return True
    return core.startswith(("b_", "beta_", "d_", "g_"))


def load_repro_results(key):
    """Rows from ``<Key> - repro_results.csv`` (bespoke scripts), if present."""
    p = _durable_or_temp(key, "repro_results.csv")
    if not p or pd is None:
        return []
    try:
        df = pd.read_csv(p, encoding="utf-8-sig")
    except Exception:
        return []
    label_col = next((c for c in ("effect_id", "effect", "experiment", "study", "dv",
                                  "outcome", "label", "contrast", "country", "condition",
                                  "item", "metric", "name", "quantity", "quantity_label")
                      if c in df.columns), df.columns[0] if len(df.columns) else None)
    repd_cols = [c for c in df.columns if _EFFECT_COL_RE.match(str(c))]
    # effect-size columns first (file order preserved), so a p/t/F column is only ever a
    # fallback and never the primary reproduced value.
    repd_cols = ([c for c in repd_cols if _is_effect_col(c)]
                 + [c for c in repd_cols if not _is_effect_col(c)])
    reported_cols = [c for c in df.columns if str(c).startswith("reported")]
    out = []
    for i, row in df.iterrows():
        label = str(row[label_col]) if label_col else f"row {i + 1}"
        dom = str(row.get("domain", "") or row.get("dv_domain", "") or "")
        rep_v = rep_m = None
        for rc in repd_cols:
            if not _is_effect_col(rc):
                continue
            v = num(row[rc])
            if v is not None:
                rep_v, rep_m = v, str(rc)
                break
        repd_v = None
        for pc in reported_cols:
            v = num(row[pc])
            if v is not None:
                repd_v = v
                break
        out.append({"label": label, "domain": dom, "reproduced_value": rep_v,
                    "reproduced_metric": rep_m or "", "original_cross": repd_v,
                    "verdict": str(row.get("verdict", "") or ""),
                    "note": str(row.get("note", "") or row.get("notes", "") or ""),
                    "delta_ok": True,
                    "priority": 1, "source": os.path.relpath(p, ROOT).replace("\\", "/")})
    return out


def _rows_from_lib_facts(lib):
    """Reproduced rows from the dict ``repro_lib.reproduce()`` returns."""
    if not isinstance(lib, dict):
        return []
    out = []
    verdicts = {}
    for vkey, vname in (("reproduced", "REPRODUCED"),
                        ("direction", "reproduced in direction"),
                        ("not_reproduced", "NOT REPRODUCED"),
                        ("not_comparable", "NOT COMPARABLE"),
                        ("not_checkable", "NOT CHECKABLE")):
        for pair in lib.get(vkey) or []:
            if isinstance(pair, (list, tuple)) and pair:
                verdicts[(str(pair[0]), str(pair[1]) if len(pair) > 1 else "")] = vname
    for r in lib.get("recalc") or []:
        if not isinstance(r, dict):
            continue
        out.append({"label": "", "domain": str(r.get("domain") or ""),
                    "reproduced_value": num(r.get("d")),
                    "reproduced_metric": "d (mean true - mean false)",
                    "original_cross": None, "verdict": "", "note": "recalc", "priority": 2,
                    "source": "repro_facts.json:lib_facts.recalc"})
    for (dom, dvname), vname in verdicts.items():
        out.append({"label": dvname, "domain": dom, "reproduced_value": None,
                    "reproduced_metric": "", "original_cross": None, "verdict": vname,
                    "note": "", "priority": 2, "source": "repro_facts.json:lib_facts"})
    return out


def load_repro_facts(key):
    p = _durable_or_temp(key, "repro_facts.json")
    if not p:
        return []
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    rel = os.path.relpath(p, ROOT).replace("\\", "/")
    doms = data.get("domains")
    if isinstance(doms, list) and doms and isinstance(doms[0], dict):
        out = []
        for d in doms:
            out.append({"label": str(d.get("contrast") or d.get("label") or ""),
                        "domain": str(d.get("domain") or ""),
                        "reproduced_value": strict_num(d.get("recalc_twin")),
                        "reproduced_metric": "d (mean true - mean false)",
                        "original_cross": num(d.get("coded_effect")),
                        "verdict": str(d.get("verdict") or ""),
                        "note": str(d.get("note") or ""),
                        # a prose/md-table twin may be a mean or a test statistic, never
                        # differenced against the coded effect
                        "delta_ok": "md" not in str(d.get("source") or "").lower(),
                        "priority": 2,
                        "source": rel + ":domains"})
        return out
    rows = _rows_from_lib_facts(data.get("lib_facts"))
    for r in rows:
        r["source"] = rel + ":" + r["source"]
    if rows:
        return rows
    return _rows_from_lib_facts(data)


def load_recalc_rows(key):
    """The domain-level ``recalc_rows`` a facts file carries (per domain × timepoint).

    Each row is ``{domain, timepoint, n_true, n_false, mean_true, mean_false, d}``.  These are
    the *domain* mean-difference recalculations; they are NOT the per-contrast coded effect and
    must never be differenced against one (see ``build_study``).
    """
    p = _durable_or_temp(key, "repro_facts.json")
    if not p:
        return []
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    out = []
    for r in data.get("recalc_rows") or []:
        if not isinstance(r, dict):
            continue
        d = strict_num(r.get("d"))
        if d is None:
            continue
        out.append({
            "domain": str(r.get("domain") or "").strip().lower(),
            "timepoint": r.get("timepoint"),
            "bucket": _tp_bucket(r.get("timepoint")),
            "d": d,
            "n": (strict_num(r.get("n_true")) or strict_num(r.get("n_false"))
                  or strict_num(r.get("n"))),
            "n_true": strict_num(r.get("n_true")),
            "n_false": strict_num(r.get("n_false")),
            "source": "repro_facts.json:recalc_rows",
        })
    return out


def _tp_bucket(label):
    """Route a timepoint label to post / followup / other for recalc-row matching.

    Blank and ``'nan'`` labels follow the reanalysis convention (posttest): the sidecar leaves
    the posttest timepoint blank, so a blank recalc row is the posttest row.
    """
    raw = label
    if raw is None or (isinstance(raw, float) and raw != raw):
        raw = ""
    raw = str(raw).strip()
    if raw.lower() in ("", "nan", "none", "na", "n/a", "(blank)", "blank"):
        return "post"
    try:
        import contract
        b = contract.classify_timepoint(raw)
    except Exception:
        b = raw.lower()
    return "post" if b in ("post", "post_assumed") else b


def load_domains(key, domains_df):
    """Rows from ``reproduction_summary.xlsx`` Domains (source priority 3)."""
    if domains_df is None or not len(domains_df):
        return []
    sub = domains_df[domains_df["key"] == key]
    out = []
    for _, r in sub.iterrows():
        out.append({"label": str(r.get("contrast") or ""),
                    "domain": str(r.get("domain") or ""),
                    "reproduced_value": strict_num(r.get("recalc_twin")),
                    "reproduced_metric": "d (mean true - mean false)",
                    "original_cross": first_num(r.get("coded_effect")),
                    "verdict": str(r.get("result") or ""),
                    "note": str(r.get("note") or ""),
                    "delta_ok": "md" not in str(r.get("source") or "").lower(),
                    "priority": 3,
                    "source": "Results/reproduction_summary.xlsx:Domains[" +
                              str(r.get("source") or "") + "]"})
    return out


def load_md_table(key):
    """Rows from the Reproduction markdown table (source priority 4)."""
    try:
        import run_reproducers as RR
    except Exception:
        RR = None
    if RR is None:
        return []
    out = []
    for r in RR.md_repro_table(key):
        out.append({"label": r.get("label", ""), "domain": "",
                    "reproduced_value": RR._first_num(r.get("reproduced")),
                    "reproduced_metric": "value",
                    "original_cross": RR._first_num(r.get("reported")),
                    "verdict": r.get("verdict", ""), "note": r.get("match_raw", ""),
                    "delta_ok": False,      # prose table: means / t / F, never differenced
                    "priority": 4,
                    "source": f"Reproduction/{key}.md (md table)"})
    return out


# ---------------------------------------------------------------------------
# matching reproduced candidates to originals
# ---------------------------------------------------------------------------
def _match_candidate(orig, cands):
    """Best reproduced candidate for one original contrast: (cand, basis) or (None, '')."""
    if not cands:
        return None, ""
    oname = orig["dv_name"].lower()
    olabel = (orig.get("comparison_label") or "").lower()
    odom = (orig.get("dv_domain") or "").lower()
    # 1. exact label
    for c in cands:
        if c["label"].strip().lower() == oname and c["label"].strip():
            return c, "match_exact_dv_name"
    # 2. label contained in coded comparison / dv name (or vice versa)
    for c in cands:
        cl = c["label"].strip().lower()
        if not cl:
            continue
        if cl in oname or oname in cl or cl in olabel:
            return c, "match_substring"
    # 3. domain equal + fuzzy label
    best, bs = None, 0.0
    for c in cands:
        if c["domain"] and odom and c["domain"].lower() != odom.split(";")[0]:
            continue
        s = _sim(c["label"], orig["dv_name"] + " " + (orig.get("comparison_label") or ""))
        if s > bs:
            best, bs = c, s
    if best is not None and bs >= 0.5:
        return best, f"match_fuzzy({bs:.2f})"
    # 4. unique domain-only match
    dom_c = [c for c in cands if c["domain"] and odom
             and c["domain"].lower() == odom.split(";")[0]]
    if len(dom_c) == 1:
        return dom_c[0], "match_domain_unique"
    return best, (f"match_fuzzy({bs:.2f})" if best is not None and bs > 0 else "")


# ---------------------------------------------------------------------------
# 6. without-exclusions + exclusion provenance
# ---------------------------------------------------------------------------
def load_woe():
    p = os.path.join(WS_C, "without_exclusions.json")
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def load_provenance():
    p = os.path.join(WS_C, "exclusion_provenance.json")
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _domain_recalc_for(orig, recalc_rows):
    """The recalc row for one original contrast, matched on (dv_domain, timepoint bucket).

    Returns ``None`` unless a recalc row exists for the SAME domain AND the same timepoint
    bucket -- a follow-up recalc is never attached to a posttest contrast.
    """
    odom = (orig.get("dv_domain") or "").split(";")[0].strip().lower()
    if not odom or not recalc_rows:
        return None
    ob = _tp_bucket(orig.get("_timepoint"))
    same = [r for r in recalc_rows if r["domain"] == odom]
    for r in same:
        if r["bucket"] == ob:
            return r
    return None


def _pick_woe_cell(woe_study, dv_domain, artifact):
    """Best posttest without-exclusions cell for a result row."""
    if not woe_study:
        return None
    odom = (dv_domain or "").split(";")[0].strip().lower()
    cands = [c for c in (woe_study.get("cells") or [])
             if str(c.get("dv_domain") or "").lower() == odom]
    if not cands:
        return None
    if artifact:
        for c in cands:
            if str(c.get("artifact") or "") == str(artifact):
                return c
    return cands[0]


def _prov_for(dv_domain, provenance):
    """The provenance entry (rules + cumulative/loro cells) for a result domain."""
    if not provenance:
        return None
    return provenance


def _prov_cells(provenance, mode, dv_domain):
    """Per-rule per-cell removed counts for this domain, keyed by rule flag."""
    out = {}
    odom = (dv_domain or "").split(";")[0].strip().lower()
    for entry in provenance.get(mode) or []:
        cells = [c for c in entry.get("cells") or []
                 if str(c.get("dv_domain") or "").lower() == odom]
        if cells:
            out[str(entry.get("rule", {}).get("flag") or entry.get("order"))] = {
                "rule": entry.get("rule"), "order": entry.get("order"),
                "n_participants_removed": entry.get("n_participants_removed"),
                "cells": cells,
            }
    return out


# ---------------------------------------------------------------------------
# file inventory
# ---------------------------------------------------------------------------
_PRODUCT_SUFFIXES = (" - prepared", " - harmonized", " - variables", " - dv_direction",
                     " - exclusions", " - conditions", " - variable_assignments",
                     " - repro_results", " - repro_facts")


def _role_of(fn, key):
    low = fn.lower()
    if low == "harmonizer.py":
        return "harmonizer"
    if low == key.lower() + " - reproduction.py":
        return "reproduction script"
    if low.endswith(" - repro_results.csv"):
        return "repro results (D10)"
    if low.endswith(" - repro_facts.json"):
        return "repro facts (D10)"
    if low.endswith(" - harmonized.csv") or low.endswith(" - harmonized_variables.csv"):
        return "harmonized (product)"
    if low.endswith(" - prepared.csv"):
        return "prepared (product)"
    if low.endswith(" - variables.csv"):
        return "variables sidecar"
    if low.endswith(" - dv_direction.csv"):
        return "dv_direction sidecar"
    if low.endswith(" - exclusions.csv"):
        return "exclusions sidecar"
    if low.endswith(" - conditions.csv"):
        return "conditions sidecar"
    if low.endswith(" - variable_assignments.csv"):
        return "assignments sidecar"
    if low in ("source.md", "readme.txt", "readme.md"):
        return "provenance"
    if "codebook" in low or "code_book" in low:
        return "codebook"
    if "survey" in low or "instrument" in low or "questionnaire" in low:
        return "survey instrument"
    if low.endswith(".pdf"):
        return "document"
    if low.endswith((".sav", ".dta", ".rds", ".rdata", ".rda", ".xlsx", ".xls", ".csv",
                     ".tsv", ".json", ".txt", ".zip", ".7z")):
        return "raw data"
    return "other"


def file_inventory(key):
    folder = os.path.join(DATASETS, key)
    if not os.path.isdir(folder):
        return []
    scripts = []
    for fn in os.listdir(folder):
        if fn.endswith(".py"):
            try:
                with open(os.path.join(folder, fn), encoding="utf-8", errors="replace") as fh:
                    scripts.append((fn, fh.read()))
            except OSError:
                pass
    out = []
    for root, _dirs, files in os.walk(folder):
        for fn in sorted(files):
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, folder).replace("\\", "/")
            try:
                size = os.path.getsize(p)
            except OSError:
                size = None
            read_by = []
            for sname, stext in scripts:
                if fn in stext:
                    read_by.append(sname)
            prov = "SOURCE.md" if os.path.isfile(os.path.join(folder, "SOURCE.md")) else ""
            out.append({"file": rel, "role": _role_of(fn, key), "size_bytes": size,
                        "provenance": prov, "read_by": "; ".join(read_by)})
    return out


# ---------------------------------------------------------------------------
# run card
# ---------------------------------------------------------------------------
def _resolve_log(key, facts_log=""):
    """Locate a study's run log, preferring the facts dict's own ``log_path``.

    Resolution order: (1) the basename the facts dict recorded, mirrored into
    ``Results/repro_logs/`` (durable); (2) ``run_reproducers.safe_slug`` / ``legacy_slug``
    durable names; (3) the volatile ``temp/reproduce_logs/`` copy; (4) the recorded absolute
    path itself.  Returns "" when no log exists -- never a guessed path.
    """
    try:
        import run_reproducers as RR
    except Exception:
        RR = None
    names = []
    if facts_log:
        stem = os.path.splitext(os.path.basename(str(facts_log)))[0]
        if stem:
            names.append(stem)
    if RR is not None:
        for fn in ("safe_slug", "legacy_slug"):
            try:
                names.append(getattr(RR, fn)(key))
            except Exception:
                pass
    else:
        names.append(re.sub(r'[<>:"/\\|?*]', "", key))
    for n in names:
        p = os.path.join(DURABLE_LOGS, n + ".log")
        if os.path.isfile(p):
            return p
    for n in names:
        p = os.path.join(TEMP, "reproduce_logs", n + ".txt")
        if os.path.isfile(p):
            return p
    if facts_log and os.path.isfile(str(facts_log)):
        return str(facts_log)
    return ""


def _run_card(key):
    script = os.path.join("Datasets", key, key + " - reproduction.py")
    has_script = os.path.isfile(os.path.join(ROOT, script))
    card = {"script": script if has_script else "",
            "command": f'python "{script}"' if has_script else "",
            "batch_command": f'python Scripts/analysis/run_reproducers.py --only "{key}"',
            "log_path": "", "exit_code": None, "runtime_s": None}
    fp = _durable_or_temp(key, "repro_facts.json")
    facts_log = ""
    if fp:
        try:
            with open(fp, encoding="utf-8") as fh:
                data = json.load(fh)
            facts_log = str(data.get("log_path") or "")
            card["exit_code"] = data.get("rc", data.get("returncode"))
            card["runtime_s"] = data.get("runtime")
            card["n_rows"] = data.get("n_rows")
            card["status"] = data.get("status", "")
        except Exception:
            pass
    log = _resolve_log(key, facts_log)
    if log:
        try:
            card["log_path"] = os.path.relpath(log, ROOT).replace("\\", "/")
        except ValueError:                       # different drive -> keep absolute
            card["log_path"] = str(log)
    return card


# ---------------------------------------------------------------------------
# build one dossier
# ---------------------------------------------------------------------------
def build_study(key, domains_df, woe, prov, md_status_map=None, audit_map=None):
    originals = extract_originals(key)
    # A few studies repeat the same (dv_name, comparison_label) at several timepoints, so the
    # interface contrast_id is not unique there.  Disambiguate deterministically (occurrence
    # order) instead of dropping contrasts; the base formula is untouched when unique.
    from collections import Counter
    _base_ids = [contrast_id(key, o["study_id"], o["dv_name"], o["comparison_label"])
                 for o in originals]
    _cnt = Counter(_base_ids)
    _seen = {}
    for o, _b in zip(originals, _base_ids):
        if _cnt[_b] > 1:
            _seen[_b] = _seen.get(_b, 0) + 1
            o["_cid_override"] = f"{_b}#{_seen[_b]}"
    rr = load_repro_results(key)
    facts = load_repro_facts(key)
    doms = load_domains(key, domains_df)
    md = load_md_table(key)
    recalc_rows = load_recalc_rows(key)

    # drop invalid rows
    rr = [c for c in rr if c.get("label") or c.get("domain") or c.get("reproduced_value") is not None]
    facts = [c for c in facts if c.get("label") or c.get("domain")
             or c.get("reproduced_value") is not None or c.get("verdict")]
    md = [c for c in md if c.get("reproduced_value") is not None
          or c.get("original_cross") is not None or c.get("verdict")]

    # De-duplicate the same logical contrast across sources (keep the highest-priority
    # occurrence) so a lower-priority copy never resurfaces as an orphan row.  The key is the
    # label alone: the same contrast appears in repro_results.csv (domain "") and in the facts
    # domains list (a real domain), and must collapse to the higher-priority copy.  When the
    # higher-priority copy carries no verdict/note/domain, fill them from the lower-priority
    # duplicate so the documented verdict vocabulary is not lost.
    seen_lbl = {}

    def _dedupe(rows):
        out = []
        for c in rows:
            lbl = norm_key(c.get("label", ""))
            k = (lbl, "") if lbl else ("", norm_key(c.get("domain", "")))
            prev = seen_lbl.get(k)
            if prev is not None:
                for f in ("verdict", "note", "domain", "original_cross"):
                    if prev.get(f) in (None, "") and c.get(f) not in (None, ""):
                        prev[f] = c[f]
                # a lower-priority copy may be the only one carrying a numeric value; take
                # it (and its delta permission) only when the higher-priority copy has none
                if prev.get("reproduced_value") in (None, "") and \
                        c.get("reproduced_value") is not None:
                    prev["reproduced_value"] = c["reproduced_value"]
                    prev["reproduced_metric"] = c.get("reproduced_metric", "")
                    prev["delta_ok"] = c.get("delta_ok", True)
                continue
            seen_lbl[k] = c
            out.append(c)
        return out

    rr, facts, doms, md = _dedupe(rr), _dedupe(facts), _dedupe(doms), _dedupe(md)
    cands = rr + facts + doms + md        # already priority-ordered within each source
    used = set()

    woe_study = woe.get(key) or woe.get(key) or {}
    provenance = prov.get(key) or {}
    rows = []
    study_gaps = []
    if not originals and not cands:
        study_gaps.append("no_eligible_primary_contrast_coded")

    has_harm = os.path.isfile(os.path.join(DATASETS, key, key + "- harmonized.csv")) or \
        os.path.isfile(os.path.join(DATASETS, key, key + " - harmonized.csv"))
    fw = _fully_within(key)

    for o in originals:
        # choose the best-priority candidate that provides a numeric reproduced value;
        # fall back to the best-priority candidate with a verdict.  A same-label lower-priority
        # copy has already been merged into the higher-priority one, so a verdict-only runner
        # row keeps its documented NOT CHECKABLE verdict instead of being overridden by a
        # cross-estimand prose value.
        cand_best = None
        basis = ""
        for pool in (rr, facts, doms, md):
            available = [c for c in pool if id(c) not in used]
            cand, b = _match_candidate(o, available)
            if cand is not None and (cand.get("reproduced_value") is not None
                                     or (cand.get("verdict") and cand.get("verdict") != "")):
                cand_best, basis = cand, b
                break
        gaps = list(o["gaps"])
        cid = o.get("_cid_override") or contrast_id(key, o["study_id"], o["dv_name"],
                                                   o["comparison_label"])
        reproduced_value = reproduced_metric = reproduced_estimand = None
        reproduced_n = None
        verdict = ""
        reproduced_source = ""
        if cand_best is not None:
            used.add(id(cand_best))
            reproduced_value = cand_best.get("reproduced_value")
            reproduced_metric = cand_best.get("reproduced_metric") or ""
            reproduced_estimand = _estimand_label(reproduced_metric)
            reproduced_source = cand_best.get("source", "")
            verdict = cand_best.get("verdict", "") or ""
            reproduced_n = cand_best.get("n_used")
            if reproduced_value is None and not verdict:
                gaps.append("reproduced_value_missing")
        else:
            gaps.append("no_reproduced_contrast_matched")
        if o["original_value"] is None:
            gaps.append("original_metric_missing")
        delta_abs = delta_rel = None
        if o["original_value"] is not None and reproduced_value is not None:
            if cand_best is not None and not cand_best.get("delta_ok", True):
                # prose / test-statistic source: never difference against the coded effect
                gaps.append("different_estimand_not_differenced")
            else:
                delta_abs = reproduced_value - o["original_value"]
                if o["original_value"] != 0:
                    delta_rel = delta_abs / abs(o["original_value"])
        # without exclusions + provenance
        wcell = _pick_woe_cell(woe_study, o["dv_domain"], "")
        woe_vals = {k: None for k in ("without_exclusions_estimand", "woe_d_on", "woe_d_off",
                                      "woe_delta", "woe_n_treat_on", "woe_n_control_on",
                                      "woe_n_treat_off", "woe_n_control_off")}
        if wcell is not None:
            woe_vals.update({
                "without_exclusions_estimand": "d'_treatment - d'_control (posttest; ON vs OFF)",
                "woe_d_on": wcell.get("d_on"), "woe_d_off": wcell.get("d_off"),
                "woe_delta": wcell.get("delta"),
                "woe_n_treat_on": wcell.get("n_treat_on"),
                "woe_n_control_on": wcell.get("n_control_on"),
                "woe_n_treat_off": wcell.get("n_treat_off"),
                "woe_n_control_off": wcell.get("n_control_off")})
        else:
            if not has_harm:
                woe_vals["without_exclusions_estimand"] = "not_applicable: no harmonized data"
            elif fw:
                woe_vals["without_exclusions_estimand"] = "not_computed: fully-within (no control)"
            else:
                woe_vals["without_exclusions_estimand"] = \
                    "not_computed: no eligible posttest SDT cell"
        exclusions_json = _exclusions_json(key, provenance, o["dv_domain"])
        dr = _domain_recalc_for(o, recalc_rows)
        how = {
            "original": o["extraction_ref"],
            "reproduced": reproduced_source or "none",
            "without_exclusions": ("reanalysis.collect_datasets(no_excl=False/True)"
                                   if wcell is not None else "n/a"),
            "estimator": reproduced_estimand or reproduced_metric or "",
        }
        rows.append({
            "contrast_id": cid, "study_id": o["study_id"], "dv_name": o["dv_name"],
            "dv_domain": o["dv_domain"], "contrast_type": o["contrast_type"],
            "eligible_primary": o["eligible_primary"],
            "original_value": o["original_value"], "original_metric": o["original_metric"],
            "original_ci_low": o["original_ci_low"], "original_ci_high": o["original_ci_high"],
            "original_p": o["original_p"], "original_n1": o["original_n1"],
            "original_n2": o["original_n2"],
            "source_location": o["source_location"], "source_quote": o["source_quote"],
            "extraction_ref": o["extraction_ref"],
            "reproduced_value": reproduced_value, "reproduced_metric": reproduced_metric,
            "reproduced_estimand": reproduced_estimand, "reproduced_n_used": reproduced_n,
            "delta_abs": delta_abs, "delta_rel": delta_rel,
            "verdict": verdict, "verdict_basis": basis,
            "reproduced_source": reproduced_source,
            "domain_recalc_d": dr["d"] if dr else None,
            "domain_recalc_metric": DOMAIN_RECALC_METRIC if dr else "",
            "domain_recalc_n": dr["n"] if dr else None,
            **woe_vals,
            "exclusions_applied_json": exclusions_json,
            "how": json.dumps(how, ensure_ascii=False),
            "gaps_json": json.dumps(sorted(set(gaps)), ensure_ascii=False),
            # internal (stripped before CSV)
            "_is_original": True,
        })

    # reproduced candidates that no original matched -> orphan rows with a gap reason
    for c in cands:
        if id(c) in used:
            continue
        if c.get("reproduced_value") is None and not c.get("verdict"):
            continue
        gaps = ["no_original_coded"]
        reprod = c.get("reproduced_value")
        rows.append({
            "contrast_id": contrast_id(key, "", c.get("label", ""), c.get("domain", "")),
            "study_id": "", "dv_name": c.get("label", ""), "dv_domain": c.get("domain", ""),
            "contrast_type": None, "eligible_primary": None,
            "original_value": None, "original_metric": "", "original_ci_low": None,
            "original_ci_high": None, "original_p": None, "original_n1": None,
            "original_n2": None, "source_location": "", "source_quote": "",
            "extraction_ref": "",
            "reproduced_value": reprod, "reproduced_metric": c.get("reproduced_metric", ""),
            "reproduced_estimand": c.get("reproduced_metric", ""), "reproduced_n_used": None,
            "delta_abs": None, "delta_rel": None, "verdict": c.get("verdict", ""),
            "verdict_basis": "orphan_reproduced_row", "reproduced_source": c.get("source", ""),
            "domain_recalc_d": None, "domain_recalc_metric": "", "domain_recalc_n": None,
            "without_exclusions_estimand": "not_computed: no coded original to anchor the cell",
            "woe_d_on": None, "woe_d_off": None, "woe_delta": None,
            "woe_n_treat_on": None, "woe_n_control_on": None,
            "woe_n_treat_off": None, "woe_n_control_off": None,
            "exclusions_applied_json": _exclusions_json(key, provenance, c.get("domain", "")),
            "how": json.dumps({"original": "none", "reproduced": c.get("source", "")},
                              ensure_ascii=False),
            "gaps_json": json.dumps(gaps, ensure_ascii=False),
            "_is_original": False,
        })

    run = _run_card(key)
    # Final guarantee: contrast_id is unique inside a dossier (a few studies repeat a
    # (dv_name, comparison_label) or an orphan label).  Deterministic occurrence suffix.
    _seen_cid = {}
    for r in rows:
        cid = r["contrast_id"]
        _seen_cid[cid] = _seen_cid.get(cid, 0) + 1
        if _seen_cid[cid] > 1:
            r["contrast_id"] = f"{cid}#{_seen_cid[cid]}"
    status, machine_status, documented_status = _statuses(key, md_status_map, audit_map)
    dossier = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": None,           # stamped by the writer
        "study_key": key,
        "has_dataset": has_harm,
        "fully_within": fw,
        "file_inventory": file_inventory(key),
        "run": run,
        "status": status, "machine_status": machine_status,
        "documented_status": documented_status,
        "exclusion_rules": [_rule_public(r) for r in (provenance.get("rules") or [])],
        "exclusion_provenance": {
            "source": provenance.get("source", ""),
            "computed": provenance.get("computed"),
            "reason": provenance.get("reason", ""),
            "cumulative": provenance.get("cumulative", []),
            "loro": provenance.get("loro", []),
        },
        "results": rows,
        "study_gaps": study_gaps,
        "preparations": ("has_d=yes" if _has_report_section(key, "d", md_status_map, audit_map,
                                                            "has_d_preparations")
                         else "has_d=no"),
        "failures": _report_failures(key, audit_map),
        "discrepancy_count": _discrepancy_count(key, audit_map),
    }
    return dossier


def _fully_within(key):
    for e in load_experiments(key):
        if e.get("Fully_Within"):
            continue
        return False
    return bool(load_experiments(key))


def _rule_public(r):
    return {"flag": r.get("flag"), "keep": r.get("keep"),
            "applies_to": list(r.get("applies_to") or []), "reason": r.get("reason", "")}


def _exclusions_json(key, provenance, dv_domain):
    if not provenance or not provenance.get("rules"):
        return json.dumps({"rules": [], "note": "no author exclusion rules for this study"},
                          ensure_ascii=False)
    odom = (dv_domain or "").split(";")[0].strip().lower()
    rules = []
    cum_by_flag = _prov_cells(provenance, "cumulative", dv_domain)
    loro_by_flag = _prov_cells(provenance, "loro", dv_domain)
    for r in provenance.get("rules") or []:
        flag = str(r.get("flag"))
        rules.append({
            "flag": flag, "keep": r.get("keep"),
            "applies_to": list(r.get("applies_to") or []),
            "reason": r.get("reason", ""),
            "gates_this_domain": _gates(r, dv_domain),
            "cumulative": cum_by_flag.get(flag),
            "leave_one_out": loro_by_flag.get(flag),
        })
    return json.dumps({"rules": rules,
                       "source": "temp/ws_c/exclusion_provenance.json "
                                 "(reanalysis long frame, read-only)"},
                      ensure_ascii=False)


def _gates(rule, dv_domain):
    """Does this rule apply to the DV domain of the result row?"""
    apps = rule.get("applies_to") or []
    if not apps or any(p == "*" for p in apps):
        return True
    odom = (dv_domain or "").split(";")[0].strip().lower()
    for p in apps:
        if p.startswith("domain:") and p.split(":", 1)[1].strip().lower() == odom:
            return True
    return any(not p.startswith("domain:") for p in apps)


# ---------------------------------------------------------------------------
# report-level status / failure fields
# ---------------------------------------------------------------------------
def _load_audit(audit_map):
    return audit_map or {}


def _statuses(key, md_status_map, audit_map):
    machine = ""
    facts = load_repro_facts(key)
    try:
        import run_reproducers as RR
        for c in facts:
            pass
    except Exception:
        pass
    fp = _durable_or_temp(key, "repro_facts.json")
    if fp:
        try:
            with open(fp, encoding="utf-8") as fh:
                data = json.load(fh)
            machine = str(data.get("status", "") or "")
        except Exception:
            machine = ""
    doc = ""
    if audit_map and key in audit_map:
        doc = str(audit_map[key].get("status", "") or "")
    if md_status_map and key in md_status_map:
        doc = str(md_status_map[key] or "") or doc
    # normalise the report-parser vocabulary (yes/no/partial) onto the runner vocabulary
    doc = {"yes": "reproduced", "no": "not reproduced"}.get(doc, doc)
    status = machine or doc or "unknown"
    return status, machine, doc


def _has_report_section(key, letter, md_status_map, audit_map, col):
    if audit_map and key in audit_map:
        return bool(audit_map[key].get(col))
    return False


def _report_failures(key, audit_map):
    if audit_map and key in audit_map:
        return str(audit_map[key].get("discrepancies_lines", "") or "")
    return ""


def _discrepancy_count(key, audit_map):
    txt = _report_failures(key, audit_map)
    return len([x for x in txt.split(" | ") if x]) if txt else 0


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------
def summarize_for_index(dossier, path, dataset_path, report_path):
    rows = dossier.get("results") or []
    n_orig = sum(1 for r in rows if r.get("_is_original")
                 or (r.get("original_value") is not None))
    n_rep = sum(1 for r in rows if str(r.get("verdict", "")).upper() == "REPRODUCED"
                or str(r.get("verdict", "")).lower().startswith("reproduced (sign"))
    n_rep_numeric = sum(1 for r in rows if r.get("original_value") is not None
                        and r.get("reproduced_value") is not None)
    n_domain_recalc = sum(1 for r in rows if r.get("domain_recalc_d") is not None)
    n_repro_side = sum(1 for r in rows if r.get("reproduced_value") is not None
                       or r.get("domain_recalc_d") is not None)
    n_nr = sum(1 for r in rows if str(r.get("verdict", "")) in ("NOT REPRODUCED",))
    n_nc = sum(1 for r in rows if str(r.get("verdict", "")) in ("NOT COMPARABLE",))
    n_nch = sum(1 for r in rows if str(r.get("verdict", "")) in ("NOT CHECKABLE",))
    gaps = sum(1 for r in rows if json.loads(r.get("gaps_json") or "[]"))
    woe_rows = [r for r in rows if r.get("woe_d_on") is not None
                or r.get("woe_d_off") is not None]
    woe_full = [r for r in woe_rows if r.get("woe_d_on") is not None
                and r.get("woe_d_off") is not None]
    woe_cells = len(woe_rows)
    woe_prim = (woe_full or woe_rows)[0] if (woe_full or woe_rows) else {}
    prov = dossier.get("exclusion_provenance") or {}
    n_prov_rules = len(dossier.get("exclusion_rules") or []) if prov.get("computed") else 0
    n_paper = sum(1 for r in rows if r.get("original_value") is not None)
    orig_txt = "; ".join(_fmt_pair(r, "original_value", "original_metric")
                         for r in rows[:4] if r.get("original_value") is not None)
    rep_txt = "; ".join(_fmt_pair(r, "reproduced_value", "reproduced_metric")
                        for r in rows[:4] if r.get("reproduced_value") is not None)
    if not rep_txt:
        rep_txt = "; ".join(f"{r.get('dv_domain')}:domain_d={r['domain_recalc_d']:.4g}"
                            for r in rows[:4] if r.get("domain_recalc_d") is not None)
    n_with_off = None
    offs = [r.get("woe_n_treat_off") for r in rows if r.get("woe_n_treat_off") is not None]
    if offs:
        n_with_off = sum(int(x) for x in offs if x == x)
    if woe_cells:
        woe_txt = ("d_on=%s; d_off=%s; delta=%s; n=%s/%s->%s/%s"
                   % (_fnum(woe_prim.get("woe_d_on")), _fnum(woe_prim.get("woe_d_off")),
                      _fnum(woe_prim.get("woe_delta")),
                      _int0(woe_prim.get("woe_n_treat_on")),
                      _int0(woe_prim.get("woe_n_control_on")),
                      _int0(woe_prim.get("woe_n_treat_off")),
                      _int0(woe_prim.get("woe_n_control_off"))))
        if woe_cells > 1:
            woe_txt += f" (primary cell; k={woe_cells})"
    else:
        woe_txt = ((dossier.get("results") or [{}])[0].get("without_exclusions_estimand", "")
                   or "")
    run = dossier.get("run") or {}
    return {
        "study_key": dossier.get("study_key"),
        "generated_at": dossier.get("generated_at"),
        "schema_version": dossier.get("schema_version"),
        "dataset_backed": "yes" if dossier.get("dataset_backed") else "no",
        "has_dataset": "yes" if dossier.get("has_dataset") else "no",
        "report_present": "yes" if report_path else "no",
        "dossier_path": os.path.relpath(path, ROOT).replace("\\", "/") if path else "",
        "reproduced_status": dossier.get("status", ""),
        "machine_status": dossier.get("machine_status", ""),
        "documented_status": dossier.get("documented_status", ""),
        "n_contrasts": len(rows),
        "n_original": n_orig,
        "n_reproduced": n_rep,
        "n_reproduced_numeric": n_rep_numeric,
        "n_domain_recalc_numeric": n_domain_recalc,
        "n_reproduced_side_numeric": n_repro_side,
        "n_participants_reproduced": run.get("n_rows"),
        "n_not_reproduced": n_nr,
        "n_not_comparable": n_nc,
        "n_not_checkable": n_nch,
        "n_gap_rows": gaps,
        "numeric_pair_studies_marker": 1 if (n_orig > 0 and n_repro_side > 0) else 0,
        "n_woe_cells": woe_cells,
        "n_woe_full": len(woe_full),
        "n_without_exclusions": n_with_off,
        "without_exclusions_source": prov.get("source", ""),
        "woe_d_on": woe_prim.get("woe_d_on"),
        "woe_d_off": woe_prim.get("woe_d_off"),
        "woe_delta": woe_prim.get("woe_delta"),
        "woe_n_treat_on": woe_prim.get("woe_n_treat_on"),
        "woe_n_control_on": woe_prim.get("woe_n_control_on"),
        "woe_n_treat_off": woe_prim.get("woe_n_treat_off"),
        "woe_n_control_off": woe_prim.get("woe_n_control_off"),
        "n_exclusion_rules": len(dossier.get("exclusion_rules") or []),
        "n_exclusion_provenance_rules": n_prov_rules,
        "exclusion_provenance_source": prov.get("source", ""),
        "original_results": orig_txt,
        "reproduced_results": rep_txt,
        "results_without_exclusions": woe_txt,
        "preparations": dossier.get("preparations", ""),
        "failures": (dossier.get("failures") or "")[:160],
        "discrepancy_count": dossier.get("discrepancy_count", 0),
        "n_paper_contrasts": n_paper,
        "report_path": report_path or "",
        "dataset_path": dataset_path or "",
        "log_path": run.get("log_path", ""),
        "command": run.get("command", ""),
        "exit_code": run.get("exit_code"),
        "runtime_s": run.get("runtime_s"),
        "notes": "; ".join(dossier.get("study_gaps") or []),
    }


def _fnum(x):
    try:
        return f"{float(x):+.4f}"
    except (TypeError, ValueError):
        return "—"


def _int0(x):
    try:
        return str(int(x))
    except (TypeError, ValueError):
        return "—"


def _fmt_pair(row, valcol, metcol):
    v = row.get(valcol)
    m = row.get(metcol) or ""
    if v is None:
        return ""
    return f"{m}={v:.4g}" if m else f"{v:.4g}"


def strip_internal(row):
    return {k: v for k, v in row.items() if not k.startswith("_")}
