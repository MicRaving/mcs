# Screening prompt

## Job

You are screening a study for inclusion in a systematic review. Read the abstract (or the paper, at the full-text stage) and decide every criterion below.

## Output contract

Return **one JSON object** and nothing else - no prose, no markdown fence:

```json
{
  "<criterion key>": { "applies": "Yes | No | Unsure",
                     "certainty": "High | Medium | Low",
                     "rationale": "one sentence, citing the abstract" }
}
```

- one entry per criterion, for every criterion listed below;
- `applies` is the answer to the criterion as it is worded;
- `Unsure` is a legitimate answer - say so when the abstract does not settle it, and say why in the rationale;
- the rationale must point at the abstract, not at the paper in general.

## Rules

- judge only what the text you are given says; never assume a study design that is not stated;
- never guess a verdict to be helpful - an undecided abstract is screened again by a human;
- keep the rationale to the sentence that decides it.

## The criteria

Every **gating** criterion must pass. A study that fails a gating criterion is excluded, but it is still coded in full: record the failing criterion and keep the data you read.

### Abstract screening

- **C1** — Misinformation intervention
  Does the study feature an intervention aimed at reducing susceptibility to misinformation (inoculation/prebunking, debunking, media literacy, source-credibility or lateral-reading training, accuracy nudges, social norms, platform exposure reduction)?
  Options: Yes, No, Unsure.
  Pass values: Yes.
  Exclusion reason: No misinformation intervention (C1)
- **C2** — Control group
  Does the study include a control, placebo, or comparison group that does NOT receive the misinformation intervention?
  Options: Yes, No, Unsure.
  Pass values: Yes.
  Exclusion reason: No control/comparison group (C2)
- **C3** — Language (English)
  Is the paper in English?
  Options: Yes, No, Unsure.
  Pass values: Yes.
  Exclusion reason: Not English (C3)
- **C4** — Experimental (primary quantitative study)
  Is this a primary empirical study with a quantitative experimental or quasi-experimental design?
  Options: Yes, No, Unsure.
  Pass values: Yes.
  Exclusion reason: Not a primary experimental study (C4)

### Full-text exclusion

- **C1** — No misinformation intervention (C1) (not gating)
  No misinformation intervention (C1)
  Options: C1, C2, C3, C4, irretrievable, no_contrast, duplicate, observational, domain_mismatch, other.
  Exclusion reason: No misinformation intervention (C1)
- **C2** — No control/comparison group (C2) (not gating)
  No control/comparison group (C2)
  Options: C1, C2, C3, C4, irretrievable, no_contrast, duplicate, observational, domain_mismatch, other.
  Exclusion reason: No control/comparison group (C2)
- **C3** — Not English (C3) (not gating)
  Not English (C3)
  Options: C1, C2, C3, C4, irretrievable, no_contrast, duplicate, observational, domain_mismatch, other.
  Exclusion reason: Not English (C3)
- **C4** — Not a primary experimental study (C4) (not gating)
  Not a primary experimental study (C4)
  Options: C1, C2, C3, C4, irretrievable, no_contrast, duplicate, observational, domain_mismatch, other.
  Exclusion reason: Not a primary experimental study (C4)
- **irretrievable** — Fulltext irretrievable (not gating)
  Fulltext irretrievable
  Options: C1, C2, C3, C4, irretrievable, no_contrast, duplicate, observational, domain_mismatch, other.
  Exclusion reason: Fulltext irretrievable
- **no_contrast** — No eligible contrast reported (not gating)
  No eligible contrast reported
  Options: C1, C2, C3, C4, irretrievable, no_contrast, duplicate, observational, domain_mismatch, other.
  Exclusion reason: No eligible contrast reported
- **duplicate** — Duplicate/non-independent report of an included study (not gating)
  Duplicate/non-independent report of an included study
  Options: C1, C2, C3, C4, irretrievable, no_contrast, duplicate, observational, domain_mismatch, other.
  Exclusion reason: Duplicate/non-independent report of an included study
- **observational** — No experimental intervention (observational) (not gating)
  No experimental intervention (observational)
  Options: C1, C2, C3, C4, irretrievable, no_contrast, duplicate, observational, domain_mismatch, other.
  Exclusion reason: No experimental intervention (observational)
- **domain_mismatch** — Outcome/intervention outside misinformation susceptibility (not gating)
  Outcome/intervention outside misinformation susceptibility
  Options: C1, C2, C3, C4, irretrievable, no_contrast, duplicate, observational, domain_mismatch, other.
  Exclusion reason: Outcome/intervention outside misinformation susceptibility
- **other** — Other (specify) (not gating)
  Other (specify)
  Options: C1, C2, C3, C4, irretrievable, no_contrast, duplicate, observational, domain_mismatch, other.
  Exclusion reason: Other (specify)

## Deciding

Every **gating** criterion must pass for the study to be included. When one fails, it is the exclusion reason - so answer every criterion, and do not stop at the first failure.
