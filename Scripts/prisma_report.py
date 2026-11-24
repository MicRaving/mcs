#!/usr/bin/env python
"""Report-level PRISMA counting for the misinformation-intervention review.

The master workbook (`master_dataset.xlsx`) is a set of database *records*: the
same report is often present several times (different databases, an extra
accompaniment record, a review reference-list row).  Counting rows therefore
overstates every PRISMA box.  This module collapses records into **reports** and
returns the flow at report level.

Deduplication rule (applied with union-find over master rows):

  1. rows sharing a primary via ``Duplicate_Of`` (the value may be an accession
     number *or* a study key, e.g. ``Gollwitzer et al. (2026)`` -> resolved via
     the ``Curation_Note`` that names the study); or
  2. rows sharing a non-empty normalised DOI; or
  3. rows sharing a non-empty normalised title; or
  4. rows sharing a non-empty accession (``an``).

Each resulting component is one *report*.  A report's stage is the most advanced
stage reached by any of its records:

    Include  >  Exclude (full text)  >  Unscreened  >  Abstract excluded
             >  Not retrieved / not assessed

so a report that has an included record is counted as included even when a
duplicate record was excluded.

Run:  python Scripts/prisma_report.py        # prints the flow as JSON
"""
from __future__ import annotations

import glob
import json
import os
import re
import unicodedata
from collections import defaultdict

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER = os.path.join(ROOT, "master_dataset.xlsx")
EXTRACTION = os.path.join(ROOT, "Extraction")
AGREED_FULLTEXT = os.path.join(ROOT, "Screening", "fulltext_agreed.json")
SHEET = "Search"


def _resolve_sheet(path: str) -> str:
    """Master workbooks have been renamed over time (Sheet1 -> Search); use the
    requested sheet when present, else fall back to the first sheet."""
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        names = list(wb.sheetnames)
    finally:
        wb.close()
    if SHEET in names:
        return SHEET
    if not names:
        raise ValueError(f"workbook {path} has no sheets")
    return names[0]

CRITERION_REASON = {
    "C1": "No misinformation intervention (C1)",
    "C2": "No control/comparison group (C2)",
    "C3": "Not English (C3)",
    "C4": "Not a primary experimental study (C4)",
}
DUP_CRITERION = "Duplicate/non-independent report of an included study"
_CRIT_ORDER = ("C1", "C2", "C3", "C4")
_STAGES = ("Include", "Exclude", "Unscreened", "Abstract excluded",
           "Not retrieved / not assessed")


# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #
def norm_doi(value) -> str:
    s = str(value or "").strip().lower()
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s)
    s = re.sub(r"^doi:\s*", "", s)
    return s.strip().strip(".")


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c))


def norm_title(value, strip_annotation: bool = False) -> str:
    s = str(value or "")
    # fully-within studies carry a leading "[fw] " marker in the coded Title; ignore it
    # when reconciling against the master workbook.
    s = re.sub(r"^\s*\[fw\]\s*", " ", s, flags=re.I)
    if strip_annotation:
        s = re.sub(r"\s*[\[\(][^\]\)]*[\]\)]\s*$", " ", s)
    s = _fold(s).lower()
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def load_master(path: str = MASTER) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name=_resolve_sheet(path), dtype=str).fillna("")
    for col in df.columns:
        df[col] = df[col].astype(str)
    return df


