# AGENTS.md — Misinformation-intervention meta-analysis

Workspace root: `C:\Users\marvi\Nextcloud\Studium\Studies\Inoculation_Meta\meta`

## What this project is
A meta-analysis and re-analysis of experiments that test **misinformation interventions**
(inoculation, sourcing / lateral reading, media-literacy tips, fact-checking, debunking,
accuracy prompts, friction, social norms, source-credibility labels, warning labels, community
correction, platform exposure reduction, open-mindedness). Papers are screened, coded to a
schema, their public datasets are acquired, harmonized, reproduced, and pooled.

## Folder contract (root)
```
Datasets/<Study Key>/     derivation sources + codebooks + `harmonizer.py` + products
                          (`<Key> - prepared*.csv`, `<Key> - harmonized*.csv`,
                          `<Key> - variables.csv`, `- dv_direction.csv`, `- exclusions.csv`,
                          `- conditions.csv`, `- variable_assignments.csv` (coder assignments),
                          `- repro_results.csv` + `- repro_facts.json` (durable reproduction
                          facts, see D10)); data no derivation step reads is archived
                          (see "Datasets hygiene" below)
Papers/<Study Key>.pdf    the included paper
Text/<Study Key>.txt      extracted text of that paper
Supplements/              supplementary material, one canonical home per study:
                          `<Key> Supplement N.pdf|docx|html|...` (never inside `Datasets/`)
Reproduction/<Study Key>.md   reproduction report (exclusions, discrepancies, results)
Extraction/<Study Key>.json   schema-v3 coding of the paper (one object per experiment)
Extraction/<Study Key>.md     coding decisions / notes
Project/                  the project's own configuration — one scheme per project:
                          project.json (the manifest), scheme.json (the coding scheme),
                          criteria.json (abstract + full-text exclusion criteria),
                          bundles/ (generated agent bundles), history/ (scheme revisions)
Scripts/                  essential Python used by the campaign
Scripts/analysis/         the analysis pipeline (metaanalysis.py, reanalysis.py, state.py,
                          run_harmonizers.py, run_reproducers.py)
run_analysis.py           one entry point that runs the analysis pipeline (see below)
master_dataset.xlsx       the master screening dataset (abstract + full-text verdicts)
Results/                  EVERY analysis product (metaanalysis.xlsx, reanalysis.xlsx,
                          reanalysis_all.csv, state.json, harmonization_mapping.xlsx,
                          reproduction_summary.xlsx, combined_harmonized.csv.gz,
                          Analysis_Suite_Results.xlsx, analysis_suite.xlsx, figures/, ...)
                          plus the audit/validation products: analysis_log.jsonl,
                          acquisition_log.jsonl, acquisition_attempts.csv,
                          reproduction_report_audit.csv, master_reproduction_sheet.xlsx,
                          extraction_validation.csv, extraction_needs_coding.csv,
                          extraction_autofix_log.csv, sanity_report.xlsx, text_quality.csv,
                          harmonizer_audit.csv, polarity_audit.csv, audit_summary_*.md,
                          MANUAL_DECISIONS_NEEDED.md, IMPLEMENTATION_STATUS.md,
                          harmonization_coverage.csv, harmonization_coverage_summary.csv,
                          assignment_audit.csv, variable_assignment_log.jsonl,
                          repro_dossier/ (per-study JSON + results CSV), repro_dossier_index.csv,
                          repro_dossier_validation.csv, repro_logs/, extraction_source_location_*.csv,
                          PLAN_*.md, INTERFACES_*.md, TRACK_*_CHANGES.md
temp/                     ALL temporary/intermediate files go here (never the root)
archive/                  everything not part of the current contract, for review/deletion
```

Nothing is written to the project root except `master_dataset.xlsx`, `MR_Preregistration.*`,
`CodingStudio.exe` / `CodingStudio/` (the built desktop app; onedir by default — keep `_internal/`
beside the exe), `run_analysis.py` and `AGENTS.md`; every analysis product lands in `Results/`.
The one exception is the `Project/` folder, which holds the project's own configuration
(the scheme, the criteria, the manifest) and is meant to be versioned with the code.

## The coding scheme (project = folder, one scheme per project)

