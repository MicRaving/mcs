"""Agreement logic: rater loading, the coding index, Gwet's AC and the abstract-level index."""

import glob
import json
import os
import re
import sys
import tempfile
import threading
import time
from screening_lib import screening_index
from studio_lib import (_is_continuous, _is_object_field, _norm, _schema, _values,
                        atomic_replace, coder_files, load_extraction, safe_token)
from studio_state import S, paper_keys

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


# ---------------------------------------------------------------- agreement tab
# Inter-rater agreement (Gwet's AC1/AC2) and coder-conflict resolution across
# every deterministic label: schema fields with a closed option list or a fixed
# vocabulary, fewer than 50 levels.  Raters are the canonical Extraction
# (labelled "LLM/base") plus every Codings/<Key> - <coder>.json file.
# Experiment-level fields are matched by Study_ID, DV-level fields by
# Study_ID + dv_index; a cell a rater did not code is simply absent, so raters
# need not overlap (e.g. rater1 codes the first 10% of items, rater2 the last
# 10%).  The Master's resolutions are written to
# Codings/<Key> - <master>.json; the canonical Extraction is never touched.

AGREEMENT_REFERENCE = "LLM/base"

_LABEL_MAX_LEVELS = 50

_AGREE_CACHE = {"at": 0.0, "data": None}

#: Cached exclude-filtered DV alignments, keyed by the excluded-rater tuple.
_DV_ALIGN_CACHE = {}

_AGREE_LOCK = threading.Lock()

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
            multi = False
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

