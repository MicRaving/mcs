# -*- coding: utf-8 -*-
"""Coder-owned **variable assignments** for the harmonizer rework (interface §5).

A coding team can label / assign a raw study variable that the harmonizer left
unmapped or mis-decoded, without touching the 264 per-study harmonizers.  Every
decision lives in ONE coder-owned file per study:

    Datasets/<Key>/<Key> - variable_assignments.csv

and is applied to the generated harmonized products by :func:`apply`, which is
idempotent and never silently overwrites a non-blank study-code label.

Keying
------
Rows are keyed on the **raw side** ``(artifact, original_variable[, item_scope])``
-- *never* on the uniform name -- so an assignment survives a uniform rename and
sidecar regeneration.

    artifact         "main" | "1" | "*" (wildcard / unknown)
    original_variable the raw column name in the prepared data
    item_scope       optional sub-item qualifier (e.g. a column suffix)

Schema (:data:`ASSIGN_COLS`, status in {proposed, confirmed, conflict})
--------------------------------------------------------------------------
    artifact, original_variable, source_file, item_scope, uniform_variable,
    role, dv_domain, veracity, timepoint, dv_polarity, slot_fix, evidence,
    note, coder, decided_at, status

Apply semantics (:func:`apply`)
-------------------------------
* **Fills blanks only.**  A proposed row fills a blank sidecar field.
* **Confirmed overrides a decoded guess** (the study code's value), logged as an
  ``override`` record -- this is the only path that may change a non-blank label.
* **Conflicts are logged, never applied.**  A proposed row that disagrees with a
  non-blank label is recorded and the row's ``status`` is stamped ``conflict``.
* **slot_fix rows** patch sidecar fields only, with **no rename** (no wide edit).
  ``slot_fix`` may be a sidecar field name (``dv_polarity``) or a JSON object
  ``{"veracity": "false", "timepoint": "post"}``.
* **true/false split rows** (``slot_fix``/``role`` in
  ``{"veracity_split","split_veracity"}``) drive
  :func:`Scripts.dv_veracity_split.attach`.  The split spec is a JSON object (or
  ``key=value; ...`` pairs) in ``evidence`` (preferred) or ``note``:
  ``pid_col``, ``true_values``, ``false_values``, ``ren``.  ``source_file`` is
  the trial-level raw file and ``original_variable`` the item-veracity column.
  Splits are processed only when ``wide`` is supplied (the runner path), because
  they need the wide table; a standalone template run therefore honours label
  assignments but leaves splits to the runner post-hook.
* **Unmapped raw columns** with a ``uniform_variable`` and ``role`` are added as
  new sidecar rows (and renamed in the wide when the raw column is present).

Every applied fill/override/conflict/add/split is appended to
``Results/variable_assignment_log.jsonl``; a re-apply is a no-op (no records, no
log lines, no data change).

The module optionally imports ``Scripts/contract.py`` (authored by the
coordination workstream) for shared decoders and **never fails** when it is
absent.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import sys
import time

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# --- shared contract (defensive: authored by another workstream) ------------
try:  # pragma: no cover - import shape depends on the parallel workstream
    import contract as _contract
    HAS_CONTRACT = True
except Exception:  # noqa: BLE001  (must not fail hard when absent)
    _contract = None
    HAS_CONTRACT = False

try:  # atomic replace (Windows/Nextcloud locks)
    import safe_replace as _safe_replace
    _HAVE_SAFE_REPLACE = True
except Exception:  # noqa: BLE001
    _safe_replace = None
    _HAVE_SAFE_REPLACE = False


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
ASSIGN_COLS = ["artifact", "original_variable", "source_file", "item_scope",
               "uniform_variable", "role", "dv_domain", "veracity", "timepoint",
               "dv_polarity", "slot_fix", "evidence", "note", "coder",
               "decided_at", "status"]
ASSIGN_FILE = "<folder>/<Key> - variable_assignments.csv"
STATUSES = ("proposed", "confirmed", "conflict")

LOG_PATH = os.path.join(ROOT, "Results", "variable_assignment_log.jsonl")
ARCHIVE_DIR = os.path.join(ROOT, "archive", "ws_b3_backup_2026-09-22")
TMP_DIR = os.path.join(ROOT, "temp", "ws_b3")


def _log_path() -> str:
    """Audit-log path (``VARIABLE_ASSIGNMENT_LOG`` overrides for tests)."""
    return os.environ.get("VARIABLE_ASSIGNMENT_LOG", LOG_PATH)


def _archive_dir() -> str:
    return os.environ.get("VARIABLE_ASSIGNMENT_ARCHIVE", ARCHIVE_DIR)

#: sidecar fields an assignment may set (slot_fix rows may patch any of them)
LABEL_FIELDS = ("uniform_variable", "role", "dv_domain", "veracity", "timepoint",
                "dv_polarity")

#: role values meaning "not applicable" (coverage turns these into status `na`)
NA_ROLES = {"na", "n/a", "not_applicable", "not applicable", "none", "notapplicable"}
NA_ROLE_CANON = "na"

#: slot_fix / role values that select the true/false split path
SPLIT_FIXES = {"veracity_split", "split_veracity", "veracity-split", "split",
               "split_true_false"}

_BLANK = {"", "nan", "none", "nat", "<na>"}

RECORD_COLS = ["action", "artifact", "original_variable", "item_scope",
               "uniform_variable", "field", "old", "new", "status", "coder",
               "evidence", "note"]


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def is_blank(v) -> bool:
    """True for the conventional blank sentinels (also for NaN)."""
    if v is None:
        return True
    try:
        if pd.isna(v):
            return True
    except (TypeError, ValueError):
        pass
    return str(v).strip().lower() in _BLANK


def _s(v) -> str:
    # pandas parses a `veracity` column of "true"/"false" into Python booleans;
    # canonicalise back to the lowercase tokens the pipeline uses.
    if isinstance(v, bool):
        return "true" if v else "false"
    if type(v).__name__ == "bool_":  # numpy.bool_
        return "true" if bool(v) else "false"
    return "" if is_blank(v) else str(v).strip()


def _norm_artifact(a) -> str:
    s = _s(a)
    if s.lower() in ("", "*", "all", "any"):
        return "*"
    return s


def _norm_key(artifact, original, item_scope="") -> tuple:
    return (_norm_artifact(artifact), _s(original), _s(item_scope))


def _key_of_row(r) -> tuple:
    return _norm_key(r.get("artifact"), r.get("original_variable"),
                     r.get("item_scope"))


def _is_split_row(r) -> bool:
    sf = _s(r.get("slot_fix")).lower()
    role = _s(r.get("role")).lower()
    return sf in SPLIT_FIXES or role in SPLIT_FIXES


def _is_na_row(r) -> bool:
    return _s(r.get("role")).lower() in NA_ROLES


def _read_csv(path):
    """Read a CSV with project conventions (utf-8-sig, de-BOM-ed header)."""
    df = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    df.columns = [str(c).strip().lstrip("\ufeff") for c in df.columns]
    return df


def _file_hash(path):
    try:
        h = hashlib.md5()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _ensure_dirs():
    for d in (os.path.dirname(_log_path()), _archive_dir(), TMP_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass


def _atomic_csv(df, path):
    """Write ``df`` to ``path`` atomically (temp file + safe_replace)."""
    _ensure_dirs()
    tmp = os.path.join(TMP_DIR, f".__tmp_{os.getpid()}_{int(time.time()*1000)}.csv")
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    if _HAVE_SAFE_REPLACE:
        _safe_replace.safe_replace(tmp, path)
    else:
        os.replace(tmp, path)


def _append_jsonl(records):
    if not records:
        return
    _ensure_dirs()
    with open(_log_path(), "a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")


def _archive_existing(path, key):
    if not (os.path.isfile(path) and os.path.getsize(path) > 0):
        return ""
    try:
        adir = _archive_dir()
        os.makedirs(adir, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        base = f"{key} - variable_assignments {ts}.csv"
        dst = os.path.join(adir, base)
        i = 1
        while os.path.exists(dst):
            dst = os.path.join(adir, base.replace(".csv", f" ({i}).csv"))
            i += 1
        shutil.copy2(path, dst)
        return dst
    except OSError:
        return ""


def _empty_assignments():
    return pd.DataFrame(columns=ASSIGN_COLS)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def assign_path(key, folder) -> str:
    return os.path.join(folder, f"{key} - variable_assignments.csv")


def load(key, folder):
    """Load a study's assignments as a :class:`pandas.DataFrame`.

    Missing file -> header-only frame.  Missing columns are filled with ``""``
    and every value is returned as a stripped string, so callers can rely on
    :data:`ASSIGN_COLS`.
    """
    path = assign_path(key, folder)
    if not os.path.isfile(path):
        return _empty_assignments()
    try:
        df = _read_csv(path)
    except Exception:  # noqa: BLE001
        return _empty_assignments()
    for c in ASSIGN_COLS:
        if c not in df.columns:
            df[c] = ""
    out = df[ASSIGN_COLS].copy()
    for c in ASSIGN_COLS:
        out[c] = out[c].map(_s)
    return out


def write_rows(key, folder, rows) -> int:
    """Atomic upsert of ``rows`` into the study's assignments CSV.

    Existing rows whose key does not appear in ``rows`` are preserved; a row
    with the same ``(artifact, original_variable, item_scope)`` is replaced.
    The previous file is archived to ``archive/ws_b3_backup_2026-09-22/`` and
    every written row is appended to ``Results/variable_assignment_log.jsonl``.
    Returns the total number of rows now in the file.
    """
    if rows is None:
        rows = []
    if isinstance(rows, pd.DataFrame):
        new = rows.copy()
    else:
        new = pd.DataFrame(list(rows))
    if new.empty and not len(new.columns):
        new = _empty_assignments()
    for c in ASSIGN_COLS:
        if c not in new.columns:
            new[c] = ""
    new = new[ASSIGN_COLS].copy()
    for c in ASSIGN_COLS:
        new[c] = new[c].map(_s)

    existing = load(key, folder)
    incoming = {_key_of_row(r) for _, r in new.iterrows()}
    if len(existing):
        keep = existing[[_key_of_row(r) not in incoming
                         for _, r in existing.iterrows()]]
    else:
        keep = existing
    out = pd.concat([keep, new], ignore_index=True)[ASSIGN_COLS]

    path = assign_path(key, folder)
    archived = _archive_existing(path, key)
    _atomic_csv(out, path)
    _append_jsonl([{"event": "write_rows", "key": key, "ts": time.time(),
                    "archived": archived, **{c: r.get(c, "") for c in ASSIGN_COLS}}
                   for _, r in new.iterrows()])
    return len(out)


def _match_indices(side, artifact, original, item_scope):
    """Sidecar row labels matching the raw key (artifact wildcards allowed)."""
    if "original_variable" not in side.columns or not original:
        return []
    orig = side["original_variable"].map(_s)
    m = orig == original
    if "artifact" in side.columns:
        sa = side["artifact"].map(_norm_artifact)
        a = _norm_artifact(artifact)
        m &= (sa == a) | (a == "*") | (sa == "*")
    if item_scope and "item_scope" in side.columns:
        m &= side["item_scope"].map(_s) == item_scope
    return list(side.index[m])


def _side_label_cols(side):
    cols = ["uniform_variable", "role", "dv_domain", "veracity", "timepoint",
            "dv_polarity", "original_variable", "notes"]
    for c in cols:
        if c not in side.columns:
            side[c] = ""
        else:
            # force object dtype: pandas may have parsed a veracity column of
            # "true"/"false" into bool, which cannot hold a string patch.
            side[c] = side[c].astype(object)
    return side


def _rec(action, key, field="", old="", new="", status="", coder="", evidence="",
         note="", uniform=""):
    return {"action": action, "artifact": key[0], "original_variable": key[1],
            "item_scope": key[2], "uniform_variable": uniform, "field": field,
            "old": _s(old), "new": _s(new), "status": status, "coder": _s(coder),
            "evidence": _s(evidence), "note": _s(note)}


def _effective_fields(group):
    """Resolve one field value per raw key; ``confirmed`` rows win.

    Returns ``{field: (value, status, row)}``.  Processing proposed rows first
    and confirmed rows last makes the confirmed decision authoritative.
    """
    rows = list(group.iterrows())
    rows.sort(key=lambda kv: 1 if _s(kv[1].get("status")).lower() == "confirmed" else 0)
    eff = {}
    for _, r in rows:
        st = _s(r.get("status")).lower()
        if st == "conflict":
            continue
        for f in LABEL_FIELDS:
            v = _s(r.get(f))
            if v == "":
                continue
            if f == "role" and v.lower() in NA_ROLES:
                # an N/A verdict is not written into the sidecar `role`
                eff[f] = (NA_ROLE_CANON, st, r)
                continue
            eff[f] = (v, st, r)
    return eff


def _patch_sidecar(side, i, field, value, status, row, key, records):
    """Fill / override / conflict for one sidecar cell.  Never silent."""
    coder = row.get("coder", "")
    evidence = row.get("evidence", "")
    note = row.get("note", "")
    if field == "role" and _s(value).lower() in NA_ROLES:
        records.append(_rec("na", key, field, _s(side.at[i, field]), "",
                            status, coder, evidence, note))
        return False
    cur = _s(side.at[i, field]) if field in side.columns else ""
    if cur == "":
        side.at[i, field] = value
        records.append(_rec("fill", key, field, cur, value, status, coder,
                            evidence, note, uniform=_s(side.at[i, "uniform_variable"])
                            if "uniform_variable" in side.columns else ""))
        return True
    if cur == value:
        return False  # already applied -> idempotent no-op
    if status == "confirmed":
        side.at[i, field] = value
        records.append(_rec("override", key, field, cur, value, status, coder,
                            evidence, note, uniform=_s(side.at[i, "uniform_variable"])
                            if "uniform_variable" in side.columns else ""))
        return True
    records.append(_rec("conflict", key, field, cur, value, "conflict", coder,
                        evidence, note, uniform=_s(side.at[i, "uniform_variable"])
                        if "uniform_variable" in side.columns else ""))
    return "conflict"


def _rename_uniform(side, i, new_name, status, row, key, wide, records):
    """Handle the ``uniform_variable`` label (rename only when confirmed+free)."""
    cur = _s(side.at[i, "uniform_variable"])
    coder = row.get("coder", "")
    evidence = row.get("evidence", "")
    note = row.get("note", "")
    if cur == "":
        side.at[i, "uniform_variable"] = new_name
        records.append(_rec("fill", key, "uniform_variable", cur, new_name,
                            status, coder, evidence, note, uniform=new_name))
        return True, wide
    if cur == new_name:
        return False, wide
    if status != "confirmed":
        records.append(_rec("conflict", key, "uniform_variable", cur, new_name,
                            "conflict", coder, evidence, note, uniform=cur))
        return "conflict", wide
    taken = set(side["uniform_variable"].map(_s)) if "uniform_variable" in side.columns else set()
    taken.discard(cur)
    if new_name in taken:
        records.append(_rec("conflict", key, "uniform_variable", cur, new_name,
                            "conflict", coder, evidence, note, uniform=cur))
        return "conflict", wide
    side.at[i, "uniform_variable"] = new_name
    if wide is not None:
        for suf in ("", "_01"):
            if cur + suf in wide.columns and new_name + suf not in wide.columns:
                wide = wide.rename(columns={cur + suf: new_name + suf})
    records.append(_rec("override", key, "uniform_variable", cur, new_name,
                        status, coder, evidence, note, uniform=new_name))
    return True, wide


def _parse_kv(text):
    """Parse a JSON object or ``key=value; key=value`` pairs into a dict."""
    out = {}
    s = _s(text)
    if not s:
        return out
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                return {str(k): v for k, v in obj.items()}
        except (ValueError, TypeError):
            pass
    for part in re.split(r"[;\n]", s):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in re.split(r"[,|]", str(v)) if x.strip()]


def _split_sources(rows, folder):
    """Build ``dv_veracity_split.attach`` sources from split assignment rows."""
    sources = []
    for _, r in rows.iterrows():
        spec = {}
        spec.update(_parse_kv(r.get("note")))
        spec.update(_parse_kv(r.get("evidence")))
        path = _s(r.get("source_file")) or _s(spec.get("path"))
        if not path:
            continue
        if not os.path.isabs(path):
            cand = os.path.join(folder, path)
            path = cand if os.path.exists(cand) else path
        vc = _s(r.get("original_variable")) or _s(spec.get("veracity_col"))
        if not vc:
            continue
        src = {
            "path": path,
            "artifact": _s(r.get("artifact")) or _s(spec.get("artifact")) or "main",
            "pid_col": _s(spec.get("pid_col")) or "pid",
            "veracity_col": vc,
            "true_values": _as_list(spec.get("true_values")) or ["1", "true", "real"],
            "false_values": _as_list(spec.get("false_values")) or ["0", "-1", "false", "fake"],
        }
        ren = spec.get("ren")
        if isinstance(ren, str) and ren.strip().startswith("{"):
            try:
                ren = json.loads(ren)
            except (ValueError, TypeError):
                ren = None
        if isinstance(ren, dict) and ren:
            src["ren"] = ren
        sources.append(src)
    return sources


def _expected_split_cols(sources):
    """The `dv_*_true` / `dv_*_false` columns a split would produce."""
    cols = []
    for s in sources:
        try:
            hdr = pd.read_csv(s["path"], nrows=0, encoding="utf-8-sig")
        except Exception:  # noqa: BLE001
            continue
        for c in hdr.columns:
            if str(c).startswith("dv_"):
                cols.extend([str(c) + "_true", str(c) + "_false"])
    return cols


def _split_already_applied(side, wide, sources):
    """True when every expected split column is present in the wide + sidecar.

    ``dv_veracity_split.attach`` is idempotent in *values* but not in column
    order (it re-appends the split columns), so re-running it would rewrite the
    files and defeat apply's no-op guarantee.  Presence is the safe guard.
    """
    need = _expected_split_cols(sources)
    if not need or wide is None:
        return False
    have_w = {str(c) for c in wide.columns}
    have_s = ({str(v) for v in side["uniform_variable"]}
              if "uniform_variable" in side.columns else set())
    return all(c in have_w and c in have_s for c in need)


def _handle_splits(key, folder, side, wide, assignments, records):
    """Drive ``dv_veracity_split.attach`` for true/false split rows.

    Only runs when ``wide`` is supplied (runner path), the split is not already
    present, and the attach produced a real change (content-hash guard ->
    idempotent re-apply)."""
    split_rows = assignments[[_is_split_row(r) for _, r in assignments.iterrows()]]
    if split_rows.empty or wide is None:
        return side, wide
    sources = _split_sources(split_rows, folder)
    if not sources:
        return side, wide
    if _split_already_applied(side, wide, sources):
        return side, wide
    try:
        import dv_veracity_split as _split
    except Exception:  # noqa: BLE001
        records.append(_rec("split_skipped", _norm_key("", ""), "veracity_split",
                            note="dv_veracity_split not importable"))
        return side, wide
    side_path = os.path.join(folder, f"{key} - harmonized_variables.csv")
    wide_path = os.path.join(folder, f"{key} - harmonized.csv")
    before = (_file_hash(side_path), _file_hash(wide_path))
    try:
        n_new, n_src = _split.attach(key, folder, sources)
    except Exception as e:  # noqa: BLE001
        records.append(_rec("split_failed", _norm_key("", ""), "veracity_split",
                            note=repr(e)))
        return side, wide
    after = (_file_hash(side_path), _file_hash(wide_path))
    if n_new and after != before and os.path.isfile(side_path) and os.path.isfile(wide_path):
        side = _read_csv(side_path)
        wide = _read_csv(wide_path)
        records.append(_rec("split", _norm_key("", ""), "veracity_split",
                            old="", new=f"{n_new} columns x {n_src} sources",
                            status="confirmed"))
    return side, wide


def apply(key, folder, side, wide):
    """Apply ``<Key> - variable_assignments.csv`` to ``(side, wide)``.

    Returns ``(side, wide, records)``.  Fills blanks, applies ``confirmed`` rows
    over decoded guesses, logs conflicts (never silently overwriting), supports
    ``slot_fix`` sidecar patches and true/false split rows.  Idempotent: a second
    call changes nothing and returns no records.
    """
    records = []
    if side is None:
        return side, wide, records
    assignments = load(key, folder)
    if assignments.empty:
        return side, wide, records

    side, wide = _handle_splits(key, folder, side, wide, assignments, records)
    side = _side_label_cols(side)

    conflict_keys = []
    assignments = assignments.copy()
    assignments["_key"] = [_key_of_row(r) for _, r in assignments.iterrows()]
    for key_tuple, group in assignments.groupby("_key", sort=False):
        artifact, original, item_scope = key_tuple
        rkey = _norm_key(artifact, original, item_scope)
        if not original:
            continue
        if all(_is_split_row(r) for _, r in group.iterrows()):
            continue
        eff = _effective_fields(group)
        if not eff:
            continue
        targets = _match_indices(side, artifact, original, item_scope)
        if targets:
            conflicted = False
            for field, (value, status, row) in eff.items():
                if field == "uniform_variable":
                    res, wide = _rename_uniform(side, targets[0], value, status,
                                                row, rkey, wide, records)
                    conflicted = conflicted or res == "conflict"
                else:
                    res = _patch_sidecar(side, targets[0], field, value, status,
                                         row, rkey, records)
                    conflicted = conflicted or res == "conflict"
            if conflicted:
                conflict_keys.append(rkey)
        else:
            # unmapped raw column: add a sidecar row when we have name + role
            uv = eff.get("uniform_variable", ("", "", None))[0]
            role = eff.get("role", ("", "", None))[0]
            if uv and role and role != NA_ROLE_CANON:
                rep = eff.get("uniform_variable", eff.get("role"))[2]
                new_row = {c: "" for c in side.columns}
                for f in LABEL_FIELDS:
                    if f in eff:
                        new_row[f] = eff[f][0]
                new_row["original_variable"] = original
                if "artifact" in side.columns and artifact != "*":
                    new_row["artifact"] = artifact
                if "item_scope" in side.columns:
                    new_row["item_scope"] = item_scope
                side = pd.concat([side, pd.DataFrame([new_row])], ignore_index=True)
                if wide is not None and original in wide.columns and uv != original:
                    wide = wide.rename(columns={original: uv})
                    if original + "_01" in wide.columns and uv + "_01" not in wide.columns:
                        wide = wide.rename(columns={original + "_01": uv + "_01"})
                records.append(_rec("add_row", rkey, "", "", uv,
                                    _s(rep.get("status")), _s(rep.get("coder")),
                                    _s(rep.get("evidence")),
                                    note="added sidecar row for unmapped raw column"))
            else:
                records.append(_rec("stale", rkey, "", "", uv,
                                    note="no matching sidecar row and no assignable "
                                         "uniform_variable/role"))

    # Stamp conflicts back so a re-apply is a strict no-op (never silent).
    if conflict_keys:
        patched = assignments.copy()
        hit = False
        for idx, r in patched.iterrows():
            if _key_of_row(r) in conflict_keys and _s(r.get("status")).lower() != "confirmed":
                patched.at[idx, "status"] = "conflict"
                hit = True
        if hit:
            path = assign_path(key, folder)
            _archive_existing(path, key)
            _atomic_csv(patched[ASSIGN_COLS], path)

    _append_jsonl([{"event": "apply", "key": key, "ts": time.time(), **r}
                   for r in records])
    return side, wide, records


def apply_files(key, folder):
    """Read -> :func:`apply` -> write back the study's harmonized products.

    Convenience for the runner post-hook: applies the assignments to the files
    on disk (atomically) and returns the records.  Writes only when a
    data-changing record (fill / override / add_row / split) was produced, so a
    no-op re-apply touches nothing.  Returns ``[]`` when there is no sidecar.
    """
    side_path = os.path.join(folder, f"{key} - harmonized_variables.csv")
    wide_path = os.path.join(folder, f"{key} - harmonized.csv")
    if not os.path.isfile(side_path):
        return []
    side = _read_csv(side_path)
    wide = _read_csv(wide_path) if os.path.isfile(wide_path) else None
    side, wide, records = apply(key, folder, side, wide)
    if any(r.get("action") in ("fill", "override", "add_row", "split")
           for r in records):
        _atomic_csv(side, side_path)
        if wide is not None:
            _atomic_csv(wide, wide_path)
    return records


def read_verified(key, folder):
    """Convenience: the subset of assignments considered authoritative.

    ``confirmed`` rows plus any non-conflict ``proposed`` rows (used by the
    coverage matrix).  Never raises.
    """
    a = load(key, folder)
    if a.empty:
        return a
    return a[a["status"].map(lambda s: _s(s).lower() != "conflict")].copy()


# ---------------------------------------------------------------------------
# tiny CLI (read-only report)
# ---------------------------------------------------------------------------
def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Report a study's variable assignments.")
    ap.add_argument("--key", required=True)
    ap.add_argument("--datasets", default=os.path.join(ROOT, "Datasets"))
    args = ap.parse_args(argv)
    folder = os.path.join(args.datasets, args.key)
    df = load(args.key, folder)
    print(f"{args.key}: {len(df)} assignment row(s) -> {assign_path(args.key, folder)}")
    if len(df):
        print(df.to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
