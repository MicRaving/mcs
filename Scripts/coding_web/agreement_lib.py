"""Agreement logic: raters, the per-study coding index, Gwet's AC, conflicts and settlements.

Full-text raters are the ORIGINAL coder files ``Codings/<Key> - <coder>.json``
(every human or model coder).  They are read and never written.
``Extraction/<Key>.json`` is the CONSENSUS coding: the analysis route and the
one file a settlement writes into.  It is deliberately not a rater: settling a
conflict changes it, and it is usually reconciled from the raters' files, so
scoring it would make the agreement circular and inflate it (on the reference
project: .91 with it counted as a rater, .82 without).  Only a project that has
fewer than two coder files in total (a fresh project: the Extraction plus one
coder) falls back to scoring the Extraction as the rater ``LLM/base`` - the
``agreement.extraction_role`` setting of the project manifest overrides this
(``auto`` | ``rater`` | ``consensus``).

Every Master decision - full-text and abstract - lands in ONE file,
``Codings/_settlements.json``, together with the value the consensus held
before, the raters' values at decision time and the rows the decision applies
to, so a settlement is auditable and can be undone.  A study only counts as
settled when the file says so; a consensus that happens to equal a rater's file
is not a settlement.

Nothing here names a project's fields or coders: field names are resolved
through the scheme's roles (``_dv_name_of``, ``no_majority_fields``) and the
coder that anchors a conflict case / carries the PDF source spans is derived
from the data (the coder with the most papers / the most spans) unless the
manifest sets ``agreement.anchor_rater`` / ``agreement.span_raters``.
"""

import copy
import csv
import glob
import io
import json
import os
import pickle
import re
import sys
import tempfile
import threading
import time
import uuid
import hashlib
from datetime import datetime

import project_context
from screening_lib import screening_index
from studio_lib import (_is_continuous, _is_object_field, _norm, _schema, _scheme_stamp,
                        _values, atomic_replace, safe_token)
from studio_state import S, log, paper_keys

# The canonical veracity / timepoint decoders live in the pipeline's
# stdlib-only ``contract`` module; this tab uses them instead of its own
# pseudo-decoders (audit L6 / interface §1).
try:
    import contract as CT  # noqa: E402
except Exception:  # pragma: no cover - source layout fallback
    for _d in (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
               os.path.join(S.ROOT, "Scripts")):
        if _d and _d not in sys.path:
            sys.path.insert(0, _d)
    import contract as CT  # noqa: E402


#: The abstract stage's AI screen (master_dataset.xlsx) - a rater there.
AGREEMENT_REFERENCE = "LLM/base"

#: ``Extraction/<Key>.json`` - the consensus coding, never a rater.
CONSENSUS = "Consensus"

_LABEL_MAX_LEVELS = 50

_AGREE_LOCK = threading.RLock()

#: Cached exclude-filtered DV alignments, keyed by (index signature, excluded raters).
_DV_ALIGN_CACHE = {}


# ---------------------------------------------------------------- project settings
# Everything that used to be a hard-coded coder / field name is resolved here.

def agreement_settings():
    """The manifest's ``agreement`` block (``{}`` when the project sets none).

    Keys (all optional): ``extraction_role`` (``auto`` | ``rater`` |
    ``consensus``), ``anchor_rater`` (the coder whose rows anchor a DV case id),
    ``span_raters`` (coders preferred for PDF source spans, in order) and
    ``no_majority_fields`` (field names a bulk "accept majority" must skip, in
    addition to the role-derived ones).
    """
    try:
        block = (project_context.manifest() or {}).get("agreement")
    except Exception:  # noqa: BLE001 - the manifest is optional
        block = None
    return block if isinstance(block, dict) else {}


def _settings_sig():
    return json.dumps(agreement_settings(), sort_keys=True, default=str)


_ROLE_NAMES = {"stamp": None, "map": {}, "pin": None}


def _role_names():
    """``{(kind, role): field name}`` of the active scheme (kind = experiment | dv).

    Resolved once per index build (``pin``): the per-row lookups of a build must
    not re-stat the scheme files hundreds of thousands of times.
    """
    if _ROLE_NAMES["pin"] is not None:
        return _ROLE_NAMES["pin"]
    schema = _schema()
    if _ROLE_NAMES["stamp"] is schema:
        return _ROLE_NAMES["map"]
    out = {}
    for kind, bucket in (("experiment", "common_fields"), ("dv", "dv_fields")):
        for f in schema.get(bucket) or []:
            role = f.get("role")
            if role and f.get("name"):
                out.setdefault((kind, role), f["name"])
    _ROLE_NAMES.update(stamp=schema, map=out)
    return out


def _dv_name_of(role, default):
    """The name of the DV field carrying ``role`` (``default`` = the v3.1 name)."""
    return _role_names().get(("dv", role)) or default


def _exp_name_of(role, default):
    return _role_names().get(("experiment", role)) or default


#: Roles a bulk "accept majority" must never decide (a fact or a judgement the
#: paper does not dictate - AGENTS.md "never majority-vote a fact or a judgement").
_NO_MAJORITY_ROLES = ("eligible_primary", "polarity", "fulltext_exclusion_criterion",
                      "n_arms", "control_group_type", "fully_within", "sample_n",
                      "n_recruited", "gender_ratio")

#: Used for a scheme whose fields carry no roles (the v3.1 names).
_NO_MAJORITY_LEGACY = ("eligible_primary", "dv_polarity", "Fulltext_Exclusion_Criterion",
                       "n_arms", "control_group_type", "Fully_Within", "N_Analyzed",
                       "N_Recruited", "Gender_Ratio")


def no_majority_fields():
    """Field names no majority may settle: role-derived + the manifest's extras."""
    names = set()
    roles = _role_names()
    for role in _NO_MAJORITY_ROLES:
        for kind in ("experiment", "dv"):
            if (kind, role) in roles:
                names.add(roles[(kind, role)])
    # a field the scheme carries under a v3.1 name but without a role
    schema = _schema()
    present = {f.get("name") for bucket in ("common_fields", "dv_fields")
               for f in (schema.get(bucket) or [])}
    names.update(n for n in _NO_MAJORITY_LEGACY if n in present)
    extra = agreement_settings().get("no_majority_fields")
    if isinstance(extra, (list, tuple)):
        names.update(str(x) for x in extra)
    return frozenset(names)


def _extraction_is_rater(coder_names):
    """Whether ``Extraction/<Key>.json`` is scored as the rater ``LLM/base``.

    ``consensus`` never, ``rater`` always, ``auto`` (default) only when the
    whole project has fewer than two coder files' coders - a fresh project with
    the Extraction and one coder still gets an agreement figure.
    """
    mode = str(agreement_settings().get("extraction_role") or "auto").lower()
    if mode == "rater":
        return True
    if mode == "consensus":
        return False
    return len(set(coder_names)) < 2


# ---------------------------------------------------------------- field definitions
def _agreement_field_defs():
    """Deterministic labels: <=49 closed levels (options or vocabulary)."""
    s = _schema()
    out = []
    for kind, flist in (("experiment", s.get("common_fields") or []),
                        ("dv", s.get("dv_fields") or [])):
        for f in flist:
            if not f.get("name") or f.get("derived") or _is_continuous(f) or _is_object_field(f):
                continue
            opts = f.get("options") or []
            vocab = f.get("vocab")
            if isinstance(vocab, dict):
                vocab = [str(x) for x in vocab.keys()]
            elif isinstance(vocab, list):
                vocab = [str(x) for x in vocab]
            else:
                vocab = None
            multi = (str(f.get("type") or "").lower() == "multiselect"
                     or str(f.get("editor") or "").lower() == "multiselect")
            if opts:
                levels = [_norm(o.get("value")) for o in opts]
                label_of = {_norm(o.get("value")): str(o.get("label") or _norm(o.get("value")))
                            for o in opts}
            elif vocab:
                levels = list(dict.fromkeys(vocab))
                label_of = {x: x for x in levels}
                multi = True
            else:
                continue
            if not levels or len(levels) >= _LABEL_MAX_LEVELS:
                continue
            numeric = all(re.fullmatch(r"-?\d+", l) for l in levels)
            levels = sorted(levels, key=lambda x: int(x)) if numeric else levels
            canon = {str(l): l for l in levels}
            canon.update({str(label_of.get(l, l)).lower(): l for l in levels})
            out.append({
                "name": f["name"], "label": f.get("label") or f["name"],
                "group": f.get("group") or "Other", "kind": kind, "multi": multi,
                "ordered": bool(numeric), "levels": levels, "labels": label_of, "canon": canon,
            })
    return out


def _canon_level(raw, field):
    if raw is None:
        return ""
    if isinstance(raw, bool):
        s = "true" if raw else "false"
    elif isinstance(raw, (int, float)):
        s = _norm(raw)
    else:
        s = str(raw).strip()
    if not s:
        return ""
    return field["canon"].get(s, field["canon"].get(s.lower(), s))


def _field_value(raw, field):
    """Canonical display value of one coded cell ('' when missing)."""
    if field["multi"]:
        vals = {_canon_level(v, field) for v in _values(raw)}
        vals.discard("")
        return " + ".join(sorted(vals)) if vals else ""
    return _canon_level(raw, field)