A **project** is a folder with the layout above plus a `Project/` folder.  The project
carries exactly one scheme, so a second corpus is a second folder that the app can open
(`POST /api/root`, the "project folder" button).  The scheme is read through
`Scripts/project_context.py`, which is the single place that answers "which project is
active?" — the desktop app, the analysis pipeline and the agent tooling all go through it,
so they can never disagree.

* `Scripts/scheme_lib.py` — the scheme library: what a scheme *is* (blocks, fields, roles,
  conditions, kinds, rules), the normaliser, the block derivation, the condition evaluator
  and the scheme validator.
* `Scripts/scheme_legacy.py` — the converter from the legacy misinformation schema (v3.1,
  `Scripts/coding_web/schema.json`) into a scheme.  The conversion is **name-preserving**:
  every field keeps its v3.1 name, so the 792 `Extraction/<Key>.json` and the 3 148
  `Codings/<Key> - <Coder>.json` stay valid.
* `Scripts/project_context.py` — the active project, its scheme, its criteria, and the
  role lookups (`value_by_role`).

The **role** is the load-bearing idea: the pipeline asks "what is the value of the
`eligible_primary` field?" and never asks "what is the value of `eligible_primary`?".
Field names are user data; roles (`scheme_lib.ALL_ROLES`) are the contract.  The legacy
alias table `scheme_lib.LEGACY_ROLE_ALIASES` is one-to-one by construction — the scheme
validator fails the build when a name would claim two roles.

Regenerate the project's scheme from the legacy schema with
`python Scripts/convert_legacy_schema.py` (it refuses to overwrite without `--force`);
`python Scripts/convert_legacy_schema.py --check` proves the conversion is lossless.
`Scripts/validate_scheme.py` is the gate: scheme structure, role integrity, corpus
conformance and the legacy-fallback check.

## Naming convention (canonical)
Every artifact is named by its **study key**: `Surname et al. (Year)` (3+ authors),
`Surname & Surname (Year)` (2), `Surname (Year)` (1); collisions within a year get ` (Year a)`,
` (Year b)`. The same key is used across `Papers/`, `Text/`, `Extraction/`, `Reproduction/`,
`Datasets/`. Fix deviations with `python Scripts/fix_names.py` and audit with
`python Scripts/name_audit.py` (legitimate lowercase particles like `van`/`von`/`de` and
organisational authors are expected non-"canonical" matches).

## Pipeline
1. **Code** each paper into `Extraction/<Key>.json` (+ `.md`) against the `Sandbox` schema v3.0.
   The schema is materialised for the web coder as `Scripts/coding_web/schema.json` (rebuilt with
   `python Scripts/build_coding_schema.py`) and every coding is checked with
   `python Scripts/B_validate.py --dir Extraction`.
   Every coding also sets the common field **`Fulltext_Exclusion_Criterion`**: leave it **empty**
   when the study meets all inclusion criteria, otherwise list the unmet one(s) — **C1** no
   misinformation intervention, **C2** no control/comparison group, **C3** not English, **C4** not a
   primary experimental study.  A study that fails a criterion is **still fully coded, harmonized
   and reproduced**; the field only keeps the screening layer / PRISMA flow consistent.
   - **Intervention-vs-control contrasts — encode in this preference order.** (a) the
     per-condition descriptives `n/M/SD` (preferred, always poolable); (b) the reported
     *pairwise-vs-control* statistic (`t`, or `F` with `df_num == 1`, or `r`, or a reported
     `d`/`g`); (c) **never** a multi-df omnibus `F`/`chi2`/`eta2` (`df_num > 1`) or a
     `contrast_type = 11` (`omnibus_model_term`) term as the `eligible_primary == 1` effect.
     A multi-df omnibus compares more than two cells at once, cannot be signed, and must not be
     converted to a single two-arm `d`. If only an omnibus is reported, **search the same
     paragraph and the referenced table** for pairwise comparisons or per-condition means/SDs
     before falling back; if none exists, set a non-empty `missing_metrics_reason` that names the
     paragraph/table searched. The pipeline guard (`suite_common.meta_effect` /
     `metaanalysis.effect`, predicate `suite_common.is_omnibus_eligible`) refuses such a row, and
     `validate_extraction.py` queues it under `OMNIBUS_ONLY_ELIGIBLE` / `OMNIBUS_AS_ELIGIBLE`.
