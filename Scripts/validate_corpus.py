"""Corpus-level integrity check across `Extraction/*.json`.

Fails (exit 1) on:

1. any `Study_ID` that occurs more than once in the corpus (a duplicate identifier makes
   the same evidence poolable twice and breaks id-keyed joins);
2. any DOI shared by two *different* Extraction files (two artifact files for one paper);
3. any normalised Title shared by two *different* Extraction files.

It also *prints* (information only, never a failure):

* the thesis/dissertation-vs-article candidate pairs (same first-author surname + year,
  one artifact a thesis/dissertation and another a non-thesis artifact);
* same first-author + year pairs across files with an identical reported N, which is the
  signature of a salami-sliced sample;
* (a) same first-author surname + identical N across **any** year (catches cross-year
  preprint/working-paper -> article pairs such as Pennycook 2023 -> 2024);
* (b) shared author teams (>=2 shared surnames) + identical N (catches the Apuke/Zhao
  family and other reused cohorts);
* (c) cross-year preprint/thesis -> article candidates with high title similarity or a
  matching N.

Only exact duplicates (Study_ID, cross-file DOI, cross-file normalised title) fail.

Repeated DOIs/titles *within* one file are normal (one paper with several experiments),
so those are grouped by file and never flagged.

Run:  python Scripts/validate_corpus.py
"""
import glob
import json
import os
import re
import sys
import difflib
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTRACT = os.path.join(ROOT, "Extraction")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

THESIS_PUBS = {"thesis", "dissertation", "doctoral thesis", "phd thesis", "master's thesis"}
#: Publication types that are an unreviewed / early version of a paper.  A cross-year
#: pair between one of these and an article/preprint is a preprint -> article candidate.
PREPRINT_TYPES = {"preprint", "working_paper", "working paper", "unpublished_manuscript",
                  "unpublished"}
ARTICLE_TYPES = {"journal_article", "article", "paper", "conference_paper",
                 "published_article"}
#: Title similarity above which two titles are treated as the "same paper" for scan (c).
TITLE_SIM_THRESHOLD = 0.50


def norm_title(t):
    t = str(t or "").lower()
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def norm_doi(d):
    if not d:
        return ""
    d = str(d).strip().lower()
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d)
    return d


def first_surname(rec):
    """First author's surname, lowercased, robust to 'Surname, Given' / 'Given Surname'."""
    authors = str(rec.get("Authors") or "").strip()
    if authors:
        first = authors.split(";")[0].strip()
        if "," in first:
            return first.split(",")[0].strip().lower()
    a1 = str(rec.get("Author_1") or "").strip()
    if not a1:
        return ""
    if "," in a1:
        return a1.split(",")[0].strip().lower()
    toks = a1.split()
    return toks[-1].lower() if toks else ""


def all_surnames(rec):
    """Every author surname (lowercased) from ``Authors`` (fallback ``Author_1..N``)."""
    out = []
    authors = str(rec.get("Authors") or "").strip()
    if authors:
        for a in authors.split(";"):
            a = a.strip()
            if not a:
                continue
            sur = a.split(",")[0].strip() if "," in a else (a.split() or [""])[-1]
            sur = sur.strip().lower()
            if sur:
                out.append(sur)
        return out
    i = 1
    while True:
        a = rec.get(f"Author_{i}")
        if not a:
            break
        toks = str(a).split()
        if toks:
            out.append(toks[-1].lower())
        i += 1
    return out


def title_similarity(a, b):
    """SequenceMatcher ratio between two normalised titles."""
    return difflib.SequenceMatcher(None, a, b).ratio()


def pub_kind(rec):
    return str(rec.get("Publication_Type") or "").strip().lower()


def is_preprint_like(rec):
    return pub_kind(rec) in PREPRINT_TYPES


def is_thesis_like(rec, key):
    pubs = {str(rec.get(k) or "").strip().lower()
            for k in ("Publication_Type", "Publication_Category", "Publication")}
    if pubs & THESIS_PUBS:
        return True
    if any(pubs):
        # A non-empty publication type that is not a thesis/dissertation is authoritative;
        # the key-form fallback below would otherwise tag every single-author article
        # (`Surname (Year)`) as a thesis.
        return False
    # Thesis/dissertation artifact keys use the single-author form `Surname (Year)`.
    stem = re.sub(r"\s*\(\d{4}[a-z]?\)\s*$", "", key).strip()
    return bool(stem) and " et al." not in key and " & " not in key


