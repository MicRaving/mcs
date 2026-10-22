# Coding a paper

Coding is the part of the work only a person can do: reading a paper and deciding
what its experiments did, what they measured, and how big the effect was.

## The screen

The Coding tab is three columns.

* **Left — the paper list.** Every study in the project, with its coding status,
  the number of coders who have touched it, and a flag for missing metrics.
* **Middle — the form.** Everything you record about the paper.
* **Right — the paper.** The PDF (or its text, if the PDF is slow or unreadable).

Drag the columns' edges to resize them. The little arrows on each edge hide a
column so the form gets the whole window.

## One paper, several experiments

Most papers report more than one experiment. Each gets its **own record**, using
the `Experiment` tabs at the top of the form (`1`, `2`, `3`, `+`).

Rules of thumb:

* A new **experiment** is a new sample. New participants → new record.
* A new **condition** inside the same sample is not a new record. It is another
  arm in the same record, and it shows up as extra contrasts in the measures at
  the bottom.
* A **follow-up** measurement on the same participants is not a new record. It is
  another timepoint on the same measure.

Getting this wrong is the most common coding error, and it is expensive: the
analysis cannot merge records after the fact.

## The blocks

The form is generated from the project's scheme, so the block names differ
between projects. The usual ones:

| Block | What goes in it |
|---|---|
| **Metadata & Identification** | key, year, authors, DOI, title, a one-line description |
| **Sample** | N recruited and N analysed, age, gender, country, WEIRD flags |
| **Open science & design** | data availability, preregistration, design, arms, control type |
| **Interventions** | what each arm received: inoculation, sourcing tip, accuracy prompt, … |
| **Measures / DVs** | one row per outcome × contrast, with its effect size |

Fields marked with a red dot are required. Every field also carries an
instruction in the scheme; the *Scheme* tab's *Prompt* pane prints the form as
the prompt an agent would code from, instructions and all.

## Interventions

Open the **Interventions** strip under the experiment tabs. Every arm of the
experiment is listed; for each one you record what it was given.

* The **control arm** is the arm that received no misinformation intervention:
  no tip, no prompt, no warning, no training. A "treatment as usual" arm counts
  as control only if it really had none of the intervention's active ingredient.
* A second **active intervention** arm (e.g. inoculation *and* a fact-check) is
  another intervention, not a control.
* The coding records the intervention's **type**, its **dose** (how long, how
  often) and, where the scheme asks, whether it was a correction (after exposure)
  or a prebunk (before exposure).

The pairing of arms is what the whole analysis rests on: a contrast is
*intervention minus control*.

## Measures and effect sizes

Each **measure row** is one outcome variable in one contrast. For every row:

1. **Name the variable** (`dv_…` or the scheme's label) and what higher means —
   more discernment, more sharing, more accuracy.
2. **Say what is compared to what.** `contrast_type` records it; `eligible_primary`
   (1/0) says whether this row is the paper's eligible intervention-versus-control
   contrast.
3. **Give the numbers.** The form accepts, in order of preference:
   * per-condition `n`, `M`, `SD` — always preferred, always poolable;
   * a pairwise statistic: `t`, or `F` with `df_num = 1`, or a correlation `r`;
   * a reported `d` or `g` with its sample sizes;
   * last resort: an omnibus statistic.

**Never put a multi-df omnibus (`F` with `df_num > 1`, a `chi²`, an `η²`) in an
eligible row.** It compares more than two cells at once, it has no sign, and it
cannot be turned into one honest two-arm effect. If the paper reports only that,
search the same paragraph and the tables it points to for per-condition means or
a pairwise test. If there really is none, leave the metrics empty and write why
in `missing_metrics_reason` — naming the paragraph or table you searched. An
empty cell with a reason is fine; an invented number is not.

### Direction and sign

Code the effect in the paper's own direction and record what higher means on the
measure. The analysis flips measures that are better when lower (so more
misinformation sharing is *worse*), using that recorded direction. If you record
direction inconsistently, the pooled estimate silently changes sign — check it.

## Marking the source

While a field is focused, select the sentence in the PDF and press **Mark
source**. The app stores the page, the quoted text, and the field it belongs to.
Later, anyone reading the coding (or an agent auditing it) can jump to the exact
sentence.

Mark at least every effect size and every eligibility decision. It is what makes
a disagreement quick to settle: you are not arguing about memory, you are
comparing two sentences.

## Saving

**Save** (or `Ctrl+S`) writes `Codings/<Key> - <your initials>.json` and the
matching `.md` note. It never touches another coder's file and never touches
`Extraction/<Key>.json`.

The **Save** button changes colour while there are unsaved changes (and the
window title gains a leading dot). Switching paper, switching coder, or closing
the window with unsaved changes asks first.

## Coder mode and blind mode

* **Coder mode** (the ▶ button) keeps your hands on the keyboard: `Tab` and
  `Shift+Tab` step through variables, `1`–`9` pick a level of a closed field,
  and the form scrolls to whatever has focus. Use it once you know the form.
* **Blind mode** hides the reference coding and every other coder's answers.
  Use it when your coding must not be influenced by what someone else wrote —
  in particular when you are the second coder and agreement is being measured.
* **Compare** (the dropdown above the form) shows another coder's value in a
  small column beside each field. It is a reading aid for review, not part of
  blind coding.

## Judgement calls worth slowing down for

* **Is this an eligible intervention?** A true/false "is this headline accurate?"
  task is a measure, not an intervention. A warning label, a nudge, a literacy
  tip, a debunk, a correction, a friction step — those are interventions.
* **What is the control?** No intervention at all. A no-information arm with the
  same task is still a control.
* **One row or two?** One row per distinct outcome × contrast × timepoint.
  Two measures of the same construct (e.g. sharing intention and sharing
  behaviour) are two rows, not one averaged row.
* **Whose N?** The N analysed, not the N recruited, when the paper distinguishes
  them. Record both if the form allows it.
