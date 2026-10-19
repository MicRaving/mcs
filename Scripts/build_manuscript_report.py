#!/usr/bin/env python
"""Phase 4 — reporting: PRISMA 2020 flow + manuscript tables.

Builds, from frozen inputs only (never invents a value):

* a PRISMA 2020 flow diagram (PNG + SVG + Mermaid + ASCII) whose flow numbers
  come from ``Scripts/PRISMA_CLOSURE.md`` when present and are cross-checked
  against ``master_dataset.xlsx`` via the same counting logic the Coding Studio
  app uses (``app.py::_prisma_counts``);
* ``Scripts/PRISMA_REPORT.md`` — identification / screening / eligibility /
  inclusion with the exclusion reasons by stage, embedding the manuscript tables;
* ``Results/manuscript_tables.xlsx`` — Table 1 (study characteristics),
  Tables 2–4 (pooled posttest / persistence / DiD effects), Table 5
  (publication bias) and Table 6 (moderators);
* an APA-style ``.docx`` when ``python-docx`` and the repo's
  ``Scripts/coding_web/apa_export.py`` are available.

Every script writes its intermediates to ``temp/`` and writes outputs atomically
(temp file + ``os.replace``).  Existing outputs are never deleted.
"""
import collections
import csv
import glob
import json
import math
import os
import re
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMP = os.path.join(ROOT, "temp")
RESULTS = os.path.join(ROOT, "Results")
SCRIPTS = os.path.join(ROOT, "Scripts")
EXTRACTION = os.path.join(ROOT, "Extraction")
FIGDIR = os.path.join(RESULTS, "figures")

sys.path.insert(0, SCRIPTS)
try:
    from safe_replace import safe_replace
except Exception:  # pragma: no cover - fall back to a plain atomic replace
    safe_replace = os.replace
try:
    from suite_common import dl_random
    import suite_common as SC
except Exception:  # pragma: no cover - suite_common is present in this repo
    dl_random = None
    SC = None

os.makedirs(TEMP, exist_ok=True)
os.makedirs(FIGDIR, exist_ok=True)
tempfile.tempdir = TEMP

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #
# 2026-09-19: `belief_accuracy` merged into `credibility` (PI decision; see
# Scripts/DV_DOMAIN_MERGE_LOG.md).
SUSCEPTIBILITY = {"credibility", "sharing_intention", "technique_recognition"}

# Review-criterion and full-text exclusion labels come from the canonical counter
# (prisma_report), never a local copy: the old hand-written set ("Unrelated
# topic", "No control group", …) shipped outdated exclusion types in the figure.
try:
    import prisma_report as _PR_REPORT
    APA_CRIT = dict(getattr(_PR_REPORT, "CRITERION_REASON", {}))
    APA_DUP = getattr(_PR_REPORT, "DUP_CRITERION",
                      "Duplicate/non-independent report of an included study")
except Exception:  # pragma: no cover - prisma_report is always present in-repo
    APA_CRIT = {"C1": "No misinformation intervention (C1)",
                "C2": "No control/comparison group (C2)",
                "C3": "Not English (C3)",
                "C4": "Not a primary experimental study (C4)"}
    APA_DUP = "Duplicate/non-independent report of an included study"
if not APA_CRIT:
    APA_CRIT = {"C1": "No misinformation intervention (C1)",
                "C2": "No control/comparison group (C2)",
                "C3": "Not English (C3)",
                "C4": "Not a primary experimental study (C4)"}
# Full-text exclusion categories are collected per run from the real reasons in
# master_dataset.xlsx (see ``prisma_from_master``); no fixed list is used.
FT_GROUPS = []

BASE_SPECS = ["post_all", "post_cred", "persistence_all", "persistence_cred",
              "did_all", "did_cred"]
DERIVED_SPECS = ["didpost_all", "didpost_cred", "fupost_all", "fupost_cred"]

SPEC_LABEL = {
    "post_all": "Immediate posttest — all outcomes",
    "post_cred": "Immediate posttest — credibility",
    "persistence_all": "Follow-up persistence — all outcomes",
    "persistence_cred": "Follow-up persistence — credibility",
    "did_all": "Pretest-corrected DiD — all outcomes",
    "did_cred": "Pretest-corrected DiD — credibility",
    "didpost_all": "Pretest-corrected where possible — all outcomes",
    "didpost_cred": "Pretest-corrected where possible — credibility",
    "fupost_all": "Follow-up where available — all outcomes",
    "fupost_cred": "Follow-up where available — credibility",
}

LAYER_LABEL = {"meta": "Reported effects (meta-analysis)",
               "sdt": "Signal detection (re-analysis)"}

TABLE_TITLES = {
    "Table_1_Study_Characteristics": "Table 1. Study characteristics",
    "Table_1b_Studies": "Table 1b. Characteristics of each included study",
    "Table_2_Posttest": "Table 2. Pooled immediate-posttest effects",
    "Table_3_Persistence": "Table 3. Pooled follow-up persistence effects",
    "Table_4_DiD": "Table 4. Pooled pretest-corrected (DiD) effects",
    "Table_5_Publication_Bias": "Table 5. Publication-bias tests",
    "Table_6_Moderators": "Table 6. Moderator analyses",
    "Table_S1_Derived_Specs": "Table S1. Derived combined specifications",
}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _i(x):
    v = _f(x)
    return int(round(v)) if v is not None else None


def fmt(v, d=2):
    v = _f(v)
    if v is None:
        return "–"
    return f"{v:,.{d}f}"


def fmt_ci(lo, hi, d=2):
    return f"[{fmt(lo, d)}, {fmt(hi, d)}]".replace("–", "–")


def fmt_p(p):
    p = _f(p)
    if p is None:
        return "–"
    if p < 0.001:
        return "< .001"
    return f"{p:.3f}".lstrip("0")


def atomic_write_text(path, text):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".txt")
    os.close(fd)
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    safe_replace(tmp, path)


def atomic_save_workbook(path, build):
    """``build(workbook)`` fills an openpyxl Workbook; saved atomically."""
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    build(wb)
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".xlsx")
    os.close(fd)
    wb.save(tmp)
    safe_replace(tmp, path)


def read_sheet_dicts(path, sheet):
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet]
        it = ws.iter_rows(values_only=True)
        head = [str(h).strip() if h is not None else "" for h in next(it, ())]
        rows = []
        for r in it:
            rows.append({head[i]: r[i] for i in range(len(head))})
        return rows
    finally:
        wb.close()


