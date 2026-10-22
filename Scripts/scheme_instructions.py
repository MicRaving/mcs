"""The campaign's per-field coding instructions, as DATA.

The scheme's field model has two prose slots:

* ``description`` - **what** the field is.  Every field has one; it came from
  the legacy v3.1 schema.
* ``instruction`` - **how** to code it and where to look in the paper.  These
  instructions already existed: they are the campaign's own coding prompt,
  written for the coding agents, in

    * ``Scripts/MM_CODING_INSTRUCTIONS.md``   (MM route + PDF marking)
    * ``Scripts/SB_CODING_INSTRUCTIONS.md``   (SB route)
    * ``Scripts/MARKING_CODING_INSTRUCTIONS.md`` (marking wave)
    * ``AGENTS.md``                            (the corresponding-author rules)

  This module transcribes them into one line per field, so that every agent
  (and the desktop app) codes the campaign's actual rules instead of a generic
  description.

Two rules shaped the transcription:

* A field with **no** specific rule gets **no** entry.  An empty instruction is
  honest; an invented one is not.
* The rules that apply to a whole GROUP of fields (the metadata, the
  table-based numerics, the booleans, the lists, the absence codings) are
  written out **per field**, so the agent reads one line per field instead of a
  rule it has to generalise.

The DV-level rules (the effect routes, the contrast rules, the ``dv_polarity``
meaning, the timepoint rules) are attached to the metric fields they are
about: the effect routes onto the fields that participate in them, the
``dv_polarity`` meaning onto ``dv_polarity`` and onto the fields whose
polarity depends on the domain, the timepoint rules onto ``timepoint_1`` /
``timepoint_2`` and the ``contrast_type`` code list onto ``contrast_type``.

The module is stdlib-only and holds no imports from the rest of the scheme
package, so it can be read on its own.
"""

from __future__ import annotations

#: ``{field name: instruction}`` - the campaign's rule for that field.
INSTRUCTIONS: dict[str, str] = {}

#: ``{field name: source key}`` - which source passage the instruction came
#: from.  ``SOURCES`` describes the passages.
SOURCE_OF: dict[str, str] = {}

#: The source passages the instructions were transcribed from.
#:
#: ``key`` -> ``(document, heading, first line, last line, note)``.  The line
#: numbers refer to the documents as they stand in the campaign checkout.
SOURCES: dict[str, tuple] = {
    "record": (
        "MM_CODING_INSTRUCTIONS.md",
        "## One record per experiment",
        31, 41,
        "The record shape, the required fields and the Study_ID naming rule.",
    ),
    "exclusion": (
        "MM_CODING_INSTRUCTIONS.md / SB_CODING_INSTRUCTIONS.md",
        "Set the common field `Fulltext_Exclusion_Criterion` / "
        "## Full-text inclusion criteria (C1-C4) - required on every coding",
        43, 51,
        "The C1-C4 codes and the rule that a failed criterion never excuses a "
        "thin coding.",
    ),
    "effect_routes": (
        "MM_CODING_INSTRUCTIONS.md / SB_CODING_INSTRUCTIONS.md",
        "## Effect routes (a DV row must carry at least one, or a reason)",
        53, 67,
        "The eight routes that make a DV row poolable.",
    ),
    "contrast": (
        "MM_CODING_INSTRUCTIONS.md / SB_CODING_INSTRUCTIONS.md",
        "## Contrast rules / ## Intervention-vs-control contrasts - preference order",
        69, 93,
        "When `eligible_primary` may be 1, the preference order for the "
        "effect, and the `contrast_type` code list.",
    ),
    "granularity": (
        "MARKING_CODING_INSTRUCTIONS.md",
        "## Granularity & eligibility (lessons from the pilot wave)",
        22, 38,
        "Code every reported outcome; one row per contrast x outcome x "
        "timepoint; multi-arm and both-arms-taught designs.",
    ),
    "dv_rules": (
        "MM_CODING_INSTRUCTIONS.md / MARKING_CODING_INSTRUCTIONS.md",
        "## DV coding rules",
        95, 110,
        "The `dv_domain` and `score_types` vocabularies, the meaning of "
        "`dv_polarity` and the timepoint rules.",
    ),
    "polarity": (
        "SB_CODING_INSTRUCTIONS.md",
        "## Other rules - *Polarity.*",
        88, 92,
        "Polarity is derived per DV, not assumed; a rating where higher means "
        "more belief in / sharing of false information is `higher_is_worse`.",
    ),
    "no_invented": (
        "SB_CODING_INSTRUCTIONS.md / MARKING_CODING_INSTRUCTIONS.md",
        "## Other rules - *No invented numbers.* / **No invented numbers.**",
        88, 94,
        "Every effect comes from the paper or the deposit, else a reason.",
    ),
    "marking_dv": (
        "MM_CODING_INSTRUCTIONS.md / MARKING_CODING_INSTRUCTIONS.md",
        "## Marking every DV (required)",
        112, 138,
        "The quote must be verbatim; every DV needs a `source_span`.",
    ),
    "marking_record": (
        "MM_CODING_INSTRUCTIONS.md / MARKING_CODING_INSTRUCTIONS.md",
        "## Marking every record-level variable (required)",
        140, 170,
        "The per-kind source rules: metadata -> page 1, a boolean/derived "
        "coding -> the sentence stating the underlying fact, a list-valued "
        "field -> a supporting passage, a table-based numeric -> the table "
        "cell/row, an absence coding -> the nearest relevant statement plus a "
        "note in Rationale_Notes.",
    ),
    "estimation": (
        "MM_CODING_INSTRUCTIONS.md / MARKING_CODING_INSTRUCTIONS.md",
        "## Estimation uncertainty",
        172, 177,
        "Prefer an exact derivation; an estimated value needs its assumption "
        "and its size, and only enters the effect when the leeway is <= 10%.",
    ),
    "corresponding_author": (
        "AGENTS.md",
        "## Corresponding author",
        260, 285,
        "The contact is recorded in two separate fields plus a provenance "
        "note; a candidate counts only when the name is one of the paper's own "
        "authors, and an address is never invented from a domain pattern.",
    ),
    "module": (
        "MM_CODING_INSTRUCTIONS.md / MARKING_CODING_INSTRUCTIONS.md",
        "## Marking every record-level variable (required)",
        140, 170,
        "The per-kind source rules applied to the intervention-module fields, "
        "plus what each module field looks for in the paper.",
    ),
}


def _add(source_key, entries):
    """Add one group of instructions and remember where they came from."""
    for name, text in entries.items():
        if name in INSTRUCTIONS:
            raise ValueError("two instructions for the field %r" % name)
        INSTRUCTIONS[name] = " ".join(text.split())
        SOURCE_OF[name] = source_key


# --------------------------------------------------------------------------- #
# The record: one object per experiment
# --------------------------------------------------------------------------- #

_add("record", {
    "Study_ID": (
        "Study_ID = first-author surname + year + `_ExpN`, ASCII only, e.g. "
        "`Austin2025_Exp1`, `Jolley2014_Exp9`; a two-study paper uses `_Exp1`, "
        "`_Exp2`. One record per trial/experiment, and the record's "
        "`DV_Metrics` hold that experiment's own contrasts."
    ),
    "Parent_Study_ID": (
        "Bookkeeping key - no source span required. Leave it null for a "
        "top-level record: every record now describes one experiment and "
        "carries its own DV contrasts."
    ),
    "Year": (
        "Metadata field: read the publication year from page 1 of the paper "
        "(the title block or the citation line)."
    ),
    "Publication": (
        "Metadata field: read the publication form from page 1 (the journal "
        "line, the thesis statement or the preprint banner). Use only the "
        "codes in the options list."
    ),
    "DOI": (
        "Metadata field: read the DOI from page 1 (the header, the first-page "
        "footer or the journal citation line). Leave it null when the paper "
        "prints no DOI."
    ),
    "Authors": (
        "Metadata field: read the author list from page 1 and write it as "
        "`Last, First; Last, First`."
    ),
    "Title": (
        "Metadata field: read the title from page 1, verbatim."
    ),
    "Study_Description": (
        "Required on every record. Write two sentences describing the study "
        "design and the primary aim, taken from the paper's abstract and "
        "method section. Bookkeeping key - no source span required."
    ),
    "Rationale_Notes": (
        "Record here every inference the paper does not state: the group-size "
        "inference and its method, the effect-size conversion and its "
        "assumption, the paper's silence on an absence coding, and any design "
        "fact the fields cannot carry (e.g. a fully-within design). "
        "Bookkeeping key - no source span required. Empty string when there is "
        "nothing to note."
    ),
})

