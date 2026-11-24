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
    # --- the sign of a pooled effect, checked against the row's own label ---
    ("(corpus-wide)", "yi / vi: the sign of the stored effect vs "
     "effect_favors and beta_val",
     "numeric_error",
     "A SIGN ERROR flips a pooled effect, and two master agents found the "
     "same class in different studies: Dutta-Powell et al. (2024) stored the "
     "ABSOLUTE value of the paper's coefficient, and Kankova & Matthes (2026) "
     "stored d = +b/SD for seven rows whose contrast is *control minus "
     "intervention*.  The pipeline orients an effect by the row's polarity "
     "(contract.polarity_sign), so the oriented effect is yi * polarity_sign: "
     "positive means group 1 is favoured.  "
     "Scripts/check_effect_sign_consistency.py lists every row whose "
     "effect_favors contradicts that sign - 1,196 rows over 198 study keys, "
     "529 of them eligible_primary = 1.  Most of those are a question of the "
     "CONVENTION (whether yi is stored group1 - group2 or the other way "
     "round), which the master agents settle key by key from the paper.  A "
     "harder subset of 9 rows over 3 keys carries the paper's own "
     "coefficient in beta_val with the OPPOSITE sign to the stored effect, "
     "which cannot be a convention question: Orosz et al. (2024 a) (1 row), "
     "Pretus et al. (2024) (6 rows) and Spiridonova et al. (2025) (2 rows).  "
     "Those 9 are being settled against the papers.",
     "the master agent, then the human master for the numbers"),
    # --- the composite rule: a family-level decision, not a per-key one ---
    ("(corpus-wide)", "score_types: 'composite' on a multi-item average",
     "scale_question",
     "A FAMILY RULE, not a per-key question, and it decides which pool a row "
     "lands in.  Many papers report an outcome as the AVERAGE of several "
     "Likert items (Hu et al. (2023): 'the perceived credibility was based on "
     "the average rating on a five-point Likert scale').  The coding can "
     "call that a 'composite' (the row then stays in the primary pool) or "
     "leave it as a 'continuous_accuracy' single measure (the row is "
     "re-filed into the components strand).  The two are not the same "
     "analysis: the composite pools a sum-of-items, the component pools "
     "the item that carries the signal.  The master agents have been "
     "settling this per key - 'the corpus pools multi-item composites this "
     "way' - and the choice moves rows between the primary pool and the "
     "components strand, so it has to be one rule for the corpus, applied "
     "the same way everywhere.  The decision: does a reported per-item "
     "AVERAGE count as a composite measure (row stays in the primary pool) "
     "or as a continuous accuracy measure (row moves to the components "
     "strand)?",
     "CAMPAIGN OWNER (a family rule; it moves rows between pools)"),
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
    ("Kaňková & Matthes (2026)", "yi / vi / d_val (7 W1/W2 simple effects)",
     "numeric_error",
     "SIGN ERROR IN SEVEN POOLED ROWS.  The paper's W1/W2 emmeans contrasts "
     "are reported as CONTROL MINUS INTERVENTION (Sect. 4.1, p. 7-8: "
     "'EMMintervention = 1.84 ... vs. EMMcontrol = 2.45; b = 0.62'), so the "
     "schema's group1-group2 direction (group1 = intervention present, "
     "group2 = no intervention) needs d = -b/SD_overall.  The canonical route "
     "applied that negation on DV rows 20-23 and 29 but stored d = +b/SD on "
     "rows 24, 25, 26, 28, 30, 31 and 33, so those seven rows carry the "
     "OPPOSITE sign to the paper (row 24, MC W2: b = 0.02 = EMMcontrol 2.13 - "
     "EMMintervention 2.11, so intervention-minus-control = -0.015, not "
     "+0.015; row 25, ST W2: b = -0.09, so +0.076, not -0.076; row 26, PSR "
     "W2: b = -0.05, so +0.046, not -0.046; rows 28/30/31, overgeneralized W1: "
     "b = 0.23 / 0.09 / 0.13 -> -0.177 / -0.083 / -0.173, all currently "
     "positive; row 33, ST W2: b = 0.18 -> -0.153, currently positive).  Six "
     "of the seven are non-significant (all ps > .05), so the pooled impact is "
     "small, but the sign is wrong.  The agent set effect_favors on those rows "
     "to the paper's direction ('group1' on rows 24, 28, 30, 31, 33; rows 25 "
     "and 26 stay 'group2'), so the direction metadata now follows the paper "
     "while yi/d_val still carry the old sign.  yi/vi/d_val are on the "
     "agent's do-not-touch list: re-derive them as d = -b/SD_overall with "
     "Table 1's overall SDs (MC 1.30, BI 0.75, ST 1.18, PSR 1.09) and "
     "sei = SE_b/SD_overall.",
     "human master (numeric effect fields)"),
    ("Kaňková & Matthes (2026)", "Rationale_Notes (N_Inference_Method)",
     "stale_note",
     "The note says the per-condition n's are 'inferred by equal split "
     "(N_Inference_Method=2)' while the record carries 3 ('Other (specify in "
     "Rationale_Notes)') - the inference is an equal split WITHIN each of the "
     "three message conditions (562 -> ~187 -> 94/94 at W1, 370 -> ~123 -> "
     "62/62 at W2), not an equal split of the total, and both agent routes "
     "code 3.  check_rationale_consistency.py flags the key for it; the "
     "parenthetical is the stale part.",
     "master agent (prose refresh)"),
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
     "RESULT-CHANGING, NOW APPLIED (2026-10-21, decision D5): the six "
     "'perceived responsibility' rows kept yi = "
     "+0.043, +0.026, +0.007, +0.087, +0.067, +0.079, which carries the "
     "OPPOSITE sign to the paper's Table 4 coefficients (-0.06, -0.03, -0.01 "
     "for literacy; -0.12, -0.08, -0.11 for prebunking; p. 889). The "
     "magnitudes DID come from Table 4: the standardiser had taken the "
     "absolute value of the coefficient, which flipped the sign. The authors "
     "read the result as NULL - their third hypothesis is rejected: 'the "
     "interventions did not change consumers' attitudes about perceived "
     "responsibility to mitigate climate change' (p. 889). The six rows were "
     "re-coded from the table's own coefficients with the derivation written "
     "into conversion_method, so the pooled contribution moves from "
     "+0.0515 [+0.0111, +0.0919] to -0.0515 [-0.0919, -0.0111].",
     "closed (the owner decided; the rows are re-coded)"),
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
    ("Dutta-Powell et al. (2024)", "DV_Metrics (the third / non-greenwashed ad)",
     "scope_ambiguity",
     "The paper reports the intervention effect on the THIRD ad - which makes no "
     "environmental claim - only graphically (Figure 3, p. 887) and directionally "
     "('the green credential rating of the third ad ... is lower in both "
     "intervention groups than the control', Sect. 4.2, p. 887), with no "
     "coefficient, mean, SD or cell n, so no effect size can be extracted "
     "without inventing numbers. The canonical route codes NO DV row for that "
     "contrast; the SB route codes one row per arm with eligible_primary = 0 "
     "and the MM route one row per arm with eligible_primary = 1 and "
     "effect_size_type_reported = 'none'. The agent did not add the rows "
     "(eligible_primary is the human master's field) and removed nothing, so "
     "the contrast is reported but uncoded: its eligibility is the human "
     "master's call.",
     "human master (which contrast is eligible)"),
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
     "CLOSED (2026-10-21, decision D3): the owner decided to exclude all of "
     "them. The list was recomputed PER RECORD (the criterion is recorded per "
     "experiment), which gives 25 records over 24 keys and 381 pooled "
     "effects - two keys of the 26 drop out because their criterion sits on a "
     "record that contributes nothing (Modirrousta-Galian's Supplementary "
     "record; Stiff & Cutts' Exp1). The entries are in "
     "Scripts/study_exclusions.py under CODING_EXCLUDED_STUDIES; the studies "
     "themselves stay coded, harmonised and reproduced."),
    ("Vraga et al. (2022)", "dv_polarity (post-credibility rows)",
     "scale_question",
     "NOW APPLIED (2026-10-21, decision D6). The canonical coding was "
     "internally inconsistent in two places: 'correction vs pure control' was "
     "eligible_primary = 1 for the misperception rows but 0 for the "
     "credibility/SPNL/VNL rows, and the post-credibility polarity was 0 on "
     "one row but 1 on the other five - which left the NL x credibility row "
     "pooled with a NEGATIVE sign (the NL message reading as harmful) even "
     "though the paper reads lower credibility of the misinformation post as "
     "the desirable direction. Both were re-derived from the paper: the "
     "post-credibility polarity is 0 on all six rows (p. 2357 H3; p. 2362 "
     "'seen as least credible when it was immediately corrected'), and the "
     "eligible row is the 'misinformation + correction vs misinformation-only' "
     "contrast on raw milk misperceptions - the only arm pair that differs by "
     "the intervention alone, and the paper's own primary outcome (p. 2362). "
     "The NL x credibility row's oriented effect flips from -0.132 to +0.132.",
     "closed (the owner decided; both fields are fixed)"),
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
     "['check_evidence_and_references','consider_the_source'] was not literally "
     "supported; the record's own Intervention_Type_Detail says the paper is a "
     "greenwashing-literacy study. The agent has now set the list to []: the "
     "lesson teaches the two greenwashing TACTICS (recorded in the inoculation "
     "module's Manipulation_Strategy), and none of them is a tip of the "
     "controlled checklist, so the paper-supported subset of tips is empty.",
     "already applied by the master agent"),
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
    # --- from the master-agent pass over batch 021_pool
    ("Capraro & Celadin (2023)", "Age_Reported_As / Gender_Reported_As",
     "stale_note",
     "The agent filled Age_Mean / Age_SD / Gender_Ratio for all four records "
     "from the authors' own released data (Datasets/Capraro & Celadin (2023)/"
     "... - harmonized - SB.csv, the analysed samples the supplement prints: "
     "S1 198/198/197, S2 197/175/186, S3 186/180/184, S4 186/186 - the "
     "authors' do-file maps Gender 2 = female, 1 = male). The companion "
     "'reported as' fields still read 4 (Age) and 3 (Gender) = 'not "
     "reported'; they were not on the batch's item list, so the agent left "
     "them (Age = 1 Mean/SD, Gender = 2 Counts would be correct).",
     "human master (sample description)"),
    ("Capraro & Celadin (2023)", "n1i / n2i (every DV row)", "numeric_error",
     "The rows carry an equal split of the total N (183/186/183/186) although "
     "the supplement PRINTS the analysed cell sizes (S1 p. 2: 198 baseline / "
     "198 accuracy endorsement / 197 fake alert; S2: 197/175/186; S3: "
     "186/180/184; S4: 186/186). The stored d/g/yi/vi were computed with the "
     "equal-split N, so every pooled effect is slightly conservative. The "
     "rows' n1i/n2i and the derived effect fields are the human master's.",
     "human master (numeric effect field)"),
    ("Capraro & Celadin (2023)", "Rationale_Notes", "stale_note",
     "All four notes still say 'Per-arm cell sizes are not printed; equal "
     "split ... (N_Reported_Per_Condition=0, N_Inference_Method=2)'. The "
     "supplement prints the cell sizes and the record now holds "
     "N_Inference_Method = 1. Two of them also say 'Intervention_Type = "
     "accuracy_nudge' while the field holds 'accuracy_prompts' (flagged by "
     "Scripts/check_rationale_consistency.py).",
     "master agent (prose refresh)"),
    ("Carey et al. (2020)", "Rationale_Notes", "stale_note",
     "All three notes say 'routed to debunking_and_rebuttals (correction "
     "family) with Intervention_Type = correction' while the field holds "
     "'debunking_and_rebuttals' (flagged by "
     "Scripts/check_rationale_consistency.py).",
     "master agent (prose refresh)"),
    ("Capewell et al. (2024)", "yi / vi (Exp1 row 13: Sharing discernment, "
     "T2, four-group contrast)", "numeric_error",
     "The row's own missing_metrics_reason quotes the statistic the paper "
     "reports - Supporting Information S1 Table S5 (p. 8): Mdiff = 0.2985, "
     "t(499) = 2.423, p_tukey = .074, Cohen's d = 0.3121, 95% CI [0. ...] - "
     "yet d_val / yi / vi are null, so the contrast is not pooled. The "
     "effect fields are the human master's.",
     "human master (numeric effect field)"),
    # --- from the master-agent pass over batch 051_pool
    ("Johnson (2024)", "control_group_type (Exp1)", "scope_ambiguity",
     "control_group_type is on the master agent's do-not-touch list, but the "
     "worklist assigned it as a pool item and the split classes it as "
     "paper-settled, so the agent settled it from the paper: 1 = No-treatment/"
     "Passive. Tables 7-8 (pp. 29-30) compare 'Includes Accuracy Incentive' "
     "with the SAME benchmark WITHOUT the incentive (the two no-benchmark "
     "conditions were excluded from that analysis), so the control arm is "
     "untreated at measurement; the 9 rows with contrast_type = 1 are "
     "therefore consistent with the record again (0 rows left for this key in "
     "Results/contrast_control_consistency.csv). Exp2 keeps 3 = Active (the "
     "White Benchmark arm), which all three routes agree on.",
     "human master (the do-not-touch list and the worklist disagree)"),
    ("Johnson (2024)", "Intervention_Type (Exp1)", "pool_change",
     "The routes disagreed on completeness (canonical ['accuracy_prompts'], SB "
     "['other', 'accuracy_prompts'], MM ['other']). Exp1 manipulates BOTH the "
     "accuracy incentive (accuracy_prompts) and the informational benchmark "
     "(a true reference statistic for the other racial group -> 'other'), so "
     "the complete list the paper supports is ['accuracy_prompts', 'other']. "
     "Exp2 (benchmark only, the incentive was randomised orthogonally and "
     "pooled, p. 40) keeps ['other'].",
     "already applied by the master agent"),
    ("Johnson (2024)", "DV_Metrics[6-8] of Exp2 (dv_name / comparison_label / "
     "contrast_type)", "coding_fidelity",
     "The canonical labelled Table 11's 'Black Benchmark' row 'Black vs White "
     "Benchmark among individualists'. Table 11 (p. 48) models "
     "Individualist + Black Benchmark + their interaction, so the Black "
     "Benchmark coefficient is the effect for the REFERENCE category, i.e. "
     "among STRUCTURALISTS: among white structuralists the shift is 9.98 "
     "while among white individualists it is 9.98 - 9.56 = 0.42 (the paper's "
     "prose, p. 47). The rows were relabelled '(structuralists, reference "
     "category)' and their contrast_type set to 6 (simple effect), matching "
     "the paper's own subgroup rows from Tables 12-14.",
     "already applied by the master agent"),
    ("Johnson (2024)", "score_types (all 21 policy-index rows of Exp2)",
     "pool_change",
     "The rows carried 'continuous_accuracy' for a measure that is not an "
     "accuracy score: the racial economic policy index is built from TWO ANES "
     "items (preferential hiring/promotion; federal fair treatment in jobs), "
     "each on a 5-point scale and scaled 0-100 (p. 42), i.e. a composite. All "
     "rows of that one measure now read ['composite']. The rows are "
     "eligible_primary = 0, so no pooled estimate moves.",
     "already applied by the master agent"),
    ("Johnson (2024)", "yi / vi / beta_val / beta_se / n_total (both records)",
     "numeric_error",
     "The batch listed these as disputed, but they are on the master's "
     "do-not-touch list, so the agent left them alone after checking them "
     "against the paper: the canonical values match Table 7 (Wealth -0.01 "
     "(0.04) N 913; Income 0.07 (0.05) N 936; Graduation 0.03 (0.06) N 929), "
     "Table 8 (COVID-19 -0.01 (0.04) N 847; Incarceration 0.03 (0.03) N 864), "
     "Table 34 (p. 89), Table 9 (0.34 (0.03) N 2778; 0.14 (0.04) N 1416; 0.54 "
     "(0.05) N 1362) and Table 11 (5.53 (2.18) / 9.98 (3.07) / -0.32 (2.70), "
     "N 984 / 495 / 489). The SB and MM values on these rows are PAIRING "
     "ARTEFACTS: both agent routes order their DV rows differently from the "
     "canonical, so a positional match compares different rows. MM's 10.30 "
     "(SE 4.85, derived) for 'black individualists' is not a printed cell but "
     "the prose estimate at p. 48 (= -0.32 + 10.62); the paper prints no SE "
     "for it.",
     "human master (numeric effect field)"),
    ("Johnson (2024)", "effect_favors (rows outside this batch's list)",
     "coding_fidelity",
     "The agent made effect_favors polarity-aware on Exp2 rows 0-8 (the rows "
     "the batch listed): 'treatment' on the inequality-score rows (the score "
     "is an accuracy scale - 1 = an accurate estimate, p. 42 - and every "
     "Black-Benchmark coefficient is positive), 'control' on the policy-index "
     "rows with a positive coefficient (the index runs 0 = most liberal to 100 "
     "= most conservative, so the sign is adverse) and 'treatment' on the "
     "black-respondent row with -0.32. The remaining rows still carry 'na' "
     "(Exp1 rows 0-26, Exp2 rows 9-23) and Exp1 row 24 carries 'neither' "
     "while its neighbours carry 'na' - they were not in this batch's item "
     "list, so the agent left them.",
     "human master (a sweep for the rows outside this batch)"),
    ("Johnson (2024)", "consent_reported / ethics", "coding_fidelity",
     "The dissertation mentions no IRB, protocol or consent statement "
     "(grep over the full text finds no 'consent', 'Institutional Review "
     "Board', 'IRB', 'ethics' or 'approved by'), so ethics = 0 (No mention) "
     "on both records. consent_reported was left EMPTY: the brief's rule is "
     "that a blank is not always 'no', and the paper states nothing about "
     "consent (SB and MM both wrote 0 = 'not', which says the same in "
     "practice).",
     "human master (the blank-vs-zero convention)"),
    # --- batch 101 (pool): Talabi et al. (2022), Tabor (2025),
    #     Tamboer et al. (2024 a)
    ("Talabi et al. (2022)", "Gender_Ratio", "coding_fidelity",
     "The paper prints 'The sample for this study was 66% male and 44% "
     "female' (Results, p. 6), which sums to 110. The agent wrote the "
     "paper's numbers (66 / 44) because the paper is the authority; the "
     "canonical's 66 / 34 was a silent correction of the paper's typo. The "
     "human decides whether the record keeps the printed pair or the "
     "arithmetically consistent one.",
     "human master (the printed value vs the corrected one)"),
    ("Talabi et al. (2022)", "control_group_type (record 0)", "coding_fidelity",
     "Record 0 (Experiment 1: fake-news exposure vs no exposure) is coded "
     "control_group_type = 6 (other); the control arm received no fake news "
     "at all, i.e. 1 = no-treatment/passive, which is the value MM wrote. "
     "control_group_type is on the master's do-not-touch list, so the agent "
     "left it at 6.",
     "human master (the control arm of Experiment 1)"),
    ("Talabi et al. (2022)",
     "effect_size_type_reported / source_location (record 0 row 0, "
     "record 1 rows 0 and 1)", "coding_fidelity",
     "The agent set effect_size_type_reported = 'means' on the two rows the "
     "batch listed (record 0 DV_Metrics[0] and record 1 DV_Metrics[0]): the "
     "paper reports cell means and SDs (Tables 1 and 2, p. 7), so the rows "
     "no longer claim that the paper reported no effect size. Record 1's "
     "SECOND row ('Behavioural intention to vaccinate') still carries "
     "'none' although Table 3 (p. 7) also reports cell means/SDs for it; it "
     "carries no pooled effect (yi, d, g and beta are all null), so nothing "
     "is blocked, but the type could be set to 'means' too. Separately, "
     "record 0's row 0 has source_location 'Table 2' while its source_quote "
     "is Table 1 (the fake-news table): the source_location should read "
     "'Table 1'.",
     "human master (the rows outside this batch's list)"),
    ("Tabor (2025)", "contrast_type / control_group_type (rows 0, 1, 3, 4)",
     "pool_change",
     "The record's control_group_type is 6 (other), but the four main-effect "
     "rows claimed contrast_type = 1 (intervention vs no-treatment control), "
     "which the contrast/control checker flagged as a contradiction. The "
     "paper (p. 99) describes a 2x2 between-subjects design with random "
     "assignment, so those rows are factorial main effects: the agent set "
     "contrast_type = 5 on rows 0, 1, 3 and 4 (the interaction rows keep 10, "
     "the two mediation rows keep 7), which clears the contradiction flag. "
     "The paper's control arm is the passive 'no estimate x no graph' cell "
     "(static text only, no manipulation), i.e. control_group_type = 1, but "
     "that field is the human master's and was left at 6.",
     "human master (control_group_type)"),
    ("Tabor (2025)", "cell_descriptives (rows 0-4)", "coding_fidelity",
     "SB's rows carry per-cell means and SDs (e.g. estimate task M = 3.13, "
     "SD = 1.58 on the tough-on-crime scale) and MM's carry none. Neither "
     "the paper nor its supplement (Supplements/Tabor (2025) Supplement 1.pdf, "
     "the OSF 'Proofs Supp Materials') reports per-cell descriptives - only "
     "the overall M = 3.00, SD = 1.60 for the four-item scale (p. 100). Those "
     "cell values come from the OSF deposit (data.RData), so the agent kept "
     "the canonical's empty cell_descriptives; the numbers are available in "
     "the deposit if the human wants them written in.",
     "human master (deposit-derived cells in the record)"),
    ("Tamboer et al. (2024 a)", "effect_size_type_reported (rows 0-4, 6, 7)",
     "scale_question",
     "The paper reports mixed-effects model b coefficients with SEs (Table 2, "
     "p. 72), so the agent wrote the paper's reported type ('beta') on the "
     "seven rows that carried 'd' (row 5 already carried 'beta'). The "
     "stored yi/vi are the pipeline's STANDARDISED d (d = 0.39, SE = 0.12 "
     "for the self-efficacy row), and a raw type label makes the "
     "standardisation guard refuse that yi/vi, so the rows now pool as raw "
     "betas on a 6-point scale (b = 0.38) rather than as d. Same tension as "
     "Choi et al. (2023): the record holds a standardised effect under a raw "
     "type label.",
     "human master (the pooling metric)"),
    ("Tamboer et al. (2024 a)", "dv_domain (rows 0 and 2)", "pool_change",
     "The canonical coded the self-efficacy row 'confidence' - a label that "
     "is no longer in the controlled vocabulary: it resolves to the "
     "NON_OUTCOME 'confidence' strand, so the row was dropped from every "
     "pooled model - and the motivation row 'critical_thinking'. Both agent "
     "routes code 'media_literacy_attitudes', the domain the canonical "
     "itself uses for the sibling 'Value for media literacy' row, and the "
     "paper's outcomes are media-literacy beliefs (p. 67-68). The agent "
     "wrote 'media_literacy_attitudes' on both rows, so the self-efficacy "
     "row now enters the media_literacy_attitudes pool and the motivation "
     "row leaves critical_thinking. If the 2026-09-19 reclassification put "
     "the self-efficacy row into the confidence strand on purpose (self-"
     "efficacy as a confidence-type outcome), the human should reverse "
     "row 0.",
     "human master (the domain, and the pool change)"),
    ("Tamboer et al. (2024 a)", "ethics / N_Inference_Method", "coding_fidelity",
     "The paper names the ethics review AND the protocol number ('The study "
     "was reviewed by the Ethics Committee of the Faculty of Social Sciences "
     "at Radboud University (ECSW-2021-041)', p. 64), so ethics = 3; the "
     "canonical holds 2. The field was not in this batch's item list, so the "
     "agent left it. N_Inference_Method: the paper reports no per-condition "
     "cell sizes (Table 2 note: N = 255), so the canonical's equal-split "
     "inference (2) stands; SB's 168/87 split is not in the paper.",
     "human master (ethics 2 vs 3; the N inference)"),
    # --- batch 308 (pool): Hameleers (2024), Heuett & Westerman (2014)
    ("Hameleers (2024)", "m1i / m2i / sd1i / sd2i (third-person "
     "Established-vs-Relativized row, DV_Metrics[8])", "numeric_error",
     "The third-person 'Relativized ML vs Established ML' row carries the "
     "FIRST-PERSON cells of Table 1 (38.74/21.01 and 43.43/22.04, p. 370). The "
     "paper's third-person cells are Established ML (M = 49.29, SD = 20.25) and "
     "Relativized ML (M = 43.01, SD = 20.58) (Results p. 370 / PDF p. 12), and the "
     "row's stored effect (d/g/yi/vi = -0.217) is built from the wrong outcome. "
     "The row is eligible_primary = 0 (an intervention-vs-intervention contrast), "
     "so no pooled estimate moves, but the row is a copy of the first-person row. "
     "m1i/sd1i/m2i/sd2i are on the master's do-not-touch list, so the agent left "
     "them and instead wrote the discrepancy into the row's missing_metrics_reason "
     "(which keeps the row out of every pool) and renamed its comparison_label "
     "'(third-person, post)' so it no longer reads as a first-person contrast.",
     "human master (numeric effect field)"),
    ("Hameleers (2024)", "Rationale_Notes", "stale_note",
     "The note says 'each intervention-vs-control contrast is eligible_primary=1 "
     "(contrast_type 1)', but the rows carry contrast_type 2. The agent kept 2: the "
     "paper's control arm read a message about search engines, i.e. an unrelated-"
     "content PLACEBO control (Method p. 367-368 / PDF p. 9-10), so 'treatment vs "
     "active/placebo control' is the accurate label and contrast_type 1 ('no-"
     "treatment control') is not. The note's wording is stale (the record's own "
     "control_group_type = 1 also says 'no-treatment/passive', which is the human "
     "master's field and was left alone).",
     "master agent (prose refresh)"),
    ("Heuett & Westerman (2014)", "yi / vi / t_val (state-CA pre->post row, "
     "DV_Metrics[0])", "numeric_error",
     "The row stores the reported paired t(100) = 9.30 with dv_polarity = 0 (higher "
     "state CA = worse) and group1 = Post (M = 2.74) vs group2 = Pre (M = 4.68). The "
     "pipeline's t->z route returns a POSITIVE z (the reported t is a magnitude), "
     "which the polarity flip turns NEGATIVE, i.e. the row would read as if the "
     "intercultural interaction made apprehension worse, while the means route "
     "(post - pre) gives the correct direction. Nothing is pooled (the row is "
     "eligible_primary = 0, the study has one arm and is excluded under C1/C2), but "
     "the sign is wrong if the row is ever reused. The effect fields are the human "
     "master's, so the agent left them.",
     "human master (numeric effect field)"),
    ("Heuett & Westerman (2014)", "dv_domain / comparison_label (the three "
     "change-score rows, DV_Metrics[6-8])", "coding_fidelity",
     "The agent settled the fields on its item list: the SCAM pre->post row's "
     "dv_domain 'affect' -> 'other' ('affect' left the controlled vocabulary on "
     "2026-09-19) and the belief pretest row's 'other' -> 'attitude_policy' (the "
     "same McCroskey & Richmond measure as the attitude and belief rows above it), "
     "plus measure-specific comparison_labels on rows 0-5 (the canonical's single "
     "label was shared by three different measures and, on rows 0-2, reversed the "
     "row's own group1/group2 order). The three change-score rows still carry "
     "dv_domain 'other' while the same measures' level rows carry "
     "'attitude_policy', and they still share one comparison_label; they were not "
     "on the item list, so the agent left them.",
     "human master (coding consistency)"),
    ("Heuett & Westerman (2014)", "eligible_primary (the three pre->post rows)",
     "scope_ambiguity",
     "MM codes the three pre->post rows eligible_primary = 1 while the canonical "
     "has 0 on every row. The paper has no control arm at all (a single-group "
     "pretest-posttest design, n = 102, Method p. 45 / PDF p. 5) and the record is "
     "excluded under C1 (no misinformation intervention: the target is "
     "communication apprehension) and C2 (no control group), so 0 is right and the "
     "canonical's own Rationale_Notes says so. eligible_primary is on the master's "
     "do-not-touch list, so the agent left it.",
     "human master (eligibility)"),
    ("Heuett & Westerman (2014)", "dv_polarity (DV_Metrics[3], [7], [8])",
     "numeric_error",
     "Three rows read dv_polarity = 1 although the measure's sign says otherwise. "
     "Row 3 is the SCAM (state communication apprehension) at pretest, the same "
     "measure as row 0, where the canonical has dv_polarity = 0 (higher CA = worse); "
     "with polarity 1 the row reads as if the US group's higher CA (M = 5.31 vs "
     "4.04) were the better outcome. Rows 7 and 8 are the attitude and belief "
     "CHANGE scores, and the paper prints those as posttest minus pretest (M = "
     "-.54/-.87 and -.84/-.81, i.e. NEGATIVE = an improvement), so a higher change "
     "value is a SMALLER improvement, i.e. less desirable (dv_polarity = 0); the "
     "CA change row (row 6) is printed the other way round (pretest minus posttest: "
     "+3.38 / +.49), where a higher value is the larger reduction and dv_polarity = "
     "1 is right. MM codes the three rows 0 (SCAM pretest, attitude change, belief "
     "change) and 1 for the CA change row. dv_polarity is on the master's do-not-"
     "touch list, so the agent left the values. All three rows are "
     "eligible_primary = 0 in a study that is excluded under C1/C2, so no pooled "
     "estimate moves today; the polarity flip would matter if the row were ever "
     "reused.",
     "human master (dv_polarity)"),
    # --- r440 master-agent batch (2026-10-22) ---
    ("Heiter (2024)", "n1i / n2i (the five Experiment-2 rows)", "numeric_error",
     "The Experiment-2 rows carry n1i = 147 (training) and n2i = 161 (control), "
     "but the paper's own Table 5 (Appendix B, p. 85) prints 'Training n = 144, "
     "Control n = 164, Full sample N = 308'. The 161 is EXPERIMENT 1's control "
     "subsample: 'After removing three cases for missing completed scores from the "
     "gamified control intervention, 161 individuals from the control group (i.e. "
     "the individuals who did not receive cognitive training) were evaluated' "
     "(p. 54). Experiment 2's MANOVA uses all 308 (144 + 164), so every "
     "Experiment-2 row's arm n - and with it the SE and vi of the stored d - is "
     "computed from the wrong cell sizes. The reported M/SD (and therefore the d "
     "values themselves) are unaffected. n1i/n2i are on the master agent's "
     "do-not-touch list, so the agent left them; the same defect is described in "
     "Extraction/Heiter (2024).md.",
     "human master (numeric effect field)"),
    ("Hindin et al. (2004)", "Fulltext_Exclusion_Criterion / control_group_type",
     "scope_ambiguity",
     "The canonical records ['C2'], the SB and MM routes record nothing - and a "
     "blank criterion is ambiguous, so the human has to decide. The paper's "
     "design: 'The study used a quasi-experimental pretest-posttest, comparison "
     "condition and intervention condition design ... In this design, the food "
     "safety curriculum served as an educational placebo ... The participants "
     "served as their own controls' (p. 193) - ONE sample of 35 parents, tested "
     "four times (Time 1 pre-test, Time 2 post-test of the food-safety placebo "
     "= pre-test of the intervention, Time 3 post-test of the intervention), no "
     "concurrent control group, no randomisation. C2 is therefore well "
     "supported; C4 ('not a primary experimental study') is the arguable addition "
     "for a quasi-experimental one-group design. The paper equally settles the "
     "control arm: the comparison condition is an EDUCATIONAL PLACEBO, so "
     "control_group_type = 2 (both agent routes), while the canonical leaves it "
     "empty. control_group_type and the criterion are the human master's; the "
     "agent left both untouched.",
     "human master (inclusion, per experiment)"),
    # --- r149 master-agent batch (2026-10-22) ---
    ("Austin et al. (2006)", "n1i / n2i (the three lesson-vs-control rows)",
     "numeric_error",
     "The coded arm sizes (n1i = 151 pooled lessons, n2i = 89 control) follow the "
     "paper's own arithmetic: the Methods print the grade-by-condition counts "
     "('more eighth graders ended up in a logic-oriented group (n = 56) than an "
     "emotion-added group (n = 27), and more seventh graders ended up in an "
     "emotion-added group (n = 50) than in a logic-only group (n = 18)', p. e425), "
     "so the two lesson arms are 74 and 77 and the control arm is 240 - 151 = 89. "
     "The MM route's 159 / 79 for the skepticism row is not derivable from the paper "
     "at all. What the paper does show is that the analysed N per outcome is a "
     "little below the enrolled 240: the Figure 1 notes report t233 = 1.81 (news "
     "recall), t234 = 6.06 (ad recall) and t236 = 2.27 (skepticism), i.e. 235-239 "
     "analysed cases, so 1-5 participants are missing on each outcome and the paper "
     "does not say which arm they are in. The coded arm Ns therefore over-state the "
     "analysed N by 1-5, which slightly understates vi. n1i/n2i are on the master "
     "agent's do-not-touch list, so the agent left them; the human master decides "
     "whether to lower the arm Ns.",
     "human master (numeric effect field)"),
    ("Austin et al. (2006)", "effect_size_type_reported (the three "
     "lesson-vs-control rows)", "scale_question",
     "The paper reports t values only (Figure 1 notes, p. e430), so the reported "
     "type is arguably 'none'; the canonical keeps 'd' because the row's stored "
     "yi/vi IS the Cohen's d the pipeline derived from that t (conversion_method: "
     "'Reported t with cell Ns -> metafor (t_val, n1i, n2i) fallback', recipe "
     "'T: t->d'), and the standardisation guard reads 'none' as 'not standardised' "
     "and refuses the row: the row would stop pooling as a d and would instead "
     "fall through to the t->z route (a Fisher z of 0.147 instead of a d of 0.303), "
     "silently changing the pooled estimate and mixing metrics within one model. "
     "Do NOT 'correct' these three rows to 'none'. The sibling rows DV[3]-DV[5] "
     "carry 'none' correctly because no effect was derived for them.",
     "master agent (kept as 'd' on purpose)"),
    # --- r098 master-agent batch (2026-10-22) ---
    ("Noguera-Sepulveda (2019)", "Fulltext_Exclusion_Criterion", "scope_ambiguity",
     "The paper delivers a written mind-perception induction - participants type "
     "at least 20 words about the 'mind' of nine targets (Appendix C, pp. 34-35) - "
     "against a cooking-video control (Procedures, p. 12), and the outcome is "
     "generic conspiracist belief (GCB). No misinformation is presented, corrected "
     "or pre-bunked anywhere in the study, so C1 (no misinformation intervention) "
     "looks right; the MM route coded C1, SB and the canonical left the field "
     "empty. Fulltext_Exclusion_Criterion is on the master agent's do-not-touch "
     "list, so the agent left it; the human master decides.",
     "human master (inclusion)"),
    ("Nyhan et al. (2022)", "N_Inference_Method / Rationale_Notes", "stale_note",
     "The note says 'N_Inference_Method = 2' and describes the arm Ns as an equal "
     "split of the total (2898/7 = 414 per arm; science wave 2 = 4 arms = 1656 vs "
     "placebo wave 2 = 2 arms = 828; sequence contrasts 414 vs 414), but every "
     "row's arm N comes from the authors' public replication (t1.rds, "
     "github.com/thomasjwood/nyhanporterwoodtso): row 0 stores n1i = 1516 / n2i = "
     "822 where the equal split would be 1656/828, and the SI Table S1 arm Ns are "
     "418/402/404/352/369/397/398. The paper prints no per-condition N at all, so "
     "the agent kept N_Inference_Method = 2 and did not touch the note (not a "
     "listed field). Either the field should become 3 (Other) with the note naming "
     "the replication, or the note should be corrected to describe the replication "
     "counts.",
     "human master (N bookkeeping and the stale note)"),
    ("Nicolls et al. (2025)", "cell_descriptives (the 18 pre->post DiD rows)",
     "scope_ambiguity",
     "The 54 pairwise rows now carry Table 1's per-cell descriptives (3 arms x 3 "
     "occasions, p. 227) with the paper's own time labels ('1', '2', '3'). The 18 "
     "pre->post DiD rows (timepoint_1 = 'pre') carry those cells as well, but '1'/"
     "'2'/'3' do not classify as pre/post in the pipeline's DiD derivation "
     "(contract.classify_timepoint), so the DiD pool still receives nothing from "
     "this study although Table 1 holds the four cells each DiD needs. Relabelling "
     "the cells 'baseline (time 1)' / 'one-week post (time 2)' would let the DiD be "
     "derived and would add 9 contrasts (one per DV) to the DiD pool; the agent did "
     "NOT relabel them, because that would silently enlarge a pooled model.",
     "human master (the DiD pool)"),
    ("Nyhan et al. (2022)", "Intervention_Type / Rationale_Notes", "stale_note",
     "The note says 'routed to debunking_and_rebuttals (correction family) with "
     "Intervention_Type = correction', but the record's Intervention_Type is "
     "['other'] - and all three routes agree on ['other'] (each with a non-empty "
     "Intervention_Type_Detail), so the SENTENCE is the stale part, not the field. "
     "Intervention_Type is read by the analysis (contract.record_interventions), "
     "so the human master should decide whether this study belongs in the "
     "correction family and, if the note is right, have the field set to match.",
     "human master (module routing and the stale note)"),
    ("Nicolls et al. (2025)", "Primary_Target_Variable", "scope_ambiguity",
     "The social_norms module's four options (sharing intentions / belief in "
     "misinformation / flagging / discernment score) describe none of this study's "
     "outcomes: the target is hand-held phone use while driving and approval of it "
     "(Materials 2.4.2-2.4.3, pp. 225-226), and the paper measures no sharing, no "
     "belief in misinformation, no flagging and no discernment score. The agent set "
     "the field to null (the canonical's 1 = 'Sharing intentions' was contradicted "
     "by the paper); the human master may prefer to keep the module field empty for "
     "the whole social-norms strand.",
     "human master (module routing)"),
    ("Panizza et al. (2022)", "effect_size_type_reported (the 5 correct-guessing rows)",
     "pool_change",
     "The 5 'correct guessing' rows carried 'other', which the standardisation "
     "guard refuses (a generic type fails closed), so those rows carried a "
     "documented d-scale effect (yi = (logit p1 - logit p2)*sqrt(3)/pi from the "
     "paper's cell proportions, p. 5 / p. 8) and yet never reached a pool. The "
     "paper reports the cell PROPORTIONS (control 78.2% / incentive 80.1% / "
     "pop-up 78.1% in Exp. 1), i.e. the 'means' route that AGENTS.md puts "
     "first and calls always poolable, so the agent set 'means' on all 10 "
     "descriptives-based rows (the 5 accuracy-score rows keep the same yi/vi). "
     "Three eligible rows now pool that were previously blocked: Exp. 1 "
     "pop-up vs control (yi = -0.003, SE 0.067), Exp. 2 pop-up vs control "
     "(yi = 0.039, SE 0.060) and Exp. 2 pop-up+incentive vs control "
     "(yi = 0.214, SE 0.062). The last one is a COMBINED arm; the human "
     "master may want to look at that row before it enters a pooled model.",
     "human master (the rows that enter the pool)"),
    ("Panizza et al. (2022)", "Intervention_Type (both records)", "coding_fidelity",
     "The canonical file carried only ['lateral_reading'] while its own "
     "Rationale_Notes said 'it is coded Intervention_Type=mix'. The paper "
     "supports the fuller list: the pop-up presents 'a list of civic online "
     "reasoning techniques (e.g., lateral reading, click restraint) as tips to "
     "verify the information in the post' (p. 4) = media_literacy_tips + "
     "lateral_reading, and the second arm is a performance-based monetary "
     "incentive (doubled participation fee, p. 3) = other. The agent set "
     "['media_literacy_tips', 'lateral_reading', 'other']; the non-empty "
     "Intervention_Type_Detail already names both arms. Exp. 1 has no explicit "
     "Interventions list, so the record-level list is the analysis unit's "
     "intervention label there.",
     "human master (the intervention label of the implicit unit)"),
    ("Oswald et al. (2026)", "effect_size_type_reported / beta_se (rows 0-5)",
     "scale_question",
     "The paper reports lmer interaction coefficients (b = 0.011 / 0.016 / 0.007 "
     "/ 0.005 / 0.015 / 0.014 with 90% CIs) and NO d, so the paper's reported "
     "type is 'beta'. The stored yi/vi, however, are the STANDARDISED d of the "
     "2026-09-19 standardisation pass (yi = t*sqrt(1/n1+1/n2), the audit in "
     "conversion_inputs.effect_type_audit records original_reported_type = "
     "'beta'), and writing 'beta' would make the guard fail closed - the row "
     "would pool the raw proportion-scale coefficient beside d-scale effects. "
     "The agent therefore KEPT 'd' (the same decision the master agent took "
     "for Dutta-Powell et al. (2024)) and left the two disputed beta_se values "
     "(rows 1 and 5) as they stand: they are the SE implied by the paper's own "
     "90% CI ((0.028-0.003)/2/1.6449 = 0.007599) and they are the value the "
     "recorded t = 2.1054 and the stored yi = 0.0999 were derived from. The "
     "paper's Supp. Table S6 prints the rounded SEs (0.007 for row 1, 0.008 "
     "for row 5) and t = 2.09 / 1.90, so the unrounded SE is ~0.0076 / "
     "~0.0074 - the stored values are the closer ones. No pooled number moves.",
     "human master (the label of a standardised row)"),
    ("Orosz et al. (2024 a)", "Geographic_Setting", "scope_ambiguity",
     "Hungary: the canonical route coded 1 (Global North), MM coded 4 "
     "(other/unclear). The paper classifies Hungary neither way - it is an EU "
     "member in Central/Eastern Europe and the paper frames it as an "
     "'illiberal regime - informational autocracy' and a 'buffer zone between "
     "Russia and the Euro-Atlantic alliance' (pp. 1-2, 10). The agent kept the "
     "canonical 1 (the standard Global North taxonomy: high-income, "
     "industrialised, EU/NATO) because the paper does not support MM's 4, and "
     "the paper's 'not Western' framing is already carried by WEIRD_Western = "
     "0. Note that the sibling paper Orosz et al. (2024) (same sample country) "
     "carries Geographic_Setting = 4 in the canonical route - the two files "
     "disagree and the human master may want one convention for Hungary.",
     "human master (one convention for Hungary)"),
    ("Orosz et al. (2024)", "Self_Efficacy (and the sibling file's blank)",
     "coding_fidelity",
     "The agent set Self_Efficacy = 1 (was 0): the intervention 'putting "
     "them in an expert role' (p. 1) with peer testimonials reporting 'their "
     "favorable experiences when they managed to spot fake news' (p. 4) is "
     "explicit efficacy support, and the discussion says the material lets "
     "adolescents 'find themselves in a superior, expert position to give "
     "advice' (p. 8). The sibling file Orosz et al. (2024 a) leaves the field "
     "BLANK for the same intervention, so the two files now differ; the "
     "human master may want the same value in both.",
     "human master (the same value in both files)"),
    # --- from the master-agent pass over batch r033_pool (Austin family)
    ("Austin et al. (2020) / (2020 a) / (2024)",
     "comparison_label (which pool a pretest-adjusted MANCOVA estimate "
     "answers)", "pool_change",
     "THREE PUBLICATIONS OF ONE TRIAL, TWO POOLS. All three papers report "
     "the same kind of estimate: the standardized Condition effect on the "
     "POSTTEST outcome with the PRETEST as a covariate (2020a, p. 5: 'Posttest "
     "measures served as dependent outcomes controlling for pretest levels'; "
     "2020, p. 310: 'The MANCOVA controlled for pretest levels of model "
     "elements as well as the age of the youth'; 2024, p. 126, Table 1). The "
     "pipeline routes a row by metaanalysis.dv_timing, which sets pre = True "
     "when the comparison_label contains 'pretest': the 2020a labels ('... "
     "standardised MANCOVA Condition coefficient (posttest, adjusted for "
     "pretest and covariates)') and the 2024 labels ('... posttest (MANCOVA "
     "controlling pretest level of the same variable)') therefore answer the "
     "pretest-corrected DiD question, while the 2020 labels ('Intervention vs "
     "control, adjusted posttest (MANCOVA) - Parent report block') carry no "
     "pretest token, so those rows answer the immediate-posttest question "
     "instead. The 2024 rationale even says 'these rows belong to the "
     "immediate-posttest pool', which is not what its own label produces. The "
     "agent did not change the routing: the comparison_label was in the "
     "batch's item list, but the paper settles WHAT the estimate is, not which "
     "of the three questions it answers.",
     "human master (one rule for pretest-adjusted estimates)"),
    ("Austin et al. (2020) / (2020 a) / (2024)",
     "dv_domain (Child-initiated discussion)", "coding_fidelity",
     "The same 4-item measure of the same trial is coded three ways: "
     "media_literacy_attitudes in 2020a (rows 3-5, 27, 28, 38) and in 2024 "
     "(rows 0, 6, 9, 12, 15, 16); 'other' in 2020 (rows 1, 6), where the "
     "record's own note argues 'the remaining outcomes are family-communication "
     "... behaviours that are not discernment, credibility, sharing, search or "
     "trust measures, so they are coded other'; and 'other' in both agent "
     "routes for 2020a. The agent kept the canonical value in 2020a, because "
     "2020a and 2024 describe the identical measure (2024 prints the four "
     "items: 'I start conversations with my parents about: selling techniques "
     "... what is real or not real ... nutritional content ... healthier food "
     "choices', p. 125) and splitting one trial's outcome across two domain "
     "subgroups separates the same evidence; the 2020 record is the outlier. "
     "Note that the paper's own framing calls the measure advertising-literacy "
     "PERFORMANCE (2024, p. 124: 'the FoodMania curriculum incorporated the "
     "practice of advertising literacy performance'), which sits between "
     "'media_literacy_attitudes' and 'other'.",
     "human master (the domain for this measure across the family)"),
    ("Austin et al. (2020)", "vi (DV_Metrics[6], youth report: child-initiated "
     "discussion about media content)", "numeric_error",
     "The row carries yi = -0.661 with vi = 0.1444 (SE = 0.38); the MM route "
     "carries vi = 0.02263803 (SE = 0.15) for the same row and the SB route "
     "the canonical's 0.1444. The paper's Appendix Table A1 (the source of "
     "the SMD) is online-only and NOT in this PDF, so neither variance could "
     "be checked against the paper. The row's sign already conflicts with the "
     "paper's prose ('Youth in the intervention group, except the 9-year-olds, "
     "reported more occasions when they initiated discussion with their parents "
     "than the control group', p. 311), which the record's own note flags. A "
     "six-fold difference in the weight of one eligible row - and in its sign - "
     "is the human master's call.",
     "human master (numeric effect field)"),
    ("Austin et al. (2020)", "N_Reported_Per_Condition", "scope_ambiguity",
     "The field is 1 ('cell sizes explicitly reported') but the paper prints "
     "no per-arm MANCOVA n's: it gives N = 189 for the matched-group sample "
     "and the MGSEM subsample's arms (control n = 82, intervention n = 97, "
     "after 10 cases were dropped for excessive missing data, p. 310). The "
     "record's DV rows carry no n's (n1i/n2i/n_total are null), and the agent "
     "refreshed the stale Rationale_Notes sentence that said 0 - but the field "
     "itself was not in this batch's item list, so the value stands. Austin et "
     "al. (2020 a) is the opposite case: there the arm sizes ARE printed "
     "(Table 2 note: intervention n = 105, control n = 84), so 1 is right and "
     "the note that said 0 was stale.",
     "human master (sampling bookkeeping)"),
    ("Austin et al. (2020)", "Pedagogy_Type", "coding_fidelity",
     "The record codes 3 (Co-created/Collaborative) while the three sibling "
     "records of the same FoodMania curriculum code 2 (Guided Inquiry: 2018, "
     "2020 a, 2024). The paper's phrase that invites 3 is about the CURRICULUM "
     "DEVELOPMENT - 'Curriculum development incorporated both focus and work "
     "groups with parent and youth involvement' (p. 309) - not about the "
     "delivered sessions, which are a 2-h weekly unit of facilitator-led "
     "discussion and activities (1 h parents and youth separately, 1 h jointly, "
     "p. 309). The field was not in this batch's item list, so the agent left "
     "it.",
     "human master (coding consistency across the family)"),
    ("Austin et al. (2018)", "Tips_Content_Checklist", "coding_fidelity",
     "The record leaves the list empty ('This is media-literacy education, "
     "not a tip list'), while the 2020 a and 2024 records - the same "
     "curriculum - carry the two controlled tips the paper supports "
     "('investigate_the_source' for the four Critical-Thinking-about-Sources "
     "items and 'check_evidence_and_references' for the nutrition-label "
     "comparison work; see the 2024 rationale). The agent repaired the 2020 a "
     "list (it held 14 off-vocabulary topic strings - 'deconstruct "
     "advertising', \"the '5 Ws' of media\", 'bliss point / mouthfeel' ... - "
     "which the schema's controlled vocabulary does not allow) to the same two "
     "entries, but the 2018 field was not in this batch's item list and was "
     "left empty. One convention for the family is what the corpus needs.",
     "human master (coding consistency across the family)"),
    ("Austin et al. (2018) / (2020 a) / (2020) / (2024)", "Self_Efficacy",
     "scale_question",
     "The same curriculum is coded 1 in 2018 and 2020 a (the 2018 note's "
     "reason: 'parent Efficacy is an explicit, measured target of the "
     "intervention and a key path in the model') and 0 in 2020 and 2024, whose "
     "models contain no efficacy construct at all - so the per-key readings "
     "are coherent, but the family is split. The agent kept 2020 a at 1 (its "
     "model has 'Parental efficacy for healthy dietary practices' as a key "
     "path, Figure 1, p. 3, so the SB route's 0 was the outlier) and left 2020 "
     "and 2024 at 0, where the agent routes that coded the field also wrote 0 "
     "and the papers' models have no efficacy construct. Whether the field "
     "means 'the intervention supports efficacy' or 'efficacy is part of the "
     "intervention's model' is a corpus-wide convention.",
     "human master (the convention)"),
    # --- batch r389_pool: Xue et al. (2024), Xiao et al. (2023),
    #     Xie et al. (2024), Wong & Wu (2023)
    ("Wong & Wu (2023)", "Gender_Ratio", "coding_fidelity",
     "The record's Gender_Reported_As now reads 2 (Counts). The article text "
     "prints no gender ratio (it uses gender only as a moderator, PDF p. 6), "
     "but the paper's own supplementary material "
     "(Supplements/Wong & Wu (2023) Supplement 1.pdf, Figure 2 'Overview of "
     "participant demographics', p. 10) reports the split as COUNTS: "
     "treatment Males 26 / Females 42, control Males 18 / Females 36 - i.e. "
     "44 male and 78 female of the 122 finishers, and the four cells add up "
     "to the article's arms (68 / 54, PDF p. 6). The canonical's 3 ('Not "
     "reported') was therefore wrong once the supplement is read. "
     "Gender_Ratio was not in this batch's item list, so it stays null; the "
     "values above can be written in one step (44 / 78, i.e. 36.1% male).",
     "human master (the ratio value)"),
    ("Wong & Wu (2023)", "code_available", "coding_fidelity",
     "SB and MM both wrote 0 ('analysis code not available'), the canonical "
     "leaves it empty. The paper states no code availability at all - it "
     "gives only 'supplemental data for this article can be accessed online' "
     "(PDF p. 1) and a disclosure statement (PDF p. 12) - and empty is the "
     "code for 'not stated' (0 would assert that no code exists). The agent "
     "left the field empty.",
     "human master (only if a 0 is preferred over 'not stated')"),
    ("Xie et al. (2024)", "score_types (DV_Metrics[0] and DV_Metrics[2])",
     "scale_question",
     "The canonical codes 'composite' on the two rows the batch listed, MM "
     "codes 'other' (SB codes 'other' for the brand-attitude row and "
     "'separate_false' for the credibility row). The paper is a ~1.9k-character "
     "conference abstract (PDF p. 1) that states NO measurement structure, so "
     "it supports neither value; the agent kept the canonical 'composite', "
     "which is the corpus's code for a measure built from several items (see "
     "the Johnson (2024) flag: a two-item index became 'composite'; Dugas et "
     "al. (2024): a single-item attitude measure became 'single_item') and is "
     "consistent with the record's 7-point Likert coding. The two rows that "
     "were NOT in the batch's list - DV_Metrics[1] ('Perceived influencer "
     "ethicalness (Experiment 1)') and DV_Metrics[4] ('Misinformation beliefs "
     "and endorsement effectiveness ... (anti-vaxxers, Experiment 2)') - carry "
     "the same canonical 'composite' vs MM 'other' split and were left as they "
     "are, so the record stays uniform (all four non-purchase rows "
     "'composite', the purchase-intention row 'behavioral_intention'). All "
     "five rows are eligible_primary = 0, so no pooled estimate moves either "
     "way. The open corpus-wide question on 'composite' (a multi-item average "
     "vs 'continuous_accuracy') is still with the campaign owner.",
     "human master (the 'composite' family rule; the two rows outside this batch)"),
    ("Xie et al. (2024)", "dv_name (DV_Metrics[0], [2], [3])",
     "coding_fidelity",
     "The canonical names keep the study and subgroup qualifiers ('Perceived "
     "credibility of COVID-19 vaccine misinformation (pro-vaxxers, Experiment "
     "1)', 'Attitude toward the endorsed brand (Experiment 1)', 'Purchase "
     "intention for the endorsed brand (Experiment 1)'); MM uses the bare "
     "outcome names. The record is ONE record for the paper's two studies, so "
     "the qualifiers are what tell the Study 1 rows from the Study 2 row, and "
     "the abstract's own wording supports them ('misinformation labels "
     "significantly reduce the perceived credibility of misinformation among "
     "pro-vaxxers ... (Study 1)', PDF p. 1). The agent kept the canonical "
     "names. Nothing pools (all rows eligible_primary = 0); the convention for "
     "the corpus - short outcome name vs name + study/subgroup qualifier - is "
     "a naming decision the human may want to make once for the corpus.",
     "human master (the naming convention)"),
    ("Xiao et al. (2023)", "Fulltext_Exclusion_Criterion", "scope_ambiguity",
     "The batch listed this field (canonical empty, SB ['C2']); it is on the "
     "master's do-not-touch list, so the agent left it empty. The paper's "
     "design is a 2 (message types: narrative vs statistics) x 2 (social "
     "frames: individual vs collective) between-subjects experiment in which "
     "EVERY arm sees the same misinformation Facebook post AND a corrective "
     "comment (PDF p. 3) - so no arm is free of a misinformation intervention "
     "and no contrast is intervention vs a no-misinformation-intervention "
     "control, which is the reading behind SB's C2. On the literal criterion "
     "('the study has comparison groups') the record stays empty, and the "
     "study contributes no poolable row either way (all 12 DV rows are "
     "eligible_primary = 0).",
     "human master (the screening criterion)"),
    ("Xue et al. (2024)", "Outcome_Measures", "coding_fidelity",
     "The canonical lists the three outcome measures the paper's Measures "
     "section reports (Sharing Intention, Perceived Credibility, Information "
     "Verification, PDF p. 8); MM adds 'Manipulation checks' to the list. The "
     "paper does collect manipulation checks (Sect. 5.1, PDF p. 9: four "
     "7-point items on the emotional-appeal and source-type manipulations) but "
     "reports them as checks on the message FEATURES and states that no "
     "manipulation check was needed for the nudge itself ('the accuracy nudge "
     "label had only two states (Yes or No)'). The agent kept the three "
     "outcomes, which are the ones the 13 DV rows measure.",
     "human master (whether manipulation checks belong in Outcome_Measures)"),
    ("Ranieri et al. (2017)", "Setting", "coding_fidelity",
     "The three routes split three ways on the setting of one teacher "
     "professional-development course: canonical 2 (field), SB 7 (other), MM 3 "
     "(classroom). The paper describes a blended course for in-service "
     "teachers - face-to-face meetings plus the eLAB/Moodle platform (Sect. "
     "2.2, p. 13; Sect. 5.2, p. 16) - delivered by three organisations to real "
     "teachers, so it is neither a laboratory nor an online experiment. The "
     "canonical 2 was kept because the paper does not settle which of the "
     "three terms fits; one setting code for 'in-service teacher training' "
     "would be better than three.",
     "human master (the setting code for in-service training)"),
    ("Ratajczak et al. (2019)", "Concept_Labeling", "coding_fidelity",
     "Canonical 0 (implicit) vs SB 1 (explicit). The intervention is a "
     "social-norm feedback letter whose operative sentence is quoted in the "
     "paper: 'The great majority (80%) of practices in England prescribe fewer "
     "antibiotics per head than yours' (PDF p. 4). Read as 'is the concept "
     "named in the message', that is an explicit statement; read as 'does the "
     "training name the technique or concept it teaches', nothing is taught "
     "or labelled, so 0. The canonical 0 was kept, consistent with how the "
     "same corpus codes explicit concept naming in the media-literacy "
     "trainings.",
     "human master (what Concept_Labeling means for a non-training message)"),
    ("ROJAS-ESTRADA et al. (2025)", "WEIRD_Democratic", "coding_fidelity",
     "Canonical 1 vs SB 0 for a Peruvian sample. The paper fixes the setting "
     "(students of higher education in Peru, five institutions, p. 158) but "
     "says nothing about the country's regime, so the flag is a convention "
     "question: Peru is an electoral democracy but not one of the liberal "
     "democracies the WEIRD 'democratic' feature is usually read from. The "
     "canonical 1 was kept, because the same record codes Peru as not "
     "Western, not industrialised and not rich (0) - the pairing only makes "
     "sense if 'democratic' is read at the level of electoral regime.",
     "human master (the WEIRD democratic convention)"),
    ("Reznicek (2021)", "Modality", "coding_fidelity",
     "Canonical 4 (mixed) vs SB 5 (other). The treatment was 'four lessons "
     "bundled into a 2-week unit delivered online via the Canvas Online "
     "Learning Management System' (Procedures, p. 37-38); the paper never "
     "reports the media format of the lessons - the 'Curriculum and "
     "Instruction' section named in the table of contents is missing from the "
     "PDF, and no figure appears in the methods chapter. Modality was set to "
     "5 (other: an online course unit whose media formats are not reported). "
     "The canonical's Media_Support = ['Visual aids'] has no basis in the "
     "paper and was left untouched (not in this batch's item list). The "
     "vocabulary asks for 'other' values to be specified in the notes, but "
     "Rationale_Notes was outside this agent's remit - the human master may "
     "want to add that detail.",
     "human master (the note detail for Modality = 5)"),
    ("(corpus-wide)", "WEIRD_Rich / WEIRD_Educated", "coding_fidelity",
     "In this batch the flags that the paper does not support were set to "
     "null (unclear) rather than kept at the canonical's 1: Quinn (2025) - a "
     "US Qualtrics panel with no education and no income data reported "
     "(Methods, p. 16) - and Reznicek (2021) - undergraduates at an urban "
     "public university, no income data (p. 31). Where the paper supports a "
     "value it was kept: ROJAS-ESTRADA (Peru: Rich 0, Educated 1, higher-"
     "education students, p. 158), Powell & Gross (Rich 0 and Educated 0: "
     "rural county, 83.3% of parents below an associate degree, Table 1, "
     "p. 84), Ratajczak (Rich 1: the paper's own 'all Western Educated "
     "Industrialized Rich Democracies (WEIRD)', PDF p. 15). If the corpus "
     "means these flags at the COUNTRY level (US = rich, educated), the two "
     "nulls should go back to 1; if it means them at the SAMPLE level, the "
     "nulls are right and other keys need the same pass.",
     "human master (sample level or country level)"),
    ("Powell & Gross (2018)", "code_available", "coding_fidelity",
     "The canonical coded 0 and documented it ('no repository, no code'); the "
     "SB route also coded 0, the MM route left it blank. The paper makes no "
     "data- or code-availability statement (it names only SPSS v21), so under "
     "the campaign's own rule - 'writing 0 would turn we do not know into no' "
     "(Scripts/PRISMA_REPORT.md) - the field was set to null ('not stated'), "
     "and the one clause of the note that stated the old value was updated "
     "so the record stays internally consistent. The two gap keys in this "
     "batch (ROJAS-ESTRADA 2025, Ridolfi & Vander Wal 2008) stay empty for "
     "the same reason.",
     "human master (0 = not available, or null = not stated)"),
    # --- from the master-agent pass over batch r062_pool
    ("Geegan (2022)", "Fulltext_Exclusion_Criterion", "scope_ambiguity",
     "The field is ABSENT from both Geegan records (a gap, not a disagreement) and it "
     "is on the master agent's do-not-touch list, so the agent did not add it; the "
     "routes disagree (SB ['C1'], MM empty) and the paper does not settle the call. "
     "The study inoculates against ARGUMENTS that "
     "discourage mental-health help-seeking ('college is supposed to be "
     "stressful', 'the counselling centre is prohibitively expensive', "
     "pp. 105-106): the counter-message is a persuasive argument and the "
     "refuted claims are everyday beliefs, not veridical falsehoods, so C1 "
     "('no misinformation intervention') is defensible; but the paper is a "
     "primary English experiment with a no-message control, so C2 and C4 do "
     "not apply and the empty value is defensible too.",
     "human master (inclusion)"),
    ("Geers et al. (2020)", "Fully_Within", "scope_ambiguity",
     "Fully_Within is on the master agent's do-not-touch list, so the agent "
     "left it at the canonical False and did not apply the SB route's True. "
     "The paper supports False: the design has THREE arms - co-creation "
     "(N = 11), watched video (N = 41) and lessons only (N = 49) - and the "
     "analyses are between-subject ('a mixed-model ANOVA examining whether "
     "levels ... differ for the different experimental conditions', p. 45), "
     "so the study is not single-arm. The SB route's True would mark the "
     "experiment as a no-control study. Note also that the 'lessons only' arm "
     "received the same media-literacy lessons, so it is the minimal-dose arm "
     "of the SAME intervention, not a no-misinformation-intervention control.",
     "human master (the fully-within switch)"),
    ("Geers et al. (2020)", "Instructor_Type / Practice_Strategy",
     "coding_fidelity",
     "Instructor_Type stays 2 (Teacher): the paper never presents a researcher "
     "as the instructor (it mentions only the students' own teachers - the "
     "survey items were adapted 'in coordination with the teacher', p. 44), so "
     "the SB route's 1 is unsupported. Practice_Strategy was rewritten to "
     "['Self-explanation/reflection', 'Media production']: the co-creation arm "
     "produced the fake-news videos, and in the watched-video arm 'watching the "
     "videos was embedded in a discussion about the creation of news and "
     "misinformation' (p. 44); the paper describes no practice exercise with "
     "feedback, so the canonical's 'Guided practice' was dropped. The SB route "
     "had the same list; the MM route had only 'Media production'.",
     "already applied by the master agent"),
    ("Gawronski et al. (2022)", "n_arms", "scope_ambiguity",
     "The canonical holds n_arms = 6 (3 Message conditions x 2 Measurement "
     "Order conditions, pp. 584, 590) while the SB and MM routes both hold 3. "
     "The measurement order is a counterbalancing nuisance factor, so the "
     "paper's arms are the three messages (neutral message, debunking-via-"
     "negation, debunking-via-alternative / -affirmation). n_arms is on the "
     "master agent's do-not-touch list and was not on the batch's item list, so "
     "the agent left it at 6.",
     "human master (n_arms)"),
    ("Gawronski et al. (2022)", "N_Inference_Method", "stale_note",
     "Both records hold N_Inference_Method = 3 ('Other') while the note "
     "described the inference as an equal split (2), and "
     "Scripts/check_rationale_consistency.py flags the key for it. The agent "
     "refreshed the prose so it matches the record and describes the inference "
     "accurately (an approximate equal split of the analysed N; the paper "
     "prints no realised cell size, and its '300 in each of the three Debunking "
     "conditions' is the pre-exclusion design target). The field itself was "
     "not on the batch's item list, so the value was left at 3; a human may "
     "prefer 2 ('Equal split of total N').",
     "human master (the inference method)"),
    ("Genereux (2016)", "Intervention_Duration", "numeric_error",
     "The dissertation gives THREE durations for the same instruction: "
     "'approximately 40 minutes of media literacy instruction' and the TAVV "
     "administered 'immediately prior to the 40-minute media literacy "
     "instruction' (methods, pp. 38 and 40), 'one hour of instruction' "
     "(executive summary p. viii and methodology overview p. 36) and 'the same "
     "45-minute instruction on media literacy' (discussion p. 86). The agent "
     "wrote 40, the value the METHODS state twice; the SB route's 45 comes "
     "from the discussion and the MM route's 40 agrees with the methods. The "
     "paper should be picked once, corpus-wide, for this study.",
     "human master (the stated duration)"),
    ("Genereux (2016)", "Discernment_vs_General_Skepticism", "scale_question",
     "The agent wrote 3 ('No significant effect'), following the sibling "
     "media-literacy study Geers et al. (2020) and the paper's own conclusion "
     "that neither the media-literacy instruction nor the video project "
     "increased media-literacy knowledge (abstract, p. viii). The opposite "
     "answer is defensible: the outcome is a media-analysis knowledge score "
     "with NO true/false contrast, so neither selective discernment nor "
     "induced general skepticism is measurable - the reasoning the register "
     "records for Feuerstein (2002), where the field was set to null. The "
     "SB route left it blank. The field was a pool item (not on the do-not-touch "
     "list), so the agent settled it; a human may prefer null.",
     "human master (the field's meaning for a knowledge outcome)"),
    ("Genereux (2016)", "Instructor_Type", "scope_ambiguity",
     "Not on the batch's item list, so the agent left the canonical's 2 "
     "(Teacher). The paper says the participants came from 'a technology "
     "course taught by the researcher and from a writing course taught by a "
     "colleague' (abstract, p. viii), i.e. the INTERVENTION arm was taught by "
     "the researcher (1), while the control arm's course was taught by the "
     "colleague. The MM route coded 1, the SB route 2.",
     "human master (who delivered the intervention)"),
    # --- batch r107_pool: Qi (2026), Qian (2025), Qian et al. (2023),
    #     Ragsdale (2026), Raich et al. (2010)
    ("Qian et al. (2023)", "effect_favors (rows 2 and 3, outside the batch's "
     "row list)", "pool_change",
     "The two 'perceived credibility of misattributed visual posts' rows "
     "carried effect_favors = 'group2', which contradicts the sign of their "
     "own oriented effect (yi x polarity_sign: -0.113 x -1 = +0.113). The "
     "agent set both to 'group1': Table 2 (p. 8) reports B = -0.090 (active) "
     "and B = -0.113 (passive) for the misattributed posts, i.e. the "
     "intervention arms judged the misinformation posts as LESS credible, so "
     "the intervention arm is the favoured one. The rows 0-1 the batch listed "
     "were left as they are (row 0 'group1' follows B = +0.018, row 1 "
     "'group2' follows B = -0.070). The rows were not in the batch's item "
     "list, so a human may prefer to leave them for the corpus-wide sweep "
     "(the field is not read by the analysis, so no pooled estimate moves).",
     "human master (the rows outside this batch)"),
    ("Qian et al. (2023)", "effect_size_type_reported (rows 0-5)",
     "scale_question",
     "The paper reports OLS B coefficients (Table 2, p. 8), so the reported "
     "type is 'beta', but the record keeps 'd' because the pooled yi/vi on "
     "these rows are the pipeline's STANDARDISED d (e.g. row 0: yi = 0.024, "
     "vi = 0.0068, derived from t = 0.290 with n1 = 267, n2 = 324; the raw "
     "B = 0.018 and SE = 0.062 are kept in beta_val/beta_se and in the "
     "conversion audit). The standardisation guard reads the REPORTED type, "
     "so writing 'beta' here would make it refuse the standardised yi/vi and "
     "the row would then pool as a RAW beta (b = 0.018) beside d/g/z - the "
     "same tension as Choi et al. (2023) and Tamboer et al. (2024 a), in the "
     "opposite direction. The agent therefore kept 'd'. The paper's own type "
     "is preserved in the effect_type_audit block of conversion_inputs.",
     "human master (the pooling metric)"),
    ("Qian (2025)", "dataset_availability_status / data_url / data_repository",
     "coding_fidelity",
     "The record carried dataset_availability_status = 1 (Open URL) with "
     "data_url = the Chapter 2 PREREGISTRATION (osf.io/ra3zd) and "
     "data_repository = 'OSF ra3zd'. The dissertation prints no data-"
     "availability statement for any chapter (the only OSF links are the "
     "three preregistrations: ra3zd for Ch. 2, t8eau for Ch. 3 and eysgq for "
     "Ch. 4), and the record's own Rationale_Notes already said 'DATA: no "
     "dataset repository/URL (4)'. The agent set dataset_availability_status "
     "= 4 (not stated), data_url = null and data_repository = null, so the "
     "three fields now agree with the note and with the paper. The "
     "preregistration of this study (Ch. 3) is osf.io/t8eau, which the "
     "record keeps in preregistration_url.",
     "human master (a data statement per chapter)"),
    ("Qian (2025)", "the number of records (experiment count)",
     "scope_ambiguity",
     "The dissertation has THREE studies (Chapters 2-4): Study 1 is the "
     "published Qian, Shen & Zhang (2023) trial, Study 2 is the incentive "
     "experiment and Study 3 is a scale-validation study with no "
     "misinformation intervention (N = 1,498 across three national samples; "
     "it contains an EFA and a CFA sub-study, which is why the SB route "
     "counted four). The canonical route codes ONE record (Study 2) by "
     "design, as its Rationale_Notes document: Ch. 2 is already coded under "
     "the key 'Qian et al. (2023)' and re-coding it here would double-count "
     "one trial, and Ch. 4 has no misinformation intervention. The agent did "
     "not add records; a human may want a Chapter 4 record carrying "
     "Fulltext_Exclusion_Criterion = C1 (that field is the human master's).",
     "human master (the scope of the key)"),
    ("Ragsdale (2026)", "preregistration_url / preregistration_status",
     "coding_fidelity",
     "The canonical and SB routes carry the OSF registration "
     "https://osf.io/m3py2/ with preregistration_status = 3, MM carries null "
     "and 1 (none). The DISSERTATION PRINTS NO PREREGISTRATION STATEMENT: a "
     "sweep of all 255 PDF pages finds no 'm3py2', no 'preregis*', no 'Open "
     "Science Framework' and no OSF link (the only osf.io string in the file "
     "is a PsyArXiv reference on p. 186). The paper therefore cannot settle "
     "the field; the agent kept the canonical's value, which rests on the "
     "coder's external check of the OSF project (SB: 'materials only - no "
     "participant data'). If the human prefers the paper-only reading, the "
     "record should read null / 1.",
     "human master (the paper vs the OSF project)"),
    ("Ragsdale (2026)", "Fulltext_Exclusion_Criterion (both records)",
     "scope_ambiguity",
     "Neither canonical record carries a Fulltext_Exclusion_Criterion at all "
     "(the key is absent, not empty), while both agent routes set C1 on every "
     "record: Study 1 is a moral-reframing persuasion experiment with no "
     "misinformation to correct or pre-bunk, and Study 2 inoculates against "
     "anti-needle-exchange messages (a topic-based inoculation, not a "
     "misinformation intervention). The field is on the master's do-not-touch "
     "list, so the agent left it; the human decides whether the key's rows "
     "may stay in the pools.",
     "human master (C1 for this key)"),
    ("Ragsdale (2026)", "effect_favors (record 1, row 3, outside the batch's "
     "item list)", "pool_change",
     "The conservative-frame donation row carried effect_favors = 'group1' "
     "while its stored effect is negative (yi = -0.0662, dv_polarity = 1), "
     "so the sign and the label contradicted each other. The agent set it to "
     "'group2': Table 10 (p. 116) reports b = -0.12, OR = 0.89 (ns) for the "
     "conservative argument, i.e. the conservative-frame inoculation LOWERED "
     "donation willingness relative to the neutral-information control, "
     "while its liberal counterpart (b = 0.12, OR = 1.13) favours the "
     "inoculation. The field is not read by the analysis, so no pooled "
     "estimate moves; the row was not in the batch's item list, so a human "
     "may prefer to leave it for the corpus-wide sweep.",
     "human master (the rows outside this batch)"),
    ("Ragsdale (2026)", "Exposure_Schedule (record 1)", "coding_fidelity",
     "The inoculation record carries Exposure_Schedule = 3 (boosted: a "
     "delayed follow-up session), but the study has no booster - the "
     "inoculation was delivered at Time 1 and the outcome was measured at "
     "Time 2, three days later, in the same survey wave. 2 (spaced) would "
     "describe it better. The field was not in the batch's item list, so the "
     "agent left it at 3.",
     "human master (the exposure schedule)"),
    ("Qi (2026)", "contrast_type (rows 0-2)", "pool_change",
     "The canonical codes the three eligible fact-check-vs-control rows as "
     "contrast_type = 9 (informative message vs control); SB and MM code 1 "
     "(intervention vs no-treatment control). The record's own "
     "control_group_type is 5 (misinformation-only control), and "
     "Scripts/check_contrast_control_consistency.py treats a contrast_type of "
     "1 as a contradiction unless the control is untreated at measurement "
     "(control_group_type 1 or 4), so 1 would flag the key. The agent kept 9, "
     "which fits a corrective warning label against a control that saw the "
     "misinformation only. The rows carry no yi/vi (the paper reports no "
     "per-cell n), so no pooled estimate moves either way.",
     "human master (the contrast-type convention)"),
    ("Qi (2026)", "code_available / Attention_Cues / Comm_Strategies",
     "coding_fidelity",
     "SB coded code_available = 0, Attention_Cues = ['Visual Signaling/"
     "Highlighting'] and Comm_Strategies = ['Verbal labelling']; the "
     "canonical carries null, [] and []. The agent kept the canonical: the "
     "dissertation prints no code- or data-availability statement (null = "
     "not stated, not 'not available'), and the intervention is a WRITTEN "
     "warning label ('False information. Reviewed by Professional Human "
     "Fact-checkers' / '... Artificial Intelligence Algorithms', p. 61), so "
     "no visual-highlighting or verbal-labelling technique is delivered. "
     "MM's route omits the two agent codes entirely.",
     "human master (the blank-vs-zero convention)"),
    ("Raich et al. (2010)", "Exposure_Schedule", "coding_fidelity",
     "The canonical codes Exposure_Schedule = 2 (spaced, multiple "
     "continuous sessions), SB leaves it empty. The agent kept 2: the paper "
     "says the pre-test was carried out 'one week before the intervention' "
     "and the post-test 'on the last day of the programme' (abstract, p. 49; "
     "Method, p. 52), which is a multi-session school programme, not one "
     "massed session. The number of sessions and the session length are NOT "
     "reported, which is why Intervention_Duration and Instructor_Type stay "
     "empty; if the human wants a blank whenever the session count is "
     "unreported, the field should be null.",
     "human master (the exposure schedule)"),
    ("Spearing et al. (2026) / Smith et al. (2025) / Smith & Seitz (2019) / "
     "Tandoc & Seet (2023)", "WEIRD_Educated / WEIRD_Rich / WEIRD_Democratic",
     "coding_fidelity",
     "Batch r163 (master agent) settled the WEIRD dimensions against the "
     "papers. Where the paper REPORTS the sample characteristic, the paper "
     "decides: Smith & Seitz (2019) prints the education distribution "
     "(Table 1, p. 799: 41.1% bachelor, 9.4% master, 1.5% professional, 0.9% "
     "doctorate in the analysed sample), so WEIRD_Educated = 1; Steinke et "
     "al. (2007) states the sample is seventh-grade students (abstract, PDF "
     "p. 1 / journal p. 35), so WEIRD_Educated = 1; Tandoc & Seet (2023) "
     "recruited 100 college students (p. 776) in Study 1 and 60 secondary / "
     "junior-high students (p. 777) in Study 2, so WEIRD_Educated = 1 and 0. "
     "Where the paper is SILENT the canonical value was kept, and the gap is "
     "recorded here: Spearing et al. (2026) collects age and gender only "
     "(Experiment 1, Participants) and Smith et al. (2025) collects age, "
     "Brexit identity strength and political orientation (p. 3), yet both "
     "keep WEIRD_Educated = 1, although the MM route leaves both null and the "
     "Bogart & Markman (2025) flag set WEIRD_Educated = null when the paper "
     "reports no education data. Smith & Seitz (2019) prints no income data, "
     "so WEIRD_Rich = 1 is an inference from the US sample, and Tandoc & Seet "
     "(2023) keeps WEIRD_Democratic = 1 for the Singaporean samples (an "
     "electoral but not a liberal democracy), where MM leaves it null. A "
     "corpus-wide rule - 'WEIRD_Educated = 1 only where the paper reports "
     "education' - would flip those two records to null and would also make "
     "Steinke (seventh graders = 1) inconsistent with Tandoc Exp 2 "
     "(secondary students = 0).",
     "human master (the WEIRD convention)"),
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
