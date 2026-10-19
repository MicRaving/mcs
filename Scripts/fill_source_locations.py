"""Fill ``source_location`` + ``source_quote`` for every coded DV row (D8).

For every ``DV_Metrics[]`` row in every ``Extraction/<Key>.json`` this populates
two schema-v3.1 fields:

* ``source_location`` — where the coded statistic lives in the paper, e.g.
  ``"Table 2"``, ``"Fig. 1"``, ``"Results, p. 735"`` (``""`` when truly absent).
* ``source_quote``    — the verbatim sentence / table fragment from
  ``Text/<Key>.txt`` that contains the coded statistic (normalised whitespace),
  so it can be re-found.

No data is invented.  A value is written only when it can be traced to one of:

1. the per-DV ``conversion_method`` provenance already recorded in the coding
   (``source_location`` only);
2. the study's ``Extraction/<Key>.md`` coding notes, when a location reference
   sits next to a strong match of the DV name / contrast labels
   (``source_location`` only);
3. the study's ``Text/<Key>.txt``, where the coded statistic itself is located
   by searching several numeric formats/precisions and the surrounding passage
   is taken as the quote; the nearest table / figure / section heading above it
   gives the location.

Rows where neither a quote nor a location can be established are left empty and
listed in ``Results/extraction_source_location_gaps.csv`` with the reason.

Safety
------
* ``--dry`` is the default; pass ``--apply`` to write.
* Before a file is modified it is copied once to
  ``archive/ws_d2_extractions_2026-09-22/<Key>.json``.
* Every change is appended to ``Results/extraction_source_location_log.csv``.
* Re-running ``--apply`` is idempotent (existing non-empty fields are kept).

Usage::

    python Scripts/fill_source_locations.py                 # dry run, prints plan
    python Scripts/fill_source_locations.py --apply
    python Scripts/fill_source_locations.py --apply --limit 20
"""
import argparse
import csv
import glob
import json
import os
import re
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTRACT = os.path.join(ROOT, "Extraction")
TEXT = os.path.join(ROOT, "Text")
RESULTS = os.path.join(ROOT, "Results")
ARCHIVE = os.path.join(ROOT, "archive", "ws_d2_extractions_2026-09-22")
LOG = os.path.join(RESULTS, "extraction_source_location_log.csv")
GAPS = os.path.join(RESULTS, "extraction_source_location_gaps.csv")

LOG_COLS = ["key", "experiment_id", "dv_name", "field", "old", "new", "rule", "ts"]
GAP_COLS = ["key", "experiment_id", "dv_name", "missing", "reason"]

_RUN_TS = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

NEW_FIELDS = ("source_location", "source_quote")

# ---------------------------------------------------------------------------
# numeric normalisation / formatting
# ---------------------------------------------------------------------------


def norm(s):
    """Whitespace + typographic normalisation used for both quote and text."""
    if not s:
        return ""
    s = str(s)
    s = s.replace("\u00ad", "")
    for a, b in [("\ufb01", "fi"), ("\ufb02", "fl"), ("\ufb00", "ff"),
                 ("\ufb03", "ffi"), ("\ufb04", "ffl"),
                 ("\u2013", "-"), ("\u2014", "-"), ("\u2212", "-"),
                 ("\u2010", "-"), ("\u2011", "-"),
                 ("\u2018", "'"), ("\u2019", "'"), ("\u201c", '"'),
                 ("\u201d", '"'), ("\u00a0", " "), ("\u2026", "...")]:
        s = s.replace(a, b)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def num_forms(v):
    """String forms of a numeric value at several precisions (plus no-leading-zero)."""
    out = set()
    try:
        f = float(v)
    except (TypeError, ValueError):
        return out
    if f != f or f in (float("inf"), float("-inf")):
        return out
    if f == int(f) and abs(f) < 1e6:
        out.add(str(int(f)))
    for dec in range(1, 5):
        s = f"{f:.{dec}f}"
        out.add(s)
        if s.startswith("0."):
            out.add(s[1:])
        elif s.startswith("-0."):
            out.add("-" + s[2:])
    # keep only forms with >=2 digits so a lone "0" never matches
    return {s for s in out if len(re.sub(r"[^0-9]", "", s)) >= 2}


# ---------------------------------------------------------------------------
# statistic fields, tiers and anchors
# ---------------------------------------------------------------------------

