"""Semantic-homogenisation rules shared by the apply pass (temp/semhom_apply.py) and by
every `Datasets/<Key>/harmonizer.py`, so a harmonizer re-run reproduces the same result.

Rules (Scripts/VARIABLE_HOMOGENIZATION_PROPOSAL.md §3, §4, §1, §7):
  R1 AGE      : binned age -> continuous `cov_age` (bin midpoint, derived & flagged);
                raw bins kept as `cov_age_bin*`; source/bounds recorded in the sidecar notes.
  R2 ATTENTION: documented attention checks -> `flag_attention_fail_pct` (+ pass/n/k);
                raw per-item columns kept verbatim (`flag_attention_raw*`).
  R3 ROLE     : controlled roles {pid, condition, factor, flag, covariate, metacog, dv, item,
                other}; `cov` -> `covariate`; attention/manipulation/exclusion checks -> `flag`.
  R4 METACOG  : every confidence/certainty/metacognition variable -> `metacog_*` (never `dv_*`),
                excluded from the SDT / primary pools (NON_OUTCOME_DOMAINS / is_confidence_name).

Nothing is invented: age midpoints and 0/50/100 attention percentages are arithmetic on the
existing coded values; bands/checks without a documented coding are reported, not guessed.

Public API:
  finalize(key, side, wide) -> (side, wide, records)
      Applies every rule in place (rename map leftovers, role, metacog, age, attention) and
      returns the (possibly new) DataFrames plus a list of reversible change records
      `{rule, old_name, new_name, detail}`.
"""
import os
import re

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_TEMP = os.path.join(_ROOT, "temp")

import sys as _sys
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
import domain_restriction as DR  # noqa: E402

_RENAMES = None


def load_rename_map():
    global _RENAMES
    if _RENAMES is not None:
        return _RENAMES
    _RENAMES = {}
    p = None
    for cand in ("variable_rename_map_final.csv", "variable_rename_map_proposed.csv"):
        q = os.path.join(_TEMP, cand)
        if os.path.isfile(q):
            p = q
            break
    if p is None:
        return _RENAMES
    rm = pd.read_csv(p, encoding="utf-8-sig", low_memory=False)
    for _, r in rm.iterrows():
        old, new = str(r["old_name"]).strip(), str(r["new_name"]).strip()
        if not new or new.lower() in ("nan", "none") or new == old:
            continue
        _RENAMES.setdefault(str(r["study_key"]), {})[old] = (new, str(r.get("new_role", "")))
    return _RENAMES


# ======================================================================================
# R3 roles
# ======================================================================================
FLAG_RX = re.compile(
    r"^(cov_(attention|attn|check|manipulation|exclusion|screener)|attention|attn|flag_)", re.I)


def canon_role(old_role, name):
    r = str(old_role).strip().lower()
    n = str(name)
    if r in ("pid", "condition", "factor", "flag", "metacog", "item"):
        return r
    if r in ("cov", "covariate"):
        return "flag" if FLAG_RX.match(n) else "covariate"
    if r == "dv":
        return "metacog" if DR.is_confidence_name(n) else "dv"
    if n.startswith("flag_") or FLAG_RX.match(n):
        return "flag"
    if n.startswith("metacog_") or DR.is_confidence_name(n):
        return "metacog"
    if n.startswith("item_"):
        return "item"
    if n.startswith("dv_"):
        return "dv"
    if n.startswith("cov_"):
        return "covariate"
    return "other"


# ======================================================================================
# R4 metacog
# ======================================================================================
_DOM_TOKENS = ("credibility", "belief_accuracy", "attitude_policy", "sharing_intention",
               "technique_recognition", "trust", "other", "search_behavior",
               "media_literacy_attitudes", "critical_thinking")


def mc_name(name):
    toks = [t for t in re.split(r"[_\-]+", str(name)) if t]
    toks = [t for t in toks if t.lower() not in ("dv", "cov", "confidence", "certainty", "conf")]
    if toks and toks[0].lower() in _DOM_TOKENS:
        toks.pop(0)
    return "_".join(["metacog", "confidence"] + toks)


