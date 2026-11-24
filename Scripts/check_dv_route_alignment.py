"""Align DV rows across the coding routes by the CONTRAST, not the name.

The tiering pairs two routes' DV rows by position inside a ``dv_name``.
That works when the routes name the rows the same way.  It does not when
one route embeds the contrast in the name and the other does not:

    canonical   'Food advertising literacy: differentiating advertising
                 authenticity (Group A vs Group C, posttest)'
    SB          'Advertising authenticity'

The two names never match, so the comparison finds **no** rows to pair,
and a disagreement between the canonical route and that route is
invisible - it can only surface as a disagreement between the two agent
routes, under the agent route's name.  177 keys are in this state.

This script measures what an extra alignment pass would find: it pairs
the rows the name-based pass left unpaired, by the contrast they describe
(arm labels, comparison label, timepoints) and, within a contrast, by how
well the outcome names match.  The pass is strictly **additive** - it
never removes a conflict the name-based pass found, so it cannot hide a
disagreement.

Usage
-----
    python Scripts/check_dv_route_alignment.py            # report only
    python Scripts/check_dv_route_alignment.py --write    # also write
                                                             the extra
                                                             conflicts
"""
import argparse
import collections
import csv
import glob
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")

#: the DV fields a disagreement can be about.
#:
#: The purely *descriptive* fields are deliberately NOT in this set.  The
#: routes word the same row differently - "experimental group a (food
#: advertising literacy)" against "group a (food advertising literacy)" -
#: and that is not a disagreement about the study, it is two routes
#: writing the same thing down.  What is a disagreement is a field that
#: changes what the pipeline does: the eligibility flag, the polarity,
#: the domain, the contrast type, the effect and its variance, and the
#: cell descriptives they are derived from.
POOL_DV_FIELDS = {
    "eligible_primary", "dv_polarity", "dv_domain", "yi", "vi",
    "effect_size_type_reported", "effect_size_value", "effect_favors",
    "contrast_type", "n1i", "n2i", "m1i", "m2i", "sd1i", "sd2i",
    "df_num", "df_den", "t_val", "F_val", "r_val", "d_val", "g_val",
}
NUMERIC = {
    "yi", "vi", "effect_size_value", "n1i", "n2i", "m1i", "m2i",
    "sd1i", "sd2i", "df_num", "df_den", "t_val", "F_val", "r_val",
    "d_val", "g_val",
}

#: The minimum outcome-name overlap for a contrast-aligned pair to be
#: believed.  Below this the two rows are not recognisably the same
#: outcome and the pairing would be a guess.  0.6 keeps the clear cases
#: ("Sharing of true headlines" against "Sharing of true headlines")
#: and drops the look-alikes ("Sharing discernment (50% misinformation
#: condition)" against "Sharing discernment (nudge x headline veracity)"),
#: which are different analyses of the same construct.
MIN_OVERLAP = 0.6


STOP = {"the", "a", "an", "of", "and", "or", "in", "on", "for", "to", "vs",
        "index", "scale", "score", "subscale", "item", "items", "total",
        "mean", "measure", "measures", "rating", "ratings", "level",
        "pretest", "posttest", "follow", "up", "change", "corrected",
        "difference", "differences", "differences"}


def norm(text):
    return " ".join(str(text or "").split()).lower()


def tokens(text):
    out = set()
    for w in re.findall(r"[a-z0-9%\-\.]+", str(text or "").lower()):
        w = w.strip(".-")
        if len(w) > 2 and w not in STOP:
            out.add(w)
    return out


def outcome_of(dv):
    """The outcome part of a ``dv_name``.

    A name may end in a parenthetical that carries the contrast; that
    parenthetical belongs to the contrast, not to the outcome.
    """
    name = str(dv.get("dv_name") or "")
    name = re.sub(r"\s*\([^()]*\)\s*$", "", name).strip()
    return name


ARM_RE = re.compile(
    r"(?:groups?\s+|arm\s+|condition\s+|arms?\s+)(\w+)")


