"""Analysis logic: specs, moderators, pooling wrappers, export, staleness and the background worker."""

from datetime import datetime
from flask import request
import collections
import csv
import glob
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from statslib import BAYES_MU_SD, BAYES_MU_SD_PRESETS, BAYES_TAU_PRESETS, BAYES_TAU_SD, _bayes_pool, _wls
from studio_lib import _is_continuous, _is_object_field, _norm, _schema, _summary_field_lists, _values, atomic_replace, load_extraction
from studio_state import FROZEN, S, log, paper_keys

# Per-intervention model (Scripts/PER_INTERVENTION_MODEL.md): route every coding
# read through suite_common's record_interventions / effective_record so
# intervention-level overrides are honoured.  Kept optional so the app still
# loads if suite_common (numpy/pandas/scipy) is unavailable.
try:
    if os.path.join(S.ROOT, "Scripts") not in sys.path:
        sys.path.insert(0, os.path.join(S.ROOT, "Scripts"))
    import suite_common as _SC
except Exception:  # pragma: no cover - degraded mode
    _SC = None

# The ONE spec-row selector (interface §3): every app pool must select its rows
# through ``specs_api.select_spec_rows`` so a table's ``k`` and its estimate
# always come from the same row set (audit C2/H1).  ``pandas`` is required; with
# D6 the packaged app bundles numpy/scipy/pandas.
try:
    import specs_api as SPECS_API
except Exception:  # pragma: no cover - degraded mode
    SPECS_API = None

# The stdlib-only shared coding contract (veracity/timepoint/polarity).
try:
    import contract as _CT
except Exception:  # pragma: no cover
    _CT = None


# ---------------------------------------------------------------- analysis tab
# Publication-bias + graphics tab.  Reads the tables written by
# Scripts/publication_bias.py (temp/pb_*.csv) and serves figures/ (written by
# Scripts/figures.py).  "Run" executes those scripts with the project's Python
# interpreter when the scripts are reachable from the data root.

ANALYSIS_TABLES = [
    ("publication_bias", "pb_publication_bias.csv", "Publication bias"),
    ("pooled", "pb_pooled.csv", "Pooled estimates"),
    ("intervention_type", "pb_intervention_type.csv", "Effects by intervention type"),
    ("moderator", "pb_moderator.csv", "Moderator"),
    ("domain", "pb_domain.csv", "Domain estimates"),
    ("metric_family", "pb_metric_family.csv", "Metric family"),
    ("exclusions_onoff", "pb_exclusions_onoff.csv", "Exclusions ON/OFF"),
    ("r_sensitivity", "pb_r_sensitivity.csv", "r sensitivity"),
    ("leave_one_out", "pb_leaveoneout.csv", "Leave-one-out"),
    ("spec_inventory", "pb_spec_inventory.csv", "Spec inventory"),
    ("specs_excluded", "meta_did_excluded.csv", "Excluded (quality guard)"),
    ("condition_audit", "sdt_condition_audit.csv", "SDT condition audit"),
    ("sdt_components", "pb_sdt_components.csv", "SDT components"),
]

ANALYSIS_RUN_KINDS = {
    "essential": ["publication_bias.py"],           # tables + models (fast)
    # `full` also rebuilds the participant-level SDT specs, so the
    # participant-moderator panel cannot go stale after a harmonizer change.
    "full": ["analysis_multiverse_meta.py", "publication_bias.py", "figures.py",
             "build_sdt_participant.py"],
    "bias": ["publication_bias.py"], "figures": ["figures.py"],
    "all": ["analysis_multiverse_meta.py", "publication_bias.py", "figures.py",
            "build_sdt_participant.py"],
    # The SDT multiverse suite (also writes ``temp/sdt_specs.csv``).  Kept as its
    # own kind: its canonical ``effect`` is the standardised linear twin
    # (HR − FAR)/SD, a DIFFERENT estimand from the intervention Δd′ the app's
    # pools/reports use (suite_common.build_sdt_specs, matching reanalysis.xlsx),
    # so running it deliberately switches the app's SDT spec.  See the W2-app note.
    "multiverse": ["analysis_multiverse_sdt.py"],
}

_analysis_lock = threading.Lock()

_analysis_run = {"running": False, "kind": None, "log": "", "ok": None,
                 "started": None, "finished": None, "error": "", "cached": False,
                 "fully_within": False}

# Short-lived caches for the overview's filesystem scans (figures dir listing and
# the newest input mtime): both walk hundreds of files, and the overview is the
# first thing the Analysis tab requests.
_FIGURE_CACHE = {"at": 0.0, "dir": None, "data": None}
_NEWEST_INPUT_CACHE = {"at": 0.0, "mtime": 0.0}
_FIGDIR_CACHE = {"at": 0.0, "dir": None}
_ROWCOUNT_CACHE = {}

ANALYSIS_CACHE = os.path.join(S.TEMP, ".analysis_cache.json")

def _analysis_fingerprint():
    """Hash of the inputs that drive publication_bias.py / figures.py."""
    items = []
    patterns = [os.path.join(S.EXTRACTION, "*.json"),
                os.path.join(S.DATASETS or "", "*", "* - harmonized.csv"),
                os.path.join(S.ROOT, "Scripts", "*.py")]
    for pat in patterns:
        for f in glob.glob(pat):
            try:
                st = os.stat(f)
                items.append(f"{f}:{int(st.st_mtime)}:{st.st_size}")
            except OSError:
                pass
    return hashlib.sha1("|".join(sorted(items)).encode("utf-8")).hexdigest()

def _load_analysis_cache():
    try:
        with open(ANALYSIS_CACHE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def _save_analysis_cache(d):
    try:
        os.makedirs(S.TEMP, exist_ok=True)
        with open(ANALYSIS_CACHE, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=2)
    except OSError:
        pass

def _results_path(name):
    """Resolve a results file, preferring the freshest copy across root and ``Results/``."""
    cands = [p for p in (os.path.join(S.ROOT, "Results", name), os.path.join(S.ROOT, name))
             if os.path.exists(p)]
    if cands:
        return max(cands, key=os.path.getmtime)
    return os.path.join(S.ROOT, "Results", name)

def _figures_dir():
    """Figures folder, preferring the freshest of ``Results/figures`` and ``figures``."""
    now = time.time()
    if _FIGDIR_CACHE["dir"] is not None and now - _FIGDIR_CACHE["at"] < 5:
        return _FIGDIR_CACHE["dir"]
    cands = [p for p in (os.path.join(S.ROOT, "Results", "figures"), os.path.join(S.ROOT, "figures"))
             if os.path.isdir(p)]
    if not cands:
        out = os.path.join(S.ROOT, "Results", "figures")
        _FIGDIR_CACHE.update(at=now, dir=out)
        return out

    def newest(p):
        return max((os.path.getmtime(f) for f in glob.glob(os.path.join(p, "*"))), default=0.0)

    out = max(cands, key=newest)
    _FIGDIR_CACHE.update(at=now, dir=out)
    return out

def _analysis_files():
    return (os.path.join(S.ROOT, "temp"), _results_path("Analysis_Suite_Results.xlsx"),
            _figures_dir(), os.path.join(S.ROOT, "Scripts"))

def _read_table(path, limit=None):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        cols = [c for c in (reader.fieldnames or []) if c is not None]
        rows = []
        for i, row in enumerate(reader):
            if limit is not None and i >= limit:
                break
            rows.append({k: row.get(k) for k in cols})
    return cols, rows

def _csv_rowcount(path):
    """Data-row count, counting newlines in binary chunks (the leave-one-out table
    is several MB) and cached by file identity.

    A file without a trailing newline still counts its last (unterminated) row:
    ``newlines = header + data_rows - (1 if trailing newline else 0)``.
    """
    try:
        st = os.stat(path)
    except OSError:
        return 0
    key = (int(st.st_mtime), st.st_size)
    hit = _ROWCOUNT_CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]
    try:
        n = 0
        trailing_nl = True
        last = b""
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                n += chunk.count(b"\n")
                last = chunk[-1:]
        if st.st_size == 0:
            count = 0
        else:
            trailing_nl = last == b"\n"
            count = max(0, n - 1) + (0 if trailing_nl else 1)
    except OSError:
        count = 0
    _ROWCOUNT_CACHE[path] = (key, count)
    return count

def _figure_index(force=False):
    """Figures on disk (cached briefly: the directory holds hundreds of files)."""
    now = time.time()
    figs = _figures_dir()
    if (not force and _FIGURE_CACHE["data"] is not None and _FIGURE_CACHE["dir"] == figs
            and now - _FIGURE_CACHE["at"] < 5):
        return _FIGURE_CACHE["data"]
    out = []
    if not os.path.isdir(figs):
        _FIGURE_CACHE.update(at=now, dir=figs, data=out)
        return out
    for f in sorted(glob.glob(os.path.join(figs, "*"))):
        base = os.path.basename(f)
        stem, ext = os.path.splitext(base)
        if ext.lower() not in (".png", ".pdf"):
            continue
        if stem.startswith("moderatorfit_"):
            continue  # transient moderator-fit side figures
        toks = stem.split("_")
        idx = next((i for i, t in enumerate(toks) if t in ("meta", "sdt")), None)
        if idx is None:
            kind, layer, spec = toks[0], "both", "_".join(toks[1:])
        else:
            kind = "_".join(toks[:idx]) or toks[0]
            layer = toks[idx]
            spec = "_".join(toks[idx + 1:])
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(f)).isoformat(timespec="seconds")
            size = os.path.getsize(f)
        except OSError:
            mtime, size = None, 0
        out.append({"name": base, "stem": stem, "ext": ext.lower(), "kind": kind,
                    "layer": layer, "spec": spec, "modified": mtime, "size": size,
                    "png": os.path.exists(os.path.join(figs, stem + ".png")),
                    "pdf": os.path.exists(os.path.join(figs, stem + ".pdf"))})
    _FIGURE_CACHE.update(at=now, dir=figs, data=out)
    return out

def _invalidate_figure_cache():
    _FIGURE_CACHE.update(at=0.0, dir=None, data=None)
    _FIGDIR_CACHE.update(at=0.0, dir=None)

def _spec_options(temp):
    """(layer, spec, exclusions) options with k and pooled estimate."""
    pooled = os.path.join(temp, "pb_pooled.csv")
    if not os.path.exists(pooled):
        return []
    rows = _read_table(pooled)[1]
    out = []
    for r in rows:
        try:
            k = int(float(r.get("k") or 0))
        except (TypeError, ValueError):
            k = 0
        # A derived spec present with k=0 in the publication-bias table (the SDT
        # derived specs before the pipeline materialises them) is dropped here;
        # `_derived_spec_options` reconstructs it on the fly instead.
        if r.get("spec", "") in DERIVED_SPECS and k == 0:
            continue
        out.append({"layer": r.get("layer", ""), "spec": r.get("spec", ""),
                    "exclusions": r.get("exclusions", ""), "k": k,
                    "n_studies": r.get("n_studies", ""),
                    "est": r.get("dl_est", ""), "lo": r.get("dl_lo", ""),
                    "hi": r.get("dl_hi", "")})
    return out

# ---------------------------------------------------------------- moderator analysis
# On-the-fly random-effects meta-regression over the prerequisite spec tables,
# with study-level moderators joined from the coded extractions.  Supports
# interactions between moderators.  Implemented with plain Python linear algebra
# so it also works in the packaged app (no numpy/statsmodels dependency).

# Featured (shortlist) moderators shown in the first column of the panel.
FEATURED_MODERATORS = [
    ("Age_Group", "Age"),
    ("data_available", "Primary data available"),
    ("Intervention_Type", "Intervention type"),
    ("Stimuli", "Stimulus type"),
    ("control_group_type", "Control type"),
    ("WEIRD_Western", "WESTERN sample"),
]

MAX_CAT_LEVELS = 25         # disclosure cap for HIGH-CARDINALITY / open moderators only
# (a controlled vocabulary with <= 25 levels, e.g. Intervention_Type with 14, is
#  disclosed in full; the earlier flat 8-level cap silently truncated it)

# ---------------------------------------------------------------------------
# 2026-09-19 DESIGN-FACTORS session.  `Design_Factors_Cluster` is a MULTI-LABEL
# list field: an experiment can carry several of 19 atomic design factors and
# the list combines freely (153 observed combinations across the campaign).
# PI decision: KEEP the 19 atomic labels (do NOT merge them) and treat the field
# as a MEMBERSHIP moderator -- a record carrying ["time", "design_control"]
# contributes to BOTH `time` and `design_control`.  The combination itself is
# only needed in a few places (the catalogue disclosure / a fit on the joined
# string); there the top `MAX_CAT_LEVELS` combinations are disclosed and the
# rest pooled into one `Other combination (n combinations)` level, mirroring the
# aggregation already used for `Comm_Strategies` / `Stimuli_Topic_Cluster`.
# See Scripts/DESIGN_FACTORS_BUCKETING.md for the full rationale.
DESIGN_FACTORS_FIELD = "Design_Factors_Cluster"
DESIGN_FACTORS_COMBINATION_FIELD = "Design_Factors_Combination"

# Multi-label fields: the moderator levels are the union of the list elements,
# not the list itself (see `_moderator_level_effects`).
MULTI_LABEL_MODERATORS = {DESIGN_FACTORS_FIELD}
# Combination fields: derived one-value-per-study strings rendered bucketed.
COMBINATION_MODERATORS = {DESIGN_FACTORS_COMBINATION_FIELD}

def _level_cap(name, n):
    """Number of levels to disclose, or ``None`` for all.

    A controlled vocabulary with at most ``MAX_CAT_LEVELS`` observed levels is
    disclosed in full (2026-09-21: a flat 8-level cap silently truncated
    ``Intervention_Type``, 14 levels, to 8).  Only genuinely high-cardinality /
    open fields -- ``Country`` (60 levels), the design-factor *combination* field
    (100+ strings) -- stay bounded, which also keeps the meta-regression design
    (and its O(p^3 n) collinearity guard) tractable.
    """
    if name in COMBINATION_MODERATORS:
        return MAX_CAT_LEVELS
    return MAX_CAT_LEVELS if (n or 0) > MAX_CAT_LEVELS else None

# The 19 atomic base labels in the frozen taxonomy order (Scripts/cluster_map.json
# -> taxonomies.design_factors).  Display metadata only: membership and counts
# always come from the coded data.
DESIGN_FACTOR_LABELS = {
    "inoculation": "Inoculation",
    "debunking_refutation": "Debunking / refutation",
    "fact_checking_verification": "Fact-checking / verification",
    "media_literacy": "Media literacy",
    "accuracy_prompt": "Accuracy prompt",
    "warning_label": "Warning label",
    "social_norms": "Social norms",
    "feedback": "Feedback",
    "source_credibility": "Source credibility",
    "exposure_reduction_friction": "Exposure reduction / friction",
    "veracity": "Veracity",
    "time": "Time",
    "framing": "Framing",
    "emotion_affect": "Emotion / affect",
    "identity_partisanship": "Identity / partisanship",
    "content_or_format": "Content or format",
    "participant_moderator": "Participant moderator",
    "design_control": "Design control",
    "other": "Other",
}
DESIGN_FACTOR_ORDER = list(DESIGN_FACTOR_LABELS)

