"""Apply the rule-resolved coder disagreements to the canonical route.

The triage (``Scripts/tier_coder_disagreements.py`` +
``Scripts/split_coder_disagreements.py``) sorts the disagreements between the
canonical route and the two agent routes into

* a **majority** — two of the three routes carry the same value, so the
  value is settled and no decision is needed;
* a **paper** question — the paper's own wording settles it, so a master
  agent reads the paper and states the value;
* a **human** question — a judgement the paper does not dictate.

This script applies the first kind.  It writes the majority value into the
canonical route, but only where doing so is *safe*:

* the field is a controlled enumeration, so the majority is a choice from a
  fixed list rather than a free-text paraphrase;
* the field is not one of the campaign's own facts or judgements
  (``NEVER_MAJORITY``);
* the three routes agree on *which record* they are talking about, so a
  record-level value is never transferred to the wrong experiment.

A field can differ between the records of one paper (``Intervention_Type``
does in 51 files, ``Setting`` in 38), so the comparison and the write are
both per record, aligned on ``Study_ID``.

Nothing is invented: the value always comes from one of the three routes,
and the log records which route it came from.

Safety
------
* ``--dry`` is the default; pass ``--apply`` to write.
* Every modified file is copied to
  ``archive/extraction_disagreements_<date>/<Key>.json`` first.
* Every change is appended to ``Results/coder_majority_fixes.csv``.
* Re-running is idempotent.

Usage
-----
    python Scripts/apply_coder_majority_fixes.py            # dry run
    python Scripts/apply_coder_majority_fixes.py --apply
"""
import argparse
import csv
import glob
import json
import os
import re
import shutil
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTRACT = os.path.join(ROOT, "Extraction")
RESULTS = os.path.join(ROOT, "Results")
LOG = os.path.join(RESULTS, "coder_majority_fixes.csv")

#: record fields whose majority value may be written into the canonical route
SAFE_RECORD_FIELDS = {
    "Intervention_Type", "Intervention_Topic", "Media_Support",
    "Comm_Strategies", "Practice_Strategy", "Attention_Cues",
    "Retrieval_Practice", "Contrasting_Cases", "Concept_Labeling",
    "Interleaving", "Feedback_Mechanics", "ICAP_Role", "Social_Context",
    "Affective_Design", "Self_Efficacy", "Setting", "Country",
    "Stimuli_Topic", "Stimuli", "Pedagogy_Type", "Instructor_Type",
    "Modality", "Synthetic", "ethics", "consent_reported",
    "preregistration_status", "dataset_availability_status", "data_available",
    "code_available", "data_repository", "data_url", "clustering",
    "within_between", "unit_of_analysis", "N_Inference_Method",
    "N_Reported_Per_Condition", "Age_Reported_As", "Gender_Reported_As",
    "Age_Group", "International_Sample", "WEIRD_Western", "WEIRD_Educated",
    "WEIRD_Industrialized", "WEIRD_Rich", "WEIRD_Democratic",
    "Intervention_Topic_Cluster", "Stimuli_Topic_Cluster",
    "Design_Factors_Cluster", "Technique_Taxonomy", "Intervention_Components",
    "Manipulation_Strategy", "Fact_Check_Strategy",
    "Verification_Strategy_Type", "Correction_Structure_Steps",
    "Training_Strategies", "Tips_Content_Checklist", "Search_Task",
    "Outcome_Measures", "Posttest_type", "posttest_type",
    "Discernment_Defined_As",
}

