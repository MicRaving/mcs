"""APA / PRISMA routes: the PRISMA figure and the APA Methods and Results document."""

from datetime import datetime
from flask import Blueprint, abort, jsonify, request, send_file
import apa_export as _APA
import collections
import glob
import json
import math
import os
import re
from agreement_lib import _LABEL_MAX_LEVELS, _agreement_index, _field_case_map, gwet_ac
from analysis_lib import SUSCEPTIBILITY, _APA_LAYER, _APA_SPEC, _apa_domain, _apa_metric, _apa_spec, _bayes_for, _bayes_rows, _figure_index, _figures_dir, _human_mod_label, _level_cap, _loo_summary, _mod_label, _moderator_catalog, _moderator_single_figure, _pooled_for, _pool_rows, _sel_rows, _spec_rows_for, _study_moderators
from statslib import _bayes_meta, _bayes_pool, _dl_pool, _mat_inv, _resolve_bayes_priors, _study_level, _t_ppf, _t_sf, _two_level_components, _wls_cluster
from studio_lib import _norm, _values, load_extraction
from studio_state import S, paper_keys

# ONE PRISMA counter (interface §4 / audit H6): the app must not re-count.
import sys as _sys
if S.ROOT and os.path.join(S.ROOT, "Scripts") not in _sys.path:
    _sys.path.insert(0, os.path.join(S.ROOT, "Scripts"))
try:
    import prisma as _PRISMA
except Exception:  # pragma: no cover - source layout fallback
    for _d in (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
               os.path.join(S.ROOT, "Scripts")):
        if _d and _d not in _sys.path:
            _sys.path.insert(0, _d)
    import prisma as _PRISMA  # noqa: E402

bp = Blueprint("apa", __name__)


# Review-criterion labels come from the canonical counter, NOT a local copy:
# the old hand-written set ("Unrelated topic", "No control group", …) drifted from
# the master-dataset vocabulary and shipped outdated exclusion types in the figure.
try:
    import prisma_report as _PR_REPORT
    _APA_CRIT = dict(getattr(_PR_REPORT, "CRITERION_REASON", {}))
    _APA_DUP = getattr(_PR_REPORT, "DUP_CRITERION",
                       "Duplicate/non-independent report of an included study")
except Exception:  # pragma: no cover - prisma_report is always present in-repo
    _APA_CRIT = {
        "C1": "No misinformation intervention (C1)",
        "C2": "No control/comparison group (C2)",
        "C3": "Not English (C3)",
        "C4": "Not a primary experimental study (C4)",
    }
    _APA_DUP = "Duplicate/non-independent report of an included study"
if not _APA_CRIT:
    _APA_CRIT = {"C1": "No misinformation intervention (C1)",
                 "C2": "No control/comparison group (C2)",
                 "C3": "Not English (C3)",
                 "C4": "Not a primary experimental study (C4)"}

def _ap_num(v, d=2):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "–"
    if not math.isfinite(f):
        return "–"
    return f"{f:.{d}f}"

