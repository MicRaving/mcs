# -*- coding: utf-8 -*-
"""Coverage routes: the harmonizer **triage screen** (interface §6 / plan §4 B3).

Serves the derived harmonization coverage products

* ``Results/harmonization_coverage_summary.csv`` -- one row per study with the
  per-slot rollup statuses, missing counts, blocking flags and the
  poolability-weighted missingness score;
* ``Results/harmonization_coverage.csv`` -- the per-``(study, slot[, variable])``
  matrix the chips and the assignment workspace read.

Endpoints
---------
GET /api/coverage                     ranked study list + per-slot chips + the
                                      readiness funnel + the assignment-audit
                                      counters (parity / conflicts / needs-veracity
                                      and the non-parity findings).
GET /api/coverage/study/<key>         the coverage matrix rows for one study plus
                                      the assignment targets (open coverage gaps +
                                      unmapped raw columns from ``variables.csv``).

The endpoint is read-only with respect to ``Datasets/``: it never writes a
dataset, only serves the products that ``Scripts/harmonization_coverage.py`` generated.
"""

from __future__ import annotations

from flask import Blueprint, abort, jsonify, request
import csv
import fnmatch
import glob
import os
import re

from studio_state import S

bp = Blueprint("coverage", __name__)


def _dataset_paths(folder, pattern):
    """Dataset paths matching *pattern*; manifest-aware in an online project.

    The CSV products of a dataset are on demand (INTERFACES 4), so a glob over
    the mirror would miss one that is not downloaded: the manifest lists it and
    the caller's ``S.ensure_local`` fetches the file it reads.
    """
    if S.online and S.store is not None:
        rel = S.relpath(folder)
        if rel is not None:
            return [os.path.join(folder, e["name"]) for e in S.listdir(rel)
                    if not e.get("is_dir") and fnmatch.fnmatchcase(e["name"], pattern)]
    return sorted(glob.glob(os.path.join(folder, pattern)))

# --- slot vocabulary (mirrors Scripts/harmonization_coverage.py; interface §6 is frozen) ----
# S10 (covariates) was retired 2026-11-19 (owner decision): its only coding
# source (the `Covariates` field) was coded in 0 of 1,272 records, so the slot
# was a permanent gap; the coverage matrix tracks ten essential slots.
SLOT_ORDER = ["S1", "S2", "S3", "S4", "S5", "S6", "S6cell", "S7", "S8",
              "S9", "S11"]
SLOT_NAMES = {
    "S1": "id", "S2": "condition_col", "S3": "arm_roles", "S4": "dv_pairs",
    "S5": "dv_domain", "S6": "veracity", "S6cell": "veracity_cell",
    "S7": "timepoint", "S8": "dv_polarity", "S9": "exclusions",
    "S11": "provenance",
}
SLOT_WEIGHT = {
    "S1": 2.0, "S2": 5.0, "S3": 4.0, "S4": 2.0, "S5": 4.0, "S6": 5.0,
    "S6cell": 5.0, "S7": 1.0, "S8": 3.0, "S9": 2.0, "S11": 1.0,
}

_FILTERS = ("all", "blocks_sdt", "blocks_did", "blocks_exclusions_on",
            "needs_na_verdict", "has_dataset", "paper_only")

_BLANK = {"", "nan", "none", "nat", "<na>"}


