"""Read-only validation of the campaign coding JSONs (``Extraction/*.json``).

Writes two products and prints a summary; it never modifies an Extraction file.

  * ``Results/extraction_validation.csv`` — one row per problem
    (``key, experiment_id, dv_name, problem_code, detail``).
  * ``Results/extraction_needs_coding.csv`` — the human work queue: one row per
    problem that needs paper-level coding (``key, experiment_id, dv_name, field,
    current_value, suggested_source, priority``), sorted by priority then key.
    Values are NEVER guessed here -- the queue only points at the source.

Problem codes
-------------
METRIC_MISSING_NO_REASON
    A ``DV_Metrics`` row with neither ``yi`` nor ``vi`` and an empty
    ``missing_metrics_reason`` (violates the "no invented numbers" rule: every
    effect must be sourced or explicitly explained).
ELIGIBLE_NO_METRIC_NO_REASON
    The ``eligible_primary == 1`` subset of the above (a primary outcome
    silently absent from the meta-analysis).
YI_XOR_VI
    Exactly one of ``yi``/``vi`` present and no ``missing_metrics_reason``.
NO_N_ANALYZED
    An experiment object without ``N_Analyzed``.
NO_N_RECRUITED
    An experiment object without ``N_Recruited``.
NO_ELIGIBLE_DV
    A file with zero ``eligible_primary == 1`` DVs.
UNRECOGNISED_TIMEPOINT
    An ``eligible_primary == 1`` contrast whose coded timing does not resolve to
    a canonical post / follow-up / pre->post span bucket.  The canonical set is
    pre, post, follow-up(followup) and change/did, plus the coded vocabulary
    actually used (the audit's timing test -- see ``_timing``).  Labels that a
    canonical classification routes to ``other`` are also reported.
NO_TITLE
    An experiment object without ``Title``.
DUPLICATE_DV_NAME
    Two or more DV rows inside one experiment with the same contrast identity
    (``dv_name`` + ``comparison_label`` + ``contrast_type`` + both timepoints +
    group labels).  A repeated ``dv_name`` with a distinct contrast label is
    legitimate (multiple contrasts on one measure) and is NOT flagged.
MISSING_NOTE_MD
    A JSON without a sibling ``Extraction/<Key>.md`` coding note.
MISSING_SOURCE_LOCATION
    A ``DV_Metrics`` row with an empty ``source_location`` (schema v3.1, D8:
    every coded variable should record where its statistic lives in the paper).
    Report-only — it never blocks the pipeline.
MISSING_SOURCE_QUOTE
    A ``DV_Metrics`` row with an empty ``source_quote`` (the verbatim statistic
    from the paper).  Report-only — it never blocks the pipeline.
SOURCE_QUOTE_NOT_FOUND
    A non-empty ``source_quote`` that cannot be re-found in the study's
    ``Text/<Key>.txt`` after whitespace/punctuation normalisation.  This is the
    mechanical false-positive check on the provenance fields; it never blocks.
OMNIBUS_ONLY_ELIGIBLE
    An ``eligible_primary == 1`` contrast whose only coded evidence is a
    multi-df F/chi2/eta2 omnibus (``df_num > 1``): no per-condition
    descriptives, no reported pairwise d/g, no usable pairwise statistic.  The
    pipeline refuses to derive an intervention effect from it.
OMNIBUS_AS_ELIGIBLE
    An ``eligible_primary == 1`` contrast with ``contrast_type == 11``
    (``omnibus_model_term``): a model-level term, never a two-arm contrast.
OMNIBUS_MISSING_REVIEW
    No metric coded at all AND ``missing_metrics_reason`` admits an omnibus /
    main-effect / only-overall / no-pairwise report.  A reviewer must re-search
    the same paragraph and the referenced table for a pairwise statistic or
    condition M/SD before accepting the reason.
SINGLE_DF_OMNIBUS_VERIFY
    An ``eligible_primary == 1`` contrast carrying only a single-df
    (``df_num <= 1``) omnibus/simple-effect F/eta2 (or a single-df
    ``contrast_type == 11`` term).  It may be a valid 2-group contrast but the
    sign/direction cannot be taken on faith and needs a look.

Usage::

    python Scripts/validate_extraction.py            # strict (exit 1 on the two metric codes)
    python Scripts/validate_extraction.py --lenient  # always exit 0
"""
import argparse
import csv
import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTRACT = os.path.join(ROOT, "Extraction")
TEXT = os.path.join(ROOT, "Text")
RESULTS = os.path.join(ROOT, "Results")
OUT_VALIDATION = os.path.join(RESULTS, "extraction_validation.csv")
OUT_NEEDS = os.path.join(RESULTS, "extraction_needs_coding.csv")

