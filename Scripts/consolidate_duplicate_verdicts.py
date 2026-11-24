"""The verified duplicate-trial verdicts, consolidated.

The verification agents worked in parallel and each wrote its own rows to
``Results/duplicate_trial_verdicts.csv``; the *keep* decision was often
given in the agent's report rather than in the CSV row, and two agents
sometimes judged the same pair differently.  This file is the
consolidated record: one row per pair, with the verdict, the paper to
keep, the paper to drop, and the evidence.

Where the verifiers disagree, the pair is recorded as ``CONTESTED`` and
**no** exclusion is made - the owner's decision was to exclude only the
confirmed pairs, and a contested pair is not confirmed.

Generated from the agents' own reports; the source rows stay in
``Results/duplicate_trial_verdicts.csv``.
"""
import csv
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")
OUT = os.path.join(RESULTS, "duplicate_trial_verdicts_consolidated.csv")

# (key_a, key_b, verdict, keep, drop, note)
ROWS = [
    ("Apuke & Gever (2023)", "Apuke et al. (2023 a)", "DIFFERENT", "", "",
     "same 470-person frame, but different waves and different source tables "
     "(April 2021 vs December 2021-April 2022)"),
    ("Apuke & Gever (2023)", "Apuke et al. (2023 b)", "DIFFERENT", "", "",
     "different wave; identical frame flagged as one related series"),
    ("Apuke & Gever (2023)", "Apuke et al. (2023)", "DIFFERENT", "", "",
     "a 6-week fake-news-awareness campaign with a TPB battery against an "
     "8-week library course; no outcome overlap"),
    ("Apuke & Gever (2023)", "Zhang et al. (2025)", "CONTESTED", "", "",
     "batch 000 judged it a SAME TRIAL (Zhang's control received training, "
     "so it is not a no-intervention control); batches 001 and 002 judged "
     "the two Apuke waves as different samples"),
    ("Apuke & Gever (2023)", "Zhao et al. (2025)", "CONTESTED", "", "",
     "batch 000 judged it a SAME TRIAL (Zhao's cells reproduce JAL's); "
     "batches 001 and 002 judged the two Apuke waves as different samples"),
    ("Apuke et al. (2023 a)", "Apuke et al. (2023 b)", "SAME TRIAL",
     "Apuke et al. (2023 b)", "Apuke et al. (2023 a)",
     "both April 2021, same 470/235/235, identical arm-level gender split, "
     "same 12-item knowledge scale and Preston task; (2023 b) published first"),
    ("Apuke et al. (2023 a)", "Apuke et al. (2023)", "CONTESTED", "", "",
     "batch 000 judged the pair DIFFERENT (different waves, different "
     "outcome battery); batch 001 judged it the SAME April-2021 trial "
     "re-sliced; both cite the same per-arm demographics"),
    ("Apuke et al. (2023 b)", "Apuke et al. (2023)", "CONTESTED", "", "",
     "batch 000 judged the pair DIFFERENT; batch 001 judged it the SAME "
     "April-2021 trial re-reported with a TPB battery"),
    ("Apuke et al. (2023 a)", "Zhang et al. (2025)", "DIFFERENT", "", "",
     "different waves: April 2021 vs September 2021-January 2022; Zhang "
     "cites (2023 b) as prior work"),
    ("Apuke et al. (2023 a)", "Zhao et al. (2025)", "DIFFERENT", "", "",
     "different samples; Zhao's inclusion criteria exclude the April-2021 "
     "participants; Zhao cites (2023 a) and (2023 b) as prior work"),
    ("Apuke et al. (2023 b)", "Zhao et al. (2025)", "DIFFERENT", "", "",
     "different samples, separately recruited and screened; Zhao cites "
     "(2023 b) as prior work"),
    ("Apuke et al. (2023)", "Zhang et al. (2025)", "CONTESTED", "", "",
     "batches 001 and 002 judged the pair DIFFERENT; batch 000 judged it a "
     "SAME TRIAL (Zhang's control received training, so it is not a "
     "no-intervention control)"),
    ("Apuke et al. (2023)", "Zhao et al. (2025)", "CONTESTED", "", "",
     "batches 001 and 002 judged the pair DIFFERENT; batch 000 judged it a "
     "SAME TRIAL"),
    ("Austin et al. (2007)", "Pinkleton et al. (2007)", "SAME TRIAL",
     "Pinkleton et al. (2007)", "Austin et al. (2007)",
     "a secondary analysis of a quasi-experiment: the same 723 participants "
     "from 76 schools, identical arms, same six-lesson curriculum"),
    ("Austin et al. (2015)", "Pinkleton et al. (2012)", "SAME TRIAL",
     "Pinkleton et al. (2012)", "Austin et al. (2015)",
     "wave 1 is Pinkleton (2012): N = 922, 655 treatment / 267 control; "
     "Austin (2015) says the original evaluations are in Pinkleton et al."),
    ("Austin et al. (2020 a)", "Austin et al. (2020)", "SAME TRIAL",
     "Austin et al. (2020)", "Austin et al. (2020 a)",
     "the same FoodMania! trial: 189 parent-child dyads, same counties, "
     "Extension recruitment, same IRB No. 15012"),
    ("Austin et al. (2020 a)", "Austin et al. (2024)", "SAME TRIAL",
     "Austin et al. (2020)", "Austin et al. (2024)",
     "the 2024 paper re-analyses the same 189 dyads and cites Austin et al. "
     "(2020a, 2020b) for the trial"),
    ("Austin et al. (2020)", "Austin et al. (2024)", "SAME TRIAL",
     "Austin et al. (2020)", "Austin et al. (2024)",
     "the 2024 paper cites this very article for the same 189 FoodMania! "
     "dyads; its distinctive output is mediation paths, not new contrasts"),
    ("Banerjee & Greene (2006)", "Banerjee & Greene (2007)", "SAME TRIAL",
     "Banerjee & Greene (2006)", "Banerjee & Greene (2007)",
     "the same 260 students in two Northeastern schools, identical "
     "classroom-randomised arms and the same attitude-scale statistics"),
    ("Banerjee et al. (2015)", "Greene et al. (2015)", "SAME TRIAL",
     "Banerjee et al. (2015)", "Greene et al. (2015)",
     "Greene et al. (2015) Sample II is the same 2011 YMD test: identical "
     "N = 171, same ages, 34 Pennsylvania schools; Greene's Sample I is a "
     "different sample"),
    ("Banks & Brannon (2026)", "Banks (2025)", "SAME TRIAL",
     "Banks & Brannon (2026)", "Banks (2025)",
     "the article is the peer-reviewed version of dissertation Study 1: "
     "same 462 recruits, same 55+1 exclusions, final N = 406, same 6 "
     "message frames; the thesis Study 2 is a different trial"),
    ("Basol (2022)", "Basol et al. (2021)", "SAME TRIAL",
     "Basol et al. (2021)", "Basol (2022)",
     "thesis chapter 4 is the published article itself (same chapter "
     "title, same N = 1,771/1,777); the other chapters are different trials"),
    ("Biddlestone et al. (2025 a)", "Biddlestone et al. (2025)", "SAME TRIAL",
     "Biddlestone et al. (2025)", "Biddlestone et al. (2025 a)",
     "byte-identical PDFs (the same SHA-256): one Political Psychology "
     "article filed twice"),
    ("Gordon et al. (2016)", "Gordon et al. (2018)", "SAME TRIAL",
     "Gordon et al. (2016)", "Gordon et al. (2018)",
     "the 2018 brief report re-analyses the same trial: the same 165 "
     "fifth/sixth-graders, same 10-week program and three waves"),
    ("Jolley & Douglas (2017)", "Jolley (2014)", "SAME TRIAL",
     "Jolley & Douglas (2017)", "Jolley (2014)",
     "2017 Study 1 is thesis Study 9: both 267 MTurk participants, the same "
     "five conditions and cell sizes, identical cell means"),
    ("Lutzke (2019)", "Lutzke et al. (2019)", "SAME TRIAL",
     "Lutzke et al. (2019)", "Lutzke (2019)",
     "identical experiment, final N = 2,750, the same 3x2 design and the "
     "same F statistics; the single-author file is the manuscript"),
    ("Maertens (2022)", "Maertens et al. (2020)", "SAME TRIAL",
     "Maertens et al. (2020)", "Maertens (2022)",
     "thesis Study 1 is the 2020 article: 480 Prolific participants, 415 "
     "complete cases, the same 80-participant pilot"),
    ("Maertens (2022)", "Maertens et al. (2021)", "SAME TRIAL",
     "Maertens et al. (2021)", "Maertens (2022)",
     "thesis Study 3 is the 2021 article's Experiments 1-3; word-for-word "
     "identical Methods"),
    ("Maertens (2022)", "Maertens et al. (2025)", "SAME TRIAL",
     "Maertens et al. (2025)", "Maertens (2022)",
     "thesis Studies 2, 4, 5 are 2025 Studies 1, 2, 3-5: identical Ns and "
     "panels"),
    ("Marten & Stadtler (2025 b)", "Marten et al. (2025)", "SAME TRIAL",
     "Marten et al. (2025)", "Marten & Stadtler (2025 b)",
     "the '2025 b' file is the author accepted manuscript of the Learning "
     "and Individual Differences article: same 254 students, final 210"),
    ("Moravec (2020)", "Moravec et al. (2020)", "SAME TRIAL",
     "Moravec et al. (2020)", "Moravec (2020)",
     "the dissertation's chapter 3 is titled exactly like the ISR article "
     "and reports the same N = 398 Qualtrics main study"),
    ("Neylan et al. (2023 b)", "Neylan et al. (2023)", "SAME TRIAL",
     "Neylan et al. (2023)", "Neylan et al. (2023 b)",
     "the same DOI, the same 10 pages, the extracted texts match at 0.9999: "
     "one article downloaded twice"),
    ("Pennycook et al. (2023)", "Pennycook et al. (2024)", "SAME TRIAL",
     "Pennycook et al. (2024)", "Pennycook et al. (2023)",
     "the 2023 working paper and the 2024 article have identical per-study "
     "Ns"),
    ("Ramasubramanian & Oliver (2007)", "Ramasubramanian (2005)", "SAME TRIAL",
     "Ramasubramanian & Oliver (2007)", "Ramasubramanian (2005)",
     "identical N = 227 (196 analysed) and identical statistics to the "
     "decimals"),
    ("Ramasubramanian & Oliver (2007)", "Ramasubramanian (2007)", "SAME TRIAL",
     "Ramasubramanian & Oliver (2007)", "Ramasubramanian (2007)",
     "the Media Psychology note 1 states the same experiment included the "
     "implicit judgment-and-memory mini-study, a salami slice of the "
     "same session"),
    ("Scull et al. (2024)", "Scull et al. (2026 a)", "CONTESTED", "", "",
     "the 2026 paper analyses longitudinal data collected from the same "
     "national sample (23 campuses, N = 1,139, NCT04950686) - but the two "
     "papers report DIFFERENT timepoints (2024: 20 eligible posttest rows; "
     "2026 a: 30 eligible followup_52w rows), so each pool takes one "
     "contribution from the trial and there is no double counting to "
     "prevent; excluding 2024 would only lose the posttest"),
    ("Winters (2021)", "Winters et al. (2021)", "SAME TRIAL",
     "Winters et al. (2021)", "Winters (2021)",
     "the thesis's Study IV is the article's CMT RCT: the same 736 "
     "randomised 1:1:1, the same recruitment, the same registration "
     "NCT04112680, the same results"),
    ("Zhang et al. (2025)", "Zhao et al. (2025)", "CONTESTED", "", "",
     "batch 002 judged the pair DIFFERENT (the shared-participant risk "
     "sits with the Apuke 2021 wave); batch 006 judged it a SAME TRIAL "
     "(one Nigerian trial reported twice)"),
]