#: fields where a majority must NOT be written: they are facts that have to
#: be read from the paper, or judgements with campaign consequences
NEVER_MAJORITY = {
    "N_Analyzed", "N_Recruited", "Age_Mean", "Age_SD", "Gender_Ratio",
    "Authors", "Title", "DOI", "Year", "Publication", "Study_Description",
    "Fulltext_Exclusion_Criterion", "n_arms", "Fully_Within",
    "control_group_type", "Corresponding_Author_Name",
    "Corresponding_Author_Email", "Corresponding_Author_Source",
    "Study_ID", "Parent_Study_ID", "DV_Metrics", "source_spans",
    "Rationale_Notes", "Interventions", "Covariates",
    "Intervention_Type_Detail", "factor_levels", "design_factors",
}


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
        "canonical": load(os.path.join(EXTRACT, key + ".json")),
        "SB": load(os.path.join(ROOT, "Codings", key + " - SB.json")),
        "MM": load(os.path.join(ROOT, "Codings", key + " - MM.json")),
    }


def keyof(v):
    return json.dumps(v, sort_keys=True, default=str)


def canon(v):
    if isinstance(v, str):
        return " ".join(v.split()).strip().lower() or None
    if isinstance(v, (list, tuple)):
        return tuple(sorted(" ".join(str(x).split()).strip().lower() for x in v)) or None
    if isinstance(v, float) and v == int(v):
        return int(v)
    return v


def _needs_detail(value):
    """True when the type code needs a companion detail field."""
    codes = value if isinstance(value, (list, tuple)) else [value]
    return any(str(c) == "other" for c in codes)


def _is_subset(a, b):
    """True when a's list is a strict subset of b's."""
    if not isinstance(a, (list, tuple)) or not isinstance(b, (list, tuple)):
        return False
    sa, sb = set(map(str, a)), set(map(str, b))
    return bool(sa) and sa < sb


def by_study_id(recs):
    """{Study_ID: record}; a record without a Study_ID keeps its position."""
    out = {}
    for i, rec in enumerate(recs or []):
        sid = rec.get("Study_ID")
        out[str(sid) if sid else ("#%d" % i)] = rec
    return out


def norm_id(sid):
    """A spelling-insensitive form of a ``Study_ID``.

    The three routes spell the same experiment's id differently
    (``Amar2026_Exp1`` vs ``AMAR2026_Exp1``,
    ``Agostinelli-Grube2005_Exp1`` vs ``Agostinelli_Grube_2005_Exp1``), so an
    exact match silently skips the record-level comparison for 270 of the 792
    keys.  This form lets the comparison run; it never rewrites an id.
    """
    return re.sub(r"[^a-z0-9]", "", str(sid).lower())


def align_records(routes):
    """{canonical_sid: {route: record}} for the records that can be matched.

    Three passes, strongest first:

    1. the exact ``Study_ID``;
    2. the spelling-insensitive id, which recovers the routes that merely
       spell the same experiment differently;
    3. position, when the routes hold the same number of experiments and the
       ids do not line up at all (a genuine naming disagreement).

    Returns ``(aligned, unmatched)``.
    """
    by_route = {r: by_study_id(recs) for r, recs in routes.items()}
    canon = by_route.get("canonical", {})
    agents = {r: v for r, v in by_route.items() if r != "canonical"}
    aligned = {}
    unmatched = []

    # pass 1 + 2: exact id, then spelling-insensitive id
    norm_index = {}
    for r, recs in agents.items():
        for sid, rec in recs.items():
            norm_index.setdefault(norm_id(sid), {}).setdefault(r, rec)
    for csid, crec in canon.items():
        matched = {}
        for r, recs in agents.items():
            hit = recs.get(csid)
            if hit is None:
                hit = norm_index.get(norm_id(csid), {}).get(r)
            if hit is not None:
                matched[r] = hit
        if matched:
            aligned[csid] = matched
        else:
            unmatched.append(csid)

    # pass 3: the ids do not line up at all -> pair by position, and only
    # when the routes hold the same number of experiments
    if unmatched:
        for r, recs in agents.items():
            if len(recs) == len(canon):
                for csid, rec in zip(canon.items(), recs.items()):
                    if csid in unmatched and csid not in aligned:
                        aligned[csid] = {r: rec[1]}
                break

    return aligned, unmatched