def gwet_ac(case_ratings, categories, weights=None):
    """Gwet's AC1 (weights=None) / AC2 (weights callable) with missing data.

    Follows Gwet (2008) and the irrCAC `gwet.ac1.dist` implementation: pa is the
    mean pairwise agreement over subjects with >=2 raters; pe uses pi_k = the
    mean over all rated subjects of the proportion of raters choosing category k.
    """
    n = len(case_ratings)
    q = len(categories)
    if n == 0 or q < 1:
        return None
    idx = {c: i for i, c in enumerate(categories)}
    agree = [[0] * q for _ in range(n)]
    ri = [0] * n
    for i, vals in enumerate(case_ratings):
        for v in vals:
            j = idx.get(v)
            if j is not None:
                agree[i][j] += 1
        ri[i] = sum(agree[i])
    n2 = sum(1 for r in ri if r >= 2)
    if n2 == 0:
        return None

    def w(a, b):
        return 1.0 if a == b else 0.0 if weights is None else weights(a, b)

    sum_q = []
    for i in range(n):
        s = 0.0
        for k in range(q):
            nk = agree[i][k]
            if not nk:
                continue
            for l in range(q):
                nl = agree[i][l]
                if nl:
                    s += w(k, l) * nk * nl
        sum_q.append(s - ri[i])
    pa = sum(sum_q[i] / (ri[i] * (ri[i] - 1)) for i in range(n) if ri[i] >= 2) / n2
    # irrCAC drops subjects nobody rated in a scored category (e.g. only an
    # out-of-vocabulary value): pi_k is the mean over RATED subjects.
    n1 = sum(1 for r in ri if r >= 1)
    pi = [sum((agree[i][k] / ri[i]) for i in range(n) if ri[i] >= 1) / n1 for k in range(q)]
    sum_w = sum(w(k, l) for k in range(q) for l in range(q))
    pe = (sum_w * sum(pi[k] * (1 - pi[k]) for k in range(q))) / (q * (q - 1)) if q >= 2 else 0.0
    coeff = (pa - pe) / (1 - pe) if pe < 1 else None
    return {"pa": pa, "pe": pe, "coeff": coeff, "n": n, "n2": n2, "q": q}


def _linear_weights(q):
    if q <= 1:
        return lambda a, b: 1.0
    return lambda a, b: 1.0 - abs(a - b) / (q - 1)


# ---------------------------------------------------------------- DV alignment
# DV-level fields are aligned by a canonical DV key, never by the position of the
# row in ``DV_Metrics``: two coders may list the same DVs in a different order or
# with different names, so ``(Study_ID, i)`` mis-pairs them.  Alignment is done
# within each (paper, experiment) block and only the closed-label DV fields are
# scored (dv_domain, veracity, timepoint bucket, contrast_type, dv_polarity,
# eligible_primary, score_types); free-text dv_name and every numeric metric are
# used for matching only and never enter an agreement coefficient.
#
#   stage 1  exact match on the canonical key WITHOUT dv_name;
#   stage 2  the remaining rows are paired greedily by dv_name token Jaccard
#            (>= DV_NAME_MATCH_THRESHOLD);
#   leftover rows are UNMATCHED, reported and excluded from the coefficients.
#
# A study's statistic is only shown when its alignment has at least
# DV_MIN_MATCHED paired DVs and DV_MIN_COVERAGE overlap.

DV_NAME_MATCH_THRESHOLD = 0.5

DV_MIN_MATCHED = 5

DV_MIN_COVERAGE = 0.6

# Tokens dropped from a dv_name before fingerprinting: scale annotations and
# timepoint/scale words that are already captured by the key itself.
DV_NAME_DROP = {
    "recoded", "recoding", "reverse", "reversed", "rev",
    "posttest", "post", "pretest", "pre", "followup", "follow", "up",
    "proportion", "proportional", "proportioncorrect",
}

_PSEUDO_DV_LEVELS = {
    "veracity": ["true", "false"],
    "timepoint": ["pre", "post", "followup"],
}

#: Scored DV labels that are derived from dv_name (not a separate JSON field):
#: a settlement on ``veracity`` is recorded in ``_settlements.json``; ``timepoint``
#: writes ``timepoint_1`` in the consensus DV row so the pipeline picks it up.
PSEUDO_DV_FIELDS = frozenset({"veracity"})


