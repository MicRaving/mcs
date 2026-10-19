"""Per-study harmonizer (copied into each Datasets/<Study Key>/ folder as harmonizer.py).

Unifies a study's data into ONE WIDE table `<Study Key> - harmonized.csv` (one row per
participant/session, one column per variable), plus a sidecar `<Study Key> - harmonized_variables.csv`
describing every uniform variable.

Design (per project decision):
- The merged data stays WIDE. Only the SDT re-analysis melts the `dv_*` columns to long, and
  it drops covariates.
- DV names are uniform (`dv_<domain>[_<veracity>][_<time>]`); `veracity` labels TRUE vs FALSE
  stimuli and `timepoint` labels pre/post/follow-up, both decoded from the name and recorded
  in the sidecar. `dv_domain` is taken from the study's `variables.csv` when present, otherwise
  it is recovered from the uniform-name prefix (the naming convention guarantees `dv_<domain>_...`).
- Covariates are labelled uniformly from the controlled `cov_*` vocabulary below
  (one canonical name per construct; see Scripts/COVARIATE_VOCABULARY.md), so the
  participant-moderator panel is not fragmented by alias names.  Subscales of one
  construct are named `cov_<canonical>_<qualifier>`.  Attention/quality checks,
  timing, weights, condition codes, order variables and bookkeeping stay `flag_*`
  / `EXTRA_COLS`, never `cov_*`.
- Scale width is respected: for every numeric variable a `<name>_01` column is added, linearly
  rescaled to 0-1 within that variable's observed min/max, so a 1-7 scale cannot dominate a 0-1
  scale when pooled.

Inputs (whatever exists in the folder):
  <Study Key> - variables.csv      original_variable, uniform_variable|uniform_name, role, dv_domain
  <Study Key> - dv_direction.csv   uniform_variable, include, direction
  <Study Key> - prepared*.csv      shape-preserved per-study data (wide)
Run:  python harmonizer.py

DV-LABEL DERIVATION (v2)
- `domain_of(uniform_name)` fills a missing `dv_domain` from the uniform-name prefix.
- `veracity_of` decodes true/false with optional explicit-token priority
  (`EXPLICIT_VERACITY_FIRST = True`) for studies whose names combine a manipulation flag with a
  veracity flag (e.g. `..._nonmanipulative_false_...` -> veracity=false).
- `STUDY_LABELS` gives a final per-variable override: {uniform_variable: {domain/veracity/timepoint}}.
- `EXTRA_COLS` retains exclusion/attention flags and analysis-sample provenance columns (renamed)
  so the author's per-model exclusions are PART OF THE HARMONIZATION, not only of the reproduction.
  Keep the raw codes (no `_01` rescaling) so the analysis layer can filter on them.
"""
import glob
import os
import re
import sys
import tempfile

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
KEY = os.path.basename(HERE)
_TMP = os.path.join(os.path.dirname(os.path.dirname(HERE)), "temp")
os.makedirs(_TMP, exist_ok=True)
tempfile.tempdir = _TMP

# --- per-study label hooks (edited per study when needed) -------------------
EXPLICIT_VERACITY_FIRST = False
STUDY_LABELS = {}

# Exclusion / attention flags and analysis-sample provenance: {original_col: harmonized_name}.
# These are retained verbatim (no _01 twin) so the per-model analytic sample can be reconstructed.
EXTRA_COLS = {}

# Per-DV orientation for the SDT layer: {uniform_variable: 'higher_is_better'|'higher_is_worse'}.
# 2026-09-20 (Scripts/DV_POLARITY_FIX.md): polarity is VERACITY-DEPENDENT for the
# behaviour/credibility domains -- reanalysis.py's d' = z(HR) - z(FAR) reads the
# false-stimulus DV as a correct-rejection rate, so true stimuli -> higher_is_better
# and false stimuli -> higher_is_worse (vice-versa for suspiciousness scales).
# 2026-09-23 (W5-POL, Scripts/polarity_reconciliation.py): the VERACITY RULE is the
# fallback.  `dv_polarity.normalize_sidecar` below applies it here, but the runner
# post-hook (`dv_polarity.normalize_sidecar_file` in run_harmonizers.py /
# reanalysis.py) then re-applies the full PRECEDENCE: a study-coded polarity from
# `variables.csv` / `dv_direction.csv` wins when the reproduction arbitration
# verifies it, otherwise the rule fills in.  So this in-memory call only needs to
# be blank-preserving; the file-level normaliser is authoritative.
POLARITY = {}

