"""The screening rules ported from the project folder (owner decision 2026-11-24).

* the four-valued outcome: a failing gating criterion excludes, all passing
  includes, all answered with an "Unsure" and no failure is **Unsure** (goes on
  to full text), anything blank is incomplete;
* the criteria verdicts override a stored ``decision`` (the stored value is
  only the fallback of a record without verdicts);
* a positional (``row-<n>``) decision whose title fingerprint no longer matches
  its row is **stale**: shown, counted and merged as unscreened;
* the save validates verdicts against the criterion's options, normalises the
  certainty, caps the rationale, stores the fingerprint, and records the
  pre-fill provenance - an "ai" pre-fill is never accepted when the source is
  the decision-free records file (a blinded mirror).
"""

from __future__ import annotations

import csv
import gzip
import json
import os

import pytest

import screening_lib as SL
from studio_state import S, configure, refresh_keys

CRITS = [{"key": k, "label": k, "kind": "verdict", "gating": True, "pass_values": ["Yes"],
          "options": ["Yes", "No", "Unsure"]} for k in ("C1", "C2", "C3", "C4")]
HEADER = ["title", "abstract", "authors", "year", "doi", "source",
          "C1_verdict", "C2_verdict", "C3_verdict", "C4_verdict"]


def _vals(*verdicts):
    table = dict(zip(("C1", "C2", "C3", "C4"), verdicts))
    return lambda key: table.get(key, "")


# ---------------------------------------------------------------- pure rules
@pytest.mark.parametrize("verdicts, expected", [
    (("Yes", "Yes", "Yes", "Yes"), "Include"),
    (("Yes", "No", "Yes", "Yes"), "Exclude"),
    (("Yes", "Unsure", "Yes", "Yes"), "Unsure"),
    (("Unsure", "Unsure", "Unsure", "Unsure"), "Unsure"),
    (("Unsure", "No", "", ""), "Exclude"),       # one failure excludes at once
    (("Yes", "Yes", "", "Yes"), ""),             # incomplete
    (("Yes", "Unsure", "", "Yes"), ""),          # incomplete, not Unsure
    (("", "", "", ""), ""),
    (("yes", "YES ", "Yes", "yes"), "Include"),  # case-insensitive pass values
])
def test_screen_outcome(verdicts, expected):
    assert SL.screen_outcome(CRITS, _vals(*verdicts)) == expected


def test_non_gating_and_classify_criteria_never_decide():
    crits = CRITS + [{"key": "Type", "kind": "classify", "gating": True},
                     {"key": "Info", "kind": "verdict", "gating": False,
                      "pass_values": ["Yes"]}]
    table = {"C1": "Yes", "C2": "Yes", "C3": "Yes", "C4": "Yes", "Type": "generic", "Info": "No"}
    assert SL.screen_outcome(crits, table.get) == "Include"


def test_criteria_override_a_stored_decision():
    dec = {"decision": "Include",
           "criteria": {"C1": {"verdict": "Yes"}, "C2": {"verdict": "No"},
                        "C3": {"verdict": "Yes"}, "C4": {"verdict": "Yes"}}}
    assert SL.human_screen_outcome(dec, CRITS) == "Exclude"
    dec["criteria"]["C2"]["verdict"] = "Unsure"
    assert SL.human_screen_outcome(dec, CRITS) == "Unsure"
    assert SL.human_screen_decision(dec, CRITS) == ""          # two-valued form
    # no verdict at all: the stored decision is the fallback (Unsure included)
    assert SL.human_screen_outcome({"decision": "Unsure", "criteria": {}}, CRITS) == "Unsure"
    assert SL.human_screen_outcome({"decision": "bogus"}, CRITS) == ""
    assert SL.human_screen_outcome(None, CRITS) == ""


