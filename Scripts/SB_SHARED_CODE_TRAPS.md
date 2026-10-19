# SB route — shared-code traps in the effect-conversion layer

**Status: OPEN. All four defects verified by direct execution, not inferred. None is caught
by `parity_check`, `validate_polarity`, the omnibus guard or `B_validate`, because all four
live *below* the layer those gates inspect.**

> ### IMPLEMENTED 2026-09-27 (supervision pass) — see the per-item notes below
> | item | change | effect |
> |---|---|---|
> | #2 `beta/n` fabricated variance | **disabled by default** in `stat_core.meta_effect` (opt back in with `ALLOW_BETA_N_VARIANCE=1`) | removes 175 rows / 13 studies of fabricated variance |
> | #3/#4 standardisation guard | `_RAW_EFFECT_RE` is now infix-aware (`odds_ratio`, `log_odds`, `unstandardized_beta`, ...), `unstandardis/ized` negates a marker, the conversion free text **no longer rescues** a raw type, and a generic type (`other`/`none`/...) fails closed | closes the ≤2,179-row fail-open path |
> | #8 `meta_did` r=0.5 | baseline-imbalance guard: above a 0.25 pooled-SD baseline gap use the conservative r=0 and set `baseline_imbalance` in the returned dict | stops an imbalanced arm taking ~20× weight |
> | #11 error-rate polarity | `dv_polarity.expected_polarity` treats a recognition false-alarm / false-positive / error rate as `higher_is_worse` on both veracities; the 10 dependent `Capewell et al. (2024)` canonical sidecar rows were corrected (original archived to `archive/_polarity_fix_20260927/`) | `validate_polarity` PASS, `audit_polarity` 0 mismatches |
> | #6 fully-within identity | `validate_fully_within` now asserts the bound `default <= opt-in <= default + n_fw` instead of an exact equality (one fully-within row is now correctly non-poolable) | gate consistent with the guards |
>
> **Consequence for the products.** The meta pool moved exactly as intended: posttest
> **k 3,115 → 2,871**, est **0.206 → 0.212**. `parity_check` therefore reports divergence
> against the pre-built app/workbooks until they are regenerated. Items #1 (a *visible* skip
> log for the 1,079 silently-dropped rows), #5 (fully-within convention), #6 (timing
> classifiers ... see item 6 in the list below) and #9 (a `Text/` minus-sign / column audit)
> remain to do.

The pipeline's effect-conversion code silently mis-handles four classes of coded row. A
coder **cannot rely on the pipeline to drop a bad row** — you must police these yourself.
Probes that demonstrate each one live in `temp/sb_route_refill/`
(`probe_f_route.py`, `probe_std_guard_odds.py`, `recheck_std_guard.py`,
`count_dead_eligible.py`, `count_fabricated_variance.py`, `size_std_guard_gap.py`).

---

## 1. `F_val` is absent from `stat_core.meta_effect` — row silently never pools

`meta_effect` has branches for `g_val`, `d_val`, `beta_val`, `t_val`, `r_val`, `yi/vi` and the
per-condition descriptives. There is **no `F_val` branch** (0 occurrences in the function
source). A row whose only metric is an F returns `(nan, nan, 'none', 'none')`.

`contract.dv_has_effect` *accepts* such a row and `B_validate` passes it, so it reads
`eligible_primary = 1` in the DV_Metrics sheet and then vanishes — `metaanalysis.py` builds
its pool with `q_yi.notna() & q_vi.notna()`, which drops it. Unlike a multi-df omnibus (which
`SC.log_omnibus_skip` records in the `Omnibus_Guard` sheet), **this drop leaves no trace.**

Current exposure: **960 of 10,018 eligible rows (9.6%)** — 631 SB / 329 canonical, 95/99 studies.

**What to do.** A 1-df F is a two-cell comparison in principle, so it is *not* covered by the
multi-df prohibition. But χ²/F carries no sign: without cell means or a stated direction the
effect cannot be signed and must not be pooled. Prefer a route that converts — per-condition
`n/M/SD`, `t`, `r`, a reported `d`/`g`, or `beta + SE`. If only an F exists, set a non-empty
`missing_metrics_reason` naming the paragraph and table you searched.

