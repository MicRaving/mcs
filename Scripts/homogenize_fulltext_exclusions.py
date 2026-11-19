#!/usr/bin/env python
"""Homogenize the two full-text-exclusion sources (owner decision 2026-11-19).

The master screening workbook and the canonical coding each record full-text
exclusions, and they disagree in both directions:

* the workbook's screening verdicts exclude reports the coding never marked
  (a coded paper can sit on an ``Exclude``-stage report while every record of
  its ``Extraction/<Key>.json`` leaves ``Fulltext_Exclusion_Criterion`` blank);
* the coding's ``Fulltext_Exclusion_Criterion`` field (C1-C4) marks papers the
  screening verdicts never excluded (some sit on ``Include``-stage reports,
  more on ``Abstract excluded``-stage reports, a few on no master row at all).

Approved rule (this script implements exactly this, nothing more):

1. A record is FULL-TEXT-EXCLUDED if EITHER source excludes it (UNION).
2. master -> coding: every coded paper on a master-excluded report gets
   ``Fulltext_Exclusion_Criterion`` set on ALL its records, using the master
   row's criterion mapped to C1-C4 via ``fulltext_lib.FULLTEXT_CRITERIA``.
   When the master row's criterion is blank or does not map to C1-C4 the field
   is left blank and the paper is listed in the anomalies report - a criterion
   is never invented.  A paper that is already partially marked keeps its own
   coded value everywhere: blank records are completed from the paper's own
   codes, an existing non-empty value is never overwritten (a coding-vs-master
   criterion disagreement is reported, not "fixed").
3. coding -> master: every marked paper whose master report is NOT excluded
   gets the master's full-text verdict written the way the Screening tab's
   locked decision writes it (``fulltext_lib._mark_master_locked``):
   ``Fulltext_Verdict = Exclude``, ``Fulltext_Exclusion_Criterion = <label>``,
   ``Fulltext_Reason = <note>``, ``Effective_Verdict = Exclude``.  A marked
   paper with no master row at all is an anomaly - no row is invented.
4. ``Codings/<Key> - SB.json`` / ``- MM.json`` / any other coder file is never
   touched; only canonical ``Extraction/<Key>.json`` and
   ``master_dataset.xlsx`` change.  Before the first ``--apply`` the pristine
   ``master_dataset.xlsx`` and every affected ``Extraction`` file are copied
   to ``temp/homog/backup/`` (byte-exact pre-state), and every change is
   written to the change-log CSV (``--report``, default
   ``temp/homog/changes.csv``).
5. Agreements are never rewritten, and a master row that already carries the
   desired values is left alone.

Dry run by default (prints the full three-way diff plus every anomaly);
``--apply`` performs the union updates with atomic writes (temp file +
``os.replace``) and logs one ``kind=fulltext_homogenization`` record with the
counts to ``Results/analysis_log.jsonl``.  Idempotent: a second run of either
mode reports zero pending changes after a successful ``--apply``.

Run:
  python Scripts/homogenize_fulltext_exclusions.py                 # dry run
  python Scripts/homogenize_fulltext_exclusions.py --apply        # do it
  python Scripts/homogenize_fulltext_exclusions.py --report PATH  # change log
"""
from __future__ import annotations

import argparse
import csv
import datetime
import glob
import json
import os
import re
import shutil
import sys
import tempfile
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "Scripts"))
sys.path.insert(0, os.path.join(ROOT, "Scripts", "coding_web"))

import prisma_report as PR  # noqa: E402  (master-row <-> study-key matching)
import fulltext_lib as FL  # noqa: E402  (FULLTEXT_CRITERIA: the C1-C4 mapping)
from studio_lib import atomic_replace  # noqa: E402  (os.replace + lock retry)

FIELD = "Fulltext_Exclusion_Criterion"
CODES = ("C1", "C2", "C3", "C4")
#: fulltext_lib's label per C-code (the master-write convention, rule 3).
CRIT_LABEL = {c["value"]: c["label"] for c in FL.FULLTEXT_CRITERIA
              if c["value"] in CODES}
#: fulltext_lib's label (lower-cased) -> C-code, for the master -> coding map.
LABEL_TO_CODE = {c["label"].lower(): c["value"] for c in FL.FULLTEXT_CRITERIA
                 if c["value"] in CODES}

