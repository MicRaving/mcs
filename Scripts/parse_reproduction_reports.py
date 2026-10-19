"""Parse every ``Reproduction/*.md`` report into structured records.

Workstream C (2026-09-22).  The audit question: does each report contain the five
required elements?

  (a) original results from the paper
  (b) reproduced results
  (c) results WITHOUT exclusions
  (d) necessary preparations to derive the dataset from raw data
  (e) failures / discrepancies

Because the 342 reports were written against eight different heading templates, the
parser maps headings to element classes by prefix (the equivalence classes listed in the
workstream brief) rather than exact heading strings.

Outputs
-------
``Results/reproduction_report_audit.csv`` -- one row per report.
Console summary of which required element is missing in which file.

Run:  python Scripts/parse_reproduction_reports.py
"""
import os
import re
import sys
import json

import pandas as pd

try:  # console-safe output for non-cp1252 study names
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
REPRO = os.path.join(ROOT, "Reproduction")
RESULTS = os.path.join(ROOT, "Results")
TEMP = os.path.join(ROOT, "temp", "ws_c")
os.makedirs(RESULTS, exist_ok=True)
os.makedirs(TEMP, exist_ok=True)

AUDIT_CSV = os.path.join(RESULTS, "reproduction_report_audit.csv")
AUDIT_JSON = os.path.join(TEMP, "reproduction_report_audit.json")

# ------------------------------------------------------------------ heading classes
# (a)+(b): reported vs reproduced / reproduced results
AB_PREFIXES = (
    "Reproduced vs reported",
    "Comparison vs reported",
    "Concordance vs reported",
    "Reported effects",
    "Reported eligible effects",
    "What was reproduced",
    "Reproduced ",
    "Reproduction attempt",
    "Verification performed",
    "Verification ",
    "Coded effects",
    "What was verified instead",
    "Fallback (reported",
    "(b) Reproduced results",
    "Reproduced results",
    "SDT discrimination derived",
    "What can and cannot be reproduced",
    "Reproduction from the deposited",
)
A_ONLY_PREFIXES = (
    "Reported effects that cannot be recomputed",
    "Reported eligible effects",
    "(a) Original results",
    "Original results",
    "Reported ",
)
# (d): preparations / derivation
D_PREFIXES = (
    "Data used",
    "Data & scripts",
    "Data & files",
    "Data & files (",
    "Code",
    "Author-script run",
    "Author scripts available",
    "Reimplementation",
    "Harmonization",
    "Harmonized artifacts",
    "Acquisition attempt",
    "Access attempts",
    "Repository re-check",
    "Measures / harmonization",
    "Harmonization mapping",
    "Harmonization steps",
    "Data ",
    "(d) Preparations",
    "Preparations to derive",
)
# (e): failures / discrepancies
E_PREFIXES = (
    "Discrepancies",
    "To resolve",
    "Dataset status",
    "Column decode",
    "Non-independence",
    "Non-independent",
    "Discrepancies / blockers",
    "(e) Failures",
    "Failures / discrepancies",
    "Evidence that",
)
# notes / limitations: a distinct class. Included in the (e) coverage element (a report
# with only a Notes section still documents failures/limitations) but its bullets are NOT
# fed into the `failures`/`discrepancies_lines` column.
NOTES_PREFIXES = ("Notes",)
# exclusions
EXCL_PREFIXES = ("Exclusions",)
# (c)
C_PREFIXES = ("Results without exclusions", "(c) Results without exclusions")

STUB_BOILERPLATE = "Participant-level re-analysis (no public dataset, except Cook et al. 2024)"


def _cls(heading, prefixes):
    h = heading.strip()
    return any(h == p or h.startswith(p) for p in prefixes)


