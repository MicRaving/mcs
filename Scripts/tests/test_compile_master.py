"""Tests for the derived master: compile, migration, comparison, union rule.

The compiler is the phase-5 contract (plan §11): ``master_dataset.xlsx`` is
rebuilt from ``Project/master_base.xlsx`` + every ``Screening/<Tag>.json`` +
every ``Fulltext/<Tag>.json`` with the app's own merge core.  These tests build a
small project on disk and check the cells, the determinism, the migration
round-trip and that the full-text union rule stayed where it was.
"""

from __future__ import annotations

import json
import os

import pytest

import compare_master
import compile_master
import fulltext_lib
import migrate_fulltext_decisions
import prisma_report
import screening_lib as SL

HEADER = [
    "an", "title", "doi",
    "C1_verdict", "C1_certainty", "C1_rationale",
    "C2_verdict", "C2_certainty", "C2_rationale",
    "C3_verdict", "C3_certainty", "C3_rationale",
    "C4_verdict", "C4_certainty", "C4_rationale",
    "Fulltext_Verdict", "Fulltext_Exclusion_Criterion", "Fulltext_Reason",
    "Effective_Verdict", "Curation_Note",
]


# ---------------------------------------------------------------- helpers
def _write_xlsx(path, header, rows, title="Search"):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = title
    ws.append(list(header))
    for row in rows:
        ws.append(list(row))
    wb.save(str(path))
    return str(path)


def _read_xlsx(path):
    import openpyxl

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=False)
    try:
        ws = wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        head = [str(h) if h is not None else "" for h in next(it)]
        rows = [dict(zip(head, row)) for row in it]
    finally:
        wb.close()
    return head, rows


def _row(**values):
    return [values.get(name) for name in HEADER]


def _decision(saved_at, **criteria):
    return {
        "id": "", "coder": "", "decision": "", "saved_at": saved_at,
        "criteria": {key: {"verdict": verdict, "certainty": "High", "rationale": ""}
                     for key, verdict in criteria.items()},
    }


@pytest.fixture
def project(tmp_path):
    """A small project: base records, two abstract tags, one full-text tag."""
    root = tmp_path / "project"
    for folder in ("Screening", "Fulltext", "Project", "temp", "Extraction"):
        (root / folder).mkdir(parents=True)
    base = _write_xlsx(
        root / "Project" / "master_base.xlsx", HEADER,
        [
            _row(an="A1", title="Alpha", doi="10.1/alpha", Curation_Note="kept"),
            _row(an="A2", title="Beta", doi="10.1/beta"),
            _row(an=None, title="Gamma", doi="10.1/gamma"),
        ],
        title="Sheet1",
    )
    (root / "Screening" / "OG.json").write_text(json.dumps({
        "A1": _decision("2026-09-21T00:00:00", C1="No", C2="No"),
        "A2": _decision("2026-09-21T00:00:00", C1="Yes", C2="Yes"),
        "row-4": _decision("2026-09-21T00:00:00", C1="Unsure", C2="No"),
    }, indent=1), encoding="utf-8")
    (root / "Screening" / "MF.json").write_text(json.dumps({
        "A2": _decision("2026-09-22T00:00:00", C1="No", C2="Yes"),
    }, indent=1), encoding="utf-8")
    (root / "Fulltext" / "MF.json").write_text(json.dumps({
        "A1": {"exclude": True, "criterion": "C1",
               "criterion_label": "No misinformation intervention (C1)",
               "note": "not an intervention", "saved_at": "2026-09-23T00:00:00",
               "effective": "Exclude"},
        "A2": {"exclude": False, "criterion": "", "criterion_label": "",
               "note": "eligible", "saved_at": "2026-09-23T00:00:00",
               "effective": "Include"},
    }, indent=1), encoding="utf-8")
    return {"root": str(root), "base": base}


