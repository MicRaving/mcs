# Reproducing a study

Reproduction is the check that catches numbers that were read wrong. It takes the
study's harmonized dataset, re-runs the paper's own analysis on it, and compares
the result with what the paper reports.

Open the **Reproduction** tab.

## Why it exists

A coded effect size is a *transcription*. The paper printed `F(1, 210) = 4.8`;
somebody typed `4.8` and `210`. If the transcription is wrong — a swapped group,
a misread table, a `t` treated as `F` — every pooled number downstream is wrong
too, and nothing in the meta-analysis can tell you.

Re-running the analysis from the raw data cannot hide that kind of error: either
the number comes back or it does not.

## What a reproduction does

For one study:

1. Read the harmonized dataset.
2. Recompute the effect the paper reports — usually a group difference in a DV,
   or a regression coefficient.
3. Compare it with the value in the coding.
4. Write the verdict.

The report is `Reproduction/<Key>.md`, and it carries five things a reader needs:
the **exclusions** applied, the **discrepancies** (with both numbers), the
**analysis** actually run, the **result**, and the **verdict** — *reproduced*,
*reproduced with a discrepancy*, *not reproducible*, or a partial verdict when
only some outcomes could be checked.

A study can legitimately be **not reproducible**: raw fNIRS traces, a proprietary
dataset, or a paper that reports only a figure with no numbers. That is recorded,
not hidden.

## The tab

* **KPIs** — how many studies reproduce, how many differ, how many cannot be
  checked.
* **The list** — every study with its verdict, filterable by verdict.
* **The detail** — the harmonization mapping (which dataset column became which
  coding variable), the exclusions applied, and the verdict with its numbers.

The run bar has three buttons:

| Button | What it does |
|---|---|
| **Generate scripts** | writes a thin per-study reproduction wrapper; a bespoke script is never overwritten |
| **Run all harmonizers** | runs `Datasets/<Key>/harmonizer.py` for every study and rebuilds `harmonization_mapping.xlsx` |
| **Run all reproducers** | runs every per-study reproductions script and rebuilds `reproduction_summary.xlsx` |

The **Only** box restricts a run to study keys containing a substring — useful
while working on one study. Do a run without a filter afterwards, or the summary
workbook stays restricted to the studies you filtered.

## When the numbers disagree

Work through it in this order:

1. **The exclusions.** Re-run without the author's attention check. If the gap
   closes, the coding and the data used different participants.
2. **The condition variable.** Check `condition` against `condition_raw`: a
   merged arm, a re-coded level, a control that is not the one you think.
3. **The DV.** A sum score versus a mean, a reversed item, a standardised
   outcome, a log transformation.
4. **The analysis.** The paper may report a covariate-adjusted estimate while the
   reproduction is unadjusted, or a mixed model where the reproduction is a
   simple difference.

A discrepancy is a finding. Write it down with both numbers; do not make the
numbers agree by adjusting the transcription until they do.

## What reproduction is not

It is not a re-analysis with your own model. If your model differs from the
paper's, you are no longer checking the paper — you are running a new study. Keep
the two apart: the reproduction re-runs *their* analysis; the analysis pools
*your* effect sizes.