_add("exclusion", {
    "Fulltext_Exclusion_Criterion": (
        "Leave it EMPTY when the study meets every inclusion criterion. "
        "Otherwise list the unmet criterion/criteria and only those: "
        "C1 = no misinformation intervention, C2 = no control/comparison "
        "group, C3 = not English, C4 = not a primary experimental study. If "
        "the study meets C1-C4 but reports no eligible intervention-vs-control "
        "contrast (e.g. every arm is treated), leave the field empty and "
        "explain the design fact in Rationale_Notes - never write a label such "
        "as 'insufficient reporting' here. A failed criterion never excuses a "
        "thin coding: still code every experiment and every DV normally."
    ),
})

_add("corresponding_author", {
    "Corresponding_Author_Name": (
        "The person the paper directs correspondence to, e.g. 'Jane Roe'; null "
        "when the paper names no corresponding author. A candidate counts only "
        "when the name is one of the paper's own authors, so an editor, a "
        "production office, a repository mailbox or an IRB contact is never "
        "mistaken for the contact. Bookkeeping key - no source span required."
    ),
    "Corresponding_Author_Email": (
        "The e-mail address as printed in the paper; null when no address is "
        "printed or none resolves to the corresponding author. Never invent an "
        "address from a domain pattern - null plus "
        "'paper: no corresponding author named' is the correct answer for a "
        "paper that prints no address."
    ),
    "Corresponding_Author_Source": (
        "Where the contact was read from, e.g. 'pdf: correspondence note, "
        "p. 1', 'openalex: corresponding_author_ids', or "
        "'paper: no corresponding author named' when the paper names none. "
        "All three corresponding-author fields are record-level and appear on "
        "every record."
    ),
})


# --------------------------------------------------------------------------- #
# The sample
# --------------------------------------------------------------------------- #

_add("marking_record", {
    "N_Recruited": (
        "A table-based numeric field: mark the table cell/row (or the "
        "participant-flow sentence) that reports how many were recruited "
        "before exclusions. Leave it null when the paper does not state it."
    ),
    "N_Analyzed": (
        "A table-based numeric field: mark the table cell/row (or the "
        "participant-flow sentence) that reports the final analysed sample. "
        "Leave it null when the paper does not state the analysed N."
    ),
    "N_Reported_Per_Condition": (
        "1 = the paper reports the cell size of every condition explicitly, "
        "0 = the cell sizes must be inferred, null = unclear. When you set 0, "
        "say in Rationale_Notes how the cell sizes were obtained."
    ),
    "N_Inference_Method": (
        "1 = the cell sizes are reported, 2 = an equal split of the total N "
        "was assumed, 3 = another derivation - then specify it in "
        "Rationale_Notes."
    ),
    "Age_Mean": (
        "A table-based numeric field: mark the table cell/row that reports the "
        "mean age. Leave it null when the paper reports only a median, a "
        "range or a category."
    ),
    "Age_SD": (
        "A table-based numeric field: mark the table cell/row that reports the "
        "SD of age; null when the paper reports only the mean."
    ),
    "Age_Reported_As": (
        "1 = mean/SD, 2 = median/range, 3 = categorical band, 4 = not "
        "reported. This field says how the paper reports age; Age_Mean and "
        "Age_SD carry the numbers."
    ),
    "Age_Group": (
        "1 = children, 2 = adolescents, 3 = adults, 4 = mixed. Derive it from "
        "the sample the paper describes; null when it cannot be determined."
    ),
    "Gender_Ratio": (
        "A table-based numeric field: mark the table cell/row that reports the "
        "gender breakdown, and write it as an object mapping the paper's own "
        "gender labels to the reported percentages or counts. Null if not "
        "reported."
    ),
    "Gender_Reported_As": (
        "1 = the paper reports percentages, 2 = counts, 3 = not reported. "
        "Gender_Ratio carries the numbers."
    ),
    "Covariates": (
        "A list-valued field: mark a passage that supports the listed values - "
        "one entry per variable the paper controlled for, with the DV it "
        "belongs to and the reported correlation when the paper gives one. "
        "Empty array if the paper controls for nothing."
    ),
    "Country": (
        "A list-valued field: mark a passage that supports the listed values - "
        "one country string per country the sample was drawn in. Empty array "
        "if the paper does not report the country."
    ),
    "Sample_Region": (
        "A free-text field: write the region or description the paper gives "
        "where a country does not apply (e.g. 'international English-speaking', "
        "'Sub-Saharan Africa'). Leave it empty when the paper does not report "
        "one - never invent a value."
    ),
    "International_Sample": (
        "A boolean/derived coding: mark the sentence that states the "
        "underlying fact - the sample spans more than one country (1) or a "
        "single country (0). Null when the paper does not make it clear."
    ),
    "WEIRD_Western": (
        "A boolean/derived coding: mark the sentence that states the "
        "underlying fact (the sample's Western status, or the country and "
        "region it comes from), not the code itself. Null when the paper does "
        "not make it clear."
    ),
    "WEIRD_Educated": (
        "A boolean/derived coding: mark the sentence that states the "
        "underlying fact (the sample's education level, or the country and "
        "region it comes from), not the code itself. Null when the paper does "
        "not make it clear."
    ),
    "WEIRD_Industrialized": (
        "A boolean/derived coding: mark the sentence that states the "
        "underlying fact (the sample's industrialized status, or the country "
        "and region it comes from), not the code itself. Null when the paper "
        "does not make it clear."
    ),
    "WEIRD_Rich": (
        "A boolean/derived coding: mark the sentence that states the "
        "underlying fact (the sample's income status, or the country and "
        "region it comes from), not the code itself. Null when the paper does "
        "not make it clear."
    ),
    "WEIRD_Democratic": (
        "A boolean/derived coding: mark the sentence that states the "
        "underlying fact (the sample's democratic status, or the country and "
        "region it comes from), not the code itself. Null when the paper does "
        "not make it clear."
    ),
    "Setting": (
        "Mark the sentence or the method section that states where the study "
        "ran: 1 lab, 2 field, 3 classroom, 4 online panel survey, 5 online "
        "experiment, 6 simulated field, 7 other."
    ),
})


# --------------------------------------------------------------------------- #
# Open science and design
# --------------------------------------------------------------------------- #

