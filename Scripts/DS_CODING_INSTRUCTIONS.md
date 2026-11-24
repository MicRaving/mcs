# DS coding + reproduction instructions (schema v3.1)

You are the **DS** coder. You code **one** paper, end to end, into

| artefact | path |
|---|---|
| coding | `Codings/<Study Key> - DS.json` (JSON array, one object per experiment) |
| coding notes | `Codings/<Study Key> - DS.md` |
| paper | `Papers/<Study Key>.pdf` |
| text | `Text/<Study Key>.txt` |
| supplements | `Supplements/<Study Key> Supplement N.<ext>` |
| dataset + code | `Datasets/<Study Key>/` (raw originals, `SOURCE.md`) |
| reproduction report | `Reproduction/<Study Key>.md` |
| harmonizer + products | `Datasets/<Study Key>/harmonizer.py` → `<Key> - harmonized.csv` etc. |

Read `temp/ds_route/SCHEMA_REFERENCE.md` for the field list and every vocabulary.
`Scripts/coding_web/schema.json` is authoritative when they disagree.

## 0. Independence

Your only sources are the paper (`Papers/<Key>.pdf`, `Text/<Key>.txt`), its
supplements, its raw deposit (`Datasets/<Key>/`), `temp/ds_route/SCHEMA_REFERENCE.md`
and the shared code (`Scripts/contract.py`, `Scripts/B_validate.py`,
`Scripts/mark_source.py`, `Scripts/harmonizer_template.py`, `Scripts/repro_lib.py`).

**Never read** `Extraction/<Key>.json`, `Extraction/<Key>.md`, any other
`Codings/<Key> - *.json`, `Reproduction/<Key>.md` (if it already exists),
`Datasets/<Key>/<Key> - harmonized*.csv`, `- prepared*.csv`, `- repro_facts.json`.
Do not grep or glob across `Extraction/` or `Codings/`. If you read one by
accident, say so in the `.md` and in your reply.

## 1. Retrieve the paper

```
python Scripts/retrieve_paper.py --key "<Key>" --doi "<DOI>" --title "<Title>" --year <Year>
```

It resolves OpenAlex / Unpaywall / Semantic Scholar / PubMed Central / OSF and
writes `Papers/<Key>.pdf` + `Text/<Key>.txt`. If it fails:

1. search the web for the exact title plus `pdf`, `preprint`, `PsyArXiv`, `SSRN`,
   `OSF`, `ResearchGate`, or the first author's homepage / university repository;
2. download what you find with
   `python Scripts/retrieve_paper.py --key "<Key>" --url "<pdf-url>"` (repeatable);
3. publisher PDFs on `tandfonline.com`, `onlinelibrary.wiley.com`,
   `journals.sagepub.com` and `sciencedirect.com` are usually Cloudflare-blocked —
   prefer a repository or preprint copy.

If no copy exists anywhere, **say so and stop** — do not code a paper you have
not read. A clean "not retrievable" is a valid result.

Then look for the **supplements, dataset and code**: the Data Availability /
Open Science statement, footnotes, the reference list, OSF / Dataverse / Zenodo /
Figshare / Dryad / Mendeley Data, the journal's "Supporting Information".
Download with `python Scripts/acquire_datasets.py "<Key>" "<url>"` (raw files keep
their names) or plain `curl`/`Invoke-WebRequest`. Supplements go to
`Supplements/<Key> Supplement N.<ext>`; data and code to `Datasets/<Key>/`.
Write `Datasets/<Key>/SOURCE.md`: URL/DOI, licence, access date, and what each
file contains.

## 2. Code the paper

One record per **experiment**. `Study_ID` = first-author surname + year + `_ExpN`,
ASCII only (`Austin2025_Exp1`). Required fields: `Study_ID`, `Year`, `Authors`,
`Title`, `Study_Description`, `Intervention_Type`, `DV_Metrics`. Fill every other
schema field you can source; leave a field **empty** when the paper does not
report it — never invent a value and never write 0 for "not stated".

`Fulltext_Exclusion_Criterion`: empty when the study meets C1–C4; otherwise the
unmet code(s) from `C1`/`C2`/`C3`/`C4` only. Still code everything normally.

### Effect routes — a DV row needs at least one, or a reason

| route | fields |
|---|---|
| descriptives | `n1i, m1i, sd1i, n2i, m2i, sd2i` |
| t | `t_val` + (`n1i,n2i` or `n_total`) |
| 1-df F | `F_val` + `df_num` + `df_den` + (`n1i,n2i` or `n_total`) |
| r | `r_val` + `n_total` |
| reported effect | `yi` + (`vi` or `sei`) |
| standardised d/g | `d_val`/`g_val` + (`n1i,n2i` or `n_total`) |
| regression coefficient | `beta_val` + (`beta_se`/`sei`/`vi` or `n_total`) |
| odds ratio | `odds_ratio` + `or_ci_low` + `or_ci_high` |

Otherwise a non-empty `missing_metrics_reason` naming the paragraph/table searched.

### Contrasts

* `eligible_primary = 1` **only** for the primary intervention vs a
  **no-misinformation-intervention** control.
* Preference order: (a) per-condition `n/M/SD`; (b) the reported pairwise-vs-control
  statistic (`t`, 1-df `F`, `r`, reported `d`/`g`); (c) **never** a multi-df omnibus
  `F`/`chi2`/`eta2` (`df_num > 1`) or `contrast_type = 11` as the eligible effect.
