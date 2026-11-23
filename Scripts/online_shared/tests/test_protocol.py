"""Tests for ``online_shared.protocol`` (classification, core, hashing)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from online_shared.protocol import (  # noqa: E402
    CORE_PREFIXES,
    DEFAULT_PORT,
    PROTOCOL_VERSION,
    ROLES,
    STAGES,
    TASKS,
    FileClass,
    classify,
    is_core,
    normalize_path,
    sha256_bytes,
    sha256_file,
)

# --- constants -------------------------------------------------------------


def test_protocol_constants():
    assert PROTOCOL_VERSION == 1
    assert DEFAULT_PORT == 8770
    assert STAGES == ("abstract", "fulltext", "coding")
    assert TASKS == ("abstract", "fulltext", "coding", "settle", "harmonize", "reproduce")
    assert ROLES == ("owner", "coder", "viewer")
    assert "Codings/" in CORE_PREFIXES and "Text/" in CORE_PREFIXES
    assert "Papers/" not in CORE_PREFIXES


# --- classify --------------------------------------------------------------

#: (path, expected FileClass) — one row per rule of the §1.1 table.
CLASSIFY_CASES = [
    # Rule 1 — excluded
    ("temp/x", FileClass("excluded")),
    ("temp/online/shared/scratch.json", FileClass("excluded")),
    ("archive/y", FileClass("excluded")),
    (".studio/state.json", FileClass("excluded")),
    (".claude/settings.json", FileClass("excluded")),
    (".git/HEAD", FileClass("excluded")),
    ("a/__pycache__/b.pyc", FileClass("excluded")),
    ("CodingStudioUni/x.exe", FileClass("excluded")),
    ("CodingStudio/_internal/x.dll", FileClass("excluded")),
    ("tool.exe", FileClass("excluded")),
    ("tool.pyc", FileClass("excluded")),
    ("draft.json.tmp", FileClass("excluded")),
    ("~$paper.docx", FileClass("excluded")),
    # Rule 2 — owned coding files (key greedy: tag = LAST " - " segment)
    (
        "Codings/Amar et al. (2026) - MM.json",
        FileClass("owned", key="Amar et al. (2026)", tag="MM", stage="coding"),
    ),
    (
        "Codings/Amar et al. (2026) - MM.md",
        FileClass("owned", key="Amar et al. (2026)", tag="MM", stage="coding"),
    ),
    (
        "Codings/Foo - Bar (2020) - SB.json",
        FileClass("owned", key="Foo - Bar (2020)", tag="SB", stage="coding"),
    ),
    (
        "Codings/Key (2021) - canonical.json",
        FileClass("owned", key="Key (2021)", tag="canonical", stage="coding"),
    ),
    # Rule 3 — administrative coding files
    ("Codings/x - _settlements.json", FileClass("admin")),
    ("Codings/Key (2021) - _notes.md", FileClass("admin")),
    # Rule 4 — screening stage file, one per tag
    ("Screening/OG.json", FileClass("owned", tag="OG", stage="abstract")),
    # Rule 5 — fulltext stage file
    ("Fulltext/MM.json", FileClass("owned", tag="MM", stage="fulltext")),
    # Rule 6 — settled extraction
    ("Extraction/Key (2021).json", FileClass("shared", key="Key (2021)", task="settle")),
    ("Extraction/Key (2021).md", FileClass("shared", key="Key (2021)", task="settle")),
    # Rule 7 — reproduction report
    ("Reproduction/Key (2021).md", FileClass("shared", key="Key (2021)", task="reproduce")),
    # Rule 8 — variable assignments
    (
        "Datasets/K/K - variable_assignments.csv",
        FileClass("shared", key="K", task="harmonize"),
    ),
    # Rule 9 — dataset products
    ("Datasets/K/K - harmonized.csv", FileClass("shared", key="K", task="reproduce")),
    ("Datasets/K/K - variables.json", FileClass("shared", key="K", task="reproduce")),
    ("Datasets/K/K - dv_direction.csv", FileClass("shared", key="K", task="reproduce")),
    # Rule 10 — dataset code
    ("Datasets/K/harmonizer.py", FileClass("shared", key="K", task="reproduce")),
    ("Datasets/K/K - prepare.py", FileClass("shared", key="K", task="reproduce")),
    # Rule 11 — raw dataset data
    ("Datasets/K/raw/data.sav", FileClass("immutable", key="K")),
    ("Datasets/K/data.csv", FileClass("immutable", key="K")),
    # Rule 12 — sources
    ("Papers/Smith (2020).pdf", FileClass("immutable", key="Smith (2020)")),
    ("Text/Smith (2020).txt", FileClass("immutable", key="Smith (2020)")),
    (
        "Supplements/Smith (2020) Supplement 2.pdf",
        FileClass("immutable", key="Smith (2020)"),
    ),
    (
        "Supplements/Smith (2020) Supplement 10.docx",
        FileClass("immutable", key="Smith (2020)"),
    ),
    # Rule 13 — analysis products
    ("Results/a.xlsx", FileClass("derived")),
    ("Results/figures/a.png", FileClass("derived")),
    ("master_dataset.xlsx", FileClass("derived")),
    # Rule 14 — administrative files
    ("Project/project.json", FileClass("admin")),
    ("Screening/abc.csv", FileClass("admin")),
    ("README.md", FileClass("admin")),
    ("Something/else.txt", FileClass("admin")),
]


@pytest.mark.parametrize("path,expected", CLASSIFY_CASES)
def test_classify_table(path, expected):
    assert classify(path) == expected


def test_classify_accepts_backslashes_and_dot_slash():
    assert classify(r"Codings\Amar et al. (2026) - MM.json") == FileClass(
        "owned", key="Amar et al. (2026)", tag="MM", stage="coding"
    )
    assert classify(r"Datasets\K\K - harmonized.csv") == FileClass(
        "shared", key="K", task="reproduce"
    )
    assert classify("./.studio/state.json") == FileClass("excluded")
    assert classify(r"Papers\Smith (2020).pdf") == FileClass(
        "immutable", key="Smith (2020)"
    )


def test_classify_first_match_wins_excluded_over_core_prefix():
    # A .pyc under a core prefix is still excluded (rule 1 before everything).
    assert classify("Codings/__pycache__/x.pyc").kind == "excluded"


def test_normalize_path():
    assert normalize_path(r"Coding\x.json") == "Coding/x.json"
    assert normalize_path("././a/b") == "a/b"


def test_file_class_rejects_unknown_kind():
    with pytest.raises(ValueError):
        FileClass("nonsense")
    with pytest.raises(ValueError):
        FileClass(kind="nonsense")


# --- is_core ---------------------------------------------------------------

CORE_CASES = [
    # core
    ("Project/project.json", True),
    ("Codings/Amar et al. (2026) - MM.json", True),
    ("Extraction/Key (2021).json", True),
    ("Screening/OG.json", True),
    ("Screening/abc.csv", True),          # admin under a core prefix
    ("Fulltext/MM.json", True),
    ("Text/Smith (2020).txt", True),      # immutable but in CORE_PREFIXES
    ("Reproduction/Key (2021).md", True),
    ("master_dataset.xlsx", True),
    ("Datasets/K/K - harmonized.csv", True),      # shared
    ("Datasets/K/K - variable_assignments.csv", True),
    ("Datasets/K/harmonizer.py", True),
    # not core
    ("Papers/Smith (2020).pdf", False),
    ("Supplements/Smith (2020) Supplement 2.pdf", False),
    ("Datasets/K/raw/data.sav", False),   # immutable raw data
    ("Results/a.xlsx", False),            # derived, pulled separately
    ("temp/x", False),
    ("Codings/__pycache__/x.pyc", False), # excluded beats the core prefix
    ("README.md", False),                 # admin outside every core prefix
]


@pytest.mark.parametrize("path,expected", CORE_CASES)
def test_is_core(path, expected):
    assert is_core(path) is expected
    assert is_core(path, classify(path)) is expected


# --- hashing ---------------------------------------------------------------


def test_sha256_bytes_known_value():
    assert sha256_bytes(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert sha256_bytes(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_sha256_file_streams_and_matches(tmp_path):
    payload = b"line one\n" + b"x" * 3_000_000 + b"\nlast\n"
    target = tmp_path / "big.bin"
    target.write_bytes(payload)
    assert sha256_file(str(target)) == sha256_bytes(payload)


# --- audit-driven rules (2026-11-23, AUDIT_client.md W4/W5/W7/W8/W9) ---------
import pytest as _pt
from online_shared.protocol import classify as _cl, is_core as _core


@_pt.mark.parametrize("path,kind,key,tag,stage,core", [
    ("Codings/Moravec et al. (2020) - assignments - MM.json", "owned", "Moravec et al. (2020)", "MM", "coding", True),
    ("Codings/A - B (2020) - harmonizer - DS.json", "owned", "A - B (2020)", "DS", "coding", True),
    ("Codings/x - harmonizer - _y.json", "admin", None, None, None, True),
    ("Screening/fulltext - MM.json", "owned", None, "MM", "fulltext", True),
    ("Screening/fulltext_agreed.json", "derived", None, None, None, True),
    ("Results/metaanalysis.xlsx", "derived", None, None, None, False),
])
def test_audit_rules(path, kind, key, tag, stage, core):
    fc = _cl(path)
    assert (fc.kind, fc.key, fc.tag, fc.stage) == (kind, key, tag, stage)
    assert _core(path, fc) is core