#: Which experiments of the dropped paper go.  A confirmed duplicate is
#: often only *part* of a paper: a thesis whose one study is published as
#: an article keeps its other studies, and a paper that reports two
#: samples keeps the one that is not the duplicate.  A pair absent from
#: this table drops every experiment of the paper ("*").
SCOPE = {
    "Basol (2022)": ["Basol2022_Exp3", "Basol2022_Exp4"],
    "Banks (2025)": ["Banks2025_Exp1"],
    "Greene et al. (2015)": ["Greene2015_Exp2"],
    "Jolley (2014)": ["Jolley2014_Exp9"],
    "Winters (2021)": ["WintersDiss2021_Exp2"],
    "Maertens (2022)": ["Maertens2022_Exp1", "Maertens2022_Exp2",
                        "Maertens2022_Exp3", "Maertens2022_Exp4",
                        "Maertens2022_Exp5"],
    "Austin et al. (2015)": ["Austin2015_Exp1"],
}


def main():
    with open(OUT, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "key_a", "key_b", "verdict", "keep", "drop", "drop_experiments",
            "note"])
        w.writeheader()
        for a, b, v, keep, drop, note in ROWS:
            scope = SCOPE.get(drop, [])
            w.writerow(dict(key_a=a, key_b=b, verdict=v, keep=keep,
                            drop=drop,
                            drop_experiments=";".join(scope),
                            note=note))
    same = [r for r in ROWS if r[2] == "SAME TRIAL"]
    diff = [r for r in ROWS if r[2] == "DIFFERENT"]
    contest = [r for r in ROWS if r[2] == "CONTESTED"]
    print("pairs recorded: %d" % len(ROWS))
    print("   confirmed same trial: %d" % len(same))
    print("   different trials:     %d" % len(diff))
    print("   contested:            %d" % len(contest))
    drops = sorted({r[4] for r in same if r[4]})
    keeps = sorted({r[3] for r in same if r[3]})
    print("\npapers a confirmed pair would DROP: %d" % len(drops))
    for d in drops:
        scope = SCOPE.get(d)
        print("   %-40s %s" % (d[:40],
                               ("only " + ", ".join(scope)) if scope
                               else "every experiment"))
    print("\npapers a confirmed pair would KEEP: %d" % len(keeps))
    for k in keeps:
        print("   %s" % k)
    print("\ncontested pairs (no exclusion is made):")
    for a, b, v, keep, drop, note in contest:
        print("   %-36s <-> %-36s" % (a[:36], b[:36]))
    print("\nwrote", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
