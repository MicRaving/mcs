# ANALYSIS SUITE NOTE

<!-- META-SECTION:START -->
## Meta (reported-effects) layer

_Generated 2026-09-23 20:29 by `Scripts/analysis_multiverse_meta.py`._

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

- **DiD quality guard**: 1 contrasts with |standardised DiD| > 3.0 were dropped (coded SDs behaving like SEs) and listed in `temp/meta_did_excluded.csv`.

## Effects per spec (meta layer)

| spec | exclusions | scope | k | est | lo | hi | i2 | two_level_est | two_level_lo | two_level_hi | cr_se | cr_lo | cr_hi |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| did_all | ON | unrestricted | 355 | 0.4452 | 0.3960 | 0.4944 | 94.3614 | 0.4757 | 0.3654 | 0.5860 | 0.0676 | 0.3127 | 0.5777 |
| did_all | ON | restricted | 196 | 0.4328 | 0.3641 | 0.5015 | 95.0529 | 0.4324 | 0.3120 | 0.5529 | 0.0655 | 0.3045 | 0.5611 |
| did_all | OFF | unrestricted | 355 | 0.4333 | 0.3847 | 0.4819 | 94.2900 | 0.4652 | 0.3559 | 0.5746 | 0.0665 | 0.3029 | 0.5637 |
| did_all | OFF | restricted | 196 | 0.4151 | 0.3478 | 0.4823 | 94.9321 | 0.4189 | 0.3011 | 0.5368 | 0.0633 | 0.2911 | 0.5391 |
| did_cred | ON | unrestricted | 180 | 0.4498 | 0.3741 | 0.5255 | 95.2074 | 0.4406 | 0.3147 | 0.5665 | 0.0687 | 0.3151 | 0.5845 |
| did_cred | ON | restricted | 180 | 0.4498 | 0.3741 | 0.5255 | 95.2074 | 0.4406 | 0.3147 | 0.5665 | 0.0687 | 0.3151 | 0.5845 |
| did_cred | OFF | unrestricted | 180 | 0.4304 | 0.3563 | 0.5045 | 95.0989 | 0.4265 | 0.3033 | 0.5497 | 0.0665 | 0.3001 | 0.5607 |
| did_cred | OFF | restricted | 180 | 0.4304 | 0.3563 | 0.5045 | 95.0989 | 0.4265 | 0.3033 | 0.5497 | 0.0665 | 0.3001 | 0.5607 |
| post_all | ON | unrestricted | 3088 | 0.2073 | 0.2000 | 0.2147 | 94.7648 | 0.2848 | 0.2552 | 0.3144 | 0.0155 | 0.1769 | 0.2377 |
| post_all | ON | restricted | 1959 | 0.2060 | 0.1979 | 0.2141 | 94.5757 | 0.2813 | 0.2465 | 0.3162 | 0.0189 | 0.1689 | 0.2431 |
| post_all | OFF | unrestricted | 3088 | 0.2020 | 0.1945 | 0.2094 | 94.8718 | 0.2813 | 0.2516 | 0.3111 | 0.0155 | 0.1715 | 0.2324 |
| post_all | OFF | restricted | 1959 | 0.1975 | 0.1893 | 0.2057 | 94.7027 | 0.2774 | 0.2424 | 0.3124 | 0.0191 | 0.1600 | 0.2350 |
| post_cred | ON | unrestricted | 1606 | 0.2264 | 0.2135 | 0.2393 | 94.1054 | 0.2832 | 0.2448 | 0.3215 | 0.0229 | 0.1815 | 0.2713 |
| post_cred | ON | restricted | 1606 | 0.2264 | 0.2135 | 0.2393 | 94.1054 | 0.2832 | 0.2448 | 0.3215 | 0.0229 | 0.1815 | 0.2713 |
| post_cred | OFF | unrestricted | 1606 | 0.2178 | 0.2042 | 0.2313 | 94.5961 | 0.2778 | 0.2391 | 0.3165 | 0.0231 | 0.1724 | 0.2631 |
| post_cred | OFF | restricted | 1606 | 0.2178 | 0.2042 | 0.2313 | 94.5961 | 0.2778 | 0.2391 | 0.3165 | 0.0231 | 0.1724 | 0.2631 |
| persistence_all | ON | unrestricted | 489 | 0.2203 | 0.2015 | 0.2392 | 87.9336 | 0.2238 | 0.1655 | 0.2821 | 0.0298 | 0.1620 | 0.2787 |
| persistence_all | ON | restricted | 303 | 0.2437 | 0.2205 | 0.2670 | 90.0082 | 0.2348 | 0.1643 | 0.3052 | 0.0382 | 0.1688 | 0.3187 |
| persistence_all | OFF | unrestricted | 489 | 0.2186 | 0.1998 | 0.2375 | 87.9247 | 0.2204 | 0.1620 | 0.2788 | 0.0297 | 0.1604 | 0.2768 |
| persistence_all | OFF | restricted | 303 | 0.2418 | 0.2186 | 0.2650 | 89.9645 | 0.2312 | 0.1609 | 0.3016 | 0.0381 | 0.1671 | 0.3165 |
| persistence_cred | ON | unrestricted | 256 | 0.2519 | 0.2264 | 0.2773 | 91.0699 | 0.2475 | 0.1693 | 0.3257 | 0.0451 | 0.1635 | 0.3402 |
| persistence_cred | ON | restricted | 256 | 0.2519 | 0.2264 | 0.2773 | 91.0699 | 0.2475 | 0.1693 | 0.3257 | 0.0451 | 0.1635 | 0.3402 |
| persistence_cred | OFF | unrestricted | 256 | 0.2495 | 0.2241 | 0.2749 | 91.0285 | 0.2437 | 0.1656 | 0.3218 | 0.0449 | 0.1616 | 0.3375 |
| persistence_cred | OFF | restricted | 256 | 0.2495 | 0.2241 | 0.2749 | 91.0285 | 0.2437 | 0.1656 | 0.3218 | 0.0449 | 0.1616 | 0.3375 |

