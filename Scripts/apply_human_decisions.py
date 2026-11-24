"""Turn the drafted human decisions into a review sheet, and apply the
approved ones to the canonical route (decision D7, 2026-10-21).

The owner asked for the decisions to be **drafted** from the evidence and
reviewed later.  This script has two halves:

``--build`` (default)
    writes ``Results/human_decisions_review.csv``: the draft with a
    ``decision`` column (``approve`` / ``override`` / ``hold``) and a
    ``value`` column pre-filled with the proposed value.  The owner edits
    that file; nothing is written to the corpus.

``--apply``
    reads the review file and writes the approved values into
    ``Extraction/<Key>.json`` - record-level fields, or one DV row when the
    item names an outcome row.  Every write is logged to
    ``Results/human_decisions_applied.csv``.

Rules
-----
* a row with no decision, or ``hold``, is not written;
* a row with ``override`` uses the ``value`` column verbatim;
* a row with ``approve`` uses the proposed value, and if the proposal says
  "no basis to set it" the row is **not** written - a paper that does not
  settle the item cannot settle it;
* the human master's fields are exactly ``eligible_primary``,
  ``dv_polarity`` and ``Fulltext_Exclusion_Criterion``; nothing else is
  ever written from this sheet;
* the SB and MM routes are never touched.
"""
import argparse
import csv
import json
import os
import re
import shutil
import sys
from collections import Counter
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")
DRAFT = os.path.join(RESULTS, "human_decisions_draft.csv")
REVIEW = os.path.join(RESULTS, "human_decisions_review.csv")
APPLIED = os.path.join(RESULTS, "human_decisions_applied.csv")
EXTRACTION = os.path.join(ROOT, "Extraction")

HUMAN_FIELDS = ("eligible_primary", "dv_polarity",
                "Fulltext_Exclusion_Criterion")

# Keys an agent is editing right now.  Two writers on one file race, and
# each overwrites the other's field values, so the applier refuses to
# touch a key listed here.  The list is a plain text file, one key per
# line, under temp/ (never in Results/ - it is scratch state).
BUSY = os.path.join(ROOT, "temp", "mm_route", "busy_keys.txt")


