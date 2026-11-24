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
    # Rule 9 — dataset products (a gzip-compressed product is the same product)
    ("Datasets/K/K - harmonized.csv", FileClass("shared", key="K", task="reproduce")),
    ("Datasets/K/K - harmonized.csv.gz", FileClass("shared", key="K", task="reproduce")),
    ("Datasets/K/K - prepared.csv.gz", FileClass("shared", key="K", task="reproduce")),
    ("Datasets/K/K - harmonized - SB.csv.gz", FileClass("shared", key="K", task="reproduce")),
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
    # Rule 13 - analysis products
    ("Results/a.xlsx", FileClass("derived")),
    ("Results/figures/a.png", FileClass("derived")),
    (
        "master_dataset.xlsx",
        FileClass("derived", blind_stages=("abstract", "fulltext")),
    ),
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
    ("Codings/Amar et al. (2026) - A.json", True),
    ("Extraction/Key (2021).json", True),
    ("Screening/OG.json", True),
    ("Screening/abc.csv", True),          # admin under a core prefix
    ("Fulltext/MM.json", True),
    ("Text/Smith (2020).txt", True),      # immutable but in CORE_PREFIXES
    ("Reproduction/Key (2021).md", True),
    ("master_dataset.xlsx", True),
    ("Datasets/K/K - variable_assignments.csv", True),
    ("Datasets/K/K - variables.csv", True),             # metadata sidecars stay
    ("Datasets/K/K - harmonized_variables.csv", True),
    ("Datasets/K/K - dv_direction.csv", True),
    ("Datasets/K/K - exclusions.csv", True),
    ("Datasets/K/K - conditions.csv", True),
    ("Datasets/K/K - repro_results.csv", True),
    ("Datasets/K/K - repro_facts.json", True),
    ("Datasets/K/harmonizer.py", True),
    ("Datasets/K/K - codebook.md", True),
    # not core: the dataset data products are fetched on demand (INTERFACES 4)
    ("Datasets/K/K - harmonized.csv", False),
    ("Datasets/K/K - harmonized.csv.gz", False),
    ("Datasets/K/K - harmonized - SB.csv", False),
    ("Datasets/K/K - harmonized - SB.csv.gz", False),
    ("Datasets/K/K - prepared.csv", False),
    ("Datasets/K/K - prepared 2.csv", False),
    ("Datasets/K/K - prepared.csv.gz", False),
    ("Datasets/K/K - dataset.csv", False),
    # not core: the AI coding routes are fetched on demand (AI_ROUTE_TAGS)
    ("Codings/Amar et al. (2026) - SB.json", False),
    ("Codings/Amar et al. (2026) - SB.md", False),
    ("Codings/Amar et al. (2026) - MM.json", False),
    ("Codings/Amar et al. (2026) - DS.json", False),
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


def test_gzip_product_is_classified_exactly_like_its_plain_twin():
    """A ``.csv.gz`` product must not fall through to rule 11 (immutable raw data).

    ``.csv`` matched rule 9 and ``.csv.gz`` used to fall to the raw-data rule, which
    lost the shared/reproduce semantics and made every reader treat it as source.
    """
    for base in ("harmonized", "prepared", "prepared 2", "harmonized - SB",
                 "dataset", "variables", "dv_direction", "exclusions", "conditions",
                 "repro_results"):
        plain = f"Datasets/K/K - {base}.csv"
        gz = plain + ".gz"
        assert classify(gz) == classify(plain), (plain, classify(plain), classify(gz))
        assert is_core(gz) is is_core(plain), (plain, is_core(plain), is_core(gz))
    # ... and a sidecar keeps its core-tier membership in both spellings
    assert is_core("Datasets/K/K - variables.csv.gz") is True
    assert is_core("Datasets/K/K - harmonized_variables.csv.gz") is True


# --- the AI coding routes are on demand (AI_ROUTE_TAGS, 2026-11-24) ---------

from online_shared.protocol import AI_ROUTE_TAGS, is_ai_route_file  # noqa: E402


def test_ai_route_tags_are_the_three_routes():
    assert AI_ROUTE_TAGS == frozenset({"SB", "MM", "DS"})


@pytest.mark.parametrize("tag", sorted(AI_ROUTE_TAGS))
@pytest.mark.parametrize("ext", ["json", "md"])
def test_ai_route_coding_files_are_on_demand(tag, ext):
    """``Codings/<Key> - SB|MM|DS.json(.md)``: owned like any coder, not core."""
    path = f"Codings/Amar et al. (2026) - {tag}.{ext}"
    fc = classify(path)
    assert fc == FileClass("owned", key="Amar et al. (2026)", tag=tag, stage="coding")
    assert is_ai_route_file(path) is True
    assert is_ai_route_file(path, fc) is True
    assert is_core(path) is False and is_core(path, fc) is False


@pytest.mark.parametrize("tag", sorted(AI_ROUTE_TAGS))
def test_ai_route_owned_drafts_and_stage_files_stay_core(tag):
    """Only the route's *coding* file is on demand - its tiny owned neighbours stay.

    A harmonizer/assignment draft is walked by its own tab on disk and a
    screening/full-text decision file is read offline by every coder, so both
    keep the old core rule even when the tag is an AI route.
    """
    for path in (f"Codings/Key (2021) - harmonizer - {tag}.json",
                 f"Codings/Key (2021) - assignments - {tag}.json",
                 f"Screening/{tag}.json",
                 f"Screening/fulltext - {tag}.json",
                 f"Fulltext/{tag}.json"):
        assert is_ai_route_file(path) is False, path
        assert is_core(path) is True, path


