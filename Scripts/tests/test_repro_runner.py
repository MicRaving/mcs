"""The reproduction runner ported from the project folder (owner decision 2026-11-24).

* a filtered run (``--only``) rebuilds a Results summary for the matching
  studies only: the runner backs the summary up and merges the rows of every
  other study back in, so a spot check never truncates the campaign summary;
* a summary the runner leaves unreadable is restored from the backup;
* a run can be cancelled: the running step and its children are killed;
* ``dossier`` and ``study`` runs are multi-step plans; the dossier validator is
  a soft step (its failure is a warning, not a failed run);
* the run routes take JSON only (a cross-site form POST cannot start a run).

The runner scripts are fakes written into a temporary project.
"""

from __future__ import annotations

import time

import pytest

from studio_state import S, configure, refresh_keys

FAKE_REPRODUCERS = r'''
import os, sys, time
only = ""
for a in sys.argv[1:]:
    if a.startswith("--only="):
        only = a.split("=", 1)[1]
mode = open(os.path.join("Scripts", "mode.txt")).read().strip() if os.path.exists(os.path.join("Scripts", "mode.txt")) else "ok"
out = os.path.join("Results", "reproduction_summary.xlsx")
if mode == "sleep":
    for i in range(600):
        print("working", i, flush=True)
        time.sleep(0.1)
    sys.exit(0)
if mode == "garbage":
    open(out, "wb").write(b"not a workbook")
    sys.exit(0)
import openpyxl
wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Reproduction"
ws.append(["key", "status"])
dom = wb.create_sheet("Domains")
dom.append(["key", "domain"])
for k in ("Alpha (2020)", "Beta (2021)", "Gamma (2022)"):
    parts = [p.strip().lower() for p in only.split(",") if p.strip()]
    if parts and not any(p in k.lower() for p in parts):
        continue
    ws.append([k, "new"])
    dom.append([k, "new-domain"])
wb.save(out)
print("wrote", out, flush=True)
'''

FAKE_DOSSIER = r'''
import os, sys
only = ""
for a in sys.argv[1:]:
    if a.startswith("--only="):
        only = a.split("=", 1)[1]
rows = [k for k in ("Alpha (2020)", "Beta (2021)", "Gamma (2022)")
        if not only or only.lower() in k.lower()]
with open(os.path.join("Results", "repro_dossier_index.csv"), "w", encoding="utf-8-sig") as fh:
    fh.write("study_key,reproduced_status\n")
    for k in rows:
        fh.write(f'"{k}",rebuilt\n')
print("dossier", rows, flush=True)
'''

FAKE_VALIDATE = "import sys; print('3 problems'); sys.exit(1)\n"


def _write_summary(path):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Reproduction"
    ws.append(["key", "status"])
    dom = wb.create_sheet("Domains")
    dom.append(["key", "domain"])
    for k in ("Alpha (2020)", "Beta (2021)", "Gamma (2022)"):
        ws.append([k, "old"])
        dom.append([k, "old-domain"])
    wb.save(str(path))


def _read_summary(path):
    import openpyxl

    wb = openpyxl.load_workbook(str(path), read_only=True)
    try:
        return {name: [list(r) for r in wb[name].iter_rows(values_only=True)][1:]
                for name in wb.sheetnames}
    finally:
        wb.close()


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    for name in ("Papers", "Text", "Extraction", "Codings", "Screening", "temp", "Results",
                 "Scripts/analysis", "Datasets/Beta (2021)"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "Scripts" / "analysis" / "run_reproducers.py").write_text(FAKE_REPRODUCERS,
                                                                       encoding="utf-8")
    (root / "Scripts" / "build_repro_dossier.py").write_text(FAKE_DOSSIER, encoding="utf-8")
    (root / "Scripts" / "validate_repro_dossier.py").write_text(FAKE_VALIDATE, encoding="utf-8")
    (root / "Datasets" / "Beta (2021)" / "harmonizer.py").write_text("", encoding="utf-8")
    _write_summary(root / "Results" / "reproduction_summary.xlsx")
    (root / "Results" / "repro_dossier_index.csv").write_text(
        "study_key,reproduced_status\nAlpha (2020),old\nBeta (2021),old\nGamma (2022),old\n",
        encoding="utf-8-sig")
    previous = S.ROOT
    configure(str(root))
    refresh_keys()
    try:
        yield root
    finally:
        configure(previous)
        refresh_keys()