def _ap_p(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "–"
    if not math.isfinite(f):
        return "–"
    if f < 0.001:
        return "< .001"
    return f"{f:.3f}".lstrip("0")

def _ap_ci(lo, hi, d=2):
    return f"[{_ap_num(lo, d)}, {_ap_num(hi, d)}]"

_APA_PRISMA_CACHE = {"mtime": None, "data": None}

def _short_ft_reason(reason):
    """Shorten the long master-workbook full-text criterion labels for the diagram."""
    r = str(reason or "").strip()
    r = r.replace(" (or their advertising)", "")
    r = r.replace(" rather than misinformation susceptibility", "")
    r = r.replace("Intervention/outcome concerns a non-information behaviour or a "
                  "non-misinformation experiment",
                  "Non-information behaviour / non-misinformation experiment")
    r = r.replace("Outcome is eyewitness/memory performance, not misinformation susceptibility",
                  "Outcome is eyewitness/memory performance")
    r = r.replace("Stored full text is a different document (source mismatch); no valid "
                  "primary study data", "Stored full text is a different document")
    return r or "(unspecified)"

# Full-text exclusion categories are the master dataset's REAL review reasons
# (shortened for the diagram), collected dynamically per run — not a collapsed,
# hard-coded bucket list ("Unrelated topic", "No control group", …) that dropped
# the specific reasons (substance-use/body-image/sexual-health/advertising topics,
# eyewitness outcomes, no eligible contrast, …).
def _prisma_ft_group(reason):
    """Map a master-workbook full-text criterion to its diagram label."""
    return _short_ft_reason(reason)

def _crit_label(reason):
    """Map a canonical abstract-exclusion reason to the review criterion label."""
    s = str(reason or "").lower()
    for tok, crit in (("c1", "C1"), ("c2", "C2"), ("c3", "C3"), ("c4", "C4")):
        if tok in s:
            return _APA_CRIT[crit]
    if "no misinformation" in s or "unrelated" in s:
        return _APA_CRIT["C1"]
    if "control" in s or "comparison" in s:
        return _APA_CRIT["C2"]
    if "english" in s:
        return _APA_CRIT["C3"]
    if "primary" in s or "experimental" in s:
        return _APA_CRIT["C4"]
    return _short_ft_reason(reason)

def _prisma_counts():
    """PRISMA 2020 flow counts — canonical counter + presentation split.

    The **totals** come from ``Scripts/prisma.py`` (a facade over
    ``prisma_report.py``), so the app no longer maintains a second, divergent
    count (audit H6 / interface §4).  ``prisma.py`` exposes no database/other
    identification-track split, so the SAME canonical report set is re-bucketed
    by ``Hit_Source`` here purely for the two-column diagram; the two tracks are
    reconciled to the canonical totals before returning.

    Returns the flowing structure consumed by ``apa_export.prisma_png`` and the
    APA Methods text (``total``, ``abstract_excluded``, ``fulltext_assessed``,
    ``fulltext_excluded``, ``coded``, ``tracks``, ...).
    """
    path = os.path.join(S.ROOT, "master_dataset.xlsx")
    if not os.path.isfile(path):
        return None
    try:
        mtime = int(os.path.getmtime(path))
    except OSError:
        mtime = 0
    if _APA_PRISMA_CACHE["data"] is not None and _APA_PRISMA_CACHE["mtime"] == mtime:
        return _APA_PRISMA_CACHE["data"]
    try:
        flow = _PRISMA.flow()
    except Exception:
        return None
    if not flow:
        return None

    # ---- canonical totals (ONE counter) -------------------------------------
    total = int(flow.get("identified_records") or 0)
    duplicates = int(flow.get("duplicates_removed") or 0)
    reports = int(flow.get("reports") or 0)
    abstract_excluded = int(flow.get("abstract_excluded") or 0)
    fulltext_excluded = int(flow.get("fulltext_excluded") or 0)
    reports_included = int(flow.get("included_studies") or 0)
    fulltext_assessed = int(flow.get("fulltext_assessed") or 0)
    sought = int(flow.get("sought_for_retrieval") or 0)
    not_retrieved = int(flow.get("not_retrieved") or 0)
    extraction = flow.get("extraction") or {}
    coded = int(extraction.get("distinct_coded_papers") or 0)
    dup_reports = int(flow.get("duplicate_included_records") or 0)

    abstract_reasons = {_APA_CRIT[c]: 0 for c in ("C1", "C2", "C3", "C4")}
    for reason, n in (flow.get("abstract_excluded_by_reason") or {}).items():
        lab = _crit_label(reason)
        abstract_reasons[lab] = abstract_reasons.get(lab, 0) + int(n)
    fulltext_reasons = collections.Counter()
    for reason, n in (flow.get("fulltext_excluded_by_reason") or {}).items():
        fulltext_reasons[_short_ft_reason(reason)] += int(n)
    # The diagram's full-text exclusion categories are these canonical reasons
    # (ordered by frequency) — never a collapsed/outdated bucket list.
    ft_labels = [lab for lab, _n in fulltext_reasons.most_common()]

    # ---- presentational track split over the SAME canonical reports ---------
    db = {"identified": 0, "abstract_excluded": 0, "abstract_reasons": collections.Counter(),
          "include": 0, "exclude": 0, "ft_groups": collections.Counter()}
    other = {"identified": 0, "abstract_excluded": 0, "abstract_reasons": collections.Counter(),
             "include": 0, "exclude": 0, "ft_groups": collections.Counter()}
    try:
        import prisma_report as _PR
        df = _PR.load_master(path)
        roots = _PR.report_ids(df)
        stages = _PR.report_stages(df, roots)
        members = collections.defaultdict(list)
        for i, r in enumerate(roots):
            members[r].append(i)
        for root, stage in stages.items():
            idxs = members[root]
            is_db = any(str(df.at[i, "Hit_Source"]).strip() == "ebsco_export" for i in idxs)
            T = db if is_db else other
            T["identified"] += 1
            if stage == "Include":
                T["include"] += 1
            elif stage == "Exclude":
                T["exclude"] += 1
                T["ft_groups"][_prisma_ft_group(_PR._reason_for_fulltext(df, idxs))] += 1
            elif stage == "Abstract excluded":
                T["abstract_excluded"] += 1
                T["abstract_reasons"][_crit_label(_PR._reason_for_abstract(df, idxs))] += 1
            else:
                T["ft_groups"]["Fulltext irretrievable"] += 1
    except Exception:
        # If the split cannot be computed, put every report on the database track
        # (the totals below then match by construction).
        db["identified"], db["abstract_excluded"], db["include"], db["exclude"] = (
            reports, abstract_excluded, reports_included, fulltext_excluded)

    # Reconcile the presentational split with the canonical totals: any residual
    # (rows without a resolvable Hit_Source) is assigned to the database track, so
    # the diagram's boxes never contradict prisma.py.
    for key, want in (("identified", reports), ("abstract_excluded", abstract_excluded),
                      ("include", reports_included), ("exclude", fulltext_excluded)):
        got = db[key] + other[key]
        if got != want:
            db[key] += want - got

    db_coded = min(coded, db["include"])
    other_coded = max(0, coded - db_coded)
    # include every label actually seen in either track (canonical order, then extras)
    for _T in (db, other):
        for _lab in _T["ft_groups"]:
            if _lab not in ft_labels:
                ft_labels.append(_lab)
    db_excl = sum(db["ft_groups"].get(g, 0) for g in ft_labels)
    other_excl = sum(other["ft_groups"].get(g, 0) for g in ft_labels)

    def _group_line(T, dup):
        lines = {g: T["ft_groups"].get(g, 0) for g in ft_labels}
        if dup:
            lines[_APA_DUP] = dup
        return lines

    db_groups = _group_line(db, max(0, db["include"] - db_coded))
    other_groups = _group_line(other, max(0, other["include"] - other_coded))
    tracks = {
        "database": {
            "identified": db["identified"], "screened": db["identified"],
            "abstract_excluded": db["abstract_excluded"],
            "abstract_reasons": {_APA_CRIT[c]: db["abstract_reasons"].get(_APA_CRIT[c], 0)
                                 for c in ("C1", "C2", "C3", "C4")
                                 if db["abstract_reasons"].get(_APA_CRIT[c])},
            "sought": max(0, db["identified"] - db["abstract_excluded"]),
            "assessed": db["include"] + db["exclude"],
            "excluded_groups": db_groups, "excluded_total": db_excl,
            "included_reports": db["include"], "coded": db_coded,
            "duplicates": max(0, db["include"] - db_coded),
        },
        "other": {
            "identified": other["identified"],
            "sources": ({"Other sources": other["identified"]} if other["identified"] else {}),
            "sought": max(0, other["identified"] - other["abstract_excluded"]),
            "assessed": other["include"] + other["exclude"],
            "excluded_groups": other_groups, "excluded_total": other_excl,
            "included_reports": other["include"], "coded": other_coded,
            "duplicates": max(0, other["include"] - other_coded),
        },
    }
    data = {
        "total": total, "duplicates": duplicates, "screened": reports,
        "sources": {k: v for k, v in (("Database searches", db["identified"]),
                                      ("Other sources", other["identified"])) if v},
        "abstract_excluded": abstract_excluded,
        "abstract_reasons": {k: v for k, v in abstract_reasons.items() if v},
        "sought": sought, "not_retrieved": not_retrieved,
        "fulltext_assessed": fulltext_assessed, "fulltext_excluded": fulltext_excluded,
        "fulltext_reasons": dict(fulltext_reasons.most_common()),
        "reports_included": reports_included, "duplicate_reports": dup_reports,
        "included": coded, "coded": coded,
        "tracks": tracks, "ft_groups": ft_labels,
        "canonical": {"identified_records": total, "reports": reports,
                      "abstract_excluded": abstract_excluded,
                      "fulltext_excluded": fulltext_excluded,
                      "included_studies": reports_included,
                      "distinct_coded_papers": coded},
    }
    _APA_PRISMA_CACHE.update(mtime=mtime, data=data)
    return data

def _prisma_figure():
    counts = _prisma_counts() or {}
    out = os.path.join(S.TEMP, "apa_prisma.png")
    try:
        _APA.prisma_png(counts, out)
        return out
    except Exception:
        return None

def _excl_types_txt(counts, top=8):
    """The actual full-text exclusion reasons (canonical, most frequent first)."""
    r = counts.get("fulltext_reasons") or {}
    if not r:
        return "the named review criteria"
    items = list(r.items())[:top]
    txt = "; ".join(f"{k} (n = {v:,})" for k, v in items)
    extra = len(r) - len(items)
    if extra > 0:
        txt += f"; {extra} further reason(s)"
    return txt


def _apa_descriptive_tables(keys, records):
    tables = []

    def dist(field, multi=False):
        c = collections.Counter()
        for r in records:
            for v in _values(r.get(field)):
                if v not in (None, ""):
                    c[_mod_label(field, v)] += 1
        return c

    if "sample" in keys:
        n_studies = len({r.get("_key") for r in records})
        ns = []
        for r in records:
            try:
                ns.append(float(r.get("N_Analyzed")))
            except (TypeError, ValueError):
                pass
        total_n = int(sum(ns)) if ns else None
        rows = [{"Characteristic": "Studies (k)", "Value": f"{n_studies:,}"},
                {"Characteristic": "Experiments", "Value": f"{len(records):,}"},
                {"Characteristic": "Participants (sum of analysed N)", "Value": f"{total_n:,}" if total_n else "–"}]
        tables.append({"title": "Sample sizes", "columns": ["Characteristic", "Value"], "rows": rows})
    if "age" in keys:
        c = dist("Age_Group")
        tables.append({"title": "Age group",
                       "columns": ["Age group", "k (experiments)"],
                       "rows": [{"Age group": k, "k (experiments)": v} for k, v in c.most_common()]})
    if "setting" in keys:
        c = dist("Setting")
        tables.append({"title": "Setting",
                       "columns": ["Setting", "k (experiments)"],
                       "rows": [{"Setting": k, "k (experiments)": v} for k, v in c.most_common()]})
    if "design" in keys:
        c = dist("within_between")
        tables.append({"title": "Design",
                       "columns": ["Design", "k (experiments)"],
                       "rows": [{"Design": k, "k (experiments)": v} for k, v in c.most_common()]})
    if "control" in keys:
        c = dist("control_group_type")
        tables.append({"title": "Control group type",
                       "columns": ["Control type", "k (experiments)"],
                       "rows": [{"Control type": k, "k (experiments)": v} for k, v in c.most_common()]})
    if "intervention" in keys:
        c = collections.Counter()
        for r in records:
            for v in set(_values(r.get("Intervention_Type"))):
                if v not in (None, ""):
                    c[_human_mod_label(v)] += 1
        tables.append({"title": "Intervention type (multi-label membership)",
                       "columns": ["Intervention type", "k (experiments)"],
                       "rows": [{"Intervention type": k, "k (experiments)": v}
                                for k, v in c.most_common()]})
    if "domains" in keys:
        c = collections.Counter()
        for r in records:
            for d in r.get("DV_Metrics") or []:
                for dom in _values(d.get("dv_domain")):
                    c[_apa_domain(dom)] += 1
        tables.append({"title": "Outcome domains (contrasts)",
                       "columns": ["Outcome domain", "k (contrasts)"],
                       "rows": [{"Outcome domain": k, "k (contrasts)": v}
                                for k, v in c.most_common()]})
    if "year" in keys:
        ys = []
        for r in records:
            try:
                ys.append(int(float(r.get("Year"))))
            except (TypeError, ValueError):
                pass
        if ys:
            ys.sort()
            med = ys[len(ys) // 2]
            tables.append({"title": "Publication year",
                           "columns": ["Statistic", "Value"],
                           "rows": [{"Statistic": "Median", "Value": med},
                                    {"Statistic": "Range", "Value": f"{ys[0]}–{ys[-1]}"},
                                    {"Statistic": "k (with year)", "Value": len(ys)}]})
    if "wereid" in keys:
        c = dist("WEIRD_Western")
        tables.append({"title": "Western sample",
                       "columns": ["WEIRD (Western)", "k (experiments)"],
                       "rows": [{"WEIRD (Western)": k, "k (experiments)": v} for k, v in c.most_common()]})
    return tables

def _apa_records():
    out = []
    for key in paper_keys():
        for r in (load_extraction(key) or []):
            if isinstance(r, dict):
                out.append(dict(r, _key=key))
    return out

def _apa_freq_models(layer, spec, exclusions, scope):
    # Rows are selected through the ONE shared selector (``_spec_rows_for`` ->
    # ``specs_api.select_spec_rows``), and every model below is fitted on that
    # same row set, so ``k`` and the estimate can never come from different sets
    # (audit C2).  The dependence-aware pool (3-level + RVE) is reported alongside
    # (audit H1).
    rows = _spec_rows_for(layer, spec, exclusions)
    if scope == "restricted":
        rows = [r for r in rows if r["dv_domain"] in SUSCEPTIBILITY]
    rows = _bayes_rows(rows)
    if not rows:
        return None
    y = [r["effect"] for r in rows]
    v = [r["var"] for r in rows]
    groups = [r["study_key"] for r in rows]
    dep = _pool_rows(rows, layer)
    dl = {"est": dep.get("dl_est"), "lo": dep.get("dl_lo"), "hi": dep.get("dl_hi"),
          "k": dep.get("dl_k"), "se": dep.get("dl_se")}
    comp = _two_level_components(y, v, groups)
    # Two-level pooled estimate: stage-1 combines effects within each study, then
    # a DL random-effects model across the study-level estimates.
    st_y, st_v, _stX, _ids = _study_level(y, [[1.0] for _ in y], v, groups)
    two = _dl_pool(st_y, st_v) if st_y else None
    # cluster-robust intercept-only model
    fit = _wls_cluster(y, [[1.0] for _ in y], [1.0 / vi for vi in v], groups)
    clus_est = fit["beta"][0] if fit else float("nan")
    clus_se = fit["se"][0] if fit else float("nan")
    return {
        "k": len(rows), "n_studies": len(set(groups)),
        "dl": dl,
        "dep": dep,
        "two": {"est": (two["est"] if two else math.nan),
                "lo": (two["lo"] if two else math.nan),
                "hi": (two["hi"] if two else math.nan),
                "tau2_between": comp["tau2_between"],
                "tau2_within": comp["tau2_within"], "i2_between": comp["i2_between"],
                "i2_within": comp["i2_within"], "vbar": comp["vbar"],
                "i2_sampling": comp["i2_sampling"],
                "i2_within_alloc": comp["i2_within_alloc"],
                "i2_between_alloc": comp["i2_between_alloc"]},
        "cluster": {"est": clus_est, "se": clus_se,
                    "lo": clus_est - 1.96 * clus_se if fit else math.nan,
                    "hi": clus_est + 1.96 * clus_se if fit else math.nan},
        "tau2": comp["tau2_between"] + comp["tau2_within"],
        "i2": max(comp["i2_between"], comp["i2_within"]),
        "estimand": "intervention-vs-control" if layer == "sdt" else "reported effect",
    }

def _apa_fit_design(y, v, groups, X, terms, model, priors, k_by_term=None):
    """Two-level fit (frequentist or Bayesian) for a prepared design matrix.

    Also returns R² among τ (proportional reduction in the method-of-moments
    residual between-study variance relative to the intercept-only model, on the
    same weights) and ``k`` per term when supplied.
    """
    mu_sd, tau_prior, tau_sd = _resolve_bayes_priors(priors or {})
    comp = _two_level_components(y, v, groups)
    if comp["k"] < len(terms) + 2:
        return None
    k_by_term = k_by_term or {}
    st_y, st_v, st_X, st_ids = _study_level(y, X, v, groups)
    tau2_null = comp["tau2_between"]
    # R² among τ from a method-of-moments residual fit (same weights as the null).
    w_re = [1.0 / (vi + tau2_null) for vi in st_v]
    fit_re = _wls_cluster(st_y, st_X, w_re, st_ids)
    r2_tau = None
    if fit_re:
        sw = sum(w_re)
        c = sw - sum(x * x for x in w_re) / sw if sw > 0 else 0.0
        if c > 0:
            q_res = sum(w_re[i] * fit_re["resid"][i] ** 2 for i in range(len(w_re)))
            tau2_res = max(0.0, (q_res - (len(w_re) - len(terms))) / c)
            ybar = sum(wi * yi for wi, yi in zip(w_re, st_y)) / sw
            q_nul = sum(wi * (yi - ybar) ** 2 for wi, yi in zip(w_re, st_y))
            tau2_nul = max(0.0, (q_nul - (len(w_re) - 1)) / c)
            if tau2_nul > 0:
                r2_tau = 1.0 - tau2_res / tau2_nul
    out_rows = []
    if model == "bayes":
        bfit = _bayes_meta(st_y, st_X, st_v, mu_sd=mu_sd, tau_sd=tau_sd, tau_prior=tau_prior)
        if not bfit:
            return None
        for a, term in enumerate(terms):
            t = bfit["terms"][a]
            out_rows.append({"Term": term, "k": k_by_term.get(term),
                             "Post. mean": _ap_num(t["estimate"]),
                             "95% CrI": _ap_ci(t["lo"], t["hi"]),
                             "BF₁₀": t["bf10"], "p": _ap_p(t["p"])})
    else:
        fit = fit_re
        if not fit:
            return None
        # One inference convention (D2/H4): t(G-1) with the cluster-robust (CR1)
        # sandwich — no normal-approximation fallback.
        gdf = max(1, int(fit.get("n_studies") or 1) - 1)
        tcrit = _t_ppf(0.975, gdf)
        for a, term in enumerate(terms):
            z = fit["beta"][a] / fit["se"][a] if fit["se"][a] > 0 else float("nan")
            sf = _t_sf(abs(z), gdf) if math.isfinite(z) else None
            p = 2.0 * sf if sf is not None else float("nan")
            lo = fit["beta"][a] - tcrit * fit["se"][a] if tcrit is not None else math.nan
            hi = fit["beta"][a] + tcrit * fit["se"][a] if tcrit is not None else math.nan
            out_rows.append({"Term": term, "k": k_by_term.get(term),
                             "Estimate": _ap_num(fit["beta"][a]),
                             "SE": _ap_num(fit["se"][a]),
                             "95% CI": _ap_ci(lo, hi),
                             "p": _ap_p(p)})
    return {"rows": out_rows, "k": comp["k"], "tau_between": comp["tau2_between"],
            "tau_within": comp["tau2_within"], "model": model,
            "tau2_between": tau2_null, "r2_tau": r2_tau}

def _level_label(name, value):
    """Presentation label for a moderator level (humanises raw snake_case tokens)."""
    lab = _mod_label(name, value)
    if lab == value and "_" in str(value):
        return _human_mod_label(value)
    return lab

def _model_note(fit):
    """Heterogeneity note for a moderator model, including R² among τ.

    Also discloses the moderator policy (audit H5): the reference level, the
    first-value substitution for multi-valued cells, and the explicit "Other"
    bucket for rare levels.
    """
    note = (f"Between-study τ² = {_ap_num(fit.get('tau_between'), 3)}; "
            f"within-study τ² = {_ap_num(fit.get('tau_within'), 3)}")
    if fit.get("r2_tau") is not None:
        note += f"; R² among τ = {_ap_num(fit['r2_tau'], 2)}"
    ref = fit.get("reference_level") or fit.get("reference_levels")
    if ref:
        if isinstance(ref, dict):
            ref = ", ".join(f"{k} = {v}" for k, v in ref.items())
            note += f"; reference level = most common ({ref})"
        else:
            note += f"; reference level = most common ({ref})"
    for sub in (fit.get("substitutions") or []):
        note += (f"; {sub.get('n_studies', 0)} study(ies) with a multi-valued cell "
                 "were assigned their first value")
    ob = fit.get("other_bucket") or fit.get("other_buckets")
    if ob:
        note += "; rare levels are pooled into an explicit Other bucket"
    return note + "."

def _apa_moderator(moderator, label, layer, spec, exclusions, scope, model, priors):
    # H5 (audit): one moderator policy, shared with the Analysis tab —
    # multi-valued cells use the study's FIRST value (disclosed), a categorical
    # moderator beyond the disclosure cap gets an explicit "Other (n levels)"
    # dummy (never a silent levels[1:9] truncation), and the reference level is
    # the most common level (documented in the table note).
    rows = _spec_rows_for(layer, spec, exclusions)
    if scope == "restricted":
        rows = [r for r in rows if r["dv_domain"] in SUSCEPTIBILITY]
    rows = _bayes_rows(rows)
    mods = _study_moderators()
    data = []
    multi_studies = set()
    for r in rows:
        vals = [str(v).strip() for v in _values(mods.get(r["study_key"], {}).get(moderator))
                if str(v).strip()]
        if not vals:
            continue
        if len(vals) > 1:
            multi_studies.add(r["study_key"])
        data.append((r, _norm(vals[0])))
    if len(data) < 6:
        return None
    counts = collections.Counter(v for _, v in data)
    if len(counts) < 2:
        return None
    ranked = [lv for lv, _ in counts.most_common()]
    ref = ranked[0]                       # reference level = most common
    cap = _level_cap(moderator, len(ranked))
    shown = ranked[1:] if cap is None else ranked[1:cap]
    shown_set = set(shown)
    rest = [lv for lv in ranked[1:] if lv not in shown_set]
    base_levels = list(shown) + (["__other__"] if rest else [])
    rest_set = set(rest)

    def _vec(val, lv):
        if lv == "__other__":
            return 1.0 if val in rest_set else 0.0
        return 1.0 if val == lv else 0.0

    lvl_label = {lv: _level_label(moderator, lv) for lv in shown}
    if rest:
        lvl_label["__other__"] = f"Other ({len(rest)} levels)"
    y = [r["effect"] for r, _ in data]
    v = [r["var"] for r, _ in data]
    X = [[1.0] + [_vec(val, lv) for lv in base_levels] for _, val in data]
    terms = ["Intercept"] + [f"{label}: {lvl_label[lv]}" for lv in base_levels]
    k_by_term = {"Intercept": counts[ref]}
    for lv, term in zip(base_levels, terms[1:]):
        k_by_term[term] = sum(counts[x] for x in rest) if lv == "__other__" else counts[lv]
    groups = [r["study_key"] for r, _ in data]
    fit = _apa_fit_design(y, v, groups, X, terms, model, priors, k_by_term=k_by_term)
    if not fit:
        return None
    fit["title"] = f"Two-level meta-regression: {label}"
    fit["reference_level"] = _level_label(moderator, ref)
    # One picture for this moderator (per-level pooled forest), so the exported
    # report carries a figure for EVERY moderator that was included -- not a
    # fallback to the DV-type forest.
    try:
        _entries = []
        for lv in ranked:
            g = [r for r, val in data if val == lv]
            if not len(g):
                continue
            dl = _dl_pool([r["effect"] for r in g], [r["var"] for r in g])
            _entries.append({"label": _level_label(moderator, lv), "estimate": dl["est"],
                             "lo": dl["lo"], "hi": dl["hi"]})
        _fname = _moderator_single_figure(moderator, "categorical", _entries,
                                          [r for r, _ in data])
        if _fname:
            fit["figure"] = os.path.join(_figures_dir(), _fname)
    except Exception:
        pass
    if multi_studies:
        fit["substitutions"] = [{
            "moderator": moderator, "n_studies": len(multi_studies),
            "note": "multi-valued studies assigned their first value"}]
    if rest:
        fit["other_bucket"] = f"Other ({len(rest)} levels)"
    return fit

def _apa_combined_moderators(names, interactions, layer, spec, exclusions, scope, model, priors):
    """One combined model with all selected moderators and their interactions."""
    rows = _spec_rows_for(layer, spec, exclusions)
    if scope == "restricted":
        rows = [r for r in rows if r["dv_domain"] in SUSCEPTIBILITY]
    rows = _bayes_rows(rows)
    mods = _study_moderators()
    data = []
    multi_studies = set()
    for r in rows:
        vals = {}
        ok = True
        for n in names:
            vs = [str(v).strip() for v in _values(mods.get(r["study_key"], {}).get(n))
                  if str(v).strip()]
            if not vs:
                ok = False
                break
            if len(vs) > 1:
                multi_studies.add(r["study_key"])
            vals[n] = _norm(vs[0])
        if ok:
            data.append((r, vals))
    if len(data) < 6:
        return None
    labels = {m["name"]: m.get("label", m["name"]) for m in _moderator_catalog()["moderators"]}
    base_cols = []
    ref_of = {}
    other_terms = {}
    lvl_terms = {}
    for n in names:
        counts = collections.Counter(vv[n] for _, vv in data)
        if len(counts) < 2:
            continue
        ranked = [lv for lv, _ in counts.most_common()]
        ref_of[n] = ranked[0]                  # reference = most common
        cap = _level_cap(n, len(ranked))
        shown = ranked[1:] if cap is None else ranked[1:cap]
        shown_set = set(shown)
        rest = [lv for lv in ranked[1:] if lv not in shown_set]
        rest_set = set(rest)
        for lv in shown:
            vec = [1.0 if vv[n] == lv else 0.0 for _, vv in data]
            term = f"{labels.get(n, n)}: {_level_label(n, lv)}"
            base_cols.append((n, term, vec))
            lvl_terms[(n, lv)] = term
        if rest:
            vec = [1.0 if vv[n] in rest_set else 0.0 for _, vv in data]
            term = f"{labels.get(n, n)}: Other ({len(rest)} levels)"
            base_cols.append((n, term, vec))
            other_terms[n] = term
    if not base_cols:
        return None
    terms = ["Intercept"] + [t for _, t, _ in base_cols]
    cols = [[1.0] * len(data)] + [vec for _, _, vec in base_cols]
    k_by_term = {t: int(sum(vec)) for _, t, vec in base_cols}
    k_by_term["Intercept"] = (sum(1 for _, vv in data
                                  if all(vv[n] == ref_of[n] for n in ref_of))
                              if ref_of else len(data))
    for a, b in interactions:
        for _, ta, va in [c for c in base_cols if c[0] == a]:
            for _, tb, vb in [c for c in base_cols if c[0] == b]:
                term = f"{ta} × {tb}"
                vec = [x * y for x, y in zip(va, vb)]
                terms.append(term)
                cols.append(vec)
                k_by_term[term] = int(sum(vec))
    keep, dropped = [0], []
    for j in range(1, len(cols)):
        cand = keep + [j]
        M = [[sum(cols[a][i] * cols[b][i] for i in range(len(cols[a]))) for b in cand]
             for a in cand]
        if _mat_inv(M) is not None:
            keep.append(j)
    dropped = [terms[j] for j in range(len(terms)) if j not in keep]
    cols = [cols[j] for j in keep]
    terms = [terms[j] for j in keep]
    X = [[cols[c][i] for c in range(len(cols))] for i in range(len(data))]
    y = [r["effect"] for r, _ in data]
    v = [r["var"] for r, _ in data]
    groups = [r["study_key"] for r, _ in data]
    fit = _apa_fit_design(y, v, groups, X, terms, model, priors, k_by_term=k_by_term)
    if not fit:
        return None
    fit["title"] = "Combined moderator model (all selected moderators)"
    fit["dropped_terms"] = dropped
    fit["n_terms"] = len(terms)
    fit["reference_levels"] = {n: _level_label(n, ref_of[n]) for n in ref_of}
    if multi_studies:
        fit["substitutions"] = [{
            "moderator": " / ".join(names), "n_studies": len(multi_studies),
            "note": "multi-valued studies assigned their first value"}]
    if other_terms:
        fit["other_buckets"] = list(other_terms.values())
    return fit

def _apa_moderator_descriptives(names):
    """Level distributions of the moderators carried over from the Moderator tab."""
    mods = _study_moderators()
    labels = {m["name"]: m.get("label", m["name"]) for m in _moderator_catalog()["moderators"]}
    tables = []
    for n in names:
        c = collections.Counter()
        for m in mods.values():
            for v in set(_values(m.get(n))):
                if v not in (None, ""):
                    c[_mod_label(n, v)] += 1
        if not c:
            continue
        lab = labels.get(n, n)
        tables.append({"title": f"Moderator distribution: {lab}",
                       "columns": [lab, "Studies"],
                       "rows": [{lab: k, "Studies": v} for k, v in c.most_common()]})
    return tables

def _apa_reliability():
    try:
        data = _agreement_index()
    except Exception:
        return {"available": False, "note": "Agreement backend unavailable."}
    raters = data.get("rater_names") or []
    if len(raters) < 2:
        return {"available": False,
                "note": (f"Only {len(raters)} rater(s) available "
                         f"({', '.join(raters) or 'none'}), so Gwet's AC could not be computed; "
                         "at least two coders (or coder files) are required.")}
    per, wsum, acc = [], 0.0, 0.0
    for f in data["fields"]:
        cmap = _field_case_map(data["raters"], f["name"])
        case_ratings, observed = [], set()
        for key, cases in cmap.items():
            for case, vals in cases.items():
                observed.update(vals.values())
                case_ratings.append(list(vals.values()))
        n_multi = sum(1 for v in case_ratings if len(v) >= 2)
        if not n_multi:
            continue
        cats = sorted(observed) if f["multi"] else list(f["levels"]) + sorted(
            v for v in observed if v not in f["levels"])
        res = gwet_ac(case_ratings, cats) if 2 <= len(cats) < _LABEL_MAX_LEVELS else None
        if res and res["coeff"] is not None:
            per.append({"Field": f["label"], "AC1": res["coeff"], "k (items)": res["n2"]})
            acc += res["coeff"] * res["n2"]
            wsum += res["n2"]
    overall = (acc / wsum) if wsum else None
    return {"available": bool(per), "overall_ac1": overall,
            "n_fields": len(per), "n_items": wsum, "rows": per}

def _apa_context(payload):
    layer = (payload.get("layer") or "meta").strip()
    spec = (payload.get("spec") or "").strip()
    exclusions = (payload.get("exclusions") or "on").strip().lower()
    scope = (payload.get("scope") or "unrestricted").strip()
    model = (payload.get("model") or "freq").strip().lower()
    descriptives = payload.get("descriptives") or ["sample", "age", "setting", "design",
                                                   "control", "intervention", "domains", "year"]
    analyses = payload.get("analyses") or ["pooled_freq", "heterogeneity", "bias"]
    priors = payload.get("priors") or {}
    moderator_names = payload.get("moderators") or []
    apa_interactions = [tuple(x) for x in (payload.get("interactions") or []) if len(x) == 2]

    counts = _prisma_counts() or {}
    layer_label = _APA_LAYER.get(layer, layer)
    spec_label = _APA_SPEC.get(spec, spec)
    excl_label = "author exclusions applied" if exclusions == "on" else "author exclusions ignored"

    # Analysis set (audit M6): the descriptive tables describe the same studies /
    # experiments that contribute to the pool — author exclusions, fully-within
    # and eligibility are already applied by ``_spec_rows_for``.
    try:
        _pool_rows_for_set = _spec_rows_for(layer, spec, exclusions)
        if scope == "restricted":
            _pool_rows_for_set = [r for r in _pool_rows_for_set
                                  if r["dv_domain"] in SUSCEPTIBILITY]
    except Exception:
        _pool_rows_for_set = []
    _pool_studies = {r.get("study_key") for r in _pool_rows_for_set if r.get("study_key")}
    records_all = _apa_records()
    if _pool_studies:
        if layer == "meta":
            _exp_ids = {(r.get("study_key"), str(r.get("experiment_id") or ""))
                        for r in _pool_rows_for_set}
            records = [r for r in records_all
                       if (r.get("_key"), str(r.get("Study_ID") or "")) in _exp_ids]
            if not records:
                records = [r for r in records_all if r.get("_key") in _pool_studies]
        else:
            records = [r for r in records_all if r.get("_key") in _pool_studies]
    else:
        records = records_all
    analyses_set_note = (f"Study characteristics describe the analysis set for "
                         f"'{spec_label}' ({excl_label}); "
                         f"{len({r.get('_key') for r in records})} studies.")

    # ---------------- Method ----------------
    methods = []
    methods.append({"heading": "Search strategy", "paragraphs": [
        f"Records were identified by a database search and deduplicated; "
        f"the search yielded {counts.get('total', 0):,} records. No language or date "
        f"restriction was applied beyond the eligibility criteria below."]})
    methods.append({"heading": "Eligibility criteria", "paragraphs": [
        "Records were eligible when the title/abstract indicated that (C1) the study tested an "
        "intervention aimed at reducing susceptibility to misinformation; (C2) the study included "
        "a control, placebo, or comparison group; (C3) the paper was written in English; and "
        "(C4) the study was a primary quantitative experimental or quasi-experimental study. "
        "Records failing any criterion were excluded; uncertain records were retained."]})
    methods.append({"heading": "Selection process", "paragraphs": [
        f"Records from database and register searches were screened on title/abstract against "
        f"C1–C4 ({counts.get('abstract_excluded', 0):,} excluded); records identified via other "
        f"sources (meta-analyses and reviews) went straight to full-text screening. "
        f"{counts.get('fulltext_assessed', 0):,} reports were assessed for eligibility, of which "
        f"{counts.get('fulltext_excluded', 0):,} were excluded under the review criteria "
        f"({_excl_types_txt(counts)}). "
        f"{counts.get('coded', 0):,} studies were included in the synthesis."]})
    methods.append({"heading": "Data extraction and effect-size computation", "paragraphs": [
        "Each eligible study was coded for sample, design, intervention type, control type, and "
        "outcome characteristics. Reported effects were converted to a common standardised metric; "
        "pretest-corrected difference-in-differences effects were derived where pretest and posttest "
        "cell statistics were available."]})
    if model == "bayes":
        mu_sd, tau_prior, tau_sd = _resolve_bayes_priors(priors)
        methods.append({"heading": "Statistical analysis", "paragraphs": [
            f"Random-effects models were fitted for the {layer_label} under the specification "
            f"'{spec_label}' ({excl_label}). The primary estimate is the posterior mean with a 95% "
            f"credible interval from a Bayesian normal–normal random-effects model with "
            f"μ ~ N(0, {mu_sd:g}²) and τ ~ {tau_prior}({tau_sd:g}); Bayes factors were obtained by "
            f"the Savage–Dickey density ratio. Between- and within-study heterogeneity were estimated "
            f"with a two-level model (effects nested in studies)."]})
    else:
        methods.append({"heading": "Statistical analysis", "paragraphs": [
            f"Random-effects models were fitted for the {layer_label} under the specification "
            f"'{spec_label}' ({excl_label}). Dependent contrasts are handled with a three-level "
            f"random-effects model (effects within experiments within studies) and cluster-robust "
            f"(RVE) standard errors clustered on the experiment × control-arm unit; the "
            f"DerSimonian–Laird, two-level (effects nested in studies) and fixed-effect "
            f"study-clustered (CR1) estimates are reported as comparisons. Meta-regression "
            f"coefficients use the cluster-robust covariance with a t(G−1) reference. "
            f"Heterogeneity is reported as τ² and I²."]})
    methods.append({"heading": "Variance components", "paragraphs": [
        "We examined sampling variance at level 1 (i.e., variance due to studies only collecting "
        "samples from the entire population), level 2 (i.e., the variance τ² between different "
        "effects within the same study), and level 3 (i.e., the variance σ² between different "
        "studies). The three components are estimated by method of moments from the two-level "
        "(effects nested in studies) model and reported as a proportional variance decomposition."]})
    if layer == "sdt":
        methods.append({"heading": "Signal-detection estimand", "paragraphs": [
            "The signal-detection layer estimates the intervention effect on discrimination: "
            "d′ = z(HR) − z(FAR), where HR is the hit rate for true (non-manipulative) stimuli and "
            "FAR the false-alarm rate for false (manipulative) stimuli; per participant, d′ is "
            "computed within each arm and the pooled quantity is the intervention-vs-control "
            "difference d′_intervention − d′_control. Studies or cells without a no-misinformation "
            "control arm are excluded from the effect (and counted in the run audit). The "
            "arm-pooled discrimination level is an auxiliary descriptive quantity "
            "('AUXILIARY arm-pooled discrimination level'), not the intervention effect. Because the "
            "outcome is a within-person discrimination index, the meta-analytic small-study-effect "
            "battery is not applicable; the mean endorsement of true stimuli (true score), of false "
            "stimuli (fake score) and the response bias (criterion c = −0.5[z(HR) + z(FAR)]) are "
            "reported separately."]})
    else:
        methods.append({"heading": "Publication bias", "paragraphs": [
            "Small-study effects were assessed with Egger's regression test, PET-PEESE, trim-and-fill "
            "(Duval & Tweedie L0), and p-curve where enough significant p values were available."]})
    # Inter-rater reliability belongs in ONE place: the Method.  The coefficient
    # and its table are reported here and are NOT repeated in the Results.
    rel_irr = _apa_reliability()
    _rel_tables = []
    _irr_para = ("Reliability of the deterministic coding labels was quantified with Gwet's AC1 "
                 "(nominal) and AC2 (linear weights for ordered labels), which accommodate raters "
                 "who do not all code the same items.")
    if "reliability" in analyses:
        if rel_irr.get("available"):
            _rel_rows = [{"Field": x["Field"], "AC1": _ap_num(x["AC1"], 3),
                          "k (items)": x["k (items)"]}
                         for x in sorted(rel_irr["rows"], key=lambda z: -z["AC1"])[:20]]
            _rel_tables.append({"title": "Gwet's AC1 by deterministic coding label",
                                "columns": ["Field", "AC1", "k (items)"], "rows": _rel_rows})
            _irr_para += (f" Across {rel_irr['n_fields']} deterministic labels "
                          f"({rel_irr['n_items']:,} item cells), the item-weighted mean Gwet's AC1 "
                          f"was {_ap_num(rel_irr['overall_ac1'], 3)}.")
        else:
            _irr_para += " " + rel_irr.get("note", "Not available.")
    methods.append({"heading": "Inter-rater reliability", "paragraphs": [_irr_para],
                    "tables": _rel_tables})
    methods.append({"heading": "Software", "paragraphs": [
        "Screening, coding, the analysis suite, the reliability computation, the PRISMA diagram and "
        "this report were produced by the Coding Studio application and its Python analysis scripts; "
        "the APA 7 document was assembled automatically from those outputs."]})

    # ---------------- Results ----------------
    results = []
    fig = _prisma_figure()
    src = counts.get("sources") or {}
    src_txt = ", ".join(f"{n:,} from {k.lower()}" for k, n in src.items())
    sel_txt = (f"{counts.get('total', 0):,} records were identified ({src_txt or 'no sources recorded'}). "
               f"{counts.get('abstract_excluded', 0):,} database records were excluded during "
               f"title/abstract screening; records identified via other sources went straight to "
               f"full-text screening. {counts.get('fulltext_assessed', 0):,} reports were assessed for "
               f"eligibility; {counts.get('fulltext_excluded', 0):,} were excluded for the named "
               f"criteria ({_excl_types_txt(counts)}), and {counts.get('coded', 0):,} studies were "
               f"included in the synthesis.")
    prisma_sec = {"heading": "Study selection", "paragraphs": [sel_txt], "tables": []}
    if fig:
        prisma_sec["figures"] = [{"title": "PRISMA 2020 flow diagram", "path": fig}]
    results.append(prisma_sec)

    desc_tables = _apa_descriptive_tables(descriptives, records) \
        + _apa_moderator_descriptives(moderator_names)
    results.append({"heading": "Study characteristics",
                    "paragraphs": [analyses_set_note,
                                   f"The synthesis comprised {len(records):,} experiments from "
                                   f"{len({r.get('_key') for r in records}):,} studies."],
                    "tables": desc_tables})

    synth_tables, synth_paras = [], []
    freq = _apa_freq_models(layer, spec, exclusions, scope)
    if freq and ("pooled_freq" in analyses or "heterogeneity" in analyses):
        if "pooled_freq" in analyses:
            dep = freq.get("dep") or {}
            synth_tables.append({
                "title": "Pooled effect estimates (frequentist)",
                "columns": ["Model", "k", "Studies", "Estimate", "95% CI"],
                "rows": [
                    {"Model": "DerSimonian–Laird random effects", "k": freq["k"],
                     "Studies": freq["n_studies"], "Estimate": _ap_num(freq["dl"]["est"]),
                     "95% CI": _ap_ci(freq["dl"]["lo"], freq["dl"]["hi"])},
                    {"Model": "Three-level (effects in experiments in studies)", "k": freq["k"],
                     "Studies": freq["n_studies"], "Estimate": _ap_num(dep.get("three_est")),
                     "95% CI": _ap_ci(dep.get("three_lo"), dep.get("three_hi"))},
                    {"Model": "RVE (cluster-robust, ρ = .5)", "k": freq["k"],
                     "Studies": freq["n_studies"], "Estimate": _ap_num(dep.get("rve_est")),
                     "95% CI": _ap_ci(dep.get("rve_lo"), dep.get("rve_hi"))},
                    {"Model": "Two-level (effects nested in studies)", "k": freq["k"],
                     "Studies": freq["n_studies"], "Estimate": _ap_num(freq["two"].get("est")),
                     "95% CI": _ap_ci(freq["two"].get("lo"), freq["two"].get("hi"))},
                    {"Model": "Fixed effect, study-clustered (CR1)", "k": freq["k"],
                     "Studies": freq["n_studies"], "Estimate": _ap_num(freq["cluster"]["est"]),
                     "95% CI": _ap_ci(freq["cluster"]["lo"], freq["cluster"]["hi"])},
                ],
                "note": "CR1 = cluster-robust standard errors; the two-level model combines "
                        "effects within each study before pooling across studies; the "
                        "dependence-aware three-level/RVE models cluster on the experiment × "
                        "control-arm unit (same rule as the pipeline)."})
            if layer == "sdt":
                _dep = freq.get("dep") or {}
                _pe, _plo, _phi = _dep.get("three_est"), _dep.get("three_lo"), _dep.get("three_hi")
                if _pe is None or not math.isfinite(float(_pe)):
                    _pe, _plo, _phi = freq["dl"]["est"], freq["dl"]["lo"], freq["dl"]["hi"]
                synth_paras.append(
                    f"The pooled intervention effect on discrimination (three-level model) was "
                    f"Δd′ = {_ap_num(_pe)} (95% CI {_ap_ci(_plo, _phi)}; k = {freq['k']}, "
                    f"{freq['n_studies']} studies).")
            else:
                synth_paras.append(
                    f"The pooled random-effects estimate was {_ap_num(freq['dl']['est'])} "
                    f"(95% CI {_ap_ci(freq['dl']['lo'], freq['dl']['hi'])}; k = {freq['k']}, "
                    f"{freq['n_studies']} studies).")
        if "heterogeneity" in analyses:
            two = freq["two"]
            synth_tables.append({
                "title": "Variance decomposition (three-level model)",
                "columns": ["Level", "Component", "Variance", "Share of variance (%)"],
                "rows": [
                    {"Level": "Level 1", "Component": "Sampling variance",
                     "Variance": _ap_num(two.get("vbar"), 4),
                     "Share of variance (%)": _ap_num(two.get("i2_sampling"), 1)},
                    {"Level": "Level 2", "Component": "τ² (between effects within studies)",
                     "Variance": _ap_num(two["tau2_within"], 4),
                     "Share of variance (%)": _ap_num(two.get("i2_within_alloc"), 1)},
                    {"Level": "Level 3", "Component": "σ² (between studies)",
                     "Variance": _ap_num(two["tau2_between"], 4),
                     "Share of variance (%)": _ap_num(two.get("i2_between_alloc"), 1)},
                ],
                "note": "Level 1 is the median within-effect sampling variance; τ² and σ² "
                        "are the method-of-moments within- and between-study heterogeneity; "
                        "shares are the proportional allocation of total variance."})
            synth_paras.append(
                f"Variance was decomposed into level-1 sampling variance "
                f"({_ap_num(two.get('i2_sampling'), 0)}% of total), level-2 τ² between "
                f"effects within studies ({_ap_num(two.get('i2_within_alloc'), 0)}%) and "
                f"level-3 σ² between studies ({_ap_num(two.get('i2_between_alloc'), 0)}%).")
    if freq and "pooled_bayes" in analyses:
        bp = _bayes_for(layer, spec, exclusions, scope)
        if bp is None:
            rows = [r for r in _bayes_rows(_spec_rows_for(layer, spec, exclusions))]
            mu_sd, tau_prior, tau_sd = _resolve_bayes_priors(priors)
            bp = _bayes_pool([r["effect"] for r in rows], [r["var"] for r in rows],
                             mu_sd=mu_sd, tau_sd=tau_sd, tau_prior=tau_prior) if rows else None
        if bp:
            def _bf(x):
                try:
                    x = float(x)
                except (TypeError, ValueError):
                    return "–"
                if not math.isfinite(x) or x > 1000:
                    return "> 1,000"
                if x < 0.001:
                    return "< 0.001"
                return f"{x:.3f}"
            synth_tables.append({
                "title": "Bayesian random-effects estimate",
                "columns": ["Estimate", "95% CrI", "τ", "BF₁₀ (effect)", "BF₀₁ (τ = 0)"],
                "rows": [{"Estimate": _ap_num(bp["est"]), "95% CrI": _ap_ci(bp["lo"], bp["hi"]),
                          "τ": _ap_num(bp["tau"], 3), "BF₁₀ (effect)": _bf(bp.get("bf10")),
                          "BF₀₁ (τ = 0)": _bf(bp.get("bf01_tau0"))}],
                "note": f"Prior: μ ~ N(0, {bp.get('prior_mu_sd', 10):g}²); "
                        f"τ ~ {bp.get('tau_prior', 'half-normal')}({bp.get('prior_tau_sd', 0.5):g}); "
                        f"τ grid truncated at {bp.get('tau_max', 3.0):g} (disclosed)."})
            synth_paras.append(
                f"Under the Bayesian model the posterior mean was {_ap_num(bp['est'])} "
                f"(95% CrI {_ap_ci(bp['lo'], bp['hi'])}), τ = {_ap_num(bp['tau'], 3)}.")
            # Bayes-appropriate small-study-effect analysis: the frequentist
            # Egger/PET battery is not a Bayesian procedure, so the report carries
            # the Bayesian analogue (meta-regression of the effect on its SE).
            if layer != "sdt":
                _, bpb = _sel_rows(os.path.join(S.ROOT, "temp", "pb_bayes_pooled.csv"),
                                   layer, spec, exclusions)
                if bpb:
                    b0 = bpb[0]
                    _gslo = b0.get("bayes_egger_slope_lo")
                    _gshi = b0.get("bayes_egger_slope_hi")
                    _ppos = b0.get("bayes_egger_slope_p_pos")
                    if _ap_num(b0.get("bayes_egger_slope")) not in ("–", None):
                        synth_tables.append({
                            "title": "Bayesian small-study-effect analysis",
                            "columns": ["Term", "Estimate", "95% CrI"],
                            "rows": [
                                {"Term": "PET (SE-regression intercept)",
                                 "Estimate": _ap_num(b0.get("bayes_pet_intercept")),
                                 "95% CrI": _ap_ci(b0.get("bayes_pet_lo"), b0.get("bayes_pet_hi"))},
                                {"Term": "Egger asymmetry (slope on SE)",
                                 "Estimate": _ap_num(b0.get("bayes_egger_slope")),
                                 "95% CrI": _ap_ci(_gslo, _gshi)},
                            ],
                            "note": (f"Bayesian meta-regression of the effect on its standard error "
                                     f"(k = {b0.get('k')}); posterior P(slope > 0) = "
                                     f"{_ap_num(_ppos, 3)}. Positive slope = small-study effect.")})
                        synth_paras.append(
                            f"A Bayesian meta-regression of the effect on its standard error gave an "
                            f"intercept (PET) of {_ap_num(b0.get('bayes_pet_intercept'))} and a slope "
                            f"(Egger asymmetry) of {_ap_num(b0.get('bayes_egger_slope'))} "
                            f"(95% CrI {_ap_ci(_gslo, _gshi)}).")
    results.append({"heading": "Synthesis of effects", "paragraphs": synth_paras,
                    "tables": synth_tables})

    sub_tables = []
    if "domain" in analyses:
        cols, rows = _sel_rows(os.path.join(S.ROOT, "temp", "pb_domain.csv"), layer, spec, exclusions)
        if rows:
            t = [{"Outcome domain": _apa_domain(r.get("dv_domain")), "k": r.get("k"),
                  "Studies": r.get("n_studies"), "Estimate": _ap_num(r.get("est")),
                  "95% CI": _ap_ci(r.get("lo"), r.get("hi"))} for r in rows]
            sub_tables.append({"title": "Pooled effects by outcome domain",
                               "columns": ["Outcome domain", "k", "Studies", "Estimate", "95% CI"],
                               "rows": t})
    if "intervention" in analyses:
        cols, rows = _sel_rows(os.path.join(S.ROOT, "temp", "pb_intervention_type.csv"),
                               layer, spec, exclusions)
        if rows:
            t = [{"Intervention type": r.get("intervention_type"), "k": r.get("k"),
                  "Studies": r.get("n_studies"), "Estimate": _ap_num(r.get("est")),
                  "95% CI": _ap_ci(r.get("lo"), r.get("hi"))} for r in rows]
            sub_tables.append({"title": "Pooled effects by intervention type",
                               "columns": ["Intervention type", "k", "Studies", "Estimate", "95% CI"],
                               "rows": t})
    if "data_available" in analyses:
        cols, rows = _sel_rows(os.path.join(S.ROOT, "temp", "pb_moderator.csv"), layer, spec, exclusions)
        if rows:
            t = [{"Moderator": r.get("moderator"), "Level": r.get("level_label"), "k": r.get("k"),
                  "Estimate": _ap_num(r.get("est")), "95% CI": _ap_ci(r.get("lo"), r.get("hi"))}
                 for r in rows]
            sub_tables.append({"title": "Pooled effects by primary-data availability",
                               "columns": ["Moderator", "Level", "k", "Estimate", "95% CI"],
                               "rows": t})
    if "metric" in analyses:
        cols, rows = _sel_rows(os.path.join(S.ROOT, "temp", "pb_metric_family.csv"), layer, spec, exclusions)
        if rows:
            t = [{"Metric family": _apa_metric(r.get("metric")), "k": r.get("k"), "Estimate": _ap_num(r.get("est")),
                  "95% CI": _ap_ci(r.get("lo"), r.get("hi"))} for r in rows]
            sub_tables.append({"title": "Pooled effects by effect-size metric family",
                               "columns": ["Metric family", "k", "Estimate", "95% CI"], "rows": t})
    if "r_sensitivity" in analyses and layer == "meta":
        cols, rows = _sel_rows(os.path.join(S.ROOT, "temp", "pb_r_sensitivity.csv"), layer, spec, exclusions)
        if rows:
            t = [{"Specification": _apa_spec(r.get("spec")), "r": r.get("r_used"), "k": r.get("k"),
                  "Estimate": _ap_num(r.get("est")), "95% CI": _ap_ci(r.get("lo"), r.get("hi"))}
                 for r in rows]
            sub_tables.append({"title": "Sensitivity to the assumed pretest correlation (r)",
                               "columns": ["Specification", "r", "k", "Estimate", "95% CI"], "rows": t})
    if "exclusions" in analyses:
        cols, rows = _sel_rows(os.path.join(S.ROOT, "temp", "pb_exclusions_onoff.csv"),
                               layer, spec, exclusions)
        if rows:
            t = [{"Specification": _apa_spec(r.get("spec")), "k (applied)": r.get("on_k"),
                  "Estimate (applied)": _ap_num(r.get("on_est")),
                  "Estimate (ignored)": _ap_num(r.get("off_est")),
                  "Change": _ap_num(r.get("delta_off_minus_on"))} for r in rows]
            sub_tables.append({"title": "Author exclusions: applied vs ignored",
                               "columns": ["Specification", "k (applied)", "Estimate (applied)",
                                           "Estimate (ignored)", "Change"], "rows": t})
    label_of = {m["name"]: m.get("label", m["name"])
                for m in _moderator_catalog()["moderators"]}
    mod_para = []
    mod_figures = []
    for name in moderator_names:
        label = label_of.get(name, name)
        fit = _apa_moderator(name, label, layer, spec, exclusions, scope, model, priors)
        if fit:
            cols = (["Term", "k", "Post. mean", "95% CrI", "BF₁₀", "p"] if model == "bayes"
                    else ["Term", "k", "Estimate", "SE", "95% CI", "p"])
            sub_tables.append({"title": fit["title"], "columns": cols, "rows": fit["rows"],
                               "note": _model_note(fit)})
            if fit.get("figure") and os.path.isfile(fit["figure"]):
                mod_figures.append({"title": f"Moderator: {label}", "path": fit["figure"]})
    if moderator_names:
        comb = _apa_combined_moderators(moderator_names, apa_interactions,
                                        layer, spec, exclusions, scope, model, priors)
        if comb:
            cols = (["Term", "k", "Post. mean", "95% CrI", "BF₁₀", "p"] if model == "bayes"
                    else ["Term", "k", "Estimate", "SE", "95% CI", "p"])
            note = _model_note(comb)
            if comb.get("dropped_terms"):
                note += " Dropped collinear term(s): " + "; ".join(comb["dropped_terms"]) + "."
            sub_tables.append({"title": comb["title"], "columns": cols, "rows": comb["rows"],
                               "note": note})
            mod_para.append(f"A combined model including all {len(moderator_names)} selected "
                            f"moderator(s) was also fitted.")
    if "leave_one_out" in analyses:
        loo_cols, loo_rows = _loo_summary(layer, spec, exclusions)
        if loo_rows:
            sub_tables.append({"title": "Leave-one-out influence", "columns": loo_cols,
                               "rows": loo_rows,
                               "note": "Each row omits one contrast or study and re-pools; "
                                       "influential cases are flagged where omission moves the "
                                       "estimate markedly."})
    if sub_tables:
        results.append({"heading": "Subgroup and moderator analyses",
                        "paragraphs": mod_para, "tables": sub_tables})

    if "bias" in analyses and layer == "sdt":
        # For the SDT layer the meta publication-bias battery is not meaningful;
        # report the decomposition into true score, fake score and response bias
        # (discrimination is already the pooled effect in the synthesis table).
        _, comps = _sel_rows(os.path.join(S.ROOT, "temp", "pb_sdt_components.csv"),
                             layer, spec, exclusions)
        if comps:
            rows = [{"Component": c.get("component"), "k": c.get("k"),
                     "n_studies": c.get("n_studies"), "Estimate": _ap_num(c.get("est")),
                     "95% CI": _ap_ci(c.get("lo"), c.get("hi"))} for c in comps]
            para = ("Signal-detection components: the estimated mean endorsement of true "
                    "stimuli (true score), of false stimuli (fake score) and the response bias "
                    "(criterion c = −0.5[z(HR) + z(FAR)]; positive = conservative, "
                    "negative = liberal) are reported separately as intervention-vs-control "
                    "differences. The intervention effect on discrimination (Δd′, "
                    "d′ = z(HR) − z(FAR)) is given in the synthesis table above; the arm-pooled "
                    "discrimination level is an auxiliary quantity, not the effect.")
            results.append({"heading": "SDT components (true score, fake score, response bias)",
                            "paragraphs": [para],
                            "tables": [{"title": "True score, fake score and response bias",
                                        "columns": ["Component", "k", "n_studies", "Estimate", "95% CI"],
                                        "rows": rows}]})
    elif "bias" in analyses:
        cols, rows = _sel_rows(os.path.join(S.ROOT, "temp", "pb_publication_bias.csv"),
                               layer, spec, exclusions)
        if rows:
            r = rows[0]
            bias = [{"Test": "Egger's asymmetry (slope on SE)", "Value": _ap_num(r.get("egger_slope")),
                     "Statistic": f"t = {_ap_num(r.get('egger_slope_t'))}",
                     "p": _ap_p(r.get("egger_slope_p"))},
                    {"Test": "PET (Egger intercept)", "Value": _ap_num(r.get("egger_intercept")),
                     "Statistic": f"t = {_ap_num(r.get('egger_t'))}", "p": _ap_p(r.get("egger_p"))},
                    {"Test": "PEESE", "Value": _ap_num(r.get("peese")),
                     "Statistic": "", "p": _ap_p(r.get("peese_p"))},
                    {"Test": "Trim-and-fill (adjusted)", "Value": _ap_num(r.get("tf_adj_est")),
                     "Statistic": f"k0 = {r.get('tf_k0')}", "p": ""}]
            results.append({"heading": "Publication bias", "paragraphs": [
                f"Egger's regression asymmetry test (slope of the effect on its standard error) "
                f"was {_ap_num(r.get('egger_slope'))} (t = {_ap_num(r.get('egger_slope_t'))}, "
                f"p {_ap_p(r.get('egger_slope_p'))}); the PET bias-adjusted estimate was "
                f"{_ap_num(r.get('egger_intercept'))}. Preferred PET-PEESE estimator: "
                f"{r.get('pet_peese_prefer', '–')} = {_ap_num(r.get('pet_peese_est'))}."],
                "tables": [{"title": "Publication-bias analyses",
                            "columns": ["Test", "Value", "Statistic", "p"], "rows": bias}]})

    # (Inter-rater reliability is reported once, in the Method above — not here.)

    if "figures" in analyses:
        figs = _figure_index()
        forest = [f for f in figs if f.get("ext") == ".png" and f.get("kind") == "forest"
                  and f.get("layer") == layer and f.get("spec") == spec]
        if not forest:
            forest = [f for f in figs if f.get("ext") == ".png" and f.get("kind") == "forest"
                      and f.get("layer") == layer]
        section = {"heading": "Figures", "paragraphs": [], "tables": [], "figures": []}
        for f in forest[:1]:
            p = os.path.join(_figures_dir(), f["stem"] + ".png")
            if os.path.isfile(p):
                section["figures"].append({"title": _human_mod_label(f.get("stem")), "path": p})
        # One figure for EACH moderator that was included (never a DV-type fallback).
        section["figures"] += mod_figures
        if section["figures"]:
            _nmod = len(mod_figures)
            section["paragraphs"] = [
                f"Forest and moderator figures for the {_APA_LAYER.get(layer, layer)} layer "
                f"({_APA_SPEC.get(spec, spec)}) are reproduced below"
                + (f"; one figure per moderator ({_nmod}), plus the specification forest."
                   if _nmod else ".")]
            results.append(section)

    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    ctx = {
        "title": "Methods and Results",
        "generated": generated,
        "priors": priors,
        "methods": methods,
        "results": results,
        "_table_no": 1, "_figure_no": 1,
        "prisma": counts,
        "layer": layer, "spec": spec, "exclusions": exclusions, "model": model,
    }
    return ctx

@bp.get("/api/analysis/prisma.png")
def api_analysis_prisma_png():
    path = _prisma_figure()
    if not path or not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype="image/png", download_name="prisma.png", max_age=0)

@bp.post("/api/analysis/apa")
def api_analysis_apa():
    payload = request.get_json(force=True, silent=True) or {}
    try:
        ctx = _apa_context(payload)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(S.TEMP, exist_ok=True)
    docx_path = os.path.join(S.TEMP, f"analysis_report_{stamp}.docx")
    built = _APA.build_docx(ctx, docx_path)
    fmt = (payload.get("format") or "docx").strip().lower()
    if fmt == "md" or built is None:
        md = _APA.build_markdown(ctx)
        md_path = os.path.join(S.TEMP, f"analysis_report_{stamp}.md")
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(md)
        return send_file(md_path, mimetype="text/markdown",
                         download_name=f"analysis_report_{stamp}.md", as_attachment=True)
    return send_file(built, mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                     download_name=f"analysis_report_{stamp}.docx", as_attachment=True)
