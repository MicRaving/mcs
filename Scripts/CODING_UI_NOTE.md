# CODING_UI_NOTE — coder-mode highlighting, unified coder, compare column

_2026-09-22.  Changes to the Coding Studio coding tab (`Scripts/coding_web/static/`).
Backups (post-change record) in `temp/app_ui_backup/`._

## 1. Automatic highlighting reworked (coder mode)

The locate terms were a hand-written list whose first entry for `N_Recruited` was
`"participants"`, so focusing that field marked every occurrence of "participant" (and the
first hit won, so "recruited" was never tried).

* `CODER_LOCATE` now holds **specific stems** (`N_Recruited: ["recruit", "enrolled",
  "sampled"]`, `N_Analyzed: ["analy", ...]`, …).
* `coderTerms(def)` now derives terms from the field's **label** as well
  (`labelStems()` strips leading `N`/`Number of`/… and returns the phrase + its significant
  words), and drops generic noise words (`participants`, `sample`, `group`, …) from the
  auto-derived words.  Curated hints are kept verbatim (so `unit_of_analysis` still searches
  "participants"/"items").
* Net effect: `N Recruited` → searches **"recruit"** (prefix-matches recruited/recruitment)
  instead of "participant"; the first hitting term wins, so hits are far fewer and on-target.

Unit test: `temp/test_coderterms.js` (N_Recruited first term = "recruit"; label-derived
stems; generic fallback).

## 2. Unified coder dropdown (initials + source)

The top-bar coder field is now a **datalist-backed dropdown** (`#coder-input` +
`#coder-options`) listing every present coder (from `papers[*].codings` plus coders remembered
in `localStorage` `cs.coders`).  A coder can add themselves by typing a new name.

* `renderCoderOptions()` populates the list (at boot, on paper refresh, on commit).
* Committing a coder (`change`) sets `state.coder`, remembers the name, and **loads that
  coder's working copy** (`loadWorkingCopyForCoder()`): their saved coding, else the canonical
  extraction, else blank — i.e. the old separate "Source" switch is folded into the coder.
* The old `#source-select` was removed.

## 3. Compare values inline (replaces the source switch)

* The toolstrip's Source select is now a **Compare** select (`#compare-select`) listing the
  other coders with a coding for the current paper (plus `Original (OG)`).
* Choosing one shows the comparison coder's answer **inline, directly to the left of each
  input**, with a slight tinted background.  The badge is **green when it exactly matches the
  coder's own value and red when it differs** (`valuesEqual()` normalises numbers numerically,
  multi-value lists as unordered sets, strings trimmed/case-insensitively).  For schema fields
  the badge is right-aligned in the label column (immediately left of the control); for DV
  metric cells and DV timepoint inputs it is inserted just before the input.  Nothing else about
  the field grid, spacing or widths changes — the original layout is preserved.
* `renderCompareGutter()` (called from `renderForm()`) does the injection and removes the badges
  when compare is cleared.  `?compare=<coder>` (or `?compare=OG`) deep-links it.

Verified live: on `Alon et al. (2024)` with `?compare=MF`, the `N Recruited` badge ("3701") sits
at x 488–525 and the input starts at 537 — i.e. immediately left of the field, form width back
to its normal 650 px.

## 4. Original (OG) as a read-only "coder" in the top bar

* `#coder-options` now always offers **`Original (OG)`**; `isOriginalSource()` treats `OG` /
  `Original (OG)` as the canonical extraction.
* `loadWorkingCopyForCoder()` loads the extraction for OG (the "Source" fallback made explicit).
* `save()` **blocks** OG with a toast — the extraction is never overwritten; pick your own
  initials to save a coding.  `goNext()` skips the save for OG so browsing still advances.
* The coder input gets an `og-mode` tint while OG is active; the Compare dropdown omits the
  `Original (OG)` entry when you are already viewing it.
* Fix: the `input` handler sets `state.coder` before `change` fires, so the old
  `if (name === state.coder) return;` skipped loading entirely.  A `state.coderLoaded` marker
  now drives the early-return, so committing a coder really reloads the working copy.

## 5. Blind mode keeps the structure (values hidden)

`loadWorkingCopyForCoder()` in blind mode builds a **structure seed** from the extraction
(`blindSeedRecords()`): one record per experiment (Study_ID / Year / Authors / Title / DOI /
Publication), every intervention arm (id + type + label, no overrides) and one blank DV row per
original contrast (`dv_name` + `dv_domain` + arm binding).  **`Covariates` rows keep their
labels** (`covariate` + `dv_name`) but drop `r_val`.  `Rationale_Notes` and `Study_Description`
are hidden.  `stripEmptyForBlind()` persists the seeded structure (dropping empty intervention
overrides and empty DV rows).  Compare is disabled in blind mode (and the original-coder notes
panel is hidden), so no other coding leaks.

## 6. DV paper highlighting fixed (no stray "202")

* `dvNumberTerms()` no longer falls back to the generic effect-size order for a focused field;
  it searches **only that field's own value**.  The fallback was what highlighted "202" (an
  `n1i` sample size) for every empty field with no specific rule.  The fallback is now opt-in
  and used only by the explicit card-header jump (`coderLocateDvCard`).
* DV grid inputs now schedule a locate on focus **and** on change (not only keyboard
  navigation), so a DV field's number is highlighted in the paper in coder mode — including in
  blind mode, where the seeded rows give the coder a DV to select.

## Files

`static/app.js`, `static/index.html`, `static/styles.css`.  `Scripts/coding_web/CodingStudio.exe`
and the root `CodingStudio.exe` were rebuilt.

## Notes / follow-ups

* The compare column mirrors the fields **present in the current form**; if the comparison
  coder used a different module/intervention, only the shared field rows appear.
* `renderCompareGutter()` runs from `renderForm()`, so it follows experiment/intervention
  switches.
* The other tabs (Screening/Harmonizer/Summary) share the top-bar coder input; the coder
  `change` handler only loads a working copy while the Coding view is active.
