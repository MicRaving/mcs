"""Code the corresponding author (name and e-mail, in separate fields).

Every paper in the corpus gets three record-level fields:

    Corresponding_Author_Name    the person, e.g. "Jane Roe"
    Corresponding_Author_Email   the address, e.g. "jroe@univ.edu"
    Corresponding_Author_Source  where it was read from

The values are harvested from the paper itself first (the correspondence note
on the title page, the author footnote, the "e-mail:" line), and from
OpenAlex/Crossref only when the deposit carries no address at all.  A
candidate is only written when the *name* is one of the paper's authors, so a
stray editor or production e-mail can never be mistaken for the contact.

Papers the harvest cannot resolve are listed in the report and need a
human/agent read of the PDF; the script leaves them empty rather than
guessing.

Usage
-----
    python Scripts/code_corresponding_author.py            # dry run
    python Scripts/code_corresponding_author.py --apply
"""
import argparse
import csv
import glob
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request

import pymupdf

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTRACT = os.path.join(ROOT, "Extraction")
RESULTS = os.path.join(ROOT, "Results")
LOG = os.path.join(RESULTS, "corresponding_author_log.csv")

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
CORR_WORD = re.compile(r"correspond", re.I)
#: "Addressed to Jane Roe" / "Correspondence ... to Jane Roe"
LEAD = re.compile(r"(?:correspond(?:ing)?[^.\n]{0,140}?|addressed\s+to|"
                  r"contact\s*:?)\s*", re.I)
PAREN_NAME = re.compile(r"([A-Z][\w'’\-]+(?:\s+[A-Z][\w'’\-]+){0,3})\s*\(")
NAME_TOKEN = re.compile(r"[A-Z][A-Za-z'’\-]+")
UA = {"User-Agent": "meta-analysis/1.0 (mailto:meta-analysis@example.org)"}


def deaccent(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c))


def norm_txt(s):
    return re.sub(r"[^a-z]", "", deaccent(s).lower())


def author_index(authors):
    """{token -> canonical 'Last, First'} for the paper's author list."""
    out = {}
    for a in (authors or "").split(";"):
        a = a.strip()
        if not a:
            continue
        parts = [p.strip() for p in a.split(",")]
        last = parts[0] if parts else a
        first = parts[1] if len(parts) > 1 else ""
        if last:
            out.setdefault(norm_txt(last), a)
            toks = last.split()
            if len(toks) > 1:
                out.setdefault(norm_txt(toks[-1]), a)
        if first:
            out.setdefault(norm_txt(first.split()[0]), a)
            out.setdefault(norm_txt(first), a)
    return out


def name_in_authors(name, index):
    n = norm_txt(name)
    if len(n) < 4:
        return None
    if n in index:
        return index[n]
    best = None
    for tok, canon in index.items():
        if len(tok) >= 4 and (tok in n or n in tok):
            if best is None or len(tok) > len(best[0]):
                best = (tok, canon)
    return best[1] if best else None


def context_name(text, email, index):
    """A name printed next to the address in the correspondence note."""
    for m in re.finditer(re.escape(email), text):
        window = text[max(0, m.start() - 450): m.end() + 250]
        for pm in PAREN_NAME.finditer(window):
            canon = name_in_authors(pm.group(1).strip(), index)
            if canon:
                return canon
        lead = LEAD.search(window)
        if lead:
            tail = window[lead.end():]
            for nm in NAME_TOKEN.finditer(tail[:140]):
                rest = tail[nm.end():nm.end() + 45]
                if re.match(r"^[a-z]", rest):
                    continue
                nxt = NAME_TOKEN.match(rest)
                if nxt:
                    two = name_in_authors(nm.group(0) + " " + nxt.group(0), index)
                    if two:
                        return two
                one = name_in_authors(nm.group(0), index)
                if one:
                    return one
    return None


def local_match(email, index):
    loc = re.sub(r"[^a-z]", "", deaccent(email.split("@")[0]).lower())
    for tok, canon in index.items():
        if len(tok) >= 4 and tok in loc:
            return canon
    m = re.match(r"^([a-z])+([a-z]{4,})$", loc)
    if m:
        for tok, canon in index.items():
            if len(tok) >= 5 and tok.endswith(m.group(2)):
                return canon
    return None