# Author exclusion criteria, as rows for `<Key> - exclusions.csv`:
#   {"flag": "flag_x", "keep": "!=3", "applies_to": "dv_*_source_*;domain:credibility",
#    "reason": "failed treatment check"}   ('*' = all DVs; empty list = no exclusions)
EXCLUSIONS = []

# Allowed dv_domain values (schema v3).  2026-09-19: confidence/affect fold into
# `other`, engagement_behavior into `sharing_intention`, and `belief_accuracy` is
# merged into `credibility` (Scripts/domain_restriction.py, DV_DOMAIN_MERGE_LOG.md).
DOMAINS = ["credibility", "sharing_intention", "technique_recognition",
           "trust", "attitude_policy", "search_behavior", "media_literacy_attitudes",
           "critical_thinking", "other"]
# Legacy uniform-name prefixes still present in existing names (variable names are
# never renamed) that decode to the merged domain.
LEGACY_DOMAIN_MERGE = {"belief_accuracy": "credibility"}

# --- canonical cov_* participant-covariate vocabulary (Scripts/COVARIATE_VOCABULARY.md) --
# Name participant covariates with ONE canonical name per construct so the
# participant-moderator panel is not fragmented.  If a study has several distinct
# subscales of one construct, name them `cov_<canonical>_<qualifier>` (e.g.
# `cov_political_ideology_social`); the analysis layer
# (Scripts/covariate_restriction.py) maps both the bare canonical and its
# `cov_<canonical>_*` qualifiers to the canonical construct.
# Do NOT emit attention/quality checks, completion/response time, survey weights,
# condition/arm codes, order/counterbalance variables or bookkeeping as `cov_*`:
# keep them as `flag_*` (EXTRA_COLS) instead.  Record raw units in the sidecar
# `notes`.  Prefer a continuous `cov_age` in years; binned age is not a moderator
# (continuous covariates are analysed on the within-study 0-1 rescale).
CANONICAL_COVARIATES = [
    "cov_age", "cov_gender", "cov_education", "cov_education_parental",
    "cov_income", "cov_ethnicity", "cov_employment", "cov_language",
    "cov_relationship", "cov_household_size", "cov_country", "cov_region",
    "cov_political_ideology", "cov_political_party", "cov_political_interest",
    "cov_political_knowledge", "cov_vote_behavior", "cov_trust_government",
    "cov_analytic_thinking", "cov_conspiracy_belief", "cov_prior_knowledge",
    "cov_news_consumption", "cov_social_media_use", "cov_digital_literacy",
    "cov_media_literacy", "cov_need_for_closure", "cov_bullshit_receptivity",
    "cov_intellectual_humility", "cov_actively_open_minded_thinking",
    "cov_religiosity", "cov_climate_attitudes", "cov_covid_concern",
    "cov_health_literacy", "cov_health_status", "cov_vaccine_status",
    "cov_vaccine_attitudes", "cov_self_efficacy", "cov_risk_perception",
    "cov_trust_in_science", "cov_trust_in_media", "cov_trust_general",
]

