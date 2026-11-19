# -*- coding: utf-8 -*-
"""Gap-walk library: the guided harmonization queue for ONE study.

``GET /api/gapwalk/<key>`` (served by :mod:`routes_coverage`) builds, for one
study, the queue of open harmonization gaps the redesigned Harmonization tab
walks one card at a time.  Every card carries

* the **plain question** (the eleven slot codes S1..S11 stay the pipeline's
  vocabulary -- the UI keeps them in tooltips only),
* **evidence blocks** with a source tag: short quotes from the paper text
  (``Text/<Key>.txt``, searched server-side with per-slot terms, cached per
  key), a **pre-fill from the coding tab** (``Extraction/<Key>.json`` --
  domains, veracity hints, timepoints, polarity, arm roles) and the
  **harmonizer's own guess** (the shared decoders),
* the exact **write** the answer becomes in
  ``Datasets/<Key>/<Key> - variable_assignments.csv`` -- no new data
  contracts: every card answers through the same fields
  (:data:`variable_assignments.ASSIGN_COLS`) the apply/audit scripts read.

Everything here is read-only with respect to ``Datasets/``; the save is the
existing ``POST /api/assignments/<key>``.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
from collections import OrderedDict

from studio_state import S

# ---------------------------------------------------------------------------
# the eleven questions, in plain words (approved wording)
# ---------------------------------------------------------------------------
SLOT_ORDER = ["S1", "S2", "S3", "S4", "S5", "S6", "S6cell", "S7", "S8",
              "S9", "S10", "S11"]

#: code -> plain question, short label (chips), one-line hint (tooltips)
SLOT_QUESTIONS = {
    "S1": {
        "question": "One row per person - which column identifies a participant?",
        "short": "Participant ID",
        "hint": "The pooled analyses need one column that identifies a participant. "
                "Without it the study cannot join the pre-post (DiD) pool.",
    },
    "S2": {
        "question": "Which column says which group a person was in?",
        "short": "Group column",
        "hint": "One column with the study's arm codes (condition / condition_raw). "
                "Without it no arm can be split into intervention vs control.",
    },
    "S3": {
        "question": "Which group is the intervention, which the control?",
        "short": "Intervention vs control",
        "hint": "The arm-role map: which condition values are the intervention arm "
                "and which are the no-misinformation-intervention control.",
    },
    "S4": {
        "question": "Which columns are the outcome measures?",
        "short": "Outcome columns",
        "hint": "Every outcome (dv_*) needs a 0-1 rescaled twin (_01) so a 1-7 scale "
                "cannot dominate a 0-1 scale in the pooled tables.",
    },
    "S5": {
        "question": "What does each outcome measure (belief, credibility, sharing...)?",
        "short": "What it measures",
        "hint": "The DV domain: credibility, sharing intention, belief, trust... "
                "used to group effects per outcome family in the meta-analysis.",
    },
    "S6": {
        "question": "Which outcomes are about TRUE stories, which about FALSE ones?",
        "short": "True vs false",
        "hint": "Veracity of the stimuli the outcome is about. True+false outcomes "
                "are what the signal-detection (d') pool is built from.",
    },
    "S6cell": {
        "question": "The true-vs-false pair used for discernment - which two columns?",
        "short": "Discernment pair",
        "hint": "Discernment needs one true-side and one false-side outcome for the "
                "same measure. Labelling both columns' veracity creates the pair.",
    },
    "S7": {
        "question": "Was each outcome measured before, right after, or later?",
        "short": "When it was measured",
        "hint": "Timepoint: pre, post (right after) or followup (later). A blank "
                "timepoint is read as the immediate posttest by convention.",
    },
    "S8": {
        "question": "Is a higher number better or worse?",
        "short": "Direction",
        "hint": "Every DV is oriented so higher = more desirable before pooling; "
                "higher_is_worse outcomes are reverse-coded by reanalysis.py.",
    },
    "S9": {
        "question": "Which rows should be dropped (attention checks, failed checks)?",
        "short": "Rows to drop",
        "hint": "The authors' own exclusion rules over the flag_* columns "
                "(which checks gate which outcomes).",
    },
    "S10": {
        "question": "Which columns are covariates (age, politics...)?",
        "short": "Covariates",
        "hint": "Participant attributes (age, gender, political leaning...) the "
                "harmonized table carries as cov_* columns.",
    },
    "S11": {
        "question": "Where did each variable come from (source/derivation)?",
        "short": "Variable sources",
        "hint": "The provenance map (variables.csv): which raw column each "
                "harmonized variable was derived from.",
    },
}

# ---------------------------------------------------------------------------
# per-slot search terms for the paper-text quotes (Text/<Key>.txt)
# ---------------------------------------------------------------------------
SLOT_TERM_RE = {
    "S1": re.compile(r"participant\s+(id|identifier|number)|respondent\s+id|"
                     r"subject\s+id|worker\s+id|mturk\s+id|prolific\s+id|"
                     r"each\s+participant", re.I),
    "S2": re.compile(r"randomly\s+assigned|assigned\s+to\s+(one|the|a)|"
                     r"between-subjects\s+factor|treatment\s+group|"
                     r"experimental\s+condition|one\s+of\s+(four|three|two|the)", re.I),
    "S3": re.compile(r"control\s+(group|condition|treatment)|treatment\s+"
                     r"(group|condition)|intervention\s+(group|condition|arm)|"
                     r"baseline\s+condition|no-treatment", re.I),
    "S4": re.compile(r"dependent\s+variable|outcome\s+(measure|variable)|"
                     r"measured\s+with|scale\s+consisted|items\s+were|"
                     r"composite\s+(score|measure)", re.I),
    "S5": re.compile(r"credibilit|believabilit|sharing\s+intention|"
                     r"willingness\s+to\s+share|trust|accuracy\s+of|"
                     r"discernment|attitude", re.I),
    "S6": re.compile(r"true\s+(news|stories|headlines|items|statements|posts)|"
                     r"false\s+(news|stories|headlines|items|statements|posts)|"
                     r"fake\s+(news|stories|headlines|stories)|"
                     r"real\s+(news|headlines|stories)|fabricated|"
                     r"misinformation\s+(headlines|stories|posts|posted)|"
                     r"untrue|fact-checked\s+(stories|posts|claims)", re.I),
    "S6cell": re.compile(r"discern\w*|signal[\s-]detection|d\s?prime|d′|"
                          r"sensitivity|true\s+and\s+false|both\s+true|"
                          r"true-versus-false|true/false|"
                          r"distinguish\w*\s+(true|false|credible|real|mainstream)|"
                          r"discriminate\w*\s+(between|true|false)", re.I),
    "S7": re.compile(r"follow[\s-]?up|one\s+week\s+later|weeks?\s+later|"
                     r"immediately\s+after|post-?test|pre-?test|delayed|"
                     r"second\s+wave|after\s+\d+\s+(days|weeks)", re.I),
    "S8": re.compile(r"reverse[\s-]?cod|reverse[\s-]?scor|higher\s+(scores|"
                     r"values|numbers)|lower\s+(scores|values|numbers)|"
                     r"1\s*=\s*(not|strongly)|anchored", re.I),
    "S9": re.compile(r"attention\s+check|manipulation\s+check|excluded|"
                     r"removed\s+from\s+(the\s+)?(analysis|sample)|failed\s+(the|a|"
                     r"their)\s+check|did\s+not\s+pass|screened\s+out", re.I),
    "S10": re.compile(r"age|gender|political\s+(ideology|leaning|orientation|"
                      r"affiliation)|education\s+level|party\s+identification|"
                      r"participants\s+reported\s+their", re.I),
    "S11": re.compile(r"codebook|variable\s+names|open-?ended|derived\s+"
                      r"(variable|score|measure)|averaged\s+across|mean\s+of\s+"
                      r"(the\s+)?(three|four|two|three)", re.I),
}

#: per-slot answer kinds understood by the workspace
_SLOT_KIND = {
    "S1": "column", "S2": "column", "S3": "roles", "S4": "rename",
    "S5": "select", "S6": "select", "S6cell": "pair", "S7": "select",
    "S8": "select", "S9": "exclusions", "S10": "column", "S11": "note",
}

#: the assignment field a select-kind answer writes
SLOT_FIELD = {"S5": "dv_domain", "S6": "veracity", "S7": "timepoint",
              "S8": "dv_polarity"}

_CONTROL_GROUP_LABELS = {1: "No-treatment / passive control",
                         2: "Placebo (unrelated content)",
                         3: "Active control (alternative intervention)",
                         4: "Waitlist control",
                         5: "Misinformation-only control",
                         6: "Other / no clean control"}

_POSTTEST_LABELS = {1: "Immediate posttest", 2: "Delayed posttest",
                    3: "Immediate + delayed", 4: "Cross-sectional",
                    5: "Other"}

_VER_TRUE = re.compile(r"true\s+(news|stor|headlin|item|statement)|"
                       r"real\s+(news|headlin|stor)|accurate\s+(news|headlin)|"
                       r"true\s+items|true\s+stories", re.I)
_VER_FALSE = re.compile(r"false\s+(news|stor|headlin|item|statement)|"
                        r"fake\s+(news|headlin|stor)|fabricat|misinform|"
                        r"bogus|unverifiable|false\s+items|false\s+stories", re.I)
_COMPOSITE_RE = re.compile(r"discernment|differen|auc|d\s?prime|d′|"
                           r"signal[\s-]detection|composite|both\s+true", re.I)

_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")
_STOP = {"the", "and", "for", "with", "that", "this", "from", "were", "was",
         "are", "not", "all", "their", "each", "have", "has", "had", "its",
         "into", "one", "two", "three", "per", "how", "what", "which", "when",
         "your", "you", "they", "them", "his", "her", "our", "who", "will",
         "can", "may", "did", "does", "than", "then", "also", "only", "very",
         "over", "under", "between", "across", "during", "after", "before",
         "based", "using", "used", "scale", "item", "items", "variable",
         "variables", "column", "columns", "score", "scores", "mean",
         "measured", "measure", "measures", "participants", "participant",
         "study", "experiment", "condition", "conditions", "group", "sample",
         "data", "value", "values", "high", "low", "point", "points",
         "asked", "reported", "print", "printed", "table"}


def _s(v):
    """Clean string (None/NaN sentinels -> ""; bool -> true/false)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return ""
    try:
        import pandas as pd
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    return "" if s.lower() in ("", "nan", "none", "nat", "<na>") else s