def _s(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if type(v).__name__ == "bool_":
        return "true" if bool(v) else "false"
    if v is None:
        return ""
    try:
        import pandas as pd
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    return "" if s.lower() in _BLANK else s


def _results_dir():
    return os.path.join(S.ROOT, "Results")


def _path(name):
    return os.path.join(_results_dir(), name)


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# cached CSV reads (the matrix is ~5 MB; one load per product revision)
# ---------------------------------------------------------------------------
_CACHE = {}


def _read_csv_cached(path):
    import pandas as pd
    key = os.path.abspath(path)
    mt = _mtime(path)
    hit = _CACHE.get(key)
    if hit is not None and hit[0] == mt:
        return hit[1]
    if not os.path.isfile(path):
        _CACHE[key] = (mt, None)
        return None
    try:
        df = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
        df.columns = [str(c).strip().lstrip("\ufeff") for c in df.columns]
    except Exception:  # noqa: BLE001  (never take the app down)
        df = None
    _CACHE[key] = (mt, df)
    return df


# ---------------------------------------------------------------------------
# derived views over the matrix / summary
# ---------------------------------------------------------------------------
_DERIVED = {"stamp": None, "data": None}


def _derive():
    """Load + index the coverage products once per (summary, matrix) revision."""
    summary_path = _path("harmonization_coverage_summary.csv")
    matrix_path = _path("harmonization_coverage.csv")
    stamp = (_mtime(summary_path), _mtime(matrix_path))
    if _DERIVED["data"] is not None and _DERIVED["stamp"] == stamp:
        return _DERIVED["data"]

    summary = _read_csv_cached(summary_path)
    matrix = _read_csv_cached(matrix_path)

    slot_counts, open_by_study, row_index = {}, {}, {}
    if matrix is not None and len(matrix) and {"study_key", "slot", "status"} <= set(matrix.columns):
        g = matrix.groupby(["study_key", "slot", "status"], dropna=False).size()
        for (k, slot, st), n in g.items():
            k, slot, st = _s(k), _s(slot), _s(st)
            if not k or not slot:
                continue
            slot_counts.setdefault(k, {}).setdefault(slot, {})
            slot_counts[k][slot][st] = slot_counts[k][slot].get(st, 0) + int(n)
        open_rows = matrix[matrix["status"].astype(str).str.strip().isin(("missing", "ambiguous"))]
        for k, sub in open_rows.groupby("study_key", sort=False):
            open_by_study[_s(k)] = _index_rows(sub.to_dict("records"))[1]
        # positional row index per study, so one study's rows are sliced, not filtered
        row_index = {_s(k): idx for k, idx in matrix.groupby("study_key", sort=False).indices.items()}
    data = {"summary": summary, "matrix": matrix, "slot_counts": slot_counts,
            "open_by_study": open_by_study, "row_index": row_index}
    _DERIVED.update(stamp=stamp, data=data)
    return data


def _matrix_rows(derived, key):
    m = derived["matrix"]
    idx = derived["row_index"].get(key)
    if m is None or idx is None:
        return []
    return [{c: _s(v) for c, v in r.items()} for r in m.iloc[idx].to_dict("records")]


def _index_rows(rows):
    """One study's matrix rows -> ({slot: {status: n}}, open gaps by poolability weight)."""
    counts, opens = {}, []
    for r in rows:
        slot, st = _s(r.get("slot")), _s(r.get("status"))
        if not slot:
            continue
        counts.setdefault(slot, {})
        counts[slot][st] = counts[slot].get(st, 0) + 1
        if st in ("missing", "ambiguous"):
            opens.append({"slot": slot, "scope": _s(r.get("scope")),
                          "target": _s(r.get("target")), "status": st,
                          "evidence": _s(r.get("evidence")),
                          "auto_detail": _s(r.get("auto_detail"))})
    opens.sort(key=lambda d: (-SLOT_WEIGHT.get(d["slot"], 0.0),
                              SLOT_ORDER.index(d["slot"]) if d["slot"] in SLOT_ORDER else 99,
                              d["target"]))
    return counts, opens


# ---------------------------------------------------------------------------
# live coverage for studies changed since the products were generated
# ---------------------------------------------------------------------------
_DIRTY = set()
_LIVE = {}
_INPUT_SUFFIXES = (" - harmonized_variables.csv", " - harmonized.csv", " - variables.csv",
                   " - exclusions.csv", " - conditions.csv", " - variable_assignments.csv",
                   " - dv_direction.csv")


def _inputs_stamp(key):
    folder = os.path.join(S.DATASETS or "", key)
    return tuple(_mtime(os.path.join(folder, key + s)) for s in _INPUT_SUFFIXES)


def _is_stale(key):
    """True when one of the study's coverage inputs is newer than the matrix product."""
    mt = _mtime(_path("harmonization_coverage.csv"))
    if mt is None:
        return False
    return any(t is not None and t > mt for t in _inputs_stamp(key))


def _live_study(key):
    """``(rows, summary)`` for one study from ``harmonization_coverage.coverage_for_study``,
    the function that writes the products, cached per input revision; ``None`` when it
    cannot be computed."""
    folder = os.path.join(S.DATASETS or "", key)
    if not os.path.isfile(os.path.join(folder, key + " - harmonized_variables.csv")):
        return None
    stamp = (S.DATASETS, _inputs_stamp(key))
    hit = _LIVE.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1], hit[2]
    try:
        from routes_harmonizer import _ensure_scripts_path
        _ensure_scripts_path()
        import harmonization_coverage as hc  # noqa: PLC0415
        rows, summ = hc.coverage_for_study(key, folder)
    except Exception:  # noqa: BLE001  (fall back to the products on disk)
        return None
    rows = [{c: _s(v) for c, v in r.items()} for r in rows]
    _LIVE[key] = (stamp, rows, summ)
    return rows, summ