# ======================================================================================
# R1 AGE
# ======================================================================================
AGE_SPECS = {
    "BOWLES et al. (2025)": dict(
        kind="dummies",
        dummies={"val_q2_1": 21.0, "val_q2_2": 29.5, "val_q2_3": 39.5,
                 "val_q2_4": 49.5, "val_q2_5": 60.0},
        bands={"val_q2_1": "18_24", "val_q2_2": "25_34", "val_q2_3": "35_44",
               "val_q2_4": "45_54", "val_q2_5": "55_plus"},
        source="age-band dummies 18-24/25-34/35-44/45-54/55+; midpoint A; open top 55+ = 55+10/2",
        bounds=(21.0, 60.0)),
    "Fendt et al. (2023)": dict(
        kind="categorical", orig="AGE",
        mapping={2: 18.5, 3: 22.0, 4: 27.0, 5: 32.0, 6: 37.0, 7: 42.0, 8: 47.0,
                 9: 52.0, 10: 57.0, 11: 62.0, 12: 67.5},
        source="SPSS value labels (18-19..65+); midpoint A; open top 65+ = 65+5/2",
        bounds=(18.5, 67.5)),
    "Jamieson et al. (2025)": dict(
        kind="categorical", orig="all_age",
        mapping={1: 23.5, 2: 39.5, 3: 57.0, 4: 72.5},
        source="codebook: 18-29/30-49/50-64/65+; midpoint A; open top 65+ = 65+15/2",
        bounds=(23.5, 72.5)),
    "Leder et al. (2024)": dict(
        kind="categorical", orig="DS01",
        mapping={1: 12.0, 2: 23.5, 3: 39.5, 4: 60.0},
        source="notes: under 18/18-29/30-49/over 50; midpoint A; open bottom under18=18-12/2; open top 50+=50+20/2",
        bounds=(12.0, 60.0)),
    "Linegar et al. (2025)": dict(
        kind="categorical_text", orig="Age_Group",
        mapping={"Under 30": 22.5, "30-44": 37.0, "45-64": 54.5, "65+": 75.0},
        source="text bands Under30/30-44/45-64/65+; midpoint A; open bottom=30-15/2; open top 65+=65+20/2",
        bounds=(22.5, 75.0)),
    "Neylan et al. (2023)": dict(
        kind="categorical_text", orig="Age",
        mapping={"18-24": 21.0, "25-34": 29.5, "35-44": 39.5, "45-54": 49.5, "55 or older": 60.0},
        source="text bands 18-24..55+; midpoint A; open top 55+ = 55+10/2",
        bounds=(21.0, 60.0)),
    "Roozenbeek et al. (2022 b)": dict(
        kind="categorical", orig="Age",
        mapping={1: 23.5, 2: 39.5, 3: 60.0},
        source="notes 1=18-29,2=30-49,3=50+; midpoint A; open top 50+ = 50+20/2",
        bounds=(23.5, 60.0)),
    "Saleh et al. (2024)": dict(
        kind="categorical_text", orig="Age",
        mapping={"18 - 24": 21.0, "25 - 34": 29.5, "35 - 44": 39.5, "45 - 54": 49.5,
                 "55 - 64": 59.5, "65 - 74": 69.5, "75 - 84": 79.5},
        source="text bands 18-24..75-84 (all closed); midpoint A",
        bounds=(21.0, 79.5)),
    "Traberg et al. (2024 b)": dict(
        kind="categorical_text", orig="AgePretest",
        mapping={"18-29": 23.5, "30-49": 39.5, "Over 50": 60.0},
        source="notes 18-29/30-49/over 50; midpoint A; open top 50+ = 50+20/2",
        bounds=(23.5, 60.0)),
    "Williams & Bond (2020)": dict(
        kind="categorical", orig="Age",
        mapping={0: 21.0, 1: 34.5, 2: 52.0, 3: 67.5},
        source="SPSS labels 1=18-24,2=25-44,3=45-59,4=60+; harmonized cat.codes shifted -1; midpoint A; open top 60+=60+15/2",
        bounds=(21.0, 67.5)),
    "Winters et al. (2025)": dict(
        kind="categorical_text", orig="age_cat",
        mapping={"18-24": 21.0, "25-29": 27.0, "30-34": 32.0, "35-39": 37.0,
                 "40-44": 42.0, "45+": 47.5},
        source="text bands 18-24..45+; midpoint A; open top 45+ = 45+5/2",
        bounds=(21.0, 47.5)),
}
HARROP = dict(
    artifact_maps={"main": {1: 23.5, 2: 39.5, 3: 60.0},
                   "2": None,
                   "3": {1: 21.0, 2: 29.5, 3: 39.5, 4: 49.5, 5: 59.5, 6: 70.0}},
    source="per-artifact: main 1=18-29/2=30-49/3=50+; artifact2 age in years; artifact3 1=18-24..6=65+; midpoint A",
    bounds=(18.0, 70.0))
