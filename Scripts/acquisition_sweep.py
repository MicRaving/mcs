#!/usr/bin/env python
"""Systematic, bounded dataset-acquisition sweep over coded studies.

Goal: make "datasets must be tried to acquire for every paper" verifiable and
machine-readable.  Every coded study gets exactly one row in
`Results/acquisition_attempts.csv`; every actual fetch attempt also appends one
JSON object to `Results/acquisition_log.jsonl` (via `acquire_datasets.run`).

Targets = coded studies in `Extraction/` with **no** `Datasets/<Key>/` folder
(cross-checked against `Results/state.json: datasets.missing`).  For each target
the declared `dataset_availability_status` / `data_url` / `data_repository`
(and `preregistration_url`, plus repository links found in the coding prose) are
read, candidate URLs are normalised, and the best candidate is attempted through
the importable `acquire_datasets.run`.

Studies without a declared/derivable URL are still recorded as an attempt with
status `no_url_declared` — a failed or empty attempt is still a documented
attempt.  Studies that already have a `Datasets/<Key>/SOURCE.md` or
`ACQUISITION_REPORT.md` are backfilled as `documented_existing`.

Usage:
  python Scripts/acquisition_sweep.py --dry                 # build the plan only
  python Scripts/acquisition_sweep.py --apply --limit 25 --timeout 60
  python Scripts/acquisition_sweep.py --apply --only "Biddlestone et al. (2025 a)"
  python Scripts/acquisition_sweep.py --apply --from-file keys.txt

Products:
  Results/acquisition_attempts.csv   one row per coded study (full coverage)
  Results/acquisition_log.jsonl      one JSON line per fetch attempt (appended)
  temp/ws_f/acquisition_plan.csv     the dry-run plan (intermediate)
"""
import argparse
import csv
import glob
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import acquire_datasets as AD  # noqa: E402

DATA = os.path.join(ROOT, "Datasets")
EXTRACTION = os.path.join(ROOT, "Extraction")
RESULTS = os.path.join(ROOT, "Results")
TEMP = os.path.join(ROOT, "temp", "ws_f")
ATTEMPTS_CSV = os.path.join(RESULTS, "acquisition_attempts.csv")
PLAN_CSV = os.path.join(TEMP, "acquisition_plan.csv")
LOG_PATH = AD.LOG_PATH

STATUS_LABEL = {1: "open_url", 2: "upon_request", 3: "not_available", 4: "not_stated"}

FIELDS = ["study_key", "has_dataset", "declared_status", "data_url", "data_repository",
          "candidate_url", "attempt_status", "n_files", "n_data_files", "error", "timestamp",
          "evidence_path", "evidence_kind", "evidence_date", "n_candidates", "platform",
          "bytes"]

# repository-specific link patterns accepted from the coding prose
MD_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:"
    r"osf\.io/[A-Za-z0-9]{5,}|"
    r"zenodo\.org/records?/\d+|"
    r"figshare\.com/[^\s\)\]\"'`]+|"
    r"dataverse\.[a-z0-9.\-]+(?:/[^\s\)\]\"'`]*)?|"
    r"datadryad\.org/[^\s\)\]\"'`]+|"
    r"psycharchives\.org/[^\s\)\]\"'`]+|"
    r"github\.com/[^\s\)\]\"'`#?]+|"
    r"livrepository\.[^\s\)\]\"'`]+|"
    r"10\.17605/OSF\.IO/[A-Za-z0-9]+|"
    r"10\.5281/zenodo\.\d+|"
    r"10\.6084/m9\.figshare\.\d+|"
    r"10\.5061/[a-z0-9./\-]+|"
    r"10\.23668/[a-z0-9./\-]+"
    r")",
    re.I)


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _rstrip_punct(s):
    return s.strip().strip("<>\"'`").rstrip(".,;:").strip()


