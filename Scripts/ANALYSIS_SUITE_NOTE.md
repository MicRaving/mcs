# ANALYSIS SUITE NOTE

<!-- META-SECTION:START -->
## Meta (reported-effects) layer

_Generated 2026-11-23 16:28 by `Scripts/analysis_multiverse_meta.py`._

This is the **meta (reported-effects)** section of `ANALYSIS_SUITE_SPEC.md`; the signal-detection (SDT) section is produced by the SDT-layer session.

## Definitions

- **Unit**: one eligible coded contrast (`eligible_primary == 1`); contrasts are nested in studies.

- **post_all / post_cred**: the immediate-post contrast (follow-ups excluded). The effect is the coded/reported effect, or a Hedges' g derived from posttest cell means when the coded effect is missing.

- **persistence_all / persistence_cred**: follow-up only. The same effect rule as the posttest, applied at the follow-up timepoint. Never pooled with the immediate posttest.

- **did_all / did_cred**: `(post_IG - pre_IG) - (post_CG - pre_CG)`, standardised by the pooled pretest SD, requiring a pretest and an immediate posttest. Pre-post correlation r = 0.50 primary; r = 0.30 / 0.70 sensitivity (see `r_sensitivity`).

- **Baselines**: pretest rows are never outcomes; they enter only the DiD.

- **Polarity**: every effect is oriented so positive = more desirable, using the coded `dv_polarity` (1 = higher is better, 0 = higher is worse); reported effects are multiplied by the sign and derived-from-descriptives effects are already oriented. The unoriented reported value is kept in `reported_yi`; contrasts with unknown polarity are flagged `oriented = 0`.

- **Models (dependence-aware, Option C)**: every pooled cell reports the plain one-level DerSimonian–Laird estimate (`dl_*`, the previous model, kept as the comparison column), a **three-level** random-effects model (`three_*`: effect within experiment x sample within study, with `tau2_cluster` and `tau2_study`), and **robust variance estimation** (`rve_*`) with cluster-robust SEs clustered by `cluster_id` (study x experiment x shared control arm x DV family), at rho = 0.5 and rho = 0.8 (`rve_rho80_*`), mirroring the DiD r sensitivity. `max_weight_share_dl` / `max_weight_share_rve` report the inverse-variance weight share of the largest contributing experiment/cluster per spec x domain, so a single large experiment (e.g. Biddlestone et al. 2026) can be shown not to dominate.

- **Family rule (primary composite / secondary components)**: the meta layer and the SDT re-analysis are separate products and are never mixed. In the meta layer the primary pool is the **composite**: when a cell codes both `separate_true` and `separate_false` effects the composite is constructed from them as `effect_true_oriented + effect_false_oriented` (variance = sum of the two variances; the positive within-person covariance is unavailable, so this is conservative), and a directly reported composite in the same cell is demoted; when the pair is unavailable the directly reported composite is used; `other` DVs always stay primary. Every `separate_*` effect is routed to the `Components_Strand` sheet (sensitivity/specificity), where the Biddlestone non-manipulative backfire stays visible, and is never pooled with a composite. The suppressed-contrast audit is `Scripts/NONREDUNDANCY_SUPPRESSED.csv`.

- **Skipped** 0 extraction files changed in the last 15 min.

- **DiD quality guard**: 2 contrasts with |standardised DiD| > 3.0 were dropped (coded SDs behaving like SEs) and listed in `temp/meta_did_excluded.csv`.

## Effects per spec (meta layer)