# Common off-vocabulary names seen in the corpus -> canonical.  Used only as a
# fallback hint when Scripts/covariate_restriction.py is not importable.
COV_ALIAS_HINTS = {
    "cov_politics": "cov_political_ideology",
    "cov_political": "cov_political_ideology",
    "cov_ideology": "cov_political_ideology",
    "cov_political_orientation": "cov_political_ideology",
    "cov_crt": "cov_analytic_thinking",
    "cov_numeracy": "cov_analytic_thinking",
    "cov_cognitive_ability": "cov_analytic_thinking",
    "cov_birth_year": "cov_age",
    "cov_age_recoded": "cov_age",
    "cov_male": "cov_gender",
    "cov_female": "cov_gender",
    "cov_sex": "cov_gender",
    "cov_parental_ed": "cov_education_parental",
    "cov_ses": "cov_income",
    "cov_partisanship": "cov_political_party",
    "cov_twitter_use": "cov_social_media_use",
    "cov_religion": "cov_religiosity",
}


def canonical_cov_name(name):
    """Canonical `cov_*` name for `name` (shared vocabulary), else `name`."""
    try:
        import covariate_restriction as _cr
        return _cr.canonical_cov(name) or str(name)
    except Exception:
        return COV_ALIAS_HINTS.get(str(name), str(name))


def _apply_cov_vocabulary(side, wide):
    """Best-effort canonicalisation of this study's `cov_*` names (future runs).

    Renames a covariate and its `_01` twin only when the canonical name is free,
    so distinct subscales of one construct are never silently merged; collisions
    keep the study name and are reported for the author.  Existing sidecars are
    untouched -- this affects only harmonizations run with this template.
    """
    renamed, skipped = [], []
    have = set(side["uniform_variable"].astype(str))
    for i in list(side.index):
        uv = str(side.loc[i, "uniform_variable"])
        if not uv.startswith("cov_"):
            continue
        can = canonical_cov_name(uv)
        if can == uv:
            continue
        if can in wide.columns or (can in have and can != uv):
            skipped.append((uv, can))
            continue
        for suf in ("", "_01"):
            if uv + suf in wide.columns:
                wide = wide.rename(columns={uv + suf: can + suf})
        side.loc[i, "uniform_variable"] = can
        have.discard(uv)
        have.add(can)
        renamed.append((uv, can))
    if renamed:
        print(f"[{KEY}] cov vocabulary renames: {renamed}")
    if skipped:
        print(f"[{KEY}] cov vocabulary NOT renamed (canonical already present): {skipped}")
    return side, wide


def _find(pat):
    return sorted(glob.glob(os.path.join(HERE, pat)))


def _read(p):
    df = pd.read_csv(p, encoding="utf-8-sig", low_memory=False)
    df.columns = [str(c).strip().lstrip("\ufeff") for c in df.columns]
    return df


def domain_of(name):
    """Recover dv_domain from the uniform name prefix (dv_<domain>[_...]).

    `dv_belief_accuracy_*` names are kept (uniform variable names are never
    renamed) and decode to the merged `credibility` domain.
    """
    s = str(name)
    if not s.startswith("dv_"):
        return ""
    rest = s[3:]
    for dom in sorted(DOMAINS, key=len, reverse=True):
        if rest == dom or rest.startswith(dom + "_"):
            return dom
    for dom, merged in LEGACY_DOMAIN_MERGE.items():
        if rest == dom or rest.startswith(dom + "_"):
            return merged
    return ""


# Veracity tokens, anchored with `(?<![a-z]) ... (?![a-z])` rather than `\b`:
# uniform-name tokens are joined by `_`, which regex counts as a word character,
# so `\btrue\b` would NOT match `_true_`.  The lookarounds also reject longer
# words that merely *contain* a token -- a bare substring search classified
# "untrue" (contains "true"), "inaccurate" (contains "accurate") and "realistic"
# (contains "real") as TRUE stimuli.  `inaccurate`/`untrue` are false-veracity
# tokens; `realistic` carries no veracity at all.
_VER_FALSE = re.compile(
    r"(?<![a-z])(false|fake|inaccurate|untrue|misinformation|disinformation|"
    r"misleading|manipulative|hyperpartisan|fiction|fictitious|hoax)(?![a-z])",
    re.I)
_VER_TRUE = re.compile(
    r"(?<![a-z])(true|real|accurate|factual|legitimate|mainstream|"
    r"nonmanipulative|non_manipulative)(?![a-z])", re.I)


