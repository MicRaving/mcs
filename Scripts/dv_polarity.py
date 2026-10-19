"""Veracity-dependent ``dv_polarity`` for the signal-detection (d-prime) layer.

Why this module exists
----------------------
``reanalysis.py`` computes a *discrimination* index

    d' = z(HR) - z(FAR),      FAR = 1 - CR

where the **true-veracity** DV supplies the hit rate ``HR`` and the
**false-veracity** DV supplies the correct-rejection rate ``CR`` (the code
reverses a ``higher_is_worse`` column with ``x -> 1 - x``).  Every DV must
therefore read **"higher = more desirable given its veracity"**:

    true  stimuli: higher = hit             (correctly accepting true material)
    false stimuli: higher = correct reject  (correctly rejecting false material)

For **endorsement** scales (sharing, belief, credibility, agreement, trust)
that means the false-stimulus variant must be reversed:

    true  -> higher_is_better      false -> higher_is_worse

For **suspicion** scales (perceived manipulativeness / misleadingness /
deepfake-ness) the desirable pole is inverted on *both* sides:

    true  -> higher_is_worse       false -> higher_is_better

The 2026-09-17 ``Scripts/POLARITY_LOG.md`` used a *scale-level* convention
(the true- and false-stimulus variants always shared one polarity).  That was
correct for the old simple-difference estimator ``d = mean(true) - mean(false)``
but is wrong for the current ``d'`` estimator, where the false-stimulus value
is read as a correct-rejection rate.  This module is the single source of truth
for the veracity-dependent domains; see ``Scripts/DV_POLARITY_FIX.md``.

Scope
-----
* ``sharing_intention`` and ``engagement_behavior`` (behaviour/endorsement):
  the PI rule -- true -> better, false -> worse.  A corrective/prosocial action
  on a false item (report / flag / block_channel / find_support / see_why /
  post_see / skip) stays ``higher_is_better``.
* ``credibility`` and ``belief_accuracy`` (credibility/endorsement): the
  belief pole follows the PI rule; a genuine suspicion/misleadingness rating is
  inverted on both sides; genuine accuracy/recognition measures stay
  ``higher_is_better``; everything the classifier cannot decide is left
  untouched and reported for a human decision.
* ``technique_recognition``: a correct-recognition/accuracy measure is
  ``higher_is_better`` on both veracities; a suspicion/perceived-
  manipulativeness rating is ``higher_is_better`` (false) / ``higher_is_worse``
  (true).  See ``Scripts/POLARITY2_REPORT.md``.
* Composite / difference / discernment DVs carry no stimulus veracity and are
  **never** touched (the PI's explicit carve-out).  A plain averaged index
  ("... composite") is NOT a difference score and is oriented normally.

Public API
----------
``expected_polarity(domain, veracity, name, notes)`` -> str | None
``normalize_sidecar(side)`` -> (side, n_changed, changes)
``normalize_sidecar_file(path)`` -> int
"""

import json
import os
import re

# Veracity-dependent domains handled by the PI rule.
BEHAVIOUR_DOMAINS = {"sharing_intention", "engagement_behavior"}
CREDIBILITY_DOMAINS = {"credibility", "belief_accuracy"}
# technique_recognition mixes two construct families that need opposite
# treatments under d' (Scripts/POLARITY2_REPORT.md):
#   * a SUSPICION / perceived-manipulativeness rating reads higher = sees the
#     material as manipulative (correct on false stimuli, a false alarm on true),
#     so true -> higher_is_worse, false -> higher_is_better;
#   * a CORRECT-RECOGNITION / accuracy measure reads higher = correctly
#     identified the tactic/veracity, so higher_is_better on BOTH.
TR_DOMAINS = {"technique_recognition"}
# An ERROR RATE inside a recognition task -- false alarms / false positives /
# misclassifications -- is undesirable on BOTH veracities (higher = worse).  It is
# NOT a suspicion rating, so it must be caught before `classify_tr_scale`'s
# suspicion default.  Note the veracity detector *strips* the token `false_alarm`
# (so it is not read as a false *stimulus*), which means such a DV usually arrives
# with a blank veracity -- hence the check runs before the blank-veracity early
# return in `expected_polarity`.  (2026-09-27, SB_SHARED_CODE_TRAPS.md #11.)
_TR_ERROR = re.compile(
    r"(false[ _]?alarms?|false[ _]?positives?|error[ _]?rate|misclassif|"
    r"false[ _]?detect|over[ _]?report)",
    re.I,
)
VERACITY_DEPENDENT_DOMAINS = BEHAVIOUR_DOMAINS | CREDIBILITY_DOMAINS | TR_DOMAINS

HIGHER = "higher_is_better"
LOWER = "higher_is_worse"

# Domains whose uniform-name prefix must be stripped before token matching, so
# that `dv_belief_accuracy_...` does not match the "accuracy" accuracy-token.
_DOMAIN_PREFIXES = (
    "sharing_intention", "engagement_behavior", "belief_accuracy", "credibility",
    "technique_recognition", "attitude_policy", "media_literacy_attitudes",
    "critical_thinking", "search_behavior", "trust", "other",
)

