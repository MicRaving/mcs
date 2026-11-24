"""Tests for the decision-free records file (`Project/master_records.csv.gz`).

A blinded coder's mirror does not receive `master_dataset.xlsx` (it carries the
AI's and every coder's verdicts), so the screening tab reads the compiler's
records file instead.  These tests pin the four properties that make that safe:

* the file carries **only** the bibliographic columns the index reads - no
  verdict/certainty/rationale/stage/`Screened_*` column may ever appear;
* the index works from the records file **alone** (list, abstract text,
  decision save);
* the record ids and row numbers are **identical** to the workbook-based index
  (duplicate accessions `X#2`, rows without an accession `row-<n>`);
* `--init-base` writes it too, and a second compile does not rewrite it (no
  mirror churn).
"""

from __future__ import annotations

import csv
import gzip
import json
import os

import pytest

import compile_master
import screening_lib as SL
from studio_state import S, configure, refresh_keys

#: The master's columns: the seven records fields plus decision columns whose
#: values must never reach the records file.
HEADER = [
    "an", "title", "abstract", "authors", "year", "doi", "source",
    "C1_verdict", "C1_certainty", "C1_rationale",
    "C2_verdict", "C2_certainty", "C2_rationale",
    "Fulltext_Verdict", "Fulltext_Exclusion_Criterion", "Fulltext_Reason",
    "Effective_Verdict", "Screened_By", "Screened_At",
]

FORBIDDEN = ("verdict", "certainty", "rationale", "screened", "effective",
             "fulltext", "stage")


def _row(**values):
    return [values.get(name, "") for name in HEADER]


ROWS = [
    _row(an="1001", title="Alpha study", abstract="First abstract text.",
         authors="Doe, J.", year=2020, doi="10.1/a", source="Journal A",
         C1_verdict="Yes", C1_certainty="High", C1_rationale="An intervention",
         C2_verdict="No", Effective_Verdict="Abstract excluded", Screened_By="OG"),
    # a repeated accession: the second row is "1001#2"
    _row(an="1001", title="Beta study", abstract="Second abstract text.",
         authors="Roe, R.", year=2021, doi="10.1/b", source="Journal B",
         C1_verdict="No", C2_verdict="Yes", Effective_Verdict="Exclude",
         Fulltext_Verdict="Exclude", Screened_By="MF"),
    # no accession at all: the id is "row-4"
    _row(an="", title="Gamma study", abstract="Third abstract text.",
         authors="Poe, P.", year=2022, doi="10.1/c", source="Journal C",
         Effective_Verdict="Not retrieved / not assessed"),
]


def _write_master(path, rows):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Search"
    ws.append(list(HEADER))
    for row in rows:
        ws.append(list(row))
    wb.save(str(path))
    return str(path)


