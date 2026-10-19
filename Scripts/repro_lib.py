# -*- coding: utf-8 -*-
"""Shared reproduction-report engine for the misinformation-intervention meta-analysis.

``reproduce(key)`` is the single entry point used by every (new) thin per-study
reproduction script ``Datasets/<Key>/<Key> - reproduction.py``.  It prints a SHORT
four-section text report to stdout (writing **nothing** into ``Datasets/``; all
intermediate work happens in memory and the process temp dir is redirected to the
root ``temp/``) and returns a plain dict with the parsed facts.

The four sections
-----------------
(a) EXCLUSION CRITERIA   -- reads ``Datasets/<Key>/<Key> - exclusions.csv`` and prints
    each ``flag``, whether it gates ALL DVs (*) or a ``dv_*`` subset, and the reason;
(b) ANALYSIS APPLIED     -- from ``<Key> - harmonized_variables.csv``: the resolved
    ``dv_*`` domains / veracities / timepoints present, the exclusion rules applied,
    and the estimator used;
(c) RECALCULATION        -- loads ``<Key> - harmonized.csv``, applies the exclusion
    rules per DV (``fnmatch`` globs / ``domain:`` matches on the uniform DV name,
    exactly as ``reanalysis.py`` does), reverse-codes the 0-1 rescaled values for
    ``dv_polarity = higher_is_worse`` DVs per the sidecar, aggregates
    participant-level cell means per (domain, timepoint, veracity) and reports for
    every domain ``N, mean true, mean false, d = mean(true) - mean(false)``;
(d) REPRODUCED VS NOT    -- compares the recalculated per-domain discrimination (or,
    for single-veracity within-subject contrasts, the veracity column-mean change)
    to the coded eligible-primary contrasts in ``Extraction/<Key>.json`` (and to the
    ``**reproduced:**`` verdict documented in ``Reproduction/<Key>.md`` where
    parseable) and reports which domains/effects reproduced (within ~2% when the coded
    effect is on the same 0-1 mean-difference scale) and which did not.

No-data studies (no ``<Key> - harmonized.csv``) print ``no participant data -
NOT reproducible`` plus the documented reason from ``Reproduction/<Key>.md``.

Estimator note (polarity)
-------------------------
The corpus defines discrimination on the false-endorsement side: ``d = mean(true) -
mean(false)`` where the false group is the FALSE-ALARM side (endorsement of false
material), exactly the quantity the studies call discernment (e.g. mean(true sharing) -
mean(false sharing)).  This is the linear twin of ``reanalysis.py``'s
``d' = z(HR) - z(1 - CR)``: the explicit `1 - x` reverse-coding that pipeline applies
for ``higher_is_worse`` DVs cancels out of that formula (``1 - (1 - xF) == xF``), so the
recalculated ``d`` reads in the desirable direction without a second recode.  Applying
the literal recode *before* subtraction would flip the sign (verified on Arechar et al.
2023: 0.162 -> -0.103) and contradict the studies' own reported discernment.  The
polarity of every DV is still reported from the sidecar so the bookkeeping stays
visible.
"""
import fnmatch
import json
import math
import os
import re
import sys
import tempfile
import warnings

import numpy as np
import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

B = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # project root
DS = os.path.join(B, "Datasets")
EXTRACT = os.path.join(B, "Extraction")
REPRO = os.path.join(B, "Reproduction")
TMP = os.path.join(B, "temp")
os.makedirs(TMP, exist_ok=True)
tempfile.tempdir = TMP

sys.path.insert(0, os.path.join(B, "Scripts"))
try:
    import domain_restriction as DR  # noqa: E402  (shared controlled vocabulary)
except Exception:  # pragma: no cover - fall back to raw labels
    DR = None

_REV_POLARITY = {"higher_is_worse", "lower_is_better", "reverse", "reversed", "negative"}

# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def num(x):
    try:
        if x is None:
            return np.nan
        f = float(x)
        return f if math.isfinite(f) else np.nan
    except (TypeError, ValueError):
        return np.nan


def resolve_domain(raw, uniform_variable):
    if DR is not None:
        return DR.resolve_sidecar_domain(raw, uniform_variable)
    return str(raw or "").strip() or "other"


def is_confidence_name(name):
    s = str(name)
    return bool(re.search(r"(^|[_\-])(confidence|certainty|conf)([_\-]|$)", s, re.I)) \
        and not re.search(r"uncertain", s, re.I)


def keep_mask(s, expr):
    s = pd.to_numeric(s, errors="coerce")
    m = re.match(r"^(==|!=|>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)$", str(expr).strip())
    if m:
        op, v = m.group(1), float(m.group(2))
        mask = {"==": s == v, "!=": s != v, ">=": s >= v, "<=": s <= v,
                ">": s > v, "<": s < v}[op]
        # Exclude only participants whose flag is DEFINED and fails the condition.
        # A missing flag means the check was not administered (e.g. another
        # artifact in the same study), so the row is retained; otherwise a rule
        # defined for one artifact silently wipes out the others.
        return mask | s.isna()
    if str(expr).strip().lower() == "notna":
        return s.notna()
    return pd.Series(True, index=s.index)


def time_bucket(tp):
    """Normalise a sidecar timepoint to pre / post / followup (blank = post).

    Mirrors ``reanalysis.py: time_bucket`` so the recalculated rows line up with the
    pooled-analysis timepoint separation.
    """
    t = "" if tp is None else str(tp).strip().lower()
    if t in ("", "nan", "none", "nat"):
        return "post"
    if "follow" in t or t.startswith("fu") or "wave" in t or "month" in t or "week" in t:
        return "followup"
    if "pre" in t or "baseline" in t or "before" in t or "pretreat" in t:
        return "pre"
    if "post" in t or "immediate" in t or "endline" in t:
        return "post"
    return "other"


def coded_bucket(dv):
    """Map a coded ``timepoint_1`` to the same pre/post/followup vocabulary."""
    return time_bucket(dv.get("timepoint_1"))


def read_md_reason(key):
    """Pull the documented ``**reproduced:**`` verdict and ``**reason:**`` from the md."""
    p = os.path.join(REPRO, key + ".md")
    if not os.path.isfile(p):
        return None, None, "no Reproduction report file"
    try:
        txt = open(p, encoding="utf-8", errors="replace").read()
    except Exception as e:
        return None, None, f"could not read Reproduction report: {e}"
    md = txt
    m = re.search(r"\*\*reproduced:\*\*\s*([^\n*]+)", md, re.I)
    verdict = m.group(1).strip() if m else None
    m = re.search(r"\*\*reason:\*\*", md, re.I)
    reason = None
    if m:
        tail = md[m.end():]
        nxt = re.search(r"\n\s*\*\*", tail)
        seg = tail[:nxt.start()] if nxt else tail[:600]
        seg = re.sub(r"[\r\n]+", " ", seg).strip().strip("|").strip("*").strip()
        reason = seg or None
    if verdict is None:
        # fall back to a sentence mentioning reason / the first content line
        m = re.search(r"(?im)^\s*[-*]?\s*(the coding records[^\n]*|dataset_availability[^\n]*)", md)
        if m:
            reason = m.group(1).strip()
    return verdict, reason, None


def _fmt(x, nd=4):
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "NA"
    return f"{float(x):.{nd}f}"


def blank_or_empty(tp):
    """Sidecar blank/nan timepoint -> '' (rendered as (blank) in reports)."""
    t = str(tp or "").strip()
    if t.lower() in ("nan", "none", "nat"):
        return ""
    return t


# ---------------------------------------------------------------------------
# between-group twin helpers
#
# The coded contrasts split into two quantities that must NEVER be compared to
# each other: a WITHIN-PERSON discernment d = mean(true) - mean(false) (the
# discrimination twin) and a BETWEEN-GROUP intervention vs control effect.  The
# helpers below build the latter from the harmonized participant data by mapping
# the coded ``group1_label``/``group2_label`` onto the ``condition`` column and
# computing Hedges' g (or a 0-1 mean difference) on the matching DV column(s).
# ---------------------------------------------------------------------------

_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "with", "by",
    "vs", "versus", "post", "pre", "test", "rating", "ratings", "score", "scores",
    "composite", "mean", "index", "items", "item", "participant", "measure",
}


