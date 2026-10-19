# -*- coding: utf-8 -*-
"""D10 — import the reproduction facts/results/logs from volatile ``temp/`` into durable homes.

The reproduction run used to keep its only machine-readable numbers in ``temp/``
(``<Key> - repro_results.csv``, ``<Key> - repro_facts.json`` and the run logs under
``temp/reproduce_logs/``).  ``temp/`` is volatile by contract, so those numbers were
deleted.  This script copies them to their durable homes:

  * ``<Key> - repro_results.csv``  -> ``Datasets/<Key>/<Key> - repro_results.csv``
  * ``<Key> - repro_facts.json``   -> ``Datasets/<Key>/<Key> - repro_facts.json``
  * ``temp/reproduce_logs/*``      -> ``Results/repro_logs/<safe_slug>.log``

It scans both the project ``temp/`` and the parent ``../temp`` (the owner's restored
files may sync there first), is idempotent (byte-identical destinations are skipped),
never deletes the source, and appends one JSONL record per file to
``Results/persist_repro_log.jsonl``.  Tolerant of an empty ``temp/``: it then reports
"nothing to import" and exits 0, so it can be re-run once Nextcloud finishes syncing.

Run:  python Scripts/persist_repro_artifacts.py
      python Scripts/persist_repro_artifacts.py --dry-run
"""
import argparse
import hashlib
import json
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))          # Scripts
ROOT = os.path.dirname(_HERE)                                # project root
DS = os.path.join(ROOT, "Datasets")
RESULTS = os.path.join(ROOT, "Results")
TEMP = os.path.join(ROOT, "temp")
PARENT_TEMP = os.path.abspath(os.path.join(ROOT, os.pardir, "temp"))
DURABLE_LOGS = os.path.join(RESULTS, "repro_logs")
LOG_JSONL = os.path.join(RESULTS, "persist_repro_log.jsonl")

sys.path.insert(0, os.path.join(ROOT, "Scripts", "analysis"))
try:
    import run_reproducers as RR       # safe_slug / legacy_slug / log naming (single source)
except Exception:                       # pragma: no cover - import must not be fatal
    RR = None


def _sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_atomic(src, dst):
    """Copy src->dst via a temp file + os.replace; return 'copied'|'same'|'missing'."""
    if not os.path.isfile(src):
        return "missing"
    if os.path.isfile(dst):
        try:
            if _sha1(src) == _sha1(dst):
                return "same"
        except OSError:
            pass
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".tmp"
    with open(src, "rb") as a, open(tmp, "wb") as b:
        for chunk in iter(lambda: a.read(1 << 20), b""):
            b.write(chunk)
    try:
        from safe_replace import safe_replace
        safe_replace(tmp, dst)
    except Exception:
        os.replace(tmp, dst)
    return "copied"


def _would_copy(src, dst):
    """Compare without writing: 'missing' | 'same' | 'copy'."""
    if not os.path.isfile(src):
        return "missing"
    if os.path.isfile(dst):
        try:
            if _sha1(src) == _sha1(dst):
                return "same"
        except OSError:
            pass
    return "copy"


def _dataset_keys():
    if not os.path.isdir(DS):
        return []
    return sorted(d for d in os.listdir(DS)
                  if os.path.isdir(os.path.join(DS, d)) and not d.startswith("_"))


def _temp_dirs():
    out = []
    for d in (TEMP, PARENT_TEMP):
        if os.path.isdir(d) and os.path.abspath(d) not in (os.path.abspath(x) for x in out):
            out.append(d)
    return out


def _log_key_map(keys):
    """filename stem -> study key for the log files written by run_reproducers."""
    m = {}
    if RR is not None:
        for k in keys:
            m[RR.safe_slug(k)] = k
            m[RR.legacy_slug(k)] = k
    return m


def _results_alias_map(keys):
    """stem -> key for ``* - repro_results.csv`` names that are not the canonical key.

    Two bespoke scripts write ``Martinez2024`` / ``LR23``; map those back to the study
    whose reproduction script references the filename.
    """
    import re
    alias = {}
    for k in keys:
        script = os.path.join(DS, k, k + " - reproduction.py")
        if not os.path.isfile(script):
            continue
        try:
            with open(script, encoding="utf-8", errors="replace") as fh:
                txt = fh.read()
        except OSError:
            continue
        for m in re.finditer(r"([^\"'\s/\\]+) - repro_results\.csv", txt):
            alias.setdefault(m.group(1), k)
    return alias