VALIDATION_COLS = ["key", "experiment_id", "dv_name", "problem_code", "detail"]
NEEDS_COLS = ["key", "experiment_id", "dv_name", "field", "current_value",
              "suggested_source", "priority"]

# Codes that block the pipeline (exit 1 unless --lenient).
BLOCKING = ("METRIC_MISSING_NO_REASON", "ELIGIBLE_NO_METRIC_NO_REASON")


def blank(v):
    return v is None or (isinstance(v, str) and v.strip() == "")


def as_num(v):
    try:
        if blank(v):
            return None
        f = float(v)
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def norm_text(s):
    """Whitespace/punctuation normalisation shared with fill_source_locations.

    A stored ``source_quote`` is written as a slice of the normalised Text, so
    the same transform must be applied on both sides of the findability check.
    """
    if not s:
        return ""
    s = str(s)
    s = s.replace("\u00ad", "")
    for a, b in [("\ufb01", "fi"), ("\ufb02", "fl"), ("\ufb00", "ff"),
                 ("\ufb03", "ffi"), ("\ufb04", "ffl"),
                 ("\u2013", "-"), ("\u2014", "-"), ("\u2212", "-"),
                 ("\u2010", "-"), ("\u2011", "-"),
                 ("\u2018", "'"), ("\u2019", "'"), ("\u201c", '"'),
                 ("\u201d", '"'), ("\u00a0", " "), ("\u2026", "...")]:
        s = s.replace(a, b)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def timing(t1, t2):
    """Reproduce the audit's timing test (see temp/audit/extraction_audit.py).

    Returns ``(pre, post, follow, span)``.  ``pre``/``post``/``follow`` are the
    resolved canonical buckets; ``span`` is a pre->post/follow-up change.
    """
    a = (t1 or "").lower()
    b = (t2 or "").lower()
    blob = a + "|" + b
    pre = ("pre" in a or "baseline" in a)
    post = ("post" in a or "post" in b or "post_assumed" in blob)
    follow = ("follow" in blob or blob.startswith("fu") or "wave" in blob
              or "month" in blob or "week" in blob or "delay" in blob)
    span = (("pre" in a or "baseline" in a)
            and ("post" in b or "follow" in b or bool(b)))
    return pre, post, follow, span


def bucket_other(raw):
    """True when a non-blank label cannot be routed to a canonical bucket.

    Mirrors ``suite_common.classify_timepoint``'s canonical vocabulary
    (pre/post/follow-up/change) without importing the analysis layer.
    """
    if blank(raw):
        return False
    s = str(raw).strip().lower()
    exact_post = {"post", "posttest", "post_test", "immediate", "endline", "posttreatment",
                  "post_intervention", "first_endline_immediate", "t1_immediate",
                  "time 1 (immediate)", "post_attack", "post_decision", "post_training"}
    exact_pre = {"pre", "cross_sectional", "cross-sectional"}
    exact_follow = {"followup", "follow-up", "follow_up", "time 2 (7-day)"}
    exact_change = {"change", "did"}
    tlabels = {"T0": "pre", "T1": "pre", "T2": "post", "T3": "followup", "T4": "followup",
               "t0": "pre", "t1": "pre", "t2": "post", "t3": "post", "t4": "post"}
    if raw in tlabels:
        return False
    if s in exact_post or s in exact_pre or s in exact_follow or s in exact_change:
        return False
    if "follow" in s or s.startswith("fu") or "wave" in s:
        return False
    if ("pre" in s or "baseline" in s or "pretreat" in s or "before" in s):
        return False
    if "change" in s or "minus" in s:
        return False
    if "post" in s:
        return False
    return True


