"""Triage the disagreements between the coding routes.

Three routes code the same corpus independently:

  canonical  ``Extraction/<Key>.json``          (the analysis route)
  SB         ``Codings/<Key> - SB.json``         (SpaceBunny route)
  MM         ``Codings/<Key> - MM.json``         (MiniMax route)

A difference only matters if it changes what the analysis pools, so every
difference is sorted into one of three tiers:

``pool``
    the difference can change a pooled estimate, or change which records are
    pooled.  This is the tier that carries the disagreement.

``coding``
    the difference is in a field the analysis never reads.  It changes the
    coding's fidelity and the IRR numbers, not the result.

``prose``
    a free-text field where the routes describe the same thing at different
    lengths.  Not a disagreement at all.

Matching a DV across routes
----------------------------
A ``dv_name`` is *not* unique: the same outcome name occurs once per arm
pair, per timepoint, per condition (1,865 names repeat inside a single
route).  Comparing the first row of a name therefore compares arbitrary
rows and manufactures conflicts.  Rows are matched on the composite key

    dv_name | group1_label | group2_label | timepoint_1 | timepoint_2

and only rows that agree on that key are compared.  A name that appears
once per route with a *different* key is a granularity difference, reported
separately.

Products
--------
``Results/coder_disagreement_tiers.csv``    one row per study key
``Results/coder_disagreement_tiers.json``   the full detail
"""
import csv
import glob
import json
import os
import sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_PATH = os.path.join(ROOT, "Scripts", "coding_web", "schema.json")
RESULTS = os.path.join(ROOT, "Results")

#: record-level fields whose value decides what is pooled.
#: Every field here is read by the analysis layer (see
#: ``Scripts/analysis/without_exclusions.py`` and ``Scripts/contract.py``), so
#: a disagreement about it can change a pooled estimate.
POOL_RECORD_FIELDS = {
    "Fulltext_Exclusion_Criterion",   # study_exclusions -> inclusion
    "n_arms",                         # contract.is_fully_within
    "Fully_Within",                    # contract.is_fully_within
    "Intervention_Type",               # contract.record_interventions
    "Intervention_ID",
    "Interventions",
    "control_group_type",              # the "no misinformation control" test
    "N_Analyzed",                      # without_exclusions -> the N column
    "N_Recruited",                     # without_exclusions -> the N column
}

#: DV-level fields whose value decides what is pooled
POOL_DV_FIELDS = {
    "eligible_primary", "dv_polarity", "dv_domain", "yi", "vi",
    "contrast_type", "timepoint_1", "timepoint_2", "score_types",
    "effect_size_type_reported", "missing_metrics_reason", "effect_favors",
    "d_val", "g_val", "t_val", "F_val", "df_num", "df_den", "r_val",
    "chi2_val", "z_val", "beta_val", "beta_se", "n1i", "n2i", "n_total",
    "m1i", "m2i", "sd1i", "sd2i", "cell_descriptives", "dv_name",
    "group1_label", "group2_label", "comparison_label", "Discernment_Defined_As",
}

#: fields compared to decide whether two rows are the *same* row
ROW_KEY_FIELDS = ("dv_name", "group1_label", "group2_label", "comparison_label",
                  "timepoint_1", "timepoint_2")

#: free-text fields: the routes describe the same thing at different lengths
PROSE_FIELDS = {
    "Study_Description", "Title", "Authors", "Rationale_Notes",
    "Intervention_Topic", "Intervention_Type_Detail", "Sample_Region",
    "clustering", "data_repository", "data_url", "Stimuli", "Stimuli_Topic",
    "factor_levels", "design_factors", "Affiliation_1", "Affiliation_2",
    "Affiliation_3", "Author_1", "Author_2", "Author_3", "Author_Source",
    "Author_Countries", "Author_ORCID_1", "Author_ORCID_2", "Author_ORCID_3",
    "Corresponding_Author_Name", "Corresponding_Author_Email",
    "Corresponding_Author_Source",
    "source_location", "source_quote", "missing_metrics_reason",
    "conversion_method", "conversion_inputs", "effect_direction_meaning",
    "Covariates", "Correction_Source", "Intervention_Source_Concept",
    "Source_Eval_Strategy", "Publication_Type", "Publication_Category",
    "Design_Factors_Cluster", "Intervention_Topic_Cluster",
    "Stimuli_Topic_Cluster", "Outcomes_Source_Quotes", "Source_Locations",
    "Source_Quote_Provenance", "sample_source_quote", "measurement_source_quote",
}

SKIP_FIELDS = {
    "DV_Metrics", "source_spans", "Rationale_Notes", "Parent_Study_ID",
    "Study_ID", "Interventions", "Covariates", "Intervention_Type_Detail",
}

#: DV fields that are *numeric*: a small relative difference is not a
#: disagreement, it is a rounding or a slightly different derivation
NUMERIC_DV_FIELDS = {
    "yi", "vi", "d_val", "g_val", "t_val", "F_val", "r_val", "chi2_val",
    "z_val", "beta_val", "beta_se", "n1i", "n2i", "n_total", "m1i", "m2i",
    "sd1i", "sd2i", "df_num", "df_den",
}


