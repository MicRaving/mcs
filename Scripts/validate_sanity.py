"""Corpus sanity validation: coded title/author vs PDF+Text, participant N vs paper/report,
and a garbled-text detector.

This institutionalises three requirements:

1. **TitleAuthor** -- every ``Extraction/<Key>.json`` coded ``Title``/``Authors`` must be
   present in (i) the first ~6000 chars of ``Text/<Key>.txt`` and (ii) the first page of
   ``Papers/<Key>.pdf``.  A title-token overlap below ``TITLE_THRESHOLD`` or a missing first
   author surname is flagged.  ``WRONG_PAPER`` is the hard case: the source's title shares
   **no distinctive token** with the coded title and the overall overlap is low -- i.e. the
   file clearly belongs to a different paper.  Known-benign cases (thesis cover pages,
   Elsevier COVID cover sheets, author-name-format artifacts) are recorded in
   ``Scripts/sanity_exceptions.csv`` (columns ``key,check,reason``) instead of re-flagged.

2. **Participants** -- for every coded experiment the ``N_Recruited`` / ``N_Analyzed`` digit
   strings (with and without thousands separators) are looked up in the paper ``Text`` and in
   the ``Reproduction`` report.  ``discrepancy`` is True when the report states its own N
   values and none of them matches either coded N.

3. **TextQuality** -- a garbled-extraction detector over the first 4000 chars of every
   ``Text/*.txt``: glyph-index tokens (``/g12``), a very low alphabetic ratio, an
   almost-zero common-English stopword ratio (custom-font cipher), or a high control-char
   ratio.  Writes ``Results/text_quality.csv``.

Products (``Results/``): ``sanity_report.xlsx`` (sheets ``TitleAuthor``, ``Participants``,
``TextQuality``) and ``text_quality.csv``.  Intermediates (if any) go to ``temp/``.

Run:  python Scripts/validate_sanity.py [--fail-on-hard] [--quiet]
Exit: 0 normally; with ``--fail-on-hard`` 1 if any un-excepted ``WRONG_PAPER`` or garbled
      flag remains (usable in CI).
"""
import argparse
import csv
import glob
import json
import os
import re
import sys
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTRACT = os.path.join(ROOT, "Extraction")
TEXT = os.path.join(ROOT, "Text")
PAPERS = os.path.join(ROOT, "Papers")
REPRO = os.path.join(ROOT, "Reproduction")
RESULTS = os.path.join(ROOT, "Results")
EXCEPTIONS = os.path.join(ROOT, "Scripts", "sanity_exceptions.csv")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# ---------------------------------------------------------------- thresholds
TITLE_HEAD = 6000          # chars of Text used for title matching
AUTHOR_HEAD = 6000         # chars of source used for the first-author surname
PDF_HEAD = 6000            # chars of PDF page 1
TITLE_THRESHOLD = 0.50     # token overlap below this is a title flag
WRONG_OVERLAP = 0.50       # WRONG_PAPER also requires overlap < this
QUALITY_HEAD = 4000        # chars used by the garbled detector
ALPHA_MIN = 0.55           # alphabetic chars / non-space chars
GLYPH_MAX = 0.05           # fraction of ``/g12``-style tokens
STOPWORD_MIN = 0.005       # fraction of common-English tokens (cipher => ~0)
CTRL_MAX = 0.10            # fraction of control characters (cipher => high)
N_TOKENS_MIN = 40          # ignore tiny snippets for the stopword rule

# Generic words that do not distinguish one title from another.
GENERIC = set(
    "a an the and or but of to in on for with from by as at is are was were be been being "
    "this that these those it its his her their our your they them we you all any can may "
    "might will would should could not no new news study studies effect effects impact "
    "impacts role roles using use used information about between among during after before "
    "over under more less most than then also such other others into out up down how what "
    "when where who why".split()
)

STOPWORDS = set(
    "the of and to in a is that for it as was with be by on not he this are or his from at "
    "which but have an they you we all their has had were what when where who will would can "
    "could should there here them him she her its our your".split()
)

GLYPH_TOKEN = re.compile(r"^/[a-z]\d+$")