_add("marking_record", {
    "dataset_availability_status": (
        "1 = the paper gives an open URL, 2 = the data are available on "
        "request, 3 = the data are not available, 4 = not stated. An absence "
        "coding: for 4, mark the nearest relevant statement and say in "
        "Rationale_Notes that the paper contains no explicit statement."
    ),
    "data_url": (
        "A free-text field: copy the direct URL to the dataset exactly as the "
        "paper prints it. Leave it empty when there is none - never "
        "reconstruct a URL."
    ),
    "data_repository": (
        "A free-text field: name the repository and, where the paper gives "
        "one, its identifier (e.g. 'OSF ujwy3', 'Dryad', 'Zenodo'). Leave it "
        "empty when the paper names no repository."
    ),
    "data_available": (
        "Not paper-coded: the pipeline sets this to 1 when a harmonized "
        "participant-level dataset exists in `Datasets/<Study Key>/` and to 0 "
        "otherwise. Leave it empty when you code."
    ),
    "code_available": (
        "1 = the paper offers analysis code, 0 = it does not, null = not "
        "stated. A boolean/derived coding: mark the sentence that states the "
        "underlying fact (the availability statement, a repository link or the "
        "open-science badge)."
    ),
    "preregistration_status": (
        "1 = no preregistration, 2 = registered but no URL given, 3 = "
        "registered with a URL. An absence coding: for 1, mark the nearest "
        "relevant statement (e.g. the open-science statement) and say in "
        "Rationale_Notes that the paper contains no explicit statement of a "
        "preregistration."
    ),
    "preregistration_url": (
        "Copy the preregistration URL exactly as the paper prints it. Leave it "
        "empty when the paper registers without a URL (preregistration_status "
        "= 2) or does not register at all."
    ),
    "ethics": (
        "0 = the paper does not mention ethics, 1 = it mentions ethics "
        "approval, 2 = it names the institution, 3 = it gives the institution "
        "and the protocol number. Mark the sentence (or the ethics-approval "
        "footnote) that states it."
    ),
    "consent_reported": (
        "1 = the paper reports that informed consent was obtained, 0 = it does "
        "not, null = not stated. A boolean/derived coding: mark the sentence "
        "that states the underlying fact (the consent sentence in the "
        "participants or ethics section)."
    ),
    "n_arms": (
        "Count the independent experimental groups/conditions the experiment "
        "has, including the control arm. The number decides how many eligible "
        "primary contrasts the experiment can carry - one per intervention arm "
        "against the shared no-intervention control."
    ),
    "design_factors": (
        "A list-valued field: mark a passage that supports the listed values "
        "- one string per manipulated factor, e.g. ['inoculation', "
        "'misinformation']. Empty array when the design is single-factor."
    ),
    "factor_levels": (
        "A list-valued field: mark a passage that supports the listed values "
        "- one entry per factor with its levels, e.g. [{'factor': "
        "'inoculation', 'levels': ['yes', 'no']}]."
    ),
    "within_between": (
        "1 = between-subjects, 2 = within-subjects, 3 = mixed. Mark the "
        "sentence that states how participants were allocated."
    ),
    "unit_of_analysis": (
        "1 = participant, 2 = item/headline, 3 = dyad/group, 4 = other. Mark "
        "the sentence that states the unit the analysis treats as the "
        "observation - and set 2 when the paper analyses headlines or claims "
        "as the data points."
    ),
    "clustering": (
        "A free-text field: write the clustering/random-effects structure the "
        "paper uses, e.g. 'standard errors clustered by post'. Leave it empty "
        "when the paper does not state one - never invent a value."
    ),
    "control_group_type": (
        "1 = no-treatment/passive, 2 = placebo (unrelated content), 3 = active "
        "(alternative intervention), 4 = waitlist, 5 = misinformation-only, 6 = "
        "other. Mark the sentence that describes the control arm. A contrast "
        "is eligible_primary = 1 only when this control receives no "
        "misinformation intervention of its own."
    ),
})

_add("record", {
    "Fully_Within": (
        "Set it true when the experiment has no control arm at all (every arm "
        "is treated, or a single-arm pre-post design). A fully-within "
        "experiment is excluded from every pooled product by default, and its "
        "within-person pre-post change is coded as contrast_type = 4. Record "
        "the design fact in Rationale_Notes."
    ),
})


# --------------------------------------------------------------------------- #
# The intervention
# --------------------------------------------------------------------------- #

_add("granularity", {
    "Intervention_Type": (
        "A list-valued field: mark a passage that supports the listed values "
        "- one code per intervention the experiment delivers; a multi-type "
        "experiment is allowed. When the paper separates the components, keep "
        "the `Interventions[]` arms explicit and bind every DV to its arm with "
        "`Intervention_ID`."
    ),
})

_add("marking_record", {
    "Intervention_Type_Detail": (
        "REQUIRED when Intervention_Type includes 'other': write the combined "
        "or specific approach the paper used. Otherwise leave it empty."
    ),
    "Intervention_Topic": (
        "A free-text field: write the topic or domain the intervention "
        "addressed (e.g. vaccines, climate change). Leave it empty when the "
        "paper does not report one - never invent a value."
    ),
    "Intervention_Duration": (
        "A table-based numeric field: mark the sentence or table cell that "
        "reports the exposure time in minutes. Leave it null when the paper "
        "does not state a duration."
    ),
    "Modality": (
        "1 = text, 2 = video, 3 = game/gamified, 4 = mixed, 5 = other (say "
        "which in the notes), 6 = personal training. Mark the method-section "
        "sentence that describes the delivery channel."
    ),
    "Exposure_Schedule": (
        "1 = a single massed session, 2 = spaced over several continuous "
        "sessions, 3 = boosted (a delayed follow-up session). Mark the "
        "procedure-section sentence that describes the schedule."
    ),
})


# --------------------------------------------------------------------------- #
# Instruction and learning
# --------------------------------------------------------------------------- #

_add("marking_record", {
    "Pedagogy_Type": (
        "1 = direct instruction, 2 = guided inquiry, 3 = co-created/"
        "collaborative. Mark the procedure sentence that describes how the "
        "material was taught."
    ),
    "Instructor_Type": (
        "1 = researcher, 2 = teacher, 3 = peer, 4 = computer, 5 = "
        "anthropomorphic agent, 6 = other. Mark the sentence that names who "
        "delivered the intervention."
    ),
    "Feedback_Mechanics": (
        "0 = none, 1 = knowledge of results (correct/incorrect), 2 = "
        "elaborated (an explanation), 3 = forced mastery. Mark the procedure "
        "sentence that describes the feedback given during the intervention."
    ),
    "Attention_Cues": (
        "A list-valued field: mark a passage that supports the listed values "
        "- every cue the intervention used to hold attention: visual "
        "signalling/highlighting, explicit ignore instructions, "
        "physiological/emotional focus."
    ),
    "Affective_Design": (
        "0 = neutral, 1 = intentional negative trigger, 2 = humour/positive "
        "reinforcement. Mark the sentence that describes the affective framing "
        "of the intervention."
    ),
    "Media_Support": (
        "A list-valued field: mark a passage that supports the listed values "
        "- the media supports the intervention used, e.g. ['Model teaching', "
        "'Visual aids']."
    ),
    "Comm_Strategies": (
        "A list-valued field: mark a passage that supports the listed values "
        "- the communication strategies used, e.g. ['Interview techniques', "
        "'Sketching/narrative', 'Verbal labelling']."
    ),
    "ICAP_Role": (
        "1 = passive, 2 = active, 3 = constructive, 4 = interactive. Mark the "
        "sentence that describes how far the intervention made the learner "
        "engage with the material."
    ),
    "Social_Context": (
        "1 = individual, 2 = prosocial, 3 = competitive. Mark the sentence "
        "that describes whether the intervention was done alone, in a "
        "prosocial setting or in a competitive one."
    ),
    "Retrieval_Practice": (
        "0 = absent, 1 = present. Mark the procedure sentence that shows "
        "whether the intervention made learners retrieve the material (e.g. "
        "practice tests, recall prompts)."
    ),
    "Contrasting_Cases": (
        "0 = no, 1 = yes - the intervention contrasted manipulative with "
        "neutral examples. Mark the passage that shows the contrast."
    ),
    "Concept_Labeling": (
        "0 = implicit, 1 = explicit. Mark the passage that shows whether the "
        "intervention named the concept explicitly."
    ),
    "Interleaving": (
        "0 = blocked, 1 = interleaved. Mark the passage that shows whether the "
        "material or the practice was mixed or blocked."
    ),
    "Practice_Strategy": (
        "A list-valued field: mark a passage that supports the listed values "
        "- the practice strategies used, e.g. ['Recall tests', 'Easy-to-hard "
        "sequencing']."
    ),
    "Self_Efficacy": (
        "0 = no, 1 = yes - the intervention explicitly supported self-efficacy "
        "(e.g. mastery feedback, efficacy statements). Mark the passage that "
        "shows it."
    ),
})


