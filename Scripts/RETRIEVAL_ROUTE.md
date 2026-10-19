# Retrieval route — conventions

Recover the numbers behind **uncodeable DVs** and **missing datasets**. One subagent per paper,
model `opencode-go/space-bunny-free`; the task itself is `Scripts/RETRIEVAL_BRIEF.md`.

## Target set (498 studies)
Every `Extraction/*.json` key that either has an uncodeable DV row (non-empty
`missing_metrics_reason`) **or** is missing its dataset (`Results/state.json → datasets.missing`).

## Queue / dispatch
```
python Scripts/retrieval_route.py --status          # queue / pending / in_flight / done / failed
python Scripts/retrieval_route.py --next 24         # JSON list of the next 24 keys
python Scripts/retrieval_route.py --mark "<Key>" --status running|done|failed
python Scripts/retrieval_route.py --pool 24         # {"room": n, "dispatch": [keys]}
```
State: `temp/retrieval/state.json`. A study counts as **done** when `Results/<Key> - retrieval.md`
exists (so a crash that wrote nothing is retried).

## Rules (in the brief, enforced by review)
* **Additive only.** Never modify `Extraction/*`, `Papers/*`, `Text/*`, any
  `Datasets/<Key>/<Key> - harmonized*` / `- prepared*`, `harmonizer.py` or `Reproduction/*`.
  Everything written carries the suffix **` - retrieval`**; raw downloads keep the authors'
  filenames; supplements go to `Supplements/<Key> Supplement N.<ext>`; scratch in
  `temp/retrieval/<Key>/`.
* **No invented numbers.** A bound is written as a bound, never as an effect. A clean
  "none found" is a valid result.
* **Provenance** for every derived cell: file + page/table, in
  `Datasets/<Key>/<Key> - retrieved_dvs - retrieval.csv`.
* **Banking is a separate step**, done by the parent session with the original extraction archived
  first — subagents never edit a canonical coding.

## What the 21-paper pilot established (see `Results/retrieval_pilot_evaluation.md`)
* participant-level deposit found in **1 of 21** (and only via a **PDF link annotation** with a
  view-only OSF token, nested two components deep — not visible in the text);
* **17 eligible rows unlocked** and **14 upgraded** by *derivations*: exact SD from printed binary
  counts, SD from `S.E.×√n`, SD from ANOVA MSB/MSW, vector-figure digitisation, **raster-embedded
  appendix tables (render the page and read it)**, unread appendix pairwise grids, σ̂ from
  digitised cell means;
* a **stale blocker** is a real failure mode — `Ku et al. (2026)`'s coding says "this PDF contains
  a different article" because the file was replaced *after* the coding froze;
* so the brief weights, in order: (1) re-read the material we already hold (figures, raster pages,
  appendices, link annotations), (2) derive exact SD/SE from printed counts/df/figures,
  (3) search the web last.
