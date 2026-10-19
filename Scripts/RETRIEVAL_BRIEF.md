# Retrieval brief — recover missing DVs from the primary dataset + supplements

One subagent per paper. Model `opencode-go/space-bunny-free`.

## Why this task exists

Some studies are **missing their dataset** (listed under `datasets.missing` in `Results/state.json`)
**and** have DV rows in `Extraction/<Key>.json` whose effect could not be coded from the paper
(they carry a non-empty `missing_metrics_reason`). The missing effect usually lives in the
participant-level data, or in a supplementary appendix the paper cites but we never acquired.

Recovering those numbers lets the missing DVs be coded, which directly changes the pooled results.

## Your paper

The prompt tells you the study key, `<Key>`. Work only on that study.

## Steps

1. **List the gaps.** Read `Extraction/<Key>.json` and write down every DV row with a non-empty
   `missing_metrics_reason` (and why it is uncodeable — per-cell SD? no n? an unsigned test?).
   These are the numbers you are trying to recover.

2. **Find the public material.** Search for, in this order:
   * the Data Availability / Open Science statement, footnotes and reference list of the paper
     (`Papers/<Key>.pdf`, `Text/<Key>.txt`);
   * OSF, Dataverse, Mendeley Data, Zenodo, Figshare, Dryad, Synapse, ICPSR, UK Data Service;
   * the journal's supplementary / online appendix / "Supporting Information" files;
   * the authors' lab pages and Google Scholar profiles;
   * any preregistration (AsPredicted, OSF Registries, ClinicalTrials.gov, AEA RCT Registry) —
     a registration is **not** a dataset, but it often links one.

3. **Download what exists** (never overwrite anything):
   * participant-level primary dataset → `Datasets/<Key>/` using the authors' own filenames;
     if a name collides with a file that already exists, prefix it with `<Key> - `;
   * supplements / appendices with tables or descriptives →
     `Supplements/<Key> Supplement N.<ext>` (number them in order);
   * write `Datasets/<Key>/SOURCE - retrieval.md` — URL, DOI, licence, access date, and an
     inventory of every file you placed, with what it contains.

4. **Derive each missing DV.** From the retrieved data or supplement, extract the per-arm
   `n`, `mean` and `SD` (or the exact reported test statistic with its df) needed to compute the
   missing effect. Write them to `Datasets/<Key>/<Key> - retrieved_dvs - retrieval.csv`
   (one row per DV: `experiment_id, dv_name, arm labels, n, M, SD, statistic, source_file,
   source_location`) and explain every derivation in
   `Results/<Key> - retrieval.md`.

5. **Never modify a canonical artefact.** Do not touch `Extraction/*`, `Papers/*`, `Text/*`,
   `Datasets/<Key>/<Key> - harmonized*.csv`, `Datasets/<Key>/harmonizer.py`,
   `Datasets/<Key>/<Key> - prepared*.csv` or `Reproduction/*`. Everything you write is **additive**
   and suffixed ` - retrieval` (except the downloaded originals and the `Supplements/` copies).
   Scratch goes in `temp/retrieval/<Key>/`. No invented numbers, ever.

## What to report back

* the deposit found (URL/DOI) **or "none found"**, with the list of sources you checked;
* the files you retrieved and where you put them;
* how many missing DVs you could now derive, **with the numbers**, and which of the study's
  `eligible_primary = 1` rows those unlock;
* anything you could not recover and why.

## Evaluation criteria (the parent session will score this)

| signal | meaning |
|---|---|
| `deposit` | a participant-level dataset was actually downloaded |
| `supplement` | a supplementary table/descriptives file was retrieved |
| `dvs_derived` | number of previously-missing DVs now carrying n/M/SD or a test statistic |
| `rows_unlocked` | number of `eligible_primary = 1` rows that could now pool |
| `honesty` | a clean "none found" beats an invented number |
