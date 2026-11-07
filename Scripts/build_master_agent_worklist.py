"""Build the master agent's worklist.

The triage (``Scripts/tier_coder_disagreements.py`` +
``Scripts/split_coder_disagreements.py``) sorts every disagreement between
the canonical route and the two agent routes into a rule, a master-agent
job, or a human decision.  The master agent's jobs are the ones the paper
settles: the experiment count, the value of a field the paper states, the
taxonomy a row belongs to, the completeness of an enumeration.

This script turns that queue into per-study worklists, split into the
*pool* jobs (whose resolution changes what is pooled, so they are worth an
agent's time first) and the *coding* jobs (which change the coding's
fidelity but not the result).

Outputs
-------
``Results/coder_master_worklist.json``   per study: the jobs and the
                                         conflicting values
``Results/coder_master_worklist.csv``    per study: the counts
``Results/coder_master_scope.json``      the totals
``temp/mm_route/master_batches/<NN>.txt``  one prompt per batch of studies
"""
import csv
import json
import os
import sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")
BATCH_DIR = os.path.join(ROOT, "temp", "mm_route", "master_batches")

#: fields whose disagreement cannot change a pooled estimate: the master
#: agent fixes them for coding fidelity, not for the result
CODING_ONLY_FIELDS = {
    "Media_Support", "Comm_Strategies", "Practice_Strategy", "Attention_Cues",
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
    "Intervention_Topic", "Intervention_Topic_Cluster",
    "Stimuli_Topic_Cluster", "Design_Factors_Cluster", "Technique_Taxonomy",
    "Intervention_Components", "Outcome_Measures",
    "Manipulation_Strategy", "Fact_Check_Strategy",
    "Verification_Strategy_Type", "Correction_Structure_Steps",
    "Training_Strategies", "Tips_Content_Checklist", "Search_Task",
    "Intervention_Type", "Posttest_type", "posttest_type",
    "Gender_Ratio", "N_Analyzed", "N_Recruited", "Publication",
    "Discernment_Defined_As", "Media_Support_Other",
}
#: ``Intervention_Type`` is read by the analysis (contract.record_interventions),
#: so it belongs to the POOL bucket even though it is a coding field.
CODING_ONLY_FIELDS.discard("Intervention_Type")

HEAD = """You are the MASTER agent for the misinformation-intervention meta-analysis.
You settle the disagreements between the three independent coding routes.

Workspace: {root}

There are three routes over the same corpus:
  canonical  Extraction/<Key>.json     (the analysis route)
  SB         Codings/<Key> - SB.json  (SpaceBunny route)
  MM         Codings/<Key> - MM.json  (MiniMax route)

For EACH study key listed below, the routes disagree on the items given.
Your job is to settle each one by READING THE PAPER and write the agreed
value into the canonical file Extraction/<Key>.json.

WHAT TO READ
  Papers/<Key>.pdf   and   Text/<Key>.txt
  You may also read the two agent codings for THIS key only
  (Codings/<Key> - SB.json, Codings/<Key> - MM.json) to see what each route
  claimed.  Do NOT read any other key's Codings/ or Extraction/ file, and do
  not grep across those directories.

DO NOT READ `Results/coder_disagreement_agent_queue.csv` or any other
`Results/coder_*` queue: they carry the three routes' values for EVERY key,
so reading one for a field definition would show you other keys' route
values and bias the judgement you are supposed to make from the paper.  The
items for your keys are already listed below, and the field vocabularies are
in `FIELD VOCABULARY` at the end of this brief.  If a field is not listed
there, read its definition from `Scripts/coding_web/schema.json` only.


HOW TO SETTLE AN ITEM
  - The paper is the authority.  If the paper states the value, write it.
  - If the routes differ only in HOW COMPLETELY they enumerated a field
    (one route's list is a subset of another's), write the complete list the
    paper supports, not the shorter one.
  - If the paper does not support either value, keep the canonical route's
    value and say so in your reply.
  - Never invent a value.  Never carry a value over from another paper.
  - For a field the routes disagree on at the DV level (a row of DV_Metrics),
    the paper's table settles the row's label, domain, score type, the
    comparison it describes and its timepoint.

A BLANK IS NOT ALWAYS "NO"
  Several fields use an empty value to mean "not stated" or "unclear" rather
  than "no": code_available (0=not available, null=not stated),
  consent_reported (0=not, null=not stated), WEIRD_Rich (0=not, null=unclear).
  For those, if the paper does not state the thing, LEAVE THE FIELD EMPTY.
  For a 0/1 feature flag (Interleaving, Self_Efficacy, Retrieval_Practice,
  Contrasting_Cases, Concept_Labeling, ...), a 0 means the feature was absent
  and a 1 means it was present, and the paper's method section settles it.
  When you fill a field, say in your reply which paper page supports it.

FIELDS YOU MUST NOT TOUCH
  Fulltext_Exclusion_Criterion, eligible_primary, dv_polarity, n_arms,
  control_group_type, Fully_Within, and the numeric effect fields
  (yi, vi, d_val, g_val, t_val, F_val, n1i, n2i, m1i, m2i, sd1i, sd2i,
  df_num, df_den).  A human master decides those; they are listed here only
  for context.  Leave them EXACTLY as they are.

HOW TO WRITE
  Edit Extraction/<Key>.json in place.  Change ONLY the listed fields.
  Keep every other key, its value, its order and the file's formatting
  exactly as it is.  Do not reformat, reorder or "tidy" anything else.

  If Intervention_Type is set to a value containing "other", the record must
  also carry a non-empty Intervention_Type_Detail.  If you set
  Intervention_Type to something else, and the record has an
  Intervention_Type_Detail that no longer fits, leave the detail alone
  rather than deleting it.

VERIFY before finishing, for every key:
  python Scripts/B_validate.py "Extraction/<Key>.json"
It must print OK for that key.

Scratch under temp/mm_route/master/ only.  Never delete anything under temp/.

Reply with one line per item you settled:
  <key> | <field or dv name> | <value you wrote> | <page of the paper>
and a final line:  DONE <n> keys

FIELD VOCABULARY
The controlled vocabularies for the fields in play, so you never have to
hunt for a definition.  A value outside its vocabulary is a coding error.

{fields}
"""