def read_csv_dicts(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


# --------------------------------------------------------------------------- #
# PRISMA counts from master_dataset.xlsx (mirrors app.py::_prisma_counts)
# --------------------------------------------------------------------------- #
def _short_ft_reason(reason):
    r = str(reason or "").strip()
    r = r.replace(" (or their advertising)", "")
    r = r.replace(" rather than misinformation susceptibility", "")
    r = r.replace("Intervention/outcome concerns a non-information behaviour or a "
                  "non-misinformation experiment",
                  "Non-information behaviour / non-misinformation experiment")
    r = r.replace("Outcome is eyewitness/memory performance, not misinformation susceptibility",
                  "Outcome is eyewitness/memory performance")
    r = r.replace("Stored full text is a different document (source mismatch); no valid "
                  "primary study data", "Stored full text is a different document")
    return r or "(unspecified)"


def _ft_group(reason):
    """Map a master-workbook full-text criterion to its diagram label."""
    return _short_ft_reason(reason)


def prisma_from_master():
    """Recompute the PRISMA flow from ``master_dataset.xlsx`` (authoritative)."""
    import openpyxl
    path = os.path.join(ROOT, "master_dataset.xlsx")
    if not os.path.isfile(path):
        return None
    total = ft_excl = ft_incl = 0
    crit = collections.Counter()
    ft_reasons = collections.Counter()
    src = collections.Counter()
    db = {"identified": 0, "abstract_excluded": 0, "abstract_reasons": collections.Counter(),
          "include": 0, "exclude": 0, "ft_groups": collections.Counter()}
    other = {"identified": 0, "include": 0, "exclude": 0, "ft_groups": collections.Counter(),
             "sources": collections.Counter()}
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        head = [str(h).strip() if h is not None else "" for h in next(it, ())]
        H = {h: i for i, h in enumerate(head)}

        def cell(row, name):
            i = H.get(name)
            if i is None or i >= len(row) or row[i] is None:
                return ""
            v = row[i]
            return v if isinstance(v, (int, float)) else str(v).strip()

        def _nd(x):
            return re.sub(r"[^a-z0-9]+", "", str(x or "").lower())[:80]

        def _ndoi(x):
            x = str(x or "").lower().strip()
            return re.sub(r"^https?://(dx\.)?doi\.org/", "", x)

        doi_src, title_src = {}, {}
        for row in it:
            total += 1
            verdicts = {c: str(cell(row, f"C{c}_verdict")) for c in (1, 2, 3, 4)}
            ft = str(cell(row, "Fulltext_Verdict"))
            assessed = ft in ("Include", "Exclude")
            hs = str(cell(row, "Hit_Source"))
            is_db = hs == "ebsco_export"
            d = _ndoi(cell(row, "doi"))
            t = _nd(cell(row, "title"))
            if d:
                doi_src.setdefault(d, is_db)
            if t:
                title_src.setdefault(t, is_db)
            T = db if is_db else other
            T["identified"] += 1
            if not assessed and any(v == "No" for v in verdicts.values()):
                for c, v in verdicts.items():
                    if v == "No":
                        crit[f"C{c}"] += 1
                if is_db:
                    db["abstract_excluded"] += 1
                    for c, v in verdicts.items():
                        if v == "No":
                            db["abstract_reasons"][APA_CRIT[f"C{c}"]] += 1
            if ft == "Exclude":
                ft_excl += 1
                reason = (str(cell(row, "Fulltext_Exclusion_Criterion"))
                          or str(cell(row, "Fulltext_Reason")) or "(unspecified)")
                ft_reasons[_short_ft_reason(reason)] += 1
                T["exclude"] += 1
                T["ft_groups"][_ft_group(reason)] += 1
            elif ft == "Include":
                ft_incl += 1
                T["include"] += 1
            else:
                c_no = [c for c, v in verdicts.items() if v == "No"]
                if is_db:
                    if not c_no:
                        T["ft_groups"]["Fulltext irretrievable"] += 1
                elif c_no:
                    T["ft_groups"][APA_CRIT[f"C{c_no[0]}"]] += 1
                else:
                    T["ft_groups"]["Fulltext irretrievable"] += 1
            if is_db:
                src["Database searches"] += 1
            else:
                key = "Meta-analyses / reviews" if hs else "Other sources"
                src[key] += 1
                other["sources"][key] += 1
    finally:
        wb.close()

    coded_keys = {re.sub(r"\s+Exp\d+$", "", os.path.splitext(os.path.basename(p))[0])
                  for p in glob.glob(os.path.join(EXTRACTION, "*.json"))}
    coded_keys.discard("harmonizer_template")
    coded_keys.discard("_harmonizer_template")
    coded = len(coded_keys)
    coded_db = coded_other = 0
    seen = set(coded_keys)
    for p in glob.glob(os.path.join(EXTRACTION, "*.json")):
        key = re.sub(r"\s+Exp\d+$", "", os.path.splitext(os.path.basename(p))[0])
        if key in seen:
            seen.discard(key)
        else:
            continue
        try:
            data = json.load(open(p, encoding="utf-8"))
            data = data if isinstance(data, list) else [data]
            o = data[0] if data else {}
        except Exception:
            o = {}
        s = doi_src.get(_ndoi(o.get("DOI")))
        if s is None:
            tt = _nd(o.get("Title"))
            s = title_src.get(tt) if tt else None
        if s is False:
            coded_other += 1
        else:
            coded_db += 1

    reports_included = ft_incl
    dup_reports = max(0, reports_included - coded)
    if dup_reports:
        ft_reasons["Duplicate/non-independent report of an included study"] = dup_reports
    dup_db = max(0, db["include"] - coded_db)
    dup_other = max(0, other["include"] - coded_other)

    # Canonical full-text exclusion categories (by frequency) + any seen in a track.
    ft_labels = [lab for lab, _n in ft_reasons.most_common()]
    for _T in (db, other):
        for _lab in _T["ft_groups"]:
            if _lab not in ft_labels:
                ft_labels.append(_lab)

    def group_line(T, dup):
        lines = {g: T["ft_groups"].get(g, 0) for g in ft_labels}
        if dup:
            lines[APA_DUP] = dup
        return lines

    db_groups = group_line(db, dup_db)
    other_groups = group_line(other, dup_other)
    db_excl = sum(db_groups.values())
    other_excl = sum(other_groups.values())
    db_sought = db["identified"] - db["abstract_excluded"]
    other_sought = other["identified"]
    abstract_excluded = db["abstract_excluded"]
    sought = db_sought + other_sought
    excluded = db_excl + other_excl
    tracks = {
        "database": {"identified": db["identified"], "screened": db["identified"],
                     "abstract_excluded": db["abstract_excluded"],
                     "abstract_reasons": {APA_CRIT[c]: db["abstract_reasons"].get(APA_CRIT[c], 0)
                                          for c in ("C1", "C2", "C3", "C4")
                                          if db["abstract_reasons"].get(APA_CRIT[c])},
                     "sought": db_sought, "assessed": db_excl + coded_db,
                     "excluded_groups": db_groups, "excluded_total": db_excl,
                     "included_reports": db["include"], "coded": coded_db,
                     "duplicates": dup_db},
        "other": {"identified": other["identified"],
                  "sources": {k: other["sources"].get(k, 0) for k in
                              ("Meta-analyses / reviews", "Other sources")
                              if other["sources"].get(k)},
                  "sought": other_sought, "assessed": other_excl + coded_other,
                  "excluded_groups": other_groups, "excluded_total": other_excl,
                  "included_reports": other["include"], "coded": coded_other,
                  "duplicates": dup_other},
    }
    return {
        "total": total,
        "sources": {k: src.get(k, 0) for k in
                    ("Database searches", "Meta-analyses / reviews", "Other sources")
                    if src.get(k)},
        "duplicates": 0, "screened": total,
        "abstract_excluded": abstract_excluded,
        "abstract_reasons": {APA_CRIT[c]: crit.get(c, 0) for c in ("C1", "C2", "C3", "C4")
                             if crit.get(c)},
        "sought": sought, "not_retrieved": 0,
        "fulltext_assessed": excluded + coded,
        "fulltext_excluded": excluded,
        "fulltext_reasons": dict(ft_reasons.most_common()),
        "reports_included": reports_included,
        "duplicate_reports": dup_reports,
        "included": coded, "coded": coded,
        "coded_db": coded_db, "coded_other": coded_other,
        "tracks": tracks, "ft_groups": ft_labels,
    }


# --------------------------------------------------------------------------- #
# PRISMA closure (report level) — authoritative flow
# --------------------------------------------------------------------------- #
# Closure box label -> prisma_report.prisma_flow() key
CLOSURE_BOX_KEYS = [
    ("records identified", "identified_records"),
    ("duplicates removed", "duplicates_removed"),
    ("reports screened", "reports"),
    ("abstract excluded", "abstract_excluded"),
    ("sought", "sought_for_retrieval"),
    ("not retrieved", "not_retrieved"),
    ("assessed", "fulltext_assessed"),
    ("full-text excluded", "fulltext_excluded"),
    ("included studies", "included_studies"),
]


def parse_closure_md(path):
    """Parse the three tables of ``Scripts/PRISMA_CLOSURE.md`` by heading."""
    out = {"boxes": collections.OrderedDict(),
           "abstract_reasons": collections.OrderedDict(),
           "fulltext_reasons": collections.OrderedDict(),
           "text": ""}
    if not os.path.isfile(path):
        return out
    text = open(path, encoding="utf-8", errors="replace").read()
    out["text"] = text
    target = "boxes"
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            heading = s.lstrip("# ").strip().lower()
            if "abstract exclusion" in heading:
                target = "abstract_reasons"
            elif "full-text exclusion" in heading:
                target = "fulltext_reasons"
            elif "prisma flow" in heading:
                target = "boxes"
            continue
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 2 or not cells[0] or set(cells[0]) <= set("-: "):
            continue
        nums = re.findall(r"-?\d[\d,]*", cells[-1])
        if not nums:
            continue
        label = re.sub(r"\*+", "", cells[0]).strip()
        out[target][label] = int(nums[-1].replace(",", ""))
    for key in ("boxes", "abstract_reasons", "fulltext_reasons"):
        out[key].pop("Box", None)
        out[key].pop("Reason", None)
    return out


def _flow_from_closure(closure):
    boxes = {re.sub(r"\*+", "", k).strip().lower(): v
             for k, v in (closure.get("boxes") or {}).items()}

    def g(*needles):
        best = None
        for label, n in boxes.items():
            if all(nd in label for nd in needles):
                if best is None or len(label) < len(best[0]):
                    best = (label, n)
        return best[1] if best else None

    return {
        "identified_records": g("records identified") or g("identified"),
        "duplicates_removed": g("duplicates removed") or 0,
        "reports": g("reports screened") or g("screened"),
        "abstract_excluded": g("abstract excluded"),
        "abstract_excluded_by_reason": dict(closure.get("abstract_reasons") or {}),
        "sought_for_retrieval": g("sought"),
        "not_retrieved": g("not retrieved") or 0,
        "fulltext_assessed": g("assessed"),
        "fulltext_excluded": g("full-text excluded") or g("fulltext excluded"),
        "fulltext_excluded_by_reason": dict(closure.get("fulltext_reasons") or {}),
        "included_studies": g("included studies"),
        "included_records": None,
        "duplicate_included_records": None,
        "extraction": {},
        "_source": "Scripts/PRISMA_CLOSURE.md",
    }


def _flow_from_rowlevel(computed):
    db = computed["tracks"]["database"]
    return {
        "identified_records": computed.get("total"),
        "duplicates_removed": 0,
        "reports": computed.get("total"),
        "abstract_excluded": computed.get("abstract_excluded"),
        "abstract_excluded_by_reason": dict(computed.get("abstract_reasons") or {}),
        "sought_for_retrieval": computed.get("sought"),
        "not_retrieved": 0,
        "fulltext_assessed": computed.get("fulltext_assessed"),
        "fulltext_excluded": computed.get("fulltext_excluded"),
        "fulltext_excluded_by_reason": dict(db.get("excluded_groups") or {}),
        "included_studies": computed.get("included"),
        "included_records": computed.get("reports_included"),
        "duplicate_included_records": computed.get("duplicate_reports"),
        "extraction": {},
        "_source": "master_dataset.xlsx (row-level fallback; report-level closure unavailable)",
    }


def load_prisma(closure_path):
    """Authoritative report-level flow, cross-checked against the closure markdown."""
    closure = parse_closure_md(closure_path)
    flow, source = None, ""
    try:
        if SCRIPTS not in sys.path:
            sys.path.insert(0, SCRIPTS)
        import prisma_report
        flow = prisma_report.prisma_flow()
        source = "Scripts/prisma_report.py (generator of Scripts/PRISMA_CLOSURE.md)"
    except Exception as exc:  # pragma: no cover
        print("WARNING: prisma_report.prisma_flow failed:", exc)

    disagreements = []
    if flow:
        low = {re.sub(r"\*+", "", k).strip().lower(): v
               for k, v in (closure.get("boxes") or {}).items()}
        for label, key in CLOSURE_BOX_KEYS:
            if flow.get(key) is None:
                continue
            match = next((cv for cl, cv in low.items() if label in cl), None)
            if match is not None and match != flow[key]:
                disagreements.append((label, match, flow[key]))
        for ckey, fkey in (("abstract_reasons", "abstract_excluded_by_reason"),
                           ("fulltext_reasons", "fulltext_excluded_by_reason")):
            cd = closure.get(ckey) or {}
            fd = flow.get(fkey) or {}
            for label, n in cd.items():
                if label in fd and fd[label] != n:
                    disagreements.append((label, n, fd[label]))
        flow["_source"] = source
    elif closure.get("boxes"):
        flow = _flow_from_closure(closure)
    else:
        computed = prisma_from_master() or {"tracks": {"database": {}, "other": {}}}
        flow = _flow_from_rowlevel(computed)
    return flow, closure, disagreements


# --------------------------------------------------------------------------- #
# specification rows + pooling
# --------------------------------------------------------------------------- #
def _spec_rows():
    rows = []
    for fname, layer in (("meta_specs.csv", "meta"), ("sdt_specs.csv", "sdt")):
        path = os.path.join(TEMP, fname)
        if not os.path.isfile(path):
            continue
        for r in read_csv_dicts(path):
            eff, var = _f(r.get("effect")), _f(r.get("var"))
            if eff is None or var is None or var <= 0:
                continue
            ru = (r.get("r_used") or "").strip()
            if layer == "meta" and ru not in ("", "0.5"):
                continue
            n = _f(r.get("n_total"))
            if n is None:
                n1, n2 = _f(r.get("n1")), _f(r.get("n2"))
                n = (n1 + n2) if (n1 is not None and n2 is not None) else None
            rows.append({
                "layer": layer,
                "spec": (r.get("spec") or "").strip(),
                "exclusions": (r.get("exclusions") or "on").strip().lower(),
                "dv_domain": (r.get("dv_domain") or "").strip(),
                "effect": eff, "var": var, "n": n,
                "unit": (r.get("study_key") or "") + "|" + (r.get("experiment_id") or ""),
            })
    return rows


def pool(rows):
    """DL pooled estimate + k + studies + summed participants over distinct units."""
    if not rows:
        return None
    res = dl_random([r["effect"] for r in rows], [r["var"] for r in rows])
    if not res or not res["k"]:
        return None
    per_unit = {}
    for r in rows:
        if r["n"] is not None:
            per_unit[r["unit"]] = max(per_unit.get(r["unit"], 0.0), r["n"])
    res["n_studies"] = len({r["unit"].split("|")[0] for r in rows})
    res["N"] = int(round(sum(per_unit.values()))) if per_unit else None
    res["n_units_with_N"] = len(per_unit)
    res["n_units"] = len({r["unit"] for r in rows})
    return res


def pooled_rows(specs, rows):
    """One row per layer x spec x exclusions x scope."""
    out = []
    for layer in ("meta", "sdt"):
        pool_rows = [r for r in rows if r["layer"] == layer]
        for spec in specs:
            for exc in ("on", "off"):
                sel = [r for r in pool_rows if r["spec"] == spec and r["exclusions"] == exc]
                for scope in ("unrestricted", "restricted"):
                    use = sel if scope == "unrestricted" else [
                        r for r in sel if r["dv_domain"] in SUSCEPTIBILITY]
                    res = pool(use)
                    if not res:
                        continue
                    out.append({
                        "layer": layer, "spec": spec, "exclusions": exc, "scope": scope,
                        "k": res["k"], "n_studies": res["n_studies"], "N": res["N"],
                        "g": res["est"], "lo": res["lo"], "hi": res["hi"],
                        "tau2": res["tau2"], "i2": res["i2"],
                        "n_units": res["n_units"], "n_units_with_N": res["n_units_with_N"],
                    })
    return out


# --------------------------------------------------------------------------- #
# Table 1 — study characteristics from Extraction/*.json
# --------------------------------------------------------------------------- #
def _schema_labels():
    path = os.path.join(SCRIPTS, "coding_web", "schema.json")
    labels = {}
    try:
        s = json.load(open(path, encoding="utf-8"))
    except Exception:
        return labels
    for grp in ("common_fields", "dv_fields"):
        for f in s.get(grp) or []:
            name = f.get("name")
            if not name:
                continue
            opts = {}
            for o in f.get("options") or []:
                if isinstance(o, dict) and "value" in o:
                    opts[str(o["value"])] = str(o.get("label", o["value"]))
            labels[name] = opts
    return labels


def _val(o, field):
    v = o.get(field)
    if isinstance(v, list):
        return v
    if v in (None, ""):
        return []
    return [v]


def build_table1():
    labels = _schema_labels()

    def lab(field, v):
        m = labels.get(field) or {}
        return m.get(str(v), str(v))

    files = sorted(glob.glob(os.path.join(EXTRACTION, "*.json")))
    keys, objects = set(), []
    for p in files:
        k = os.path.splitext(os.path.basename(p))[0]
        if k in ("harmonizer_template", "_harmonizer_template"):
            continue
        keys.add(k)
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        d = d if isinstance(d, list) else [d]
        for o in d:
            if isinstance(o, dict):
                objects.append((k, o))

    def dist(field):
        c = collections.Counter()
        for _, o in objects:
            for v in _val(o, field):
                if str(v).strip():
                    c[lab(field, v)] += 1
        return c

    ns = [_f(o.get("N_Analyzed")) for _, o in objects]
    ns = [x for x in ns if x is not None]
    years = [_f(o.get("Year")) for _, o in objects]
    years = [int(y) for y in years if y is not None]
    intv = collections.Counter()
    for _, o in objects:
        for v in set(_val(o, "Intervention_Type")):
            if str(v).strip():
                intv[lab("Intervention_Type", v)] += 1
    dom = collections.Counter()
    for _, o in objects:
        for d in o.get("DV_Metrics") or []:
            if isinstance(d, dict):
                for v in _val(d, "dv_domain"):
                    if str(v).strip():
                        dom[lab("dv_domain", v)] += 1

    summary = [
        ("Sample", "Studies (k)", f"{len(keys):,}"),
        ("Sample", "Experiments (coded)", f"{len(objects):,}"),
        ("Sample", "Participants (sum of analysed N)", f"{int(round(sum(ns))):,}"),
        ("Sample", "Experiments with analysable N", f"{len(ns):,}"),
    ]
    if years:
        ys = sorted(years)
        summary += [("Publication year", "Median", str(ys[len(ys) // 2])),
                    ("Publication year", "Range", f"{ys[0]}–{ys[-1]}"),
                    ("Publication year", "Experiments with year", f"{len(ys):,}")]
    for field, section in (("Age_Group", "Age group"), ("Setting", "Setting"),
                           ("within_between", "Design"),
                           ("control_group_type", "Control group type"),
                           ("WEIRD_Western", "Western sample")):
        for k, v in dist(field).most_common():
            summary.append((section, k, f"{v:,}"))
    for k, v in intv.most_common():
        summary.append(("Intervention type", k, f"{v:,}"))
    for k, v in dom.most_common():
        summary.append(("Outcome domain (contrasts)", k, f"{v:,}"))

    per_study = []
    for k, o in sorted(objects, key=lambda x: x[0].lower()):
        itypes = "; ".join(sorted({lab("Intervention_Type", v)
                                   for v in _val(o, "Intervention_Type") if str(v).strip()}))
        doms = sorted({lab("dv_domain", v)
                       for d in (o.get("DV_Metrics") or []) if isinstance(d, dict)
                       for v in _val(d, "dv_domain") if str(v).strip()})
        per_study.append({
            "Study key": k,
            "Study_ID": o.get("Study_ID") or "",
            "Year": _i(o.get("Year")),
            "Design": lab("within_between", o.get("within_between")),
            "Setting": lab("Setting", o.get("Setting")),
            "Control": lab("control_group_type", o.get("control_group_type")),
            "Age group": lab("Age_Group", o.get("Age_Group")),
            "Intervention type": itypes,
            "N analysed": _i(o.get("N_Analyzed")),
            "Outcome domains": "; ".join(doms),
            # Transparency marker only: fully-within records stay in the descriptive
            # study-characteristics table but are excluded from the pooled Tables 2-4
            # by default (the spec tables carry the filter).
            "Fully within": "yes" if (SC is not None and SC.is_fully_within(o)) else "no",
        })
    return summary, per_study


# --------------------------------------------------------------------------- #
# Tables 2–6 from the frozen Results workbook (+ spec CSVs for N/scope)
# --------------------------------------------------------------------------- #
def build_pooled_sheets(rows):
    specs = BASE_SPECS
    all_rows = pooled_rows(specs, rows)
    derived = pooled_rows(DERIVED_SPECS, rows)

    def table(spec_set):
        return [r for r in all_rows + derived if r["spec"] in spec_set]

    t2 = table({"post_all", "post_cred"})
    t3 = table({"persistence_all", "persistence_cred"})
    t4 = table({"did_all", "did_cred"})
    return t2, t3, t4, derived


def table_pub_bias(path):
    rows = read_sheet_dicts(path, "Publication_Bias")
    out = []
    for r in rows:
        out.append({
            "Layer": LAYER_LABEL.get(str(r.get("layer")), r.get("layer")),
            "Specification": SPEC_LABEL.get(str(r.get("spec")), r.get("spec")),
            "Exclusions": r.get("exclusions"),
            "k": _i(r.get("k")),
            "Studies": _i(r.get("n_studies")),
            "Egger intercept": fmt(r.get("egger_intercept"), 3),
            "Egger SE": fmt(r.get("egger_se"), 3),
            "Egger t": fmt(r.get("egger_t"), 2),
            "Egger p": fmt_p(r.get("egger_p")),
            "PET estimate": fmt(r.get("pet"), 3),
            "PET p": fmt_p(r.get("pet_p")),
            "PEESE estimate": fmt(r.get("peese"), 3),
            "PEESE p": fmt_p(r.get("peese_p")),
            "Preferred": r.get("pet_peese_prefer") or "",
            "Preferred estimate": fmt(r.get("pet_peese_est"), 3),
            "Preferred 95% CI": fmt_ci(r.get("pet_peese_lo"), r.get("pet_peese_hi"), 3),
            "Trim-and-fill k0": _i(r.get("tf_k0")),
            "Trim-and-fill adjusted": fmt(r.get("tf_adj_est"), 3),
            "Trim-and-fill status": r.get("tf_status") or "",
            "p-curve status": r.get("pcurve_status") or "",
        })
    return out


def table_moderators(path):
    out = []
    for r in read_sheet_dicts(path, "Moderator"):
        out.append({
            "Moderator": r.get("moderator") or "",
            "Level": r.get("level_label") or r.get("level") or "",
            "Layer": LAYER_LABEL.get(str(r.get("layer")), r.get("layer")),
            "Specification": SPEC_LABEL.get(str(r.get("spec")), r.get("spec")),
            "Exclusions": r.get("exclusions"),
            "k": _i(r.get("k")), "Studies": _i(r.get("n_studies")),
            "Estimate": fmt(r.get("est"), 3),
            "95% CI": fmt_ci(r.get("lo"), r.get("hi"), 3),
            "p": fmt_p(r.get("p")) if str(r.get("p") or "").strip() else "",
        })
    for sheet, key in (("Domain", "dv_domain"), ("Metric_Family", "metric"),
                       ("Intervention_Type", "intervention_type")):
        try:
            rows = read_sheet_dicts(path, sheet)
        except Exception:
            continue
        for r in rows:
            out.append({
                "Moderator": sheet.replace("_", " "),
                "Level": r.get(key) or "",
                "Layer": LAYER_LABEL.get(str(r.get("layer")), r.get("layer")),
                "Specification": SPEC_LABEL.get(str(r.get("spec")), r.get("spec")),
                "Exclusions": r.get("exclusions"),
                "k": _i(r.get("k")), "Studies": _i(r.get("n_studies")),
                "Estimate": fmt(r.get("est"), 3),
                "95% CI": fmt_ci(r.get("lo"), r.get("hi"), 3),
                "tau2": fmt(r.get("tau2"), 4), "I2": fmt(r.get("i2"), 1),
            })
    return out


# --------------------------------------------------------------------------- #
# PRISMA 2020 single-column flow (report level, closure-driven).
# Visual style adapted from the Coding Studio's ``apa_export.prisma_png``; the
# layout is single-column because report-level counting sends records from every
# source through the same title/abstract screen (the two-track studio diagram
# cannot represent the 421 abstract exclusions among non-database reports).
# --------------------------------------------------------------------------- #
def _pil_font(size):
    from PIL import ImageFont
    for name in ("DejaVuSans.ttf", "arial.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _scene_wrap(draw, text, font, maxw):
    out = []
    for para in str(text).split("\n"):
        words = para.split()
        if not words:
            out.append("")
            continue
        cur = ""
        for w in words:
            t = (cur + " " + w).strip()
            if draw.textlength(t, font=font) <= maxw:
                cur = t
            else:
                if cur:
                    out.append(cur)
                cur = w
        if cur:
            out.append(cur)
    return out


def prisma_scene(flow):
    """Build the PRISMA scene primitives from a report-level flow."""
    from PIL import Image, ImageDraw
    W = 1240
    f_title = _pil_font(24)
    f_head = _pil_font(19)
    f_box = _pil_font(20)
    f_small = _pil_font(18)
    f_phase = _pil_font(18)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    prims = []

    def rect(x, y, w, h, thick=False):
        prims.append(("rect", x, y, w, h, thick))

    def line(x1, y1, x2, y2):
        prims.append(("line", x1, y1, x2, y2))

    def poly(pts):
        prims.append(("poly", list(pts)))

    def text(x, y, s, size, anchor="start", bold=False):
        prims.append(("text", x, y, s, size, anchor, bold))

    def vtext(cx, cy, s, size):
        prims.append(("vtext", cx, cy, s, size))

    def box(x, y, w, h, txt, size=f_box.size, thick=False):
        rect(x, y, w, h, thick)
        lines = _scene_wrap(probe, txt, _pil_font(size), w - 18)
        lh = size + 6
        cy = y + (h - len(lines) * lh) / 2
        for ln in lines:
            text(x + w / 2, cy + size * 0.8, ln, size, anchor="middle", bold=thick)
            cy += lh

    def panel(x, y, w, title, items):
        lines = []
        for label, n in items:
            lines += _scene_wrap(probe, f"{label} (n = {n:,})", f_small, w - 30)
        h = 18 + (f_box.size + 6) + len(lines) * (f_small.size + 6)
        rect(x, y, w, h)
        text(x + 14, y + 9 + f_box.size * 0.8, title, f_box.size, bold=True)
        cy = y + 9 + f_box.size + 6
        for ln in lines:
            text(x + 24, cy + f_small.size * 0.8, ln, f_small.size)
            cy += f_small.size + 6
        return h

    def arrow(x1, y1, x2, y2):
        line(x1, y1, x2, y2)
        ang = math.atan2(y2 - y1, x2 - x1)
        L, Wd = 14, 6
        p2 = (x2 - L * math.cos(ang) + Wd * math.sin(ang),
              y2 - L * math.sin(ang) - Wd * math.cos(ang))
        p3 = (x2 - L * math.cos(ang) - Wd * math.sin(ang),
              y2 - L * math.sin(ang) + Wd * math.cos(ang))
        poly([(x2, y2), p2, p3])

    total = flow.get("identified_records")
    dup = flow.get("duplicates_removed") or 0
    screened = flow.get("reports")
    if screened is None and total is not None:
        screened = total - dup
    abstract = flow.get("abstract_excluded")
    sought = flow.get("sought_for_retrieval")
    notret = flow.get("not_retrieved") or 0
    assessed = flow.get("fulltext_assessed")
    if assessed is None:
        assessed = sought
    ft = flow.get("fulltext_excluded")
    included = flow.get("included_studies")
    abs_reasons = list((flow.get("abstract_excluded_by_reason") or {}).items())
    ft_reasons = list((flow.get("fulltext_excluded_by_reason") or {}).items())

    def n(x):
        return "–" if x is None else f"{x:,}"

    col_x, col_w = 70, 360
    right_x = 500
    right_w = W - right_x - 40
    gap, h_box, h_head = 46, 96, 54

    text(40, 30 + f_title.size * 0.8, "PRISMA 2020 flow diagram",
         f_title.size, bold=True)
    y = 70
    rect(col_x, y, right_x + right_w - col_x, h_head)
    text(col_x + 14, y + 13 + f_head.size * 0.8,
         "Identification of studies via databases, registers and other methods",
         f_head.size, bold=True)
    y += h_head + 44

    id_y = y
    box(col_x, id_y, col_w, h_box, f"Records identified\n(n = {n(total)})")
    box(right_x, id_y, right_w, h_box, f"Duplicates removed\n(n = {n(dup)})")
    arrow(col_x + col_w, id_y + h_box / 2, right_x, id_y + h_box / 2)
    y = id_y + h_box + gap
    arrow(col_x + col_w / 2, id_y + h_box, col_x + col_w / 2, y)

    scr_y = y
    box(col_x, scr_y, col_w, h_box,
        f"Records screened\n(title/abstract)\n(n = {n(screened)})")
    hp_abs = panel(right_x, scr_y, right_w, "Records excluded", abs_reasons)
    arrow(col_x + col_w, scr_y + h_box / 2, right_x, scr_y + h_box / 2)
    y = max(scr_y + h_box, scr_y + hp_abs) + gap
    arrow(col_x + col_w / 2, scr_y + h_box, col_x + col_w / 2, y)

    sot_y = y
    box(col_x, sot_y, col_w, h_box,
        f"Reports sought for retrieval\n(n = {n(sought)})")
    box(right_x, sot_y, right_w, h_box, f"Reports not retrieved\n(n = {n(notret)})")
    arrow(col_x + col_w, sot_y + h_box / 2, right_x, sot_y + h_box / 2)
    y = sot_y + h_box + gap
    arrow(col_x + col_w / 2, sot_y + h_box, col_x + col_w / 2, y)

    ass_y = y
    box(col_x, ass_y, col_w, h_box,
        f"Reports assessed for eligibility\n(n = {n(assessed)})")
    hp_ft = panel(right_x, ass_y, right_w, "Reports excluded", ft_reasons)
    arrow(col_x + col_w, ass_y + h_box / 2, right_x, ass_y + h_box / 2)
    y = max(ass_y + h_box, ass_y + hp_ft) + gap
    arrow(col_x + col_w / 2, ass_y + h_box, col_x + col_w / 2, y)

    inc_y = y
    box(col_x, inc_y, col_w, h_box, f"Studies included\n(n = {n(included)})", thick=True)
    y = inc_y + h_box

    vtext(30, id_y + h_box / 2, "Identification", f_phase.size)
    vtext(30, scr_y + h_box / 2, "Screening", f_phase.size)
    vtext(30, (sot_y + ass_y + h_box) / 2, "Eligibility", f_phase.size)
    vtext(30, inc_y + h_box / 2, "Included", f_phase.size)

    ex = flow.get("extraction") or {}
    notes = []
    if flow.get("included_records") is not None:
        notes.append(
            f"Included records: {n(flow.get('included_records'))}; "
            f"duplicate/non-independent included records: "
            f"{n(flow.get('duplicate_included_records'))}.")
    if ex.get("distinct_coded_papers") is not None:
        notes.append(
            f"Coded papers: {n(ex.get('distinct_coded_papers'))}; "
            f"coded papers in included reports: "
            f"{n(ex.get('coded_papers_in_included_reports'))}; "
            f"included reports without a coding: "
            f"{n(ex.get('included_reports_without_a_coding'))}.")
    py = y + 22
    for note in notes:
        text(40, py + f_small.size * 0.8, note, f_small.size)
        py += f_small.size + 10
    H = int(py + 18)
    return W, H, prims


def render_scene_png(W, H, prims, path):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    fonts = {}

    def font(size):
        if size not in fonts:
            fonts[size] = _pil_font(size)
        return fonts[size]

    for p in prims:
        kind = p[0]
        if kind == "rect":
            _, x, y, w, h, thick = p
            d.rectangle([x, y, x + w, y + h], outline="black",
                        width=3 if thick else 2, fill="white")
        elif kind == "line":
            _, x1, y1, x2, y2 = p
            d.line([x1, y1, x2, y2], fill="black", width=2)
        elif kind == "poly":
            d.polygon(p[1], fill="black")
        elif kind == "text":
            _, x, y, s, size, anchor, bold = p
            d.text((x, y), str(s), font=font(size), fill="black",
                   anchor=("ms" if anchor == "middle" else "ls"))
        elif kind == "vtext":
            _, cx, cy, s, size = p
            f = font(size)
            tw = int(d.textlength(s, font=f)) + 8
            th = size + 8
            timg = Image.new("RGBA", (tw, th), (255, 255, 255, 0))
            ImageDraw.Draw(timg).text((4, 4), s, font=f, fill="black")
            timg = timg.rotate(90, expand=True)
            img.paste(timg, (int(cx - timg.width / 2), int(cy - timg.height / 2)), timg)
    img.save(path, "PNG")


def render_scene_svg(W, H, prims, path):
    def esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
             f'viewBox="0 0 {W} {H}">',
             f'<rect x="0" y="0" width="{W}" height="{H}" fill="white"/>']
    for p in prims:
        kind = p[0]
        if kind == "rect":
            _, x, y, w, h, thick = p
            parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
                         f'fill="white" stroke="black" stroke-width="{3 if thick else 2}"/>')
        elif kind == "line":
            _, x1, y1, x2, y2 = p
            parts.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                         f'stroke="black" stroke-width="2"/>')
        elif kind == "poly":
            pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in p[1])
            parts.append(f'<polygon points="{pts}" fill="black"/>')
        elif kind == "text":
            _, x, y, s, size, anchor, bold = p
            ta = ' text-anchor="middle"' if anchor == "middle" else ''
            wgt = ' font-weight="bold"' if bold else ''
            parts.append(f'<text x="{x:.1f}" y="{y:.1f}" font-family="DejaVu Sans, Arial, '
                         f'Helvetica, sans-serif" font-size="{size}"{ta}{wgt}>{esc(s)}</text>')
        elif kind == "vtext":
            _, cx, cy, s, size = p
            parts.append(f'<text x="{cx:.1f}" y="{cy:.1f}" font-family="DejaVu Sans, Arial, '
                         f'Helvetica, sans-serif" font-size="{size}" text-anchor="middle" '
                         f'transform="rotate(-90 {cx:.1f} {cy:.1f})">{esc(s)}</text>')
    parts.append("</svg>")
    atomic_write_text(path, "\n".join(parts) + "\n")


def draw_prisma(flow, png_path, svg_path):
    """Single-column PRISMA 2020 flow (report level, from the closure)."""
    W, H, prims = prisma_scene(flow)
    render_scene_png(W, H, prims, png_path)
    render_scene_svg(W, H, prims, svg_path)
    return png_path, svg_path


def prisma_ascii(flow):
    def n(x):
        return "–" if x is None else f"{x:,}"
    lines = ["PRISMA 2020 flow (report level, ASCII)", "=" * 78,
             "[Identification]",
             f"  Records identified: {n(flow.get('identified_records'))}",
             f"  Duplicates removed: {n(flow.get('duplicates_removed'))}",
             "[Screening]",
             f"  Reports screened (title/abstract): {n(flow.get('reports'))}",
             f"  Records excluded at title/abstract: {n(flow.get('abstract_excluded'))}"]
    for k, v in (flow.get("abstract_excluded_by_reason") or {}).items():
        lines.append(f"      - {k}: {v:,}")
    lines += ["[Eligibility]",
              f"  Reports sought for retrieval: {n(flow.get('sought_for_retrieval'))}",
              f"  Reports not retrieved: {n(flow.get('not_retrieved'))}",
              f"  Reports assessed for eligibility: {n(flow.get('fulltext_assessed'))}",
              f"  Reports excluded: {n(flow.get('fulltext_excluded'))}"]
    for k, v in (flow.get("fulltext_excluded_by_reason") or {}).items():
        lines.append(f"      - {k}: {v:,}")
    lines += ["[Included]",
              f"  Studies included in synthesis: {n(flow.get('included_studies'))}"]
    ex = flow.get("extraction") or {}
    if flow.get("included_records") is not None:
        lines.append(
            f"  Included records: {n(flow.get('included_records'))}; "
            f"duplicate/non-independent included records: "
            f"{n(flow.get('duplicate_included_records'))}")
    if ex:
        lines.append(
            f"  Coded papers: {n(ex.get('distinct_coded_papers'))}; "
            f"coded papers in included reports: "
            f"{n(ex.get('coded_papers_in_included_reports'))}; "
            f"included reports without a coding: "
            f"{n(ex.get('included_reports_without_a_coding'))}")
    return "\n".join(lines)


def prisma_mermaid(flow):
    def n(x):
        return "–" if x is None else f"{x:,}"
    return "\n".join([
        "```mermaid", "flowchart TD",
        f'  A["Records identified<br/>n = {n(flow.get("identified_records"))}"] '
        f'--> B["Records screened (title/abstract)<br/>n = {n(flow.get("reports"))}"]',
        f'  A --> DUP["Duplicates removed<br/>n = {n(flow.get("duplicates_removed"))}"]',
        f'  B --> C["Records excluded<br/>n = {n(flow.get("abstract_excluded"))}"]',
        f'  B --> D["Reports sought for retrieval<br/>n = {n(flow.get("sought_for_retrieval"))}"]',
        f'  D --> NR["Reports not retrieved<br/>n = {n(flow.get("not_retrieved"))}"]',
        f'  D --> E["Reports assessed for eligibility<br/>n = {n(flow.get("fulltext_assessed"))}"]',
        f'  E --> F["Reports excluded<br/>n = {n(flow.get("fulltext_excluded"))}"]',
        f'  E --> G["Studies included<br/>n = {n(flow.get("included_studies"))}"]',
        "```"])


# --------------------------------------------------------------------------- #
# markdown
# --------------------------------------------------------------------------- #
def md_table(columns, rows):
    out = ["| " + " | ".join(str(c) for c in columns) + " |",
           "| " + " | ".join("---" for _ in columns) + " |"]
    for r in rows:
        if isinstance(r, dict):
            out.append("| " + " | ".join("" if r.get(c) is None else str(r.get(c))
                                         for c in columns) + " |")
        else:
            out.append("| " + " | ".join("" if v is None else str(v) for v in r) + " |")
    return "\n".join(out)


def pooled_md(rows):
    cols = ["Layer", "Specification", "Exclusions", "Scope", "k", "Studies", "N (participants)",
            "g", "95% CI", "I² (%)", "τ²"]
    out = []
    for r in rows:
        out.append({
            "Layer": LAYER_LABEL.get(r["layer"], r["layer"]),
            "Specification": SPEC_LABEL.get(r["spec"], r["spec"]),
            "Exclusions": r["exclusions"],
            "Scope": r["scope"],
            "k": f"{r['k']:,}",
            "Studies": f"{r['n_studies']:,}",
            "N (participants)": f"{r['N']:,}" if r["N"] else "–",
            "g": fmt(r["g"], 3),
            "95% CI": fmt_ci(r["lo"], r["hi"], 3),
            "I² (%)": fmt(r["i2"], 1),
            "τ²": fmt(r["tau2"], 4),
        })
    return md_table(cols, out)


# --------------------------------------------------------------------------- #
# docx (reuse repo APA tooling when present)
# --------------------------------------------------------------------------- #
def build_docx(flow, table1_summary, tables, pub, mods, fig_png, out_path):
    try:
        sys.path.insert(0, os.path.join(SCRIPTS, "coding_web"))
        import apa_export
    except Exception:
        return None
    n = lambda x: "–" if x is None else f"{x:,}"  # noqa: E731
    ctx = {"title": "Misinformation-intervention meta-analysis: reporting tables",
           "generated": "", "_table_no": 1, "_figure_no": 1,
           "methods": [{"heading": "Study selection", "paragraphs": [
               f"{n(flow.get('identified_records'))} records were identified; "
               f"{n(flow.get('duplicates_removed'))} duplicates were removed; "
               f"{n(flow.get('abstract_excluded'))} records were excluded at title/abstract "
               f"screening; {n(flow.get('fulltext_assessed'))} reports were assessed for "
               f"eligibility; {n(flow.get('fulltext_excluded'))} were excluded; and "
               f"{n(flow.get('included_studies'))} studies were included in the synthesis."]}],
           "results": []}
    res = ctx["results"]
    sel = {"heading": "Study selection", "paragraphs": [
        f"{n(flow.get('included_studies'))} studies were included in the synthesis."],
        "tables": []}
    if fig_png and os.path.exists(fig_png):
        sel["figures"] = [{"title": "PRISMA 2020 flow diagram", "path": fig_png}]
    res.append(sel)
    res.append({"heading": "Study characteristics", "paragraphs": [], "tables": [
        {"title": "Study characteristics", "columns": ["Section", "Characteristic", "Value"],
         "rows": [{"Section": s, "Characteristic": c, "Value": v}
                  for s, c, v in table1_summary]}]})
    for title, rows in tables:
        res.append({"heading": title, "paragraphs": [], "tables": [
            {"title": title, "columns": list(rows[0].keys()), "rows": rows}]})
    for title, rows in (("Publication-bias tests", pub), ("Moderator analyses", mods)):
        if rows:
            res.append({"heading": title, "paragraphs": [], "tables": [
                {"title": title, "columns": list(rows[0].keys()), "rows": rows}]})
    apa_export.build_docx(ctx, out_path)
    return out_path


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    if SC is not None:
        SC.include_fully_within_cli()
        SC.announce_fully_within("manuscript report")
        # The pooled Tables 2-4 read temp/meta_specs.csv + temp/sdt_specs.csv; make
        # sure they match the current switch (rebuilds when the marker differs).
        SC.ensure_specs()
    flow, closure, disagreements = load_prisma(os.path.join(SCRIPTS, "PRISMA_CLOSURE.md"))
    print("PRISMA source:", flow.get("_source"))
    print("PRISMA flow:", {k: flow.get(k) for k in
                           ("identified_records", "duplicates_removed", "reports",
                            "abstract_excluded", "sought_for_retrieval", "not_retrieved",
                            "fulltext_assessed", "fulltext_excluded", "included_studies")})

    # ---- tables
    spec_rows = _spec_rows()
    t2, t3, t4, derived = build_pooled_sheets(spec_rows)
    t1_summary, t1_studies = build_table1()
    results_wb = os.path.join(RESULTS, "Analysis_Suite_Results.xlsx")
    pub = table_pub_bias(results_wb)
    mods = table_moderators(results_wb)

    md_tables = {
        "Table_2_Posttest": pooled_md(t2),
        "Table_3_Persistence": pooled_md(t3),
        "Table_4_DiD": pooled_md(t4),
        "Table_S1_Derived_Specs": pooled_md(derived),
        "Table_5_Publication_Bias": md_table(
            list(pub[0].keys()) if pub else ["col"], pub),
        "Table_6_Moderators": md_table(
            list(mods[0].keys()) if mods else ["col"], mods),
    }

    # ---- workbook
    def md_rows(rows):
        return [{"Layer": LAYER_LABEL.get(r["layer"], r["layer"]),
                 "Specification": SPEC_LABEL.get(r["spec"], r["spec"]),
                 "Exclusions": r["exclusions"], "Scope": r["scope"],
                 "k": r["k"], "Studies": r["n_studies"], "N (participants)": r["N"],
                 "g": None if r["g"] is None else round(r["g"], 4),
                 "95% CI low": None if r["lo"] is None else round(r["lo"], 4),
                 "95% CI high": None if r["hi"] is None else round(r["hi"], 4),
                 "I2": None if r["i2"] is None else round(r["i2"], 3),
                 "tau2": None if r["tau2"] is None else round(r["tau2"], 5)}
                for r in rows]

    def fill(wb):
        sheets = [
            ("Table_1_Study_Characteristics", ["Section", "Characteristic", "Value"], t1_summary),
            ("Table_1b_Studies", list(t1_studies[0].keys()) if t1_studies else ["Study key"], t1_studies),
            ("Table_2_Posttest", list(md_rows(t2)[0].keys()) if t2 else ["col"], md_rows(t2)),
            ("Table_3_Persistence", list(md_rows(t3)[0].keys()) if t3 else ["col"], md_rows(t3)),
            ("Table_4_DiD", list(md_rows(t4)[0].keys()) if t4 else ["col"], md_rows(t4)),
            ("Table_S1_Derived_Specs", list(md_rows(derived)[0].keys()) if derived else ["col"], md_rows(derived)),
            ("Table_5_Publication_Bias", list(pub[0].keys()) if pub else ["col"], pub),
            ("Table_6_Moderators", list(mods[0].keys()) if mods else ["col"], mods),
        ]
        for name, cols, rows in sheets:
            ws = wb.create_sheet(name[:31])
            ws.append(cols)
            for r in rows:
                if isinstance(r, dict):
                    ws.append([r.get(c) for c in cols])
                else:
                    ws.append(list(r))

    out_xlsx = os.path.join(RESULTS, "manuscript_tables.xlsx")
    atomic_save_workbook(out_xlsx, fill)

    # ---- figures
    png = os.path.join(FIGDIR, "PRISMA_2020_flow.png")
    svg = os.path.join(FIGDIR, "PRISMA_2020_flow.svg")
    try:
        draw_prisma(flow, png, svg)
        fig_ok = True
    except Exception as exc:  # pragma: no cover
        print("WARNING: PRISMA figure failed:", exc)
        fig_ok = False
    ascii_fig = prisma_ascii(flow)
    mermaid_fig = prisma_mermaid(flow)
    atomic_write_text(os.path.join(SCRIPTS, "PRISMA_flow_ascii.txt"), ascii_fig + "\n")
    atomic_write_text(os.path.join(SCRIPTS, "PRISMA_flow.mmd"), mermaid_fig + "\n")

    # ---- report
    report = build_report(flow, closure, disagreements,
                          t1_summary, t1_studies, md_tables, t2, t3, t4, pub, mods,
                          png, svg, ascii_fig, mermaid_fig)
    out_md = os.path.join(SCRIPTS, "PRISMA_REPORT.md")
    atomic_write_text(out_md, report)

    # ---- docx
    docx_path = os.path.join(RESULTS, "apa_report_phase4.docx")
    try:
        made = build_docx(flow, t1_summary,
                          [("Pooled posttest effects", md_rows(t2)),
                           ("Pooled persistence effects", md_rows(t3)),
                           ("Pooled DiD effects", md_rows(t4))],
                          pub, mods, png, docx_path)
        print("docx:", made)
    except Exception as exc:
        print("WARNING: docx generation failed:", exc)

    print("wrote:", out_xlsx)
    print("wrote:", out_md)
    print("wrote:", png, svg)
    print("closure disagreements:", disagreements)
    return 0


def build_report(flow, closure, disagreements,
                 t1_summary, t1_studies, md_tables, t2, t3, t4, pub, mods,
                 png, svg, ascii_fig, mermaid_fig):
    def n(x):
        return "–" if x is None else f"{x:,}"

    L = []
    L.append("# PRISMA 2020 report — misinformation-intervention meta-analysis")
    L.append("")
    L.append("_Generated by `Scripts/build_manuscript_report.py` from the frozen "
             "`Results/` outputs and the report-level PRISMA closure. No value is "
             "invented: every number is traced to its source._")
    L.append("")

    # ---- provenance
    L.append("## Provenance of the flow numbers")
    L.append("")
    L.append(f"- Primary source: `{flow.get('_source', '')}` — the generator of "
             "`Scripts/PRISMA_CLOSURE.md`.")
    if os.path.isfile(os.path.join(SCRIPTS, "PRISMA_CLOSURE.md")):
        L.append("- Closure document: `Scripts/PRISMA_CLOSURE.md`. Counting is at **report "
                 "level**: master rows are collapsed into reports by DOI / title / "
                 "accession / `Duplicate_Of` before any box is counted, so duplicated "
                 "database records do not inflate the flow.")
    if disagreements:
        L.append("- **Discrepancies between `PRISMA_CLOSURE.md` and the regenerated flow**: "
                 + "; ".join(f"{k}: closure {b:,} vs regenerated {a:,}"
                             for k, b, a in disagreements) + ".")
    else:
        L.append("- `PRISMA_CLOSURE.md` and the regenerated report-level flow agree on every "
                 "box and reason (no discrepancies).")
    L.append("")

    ex = flow.get("extraction") or {}

    # ---- identification
    L.append("## Identification")
    L.append("")
    L.append(f"- Records identified: **{n(flow.get('identified_records'))}**.")
    L.append(f"- Duplicates removed: {n(flow.get('duplicates_removed'))}.")
    L.append(f"- Reports after deduplication: {n(flow.get('reports'))}.")
    L.append("")

    # ---- screening
    L.append("## Screening")
    L.append("")
    L.append(f"- Reports screened (title/abstract): {n(flow.get('reports'))}.")
    L.append(f"- Records excluded at title/abstract: {n(flow.get('abstract_excluded'))}.")
    for k, v in (flow.get("abstract_excluded_by_reason") or {}).items():
        L.append(f"  - {k}: {v:,}.")
    L.append("")

    # ---- eligibility
    L.append("## Eligibility")
    L.append("")
    L.append(f"- Reports sought for retrieval: {n(flow.get('sought_for_retrieval'))}.")
    L.append(f"- Reports not retrieved: {n(flow.get('not_retrieved'))} (sought-but-unobtained "
             "reports are stored as `Fulltext irretrievable` exclusions, so this box is 0).")
    L.append(f"- Reports assessed for eligibility: {n(flow.get('fulltext_assessed'))}.")
    L.append(f"- Reports excluded: {n(flow.get('fulltext_excluded'))}.")
    for k, v in (flow.get("fulltext_excluded_by_reason") or {}).items():
        L.append(f"  - {k}: {v:,}.")
    L.append("")

    # ---- inclusion
    L.append("## Inclusion")
    L.append("")
    L.append(f"- **Studies included in the synthesis: {n(flow.get('included_studies'))}**.")
    if flow.get("included_records") is not None:
        L.append(f"- Included records: {n(flow.get('included_records'))}; duplicate / "
                 f"non-independent included records: "
                 f"{n(flow.get('duplicate_included_records'))}.")
    if ex:
        L.append(f"- Distinct coded papers in `Extraction/`: "
                 f"{n(ex.get('distinct_coded_papers'))}.")
        L.append(f"- Coded papers in an included report: "
                 f"{n(ex.get('coded_papers_in_included_reports'))}.")
        L.append(f"- Included reports without a matching coding: "
                 f"{n(ex.get('included_reports_without_a_coding'))} "
                 "(the reconciliation gap against the analysis set).")
    L.append("")

    # ---- figure
    L.append("## PRISMA 2020 flow diagram")
    L.append("")
    L.append("- PNG (300 dpi): `Results/figures/PRISMA_2020_flow.png`")
    L.append("- SVG: `Results/figures/PRISMA_2020_flow.svg`")
    L.append("- Mermaid: `Scripts/PRISMA_flow.mmd`")
    L.append("- ASCII: `Scripts/PRISMA_flow_ascii.txt`")
    L.append("")
    L.append("```")
    L.append(ascii_fig)
    L.append("```")
    L.append("")
    L.append(mermaid_fig)
    L.append("")

    # ---- tables
    L.append("## Manuscript tables")
    L.append("")
    L.append("Workbook: `Results/manuscript_tables.xlsx` "
             "(sheets: Table_1_Study_Characteristics, Table_1b_Studies, Table_2_Posttest, "
             "Table_3_Persistence, Table_4_DiD, Table_S1_Derived_Specs, "
             "Table_5_Publication_Bias, Table_6_Moderators).")
    L.append("")

    L.append("### Table 1. Study characteristics")
    L.append("")
    L.append(md_table(["Section", "Characteristic", "Value"], t1_summary))
    L.append("")
    L.append(f"Per-study characteristics are in the `Table_1b_Studies` sheet "
             f"({len(t1_studies):,} coded experiments).")
    L.append("")

    L.append("### Table 2. Pooled immediate-posttest effects")
    L.append("")
    L.append(md_tables["Table_2_Posttest"])
    L.append("")
    L.append("### Table 3. Pooled follow-up persistence effects")
    L.append("")
    L.append(md_tables["Table_3_Persistence"])
    L.append("")
    L.append("### Table 4. Pooled pretest-corrected (DiD) effects")
    L.append("")
    L.append(md_tables["Table_4_DiD"])
    L.append("")
    L.append("### Table S1. Derived combined specifications")
    L.append("")
    L.append(md_tables["Table_S1_Derived_Specs"])
    L.append("")
    L.append("### Table 5. Publication-bias tests")
    L.append("")
    L.append(md_tables["Table_5_Publication_Bias"])
    L.append("")
    L.append("### Table 6. Moderator analyses")
    L.append("")
    L.append(md_tables["Table_6_Moderators"])
    L.append("")
    L.append("## Notes")
    L.append("")
    L.append("- Pooled effects are DerSimonian–Laird random-effects estimates; `g` is the "
             "effect size on the harmonised scale (positive = more desirable). `N` is the "
             "sum of analysed participants over distinct study × experiment units "
             "contributing at least one contrast to the specification (repeated outcomes "
             "within a unit counted once); it is blank when no contrast carries an N.")
    L.append("- Restricted scope keeps only misinformation-susceptibility domains "
             "(belief accuracy, credibility, sharing intention, technique recognition); "
             "unrestricted keeps all outcome domains.")
    L.append("- Exclusions `on`/`off` applies or ignores the author exclusion rules recorded "
             "in each dataset's `<Key> - exclusions.csv`.")
    L.append("- Table 5 reports Egger's regression, PET-PEESE and trim-and-fill; the p-curve "
             "column is the frozen status from the analysis suite.")
    L.append("- Table 6 pools each moderator level and the per-domain / per-metric / "
             "per-intervention-type subgroups from the frozen analysis suite.")
    L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    raise SystemExit(main())
