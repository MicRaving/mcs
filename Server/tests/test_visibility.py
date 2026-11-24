"""Unit tests for the pure visibility rules (INTERFACES §3.5)."""

from __future__ import annotations

from online_shared.protocol import classify

from studio_server.visibility import (
    can_read,
    can_write,
    done_set,
    has_done_for_member,
    lock_allows,
    lock_expired,
    stage_blinding,
    write_denial,
)

KEY = "Smith et al. (2020)"
BLINDED = {"blinding": {"abstract": "blinded", "fulltext": "blinded", "coding": "blinded"}}
VISIBLE = {"blinding": {"abstract": "visible", "fulltext": "visible", "coding": "visible"}}


def member(role="coder", tags=("Anna",), tasks=("coding",), uid=2):
    return {
        "user_id": uid,
        "role": role,
        "tags": list(tags),
        "tasks": list(tasks),
    }


OWNER = member(role="owner", tags=["*"], tasks=["abstract", "fulltext", "coding", "settle", "harmonize", "reproduce"])
ANNA = member()
BOB = member(tags=["Bob"], uid=3)


def lock(uid=2, expires="2999-01-01T00:00:00Z", key=KEY):
    return {
        "project_id": "p",
        "key": key,
        "user_id": uid,
        "client_id": "c",
        "acquired": "2026-01-01T00:00:00Z",
        "renewed": "2026-01-01T00:00:00Z",
        "expires": expires,
        "offline": 0,
    }


# ── read ───────────────────────────────────────────────────────────────


def test_owner_reads_everything_but_excluded():
    for path in (
        f"Codings/{KEY} - Bob.json",
        f"Screening/Bob.json",
        f"Extraction/{KEY}.json",
        "Results/reanalysis.xlsx",
        "Project/project.json",
        "Papers/x.pdf",
    ):
        assert can_read(OWNER, classify(path), set(), BLINDED), path
    assert not can_read(OWNER, classify("temp/scratch.txt"), set(), BLINDED)


def test_member_reads_own_tag_file():
    assert can_read(ANNA, classify(f"Codings/{KEY} - Anna.json"), set(), BLINDED)
    assert can_read(ANNA, classify("Screening/Anna.json"), set(), BLINDED)


def test_coding_blinding_lifts_per_key_only_after_done():
    path = classify(f"Codings/{KEY} - Bob.json")
    assert not can_read(ANNA, path, set(), BLINDED)
    # a done mark for another key does not help
    assert not can_read(ANNA, path, {("Anna", "Other (2021)")}, BLINDED)
    done = {("Anna", KEY)}
    assert can_read(ANNA, path, done, BLINDED)
    # the mark is per key: another tag's file of a different key stays hidden
    assert not can_read(
        ANNA, classify("Codings/Jones (2021) - Bob.json"), done, BLINDED
    )
    # ... while every tag's file of the finished key is now readable
    assert can_read(ANNA, classify(f"Codings/{KEY} - MM.json"), done, BLINDED)


def test_coding_blinding_visible_setting_opens_everything():
    assert can_read(ANNA, classify(f"Codings/{KEY} - Bob.json"), set(), VISIBLE)


def test_whole_stage_files_stay_hidden_while_blinded():
    abstract = classify("Screening/Bob.json")
    fulltext = classify("Screening/fulltext - Bob.json")
    assert not can_read(ANNA, abstract, {("Anna", KEY)}, BLINDED)
    assert not can_read(ANNA, fulltext, {("Anna", KEY)}, BLINDED)
    assert can_read(ANNA, abstract, set(), VISIBLE)
    assert can_read(ANNA, fulltext, set(), VISIBLE)


def test_ai_tags_are_ordinary_tags():
    sb = member(tags=["SB"], tasks=["coding"], uid=9)
    mm_file = classify(f"Codings/{KEY} - MM.json")
    assert not can_read(sb, mm_file, set(), BLINDED)
    assert can_read(sb, mm_file, {("SB", KEY)}, BLINDED)
    assert can_read(sb, classify(f"Codings/{KEY} - SB.json"), set(), BLINDED)