def test_a_human_tag_that_contains_an_ai_route_name_is_not_matched():
    """The match is the whole tag after the LAST `` - `` (a key may contain it)."""
    # 'MM' as part of a longer coder name is a different coder
    assert is_ai_route_file("Codings/Key (2021) - MM2.json") is False
    assert is_core("Codings/Key (2021) - MM2.json") is True
    # lower-case tag: classification is case-sensitive, so this is a human coder
    assert is_core("Codings/Key (2021) - mm.json") is True
    # a study key that itself ends in ' - SB' keeps its own coder file core
    assert is_core("Codings/Foo - SB (2021) - A.json") is True


def test_ai_route_artefacts_are_never_excluded_from_the_manifest():
    """On demand means "synced, listed, fetched lazily" - not "excluded".

    ``classify`` still says ``owned`` so the route stays a normal coder to every
    visibility/push rule; only the cache tier changes.
    """
    fc = classify("Codings/K (2021) - MM.json")
    assert fc.kind == "owned" and fc.stage == "coding" and fc.tag == "MM"
    assert fc.blind_stages == ()            # blinded through its stage, as before


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
    ("Fulltext/MF.json", "owned", None, "MF", "fulltext", True),
    ("Fulltext/OG.json", "owned", None, "OG", "fulltext", True),
    ("Project/master_records.csv.gz", "derived", None, None, None, True),
    ("Project/master_base.xlsx", "admin", None, None, None, True),
    ("Results/metaanalysis.xlsx", "derived", None, None, None, False),
])
def test_audit_rules(path, kind, key, tag, stage, core):
    fc = _cl(path)
    assert (fc.kind, fc.key, fc.tag, fc.stage) == (kind, key, tag, stage)
    assert _core(path, fc) is core


def test_the_screening_records_file_is_never_blinded_but_the_base_is():
    """The records file is what a blinded coder screens from; the base is not.

    ``master_records`` carries the bibliographic rows only (no verdict, stage or
    Screened_* column), so every member reads it whatever the blinding says.
    ``master_base`` keeps the curated stages (``Effective_Verdict``), so it
    follows the master workbook's stages.
    """
    records = classify("Project/master_records.csv.gz")
    assert records.kind == "derived" and records.blind_stages == ()
    assert classify("Project/master_records.csv").blind_stages == ()
    base = classify("Project/master_base.xlsx")
    assert base.kind == "admin" and base.blind_stages == ("abstract", "fulltext")
    # the master workbook itself stays a blocked consensus file
    assert classify("master_dataset.xlsx").blind_stages == ("abstract", "fulltext")


def test_fulltext_decision_file_is_owned_by_its_tag():
    """``Fulltext/<Tag>.json`` is the full-text stage's owned decision file.

    Only the owner of the tag may write it (no lock), it is always mirrored (a
    coder needs it offline), and nothing else in ``Fulltext/`` is a decision
    file - an arbitrary file there falls back to the administrative class.
    """
    fc = classify("Fulltext/MF.json")
    assert fc == FileClass("owned", tag="MF", stage="fulltext")
    assert is_core("Fulltext/MF.json", fc) is True
    assert classify("Fulltext/notes.txt") == FileClass("admin")
    # the legacy location stays owned too, so a half-migrated project keeps working
    assert classify("Screening/fulltext - MF.json") == FileClass(
        "owned", tag="MF", stage="fulltext")

# --- blind_stages: derived consensus files (INTERFACES 3.5, 2026-11-24) -----

BLIND_CASES = [
    ("master_dataset.xlsx", ("abstract", "fulltext")),
    # the compiler's base keeps the curated stages (Effective_Verdict): blinded
    ("Project/master_base.xlsx", ("abstract", "fulltext")),
    # the decision-free records table is what a blinded coder screens from
    ("Project/master_records.csv.gz", ()),
    ("Project/master_records.csv", ()),
    ("Screening/fulltext_agreed.json", ("fulltext",)),
    ("Codings/_settlements.json", ("abstract", "fulltext")),
    ("Codings/_settled.jsonl", ("abstract", "fulltext")),
    ("Codings/_resolutions/OG.json", ("abstract", "fulltext")),
    ("Results/sb_coding_agreement.csv", ("coding",)),
    ("Results/mm_coding_agreement.csv", ("coding",)),
    ("Results/coder_disagreement_tiers.csv", ("coding",)),
    ("Results/CODER_DISAGREEMENTS.md", ("coding",)),
    ("Results/HUMAN_MASTER_BRIEFING.md", ("coding",)),
    ("Results/irr_baseline_2026-11-23/agreement_overall.csv", ("coding",)),
    ("Results/sb_route_summary.json", ("coding",)),
    # a plain analysis product is not a per-coder verdict
    ("Results/metaanalysis.xlsx", ()),
    ("Results/harmonization_coverage.csv", ()),
    ("Results/repro_dossier_index.csv", ()),
    ("Codings/K (2021) - MM.json", ()),                   # owned files use stage
    ("Extraction/K (2021).json", ()),
]


@_pt.mark.parametrize("path,stages", BLIND_CASES)
def test_classify_blind_stages(path, stages):
    assert classify(path).blind_stages == stages


def test_blind_stages_are_validated():
    with pytest.raises(ValueError):
        FileClass("derived", blind_stages=("nonsense",))