@pytest.fixture
def client(project):
    from app import app as flask_app

    return flask_app.test_client()


def _wait(client, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = client.get("/api/repro/run/status").get_json()
        if not s["running"]:
            return s
        time.sleep(0.2)
    raise AssertionError("the run did not finish")


def test_a_filtered_run_merges_the_other_studies_back(client, project):
    resp = client.post("/api/repro/run", json={"kind": "reproducers", "only": "beta"})
    assert resp.status_code == 200, resp.get_json()
    s = _wait(client)
    assert s["ok"] is True, s
    rows = _read_summary(project / "Results" / "reproduction_summary.xlsx")
    assert rows["Reproduction"] == [["Alpha (2020)", "old"], ["Beta (2021)", "new"],
                                    ["Gamma (2022)", "old"]]
    assert rows["Domains"] == [["Alpha (2020)", "old-domain"], ["Beta (2021)", "new-domain"],
                               ["Gamma (2022)", "old-domain"]]
    assert any("merged back 4 row(s)" in n for n in s["notes"]), s["notes"]
    assert s["log_file"] and "reproducers" in s["log_file"]


def test_an_unfiltered_run_keeps_the_runner_output(client, project):
    s = (client.post("/api/repro/run", json={"kind": "reproducers"}), _wait(client))[1]
    assert s["ok"] is True and s["notes"] == []
    rows = _read_summary(project / "Results" / "reproduction_summary.xlsx")
    assert [r[1] for r in rows["Reproduction"]] == ["new", "new", "new"]


def test_an_unreadable_summary_is_restored(client, project):
    (project / "Scripts" / "mode.txt").write_text("garbage", encoding="utf-8")
    client.post("/api/repro/run", json={"kind": "reproducers"})
    s = _wait(client)
    assert any("restored the backup" in n for n in s["notes"]), s["notes"]
    rows = _read_summary(project / "Results" / "reproduction_summary.xlsx")
    assert [r[1] for r in rows["Reproduction"]] == ["old", "old", "old"]


def test_a_run_can_be_cancelled(client, project):
    (project / "Scripts" / "mode.txt").write_text("sleep", encoding="utf-8")
    assert client.post("/api/repro/run", json={"kind": "reproducers"}).status_code == 200
    deadline = time.time() + 30
    while "working" not in client.get("/api/repro/run/status").get_json()["log"]:
        assert time.time() < deadline, "the step never started"
        time.sleep(0.1)
    t0 = time.time()
    resp = client.post("/api/repro/run/cancel", json={})
    assert resp.status_code == 200 and resp.get_json()["cancelling"] is True
    s = _wait(client, timeout=30)
    assert time.time() - t0 < 20
    assert s["cancelled"] is True and s["ok"] is False
    assert "cancelled" in s["error"]
    # the summary was not touched by the killed step
    rows = _read_summary(project / "Results" / "reproduction_summary.xlsx")
    assert [r[1] for r in rows["Reproduction"]] == ["old", "old", "old"]


def test_a_study_run_is_three_steps_with_a_soft_validator(client, project):
    resp = client.post("/api/repro/run", json={"kind": "study", "only": "Beta (2021)"})
    assert resp.status_code == 200, resp.get_json()
    s = _wait(client)
    assert s["steps"] == ["run reproducer", "build dossier", "validate dossier"]
    assert s["ok"] is True, s                       # the validator failure is soft
    assert s["warnings"] == ["validate dossier exited 1"]
    index = (project / "Results" / "repro_dossier_index.csv").read_text(encoding="utf-8-sig")
    assert "Alpha (2020)" in index and "Gamma (2022)" in index   # merged back
    assert "rebuilt" in index
    rows = _read_summary(project / "Results" / "reproduction_summary.xlsx")
    assert [r[1] for r in rows["Reproduction"]] == ["old", "new", "old"]


def test_run_routes_take_json_and_validate(client, project):
    assert client.post("/api/repro/run", data="kind=reproducers",
                       content_type="text/plain").status_code == 415
    assert client.post("/api/repro/run/cancel", data="x",
                       content_type="text/plain").status_code == 415
    assert client.post("/api/repro/run/cancel", json={}).status_code == 409   # idle
    resp = client.post("/api/repro/run", json={"kind": "study", "only": "Nobody (1999)"})
    assert resp.status_code == 400
    resp = client.post("/api/repro/run", json={"kind": "nope"})
    assert resp.status_code == 400
