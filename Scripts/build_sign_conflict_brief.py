"""The rows whose stored effect has the OPPOSITE sign to the paper's own
coefficient.

``Scripts/check_effect_sign_consistency.py`` lists every row whose
``effect_favors`` contradicts the sign of its oriented effect.  Most of
those are a question of the *convention* (whether ``yi`` is stored as
group1 - group2 or the other way round), which the master agents settle
key by key.  A small subset is harder: the row carries the paper's own
coefficient in ``beta_val`` and the stored effect has the opposite sign
to it.  That cannot be a convention question - one of the two is wrong.

This script writes the brief for those rows.
"""
import csv
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")
SRC = os.path.join(RESULTS, "effect_sign_consistency.csv")
OUT = os.path.join(ROOT, "temp", "mm_route", "sign_conflicts.txt")

HEAD = """You are settling a SIGN error in a meta-analysis's coding.

Workspace: {root}

For each row below, the coding stores a pooled effect `yi`, and the row
also stores the paper's OWN regression coefficient in `beta_val`.  The two
have the OPPOSITE sign.  One of them is wrong, and the pooled number
flips depending on which - so the paper decides.

Read `Papers/<Key>.pdf` and `Text/<Key>.txt` and work out, for each row:

  1. The paper's own coefficient for this exact contrast, and its sign.
  2. Which group is group 1 and which is group 2 in this row.
  3. Whether the paper's contrast is written as (group1 - group2) or
     (group2 - group1).  Many papers report the CONTROL minus the
     INTERVENTION, and a row that stores `d = +b/SD` for such a contrast
     has the wrong sign.
  4. What the row's `yi` and `vi` SHOULD be, with the derivation
     written out so it can be checked.  If the row carries `n1i`, `n2i`,
     `m1i`, `m2i`, `sd1i`, `sd2i`, the standardised effect is
     d = (m1 - m2) / pooled_SD, and the variance is
     vi = (n1 + n2) / (n1 * n2) + d^2 / (2 * (n1 + n2)).
  5. Whether the row's `dv_polarity` and `effect_favors` are right.

RULES
  - Edit ONLY `Extraction/<Key>.json`, and only the fields the rows
    below name: `yi`, `vi`, `d_val`, `g_val`, `beta_val`, `beta_se`,
    `effect_favors`, `dv_polarity`, `conversion_method`,
    `conversion_inputs`, `missing_metrics_reason`, `source_location`,
    `source_quote`, `source_span`, `effect_size_type_reported`.
  - Do NOT touch `eligible_primary`, `n_arms`, `control_group_type`,
    `Fully_Within`, `Fulltext_Exclusion_Criterion`, or any other key.
  - Never invent a number.  Every value must be traceable to a page of
    the paper, and the derivation goes into `conversion_method`.
  - If the paper does not report the statistic, leave the row alone and
    say so.
  - Keep the file at indent=2, ensure_ascii=False, with a trailing
    newline, and do not reorder or reformat anything you did not change.
  - Scratch under `temp/mm_route/sign/` only; never delete anything under
    `temp/`.
  - Do not open any other key's `Extraction/` or `Codings/` file.

ROWS
{rows}

VERIFY
  python Scripts/B_validate.py "Extraction/<Key>.json"   -> OK
  python Scripts/validate_polarity.py                    -> still PASS
  python Scripts/check_effect_sign_consistency.py        -> these rows gone

Reply with one line per row: key | dv_name | the paper's coefficient and
its sign | the value you wrote for yi | the page.
"""


def main():
    if not os.path.isfile(SRC):
        print("no sign-consistency product", file=sys.stderr)
        return 1
    rows = [r for r in csv.DictReader(open(SRC, encoding="utf-8-sig", newline=""))
            if r.get("beta_sign_agrees_with_yi") == "NO"]
    if not rows:
        print("no sign conflicts where the paper's coefficient is on the row")
        return 0
    by_key = {}
    for r in rows:
        by_key.setdefault(r["study_key"], []).append(r)
    body = []
    for key in sorted(by_key):
        body.append("KEY: %s" % key)
        body.append("  papers: Papers/%s.pdf , Text/%s.txt" % (key, key))
        for r in by_key[key]:
            body.append(
                "  ROW: record %s, DV_Metrics[%s] - %s"
                % (r["record"], r["dv_index"], r["dv_name"]))
            body.append("       yi=%s  vi=?  beta_val=%s  dv_polarity=%s"
                        % (r["yi"], r["beta_val"], r["dv_polarity"]))
            body.append("       effect_favors=%s  eligible_primary=%s"
                        % (r["effect_favors"], r["eligible_primary"]))
            body.append("       comparison_label: %s" % r["comparison_label"])
            body.append("       group1: %s" % r["group1_label"])
            body.append("       group2: %s" % r["group2_label"])
    out = os.path.join(ROOT, "temp", "mm_route", "sign")
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, "sign_conflicts.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(HEAD.format(root=ROOT, rows="\n".join(body)))
    print("rows with a coefficient/effect sign conflict: %d over %d keys"
          % (len(rows), len(by_key)))
    for k in sorted(by_key):
        print("   %s (%d rows)" % (k, len(by_key[k])))
    print("wrote", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