def test_ai_outcome_has_the_same_rule():
    rec = {"ai": {"C1": "Yes", "C2": "Unsure", "C3": "Yes", "C4": "Yes"}}
    assert SL.ai_screen_outcome(rec, CRITS) == "Unsure"
    assert SL.ai_screen_decision(rec, CRITS) == ""
    rec["ai"]["C2"] = "No"
    assert SL.ai_screen_decision(rec, CRITS) == "Exclude"


def test_staleness_is_positional_and_needs_a_fingerprint():
    fp = SL.title_fingerprint("Alpha study")
    assert fp == SL.title_fingerprint("  ALPHA, study! ")        # spelling-insensitive
    dec = {"fp": fp}
    assert SL.decision_is_stale("row-2", dec, "Beta study") is True
    assert SL.decision_is_stale("row-2", dec, "Alpha study") is False
    assert SL.decision_is_stale("1001", dec, "Beta study") is False   # accession id
    assert SL.decision_is_stale("row-2", {}, "Beta study") is False   # no fingerprint
    # the merge core skips a stale positional decision only when given the title
    decisions = {"row-2": {"fp": fp, "criteria": {"C1": {"verdict": "No"}}}}
    assert SL.screening_decision_for(decisions, "row-2", 2) is not None
    assert SL.screening_decision_for(decisions, "row-2", 2, title="Beta study") is None
    assert SL.screening_decision_for(decisions, "row-2", 2, title="Alpha study") is not None


# ---------------------------------------------------------------- the routes
def _workbook(path, rows):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(HEADER)
    for r in rows:
        ws.append([r.get(h, "") for h in HEADER])
    wb.save(str(path))


ROWS = [
    {"title": "Alpha study", "abstract": "A misinformation intervention with a control group.",
     "C1_verdict": "Yes", "C2_verdict": "Unsure", "C3_verdict": "Yes", "C4_verdict": "Yes"},
    {"title": "Beta study", "abstract": "Something else.",
     "C1_verdict": "No", "C2_verdict": "No", "C3_verdict": "Yes", "C4_verdict": "No"},
]


def _configure(root):
    previous = S.ROOT
    configure(str(root))
    refresh_keys()
    SL._close_text_db()
    return previous


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    for name in ("Papers", "Text", "Extraction", "Codings", "Screening", "temp"):
        (root / name).mkdir(parents=True, exist_ok=True)
    _workbook(root / "master_dataset.xlsx", ROWS)      # no id column: row-2, row-3
    previous = _configure(root)
    try:
        yield root
    finally:
        SL._close_text_db()
        configure(previous)
        refresh_keys()


@pytest.fixture
def client(project):
    from app import app as flask_app

    return flask_app.test_client()


def _save(client, uid, verdicts, **extra):
    payload = {"coder": "AB", "id": uid,
               "criteria": {k: {"verdict": v, "certainty": "high", "rationale": "r"}
                            for k, v in zip(("C1", "C2", "C3", "C4"), verdicts)}}
    payload.update(extra)
    return client.post("/api/screening/decision", json=payload)


def test_unsure_outcome_is_saved_filtered_and_counted(client, project):
    resp = _save(client, "row-2", ("Yes", "Unsure", "Yes", "Yes"),
                 prefilled={"C1": "ai", "C3": "language", "C2": "bogus"})
    body = resp.get_json()
    assert resp.status_code == 200, body
    assert body["outcome"] == "Unsure" and body["decision"] == ""
    stored = json.loads((project / "Screening" / "AB.json").read_text(encoding="utf-8"))["row-2"]
    assert stored["fp"] == SL.title_fingerprint("Alpha study")
    assert stored["criteria"]["C1"]["certainty"] == "High"          # normalised
    assert stored["prefilled"] == {"C1": "ai", "C3": "language"}     # bogus dropped

    rows = client.get("/api/screening/list?coder=AB&filter=unsure").get_json()["rows"]
    assert [r["i"] for r in rows] == ["row-2"]
    assert rows[0]["h"] == "Unsure" and rows[0]["d"] == 1
    # the AI outcome follows the same rule (row-2's workbook verdicts are Unsure)
    assert rows[0]["ai"] == "Unsure"
    counts = client.get("/api/screening/meta?coder=AB").get_json()["counts"]
    assert counts["unsure"] == 1 and counts["stale"] == 0 and counts["complete"] == 1