def _load_agreement_raters():
    raters = {}
    for key in paper_keys():
        recs = load_extraction(key)
        if recs:
            raters.setdefault(AGREEMENT_REFERENCE, {})[key] = recs
        for info in coder_files(key):
            try:
                with open(os.path.join(S.CODINGS, info["file"]), encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:
                continue
            raters.setdefault(info["coder"], {})[key] = data if isinstance(data, list) else [data]
    return raters

def _agreement_index():
    """{rater: {key: {field: {case_tuple: value}}}} built once, TTL-cached."""
    now = time.time()
    with _AGREE_LOCK:
        if _AGREE_CACHE["data"] is not None and now - _AGREE_CACHE["at"] < 15:
            return _AGREE_CACHE["data"]
    fields = _agreement_field_defs()
    exp_fields = [f["name"] for f in fields if f["kind"] == "experiment"]
    dv_fields = [f["name"] for f in fields if f["kind"] == "dv"]
    fmap = {f["name"]: f for f in fields}
    raw = _load_agreement_raters()
    index = {}
    for rater, by_key in raw.items():
        rindex = {}
        for key, recs in by_key.items():
            fvals = {}
            for rec in recs or []:
                sid = str(rec.get("Study_ID") or "").strip()
                if not sid:
                    continue
                for fn in exp_fields:
                    v = _field_value(rec.get(fn), fmap[fn])
                    if v:
                        fvals.setdefault(fn, {})[(sid,)] = v
                for i, dv in enumerate(rec.get("DV_Metrics") or []):
                    if not isinstance(dv, dict):
                        continue
                    for fn in dv_fields:
                        v = _field_value(dv.get(fn), fmap[fn])
                        if v:
                            fvals.setdefault(fn, {})[(sid, i)] = v
            if fvals:
                rindex[key] = fvals
        index[rater] = rindex
    dv_blocks = _build_dv_alignment(raw)
    data = {"fields": fields, "raters": index,
            "dv_blocks": dv_blocks,
            "dv_studies": _dv_study_alignment(dv_blocks),
            "rater_names": sorted(raw.keys()),
            "rater_summary": [
                {"rater": r, "keys": len(by_key),
                 "experiments": sum(len(recs) for recs in by_key.values())}
                for r, by_key in raw.items()]}
    with _AGREE_LOCK:
        _AGREE_CACHE.update(at=now, data=data)
    return data

def _invalidate_agreement():
    with _AGREE_LOCK:
        _AGREE_CACHE.update(at=0.0, data=None)
        _DV_ALIGN_CACHE.clear()

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
    pi = [sum((agree[i][k] / ri[i]) for i in range(n) if ri[i] >= 1) / n for k in range(q)]
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
# within each (paper, Study_ID) block and only the closed-label DV fields are
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

def _timepoint_bucket(tp1, tp2=None):
    """Canonical timepoint bucket (pre/post/followup) from timepoint_1/2."""
    bs = {b for b in (_bucket_one(tp1), _bucket_one(tp2)) if b}
    if not bs:
        return ""
    if len(bs) == 1:
        return next(iter(bs))
    if "followup" in bs:
        return "followup"
    if "post" in bs:
        return "post"
    if "pre" in bs:
        return "pre"
    return ""

def _domains_tuple(raw):
    return tuple(sorted({str(v).strip() for v in _values(raw) if str(v).strip()}))

def canonical_dv_key(dv):
    """Stable identity of one DV row, WITHOUT the free-text dv_name."""
    return (
        _domains_tuple(dv.get("dv_domain")),
        _veracity_token(dv.get("dv_name")),
        _timepoint_bucket(dv.get("timepoint_1"), dv.get("timepoint_2")),
        _norm(dv.get("contrast_type")),
        str(dv.get("Intervention_ID") or "").strip().lower(),
    )

def _pseudo_dv_field(name, label, levels):
    levels = list(levels)
    canon = {str(l): l for l in levels}
    canon.update({str(l).lower(): l for l in levels})
    return {"name": name, "label": label, "group": "DV identity", "kind": "dv",
            "multi": False, "ordered": False, "levels": levels,
            "labels": {l: l for l in levels}, "canon": canon}

def _dv_agreement_defs():
    """Closed-label DV fields that may be scored (never dv_name / numeric metrics)."""
    base = {f["name"]: f for f in _agreement_field_defs() if f["kind"] == "dv"}
    out = []
    for name in ("dv_domain", "veracity", "contrast_type", "dv_polarity",
                 "eligible_primary", "score_types", "timepoint"):
        if name in ("veracity", "timepoint"):
            out.append(_pseudo_dv_field(name, name.capitalize(), _PSEUDO_DV_LEVELS[name]))
        elif name in base:
            out.append(base[name])
    return out

def _dv_field_value(dv, fdef):
    name = fdef["name"]
    if name == "veracity":
        return _veracity_token(dv.get("dv_name"))
    if name == "timepoint":
        return _timepoint_bucket(dv.get("timepoint_1"), dv.get("timepoint_2"))
    return _field_value(dv.get(name), fdef)

def _dv_row_info(rater, key, rec, rec_index, dv, dv_index, fdefs):
    sid = str(rec.get("Study_ID") or "").strip()
    norm_name = _norm_dv_name(dv.get("dv_name"))
    return {
        "rater": rater, "key": key, "study_id": sid,
        "block": (key, sid or f"#{rec_index}"),
        "rec_index": rec_index, "dv_index": dv_index,
        "name": dv.get("dv_name") or "",
        "align_key": canonical_dv_key(dv),
        "name_norm": norm_name,
        "tokens": _name_tokens(norm_name),
        "values": {f["name"]: _dv_field_value(dv, f) for f in fdefs},
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

def _make_block(rows):
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
        raters.setdefault(row["rater"], {"rater": row["rater"], "n_dv": 0,
                                         "matched": 0, "unmatched": 0})
        raters[row["rater"]]["unmatched"] += 1
    max_n = max((d["n_dv"] for d in raters.values()), default=0)
    matched_pairs = len(matched)
    return {
        "key": key, "study_id": sid,
        "raters": sorted(raters.values(), key=lambda d: str(d["rater"]).lower()),
        "matched_pairs": matched_pairs, "unmatched": len(unmatched),
        "n_dv_total": len(rows),
        "coverage": (matched_pairs / max_n) if max_n else 0.0,
        "_units": matched, "_rows": list(rows),
    }

def _build_dv_alignment(raw):
    fdefs = _dv_agreement_defs()
    blocks = {}
    for rater, by_key in raw.items():
        for key, recs in by_key.items():
            for rec_index, rec in enumerate(recs or []):
                if not isinstance(rec, dict):
                    continue
                for dv_index, dv in enumerate(rec.get("DV_Metrics") or []):
                    if not isinstance(dv, dict):
                        continue
                    info = _dv_row_info(rater, key, rec, rec_index, dv, dv_index, fdefs)
                    blocks.setdefault(info["block"], []).append(info)
    out = []
    for rows in blocks.values():
        block = _make_block(rows)
        if block:
            out.append(block)
    out.sort(key=lambda b: (str(b["key"]).lower(), str(b["study_id"]).lower()))
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
    """Blocks + study rollup for a given rater-exclusion set (cached default)."""
    exclude = {e for e in exclude if e}
    if not exclude:
        return index.get("dv_blocks") or [], index.get("dv_studies") or []
    key = tuple(sorted(exclude))
    now = time.time()
    hit = _DV_ALIGN_CACHE.get(key)
    if hit and now - hit[0] < 15:
        return hit[1], hit[2]
    blocks = []
    for block in index.get("dv_blocks") or []:
        rows = [r for r in block["_rows"] if r["rater"] not in exclude]
        if len({r["rater"] for r in rows}) >= 2:
            nb = _make_block(rows)
            if nb:
                blocks.append(nb)
    studies = _dv_study_alignment(blocks)
    _DV_ALIGN_CACHE[key] = (now, blocks, studies)
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
        "by_block": [{k: v for k, v in b.items() if k not in ("_units", "_rows")}
                     for b in blocks],
    }

def insufficient_note(coverage, matched_pairs=None):
    if (matched_pairs is not None and matched_pairs < DV_MIN_MATCHED
            and coverage >= DV_MIN_COVERAGE):
        return f"insufficient matched DVs ({matched_pairs} of {DV_MIN_MATCHED} needed)"
    return f"insufficient DV overlap (coverage {round(100 * coverage)}%)"

# ---------------------------------------------------------------- abstract agreement
# Inter-rater agreement and conflict resolution for the abstract-screening stage,
# over the C1–C4 verdicts in Screening/<coder>.json.

_ABSTRACT_FIELDS = [("C1", "Unrelated topic"), ("C2", "No control group"),
                    ("C3", "Not in English"), ("C4", "No primary experimental study")]

_ABSTRACT_CATS = ["Yes", "No", "Unsure"]

def _abstract_agreement_index():
    """{coder: {uid: {C1..C4: verdict}}} from Screening/<coder>.json."""
    out = {}
    if not S.SCREENING or not os.path.isdir(S.SCREENING):
        return out
    for p in sorted(glob.glob(os.path.join(S.SCREENING, "*.json"))):
        coder = os.path.splitext(os.path.basename(p))[0]
        try:
            with open(p, encoding="utf-8") as fh:
                dec = json.load(fh)
        except Exception:
            continue
        if not isinstance(dec, dict):
            continue
        by_uid = {}
        for uid, rec in dec.items():
            crits = (rec or {}).get("criteria") or {}
            vals = {}
            for lab, _ in _ABSTRACT_FIELDS:
                v = (crits.get(lab) or {}).get("verdict")
                if v:
                    vals[lab] = str(v).strip()
            if vals:
                by_uid[str(uid)] = vals
        if by_uid:
            out[coder] = by_uid
    # The original LLM abstract codings: the master workbook's C1–C4 verdicts
    # (the AI screening reference) are included as a rater.
    try:
        idx = screening_index()
    except Exception:
        idx = {}
    llm = {}
    for rec in (idx.get("rows") or []):
        ai = rec.get("ai") or {}
        vals = {}
        for lab, _ in _ABSTRACT_FIELDS:
            v = str((ai.get(lab) or {}).get("verdict") or "").strip()
            if v:
                vals[lab] = v
        if vals:
            llm[str(rec.get("id"))] = vals
    if llm:
        out.setdefault(AGREEMENT_REFERENCE, llm)
    return out

def _abstract_case_maps(index):
    """{label: {uid: {rater: verdict}}}."""
    maps = {lab: {} for lab, _ in _ABSTRACT_FIELDS}
    for rater, by_uid in index.items():
        for uid, vals in by_uid.items():
            for lab, v in vals.items():
                maps[lab].setdefault(uid, {})[rater] = v
    return maps


# ---------------------------------------------------------------- resolutions
# The Master resolves conflicts by *deciding* a value.  Those decisions live in a
# dedicated store, not inferred from the Master's coding file: a Master who has
# not coded a paper gets a file cloned from the canonical Extraction when they
# settle their first conflict, and inferring "settled" from that clone would mark
# every cell settled.  The decided value is ALSO written into
# ``Codings/<Key> - <master>.json`` so it feeds the agreement and the coding.

_RESOLUTIONS_DIRNAME = "_resolutions"


def _resolutions_path(master):
    safe = safe_token(master)
    if not safe or safe == "coder":
        return None
    return os.path.join(S.CODINGS, _RESOLUTIONS_DIRNAME, safe + ".json")


def load_resolutions(master):
    """{key: {case_id: {field: value}}} decided by the Master."""
    path = _resolutions_path(master)
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_resolutions(master, data):
    path = _resolutions_path(master)
    if not path:
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=S.TEMP)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        atomic_replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return True