def dv_row_hint(key, item):
    """Which DV row an item refers to.

    An item names the *field* and the outcome name, but the same outcome
    name recurs once per arm pair and per timepoint, so the agent otherwise
    has to go hunting through the route files (and, worse, through the
    global queue files) to work out which row it is looking at.  The rows
    are named here instead.
    """
    name = item.get("dv") or ""
    if not name or item.get("kind") != "dv":
        return []
    path = os.path.join(ROOT, "Extraction", key + ".json")
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return []
    recs = d if isinstance(d, list) else d.get("experiments", [])
    out = []
    for i, rec in enumerate(recs):
        for j, dv in enumerate(rec.get("DV_Metrics") or []):
            if not isinstance(dv, dict):
                continue
            if str(dv.get("dv_name")) != str(name):
                continue
            out.append("      row: record %d, DV_Metrics[%d] - %s | %s | %s"
                       % (i, j, dv.get("comparison_label") or "",
                          dv.get("group1_label") or "", dv.get("group2_label") or ""))
    if not out:
        out.append("      row: (no DV row with this name in the canonical file)")
    return out


def field_vocabulary():
    """The schema's option lists for the fields the master agent touches."""
    path = os.path.join(ROOT, "Scripts", "coding_web", "schema.json")
    try:
        with open(path, encoding="utf-8") as fh:
            s = json.load(fh)
    except Exception:
        return "(schema unavailable)"

    named = {}
    for f in s.get("common_fields") or []:
        if isinstance(f, dict) and f.get("options"):
            named[f["name"]] = f
    for m in (s.get("modules") or {}).values():
        for f in (m or {}).get("fields") or []:
            if isinstance(f, dict) and f.get("options"):
                named[f["name"]] = f

    lines = []
    for name in sorted(named):
        f = named[name]
        opts = ", ".join("%s=%s" % (o.get("value"), o.get("label"))
                         for o in f["options"])
        lines.append("- %s: %s" % (name, opts))
    return "\n".join(lines) if lines else "(no option lists found)"