TIER1 = {"d_val", "g_val", "t_val", "F_val", "r_val", "z_val", "chi2_val",
         "odds_ratio", "risk_ratio", "eta2_p", "beta_val"}
TIER2 = {"d_ci_low", "d_ci_high", "g_ci_low", "g_ci_high", "or_ci_low",
         "or_ci_high", "eta2_p_ci_low", "eta2_p_ci_high"}
TIER3 = {"m1i", "m2i", "sd1i", "sd2i", "m1i_pre", "m2i_pre", "sd1i_pre",
         "sd2i_pre", "m1i_post", "m2i_post", "sd1i_post", "sd2i_post"}

WEIGHT = {}
for _f in TIER1:
    WEIGHT[_f] = 3
for _f in TIER2:
    WEIGHT[_f] = 2
for _f in TIER3:
    WEIGHT[_f] = 1
PRIMARY = list(WEIGHT)

LABELS = {
    "d_val": ["d", "cohen's d", "cohen d", "cohens d", "cohen"],
    "g_val": ["g", "hedges g", "hedges", "cohen's g"],
    "t_val": ["t"],
    "F_val": ["f"],
    "r_val": ["r", "pearson", "pearson's r", "pearson r"],
    "z_val": ["z"],
    "chi2_val": ["chi2", "\u03c7\u00b2", "\U0001d74c", "x2", "chi-square",
                 "chi squared"],
    "odds_ratio": ["or", "odds", "odds ratio", "odds ratios"],
    "risk_ratio": ["rr", "risk", "relative risk", "risk ratio"],
    "eta2_p": ["eta", "\u03b7", "partial", "\u03b7p2", "\u03b72",
               "\u03b7p\u00b2", "partial eta2"],
    "beta_val": ["b", "\u03b2", "beta", "coeff", "coefficient", "estimate",
                 "slope", "coef"],
}
for _f in TIER2:
    LABELS[_f] = ["ci", "confidence interval", "95% ci", "95%"]
for _f in TIER3:
    LABELS[_f] = (["sd", "s.d.", "se", "standard deviation", "standard error"]
                  if _f.startswith("sd") else ["m", "mean", "means"])

DF_ANCHOR = re.compile(
    r"(?:^|[\s(\[=,:;])(?:F|t|r|z|\u03c72|chi2)\s*\([^)]{0,20}\)\s*[=:<]?\s*$",
    re.I)
BAD_PRE = re.compile(r"(?:\bp|\bP|\bn|\bN|\bdf|\bSE|\bsd)\s*[=<>]\s*$")
CITE_RE = re.compile(r"\[\s*\d+(?:\s*,\s*\d+){1,}\s*\]")
FOOTNOTE_RE = re.compile(
    r"significance level|standard errors?|robust standard|\bnote[:.]|notes[:.]|"
    r"^\s*\*+\s*p|\bp\s*<\s*0?\.0\s*,|source[:.]|dependent variable",
    re.IGNORECASE)

STOP = {"with", "from", "that", "this", "post", "pre", "follow", "mean",
        "difference", "effect", "change", "versus", "control", "condition",
        "group", "score", "scores", "among", "between", "within", "after",
        "before", "both", "only", "more", "less", "than", "significant",
        "positive", "negative", "total", "item", "items", "test", "news",
        "true", "false", "fake", "real", "correct", "accuracy", "willingness",
        "perceived", "intention", "knowledge", "confidence", "trust", "attitude"}

# ---------------------------------------------------------------------------
# location references
# ---------------------------------------------------------------------------

SUP_RE = re.compile(r"Supplementary\s+(?:Section|Table|Fig(?:ure)?)\s+[A-Z0-9.]+")
TABLE_RE = re.compile(r"Table\s+[A-Z]?\d+(?:[.\-][0-9A-Za-z]+)?")
FIG_RE = re.compile(r"Fig(?:ure)?\.?\s+[A-Z]?\d+(?:[.\-][0-9A-Za-z]+)?")
APP_RE = re.compile(r"Appendix\s+[A-Z0-9]+")
SECT_RE = re.compile(r"Sect(?:ion)?\.?\s+[A-Z0-9]+(?:\.\d+)*")
PAGE_RE = re.compile(r"\bp\.?\s*\d+(?:\s*[-\u2013]\s*\d+)?")

