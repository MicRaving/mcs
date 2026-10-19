"""Experiment-level exclusions.

Two kinds of excluded *experiment* (the paper/source is kept whenever it contains other
eligible experiments):

1. **Non-independent experiments.**  Some included sources report the *same participants /
   same experiment* as another included source.  Pooling both would violate the
   independence assumption.  Where the duplicated source contains no unique eligible study
   it is excluded at full text (`master_dataset.xlsx`, criterion "Duplicate/non-independent
   report of an included study"; see `Scripts/FULLTEXT_DUPLICATE_LOG.md`).  Where the
   duplicated source also contains unique experiments (a multi-study dissertation), the
   whole source is kept and only the duplicated experiment(s) are dropped here, at the
   analysis layer.
2. **No misinformation intervention (C1).**  Some included papers contain a companion
   experiment that delivers no misinformation intervention (single-arm baseline,
   observational survey, measurement-only preference task).  The paper is kept for its
   other, eligible experiments; the non-intervention experiment is dropped here.  These
   records carry `Intervention_Type = ["other"]` with a `NO_INTERVENTION:` detail; see
   `Scripts/MODERATOR_JSON_FIX.md`.

`Extraction/*.json` and the harmonized datasets are never edited (AGENTS.md); these maps are
applied by `metaanalysis.py` (keys are `Study_ID`) and `reanalysis.py` (keys are the
dataset `artifact` label, where one exists).

Nothing here is heuristic: each entry is a same-sample match (kind 1) or a paper-verified
absence of any intervention (kind 2).
"""

import json
import os

# Full-text exclusions agreed by the coders in Coding Studio are published to
# ``Screening/fulltext_agreed.json``; every stage drops those whole studies.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FULLTEXT_PATH = os.path.join(_ROOT, "Screening", "fulltext_agreed.json")
_FULLTEXT_CACHE = {"mtime": None, "keys": set()}


