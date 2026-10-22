# Glossary

Terms you meet in the app, in the order you are likely to meet them.

**Abstract screening** — deciding, from the abstract alone, whether a paper should
be read in full. The first of the two screening stages.

**AC1 / AC2 (Gwet)** — chance-corrected agreement coefficients between coders.
AC1 is used for unordered categories, AC2 for ordered ones. They behave better
than Cohen's kappa when one level is very common.

**Arm** — one group of participants in an experiment. The *intervention arm*
receives the intervention; the *control arm* receives nothing.

**Coding key** — a study's identifier, `Surname et al. (Year)`, used as the file
name in every folder.

**Contrast** — one comparison between two arms on one outcome (optionally at one
timepoint). The unit the meta-analysis pools.

**d / g** — standardised mean differences. `d` is the raw difference divided by
the pooled standard deviation; `g` is `d` corrected for small samples (Hedges'
g). `g` is what usually gets pooled.

**d′ (d-prime)** — a signal-detection measure of how well someone tells true from
false items: `d′ = z(hit rate) − z(false-alarm rate)`. Higher is better
discernment. It separates *accuracy* from *response bias*.

**DerSimonian–Laird** — the standard random-effects estimator. It estimates the
average effect and the between-study variance (τ²).

**DiD (difference in differences)** — the change from pretest to posttest in the
intervention arm, minus the same change in the control arm. The pretest-corrected
effect.

**DV (dependent variable)** — an outcome measure. In the app: `dv_*`.

**Effect size** — the size of a difference on a standardised scale, so it can be
compared across studies with different measures.

**Eligibility (`eligible_primary`)** — the flag that says this contrast is the
paper's eligible intervention-versus-control comparison and may enter the pooled
analysis.

**Fully within** — a single-arm study with no control group. Excluded from the
pool by default.

**Harmonization** — converting a study's raw dataset into the project's common
shape: one row per participant, one column per variable, higher = better.

**Heterogeneity (τ², I²)** — how much the true effect differs across studies. `I²`
is the percentage of variation that is real difference rather than chance.

**Hit / false alarm** — calling a true item true / calling a false item true. The
two rates that make up d′.

**Intervention** — anything the study does to try to improve people's handling of
misinformation: inoculation, a sourcing or lateral-reading tip, a media-literacy
lesson, a fact-check, a debunk, an accuracy prompt, friction, a warning or
credibility label, a social norm, a community correction, exposure reduction.

**IRR (inter-rater reliability)** — agreement between two people coding the same
thing. In the app: the Agreement tab.

**k** — the number of pooled effects. Not the number of papers: one paper can
contribute several contrasts.

**Key (study key)** — see *coding key*.

**Meta-analysis** — a statistical summary of several studies' effect sizes, with
an estimate of the average effect and of how much it varies.

**Moderator** — a variable that explains why effects differ between studies or
participants (setting, intervention type, age …).

**Polarity** — which direction of a measure is the good one. Recorded per DV;
`higher_is_worse` measures are flipped before pooling.

**PRISMA** — the reporting standard for systematic reviews: how many records were
identified, screened, excluded and included, and why.

**Random effects** — a model in which each study estimates its own true effect,
drawn from a distribution; the pool estimates that distribution's mean.

**Reproduction (here)** — re-running a study's own analysis on its own data and
comparing the result with the published number. A check on the transcription, not
a new analysis.

**Scheme** — the project's definition of the coding form: blocks, fields, kinds,
conditions and screening criteria. Not hard-coded in the app.

**SDT (signal detection theory)** — the framework behind d′ and response bias: it
separates the ability to discriminate from the willingness to say "true".

**τ² (tau squared)** — the estimated between-study variance of the true effect.

**WEIRD** — Western, Educated, Industrialized, Rich, Democratic. A flag on a
sample, not a judgement.

**Wide format** — one row per participant, one column per variable. The app's
harmonized datasets are wide; nothing is melted for storage.