# Composite / difference / discernment markers -- never touched.  NOTE: the
# generic word "composite" is deliberately NOT a marker: an averaged index
# ("fake-news manipulativeness composite", "overall reliability composite")
# still carries stimulus veracity, and skipping it left aggregate scales
# un-oriented (POLARITY2_REPORT.md).  Only true difference/discernment names
# (diff/discern/minus/net/gap/d') are excluded.
_COMPOSITE = re.compile(
    r"(discern|difference|\bdiff\b|_diff|minus|\bnet\b|\bgap\b|"
    r"difference_score|d_prime|dprime|signal_detection)",
    re.I,
)

# Genuine judgement-correctness markers (accuracy at judging a claim):
# higher score = correctly identifying/judging the claim.  These stay
# `higher_is_better` on both veracities.
_ACCURACY = re.compile(
    r"(correct|proportion|percentage|percent|recognit|recogniz|recognis|"
    r"identif|detect|classif|distinguish|\bspot\b|\bhit\b|\bauc\b|\bdiscern|"
    r"inaccuracy|distrust|"
    r"\bveracity\b|"
    r"truth_?fulness|veracity[ _]?judg|veracity[ _]?classif|evaluation_?accuracy|"
    r"\baccuracy\b|\baccurate\b)",
    re.I,
)
# Suspiciousness / manipulativeness scales: higher = sees the material as
# manipulative (correct for false, incorrect for true).  POLARITY2 narrowed this
# from the original technique-name list (which fired on *stimulus descriptions*
# such as "perceived reliability of the 'discrediting' item") to the construct
# actually being measured, so credibility/reliability belief ratings are no
# longer mis-labelled as suspicion.
_SUSPICION = re.compile(
    r"(manipulativeness|manipulation[ _]rating|\bmanip[ _]rating\b|"
    r"perceived[ _]manipulat|perceived[ _]misleading|misleadingness|"
    r"perceived[ _]decept|deceptiveness|"
    r"perceived[ _](greenwash|healthwash)|greenwashing|healthwashing|"
    r"perceived[ _]problematic|problematic[- _]?(ness|rating)|"
    r"deepfake[ _]?(rating|likelihood|probability)|"
    r"perceived[ _]suspic|suspicion\b)",
    re.I,
)
# Belief / endorsement markers.
_BELIEF = re.compile(
    r"(credib|believ|reliab|trust|agree|agreement|persuas|endorse|support|"
    r"concern|worry|perceived_accuracy|accuracy|truth|attitude|intent)",
    re.I,
)
# Explicit "this recode already points the right way" markers: the DV is
# already oriented higher = correct, so it is an accuracy-equivalent and is
# left `higher_is_better`.
_RECODED = re.compile(
    r"(higher\s*=\s*(more\s*)?(correct|better|less\s+credib|less\s+belie)|"
    r"\brecoded\b|already\s+oriented|reverse[- ]?scored\s+so\s+higher|"
    r"higher\s+means\s+more\s+(accurate|correct))",
    re.I,
)

# Corrective / prosocial behaviours toward a *false* item.  Higher = the
# participant did the beneficial thing, so these stay `higher_is_better` even
# though `veracity == "false"`.  POLARITY2 tightened the match to the explicit
# corrective-action tokens (the old regex also fired on the item LABEL
# "..._polarization_reporting", a plain sharing item); confirmed in
# Scripts/POLARITY2_REPORT.md.
_PROSOCIAL = re.compile(
    r"(^|[_\-])(report|flag|block_channel|find_support|see_why|post_see|skip)([_\-]|$)",
    re.I,
)

# ---------------------------------------------------------------------------
# technique_recognition: SUSPICION rating vs CORRECT-RECOGNITION accuracy.
# See Scripts/POLARITY2_REPORT.md §2.  The default for an unmarked scale is a
# suspicion (perceived-manipulativeness) rating, because that is what the
# technique_recognition domain overwhelmingly measures once accuracy wording
# is absent.
# ---------------------------------------------------------------------------
_TR_SUSP = re.compile(
    r"(manipulativeness|manipulation[ _]rating|\bmanip[ _]?rating\b|manip\d|"
    r"misleadingness|perceived[ _]misleading|"
    r"deepfake[ _](rating|likelihood|probability)|false[ _]?alarm|"
    r"perceived[ _]manipulat|perceived[ _]problematic|problematic[- _]?ness)",
    re.I,
)
_TR_RECOG = re.compile(
    r"(proportion of correct|number of correct|"
    r"correctly[ _](judg|ident|class|detect|select|reject)|"
    r"correct[ _](tactic|targeting|on the|account|technique|identification|none|categor)|"
    r"item technique-recognition|item-level technique-recognition|"
    r"technique[ _]recognition[ _]of|"
    r"technique[ _]identification|tactic[ _]identification|identification accuracy|"
    r"recognition accuracy|proportion correct|technique[ _]score|"
    r"persuasion[-_ ]knowledge|realness|authenticity|tru_man|"
    r"identif(y|ication|ied))",
    re.I,
)


