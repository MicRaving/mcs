"""Tests for ``online_shared.merge`` — three-way merge of project files."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from online_shared.merge import DELETED, MergeResult, merge3  # noqa: E402


# --- helpers ---------------------------------------------------------------

def enc(obj) -> bytes:
    """Compact JSON bytes for the inputs (merge re-writes the canonical form)."""
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def dec(result: MergeResult):
    assert result.ok, result.conflicts
    assert result.merged is not None
    return json.loads(result.merged.decode("utf-8"))


def canonical(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def dv(name, mean, sd=1.0):
    return {"dv_name": name, "mean": mean, "sd": sd}


# --- JSON: dicts -----------------------------------------------------------

def test_disjoint_field_edits_of_records_merge_cleanly():
    base = {"Study_ID": "PAPER", "Records": [
        {"Study_ID": "A", "N_Analyzed": 100, "Setting": "lab"},
        {"Study_ID": "B", "N_Analyzed": 50, "Setting": "lab"},
    ]}
    ours = json.loads(json.dumps(base))
    theirs = json.loads(json.dumps(base))
    ours["Records"][0]["N_Analyzed"] = 120        # ours edits record A
    theirs["Records"][1]["Setting"] = "online"    # theirs edits record B

    result = merge3("Extraction/PAPER.json", enc(base), enc(ours), enc(theirs))
    merged = dec(result)
    assert merged["Records"][0]["N_Analyzed"] == 120
    assert merged["Records"][1]["Setting"] == "online"
    assert merged["Records"][0]["Setting"] == "lab"
    assert merged["Records"][1]["N_Analyzed"] == 50
    assert result.merged == canonical(merged)


def test_same_field_changed_differently_conflicts_with_json_path():
    base = {"Records": [{"Study_ID": "A", "N_Analyzed": 100}]}
    ours = {"Records": [{"Study_ID": "A", "N_Analyzed": 120}]}
    theirs = {"Records": [{"Study_ID": "A", "N_Analyzed": 130}]}

    result = merge3("Extraction/PAPER.json", enc(base), enc(ours), enc(theirs))
    assert result.ok is False
    assert result.merged is None
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict["where"] == '$.Records[Study_ID="A"].N_Analyzed'
    assert conflict["base"] == 100
    assert conflict["ours"] == 120
    assert conflict["theirs"] == 130


def test_equal_change_on_both_sides_is_clean():
    base = {"a": 1, "b": 2}
    ours = {"a": 9, "b": 2}
    theirs = {"a": 9, "b": 2}
    result = merge3("Project/project.json", enc(base), enc(ours), enc(theirs))
    assert dec(result) == {"a": 9, "b": 2}


def test_key_added_by_both_sides_with_different_values_conflicts():
    base = {"a": 1}
    ours = {"a": 1, "b": 2}
    theirs = {"a": 1, "b": 3}
    result = merge3("Project/project.json", enc(base), enc(ours), enc(theirs))
    assert result.ok is False
    assert result.conflicts[0]["where"] == "$.b"


def test_key_added_by_both_sides_with_same_value_is_clean():
    base = {"a": 1}
    ours = {"a": 1, "b": 2}
    theirs = {"a": 1, "b": 2}
    result = merge3("Project/project.json", enc(base), enc(ours), enc(theirs))
    assert dec(result) == {"a": 1, "b": 2}


def test_dict_key_deleted_on_one_side_unchanged_on_other_is_applied():
    base = {"a": 1, "b": 2}
    ours = {"a": 1}
    theirs = {"a": 1, "b": 2}
    assert dec(merge3("Project/project.json", enc(base), enc(ours), enc(theirs))) == {"a": 1}


def test_dict_key_deletion_versus_modification_conflicts():
    base = {"a": 1, "b": 2}
    ours = {"a": 1}
    theirs = {"a": 1, "b": 3}
    result = merge3("Project/project.json", enc(base), enc(ours), enc(theirs))
    assert result.ok is False
    assert result.conflicts[0]["where"] == "$.b"
    assert result.conflicts[0]["ours"] == DELETED


def test_nested_dicts_merge_by_field():
    base = {"settings": {"blinding": {"abstract": "blinded", "coding": "blinded"}}}
    ours = json.loads(json.dumps(base))
    theirs = json.loads(json.dumps(base))
    ours["settings"]["blinding"]["coding"] = "visible"
    theirs["settings"]["blinding"]["abstract"] = "visible"
    merged = dec(merge3("Project/project.json", enc(base), enc(ours), enc(theirs)))
    assert merged["settings"]["blinding"] == {"abstract": "visible", "coding": "visible"}


# --- JSON: DV rows (dv_name + occurrence index, never a composite key) -----

def test_dv_rows_aligned_by_name_and_occurrence_index():
    base = {"dvs": [dv("support", 5.0, 1.2), dv("support", 3.0, 0.8)]}
    ours = json.loads(json.dumps(base))
    theirs = json.loads(json.dumps(base))
    ours["dvs"][1]["mean"] = 3.5           # ours edits row #2
    theirs["dvs"][0]["sd"] = 1.0           # theirs edits row #1's other field

    result = merge3("Extraction/PAPER.json", enc(base), enc(ours), enc(theirs))
    merged = dec(result)
    assert merged["dvs"] == [
        dv("support", 5.0, 1.0),
        dv("support", 3.5, 0.8),
    ]


def test_dv_rows_same_row_same_field_different_values_conflicts():
    base = {"dvs": [dv("support", 5.0), dv("support", 3.0)]}
    ours = {"dvs": [dv("support", 5.0), dv("support", 3.5)]}
    theirs = {"dvs": [dv("support", 5.0), dv("support", 3.7)]}
    result = merge3("Extraction/PAPER.json", enc(base), enc(ours), enc(theirs))
    assert result.ok is False
    assert result.conflicts[0]["where"] == '$.dvs[dv_name="support"#2].mean'


def test_dv_row_inserted_on_one_side_is_applied():
    base = {"dvs": [dv("support", 5.0), dv("support", 3.0)]}
    ours = json.loads(json.dumps(base))
    ours["dvs"].append(dv("oppose", 2.0))           # new row at the end
    theirs = json.loads(json.dumps(base))
    theirs["dvs"][0]["mean"] = 5.5                  # unrelated edit

    merged = dec(merge3("Extraction/PAPER.json", enc(base), enc(ours), enc(theirs)))
    assert merged["dvs"] == [
        dv("support", 5.5),
        dv("support", 3.0),
        dv("oppose", 2.0),
    ]


def test_dv_row_inserted_at_front_with_new_name_is_applied():
    base = {"dvs": [dv("support", 5.0), dv("support", 3.0)]}
    ours = {"dvs": [dv("neutral", 4.0)] + base["dvs"]}
    theirs = json.loads(json.dumps(base))
    theirs["dvs"][0]["sd"] = 2.0

    merged = dec(merge3("Extraction/PAPER.json", enc(base), enc(ours), enc(theirs)))
    assert merged["dvs"] == [
        dv("neutral", 4.0),
        dv("support", 5.0, 2.0),
        dv("support", 3.0),
    ]


def test_dv_row_deleted_on_one_side_unchanged_on_other_is_applied():
    base = {"dvs": [dv("support", 5.0), dv("oppose", 3.0)]}
    ours = {"dvs": [dv("support", 5.0)]}           # ours deletes the row
    theirs = json.loads(json.dumps(base))
    theirs["dvs"][0]["sd"] = 1.5

    merged = dec(merge3("Extraction/PAPER.json", enc(base), enc(ours), enc(theirs)))
    assert merged["dvs"] == [dv("support", 5.0, 1.5)]


def test_dv_row_deletion_versus_modification_conflicts():
    base = {"dvs": [dv("support", 5.0), dv("oppose", 3.0)]}
    ours = {"dvs": [dv("support", 5.0)]}
    theirs = {"dvs": [dv("support", 5.0), dv("oppose", 3.9)]}
    result = merge3("Extraction/PAPER.json", enc(base), enc(ours), enc(theirs))
    assert result.ok is False
    assert result.conflicts[0]["where"] == '$.dvs[dv_name="oppose"]'
    assert result.conflicts[0]["theirs"] == dv("oppose", 3.9)


def test_followup_and_pretest_rows_stay_distinct():
    # The same dv_name three times: pre / post / follow-up rows must not be
    # collapsed into one composite key.
    rows = [dv("sharing", 4.0), dv("sharing", 3.0), dv("sharing", 2.0)]
    base = {"dvs": rows}
    ours = json.loads(json.dumps(base))
    theirs = json.loads(json.dumps(base))
    ours["dvs"][2]["mean"] = 2.5      # follow-up
    theirs["dvs"][0]["mean"] = 4.5    # pretest

    merged = dec(merge3("Extraction/PAPER.json", enc(base), enc(ours), enc(theirs)))
    assert [row["mean"] for row in merged["dvs"]] == [4.5, 3.0, 2.5]


# --- JSON: record lists without identity (positional) ----------------------

def test_record_list_without_identity_aligns_by_position():
    base = {"rows": [{"label": "a", "v": 1}, {"label": "b", "v": 2}]}
    ours = {"rows": [{"label": "a", "v": 10}, {"label": "b", "v": 2}]}
    theirs = {"rows": [{"label": "a", "v": 1}, {"label": "b", "v": 20}]}
    merged = dec(merge3("Project/project.json", enc(base), enc(ours), enc(theirs)))
    assert merged["rows"] == [{"label": "a", "v": 10}, {"label": "b", "v": 20}]


def test_record_list_without_identity_same_index_conflict():
    base = {"rows": [{"label": "a", "v": 1}]}
    ours = {"rows": [{"label": "a", "v": 10}]}
    theirs = {"rows": [{"label": "a", "v": 20}]}
    result = merge3("Project/project.json", enc(base), enc(ours), enc(theirs))
    assert result.ok is False
    assert result.conflicts[0]["where"] == "$.rows[0].v"


def test_record_list_without_identity_insertion_applies():
    base = {"rows": [{"label": "a", "v": 1}]}
    ours = {"rows": [{"label": "a", "v": 1}, {"label": "b", "v": 2}]}
    theirs = {"rows": [{"label": "a", "v": 9}]}
    merged = dec(merge3("Project/project.json", enc(base), enc(ours), enc(theirs)))
    assert merged["rows"] == [{"label": "a", "v": 9}, {"label": "b", "v": 2}]


def test_scalar_lists_are_atomic():
    base = {"tags": ["a", "b"]}
    ours = {"tags": ["a", "b", "ours"]}
    theirs = {"tags": ["a", "b", "theirs"]}
    result = merge3("Project/project.json", enc(base), enc(ours), enc(theirs))
    assert result.ok is False
    assert result.conflicts[0]["where"] == "$.tags"


# --- JSON: format and failure modes ---------------------------------------

def test_output_uses_canonical_json_format_and_keeps_unicode():
    base = {"title": "Café study", "n": 1}
    ours = {"title": "Café study", "n": 2}
    theirs = {"title": "Café study — revised", "n": 1}
    result = merge3("Project/project.json", enc(base), enc(ours), enc(theirs))
    merged = dec(result)
    assert merged == {"title": "Café study — revised", "n": 2}
    assert "Café".encode("utf-8") in result.merged          # not \u escaped
    assert result.merged == canonical(merged)


def test_base_none_with_equal_sides_is_clean():
    result = merge3("Extraction/PAPER.json", None, b'{"a": 1}', b'{"a": 1}')
    assert result.ok is True
    assert result.merged == b'{"a": 1}'


def test_base_none_with_different_sides_is_not_ok():
    result = merge3("Extraction/PAPER.json", None, b'{"a": 1}', b'{"a": 2}')
    assert result.ok is False
    assert result.merged is None
    assert result.conflicts


def test_unparsable_json_is_not_ok():
    result = merge3("Extraction/PAPER.json", b"{", b'{"a": 1}', b'{"a": 2}')
    assert result.ok is False
    assert result.merged is None


def test_identical_ours_and_theirs_wins_for_any_extension():
    payload = b"whatever\x00bytes"
    result = merge3("Datasets/K/blob.bin", b"old", payload, payload)
    assert result == MergeResult(ok=True, merged=payload, conflicts=[])


def test_unknown_extension_with_different_sides_is_not_ok():
    result = merge3("Datasets/K/blob.bin", b"old", b"ours", b"theirs")
    assert result.ok is False
    assert result.conflicts[0]["where"] == "Datasets/K/blob.bin"


# --- line-based merges -----------------------------------------------------

def test_csv_non_overlapping_lines_merge_cleanly():
    base = b"id,score\n1,10\n2,20\n3,30\n"
    ours = b"id,score\n1,11\n2,20\n3,30\n"
    theirs = b"id,score\n1,10\n2,20\n3,33\n"
    result = merge3("Datasets/K/K - harmonized.csv", base, ours, theirs)
    assert result.ok is True
    assert result.merged == b"id,score\n1,11\n2,20\n3,33\n"


def test_csv_overlapping_lines_conflict():
    base = b"id,score\n1,10\n2,20\n"
    ours = b"id,score\n1,11\n2,20\n"
    theirs = b"id,score\n1,12\n2,20\n"
    result = merge3("Datasets/K/K - harmonized.csv", base, ours, theirs)
    assert result.ok is False
    assert result.merged is None
    assert "Datasets/K/K - harmonized.csv" in result.conflicts[0]["where"]


def test_markdown_insertion_on_one_side_merges():
    base = b"# Title\n\nalpha\n\nomega\n"
    ours = b"# Title\n\nalpha\n\nbeta\n\nomega\n"
    theirs = b"# Title\n\nalpha revised\n\nomega\n"
    result = merge3("Reproduction/Key (2021).md", base, ours, theirs)
    assert result.ok is True
    assert result.merged == b"# Title\n\nalpha revised\n\nbeta\n\nomega\n"


def test_concurrent_insertion_at_same_place_conflicts():
    base = b"a\nb\n"
    ours = b"a\nours\nb\n"
    theirs = b"a\ntheirs\nb\n"
    result = merge3("README.md", base, ours, theirs)
    assert result.ok is False
    assert len(result.conflicts) == 1


def test_line_deletion_versus_modification_conflicts():
    base = b"a\nb\nc\n"
    ours = b"a\nc\n"                  # deletes b
    theirs = b"a\nB\nc\n"             # modifies b
    result = merge3("notes.txt", base, ours, theirs)
    assert result.ok is False


def test_line_deletion_one_side_applies():
    base = b"a\nb\nc\n"
    ours = b"a\nc\n"
    theirs = b"a\nb\nc\n"
    result = merge3("notes.txt", base, ours, theirs)
    assert result.ok is True
    assert result.merged == b"a\nc\n"


def test_python_line_merge_keeps_no_trailing_newline():
    base = b"x = 1\ny = 2"
    ours = b"x = 10\ny = 2"
    theirs = b"x = 1\ny = 20"
    result = merge3("Datasets/K/harmonizer.py", base, ours, theirs)
    assert result.ok is True
    assert result.merged == b"x = 10\ny = 20"


def test_identical_line_change_on_both_sides_is_clean():
    base = b"a\nb\nc\nd\n"
    ours = b"a\nB\nc\nD\n"
    theirs = b"a\nB\nc\nd\n"
    result = merge3("notes.txt", base, ours, theirs)
    assert result.ok is True
    assert result.merged == b"a\nB\nc\nD\n"


def test_empty_base_two_different_insertions_conflict():
    result = merge3("README.md", b"", b"ours\n", b"theirs\n")
    assert result.ok is False


def test_non_utf8_text_is_not_ok():
    result = merge3("notes.txt", b"a\n", b"\xff\n", b"b\n")
    assert result.ok is False
    assert result.merged is None