# --------------------------------------------------------------------------- #
# Stimuli
# --------------------------------------------------------------------------- #

_add("marking_record", {
    "Synthetic": (
        "1 = synthetic stimuli, 2 = real stimuli, 3 = mixed. Mark the method "
        "sentence that says how the stimuli were made."
    ),
    "Stimuli": (
        "1 = headlines, 2 = claims, 3 = source, 4 = headline+claim, 5 = "
        "claim+source, 6 = headline+source, 7 = video, 8 = images, 9 = other. "
        "Mark the stimuli-section sentence (or the table heading) that names "
        "the material."
    ),
    "Stimuli_Topic": (
        "A free-text field: write the topic or domain of the stimuli, e.g. "
        "vaccines, climate change. Leave it empty when the paper does not "
        "report one - never invent a value."
    ),
})


# --------------------------------------------------------------------------- #
# The DV: what the outcome is
# --------------------------------------------------------------------------- #

_add("dv_rules", {
    "dv_name": (
        "Name the dependent variable as the paper names it, e.g. 'Accuracy "
        "(true minus false headlines)'. One row per distinct contrast x "
        "outcome x timepoint, so the name may repeat across rows; keep it "
        "recognisable."
    ),
    "dv_domain": (
        "Controlled list: technique_recognition, credibility, "
        "sharing_intention, search_behavior, attitude_policy, trust, "
        "media_literacy_attitudes, critical_thinking, other. Do not code "
        "confidence, certainty or metacognition as a dv_domain - they are a "
        "separate strand that is never pooled, so they belong in 'other'."
    ),
    "score_types": (
        "Controlled list: separate_true, separate_false, "
        "separate_manipulative, separate_nonmanipulative, "
        "discernment_difference, composite, signal_detection_auc, "
        "signal_detection_d_prime, signal_detection_c, dichotomous_accuracy, "
        "continuous_accuracy, proportion, proportion_difference, "
        "behavioral_choice, other, count, single_item, behavioral_intention, "
        "change_score. Use separate_true / separate_false when the paper "
        "reports a score per stimulus veracity, and discernment_difference "
        "when the DV is the difference between them."
    ),
    "discernment_defined_as": (
        "Write how the difference/discernment score is computed, e.g. "
        "'manipulative minus non-manipulative'. Null when the DV is not a "
        "difference score. The veracity of the two components decides the "
        "sign of dv_polarity."
    ),
})

_add("polarity", {
    "dv_polarity": (
        "1 = higher is better/protective, 0 = higher is worse, null = not "
        "applicable. Derive it per DV, never assume it: more discernment, "
        "more credibility for TRUE news, LESS sharing of FALSE news and more "
        "analytic processing are better (1). A rating where higher means more "
        "belief in or sharing of false information is 0. The polarity depends "
        "on the domain and on the stimulus veracity, so read dv_domain and "
        "score_types before answering."
    ),
})


# --------------------------------------------------------------------------- #
# The DV: the scale
# --------------------------------------------------------------------------- #

_add("dv_rules", {
    "scale_type": (
        "1 = limited Likert, 2 = slider/continuous, 3 = proportion/"
        "percentage, 4 = count, 5 = signal-detection index, 6 = dichotomous, "
        "7 = other."
    ),
    "scale_points": (
        "The number of response-scale points the DV used (e.g. 7 for a 7-point "
        "Likert). Null when the DV is not a scale."
    ),
    "scale_min": (
        "The minimum possible score or value of the DV."
    ),
    "scale_max": (
        "The maximum possible score or value of the DV."
    ),
})


# --------------------------------------------------------------------------- #
# The DV: the contrast
# --------------------------------------------------------------------------- #

_add("granularity", {
    "comparison_label": (
        "Label the specific contrast and timepoint, e.g. 'Good Bad Vaxx vs "
        "Control (post)'."
    ),
    "group1_label": (
        "The name of the first group in the comparison, as the paper names it "
        "- the group the n1i/m1i/sd1i values and the t/F statistics belong "
        "to."
    ),
    "group2_label": (
        "The name of the second group in the comparison, as the paper names it "
        "- the control arm for an eligible primary contrast."
    ),
})

_add("contrast", {
    "contrast_type": (
        "1 = primary treatment vs no-treatment control, 2 = treatment vs "
        "active/placebo control, 3 = dose-response, 4 = within-person, 5 = "
        "factorial main effect, 6 = simple effect, 7 = other, 8 = "
        "misinformation vs no misinformation, 9 = informative message vs "
        "control, 10 = factorial interaction, 11 = omnibus model term (never "
        "eligible), 12 = moderator main effect, 13 = non-intervention factor."
    ),
    "eligible_primary": (
        "1 = the eligible primary meta-analytic contrast: the primary "
        "intervention vs a NO-misinformation-intervention control. 0 = "
        "everything else: active comparators, dose contrasts, within-person "
        "training gains, moderator terms, omnibus terms, interactions, "
        "descriptives and arm-vs-arm contrasts. Never code an omnibus term or "
        "a decomposition more than once as eligible."
    ),
})

_add("dv_rules", {
    "timepoint_1": (
        "Put the measured timepoint in timepoint_1: 'post' for the immediate "
        "posttest, 'pre' for a pretest, 'followup_<n>' for a later "
        "measurement. Never pool a follow-up as an immediate posttest and "
        "never treat a pretest row as an outcome."
    ),
    "timepoint_2": (
        "The comparison timepoint when the contrast is a change (e.g. 'post' "
        "when timepoint_1 = 'pre'), null for a cross-sectional single-time "
        "contrast. A pretest row is not an outcome: use it only as a pre-post "
        "change."
    ),
    "posttest_type": (
        "1 = immediate, 2 = delayed, 3 = both, 4 = cross-sectional, 5 = other."
    ),
    "delay_weeks": (
        "The numeric delay in weeks between the intervention and the measured "
        "outcome; null if only immediate effects are reported."
    ),
})


# --------------------------------------------------------------------------- #
# The DV: the effect
# --------------------------------------------------------------------------- #