def test_member_reads_shared_admin_derived_immutable():
    for path in (
        f"Extraction/{KEY}.json",
        f"Reproduction/{KEY}.md",
        f"Datasets/{KEY}/{KEY} - prepared.csv",
        "Project/project.json",
        "Results/x.xlsx",
        "Papers/x.pdf",
    ):
        assert can_read(BOB, classify(path), set(), BLINDED), path
    # ... except the derived consensus files (see the blind_stages tests below)
    assert can_read(BOB, classify("master_dataset.xlsx"), set(), VISIBLE)


def test_blinded_derived_consensus_files_are_hidden_from_a_coder():
    """The master workbook, settlements, the agreed full text and the Results/
    agreement products carry other coders' (and the AI's) verdicts: a blinded
    coder cannot read them, the owner always can, and "visible" opens them."""
    paths = (
        "master_dataset.xlsx",
        "Codings/_settlements.json",
        "Screening/fulltext_agreed.json",
        "Results/sb_coding_agreement.csv",
        "Results/mm_coding_agreement.csv",
        "Results/coder_disagreement_tiers.csv",
        "Results/HUMAN_MASTER_BRIEFING.md",
        "Results/irr_baseline_2026-11-23/agreement_overall.csv",
    )
    for path in paths:
        fc = classify(path)
        assert fc.blind_stages, path
        assert not can_read(ANNA, fc, set(), BLINDED), path
        assert can_read(OWNER, fc, set(), BLINDED), path
        assert can_read(ANNA, fc, set(), VISIBLE), path
    # a plain analysis product is not a per-coder verdict: readable either way
    assert can_read(ANNA, classify("Results/metaanalysis.xlsx"), set(), BLINDED)


def test_each_derived_file_follows_its_own_stage():
    """Only the stage a consensus file encodes governs it: blinding ``coding``
    does not hide the screening workbook, and a visible screening stage does
    not open the coding agreement products."""
    coding_blinded = {"blinding": {"abstract": "visible", "fulltext": "visible",
                                   "coding": "blinded"}}
    screening_blinded = {"blinding": {"abstract": "blinded", "fulltext": "blinded",
                                      "coding": "visible"}}
    assert can_read(ANNA, classify("master_dataset.xlsx"), set(), coding_blinded)
    assert not can_read(ANNA, classify("master_dataset.xlsx"), set(), screening_blinded)
    assert can_read(ANNA, classify("Screening/fulltext_agreed.json"), set(), coding_blinded)
    assert not can_read(ANNA, classify("Screening/fulltext_agreed.json"), set(),
                        screening_blinded)
    assert can_read(ANNA, classify("Results/sb_coding_agreement.csv"), set(),
                    screening_blinded)
    assert not can_read(ANNA, classify("Results/sb_coding_agreement.csv"), set(),
                        coding_blinded)


def test_unknown_member_reads_nothing_and_harmonizer_drafts_are_owned():
    assert not can_read(None, classify(f"Codings/{KEY} - Anna.json"), set(), BLINDED)
    draft = classify(f"Codings/{KEY} - harmonizer - Bob.json")
    assert draft.kind == "owned" and draft.stage == "coding"
    assert not can_read(ANNA, draft, set(), BLINDED)
    assert can_read(ANNA, draft, {("Anna", KEY)}, BLINDED)


def test_done_set_normalises_both_shapes():
    assert done_set({("A", "k")}) == {("A", "k")}
    assert done_set({"A": ["k", "j"]}) == {("A", "k"), ("A", "j")}
    assert done_set(None) == set()
    assert has_done_for_member(ANNA, {"Anna": ["k"]}, "k")


def test_stage_blinding_defaults_to_blinded():
    assert stage_blinding({}, "coding") == "blinded"
    assert stage_blinding(None, "coding") == "blinded"
    assert stage_blinding({"blinding": {"coding": "visible"}}, "coding") == "visible"