AGE_CONTINUOUS_EXISTS = {
    "Calvillo et al. (2026)": "age_3groups",
    "Craig & Vijaykumar (2023)": "AGE_GROUP",
    "Orosz et al. (2024)": "Age_recoded",
    "Stekelenburg et al. (2021)": "Age_cat",
}
AGE_SKIP = {
    "Agadjanian et al. (2019)": "agegroup codes 2-8 with NO documented band labels "
                                "(datainfo PDF and do-file carry none; not coded in variables.csv)",
}


def _find_col(side, df, orig, prefer=()):
    m = side[side["original_variable"].astype(str).str.strip() == str(orig).strip()]
    for _, r in m.iterrows():
        uv = str(r["uniform_variable"])
        if uv in df.columns:
            return uv
    for p in prefer:
        if p in df.columns:
            return p
    return None


def _age_side_row(source, bounds):
    note = ("cov_age_source=derived:bin_midpoint; midpoint rule A ((l+u)/2, open bins "
            f"extended by adjacent width); bounds min={bounds[0]} max={bounds[1]}; {source}")
    return {"uniform_variable": "cov_age", "role": "covariate", "dv_domain": "", "veracity": "",
            "timepoint": "", "dv_polarity": "", "original_variable": "derived from binned age",
            "notes": note}


def _rename_pair(wide, old, new):
    """Rename a column and its `_01` twin (if present)."""
    if old == new:
        return wide
    for suffix in ("", "_01"):
        if old + suffix in wide.columns:
            if new + suffix in wide.columns:
                wide = wide.drop(columns=[old + suffix])
            else:
                wide = wide.rename(columns={old + suffix: new + suffix})
    return wide


def _set_side(side, old_name, new_name):
    m = side["uniform_variable"].astype(str) == str(old_name)
    if m.any():
        side.loc[m, "uniform_variable"] = new_name
    return side