_add("effect_routes", {
    "n1i": (
        "Part of the 'descriptives' effect route (n1i, m1i, sd1i, n2i, m2i, "
        "sd2i): a DV row that carries all six is poolable on its own, and "
        "this is the PREFERRED route. The sample size of group 1 at the "
        "tested timepoint."
    ),
    "m1i": (
        "Part of the 'descriptives' effect route (n1i, m1i, sd1i, n2i, m2i, "
        "sd2i) - the preferred, always poolable route. The mean of group 1 at "
        "the tested timepoint."
    ),
    "sd1i": (
        "Part of the 'descriptives' effect route (n1i, m1i, sd1i, n2i, m2i, "
        "sd2i) - the preferred, always poolable route. The SD of group 1 at "
        "the tested timepoint."
    ),
    "n2i": (
        "Part of the 'descriptives' effect route (n1i, m1i, sd1i, n2i, m2i, "
        "sd2i) - the preferred, always poolable route. The sample size of "
        "group 2 at the tested timepoint (the control arm for an eligible "
        "primary contrast)."
    ),
    "m2i": (
        "Part of the 'descriptives' effect route (n1i, m1i, sd1i, n2i, m2i, "
        "sd2i) - the preferred, always poolable route. The mean of group 2 at "
        "the tested timepoint."
    ),
    "sd2i": (
        "Part of the 'descriptives' effect route (n1i, m1i, sd1i, n2i, m2i, "
        "sd2i) - the preferred, always poolable route. The SD of group 2 at "
        "the tested timepoint."
    ),
    "n1i_pre": (
        "The pretest sample size of group 1, read from the table cell that "
        "reports it. A pre-post row carries the six pretest descriptives "
        "(n1i_pre, m1i_pre, sd1i_pre, n2i_pre, m2i_pre, sd2i_pre) and the six "
        "posttest ones; that pair is the 'descriptives' effect route - the "
        "preferred, always poolable one."
    ),
    "m1i_pre": (
        "The pretest mean of group 1, read from the table cell that reports "
        "it. A pre-post row carries the six pretest descriptives and the six "
        "posttest ones; that pair is the 'descriptives' effect route."
    ),
    "sd1i_pre": (
        "The pretest SD of group 1, read from the table cell that reports it. "
        "A pre-post row carries the six pretest descriptives and the six "
        "posttest ones; that pair is the 'descriptives' effect route."
    ),
    "n2i_pre": (
        "The pretest sample size of group 2, read from the table cell that "
        "reports it. A pre-post row carries the six pretest descriptives and "
        "the six posttest ones; that pair is the 'descriptives' effect route."
    ),
    "m2i_pre": (
        "The pretest mean of group 2, read from the table cell that reports "
        "it. A pre-post row carries the six pretest descriptives and the six "
        "posttest ones; that pair is the 'descriptives' effect route."
    ),
    "sd2i_pre": (
        "The pretest SD of group 2, read from the table cell that reports it. "
        "A pre-post row carries the six pretest descriptives and the six "
        "posttest ones; that pair is the 'descriptives' effect route."
    ),
    "n1i_post": (
        "The posttest sample size of group 1, read from the table cell that "
        "reports it. A pre-post row carries the six pretest descriptives and "
        "the six posttest ones; that pair is the 'descriptives' effect route - "
        "the preferred, always poolable one."
    ),
    "m1i_post": (
        "The posttest mean of group 1, read from the table cell that reports "
        "it. A pre-post row carries the six pretest descriptives and the six "
        "posttest ones; that pair is the 'descriptives' effect route."
    ),
    "sd1i_post": (
        "The posttest SD of group 1, read from the table cell that reports it. "
        "A pre-post row carries the six pretest descriptives and the six "
        "posttest ones; that pair is the 'descriptives' effect route."
    ),
    "n2i_post": (
        "The posttest sample size of group 2, read from the table cell that "
        "reports it. A pre-post row carries the six pretest descriptives and "
        "the six posttest ones; that pair is the 'descriptives' effect route."
    ),
    "m2i_post": (
        "The posttest mean of group 2, read from the table cell that reports "
        "it. A pre-post row carries the six pretest descriptives and the six "
        "posttest ones; that pair is the 'descriptives' effect route."
    ),
    "sd2i_post": (
        "The posttest SD of group 2, read from the table cell that reports it. "
        "A pre-post row carries the six pretest descriptives and the six "
        "posttest ones; that pair is the 'descriptives' effect route."
    ),
    "t_val": (
        "Part of the 't' effect route: t_val + (n1i and n2i, or n_total). A "
        "balanced two-arm design may give only n_total (n1 = n2 = n_total / "
        "2)."
    ),
    "F_val": (
        "Part of the '1-df F' effect route: F_val + df_num + df_den + (n1i and "
        "n2i, or n_total). Only df_num == 1 is a two-arm contrast."
    ),
    "df_num": (
        "The numerator df of the F test - part of the '1-df F' effect route. "
        "df_num > 1 means a multi-df omnibus: it compares more than two cells "
        "at once, cannot be signed, and must never be converted into a single "
        "two-arm d nor be the eligible primary effect."
    ),
    "df_den": (
        "The denominator df of the F test - part of the '1-df F' effect route "
        "(F_val + df_num + df_den + n)."
    ),
    "r_val": (
        "Part of the 'r' effect route: r_val + n_total."
    ),
    "n_total": (
        "Part of the 't' and 'r' effect routes (t_val + n_total, r_val + "
        "n_total), and the fallback for a balanced two-arm design, where "
        "n1 = n2 = n_total / 2. A DV row with no complete effect route must "
        "carry a non-empty missing_metrics_reason."
    ),
    "yi": (
        "Part of the 'reported effect' effect route: yi + (vi or sei)."
    ),
    "vi": (
        "The variance of the effect size - part of the 'reported effect' "
        "effect route (yi + vi or sei)."
    ),
    "sei": (
        "The standard error of the effect size - part of the 'reported effect' "
        "effect route (yi + vi or sei)."
    ),
    "d_val": (
        "Part of the 'standardised d/g' effect route: d_val or g_val + (n1i "
        "and n2i, or n_total)."
    ),
    "g_val": (
        "Part of the 'standardised d/g' effect route: d_val or g_val + (n1i "
        "and n2i, or n_total)."
    ),
    "beta_val": (
        "Part of the 'regression coefficient' effect route: beta_val + "
        "(beta_se, sei or vi) or n_total."
    ),
    "beta_se": (
        "The standard error of the regression coefficient - part of the "
        "'regression coefficient' effect route."
    ),
    "odds_ratio": (
        "Part of the 'odds ratio' effect route: odds_ratio + or_ci_low + "
        "or_ci_high."
    ),
    "or_ci_low": (
        "The lower bound of the odds-ratio confidence interval - part of the "
        "'odds ratio' effect route."
    ),
    "or_ci_high": (
        "The upper bound of the odds-ratio confidence interval - part of the "
        "'odds ratio' effect route."
    ),
})

_add("no_invented", {
    "z_val": (
        "The exact z statistic the paper reports (e.g. logistic or ordinal "
        "regression). It is not one of the eight effect routes on its own: "
        "report it, but the row still needs a route it can be pooled by."
    ),
    "chi2_val": (
        "The exact chi-square statistic the paper reports. A multi-df chi2 is "
        "an omnibus: it is never the eligible primary effect and must not be "
        "converted into a single two-arm effect."
    ),
    "log_odds": (
        "The log-odds coefficient the paper reports."
    ),
    "risk_ratio": (
        "The risk ratio the paper reports."
    ),
    "p_val": (
        "The exact p-value, or the reported bound when the p-value is "
        "censored. A p-value alone is not an effect: leave the effect empty "
        "and record the reason in missing_metrics_reason unless the p-value "
        "came with a usable statistic."
    ),
    "p_censored": (
        "1 = the p-value was reported as an inequality (e.g. p < .001), 0 = an "
        "exact value."
    ),
    "eta2_p": (
        "The partial eta-squared the paper reports. A multi-df eta2 is an "
        "omnibus: it is never the eligible primary effect and must not be "
        "converted into a single two-arm effect."
    ),
    "eta2_p_ci_low": (
        "The lower bound of the partial eta-squared confidence interval."
    ),
    "eta2_p_ci_high": (
        "The upper bound of the partial eta-squared confidence interval."
    ),
    "d_ci_low": (
        "The lower confidence bound the paper reports for d."
    ),
    "d_ci_high": (
        "The upper confidence bound the paper reports for d."
    ),
    "g_ci_low": (
        "The lower confidence bound the paper reports for g."
    ),
    "g_ci_high": (
        "The upper confidence bound the paper reports for g."
    ),
    "d_se": (
        "The standard error the paper reports for d."
    ),
    "g_se": (
        "The standard error the paper reports for g."
    ),
})

_add("effect_routes", {
    "effect_size_type_reported": (
        "Which effect size the paper reported: d, g, eta2p, beta, odds_ratio, "
        "r, none. Read it from the paper - 'none' means the paper reported no "
        "standardised effect, and the row is then refused by the "
        "standardisation guard, so set it to the estimator the paper actually "
        "used."
    ),
})