2. **Acquire** the study data into `Datasets/<Key>/` (raw originals + codebooks + analysis
   scripts). Originals are never overwritten; once the dataset is derived + harmonized, prune it
   to the derivation sources per **Datasets hygiene** below.
3. **Harmonize** — `Datasets/<Key>/harmonizer.py` writes a **WIDE** `<Key> - harmonized.csv`
   (one row per participant/session) plus `<Key> - harmonized_variables.csv`. Rules:
   - uniform DV names `dv_<domain>[_<veracity>][_<time>]`; the sidecar records `veracity`
     (true/false stimuli) and `timepoint` (pre/post/follow-up);
   - uniform covariate names `cov_*`;
   - every numeric variable also gets a `<name>_01` column linearly rescaled to 0–1 within its
     own observed range, so a 1–7 scale cannot dominate a 0–1 scale.
   - author exclusion/attention checks are part of the harmonization: retain them as `flag_*`
     columns (verbatim, **no** `_01` twin) and write `<Key> - exclusions.csv`
     (`flag, keep, applies_to, reason`) stating which DVs each check gates (`*` = all,
     `dv_*_source_*` globs, or `domain:<dv_domain>`); no exclusions ⇒ header-only file.
   - every DV is oriented so **higher = more desirable** (more discernment, more credibility for
     true news, less misinformation sharing, more analytic processing): record its orientation in
     the sidecar as `dv_polarity` (`higher_is_better` / `higher_is_worse`); `reanalysis.py`
     (`Scripts/analysis/`) reverse-codes the `higher_is_worse` DVs (value → 1 − value) before pooling.
4. **Merge stays WIDE** — study data is never melted for the merge. The full union (~19k column
   names × 2.26M rows) is *not* materialised; `reanalysis_all.csv` is a catalogue of the wide
   datasets. A single **combined wide table** is also written with only the columns present in
   **≥ 3 studies** (`combined_harmonized.csv.gz` + `combined_harmonized_variables.csv`, built by
   `Scripts/combine_harmonized.py`, run automatically by `reanalysis.py`; ids + `dv_*`/`cov_*`
   raw and `_01` + `flag_*`), carrying a `study_key` column that names the source dataset.
   The gzip CSV is readable by base R (`read.csv("combined_harmonized.csv.gz")`); a parquet
   twin is available with `--parquet` for `arrow::read_parquet()`.
5. **SDT re-analysis** — only `reanalysis.py` melts the `dv_*_01` columns, **without covariates**,
   reverse-codes `dv_polarity = higher_is_worse` DVs (so positive = better), applies every study's
   `<Key> - exclusions.csv` rules (per-DV row filters on the `flag_*` columns), splits participants
   by the harmonized arm column (`condition`, falling back to `condition_raw`) into the
   intervention arm and the no-misinformation-intervention control arm (roles from the study's
   `<Key> - conditions.csv` when present, else the flagged heuristic in
   `Scripts/suite_common.classify_condition`), and pools the **intervention-vs-control effect on
   discrimination** (`d′_treatment − d′_control`, `d′ = z(HR) − z(FAR)`) with a random-effects
   (DerSimonian–Laird) model into `reanalysis.xlsx`. The arm-pooled discrimination **level** is
   kept as the auxiliary `Discrimination_level` sheet. Studies/cells without a control arm are
   excluded from the intervention effect and counted in `RunAudit`.
6. **Meta-analysis** — `metaanalysis.py` flattens `Extraction/*.json`, derives one effect per
   contrast (reported `yi`/`vi`, `d`/`g`, `t`, `F`, `r`, or means/SDs), and runs a random-effects
   model on `eligible_primary == 1` contrasts, overall and per DV domain.
   **Three separate questions are reported, never pooled together:** (1) immediate posttest
   (effectiveness), (2) follow-up only (persistence), (3) pretest-corrected DiD (pre→post). Never
   pool follow-up with the immediate posttest and never treat a pretest/baseline row as an outcome.
7. **State** — `Scripts/analysis/state.py` writes `Results/state.json` (abstract/full-text
   exclusions by reason, full texts acquired/coded, datasets reproduced/harmonized, missing datasets).