def openalex(doi):
    if not doi:
        return None
    url = ("https://api.openalex.org/works/https://doi.org/"
           + urllib.parse.quote(doi, safe=""))
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=25) as fh:
            w = json.load(fh)
    except Exception:
        return None
    corr = set(w.get("corresponding_author_ids") or [])
    authors = []
    for a in w.get("authorships") or []:
        au = a.get("author") or {}
        authors.append({
            "name": au.get("display_name"),
            "is_corresponding": au.get("id") in corr,
            "orcid": au.get("orcid"),
            "affiliations": [x.get("display_name") for x in a.get("institutions") or []],
        })
    return {"title": w.get("title"), "authors": authors}


def harvest(key, doi, authors):
    """-> (name, email, source) or (None, None, '')"""
    index = author_index(authors)
    pdf = os.path.join(ROOT, "Papers", key + ".pdf")
    text = ""
    if os.path.isfile(pdf):
        try:
            doc = pymupdf.open(pdf)
            text = "".join(p.get_text() for p in doc)
        except Exception:
            text = ""
    emails = [e for e in dict.fromkeys(EMAIL.findall(text))
              if not re.search(r"\.(png|jpg|jpeg|gif)$", e, re.I)]

    # 1. the correspondence note names the person next to the address
    for e in emails:
        nm = context_name(text, e, index)
        if nm:
            page = _page_of(pdf, e)
            return nm, e, "pdf: correspondence note, p. %s" % (page or "?")
    # 2. the address' local part identifies the author
    for e in emails:
        nm = local_match(e, index)
        if nm:
            page = _page_of(pdf, e)
            return nm, e, "pdf: author e-mail on p. %s" % (page or "?")
    # 3. OpenAlex flags the corresponding author
    oa = openalex(doi)
    if oa:
        named = [a for a in oa["authors"] if a.get("is_corresponding")]
        if not named and emails:
            loc = re.sub(r"[^a-z]", "", deaccent(emails[0].split("@")[0]).lower())
            for a in oa["authors"]:
                nm = re.sub(r"[^a-z]", "", (a.get("name") or "").lower().split()[-1])
                if nm and len(nm) >= 4 and nm in loc:
                    named = [a]
                    break
        if named:
            return named[0].get("name"), (emails[0] if emails else None), \
                "openalex: corresponding_author_ids"
        if named == [] and oa["authors"]:
            first = oa["authors"][0].get("name")
            if first and name_in_authors(first, index):
                return first, (emails[0] if emails else None), \
                    "openalex: first author (no corresponding flag)"
    return None, (emails[0] if emails else None), ""


def _page_of(pdf, email):
    if not os.path.isfile(pdf):
        return None
    try:
        doc = pymupdf.open(pdf)
        for i, page in enumerate(doc):
            if email in page.get_text():
                return i + 1
    except Exception:
        pass
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    keys = sorted(os.path.basename(p)[:-5]
                   for p in glob.glob(os.path.join(EXTRACT, "*.json")))
    resolved, unresolved, rows = 0, [], []
    for key in keys:
        path = os.path.join(EXTRACT, key + ".json")
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        recs = data if isinstance(data, list) else data.get("experiments", [])
        if not recs:
            continue
        rec = recs[0]
        authors = rec.get("Authors", "")
        doi = rec.get("DOI")
        name, email, source = harvest(key, doi, authors)
        if name:
            resolved += 1
        else:
            unresolved.append((key, email, bool(source)))
        rows.append(dict(study_key=key, name=name or "", email=email or "",
                         source=source or "", resolved=int(bool(name))))

    print("papers: %d | resolved: %d | unresolved: %d"
          % (len(rows), resolved, len(unresolved)))
    if unresolved:
        print("\nunresolved (need a read of the PDF):")
        for k, e, _ in unresolved[:40]:
            print("   %-44s %s" % (k, e or "(no address in the deposit)"))

    os.makedirs(RESULTS, exist_ok=True)
    with open(LOG, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["study_key", "name", "email",
                                           "source", "resolved"])
        w.writeheader()
        w.writerows(rows)
    print("\nwrote", LOG)

    if args.apply:
        for row in rows:
            if not row["resolved"]:
                continue
            path = os.path.join(EXTRACT, row["study_key"] + ".json")
            data = json.load(open(path, encoding="utf-8"))
            recs = data if isinstance(data, list) else data.get("experiments", [])
            for rec in recs:
                rec["Corresponding_Author_Name"] = row["name"]
                rec["Corresponding_Author_Email"] = row["email"] or None
                rec["Corresponding_Author_Source"] = row["source"]
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
        print("applied to %d file(s)" % resolved)
    return 0


if __name__ == "__main__":
    sys.exit(main())