_OTHER_COMBINATION = "__other_combination__"

def _design_factor_label(token):
    """Human label for one atomic design factor (falls back to the token)."""
    return DESIGN_FACTOR_LABELS.get(_norm(token)) or _human_mod_label(token)

def _design_factor_combination(values):
    """Canonical ``a+b`` key for a study's design-factor list (taxonomy order)."""
    toks = {_norm(v) for v in (values or []) if _norm(v)}
    ordered = [t for t in DESIGN_FACTOR_ORDER if t in toks]
    ordered += sorted(toks - set(ordered))
    return "+".join(ordered)

def _design_factor_combination_label(combo):
    """Human label for a canonical combination key (``a+b`` -> ``A + B``)."""
    if combo in (None, "", _OTHER_COMBINATION):
        return "Other combination"
    return " + ".join(_design_factor_label(t) for t in str(combo).split("+") if t)

def _combination_levels(mods, top_n=MAX_CAT_LEVELS):
    """Top ``top_n`` design-factor combinations + one pooled rare-combination row."""
    counts = collections.Counter()
    for m in _iter_mods(mods):
        v = _norm(m.get(DESIGN_FACTORS_COMBINATION_FIELD) or "")
        if v:
            counts[v] += 1
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    levels = [{"value": v, "label": _design_factor_combination_label(v), "n_studies": n}
              for v, n in items[:top_n]]
    rest = items[top_n:]
    if rest:
        levels.append({"value": _OTHER_COMBINATION,
                       "label": f"Other combination ({len(rest)} combinations)",
                       "n_studies": sum(n for _, n in rest)})
    return levels

def _iter_mods(mods):
    """Accept either a {key: {field: value}} mapping or an iterable of such dicts.

    The moderator catalogue builds a combined list of study- and intervention-level
    moderator dicts, while the design-factor helpers historically took the study
    mapping.  Normalising here keeps both call styles working.
    """
    return list(mods.values()) if isinstance(mods, dict) else list(mods or [])

def _design_factors_entry(mods):
    """Atomic membership entry: the 19 base labels, each scored per study."""
    counts = collections.Counter()
    for m in _iter_mods(mods):
        for v in _values(m.get(DESIGN_FACTORS_FIELD)):
            if v not in (None, ""):
                counts[_norm(v)] += 1
    order = [t for t in DESIGN_FACTOR_ORDER if t in counts]
    order += [v for v in counts if v not in DESIGN_FACTOR_ORDER]
    levels = [{"value": v, "label": _design_factor_label(v), "n_studies": counts[v]}
              for v in order]
    return {"name": DESIGN_FACTORS_FIELD,
            "label": _MOD_META[DESIGN_FACTORS_FIELD][0],
            "group": _MOD_META[DESIGN_FACTORS_FIELD][1],
            "kind": "categorical", "levels": levels,
            "multi_label": True,
            "combination_field": DESIGN_FACTORS_COMBINATION_FIELD,
            "combination_levels": _combination_levels(mods)}

def _combination_entry(mods):
    """Companion moderator for the joined design-factor combination string."""
    return {"name": DESIGN_FACTORS_COMBINATION_FIELD,
            "label": _MOD_META[DESIGN_FACTORS_COMBINATION_FIELD][0],
            "group": _MOD_META[DESIGN_FACTORS_COMBINATION_FIELD][1],
            "kind": "categorical", "levels": _combination_levels(mods),
            "combination": True}

# 2026-09-19: `belief_accuracy` merged into `credibility` (PI decision; see
# Scripts/DV_DOMAIN_MERGE_LOG.md).
SUSCEPTIBILITY = {"credibility", "sharing_intention", "technique_recognition"}

# ---------------------------------------------------------------------------
# Moderator fields.  Free-text coding fields are not usable as moderators (they
# have hundreds of ad-hoc levels), so they are replaced by finite taxonomy
# clusters.  The topic / clustering clusters are persisted by
# `Scripts/apply_cluster.py`; practice/media have no persisted cluster field and
# are clustered on the fly here (the raw extractions are never modified).
_RAW_MODERATOR_EXCLUDE = {"Stimuli_Topic", "Intervention_Topic",
                          "Practice_Strategy", "Media_Support",
                          # 2026-09-19 analysis-tab fix: `n_arms` is bookkeeping,
                          # not a moderator; Publication_Type/Category are merged
                          # into the single `Publication` moderator below.
                          "n_arms", "Publication_Type", "Publication_Category"}

# Auto-discovery: any categorical value with fewer than this many observed levels
# is offered as a moderator (free-text / identifier / numeric fields excepted).
_MOD_MAX_LEVELS = 100

# Fields that are identifiers, open-ended text or internal bookkeeping: never moderators.
_MOD_FREETEXT_RE = re.compile(
    r"^(?:_dvs|Study_ID|Parent_Study_ID|DOI|Title|Authors?$|Author.*|Affiliation.*|"
    r"Study_Description|Rationale_Notes|data_url|preregistration_url|.*_url$|.*_doi$|"
    r".*_notes?$)$", re.I)

# Label / group overrides for fields that are not paper-coded (clusters, data_available).
# ``data_available`` is promoted to the Featured column because it is the
# data-availability contrast the campaign reports on directly.
_MOD_META = {
    # 2026-09-19 analysis-tab fix: data availability is an open-science/design
    # moderator, not a Sample one.
    "data_available": ("Primary data available (vs paper only)", "Open Science & Design"),
    "Intervention_Topic_Cluster": ("Intervention topic (clustered)", "Intervention"),
    "Stimuli_Topic_Cluster": ("Stimulus topic (clustered)", "Stimuli"),
    "Design_Factors_Cluster": ("Design factors (clustered)", "Open Science & Design"),
    "Design_Factors_Combination": ("Design factor combination (clustered)", "Open Science & Design"),
    "Clustering_Cluster": ("Clustering (clustered)", "Open Science & Design"),
    "Practice_Strategy_Cluster": ("Practice strategy (clustered)", "Instruction & Learning"),
    "Media_Support_Cluster": ("Media support (clustered)", "Instruction & Learning"),
    # Publication_Type + Publication_Category are merged into this one moderator.
    "Publication": ("Publication", "Metadata & Identification"),
    "Gender_Ratio": ("Gender ratio (male vs non-male)", "Sample"),
    "Age_Mean": ("Age (mean, years)", "Sample"),
}

# The analysis tab's Sample group is restricted to these fields.  Any other
# field whose coding-schema group is ``Sample`` is moved to ``Sample detail`` at
# catalogue-build time (see ``_group_override``), so Sample never grows beyond
# the agreed shortlist.
_SAMPLE_GROUP = "Sample"
_SAMPLE_DETAIL_GROUP = "Sample detail"
_SAMPLE_GROUP_WHITELIST = {
    "N_Recruited", "N_Analyzed", "Age_Mean", "Age_Group", "Country",
    "International_Sample", "WEIRD_Western", "WEIRD_Educated",
    "WEIRD_Industrialized", "WEIRD_Rich", "WEIRD_Democratic", "Setting",
    "Gender_Ratio",
}

# Group overrides applied on top of the coding schema group.
_GROUP_OVERRIDES = {
    "data_available": "Open Science & Design",
    "Attention_Cues": "Intervention",
}

def _group_override(name, group):
    """Final analysis-tab group for a field (whitelist + explicit overrides)."""
    if name in _GROUP_OVERRIDES:
        return _GROUP_OVERRIDES[name]
    if group == _SAMPLE_GROUP and name not in _SAMPLE_GROUP_WHITELIST:
        return _SAMPLE_DETAIL_GROUP
    return group

# Numeric moderators that are computed from a coded field rather than read directly.
_COMPUTED_CONTINUOUS = {"Gender_Ratio"}

# Contrast-level moderators: read per effect from the spec rows, not per study,
# so they can vary within a study (DV type, stimulus veracity, contrast type).
_CONTRAST_MODERATORS = [
    ("dv_domain", "DV type", "DV metrics"),
    ("contrast_type", "Contrast type", "DV metrics"),
]

_CONTRAST_NAMES = {n for n, _, _ in _CONTRAST_MODERATORS}

_CONTRAST_OF = {n: g for n, _, g in _CONTRAST_MODERATORS}

_PRACTICE_CLUSTERS = {
    "recall tests": "retrieval_practice",
    "daily content question": "retrieval_practice",
    "knowledge test construction": "retrieval_practice",
    "easy-to-hard sequencing": "spacing_sequencing",
    "between-session assignments applied to daily sns use": "spacing_sequencing",
    "review of prior session and assignments": "spacing_sequencing",
    "reason listing": "elaboration_reflection",
    "counterargument generation": "elaboration_reflection",
    "written counter-argument generation (ce only)": "elaboration_reflection",
    "consider the opposite": "elaboration_reflection",
    "refutation exercises": "elaboration_reflection",
    "written reflection": "elaboration_reflection",
    "self-explanation": "elaboration_reflection",
    "elaborative self-explanation": "elaboration_reflection",
    "analysis of media texts": "elaboration_reflection",
    "headline reframing": "elaboration_reflection",
    "feedback-based classification practice": "classification_sorting",
    "compare-and-contrast": "classification_sorting",
    "sorting/grouping exercise": "classification_sorting",
    "discrimination practice": "classification_sorting",
    "categorization practice": "classification_sorting",
    "technique identification tasks": "classification_sorting",
    "selecting misleading graphs to support false headlines": "classification_sorting",
    "scaffolded practice": "guided_application",
    "guided practice": "guided_application",
    "guided reading practice": "guided_application",
    "guided practice assessing news credibility": "guided_application",
    "guided web searching": "guided_application",
    "reverse image search practice": "guided_application",
    "practice using fact-checking tools": "guided_application",
    "applying a checklist": "guided_application",
    "checklist application": "guided_application",
    "revision/practical exercises": "guided_application",
    "interactive quiz with elaborated feedback": "feedback_practice",
    "role play with feedback": "feedback_practice",
    "feedback on preparatory actions": "feedback_practice",
    "peer feedback and poster revision": "feedback_practice",
    "think-pair-share": "collaboration_production",
    "class presentation and peer feedback": "collaboration_production",
    "media production": "collaboration_production",
    "refutational poster development": "collaboration_production",
    "generate own tweets": "collaboration_production",
    "hands-on digital media production / game design": "collaboration_production",
    "role play": "collaboration_production",
    "role-play": "collaboration_production",
    "card duel": "collaboration_production",
}

_PRACTICE_ORDER = ["retrieval_practice", "spacing_sequencing", "elaboration_reflection",
                   "classification_sorting", "guided_application", "feedback_practice",
                   "collaboration_production", "other"]

_MEDIA_CLUSTERS = {
    "visual aids": "visual_aids", "news headline images": "visual_aids",
    "example graphs": "visual_aids", "graphic models": "visual_aids",
    "photographs": "visual_aids", "tweets/screenshots": "visual_aids",
    "facebook-style post": "visual_aids", "facebook-style news posts": "visual_aids",
    "poster": "visual_aids", "physical cards": "visual_aids",
    "model teaching": "modeling",
    "video": "video", "video clips": "video", "video animations": "video",
    "documentary films": "video",
    "text": "text", "written vignette": "text",
    "verified-information articles": "text", "teacher manual": "text",
    "gamified interactive module": "interactive_digital",
    "gamified challenges": "interactive_digital", "vr simulations": "interactive_digital",
    "website": "interactive_digital", "digital media platform": "interactive_digital",
    "internet/computer workstations": "interactive_digital",
    "audio": "audio", "voice-over narration": "audio",
}

_MEDIA_ORDER = ["interactive_digital", "video", "audio", "visual_aids", "modeling", "text", "other"]

_CLUSTER_LABELS = {
    # topic clusters (Scripts/cluster_map.json taxonomy)
    "vaccines": "Vaccines", "covid19": "COVID-19", "health_other": "Other health",
    "politics_elections": "Politics & elections", "climate_environment": "Climate & environment",
    "science_gmo": "Science & GMO", "immigration_race": "Immigration & race",
    "conflict_war": "Conflict & war", "crime_justice": "Crime & justice",
    "media_literacy_skills": "Media-literacy skills", "technology_ai": "Technology & AI",
    "conspiracies": "Conspiracies", "general_misinformation": "General misinformation",
    "climate_change": "Climate change", "science": "Science", "politics": "Politics",
    # clustering taxonomy
    "none": "None", "by_participant": "By participant", "by_stimulus": "By stimulus",
    "by_participant_and_stimulus": "Participant & stimulus", "by_group": "By group",
    "by_participant_and_group": "Participant & group", "repeated_measures": "Repeated measures",
    # practice clusters
    "retrieval_practice": "Retrieval practice", "spacing_sequencing": "Spacing / sequencing",
    "elaboration_reflection": "Elaboration / reflection",
    "classification_sorting": "Classification / sorting", "guided_application": "Guided application",
    "feedback_practice": "Feedback practice", "collaboration_production": "Collaboration / production",
    # media clusters
    "visual_aids": "Visual aids", "modeling": "Model teaching", "video": "Video",
    "text": "Text", "interactive_digital": "Interactive / digital", "audio": "Audio",
    # contrast levels
    "true": "True", "false": "False", "manipulative": "Manipulative",
    "nonmanipulative": "Non-manipulative", "mixed": "Mixed",
    "other": "Other",
}

# 2026-09-19 analysis-tab fix: Publication_Type + Publication_Category are
# merged into ONE `Publication` moderator.  This is the agreed controlled
# vocabulary (mirrored in Scripts/build_coding_schema.py: PUBLICATION_VOCAB and
# published in Scripts/ANALYSIS_TAB_FIX.md).  The data layer sets these values;
# the derivation below is only the interim fallback for records that have not
# been recoded yet.
_PUBLICATION_VOCAB = [
    ("paper_peer_reviewed", "Paper (peer-reviewed)"),
    ("paper_unreviewed", "Paper (unreviewed)"),
    ("thesis", "Thesis"),
    ("dissertation", "Dissertation"),
    ("preprint", "Preprint"),
    ("working_paper", "Working paper"),
    ("conference_paper", "Conference paper"),
    ("unpublished_manuscript", "Unpublished manuscript"),
    ("report", "Report"),
]
_PUBLICATION_LABELS = {v: label for v, label in _PUBLICATION_VOCAB}

# Raw value (normalised) -> canonical Publication level.  `paper` cannot be
# split from the raw value alone, so it maps to unreviewed; `journal_article`
# (and anything naming a journal / peer review) maps to peer-reviewed.
_PUBLICATION_SYNONYMS = {
    "journal_article": "paper_peer_reviewed",
    "journal": "paper_peer_reviewed",
    "peer_reviewed": "paper_peer_reviewed",
    "peerreviewed": "paper_peer_reviewed",
    "paper": "paper_unreviewed",
    "paper_unreviewed": "paper_unreviewed",
    "conference_paper": "conference_paper",
    "conference": "conference_paper",
    "proceedings": "conference_paper",
    "thesis": "thesis",
    "phd_thesis": "thesis",
    "masters_thesis": "thesis",
    "dissertation": "dissertation",
    "phd_dissertation": "dissertation",
    "preprint": "preprint",
    "working_paper": "working_paper",
    "unpublished_manuscript": "unpublished_manuscript",
    "manuscript": "unpublished_manuscript",
    "unpublished": "unpublished_manuscript",
    "report": "report",
    "technical_report": "report",
}