_AUDIT = {"stamp": None, "data": None}


def _truthy(v):
    return str(v).strip().lower() in ("true", "1", "yes", "1.0")


def _audit_checks():
    """``Results/assignment_audit.csv`` as counters + findings, once per revision."""
    path = _path("assignment_audit.csv")
    mt = _mtime(path)
    if _AUDIT["data"] is not None and _AUDIT["stamp"] == mt:
        return _AUDIT["data"]
    df = _read_csv_cached(path)
    checks, findings = [], []
    if df is not None and len(df) and {"check", "ok"} <= set(df.columns):
        has_n = "n" in df.columns
        for chk, sub in df.groupby("check"):
            okm = sub["ok"].map(_truthy)
            ok = int(okm.sum())
            n = pd_num(sub["n"]).sum() if has_n else 0
            checks.append({"check": str(chk), "ok": ok, "bad": len(sub) - ok, "n": int(n)})
            for r in sub[~okm].to_dict("records"):
                try:
                    rn = int(float(r.get("n") or 0))
                except (TypeError, ValueError):
                    rn = 0
                findings.append({"study_key": _s(r.get("study_key")), "check": str(chk),
                                 "n": rn, "detail": _s(r.get("detail"))})
    checks.sort(key=lambda c: c["check"])
    data = (checks, findings)
    _AUDIT.update(stamp=mt, data=data)
    return data


def pd_num(series):
    import pandas as pd
    return pd.to_numeric(series, errors="coerce").fillna(0)


def _audit_block():
    """Aggregate ``Results/assignment_audit.csv`` into counters + findings."""
    checks, findings = _audit_checks()
    parity = next((c for c in checks if c["check"] == "parity"), {"ok": 0, "bad": 0, "n": 0})
    return {"checks": checks, "parity": parity, "findings": findings,
            "conflicts": _count_conflicts(),
            "conflict_studies": _conflict_studies(),
            "needs_veracity": _count_needs_veracity()}


_CONFLICTS = {"root": None, "at": 0.0, "stamp": None, "n": 0}
_CONFLICTS_TTL = 10.0


def _assignment_files():
    """[(key, path, mtime)] of every ``Datasets/<Key>/<Key> - variable_assignments.csv``
    (one scandir + one stat per folder; a glob lists every folder's contents)."""
    ds = S.DATASETS or ""
    out = []
    try:
        entries = list(os.scandir(ds)) if os.path.isdir(ds) else []
    except OSError:
        entries = []
    for e in entries:
        try:
            if e.is_dir():
                p = os.path.join(e.path, e.name + " - variable_assignments.csv")
                out.append((e.name, p, os.stat(p).st_mtime))
        except OSError:
            continue
    return sorted(out)


def _count_conflicts():
    """Assignment rows stamped ``conflict`` across every study file."""
    import time
    now = time.monotonic()
    if _CONFLICTS["root"] == S.DATASETS and now - _CONFLICTS["at"] < _CONFLICTS_TTL:
        return _CONFLICTS["n"]
    files = _assignment_files()
    stamp = tuple(files)
    if _CONFLICTS["root"] == S.DATASETS and _CONFLICTS["stamp"] == stamp:
        _CONFLICTS["at"] = now
        return _CONFLICTS["n"]
    n = 0
    for _k, p, _mt in files:
        try:
            with open(p, encoding="utf-8-sig", newline="") as fh:
                n += sum(1 for r in csv.DictReader(fh)
                         if (r.get("status") or "").strip().lower() == "conflict")
        except (OSError, csv.Error):
            continue
    _CONFLICTS.update(root=S.DATASETS, at=now, stamp=stamp, n=n)
    return n