# ---------------------------------------------------------------------------
# Omnibus / multi-df eligible-contrast detection (W7-STRUCT)
# ---------------------------------------------------------------------------
# The systematic extraction defect: a multi-df omnibus F/chi2 (or a
# ``contrast_type == 11`` omnibus model term) was coded as the
# ``eligible_primary == 1`` effect for an intervention-vs-control contrast, even
# though the same paragraph/table reported pairwise-vs-control comparisons or
# per-condition descriptives.  The pipeline's effect builder now REFUSES to
# derive an effect from such a row (``suite_common.meta_effect`` /
# ``metaanalysis.effect``); these four problem codes surface the rows that need
# re-coding.  The predicate mirrors the pipeline guard exactly so the work queue
# and the guard can never disagree.

#: Fields carrying a directly coded effect/statistic (a "metric").
EFFECT_METRIC_FIELDS = ("yi", "vi", "d_val", "g_val", "t_val", "r_val", "z_val",
                        "beta_val", "odds_ratio")
#: Fields carrying the omnibus test statistic.
OMNIBUS_METRIC_FIELDS = ("F_val", "chi2_val", "eta2_p")

#: ``missing_metrics_reason`` wording that admits an omnibus / main-effect-only report.
OMNIBUS_REASON_RE = re.compile(
    r"omnibus|main[-\s]?effect|only[-\s]?overall|no[-\s]?pairwise|overall model|"
    r"only the overall|no per[-\s]?contrast|not convertible|model[-\s]?term",
    re.I)

#: ``effect_size_type_reported`` values that denote a *reported pairwise*
#: standardised effect (so the row is NOT an omnibus-only row even when an
#: ancillary multi-df F is also recorded).
_PAIRWISE_EST_EXACT = {"d", "g", "d_val", "g_val", "cohen_d", "cohens_d", "hedges_g"}
_PAIRWISE_EST_TOKENS = ("cohen", "hedges", "smd", "standardis", "standardiz")


def has_descriptives(dv):
    """True when the DV carries usable per-condition means + SDs + arm n's.

    A single stray ``n1i`` (arm flow) does NOT count: the pipeline derives a
    means/SD effect only from the complete ``m/sd`` pair for both arms.  A
    non-empty ``cell_descriptives`` list also counts as a pairwise source.
    """
    if dv.get("cell_descriptives"):
        return True
    for pre, post in (("m1i", "m1i_post"), ("sd1i", "sd1i_post"),
                      ("m2i", "m2i_post"), ("sd2i", "sd2i_post")):
        if blank(dv.get(pre)) and blank(dv.get(post)):
            return False
    if blank(dv.get("n1i")) and blank(dv.get("n1i_post")):
        return False
    if blank(dv.get("n2i")) and blank(dv.get("n2i_post")):
        return False
    return True


def is_omnibus_style_effect(dv):
    """True when the coded ``effect_size_type_reported`` is omnibus-style (not d/g/t/r)."""
    est = str(dv.get("effect_size_type_reported") or "").strip().lower()
    if any(t in est for t in _PAIRWISE_EST_TOKENS):
        return False
    return est not in _PAIRWISE_EST_EXACT


def has_multidf_omnibus(dv):
    """True when a multi-df F/chi2/eta2 omnibus statistic is coded."""
    dfn = as_num(dv.get("df_num"))
    if dfn is None or dfn <= 1:
        return False
    return any(not blank(dv.get(k)) for k in OMNIBUS_METRIC_FIELDS)