## 2. `beta/n` FABRICATES a variance (worse than #1 — the row *does* pool)

```python
nt = num(dv.get("n_total"))
if not np.isnan(nt) and nt > 3:
    return b, 1.0 / max(nt - 3, 1), "beta", "beta/n"     # stat_core.py ~line 776
```

`1/(n−3)` is the **Fisher-z variance**. It is correct for the `r→z` and `t→z` branches
immediately below it. Applied to a **raw regression coefficient** (log-odds, scale-point
slope) it is dimensionally wrong: the variance of *b* depends on residual SD and predictor
scale, not on *n* alone.

So where defect #1 produces a row that is dropped, this produces **a real point estimate
carrying an implausibly small SE**, which then takes near-dominant weight in a random-effects
model. Current exposure: **175 eligible rows across 13 studies** (113 SB / 62 canonical;
implied SE 0.0015 – 0.2109). Both routes are affected, so this reaches canonical numbers.

**What to do.** If the paper gives a bare coefficient with only a significance marker and no
SE / CI / *t* / *F*, set **`n_total = null`** (leave the route unusable) and give a non-empty
`missing_metrics_reason` naming the table/paragraph searched. Record `beta_val` verbatim for
the record. Never supply *n* to make a route fire.

## 3. The standardisation guard fails OPEN on a blank / non-vocabulary type

`stat_core.source_is_standardised(src, reported_type, conversion_text)` refuses a row **only**
when `reported_type`/`conversion_text` carry a *raw-effect token* with no standardised marker
alongside. Therefore:

| `effect_size_type_reported` | guard verdict |
|---|---|
| `"beta"` | **False** (correctly refused) |
| `"logistic regression"` | **False** (correctly refused) |
| `"odds_ratio"` | **True** — wrong, see #4 |
| `"other"` | **True** — wrong, no raw token at all |
| `""` (blank) | **True** — wrong, no raw token at all |

A row tagged `"other"` with a `yi` on a 0–100 scale returns `(0.2, 3.28, "yi")` and is pooled
alongside `d`/`g`/`z`.

**What to do.** Never leave `effect_size_type_reported` blank or generic on a raw effect, and
do not rely on the guard — set `eligible_primary = 0` yourself.

## 4. `\b` word boundaries cannot match across `_`, and a stray marker rescues a raw row

`_RAW_EFFECT_RE = r"\b(beta|log|logit|logistic|odds|or|coef|coefficient|regression|b)\b"`.
`_` **is a word character**, so there is no boundary between `odds` and `_ratio`:
`re.search(r"\bodds\b", "odds_ratio")` is `None`. Defeated spellings include `odds_ratio`,
`log_odds`, `logit_model`, `logistic_regression`, `unstandardized_beta`, `risk_ratio`,
`hazard_ratio`, `incidence_rate_ratio`, `mean_difference`, `regression_coefficient`,
`oddsratio`. A space-separated `"odds ratio"` *is* correctly refused.

Separately, `_STD_MARKER_RE` contains `\bstandardis` with **no trailing `\b`**, plus bare
`\bd\b`, `\bg\b`, `\bz\b` — and it is matched against your **conversion free text**. So a
conversion note that merely *mentions* `d`, `g`, `z` or "standardised" **re-enables** a row
whose reported type is plainly `beta`:

```
source_is_standardised("yi/vi", "beta", "")                      -> False   (refused)
source_is_standardised("yi/vi", "beta", "converted to d")        -> True    (rescued)
source_is_standardised("yi/vi", "beta", "standardised mean difference") -> True (rescued)
```

This is what actually rescues the mass of rows: 1,734 of the 2,179 raw-`beta` rows the guard
accepts report `effect_size_type_reported == "beta"` and are admitted purely by a
standardised marker in their conversion text. Observed markers in real rows: `['d','d']`,
`['standardis','d','d','d','z']`.

**Consequence: do not try to defeat the guard by wording.** There is no wording that reliably
works in both directions. Set `eligible_primary = 0` and document it.