HEAD_RE = re.compile(
    r"(Table\s+[A-Z]?\d+(?:[.\-][0-9A-Za-z]+)?"
    r"|Figure\s+[A-Z]?\d+(?:[.\-][0-9A-Za-z]+)?"
    r"|Fig\.?\s+[A-Z]?\d+(?:[.\-][0-9A-Za-z]+)?"
    r"|Appendix\s+[A-Z0-9]+"
    r"|Sect(?:ion)?\.?\s+\d+(?:\.\d+)*"
    r"|\b(?:METHODS|METHOD|RESULTS|DISCUSSION|ABSTRACT|INTRODUCTION|CONCLUSION|"
    r"SUPPLEMENTARY|APPENDIX|Participants|Procedure|Measures|"
    r"Statistical Analysis|Analyses|Results and Discussion)\b)")
_STRONG_HEAD = re.compile(r"^(Table|Fig|Figure|Appendix)")

LOC_ANY = re.compile(
    r"(?:Supplementary\s+(?:Section|Table|Fig(?:ure)?)\s+[A-Z0-9.]+"
    r"|Table\s+[A-Z]?\d+(?:[.\-][0-9A-Za-z]+)?"
    r"|Fig(?:ure)?\.?\s+[A-Z]?\d+(?:[.\-][0-9A-Za-z]+)?"
    r"|Appendix\s+[A-Z0-9]+"
    r"|Sect(?:ion)?\.?\s+[A-Z0-9]+(?:\.\d+)*"
    r"|\bp\.?\s*\d+(?:\s*[-\u2013]\s*\d+)?)")


def parse_location_from_cm(cm):
    """Extract a source location from the coded ``conversion_method`` prose."""
    if not cm:
        return ""
    seg = None
    m = re.search(r"source\s*:\s*([^;.]{3,140})", cm, re.I)
    if m:
        seg = m.group(1)
    else:
        m = re.search(
            r"(?:from|in|reported in|taken from|tabulated in|reproduced from|"
            r"based on|described in|see)\s+([^;.]{3,120})", cm, re.I)
        if m:
            seg = m.group(1)
    parts = []
    for probe in (seg, cm):
        if not probe:
            continue
        for rx in (SUP_RE, TABLE_RE, FIG_RE, APP_RE, SECT_RE, PAGE_RE):
            mm = rx.search(probe)
            if mm:
                t = re.sub(r"\s+", " ", mm.group(0)).strip()
                if t not in parts:
                    parts.append(t)
        if parts:
            break
    if not parts:
        for w in ("Results", "Methods", "Abstract", "Discussion",
                  "Introduction", "Supplementary"):
            if re.search(r"\b" + w + r"\b", cm, re.I):
                parts.append(w)
                break
    # drop a token that is fully contained in a more specific token
    # (e.g. "Section G" inside "Supplementary Section G").
    deduped = []
    for p in parts:
        if any(p != q and p.lower() in q.lower() for q in parts):
            continue
        if p not in deduped:
            deduped.append(p)
    return ", ".join(deduped)


def heading_positions(text):
    return [(m.start(), m.group(0)) for m in HEAD_RE.finditer(text)]


def infer_location(text, pos, heads):
    """Nearest heading above ``pos``; a close Table/Fig caption below wins."""
    prev = None
    for hp, label in heads:
        if hp < pos and pos - hp <= 9000:
            prev = (hp, label)
        elif hp >= pos:
            break
    nxt = None
    for hp, label in heads:
        if hp >= pos:
            if _STRONG_HEAD.match(label) and hp - pos <= 500:
                nxt = (hp, label)
            break
    pick = prev
    if nxt and (prev is None or (nxt[0] - pos) < (pos - prev[0])):
        pick = nxt
    if pick:
        return re.sub(r"\s+", " ", pick[1]).strip()
    return ""


