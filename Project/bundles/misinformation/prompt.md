# Misinformation Interventions (v3.1) — coding prompt

## Job

You are an expert academic research assistant and data coder. Read the paper thoroughly and code every independent experiment it reports.

## Output contract

Return a **JSON array** with one object per experiment, and nothing else - no prose, no markdown fence:

```json
[
  {
    "<study field>": "value",
    "DV_Metrics": [
      { "<metric field>": "value" }
    ]
  }
]
```

- one array element per **independent experiment**, not per paper; a paper with two experiments has two elements;
- every study field of this scheme appears in every record, even when the paper does not state it - use `null` (or `[]` for a list field) instead of leaving the key out;
- `DV_Metrics` holds one entry per inferential dependent variable and per reported group contrast;
- use the option values exactly as listed below - the label in brackets is the meaning of the value, the value itself is what you write.

## Rules

- **No invented numbers.** Every number must be sourced from the paper (its text, tables or figures). If a number is not reported, leave the field `null` and record a non-empty `missing_metrics_reason` that names what you looked for (the table, figure or paragraph). Never derive, estimate, or carry over a value that the paper does not report.
- **Never leave a contrast unexplained.** A DV row without a complete effect needs a non-empty `missing_metrics_reason`.
- code what the paper says, not what the design implies; leave a field empty rather than guessing.
- cite the source of every value in the source-location field of the DV row (page, table or figure) when the scheme provides one.

## Study fields

### Metadata & Identification

* **Study_ID** — Study ID (required): Unique ID string, [Author][Year]_Exp[1-9]. [role: study_id]
* **Parent_Study_ID** — Parent Study ID: Study_ID of the record one level up in the hierarchy (experiment -> paper/study). Null for a top-level record.  The per-record aggregation level was removed on 2026-09-17; every extraction record now describes one experiment and carries its own DV contrasts. [role: parent_study_id]
* **Year** — Publication Year (required): Publication year. [role: year]
* **Publication**: Merged publication form (one moderator). Controlled vocabulary: paper_peer_reviewed, paper_unreviewed, thesis, dissertation, preprint, working_paper, conference_paper, unpublished_manuscript, report. Splits `paper` into peer-reviewed vs unreviewed. Options: paper_peer_reviewed (Paper (peer-reviewed)), paper_unreviewed (Paper (unreviewed)), thesis, dissertation, preprint, working_paper (Working paper), conference_paper (Conference paper), unpublished_manuscript (Unpublished manuscript), report. [role: publication]
* **DOI**: Digital Object Identifier, or null. [role: doi]
* **Authors** (required): Author names (Last, First), semicolon-separated. [role: authors]
* **Title** (required): Title of the paper. [role: title]
* **Study_Description** — Study Description (required): 2-sentence description of the study design and primary aim. [role: description]
* **Rationale_Notes** — Rationale Notes: Brief note on inferred values, group-size inference, and effect-size conversions. Empty string if none. [role: notes]
* **Fulltext_Exclusion_Criterion** — Full-text exclusion criterion (C1-C4): Leave EMPTY when the study meets every inclusion criterion. Otherwise list the unmet criterion/criteria: C1 = No misinformation intervention; C2 = No control/comparison group; C3 = Not English; C4 = Not a primary experimental study. A study that fails a criterion is STILL fully coded, harmonized and reproduced as normal. Options: C1 (No misinformation intervention (C1)), C2 (No control/comparison group (C2)), C3 (Not English (C3)), C4 (Not a primary experimental study (C4)), insufficient reporting (insufficient reporting (study ran, but no eligible contrast is reported)), fulltext irretrievable (fulltext irretrievable (no obtainable copy)), duplicate of an included study (duplicate of an included study (non-independent report)). [role: fulltext_exclusion_criterion]
* **Corresponding_Author_Name** — Corresponding author (name): Name of the author the paper directs correspondence to, e.g. "Jane Roe". Empty when the paper names no corresponding author. Recorded separately from the address in 'Corresponding_Author_Email'. [role: corresponding_author_name]
* **Corresponding_Author_Email** — Corresponding author (email): E-mail address of the corresponding author, as printed in the paper. Empty when no address is printed or none could be resolved to the corresponding author. [role: corresponding_author_email]
* **Corresponding_Author_Source** — Corresponding author (source): Where the corresponding author was read from, e.g. "pdf: correspondence note, p. 1" or "openalex: corresponding_author_ids". [role: corresponding_author_source]

### Sample

* **N_Recruited** — N Recruited: Number of participants recruited before exclusions, or null. [role: n_recruited]
* **N_Analyzed** — N Analyzed: Final analyzed sample size, or null. [role: sample_n]
* **Age_Mean** — Mean Age: Numeric mean age, or null (leave null when only a median/range/category is reported). [role: age_mean]
* **Age_Group** — Age Group: 1=Children, 2=Adolescents, 3=Adults, 4=Mixed. Null if cannot be determined. Options: 1, 2, 3, 4. [role: age_group]
* **Gender_Ratio** — Gender Ratio: Object mapping gender labels to percentages or counts, e.g. {"female": 59, "male": 40, "non-binary": 1}; null if not reported. [role: gender_ratio]
* **Country** — Countries: Array of country strings, e.g. ["USA", "UK"]. Empty array if not reported. [role: country]
* **International_Sample** — International Sample: 1=sample spans multiple countries, 0=single country, null=unclear. Options: 0, 1. [role: international_sample]
* **WEIRD_Western** — WEIRD - Western: 1=Western, 0=not, null=unclear. Options: 0, 1. [role: weird_western]
* **WEIRD_Educated** — WEIRD - Educated: 1=Educated/high-education, 0=not, null=unclear. Options: 0, 1. [role: weird_educated]
* **WEIRD_Industrialized** — WEIRD - Industrialized: 1=Industrialized, 0=not, null=unclear. Options: 0, 1. [role: weird_industrialized]
* **WEIRD_Rich** — WEIRD - Rich: 1=Rich/high-income, 0=not, null=unclear. Options: 0, 1. [role: weird_rich]
* **WEIRD_Democratic** — WEIRD - Democratic: 1=Democratic, 0=not, null=unclear. Options: 0, 1. [role: weird_democratic]
* **Setting**: 1=Lab, 2=Field, 3=Classroom, 4=Online panel survey, 5=Online experiment, 6=Simulated field, 7=Other. Options: 1, 2, 3, 4, 5, 6, 7. [role: setting]

### Sample detail