def apply_age(key, side, wide, rec):
    touched = []
    if key in AGE_SKIP:
        rec["age"] = f"SKIPPED: {AGE_SKIP[key]}"
        return side, wide, touched, False
    if key == "HARROP et al. (2023)":
        bin_col = _find_col(side, wide, "Age", ["cov_age", "cov_age_band_raw", "cov_age_bin"])
        if bin_col is None:
            rec["age"] = "SKIPPED: no Age column"
            return side, wide, touched, False
        art = wide["artifact"].astype(str) if "artifact" in wide.columns else pd.Series("main", index=wide.index)
        vals = pd.to_numeric(wide[bin_col], errors="coerce")
        age = pd.Series(np.nan, index=wide.index, dtype=float)
        for a, mp in HARROP["artifact_maps"].items():
            mask = art == a
            age.loc[mask] = vals.loc[mask] if mp is None else vals.loc[mask].map(mp)
        if bin_col != "cov_age_bin":
            wide = _rename_pair(wide, bin_col, "cov_age_bin")
            touched.append((bin_col, "cov_age_bin"))
            side = _set_side(side, bin_col, "cov_age_bin")
        wide["cov_age"] = age
        side = side[side["uniform_variable"].astype(str) != "cov_age"]
        side = pd.concat([side, pd.DataFrame([_age_side_row(HARROP["source"], HARROP["bounds"])])],
                         ignore_index=True)
        rec["age"] = f"derived cov_age per artifact; raw {bin_col}->cov_age_bin"
        return side, wide, touched, True
    if key in AGE_CONTINUOUS_EXISTS:
        bin_col = _find_col(side, wide, AGE_CONTINUOUS_EXISTS[key],
                            ["cov_age_band_raw", "cov_age_group", "cov_age_cat",
                             "cov_age_category", "cov_age_recoded", "cov_age"])
        if bin_col and bin_col != "cov_age_bin":
            wide = _rename_pair(wide, bin_col, "cov_age_bin")
            touched.append((bin_col, "cov_age_bin"))
            side = _set_side(side, bin_col, "cov_age_bin")
        rec["age"] = f"continuous cov_age already coded; raw bin {bin_col} -> cov_age_bin"
        return side, wide, touched, bool(touched)
    spec = AGE_SPECS.get(key)
    if spec is None:
        rec["age"] = "-"
        return side, wide, touched, False
    if spec["kind"] == "dummies":
        cols = {}
        for o, mid in spec["dummies"].items():
            c = _find_col(side, wide, o, ())
            if c:
                cols[c] = (mid, o)
        if not cols:
            rec["age"] = "SKIPPED: dummy columns not found"
            return side, wide, touched, False
        num = pd.DataFrame({c: pd.to_numeric(wide[c], errors="coerce") for c in cols})
        age = sum(num[c] * mid for c, (mid, _) in cols.items())
        age = age.where(num.fillna(0).sum(axis=1) > 0, np.nan)
        for c, (mid, o) in cols.items():
            newc = "cov_age_bin_" + spec["bands"].get(o, "")
            if c != newc and newc not in wide.columns:
                wide = _rename_pair(wide, c, newc)
                touched.append((c, newc))
                side = _set_side(side, c, newc)
        wide["cov_age"] = age
        side = side[side["uniform_variable"].astype(str) != "cov_age"]
        side = pd.concat([side, pd.DataFrame([_age_side_row(spec["source"], spec["bounds"])])],
                         ignore_index=True)
        rec["age"] = f"derived cov_age from {len(cols)} band dummies -> cov_age_bin_*"
        return side, wide, touched, True
    bin_col = _find_col(side, wide, spec["orig"], ["cov_age_band_raw", "cov_age_bin", "cov_age"])
    if bin_col is None:
        rec["age"] = f"SKIPPED: no raw column for orig={spec['orig']!r}"
        return side, wide, touched, False
    raw = wide[bin_col]
    if spec["kind"] == "categorical":
        age = pd.to_numeric(raw, errors="coerce").map(spec["mapping"])
    else:
        age = raw.astype(str).str.strip().map(spec["mapping"])
    if bin_col != "cov_age_bin":
        wide = _rename_pair(wide, bin_col, "cov_age_bin")
        touched.append((bin_col, "cov_age_bin"))
        side = _set_side(side, bin_col, "cov_age_bin")
    wide["cov_age"] = age
    side = side[side["uniform_variable"].astype(str) != "cov_age"]
    side = pd.concat([side, pd.DataFrame([_age_side_row(spec["source"], spec["bounds"])])],
                     ignore_index=True)
    rec["age"] = f"derived cov_age (midpoint) from {bin_col} -> cov_age_bin"
    return side, wide, touched, True


# ======================================================================================
# R2 ATTENTION
# ======================================================================================
def _norm(v):
    s = str(v).strip()
    if s.lower() in ("nan", "none", ""):
        return None
    if s in ("True", "False"):
        return s
    try:
        return float(s)
    except ValueError:
        return s


