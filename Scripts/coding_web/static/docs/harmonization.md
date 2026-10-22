# Harmonizing a study's data

Harmonisation is the step that makes two studies comparable. A coded paper says
*"p = .03, F(1, 210) = 4.8"*; a harmonized dataset says *`dv_discernment` for
participant 17 = 0.62*. The analysis can only pool the second kind.

Open the **Harmonization** tab for the overview, and the study's folder in
`Datasets/<Key>/` for the detail.

## What a study needs before it can be harmonized

* a public dataset (or one obtained from the authors) in `Datasets/<Key>/`;
* a codebook — which column is which condition, which is the outcome;
* a `harmonizer.py` that reads the raw files and writes the harmonized file.

A study with no public data cannot be harmonized. It keeps its coded effect size
and is analysed from the coding alone — that is a legitimate state, not a
failure.

## The shape of a harmonized dataset

One row per participant (or per session), **wide**: one column per variable.

| Column | Meaning |
|---|---|
| `condition` | the arm, harmonized across studies (plus `condition_raw` as printed) |
| `dv_<domain>[_<veracity>][_<time>]` | an outcome measure |
| `cov_<name>` | a covariate — age, education, political leaning … |
| `flag_<name>` | the authors' own attention check or exclusion flag |

Every numeric variable also gets a `<name>_01` twin: the same values rescaled to
0–1 within their own observed range. That is what stops a 1–7 scale from
dominating a 0–1 scale in any model that mixes them.

The sidecar `<Key> - harmonized_variables.csv` records, for each column, whether
it is a DV or a covariate, its veracity (true or false stimuli), its timepoint
(pre, post, follow-up) and its **polarity**.

## Polarity: which direction is good

Every DV is oriented so that **higher is better** on that outcome — whatever
"better" means for the review. The sidecar records which of the two directions
the raw column had (`higher_is_better` / `higher_is_worse`), and the re-analysis
flips the second kind before pooling.

This is the single place where a sign error is easy to make and hard to see. If a
measure counts something undesirable, the raw numbers are *bad* when high:
polarity is `higher_is_worse`, and the analysis turns them into `1 − value`. Get
that wrong and the study pulls the pooled estimate the wrong way.

## Exclusions

Authors drop participants for failing attention checks; that is part of the
study's result, so it is part of its data. The rules are written in
`<Key> - exclusions.csv`:

| column | meaning |
|---|---|
| `flag` | the `flag_*` column the rule uses |
| `keep` | which value of that column means "keep" |
| `applies_to` | `*` (all DVs), a `dv_*_source_*` glob, or `domain:<dv_domain>` |
| `reason` | why, in one line |

The re-analysis applies those rules per DV before pooling, so a study whose
attention check only applies to the outcome measures does not lose participants
from its covariates.

## The coverage tab

The **Harmonization** tab ranks studies by how much of the 11 **essential slots**
is filled — condition, a discrimination measure, a sharing measure, sample size,
and so on. Each slot is filled, missing or ambiguous.

Click a study or a slot chip to open its assignment workspace: here a coder can
label a variable the machine could not identify (`n/a`, `confirmed`, or the
variable it really is). Those labels are saved in
`Datasets/<Key>/<Key> - variable_assignments.csv` and are what the coverage
matrix counts.

Missingness is not evenly bad: a study missing its condition column cannot be
pooled at all, while a study missing a covariate loses only moderator analyses.
The ranking is weighted that way.

## Habits

* **Look at the data first.** Open the raw file, find the condition variable and
  the outcome, and write down what the codebook says before writing code.
* **Never overwrite a raw file.** Harmonizers read the originals and write
  `<Key> - harmonized.csv`; the original stays as it was downloaded.
* **Check the N.** The harmonized N should be close to the coded N. A large gap
  means the merge dropped rows — a survey with two waves, a long-format file that
  needed reshaping, or a filter on an attention check.
* **One study, one harmonizer.** If a paper reports two experiments in one
  dataset, the `condition` column usually carries the experiment too, and the
  harmonizer keeps both.
