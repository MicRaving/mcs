"""State of the campaign.

Reports (report level, not record level -- see `Scripts/prisma_report.py`):

  - the PRISMA flow (records identified -> duplicates removed -> screened ->
    abstract excluded by reason -> sought -> assessed -> full-text excluded by
    reason -> included studies), with the PRISMA identities checked,
  - how the remaining `Unscreened` records were resolved
    (`temp/unscreened_resolution.csv`),
  - the number of full texts acquired and coded,
  - the number of datasets reproduced and harmonized,
  - the number and names of datasets still missing.

Writes `state.json` (and prints).

Run:  python state.py
"""
import glob
import json
import os
import sys

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = os.path.dirname(os.path.abspath(__file__))     # Scripts/analysis
SCRIPTS = os.path.dirname(_HERE)                       # Scripts
B = os.path.dirname(SCRIPTS)                           # project root
RESULTS = os.path.join(B, "Results")
os.makedirs(RESULTS, exist_ok=True)
TEMP = os.path.join(B, "temp")
sys.path.insert(0, SCRIPTS)

os.makedirs(TEMP, exist_ok=True)
try:
    import tempfile

    tempfile.tempdir = TEMP
except Exception:  # pragma: no cover
    pass

import prisma_report  # noqa: E402

MASTER = os.path.join(B, "master_dataset.xlsx")


def keys_of(pattern_dir, suffix):
    return {os.path.splitext(os.path.basename(p))[0]
            for p in glob.glob(os.path.join(B, pattern_dir, suffix))}


def unscreened_resolution():
    """Summarise temp/unscreened_resolution.csv if the Phase-1 pass has run."""
    path = os.path.join(TEMP, "unscreened_resolution.csv")
    if not os.path.exists(path):
        return {}
    r = pd.read_csv(path, dtype=str).fillna("")
    out = {
        "rows": int(len(r)),
        "abstract_excluded": int((r["effective_verdict"] == "Abstract excluded").sum()),
        "fulltext_excluded": int((r["effective_verdict"] == "Exclude").sum()),
        "included": int((r["effective_verdict"] == "Include").sum()),
        "by_decisive_criterion": {
            str(k): int(v) for k, v in r["failed"].replace("", "(none)").value_counts().items()
        },
        "by_criterion": {
            str(k): int(v) for k, v in r["criterion"].value_counts().items()
        },
    }
    return out


def main():
    coded = keys_of("Extraction", "*.json")
    acquired = keys_of("Papers", "*.pdf")
    # a ` - SB` reproduction report is the Space-Bunny route, not a canonical reproduction
    reproduced = {k for k in keys_of("Reproduction", "*.md") if not k.endswith(" - SB")}
    # a Datasets/<Key>/ folder counts as "present" only when it holds a canonical (non-` - SB`)
    # file; an SB-only folder must not hide a missing dataset nor count as a study
    ds_dirs = set()
    _AGENT_DOC = {"SOURCE.md", "ACQUISITION_REPORT.md", "HARMONIZATION_MISMATCH.md",
                  "HARMONIZER_FIX.md"}
    for d in glob.glob(os.path.join(B, "Datasets", "*")):
        if not os.path.isdir(d):
            continue
        # Only a canonical file makes a Datasets/<Key>/ folder count as "present".  The
        # Space-Bunny (` - SB`) and retrieval (` - retrieval`) routes are additive side
        # routes: their products must not mask a genuinely missing dataset, nor invent one.
        if any(os.path.isfile(os.path.join(d, f)) and " - SB" not in f
               and " - retrieval" not in f and f not in _AGENT_DOC
               for f in os.listdir(d)):
            ds_dirs.add(os.path.basename(d))
    harmonized = {os.path.basename(os.path.dirname(p))
                  for p in glob.glob(os.path.join(B, "Datasets", "*", "* - harmonized.csv"))}

    flow = {}
    if os.path.exists(MASTER):
        master = prisma_report.load_master(MASTER)
        flow = prisma_report.prisma_flow(master)

    state = {
        "prisma": flow,
        "unscreened_resolution": unscreened_resolution(),
        # ---- report-level reason tables (back-compatible keys) ------------ #
        "abstract_screening_exclusions_by_reason": flow.get("abstract_excluded_by_reason", {}),
        "fulltext_exclusions_by_reason": flow.get("fulltext_excluded_by_reason", {}),
        "intervention_exclusions_by_reason": {},
        "fulltexts": {"acquired": len(acquired), "coded": len(coded)},
        "datasets": {
            "reproduced": len(reproduced),
            "harmonized": len(harmonized),
            "present": len(ds_dirs),
            "missing": sorted(coded - ds_dirs),
        },
    }
    state["datasets"]["missing_count"] = len(state["datasets"]["missing"])

    json.dump(state, open(os.path.join(RESULTS, "state.json"), "w", encoding="utf-8"),
              indent=2, ensure_ascii=False)
    print(json.dumps(state, indent=2, ensure_ascii=False))
    print("\nwrote", os.path.join(RESULTS, "state.json"))


if __name__ == "__main__":
    main()