def _toks(s):
    """Lower-cased alphanumeric tokens for fuzzy name/condition matching."""
    return {w for w in re.findall(r"[a-z0-9]+", str(s).lower())
            if len(w) > 1 and w not in _STOPWORDS}


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _match_group(cell_group, label):
    """Token-overlap agreement between a ``condition`` value and a coded label."""
    a, b = _toks(cell_group), _toks(label)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def hedges_g(m1, s1, n1, m2, s2, n2):
    """Hedges' g of group1 minus group2 (unoriented, on the coded raw scale)."""
    vals = [num(m1), num(s1), num(n1), num(m2), num(s2), num(n2)]
    if any(not np.isfinite(v) for v in vals):
        return np.nan
    m1, s1, n1, m2, s2, n2 = vals
    if n1 < 2 or n2 < 2 or s1 <= 0 or s2 <= 0:
        return np.nan
    sp = math.sqrt(((n1 - 1) * s1 ** 2 + (n2 - 1) * s2 ** 2) / (n1 + n2 - 2))
    if sp <= 0 or not np.isfinite(sp):
        return np.nan
    return ((m1 - m2) / sp) * (1 - 3 / (4 * (n1 + n2) - 9))


# A between-group twin is computed when the coded contrast is a group1-vs-group2
# comparison.  Besides the primary treatment-vs-control (1) / treatment-vs-active
# (2) contrasts, simple effects (6), factorial main effects (5), dose-response (3),
# misinformation-vs-none (8) and informative-vs-control (9) contrasts also carry
# group1/group2 arms; comparing any of them to the within-person discrimination
# level is the incomparable-quantity defect.  Interactions (10), omnibus terms
# (11), other (7) and non-intervention factors (13) have no group-vs-group twin
# and are reported NOT CHECKABLE rather than compared to the wrong quantity.
_BETWEEN_CONTRAST_TYPES = {1, 2, 3, 5, 6, 8, 9, 12}
_NONCOMPARABLE_CONTRAST_TYPES = {7, 10, 11, 13}
_DISCERNMENT_SCORES = {"discernment_difference", "proportion_difference"}


def is_between_group(dv):
    """True for a coded group1-vs-group2 contrast (``contrast_type`` 1/2)."""
    try:
        ct = int(dv.get("contrast_type"))
    except (TypeError, ValueError):
        return False
    if ct not in _BETWEEN_CONTRAST_TYPES:
        return False
    g1 = str(dv.get("group1_label") or "").strip()
    g2 = str(dv.get("group2_label") or "").strip()
    return bool(g1 and g2)


def is_discernment_composite(dv):
    """True when the coded DV is itself a within-person true-vs-false difference."""
    st = {str(s).strip().lower() for s in (dv.get("score_types") or [])}
    if st & _DISCERNMENT_SCORES:
        return True
    txt = (str(dv.get("discernment_defined_as") or "") + " "
           + str(dv.get("dv_name") or "")).lower()
    return bool(re.search(r"true\s*(?:vs\.?|minus|[-–]|to)\s*false", txt)
                or re.search(r"false\s*(?:vs\.?|minus|[-–]|to)\s*true", txt))


def veracity_filter(dv):
    """Explicit single-veracity filter for a between-group contrast.

    Only ``score_types``/``veracity`` fields count; the DV *name* is NOT used
    (a between-group effect on a fake-news rating is still the whole contrast,
    not a within-person veracity cell).
    """
    st = {str(s).strip().lower() for s in (dv.get("score_types") or [])}
    if "separate_false" in st:
        return "false"
    if "separate_true" in st:
        return "true"
    v = str(dv.get("veracity") or "").strip().lower()
    if v in ("true", "false"):
        return v
    return None


# ---------------------------------------------------------------------------
# exclusion machinery (mirrors reanalysis.py)
# ---------------------------------------------------------------------------


def load_exclusions(folder, key):
    p = os.path.join(folder, key + " - exclusions.csv")
    if not os.path.isfile(p):
        return [], False  # (rules, file_present)
    try:
        d = pd.read_csv(p, encoding="utf-8-sig")
    except Exception:
        return [], False
    if d.empty:
        return [], True
    rules = []
    for _, r in d.iterrows():
        flag = str(r.get("flag", "")).strip()
        keep = str(r.get("keep", "")).strip()
        app = str(r.get("applies_to", "*")).strip() or "*"
        reason = str(r.get("reason", "")).strip()
        if flag and keep and flag.lower() != "nan":
            rules.append({"flag": flag, "keep": keep,
                          "applies_to": [x.strip() for x in app.split(";") if x.strip()],
                          "reason": reason})
    return rules, True


def rule_applies_to_base(base, apps, resid):
    """True when a rule's ``applies_to`` list covers the uniform ``dv_*`` name ``base``."""
    b = str(base)
    for p in apps:
        if p == "*":
            return True
        if p.startswith("domain:"):
            if resid is not None and str(resid) == p.split(":", 1)[1]:
                return True
        elif fnmatch.fnmatch(b, p):
            return True
    return False


def exclusion_summary(rules):
    """Human description of what an applies_to list gates."""
    apps = [p for r in rules for p in r["applies_to"]]
    if not apps:
        return "ALL DVs (*)"
    if any(p == "*" for p in apps):
        return "ALL DVs (*)"
    parts = []
    dom = sorted({p.split(":", 1)[1] for p in apps if p.startswith("domain:")})
    if dom:
        parts.append("domain: " + ", ".join(dom))
    globs = [p for p in apps if p != "*" and not p.startswith("domain:")]
    if globs:
        parts.append("dv subset (" + str(len(globs)) + " globs)")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# recalc table (participant-level, wide-file columns -> no full long melt)
# ---------------------------------------------------------------------------