def invalidate_study(key):
    """Called after an assignment save: the products on disk no longer describe
    ``key``, so its coverage is recomputed live until the products are rebuilt."""
    _CONFLICTS["at"] = 0.0
    _DIRTY.add(key)


def _conflict_studies(limit=8):
    """Studies with the most conflict-stamped assignment rows (click-through)."""
    out = []
    for p in glob.glob(os.path.join(S.DATASETS or "", "*",
                                    "* - variable_assignments.csv")):
        key = os.path.basename(os.path.dirname(p))
        try:
            import pandas as pd
            df = pd.read_csv(p, encoding="utf-8-sig", low_memory=False)
            if "status" not in df.columns:
                continue
            n = int(df["status"].astype(str).str.strip().str.lower()
                    .eq("conflict").sum())
            if n:
                out.append({"study_key": key, "n": n})
        except Exception:  # noqa: BLE001
            continue
    out.sort(key=lambda d: (-d["n"], d["study_key"].lower()))
    return out[:limit]


def _count_needs_veracity():
    """Studies whose sidecar still carries a blank veracity (S6 missing)."""
    d = _derive()
    s = d["summary"]
    if s is None or "S6_missing" not in s.columns:
        return 0
    try:
        return int((s["S6_missing"].fillna(0).astype(float) > 0).sum())
    except Exception:  # noqa: BLE001
        return 0


def _paper_keys():
    return set(getattr(S, "KEYS", set()) or set())


def _study_record(rec, slot_counts, open_by_study, paper_keys):
    key = _s(rec.get("study_key"))
    out = {"study_key": key, "has_dataset": True,
           "is_paper": key in paper_keys}
    for c in ("n_dv_rows", "n_dv_cols", "missingness_score",
              "blocks_sdt", "blocks_did", "blocks_exclusions_on",
              "needs_na_verdict"):
        v = rec.get(c)
        try:
            if v is not None and str(v) != "":
                f = float(v)
                out[c] = int(f) if f == int(f) else f
            else:
                out[c] = None
        except (TypeError, ValueError):
            out[c] = v
    for slot in SLOT_ORDER:
        out[slot] = _s(rec.get(slot))
        try:
            out[slot + "_missing"] = int(float(rec.get(slot + "_missing") or 0))
        except (TypeError, ValueError):
            out[slot + "_missing"] = 0
    counts = slot_counts.get(key, {})
    out["slot_counts"] = {slot: dict(counts.get(slot, {})) for slot in SLOT_ORDER}
    opens = list(open_by_study.get(key, []))
    out["open_slots"] = opens
    out["first_open_slot"] = opens[0] if opens else None
    # redesign additions (additive -- old keys unchanged): the three pool
    # pills with what still blocks each pool, and the open-gap count
    out["open_count"] = len(opens)
    out["pools"] = _pools_block(rec, opens)
    return out


def _pools_block(rec, opens):
    """The three pool pills for one study row (plain labels + what is missing).

    Mirrors gapwalk_lib.pools_for (the workspace payload) so the list and the
    workspace can never disagree about a study's pools.
    """
    from gapwalk_lib import pools_for
    try:
        return pools_for(rec, opens)
    except Exception:  # noqa: BLE001  (never take the tab down)
        return {"sdt": {"joins": None, "label": "Joins the signal-detection pool",
                        "define": "", "missing": []},
                "did": {"joins": None, "label": "Joins the pre-post pool",
                        "define": "", "missing": []},
                "excl": {"joins": None, "label": "Author exclusions apply",
                         "define": "", "missing": []}}


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------
# --------------------------------------------------------------------------- #
# plain-language metadata for the redesigned tab (single source: gapwalk_lib)
# --------------------------------------------------------------------------- #
try:
    from gapwalk_lib import SLOT_QUESTIONS as _SLOT_QUESTIONS
except Exception:  # noqa: BLE001  (the tab degrades to bare codes)
    _SLOT_QUESTIONS = {}