## Credibility fallbacks (sharing_intention used where no credibility contrast)

Fallback contrasts: **1830** across 53 studies.

## Exclusions ON vs OFF

- Harmonized re-derivations (no exclusion filters): **283** contrast-runs.
- `paper_only`: no usable data, coded effect retained (author sample embedded).
- `harmonized_unmatched`: data exist but no confident DV match, coded effect retained.

| spec | exclusions | category | n |
| --- | --- | --- | --- |
| did_all | OFF | derived:components | 10 |
| did_all | OFF | harmonized_rederived | 20 |
| did_all | OFF | harmonized_unmatched | 115 |
| did_all | OFF | paper_only | 210 |
| did_cred | OFF | derived:components | 9 |
| did_cred | OFF | harmonized_rederived | 15 |
| did_cred | OFF | harmonized_unmatched | 67 |
| did_cred | OFF | paper_only | 89 |
| persistence_all | OFF | derived:components | 21 |
| persistence_all | OFF | harmonized_rederived | 35 |
| persistence_all | OFF | harmonized_unmatched | 168 |
| persistence_all | OFF | paper_only | 265 |
| persistence_cred | OFF | derived:components | 21 |
| persistence_cred | OFF | harmonized_rederived | 26 |
| persistence_cred | OFF | harmonized_unmatched | 102 |
| persistence_cred | OFF | paper_only | 107 |
| post_all | OFF | derived:components | 232 |
| post_all | OFF | harmonized_rederived | 111 |
| post_all | OFF | harmonized_unmatched | 1175 |
| post_all | OFF | paper_only | 1570 |
| post_cred | OFF | derived:components | 185 |
| post_cred | OFF | harmonized_rederived | 76 |
| post_cred | OFF | harmonized_unmatched | 578 |
| post_cred | OFF | paper_only | 767 |

## Moderator: data_available vs paper_only