def _text(name, notes):
    return (str(name or "") + " " + str(notes or "")).strip()


def _clean(s):
    """Normalise a coded value: '', 'nan', 'none' -> blank; else lower/strip."""
    v = str(s).strip().lower()
    return "" if v in ("", "nan", "none", "nat") else v


_VERACITY_NOTE = re.compile(r"veracity\s*[:=]\s*(true|false|nan|none|\?)", re.I)


def _scrub_notes(notes):
    """Drop 'veracity=true/false' provenance tokens so they cannot be mistaken
    for an accuracy cue, then return the remaining note text."""
    return _VERACITY_NOTE.sub(" ", str(notes or ""))


def _strip_domain_prefix(name):
    s = str(name or "")
    if s.startswith("dv_"):
        s = s[3:]
    for dom in sorted(_DOMAIN_PREFIXES, key=len, reverse=True):
        if s == dom:
            return ""
        if s.startswith(dom + "_"):
            return s[len(dom) + 1:]
    return s


def is_composite(name, notes=""):
    """True for discernment / difference / composite DVs (no stimulus veracity)."""
    return bool(_COMPOSITE.search(_text(name, notes)))


def classify_credibility_scale(name, notes=""):
    """Return 'accuracy' | 'suspicion' | 'belief' | 'ambiguous' for a credibility DV.

    POLARITY2 order: explicit "already oriented higher = correct" recodes, then
    a perceived-accuracy belief composite, then a genuine suspicion
    (perceived-manipulativeness/misleadingness) rating, then the generic
    accuracy/belief split.  A combined accuracy+belief signal with no
    perceived-accuracy marker is left 'ambiguous' for a human decision; an
    unresolvable endorsement DV defaults to 'belief'.
    """
    rest = _strip_domain_prefix(name)
    txt = _text(rest, _scrub_notes(notes))
    if _RECODED.search(txt):
        return "accuracy"
    # Perceived "washing" (greenwashing / healthwashing) scales rate whether an
    # ad is deceptive; their desirable pole depends on whether the specific ad
    # actually washes, which the coded veracity does not encode.  Leave them be.
    if re.search(r"washing", txt, re.I):
        return "ambiguous"
    if re.search(r"perceived[ _]accuracy", txt, re.I):
        return "belief"
    if _SUSPICION.search(txt):
        return "suspicion"
    # `judg(ment)` without other accuracy wording is a correctness measure
    # (e.g. a deepfake/real judgment), not an endorsement rating.
    acc = bool(_ACCURACY.search(txt) or re.search(r"\bjudg", txt, re.I))
    bel = bool(_BELIEF.search(txt))
    if acc and bel:
        return "ambiguous"
    if acc:
        return "accuracy"
    return "belief"


def classify_tr_scale(name, notes="", original=""):
    """Return 'suspicion' | 'recognition' for a ``technique_recognition`` DV.

    A *correct-recognition* measure (proportion/number correct, correctly
    identified tactics, recognition accuracy) reads higher = better on both
    veracities.  A *suspicion* / perceived-manipulativeness rating reads
    higher = sees manipulation, i.e. correct on false and a false alarm on true.
    ``original`` is the sidecar ``original_variable`` (helps distinguish the
    technique-identification items from manipulativeness ratings); an unmarked
    scale defaults to suspicion.
    """
    rest = _strip_domain_prefix(name)
    txt = _text(rest, notes) + " " + str(original or "")
    if _TR_SUSP.search(txt):
        return "suspicion"
    if _TR_RECOG.search(txt):
        return "recognition"
    return "suspicion"


