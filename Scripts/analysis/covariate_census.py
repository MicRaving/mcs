"""Covariate census — what individual-difference measures does the harmonised
corpus actually contain, and which are worth analysing?

Scans every ``<Key> - harmonized_variables.csv`` sidecar, maps each ``cov_*``
name onto a construct family, and reports (a) studies and participants per
family, and (b) for the constructs that matter for the backfire question, how
many studies also carry the true/false endorsement outcomes and sit in the
moderation sample of ``motivated_reasoning_moderation.py``.

Products (Results/):
  covariate_census.csv      every construct family: studies, participants,
                            variable names, example names
  covariate_shortlist.csv   the theorised constructs with the overlap counts
                            that decide whether they are worth pooling

Run: python Scripts/analysis/covariate_census.py
"""
import os
import re
import sys
import glob
import tempfile

import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
B = os.path.dirname(os.path.dirname(_HERE))
DS = os.path.join(B, "Datasets")
RESULTS = os.path.join(B, "Results")
os.makedirs(RESULTS, exist_ok=True)
os.makedirs(os.path.join(B, "temp"), exist_ok=True)
tempfile.tempdir = os.path.join(B, "temp")

# ordered taxonomy: first matching rule wins
RULES = [
 ("Dark triad / antagonistic traits",
  r"dark_traits|narciss|machiavell|psychopath|sadism"),
 ("Big Five / personality trait",
  r"bigfive|big_five|bfi|tipi|hexaco|personality|extraver|agreeabl|conscient|"
  r"neurotic|openness|self_control|grit|impulsiv"),
 ("Intelligence / cognitive ability",
  r"intelligence|^cov_iq|cognitive_ability|fluid|crystalli|working_memory|ravens|"
  r"matrix_reasoning|verbal_|numeracy|quantitative|spatial|processing_speed|memory_"),
 ("Analytic / reflective style",
  r"analytic_thinking|^cov_crt|need_for_cognition|cognitive_reflection|rational"),
 ("Epistemic style (open-minded, humility, closure)",
  r"actively_open_minded|open_minded|intellectual_humility|need_for_closure|dogmat|"
  r"curiosit|need_to_evaluate|need_for_affect"),
 ("Political ideology / partisanship",
  r"political_ideology|political_orientation|conservatism|political_party|party_id|"
  r"party_identification|political_affiliation|partisan|political_interest|"
  r"political_knowledge|extremism|authoritarian|dominance|populis|nationalis|"
  r"patriot|left_right|ideolog"),
 ("Conspiracy / unwarranted beliefs",
  r"conspiracy|pseudoscience|unwarranted|paranormal|bullshit|pseudoscientific"),
 ("Trust in science",
  r"trust_in_science|confidence_in_science|science_attitude|deference_scientific|"
  r"science_efficacy"),
 ("Trust (other)", r"trust"),
 ("Moral / values / disgust", r"moral_|schwartz|values_|disgust|sacred|fairness"),
 ("Media / news / tech use",
  r"news_consumption|news_use|social_media|media_use|internet_use|smartphone|twitter|"
  r"facebook|youtube|tiktok|screen_time|digital_use|genai|ai_use|ai_familiar|chatbot"),
 ("Literacy / topic knowledge",
  r"literacy|persuasion_knowledge|prior_knowledge|science_literacy|health_literacy|"
  r"self_efficacy|neuroscience_literacy|scientific_literacy"),
 ("Affect / well-being / clinical",
  r"anxiety|depress|stress|well_?being|loneliness|self_esteem|empathy|emotion|anger|"
  r"affect|happiness|life_satisfaction|mental_health|trauma"),
 ("Prior topic attitudes",
  r"vaccine_attitude|climate_attitude|immigration_attitude|prior_belief|"
  r"belief_specific|prior_attitude|attitude_"),
 ("Vaccination / health behaviour",
  r"vaccine_status|vaccinat|health_behav|mask|distanc"),
 ("Experimental / data quality",
  r"completion_time|attention|device|sample_source|response_time|straightlin|"
  r"platform|recruit|order|condition|survey_|wave|dropout|duration"),
 ("Demographics",
  r"^cov_age|^cov_gender|^cov_sex|^cov_female|^cov_male|education|income|ses|"
  r"ethnicity|^cov_race|employment|^cov_region|country|language|marital|"
  r"^cov_religion|urban|rural|student|birth_year|household|occupation|religiosity"),
]