# --------------------------------------------------------------------------- #
# report construction
# --------------------------------------------------------------------------- #
def report_ids(df: pd.DataFrame) -> list:
    """Union-find over rows; returns the component root for every row."""
    n = len(df)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    doi = df["doi"].astype(str)
    if "doids" in df.columns:
        doi = doi.where(doi.str.strip() != "", df["doids"].astype(str))

    doi_idx, title_idx, an_idx = {}, {}, {}
    for i in range(n):
        nd = norm_doi(doi.iloc[i])
        if nd:
            doi_idx.setdefault(nd, []).append(i)
        nt = norm_title(df["title"].iloc[i])
        if nt:
            title_idx.setdefault(nt, []).append(i)
        an = str(df["an"].iloc[i]).strip()
        if an:
            an_idx.setdefault(an, []).append(i)
    for bucket in (doi_idx, title_idx, an_idx):
        for idxs in bucket.values():
            for j in idxs[1:]:
                union(idxs[0], j)

    curation = df["Curation_Note"].astype(str)
    for i in range(n):
        target = str(df["Duplicate_Of"].iloc[i]).strip() if "Duplicate_Of" in df.columns else ""
        if not target:
            continue
        if target in an_idx:
            for j in an_idx[target]:
                union(i, j)
            continue
        # study-key form: resolve through the notes that name the study
        for j in df.index[curation.str.contains(re.escape(target), regex=True)]:
            union(i, j)

    return [find(i) for i in range(n)]


def _decisive_criterion(df: pd.DataFrame, i) -> str:
    for c in _CRIT_ORDER:
        if str(df.at[i, f"{c}_verdict"]).strip() == "No":
            return c
    return ""


def _reason_for_abstract(df: pd.DataFrame, idxs) -> str:
    for i in idxs:
        if str(df.at[i, "Effective_Verdict"]).strip() != "Abstract excluded":
            continue
        crit = str(df.at[i, "Fulltext_Exclusion_Criterion"]).strip()
        if crit:
            return crit
        c = _decisive_criterion(df, i)
        if c:
            return CRITERION_REASON[c]
    return "(unspecified)"


def _reason_for_fulltext(df: pd.DataFrame, idxs) -> str:
    for i in idxs:
        if str(df.at[i, "Effective_Verdict"]).strip() != "Exclude":
            continue
        crit = str(df.at[i, "Fulltext_Exclusion_Criterion"]).strip()
        if crit:
            return crit
        reason = str(df.at[i, "Fulltext_Reason"]).strip()
        if reason:
            return reason
    return "(unspecified)"


def report_stages(df: pd.DataFrame, roots=None) -> dict:
    """Map each report (component root) to its most-advanced stage."""
    if roots is None:
        roots = report_ids(df)
    members = defaultdict(list)
    for i, r in enumerate(roots):
        members[r].append(i)
    stages = {}
    for root, idxs in members.items():
        vals = {str(df.at[i, "Effective_Verdict"]).strip() for i in idxs}
        stage = "Abstract excluded"
        for cand in ("Include", "Exclude", "Unscreened", "Not retrieved / not assessed"):
            if cand in vals:
                stage = cand
                break
        stages[root] = stage
    return stages


# --------------------------------------------------------------------------- #
# extraction reconciliation
# --------------------------------------------------------------------------- #
def coded_papers() -> dict:
    """One record per coded paper in Extraction/ (collapsing Exp suffixes).

    Each entry carries the paper's title/DOI (for master matching) and
    ``criteria``: the set of non-empty ``Fulltext_Exclusion_Criterion`` values
    across the paper's records - the canonical coding's own full-text
    exclusion marker (C1 no intervention / C2 no control / C3 not English /
    C4 not primary-experimental; a blank field means the paper met every
    inclusion criterion, or the route recorded none).
    """
    papers = {}
    for path in sorted(glob.glob(os.path.join(EXTRACTION, "*.json"))):
        key = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path, encoding="utf-8") as fh:
                objs = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(objs, dict):
            objs = [objs]
        objs = [o for o in objs if isinstance(o, dict)]
        if not objs:
            continue
        title = next((str(o.get("Title") or "").strip() for o in objs
                      if str(o.get("Title") or "").strip()), "")
        doi = next((str(o.get("DOI") or "").strip() for o in objs
                    if str(o.get("DOI") or "").strip()), "")
        paper_key = re.sub(r"\s+Exp\d+$", "", key)
        entry = papers.setdefault(paper_key, {"title": title, "doi": doi,
                                              "criteria": set()})
        for o in objs:
            value = o.get("Fulltext_Exclusion_Criterion")
            values = value if isinstance(value, list) else (
                [value] if value not in (None, "") else [])
            for v in values:
                v = str(v).strip()
                if v:
                    entry["criteria"].add(v)
        if not entry["title"] and title:
            entry["title"] = title
        if not entry["doi"] and doi:
            entry["doi"] = doi
    return papers


