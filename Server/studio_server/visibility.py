"""Who may read and write a project file (INTERFACES §3.5).

Two pure predicates over the ``FileClass`` answer from
``online_shared.protocol.classify``:

* :func:`can_read` — may this member *see* this path?  The owner sees
  everything except ``excluded``; a coder sees its own tag's files; another
  tag's file is hidden while the stage is blinded, except a coding file whose
  key the member has marked ``done(key, "coding")`` for one of its tags.
  Shared/admin/derived/immutable files are readable by every member
  (viewers included).
* :func:`can_write` — may this member *change* it?  Excluded is never
  writable, derived/admin/immutable are owner-only, an owned file needs its
  tag, and a shared file needs the write task plus an unheld (or own, or
  expired) lock; ``settle`` (``Extraction/``) additionally needs
  ``done(key, "coding")``.

Both functions are I/O-free so the server and the tests can call them with
in-memory values.  :func:`write_denial` is the endpoint-facing variant: it
returns ``None`` or the push reason (``"forbidden"`` / ``"locked"``) so the
sync API can report the exact rejection.
"""

from __future__ import annotations

from typing import Any, Mapping

from .db import utcnow

#: Tag that matches every owned file (owners carry it; see ``db.get_member``).
ANY_TAG = "*"

#: Roles / kinds the rules refer to by name.
OWNER_ROLE = "owner"
EXCLUDED = "excluded"
OWNED = "owned"
SHARED = "shared"
OWNER_ONLY_KINDS = ("derived", "admin", "immutable")
BLINDED = "blinded"
VISIBLE = "visible"


# ── member helpers ─────────────────────────────────────────────────────


def is_owner(member: Mapping[str, Any] | None) -> bool:
    """True for the project owner (a server admin is an effective owner)."""
    return bool(member) and str(member.get("role") or "") == OWNER_ROLE


def member_tags(member: Mapping[str, Any] | None) -> list[str]:
    """The member's coder tags (never ``None``)."""
    if not member:
        return []
    tags = member.get("tags") or []
    return [str(tag) for tag in tags]


def member_tasks(member: Mapping[str, Any] | None) -> list[str]:
    """The member's write tasks (never ``None``)."""
    if not member:
        return []
    tasks = member.get("tasks") or []
    return [str(task) for task in tasks]


def has_tag(member: Mapping[str, Any] | None, tag: str | None) -> bool:
    """Owner matches every tag; ``"*"`` in ``tags`` matches every tag."""
    if is_owner(member):
        return True
    if tag is None:
        return False
    tags = member_tags(member)
    return ANY_TAG in tags or str(tag) in tags


def has_task(member: Mapping[str, Any] | None, task: str | None) -> bool:
    """Owner passes every task check; other roles need the task in ``tasks``."""
    if is_owner(member):
        return True
    if task is None:
        return False
    return str(task) in member_tasks(member)


# ── done marks ─────────────────────────────────────────────────────────


def done_set(done_tags_keys: Any) -> set[tuple[str, str]]:
    """Normalise the ``(tag, key)`` done marks to a set of pairs.

    Accepted shapes (all equivalent): ``None``, a set/list of
    ``(tag, key)`` pairs, or a mapping ``{tag: [key, ...]}``.  The server
    passes a set; the mapping form makes tests and callers readable.
    """
    if not done_tags_keys:
        return set()
    if isinstance(done_tags_keys, Mapping):
        pairs: set[tuple[str, str]] = set()
        for tag, keys in done_tags_keys.items():
            if isinstance(keys, str):
                pairs.add((str(tag), keys))
                continue
            for key in keys or ():
                pairs.add((str(tag), str(key)))
        return pairs
    pairs = set()
    for item in done_tags_keys:
        try:
            tag, key = item
        except (TypeError, ValueError):
            continue
        pairs.add((str(tag), str(key)))
    return pairs


def done_for(done_tags_keys: Any, tag: str, key: str | None) -> bool:
    """True when *tag* has marked ``done(key, "coding")``."""
    if key is None:
        return False
    return (str(tag), str(key)) in done_set(done_tags_keys)


def has_done_for_member(member: Mapping[str, Any] | None, done_tags_keys: Any, key: str | None) -> bool:
    """True when **any** of the member's tags has finished *key*."""
    if key is None:
        return False
    done = done_set(done_tags_keys)
    if not done:
        return False
    return any((tag, str(key)) in done for tag in member_tags(member))


# ── settings / locks ───────────────────────────────────────────────────