| layer | spec | exclusions | coef | se | lo | hi | p | k | n_studies | data_available_est | data_available_k | paper_only_est | paper_only_k |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| meta | did_all | ON | 0.0214 | 0.1240 | -0.2217 | 0.2645 | 0.8629 | 355 | 70 | 0.4545 | 139 | 0.4403 | 216 |
| meta | did_all | OFF | -0.0068 | 0.1220 | -0.2459 | 0.2322 | 0.9553 | 355 | 70 | 0.4261 | 139 | 0.4403 | 216 |
| meta | did_cred | ON | 0.0547 | 0.1351 | -0.2100 | 0.3195 | 0.6854 | 180 | 52 | 0.4774 | 86 | 0.4245 | 94 |
| meta | did_cred | OFF | 0.0157 | 0.1322 | -0.2434 | 0.2749 | 0.9054 | 180 | 52 | 0.4392 | 86 | 0.4245 | 94 |
| meta | post_all | ON | -0.0276 | 0.0310 | -0.0883 | 0.0330 | 0.3719 | 3088 | 385 | 0.1987 | 1491 | 0.2194 | 1597 |
| meta | post_all | OFF | -0.0385 | 0.0310 | -0.0993 | 0.0223 | 0.2141 | 3088 | 385 | 0.1895 | 1491 | 0.2194 | 1597 |
| meta | post_cred | ON | -0.0423 | 0.0454 | -0.1313 | 0.0466 | 0.3507 | 1606 | 323 | 0.2035 | 821 | 0.2531 | 785 |
| meta | post_cred | OFF | -0.0606 | 0.0457 | -0.1502 | 0.0291 | 0.1855 | 1606 | 323 | 0.1868 | 821 | 0.2531 | 785 |
| meta | persistence_all | ON | 0.0767 | 0.0584 | -0.0378 | 0.1912 | 0.1894 | 489 | 89 | 0.2671 | 224 | 0.1757 | 265 |
| meta | persistence_all | OFF | 0.0728 | 0.0584 | -0.0416 | 0.1872 | 0.2123 | 489 | 89 | 0.2631 | 224 | 0.1757 | 265 |
| meta | persistence_cred | ON | 0.1549 | 0.0762 | 0.0055 | 0.3042 | 0.0421 | 256 | 72 | 0.3259 | 149 | 0.1395 | 107 |
| meta | persistence_cred | OFF | 0.1510 | 0.0761 | 0.0018 | 0.3003 | 0.0473 | 256 | 72 | 0.3212 | 149 | 0.1395 | 107 |

## Publication bias

Egger:

| layer | spec | exclusions | k | intercept | se | t | p | slope |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| meta | did_all | ON | 355 | 3.9963 | 0.4559 | 8.7659 | 0.0000 | -0.0587 |
| meta | did_all | OFF | 355 | 3.9276 | 0.4554 | 8.6254 | 0.0000 | -0.0574 |
| meta | did_cred | ON | 180 | 5.8142 | 0.8773 | 6.6276 | 0.0000 | -0.1822 |
| meta | did_cred | OFF | 180 | 5.7202 | 0.8760 | 6.5297 | 0.0000 | -0.1789 |
| meta | post_all | ON | 3088 | 5.7357 | 0.2599 | 22.0664 | 0.0000 | -0.0130 |
| meta | post_all | OFF | 3088 | 4.8407 | 0.2610 | 18.5468 | 0.0000 | -0.0108 |
| meta | post_cred | ON | 1606 | 3.2573 | 0.1752 | 18.5969 | 0.0000 | 0.0008 |
| meta | post_cred | OFF | 1606 | 1.7044 | 0.1684 | 10.1192 | 0.0000 | 0.0043 |
| meta | persistence_all | ON | 489 | 2.2057 | 0.2459 | 8.9709 | 0.0000 | 0.0184 |
| meta | persistence_all | OFF | 489 | 2.2000 | 0.2456 | 8.9557 | 0.0000 | 0.0185 |
| meta | persistence_cred | ON | 256 | 2.5682 | 0.4210 | 6.1005 | 0.0000 | 0.0149 |
| meta | persistence_cred | OFF | 256 | 2.5729 | 0.4210 | 6.1116 | 0.0000 | 0.0149 |