def scan(dry_run=False):
    keys = _dataset_keys()
    key_set = set(keys)
    log_map = _log_key_map(keys)
    alias_map = _results_alias_map(keys)
    records = []
    seen = set()          # (kind, key/durable-name) -> avoid re-copying from the parent temp

    for temp_dir in _temp_dirs():
        # ---- per-study facts + results ------------------------------------
        try:
            names = sorted(os.listdir(temp_dir))
        except OSError:
            names = []
        for fn in names:
            src = os.path.join(temp_dir, fn)
            if not os.path.isfile(src):
                continue
            if fn.endswith(" - repro_results.csv"):
                key = fn[: -len(" - repro_results.csv")]
                kind = "repro_results.csv"
                if key not in key_set and key in alias_map:
                    key = alias_map[key]
            elif fn.endswith(" - repro_facts.json"):
                key = fn[: -len(" - repro_facts.json")]
                kind = "repro_facts.json"
            else:
                continue
            if key not in key_set:
                records.append({"kind": kind, "key": key, "src": src, "dst": "",
                                "status": "no_dataset_folder",
                                "detail": "no Datasets/<Key>/ folder for this artifact"})
                continue
            sig = (kind, key)
            if sig in seen:
                continue
            seen.add(sig)
            dst = os.path.join(DS, key, key + " - " + kind)
            if dry_run:
                st = {"same": "same", "copy": "would-copy",
                      "missing": "missing"}[_would_copy(src, dst)]
            else:
                st = _copy_atomic(src, dst)
            records.append({"kind": kind, "key": key, "src": src, "dst": dst,
                            "status": st, "detail": ""})

        # ---- run logs ------------------------------------------------------
        log_dir = os.path.join(temp_dir, "reproduce_logs")
        if os.path.isdir(log_dir):
            for fn in sorted(os.listdir(log_dir)):
                src = os.path.join(log_dir, fn)
                if not os.path.isfile(src):
                    continue
                stem = os.path.splitext(fn)[0]
                key = log_map.get(stem, "")
                durable = (RR.safe_slug(key) if (key and RR is not None) else stem) + ".log"
                sig = ("log", durable)
                if sig in seen:
                    continue
                seen.add(sig)
                dst = os.path.join(DURABLE_LOGS, durable)
                if dry_run:
                    st = {"same": "same", "copy": "would-copy",
                          "missing": "missing"}[_would_copy(src, dst)]
                else:
                    st = _copy_atomic(src, dst)
                records.append({"kind": "log", "key": key, "src": src, "dst": dst,
                                "status": st, "detail": "" if key else "unmapped log name"})
    return records


def report(records, dry_run=False):
    os.makedirs(RESULTS, exist_ok=True)
    n = len(records)
    if n == 0:
        print("persist_repro_artifacts: nothing to import "
              f"(scanned {', '.join(os.path.relpath(d, ROOT) for d in _temp_dirs()) or 'no temp dirs'})")
        return records
    counts = {}
    kinds = {}
    for r in records:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        kinds[(r["kind"], r["status"])] = kinds.get((r["kind"], r["status"]), 0) + 1
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    if not dry_run:
        with open(LOG_JSONL, "a", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps({"ts": ts, "dry_run": dry_run, **r},
                                    ensure_ascii=False) + "\n")
    print(f"persist_repro_artifacts: {n} artifact(s) found "
          f"({', '.join(f'{k}={v}' for k, v in sorted(counts.items()))})")
    for (kind, st) in sorted(kinds):
        print(f"    {kind:<18} {st:<11} {kinds[(kind, st)]}")
    n_new = counts.get("copied", 0) + counts.get("would-copy", 0)
    print(f"  new/changed to mirror: {n_new}")
    for r in records:
        if r["status"] in ("copied", "would-copy"):
            rel = os.path.relpath(r["dst"], ROOT) if r["dst"] else "(none)"
            print(f"  {r['status']:<11} {r['kind']:<18} {r['key'] or '?':<34} -> {rel}")
        elif r["status"] == "no_dataset_folder":
            print(f"  SKIP        {r['kind']:<18} {r['key']:<34} ({r['detail']})")
        elif r["status"] == "missing":
            print(f"  MISSING     {r['kind']:<18} {r['key']}")
    if not dry_run:
        print(f"  audit: {os.path.relpath(LOG_JSONL, ROOT)}")
    return records


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="report without copying")
    args = ap.parse_args(argv)
    recs = scan(dry_run=args.dry_run)
    report(recs, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
