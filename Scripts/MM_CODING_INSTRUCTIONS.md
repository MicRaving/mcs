# MM coding + PDF-marking instructions (schema v3.1)

You are coding **one** study independently into
`Codings/<Study Key> - MM.json` (a **JSON array**, one object per experiment) and
writing the decisions to `Codings/<Study Key> - MM.md`.

Every dependent variable (DV) **and every non-empty record field** must carry the
exact place in the paper that proves it (`source_span`, and a record-level
`source_spans` map). This is checked mechanically — see **Validate** below.

Read `Scripts/coding_web/schema.json` for the authoritative field list
(`common_fields`, `dv_fields`, per-intervention `modules`, `vocabularies`).

## Independence — read the paper, not another coder

Sources you may read: `Text/<Key>.txt`, `Papers/<Key>.pdf`,
`Scripts/coding_web/schema.json`, and the shared code (`contract.py`,
`stat_core.py`, `B_validate.py`, `mark_source.py`). If `Datasets/<Key>/` holds a
raw deposit you may read the **raw** files only to confirm a reported effect, but
never a derived product.

**Do not read** `Extraction/<Key>.json`, `Extraction/<Key>.md`, any
`Codings/<Key> - *.json`, `Datasets/<Key>/<Key> - *.csv`,
`Datasets/<Key>/repro_facts.json`, or `Reproduction/<Key>.md`. These contain the
canonical or another coder's answers and would destroy the independent comparison.
Do **not** grep, glob or summarise across `Extraction/` or `Codings/` either — a
"conventions" search prints other coders' values and counts. Read only the schema
and the shared code for conventions. If you read or print any of them by accident,
say so in the `.md` and in your reply, naming what was exposed.

## One record per experiment

`Study_ID` = first-author surname + year + `_ExpN`, ASCII only, e.g.
`Austin2025_Exp1`, `Jolley2014_Exp9`. For a two-study paper use `_Exp1`, `_Exp2`.

Required record fields: `Study_ID`, `Year`, `Authors`, `Title`,
`Study_Description`, `Intervention_Type`, `DV_Metrics`.
Fill the other schema fields you can source (`Publication`, `DOI`, `Setting`,
`N_Recruited`, `N_Analyzed`, `N_Reported_Per_Condition`, `N_Inference_Method`,
`Age_*`, `Gender_*`, `Control_Group_Type`, `Intervention_*`, design fields, …).
Leave a field empty when the paper does not report it — never invent a value.

Set the common field `Fulltext_Exclusion_Criterion`: leave it **empty** when the
study meets all inclusion criteria; otherwise list the unmet one(s) — **C1** no
misinformation intervention, **C2** no control/comparison group, **C3** not
English, **C4** not a primary experimental study. Use **only** those four codes
(or an empty list). If the study meets C1–C4 but reports no eligible
intervention-vs-control contrast (e.g. every arm is treated), leave the field
empty and explain the design fact in `Rationale_Notes`; never write a label such
as "insufficient reporting" there. A failed criterion never excuses a thin
coding: still code every experiment and every DV normally.

## Effect routes (a DV row must carry at least one, or a reason)

| route | fields |
|---|---|
| descriptives | `n1i, m1i, sd1i, n2i, m2i, sd2i` |
| t | `t_val` + (`n1i,n2i` **or** `n_total`) |
| 1-df F | `F_val` + `df_num` + `df_den` + (`n1i,n2i` **or** `n_total`) |
| r | `r_val` + `n_total` |
| reported effect | `yi` + (`vi` **or** `sei`) |
| standardised d/g | `d_val`/`g_val` + (`n1i,n2i` **or** `n_total`) |
| regression coefficient | `beta_val` + (`beta_se`/`sei`/`vi` **or** `n_total`) |
| odds ratio | `odds_ratio` + `or_ci_low` + `or_ci_high` |

A balanced two-arm design may give only `n_total` (`n1 = n2 = n_total / 2`).
Otherwise supply a non-empty `missing_metrics_reason`.

## Contrast rules

- `eligible_primary = 1` **only** for the primary intervention vs a
  **no-misinformation-intervention** control contrast.
- Preference order for the effect: (a) per-condition `n/M/SD`; (b) the reported
  **pairwise-vs-control** statistic (`t`, 1-df `F`, `r`, reported `d`/`g`);
  (c) **never** a multi-df omnibus `F`/`chi2`/`eta2` (`df_num > 1`) or a
  `contrast_type = 11` term as the `eligible_primary == 1` effect. If only an
  omnibus is reported, search the same paragraph and the referenced table for
  pairwise comparisons or per-condition means/SDs; if none exists, set a
  non-empty `missing_metrics_reason` naming the paragraph/table searched.
- `contrast_type`: 1 treatment vs no treatment, 2 treatment vs active/placebo,
  3 dose-response, 4 within-person, 5 factorial main effect, 6 simple effect,
  7 other, 8 misinformation vs no misinformation, 9 informative message vs
  control, 10 factorial interaction, 11 omnibus model term (never eligible),
  12 moderator main effect, 13 non-intervention factor.
- Code **every reported outcome** of the primary comparison, not only the
  headline. Non-eligible rows (interactions, descriptives, active-comparator
  contrasts) are still coded with `eligible_primary = 0`.
- One row per distinct contrast × outcome × timepoint. For a multi-arm
  intervention, code one eligible primary contrast per arm against the shared
  no-intervention control; code arm-vs-arm contrasts as `eligible_primary = 0`.