PET-PEESE:

| layer | spec | exclusions | k | pet_int | pet_se | pet_p | pet_slope | peese_int | peese_se | peese_p | peese_slope | prefer |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| meta | did_all | ON | 355 | 0.0406 | 0.0490 | 0.4072 | 2.8536 | 0.2263 | 0.0295 | 0.0000 | 7.4292 | PET |
| meta | did_all | OFF | 355 | 0.0413 | 0.0488 | 0.3982 | 2.7910 | 0.2231 | 0.0292 | 0.0000 | 7.2467 | PET |
| meta | did_cred | ON | 180 | 0.1163 | 0.0835 | 0.1651 | 2.4518 | 0.3055 | 0.0463 | 0.0000 | 4.3930 | PET |
| meta | did_cred | OFF | 180 | 0.1237 | 0.0829 | 0.1374 | 2.2967 | 0.3014 | 0.0453 | 0.0000 | 4.0073 | PET |
| meta | post_all | ON | 3088 | 0.0199 | 0.0034 | 0.0000 | 1.9468 | 0.0472 | 0.0031 | 0.0000 | 8.2795 | PEESE |
| meta | post_all | OFF | 3088 | 0.0132 | 0.0035 | 0.0002 | 1.9322 | 0.0399 | 0.0032 | 0.0000 | 8.7471 | PEESE |
| meta | post_cred | ON | 1606 | 0.0365 | 0.0081 | 0.0000 | 1.8720 | 0.0901 | 0.0063 | 0.0000 | 7.2456 | PEESE |
| meta | post_cred | OFF | 1606 | -0.0020 | 0.0087 | 0.8205 | 2.1489 | 0.0596 | 0.0067 | 0.0000 | 8.9733 | PET |
| meta | persistence_all | ON | 489 | 0.0300 | 0.0101 | 0.0031 | 1.6733 | 0.0756 | 0.0084 | 0.0000 | 6.5814 | PEESE |
| meta | persistence_all | OFF | 489 | 0.0302 | 0.0101 | 0.0029 | 1.6612 | 0.0757 | 0.0084 | 0.0000 | 6.5105 | PEESE |
| meta | persistence_cred | ON | 256 | 0.0212 | 0.0120 | 0.0787 | 2.0708 | 0.0601 | 0.0104 | 0.0000 | 10.5524 | PET |
| meta | persistence_cred | OFF | 256 | 0.0219 | 0.0120 | 0.0702 | 2.0468 | 0.0606 | 0.0104 | 0.0000 | 10.3149 | PET |

Trim-and-fill (L0-style linear rank estimator):

| layer | spec | exclusions | k_obs | k0_missing | side | orig_est | adjusted_est |
| --- | --- | --- | --- | --- | --- | --- | --- |
| meta | did_all | ON | 355 | 8 | left | 0.4452 | 0.4041 |
| meta | did_cred | ON | 180 | 7 | left | 0.4498 | 0.3893 |
| meta | post_all | ON | 3088 | 74 | left | 0.2073 | 0.1837 |
| meta | post_cred | ON | 1606 | 0 | left | 0.2264 | 0.2264 |
| meta | persistence_all | ON | 489 | 0 | left | 0.2203 | 0.2203 |
| meta | persistence_cred | ON | 256 | 0 | left | 0.2519 | 0.2519 |

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

Studies/artifacts skipped: **258** rows in `temp/sdt_skips.csv`.

| reason | rows |
|---|---:|
| no true/false veracity DVs | 112 |
| no identifiable control arm | 83 |
| no participant with both veracities | 20 |
| no condition column | 11 |
| 25 rows with unassignable condition dropped | 3 |
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
| post_all | 240 | 111 | – | 0 |
| post_cred | 171 | 106 | – | 38 |
| persistence_all | 43 | 23 | – | 0 |
| persistence_cred | 30 | 23 | – | 2 |
| did_all | 47 | 28 | 0.441 | 0 |
| did_cred | 35 | 27 | 0.448 | 4 |

