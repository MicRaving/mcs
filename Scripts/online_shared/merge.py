"""Three-way merge (``base``, ``ours``, ``theirs``) for project files.

Called by the server when a push no longer matches the head (another client
wrote first) and, for the same reason, by the desktop client when resolving a
pull.  The contract is ``Results/INTERFACES_online_projects.md`` §2.

Two strategies:

* ``.json`` — parsed, merged at **field level**, so two coders editing
  different fields of the same coding record do not collide.  Lists of records
  are aligned by *identity*, never by a composite key (see AGENTS.md, "Coder
  disagreements"): ``Study_ID`` when the records carry it, otherwise
  ``(dv_name, occurrence index of that dv_name)`` for coding DV rows, and
  position only when nothing else identifies an item.
* ``.csv`` / ``.md`` / ``.txt`` / ``.py`` — line-based diff3 on top of
  :class:`difflib.SequenceMatcher`: non-overlapping changes are applied,
  overlapping ones are a conflict.

Anything else, a missing base with two different versions, or unparsable JSON
is ``ok=False``.  A clean merge returns the merged bytes; the JSON writer uses
the app's canonical format (``indent=2``, non-ASCII kept, trailing newline).
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field

JSON_EXTENSIONS = (".json",)
TEXT_EXTENSIONS = (".csv", ".md", ".txt", ".py")

#: Marker used in a conflict entry for the side that **deleted** the value.
#: A plain ``None`` would be ambiguous with a JSON ``null``; this sentinel is
#: a display string shown by the conflict UI.
DELETED = "<deleted>"


@dataclass
class MergeResult:
    """Outcome of :func:`merge3`.

    ``ok`` is True only when ``merged`` is a valid file.  ``conflicts`` holds
    ``{"where", "base", "ours", "theirs"}`` dicts; ``where`` is a JSON path
    (``$.Records[Study_ID="A"].N_Analyzed``) for JSON or the file path with a
    base line range for text merges.
    """

    ok: bool
    merged: bytes | None
    conflicts: list[dict] = field(default_factory=list)


class _Missing:
    """Sentinel: the key/value does not exist on that side."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<missing>"


_MISSING = _Missing()


# --- public entry point ----------------------------------------------------

def merge3(path: str, base: bytes | None, ours: bytes, theirs: bytes) -> MergeResult:
    """Merge two edited versions of *path* against their common *base*.

    Identical ``ours``/``theirs`` always win (``merged = ours``) — no parse is
    attempted.  ``base is None`` means "the file did not exist"; then only two
    identical versions can be merged.  The strategy is chosen from the file
    extension, not from the content.
    """
    if ours == theirs:
        return MergeResult(ok=True, merged=ours, conflicts=[])
    if base is None:
        return _failed(path, None, ours, theirs)
    ext = _extension(path)
    if ext in JSON_EXTENSIONS:
        return _merge_json(path, base, ours, theirs)
    if ext in TEXT_EXTENSIONS:
        return _merge_text(path, base, ours, theirs)
    return _failed(path, base, ours, theirs)


# --- JSON merge ------------------------------------------------------------