def _canonical_publication(value):
    """Canonical Publication level for one raw token, or ''."""
    s = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not s:
        return ""
    if s in _PUBLICATION_LABELS:
        return s
    if s in _PUBLICATION_SYNONYMS:
        return _PUBLICATION_SYNONYMS[s]
    if "journal" in s or "peer" in s:
        return "paper_peer_reviewed"
    return ""

def _derive_publication(rec):
    """Merge Publication_Type + Publication_Category into one level.

    Prefers an explicit canonical ``Publication`` value (set by the data layer),
    then Publication_Type, then Publication_Category.
    """
    for field in ("Publication", "Publication_Type", "Publication_Category"):
        lvl = _canonical_publication(rec.get(field))
        if lvl:
            return lvl
    return ""

# Fields whose option labels live in the schema-level controlled vocabulary
# (dict of numeric code -> token) rather than in the field's own option list.
_VOCAB_FOR_FIELD = {
    "contrast_type": "contrast_types",
    "posttest_type": "posttest_types",
}

# Display-only level names for values that are observed but not in the schema
# option list (so the analysis tab never shows a bare number).
_EXTRA_LEVEL_LABELS = {
    "Social_Context": {"4": "Other"},
}

def _primary_cluster(values, mapping, order):
    """Primary (first-in-priority) cluster for a list-valued raw field."""
    found = set()
    for v in values or []:
        s = str(v or "").strip().lower()
        if s:
            found.add(mapping.get(s, "other"))
    for lvl in order:
        if lvl in found:
            return lvl
    return ""

def _gender_male_ratio(obj):
    """Share of male participants from a coded ``Gender_Ratio`` object (0-1).

    Handles counts or percentages and the male synonyms (male/men/...); the
    denominator is the whole reported sample, so the value contrasts male vs
    non-male.
    """
    if not isinstance(obj, dict) or not obj:
        return None
    total = male = 0.0
    for k, v in obj.items():
        try:
            x = float(v)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(x) or x < 0:
            continue
        key = str(k).strip().lower()
        total += x
        if key in ("male", "men", "man", "m", "boy", "boys") or (
                "male" in key and "female" not in key):
            male += x
    if total <= 0:
        return None
    return max(0.0, min(1.0, male / total))

_MOD_CACHE = {"specs_at": 0.0, "mods_at": 0.0, "specs": None, "mods": None}

# Per-intervention effective moderators (PER_INTERVENTION_MODEL.md).
_IV_MOD_CACHE = {"at": 0.0, "data": None}

_CAT_CACHE = {"at": 0.0, "data": None}
_SPEC_CACHE_TTL = 300

_MOD_CACHE_TTL = 300

_CAT_CACHE_TTL = 300

_MODS_LOCK = threading.Lock()

_CAT_LOCK = threading.Lock()

_INPUTS_FP_CACHE = {"at": 0.0, "fp": None}

_PREWARM = {"started": False}


def _invalidate_analysis_caches():
    """Reset every in-process analysis cache after a spec/table rebuild (M4).

    A background rebuild rewrites the spec tables and the ``pb_*`` tables; the
    short-lived caches below would otherwise keep serving the pre-rebuild row
    set for up to five minutes, so one screen could show numbers from two
    different builds.
    """
    _MOD_CACHE.update(specs_at=0.0, mods_at=0.0, specs=None, mods=None)
    _IV_MOD_CACHE.update(at=0.0, data=None)
    _CAT_CACHE.update(at=0.0, data=None)
    _PART_CACHE.update(at=0.0, data=None)
    _FW_KEYS_CACHE.update(at=0.0, records=None, studies=None)
    _INPUTS_FP_CACHE.update(at=0.0, fp=None)
    _NEWEST_INPUT_CACHE.update(at=0.0, mtime=0.0)
    _invalidate_figure_cache()
    _ROWCOUNT_CACHE.clear()

# Bump when the derived moderators (Gender_Ratio, Publication, ...) change, so
# a disk cache written by an older derivation is not reused.
_MOD_DERIVATION_VERSION = "2026-09-26-union-multilabel"

# object/list containers and the computed gender field are not stored raw
_MOD_OBJECT_SKIP = {"Covariates", "factor_levels", "cell_descriptives",
                    "conversion_inputs", "Gender_Ratio", "Gender_Ratio_01"}

def _inputs_fingerprint():
    """Hash of the Extraction/Datasets inputs that drive the moderator scan."""
    now = time.time()
    if _INPUTS_FP_CACHE["fp"] is not None and now - _INPUTS_FP_CACHE["at"] < 20:
        return _INPUTS_FP_CACHE["fp"]
    items = []
    for pat in (os.path.join(S.EXTRACTION, "*.json"),
                os.path.join(S.DATASETS or "", "*", "* - harmonized.csv"),
                os.path.join(S.DATASETS or "", "*", "* - harmonized_variables.csv")):
        for f in glob.glob(pat):
            try:
                st = os.stat(f)
                items.append(f"{f}:{int(st.st_mtime)}:{st.st_size}")
            except OSError:
                pass
    fp = hashlib.sha1("|".join(sorted(items)).encode("utf-8")).hexdigest()
    _INPUTS_FP_CACHE.update(at=now, fp=fp)
    return fp

def _prewarm_analysis():
    """Warm the moderator scan/catalogue and spec tables in the background.

    Called on server start (and on the first overview) so the Analysis tab's
    first paint does not pay for the cold scan.
    """
    if _PREWARM["started"]:
        return
    _PREWARM["started"] = True

    def work():
        for fn in (_study_moderators, _moderator_catalog, _spec_rows, _figure_index):
            try:
                fn()
            except Exception:
                pass
        try:
            _spec_options(_analysis_files()[0])
        except Exception:
            pass

    threading.Thread(target=work, daemon=True).start()

def _study_has_data(key):
    return os.path.isfile(os.path.join(S.DATASETS or "", key, key + " - harmonized.csv"))

def _common_cat_fields():
    return _summary_field_lists()[0]

def _schema_field_map():
    """Every schema field (common + module-routed) by name; module fields grouped."""
    out = {}
    s = _schema()
    for f in (s.get("common_fields") or []):
        if f.get("name"):
            out[f["name"]] = f
    for m, mod in (s.get("modules") or {}).items():
        for f in (mod.get("fields") or []):
            n = f.get("name")
            if not n:
                continue
            g = dict(f)
            if not g.get("group"):
                g["group"] = f"Intervention module ({m})"
            out.setdefault(n, g)
    return out

def _module_field_map():
    """{field_name: module_key} for module-only (intervention-specific) fields."""
    common = {f.get("name") for f in (_schema().get("common_fields") or [])}
    out = {}
    for m, mod in (_schema().get("modules") or {}).items():
        for f in (mod.get("fields") or []):
            n = f.get("name")
            if n and n not in common:
                out.setdefault(n, m)
    return out

def _declared_vocab_size(f):
    if not f:
        return 0
    opts = f.get("options") or []
    if opts:
        return len(opts)
    v = f.get("vocab")
    if isinstance(v, dict):
        return len(v)
    if isinstance(v, (list, tuple)):
        return len(v)
    return 0

def _raw_moderator_fields(recs):
    """Merge the raw moderatable fields of one or more experiment records.

    Free-text/identifier/object fields are skipped; ``Interventions`` (a
    non-schema object) is skipped too so it never becomes a moderator level.
    Returns ``(merged, gender_raw)``.
    """
    merged = {}
    gender_raw = None
    for r in recs or []:
        if not isinstance(r, dict):
            continue
        for name, val in r.items():
            if name in ("DV_Metrics", "Interventions") or name.startswith("_"):
                continue
            # Gender_Ratio is a container: keep the raw object for the
            # computed male ratio, but never store it as a moderator.
            if name == "Gender_Ratio":
                if gender_raw is None and val not in (None, "", [], {}):
                    gender_raw = val
                continue
            if _MOD_FREETEXT_RE.match(name) or name in _MOD_OBJECT_SKIP:
                continue
            if isinstance(val, str) and len(val) > 200:
                continue
            if val in (None, "", [], {}):
                continue
            if name not in merged:
                merged[name] = val
            elif isinstance(merged[name], list) or isinstance(val, list):
                # A multi-experiment study must keep every experiment's labels:
                # the summary counts a study under each label any experiment has,
                # so the study-level moderator (used by the analysis tables) has
                # to be the union, not just the first experiment's value.
                prev = merged[name] if isinstance(merged[name], list) else [merged[name]]
                for x in (val if isinstance(val, list) else [val]):
                    if x not in prev:
                        prev.append(x)
                merged[name] = prev
    return merged, gender_raw


def _finalize_moderators(base, merged, gender_raw):
    """Add the merged raw values and the derived moderators to ``base``."""
    base = dict(base)
    base.update(merged)
    # Multi-label design factors also get a joined key so the tab can
    # disclose the combination (bucketed) wherever it needs it.
    base[DESIGN_FACTORS_COMBINATION_FIELD] = _design_factor_combination(
        merged.get(DESIGN_FACTORS_FIELD))
    base["Practice_Strategy_Cluster"] = _primary_cluster(
        _values(merged.get("Practice_Strategy")), _PRACTICE_CLUSTERS, _PRACTICE_ORDER)
    base["Media_Support_Cluster"] = _primary_cluster(
        _values(merged.get("Media_Support")), _MEDIA_CLUSTERS, _MEDIA_ORDER)
    base["Gender_Ratio"] = _gender_male_ratio(gender_raw)
    # Interim merged Publication (data layer sets canonical values).
    if not base.get("Publication"):
        pub = _derive_publication(merged)
        if pub:
            base["Publication"] = pub
    return base


def _study_moderators():
    """{study_key: {field: value}} for every moderatable coded field (+ data_available).

    Free-text/identifier fields are skipped and the practice/media/gender values
    are derived here (raw extractions are never modified).  The scan is cached in
    memory and on disk (keyed by the input fingerprint) so startup is fast.
    """
    now = time.time()
    if _MOD_CACHE["mods"] is not None and now - _MOD_CACHE["mods_at"] < _MOD_CACHE_TTL:
        return _MOD_CACHE["mods"]
    with _MODS_LOCK:
        now = time.time()
        if _MOD_CACHE["mods"] is not None and now - _MOD_CACHE["mods_at"] < _MOD_CACHE_TTL:
            return _MOD_CACHE["mods"]
        fp = _inputs_fingerprint()
        cache_file = os.path.join(S.ROOT, "temp", "study_moderators_cache.json")
        try:
            with open(cache_file, encoding="utf-8") as fh:
                blob = json.load(fh)
            if (blob.get("fingerprint") == fp
                    and blob.get("version") == _MOD_DERIVATION_VERSION
                    and isinstance(blob.get("mods"), dict)):
                _MOD_CACHE.update(mods_at=now, mods=blob["mods"])
                return blob["mods"]
        except Exception:
            pass
        out = {}
        for key in paper_keys():
            recs = load_extraction(key)
            base = {"data_available": 1 if _study_has_data(key) else 0}
            # A study can hold several experiment records; a module field may be
            # coded on any of them.  Scalar fields keep the first non-empty value,
            # list fields (e.g. Intervention_Type, Design_Factors) keep the UNION
            # so a study is counted under every label any of its experiments has
            # (which is what the Summary tab reports).
            merged, gender_raw = _raw_moderator_fields(recs)
            out[key] = _finalize_moderators(base, merged, gender_raw)
        _MOD_CACHE.update(mods_at=now, mods=out)
        try:
            tmp = cache_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"fingerprint": fp, "version": _MOD_DERIVATION_VERSION,
                           "mods": out}, fh)
            atomic_replace(tmp, cache_file)
        except OSError:
            pass
        return out


def _intervention_moderators():
    """{(study_key, experiment_id, Intervention_ID): {field: value}}.

    Per-intervention effective moderators (PER_INTERVENTION_MODEL.md): the study
    merged moderators with the *effective* coding of each intervention overlaid,
    so an override on one arm is honoured.  Empty when no record carries
    ``Interventions`` (backward compatible).
    """
    now = time.time()
    if _IV_MOD_CACHE["data"] is not None and now - _IV_MOD_CACHE["at"] < _MOD_CACHE_TTL:
        return _IV_MOD_CACHE["data"]
    if _SC is None:
        return {}
    study_mods = _study_moderators()
    out = {}
    for key in paper_keys():
        for r in load_extraction(key) or []:
            if not isinstance(r, dict) or not _SC.record_has_interventions(r):
                continue
            exp = str(r.get("Study_ID") or "")
            for iv in _SC.record_interventions(r):
                iid = str(iv.get("Intervention_ID") or "").strip()
                eff = _SC.effective_record(r, iv)
                merged, gender_raw = _raw_moderator_fields([eff])
                base = dict(study_mods.get(key, {}))
                base["data_available"] = 1 if _study_has_data(key) else 0
                out[(key, exp, iid)] = _finalize_moderators(base, merged, gender_raw)
    _IV_MOD_CACHE.update(at=now, data=out)
    return out


def _mods_for_row(row):
    """Effective moderators for one spec row (per-intervention override aware).

    A row bound to an intervention (``intervention_id`` set) resolves against that
    intervention's effective coding; otherwise the study-level merged moderators
    are used exactly as before.
    """
    key = str(row.get("study_key") or "")
    iid = str(row.get("intervention_id") or "").strip()
    if iid:
        m = _intervention_moderators().get(
            (key, str(row.get("experiment_id") or ""), iid))
        if m:
            return m
    return _study_moderators().get(key, {})

def _filter_args(req):
    name = (req.args.get("filter_name") or "").strip()
    value = (req.args.get("filter_value") or "").strip()
    return (name, value) if name and value else (None, None)

def _apply_study_filter(rows, name, value):
    """Restrict rows to studies whose coded ``name`` includes ``value``.

    Lets the overall analysis be run on a subgroup (e.g. only lateral-reading
    studies) without touching the prerequisite tables.
    """
    if not name or value is None or value == "":
        return rows
    want = _norm(value)
    out = []
    for r in rows:
        vals = {_norm(v) for v in _values(_mods_for_row(r).get(name))}
        if want in vals:
            out.append(r)
    return out

def _moderator_catalog():
    """Featured + all categorical coding fields (grouped by coding-tab category).

    A lock keeps the server-start prewarm and a concurrent request from building
    the catalogue twice.
    """
    now = time.time()
    if _CAT_CACHE["data"] is not None and now - _CAT_CACHE["at"] < _CAT_CACHE_TTL:
        return _CAT_CACHE["data"]
    with _CAT_LOCK:
        now = time.time()
        if _CAT_CACHE["data"] is not None and now - _CAT_CACHE["at"] < _CAT_CACHE_TTL:
            return _CAT_CACHE["data"]
        data = _build_moderator_catalog()
        _CAT_CACHE.update(at=now, data=data)
        return data