### Pooled results (two-level RE, exclusions ON vs OFF)

| spec | excl | group | k | k studies | est | 95% CI | tau2_between |
|---|---|---|---:|---:|---:|---|---:|
| post_all | ON | ALL | 240 | 111 | 0.141 | [0.099, 0.184] | 0.0442 |
| post_all | ON | domain: credibility | 133 | 88 | 0.125 | [0.063, 0.187] | 0.0796 |
| post_all | ON | domain: sharing_intention | 80 | 50 | 0.158 | [0.109, 0.206] | 0.0237 |
| post_all | ON | domain: technique_recognition | 27 | 14 | 0.173 | [0.081, 0.266] | 0.0256 |
| post_all | OFF | ALL | 242 | 111 | 0.137 | [0.095, 0.179] | 0.0434 |
| post_all | OFF | domain: credibility | 134 | 88 | 0.119 | [0.058, 0.181] | 0.0771 |
| post_all | OFF | domain: sharing_intention | 81 | 50 | 0.154 | [0.105, 0.203] | 0.0239 |
| post_all | OFF | domain: technique_recognition | 27 | 14 | 0.218 | [0.032, 0.403] | 0.1198 |
| post_cred | ON | ALL | 171 | 106 | 0.149 | [0.094, 0.203] | 0.0736 |
| post_cred | ON | domain: credibility | 133 | 88 | 0.125 | [0.063, 0.187] | 0.0796 |
| post_cred | ON | domain: sharing_intention | 38 | 21 | 0.269 | [0.184, 0.353] | 0.0318 |
| post_cred | OFF | ALL | 172 | 106 | 0.143 | [0.090, 0.197] | 0.0713 |
| post_cred | OFF | domain: credibility | 134 | 88 | 0.119 | [0.058, 0.181] | 0.0771 |
| post_cred | OFF | domain: sharing_intention | 38 | 21 | 0.266 | [0.182, 0.350] | 0.0317 |
| persistence_all | ON | ALL | 43 | 23 | 0.182 | [0.082, 0.283] | 0.0519 |
| persistence_all | ON | domain: credibility | 28 | 22 | 0.181 | [0.075, 0.287] | 0.0542 |
| persistence_all | ON | domain: sharing_intention | 9 | 7 | 0.100 | [0.011, 0.188] | 0.0055 |
| persistence_all | ON | domain: technique_recognition | 6 | 3 | 0.068 | [-0.210, 0.346] | 0.0559 |
| persistence_all | OFF | ALL | 43 | 23 | 0.180 | [0.080, 0.280] | 0.0515 |
| persistence_all | OFF | domain: credibility | 28 | 22 | 0.179 | [0.073, 0.285] | 0.0536 |
| persistence_all | OFF | domain: sharing_intention | 9 | 7 | 0.095 | [0.003, 0.187] | 0.0066 |
| persistence_all | OFF | domain: technique_recognition | 6 | 3 | 0.071 | [-0.210, 0.352] | 0.0574 |
| persistence_cred | ON | ALL | 30 | 23 | 0.187 | [0.084, 0.290] | 0.0524 |
| persistence_cred | ON | domain: credibility | 28 | 22 | 0.181 | [0.075, 0.287] | 0.0542 |
| persistence_cred | ON | domain: sharing_intention | 2 | 2 | 0.165 | [0.053, 0.277] | 0.0000 |
| persistence_cred | OFF | ALL | 30 | 23 | 0.184 | [0.082, 0.287] | 0.0519 |
| persistence_cred | OFF | domain: credibility | 28 | 22 | 0.179 | [0.073, 0.285] | 0.0536 |
| persistence_cred | OFF | domain: sharing_intention | 2 | 2 | 0.165 | [0.053, 0.277] | 0.0000 |
| did_all | ON | ALL | 47 | 28 | 0.111 | [0.038, 0.183] | 0.0288 |
| did_all | ON | domain: credibility | 31 | 23 | 0.089 | [-0.008, 0.186] | 0.0439 |
| did_all | ON | domain: sharing_intention | 14 | 12 | 0.100 | [0.018, 0.181] | 0.0115 |
| did_all | ON | domain: technique_recognition | 2 | 2 | 0.325 | [-0.182, 0.832] | 0.1255 |
| did_all | OFF | ALL | 49 | 28 | 0.103 | [0.028, 0.178] | 0.0305 |
| did_all | OFF | domain: credibility | 32 | 23 | 0.082 | [-0.018, 0.183] | 0.0465 |
| did_all | OFF | domain: sharing_intention | 15 | 12 | 0.087 | [0.003, 0.171] | 0.0112 |
| did_all | OFF | domain: technique_recognition | 2 | 2 | 0.325 | [-0.182, 0.832] | 0.1255 |
| did_cred | ON | ALL | 35 | 27 | 0.098 | [0.020, 0.176] | 0.0317 |
| did_cred | ON | domain: credibility | 31 | 23 | 0.089 | [-0.008, 0.186] | 0.0439 |
| did_cred | ON | domain: sharing_intention | 4 | 4 | 0.164 | [0.004, 0.325] | 0.0200 |
| did_cred | OFF | ALL | 36 | 27 | 0.091 | [0.011, 0.171] | 0.0325 |
| did_cred | OFF | domain: credibility | 32 | 23 | 0.082 | [-0.018, 0.183] | 0.0465 |
| did_cred | OFF | domain: sharing_intention | 4 | 4 | 0.156 | [0.000, 0.313] | 0.0190 |