EXTRACTION = os.path.join(ROOT, "Extraction")
BACKUP_DIR = os.path.join(ROOT, "temp", "homog", "backup")
TMP_DIR = os.path.join(ROOT, "temp", "homog", "tmp")
MASTER = os.path.join(ROOT, "master_dataset.xlsx")

CHANGE_FIELDS = ["study_key", "direction", "field", "old", "new", "source_row"]
ANOMALY_FIELDS = ["study_key", "kind", "detail"]


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


# --------------------------------------------------------------------------- #
# criterion mapping (master label -> C1-C4); never invents
# --------------------------------------------------------------------------- #
def map_label_to_codes(label: str):
    """Map a master full-text criterion label to C-codes.

    Returns ``(codes, how)`` or ``None`` when the label does not map (the
    caller must then leave the coding field blank and record an anomaly -
    "never invent a criterion").  Preference order: an explicit C1-C4 token
    in the label, an exact ``fulltext_lib.FULLTEXT_CRITERIA`` label, then the
    keyword fallback ``routes_apa._crit_label`` uses for the screening layer
    (the "closest C-code ... when the label maps").
    """
    s = str(label or "").strip()
    if not s or s == "(unspecified)":
        return None
    low = s.lower()
    codes = [c for c, tok in (("C1", "c1"), ("C2", "c2"),
                              ("C3", "c3"), ("C4", "c4")) if tok in low]
    if codes:
        return codes, "c-token in label"
    code = LABEL_TO_CODE.get(low)
    if code:
        return [code], "fulltext_lib label"
    # keyword fallback (routes_apa._crit_label): the closest C-code, only
    # when the label actually names the criterion.
    if "no misinformation" in low or "unrelated" in low:
        return ["C1"], "keyword"
    if "control" in low or "comparison" in low:
        return ["C2"], "keyword"
    if "english" in low:
        return ["C3"], "keyword"
    if "primary" in low or "experimental" in low:
        return ["C4"], "keyword"
    return None


def record_codes(rec: dict) -> list[str]:
    """The C-codes one record's field carries (ordered C1..C4)."""
    v = rec.get(FIELD)
    values = v if isinstance(v, list) else ([v] if v not in (None, "") else [])
    out = []
    for x in values:
        for part in re.findall(r"C[1-4]", str(x or "").upper()):
            if part not in out:
                out.append(part)
    return [c for c in CODES if c in out]


def label_for_codes(codes: list[str]) -> str:
    """The master-write criterion label for a set of C-codes (rule 3)."""
    return "; ".join(CRIT_LABEL[c] for c in codes if c in CRIT_LABEL)


# --------------------------------------------------------------------------- #
# corpus / master state
# --------------------------------------------------------------------------- #
class Paper:
    """One coded paper (Exp-suffix files collapse onto the same paper)."""

    def __init__(self, key: str):
        self.key = key
        self.files: list[str] = []       # Extraction paths of this paper
        self.title = ""
        self.doi = ""
        self.codes: list[str] = []       # union of the paper's own C-codes
        self.blank_records: list[tuple[str, int]] = []   # (path, record index)
        self.hits: list[int] = []        # matched master df rows
        self.stage: set[str] = set()     # stages of the matched reports

    @property
    def marked(self) -> bool:
        return bool(self.codes)

    @property
    def on_excluded_report(self) -> bool:
        return "Exclude" in self.stage

    @property
    def on_included_report(self) -> bool:
        return "Include" in self.stage


def load_papers() -> dict[str, Paper]:
    papers: dict[str, Paper] = {}
    for path in sorted(glob.glob(os.path.join(EXTRACTION, "*.json"))):
        key = os.path.splitext(os.path.basename(path))[0]
        paper_key = re.sub(r"\s+Exp\d+$", "", key)
        p = papers.setdefault(paper_key, Paper(paper_key))
        p.files.append(path)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  !! unreadable coding skipped: {os.path.basename(path)}: {exc}")
            continue
        recs = data if isinstance(data, list) else [data]
        for i, rec in enumerate(recs):
            if not isinstance(rec, dict):
                continue
            if not p.title and str(rec.get("Title") or "").strip():
                p.title = str(rec["Title"]).strip()
            if not p.doi and str(rec.get("DOI") or "").strip():
                p.doi = str(rec["DOI"]).strip()
            codes = record_codes(rec)
            if codes:
                for c in codes:
                    if c not in p.codes:
                        p.codes.append(c)
            else:
                p.blank_records.append((path, i))
    for p in papers.values():
        p.codes = [c for c in CODES if c in p.codes]
    return papers