def safe_study_key(key):
    """Mirror of ``routes_harmonizer.safe_study_key`` (same refusal rules)."""
    if not isinstance(key, str):
        return False
    k = key.strip()
    if not k or k != key or k in (".", ".."):
        return False
    if "/" in k or "\\" in k or ".." in k or "\x00" in k or os.path.isabs(k):
        return False
    return os.path.basename(k) == k


# ---------------------------------------------------------------------------
# paper text: cached per (path, mtime), searched with the slot terms
# ---------------------------------------------------------------------------
_TEXT_CACHE = OrderedDict()          # path -> (mtime, text, lower)
_TEXT_CACHE_MAX = 16


def _paper_text(key):
    path = os.path.join(S.TEXT or "", key + ".txt")
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return None, None
    hit = _TEXT_CACHE.get(path)
    if hit is not None and hit[0] == mt:
        _TEXT_CACHE.move_to_end(path)
        return hit[1], hit[2]
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None, None
    if len(_TEXT_CACHE) >= _TEXT_CACHE_MAX:
        _TEXT_CACHE.popitem(last=False)
    low = text.lower()
    _TEXT_CACHE[path] = (mt, text, low)
    return text, low


_REF_LINE = re.compile(r"^\s*(references|bibliography|literature cited)\s*$",
                       re.I | re.M)