def _build_moderator_catalog():
    mods = _study_moderators()
    # Per-intervention effective values (empty when no record carries
    # ``Interventions``) so an override on one arm appears as a level too.
    ivmods = _intervention_moderators()
    all_mods = list(mods.values()) + list(ivmods.values())
    schema_fields = _schema_field_map()
    modules_of = _module_field_map()

    def levels_of(name):
        counts = collections.Counter()
        for m in all_mods:
            for v in _values(m.get(name)):
                if v not in (None, ""):
                    counts[_norm(v)] += 1
        return counts

    def label_group(name, f):
        meta = _MOD_META.get(name)
        label = meta[0] if meta else ((f.get("label") if f else None) or _human_mod_label(name))
        group = meta[1] if meta else ((f.get("group") if f else None) or "Other")
        return label, _group_override(name, group)

    def with_module(e, name):
        mod = modules_of.get(name)
        if mod:
            e["module"] = mod
            e["module_label"] = ((_schema().get("modules") or {}).get(mod) or {}).get("label") or mod
        return e

    def entry(name):
        """Auto-discovered moderator entry, or None when the field does not qualify."""
        if name in _RAW_MODERATOR_EXCLUDE or name.startswith("_") \
                or _MOD_FREETEXT_RE.match(name):
            return None
        f = schema_fields.get(name)
        # data_available is a coded flag (derived, numeric): keep it binary
        if name == "data_available":
            counts = levels_of(name)
            if len(counts) < 2:
                return None
            label, group = label_group(name, f)
            return with_module({"name": name, "label": label, "group": group, "kind": "binary",
                                "levels": [{"value": v, "label": _mod_label(name, v), "n_studies": n}
                                           for v, n in counts.most_common(60)]}, name)
        # continuous / scale moderator (numeric field, or a computed scalar)
        if name in _COMPUTED_CONTINUOUS or (
                f is not None and not f.get("derived") and _is_continuous(f)):
            xs = []
            for m in all_mods:
                for v in _values(m.get(name)):
                    try:
                        xs.append(float(v))
                    except (TypeError, ValueError):
                        pass
            if len(xs) < 3 or len(set(xs)) < 2:
                return None
            label, group = label_group(name, f)
            return with_module({"name": name, "label": label, "group": group, "kind": "continuous",
                                "levels": [], "n_studies": len(xs)}, name)
        # categorical / binary
        if f is not None and (f.get("derived") or _is_object_field(f)):
            return None
        if f is not None and str(f.get("editor") or "").lower() in ("text", "textarea", "richtext", "number"):
            return None
        counts = levels_of(name)
        if len(counts) < 2 or len(counts) > _MOD_MAX_LEVELS:
            return None
        label, group = label_group(name, f)
        levels = [{"value": v, "label": _mod_label(name, v), "n_studies": n}
                  for v, n in counts.most_common(60)]
        kind = "binary" if len(counts) <= 2 else "categorical"
        return with_module({"name": name, "label": label, "group": group, "kind": kind,
                            "levels": levels}, name)

    def contrast_entry(name, label, group):
        counts = collections.Counter()
        studies = {}
        for r in _spec_rows():
            v = _norm(r.get(name))
            if v == "":
                continue
            counts[v] += 1
            studies.setdefault(v, set()).add(r["study_key"])
        levels = [{"value": v, "label": _mod_label(name, v),
                   "n_studies": len(studies.get(v, ()))}
                  for v, _ in counts.most_common(60)]
        kind = "binary" if len(levels) <= 2 else "categorical"
        return {"name": name, "label": label, "group": group, "kind": kind,
                "levels": levels, "contrast": True}

    # candidate order: common fields, module fields, then extraction-only fields
    s = _schema()
    ordered, seen = [], set()
    for f in (s.get("common_fields") or []):
        n = f.get("name")
        if n and n not in seen:
            seen.add(n)
            ordered.append(n)
    for _m, mod in (s.get("modules") or {}).items():
        for f in (mod.get("fields") or []):
            n = f.get("name")
            if n and n not in seen:
                seen.add(n)
                ordered.append(n)
    extra = set()
    for m in all_mods:
        extra.update(m.keys())
    for n in sorted(extra):
        if n not in seen:
            seen.add(n)
            ordered.append(n)

    core = []
    mod_entries = {}
    for name in ordered:
        if name == DESIGN_FACTORS_FIELD:
            e = _design_factors_entry(all_mods)
        elif name == DESIGN_FACTORS_COMBINATION_FIELD:
            e = _combination_entry(all_mods)
        else:
            e = entry(name)
        if not e:
            continue
        if e.get("module"):
            mod_entries.setdefault(e["module"], []).append(e)
        else:
            core.append(e)
    for name, label, group in _CONTRAST_MODERATORS:
        core.append(contrast_entry(name, label, group))
    # Participant-level covariates from the primary datasets (SDT layer only).
    core.extend(_participant_covariates())

    by_name = {m["name"]: m for m in core}
    featured = []
    for name, label in FEATURED_MODERATORS:
        m = by_name.get(name)
        if m:
            featured.append(dict(m, label=label))

    groups, order = {}, []
    schema_groups = [g for g in (_schema().get("groups") or []) if isinstance(g, str)]
    for g in schema_groups:
        if g in {m["group"] for m in core} and g not in order:
            order.append(g)
    for m in core:
        if m["group"] not in order:
            order.append(m["group"])
    for m in core:
        groups.setdefault(m["group"], []).append(m)

    # Intervention-module fields go into ONE collapsible section with a
    # sub-heading per intervention, so they do not clutter the panel.
    module_groups = []
    for mod_key, items in mod_entries.items():
        label = ((_schema().get("modules") or {}).get(mod_key) or {}).get("label") or mod_key
        module_groups.append({"module": mod_key, "label": label,
                              "moderators": sorted(items, key=lambda m: m["name"].lower())})
    module_groups.sort(key=lambda g: g["label"].lower())

    everything = core + [m for g in module_groups for m in g["moderators"]]
    data = {"moderators": everything, "featured": featured,
            "groups": [{"group": g, "moderators": groups[g]} for g in order],
            "module_groups": module_groups,
            "names": [m["name"] for m in everything],
            "kinds": {m["name"]: m["kind"] for m in everything}}
    return data

_RETIRED_DOMAINS = {"confidence", "affect", "engagement_behavior"}

def _sdt_spec_source(p):
    """The ``source`` tag of the first data row, or "" (identifies the writer)."""
    try:
        cols, rows = _read_table(p, limit=1)
    except OSError:
        return ""
    if not rows or "source" not in cols:
        return ""
    return str(rows[0].get("source") or "").strip()


def _sdt_spec_path():
    """The SDT spec file to read.

    Delegates to ``suite_common.sdt_spec_path`` when the pipeline modules are
    importable, so the app and ``publication_bias``/``parity_check`` read the SAME
    file (W2-parity).  Preference: the **intervention Δd′** arm contrast
    (``suite_common.build_sdt_specs``, ``source``
    ``sdt_participant_delta_arm_contrast``, matching ``Results/reanalysis.xlsx``)
    over the multiverse suite's standardised linear twin (``analysis_multiverse_sdt``'s
    ``source`` ``sdt_arm_contrast``), so the app's estimand cannot silently change
    when the multiverse suite is run.
    """
    if _SC is not None:
        try:
            return _SC.sdt_spec_path()
        except Exception:
            pass
    canon = os.path.join(S.ROOT, "temp", "sdt_specs.csv")
    fallback = os.path.join(S.ROOT, "temp", "sdt_specs_pb.csv")

    def has_comp(p):
        try:
            with open(p, encoding="utf-8-sig", newline="") as fh:
                cols = next(csv.reader(fh), [])
            return any(str(c).strip() == "true_score" for c in cols)
        except OSError:
            return False

    def is_delta_dprime(p):
        return _sdt_spec_source(p) == "sdt_participant_delta_arm_contrast"

    if (has_comp(canon) and not is_delta_dprime(canon)
            and has_comp(fallback) and is_delta_dprime(fallback)):
        return fallback
    if not has_comp(canon) and has_comp(fallback):
        return fallback
    return canon

# A participant-level covariate needs at least this many studies to enter the
# moderator panel (the harmonized covariate vocabulary is highly fragmented).
MIN_PARTICIPANT_STUDIES = 5

_PART_CACHE = {"at": 0.0, "data": None}

def _participant_specs():
    """Participant-level SDT moderation rows (``temp/sdt_participant_specs.csv``)."""
    now = time.time()
    if _PART_CACHE["data"] is not None and now - _PART_CACHE["at"] < 60:
        return _PART_CACHE["data"]
    p = os.path.join(S.ROOT, "temp", "sdt_participant_specs.csv")
    rows = []
    if os.path.isfile(p):
        for r in _read_table(p)[1]:
            try:
                eff = float(r.get("effect"))
                var = float(r.get("var"))
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(eff) and math.isfinite(var) and var > 0):
                continue
            cov = (r.get("participant_cov") or "").strip()
            lvl = (r.get("participant_level") or "").strip()
            if not cov or not lvl:
                continue

            def _num(col):
                try:
                    x = float(r.get(col))
                except (TypeError, ValueError):
                    return None
                return x if math.isfinite(x) else None

            rows.append({
                "layer": (r.get("layer") or "sdt").strip(),
                "spec": (r.get("spec") or "").strip(),
                "exclusions": (r.get("exclusions") or "on").strip().lower(),
                "study_key": r.get("study_key") or "",
                "experiment_id": r.get("experiment_id") or "",
                "intervention_id": (r.get("intervention_id") or "").strip(),
                "contrast_id": (r.get("contrast_id") or "").strip(),
                "dv_domain": (r.get("dv_domain") or "").strip(),
                "dv_name": (r.get("dv_name") or "").strip(),
                "veracity": (r.get("veracity") or "").strip(),
                "contrast_type": (r.get("contrast_type") or "").strip(),
                "r_used": None,
                "true_score": _num("true_score"), "true_var": _num("true_var"),
                "fake_score": _num("fake_score"), "fake_var": _num("fake_var"),
                "criterion_score": _num("criterion_score"), "criterion_var": _num("criterion_var"),
                "participant_cov": cov, "participant_level": lvl,
                "participant_kind": (r.get("participant_kind") or "").strip(),
                "effect": eff, "var": var})
    _PART_CACHE.update(at=now, data=rows)
    return rows

def _participant_covariates():
    """Catalogue entries for the participant-level SDT moderators.

    Continuous covariates (numeric, e.g. age) carry numeric level values and are
    fitted as a continuous meta-regression; categorical covariates keep their
    observed levels.  Covariates mentioned by fewer than
    ``MIN_PARTICIPANT_STUDIES`` studies are hidden: a moderator that only one or
    two studies define cannot support a stable model.  The harmonized covariate
    vocabulary is highly fragmented (245 base names, 226 of them in <5 studies);
    the underlying naming issue is documented in
    ``Scripts/PARTICIPANT_MODERATORS_ASSESSMENT.md``.
    """
    by = {}
    kind_of = {}
    for r in _participant_specs():
        by.setdefault(r["participant_cov"], collections.defaultdict(set))[
            r["participant_level"]].add(r["study_key"])
        kind_of.setdefault(r["participant_cov"], r.get("participant_kind") or "categorical")
    out = []
    for cov, levels in by.items():
        studies = set().union(*levels.values()) if levels else set()
        if len(studies) < MIN_PARTICIPANT_STUDIES:
            continue
        label = _human_mod_label(re.sub(r"^cov_", "", cov))
        if kind_of.get(cov) == "continuous":
            out.append({
                "name": cov, "label": label, "group": "Participant-level",
                "kind": "continuous", "levels": [], "n_studies": len(studies),
                "sdt_only": True, "participant": True})
            continue
        if len(levels) < 2:
            continue
        out.append({
            "name": cov, "label": label, "group": "Participant-level",
            "kind": "categorical",
            "levels": [{"value": lv, "label": _human_mod_label(lv), "n_studies": len(lv_studies)}
                       for lv, lv_studies in sorted(levels.items())],
            "n_studies": len(studies), "sdt_only": True, "participant": True})
    out.sort(key=lambda m: m["label"].lower())
    return out

def _participant_cov_names():
    return {m["name"] for m in _participant_covariates()}

def _participant_rows_for(cov, layer, spec, exclusions, include_fully_within=None):
    pool = [r for r in _participant_specs()
            if r["participant_cov"] == cov and r["layer"] == layer
            and r["exclusions"] == exclusions]
    pre = [r for r in pool if r["spec"] == spec]
    if spec in DERIVED_SPECS and not pre:
        pre = _derived_rows(pool, spec) or []
    return _filter_fully_within(pre, include_fully_within)

def _spec_rows():
    """Flattened prerequisite spec rows (meta + sdt) with effect/variance + domain.

    Both layers are read through ``suite_common.load_specs`` -- the SAME
    normaliser (``yi``->``effect``, ``vi``->``var``, ``did_r_used``->``r_used``,
    the SDT file preference) that ``publication_bias.py`` uses -- so the app's
    pools and the published ``pb_*`` tables are computed from identical rows
    (W2-parity).  The legacy per-file read (with a minimal ``yi``/``vi`` shim) is
    kept as a fallback for a degraded bundle without numpy/pandas.
    """
    now = time.time()
    if _MOD_CACHE["specs"] is not None and now - _MOD_CACHE["specs_at"] < _SPEC_CACHE_TTL:
        return _MOD_CACHE["specs"]
    sourced = []          # [(layer_hint, row_dict)]
    if _SC is not None:
        try:
            _df = _SC.load_specs()
            sourced = [(str(r.get("layer") or ""), r) for r in _df.to_dict("records")]
        except Exception:
            sourced = []
    if not sourced:
        for fname, layer in (("meta_specs.csv", "meta"), ("sdt_specs.csv", "sdt")):
            p = os.path.join(S.ROOT, "temp", fname)
            if layer == "sdt":
                p = _sdt_spec_path()
            if not os.path.isfile(p):
                continue
            for r in _read_table(p)[1]:
                sourced.append((layer, r))
    def _s(x):
        if x is None:
            return ""
        sx = str(x).strip()
        return "" if sx.lower() in ("nan", "none", "<na>") else sx

    frames = []
    for layer_hint, r in sourced:
        layer = (_s(r.get("layer")) or _s(layer_hint))
        # M7 (audit): no unoriented raw-``yi`` fallback.  A spec row without a
        # finite, oriented ``effect``/``var`` is dropped (fail loud) rather than
        # accidentally pooled without its polarity sign.  The pipeline selector
        # applies the same rule.  The ``yi``/``vi`` shim below only fires for the
        # degraded no-numpy fallback, which cannot pool anyway.
        try:
            eff = float(r.get("effect") if r.get("effect") not in (None, "") else r.get("yi"))
            var = float(r.get("var") if r.get("var") not in (None, "") else r.get("vi"))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(eff) and math.isfinite(var) and var > 0):
            continue
        dom = _s(r.get("dv_domain")) or _s(r.get("domain"))
        if layer == "sdt" and dom in _RETIRED_DOMAINS:
            continue
        try:
            r_used = float(r.get("r_used") if r.get("r_used") not in (None, "") else r.get("did_r_used"))
        except (TypeError, ValueError):
            r_used = None

        def _num(col):
            try:
                x = float(r.get(col))
            except (TypeError, ValueError):
                return None
            return x if math.isfinite(x) else None

        frames.append({
            "layer": layer, "spec": _s(r.get("spec")),
            "exclusions": _s(r.get("exclusions") or "on").lower(),
            "study_key": _s(r.get("study_key")),
            "experiment_id": _s(r.get("experiment_id")) or _s(r.get("Study_ID")),
            "intervention_id": _s(r.get("intervention_id")),
            "contrast_id": _s(r.get("contrast_id")),
            "cluster_id": _s(r.get("cluster_id")),
            "dv_domain": dom,
            "dv_name": _s(r.get("dv_name")),
            "metric": _s(r.get("metric")),
            "veracity": _s(r.get("veracity")),
            "contrast_type": _s(r.get("contrast_type")),
            "r_used": r_used,
            "true_score": _num("true_score"), "true_var": _num("true_var"),
            "fake_score": _num("fake_score"), "fake_var": _num("fake_var"),
            "criterion_score": _num("criterion_score"), "criterion_var": _num("criterion_var"),
            "effect": eff, "var": var})
    _MOD_CACHE.update(specs=frames, specs_at=now)
    return frames