def match_papers(df, papers: dict[str, Paper]):
    idx = PR._build_match_index(df)
    roots = PR.report_ids(df)
    stages = PR.report_stages(df, roots)
    for p in papers.values():
        hits = list(dict.fromkeys(PR._match_master(df, idx, p.doi, p.title)))
        p.hits = hits
        p.stage = {stages[roots[i]] for i in hits} if hits else set()
    return roots, stages


# --------------------------------------------------------------------------- #
# the plan: what rule 2 / rule 3 would change (pure computation, no writes)
# --------------------------------------------------------------------------- #
class Plan:
    def __init__(self):
        # rule 2 (master -> coding): (paper, codes, master label, how-mapped)
        self.mark: list[tuple[Paper, list[str], str, str]] = []
        # fill blanks of an already-marked paper from its own codes
        self.complete: list[tuple[Paper, list[str]]] = []
        # rule 3 (coding -> master): (paper, codes, matched master rows)
        self.flip: list[tuple[Paper, list[str], list[int]]] = []
        # anomalies: (study_key, kind, detail)
        self.anomalies: list[tuple[str, str, str]] = []
        # informational coding-vs-master criterion disagreements
        self.disagreements: list[tuple[str, str, str]] = []
        # unchanged agreements, for the summary
        self.agree_marked_excluded = 0
        self.agree_unmarked_included = 0


def build_plan(df, roots, stages, papers: dict[str, Paper]) -> Plan:
    plan = Plan()
    members = defaultdict(list)
    for i, r in enumerate(roots):
        members[r].append(i)

    for pkey in sorted(papers):
        p = papers[pkey]
        if not p.hits:
            if p.marked:
                plan.anomalies.append(
                    (pkey, "unmatched_marked_paper",
                     f"marked {p.codes} but no master row matches "
                     f"(doi={p.doi!r}); no master row invented"))
            else:
                plan.anomalies.append(
                    (pkey, "unmatched_unmarked_paper",
                     "no master row matches (coded paper, unmarked); left "
                     "unchanged - no criterion source in this workbook"))
            continue

        if p.on_excluded_report:
            # the excluded report supplies the criterion for this paper
            root = roots[next(i for i in p.hits if stages[roots[i]] == "Exclude")]
            label = PR._reason_for_fulltext(df, members[root])
            if "Include" in p.stage:
                plan.anomalies.append(
                    (pkey, "matched_include_and_exclude_reports",
                     "the paper matches reports at both Include and Exclude "
                     "stage; the excluded report supplies the criterion, the "
                     "Include-stage match is reported here"))
            if p.marked:
                mapped = map_label_to_codes(label)
                if mapped and mapped[0] != p.codes:
                    plan.disagreements.append(
                        (pkey, ", ".join(p.codes),
                         f"{label} -> {', '.join(mapped[0])} ({mapped[1]})"))
                plan.agree_marked_excluded += 1
                # existing coding value stands; blank records are completed
                # from the paper's own codes (never overwrite, never invent)
                if p.blank_records:
                    plan.complete.append((p, list(p.codes)))
            else:
                mapped = map_label_to_codes(label)
                if mapped is None:
                    why = ("blank criterion on the excluded report"
                           if label in ("", "(unspecified)")
                           else f"criterion label {label!r} does not map to "
                                f"C1-C4; field left blank")
                    plan.anomalies.append(
                        (pkey, "blank_or_unmappable_criterion", why))
                else:
                    plan.mark.append((p, mapped[0], label, mapped[1]))
            continue

        if p.marked:
            # rule 3: coding -> master (matched, but no excluded report)
            plan.flip.append((p, list(p.codes), list(p.hits)))
            if p.on_included_report:
                plan.anomalies.append(
                    (pkey, "marked_paper_on_included_report",
                     f"marked {p.codes} on an Include-stage report; the union "
                     f"rule excludes the paper, so the report's verdict is "
                     f"flipped to Exclude (the included figure drops)"))
            if p.blank_records:
                plan.complete.append((p, list(p.codes)))
            continue

        if p.on_included_report:
            plan.agree_unmarked_included += 1
        else:
            plan.anomalies.append(
                (pkey, "abstract_stage_unmarked_paper",
                 f"matched report(s) at {'/'.join(sorted(p.stage))} stage; the "
                 f"coding records no full-text exclusion - left unchanged "
                 f"(abstract-level disagreement, out of scope)"))
    return plan