def normalize_candidate(raw, paper_doi=None, repo_only=False):
    """Normalise a raw URL/DOI/repository string to a fetchable URL, or None.

    `repo_only=True` accepts only genuine dataset-repository platforms (OSF,
    Zenodo, Figshare, Dataverse, Dryad, PsychArchives, GitHub) and rejects
    preregistration registries / arbitrary landing pages (used for
    `preregistration_url`, which is otherwise not a dataset source).
    """
    if raw is None:
        return None
    s = _rstrip_punct(str(raw))
    if not s:
        return None
    pd = re.sub(r"^https?://(dx\.)?doi\.org/", "", str(paper_doi or "").strip(), flags=re.I)

    # OSF (DOI form, node URL, or "OSF <id>")
    m = re.search(r"10\.17605/OSF\.IO/([A-Za-z0-9]+)", s, re.I)
    if m:
        url = f"https://osf.io/{m.group(1).lower()}/"
    else:
        m = re.search(r"osf\.io/([A-Za-z0-9]{5,})", s, re.I)
        if m:
            url = f"https://osf.io/{m.group(1).lower()}/"
            vo = re.search(r"[?&]view_only=([^&\s)\]]+)", s)
            if vo:
                url += f"?view_only={vo.group(1)}"
        else:
            m = re.match(r"^OSF[\s:#\-]+([A-Za-z0-9]{5,})\s*$", s, re.I)
            url = f"https://osf.io/{m.group(1).lower()}/" if m else None

    if url and pd and pd.lower() in url.lower():
        return None
    if url:
        return url

    # Zenodo
    m = (re.search(r"10\.5281/zenodo\.(\d+)", s, re.I)
         or re.search(r"zenodo\.org/records?/(\d+)", s, re.I))
    if m:
        return f"https://zenodo.org/records/{m.group(1)}"

    # Figshare
    m = (re.search(r"10\.6084/m9\.figshare\.(\d+)", s, re.I)
         or re.search(r"figshare\.com/(?:articles?/[^/]+/)?(\d{6,})", s, re.I))
    if m:
        return f"https://figshare.com/articles/dataset/x/{m.group(1)}"

    # Dataverse / Dryad / PsychArchives (pass through; acquire_datasets classifies)
    if "dataverse" in s.lower() or re.search(r"10\.7910/DVN", s, re.I):
        return s if s.lower().startswith("http") else f"https://doi.org/{s}"
    if "dryad" in s.lower() or s.startswith("10.5061") or "10.5061" in s:
        return s if s.lower().startswith("http") else s
    if "psycharchives" in s.lower() or "10.23668" in s:
        return s if s.lower().startswith("http") else s

    # GitHub
    m = re.search(r"github\.com/([^/\s#?]+/[^/\s#?]+)", s, re.I)
    if m:
        return f"https://github.com/{m.group(1).rstrip('.git')}"

    if repo_only:
        return None

    # DOI fallback (only if not the paper's own DOI)
    m = re.search(r"10\.\d{4,9}/[^\s\)\]\"'`]+", s, re.I)
    if m:
        doi = m.group(0).rstrip("/")
        if pd and doi.lower() == pd.lower():
            return None
        return f"https://doi.org/{doi}"

    # Generic landing page
    if s.lower().startswith("http"):
        if pd and pd.lower() in s.lower():
            return None
        return s
    return None


def md_candidates(text, paper_doi=None):
    out = []
    for m in MD_URL_RE.finditer(text or ""):
        u = normalize_candidate(m.group(0), paper_doi, repo_only=True)
        if u:
            out.append(u)
    return out


def extract_candidates(rec, md_text):
    """Ordered, de-duplicated candidate URLs from coding fields + prose."""
    paper_doi = rec.get("DOI")
    found = []

    def add(raw, source, repo_only=False):
        u = normalize_candidate(raw, paper_doi, repo_only=repo_only)
        if u:
            found.append({"url": u, "source": source})

    add(rec.get("data_url"), "data_url")
    add(rec.get("data_repository"), "data_repository")
    # preregistration pages only count when they live on a data repository
    add(rec.get("preregistration_url"), "preregistration_url", repo_only=True)
    for u in md_candidates(md_text, paper_doi):
        found.append({"url": u, "source": "coding_md"})

    seen, out = set(), []
    for c in found:
        if c["url"] not in seen:
            seen.add(c["url"])
            out.append(c)
    return out


