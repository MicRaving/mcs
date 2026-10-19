"""Meta-analysis over the coded extraction JSONs (Extraction/*.json).

- Flattens every experiment object + every DV_Metric into two sheets (Study_Level, DV_Metrics).
- Derives one effect size per contrast (Hedges' g / Fisher's z) from, in order of preference:
  reported `yi`+`vi`, reported `d`/`g`, `t`, `F`, `r`, or group means/SDs.
- Runs a random-effects (DerSimonian-Laird) meta-analysis on `eligible_primary == 1`
  contrasts, overall and per DV domain (domains resolved via Scripts/domain_restriction.py).
- TIMEPOINT SEPARATION (2026-09-17, see Scripts/TIMEPOINT_SEPARATION_NOTE.md): the three
  questions are NEVER pooled together.  `MetaResults` is split into
  `posttest (immediate)`, `follow-up (persistence)` and `DiD (pre->post)`; pretest/baseline
  rows are excluded from every outcome and are used ONLY to derive the DiD.  A follow-up row
  never enters the immediate-posttest pool and vice versa.
- Restricts each question to the misinformation-susceptibility domains (belief_accuracy,
  credibility, sharing_intention, technique_recognition) in `MetaResults_restricted`.
- Writes `metaanalysis.xlsx` (sheets: Study_Level, DV_Metrics, Effects, MetaResults,
  MetaResults_restricted, Domain_Counts) in the project root.

Run:  python metaanalysis.py
"""
import glob
import json
import math
import os
import re
import sys

import numpy as np
import pandas as pd

import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))     # Scripts/analysis
SCRIPTS = os.path.dirname(_HERE)                       # Scripts
B = os.path.dirname(SCRIPTS)                           # project root
RESULTS = os.path.join(B, "Results")
os.makedirs(RESULTS, exist_ok=True)
_TMP = os.path.join(B, "temp")
os.makedirs(_TMP, exist_ok=True)
tempfile.tempdir = _TMP

EXTRACT = os.path.join(B, "Extraction")
OUT = os.path.join(RESULTS, "metaanalysis.xlsx")

# Shared controlled-vocabulary / restriction logic (see Scripts/DOMAIN_RESTRICTION_NOTE.md),
# non-independent experiment exclusions (see Scripts/FULLTEXT_DUPLICATE_LOG.md), and the
# canonical timepoint classifier shared with the analysis suite.
sys.path.insert(0, SCRIPTS)
import domain_restriction as DR  # noqa: E402
import study_exclusions as SE  # noqa: E402
import suite_common as SC  # noqa: E402
import analysis_log as AL  # noqa: E402  (durable drop log, Results/analysis_log.jsonl)

try:  # console-safe output for study names with non-cp1252 characters
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

QUESTIONS = ["posttest (immediate)", "follow-up (persistence)", "DiD (pre->post)"]
DID_ABS_MAX = 3.0      # |standardised DiD| implausible above this (coded SD likely a SE)


def num(x):
    try:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return np.nan
        return float(x)
    except (TypeError, ValueError):
        return np.nan


def first_num(dv, *keys):
    """First non-null numeric value among ``keys`` (present-but-null falls through).

    ``dv.get("m1i_post", dv.get("m1i"))`` is wrong when ``m1i_post`` is present but
    null: the default is not used.  This helper skips None/NaN/blank and returns the
    next key's value, else NaN.
    """
    for k in keys:
        v = dv.get(k)
        if v is None:
            continue
        n = num(v)
        if not np.isnan(n):
            return n
    return np.nan


def polarity_sign(dv):
    """-1 when higher scores are worse, else +1 (canonical contract.polarity_sign)."""
    return SC.polarity_sign(dv)


def effect(dv):
    """Return (yi, vi, how) for one DV contrast (stat_core.meta_effect).

    W7-STRUCT: refuses a multi-df omnibus / ``contrast_type == 11`` eligible row
    via the shared predicate (identical rule to the spec-table path, so the
    workbook and the spec pool agree).
    """
    if SC.is_omnibus_eligible(dv):
        return np.nan, np.nan, "omnibus_eligible"
    yi, vi, _metric, how = SC.meta_effect(dv)
    return yi, vi, how


def dl_random(yi, vi):
    """DerSimonian-Laird random-effects pooled estimate (stat_core.dl_random)."""
    return SC.dl_random(yi, vi)