def _norm_dv_name(name):
    """Lower-case fingerprint of a dv_name with punctuation/scale noise removed."""
    s = str(name or "").lower().replace("\u2013", "-").replace("\u2014", "-")

    def _drop_annot(m):
        body = m.group(1)
        if re.search(r"\d", body) or "proportion" in body or "point" in body:
            return " "
        return " " + body + " "
    s = re.sub(r"\(([^)]*)\)", _drop_annot, s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    toks = [t for t in s.split() if t and t not in DV_NAME_DROP]
    return " ".join(toks)


def _name_tokens(norm_name):
    return frozenset(norm_name.split()) if norm_name else frozenset()


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    union = len(a | b)
    return (len(a & b) / union) if union else 0.0


def _veracity_token(name):
    """Veracity implied by a dv_name — canonical decoder (``contract.dv_veracity``).

    Thin adapter so the agreement tab cannot disagree with the pipeline about a
    stimulus's veracity (audit L6).  ``false alarm`` is neutralised the same way
    the old local decoder did.
    """
    s = str(name or "").replace("false alarm", " ").replace("falsealarm", " ")
    return CT.dv_veracity(s)


def _bucket_one(raw):
    """Canonical timepoint bucket (pre/post/followup) or '' (``contract``)."""
    b = CT.classify_timepoint(raw)
    return b if b in ("pre", "post", "followup") else ""


def _tp_names():
    return (_dv_name_of("timepoint_1", "timepoint_1"), _dv_name_of("timepoint_2", "timepoint_2"))


def _timepoint_bucket(tp1, tp2=None):
    """Canonical timepoint bucket (pre/post/followup) from timepoint_1/2."""
    bs = {b for b in (_bucket_one(tp1), _bucket_one(tp2)) if b}
    if not bs:
        return ""
    for b in ("followup", "post", "pre"):
        if b in bs:
            return b
    return ""


def _domains_tuple(raw):
    return tuple(sorted({str(v).strip() for v in _values(raw) if str(v).strip()}))


def canonical_dv_key(dv):
    """Stable identity of one DV row, WITHOUT the free-text dv_name."""
    return (
        _domains_tuple(dv.get(_dv_name_of("dv_domain", "dv_domain"))),
        _veracity_token(dv.get(_dv_name_of("dv_name", "dv_name"))),
        _timepoint_bucket(dv.get(_dv_name_of("timepoint_1", "timepoint_1")),
                          dv.get(_dv_name_of("timepoint_2", "timepoint_2"))),
        _norm(dv.get(_dv_name_of("contrast_type", "contrast_type"))),
        str(dv.get(_dv_name_of("intervention_id", "Intervention_ID")) or "").strip().lower(),
    )


def _pseudo_dv_field(name, label, levels):
    levels = list(levels)
    canon = {str(l): l for l in levels}
    canon.update({str(l).lower(): l for l in levels})
    return {"name": name, "label": label, "group": "DV identity", "kind": "dv",
            "multi": False, "ordered": False, "levels": levels,
            "labels": {l: l for l in levels}, "canon": canon, "derived": True}


def _dv_agreement_defs():
    """Closed-label DV fields that may be scored (never dv_name / numeric metrics)."""
    base = {f["name"]: f for f in _agreement_field_defs() if f["kind"] == "dv"}
    out = []
    # (role, v3.1 name): the scheme's own name for the role wins
    for role, legacy in (("dv_domain", "dv_domain"), ("veracity", "veracity"),
                         ("contrast_type", "contrast_type"), ("polarity", "dv_polarity"),
                         ("eligible_primary", "eligible_primary"),
                         ("score_types", "score_types"), ("timepoint", "timepoint")):
        if role in _PSEUDO_DV_LEVELS:
            # derived from dv_name / timepoint_1,2, not a field of the scheme
            out.append(_pseudo_dv_field(role, role.capitalize(), _PSEUDO_DV_LEVELS[role]))
            continue
        name = _dv_name_of(role, legacy)
        if name in base:
            out.append(base[name])
    return out


def settleable_field(name):
    """The field definition a conflict on ``name`` is settled against (or None)."""
    for f in _agreement_field_defs():
        if f["name"] == name:
            return f
    for f in _dv_agreement_defs():
        if f["name"] == name:
            return f
    return None


def _dv_field_value(dv, fdef):
    name = fdef["name"]
    if name == "veracity" and fdef.get("derived"):
        return _veracity_token(dv.get(_dv_name_of("dv_name", "dv_name")))
    if name == "timepoint" and fdef.get("derived"):
        return _timepoint_bucket(dv.get(_dv_name_of("timepoint_1", "timepoint_1")),
                                 dv.get(_dv_name_of("timepoint_2", "timepoint_2")))
    return _field_value(dv.get(name), fdef)


def _compact_span(sp):
    if not isinstance(sp, dict):
        return None
    if not (sp.get("text") or sp.get("rects")):
        return None
    return {"page": sp.get("page"), "quote": sp.get("text"), "rects": sp.get("rects") or []}


def _dv_row_info(rater, key, case_sid, rec_index, dv, dv_index, fdefs):
    dv_name = dv.get(_dv_name_of("dv_name", "dv_name"))
    norm_name = _norm_dv_name(dv_name)
    return {
        "rater": rater, "key": key, "study_id": case_sid,
        "block": (key, case_sid),
        "rec_index": rec_index, "dv_index": dv_index,
        "name": dv_name or "",
        "align_key": canonical_dv_key(dv),
        "name_norm": norm_name,
        "tokens": _name_tokens(norm_name),
        "values": {f["name"]: _dv_field_value(dv, f) for f in fdefs},
        "span": _compact_span(dv.get("source_span")),
    }


def _greedy_units(items, exact_key, threshold=0.0):
    """Greedy one-row-per-rater clustering.

    ``exact_key`` requires an identical canonical key (stage 1) and pairs
    duplicates by name similarity; otherwise rows join a unit only when their
    dv_name Jaccard to every current member is >= ``threshold`` (stage 2).
    """
    units = []
    order = sorted(items, key=lambda r: (str(r["rater"]).lower(), r["rec_index"], r["dv_index"]))
    for it in order:
        best, best_score = None, -1.0
        for unit in units:
            if it["rater"] in unit:
                continue
            sims, ok = [], True
            for member in unit.values():
                if exact_key and member["align_key"] != it["align_key"]:
                    ok = False
                    break
                s = _jaccard(member["tokens"], it["tokens"])
                if not exact_key and s < threshold:
                    ok = False
                    break
                sims.append(s)
            if not ok:
                continue
            score = (sum(sims) / len(sims)) if sims else 0.0
            if score > best_score:
                best, best_score = unit, score
        if best is not None:
            best[it["rater"]] = it
        else:
            units.append({it["rater"]: it})
    return units


def _align_dv_rows(rows):
    """Two-stage alignment; returns (matched_units, unmatched_rows)."""
    by_key = {}
    for row in rows:
        by_key.setdefault(row["align_key"], []).append(row)
    units, leftovers = [], []
    for group in by_key.values():
        for unit in _greedy_units(group, exact_key=True):
            if len(unit) >= 2:
                units.append(unit)
            else:
                leftovers.extend(unit.values())
    unmatched = []
    for unit in _greedy_units(leftovers, exact_key=False, threshold=DV_NAME_MATCH_THRESHOLD):
        if len(unit) >= 2:
            units.append(unit)
        else:
            unmatched.extend(unit.values())
    return units, unmatched


def _make_block(rows, label=None):
    if not rows:
        return None
    key, sid = rows[0]["block"]
    units, unmatched = _align_dv_rows(rows)
    matched = [u for u in units if len(u) >= 2]
    raters = {}
    for row in rows:
        d = raters.setdefault(row["rater"], {"rater": row["rater"], "n_dv": 0,
                                             "matched": 0, "unmatched": 0})
        d["n_dv"] += 1
    for unit in matched:
        for rater in unit:
            raters[rater]["matched"] += 1
    for row in unmatched:
        raters[row["rater"]]["unmatched"] += 1
    max_n = max((d["n_dv"] for d in raters.values()), default=0)
    matched_pairs = len(matched)
    return {
        "key": key, "study_id": label or sid, "case_sid": sid,
        "raters": sorted(raters.values(), key=lambda d: str(d["rater"]).lower()),
        "matched_pairs": matched_pairs, "unmatched": len(unmatched),
        "n_dv_total": len(rows),
        "coverage": (matched_pairs / max_n) if max_n else 0.0,
        "_units": matched, "_rows": list(rows),
    }


def _attach_consensus(units, crows):
    """Pair each matched unit with at most one consensus DV row.

    A consensus row joins the unit it resembles most (canonical key share plus
    dv_name similarity; an identical row at the same position in a rater's
    file - the consensus usually descends from the first-round coding - is a
    strong hint).  Greedy and one-to-one, so two units never write into the
    same consensus row.
    """
    cands = []
    for ui, unit in enumerate(units):
        members = list(unit.values())
        for ci, c in enumerate(crows):
            same_key = sum(1 for m in members if m["align_key"] == c["align_key"]) / len(members)
            sim = sum(_jaccard(m["tokens"], c["tokens"]) for m in members) / len(members)
            exact = any(m["name_norm"] == c["name_norm"] and m["rec_index"] == c["rec_index"]
                        and m["dv_index"] == c["dv_index"] for m in members)
            if not (exact or sim >= DV_NAME_MATCH_THRESHOLD or (same_key >= 0.5 and sim >= 0.25)):
                continue
            cands.append((sim + same_key + (1.0 if exact else 0.0), -ui, -ci, ui, ci))
    cands.sort(reverse=True)
    out, used = {}, set()
    for _score, _a, _b, ui, ci in cands:
        if ui in out or ci in used:
            continue
        out[ui] = crows[ci]
        used.add(ci)
    return out


def _dv_study_alignment(blocks):
    """Aggregate alignment per paper and apply the coverage gate."""
    studies = {}
    for block in blocks:
        s = studies.setdefault(block["key"], {"key": block["key"], "blocks": 0,
                                              "matched_pairs": 0, "unmatched": 0,
                                              "aligned_items": 0, "total_items": 0,
                                              "raters": {}})
        s["blocks"] += 1
        s["matched_pairs"] += block["matched_pairs"]
        s["unmatched"] += block["unmatched"]
        s["aligned_items"] += sum(len(u) for u in block["_units"])
        s["total_items"] += block["n_dv_total"]
        for r in block["raters"]:
            d = s["raters"].setdefault(r["rater"], {"rater": r["rater"], "n_dv": 0,
                                                    "matched": 0, "unmatched": 0})
            d["n_dv"] += r["n_dv"]
            d["matched"] += r["matched"]
            d["unmatched"] += r["unmatched"]
    out = []
    for s in studies.values():
        max_n = max((d["n_dv"] for d in s["raters"].values()), default=0)
        coverage = (s["matched_pairs"] / max_n) if max_n else 0.0
        out.append({
            "key": s["key"], "blocks": s["blocks"],
            "raters": sorted(s["raters"].values(), key=lambda d: str(d["rater"]).lower()),
            "matched_pairs": s["matched_pairs"], "unmatched": s["unmatched"],
            "coverage": coverage,
            "gate": bool(s["matched_pairs"] >= DV_MIN_MATCHED and coverage >= DV_MIN_COVERAGE),
        })
    out.sort(key=lambda s: (not s["gate"], -s["unmatched"], str(s["key"]).lower()))
    return out


def _dv_alignment_for(index, exclude=()):
    """Blocks + study rollup for a given rater-exclusion set (cached per index)."""
    exclude = {e for e in exclude if e}
    if not exclude:
        return index.get("dv_blocks") or [], index.get("dv_studies") or []
    ck = (index.get("sig"), tuple(sorted(exclude)))
    hit = _DV_ALIGN_CACHE.get(ck)
    if hit:
        return hit
    blocks = []
    for block in index.get("dv_blocks") or []:
        rows = [r for r in block["_rows"] if r["rater"] not in exclude]
        if len({r["rater"] for r in rows}) >= 2:
            nb = _make_block(rows, block["study_id"])
            if nb:
                blocks.append(nb)
    studies = _dv_study_alignment(blocks)
    if len(_DV_ALIGN_CACHE) > 16:
        _DV_ALIGN_CACHE.clear()
    _DV_ALIGN_CACHE[ck] = (blocks, studies)
    return blocks, studies


def dv_field_cases(block, field_name):
    """Case ratings (one list per matched unit) for one DV field in one block."""
    out = []
    for unit in block["_units"]:
        vals = [m["values"].get(field_name) or "" for m in unit.values()]
        vals = [v for v in vals if v]
        if vals:
            out.append(vals)
    return out


def pool_gwet(cases_by_study, categories, weights=None):
    """Gwet AC pooled across studies, weighted by matched pairs (n2)."""
    num = den = 0.0
    n_items = n_multi = 0
    per_study = {}
    for key, cases in cases_by_study.items():
        n_items += len(cases)
        res = gwet_ac(cases, categories, weights)
        if not res:
            continue
        n_multi += res["n2"]
        if res["coeff"] is not None:
            per_study[key] = res["coeff"]
            num += res["coeff"] * res["n2"]
            den += res["n2"]
    return {"coeff": (num / den) if den else None, "n_items": n_items,
            "n_multi": n_multi, "n2": n_multi, "n_studies": len(cases_by_study),
            "per_study": per_study}


def dv_field_categories(field, cases_by_study):
    observed = {v for cases in cases_by_study.values() for vals in cases for v in vals}
    if field["multi"]:
        return sorted(observed)
    return list(field["levels"]) + [v for v in sorted(observed) if v not in field["levels"]]


def dv_alignment_summary(blocks, studies):
    passing = [s for s in studies if s.get("gate")]
    aligned_items = sum(len(u) for b in blocks for u in b["_units"])
    total_items = sum(b["n_dv_total"] for b in blocks)
    return {
        "matched_pairs": sum(b["matched_pairs"] for b in blocks),
        "unmatched": sum(b["unmatched"] for b in blocks),
        "aligned_items": aligned_items,
        "total_items": total_items,
        "coverage": (aligned_items / total_items) if total_items else 0.0,
        "studies": len(studies),
        "studies_passing": len(passing),
        "min_matched": DV_MIN_MATCHED,
        "min_coverage": DV_MIN_COVERAGE,
        "by_study": studies,
        "by_block": [{k: v for k, v in b.items() if not k.startswith("_")}
                     for b in blocks],
    }


def insufficient_note(coverage, matched_pairs=None):
    if (matched_pairs is not None and matched_pairs < DV_MIN_MATCHED
            and coverage >= DV_MIN_COVERAGE):
        return f"insufficient matched DVs ({matched_pairs} of {DV_MIN_MATCHED} needed)"
    return f"insufficient DV overlap (coverage {round(100 * coverage)}%)"


# ---------------------------------------------------------------- corpus files
# One directory listing per folder per request.  The old loader globbed
# ``Codings/`` once per paper (826 x ~3,900 entries = 3.3 M names, ~26 s).

def _scan(folder):
    """{file name: (mtime_ns, size)} for the ``*.json`` files of one folder."""
    out = {}
    if not folder or not os.path.isdir(folder):
        return out
    try:
        with os.scandir(folder) as it:
            for e in it:
                if not e.name.endswith(".json"):
                    continue
                try:
                    if not e.is_file():
                        continue
                    st = e.stat()
                except OSError:
                    continue
                out[e.name] = (st.st_mtime_ns, st.st_size)
    except OSError as exc:
        log(f"agreement: cannot list {folder}: {exc!r}")
    return out


def _split_coder_name(base, keys):
    """``(key, coder)`` for ``'<Key> - <coder>'`` (the key must be a known paper)."""
    i = base.find(" - ")
    while i != -1:
        if base[:i] in keys:
            return base[:i], base[i + 3:]
        i = base.find(" - ", i + 3)
    return None


def is_rater_coder(coder):
    """Paper-coding rater files only: no harmonizer overrides, no store files."""
    c = str(coder or "").strip()
    return (bool(c) and not c.startswith("harmonizer") and c != CONSENSUS
            and c != AGREEMENT_REFERENCE and not c.startswith("_"))


def corpus_files():
    """{key: {"consensus": (path, sig) | None, "raters": {coder: (path, sig)}}}."""
    keys = set(paper_keys())
    out = {}
    for name, sig in _scan(S.EXTRACTION).items():
        k = name[:-5]
        if k in keys:
            out.setdefault(k, {"consensus": None, "raters": {}})["consensus"] = (
                os.path.join(S.EXTRACTION, name), sig)
    for name, sig in _scan(S.CODINGS).items():
        if name.startswith("_"):
            continue
        sp = _split_coder_name(name[:-5], keys)
        if not sp or not is_rater_coder(sp[1]):
            continue
        out.setdefault(sp[0], {"consensus": None, "raters": {}})["raters"][sp[1]] = (
            os.path.join(S.CODINGS, name), sig)
    return out


def _read_records(path):
    """The record list of one coding file (non-dict entries kept for positions)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        log(f"agreement: cannot read {path}: {exc!r}")
        return None
    if isinstance(data, dict):
        return [data]
    return data if isinstance(data, list) else None


def norm_sid(sid):
    """Spelling-insensitive experiment id (``Amar2026_Exp1`` == ``AMAR2026_Exp1``)."""
    return re.sub(r"[^a-z0-9]", "", str(sid or "").lower())


def _own_sids(recs):
    return [(norm_sid(r.get("Study_ID")) if isinstance(r, dict) else "") or f"#{i}"
            for i, r in enumerate(recs)]


def _case_sids(recs, ref):
    """Map a rater's records onto the reference experiment ids.

    The routes spell the same experiment differently, so an exact ``Study_ID``
    match alone skips the comparison for hundreds of keys: match the
    spelling-insensitive id first, then fall back to the position when both
    files code the same number of experiments.
    """
    own = _own_sids(recs)
    if not ref:
        return own
    refset = set(ref)
    out = [None] * len(recs)
    used = set()
    for i, s in enumerate(own):
        if s in refset and s not in used:
            out[i] = s
            used.add(s)
    if len(recs) == len(ref):
        for i in range(len(recs)):
            if out[i] is None and ref[i] not in used:
                out[i] = ref[i]
                used.add(ref[i])
    return [o if o is not None else own[i] for i, o in enumerate(out)]


#: The coder whose rows anchor DV case ids (set per index build, see
#: :func:`_agreement_index`): ``agreement.anchor_rater`` of the manifest, else the
#: first-round coder = the one who coded the most papers.
_ANCHOR = {"rater": None}


def _anchor_of(raters):
    a = _ANCHOR["rater"]
    if a and a in raters:
        return a
    return sorted(raters, key=lambda r: str(r).lower())[0] if raters else None


def _project_anchor(files):
    """The configured anchor coder, else the coder with the most papers."""
    cfg = str(agreement_settings().get("anchor_rater") or "").strip()
    if cfg:
        return cfg
    counts = {}
    for f in files.values():
        for coder in f["raters"]:
            counts[coder] = counts.get(coder, 0) + 1
    if not counts:
        return None
    return sorted(counts, key=lambda c: (-counts[c], str(c).lower()))[0]


def _span_order(recs_by):
    """Coders in order of preference for a PDF source span.

    The manifest's ``agreement.span_raters`` first; then the coders that carry
    spans, the one with the most ``source_spans`` entries first.
    """
    cfg = agreement_settings().get("span_raters")
    first = [c for c in (cfg if isinstance(cfg, (list, tuple)) else []) if c in recs_by]

    def n_spans(c):
        return sum(len(r.get("source_spans") or {}) for r in recs_by[c] if isinstance(r, dict))
    rest = sorted((c for c in recs_by if c not in first),
                  key=lambda c: (-n_spans(c), str(c).lower()))
    return first + rest


# ---------------------------------------------------------------- per-study index
def _derive_key(key, files, fields, dv_defs, ext_rater=False):
    """Everything the agreement tab needs about one paper (cached per file signature)."""
    recs_by = {}
    for coder, (path, _sig) in sorted(files["raters"].items()):
        recs = _read_records(path)
        if recs:
            recs_by[coder] = recs
    cons = _read_records(files["consensus"][0]) if files.get("consensus") else None
    if ext_rater and cons:
        # a project without a second coder: the Extraction is the other rater
        recs_by.setdefault(AGREEMENT_REFERENCE, cons)
    if cons:
        ref = _own_sids(cons)
    else:
        anchor = _anchor_of(recs_by)
        ref = _own_sids(recs_by[anchor]) if anchor else []
    sidmaps = {c: _case_sids(r, ref) for c, r in recs_by.items()}
    labels = {}
    for i, rec in enumerate(cons or []):
        if isinstance(rec, dict):
            labels.setdefault(ref[i], str(rec.get("Study_ID") or "").strip() or f"#{i + 1}")
    for c, recs in recs_by.items():
        for i, rec in enumerate(recs):
            if isinstance(rec, dict):
                labels.setdefault(sidmaps[c][i],
                                  str(rec.get("Study_ID") or "").strip() or f"#{i + 1}")

    exp_fields = [f for f in fields if f["kind"] == "experiment"]
    pos_dv_fields = [f for f in fields if f["kind"] == "dv"]
    exp = {}        # field -> {case_sid: {rater: value}}
    pos = {}        # rater -> {field: {case: value}}  (positional index; APA export)
    rec_spans = {}  # (case_sid, field) -> span from a span-carrying route
    rows = []
    span_order = _span_order(recs_by)
    for coder in span_order:
        for i, rec in enumerate(recs_by.get(coder) or []):
            if not isinstance(rec, dict):
                continue
            for fname, sp in (rec.get("source_spans") or {}).items():
                csp = _compact_span(sp)
                if csp:
                    rec_spans.setdefault((sidmaps[coder][i], fname), dict(csp, coder=coder))
    for coder, recs in recs_by.items():
        rpos = {}
        for ri, rec in enumerate(recs):
            if not isinstance(rec, dict):
                continue
            csid = sidmaps[coder][ri]
            for f in exp_fields:
                v = _field_value(rec.get(f["name"]), f)
                if v:
                    exp.setdefault(f["name"], {}).setdefault(csid, {})[coder] = v
                    rpos.setdefault(f["name"], {})[(csid,)] = v
            for di, dv in enumerate(rec.get("DV_Metrics") or []):
                if not isinstance(dv, dict):
                    continue
                for f in pos_dv_fields:
                    v = _field_value(dv.get(f["name"]), f)
                    if v:
                        rpos.setdefault(f["name"], {})[(csid, di)] = v
                rows.append(_dv_row_info(coder, key, csid, ri, dv, di, dv_defs))
        if rpos:
            pos[coder] = rpos

    cons_exp = {}
    crows_by = {}
    for i, rec in enumerate(cons or []):
        if not isinstance(rec, dict):
            continue
        csid = ref[i]
        if csid in cons_exp:
            continue
        cons_exp[csid] = {"rec_index": i,
                          "values": {f["name"]: _field_value(rec.get(f["name"]), f)
                                     for f in exp_fields}}
        for di, dv in enumerate(rec.get("DV_Metrics") or []):
            if isinstance(dv, dict):
                crows_by.setdefault(csid, []).append(
                    _dv_row_info(CONSENSUS, key, csid, i, dv, di, dv_defs))

    by_block = {}
    for row in rows:
        by_block.setdefault(row["study_id"], []).append(row)
    blocks = []
    for csid in sorted(by_block, key=lambda s: str(labels.get(s, s)).lower()):
        block = _make_block(by_block[csid], labels.get(csid, csid))
        if not block:
            continue
        block["_cons"] = _attach_consensus(block["_units"], crows_by.get(csid) or [])
        blocks.append(block)
    return {
        "key": key, "raters": sorted(recs_by), "has_consensus": bool(cons),
        "n_records": {c: len(r) for c, r in recs_by.items()},
        "labels": labels, "exp": exp, "pos": pos, "rec_spans": rec_spans,
        "cons_exp": cons_exp, "blocks": blocks, "span_order": span_order,
    }


_INDEX = {"sig": None, "data": None}

#: {key: (file signature, derived)} - rebuilt per paper when one of its files changes.
_KEY_CACHE = {}

_CACHE_META = {"root": None, "schema": None, "disk_loaded": False}

#: Bump when the derived structure changes, so a stale pickle is never trusted.
_CACHE_VERSION = 5

_REL_CACHE = {}


def _schema_stamp():
    """Changes whenever the active project's scheme (fields, roles) changes."""
    try:
        return (_scheme_stamp(), _settings_sig())
    except Exception:  # noqa: BLE001
        return None


def _disk_cache_path():
    """The pickle lives in the USER's cache folder, never inside the project.

    A pickle is code: a project folder received from somebody else must not be
    able to plant one that is loaded when the project is opened.
    """
    if not S.ROOT:
        return None
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") \
        or os.path.join(os.path.expanduser("~"), ".cache")
    tag = hashlib.sha1(os.path.normcase(os.path.abspath(S.ROOT)).encode("utf-8")).hexdigest()[:16]
    return os.path.join(base, "CodingStudio", "cache", f"agreement_index_{tag}.pkl")


def _load_disk_cache():
    """Seed the per-paper cache from the last run (a first build takes ~20 s)."""
    _CACHE_META["disk_loaded"] = True
    path = _disk_cache_path()
    if not path or not os.path.exists(path):
        return
    try:
        with open(path, "rb") as fh:
            blob = pickle.load(fh)
    except Exception as exc:  # noqa: BLE001 - a broken cache is only a slow start
        log(f"agreement: ignoring unreadable index cache: {exc!r}")
        return
    if (isinstance(blob, dict) and blob.get("version") == _CACHE_VERSION
            and blob.get("root") == S.ROOT and blob.get("schema") == _CACHE_META["schema"]):
        _KEY_CACHE.update(blob.get("keys") or {})


def _save_disk_cache():
    path = _disk_cache_path()
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".pkl", dir=os.path.dirname(path))
        with os.fdopen(fd, "wb") as fh:
            pickle.dump({"version": _CACHE_VERSION, "root": S.ROOT,
                         "schema": _CACHE_META["schema"], "keys": dict(_KEY_CACHE)},
                        fh, protocol=pickle.HIGHEST_PROTOCOL)
        atomic_replace(tmp, path)
    except Exception as exc:  # noqa: BLE001
        log(f"agreement: could not save the index cache: {exc!r}")


