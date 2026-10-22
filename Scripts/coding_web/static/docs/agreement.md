# Agreement between coders

Two people coding the same paper will not code it identically. Agreement is how
you find out **where** they differ, and whether those differences are random
noise or a real disagreement about what the paper says.

## Why it matters

A meta-analysis is only as good as its coding. If two coders disagree on whether
a study had a control group, the effect size of that study is not a measurement —
it is one person's reading. Measuring agreement:

* finds the fields whose instructions are unclear (usually: the ones with low
  agreement, not the ones that feel hard);
* lets you report a number in the methods section, which reviewers expect;
* turns a silent inconsistency into a discussable case.

## Where to look

The **Agreement** tab. Two scopes — *Abstract* (screening verdicts) and
*Fulltext* (coding) — and two views.

### Reliability

The KPIs summarise the agreement per field. The table lists every field with
enough data to compute it.

The statistic used is **Gwet's AC1** (and AC2 for ordered fields). Both are
chance-corrected agreement coefficients, like Cohen's kappa, but they stay
stable when one level dominates — which happens constantly in coding, where most
papers are *not* pre-registered, *not* a field experiment, and so on. Kappa
collapses toward zero in exactly that situation, which is why it is not the
default here.

Rules of thumb (they are conventions, not laws):

| AC1 | Read it as |
|---|---|
| below 0.40 | the field is not being coded the same way — read the instruction |
| 0.40 – 0.60 | moderate; look at the conflicts to see what the split is about |
| 0.60 – 0.80 | substantial; normal for genuinely judgemental fields |
| above 0.80 | strong; typical for factual fields (year, country, N) |

Fields that are free text (titles, notes, quotes) are excluded: a text field
cannot be "in agreement" in a meaningful sense.

### Conflicts

Every field where two coders differ, side by side, with both values and both
rationales. This is the working list.

## Settling a conflict

1. Read the paper at the place the coders marked (*Mark source*). Do not argue
   from memory.
2. Decide which value the paper supports.
3. Write the decision in the rationale — one sentence saying what settled it.
4. Move on. A field you cannot settle from the paper is a **scheme** problem
   (the instruction is ambiguous) — fix the instruction, then re-code.

A settled conflict is not "the majority value". Two coders disagreeing does not
tell you who is right; the paper does.

## What low agreement usually means

| Symptom | Usual cause |
|---|---|
| One field is low, everything else is fine | the instruction does not define an edge case (e.g. "is a warning label an intervention?") |
| Many fields are low for one coder | that coder applied their own reading of the scheme — re-read the instructions together |
| A field is low only on a subgroup of papers | the field's condition (`show_if`) fires on papers where it does not apply |
| Agreement is perfect | suspicious: one coder may have seen the other's answers, or copied the reference coding |

## More than two raters

Every coding file in `Codings/` is a rater, so a study coded by three people (or
by a person and two machine-assisted passes) simply has more rows in the
reliability table. A machine coder being wrong is informative: it usually means
the instruction is ambiguous enough for a reader to get it wrong too.

One route is the **canonical** one — `Extraction/`, the reference coding the
analysis reads. Agreement never silently overwrites it; disagreements are
reviewed and written back deliberately.
