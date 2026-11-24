"""Merge a batch of new database exports into ``master_dataset.xlsx``.

The master workbook is the screening corpus: one row per database record, with
the abstract-screening verdicts in the ``C*_verdict`` columns.  New exports
(EBSCO CSV downloads) are appended here, de-duplicated against the existing
rows, and written out as per-agent screening batches under
``temp/screening/batches/``.  The verdicts come back through
``Scripts/apply_screening_results.py``.

Usage
-----
    python Scripts/merge_screening_batch.py --csv "Screening/a.csv" "Screening/b.csv"
    python Scripts/merge_screening_batch.py --csv ... --batch-size 150 --dry-run

What it does
------------
* reads the master with openpyxl and appends rows (formatting of the existing
  sheet is preserved, nothing is rewritten in place);
* maps the export columns onto the master schema (``authors``/``authors_raw``
  from ``contributors``, ``year`` from ``publicationDate``);
* de-duplicates with the same union-find the PRISMA report uses (normalised
  DOI -> normalised title -> accession number), so a new record that re-reports
  an existing one is marked ``duplicate`` and inherits that group;
* marks every appended row ``Record_Source = new_hit_<date>`` and leaves the
  screening columns empty (``Effective_Verdict`` blank = not yet screened);
* writes one JSON batch per agent plus an index, and a manifest of what was
  added.

The master is backed up to ``archive/master_dataset_pre_merge_<stamp>.xlsx``
before it is written.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import sys
import unicodedata
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER = os.path.join(ROOT, "master_dataset.xlsx")
TEMP = os.path.join(ROOT, "temp", "screening")
ARCHIVE = os.path.join(ROOT, "archive")

#: master column order (the workbook header is authoritative; this is the fallback)
MASTER_COLUMNS = [
    "an", "title", "abstract", "publicationDate", "shortDBName", "longDBName",
    "src_file", "authors", "authors_raw", "doi", "doids", "year", "source",
    "language", "contributors", "Record_Source", "Download_Status",
    "PDF_Filename", "C1_verdict", "C1_certainty", "C1_rationale",
    "C2_verdict", "C2_certainty", "C2_rationale", "C3_verdict", "C3_certainty",
    "C3_rationale", "C4_verdict", "C4_certainty", "C4_rationale",
    "Intervention_verdict", "Intervention_certainty", "Intervention_rationale",
    "Fulltext_Verdict", "Fulltext_Exclusion_Criterion", "Source_Review",
    "S2_Unsure_Reason", "Hit_Source", "new_index", "csv_row", "Duplicate",
    "Duplicate_Group", "Duplicate_Of", "Fulltext_Reason", "Curation_Note",
    "Effective_Verdict", "Harmonization_Status", "Harmonization_Reason",
]

SCREEN_COLUMNS = [
    "C1_verdict", "C1_certainty", "C1_rationale",
    "C2_verdict", "C2_certainty", "C2_rationale",
    "C3_verdict", "C3_certainty", "C3_rationale",
    "C4_verdict", "C4_certainty", "C4_rationale",
    "Intervention_verdict", "Intervention_certainty", "Intervention_rationale",
]


# ---------------------------------------------------------------- normalisation
def norm_doi(value) -> str:
    s = str(value or "").strip().lower()
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s)
    s = re.sub(r"^doi:\s*", "", s)
    s = s.strip().strip(".")
    return "" if s in ("", "nan", "none") else s


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c))


def norm_title(value) -> str:
    s = _fold(value).lower()
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean(value):
    """A CSV cell as a stripped string, or None for an empty/NaN cell."""
    if value is None:
        return None
    if isinstance(value, float) and value != value:      # NaN
        return None
    s = str(value).strip()
    return s or None


# ---------------------------------------------------------------- workbook I/O
def _sheet(wb):
    for name in ("Search", "Sheet1"):
        if name in wb.sheetnames:
            return wb[name]
    return wb.worksheets[0]


def read_master(path=MASTER):
    """(header, rows) from the master workbook, values only."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = _sheet(wb)
        it = ws.iter_rows(values_only=True)
        header = [str(h).strip() if h is not None else "" for h in (next(it, None) or ())]
        rows = [list(r) for r in it]
        return header, rows
    finally:
        wb.close()