# ---------------------------------------------------------------- normalisation
def fold(s):
    """NFKD-fold and drop combining marks."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c))


def norm(s):
    """Lowercase, strip markup, keep only alphanumerics as single-spaced tokens."""
    s = fold(s).lower()
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def title_tokens(s):
    return [w for w in norm(s).split() if len(w) > 2]


def first_surname(rec):
    """First author's surname, robust to 'Surname, Given' / 'Given Surname' / ';' lists."""
    authors = str(rec.get("Authors") or "").strip()
    if authors:
        first = authors.split(";")[0].strip()
        if "," in first:
            return first.split(",")[0].strip()
        toks = first.split()
        return toks[-1] if toks else ""
    a1 = str(rec.get("Author_1") or "").strip()
    if not a1:
        return ""
    if "," in a1:
        return a1.split(",")[0].strip()
    toks = a1.split()
    return toks[-1] if toks else ""


def title_overlap(coded_title, source_head):
    tw = set(title_tokens(coded_title))
    if not tw:
        return None, 0, 0
    hw = set(norm(source_head).split())
    inter = tw & hw
    distinct = {w for w in tw if w not in GENERIC}
    dist_inter = distinct & hw
    return len(inter) / len(tw), len(inter), len(dist_inter)


def read_text_head(path, n=TITLE_HEAD):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(n)
    except OSError:
        return None


def read_pdf_p1(path, n=PDF_HEAD):
    """First-page text via PyMuPDF, falling back to pypdf.  Returns (text|None, note)."""
    if not os.path.exists(path):
        return None, "no PDF"
    try:
        import pymupdf
        doc = pymupdf.open(path)
        txt = doc[0].get_text() if doc.page_count else ""
        doc.close()
        return txt[:n], ""
    except Exception as exc:  # noqa: BLE001
        note = f"pymupdf: {type(exc).__name__}"
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        txt = (reader.pages[0].extract_text() or "") if reader.pages else ""
        return txt[:n], note
    except Exception as exc:  # noqa: BLE001
        return None, f"{note}; pypdf: {type(exc).__name__}"


# ---------------------------------------------------------------- exceptions
def load_exceptions(path=EXCEPTIONS):
    """``(key, check) -> reason`` plus the special ``('*', check)`` wildcards."""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            key = str(row.get("key") or "").strip()
            check = str(row.get("check") or "").strip()
            reason = str(row.get("reason") or "").strip()
            if key and check:
                out[(key, check)] = reason
    return out


def excepted(exc, key, *checks):
    for c in checks:
        if (key, c) in exc:
            return exc[(key, c)]
        if (key, "*") in exc:
            return exc[(key, "*")]
        if ("*", c) in exc:
            return exc[("*", c)]
    return None


# ---------------------------------------------------------------- extraction metadata
def load_metadata():
    """key -> dict(title, authors, experiments=[(Study_ID, N_Recruited, N_Analyzed)])"""
    meta = {}
    for path in sorted(glob.glob(os.path.join(EXTRACT, "*.json"))):
        key = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:  # noqa: BLE001
            continue
        exps = [o for o in (data if isinstance(data, list) else [data]) if isinstance(o, dict)]
        if not exps:
            continue
        first = exps[0]
        meta[key] = dict(
            title=str(first.get("Title") or ""),
            authors=str(first.get("Authors") or first.get("Author_1") or ""),
            experiments=[(str(e.get("Study_ID") or ""), e.get("N_Recruited"), e.get("N_Analyzed"))
                         for e in exps],
        )
    return meta