* **N_Reported_Per_Condition** — N Reported Per Condition: 1=cell sizes explicitly reported, 0=not reported (must be inferred), null=unclear. Options: 0, 1. [role: n_reported_per_condition]
* **N_Inference_Method** — N Inference Method: 1=Reported, 2=Equal split of total N, 3=Other (specify in Rationale_Notes). Options: 1, 2, 3. [role: n_inference_method]
* **Age_SD** — Age SD: Standard deviation of age, or null. [role: age_sd]
* **Age_Reported_As** — Age Reported As: 1=Mean/SD, 2=Median/Range, 3=Categorical band, 4=Not reported. Options: 1, 2, 3, 4. [role: age_reported_as]
* **Gender_Reported_As** — Gender Reported As: 1=Percentages, 2=Counts, 3=Not reported. Options: 1, 2, 3. [role: gender_reported_as]
* **Covariates**: Array of objects listing variables controlled for, [{"covariate": string, "dv_name": string, "r_val": number|null}]. Empty array if none. [role: covariates]
* **Sample_Region** — Sample Region: Region/description when Country is not applicable (e.g. "international English-speaking", "Sub-Saharan Africa"). [role: sample_region]

### Open Science & Design

* **dataset_availability_status** — Dataset Availability Status: 1=Open URL, 2=Upon request, 3=Not available, 4=Not stated. Options: 1, 2, 3, 4. [role: dataset_availability]
* **data_url** — Data URL: Direct URL to the dataset, or null. [role: dataset_url]
* **data_repository** — Data Repository: Repository name/ID (e.g. OSF ujwy3, Dryad, Zenodo), or null. [role: data_repository]
* **data_available** — Data Available (derived): Derived, not paper-coded: 1 when a harmonized participant-level dataset exists in `Datasets/<Study Key>/`, else 0. Populated by the reanalysis / analysis-suite scripts (`Scripts/suite_common.py:_data_available_map`). [role: data_available]
* **code_available** — Code Available: 1=analysis code available, 0=not, null=not stated. Options: 0, 1. [role: code_available]
* **preregistration_status** — Preregistration Status: 1=None, 2=Registered (no URL given), 3=Registered (URL given). Options: 1, 2, 3. [role: preregistration_status]
* **preregistration_url** — Preregistration URL: URL of the preregistration, or null. [role: preregistration_url]
* **ethics** — Ethics Approval: 0=No mention, 1=Mentioned, 2=Institution named, 3=Institution and protocol number. Options: 0, 1, 2, 3. [role: ethics]
* **consent_reported** — Consent Reported: 1=informed consent reported, 0=not, null=not stated. Options: 0, 1. [role: consent_reported]
* **n_arms** — Number of Arms: Number of independent experimental groups/conditions. [role: n_arms]
* **design_factors** — Design Factors: Array of manipulated factors, e.g. ["inoculation", "misinformation"] or ["credibility_badge", "social_norm"]. [role: design_factors]
* **factor_levels** — Factor Levels: Array of objects, [{"factor": string, "levels": [string]}]. [role: factor_levels]
* **within_between** — Within/Between: 1=Between-subjects, 2=Within-subjects, 3=Mixed. Options: 1, 2, 3. [role: design]
* **unit_of_analysis** — Unit of Analysis: 1=Participant, 2=Item/headline, 3=Dyad/group, 4=Other. Options: 1, 2, 3, 4. [role: unit_of_analysis]
* **clustering** — Clustering: Clustering/random effects structure, e.g. "standard errors clustered by post", or null. [role: clustering]
* **control_group_type** — Control Group Type: 1=No-treatment/Passive, 2=Placebo (unrelated content), 3=Active (alternative intervention), 4=Waitlist, 5=Misinformation-only, 6=Other. Options: 1, 2, 3, 4, 5, 6. [role: control_group_type]

### Intervention

* **Intervention_Type** — Intervention Type: Multi-label list of intervention types from the merged vocabulary: inoculation, prebunking_warning, lateral_reading, credibility_labels, media_literacy_tips, fact_checking_training, openmindedness_intervention, debunking_and_rebuttals, community_correction, accuracy_prompts, friction, social_norms, platform_exposure_reduction, other. Multi-type experiments are allowed (e.g. ["inoculation", "friction"]). The former intervention_module field was removed on 2026-09-17; the module-specific coding fields are now implied by these types. [role: intervention_type]
* **Intervention_Type_Detail** — Intervention Type Detail: REQUIRED when Intervention_Type includes "other"; specify the combined/specific approach. Otherwise null. [role: intervention_detail]
* **Intervention_Topic** — Intervention Topic: Topic or domain of the intervention (e.g. vaccines, climate change), or null. [role: intervention_topic]
* **Intervention_Duration** — Intervention Duration (minutes): Numeric duration of the intervention in minutes, or null. [role: intervention_duration]
* **Modality**: 1=Text, 2=Video, 3=Game/Gamified, 4=Mixed, 5=Other (specify in detail/notes), 6=Personal training. Options: 1, 2, 3, 4, 5, 6. [role: modality]
* **Exposure_Schedule** — Exposure Schedule: 1=Single massed session, 2=Spaced (multiple continuous sessions), 3=Boosted (delayed follow-up session). Options: 1, 2, 3. [role: exposure_schedule]
* **Attention_Cues** — Attention Cues: Array of applicable strings: ["Visual Signaling/Highlighting", "Explicit Ignore Instructions", "Physiological/Emotional Focus"]. [role: attention_cues]

### Instruction & Learning