def _key_sig(files, mode):
    cons = files.get("consensus")
    return (cons[1] if cons else None,
            tuple(sorted((c, s) for c, (_p, s) in files["raters"].items())), mode)


def _agreement_index():
    """The corpus-wide agreement index; only papers whose files changed are rebuilt.

    Shape (kept for the APA export): ``fields``, ``raters`` =
    ``{rater: {key: {field: {case: value}}}}``, ``dv_blocks``, ``dv_studies``,
    ``rater_names``, ``rater_summary``; plus ``by_key`` (per-paper data) and
    ``sig`` (changes whenever any coding file changes).
    """
    with _AGREE_LOCK:
        schema = _schema_stamp()
        if _CACHE_META["root"] != S.ROOT or _CACHE_META["schema"] != schema:
            _KEY_CACHE.clear()
            _DV_ALIGN_CACHE.clear()
            _INDEX.update(sig=None, data=None)
            _CACHE_META.update(root=S.ROOT, schema=schema, disk_loaded=False)
        files = corpus_files()
        ext_rater = _extraction_is_rater(c for f in files.values() for c in f["raters"])
        _ANCHOR["rater"] = _project_anchor(files)
        mode = (bool(ext_rater), _ANCHOR["rater"])
        sigs = {k: _key_sig(f, mode) for k, f in files.items()}
        gsig = hash((S.ROOT, schema, tuple(sorted(sigs.items()))))
        if _INDEX["sig"] == gsig and _INDEX["data"] is not None:
            return _INDEX["data"]
        if not _CACHE_META["disk_loaded"]:
            _load_disk_cache()
        fields = _agreement_field_defs()
        dv_defs = _dv_agreement_defs()
        rebuilt = 0
        t0 = time.time()
        _ROLE_NAMES["pin"] = None
        _ROLE_NAMES["pin"] = _role_names()
        try:
            for k, sig in sigs.items():
                hit = _KEY_CACHE.get(k)
                if hit is None or hit[0] != sig:
                    _KEY_CACHE[k] = (sig, _derive_key(k, files[k], fields, dv_defs, ext_rater))
                    rebuilt += 1
        finally:
            _ROLE_NAMES["pin"] = None
        for k in [k for k in _KEY_CACHE if k not in sigs]:
            del _KEY_CACHE[k]
        by_key = {k: _KEY_CACHE[k][1] for k in sorted(sigs, key=str.lower)}
        raters_idx, blocks, summary = {}, [], {}
        for k, d in by_key.items():
            for coder, fv in d["pos"].items():
                raters_idx.setdefault(coder, {})[k] = fv
            for coder, n in d["n_records"].items():
                s = summary.setdefault(coder, {"rater": coder, "keys": 0, "experiments": 0})
                s["keys"] += 1
                s["experiments"] += n
            blocks.extend(d["blocks"])
        data = {"fields": fields, "raters": raters_idx, "dv_blocks": blocks,
                "dv_studies": _dv_study_alignment(blocks),
                "rater_names": sorted(summary, key=str.lower),
                "rater_summary": [summary[r] for r in sorted(summary, key=str.lower)],
                "by_key": by_key, "sig": gsig, "ext_rater": bool(ext_rater),
                "anchor": _ANCHOR["rater"]}
        _INDEX.update(sig=gsig, data=data)
        _DV_ALIGN_CACHE.clear()
        _REL_CACHE.clear()
        if rebuilt:
            log(f"agreement: index rebuilt for {rebuilt} paper(s) in {time.time() - t0:.1f}s")
        if rebuilt >= 20:
            _save_disk_cache()
        return data