def expected_polarity(domain, veracity, name, notes="", original=""):
    """Target ``dv_polarity`` for a veracity-dependent DV, or ``None`` (leave).

    ``domain`` is a resolved controlled domain; ``veracity`` is 'true'/'false'
    (blank -> None).  Out-of-scope domains and composites return ``None``.
    ``original`` is the harmonized sidecar ``original_variable`` (optional).
    """
    dom = str(domain or "").strip()
    ver = _clean(veracity)
    if dom not in VERACITY_DEPENDENT_DOMAINS:
        return None
    if is_composite(name, notes):
        return None
    if dom in TR_DOMAINS and _TR_ERROR.search(str(name or "")):
        # A false-alarm / false-positive / error rate is an error on either side,
        # so it is `higher_is_worse` whatever the stimulus veracity -- including
        # when the veracity is blank because the detector stripped `false_alarm`.
        return LOWER
    if not ver:
        return None
    if dom in BEHAVIOUR_DOMAINS:
        # PI rule (blanket): true stimuli -> higher_is_better,
        # false stimuli -> higher_is_worse.  A corrective/prosocial action on a
        # false item is desirable in its own right, so it stays
        # higher_is_better (POLARITY2_REPORT.md §3).  Composite/difference DVs
        # were already excluded above.
        if ver == "false" and _PROSOCIAL.search(str(name or "")):
            return HIGHER
        if ver == "true":
            return HIGHER
        if ver == "false":
            return LOWER
        return None
    if dom in TR_DOMAINS:
        # Suspicion rating -> invert on both sides; recognition -> higher better.
        # (#4, 2026-09-29) See the module note: `notes` must ultimately move to a STRUCTURED
        # field.  Removing it outright is NOT safe: the note text carries a real construct
        # judgement for ~135 pre-existing rows (Capewell, Carey, ...), and dropping it flipped
        # their expected polarity and failed the gate.  Keep reading it until a
        # `polarity_evidence` column exists and the sidecars are re-normalised in one pass.
        scale = classify_tr_scale(name, notes, original)
        if scale == "recognition":
            return HIGHER
        return HIGHER if ver == "false" else LOWER
    # credibility / belief_accuracy: an endorsement ("belief") construct follows
    # the PI rule; a genuine suspicion rating is inverted on both sides; an
    # accuracy/ambiguous row is left as coded.  (#4) `notes` is still consulted --
    # see the TR_DOMAINS branch above for why it cannot be dropped without a
    # structured replacement and a sidecar re-normalisation pass.
    scale = classify_credibility_scale(name, notes)
    if scale == "belief":
        return HIGHER if ver == "true" else LOWER
    if scale == "suspicion":
        return HIGHER if ver == "false" else LOWER
    return None


# ===========================================================================
# Study-coded polarity precedence + reproduction arbitration (W5-POL, 2026-09-23)
#
# Owner decision: the *study-coded* direction wins IF it reproduces the paper's
# effects; otherwise the veracity-dependent rule fills in.  Study coding is read
# from `<Key> - variables.csv` (`dv_polarity` / `polarity`) and
# `<Key> - dv_direction.csv` (`direction`).  `variables.csv` is preferred by
# default (precedence a), BUT the historical `normalize_variables_file` mirror
# overwrote its `dv_polarity` with the rule for every in-scope row.  A
# `variables.csv` value that EQUALS the rule while `dv_direction.csv` disagrees
# is therefore treated as a rule mirror and the dedicated direction file wins.
# ===========================================================================

_HIB = {
    "higher_is_better", "higher_better", "higher is better", "better", "hib",
    "positive", "pos", "forward", "fwd", "1", "1.0", "+1", "up", "increase",
    "higher", "true",
}
_HIW = {
    "higher_is_worse", "lower_is_better", "higher is worse", "worse", "hiw",
    "negative", "neg", "reverse", "reversed", "rev", "-1", "-1.0", "down",
    "decrease", "lower",
}


def normalize_polarity(x):
    """Canonicalise a coded polarity/direction token.

    ``higher_is_better``/``1``/``positive``/``forward``/``hib`` ->
    ``higher_is_better``; ``higher_is_worse``/``-1``/``negative``/``reverse``/
    ``hiw`` -> ``higher_is_worse``; blank / ``0`` / ``ambiguous`` -> ``''``.
    """
    v = _clean(x)
    if v in _HIB:
        return HIGHER
    if v in _HIW:
        return LOWER
    return ""


def _first_col(df, names):
    for n in names:
        if n in df.columns:
            return n
    return None


def _as_str(x):
    """Robust cell -> str; pandas-3 missing values become '' (not 'nan'/NA)."""
    if x is None:
        return ""
    try:
        if isinstance(x, float) and x != x:
            return ""
    except Exception:
        pass
    s = str(x)
    return "" if s in ("nan", "None", "<NA>", "NaT", "NaN") else s


def read_variables_polarity(path):
    """{uniform_variable: canonical polarity} from a ``variables.csv``.

    Returns ``(values, conflicts)``; a variable coded inconsistently across
    artifacts is dropped (returned in ``conflicts``) so the rule fills it.
    """
    import pandas as pd
    if not os.path.isfile(path):
        return {}, set()
    try:
        v = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    except Exception:
        return {}, set()
    uc = _first_col(v, ("uniform_variable", "uniform_name"))
    pc = _first_col(v, ("dv_polarity", "polarity"))
    if uc is None or pc is None:
        return {}, set()
    seen, conflict = {}, set()
    for uv, raw in zip(v[uc], v[pc]):
        uv = _as_str(uv)
        if not uv.startswith("dv_"):
            continue
        p = normalize_polarity(raw)
        if not p:
            continue
        if uv in seen and seen[uv] != p:
            conflict.add(uv)
        else:
            seen[uv] = p
    for uv in conflict:
        seen.pop(uv, None)
    return seen, conflict