| spec | exclusions | scope | k | est | lo | hi | i2 | two_level_est | two_level_lo | two_level_hi | cr_se | cr_lo | cr_hi |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| did_all | ON | unrestricted | 529 | 0.3990 | 0.3609 | 0.4371 | 92.9383 | 0.4334 | 0.3407 | 0.5261 | 0.0508 | 0.2995 | 0.4985 |
| did_all | ON | restricted | 168 | 0.5123 | 0.4242 | 0.6004 | 95.2846 | 0.5092 | 0.3585 | 0.6598 | 0.0733 | 0.3687 | 0.6559 |
| did_all | OFF | unrestricted | 529 | 0.3852 | 0.3474 | 0.4230 | 92.9377 | 0.4132 | 0.3241 | 0.5023 | 0.0506 | 0.2860 | 0.4843 |
| did_all | OFF | restricted | 168 | 0.4657 | 0.3761 | 0.5553 | 95.5656 | 0.4589 | 0.3068 | 0.6110 | 0.0757 | 0.3173 | 0.6141 |
| did_cred | ON | unrestricted | 155 | 0.5148 | 0.4224 | 0.6073 | 95.4526 | 0.4992 | 0.3446 | 0.6538 | 0.0783 | 0.3614 | 0.6682 |
| did_cred | ON | restricted | 155 | 0.5148 | 0.4224 | 0.6073 | 95.4526 | 0.4992 | 0.3446 | 0.6538 | 0.0783 | 0.3614 | 0.6682 |
| did_cred | OFF | unrestricted | 155 | 0.4703 | 0.3764 | 0.5641 | 95.7073 | 0.4300 | 0.2749 | 0.5850 | 0.0803 | 0.3128 | 0.6277 |
| did_cred | OFF | restricted | 155 | 0.4703 | 0.3764 | 0.5641 | 95.7073 | 0.4300 | 0.2749 | 0.5850 | 0.0803 | 0.3128 | 0.6277 |
| post_all | ON | unrestricted | 3294 | 0.2144 | 0.2073 | 0.2216 | 94.5794 | 0.2922 | 0.2630 | 0.3215 | 0.0151 | 0.1848 | 0.2440 |
| post_all | ON | restricted | 1816 | 0.2098 | 0.2014 | 0.2181 | 94.7077 | 0.2874 | 0.2501 | 0.3248 | 0.0203 | 0.1700 | 0.2496 |
| post_all | OFF | unrestricted | 3294 | 0.2073 | 0.2001 | 0.2146 | 94.7015 | 0.2870 | 0.2580 | 0.3160 | 0.0151 | 0.1778 | 0.2369 |
| post_all | OFF | restricted | 1816 | 0.1977 | 0.1893 | 0.2061 | 94.8541 | 0.2802 | 0.2433 | 0.3170 | 0.0203 | 0.1579 | 0.2375 |
| post_cred | ON | unrestricted | 1518 | 0.2224 | 0.2104 | 0.2344 | 94.5475 | 0.2830 | 0.2421 | 0.3238 | 0.0236 | 0.1761 | 0.2687 |
| post_cred | ON | restricted | 1518 | 0.2224 | 0.2104 | 0.2344 | 94.5475 | 0.2830 | 0.2421 | 0.3238 | 0.0236 | 0.1761 | 0.2687 |
| post_cred | OFF | unrestricted | 1518 | 0.2099 | 0.1975 | 0.2224 | 94.9546 | 0.2749 | 0.2347 | 0.3151 | 0.0236 | 0.1637 | 0.2562 |
| post_cred | OFF | restricted | 1518 | 0.2099 | 0.1975 | 0.2224 | 94.9546 | 0.2749 | 0.2347 | 0.3151 | 0.0236 | 0.1637 | 0.2562 |
| persistence_all | ON | unrestricted | 445 | 0.1736 | 0.1600 | 0.1871 | 86.8114 | 0.2248 | 0.1662 | 0.2834 | 0.0323 | 0.1103 | 0.2368 |
| persistence_all | ON | restricted | 210 | 0.1827 | 0.1651 | 0.2002 | 89.3225 | 0.2259 | 0.1474 | 0.3045 | 0.0444 | 0.0956 | 0.2697 |
| persistence_all | OFF | unrestricted | 445 | 0.1707 | 0.1572 | 0.1842 | 86.6836 | 0.2197 | 0.1607 | 0.2786 | 0.0322 | 0.1076 | 0.2338 |
| persistence_all | OFF | restricted | 210 | 0.1836 | 0.1661 | 0.2011 | 89.1419 | 0.2241 | 0.1459 | 0.3023 | 0.0448 | 0.0958 | 0.2715 |
| persistence_cred | ON | unrestricted | 158 | 0.1761 | 0.1569 | 0.1953 | 90.9542 | 0.2415 | 0.1484 | 0.3345 | 0.0543 | 0.0696 | 0.2826 |
| persistence_cred | ON | restricted | 158 | 0.1761 | 0.1569 | 0.1953 | 90.9542 | 0.2415 | 0.1484 | 0.3345 | 0.0543 | 0.0696 | 0.2826 |
| persistence_cred | OFF | unrestricted | 158 | 0.1773 | 0.1581 | 0.1965 | 90.7832 | 0.2393 | 0.1468 | 0.3318 | 0.0550 | 0.0695 | 0.2851 |
| persistence_cred | OFF | restricted | 158 | 0.1773 | 0.1581 | 0.1965 | 90.7832 | 0.2393 | 0.1468 | 0.3318 | 0.0550 | 0.0695 | 0.2851 |

## Credibility fallbacks (sharing_intention used where no credibility contrast)

Fallback contrasts: **1820** across 56 studies.

## Exclusions ON vs OFF

- Harmonized re-derivations (no exclusion filters): **294** contrast-runs.
- `paper_only`: no usable data, coded effect retained (author sample embedded).
- `harmonized_unmatched`: data exist but no confident DV match, coded effect retained.