ATT_SPECS = {
    "Benegal & Scruggs (2018)": dict(
        checks=[dict(name="article_summary", col="flag_attention_pass", by="name", pass_=[1.0])],
        source="existing flag_attention_pass (1=correct summary); lossless pass-indicator conversion"),
    "Brugman et al. (2026)": dict(
        checks=[dict(name="attention_check", col="Attention_check", by="orig", pass_=[6.0]),
                dict(name="topic", col="AttentionQuestion1", by="orig", pass_=[1.0])],
        source="Syntax.sps SELECT IF Attention_check = 6 and AttentionQuestion1 = 1"),
    "Butler & Swire-Thompson (2026)": dict(
        checks=[dict(name="effort", col="Effort", by="orig", fail_=[3.0])],
        source="Rmd filter(Effort != 3): Effort == 3 = no attention (fail)"),
    "Fendt et al. (2026 b) Exp2": dict(
        checks=[dict(name="usable_response", col="usable_response", by="orig", pass_=[1.0])],
        source="usable_response: 1 = usable/honest (pass); other codes fail"),
    "Kotz et al. (2023)": dict(
        checks=[dict(name="check1", col="control1", by="orig", pass_=[1.0]),
                dict(name="check2", col="control2", by="orig", pass_=[1.0])],
        source="sidecar notes: 1 = correct on both control items"),
    "Kreuer et al. (2026)": dict(
        checks=[dict(name="check1", col="flag_attention_check_1", by="name", pass_=[1.0]),
                dict(name="check2", col="flag_attention_check_2", by="name", pass_=[2.0]),
                dict(name="check3", col="flag_attention_check_3", by="name", pass_=[2.0])],
        source="exclusions.csv keep codes 1 / 2 / 2"),
    "Kuru (2026)": dict(
        checks=[dict(name="boost_arithmetic", col="boost2", by="orig", pass_=[4.0]),
                dict(name="boost_always", col="boost3", by="orig", pass_=[5.0]),
                dict(name="w2_middle", col="W2_Q1", by="orig", pass_=[4.0]),
                dict(name="w2_arithmetic", col="W2_Q537", by="orig", pass_=[3.0]),
                dict(name="w2_always", col="W2_attentioncheck1", by="orig", pass_=[5.0])],
        source="sidecar item texts document the correct answer for 5 checks"),
    "Linegar et al. (2025)": dict(
        checks=[dict(name="i1", col="attention_1_correct", by="orig", pass_=["True", 1.0]),
                dict(name="i2", col="attention_2_correct", by="orig", pass_=["True", 1.0])],
        source="items named *_correct; True = passed"),
    "Orosz et al. (2023)": dict(
        checks=[dict(name="random", col="Random", by="orig", pass_=[2.0])],
        source="sidecar notes: 2 = never random (pass), 1 = answered randomly (fail)"),
    "Oswald et al. (2026)": dict(
        checks=[dict(name="comprehension", col="comp1", by="orig", pass_=[3.0])],
        source="sidecar notes: correct answer = 3"),
    "Panizza et al. (2022)": dict(
        checks=[dict(name="two_checks", col="attentionchecks", by="orig",
                     score_map={3.0: 0.0, 1.0: 1.0, -1.0: 2.0}, k=2)],
        source="sidecar notes: score -3..3, 3 = passed both -> fail_n=(3-score)/2, k=2"),
    "Prike et al. (2024)": dict(
        checks=[dict(name="data_honesty", col="Data_Honesty", by="orig",
                     pass_=["Yes; I put in a reasonable effort"])],
        source="Data_Honesty: 'reasonable effort' = pass"),
    "Reynolds & Estrada-Reynolds (2026)": dict(
        checks=[dict(name="attn1", col="attn1", by="orig", pass_=[1.0]),
                dict(name="attn2", col="attn2", by="orig", pass_=[1.0])],
        source="sidecar notes: attention-check pass items (1 = pass)"),
    "Seffen et al. (2026)": dict(
        checks=[dict(name="trust_attention_check", col="trust_attention_check", by="orig", pass_=[3.0])],
        source="sidecar notes / analysis.R filter(trust_attention_check == 3)"),
    "Stekelenburg et al. (2021)": dict(
        checks=[dict(name="ac_0", col="ac_0", by="orig", pass_=["False", 0.0]),
                dict(name="ac_1", col="ac_1", by="orig", pass_=["False", 0.0]),
                dict(name="ac_2", col="ac_2", by="orig", pass_=["False", 0.0]),
                dict(name="ac_3", col="ac_3", by="orig", pass_=["False", 0.0])],
        source="analysis.R ac_* == 'FALSE' = pass; four wave checks"),
    "Tay et al. (2022)": dict(
        checks=[dict(name="responsevalidity", col="responsevalidity", by="orig",
                     pass_=["Yes; I put in a reasonable effort"])],
        source="responsevalidity: 'reasonable effort' = pass"),
    "van Erkel et al. (2026)": dict(
        checks=[dict(name="straight_1", col="straight_1", by="orig", pass_=[0.0])],
        source="Do file drop if straight_1 == 1 -> 0 = pass"),
    "van Huijstee et al. (2026)": dict(
        checks=[dict(name="attention_check", col="Attention_Check", by="orig", pass_=[5.0])],
        source="paper: pass = 5"),
    "HARROP et al. (2023)": dict(
        checks=[dict(name="attention_check", col="flag_attention_check", by="name", pass_=[1.0])],
        source="author recoded flag: 1 = keep/pass (AttentionCheck == 'Play a game')"),
    "Saling et al. (2024)": dict(
        checks=[dict(name="attention_check", col="AttCheck", by="orig", pass_=[3.0])],
        source="paper: 'avocado juice' = 3; delivered data marks 3 = passed/kept"),
    "Jamieson et al. (2025)": dict(
        checks=[dict(name="ladapo_topic", col="check2r", by="orig", pass_=[2.0])],
        source="check2r = correct identification of Ladapo topic; 2 = correct (matches raw check2 == 1)",
        confidence="low"),
}
ATT_SKIP = {
    "Appel et al. (2025)": "manipulation column + 'Pass' column with undocumented missingness; correct options not documented",
    "Biddlestone et al. (2025 c)": "6 raw columns hold ' ' vs '3'; no documented correct option",
    "Butler et al. (2024)": "prompt_rating is a 0-7 self-report quality rating, not a pass/fail check",
    "Capewell et al. (2024)": "per-item content/date checks with free-text answers; trap item(s) not documented",
    "Fendt (2026)": "miniQ_Score is a continuous quality score with no documented threshold",
    "Fujimoto et al. (2026)": "0/1/2 responses with no documented correct option",
    "Graham & Porter (2025)": "catch columns hold year values; the correct year is not documented",
    "Howe et al. (2024)": "two comprehension items 1-4 with no documented correct option",
    "Kankova & Matthes (2026)": "summed attention score constant (1) + item matrix without a documented trap item",
    "Kuru (2025)": "attentioncheck1/2 constant; typecheck correct option not documented",
    "Lin et al. (2024)": "attentionZ is a continuous accuracy z-score, not a pass/fail item",
    "McPhedran et al. (2023)": "quality_control constant (=3); correct option not documented",
    "Neylan et al. (2023)": "free-text responses; sidecar explicitly notes the correct option is undocumented",
    "Pennycook et al. (2020)": "screener matrices (screen1/2/3) + CRT self-report; trap item(s) not documented",
    "Somoray & Miller (2023)": "AttCheck2_Q1 constant (=2); correct option not documented",
    "Spampatti et al. (2024)": "'soft' attention check with no documented correct option",
}


