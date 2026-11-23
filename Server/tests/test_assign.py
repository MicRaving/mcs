"""Tests for studio_server.assign.plan_assignment (INTERFACES §3.9)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from studio_server.assign import plan_assignment  # noqa: E402

KEYS = [f"K{i:03d}" for i in range(100)]
EQUAL3 = [("A", 1), ("B", 1), ("C", 1)]
SHARES211 = [("A", 2), ("B", 1), ("C", 1)]


def _plan(keys=KEYS, coders=EQUAL3, pct=20, mode="pairs", seed=7):
    return plan_assignment(keys, coders, pct, mode, seed)


def test_deterministic_for_same_seed():
    assert _plan(seed=3) == _plan(seed=3)


def test_different_seed_usually_differs():
    a = _plan(seed=1)["assignments"]
    diffs = sum(_plan(seed=s)["assignments"] != a for s in range(2, 12))
    assert diffs >= 8


@pytest.mark.parametrize("mode", ["pairs", "all"])
def test_every_key_assigned_at_least_once(mode):
    res = _plan(mode=mode, pct=35)
    assert set(res["assignments"]) == set(KEYS)
    assert all(len(v) >= 1 for v in res["assignments"].values())


@pytest.mark.parametrize("pct", [0, 10, 20, 33, 50, 99, 100])
def test_overlap_count_is_round_n_pct(pct):
    res = _plan(pct=pct)
    n_over = sum(1 for v in res["assignments"].values() if len(v) > 1)
    assert n_over == round(len(KEYS) * pct / 100)
    assert res["params"]["n_overlap"] == round(len(KEYS) * pct / 100)


def test_pairs_mode_each_overlap_key_has_two_distinct_coders():
    res = _plan(pct=40, mode="pairs")
    for k, tags in res["assignments"].items():
        assert len(tags) in (1, 2)
        assert len(set(tags)) == len(tags)
        if len(tags) == 2:
            assert tags == sorted(tags, key=["A", "B", "C"].index)


def test_pairs_mode_pair_counts_balanced_for_equal_shares():
    res = _plan(pct=30, mode="pairs")
    counts = list(res["pair_matrix"].values())
    assert len(counts) == 3
    assert max(counts) - min(counts) <= 1
    assert sum(counts) == round(len(KEYS) * 0.30)


def test_pair_matrix_keys_sorted_and_zero_counts_included():
    res = _plan(coders=[("zed", 1), ("amy", 1), ("mid", 1)], pct=0, mode="pairs")
    assert res["pair_matrix"] == {"amy|mid": 0, "amy|zed": 0, "mid|zed": 0}


def test_all_mode_overlap_keys_have_all_coders():
    res = _plan(pct=25, mode="all")
    for tags in res["assignments"].values():
        assert tags in (["A"], ["B"], ["C"], ["A", "B", "C"])
    overlap = [k for k, v in res["assignments"].items() if len(v) == 3]
    assert len(overlap) == round(len(KEYS) * 0.25)
    # every pair counts each overlap key once
    assert all(v == len(overlap) for v in res["pair_matrix"].values())


def test_per_coder_balanced_for_equal_shares():
    res = _plan(pct=20, mode="pairs")
    loads = list(res["per_coder"].values())
    assert max(loads) - min(loads) <= 1


def test_per_coder_proportional_to_shares_211():
    res = _plan(coders=SHARES211, pct=20, mode="pairs")
    total = sum(res["per_coder"].values())
    ideal = {"A": total * 2 / 4, "B": total * 1 / 4, "C": total * 1 / 4}
    for tag, n in res["per_coder"].items():
        assert abs(n - ideal[tag]) <= 2, (tag, n, ideal[tag])


def test_zero_overlap_each_key_exactly_one_coder():
    res = _plan(pct=0, mode="pairs")
    assert all(len(v) == 1 for v in res["assignments"].values())
    assert sum(res["per_coder"].values()) == len(KEYS)


def test_full_overlap_pairs_every_key_two_coders():
    res = _plan(pct=100, mode="pairs")
    assert all(len(v) == 2 for v in res["assignments"].values())
    assert sum(res["pair_matrix"].values()) == len(KEYS)


def test_full_overlap_all_every_key_all_coders():
    res = _plan(pct=100, mode="all")
    assert all(v == ["A", "B", "C"] for v in res["assignments"].values())
    assert res["per_coder"] == {"A": 100, "B": 100, "C": 100}


def test_per_coder_sums_equal_total_slots():
    for mode, pct in [("pairs", 0), ("pairs", 37), ("all", 20), ("all", 100)]:
        res = _plan(pct=pct, mode=mode)
        slots = sum(len(v) for v in res["assignments"].values())
        assert sum(res["per_coder"].values()) == slots


def test_params_echo_inputs_and_seed():
    res = _plan(coders=SHARES211, pct=20, mode="pairs", seed=99)
    p = res["params"]
    assert p["seed"] == 99
    assert p["overlap_pct"] == 20
    assert p["overlap_mode"] == "pairs"
    assert p["coders"] == [["A", 2], ["B", 1], ["C", 1]]


def test_empty_keys_gives_empty_result():
    res = plan_assignment([], EQUAL3, 20, "pairs", 1)
    assert res["assignments"] == {}
    assert res["per_coder"] == {"A": 0, "B": 0, "C": 0}
    assert res["pair_matrix"] == {"A|B": 0, "A|C": 0, "B|C": 0}


def test_single_coder_all_mode_ok():
    res = plan_assignment(KEYS[:5], [("solo", 1)], 40, "all", 1)
    assert res["per_coder"] == {"solo": 5}


@pytest.mark.parametrize(
    "args, msg",
    [
        (([], [], 10, "pairs", 1), "non-empty"),
        ((KEYS, [("A", 1), ("A", 2)], 10, "all", 1), "unique"),
        ((KEYS, [("A", 0)], 10, "all", 1), "> 0"),
        ((KEYS, [("A", -1), ("B", 1)], 10, "pairs", 1), "> 0"),
        ((KEYS, EQUAL3, -1, "pairs", 1), "between 0 and 100"),
        ((KEYS, EQUAL3, 101, "pairs", 1), "between 0 and 100"),
        ((KEYS, EQUAL3, 10, "triples", 1), "overlap_mode"),
        ((KEYS, [("A", 1)], 10, "pairs", 1), "at least 2 coders"),
        ((["x", "y", "x"], EQUAL3, 10, "pairs", 1), "duplicate"),
        ((KEYS, EQUAL3, 10, "pairs", "7"), "seed"),
    ],
)
def test_validation_errors(args, msg):
    with pytest.raises(ValueError, match=msg):
        plan_assignment(*args)
