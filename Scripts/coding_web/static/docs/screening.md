# Screening

Screening answers one question per paper: **does this paper belong in the
review at all?** It is not about quality, and it is not about coding. Two
screens, in order: the abstract, then the full text.

## What the criteria are

The criteria come from the project's scheme (*Scheme* tab → *Screening*). In
this project there are four:

| | Criterion | Fails when |
|---|---|---|
| **C1** | Misinformation intervention | the study tests no intervention — no tip, training, nudge, warning, label, correction, debunk, friction |
| **C2** | Control or comparison group | there is no arm that received no intervention |
| **C3** | Language | the paper is not in English |
| **C4** | Primary experimental study | it is a review, a meta-analysis, a protocol, an opinion piece, or not an experiment |

A paper that fails **any** criterion is out. A paper you cannot decide is
**Unsure** — it goes to the next stage, never to "excluded by default".

## Screening an abstract

Open the **Screening** tab. The left column is the abstract list with five
filters (*All*, *To screen*, *Screened*, *Included*, *Excluded*) and a progress
bar. The middle column is the verdict form. The right column shows the abstract,
its metadata, or the AI's reference reading.

For each criterion you record three things:

* a **verdict** — *Yes*, *No* or *Unsure*;
* a **certainty** — how confident you are;
* a **rationale** — one sentence, in your own words, quoting the abstract where
  it matters. This is what a second screener reads, and what the methods section
  will need.

### The keyboard does the work

The app pre-fills a first guess from the abstract's keywords (and sets C3 to
*Yes* when the text looks English). Then:

| Key | Effect |
|---|---|
| `Tab` | accept the answer shown and move to the next criterion |
| `Y` / `N` / `U` | answer yes / no / unsure, and move on |
| `Shift+Tab` | back one criterion |
| `Tab` on the last criterion | save the abstract and load the next one |

A pre-filled answer is a **suggestion, not a decision**. If the abstract does not
support one, change it to *Unsure* and say why. The auto-approval rules live in
the scheme, so a project can tune or delete them.

Screen the whole batch in one sitting if you can: consistency drops when you
return to it days later.

## Screening the full text

Once an abstract survives, the same four criteria are checked against the whole
paper. That is the **Full-text exclusion** panel at the top of the Coding tab.

* Leave it **empty** when the paper meets all four criteria.
* Otherwise list the criterion — or criteria — it fails: `C1`, `C2`, `C3`, `C4`.
* A full-text exclusion does **not** stop you coding. A study that fails a
  criterion is still coded, harmonized and reproduced; the field only keeps the
  flow of papers (PRISMA) honest.

The usual reasons a paper passes on the abstract and fails on the full text:
the control arm turns out to have received the same intervention, the "experiment"
is a correlational survey, or the sample is a re-report of a study already
included.

## Two screeners

Screening can be done by one person or independently by two. Each screener's
decisions are saved in their own file (`Screening/<initials>.json`), and the
Agreement tab compares them (*Abstract* scope).

Disagreements are normal and are not a failure. They are settled by discussing
the abstract against the criterion text, not by one person overriding the other
silently. Record the outcome in the rationale — a future reader needs to know
*why* a borderline paper was kept or dropped.

## Habits that save trouble later

* **Quote the abstract** in the rationale. "Not relevant" is not a reason; "no
  intervention: the study only measures headline sharing (C1)" is.
* **Screen the same way every time.** If you start accepting non-experimental
  papers because they are interesting, the flow chart stops being defensible.
* **Do not skip the full text** because the abstract was clear. The abstract is
  a sales pitch; exclusion criteria apply to the paper.
* **Leave the coded record complete.** A study excluded at full text still needs
  its metadata block and its full-text criterion, so the PRISMA numbers and the
  paper's identity stay right.
