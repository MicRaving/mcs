"""Draft the human master's decisions from the gathered evidence.

The evidence pass read the paper for each item in the human queue and
wrote what it shows to ``Results/human_evidence/<Key>.md``.  This script
turns that evidence into a *proposed value* per item, with the evidence
attached, so the human master can approve or override the list rather
than read 133 papers again.

The proposal is the evidence file's own recommendation; where there is no
evidence file for a key, the item is listed as still to be read.

Products
--------
``Results/human_decisions_draft.csv``    the proposals, one row per item
``Results/HUMAN_DECISIONS_DRAFT.md``     the same, grouped by field, for review
"""
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")
EVID = os.path.join(RESULTS, "human_evidence")
QUEUE = os.path.join(RESULTS, "coder_disagreement_human_queue.csv")
OUT_CSV = os.path.join(RESULTS, "human_decisions_draft.csv")
OUT_MD = os.path.join(RESULTS, "HUMAN_DECISIONS_DRAFT.md")

HEADING = re.compile(r"^##\s+(.+?)\s*$", re.M)
FIELD_LINE = re.compile(r"^Recommendation:\s*(.+?)\s*$", re.M)
EVID_LINE = re.compile(r"^Evidence:\s*(.+?)\s*$", re.M | re.S)
QUOTE_LINE = re.compile(r'^Relevant quote:\s*"(.+?)"', re.M | re.S)

# The evidence agents write a prose recommendation that usually LEADS with
# the value ("**1** for the ...", "`['C1', 'C2']` - i.e. ..."), sometimes
# with a caveat after it.  These are the shapes the value is read from.
BRACKETED = re.compile(r"\[\s*['\"]?(C[1-4])['\"]?(?:\s*,\s*['\"]?(C[1-4])['\"]?)*\s*\]")
AND_LIST = re.compile(r"\bC[1-4]\b(?:\s*(?:,|and|&)\s*\bC[1-4]\b)+")
LEADING_BIT = re.compile(r"^[^01]{0,40}?\b([01])\b")
CONDITIONAL = (
    "if the human", "the human must", "the human owns", "judgement call",
    "judgement", "arguable", "ambiguous", "the choice is between",
    "if the project reads", "depends", "not settled", "does not settle",
    "defer", "the human", "which of", "the paper does not",
)


def _clean(text):
    t = text or ""
    t = t.replace("**", " ").replace("`", " ").replace("*", " ")
    t = t.replace("’", "'").replace("‘", "'")
    return " ".join(t.split())


def parse_proposal(field, text):
    """-> (value, confidence) from a prose recommendation.

    ``value`` is the field's own type (``0``/``1`` for the two flags, a
    list of ``C1``-``C4`` for the criterion) or ``None``.  ``confidence``
    is ``clear``, ``conditional``, ``keep`` (the item is an artefact -
    keep the canonical value) or ``none``.
    """
    t = _clean(text)
    if not t:
        return None, "none"
    low = t.lower()

    if "artefact" in low or "artifact" in low:
        if "keep" in low or "no substantive disagreement" in low:
            return "KEEP_CANONICAL", "keep"
    if any(s in low for s in ("no basis to set it", "not stated", "cannot be set")):
        return None, "none"

    cond = any(s in low for s in CONDITIONAL)

    if field in ("eligible_primary", "dv_polarity"):
        m = LEADING_BIT.match(t)
        if not m:
            return None, "none"
        value = int(m.group(1))
        return value, ("conditional" if cond else "clear")

    if field == "Fulltext_Exclusion_Criterion":
        m = BRACKETED.search(t)
        if m:
            found = re.findall(r"C[1-4]", m.group(0))
            seen, out = set(), []
            for c in found:
                if c not in seen:
                    seen.add(c)
                    out.append(c)
            return out, ("conditional" if cond else "clear")
        # the leading tokens, however they are separated
        head = t[:44]
        toks = re.findall(r"\bC[1-4]\b", head)
        if toks:
            out = []
            for c in toks:
                if c not in out:
                    out.append(c)
            return out, ("conditional" if cond else "clear")
        if re.search(r"\bno criterion\b|\bmeets every criterion\b|\bnone\b",
                     low):
            return [], ("conditional" if cond else "clear")
        return None, "none"

    return None, "none"



def parse_evidence(path):
    """-> {heading: (recommendation, evidence, quote)}"""
    txt = open(path, encoding="utf-8").read()
    parts = HEADING.split(txt)
    out = {}
    for i in range(1, len(parts) - 1, 2):
        head = parts[i].strip()
        body = parts[i + 1]
        rec = FIELD_LINE.search(body)
        ev = EVID_LINE.search(body)
        q = QUOTE_LINE.search(body)
        out[head] = (
            rec.group(1).strip() if rec else "",
            " ".join(ev.group(1).split()) if ev else "",
            " ".join(q.group(1).split())[:400] if q else "",
        )
    return out


