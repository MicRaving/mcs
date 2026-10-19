# Space Bunny (SB) coding route

A second, independent coding route produced by **`opencode-go/space-bunny-free`**, run in parallel to
the canonical route, for **reliability / inter-coder agreement**.  Nothing in the SB route ever
overwrites a canonical artefact; every SB artefact carries a ` - SB` suffix.

## Where the SB artefacts live

| layer | canonical | **SB route** |
|---|---|---|
| Extraction (coding) | `Extraction/<Key>.json` (+ `.md`) | **`Codings/<Key> - SB.json`** (+ `Codings/<Key> - SB.md`) |
| Harmonizer | `Datasets/<Key>/harmonizer.py` | **`Datasets/<Key>/harmonizer - SB.py`** |
| Harmonized output | `Datasets/<Key>/<Key> - harmonized.csv` (+ `- harmonized_variables.csv`, `- exclusions.csv`, `- conditions.csv`) | **`Datasets/<Key>/<Key> - harmonized - SB.csv`** (+ `- harmonized_variables - SB.csv`, `- exclusions - SB.csv`, `- conditions - SB.csv`) |
| Reproducer | `Datasets/<Key>/<Key> - reproduction.py` + `Reproduction/<Key>.md` | **`Datasets/<Key>/<Key> - reproduction - SB.py`** + **`Reproduction/<Key> - SB.md`** |

`Codings/<Key> - SB.json` follows the coder-slot convention already used by
`Codings/<Key> - MF.json` (the SR coder): the app's agreement/IRR screens read coder files from
`Codings/`, so SB becomes **a new coder** automatically once its files exist.

## Rules for every SB agent

1. Work on **one** study key; never touch another study's files or any canonical artefact.
2. Code per `Scripts/SB_CODING_INSTRUCTIONS.md` (schema v3.0) — no invented numbers; every effect
   sourced or a non-empty `missing_metrics_reason`; never a multi-df omnibus as the
   `eligible_primary == 1` effect.
3. The SB harmonizer/reproducer read **only** the raw deposit in `Datasets/<Key>/` (never a
   canonical `- prepared`/`- harmonized` file) and write only the `- SB` outputs.
4. Scratch files go to `temp/sb/<Key>/` (unique namespace; never a generic `temp/<name>`).
5. Reply with the study key and the artefacts written.

## Merge / agreement

`Scripts/sb_route.py` owns the queue + prompts; `Scripts/sb_merge.py` consolidates the SB route:

* **Extraction** — per-DV agreement between `Extraction/<Key>.json` and `Codings/<Key> - SB.json`
  (experiments, DV rows, `eligible_primary`, domains, polarity) →
  `Results/sb_coding_agreement.csv` + a summary.
* **Harmonized** — shape and DV-set comparison between the canonical and `- SB` wide files →
  `Results/sb_harmonization_agreement.csv`.
* **Reproduction** — SB verdict vs the canonical `Results/reproduction_summary.xlsx` status.

## Dispatch

`python Scripts/sb_route.py --next 24` prints the next queued study keys (state in
`temp/sb/state.json`).  The parent session spawns `opencode-go/space-bunny-free` subagents for them
and calls `python Scripts/sb_route.py --mark <Key> --status running|done|failed` as they complete;
the pool is topped up to 24 in flight.  `Scripts/sb_route.py --dispatch-prompt` prints the
self-refill instruction used by the scheduled task.