# ------------------------------------------------------------------ evidence
def parse_evidence(key):
    """Return (kind, relpath, date, n_files) for an existing acquisition record."""
    d = os.path.join(DATA, key)
    for fn, kind in (("ACQUISITION_REPORT.md", "acquisition_report"),
                     ("SOURCE.md", "source_md")):
        p = os.path.join(d, fn)
        if os.path.isfile(p):
            try:
                t = open(p, encoding="utf-8", errors="ignore").read()
            except Exception:
                t = ""
            date = ""
            for rx in (r"Access date:\s*(\d{4}-\d{2}-\d{2})",
                       r"ACQUIRE_DONE_(\d{4}-\d{2}-\d{2})",
                       r"##\s*Attempt\s*\((\d{4}-\d{2}-\d{2})",
                       r"(\d{4}-\d{2}-\d{2})"):
                m = re.search(rx, t)
                if m:
                    date = m.group(1)
                    break
            n = ""
            m = (re.search(r"Files downloaded\s*\((\d+)\)", t)
                 or re.search(r"Files\s*\((\d+)\s*,", t)
                 or re.search(r"Files\s*\((\d+)\)", t))
            if m:
                n = int(m.group(1))
            return kind, os.path.relpath(p, ROOT).replace("\\", "/"), date, n
    if os.path.isdir(d):
        return "dataset_dir", os.path.relpath(d, ROOT).replace("\\", "/"), "", ""
    return "none", "", "", ""


# ------------------------------------------------------------------ workspace
def coded_keys():
    return sorted(os.path.splitext(os.path.basename(p))[0]
                  for p in glob.glob(os.path.join(EXTRACTION, "*.json")))


def dataset_dirs():
    return {os.path.basename(d) for d in glob.glob(os.path.join(DATA, "*"))
            if os.path.isdir(d)}


def load_records():
    recs = {}
    for p in glob.glob(os.path.join(EXTRACTION, "*.json")):
        key = os.path.splitext(os.path.basename(p))[0]
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception as e:  # noqa
            recs[key] = {"_load_error": str(e)}
            continue
        recs[key] = d[0] if isinstance(d, list) and d else (d if isinstance(d, dict) else {})
    return recs


def load_md(key):
    p = os.path.join(EXTRACTION, key + ".md")
    if os.path.isfile(p):
        return open(p, encoding="utf-8", errors="ignore").read()
    return ""


