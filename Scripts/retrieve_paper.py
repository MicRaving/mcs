"""Retrieve one paper's PDF and text for the DS coding route.

Given a study key and a DOI (or a title), this resolves the work on OpenAlex,
tries every open-access PDF it names (plus Unpaywall and PubMed Central), saves
the first real PDF as ``Papers/<Key>.pdf`` and extracts ``Text/<Key>.txt``.

    python Scripts/retrieve_paper.py --key "Doe et al. (2026)" --doi 10.1038/xyz
    python Scripts/retrieve_paper.py --key "Doe et al. (2026)" --title "..." --year 2026

It prints a JSON report and exits 0 when a PDF was written, 1 otherwise.  An
existing ``Papers/<Key>.pdf`` is never overwritten.

The text file is plain concatenated page text, the same shape as every other
``Text/<Key>.txt`` in the corpus (no page markers, so a quote that crosses a page
break still matches ``Scripts/validate_extraction.py``).  A page-indexed copy is
written to ``temp/ds_route/<Key>/pages.json`` for locating a quote's page.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAPERS = os.path.join(ROOT, "Papers")
TEXT = os.path.join(ROOT, "Text")
TEMP = os.path.join(ROOT, "temp", "ds_route")

UA = {"User-Agent": "Mozilla/5.0 (research meta-analysis; mailto:meta@example.org)"}
TIMEOUT = 60
MAILTO = "meta@example.org"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get(url, **kw):
    kw.setdefault("timeout", TIMEOUT)
    kw.setdefault("headers", UA)
    return requests.get(url, **kw)


def openalex_by_doi(doi):
    try:
        r = get(f"https://api.openalex.org/works/doi:{doi}")
        if r.status_code == 200:
            return r.json()
    except Exception as exc:  # noqa: BLE001
        log(f"openalex doi: {exc}")
    return None


def openalex_by_title(title, year=None):
    try:
        params = {"search": title, "per-page": 5}
        if year:
            params["filter"] = f"publication_year:{year}"
        r = get("https://api.openalex.org/works", params=params)
        if r.status_code != 200:
            return None
        for work in r.json().get("results", []):
            if _title_match(work.get("title") or work.get("display_name"), title):
                return work
    except Exception as exc:  # noqa: BLE001
        log(f"openalex title: {exc}")
    return None


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _title_match(a, b):
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long_ = sorted((na, nb), key=len)
    return len(short) > 25 and short in long_


def unpaywall(doi):
    try:
        r = get(f"https://api.unpaywall.org/v2/{doi}", params={"email": MAILTO})
        if r.status_code == 200:
            return r.json()
    except Exception as exc:  # noqa: BLE001
        log(f"unpaywall: {exc}")
    return None


def semanticscholar(doi):
    try:
        r = get("https://api.semanticscholar.org/graph/v1/paper/DOI:" + doi,
                params={"fields": "openAccessPdf,externalIds,title"})
        if r.status_code == 200:
            return r.json()
    except Exception as exc:  # noqa: BLE001
        log(f"semanticscholar: {exc}")
    return None


def publisher_patterns(doi):
    """Publisher-specific PDF URLs, tried after the metadata services."""
    d = doi.lower()
    out = []
    if d.startswith("10.1080/"):
        out.append(f"https://www.tandfonline.com/doi/pdf/{doi}?download=true")
        out.append(f"https://www.tandfonline.com/doi/epdf/{doi}?needAccess=true")
    if d.startswith("10.1177/"):
        out.append(f"https://journals.sagepub.com/doi/pdf/{doi}?download=true")
    if d.startswith("10.1111/"):
        out.append(f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}?download=true")
    if d.startswith("10.1007/"):
        out.append(f"https://link.springer.com/content/pdf/{doi}.pdf")
    if d.startswith("10.1037/"):
        out.append(f"https://psycnet.apa.org/record/{doi}")
    if d.startswith("10.1093/"):
        out.append(f"https://academic.oup.com/doi/pdf/{doi}")
    if d.startswith("10.1017/"):
        out.append(f"https://www.cambridge.org/core/services/aop-cambridge-core/"
                   f"content/view/{doi}")
    if d.startswith("10.1086/"):
        out.append(f"https://www.journals.uchicago.edu/doi/pdf/{doi}")
    if d.startswith("10.1002/"):
        out.append(f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}?download=true")
    if d.startswith("10.1186/"):
        out.append(f"https://link.springer.com/content/pdf/{doi}.pdf")
    if d.startswith("10.1016/"):
        out.append(f"https://www.sciencedirect.com/science/article/pii/"
                   f"{doi.split('/')[-1]}/pdfft")
    return out


def _meta_pdf_url(html):
    """The `citation_pdf_url` a publisher landing page advertises."""
    import re as _re
    for pat in (r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']'):
        m = _re.search(pat, html, _re.I)
        if m:
            return m.group(1)
    return None


def _pmcid(raw):
    """'PMC13035968' from any of the shapes OpenAlex/PMC use."""
    if not raw:
        return None
    digits = re.sub(r"[^0-9]", "", str(raw))
    return f"PMC{digits}" if digits else None


def _normalise_url(u):
    """OSF/PsyArXiv landing URLs -> the direct download endpoint."""
    if not u:
        return u
    if "osf.io/download/" in u:
        return u
    m = re.search(r"(?:psyarxiv|socarxiv|edarxiv|osf\.io)/(?:.*?/)?([a-z0-9]{5})/?(?:download)?/?$", u)
    if m:
        return f"https://osf.io/download/{m.group(1)}/"
    return u


def candidate_urls(work, doi):
    urls = []

    def add(u):
        u = _normalise_url(u)
        if u and u not in urls:
            urls.append(u)

    if work:
        for loc in [work.get("best_oa_location")] + (work.get("locations") or []):
            if not loc:
                continue
            if loc.get("is_oa") is False:
                continue
            add(loc.get("pdf_url"))
        add((work.get("content_urls") or {}).get("pdf"))
        pmcid = _pmcid((work.get("ids") or {}).get("pmcid"))
        if pmcid:
            add(f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/")
            add(f"https://europepmc.org/articles/{pmcid}?pdf=render")
    if doi:
        up = unpaywall(doi)
        if up:
            for loc in up.get("oa_locations") or []:
                add(loc.get("url_for_pdf"))
                add(loc.get("url"))
            best = up.get("best_oa_location") or {}
            add(best.get("url_for_pdf"))
            add(best.get("url"))
        ss = semanticscholar(doi)
        if ss:
            add((ss.get("openAccessPdf") or {}).get("url"))
        for u in publisher_patterns(doi):
            add(u)
    return urls


def download_pdf(url, dest, depth=0):
    try:
        r = get(url, allow_redirects=True, stream=True)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        ctype = (r.headers.get("Content-Type") or "").lower()
        first = b""
        chunks = []
        for chunk in r.iter_content(65536):
            if not first:
                first = chunk[:1024]
            chunks.append(chunk)
            if sum(len(c) for c in chunks) > 120 * 1024 * 1024:
                return False, "too large"
        blob = b"".join(chunks)
        if not blob.startswith(b"%PDF") and b"%PDF" not in first:
            # a landing page: follow the PDF it advertises, once
            if depth == 0 and b"<html" in blob[:4096].lower():
                target = _meta_pdf_url(blob[:400000].decode("utf-8", "replace"))
                if target:
                    if target.startswith("/"):
                        from urllib.parse import urljoin
                        target = urljoin(url, target)
                    ok, why = download_pdf(target, dest, depth=1)
                    return (ok, f"via citation_pdf_url: {why}") if ok else (False, why)
            return False, f"not a PDF ({ctype or 'unknown'})"
        tmp = dest + ".part"
        with open(tmp, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, dest)
        return True, f"{len(blob)} bytes"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def extract_text(pdf_path, key):
    """Write Text/<Key>.txt and temp/ds_route/<Key>/pages.json."""
    try:
        import pymupdf
    except ImportError:  # pragma: no cover
        import fitz as pymupdf
    doc = pymupdf.open(pdf_path)
    pages = [page.get_text() for page in doc]
    doc.close()
    os.makedirs(TEXT, exist_ok=True)
    out = os.path.join(TEXT, f"{key}.txt")
    if not os.path.exists(out):
        with open(out, "w", encoding="utf-8") as fh:
            fh.write("\n".join(pages))
    pdir = os.path.join(TEMP, key)
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, "pages.json"), "w", encoding="utf-8") as fh:
        json.dump({str(i + 1): t for i, t in enumerate(pages)}, fh, ensure_ascii=False)
    return len(pages), sum(len(p) for p in pages)


def europepmc_text(pmcid):
    """Full text as plain text from the Europe PMC REST service (no PDF needed)."""
    try:
        r = get(f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML")
        if r.status_code != 200 or b"<" not in r.content[:200]:
            return None
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.content, "xml")
        for tag in soup.find_all(["ref-list", "back"]):
            tag.decompose()
        return soup.get_text("\n", strip=True)
    except Exception as exc:  # noqa: BLE001
        log(f"europepmc text: {exc}")
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--key", required=True)
    ap.add_argument("--doi", default=None)
    ap.add_argument("--title", default=None)
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--url", action="append", default=None,
                    help="an extra PDF URL to try first (repeatable)")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    key = args.key
    doi = (args.doi or "").strip().lower()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)

    pdf_path = os.path.join(PAPERS, f"{key}.pdf")
    text_path = os.path.join(TEXT, f"{key}.txt")
    os.makedirs(PAPERS, exist_ok=True)
    os.makedirs(TEXT, exist_ok=True)
    os.makedirs(os.path.join(TEMP, key), exist_ok=True)

    report = {"key": key, "doi": doi or None, "title": args.title,
              "pdf": None, "pages": 0, "chars": 0, "text_only": False,
              "tried": [], "error": None}

    if os.path.exists(pdf_path) and not args.force:
        report["pdf"] = os.path.relpath(pdf_path, ROOT).replace("\\", "/")
        report["note"] = "already present"
    else:
        work = openalex_by_doi(doi) if doi else None
        if work is None and args.title:
            work = openalex_by_title(args.title, args.year)
        pmcid = None
        if work:
            report["openalex"] = work.get("id")
            report["oa_status"] = (work.get("open_access") or {}).get("oa_status")
            pmcid = _pmcid((work.get("ids") or {}).get("pmcid"))
            if not doi:
                doi = re.sub(r"^https?://(doi\.org/)?", "",
                             (work.get("doi") or ""))
                report["doi"] = doi or None
        urls = list(args.url or []) + candidate_urls(work, doi)
        for url in urls:
            ok, why = download_pdf(url, pdf_path)
            report["tried"].append({"url": url, "ok": ok, "why": why})
            if ok:
                report["pdf"] = os.path.relpath(pdf_path, ROOT).replace("\\", "/")
                break
        if not report["pdf"]:
            report["error"] = "every candidate URL failed"

    if report["pdf"]:
        try:
            pages, chars = extract_text(pdf_path, key)
            report["pages"] = pages
            report["chars"] = chars
        except Exception as exc:  # noqa: BLE001
            report["error"] = f"text extraction failed: {exc}"
    elif not os.path.exists(text_path) and pmcid:
        # no PDF anywhere: keep the full text so the paper can still be coded
        text = europepmc_text(pmcid)
        if text:
            with open(text_path, "w", encoding="utf-8") as fh:
                fh.write(text)
            report["text_only"] = True
            report["chars"] = len(text)
            report["error"] = None
            report["note"] = "full text from Europe PMC (no PDF: no page/rect spans)"
            pdir = os.path.join(TEMP, key)
            with open(os.path.join(pdir, "pages.json"), "w", encoding="utf-8") as fh:
                json.dump({"1": text}, fh, ensure_ascii=False)

    with open(os.path.join(TEMP, key, "retrieval.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if (report["pdf"] and report["chars"]) or report["text_only"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
