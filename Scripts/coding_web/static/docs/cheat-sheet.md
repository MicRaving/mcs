# Cheat sheet

CodingStudio turns a folder of papers into a coded, double-checked, pooled
evidence base. You screen each paper, code its experiments, compare your coding
with a second coder's, and pool the effects. Everything you do lives in a
**project folder**, and every step writes a plain file there — nothing is
hidden in a database.

## A project is a folder

| Folder / file | What lives there |
|---|---|
| `Papers/` | one PDF per study, named `Surname et al. (Year).pdf` |
| `Text/` | the PDF's text, same name, `.txt` — used by search and the text view |
| `Extraction/` | the **reference** coding: `<Key>.json`, one object per experiment |
| `Codings/` | one file per person who coded: `<Key> - <Initials>.json` |
| `Screening/` | abstract decisions, one file per screener |
| `Datasets/` | a study's public data, its `harmonizer.py` and the outputs |
| `Reproduction/` | `<Key>.md` — what was re-run and whether the paper's numbers came back |
| `Supplements/` | the paper's supplementary material |
| `Project/project.json` | the project's name and which scheme it uses |
| `Results/` | every product the analysis writes |
| `master_dataset.xlsx` | the screening workbook (abstracts and full-text verdicts) |

A study's **key** — `Surname et al. (Year)` — is the same string everywhere:
`Papers/`, `Text/`, `Extraction/`, `Codings/`, `Datasets/`, `Reproduction/`.
That is what lets the app line the files up.

## The workflow, in order

1. **Open the project.** Home, the box at the top.
2. **Screen the abstracts** (Screening tab). Four inclusion criteria, **C1–C4**;
   answer include / exclude / unsure for each.
3. **Screen the full text** (Coding tab → *Full-text exclusion*, at the top of
   the form). Same criteria, now against the whole paper.
4. **Code the paper** (Coding tab): its experiments, the interventions compared,
   and the outcome measures, with an effect size for each contrast.
5. **Let a second person code it.** They type their own initials in the coder
   field; their answers are saved in their own file and never touch yours.
6. **Compare** (Agreement tab → *Reliability*, then *Conflicts*). Anything the
   two coders disagree on is settled there.
7. **Bring the data together** (Harmonization tab) and **check each study**
   (Reproduction tab).
8. **Pool the evidence** (Analysis tab).

Screening and coding are independent jobs and can run in parallel. Agreement and
analysis need the codings to be finished first.

## Your first hour

1. Open the project folder (Home → *Open a folder*).
2. Type your initials in the coder field at the top right. Do this **first** —
   everything you save is filed under those initials.
3. Open **Screening** and screen five abstracts. It is the fastest way to learn
   what the corpus is about.
4. Open **Coding**, pick a paper, and read the form top to bottom without
   filling it in. The form is generated from the scheme, so it is also the
   definition of what you are expected to record.
5. Read the *Coding a paper* and *Screening* instructions here before you code
   for real.

## Keys

| Key | What it does |
|---|---|
| `Ctrl+K` | jump to the search box of the current tab |
| `Ctrl+S` | save the paper you are coding |
| `Tab` / `Shift+Tab` | in coder mode, next / previous variable |
| `1`–`9` | pick that level of a closed field (coder mode) |
| `Y` / `N` / `U` | in screening: yes / no / unsure, then next criterion |
| `Tab` in screening | accept the pre-filled answer and go to the next criterion |

## Where things are saved

* **Your coding** — `Codings/<Key> - <your initials>.json`, plus a readable
  `.md` note next to it. Saving twice overwrites only *your* file.
* **The reference coding** — `Extraction/<Key>.json`. Ordinary coding never
  writes there; the app only reads it.
* **Your screenings** — `Screening/<your initials>.json`, after every abstract.
* **The scheme** — next to the application, in `Schemes/<id>/`, not inside the
  project. Several projects can share one scheme.

Nothing is written until you press **Save** (coding form) or finish an abstract
(screening). A paper you closed unsaved is still there when you come back — but
it is not coded.

## When something looks wrong

| Symptom | Likely cause |
|---|---|
| A paper is greyed out or marked **missing** | its PDF is not in `Papers/` |
| The form is empty for a paper | no `Extraction/` record and no coding of yours — start from blank |
| A field you expect is not there | the scheme does not have it; the scheme is shared by the project, not fixed by the app |
| The Analysis tab shows nothing | no `Results/` products yet — the analysis has not been run for this project |
| Your coding vanished | check the coder field: you may have saved under other initials |

## What the app does *not* do for you

* It does not fetch papers. You put the PDFs in `Papers/`.
* It does not decide what belongs in the meta-analysis. You set
  `eligible_primary` on each contrast — that flag is the single most
  consequential judgement in the whole coding.
* It does not write your methods section. It gives you the numbers, the flow of
  papers, and the agreement statistics.