| spec | exclusions | category | n |
| --- | --- | --- | --- |
| did_all | OFF | derived:components | 11 |
| did_all | OFF | harmonized_rederived | 30 |
| did_all | OFF | harmonized_unmatched | 161 |
| did_all | OFF | paper_only | 327 |
| did_cred | OFF | derived:components | 10 |
| did_cred | OFF | harmonized_rederived | 16 |
| did_cred | OFF | harmonized_unmatched | 74 |
| did_cred | OFF | paper_only | 55 |
| persistence_all | OFF | derived:components | 21 |
| persistence_all | OFF | harmonized_rederived | 26 |
| persistence_all | OFF | harmonized_unmatched | 153 |
| persistence_all | OFF | paper_only | 245 |
| persistence_cred | OFF | derived:components | 20 |
| persistence_cred | OFF | harmonized_rederived | 19 |
| persistence_cred | OFF | harmonized_unmatched | 73 |
| persistence_cred | OFF | paper_only | 46 |
| post_all | OFF | derived:components | 275 |
| post_all | OFF | harmonized_rederived | 125 |
| post_all | OFF | harmonized_unmatched | 1620 |
| post_all | OFF | paper_only | 1274 |
| post_cred | OFF | derived:components | 219 |
| post_cred | OFF | harmonized_rederived | 78 |
| post_cred | OFF | harmonized_unmatched | 785 |
| post_cred | OFF | paper_only | 436 |

## Moderator: data_available vs paper_only

| layer | spec | exclusions | coef | se | lo | hi | p | k | n_studies | data_available_est | data_available_k | paper_only_est | paper_only_k |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| meta | did_all | ON | 0.0400 | 0.0966 | -0.1493 | 0.2292 | 0.6790 | 529 | 101 | 0.4232 | 199 | 0.3824 | 330 |
| meta | did_all | OFF | 0.0057 | 0.0967 | -0.1839 | 0.1953 | 0.9530 | 529 | 101 | 0.3888 | 199 | 0.3824 | 330 |
| meta | did_cred | ON | 0.0131 | 0.1753 | -0.3306 | 0.3567 | 0.9406 | 155 | 50 | 0.5194 | 98 | 0.5057 | 57 |
| meta | did_cred | OFF | -0.0545 | 0.1776 | -0.4026 | 0.2937 | 0.7592 | 155 | 50 | 0.4515 | 98 | 0.5057 | 57 |
| meta | post_all | ON | -0.0645 | 0.0303 | -0.1240 | -0.0051 | 0.0333 | 3294 | 438 | 0.1908 | 1973 | 0.2683 | 1321 |
| meta | post_all | OFF | -0.0763 | 0.0303 | -0.1356 | -0.0169 | 0.0118 | 3294 | 438 | 0.1797 | 1973 | 0.2683 | 1321 |
| meta | post_cred | ON | -0.0804 | 0.0440 | -0.1665 | 0.0058 | 0.0676 | 1518 | 324 | 0.1996 | 1048 | 0.2833 | 470 |
| meta | post_cred | OFF | -0.1000 | 0.0439 | -0.1860 | -0.0140 | 0.0227 | 1518 | 324 | 0.1816 | 1048 | 0.2833 | 470 |
| meta | persistence_all | ON | 0.0792 | 0.0587 | -0.0358 | 0.1942 | 0.1772 | 445 | 83 | 0.2120 | 200 | 0.1607 | 245 |
| meta | persistence_all | OFF | 0.0732 | 0.0590 | -0.0425 | 0.1889 | 0.2148 | 445 | 83 | 0.2059 | 200 | 0.1607 | 245 |
| meta | persistence_cred | ON | 0.1207 | 0.0944 | -0.0644 | 0.3058 | 0.2012 | 158 | 56 | 0.2098 | 112 | 0.1552 | 46 |
| meta | persistence_cred | OFF | 0.1245 | 0.0945 | -0.0606 | 0.3096 | 0.1875 | 158 | 56 | 0.2128 | 112 | 0.1552 | 46 |

## Publication bias

Egger:

| layer | spec | exclusions | k | intercept | se | t | p | slope |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| meta | did_all | ON | 529 | 3.0658 | 0.4272 | 7.1765 | 0.0000 | 0.0283 |
| meta | did_all | OFF | 529 | 2.8715 | 0.4323 | 6.6416 | 0.0000 | 0.0357 |
| meta | did_cred | ON | 155 | 5.6779 | 0.9589 | 5.9215 | 0.0000 | -0.0459 |
| meta | did_cred | OFF | 155 | 4.9962 | 0.9943 | 5.0248 | 0.0000 | -0.0243 |
| meta | post_all | ON | 3294 | 5.4768 | 0.2390 | 22.9149 | 0.0000 | -0.0122 |
| meta | post_all | OFF | 3294 | 4.6948 | 0.2404 | 19.5303 | 0.0000 | -0.0102 |
| meta | post_cred | ON | 1518 | 2.9777 | 0.2151 | 13.8404 | 0.0000 | 0.0097 |
| meta | post_cred | OFF | 1518 | 1.7103 | 0.2172 | 7.8738 | 0.0000 | 0.0145 |
| meta | persistence_all | ON | 445 | -0.0653 | 0.4462 | -0.1463 | 0.8838 | 0.0279 |
| meta | persistence_all | OFF | 445 | -0.0558 | 0.4464 | -0.1250 | 0.9006 | 0.0279 |
| meta | persistence_cred | ON | 158 | -1.2159 | 1.0501 | -1.1579 | 0.2487 | 0.0331 |
| meta | persistence_cred | OFF | 158 | -1.1700 | 1.0541 | -1.1100 | 0.2687 | 0.0328 |