#: The PRISMA box's short criterion labels (the diagram's breakdown line).
#: The eight full-text exclusion criteria (owner decision 2026-11-19), in
#: canonical order; a paper that fails several counts under each.
CODED_EXCLUSION_SHORT = {
    "C1": "C1 no intervention",
    "C2": "C2 no control",
    "C3": "C3 not English",
    "C4": "C4 not experimental",
    "irretrievable": "irretrievable",
    "no_contrast": "no eligible contrast",
    "duplicate": "duplicate report",
    "domain_mismatch": "outside scope",
}


def agreed_fulltext_exclusions() -> dict:
    """{study_key: criterion} from ``Screening/fulltext_agreed.json``.

    That file is the screening layer's canonical record of the full-text
    exclusions the coders agreed on (the same file
    ``Scripts/study_exclusions.py::fulltext_excluded_keys`` reads); it is
    written by the coding app's full-text tab, so it is absent in a checkout
    where no agreement pass has run - an absent file is an empty dict, never
    an error.  The criterion is the entry's majority criterion code (C1..C4,
    or one of the screening tab's own codes such as ``duplicate``).
    """
    if not os.path.isfile(AGREED_FULLTEXT):
        return {}
    try:
        with open(AGREED_FULLTEXT, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, info in data.items():
        crit = ""
        if isinstance(info, dict):
            crit = str(info.get("criterion") or "").strip()
        out[str(key)] = crit
    return out


def coded_fulltext_excluded(papers: dict | None = None) -> dict:
    """Coded papers carrying a full-text exclusion - ONE helper for the box.

    The figure behind the PRISMA box "Coded but excluded at full text" is the
    UNION, over coded papers, of the two sources that record a full-text
    exclusion:

      * the canonical coding's own ``Fulltext_Exclusion_Criterion`` field
        (``Extraction/<Key>.json`` - a paper counts when any of its records
        carries a non-empty criterion), and
      * the agreed screening file ``Screening/fulltext_agreed.json`` (the
        screening layer's canonical record; see
        :func:`agreed_fulltext_exclusions`), when it is present.

    The master workbook's own full-text verdicts are NOT a source for this
    box - ``prisma_flow`` counts them separately (``fulltext_excluded`` and
    ``coded_papers_in_excluded_reports``) so the report can reconcile the two.

    Returns a dict with the box count, the key set (``keys``), the
    per-criterion breakdown (a paper that fails several criteria counts
    under each of them, so the breakdown can exceed the count) and the
    per-source counts for the reconciliation note.
    """
    papers = coded_papers() if papers is None else papers
    agreed = agreed_fulltext_exclusions()
    by_key: dict[str, set] = {}
    from_field = set()
    from_agreed = set()
    for key, meta in papers.items():
        crits = {c for c in (meta.get("criteria") or ()) if str(c).strip()}
        if crits:
            from_field.add(key)
        if key in agreed:
            from_agreed.add(key)
            # the agreed file's majority criterion (compute_agreed defaults
            # it to "other", so a blank is rare - still counts the paper)
            crits.add(agreed[key] or "(unspecified)")
        if crits:
            by_key[key] = crits

    breakdown: dict[str, int] = {}
    multi = 0
    for crits in by_key.values():
        if len(crits) > 1:
            multi += 1
        for c in crits:
            breakdown[c] = breakdown.get(c, 0) + 1
    # canonical order first (the eight criteria), then anything the sources
    # additionally carry (an unusual coding value, the agreed file's own codes)
    ordered = {}
    for code in CODED_EXCLUSION_SHORT:
        if code in breakdown:
            ordered[CODED_EXCLUSION_SHORT[code]] = breakdown.pop(code)
    for code in sorted(breakdown):
        ordered[code] = breakdown[code]
    return {
        "count": len(by_key),
        "keys": set(by_key),
        "by_criterion": ordered,
        "papers_with_several_criteria": multi,
        "from_extraction_field": len(from_field),
        "from_agreed_file": len(from_agreed),
        "agreed_file_present": os.path.isfile(AGREED_FULLTEXT),
        "agreed_keys_uncoded": len(set(agreed) - set(papers)),
    }


def _build_match_index(df: pd.DataFrame) -> dict:
    """Pre-compute normalised DOI / title indexes once."""
    ndoi, ndoids, ntitle, ntitle_s = [], [], [], []
    doi_idx, doids_idx, title_idx, tstrip_idx = {}, {}, {}, {}
    for i in df.index:
        nd = norm_doi(df.at[i, "doi"])
        ndd = norm_doi(df.at[i, "doids"]) if "doids" in df.columns else ""
        nt = norm_title(df.at[i, "title"])
        nts = norm_title(df.at[i, "title"], strip_annotation=True)
        ndoi.append(nd)
        ndoids.append(ndd)
        ntitle.append(nt)
        ntitle_s.append(nts)
        if nd:
            doi_idx.setdefault(nd, []).append(i)
        if ndd:
            doids_idx.setdefault(ndd, []).append(i)
        if nt:
            title_idx.setdefault(nt, []).append(i)
        if nts:
            tstrip_idx.setdefault(nts, []).append(i)
    return {
        "ndoi": ndoi, "ndoids": ndoids, "ntitle": ntitle, "ntitle_s": ntitle_s,
        "doi_idx": doi_idx, "doids_idx": doids_idx,
        "title_idx": title_idx, "tstrip_idx": tstrip_idx,
    }


def _match_master(df: pd.DataFrame, idx: dict, doi: str, title: str):
    nd = norm_doi(doi)
    if nd:
        if nd in idx["doi_idx"]:
            return list(idx["doi_idx"][nd])
        if nd in idx["doids_idx"]:
            return list(idx["doids_idx"][nd])
    nts = norm_title(title, strip_annotation=True)
    if nts and nts in idx["tstrip_idx"]:
        return list(idx["tstrip_idx"][nts])
    nt = norm_title(title)
    if nt:
        if nt in idx["title_idx"]:
            return list(idx["title_idx"][nt])
        if len(nt) >= 25:
            pfx = nt[:45]
            hits = [i for i in df.index
                    if len(idx["ntitle"][i]) >= 45
                    and (idx["ntitle"][i].startswith(pfx)
                         or nt.startswith(idx["ntitle"][i][:45]))]
            if hits:
                return hits
    return []


# --------------------------------------------------------------------------- #
# the flow
# --------------------------------------------------------------------------- #
def prisma_flow(df: pd.DataFrame | None = None) -> dict:
    if df is None:
        df = load_master()

    roots = report_ids(df)
    stages = report_stages(df, roots)
    members = defaultdict(list)
    for i, r in enumerate(roots):
        members[r].append(i)

    by_stage = defaultdict(list)
    for root, st in stages.items():
        by_stage[st].append(root)

    abstract_reasons = defaultdict(set)
    fulltext_reasons = defaultdict(set)
    for root in by_stage.get("Abstract excluded", []):
        abstract_reasons[_reason_for_abstract(df, members[root])].add(root)
    for root in by_stage.get("Exclude", []):
        fulltext_reasons[_reason_for_fulltext(df, members[root])].add(root)

    n_rows = len(df)
    n_reports = len(members)
    abstract_excl = len(by_stage.get("Abstract excluded", []))
    included_reports = len(by_stage.get("Include", []))
    fulltext_excl = len(by_stage.get("Exclude", []))
    not_retrieved = len(by_stage.get("Not retrieved / not assessed", []))
    unscreened = len(by_stage.get("Unscreened", []))
    assessed = included_reports + fulltext_excl
    sought = n_reports - abstract_excl

    included_rows = int((df["Effective_Verdict"].str.strip() == "Include").sum())

    # reconciliation against the coded corpus
    papers = coded_papers()
    match_idx = _build_match_index(df)
    coded_reached_roots = set()
    paper_roots: dict = {}
    for pkey, meta in papers.items():
        proots = set()
        for i in _match_master(df, match_idx, meta["doi"], meta["title"]):
            proots.add(roots[i])
        if proots:
            coded_reached_roots |= proots
            paper_roots[pkey] = proots
    coded_in_include = {r for r in coded_reached_roots
                        if stages.get(r) == "Include"}
    included_uncoded = sorted(
        r for r in by_stage.get("Include", []) if r not in coded_reached_roots)
    uncoded_titles = []
    for r in included_uncoded:
        for i in members[r]:
            if str(df.at[i, "Effective_Verdict"]).strip() == "Include":
                uncoded_titles.append(str(df.at[i, "title"]))
                break

    # coded papers carrying a full-text exclusion (ONE helper - the PRISMA
    # box "Coded but excluded at full text"; the union of the coding's own
    # criterion field and the agreed screening file when present).  The
    # master workbook's excluded reports are counted above and are kept here
    # only as the reconciliation figures.
    coded_ft = coded_fulltext_excluded(papers)
    ft_keys = coded_ft["keys"]
    coded_ft_in_excluded_reports = sum(
        1 for k in ft_keys
        if any(stages.get(r) == "Exclude" for r in paper_roots.get(k, ())))
    coded_ft_without_master_row = sum(1 for k in ft_keys
                                      if not paper_roots.get(k))

    flow = {
        "identified_records": n_rows,
        "duplicates_removed": n_rows - n_reports,
        "reports": n_reports,
        "abstract_screened": n_reports,
        "abstract_excluded": abstract_excl,
        "abstract_excluded_by_reason": {
            k: len(v) for k, v in sorted(abstract_reasons.items(),
                                         key=lambda kv: -len(kv[1]))},
        "sought_for_retrieval": sought,
        "not_retrieved": not_retrieved,
        "fulltext_assessed": assessed,
        "fulltext_excluded": fulltext_excl,
        "fulltext_excluded_by_reason": {
            k: len(v) for k, v in sorted(fulltext_reasons.items(),
                                         key=lambda kv: -len(kv[1]))},
        "included_studies": included_reports,
        "included_records": included_rows,
        "duplicate_included_records": included_rows - included_reports,
        "unscreened": unscreened,
        "identity_sought_ok": sought == not_retrieved + assessed,
        "identity_included_ok": included_rows >= included_reports,
        "extraction": {
            "distinct_coded_papers": len(papers),
            "coded_papers_reaching_a_report": len(coded_reached_roots),
            "coded_papers_in_included_reports": len(coded_in_include),
            "included_reports_without_a_coding": len(included_uncoded),
            "included_reports_without_a_coding_titles": uncoded_titles,
        },
        # the PRISMA box "Coded but excluded at full text": coded papers
        # carrying a full-text exclusion (union of the coding's own
        # criterion field and the agreed screening file when present), with
        # the per-source counts and the master-verdict reconciliation.
        "coded_fulltext_excluded": coded_ft["count"],
        "coded_fulltext_excluded_by_criterion": coded_ft["by_criterion"],
        "coded_fulltext_excluded_detail": {
            "from_extraction_field": coded_ft["from_extraction_field"],
            "from_agreed_file": coded_ft["from_agreed_file"],
            "agreed_file_present": coded_ft["agreed_file_present"],
            "agreed_keys_uncoded": coded_ft["agreed_keys_uncoded"],
            "papers_with_several_criteria": coded_ft["papers_with_several_criteria"],
            "in_master_excluded_reports": coded_ft_in_excluded_reports,
            "without_a_master_row": coded_ft_without_master_row,
        },
    }
    return flow


def main() -> int:
    flow = prisma_flow()
    print(json.dumps(flow, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