def running_batch_keys():
    """The study keys held by a master-agent batch that is still running.

    Writing to a key an agent is editing at the same time loses one side's
    change, so the fixer defers those keys until the batch lands.
    """
    state = os.path.join(ROOT, "temp", "mm_route", "master_state.json")
    if not os.path.isfile(state):
        return set()
    try:
        with open(state, encoding="utf-8") as fh:
            st = json.load(fh)
    except Exception:
        return set()
    running = {b for b, v in (st.get("batches") or {}).items()
               if v.get("status") == "running"}
    if not running:
        return set()
    out = set()
    bdir = os.path.join(ROOT, "temp", "mm_route", "master_batches")
    for b in running:
        path = os.path.join(bdir, b + ".txt")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("Study key: "):
                        out.add(line[len("Study key: "):].strip())
        except Exception:
            continue
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--include-running", action="store_true",
                    help="also write to the keys a master agent is editing "
                         "right now (races)")
    args = ap.parse_args()

    keys = sorted(os.path.basename(p)[:-5]
                   for p in glob.glob(os.path.join(EXTRACT, "*.json")))
    busy = set() if args.include_running else running_batch_keys()
    deferred = 0
    plan = []          # (key, {study_id: {field: (value, win, lose)}})
    rows = []
    skipped = 0
    subset_skipped = 0
    fills = 0
    for key in keys:
        routes = routes_for(key)
        present = {r: v for r, v in routes.items() if v}
        if "canonical" not in present or len(present) < 2:
            continue
        if key in busy:
            deferred += 1
            continue
        by_route = {r: by_study_id(v) for r, v in present.items()}
        aligned, unmatched_ids = align_records(present)
        per_record = {}
        for sid in sorted(aligned):
            recs = dict(aligned[sid])
            recs["canonical"] = by_route["canonical"][sid]
            fields = {}
            companion = {}
            for f in set().union(*(set(rec) for rec in recs.values())):
                if f in NEVER_MAJORITY or f not in SAFE_RECORD_FIELDS:
                    continue
                present = {r: rec.get(f) for r, rec in recs.items()
                           if rec.get(f) not in (None, "", [], {})}
                blank = [r for r, rec in recs.items() if r not in present]
                if len(present) < 2:
                    continue
                groups = {}
                for r, v in present.items():
                    groups.setdefault(keyof(canon(v)), []).append((r, v))
                if len(groups) == 1 and blank:
                    # The canonical route left the field empty and the other
                    # two agree on a value.  This is NOT a disagreement to
                    # reconcile but a gap to fill, and a gap needs the paper:
                    # for several fields ``null`` means "not stated" or
                    # "unclear" (code_available, consent_reported,
                    # WEIRD_Rich, ...), so writing 0 would turn "we do not
                    # know" into "no".  The master agent records it instead.
                    fills += 1
                    continue
                if len(groups) != 2:
                    continue
                win = [g for g in groups.values() if len(g) == 2]
                lose = [g for g in groups.values() if len(g) == 1]
                if not win or not lose:
                    continue
                w_routes = sorted(r for r, _ in win[0])
                w_val = win[0][0][1]
                l_routes = sorted(r for r, _ in lose[0])
                l_val = lose[0][0][1]
                # A list-valued field where one route's list is a strict
                # subset of another's is a *completeness* question, not a
                # majority: the routes agree on the items they share and
                # differ only in how fully they enumerated the field.  The
                # triage sends that to the master agent, so the fixer must
                # not apply it.
                if _is_subset(w_val, l_val) or _is_subset(l_val, w_val):
                    subset_skipped += 1
                    continue
                # A code that requires a companion free-text field must bring
                # that field with it: ``Intervention_Type = ['other']`` needs
                # an ``Intervention_Type_Detail``, and the canonical record
                # that had a different type carries no detail.  The detail is
                # taken from the same route whose type was adopted, so the
                # pair stays internally consistent.
                if f == "Intervention_Type" and _needs_detail(w_val):
                    detail = None
                    for r in w_routes:
                        cand = recs[r].get("Intervention_Type_Detail")
                        if cand:
                            detail = cand
                            break
                    if not detail:
                        continue        # no detail available: hold it back
                    fields[f] = (w_val, w_routes, l_routes, l_val)
                    companion[f] = ("Intervention_Type_Detail", detail)
                    continue
                fields[f] = (w_val, w_routes, l_routes, l_val)
            if fields:
                per_record[sid] = (fields, companion)
                for f, (w_val, w_routes, l_routes, l_val) in sorted(fields.items()):
                    # A "planned" row is a route disagreement of the majority
                    # kind; it is a *pending* write only when the canonical
                    # route does not already hold the majority value.  The
                    # two counts differ, and only the pending ones are work.
                    pending = (keyof(canon(recs["canonical"].get(f)))
                               != keyof(canon(w_val)))
                    rows.append(dict(
                        study_key=key, study_id=sid, field=f,
                        adopted_from="+".join(w_routes),
                        rejected_from="+".join(l_routes),
                        adopted_value=json.dumps(w_val, ensure_ascii=False)[:200],
                        rejected_value=json.dumps(l_val, ensure_ascii=False)[:200],
                        pending=int(pending)))
        if per_record:
            plan.append((key, per_record))
        # records that only one route has are a granularity difference, not a
        # majority; they are handled by the master agent, not here
        for r in present:
            if r == "canonical":
                continue
            extra = set(by_route[r]) - set(by_route["canonical"])
            skipped += len(extra)

    pending = [r for r in rows if r["pending"]]
    print("keys with a majority-resolvable difference: %d" % len(plan))
    print("record-level majority differences found: %d" % len(rows))
    print("of which STILL PENDING a write: %d" % len(pending))
    print("records only one route has (master agent, not a majority): %d" % skipped)
    print("list subset relations held back for the master agent: %d" % subset_skipped)
    print("blank canonical fields held back for the master agent: %d" % fills)
    if busy:
        print("keys deferred (a master agent is editing them now): %d" % deferred)
    for r in rows[:20]:
        print("   %-30s %-14s %-26s <- %-18s (was %s)"
              % (r["study_key"][:30], r["study_id"][:14], r["field"][:26],
                 r["adopted_from"], r["rejected_from"]))

    os.makedirs(RESULTS, exist_ok=True)
    with open(LOG, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["study_key", "study_id", "field",
                                           "adopted_from", "rejected_from",
                                           "adopted_value", "rejected_value",
                                           "pending"])
        w.writeheader()
        w.writerows(rows)
    print("\nwrote", LOG)

    if not args.apply:
        print("\ndry run - pass --apply to write")
        return 0

    stamp = datetime.now().strftime("%Y-%m-%d")
    backup_dir = os.path.join(ROOT, "archive", "extraction_disagreements_" + stamp)
    os.makedirs(backup_dir, exist_ok=True)
    written = 0
    for key, per_record in plan:
        path = os.path.join(EXTRACT, key + ".json")
        data = json.load(open(path, encoding="utf-8"))
        recs = data if isinstance(data, list) else data.get("experiments", [])
        index = {}
        for i, rec in enumerate(recs):
            sid = rec.get("Study_ID")
            index[str(sid) if sid else ("#%d" % i)] = rec
        changed = False
        for sid, (fields, companion) in per_record.items():
            rec = index.get(sid)
            if rec is None:
                continue
            for f, (w_val, _w, _l, _lv) in fields.items():
                if f not in rec:
                    continue
                if keyof(canon(rec.get(f))) == keyof(canon(w_val)):
                    continue
                rec[f] = w_val
                changed = True
                # the companion field travels with its code
                if f in companion:
                    cf, cv = companion[f]
                    rec[cf] = cv
                    changed = True
        if not changed:
            continue
        shutil.copy2(path, os.path.join(backup_dir, key + ".json"))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        written += 1
    print("wrote %d file(s); backups in %s" % (written, backup_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
