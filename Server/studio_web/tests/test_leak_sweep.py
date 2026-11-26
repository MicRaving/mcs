"""Blinding leak sweep: every GET route of the app, asked as a blinded browser coder.

PLAN_online_only §10 T3.  The suite is generated from ``app.url_map`` (so a route
added later is covered without touching this file): for every GET rule the path
parameters are filled with real keys and ids of the fixture project and the request
goes through the gateway exactly as the browser's would (session cookie, same
origin).  No response body may contain

* another tag's coding values (the fixture gives the other tag's coding file a
  ``FOREIGN-CODING-<i>`` marker in ``Rationale_Notes``, a field the coding route
  returns),
* another tag's screening or full-text decisions (``FOREIGN-SCREEN`` /
  ``FOREIGN-FULLTEXT`` in those files),
* the AI screening verdicts (``FOREIGN-AI-VERDICT`` in the master workbook),
* the Master's settlement store (``FOREIGN-SETTLEMENT``) or a derived
  cross-coder product (``FOREIGN-DERIVED`` in ``Results/…agreement…``),

for papers the coder has not finished.  The same sweep runs again after
"Finish paper": from then on the other tag's coding for *that* paper may appear -
and nothing else may.

The rule that makes this work is the server's visibility filter (the manifest and
the blob read check, ``Server/studio_server/visibility.py``): a worker's mirror can
only hold what the member may read.  The sweep is what proves no route re-derives
or reads around it.

Run with::

    $env:STUDIO_E2E="1"; python -m pytest Server/studio_web/tests/test_leak_sweep.py -q
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

import pytest

from .test_e2e_browser import (  # noqa: E402
    CODING_WEB,
    CODER,
    CODER_TAG,
    KEYS,
    OTHER_TAG,
    OWNER,
    SCRIPTS,
    app_path,
    browser_world,
    mirror_of,
)

pytestmark = pytest.mark.skipif(os.environ.get("STUDIO_E2E") != "1",
                                reason="set STUDIO_E2E=1 to run against the real services")

#: the fixture's marker helper lives next to the synthetic project
from online.tests.e2e_real_server import marker, markers  # noqa: E402

#: the paper the coder has NOT finished; the other tag's coding must stay hidden
UNFINISHED = KEYS[0]
STILL_HIDDEN = KEYS[1]

_ROUTE_SCRIPT = """
import json
import app
rules = set()
for rule in app.app.url_map.iter_rules():
    if (rule.methods - {"HEAD", "OPTIONS"}) == {"GET"}:
        rules.add(str(rule))
print(json.dumps(sorted(rules)))
"""

_ARG = re.compile(r"<(?:[^<>:]+:)?(\w+)>")

_rules_cache: list[str] | None = None


def app_get_rules(scratch: str) -> list[str]:
    """Every GET rule of the app, read from its URL map in a throw-away interpreter.

    The route table is code, not project state: a separate process with an empty
    ``CODING_STUDIO_ROOT`` reads it, so the test process never configures the app
    to a root of its own.
    """
    global _rules_cache
    if _rules_cache is not None:
        return _rules_cache
    env = dict(os.environ)
    env["CODING_STUDIO_ROOT"] = os.path.join(scratch, "route-root")
    env["PYTHONPATH"] = SCRIPTS + os.pathsep + env.get("PYTHONPATH", "")
    out = subprocess.run([sys.executable, "-c", _ROUTE_SCRIPT], cwd=CODING_WEB, env=env,
                         capture_output=True, text=True, timeout=180, check=False)
    assert out.returncode == 0, f"could not read the app's URL map:\n{out.stderr[-2000:]}"
    _rules_cache = json.loads(out.stdout)
    assert _rules_cache, "no GET rules"
    return _rules_cache


def concrete(rule: str, samples: dict) -> str:
    """A rule with every path parameter replaced by its sample value."""
    return _ARG.sub(lambda m: urllib.parse.quote(str(samples[m.group(1)]), safe=""), rule)


def samples_for(w, uid: str) -> dict:
    # "username" covers the owner's invitation routes (/api/online/link/invite/
    # <username>); the sweep asks them as a coder, which the policy must refuse
    return {"key": UNFINISHED, "coder": CODER_TAG, "uid": uid, "page": 1, "cid": 1,
            "name": "notes.pdf", "path": "app.js", "username": OWNER[0]}


#: query parameters a route needs to reach its per-study code path (without them
#: the app answers 400 "a valid study key is required" and the interesting body
#: is never produced)
QUERY = (
    ("/api/harmonizer_agreement/", {"key": UNFINISHED}),
    ("/api/agreement/conflicts", {"key": UNFINISHED}),
)


def query_for(rule: str) -> str:
    for prefix, extra in QUERY:
        if rule.startswith(prefix):
            return "?" + urllib.parse.urlencode({k: str(v) for k, v in extra.items()})
    return ""


def screening_uid(w) -> str:
    """A real abstract id from the fixture's decision-free records file."""
    st, body = w.anna.json("GET", app_path(w, "api/screening/list?coder=" + CODER_TAG))
    if st == 200 and body.get("rows"):
        return str(body["rows"][0]["i"])
    return "E2E001"