def is_omnibus_eligible(dv):
    """The pipeline guard predicate (mirrored from ``suite_common``).

    True for an ``eligible_primary == 1`` row whose only usable evidence is a
    multi-df omnibus (or which is a ``contrast_type == 11`` model term), and
    which has no directly reported pairwise d/g and no per-condition
    descriptives.  Such a row must not be pooled as an intervention contrast.
    """
    if dv.get("eligible_primary") != 1:
        return False
    if str(dv.get("contrast_type") or "").strip() == "11":
        return True
    if not has_multidf_omnibus(dv):
        return False
    if has_descriptives(dv):
        return False
    if not is_omnibus_style_effect(dv):
        return False
    return True


def suggest_source(key, note_exists):
    return "Extraction/%s.md" % key if note_exists else "paper Text"


class Collector:
    def __init__(self):
        self.validation = []
        self.needs = []
        self._needs_seen = set()

    def problem(self, key, exp_id, dv_name, code, detail):
        self.validation.append({
            "key": key, "experiment_id": exp_id or "", "dv_name": dv_name or "",
            "problem_code": code, "detail": detail})

    def need(self, key, exp_id, dv_name, field, current_value, source, priority):
        sig = (key, exp_id or "", dv_name or "", field)
        if sig in self._needs_seen:
            return
        self._needs_seen.add(sig)
        self.needs.append({
            "key": key, "experiment_id": exp_id or "", "dv_name": dv_name or "",
            "field": field, "current_value": current_value,
            "suggested_source": source, "priority": priority})