def dv_timing(dv):
    """Classify a coded contrast's timepoint: (pre, post, follow, spans_pre_post).

    Mirrors `Scripts/analysis_multiverse_meta.timing` so the two reported-effects entry
    points agree.  `timepoint_1`/`timepoint_2` and the comparison label are all consulted.
    """
    c1 = SC.classify_timepoint(dv.get("timepoint_1"))
    c2 = SC.classify_timepoint(dv.get("timepoint_2"))
    label = str(dv.get("comparison_label") or "").lower()
    pre = c1 == "pre" or c2 == "pre" or "baseline" in label or "pretest" in label
    post = c1 in ("post", "post_assumed") or c2 in ("post", "post_assumed")
    follow = c1 == "followup" or c2 == "followup" or "follow" in label
    spans = bool(pre and post)
    return bool(pre), bool(post), bool(follow), spans


def dv_bucket(dv):
    """Route one eligible contrast to exactly one question.

    Follow-up first (so a pre->follow-up change is persistence), then immediate
    pre->post changes / reconstructed DiD (`change` timepoints are pre->post changes),
    then the immediate posttest.  `pre` and `other` are never outcomes.
    """
    pre, post, follow, spans = dv_timing(dv)
    c1 = SC.classify_timepoint(dv.get("timepoint_1"))
    c2 = SC.classify_timepoint(dv.get("timepoint_2"))
    change = c1 == "change" or c2 == "change"
    if follow:
        return "followup"
    if change or spans:
        return "did"
    if post:
        return "posttest"
    if pre:
        return "pre"
    return "other"


def did_effect(dv, r=0.5):
    """(yi, vi, how) for the pretest-corrected DiD of one contrast, or (nan, nan, '').

    Preference: a DiD derived from the coded pre/post means (the paper's own data),
    else a coded pre->post change effect (the paper's reported DiD), else nothing.
    """
    d = SC._meta_did(dv, r=r)
    if d is not None and np.isfinite(d.get("effect", np.nan)) and d.get("var", 0) > 0:
        return d["effect"], d["var"], "did:derived"
    yi, vi = num(dv.get("yi")), num(dv.get("vi"))
    if np.isfinite(yi) and np.isfinite(vi) and vi > 0:
        return yi, vi, "did:reported"
    return np.nan, np.nan, ""