* **Pedagogy_Type** — Pedagogy Type: 1=Direct Instruction, 2=Guided Inquiry, 3=Co-created/Collaborative. Options: 1, 2, 3. [role: pedagogy_type]
* **Instructor_Type** — Instructor Type: 1=Researcher, 2=Teacher, 3=Peer, 4=Computer, 5=Anthropomorphic Agent, 6=Other. Options: 1, 2, 3, 4, 5, 6. [role: instructor_type]
* **Feedback_Mechanics** — Feedback Mechanics: 0=None, 1=Knowledge of Results (correct/incorrect), 2=Elaborated (explanation), 3=Forced Mastery. Options: 0, 1, 2, 3. [role: feedback_mechanics]
* **Affective_Design** — Affective Design: 0=Neutral, 1=Intentional Negative Trigger, 2=Humor/Positive Reinforcement. Options: 0, 1, 2. [role: affective_design]
* **Media_Support** — Media Support: Array of applicable strings: ["Model teaching", "Visual aids"]. [role: media_support]
* **Comm_Strategies** — Communication Strategies: Array of applicable strings: ["Interview techniques", "Sketching/narrative", "Verbal labelling"]. [role: comm_strategies]
* **ICAP_Role** — ICAP Role: 1=Passive, 2=Active, 3=Constructive, 4=Interactive. Options: 1, 2, 3, 4. [role: icap_role]
* **Social_Context** — Social Context: 1=Individual, 2=Prosocial, 3=Competitive. Options: 1, 2, 3. [role: social_context]
* **Retrieval_Practice** — Retrieval Practice: 0=Absent, 1=Present. Options: 0, 1. [role: retrieval_practice]
* **Contrasting_Cases** — Contrasting Cases: 0=No, 1=Yes (manipulative vs. neutral examples). Options: 0, 1. [role: contrasting_cases]
* **Concept_Labeling** — Concept Labeling: 0=Implicit, 1=Explicit. Options: 0, 1. [role: concept_labeling]
* **Interleaving**: 0=Blocked, 1=Interleaved. Options: 0, 1. [role: interleaving]
* **Practice_Strategy** — Practice Strategy: Array of applicable strings: ["Recall tests", "Easy-to-hard sequencing"]. [role: practice_strategy]
* **Self_Efficacy** — Self-Efficacy Support: 0=No, 1=Yes (explicit support/indicators). Options: 0, 1. [role: self_efficacy]

### Stimuli

* **Synthetic** — Synthetic Stimuli: 1=Synthetic, 2=Real, 3=Mixed. Options: 1, 2, 3. [role: synthetic]
* **Stimuli** — Stimuli Type: 1=Headlines, 2=Claims, 3=Source, 4=Headline+Claim, 5=Claim+Source, 6=Headline+Source, 7=Video, 8=Images, 9=Other. Options: 1, 2, 3, 4, 5, 6, 7, 8, 9. [role: stimuli]
* **Stimuli_Topic** — Stimuli Topic: Topic or domain of the stimuli, or null. [role: stimuli_topic]

## Intervention modules

A module block applies when the record's intervention type routes to it. Fill only the block that applies.

_Psychological Inoculation / Prebunking applies when Intervention_Type is one of: inoculation._

### Psychological Inoculation / Prebunking

* **Inoculation_Target** — Inoculation Target: 1=Fact-based, 2=Technique-based, 3=Logic-based. Options: 1, 2, 3.
* **Threat_Warning** — Threat Warning: 0=Absent, 1=Implicit, 2=Explicit forewarning. Options: 0, 1, 2.
* **Manipulation_Strategy** — Manipulation Strategies: Verbatim list of techniques refuted, e.g. ["Ad Hominem", "False Cause", "Bandwagon", "Emotional storytelling", "Fake experts", "Naturalistic fallacy", "Conspiracy theories", "False balance"].
* **Active_vs_Passive** — Active vs Passive: 1=Passive inoculation (text reading, video viewing), 2=Active inoculation (gamified interactive roleplay), 3=Mixed. Options: 1, 2, 3.
* **Perspective_Taking** — Perspective Taking: 0=None/neutral, 1=Defender perspective, 2=Misinformation creator/evil perspective (e.g. Bad News game), 3=Both. Options: 0, 1, 2, 3.
* **Technique_Count** — Number of Techniques: Number of manipulation techniques explicitly taught/refuted, or null.
* **Technique_Taxonomy** — Standardized Technique Taxonomy: Standardized controlled names: ["emotional_manipulation", "fake_experts", "conspiracy", "polarization", "amplification", "trolling", "naturalistic_fallacy", "ad_hominem", "false_dichotomy", "coherence_break", "discrediting", "scapegoating", "false_balance"].
* **Forewarning_Explicitness** — Forewarning Explicitness: 0=Absent, 1=Implicit, 2=Explicit. Options: 0, 1, 2.
* **Refutation_Present** — Refutation Present: 1=Explicit refutation included, 0=Threat forewarning only, null=Unclear. Options: 0, 1.
* **Immunizing_Message_Length_Words** — Immunizing Message Length (words): Word count of the inoculation/immunizing text, or null.
* **Booster_Session_Administered** — Booster Session Administered: 0=No booster, 1=Booster administered at follow-up. Options: 0, 1.
* **Booster_Content** — Booster Content: Description of any delayed booster session content, or null.
* **Longevity_Test_Delay_Days** — Follow-up Delay (Days): Days between inoculation and delayed post-test, or 0 if immediate only.

_Lateral Reading and Verification Strategies applies when Intervention_Type is one of: lateral_reading_and_verification, sourcing._

### Lateral Reading and Verification Strategies

* **Verification_Strategy_Type** — Strategy Taught: List of verification heuristics taught: ["lateral_reading", "reverse_image_search", "click_restraint", "wikipedia_source_tracking", "original_context_tracing", "source_credibility_check", "fact_checking_site_use", "CRAAP_checklist"].
* **Educational_Format** — Format of Instruction: 1=Classroom curriculum/course modules (teacher modeling and guided practice), 2=Short standalone instructional video, 3=Gamified interactive learning module, 4=Prompt/pop-up nudge during browsing, 5=Written guidelines/cheatsheet, 6=Self-guided web platform, 7=Other. Options: 1, 2, 3, 4, 5, 6, 7.
* **Instructional_Duration_Minutes** — Instruction Duration (Minutes): Total exposure in minutes (or estimate for a curriculum).
* **Assessment_Paradigm** — Assessment Paradigm: 1=Technique-adoption paradigm (authentic web evaluation with screen recording/digital trace), 2=Controlled website credibility judgment task, 3=Self-reported verification-strategy usage, 4=Knowledge/skill assessment quiz. Options: 1, 2, 3, 4.
* **Tracked_Behavioral_Indicators** — Tracked Web Navigation Behaviors: List of tracked metrics: ["opened_new_tabs", "search_engine_queries", "time_on_source_domain", "external_credibility_references"].
* **Target_Audience_Level** — Target Population: 1=Middle/high-school students, 2=College/university students, 3=General adult public, 4=Older adults, 5=Professionals (e.g. teachers, journalists). Options: 1, 2, 3, 4, 5.
* **Intervention_Source_Concept** — Source Concept: 1=Sourcing, 2=Lateral Reading, 3=Both/Mixed. Sourcing = leaving the text to assess author/publisher credibility via third-party sources. Options: 1, 2, 3.
* **Search_Task** — Search Task: Description of the search/evaluation task participants completed, or null.
* **Training_Strategies** — Training Strategies: Array: ["Explicit strategy instruction", "Modeling/demonstration", "Scaffolded practice", "Feedback", "Group discussion"].
* **Human_Trainer** — Human Trainer: 1=Researcher, 2=Teacher, 3=Peer, 4=Computer/AI, 5=Other. Options: 1, 2, 3, 4, 5.