# ---------------------------------------------------------------- check 1: title/author
def check_title_author(meta, exc):
    rows = []
    for key in sorted(meta):
        m = meta[key]
        title, authors = m["title"], m["authors"]
        text_head = read_text_head(os.path.join(TEXT, f"{key}.txt"))
        pdf_head, pdf_note = read_pdf_p1(os.path.join(PAPERS, f"{key}.pdf"))

        notes = []
        if text_head is None:
            notes.append("no Text")
        if pdf_head is None:
            notes.append(pdf_note or "no PDF")

        t_over = t_inter = t_dist = None
        if text_head is not None:
            t_over, t_inter, t_dist = title_overlap(title, text_head)
        p_over = p_inter = p_dist = None
        if pdf_head is not None:
            p_over, p_inter, p_dist = title_overlap(title, pdf_head)

        surname = norm(first_surname({"Authors": authors})).split()
        surname = surname[0] if surname else ""
        t_auth = (surname in norm(text_head or "")) if (surname and text_head is not None) else None
        p_auth = (surname in norm(pdf_head or "")) if (surname and pdf_head is not None) else None

        t_title_flag = t_over is not None and t_over < TITLE_THRESHOLD
        p_title_flag = p_over is not None and p_over < TITLE_THRESHOLD
        t_auth_flag = t_auth is False
        p_auth_flag = p_auth is False

        # Hard "different paper": a source shares no distinctive title token and has a low
        # overall overlap.  A PDF page-1 failure alone is not enough -- thesis/COVID cover
        # pages legitimately omit the title, so the PDF verdict is trusted only when the
        # Text is missing or also fails (or when the Text itself is the wrong paper, as for
        # Tulin et al. (2025 a)).
        t_wrong = (t_over is not None and t_over < WRONG_OVERLAP and t_dist == 0
                   and t_inter < len(title_tokens(title)))
        p_wrong = (p_over is not None and p_over < WRONG_OVERLAP and p_dist == 0
                   and p_inter < len(title_tokens(title)))
        text_ok = t_over is not None and t_over >= TITLE_THRESHOLD
        wrong = bool(t_wrong or (p_wrong and not text_ok))

        title_flag = t_title_flag or p_title_flag
        author_flag = t_auth_flag or p_auth_flag

        reason = None
        if wrong:
            reason = excepted(exc, key, "WRONG_PAPER", "TitleAuthor")
        elif title_flag:
            reason = excepted(exc, key, "TitleOverlap", "TitleAuthor")
        if reason is None and author_flag:
            reason = excepted(exc, key, "AuthorSurname", "TitleAuthor")

        flagged = bool(wrong or title_flag or author_flag) and reason is None
        rows.append(dict(
            key=key, coded_title=title[:120], coded_authors=authors[:120],
            text_overlap=round(t_over, 3) if t_over is not None else None,
            pdf_overlap=round(p_over, 3) if p_over is not None else None,
            text_distinct_hits=t_dist, pdf_distinct_hits=p_dist,
            text_author_found=t_auth, pdf_author_found=p_auth,
            title_flagged=title_flag, author_flagged=author_flag, wrong_paper=wrong,
            excepted=bool(reason), exception_reason=reason or "",
            flagged=flagged, notes="; ".join(notes),
        ))
    return rows


# ---------------------------------------------------------------- check 2: participants
REPORT_N_RES = [
    re.compile(r"(?<![\w.])[Nn]\s*[=:]\s*(\d[\d,]*)"),                       # N = 1,234 / n=123
    re.compile(r"(?<![\w.])[Nn]\s+(?:recruited|analysed|analyzed|total|final|included|"
               r"participants|subjects|respondents)\b\s*[=:]?\s*(\d[\d,]*)"),  # N recruited 123
    re.compile(r"\(\s*[Nn]\s*=\s*(\d[\d,]*)\s*\)"),                          # (n = 123)
]


def parse_report_ns(txt):
    out = set()
    for rx in REPORT_N_RES:
        for m in rx.finditer(txt):
            v = m.group(1).replace(",", "").strip()
            if v.isdigit():
                out.add(int(v))
    return out