def main():
    SC.include_fully_within_cli()
    SC.announce_fully_within("metaanalysis")
    AL.reset("metaanalysis")
    study_rows, dv_rows = [], []
    fw_skipped = []
    omnibus_skipped = []
    for p in sorted(glob.glob(os.path.join(EXTRACT, "*.json"))):
        key = os.path.splitext(os.path.basename(p))[0]
        try:
            data = json.load(open(p, encoding="utf-8"))
        except Exception as e:
            print("skip", key, e)
            AL.log("extraction_skipped", study=key, error=f"{type(e).__name__}: {e}")
            continue
        data = data if isinstance(data, list) else [data]
        for o in data:
            if not isinstance(o, dict):
                continue
            # Non-independent experiment (same sample reported in another included source).
            excl = SE.is_excluded(key, o.get("Study_ID"))
            # Fully-within (single-arm, no-control) experiments are visible in the
            # export but excluded from every pooled model by default.
            fw = SC.is_fully_within(o)
            if fw and not SC.include_fully_within():
                fw_skipped.append({"study_key": key, "Study_ID": o.get("Study_ID")})
            study_rows.append({k: v for k, v in o.items() if k not in ("DV_Metrics", "Covariates")}
                              | {"study_key": key, "study_excluded": excl, "fully_within": fw})
            explicit_iv = SC.record_has_interventions(o)
            # Per-intervention model (PER_INTERVENTION_MODEL.md): for a record with
            # explicit ``Interventions`` also expose one effective study row per
            # intervention so moderators resolve per arm.
            if explicit_iv:
                for iv in SC.record_interventions(o):
                    eff = SC.effective_record(o, iv)
                    study_rows.append(
                        {k: v for k, v in eff.items() if k not in ("DV_Metrics", "Covariates")}
                        | {"study_key": key, "study_excluded": excl, "fully_within": fw})
            for iv, eff, i, dv in SC.effective_dvs_unique(o, key):
                if not isinstance(dv, dict):
                    continue
                _iid = (str(iv.get("Intervention_ID") or "").strip() if explicit_iv else "")
                yi, vi, how = effect(dv)
                # W7-STRUCT omnibus guard: the shared builder refuses a multi-df
                # omnibus / contrast_type==11 eligible row (NaN above); record it
                # durably and in the Omnibus_Guard sheet so the drop is auditable.
                if SC.is_omnibus_eligible(dv):
                    SC.log_omnibus_skip(key, o.get("Study_ID"), dv, where="metaanalysis")
                    omnibus_skipped.append({
                        "study_key": key, "Study_ID": o.get("Study_ID"),
                        "dv_name": dv.get("dv_name"), "dv_domain": dv.get("dv_domain"),
                        "contrast_type": dv.get("contrast_type"),
                        "df_num": dv.get("df_num"), "df_den": dv.get("df_den"),
                        "F_val": dv.get("F_val"), "chi2_val": dv.get("chi2_val"),
                        "eta2_p": dv.get("eta2_p"),
                        "missing_metrics_reason": dv.get("missing_metrics_reason")})
                # Controlled domain, resolving blank / off-vocabulary labels (task: domain restriction).
                dom = DR.resolve_domain(dv.get("dv_domain"), key, dv.get("dv_name"))
                # Timepoint routing: exactly one of the three questions, or a non-outcome
                # (`pre`/`other`).  Baselines are never outcomes.
                pre, post, follow, spans = dv_timing(dv)
                bucket = dv_bucket(dv)
                did_yi, did_vi, did_from = did_effect(dv) if bucket == "did" else (np.nan, np.nan, "")
                row = {
                    "study_key": key, "Study_ID": o.get("Study_ID"),
                    # Normalized experiment key so build_primary_composite's grouping key
                    # actually exists (the frame carried only Study_ID -> the key was
                    # silently dropped and composites paired across experiments).
                    "experiment_id": o.get("Study_ID"),
                    "contrast": i + 1,
                    "dv_name": dv.get("dv_name"), "dv_domain": dom,
                    "contrast_type": dv.get("contrast_type"), "eligible_primary": dv.get("eligible_primary"),
                    "comparison": dv.get("comparison_label"), "veracity": SC.dv_veracity(dv),
                    "dv_family": SC.dv_family(dv), "component_side": SC.dv_component_side(dv),
                    "cluster_id": SC.contrast_cluster_id(key, o.get("Study_ID"), dv),
                    "dv_sign": polarity_sign(dv),
                    "oriented": int(dv.get("dv_polarity") in (0, 1)),
                    "timepoint": dv.get("timepoint_1"), "timepoint_2": dv.get("timepoint_2"),
                    "bucket": bucket, "spans_pre_post": spans, "is_post": post, "is_followup": follow,
                    "yi": yi, "vi": vi, "effect_from": how,
                    "did_yi": did_yi, "did_vi": did_vi, "did_from": did_from,
                    "reported": dv.get("effect_size_type_reported"),
                    "missing_reason": dv.get("missing_metrics_reason"),
                    "study_excluded": excl,
                    "fully_within": fw}
                if _iid:
                    row["Intervention_ID"] = _iid
                    row["Intervention_Type"] = eff.get("Intervention_Type")
                dv_rows.append(row)
    studies = pd.DataFrame(study_rows)
    dvs = pd.DataFrame(dv_rows)
    # Non-independent experiments stay visible in DV_Metrics (flagged) but are removed
    # from the analysis set, so they cannot be double-counted in any pooled result.
    analysis_dvs = dvs[~dvs["study_excluded"]] if "study_excluded" in dvs.columns else dvs
    analysis_dvs = analysis_dvs.copy()
    # Fully-within (single-arm, no-control) records: excluded from every pooled
    # model by default; opt in with INCLUDE_FULLY_WITHIN=1 / --include-fully-within.
    n_fw_dropped = 0
    if "fully_within" in analysis_dvs.columns and not SC.include_fully_within():
        _fw = analysis_dvs["fully_within"].fillna(False).astype(bool)
        n_fw_dropped = int(_fw.sum())
        analysis_dvs = analysis_dvs[~_fw].copy()
    # Confidence/certainty DVs are a separate strand (metacognition): they are labelled
    # `confidence` and are never pooled -- not as a domain, not as `other`.  They stay visible in
    # the exported DV_Metrics sheet (labelled), just not in any pooled model.
    analysis_dvs = analysis_dvs[~analysis_dvs["dv_domain"].isin(DR.NON_OUTCOME_DOMAINS)].copy()
    # Route every eligible contrast to exactly one question.  posttest/followup use the
    # coded/reported effect; did uses the derived pre->post DiD (falling back to a coded
    # pre->post change).  `pre`/`other` rows are never outcomes.
    analysis_dvs["question"] = analysis_dvs["bucket"].map({
        "posttest": "posttest (immediate)",
        "followup": "follow-up (persistence)",
        "did": "DiD (pre->post)"})
    # Orientation: every DV is read "higher = more desirable" (AGENTS.md / harmonized
    # contract), so the coded effect is multiplied by the dv_polarity sign before any
    # pooling or composite construction.  The unoriented value stays in ``yi`` /
    # ``did_yi`` for audit; a null polarity leaves the sign at +1 and is flagged.
    analysis_dvs["q_yi"] = analysis_dvs["dv_sign"] * analysis_dvs["yi"]
    analysis_dvs["q_vi"] = analysis_dvs["vi"]
    is_did = analysis_dvs["question"] == "DiD (pre->post)"
    analysis_dvs.loc[is_did, "q_yi"] = (analysis_dvs.loc[is_did, "dv_sign"]
                                        * analysis_dvs.loc[is_did, "did_yi"])
    analysis_dvs.loc[is_did, "q_vi"] = analysis_dvs.loc[is_did, "did_vi"]
    # Per-contrast input sheet: every eligible contrast with a computable coded effect.
    effects = analysis_dvs[(analysis_dvs["eligible_primary"] == 1) & analysis_dvs["yi"].notna()
                           & analysis_dvs["vi"].notna()].copy()
    # The pooled analysis set: eligible, assigned to a question, with a usable effect.
    pool = analysis_dvs[(analysis_dvs["eligible_primary"] == 1)
                        & analysis_dvs["question"].notna()
                        & analysis_dvs["q_yi"].notna() & analysis_dvs["q_vi"].notna()
                        & (analysis_dvs["q_vi"] > 0)].copy()
    # Contradictory rows: an eligible contrast that carries BOTH a numeric effect and a
    # non-empty `missing_metrics_reason` is not poolable (the coder flagged the effect as
    # unavailable) -- excluded from pooling, still visible in the DV_Metrics sheet.
    missing_drop = pool[pool["missing_reason"].fillna("").astype(str).str.strip() != ""]
    pool = pool[pool["missing_reason"].fillna("").astype(str).str.strip() == ""]
    # DiD quality guard (same rule as the analysis suite): a standardised pre-post change
    # cannot plausibly exceed |3|; extreme values trace to coded SDs behaving like SEs.
    did_over = pool[(pool["question"] == "DiD (pre->post)") & (pool["q_yi"].abs() > DID_ABS_MAX)]
    pool = pool[~((pool["question"] == "DiD (pre->post)") & (pool["q_yi"].abs() > DID_ABS_MAX))]
    # Family rule (DEPENDENT_EFFECTS / Option C, PI-corrected): the primary pool is the
    # composite (derived from the coded separate_true/separate_false pair when both are
    # available, else the directly reported composite); every separate_* effect goes to
    # the secondary components strand and is never pooled with a composite.
    pool, components, fam_log, fam_stats = SC.build_primary_composite(
        pool, effect_col="q_yi", var_col="q_vi",
        group_cols=("study_key", "experiment_id", "dv_domain", "question"))
    SC.write_nonredundancy_log(fam_log)

    def pool_table(frame, scope):
        rows = []
        for q in QUESTIONS:
            sub = frame[frame["question"] == q]
            if not len(sub):
                continue
            rows.append({"question": q, "scope": scope, "group": "ALL",
                         **SC.pool_dependence(sub["q_yi"], sub["q_vi"],
                                              sub["cluster_id"], sub["study_key"])})
            for dom, g in sub.groupby("dv_domain"):
                if len(g):
                    rows.append({"question": q, "scope": scope, "group": f"domain: {dom}",
                                 **SC.pool_dependence(g["q_yi"], g["q_vi"],
                                                      g["cluster_id"], g["study_key"])})
        return rows

    res = pd.DataFrame(pool_table(pool, "unrestricted"))

    # Restricted model kept separate: misinformation-susceptibility domains only, per question.
    pool_r = pool[pool["dv_domain"].isin(DR.SUSCEPTIBILITY_DOMAINS)]
    res_restricted = pd.DataFrame(pool_table(pool_r, "restricted"))

    # Domain table: per question x controlled domain, how many contrasts exist / are
    # eligible / enter the pool, plus the DL comparison and the dependence-aware
    # (three-level / RVE) estimates and the largest-cluster weight share.
    EMPTY_DEP = dict(dl_k=0, dl_est=np.nan, dl_se=np.nan, dl_lo=np.nan, dl_hi=np.nan,
                     dl_tau2=np.nan, dl_i2=np.nan, three_est=np.nan, three_se=np.nan,
                     three_lo=np.nan, three_hi=np.nan,
                     rve_est=np.nan, rve_se=np.nan, rve_lo=np.nan, rve_hi=np.nan,
                     n_clusters=0, n_studies=0, max_weight_share_dl=np.nan,
                     max_weight_share_rve=np.nan)

    def _dep(g):
        if not len(g):
            return EMPTY_DEP
        return SC.pool_dependence(g["q_yi"], g["q_vi"], g["cluster_id"], g["study_key"])

    dom_rows = []
    for q in QUESTIONS:
        qall = analysis_dvs[analysis_dvs["question"] == q]
        qeff = pool[pool["question"] == q]
        for dom in DR.DV_DOMAINS:
            d_all = qall[qall["dv_domain"] == dom]
            d_elig = d_all[d_all["eligible_primary"] == 1]
            d_eff = qeff[qeff["dv_domain"] == dom]
            r = _dep(d_eff) if len(d_eff) else EMPTY_DEP
            dom_rows.append({
                "question": q, "dv_domain": dom,
                "restricted": int(dom in DR.SUSCEPTIBILITY_DOMAINS),
                "n_dv_contrasts": len(d_all), "n_eligible_primary": len(d_elig),
                "n_effects": len(d_eff), "n_studies": d_eff["study_key"].nunique(),
                "k": r["dl_k"], "est": r["dl_est"], "lo": r["dl_lo"], "hi": r["dl_hi"],
                "tau2": r["dl_tau2"], "i2": r["dl_i2"],
                "three_est": r["three_est"], "rve_est": r["rve_est"],
                "rve_lo": r["rve_lo"], "rve_hi": r["rve_hi"],
                "n_clusters": r["n_clusters"],
                "max_weight_share_dl": r["max_weight_share_dl"],
                "max_weight_share_rve": r["max_weight_share_rve"]})
        for label in ("ALL_DOMAINS", "RESTRICTED_SUSCEPTIBILITY"):
            sel = qeff if label == "ALL_DOMAINS" else qeff[
                qeff["dv_domain"].isin(DR.SUSCEPTIBILITY_DOMAINS)]
            r = _dep(sel) if len(sel) else EMPTY_DEP
            dom_rows.append({
                "question": q, "dv_domain": label,
                "restricted": int(label == "RESTRICTED_SUSCEPTIBILITY"),
                "n_dv_contrasts": len(qall), "n_eligible_primary": len(qall),
                "n_effects": len(sel), "n_studies": sel["study_key"].nunique() if len(sel) else 0,
                "k": r["dl_k"], "est": r["dl_est"], "lo": r["dl_lo"], "hi": r["dl_hi"],
                "tau2": r["dl_tau2"], "i2": r["dl_i2"],
                "three_est": r["three_est"], "rve_est": r["rve_est"],
                "rve_lo": r["rve_lo"], "rve_hi": r["rve_hi"],
                "n_clusters": r["n_clusters"],
                "max_weight_share_dl": r["max_weight_share_dl"],
                "max_weight_share_rve": r["max_weight_share_rve"]})
    domain_counts = pd.DataFrame(dom_rows)

    # ------------------------------------------------------------------
    # Canonical pooled models: reuse the analysis-suite derivation
    # (Scripts/analysis_multiverse_meta.py) so that metaanalysis.py and the
    # suite apply dv_polarity and derive the per-contrast effects IDENTICALLY
    # and therefore report the SAME pooled estimates on the shared specs.
    # ------------------------------------------------------------------
    import analysis_multiverse_meta as AM
    coded_am, _sk_am, _did_am, _miss_am = AM.load_contrasts(verbose=False)
    # AM.load_contrasts silently discards its diagnostics when verbose=False; the
    # canonical model would otherwise drop studies/contrasts with no durable trace.
    AL.log("am_contrasts_skipped", count=len(_sk_am), keys=sorted(_sk_am))
    AL.log("am_did_excluded", count=len(_did_am), records=_did_am)
    AL.log("am_missing_dropped", count=len(_miss_am), records=_miss_am)
    qmap = {"post_all": "posttest (immediate)",
            "persistence_all": "follow-up (persistence)",
            "did_all": "DiD (pre->post)"}

    def _am_prim(spec, restricted=False):
        d0 = AM.build_spec(coded_am, spec)
        if not len(d0):
            return d0
        d0 = AM.apply_exclusions_off(d0, spec)
        # Canonical family split, shared with the spec-table builder
        # (``analysis_multiverse_meta.primary_composite_frames`` /
        # ``write_meta_spec_table``): the experiment key is normalized inside so a
        # separate_true/separate_false pair is never matched across experiments.
        # W2-parity2: ``suite_common.build_meta_specs`` now derives the spec table
        # from this same function, so the workbook and the spec-based pool agree.
        prim = AM.primary_composite_frames(d0)[0]
        # Mirror the canonical selector (specs_api.select_spec_rows): the meta-DiD
        # quality guard |effect| <= DID_ABS_MAX applies to every did* spec, so the
        # workbook pools exactly the rows the spec-based app pool does.
        if spec.startswith("did") and len(prim):
            prim = prim[prim["spec_yi"].abs() <= DID_ABS_MAX]
        if restricted:
            prim = prim[prim["dv_domain"].isin(DR.SUSCEPTIBILITY_DOMAINS)]
        return prim

    def _am_table(restricted, scope):
        rows = []
        for spec, q in qmap.items():
            prim = _am_prim(spec, restricted)
            if not len(prim):
                continue
            rows.append({"question": q, "scope": scope, "group": "ALL",
                         **SC.pool_dependence(prim["spec_yi"], prim["spec_vi"],
                                              prim["cluster_id"], prim["study_key"])})
            for dom, g in prim.groupby("dv_domain"):
                if len(g):
                    rows.append({"question": q, "scope": scope, "group": f"domain: {dom}",
                                 **SC.pool_dependence(g["spec_yi"], g["spec_vi"],
                                                      g["cluster_id"], g["study_key"])})
        return pd.DataFrame(rows)

    res = _am_table(False, "unrestricted")
    res_restricted = _am_table(True, "restricted")
    dc_rows = []
    for spec, q in qmap.items():
        pa = _am_prim(spec, False)
        pr = (pa[pa["dv_domain"].isin(DR.SUSCEPTIBILITY_DOMAINS)] if len(pa) else pa)
        for label, sel in (("ALL_DOMAINS", pa), ("RESTRICTED_SUSCEPTIBILITY", pr)):
            dep = (SC.pool_dependence(sel["spec_yi"], sel["spec_vi"],
                                      sel["cluster_id"], sel["study_key"])
                   if len(sel) else EMPTY_DEP)
            dc_rows.append({"question": q, "dv_domain": label,
                            "restricted": int(label == "RESTRICTED_SUSCEPTIBILITY"),
                            "n_dv_contrasts": len(pa), "n_eligible_primary": len(pa),
                            "n_effects": len(sel),
                            "n_studies": sel["study_key"].nunique() if len(sel) else 0,
                            "k": dep["dl_k"], "est": dep["dl_est"], "lo": dep["dl_lo"],
                            "hi": dep["dl_hi"], "tau2": dep["dl_tau2"], "i2": dep["dl_i2"],
                            "est_multilevel": dep["three_est"], "lo_multilevel": dep["three_lo"],
                            "hi_multilevel": dep["three_hi"],
                            "est_rve": dep["rve_est"], "lo_rve": dep["rve_lo"],
                            "hi_rve": dep["rve_hi"],
                            "three_est": dep["three_est"], "rve_est": dep["rve_est"],
                            "rve_lo": dep["rve_lo"], "rve_hi": dep["rve_hi"],
                            "n_clusters": dep["n_clusters"],
                            "max_weight_share_dl": dep["max_weight_share_dl"],
                            "max_weight_share_rve": dep["max_weight_share_rve"]})
    domain_counts = pd.DataFrame(dc_rows)
    excluded_tbl = pd.DataFrame(SE.table())

    def safe(df):
        d = df.copy()
        for c in d.columns:
            d[c] = d[c].map(lambda v: v if (v is None or isinstance(v, (str, int, float, bool)))
                            else json.dumps(v, ensure_ascii=False))
        # 2026-09-29: openpyxl REFUSES control characters, and codings inherit them from
        # PDF text layers (the same U+0001/U+0002/U+0003 glyph family that eats minus signs
        # and stars in Text/*.txt).  A single `missing_metrics_reason` carrying one crashed
        # the whole meta workbook mid-write, which is why an earlier run left
        # Results/metaanalysis.xlsx with only its first two sheets.  Sanitise every string
        # cell (keep \t \n \r) so one bad character can never destroy a product.
        _CTRL = dict.fromkeys(list(range(0x00, 0x20)) + [0x7f], None)
        for _c in ("\t", "\n", "\r"):
            _CTRL.pop(ord(_c), None)
        for c in d.columns:
            d[c] = d[c].map(lambda v: v.translate(_CTRL) if isinstance(v, str) else v)
        d.columns = [str(c).translate(_CTRL) for c in d.columns]
        return d
    with pd.ExcelWriter(OUT, engine="openpyxl") as xl:
        safe(studies).to_excel(xl, sheet_name="Study_Level", index=False)
        safe(dvs).to_excel(xl, sheet_name="DV_Metrics", index=False)
        safe(effects).to_excel(xl, sheet_name="Effects", index=False)
        safe(res).to_excel(xl, sheet_name="MetaResults", index=False)
        safe(res_restricted).to_excel(xl, sheet_name="MetaResults_restricted", index=False)
        safe(domain_counts).to_excel(xl, sheet_name="Domain_Counts", index=False)
        safe(components).to_excel(xl, sheet_name="Components_Strand", index=False)
        safe(pd.DataFrame([fam_stats])).to_excel(xl, sheet_name="Family_Stats", index=False)
        safe(pd.DataFrame(omnibus_skipped)).to_excel(xl, sheet_name="Omnibus_Guard", index=False)
        if len(excluded_tbl):
            safe(excluded_tbl).to_excel(xl, sheet_name="Excluded_Experiments", index=False)
    print(f"studies: {len(studies)} | dv rows: {len(dvs)} | analysis dv rows: {len(analysis_dvs)} "
          f"| excluded experiments: {len(excluded_tbl)} | eligible contrasts with effects: {len(effects)} "
          f"| DiD guard dropped: {len(did_over)} | missing_metrics_reason dropped: {len(missing_drop)}")
    print(SC.fully_within_report(len(fw_skipped), "record(s)")
          + f"; {n_fw_dropped} analysis DV row(s) dropped")
    print(f"omnibus guard: {len(omnibus_skipped)} eligible contrast row(s) refused "
          f"(multi-df omnibus / contrast_type=11); see sheet 'Omnibus_Guard' and "
          f"Results/analysis_log.jsonl kind=omnibus_eligible_skipped")
    try:
        import analysis_multiverse_meta as _AM
        _ns = (pd.DataFrame(_AM.STD_DROPPED).drop_duplicates(
            subset=["study_key", "Study_ID", "dv_name", "dv_domain"])
            if _AM.STD_DROPPED else pd.DataFrame())
        print(f"canonical guards: confidence/metacognition routed out: "
              f"{len(_AM.METACOG_DROPPED)} | non-standardised contrasts dropped: {len(_ns)} "
              f"| omnibus-eligible refused from spec pool: {len(_AM.OMNIBUS_SKIPPED)}")
    except Exception as e:
        # Never swallow the failure silently: the canonical-guard counts are part of
        # the run's audit, so record why they are missing.
        AL.log("am_import_failed", error=f"{type(e).__name__}: {e}")
        print(f"  ! canonical guard diagnostics unavailable: {type(e).__name__}: {e}")
    if len(res):
        print(res[res["group"] == "ALL"].to_string(index=False))
    if len(res_restricted):
        print(res_restricted.to_string(index=False))
    print("wrote", OUT)


if __name__ == "__main__":
    sys.exit(main())