def veracity_of(name):
    s = str(name).lower()
    explicit_false = bool(_VER_FALSE.search(s))
    explicit_true = bool(_VER_TRUE.search(s))
    if EXPLICIT_VERACITY_FIRST:
        if explicit_false:
            return "false"
        if explicit_true:
            return "true"
    if explicit_true:
        return "true"
    if explicit_false:
        return "false"
    return ""


def timepoint_of(name):
    s = str(name).lower()
    if re.search(r"_pre(_|$)", s):
        return "pre"
    if re.search(r"_post(_|$)", s):
        return "post"
    if re.search(r"follow[_ ]?up", s):
        return "followup"
    return ""


def rename_forbid_flag01(original, renamed):
    """Safe rename target for the per-study variable-rename shim.

    A `cov_*` variable may legitimately be re-labelled `flag_*` (attention /
    quality check).  Its `_01` twin must then NOT also be emitted: the contract
    says `flag_*` columns carry no `_01` twin.  The shims call this and DROP the
    `_01` suffix (collapsing the twin onto the flag) instead of producing
    `flag_*_01`.  Any other rename is passed through unchanged.
    """
    if str(original).endswith("_01") and str(renamed).startswith("flag_"):
        return str(renamed)[:-3]
    return str(renamed)


def _varmap():
    """Merge every `<...> - variables.csv` / `variables.csv` in the folder.

    The old version returned the first match only, so multi-artifact studies silently lost
    the sidecar metadata for DVs defined in the other artifacts' variable maps; those
    `dv_*_01` columns were then melted by reanalysis.py with a blank domain and dropped.
    First occurrence of a uniform variable wins.
    """
    frames, seen = [], set()
    for p in _find("* - variables.csv") + _find("variables.csv"):
        v = _read(p)
        if "uniform_variable" not in v.columns and "uniform_name" in v.columns:
            v = v.rename(columns={"uniform_name": "uniform_variable"})
        if "uniform_variable" in v.columns:
            v = v[~v["uniform_variable"].astype(str).isin(seen)].copy()
            seen |= set(v["uniform_variable"].astype(str))
        frames.append(v)
    if not frames:
        return None
    import pandas as _pd
    return _pd.concat(frames, ignore_index=True)


def _write_exclusions():
    """Always emit `<Key> - exclusions.csv` (header-only when there are none)."""
    pd.DataFrame(EXCLUSIONS, columns=["flag", "keep", "applies_to", "reason"]).to_csv(
        os.path.join(HERE, KEY + " - exclusions.csv"), index=False, encoding="utf-8-sig")