8. **Audit & gates** — `run_analysis.py` also wires `persist` (`Scripts/persist_repro_artifacts.py`),
   `dossier` (`Scripts/build_repro_dossier.py` -> `Results/repro_dossier/`), `coverage`
   (`Scripts/harmonization_coverage.py` -> `Results/harmonization_coverage.csv`), a report-only
   `audit` step (harmonizer/polarity/assignment/dossier/report/sanity/extraction validators; a
   non-zero exit is a WARNING) and a hard `gate` step (`validate_polarity`, `validate_fully_within`,
   `B_validate --dir Extraction`, `parity_check`). `python run_analysis.py --validate` runs the core
   pipeline plus both; `Scripts/parity_check.py` is the single-source-of-truth acceptance test (the
   app, `publication_bias` and the canonical workbooks must agree to 1e-9).
9. **Harmonizer assignments** — coders label missing variables in
   `Datasets/<Key>/<Key> - variable_assignments.csv` (drafts in `Codings/`), applied by the runners'
   post-hook via `Scripts/variable_assignments.py`; `Scripts/audit_assignments.py` checks parity.
   Coverage gaps are listed by `Scripts/harmonization_coverage.py` (11 essential slots).

## Conventions
- **Temporary files**: every script MUST write intermediates to `temp/` (root-level), never the
  project root or a study folder; every analysis product goes to `Results/`. The analysis scripts
  set `tempfile.tempdir` to `temp/`; per-study `harmonizer.py` does the same (`<root>/temp`).
  **Never read or write under `Sandbox/`** — that tree is curated by a different session. Namespace
  your own intermediates as `temp/<owner>/` (a unique owner name, never a generic filename such as
  `temp/validate_extraction.py`); per-study writes go only to the real `Extraction/<Key>.{json,md}`
  (archive the original first).
  `__pycache__` is regenerated by Python and can be redirected with
  `PYTHONPYCACHEPREFIX=<root>/temp/pycache`.
- **Never overwrite** non-empty harmonized files, papers or extractions, and never delete another
  process's files — move unwanted items to `archive/` (recoverable). Archived **source** data must
  be restored before a dataset can be re-derived; see "Datasets hygiene".
- **No invented numbers** in coding: every effect is sourced, else a non-empty
  `missing_metrics_reason`. DVs carry `contrast_type` and `eligible_primary` (1 = intervention vs
  a no-misinformation-intervention control).
- **pandas ≥ 3**: `to_excel` must use the `sheet_name=` keyword (positional args changed).

## Space Bunny (SB) reliability route

A second, independent coding route produced by `opencode-go/space-bunny-free`, run in parallel for
reliability/IRR.  It is purely **additive** — every SB artefact carries a ` - SB` suffix and no
canonical artefact is ever overwritten:

| layer | SB artefact |
|---|---|
| coding | `Codings/<Key> - SB.json` (+ `.md`) — the coder slot, so the app/IRR reads SB as a new coder |
| harmonizer + products | `Datasets/<Key>/harmonizer - SB.py` → `<Key> - harmonized - SB.csv` (+ `- harmonized_variables - SB.csv`, `- exclusions - SB.csv`, `- conditions - SB.csv`) |
| reproducer | `<Key> - reproduction - SB.py` + `Reproduction/<Key> - SB.md` |

Conventions in `Scripts/SB_ROUTE.md`; coding rules in `Scripts/SB_CODING_INSTRUCTIONS.md`.
Queue/dispatch: `python Scripts/sb_route.py --status|--next N|--prompt "<Key>"|--mark "<Key>" --status running|done|failed`
(state in `temp/sb/state.json`; the scheduled task *SB pool refill* tops the pool back up to 24).
Merge/agreement: `python Scripts/sb_merge.py` → `Results/sb_coding_agreement.csv`,
`Results/sb_harmonization_agreement.csv`, `Results/sb_route_summary.json`.  The survey/analysis
scripts ignore ` - SB` artefacts (`state.py`, `parse_reproduction_reports.py`,
`build_repro_dossier.py`).

## MM (MiniMax) coding route