# --------------------------------------------------------------------------- #
# writers (atomic; a change-log row for every cell/field actually written)
# --------------------------------------------------------------------------- #
def _serialize(data, style: str) -> str:
    if style == "indent1":
        return json.dumps(data, indent=1, ensure_ascii=False) + "\n"
    if style == "ascii2":
        return json.dumps(data, indent=2, ensure_ascii=True) + "\n"
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _detect_style(raw: str, data) -> str | None:
    for style in ("utf8-2", "indent1", "ascii2"):
        if _serialize(data, style) == raw:
            return style
    return None


def _write_json(path: str, data, style: str | None, paper: Paper,
                direction: str, changes: list[dict]) -> None:
    if style is None:
        changes.append({
            "study_key": paper.key, "direction": direction,
            "field": "(file style)", "old": "unrecognised serialisation",
            "new": "json.dumps indent=2 ensure_ascii=False (reformatted)",
            "source_row": os.path.basename(path),
        })
    out = _serialize(data, style or "utf8-2")
    os.makedirs(TMP_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=TMP_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(out)
        atomic_replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def set_field_on_blank_records(paper: Paper, codes: list[str],
                                direction: str, changes: list[dict]) -> int:
    """Set FIELD on this paper's blank records (value: ``codes``).

    Existing non-empty values are never touched; the paper's Exp-suffixed
    files are all updated.  Returns the number of records written.
    """
    want = [c for c in CODES if c in codes]
    n = 0
    for path in paper.files:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
        data = json.loads(raw)
        recs = data if isinstance(data, list) else [data]
        style = _detect_style(raw, data)
        touched = False
        for i, rec in enumerate(recs):
            if not isinstance(rec, dict) or record_codes(rec):
                continue  # only blank records; never overwrite
            old = rec.get(FIELD)
            rec[FIELD] = list(want)
            touched = True
            n += 1
            sid = str(rec.get("Study_ID") or "")
            changes.append({
                "study_key": paper.key, "direction": direction,
                "field": FIELD,
                "old": "" if old in (None, "") else json.dumps(old, ensure_ascii=False),
                "new": json.dumps(rec[FIELD]),
                "source_row": f"{os.path.basename(path)} record {i} "
                              f"(Study_ID={sid})",
            })
        if touched:
            _write_json(path, data, style, paper, direction, changes)
    return n


def write_master(df, flips, changes: list[dict]) -> int:
    """Rule 3: the Screening tab's locked-decision write, on matched rows."""
    import openpyxl

    sheet = PR._resolve_sheet(MASTER)
    wb = openpyxl.load_workbook(MASTER)
    try:
        ws = wb[sheet]
        # 1-based column indexes, matched case-/space-insensitively
        # (fulltext_lib._mark_master_locked's convention)
        head = {}
        for i, c in enumerate(ws[1]):
            head.setdefault(_norm(c.value), i + 1)

        def col(name):
            return head.get(_norm(name))

        def row_ident(i):
            an = str(df.at[i, "an"] or "").strip()
            doi = str(df.at[i, "doi"] or "").strip()
            return f"master row {i + 2} (an={an or '-'}, doi={doi or '-'})"

        n_cells = 0
        for paper, codes, hits in flips:
            label = label_for_codes(codes)
            reason = (f"Coding-route exclusion criteria "
                      f"{', '.join(codes)} (fulltext homogenization "
                      f"{_now()[:10]})")
            for i in hits:
                xl_row = i + 2   # df index -> sheet row (header on row 1)
                for field, want in (("Fulltext_Verdict", "Exclude"),
                                    (FIELD, label),
                                    ("Fulltext_Reason", reason),
                                    ("Effective_Verdict", "Exclude")):
                    c = col(field)
                    if c is None:
                        continue
                    cur = ws.cell(row=xl_row, column=c).value
                    if str(cur or "").strip() == str(want).strip():
                        continue   # already there (idempotent)
                    ws.cell(row=xl_row, column=c, value=want)
                    n_cells += 1
                    changes.append({
                        "study_key": paper.key,
                        "direction": "coding_to_master",
                        "field": field,
                        "old": "" if cur is None else str(cur),
                        "new": want,
                        "source_row": row_ident(i),
                    })
        os.makedirs(TMP_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".xlsx", dir=TMP_DIR)
        os.close(fd)
        wb.save(tmp)
        wb.close()
        atomic_replace(tmp, MASTER)
        return n_cells
    except Exception:
        wb.close()
        raise


def backup_pristine_state(flips: bool, papers: list[Paper]) -> None:
    """Byte-exact pre-state copies, taken before the first write of each file."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    done = []
    if flips and not os.path.exists(os.path.join(BACKUP_DIR, "master_dataset.xlsx")):
        shutil.copy2(MASTER, os.path.join(BACKUP_DIR, "master_dataset.xlsx"))
        done.append("master_dataset.xlsx")
    for p in papers:
        for path in p.files:
            dest = os.path.join(BACKUP_DIR, os.path.basename(path))
            if os.path.exists(path) and not os.path.exists(dest):
                shutil.copy2(path, dest)
                done.append(os.path.basename(path))
    if done:
        print(f"backup: {len(done)} pristine file(s) copied to "
              f"temp/homog/backup/")


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def write_csv(path: str, rows: list[dict], fields: list[str]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def print_plan(plan: Plan, papers: dict[str, Paper]) -> None:
    print("=" * 78)
    print("THREE-WAY DIFF (coding field vs master screening verdicts)")
    print("=" * 78)
    n_marked = sum(1 for p in papers.values() if p.marked)
    print(f"coded papers: {len(papers)}   marked (C1-C4 field): {n_marked}")
    print()
    print(f"A. master-excluded but UNMARKED - to be marked (rule 2): "
          f"{len(plan.mark)}")
    by: dict[str, int] = defaultdict(int)
    for _p, codes, label, how in plan.mark:
        by[f"{'+'.join(codes)}  <-  {label!r} ({how})"] += 1
    for k, n in sorted(by.items(), key=lambda kv: -kv[1]):
        print(f"     {n:3d}  {k}")
    unmap = [a for a in plan.anomalies
             if a[1] == "blank_or_unmappable_criterion"]
    print(f"     ({len(unmap)} more on excluded reports whose criterion does "
          f"not map - left blank, see anomalies)")
    print(f"B. marked but master NOT excluded - master rows to flip "
          f"(rule 3): {len(plan.flip)}")
    by = defaultdict(int)
    for p, _codes, _hits in plan.flip:
        by["/".join(sorted(p.stage)) or "?"] += 1
    for k, n in sorted(by.items()):
        print(f"     {n:3d}  matched report stage: {k}")
    print(f"C. marked papers with blank records - to be completed from their "
          f"own codes: {len(plan.complete)}")
    print(f"D. agreement, no change: marked-on-excluded "
          f"{plan.agree_marked_excluded}; unmarked-on-include "
          f"{plan.agree_unmarked_included}")
    print()
    print("ANOMALIES (never invent: left unchanged, listed for the owner)")
    for pkey, kind, detail in plan.anomalies:
        print(f"  [{kind}] {pkey}: {detail}")
    if plan.disagreements:
        print()
        print("coding-vs-master criterion disagreements (coding value stands):")
        for pkey, coded, master in plan.disagreements:
            print(f"  {pkey}: coding={coded}; master={master}")


def print_flow(flow_before: dict, flow_after: dict) -> None:
    print()
    print("=" * 78)
    print("PRISMA / union state (before -> after)")
    print("=" * 78)
    for k in ("reports", "abstract_excluded", "fulltext_excluded",
              "included_studies"):
        old, new = flow_before.get(k), flow_after.get(k)
        note = "" if old == new else f"   (was {old})"
        print(f"  {k:24s} {new}{note}")
    b = flow_before.get("coded_fulltext_excluded")
    a = flow_after.get("coded_fulltext_excluded")
    note = "" if b == a else f"   (was {b})"
    print(f"  {'coded-excluded union box':24s} {a}{note}")
    det_b = (flow_before.get("coded_fulltext_excluded_detail") or {})
    det_a = (flow_after.get("coded_fulltext_excluded_detail") or {})
    print(f"    in_master_excluded_reports: "
          f"{det_a.get('in_master_excluded_reports')} "
          f"(was {det_b.get('in_master_excluded_reports')}); "
          f"without_a_master_row: {det_a.get('without_a_master_row')} "
          f"(was {det_b.get('without_a_master_row')})")
    print(f"  breakdown after: "
          f"{flow_after.get('coded_fulltext_excluded_by_criterion')}")
    print(f"  breakdown before: "
          f"{flow_before.get('coded_fulltext_excluded_by_criterion')}")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="perform the union updates (default: dry run)")
    ap.add_argument("--report", default=os.path.join(ROOT, "temp", "homog",
                                                     "changes.csv"),
                    help="change-log CSV (default temp/homog/changes.csv)")
    ap.add_argument("--anomalies", default=os.path.join(ROOT, "temp", "homog",
                                                        "anomalies.csv"),
                    help="anomaly CSV (default temp/homog/anomalies.csv)")
    args = ap.parse_args()

    if not os.path.exists(MASTER):
        print("master_dataset.xlsx not found at", MASTER)
        return 2

    print(f"[{_now()}] loading master workbook ...")
    df = PR.load_master(MASTER)
    papers = load_papers()
    print(f"[{_now()}] coded papers: {len(papers)}; matching against the "
          f"master ...")
    roots, stages = match_papers(df, papers)
    plan = build_plan(df, roots, stages, papers)
    flow_before = PR.prisma_flow(df)
    print(f"[{_now()}] before: excluded reports "
          f"{flow_before['fulltext_excluded']}, included "
          f"{flow_before['included_studies']}, union box "
          f"{flow_before['coded_fulltext_excluded']}")

    print_plan(plan, papers)
    anom_rows = [{"study_key": k, "kind": kind, "detail": d}
                 for k, kind, d in plan.anomalies]
    write_csv(args.anomalies, anom_rows, ANOMALY_FIELDS)

    if not args.apply:
        print()
        print(f"DRY RUN - nothing written. --apply would: mark "
              f"{len(plan.mark)} papers, complete {len(plan.complete)}, "
              f"flip {len(plan.flip)} papers' master rows. Anomalies: "
              f"{len(anom_rows)} (temp/homog/anomalies.csv).")
        return 0

    # ---- apply ------------------------------------------------------------- #
    changes: list[dict] = []
    touched = [p for p, *_ in plan.mark] + [p for p, _ in plan.complete]
    backup_pristine_state(flips=bool(plan.flip), papers=touched)

    n_marked = n_completed = n_cells = 0
    try:
        for paper, codes, label, how in plan.mark:
            n_marked += set_field_on_blank_records(
                paper, codes, "master_to_coding", changes)
        for paper, codes in plan.complete:
            n_completed += set_field_on_blank_records(
                paper, codes, "coding_complete", changes)
        n_cells = write_master(df, plan.flip, changes) if plan.flip else 0
    finally:
        # the change log must exist even if a write fails mid-way (the
        # backups plus this log make any partial state fully auditable)
        write_csv(args.report, changes, CHANGE_FIELDS)

    # durable log (Results/analysis_log.jsonl), with counts
    sys.path.insert(0, os.path.join(ROOT, "Scripts", "analysis"))
    import analysis_log
    analysis_log.log(
        "fulltext_homogenization",
        n_changes=len(changes),
        n_coding_records_marked=n_marked,
        n_papers_marked=len(plan.mark),
        n_coding_records_completed=n_completed,
        n_papers_completed=len(plan.complete),
        n_master_cells_written=n_cells,
        n_papers_flipped=len(plan.flip),
        n_anomalies=len(anom_rows),
        anomaly_kinds=sorted({r["kind"] for r in anom_rows}),
        n_coding_vs_master_disagreements=len(plan.disagreements),
        report=os.path.relpath(args.report, ROOT),
        anomalies_csv=os.path.relpath(args.anomalies, ROOT),
    )

    # ---- verify: reload, re-plan (must be empty), show the new state ------- #
    print(f"[{_now()}] apply done; re-loading master and re-planning ...")
    df2 = PR.load_master(MASTER)
    papers2 = load_papers()
    roots2, stages2 = match_papers(df2, papers2)
    plan2 = build_plan(df2, roots2, stages2, papers2)
    flow_after = PR.prisma_flow(df2)
    pending = len(plan2.mark) + len(plan2.complete) + len(plan2.flip)
    print(f"pending changes on the re-plan (idempotence check): "
          f"mark={len(plan2.mark)} complete={len(plan2.complete)} "
          f"flip={len(plan2.flip)}  -> {'ZERO (idempotent)' if not pending else 'NOT ZERO!'}")
    print_flow(flow_before, flow_after)

    import study_exclusions as SE
    keys = SE.fulltext_excluded_keys()
    print(f"study_exclusions.fulltext_excluded_keys(): {len(keys)}")
    print(f"[{_now()}] change log written: {args.report} ({len(changes)} rows)")
    return 0 if not pending else 1


if __name__ == "__main__":
    raise SystemExit(main())