def _ref_cutoff(text):
    """Character offset where the reference list starts (else len(text)).

    The LAST standalone 'References' heading is the cut, so quotes never
    come out of the bibliography.
    """
    cut = None
    for m in _REF_LINE.finditer(text):
        cut = m.start()
    return cut if cut is not None else len(text)


def _clean_window(text, a, b):
    """Sentence-ish window, trimmed to word boundaries, whitespace folded."""
    while a > 0 and text[a] not in ".!?\n":
        a -= 1
    a = a + 1 if a > 0 else a
    while b < len(text) and text[b] not in ".!?\n":
        b += 1
    w = re.sub(r"\s+", " ", text[a:b]).strip()
    w = w.strip(" .;,:")
    return w[:260]


def find_quotes(key, slot, extra_tokens=(), limit=3):
    """Best 1-3 short quotes from ``Text/<Key>.txt`` for one question.

    Searches the per-slot term list plus the target's own content words
    (the raw column / DV description), scores windows by how many distinct
    terms they cover, prefers windows that name the target, and skips the
    reference list.  Returns ``[]`` when the paper text is absent.
    """
    text, low = _paper_text(key)
    if not text:
        return []
    rx = SLOT_TERM_RE.get(slot)
    if rx is None:
        return []
    cutoff = _ref_cutoff(low)
    windows = []
    best_by_block = {}
    extras = [t for t in (str(x).lower() for x in extra_tokens) if len(t) >= 4]
    for m in rx.finditer(low, 0, cutoff):
        a, b = max(0, m.start() - 170), min(len(text), m.end() + 200)
        w = _clean_window(text, a, b)
        if len(w) < 40:
            continue
        wl = w.lower()
        n_terms = len({t for t in ("true", "false", "follow", "control",
                                    "excluded", "reverse", "credib") if t in wl})
        n_extra = sum(1 for t in extras if t in wl)
        # the target's own wording (up to 6 token hits) outweighs generic
        # slot wording (up to 3), so the item's sentence ranks first
        score = min(n_terms, 3) * 2 + min(n_extra, 6) * 2.5
        line = low.count("\n", 0, m.start()) + 1
        # best window per 240-char block: a later match in the same block can
        # still win when it scores higher (the item's own wording usually does)
        block = a // 240
        cur = best_by_block.get(block)
        if cur is None or score > cur["score"]:
            best_by_block[block] = {"score": score, "text": w, "line": line,
                                     "char": m.start(),
                                     "term": re.sub(r"\s+", " ", m.group(0))[:40]}
    windows = list(best_by_block.values())
    # a target's own words beat generic slot wording: best score, earliest first
    windows.sort(key=lambda d: (-d["score"], d["char"]))
    out, taken = [], set()
    for d in windows:
        if len(out) >= limit:
            break
        sig = d["text"][:80].lower()
        if sig in taken:
            continue
        taken.add(sig)
        out.append({"text": d["text"], "line": d["line"], "char": d["char"],
                    "term": d["term"], "page": None})
    return out


# ---------------------------------------------------------------------------
# cheap column profile (candidates for column-kind questions)
# ---------------------------------------------------------------------------
_PROFILE_CACHE = OrderedDict()     # path -> (mtime, columns list)
_PROFILE_CACHE_MAX = 12
_PROFILE_ROWS = 60


def _profile_from_csv(path, label, artifact):
    import pandas as pd
    hdr = []
    with open(path, encoding="utf-8-sig", newline="", errors="replace") as fh:
        rd = csv.reader(fh)
        for row in rd:
            hdr = [str(c).strip().lstrip("\ufeff") for c in row]
            break
    hdr = [c for c in hdr if c]
    if not hdr:
        return None
    df = pd.read_csv(path, encoding="utf-8-sig", nrows=_PROFILE_ROWS,
                     low_memory=False)
    out = []
    for c in hdr:
        if c not in df.columns:
            continue
        col = {"name": c, "artifact": artifact, "source": label,
               "numeric": False, "min": None, "max": None, "mean": None,
               "top": [], "missing": 0}
        try:
            s = df[c]
            col["missing"] = int(s.isna().sum())
            num = s.dropna() if len(s) else s
            try:
                num = num.astype(float)
                col["numeric"] = bool(len(num))
                if col["numeric"]:
                    col["min"] = round(float(num.min()), 4)
                    col["max"] = round(float(num.max()), 4)
                    col["mean"] = round(float(num.mean()), 4)
            except (TypeError, ValueError):
                col["numeric"] = False
            vc = s.dropna().astype(str).value_counts().head(3)
            col["top"] = [{"value": str(k)[:40], "n": int(v)} for k, v in vc.items()]
        except Exception:  # noqa: BLE001
            continue
        out.append(col)
    return out


