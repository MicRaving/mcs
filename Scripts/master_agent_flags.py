"""Collect the flags the master agents raise for the human master.

While settling the paper class, the master agents keep finding things the
human master needs to see: a factual error in a numeric field, a
``Rationale_Notes`` sentence that contradicts the record, a raw-versus-
standardised scale question.  They report these in their replies, where they
would be lost.

This script is the register those flags are written into.  Each row says
which study, what the agent found, and what the human has to decide.

Products
--------
``Results/master_agent_flags.csv``   the register
"""
import csv
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")
OUT = os.path.join(RESULTS, "master_agent_flags.csv")

COLS = ["study_key", "field", "kind", "what_the_agent_found", "who_decides"]

#: the flags raised by the master agents so far, transcribed from their
#: reports.  ``kind``:
#:   numeric_error      a numeric field is wrong; the human owns the numbers
#:   stale_note         Rationale_Notes contradicts the record
#:   scale_question     a pooled metric mixes scales
#:   scope_ambiguity    which experiment or contrast belongs in the review
FLAGS = [
    # --- Comer et al. (2008)
    ("Comer et al. (2008)", "m1i / m2i / sd1i / sd2i", "numeric_error",
     "Rows 26, 28, 30, 32 (the T2 threat-perception rows) carry Table 1's "
     "T1 (pre-clip) cells (e.g. CML 2.8 / DAU 3.1) where Table 1's T2 is "
     "3.0 / 3.6. The MM route has the T2 cells. The agent set "
     "effect_favors from the paper's T2 cells so the direction is right once "
     "the values are corrected.", "human master (numeric effect field)"),
    ("Comer et al. (2008)", "Rationale_Notes", "stale_note",
     "The note says 'ELIGIBLE_PRIMARY = 0 on every row' but the rows carry "
     "eligible_primary = 1.", "master agent (prose refresh)"),
    ("Comer et al. (2008)", "timepoint_1", "pool_change",
     "The 15 T3 rows move from the persistence pool to the immediate-posttest "
     "pool: T3 is the same-session post-discussion measure and the paper "
     "states no long-term follow-up was taken.", "already applied by the "
     "master agent"),
    ("Comer et al. (2008)", "dv_domain", "pool_change",
     "34 rows move out of the `credibility` domain into `other`: the paper "
     "measures no source or message credibility.", "already applied by the "
     "master agent"),
    # --- Choi et al. (2023)
    ("Choi et al. (2023)", "yi / vi / effect_size_type_reported",
     "scale_question",
     "The rows pool as raw betas (yi = beta, vi = SE^2). The standardised "
     "yi/vi already stored are a d derived from the reported t, and only a "
     "standardised effect_size_type_reported would let them pool as d. The "
     "agent kept the paper's reported type ('beta'), so the pooled metric is "
     "unchanged - but the record now holds a standardised effect under a raw "
     "type label.", "human master (pooling metric)"),
    # --- Clayton et al. (2021)
    ("Clayton et al. (2021)", "Age_Mean / Age_SD / Gender_Ratio / "
     "N_Recruited / code_available", "scope_ambiguity",
     "Only the Online Appendix is in the workspace (the PDF *is* the "
     "appendix); age, gender, recruited N and any data/code statement live in "
     "the missing main text, so those fields stay empty.", "human master "
     "(acquire the main text)"),
    # --- Athey et al. (2022) / Axson
    ("Athey et al. (2022)", "Study_ID / N_Recruited", "scope_ambiguity",
     "The SB route splits the paper into two records (S1_Courses, "
     "S1_Nudge) where the canonical and MM routes hold one; the agent noted "
     "the arm-level Ns (8684 total) come from the paper's own table.",
     "master agent (record structure)"),
    # --- Austin et al. (2020 / 2020 a / 2024)
    ("Austin et al. (2020) / (2020 a) / (2024)", "study_excluded",
     "duplicate_trial",
     "Three publications of the same FoodMania! trial (189 dyads), all "
     "pooled; the 2024 paper says so on p. 126. See "
     "Results/possible_duplicate_trials.csv.", "human master (which "
     "outcomes are unique)"),
    ("Austin et al. (2025) / (2026)", "study_excluded", "duplicate_trial",
     "Companion reports of one experiment (1201 Qualtrics sample; 1167 "
     "analysed).", "human master (which outcomes are unique)"),
    # --- Armitage (2022)
    ("Armitage (2022)", "Study_ID / N_Analyzed", "scope_ambiguity",
     "The canonical file's single record holds the MAIN experiment (N = 980) "
     "but is labelled Armitage2022_Exp1, which the agent routes use for the "
     "PILOT (n = 94). The pilot is uncoded and the id misleads; the agent "
     "wrote the main experiment's paper values.",
     "human master (record structure and Study_ID)"),
    # --- Bermejo-Berros (2021)
    ("Bermejo-Berros (2021)", "N_Analyzed", "scope_ambiguity",
     "The agent set N_Analyzed = 246 from '246 ninos ... participan en cuatro "
     "sesiones' with no exclusions reported, and the paired t-tests of "
     "Table 1 cover all 12 groups.", "already applied by the master agent"),
    # --- Biddlestone et al. (2025 a)
    ("Biddlestone et al. (2025 a)", "Media_Support / Technique_Taxonomy",
     "vocabulary_error",
     "Media_Support holds the bare string 'video' (not a schema token), and "
     "Exp2's Technique_Taxonomy holds the fallacy names (whataboutism, "
     "moving_goalposts, strawman) rather than schema tokens.",
     "master agent (vocabulary repair)"),
    # --- from the human-evidence pass
    ("Lane (2021)", "dv_polarity (the parallel untrustworthy DV)",
     "numeric_error",
     "The evidence pass found the disputed trustworthy-headline DV is 1 (higher "
     "= more willingness to share credible news), but the canonical route "
     "codes the parallel UNtrustworthy DV as 1 in Exp1/2b/3a/3b and 0 in "
     "Exp4 - the same measure with inconsistent polarity.",
     "human master (dv_polarity)"),
    ("Fridkin et al. (2015)", "dv_polarity (ad tone, ad usefulness)",
     "scale_question",
     "Both scales rate the respondent's approval of a misleading attack ad, "
     "and the debunking fact-check LOWERS both (p. 136), so lower = desirable "
     "-> polarity 0, consistent with the accuracy/claim-acceptance DVs all "
     "three routes already code 0; the canonical 1 is the outlier.",
     "human master (dv_polarity)"),
    ("Gambin & Munzel (2026)", "dv_polarity (critical thinking, information "
     "overload)", "scale_question",
     "Critical thinking = 1 (higher = more skill; the intervention lowered "
     "it) and information overload = 0 (higher = more overload; the "
     "intervention raised it); the canonical route flips polarity within the "
     "same measure from row to row. The flagged eligible_primary conflict is "
     "a row-ordering artefact (both routes code prior-exposure/participation "
     "= 1, issue-involvement = 0); the hidden difference is the Exp 2 "
     "prior-exposure x change rows (canonical 1 vs MM 0).",
     "human master (dv_polarity)"),
    ("Faulkner et al. (2011)", "Fulltext_Exclusion_Criterion",
     "scope_ambiguity",
     "One-group 'preexperimental' panel design with no comparison condition "
     "(the authors say 'no comparison regions are possible', p. 522): C2 is "
     "certain, C4 follows their own word 'preexperimental', and the live call "
     "is C1 - the paper calls LLK a 'media literacy campaign' (p. 520) but "
     "the outcome is campaign recall of health messaging, not misinformation "
     "resistance.", "human master (inclusion)"),
    ("Lawson & Kakkar (2022)", "Fulltext_Exclusion_Criterion",
     "scope_ambiguity",
     "C2 applies to all 8 experiments, but C1 only to the 6 without an "
     "intervention (Studies 1, 2, 5, 6, S1, S2): Studies 3 and 4 do contain "
     "misinformation interventions (fact-checker warning tags, pp. 24, "
     "27-29), so a blanket C1 is wrong. All studies also embed a uniform "
     "accuracy prompt before the sharing question (p. 15), which never "
     "creates a contrast.", "human master (inclusion)"),
    ("König & Suhr (2023)", "eligible_primary (x2)", "scope_ambiguity",
     "A one-group pre->post evaluation with no comparison arm at all (p. 3) "
     "and only within-subject paired t-tests; the authors state pre-post "
     "designs 'do not allow for causal inferences' (p. 13) -> 0 for both DVs. "
     "A pre->post change can enter the DiD pool only via the opt-in "
     "fully-within switch.", "human master (eligibility)"),
    ("Gordon et al. (2016)", "eligible_primary (Media deconstruction)",
     "scope_ambiguity",
     "The disputed slot holds two different contrasts (SB: the 2-df omnibus "
     "Time x Condition; MM: the Wave-3 'crossover' 10.27 vs 11.53). The "
     "paper says Wave 3 'does not ensure parallel data structure ... both "
     "groups have received the intervention' and its Wave-3 analyses are "
     "within-arm change contrasts (p. 951) -> keep 0 there and keep 1 for the "
     "Wave-2 post-test (11.41 vs 7.99).", "human master (eligibility)"),
    ("Kuhn et al. (2022)", "Fulltext_Exclusion_Criterion", "scope_ambiguity",
     "C1, C2 and C4 all apply: an economics working paper on Swiss "
     "apprenticeship training whose 'norms' come from plebiscite voting "
     "results used as an instrumental variable (pp. 3, 19) - no "
     "misinformation content, no control group, not an experiment. Flag: the "
     "PDF is the February 2019 WP No. 157 while the key says 2022.",
     "human master (inclusion, and the year)"),
    ("Fayard & Fayard (2021)", "Fulltext_Exclusion_Criterion",
     "scope_ambiguity",
     "A signed opinion column in Neonatology Today - no methods, "
     "participants, results, tables or figures, only 17 references -> C1, C2 "
     "and C4 all hold; the SB list differs only by omitting C2.",
     "human master (inclusion)"),
    ("Ku et al. (2026 c)", "Fulltext_Exclusion_Criterion", "scope_ambiguity",
     "It is a protocol: 'No data was used for the research described in the "
     "article' (p. 5) -> C4 is certain. The open call is C1, since the SMS "
     "content does carry myth-correction ('myths and facts', p. 3) and one "
     "outcome is 'knowledge of common vaccine misinformation' (p. 1), while "
     "the primary aim is vaccine uptake. C2/C3 do not apply.",
     "human master (inclusion)"),
    ("Bogart & Markman (2025)", "Fulltext_Exclusion_Criterion",
     "scope_ambiguity",
     "The field is PER EXPERIMENT and the four studies are not alike: the "
     "disputed record is Study 1 (a single-sample predictor study, no "
     "manipulation, no control arm -> C1+C2+C4), while Study 4 is the only "
     "record with a misinformation-type intervention (a "
     "polarization-misperception correction video) and a no-intervention "
     "control.", "human master (inclusion, per experiment)"),
    ("Bohman et al. (2004)", "eligible_primary (x3)", "scope_ambiguity",
     "It is an ALCOHOL-PREVENTION curriculum, not a misinformation study "
     "(the only outcome with flavour is a 3-item 'Media Literacy' scale about "
     "commercials), and the record already carries "
     "Fulltext_Exclusion_Criterion = 'C1'. Table 4 offers TWO "
     "intervention-vs-control contrasts per dimension, so at most one row "
     "could carry the flag.", "human master (eligibility)"),
    ("Boon & Baxter (2000)", "eligible_primary", "scope_ambiguity",
     "The routes disagree about WHICH contrast the row is (Warned-vs-Neutral "
     "vs Warned-vs-Standard), not just the flag. Only Warned-vs-Neutral "
     "isolates the warning (the paper says Yield 1 is 'indicators of the "
     "effect of the warning alone'); Warned-vs-Standard is the paper's own "
     "pre-stated hypothesis but confounds the warning with the absence of "
     "negative feedback.", "human master (eligibility)"),
    ("Brugman et al. (2026)", "Perceived ad truthfulness (dv_polarity)",
     "numeric_error",
     "The paper's manipulation-check sentence contradicts itself ('less "
     "true' but M = +3.69 for the debunk arm): only the prose is consistent "
     "with the design, so the means are swapped in print. With dv_polarity 0 "
     "and the swapped means the row re-signs to a beneficial +1.79.",
     "human master (dv_polarity, and the printed means)"),
    ("Bråten et al. (2019)", "Content-based justifications (dv_polarity)",
     "scale_question",
     "Content-based justification (relevance/believability/style) is the "
     "OPPOSITE of the source-feature-based reasoning the intervention "
     "teaches, so higher = worse -> 0; with the canonical 1 the row enters "
     "the pool as a harmful effect that contradicts the paper.",
     "human master (dv_polarity)"),
    ("Brugman et al. (2026)", "n1i / n2i (DV[0], DV[1])", "numeric_error",
     "The paper is internally inconsistent on the arm sizes: Table A1 and the "
     "one-sample t(327) give debunk n=328 / no-debunk n=329 for the "
     "persuasive-impact variables, while the prose (p. 8) and the "
     "manipulation-check Welch df (600.44, verified numerically) give "
     "329/328. Each was recorded as printed; the n's need one decision.",
     "human master (numeric effect field)"),
    ("Bråten et al. (2019)", "vi (DV[4]-DV[9], DV[16])", "numeric_error",
     "The within-group vi uses the independent-groups formula where a paired "
     "formula fits the reported paired t (e.g. DV[7] canonical 0.0823075 vs "
     "paired 0.064615).", "human master (numeric effect field)"),
    ("Bråten et al. (2019)", "Rationale_Notes", "stale_note",
     "The note says 'Intervention_Type=sourcing' while the record holds "
     "'lateral_reading'. Confirmed independently by "
     "Scripts/check_rationale_consistency.py.", "master agent (prose refresh)"),
    ("Burger et al. (2022)", "Rationale_Notes", "stale_note",
     "The note says 'N_Inference_Method=3' while the record holds 1 - and the "
     "note itself describes an equal split of 1324 across 3 arms = 441/arm. "
     "Confirmed by Scripts/check_rationale_consistency.py.",
     "master agent (prose refresh)"),
    # --- from the human-evidence pass (the per-experiment cases)
    ("Pan & Hu (2025)", "dv_polarity (the PTJ rows)", "numeric_error",
     "'Proportion of true judgments' is PERCEIVED truth, not accuracy: the "
     "paper says the warning flags reduced truth perception but 'neither "
     "improved individual discernment (judgmental accuracy)', so higher PTJ "
     "= more falsehoods believed = worse (0). The polarity must flip for the "
     "true-statements-only rows, and the canonical file is currently "
     "inconsistent - the Exp3 warning row is 0 while every other PTJ row is 1.",
     "human master (dv_polarity, per row)"),
    ("Orosz et al. (2024 a)", "Fulltext_Exclusion_Criterion",
     "scope_ambiguity",
     "The dispute is on Exp1/Study 1, a one-wave observational survey with no "
     "manipulation: the call is whether its pro-government-vs-anti-government "
     "voter comparison counts as a 'comparison group' (C2). Read as no control "
     "arm -> ['C1','C2','C4']. Study 2 is a clean RCT with a matched control "
     "and stays empty - the field is per experiment.",
     "human master (inclusion, per experiment)"),
    ("Poon et al. (2020)", "Fulltext_Exclusion_Criterion", "scope_ambiguity",
     "The dispute is on Exp1/Study 1, a single-sample correlational MTurk "
     "survey - 'the correlational nature of the study did not allow us to "
     "draw a causal conclusion' - with no arm that could be a control, so C2 "
     "belongs -> ['C1','C2','C4']. Exp4 (self-affirmation) properly keeps an "
     "empty criterion.", "human master (inclusion, per experiment)"),
    ("Paek & Hove (2012)", "Fulltext_Exclusion_Criterion", "scope_ambiguity",
     "The paper never says 'misinformation' or 'fake news' and the authors "
     "state 'our research design is limited due to a pre- and posttest design "
     "without a control group' -> C1+C2; the only open call is whether to add "
     "C4 for the uncontrolled design.", "human master (inclusion)"),
    ("Porter et al. (2018)", "Fulltext_Exclusion_Criterion", "scope_ambiguity",
     "A Research Brief / secondary analysis of media-literacy counter-ad "
     "production with no misinformation content at all (the words never "
     "appear) -> C1, giving ['C1','C2','C4']; the only wiggle room is the "
     "'truthful labeling' counter-ad format, whose target is a beverage label "
     "rather than misinformation.", "human master (inclusion)"),
    ("Capewell et al. (2024)", "yi / vi (Exp1 row 9)", "numeric_error",
     "Exp1 row 9 ('Confidence ... pooled items', 48 h, no immediate posttest) "
     "carries yi/vi IDENTICAL to row 7's manipulativeness row - a copy error "
     "in the effect fields (the SB route has d = 0.0101 for that contrast).",
     "human master (numeric effect field)"),
    ("Capraro & Celadin (2023)", "N_Reported_Per_Condition", "numeric_error",
     "Still 0 although the supplement PRINTS the analysed cell sizes (S1 "
     "198/198/197, S2 197/175/186, S3 186/180/184, S4 186/186) and the record's "
     "own Rationale_Notes says 'cell sizes are not printed' - which is now "
     "stale. The agent set N_Inference_Method = 1 (Reported) but left this "
     "field, which was not on its list.", "human master (sample sizes)"),
    ("Calvillo et al. (2026)", "Gender_Ratio (Exp1)", "numeric_error",
     "The paper's printed percentages 82.2 / 85.6 are a typesetting error; the "
     "counts are what the paper prints (female 240, male 250, other 0, "
     "prefer_not_to_say 2). The record holds the counts.",
     "already applied by the master agent"),
    ("Calvillo et al. (2026)", "Fulltext_Exclusion_Criterion",
     "scope_ambiguity",
     "Study 1 is a task-order manipulation only - both arms get the same 12 "
     "statements, no intervention - so Exp1 carries ['C1'] and Exp2 stays "
     "empty; control_group_type likewise (Exp1 = 1, Exp2 = 5).",
     "human master (inclusion, per experiment)"),
    ("Nyhan et al. (2023)", "dv_polarity (Total content views per day)",
     "scale_question",
     "The paper NEVER orients this measure - it is an exposure count (effect "
     "-0.05 s.d., 267 -> 255 views/day) - so 0 vs 1 flips the sign of the "
     "pooled effect. It is a convention call, not a paper fact.",
     "human master (dv_polarity)"),
    ("O'Connor (2020)", "Fulltext_Exclusion_Criterion", "scope_ambiguity",
     "Explicitly a commentary: no experiment, no arms, no sample, no "
     "misinformation intervention -> the paper supports C1 + C2 + C4 (the SB "
     "C4-only value is the minority).", "human master (inclusion)"),
    ("OPHIR et al. (2020)", "eligible_primary (x2) / control_group_type",
     "scope_ambiguity",
     "The study has BOTH control kinds - a salmon video and the uncorrected "
     "protobacco video - and the paper states its effect claims against the "
     "UNCORRECTED video while calling the salmon video 'one control'. "
     "control_group_type can only hold one, and marking the salmon contrast 1 "
     "would add a third contrast per outcome to the same experiment.",
     "human master (eligibility and control type)"),
    ("Bronnenberg et al. (2020)", "Intervention_Type", "scope_ambiguity",
     "The 'intervention' is an in-store blind taste test - an information "
     "treatment about product quality, not a message correcting "
     "misinformation (the SB route recorded ['C1']).", "human master "
     "(inclusion)"),
    ("Broniatowski et al. (2024)", "Rationale_Notes (Exp3/Exp4)",
     "stale_note",
     "The note says '(N_Inference_Method=2)' and 'inferred by equal split' "
     "while the field is now 1 (cell n reported). Flagged by "
     "Scripts/check_rationale_consistency.py.", "master agent (prose refresh)"),
    ("Brodsky (2022)", "Age_Mean / Age_SD (Exp3)", "numeric_error",
     "Still null although p. 96 gives M 19.6 (SD 4.5). The field was not on "
     "the batch's item list.", "master agent (sample description)"),
    ("Bronnenberg et al. (2020)", "Rationale_Notes (Feedback_Mechanics)",
     "stale_note",
     "The note says 'Feedback_Mechanics=1' while the record carries 2 "
     "(reported by the agent as a pre-existing mismatch).",
     "master agent (prose refresh)"),
    ("Dorling et al. (2018)", "Rationale_Notes (Feedback_Mechanics)",
     "stale_note",
     "The note says 'Feedback_Mechanics=1' while the record carries 2.",
     "master agent (prose refresh)"),
    # --- more from the human-evidence pass
    ("McVey & Davis (2002)", "eligible_primary (x3)", "scope_ambiguity",
     "All three items reduce to two questions: the baseline (pretest) row must "
     "be 0, and the 6-/12-month follow-up cells are genuine programme-vs-"
     "control contrasts (1). The routes disagree because they carry 15/14/17 "
     "rows per name and the positional pairing mixes Table 1 cells. The "
     "paper's headline is 'There was no program effect' - the Group x Time "
     "interactions are null and the only significant between-group term "
     "(F(1,157) = 4.34) is time-averaged over all four occasions.",
     "human master (eligibility)"),
    ("Maertens (2022)", "eligible_primary", "scope_ambiguity",
     "A row-alignment artefact: the canonical uses study-specific dv_names so "
     "it takes no part; MM's slot-14 row is the booster-vs-control contrast at "
     "29 days (d = 0.475, t(685) = 5.02) and SB's is Study 2's within-person "
     "misinformation effect (d = -0.237) - only the former is a control "
     "contrast. Separately, decide whether Study 2's 8-day, 29-day and "
     "29-day-booster rows may all be eligible: the booster and plain-"
     "inoculation rows share ONE control arm (n = 206 at T3).",
     "human master (eligibility and dependence)"),
    ("Jolley (2014)", "eligible_primary (x2)", "scope_ambiguity",
     "Both disputes are the same question - the pre-warning vs no-warning "
     "contrast is a genuine intervention-vs-control row with per-cell M/SD in "
     "Table 16 (3.00 / 3.11 / 3.33), so 1 is supportable. The SB route's "
     "F(4, 255) = 8.32 belongs to Study 9 (5 conditions, N = 260); Study 10's "
     "own omnibus is F(2, 127) = 0.63, p = .534, and Study 10 reports no "
     "pairwise test at all.", "human master (eligibility)"),
    ("Jolley & Douglas (2017)", "eligible_primary (Perceived dangers of "
     "vaccines)", "scope_ambiguity",
     "The disputed contrast is intervention-vs-ACTIVE (conspiracy/anti-"
     "conspiracy vs conspiracy-only), not intervention-vs-no-information-"
     "control, so 0 is the better flag - and Study 2 has NO control condition "
     "at all. Separately, the MM route's row is mislabelled: its numbers 2.56 "
     "(1.48) n = 64 vs 3.28 n = 58 are Study 2's Anti-conspiracy/Conspiracy "
     "vs Conspiracy cells, not Conspiracy/Anti-conspiracy.",
     "human master (eligibility)"),
    ("Mariia Plotnikova et al. (2025)", "Fulltext_Exclusion_Criterion",
     "scope_ambiguity",
     "C4 is certain (a 507-person survey, no manipulation, no randomisation); "
     "the judgement is C2 - the only 'comparison' is self-selected, "
     "non-randomised course-takers (n = 153) vs non-takers (n = 354), and the "
     "paper flags self-selection bias itself. If that counts as a comparison "
     "group the field becomes ['C4'] and an observational contrast enters an "
     "experimental meta-analysis. The paper also swaps its group labels "
     "between Table 1 and the prose.", "human master (inclusion)"),
    ("Illari et al. (2022)", "Fulltext_Exclusion_Criterion", "scope_ambiguity",
     "C1+C2+C4 (canonical and MM), not the SB route's C1+C4: the paper is "
     "observational Facebook network mapping plus a mathematical model, "
     "nothing is delivered (C1), there is no control arm (C2), and it says "
     "'The true impact of any intervention must ultimately be tested "
     "empirically' (p. 6). The whole deposit contains no p value, no F, no CI "
     "and no chi-square.", "human master (inclusion)"),
    ("McDougall et al. (2022)", "Fulltext_Exclusion_Criterion",
     "scope_ambiguity",
     "C2 and C4 are certain (qualitative evaluation: analytics, one public "
     "focus group of six volunteers, seven staff interviews - no control "
     "group, no outcome measure, no test statistic); the routes differ only "
     "on C1, and the paper itself calls Boubli 'a truly unique media "
     "literacy intervention' that 'boosts resilience to misinformation', so C1 "
     "should stay off (canonical and MM).", "human master (inclusion)"),
    ("Meador et al. (2024)", "Fulltext_Exclusion_Criterion", "scope_ambiguity",
     "C4 is certain (matched-county observational 'Brief Research Report'; the "
     "authors say 'we cannot definitively assign causality'); the call is C1 - "
     "the exposures are vaccination-promotion/access channels with "
     "vaccination totals as the only outcome, but the paper explicitly frames "
     "the programme as 'quashing'/'counteracting misinformation' (pp. 1, 5), "
     "which would make ['C4'] (the MM route) defensible. Also: the key says "
     "2024 but the paper is published 2023.", "human master (inclusion, and "
     "the year)"),
    ("Hämäläinen et al. (2023)", "eligible_primary", "scope_ambiguity",
     "The MM route's 0 sits on a row carrying the PRE-test cells (0.37/0.87 "
     "vs 0.32/0.72, n = 196/169 = the group sizes, not the analysed cells "
     "182/143) - a baseline row - while the canonical's 1 is on the pre->post "
     "row. The paper reports no DiD, only the pre-test-adjusted regression "
     "(Table 4, d = 0.16 / 0.39 / 0.37), and 'Sourcing in credibility "
     "judgments' is unbalanced at baseline (t(342.03) = -2.05, p = .041, "
     "d = 0.22).", "human master (eligibility)"),
    ("Iles et al. (2021)", "dv_polarity (Message-elicited anger)",
     "scale_question",
     "The paper explicitly classes anger as an AFFECTIVE RESISTANCE outcome "
     "and counts the inoculation's increase in anger as success (Table 4: "
     "Anger *(+)), which supports the canonical 1 - but the disputed row is "
     "the 2-df interaction, where the paper reports 'anger: F(2, 797) = 0.57, "
     "p = .567' and no signable simple effect, so the flag is nearly inert "
     "there. The SB route's 0 (more anger = worse) is the ordinary reading "
     "and is also defensible.", "human master (dv_polarity)"),
    ("Bogart & Markman (2025)", "WEIRD_Educated / Media_Support (Exp4)",
     "scale_question",
     "Two judgement calls the master made that a human may want to review: "
     "WEIRD_Educated -> null (the paper reports no education data, so "
     "'unclear' beats an unsupported 'educated'), and Exp4 Media_Support -> "
     "['Text','Visual aids','Video'] (the text and graph belong to the threat "
     "induction the video is added to).", "human master (coding fidelity)"),
    ("Bogart & Markman (2025)", "Rationale_Notes (Exp4)", "stale_note",
     "N_Inference_Method = 3 while the note says '=2' (the paper reports "
     "N = 297 and no cell sizes). All three routes agree on the value, so it "
     "is not a disagreement - only the prose is stale.",
     "master agent (prose refresh)"),
    ("Bohman et al. (2004)", "missing_metrics_reason (DV rows 25-30)",
     "narrative_correction",
     "The canonical text claimed 'no per-cell means/SDs', but p. 30 prints "
     "the cell means and no SDs; the two grade rows are df_num = 2 three-cell "
     "omnibus terms. The master agent rewrote the reason accordingly.",
     "already applied by the master agent"),
    ("Butler et al. (2024) / Butler & Swire-Thompson (2026)",
     "WEIRD_Educated / WEIRD_Rich", "scale_question",
     "The two Butler papers now code WEIRD_Rich = 1 but WEIRD_Educated stays "
     "null in Butler et al. (2024) (no education data) while it is 1 in "
     "Butler & Swire-Thompson (2026) (supplement Tables S2/S3). If the two "
     "should be coded identically, that is a separate decision.",
     "human master (coding consistency)"),
    ("Jolley et al. (2026)", "Fulltext_Exclusion_Criterion", "scope_ambiguity",
     "The two records need DIFFERENT answers: Study 1 is correlational "
     "(-> ['C2','C4']), Study 2 is a randomised experiment with a real "
     "control (so C2/C4 are out) whose manipulation is positive parasocial "
     "contact with the police, not a misinformation intervention -> ['C1']. "
     "The word 'misinformation' appears exactly once in the whole paper.",
     "human master (inclusion, per experiment)"),
    ("Kaňková & Matthes (2026)", "eligible_primary / dv_polarity",
     "scope_ambiguity",
     "The misinformation x intervention contrast (H3) is a real "
     "intervention-vs-no-inoculation control -> 1; but the OVERGENERALIZED "
     "cells (RQ1) are a message type the paper explicitly separates from "
     "misinformation -> 0 unless you count it. dv_polarity = 0 for all four "
     "DVs because in the misinformation condition higher credibility/trust/PSR "
     "= more taken in - with the caveat that 'belief inaccuracy' vs 'belief "
     "accuracy' flips the sign.", "human master (eligibility and polarity)"),
    ("Kershaw et al. (2021)", "Fulltext_Exclusion_Criterion",
     "scope_ambiguity",
     "C4 is certain (the authors call it a 'quasi-experimental design', a "
     "cross-sectional survey) and C1 is certain (an eHealth information "
     "resource, no misinformation stimulus); C2 is the open call - there IS a "
     "comparison group (CITI-naive) but exposure was self-selected, so it "
     "depends on whether 'control group' means an ASSIGNED control.",
     "human master (inclusion)"),
    ("Kim & Lim (2026)", "dv_polarity (organizational reputation)",
     "scale_question",
     "The company is the VICTIM of the misinformation and issues the "
     "correction, which raises its reputation (3.31 -> 5.09 etc.), so "
     "organizational reputation is 1 (the SB route's 0 is wrong on the "
     "paper's own numbers); the only arguable one is bandwagon perception, a "
     "mediator with no protective direction.",
     "human master (dv_polarity)"),
    ("Ku et al. (2025)", "eligible_primary", "scope_ambiguity",
     "Single-arm pre/post, 101 students, paired-samples t-tests only, no "
     "control (the 27-student pilot explicitly did not take part) -> 0; the "
     "MM route's 1 cannot stand, and the study therefore contributes nothing "
     "to the pooled products.", "human master (eligibility)"),
    ("Modirrousta-Galian & Higham (2023)", "eligible_primary",
     "duplicate_trial",
     "A secondary reanalysis (no new participants) whose every "
     "per-experiment statistic is a within-arm pre-post AUC/B\"D contrast; "
     "the only intervention-vs-control number is its pooled Table 3 model "
     "comparison (p. 52) across k = 13 experiments - and 4 of the 5 "
     "reanalysed studies are ALREADY separate papers in the corpus, so "
     "coding an eligible effect here double-counts the same trials.",
     "human master (eligibility and non-independence)"),
    ("Mihailidis (2020)", "Fulltext_Exclusion_Criterion", "scope_ambiguity",
     "Only C1 is open: the paper's own Limitations state 'we did not "
     "implement controls or quantitative measurements' (p. 770) and 'Future "
     "studies can build on this by designing controlled experiments' (p. 777), "
     "so C2 + C4 are certain. C1 turns on whether a civic-expression "
     "digital-literacy toolkit with no misinformation content counts as a "
     "misinformation intervention (if not, the field is ['C2','C4']).",
     "human master (inclusion)"),
    ("Naranđa (2022)", "Fulltext_Exclusion_Criterion", "scope_ambiguity",
     "All routes agree on C2 + C3; only C4 is disputed - the article is a "
     "'Stručni rad / Professional Paper' (p. 40) reporting one post-hoc "
     "anonymous questionnaire (151 students, no comparison, three threshold "
     "hypotheses, p. 42), so the MM route should add C4 -> ['C2','C3','C4'].",
     "human master (inclusion)"),
    ("National Literacy Trust (2018)", "Fulltext_Exclusion_Criterion",
     "scope_ambiguity",
     "C2 is certain (the report contains no control/comparison group at all - "
     "every contrast is a within-pupil pre->post percentage); the judgement "
     "is whether this single-arm pre-post programme evaluation is a 'primary "
     "experimental study' (canonical: no -> ['C2','C4']) or a fully-within "
     "design the campaign already handles (SB/MM: yes -> ['C2']). It is grey "
     "literature reporting only self-reported survey percentages and no effect "
     "sizes.", "human master (inclusion)"),
    ("Nazarnia et al. (2023)", "eligible_primary", "scope_ambiguity",
     "The MM route's 1 is supportable - Table 3 (p. 232) gives per-arm n/M/SD "
     "for intervention (50) vs no-treatment control (50) at pre-test, "
     "immediate post-test and 12 weeks, so the two-arm d is computable - but "
     "the value must be derived from those descriptives (the paper's own "
     "reported test is the omnibus group x time ANOVA, which the pipeline "
     "guard refuses), and only the immediate-post-test contrast belongs to "
     "the primary pool.", "human master (eligibility)"),
    ("Bråten et al. (2019)", "effect_size_type_reported (the six delayed rows)",
     "pool_change",
     "A mis-coded reported type silently DROPS rows from the pool: the six "
     "delayed-posttest contrasts were coded 'none', which the standardisation "
     "guard read as 'not standardised'. Corrected to the paper's Table 4 "
     "types ('means'/'r'), they re-enter the persistence pool (381 -> 387 "
     "contrasts, 168 -> 172 restricted); their yi/vi were unchanged and the "
     "post-test and DiD pools did not move.", "already applied by the "
     "master agent"),
    ("Ranieri et al. (2017)", "Fulltext_Exclusion_Criterion",
     "scope_ambiguity",
     "An e-MEL project report: 81 teachers, qualitative triangulation, and the "
     "authors' own words 'our study cannot be defined as a comparative one' "
     "(C2 certain; English, so no C3). Zero mentions of "
     "misinformation/disinformation/fake news/fact-checking (C1 defensible) and "
     "no experimental design (C4 defensible) -> recommend ['C1','C2','C4'].",
     "human master (inclusion)"),
    ("ROJAS-ESTRADA et al. (2025)", "Fulltext_Exclusion_Criterion",
     "scope_ambiguity",
     "A Spanish-language, qualitative phenomenological single-group course "
     "evaluation (52 students) with no 'control'/'grupo control'/randomisation/"
     "inferential test anywhere (C2 + C3 certain). C1 does NOT apply - the "
     "course explicitly trains 'Acciones para mitigar la desinformacion'. C4 is "
     "the arguable addition (recommend ['C2','C3']; the canonical "
     "['C2','C3','C4'] if 'experimental' is required).",
     "human master (inclusion)"),
    ("Powell & Gross (2018)", "Fulltext_Exclusion_Criterion",
     "scope_ambiguity",
     "A one-group pretest-posttest media-literacy workshop on food "
     "advertising with no control arm (C2 certain); the paper never says "
     "'misinformation'/'fake news'/'veracity' (0 hits), so C1 is the human's "
     "call - recommend ['C2'], or the SB route's ['C1','C2'] if "
     "misinformation content is required.", "human master (inclusion)"),
    ("Rathje (2022)", "eligible_primary", "scope_ambiguity",
     "Four experiments, a genuine no-incentive control in each, all statistics "
     "single-df; each experiment yields 3-4 intervention-vs-control contrasts, "
     "so only ONE qualifies per experiment - accuracy-incentive vs control on "
     "truth discernment (Exp 1 d = 0.24, Exp 2 d = 0.41, Exp 3 F(1,917) = "
     "4.44, Exp 4 d = 0.28). The partisan-sharing and mixed conditions are "
     "non-protective (ns vs control), and the paper's headline d = 0.47 is "
     "belief in TRUE news, not a discernment measure.",
     "human master (eligibility)"),
    ("Prike et al. (2024)", "eligible_primary", "scope_ambiguity",
     "A 2x2 design (badge x norm) with one no-intervention cell; the results "
     "are FACTOR MAIN EFFECTS (all df_num = 1, so not omnibus), and the paper "
     "prints no per-cell M/SD (means only, in figures). The human must fix "
     "ONE badge-vs-control contrast on one outcome; the paper itself says the "
     "badge is the more promising intervention.",
     "human master (eligibility)"),
    ("Heiter (2024)", "Fulltext_Exclusion_Criterion", "scope_ambiguity",
     "The thesis has 4 components with OPPOSITE answers: Pilot 2 and "
     "Experiment 2 (a Bad News inoculation against a real control) meet every "
     "criterion -> EMPTY; Pilot 1 and Experiment 1 are correlational with no "
     "arm -> C1+C2+C4. Neither the SB route (C1 C2) nor the MM route (C1) "
     "is right for the whole paper - the field is per experiment.",
     "human master (inclusion, per experiment)"),
    ("Heuett & Westerman (2014)", "Fulltext_Exclusion_Criterion",
     "scope_ambiguity",
     "The SB route's C1+C2 is the better-supported value (the MM route's C1 "
     "misses C2): the 'inoculation' is a 15-minute intercultural "
     "conversation with no false content, and the design is one-group "
     "pretest-posttest with no control arm. C4 does NOT apply.",
     "human master (inclusion)"),
    ("Hopkins et al. (2025)", "eligible_primary (Confidence in manipulation "
     "detection)", "scope_ambiguity",
     "Confidence is NOT the paper's pre-registered primary outcome "
     "(manipulation detection is); the effect is a marginal single-arm "
     "interaction (beta = 0.14, P = .047; the guide null) with no per-arm "
     "means/SDs anywhere, and one contrast per experiment may carry the flag "
     "-> 0, with the preregistered manipulation-detection contrast as the "
     "carrier. The contrast itself does qualify: Tetris is a "
     "no-misinformation placebo, so contrast_type should be 2, not 1.",
     "human master (eligibility)"),
    ("Huang & Hu (2025)", "eligible_primary (Perceived AI literacy)",
     "scope_ambiguity",
     "An interaction term can never be the pooled contrast, so the canonical "
     "0 stands for the row AS CODED - but the row should be re-coded as a "
     "Wave-1 simple-vs-control contrast, and then the cell matters: the MM "
     "route's Text/Warning cell (5.06 vs 4.86) is the WEAKEST cell, because "
     "the paper found only technique-tips/combined/video beat the control "
     "(M = 5.24 / 5.33 / 5.38 vs 4.84, all p < .05) and the warning "
     "condition was indistinguishable from control.",
     "human master (eligibility)"),
    ("Hylton-Kong et al. (2021)", "eligible_primary (Any current "
     "contraception use at follow-up)", "numeric_error",
     "0 (the SB route's value): 'any current contraception use' is explicitly "
     "a SECONDARY outcome - the LARC-adoption primary was 0 (0.0%) in both "
     "arms - and the flat null (63.6% vs 59.6%, p = .54); the only "
     "informative contrast is 'Perceives IUD to be very or mostly safe' "
     "(59.1% vs 43.6%, p = .02). SEPARATELY: the numbers filed under this DV "
     "name (27/110 vs 16/110) are Table 2's INJECTABLE row, not this one - a "
     "copy error - and a reported type of 'none' would silently drop the row "
     "from the pool.", "human master (eligibility and the filed numbers)"),
    ("Deng et al. (2025)", "effect_size_type_reported (Exp5 rows 12-13)",
     "pool_change",
     "The type said 'none', which makes the standardisation guard refuse the "
     "row's own d - the same defect class found in Bråten. The paper reports "
     "M/SD per group plus t(553) = 3.02 / -.30 (p. 21), so the type is "
     "'means'.", "already applied by the master agent"),
    ("Deng et al. (2025)", "Fulltext_Exclusion_Criterion", "scope_ambiguity",
     "The field is ABSENT from all five Deng records - a gap rather than a "
     "disagreement, so no route could flag it. Studies 1-3 deliver no "
     "intervention at all (they measure self-construal only).",
     "human master (inclusion)"),
    ("Delgado et al. (2019)", "cell_descriptives", "numeric_error",
     "Left empty on purpose: Tables 3-4 give percentages and medians/IQRs, "
     "never means/SDs, so the cells are not a genuine descriptives route. One "
     "Table 4 cell (delayed text content 80%) CONTRADICTS the paper's own "
     "count and chi2, and the MM route's version adds an invented binomial SD.",
     "human master (the paper's own inconsistency)"),
    ("Delgado et al. (2019)", "timepoint_1 / timepoint_2", "scope_ambiguity",
     "The MM route had 'pre' and 'post' REVERSED; the paper's Tables 3-4 "
     "(pp. 34-35) give pre-test vs immediate post-test, with the delayed "
     "sub-sample rows at followup_3w. The agent also relabelled all 15 DV "
     "rows' arm labels to the paper's own column names.",
     "already applied by the master agent"),
    ("Saman et al. (2025)", "Fulltext_Exclusion_Criterion", "scope_ambiguity",
     "Recommend C1 + C4 and DROP C2: the study DOES have a waitlist control "
     "arm, so the SB and MM routes' C2 is unsupported - a case where two "
     "routes agree and are both wrong. C4 is the real question, because the "
     "paper calls itself 'a post hoc subgroup analysis' of an earlier RCT "
     "(limitations, PDF p. 9) whose parent study is NOT in this corpus. C1 "
     "rests on one abstract phrase about reducing 'misinformation about OCD "
     "symptoms'.", "human master (inclusion)"),
    ("Sanchez & Dunning (2021)", "Fulltext_Exclusion_Criterion",
     "scope_ambiguity",
     "Per record: Study 4 -> C1 only (it has a non-randomised 'baseline "
     "group' of 40, so C2 does not apply); Studies 1a/1b/2/3 -> C1 + C2 + C4 "
     "(correlational individual-difference studies, no manipulation, no "
     "comparison arm).", "human master (inclusion, per experiment)"),
    ("Rutsztein et al. (2021)", "Fulltext_Exclusion_Criterion",
     "scope_ambiguity",
     "Recommend C1 + C2: the paper says outright it had 'no control group', "
     "so C2 is certain; C1 turns on whether the Programa PIA's body-image "
     "'media literacy' counts as a misinformation intervention - the word "
     "'misinformation' never appears and the only 'myth' content is "
     "healthy-eating truths/myths (p. 3).", "human master (inclusion)"),
    ("Salma Banu Nazeer Khan et al. (2024)",
     "Fulltext_Exclusion_Criterion", "scope_ambiguity",
     "Recommend C1 + C2: a single-group pretest -> 3-minute ethics training -> "
     "posttest in one sitting (no control or comparison arm anywhere; McNemar "
     "on 364 participants). C1 holds because the training is "
     "infographic-design ethics with no true/false misinformation items - the "
     "only 'misinformation' is the intro framing ('message-level deception', "
     "p. 70).", "human master (inclusion)"),
    ("Sandor (2025)", "eligible_primary", "scope_ambiguity",
     "Recommend 1 for the accuracy-nudge vs no-nudge contrast in each sharing "
     "experiment (the only misinformation intervention), and 0 for all "
     "identity-threat, interaction and moderator contrasts and for the Anger "
     "Study. Note the paper's 'Study 1 ... lacks a control condition' "
     "(footnote 18, p. 35) refers to the IDENTITY factor - the accuracy "
     "factor is properly controlled in both Ch. 4 (effect null) and Ch. 5 "
     "(discernment 0.04**, fake-sharing p = .067).",
     "human master (eligibility)"),
    ("Dugas et al. (2024)", "contrast_type (the 18 'vs placebo' rows)",
     "pool_change",
     "Set to 2 (was 1): the paper uses the PLACEBO, not the no-treatment "
     "arm, as its comparison group - 'we use the placebo group (instead of "
     "the control group) as the comparison group' (p. 21) - and the record's "
     "own control_group_type is 2 = Placebo. The agent also set "
     "Intervention_Duration = 10 minutes (p. 18) and corrected the "
     "score_types of the 9 attitude rows from 'composite' to ['single_item'].",
     "already applied by the master agent"),
    ("Drolsbach et al. (2024)", "effect_size_type_reported (the AME rows)",
     "pool_change",
     "The 8 rows carrying the paper's raw AME now read 'other', which is the "
     "vocabulary-valid code for an AME and makes the standardisation guard "
     "REFUSE them rather than pool a 0-1 AME as if it were a d. The rows "
     "holding a standardised d keep 'd'. All of them are eligible_primary = 0, "
     "so no pooled product changes.", "already applied by the master agent"),
    ("Drolsbach et al. (2024) / Dugas et al. (2024)", "DV_Metrics rows",
     "scope_ambiguity",
     "The agent routes' EXTRA DV rows cannot be added: the SB route's 18 "
     "'vs no-treatment control' contrasts for Dugas and the MM/SB subgroup "
     "AMEs for Drolsbach would each require setting eligible_primary, a "
     "human-master field. The canonical rows already match the paper's tables.",
     "human master (which rows qualify)"),
    ("Duan (2026)", "control_group_type / Fulltext_Exclusion_Criterion",
     "scope_ambiguity",
     "control_group_type: the canonical holds 6 (Exp1/Exp3) and 1 (Exp2), but "
     "Exp1/Exp3's comparison arm ALSO receives a consensus message (low base "
     "rate), so an active comparator (3) fits; Exp2's control reads no essay "
     "(1). Fulltext_Exclusion_Criterion is left ABSENT from the record - a "
     "gap, not a disagreement; the MM route's C1 is plausible, since the "
     "study manipulates social-consensus/moral-conviction frames on polarized "
     "topics and mentions misinformation only as context (Ch. 1, p. 1).",
     "human master (inclusion and control type)"),
    ("Celio & Lisman (2014)", "contrast_type (the 15 PNF-vs-Control rows)",
     "pool_change",
     "Set to 2 (was 1): the paper used 'an attention-placebo control group' "
     "(p. 4), and the canonical's contrast_type of 1 contradicted its own "
     "control_group_type = 2. The agent also made effect_favors "
     "polarity-aware across all 21 rows.", "already applied by the master "
     "agent"),
    ("Cook et al. (2017)", "effect_size_type_reported (all 90 rows)",
     "pool_change",
     "Set to 'means': the paper reports the cell means/SDs the effects are "
     "computed from (pp. 7, 12), and the old 'none' said nothing was "
     "reported, which made the standardisation guard refuse the rows' own "
     "yi/vi - the third instance of that defect class (after Bråten and "
     "Deng). The agent also set contrast_type = 8 on the 16 rows whose "
     "contrast is exactly misinformation vs no misinformation.",
     "already applied by the master agent"),
    ("Cook et al. (2017) / Chae & Tewksbury (2026) / Chae et al. (2026)",
     "ethics / consent_reported", "scope_ambiguity",
     "Three papers where the canonical carried no ethics or consent statement: "
     "Chae & Tewksbury (2026) has no ethics statement in the article or its "
     "supplement (set to 0 = No mention), and Chae et al. (2026) likewise "
     "(null -> 0, with consent left empty because it is not stated).",
     "already applied by the master agent"),
    ("Chae & Tewksbury (2026)", "Age_Mean / Age_SD (Study 2)",
     "numeric_error",
     "The supplement's Table D10 (p. 64) age row is MIS-PRINTED (M = 4.12, "
     "SD = 1.44, min 2, max 7), so Study 2's age stays empty "
     "(Age_Reported_As = 4); Study 1's is fine (42.61 / 16.26, Supp. Table "
     "D9, p. 63).", "human master (leave empty; the paper's own print "
     "error)"),
    ("Deibl et al. (2024)", "Affiliation_2 / Affiliation_3", "scope_ambiguity",
     "Both still read 'University of Salzburg' while the paper's title page "
     "(p. 1) says 'Paris Lodron University Salzburg' for Zeitlhofer, "
     "Geroldinger and Zumbach - and Affiliation_4, which WAS in the batch's "
     "item list, was corrected to that string. The three affiliations "
     "therefore now disagree with each other.",
     "master agent (affiliation strings)"),
    ("Delgado et al. (2019)", "Intervention_Duration / Feedback_Mechanics",
     "numeric_error",
     "Intervention_Duration was blank and is 385 minutes (p. 16: 'seven "
     "sessions ... Each session lasted for 50-60 minutes' -> 7 x 55; the SB "
     "route's 345 is below the paper's own minimum). Feedback_Mechanics was "
     "1 and is 2 (Elaborated) - modules 5-7 have students discuss which "
     "fictitious student is correct 'and to explain why' (p. 13).",
     "already applied by the master agent"),
    ("Dutta-Powell et al. (2024)", "yi / vi (the six Table-4 rows)",
     "numeric_error",
     "RESULT-CHANGING: the six 'perceived responsibility' rows keep yi = "
     "+0.043, +0.026, +0.007, +0.087, +0.067, +0.079, which carries the "
     "OPPOSITE sign to the paper's Table 4 coefficients (-0.06, -0.03, -0.01 "
     "for literacy; -0.12, -0.08, -0.11 for prebunking; p. 888). The "
     "magnitudes do not match the paper either, so the stored yi did not come "
     "from Table 4. On a higher-is-better measure the intervention slightly "
     "REDUCED the rating, so the record currently reads 'the intervention "
     "increased perceived responsibility'. The agent set beta_val to the "
     "paper's coefficients and effect_favors to 'control', which exposes the "
     "discrepancy rather than hiding it behind a flipped coefficient, but left "
     "yi/vi (the human master's fields) alone.",
     "human master (numeric effect field - the pooled sign)"),
    ("Ecker & Ang (2019)", "Modality", "scope_ambiguity",
     "Modality = 2 (Video) although the intervention is a written news article "
     "and retraction shown on a screen - Media_Support is now ['Text']. The "
     "two fields contradict each other; Modality was not in the batch's item "
     "list.", "human master (or the master agent)"),
    ("Dutta-Powell et al. (2024)", "effect_size_type_reported",
     "scale_question",
     "Writing the MM route's 'beta' would make the standardisation guard "
     "fail closed (beta is a raw type), so the row would pool the raw 7-point "
     "coefficient instead of the standardised d already stored. 'd' is kept "
     "and conversion_inputs.effect_type_audit.original_reported_type preserves "
     "the paper's type; no pooled number changes.",
     "already applied by the master agent"),
    ("Duzova et al. (2025)", "Instructor_Type", "scope_ambiguity",
     "3 (Peer) retained - the paper's model is peer education (five peer "
     "educators, pp. 3-4) - but session 1 was delivered by a nurse-researcher "
     "faculty member (p. 3), so 1 (Researcher) is also supported.",
     "human master (which arm)"),
    ("Ecker & Ang (2019)", "Intervention_Duration", "scope_ambiguity",
     "Kept at 1.0 although the paper states no duration; the only basis is "
     "400 ms/word (pp. 13-14).", "human master (a duration with no stated "
     "source)"),
    ("Scull et al. (2022)", "dv_polarity (2 rows)", "scale_question",
     "Both scales run 'higher = more credulous / more complete about a "
     "MANIPULATIVE alcohol advert' (completeness is '1 = incomplete; 4 = "
     "complete', p. 714), and the paper's rubric counts 'missing "
     "information' as a category (p. 714) - so higher = worse -> 0. With the "
     "canonical's 1 the paper's own negative coefficients (-0.07, -0.08) would "
     "read as the intervention making students MORE credulous.",
     "human master (dv_polarity)"),
    ("Scull et al. (2021)", "eligible_primary (Willingness for unprotected "
     "sex, males only)", "numeric_error",
     "Table 5 (p. 157) prints this MALES-ONLY row as the only effect for the "
     "outcome, but there is no main effect and no interaction F/df/p "
     "anywhere, and the printed df_den = 271 cannot belong to the 168 males "
     "in Table 1 (81 control / 87 intervention, p. 151) - it implies N ~ 276. "
     "If set to 1, the pool would take the paper's d = 0.32 with a variance "
     "computed from the FULL-arm Ns (166/165): a subgroup effect weighted as "
     "if it came from 331 students. Counter-case: it is a genuine "
     "between-arm contrast with printed F/d/p, and every other row of this "
     "experiment carries 1.", "human master (eligibility)"),
    ("Scull et al. (2018)", "eligible_primary / dv_polarity (Attitudes about "
     "teen sexual activity)", "scope_ambiguity",
     "The paper REMOVED the scale before any analysis ('removed from the "
     "analyses due to low reliability in the measure (alpha = 0.59)', p. "
     "1059; Table 4 prints only significant findings), so no contrast exists "
     "at all -> eligible_primary = 0, not 1. For polarity the paper's own "
     "hypothesis settles it: the intervention should produce 'decreased "
     "favorable attitudes and normative beliefs about teen sex' (p. 1052) and "
     "the sample item is 'I believe it is OK for teens to be sexually active.' "
     "(p. 1056) -> higher = worse = 0 (the canonical's 1 is the outlier).",
     "human master (eligibility and polarity)"),
    ("Schmid & Betsch (2022)", "eligible_primary (Credibility of misleading "
     "pro-vaccine headline)", "scale_question",
     "Recommend 0 (canonical and SB already 0; the MM route has 1). The "
     "paper calls a DIFFERENT DV the primary outcome variable ('This item "
     "served as the primary outcome variable', p. 540) and files this one "
     "under an explicitly EXPLORATORY 'unintended effects' question (pp. "
     "536-537) whose immediate-posttest effect is a HARM - the debunking "
     "made students MORE believing of a second misleading claim. Flipping it "
     "to 1 would drop q_yi = -3.77 (raw 0-100 POMP, vi = 1.614) into the "
     "immediate-posttest credibility pool beside the confirmatory row "
     "standardised to yi = -0.27061: a harm-direction, unstandardised row "
     "from the same experiment.", "human master (eligibility)"),
    ("Sheikh et al. (2017)", "Fulltext_Exclusion_Criterion", "scope_ambiguity",
     "Recommend C2 (canonical and MM); the SB route's extra C1 is the open "
     "half. C2 is certain from the paper's own words: 'The RUDifferent? "
     "programme was not formally designed with \"control\" schools in mind, "
     "therefore we were unable to reliably measure the impact of schools "
     "receiving no interventions' (p. 535) - all 57 schools got the "
     "programme. C1 is a judgement the paper does not settle: the programme "
     "corrects a statistical misperception about peer smoking ('pluralistic "
     "ignorance', pp. 531, 535) with no discernment outcome. ALSO unclaimed "
     "by any route: the paper describes no randomisation at all, so C4 is "
     "arguable.", "human master (inclusion)"),
    ("26 study keys (Turetsky & Sanderson (2018), Yager & O'Dea (2010), "
     "Scull et al. (2018 a), Ridolfi & Vander Wal (2008), Golan et al. "
     "(2013), Saman et al. (2025), Hunze et al. (2025), Sundgot-Borgen et al. "
     "(2019), Bender et al. (2023) and 16 more)",
     "eligible_primary / Fulltext_Exclusion_Criterion", "pool_change",
     "RESULT-CHANGING, CORPUS-WIDE: 386 POOLED EFFECTS come from 26 study "
     "keys that the coding itself marks as excluded (381 under C1, 24 under "
     "C4, 5 under C2). The meta-analysis pools on eligible_primary == 1 and "
     "never reads Fulltext_Exclusion_Criterion, so a study the coding has "
     "excluded still contributes its effects unless its key is listed by hand "
     "in Scripts/study_exclusions.py. Turetsky & Sanderson (2018) is the "
     "clearest case: a mental-health-stigma norms experiment with no "
     "misinformation content, criterion ['C1'], 34 eligible rows, 30 of them "
     "pooled with an effect, and the key is not in the exclusion list. The "
     "mechanism exists and works - NO_INTERVENTION_EXCLUSIONS is keyed by "
     "(study_key, experiment) - it just does not list these 26 keys. The full "
     "list is Results/excluded_but_pooled.csv.",
     "CAMPAIGN OWNER (this changes the pooled result)"),
    ("Vraga et al. (2022)", "dv_polarity (post-credibility rows)",
     "scale_question",
     "The canonical coding is internally inconsistent in two places the human "
     "should settle at once: 'correction vs pure control' is eligible_primary "
     "= 1 for the misperception rows but 0 for the credibility/SPNL/VNL rows, "
     "and the post-credibility polarity is 0 on one row but 1 on the other "
     "five - which leaves the NL x credibility row pooled with a NEGATIVE "
     "sign (the NL message reading as harmful) even though the paper reads "
     "lower credibility of the misinformation post as the desirable "
     "direction. Four eligible_primary items and two polarity items.",
     "human master (eligibility and polarity)"),
    ("Vraga et al. (2019)", "eligible_primary", "scope_ambiguity",
     "Only the two change-in-issue-attitudes contrasts (logic-vs-"
     "misinformation-only, humor-vs-misinformation-only) have per-condition "
     "statistics; the credibility-of-the-misinformation-tweet effect exists "
     "only as a null 3-level omnibus F(2,395) = .34 (already refused by the "
     "omnibus guard), the reply-tweet contrast has no control arm, and the "
     "paper's own effect is topic-conditional (HPV only) - so the human must "
     "decide which of the two qualifying contrasts is 'the one'.",
     "human master (eligibility)"),
    ("Truman & Elliott (2020)", "Fulltext_Exclusion_Criterion",
     "scope_ambiguity",
     "A single-group pre/post food-marketing media-literacy evaluation - the "
     "words *control*, *misinformation* and *news* never occur in it - so "
     "['C1','C2'], with C1 the only arguable one (one question does teach "
     "truthfulness of marketing claims).", "human master (inclusion)"),
    ("Valeria Edelsztein et al. (2025)", "Fulltext_Exclusion_Criterion",
     "scope_ambiguity",
     "C2 is certain (46 students, one class, no control, no pre/post - the "
     "authors say so); the C1 call is a scope judgement: the paper explicitly "
     "frames itself as 'active inoculation' borrowed from the fake-news "
     "literature but insists astrology's claims are unfalsifiable rather "
     "than false. Either way the study contributes no contrast (Fully_Within, "
     "eligible_primary = 0).", "human master (inclusion)"),
    ("Shelby M. Cagle et al. (2025)", "eligible_primary (all 22 rows)",
     "scope_ambiguity",
     "The paper has NO control arm at all ('a one group, pre-test/post-test "
     "... within-subjects design', p. 9; n = 15; the authors name the missing "
     "control group as a limitation, pp. 22-23), so no row of the 22 can be "
     "the intervention-vs-control contrast - the MM route's "
     "eligible_primary = 1 on all 22 is unsupported; the answer is 0 on every "
     "row.", "human master (eligibility)"),
    ("Stiff & Cutts (2024)", "Fulltext_Exclusion_Criterion",
     "scope_ambiguity",
     "Two experiments, two different answers: Study 1 (N = 192) is pre/post "
     "with everyone exposed -> no control (C2 unmet); Study 2 (N = 301) is "
     "randomised intervention vs control -> C2 met. The human must decide the "
     "C1 call (body-image target, but the media-literacy slides do teach "
     "image veracity, and the screen excluded on C1: 'targets body "
     "image/eating/appearance rather than misinformation susceptibility') - "
     "recommend ['C1','C2'] for Exp1 and ['C1'] for Exp2.",
     "human master (inclusion, per experiment)"),
    ("Sheng et al. (2021)", "Fulltext_Exclusion_Criterion", "scope_ambiguity",
     "A 4-page invited COMMENTARY (the article type is printed on the paper; "
     "no Methods, Results or data) -> C4 is certain; and master_dataset.xlsx "
     "already records this record's full-text reason as 'Not a primary "
     "experimental study (C4)', so recommend ['C4']. The SB/MM value "
     "['C2','C4'] is literally true but breaks the one-reason-per-record "
     "PRISMA convention.", "human master (inclusion)"),
    ("Sousa & Costa (2019)", "Fulltext_Exclusion_Criterion",
     "scope_ambiguity",
     "A single-arm pre/post of 21 teachers (Wilcoxon, Table 3) -> C2 is "
     "certain, and the screen records 'No control/comparison group (C2)' with "
     "C1 = Yes, so recommend ['C2']. The SB route's C1 has real support (the "
     "word 'misinformation' never appears; all 19 items are media-PRODUCTION "
     "skills), so ['C1','C2'] is the override if C1 must mean 'targets "
     "misinformation'. eligible_primary = 0 on all 19 rows (no second arm).",
     "human master (inclusion)"),
    ("Fendt & Müller (2026)", "Fulltext_Exclusion_Criterion",
     "scope_ambiguity",
     "The record has NO Fulltext_Exclusion_Criterion key at all, and the "
     "paper is German, so C3 (not English) appears to apply. The field is the "
     "human master's, so the agent left it untouched.",
     "human master (inclusion)"),
    ("Fendt et al. (2026 x)", "the key's paper vs the key's dataset",
     "scope_ambiguity",
     "PAPER != DATASET. `Papers/Fendt et al. (2026 x).pdf` is 'Analytic "
     "Thinking x Lateral Reading' - a PREREGISTRATION with one planned "
     "experiment (N = 800) and no results - while `Datasets/Fendt et al. "
     "(2026 x)/` holds a DIFFERENT project ('Analytic Reflection and Morality "
     "Framing': two preregistrations, two data files, two R scripts, "
     "codebook_morality.xlsx) whose two studies the canonical route had "
     "coded under this key and which the reproduction and the pipeline treat "
     "as this key's data. The master agent added the paper's own experiment "
     "as record 0 (10 eligible rows, none carrying an effect number, so they "
     "cannot pool) and left the two Morality records intact. So the effects "
     "being pooled for this key come from a study the key's paper does not "
     "report. If the key should mean the ATLR paper only, the two Morality "
     "records need a key of their own.",
     "CAMPAIGN OWNER (changes what is pooled)"),
    ("Feuerstein (2002)", "Discernment_vs_General_Skepticism",
     "scope_ambiguity",
     "Changed from 1 to null: the outcomes are media-analysis test scores "
     "(pp. 184-185, 193-194) and no true/false belief contrast exists, so "
     "neither selective discernment nor induced general skepticism is "
     "measurable. Contrasting_Cases went 1 -> 0 for the same reason (no "
     "manipulative-vs-neutral example pairing).",
     "already applied by the master agent"),
    ("Fendt et al. (2026 b)", "the two records hold each other's data",
     "numeric_error",
     "RECORD SWAP: `Fendt2026_Friction_Exp1` carries the paper's EXPERIMENT 2 "
     "cells (153/157/158/159; N_Analyzed 627 against the paper's 630) and "
     "`Fendt2026_Friction_Exp2` carries EXPERIMENT 1's cells (115/125/140/257; "
     "N_Analyzed 637). The agent assigned the per-record demographics by "
     "Study_ID name (Exp1 <- paper's Experiment 1, Exp2 <- Experiment 2), which "
     "matches both agent routes and the harmonizer's artifact naming "
     "(artifact 'Exp1' = 637 rows, age M 21.93) - but the DV rows' record-"
     "experiment mismatch still needs a decision.",
     "human master (which record holds which experiment)"),
    ("Fendt et al. (2025)", "n_arms", "scope_ambiguity",
     "n_arms is on the master agent's do-not-touch list, but the worklist "
     "assigned it as a pool item and the split classes it as paper-settled, so "
     "the agent settled it from the paper (2: at least one treatment and one "
     "control condition, p. 6; treatment-control synthesis, p. 8).",
     "human master (the do-not-touch list and the worklist disagree)"),
    ("Fendt et al. (2023)", "Rationale_Notes", "stale_note",
     "The note claims the sandbox text/PDF was Collins (1988) 'Cognitive "
     "Apprenticeship and Instructional Technology' (it is now the real paper) "
     "and states 'Human_Trainer=4' (now 3). "
     "Scripts/check_rationale_consistency.py flags the Educational_Format=5 "
     "prose for this key.", "master agent (prose refresh)"),
    ("Fendt et al. (2026 d)", "N_Analyzed / Age_Mean / Age_SD",
     "numeric_error",
     "The canonical holds N_Analyzed 677 and age 33.71 / 15.71 against the "
     "paper's 674 and 33.75 / 15.75 (p. 4) - the agent's N_Recruited 703 is "
     "the deposited complete-record count before the honesty-check exclusion.",
     "human master (numeric effect fields)"),
    ("Dutta-Powell et al. (2024)", "Tips_Content_Checklist", "scope_ambiguity",
     "The literacy arm is three screens (Appendix A, p. 894): the definition of "
     "greenwashing, the two practices, and one description + mock-up ad each. "
     "No checklist tip is named, so the canonical "
     "['check_evidence_and_references','consider_the_source'] is not literally "
     "supported; the record's own Intervention_Type_Detail says the paper is a "
     "greenwashing-literacy study. The agent kept the canonical value because "
     "the paper supports neither route's value - a possible [] is worth "
     "considering.", "human master (the tips list)"),
    ("Sánchez & Sandoval (2012)", "Fulltext_Exclusion_Criterion",
     "scope_ambiguity",
     "ALL FOUR criteria are supported by the paper: C3 (the body is entirely "
     "Spanish), C2 (the authors' own 'diseño experimental de un solo grupo "
     "con pretest y postest', p. 115), C4 (Sect. 2 is a METHOD article - "
     "Tables 1-4 are rubrics defining levels and there is no inferential test "
     "anywhere), and C1 (no misinformation content at all). Only the MM route "
     "dissents, omitting C1 and C4. SEPARATELY, the routes also disagree on "
     "the NUMBER of experiments (canonical 1, SB/MM 2) over a paper "
     "describing three country experiences under one shared model.",
     "human master (inclusion, and the record count)"),
    ("Tran Mai Chi Nguyen & Chei Sian Lee (2026)",
     "Fulltext_Exclusion_Criterion", "scope_ambiguity",
     "None of the three route values is right, and the MM route's blank should "
     "be rejected: the authors state that a FUTURE study would need "
     "experimental methods (p. 1074), so C4 is certain, and all 300 "
     "participants rated all six vignettes with no allocation, so C2 also "
     "applies. Recommend C4 + C2.", "human master (inclusion)"),
    ("Szmigielska (2018)", "Fulltext_Exclusion_Criterion", "scope_ambiguity",
     "C2 is certain (the author states 'no control group or follow-up', p. 13), "
     "but C1 is a scope call the paper cannot settle: the media-literacy "
     "section is about airbrushed body ideals (p. 73) and the 231-page thesis "
     "contains ZERO hits for misinformation / disinformation / veracity / "
     "credibility - so C1 hinges on whether the campaign's media_literacy_tips "
     "family covers body-image media literacy.",
     "human master (inclusion)"),
    ("Tabor (2025)", "dv_polarity", "scale_question",
     "The paper's own framing decides it: 0 (higher is worse) for "
     "tough-on-crime support - the author HYPOTHESISES decreased support "
     "(p. 97) and calls the observed increase a 'backfire effect' and "
     "'potentially harmful' (pp. 91, 102). SEPARATELY, the canonical file is "
     "internally inconsistent on dv_polarity across its own two DVs: the "
     "sibling 'Perceived risk' DV is 0 on the first row and 1 on the other "
     "two.", "human master (dv_polarity, both DVs)"),
    ("Tiemann et al. (2021)", "Fulltext_Exclusion_Criterion",
     "scope_ambiguity",
     "C2 + C4 (canonical and MM). C2 is certain (nothing is manipulated; "
     "routine post-session self-report only) and C4 is supported - the paper "
     "frames itself as an EVALUATION of a nationwide programme, with no "
     "manipulation and no misinformation outcome measured. C1 is correctly "
     "left unset (the training covers disinformation, p. 399).",
     "human master (inclusion)"),
    ("Liao et al. (2016)", "eligible_primary (a spurious queue row)",
     "scope_ambiguity",
     "NOT A REAL DISAGREEMENT, and its value is not even stable across runs: "
     "the canonical route's dv_names EMBED the contrast, so the canonical "
     "route is never compared at all, and SB and MM list the same seven names "
     "in different orders, so the script pairs SB's omnibus row against MM's "
     "A-vs-C row. Read per contrast, the paper supports A vs C (the "
     "no-treatment control) = 1 and A vs B / the omnibus / the J-N rows = 0. "
     "SB's oddity is marking *nutritional knowledge* A-vs-C as 0 while "
     "marking the other six as 1 - the paper reports no such distinction.",
     "human master (the item is spurious; the real per-contrast call)"),
    ("M. Scull et al. (2019)", "eligible_primary (39 rows)", "scope_ambiguity",
     "There is exactly ONE contrast in the study (Media Aware Parent vs a "
     "CDC-brochure active control), so the human's real choice is which "
     "outcomes carry it. The canonical marks ALL 39 rows 0, so Scull currently "
     "contributes nothing; the SB route picks 3 rows and the MM route 16. Only "
     "~9 outcomes have a reported standardised d (CI), and the four "
     "media-mediation rows in dispute have no d at all.",
     "human master (eligibility)"),
    ("Levy et al. (2025)", "dv_polarity (Negative feelings about climate "
     "change)", "scale_question",
     "Both items are the same DV, 'Negative feelings about climate change' "
     "(0 = not at all to 5 = extremely, across nine negative emotions) -> "
     "higher = worse = 0. The intervention LOWERED the score and the paper "
     "presents that as a benefit, so the canonical's 1 would flip the sign of "
     "a beneficial effect.", "human master (dv_polarity)"),
    ("Lee et al. (2025)", "eligible_primary", "scope_ambiguity",
     "The queue's conflict is a ROW-ALIGNMENT ARTEFACT (the canonical has one "
     "row under that name, the SB route two). Matched contrast by contrast the "
     "routes agree: Study 1 has NO control group (0) and Study 2 is a proper "
     "RCT (1); the real outlier is the MM route, which marks Study 1's "
     "single-arm rows as eligible. ALSO: the SB route's 'using search engines' "
     "row is marked 1 although that skill was never taught (a specificity "
     "check -> 0), and the canonical's Study 2 skill rows carry "
     "timepoint_1='post' / timepoint_2='pre', which routes them into the DiD "
     "pool rather than the posttest pool.",
     "human master (eligibility, and the pool the rows land in)"),
    ("Leder et al. (2024)", "eligible_primary (Experiments 5 and 6)",
     "scope_ambiguity",
     "The dispute is confined to Experiments 5 and 6, which the paper says "
     "collected data ONLY with the feedback tool active - no control arm - so "
     "their within-person pre->post rows are not an "
     "intervention-vs-control contrast. The canonical's 0 is right and the SB "
     "route's 1 is wrong; setting 1 would also trip the pipeline's "
     "fully-within rule, so it adds nothing to the pool.",
     "human master (eligibility)"),
    ("Austin et al. (2015)", "re-analysis of studies already in the corpus",
     "duplicate_trial",
     "This paper RE-ANALYSES Pinkleton et al. (2012) and (2013), which are "
     "already included studies in the corpus as separate records - a "
     "duplicate-trial decision, not an eligible_primary one. The N-based "
     "detector already pairs Austin2015_Exp1 with Pinkleton2012_Exp1 (N = 922) "
     "in Results/possible_duplicate_trials.csv. The related eligible_primary "
     "question: canonical codes NO change row, while the SB and MM routes do, "
     "and Table 1 (p. 410) reports the pretest cell means, so the pre->post "
     "DiD is derivable.",
     "human master (duplicate trial, and whether a DiD row is added)"),
    ("Ballout et al. (2026)", "Fulltext_Exclusion_Criterion",
     "scope_ambiguity",
     "C4 is certain (a PRISMA-ScR scoping review with no experiment of its "
     "own), but C1 and C2 are BOTH genuinely arguable: C1 because the review "
     "is explicitly framed as countering VACCINE MISINFORMATION, and C2 "
     "because the paper has no arms of its own but its PICOS comparators are "
     "real control groups in the 12 included studies. The three route values "
     "(canonical ['C2','C4'], SB ['C1','C2','C4'], MM ['C4']) each pick a "
     "different subset.", "human master (inclusion)"),
    ("Banerjee & Greene (2007)", "eligible_primary (x2)", "scope_ambiguity",
     "Both items are the same A+P-vs-control Time 4 row. The SB route alone "
     "flags A+A and zeroes A+P, while the paper's own headline result is the "
     "A+P-vs-control comparison at the delayed posttest; and because the "
     "control arm was measured ONLY at Time 1 and Time 4, the delayed posttest "
     "is the sole between-arm point and belongs in the persistence pool.",
     "human master (eligibility)"),
    ("Bennett (2020)", "eligible_primary (which outcomes carry it)",
     "scope_ambiguity",
     "The difference is only WHICH outcomes carry the flag (canonical: 6 "
     "media-literacy rows; MM: all 90) - and the flag is moot for pooling, "
     "because no route coded a numeric yi for any Bennett row. This is also "
     "not a misinformation study (the canonical already flagged C1).",
     "human master (eligibility)"),
    ("Bermejo-Berros (2021)", "Fulltext_Exclusion_Criterion",
     "scope_ambiguity",
     "Recommend ['C1','C3']: C1 is clear (a narrative/media-competence "
     "training with no misinformation component), and the article's BODY is in "
     "SPANISH - only the title, abstract and keywords are English - so the "
     "human's call is whether a Spanish body with an English abstract counts as "
     "'not English'. C2 and C4 do not apply.", "human master (inclusion)"),
    # --- Cho et al. (2020) / Cheng et al. (2026) / Chen et al. (2015)
    ("Cho et al. (2020) / Cheng et al. (2026) / Chen et al. (2015)",
     "effect_size_type_reported (17 rows)", "pool_change",
     "A mis-coded reported type silently DROPS rows from the pool. Ten Cho et al. "
     "(2020) mediator rows (DV[6]-DV[15]) carried 'none' although Table 2 reports "
     "the cell means/SDs - they now carry 'means'. Five Chen et al. (2015) rows "
     "carried 'none': DV[0]/DV[1] now carry 'means' (Table 1's cell means/SDs) and "
     "DV[2]-DV[4] now carry 'd' (the standardised d each row already holds, "
     "converted from the reported F(1, 129)). The numeric fields were left "
     "untouched; the post-test and DiD pools move accordingly.",
     "already applied by the master agent"),
    ("Cho et al. (2020) / Cheng et al. (2026)", "contrast_type (the rows the "
     "contrast/control check flags)", "scope_ambiguity",
     "Scripts/check_contrast_control_consistency.py flags every row whose "
     "contrast_type disagrees with the record's control_group_type, including rows "
     "that compare TWO INTERVENTION ARMS, where the flag is a false positive. In "
     "Cho et al. (2020) DV[2] and DV[5] are Table 1's 'Argument vs. story' rows, and "
     "in Cheng et al. (2026) eleven rows are 'Jiupai News vs. People's Daily' "
     "(non-authoritative vs. authoritative fact-checker label); both are "
     "active-comparator contrasts, so contrast_type = 2 already says what the paper "
     "says and was LEFT UNCHANGED (every one of those rows is eligible_primary = 0). "
     "Only the genuine case was fixed: Chen et al. (2015) DV[0]/DV[1] compared the "
     "intervention with a control that read 'a message that contained irrelevant "
     "information', so contrast_type moved 1 -> 2 (placebo) to match "
     "control_group_type = 2.",
     "master agent (paper label kept; refine the check to skip two-intervention-arm rows)"),
    # --- Cho et al al. (2023)
    ("Cho et al. (2023)", "whole record / Study_ID", "scope_ambiguity",
     "THE CANONICAL FILE DESCRIBES A DIFFERENT PAPER. Papers/Cho et al. (2023).pdf "
     "and Text/Cho et al. (2023).txt now hold the REAL Cho et al. (2023) ('Social "
     "processes of participatory engagement effects', J Appl Commun Res 2023; 51(3): "
     "320-339, doi 10.1080/00909882.2022.2147402; the mis-filed Litt et al. (2015) "
     "PDF was archived to archive/source_mismatch_20261019/ on 2026-10-19), but "
     "Extraction/Cho et al. (2023).json still holds the Litt et al. (2015) record "
     "(Study_ID = Litt2015_Exp1, DOI 10.1037/adb0000122, N_Recruited 1083, "
     "Fulltext_Exclusion_Criterion = ['C1','C2','C4']). The master agent did NOT "
     "write Cho facts into that file: a field-level edit would create a hybrid of "
     "two papers, and a full re-code needs the human master's fields (eligible_primary, "
     "dv_polarity, n_arms, control_group_type, Fully_Within, "
     "Fulltext_Exclusion_Criterion) and the numeric effect fields, which a master "
     "agent may not touch. THE RE-CODE SPEC IS ALREADY WRITTEN: Results/retrieval/"
     "Cho et al. (2023) - retrieval - pass 2.md (banking notes B1-B10) and "
     "Results/retrieval/Cho et al. (2023) - retrieval.md (section 7, "
     "recommendations 1-4), with the 7 standardised cells in Datasets/Cho et al. "
     "(2023)/Cho et al. (2023) - retrieved_dvs - retrieval.csv (N = 331 = the two "
     "message-generation arms of the parent 3-arm N = 518 trial, 232/99 derived, "
     "every row eligible_primary = 0, n_arms = 2, Fully_Within = True, "
     "Fulltext_Exclusion_Criterion = ['C2'], attitude-toward-tanning DV "
     "higher_is_worse). Also: Cho et al. (2018), (2020) and (2023) are nested "
     "subsets of ONE trial family, so only 2018/2020 carry the control contrast and "
     "the three must be de-duplicated before pooling.",
     "human master (record structure: re-file the Litt record and re-code Cho et al. (2023))"),
    ("O'Rourke (2019)", "the key's coding is for a DIFFERENT paper",
     "scope_ambiguity",
     "THE CODING IS FOR ANOTHER PAPER. `Papers/O'Rourke (2019).pdf` is "
     "Victoria O'Rourke's 2019 QUB EdD thesis, 'Exploring the impact of a "
     "media literacy intervention on children's advertising literacy, "
     "materialism and wellbeing'. The record instead carries Rozendaal, Buijs "
     "& van Reijmersdal (2016), 'Strengthening Children's Advertising "
     "Defenses...', Frontiers in Psychology, doi 10.3389/fpsyg.2016.01186 - the "
     "wrong source that was archived earlier. The PAPER was repaired; the "
     "CODING never was. CONSEQUENCE: one eligible row with a pooled effect "
     "(yi = 0.8428571, 'Skepticism toward advertising') sits in the pool "
     "under a key whose paper does not report it.",
     "CAMPAIGN OWNER (a pooled effect comes from a paper not in the corpus "
     "under this key)"),
]








def main():
    os.makedirs(RESULTS, exist_ok=True)
    with open(OUT, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        for row in FLAGS:
            w.writerow(dict(zip(COLS, row)))
    kinds = {}
    for r in FLAGS:
        kinds[r[2]] = kinds.get(r[2], 0) + 1
    print("master-agent flags: %d" % len(FLAGS))
    for k, v in sorted(kinds.items()):
        print("   %-18s %d" % (k, v))
    print("\nwrote", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
