# SB coding instructions — schema v3.0

Code every experiment of a paper into `Codings/<Study Key> - SB.json` (a **JSON array**, one
object per experiment) and write the narrative decisions to `Codings/<Study Key> - SB.md`.
The authoritative field list is `Scripts/coding_web/schema.json` (`common_fields`, `dv_fields`,
per-intervention `modules`).  `Scripts/B_validate.py` checks completeness.

**Shared traps the pool has settled — read `Scripts/SB_SHARED_CODE_TRAPS.md` before coding.**  It
records the recurring, non-obvious judgements (sibling specs/arms are `eligible_primary = 0`;
nulls are findings and are pooled; the sign convention must be *derived*, not assumed; **no
deposit ⇒ write no harmonizer, no ` - SB` products, no empty `Datasets/` folder and no reproducer —
document the planned schema in the coding `.md` instead**; one experiment record per trial).

## Independence — read the paper, not the other coder's coding

The SB route exists to be an **independent second coding**, so its only sources are
`Papers/<Key>.pdf`, `Text/<Key>.txt` and the raw deposit in `Datasets/<Key>/`.

**Do not read any canonical-route artefact for this study:**

| path | why |
|---|---|
| `Extraction/<Key>.json`, `Extraction/<Key>.md` | the canonical coding itself |
| `Datasets/<Key>/harmonizer.py` | the canonical harmonizer (read the raw deposit instead) |
| `Datasets/<Key>/<Key> - prepared*.csv`, `- harmonized*.csv`, `- variables.csv`, `- dv_direction.csv`, `- conditions.csv`, `- exclusions.csv` | canonical derived data / sidecars |
| `Datasets/<Key>/<Key> - repro_results.csv`, **`- repro_facts.json`** | `repro_facts.json` carries the canonical coder's effect **sizes and signs** |
| `Reproduction/<Key>.md` | the canonical reproduction verdict and its recomputed values |

`repro_facts.json` is the easy accident: hunting for a data URL lands you in the dataset
folder, the filename looks like harmless provenance, and it silently hands over the answer.
One SB study did exactly that. If you read any of the above **by accident**, say so in the
coding `.md` and in your reply and name what was exposed — a disclosed, bounded leak is
usable; a silent one quietly corrupts the whole exercise.

You may still *reuse* the campaign's shared code (`stat_core`, `contract`, `suite_common`,
`dv_polarity`, `B_validate.py`, `audit_harmonizers.py`); that is shared infrastructure, not
another coder's judgement.

`Scripts/sb_merge.py` deliberately reports `sb_valid` as a placeholder, because the real
validity check is that both routes were produced independently.

## Effect routes (a DV row must carry at least one)

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

A balanced two-arm design may give only `n_total` (`n1 = n2 = n_total / 2`).  Otherwise supply a
non-empty `missing_metrics_reason`.

## Intervention-vs-control contrasts — preference order

1. per-condition descriptives `n/M/SD` (preferred, always poolable);
2. the reported **pairwise-vs-control** statistic (`t`, 1-df `F`, `r`, or a reported `d`/`g`);
3. **never** a multi-df omnibus `F`/`chi2`/`eta2` (`df_num > 1`) or a `contrast_type = 11`
   term as the `eligible_primary == 1` effect.  A multi-df omnibus compares more than two cells,
   cannot be signed and must not be turned into a single two-arm `d`.  If only an omnibus is
   reported, search the same paragraph and the referenced table for pairwise statistics or
   per-condition means/SDs; if none exists, set a non-empty `missing_metrics_reason` naming the
   paragraph/table searched.

## Full-text inclusion criteria (C1-C4) — required on every coding

Most studies meet all inclusion criteria.  If this study does **not**, set the common field
**`Fulltext_Exclusion_Criterion`** to the unmet criterion/criteria and **still do everything else
normally** (code every experiment, harmonize, reproduce):

| code | criterion |
|---|---|
| **C1** | No misinformation intervention |
| **C2** | No control / comparison group |
| **C3** | Not English |
| **C4** | Not a primary experimental study |

Leave `Fulltext_Exclusion_Criterion` **empty** when all criteria are met.  Failing a criterion never
excuses a thin coding: the paper is still fully coded, harmonized and reproduced, and the exclusion
is recorded only so the screening layer and the PRISMA flow stay consistent.

## Other rules

* **No invented numbers.** Every effect comes from the paper or the deposit, else a reason.
* **Polarity.** `dv_polarity` so that **higher = more desirable**: more discernment, more
  credibility for **true** news, **less** sharing of **false** news, more analytic processing.
  A rating where higher means *more belief in / sharing of false information* is `higher_is_worse`.
* **Do not code confidence / certainty / metacognition** as a `dv_domain` (separate strand).
* **Pretest rows are not outcomes** — use them only as a pre→post change (`timepoint_1 = pre`,
  `timepoint_2 = post`).
* `eligible_primary = 1` only for an intervention vs a **no-misinformation-intervention** control.
* Keep `Interventions[]` explicit when a paper separates components (e.g. inoculation vs lateral
  reading vs reflection) and bind each DV to its arm with `Intervention_ID`.
