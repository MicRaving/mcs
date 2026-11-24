"""Tests for the lean screening index (PLAN_online_only §6.3.1, WP6).

The index keeps only what the list/filter/sort need; the abstract and the long
AI rationale/certainty text live in ``temp/screening_text.sqlite`` and are read
for the one record the abstract pane shows.  These tests build a small master
workbook and check the three properties the optimisation must hold:

* the rows no longer carry the long texts, and the on-disk index cache does not
  either (the memory the change was made for);
* ``record_text`` returns the exact texts, including for a duplicate-accession
  alias (``row-<n>``) and after a restart (the cache path);
* the abstract route still returns the historical record shape and key order,
  i.e. the response is unchanged.
"""

from __future__ import annotations

import json
import os

import pytest

import screening_lib as SL
from studio_state import S, configure, refresh_keys

HEADER = [
    "an", "title", "abstract", "authors", "year", "doi", "source",
    "C1_verdict", "C1_certainty", "C1_rationale",
    "C2_verdict", "C2_certainty", "C2_rationale",
]

RECORD_ORDER = ["id", "an", "row", "title", "abstract", "authors", "year",
                "doi", "source", "ai", "fulltext"]


def _write_master(path, records):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Search"
    ws.append(list(HEADER))
    for rec in records:
        ws.append([rec.get(h, "") for h in HEADER])
    wb.save(str(path))
    return str(path)


def _project(tmp_path):
    root = tmp_path / "proj"
    for name in ("Papers", "Text", "Extraction", "Codings", "Screening", "temp"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def project(tmp_path):
    root = _project(tmp_path)
    master = root / "master_dataset.xlsx"
    _write_master(master, [
        {"an": "1001", "title": "Alpha study", "abstract": "First abstract text.",
         "authors": "Doe, J.", "year": 2020, "doi": "10.1/a", "source": "Journal A",
         "C1_verdict": "Yes", "C1_certainty": "High",
         "C1_rationale": "Clearly an intervention",
         "C2_verdict": "No", "C2_certainty": "Low", "C2_rationale": "No control"},
        # a repeated accession number: the second row is "1001#2", reachable as
        # "row-3" by decision files that key it by its Excel row
        {"an": "1001", "title": "Beta study", "abstract": "Second abstract text.",
         "authors": "Roe, R.", "year": 2021, "doi": "10.1/b", "source": "Journal B",
         "C1_verdict": "Yes", "C1_certainty": "Medium",
         "C1_rationale": "Intervention present",
         "C2_verdict": "Yes", "C2_certainty": "High", "C2_rationale": "Control present"},
    ])
    previous = S.ROOT
    configure(str(root))
    refresh_keys()
    SL._close_text_db()
    try:
        yield root
    finally:
        SL._close_text_db()
        configure(previous)
        refresh_keys()


# ---------------------------------------------------------------- lean rows
def test_rows_do_not_carry_the_long_texts(project):
    idx = SL.screening_index()
    assert idx["available"] is True
    rows = idx["rows"]
    assert [r["id"] for r in rows] == ["1001", "1001#2"]
    for rec in rows:
        assert "abstract" not in rec
        assert all(isinstance(v, str) for v in rec["ai"].values())
    assert rows[0]["ai"] == {"C1": "Yes", "C2": "No"}
    # the side file is next to the index cache, named as documented
    assert os.path.isfile(os.path.join(S.TEMP, "screening_text.sqlite"))


def test_index_cache_carries_no_abstract_text(project):
    SL.screening_index()
    with open(os.path.join(S.TEMP, "screening_index.json"), encoding="utf-8") as fh:
        blob = json.load(fh)
    for row in blob["rows"]:
        assert "abstract" not in row
    assert "First abstract text." not in json.dumps(blob["rows"], ensure_ascii=False)


def test_record_text_reads_the_one_record(project):
    rows = SL.screening_index()["rows"]
    abstract, ai = SL.record_text(rows[0])
    assert abstract == "First abstract text."
    assert ai["C1"] == {"verdict": "Yes", "certainty": "High",
                        "rationale": "Clearly an intervention"}
    assert ai["C2"]["rationale"] == "No control"
    # the duplicate-accession alias reads the second record's text
    alias = SL.screening_index()["by_id"]["row-3"]
    assert alias["id"] == "1001#2"
    assert SL.record_text(alias)[0] == "Second abstract text."


def test_record_survives_a_restart_from_the_cache(project):
    SL.screening_index()
    # what a process restart does: drop the in-memory index, keep temp/
    SL._close_text_db()
    SL._SCREEN_INDEX.update(stamp=None, rows=None, by_id=None, info=None)
    idx = SL.screening_index()
    assert [r["id"] for r in idx["rows"]] == ["1001", "1001#2"]
    assert SL.record_text(idx["rows"][1])[0] == "Second abstract text."


def test_a_rewritten_master_rebuilds_both_caches(project):
    SL.screening_index()
    rows = SL.screening_index()["rows"]
    assert SL.record_text(rows[0])[0] == "First abstract text."
    _write_master(project / "master_dataset.xlsx", [
        {"an": "1001", "title": "Alpha study", "abstract": "Changed abstract text.",
         "authors": "Doe, J.", "year": 2020, "doi": "10.1/a", "source": "Journal A",
         "C1_verdict": "No", "C1_certainty": "High", "C1_rationale": "Not eligible",
         "C2_verdict": "Yes", "C2_certainty": "Low", "C2_rationale": ""},
    ])
    idx = SL.screening_index()
    assert [r["id"] for r in idx["rows"]] == ["1001"]
    assert SL.record_text(idx["rows"][0])[0] == "Changed abstract text."
    assert idx["rows"][0]["ai"] == {"C1": "No", "C2": "Yes"}


def test_abstract_record_keeps_the_historical_shape(project):
    rec = SL.screening_index()["rows"][0]
    record = SL.abstract_record(rec, [])
    assert list(record) == RECORD_ORDER
    assert record["abstract"] == "First abstract text."
    assert record["ai"]["C1"]["rationale"] == "Clearly an intervention"
    assert record["fulltext"] == ""


# ---------------------------------------------------------------- the route
def test_abstract_route_response_is_unchanged(project):
    from app import app as flask_app

    idx = SL.screening_index()
    client = flask_app.test_client()
    resp = client.get("/api/screening/abstract/1001")
    assert resp.status_code == 200
    body = resp.get_json()
    assert list(body["record"]) == RECORD_ORDER
    assert body["record"]["id"] == "1001"
    assert body["record"]["abstract"] == "First abstract text."
    assert body["record"]["ai"]["C1"] == {"verdict": "Yes", "certainty": "High",
                                          "rationale": "Clearly an intervention"}
    assert body["human"] is None
    # a duplicate-accession record is reachable by its row alias
    resp = client.get("/api/screening/abstract/row-3")
    assert resp.status_code == 200
    assert resp.get_json()["record"]["id"] == "1001#2"
    assert resp.get_json()["record"]["abstract"] == "Second abstract text."
    # an unknown id is still a 404 with the old error text
    resp = client.get("/api/screening/abstract/nope")
    assert resp.status_code == 404
    assert "no abstract with id" in resp.get_json()["error"]
    # the list route never needs the text
    resp = client.get("/api/screening/list?filter=all")
    assert resp.status_code == 200
    rows = resp.get_json()["rows"]
    assert [r["i"] for r in rows] == ["1001", "1001#2"]
    assert all("abstract" not in r for r in rows)