def _resolve_att_col(side, wide, chk):
    if chk["by"] == "name":
        return chk["col"] if chk["col"] in wide.columns else None
    return _find_col(side, wide, chk["col"], [chk["col"]])


def apply_attention(key, side, wide, rec):
    spec = ATT_SPECS.get(key)
    if spec is None:
        rec["attention"] = f"SKIPPED: {ATT_SKIP[key]}" if key in ATT_SKIP else "-"
        return side, wide, False
    fails, used = [], []
    for chk in spec["checks"]:
        col = _resolve_att_col(side, wide, chk)
        if col is None:
            continue
        if "score_map" in chk:
            k = int(chk["k"])
            fn = pd.to_numeric(wide[col], errors="coerce").map(chk["score_map"])
            for j in range(k):
                fails.append((fn > j).astype(float).where(fn.notna()))
            used.append(f"{chk['name']}(k={k})")
            continue
        raw = wide[col].map(_norm)
        na = raw.isna()
        pv = {_norm(x) for x in chk.get("pass_", [])}
        fv = {_norm(x) for x in chk.get("fail_", [])}
        if pv:
            f = raw.map(lambda v: 0.0 if v in pv else 1.0).where(~na)
        else:
            f = raw.map(lambda v: 1.0 if v in fv else 0.0).where(~na)
        fails.append(f)
        used.append(chk["name"])
    if not fails:
        rec["attention"] = "SKIPPED: no check columns resolved"
        return side, wide, False
    F = pd.concat(fails, axis=1)
    n_fail = F.sum(axis=1, skipna=True)
    n_obs = F.notna().sum(axis=1)
    pct = pd.Series(np.where(n_obs > 0, np.round(100.0 * n_fail / n_obs), np.nan), index=wide.index)
    wide["flag_attention_fail_n"] = n_fail
    wide["flag_attention_n_checks"] = n_obs
    wide["flag_attention_fail_pct"] = pct
    wide["flag_attention_pass"] = pd.Series(
        np.where(pct.notna(), (pct == 0).astype(float), np.nan), index=wide.index)
    for c in ("flag_attention_fail_pct_01", "flag_attention_pass_01", "flag_attention_fail_n_01"):
        if c in wide.columns:
            wide = wide.drop(columns=[c])
    new_rows = [
        ("flag_attention_fail_pct",
         f"percentage of attention checks failed (0-100); derived arithmetic; {spec['source']}; "
         f"checks: {', '.join(used)}; values grid by k"),
        ("flag_attention_pass", "1 iff flag_attention_fail_pct == 0"),
        ("flag_attention_fail_n", "number of attention checks failed (audit)"),
        ("flag_attention_n_checks", "number of non-missing attention checks for the row"),
    ]
    have = set(side["uniform_variable"].astype(str))
    for name, note in new_rows:
        if name in have:
            side.loc[side["uniform_variable"].astype(str) == name, "role"] = "flag"
            continue
        side = pd.concat([side, pd.DataFrame([{
            "uniform_variable": name, "role": "flag", "dv_domain": "", "veracity": "",
            "timepoint": "", "dv_polarity": "", "original_variable": "derived",
            "notes": note}])], ignore_index=True)
    rec["attention"] = f"derived flag_attention_fail_pct/pass/n_checks from [{', '.join(used)}]"
    return side, wide, True