A third, independent coding route produced by `opencode/minimax-m3.1-flash-free#max` (max
thinking), used for the same reliability/IRR purpose as SB.  Like SB it is purely **additive** —
every MM artefact carries a ` - MM` suffix and no canonical artefact is ever overwritten:

| layer | MM artefact |
|---|---|
| coding | `Codings/<Key> - MM.json` (+ `.md`) — the coder slot, so the app/IRR reads MM as a new coder |

Coding rules in `Scripts/MM_CODING_INSTRUCTIONS.md`.  Every DV carries
`source_span`/`source_quote`/`source_location` and every non-empty record field carries a
`source_spans` entry; both are checked by `Scripts/B_validate.py` and
`Scripts/mark_source.py --verify --fields`.
Queue/dispatch: `python Scripts/mm_route.py --status|--next N|--prompt "<Key>"|--mark "<Key>" --status done|failed|--live|--unlive`
(state in `temp/mm_route/state.json`, mirrored to `temp/mm_route_state_mirror.json`).
Wave audit: `python Scripts/mm_verify_all.py` → `Results/mm_coding_audit.csv`.
Merge/agreement: `python Scripts/mm_merge.py` → `Results/mm_coding_agreement.csv`,
`Results/mm_route_summary.json`.  The survey/analysis scripts ignore ` - MM` artefacts.

**The route is finished** — 792/792 keys coded, 0 queued, 0 failed, 0 blocked; the last wave
audit found 21 622 DV rows, 61 798 record fields, 0 mark problems, 0 failing files, 0
`Study_ID` collisions, 0 invalid exclusion values.  The two scheduled pool-refill tasks are
**stopped**: "Retrieval pool refill" is disabled, and "MM pool refill" is parked on a one-shot
date (2035-01-01) because the OpenChamber scheduler drops the `disabled` boolean before it
reaches the tool (`schedule.toggle` rejects it with *"disabled must be a boolean"* for both
`true` and `false`, while `name`, `prompt` and `cron` update fine).  **To re-arm the MM pool**
after new papers are screened into `Extraction/`: set that task's schedule back to a cron
(`*/25 * * * *`) and run `python Scripts/mm_route.py --housekeeping` then `--next 20`.

Two invariants the MM route must hold, both checked by `mm_verify_all.py`:
* no `Study_ID` is shared by two study keys (`Study_ID` is the IRR join key) — fix a collision
  with `python Scripts/mm_align_study_ids.py --apply`;
* `Fulltext_Exclusion_Criterion` carries only `C1`–`C4` or nothing.

**Coder-agent rules that matter.**  An agent may delete **only** its own scratch folder
`temp/mm/<Key>/`; the router's state lives in `temp/mm_route/` and must survive.  A coding
agent must not read `Extraction/<Key>.*` or any other `Codings/<Key> - *.json` (another coder's
answers) and must not grep across `Extraction/` or `Codings/`.

## Corresponding author

Every paper carries the contact the paper directs correspondence to, in **two separate
fields** (name and address are not one blob) plus a provenance note:

| field | meaning |
|---|---|
| `Corresponding_Author_Name` | the person, e.g. `"Jane Roe"`; `null` when the paper names no corresponding author |
| `Corresponding_Author_Email` | the address as printed in the paper; `null` when none is printed or none resolves to the corresponding author |
| `Corresponding_Author_Source` | where it was read from, e.g. `"pdf: correspondence note, p. 1"`, `"openalex: corresponding_author_ids"`, `"paper: no corresponding author named"` |

All three are record-level fields on **every** record of `Extraction/<Key>.json`.  They are in
the schema (`Scripts/coding_web/schema.json`, group *Metadata & Identification*) and are added
by `Scripts/build_coding_schema.py`'s campaign overrides, so they survive a schema rebuild.

Harvest order (`Scripts/code_corresponding_author.py`): the paper's own correspondence note
→ the address' local part matched against the author list → OpenAlex
`corresponding_author_ids`.  A candidate is only written when the **name is one of the
paper's own authors**, so an editor, production office, repository mailbox or IRB contact
can never be mistaken for the contact.  What the harvest cannot resolve goes to an agent
(`Scripts/corresponding_author_queue.py`); what no agent can resolve stays `null` with an
explicit source — a paper that prints no address has no address.