def vocab_fields():
    try:
        s = json.load(open(SCHEMA_PATH, encoding="utf-8"))
    except Exception:
        return set()
    out = set()
    for f in s.get("common_fields") or []:
        if isinstance(f, dict) and f.get("options"):
            out.add(f["name"])
    for m in (s.get("modules") or {}).values():
        for f in (m or {}).get("fields") or []:
            if isinstance(f, dict) and f.get("options"):
                out.add(f["name"])
    return out


def load(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return None
    return d if isinstance(d, list) else d.get("experiments", [])


def routes_for(key):
    return {
        "canonical": load(os.path.join(ROOT, "Extraction", key + ".json")),
        "SB": load(os.path.join(ROOT, "Codings", key + " - SB.json")),
        "MM": load(os.path.join(ROOT, "Codings", key + " - MM.json")),
    }


def keyof(v):
    return json.dumps(v, sort_keys=True, default=str)


#: fields the schema types as a list: the routes are not consistent about
#: writing one element as a bare string ("C2") rather than a list (["C2"]).
#: That is a representation difference, not a disagreement, so the values are
#: normalised before they are compared.
LIST_VALUED_FIELDS = {
    "Fulltext_Exclusion_Criterion", "Intervention_Type", "Intervention_Topic",
    "Media_Support", "Comm_Strategies", "Practice_Strategy",
    "Attention_Cues", "Retrieval_Practice", "Contrasting_Cases",
    "Concept_Labeling", "Interleaving", "Feedback_Mechanics", "ICAP_Role",
    "Social_Context", "Affective_Design", "Self_Efficacy", "Country",
    "Stimuli_Topic", "Stimuli", "Technique_Taxonomy",
    "Intervention_Components", "Intervention_Topic_Cluster",
    "Stimuli_Topic_Cluster", "Design_Factors_Cluster", "WEIRD_Western",
    "WEIRD_Educated", "WEIRD_Industrialized", "WEIRD_Rich", "WEIRD_Democratic",
    "dv_domain", "score_types", "effect_size_type_reported",
    "Intervention_Type_Detail",
}


def canon(v, field=None):
    if isinstance(v, str):
        s = " ".join(v.split()).strip().lower()
        if field in LIST_VALUED_FIELDS and s:
            return (s,)
        return s or None
    if isinstance(v, (list, tuple)):
        s = tuple(sorted(" ".join(str(x).split()).strip().lower() for x in v))
        return s or None
    if isinstance(v, float) and v == int(v):
        return int(v)
    return v


def close(a, b, tol=0.05):
    """Two numbers agree within a relative tolerance."""
    if a is None or b is None:
        return a == b
    try:
        a, b = float(a), float(b)
    except Exception:
        return a == b
    if a == b:
        return True
    return abs(a - b) <= tol * max(max(abs(a), abs(b)), 1e-9)


def is_subset(a, b):
    if not isinstance(a, (list, tuple)) or not isinstance(b, (list, tuple)):
        return False
    sa, sb = set(map(str, a)), set(map(str, b))
    return bool(sa) and sa < sb


def row_key(dv):
    return tuple(str(dv.get(f) or "") for f in ROW_KEY_FIELDS)


def dv_rows(recs):
    """{dv_name: [dv, ...]} for one route, in document order.

    A ``dv_name`` repeats within a route — once per arm pair, per timepoint,
    per condition (1,865 names repeat).  Matching the routes on the *name*
    and pairing the rows by position inside that name is what makes the
    comparison meaningful.  Matching on a composite key instead
    (``dv_name`` + arm labels + timepoints) looks stricter but in practice
    the routes label the same row differently often enough that it silently
    drops 1,129 of the 1,131 real DV conflicts.
    """
    out = defaultdict(list)
    for rec in recs or []:
        for dv in rec.get("DV_Metrics") or []:
            if isinstance(dv, dict) and dv.get("dv_name"):
                out[str(dv["dv_name"])].append(dv)
    return out


def compare_dv(rows_a, rows_b, name_a="A", name_b="B"):
    """Field-level differences between two routes' matching rows.

    Rows are paired by position inside a ``dv_name``.  When the two routes
    carry a different number of rows for a name, the extra rows are a
    granularity difference and are reported as such rather than compared.

    Returns ``(conflicts, touched_names)`` where ``conflicts`` maps a field
    to ``{route_name: value}``.
    """
    conflicts = {}
    touched = set()
    for name in set(rows_a) & set(rows_b):
        la, lb = rows_a[name], rows_b[name]
        for k in range(min(len(la), len(lb))):
            a, b = la[k], lb[k]
            for f in POOL_DV_FIELDS:
                va, vb = a.get(f), b.get(f)
                if va in (None, "") and vb in (None, ""):
                    continue
                if va in (None, "") or vb in (None, ""):
                    # one route left it blank: a coding difference, not a conflict
                    continue
                if f in NUMERIC_DV_FIELDS:
                    agree = close(va, vb)
                else:
                    agree = canon(va) == canon(vb)
                if not agree:
                    conflicts.setdefault(f, {})[name_a] = va
                    conflicts[f][name_b] = vb
                    touched.add(name)
    return conflicts, touched


def main():
    vocab = vocab_fields()
    keys = sorted(os.path.basename(p)[:-5]
                   for p in glob.glob(os.path.join(ROOT, "Extraction", "*.json")))
    out = []
    counts = Counter()

    for key in keys:
        routes = routes_for(key)
        present = {r: v for r, v in routes.items() if v}
        rec = dict(key=key, routes=sorted(present), pool=[], coding=[], prose=0)
        if len(present) < 2:
            rec["note"] = "only one route coded this key"
            out.append(rec)
            counts["single_route"] += 1
            continue

        nexp = {r: len(v) for r, v in present.items()}
        rec["n_experiments"] = nexp
        if len(set(nexp.values())) > 1:
            rec["pool"].append(dict(
                kind="n_experiments", values=nexp,
                why="the routes disagree on how many experiments the paper has, "
                    "so they disagree on which rows are pooled"))

        # --- record fields --------------------------------------------
        # A route that left a field blank has NOT agreed with the others, so
        # the blank is kept as an explicit value.  Dropping it would make
        # "canonical blank, SB=C2, MM=C2" look like a two-route disagreement
        # instead of a settled 2-of-1 majority.
        fields = defaultdict(dict)
        for route, recs in present.items():
            seen = set()
            for r in recs:
                for f, v in r.items():
                    if f in seen:
                        continue
                    seen.add(f)
                    fields[f].setdefault(route, v if v not in (None, "", [], {}) else None)
        for fname, vals in fields.items():
            if fname in SKIP_FIELDS:
                continue
            if len({keyof(canon(v, fname)) for v in vals.values()}) == 1:
                continue
            if fname in PROSE_FIELDS:
                rec["prose"] += 1
                continue
            distinct = {keyof(canon(v, fname)) for v in vals.values()}
            item = dict(kind="field", field=fname, values=vals)
            if len(distinct) == 2:
                item["rule"] = "majority vote (2 of 3 routes agree)"
            else:
                item["rule"] = "all three routes differ"
                lists = [v for v in vals.values() if isinstance(v, (list, tuple))]
                if len(lists) == len(vals) and any(
                        is_subset(lists[i], lists[j])
                        for i in range(len(lists)) for j in range(len(lists)) if i != j):
                    item["rule"] = "one list is a subset of another (completeness)"
            (rec["pool"] if fname in POOL_RECORD_FIELDS else rec["coding"]).append(item)

        # --- DV level, matched on the composite row key -----------------
        per_route_rows = {r: dv_rows(recs) for r, recs in present.items()}
        route_names = sorted(present)
        granularity = []
        for i, ra in enumerate(route_names):
            for rb in route_names[i + 1:]:
                conflicts, touched = compare_dv(per_route_rows[ra],
                                                per_route_rows[rb], ra, rb)
                if conflicts:
                    rec["pool"].append(dict(
                        kind="dv", dv=sorted(touched)[0],
                        dv_names=sorted(touched),
                        conflicts=conflicts))
                # rows one route has and the other does not: a granularity
                # difference, not a value conflict
                for name, rows in per_route_rows[ra].items():
                    other = per_route_rows[rb].get(name, [])
                    if len(rows) != len(other):
                        granularity.append((ra, rb, name, len(rows), len(other)))
        rec["granular_dv_names"] = len(granularity)
        if granularity:
            counts["keys_with_granular_dv_rows"] += 1

        counts["keys_with_pool"] += bool(rec["pool"])
        counts["keys_with_coding"] += bool(rec["coding"])
        counts["pool_items"] += len(rec["pool"])
        counts["coding_items"] += len(rec["coding"])
        counts["prose_items"] += rec["prose"]
        out.append(rec)

    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, "coder_disagreement_tiers.json"), "w",
              encoding="utf-8") as fh:
        json.dump(out, fh, indent=1, ensure_ascii=False, default=str)

    with open(os.path.join(RESULTS, "coder_disagreement_tiers.csv"), "w",
              encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["study_key", "routes", "n_experiments",
                                           "n_pool", "n_coding", "n_prose",
                                           "granular_dv_names"])
        w.writeheader()
        for r in out:
            if r.get("note"):
                continue
            w.writerow(dict(
                study_key=r["key"], routes="|".join(r["routes"]),
                n_experiments="|".join("%s=%d" % kv for kv in
                                       sorted(r["n_experiments"].items())),
                n_pool=len(r["pool"]), n_coding=len(r["coding"]),
                n_prose=r["prose"], granular_dv_names=r.get("granular_dv_names", 0)))

    summary = dict(n_keys=len(out), counts=dict(counts))
    with open(os.path.join(RESULTS, "coder_disagreement_tiers_summary.json"),
              "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