def ensure_exclusion_aliases(key, folder, side, wide, rec):
    """Keep the existing `<Key> - exclusions.csv` attention rules working after the rename."""
    p = os.path.join(folder, key + " - exclusions.csv")
    if not os.path.isfile(p):
        return side, wide, 0, []
    try:
        ex = pd.read_csv(p, encoding="utf-8-sig")
    except Exception:
        return side, wide, 0, []
    if "flag" not in ex.columns:
        return side, wide, 0, []
    made = []
    for flag in ex["flag"].astype(str):
        if not flag.startswith("flag_attention") or flag in wide.columns:
            continue
        cand = None
        if flag == "flag_attention_check":
            cand = "flag_attention_raw" if "flag_attention_raw" in wide.columns else None
        elif flag.startswith("flag_attention_check_"):
            suffix = flag[len("flag_attention_check_"):]
            for c in (f"flag_attention_raw_{suffix}", f"flag_attention_raw_i{suffix}",
                      f"flag_attention_raw_{suffix}_i1"):
                if c in wide.columns:
                    cand = c
                    break
        if cand is None:
            continue
        wide[flag] = wide[cand]
        made.append((flag, cand))
        have = set(side["uniform_variable"].astype(str))
        if flag not in have:
            side = pd.concat([side, pd.DataFrame([{
                "uniform_variable": flag, "role": "flag", "dv_domain": "", "veracity": "",
                "timepoint": "", "dv_polarity": "", "original_variable": f"alias of {cand}",
                "notes": f"alias of {cand} kept so the existing exclusions.csv rule applies"}])],
                ignore_index=True)
    rec["att_aliases"] = len(made)
    return side, wide, len(made), made