def _invalidate_agreement(_old=None, _new=None):
    """Force a re-scan on the next request (file signatures pick up what changed)."""
    with _AGREE_LOCK:
        _INDEX.update(sig=None, data=None)
        _DV_ALIGN_CACHE.clear()
        _REL_CACHE.clear()


def _field_case_map(index, field_name, exclude=()):
    """{key: {case_tuple: {rater: value}}} across raters (excluding names)."""
    out = {}
    for rater, by_key in index.items():
        if rater in exclude:
            continue
        for key, fvals in by_key.items():
            cases = fvals.get(field_name)
            if not cases:
                continue
            dest = out.setdefault(key, {})
            for case, val in cases.items():
                dest.setdefault(case, {})[rater] = val
    return out


def reliability_rows(data=None, exclude=()):
    """Per-field Gwet AC1/AC2 rows and the pooled overview.

    The single implementation behind the Agreement tab and the APA export:
    experiment fields pair raters by experiment, DV fields by the canonical
    two-stage alignment (never by row position).
    """
    data = data or _agreement_index()
    exclude = {e for e in exclude if e}
    ck = (data.get("sig"), tuple(sorted(exclude)))
    hit = _REL_CACHE.get(ck)
    if hit is not None:
        return hit
    rows = []
    wsum1 = wsum2 = acc1 = acc2 = 0.0

    def _add_row(f, ac1, ac2, extra):
        nonlocal acc1, acc2, wsum1, wsum2
        if ac1 and ac1.get("coeff") is not None:
            acc1 += ac1["coeff"] * ac1.get("n2", 0)
            wsum1 += ac1.get("n2", 0)
        if ac2 and ac2.get("coeff") is not None:
            acc2 += ac2["coeff"] * ac2.get("n2", 0)
            wsum2 += ac2.get("n2", 0)
        row = {
            "name": f["name"], "label": f["label"], "group": f["group"], "kind": f["kind"],
            "multi": f["multi"], "ordered": f["ordered"],
            "n_levels": len(f["levels"]), "n_items": 0, "n_multi": 0, "n_raters": 0,
            "ac1": ac1["coeff"] if ac1 else None, "ac1_pa": ac1.get("pa") if ac1 else None,
            "ac1_pe": ac1.get("pe") if ac1 else None,
            "ac2": ac2["coeff"] if ac2 else None, "ac2_pa": ac2.get("pa") if ac2 else None,
            "ac2_pe": ac2.get("pe") if ac2 else None,
            "matched_pairs": None, "unmatched": None, "coverage": None,
            "insufficient": False, "note": None,
        }
        row.update(extra)
        rows.append(row)

    # ---- experiment-level labels (case = one experiment, position-independent)
    for f in data["fields"]:
        if f["kind"] != "experiment":
            continue
        cmap = _field_case_map(data["raters"], f["name"], exclude)
        observed, case_ratings, raters_seen = set(), [], set()
        for vals in (v for cases in cmap.values() for v in cases.values()):
            observed.update(vals.values())
            raters_seen.update(vals.keys())
            case_ratings.append(list(vals.values()))
        if f["multi"]:
            cats = sorted(observed)
        else:
            cats = list(f["levels"]) + [v for v in sorted(observed) if v not in f["levels"]]
        ac1 = ac2 = None
        if 2 <= len(cats) < _LABEL_MAX_LEVELS and any(len(v) >= 2 for v in case_ratings):
            ac1 = gwet_ac(case_ratings, cats)
            if f["ordered"] and all(re.fullmatch(r"-?\d+", c) for c in cats):
                ac2 = gwet_ac(case_ratings, cats, _linear_weights(len(cats)))
        _add_row(f, ac1, ac2, {"n_levels": len(cats), "n_items": len(case_ratings),
                               "n_multi": sum(1 for v in case_ratings if len(v) >= 2),
                               "n_raters": len(raters_seen)})

    # ---- DV-level labels (canonical two-stage alignment, closed labels only)
    blocks, studies = _dv_alignment_for(data, exclude)
    summary = dv_alignment_summary(blocks, studies)
    study_pass = [s for s in studies if s.get("gate")]
    passing_keys = {s["key"] for s in study_pass}
    passing_blocks = [b for b in blocks if b["key"] in passing_keys]
    if passing_blocks:
        pass_summary = dv_alignment_summary(passing_blocks, study_pass)
        pooled_cov = pass_summary["coverage"]
        pooled_unmatched = pass_summary["unmatched"]
    else:
        pooled_cov = summary["coverage"]
        pooled_unmatched = summary["unmatched"]
    n_dv_raters = len({r["rater"] for b in passing_blocks for r in b["raters"]})
    for f in _dv_agreement_defs():
        cases_by_study = {}
        for block in passing_blocks:
            cases = dv_field_cases(block, f["name"])
            if cases:
                cases_by_study.setdefault(block["key"], []).extend(cases)
        cats = dv_field_categories(f, cases_by_study)
        res1 = res2 = None
        if 2 <= len(cats) < _LABEL_MAX_LEVELS and cases_by_study:
            pooled = pool_gwet(cases_by_study, cats)
            if pooled["coeff"] is not None:
                res1 = pooled
            if f["ordered"] and all(re.fullmatch(r"-?\d+", c) for c in cats):
                pooled2 = pool_gwet(cases_by_study, cats, _linear_weights(len(cats)))
                if pooled2["coeff"] is not None:
                    res2 = pooled2
        matched_pairs = res1["n_multi"] if res1 else 0
        insufficient = bool(matched_pairs < DV_MIN_MATCHED or pooled_cov < DV_MIN_COVERAGE)
        note = insufficient_note(pooled_cov, matched_pairs) if insufficient else None
        n_items = res1["n_items"] if res1 else sum(len(c) for c in cases_by_study.values())
        if insufficient:
            res1 = res2 = None
        _add_row(f, res1, res2, {
            "n_levels": len(cats), "n_items": n_items, "n_multi": matched_pairs,
            "n_raters": n_dv_raters, "matched_pairs": matched_pairs,
            "unmatched": pooled_unmatched, "coverage": pooled_cov,
            "insufficient": insufficient, "note": note,
        })

    overall = {
        "ac1": (acc1 / wsum1) if wsum1 else None,
        "ac2": (acc2 / wsum2) if wsum2 else None,
        "n_fields": len([r for r in rows if r["ac1"] is not None]),
        "n_items_total": sum(r["n_multi"] for r in rows),
    }
    if len(_REL_CACHE) > 16:
        _REL_CACHE.clear()
    _REL_CACHE[ck] = (rows, overall, summary)
    return rows, overall, summary


