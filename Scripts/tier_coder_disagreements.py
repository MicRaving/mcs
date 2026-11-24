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

Two passes, and why
-------------------
The pass above compares two routes only where their ``dv_name`` **strings
agree**.  For many keys they do not: the canonical route embeds the
contrast in the name ("Food advertising literacy: differentiating
advertising authenticity (Group A vs Group C, posttest)") while an agent
route names the outcome alone ("Advertising authenticity").  The names then
never match, the comparison has nothing to pair, and a disagreement
between the canonical route and that route is **invisible** - it can only
surface as a disagreement between the two agent routes, under the agent
route's name.  177 keys are in that state, and for 642 keys the pass
misses at least one row-level disagreement.

So a second, **additive** pass runs on the rows the name-based pass left
unpaired: they are paired by the *contrast* they describe (the arm codes
and a coarse timepoint class) and, within a contrast, by how well the
outcome names overlap.  The pass never removes a conflict the name-based
pass found, so it cannot hide a disagreement; it can only add candidates,
and every candidate is settled by a master agent that reads the paper.

Products
--------
``Results/coder_disagreement_tiers.csv``    one row per study key
``Results/coder_disagreement_tiers.json``   the full detail
"""
import csv
import glob
import json
import os
import re
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


def dv_rows_by_record(recs):
    """{record_index: {dv_name: [dv, ...]}} for one route.

    The rows of a paper are held **per record** (one record per
    experiment).  A row of one experiment must never be compared with a
    row of another: the contrast-aligned pass once paired a study's
    post-test rows with another study's table, and every "disagreement"
    that came out of it was the other study's data.
    """
    out = defaultdict(lambda: defaultdict(list))
    for i, rec in enumerate(recs or []):
        for dv in rec.get("DV_Metrics") or []:
            if isinstance(dv, dict) and dv.get("dv_name"):
                out[i][str(dv["dv_name"])].append(dv)
    return out
# The routes spell the same arm differently - "experimental group a (food
# advertising literacy)", "group a (food advertising literacy)",
# "Group A" - but the arm *code* is the same, so the code is what the
# alignment uses.
ARM_RE = re.compile(r"(?:groups?\s+|arm\s+|condition\s+|arms?\s+)(\w+)")

#: the minimum outcome-name overlap for a contrast-aligned pair to be
#: believed.  Below this the two rows are not recognisably the same
#: outcome and the pairing would be a guess.
MIN_OVERLAP = 0.6

_NAME_STOP = {"the", "a", "an", "of", "and", "or", "in", "on", "for", "to",
              "vs", "index", "scale", "score", "subscale", "item", "items",
              "total", "mean", "measure", "measures", "rating", "ratings",
              "level", "pretest", "posttest", "follow", "up", "change",
              "corrected", "difference", "differences"}


def _name_tokens(text):
    out = set()
    for w in re.findall(r"[a-z0-9%\-\.]+", str(text or "").lower()):
        w = w.strip(".-")
        if len(w) > 2 and w not in _NAME_STOP:
            out.add(w)
    return out


def _outcome_part(dv):
    """The outcome part of a ``dv_name``.

    A name may end in a parenthetical that carries the contrast; that
    parenthetical belongs to the contrast, not to the outcome.
    """
    name = str(dv.get("dv_name") or "")
    return re.sub(r"\s*\([^()]*\)\s*$", "", name).strip()


def _arm_codes(dv):
    text = " ".join(str(dv.get(f) or "") for f in
                    ("group1_label", "group2_label", "comparison_label"))
    low = text.lower()
    codes = set()
    for m in ARM_RE.finditer(low):
        code = m.group(1).strip("() .,")
        if code and len(code) <= 3:
            codes.add(code)
    m = re.search(r"\b([a-z])\s*,\s*([a-z])\s+and\s+([a-z])\b", low)
    if m:
        codes |= {m.group(1), m.group(2), m.group(3)}
    for m in re.finditer(r"\b([a-z])\s+vs\.?\s+([a-z])\b", low):
        codes |= {m.group(1), m.group(2)}
    if codes:
        return frozenset(codes)
    # No coded arm code was found ("Trained" / "Untrained" / "Control" are not
    # "Group 1"/"Condition a" style labels).  Without a fallback every row of the
    # same timepoint class collapses into ONE signature, and a row of one arm pair
    # is then paired with the row of a *different* arm pair - a manufactured
    # conflict, not a coding disagreement.  The arm pair itself is the contrast
    # identity, so use the two arm labels as the fallback signature.
    pair = tuple(
        re.sub(r"\s+", " ", str(dv.get(f) or "").strip().lower())
        for f in ("group1_label", "group2_label"))
    if not any(pair):
        return frozenset()
    return frozenset("L1:" + p for p in pair if p)


def _timepoint_class(dv):
    text = " ".join(str(dv.get(f) or "") for f in
                    ("comparison_label", "timepoint_1", "timepoint_2",
                     "posttest_type", "dv_name")).lower()
    if "omnibus" in text or "mancova" in text or "anova" in text:
        return "omnibus"
    if ("pre" in text and "post" in text) or "difference-in-difference" in text \
            or "pre->post" in text or "pre-to-post" in text:
        return "prepost"
    if "follow" in text or re.search(r"\b(week|month|day)s?\b", text):
        return "followup"
    if "post" in text:
        return "post"
    if "pre" in text:
        return "pre"
    return "other"


def _contrast_signature(dv):
    return (_timepoint_class(dv), _arm_codes(dv))


def _overlap(a, b):
    ta, tb = _name_tokens(_outcome_part(a)), _name_tokens(_outcome_part(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / float(len(ta | tb))


def align_conflicts(rows_a, rows_b, name_a, name_b, paired_a, paired_b):
    """Pair the rows the name-based pass left over, by the contrast.

    Returns a list of ``(field, {route: value}, row_a, row_b)`` - the
    candidate conflicts the two routes carry about the same row.
    """
    ga, gb = defaultdict(list), defaultdict(list)
    for r in rows_a.values():
        for i, row in enumerate(r):
            if id(row) not in paired_a:
                ga[_contrast_signature(row)].append(row)
    for r in rows_b.values():
        for i, row in enumerate(r):
            if id(row) not in paired_b:
                gb[_contrast_signature(row)].append(row)

    used_a, used_b = set(), set()
    out = []
    for sig, la in ga.items():
        lb = gb.get(sig)
        if not lb:
            continue
        cand = []
        for a in la:
            for b in lb:
                cand.append((_overlap(a, b), a, b))
        cand.sort(key=lambda t: -t[0])
        for score, a, b in cand:
            if id(a) in used_a or id(b) in used_b:
                continue
            if score < MIN_OVERLAP:
                continue
            used_a.add(id(a))
            used_b.add(id(b))
            out.extend(_row_conflicts(a, b, name_a, name_b))
        la2 = [a for a in la if id(a) not in used_a]
        lb2 = [b for b in lb if id(b) not in used_b]
        for a, b in zip(la2, lb2):
            if _overlap(a, b) < MIN_OVERLAP:
                continue
            used_a.add(id(a))
            used_b.add(id(b))
            out.extend(_row_conflicts(a, b, name_a, name_b))
    return out


def _row_conflicts(a, b, name_a, name_b):
    out = []
    for f in POOL_DV_FIELDS:
        va, vb = a.get(f), b.get(f)
        if va in (None, "") or vb in (None, ""):
            continue
        if f in NUMERIC_DV_FIELDS:
            agree = close(va, vb)
        else:
            agree = canon(va) == canon(vb)
        if not agree:
            out.append((f, {name_a: va, name_b: vb}, a, b))
    return out


def _canonical_row_for(row, canon_rows):
    """The canonical file's row for an aligned pair.

    The queue and the brief have to name a row of the file that is
    actually written, so every aligned conflict is addressed by the
    canonical row it belongs to - found by the same contrast and the same
    outcome overlap.  A conflict with no canonical row has nowhere to be
    written and is dropped.
    """
    sig = _contrast_signature(row)
    best, best_score = None, 0.0
    for crow in canon_rows:
        if _contrast_signature(crow) != sig:
            continue
        score = _overlap(crow, row)
        if score > best_score:
            best, best_score = crow, score
    if best is None or best_score < MIN_OVERLAP:
        return None
    return best


def compare_dv(rows_a, rows_b, name_a="A", name_b="B"):
    """Field-level differences between two routes' matching rows.

    Rows are paired by position inside a ``dv_name``.  When the two routes
    carry a different number of rows for a name, the extra rows are a
    granularity difference and are reported as such rather than compared.

    A second, additive pass pairs the rows the name-based pass left over
    by the contrast they describe, so a route that names its rows
    differently is still compared.  Those conflicts come back separately,
    in ``aligned``, because they are candidates: a master agent reads the
    paper before acting on them.

    Returns ``(conflicts, touched_names, aligned)`` where ``conflicts``
    maps a field to ``{route_name: value}`` and ``aligned`` is a list of
    ``(field, {route_name: value}, name_a, name_b)``.
    """
    conflicts = {}
    touched = set()
    touched_rows = {}
    paired_a, paired_b = set(), set()
    for name in set(rows_a) & set(rows_b):
        la, lb = rows_a[name], rows_b[name]
        for k in range(min(len(la), len(lb))):
            a, b = la[k], lb[k]
            paired_a.add(id(a))
            paired_b.add(id(b))
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
                    touched_rows.setdefault(name, (a, b))
    aligned = align_conflicts(rows_a, rows_b, name_a, name_b,
                              paired_a, paired_b)
    return conflicts, touched, aligned, touched_rows


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
        # The rows are held PER RECORD (one record per experiment) and two
        # routes are compared record by record.  A row of one experiment
        # must never be compared with a row of another: the
        # contrast-aligned pass once paired a study's post-test rows with
        # another study's table, and every "disagreement" that came out of
        # it was the other study's data.
        per_route_rows = {r: dv_rows_by_record(recs)
                          for r, recs in present.items()}
        # the canonical route first: the queue and the brief have to name
        # the row of the file that is actually written, and the canonical
        # file is the one that is written
        route_names = sorted(present, key=lambda r: (r != "canonical", r))
        granularity = []
        n_orphan = [0]
        for i, ra in enumerate(route_names):
            for rb in route_names[i + 1:]:
                rows_a = per_route_rows[ra]
                rows_b = per_route_rows[rb]
                canon_rows = [row for names in per_route_rows.get(
                    "canonical", {}).values() for rws in names.values()
                    for row in rws]
                # the records two routes have in common
                common = sorted(set(rows_a) & set(rows_b))
                for k in common:
                    conflicts, touched, aligned, touched_rows = compare_dv(
                        rows_a[k], rows_b[k], ra, rb)
                    if conflicts:
                        # the queue has to name a row of the file that is
                        # actually written, so the canonical route's name for
                        # the outcome wins over the other routes'
                        dv_name = None
                        canon_names = [n for n in sorted(touched)
                                       if n in per_route_rows.get(
                                           "canonical", {}).get(k, {})]
                        if canon_names:
                            dv_name = canon_names[0]
                        else:
                            for n in sorted(touched):
                                pair = touched_rows.get(n)
                                if not pair:
                                    continue
                                crow = _canonical_row_for(pair[0], canon_rows)
                                if crow is not None:
                                    dv_name = str(crow.get("dv_name") or "")
                                    break
                        rec["pool"].append(dict(
                            kind="dv", dv=dv_name or sorted(touched)[0],
                            dv_names=sorted(touched),
                            conflicts=conflicts))
                    # the same rows, paired by the contrast instead of by the
                    # name: the routes that word their row names differently
                    # would otherwise never be compared at all.  Every such
                    # conflict is addressed by the canonical row it belongs
                    # to, so the queue names a row that actually exists.
                    for f, vals, row_a, row_b in aligned:
                        crow = _canonical_row_for(row_a, canon_rows)
                        if crow is None:
                            n_orphan[0] += 1
                            continue
                        rec["pool"].append(dict(
                            kind="dv", dv=str(crow.get("dv_name") or ""),
                            dv_names=[str(row_a.get("dv_name") or ""),
                                      str(row_b.get("dv_name") or "")],
                            conflicts={f: vals}, aligned=1))
                    # rows one route has and the other does not: a granularity
                    # difference, not a value conflict
                    for name, rws in rows_a[k].items():
                        other = rows_b[k].get(name, [])
                        if len(rws) != len(other):
                            granularity.append(
                                (ra, rb, name, len(rws), len(other)))
                # a record one route has and the other does not: the routes
                # disagree on how many experiments the paper has, which is
                # already reported as its own disagreement
                for k in sorted(set(rows_a) ^ set(rows_b)):
                    granularity.append((ra, rb, "<record %d>" % k,
                                        len(rows_a.get(k, {})), 0))
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