Never invent an address from a domain pattern.  `null` + `"paper: no corresponding author
named"` is the correct answer for the many theses, preprints and blind submissions in the
corpus.

## Coder disagreements (canonical vs SB vs MM)

The three routes code the same corpus independently, so they disagree.  Every difference is
sorted by **what settling it requires**, never by how often it happens:

| destination | meaning | who |
|---|---|---|
| **rule** | two of the three routes carry the same value | a script (`Scripts/apply_coder_majority_fixes.py`) |
| **master agent** | the paper's wording settles it (experiment count, a field the paper states, a row's taxonomy, how complete an enumeration is) | an agent reads the paper (`Scripts/build_master_agent_worklist.py` → `temp/mm_route/master_batches/`) |
| **human master** | a judgement the paper does **not** dictate, with no majority: `eligible_primary`, `dv_polarity`, `Fulltext_Exclusion_Criterion` | a person (`Results/HUMAN_MASTER_BRIEFING.md`) |

Pipeline:
```
python Scripts/tier_coder_disagreements.py     # what differs, per key
python Scripts/split_coder_disagreements.py    # rule / agent / human
python Scripts/apply_coder_majority_fixes.py   # apply the rule  (--apply)
python Scripts/build_master_agent_worklist.py  # the agent's work
python Scripts/master_agent.py --next          # hand out the next batch
python Scripts/build_human_master_briefing.py  # the human's sheet
python Scripts/compare_irr_baseline.py         # did the IRR move?
python Scripts/check_disagreement_resolution.py  # is the rule class closed?
python Scripts/write_coder_disagreement_report.py
```
The master-agent pass is **resumable**: `Scripts/master_agent.py` keeps its queue in
`temp/mm_route/master_state.json` (mirrored to `temp/mm_route_master_state_mirror.json`), hands
out one batch at a time (`--next`, `--next --all`) and records completion
(`--mark <batch> --set-status done|failed|running`).  A rate limit, a crash or the end of a
session costs at most one batch.  Its progress is reported by
`Scripts/check_disagreement_resolution.py` and appears in the report's methods section
("the paper class … *N of M batches settled so far*"), so the report always shows the
state of the pass rather than a snapshot of it.

**Never dispatch a batch that is already running.**  Two agents editing the same study key
race, and each overwrites the other's field values; one such race was caught mid-pass and
had to be adjudicated by hand.  `--next` therefore skips the batches marked `running`
(`--include-running` overrides that, but only after checking no agent is still on them).
Mark a batch `running` the moment it is handed out, and `done` when it lands.

**A master agent must not read `Results/coder_*queue*.csv`.**  Those files carry the three
routes' values for *every* key, so opening one to look up a field definition would expose
other keys' route values and bias a judgement that is supposed to come from the paper (one
agent did exactly this, and disclosed it).  The brief therefore carries the field
vocabularies and the key's own items inline, and says not to open the queue files.

**A mis-coded `effect_size_type_reported` silently drops rows from the pool.**  The
standardisation guard reads `none` as "not standardised" and refuses the row, so a row can
be missing from a pooled product with nothing in the logs to say why.  The master pass
found six such rows in `Bråten et al. (2019)`; correcting the type to the paper's put them
back (the persistence pool moved 381 -> 387 contrasts).

The master agent reads the paper and writes the **canonical** route only; it must not touch
`Fulltext_Exclusion_Criterion`, `eligible_primary`, `dv_polarity`, `n_arms`,
`control_group_type`, `Fully_Within` or any numeric effect field (`yi`, `vi`, `d_val`,
`g_val`, `t_val`, `F_val`, `n1i`, `n2i`, `m1i`, `m2i`, `sd1i`, `sd2i`, `df_num`, `df_den`) —
those are the human master's.  It must also leave a field **empty** rather than fill it with
0 when the paper does not state the thing, because an empty value can mean "not stated".

Products: `Results/coder_disagreement_tiers.{json,csv}`, `coder_disagreement_human_queue.csv`,
`coder_disagreement_agent_queue.csv`, `coder_master_worklist.{json,csv}`,
`coder_majority_fixes.csv`, `Results/HUMAN_MASTER_BRIEFING.md`, `Results/CODER_DISAGREEMENTS.md`.

