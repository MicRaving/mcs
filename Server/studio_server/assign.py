"""Pure assignment planner (INTERFACES_online_projects.md §3.9).

``plan_assignment`` splits keys among coders, giving a chosen share of keys to
two (``pairs`` mode) or all (``all`` mode) coders for inter-rater reliability.
Stdlib only, no I/O, deterministic for a given seed.

Tie-breaks left open by the algorithm text (all resolved by coder/list order):
  * ``round()`` is Python's banker's rounding (``round(2.5) == 2``).
  * Pair choice (pairs mode): among pairs with the minimal current count, the
    pair with the smallest ``load_a/share_a + load_b/share_b`` wins; a tie goes
    to the pair that comes first in the (already shuffled) pair list.
  * Single-key choice: the coder with the largest ``share*T - load`` wins; a
    tie goes to the coder that comes first in the coder list.
  * A pair ``(a, b)`` is stored with its coders in coder-list order.
  * ``pair_matrix`` includes every unordered pair of coders (zeros included) in
    both modes. Each key is ``"A|B"`` with A < B by string comparison (as the
    spec requires), and the dict is returned sorted by key.
  * Keys are shuffled first, then (pairs mode) the pair list is shuffled; both
    use the single ``random.Random(seed)`` in that order.
"""

from __future__ import annotations

import itertools
import random


def _validate(keys, coders, overlap_pct, overlap_mode, seed):
    if not isinstance(coders, (list, tuple)) or len(coders) == 0:
        raise ValueError("coders must be a non-empty list of (tag, share) pairs")
    tags = []
    shares = []
    for item in coders:
        try:
            tag, share = item
        except (TypeError, ValueError):
            raise ValueError(f"each coder must be a (tag, share) pair, got {item!r}")
        if not isinstance(tag, str) or not tag:
            raise ValueError(f"coder tag must be a non-empty string, got {tag!r}")
        if isinstance(share, bool) or not isinstance(share, (int, float)):
            raise ValueError(f"share for coder {tag!r} must be a number, got {share!r}")
        if not share > 0:
            raise ValueError(f"share for coder {tag!r} must be > 0, got {share!r}")
        tags.append(tag)
        shares.append(float(share))
    if len(set(tags)) != len(tags):
        raise ValueError("coder tags must be unique")
    if isinstance(overlap_pct, bool) or not isinstance(overlap_pct, (int, float)):
        raise ValueError("overlap_pct must be a number")
    if not 0 <= overlap_pct <= 100:
        raise ValueError(f"overlap_pct must be between 0 and 100, got {overlap_pct!r}")
    if overlap_mode not in ("pairs", "all"):
        raise ValueError(f"overlap_mode must be 'pairs' or 'all', got {overlap_mode!r}")
    if overlap_mode == "pairs" and len(coders) < 2:
        raise ValueError("overlap_mode 'pairs' needs at least 2 coders")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError(f"seed must be an integer, got {seed!r}")
    keys = list(keys)
    if len(set(keys)) != len(keys):
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        raise ValueError(f"duplicate keys: {dupes[:5]}")
    return keys, list(zip(tags, shares))


def plan_assignment(keys, coders, overlap_pct, overlap_mode, seed):
    """Plan which coder(s) get each key. See the module docstring for the rules."""
    keys, coders = _validate(keys, coders, overlap_pct, overlap_mode, seed)
    tags = [t for t, _ in coders]
    shares = [s for _, s in coders]
    idx = {t: i for i, t in enumerate(tags)}
    n_coders = len(coders)

    params = {
        "coders": [[t, s] for t, s in coders],
        "overlap_pct": overlap_pct,
        "overlap_mode": overlap_mode,
        "seed": seed,
        "n_keys": len(keys),
    }

    # All unordered pairs of coders, in coder-list order (for the zero-count matrix).
    all_pairs = list(itertools.combinations(range(n_coders), 2))
    pair_name = lambda a, b: "|".join(sorted((tags[a], tags[b])))  # noqa: E731

    if not keys:
        params["n_overlap"] = 0
        return {
            "assignments": {},
            "per_coder": {t: 0 for t in tags},
            "pair_matrix": {pair_name(a, b): 0 for a, b in all_pairs},
            "params": params,
        }

    rng = random.Random(seed)

    # Step 1: shuffle keys; first n_over are overlap keys.
    shuffled = list(keys)
    rng.shuffle(shuffled)
    n_over = round(len(keys) * overlap_pct / 100)
    params["n_overlap"] = n_over
    overlap_keys = shuffled[:n_over]
    single_keys = shuffled[n_over:]

    assign_idx = {}  # key -> sorted list of coder indices
    load = [0] * n_coders  # slots per coder so far
    pair_count = {p: 0 for p in all_pairs}

    # Step 2: overlap keys.
    if overlap_mode == "all":
        for k in overlap_keys:
            assign_idx[k] = list(range(n_coders))
            for i in range(n_coders):
                load[i] += 1
            for p in all_pairs:
                pair_count[p] += 1
        per_key_count = n_coders
    else:  # pairs
        pairs = list(all_pairs)
        rng.shuffle(pairs)
        for k in overlap_keys:
            min_c = min(pair_count[p] for p in pairs)
            best = None
            best_score = None
            for p in pairs:  # list order breaks ties (strict < keeps first)
                if pair_count[p] != min_c:
                    continue
                a, b = p
                score = load[a] / shares[a] + load[b] / shares[b]
                if best_score is None or score < best_score:
                    best, best_score = p, score
            a, b = best
            assign_idx[k] = [a, b]
            load[a] += 1
            load[b] += 1
            pair_count[best] += 1
        per_key_count = 2

    # Step 3: single keys to the coder furthest below its target load.
    total_target = (len(single_keys) + per_key_count * n_over) / sum(shares)
    for k in single_keys:
        best_i = None
        best_score = None
        for i in range(n_coders):  # coder order breaks ties (strict > keeps first)
            score = shares[i] * total_target - load[i]
            if best_score is None or score > best_score:
                best_i, best_score = i, score
        assign_idx[k] = [best_i]
        load[best_i] += 1

    # Step 4: return the structures.
    assignments = {}
    for k in keys:  # input order
        assignments[k] = [tags[i] for i in sorted(assign_idx[k])]
    per_coder = {tags[i]: load[i] for i in range(n_coders)}

    pair_matrix = {}
    for a, b in all_pairs:
        pair_matrix[pair_name(a, b)] = pair_count[(a, b)]

    return {
        "assignments": assignments,
        "per_coder": per_coder,
        "pair_matrix": dict(sorted(pair_matrix.items())),
        "params": params,
    }