# ---------------------------------------------------------------- compile
def test_compile_writes_the_decisions_into_the_master(project):
    summary = compile_master.compile_master(project["root"],
                                            os.path.join(project["root"], "out.xlsx"))
    head, rows = _read_xlsx(os.path.join(project["root"], "out.xlsx"))
    assert summary["rows"] == 3
    assert summary["fulltext"]["rows_excluded"] == 1
    assert summary["fulltext"]["rows_included"] == 1
    assert summary["screening"]["rows_matched"] == 4      # 3 rows; A2 by OG and MF
    # the merge's provenance columns are appended (the base does not have them)
    assert head[-2:] == ["Screened_By", "Screened_At"]
    by_an = {row["an"]: row for row in rows}

    assert by_an["A1"]["C1_verdict"] == "No"
    assert by_an["A1"]["Screened_By"] == "OG"
    assert by_an["A1"]["Fulltext_Verdict"] == "Exclude"
    assert by_an["A1"]["Fulltext_Exclusion_Criterion"] == "No misinformation intervention (C1)"
    assert by_an["A1"]["Fulltext_Reason"] == "not an intervention"
    assert by_an["A1"]["Effective_Verdict"] == "Exclude"
    assert by_an["A1"]["Curation_Note"] == "kept"           # base data untouched

    # MF saved later, so its verdicts win (and record the provenance)
    assert by_an["A2"]["C1_verdict"] == "No"
    assert by_an["A2"]["C2_verdict"] == "Yes"
    assert by_an["A2"]["Screened_By"] == "MF"
    assert by_an["A2"]["Fulltext_Verdict"] == "Include"
    assert by_an["A2"]["Effective_Verdict"] == "Include"

    # a record nobody decided at full text stays at the abstract stage
    third = rows[2]                       # the row without an accession number
    assert third["an"] in (None, "")
    assert third["C1_verdict"] == "Unsure"
    assert third["Screened_By"] == "OG"
    assert third["Fulltext_Verdict"] in (None, "")
    assert third["Effective_Verdict"] == "Abstract excluded"


def test_compile_is_deterministic(project):
    first = os.path.join(project["root"], "one.xlsx")
    second = os.path.join(project["root"], "two.xlsx")
    compile_master.compile_master(project["root"], first)
    compile_master.compile_master(project["root"], second)
    assert compare_master.content_signature(first) == compare_master.content_signature(second)
    diff = compare_master.compare_master(first, second)
    assert diff["counts"]["value_differences"] == 0
    assert diff["counts"]["columns_added"] == []


def test_fulltext_decision_by_study_key_resolves_the_row(project):
    """The app decides per study key; the compiler finds the record by DOI."""
    root = project["root"]
    with open(os.path.join(root, "Extraction", "Alpha (2020).json"), "w",
              encoding="utf-8") as fh:
        json.dump([{"Study_ID": "Alpha2020", "Title": "Alpha", "DOI": "10.1/alpha"}], fh)
    with open(os.path.join(root, "Fulltext", "SK.json"), "w", encoding="utf-8") as fh:
        json.dump({"Alpha (2020)": {
            "exclude": True, "criterion": "C2",
            "criterion_label": "No control/comparison group (C2)",
            "note": "by study key", "saved_at": "2026-09-24T00:00:00",
        }}, fh)
    out = os.path.join(root, "bykey.xlsx")
    summary = compile_master.compile_master(root, out)
    _head, rows = _read_xlsx(out)
    alpha = rows[0]
    assert alpha["an"] == "A1"
    assert alpha["Fulltext_Verdict"] == "Exclude"
    assert alpha["Fulltext_Exclusion_Criterion"] == "No control/comparison group (C2)"
    assert alpha["Fulltext_Reason"] == "by study key"
    assert summary["fulltext"]["unmatched"] == []


def test_cli_check_reports_no_difference_against_itself(project):
    out = os.path.join(project["root"], "compiled.xlsx")
    compile_master.compile_master(project["root"], out)
    assert compile_master.main(["--root", project["root"], "--out", out, "--check",
                                "--against", out]) == 0


def test_compile_refuses_without_a_base(tmp_path):
    root = tmp_path / "empty"
    (root / "temp").mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        compile_master.compile_master(str(root), str(root / "out.xlsx"))