def sweep(w, browser, forbidden: list[str], samples: dict) -> tuple[int, dict, dict]:
    """Ask every GET route; return ``(requests, hits, status_counts)``.

    ``hits`` maps ``"<rule> -> <status>"`` to the markers found in its body.  The
    sweep is exhaustive on purpose: a route that leaks need not be a route the UI
    calls.
    """
    hits: dict[str, list[str]] = {}
    statuses: dict[int, int] = {}
    requests = 0
    for rule in app_get_rules(w.scratch):
        variants = [samples]
        if "<path:coder>" in rule:
            # the coder's own tag and the foreign tag: the gate must refuse the
            # second, and neither answer may carry the foreign values
            variants.append({**samples, "coder": OTHER_TAG})
        for variant in variants:
            rest = concrete(rule, variant).lstrip("/")
            status, raw, _headers = browser.req("GET", app_path(w, rest) + query_for(rule))
            requests += 1
            statuses[status] = statuses.get(status, 0) + 1
            text = raw.decode("utf-8", "replace")
            found = [m for m in forbidden if m in text]
            if found:
                hits[f"{rule} -> {status}"] = found
    return requests, hits, statuses


def hidden_paths() -> list[str]:
    """Fixture paths a blinded coder must not receive, whoever asks."""
    paths = ["master_dataset.xlsx", "Results/MM_SB_agreement.csv", "Codings/_settlements.json",
             f"Screening/{OTHER_TAG}.json", f"Screening/fulltext - {OTHER_TAG}.json"]
    paths += [f"Codings/{key} - {OTHER_TAG}.json" for key in KEYS]
    return paths


# ---------------------------------------------------------------------------
# the world: one server, one gateway, one browser coder
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def sweep_world():
    with browser_world(prefix="leak-") as w:
        w.anna.login(*CODER)
        # the coder's worker links the core tier on the first request
        st, status = w.anna.json("GET", app_path(w, "api/online/status"))
        assert st == 200 and status["browser"] is True, status
        yield w


def test_s00_the_fixture_really_carries_the_markers(sweep_world):
    """Guard against a vacuous sweep: every marker exists in the published project."""
    w = sweep_world
    classes = {
        "CODING": [f"Codings/{key} - {OTHER_TAG}.json" for key in KEYS],
        "SCREEN": [f"Screening/{OTHER_TAG}.json"],
        "FULLTEXT": [f"Screening/fulltext - {OTHER_TAG}.json"],
        "AI-VERDICT": ["master_dataset.xlsx"],
        "SETTLEMENT": ["Codings/_settlements.json"],
        "DERIVED": ["Results/MM_SB_agreement.csv"],
    }
    for kind, rels in classes.items():
        text = ""
        for rel in rels:
            with open(os.path.join(w.source, *rel.split("/")), "rb") as fh:
                text += fh.read().decode("utf-8", "replace")
        for i in range(len(KEYS)):
            if kind in ("AI-VERDICT", "DERIVED", "SETTLEMENT") and i > 0:
                continue          # one marker for the whole file
            assert marker(kind, i) in text, f"{kind}: {marker(kind, i)} missing from {rels}"
    assert len(markers()) == 6 * len(KEYS)


def test_s01_the_blinded_mirror_holds_only_the_coders_own_view(sweep_world):
    """The file-level rule the route sweep then exercises (PLAN_online_only §5)."""
    w = sweep_world
    mirror = mirror_of(w, CODER[0])
    assert mirror, "the coder has no mirror"
    for rel in hidden_paths():
        assert not os.path.exists(os.path.join(mirror, *rel.split("/"))), rel
    assert os.path.isfile(os.path.join(mirror, "Codings", f"{UNFINISHED} - {CODER_TAG}.json"))
    assert os.path.isfile(os.path.join(mirror, "Project", "master_records.csv.gz"))