def main():
    # Emit the exclusion map up front so the contract holds even when the
    # harmonization is skipped below (no varmap / no prepared data).
    _write_exclusions()
    vmap = _varmap()
    preps = [p for p in _find("* - prepared*.csv") if "variables" not in p and "dv_direction" not in p]
    if vmap is None or not preps:
        # 2026-09-28 (harmonization pilot): this used to `return` silently with exit 0,
        # leaving a folder that held only a header-only exclusions file -- visually
        # identical to "the study has no data" -- and it hid FOUR studies whose deposit
        # had actually arrived (Bauer & Wilson 2022, Bayrak et al. 2025 b, DeVerna 2025,
        # Lutzke 2019) until the pilot re-derived them.  Never skip silently again:
        # announce the missing input path so the gap is greppable, and do not pretend a
        # product was written.  A study WITH a deposit needs a study-specific harmonizer
        # (see Scripts/HARMONIZER_INSTRUCTIONS.md), not this template.
        print(f"[{KEY}] HARMONIZER_STATUS: no_input_path -- absent "
              f"'* - variables.csv' and '* - prepared*.csv'.  Derive from the raw deposit "
              f"with a study-specific harmonizer, or record 'no participant data' explicitly.")
        return 0
    for c in ("role", "dv_domain", "original_variable", "uniform_variable", "artifact"):
        if c not in vmap.columns:
            vmap[c] = ""
    ren = {str(r["original_variable"]): str(r["uniform_variable"])
           for _, r in vmap.iterrows()
           if str(r["uniform_variable"]) and str(r["original_variable"])}
    side = vmap[vmap["uniform_variable"].astype(str).str.match(r"^(dv_|cov_)")].copy()
    side["veracity"] = side["uniform_variable"].map(veracity_of)
    side["timepoint"] = side["uniform_variable"].map(timepoint_of)
    # Fill a missing dv_domain from the uniform-name prefix (keep coded values as-is).
    dom = side["dv_domain"].fillna("").astype(str).str.strip()
    fill = dom.isin(["", "nan", "None"])
    side.loc[fill, "dv_domain"] = side.loc[fill, "uniform_variable"].map(domain_of)
    # Final per-study overrides.
    if STUDY_LABELS:
        for var, lab in STUDY_LABELS.items():
            m = side["uniform_variable"].astype(str) == var
            if "dv_domain" in lab:
                side.loc[m, "dv_domain"] = lab["dv_domain"]
            if "veracity" in lab:
                side.loc[m, "veracity"] = lab["veracity"]
            if "timepoint" in lab:
                side.loc[m, "timepoint"] = lab["timepoint"]
    # Coder variable assignments (Scripts/variable_assignments.py): durable
    # decisions keyed on the raw (artifact, original_variable[, item_scope])
    # side.  Applied here so a standalone run honours every label assignment --
    # blanks are filled and a `confirmed` row overrides a decoded guess.  The
    # wide is not built yet, so wide-side renames/splits are the runner
    # post-hook's job (Scripts/analysis/run_harmonizers.py, reanalysis.py).
    try:
        import variable_assignments as _va
        side, _, _va_recs = _va.apply(KEY, HERE, side, None)
        _n_va = sum(1 for r in _va_recs
                    if r.get("action") in ("fill", "override", "add_row"))
        if _n_va:
            print(f"[{KEY}] variable assignments applied: {_n_va} change(s)")
    except Exception as _va_err:
        # Surface, do not swallow: a silent failure here writes a sidecar that
        # ignores coder decisions.
        print(f"[{KEY}] variable assignments skipped: {_va_err!r}")
    side_cols = ["uniform_variable", "role", "dv_domain", "veracity", "timepoint",
                 "dv_polarity", "original_variable", "notes"]
    side["dv_polarity"] = side["uniform_variable"].map(lambda v: POLARITY.get(str(v), ""))
    if "dv_polarity" in vmap.columns:
        coded = vmap.drop_duplicates("uniform_variable").set_index("uniform_variable")["dv_polarity"]
        mapped = side["uniform_variable"].map(coded)
        ok = mapped.notna() & (mapped.astype(str).str.strip() != "") & (mapped.astype(str).str.lower() != "nan")
        side.loc[ok, "dv_polarity"] = mapped[ok]
    side = side[[c for c in side_cols if c in side.columns]].drop_duplicates("uniform_variable")

    # Veracity-dependent polarity (Scripts/dv_polarity.py / DV_POLARITY_FIX.md):
    # applied last so it also covers DVs resolved from the coded variables.csv.
    try:
        import dv_polarity as _dvp
        side, _n_pol, _ = _dvp.normalize_sidecar(side)
        # The classifier must NEVER force a guessed "higher_is_better" where it
        # declines: such a row stays blank (or keeps an explicit coded value).
        # Blank any out-of-vocabulary value (e.g. legacy "ambiguous") that leaked
        # in through POLARITY or the coded variables.csv, and say so loudly.
        _allowed = {_dvp.HIGHER, _dvp.LOWER, ""}
        _pcur = side["dv_polarity"].fillna("").astype(str).str.strip().str.lower()
        _bad = ~_pcur.isin(_allowed)
        if _bad.any():
            print(f"[{KEY}] WARNING: blanking {int(_bad.sum())} out-of-vocabulary "
                  f"dv_polarity value(s) {sorted(set(_pcur[_bad]))}")
            side.loc[_bad, "dv_polarity"] = ""
    except Exception as _pol_err:
        # Surface, do not swallow: a silent failure here writes a wrong sidecar.
        print(f"[{KEY}] polarity normalization FAILED: {_pol_err!r}")
        raise

    wide = []
    for p in preps:
        bn = os.path.basename(p)
        art = "main"
        m = re.search(r" - prepared (\d+|[A-Za-z0-9]+)\.csv$", bn)
        if m:
            art = m.group(1)
        df = _read(p).rename(columns=ren)
        keep = [c for c in df.columns
                if c in ("pid", "condition", "condition_raw", "group", "cond")
                or str(c).startswith("dv_") or str(c).startswith("cov_")
                or c in EXTRA_COLS]
        df = df[keep].copy()
        if EXTRA_COLS:
            df = df.rename(columns={k: v for k, v in EXTRA_COLS.items() if k in df.columns})
        newcols = {}
        for c in df.columns:
            if str(c).startswith("dv_") or str(c).startswith("cov_"):
                s = pd.to_numeric(df[c], errors="coerce")
                lo, hi = s.min(), s.max()
                if pd.notna(lo) and pd.notna(hi) and hi > lo:
                    newcols[str(c) + "_01"] = (s - lo) / (hi - lo)
        if newcols:
            df = pd.concat([df, pd.DataFrame(newcols, index=df.index)], axis=1)
        df.insert(0, "artifact", art)
        df.insert(0, "study_key", KEY)
        wide.append(df)
    out = pd.concat(wide, ignore_index=True) if len(wide) > 1 else wide[0]
    # Future harmonizations emit canonical cov_* names (best-effort, collision-safe;
    # existing sidecars are never touched).  See Scripts/COVARIATE_VOCABULARY.md.
    side, out = _apply_cov_vocabulary(side, out)
    out.to_csv(os.path.join(HERE, KEY + " - harmonized.csv"), index=False, encoding="utf-8-sig")
    side.to_csv(os.path.join(HERE, KEY + " - harmonized_variables.csv"), index=False, encoding="utf-8-sig")
    _write_exclusions()
    print(f"[{KEY}] wide harmonized: {len(out):,} rows x {out.shape[1]} cols -> {KEY} - harmonized.csv")