def _nanmean(vals, axis):
    """nanmean that stays silent about all-NaN slices (participant without that veracity)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmean(vals, axis=axis)


def _num_mat(H, cols):
    """Numeric matrix for a column slice; non-numeric / missing cells become NaN."""
    X = np.empty((len(H), len(cols)), dtype=float)
    for i, c in enumerate(cols):
        s = H[c]
        if s.dtype.kind in "iuf":
            X[:, i] = s.to_numpy(dtype=float)
        else:
            X[:, i] = pd.to_numeric(s, errors="coerce").to_numpy(dtype=float)
    return X


_CTX_CACHE = {}


def get_context(folder, key):
    """Load (and cache) the harmonized data + sidecar context for one study.

    Returns a dict with ``H`` (the wide harmonized frame), ``meta`` (the
    ``base -> (domain, veracity, timepoint, polarity)`` tuple used by the report),
    ``meta_full`` (adds ``original_variable``/``notes`` for column matching),
    ``base2col``, ``use_bases``, ``col_mask``, ``rules``, ``notes``,
    ``n_after`` and ``has_harmonized``.
    """
    ck = (os.path.abspath(folder), key)
    if ck in _CTX_CACHE:
        return _CTX_CACHE[ck]
    harm = os.path.join(folder, key + " - harmonized.csv")
    side = os.path.join(folder, key + " - harmonized_variables.csv")
    notes = []
    meta, meta_full = {}, {}
    has_harmonized = os.path.isfile(harm)
    H = pd.DataFrame()
    rules, base2col, col_mask, use_bases = [], {}, {}, []
    n_after = 0
    if has_harmonized:
        if os.path.isfile(side):
            try:
                sd = pd.read_csv(side, encoding="utf-8-sig", low_memory=False)
                try:
                    import dv_dedup
                    sd, _pruned = dv_dedup.prune_sidecar(sd)
                    if _pruned:
                        notes.append(f"dropped {len(_pruned)} nested sub-item DV(s) from the "
                                     f"true/false split (composite double-weight guard)")
                except Exception:
                    pass
                for _, r in sd.iterrows():
                    uv = str(r.get("uniform_variable", "")).strip()
                    if not uv:
                        continue
                    raw_dom = r.get("dv_domain")
                    if isinstance(raw_dom, float) and math.isnan(raw_dom):
                        raw_dom = ""
                    dom = resolve_domain(raw_dom, uv)
                    ver = str(r.get("veracity", "")).strip()
                    tp = blank_or_empty(str(r.get("timepoint", "")).strip())
                    pol = str(r.get("dv_polarity", "")).strip()
                    meta[uv] = (dom, ver, tp, pol)
                    meta_full[uv] = {"domain": dom, "veracity": ver.lower(),
                                     "timepoint": tp, "polarity": pol.lower(),
                                     "orig": str(r.get("original_variable", "") or ""),
                                     "notes": str(r.get("notes", "") or "")}
            except Exception as e:
                notes.append(f"sidecar read failed ({e}); using name-derived domains")
        try:
            H = pd.read_csv(harm, encoding="utf-8-sig", low_memory=False)
        except Exception as e:
            ctx = dict(H=pd.DataFrame(), meta={}, meta_full={}, base2col={},
                       use_bases=[], col_mask={}, rules=[],
                       notes=[f"harmonized read failed: {e}"], n_after=0,
                       has_harmonized=False)
            _CTX_CACHE[ck] = ctx
            return ctx

        # per-row drop for all-DV rules (author analysis samples); DV-scoped rules are
        # applied per column further down (a failed DV-scoped check drops only that DV).
        # Per-column masks below handle BOTH "*" and "domain:" / dv-subset rules (a "*"
        # rule gates every DV column), mirroring reanalysis.py's apply_exclusions.
        rules, _ = load_exclusions(folder, key)

        dv01 = [c for c in H.columns if str(c).startswith("dv_") and str(c).endswith("_01")]
        base2col = {}
        for c in dv01:
            base = str(c)[:-3]
            if base not in meta:
                # twin whose base is not in the sidecar: derive domain/veracity from name
                meta[base] = (resolve_domain("", base), "", "", "")
            if base not in meta_full:
                meta_full[base] = {"domain": resolve_domain("", base), "veracity": "",
                                   "timepoint": "", "polarity": "", "orig": "", "notes": ""}
            base2col[base] = c
        # swallow item twins ONLY when the domain has a composite (the composite already
        # is the participant mean); when a domain has no composite the item columns are
        # its only data and must be kept (e.g. Rathje et al. 2023 has no composites).
        use_bases = []
        item_kept = []
        for b in base2col:
            if re.search(r"_i\d+$", b):
                if b.rsplit("_i", 1)[0] in base2col:
                    continue  # composite twin exists; item would double-weight
                item_kept.append(b)
            else:
                use_bases.append(b)
        use_bases.extend(item_kept)
        if item_kept:
            notes.append(f"item-level dv_*_01 columns used for {len(item_kept)} bases "
                         f"whose domain has no composite")

        # per-column exclusion masks
        col_mask = {}
        for b in use_bases:
            m = pd.Series(True, index=H.index)
            dom, _, _, _ = meta.get(b, ("other", "", "", ""))
            for r in rules:
                if r["flag"] not in H.columns:
                    continue
                if not rule_applies_to_base(b, r["applies_to"], dom):
                    continue
                m &= keep_mask(H[r["flag"]], r["keep"])
            col_mask[b] = m
        n_after = int(_all_dv_keep_mask(H, rules).sum())
    ctx = dict(H=H, meta=meta, meta_full=meta_full, base2col=base2col,
               use_bases=use_bases, col_mask=col_mask, rules=rules,
               notes=notes, n_after=n_after, has_harmonized=has_harmonized)
    _CTX_CACHE[ck] = ctx
    return ctx


def build_recalc(folder, key):
    """Return (rows, meta, notes, n_after) with per (domain, timepoint) discrimination rows.

    ``rows``: list of dicts {domain, timepoint, bucket, n, n_true, n_false,
    mean_true, mean_false, d, polarity_note}.  ``meta``: {uniform_base: (domain,
    veracity, timepoint, polarity)} for every DV in the sidecar.  ``notes``:
    human-readable caveats (e.g. veracity absent, _01 twins absent).  ``n_after``:
    participants passing every '*'-gated exclusion rule (the author analysis
    sample), for the report header.
    """
    ctx = get_context(folder, key)
    if not ctx["has_harmonized"]:
        return [], ctx["meta"], ctx["notes"], 0
    H = ctx["H"]
    meta = ctx["meta"]
    notes = list(ctx["notes"])
    base2col = ctx["base2col"]
    use_bases = ctx["use_bases"]
    col_mask = ctx["col_mask"]

    # group columns into (domain, veracity, timepoint) cells
    cells = {}
    domains_present = set()
    for b in use_bases:
        dom, ver, tp, pol = meta.get(b, ("other", "", "", ""))
        if dom == "confidence":
            continue  # confidence/certainty is a separate strand, never an outcome
        domains_present.add(dom)
        key_cell = (dom, tp)
        cells.setdefault(key_cell, []).append(b)
    if not domains_present:
        return [], meta, notes + ["no dv_*_01 outcome columns"], 0

    rows = []
    for (dom, tp), bases in sorted(cells.items()):
        cols = [base2col[b] for b in bases]
        X = _num_mat(H, cols)
        for i, b in enumerate(bases):
            X[~col_mask[b].to_numpy(dtype=bool), i] = np.nan
        pm = _nanmean(X, axis=1)
        has = ~np.isnan(pm)
        if not has.any():
            continue
        side = {}
        for b in bases:
            ver = meta.get(b, ("", "", "", ""))[1].lower()
            if ver in ("true", "false"):
                side.setdefault(ver, []).append(b)
        n_true = n_false = None
        mean_true = mean_false = np.nan
        col_true = [base2col[b] for b in side.get("true", [])]
        col_false = [base2col[b] for b in side.get("false", [])]
        if col_true:
            T = _num_mat(H, col_true)
            for i, b in enumerate(side["true"]):
                T[~col_mask[b].to_numpy(dtype=bool), i] = np.nan
            tpmean = _nanmean(T, axis=1)
            m = ~np.isnan(tpmean)
            n_true = int(m.sum())
            mean_true = float(tpmean[m].mean()) if n_true else np.nan
        if col_false:
            F = _num_mat(H, col_false)
            for i, b in enumerate(side["false"]):
                F[~col_mask[b].to_numpy(dtype=bool), i] = np.nan
            fpmean = _nanmean(F, axis=1)
            m = ~np.isnan(fpmean)
            n_false = int(m.sum())
            mean_false = float(fpmean[m].mean()) if n_false else np.nan
        d = mean_true - mean_false
        rows.append({
            "domain": dom, "timepoint": tp, "bucket": time_bucket(tp),
            "n": int(has.sum()), "n_true": n_true, "n_false": n_false,
            "mean_true": mean_true, "mean_false": mean_false, "d": d,
            "polarities": sorted({meta.get(b, ("", "", "", ""))[3]
                                  for b in bases if meta.get(b, ("", "", "", ""))[3]}),
        })
    rows.sort(key=lambda r: (r["domain"], sorted_side(r["timepoint"]), r["bucket"]))
    return rows, meta, notes, ctx["n_after"]


def _all_dv_keep_mask(H, rules):
    """Rows passing every '*'-gated exclusion rule (the author analysis sample)."""
    if not rules:
        return pd.Series(True, index=H.index)
    mask = pd.Series(True, index=H.index)
    for r in rules:
        if r["flag"] not in H.columns:
            continue
        if any(p == "*" for p in r["applies_to"]):
            mask &= keep_mask(H[r["flag"]], r["keep"])
    return mask


def sorted_side(tp):
    order = {"pre": 0, "": 1, "post": 2, "followup": 3}
    return order.get(str(tp).strip().lower(), 9)


# ---------------------------------------------------------------------------
# extraction layer: coded eligible contrasts + per-effect rendering
# ---------------------------------------------------------------------------


def load_extraction(key):
    p = os.path.join(EXTRACT, key + ".json")
    if not os.path.isfile(p):
        return None
    try:
        data = json.load(open(p, encoding="utf-8"))
    except Exception as e:
        return None, f"could not read Extraction/{key}.json: {e}"
    data = data if isinstance(data, list) else [data]
    return data, None


def coded_effect_value(dv):
    """(numeric value, metric label, human description) for one coded DV_Metric."""
    order = [
        ("yi", "yi", "yi"), ("vi_effect", None, None),
        ("d_val", "Cohen's d", "d"), ("g_val", "Hedges' g", "g"),
        ("t_val", "t", "t"), ("r_val", "r", "r"),
        ("F_val", "F", "F (unsigned)"), ("chi2_val", "chi2", "chi2 (unsigned)"),
        ("beta_val", "beta", "beta"), ("m1i", "mean", "m1/m2 means"),
    ]
    for field, label, _ in order:
        if label is None:
            continue  # variance-only placeholder, never an effect value
        v = num(dv.get(field))
        if np.isfinite(v):
            return v, label, field
    return None, None, None


# Textual markers that betray a standardised effect (d/g/z/r, log-odds, beta, ...).
# NB the stems must NOT carry a trailing `\b`: "standardised", "beta_DiD" and
# "logOR" all continue with word characters, so `\bstandardis\b` / `\bbeta\b` /
# `\blog\b` silently failed to match them and let standardised effects through.
# Also catch standardised difference-in-differences wordings that avoid the token
# "standardised": "pooled (pretest) SD", "pre-post r=...", and "d_z".
_STANDARDIZED_MARKER = re.compile(
    r"\b(?:d|g|z|eta)\b"
    r"|\b(?:fisher|hedges|cohen|smd|odds|atanh)\b"
    r"|\blog|\bbeta|standardis|standardiz"
    r"|pooled\s+(?:\w+\s+){0,2}SD"
    r"|pre-?post\s+r\b|\bd_z\b",
    re.I,
)


def _bounded_unit_range(dv):
    """True when the coded DV's own scale spans at most [-1, 1], so a raw mean
    difference can plausibly live on the same 0-1 scale as the recalculation.

    Excludes 1-7, 0-10, -8..8 count scales and other incommensurate ranges; an
    unrecorded scale is treated as NOT same-scale (the conservative choice).
    """
    lo, hi = num(dv.get("scale_min")), num(dv.get("scale_max"))
    if not (np.isfinite(lo) and np.isfinite(hi)):
        return False
    return (hi - lo) <= 2.0


def same_scale_discernment(dv):
    """True when the coded effect is a raw (unstandardised) mean difference of
    true-vs-false scores on the same 0-1 scale as the recalculation, so a ~2%
    numeric check is legitimate.

    Requires (i) a discernment/proportion-difference score type, (ii) a coded
    magnitude <= 1, (iii) a DV scale no wider than [-1, 1] (so a 1-7, 0-10 or
    -8..8 count difference is excluded), and (iv) no standardised-effect marker
    (d/g/z/Fisher/log-odds/beta/...) in the reported size type or conversion.
    Signal-detection score types (d'/auc) are never raw mean differences.
    """
    st = {str(s).strip().lower() for s in (dv.get("score_types") or [])}
    if not (st & {"discernment_difference", "proportion_difference"}):
        return False
    coded, _, _ = coded_effect_value(dv)
    if coded is None or not np.isfinite(num(coded)) or abs(float(coded)) > 1.0:
        return False
    if not _bounded_unit_range(dv):
        return False
    src = " ".join(str(dv.get(k) or "") for k in
                   ("effect_size_type_reported", "conversion_method"))
    if _STANDARDIZED_MARKER.search(src):
        return False
    return True


def coded_veracity(dv):
    """'true' / 'false' when the coded DV names a single headline veracity; None
    for discernment-style outcomes that span both veracities."""
    nm = (str(dv.get("dv_name") or "") + " " +
          str(dv.get("discernment_defined_as") or "")).lower()
    if re.search(r"true\s*(minus|[-–]|to|vs\.?|vs)\s*(false|fake)|discernment", nm):
        return None  # both veracities in one outcome
    if re.search(r"fake|fals", nm):
        return "false"
    if re.search(r"\breal\b|\btrue\b", nm):
        return "true"
    return None


def coded_is_change(dv, within_between):
    """True when the coded contrast is a within-subject change (pre->post / follow-up).

    Within and mixed designs (schema: 2=within, 3=mixed) report pre-post changes;
    a between-subjects row is only treated as a change when its own name says so.
    """
    wb = int(within_between or 0)
    if wb in (2, 3):
        return True
    nm = (str(dv.get("dv_name") or "") + " " +
          str(dv.get("discernment_defined_as") or "")).lower()
    return bool(re.search(r"post\s*-?\s*pre|pre\s*-?\s*post|follow[- ]?up", nm))


def same_scale_col_delta(dv):
    """True when a single-veracity coded change is a raw unstandardised delta on the
    same 0-1 scale as the recalculation (no d/g/z/F/... standardization).

    Requires a magnitude <= 1, a DV scale no wider than [-1, 1] (a 1-7 or 0-100
    change is not on the same scale) and no standardised-effect marker.
    """
    coded, _, _ = coded_effect_value(dv)
    if coded is None or not np.isfinite(num(coded)) or abs(float(coded)) > 1.0:
        return False
    if not _bounded_unit_range(dv):
        return False
    src = " ".join(str(dv.get(k) or "") for k in
                   ("effect_size_type_reported", "conversion_method"))
    if _STANDARDIZED_MARKER.search(src):
        return False
    return True


_BETWEEN_DESC_TOL = 0.40      # mean mismatch (pooled-SD units) tolerated for a match
_BETWEEN_SPECIFIC = 0.25      # name-overlap above which a single column is identified
_BETWEEN_NAME_FLOOR = 0.10    # name-overlap floor for a no-descriptives single column


def map_conditions(H, g1, g2):
    """Map coded ``group1_label``/``group2_label`` onto the ``condition`` values."""
    if "condition" not in H.columns:
        return None, None
    conds = [c for c in H["condition"].dropna().astype(str).unique()]
    if not conds:
        return None, None
    s1 = sorted(((_match_group(c, g1), c) for c in conds), reverse=True)
    s2 = sorted(((_match_group(c, g2), c) for c in conds), reverse=True)
    if s1[0][0] <= 0 or s2[0][0] <= 0:
        return None, None
    best = None
    for a in s1[:3]:
        for b in s2[:3]:
            if a[1] == b[1]:
                continue
            score = a[0] + b[0]
            if best is None or score > best[0]:
                best = (score, a[1], b[1])
    return (best[1], best[2]) if best else (None, None)


def _selected_pm(h, cols, col_mask):
    """Participant-level mean across ``cols`` (raw scale), honouring exclusion masks."""
    if not cols:
        return None
    X = _num_mat(h, list(cols))
    for i, c in enumerate(cols):
        cm = col_mask.get(c)
        if cm is not None:
            X[~cm.loc[h.index].to_numpy(dtype=bool), i] = np.nan
    return _nanmean(X, axis=1)


def _two_group_stats(h, cols, ga, gb, col_mask):
    pm = _selected_pm(h, cols, col_mask)
    if pm is None:
        return None
    a, b = pm[ga.to_numpy()], pm[gb.to_numpy()]
    a, b = a[np.isfinite(a)], b[np.isfinite(b)]
    if len(a) < 2 or len(b) < 2:
        return None
    return (float(a.mean()), float(a.std(ddof=1)), len(a),
            float(b.mean()), float(b.std(ddof=1)), len(b))


def build_between_twin(dv, key, ctx):
    """Between-group Hedges g twin for a coded group1-vs-group2 contrast.

    Maps the coded ``group1_label``/``group2_label`` onto the harmonized
    ``condition`` column and builds the effect from the matching DV column(s):
    domain + (explicit) single-veracity filter + artifact, composites preferred
    over item columns.  A single column is identified when its name clearly
    matches the coded DV; otherwise all same-veracity columns in the artifact are
    averaged (a generic "mean of the falsehood cells" contrast).  Returns
    ``kind='none'`` with a reason when no defensible twin can be built, so a
    mismatched quantity is NEVER reported as NOT REPRODUCED.
    """
    H = ctx.get("H")
    if H is None or H.empty or "condition" not in H.columns:
        return {"kind": "none", "value": None,
                "note": "no condition column / harmonized data for a between-group twin"}
    dom = reset_domain(dv, key)
    mf = ctx.get("meta_full") or {}
    ver = veracity_filter(dv)
    cand = [b for b in ctx["use_bases"]
            if mf.get(b, {}).get("domain") == dom and b in H.columns]
    if ver:
        cand = [b for b in cand if mf.get(b, {}).get("veracity") == ver]
    if not cand:
        return {"kind": "none", "value": None,
                "note": f"no harmonized dv_* column in domain '{dom}'"
                        + (f" with veracity={ver}" if ver else "")}
    # composites preferred: drop item columns when a non-item column is available
    if any(not re.search(r"_i\d+$", b) for b in cand):
        cand = [b for b in cand if not re.search(r"_i\d+$", b)]
    # Match the coded timepoint bucket (blank == immediate posttest) so a
    # follow-up contrast is not reproduced against the posttest column or vice
    # versa; only restrict when the bucket leaves at least one candidate.
    cbucket = coded_bucket(dv)
    tp_ok = [b for b in cand
             if time_bucket(mf.get(b, {}).get("timepoint")) == cbucket]
    if tp_ok:
        cand = tp_ok
    g1 = str(dv.get("group1_label") or "")
    g2 = str(dv.get("group2_label") or "")
    dv_toks = _toks(str(dv.get("dv_name") or ""))
    m1c, m2c = num(dv.get("m1i")), num(dv.get("m2i"))
    s1c, s2c = num(dv.get("sd1i")), num(dv.get("sd2i"))
    n1c, n2c = num(dv.get("n1i")), num(dv.get("n2i"))
    has_desc = bool(np.isfinite(m1c) and np.isfinite(m2c))
    art_col = "artifact" if "artifact" in H.columns else None
    artifacts = sorted(H[art_col].dropna().astype(str).unique()) if art_col else []
    if not artifacts:
        artifacts = [None]
    col_mask = ctx.get("col_mask") or {}
    opts = []
    for art in artifacts:
        h = H if art is None else H[H[art_col].astype(str) == art]
        if not len(h):
            continue
        k1, k2 = map_conditions(h, g1, g2)
        if not k1 or not k2:
            continue
        ga = h["condition"].astype(str) == k1
        gb = h["condition"].astype(str) == k2
        live = [c for c in cand
                if pd.to_numeric(h[c], errors="coerce").notna().any()]
        if not live:
            continue
        byver = {}
        for b in live:
            byver.setdefault(mf.get(b, {}).get("veracity"), []).append(b)
        for _v, cols in byver.items():
            scores = {c: _jaccard(dv_toks, _toks(c) | _toks(mf[c].get("orig"))
                                  | _toks(mf[c].get("notes"))) for c in cols}
            best_name = max(scores.values())
            ordered = sorted(scores.values(), reverse=True)
            second = ordered[1] if len(ordered) > 1 else 0.0
            # A single column is identified when the coded name clearly points at
            # it (high overlap, or a unique best match).  When several columns
            # match equally (a generic "mean of the falsehood cells" contrast),
            # average them instead.
            if (len(cols) == 1 or best_name >= _BETWEEN_SPECIFIC
                    or (best_name > 0 and (second <= 0 or best_name >= 2 * second))):
                identified = [max(scores, key=scores.get)]
                pick_kind = "single"
            else:
                identified = list(cols)
                pick_kind = "aggregate"
            st = _two_group_stats(h, identified, ga, gb, col_mask)
            if st is None:
                continue
            m1, s1, n1, m2, s2, n2 = st
            g = hedges_g(m1, s1, n1, m2, s2, n2)
            if not np.isfinite(num(g)):
                continue
            desc = np.nan
            if has_desc:
                sd_ref = (s1 + s2) / 2 if (np.isfinite(s1) and np.isfinite(s2)) else np.nan
                if np.isfinite(sd_ref) and sd_ref > 0:
                    desc = abs(m1 - m1c) / sd_ref + abs(m2 - m2c) / sd_ref
            nmis = (abs(n1 - n1c) + abs(n2 - n2c)
                    if (np.isfinite(n1c) and np.isfinite(n2c)) else np.nan)
            exact = bool(np.isfinite(m1c) and np.isfinite(m2c) and np.isfinite(s1c)
                         and np.isfinite(s2c)
                         and abs(m1 - m1c) <= 0.015 and abs(m2 - m2c) <= 0.015
                         and abs(s1 - s1c) <= 0.02 and abs(s2 - s2c) <= 0.02)
            opts.append(dict(art=art, k1=k1, k2=k2, cols=identified,
                             kind=pick_kind, m1=m1, s1=s1, n1=n1,
                             m2=m2, s2=s2, n2=n2, g=float(g),
                             desc=desc, nmis=nmis, name=best_name, exact=exact,
                             polarity=str(mf.get(identified[0], {}).get("polarity") or "")))
    if not opts:
        return {"kind": "none", "value": None,
                "note": f"could not map group labels to conditions in domain '{dom}'"}
    if has_desc:
        ranked = sorted(opts, key=lambda o: (
            (0 if o["exact"] else 1),
            o["desc"] if np.isfinite(o["desc"]) else 9.9,
            o["nmis"] if np.isfinite(o["nmis"]) else 9.9))
        best = ranked[0]
        if not best["exact"] and (not np.isfinite(best["desc"]) or best["desc"] > _BETWEEN_DESC_TOL):
            return {"kind": "none", "value": None,
                    "note": "no harmonized between-group construction matches the "
                            "coded group means (mismatched quantity/column)"}
    else:
        rel = [o for o in opts
               if o["name"] >= _BETWEEN_NAME_FLOOR or len(o["cols"]) == 1]
        if not rel:
            return {"kind": "none", "value": None,
                    "note": "no harmonized dv_* column name matches this "
                            "between-group contrast"}
        rel.sort(key=lambda o: (0 if o["kind"] == "single" else 1,
                                -o["name"],
                                o["nmis"] if np.isfinite(o["nmis"]) else 9.9))
        best = rel[0]
    cols_txt = ", ".join(best["cols"][:3]) + (" ..." if len(best["cols"]) > 3 else "")
    note = (f"between-group Hedges g = group1({best['k1']}) - group2({best['k2']}) "
            f"on [{cols_txt}]"
            + (f", artifact={best['art']}" if best["art"] is not None else "")
            + f"; means {_fmt(best['m1'])}/{_fmt(best['m2'])}, "
              f"SD {_fmt(best['s1'])}/{_fmt(best['s2'])}, n {best['n1']}/{best['n2']}")
    return {"kind": "between", "value": float(best["g"]), "g": float(best["g"]),
            "md01": np.nan, "row": None, "between": best, "note": note}


def build_between_discernment_twin(dv, key, ctx):
    """Between-group Hedges g of a participant-level true-vs-false discernment score.

    Used for a group1-vs-group2 contrast whose coded DV is a
    discernment/proportion difference: the participant score is
    ``mean(true _01) - mean(false _01)`` in the coded domain/timepoint/artifact,
    and the effect is the between-group Hedges g of that score.  This is the
    quantity the coded ``yi`` (a between-group effect on discernment) is on; the
    old code compared it instead to the arm-blind pooled discrimination level.
    """
    H = ctx.get("H")
    if H is None or H.empty or "condition" not in H.columns:
        return {"kind": "none", "value": None,
                "note": "no condition column / harmonized data for a between-group twin"}
    dom = reset_domain(dv, key)
    mf = ctx.get("meta_full") or {}
    cand = [b for b in ctx["use_bases"]
            if mf.get(b, {}).get("domain") == dom and b in H.columns]
    if not cand:
        return {"kind": "none", "value": None,
                "note": f"no harmonized dv_* column in domain '{dom}'"}
    if any(not re.search(r"_i\d+$", b) for b in cand):
        cand = [b for b in cand if not re.search(r"_i\d+$", b)]
    cbucket = coded_bucket(dv)
    tp_ok = [b for b in cand
             if time_bucket(mf.get(b, {}).get("timepoint")) == cbucket]
    if tp_ok:
        cand = tp_ok
    tcols = [b for b in cand if mf[b].get("veracity") == "true"]
    fcols = [b for b in cand if mf[b].get("veracity") == "false"]
    if not tcols or not fcols:
        return {"kind": "none", "value": None,
                "note": f"domain '{dom}' has no true/false veracity split for a "
                        f"between-group discernment twin"}
    g1 = str(dv.get("group1_label") or "")
    g2 = str(dv.get("group2_label") or "")
    m1c, m2c = num(dv.get("m1i")), num(dv.get("m2i"))
    s1c, s2c = num(dv.get("sd1i")), num(dv.get("sd2i"))
    n1c, n2c = num(dv.get("n1i")), num(dv.get("n2i"))
    has_desc = bool(np.isfinite(m1c) and np.isfinite(m2c))
    art_col = "artifact" if "artifact" in H.columns else None
    artifacts = sorted(H[art_col].dropna().astype(str).unique()) if art_col else []
    if not artifacts:
        artifacts = [None]
    col_mask = ctx.get("col_mask") or {}
    opts = []
    for art in artifacts:
        h = H if art is None else H[H[art_col].astype(str) == art]
        if not len(h):
            continue
        k1, k2 = map_conditions(h, g1, g2)
        if not k1 or not k2:
            continue
        tl = [c for c in tcols if pd.to_numeric(h[c], errors="coerce").notna().any()]
        fl = [c for c in fcols if pd.to_numeric(h[c], errors="coerce").notna().any()]
        if not tl or not fl:
            continue
        # NB no polarity reversal here: exactly as the discrimination twin, the
        # true-minus-false difference already reads in the desirable direction
        # (the `1 - x` recode cancels out), verified against the studies' own
        # reported discernment.
        tm = _selected_pm(h, [c + "_01" for c in tl if c + "_01" in h.columns], col_mask)
        fm = _selected_pm(h, [c + "_01" for c in fl if c + "_01" in h.columns], col_mask)
        if tm is None or fm is None:
            continue
        score = tm - fm
        ga = h["condition"].astype(str).to_numpy() == k1
        gb = h["condition"].astype(str).to_numpy() == k2
        a = score[ga & np.isfinite(score)]
        b = score[gb & np.isfinite(score)]
        if len(a) < 2 or len(b) < 2:
            continue
        m1, s1, n1 = float(a.mean()), float(a.std(ddof=1)), len(a)
        m2, s2, n2 = float(b.mean()), float(b.std(ddof=1)), len(b)
        g = hedges_g(m1, s1, n1, m2, s2, n2)
        if not np.isfinite(num(g)):
            continue
        desc = np.nan
        if has_desc:
            sd_ref = (s1 + s2) / 2
            if np.isfinite(sd_ref) and sd_ref > 0:
                desc = abs(m1 - m1c) / sd_ref + abs(m2 - m2c) / sd_ref
        nmis = (abs(n1 - n1c) + abs(n2 - n2c)
                if (np.isfinite(n1c) and np.isfinite(n2c)) else np.nan)
        exact = bool(np.isfinite(m1c) and np.isfinite(m2c) and np.isfinite(s1c)
                     and np.isfinite(s2c)
                     and abs(m1 - m1c) <= 0.015 and abs(m2 - m2c) <= 0.015
                     and abs(s1 - s1c) <= 0.02 and abs(s2 - s2c) <= 0.02)
        opts.append(dict(art=art, k1=k1, k2=k2, cols=tl + fl, kind="disc_between",
                         m1=m1, s1=s1, n1=n1, m2=m2, s2=s2, n2=n2, g=float(g),
                         desc=desc, nmis=nmis, name=1.0, exact=exact,
                         confident=(len(tl) == 1 and len(fl) == 1)))
    if not opts:
        return {"kind": "none", "value": None,
                "note": f"could not build a between-group discernment score in "
                        f"domain '{dom}'"}
    if has_desc:
        ranked = sorted(opts, key=lambda o: (
            (0 if o["exact"] else 1),
            o["desc"] if np.isfinite(o["desc"]) else 9.9,
            o["nmis"] if np.isfinite(o["nmis"]) else 9.9))
    else:
        ranked = sorted(opts, key=lambda o: (o["nmis"] if np.isfinite(o["nmis"]) else 9.9))
    best = ranked[0]
    if has_desc and not best["exact"] and (
            not np.isfinite(best["desc"]) or best["desc"] > _BETWEEN_DESC_TOL):
        return {"kind": "none", "value": None,
                "note": "no harmonized between-group discernment construction matches "
                        "the coded group means (mismatched quantity/column)"}
    note = (f"between-group Hedges g of participant discernment "
            f"(mean true-_01 - mean false-_01) = group1({best['k1']}) - group2({best['k2']})"
            + (f", artifact={best['art']}" if best["art"] is not None else "")
            + f"; means {_fmt(best['m1'])}/{_fmt(best['m2'])}, n {best['n1']}/{best['n2']}")
    return {"kind": "between", "value": float(best["g"]), "g": float(best["g"]),
            "md01": np.nan, "row": None, "between": best, "note": note}


def build_twin(dv, exp, key, rows, rec_index, ctx=None):
    """Recalc twin for one coded contrast.

    A between-group coded contrast (contrast_type 1/2, group1 vs group2) gets a
    between-group twin (group means/SDs -> Hedges g), including the case where
    the coded DV is itself a discernment/proportion difference (then the twin is
    the between-group g of the participant true-minus-false score).  A
    within-person discernment/change contrast keeps the discrimination /
    column-delta twin.  Priority for the within-person path: (1) single-veracity
    within-subject change -> the veracity column-mean difference (post/followup
    minus pre) on the 0-1 scale; (2) otherwise the discrimination d = mean(true)
    - mean(false) at the coded timepoint; (3) nothing.
    Returns a dict {kind: 'between'|'col_delta'|'disc'|'none', value, row, note}.
    """
    if ctx is None:
        try:
            ctx = get_context(os.path.join(DS, key), key)
        except Exception:
            ctx = None
    if ctx is not None and is_between_group(dv):
        # NEVER fall back to the arm-blind within-person discrimination level here:
        # the two quantities are not comparable and comparing them was the defect.
        if is_discernment_composite(dv):
            return build_between_discernment_twin(dv, key, ctx)
        return build_between_twin(dv, key, ctx)
    if ctx is not None and not is_discernment_composite(dv):
        try:
            _ct = int(dv.get("contrast_type"))
        except (TypeError, ValueError):
            _ct = None
        if _ct in _NONCOMPARABLE_CONTRAST_TYPES:
            return {"kind": "none", "value": None,
                    "note": f"contrast_type={_ct} has no comparable recalc twin "
                            f"(not a group-vs-group or within-person discernment "
                            f"contrast)"}
    dom = reset_domain(dv, key)
    cbucket = coded_bucket(dv)
    ver = coded_veracity(dv)
    if ver and coded_is_change(dv, exp.get("within_between") if isinstance(exp, dict) else None):
        km = "mean_false" if ver == "false" else "mean_true"
        r = rec_index.get((dom, cbucket)) or rec_index.get((dom, "post"))
        pre = rec_index.get((dom, "pre"))
        if not (r is None or pre is None or
                not np.isfinite(num(r.get(km))) or not np.isfinite(num(pre.get(km)))):
            val = float(num(r.get(km)) - num(pre.get(km)))
            return {"kind": "col_delta", "value": val, "row": r, "veracity": ver,
                    "note": f"twin = {ver}-veracity column mean ({r.get('bucket')}) minus (pre), 0-1 scale"}
        # no pre/post change cell: fall through to the discrimination twin below
    r = pick_rec_row(rows, rec_index, dom, cbucket)
    if r is None:
        return {"kind": "none", "value": None, "note": f"no recalc row for domain {dom}"}
    if not np.isfinite(num(r.get("d"))):
        return {"kind": "none", "value": None,
                "note": "no true/false discrimination recalculated for this domain"}
    return {"kind": "disc", "value": float(num(r.get("d"))), "row": r,
            "note": "discrimination d = mean(true) - mean(false) at the coded timepoint (0-1 scale)"}


def classify_verdict(dv, twin):
    """Return (verdict, note) comparing one coded contrast to its recalc twin."""
    coded, mlabel, _ = coded_effect_value(dv)
    if twin is None or twin.get("kind") == "none":
        return "NOT CHECKABLE", (twin.get("note") if twin else "no recalc twin for this contrast")
    tv = twin.get("value")
    if not np.isfinite(num(tv)):
        return "NOT CHECKABLE", "recalculated twin is not finite"
    if coded is None or not np.isfinite(num(coded)):
        reason = str(dv.get("missing_metrics_reason") or "").strip()
        note = "no numeric effect coded" + (f" ({reason})" if reason else "")
        return "NOT CHECKABLE", note
    if twin.get("kind") == "between":
        return _classify_between(dv, twin)
    twin_txt = (f"Δ={_fmt(tv)}" if twin.get("kind") == "col_delta" else f"d={_fmt(tv)}")
    # Unsigned metrics (F, chi2) carry no direction, and a mean is a level rather
    # than a difference: they can only ever be NOT COMPARABLE -- never same-scale
    # and never sign-checked (an unsigned F is >= 0 and would otherwise spuriously
    # "agree in direction" with any positive recalc d).  NB the labels here must
    # match `coded_effect_value`'s ("F", "chi2", "mean"), not its descriptions.
    if mlabel in ("F", "chi2", "mean"):
        return ("NOT COMPARABLE",
                f"coded effect ({mlabel}={_fmt(coded)}) has no directional meaning vs {twin_txt}")
    scale_ok = same_scale_col_delta(dv) if twin.get("kind") == "col_delta" \
        else same_scale_discernment(dv)
    if scale_ok:
        denom = max(abs(float(coded)), 1e-6)
        rel = abs(float(tv) - float(coded)) / denom
        if rel <= 0.02:
            return ("REPRODUCED",
                    f"same-scale {mlabel}={_fmt(coded)} vs recalc {_fmt(tv)} (rel diff {rel:.2%})")
        return ("NOT REPRODUCED",
                f"same-scale {mlabel}={_fmt(coded)} vs recalc {_fmt(tv)} (rel diff {rel:.2%} > 2%)")
    # A zero recalculated twin has no direction; a sign comparison against a
    # non-zero coded effect would be meaningless (never NOT REPRODUCED).
    if abs(float(tv)) <= 1e-12:
        return ("NOT CHECKABLE",
                f"recalculated twin is zero ({twin_txt}); no direction to compare "
                f"with coded {mlabel}={_fmt(coded)}")
    # different metric: only the direction is comparable
    sign_rec = math.copysign(1.0, float(tv))
    sign_cod = math.copysign(1.0, float(coded))
    if sign_rec == sign_cod:
        return ("reproduced in direction",
                f"recalc {twin_txt} and coded {mlabel}={_fmt(coded)} same sign "
                f"(exact ~2% check N/A: {mlabel} is not a raw "
                + ("0-1 veracity delta" if twin.get("kind") == "col_delta"
                   else "true-minus-false 0-1 difference") + ")")
    return ("NOT REPRODUCED",
            f"recalc {twin_txt} and coded {mlabel}={_fmt(coded)} opposite signs")


def _classify_between(dv, twin):
    """Compare a coded between-group contrast to its between-group recalc twin."""
    coded, mlabel, _ = coded_effect_value(dv)
    b = twin.get("between") or {}
    # Unsigned/level metrics have no directional meaning (as in classify_verdict).
    if mlabel in ("F", "chi2", "mean"):
        return ("NOT COMPARABLE",
                f"coded between-group effect ({mlabel}={_fmt(coded)}) has no "
                f"directional meaning vs between-group g={_fmt(twin.get('g'))}")
    g = num(twin.get("g"))
    if not np.isfinite(g):
        return "NOT CHECKABLE", "between-group twin effect is not finite"
    if abs(float(g)) <= 1e-12 and abs(float(coded)) > 1e-12:
        return ("NOT CHECKABLE",
                f"between-group recalculated effect is zero; no direction to compare "
                f"with coded {mlabel}={_fmt(coded)}")
    unit = "Hedges g"
    val = g
    # The harmonized g is on the raw coded scale (unoriented group1-group2), so it
    # is directly comparable to the coded yi/d/g.  A tiny absolute floor absorbs the
    # rounding of means/SDs that the coded effect was derived from (e.g. 2 dp).
    tol = max(0.02 * abs(float(coded)), 0.005)
    diff = abs(float(val) - float(coded))
    exact_reason = ""
    if b.get("exact"):
        exact_reason = "; harmonized group means/SDs match the coded descriptives"
    if diff <= tol:
        return ("REPRODUCED",
                f"between-group {unit}={_fmt(val)} vs coded {mlabel}={_fmt(coded)} "
                f"(|Δ|={_fmt(diff)} ≤ {_fmt(tol)}){exact_reason}")
    sign_rec = math.copysign(1.0, float(val))
    sign_cod = math.copysign(1.0, float(coded))
    kind = b.get("kind")
    # A raw (unoriented) between-group g on a `higher_is_worse` column has the
    # OPPOSITE sign to a coded effect that was oriented so positive = better
    # (e.g. Spiridonova: inoculation lowers false-headline accuracy, sidecar
    # higher_is_worse, coded yi positive = protective).  Before calling an
    # opposite-sign raw comparison a non-reproduction, check the polarity-flipped
    # value and report it as directional when it agrees.
    if (kind == "single" and sign_rec != sign_cod
            and str(b.get("polarity") or "") in _REV_POLARITY
            and abs(float(coded)) > 1e-12):
        val2 = -float(val)
        if abs(val2 - float(coded)) <= tol:
            return ("REPRODUCED",
                    f"between-group {unit}={_fmt(val)} (polarity-oriented to "
                    f"{_fmt(val2)}) vs coded {mlabel}={_fmt(coded)}")
        return ("reproduced in direction",
                f"between-group {unit}={_fmt(val)} (polarity-oriented to {_fmt(val2)}) "
                f"vs coded {mlabel}={_fmt(coded)}; higher_is_worse column")
    # An averaged-column twin cannot be tied to the specific coded contrast, so a
    # magnitude mismatch is NOT CHECKABLE rather than a NOT REPRODUCED.  A
    # discernment aggregate (boolean true/false over a whole domain) is likewise
    # not attributable on an OPPOSITE-SIGN mismatch: never call a mismatched
    # quantity a non-reproduction.  Same-sign disagreements stay directional.
    if sign_rec == sign_cod:
        if kind == "aggregate" and not b.get("exact"):
            return ("NOT CHECKABLE",
                    f"between-group {unit}={_fmt(val)} vs coded {mlabel}={_fmt(coded)} "
                    f"same sign but magnitude differs for an averaged-column twin")
        return ("reproduced in direction",
                f"between-group {unit}={_fmt(val)} vs coded {mlabel}={_fmt(coded)} "
                f"same sign, magnitude differs (|Δ|={_fmt(diff)})")
    if kind in ("aggregate", "disc_between") and not b.get("exact"):
        return ("NOT CHECKABLE",
                f"between-group {unit}={_fmt(val)} vs coded {mlabel}={_fmt(coded)} "
                f"opposite signs for a domain-level twin (not attributable to the "
                f"coded contrast)")
    return ("NOT REPRODUCED",
            f"between-group {unit}={_fmt(val)} vs coded {mlabel}={_fmt(coded)} "
            f"opposite signs")


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------


def reproduce(key):
    """Print the four-section reproduction report for one study key; return the dict."""
    folder = os.path.join(DS, key)
    out = {"key": key, "status": "error", "has_data": False, "reasons": []}
    if not os.path.isdir(folder):
        _print_header(key)
        print("  ERROR: no Datasets folder for this key\n")
        return out

    md_verdict, md_reason, md_err = read_md_reason(key)

    _print_header(key)

    # ---------------- no participant data ----------------
    harm = os.path.join(folder, key + " - harmonized.csv")
    if not os.path.isfile(harm):
        print("EXCLUSION CRITERIA")
        print("  - no participant data: no `" + key + " - harmonized.csv` present\n")
        print("ANALYSIS APPLIED")
        print("  - no participant data to analyse\n")
        print("RECALCULATION")
        print("  no participant data - NOT reproducible")
        if md_verdict:
            print(f"  documented verdict (Reproduction/{key}.md): {md_verdict}")
        reason = md_reason or md_err
        if md_reason is None and md_err is None:
            reason = "no reason documented in Reproduction/" + key + ".md"
        print(f"  reason: {reason}")
        print("\nREPRODUCED VS NOT")
        print("  not applicable - no participant data to compare against")
        try:
            entries = sorted(os.listdir(folder))
            print("  dataset folder contents: " + (", ".join(entries) or "(empty)"))
        except OSError:
            pass
        out.update({"status": "not_reproducible", "has_data": False,
                    "reason": reason, "documented_verdict": md_verdict})
        _maybe_dump_facts(out)
        print("\n" + "=" * 72)
        return out

    # ---------------- exclusions ----------------
    rules, rules_file = load_exclusions(folder, key)
    print("EXCLUSION CRITERIA")
    if rules_file and not rules:
        print("  - exclusion file is header-only: no author exclusion/attention check gates any DV")
    elif not rules_file:
        print("  - no exclusion file: no author exclusion/attention check applied")
    else:
        print("  flag | gates | keep | reason")
        for r in rules:
            gating = ("ALL DVs" if any(p == "*" for p in r["applies_to"])
                      else (("domain:" + r["applies_to"][0].split(":", 1)[1])
                            if r["applies_to"][0].startswith("domain:")
                            else "dv_* subset (" + str(len(r["applies_to"])) + " globs)"))
            print(f"  {r['flag']} | {gating} | {r['keep']} | {r['reason'] or '-'}")
    excls_txt = "none" if not rules else ", ".join(
        f"{r['flag']} {r['keep']}" + ("" if any(p == "*" for p in r["applies_to"])
                                      else " (dv subset)") for r in rules)
    print()

    # ---------------- analysis applied ----------------
    rows, meta, notes, n_after = build_recalc(folder, key)
    print("ANALYSIS APPLIED")
    dom_ver = {}
    for b, (dom, ver, tp, pol) in meta.items():
        if dom == "confidence" or not str(b).startswith("dv_"):
            continue
        dom_ver.setdefault(dom, {"veracity": set(), "timepoint": set(), "polarity": set()})
        if ver.lower() in ("true", "false"):
            dom_ver[dom]["veracity"].add(ver.lower())
        dom_ver[dom]["timepoint"].add(tp or "blank")
        if pol:
            dom_ver[dom]["polarity"].add(pol)
    if dom_ver:
        print("  DV domains present (sidecar, resolved):")
        for dom in sorted(dom_ver):
            info = dom_ver[dom]
            v = sorted(info["veracity"]) or ["(no veracity)"]
            t = sorted(info["timepoint"])
            p = sorted(info["polarity"])
            print(f"    {dom}: veracity={{{', '.join(v)}}}  timepoint={{{', '.join(t)}}}"
                  + (f"  polarity={{{', '.join(p)}}}" if p else ""))
    else:
        print("  no DV domains found in the sidecar / harmonized file")
    print(f"  exclusions applied: {excls_txt}")
    print("  estimator: discrimination d = mean(true) - mean(false) per domain on the "
          "dv_*_01 (0-1 rescaled) scores;")
    print("             higher_is_worse DVs are applied on the false-alarm side of the "
          "difference (linear twin of reanalysis.py's")
    print("             d' = z(HR) - z(1-CR)); participant-level cell means, one row per "
          "participant per (domain, timepoint, veracity).")
    for n in notes:
        print(f"  NOTE: {n}")
    print()

    # ---------------- recalculation ----------------
    print("RECALCULATION")
    disc_rows = [r for r in rows if r["n_true"] and r["n_false"]
                 and np.isfinite(num(r["d"]))]
    if not disc_rows:
        print("  no true/false discrimination computable from the harmonized data "
              "(veracity split absent)")
    else:
        print(f"  rows analysed: {n_after} participants after all-DV exclusion rules")
        hdr = f"  {'domain':<26} {'tp':<10} {'n_true':>7} {'mean_true':>10} " \
              f"{'n_false':>7} {'mean_false':>10} {'d':>9}"
        print(hdr)
        print("  " + "-" * (len(hdr) - 1))
        for r in rows:
            if not (r["n_true"] and r["n_false"]):
                continue
            line = (f"  {r['domain']:<26} {(r['timepoint'] or '(blank)')[:10]:<10} "
                    f"{r['n_true']:>7} {_fmt(r['mean_true']):>10} "
                    f"{r['n_false']:>7} {_fmt(r['mean_false']):>10} {_fmt(r['d']):>9}")
            print(line)
    print()

    # ---------------- reproduced vs not ----------------
    print("REPRODUCED VS NOT")
    if md_verdict:
        print(f"  documented verdict (Reproduction/{key}.md): {md_verdict}"
              + (f" - {md_reason[:300]}" if md_reason else ""))
    elif md_err:
        print(f"  documented verdict: unavailable ({md_err})")

    ex_ok = load_extraction(key)
    if isinstance(ex_ok, tuple) and ex_ok[1]:
        print("  " + ex_ok[1])
        contrasts = []
        gen_note = f"(Extraction/{key}.json unreadable)"
    else:
        data = ex_ok if isinstance(ex_ok, list) else (ex_ok[0] if ex_ok else [])
        contrasts = []
        for exp in data or []:
            if not isinstance(exp, dict):
                continue
            for dv in exp.get("DV_Metrics") or []:
                if not isinstance(dv, dict):
                    continue
                if int(num(dv.get("eligible_primary")) or 0) == 1:
                    dom = reset_domain(dv, key)
                    contrasts.append((dom, dv, exp.get("Study_ID", "")))
        gen_note = f"{len(contrasts)} eligible-primary contrast(s) coded in Extraction/{key}.json"
    print(f"  coded contrasts: {gen_note}")

    rec_index = {(r["domain"], r["bucket"]): r for r in rows}

    reproduced, direction, not_reproduced, not_checkable, not_comparable = [], [], [], [], []
    seen = set()
    ctx = get_context(folder, key)
    if contrasts:
        print("  domain | coded effect | recalc twin | verdict")
        for dom, dv, sid in contrasts:
            dvname = str(dv.get("dv_name") or "?").strip()
            # De-duplicate only IDENTICAL coded contrasts (same arms/timepoint):
            # keying on the DV name alone silently collapsed the two intervention
            # arms of a multi-arm experiment into one check.
            ckey = (sid, dom, dvname, str(dv.get("comparison_label") or ""),
                    str(dv.get("group1_label") or ""), str(dv.get("group2_label") or ""),
                    str(dv.get("timepoint_1") or ""), str(dv.get("timepoint_2") or ""),
                    str(dv.get("contrast_type") or ""))
            if ckey in seen:
                continue
            seen.add(ckey)
            twin = build_twin(dv, _exp_for(data, sid), key, rows, rec_index, ctx)
            verdict, note = classify_verdict(dv, twin)
            coded, mlabel, _ = coded_effect_value(dv)
            coded_txt = (f"{mlabel if mlabel else 'effect'}="
                         + (_fmt(coded) if np.isfinite(num(coded)) else "NA")) if coded is not None \
                else "no numeric effect"
            tval = twin.get("value") if twin else None
            d_txt = (_fmt(tval) if tval is not None and np.isfinite(num(tval)) else "NA")
            print(f"  {dom:<26} {coded_txt:<32} {d_txt:>9} | {verdict}")
            print(f"      contrast: {dvname[:110]}" + ("..." if len(dvname) > 110 else ""))
            print(f"      note: {note}")
            (reproduced if verdict == "REPRODUCED" else
             direction if verdict == "reproduced in direction" else
             not_reproduced if verdict.startswith("NOT REPRODUCED") else
             not_comparable if verdict == "NOT COMPARABLE" else
             not_checkable).append((dom, dvname))
    else:
        print("  no eligible-primary contrasts coded")

    print("\n  summary:")
    print(f"    REPRODUCED (within ~2%, same-scale): "
          + (", ".join(f"{d}: {n}" for d, n in reproduced) if reproduced else "none"))
    print(f"    reproduced in direction (scale-mismatched coded metric): "
          + (", ".join(f"{d}: {n}" for d, n in direction) if direction else "none"))
    print(f"    NOT REPRODUCED "
          + (", ".join(f"{d}: {n}" for d, n in not_reproduced) if not_reproduced else "none"))
    print(f"    NOT COMPARABLE (unsigned coded metric, no sign to compare): "
          + (", ".join(f"{d}: {n}" for d, n in not_comparable) if not_comparable else "none"))
    print(f"    NOT CHECKABLE "
          + (", ".join(f"{d}: {n}" for d, n in not_checkable) if not_checkable else "none"))

    status = ("reproduced" if reproduced and not not_reproduced
              else "direction" if direction and not not_reproduced
              else "not_reproduced" if not_reproduced else "reported_only")
    out.update({
        "status": status, "has_data": True, "n_rows": int(rows[0]["n"]) if rows else 0,
        "domains": sorted({r["domain"] for r in rows}),
        "recalc": rows, "reproduced": [list(x) for x in reproduced],
        "direction": [list(x) for x in direction],
        "not_reproduced": [list(x) for x in not_reproduced],
        "not_checkable": [list(x) for x in not_checkable],
        "not_comparable": [list(x) for x in not_comparable],
        "documented_verdict": md_verdict,
    })
    _maybe_dump_facts(out)
    print("\n" + "=" * 72)
    return out


def _maybe_dump_facts(out):
    """Durability hook (D10): persist the returned fact dict when the runner asks.

    The per-study thin wrappers call ``reproduce(KEY)`` and discard the return value,
    so ``run_reproducers.py`` sets ``REPRO_FACTS_OUT`` to the target path and the
    engine writes the exact dict it returns.  No-op when the env var is unset.
    """
    dest = os.environ.get("REPRO_FACTS_OUT")
    if not dest:
        return
    try:
        tmp = dest + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1, default=str)
        os.replace(tmp, dest)
    except Exception as e:  # never let a durability write break the reproduction
        print(f"  WARNING: could not write REPRO_FACTS_OUT ({type(e).__name__}: {e})")


def reset_domain(dv, key):
    """Resolve a coded DV_Metrics domain via the shared rule (merge/off-vocab)."""
    if DR is not None:
        return DR.resolve_domain(dv.get("dv_domain"), study_key=key,
                                 dv_name=str(dv.get("dv_name") or ""))
    raw = dv.get("dv_domain") or []
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    return str(raw or "other")


def _rec_flat(rows):
    out = {}
    for r in rows:
        out.setdefault(r["domain"], []).append(r)
    return out


def _exp_for(data, sid):
    for e in data or []:
        if isinstance(e, dict) and e.get("Study_ID") == sid:
            return e
    return None


def pick_rec_row(rows, rec_index, dom, cbucket):
    """Best recalculated (domain, timepoint) row for a coded contrast.

    Exact bucket match first; otherwise prefer the immediate-"post" row for the
    domain (the coded contrasts are overwhelmingly immediate posttests), then the
    first row recorded for the domain.
    """
    r = rec_index.get((dom, cbucket))
    if r is not None:
        return r
    for b in ("post", "followup", "pre", "other"):
        r = rec_index.get((dom, b))
        if r is not None:
            return r
    flat = _rec_flat(rows)
    cand = flat.get(dom)
    return cand[0] if cand else None


def _print_header(key):
    print("=" * 72)
    print("REPRODUCTION REPORT - " + key)
    print("=" * 72)


if __name__ == "__main__":
    _key = sys.argv[1] if len(sys.argv) > 1 else None
    if _key:
        reproduce(_key)
    else:
        print("usage: python Scripts/repro_lib.py '<Study Key>'")