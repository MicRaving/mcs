# The analysis

The analysis tab pools the codings into one estimate per question, and shows you
what it is made of. It is the last step, and the only one that needs the coding to
be finished.

## What is being pooled

Every *eligible* contrast — intervention arm versus a control arm that received no
intervention — contributes one **effect size**. The effect size is expressed on a
standardised scale so that studies with different measures can sit together:

* **d** — the difference between arms in standard-deviation units;
* **g** — the same, corrected for small samples (Hedges' g). This is what gets
  pooled most of the time;
* a correlation **r**, converted to the same scale.

The sign is always *intervention minus control, positive = better*. A study coded
backwards pulls the pooled estimate the wrong way, which is why the direction of
each measure is recorded during harmonization.

## Random effects, and why

A fixed-effect model assumes every study estimates the same true effect. That is
never true here: the interventions differ (a tip, a game, a warning), the
populations differ, the outcomes differ. So the model is **random effects**
(DerSimonian–Laird): it estimates the average effect *and* how much the true
effect varies between studies (τ²). The confidence interval is wider than a
fixed-effect model would give — that is the honest answer, not a penalty.

**Heterogeneity** is reported as τ² and I²:

| I² | Read it as |
|---|---|
| below 25% | the studies broadly agree |
| 25 – 75% | moderate; look for moderators |
| above 75% | the studies disagree a lot; a single average is a poor summary |

A large I² is not a bug. It means the *question* has no single answer, and the
moderator models matter more than the pooled number.

## Three questions, never pooled together

| Question | What it answers | Which rows |
|---|---|---|
| **Immediate posttest** | did the intervention work, right after it? | post-test outcomes |
| **Follow-up** | did it last? | delayed posttest only |
| **Pretest-corrected (DiD)** | did the change from before to after differ between arms? | studies with a baseline |

Mixing them is the classic error: pooling a follow-up with an immediate posttest
answers a question nobody asked, and pooling a baseline row as an outcome does
the same. The controls above the results (*Pretest*, *Follow-up*) decide which of
the three you are looking at, and the app never merges them into one number.

## The discrimination re-analysis

The second layer pools **signal-detection** measures when a study's outcome is
"correctly told apart from not" rather than a rating.
From each study's data it computes:

* the **hit rate** (calling true items true) and the **false-alarm rate**
  (calling false items true);
* **d′** = z(HR) − z(FAR), the standardised ability to tell true from false;
* the difference in d′ between arms: the intervention's effect on *discernment*,
  as opposed to its effect on what people say they would share.

This is the sharper test whenever an arm could plausibly change *willingness*
rather than *ability*: "did less of the bad thing" is ambiguous — it can mean
"recognised it as bad" or "did less of everything".

## Moderators and subgroups

Right of the results you can add moderators: study-level (intervention type,
setting, sample) and, in the re-analysis layer, participant-level ones (age,
education, political leaning). The output is a coefficient per moderator:
whether the effect is larger in one group than another, with its interval.

Moderators are hypotheses. With few studies, a moderator model will fit something
to any split. Prefer few, pre-specified ones; treat exploratory splits as
descriptive.

## Controls that change the answer

| Control | Effect |
|---|---|
| **Frequentist / Bayes** | Bayesian gives a posterior mean and credible interval; the priors are shown and can be changed |
| **Credibility only** | restricts the pool to studies judged credible on the pre-specified criteria |
| **Apply author exclusions** | applies the studies' own attention checks (recommended on) |
| **Include fully-within studies** | single-arm, no-control studies are excluded by default; this opts them in |
| **Subgroup** | restricts the pool to a subgroup, e.g. lateral-reading studies |
| **Layer** | Meta-Analysis (coded effects) or Re-Analysis (data-derived d′) |

## Reading the output

* The **pooled estimate** with its interval: the number you report.
* The **forest plot**: each study's effect and its weight. A single study with a
  huge weight means the pool is really that study.
* **k** (number of contrasts) and the number of studies behind them: k is not the
  number of papers. One paper can contribute five contrasts, and those five are
  not independent — tighter intervals than reality if you forget it.
* The **funnel plot / publication bias** view: with fewer than ten studies per
  question, it cannot tell you much, and its absence of asymmetry proves nothing.

## What the analysis will not tell you

* Is the literature biased? Not really — it can only hint.
* Which intervention is best? Only for the moderators that were coded, and only
  in terms of the outcomes that were measured.
* Whether an effect matters in the world. That is a judgement about the outcome,
  not about its standardised size.
