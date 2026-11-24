"""The assignment save guards the guided gap walk relies on.

``variable_assignments.apply`` patches only the FIRST sidecar row of a raw key,
so an answer for another variable fed by the same raw column would silently
label the wrong variable.  The gap walk now sends the variable it labels
(``target_uniform``); the server refuses such a save with a readable reason,
and returns warnings (e.g. a "proposed" change over a non-blank label is only
logged as a conflict) that the UI shows.
"""

from __future__ import annotations

import pytest

from studio_state import S, configure, refresh_keys

KEY = "Shared (2024)"
SIDECAR = ("uniform_variable,role,dv_domain,veracity,timepoint,dv_polarity,original_variable,"
           "artifact,item_scope,notes\n"
           "dv_credibility_true_post,dv,credibility,true,post,higher_is_better,q1,*,,\n"
           "dv_credibility_false_post,dv,credibility,false,post,,q1,*,,\n")


@pytest.fixture
def client(tmp_path):
    root = tmp_path / "proj"
    folder = root / "Datasets" / KEY
    for name in ("Papers", "Text", "Extraction", "Codings", "Screening", "temp", "Results"):
        (root / name).mkdir(parents=True, exist_ok=True)
    folder.mkdir(parents=True)
    (folder / f"{KEY} - harmonized_variables.csv").write_text(SIDECAR, encoding="utf-8")
    previous = S.ROOT
    configure(str(root))
    refresh_keys()
    try:
        from app import app as flask_app

        yield flask_app.test_client()
    finally:
        configure(previous)
        refresh_keys()


def _row(**extra):
    row = {"artifact": "*", "original_variable": "q1", "item_scope": "",
           "uniform_variable": "", "role": "", "dv_domain": "", "veracity": "",
           "timepoint": "", "dv_polarity": "higher_is_worse", "status": "confirmed"}
    row.update(extra)
    return row


def test_a_shared_raw_column_cannot_label_the_second_variable(client):
    resp = client.post(f"/api/assignments/{KEY}", json={
        "coder": "AB", "apply_to_dataset": False,
        "assignments": [_row(target_uniform="dv_credibility_false_post")]})
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["ok"] is False
    assert any("applied to the first one" in e for e in body["errors"]), body


def test_the_first_variable_saves_with_a_warning(client):
    resp = client.post(f"/api/assignments/{KEY}", json={
        "coder": "AB", "apply_to_dataset": False,
        "assignments": [_row(target_uniform="dv_credibility_true_post")]})
    body = resp.get_json()
    assert resp.status_code == 200, body
    assert any("feeds 2 harmonized variables" in w for w in body["warnings"]), body


def test_a_proposed_change_over_a_label_is_flagged(client):
    resp = client.post(f"/api/assignments/{KEY}", json={
        "coder": "AB", "apply_to_dataset": False,
        "assignments": [_row(status="proposed", target_uniform="dv_credibility_true_post")]})
    body = resp.get_json()
    assert resp.status_code == 200, body
    assert any("only proposed" in w for w in body["warnings"]), body


def test_a_role_and_name_mismatch_is_refused(client):
    resp = client.post(f"/api/assignments/{KEY}", json={
        "coder": "AB", "apply_to_dataset": False,
        "assignments": [_row(original_variable="q9", role="dv",
                             uniform_variable="cov_age", dv_polarity="")]})
    assert resp.status_code == 400
    assert any("needs a dv_* uniform name" in e for e in resp.get_json()["errors"])