def _merge_json(path: str, base: bytes, ours: bytes, theirs: bytes) -> MergeResult:
    try:
        base_obj = json.loads(base.decode("utf-8"))
        ours_obj = json.loads(ours.decode("utf-8"))
        theirs_obj = json.loads(theirs.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _failed(path, base, ours, theirs)

    conflicts: list[dict] = []
    merged = _merge_value(base_obj, ours_obj, theirs_obj, "$", conflicts)
    if conflicts or merged is _MISSING:
        return MergeResult(ok=False, merged=None, conflicts=conflicts)
    text = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
    return MergeResult(ok=True, merged=text.encode("utf-8"), conflicts=[])


def _merge_value(base, ours, theirs, where: str, conflicts: list[dict]):
    """Three-way merge of one JSON value; returns the merged value or ``_MISSING``.

    Rule order: an unchanged side defers to the other, equal changes pass,
    otherwise dicts and lists-of-records are merged recursively and every other
    value is an atomic conflict.
    """
    if ours == theirs:
        return ours
    if base is not _MISSING:
        if base == ours:
            return theirs
        if base == theirs:
            return ours

    if isinstance(ours, dict) and isinstance(theirs, dict):
        if base is _MISSING or isinstance(base, dict):
            return _merge_dict(
                {} if base is _MISSING else base, ours, theirs, where, conflicts
            )

    if _is_record_list(ours) and _is_record_list(theirs):
        if base is _MISSING or _is_record_list(base):
            base_list = [] if base is _MISSING else base
            identity = _identity_field(base_list, ours, theirs)
            if identity is not None:
                return _merge_records(base_list, ours, theirs, where, conflicts, identity)
            return _merge_positional(base_list, ours, theirs, where, conflicts)

    conflicts.append(
        {
            "where": where,
            "base": None if base is _MISSING else base,
            "ours": ours,
            "theirs": theirs,
        }
    )
    return ours


def _merge_dict(base: dict, ours: dict, theirs: dict, where: str, conflicts: list[dict]):
    """Union of the three key sets; per key the three-way rule; deletions apply."""
    keys = list(ours)
    keys += [k for k in base if k not in ours]
    keys += [k for k in theirs if k not in ours and k not in base]

    out: dict = {}
    for key in keys:
        in_o, in_t, in_b = key in ours, key in theirs, key in base
        child = _child_path(where, key)

        if in_o and in_t and in_b:
            value = _merge_value(base[key], ours[key], theirs[key], child, conflicts)
        elif in_o and in_t:                      # added on both sides
            value = _merge_value(_MISSING, ours[key], theirs[key], child, conflicts)
        elif in_o and in_b:                      # theirs deleted the key
            if ours[key] == base[key]:
                value = _MISSING                  # unchanged -> deletion applies
            else:
                conflicts.append(
                    {"where": child, "base": base[key], "ours": ours[key], "theirs": DELETED}
                )
                value = ours[key]
        elif in_t and in_b:                      # ours deleted the key
            if theirs[key] == base[key]:
                value = _MISSING
            else:
                conflicts.append(
                    {"where": child, "base": base[key], "ours": DELETED, "theirs": theirs[key]}
                )
                value = theirs[key]
        elif in_o:                               # added by ours
            value = ours[key]
        elif in_t:                               # added by theirs
            value = theirs[key]
        else:                                    # both deleted
            value = _MISSING

        if value is not _MISSING:
            out[key] = value
    return out


# --- list-of-records merge -------------------------------------------------

def _is_record_list(value) -> bool:
    """True for a list whose items are all dicts (an empty list qualifies)."""
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def _identity_field(base: list, ours: list, theirs: list) -> str | None:
    """Pick the identity field for a list of records, if there is one.

    ``Study_ID`` first (the extraction records' join key), then ``dv_name``
    (coding DV rows: aligned by name *and* occurrence index, never by a
    composite key).  ``None`` means "align by position".
    """
    for candidate in ("Study_ID", "dv_name"):
        if all(_has_scalar_identity(item, candidate) for item in (*base, *ours, *theirs)):
            return candidate
    return None


def _has_scalar_identity(item: dict, field_name: str) -> bool:
    value = item.get(field_name)
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def _identity_keys(items: list, field_name: str) -> list[tuple]:
    """``(value, occurrence index)`` per item — the occurrence index keeps a
    repeated ``dv_name`` (one per arm/timepoint) addressable without inventing
    a composite key."""
    seen: dict = {}
    keys: list[tuple] = []
    for item in items:
        value = item.get(field_name)
        occurrence = seen.get(value, 0)
        seen[value] = occurrence + 1
        keys.append((value, occurrence))
    return keys


def _mapping(a: list, b: list) -> dict[int, int]:
    """Index map ``a[i] -> b[j]`` for equal elements (no autojunk)."""
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    result: dict[int, int] = {}
    for tag, i1, i2, j1, _j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                result[i1 + offset] = j1 + offset
    return result


def _insertion_anchors(keys: list[tuple], mapping: dict[int, int], base_len: int) -> dict[int, list[int]]:
    """Map base index -> side indices inserted just before that base item.

    An inserted item is anchored at the base index of the next matched item
    (``base_len`` when it is appended).  This is what lets an insertion be
    applied without shifting every following record.
    """
    side_to_base = {side: base for base, side in mapping.items()}
    anchors: dict[int, list[int]] = {}
    next_anchor = base_len
    for index in range(len(keys) - 1, -1, -1):
        if index in side_to_base:
            next_anchor = side_to_base[index]
        else:
            anchors.setdefault(next_anchor, []).insert(0, index)
    return anchors


def _merge_records(
    base: list,
    ours: list,
    theirs: list,
    where: str,
    conflicts: list[dict],
    identity: str,
) -> list:
    """Merge two lists of records aligned by *identity* (``Study_ID``/``dv_name``)."""
    base_keys = _identity_keys(base, identity)
    ours_keys = _identity_keys(ours, identity)
    theirs_keys = _identity_keys(theirs, identity)

    map_ours = _mapping(base_keys, ours_keys)
    map_theirs = _mapping(base_keys, theirs_keys)
    anchors_ours = _insertion_anchors(ours_keys, map_ours, len(base))
    anchors_theirs = _insertion_anchors(theirs_keys, map_theirs, len(base))

    out: list = []
    for index in range(len(base) + 1):
        _emit_insertions(
            out,
            ours, ours_keys, anchors_ours.get(index, []),
            theirs, theirs_keys, anchors_theirs.get(index, []),
            where, conflicts, identity,
        )
        if index == len(base):
            break

        in_ours, in_theirs = index in map_ours, index in map_theirs
        item_where = _item_path(where, identity, base_keys[index])

        if in_ours and in_theirs:
            merged = _merge_value(
                base[index], ours[map_ours[index]], theirs[map_theirs[index]],
                item_where, conflicts,
            )
            if merged is not _MISSING:
                out.append(merged)
        elif in_ours:
            if ours[map_ours[index]] == base[index]:
                continue                          # theirs deleted it, ours kept it
            conflicts.append(
                {
                    "where": item_where,
                    "base": base[index],
                    "ours": ours[map_ours[index]],
                    "theirs": DELETED,
                }
            )
            out.append(ours[map_ours[index]])
        elif in_theirs:
            if theirs[map_theirs[index]] == base[index]:
                continue                          # ours deleted it, theirs kept it
            conflicts.append(
                {
                    "where": item_where,
                    "base": base[index],
                    "ours": DELETED,
                    "theirs": theirs[map_theirs[index]],
                }
            )
            out.append(theirs[map_theirs[index]])
        # matched by neither side: deleted on both sides -> drop

    return out


def _emit_insertions(
    out: list,
    ours: list, ours_keys: list[tuple], ours_here: list[int],
    theirs: list, theirs_keys: list[tuple], theirs_here: list[int],
    where: str,
    conflicts: list[dict],
    identity: str,
) -> None:
    """Emit items inserted at one anchor; the same identity inserted on both
    sides is merged rather than duplicated."""
    theirs_by_key = {theirs_keys[i]: theirs[i] for i in theirs_here}
    emitted: set = set()

    for side_index in ours_here:
        key = ours_keys[side_index]
        item_where = _item_path(where, identity, key)
        if key in theirs_by_key:
            merged = _merge_value(
                _MISSING, ours[side_index], theirs_by_key[key], item_where, conflicts
            )
            if merged is not _MISSING:
                out.append(merged)
            emitted.add(key)
        else:
            out.append(ours[side_index])

    for side_index in theirs_here:
        key = theirs_keys[side_index]
        if key in emitted:
            continue
        out.append(theirs[side_index])


def _merge_positional(
    base: list, ours: list, theirs: list, where: str, conflicts: list[dict]
) -> list:
    """Merge record lists that carry no identity field, item by index."""
    out: list = []
    for index in range(max(len(base), len(ours), len(theirs))):
        in_b, in_o, in_t = index < len(base), index < len(ours), index < len(theirs)
        item_where = f"{where}[{index}]"

        if in_b and in_o and in_t:
            merged = _merge_value(base[index], ours[index], theirs[index], item_where, conflicts)
            if merged is not _MISSING:
                out.append(merged)
        elif in_o and in_t:                       # added on both sides
            merged = _merge_value(_MISSING, ours[index], theirs[index], item_where, conflicts)
            if merged is not _MISSING:
                out.append(merged)
        elif in_o:
            if in_b and ours[index] == base[index]:
                continue                          # theirs deleted it
            if in_b:
                conflicts.append(
                    {"where": item_where, "base": base[index], "ours": ours[index], "theirs": DELETED}
                )
            out.append(ours[index])
        elif in_t:
            if in_b and theirs[index] == base[index]:
                continue                          # ours deleted it
            if in_b:
                conflicts.append(
                    {"where": item_where, "base": base[index], "ours": DELETED, "theirs": theirs[index]}
                )
            out.append(theirs[index])
        # index missing everywhere but base: deleted on both sides -> drop
    return out


# --- text (line) merge -----------------------------------------------------

def _merge_text(path: str, base: bytes, ours: bytes, theirs: bytes) -> MergeResult:
    """Line-based diff3: change *hunks* are applied where they do not overlap.

    A hunk is ``(start, end, replacement_lines)`` over base line indices, taken
    from :class:`difflib.SequenceMatcher` (``autojunk=False``, because project
    files repeat lines a lot).  Hunks of the two sides that overlap — including
    two insertions at the same point — form one cluster; the cluster is resolved
    by rebuilding each side's version of the base segment and comparing:

    * both sides produced the same lines -> take it once;
    * one side produced the base segment -> take the other side;
    * both changed it differently -> conflict.
    """
    try:
        base_lines = _split_lines(base.decode("utf-8"))
        ours_lines = _split_lines(ours.decode("utf-8"))
        theirs_lines = _split_lines(theirs.decode("utf-8"))
    except UnicodeDecodeError:
        return _failed(path, base, ours, theirs)

    out: list[str] = []
    conflicts: list[dict] = []
    cursor = 0

    for cluster in _hunk_clusters(
        _change_hunks(base_lines, ours_lines),
        _change_hunks(base_lines, theirs_lines),
    ):
        start = min(hunk[0] for hunk in cluster)
        end = max(hunk[1] for hunk in cluster)
        if start < cursor:                        # pragma: no cover - defensive
            start = cursor
        out.extend(base_lines[cursor:start])

        ours_hunks = [hunk for hunk in cluster if hunk[3] == "ours"]
        theirs_hunks = [hunk for hunk in cluster if hunk[3] == "theirs"]
        base_seg = base_lines[start:end]
        ours_seg = _apply_hunks(base_lines, ours_hunks, start, end)
        theirs_seg = _apply_hunks(base_lines, theirs_hunks, start, end)

        if ours_seg == theirs_seg:
            out.extend(ours_seg)
        elif not ours_hunks:
            out.extend(theirs_seg)
        elif not theirs_hunks:
            out.extend(ours_seg)
        elif ours_seg == base_seg:
            out.extend(theirs_seg)
        elif theirs_seg == base_seg:
            out.extend(ours_seg)
        else:
            line = start + 1
            conflicts.append(
                {
                    "where": f"{path} (lines {line}-{line + len(base_seg) - 1})"
                    if base_seg
                    else f"{path} (at line {line})",
                    "base": "".join(base_seg),
                    "ours": "".join(ours_seg),
                    "theirs": "".join(theirs_seg),
                }
            )
        cursor = end

    out.extend(base_lines[cursor:])

    if conflicts:
        return MergeResult(ok=False, merged=None, conflicts=conflicts)
    return MergeResult(ok=True, merged="".join(out).encode("utf-8"), conflicts=[])


def _split_lines(text: str) -> list[str]:
    """Lines **with** their endings, so a merge keeps the file's newline style."""
    return text.splitlines(keepends=True)


def _change_hunks(base: list[str], side: list[str]) -> list[tuple[int, int, list[str]]]:
    """The side's changes as ``(base_start, base_end, replacement_lines)``."""
    matcher = difflib.SequenceMatcher(None, base, side, autojunk=False)
    return [
        (i1, i2, side[j1:j2])
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    ]


def _hunk_clusters(
    ours_hunks: list[tuple[int, int, list[str]]],
    theirs_hunks: list[tuple[int, int, list[str]]],
) -> list[list[tuple[int, int, list[str], str]]]:
    """Group overlapping hunks of both sides; touching-but-disjoint hunks stay
    separate so two edits on adjacent lines merge cleanly."""
    tagged = [(s, e, rep, "ours") for s, e, rep in ours_hunks]
    tagged += [(s, e, rep, "theirs") for s, e, rep in theirs_hunks]
    tagged.sort(key=lambda hunk: (hunk[0], hunk[1]))

    clusters: list[list[tuple[int, int, list[str], str]]] = []
    for hunk in tagged:
        if clusters and _overlaps(clusters[-1], hunk):
            clusters[-1].append(hunk)
        else:
            clusters.append([hunk])
    return clusters


def _overlaps(cluster: list[tuple[int, int, list[str], str]], hunk) -> bool:
    start = min(member[0] for member in cluster)
    end = max(member[1] for member in cluster)
    hunk_start, hunk_end = hunk[0], hunk[1]

    if hunk_start == hunk_end and start == end:   # two insertions at one point
        return hunk_start == start
    if hunk_start == hunk_end or start == end:    # insertion vs a changed range
        insertion = hunk_start if hunk_start == hunk_end else start
        low, high = (start, end) if start != end else (hunk_start, hunk_end)
        return low < insertion < high
    return hunk_start < end and start < hunk_end    # two non-empty ranges


def _apply_hunks(
    base: list[str], hunks: list[tuple[int, int, list[str], str]], start: int, end: int
) -> list[str]:
    """Rebuild one side's version of ``base[start:end]`` from its own hunks."""
    out: list[str] = []
    position = start
    for hunk_start, hunk_end, replacement, _side in hunks:
        out.extend(base[position:hunk_start])
        out.extend(replacement)
        position = hunk_end
    out.extend(base[position:end])
    return out


# --- helpers ---------------------------------------------------------------

def _extension(path: str) -> str:
    name = re.split(r"[/\\]", path)[-1]
    match = re.search(r"\.[^.]+$", name)
    return match.group(0).lower() if match else ""


def _failed(path: str, base: bytes | None, ours: bytes, theirs: bytes) -> MergeResult:
    """Whole-file conflict (binary, unknown type, missing base, unparsable JSON)."""
    return MergeResult(
        ok=False,
        merged=None,
        conflicts=[
            {
                "where": path,
                "base": _as_text(base),
                "ours": _as_text(ours),
                "theirs": _as_text(theirs),
            }
        ],
    )


def _as_text(value: bytes | None) -> str | None:
    if value is None:
        return None
    return value.decode("utf-8", "replace")


def _child_path(where: str, key) -> str:
    """JSON-path segment for a dict key: ``$.a`` for identifiers, ``$["a b"]``."""
    name = str(key)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return f"{where}.{name}"
    return f'{where}[{json.dumps(name, ensure_ascii=False)}]'


def _item_path(where: str, identity: str, key: tuple) -> str:
    """JSON-path segment for a record: ``[Study_ID="A"]`` / ``[dv_name="d"#2]``."""
    value, occurrence = key
    label = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if occurrence:
        return f'{where}[{identity}="{label}"#{occurrence + 1}]'
    return f'{where}[{identity}="{label}"]'