def read_direction_polarity(path):
    """{uniform_variable: canonical polarity} from a ``dv_direction.csv``.

    Only rows that carry an explicit desirability basis (a non-empty
    ``polarity_basis`` / ``rationale`` / ``higher_raw_means``) are treated as
    polarity labels -- the bare legacy ``direction`` field is a *scale-direction*
    field ("higher = more sharing"), which ``harmonization_coverage.py`` already
    documents as "legacy, not a polarity label".  Those rows are returned in the
    third map for reporting only and never override the rule.

    Registers the ``dv``/``uniform_variable`` name and its ``renamed_from`` alias
    (uniform names are never renamed, so a sidecar may carry the old form).
    Returns ``(polarity_map, conflicts, legacy_map)``.
    """
    import pandas as pd
    if not os.path.isfile(path):
        return {}, set(), {}
    try:
        d = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    except Exception:
        return {}, set(), {}
    uc = _first_col(d, ("uniform_variable", "dv"))
    dc = _first_col(d, ("direction", "dv_polarity"))
    if uc is None or dc is None:
        return {}, set(), {}
    alias = d["renamed_from"].tolist() if "renamed_from" in d.columns else None
    basis_cols = [c for c in ("polarity_basis", "rationale", "higher_raw_means")
                  if c in d.columns]
    seen, conflict, legacy = {}, set(), {}
    for i, (uv, raw) in enumerate(zip(d[uc], d[dc])):
        p = normalize_polarity(raw)
        if not p:
            continue
        uv = _as_str(uv)
        bearing = False
        for c in basis_cols:
            val = _as_str(d[c].iloc[i]).strip().lower()
            if val and val not in ("nan", "none"):
                bearing = True
                break
        names = [uv]
        if alias is not None:
            a = _as_str(alias[i]).strip()
            if a and a.startswith("dv_"):
                names.append(a)
        for nm in names:
            if not nm.startswith("dv_"):
                continue
            if not bearing:
                legacy[nm] = p
                continue
            if nm in seen and seen[nm] != p:
                conflict.add(nm)
            else:
                seen[nm] = p
    for nm in conflict:
        seen.pop(nm, None)
    return seen, conflict, legacy


def study_coded_polarity(uv, vmap, dmap, rule, legacy=None):
    """Resolve the study-coded polarity for ``uv`` with precedence (a) then (b).

    ``dmap`` holds explicit polarity-bearing ``dv_direction`` rows; ``legacy``
    (if given) holds the bare scale-direction rows, which are used as a
    *candidate* only -- the caller's reproduction arbitration must verify them.
    Returns ``(value, source, note)``; ``value`` is canonical or ``''``.
    """
    v = vmap.get(uv, "")
    d = dmap.get(uv, "")
    if d:
        d_source = "direction"
    else:
        d = (legacy or {}).get(uv, "")
        d_source = "direction(legacy)"
    if v and d:
        if v == d:
            return v, "variables+" + d_source, ""
        if rule is not None and v == rule:
            return d, d_source, "variables.csv equals the rule mirror"
        return v, "variables", "variables.csv preferred over conflicting direction"
    if v:
        return v, "variables", ""
    if d:
        return d, d_source, ("scale direction (legacy)" if d_source.endswith("legacy)") else "")
    return "", "", ""


# ---------------------------------------------------------------------------
# reproduction arbitration
# ---------------------------------------------------------------------------
_REPRO_CACHE = {}

_VERIFIED_CHECKS = {"sign_match", "study_reproduced"}# "checked and did NOT reproduce" -> fall back to the rule.  ``study_partial``
# means the reproduction did not confirm the paper's effects, so the study's
# coding is not trusted; only a study that could not be checked at all
# (``unverified`` / ``no_data`` / ``reported_only``) keeps its coding, marked
# unverified.
_FALLBACK_CHECKS = {"sign_mismatch", "study_not_reproduced", "study_partial",
                    "repro_row_unparsed", "no_domain_row"}