Upper bound on the guard's blind spot: **2,179 eligible rows across 154 studies** whose metric
is raw `beta` but which the guard calls standardised (1,167 canonical / 1,012 SB; routes
`yi/vi` 1,517, `beta/se` 518, `beta/n` 144). This is an **upper bound, not a miscount** — many
are legitimately standardised (`standardized_beta`, `standardized mean difference`,
`d (computed from reported means/SD)`) and merely pass a guard that is not consulted. The
`beta/n` subset (144) and the `odds_ratio` rows (41) are unambiguous.

---

## What IS a poolable effect

Only a genuinely standardised `d`/`g`/`z`. In descending order of preference:

1. **per-condition `n/M/SD`** (always poolable, and the only route that survives a broken guard)
2. `t` + arm *n* (→ Fisher z)
3. `r` + *n* (→ Fisher z)
4. a **reported** `d`/`g`
5. `beta + SE` **only if the coefficient is already in SD units**

Never: a p value converted to a z; an invented SD; an M1/M2/p table with no SD and no *n*
(the precision is unrecoverable); a multi-df omnibus; an unsigned 1-df F or χ².

### A legitimate rescue for a raw slope

If the paper reports a regression coefficient **and** the variance components of its mixed
model (e.g. a between-unit σ²u and a within-unit σ²e), the total SD is `√(σ²u+σ²e)`, so

```
yi = beta / sqrt(sigma2_u + sigma2_e)        # coefficient in SD units
vi = (SE / sqrt(sigma2_u + sigma2_e))**2
```

This is a real standardisation from reported quantities — not an invented denominator — and it
is auditable and reversible. Keep `n_total` null and record the derivation per row. This is how
`VANDERHOVEN et al. (2014)` recovered 27 otherwise-unpoolable rows.

---

## Route conventions that are settled (several subagents converged on these)

* **Fully-within / single-arm** (no no-misinformation-intervention control): `eligible_primary = 0`
  on all rows, `Fully_Within = true`, and `C2` on `Fulltext_Exclusion_Criterion` if the study has
  no comparison group at all. *Caveat: the canonical route instead uses `eligible_primary = 1` plus
  the `INCLUDE_FULLY_WITHIN` switch. The two are equivalent only for default products; under the
  opt-in switch they diverge. 13 studies currently disagree — see below.*
* **Active control** — a control arm that itself delivers a misinformation intervention (a
  fact-check arm, a generic-tips / accuracy-prompt arm, a "related articles" arm) is **not** a
  no-intervention control. Code those rows `eligible_primary = 0`, `contrast_type = 2`, and give it
  a non-`control` role so `reanalysis.py` cannot pool it with a pure control.
* **One eligible specification per experiment × outcome.** Sibling *specifications* of one contrast
  (different covariate sets, missing-data estimators, model variants) and sibling *arms* sharing one
  control are `eligible_primary = 0`. Otherwise a single trial is multiply counted — Table 5's
  3 covariates × 3 estimators would count one trial nine times.
* **Nulls are findings.** Pool them. Never make a sibling arm or spec ineligible because it is null.
* **Sign convention must be derived, not assumed.** Check `d = t/√n` vs `d = t/√(n1+n2)` and which
  direction the difference runs; papers reverse their own convention mid-paper. Orient so
  positive = desirable (intervention − control, post − pre). A log-odds on a `higher_is_worse` DV
  inverts.
* **No deposit** → write no harmonizer, no ` - SB` products, no empty `Datasets/` folder, and no
  reproduction script with nothing to read. Document the planned harmonization schema in the coding
  `.md` instead, and say so in your reply.
* **`cell_descriptives` is not free documentation.** A *non-empty* `cell_descriptives` list (even one
  carrying only `n: null` placeholders) makes `suite_common.dv_has_descriptives` return True and
  therefore **silently disables the `is_omnibus_eligible` guard** — a multi-df omnibus row then drops
  out of the pool with no `Omnibus_Guard` trace instead of being flagged. On an omnibus row, either
  give real per-cell `mean`/`sd`/`n` (so the descriptives route is a genuine effect) or leave the
  list empty and record the searched locations in `missing_metrics_reason`.
* **Only ONE experiment record per trial.** Arms of one 2×2×2 are arms, not studies; per-country
  replications may be separate records.

