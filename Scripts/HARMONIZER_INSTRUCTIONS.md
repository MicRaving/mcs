# Harmonizer instructions — the adjusted contract (post-pilot, 2026-09-28)

You are harmonizing **one dataset** for the misinformation-intervention meta-analysis.
Read `AGENTS.md` §"Pipeline" step 3 + §"Datasets hygiene" first; this file records what the
10-dataset pilot taught us about *how* to do it reliably.

## 0. What the pilot found (why these rules exist)

| pilot lesson | what went wrong | the rule |
|---|---|---|
| **Silent template skips** | The shared `Scripts/harmonizer_template.py` globs `* - variables.csv` + `* - prepared*.csv` and **silently exits 0** when neither exists. Four studies whose deposit *had* arrived (Bauer & Wilson 2022, Bayrak 2025 b, DeVerna 2025, Lutzke 2019) therefore looked like "no data" for days. | **Never trust a template's "skipped" verdict.** Open the folder and list the files. If a deposit exists, write a study-specific harmonizer. |
| **Stale acquisition notes** | `SOURCE.md`/`Reproduction/*.md`/`- repro_facts.json` said "acquisition FAILED / no raw data" *while the data sat in the folder* (Gottschling, DeVerna, Lutzke, Cookson). | The **files on disk** are the source of truth, not the notes. Say so in your reply if the notes are stale; do not edit them. |
| **"Raw data" is not always participant data** | Filenames lied: headline CSVs (Horne) and financial/OWID CSVs (Nadib) are **stimulus lists / chart data**, not responses. | Verify **participant-level structure** (an id, an arm, an outcome) before deriving. If it is absent, emit the declared schema with **0 rows** and state why. |
| **Mapping must be proven** | Summaries are not enough: a wrong column→variable map still looks plausible. | Reproduce a **set of the paper's printed cells** from your wide table (means/SDs/n/F/d) and report the check. If you cannot, say exactly which cell fails. |
| **Polarity is evidence, not assumption** | Papers, the coding, and the reference rule can all disagree (Bauer & Wilson's mainland-attitude DV; Nadib's "higher is better" trust axis on an intentionally misleading chart). | Cite ≥2 independent sources per DV (paper wording, the coding's `dv_polarity`, `Scripts/dv_polarity.expected_polarity`) and **record any conflict** in the sidecar `notes` instead of silently choosing. |
| **The coding itself can be wrong** | Bayrak's DV labels were mislabelled and its arm `n1i/n2i` was wrong; Bauer & Wilson's coding contradicted its own `Rationale_Notes`. | Report discrepancies; never edit `Extraction/`. |
| **Not everything poolable is harmonizable to the coded experiment** | Cookson's deposit is the thesis's *cross-sectional* studies, with no arm column. | Harmonize what exists, set roles honestly, and say in your reply that the coded experiments are not covered. |

## 1. Deliverables (all inside `Datasets/<Key>/`)

1. `harmonizer.py` — deterministic, reads **only** files in its own folder, exits 0.
2. `<Key> - harmonized.csv` — WIDE, **one row per participant/session**.
3. `<Key> - harmonized_variables.csv` — one row per emitted column.
4. `<Key> - exclusions.csv` — `flag,keep,applies_to,reason`; header-only when there are none.
5. `<Key> - conditions.csv` — `arm,role,evidence` (`intervention` / `control` /
   `active_control` / `other`).
6. `<Key> - variables.csv` — the provenance mirror (coverage slot **S11**); include it, most
   harmonized studies have it, and `Scripts/dv_polarity.py` reads it back as the
   study-coded polarity source.

## 2. The contract that MUST hold

* **Uniform names** `dv_<domain>[_<veracity>][_<time>]`, `cov_*`, `flag_*`
  (`metacog_*` for the confidence strand). Flags **never** get an `_01` twin.
* **`_01` twins**: every numeric `dv_`/`cov_` column also gets `<name>_01`, linearly rescaled
  to 0–1 **within its own observed range**. Verify the twin spans 0–1 and that no
  `flag_*_01` exists.
* **Polarity**: every DV is oriented so **higher = more desirable**, recorded in the sidecar
  `dv_polarity` (`higher_is_better` / `higher_is_worse`; blank is legitimate when no
  desirable pole exists — e.g. a nominal category or a bare partisan attitude). Evidence goes
  in `notes`.
* **Veracity** only where the DV genuinely separates true from false stimuli; leave it blank
  otherwise. **Never tag a within-participant baseline or a "neutral" cell as veracity** —
  that fabricates a d′ (Gottschling showed a spurious d′ ≈ −2.1).
* **Roles** for every emitted column, from the campaign vocabulary: `dv` / `covariate` /
  `flag` / `metacog` / `pid` / `condition` / `factor` / `id` / `other`. Confidence and
  response-time columns are **never** `dv` (separate strand).
* **Exclusions**: the authors' checks become `flag_*` columns plus rows in
  `exclusions.csv` naming which DVs they gate (`*`, a `dv_*_source_*` glob, or
  `domain:<dv_domain>`). If the deposit is already post-exclusion, say so and emit
  header-only — do not invent a flag.
* **Conditions**: map every arm; an arm that itself delivers a misinformation intervention is
  `active_control`, not `control`.

## 3. Verify before you reply (all of it)

1. Run the harmonizer **twice** → all products byte-identical.
2. `python Scripts/audit_harmonizers.py` → **7/7 clauses** for your file.
3. `python Scripts/validate_polarity.py` → PASS; `python Scripts/audit_polarity.py` → 0
   mismatches for your study.
4. `python Scripts/harmonization_coverage.py` → note your slots (S6 blank is expected when
   there is no veracity split).
5. Mapping check: reproduce ≥ 5 printed cells from your wide table.
6. **Do NOT run `run_harmonizers.py --only "<Key>"`.** That flag rewrites the *shared*
   `Results/harmonization_mapping.xlsx` with just your one study, destroying every other
   study's mapping rows — several agents have already clobbered it, and it now holds a single
   study. To smoke-test your harmonizer, run **your `harmonizer.py` directly**
   (`python "Datasets/<Key>/harmonizer.py"`) and check its products; leave
   `run_harmonizers.py` to a single full pass afterwards. If you do clobber it by accident,
   restore your backup and say so in your reply.
7. `python Scripts/harmonization_coverage.py` → note your slots (S6 blank is expected when
   there is no veracity split).

## 4. Hard rules

* **Never overwrite** a non-empty file; back anything you replace up under
  `temp/harmonize/<Key>/`.
* Never touch `Extraction/`, `Papers/`, `Text/`, `Results/` or another study.
* Never prune the folder (hygiene gate: only after harmonized **and** reproduced).
* No absolute paths; scratch in `temp/harmonize/<Key>/`.
* If the folder has **no participant data**: still emit the four/five declared products with
  **0 rows**, a fully populated sidecar, and a short note — that closes the contract and
  documents the absence. Do not fabricate rows.

## 5. Reply format

`rows × cols` · `#dv/#cov/#flag` · how role and polarity were inferred (and any conflict) ·
which printed cells you reproduced · any variable you could not infer · the audit result ·
whether the acquisition/reproduction notes are stale.