def scan():
    c = Collector()
    counts = Counter()
    n_files = n_exps = n_dvs = n_elig = 0
    fixable_vi = 0  # vi derivable from a reported SE (reported for the apply step)
    n_loc = n_quote = 0

    files = sorted(glob.glob(os.path.join(EXTRACT, "*.json")))
    for path in files:
        key = os.path.splitext(os.path.basename(path))[0]
        n_files += 1
        note_exists = os.path.exists(os.path.join(EXTRACT, key + ".md"))
        source = suggest_source(key, note_exists)
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            c.problem(key, "", "", "JSON_PARSE_ERROR", str(e))
            counts["JSON_PARSE_ERROR"] += 1
            continue
        if not isinstance(data, list):
            data = [data]

        # Normalised Text for the SOURCE_QUOTE_NOT_FOUND findability check.
        text_norm = None
        text_path = os.path.join(TEXT, key + ".txt")
        if os.path.exists(text_path):
            try:
                text_norm = norm_text(
                    open(text_path, encoding="utf-8", errors="replace").read())
            except OSError:
                text_norm = None

        file_has_elig = False
        for o in data:
            if not isinstance(o, dict):
                continue
            n_exps += 1
            exp_id = o.get("Study_ID") or ""
            if blank(o.get("Title")):
                c.problem(key, exp_id, "", "NO_TITLE", "experiment object has no Title")
                counts["NO_TITLE"] += 1
                c.need(key, exp_id, "", "Title", "", source, 1)
            if blank(o.get("N_Analyzed")):
                c.problem(key, exp_id, "", "NO_N_ANALYZED", "N_Analyzed is empty")
                counts["NO_N_ANALYZED"] += 1
            if blank(o.get("N_Recruited")):
                c.problem(key, exp_id, "", "NO_N_RECRUITED", "N_Recruited is empty")
                counts["NO_N_RECRUITED"] += 1

            dvs = o.get("DV_Metrics") or []
            exp_has_elig = any(isinstance(d, dict) and d.get("eligible_primary") == 1
                               for d in dvs)
            if exp_has_elig:
                file_has_elig = True
            prio = 0 if exp_has_elig else 1

            # Experiment-level needs (priority depends on eligibility).
            if blank(o.get("N_Analyzed")):
                c.need(key, exp_id, "", "N_Analyzed", "", source, prio)
            if blank(o.get("N_Recruited")):
                c.need(key, exp_id, "", "N_Recruited", "", source, prio)

            # Duplicate contrast identity within the experiment.
            groups = defaultdict(list)
            for dv in dvs:
                if not isinstance(dv, dict):
                    continue
                sig = (str(dv.get("dv_name") or "").strip(),
                       str(dv.get("comparison_label") or "").strip(),
                       str(dv.get("contrast_type")), str(dv.get("timepoint_1")),
                       str(dv.get("timepoint_2")), str(dv.get("group1_label") or ""),
                       str(dv.get("group2_label") or ""))
                groups[sig].append(dv)
            for sig, members in groups.items():
                if len(members) > 1:
                    for dv in members:
                        name = dv.get("dv_name") or ""
                        detail = ("duplicate contrast identity x%d: %r" % (len(members), name))
                        c.problem(key, exp_id, name, "DUPLICATE_DV_NAME", detail)
                        counts["DUPLICATE_DV_NAME"] += 1
                    first = members[0]
                    dp = 0 if first.get("eligible_primary") == 1 else 1
                    c.need(key, exp_id, first.get("dv_name") or "", "dv_name",
                           "%s x%d (identical contrast identity)" % (first.get("dv_name") or "", len(members)),
                           source, dp)

            for dv in dvs:
                if not isinstance(dv, dict):
                    continue
                n_dvs += 1
                name = dv.get("dv_name") or ""
                yi, vi, reason = dv.get("yi"), dv.get("vi"), dv.get("missing_metrics_reason")
                has_yi, has_vi = not blank(yi), not blank(vi)
                elig = dv.get("eligible_primary") == 1
                dp = 0 if elig else 1
                if elig:
                    n_elig += 1

                # Schema v3.1 (D8) provenance fields — report-only.
                loc = dv.get("source_location")
                quote = dv.get("source_quote")
                if blank(loc):
                    c.problem(key, exp_id, name, "MISSING_SOURCE_LOCATION",
                              "source_location is empty")
                    counts["MISSING_SOURCE_LOCATION"] += 1
                else:
                    n_loc += 1
                if blank(quote):
                    c.problem(key, exp_id, name, "MISSING_SOURCE_QUOTE",
                              "source_quote is empty")
                    counts["MISSING_SOURCE_QUOTE"] += 1
                else:
                    n_quote += 1
                    if text_norm is not None and norm_text(quote) not in text_norm:
                        c.problem(key, exp_id, name, "SOURCE_QUOTE_NOT_FOUND",
                                  "source_quote not found in Text/%s.txt after "
                                  "normalisation" % key)
                        counts["SOURCE_QUOTE_NOT_FOUND"] += 1

                if not has_yi and not has_vi and blank(reason):
                    detail = "yi and vi empty and missing_metrics_reason is empty"
                    c.problem(key, exp_id, name, "METRIC_MISSING_NO_REASON", detail)
                    counts["METRIC_MISSING_NO_REASON"] += 1
                    c.need(key, exp_id, name, "missing_metrics_reason", "", source, dp)
                    if elig:
                        c.problem(key, exp_id, name, "ELIGIBLE_NO_METRIC_NO_REASON", detail)
                        counts["ELIGIBLE_NO_METRIC_NO_REASON"] += 1
                elif has_yi != has_vi and blank(reason):
                    miss = "vi" if has_vi is False else "yi"
                    other = "yi" if miss == "vi" else "vi"
                    detail = "%s present, %s empty, missing_metrics_reason empty" % (
                        other, miss)
                    c.problem(key, exp_id, name, "YI_XOR_VI", detail)
                    counts["YI_XOR_VI"] += 1
                    cur = "yi=%s, vi=%s" % (yi, vi)
                    c.need(key, exp_id, name, miss, cur, source, dp)
                    # Count the arithmetic-autofixable subset (reported for the apply step).
                    if miss == "vi":
                        se = dv.get("sei")
                        if as_num(se) is not None and as_num(se) > 0:
                            fixable_vi += 1
                        elif (as_num(dv.get("d_se")) or 0) > 0 or (as_num(dv.get("g_se")) or 0) > 0:
                            fixable_vi += 1

                # --- W7-STRUCT: omnibus / multi-df eligible-contrast detection ----
                # Mirrors the pipeline guard (suite_common.is_omnibus_eligible):
                # a multi-df omnibus F/chi2 (or a contrast_type == 11 model term)
                # must never be the pooled intervention-vs-control effect.
                ct = str(dv.get("contrast_type") or "").strip()
                dfn = as_num(dv.get("df_num"))
                is_ct11 = ct == "11"
                subject = (any(not blank(dv.get(k)) for k in EFFECT_METRIC_FIELDS)
                           or any(not blank(dv.get(k)) for k in OMNIBUS_METRIC_FIELDS)
                           or has_descriptives(dv))
                if elig and is_ct11:
                    detail = "eligible_primary=1 with contrast_type=11 (omnibus_model_term)"
                    c.problem(key, exp_id, name, "OMNIBUS_AS_ELIGIBLE", detail)
                    counts["OMNIBUS_AS_ELIGIBLE"] += 1
                    c.need(key, exp_id, name, "contrast_type",
                           "11 (omnibus_model_term)", source, 0)
                if elig and is_omnibus_eligible(dv) and not is_ct11:
                    cur = "F=%s, chi2=%s, eta2_p=%s, df_num=%s" % (
                        dv.get("F_val"), dv.get("chi2_val"), dv.get("eta2_p"), dv.get("df_num"))
                    detail = "eligible_primary=1; only a multi-df omnibus is coded (%s)" % cur
                    c.problem(key, exp_id, name, "OMNIBUS_ONLY_ELIGIBLE", detail)
                    counts["OMNIBUS_ONLY_ELIGIBLE"] += 1
                    c.need(key, exp_id, name, "omnibus_metric", cur, source, 0)
                single_df_omnibus = (
                    elig and dfn is not None and dfn <= 1
                    and (is_ct11 or (any(not blank(dv.get(k)) for k in OMNIBUS_METRIC_FIELDS)
                                     and not has_descriptives(dv)
                                     and is_omnibus_style_effect(dv))))
                if single_df_omnibus:
                    cur = "contrast_type=%s, df_num=%s, F=%s, chi2=%s, eta2_p=%s" % (
                        ct or "", dv.get("df_num"), dv.get("F_val"),
                        dv.get("chi2_val"), dv.get("eta2_p"))
                    c.problem(key, exp_id, name, "SINGLE_DF_OMNIBUS_VERIFY",
                              "single-df omnibus/simple-effect needs a direction check (%s)" % cur)
                    counts["SINGLE_DF_OMNIBUS_VERIFY"] += 1
                    c.need(key, exp_id, name, "single_df_omnibus", cur, source, 0)
                if not subject and OMNIBUS_REASON_RE.search(str(reason or "")):
                    detail = "no metric coded; missing_metrics_reason blocks on an omnibus: %s" % (
                        str(reason).strip()[:160])
                    c.problem(key, exp_id, name, "OMNIBUS_MISSING_REVIEW", detail)
                    counts["OMNIBUS_MISSING_REVIEW"] += 1
                    c.need(key, exp_id, name, "omnibus_missing_reason",
                           str(reason).strip()[:160], source, dp)

                # Timepoint routing for eligible contrasts.
                if elig:
                    t1, t2 = dv.get("timepoint_1"), dv.get("timepoint_2")
                    pre, post, follow, span = timing(t1, t2)
                    other_slots = [s for s, lab in (("timepoint_1", t1), ("timepoint_2", t2))
                                   if bucket_other(lab)]
                    if not (post or follow or span) or other_slots:
                        why = []
                        if not (post or follow or span):
                            why.append("no canonical post/follow-up/pre->post timing")
                        if other_slots:
                            why.append("label(s) not in canonical vocabulary: " + ",".join(other_slots))
                        detail = "timepoint_1=%r, timepoint_2=%r (%s)" % (t1, t2, "; ".join(why))
                        c.problem(key, exp_id, name, "UNRECOGNISED_TIMEPOINT", detail)
                        counts["UNRECOGNISED_TIMEPOINT"] += 1
                        c.need(key, exp_id, name, "timepoint",
                               "timepoint_1=%s, timepoint_2=%s" % (t1, t2), source, 0)

        if not file_has_elig:
            c.problem(key, "", "", "NO_ELIGIBLE_DV", "no eligible_primary == 1 DV in file")
            counts["NO_ELIGIBLE_DV"] += 1
            c.need(key, "", "", "eligible_primary", "no eligible_primary=1 DV", source, 0)

        if not note_exists:
            c.problem(key, "", "", "MISSING_NOTE_MD", "no sibling Extraction/%s.md" % key)
            counts["MISSING_NOTE_MD"] += 1
            c.need(key, "", "", "note_md", "", "paper Text", 0 if file_has_elig else 1)

    meta = {"n_files": n_files, "n_experiments": n_exps, "n_dv_rows": n_dvs,
            "n_eligible": n_elig, "fixable_vi": fixable_vi,
            "n_loc": n_loc, "n_quote": n_quote}
    return c, counts, meta