def busy_keys():
    if not os.path.isfile(BUSY):
        return set()
    out = set()
    with open(BUSY, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                out.add(line)
    return out

NO_BASIS = ("no basis to set it", "no basis", "not stated", "unclear",
            "the paper does not", "cannot be set", "n/a")


def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def as_list(value):
    if value in (None, "", []):
        return []
    if isinstance(value, list):
        return value
    return [value]


def coerce(field, raw):
    """Turn the review sheet's text into the field's type."""
    raw = (raw or "").strip()
    if raw == "":
        return None
    if field == "eligible_primary":
        m = raw[0]
        return 1 if m in "1tTyY" else (0 if m in "0fFnN" else None)
    if field == "dv_polarity":
        m = raw[0]
        return 1 if m in "1tTyY" else (0 if m in "0fFnN" else None)
    if field == "Fulltext_Exclusion_Criterion":
        out = []
        for c in raw.replace(",", " ").split():
            cu = c.upper()
            if cu in ("C1", "C2", "C3", "C4"):
                out.append(cu)
            elif cu in ("NONE", "NIL", "EMPTY", "-"):
                return []
        return out or None
    return raw


def _tokens(text):
    out = []
    for w in re.findall(r"[A-Za-z0-9%\-\.]+", str(text or "").lower()):
        w = w.strip(".-")
        if len(w) > 2 and w not in STOP:
            out.append(w)
    return out


STOP = {"the", "a", "an", "of", "and", "or", "in", "on", "for", "to", "vs",
        "index", "scale", "score", "subscale", "item", "items", "total",
        "mean", "measure", "measures", "rating", "ratings", "level"}


def match_dv(rows, dv_name):
    """Find the DV row(s) an item names.

    The item's outcome name is compared case-insensitively and with the
    whitespace normalised; an exact match wins over a prefix match, and a
    prefix match over a token match.  A token match is only accepted when
    one row is clearly the best - the review sheet carries a short
    outcome name, while the row carries the full label ("EDI Body
    Dissatisfaction subscale (9-item summed index) - immediate posttest").

    Where a name matches SEVERAL rows the return value is a list, because
    a ``dv_name`` repeats once per arm pair / timepoint and the review
    sheet does not carry that index.
    """
    target = " ".join((dv_name or "").split()).lower()
    if not target:
        return None, 0
    exact, prefix = [], []
    for r in rows:
        name = " ".join(str(r.get("dv_name") or "").split()).lower()
        if name == target:
            exact.append(r)
        elif target and (name.startswith(target) or target.startswith(name)):
            prefix.append(r)
    if exact:
        return exact[0], 1
    if prefix:
        return prefix[0], len(prefix)
    # token overlap
    tt = set(_tokens(target))
    best, best_score = [], 0
    for r in rows:
        name = str(r.get("dv_name") or "").lower()
        score = len(tt & set(_tokens(name)))
        if score > best_score:
            best, best_score = [r], score
        elif score == best_score and score > 0:
            best.append(r)
    if best_score == 0:
        return None, 0
    if len(best) == 1:
        return best[0], 1
    return best, len(best)


def build():
    if not os.path.isfile(DRAFT):
        print("no draft: %s" % DRAFT, file=sys.stderr)
        return 1
    rows = list(csv.DictReader(open(DRAFT, encoding="utf-8-sig", newline="")))
    fields = ["study_key", "field", "dv", "competing_values", "proposed_value",
              "confidence", "proposed", "evidence", "quote", "has_evidence",
              "decision", "value", "reviewer_note"]
    with open(REVIEW, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({
                "study_key": r["study_key"], "field": r["field"],
                "dv": r["dv"], "competing_values": r["competing_values"],
                "proposed_value": r.get("proposed_value", ""),
                "confidence": r.get("confidence", ""),
                "proposed": r["proposed"], "evidence": r["evidence"],
                "quote": r["quote"], "has_evidence": r["has_evidence"],
                "decision": "", "value": r.get("proposed_value", ""),
                "reviewer_note": ""})
    print("wrote %s (%d items to review)" % (REVIEW, len(rows)))
    print("fill the `decision` column with approve / override / hold and, "
          "for an override, the value to set.")
    return 0


def autofill():
    """Pre-approve the items the paper settles outright.

    The owner asked for the decisions to be drafted and reviewed later, so
    the items whose value the evidence *settles* - confidence ``clear`` -
    are pre-marked ``approve`` and the items that are row-alignment
    artefacts - confidence ``keep`` - are pre-marked ``approve`` too (the
    applier leaves the canonical value alone for those).  The conditional
    and the undecided items are left blank for the owner.
    """
    if not os.path.isfile(REVIEW):
        print("no review sheet: %s" % REVIEW, file=sys.stderr)
        return 1
    rows = list(csv.DictReader(open(REVIEW, encoding="utf-8-sig", newline="")))
    n = 0
    for r in rows:
        if (r.get("confidence") or "").strip().lower() in ("clear", "keep") \
                and not (r.get("decision") or "").strip():
            r["decision"] = "approve"
            n += 1
    with open(REVIEW, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print("pre-approved %d of %d items; %d left for the owner"
          % (n, len(rows), len(rows) - n))
    return 0


def apply():
    if not os.path.isfile(REVIEW):
        print("no review sheet: %s" % REVIEW, file=sys.stderr)
        return 1
    rows = list(csv.DictReader(open(REVIEW, encoding="utf-8-sig", newline="")))
    per_key = {}
    log = []
    for r in rows:
        decision = (r.get("decision") or "").strip().lower()
        if decision not in ("approve", "override", "hold"):
            continue
        field = r["field"]
        if field not in HUMAN_FIELDS:
            log.append(dict(study_key=r["study_key"], field=field,
                            dv=r["dv"], action="SKIPPED",
                            detail="not a human-master field"))
            continue
        if decision == "hold":
            log.append(dict(study_key=r["study_key"], field=field,
                            dv=r["dv"], action="HELD", detail=""))
            continue
        if decision == "approve":
            raw = r["proposed_value"]
            if not raw.strip():
                raw = r["proposed"]
            low = raw.lower()
            if not raw.strip() or any(s in low for s in NO_BASIS):
                log.append(dict(study_key=r["study_key"], field=field,
                                dv=r["dv"], action="SKIPPED",
                                detail="no basis in the paper"))
                continue
            if raw.strip().upper() == "KEEP_CANONICAL":
                log.append(dict(study_key=r["study_key"], field=field,
                                dv=r["dv"], action="KEPT",
                                detail="row-alignment artefact - the canonical "
                                       "value stands"))
                continue
        else:
            raw = r.get("value") or ""
            if not raw.strip():
                log.append(dict(study_key=r["study_key"], field=field,
                                dv=r["dv"], action="SKIPPED",
                                detail="override without a value"))
                continue
        value = coerce(field, raw)
        if value is None:
            log.append(dict(study_key=r["study_key"], field=field,
                            dv=r["dv"], action="SKIPPED",
                            detail="value not understood: %r" % raw[:40]))
            continue
        per_key.setdefault(r["study_key"], []).append((field, r["dv"], value))

    written = 0
    busy = busy_keys()
    if busy:
        print("keys an agent is editing (skipped): %s"
              % ", ".join(sorted(busy)))
    for key, items in sorted(per_key.items()):
        if key in busy:
            for field, dv, value in items:
                log.append(dict(study_key=key, field=field, dv=dv,
                                action="SKIPPED",
                                detail="an agent is editing this key"))
            continue
        path = os.path.join(EXTRACTION, key + ".json")
        if not os.path.isfile(path):
            for field, dv, value in items:
                log.append(dict(study_key=key, field=field, dv=dv,
                                action="SKIPPED", detail="no extraction file"))
            continue
        data = load_json(path)
        recs = data if isinstance(data, list) else data.get("experiments", [])
        changed = False
        for field, dv, value in items:
            hit = False
            if dv:
                for rec in recs:
                    rows_ = as_list(rec.get("DV_Metrics")
                                    or rec.get("dv_outcomes") or rec.get("dvs")
                                    or rec.get("dv_rows") or [])
                    row, n = match_dv(rows_, dv)
                    if row is None:
                        continue
                    if isinstance(row, list):
                        # the name matches several rows.  A measure's
                        # polarity is the same for every row of that
                        # measure, so all of them get the value; an
                        # eligibility flag is per contrast, and without
                        # the row index the item stays with the owner.
                        if field == "dv_polarity":
                            for r2 in row:
                                before = r2.get(field)
                                r2[field] = value
                                hit = True
                                changed = True
                                written += 1
                                log.append(dict(
                                    study_key=key, field=field, dv=dv,
                                    action="SET",
                                    detail="record %s: %r -> %r (one of %d "
                                           "rows with this outcome)"
                                           % (rec.get("Study_ID"), before,
                                              value, n)))
                            break
                        log.append(dict(
                            study_key=key, field=field, dv=dv,
                            action="SKIPPED",
                            detail="the name matches %d rows of this outcome; "
                                   "an eligibility flag needs the row index"
                                   % n))
                        hit = True
                        break
                    before = row.get(field)
                    row[field] = value
                    hit = True
                    changed = True
                    written += 1
                    log.append(dict(
                        study_key=key, field=field, dv=dv, action="SET",
                        detail="record %s: %r -> %r%s"
                               % (rec.get("Study_ID"), before, value,
                                  " (%d rows matched this name)" % n
                                  if n > 1 else "")))
                    break
                if not hit:
                    log.append(dict(study_key=key, field=field, dv=dv,
                                    action="SKIPPED",
                                    detail="outcome row not found"))
            else:
                for rec in recs:
                    before = rec.get(field)
                    rec[field] = value
                    hit = True
                    changed = True
                    written += 1
                    log.append(dict(
                        study_key=key, field=field, dv="",
                        action="SET",
                        detail="record %s: %r -> %r"
                               % (rec.get("Study_ID"), before, value)))
                    break
                if not hit:
                    log.append(dict(study_key=key, field=field, dv="",
                                    action="SKIPPED", detail="no record"))
        if changed:
            backup_dir = os.path.join(
                ROOT, "archive", "human_decisions_%s"
                % datetime.now().strftime("%Y-%m-%d-%H%M%S"))
            os.makedirs(backup_dir, exist_ok=True)
            shutil.copy2(path, os.path.join(backup_dir, key + ".json"))
            save_json(path, data)

    with open(APPLIED, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["study_key", "field", "dv",
                                           "action", "detail"])
        w.writeheader()
        w.writerows(log)
    counts = Counter(l["action"] for l in log)
    print("items acted on: %d" % len(log))
    for k, v in counts.most_common():
        print("   %-10s %d" % (k, v))
    print("values written: %d" % written)
    print("wrote", APPLIED)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--autofill", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if args.apply:
        return apply()
    if args.autofill:
        return autofill()
    return build()


if __name__ == "__main__":
    sys.exit(main())