def _signed_first(text):
    """First signed number in a report cell ('0.29 vs -0.02, t=3.37' -> 0.29)."""
    if text is None:
        return None
    m = re.search(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", str(text))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def repro_facts_for(folder, key):
    """Top-level reproduction status for a study (cached); ``None`` when absent."""
    ck = (os.path.abspath(folder), key)
    if ck in _REPRO_CACHE:
        return _REPRO_CACHE[ck]
    p = os.path.join(folder, key + " - repro_facts.json")
    out = None
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as fh:
                j = json.load(fh)
            rows = []
            for r in (j.get("rows") or []):
                if isinstance(r, dict):
                    rows.append({"domain": str(r.get("domain") or ""),
                                 "reported": r.get("reported"),
                                 "recalculated": r.get("recalculated"),
                                 "verdict": str(r.get("verdict") or "")})
            domains = []
            for d in (j.get("domains") or []):
                if isinstance(d, dict):
                    domains.append({"domain": str(d.get("domain") or ""),
                                    "verdict": str(d.get("verdict") or "")})
            out = {"machine_status": str(j.get("machine_status")
                                         or j.get("status") or "").strip().lower(),
                   "has_data": bool(j.get("has_data")),
                   "rows": rows, "domains": domains}
        except Exception:
            out = None
    _REPRO_CACHE[ck] = out
    return out


def direction_repro_check(uv, folder, key, study_coded):
    """Polarity-SENSITIVE reproduction check for one DV or a cell of DVs.

    Asks whether the intervention actually moves the DV(s) in the direction the
    study coded as desirable: compute the raw (un-reversed) intervention-vs-
    control effect on the DV 0-1 column(s); a positive effect means the
    intervention raised the raw score.  ``sign_match`` when that raw sign agrees
    with the study-coded orientation, ``sign_mismatch`` when it is opposite,
    ``unverified`` when no control arm / column / effect is available.

    This is what makes the owner's "wins IF it reproduces the paper's effects"
    condition meaningful: the generic domain check (``repro_check_for_domain``)
    compares reported vs recalculated effects, which is polarity-insensitive, so
    on its own it cannot tell a correct from a flipped orientation.
    """
    try:
        import numpy as np
        import pandas as pd
        import repro_lib as RL
        import suite_common as SC
    except Exception:
        return "unverified"
    try:
        ctx = RL.get_context(folder, key)
    except Exception:
        return "unverified"
    H = ctx.get("H")
    if H is None or H.empty or "condition" not in H.columns:
        return "unverified"
    uvs = [uv] if isinstance(uv, str) else list(uv)
    cols = []
    for u in uvs:
        c = u + "_01"
        if c not in H.columns:
            c = (ctx.get("base2col") or {}).get(u)
        if c and c in H.columns:
            cols.append(c)
    if not cols:
        return "unverified"
    art_col = "artifact" if "artifact" in H.columns else None
    arts = sorted(H[art_col].dropna().astype(str).unique()) if art_col else [None]
    effects = []
    for art in arts:
        h = H if art is None else H[H[art_col].astype(str) == art]
        if not len(h):
            continue
        roles = []
        for c in h["condition"].astype(str):
            try:
                roles.append(SC.classify_condition(c, key, str(art), folder=folder)[0])
            except Exception:
                roles.append("unknown")
        roles = pd.Series(roles, index=h.index)
        ti = h[roles == "intervention"]
        tc = h[roles.isin(SC.COND_CONTROL_ROLES)]
        if len(ti) < 2 or len(tc) < 2:
            continue
        # participant-level mean across the cell's raw columns, then arm means
        pm = pd.concat([pd.to_numeric(h[c], errors="coerce") for c in cols],
                       axis=1).mean(axis=1)
        a = pm.loc[ti.index].dropna()
        b = pm.loc[tc.index].dropna()
        if len(a) < 2 or len(b) < 2:
            continue
        effects.append(float(a.mean() - b.mean()))
    if not effects:
        return "unverified"
    eff = float(np.mean(effects))
    if abs(eff) < 1e-9:
        return "unverified"
    sign_raw = 1.0 if eff > 0 else -1.0
    sign_study = 1.0 if study_coded == HIGHER else -1.0
    return "sign_match" if sign_raw == sign_study else "sign_mismatch"


def repro_check_for_domain(folder, key, domain):
    """Reproduction evidence for one DV domain.

    Uses the per-contrast ``domains`` verdicts first (richer than ``rows``),
    then the numeric ``rows``, then the study-level ``machine_status``.  Returns
    one of ``sign_match`` / ``sign_mismatch`` / ``study_reproduced`` /
    ``study_not_reproduced`` / ``study_partial`` / ``repro_row_unparsed`` /
    ``no_domain_row`` / ``no_data`` / ``unverified``.
    """
    st = repro_facts_for(folder, key)
    if st is None:
        return "unverified"
    if not st["has_data"]:
        return "no_data"
    dom = str(domain or "")
    verdicts = [d["verdict"].strip().lower() for d in st.get("domains", [])
                if d["domain"] == dom]
    if any("not reproduced" in v or "not reproducible" in v for v in verdicts):
        return "sign_mismatch"
    if any(v in ("reproduced", "reproduced in direction") for v in verdicts):
        return "sign_match"
    if any(v == "partial" for v in verdicts):
        return "study_partial"
    rows = [r for r in st["rows"] if r["domain"] == dom]
    matches = mismatches = unparsed = 0
    for r in rows:
        a = _signed_first(r["reported"])
        b = _signed_first(r["recalculated"])
        if a is None or b is None or a == 0 or b == 0:
            unparsed += 1
            continue
        if (a > 0) == (b > 0):
            matches += 1
        else:
            mismatches += 1
    if mismatches:
        return "sign_mismatch"
    if matches:
        return "sign_match"
    if unparsed:
        return "repro_row_unparsed"
    ms = st["machine_status"]
    if ms == "reproduced":
        return "study_reproduced"
    if ms in ("not reproduced", "not_reproduced"):
        return "study_not_reproduced"
    if ms == "partial":
        return "study_partial"
    if ms in ("reported_only",):
        return "reported_only"
    return "no_domain_row"


def resolve_polarity(rule, study_coded, check):
    """Arbitrate the final ``dv_polarity`` for one DV.

    Returns ``(chosen, reason)``:
    * no study coding -> the rule (or ``''`` when the rule declines);
    * study coding agrees with the rule -> the study coding;
    * conflict -> study-coded when the reproduction verifies it, the rule when
      the reproduction contradicts it, else study-coded marked ``unverified``.
    """
    if not study_coded:
        return (rule or ""), "no_study_coding"
    if rule is None:
        return study_coded, "study_coded_only"
    if study_coded == rule:
        return study_coded, "agree"
    if check in _VERIFIED_CHECKS:
        return study_coded, "study_coded_reproduces"
    if check in _FALLBACK_CHECKS:
        return rule, "rule_fallback_not_reproduced"
    return study_coded, "study_coded_unverified"


def _sidecar_rules(side):
    """Per-row resolved domain + rule + raw label columns for a sidecar frame."""
    for c in ("dv_domain", "veracity", "dv_polarity", "notes", "original_variable"):
        if c not in side.columns:
            side[c] = ""
    domains = side["dv_domain"].fillna("").astype(str)
    verac = side["veracity"].fillna("").astype(str).map(_clean)
    names = side["uniform_variable"].fillna("").astype(str)
    notes = side["notes"].fillna("").astype(str)
    origs = side["original_variable"].fillna("").astype(str)
    try:
        import pandas as pd
        import domain_restriction as DR
        res = pd.Series([DR.resolve_sidecar_domain(d, n) for d, n in zip(domains, names)],
                        index=side.index, dtype=object)
    except Exception:
        res = domains.copy()
    rule = pd.Series([expected_polarity(res.loc[i], verac.loc[i], names.loc[i],
                                        notes.loc[i], origs.loc[i])
                      for i in side.index], index=side.index, dtype=object)
    return res, verac, names, notes, origs, rule


def normalize_sidecar(side, study_coded=None, resolution=None):
    """Set ``dv_polarity`` for every in-scope sidecar DV row.

    ``side`` is the ``<Key> - harmonized_variables.csv`` DataFrame.  Returns
    ``(side, n_changed, changes)`` where ``changes`` is a list of dicts
    ``{uniform_variable, dv_domain, veracity, old, new, rule}``.

    ``study_coded`` maps a uniform variable to a canonical study-coded value and
    wins over the rule; ``resolution`` maps it to the final arbitrated value and
    wins over everything.  With neither, the behaviour is the pure rule
    (blank-preserving), so the in-memory harmonizer call is unchanged.
    """
    changes = []
    if side is None or "uniform_variable" not in getattr(side, "columns", []):
        return side, 0, changes
    res, verac, names, notes, origs, rule = _sidecar_rules(side)
    cur = side["dv_polarity"].fillna("").astype(str).map(_clean)
    new = cur.copy()
    for i in side.index:
        uv = names.loc[i]
        if not uv.startswith("dv_"):
            continue
        exp = rule.loc[i]
        chosen, reason = None, ""
        if resolution is not None and resolution.get(uv):
            chosen, reason = resolution[uv], "resolution"
        elif study_coded is not None and study_coded.get(uv):
            chosen, reason = study_coded[uv], "study_coded"
        else:
            chosen, reason = exp, ("rule" if exp is not None else "")
        if chosen is None or chosen == "":
            continue
        if cur.loc[i] != chosen:
            new.loc[i] = chosen
            changes.append({"uniform_variable": uv, "dv_domain": res.loc[i],
                            "veracity": verac.loc[i].strip().lower(),
                            "old": cur.loc[i], "new": chosen, "rule": reason})
    side["dv_polarity"] = new
    return side, len(changes), changes


def _apply_resolution_file(side, folder, key, use_repro=True):
    """Compute the study-coded map + reproduction arbitration for one sidecar.

    Returns ``(resolution, study_coded, details)`` where ``details[uv]`` carries
    the audit fields (study_coded, source, rule, check, chosen, reason).

    Conflicts are arbitrated at the ``(domain, veracity, rule, study_coded)``
    cell level so a veracity cell is never split between two orientations (an
    item-level split would corrupt the pooled discrimination).
    """
    vmap, _vc = read_variables_polarity(os.path.join(folder, key + " - variables.csv"))
    dmap, _dc, legacy = read_direction_polarity(
        os.path.join(folder, key + " - dv_direction.csv"))
    res, verac, names, notes, origs, rule = _sidecar_rules(side)
    resolution, study_coded, details = {}, {}, {}
    for i in side.index:
        uv = names.loc[i]
        if not uv.startswith("dv_"):
            continue
        r = rule.loc[i]
        sc, source, note = study_coded_polarity(uv, vmap, dmap, r, legacy)
        if sc:
            study_coded[uv] = sc
        details[uv] = {"uniform_variable": uv, "dv_domain": res.loc[i],
                       "veracity": verac.loc[i].strip().lower(),
                       "study_coded": sc, "study_coded_source": source,
                       "study_coded_note": note,
                       "legacy_direction": legacy.get(uv, ""),
                       "rule": (r or ""), "reproduction_check": "",
                       "chosen": "", "reason": ""}
    # cell-level arbitration for rows where study coding conflicts with the rule
    groups = {}
    for uv, d in details.items():
        if d["study_coded"] and d["rule"] and d["study_coded"] != d["rule"]:
            gk = (d["dv_domain"], d["veracity"], d["rule"], d["study_coded"])
            groups.setdefault(gk, []).append(uv)
    cell_check = {}
    for gk, uvs in groups.items():
        check = ""
        if use_repro:
            check = direction_repro_check(uvs, folder, key, gk[3])
            if check == "unverified":
                check = repro_check_for_domain(folder, key, gk[0])
        for u in uvs:
            cell_check[u] = check
    for uv, d in details.items():
        r = d["rule"] or None
        check = cell_check.get(uv, "")
        chosen, reason = resolve_polarity(r, d["study_coded"], check)
        d["reproduction_check"] = check
        d["chosen"] = chosen
        d["reason"] = reason
        if chosen:
            resolution[uv] = chosen
    return resolution, study_coded, details


def normalize_sidecar_file(path, use_repro=True):
    """Normalize one sidecar CSV in place.  Returns the number of rows changed.

    Applies the W5-POL precedence: study-coded polarity (``variables.csv`` then
    ``dv_direction.csv``, with the rule-mirror guard) wins over the veracity
    rule, and the reproduction arbitration decides conflicting rows.
    """
    import pandas as pd
    if not os.path.isfile(path):
        return 0
    folder = os.path.dirname(os.path.abspath(path))
    key = os.path.basename(folder)
    side = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    resolution, _sc, _d = _apply_resolution_file(side, folder, key, use_repro=use_repro)
    side, n, _ = normalize_sidecar(side, resolution=resolution)
    if n:
        side.to_csv(path, index=False, encoding="utf-8-sig")
    return n


def normalize_variables_file(var_path, sidecar_path):
    """Mirror the sidecar ``dv_polarity`` into the provenance ``variables.csv``.

    Some harmonizers rebuild ``variables.csv`` from hard-coded tables on every
    run; without this mirror the provenance file would revert to the old
    scale-level polarity even though the sidecar (and the re-analysis) are
    correct.  Returns the number of rows changed.
    """
    import pandas as pd
    if not (os.path.isfile(var_path) and os.path.isfile(sidecar_path)):
        return 0
    try:
        sc = pd.read_csv(sidecar_path, encoding="utf-8-sig", low_memory=False)
        v = pd.read_csv(var_path, encoding="utf-8-sig", low_memory=False)
    except Exception:
        return 0
    if "uniform_variable" not in sc.columns or "dv_polarity" not in sc.columns:
        return 0
    m = {_as_str(u): _clean(p) for u, p in zip(sc["uniform_variable"], sc["dv_polarity"])
         if _as_str(u).startswith("dv_")}
    uc = ("uniform_name" if "uniform_name" in v.columns
          else ("uniform_variable" if "uniform_variable" in v.columns else None))
    if uc is None or "dv_polarity" not in v.columns:
        return 0
    cur = v["dv_polarity"].fillna("").astype(str).map(_clean)
    new = cur.copy()
    n = 0
    for i in v.index:
        name = _as_str(v.loc[i, uc])
        if name.startswith("dv_") and m.get(name) and cur.loc[i] != m[name]:
            new.loc[i] = m[name]
            n += 1
    if n:
        v["dv_polarity"] = new
        v.to_csv(var_path, index=False, encoding="utf-8-sig")
    return n


def expected_from_score_types(score_types, name, notes=""):
    """Derive ('true'|'false'|'', is_composite) from coded ``score_types`` + name."""
    if isinstance(score_types, (list, tuple, set)):
        st = {str(x).strip().lower() for x in score_types}
    else:
        st = {str(score_types).strip().lower()} if score_types else set()
    if "separate_true" in st or "separate_nonmanipulative" in st:
        return "true", is_composite(name, notes)
    if "separate_false" in st or "separate_manipulative" in st:
        return "false", is_composite(name, notes)
    s = str(name or "").lower().replace("false_alarm", "").replace("falsealarm", "")
    exf = re.search(r"(^|_)(false|fake|inaccurate|misinformation|disinformation|hyperpartisan)(_|$)", s)
    ext = re.search(r"(^|_)(true|real|accurate|factual|legitimate|mainstream)(_|$)", s)
    if ext and not exf:
        return "true", is_composite(name, notes)
    if exf and not ext:
        return "false", is_composite(name, notes)
    return "", is_composite(name, notes)