_Media-Literacy Tips applies when Intervention_Type is one of: media_literacy_tips._

### Media-Literacy Tips

* **Tips_Content_Checklist** — Specific Tips Included: Subset of tips: ["be_skeptical_of_headlines", "inspect_the_url", "investigate_the_source", "watch_for_unusual_formatting", "check_the_photos_images", "inspect_dates", "check_evidence_and_references", "cross_check_other_reports", "check_if_satire", "consider_the_source", "look_for_expert_consensus"].
* **Delivery_Channel** — Delivery Channel: 1=Static graphic/infographic embedded in feed/survey, 2=Platform advertisement/sponsored post (e.g. Meta tips campaign), 3=Text message/ WhatsApp micro-course, 4=Print pamphlet/flyer, 5=Short video clip, 6=Other. Options: 1, 2, 3, 4, 5, 6.
* **Exposure_Frequency** — Delivery Schedule: 1=Single one-off exposure, 2=Multi-day repeated micro-lessons (e.g. 5-day SMS), 3=Always-accessible sidebar/guide. Options: 1, 2, 3.
* **Geographic_Setting** — Geographic / Economic Context: 1=Global North, 2=Global South, 3=Cross-national, 4=Other/unclear. Options: 1, 2, 3, 4.
* **Target_Sample_Literacy** — Target Sample Demographics: 1=General urban/internet users, 2=Rural / lower education and digital literacy population, 3=Mixed. Options: 1, 2, 3.
* **Discernment_vs_General_Skepticism** — Evaluated Discernment Effect: 1=Selective discernment increased (lower belief/sharing of false, preserved belief of true), 2=General skepticism induced (decreased belief in both true and false), 3=No significant effect. Options: 1, 2, 3.
* **Tip_Topic_Specific** — Topic-Specific vs General: 1=Generic/topic-agnostic tips, 0=Topic-specific tips, null=Unclear. Options: 0, 1.

_Fact-Checking Training applies when Intervention_Type is one of: fact_checking_training._

### Fact-Checking Training

* **Fact_Check_Strategy** — Strategy Taught: Subset: ["claim_decomposition", "search_operator_use", "fact_checking_website_use", "reverse_image_search", "original_context_tracing", "SIFT", "evidence_tracing", "expert_consensus_lookup"].
* **Training_Focus** — Training Focus: 1=Claim/fact verification, 2=Source verification, 3=Both claim and source, 4=Other. Options: 1, 2, 3, 4.
* **Training_Format** — Training Format: 1=Direct instruction, 2=Modeling/demonstration, 3=Scaffolded practice with feedback, 4=Gamified practice, 5=Self-directed materials, 6=Mixed. Options: 1, 2, 3, 4, 5, 6.
* **Transfer_Task** — Transfer Task: Description of any novel-context transfer task, or null.
* **Feedback_Provided** — Feedback Provided: 1=Yes (corrective/elaborated feedback during practice), 0=No, null=Unclear. Options: 0, 1.

_Open-Mindedness / Actively Open-Minded Thinking applies when Intervention_Type is one of: openmindedness_intervention._

### Open-Mindedness / Actively Open-Minded Thinking

* **Mechanism**: 1=Actively open-minded thinking (AOT) training, 2=Consider-the-opposite/ counterargument generation, 3=Perspective-taking induction, 4=Intellectual-humility training, 5=Values affirmation, 6=Reflection/ metacognitive prompt, 7=Other. Options: 1, 2, 3, 4, 5, 6, 7.
* **Target_Response** — Target Response: 1=Belief updating/receptivity to correction, 2=Myside bias reduction, 3=Sharing behaviour, 4=Openness attitudes, 5=Multiple. Options: 1, 2, 3, 4, 5.
* **Session_Structure** — Session Structure: 1=Single brief exercise, 2=Multi-session training, 3=Integrated into curriculum, 4=Booster session. Options: 1, 2, 3, 4.
* **Delivered_Via** — Delivered Via: 1=Text exercise, 2=Video, 3=Interactive dialogue, 4=Group discussion, 5=Task/quiz, 6=Mixed. Options: 1, 2, 3, 4, 5, 6.
* **Outcome_Measures** — Outcome Measures: Array, e.g. ["AOT scale", "receptiveness to opposing views", "belief accuracy", "sharing intentions"].

_Debunking and Rebuttals applies when Intervention_Type is one of: debunking_and_rebuttals._

### Debunking and Rebuttals

* **Refutation_Type** — Refutation Strategy Type: 1=Topic rebuttal / factual debunking (correcting facts directly), 2=Technique rebuttal (exposing rhetorical tactics/fallacies), 3=Combined topic and technique rebuttal. Options: 1, 2, 3.
* **Correction_Structure_Steps** — Four-Step Structure Compliance: Subset of the 4 debunking steps present: ["fact_first_statement", "salience_warning_pre_exposure", "misinformation_specification_and_flaw_explanation", "alternative_causal_explanation"].
* **Alternative_Causal_Explanation_Present** — Alternative Causal Explanation Present: 1=Explicit alternative causal model provided, 0=Simple retraction/negation only, null=Unclear. Options: 0, 1.
* **Narrative_Format** — Correction Format: 1=Narrative correction (story/case-based), 2=Non-narrative correction (factual/list-based), 3=Visual/infographic. Options: 1, 2, 3.
* **Correction_Source** — Source of Debunking: 1=Professional independent fact-checker, 2=Government/public-health institution, 3=Peer/social correction (user comment), 4=Algorithmic/ platform system message, 5=Anonymous/unattributed source, 6=Trusted messenger (e.g. local health worker), 7=Other. Options: 1, 2, 3, 4, 5, 6, 7.
* **Timing_Relative_to_Misinformation** — Timing of Correction: 1=Immediate correction (same exposure instance), 2=Delayed correction (subsequent session/turn), 3=Pre-exposure rebuttal. Options: 1, 2, 3.
* **Misinformation_Continued_Influence_Assessed** — Continued Influence Effect (CIE) Measured: 1=Yes (open-ended inferential reasoning/scenario comprehension), 0=No (isolated item belief/accuracy rating only). Options: 0, 1.
* **Repeated_Correction** — Repeated Correction: 1=Yes (same correction delivered more than once), 0=No, null=Unclear. Options: 0, 1.
* **Threat_to_Worldview** — Identities/Worldviews Threat: 1=Yes (correction challenges a salient identity/worldview), 0=No, null=Unclear. Options: 0, 1.