PET-PEESE:

| layer | spec | exclusions | k | pet_int | pet_se | pet_p | pet_slope | peese_int | peese_se | peese_p | peese_slope | prefer |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| meta | did_all | ON | 529 | 0.1560 | 0.0384 | 0.0001 | 1.6166 | 0.2689 | 0.0224 | 0.0000 | 3.7763 | PEESE |
| meta | did_all | OFF | 529 | 0.1477 | 0.0385 | 0.0001 | 1.5884 | 0.2552 | 0.0223 | 0.0000 | 3.9780 | PEESE |
| meta | did_cred | ON | 155 | 0.3791 | 0.0947 | 0.0001 | 1.0324 | 0.4818 | 0.0550 | 0.0000 | 0.4422 | PEESE |
| meta | did_cred | OFF | 155 | 0.3438 | 0.0984 | 0.0006 | 0.9402 | 0.4299 | 0.0567 | 0.0000 | 0.8976 | PEESE |
| meta | post_all | ON | 3294 | 0.0194 | 0.0033 | 0.0000 | 1.9728 | 0.0483 | 0.0030 | 0.0000 | 8.1127 | PEESE |
| meta | post_all | OFF | 3294 | 0.0131 | 0.0034 | 0.0001 | 1.9426 | 0.0409 | 0.0030 | 0.0000 | 8.5990 | PEESE |
| meta | post_cred | ON | 1518 | 0.0316 | 0.0069 | 0.0000 | 1.9225 | 0.0780 | 0.0056 | 0.0000 | 7.2797 | PEESE |
| meta | post_cred | OFF | 1518 | 0.0076 | 0.0074 | 0.3012 | 2.0117 | 0.0542 | 0.0059 | 0.0000 | 8.8691 | PET |
| meta | persistence_all | ON | 445 | 0.0147 | 0.0044 | 0.0008 | 1.5882 | 0.0322 | 0.0042 | 0.0000 | 6.6877 | PEESE |
| meta | persistence_all | OFF | 445 | 0.0151 | 0.0044 | 0.0006 | 1.5605 | 0.0323 | 0.0042 | 0.0000 | 6.5390 | PEESE |
| meta | persistence_cred | ON | 158 | 0.0132 | 0.0054 | 0.0152 | 1.9847 | 0.0270 | 0.0052 | 0.0000 | 10.3236 | PEESE |
| meta | persistence_cred | OFF | 158 | 0.0136 | 0.0053 | 0.0117 | 1.9801 | 0.0274 | 0.0052 | 0.0000 | 9.9385 | PEESE |

Trim-and-fill (L0-style linear rank estimator):

| layer | spec | exclusions | k_obs | k0_missing | side | orig_est | adjusted_est |
| --- | --- | --- | --- | --- | --- | --- | --- |
| meta | did_all | ON | 529 | 10 | left | 0.3990 | 0.3649 |
| meta | did_cred | ON | 155 | 6 | left | 0.5148 | 0.4419 |
| meta | post_all | ON | 3294 | 80 | left | 0.2144 | 0.1917 |
| meta | post_cred | ON | 1518 | 4 | left | 0.2224 | 0.2179 |
| meta | persistence_all | ON | 445 | 0 | left | 0.1736 | 0.1736 |
| meta | persistence_cred | ON | 158 | 0 | left | 0.1761 | 0.1761 |

## Limitations (meta layer)

- **did_cred** is near-empty: credibility judgements are usually measured post-exposure only, so very few contrasts have a pretest + immediate posttest. The spec is reported with its actual k and the sharing_intention fallback.

- **Exclusions OFF** only changes effects that could be confidently matched to a harmonized participant-level variable; all other effects keep the coded value and are flagged `paper_only`/`harmonized_unmatched`.

- The publication-bias tests inherit the mixed effect-metric pool of `metaanalysis.py` (Hedges g, Fisher z, log-odds, raw betas); on `did_all` the derived DiD values can be extreme because some papers report implausibly small pretest SDs. PET-PEESE/VOC per-metric is left as a future refinement.

- Trim-and-fill uses an L0-style linear rank estimator (validated on simulated symmetric/truncated funnels), not a specific software build; k0 is reported as a sensitivity.


## Outputs

- `temp/meta_specs.csv` — tidy per-effect table (spec x exclusions x contrast).
- `temp/meta_did_excluded.csv` — contrasts dropped by the DiD quality guard.
- `analysis_suite.xlsx` — Specs_Summary, Effects, Moderator, Egger, PET_PEESE, TrimFill, LeaveOneOut, Exclusions_Coverage, r_sensitivity, Credibility_Fallbacks.
- `metaanalysis.xlsx` — `Suite_*` sheets (existing sheets untouched).
- `figures/*.png|pdf` — forest / funnel / LOO / moderator / PET-PEESE / exclusions-on-off / four-spec / domains.

## Reproduce
```
python Scripts/analysis_multiverse_meta.py
```

<!-- META-SECTION:END -->

<!-- SDT-SUITE:START -->