def region_for(text, loc, heads):
    """Text span (start, end) belonging to a declared Table/Figure heading."""
    if not loc:
        return None
    m = re.match(r"(Table|Fig(?:ure)?\.?|Appendix)\s+([A-Z]?\d+(?:[.\-][0-9A-Za-z]+)?)",
                 loc)
    if not m:
        return None
    rx = re.compile(r"\b" + re.escape(m.group(1).rstrip(".")) + r"\.?\s+"
                    + re.escape(m.group(2)) + r"\b")
    cands = [mm.start() for mm in rx.finditer(text)]
    if not cands:
        return None
    start = None
    for c in cands:
        tail = text[c:c + 160]
        if re.search(r"\.{3,}\s*\d+", tail):   # table-of-contents entry
            continue
        start = c
        break
    if start is None:
        start = cands[0]
    end = min(len(text), start + 12000)
    for hp, _label in heads:
        if hp > start + 20:
            end = hp
            break
    return (start, end)


def extract_quote(text, pos, end, cap=420):
    left = max(0, pos - 280)
    right = min(len(text), end + 280)
    seg = text[left:right]
    mp = pos - left
    start = 0
    for m in re.finditer(r"(?:[.!?]\s+|;\s+)", seg[:mp]):
        start = m.end()
    endm = len(seg)
    m2 = re.search(r"[.!?](?=\s+[A-Z0-9\u201c])|$", seg[mp:])
    if m2:
        endm = mp + m2.end()
    q = seg[start:endm].strip()
    if len(q) > cap:
        q = q[:cap].rsplit(" ", 1)[0]
    return q


# ---------------------------------------------------------------------------
# DV token / candidate engine
# ---------------------------------------------------------------------------


def dv_tokens(dv):
    parts = [dv.get("dv_name"), dv.get("comparison_label"),
             dv.get("group1_label"), dv.get("group2_label")]
    toks = []
    for p in parts:
        for t in re.findall(r"[a-z]{4,}", (p or "").lower()):
            if t not in STOP and t not in toks:
                toks.append(t)
    return toks[:14]


def anchor_for(before, field):
    if field in ("t_val", "F_val", "r_val", "z_val", "chi2_val") \
            and DF_ANCHOR.search(before):
        return True
    for lab in LABELS.get(field, []):
        if re.search(r"(?:^|[\s(\[=,:;])" + re.escape(lab) + r"\s*[=:<(]?\s*$",
                     before, re.I):
            return True
    return False


def window_support(text, pos, field_forms, toks):
    lo, hi = max(0, pos - 110), pos + 110
    win = text[lo:hi]
    support = 0
    present = []
    for field, forms in field_forms.items():
        for form in forms:
            if re.search(r"(?<![\d])" + re.escape(form) + r"(?![\d])", win):
                support += WEIGHT[field]
                present.append(field)
                break
    wl = win.lower()
    nh = sum(1 for t in toks if t in wl)
    return support, nh, present


def build_field_forms(dv):
    field_forms = {}
    for f in PRIMARY:
        v = dv.get(f)
        if v in (None, ""):
            continue
        forms = num_forms(v)
        if forms:
            field_forms[f] = forms
    return field_forms


def candidates(text, dv, region, heads):
    field_forms = build_field_forms(dv)
    if not field_forms:
        return []
    toks = dv_tokens(dv)
    seen = {}
    for f, forms in field_forms.items():
        for form in forms:
            pat = re.compile(r"(?<![\d])" + re.escape(form) + r"(?![\d])")
            for m in pat.finditer(text):
                pos, end = m.start(), m.end()
                before = text[max(0, pos - 24):pos]
                if BAD_PRE.search(before):
                    continue
                if any(cm.start() <= pos < cm.end()
                       for cm in CITE_RE.finditer(text, max(0, pos - 40),
                                                  min(len(text), end + 40))):
                    continue
                support, nh, present = window_support(text, pos, field_forms, toks)
                anch = anchor_for(before, f)
                q = extract_quote(text, pos, end)
                foot = bool(FOOTNOTE_RE.search(q))
                score = support + (2 if anch else 0) + min(nh, 3) - (8 if foot else 0)
                if region and region[0] <= pos < region[1]:
                    score += 4
                rec = {"score": score, "support": support, "field": f, "form": form,
                       "name_hits": nh, "anchor": anch, "distinct": len(present),
                       "present": present, "footnote": foot, "pos": pos, "quote": q,
                       "location": infer_location(text, pos, heads)}
                key = (pos, end)
                if key not in seen or rec["score"] > seen[key]["score"]:
                    seen[key] = rec
    return sorted(seen.values(), key=lambda r: (-r["score"], r["pos"]))