_Community / Collective Correction applies when Intervention_Type is one of: community_correction._

### Community / Collective Correction

* **Crowd_Mechanism** — Crowd Mechanism: 1=Community notes/context annotations, 2=Peer comment correction, 3=Collective intelligence/judgment platform, 4=Crowd flagging/reporting, 5=Collective evaluation task, 6=Other. Options: 1, 2, 3, 4, 5, 6.
* **Visibility** — Correction Visibility: 1=Visible to all users, 2=Visible only to a subgroup, 3=Collapsed/behind a click, null=Unclear. Options: 1, 2, 3.
* **Primary_Outcome** — Primary Outcome: 1=Belief accuracy, 2=Sharing/engagement, 3=Correction adoption, 4=Discernment, 5=Multiple. Options: 1, 2, 3, 4, 5.

_Source-Credibility Labels applies when Intervention_Type is one of: source_credibility_labels, warning_and_fact_checking_labels._

### Source-Credibility Labels

* **Rating_System** — Credibility System: 1=Continuous journalistic index (e.g. NewsGuard 0-100), 2=Categorical/ color-coded badge (green trustworthy/red untrustworthy), 3=Binary trust seal/warning shield, 4=Crowdsourced trust metric, 5=The Trust Project transparency indicators, 6=Other. Options: 1, 2, 3, 4, 5, 6.
* **Unit_of_Evaluation** — Evaluated Unit: 1=Domain/news-outlet level, 2=Specific publication channel/page, 3=Individual author/journalist. Options: 1, 2, 3.
* **Interface_Integration_Method** — Interface Integration Method: 1=Browser extension/add-on in natural browsing, 2=In-feed badge beside the headline, 3=Search-engine results page snippet label, 4=Survey experimental card overlay, 5=Other. Options: 1, 2, 3, 4, 5.
* **Metric_Transparency_Degree** — Information Detail Provided: 1=Simple badge/score only, 2=Expandable nutrition label with specific criteria (transparency, corrections, ownership). Options: 1, 2.
* **Information_Diet_Quality_Assessed** — Information Diet Quality Assessed: 1=Yes (tracked domain visits/exposure quality via digital trace), 0=No (isolated headline evaluation only). Options: 0, 1.
* **Label_Provider** — Label Provider: 1=Independent rating org (NewsGuard/Trust Project), 2=Platform, 3=Researchers, 4=Crowd, 5=Other. Options: 1, 2, 3, 4, 5.

_Warning and Fact-Checking Labels applies when Intervention_Type is one of: source_credibility_labels, warning_and_fact_checking_labels._

### Warning and Fact-Checking Labels

* **Label_Type** — Label Type: 1=General warning label (content may be disputed/inaccurate), 2=Specific fact-checking label (fact-checker attribution + rating), 3=Contextual information label (background without rating). Options: 1, 2, 3.
* **Rating_Granularity** — Veracity Rating Granularity: 1=Binary ("False"/"Disputed"), 2=Graded/multi-level (e.g. False, Partly False, Altered, Missing Context), 3=Numeric scale. Options: 1, 2, 3.
* **Display_Timing_and_Salience** — Display Mode: 1=In-line tag below/above headline, 2=Interstitial warning/obscuring overlay (click-through), 3=Post-interaction warning (after share click). Options: 1, 2, 3.
* **Fact_Checker_Identity_Visible** — Fact-Checking Source Disclosed: 1=Named independent fact-checker (PolitiFact, Snopes), 2=Platform message ("Independent fact-checkers"), 3=Unspecified source. Options: 1, 2, 3.
* **Implied_Truth_Effect_Tested** — Implied Truth Effect Tested: 1=Yes (tested whether unlabelled false headlines gained spurious accuracy), 0=No (only labelled vs control). Options: 0, 1.
* **Primary_Outcome_Variable** — Primary Evaluated Outcome: 1=Accuracy/veracity judgments, 2=Sharing intentions/click-to-share, 3=Belief in underlying claim, 4=Both accuracy and sharing. Options: 1, 2, 3, 4.
* **Audience** — Label Audience: 1=General users, 2=Targeted at sharers, 3=Targeted at consumers, 4=Both. Options: 1, 2, 3, 4.

_Accuracy Prompts applies when Intervention_Type is one of: accuracy_prompts._

### Accuracy Prompts

* **Prompt_Mechanism** — Prompt Mechanism: 1=Direct headline accuracy evaluation (rate veracity of a single neutral/unrelated headline), 2=Explicit prime/reminder (text prompt reminding user accuracy matters), 3=Educational video/multimedia prime on the value of accurate information, 4=Passive accuracy salience banner/header, 5=Other prompt format. Options: 1, 2, 3, 4, 5.
* **Evaluation_Item_Veracity** — Veracity of Prompt Item: 1=True, 2=False, 3=Neutral/non-political, 4=Both true and false shown, null=Not applicable. Options: 1, 2, 3, 4.
* **Evaluation_Item_Political_Concordance** — Political Concordance of Prompt Headline: 1=Politically concordant, 2=Politically discordant, 3=Politically neutral/apolitical, null=Not applicable. Options: 1, 2, 3.
* **Prompt_Timing** — Timing of Prompt: 1=Prior to feed browsing task, 2=In-line during feed browsing (periodically), 3=At immediate point of sharing decision. Options: 1, 2, 3.
* **Delivery_Context** — Delivery Context: 1=Survey/lab simulation (headline-discernment paradigm), 2=Social media platform direct message (field experiment, e.g. Twitter DM), 3=Platform public feed/interstitial. Options: 1, 2, 3.
* **Number_of_Prompts** — Prompt Repetition Count: Total number of accuracy prompts delivered during the experimental session.
* **Evaluated_Headline_Domain** — Evaluated Content Domain: Array of content domains tested, e.g. ["politics", "covid-19", "climate_change", "general_science", "conspiracy"].
* **Primary_Discernment_Metric** — Primary Discernment Metric: 1=Sharing discernment (Sharing True minus Sharing False), 2=Accuracy discernment (Belief True minus Belief False), 3=Raw false-sharing reduction only, 4=Quality score of shared domains (digital trace data). Options: 1, 2, 3, 4.