## SDT layer — multiverse analysis suite

_Generated by `Scripts/analysis_multiverse_sdt.py`._

### Contrast definition

Arm-based intervention effect on discrimination. A participant's discrimination is the
linear twin of the signal-detection index,
`d_i = mean(oriented DV | veracity=true) + mean(oriented DV | veracity=false) - 1`
`= HR - FAR` within the study x artifact x domain x timepoint cell (polarity rule
applied: `higher_is_worse` -> `1 - x`, so the false-veracity mean is the correct-
rejection rate `CR` and `FAR = 1 - CR`). Arm means `d_arm = mean(d_i)`. Then

* `post = d_IG,post - d_CG,post`
* `did  = (d_IG,post - d_IG,pre) - (d_CG,post - d_CG,pre)`, change scores on
  participants matched by `pid` (so the empirical within-person pre-post correlation
  enters the variance; the value is recorded per contrast as `r_used`).

Effects are standardised by the pooled SD of the analysis sample (`sd_pool` of the
participant-level `d_i` for post, of the participant-level change for did). This is
deliberately different from `reanalysis.py`'s pooled all-arm discrimination, which is
a discrimination level rather than a treatment effect.

The 2026-09-17 decision was arm-based (Definition A); the earlier `PUBBIAS-GRAPHICS`
section's SDT rows used a pooled-over-arms `post` and should be regenerated from
`temp/sdt_specs.csv`, which this script now writes in the shared `suite_common`
schema.

### Arms and coverage

Arms are read first from the study's explicit `Datasets/<Key>/<Key> - conditions.csv`
mapping (columns `code, role, label, evidence, artifact`; role in
control|active_control|intervention|other). That mapping is authoritative and never
flagged: `control`/`active_control` form the comparison arm, `intervention` the
treatment arm, `other`/unmapped codes are dropped. Only when a study has no mapping
does the legacy label heuristic run (explicit control labels containing
control/ctrl/kontrol/placebo/baseline/no_*/assessment_only/...; a secondary set
`misinformation`, `misinformation_only`, `2048game`); a purely numeric two-level
`0/1` set then uses the documented 0=control/1=intervention convention, and every
such contrast carries `cond_ambiguous = 1`. Single-arm designs are skipped and
listed in `temp/sdt_skips.csv`.

Studies/artifacts skipped: **279** rows in `temp/sdt_skips.csv`.

| reason | rows |
|---|---:|
| no true/false veracity DVs | 121 |
| no identifiable control arm | 84 |
| no participant with both veracities | 26 |
| no condition column | 13 |
| 25 rows with unassignable condition dropped | 3 |
| error: KeyError: 'artifact' | 2 |
| non-independent experiment | 2 |
| 217 rows with unassignable condition dropped | 2 |
| 1 rows with unassignable condition dropped | 2 |
| 81 rows with unassignable condition dropped | 1 |
| 1096 rows with unassignable condition dropped | 1 |
| 186 rows with unassignable condition dropped | 1 |
| 3 rows with unassignable condition dropped | 1 |
| 132 rows with unassignable condition dropped | 1 |
| 117 rows with unassignable condition dropped | 1 |
| 214 rows with unassignable condition dropped | 1 |
| 113 rows with unassignable condition dropped | 1 |
| empty after exclusions | 1 |
| 1968 rows with unassignable condition dropped | 1 |
| 22 rows with unassignable condition dropped | 1 |
| 525 rows with unassignable condition dropped | 1 |
| 1812 confidence/metacognition rows routed out | 1 |
| 2235 confidence/metacognition rows routed out | 1 |
| 12 rows with unassignable condition dropped | 1 |
| 240 rows with unassignable condition dropped | 1 |
| 55 rows with unassignable condition dropped | 1 |
| 38 rows with unassignable condition dropped | 1 |
| 43 rows with unassignable condition dropped | 1 |
| 65 rows with unassignable condition dropped | 1 |
| 52 rows with unassignable condition dropped | 1 |
| 31 rows with unassignable condition dropped | 1 |
| 194 rows with unassignable condition dropped | 1 |
| 39120 rows with unassignable condition dropped | 1 |

### Contrasts per specification (exclusions ON)

| spec | contrasts | studies | median r_used | fallbacks |
|---|---:|---:|---:|---:|
| post_all | 249 | 115 | – | 0 |
| post_cred | 176 | 110 | – | 40 |
| persistence_all | 43 | 23 | – | 0 |
| persistence_cred | 30 | 23 | – | 2 |
| did_all | 49 | 29 | 0.422 | 0 |
| did_cred | 36 | 28 | 0.445 | 4 |

### Pooled results (two-level RE, exclusions ON vs OFF)