def int_or_none(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return int(f) if f == int(f) else None


def digit_present(text, n):
    """Exact number present with non-digit boundaries, with or without thousands sep."""
    for form in (str(n), f"{n:,}"):
        if re.search(r"(?<!\d)" + re.escape(form) + r"(?!\d)", text):
            return True
    return False


def check_participants(meta):
    rows = []
    for key in sorted(meta):
        rp = os.path.join(REPRO, f"{key}.md")
        report = None
        if os.path.exists(rp):
            report = read_text_head(rp, 10 ** 9) or ""
        report_ns = parse_report_ns(report) if report is not None else set()
        text = read_text_head(os.path.join(TEXT, f"{key}.txt"), 10 ** 9)
        for sid, nrec, nana in meta[key]["experiments"]:
            vals = sorted({int_or_none(nrec), int_or_none(nana)} - {None})
            found_text = None if text is None else any(digit_present(text, v) for v in vals)
            found_report = (None if report is None
                            else any(digit_present(report, v) for v in vals))
            report_has_n = bool(report_ns)
            match = bool(set(vals) & report_ns)
            discrepancy = bool(report is not None and report_has_n and not match and vals)
            notes = []
            if text is None:
                notes.append("no Text")
            if report is None:
                notes.append("no Reproduction report")
            rows.append(dict(
                key=key, Study_ID=sid,
                N_Recruited=nrec, N_Analyzed=nana,
                n_values=";".join(str(v) for v in vals),
                found_in_text=found_text, found_in_report=found_report,
                report_available=report is not None,
                report_n_values=";".join(str(v) for v in sorted(report_ns)),
                discrepancy=discrepancy, notes="; ".join(notes),
            ))
    return rows


# ---------------------------------------------------------------- check 3: text quality
def text_quality_metrics(text):
    nw = [c for c in text if not c.isspace()]
    alpha = (sum(c.isalpha() for c in nw) / len(nw)) if nw else 0.0
    ws = text.split()
    glyph = (sum(1 for w in ws if GLYPH_TOKEN.match(w)) / len(ws)) if ws else 0.0
    low = [w.lower().strip(".,;:()[]\"'") for w in ws]
    stop = (sum(1 for w in low if w in STOPWORDS) / len(ws)) if ws else 0.0
    ctrl = (sum(1 for c in text if ord(c) < 32 and c not in "\n\r\t") / len(text)) if text else 0.0
    return alpha, glyph, stop, ctrl, len(ws)


def check_text_quality(exc):
    rows = []
    for path in sorted(glob.glob(os.path.join(TEXT, "*.txt"))):
        key = os.path.splitext(os.path.basename(path))[0]
        head = read_text_head(path, QUALITY_HEAD)
        if head is None:
            head = ""
        alpha, glyph, stop, ctrl, ntok = text_quality_metrics(head)
        garbled = bool(
            glyph > GLYPH_MAX
            or alpha < ALPHA_MIN
            or (ntok >= N_TOKENS_MIN and stop < STOPWORD_MIN)
            or ctrl > CTRL_MAX
        )
        reason = excepted(exc, key, "TextQuality") if garbled else None
        notes = []
        if glyph > GLYPH_MAX:
            notes.append("glyph-index tokens")
        if alpha < ALPHA_MIN:
            notes.append("low alpha ratio")
        if ntok >= N_TOKENS_MIN and stop < STOPWORD_MIN:
            notes.append("no common-English tokens (cipher?)")
        if ctrl > CTRL_MAX:
            notes.append("high control-char ratio")
        rows.append(dict(
            key=key, alpha_ratio=round(alpha, 4), glyph_index_ratio=round(glyph, 4),
            stopword_ratio=round(stop, 4), ctrl_ratio=round(ctrl, 4), n_tokens=ntok,
            garbled=garbled, excepted=bool(reason), exception_reason=reason or "",
            flagged=bool(garbled and reason is None), notes="; ".join(notes),
        ))
    return rows


# ---------------------------------------------------------------- main
# ---------------------------------------------------------------- check 4: duplicates
def check_duplicate_sources():
    """Flag byte-identical PDFs / near-identical text heads under different keys.

    A swapped paper is invisible to the title-overlap test when the wrong source is
    topically close (e.g. two conspiracy-belief papers); an identical file under two
    study keys is direct evidence of a copy/mapping defect.
    """
    import hashlib

    pdf_groups, text_groups = {}, {}
    for p in sorted(glob.glob(os.path.join(PAPERS, "*.pdf"))):
        key = os.path.splitext(os.path.basename(p))[0]
        try:
            with open(p, "rb") as fh:
                h = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            continue
        pdf_groups.setdefault(h, []).append(key)
    for p in sorted(glob.glob(os.path.join(TEXT, "*.txt"))):
        key = os.path.splitext(os.path.basename(p))[0]
        head = read_text_head(p, 6000)
        if not head:
            continue
        h = hashlib.sha1(norm(head).encode("utf-8")).hexdigest()
        text_groups.setdefault(h, []).append(key)
    rows = []
    for kind, groups in (("pdf", pdf_groups), ("text", text_groups)):
        for h, keys in groups.items():
            if len(keys) < 2:
                continue
            for k in keys:
                rows.append({"kind": kind, "key": k,
                             "duplicate_of": "; ".join(x for x in keys if x != k),
                             "hash": h[:12], "flagged": True})
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fail-on-hard", action="store_true",
                    help="exit 1 if any un-excepted WRONG_PAPER or garbled flag remains")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    exc = load_exceptions()
    meta = load_metadata()

    ta = check_title_author(meta, exc)
    pt = check_participants(meta)
    tq = check_text_quality(exc)
    dup = check_duplicate_sources()

    # ---- write the workbook ----
    os.makedirs(RESULTS, exist_ok=True)
    import pandas as pd
    xlsx = os.path.join(RESULTS, "sanity_report.xlsx")
    with pd.ExcelWriter(xlsx, engine="openpyxl") as xw:
        pd.DataFrame(ta).to_excel(xw, sheet_name="TitleAuthor", index=False)
        pd.DataFrame(pt).to_excel(xw, sheet_name="Participants", index=False)
        pd.DataFrame(tq).to_excel(xw, sheet_name="TextQuality", index=False)
        pd.DataFrame(dup).to_excel(xw, sheet_name="Duplicates", index=False)
    tq_csv = os.path.join(RESULTS, "text_quality.csv")
    pd.DataFrame(tq).to_csv(tq_csv, index=False, encoding="utf-8-sig")

    # ---- summary ----
    title_flags = [r for r in ta if r["title_flagged"]]
    author_flags = [r for r in ta if r["author_flagged"]]
    wrong = [r for r in ta if r["wrong_paper"]]
    wrong_hard = [r for r in wrong if r["flagged"]]
    flagged_ta = [r for r in ta if r["flagged"]]
    garbled = [r for r in tq if r["garbled"]]
    garbled_hard = [r for r in garbled if r["flagged"]]
    disc = [r for r in pt if r["discrepancy"]]
    no_text = [r for r in pt if r["found_in_text"] is None]

    if not args.quiet:
        print(f"validate_sanity: {len(meta)} Extraction keys, "
              f"{len(tq)} Text files, {len(pt)} experiments")
        print(f"  exceptions loaded: {len(exc)} (Scripts/sanity_exceptions.csv)")
        print(f"[TitleAuthor] title flags {len(title_flags)} | author flags {len(author_flags)} "
              f"| WRONG_PAPER {len(wrong)} | un-excepted flags {len(flagged_ta)}")
        for r in sorted(wrong, key=lambda x: x["key"]):
            tag = "HARD" if r["flagged"] else "excepted"
            print(f"    WRONG_PAPER [{tag}] {r['key']}: text={r['text_overlap']} "
                  f"pdf={r['pdf_overlap']} | {r['coded_title'][:60]}")
        for r in flagged_ta:
            if not r["wrong_paper"]:
                print(f"    flagged {r['key']}: text={r['text_overlap']} pdf={r['pdf_overlap']} "
                      f"title={r['title_flagged']} author={r['author_flagged']} | "
                      f"{r['coded_title'][:55]}")
        print(f"[Participants] experiments {len(pt)} | no paper Text {len(no_text)} "
              f"| strict discrepancies {len(disc)}")
        for r in disc:
            print(f"    discrepancy {r['key']} / {r['Study_ID']}: coded={r['n_values']} "
                  f"report={r['report_n_values']}")
        print(f"[TextQuality] garbled {len(garbled)} (un-excepted {len(garbled_hard)})")
        for r in garbled:
            tag = "HARD" if r["flagged"] else "excepted"
            print(f"    garbled [{tag}] {r['key']}: alpha={r['alpha_ratio']} "
                  f"glyph={r['glyph_index_ratio']} stop={r['stopword_ratio']} "
                  f"ctrl={r['ctrl_ratio']} ({r['notes']})")
        dup_keys = sorted({r["key"] for r in dup})
        print(f"[Duplicates] identical PDF/text sources {len(dup_keys)} key(s)")
        for r in dup:
            print(f"    duplicate {r['kind']} {r['key']} == {r['duplicate_of']} ({r['hash']})")
        print(f"wrote {xlsx}")
        print(f"wrote {tq_csv}")

    if args.fail_on_hard and (wrong_hard or garbled_hard or dup):
        print(f"SANITY VALIDATION FAIL: {len(wrong_hard)} WRONG_PAPER, "
              f"{len(garbled_hard)} garbled (un-excepted), "
              f"{len({r['key'] for r in dup})} duplicate source(s)")
        return 1
    print("SANITY VALIDATION PASS (report written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