def load():
    records = []
    for path in sorted(glob.glob(os.path.join(EXTRACT, "*.json"))):
        key = os.path.splitext(os.path.basename(path))[0]
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"  PARSE FAIL {key}: {exc}")
            continue
        data = data if isinstance(data, list) else [data]
        for rec in data:
            if not isinstance(rec, dict):
                continue
            records.append(dict(
                key=key, study_id=str(rec.get("Study_ID") or ""),
                doi=norm_doi(rec.get("DOI")), title=rec.get("Title"),
                norm_title=norm_title(rec.get("Title")),
                pub=rec.get("Publication_Type"), year=rec.get("Year"),
                author=first_surname(rec), n=rec.get("N_Analyzed"),
                sample=rec.get("N_Recruited"),
                surnames=all_surnames(rec),
                thesis_like=is_thesis_like(rec, key),
                preprint_like=is_preprint_like(rec),
            ))
    return records


def cross_file(groups):
    """Groups whose members span more than one Extraction file."""
    return {g: rows for g, rows in groups.items()
            if len({r["key"] for r in rows}) > 1}


def main():
    records = load()
    failures = []
    print(f"validate_corpus: {len(records)} records across "
          f"{len({r['key'] for r in records})} Extraction files")

    # ---- 1. duplicate Study_ID (global uniqueness) -------------------------
    by_sid = defaultdict(list)
    for r in records:
        if r["study_id"]:
            by_sid[r["study_id"]].append(r)
    dup_sid = {k: v for k, v in by_sid.items() if len(v) > 1}
    print("\n[1] duplicate Study_ID:")
    if not dup_sid:
        print("    none")
    for sid, rows in sorted(dup_sid.items()):
        print(f"    FAIL {sid}")
        for r in rows:
            print(f"        {r['key']:<34} pub={r['pub']:<16} year={r['year']} n={r['n']}")
        failures.append(f"duplicate Study_ID {sid}")

    # ---- 2. duplicate DOI across files -------------------------------------
    by_doi = defaultdict(list)
    for r in records:
        if r["doi"]:
            by_doi[r["doi"]].append(r)
    dup_doi = cross_file(by_doi)
    print("\n[2] duplicate DOI across files:")
    if not dup_doi:
        print("    none")
    for doi, rows in sorted(dup_doi.items()):
        print(f"    FAIL {doi}")
        for r in rows:
            print(f"        {r['key']:<34} sid={r['study_id']}")
        failures.append(f"duplicate DOI {doi}")

    # ---- 3. duplicate normalised title across files ------------------------
    by_ti = defaultdict(list)
    for r in records:
        if r["norm_title"]:
            by_ti[r["norm_title"]].append(r)
    dup_ti = cross_file(by_ti)
    print("\n[3] duplicate normalised title across files:")
    if not dup_ti:
        print("    none")
    for ti, rows in sorted(dup_ti.items()):
        print(f"    FAIL {ti[:80]!r}")
        for r in rows:
            print(f"        {r['key']:<34} sid={r['study_id']}")
        failures.append(f"duplicate title {ti[:60]!r}")

    # ---- thesis/article candidates (information only) ----------------------
    by_ay = defaultdict(list)
    for r in records:
        if r["author"] and r["year"]:
            by_ay[(r["author"], str(r["year"]))].append(r)

    print("\n[*] thesis/dissertation-vs-article candidate pairs "
          "(same first-author surname + year):")
    shown = 0
    for (author, year), rows in sorted(by_ay.items()):
        thesis = [r for r in rows if r["thesis_like"]]
        other = [r for r in rows if not r["thesis_like"]]
        if not thesis or not other:
            continue
        keys = {r["key"] for r in rows}
        if len(keys) < 2:
            continue
        shown += 1
        print(f"    {author} {year}:")
        for r in sorted(rows, key=lambda x: x["key"]):
            tag = "THESIS" if r["thesis_like"] else "article"
            print(f"        [{tag}] {r['key']:<32} sid={r['study_id']:<22} "
                  f"n={r['n']} doi={r['doi'] or '-'}")
    if not shown:
        print("    none")

    print("\n[*] same first-author + year with identical N across different files "
          "(salami-slice signal):")
    shown = 0
    for (author, year), rows in sorted(by_ay.items()):
        if len({r["key"] for r in rows}) < 2:
            continue
        by_n = defaultdict(set)
        for r in rows:
            if r["n"]:
                by_n[r["n"]].add(r["key"])
        for n, keys in sorted(by_n.items()):
            if len(keys) > 1:
                shown += 1
                print(f"    {author} {year}: N={n} across {sorted(keys)}")
    if not shown:
        print("    none")

    # ---- (a) same first-author surname + identical N across ANY year ----------
    # The existing same-year scan above is blind to a preprint/working paper -> article
    # pair (e.g. Pennycook 2023 -> 2024) and to thesis -> article pairs across years.
    print("\n[*] (a) same first-author surname + identical N across ANY year "
          "(cross-year salami signal):")
    shown = 0
    by_author_all = defaultdict(list)
    for r in records:
        if r["author"] and r["n"]:
            by_author_all[r["author"]].append(r)
    for author, rows in sorted(by_author_all.items()):
        by_n = defaultdict(list)
        for r in rows:
            by_n[r["n"]].append(r)
        for n, rs in sorted(by_n.items()):
            if len({x["key"] for x in rs}) < 2:
                continue
            years = {str(x["year"]) for x in rs if x["year"]}
            if len(years) < 2:
                continue                      # same-year pairs already printed above
            shown += 1
            print(f"    {author}: N={n} years={sorted(years)} "
                  f"files={sorted({x['key'] for x in rs})}")
    if not shown:
        print("    none")

    # ---- (b) shared author team (>=2 surnames) + identical N ------------------
    print("\n[*] (b) shared author team (>=2 surnames) + identical N across different files:")
    shown = 0
    by_n_all = defaultdict(list)
    for r in records:
        if r["n"]:
            by_n_all[r["n"]].append(r)
    for n, rows in sorted(by_n_all.items()):
        by_file = defaultdict(set)
        for r in rows:
            by_file[r["key"]] |= set(r["surnames"])
        keys = sorted(by_file)
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                shared = by_file[keys[i]] & by_file[keys[j]]
                if len(shared) >= 2:
                    shown += 1
                    print(f"    N={n}: {keys[i]} <-> {keys[j]} "
                          f"shared={sorted(shared)}")
    if not shown:
        print("    none")

    # ---- (c) cross-year preprint/thesis -> article candidate ------------------
    # One artifact a working paper / preprint / thesis / dissertation and the other an
    # article or preprint, different years, with high title similarity OR a matching N.
    print("\n[*] (c) cross-year preprint/thesis -> article candidates "
          "(title similarity or matching N):")
    shown = 0
    for author, rows in sorted(by_author_all.items()):
        keys = sorted({r["key"] for r in rows})
        if len(keys) < 2:
            continue
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                a = [r for r in rows if r["key"] == keys[i]]
                b = [r for r in rows if r["key"] == keys[j]]
                if {str(r["year"]) for r in a} == {str(r["year"]) for r in b}:
                    continue
                early = [r for r in a + b if r["thesis_like"] or r["preprint_like"]]
                later = [r for r in a + b
                         if not (r["thesis_like"] or r["preprint_like"])
                         and pub_kind({"Publication_Type": r["pub"]}) in ARTICLE_TYPES]
                if not early or not later:
                    continue
                best_sim = max((title_similarity(e["norm_title"], l["norm_title"])
                                for e in early for l in later), default=0.0)
                ns_early = {r["n"] for r in early if r["n"]}
                ns_later = {r["n"] for r in later if r["n"]}
                matched_n = sorted(ns_early & ns_later)
                if best_sim >= TITLE_SIM_THRESHOLD or matched_n:
                    shown += 1
                    why = []
                    if best_sim >= TITLE_SIM_THRESHOLD:
                        why.append(f"title_sim={best_sim:.2f}")
                    if matched_n:
                        why.append(f"matching N={matched_n}")
                    print(f"    {author}: {keys[i]} <-> {keys[j]}  [{', '.join(why)}]")
                    for r in sorted(a + b, key=lambda x: x["key"]):
                        tag = ("THESIS" if r["thesis_like"]
                               else "PREPRINT" if r["preprint_like"] else "article")
                        print(f"        [{tag}] {r['key']:<32} year={r['year']} "
                              f"n={r['n']} pub={r['pub']} doi={r['doi'] or '-'}")
    if not shown:
        print("    none")

    print()
    if failures:
        print(f"CORPUS VALIDATION FAIL ({len(failures)} issue(s))")
        return 1
    print("CORPUS VALIDATION PASS (0 duplicate Study_IDs/DOIs/titles)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