def profile_columns(key, folder):
    """Cheap per-column profile of the study's main data file (first ~60
    rows): name, numeric min/max/mean, top values, missing count.  Prefers
    the prepared CSVs, then the harmonized wide, then SPSS/Stata metadata
    (header only).  Cached per file revision; ``None`` when nothing is
    readable."""
    cands = []
    for p in sorted(_glob(folder, key + " - prepared*.csv")):
        cands.append((p, "prepared", _artifact_of(os.path.basename(p), key)))
    harm = os.path.join(folder, f"{key} - harmonized.csv")
    if os.path.isfile(harm):
        cands.append((harm, "harmonized", "*"))
    best = None
    for path, label, artifact in cands:
        try:
            mt = os.path.getmtime(path)
        except OSError:
            continue
        hit = _PROFILE_CACHE.get(path)
        if hit is not None and hit[0] == mt:
            cols = hit[1]
            _PROFILE_CACHE.move_to_end(path)
        else:
            try:
                cols = _profile_from_csv(path, label, artifact)
            except Exception:  # noqa: BLE001
                cols = None
            if cols is None:
                continue
            if len(_PROFILE_CACHE) >= _PROFILE_CACHE_MAX:
                _PROFILE_CACHE.popitem(last=False)
            _PROFILE_CACHE[path] = (mt, cols)
        if cols:
            if best is None or label == "prepared":
                best = cols
            if label == "prepared":
                break
    if best is None:
        best = _profile_from_spss_headers(folder)
    return best or []


def _glob(folder, pattern):
    import glob as _g
    return _g.glob(os.path.join(folder, pattern))


def _artifact_of(base, key):
    m = re.search(r" - prepared (\d+|[A-Za-z0-9]+)\.csv$", base)
    return m.group(1) if m else "main"


def _profile_from_spss_headers(folder):
    """Column names + value labels from the SPSS/Stata file metadata only."""
    try:
        import pyreadstat  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    for p in sorted(list(_glob(folder, "*.sav")) + list(_glob(folder, "*.dta"))):
        reader = pyreadstat.read_dta if p.lower().endswith(".dta") else pyreadstat.read_sav
        try:
            _df, meta = reader(p, metadataonly=True)
        except Exception:  # noqa: BLE001
            continue
        out = []
        for i, nm in enumerate(meta.column_names or []):
            lab = ""
            try:
                lab = str((meta.column_names_to_labels or {}).get(nm) or "")[:120]
            except Exception:  # noqa: BLE001
                lab = ""
            out.append({"name": str(nm), "artifact": "*",
                        "source": os.path.basename(p),
                        "numeric": False, "min": None, "max": None, "mean": None,
                        "top": [{"value": lab, "n": 0}] if lab else [],
                        "missing": 0})
        if out:
            return out
    return None


# ---------------------------------------------------------------------------
# coding facts: the pre-fill source (Extraction/<Key>.json)
# ---------------------------------------------------------------------------
def _join(v):
    if isinstance(v, (list, tuple)):
        return "; ".join(str(x) for x in v if x not in (None, ""))
    return str(v or "")


def _tokens(s):
    return [t for t in _TOKEN_RE.findall(str(s or "").lower()) if t not in _STOP]


def coding_facts(key):
    """Records + a flat DV list from the coding tab, for pane (a) and the
    pre-fills.  ``[]`` / ``[]`` when the study is not coded."""
    from studio_lib import load_extraction
    records = load_extraction(key) or []
    facts, dvs = [], []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        rec_dvs = []
        for dv in (rec.get("DV_Metrics") or []):
            if not isinstance(dv, dict):
                continue
            flat = {
                "name": str(dv.get("dv_name") or "")[:160],
                "domain": _join(dv.get("dv_domain")),
                "score_types": _join(dv.get("score_types")),
                "timepoint_1": str(dv.get("timepoint_1") or ""),
                "timepoint_2": str(dv.get("timepoint_2") or ""),
                "polarity": dv.get("dv_polarity"),
                "posttest_type": dv.get("posttest_type"),
                "delay_weeks": dv.get("delay_weeks"),
                "group1": str(dv.get("group1_label") or ""),
                "group2": str(dv.get("group2_label") or ""),
                "eligible": dv.get("eligible_primary"),
                "discernment_as": str(dv.get("discernment_defined_as") or ""),
                "tokens": _tokens(dv.get("dv_name")),
            }
            rec_dvs.append(flat)
        dvs.extend(rec_dvs)
        cgt = rec.get("control_group_type")
        # the payload's DV rows stay small: the matcher's tokens/_match never
        # leave this module
        clean_dvs = [{k: v for k, v in d.items()
                      if k not in ("tokens", "_match")} for d in rec_dvs]
        facts.append({
            "study_id": str(rec.get("Study_ID") or ""),
            "title": str(rec.get("Title") or "")[:300],
            "authors": str(rec.get("Authors") or ""),
            "year": rec.get("Year") or "",
            "description": str(rec.get("Study_Description") or "")[:800],
            "n_analyzed": rec.get("N_Analyzed"),
            "n_recruited": rec.get("N_Recruited"),
            "n_arms": rec.get("n_arms"),
            "control_group_type": cgt,
            "control_group_label": _CONTROL_GROUP_LABELS.get(cgt, "") if isinstance(cgt, int) else "",
            "within_between": rec.get("within_between"),
            "intervention_types": _join(rec.get("Intervention_Type")),
            "setting": _join(rec.get("Setting")),
            "country": _join(rec.get("Country")),
            "covariates": _join(rec.get("Covariates")),
            "fully_within": bool(rec.get("Fully_Within")),
            "dvs": clean_dvs,
        })
    return facts, dvs


def match_dv(target_tokens, dvs):
    """Best-matching coded DV for a harmonized variable (token overlap)."""
    if not target_tokens or not dvs:
        return None, 0.0
    tt = set(target_tokens)
    best, best_score = None, 0.0
    for dv in dvs:
        dt = set(dv.get("tokens") or [])
        inter = tt & dt
        if not inter:
            continue
        score = len(inter) / max(3.0, min(len(tt), 12.0))
        if len(inter) >= 2 and score > best_score:
            best, best_score = dv, score
    if best is None:
        return None, 0.0
    return best, min(1.0, best_score)