# ── write ──────────────────────────────────────────────────────────────


def test_owner_writes_everything_but_excluded():
    for path in (
        "Results/x.xlsx",
        "Project/project.json",
        "Papers/x.pdf",
        f"Codings/{KEY} - Anna.json",
        f"Extraction/{KEY}.json",
    ):
        assert can_write(OWNER, classify(path), set(), None), path
    assert not can_write(OWNER, classify("temp/x.py"), set(), None)


def test_owned_writes_need_the_tag():
    assert can_write(ANNA, classify(f"Codings/{KEY} - Anna.json"), set(), None)
    assert not can_write(ANNA, classify(f"Codings/{KEY} - Bob.json"), set(), None)
    assert write_denial(ANNA, classify(f"Codings/{KEY} - Bob.json"), set(), None) == "forbidden"
    assert not can_write(ANNA, classify("Screening/Bob.json"), set(), None)


def test_non_owner_cannot_write_derived_admin_immutable():
    for path in ("Results/x.xlsx", "Project/project.json", "Papers/x.pdf", "master_dataset.xlsx"):
        assert write_denial(ANNA, classify(path), set(), None) == "forbidden", path


def test_shared_writes_need_the_task():
    settle_coder = member(tasks=["settle"])
    assert can_write(settle_coder, classify(f"Extraction/{KEY}.json"), {("Anna", KEY)}, None)
    assert not can_write(ANNA, classify(f"Extraction/{KEY}.json"), {("Anna", KEY)}, None)
    reproduce_coder = member(tasks=["reproduce"])
    assert can_write(reproduce_coder, classify(f"Reproduction/{KEY}.md"), set(), None)
    assert not can_write(reproduce_coder, classify(f"Extraction/{KEY}.json"), set(), None)


def test_shared_write_lock_rule():
    settler = member(tasks=["settle"])
    fc = classify(f"Extraction/{KEY}.json")
    done = {("Anna", KEY)}
    assert can_write(settler, fc, done, None)
    assert can_write(settler, fc, done, lock(uid=2))
    assert not can_write(settler, fc, done, lock(uid=99))
    assert write_denial(settler, fc, done, lock(uid=99)) == "locked"
    # an expired lock does not block
    assert can_write(settler, fc, done, lock(uid=99, expires="2000-01-01T00:00:00Z"))
    assert lock_expired(lock(uid=99, expires="2000-01-01T00:00:00Z"))
    assert not lock_expired(lock(uid=99))
    # a lockless row (no expiry recorded) counts as expired
    assert lock_expired({"user_id": 99})


def test_settle_needs_a_done_mark():
    settler = member(tasks=["settle"])
    fc = classify(f"Extraction/{KEY}.json")
    assert write_denial(settler, fc, set(), None) == "forbidden"
    assert write_denial(settler, fc, {("Anna", KEY)}, None) is None
    # owner is exempt from the done rule
    assert can_write(OWNER, fc, set(), None)


def test_lock_allows_own_holder_and_none():
    assert lock_allows(None, ANNA)
    assert lock_allows(lock(uid=2), ANNA)
    assert not lock_allows(lock(uid=3), ANNA)


def test_viewer_has_only_the_tag_and_task_rights_it_was_given():
    # Roles carry no hidden write permission: a viewer's rights are its tags
    # and tasks, exactly as §3.5 spells them.
    viewer = member(role="viewer", tags=["Anna"], tasks=[])
    assert can_write(viewer, classify(f"Codings/{KEY} - Anna.json"), set(), None)
    assert not can_write(viewer, classify(f"Codings/{KEY} - Bob.json"), set(), None)
    assert not can_write(viewer, classify(f"Extraction/{KEY}.json"), {("Anna", KEY)}, None)
    assert not can_write(viewer, classify("Results/x.xlsx"), set(), None)
    # ... but may read
    assert can_read(viewer, classify(f"Extraction/{KEY}.json"), set(), BLINDED)