def _read_records(path):
    with gzip.open(str(path), "rt", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        return header, [row for row in reader]


def _project(tmp_path, name="proj"):
    root = tmp_path / name
    for folder in ("Papers", "Text", "Extraction", "Codings", "Screening", "temp",
                   "Project"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def project(tmp_path):
    root = _project(tmp_path)
    _write_master(root / "Project" / "master_base.xlsx", ROWS)
    previous = S.ROOT
    SL._close_text_db()
    SL._SCREEN_INDEX.update(stamp=None, rows=None, by_id=None, info=None)
    configure(str(root))
    refresh_keys()
    try:
        yield root
    finally:
        SL._close_text_db()
        SL._SCREEN_INDEX.update(stamp=None, rows=None, by_id=None, info=None)
        configure(previous)
        refresh_keys()


# ---------------------------------------------------------------- the file
def test_compile_writes_only_the_record_columns(project):
    out = os.path.join(str(project), "out.xlsx")
    summary = compile_master.compile_master(str(project), out)
    records = summary["records"]
    assert records["written"] is True and records["rows"] == len(ROWS)
    assert records["missing_columns"] == []
    assert os.path.isfile(records["path"])

    header, rows = _read_records(records["path"])
    assert header == list(compile_master.RECORD_COLUMNS) == [
        "an", "title", "abstract", "authors", "year", "doi", "source"]
    for name in header:
        assert not any(token in name.lower() for token in FORBIDDEN), name
    assert len(rows) == len(ROWS)
    assert rows[0] == ["1001", "Alpha study", "First abstract text.", "Doe, J.",
                       "2020", "10.1/a", "Journal A"]
    assert rows[1][0] == "1001" and rows[2][0] == ""       # no verdict crept in
    # the whole file must not name a verdict anywhere (header *or* cells)
    raw = "\n".join(",".join(row) for row in rows)
    assert "Abstract excluded" not in raw and "Not retrieved" not in raw


def test_a_second_compile_does_not_rewrite_the_records(project):
    out = os.path.join(str(project), "out.xlsx")
    first = compile_master.compile_master(str(project), out)["records"]
    stamp = os.stat(first["path"]).st_mtime_ns
    second = compile_master.compile_master(str(project), out)["records"]
    assert second["written"] is False
    assert second["sha256"] == first["sha256"]
    assert os.stat(second["path"]).st_mtime_ns == stamp


def test_init_base_writes_the_records_file(tmp_path):
    root = _project(tmp_path, "init")
    _write_master(root / "master_dataset.xlsx", ROWS)
    assert compile_master.main(["--root", str(root), "--init-base"]) == 0
    assert os.path.isfile(root / "Project" / "master_base.xlsx")
    header, rows = _read_records(root / "Project" / "master_records.csv.gz")
    assert header == list(compile_master.RECORD_COLUMNS)
    assert len(rows) == len(ROWS)


# ---------------------------------------------------------------- the index
def test_screening_works_from_the_records_file_alone(project):
    compile_master.compile_master(str(project), os.path.join(str(project), "out.xlsx"))
    master = project / "master_dataset.xlsx"
    assert not master.exists()                       # a blinded coder's mirror
    idx = SL.screening_index()
    assert idx["available"] is True, idx["error"]
    assert idx["info"]["source"] == "records"
    assert idx["error"] == ""
    assert [r["id"] for r in idx["rows"]] == ["1001", "1001#2", "row-4"]
    # no AI verdicts from the records file (they are foreign-tag decisions)
    assert all(r["ai"] == {} for r in idx["rows"])

    abstract, ai = SL.record_text(idx["rows"][1])
    assert abstract == "Second abstract text." and ai == {}
    record = SL.abstract_record(idx["rows"][2], [])
    assert record["abstract"] == "Third abstract text."
    assert record["title"] == "Gamma study"

    # the coder's own decision still saves to its owned file
    SL.record_screening_decision("Anna", "1001", {
        "id": "1001", "an": "1001", "coder": "Anna", "decision": "Include",
        "saved_at": "2026-01-01T00:00:00",
        "criteria": {"C1": {"verdict": "Yes", "certainty": "High", "rationale": ""}},
    })
    saved = json.load(open(project / "Screening" / "Anna.json", encoding="utf-8"))
    assert saved["1001"]["decision"] == "Include"
    assert SL.decision_for(SL.load_screening_decisions("Anna"), idx["rows"][0])["decision"] \
        == "Include"


def test_the_records_and_the_workbook_index_agree_on_every_id(project):
    out = os.path.join(str(project), "master_dataset.xlsx")
    compile_master.compile_master(str(project), out)     # writes master + records
    wb_idx = SL.screening_index()
    assert wb_idx["info"]["source"] == "master"
    wb_ids = [r["id"] for r in wb_idx["rows"]]
    wb_rows = [r["row"] for r in wb_idx["rows"]]
    assert wb_ids == ["1001", "1001#2", "row-4"]

    os.remove(os.path.join(str(project), "master_dataset.xlsx"))
    SL._SCREEN_INDEX.update(stamp=None, rows=None, by_id=None, info=None)
    rec_idx = SL.screening_index()
    assert rec_idx["info"]["source"] == "records"
    assert [r["id"] for r in rec_idx["rows"]] == wb_ids
    assert [r["row"] for r in rec_idx["rows"]] == wb_rows
    # the duplicate keeps its row alias, and the row-less record resolves too
    assert rec_idx["by_id"]["row-3"]["id"] == "1001#2"
    assert rec_idx["by_id"]["row-4"]["id"] == "row-4"


def test_the_route_meta_names_the_records_source(project):
    from app import app as flask_app

    compile_master.compile_master(str(project),
                                  os.path.join(str(project), "master_dataset.xlsx"))
    os.remove(os.path.join(str(project), "master_dataset.xlsx"))
    SL._SCREEN_INDEX.update(stamp=None, rows=None, by_id=None, info=None)
    client = flask_app.test_client()
    body = client.get("/api/screening/meta?coder=Anna").get_json()
    assert body["available"] is True
    assert body["source"] == "records"
    assert body["master"] == "master_records.csv.gz"
    assert body["total"] == 3
    listing = client.get("/api/screening/list?coder=Anna&filter=all").get_json()
    assert [r["i"] for r in listing["rows"]] == ["1001", "1001#2", "row-4"]


def test_a_project_with_no_source_says_so(project):
    """The base alone is not a screening source (it carries the curated stages)."""
    assert not os.path.exists(os.path.join(str(project), "master_dataset.xlsx"))
    assert not os.path.exists(
        os.path.join(str(project), "Project", "master_records.csv.gz"))
    idx = SL.screening_index()
    assert idx["available"] is False
    assert "master_records.csv.gz" in idx["error"]