def write_csv(path, cols, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lenient", action="store_true",
                    help="always exit 0 (do not fail on the two metric codes)")
    args = ap.parse_args()

    c, counts, meta = scan()
    c.validation.sort(key=lambda r: (r["key"], r["experiment_id"], r["problem_code"], r["dv_name"]))
    c.needs.sort(key=lambda r: (r["priority"], r["key"], r["experiment_id"], r["field"], r["dv_name"]))
    write_csv(OUT_VALIDATION, VALIDATION_COLS, c.validation)
    write_csv(OUT_NEEDS, NEEDS_COLS, c.needs)

    print("== extraction validation ==")
    print("files: %d | experiments: %d | DV rows: %d | eligible_primary=1: %d"
          % (meta["n_files"], meta["n_experiments"], meta["n_dv_rows"], meta["n_eligible"]))
    print("provenance (schema v3.1): source_location filled %d/%d | "
          "source_quote filled %d/%d"
          % (meta["n_loc"], meta["n_dv_rows"], meta["n_quote"], meta["n_dv_rows"]))
    print("vi derivable from a reported SE (arithmetic autofix candidate): %d" % meta["fixable_vi"])
    print("problems: %d" % len(c.validation))
    for code in ("METRIC_MISSING_NO_REASON", "ELIGIBLE_NO_METRIC_NO_REASON", "YI_XOR_VI",
                 "NO_N_ANALYZED", "NO_N_RECRUITED", "NO_ELIGIBLE_DV",
                 "UNRECOGNISED_TIMEPOINT", "NO_TITLE", "DUPLICATE_DV_NAME",
                 "MISSING_NOTE_MD", "MISSING_SOURCE_LOCATION",
                 "MISSING_SOURCE_QUOTE", "SOURCE_QUOTE_NOT_FOUND",
                 "OMNIBUS_ONLY_ELIGIBLE", "OMNIBUS_AS_ELIGIBLE",
                 "OMNIBUS_MISSING_REVIEW", "SINGLE_DF_OMNIBUS_VERIFY",
                 "JSON_PARSE_ERROR"):
        if counts.get(code):
            print("  %-32s %d" % (code, counts[code]))
    # W7-STRUCT: always surface the omnibus guard codes (0 is a valid, meaningful count).
    print("omnibus guard (multi-df / contrast_type=11) coding queue:")
    for code in ("OMNIBUS_ONLY_ELIGIBLE", "OMNIBUS_AS_ELIGIBLE",
                 "OMNIBUS_MISSING_REVIEW", "SINGLE_DF_OMNIBUS_VERIFY"):
        print("  %-32s %d" % (code, counts.get(code, 0)))
    print("wrote %s (%d rows)" % (os.path.relpath(OUT_VALIDATION, ROOT), len(c.validation)))
    print("wrote %s (%d rows)" % (os.path.relpath(OUT_NEEDS, ROOT), len(c.needs)))

    blocking = sum(counts.get(code, 0) for code in BLOCKING)
    if blocking and not args.lenient:
        print("\nFAIL: %d metric-reason problem row(s) (use --lenient to ignore)." % blocking)
        return 1
    print("\nOK (no blocking metric-reason problems).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