# ---------------------------------------------------------------- abstract agreement
# Inter-rater agreement and conflict resolution for the abstract-screening stage,
# over the C1–C4 verdicts in Screening/<coder>.json.

_ABSTRACT_FIELDS = [("C1", "Unrelated topic"), ("C2", "No control group"),
                    ("C3", "Not in English"), ("C4", "No primary experimental study")]

_ABSTRACT_CATS = ["Yes", "No", "Unsure"]


def abstract_fields():
    """The abstract criteria that are scored for agreement.

    The project's scheme wins, so a criterion added in the Scheme editor is
    scored too; the hardcoded list is the fallback for a project that carries
    no criteria of its own.
    """
    try:
        from screening_lib import screening_criteria
        criteria = screening_criteria()
    except Exception as exc:  # noqa: BLE001
        log(f"agreement: screening criteria unavailable: {exc!r}")
        criteria = []
    if criteria:
        return [(c["key"], c.get("title") or c["key"]) for c in criteria]
    return list(_ABSTRACT_FIELDS)


def _abstract_agreement_index():
    """{coder: {uid: {C1..C4: verdict}}} from Screening/<coder>.json."""
    out = {}
    if not S.SCREENING or not os.path.isdir(S.SCREENING):
        return out
    # The criteria are normalised from the scheme on every abstract_fields()
    # call: read them ONCE here, never inside the 30k-record loops below (that
    # made the abstract agreement take minutes).
    labels = [lab for lab, _ in abstract_fields()]
    from screening_lib import ai_entry, load_screening_decisions
    try:
        idx = screening_index()
    except Exception:  # noqa: BLE001
        idx = {}
    # a decision file may key a duplicate-accession record by its row
    # ("row-30348") while another uses "X#2": compare them as ONE record
    alias = (idx.get("info") or {}).get("alias") or {}
    # "yes" / "YES " and "Yes" are one category (the workbook and hand edits
    # differ in case); anything else is kept as written
    cats = {c.lower(): c for c in _ABSTRACT_CATS}

    def canon(v):
        v = str(v).strip()
        return cats.get(v.lower(), v)
    for p in sorted(glob.glob(os.path.join(S.SCREENING, "*.json"))):
        coder = os.path.splitext(os.path.basename(p))[0]
        try:
            dec = load_screening_decisions(coder)    # cached by file stamp
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(dec, dict):
            continue
        by_uid = {}
        for uid, rec in dec.items():
            crits = (rec or {}).get("criteria") or {}
            vals = {}
            for lab in labels:
                v = (crits.get(lab) or {}).get("verdict")
                if v and str(v).strip():
                    vals[lab] = canon(v)
            if vals:
                by_uid[alias.get(str(uid), str(uid))] = vals
        if by_uid:
            out[coder] = by_uid
    # The original LLM abstract codings: the master workbook's C1–C4 verdicts
    # (the AI screening reference) are included as a rater.
    llm = {}
    for rec in (idx.get("rows") or []):
        ai = rec.get("ai") or {}
        vals = {}
        for lab in labels:
            v = str(ai_entry(ai, lab).get("verdict") or "").strip()
            if v:
                vals[lab] = canon(v)
        if vals:
            llm[str(rec.get("id"))] = vals
    if llm:
        out.setdefault(AGREEMENT_REFERENCE, llm)
    return out

def _abstract_case_maps(index):
    """{label: {uid: {rater: verdict}}}."""
    maps = {lab: {} for lab, _ in abstract_fields()}
    for rater, by_uid in index.items():
        for uid, vals in by_uid.items():
            for lab, v in vals.items():
                if lab in maps:
                    maps[lab].setdefault(uid, {})[rater] = v
    return maps


def _rater_sort_key(rater, reference=AGREEMENT_REFERENCE):
    """Raters alphabetically, the abstract stage's LLM/base reference last."""
    return (str(rater) == reference, str(rater).lower())


# ---------------------------------------------------------------- conflicts
#: Judgements the paper does not dictate: `no_majority_fields()` (role-derived,
#: see above) - a bulk "accept majority" skips them; a Master settles each.
NO_MAJORITY_FIELDS = no_majority_fields   # callable: the set depends on the scheme


def majority_value(values):
    """The value a strict majority of >= 2 raters carries, else ''."""
    counts = {}
    for v in values.values():
        if v:
            counts[v] = counts.get(v, 0) + 1
    if not counts:
        return ""
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    top, n = ranked[0]
    if n >= 2 and (len(ranked) == 1 or ranked[1][1] < n):
        return top
    return ""