### Publication bias (study-level, exclusions ON)

| spec | method | est | 95% CI | p | added k |
|---|---|---:|---|---:|---:|
| post_all | egger | 0.558 | [-1.187, 2.303] | 0.531 |  |
| post_all | pet | 0.108 | [0.037, 0.180] | 0.003 |  |
| post_all | peese | 0.118 | [0.074, 0.162] | 0.000 |  |
| post_all | trimfill | 0.141 | [0.099, 0.184] |  | 0 |
| post_cred | egger | 3.198 | [0.516, 5.880] | 0.019 |  |
| post_cred | pet | 0.036 | [-0.065, 0.137] | 0.486 |  |
| post_cred | peese | 0.092 | [0.032, 0.151] | 0.002 |  |
| post_cred | trimfill | 0.012 | [-0.050, 0.074] |  | 31 |
| persistence_all | egger | 2.379 | [-3.864, 8.622] | 0.455 |  |
| persistence_all | pet | 0.025 | [-0.144, 0.194] | 0.774 |  |
| persistence_all | peese | 0.088 | [-0.014, 0.190] | 0.091 |  |
| persistence_all | trimfill | 0.009 | [-0.103, 0.121] |  | 10 |
| persistence_cred | egger | -0.632 | [-6.150, 4.885] | 0.822 |  |
| persistence_cred | pet | 0.144 | [-0.029, 0.318] | 0.103 |  |
| persistence_cred | peese | 0.155 | [0.049, 0.262] | 0.004 |  |
| persistence_cred | trimfill | 0.187 | [0.084, 0.290] |  | 0 |
| did_all | egger | 0.625 | [-1.535, 2.785] | 0.571 |  |
| did_all | pet | 0.024 | [-0.097, 0.145] | 0.697 |  |
| did_all | peese | 0.046 | [-0.029, 0.120] | 0.229 |  |
| did_all | trimfill | 0.063 | [-0.013, 0.140] |  | 3 |
| did_cred | egger | 0.537 | [-1.437, 2.512] | 0.594 |  |
| did_cred | pet | 0.021 | [-0.108, 0.150] | 0.754 |  |
| did_cred | peese | 0.038 | [-0.043, 0.119] | 0.358 |  |
| did_cred | trimfill | 0.049 | [-0.033, 0.131] |  | 3 |

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

Total fallback contrasts: 44 (recorded per contrast in `temp/sdt_specs.csv` via `fallback = sharing_intention`).

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