# --- semantic homogenisation (age / attention / role / metacog) -------------------------
def _semhom_finalize():
    try:
        _semhom_root = HERE
        while _semhom_root and not os.path.isfile(os.path.join(_semhom_root, "Scripts", "semhom_rules.py")):
            _semhom_up = os.path.dirname(_semhom_root)
            if _semhom_up == _semhom_root:
                break
            _semhom_root = _semhom_up
        if os.path.join(_semhom_root, "Scripts") not in sys.path:
            sys.path.insert(0, os.path.join(_semhom_root, "Scripts"))
        import semhom_rules as _semhom
        _sp = os.path.join(HERE, KEY + " - harmonized_variables.csv")
        _wp = os.path.join(HERE, KEY + " - harmonized.csv")
        if not (os.path.isfile(_sp) and os.path.isfile(_wp)):
            return
        _s = pd.read_csv(_sp, encoding="utf-8-sig", low_memory=False)
        _w = pd.read_csv(_wp, encoding="utf-8-sig", low_memory=False)
        _s, _w, _recs, _meta = _semhom.finalize(KEY, _s, _w)
        _s, _w, _alias, _arecs = _semhom.ensure_exclusion_aliases(KEY, HERE, _s, _w, {})
        _w.to_csv(_wp, index=False, encoding="utf-8-sig")
        _s.to_csv(_sp, index=False, encoding="utf-8-sig")
        sys.stdout.write("[%s] semantic homogenisation applied\n" % KEY)
    except Exception as _semhom_err:
        print("[semhom] finalize skipped:", _semhom_err)


try:
    _semhom_finalize()
except Exception as _semhom_err:
    print("[semhom] finalize skipped:", _semhom_err)
if __name__ == "__main__":
    sys.exit(main())