def test_s02_no_get_route_leaks_a_foreign_value(sweep_world):
    """Every GET route, as a blinded coder who has finished nothing."""
    w = sweep_world
    # the other tag's values, and its *name* (the quoted tag value, its file names):
    # nothing may mention it before the coder has finished the paper itself
    needles = markers() + [f'"{OTHER_TAG}"', f" - {OTHER_TAG}.json", f"{OTHER_TAG}.json"]
    requests, hits, statuses = sweep(w, w.anna, needles, samples_for(w, screening_uid(w)))
    assert requests >= 90, requests          # the URL map's GET rules (90 in this build)
    assert hits == {}, f"{len(hits)} route(s) leaked:\n{json.dumps(hits, indent=2)[:3000]}"
    # the sweep must have reached the app, not just 403s
    assert statuses.get(200, 0) >= 20, statuses
    assert 0 not in statuses, statuses


def test_s03_finish_paper_shows_the_other_tag_for_that_paper_only(sweep_world):
    """After Finish paper: that paper's coding is visible, nothing else changes."""
    w = sweep_world
    st, body = w.anna.json("POST", app_path(w, "api/online/done"),
                           {"key": UNFINISHED, "stage": "coding", "tag": CODER_TAG})
    assert st == 200, body
    w.anna.json("POST", app_path(w, "api/online/sync"), {})

    q = urllib.parse.quote
    mirror = mirror_of(w, CODER[0])
    foreign_file = os.path.join(mirror, "Codings", f"{UNFINISHED} - {OTHER_TAG}.json")
    end = time.time() + 60.0
    while time.time() < end and not os.path.isfile(foreign_file):
        w.anna.json("POST", app_path(w, "api/online/sync"), {})
        time.sleep(0.5)
    assert os.path.isfile(foreign_file), "Finish paper did not unblind that paper's coding"
    # ... for that paper only
    assert not os.path.exists(os.path.join(mirror, "Codings", f"{STILL_HIDDEN} - {OTHER_TAG}.json"))

    st, body = w.anna.json("GET", app_path(w, f"api/coding/{q(UNFINISHED, safe='')}/{OTHER_TAG}"))
    assert st == 200, body
    assert marker("CODING", KEYS.index(UNFINISHED)) in json.dumps(body), body
    st, body = w.anna.json("GET", app_path(w, f"api/coding/{q(STILL_HIDDEN, safe='')}/{OTHER_TAG}"))
    assert st == 404, body

    # the whole sweep again: only this paper's coding marker may now appear
    allowed = [marker("CODING", KEYS.index(UNFINISHED))]
    forbidden = [m for m in markers() if m not in allowed]
    requests, hits, _statuses = sweep(w, w.anna, forbidden, samples_for(w, screening_uid(w)))
    assert requests >= 90, requests
    assert hits == {}, f"{len(hits)} route(s) leaked after Finish paper:\n{json.dumps(hits, indent=2)[:3000]}"

    # the unblinded files themselves are still absent from the mirror
    for rel in ("master_dataset.xlsx", "Results/MM_SB_agreement.csv", "Codings/_settlements.json",
                f"Screening/{OTHER_TAG}.json"):
        assert not os.path.exists(os.path.join(mirror, *rel.split("/"))), rel


def test_s04_owner_control_the_markers_are_reachable_for_a_permitted_viewer(sweep_world):
    """Positive control: the same calls, as the owner, do return the other tag's values.

    Without this, a marker that no route ever returns would make the sweep pass
    for the wrong reason.  The second half runs the *whole* sweep as the owner
    with the same forbidden list: it must report hits, which proves the detector
    reads real response bodies and not just the fixture.
    """
    w = sweep_world
    w.owner.login(*OWNER)
    q = urllib.parse.quote
    st, body = w.owner.json("GET", app_path(w, f"api/coding/{q(UNFINISHED, safe='')}/{OTHER_TAG}"))
    assert st == 200, body
    assert marker("CODING", KEYS.index(UNFINISHED)) in json.dumps(body), body
    # and the owner's mirror carries the workbook the blinded coder never gets
    mirror = mirror_of(w, OWNER[0])
    w.owner.json("POST", app_path(w, "api/online/sync"), {})
    assert os.path.isfile(os.path.join(mirror, "master_dataset.xlsx"))

    requests, hits, _statuses = sweep(w, w.owner, markers(), samples_for(w, screening_uid(w)))
    assert requests >= 90, requests
    seen = {m for found in hits.values() for m in found}
    assert marker("CODING", KEYS.index(UNFINISHED)) in seen, \
        f"the sweep cannot detect a leak: it found no marker even for the owner ({hits})"