def prefill_for(slot, uv, orig, notes, dv):
    """The coding tab's answer for one gap, or ``None``.

    Returns ``{"value", "detail", "confidence"}`` -- the value the card
    pre-selects, a plain sentence about where it came from, and how strong
    the DV match is."""
    if dv is None:
        return None
    name = dv["name"]
    strong = dv.get("_match", 0) or 0
    if slot == "S5":
        if not dv["domain"]:
            return None
        return {"value": dv["domain"].split(";")[0].strip(),
                "detail": f"coding tab: the DV \u201c{name}\u201d is coded "
                          f"dv_domain = {dv['domain']}",
                "confidence": "high" if strong >= 0.5 else "medium"}
    if slot == "S6":
        low = name.lower()
        if _COMPOSITE_RE.search(low) or "discernment" in dv["score_types"].lower():
            return None     # composite: that is the N/A question, not a prefill
        st = dv["score_types"].lower()
        if "separate_true" in st:
            val = "true"
        elif "separate_false" in st:
            val = "false"
        elif _VER_TRUE.search(low) and not _VER_FALSE.search(low):
            val = "true"
        elif _VER_FALSE.search(low) and not _VER_TRUE.search(low):
            val = "false"
        else:
            return None
        return {"value": val,
                "detail": f"coding tab: the DV \u201c{name}\u201d is scored "
                          f"({dv['score_types']}) and its name reads "
                          f"{'true' if val == 'true' else 'false'}-side",
                "confidence": "high" if strong >= 0.5 else "medium"}
    if slot == "S7":
        tp = (dv["timepoint_1"] or "").lower()
        if not tp:
            return None
        if "follow" in tp or "delay" in tp or "week" in tp or dv["timepoint_2"]:
            val = "followup"
        elif tp.startswith("pre"):
            val = "pre"
        else:
            val = "post"
        pt = _POSTTEST_LABELS.get(dv["posttest_type"], "")
        return {"value": val,
                "detail": f"coding tab: the DV \u201c{name}\u201d is coded "
                          f"timepoint_1 = {dv['timepoint_1']}"
                          + (f" ({pt})" if pt else ""),
                "confidence": "high" if strong >= 0.5 else "medium"}
    if slot == "S8":
        pol = dv["polarity"]
        if pol is None or str(pol) == "":
            return None
        val = "higher_is_better" if str(pol) in ("1", "1.0", "True", "true") \
            else "higher_is_worse"
        return {"value": val,
                "detail": f"coding tab: the DV \u201c{name}\u201d is coded "
                          f"dv_polarity = {pol} "
                          f"({'higher is better' if val == 'higher_is_better' else 'higher is worse'})",
                "confidence": "high" if strong >= 0.5 else "medium"}
    if slot == "S3":
        if not (dv["group1"] or dv["group2"]):
            return None
        return {"value": "",
                "detail": f"coding tab: the eligible contrast is "
                          f"\u201c{dv['group1']}\u201d (intervention) vs "
                          f"\u201c{dv['group2']}\u201d (comparison)",
                "confidence": "medium"}
    return None


# ---------------------------------------------------------------------------
# harmonizer guesses (the shared decoders, quiet imports)
# ---------------------------------------------------------------------------
def _scripts_dir():
    return os.path.join(S.ROOT or "", "Scripts")


def _ensure_scripts_path():
    d = _scripts_dir()
    if d and d not in sys.path:
        sys.path.insert(0, d)
    return d


class _Quiet:
    def __enter__(self):
        self._old, sys.stdout = sys.stdout, io.StringIO()
        return self

    def __exit__(self, *exc):
        sys.stdout = self._old
        return False


def _decoder(name):
    _ensure_scripts_path()
    try:
        with _Quiet():
            module = __import__(name)
        return module
    except Exception:  # noqa: BLE001  (guesses degrade to None)
        return None


_HT = _decoder("harmonizer_template")
_CT = _decoder("contract")
_DVP = _decoder("dv_polarity")


def harmonizer_guess(slot, uv, orig, notes, domain=""):
    """The harmonizer's own decoded guess for one gap (``None`` when the
    decoders are unavailable or silent)."""
    out = None
    if slot == "S5" and _HT is not None:
        try:
            v = _HT.domain_of(uv)
            if v:
                out = {"value": v, "detail": f"decoded from the uniform name "
                                              f"({uv}) by harmonizer_template.domain_of"}
        except Exception:  # noqa: BLE001
            pass
    elif slot in ("S6", "S6cell") and _CT is not None:
        try:
            v = _CT.dv_veracity(uv, None, notes or "")
            if v:
                via = "contract.dv_veracity"
                if _HT is not None:
                    try:
                        t = _HT.veracity_of(uv)
                        if t and t != v:
                            v = f"{v} (template decodes {t})"
                    except Exception:  # noqa: BLE001
                        pass
                out = {"value": v, "detail": f"decoded from the uniform name "
                                              f"({uv}) by {via}"}
        except Exception:  # noqa: BLE001
            pass
    elif slot == "S7" and _HT is not None:
        try:
            v = _HT.timepoint_of(uv)
            if v:
                out = {"value": v, "detail": "decoded from the uniform name by "
                                              "harmonizer_template.timepoint_of "
                                              "(a blank timepoint is the immediate "
                                              "posttest by convention)"}
        except Exception:  # noqa: BLE001
            pass
    elif slot == "S8" and _DVP is not None:
        try:
            v = _DVP.expected_polarity(domain or "", uv, uv, notes or "", orig or "")
            if v:
                out = {"value": v, "detail": "expected from the domain/veracity by "
                                              "dv_polarity.expected_polarity"}
        except Exception:  # noqa: BLE001
            pass
    return out