# ---------------------------------------------------------------- migration
def test_migration_roundtrip_reproduces_the_workbook(project):
    root = project["root"]
    # 1. the workbook as the app would have it today (abstract decisions applied)
    today = os.path.join(root, "today.xlsx")
    compile_master.compile_master(root, today)

    # 2. a curator adds full-text verdicts to that workbook, including a curated
    #    stage on a record that also carries a criterion (the 256-row pattern)
    import openpyxl

    wb = openpyxl.load_workbook(today)
    ws = wb.worksheets[0]
    cols = {str(c.value): c.column for c in ws[1]}
    ws.cell(row=2, column=cols["Fulltext_Verdict"], value="Exclude")
    ws.cell(row=2, column=cols["Fulltext_Exclusion_Criterion"],
            value="No misinformation intervention (C1)")
    ws.cell(row=2, column=cols["Fulltext_Reason"], value="hand-written reason")
    ws.cell(row=2, column=cols["Effective_Verdict"], value="Abstract excluded")
    ws.cell(row=3, column=cols["Fulltext_Verdict"], value="Include")
    ws.cell(row=3, column=cols["Fulltext_Reason"], value="Included at full text: eligible")
    ws.cell(row=3, column=cols["Effective_Verdict"], value="Include")
    wb.save(today)
    wb.close()

    # 3. migrate those decisions into Fulltext/RT.json (dry run first)
    assert migrate_fulltext_decisions.main(["--root", root, "--master", today,
                                            "--tag", "RT"]) == 0
    assert not os.path.exists(os.path.join(root, "Fulltext", "RT.json"))
    assert migrate_fulltext_decisions.main(["--root", root, "--master", today,
                                            "--tag", "RT", "--apply"]) == 0
    migrated = json.load(open(os.path.join(root, "Fulltext", "RT.json"), encoding="utf-8"))
    assert set(migrated) == {"A1", "A2"}
    assert migrated["A1"]["exclude"] is True
    assert migrated["A1"]["criterion"] == "C1"
    assert migrated["A1"]["criterion_label"] == "No misinformation intervention (C1)"
    assert migrated["A1"]["note"] == "hand-written reason"
    assert migrated["A1"]["effective"] == "Abstract excluded"
    assert migrated["A2"]["exclude"] is False

    # 4. compile from the base + decisions: same decisions, so the same workbook
    again = os.path.join(root, "again.xlsx")
    compile_master.compile_master(root, again)
    head, rows = _read_xlsx(again)
    by_an = {row["an"]: row for row in rows}
    assert by_an["A1"]["Fulltext_Verdict"] == "Exclude"
    assert by_an["A1"]["Effective_Verdict"] == "Abstract excluded"   # curated stage kept
    assert by_an["A2"]["Fulltext_Reason"] == "Included at full text: eligible"
    diff = compare_master.compare_master(today, again)
    assert diff["counts"]["value_differences"] == 0
    assert diff["counts"]["columns_added"] == []


def test_migration_never_overwrites_a_decision_file(project):
    root = project["root"]
    today = os.path.join(root, "today.xlsx")
    compile_master.compile_master(root, today)
    target = os.path.join(root, "Fulltext", "MF.json")     # created by the fixture
    assert os.path.getsize(target) > 0
    assert migrate_fulltext_decisions.main(["--root", root, "--master", today,
                                            "--tag", "MF", "--apply"]) == 1
    assert json.load(open(target, encoding="utf-8"))       # untouched


def test_migration_never_overwrites_a_legacy_decision_file(project):
    root = project["root"]
    today = os.path.join(root, "today.xlsx")
    compile_master.compile_master(root, today)
    legacy = os.path.join(root, "Screening", "fulltext - MF.json")
    with open(legacy, "w", encoding="utf-8") as fh:
        fh.write('{"Key (2021)": {"exclude": true}}\n')
    assert migrate_fulltext_decisions.main(["--root", root, "--master", today,
                                            "--tag", "MF", "--apply"]) == 1


# ---------------------------------------------------------------- merge core
def test_blank_decision_value_keeps_the_base_cell():
    header = ["an", "C1_verdict", "C1_certainty", "C1_rationale", "Intervention_verdict"]
    plan = SL.screening_merge_plan(header, ["C1"], {"Intervention"})
    vals = ["A1", "Yes", "High", "kept", "generic"]
    vals += [None] * (len(plan["header"]) - len(vals))       # the plan adds columns
    decision = {"saved_at": "2026-09-21T00:00:00",
                "criteria": {"C1": {"verdict": "", "certainty": "Low", "rationale": ""}}}
    assert SL.apply_screening_decision(vals, decision, "MF", plan) is True
    assert vals[1] == "Yes"          # blank never clears a base value
    assert vals[2] == "Low"
    assert vals[3] == "kept"
    assert vals[4] == "generic"
    assert vals[plan["by"]] == "MF"