def accept(c):
    """D2 acceptance: strong co-occurrence, or one reported stat with support."""
    if c["support"] >= 4:
        return True
    if c["support"] >= 3 and (c["anchor"] or c["name_hits"] >= 1):
        return True
    return False


def name_fallback(text, dv, heads):
    """Last resort: locate a passage by the DV's distinctive name tokens."""
    toks = [t for t in dv_tokens(dv) if len(t) >= 5]
    if len(toks) < 2:
        return "", ""
    positions = {}
    for t in toks[:8]:
        for m in re.finditer(r"\b" + re.escape(t) + r"\b", text, re.I):
            positions.setdefault(m.start(), set()).add(t)
    best = None
    for pos, hit in positions.items():
        lo, hi = max(0, pos - 160), pos + 160
        win = text[lo:hi].lower()
        n = sum(1 for t in toks if t in win)
        if best is None or n > best[0]:
            best = (n, pos)
    if best is None or best[0] < 3:
        return "", ""
    pos = best[1]
    return infer_location(text, pos, heads), ""


def md_locations(key):
    path = os.path.join(EXTRACT, key + ".md")
    if not os.path.exists(path):
        return []
    try:
        txt = norm(open(path, encoding="utf-8", errors="replace").read())
    except OSError:
        return []
    out = []
    for m in LOC_ANY.finditer(txt):
        ctx = txt[max(0, m.start() - 200):m.end() + 200]
        out.append((re.sub(r"\s+", " ", m.group(0)).strip(), ctx))
    return out


def best_md_location(md_locs, dv):
    if not md_locs:
        return ""
    toks = dv_tokens(dv)
    best_score, best_loc = 0, ""
    for loc, ctx in md_locs:
        cl = ctx.lower()
        sc = sum(1 for t in toks if t in cl)
        if sc > best_score:
            best_score, best_loc = sc, loc
    return best_loc if best_score >= 3 else ""


# ---------------------------------------------------------------------------
# per-DV computation
# ---------------------------------------------------------------------------


def compute(dv, text, heads, md_locs):
    """Return (location, quote, rule, reason)."""
    loc_cm = parse_location_from_cm(dv.get("conversion_method"))
    region = region_for(text, loc_cm, heads)
    cs = candidates(text, dv, region, heads)
    pick = None
    for c in cs:
        if c["footnote"]:
            continue
        if accept(c):
            pick = c
            break
    if pick is None:
        for c in cs:            # fall back to a footnote quote only if nothing else
            if accept(c):
                pick = c
                break
    if pick is not None:
        quote = pick["quote"]
        loc_text = pick["location"]
        if loc_cm:
            location, rule = loc_cm, "cm_location+text_quote"
        elif loc_text:
            location, rule = loc_text, "text_heading+text_quote"
        else:
            location, rule = best_md_location(md_locs, dv), "md_location+text_quote"
        return location, quote, rule, ""
    # no statistic found in the text
    loc_name, _ = name_fallback(text, dv, heads)
    if loc_cm:
        return loc_cm, "", "cm_location_only", "no reported statistic found in text"
    if loc_name:
        return loc_name, "", "name_location_only", "no reported statistic found in text"
    loc_md = best_md_location(md_locs, dv)
    if loc_md:
        return loc_md, "", "md_location_only", "no reported statistic found in text"
    return "", "", "none", "statistic and DV name not located in text"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def detect_indent(text):
    m = re.match(r"\[\r?\n(\s+)\{", text)
    return len(m.group(1)) if m else 2