def arm_codes(dv):
    """The arms a row contrasts, as a set of codes.

    The routes spell the same arm differently - "experimental group a
    (food advertising literacy)", "group a (food advertising literacy)",
    "Group A" - but the *code* is the same, so the code is what the
    alignment uses.
    """
    text = " ".join(str(dv.get(f) or "") for f in
                    ("group1_label", "group2_label", "comparison_label"))
    low = text.lower()
    codes = set()
    for m in ARM_RE.finditer(low):
        code = m.group(1).strip("() .,")
        if code and len(code) <= 3:
            codes.add(code)
    # "a, b and c" / "a vs b vs c" written without the word "group"
    m = re.search(r"\b([a-z])\s*,\s*([a-z])\s+and\s+([a-z])\b", low)
    if m:
        codes |= {m.group(1), m.group(2), m.group(3)}
    for m in re.finditer(r"\b([a-z])\s+vs\.?\s+([a-z])\b", low):
        codes |= {m.group(1), m.group(2)}
    return frozenset(codes)


def timepoint_of(dv):
    """A coarse timepoint class for a row."""
    text = " ".join(str(dv.get(f) or "") for f in
                    ("comparison_label", "timepoint_1", "timepoint_2",
                     "posttest_type", "dv_name")).lower()
    if "omnibus" in text or "mancova" in text or "anova" in text:
        return "omnibus"
    if ("pre" in text and "post" in text) or "difference-in-difference" in text \
            or "pre->post" in text or "pre-to-post" in text or "did" in text:
        return "prepost"
    if "follow" in text or re.search(r"\b(week|month|day)s?\b", text):
        return "followup"
    if "post" in text:
        return "post"
    if "pre" in text:
        return "pre"
    return "other"


def signature(dv):
    """The contrast a row describes.

    The routes word the same contrast differently, so the signature is
    built from the *arm codes* and a coarse *timepoint class* rather than
    from the text.  It is deliberately coarse: it only has to bring the
    two routes' rows of the same contrast together, and the master agent
    reads the paper to settle the conflict.
    """
    arms = arm_codes(dv)
    return (timepoint_of(dv), arms)



def canon(value, field):
    if isinstance(value, (list, tuple, set)):
        return "|".join(sorted(str(v).strip().lower() for v in value))
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return round(float(value), 8)
    return norm(value)


def close(a, b):
    try:
        return abs(float(a) - float(b)) <= 1e-6 * max(1.0, abs(float(a)))
    except (TypeError, ValueError):
        return canon(a, None) == canon(b, None)


def rows_of(path):
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return []
    recs = d if isinstance(d, list) else d.get("experiments", [])
    out = []
    for r in recs:
        for x in (r.get("DV_Metrics") or []):
            if isinstance(x, dict) and x.get("dv_name"):
                out.append(x)
    return out


def conflict_fields(a, b):
    """The DV fields the two rows disagree on."""
    out = {}
    for f in POOL_DV_FIELDS:
        va, vb = a.get(f), b.get(f)
        if va in (None, "") and vb in (None, ""):
            continue
        if va in (None, "") or vb in (None, ""):
            continue
        agree = close(va, vb) if f in NUMERIC else canon(va, f) == canon(vb, f)
        if not agree:
            out[f] = (va, vb)
    return out