| spec | excl | group | k | k studies | est | 95% CI | tau2_between |
|---|---|---|---:|---:|---:|---|---:|
| post_all | ON | ALL | 249 | 115 | 0.145 | [0.102, 0.187] | 0.0459 |
| post_all | ON | domain: credibility | 136 | 91 | 0.128 | [0.067, 0.188] | 0.0773 |
| post_all | ON | domain: sharing_intention | 84 | 53 | 0.156 | [0.109, 0.203] | 0.0231 |
| post_all | ON | domain: technique_recognition | 29 | 15 | 0.190 | [0.096, 0.285] | 0.0296 |
| post_all | OFF | ALL | 252 | 116 | 0.141 | [0.099, 0.184] | 0.0450 |
| post_all | OFF | domain: credibility | 137 | 91 | 0.123 | [0.063, 0.182] | 0.0749 |
| post_all | OFF | domain: sharing_intention | 85 | 53 | 0.152 | [0.105, 0.199] | 0.0233 |
| post_all | OFF | domain: technique_recognition | 30 | 16 | 0.239 | [0.080, 0.398] | 0.0948 |
| post_cred | ON | ALL | 176 | 110 | 0.149 | [0.096, 0.202] | 0.0710 |
| post_cred | ON | domain: credibility | 136 | 91 | 0.128 | [0.067, 0.188] | 0.0773 |
| post_cred | ON | domain: sharing_intention | 40 | 22 | 0.256 | [0.175, 0.338] | 0.0313 |
| post_cred | OFF | ALL | 177 | 110 | 0.144 | [0.092, 0.197] | 0.0688 |
| post_cred | OFF | domain: credibility | 137 | 91 | 0.123 | [0.063, 0.182] | 0.0749 |
| post_cred | OFF | domain: sharing_intention | 40 | 22 | 0.254 | [0.172, 0.335] | 0.0312 |
| persistence_all | ON | ALL | 43 | 23 | 0.181 | [0.080, 0.281] | 0.0518 |
| persistence_all | ON | domain: credibility | 28 | 22 | 0.179 | [0.073, 0.286] | 0.0542 |
| persistence_all | ON | domain: sharing_intention | 9 | 7 | 0.100 | [0.011, 0.188] | 0.0055 |
| persistence_all | ON | domain: technique_recognition | 6 | 3 | 0.067 | [-0.211, 0.345] | 0.0556 |
| persistence_all | OFF | ALL | 43 | 23 | 0.178 | [0.078, 0.278] | 0.0515 |
| persistence_all | OFF | domain: credibility | 28 | 22 | 0.177 | [0.072, 0.283] | 0.0536 |
| persistence_all | OFF | domain: sharing_intention | 9 | 7 | 0.095 | [0.003, 0.187] | 0.0066 |
| persistence_all | OFF | domain: technique_recognition | 6 | 3 | 0.059 | [-0.213, 0.331] | 0.0524 |
| persistence_cred | ON | ALL | 30 | 23 | 0.185 | [0.083, 0.288] | 0.0524 |
| persistence_cred | ON | domain: credibility | 28 | 22 | 0.179 | [0.073, 0.286] | 0.0542 |
| persistence_cred | ON | domain: sharing_intention | 2 | 2 | 0.165 | [0.053, 0.277] | 0.0000 |
| persistence_cred | OFF | ALL | 30 | 23 | 0.183 | [0.081, 0.285] | 0.0519 |
| persistence_cred | OFF | domain: credibility | 28 | 22 | 0.177 | [0.072, 0.283] | 0.0536 |
| persistence_cred | OFF | domain: sharing_intention | 2 | 2 | 0.165 | [0.053, 0.277] | 0.0000 |
| did_all | ON | ALL | 49 | 29 | 0.121 | [0.049, 0.194] | 0.0296 |
| did_all | ON | domain: credibility | 32 | 24 | 0.102 | [0.006, 0.199] | 0.0449 |
| did_all | ON | domain: sharing_intention | 15 | 13 | 0.109 | [0.027, 0.191] | 0.0121 |
| did_all | ON | domain: technique_recognition | 2 | 2 | 0.325 | [-0.182, 0.832] | 0.1255 |
| did_all | OFF | ALL | 51 | 29 | 0.114 | [0.039, 0.189] | 0.0313 |
| did_all | OFF | domain: credibility | 33 | 24 | 0.097 | [-0.003, 0.197] | 0.0477 |
| did_all | OFF | domain: sharing_intention | 16 | 13 | 0.097 | [0.013, 0.182] | 0.0118 |
| did_all | OFF | domain: technique_recognition | 2 | 2 | 0.325 | [-0.182, 0.832] | 0.1255 |
| did_cred | ON | ALL | 36 | 28 | 0.108 | [0.030, 0.186] | 0.0324 |
| did_cred | ON | domain: credibility | 32 | 24 | 0.102 | [0.006, 0.199] | 0.0449 |
| did_cred | ON | domain: sharing_intention | 4 | 4 | 0.164 | [0.004, 0.325] | 0.0200 |
| did_cred | OFF | ALL | 37 | 28 | 0.102 | [0.022, 0.181] | 0.0333 |
| did_cred | OFF | domain: credibility | 33 | 24 | 0.097 | [-0.003, 0.197] | 0.0477 |
| did_cred | OFF | domain: sharing_intention | 4 | 4 | 0.156 | [0.000, 0.313] | 0.0190 |