# ---------------------------------------------------------------------------
# sidecar reader (same file routes_harmonizer reads; kept local so this
# module never imports a route module)
# ---------------------------------------------------------------------------
def load_sidecar(key):
    folder = os.path.join(S.DATASETS or "", key)
    path = os.path.join(folder, f"{key} - harmonized_variables.csv")
    if not os.path.isfile(path):
        return []
    rows = []
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                uv = (row.get("uniform_variable") or "").strip()
                if not uv:
                    continue
                rows.append({
                    "uniform_variable": uv,
                    "role": (row.get("role") or "").strip(),
                    "dv_domain": (row.get("dv_domain") or "").strip(),
                    "veracity": (row.get("veracity") or "").strip(),
                    "timepoint": (row.get("timepoint") or "").strip(),
                    "dv_polarity": (row.get("dv_polarity") or "").strip(),
                    "original_variable": (row.get("original_variable") or "").strip(),
                    "artifact": (row.get("artifact") or "").strip() or "*",
                    "item_scope": (row.get("item_scope") or "").strip(),
                    "notes": (row.get("notes") or "").strip(),
                })
    except OSError:
        return []
    return rows


def load_wide_columns(key):
    folder = os.path.join(S.DATASETS or "", key)
    path = os.path.join(folder, f"{key} - harmonized.csv")
    try:
        with open(path, encoding="utf-8-sig", newline="", errors="replace") as fh:
            for row in csv.reader(fh):
                return [str(c).strip() for c in row if str(c).strip()]
    except OSError:
        return []
    return []


# ---------------------------------------------------------------------------
# pools: the plain pills and what still blocks each one
# ---------------------------------------------------------------------------
def pools_for(rec, open_rows):
    """The three pool pills for one study + what is still missing for each.

    ``rec`` is the coverage summary record (needs the blocks_* flags and the
    per-slot rollups); ``open_rows`` are the study's open matrix rows.
    """
    def _short_miss(rows, slots):
        out = []
        for r in rows:
            if r["slot"] in slots:
                q = SLOT_QUESTIONS[r["slot"]]
                label = r["target"] if r["scope"] == "variable" else q["short"]
                out.append(f"{q['short']}: {label}")
        return out[:6]

    blocks_sdt = int((rec or {}).get("blocks_sdt") or 0) if rec is not None else 1
    blocks_did = int((rec or {}).get("blocks_did") or 0) if rec is not None else 1
    blocks_excl = int((rec or {}).get("blocks_exclusions_on") or 0) if rec is not None else 0
    s9 = _s((rec or {}).get("S9")) if rec is not None else ""
    return {
        "sdt": {
            "joins": bool(rec is not None and not blocks_sdt),
            "label": "Joins the signal-detection pool",
            "define": "The intervention-vs-control effect on discrimination: "
                      "d' = z(hit rate) - z(false-alarm rate), pooled across studies.",
            "missing": [] if (rec is None or not blocks_sdt) else
                        _short_miss(open_rows, ("S3", "S4", "S6", "S6cell")),
        },
        "did": {
            "joins": bool(rec is not None and not blocks_did),
            "label": "Joins the pre-post pool",
            "define": "The pretest-to-posttest (difference-in-differences) effect, "
                      "pooled across studies that measured the outcome twice.",
            "missing": [] if (rec is None or not blocks_did) else
                        _short_miss(open_rows, ("S1", "S2", "S4")),
        },
        "excl": {
            "joins": s9 in ("filled", "confirmed", "na"),
            "state": ("applied" if s9 in ("filled", "confirmed")
                      else "none" if s9 == "na" else "rules-missing"),
            "label": "Author exclusions apply",
            "define": "The authors' own attention/comprehension checks over the "
                      "flag_* columns decide which rows each outcome keeps.",
            "missing": ["S9: exclusions.csv"] if blocks_excl else [],
        },
    }


# ---------------------------------------------------------------------------
# the gap walk itself
# ---------------------------------------------------------------------------
def _rollup_label(status):
    return {"filled": "done", "confirmed": "done", "na": "not applicable",
            "missing": "missing", "ambiguous": "needs a verdict",
            "partial": "partly missing"}.get(status, status or "?")


def _sub_for(slot, gap, side_row):
    """The per-target sentence under the question."""
    if gap["scope"] == "variable":
        uv = gap["target"]
        orig = (side_row or {}).get("original_variable", "")
        what = orig[:90] if orig else ""
        if what and what != uv:
            return f"outcome column {uv} (raw: {what})"
        return f"outcome column {uv}"
    return ""


def _guess_column(profile, rx):
    for col in (profile or []):
        if rx.search(col["name"].lower()):
            return col["name"]
    return ""


_ID_RE = re.compile(r"\b(pid|participant|respondent|subject|worker|prolific|"
                    r"mturk|session|response)?_?(id|no|number|key)\b|"
                    r"\bid\b|\bpid\b|participant_id|subject_id|worker_id", re.I)
_COND_RE = re.compile(r"cond|group|arm|treat|condition|wave|format|label_?"
                      r"?(type|design)|training", re.I)
_COV_RE = re.compile(r"age|gender|sex|educ|politic|party|ideolog|income|"
                     r"religio|nationalit|ethnic|race|urban|device|prefer", re.I)