## Open items for the campaign owner (do not "fix" these in passing)

1. `F_val` branch missing from `meta_effect` (960 rows).
2. `beta/n` fabricating `1/(n−3)` (175 rows).
3. Standardisation guard fails open on blank/`"other"`, defeated by `_`-joined names, and rescued
   by stray markers in free text (≤2,179 rows).
4. **Two pooling paths**: `analysis_multiverse_meta.build_spec` applies the standardisation filter;
   the local `pool` built in `metaanalysis.py` (~lines 289–310) does **not**. Which sheets the
   unfiltered path feeds is unresolved, and it determines whether defects 1–3 are in the published
   numbers or only in diagnostics.
5. **Fully-within convention differs between the routes** (13 studies) — the
   `INCLUDE_FULLY_WITHIN` switch is therefore not a controlled comparison.
6. `validate_polarity` compares sidecars against `dv_polarity.py`, i.e. against the same reference
   implementation, so a systematic misclassification validates as correct. Known gaps in that file:
   `false_alarm` is read as a *suspicion* rating (→ `higher_is_better`) when a false-alarm **error
   rate** is `higher_is_worse`; and `_TR_RECOG` has no bare `accuracy` term, so `*_accuracy` DVs fall
   through to the suspicion default and get `true → higher_is_worse`.
7. **No `odds_ratio` branch in `meta_effect`, yet the standardisation guard admits `odds_ratio`.**  An
   OR-only row passes `dv_has_effect` and then **silently never pools**.  Flagged independently in
   the SB route by Xiang et al. (2024), Winters (2021) and Winters et al. (2021); their workaround is
   to recompute per-condition n/M/SD from the deposit (route a) and keep the ORs as provenance only.
   Either add the `ln(OR)` + Wald-variance branch or make the guard refuse `odds_ratio`.
8. **Two timing classifiers disagree.**  `analysis_multiverse_meta` reads a 2–7-day post-test
   interval as *follow-up* (and excludes it from `post_all`), while `validate_extraction` calls the
   same row `post` — flagged by Wiley et al. (2009) for its transfer rows.  The three questions are
   supposed to be disjoint, so the two classifiers must share one definition.  Decide whether a
   short-delay *transfer* row belongs in the immediate-posttest or the persistence pool.
9. **`Text/*.txt` is unreliable for tables, and `text_quality.csv` does not detect it.**  Verified
   instances: Wang & Zhang (2023) loses the minus sign of 13/14 negative coefficients (PDF `-.120**`
   → text `.120**`; a coder reading the text would invert the accuracy-nudge effect); Xiang et al.
   (2024) has Table 4's column order scrambled (naive read gives an OR of 3.40 where the true value
   is 0.356); Andrews, Wade, Vraga (2009/2010/2019) and others only got correct numbers by
   re-parsing the PDF by word coordinates.  `text_quality.csv` rate-documents `garbled=False` because
   it measures glyph/stopword ratios only.  Recommend a targeted audit of any canonical coding whose
   numbers came from `Text/` for a table-heavy paper.
10. **`meta_did`'s r = 0.5 assumption can dominate the DiD pool.**  `meta_did` assumes a pre/post
   correlation of 0.5 and uses the smaller pretest SD, so a derived DiD SE can be far smaller than
   the authors' own (Wineburg et al. (2022): 0.099 vs the paper's 0.44) — that row then enters with
   ~20× the authors' weight.  Will (2002) independently derived r = 0.494 from its own paired *t*, so
   0.5 is defensible in general but should not be applied blindly to baseline-imbalanced arms.
11. **A reference-implementation misclassification that `validate_polarity` cannot see.**  Concrete
   case found this wave: `dv_polarity.py` treats a recognition-task **error rate**
   (`..._false_followup_falsealarm`) as a *suspicion* rating, i.e. `false → higher_is_better`, when an
   error rate is `higher_is_worse`.  The canonical `Capewell et al. (2024)` sidecar (10 rows) matches
   the reference, so the gate passes.  Fixing the rule flips those 10 canonical rows, so it is a
   campaign-owner decision (the SB sidecars already use the correct `higher_is_worse`).