### Publication bias (study-level, exclusions ON)

| spec | method | est | 95% CI | p | added k |
|---|---|---:|---|---:|---:|
| post_all | egger | 0.557 | [-1.187, 2.301] | 0.532 |  |
| post_all | pet | 0.118 | [0.043, 0.192] | 0.002 |  |
| post_all | peese | 0.126 | [0.081, 0.170] | 0.000 |  |
| post_all | trimfill | 0.145 | [0.102, 0.187] |  | 0 |
| post_cred | egger | 3.150 | [0.573, 5.727] | 0.017 |  |
| post_cred | pet | 0.047 | [-0.050, 0.144] | 0.345 |  |
| post_cred | peese | 0.099 | [0.042, 0.156] | 0.001 |  |
| post_cred | trimfill | 0.046 | [-0.015, 0.107] |  | 22 |
| persistence_all | egger | 2.334 | [-3.912, 8.579] | 0.464 |  |
| persistence_all | pet | 0.025 | [-0.144, 0.195] | 0.768 |  |
| persistence_all | peese | 0.088 | [-0.014, 0.189] | 0.092 |  |
| persistence_all | trimfill | 0.012 | [-0.100, 0.125] |  | 10 |
| persistence_cred | egger | -0.680 | [-6.199, 4.839] | 0.809 |  |
| persistence_cred | pet | 0.145 | [-0.029, 0.318] | 0.102 |  |
| persistence_cred | peese | 0.155 | [0.048, 0.261] | 0.004 |  |
| persistence_cred | trimfill | 0.185 | [0.083, 0.288] |  | 0 |
| did_all | egger | 0.741 | [-1.350, 2.832] | 0.487 |  |
| did_all | pet | 0.017 | [-0.101, 0.135] | 0.776 |  |
| did_all | peese | 0.044 | [-0.029, 0.117] | 0.239 |  |
| did_all | trimfill | 0.064 | [-0.012, 0.139] |  | 4 |
| did_cred | egger | 0.613 | [-1.300, 2.525] | 0.530 |  |
| did_cred | pet | 0.013 | [-0.112, 0.138] | 0.839 |  |
| did_cred | peese | 0.036 | [-0.043, 0.114] | 0.370 |  |
| did_cred | trimfill | 0.049 | [-0.032, 0.131] |  | 4 |

**Small-study caveat (SDT).** Discrimination is variance-dependent by construction
(it subtracts two condition means), so funnel asymmetry / Egger intercepts are
expected to be partly mechanical; PET-PEESE and trim-and-fill are reported as
sensitivity checks, not as clean tests of publication bias.

### Sensitivity and omitted methods

* **Cluster-robust SE (study clusters)** is reported in `Suite_SDT_Results`
  (`method = cluster_robust`) alongside the two-level estimate. It is wider because
  contrasts within a study share a sample; for `post_all` the cluster-robust CI
  includes 0 while the two-level CI does not, so the study-level evidence is weaker
  than the nominal contrast-level precision suggests.
* **Leave-one-study-out** is in `Suite_SDT_LOO`; under exclusions ON no omitted
  study's LOO CI excluded the full-sample estimate for any spec (no single
  influential study), consistent with the wide between-study heterogeneity.
* **Pre-post r**: the SDT DiD uses each study's actual within-person correlation,
  recorded per contrast (`r_used`; medians in the table above). The r = 0.30 / 0.70
  sensitivity is not applicable to this layer (it is a meta-layer device for studies
  without raw data).
* **p-curve / p-uniform** (optional in the spec) is skipped: the discrimination index
  is a variance-dependent composite and only a subset of contrasts has a meaningful
  p-value; it is not reported as a bias test here.
* **Immediate post vs follow-up**: the three questions are separate and are never
  pooled. `post_*` uses the immediate posttest (follow-ups excluded); `persistence_*`
  uses follow-up only; `did_*` needs `pre` and `post`. Pretest rows are used only as
  the DiD baseline and are never outcomes.

**Moderator.** `data_available` vs `paper_only` is NOT estimated in the SDT layer:
every SDT contrast necessarily comes from a study with usable participant data, so
the subgroup contrast is degenerate here. By the 2026-09-17 decision the moderator
belongs to the reported-effects / paper layer only.

### Fallback notes (credibility -> sharing_intention)