def build_gapwalk(key, derived):
    """The full gap-walk payload for one study (see module docstring)."""
    folder = os.path.join(S.DATASETS or "", key)
    matrix = derived.get("matrix")
    summary = derived.get("summary")
    rec = None
    if summary is not None and len(summary):
        rows = summary[summary["study_key"].astype(str) == key]
        if len(rows):
            rec = {c: rows.iloc[0].get(c) for c in rows.columns}

    open_rows = list(derived.get("open_by_study", {}).get(key, []))
    side = load_sidecar(key)
    side_by_uv = {r["uniform_variable"]: r for r in side}
    profile = profile_columns(key, folder)
    wide_cols = load_wide_columns(key)
    facts, dvs = coding_facts(key)

    # answered cards: only rows a CODER decided (confirmed, or an explicit
    # N/A verdict with a coder stamp).  Machine statuses (filled, or a bare
    # 'na' like "no flag_* columns -> nothing to encode") are not answers.
    answered = []
    if matrix is not None and len(matrix):
        mr = matrix[(matrix["study_key"].astype(str) == key)]
        for _, r in mr.iterrows():
            st = _s(r.get("status"))
            coder = _s(r.get("coder"))
            if st == "confirmed" or (st == "na" and coder):
                slot = _s(r.get("slot"))
                answered.append({
                    "slot": slot,
                    "scope": _s(r.get("scope")),
                    "target": _s(r.get("target")),
                    "status": st,
                    "coder": coder,
                    "decided_at": _s(r.get("decided_at")),
                    "evidence": _s(r.get("evidence")),
                    "question": SLOT_QUESTIONS.get(slot, {}).get("question", slot),
                    "short": SLOT_QUESTIONS.get(slot, {}).get("short", slot),
                })
    answered.sort(key=lambda d: (d["slot"], d["target"]))

    # the queue: one card per open gap, heaviest poolability weight first
    # (the backend sort -- kept from the coverage pipeline)
    queue = []
    for gap in open_rows:
        card = _card(key, gap, side_by_uv, profile, dvs, wide_cols, open_rows)
        queue.append(card)

    checklist = []
    if rec is not None:
        for slot in SLOT_ORDER:
            q = SLOT_QUESTIONS[slot]
            try:
                miss = int(float(rec.get(slot + "_missing") or 0))
            except (TypeError, ValueError):
                miss = 0
            st = _s(rec.get(slot))
            checklist.append({"code": slot, "question": q["question"],
                              "short": q["short"], "hint": q["hint"],
                              "status": st or "?", "open": miss,
                              "state": _rollup_label(st)})

    notes = ""
    notes_path = os.path.join(S.EXTRACTION or "", key + ".md")
    if os.path.isfile(notes_path):
        try:
            with open(notes_path, encoding="utf-8", errors="replace") as fh:
                notes = fh.read()[:4000]
        except OSError:
            notes = ""

    from studio_lib import domain_vocab
    options = {
        "role": ["dv", "covariate", ""],
        "dv_domain": list(domain_vocab()) + [""],
        "veracity": ["true", "false", ""],
        "timepoint": ["pre", "post", "followup", ""],
        "dv_polarity": ["higher_is_better", "higher_is_worse", "ambiguous", ""],
    }

    return {
        "key": key,
        "exists": os.path.isdir(folder),
        "has_dataset": os.path.isdir(folder),
        "paper": {
            "has_text": os.path.isfile(os.path.join(S.TEXT or "", key + ".txt")),
            "has_pdf": os.path.isfile(os.path.join(S.PAPERS or "", key + ".pdf")),
        },
        "notes": notes,
        "facts": facts,
        "dvs": dvs and len(dvs) or 0,
        "pools": pools_for(rec, open_rows),
        "checklist": checklist,
        "queue": queue,
        "answered": answered,
        "counts": {"open": len(queue), "answered": len(answered)},
        "options": options,
        "profile": profile[:160],
        "wide_columns": wide_cols[:400],
    }


