"""Tests for the PRISMA-counts disk cache (PLAN_online_only §6.3.2, WP6).

The counts cost ~100 s and a 340 MB pandas peak on the real corpus; the route
cached them in memory only, so every browser-worker restart paid again.  The
disk cache must (a) answer a second process, (b) never change the payload
(JSON round-trip is byte-identical), and (c) miss when an input changes.
"""

from __future__ import annotations

import json
import os

import pytest

import routes_apa
from studio_state import S, configure, refresh_keys


FLOW = {
    "identified_records": 12,
    "duplicates_removed": 2,
    "reports": 10,
    "abstract_excluded": 4,
    "fulltext_excluded": 3,
    "included_studies": 3,
    "fulltext_assessed": 6,
    "sought_for_retrieval": 6,
    "not_retrieved": 0,
    "included_records": 3,
    "duplicate_included_records": 0,
    "extraction": {"distinct_coded_papers": 3},
    "coded_fulltext_excluded": 1,
    "coded_fulltext_excluded_by_criterion": {"C2": 1},
    "coded_fulltext_excluded_detail": {"from_extraction_field": 1,
                                       "from_agreed_file": 0,
                                       "overlap": 0},
    "abstract_excluded_by_reason": {"C1: No misinformation intervention": 4},
    "fulltext_excluded_by_reason": {"C2: No control/comparison group": 3,
                                    "C3: Not English": 1},
}


def _write_master(path, tag="v1"):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Search"
    ws.append(["an", "title", "Hit_Source", "Fulltext_Verdict"])
    ws.append(["1", f"Study {tag}", "ebsco_export", "Include"])
    wb.save(str(path))


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    for name in ("Papers", "Text", "Extraction", "Codings", "Screening", "temp"):
        (root / name).mkdir(parents=True, exist_ok=True)
    _write_master(root / "master_dataset.xlsx")
    previous = S.ROOT
    configure(str(root))
    refresh_keys()
    calls = []

    def fake_flow(master=None):
        calls.append(1)
        return json.loads(json.dumps(FLOW))       # the counter's own fresh dict

    def no_split(path):
        raise RuntimeError("no track split in this test")

    monkeypatch.setattr(routes_apa._PRISMA, "flow", fake_flow)
    monkeypatch.setattr(routes_apa._PR_REPORT, "load_master", no_split)
    routes_apa._APA_PRISMA_CACHE.update(mtime=None, data=None)
    try:
        yield root, calls
    finally:
        routes_apa._APA_PRISMA_CACHE.update(mtime=None, data=None)
        configure(previous)
        refresh_keys()


def test_counts_are_cached_on_disk_and_unchanged(project):
    _root, calls = project
    first = routes_apa._prisma_counts()
    assert first is not None
    assert calls == [1]
    cache = os.path.join(S.TEMP, "prisma_counts.json")
    assert os.path.isfile(cache)
    with open(cache, encoding="utf-8") as fh:
        blob = json.load(fh)
    assert blob["data"] == first

    # a second request in the same process: memory hit, no recount
    assert routes_apa._prisma_counts() == first
    assert calls == [1]

    # a restarted process: the in-memory cache is empty, the disk cache answers
    routes_apa._APA_PRISMA_CACHE.update(mtime=None, data=None)
    second = routes_apa._prisma_counts()
    assert calls == [1], "a warm restart must not run the counter again"
    assert second == first
    assert (json.dumps(second, ensure_ascii=False)
            == json.dumps(first, ensure_ascii=False)), "the payload must not change"


def test_a_changed_master_misses_the_cache(project):
    root, calls = project
    assert routes_apa._prisma_counts() is not None
    assert calls == [1]
    _write_master(root / "master_dataset.xlsx", "v2")
    routes_apa._APA_PRISMA_CACHE.update(mtime=None, data=None)
    assert routes_apa._prisma_counts() is not None
    assert calls == [1, 1], "a rewritten master must be counted again"


def test_a_broken_cache_file_is_a_miss(project):
    _root, calls = project
    first = routes_apa._prisma_counts()
    with open(os.path.join(S.TEMP, "prisma_counts.json"), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    routes_apa._APA_PRISMA_CACHE.update(mtime=None, data=None)
    assert routes_apa._prisma_counts() == first
    assert calls == [1, 1]