def study_conflicts(d, field_names=None, kind=None):
    """Every rater disagreement of one paper (consensus value and target attached).

    Experiment cases are keyed by the spelling-insensitive experiment id
    (``exp:<sid>``); DV cases by the aligned unit's anchor row
    (``dv:<rater>:<record>:<row>``) and also carry every member row, so a
    settlement still finds its case when the anchor changes.
    """
    wanted = set(field_names) if field_names else None
    out = []
    key = d["key"]
    if kind in (None, "", "experiment"):
        for f in _agreement_field_defs():
            if f["kind"] != "experiment" or (wanted and f["name"] not in wanted):
                continue
            for csid, vals in (d["exp"].get(f["name"]) or {}).items():
                if len(set(vals.values())) < 2:
                    continue
                ce = d["cons_exp"].get(csid)
                ordered = {r: vals[r] for r in sorted(vals, key=_rater_sort_key)}
                out.append({
                    "key": key, "study_id": d["labels"].get(csid, csid), "case_sid": csid,
                    "case_id": "exp:" + csid, "field": f["name"], "fdef": f,
                    "kind": "experiment", "values": ordered, "majority": majority_value(ordered),
                    "consensus_value": (ce["values"].get(f["name"]) or None) if ce else None,
                    "target": {"rec_index": ce["rec_index"]} if ce else None,
                    "members": None, "dv_name": None, "dv_names": None,
                    "span": d["rec_spans"].get((csid, f["name"])),
                })
    if kind in (None, "", "dv"):
        defs = [f for f in _dv_agreement_defs() if not wanted or f["name"] in wanted]
        for block in d["blocks"]:
            for ui, unit in enumerate(block["_units"]):
                anchor = _anchor_of(unit)
                a = unit[anchor]
                case_id = f"dv:{anchor}:{a['rec_index']}:{a['dv_index']}"
                crow = block["_cons"].get(ui)
                raters = sorted(unit, key=_rater_sort_key)
                span = None
                for r in d["span_order"] + raters:
                    sp = unit.get(r, {}).get("span") if r in unit else None
                    if sp:
                        span = dict(sp, coder=r)
                        break
                members = {r: [unit[r]["rec_index"], unit[r]["dv_index"]] for r in raters}
                names = {r: unit[r]["name"] for r in raters}
                for f in defs:
                    vals = {r: unit[r]["values"].get(f["name"]) for r in raters
                            if unit[r]["values"].get(f["name"])}
                    if len(set(vals.values())) < 2:
                        continue
                    out.append({
                        "key": key, "study_id": block["study_id"], "case_sid": block["case_sid"],
                        "case_id": case_id, "field": f["name"], "fdef": f, "kind": "dv",
                        "values": vals, "majority": majority_value(vals),
                        "consensus_value": (crow["values"].get(f["name"]) or None) if crow else None,
                        "target": ({"rec_index": crow["rec_index"], "dv_index": crow["dv_index"],
                                    "dv_name": crow["name"]} if crow else None),
                        "members": members, "dv_name": (crow or a)["name"], "dv_names": names,
                        "span": span,
                    })
    return out


# ---------------------------------------------------------------- settlements
# ONE file, ``Codings/_settlements.json``, holds every Master decision:
#   settlements  the active decisions (one per scope/study/case/field)
#   history      every settle / clear, append-only
#   studies      per-study status ("complete" = the Master has finished it)
# A full-text settlement is ALSO written into the consensus coding
# (``Extraction/<Key>.json``); the raters' files are never touched.

SETTLEMENTS_NAME = "_settlements.json"

_LEGACY_DIRNAME = "_resolutions"
_LEGACY_LOG = "_settled.jsonl"

_STORE_LOCK = threading.RLock()
_STORE_CACHE = {"sig": None, "path": None, "data": None}


def settlements_path():
    return os.path.join(S.CODINGS, SETTLEMENTS_NAME)


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _empty_store():
    return {"schema": 1, "description": (
        "Every Master settlement of a coder conflict (full text and abstract). "
        "Raters' files are never modified; full-text settlements are applied to "
        "the consensus coding Extraction/<Key>.json."),
        "updated_at": None, "settlements": [], "history": [], "studies": {},
        "migrated_from": []}


def _legacy_entries():
    """Decisions in the pre-2026-10-25 stores (_resolutions/<master>.json + _settled.jsonl)."""
    stamps = {}
    log_path = os.path.join(S.CODINGS, _LEGACY_LOG)
    if os.path.exists(log_path):
        try:
            with open(log_path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    stamps[(e.get("master"), e.get("study_key"), e.get("case_id"),
                            e.get("field"))] = e.get("timestamp")
        except OSError as exc:
            log(f"agreement: cannot read {log_path}: {exc!r}")
    out, sources = [], []
    folder = os.path.join(S.CODINGS, _LEGACY_DIRNAME)
    if os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".json"):
                continue
            master = name[:-5]
            path = os.path.join(folder, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                mtime = datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")
            except (OSError, ValueError) as exc:
                log(f"agreement: cannot migrate {path}: {exc!r}")
                continue
            sources.append(f"{_LEGACY_DIRNAME}/{name}")
            for key, cases in (data or {}).items():
                for case_id, fields in (cases or {}).items():
                    target, cid = None, case_id
                    m = re.fullmatch(r"dv:(\d+):(\d+)", case_id)
                    if m:
                        # Legacy DV ids are positions in the consensus coding.
                        target = {"rec_index": int(m.group(1)), "dv_index": int(m.group(2))}
                    elif case_id.startswith("exp:"):
                        cid = "exp:" + norm_sid(case_id[4:])
                    for field, value in (fields or {}).items():
                        out.append({
                            "id": uuid.uuid4().hex, "scope": "fulltext", "study_key": key,
                            "case_id": cid, "legacy_case_id": case_id, "field": field,
                            "kind": "dv" if case_id.startswith("dv:") else "experiment",
                            "value": " + ".join(value) if isinstance(value, list) else str(value),
                            "written": value, "previous_value": None,
                            "previous_value_known": False, "members": None,
                            "target": target, "rater_values": None,
                            "master": master,
                            "decided_at": stamps.get((master, key, case_id, field)) or mtime,
                            "applied": False,
                            "apply_note": ("migrated: the value was written to the master's own "
                                           f"coding file Codings/{key} - {master}.json, not to the "
                                           "consensus; settle again to apply it"),
                            "note": "", "source": f"migrated from {_LEGACY_DIRNAME}/{name}",
                        })
    if os.path.exists(log_path):
        sources.append(_LEGACY_LOG)
    return out, sources


def load_settlements():
    """The settlement store (a deep copy is NOT made - treat as read-only)."""
    path = settlements_path()
    with _STORE_LOCK:
        try:
            st = os.stat(path)
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        if _STORE_CACHE["path"] == path and _STORE_CACHE["sig"] == sig and _STORE_CACHE["data"]:
            return _STORE_CACHE["data"]
        data = None
        if sig is not None:
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError) as exc:
                # Never silently start over on top of a store we cannot read.
                raise RuntimeError(f"cannot read {path}: {exc}") from exc
        if not isinstance(data, dict):
            data = _empty_store()
            entries, sources = _legacy_entries()
            if entries or sources:
                data["settlements"] = entries
                data["history"] = [dict(e, action="settle") for e in entries]
                data["migrated_from"] = sources
                _write_store(data)
                log(f"agreement: migrated {len(entries)} legacy settlement(s) from {sources}")
                return load_settlements()
        for k, v in _empty_store().items():
            data.setdefault(k, v)
        _STORE_CACHE.update(path=path, sig=sig, data=data)
        return data


def _write_store(data):
    path = settlements_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data["updated_at"] = _now()
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=S.TEMP)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        atomic_replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    _STORE_CACHE.update(path=None, sig=None, data=None)


def update_settlements(mutate):
    """Load, mutate and save the store under one lock; returns ``mutate``'s result."""
    with _STORE_LOCK:
        data = copy.deepcopy(load_settlements())
        result = mutate(data)
        _write_store(data)
        return result


class SettlementIndex:
    """Find the active settlement of a conflict case.

    By case id first; then by any shared member row (the anchor of an aligned
    DV unit can change when a rater file is added); then, for migrated
    decisions that only know a consensus position, by that position.
    """

    def __init__(self, store, scope="fulltext"):
        self.by_case, self.by_member, self.by_target = {}, {}, {}
        for e in store.get("settlements") or []:
            if e.get("scope", "fulltext") != scope:
                continue
            k, f = e.get("study_key"), e.get("field")
            self.by_case[(k, e.get("case_id"), f)] = e
            for rater, m in (e.get("members") or {}).items():
                self.by_member[(k, f, rater, tuple(m))] = e
            t = e.get("target")
            if t and not e.get("members") and "dv_index" in t:
                self.by_target[(k, f, t.get("rec_index"), t.get("dv_index"))] = e

    def find(self, case):
        k, f = case["key"], case["field"]
        hit = self.by_case.get((k, case["case_id"], f))
        if hit:
            return hit
        for rater, m in (case.get("members") or {}).items():
            hit = self.by_member.get((k, f, rater, tuple(m)))
            if hit:
                return hit
        t = case.get("target")
        if t and "dv_index" in t:
            return self.by_target.get((k, f, t["rec_index"], t["dv_index"]))
        return None


def masters():
    try:
        store = load_settlements()
    except RuntimeError:
        return []
    return sorted({e.get("master") for e in store.get("settlements") or [] if e.get("master")},
                  key=str.lower)


def coerce_value(value, fdef):
    """A submitted value as stored in a coding file (list / int / str / None)."""
    if fdef["multi"]:
        parts = [p.strip() for p in value.split("+")] if isinstance(value, str) else _values(value)
        clean = []
        for v in parts:
            c = _canon_level(v, fdef)
            if c and c not in clean:
                clean.append(c)
        return clean or None
    if value is None or str(value).strip() == "":
        return None
    out = _canon_level(value, fdef) or None
    if fdef["ordered"] and out is not None:
        try:
            out = int(out)
        except (TypeError, ValueError):
            pass
    return out


def _load_consensus_for_write(key):
    path = os.path.join(S.EXTRACTION, f"{key}.json")
    if not os.path.exists(path):
        return path, None, None
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    recs = [raw] if isinstance(raw, dict) else raw
    return path, raw, recs if isinstance(recs, list) else None