def main():
    agent = list(csv.DictReader(
        open(os.path.join(RESULTS, "coder_disagreement_agent_queue.csv"),
             encoding="utf-8-sig")))

    pool_jobs, coding_jobs = defaultdict(list), defaultdict(list)
    for r in agent:
        rule = r["rule"]
        if rule.startswith("majority"):
            continue          # settled by the rule (the script applied it)
        if rule.startswith("paper"):
            # every paper question, including a *completeness* relation: one
            # route's list being a subset of another's is NOT settled by a
            # rule - the paper decides which enumeration is right.  The
            # fixer deliberately holds these back, so they must reach the
            # master agent.
            if r["field"] in CODING_ONLY_FIELDS:
                coding_jobs[r["study_key"]].append(r)
            else:
                pool_jobs[r["study_key"]].append(r)
            continue
        if r["kind"] == "gap":
            # a gap the canonical route left: the paper records it
            coding_jobs[r["study_key"]].append(r)

    # A record whose DV rows label the comparison one way while the record's
    # own ``control_group_type`` says another is contradicting itself.  The
    # comparison itself (group1 vs group2) is usually right; the LABEL that
    # says which kind of control it was is wrong, and that label is what the
    # eligibility judgement ("a no-misinformation-intervention control") is
    # read from.  The master agent is already reading the paper, so it is
    # asked to settle it.
    contradictions = defaultdict(list)
    cpath = os.path.join(RESULTS, "contrast_control_consistency.csv")
    if os.path.isfile(cpath):
        import csv as _csv
        with open(cpath, encoding="utf-8-sig", newline="") as fh:
            for r in _csv.DictReader(fh):
                contradictions[r["study_key"]].append(r)

    # A row whose reported effect type is "none" while it carries a usable
    # effect: the standardisation guard reads "none" as "the paper reported
    # no effect size" and refuses the row, so the row carries an effect the
    # pipeline will not pool and nothing in the logs says why.
    effect_none = defaultdict(list)
    epath = os.path.join(RESULTS, "effect_type_none_rows.csv")
    if os.path.isfile(epath):
        import csv as _csv
        with open(epath, encoding="utf-8-sig", newline="") as fh:
            for r in _csv.DictReader(fh):
                effect_none[r["study_key"]].append(r)

    work = {}
    for key in (set(pool_jobs) | set(coding_jobs) | set(contradictions)
                | set(effect_none)):
        work[key] = dict(pool=pool_jobs.get(key, []),
                         coding=coding_jobs.get(key, []),
                         contradiction=contradictions.get(key, []),
                         effect_none=effect_none.get(key, []))

    with open(os.path.join(RESULTS, "coder_master_worklist.json"), "w",
              encoding="utf-8") as fh:
        json.dump(work, fh, indent=1, ensure_ascii=False, default=str)

    with open(os.path.join(RESULTS, "coder_master_worklist.csv"), "w",
              encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["study_key", "n_pool_jobs", "n_coding_jobs"])
        for key in sorted(work):
            w.writerow([key, len(work[key]["pool"]), len(work[key]["coding"])])

    scope = dict(
        pool_jobs=sum(len(v["pool"]) for v in work.values()),
        pool_keys=len(pool_jobs),
        coding_jobs=sum(len(v["coding"]) for v in work.values()),
        coding_keys=len(coding_jobs),
    )
    with open(os.path.join(RESULTS, "coder_master_scope.json"), "w",
              encoding="utf-8") as fh:
        json.dump(scope, fh, indent=2, ensure_ascii=False)

    # Batch the work so the *pool* jobs come first: a batch that carries a
    # pool job is worth an agent's time before a batch of coding-only jobs,
    # because a pool job changes what the analysis pools and a coding job
    # does not.
    #
    # The corpus changes as the master agent works, so a rebuild must not
    # renumber the batches: the queue state in ``temp/mm_route/master_state.json``
    # is keyed by batch id.  Keys keep their batch unless that batch is
    # already **done**, in which case they are re-queued in a new batch so the
    # items added since the batch ran are not stranded.
    os.makedirs(BATCH_DIR, exist_ok=True)
    state_path = os.path.join(ROOT, "temp", "mm_route", "master_state.json")
    done_batches = set()
    running_batches = set()
    if os.path.isfile(state_path):
        try:
            with open(state_path, encoding="utf-8") as fh:
                st = json.load(fh)
            for bid, v in (st.get("batches") or {}).items():
                if v.get("status") == "done":
                    done_batches.add(bid)
                elif v.get("status") == "running":
                    running_batches.add(bid)
        except Exception:
            pass

    # which key belongs to which existing batch file
    key_to_batch = {}
    for fn in sorted(os.listdir(BATCH_DIR)):
        if not fn.endswith(".txt"):
            continue
        bid = fn[:-4]
        try:
            with open(os.path.join(BATCH_DIR, fn), encoding="utf-8") as fh:
                body = fh.read()
        except Exception:
            continue
        for line in body.split("\n"):
            if line.startswith("Study key: "):
                key_to_batch.setdefault(line[len("Study key: "):].strip(), bid)

    pool_keys = sorted(k for k, v in work.items() if v["pool"])
    coding_keys = sorted(k for k, v in work.items() if v["coding"] and not v["pool"])
    per_pool = int(os.environ.get("MASTER_BATCH_POOL", "5"))
    per_coding = int(os.environ.get("MASTER_BATCH_CODING", "8"))

    # keep a key in its existing batch unless that batch is done
    keep = {"pool": [], "coding": []}
    requeue = {"pool": [], "coding": []}
    for label, keys in (("pool", pool_keys), ("coding", coding_keys)):
        for k in keys:
            bid = key_to_batch.get(k)
            if bid is None:
                requeue[label].append(k)
            elif bid in done_batches:
                requeue[label].append(k)
            else:
                keep[label].append(k)

    # regroup: the kept keys stay in their original batches; the re-queued
    # ones are packed into fresh batches
    batches = []          # [(batch_id, label, [keys])]
    for fn in sorted(os.listdir(BATCH_DIR)):
        if not fn.endswith(".txt"):
            continue
        bid = fn[:-4]
        label = "coding" if bid.endswith("_coding") else "pool"
        members = [k for k, v in work.items()
                   if key_to_batch.get(k) == bid and k in keep[label]]
        if members:
            batches.append((bid, label, members))
    for label, per in (("pool", per_pool), ("coding", per_coding)):
        keys = requeue[label]
        for i in range(0, len(keys), per):
            chunk = keys[i:i + per]
            label_tag = "pool" if label == "pool" else "coding"
            batches.append(("r%03d_%s" % (len(batches), label_tag), label, chunk))

    # rewrite every prompt file (old ones whose keys all moved away are removed)
    wanted = set()
    for bid, label, chunk in batches:
        parts = [HEAD.format(root=ROOT, fields=field_vocabulary())]
        for key in chunk:
            parts.append("=" * 72)
            parts.append("Study key: %s" % key)
            parts.append("Sources : Papers/%s.pdf , Text/%s.txt" % (key, key))
            parts.append("File    : Extraction/%s.json" % key)
            parts.append("")
            if work[key]["pool"]:
                parts.append("POOL ITEMS (settle these first - they change what is pooled):")
                for r in work[key]["pool"]:
                    parts.append("  - field: %-26s rule: %s" % (
                        r["field"] or r["dv"], r["rule"]))
                    parts.append("      routes: %s" % r["routes"])
                    parts.extend(dv_row_hint(key, r))
                parts.append("")
            if work[key]["coding"]:
                parts.append("CODING ITEMS (same treatment; they do not change the result):")
                for r in work[key]["coding"]:
                    parts.append("  - field: %-26s rule: %s" % (
                        r["field"] or r["dv"], r["rule"]))
                    parts.append("      routes: %s" % r["routes"])
                    parts.extend(dv_row_hint(key, r))
            if work[key].get("contradiction"):
                parts.append("CONTRADICTION TO SETTLE (the row's contrast_type "
                             "disagrees with the record's own control_group_type):")
                for r in work[key]["contradiction"]:
                    parts.append("  - record %s, DV_Metrics[%s] %s"
                                 % (r["record"], r["dv_index"], r["dv_name"]))
                    parts.append("      contrast_type=%s (%s) but the record's "
                                 "control_group_type=%s (%s)"
                                 % (r["contrast_type"], r["contrast_says"],
                                    r["control_group_type"], r["control_is"]))
                    parts.append("      The comparison itself is usually right; "
                                 "the LABEL is what is wrong. Set contrast_type "
                                 "to what the paper's control arm was.")
            if work[key].get("effect_none"):
                parts.append("REPORTED EFFECT TYPE TO FIX (the row says the "
                             "paper reported no effect size, yet it carries "
                             "one - so the standardisation guard refuses it "
                             "and it never reaches the pool):")
                for r in work[key]["effect_none"]:
                    parts.append("  - record %s, DV_Metrics[%s] %s"
                                 % (r["record"], r["dv_index"], r["dv_name"]))
                    parts.append("      %s" % r["verdict"])
                    parts.append("      the row currently carries d_val=%s, "
                                 "yi=%s, vi=%s; cells n=%s/%s M=%s/%s SD=%s/%s"
                                 % (r["d_val"], r["yi"], r["vi"], r["n1i"],
                                    r["n2i"], r["m1i"], r["m2i"], r["sd1i"],
                                    r["sd2i"]))
                    parts.append("      Set effect_size_type_reported to the "
                                 "type the PAPER reported. Leave the numeric "
                                 "fields alone.")
        fn = bid + ".txt"
        wanted.add(fn)
        with open(os.path.join(BATCH_DIR, fn), "w", encoding="utf-8") as fh:
            fh.write("\n".join(parts))
    for fn in os.listdir(BATCH_DIR):
        if not fn.endswith(".txt") or fn in wanted:
            continue
        # A batch file whose agent is still running must NOT be deleted: the
        # agent read it at the start and is still writing to those keys.
        if fn[:-4] in running_batches:
            continue
        os.remove(os.path.join(BATCH_DIR, fn))

    n = len(batches)
    print(json.dumps(scope, indent=2))
    print("wrote %d batch prompts (%d re-queued, %d kept) to %s"
          % (n, len(requeue["pool"]) + len(requeue["coding"]),
             len(keep["pool"]) + len(keep["coding"]), BATCH_DIR))
    return 0


if __name__ == "__main__":
    sys.exit(main())