def test_invalid_verdict_is_refused_and_rationale_capped(client, project):
    resp = _save(client, "row-2", ("Maybe", "Yes", "Yes", "Yes"))
    assert resp.status_code == 400
    assert "not a verdict" in resp.get_json()["error"]
    payload = {"coder": "AB", "id": "row-3",
               "criteria": {"C1": {"verdict": "no", "rationale": "x" * 5000}}}
    resp = client.post("/api/screening/decision", json=payload)
    assert resp.status_code == 200
    stored = json.loads((project / "Screening" / "AB.json").read_text(encoding="utf-8"))["row-3"]
    assert stored["criteria"]["C1"]["verdict"] == "No"                # option spelling
    assert len(stored["criteria"]["C1"]["rationale"]) == SL.RATIONALE_MAX
    assert resp.get_json()["outcome"] == "Exclude"


def test_a_stale_positional_decision_is_shown_as_unscreened(client, project):
    # a decision made on "Beta study" while it sat in row 2 (the rows moved since)
    stale = {"id": "row-2", "fp": SL.title_fingerprint("Beta study"), "coder": "AB",
             "decision": "Exclude", "criteria": {"C1": {"verdict": "No"}}}
    (project / "Screening" / "AB.json").write_text(json.dumps({"row-2": stale}),
                                                   encoding="utf-8")
    rows = client.get("/api/screening/list?coder=AB&filter=all").get_json()["rows"]
    assert {r["i"]: r["d"] for r in rows} == {"row-2": 0, "row-3": 0}
    body = client.get("/api/screening/abstract/row-2?coder=AB").get_json()
    assert body["human"] is None and body["human_stale"]["fp"] == stale["fp"]
    counts = client.get("/api/screening/meta?coder=AB").get_json()["counts"]
    assert counts["stale"] == 1 and counts["complete"] == 0
    # screening it again replaces the stale record (with the right fingerprint)
    assert _save(client, "row-2", ("Yes", "Yes", "Yes", "Yes")).status_code == 200
    body = client.get("/api/screening/abstract/row-2?coder=AB").get_json()
    assert body["human_stale"] is None and body["human"]["fp"] == SL.title_fingerprint("Alpha study")


def test_an_unreadable_decision_file_is_reported(client, project):
    (project / "Screening" / "AB.json").write_text("{not json", encoding="utf-8")
    meta = client.get("/api/screening/meta?coder=AB").get_json()
    assert "cannot be read" in meta["decisions_error"]


def test_no_ai_prefill_provenance_on_the_records_source(tmp_path):
    root = tmp_path / "blind"
    for name in ("Papers", "Text", "Extraction", "Codings", "Screening", "temp", "Project"):
        (root / name).mkdir(parents=True, exist_ok=True)
    with gzip.open(root / "Project" / "master_records.csv.gz", "wt", encoding="utf-8",
                   newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["an", "title", "abstract", "authors", "year", "doi", "source"])
        w.writerow(["E1", "Alpha study", "text", "", 2020, "", ""])
    previous = _configure(root)
    try:
        from app import app as flask_app

        client = flask_app.test_client()
        meta = client.get("/api/screening/meta?coder=AB").get_json()
        assert meta["source"] == "records"
        resp = _save(client, "E1", ("Yes", "Yes", "Yes", "Yes"),
                     prefilled={"C1": "ai", "C3": "language"})
        assert resp.status_code == 200
        stored = json.loads((root / "Screening" / "AB.json").read_text(encoding="utf-8"))["E1"]
        assert stored["prefilled"] == {"C3": "language"}
        assert os.path.isfile(root / "Screening" / "AB.json")
    finally:
        SL._close_text_db()
        configure(previous)
        refresh_keys()
