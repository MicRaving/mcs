"""Locate a quote in a study PDF and emit a ``source_span`` for the coding JSON.

The Coding Studio viewer stores a marked source as

    {"page": 3, "rects": [[x0,y0,x1,y1], ...], "text": "<marked text>"}

where every coordinate is a fraction (0-1) of the page box.  This CLI finds the
page/rects for a quote so a coder (human or agent) can attach an exact location
to a DV's ``source_span`` (and set ``source_quote`` / ``source_location``).

Usage
    python Scripts/mark_source.py "<Study Key>" "<quote>" [--page N] [--pdf PATH]
    python Scripts/mark_source.py --verify "<coding.json>"

The first form prints one JSON object (or exits 2 when the quote is not found).
The second form checks a coding file: every DV with a ``source_quote`` should
carry a valid ``source_span`` whose text matches the quote.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

B = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, B)

LIGATURES = {0xFB00: "ff", 0xFB01: "fi", 0xFB02: "fl", 0xFB03: "ffi", 0xFB04: "ffl"}


def _norm_with_spans(text):
    """Normalise for comparison; return (normalised, [(raw_start, raw_end), ...])."""
    out, spans = [], []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\u00ad":
            i += 1
            continue
        if ch == "-" and i + 1 < n and text[i + 1].isspace():
            i += 2
            while i < n and text[i].isspace():
                i += 1
            continue
        if ch.isspace():
            start = i
            while i < n and text[i].isspace():
                i += 1
            out.append(" ")
            spans.append((start, i))
            continue
        expansion = LIGATURES.get(ord(ch))
        if expansion is not None:
            for c in expansion:
                out.append(c)
                spans.append((i, i + 1))
            i += 1
            continue
        out.append(ch.casefold())
        spans.append((i, i + 1))
        i += 1
    return "".join(out), spans


def _norm(text):
    return _norm_with_spans(text)[0]


def page_spans(pdf_path, page_no, max_spans=6000):
    import pymupdf

    with pymupdf.open(pdf_path) as doc:
        if page_no < 1 or page_no > doc.page_count:
            return None
        page = doc[page_no - 1]
        box = page.rect
        if box.width <= 0 or box.height <= 0:
            return None
        spans = []
        for blk in page.get_text("dict").get("blocks", []):
            if blk.get("type") != 0:
                continue
            for line in blk.get("lines", []):
                for sp in line.get("spans", []):
                    txt = str(sp.get("text") or "")
                    if not txt.strip():
                        continue
                    x0, y0, x1, y1 = sp.get("bbox") or (0, 0, 0, 0)
                    spans.append([
                        (x0 - box.x0) / box.width, (y0 - box.y0) / box.height,
                        (x1 - box.x0) / box.width, (y1 - box.y0) / box.height, txt,
                    ])
                    if len(spans) >= max_spans:
                        break
                if len(spans) >= max_spans:
                    break
            if len(spans) >= max_spans:
                break
        return {"page": page_no, "pages": doc.page_count, "spans": spans}


def _match_in_page(spans, quote):
    """Return (indexes, matched_text, confidence) or None."""
    page_text = " ".join(s[4] for s in spans)
    owner = []            # char index -> span index
    for si, s in enumerate(spans):
        take = len(s[4])
        owner.extend([si] * take)
        owner.append(si)  # the joining space belongs to the span before it
    # correct owner length (page_text = spans joined by single spaces)
    norm_page, spans_map = _norm_with_spans(page_text)
    norm_quote = _norm(quote).strip()
    if not norm_quote:
        return None
    start = 0
    while True:
        idx = norm_page.find(norm_quote, start)
        if idx < 0:
            break
        raw0, raw1 = spans_map[idx][0], spans_map[idx + len(norm_quote) - 1][1]
        # map raw char positions back to span ownership
        lo = 0
        for si, s in enumerate(spans):
            if lo <= raw0 < lo + len(s[4]) + 1:
                first = si
                break
            lo += len(s[4]) + 1
        else:
            first = 0
        lo = 0
        for si, s in enumerate(spans):
            if lo <= raw1 <= lo + len(s[4]) + 1:
                last = si
                break
            lo += len(s[4]) + 1
        else:
            last = len(spans) - 1
        idxs = list(range(first, last + 1))
        text = " ".join(spans[i][4] for i in idxs).strip()
        return idxs, text, "exact"
    # word-sequence fallback (whitespace-insensitive, in order)
    words = re.findall(r"\S+", quote)
    if len(words) >= 3:
        page_words = []
        for si, s in enumerate(spans):
            for w in re.findall(r"\S+", s[4]):
                page_words.append((w, si))
        for i in range(len(page_words) - len(words) + 1):
            if [w for w, _ in page_words[i:i + len(words)]] == words:
                idxs = sorted({si for _, si in page_words[i:i + len(words)]})
                text = " ".join(spans[j][4] for j in idxs).strip()
                return idxs, text, "words"
    return None


def _rects(spans, idxs):
    """Union of the matched spans, one rect per text line (grouped by y)."""
    lines = {}
    for i in idxs:
        s = spans[i]
        key = round(s[1], 3)
        if key not in lines:
            lines[key] = [s[0], s[1], s[2], s[3]]
        else:
            r = lines[key]
            r[0] = min(r[0], s[0]); r[1] = min(r[1], s[1])
            r[2] = max(r[2], s[2]); r[3] = max(r[3], s[3])
    out = []
    for r in sorted(lines.values(), key=lambda r: (r[1], r[0])):
        out.append([round(max(0.0, r[0]), 5), round(max(0.0, r[1]), 5),
                    round(min(1.0, r[2]), 5), round(min(1.0, r[3]), 5)])
    return out


def locate(pdf_path, quote, page=None):
    probe = page_spans(pdf_path, page) if page else None
    pages = [page] if page else list(range(1, (page_spans(pdf_path, 1) or {}).get("pages", 0) + 1))
    for p in pages:
        data = probe if (page and p == page) else page_spans(pdf_path, p)
        if not data:
            continue
        hit = _match_in_page(data["spans"], quote)
        if hit:
            idxs, text, conf = hit
            return {"page": p, "rects": _rects(data["spans"], idxs),
                    "text": text, "confidence": conf}
    return None


def pdf_for(key, explicit=None):
    if explicit:
        return explicit
    for cand in (os.path.join(B, "Papers", key + ".pdf"),
                 os.path.join(B, "Papers", key + ".PDF")):
        if os.path.isfile(cand):
            return cand
    return None


def verify(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    recs = data if isinstance(data, list) else [data]
    n_dv = n_quote = n_span = problems = 0
    issues = []
    for rec in recs:
        sid = rec.get("Study_ID") or "?"
        for dv in (rec.get("DV_Metrics") or []):
            if not isinstance(dv, dict):
                continue
            n_dv += 1
            quote = str(dv.get("source_quote") or "").strip()
            span = dv.get("source_span")
            if quote:
                n_quote += 1
            if isinstance(span, dict) and span.get("page") and span.get("rects"):
                n_span += 1
                if quote and _norm(quote) and _norm(quote) not in _norm(str(span.get("text") or "")):
                    problems += 1
                    issues.append(f"{sid}: span text does not contain source_quote ({dv.get('dv_name')})")
            elif quote:
                problems += 1
                issues.append(f"{sid}: source_quote without source_span ({dv.get('dv_name')})")
    print(json.dumps({"file": os.path.relpath(path, B), "dv": n_dv, "with_quote": n_quote,
                      "with_span": n_span, "problems": problems, "issues": issues[:20]},
                     ensure_ascii=False, indent=1))
    return 0 if problems == 0 else 2


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("key", nargs="?")
    ap.add_argument("quote", nargs="?")
    ap.add_argument("--page", type=int)
    ap.add_argument("--pdf")
    ap.add_argument("--verify")
    args = ap.parse_args()
    if args.verify:
        return verify(args.verify)
    if not args.key or not args.quote:
        ap.error("key and quote are required (or use --verify)")
    pdf = pdf_for(args.key, args.pdf)
    if not pdf:
        print(json.dumps({"error": "PDF not found", "key": args.key}), file=sys.stderr)
        return 2
    hit = locate(pdf, args.quote, args.page)
    if not hit:
        print(json.dumps({"error": "quote not found", "key": args.key,
                          "quote": args.quote[:120]}), file=sys.stderr)
        return 2
    print(json.dumps(hit, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