_add("dv_rules", {
    "effect_favors": (
        "Which group/condition the sign favours, e.g. 'treatment', 'control', "
        "'group1'."
    ),
    "effect_direction_meaning": (
        "What a positive sign means, e.g. 'positive = treatment reduced "
        "misinformation beliefs'."
    ),
})

_add("estimation", {
    "conversion_method": (
        "How yi and vi were obtained, e.g. 'reported d', 't+n to d', 'd to vi "
        "formula'. State any assumption and its size here when a value had to "
        "be estimated (an imputed SD, an F with df_num > 1, a p-value-only "
        "result), and only enter the effect when the leeway is small "
        "(<= 10%)."
    ),
    "conversion_inputs": (
        "The inputs used for the conversion, e.g. {'n1i': 181, 'n2i': 199, "
        "'t_val': 9.2}."
    ),
    "cell_descriptives": (
        "One entry per group-time cell: {'group': string, 'timepoint': "
        "string, 'n': number or null, 'mean': number or null, 'sd': number or "
        "null}."
    ),
    "missing_metrics_reason": (
        "Required when the row carries no usable effect: say concretely what "
        "is missing and name the paragraph or table you searched, e.g. 'only "
        "an omnibus F (df = 2, 3) is reported in the results paragraph; no "
        "pairwise statistics in Table 3'. Never use it for a label such as "
        "'insufficient reporting'. Leave it empty when the row has a usable "
        "effect."
    ),
})


# --------------------------------------------------------------------------- #
# The DV: where it comes from
# --------------------------------------------------------------------------- #

_add("marking_dv", {
    "source_location": (
        "Where the statistic is reported in the paper, e.g. 'Table 2, p. 7', "
        "'Fig. 1', 'Sect. 3.2'. Empty string only when the source is truly "
        "absent."
    ),
    "source_quote": (
        "The verbatim statistic from the paper, e.g. 'd = 0.31, 95% CI [0.12, "
        "0.50]'. Copy it word for word from the paper or Text/<Key>.txt - do "
        "not paraphrase it; it must be findable in the text after whitespace "
        "normalisation. If the quote cannot be found, shorten it to a "
        "contiguous phrase and retry; if it crosses a page break or sits in "
        "an image-only table, quote the table caption or the sentence that "
        "introduces the table."
    ),
    "source_span": (
        "The page and the normalised rectangles of the passage the source "
        "quote was taken from - the object printed by "
        "`python Scripts/mark_source.py \"<Study Key>\" \"<quote>\"`, added "
        "verbatim. Every DV needs one; if a DV is genuinely unmarkable (no "
        "extractable text), say why in missing_metrics_reason and "
        "Rationale_Notes and leave the best span you can (caption or "
        "heading). Check that the page and the rectangles are sensible before "
        "moving on."
    ),
})


# --------------------------------------------------------------------------- #
# The intervention modules
# --------------------------------------------------------------------------- #