Rules that matter:
* **Never overwrite a route.**  The SB and MM routes are the IRR evidence; the canonical route
  is the analysis route.  The reconciliation writes to the canonical route only, and the
  pre-reconciliation agreement products are frozen to `Results/irr_baseline_<date>/` **first** —
  otherwise the IRR measurement becomes circular.
* **Match DV rows by position inside a `dv_name`, not by a composite key.**  A `dv_name`
  repeats once per arm pair / timepoint / condition (1,865 names repeat in one route); the
  composite-key matching silently drops 1,129 of the 1,131 real DV conflicts.
* **A list-subset relation is not a majority.**  One route's list being a subset of another's
  is a completeness question and goes to the master agent, never to the script.
  (Two routes carrying the *same* longer list against a shorter third route is exactly this
  case — e.g. `Apuke et al. (2023)`, where SB and MM both name three intervention types and
  the canonical route names one.)
* **Every "paper" rule reaches the master agent, including "paper: completeness".**  The
  worklist builder must not skip a rule class the triage routed to the agent: a skipped
  class is a class that is silently never settled.  `Intervention_Type` is read by the
  analysis, so it belongs to the *pool* bucket of the worklist even though it is a coding
  field.
* **A blank is not always "no".**  Several fields use an empty value to mean "not stated" or
  "unclear" (`code_available`, `consent_reported`, `WEIRD_Rich`), so a gap the canonical route
  left is the master agent's job, not the script's.
* **Never majority-vote a fact or a judgement.**  `N_Analyzed`, `Gender_Ratio`, `n_arms`,
  `control_group_type`, `Fulltext_Exclusion_Criterion` and the effect fields stay with the
  human/agent path.
* **Compare per record, aligned on `Study_ID`.**  A field can differ between the records of one
  paper (`Intervention_Type` in 51 files, `Setting` in 38).  The routes spell the *same*
  experiment's id differently (`Amar2026_Exp1` vs `AMAR2026_Exp1`), so the comparison must
  fall back to a spelling-insensitive id and then to position — an exact match alone
  silently skips the record-level comparison for **270 of the 792 keys**.
* **A blank `Fulltext_Exclusion_Criterion` is ambiguous** — it may mean "meets every inclusion
  criterion" or "this route did not record one" — so a criterion recorded by only one route is
  a paper question, and two different sets of recorded criteria is a human question.
* **Never write to a key a master agent is editing.**  `apply_coder_majority_fixes.py` defers
  the keys held by a running batch (the state says which); `--include-running` overrides that
  and races.

### Audits the pass produced

| script | what it finds |
|---|---|
| `Scripts/check_disagreement_resolution.py` | the state of the four classes; **fails** when a rule-settable disagreement is still pending.  Runs as the `disagreements` gate |
| `Scripts/check_rationale_consistency.py` | `Rationale_Notes` prose that states a value the record no longer has (the note is not updated when a later pass changes the field) |
| `Scripts/check_experiment_alignment.py` | keys where the routes code a different *number* of experiments, or the same experiment id carries a different N |
| `Scripts/find_duplicate_trials.py` | publications that may report the **same trial** — 45 candidate pairs over 48 keys.  The meta-analysis clusters by `study_key \| experiment_id \| control_arm`, so it **cannot** cluster across papers: a trial published three times is counted three times.  Some are already handled by `Scripts/study_exclusions.py`; the rest need a campaign-owner decision about which outcomes are unique |

## Datasets hygiene

Keep in `Datasets/<Key>/` **only** what the dataset is derived and harmonized from:

* the **source files** read by `harmonizer.py`, `<Key> - prepare*.py` and
  `<Key> - reproduction.py` (match the filename, the relative path, *and* the part after
  `<Key> - `, because scripts often build paths as `KEY + " - dataset.xls"`);
* our **products**: `- prepared*.csv`, `- harmonized*.csv`, `- harmonized_variables.csv`,
  `- variables.csv`, `- dv_direction.csv`, `- exclusions.csv`, `- conditions.csv`,
  `- variable_assignments.csv` (coder variable assignments/labels), `- repro_results.csv`,
  `- repro_facts.json` (durable per-study reproduction facts);
* **codebooks / readmes / the paper / survey instruments / variable maps**.