def latest_ledger_status():
    """key -> latest acquisition_log.jsonl record with files written."""
    out = {}
    if not os.path.isfile(LOG_PATH):
        return out
    for line in open(LOG_PATH, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:  # noqa
            continue
        key = r.get("key")
        if not key:
            continue
        out[key] = r  # last line wins
    return out


# ------------------------------------------------------------------ outcome
CONN_RE = re.compile(r"ConnectionError|ConnectTimeout|ReadTimeout|Timeout|"
                     r"NewConnectionError|Max retries|SSLError|ProxyError|"
                     r"RemoteDisconnected|ConnectionReset")

# extensions that plausibly carry participant-level / tabular data
DATA_EXT = (".csv", ".tsv", ".xlsx", ".xls", ".xlsm", ".sav", ".dta", ".rds",
            ".rdata", ".rda", ".por", ".zip", ".json", ".parquet", ".sas7bdat")


def classify_outcome(summary):
    files = summary.get("files", []) or []
    written = [f for f in files if f.get("status") in ("downloaded", "exists")]
    n_ok = len(written)
    n_bad = sum(1 for f in files if str(f.get("status", "")).startswith(("err", "skip")))
    n_data = sum(1 for f in written
                 if str(f.get("filename", "")).lower().endswith(DATA_EXT))
    if summary.get("skipped"):
        return "skipped", 0, 0, 0
    if n_ok > 0 and n_bad > 0:
        return "partial", n_ok, n_bad, n_data
    if n_ok > 0:
        return "acquired", n_ok, 0, n_data
    return "failed", 0, n_bad, 0


def attempt_candidates(key, candidates, timeout, dry=False):
    """Try candidates in order; return (status, candidate_url, summary, error).

    Files are only ever written into Datasets/<Key>/ by acquire_datasets.run;
    `acquired` is returned only when files were actually written.
    """
    last_err = ""
    last_summary = None
    first_url = candidates[0]["url"] if candidates else ""
    for c in candidates:
        url = c["url"]
        s = AD.run(key, url, dry=dry, timeout=timeout, keep_failed_dir=False,
                   log_path=LOG_PATH)
        last_summary, last_err = s, s.get("error", "")
        status, n_ok, n_bad, n_data = classify_outcome(s)
        if status in ("acquired", "partial"):
            return status, url, s, "", n_ok + n_bad, n_data
        if status == "skipped":
            return status, url, s, s.get("error", ""), 0, 0
    return "failed", first_url, last_summary, last_err, 0, 0


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(
        description="Documented acquisition sweep over coded studies without a dataset.")
    ap.add_argument("--dry", action="store_true",
                    help="build the plan only; attempt nothing (default)")
    ap.add_argument("--apply", action="store_true", help="actually fetch candidate URLs")
    ap.add_argument("--limit", type=int, default=0,
                    help="max number of studies that make network attempts (0 = all)")
    ap.add_argument("--timeout", type=int, default=60,
                    help="per-study/per-request timeout in seconds (default 60)")
    ap.add_argument("--only", default=None, help="restrict network targets to one study key")
    ap.add_argument("--from-file", default=None,
                    help="restrict network targets to the keys listed in this file")
    ap.add_argument("--retry", action="store_true",
                    help="re-attempt candidates that already have a ledger record "
                         "(default: reuse the recorded outcome, do not re-hit the host)")
    args = ap.parse_args()
    apply = args.apply and not args.dry

    os.makedirs(RESULTS, exist_ok=True)
    os.makedirs(TEMP, exist_ok=True)

    coded = coded_keys()
    ds = dataset_dirs()
    recs = load_records()

    missing = sorted(set(coded) - ds)

    # cross-check against state.json (if available)
    state_note = ""
    sp = os.path.join(RESULTS, "state.json")
    if os.path.isfile(sp):
        try:
            st_missing = set(json.load(open(sp, encoding="utf-8"))
                             .get("datasets", {}).get("missing", []))
            if st_missing and st_missing != set(missing):
                state_note = (f" (state.json missing={len(st_missing)}, "
                              f"derived={len(missing)}; using derived set)")
        except Exception:  # noqa
            pass

    # build plan for every missing study
    plan = []
    for key in missing:
        rec = recs.get(key, {})
        status = rec.get("dataset_availability_status")
        declared = STATUS_LABEL.get(status, "unset" if status is None else f"code:{status}")
        cands = extract_candidates(rec, load_md(key))
        plan.append({
            "study_key": key,
            "status_code": status if isinstance(status, int) else 9,
            "declared_status": declared,
            "data_url": rec.get("data_url") or "",
            "data_repository": rec.get("data_repository") or "",
            "candidates": cands,
            "n_candidates": len(cands),
        })

    # prioritise studies that actually have a URL
    priority = {1: 0, 2: 1, 9: 2, 3: 3, 4: 4}
    plan.sort(key=lambda r: (0 if r["n_candidates"] else 1,
                             priority.get(r["status_code"], 2),
                             r["study_key"]))

    # network target selection
    restrict = None
    if args.only:
        restrict = {args.only}
    elif args.from_file:
        restrict = {l.strip() for l in open(args.from_file, encoding="utf-8")
                    if l.strip() and not l.startswith("#")}

    net_targets = [r for r in plan if r["n_candidates"] and
                   (restrict is None or r["study_key"] in restrict)]
    if args.limit and args.limit > 0:
        net_targets = net_targets[:args.limit]
    # do not re-hit a host for a candidate already recorded in the ledger
    ledger = latest_ledger_status()
    already = {k for k, rec in ledger.items() if not rec.get("dry")}
    if apply and not args.retry:
        net_targets = [r for r in net_targets if r["study_key"] not in already]
    net_keys = {r["study_key"] for r in net_targets}

    print(f"coded studies                : {len(coded)}{state_note}")
    print(f"with a Datasets/<Key>/ folder : {len(ds)}")
    print(f"without a dataset (targets)   : {len(missing)}")
    print(f"  + with a candidate URL      : {len([r for r in plan if r['n_candidates']])}")
    print(f"  + no URL derived            : {len([r for r in plan if not r['n_candidates']])}")
    print(f"network attempts this run     : {len(net_targets)}")
    print(f"per-study timeout             : {args.timeout}s")
    print(f"mode                          : {'APPLY (live fetch)' if apply else 'DRY (plan only)'}")

    # ---- dry: write the plan and stop
    if not apply:
        with open(PLAN_CSV, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["study_key", "declared_status", "data_url", "data_repository",
                        "n_candidates", "candidate_url", "candidate_source"])
            for r in plan:
                if r["candidates"]:
                    for c in r["candidates"]:
                        w.writerow([r["study_key"], r["declared_status"], r["data_url"],
                                    r["data_repository"], r["n_candidates"], c["url"],
                                    c["source"]])
                else:
                    w.writerow([r["study_key"], r["declared_status"], r["data_url"],
                                r["data_repository"], 0, "", ""])
        print(f"\nplan -> {os.path.relpath(PLAN_CSV, ROOT)}")
        print("dry run: nothing fetched, no attempt ledger written")
        return

    # ---- apply: sweep targets + backfill the rest -> full-coverage CSV
    rows = []
    ts = now_iso()

    # backfill studies that already have a dataset folder
    for key in sorted(ds):
        if key not in recs:
            continue  # 2 dataset dirs are not coded; they stay out of the coded-set CSV
        rec = recs[key]
        status = rec.get("dataset_availability_status")
        declared = STATUS_LABEL.get(status, "unset" if status is None else f"code:{status}")
        kind, path, date, n = parse_evidence(key)
        # if this study was acquired by an earlier sweep run, keep that status
        led = ledger.get(key) or {}
        n_dl = int(led.get("n_downloaded") or 0)
        n_not = int(led.get("n_not_retrieved") or 0)
        if led.get("ok") and n_dl:
            att = "partial" if n_not else "acquired"
        else:
            att = "documented_existing"
        n_data = sum(1 for f in led.get("files", [])
                     if str(f.get("filename", "")).lower().endswith(DATA_EXT))
        note = ""
        if att in ("acquired", "partial") and n_data == 0:
            note = "retrieved documents only (no tabular data file)"
        elif att in ("acquired", "partial"):
            note = led.get("error", "") or ""
        rows.append({
            "study_key": key, "has_dataset": 1, "declared_status": declared,
            "data_url": rec.get("data_url") or "", "data_repository": rec.get("data_repository") or "",
            "candidate_url": led.get("url_or_doi", "") if att != "documented_existing" else "",
            "attempt_status": att,
            "n_files": n, "n_data_files": (n_data if att != "documented_existing" else ""),
            "error": note, "timestamp": date or led.get("timestamp") or ts,
            "evidence_path": path, "evidence_kind": kind, "evidence_date": date,
            "n_candidates": "", "platform": led.get("platform", "") if att != "documented_existing" else "",
            "bytes": "",
        })

    # sweep the targets
    consecutive_conn = 0
    aborted = False
    for r in plan:
        key = r["study_key"]
        if not r["candidates"]:
            rows.append({
                "study_key": key, "has_dataset": 0, "declared_status": r["declared_status"],
                "data_url": r["data_url"], "data_repository": r["data_repository"],
                "candidate_url": "", "attempt_status": "no_url_declared", "n_files": 0,
                "n_data_files": 0,
                "error": "no data URL declared in Extraction coding",
                "timestamp": ts,
                "evidence_path": f"Extraction/{key}.json", "evidence_kind": "coding",
                "evidence_date": "", "n_candidates": 0, "platform": "", "bytes": "",
            })
            continue

        prior = ledger.get(key) or {}
        has_prior = bool(prior) and not prior.get("dry")
        if key not in net_keys or aborted or (has_prior and not args.retry):
            prior_status = ("" if not has_prior else
                            ("partial" if (prior.get("ok") and prior.get("n_not_retrieved"))
                             else "acquired" if prior.get("ok")
                             else "failed"))
            if prior_status:
                rows.append({
                    "study_key": key, "has_dataset": 1 if prior_status in ("acquired", "partial") else 0,
                    "declared_status": r["declared_status"], "data_url": r["data_url"],
                    "data_repository": r["data_repository"],
                    "candidate_url": prior.get("url_or_doi", ""), "attempt_status": prior_status,
                    "n_files": prior.get("n_downloaded", ""),
                    "n_data_files": sum(1 for f in prior.get("files", [])
                                        if str(f.get("filename", "")).lower().endswith(DATA_EXT)),
                    "error": ("prior attempt this campaign (not re-attempted): "
                              + (prior.get("error") or "")),
                    "timestamp": prior.get("timestamp", ts), "evidence_path": "Results/acquisition_log.jsonl",
                    "evidence_kind": "log", "evidence_date": "",
                    "n_candidates": r["n_candidates"], "platform": prior.get("platform", ""),
                    "bytes": "",
                })
            else:
                rows.append({
                    "study_key": key, "has_dataset": 0, "declared_status": r["declared_status"],
                    "data_url": r["data_url"], "data_repository": r["data_repository"],
                    "candidate_url": r["candidates"][0]["url"], "attempt_status": "skipped",
                    "n_files": 0, "n_data_files": 0,
                    "error": ("aborted after repeated connection errors" if aborted else
                              "not attempted this run (--limit/--only/--from-file)"),
                    "timestamp": ts, "evidence_path": "Results/acquisition_log.jsonl",
                    "evidence_kind": "log", "evidence_date": "",
                    "n_candidates": r["n_candidates"], "platform": "", "bytes": "",
                })
            continue

        print(f"  [{key}] trying {r['candidates'][0]['url']}")
        status, url, summary, err, n_used, n_data = attempt_candidates(
            key, r["candidates"], args.timeout)
        n_written = 0
        if summary:
            n_written = sum(1 for f in summary.get("files", [])
                            if f.get("status") in ("downloaded", "exists"))
        if status in ("acquired", "partial") and n_data == 0:
            err = "retrieved documents only (no tabular data file)"
        ev = os.path.join("Datasets", key, "SOURCE.md")
        evidence = ev if os.path.exists(os.path.join(ROOT, ev)) else "Results/acquisition_log.jsonl"
        rows.append({
            "study_key": key, "has_dataset": 1 if status in ("acquired", "partial") else 0,
            "declared_status": r["declared_status"], "data_url": r["data_url"],
            "data_repository": r["data_repository"], "candidate_url": url,
            "attempt_status": status, "n_files": n_written, "n_data_files": n_data,
            "error": err,
            "timestamp": ts, "evidence_path": evidence.replace("\\", "/"),
            "evidence_kind": "source_md" if evidence.endswith("SOURCE.md") else "log",
            "evidence_date": "", "n_candidates": r["n_candidates"],
            "platform": (summary or {}).get("platform", ""),
            "bytes": (summary or {}).get("bytes", ""),
        })
        print(f"      -> {status} ({n_written} file(s) written, {n_data} data-like)")

        if status == "failed" and CONN_RE.search(err or ""):
            consecutive_conn += 1
        else:
            consecutive_conn = 0
        if consecutive_conn >= 3:
            aborted = True
            print("  !! 3 consecutive connection errors -> aborting further network attempts")

    with open(ATTEMPTS_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for row in sorted(rows, key=lambda x: x["study_key"]):
            w.writerow(row)

    # ---- coverage
    counts = {}
    for row in rows:
        counts[row["attempt_status"]] = counts.get(row["attempt_status"], 0) + 1
    total = len(coded)
    with_ds = sum(1 for r in rows if r["has_dataset"] == 1)
    no_url = counts.get("no_url_declared", 0)
    net = sum(counts.get(s, 0) for s in ("acquired", "partial", "failed", "skipped"))
    print("\n=== coverage ===")
    print(f"total coded                    : {total}")
    print(f"rows in acquisition_attempts   : {len(rows)}")
    print(f"with dataset (folder present)  : {with_ds}")
    print(f"documented_existing (backfill) : {counts.get('documented_existing', 0)}")
    print(f"attempted (any status, coded)  : {len(rows)}  (every coded study has a record)")
    print(f"  network attempts this campaign: {net}")
    print(f"  no_url_declared               : {no_url}")
    print(f"by status                      : {dict(sorted(counts.items()))}")
    print(f"\nwrote {os.path.relpath(ATTEMPTS_CSV, ROOT)}")
    print(f"ledger {os.path.relpath(LOG_PATH, ROOT)}")


if __name__ == "__main__":
    main()