def load_export(path):
    """Rows of one EBSCO CSV export, in file order, values as strings."""
    import pandas as pd
    df = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
    df = df.where(df.notna(), None)
    return [dict(r) for r in df.to_dict(orient="records")], list(df.columns)


# ---------------------------------------------------------------- mapping
def map_row(rec, src_name, csv_row, new_index):
    """One export record -> {master column: value}."""
    contributors = clean(rec.get("contributors"))
    pub_date = clean(rec.get("publicationDate"))
    year = None
    if pub_date:
        m = re.match(r"^(\d{4})", pub_date)
        if m:
            year = int(m.group(1))
    out = {
        "an": clean(rec.get("an")),
        "title": clean(rec.get("title")),
        "abstract": clean(rec.get("abstract")),
        "publicationDate": pub_date,
        "shortDBName": clean(rec.get("shortDBName")),
        "longDBName": clean(rec.get("longDBName")),
        "src_file": src_name,
        "authors": contributors,
        "authors_raw": contributors,
        "doi": clean(rec.get("doi")),
        "doids": clean(rec.get("doids")),
        "year": year,
        "source": clean(rec.get("source")),
        "language": clean(rec.get("language")),
        "contributors": contributors,
        "new_index": new_index,
        "csv_row": csv_row,
    }
    return out


# ---------------------------------------------------------------- dedup
class UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _row_keys(an, doi, doids, title):
    keys = []
    nd = norm_doi(doi) or norm_doi(doids)
    if nd:
        keys.append(("doi", nd))
    nt = norm_title(title)
    if nt:
        keys.append(("title", nt))
    if an:
        keys.append(("an", str(an).strip()))
    return keys


def _completeness(rec):
    return sum(1 for k in ("abstract", "doi", "authors", "source")
               if rec.get(k))


def assign_duplicates(header, existing, new_rows):
    """Mark the new rows as duplicate/primary against the existing corpus.

    Returns ``(duplicate_group, duplicate_of)`` lists parallel to ``new_rows``.
    Existing rows are never re-marked; only a group that a new row joins and
    that had no group yet gets one, so the new duplicate has a primary to name.
    """
    H = {c: i for i, c in enumerate(header)}

    def cell(row, name):
        i = H.get(name)
        if i is None or i >= len(row):
            return None
        return row[i]

    n_old = len(existing)
    n_new = len(new_rows)
    uf = UnionFind(n_old + n_new)
    buckets = defaultdict(list)

    for i, row in enumerate(existing):
        for key in _row_keys(cell(row, "an"), cell(row, "doi"),
                             cell(row, "doids"), cell(row, "title")):
            buckets[key].append(i)
    for j, rec in enumerate(new_rows):
        for key in _row_keys(rec.get("an"), rec.get("doi"),
                             rec.get("doids"), rec.get("title")):
            buckets[key].append(n_old + j)

    for idxs in buckets.values():
        for j in idxs[1:]:
            uf.union(idxs[0], j)

    groups = defaultdict(list)
    for i in range(n_old + n_new):
        groups[uf.find(i)].append(i)

    # existing group ids, so a joined group keeps its label
    old_group_of = {}
    old_primary_an = {}
    for i, row in enumerate(existing):
        grp = cell(row, "Duplicate_Group")
        dup = str(cell(row, "Duplicate") or "").strip().lower()
        if grp:
            old_group_of[i] = str(grp).strip()
        if dup == "primary":
            old_primary_an[i] = cell(row, "an")

    next_id = 1
    for grp in old_group_of.values():
        m = re.match(r"^D(\d+)$", grp)
        if m:
            next_id = max(next_id, int(m.group(1)) + 1)

    dup_group = [None] * n_new
    dup_of = [None] * n_new
    promotions = []          # (existing row index, group, primary an)
    for members in groups.values():
        new_members = [i for i in members if i >= n_old]
        if not new_members:
            continue
        old_members = [i for i in members if i < n_old]

        if old_members:
            # join the existing group: reuse its label, name its primary
            grp = next((old_group_of[i] for i in old_members if i in old_group_of), None)
            primary_an = next((old_primary_an[i] for i in old_members
                               if i in old_primary_an), None)
            if primary_an is None:
                # the existing side had no primary: promote its first row, so the
                # group the new duplicate joins has a named primary
                primary_an = cell(existing[old_members[0]], "an")
                if grp is None:
                    grp = f"D{next_id:05d}"
                    next_id += 1
                promotions.append((old_members[0], grp, primary_an))
            for i in new_members:
                dup_group[i - n_old] = grp
                dup_of[i - n_old] = primary_an
        else:
            # a brand-new group: the most complete row is the primary
            if len(new_members) == 1:
                continue          # a singleton is not a duplicate group
            ordered = sorted(new_members,
                             key=lambda i: (-_completeness(new_rows[i - n_old]),
                                            i))
            primary = ordered[0]
            grp = f"D{next_id:05d}"
            next_id += 1
            primary_an = new_rows[primary - n_old].get("an")
            for i in new_members:
                dup_group[i - n_old] = grp
                if i == primary:
                    dup_of[i - n_old] = None
                else:
                    dup_of[i - n_old] = primary_an
    return dup_group, dup_of, promotions