_Friction applies when Intervention_Type is one of: friction._

### Friction

* **Friction_Mechanism** — Friction Mechanism: 1=Read-before-sharing prompt (open/read article link), 2=Pause-and-think reflection prompt, 3=Second-step confirmation modal (extra click), 4=Cognitive justification requirement (write why true/false), 5=Time delay constraint (mandatory wait timer), 6=Other. Options: 1, 2, 3, 4, 5, 6.
* **Friction_Modality** — Modal Interface Type: 1=Non-blocking warning/banner, 2=Blocking pop-up modal requiring user input, 3=Hard delay timer. Options: 1, 2, 3.
* **Target_Action** — Action Subject to Friction: 1=Share/Retweet action, 2=Post creation/submission, 3=External link navigation, 4=Like/Upvote. Options: 1, 2, 3, 4.
* **Content_Targeting_Rule** — Targeting Rule for Friction Trigger: 1=Universal friction (all links/shares), 2=Unopened-link trigger only, 3=Suspect/flagged content trigger only. Options: 1, 2, 3.
* **Primary_Behavioral_Outcome** — Primary Outcome Metric: 1=Sharing intention (self-report), 2=Observed platform sharing (digital trace), 3=Link click-through/reading rate, 4=Sharing discernment (true vs. false sharing-rate delta). Options: 1, 2, 3, 4.
* **Friction_Disclosed** — Friction Disclosed to User: 1=Yes (rationale shown), 0=No (silent friction), null=Unclear. Options: 0, 1.

_Social Norms applies when Intervention_Type is one of: social_norms._

### Social Norms

* **Norm_Type** — Social Norm Type: 1=Descriptive norm (what most people do), 2=Injunctive norm (what is approved/disapproved), 3=Dynamic norm (trending change in peer behaviour), 4=Combined descriptive and injunctive. Options: 1, 2, 3, 4.
* **Norm_Framing** — Normative Framing: 1=Positive/truth-promoting framing, 2=Negative/misinformation-discouraging framing. Options: 1, 2.
* **Reference_Group** — Reference Group Specified: 1=Broad platform peer group, 2=National/fellow-citizen group, 3=Ideological/in-group peers, 4=Unspecified/generic social consensus, 5=Local community. Options: 1, 2, 3, 4, 5.
* **Delivery_Format** — Presentation Format: 1=Statistical normative statement/banner, 2=Simulated social counter/ upvote ratio, 3=Peer comment feedback prompt, 4=Interactive norm elicitation task. Options: 1, 2, 3, 4.
* **Primary_Target_Variable** — Primary Behavioral / Cognitive Outcome: 1=Sharing intentions, 2=Belief in misinformation, 3=Flagging/reporting suspicious content, 4=Discernment score. Options: 1, 2, 3, 4.
* **Ingroup_Congruence** — In-group Congruence of Norm Source: 1=Ideological in-group source, 2=Out-group/general source, 3=Not applicable, null=Unclear. Options: 1, 2, 3.

_Platform Exposure Reduction / Downranking applies when Intervention_Type is one of: platform_exposure_reduction._

### Platform Exposure Reduction / Downranking

* **Exposure_Mechanism** — Exposure Mechanism: 1=Downranking/demotion in feed or search, 2=Removal/deletion of content, 3=Deplatforming/account removal, 4=Reduced distribution/reach, 5=Warning interruption before exposure, 6=Other. Options: 1, 2, 3, 4, 5, 6.
* **Platform**: Platform or system (e.g. "Facebook", "Instagram", "Search", "YouTube"), or null.
* **Targeting_Rule** — Targeting Rule: 1=Untrustworthy sources/domains, 2=False content items, 3=Engagement/reach threshold, 4=Universal exposure limit, 5=Other. Options: 1, 2, 3, 4, 5.
* **Assignment_Level** — Assignment Level: 1=User-level randomization, 2=Cluster/geography randomization, 3=Platform-wide policy, 4=Natural experiment, 5=Other. Options: 1, 2, 3, 4, 5.
* **Content_Harm_Level** — Content Harm Level: 1=Low-quality/untrustworthy, 2=False/misleading, 3=Harmful, 4=Mixed, null=Unclear. Options: 1, 2, 3, 4.

### Generic Misinformation Intervention

* **Theory_Basis** — Theory Basis: Theory the intervention is based on (e.g. "inoculation theory", "elaboration likelihood", "motivated reasoning", "none stated"), or null.
* **Intervention_Delivery** — Intervention Delivery: 1=Direct Instruction, 2=Interactive/Practice, 3=Game-based, 4=Other. Options: 1, 2, 3, 4.
* **Message_Timing** — Message Timing: 1=Pre-exposure/prebunking, 2=Concurrent, 3=Post-exposure/debunking, 4=Mixed. Options: 1, 2, 3, 4.
* **Correction_Scope** — Correction Scope: 1=Specific claim, 2=Technique/generalizable, 3=General/abstract, 4=Other. Options: 1, 2, 3, 4.
* **Intervention_Components** — Intervention Components: Array of applicable strings: ["Correction", "Warning", "Skills training", "Feedback", "Nudge", "Platform downranking/exposure reduction", "Community messaging"].

_Sourcing / Source Evaluation applies when Intervention_Type is one of: lateral_reading_and_verification, sourcing._

### Sourcing / Source Evaluation

* **Source_Eval_Strategy** — Source Evaluation Strategy: The strategy taught - e.g. "lateral reading", "click restraint", "source-checking", "fact-checking site use", "CRAAP".
* **Assessment_Format** — Assessment Format: 1=Quantitative rating scales, 2=Multiple-choice/forced-choice, 3=Open response/qualitative, 4=Mixed. Options: 1, 2, 3, 4.

## Effect sizes (`DV_Metrics`)

One entry per inferential dependent variable (DV) and per reported group contrast, inside the array of the same name.

### DV_Metrics