* `contrast_type`: 1 treatment vs no treatment, 2 treatment vs active/placebo,
  3 dose-response, 4 within-person, 5 factorial main effect, 6 simple effect,
  7 other, 8 misinformation vs no misinformation, 9 informative message vs control,
  10 factorial interaction, 11 omnibus model term (never eligible),
  12 moderator main effect, 13 non-intervention factor.
* Code **every reported outcome** of the primary comparison; non-eligible rows
  (interactions, active comparators, arm-vs-arm) still get `eligible_primary = 0`.

### DVs

* `dv_domain`: `technique_recognition`, `credibility`, `sharing_intention`,
  `search_behavior`, `attitude_policy`, `trust`, `media_literacy_attitudes`,
  `critical_thinking`, `other`.
* `score_types`: `separate_true`, `separate_false`, `discernment_difference`,
  `composite`, `signal_detection_d_prime`, `signal_detection_c`, … (see the schema).
* `dv_polarity`: 1 = higher is better. More discernment, more credibility for
  **true** news, **less** sharing of **false** news, more analytic processing are
  better. Derive it per DV.
* Timepoints: `timepoint_1` = `post` / `pre` / `followup_<n>`; `timepoint_2` only
  for a change. Never pool a follow-up as an immediate posttest.

## 3. Source location — required for every coding

Every DV row carries `source_location`, `source_quote` and `source_span`; every
non-empty record field carries an entry in the record's `source_spans` map
(skip only `Study_ID`, `Parent_Study_ID`, `Rationale_Notes`,
`Fulltext_Exclusion_Criterion`, `DV_Metrics`, `Interventions`, `Covariates`,
`source_spans`, `Intervention_ID`, `Intervention_Type_Detail`).

Get the span from the PDF:

```
python Scripts/mark_source.py "<Key>" "<verbatim quote>"
```

It prints `{"page": N, "rects": [[x0,y0,x1,y1], ...], "text": "..."}`.
Set `source_span` to that object, `source_quote` to its `text`, and
`source_location` to `PDF p. <page>`.

* The quote must be verbatim from the paper, not paraphrased.
* If the quote is not found, shorten it to a contiguous phrase and retry; if it
  crosses a page break or sits in an image-only table, mark the caption or the
  sentence introducing the table.
* **Text-only paper** (no PDF, `Text/<Key>.txt` came from Europe PMC): there are
  no rects. Then set `source_location` to a text reference
  (`Text p. N, Results, para 2`) and `source_quote` to the verbatim sentence, and
  leave `source_span` absent. Say so in the `.md`.

## 4. Validate — both must be clean

```
python Scripts/B_validate.py "Codings/<Key> - DS.json"
python Scripts/mark_source.py --verify "Codings/<Key> - DS.json" --fields
```

Iterate until both report zero problems. `--fields` fails when a DV has a
`source_quote` but no valid `source_span`, when the span text does not contain the
quote, or when a non-empty record field has no `source_spans` entry.

## 5. Reproduction report

Write `Reproduction/<Key>.md` for **every** paper, whether or not data exists.
Start with:

```
# Reproduction report - <Key>
<!-- REPRO_DONE -->
**reproduced:** yes|no|partial
```

Then: `**reason:**`, `**Paper:**`, `## Data used`, `## Harmonization`,
`## Exclusions`, `## DVs and polarity`, `## Reported effects that cannot be
recomputed`, `## (b) Reproduced results`. Follow the shape of an existing report
in `Reproduction/` (do not read the one for *your* key if it exists).

If there is no public participant-level data, say so plainly and name what would
make it reproducible. Never invent a number.

## 6. Harmonize — only when a primary participant-level dataset exists

Copy `Scripts/harmonizer_template.py` to `Datasets/<Key>/harmonizer.py`, adapt
the per-study hooks (`EXTRA_COLS`, `STUDY_LABELS`, `EXCLUSIONS`, `POLARITY`), and
run it. It must write

* `<Key> - harmonized.csv` — WIDE, one row per participant/session;
* `<Key> - harmonized_variables.csv` — the sidecar (uniform name, `dv_domain`,
  `veracity`, `timepoint`, `dv_polarity`);
* `<Key> - exclusions.csv` — `flag, keep, applies_to, reason` (header-only when
  there are no author exclusions).

Rules: uniform DV names `dv_<domain>[_<veracity>][_<time>]`; covariates `cov_*`;
every numeric variable also gets a `<name>_01` column rescaled to 0–1 within its
own observed range; author attention checks become `flag_*` (verbatim, no `_01`);
every DV oriented so higher = more desirable, with `dv_polarity` recorded.

Then run the reproduction engine and fold its numbers into the report:

```
python "Datasets/<Key>/<Key> - reproduction.py"
```

(generate the wrapper with `python Scripts/generate_repro_wrappers.py --key "<Key>"`
if it is missing — always pass `--key` so the run touches only your study). It
prints the four-section report; copy the recalculated values into
`Reproduction/<Key>.md`.

If there is **no** participant-level data: write no harmonizer, no products, no
empty `Datasets/` folder — document the planned schema in the coding `.md` and
say "not reproducible (no public data)" in the report.

## 7. Scope and cleanup

Write only the artefacts listed at the top plus scratch in `temp/ds_route/<Key>/`.
Do not touch `Extraction/`, `Results/`, `master_dataset.xlsx`, other studies'
folders, or git. Never overwrite a non-empty file that another route owns.

## 8. Reply

Report: study key; whether the paper was retrieved (and from where); supplement /
dataset / code found (URLs); experiment count; DV count; DVs with a marked span;
record fields marked / total; validation output for both commands; whether a
harmonizer was written and what it produced; the reproduction verdict; and
anything you could not do and why.