def _slot_meta():
    """slots[] entries with the plain question/label/hint (old keys kept)."""
    out = []
    for s in SLOT_ORDER:
        q = _SLOT_QUESTIONS.get(s, {})
        out.append({"code": s, "name": SLOT_NAMES[s], "weight": SLOT_WEIGHT[s],
                    "question": q.get("question", ""),
                    "short": q.get("short", SLOT_NAMES[s]),
                    "hint": q.get("hint", "")})
    return out


@bp.get("/api/coverage")
def api_coverage():
    d = _derive()
    summary = d["summary"]
    paper_keys = _paper_keys()
    studies = []
    if summary is not None and len(summary):
        for rec in summary.to_dict("records"):
            studies.append(_study_record(rec, d["slot_counts"], d["open_by_study"],
                                         paper_keys))
    # studies saved from the assignment workspace since the products were built
    for key in sorted(_DIRTY):
        if not _is_stale(key):
            _DIRTY.discard(key)
            continue
        live = _live_study(key)
        if live is None:
            continue
        counts, opens = _index_rows(live[0])
        rec = _study_record(live[1], {key: counts}, {key: opens}, paper_keys)
        rec["live"] = True
        studies = [s for s in studies if s["study_key"] != key] + [rec]
    have = {s["study_key"] for s in studies}
    for k in sorted(paper_keys - have):
        rec = {"study_key": k, "missingness_score": None, "blocks_sdt": None,
               "blocks_did": None, "blocks_exclusions_on": None,
               "needs_na_verdict": None, "n_dv_rows": 0, "n_dv_cols": 0}
        for slot in SLOT_ORDER:
            rec[slot] = ""
            rec[slot + "_missing"] = 0
        studies.append(_study_record(rec, {}, {}, paper_keys) | {"has_dataset": False})

    # readiness funnel (true+false cell -> + condition column -> + explicit roles)
    def _filled(slot):
        return {s["study_key"] for s in studies
                if s.get("has_dataset") and s.get(slot) == "filled"}

    cell = _filled("S6cell")
    cond = _filled("S2")
    roles = _filled("S3")
    funnel = {"true_false_cell": len(cell), "with_condition": len(cell & cond),
              "explicit_roles": len(cell & cond & roles),
              "harmonized": len(have), "papers": len(paper_keys)}
    # redesign addition (additive): the three pool counts spelled out for the
    # "N studies join a pool today" headline
    n_ds = sum(1 for s in studies if s.get("has_dataset"))
    funnel["pools"] = {
        "sdt": sum(1 for s in studies if s.get("has_dataset") and s.get("blocks_sdt") != 1),
        "did": sum(1 for s in studies if s.get("has_dataset") and s.get("blocks_did") != 1),
        "exclusions_on": sum(1 for s in studies if s.get("blocks_exclusions_on") == 1),
        "any": sum(1 for s in studies if s.get("has_dataset") and
                   (s.get("blocks_sdt") != 1 or s.get("blocks_did") != 1)),
        "datasets": n_ds,
    }

    # filters
    flt = (request.args.get("filter") or "all").strip().lower()
    if flt not in _FILTERS:
        flt = "all"
    q = (request.args.get("q") or "").strip().lower()
    sort = (request.args.get("sort") or "missingness_score").strip()
    sort_key = sort[1:] if sort.startswith("-") else sort
    direction = (request.args.get("dir") or "").strip().lower()
    if direction not in ("asc", "desc"):
        direction = "desc" if sort_key in (
            "missingness_score", "blocks_sdt", "blocks_did",
            "blocks_exclusions_on", "needs_na_verdict",
            "n_dv_rows", "n_dv_cols") else "asc"

    def _keep(s):
        if flt == "paper_only":
            return not s["has_dataset"]
        if not s["has_dataset"]:
            return False
        if flt == "blocks_sdt":
            return s.get("blocks_sdt") == 1
        if flt == "blocks_did":
            return s.get("blocks_did") == 1
        if flt == "blocks_exclusions_on":
            return s.get("blocks_exclusions_on") == 1
        if flt == "needs_na_verdict":
            return s.get("needs_na_verdict") == 1
        return True

    # the KPI counters describe the whole corpus, not the current filter/search
    counts = {"total": len(studies), "has_dataset": sum(1 for s in studies if s["has_dataset"]),
              "paper_only": sum(1 for s in studies if not s["has_dataset"]),
              "blocks_sdt": sum(1 for s in studies if s.get("blocks_sdt") == 1),
              "blocks_did": sum(1 for s in studies if s.get("blocks_did") == 1),
              "blocks_exclusions_on": sum(1 for s in studies if s.get("blocks_exclusions_on") == 1),
              "needs_na_verdict": sum(1 for s in studies if s.get("needs_na_verdict") == 1)}

    studies = [s for s in studies if _keep(s) and (not q or q in s["study_key"].lower())]
    counts["shown"] = len(studies)

    def _val(s):
        v = s.get(sort_key)
        return None if v in (None, "") else v

    have = [s for s in studies if _val(s) is not None]
    miss = [s for s in studies if _val(s) is None]

    def _key(s):
        v = _val(s)
        return float(v) if isinstance(v, (int, float)) else str(v).lower()

    try:
        have.sort(key=_key, reverse=(direction == "desc"))
    except TypeError:
        have.sort(key=lambda s: str(_val(s)).lower(), reverse=(direction == "desc"))
    studies = have + miss

    return jsonify({
        "generated_at": _iso(_mtime(_path("harmonization_coverage_summary.csv"))),
        "slots": _slot_meta(),
        "funnel": funnel,
        "counts": counts,
        "filter": flt,
        "sort": sort_key,
        "dir": direction,
        "audit": _audit_block(),
        "studies": studies,
        "filters": list(_FILTERS),
        "sort_note": ("studies are ranked by the coverage pipeline's "
                      "poolability-weighted missingness score; ascending = "
                      "most pools already unblocked"),
    })