# ---------------------------------------------------------------- union rule
def test_fulltext_union_rule_is_unchanged(tmp_path, monkeypatch):
    """The PRISMA 'coded but excluded at full text' box stays a union.

    The compiler fills the master from the decision files; the coding's own
    ``Fulltext_Exclusion_Criterion`` field remains the other source, exactly as
    ``homogenize_fulltext_exclusions.py`` defines it.
    """
    root = tmp_path / "corpus"
    extraction = root / "Extraction"
    screening = root / "Screening"
    extraction.mkdir(parents=True)
    screening.mkdir()
    (extraction / "Marked et al. (2020).json").write_text(json.dumps([{
        "Study_ID": "Marked2020", "Title": "A marked study", "DOI": "10.1/marked",
        "Fulltext_Exclusion_Criterion": ["C1"],
    }]), encoding="utf-8")
    (extraction / "Clean et al. (2021).json").write_text(json.dumps([{
        "Study_ID": "Clean2021", "Title": "A clean study", "DOI": "10.1/clean",
        "Fulltext_Exclusion_Criterion": "",
    }]), encoding="utf-8")
    agreed = screening / "fulltext_agreed.json"
    agreed.write_text(json.dumps({"Marked et al. (2020)": {"criterion": "C1"}}),
                      encoding="utf-8")
    monkeypatch.setattr(prisma_report, "EXTRACTION", str(extraction))
    monkeypatch.setattr(prisma_report, "AGREED_FULLTEXT", str(agreed))
    result = prisma_report.coded_fulltext_excluded()
    assert result["count"] == 1
    assert "Marked et al. (2020)" in result["keys"]
    assert result["from_extraction_field"] == 1
    assert result["from_agreed_file"] == 1
    assert result["by_criterion"]["C1 no intervention"] == 1


# ---------------------------------------------------------------- fulltext files
def test_master_job_recompiles_in_the_background(project, monkeypatch):
    """A saved decision schedules a background compile (the app's path)."""
    import time

    import master_job
    from studio_state import S

    monkeypatch.setattr(S, "ROOT", project["root"], raising=False)
    monkeypatch.setattr(S, "online", False, raising=False)
    monkeypatch.setattr(master_job, "DEBOUNCE_SECS", 0.0)

    assert master_job.enabled() is True
    assert master_job.schedule("test") is True
    target = os.path.join(project["root"], "master_dataset.xlsx")
    for _ in range(300):
        state = master_job.status()
        if state["compiles"] and not state["running"] and not state["scheduled"]:
            break
        time.sleep(0.05)
    state = master_job.status()
    assert state["ok"] is True, state.get("error")
    assert state["compiles"] >= 1
    assert os.path.getsize(target) > 0
    assert state["summary"]["rows"] == 3


def test_fulltext_decisions_live_in_the_owned_fulltext_file(tmp_path, monkeypatch):
    from studio_state import S

    root = tmp_path / "p"
    for folder in ("Screening", "Fulltext", "temp", "Extraction"):
        (root / folder).mkdir(parents=True)
    monkeypatch.setattr(S, "ROOT", str(root), raising=False)
    monkeypatch.setattr(S, "SCREENING", str(root / "Screening"), raising=False)
    monkeypatch.setattr(S, "FULLTEXT", str(root / "Fulltext"), raising=False)
    monkeypatch.setattr(S, "TEMP", str(root / "temp"), raising=False)
    monkeypatch.setattr(S, "EXTRACTION", str(root / "Extraction"), raising=False)

    fulltext_lib.save_decision("MF", "Key (2021)", True, "C1", "not eligible")
    target = root / "Fulltext" / "MF.json"
    assert target.exists()
    assert not (root / "Screening" / "fulltext - MF.json").exists()
    assert fulltext_lib.decision_path("MF") == str(target)
    assert fulltext_lib.load_decisions("MF")["Key (2021)"]["criterion"] == "C1"

    # a legacy file is still read (and the new file wins per key)
    (root / "Screening" / "fulltext - MF.json").write_text(json.dumps({
        "Old (2019)": {"exclude": True, "criterion": "C2", "criterion_label": "",
                       "note": "", "saved_at": "2026-01-01T00:00:00"},
    }), encoding="utf-8")
    decisions = fulltext_lib.load_decisions("MF")
    assert "Old (2019)" in decisions and "Key (2021)" in decisions