def record_resolution(master, key, case_id, field, value):
    """Remember (or clear) one Master decision."""
    if not master or not key or not case_id or not field:
        return False
    data = load_resolutions(master)
    cases = data.setdefault(key, {})
    entry = cases.setdefault(case_id, {})
    if value in (None, ""):
        entry.pop(field, None)
        if not entry:
            cases.pop(case_id, None)
        if not cases:
            data.pop(key, None)
    else:
        entry[field] = value
    save_resolutions(master, data)
    return True


def resolution_value(resolutions, key, case_id, field):
    return ((resolutions.get(key) or {}).get(case_id) or {}).get(field)


def _rater_sort_key(rater, reference):
    """Human coders first (alphabetical), the LLM/base rater last."""
    return (str(rater) == reference, str(rater).lower())


def dv_conflict_cases(index, master="", field_names=None,
                      reference=AGREEMENT_REFERENCE):
    """Aligned DV-level conflicts: one entry per matched unit × scored field.

    Uses the SAME canonical DV alignment as the reliability coefficients, so a
    study cannot show hundreds of phantom conflicts merely because two raters
    list their DVs in a different order.  Only the fields the reliability scores
    (``_dv_agreement_defs``) are offered for resolution.
    """
    defs = _dv_agreement_defs()
    if field_names:
        wanted = set(field_names)
        defs = [f for f in defs if f["name"] in wanted]
    out = []
    for block in index.get("dv_blocks") or []:
        key = block["key"]
        sid = block["study_id"]
        for unit in block.get("_units") or []:
            if len(unit) < 2:
                continue
            anchor = (unit.get(reference) or (unit.get(master) if master else None)
                      or next(iter(unit.values())))
            case_id = "dv:%d:%d" % (int(anchor.get("rec_index") or 0),
                                    int(anchor.get("dv_index") or 0))
            mrow = unit.get(master) if master else None
            trow = mrow or unit.get(reference) or anchor
            for f in defs:
                vals = {}
                for rater, row in sorted(unit.items(),
                                         key=lambda kv: _rater_sort_key(kv[0], reference)):
                    v = (row.get("values") or {}).get(f["name"]) or ""
                    if v:
                        vals[rater] = v
                if len(set(vals.values())) < 2:
                    continue
                out.append({
                    "key": key, "study_id": sid, "case_id": case_id,
                    "field": f["name"], "field_label": f.get("label") or f["name"],
                    "group": f.get("group") or "DV", "kind": "dv",
                    "multi": f["multi"], "levels": f["levels"], "labels": f["labels"],
                    "values": vals,
                    "master_value": ((mrow or {}).get("values") or {}).get(f["name"]) or None,
                    "dv_index": trow.get("dv_index"),
                    "dv_name": trow.get("name") or anchor.get("name") or "",
                })
    return out