- Appel et al. (2025) / Study2 / post_cred: sharing_intention fallback (no credibility DV)
- Appel et al. (2025) / Study3 / post_cred: sharing_intention fallback (no credibility DV)
- Appel et al. (2025) / main / post_cred: sharing_intention fallback (no credibility DV)
- Basol et al. (2021) / study2 / post_cred: sharing_intention fallback (no credibility DV)
- Basol et al. (2021) / study2 / persistence_cred: sharing_intention fallback (no credibility DV)
- Basol et al. (2021) / study2 / did_cred: sharing_intention fallback (no credibility DV)
- Biddlestone et al. (2025 a) / study1 / post_cred: sharing_intention fallback (no credibility DV)
- Biddlestone et al. (2025 a) / study2 / post_cred: sharing_intention fallback (no credibility DV)
- Capewell et al. (2024) / study2 / post_cred: sharing_intention fallback (no credibility DV)
- Capewell et al. (2024) / study2 / persistence_cred: sharing_intention fallback (no credibility DV)
- Capraro & Celadin (2023) / Experiment1 / post_cred: sharing_intention fallback (no credibility DV)
- Capraro & Celadin (2023) / Experiment2 / post_cred: sharing_intention fallback (no credibility DV)
- Capraro & Celadin (2023) / Experiment3 / post_cred: sharing_intention fallback (no credibility DV)
- Capraro & Celadin (2023) / Experiment4 / post_cred: sharing_intention fallback (no credibility DV)
- Fazio (2020) / main / post_cred: sharing_intention fallback (no credibility DV)
- Fendt & Müller (2026) / main / post_cred: sharing_intention fallback (no credibility DV)
- Fendt & Müller (2026) / main / did_cred: sharing_intention fallback (no credibility DV)
- Fujimoto et al. (2026) / main / post_cred: sharing_intention fallback (no credibility DV)
- Globig et al. (2023) / main / post_cred: sharing_intention fallback (no credibility DV)
- Gollwitzer et al. (2026) / main / post_cred: sharing_intention fallback (no credibility DV)
- Gollwitzer et al. (2026) / main / did_cred: sharing_intention fallback (no credibility DV)
- Hirshleifer et al. (2026) / main / post_cred: sharing_intention fallback (no credibility DV)
- Howe et al. (2024) / main / post_cred: sharing_intention fallback (no credibility DV)
- Marie et al. (2023) / Exp10 / post_cred: sharing_intention fallback (no credibility DV)
- Martel & Rand (2024) / 11 / post_cred: sharing_intention fallback (no credibility DV)
- Martel & Rand (2024) / 12 / post_cred: sharing_intention fallback (no credibility DV)
- Martel & Rand (2024) / 13 / post_cred: sharing_intention fallback (no credibility DV)
- Martel & Rand (2024) / 14 / post_cred: sharing_intention fallback (no credibility DV)
- Martel & Rand (2024) / 15 / post_cred: sharing_intention fallback (no credibility DV)
- Martel & Rand (2024) / 16 / post_cred: sharing_intention fallback (no credibility DV)
- Martel & Rand (2024) / 17 / post_cred: sharing_intention fallback (no credibility DV)
- Martel & Rand (2024) / 18 / post_cred: sharing_intention fallback (no credibility DV)
- Martel & Rand (2024) / 19 / post_cred: sharing_intention fallback (no credibility DV)
- Martel & Rand (2024) / 20 / post_cred: sharing_intention fallback (no credibility DV)
- Martel & Rand (2024) / 21 / post_cred: sharing_intention fallback (no credibility DV)
- McPhedran et al. (2023) / main / post_cred: sharing_intention fallback (no credibility DV)
- Offer-Westort et al. (2024) / 2 / post_cred: sharing_intention fallback (no credibility DV)
- Offer-Westort et al. (2024) / 2 / did_cred: sharing_intention fallback (no credibility DV)
- Pennycook et al. (2020 a) / 2 / post_cred: sharing_intention fallback (no credibility DV)
- Pennycook et al. (2020) / main / post_cred: sharing_intention fallback (no credibility DV)
- Pennycook et al. (2021) / Study3 / post_cred: sharing_intention fallback (no credibility DV)
- Pennycook et al. (2021) / Study4 / post_cred: sharing_intention fallback (no credibility DV)
- Pennycook et al. (2021) / Study5 / post_cred: sharing_intention fallback (no credibility DV)
- Pillai & Fazio (2023) / main / post_cred: sharing_intention fallback (no credibility DV)
- Rentschler & Samad (2025) / main / post_cred: sharing_intention fallback (no credibility DV)
- Roozenbeek et al. (2021) / Stage2 / post_cred: sharing_intention fallback (no credibility DV)

Total fallback contrasts: 46 (recorded per contrast in `temp/sdt_specs.csv` via `fallback = sharing_intention`).

### Outputs

* `temp/sdt_specs.csv` — contrast-level table in the shared `suite_common`
  vocabulary (`layer`, `effect`/`var`, `on`/`off`) plus the audit columns
  (`d_ig`, `d_cg`, `sd_pool`, `r_used`, `fallback`).
* `temp/sdt_specs_multiverse.csv` — the same rows in the native SDT vocabulary.
* `reanalysis.xlsx` — sheets `Suite_SDT_Contrasts`, `Suite_SDT_Results`,
  `Suite_SDT_PubBias`, `Suite_SDT_LOO` (existing sheets untouched).
* `figures/forest_sdt_*`, `funnel_sdt_*`, `loo_sdt_*`, `petpeese_sdt_*`,
  `excl_onoff_*`, `forest_sdt_specs`, `domain_forest_sdt` (PNG 300 dpi + PDF).
* `temp/sdt_skips.csv` — studies/artifacts not analysable and why.

<!-- SDT-SUITE:END -->