_add("module", {
    # -- inoculation ------------------------------------------------------- #
    "Inoculation_Target": (
        "1 = fact-based, 2 = technique-based, 3 = logic-based. Mark the "
        "sentence that describes what the inoculation taught."
    ),
    "Threat_Warning": (
        "0 = absent, 1 = implicit, 2 = explicit forewarning. Mark the sentence "
        "that shows whether a threat was signalled."
    ),
    "Manipulation_Strategy": (
        "A list-valued field: mark a passage that supports the listed values "
        "- the techniques the inoculation refuted, verbatim, e.g. ['Ad "
        "Hominem', 'False Cause']."
    ),
    "Active_vs_Passive": (
        "1 = passive inoculation (reading a text, watching a video), 2 = active "
        "(gamified, interactive role-play), 3 = mixed. Mark the "
        "procedure-section sentence."
    ),
    "Perspective_Taking": (
        "0 = none/neutral, 1 = defender perspective, 2 = misinformation-"
        "creator/evil perspective (e.g. the Bad News game), 3 = both."
    ),
    "Technique_Count": (
        "The number of manipulation techniques explicitly taught or refuted; "
        "null when the paper does not enumerate them."
    ),
    "Technique_Taxonomy": (
        "A list-valued field: mark a passage that supports the listed values "
        "- one standardized name per technique the inoculation refuted, e.g. "
        "['emotional_manipulation', 'fake_experts']."
    ),
    "Forewarning_Explicitness": (
        "0 = absent, 1 = implicit, 2 = explicit. Mark the sentence that shows "
        "how explicitly the threat was forewarned."
    ),
    "Refutation_Present": (
        "1 = an explicit refutation was included, 0 = threat forewarning "
        "only, null = unclear. Mark the sentence that shows the refutation."
    ),
    "Immunizing_Message_Length_Words": (
        "The word count of the inoculation/immunizing text; null when the "
        "paper does not report it. If you count the message the paper prints, "
        "say in Rationale_Notes that the value was counted rather than "
        "reported."
    ),
    "Booster_Session_Administered": (
        "0 = no booster, 1 = a booster was administered at follow-up. Mark the "
        "procedure-section sentence that names the booster session."
    ),
    "Booster_Content": (
        "A free-text field: describe the content of the delayed booster "
        "session. Leave it empty when there was no booster - never invent a "
        "value."
    ),
    "Longevity_Test_Delay_Days": (
        "The number of days between the inoculation and the delayed post-test; "
        "0 when only an immediate test was run."
    ),

    # -- lateral reading and verification ---------------------------------- #
    "Verification_Strategy_Type": (
        "A list-valued field: mark a passage that supports the listed values "
        "- the verification heuristics taught, e.g. ['lateral_reading', "
        "'click_restraint']."
    ),
    "Educational_Format": (
        "1 = classroom curriculum/course module, 2 = short standalone "
        "instructional video, 3 = gamified interactive module, 4 = prompt/"
        "pop-up nudge, 5 = written guidelines/cheatsheet, 6 = self-guided web "
        "platform, 7 = other."
    ),
    "Instructional_Duration_Minutes": (
        "The total exposure in minutes; an estimate is allowed for a "
        "curriculum - say so, and say how large the estimate is, in "
        "Rationale_Notes."
    ),
    "Assessment_Paradigm": (
        "1 = technique-adoption paradigm (authentic web evaluation with screen "
        "recording or digital trace), 2 = controlled website-credibility "
        "judgment task, 3 = self-reported verification-strategy usage, 4 = "
        "knowledge/skill assessment quiz."
    ),
    "Tracked_Behavioral_Indicators": (
        "A list-valued field: mark a passage that supports the listed values "
        "- the tracked metrics, e.g. ['opened_new_tabs', "
        "'time_on_source_domain']."
    ),
    "Target_Audience_Level": (
        "1 = middle/high-school students, 2 = college/university students, 3 = "
        "general adult public, 4 = older adults, 5 = professionals (teachers, "
        "journalists)."
    ),
    "Intervention_Source_Concept": (
        "1 = sourcing, 2 = lateral reading, 3 = both/mixed. Sourcing = "
        "leaving the text to assess the author/publisher via third-party "
        "sources; lateral reading = reading what other sources say about the "
        "source."
    ),
    "Search_Task": (
        "A free-text field: describe the search/evaluation task participants "
        "completed. Leave it empty when the paper does not report one - never "
        "invent a value."
    ),
    "Training_Strategies": (
        "A list-valued field: mark a passage that supports the listed values "
        "- the training strategies used, e.g. ['Explicit strategy "
        "instruction', 'Modeling/demonstration']."
    ),
    "Human_Trainer": (
        "1 = researcher, 2 = teacher, 3 = peer, 4 = computer/AI, 5 = other. "
        "Mark the sentence that names who delivered the training."
    ),

    # -- media-literacy tips ----------------------------------------------- #
    "Tips_Content_Checklist": (
        "A list-valued field: mark a passage that supports the listed values "
        "- the tips actually delivered, e.g. ['be_skeptical_of_headlines', "
        "'inspect_the_url']."
    ),
    "Delivery_Channel": (
        "1 = static graphic/infographic embedded in a feed or survey, 2 = "
        "platform advertisement/sponsored post, 3 = text message or WhatsApp "
        "micro-course, 4 = print pamphlet/flyer, 5 = short video clip, 6 = "
        "other."
    ),
    "Exposure_Frequency": (
        "1 = a single one-off exposure, 2 = multi-day repeated micro-lessons, "
        "3 = an always-accessible sidebar/guide."
    ),
    "Geographic_Setting": (
        "1 = Global North, 2 = Global South, 3 = cross-national, 4 = "
        "other/unclear."
    ),
    "Target_Sample_Literacy": (
        "1 = general urban/internet users, 2 = rural or lower-education and "
        "lower-digital-literacy population, 3 = mixed."
    ),
    "Discernment_vs_General_Skepticism": (
        "1 = selective discernment increased (belief/sharing of false fell, "
        "belief of true preserved), 2 = general scepticism induced (belief in "
        "both true and false fell), 3 = no significant effect. Code it from "
        "the reported pattern, not from the authors' label."
    ),
    "Tip_Topic_Specific": (
        "1 = generic/topic-agnostic tips, 0 = topic-specific tips, null = "
        "unclear."
    ),

    # -- fact-checking training -------------------------------------------- #
    "Fact_Check_Strategy": (
        "A list-valued field: mark a passage that supports the listed values "
        "- the fact-checking strategies taught, e.g. ['claim_decomposition', "
        "'SIFT']."
    ),
    "Training_Focus": (
        "1 = claim/fact verification, 2 = source verification, 3 = both claim "
        "and source, 4 = other."
    ),
    "Training_Format": (
        "1 = direct instruction, 2 = modeling/demonstration, 3 = scaffolded "
        "practice with feedback, 4 = gamified practice, 5 = self-directed "
        "materials, 6 = mixed."
    ),
    "Transfer_Task": (
        "A free-text field: describe any novel-context transfer task. Leave it "
        "empty when the paper reports none - never invent a value."
    ),
    "Feedback_Provided": (
        "1 = corrective/elaborated feedback was given during practice, 0 = no, "
        "null = unclear. Mark the procedure-section sentence that shows the "
        "feedback."
    ),

    # -- open-mindedness --------------------------------------------------- #
    "Mechanism": (
        "1 = actively open-minded thinking (AOT) training, 2 = consider-the-"
        "opposite/counterargument generation, 3 = perspective-taking "
        "induction, 4 = intellectual-humility training, 5 = values "
        "affirmation, 6 = reflection/metacognitive prompt, 7 = other."
    ),
    "Target_Response": (
        "1 = belief updating/receptivity to correction, 2 = myside-bias "
        "reduction, 3 = sharing behaviour, 4 = openness attitudes, 5 = "
        "multiple."
    ),
    "Session_Structure": (
        "1 = a single brief exercise, 2 = multi-session training, 3 = "
        "integrated into the curriculum, 4 = a booster session."
    ),
    "Delivered_Via": (
        "1 = text exercise, 2 = video, 3 = interactive dialogue, 4 = group "
        "discussion, 5 = task/quiz, 6 = mixed."
    ),
    "Outcome_Measures": (
        "A list-valued field: mark a passage that supports the listed values "
        "- the outcome measures the paper reports, e.g. ['AOT scale', 'belief "
        "accuracy']."
    ),

    # -- debunking and rebuttals ------------------------------------------- #
    "Refutation_Type": (
        "1 = topic rebuttal (the facts corrected directly), 2 = technique "
        "rebuttal (rhetorical tactics exposed), 3 = combined topic and "
        "technique rebuttal."
    ),
    "Correction_Structure_Steps": (
        "A list-valued field: mark a passage that supports the listed values "
        "- the debunking steps that are present, e.g. ['fact_first_statement', "
        "'alternative_causal_explanation']."
    ),
    "Alternative_Causal_Explanation_Present": (
        "1 = an explicit alternative causal model was provided, 0 = a simple "
        "retraction/negation only, null = unclear."
    ),
    "Narrative_Format": (
        "1 = narrative (story/case-based) correction, 2 = non-narrative "
        "(factual/list-based), 3 = visual/infographic."
    ),
    "Correction_Source": (
        "1 = professional independent fact-checker, 2 = government/public-"
        "health institution, 3 = peer/social correction, 4 = algorithmic/"
        "platform system message, 5 = anonymous/unattributed source, 6 = "
        "trusted messenger, 7 = other. Mark the sentence that names who "
        "delivered the correction."
    ),
    "Timing_Relative_to_Misinformation": (
        "1 = immediate correction (the same exposure instance), 2 = delayed "
        "correction (a later session or turn), 3 = pre-exposure rebuttal."
    ),
    "Misinformation_Continued_Influence_Assessed": (
        "1 = yes - the design assessed continued influence with open-ended "
        "inferential reasoning or scenario comprehension, 0 = no - only an "
        "isolated belief or accuracy item was measured."
    ),
    "Repeated_Correction": (
        "1 = the same correction was delivered more than once, 0 = no, null = "
        "unclear."
    ),
    "Threat_to_Worldview": (
        "1 = the correction challenges a salient identity or worldview, 0 = "
        "no, null = unclear."
    ),

    # -- community correction ---------------------------------------------- #
    "Crowd_Mechanism": (
        "1 = community notes/context annotations, 2 = peer comment correction, "
        "3 = collective intelligence/judgment platform, 4 = crowd "
        "flagging/reporting, 5 = collective evaluation task, 6 = other."
    ),
    "Visibility": (
        "1 = visible to all users, 2 = visible only to a subgroup, 3 = "
        "collapsed/behind a click, null = unclear."
    ),
    "Primary_Outcome": (
        "1 = belief accuracy, 2 = sharing/engagement, 3 = correction adoption, "
        "4 = discernment, 5 = multiple."
    ),

    # -- source-credibility labels ----------------------------------------- #
    "Rating_System": (
        "1 = continuous journalistic index (e.g. NewsGuard 0-100), 2 = "
        "categorical/colour-coded badge, 3 = binary trust seal or warning "
        "shield, 4 = crowdsourced trust metric, 5 = The Trust Project "
        "transparency indicators, 6 = other."
    ),
    "Unit_of_Evaluation": (
        "1 = domain/news-outlet level, 2 = a specific publication channel or "
        "page, 3 = individual author/journalist."
    ),
    "Interface_Integration_Method": (
        "1 = browser extension/add-on in natural browsing, 2 = in-feed badge "
        "beside the headline, 3 = search-engine results snippet label, 4 = "
        "survey experimental card overlay, 5 = other."
    ),
    "Metric_Transparency_Degree": (
        "1 = a simple badge/score only, 2 = an expandable label with specific "
        "criteria (transparency, corrections, ownership)."
    ),
    "Information_Diet_Quality_Assessed": (
        "1 = yes - the design tracked domain visits or exposure quality via a "
        "digital trace, 0 = no - an isolated headline evaluation only."
    ),
    "Label_Provider": (
        "1 = an independent rating organisation (NewsGuard/Trust Project), 2 = "
        "a platform, 3 = the researchers, 4 = a crowd, 5 = other."
    ),

    # -- warning and fact-checking labels ---------------------------------- #
    "Label_Type": (
        "1 = general warning label, 2 = specific fact-checking label "
        "(fact-checker attribution plus a rating), 3 = contextual information "
        "label without a rating."
    ),
    "Rating_Granularity": (
        "1 = binary ('False'/'Disputed'), 2 = graded/multi-level (e.g. False, "
        "Partly False, Altered, Missing Context), 3 = a numeric scale."
    ),
    "Display_Timing_and_Salience": (
        "1 = an in-line tag below/above the headline, 2 = an interstitial "
        "warning or obscuring overlay, 3 = a post-interaction warning after "
        "the share click."
    ),
    "Fact_Checker_Identity_Visible": (
        "1 = a named independent fact-checker, 2 = a platform message "
        "('Independent fact-checkers'), 3 = an unspecified source."
    ),
    "Implied_Truth_Effect_Tested": (
        "1 = yes - the design tested whether unlabelled false headlines "
        "gained spurious accuracy, 0 = no - only labelled vs control."
    ),
    "Primary_Outcome_Variable": (
        "1 = accuracy/veracity judgments, 2 = sharing intentions / "
        "click-to-share, 3 = belief in the underlying claim, 4 = both accuracy "
        "and sharing."
    ),
    "Audience": (
        "1 = general users, 2 = targeted at sharers, 3 = targeted at "
        "consumers, 4 = both."
    ),

    # -- accuracy prompts -------------------------------------------------- #
    "Prompt_Mechanism": (
        "1 = direct headline accuracy evaluation, 2 = an explicit "
        "prime/reminder, 3 = an educational video/multimedia prime, 4 = a "
        "passive accuracy-salience banner/header, 5 = other prompt format."
    ),
    "Evaluation_Item_Veracity": (
        "1 = true, 2 = false, 3 = neutral/non-political, 4 = both true and "
        "false shown, null = not applicable."
    ),
    "Evaluation_Item_Political_Concordance": (
        "1 = politically concordant, 2 = politically discordant, 3 = "
        "politically neutral/apolitical, null = not applicable."
    ),
    "Prompt_Timing": (
        "1 = before the feed-browsing task, 2 = in-line during feed browsing, "
        "3 = at the immediate point of the sharing decision."
    ),
    "Delivery_Context": (
        "1 = a survey/lab simulation (headline-discernment paradigm), 2 = a "
        "social-media platform direct message (field experiment), 3 = a "
        "platform public feed or interstitial."
    ),
    "Number_of_Prompts": (
        "The total number of accuracy prompts delivered during the "
        "experimental session."
    ),
    "Evaluated_Headline_Domain": (
        "A list-valued field: mark a passage that supports the listed values "
        "- the content domains tested, e.g. ['politics', 'covid-19']."
    ),
    "Primary_Discernment_Metric": (
        "1 = sharing discernment (sharing true minus sharing false), 2 = "
        "accuracy discernment (belief true minus belief false), 3 = raw "
        "false-sharing reduction only, 4 = quality score of the shared domains "
        "(digital-trace data)."
    ),

    # -- friction ---------------------------------------------------------- #
    "Friction_Mechanism": (
        "1 = read-before-sharing prompt, 2 = pause-and-think reflection "
        "prompt, 3 = second-step confirmation modal, 4 = cognitive "
        "justification requirement, 5 = time delay constraint, 6 = other."
    ),
    "Friction_Modality": (
        "1 = non-blocking warning/banner, 2 = a blocking pop-up modal "
        "requiring user input, 3 = a hard delay timer."
    ),
    "Target_Action": (
        "1 = share/retweet action, 2 = post creation/submission, 3 = external "
        "link navigation, 4 = like/upvote."
    ),
    "Content_Targeting_Rule": (
        "1 = universal friction (all links/shares), 2 = an unopened-link "
        "trigger only, 3 = a suspect/flagged-content trigger only."
    ),
    "Primary_Behavioral_Outcome": (
        "1 = sharing intention (self-report), 2 = observed platform sharing "
        "(digital trace), 3 = link click-through/reading rate, 4 = sharing "
        "discernment (the true vs false sharing-rate delta)."
    ),
    "Friction_Disclosed": (
        "1 = yes - the rationale was shown, 0 = silent friction, null = "
        "unclear."
    ),

    # -- social norms ------------------------------------------------------ #
    "Norm_Type": (
        "1 = descriptive norm (what most people do), 2 = injunctive norm (what "
        "is approved/disapproved), 3 = dynamic norm (a trending change in peer "
        "behaviour), 4 = combined descriptive and injunctive."
    ),
    "Norm_Framing": (
        "1 = positive/truth-promoting framing, 2 = negative/"
        "misinformation-discouraging framing."
    ),
    "Reference_Group": (
        "1 = a broad platform peer group, 2 = a national/fellow-citizen group, "
        "3 = ideological in-group peers, 4 = unspecified/generic social "
        "consensus, 5 = a local community."
    ),
    "Delivery_Format": (
        "1 = a statistical normative statement/banner, 2 = a simulated social "
        "counter or upvote ratio, 3 = a peer comment feedback prompt, 4 = an "
        "interactive norm elicitation task."
    ),
    "Primary_Target_Variable": (
        "1 = sharing intentions, 2 = belief in misinformation, 3 = "
        "flagging/reporting suspicious content, 4 = a discernment score."
    ),
    "Ingroup_Congruence": (
        "1 = an ideological in-group source, 2 = an out-group/general source, "
        "3 = not applicable, null = unclear."
    ),

    # -- platform exposure reduction --------------------------------------- #
    "Exposure_Mechanism": (
        "1 = downranking/demotion in feed or search, 2 = removal/deletion of "
        "content, 3 = deplatforming/account removal, 4 = reduced "
        "distribution/reach, 5 = a warning interruption before exposure, 6 = "
        "other."
    ),
    "Platform": (
        "A free-text field: name the platform or system, e.g. 'Facebook', "
        "'Instagram', 'Search', 'YouTube'. Leave it empty when the paper does "
        "not name one - never invent a value."
    ),
    "Targeting_Rule": (
        "1 = untrustworthy sources/domains, 2 = false content items, 3 = an "
        "engagement/reach threshold, 4 = a universal exposure limit, 5 = other."
    ),
    "Assignment_Level": (
        "1 = user-level randomization, 2 = cluster/geography randomization, 3 "
        "= a platform-wide policy, 4 = a natural experiment, 5 = other."
    ),
    "Content_Harm_Level": (
        "1 = low-quality/untrustworthy, 2 = false/misleading, 3 = harmful, 4 = "
        "mixed, null = unclear."
    ),

    # -- generic ----------------------------------------------------------- #
    "Theory_Basis": (
        "A free-text field: name the theory the intervention is based on (e.g. "
        "'inoculation theory', 'elaboration likelihood', 'motivated "
        "reasoning') or write 'none stated'. Never invent a source."
    ),
    "Intervention_Delivery": (
        "1 = direct instruction, 2 = interactive/practice, 3 = game-based, 4 = "
        "other."
    ),
    "Message_Timing": (
        "1 = pre-exposure/prebunking, 2 = concurrent, 3 = post-exposure/"
        "debunking, 4 = mixed."
    ),
    "Correction_Scope": (
        "1 = a specific claim, 2 = technique/generalizable, 3 = general/"
        "abstract, 4 = other."
    ),
    "Intervention_Components": (
        "A list-valued field: mark a passage that supports the listed values "
        "- the components the intervention combined, e.g. ['Correction', "
        "'Warning', 'Skills training']."
    ),

    # -- sourcing ---------------------------------------------------------- #
    "Source_Eval_Strategy": (
        "A free-text field: name the source-evaluation strategy taught, e.g. "
        "'lateral reading', 'click restraint', 'CRAAP'. Leave it empty when "
        "the paper does not state one - never invent a value."
    ),
    "Assessment_Format": (
        "1 = quantitative rating scales, 2 = multiple-choice/forced-choice, 3 "
        "= open response/qualitative, 4 = mixed."
    ),
})


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

def instruction_for(name):
    """The instruction for a field name, or an empty string."""
    return INSTRUCTIONS.get(name, "")


def source_of(name):
    """The source key the field's instruction came from (``""`` if none)."""
    return SOURCE_OF.get(name, "")


def described_fields():
    """The names of the fields that carry an instruction, in scheme order."""
    return list(INSTRUCTIONS)