Archive everything else to `archive/_datasets_nonsource_<date>/` (recoverable): raw data that no
derivation step reads, the authors' derived outputs, geographic/auxiliary data, rendered analysis
HTML, and byte-identical duplicate copies. Supplementary documents go to `Supplements/`
(`<Key> Supplement N.<ext>`), never inside a dataset folder.

Rules of engagement:
1. Gate — only touch a dataset once it is **harmonized and reproduced/partial**.
2. Skips — leave any dataset whose `harmonizer.py` **globs** for inputs (e.g. `*.sav`, `*.csv`);
   a name-based test is unreliable there.
3. Verify after every pass — re-run the affected harmonizers in a temp copy (shape unchanged) and
   the full `Scripts/analysis/run_reproducers.py` (0 crashes). A reproduction script that reads a
   raw file it cannot find is the signal that a needed source was archived.
4. `Datasets/` never contains supplements, and raw data is never silently deleted — only archived.

## Known limitations / open items
- `veracity` (true/false) is decoded from uniform DV names; for studies whose DV names do not
  encode stimulus veracity it is blank and no SDT discrimination can be computed. Fill these from
  the coding JSON (`discernment_defined_as` / `score_types`) on a future pass.
- `Reproduction/` has a report for every dataset-backed study (337); some `not reproducible`
  verdicts are genuine data gaps (e.g. `Cheng et al. (2026)` — raw fNIRS only, no behavioural data).
- `Datasets/` exists only for studies with public data (the rest are listed in `state.json`).
- **Datasets-hygiene exceptions** (deliberately not pruned): `Marten & Stadtler (2025 b)` and
  `Offer-Westort et al. (2024)` — their harmonizer reads inputs via a glob, so the source set
  cannot be established by name.
- Archived non-source data lives in `archive/_datasets_nonsource_<date>/` (+ the aux/supplement
  archives); restore a file before re-deriving a dataset.
- **Byte-identical duplicate papers stay in `Papers/`** (e.g. `Biddlestone et al. (2025 a)` is a
  byte-for-byte copy of `Biddlestone et al. (2025)`).  The duplicate is removed from the *analysis*
  by `Scripts/study_exclusions.py` (`NONINDEPENDENT_EXCLUSIONS`), not by deleting the file: the
  `Extraction/<Key>.json` and its coding still need a paper to point at, and the coding routes
  (`- SB`, `- MM`) cover every key including the excluded ones.  Deleting the PDF would break
  `mark_source --verify` for that key.

## Quick checks
```
python run_analysis.py        # core pipeline: meta -> sdt -> state (all products in Results/)
python run_analysis.py --all  # + harmonize/combine/reproduce + the analysis suite
python run_analysis.py --list # show the steps
python Scripts/name_audit.py  # naming audit
python Scripts/validate_polarity.py  # veracity-dependent dv_polarity invariant (exit 1 on fail)
python Scripts/validate_fully_within.py  # fully-within switch, both settings (exit 1 on fail)
python Scripts/B_validate.py --dir Extraction      # coding completeness (expect 531/531)
python Scripts/validate_extraction.py             # effect/timing/N gaps -> Results/extraction_validation.csv
python Scripts/validate_sanity.py                 # title/author + participant-N + garbled-text checks
python Scripts/audit_harmonizers.py               # harmonizer contract audit (expect 266/266 per clause)
python Scripts/audit_polarity.py                  # sidecar polarity audit (expect 0 mismatches)
python Scripts/harmonization_coverage.py          # harmonization coverage matrix (11 essential slots)
python Scripts/audit_assignments.py               # coder assignments parity (exit 1 on mismatch)
python Scripts/validate_repro_dossier.py          # per-study reproduction dossier completeness
python Scripts/parse_reproduction_reports.py      # 5 required report elements (expect 342/342)
python Scripts/convert_legacy_schema.py --check    # the scheme conversion is lossless
python Scripts/validate_scheme.py                  # scheme structure, roles, corpus conformance
```

Fully-within (single-arm, no-control) experiments are **excluded from every pooled
product by default** and included only on opt-in (`INCLUDE_FULLY_WITHIN=1` /
`--include-fully-within`); the switch moves the DiD (pre→post) pool only. Enforced by
`Scripts/validate_fully_within.py`.
