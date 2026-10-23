"""Split the coder disagreements: a rule, a master agent, or a human?

The tiering (``Scripts/tier_coder_disagreements.py``) put every difference
into the *pool* tier (it can change a pooled estimate), the *coding* tier
(it changes the coding's fidelity but not the result) or the *prose* tier
(not a disagreement at all).  This script decides who acts on each.

The test is: **is there a majority to fall back on?**

A difference is resolvable by the majority rule only when at least two of
the three routes carry the same value.  A one-against-one split — the common
case, because a key is often coded by only two routes — has no majority, and
the minority value is not thereby wrong.  So:

``majority``  at least two routes agree -> adopt that value.  A script can do
              it; it is not a decision.
``paper``     the routes disagree about a *fact* the paper states (how many
              experiments, how many participants) or about a taxonomy the
              paper's own wording settles.  A master agent reads the paper
              and states the value.
``human``     the routes disagree about a *judgement* that decides what is
              pooled, and no two routes agree: whether a contrast qualifies
              as the primary eligible one, which direction is desirable,
              which domain an outcome belongs to, whether the study meets
              the inclusion criteria.

Only the last group needs the human master.

Products
--------
``Results/coder_disagreement_human_queue.csv``   the human master's queue
``Results/coder_disagreement_agent_queue.csv``   the master agent's queue
``Results/coder_disagreement_split_summary.json``
"""
import csv
import json
import os
import sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")

#: judgement fields: the value is a *call*, so "the majority said so" is
#: evidence, not proof — and a one-against-one split is a real disagreement
JUDGEMENT_POOL_FIELDS = {
    "eligible_primary", "dv_polarity", "dv_domain", "control_group_type",
    "Fulltext_Exclusion_Criterion", "n_arms", "Fully_Within",
    "Intervention_Type", "contrast_type", "timepoint_1", "timepoint_2",
    "score_types", "effect_size_type_reported", "effect_favors",
    "Synthetic", "within_between",
}

#: record fields that are *facts* the paper states: a master agent reads it
FACTUAL_POOL_FIELDS = {
    "N_Analyzed", "N_Recruited", "N_Reported_Per_Condition", "Age_Mean",
    "Age_SD", "Gender_Ratio", "Publication", "DOI", "Year",
}

#: fields the paper's own wording settles, so a master agent that reads the
#: paper can decide them without a campaign-level judgement:
#:
#:   Intervention_Type / control_group_type  the paper says what the
#:       intervention was and what the control group did
#:   n_arms / Fully_Within                    the paper states the design
#:   dv_domain / score_types                  the outcome's name and the
#:       measure's description fix both
#:   contrast_type / timepoint_*              the paper states the comparison
#:       and when it was measured
#:
#: These stay out of the human queue.
PAPER_SETTLED_POOL_FIELDS = {
    "Intervention_Type", "control_group_type", "n_arms", "Fully_Within",
    "dv_domain", "score_types", "contrast_type", "timepoint_1", "timepoint_2",
    "effect_size_type_reported", "effect_favors", "Synthetic", "within_between",
}

#: the decisions the paper does *not* dictate: they are campaign-level calls
#: with consequences for what is pooled, so a human master decides them
HUMAN_JUDGEMENT_FIELDS = {
    "eligible_primary",          # is this contrast the primary eligible one?
    "dv_polarity",               # which direction is desirable?
    "Fulltext_Exclusion_Criterion",  # does the study meet the inclusion criteria?
}

#: fields whose BLANK is a substantive value rather than a missing one.
#: ``Fulltext_Exclusion_Criterion`` is empty exactly when the study meets
#: every inclusion criterion, so a blank route disagrees with a route that
#: recorded a criterion — it does not merely abstain.  Such a field can
#: never be settled by a majority.
BLANK_IS_A_VALUE = {"Fulltext_Exclusion_Criterion"}


def is_subset(a, b):
    if not isinstance(a, (list, tuple)) or not isinstance(b, (list, tuple)):
        return False
    sa, sb = set(map(str, a)), set(map(str, b))
    return bool(sa) and sa < sb


def list_nested(fvals):
    """True when one route's list is a strict subset of another's.

    That is a *completeness* difference — the routes agree on the items they
    share — not a contradiction, so it never reaches the human.
    """
    lists = [v for v in fvals.values() if isinstance(v, (list, tuple))]
    if len(lists) < 2:
        return False
    return any(is_subset(lists[i], lists[j])
               for i in range(len(lists)) for j in range(len(lists)) if i != j)


#: fields the schema types as a list: one route may write a single element
#: as a bare string ("C2") where another writes ["C2"].  That is a
#: representation difference, not a disagreement.
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


def keyof(v, field=None):
    if isinstance(v, str) and field in LIST_VALUED_FIELDS and v.strip():
        v = [v]
    return json.dumps(v, sort_keys=True, default=str)