# ---------------------------------------------------------------- timepoint specs
# The Pretest / Follow-up controls map to a specification.  "only" uses the base
# specification directly (studies that measured the timepoint); "-" ignores it;
# "all" adds a derived specification that keeps every study, using the corrected
# (DiD) or delayed (follow-up) effect where it exists and the immediate posttest
# otherwise, so no contrast is counted twice.
DERIVED_SPECS = {"didpost_all", "didpost_cred", "fupost_all", "fupost_cred",
                 "fudidpost_all", "fudidpost_cred"}

def _derived_rows(rows, spec):
    """Rows for a derived ``all`` specification (didpost / fupost / fudidpost).

    ``rows`` holds every row for one layer+exclusions.  The corrected/delayed
    base rows are all kept; the immediate-posttest rows they supersede are
    dropped, so every other contrast is preserved and nothing is counted twice.
    Returns ``None`` when the corrected base specification is absent.

    ``fudidpost`` composes the three priorities follow-up > pretest-corrected
    (DiD) > immediate post, so the Pretest control is honoured even while the
    Follow-up control is "all".
    """
    cred = spec.endswith("_cred")
    layer = rows[0]["layer"] if rows else ""
    # Every base population is selected through the ONE shared selector so the
    # derived spec inherits the same eligible / exclusions / |DiD| guard filters
    # as its bases (audit C2).  ``rows`` already carries layer + exclusions.
    _layer_key = (rows[0].get("layer") or None) if rows else None
    _excl = (rows[0].get("exclusions") or "on") if rows else "on"

    def _sel(label):
        if SPECS_API is not None:
            try:
                d = SPECS_API.select_spec_rows(rows, layer=_layer_key, spec=label,
                                               exclusions=_excl)
                return d.to_dict("records")
            except Exception:
                pass
        return [r for r in rows if r.get("spec") == label]

    if spec.startswith("fudidpost"):
        inner = _derived_rows(rows, "didpost_cred" if cred else "didpost_all")
        if not inner:
            return None
        fu_spec = "persistence_cred" if cred else "persistence_all"
        population = {r["study_key"] for r in inner}
        fu_rows = [r for r in _sel(fu_spec) if r["study_key"] in population]
        if not fu_rows:
            return inner
        kf = (lambda r: (r["study_key"], r["experiment_id"], r["dv_domain"] or "")) \
            if layer == "sdt" else \
            (lambda r: (r["study_key"], r["experiment_id"], r["dv_name"] or r["dv_domain"]))
        superseded = {kf(r) for r in fu_rows}
        return fu_rows + [r for r in inner if kf(r) not in superseded]
    if spec.startswith("didpost"):
        primary = "did_cred" if cred else "did_all"
        secondary = "post_cred" if cred else "post_all"
    elif spec.startswith("fupost"):
        primary = "persistence_cred" if cred else "persistence_all"
        secondary = "post_cred" if cred else "post_all"
    else:
        return None
    if layer == "sdt":
        # SDT rows carry the artifact in ``experiment_id`` and the outcome in
        # ``dv_domain``; ``dv_name`` differs between the immediate and the
        # corrected/follow-up measurement of the same outcome, so match on the
        # domain (else both would be kept and a contrast counted twice).
        kf = lambda r: (r["study_key"], r["experiment_id"], r["dv_domain"] or "")
    elif spec.startswith("didpost"):
        # meta contrast ids identify the coded contrast
        kf = lambda r: (r["study_key"], r["experiment_id"], r["contrast_id"])
    else:
        # The same outcome measured at post and again at follow-up appears as two
        # rows, so the follow-up supersedes the posttest for that outcome; every
        # other posttest contrast is still included.
        kf = lambda r: (r["study_key"], r["experiment_id"],
                        r["dv_name"] or r["dv_domain"])
    base_all = _sel(primary)
    secondary_rows = _sel(secondary)
    if not secondary_rows:
        return None
    # The "all" option must not enlarge the study set relative to the immediate
    # specification ("-"): only studies that appear in the immediate spec may
    # contribute.  A study with a follow-up (or pretest correction) but no
    # immediate posttest is therefore not added merely because the follow-up
    # exists.  Study counts stay equal to the "-" specification; only which
    # measurement is used (and hence k) can change.
    population = {r["study_key"] for r in secondary_rows}
    base = [r for r in base_all if r["study_key"] in population]
    superseded = {kf(r) for r in base}
    return base + [r for r in secondary_rows if kf(r) not in superseded]

# ---------------------------------------------------------------------------
# Fully-within studies (PI decision: opt-in, OFF by default)
# ---------------------------------------------------------------------------
# Single-arm records that report only within-person pre->post effects are
# excluded from the in-process analysis views by default.  The switch arrives as
# ``fully_within=1`` on the request (GET query / POST payload); the spawned
# analysis subprocess receives it as the INCLUDE_FULLY_WITHIN env var.
_FW_KEYS_CACHE = {"at": 0.0, "records": None, "studies": None}


def _fully_within_sets():
    """(set of (study_key, experiment_id), set of all-fully-within study keys).

    Cached ~30s.  Record-level so a mixed paper (some fully-within records, some
    not) still drops only its fully-within records.
    """
    now = time.time()
    if (_FW_KEYS_CACHE["records"] is not None
            and now - _FW_KEYS_CACHE["at"] < 30):
        return _FW_KEYS_CACHE["records"], _FW_KEYS_CACHE["studies"]
    records, studies = set(), set()
    for key in paper_keys():
        try:
            recs = [r for r in (load_extraction(key) or []) if isinstance(r, dict)]
        except Exception:
            recs = []
        if not recs:
            continue
        all_fw = True
        for r in recs:
            is_fw = False
            if _SC is not None:
                try:
                    is_fw = bool(_SC.is_fully_within(r))
                except Exception:
                    is_fw = False
            if not is_fw:
                try:
                    from studio_lib import is_fully_within_record as _lib_fw
                    is_fw = bool(_lib_fw(r))
                except Exception:
                    is_fw = False
            if is_fw:
                records.add((key, str(r.get("Study_ID") or "")))
            else:
                all_fw = False
        if all_fw:
            studies.add(key)
    _FW_KEYS_CACHE.update(at=now, records=records, studies=studies)
    return records, studies


def _fw_truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _include_fully_within(src=None):
    """Resolve the fully-within switch from a bool, a dict payload, or the request."""
    if isinstance(src, bool):
        return src
    if isinstance(src, dict):
        for k in ("fully_within", "include_fully_within"):
            if k in src:
                return _fw_truthy(src.get(k))
        return False
    try:
        v = request.args.get("fully_within", request.args.get("include_fully_within"))
        if v is not None:
            return _fw_truthy(v)
    except Exception:
        pass
    try:
        payload = (request.get_json(silent=True)
                   if request.method in ("POST", "PUT", "PATCH") else None)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        for k in ("fully_within", "include_fully_within"):
            if k in payload:
                return _fw_truthy(payload.get(k))
    return False


def _filter_fully_within(rows, include=None):
    """Drop fully-within rows unless the switch is on (record-level)."""
    if include is None:
        include = _include_fully_within()
    if include or not rows:
        return rows
    records, studies = _fully_within_sets()
    if not records and not studies:
        return rows

    def keep(r):
        key = str(r.get("study_key"))
        if key in studies:
            return False
        return (key, str(r.get("experiment_id") or "")) not in records

    return [r for r in rows if keep(r)]


def _spec_rows_for(layer, spec, exclusions, include_fully_within=None):
    """Rows for one (layer, spec, exclusions) via the ONE shared selector.

    Interface §3: ``specs_api.select_spec_rows`` applies, in one place, the
    ``r_used == 0.5`` primary filter, eligibility, exclusions on/off, the
    fully-within switch and the ``|DiD| <= DID_ABS_MAX`` quality guard — so this
    table's ``k`` and its estimate always come from the same row set (audit C2).

    Derived ``all`` specifications that the pipeline has not materialised (the
    app-only ``fudidpost_*``) are built on the fly by ``_derived_rows``, whose
    bases are themselves selected through the shared selector.  Fully-within
    studies are dropped unless the request's ``fully_within`` switch is on.
    """
    include = (_include_fully_within(include_fully_within)
               if include_fully_within is not None else _include_fully_within())
    rows = _spec_rows()
    if not rows:
        return []
    if SPECS_API is None:
        # Fail loud rather than silently pooling an unselected row set.
        raise RuntimeError("specs_api unavailable: cannot select spec rows "
                           "(bundle numpy/scipy/pandas; see CodingStudio.spec)")
    d = SPECS_API.select_spec_rows(rows, layer=layer, spec=spec,
                                   exclusions=exclusions,
                                   include_fully_within=include)
    out = d.to_dict("records") if len(d) else []
    if not out and spec in DERIVED_SPECS:
        # Derive from this layer's rows only (both layers share the file).
        layer_rows = [r for r in rows if r.get("layer") == layer]
        out = _derived_rows(layer_rows, spec) or []
    return _filter_fully_within(out, include)

def _derived_spec_options(base):
    """Derived ``all`` specifications offered when the base spec is available."""
    # A base option with k=0 (e.g. the SDT derived specs before the pipeline
    # materialises them) must not suppress the on-the-fly derivation.
    have = {(o["layer"], o["spec"], o["exclusions"]) for o in base
            if int(o.get("k") or 0) > 0}
    out = []
    for layer in ("meta", "sdt"):
        for spec in sorted(DERIVED_SPECS):
            for excl in ("on", "off"):
                if (layer, spec, excl) in have:
                    continue
                res = _pooled_for(layer, spec, excl)
                if not res:
                    continue
                out.append({"layer": layer, "spec": spec, "exclusions": excl,
                            "k": res["k"], "n_studies": str(res["n_studies"]),
                            "est": f"{res['est']:.6g}", "lo": f"{res['lo']:.6g}",
                            "hi": f"{res['hi']:.6g}", "derived": True})
    return out

def _derived_pooled_row(cols, layer, spec, exclusions):
    """Synthetic pooled-model row for a derived ``all`` specification.

    Values come from ``_pooled_for`` (shared selector + ``pool_dependence``), so
    the derived row carries the same dependence-aware columns as the pipeline's
    ``pb_pooled.csv`` (audit C2).
    """
    res = _pooled_for(layer, spec, exclusions)
    if not res:
        return None
    row = {c: "" for c in cols}
    for src, dst in (("dl_k", "k"), ("n_studies", "n_studies"), ("dl_est", "dl_est"),
                     ("dl_se", "dl_se"), ("dl_lo", "dl_lo"), ("dl_hi", "dl_hi"),
                     ("dl_tau2", "dl_tau2"), ("dl_i2", "dl_i2"),
                     ("three_est", "three_est"), ("three_se", "three_se"),
                     ("three_lo", "three_lo"), ("three_hi", "three_hi"),
                     ("rve_est", "rve_est"), ("rve_se", "rve_se"),
                     ("rve_lo", "rve_lo"), ("rve_hi", "rve_hi"),
                     ("rve_df", "rve_df"), ("n_clusters", "n_clusters"),
                     ("max_weight_share_dl", "max_weight_share_dl"),
                     ("max_weight_share_rve", "max_weight_share_rve")):
        if dst in row and res.get(src) is not None:
            row[dst] = res.get(src)
    row["layer"], row["spec"], row["exclusions"] = layer, spec, exclusions
    return row

# Paper-ready labels for the Intervention_Type vocabulary (mirrors
# Scripts/suite_common.INTERVENTION_TYPE_LABELS; app.py cannot import the numpy
# pipeline, so the small map is duplicated here).
_INTERVENTION_LABELS = {
    "accuracy_nudge": "Accuracy prompt",
    "accuracy_prompts": "Accuracy prompt",
    "community_correction": "Community correction",
    "correction": "Correction",
    "credibility_labels": "Credibility labels",
    "debunking": "Debunking",
    "debunking_and_rebuttals": "Debunking / rebuttals",
    "fact_checking_training": "Fact-checking training",
    "friction": "Friction",
    "generic": "Generic / umbrella",
    "inoculation": "Inoculation / prebunking",
    "lateral_reading": "Lateral reading",
    "media_literacy": "Media literacy",
    "media_literacy_tips": "Media-literacy tips",
    "mix": "Multi-component",
    "none": "No intervention",
    "openmindedness_intervention": "Open-mindedness",
    "other": "Other",
    "platform_exposure_reduction": "Platform exposure reduction",
    "prebunking_warning": "Prebunking warning",
    "social_norms": "Social norms",
    "source_credibility_labels": "Source-credibility labels",
    "sourcing": "Sourcing",
    "verification": "Verification",
    "warning_and_fact_checking_labels": "Warning / fact-check labels",
}

def _human_mod_label(token):
    """Humanise a vocabulary token (``lateral_reading`` -> ``Lateral reading``)."""
    s = str(token or "").strip()
    if s in _INTERVENTION_LABELS:
        return _INTERVENTION_LABELS[s]
    return s.replace("_", " ").strip().capitalize()

def _mod_labels(name):
    # include module-routed fields so their option labels are humanised too
    fields = list(_schema_field_map().values()) + list(_schema().get("dv_fields") or [])
    out = {}
    for f in fields:
        if f.get("name") != name:
            continue
        opts = f.get("options") or []
        if opts:
            out.update({_norm(o.get("value")): str(o.get("label")) for o in opts})
        vocab = f.get("vocab")
        if isinstance(vocab, dict):
            vocab = list(vocab.keys())
        if vocab:
            out.update({_norm(v): _human_mod_label(v) for v in vocab})
        if out:
            break
    # Numeric controlled vocabularies (e.g. contrast_type levels 8-13) live in
    # the schema-level vocabularies, not in the field's own option list.
    vkey = _VOCAB_FOR_FIELD.get(name)
    if vkey:
        vd = (_schema().get("vocabularies") or {}).get(vkey)
        if isinstance(vd, dict):
            for k, tok in vd.items():
                out.setdefault(_norm(k), _human_mod_label(tok))
    # Display-only names for observed values missing from the schema.
    for k, lab in _EXTRA_LEVEL_LABELS.get(name, {}).items():
        out.setdefault(_norm(k), lab)
    return out