@bp.get("/api/gapwalk/<path:key>")
def api_gapwalk(key):
    """The guided gap queue for one study (redesigned Harmonization tab).

    Read-only: composes the coverage matrix's open gaps with paper quotes
    from ``Text/<Key>.txt``, pre-fills from the coding tab and the
    harmonizer's decoded guesses.  The save stays
    ``POST /api/harmonizer``-owned: ``POST /api/assignments/<key>``.
    """
    from gapwalk_lib import build_gapwalk, safe_study_key
    if not safe_study_key(key):
        abort(404)
    folder = os.path.join(S.DATASETS or "", key)
    if not os.path.isdir(folder):
        return jsonify({"key": key, "exists": False, "has_dataset": False,
                        "queue": [], "answered": [], "checklist": [],
                        "pools": {}, "counts": {"open": 0, "answered": 0},
                        "paper": {}, "facts": [], "notes": "", "options": {},
                        "profile": [], "wide_columns": [],
                        "warning": "no dataset folder for this study"})
    d = _derive()
    if d["matrix"] is None:
        return jsonify({"key": key, "exists": True, "has_dataset": True,
                        "queue": [], "answered": [], "checklist": [],
                        "pools": {}, "counts": {"open": 0, "answered": 0},
                        "paper": {}, "facts": [], "notes": "", "options": {},
                        "profile": [], "wide_columns": [],
                        "warning": "harmonization_coverage.csv not found - run "
                                   "python Scripts/harmonization_coverage.py"})
    payload = build_gapwalk(key, d)
    # leftover capability from the previous workspace: unmapped raw columns as
    # optional extra cards (kind "raw"), kept out of the main gap queue
    try:
        payload["raw_targets"] = [t for t in _assignment_targets(key, d)
                                  if t.get("kind") == "raw"]
    except Exception:  # noqa: BLE001
        payload["raw_targets"] = []
    return jsonify(payload)


@bp.get("/api/coverage/study/<path:key>")
def api_coverage_study(key):
    from routes_harmonizer import valid_key
    if not valid_key(key):
        abort(404)
    d = _derive()
    live = _live_study(key) if (d["matrix"] is None or _is_stale(key)) else None
    if live is not None:
        rows = live[0]
        _counts, opens = _index_rows(rows)
    elif d["matrix"] is None:
        return jsonify({"key": key, "rows": [], "targets": [], "open_slots": [],
                        "warning": "harmonization_coverage.csv not found — run "
                                   "python Scripts/harmonization_coverage.py"})
    else:
        rows = _matrix_rows(d, key)
        opens = d["open_by_study"].get(key, [])
    targets = _assignment_targets(key, opens)
    return jsonify({"key": key, "rows": rows, "targets": targets, "open_slots": opens,
                    "live": live is not None})