def duplicate_flag(group, of, an):
    if group is None:
        return "unique"
    return "primary" if of is None else "duplicate"


# ---------------------------------------------------------------- batches
def write_batches(records, batch_size, out_dir):
    """Split the to-screen records into agent batches; return the index."""
    os.makedirs(out_dir, exist_ok=True)
    for old in os.listdir(out_dir):
        if old.endswith(".json"):
            os.remove(os.path.join(out_dir, old))
    index = []
    for n, start in enumerate(range(0, len(records), batch_size), start=1):
        chunk = records[start:start + batch_size]
        name = f"batch_{n:03d}.json"
        payload = {
            "batch": name,
            "count": len(chunk),
            "records": chunk,
        }
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        index.append({"batch": name, "count": len(chunk),
                      "first": chunk[0]["an"], "last": chunk[-1]["an"]})
    with open(os.path.join(out_dir, "_index.json"), "w", encoding="utf-8") as fh:
        json.dump({"total": len(records), "batches": index}, fh,
                  ensure_ascii=False, indent=1)
    return index


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", nargs="+", required=True,
                    help="the export CSV(s) to merge")
    ap.add_argument("--master", default=MASTER)
    ap.add_argument("--batch-size", type=int, default=150)
    ap.add_argument("--record-source", default=None,
                    help="Record_Source label (default new_hit_<date>)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    stamp = datetime.datetime.now().strftime("%Y-%m-%d")
    record_source = args.record_source or f"new_hit_{stamp}"

    header, existing = read_master(args.master)
    if not header:
        print("master workbook has no header", file=sys.stderr)
        return 1
    print(f"master: {len(existing)} rows, {len(header)} columns")

    # ---- read + map the exports
    new_rows, seen_an, per_file = [], set(), {}
    for path in args.csv:
        recs, cols = load_export(path)
        src_name = os.path.basename(path)
        added = 0
        for i, rec in enumerate(recs):
            mapped = map_row(rec, src_name, i + 2, i)
            an = mapped.get("an")
            if an and an in seen_an:
                continue
            if an:
                seen_an.add(an)
            new_rows.append(mapped)
            added += 1
        per_file[src_name] = {"rows": len(recs), "kept": added,
                              "columns": len(cols)}
        print(f"  {src_name}: {len(recs)} rows -> {added} kept")

    # ---- de-duplicate against the master and inside the batch
    groups, dups, promotions = assign_duplicates(header, existing, new_rows)
    for rec, grp, of in zip(new_rows, groups, dups):
        rec["Duplicate"] = duplicate_flag(grp, of, rec.get("an"))
        rec["Duplicate_Group"] = grp
        rec["Duplicate_Of"] = of
        # not screened yet: an honest stage, replaced by the apply step
        rec["Effective_Verdict"] = "Unscreened"
    print(f"existing rows promoted to primary: {len(promotions)}")

    n_dup = sum(1 for r in new_rows if r["Duplicate"] == "duplicate")
    n_pri = sum(1 for r in new_rows if r["Duplicate"] == "primary")
    n_uni = sum(1 for r in new_rows if r["Duplicate"] == "unique")
    print(f"new rows: {len(new_rows)} (unique {n_uni}, primary {n_pri}, duplicate {n_dup})")

    # ---- which rows still need an abstract screen?
    # A duplicate of an existing record already has a verdict (same report);
    # a duplicate inside the batch is screened through its primary.
    to_screen = [r for r in new_rows if r["Duplicate"] != "duplicate"]
    print(f"to screen: {len(to_screen)} records")

    os.makedirs(TEMP, exist_ok=True)
    batch_records = []
    for r in to_screen:
        batch_records.append({
            "an": r.get("an"),
            "title": r.get("title"),
            "abstract": r.get("abstract"),
            "authors": r.get("authors"),
            "year": r.get("year"),
            "source": r.get("source"),
            "doi": r.get("doi"),
            "dup_group": r.get("Duplicate_Group"),
        })
    index = write_batches(batch_records, args.batch_size,
                          os.path.join(TEMP, "batches"))
    print(f"wrote {len(index)} batches to temp/screening/batches/")

    manifest = {
        "stamp": stamp,
        "record_source": record_source,
        "files": per_file,
        "new_rows": len(new_rows),
        "unique": n_uni, "primary": n_pri, "duplicate": n_dup,
        "to_screen": len(to_screen),
        "batches": index,
    }

    if args.dry_run:
        print(json.dumps(manifest, indent=1)[:4000])
        return 0

    # ---- append to the workbook
    import openpyxl
    os.makedirs(ARCHIVE, exist_ok=True)
    backup = os.path.join(ARCHIVE, f"master_dataset_pre_merge_{stamp}.xlsx")
    if not os.path.exists(backup):
        shutil.copy2(args.master, backup)
        print(f"backed up master -> {os.path.relpath(backup, ROOT)}")

    wb = openpyxl.load_workbook(args.master)
    ws = _sheet(wb)
    head = [str(c.value).strip() if c.value is not None else ""
            for c in ws[1]]
    H = {name: i for i, name in enumerate(head) if name}
    missing = [c for c in MASTER_COLUMNS if c not in H]
    if missing:
        print(f"! master is missing columns: {missing}", file=sys.stderr)
        return 1

    for rec in new_rows:
        values = [None] * len(head)
        for name, value in rec.items():
            if name in H:
                values[H[name]] = value
        values[H["Record_Source"]] = record_source
        ws.append(values)

    # an existing row that a new duplicate joined but that was still marked
    # "unique" becomes the group's primary, so the group has a named anchor
    for row_idx, grp, primary_an in promotions:
        excel_row = row_idx + 2
        ws.cell(row=excel_row, column=H["Duplicate"] + 1).value = "primary"
        ws.cell(row=excel_row, column=H["Duplicate_Group"] + 1).value = grp
        ws.cell(row=excel_row, column=H["Duplicate_Of"] + 1).value = None

    wb.save(args.master)
    wb.close()
    print(f"appended {len(new_rows)} rows -> {os.path.relpath(args.master, ROOT)}")

    with open(os.path.join(TEMP, "merge_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)
    print(f"manifest -> temp/screening/merge_manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