def _mod_label(name, value):
    v = _norm(value)
    if str(name) == "data_available":
        return {"0": "Paper only", "1": "Primary data available"}.get(v, value)
    if str(name) == "Publication":
        return _PUBLICATION_LABELS.get(v) or _mod_labels(name).get(v, value)
    if str(name).startswith("cov_"):
        return _human_mod_label(v)
    if str(name) == DESIGN_FACTORS_COMBINATION_FIELD:
        return _design_factor_combination_label(v)
    if v in _CLUSTER_LABELS:
        return _CLUSTER_LABELS[v]
    return _mod_labels(name).get(v, value)

def _moderator_level_effects(kept, moderators, kind_of, model):
    """Pooled effect at the levels of every categorical moderator.

    This is the easy stand-in for Patzl et al.'s "separate model per level": it
    fits an intercept-only random-effects model to each level subset.  Every
    observed level is disclosed for a controlled vocabulary; only the open
    design-factor *combination* field is bounded (see ``_level_cap``), with its
    rare combinations pooled into one "Other (n combinations)" row.  A genuine
    ``other`` level is always kept visible even when it is rare (so
    "Stimulus topic -> Other" still counts).
    """
    def level_entry(value, recs, label):
        y = [x["effect"] for x in recs]
        vv = [x["var"] for x in recs]
        if not y:
            return None
        n_studies = len({x["study_key"] for x in recs})
        if model == "bayes":
            res = _bayes_pool(y, vv)
            if not res:
                return None
            return {"value": value, "label": label, "k": res["k"], "n_studies": n_studies,
                    "estimate": res["est"], "lo": res["lo"], "hi": res["hi"],
                    "tau": res["tau"], "i2": None}
        # Dependence-aware pool (H1): the per-level estimate uses
        # ``pool_dependence`` (3-level + RVE on cluster_id); the reported point
        # estimate is the DL one, matching ``pb_intervention_type`` / ``pb_domain``.
        layer = (recs[0].get("layer") if recs else "meta")
        res = _pool_rows(recs, layer)
        return {"value": value, "label": label, "k": res["k"], "n_studies": n_studies,
                "estimate": res.get("dl_est", res["est"]),
                "lo": res.get("dl_lo", res["lo"]), "hi": res.get("dl_hi", res["hi"]),
                "tau": math.sqrt(max(res["dl_tau2"], 0.0))
                if res.get("dl_tau2") is not None else None,
                "i2": res.get("dl_i2")}

    out = {}
    for name in moderators:
        if kind_of.get(name) == "continuous":
            continue
        by = collections.defaultdict(list)
        if name in MULTI_LABEL_MODERATORS:
            # Multi-label moderator: a record contributes to EVERY atomic label
            # it carries (["time", "design_control"] counts for both), so the
            # per-level models are membership models, not one-level-per-row.
            for rec in kept:
                for v in _values(_mods_for_row(rec).get(name)):
                    if v != "":
                        by[_norm(v)].append(rec)
        else:
            for rec in kept:
                v = _norm(rec.get(name))
                if v != "":
                    by[v].append(rec)
        items = sorted(by.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        cap = _level_cap(name, len(items))
        top = items if cap is None else items[:cap]
        shown = {v for v, _ in top}
        # keep a genuine "other" level visible even when it is outside the top
        keep_other = [(v, rs) for v, rs in items if v == "other" and v not in shown]
        rest = [(v, rs) for v, rs in items if v not in shown and v != "other"]
        entries = []
        for v, recs in list(top) + keep_other:
            e = level_entry(v, recs, _mod_label(name, v))
            if e:
                entries.append(e)
        if rest:
            pooled = [r for _, rs in rest for r in rs]
            if name in COMBINATION_MODERATORS:
                label = f"Other combination ({len(rest)} combinations)"
            else:
                label = f"Other ({len(rest)} levels)"
            e = level_entry("__other__", pooled, label)
            if e:
                entries.append(e)
        out[name] = entries
    return out

def _bayes_rows(rows):
    """Drop non-finite cells.

    The |DiD| quality guard is applied by ``specs_api.select_spec_rows`` *before*
    this point (via ``_spec_rows_for``), so it no longer depends on
    ``rows[0]["layer"]`` (audit C2: ``fudidpost_*`` was previously unguarded).
    """
    return [r for r in rows
            if math.isfinite(r["effect"]) and math.isfinite(r["var"]) and r["var"] > 0]

def _bayes_for(layer, spec, exclusions, scope="unrestricted"):
    rows = _spec_rows_for(layer, spec, exclusions)
    if scope == "restricted":
        rows = [r for r in rows if r["dv_domain"] in SUSCEPTIBILITY]
    rows = _bayes_rows(rows)
    if not rows:
        return None
    res = _bayes_pool([r["effect"] for r in rows], [r["var"] for r in rows])
    if res:
        res["n_studies"] = len({r["study_key"] for r in rows})
    return res

def _bayes_groups(rows, by, mu_sd=BAYES_MU_SD, tau_sd=BAYES_TAU_SD, tau_prior="half-normal"):
    groups = {}
    for r in rows:
        for v in _values(_mods_for_row(r).get(by)):
            if v not in (None, ""):
                groups.setdefault(_norm(v), []).append(r)
    out = []
    for v, gg in groups.items():
        if len(gg) < 2:
            continue
        res = _bayes_pool([r["effect"] for r in gg], [r["var"] for r in gg],
                          mu_sd=mu_sd, tau_sd=tau_sd, tau_prior=tau_prior)
        if not res:
            continue
        out.append({"level": v, "label": _mod_label(by, v), "k": res["k"],
                    "n_studies": len({r["study_key"] for r in gg}),
                    "est": res["est"], "lo": res["lo"], "hi": res["hi"],
                    "tau": res["tau"], "p_gt0": res["p_gt0"]})
    out.sort(key=lambda x: -x["k"])
    return out

def _bayes_domain(rows, mu_sd=BAYES_MU_SD, tau_sd=BAYES_TAU_SD, tau_prior="half-normal"):
    groups = {}
    for r in rows:
        groups.setdefault(r["dv_domain"], []).append(r)
    out = []
    for dom, gg in groups.items():
        if len(gg) < 2:
            continue
        res = _bayes_pool([r["effect"] for r in gg], [r["var"] for r in gg],
                          mu_sd=mu_sd, tau_sd=tau_sd, tau_prior=tau_prior)
        if not res:
            continue
        out.append({"dv_domain": dom, "k": res["k"],
                    "n_studies": len({r["study_key"] for r in gg}),
                    "est": res["est"], "lo": res["lo"], "hi": res["hi"],
                    "tau": res["tau"], "p_gt0": res["p_gt0"]})
    out.sort(key=lambda x: -x["k"])
    return out

def _bayes_sensitivity(rows, grid_n=161):
    """BF robustness across the constrained prior presets (for the UI table)."""
    y = [r["effect"] for r in rows]
    v = [r["var"] for r in rows]
    mu = []
    for sd in BAYES_MU_SD_PRESETS:
        r = _bayes_pool(y, v, grid_n=grid_n, mu_sd=sd,
                        tau_sd=BAYES_TAU_SD, tau_prior="half-normal")
        if r:
            mu.append({"mu_sd": sd, "bf10": r["bf10"], "est": r["est"],
                       "lo": r["lo"], "hi": r["hi"]})
    tau = []
    for kind, sd in BAYES_TAU_PRESETS:
        r = _bayes_pool(y, v, grid_n=grid_n, mu_sd=BAYES_MU_SD,
                        tau_sd=sd, tau_prior=kind)
        if r:
            tau.append({"tau_prior": kind, "tau_sd": sd, "bf01_tau0": r["bf01_tau0"],
                        "tau": r["tau"], "tau_lo": r["tau_lo"], "tau_hi": r["tau_hi"],
                        "bf10": r["bf10"]})
    return {"mu": mu, "tau": tau}

def _human_term(term):
    """Humanise moderator variable names in a term label (display only)."""
    out = str(term)
    labels = {m["name"]: m.get("label", m["name"])
              for m in _moderator_catalog().get("moderators", [])}
    for name in sorted(labels, key=len, reverse=True):
        out = out.replace(name, labels[name])
    return out

def _moderator_figure(layer, spec, coefs):
    """Coefficient forest for the fitted moderator model (matplotlib optional)."""
    terms = [c for c in coefs if c["kind"] != "intercept"]
    if not terms:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None
    names = [_human_term(c["term"]) for c in terms][::-1]
    est = [c["estimate"] for c in terms][::-1]
    lo = [c["lo"] for c in terms][::-1]
    hi = [c["hi"] for c in terms][::-1]
    ys = list(range(len(terms)))
    fig, ax = plt.subplots(figsize=(7, max(2.4, 0.3 * len(terms) + 1.2)))
    ax.errorbar(est, ys, xerr=[[e - l for e, l in zip(est, lo)], [h - e for e, h in zip(est, hi)]],
                fmt="o", ms=4, color="#2166ac", ecolor="#999", capsize=2)
    ax.axvline(0, color="grey", ls="--", lw=0.8)
    ax.set_yticks(ys)
    ax.set_yticklabels(names, fontsize=8)
    ax.set_xlabel("moderator coefficient (95% CI)")
    ax.set_title(f"Moderator model · {_APA_LAYER.get(layer, layer)} · "
                 f"{_APA_SPEC.get(spec, spec)}", fontsize=10)
    ax.grid(axis="x", alpha=0.25)
    tag = hashlib.sha1("|".join(c["term"] for c in terms).encode("utf-8")).hexdigest()[:8]
    name = f"moderatorfit_{layer}_{spec}_{tag}.png"
    try:
        os.makedirs(_figures_dir(), exist_ok=True)
        fig.savefig(os.path.join(_figures_dir(), name), dpi=200, bbox_inches="tight")
    except Exception:
        name = None
    plt.close(fig)
    return name

def _moderator_display_label(name):
    """Paper-ready label for a moderator name (catalogue first, then humanised)."""
    for m in _moderator_catalog().get("moderators", []):
        if m.get("name") == name:
            return m.get("label") or _human_mod_label(name)
    if str(name).startswith("cov_"):
        return _human_mod_label(str(name)[4:])
    return _human_mod_label(name)

def _slug(name):
    s = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")
    return s[:60] or "mod"

def _moderator_single_figure(name, kind, entries, rows):
    """One picture for a single moderator.

    Categorical: a forest of the per-level pooled estimates.  Continuous: a
    bubble plot of the study estimates against the moderator value with a
    weighted least-squares line.  Returns the file name (or ``None``).
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None
    label = _moderator_display_label(name)
    xlab = _human_mod_label(str(name)[4:]) if str(name).startswith("cov_") else _human_mod_label(name)
    if kind == "continuous":
        pts = []
        for r in rows:
            try:
                x = float(r.get(name))
            except (TypeError, ValueError):
                continue
            if (math.isfinite(x) and math.isfinite(r["effect"])
                    and math.isfinite(r["var"]) and r["var"] > 0):
                pts.append((x, r["effect"], r["var"]))
        if len(pts) < 3:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        ws = [1.0 / p[2] for p in pts]
        fig, ax = plt.subplots(figsize=(6.4, 3.4))
        sizes = [max(6.0, min(160.0, 40.0 / math.sqrt(v))) for _, _, v in pts]
        ax.scatter(xs, ys, s=sizes, color="#2166ac", alpha=0.6, edgecolor="none")
        fit = _wls(ys, xs, ws)
        if fit and fit.get("b") is not None and math.isfinite(fit["b"]):
            b, a = fit["b"], fit["a"]
            xr = [min(xs), max(xs)]
            ax.plot(xr, [a + b * x for x in xr], color="#b2182b", lw=1.4,
                    label=f"slope={b:.3g}")
            ax.legend(fontsize=8)
        ax.axhline(0, color="grey", ls="--", lw=0.8)
        ax.set_xlabel(xlab, fontsize=9)
        ax.set_ylabel("effect", fontsize=9)
        ax.set_title(f"{label} (continuous) · k={len(pts)}", fontsize=9)
        ax.grid(alpha=0.25)
    else:
        entries = entries or []
        if not entries:
            return None
        names = [e["label"] for e in entries][::-1]
        est = [e["estimate"] for e in entries][::-1]
        lo = [e["lo"] for e in entries][::-1]
        hi = [e["hi"] for e in entries][::-1]
        ys = list(range(len(entries)))
        fig, ax = plt.subplots(figsize=(6.4, max(2.2, 0.42 * len(entries) + 1.0)))
        ax.errorbar(est, ys, xerr=[[e - l for e, l in zip(est, lo)],
                                   [h - e for e, h in zip(est, hi)]],
                    fmt="o", ms=4, color="#2166ac", ecolor="#999", capsize=2)
        ax.axvline(0, color="grey", ls="--", lw=0.8)
        ax.set_yticks(ys)
        ax.set_yticklabels(names, fontsize=8)
        ax.set_xlabel("pooled effect (95% CI)", fontsize=9)
        ax.set_title(f"{label} · {len(entries)} level(s)", fontsize=9)
        ax.grid(axis="x", alpha=0.25)
    tag = hashlib.sha1((str(name) + "|" + str(kind)).encode("utf-8")).hexdigest()[:8]
    fname = f"modsingle_{_slug(name)}_{tag}.png"
    try:
        os.makedirs(_figures_dir(), exist_ok=True)
        fig.savefig(os.path.join(_figures_dir(), fname), dpi=200, bbox_inches="tight")
    except Exception:
        fname = None
    plt.close(fig)
    return fname

def _moderator_figures(kept, moderators, kind_of, level_effects):
    """One figure per selected moderator (right rail), independent of the fit."""
    out = []
    for name in moderators:
        kind = kind_of.get(name, "categorical")
        entries = None if kind == "continuous" else (level_effects.get(name) or [])
        fname = _moderator_single_figure(name, kind, entries, kept)
        if fname:
            out.append({"name": name, "label": _moderator_display_label(name), "file": fname})
    return out

def _sel_rows(path, layer, spec, exclusions):
    if not os.path.isfile(path):
        return [], []
    cols, rows = _read_table(path)
    def ok(r):
        if "layer" in cols and r.get("layer") and r.get("layer") != layer:
            return False
        if "spec" in cols and r.get("spec") and r.get("spec") != spec:
            return False
        if "exclusions" in cols and r.get("exclusions") not in (None, ""):
            if r.get("exclusions") != exclusions:
                return False
        return True
    return cols, [r for r in rows if ok(r)]

def _fmt_ci(lo, hi, d=2):
    def f(x):
        try:
            return f"{float(x):.{d}f}"
        except (TypeError, ValueError):
            return "–"
    return f"[{f(lo)}, {f(hi)}]"

def _robustness_combined(layer, spec, exclusions):
    """One combined robustness/publication-bias view for the current selection.

    For the SDT layer the meta-layer publication-bias battery is not meaningful
    (the effect is a within-person discrimination index), so it is replaced by the
    decomposition into true score, fake score and response bias (criterion c).
    """
    temp = os.path.join(S.ROOT, "temp")
    _, ex = _sel_rows(os.path.join(temp, "pb_exclusions_onoff.csv"), layer, spec, exclusions)
    _, rs = _sel_rows(os.path.join(temp, "pb_r_sensitivity.csv"), layer, spec, exclusions)
    _, mf = _sel_rows(os.path.join(temp, "pb_metric_family.csv"), layer, spec, exclusions)
    _, mo = _sel_rows(os.path.join(temp, "pb_moderator.csv"), layer, spec, exclusions)
    cols = ["Analysis", "Test / stratum", "k", "Estimate", "95% CI", "Statistic", "p"]
    rows = []

    def add(analysis, test, k, est, ci, stat, p):
        rows.append({"Analysis": analysis, "Test / stratum": test, "k": k,
                     "Estimate": est, "95% CI": ci, "Statistic": stat, "p": p})

    if layer == "sdt":
        _, sd = _sel_rows(os.path.join(temp, "pb_sdt_components.csv"), layer, spec, exclusions)
        for r in sd:
            add("SDT components", str(r.get("component")), r.get("k"), r.get("est"),
                _fmt_ci(r.get("lo"), r.get("hi")), "", "")
    else:
        _, pb = _sel_rows(os.path.join(temp, "pb_publication_bias.csv"), layer, spec, exclusions)
        for r in pb:
            add("Publication bias", "Egger intercept", r.get("k"), r.get("egger_intercept"), "",
                f"t = {r.get('egger_t')}", r.get("egger_p"))
            add("Publication bias", "PET", r.get("k"), r.get("pet"), "", "", r.get("pet_p"))
            add("Publication bias", "PEESE", r.get("k"), r.get("peese"), "", "", r.get("peese_p"))
            add("Publication bias", f"Trim-and-fill (prefer {r.get('pet_peese_prefer') or '–'})",
                r.get("k"), r.get("tf_adj_est"), f"[{r.get('tf_adj_lo')}, {r.get('tf_adj_hi')}]",
                f"k0 = {r.get('tf_k0')}", "")
            add("Publication bias", "p-curve", r.get("pc_n_sig"), "", "",
                f"n significant = {r.get('pc_n_sig')}", r.get("pcurve_status"))
    for r in ex:
        add("Exclusions applied vs ignored", _apa_spec(r.get("spec")), r.get("on_k"),
            r.get("on_est"), "", f"ignored = {r.get('off_est')}",
            f"Δ = {r.get('delta_off_minus_on')}")
    for r in rs:
        add("Pretest r sensitivity", f"{_apa_spec(r.get('spec'))} (r = {r.get('r_used')})", r.get("k"),
            r.get("est"), _fmt_ci(r.get("lo"), r.get("hi")), "", "")
    for r in mf:
        add("Effect-size metric family", _apa_metric(r.get("metric")), r.get("k"), r.get("est"),
            _fmt_ci(r.get("lo"), r.get("hi")), "", "")
    for r in mo:
        if str(r.get("level_label")) == "Difference (meta-regression)":
            add("Moderator: data availability", str(r.get("level_label")), r.get("k"),
                r.get("est"), _fmt_ci(r.get("lo"), r.get("hi")), "", r.get("p"))
    return cols, rows

def _human_contrast(s):
    """Humanise a contrast id for display (domains + readable separators)."""
    out = str(s)
    # 2026-09-19: legacy `belief_accuracy` contrast ids display as the merged domain.
    if "belief_accuracy" in out:
        out = out.replace("belief_accuracy", _apa_domain("credibility"))
    for dom in ("sharing_intention", "credibility",
                "technique_recognition", "attitude_policy",
                "search_behavior", "trust",
                "media_literacy_attitudes", "critical_thinking"):
        if dom in out:
            out = out.replace(dom, _apa_domain(dom))
    return out.replace("|", " · ")

def _loo_summary(layer, spec, exclusions):
    """Leave-one-out reduced to mean / min / max plus the outlier studies."""
    _, loo = _sel_rows(os.path.join(S.ROOT, "temp", "pb_leaveoneout.csv"), layer, spec, exclusions)
    cols = ["Unit", "k", "Full estimate", "Mean LOO", "Min LOO", "Max LOO",
            "Influential studies", "Most extreme LOO"]
    rows = []
    by_unit = collections.defaultdict(list)
    for r in loo:
        by_unit[str(r.get("unit_type") or "contrast")].append(r)
    for unit, gg in by_unit.items():
        ests = []
        for r in gg:
            try:
                ests.append(float(r.get("est")))
            except (TypeError, ValueError):
                pass
        if not ests:
            continue
        full = gg[0].get("full_est")
        infl = [_human_contrast(r.get("omitted")) for r in gg if str(r.get("influential")) == "1"]
        extremes = []
        for r in gg:
            try:
                extremes.append((abs(float(r.get("delta"))), _human_contrast(r.get("omitted")),
                                 r.get("delta")))
            except (TypeError, ValueError):
                pass
        extremes.sort(reverse=True)
        ext_txt = "; ".join(f"{name} ({delta})" for _, name, delta in extremes[:3])
        rows.append({
            "Unit": "Contrasts" if unit == "contrast" else "Studies",
            "k": len(ests), "Full estimate": full,
            "Mean LOO": f"{sum(ests) / len(ests):.3f}",
            "Min LOO": f"{min(ests):.3f}", "Max LOO": f"{max(ests):.3f}",
            "Influential studies": ", ".join(infl[:15]) if infl else "—",
            "Most extreme LOO": ext_txt or "—",
        })
    rows.sort(key=lambda z: z["Unit"])
    return cols, rows

def _finite(x):
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _pool_rows(rows, layer="meta"):
    """Dependence-aware pool of one row set via the shared ``pool_dependence``.

    H1 (audit): dependent contrasts are pooled with the three-level model + RVE
    on ``cluster_id`` (rho = 0.5/0.8) — the pipeline's headline machinery — not
    with a row-level DerSimonian–Laird pool.

    Returned keys are the raw ``pool_dependence`` columns (``dl_*``, ``three_*``,
    ``rve_*``, ``tau2_cluster``, ``tau2_study``, ``max_weight_share_*``, ...) plus
    UI conveniences ``k`` (= ``dl_k``), ``n_studies`` and ``est``/``se``/``lo``/
    ``hi``/``tau2``/``i2``.  ``est`` is the layer's *reported* headline: the DL
    estimate for the reported-effects (meta) layer (matching ``metaanalysis.xlsx``)
    and the three-level estimate for the SDT layer (matching ``reanalysis.xlsx``),
    exactly as each pipeline product reports it.
    """
    if _SC is None:
        raise RuntimeError("suite_common unavailable: cannot pool (bundle numpy/scipy)")
    y = [r["effect"] for r in rows]
    v = [r["var"] for r in rows]
    cluster = [(r.get("cluster_id") or r.get("study_key") or "") for r in rows]
    study = [r.get("study_key") or "" for r in rows]
    dep = dict(_SC.pool_dependence(y, v, cluster, study))
    dep["k"] = int(dep.get("dl_k") or 0)
    dep["n_studies"] = len({r.get("study_key") for r in rows})
    if str(layer).lower() == "sdt" and _finite(dep.get("three_est")):
        dep["est"], dep["se"] = dep["three_est"], dep["three_se"]
        dep["lo"], dep["hi"] = dep["three_lo"], dep["three_hi"]
    else:
        dep["est"], dep["se"] = dep["dl_est"], dep["dl_se"]
        dep["lo"], dep["hi"] = dep["dl_lo"], dep["dl_hi"]
    dep["tau2"] = dep.get("dl_tau2")
    dep["i2"] = dep.get("dl_i2")
    return dep


def _pooled_for(layer, spec, exclusions, scope="unrestricted"):
    rows = _spec_rows_for(layer, spec, exclusions)
    if scope == "restricted":
        rows = [r for r in rows if r["dv_domain"] in SUSCEPTIBILITY]
    if not rows:
        return None
    return _pool_rows(rows, layer)


def _subgroup_rows(by, layer, spec, exclusions, scope="unrestricted"):
    rows = _spec_rows_for(layer, spec, exclusions)
    if scope == "restricted":
        rows = [r for r in rows if r["dv_domain"] in SUSCEPTIBILITY]
    rows = _apply_study_filter(rows, *_filter_args(request))
    groups = {}
    if by in _CONTRAST_NAMES:
        for r in rows:
            v = _norm(r.get(by))
            if v:
                groups.setdefault(v, []).append(r)
    else:
        for r in rows:
            for v in _values(_mods_for_row(r).get(by)):
                if v in (None, ""):
                    continue
                groups.setdefault(_norm(v), []).append(r)
    out = []
    n_other_combos = 0
    if by in COMBINATION_MODERATORS:
        # Disclose the top combinations; pool the rare ones into one row.
        ranked = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        groups = dict(ranked[:MAX_CAT_LEVELS])
        rest = ranked[MAX_CAT_LEVELS:]
        if rest:
            n_other_combos = len(rest)
            groups[_OTHER_COMBINATION] = [r for _, gg in rest for r in gg]
    for v, gg in groups.items():
        if len(gg) < 2:
            continue
        res = _pool_rows(gg, layer)
        label = _mod_label(by, v)
        if v == _OTHER_COMBINATION:
            label = f"Other combination ({n_other_combos} combinations)"
        out.append({"label": label, "k": res["k"],
                    "n_studies": res["n_studies"],
                    "est": res.get("dl_est", res["est"]),
                    "lo": res.get("dl_lo", res["lo"]),
                    "hi": res.get("dl_hi", res["hi"])})
    out.sort(key=lambda x: -x["k"])
    return out

def _export_xlsx(layer, spec, exclusions, scope, stamp):
    from openpyxl import Workbook
    temp = os.path.join(S.ROOT, "temp")
    out = os.path.join(temp, f"analysis_export_{layer}_{spec}_{exclusions}_{stamp}.xlsx")
    wb = Workbook()
    wb.remove(wb.active)

    def add_sheet(name, cols, rows):
        safe = re.sub(r"[\\/*?\[\]:]", "-", str(name))[:31]
        ws = wb.create_sheet(safe or "sheet")
        if cols:
            ws.append([str(c) for c in cols])
        for r in rows:
            ws.append([r.get(c) for c in cols])

    for key, fname, label in ANALYSIS_TABLES:
        path = os.path.join(temp, fname)
        if not os.path.isfile(path):
            continue
        cols, rows = _sel_rows(path, layer, spec, exclusions)
        if key == "pooled" and not rows and spec in DERIVED_SPECS:
            row = _derived_pooled_row(cols, layer, spec, exclusions)
            if row:
                rows = [row]
        if rows:
            add_sheet(label, cols, rows)
    age = _subgroup_rows("Age_Group", layer, spec, exclusions, scope)
    itype = _subgroup_rows("Intervention_Type", layer, spec, exclusions, scope)
    add_sheet("Effects by age", ["label", "k", "n_studies", "est", "lo", "hi"], age)
    add_sheet("Effects by intervention", ["label", "k", "n_studies", "est", "lo", "hi"], itype)
    wb.save(out)
    return out

def _export_pptx(layer, spec, exclusions, scope, stamp):
    from pptx import Presentation
    from pptx.util import Inches, Pt
    out = os.path.join(S.ROOT, "temp", f"analysis_export_{layer}_{spec}_{exclusions}_{stamp}.pptx")
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(10), Inches(7.5)
    blank = prs.slide_layouts[6]
    pooled = _pooled_for(layer, spec, exclusions, scope)
    layer_label = "Meta-analysis (reported effects)" if layer == "meta" else "Re-analysis (signal detection)"

    slide = prs.slides.add_slide(blank)
    box = slide.shapes.add_textbox(Inches(0.5), Inches(0.5), Inches(9), Inches(2)).text_frame
    box.text = f"Misinformation interventions — {layer_label}"
    box.paragraphs[0].font.size = Pt(28); box.paragraphs[0].font.bold = True
    sub = box.add_paragraph()
    sub.text = f"{spec} · exclusions {exclusions} · {scope}"
    sub.font.size = Pt(16)
    if pooled:
        p = box.add_paragraph()
        p.text = (f"k = {pooled['k']} contrasts · {pooled['n_studies']} studies · "
                  f"pooled {pooled['est']:.2f} [{pooled['lo']:.2f}, {pooled['hi']:.2f}]")
        p.font.size = Pt(16)

    # Effects figure
    figs = _figures_dir()
    for cand in (f"forest_{layer}_{spec}.png", f"forest_{spec}.png", "forest_domains.png"):
        p = os.path.join(figs, cand)
        if os.path.isfile(p):
            slide = prs.slides.add_slide(blank)
            slide.shapes.add_textbox(Inches(0.4), Inches(0.2), Inches(9), Inches(0.5)).text_frame.text = "Effects"
            slide.shapes.add_picture(p, Inches(0.5), Inches(0.8), width=Inches(9))
            break
    # Moderator coefficient figure (if a model was fit)
    modfig = os.path.join(figs, f"moderatorfit_{layer}_{spec}.png")
    if os.path.isfile(modfig):
        slide = prs.slides.add_slide(blank)
        slide.shapes.add_textbox(Inches(0.4), Inches(0.2), Inches(9), Inches(0.5)).text_frame.text = "Moderator model"
        slide.shapes.add_picture(modfig, Inches(0.5), Inches(0.8), width=Inches(9))
    # Age / intervention-type table
    age = _subgroup_rows("Age_Group", layer, spec, exclusions, scope)
    itype = _subgroup_rows("Intervention_Type", layer, spec, exclusions, scope)
    if age or itype:
        slide = prs.slides.add_slide(blank)
        slide.shapes.add_textbox(Inches(0.4), Inches(0.2), Inches(9), Inches(0.5)).text_frame.text = \
            "Subgroup effects (age, intervention type)"
        rows = [["Moderator", "Level", "k", "Studies", "Effect", "95% CI"]]
        rows += [["Age", r["label"], r["k"], r["n_studies"], f"{r['est']:.2f}",
                  f"{r['lo']:.2f}, {r['hi']:.2f}"] for r in age]
        rows += [["Intervention", r["label"], r["k"], r["n_studies"], f"{r['est']:.2f}",
                  f"{r['lo']:.2f}, {r['hi']:.2f}"] for r in itype]
        rows = rows[:18]
        table = slide.shapes.add_table(len(rows), 6, Inches(0.4), Inches(0.9),
                                       Inches(9.2), Inches(0.3) * len(rows)).table
        for i, row in enumerate(rows):
            for j, val in enumerate(row):
                table.cell(i, j).text = str(val)
    prs.save(out)
    return out

def _python_interpreter():
    if not FROZEN:
        return sys.executable
    for name in ("python", "python3", "py"):
        exe = shutil.which(name)
        if exe:
            return exe
    return None

def _analysis_worker(kind, force=False, include_fully_within=False):
    state = _analysis_run
    scripts_dir = os.path.join(S.ROOT, "Scripts")
    exe = _python_interpreter()
    log_lines = []
    _invalidate_analysis_caches()

    def emit(line):
        log_lines.append(line)
        state["log"] = "".join(log_lines)[-20000:]

    if exe is None:
        state.update(ok=False, error="no Python interpreter found (packaged app); "
                                     "run Scripts/publication_bias.py from a Python environment")
        state["running"] = False
        state["finished"] = datetime.now().isoformat(timespec="seconds")
        return
    wanted = ANALYSIS_RUN_KINDS.get(kind, [])
    missing = [s for s in wanted if not os.path.exists(os.path.join(scripts_dir, s))]
    if missing:
        state.update(ok=False, error="missing script(s): " + ", ".join(missing))
        state["running"] = False
        state["finished"] = datetime.now().isoformat(timespec="seconds")
        return
    # Cache: skip scripts already computed for this input fingerprint (and reset
    # everything when the fully-within switch changed, which requires new specs).
    fp = _analysis_fingerprint()
    cache = _load_analysis_cache()
    if (cache.get("fingerprint") != fp
            or bool(cache.get("fully_within")) != bool(include_fully_within)):
        cache = {"fingerprint": fp, "fully_within": bool(include_fully_within), "scripts": {}}
    done = cache.get("scripts", {})
    todo = wanted if force else [s for s in wanted if not done.get(s)]
    if not todo:
        emit("up to date (cached)\n")
        state.update(ok=True, cached=True)
        state["running"] = False
        state["finished"] = datetime.now().isoformat(timespec="seconds")
        return
    state["cached"] = False
    env = dict(os.environ)
    env["PYTHONPYCACHEPREFIX"] = os.path.join(S.TEMP, "pycache")
    env["PYTHONIOENCODING"] = "utf-8"
    # Fully-within studies: opt-in only.  The subprocess (re)builds specs/tables
    # under the switch, and the in-process views filter on the same flag.
    if include_fully_within:
        env["INCLUDE_FULLY_WITHIN"] = "1"
        emit("INCLUDE_FULLY_WITHIN=1 (fully-within studies included)\n")
    else:
        env.pop("INCLUDE_FULLY_WITHIN", None)
        emit("fully-within studies excluded (set the checkbox to include)\n")
    ok = True
    for script in todo:
        emit(f"$ {os.path.basename(script)}\n")
        cmd = [exe, "-X", "utf8", os.path.join(scripts_dir, script)]
        if force and script == "publication_bias.py":
            cmd.append("--force-specs")
        try:
            popen_kw = {"stdin": subprocess.DEVNULL}
            if os.name == "nt":
                # A windowed (console-less) parent must not pop a cmd window for
                # the analysis child process.
                popen_kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            proc = subprocess.Popen(cmd, cwd=S.ROOT, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                    errors="replace", bufsize=1, **popen_kw)
        except OSError as e:
            emit(f"! could not start: {e}\n")
            ok = False
            continue
        for line in proc.stdout:
            emit(line)
        rc = proc.wait()
        emit(f"[exit {rc}]\n\n")
        if rc != 0:
            ok = False
        else:
            done[script] = True
    cache["scripts"] = done
    cache["at"] = datetime.now().isoformat(timespec="seconds")
    _save_analysis_cache(cache)
    _invalidate_analysis_caches()
    state.update(ok=ok, error="" if ok else "one or more scripts exited non-zero")
    state["running"] = False
    state["finished"] = datetime.now().isoformat(timespec="seconds")
    log(f"analysis run kind={kind} ok={ok}")

def start_background_run(kind="essential", force=False, include_fully_within=None):
    """Start an analysis run in a daemon thread.

    Returns ``"running"`` when one is already in progress, ``"cached"`` when the
    requested scripts are already up to date for this input fingerprint and the
    same fully-within switch, and ``"started"`` when a new run was launched.
    Shared by the ``/run`` endpoint and the server-start autorun so there is one
    place that owns the lock.
    """
    include = _include_fully_within(include_fully_within)
    wanted = ANALYSIS_RUN_KINDS.get(kind, [])
    with _analysis_lock:
        if _analysis_run["running"]:
            return "running"
        if not force:
            # An output older than an input is authoritative: force a rebuild even
            # if the hash cache still claims the scripts are done (e.g. a table was
            # deleted, or another session touched an input).
            if _tables_stale_flag():
                force = True
            else:
                fp = _analysis_fingerprint()
                cache = _load_analysis_cache()
                fresh = (cache.get("fingerprint") == fp
                         and bool(cache.get("fully_within")) == include)
                done = cache.get("scripts", {}) if fresh else {}
                if all(done.get(s) for s in wanted):
                    now = datetime.now().isoformat(timespec="seconds")
                    _analysis_run.update(running=False, kind=kind, ok=True, cached=True,
                                         log="up to date (cached)\n", error="",
                                         started=now, finished=now,
                                         fully_within=include)
                    return "cached"
        _analysis_run.update(running=True, kind=kind, log="", ok=None, error="",
                             cached=False, fully_within=include,
                             started=datetime.now().isoformat(timespec="seconds"),
                             finished=None)
    threading.Thread(target=_analysis_worker, args=(kind, force, include), daemon=True).start()
    return "started"

def autorun_if_stale():
    """Regenerate stale analysis tables in the background (called at server start)."""
    if _tables_stale_flag():
        return start_background_run("essential")
    return "fresh"

def _specs_stale_flag():
    """True when either spec layer is stale (the shared ``suite_common`` check).

    H3 (audit): staleness is decided by ``suite_common._specs_stale`` for BOTH
    the meta and the SDT layer and includes the fully-within marker mismatch, so
    the freshness badge can no longer show green over a stale ``sdt_specs.csv``.
    Only reported while the analysis tables themselves are stale: once the tables
    have been regenerated the specs are necessarily current.
    """
    if not _tables_stale_flag():
        return False
    if _SC is None or not hasattr(_SC, "_specs_stale"):
        return False
    try:
        targets = [(os.path.join(S.ROOT, "temp", "meta_specs.csv"), "meta"),
                   (_sdt_spec_path(), "sdt")]
        for path, layer in targets:
            if not os.path.isfile(path):
                continue
            if _SC._specs_stale(path, layer):
                return True
            if _CT is not None and not _CT.fully_within_marker_matches(path):
                return True
        return False
    except Exception:
        return False

def _newest_data_input_mtime():
    """Newest mtime of the coded/harmonized inputs that drive the analysis outputs."""
    now = time.time()
    if now - _NEWEST_INPUT_CACHE["at"] < 10:
        return _NEWEST_INPUT_CACHE["mtime"]
    newest = 0.0
    for pat in (os.path.join(S.EXTRACTION, "*.json"),
                os.path.join(S.DATASETS or "", "*", "* - harmonized.csv"),
                os.path.join(S.DATASETS or "", "*", "* - harmonized_variables.csv")):
        for f in glob.glob(pat):
            try:
                newest = max(newest, os.path.getmtime(f))
            except OSError:
                pass
    _NEWEST_INPUT_CACHE.update(at=now, mtime=newest)
    return newest

def _tables_stale_flag():
    """True when the analysis tables are older than the newest data input.

    Only the *data* inputs (Extraction/Datasets) count — script edits by another
    session must not trigger an endless recompute loop.  Both spec layers (and
    the participant-level specs) are included, and a fully-within marker that no
    longer matches the current switch also counts as stale (audit H3/M4).
    """
    newest = _newest_data_input_mtime()
    if newest <= 0:
        return False
    temp = os.path.join(S.ROOT, "temp")
    outs = glob.glob(os.path.join(temp, "pb_*.csv"))
    for name in ("meta_specs.csv", "sdt_specs.csv", "sdt_specs_pb.csv",
                 "sdt_participant_specs.csv"):
        outs.append(os.path.join(temp, name))
    latest = max((os.path.getmtime(p) for p in outs if os.path.isfile(p)), default=0.0)
    if latest < newest:
        return True
    if _CT is not None:
        for name in ("meta_specs.csv", "sdt_specs.csv", "sdt_specs_pb.csv"):
            p = os.path.join(temp, name)
            if os.path.isfile(p) and not _CT.fully_within_marker_matches(p):
                return True
    return False

_APA_LAYER = {"meta": "meta-analysis of reported effects",
              # C1: the SDT layer's headline is the intervention-vs-control Δd′.
              "sdt": "signal-detection re-analysis (intervention effect on discrimination, Δd′)"}

_APA_SPEC = {
    "post_all": "immediate posttest (all outcomes)",
    "post_cred": "immediate posttest (credibility)",
    "did_all": "pretest-corrected difference-in-differences (all outcomes)",
    "did_cred": "pretest-corrected difference-in-differences (credibility)",
    "persistence_all": "follow-up persistence (all outcomes)",
    "persistence_cred": "follow-up persistence (credibility)",
    "didpost_all": "pretest-corrected where possible (all outcomes)",
    "didpost_cred": "pretest-corrected where possible (credibility)",
    "fupost_all": "follow-up where available (all outcomes)",
    "fupost_cred": "follow-up where available (credibility)",
    "fudidpost_all": "follow-up, else pretest-corrected (all outcomes)",
    "fudidpost_cred": "follow-up, else pretest-corrected (credibility)",
    # AUXILIARY arm-pooled discrimination LEVEL — not an intervention effect
    # (audit C1: the level must never be read as the effect).
    "discrimination_level_all": "AUXILIARY arm-pooled discrimination level (not the effect)",
    "discrimination_level_cred": "AUXILIARY arm-pooled discrimination level (credibility)",
    "discrimination_level_persistence_all":
        "AUXILIARY arm-pooled follow-up discrimination level (not the effect)",
    "discrimination_level_persistence_cred":
        "AUXILIARY arm-pooled follow-up discrimination level (credibility)",
}

#: The estimand of each SDT spec (audit C1): ``post_*``/``persistence_*`` are the
#: INTERVENTION-vs-CONTROL effect on discrimination (d'_treat - d'_control),
#: matching ``Results/reanalysis.xlsx`` sheet ``Discrimination``.
SDT_EFFECT_SPECS = {"post_all", "post_cred", "persistence_all", "persistence_cred",
                    "did_all", "did_cred"}
SDT_AUXILIARY_SPECS = {"discrimination_level_all", "discrimination_level_cred",
                       "discrimination_level_persistence_all",
                       "discrimination_level_persistence_cred"}

# Presentation-only labels: the backend keeps the raw variable values, the report
# and the app display the humanised labels.
_APA_METRIC = {"g": "Hedges' g", "d": "Cohen's d",
               "z": "Fisher z", "beta": "Regression coefficient", "yi": "Reported effect",
               "reported": "Reported effect",
               # C1: d′ = z(HR) − z(FAR); post/persistence are the
               # intervention-vs-control Δd′; the level is auxiliary.
               "d_sdt": "Discrimination (d′ = z(HR) − z(FAR))",
               "d_sdt_did": "Change in discrimination (Δd′)",
               "d_sdt_level": "Arm-pooled discrimination level (auxiliary)"}

def _apa_domain(v):
    d = str(v or "").strip()
    return _mod_label("dv_domain", d) or _human_mod_label(d)

def _apa_metric(v):
    m = str(v or "").strip()
    return _APA_METRIC.get(m, _human_mod_label(m))

def _apa_spec(v):
    s = str(v or "").strip()
    return _APA_SPEC.get(s, s)


# --------------------------------------------------------------------------- W6-AN
# Presentation guards added by workstream W6-AN (2026-09-23).  Neither changes
# an estimator: ``spec_estimand`` only labels what each pool IS, and
# ``bf_presentation`` only formats a Bayes factor that is numerically huge.

#: Layer-specific estimand labels for ``/api/analysis/pooled``.  Previously the
#: same ``SPEC_EFFECT_DEFINITION`` string ("... (SDT) / ... (meta) ...") was
#: returned for both layers, which is ambiguous.  The meta layer pools the
#: REPORTED standardised intervention effect on the coded DVs; the SDT layer
#: pools the re-analysis discrimination difference d' = z(HR) - z(FAR)
#: (stat_core.dprime), intervention minus control.
_LAYER_ESTIMAND = {
    "meta": ("meta-analysis of reported effects: standardised intervention-vs-control "
             "effect (Hedges g on the coded outcome DVs; higher = more desirable)"),
    "sdt": ("re-analysis (signal detection): intervention-vs-control difference in "
            "discrimination d\u2032 = z(HR) \u2212 z(FAR) (higher = more desirable)"),
}


def spec_estimand(layer, spec):
    """Layer-specific estimand label for one specification (W6-AN).

    Combines the layer's estimand definition with the spec's population label
    from ``_APA_SPEC``.  The SDT auxiliary arm-pooled discrimination LEVEL is
    explicitly flagged as not an intervention effect (audit C1).
    """
    lay = str(layer or "").strip()
    spc = str(spec or "").strip()
    if lay == "sdt" and spc in SDT_AUXILIARY_SPECS:
        base = ("AUXILIARY arm-pooled discrimination level (NOT an intervention "
                "effect): d\u2032 = z(HR) \u2212 z(FAR)")
        return base
    base = _LAYER_ESTIMAND.get(
        lay, "standardised intervention-vs-control effect (higher = more desirable)")
    pop = _APA_SPEC.get(spc, "")
    return f"{base} \u2014 {pop}" if pop else base


#: Above this magnitude a raw Bayes factor is numerically meaningless for a
#: reader (the normal-normal Savage-Dickey ratio over k~3000 contrast handily
#: reaches ~1e200).  The headline is capped; log10 BF is still reported.
BF_PRESENT_CAP = 1e6


def bf_presentation(bf, prior_note=""):
    """Guarded display of a Bayes factor (W6-AN; no estimator change).

    Returns ``{value, display, log10, capped, effectively_certain, note}``.  A raw
    value above ``BF_PRESENT_CAP`` (or a non-finite one) is reported as
    "> 1,000,000 (effectively certain)" with a note naming the prior and the
    numeric overflow, so the app never renders a bare 1e204.  The posterior
    mean and P(effect > 0) are the intended headline.
    """
    out = {"value": bf, "display": "\u2013", "log10": None, "capped": False,
           "effectively_certain": False, "note": prior_note or ""}
    try:
        b = float(bf)
    except (TypeError, ValueError):
        return out
    if not math.isfinite(b):
        out.update(display="> 1,000,000 (effectively certain)", capped=True,
                   effectively_certain=True,
                   note=" | ".join(filter(None, [
                       prior_note,
                       "the normal-normal Savage\u2013Dickey BF underflowed the "
                       "posterior density at zero (numeric overflow)"])))
        return out
    if b <= 0:
        return out
    out["log10"] = math.log10(b)
    if b >= BF_PRESENT_CAP:
        out.update(display="> 1,000,000 (effectively certain)", capped=True,
                   effectively_certain=True,
                   note=" | ".join(filter(None, [
                       prior_note,
                       f"raw BF \u2248 {b:.2e} exceeds the display cap"
                       + (" (numeric overflow)" if b >= 1e100 else "")
                       + f"; log\u2081\u2080 BF = {out['log10']:.1f}"])))
    elif b <= 1.0 / BF_PRESENT_CAP:
        out.update(display="< 1/1,000,000 (effectively certain for the null)", capped=True,
                   note=" | ".join(filter(None, [
                       prior_note,
                       f"raw BF \u2248 {b:.2e}; log\u2081\u2080 BF = {out['log10']:.1f}"])))
    else:
        out["display"] = f"{b:.3g}" if b >= 1000 else f"{b:.3f}"
    return out