# ======================================================================================
# orchestrator
# ======================================================================================
def finalize(key, side, wide):
    """Apply every rule; return (side, wide, records)."""
    records = []
    for c in ("uniform_variable", "role", "dv_domain", "veracity", "timepoint",
              "dv_polarity", "original_variable", "notes"):
        if c not in side.columns:
            side[c] = ""
    rec = {}

    # leftover renames (idempotent once the rename pass has run)
    rmap = load_rename_map().get(key, {})
    for old, (new, _nr) in list(rmap.items()):
        mask = side["uniform_variable"].astype(str) == old
        if not mask.any():
            continue
        # never rename a row this pass derived (it is not the original coded variable)
        derived = side.loc[mask, "original_variable"].astype(str).str.startswith("derived")
        if bool(derived.all()):
            continue
        if bool(derived.any()):
            mask = mask & ~derived
        for suffix in ("", "_01"):
            if old + suffix in wide.columns:
                if new + suffix in wide.columns:
                    wide = wide.drop(columns=[old + suffix])
                else:
                    wide = wide.rename(columns={old + suffix: new + suffix})
        side.loc[mask, "uniform_variable"] = new
        records.append(dict(rule="rename_leftover", old_name=old, new_name=new,
                            detail="from rename map"))
    for old, (new, _nr) in list(rmap.items()):
        # never rename a column this pass derives (cov_age / flag_attention_* / metacog_*)
        if old == "cov_age" or old.startswith(("flag_attention", "metacog_")):
            continue
        if old in wide.columns and new not in wide.columns:
            wide = _rename_pair(wide, old, new)
            records.append(dict(rule="rename_leftover", old_name=old, new_name=new,
                                detail="wide-only from rename map"))

    # R3 roles
    old_roles = side["role"].astype(str).tolist()
    new_roles = [canon_role(r, n) for r, n in zip(old_roles, side["uniform_variable"])]
    diffs = [i for i, (o, n) in enumerate(zip(old_roles, new_roles)) if o.strip().lower() != n]
    if diffs:
        if "role_original" not in side.columns:
            side["role_original"] = ""
        side.loc[side.index[diffs], "role_original"] = [old_roles[i] for i in diffs]
        for i in diffs:
            records.append(dict(rule="role",
                                old_name=str(side.loc[side.index[i], "uniform_variable"]),
                                new_name=str(side.loc[side.index[i], "uniform_variable"]),
                                detail=f"{old_roles[i]}->{new_roles[i]}"))
    side["role"] = new_roles
    rec["role_changes"] = len(diffs)

    # R4 metacog
    mc = 0
    for i in list(side.index):
        name = str(side.loc[i, "uniform_variable"])
        if name.startswith("metacog_") or not DR.is_confidence_name(name):
            continue
        new = rmap.get(name, (None, None))[0] or mc_name(name)
        if new == name:
            new = re.sub(r"^(dv|cov)_", "metacog_", name)
        if new in wide.columns and new != name:
            for c in [new, new + "_01"]:
                if c in wide.columns:
                    wide = wide.drop(columns=[c])
        for suffix in ("", "_01"):
            if name + suffix in wide.columns:
                wide = wide.rename(columns={name + suffix: new + suffix})
        side.loc[i, "uniform_variable"] = new
        side.loc[i, "role"] = "metacog"
        note = str(side.loc[i, "notes"])
        if "metacog namespace" not in note:
            side.loc[i, "notes"] = (note + " | " if note and note != "nan" else "") + \
                "metacog namespace (R4); excluded from SDT and primary meta pools via NON_OUTCOME_DOMAINS/is_confidence_name"
        mc += 1
        records.append(dict(rule="metacog", old_name=name, new_name=new,
                            detail="dv/cov_confidence->metacog_confidence"))
    rec["metacog_renamed"] = mc

    # R1 age
    side, wide, age_touched, age_added = apply_age(key, side, wide, rec)
    for old, new in age_touched:
        records.append(dict(rule="age", old_name=old, new_name=new,
                            detail="binned age -> cov_age_bin"))
    if age_added:
        records.append(dict(rule="age", old_name="", new_name="cov_age",
                            detail=rec.get("age", "derived continuous cov_age")))
    if age_added and "cov_age_01" not in wide.columns and "cov_age" in wide.columns:
        s = pd.to_numeric(wide["cov_age"], errors="coerce")
        lo, hi = s.min(), s.max()
        if pd.notna(lo) and pd.notna(hi) and hi > lo:
            wide["cov_age_01"] = (s - lo) / (hi - lo)

    # R2 attention
    side, wide, att_added = apply_attention(key, side, wide, rec)
    if att_added:
        records.append(dict(rule="attention", old_name="", new_name="flag_attention_fail_pct",
                            detail=rec.get("attention", "derived attention failure percentage")))

    # the attention flag family carries no `_01` twin (author-check provenance is verbatim)
    drop = [c for c in wide.columns
            if str(c).startswith("flag_attention") and str(c).endswith("_01")]
    if drop:
        wide = wide.drop(columns=drop)

    # R5 veracity-dependent dv_polarity (Scripts/DV_POLARITY_FIX.md):
    # reanalysis.py's d' = z(HR) - z(FAR) reads the false-stimulus DV as a
    # correct-rejection rate, so behaviour/credibility DVs must be oriented
    # per veracity (true -> higher=hit, false -> higher=correct-reject).
    try:
        import dv_polarity as _dvp
        side, _n_pol, _pol_ch = _dvp.normalize_sidecar(side)
        rec["polarity_changed"] = _n_pol
        for pc in _pol_ch:
            records.append(dict(rule="polarity", old_name=pc["uniform_variable"],
                                new_name=pc["uniform_variable"],
                                detail=f"{pc['old'] or '(blank)'} -> {pc['new']} "
                                       f"[{pc['rule']}, veracity={pc['veracity']}]"))
    except Exception as _pol_err:  # pragma: no cover - keep harmonization alive
        rec["polarity_error"] = str(_pol_err)

    rec.update(records=records)
    return side, wide, records, rec


def finalize_side(key, side):
    """Harmonizer hook: rule changes that affect only the sidecar are applied together with
    the wide table by `finalize`; this keeps the public hook for sidecar-only call sites."""
    return side