# constructs worth investigating, with a strict matcher
SHORTLIST = {
 "Analytic / CRT / need for cognition":
   r"analytic_thinking|^cov_crt|need_for_cognition|cognitive_reflection",
 "Conspiracy / unwarranted beliefs":
   r"conspiracy|pseudoscience|unwarranted|paranormal",
 "Open-minded / humility / closure":
   r"actively_open_minded|open_minded|intellectual_humility|need_for_closure|dogmat",
 "Trust in science":
   r"trust_in_science|confidence_in_science|science_attitude|deference_scientific",
 "Trust in media / news / fact-checkers":
   r"trust_in_media|trust_in_news|media_trust|trust_news|trust_mass_media|"
   r"trust_in_factcheck|trust_third_party",
 "Topic-specific risk / concern":
   r"covid_concern|covid_worry|risk_perception|^cov_fear$|^cov_concern",
 "Institutional / general / government trust":
   r"institutional_trust|trust_government|trust_general|general_trust|"
   r"trust_healthcare|trust_propensity",
 "Big Five (O/C/E/A/N)":
   r"bigfive|big_five|bfi|tipi|personality|^cov_openness$|^cov_conscientiousness$|"
   r"^cov_extraversion$|^cov_agreeableness$|^cov_neuroticism$",
 "Numeracy": r"numeracy|quantitative",
 "Bullshit receptivity": r"bullshit",
 "Baseline / pretest topic belief":
   r"pretest|baseline|prior_belief|belief_specific",
 "Confirmation / myside bias":
   r"confirmation|myside|biased_assimilation",
 "Affective polarisation / partisan identity":
   r"affective|polariz|partisan_identity|identity_fusion|political_identity|"
   r"in_?group|out_?group",
 "Reading / verbal proficiency": r"reading|verbal_proficien",
 "Intelligence / cognitive ability":
   r"cognitive_ability|intelligence|^cov_iq|fluid|crystalli|working_memory|ravens|"
   r"verbal_|nonverbal",
 "Dark triad (narcissism, Machiavellianism, psychopathy, sadism)": r"dark_traits",
}


def read_covariates():
    rows = []
    for f in sorted(glob.glob(os.path.join(DS, "*", "* - harmonized_variables.csv"))):
        key = os.path.basename(os.path.dirname(f))
        try:
            s = pd.read_csv(f, encoding="utf-8-sig", low_memory=False)
        except Exception:
            continue
        if "role" not in s.columns:
            continue
        for _, r in s[s["role"].astype(str) == "covariate"].iterrows():
            rows.append({"study": key,
                         "uv": str(r.get("uniform_variable") or ""),
                         "original": str(r.get("original_variable") or ""),
                         "notes": str(r.get("notes") or "")})
    return pd.DataFrame(rows)


def participant_counts(studies):
    rows = []
    for key in sorted(studies):
        w = os.path.join(DS, key, key + " - harmonized.csv")
        if not os.path.isfile(w):
            rows.append({"study": key, "n": 0})
            continue
        try:
            cols = list(pd.read_csv(w, nrows=0, encoding="utf-8-sig").columns)
            use = ["pid"] if "pid" in cols else cols[:1]
            d = pd.read_csv(w, encoding="utf-8-sig", low_memory=False, usecols=use)
            n = int(d[use[0]].nunique()) if len(d) else 0
        except Exception:
            n = 0
        rows.append({"study": key, "n": n})
    return pd.DataFrame(rows)


def family_of(uv):
    u = str(uv).lower()
    for fam, rx in RULES:
        if re.search(rx, u):
            return fam
    return "Other / unclassified"


def main():
    C = read_covariates()
    C["notes"] = C["notes"].fillna("")
    C["family"] = C["uv"].map(family_of)
    P = participant_counts(C["study"].unique())
    pmap = dict(zip(P["study"], P["n"]))

    fam = C.groupby("family")["study"].nunique().sort_values(ascending=False)
    census = pd.DataFrame({
        "construct": fam.index,
        "n_studies": fam.values,
        "n_participants": [int(sum(pmap.get(s, 0) for s in
                                  C.loc[C.family == f, "study"].unique())) for f in fam.index],
        "n_variable_names": [C.loc[C.family == f, "uv"].nunique() for f in fam.index],
        "example_variables": [
            "; ".join(C[C.family == f].groupby("uv")["study"].nunique()
                       .sort_values(ascending=False).head(4).index) for f in fam.index],
    })
    census.to_csv(os.path.join(RESULTS, "covariate_census.csv"), index=False,
                  encoding="utf-8-sig")

    # overlap with the outcome-bearing studies, if the moderation workbook exists
    tf, samp = set(), set()
    tf_path = os.path.join(B, "temp", "mr_census.csv")
    if os.path.isfile(tf_path):
        tf = set(pd.read_csv(tf_path, encoding="utf-8-sig")["study_key"])
    xl = os.path.join(RESULTS, "motivated_reasoning_moderation.xlsx")
    if os.path.isfile(xl):
        try:
            samp = set(pd.read_excel(xl, sheet_name="Person_Sample")["study_key"])
        except Exception:
            samp = set()

    rows = []
    for name, rx in SHORTLIST.items():
        m = C[C["uv"].str.contains(rx, case=False, regex=True)]
        st = set(m["study"])
        rows.append({"construct": name, "n_studies": len(st),
                     "n_participants": int(sum(pmap.get(s, 0) for s in st)),
                     "n_with_TF_outcomes": len(st & tf),
                     "n_in_moderation_sample": len(st & samp),
                     "participants_in_moderation_sample":
                         int(sum(pmap.get(s, 0) for s in st & samp))})
    short = pd.DataFrame(rows).sort_values(
        ["n_in_moderation_sample", "n_studies"], ascending=False)
    short.to_csv(os.path.join(RESULTS, "covariate_shortlist.csv"), index=False,
                 encoding="utf-8-sig")

    print(f"covariates: {len(C)} rows, {C['uv'].nunique()} names, "
          f"{C['study'].nunique()} studies")
    print("\n" + census.to_string(index=False))
    print("\n" + short.to_string(index=False))


if __name__ == "__main__":
    main()