* **dv_name** — DV Name: Name/label of the dependent variable. [role: dv_name]
* **dv_domain** — DV Domains: Controlled multi-label list: technique_recognition, credibility, sharing_intention, search_behavior, attitude_policy, trust, media_literacy_attitudes, critical_thinking, other.  The former levels echo_chamber, information_overload, social_norms, behavioral_choice and general_transfer were merged into "other" on 2026-09-17; confidence and affect were merged into "other", engagement_behavior into "sharing_intention", and belief_accuracy into "credibility" on 2026-09-19. [role: dv_domain]
* **score_types** — Score Types: Controlled multi-label list: separate_true, separate_false, separate_manipulative, separate_nonmanipulative, discernment_difference, composite, signal_detection_auc, signal_detection_d_prime, signal_detection_c, dichotomous_accuracy, continuous_accuracy, proportion, proportion_difference, behavioral_choice, other, count, single_item, behavioral_intention, change_score, signal_detection_other. [role: score_types]
* **discernment_defined_as** — Discernment Defined As: How a difference/discernment score is computed, e.g. "manipulative minus non-manipulative", or null. [role: discernment]
* **dv_polarity** — DV Polarity: 1=higher score is better/protective, 0=higher score is worse, null=not applicable. Options: 0, 1. [role: polarity]
* **scale_type** — Scale Type: 1=Limited Likert, 2=Slider/continuous, 3=Proportion/percentage, 4=Count, 5=Signal-detection index, 6=Dichotomous, 7=Other. Options: 1, 2, 3, 4, 5, 6, 7. [role: scale_type]
* **scale_points** — Scale Points: Number of response scale points, if applicable. [role: scale_points]
* **scale_min** — Scale Min: Minimum possible score/value. [role: scale_min]
* **scale_max** — Scale Max: Maximum possible score/value. [role: scale_max]
* **comparison_label** — Comparison Label: Label of the specific contrast and timepoint, e.g. "Good Bad Vaxx vs Control (post)". [role: comparison_label]
* **group1_label** — Group 1 Label: First group name in the comparison. [role: group1_label]
* **group2_label** — Group 2 Label: Second group name in the comparison. [role: group2_label]
* **contrast_type** — Contrast Type: 1=Primary treatment vs no-treatment control, 2=Treatment vs active/placebo control, 3=Dose-response, 4=Within-person, 5=Factorial main effect, 6=Simple effect, 7=Other, 8=Misinformation vs no misinformation, 9=Informative message vs control, 10=Factorial interaction, 11=Omnibus model term, 12=Moderator main effect, 13=Non-intervention factor. Options: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13. [role: contrast_type]
* **eligible_primary** — Eligible Primary: 1=eligible primary meta-analytic contrast (intervention vs no-inoculation control), 0=not primary, null=unclear. Options: 0, 1. [role: eligible_primary]
* **timepoint_1** — Timepoint 1: First timepoint label, e.g. "post", "followup_4w", "pre". [role: timepoint_1]
* **timepoint_2** — Timepoint 2: Second timepoint label, or null for cross-sectional single-time contrasts. [role: timepoint_2]
* **posttest_type** — Posttest Type: 1=Immediate, 2=Delayed, 3=Both, 4=Cross-sectional, 5=Other. Options: 1, 2, 3, 4, 5. [role: posttest_type]
* **delay_weeks** — Delay (weeks): Numeric delay in weeks; null if only immediate effects are reported. [role: delay_weeks]
* **n1i_pre** — N Pre Group 1: Pre-test sample size group 1. [role: n1_pre]
* **m1i_pre** — Mean Pre Group 1: Pre-test mean group 1. [role: m1_pre]
* **sd1i_pre** — SD Pre Group 1: Pre-test SD group 1. [role: sd1_pre]
* **n2i_pre** — N Pre Group 2: Pre-test sample size group 2. [role: n2_pre]
* **m2i_pre** — Mean Pre Group 2: Pre-test mean group 2. [role: m2_pre]
* **sd2i_pre** — SD Pre Group 2: Pre-test SD group 2. [role: sd2_pre]
* **n1i_post** — N Post Group 1: Post-test sample size group 1. [role: n1_post]
* **m1i_post** — Mean Post Group 1: Post-test mean group 1. [role: m1_post]
* **sd1i_post** — SD Post Group 1: Post-test SD group 1. [role: sd1_post]
* **n2i_post** — N Post Group 2: Post-test sample size group 2. [role: n2_post]
* **m2i_post** — Mean Post Group 2: Post-test mean group 2. [role: m2_post]
* **sd2i_post** — SD Post Group 2: Post-test SD group 2. [role: sd2_post]
* **n1i** — N Group 1: Sample size group 1 for the tested timepoint (metafor alias). [role: n1]
* **m1i** — Mean Group 1: Mean group 1 for the tested timepoint (metafor alias). [role: m1]
* **sd1i** — SD Group 1: SD group 1 for the tested timepoint (metafor alias). [role: sd1]
* **n2i** — N Group 2: Sample size group 2 for the tested timepoint (metafor alias). [role: n2]
* **m2i** — Mean Group 2: Mean group 2 for the tested timepoint (metafor alias). [role: m2]
* **sd2i** — SD Group 2: SD group 2 for the tested timepoint (metafor alias). [role: sd2]
* **t_val** — t Statistic: Exact t-test statistic. [role: t]
* **F_val** — F Statistic: Exact F-statistic. [role: F]
* **df_num** — df Numerator: Numerator df for F-tests. [role: df_num]
* **df_den** — df Denominator: Denominator df for F-tests. [role: df_den]
* **z_val** — z Statistic: Exact z statistic (e.g. logistic/ordinal regression). [role: z]
* **chi2_val** — Chi-square Statistic: Exact chi-square statistic. [role: chi2]
* **r_val** — r Correlation: Exact correlation coefficient. [role: r]
* **n_total** — Total N: Total N for correlation/statistical fallback conversions. [role: n_total]
* **beta_val** — Regression Beta: Unstandardized or standardized regression coefficient (state which in conversion_method). [role: beta]
* **beta_se** — Regression Beta SE: Standard error of the regression coefficient. [role: beta_se]
* **log_odds** — Log Odds: Log-odds coefficient. [role: log_odds]
* **odds_ratio** — Odds Ratio: Odds ratio. [role: or]
* **or_ci_low** — Odds Ratio CI Low: Lower bound of the odds-ratio confidence interval. [role: or_ci_low]
* **or_ci_high** — Odds Ratio CI High: Upper bound of the odds-ratio confidence interval. [role: or_ci_high]
* **risk_ratio** — Risk Ratio: Risk ratio. [role: rr]
* **p_val** — p Value: Exact p-value, or the reported bound when censored. [role: p]
* **p_censored** — p Censored: 1=reported as an inequality (e.g. p<.001), 0=exact value. Options: 0, 1. [role: p_censored]
* **eta2_p** — Partial Eta Squared: Partial eta-squared for ANOVA effects. [role: eta2p]
* **eta2_p_ci_low** — Partial Eta Squared CI Low: Lower bound of the partial eta-squared CI. [role: eta2p_ci_low]
* **eta2_p_ci_high** — Partial Eta Squared CI High: Upper bound of the partial eta-squared CI. [role: eta2p_ci_high]
* **d_val** — Cohen's d: Pre-calculated Cohen's d. [role: d]
* **g_val** — Hedges' g: Pre-calculated Hedges' g. [role: g]
* **d_ci_low** — d CI Low: Lower CI bound for d. [role: d_ci_low]
* **d_ci_high** — d CI High: Upper CI bound for d. [role: d_ci_high]
* **g_ci_low** — g CI Low: Lower CI bound for g. [role: g_ci_low]
* **g_ci_high** — g CI High: Upper CI bound for g. [role: g_ci_high]
* **d_se** — d SE: Standard error for d. [role: d_se]
* **g_se** — g SE: Standard error for g. [role: g_se]
* **yi** — Effect Size (yi): Standardized effect size estimate used for meta-analysis. [role: yi]
* **vi** — Effect Size Variance (vi): Variance of the effect size. [role: vi]
* **sei** — Effect Size SE (sei): Standard error of the effect size. [role: sei]
* **effect_size_type_reported** — Effect Size Type Reported: Which effect size the paper reported - d, g, eta2p, beta, odds_ratio, r, none. Options: d, g, eta2p, beta, odds_ratio, r, means, none, dprime, c, other. [role: effect_type_reported]
* **effect_favors** — Effect Favors: Which group/condition the sign favors (e.g. "treatment", "control", "group1"). [role: effect_favors]
* **effect_direction_meaning** — Effect Direction Meaning: Interpretation of a positive sign (e.g. "positive = treatment reduced misinformation beliefs"). [role: effect_direction_meaning]
* **conversion_method** — Conversion Method: How yi/vi were obtained, e.g. "reported d", "t+n to d", "d to vi formula". [role: conversion_method]
* **conversion_inputs** — Conversion Inputs: Inputs used for conversion, e.g. {"n1i":181,"n2i":199,"t_val":9.2}. [role: conversion_inputs]
* **cell_descriptives** — Cell Descriptives: Array of objects, one per group-time cell, each {"group": string, "timepoint": string, "n": number|null, "mean": number|null, "sd": number|null}. [role: cell_descriptives]
* **missing_metrics_reason** — Missing Metrics Reason: Short explanation when no full descriptives or fallback statistics are available; empty string otherwise. [role: missing_reason]
* **source_location** — Source Location: Where the statistic is reported in the paper, e.g. "Table 2, p. 7", "Fig. 1", "Sect. 3.2"; empty string when truly absent. [role: source_location]
* **source_quote** — Source Quote: Verbatim statistic from the paper, e.g. "d = 0.31, 95% CI [0.12, 0.50]"; must be findable in Text/<Key>.txt after whitespace normalisation. [role: source_quote]