def _card(key, gap, side_by_uv, profile, dvs, wide_cols, open_rows):
    slot = gap["slot"]
    q = SLOT_QUESTIONS.get(slot, {"question": slot, "short": slot, "hint": ""})
    uv = gap["target"] if gap["scope"] == "variable" else ""
    side_row = side_by_uv.get(uv) if uv else None
    orig = (side_row or {}).get("original_variable", "")
    notes = (side_row or {}).get("notes", "")
    artifact = (side_row or {}).get("artifact", "*") or "*"
    item_scope = (side_row or {}).get("item_scope", "")

    toks = _tokens(orig + " " + uv)
    if slot in ("S5", "S6", "S7", "S8"):
        dv, match_score = match_dv(toks, dvs)
        if dv is not None:
            dv["_match"] = match_score
    elif slot == "S3":
        # the arm-role pre-fill comes from the coding's own description of the
        # eligible contrast, not from a name match
        dv = next((x for x in dvs if x.get("eligible") in (1, "1", True)
                   and (x.get("group1") or x.get("group2"))), None) or \
            next((x for x in dvs if x.get("group1") or x.get("group2")), None)
        match_score = 0.0
    else:
        dv, match_score = None, 0.0
    prefill = prefill_for(slot, uv, orig, notes, dv)
    guess = harmonizer_guess(slot, uv, orig, notes,
                             (side_row or {}).get("dv_domain", ""))
    if slot in ("S1", "S2", "S3", "S9") and not guess:
        # the machine status itself is the guess for study-scope slots
        guess = None

    quotes = find_quotes(key, slot, toks[:6] if toks else ())

    kind = _SLOT_KIND.get(slot, "note")
    answer = {"kind": kind, "field": SLOT_FIELD.get(slot, ""), "options": []}
    if kind == "select":
        answer["options"] = _options_for(slot)

    candidates = []
    guess_col = ""
    if kind == "column":
        rx = _ID_RE if slot == "S1" else (_COND_RE if slot == "S2" else _COV_RE)
        guess_col = _guess_column(profile, rx)
        candidates = [
            {"name": c["name"], "numeric": c["numeric"], "min": c["min"],
             "max": c["max"], "mean": c["mean"],
             "top": [t["value"] for t in c["top"]][:3],
             "missing": c["missing"], "source": c["source"],
             "guess": bool(guess_col and c["name"] == guess_col)}
            for c in (profile or [])
            if slot != "S1" or not c["name"].startswith("dv_")
        ][:120]
        answer["candidates"] = candidates
        if guess_col:
            answer["guess"] = guess_col
    elif kind == "pair":
        dv_cols = [{"name": r["uniform_variable"], "veracity": r["veracity"],
                    "domain": r["dv_domain"], "timepoint": r["timepoint"],
                    "original_variable": r["original_variable"],
                    "artifact": r["artifact"], "item_scope": r["item_scope"]}
                   for r in side_by_uv.values()
                   if str(r["uniform_variable"]).startswith("dv_")]
        answer["dv_columns"] = dv_cols[:200]
        answer["confirm_na_targets"] = [
            g["target"] for g in open_rows
            if g["slot"] == "S6" and g["status"] in ("missing", "ambiguous")
        ][:40]
        answer["na_label"] = "Confirm: this study truly has no true/false split"
        answer["na_hint"] = ("Confirming the absence records an N/A verdict for "
                             "every unlabelled composite outcome and removes the "
                             "study from this queue.")
    elif kind == "roles":
        # observed condition values for the role map.  The pipeline splits
        # arms on `condition` (falling back to condition_raw), so the role
        # map is over those codes.
        obs = []
        cm = _load_conditions(key)
        if cm:
            obs = cm
        else:
            obs = [{"code": t["value"], "arm": "", "role": ""}
                   for t in (_column_top(profile, "condition") or [])] or \
                [{"code": t["value"], "arm": "", "role": ""}
                 for t in (_column_top(profile, "condition_raw") or [])]
        answer["observed"] = obs[:24]
    elif kind == "exclusions":
        flags = [c for c in wide_cols if str(c).startswith("flag_")]
        answer["flags"] = [{"name": c,
                             "top": [t["value"] for t in
                                     (_column_top(profile, c) or [])][:2]}
                            for c in flags[:40]]
        answer["exclusion_rule_hint"] = ("A rule is: flag column -> keep which value "
                                         "-> which outcomes it gates")
    elif kind == "rename":
        answer["current"] = uv
        answer["twin"] = (uv + "_01") if uv else ""
        answer["hint"] = ("Rename this column if its uniform name is wrong; the "
                          "0-1 twin is regenerated when the harmonizer next runs. "
                          "Otherwise leave a note for the harmonizer owner.")

    # what the answer writes
    writes = _writes_sentence(slot, kind, uv)

    return {
        "id": f"{slot}|{gap['target']}|{gap['status']}",
        "slot": slot, "scope": gap["scope"], "target": gap["target"],
        "status": gap["status"], "evidence": gap.get("evidence", ""),
        "auto_detail": gap.get("auto_detail", ""),
        "question": q["question"], "short": q["short"], "hint": q["hint"],
        "sub": _sub_for(slot, gap, side_row),
        "kind": kind, "answer": answer,
        "writes": writes,
        "prefill": prefill,
        "harmonizer_guess": guess,
        "paper_quotes": quotes,
        "uniform_variable": uv,
        "original_variable": orig,
        "artifact": artifact,
        "item_scope": item_scope,
    }


def _options_for(slot):
    if slot == "S5":
        from studio_lib import domain_vocab
        return list(domain_vocab())
    if slot == "S6":
        return ["true", "false"]
    if slot == "S7":
        return ["pre", "post", "followup"]
    if slot == "S8":
        return ["higher_is_better", "higher_is_worse"]
    return []


def _column_top(profile, name):
    for c in (profile or []):
        if c["name"] == name:
            return c["top"]
    return None


def _load_conditions(key):
    """``<Key> - conditions.csv`` rows as {code, arm, role} dicts."""
    folder = os.path.join(S.DATASETS or "", key)
    path = os.path.join(folder, f"{key} - conditions.csv")
    if not os.path.isfile(path):
        return []
    out = []
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                code = (row.get("code") or "").strip()
                if not code:
                    continue
                out.append({"code": code, "arm": (row.get("arm") or "").strip(),
                            "role": (row.get("role") or "").strip(),
                            "label": (row.get("label") or "").strip()[:110]})
    except OSError:
        return []
    return out


def _writes_sentence(slot, kind, uv):
    if kind == "select":
        field = SLOT_FIELD.get(slot, "")
        return (f"writes {field} for this variable into the harmonized sidecar, "
                f"as a confirmed row in the study's variable_assignments.csv")
    if kind == "column":
        if slot == "S1":
            return ("maps the picked raw column to the uniform name pid (role id) "
                    "in the study's variable_assignments.csv; the harmonized wide "
                    "gains the participant key on the next harmonizer run")
        if slot == "S2":
            return ("maps the picked raw column to the uniform name condition "
                    "(role condition) in the study's variable_assignments.csv")
        return ("maps the picked raw column to a cov_* uniform name (role "
                "covariate) in the study's variable_assignments.csv")
    if kind == "pair":
        return ("labels the two picked columns' veracity (true / false) as two "
                "confirmed assignment rows - the pair is what the "
                "signal-detection pool needs")
    if kind == "roles":
        return ("records the role map as a confirmed decision row keyed on the "
                "study's condition column (item_scope S3); the conditions.csv "
                "role map itself is applied by the harmonizer owner")
    if kind == "exclusions":
        return ("records the drop rules as confirmed decision rows keyed on each "
                "flag_* column (item_scope S9); the exclusions.csv rules file "
                "itself is applied by the harmonizer owner")
    if kind == "rename":
        return ("renames the uniform variable (a confirmed assignment row) or "
                "leaves a note in the study's variable_assignments.csv")
    return ("records the decision as a confirmed row in the study's "
            "variable_assignments.csv")
