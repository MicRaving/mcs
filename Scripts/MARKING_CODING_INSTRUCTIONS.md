# Coding + PDF-marking instructions (marking wave)

You are coding ONE study independently into
`Codings/<Study Key> - <coder>.json` (a **JSON array**, one object per
experiment) and, for every dependent variable (DV), marking the exact place in
the paper that proves it (`source_span`).

Read `Scripts/coding_web/schema.json` for the authoritative field list:
`common_fields`, `dv_fields`, the per-intervention `modules`, and `vocabularies`.

## Independence — read the paper, not the other coder

Sources you may read: `Text/<Key>.txt`, `Papers/<Key>.pdf`,
`Scripts/coding_web/schema.json`, and the shared code (`contract.py`,
`stat_core.py`, `B_validate.py`, `mark_source.py`).

**Do not read** `Extraction/<Key>.json`, `Extraction/<Key>.md`, any
`Codings/<Key> - *.json`, `Datasets/<Key>/`, or `Reproduction/<Key>.md` — these
contain the canonical or another coder's answers and would destroy the
independent comparison.

## Granularity & eligibility (lessons from the pilot wave)

- Code **every reported outcome** of the primary comparison, not only the headline.
  A coding with 6 DVs where the paper reports 13 loses information the agreement
  and analysis need. Non-eligible rows (interactions, descriptives,
  active-comparator contrasts) are still coded with `eligible_primary = 0` — they
  document the design.
- One row per distinct contrast × outcome × timepoint.
- For a multi-arm intervention, code one eligible primary contrast per
  intervention arm against the shared no-intervention control; code the arm-vs-arm
  and decomposition contrasts as `eligible_primary = 0` (the cluster id groups
  them, so they cannot double-count).
- Both-arms-taught designs (active comparator) have **no** eligible primary. Code
  the within-person pre→post change as `contrast_type = 4` with
  `eligible_primary = 1` only when the paper reports that change, and record the
  fully-within design in `Rationale_Notes`; otherwise `eligible_primary = 0`.
- Never code an omnibus term or a decomposition more than once as eligible.

## Record shape

```json
[
  {
    "Study_ID": "<Key>_Exp1",
    "Year": 2020,
    "Authors": "Surname, A.; Surname, B.",
    "Title": "...",
    "Study_Description": "One-paragraph summary of the design.",
    "Publication": "paper_peer_reviewed",
    "Intervention_Type": ["inoculation"],
    "Setting": 5,
    "N_Recruited": 400,
    "N_Analyzed": 380,
    "DV_Metrics": [
      {
        "dv_name": "Accuracy (true minus false headlines)",
        "dv_domain": "credibility",
        "score_types": ["discernment_difference"],
        "dv_polarity": 1,
        "contrast_type": 2,
        "eligible_primary": 1,
        "timepoint_1": "post",
        "timepoint_2": null,
        "posttest_type": 1,
        "m1i_post": 0.72, "sd1i_post": 0.18, "n1i_post": 190,
        "m2i_post": 0.55, "sd2i_post": 0.20, "n2i_post": 190,
        "effect_size_type_reported": "means",
        "source_quote": "<the marked text>",
        "source_location": "PDF p. 4",
        "source_span": {"page": 4, "rects": [[0.12, 0.34, 0.88, 0.37]], "text": "<the marked text>"}
      }
    ]
  }
]
```

Required record fields: `Study_ID`, `Year`, `Authors`, `Title`,
`Study_Description`, `Intervention_Type`, `DV_Metrics`. Every DV needs
`dv_name`, `contrast_type`, `eligible_primary`, and either a usable effect
(`contract.dv_has_effect`) or a non-empty `missing_metrics_reason`.

`Study_ID` names the experiment (one record per trial/experiment).

## DV coding rules

- **`eligible_primary`** = 1 ONLY for the primary *intervention vs no-misinformation-intervention control* contrast. Active comparators, dose contrasts, within-person training gains, moderator terms, and omnibus terms are 0.
- **`contrast_type`**: 1 primary treatment vs no treatment, 2 treatment vs active/placebo, 3 dose-response, 4 within-person, 5 factorial main effect, 6 simple effect, 7 other, 8 misinformation vs no misinformation, 9 informative message vs control, 10 factorial interaction, 11 omnibus model term (never eligible), 12 moderator main effect, 13 non-intervention factor.
- **`dv_domain`**: `technique_recognition`, `credibility`, `sharing_intention`, `search_behavior`, `attitude_policy`, `trust`, `media_literacy_attitudes`, `critical_thinking`, `other`.
- **`score_types`**: `separate_true`, `separate_false`, `discernment_difference`, `composite`, `signal_detection_d_prime`, `signal_detection_c`, `dichotomous_accuracy`, `proportion`, `proportion_difference`, `behavioral_intention`, `single_item`, `change_score`, …
- **`dv_polarity`**: 1 = higher is better, 0 = higher is worse. Derive it per DV (a "higher = worse" sharing/belief scale is 0).
- **Timepoints**: put the measurement in `timepoint_1` (`post`, `pre`, `followup_…`) and a comparison timepoint in `timepoint_2` when the contrast is a change. Never pool a follow-up as an immediate posttest.
- **Effects**: prefer reported `d`/`g`/`yi`/`vi`; otherwise means+SDs+n; otherwise `t`/`F` (only with `df_num == 1`), `r`+n, odds ratio. Do **not** convert a multi-df omnibus `F`/chi2 into a single effect.
- **No invented numbers.** If the effect is not derivable, write a concrete `missing_metrics_reason` (name the paragraph/table you searched).

## Marking every DV (required)

For each DV, mark the exact sentence/table row/cell that reports the effect or its
descriptives. Use the helper:

```
python Scripts/mark_source.py "<Study Key>" "<quote>"
```

It prints `{"page":…, "rects":[[x0,y0,x1,y1],…], "text":"…", "confidence":…}`.
Then set on that DV:

- `source_span` = the JSON object printed by the helper (add it verbatim);
- `source_quote` = `source_span.text`;
- `source_location` = `PDF p. <page>`.

Rules:
- The quote must be copied from the paper/`Text/<Key>.txt`, not paraphrased.
- If the quote is not found, shorten it to a contiguous phrase and retry; if it spans a page break or an image-only table, mark the table caption or the sentence that introduces the table.
- **Every DV must have a `source_span`.** If a DV is genuinely unmarkable (e.g. no extractable text), say why in `missing_metrics_reason` and `Rationale_Notes`.
- Check the mark's page/rects are sensible before moving on.

## Estimation uncertainty

- Prefer an **exact** derivation from reported statistics.
- If a value must be estimated (e.g. an SD imputed, an F with `df_num > 1`, a p-value-only result), state the assumption and its size in `missing_metrics_reason` or `conversion_method`, and only enter the effect when the leeway is small (**≤ 10%**). Otherwise leave the effect empty with the reason.

## Validate before you finish

```
python Scripts/B_validate.py "Codings/<Key> - <coder>.json"
python Scripts/mark_source.py --verify "Codings/<Key> - <coder>.json"
```

Both must report no problems. `--verify` fails when a DV has a `source_quote`
but no `source_span`, or when the span text does not contain the quote.

## Reply

Report, per study: the experiment count, DV count, DVs with a marked span, any
DV left without an effect (and why), any estimation assumptions, and the output
path.