def _strip_paren(text):
    """The measure part of a name, without a trailing parenthetical.

    The queue's outcome name is the canonical ``dv_name``, which may
    embed the contrast ("Perceived ad truthfulness (manipulative vs
    truthful)"); the evidence file heads the block with the measure
    alone.  Both sides are therefore reduced to the measure part before
    they are compared.
    """
    t = " ".join(str(text or "").split())
    t = re.sub(r"\s*\([^()]*\)\s*$", "", t)
    return t.strip().lower()


def _same_measure(a, b):
    if not a or not b:
        return False
    x, y = _strip_paren(a), _strip_paren(b)
    if not x or not y:
        return False
    if x == y:
        return True
    return x in y or y in x


def find_block(evidence, field, dv):
    """The evidence block that answers an item.

    Two formats are in use.  The study-level one has a single
    ``## eligible_primary`` block naming the contrast, and a
    ``## <measure> (dv_polarity)`` block per measure.  The older
    per-item one has a ``## <measure> (<field>)`` block per item.  The
    study-level block wins for ``eligible_primary``, because it answers
    the question for the whole study.
    """
    if field == "eligible_primary" and "eligible_primary" in evidence:
        return evidence["eligible_primary"]
    if field == "Fulltext_Exclusion_Criterion" \
            and field in evidence:
        return evidence[field]
    if dv:
        for head, vals in evidence.items():
            h = head.lower()
            if h.endswith("(%s)" % field.lower()):
                if _same_measure(head, dv) or _same_measure(h, dv):
                    return vals
        # a measure named without its field
        for head, vals in evidence.items():
            if h == field.lower():
                continue
            if _same_measure(head, dv):
                return vals
    if field in evidence:
        return evidence[field]
    return ("", "", "")


def canonical_rows(key):
    """{dv_name: row} for the canonical file of a key."""
    path = os.path.join(ROOT, "Extraction", key + ".json")
    if not os.path.isfile(path):
        return {}
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}
    recs = d if isinstance(d, list) else d.get("experiments", [])
    out = {}
    for r in recs:
        for x in (r.get("DV_Metrics") or []):
            if isinstance(x, dict) and x.get("dv_name"):
                out.setdefault(str(x["dv_name"]), x)
    return out


def tokens(text):
    out = set()
    for w in re.findall(r"[a-z0-9%]+", str(text or "").lower()):
        if len(w) > 2 and w not in STOP_TOKENS:
            out.add(w)
    return out


QUOTED = re.compile(r"[\"“”']([^\"“”']{6,120})[\"“”']")
HYPHEN_VS = re.compile(r"([A-Za-z][\w ]{0,40}-vs-[\w ]{0,40})")
FOR_THE = re.compile(r"for the ([^,;.()—]{3,80})", re.I)

#: words that carry no identity, so they must not count against a
#: containment score
STOP_TOKENS = {
    "the", "and", "for", "with", "vs", "that", "this", "its", "from",
    "contrasts", "contrast", "condition", "conditions", "arm", "arms",
    "row", "rows", "paper", "study", "experiment", "one", "two", "not",
    "are", "was", "were", "which", "their", "there", "than", "then",
    "per", "its", "its", "eligible", "primary",
}


def _phrases(recommendation):
    """The contrast phrases a study-level answer names.

    The evidence agents quote the contrast they mean - "the eligible
    contrast is *intervention infographic vs control infographic*" - so
    the quoted spans are the candidates, together with the hyphenated
    ``A-vs-B`` forms and the phrase after "for the".  The whole text is
    the last resort.
    """
    out = []
    for m in QUOTED.finditer(recommendation):
        p = m.group(1).strip()
        if p:
            out.append(p)
    for m in HYPHEN_VS.finditer(recommendation):
        p = m.group(1).strip()
        if p:
            out.append(p)
    for m in FOR_THE.finditer(recommendation):
        p = m.group(1).strip()
        if p:
            out.append(p)
    for seg in re.split(r"[—–]|,|;", recommendation):
        p = seg.strip()
        if 3 < len(p) <= 120:
            out.append(p)
    if recommendation.strip():
        out.append(recommendation.strip())
    return out


def eligibility_from_contrast(recommendation, row):
    """The eligibility flag a study-level answer implies for one row.

    The evidence names the contrast that carries ``eligible_primary`` in
    the paper's own words; the canonical row names the same contrast in
    the coding's words.  The two are compared by **containment** - how
    much of the named contrast appears in the row's own labels - because
    the two wordings share the distinctive words (the arms) rather than
    the whole sentence.
    """
    rec = _clean(recommendation)
    low = rec.lower()
    if "no basis" in low:
        return None, "none"
    if low.startswith("none") or " no such contrast" in low:
        return 0, "clear"
    m = re.match(r"\s*([01])\b", rec)
    if m:
        digit = int(m.group(1))
        confidence = "clear"
    else:
        # the answer does not open with a value: it names a contrast and
        # says it qualifies.  A row that matches the named contrast is
        # eligible; a row that does not is not - and the item is marked
        # conditional, because the reading is inferred rather than stated.
        digit = 1
        confidence = "conditional"
    if digit == 0:
        return 0, confidence
    if not row:
        return None, "none"
    label = " ".join(str(row.get("comparison_label") or "").split())
    arms = " ".join(str(row.get(f) or "") for f in
                    ("group1_label", "group2_label"))
    row_tokens = tokens(label) | tokens(arms)
    best = 0.0
    for phrase in _phrases(rec):
        pt = tokens(phrase)
        if not pt:
            continue
        best = max(best, len(pt & row_tokens) / float(len(pt)))
    return (1 if best >= 0.5 else 0), confidence