Metric roles in use: F, beta, beta_se, cell_descriptives, chi2, comparison_label, contrast_type, conversion_inputs, conversion_method, d, d_ci_high, d_ci_low, d_se, delay_weeks, df_den, df_num, discernment, dv_domain, dv_name, effect_direction_meaning, effect_favors, effect_type_reported, eligible_primary, eta2p, eta2p_ci_high, eta2p_ci_low, g, g_ci_high, g_ci_low, g_se, group1_label, group2_label, log_odds, m1, m1_post, m1_pre, m2, m2_post, m2_pre, missing_reason, n1, n1_post, n1_pre, n2, n2_post, n2_pre, n_total, or, or_ci_high, or_ci_low, p, p_censored, polarity, posttest_type, r, rr, scale_max, scale_min, scale_points, scale_type, score_types, sd1, sd1_post, sd1_pre, sd2, sd2_post, sd2_pre, sei, source_location, source_quote, t, timepoint_1, timepoint_2, vi, yi, z.

Preferred effect-size families: descriptives, t, F, r, yi, d_g, odds_ratio, risk_ratio, beta, eta2p, chi2.

A complete effect is one of these role bundles (field names in brackets):

- full descriptives (n1i, m1i, sd1i, n2i, m2i and sd2i)
- t with n1i and n2i
- F with df_num, df_den, n1i and n2i
- r with n_total
- yi with vi or sei
- d or g with n1i and n2i

Never leave a contrast without a complete effect set. Leave a numeric field `null` when the paper does not report it and record why in `missing_metrics_reason` (a non-empty sentence that names the table, figure or paragraph you searched).

## Inclusion criteria

Every **gating** criterion must pass. A study that fails a gating criterion is excluded, but it is still coded in full: record the failing criterion and keep the data you read.

### Abstract screening

- **C1** — Misinformation intervention
  Does the study feature an intervention aimed at reducing susceptibility to misinformation (inoculation/prebunking, debunking, media literacy, source-credibility or lateral-reading training, accuracy nudges, social norms, platform exposure reduction)?
  Pass values: Yes.
- **C2** — Control group
  Does the study include a control, placebo, or comparison group that does NOT receive the misinformation intervention?
  Pass values: Yes.
- **C3** — Language (English)
  Is the paper in English?
  Pass values: Yes.
- **C4** — Experimental (primary quantitative study)
  Is this a primary empirical study with a quantitative experimental or quasi-experimental design?
  Pass values: Yes.

### Full-text exclusion

- **C1** — No misinformation intervention (C1) (not gating)
  No misinformation intervention (C1)
- **C2** — No control/comparison group (C2) (not gating)
  No control/comparison group (C2)
- **C3** — Not English (C3) (not gating)
  Not English (C3)
- **C4** — Not a primary experimental study (C4) (not gating)
  Not a primary experimental study (C4)
- **irretrievable** — Fulltext irretrievable (not gating)
  Fulltext irretrievable
- **no_contrast** — No eligible contrast reported (not gating)
  No eligible contrast reported
- **duplicate** — Duplicate/non-independent report of an included study (not gating)
  Duplicate/non-independent report of an included study
- **observational** — No experimental intervention (observational) (not gating)
  No experimental intervention (observational)
- **domain_mismatch** — Outcome/intervention outside misinformation susceptibility (not gating)
  Outcome/intervention outside misinformation susceptibility
- **other** — Other (specify) (not gating)
  Other (specify)