- Both-arms-taught designs have no eligible primary; code the reported
  within-person pre→post change as `contrast_type = 4` with `eligible_primary = 1`
  only when the paper reports that change.

## DV coding rules

- `dv_domain`: `technique_recognition`, `credibility`, `sharing_intention`,
  `search_behavior`, `attitude_policy`, `trust`, `media_literacy_attitudes`,
  `critical_thinking`, `other`.
- `score_types`: `separate_true`, `separate_false`, `discernment_difference`,
  `composite`, `signal_detection_d_prime`, `signal_detection_c`, …
- `dv_polarity`: **1 = higher is better**, 0 = higher is worse. More
  discernment, more credibility for **true** news, **less** sharing of **false**
  news, more analytic processing are better. Derive it per DV.
- Timepoints: put the measurement in `timepoint_1` (`post`, `pre`,
  `followup_…`) and a comparison timepoint in `timepoint_2` for a change. Never
  pool a follow-up as an immediate posttest; never treat a pretest row as an
  outcome.
- Prefer reported `d`/`g`/`yi`/`vi`; otherwise means+SDs+n; otherwise `t`/`F`
  (only `df_num == 1`), `r`+n, odds ratio.

## Marking every DV (required)

For each DV, mark the exact sentence/table row/cell that reports the effect or
its descriptives:

```
python Scripts/mark_source.py "<Study Key>" "<verbatim quote>"
```

It prints `{"page":…, "rects":[[x0,y0,x1,y1],…], "text":"…", "confidence":…}`.
Then set on that DV:

- `source_span` = the printed object (add it verbatim);
- `source_quote` = `source_span.text`;
- `source_location` = `PDF p. <page>`.

Rules:

- The quote must be copied verbatim from the paper / `Text/<Key>.txt`, not
  paraphrased.
- If the quote is not found, shorten it to a contiguous phrase and retry; if it
  crosses a page break or sits in an image-only table, mark the table caption or
  the sentence introducing the table.
- If a DV is genuinely unmarkable (no extractable text), say why in
  `missing_metrics_reason` and `Rationale_Notes` and still leave the best span
  you can (caption/heading).
- Check the page/rects are sensible before moving on.

## Marking every record-level variable (required)

Every non-empty field on the experiment record needs a location: sample size,
age, gender, setting, control group, intervention type/duration, design, and so
on. Store them in a `source_spans` object on the record:

```json
"source_spans": {
  "N_Analyzed":         {"page": 2, "rects": [[...]], "text": "..."},
  "Age_Group":          {"page": 2, "rects": [[...]], "text": "..."},
  "Gender_Reported_As": {"page": 2, "rects": [[...]], "text": "..."},
  "Control_Group_Type": {"page": 3, "rects": [[...]], "text": "..."},
  "Intervention_Type":  {"page": 3, "rects": [[...]], "text": "..."}
}
```

Skip only these bookkeeping/nested keys: `Study_ID`, `Parent_Study_ID`,
`Rationale_Notes`, `Fulltext_Exclusion_Criterion`, `DV_Metrics`, `Interventions`,
`Covariates`, `source_spans`, `Intervention_ID`, `Intervention_Type_Detail`.
**Every other field that has a value** (number, string, list, boolean) must have
a span.

- Metadata (`Title`, `Authors`, `Year`, `Publication`, `DOI`) → page 1.
- A boolean/derived coding (`WEIRD_*`, `International_Sample`,
  `consent_reported`, `data_available`, …) → the sentence stating the
  underlying fact.
- A list-valued field → a passage that supports the listed values.
- A table-based numeric field (`N_Analyzed`, `Age_Mean`) → the table cell/row.
- An absence coding (e.g. `preregistration_status = 1` = none) → the nearest
  relevant statement, and say in `Rationale_Notes` that the paper contains no
  explicit statement.

## Estimation uncertainty

Prefer an **exact** derivation. If a value must be estimated (SD imputed, an F
with `df_num > 1`, a p-value-only result), state the assumption and its size in
`missing_metrics_reason`/`conversion_method`, and only enter the effect when the
leeway is small (**≤ 10%**). Otherwise leave the effect empty with the reason.

## Validate (both must report zero problems)

```
python Scripts/B_validate.py "Codings/<Key> - MM.json"
python Scripts/mark_source.py --verify "Codings/<Key> - MM.json" --fields
```

`--verify --fields` fails when a DV has a `source_quote` but no valid
`source_span`, when the span text does not contain the quote, or when any
non-empty record field lacks a `source_spans` entry. Iterate until both are
clean.

## Scope and cleanup

Write **only**:

- `Codings/<Key> - MM.json`
- `Codings/<Key> - MM.md`
- scratch under `temp/mm/<Key>/` — delete **only** that one folder when you finish.
  Never delete `temp/mm` itself, `temp/mm_route/` (the dispatcher's queue state),
  or any other session's scratch folder.

Do not modify `Extraction/`, `Papers/`, `Text/`, `Datasets/`, `Results/`, any
other `Codings/` file, or git. Keep the `.md` short: design, eligibility, DV
table, marking decisions, estimation caveats.

## Reply

Report: study key, experiment count, DV count, DVs with a marked span, record
fields marked / total, any DV without an effect (and why), any estimation
assumptions, and the two output paths.