def modal(values, field=None):
    """(modal value, votes for it, number of distinct values)."""
    counts = Counter(keyof(v, field) for v in values.values())
    if not counts:
        return None, 0, 0
    top = counts.most_common(1)[0]
    return top[0], top[1], len(counts)


def main():
    tiers = json.load(open(os.path.join(RESULTS, "coder_disagreement_tiers.json"),
                            encoding="utf-8"))
    human, agent = [], []
    counts = Counter()

    for e in tiers:
        if e.get("note"):
            continue
        key = e["key"]

        # --- pool tier: merge the pairwise DV conflicts by route --------
        # the tiering compares routes pairwise; a DV field can therefore
        # appear in up to three comparisons.  Merge them so each field is
        # judged once, with every route's value.
        merged = []          # list of (field, {route: value}, kind, dv name)
        for p in e["pool"]:
            if p["kind"] == "n_experiments":
                # handled separately below; it is not a field value
                continue
            if p["kind"] != "dv":
                merged.append((p.get("field") or "", p.get("values") or {},
                               "field", ""))
                continue
            per_field = defaultdict(dict)
            for f, vals in (p.get("conflicts") or {}).items():
                for r, v in vals.items():
                    per_field[f][r] = v
            for f, vals in per_field.items():
                # the outcome NAME, so the queue and the brief can say which
                # row the item is about
                merged.append((f, vals, "dv", p.get("dv") or ""))

        for f, fvals, kind, dvname in merged:
            if not fvals:
                continue
            modal_key, votes, distinct = modal(fvals, f)
            by = {keyof(v, f): v for v in fvals.values()}
            shown = {r: str(by.get(keyof(v, f), v))[:70] for r, v in fvals.items()}
            is_judgement = f in JUDGEMENT_POOL_FIELDS
            is_factual = f in FACTUAL_POOL_FIELDS
            base = dict(study_key=key, kind=kind, field=f, dv=dvname,
                        routes=json.dumps(shown, default=str),
                        n_routes=len(fvals), n_distinct=distinct,
                        votes_for_modal=votes)
            if votes >= 2 and f not in BLANK_IS_A_VALUE:
                base["rule"] = "majority: %d of %d routes agree" % (votes, len(fvals))
                base["resolved_value"] = str(by.get(modal_key, ""))[:140]
                agent.append(base)
                counts["agent_majority"] += 1
                continue
            if f in BLANK_IS_A_VALUE:
                # A blank here is ambiguous: it may mean "the study meets
                # every inclusion criterion" or merely "this route did not
                # record a criterion".  A positive claim (a recorded
                # criterion) is therefore weighed differently:
                #   two routes recording the same criterion  -> a majority
                #   one route recording a criterion against blanks -> a paper
                #     question, because the blanks may be a non-coding
                #   two different positive claims, or three -> a human call
                nonblank = {r: v for r, v in fvals.items()
                            if v not in (None, "", [], {})}
                distinct_named = {keyof(v, f) for v in nonblank.values()}
                if len(nonblank) >= 2 and len(distinct_named) == 1:
                    base["rule"] = "majority: %d routes recorded the same criteria" \
                        % len(nonblank)
                    base["resolved_value"] = str(list(nonblank.values())[0])[:140]
                    agent.append(base)
                    counts["agent_majority"] += 1
                    continue
                if len(nonblank) == 1:
                    base["rule"] = "paper: read the paper, settle the criteria"
                    base["why"] = ("one route recorded an unmet criterion and "
                                   "the others are blank, which may mean the "
                                   "study meets every criterion or merely that "
                                   "the other routes did not record one")
                    agent.append(base)
                    counts["agent_paper_settled"] += 1
                    continue
                base["rule"] = "human: judgement, no majority"
                base["why"] = ("the routes record different unmet criteria; "
                               "which ones really apply is a screening "
                               "judgement")
                human.append(base)
                counts["human"] += 1
                continue
            if list_nested(fvals):
                # the routes agree on the items they share; they differ only in
                # how completely the field was enumerated
                base["rule"] = "paper: completeness — the routes share every item"
                base["why"] = ("one route's list is a subset of another's: the "
                               "routes agree on the shared items and differ only "
                               "in how completely the field was enumerated")
                agent.append(base)
                counts["agent_subset"] += 1
                continue
            if f in HUMAN_JUDGEMENT_FIELDS:
                base["rule"] = "human: judgement, no majority"
                base["why"] = ("the routes disagree on a judgement the paper does "
                               "not dictate, and it decides what is pooled")
                if f in BLANK_IS_A_VALUE:
                    base["why"] = ("a blank here means the study meets every "
                                   "inclusion criterion, so an empty route "
                                   "disagrees with a route that recorded a "
                                   "criterion rather than abstaining")
                human.append(base)
                counts["human"] += 1
                continue
            if is_factual:
                base["rule"] = "paper: a fact the paper states"
                base["why"] = ("the routes report different values for a factual "
                               "count; the paper settles it")
                agent.append(base)
                counts["agent_factual"] += 1
                continue
            if is_judgement or f in PAPER_SETTLED_POOL_FIELDS:
                base["rule"] = "paper: read the paper, settle the value"
                base["why"] = ("the paper's own wording settles this field; the "
                               "routes differ in how they read it")
                agent.append(base)
                counts["agent_paper_settled"] += 1
                continue
            base["rule"] = "paper: read the paper, settle the value"
            agent.append(base)
            counts["agent_other"] += 1

        # --- the experiment count: the paper settles it ----------------
        for p in e["pool"]:
            if p["kind"] != "n_experiments":
                continue
            vals = p.get("values") or {}
            _, votes, distinct = modal(vals) if vals else (None, 0, 0)
            base = dict(
                study_key=key, kind="n_experiments", field="", dv="",
                rule="paper: read the paper, state the experiment count",
                why="the routes disagree on how many experiments the paper has, "
                    "so they disagree on which rows are pooled",
                routes=json.dumps(vals, default=str),
                n_routes=len(vals), n_distinct=distinct,
                votes_for_modal=votes)
            if votes >= 2:
                base["rule"] = ("majority: %d of %d routes agree on the "
                                "experiment count" % (votes, len(vals)))
            agent.append(base)
            counts["agent_n_experiments"] += 1

        # --- the coding tier never reaches the human --------------------
        for c in e["coding"]:
            fvals = c.get("values") or {}
            if isinstance(fvals, dict) and fvals:
                cf = c.get("field")
                modal_key, votes, distinct = modal(fvals, cf)
                by = {keyof(v, cf): v for v in fvals.values()}
                shown = {r: str(by.get(keyof(v, cf), v))[:70]
                         for r, v in fvals.items()}
                rule = ("majority: %d of %d routes agree" % (votes, len(fvals))
                        if votes >= 2 else
                        "paper: read the paper, settle the value")
            else:
                shown, rule = {}, c.get("rule", "")
            agent.append(dict(
                study_key=key, kind=c["kind"], field=c.get("field", ""), dv="",
                rule=rule,
                why="a field the analysis does not read: it changes the coding's "
                    "fidelity and the IRR numbers, not the result",
                routes=json.dumps(shown, default=str), n_routes=0,
                n_distinct=0, votes_for_modal=0))
            counts["agent_coding"] += 1

    # --- the gaps the canonical route left, which the two agent routes
    # --- both filled: a gap needs the paper, not a rule
    for e in tiers:
        if e.get("note"):
            continue
        for c in e["coding"] + e["pool"]:
            vals = c.get("values") or {}
            if not isinstance(vals, dict) or len(vals) < 3:
                continue
            present = {r: v for r, v in vals.items()
                       if v not in (None, "", [], {})}
            blanks = [r for r in vals if r not in present]
            if not blanks or len(present) < 2:
                continue
            distinct = {keyof(v, c.get("field")) for v in present.values()}
            if len(distinct) != 1:
                continue
            agent.append(dict(
                study_key=e["key"], kind="gap", field=c.get("field", ""),
                dv="", rule="paper: the canonical route left this blank - "
                            "record what the paper supports",
                why="the canonical route left the field empty and both agent "
                    "routes filled it; a gap needs the paper, and for several "
                    "fields a blank means 'not stated' rather than 'no'",
                routes=json.dumps({r: str(v)[:60] for r, v in present.items()},
                                  default=str),
                n_routes=len(present), n_distinct=1,
                votes_for_modal=len(present)))
            counts["agent_gap"] += 1

    for name, rows in (("coder_disagreement_human_queue", human),
                        ("coder_disagreement_agent_queue", agent)):
        with open(os.path.join(RESULTS, name + ".csv"), "w",
                  encoding="utf-8-sig", newline="") as fh:
            cols = ["study_key", "kind", "field", "dv", "rule", "why",
                    "n_routes", "n_distinct", "votes_for_modal", "routes",
                    "resolved_value"]
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)

    summary = dict(
        human_items=len(human),
        human_keys=len({r["study_key"] for r in human}),
        human_by_field=dict(Counter(r["field"] for r in human).most_common()),
        agent_items=len(agent),
        agent_keys=len({r["study_key"] for r in agent}),
        counts=dict(counts),
    )
    with open(os.path.join(RESULTS, "coder_disagreement_split_summary.json"),
              "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)

    print(json.dumps(summary, indent=2))
    print("\nhuman queue (first 30 of %d):" % len(human))
    for r in human[:30]:
        print("   %-32s %-26s %s" % (r["study_key"][:32], r["field"][:26],
                                       r["routes"][:88]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