def _assignment_targets(key, open_slots):
    """Open coverage gaps + unmapped raw columns for the workspace's (L) pane."""
    targets = []
    seen = set()

    # 1) open coverage gaps (per-variable slots point at a sidecar uniform name)
    for gap in open_slots:
        t = {"kind": "coverage", "slot": gap["slot"], "scope": gap["scope"],
             "target": gap["target"], "status": gap["status"],
             "evidence": gap["evidence"], "detail": gap["auto_detail"],
             "uniform_variable": "", "original_variable": "", "artifact": "",
             "item_scope": "", "keyable": True}
        if gap["scope"] == "variable":
            t["uniform_variable"] = gap["target"]
        targets.append(t)

    # 2) unmapped raw columns from the study's variables.csv
    folder = os.path.join(S.DATASETS or "", key)
    var_path = os.path.join(folder, f"{key} - variables.csv")
    var_path = S.ensure_local(var_path)
    # a column the sidecar already maps (as its raw or its uniform name) is not unmapped
    orig_seen = set()
    try:
        from routes_harmonizer import load_harmonized_variables
        for v in (load_harmonized_variables(key) or {}).get("variables", []):
            orig_seen.update(n for n in (v["original_variable"], v["uniform_variable"]) if n)
    except Exception:  # noqa: BLE001  (an unreadable sidecar only widens the list)
        pass
    if os.path.isfile(var_path):
        try:
            import pandas as pd
            v = pd.read_csv(var_path, encoding="utf-8-sig", low_memory=False)
            v.columns = [str(c).strip().lstrip("\ufeff") for c in v.columns]
            ucol = "uniform_variable" if "uniform_variable" in v.columns else (
                "uniform_name" if "uniform_name" in v.columns else None)
            for _, r in v.iterrows():
                orig = _s(r.get("original_variable"))
                if orig:
                    orig_seen.add(orig)
                role = _s(r.get("role")).lower()
                uni = _s(r.get(ucol)) if ucol else ""
                if role not in ("other", "unmapped") or not orig:
                    continue
                t = {"kind": "raw", "slot": "", "scope": "variable",
                     "target": orig, "status": "unmapped", "evidence": f"variables.csv:{orig}",
                     "detail": _s(r.get("notes")) or f"role={role}",
                     "uniform_variable": uni, "original_variable": orig,
                     "artifact": _s(r.get("artifact")), "item_scope": "",
                     "original_values": _s(r.get("original_values")),
                     "mapped_values": _s(r.get("mapped_values")),
                     "keyable": True}
                targets.append(t)
        except Exception:  # noqa: BLE001
            pass

    # 3) raw headers not present in variables.csv
    for p in _dataset_paths(folder, glob.escape(key) + " - prepared*.csv"):
        p = S.ensure_local(p)
        try:
            with open(p, encoding="utf-8-sig", newline="") as fh:
                hdr = [str(c).strip().lstrip("\ufeff") for c in next(csv.reader(fh))]
        except (OSError, StopIteration):
            continue
        art = "main"
        m = re.search(r" - prepared (\d+|[A-Za-z0-9]+)\.csv$", os.path.basename(p))
        if m:
            art = m.group(1)
        for c in hdr:
            if not c or c in orig_seen or c.lower() in ("pid", "study_key", "artifact"):
                continue
            t = {"kind": "raw", "slot": "", "scope": "variable", "target": c,
                 "status": "unmapped", "evidence": f"prepared:{art}",
                 "detail": "raw column with no variables.csv row",
                 "uniform_variable": "", "original_variable": c, "artifact": art,
                 "item_scope": "", "keyable": True}
            targets.append(t)

    # de-dup on (kind, original/target, slot)
    out = []
    for t in targets:
        k = (t["kind"], t.get("original_variable") or t["target"], t["slot"])
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def _iso(mt):
    if mt is None:
        return None
    from datetime import datetime
    return datetime.fromtimestamp(mt).isoformat(timespec="seconds")