def blank(v):
    return v is None or (isinstance(v, str) and v.strip() == "")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (default: dry run)")
    ap.add_argument("--limit", type=int, default=0,
                    help="only process the first N Extraction files (debug)")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(EXTRACT, "*.json")))
    if args.limit:
        files = files[:args.limit]

    stats = {"rows": 0, "loc": 0, "quote": 0, "both": 0, "neither": 0}
    rule_counts = Counter()
    changes = []          # (path, data, indent, rows)
    gaps = []
    n_files = 0
    for path in files:
        n_files += 1
        key = os.path.splitext(os.path.basename(path))[0]
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
        try:
            data = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            print("SKIP (parse error) %s: %s" % (key, e))
            continue
        if not isinstance(data, list):
            data = [data]
        text_path = os.path.join(TEXT, key + ".txt")
        text = ""
        if os.path.exists(text_path):
            text = norm(open(text_path, encoding="utf-8", errors="replace").read())
        heads = heading_positions(text) if text else []
        md_locs = md_locations(key)

        file_rows = []
        for o in data:
            if not isinstance(o, dict):
                continue
            exp_id = o.get("Study_ID") or ""
            for dv in o.get("DV_Metrics") or []:
                if not isinstance(dv, dict):
                    continue
                stats["rows"] += 1
                if text:
                    location, quote, rule, reason = compute(dv, text, heads, md_locs)
                else:
                    location, quote, rule, reason = "", "", "none", "no Text/<Key>.txt"
                rule_counts[rule] += 1
                row_changes = []
                for field, new in (("source_location", location),
                                   ("source_quote", quote)):
                    old = dv.get(field)
                    if not blank(old):
                        continue                      # never overwrite coded values
                    if blank(new):
                        continue
                    row_changes.append({
                        "key": key, "experiment_id": exp_id,
                        "dv_name": dv.get("dv_name") or "", "field": field,
                        "old": old if old is not None else "", "new": new,
                        "rule": rule, "ts": _RUN_TS})
                if row_changes:
                    for ch in row_changes:
                        dv[ch["field"]] = ch["new"]
                    file_rows.extend(row_changes)

                has_loc = not blank(dv.get("source_location"))
                has_q = not blank(dv.get("source_quote"))
                stats["loc"] += 1 if has_loc else 0
                stats["quote"] += 1 if has_q else 0
                stats["both"] += 1 if (has_loc and has_q) else 0
                stats["neither"] += 1 if not (has_loc or has_q) else 0
                if not has_q or not has_loc:
                    if not (has_loc or has_q):
                        missing = "both"
                        reason = reason or "statistic and DV name not located in text"
                    elif has_loc:
                        missing = "source_quote"
                        reason = reason or "no reported statistic found in text"
                    else:
                        missing = "source_location"
                        reason = reason or "no heading/location found for the quote"
                    gaps.append({
                        "key": key, "experiment_id": exp_id,
                        "dv_name": dv.get("dv_name") or "", "missing": missing,
                        "reason": reason})
        if file_rows:
            changes.append((path, data, detect_indent(raw), file_rows))

    n_rows_changes = sum(len(r) for _, _, _, r in changes)
    print("== fill source_location / source_quote ==")
    print("files scanned: %d | DV rows: %d" % (n_files, stats["rows"]))
    print("filled location: %d | filled quote: %d | both: %d | neither: %d"
          % (stats["loc"], stats["quote"], stats["both"], stats["neither"]))
    print("files to rewrite: %d | field writes: %d" % (len(changes), n_rows_changes))
    print("gap rows: %d" % len(gaps))
    print("location/quote rule mix:")
    for rule, n in rule_counts.most_common():
        print("  %-28s %d" % (rule, n))

    if not args.apply:
        print("\nDRY RUN — no files written. Re-run with --apply to write.")
        for path, _, _, rows in changes[:8]:
            print("  would update %s (%d fields)"
                  % (os.path.basename(path), len(rows)))
        if len(changes) > 8:
            print("  ... and %d more files" % (len(changes) - 8))
        return 0

    os.makedirs(RESULTS, exist_ok=True)
    os.makedirs(ARCHIVE, exist_ok=True)
    write_header = not os.path.exists(LOG)
    with open(LOG, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LOG_COLS)
        if write_header:
            w.writeheader()
        for path, data, indent, rows in changes:
            key = os.path.splitext(os.path.basename(path))[0]
            backup = os.path.join(ARCHIVE, key + ".json")
            if not os.path.exists(backup):
                shutil.copy2(path, backup)
            with open(path, "w", encoding="utf-8") as out:
                json.dump(data, out, ensure_ascii=False, indent=indent)
                out.write("\n")
            for r in rows:
                w.writerow(r)
    with open(GAPS, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=GAP_COLS)
        w.writeheader()
        w.writerows(gaps)

    print("\nAPPLIED: %d files rewritten, %d fields logged to %s"
          % (len(changes), n_rows_changes, os.path.relpath(LOG, ROOT)))
    print("gaps written to %s (%d rows)" % (os.path.relpath(GAPS, ROOT), len(gaps)))
    print("originals archived under %s" % os.path.relpath(ARCHIVE, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