def fulltext_excluded_keys():
    """Study keys excluded at full text by coder agreement (cached by mtime)."""
    try:
        mtime = os.path.getmtime(_FULLTEXT_PATH)
    except OSError:
        _FULLTEXT_CACHE.update(mtime=None, keys=set())
        return set()
    if _FULLTEXT_CACHE["mtime"] == mtime and _FULLTEXT_CACHE["keys"]:
        return _FULLTEXT_CACHE["keys"]
    keys = set()
    try:
        with open(_FULLTEXT_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            keys = {str(k) for k in data}
    except Exception:
        keys = set()
    _FULLTEXT_CACHE.update(mtime=mtime, keys=keys)
    return keys


def is_fulltext_excluded(study_key):
    return str(study_key) in fulltext_excluded_keys()


# Non-independent thesis/dissertation copies carry a distinct `Diss` Study_ID infix
# (e.g. `CooksonDiss2021`, `MoravecDiss2020`) so that the corpus has no colliding
# `Study_ID`s even though the underlying sample is shared with the peer-reviewed
# article.  The duplicated thesis experiment is then dropped here; an experiment that
# is unique to the thesis is kept and is listed under `RETAINED_UNIQUE`.  See
# `Scripts/DUPFIX_REPORT.md` (pair 1-3) for the per-pair pre-rename labels / evidence.
#
# (study_key, experiment_label) -> dict(reason, duplicate_of, kept)
NONINDEPENDENT_EXCLUSIONS = {
    ("Lutzke (2019)", "LutzkeDiss2019_Exp1"): dict(
        reason=("Non-independent duplicate of Lutzke et al. (2019) Experiment 1: the "
                "University of Michigan thesis reports the identical 3x2 (control / "
                "Guidelines / Enhanced Guidelines) x (fake / real) online experiment with "
                "the same N=2,750 analysed US adults, published as the peer-reviewed Global "
                "Environmental Change article. The thesis contains no unique eligible "
                "experiment, so its only experiment is dropped; the article copy is kept."),
        duplicate_of="Lutzke et al. (2019) Exp1",
        kept="Lutzke et al. (2019) Exp1",
    ),
    ("Moravec (2020)", "MoravecDiss2020_Exp2"): dict(
        reason=("Non-independent duplicate of Moravec et al. (2020) Experiment 2: "
                "dissertation Essay 2 is the same N=398 online experiment crossing four "
                "fake-news flag designs with awareness training (the article reports the "
                "same participants and Table 3/4 believability effects). The dissertation's "
                "unique experiments Exp1 (EEG headline flag, N=83) and Exp3 (NewsGuard "
                "source ratings, N=76) are retained."),
        duplicate_of="Moravec et al. (2020) Exp2",
        kept="Moravec et al. (2020) Exp2",
    ),
    ("Winters (2021)", "WintersDiss2021_Exp1"): dict(
        reason=("Non-independent duplicate of Winters et al. (2021) Experiment 1: thesis "
                "Study IV is the same Contagious Misinformation Trial (three-arm WhatsApp "
                "audio-drama RCT; N=668 analysed of 736) reported in BMJ Global Health. The "
                "thesis's unique non-WhatsApp sub-sample (Exp2, N=58) is retained."),
        duplicate_of="Winters et al. (2021) Exp1",
        kept="Winters et al. (2021) Exp1",
    ),
    ("Maertens (2022)", "Maertens2022_Exp3"): dict(
        reason=("Non-independent duplicate of Maertens et al. (2021) Experiment 1: same "
                "N=118 longitudinal gamified-inoculation sample. The peer-reviewed article "
                "reports the same experiment with additional timepoints, so the dissertation "
                "copy is dropped."),
        duplicate_of="Maertens et al. (2021) Exp1",
        kept="Maertens et al. (2021) Exp1",
    ),
    ("Maertens (2022)", "Maertens2022_Exp4"): dict(
        reason=("Non-independent duplicate of Maertens et al. (2021) Experiment 2: same "
                "N=110 longitudinal gamified-inoculation sample (post + 9-week follow-up). "
                "The article copy is kept."),
        duplicate_of="Maertens et al. (2021) Exp2",
        kept="Maertens et al. (2021) Exp2",
    ),
    ("Maertens (2022)", "Maertens2022_Exp5"): dict(
        reason=("Non-independent duplicate of Maertens et al. (2021) Experiment 3: same "
                "N=87 within-subjects replication (no control group; 0 eligible effects). "
                "Dropped with the other shared experiments for consistency; no numeric effect."),
        duplicate_of="Maertens et al. (2021) Exp3",
        kept="Maertens et al. (2021) Exp3",
    ),
    # --- Maertens (2022) dissertation: Studies 1-2 are duplicates too (DUPLICATE_ROUND3) --
    # The dissertation is a multi-study compilation (Studies 1-7); every coded experiment
    # (Studies 1-3) is the same experiment as a peer-reviewed article, so each is dropped
    # here and the article kept.  The dissertation's Studies 4-7 were not effect-size
    # extracted and remain unique to the thesis (partial overlap at the source level).
    ("Maertens (2022)", "Maertens2022_Exp1"): dict(
        reason=("Non-independent duplicate of Maertens et al. (2020) Experiment 1, Journal "
                "of Environmental Psychology (DOI 10.1016/j.jenvp.2020.101455): dissertation "
                "Study 1 is the identical N=415 (480 recruited, US Prolific adults) four-arm "
                "climate-inoculation experiment (control / consensus / false-balance / "
                "inoculation) measuring perceived scientific consensus, with the same "
                "complete-case sample ('Within the complete-cases dataset (N = 415), 51% of "
                "participants were female'). The published article is kept."),
        duplicate_of="Maertens et al. (2020) Exp1",
        kept="Maertens et al. (2020) Exp1",
    ),
    ("Maertens (2022)", "Maertens2022_Exp2"): dict(
        reason=("Non-independent duplicate of Maertens et al. (2025) Experiment 1, Nature "
                "Communications (DOI 10.1038/s41467-025-57205-x): dissertation Study 2 is the "
                "identical N=1,825 longitudinal climate-inoculation experiment (2,657 "
                "recruited via Prolific; ~260 per group) with control / inoculation / "
                "repeated-inoculation booster arms forming perceived scientific consensus at "
                "T0 / T10 / T30. The peer-reviewed article is kept."),
        duplicate_of="Maertens et al. (2025) Exp1",
        kept="Maertens et al. (2025) Exp1",
    ),
    ("Cookson (2021)", "CooksonDiss2021_Exp4"): dict(
        reason=("Non-independent duplicate of Cookson et al. (2021): dissertation Study 7 "
                "(Chapter 7) is the same social-norms-approach RCT in UK parents of young "
                "children (normative feedback vs assessment-only control). The dissertation "
                "row currently has N_Analyzed=None and 0 eligible effects, so the exclusion "
                "is a documented no-op."),
        duplicate_of="Cookson et al. (2021) Exp1",
        kept="Cookson et al. (2021) Exp1",
    ),
    # --- Apuke 2023 / Zhao 2025 salami family ---------------------------------
    # Six reports (four Apuke 2023 papers, Zhao et al. 2025, and the retracted
    # Xu et al. 2025) all describe the same ~470-person Nigerian undergraduate
    # quasi-experiment at the Faculty of Communication and Media Studies, Taraba
    # State University, run by the same team (Apuke / Omar / Tunca / Gever /
    # Iyendo).  Apuke et al. (2023 a) and (2023 b) share identical N, per-cell
    # gender split, data-collection month (April 2021) and -- decisively --
    # identical post-test SDs in all six cells; Zhao et al. (2025) is the full
    # 3-arm version (audio-visual 118 / classroom 117 / waitlist 235) and shares
    # its stimulus table with Apuke & Gever (2023); Xu et al. (2025) was
    # RETRACTED in 2025 for "substantial overlaps ... in all sections" with
    # Apuke & Gever (2023) "plus two other prior works".  One report is retained
    # as the anchor -- Apuke & Gever (2023), the core-DV peer-reviewed article
    # that the retraction names and whose stimulus table Zhao reuses -- and the
    # other three Apuke reports plus Zhao are dropped as non-independent
    # carve-outs of the same sample.  Evidence and pairwise matrix:
    # Scripts/APUKE_SALAMI_ASSESSMENT.md; corpus sweep:
    # Scripts/SALAMI_CORPUS_SWEEP.md.
    ("Apuke et al. (2023 a)", "Apuke2023a_Exp1"): dict(
        reason=("Non-independent carve-out of the same 470-person study as Apuke & Gever "
                "(2023) / Apuke et al. (2023 b): identical N=470 (235/235), same Faculty of "
                "Communication and Media Studies (Taraba State University), same April 2021 "
                "data collection, identical per-cell gender split (control 117M/118F; "
                "treatment 118M/117F), same DVs/instruments and identical post-test SDs in "
                "all six cells as Apuke et al. (2023 b). It reframes the same data as a "
                "3-week multimedia-vs-classroom modality trial and contributes 0 "
                "eligible_primary contrasts (active comparator), so the exclusion is also a "
                "numeric no-op. Zhao et al. (2025) reports the underlying 3-arm design "
                "(audio-visual 118 / classroom 117 / waitlist 235)."),
        duplicate_of="Apuke & Gever (2023) Exp1",
        kept="Apuke & Gever (2023) Exp1",
    ),
    ("Apuke et al. (2023 b)", "Apuke2023b_Exp1"): dict(
        reason=("Non-independent carve-out of the same 470-person study as Apuke & Gever "
                "(2023): identical N=470 (235/235), same department/site, same April 2021 "
                "collection, identical per-cell gender split and identical post-test SDs in "
                "all six cells as Apuke et al. (2023 a). Core-outcome (knowledge / detection "
                "/ sharing) version of the one underlying 3-arm experiment reported in full "
                "by Zhao et al. (2025). One report is retained; this one is dropped."),
        duplicate_of="Apuke & Gever (2023) Exp1",
        kept="Apuke & Gever (2023) Exp1",
    ),
    ("Apuke et al. (2023)", "Apuke2023_Exp1"): dict(
        reason=("Non-independent carve-out of the same 470-person study as Apuke & Gever "
                "(2023): same N=470 (235/235), same Faculty of Communication and Media "
                "Studies (Taraba State University), same team/year, same G*power 470 "
                "parameters, same 18-35 range and near-identical gender split (236M/234F vs "
                "235M/235F). A different-outcome (theory-of-planned-behaviour) carve-out "
                "from the same cohort; one report is retained, so this is dropped."),
        duplicate_of="Apuke & Gever (2023) Exp1",
        kept="Apuke & Gever (2023) Exp1",
    ),
    ("Zhao et al. (2025)", "Zhao2025_Exp1"): dict(
        reason=("Non-independent with Apuke & Gever (2023): Zhao et al. (2025) is the same "
                "470-person quasi-experiment at the Faculty of Communication and Media "
                "Studies, Taraba State University (submitted 18 Feb 2022; 520 volunteered, "
                "470 included; audio-visual 118 / classroom 117 / waitlist 235), shares the "
                "same 8-week curriculum, expert panel, 20-item Preston/fullfact battery and "
                "the identical stimulus table as Apuke & Gever (2023), and is co-authored by "
                "Apuke, Tunca and Gever. It is the overarching report of the family; only one "
                "report per sample may enter the pooled analysis, and the core-DV anchor is "
                "retained."),
        duplicate_of="Apuke & Gever (2023) Exp1",
        kept="Apuke & Gever (2023) Exp1",
    ),
    # --- Pennycook 2023 working paper -> 2024 NHB article ----------------------
    # Same authors and same experiments; the 2023 record is a working paper (no
    # DOI) whose N match the 2024 Nature Human Behaviour article. It contains no
    # unique experiment, so all five experiments are dropped; the published
    # article (Exp1..Exp7, including two added replications) is kept.
    ("Pennycook et al. (2023)", "Pennycook2023_Exp1"): dict(
        reason=("Non-independent working-paper version of Pennycook et al. (2024), Nature "
                "Human Behaviour (DOI 10.1038/s41562-024-02023-2). Same authors and the same "
                "experiments with matching N (Exp2=2,033; Exp3/4=1,211; Exp5=1,804; Exp1 "
                "1,030 vs 1,027). The 2023 record is a working paper with no DOI and no "
                "unique experiment; the published 2024 article is kept."),
        duplicate_of="Pennycook et al. (2024) Exp1..Exp7",
        kept="Pennycook et al. (2024) Exp1..Exp7",
    ),
    ("Pennycook et al. (2023)", "Pennycook2023_Exp2"): dict(
        reason=("Non-independent working-paper version of Pennycook et al. (2024) "
                "Experiment 2 (same N=2,033 and design); the published 2024 article is kept."),
        duplicate_of="Pennycook et al. (2024) Exp1..Exp7",
        kept="Pennycook et al. (2024) Exp1..Exp7",
    ),
    ("Pennycook et al. (2023)", "Pennycook2023_Exp3"): dict(
        reason=("Non-independent working-paper version of Pennycook et al. (2024) "
                "Experiment 3 (same N~1,211 and design); the published 2024 article is kept."),
        duplicate_of="Pennycook et al. (2024) Exp1..Exp7",
        kept="Pennycook et al. (2024) Exp1..Exp7",
    ),
    ("Pennycook et al. (2023)", "Pennycook2023_Exp4"): dict(
        reason=("Non-independent working-paper version of Pennycook et al. (2024) "
                "Experiment 4 (same N=1,211 and design); the published 2024 article is kept."),
        duplicate_of="Pennycook et al. (2024) Exp1..Exp7",
        kept="Pennycook et al. (2024) Exp1..Exp7",
    ),
    ("Pennycook et al. (2023)", "Pennycook2023_Exp5"): dict(
        reason=("Non-independent working-paper version of Pennycook et al. (2024) "
                "Experiment 5 (same N=1,804 and design); the published 2024 article is kept."),
        duplicate_of="Pennycook et al. (2024) Exp1..Exp7",
        kept="Pennycook et al. (2024) Exp1..Exp7",
    ),
    # --- thesis -> article partial overlaps -----------------------------------
    ("Basol (2022)", "Basol2022_Exp3"): dict(
        reason=("Non-independent duplicate of Basol et al. (2021) Experiment 1: dissertation "
                "Experiment 3 is the same voluntary within-subject pre-post in-game Go Viral! "
                "survey (N=1,771). The dissertation's unique experiments Exp1/2/5/6/7 are "
                "retained."),
        duplicate_of="Basol et al. (2021) Exp1",
        kept="Basol et al. (2021) Exp1",
    ),
    ("Basol (2022)", "Basol2022_Exp4"): dict(
        reason=("Non-independent duplicate of Basol et al. (2021) Experiment 2: dissertation "
                "Experiment 4 is the same preregistered three-arm Go Viral! / UNESCO / Tetris "
                "RCT (N=1,777, UK/FR/DE). The dissertation's unique experiments Exp1/2/5/6/7 "
                "are retained."),
        duplicate_of="Basol et al. (2021) Exp2",
        kept="Basol et al. (2021) Exp2",
    ),
    ("Jolley (2014)", "Jolley2014_Exp9"): dict(
        reason=("Non-independent duplicate of Jolley & Douglas (2017) Experiment 1: "
                "dissertation Experiment 9 is the same five-condition (conspiracy-only, "
                "anti-conspiracy-only, both orders, control) MTurk anti-vaccine-conspiracy "
                "experiment (N=260). The dissertation's unique Experiment 10 (N=130, "
                "pre-warning) is retained."),
        duplicate_of="Jolley & Douglas (2017) Exp1",
        kept="Jolley & Douglas (2017) Exp1",
    ),
    ("Banks (2025)", "Banks2025_Exp1"): dict(
        reason=("Non-independent duplicate of Banks & Brannon (2026) Experiment 1: same "
                "N=406 US online experiment with the same six message frames (control, be "
                "unbiased, consider-the-opposite, cognitive dissonance, self-affirmation, "
                "inoculation). The 2025 record's unique Experiment 2 (N=309) is retained and "
                "the 2026 peer-reviewed article is kept."),
        duplicate_of="Banks & Brannon (2026) Exp1",
        kept="Banks & Brannon (2026) Exp1",
    ),
    ("Biddlestone et al. (2025 a)", "Biddlestone2025a_Exp1"): dict(
        reason=("Duplicate of Biddlestone et al. (2025): the two study keys carry a "
                "byte-identical PDF and extracted text and the same analysed samples "
                "(Exp1 N=1583, Exp2 N=1603). Only one copy may enter the analysis; "
                "Biddlestone et al. (2025) is retained."),
        duplicate_of="Biddlestone et al. (2025) Exp1",
        kept="Biddlestone et al. (2025) Exp1",
    ),
    ("Biddlestone et al. (2025 a)", "Biddlestone2025a_Exp2"): dict(
        reason=("Duplicate of Biddlestone et al. (2025): identical PDF/text and the same "
                "analysed sample (Exp2 N=1603). Biddlestone et al. (2025) is retained."),
        duplicate_of="Biddlestone et al. (2025) Exp2",
        kept="Biddlestone et al. (2025) Exp2",
    ),
}

# Unique experiments retained from the multi-study sources above.
# Maertens (2022) no longer appears: DUPLICATE_ROUND3 excluded its remaining coded
# experiments (Exp1 -> Maertens et al. 2020; Exp2 -> Maertens et al. 2025), so all five
# coded dissertation experiments are now dropped. The dissertation's uncoded Studies 4-7
# are unique to the thesis but were never effect-size extracted.
RETAINED_UNIQUE = {
    # Moravec dissertation Essay 1 (EEG, N=83) and Essay 3 (NewsGuard, N=76) are not
    # experiments reported in the ISR article (whose Exp1/Exp3 are different studies);
    # only Essay 2 is duplicated and dropped above.
    "Moravec (2020)": ["MoravecDiss2020_Exp1", "MoravecDiss2020_Exp3"],
    # Winters thesis Study IV's no-WhatsApp sub-sample (Exp2, N=58) is not in the BMJ GH
    # article; only the WhatsApp RCT (Exp1) is duplicated and dropped above.
    "Winters (2021)": ["WintersDiss2021_Exp2"],
    # Basol dissertation experiments that are NOT in Basol et al. (2021): the Bad News
    # game (Exp1), Join this Group (Exp2), the inoculation-talk study (Exp5/Exp6) and the
    # second-order inoculation experiment (Exp7).
    "Basol (2022)": ["Basol2022_Exp1", "Basol2022_Exp2", "Basol2022_Exp5",
                      "Basol2022_Exp6", "Basol2022_Exp7"],
    # Jolley dissertation Experiment 10 (pre-warning, N=130) is not the article's Exp2.
    "Jolley (2014)": ["Jolley2014_Exp10"],
    # Banks (2025) Experiment 2 (N=309) is unique to the 2025 record.
    "Banks (2025)": ["Banks2025_Exp2"],
}

# Pre-rename labels for the entries above (documentation only; the corpus now uses the
# `Diss` ids).  Kept so the mapping from the coding-note labels is traceable.
LEGACY_STUDY_IDS = {
    ("Lutzke (2019)", "Lutzke2019_Exp1"): "LutzkeDiss2019_Exp1",
    ("Moravec (2020)", "Moravec2020_Exp2"): "MoravecDiss2020_Exp2",
    ("Winters (2021)", "Winters2021_Exp1"): "WintersDiss2021_Exp1",
}

# ---------------------------------------------------------------------------
# No misinformation intervention (C1).
#
# Each entry is a companion experiment, inside an otherwise included paper, that
# delivers no misinformation intervention.  The paper is retained for its other,
# eligible experiment(s); only the non-intervention experiment is dropped from the
# analysis.  Verified against Text/<Key>.txt and each record's Rationale_Notes.
# (study_key, experiment_label) -> dict(reason, criterion)
NO_INTERVENTION_EXCLUSIONS = {
    ("Grüner (2021)", "Grüner2021_Exp1"): dict(
        criterion="No misinformation intervention (C1)",
        reason=("Exp1 is a single-arm within-subject baseline (no manipulation). "
                "The corrective-information intervention is delivered in Exp2, which "
                "is retained."),
        kept="Grüner2021_Exp2"),
    ("Lin et al. (2024)", "Lin2024_Exp1"): dict(
        criterion="No misinformation intervention (C1)",
        reason=("Exp1 establishes the illusory-truth effect with no intervention arm. "
                "The accuracy-prompt intervention is in Exp2-4, which are retained."),
        kept="Lin2024_Exp2, Lin2024_Exp3, Lin2024_Exp4"),
    ("Mirhoseini et al. (2023)", "Mirhoseini2023_Exp1"): dict(
        criterion="No misinformation intervention (C1)",
        reason=("Exp1 is an observational lab/EEG study of predictors of fake-news "
                "detection with no manipulation. The feedback intervention is Exp2, "
                "which is retained."),
        kept="Mirhoseini2023_Exp2"),
    ("Orosz et al. (2024 a)", "Orosz2024a_Exp1"): dict(
        criterion="No misinformation intervention (C1)",
        reason=("Exp1 is a cross-sectional representative survey with no intervention. "
                "The media-literacy RCT is Exp2, which is retained."),
        kept="Orosz2024a_Exp2"),
    ("Pennycook et al. (2021)", "Pennycook2021_Exp1"): dict(
        criterion="No misinformation intervention (C1)",
        reason=("Exp1 is a non-intervention accuracy/sharing dissociation study. The "
                "accuracy-prompt experiments are Exp3-7, which are retained."),
        kept="Pennycook2021_Exp3..Exp7"),
    ("Pennycook et al. (2021)", "Pennycook2021_Exp2"): dict(
        criterion="No misinformation intervention (C1)",
        reason=("Exp2 is a descriptive survey of sharing motives with no intervention. "
                "The accuracy-prompt experiments are Exp3-7, which are retained."),
        kept="Pennycook2021_Exp3..Exp7"),
    ("Wang et al. (2025 a)", "Wang2025a_Exp1"): dict(
        criterion="No misinformation intervention (C1)",
        reason=("Exp1 is an observational simulated-feed study with no intervention. "
                "The inoculation video is delivered in Exp2-5, which are retained."),
        kept="Wang2025a_Exp2..Exp5"),
    ("Wang et al. (2025 b)", "Wang2025b_Exp1"): dict(
        criterion="No misinformation intervention (C1)",
        reason=("Exp1 is a within-subject authenticity x valence sharing task with no "
                "intervention. The targeted accuracy prompt is Exp2, which is retained."),
        kept="Wang2025b_Exp2"),
    ("Wiley et al. (2009)", "Wiley2009_Exp1"): dict(
        criterion="No misinformation intervention (C1)",
        reason=("Exp1 manipulates an argument-vs-descriptive essay-writing instruction, "
                "not a misinformation intervention. The SEEK source-evaluation unit is "
                "Exp2, which is retained."),
        kept="Wiley2009_Exp2"),
    ("Jalbert & Wack (2026)", "Jalbert2026_Exp2a"): dict(
        criterion="No misinformation intervention (C1)",
        reason=("Exp2a is a moderation-preference measurement task (importance ratings "
                "and forced choices); no intervention is delivered. The content-removal "
                "intervention is Exp1, which is retained."),
        kept="Jalbert2026_Exp1"),
    ("Jalbert & Wack (2026)", "Jalbert2026_Exp2b"): dict(
        criterion="No misinformation intervention (C1)",
        reason=("Exp2b is a moderation-preference measurement task; no intervention is "
                "delivered. Exp1 is retained."),
        kept="Jalbert2026_Exp1"),
    ("Jalbert & Wack (2026)", "Jalbert2026_Exp2c"): dict(
        criterion="No misinformation intervention (C1)",
        reason=("Exp2c is a moderation-preference measurement task; no intervention is "
                "delivered. Exp1 is retained."),
        kept="Jalbert2026_Exp1"),
}

ALL_EXCLUSIONS = {}
ALL_EXCLUSIONS.update(NONINDEPENDENT_EXCLUSIONS)
ALL_EXCLUSIONS.update(NO_INTERVENTION_EXCLUSIONS)


def is_excluded(study_key, experiment):
    """True if this (study_key, experiment label) is excluded for either reason.

    A pre-rename (legacy) experiment label is resolved through ``LEGACY_STUDY_IDS`` so
    that callers holding the old id still map to the current exclusion.
    """
    key = (str(study_key), str(experiment))
    if key in ALL_EXCLUSIONS:
        return True
    alias = LEGACY_STUDY_IDS.get(key)
    return alias is not None and (str(study_key), alias) in ALL_EXCLUSIONS


def reason(study_key, experiment):
    e = ALL_EXCLUSIONS.get((str(study_key), str(experiment)))
    return e["reason"] if e else ""


def criterion(study_key, experiment):
    e = ALL_EXCLUSIONS.get((str(study_key), str(experiment)))
    return e.get("criterion", "Duplicate/non-independent report of an included study") if e else ""


def excluded_labels(study_key):
    """Set of excluded experiment labels for a study (for artifact-level filtering).

    Includes any pre-rename label that maps to a current excluded experiment, so a
    dataset sidecar that still carries the old artifact id is filtered too.
    """
    out = {exp for (k, exp) in ALL_EXCLUSIONS if str(k) == str(study_key)}
    for (k, old), new in LEGACY_STUDY_IDS.items():
        if str(k) == str(study_key) and (str(study_key), new) in ALL_EXCLUSIONS:
            out.add(old)
    return out


def table():
    """List of rows for the workbook's `Excluded_Experiments` sheet."""
    rows = []
    for (study_key, experiment), e in NONINDEPENDENT_EXCLUSIONS.items():
        rows.append({"study_key": study_key, "experiment": experiment,
                     "kind": "non_independent",
                     "duplicate_of": e["duplicate_of"], "kept_source": e["kept"],
                     "reason": e["reason"]})
    for (study_key, experiment), e in NO_INTERVENTION_EXCLUSIONS.items():
        rows.append({"study_key": study_key, "experiment": experiment,
                     "kind": "no_intervention",
                     "duplicate_of": "", "kept_source": e.get("kept", ""),
                     "reason": e["reason"]})
    return rows


if __name__ == "__main__":
    for r in table():
        if r["kind"] == "non_independent":
            print(f"{r['study_key']} / {r['experiment']}  ->  drop (dup of {r['duplicate_of']})")
        else:
            print(f"{r['study_key']} / {r['experiment']}  ->  drop (no intervention; "
                  f"kept: {r['kept_source']})")
    print("retained unique:", RETAINED_UNIQUE)