def _write_consensus(path, raw):
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=S.TEMP)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        atomic_replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


class SettleError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _cell(recs, case):
    """(container dict, current raw value) of a case's target in the consensus."""
    t = case.get("target")
    if not t:
        return None, None
    ri = t["rec_index"]
    if not (0 <= ri < len(recs)) or not isinstance(recs[ri], dict):
        raise SettleError("the consensus coding changed; reload the conflicts", 409)
    rec = recs[ri]
    if case["kind"] == "experiment":
        return rec, rec.get(case["field"])
    dvs = rec.get("DV_Metrics") or []
    di = t["dv_index"]
    if not (0 <= di < len(dvs)) or not isinstance(dvs[di], dict) \
            or _norm_dv_name(dvs[di].get("dv_name")) != _norm_dv_name(t.get("dv_name")):
        raise SettleError("the consensus DV row changed; reload the conflicts", 409)
    return dvs[di], dvs[di].get(case["field"])


def settle_cases(key, decisions, master, note="", source="app"):
    """Settle conflicts of one paper: write the consensus once, record every decision.

    ``decisions`` = ``[(case, value)]`` with cases from :func:`study_conflicts`
    (built from the files as they are now).  Returns one result per decision.
    """
    master = str(master or "").strip()
    if not master:
        raise SettleError("master name is required")
    prepared = []
    for case, value in decisions:
        fdef = case["fdef"]
        newval = coerce_value(value, fdef)
        stored = _field_value(newval, fdef) if newval is not None else ""
        if not stored:
            raise SettleError(f"{case['field']}: choose a value to settle with")
        if not fdef["multi"] and fdef["levels"] and stored not in fdef["levels"]:
            raise SettleError(f"{case['field']}: '{value}' is not a level of this field")
        prepared.append((case, fdef, newval, stored))
    with _STORE_LOCK:
        existing_idx = SettlementIndex(load_settlements())
        path, raw, recs = _load_consensus_for_write(key)
        results, changed = [], False
        for case, fdef, newval, stored in prepared:
            old = existing_idx.find(case)
            applied, apply_note, previous, known = False, "", None, False
            written = None
            if case["field"] in PSEUDO_DV_FIELDS:
                apply_note = ("derived from dv_name: rename the consensus DV row in the "
                              "coding tab if stimulus veracity changed")
            elif recs is None:
                apply_note = "no consensus coding (Extraction) for this paper"
            elif not case.get("target"):
                apply_note = "the consensus coding has no row for this case"
            elif case["field"] == "timepoint":
                cell, _ = _cell(recs, case)
                tp1, tp2 = _tp_names()
                cur_tp = {tp1: cell.get(tp1), tp2: cell.get(tp2)}
                previous = (copy.deepcopy(old.get("previous_value"))
                            if (old and old.get("previous_value_known"))
                            else copy.deepcopy(cur_tp))
                known = True
                if _timepoint_bucket(cur_tp[tp1], cur_tp[tp2]) != stored:
                    cell[tp1] = stored
                    if _bucket_one(cell.get(tp2)) not in ("", stored):
                        cell[tp2] = None
                    changed = True
                written = {tp1: cell.get(tp1), tp2: cell.get(tp2)}
                applied = True
            else:
                cell, cur_val = _cell(recs, case)
                previous = (copy.deepcopy(old.get("previous_value"))
                            if (old and old.get("previous_value_known"))
                            else copy.deepcopy(cur_val))
                known = True
                if _field_value(cur_val, fdef) != stored or cur_val != newval:
                    cell[case["field"]] = newval
                    changed = True
                written = newval
                applied = True
            results.append({
                "id": uuid.uuid4().hex, "scope": "fulltext", "study_key": key,
                "study_id": case["study_id"], "case_id": case["case_id"],
                "field": case["field"], "kind": case["kind"], "dv_name": case.get("dv_name"),
                "value": stored, "written": written,
                "previous_value": previous, "previous_value_known": known,
                "members": case.get("members"), "target": case.get("target"),
                "rater_values": dict(case["values"]), "master": master, "decided_at": _now(),
                "applied": applied, "apply_note": apply_note, "note": note, "source": source,
            })
        if changed:
            _write_consensus(path, raw)

        def _mutate(store):
            idx = SettlementIndex(store)
            drop = set()
            for (case, *_rest), entry in zip(prepared, results):
                old = idx.find(case)
                if old:
                    drop.add(old["id"])
                store["history"].append(dict(entry, action="settle",
                                             replaces=old["id"] if old else None))
            store["settlements"] = [e for e in store["settlements"] if e.get("id") not in drop]
            store["settlements"].extend(results)
            st = store["studies"].get(key)
            if st and st.get("status") == "complete":
                st["status"] = "reopened"
                st["changed_at"] = _now()
        update_settlements(_mutate)
    _invalidate_agreement()
    return results


def clear_settlement(key, case, master):
    """Undo one settlement; the consensus is restored when it still holds the settled value."""
    with _STORE_LOCK:
        store = load_settlements()
        entry = SettlementIndex(store).find(case)
        if not entry:
            raise SettleError("this conflict is not settled", 404)
        restored, note = False, ""
        if entry.get("applied") and entry.get("previous_value_known") and case.get("target"):
            path, raw, recs = _load_consensus_for_write(key)
            if recs is not None:
                cell, current = _cell(recs, case)
                prev = entry.get("previous_value")
                if case["field"] == "timepoint" and isinstance(prev, dict):
                    tp1, tp2 = _tp_names()
                    cur_tp = {tp1: cell.get(tp1), tp2: cell.get(tp2)}
                    if cur_tp == entry.get("written"):
                        cell[tp1] = copy.deepcopy(prev.get(tp1))
                        cell[tp2] = copy.deepcopy(prev.get(tp2))
                        _write_consensus(path, raw)
                        restored = True
                    else:
                        note = "the consensus was edited since; left as it is"
                elif current == entry.get("written"):
                    cell[case["field"]] = copy.deepcopy(prev)
                    _write_consensus(path, raw)
                    restored = True
                else:
                    note = "the consensus was edited since; left as it is"
        elif entry.get("applied"):
            note = "the value before this settlement is unknown; the consensus is left as it is"

        def _mutate(st):
            st["settlements"] = [e for e in st["settlements"] if e.get("id") != entry["id"]]
            st["history"].append({"action": "clear", "id": entry["id"], "study_key": key,
                                  "case_id": entry.get("case_id"), "field": entry.get("field"),
                                  "master": str(master or "").strip(), "at": _now(),
                                  "consensus_restored": restored, "note": note})
        update_settlements(_mutate)
    _invalidate_agreement()
    return {"restored": restored, "note": note}


def set_study_status(key, status, master, scope="fulltext", open_conflicts=None):
    def _mutate(store):
        store["studies"][key] = {"scope": scope, "status": status,
                                 "master": str(master or "").strip(), "at": _now(),
                                 "open_conflicts": open_conflicts}
        store["history"].append({"action": "study-status", "study_key": key, "scope": scope,
                                 "status": status, "master": str(master or "").strip(),
                                 "at": _now()})
    update_settlements(_mutate)


def settle_abstract(uid, lab, value, master, rater_values, note=""):
    """Abstract-stage settlement; the settlement file IS the abstract consensus."""
    master = str(master or "").strip()
    if not master:
        raise SettleError("master name is required")
    v = "" if value in (None, "") else str(value).strip()
    if v and v not in _ABSTRACT_CATS:
        raise SettleError(f"'{v}' is not a verdict ({', '.join(_ABSTRACT_CATS)})")

    def _mutate(store):
        old = [e for e in store["settlements"] if e.get("scope") == "abstract"
               and e.get("study_key") == uid and e.get("field") == lab]
        store["settlements"] = [e for e in store["settlements"] if e not in old]
        if not v:
            if old:
                store["history"].append({"action": "clear", "id": old[0]["id"], "scope": "abstract",
                                         "study_key": uid, "field": lab, "master": master,
                                         "at": _now()})
            return None
        entry = {"id": uuid.uuid4().hex, "scope": "abstract", "study_key": uid,
                 "case_id": "abs:" + uid, "field": lab, "kind": "abstract", "value": v,
                 "rater_values": dict(rater_values or {}), "master": master,
                 "decided_at": _now(), "applied": True, "note": note, "source": "app"}
        store["settlements"].append(entry)
        store["history"].append(dict(entry, action="settle",
                                     replaces=old[0]["id"] if old else None))
        return entry
    with _STORE_LOCK:
        return update_settlements(_mutate)


def abstract_settlements(store=None):
    """{uid: {criterion: entry}} - the abstract-stage consensus."""
    store = store or load_settlements()
    out = {}
    for e in store.get("settlements") or []:
        if e.get("scope") == "abstract":
            out.setdefault(e.get("study_key"), {})[e.get("field")] = e
    return out


def settlements_csv(scope=None):
    store = load_settlements()
    cols = ["scope", "study_key", "study_id", "case_id", "field", "kind", "dv_name", "value",
            "previous_value", "applied", "apply_note", "rater_values", "master", "decided_at",
            "note", "source"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for e in store.get("settlements") or []:
        if scope and e.get("scope", "fulltext") != scope:
            continue
        row = []
        for c in cols:
            v = e.get(c)
            row.append(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else
                       ("" if v is None else v))
        w.writerow(row)
    return buf.getvalue()