def stage_blinding(settings: Mapping[str, Any] | None, stage: str | None) -> str:
    """``"blinded"`` / ``"visible"`` for *stage* (unknown stages are blinded)."""
    if not settings or not stage:
        return BLINDED
    blinding = settings.get("blinding")
    if not isinstance(blinding, Mapping):
        return BLINDED
    value = blinding.get(stage)
    return VISIBLE if value == VISIBLE else BLINDED


def is_blinded(settings: Mapping[str, Any] | None, stage: str | None) -> bool:
    return stage_blinding(settings, stage) != VISIBLE


def lock_expired(lock: Mapping[str, Any] | None, now: str | None = None) -> bool:
    """True when *lock* is absent, carries no expiry, or has expired at *now*.

    Timestamps are the fixed-width UTC strings of :func:`db.utcnow`, so a plain
    string comparison is a time comparison.  A missing/invalid expiry counts
    as expired: a lock that cannot be shown live must not block a writer.
    """
    if not lock:
        return True
    expires = lock.get("expires")
    if not expires:
        return True
    return str(expires) <= str(now or utcnow())


def lock_allows(
    lock: Mapping[str, Any] | None,
    member: Mapping[str, Any] | None,
    now: str | None = None,
) -> bool:
    """The §3.5 lock rule: no lock, own lock, or an expired lock permits a write."""
    if not lock:
        return True
    if member and lock.get("user_id") is not None:
        try:
            if int(lock["user_id"]) == int(member.get("user_id")):
                return True
        except (TypeError, ValueError):
            pass
    return lock_expired(lock, now)


# ── read / write ───────────────────────────────────────────────────────


def can_read(
    member: Mapping[str, Any] | None,
    fc: Any,
    done_tags_keys: Any = None,
    settings: Mapping[str, Any] | None = None,
) -> bool:
    """May *member* read the file classified as *fc*? (INTERFACES §3.5)

    ``done_tags_keys`` carries the member's ``done(key, "coding")`` marks as
    ``(tag, key)`` pairs.  ``settings`` is the project settings object (only
    ``settings["blinding"][stage]`` is read).
    """
    if member is None or fc is None:
        return False
    kind = getattr(fc, "kind", None)
    if kind == EXCLUDED:
        return False
    if is_owner(member):
        return True

    if kind == OWNED:
        tag = getattr(fc, "tag", None)
        if has_tag(member, tag):
            return True
        stage = getattr(fc, "stage", None)
        if not is_blinded(settings, stage):
            return True
        if stage == "coding":
            return has_done_for_member(member, done_tags_keys, getattr(fc, "key", None))
        # abstract / fulltext whole-stage files carry no key: nothing can
        # unblind them for a single coder while the stage is blinded.
        return False

    # shared / admin / derived / immutable: every member (viewer too) reads.
    return True


def write_denial(
    member: Mapping[str, Any] | None,
    fc: Any,
    done_tags_keys: Any = None,
    lock: Mapping[str, Any] | None = None,
    now: str | None = None,
) -> str | None:
    """Why may this write not happen?  ``None`` = allowed, else the push reason.

    The reason vocabulary is the one ``POST /push`` reports (§3.6):
    ``"forbidden"`` for a class/tag/task/settle denial, ``"locked"`` when
    another user holds an unexpired lock.
    """
    if member is None or fc is None:
        return "forbidden"
    kind = getattr(fc, "kind", None)
    if kind == EXCLUDED:
        return "forbidden"

    if kind == OWNED:
        return None if has_tag(member, getattr(fc, "tag", None)) else "forbidden"

    if kind in OWNER_ONLY_KINDS:
        return None if is_owner(member) else "forbidden"

    if kind == SHARED:
        if not has_task(member, getattr(fc, "task", None)):
            return "forbidden"
        if not lock_allows(lock, member, now):
            return "locked"
        if getattr(fc, "task", None) == "settle" and not is_owner(member):
            if not has_done_for_member(member, done_tags_keys, getattr(fc, "key", None)):
                return "forbidden"
        return None

    return "forbidden"


def can_write(
    member: Mapping[str, Any] | None,
    fc: Any,
    done_tags_keys: Any = None,
    lock: Mapping[str, Any] | None = None,
    now: str | None = None,
) -> bool:
    """May *member* write the file classified as *fc*? (INTERFACES §3.5)"""
    return write_denial(member, fc, done_tags_keys, lock, now) is None


__all__ = [
    "ANY_TAG",
    "can_read",
    "can_write",
    "done_for",
    "done_set",
    "has_done_for_member",
    "has_tag",
    "has_task",
    "is_blinded",
    "is_owner",
    "lock_allows",
    "lock_expired",
    "member_tags",
    "member_tasks",
    "stage_blinding",
    "write_denial",
]