def parse_report(path):
    """Return a structured record for one report file."""
    key = os.path.splitext(os.path.basename(path))[0]
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    lines = text.splitlines()

    # split into (heading, body) sections by H1/H2/H3
    sections = []          # list of (level, heading, [body lines])
    cur = None
    for ln in lines:
        m = re.match(r"^(#{1,4})\s+(.*)$", ln)
        if m:
            cur = (len(m.group(1)), m.group(2).strip(), [])
            sections.append(cur)
        elif cur is not None:
            cur[2].append(ln)

    has_a = has_b = has_c = has_d = has_e = has_excl = False
    has_notes = False
    a_headings, b_headings, c_headings, d_headings, e_headings, excl_headings = [], [], [], [], [], []
    notes_headings = []
    discrepancies_lines = []
    notes_lines = []
    statements = []
    for lvl, head, body in sections:
        # strip a leading "1. " / "2.3 " numbering before classification
        head_n = re.sub(r"^\d+(\.\d+)*\.\s*", "", head)
        if _cls(head_n, C_PREFIXES):
            has_c = True
            c_headings.append(head)
        if _cls(head_n, EXCL_PREFIXES):
            has_excl = True
            excl_headings.append(head)
        if _cls(head_n, A_ONLY_PREFIXES):
            has_a = True
            a_headings.append(head)
        elif _cls(head_n, AB_PREFIXES):
            # the brief's "(a)+(b)" equivalence class: a reproduced-vs-reported heading
            # implies both the paper's reported effects and the reproduced results.
            has_a = True
            has_b = True
            a_headings.append(head)
            b_headings.append(head)
        if _cls(head_n, D_PREFIXES):
            has_d = True
            d_headings.append(head)
        if _cls(head_n, E_PREFIXES):
            has_e = True
            e_headings.append(head)
            for bl in body:
                s = bl.strip()
                if s.startswith("- ") and len(s) > 2:
                    discrepancies_lines.append(s[2:].strip())
        if _cls(head_n, NOTES_PREFIXES):
            has_notes = True
            notes_headings.append(head)
            for bl in body:
                s = bl.strip()
                if s.startswith("- ") and len(s) > 2:
                    notes_lines.append(s[2:].strip())
        for bl in body:
            s = bl.strip()
            if s.startswith("- ") and len(s) > 2:
                statements.append(s[2:].strip())

    # A report whose only (e)-class heading is "Notes" still documents failures/limitations
    # (keeps the 5-element coverage green) without polluting the failures column.
    has_e = has_e or has_notes

    m = re.search(r"\*\*reproduced:\*\*\s*([A-Za-z]+)", text)
    status = m.group(1).strip().lower() if m else ""
    if not status:
        if re.search(r"status:\s*not run", text, re.I):
            status = "no"
        elif re.search(r"not reproducible", text, re.I):
            status = "no"
        elif re.search(r"\bpartial\b", text, re.I):
            status = "partial"
        else:
            status = "unknown"

    # fix: only accept the explicit yes/no/partial vocabulary
    if status not in ("yes", "no", "partial"):
        if status not in ("unknown",):
            status = "unknown"

    size = len(text.encode("utf-8"))
    is_stub = size < 1200
    is_not_run = bool(re.search(r"status:\s*not run", text, re.I))
    dangling = []
    for pat in ("sandbox/prepared/", "reported_effects.md", "reported_effects.csv"):
        if pat in text:
            dangling.append(pat)

    return dict(
        key=key,
        report_path=os.path.relpath(path, ROOT).replace("\\", "/"),
        status=status,
        has_a_original=has_a,
        has_b_reproduced=has_b,
        has_c_without_exclusions=has_c,
        has_d_preparations=has_d,
        has_e_failures=has_e,
        has_notes=has_notes,
        has_exclusions=has_excl,
        n_statements=len(statements),
        statements=" | ".join(statements[:40]),
        discrepancies_lines=" | ".join(discrepancies_lines[:40]),
        notes_lines=" | ".join(notes_lines[:40]),
        a_headings="; ".join(sorted(set(a_headings))),
        b_headings="; ".join(sorted(set(b_headings))),
        c_headings="; ".join(sorted(set(c_headings))),
        d_headings="; ".join(sorted(set(d_headings))),
        e_headings="; ".join(sorted(set(e_headings))),
        notes_headings="; ".join(sorted(set(notes_headings))),
        size_bytes=size,
        is_stub=is_stub,
        is_not_run=is_not_run,
        dangling_refs="; ".join(dangling),
        text=text,
    )


def parse_all(include_text=True, directory=None):
    d = directory or REPRO
    recs = []
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".md") or fn.startswith("_BATCH") or fn.endswith(" - SB.md"):
            continue
        recs.append(parse_report(os.path.join(d, fn)))
    if not include_text:
        for r in recs:
            r.pop("text", None)
    return recs


def main():
    recs = parse_all()
    df = pd.DataFrame(recs)
    # keep the full text in a sidecar JSON (the CSV keeps it too, for the brief, but
    # the JSON is easier to consume programmatically)
    df.to_csv(AUDIT_CSV, index=False, encoding="utf-8-sig")
    with open(AUDIT_JSON, "w", encoding="utf-8") as fh:
        json.dump(recs, fh, ensure_ascii=False, indent=1)

    n = len(df)
    print(f"parsed {n} reports -> {os.path.relpath(AUDIT_CSV, ROOT)}")
    for col, label in (("has_a_original", "(a) original results"),
                       ("has_b_reproduced", "(b) reproduced results"),
                       ("has_c_without_exclusions", "(c) results without exclusions"),
                       ("has_d_preparations", "(d) preparations"),
                       ("has_e_failures", "(e) failures/discrepancies")):
        miss = df[~df[col]]
        print(f"  {label}: {int(df[col].sum())}/{n} present; "
              f"{len(miss)} missing")
        for k in miss["key"].tolist():
            print(f"      MISSING {col}: {k}")

    print(f"  stubs (<1.2 KB): {int(df['is_stub'].sum())}")
    for k in df.loc[df["is_stub"], "key"].tolist():
        print(f"      STUB: {k}")
    print(f"  reports with a Notes section: {int(df['has_notes'].sum())} "
          f"(kept out of the failures column)")
    print(f"  'not run' reports: {int(df['is_not_run'].sum())}")
    print(f"  reports with dangling sandbox/reported_effects refs: "
          f"{int((df['dangling_refs'] != '').sum())}")
    for _, r in df[df["dangling_refs"] != ""].iterrows():
        print(f"      DANGLING: {r['key']}: {r['dangling_refs']}")


if __name__ == "__main__":
    main()