def align_by_contrast(rows_a, rows_b):
    """Pair the rows the name-based pass left over, by the contrast.

    Returns a list of ``(row_a, row_b)`` pairs.
    """
    used_a, used_b = set(), set()
    ga = collections.defaultdict(list)
    gb = collections.defaultdict(list)
    for i, r in enumerate(rows_a):
        ga[signature(r)].append(i)
    for i, r in enumerate(rows_b):
        gb[signature(r)].append(i)

    pairs = []
    for sig, ia in ga.items():
        ib = gb.get(sig)
        if not ib:
            continue
        # score every candidate pair by outcome overlap
        cand = []
        for i in ia:
            for j in ib:
                ta, tb = tokens(outcome_of(rows_a[i])), tokens(outcome_of(rows_b[j]))
                score = len(ta & tb) / max(1, len(ta | tb))
                cand.append((score, i, j))
        cand.sort(reverse=True)
        for score, i, j in cand:
            if i in used_a or j in used_b:
                continue
            if score < MIN_OVERLAP:
                continue
            used_a.add(i)
            used_b.add(j)
            pairs.append((rows_a[i], rows_b[j]))
        # leftovers within the same contrast, by position
        la = [i for i in ia if i not in used_a]
        lb = [j for j in ib if j not in used_b]
        for i, j in zip(la, lb):
            ta, tb = tokens(outcome_of(rows_a[i])), tokens(outcome_of(rows_b[j]))
            overlap = len(ta & tb) / max(1, len(ta | tb))
            if overlap < MIN_OVERLAP:
                continue
            used_a.add(i)
            used_b.add(j)
            pairs.append((rows_a[i], rows_b[j]))
    return pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    keys = sorted(os.path.basename(p)[:-5]
                  for p in glob.glob(os.path.join(ROOT, "Extraction", "*.json")))
    per_key = {}
    total_extra = 0
    keys_with_extra = 0
    for key in keys:
        routes = {"canonical": rows_of(
            os.path.join(ROOT, "Extraction", key + ".json"))}
        for route, suffix in (("SB", " - SB"), ("MM", " - MM")):
            p = os.path.join(ROOT, "Codings", key + suffix + ".json")
            if os.path.isfile(p):
                routes[route] = rows_of(p)
        routes = {k: v for k, v in routes.items() if v}
        if len(routes) < 2:
            continue
        extra = []
        names = list(routes)
        for i, ra in enumerate(names):
            for rb in names[i + 1:]:
                A, B = routes[ra], routes[rb]
                # rows already paired by name
                byname = collections.defaultdict(list)
                for r in B:
                    byname[str(r.get("dv_name"))].append(r)
                paired_a = set()
                paired_b = set()
                for r in A:
                    lst = byname.get(str(r.get("dv_name")))
                    if lst:
                        paired_a.add(id(r))
                        paired_b.add(id(lst.pop(0)))
                for a, b in align_by_contrast(A, B):
                    if id(a) in paired_a or id(b) in paired_b:
                        continue
                    diff = conflict_fields(a, b)
                    if diff:
                        extra.append((ra, rb, a, b, diff))
        if extra:
            keys_with_extra += 1
            total_extra += len(extra)
            per_key[key] = extra

    print("keys with an extra contrast-aligned conflict: %d of %d"
          % (keys_with_extra, len(keys)))
    print("extra row-level conflicts found: %d" % total_extra)
    byfield = collections.Counter()
    for key, extra in per_key.items():
        for ra, rb, a, b, diff in extra:
            for f in diff:
                byfield[f] += 1
    print("\nby field:")
    for f, n in byfield.most_common():
        print("   %-26s %d" % (f, n))
    print("\nkeys (first 25):")
    for key in sorted(per_key)[:25]:
        print("   %-44s %d" % (key[:44], len(per_key[key])))

    if args.write:
        out = os.path.join(RESULTS, "dv_route_alignment_conflicts.csv")
        with open(out, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=[
                "study_key", "route_a", "route_b", "dv_name_a", "dv_name_b",
                "field", "value_a", "value_b"])
            w.writeheader()
            for key, extra in sorted(per_key.items()):
                for ra, rb, a, b, diff in extra:
                    for f, (va, vb) in diff.items():
                        w.writerow(dict(
                            study_key=key, route_a=ra, route_b=rb,
                            dv_name_a=str(a.get("dv_name"))[:120],
                            dv_name_b=str(b.get("dv_name"))[:120],
                            field=f, value_a=str(va)[:120],
                            value_b=str(vb)[:120]))
        print("\nwrote", out)

    summary = dict(
        keys_with_extra=keys_with_extra,
        extra_conflicts=total_extra,
        min_overlap=MIN_OVERLAP,
        by_field=dict(byfield))
    with open(os.path.join(RESULTS, "dv_route_alignment_summary.json"),
              "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
    print("wrote", os.path.join(RESULTS, "dv_route_alignment_summary.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