def main():
    if not os.path.isfile(QUEUE):
        print("no human queue", file=sys.stderr)
        return 1
    rows = list(csv.DictReader(open(QUEUE, encoding="utf-8-sig", newline="")))
    evidence = {}
    if os.path.isdir(EVID):
        for fn in sorted(os.listdir(EVID)):
            if fn.endswith(".md"):
                try:
                    evidence[fn[:-3]] = parse_evidence(os.path.join(EVID, fn))
                except Exception:
                    pass

    out = []
    for r in rows:
        key = r["study_key"]
        field = r["field"]
        dv = r["dv"]
        ev = evidence.get(key, {})
        rec, text, quote = find_block(ev, field, dv)
        value, confidence = parse_proposal(field, rec)
        if field == "eligible_primary" and rec:
            # the study-level answer names the contrast in the paper's own
            # words; the canonical row names it in the coding's words, and
            # the row is eligible when the two agree
            rows_by_name = canonical_rows(key)
            row = rows_by_name.get(dv)
            if row is None:
                for name, r2 in rows_by_name.items():
                    if _same_measure(name, dv):
                        row = r2
                        break
            v2, c2 = eligibility_from_contrast(rec, row)
            if v2 is not None:
                value, confidence = v2, c2
        out.append(dict(
            study_key=key,
            field=field,
            dv=dv,
            competing_values=r["routes"],
            proposed=rec,
            proposed_value=("" if value is None
                            else (",".join(value) if isinstance(value, list)
                                  else value)),
            confidence=confidence,
            evidence=text,
            quote=quote,
            has_evidence="1" if rec else "0"))

    with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "study_key", "field", "dv", "competing_values", "proposed",
            "proposed_value", "confidence", "evidence", "quote",
            "has_evidence"])
        w.writeheader()
        w.writerows(out)

    covered = [o for o in out if o["has_evidence"] == "1"]
    missing = [o for o in out if o["has_evidence"] == "0"]
    conf = Counter(o["confidence"] for o in out)
    md = []
    md.append("# Draft decisions for the human master\n")
    md.append("\nThe evidence pass read the paper for the human queue; this is the "
               "result turned into a proposed value per item, with the evidence "
               "and the quote behind it. **Nothing has been written to the "
               "corpus.** Approve, override, or send back the items you want "
               "re-read.\n")
    md.append("\n| | |")
    md.append("|---|---|")
    md.append("| items in the queue | %d |" % len(out))
    md.append("| with a proposed value | %d |" % len(covered))
    md.append("| still to be read | %d |" % len(missing))
    md.append("| a clear value the paper settles | %d |"
              % conf.get("clear", 0))
    md.append("| a value with a condition attached | %d |"
              % conf.get("conditional", 0))
    md.append("| a row-alignment artefact - keep the canonical value | %d |"
              % conf.get("keep", 0))
    md.append("| no basis in the paper | %d |"
              % (conf.get("none", 0) + len(missing)))
    md.append("")
    md.append("")
    md.append("A **conditional** value is one the evidence makes depend on a "
              "judgement the paper does not settle; the condition is in the "
              "proposal text.  An **artefact** row is a disagreement the "
              "three routes only have because the rows are matched by "
              "position - the paper shows the routes agree, so the canonical "
              "value stands.")
    by_field = defaultdict(list)
    for o in out:
        by_field[o["field"] or o["dv"]].append(o)
    for field in sorted(by_field, key=lambda f: -len(by_field[f])):
        items = by_field[field]
        md.append("\n## %s — %d item%s\n"
                  % (field, len(items), "" if len(items) == 1 else "s"))
        for o in items:
            md.append("\n### %s%s" % (o["study_key"],
                                        (" — " + o["dv"]) if o["dv"] else ""))
            md.append("\n- **competing values:** %s" % o["competing_values"])
            if o["proposed"]:
                md.append("- **proposed value:** `%s` (%s)"
                          % (o["proposed_value"] or "—", o["confidence"]))
                md.append("- **proposal:** %s" % o["proposed"])
                md.append("- **evidence:** %s" % o["evidence"])
                if o["quote"]:
                    md.append("- **quote:** \"%s\"" % o["quote"])
            else:
                md.append("- **no evidence yet** — the paper has not been read "
                          "for this item")
            md.append("")
    with open(OUT_MD, "w", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")

    print("items: %d | proposed: %d | still to read: %d"
          % (len(out), len(covered), len(missing)))
    print("keys with an evidence file: %d of %d"
          % (len(evidence), len({r['study_key'] for r in rows})))
    print("by field:", dict(Counter(o["field"] for o in out)))
    print("\nwrote", OUT_CSV)
    print("wrote", OUT_MD)
    return 0


if __name__ == "__main__":
    sys.exit(main())
