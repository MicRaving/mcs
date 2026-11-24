"""Build the human master's briefing sheet.

The disagreement triage sorts every difference between the three coding
routes into three destinations: a rule, a master agent, or a human master.
Only the last group reaches a person, and this script turns that group
into a readable briefing: one table per study, with the competing values
side by side and the question the human has to answer.

It also writes a machine-readable companion (the same rows as JSON) so the
decisions can be applied programmatically once they are made.

Products
--------
``Results/HUMAN_MASTER_BRIEFING.md``          the briefing sheet
``Results/coder_human_briefing.json``          the same rows, structured
``Results/coder_human_briefing.csv``           the same rows, flat
"""
import csv
import json
import os
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")
QUEUE = os.path.join(RESULTS, "coder_disagreement_human_queue.csv")

#: what the human has to decide for each field
QUESTION = {
    "eligible_primary":
        "Is this contrast the primary eligible intervention-vs-control "
        "comparison?  Only one contrast per experiment may carry "
        "eligible_primary = 1, and it must be the intervention against a "
        "no-misinformation-intervention control.",
    "dv_polarity":
        "Which direction is desirable for this outcome?  1 = a higher score "
        "is better/protective (e.g. discernment), 0 = a higher score is worse "
        "(e.g. belief in a false headline).  The value is reversed before "
        "pooling, so the sign of the pooled effect depends on it.",
    "Fulltext_Exclusion_Criterion":
        "Does the study meet every inclusion criterion?  Leave empty when it "
        "does; otherwise list the unmet criterion (C1 no misinformation "
        "intervention, C2 no control group, C3 not English, C4 not a primary "
        "experimental study).",
}


def dv_rows(key, item):
    """Which DV row(s) the item is about, in the canonical file.

    The same outcome name recurs once per arm pair and per timepoint, so
    the human master needs to know which row is in question.
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
            out.append("record %d, DV_Metrics[%d] — %s | %s | %s"
                       % (i, j, dv.get("comparison_label") or "",
                          dv.get("group1_label") or "",
                          dv.get("group2_label") or ""))
    return out


def main():
    rows = list(csv.DictReader(open(QUEUE, encoding="utf-8-sig", newline="")))
    by_key = defaultdict(list)
    for r in rows:
        by_key[r["study_key"]].append(r)

    json_rows = []
    for key in sorted(by_key):
        for r in by_key[key]:
            try:
                vals = json.loads(r["routes"])
            except Exception:
                vals = {}
            json_rows.append(dict(
                study_key=key,
                scope="dv" if r["kind"] == "dv" else "field",
                field=r["field"], dv=r["dv"],
                question=QUESTION.get(r["field"], ""),
                routes=vals,
                rows=dv_rows(key, r),
            ))

    with open(os.path.join(RESULTS, "coder_human_briefing.json"), "w",
              encoding="utf-8") as fh:
        json.dump(json_rows, fh, indent=1, ensure_ascii=False, default=str)

    with open(os.path.join(RESULTS, "coder_human_briefing.csv"), "w",
              encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["study_key", "scope", "field", "dv", "question",
                    "canonical", "SB", "MM"])
        for r in json_rows:
            v = r["routes"]
            w.writerow([r["study_key"], r["scope"], r["field"], r["dv"],
                        r["question"],
                        json.dumps(v.get("canonical"), ensure_ascii=False),
                        json.dumps(v.get("SB"), ensure_ascii=False),
                        json.dumps(v.get("MM"), ensure_ascii=False)])

    md = []
    md.append("# Human master briefing — coder disagreements\n")
    md.append("Every disagreement between the three coding routes "
              "(canonical / SB / MM) is sorted into one of three "
              "destinations.  A rule settles most of them, a master agent "
              "reads the paper for the rest, and **only the items below "
              "need a person**.\n")
    md.append("\nThe items below are judgements the paper does not dictate: "
              "whether a contrast qualifies as the primary eligible "
              "comparison, which direction of an outcome is desirable, and "
              "whether a study meets the inclusion criteria.  In each case "
              "two routes disagree and there is no majority to fall back "
              "on, so the value cannot be settled by a rule.\n")
    md.append("\n**How to answer.**  For each item, write the value you "
              "decide into the canonical file `Extraction/<Key>.json` "
              "(on every record of that key).  Then re-run "
              "`python Scripts/B_validate.py --dir Extraction`.\n")
    md.append("\n*Note.* `dv_polarity` is coded 1 = higher is better / "
              "0 = higher is worse; `eligible_primary` is 1 = this is the "
              "primary eligible contrast, 0 = not eligible.\n")
    md.append("\n---\n")

    by_field = defaultdict(list)
    for r in json_rows:
        by_field[r["field"] or r["dv"]].append(r)

    for field in sorted(by_field):
        items = by_field[field]
        md.append("\n## %s — %d item%s\n"
                  % (field, len(items), "" if len(items) == 1 else "s"))
        q = QUESTION.get(field, "")
        if q:
            md.append("\n%s\n" % q)
        md.append("\n| study key | outcome row | canonical | SB | MM |")
        md.append("|---|---|---|---|---|")
        for r in sorted(items, key=lambda x: (x["study_key"], x["field"])):
            v = r["routes"]
            def cell(route):
                val = v.get(route, "-")
                s = json.dumps(val, ensure_ascii=False) if not isinstance(val, str) else val
                s = s.replace("|", "\\|").replace("\n", " ")
                return s[:80]
            row = "<br>".join(x[:70] for x in r.get("rows") or []) or "-"
            md.append("| %s | %s | %s | %s | %s |"
                      % (r["study_key"].replace("|", "\\|"), row,
                         cell("canonical"), cell("SB"), cell("MM")))

    md.append("\n---\n")
    flags = []
    fpath = os.path.join(RESULTS, "master_agent_flags.csv")
    if os.path.isfile(fpath):
        with open(fpath, encoding="utf-8-sig", newline="") as fh:
            flags = list(csv.DictReader(fh))
    if flags:
        md.append("\n## Flags raised while the paper class was settled\n")
        md.append("\nThe master agent read the paper for these and reported what it "
                  "found. They are not decisions - each names who owns it.\n")
        md.append("\n| study key | field | what the agent found | who decides |")
        md.append("|---|---|---|---|")
        for f in flags:
            md.append("| %s | %s | %s | %s |"
                      % (f["study_key"].replace("|", "\\|"),
                         (f["field"] or "-").replace("|", "\\|"),
                         f["what_the_agent_found"].replace("|", "\\|"),
                         f["who_decides"]))
        md.append("\n")

    md.append("\n---\n")
    md.append("\n## Summary\n")
    md.append("\n| field | items | studies |")
    md.append("|---|---|---|")
    for field in sorted(by_field, key=lambda f: -len(by_field[f])):
        items = by_field[field]
        md.append("| %s | %d | %d |"
                  % (field, len(items), len({i["study_key"] for i in items})))
    md.append("\n**Total: %d items across %d studies.**\n"
              % (len(json_rows), len(by_key)))

    with open(os.path.join(RESULTS, "HUMAN_MASTER_BRIEFING.md"), "w",
              encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")

    print("items: %d across %d studies" % (len(json_rows), len(by_key)))
    for field in sorted(by_field, key=lambda f: -len(by_field[f])):
        print("   %-34s %d items / %d studies" % (field, len(by_field[field]),
              len({i["study_key"] for i in by_field[field]})))
    print("\nwrote Results/HUMAN_MASTER_BRIEFING.md, coder_human_briefing.{json,csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
