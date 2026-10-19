# -*- coding: utf-8 -*-
"""RUN-REPRODUCERS: run every per-study reproduction script and summarise the outcome.

For every ``Datasets/<Key>/<Key> - reproduction.py`` (337 scripts: 270 thin wrappers
around ``Scripts/repro_lib.py::reproduce()`` and 67 bespoke hand-written scripts):

  1. runs it as a subprocess (stdout+stderr captured, timeout enforced, crashes reported
     individually without aborting the batch);
  2. parses the printed report into a per-study fact sheet:
       key | script kind | exclusion criteria | analysis applied | recalculated-vs-paper
       values | status (reproduced / partial / not reproduced / reported_only /
       not reproducible / crashed) | which results reproduced and which did not;
  3. writes ``reproduction_summary.xlsx`` with a per-study sheet ('Reproduction') and a
     per-domain sheet ('Domains'), and prints a concise console summary.

Two output modes:
  * default          - run the scripts now (sequential, or ``--jobs`` parallel);
  * ``--no-run``     - re-aggregate from the logs already in ``temp/reproduce_logs/``
                       (no subprocesses), e.g. after a reboot or to regenerate the xlsx.

``--only <substr>`` (case-insensitive) restricts to study keys containing the substring
(e.g. ``--only "Arechar"``, ``--only "Basol"``), which is useful for spot checks.

Status vocabulary
----------------
The ``status`` column is the per-study reproduction verdict.  It is the documented
``**reproduced:**`` verdict from ``Reproduction/<Key>.md`` when one exists (the
human-verified conclusion for the whole study, which also accounts for parts the
machine cannot recompute, e.g. missing source data or a manual reimplementation),
otherwise the machine verdict derived from the script's own output.  A crash or
absent participant data always takes precedence.  The machine-only verdict is kept
in ``machine_status`` and ``machine_disagrees`` flags the studies where the documented
verdict changed the headline.

  reproduced        - every checked contrast reproduced (within ~2% on the same 0-1
                      scale, sign-convention flips, or same sign when the coded metric
                      is scale-mismatched) and none failed;
  partial           - some checked contrasts reproduced, some did not;
  not reproduced    - data present but every checked contrast failed (or the documented
                      verdict says no);
  reported_only     - data present but no contrast could be checked against a recalc
                      (everything NOT CHECKABLE / NOT COMPARABLE, or no reported values
                      in a bespoke script's output);
  not reproducible  - no participant data (no ``<Key> - harmonized.csv``);
  crashed           - subprocess exception / timeout / nonzero exit;
  not run           - ``--no-run`` and no log exists for the study.

All intermediates live in ``temp/``; the only root-level output is
``reproduction_summary.xlsx``.
"""
import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time

import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))       # Scripts/analysis
SCRIPTS = os.path.dirname(_HERE)                         # Scripts
B = os.path.dirname(SCRIPTS)                             # project root
RESULTS = os.path.join(B, "Results")
os.makedirs(RESULTS, exist_ok=True)
DS = os.path.join(B, "Datasets")
REPRO_MD = os.path.join(B, "Reproduction")
TMP = os.path.join(B, "temp")
LOGS = os.path.join(TMP, "reproduce_logs")
os.makedirs(LOGS, exist_ok=True)
tempfile.tempdir = TMP

STDERR_MARK = "--- STDERR ---"
INVALID_FS = set('<>:"/\\|?*')
STATUSES = ["reproduced", "partial", "not reproduced", "reported_only",
            "not reproducible", "crashed", "not run"]

# ---------------------------------------------------------------------------
# enumeration + subprocess running
# ---------------------------------------------------------------------------


def legacy_slug(key):
    """The pre-hash log slug (kept so existing temp/reproduce_logs/ still load)."""
    return "".join(ch for ch in key if ch not in INVALID_FS) or "unnamed"


def safe_slug(key):
    """Collision-proof log slug: sanitized key + short hash of the full key.

    Two distinct keys can sanitise to the same string (every invalid character is
    removed), which would make them share one log file and lose one study's output.
    Appending a hash of the full key makes a collision impossible.
    """
    h = hashlib.sha1(str(key).encode("utf-8")).hexdigest()[:8]
    return f"{legacy_slug(key)}__{h}"


def log_path_for(key):
    """Path of this key's log: the new hashed name if written, else the legacy one."""
    new = os.path.join(LOGS, safe_slug(key) + ".txt")
    if os.path.isfile(new):
        return new
    legacy = os.path.join(LOGS, legacy_slug(key) + ".txt")
    return legacy if os.path.isfile(legacy) else new


DURABLE_LOGS = os.path.join(RESULTS, "repro_logs")


def _copy_if_changed(src, dst):
    """Copy ``src`` -> ``dst`` unless the bytes already match; return 'copied'|'same'."""
    import hashlib as _h
    if not os.path.isfile(src):
        return "missing"
    if os.path.isfile(dst):
        try:
            with open(src, "rb") as a, open(dst, "rb") as b:
                if _h.sha1(a.read()).digest() == _h.sha1(b.read()).digest():
                    return "same"
        except OSError:
            pass
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".tmp"
    with open(src, "rb") as a, open(tmp, "wb") as b:
        b.write(a.read())
    os.replace(tmp, dst)
    return "copied"


def persist_study_artifacts(key, fact=None):
    """D10: copy this study's facts/results/log into their durable homes.

    * ``<Key> - repro_results.csv`` + ``<Key> - repro_facts.json`` -> ``Datasets/<Key>/``
    * the run log -> ``Results/repro_logs/<safe_slug>.log``

    ``fact`` (the aggregated fact dict) is written to ``temp/<Key> - repro_facts.json``
    when ``repro_lib`` did not already produce one (bespoke scripts).  Idempotent.
    """
    report = {"key": key, "copied": [], "same": [], "missing": []}
    facts_path = os.path.join(TMP, key + " - repro_facts.json")
    if fact is not None:
        # ``repro_lib`` may already have written its returned dict (wrappers, D10 hook);
        # keep it nested under ``lib_facts`` and persist the runner's contrast-level dict
        # (uniform across wrappers and bespoke scripts) as the top level.
        combined = dict(fact)
        if os.path.isfile(facts_path):
            try:
                with open(facts_path, encoding="utf-8") as fh:
                    combined["lib_facts"] = json.load(fh)
            except Exception:
                pass
        try:
            os.makedirs(TMP, exist_ok=True)
            with open(facts_path, "w", encoding="utf-8") as fh:
                json.dump(combined, fh, ensure_ascii=False, indent=1, default=str)
        except Exception:
            pass
    folder = os.path.join(DS, key)
    rr_src = repro_results_path(key) or os.path.join(TMP, key + " - repro_results.csv")
    pairs = [(rr_src, os.path.join(folder, key + " - repro_results.csv")),
             (facts_path, os.path.join(folder, key + " - repro_facts.json"))]
    for src, dst in pairs:
        st = _copy_if_changed(src, dst)
        report[{"copied": "copied", "same": "same", "missing": "missing"}[st]].append(
            os.path.basename(dst))
    log_src = os.path.join(LOGS, safe_slug(key) + ".txt")
    if not os.path.isfile(log_src):
        log_src = os.path.join(LOGS, legacy_slug(key) + ".txt")
    log_dst = os.path.join(DURABLE_LOGS, safe_slug(key) + ".log")
    st = _copy_if_changed(log_src, log_dst)
    report[{"copied": "copied", "same": "same", "missing": "missing"}[st]].append(
        os.path.relpath(log_dst, B))
    return report


def enumerate_scripts(only=None, kind=None):
    """[(key, script_path, kind)] for every Datasets/<Key>/<Key> - reproduction.py."""
    out = []
    if not os.path.isdir(DS):
        return out
    onlys = [s.strip().lower() for s in (only or "").split(",") if s.strip()]
    for name in sorted(os.listdir(DS)):
        folder = os.path.join(DS, name)
        if not os.path.isdir(folder) or name.startswith(".") or name in ("temp", "_meta_analyses"):
            continue
        script = os.path.join(folder, name + " - reproduction.py")
        if not os.path.isfile(script):
            continue
        if onlys and not any(o in name.lower() for o in onlys):
            continue
        with open(script, encoding="utf-8", errors="replace") as f:
            head = f.read(4000)
        k = "wrapper" if ("repro_lib" in head and "reproduce(" in head) else "bespoke"
        if kind and k != kind:
            continue
        out.append((name, script, k))
    return out


def run_one(key, script, timeout, index, total, jobs):
    log_path = os.path.join(LOGS, safe_slug(key) + ".txt")
    t0 = time.time()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPYCACHEPREFIX"] = os.path.join(TMP, "pycache")
    # D10: ask the shared engine to persist the fact dict it returns (wrappers only).
    env["REPRO_FACTS_OUT"] = os.path.join(TMP, key + " - repro_facts.json")
    rc, err = 0, ""
    try:
        p = subprocess.run(
            [sys.executable, script], cwd=os.path.dirname(script),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env,
        )
        rc = p.returncode
        out, err = p.stdout, p.stderr
        if rc != 0 and not out.strip():
            err = (err or "") + f"\n[exit code {rc}]"
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        err = (e.stderr or "") if isinstance(e.stderr, str) else ""
        err += f"\n[run_reproducers: TIMEOUT after {timeout}s]"
        rc = -1
    except Exception as e:  # subprocess could not even start
        out, err, rc = "", f"[run_reproducers: {type(e).__name__}: {e}]", -2
    runtime = round(time.time() - t0, 1)
    body = (f"# run_reproducers log - {key}\n"
            f"# ts={time.strftime('%Y-%m-%d %H:%M:%S')} exit={rc} runtime={runtime}s\n"
            f"{out}\n{STDERR_MARK}\n{err}")
    with open(log_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    if rc not in (0,):
        status = "crashed"
    else:
        status = "ran"
    print(f"  [{index}/{total}] {key}: exit={rc} {runtime}s {status}",
          flush=True)
    return {"key": key, "script": script, "log_path": log_path, "rc": rc,
            "runtime": runtime, "error": err, "ran": rc == 0}


def load_log(key):
    """Return (stdout, stderr, rc, runtime) reconstructed from the saved log."""
    p = log_path_for(key)
    if not os.path.isfile(p):
        return None
    txt = open(p, encoding="utf-8", errors="replace").read()
    m = re.search(r"^# ts=.+? exit=(-?\d+) runtime=([\d.]+)s\s*$", txt, re.M)
    rc = int(m.group(1)) if m else 0
    runtime = float(m.group(2)) if m else float("nan")
    if STDERR_MARK in txt:
        out, err = txt.split(STDERR_MARK, 1)
    else:
        out, err = txt, ""
    out = re.sub(r"^# run_reproducers log[^\n]*\n", "", out, count=1)
    out = re.sub(r"^# ts=.*\n", "", out, count=1)
    return out, err, rc, runtime


# ---------------------------------------------------------------------------
# markdown helpers (documented verdict + reproduced-vs-reported tables)
# ---------------------------------------------------------------------------


def read_md(key):
    p = os.path.join(REPRO_MD, key + ".md")
    if not os.path.isfile(p):
        return "", None, None
    txt = open(p, encoding="utf-8", errors="replace").read()
    verdict = reason = None
    m = re.search(r"\*\*reproduced:\*\*\s*([^\n*]+)", txt, re.I)
    if m:
        verdict = m.group(1).strip()
    m = re.search(r"\*\*reason:\*\*", txt, re.I)
    if m:
        tail = txt[m.end():]
        nxt = re.search(r"\n\s*\*\*", tail)
        seg = tail[:nxt.start()] if nxt else tail[:600]
        reason = re.sub(r"[\r\n]+", " ", seg).strip().strip("|").strip("*").strip() or None
    return txt, verdict, reason


_NA_MATCH = {"", "n/a", "na", "n.a.", "n.a", "—", "-", "--", "none",
             "not applicable", "n/a (computed)", "n/a (no reported)"}


def _verdict_col(head):
    """Index of a Match?/verdict-style column, or None.

    Never returns a plain 'Reproduced'/'Reported' results column (those carry the
    values being compared); only an explicit Match?/verdict/agreement column, or a
    question-marked column such as 'Reproduced?', counts as the verdict.
    """
    for hi in range(len(head) - 1, 0, -1):
        s = head[hi].strip().lower().strip("*").strip()
        if re.search(r"report|paper|publi|author", s):
            continue
        if re.search(r"\breproduc", s) and "?" not in s:
            continue
        if re.search(r"match|verdict|agree|status|same|exact|consistent", s) \
                or "?" in s:
            return hi
    return None


def _match_verdict(raw):
    """Normalise an author Match? cell onto the runner vocabulary ('' = n/a)."""
    s = str(raw or "").strip().strip("*").strip().lower()
    s = re.sub(r"\s+", " ", s.replace("?", " ")).strip().rstrip(".").strip()
    if s in _NA_MATCH:
        return ""
    if s.startswith("~"):
        s = s.lstrip("~ ").strip()
    if s in _NA_MATCH:
        return ""
    s = s.replace("_", " ")
    if "exact" in s or s in ("y", "=", "true", "consistent") \
            or s.startswith(("yes", "b yes")):
        return "REPRODUCED"
    if "direction" in s and not s.startswith("yes"):
        return "reproduced in direction"
    if s.startswith(("no", "false", "dissimilar", "mismatch", "mean off", "≠", "✗")) \
            or "(no)" in s:
        return "NOT REPRODUCED"
    if "partial" in s:
        return "PARTIAL"
    if s.startswith(("close", "approx", "approximate", "near", "similar")):
        return "PARTIAL"
    return "PARTIAL"


def md_repro_table(key):
    """Parse 'Reproduced vs reported'-style markdown tables from Reproduction/<Key>.md.

    Recognises tables whose header has a results-side column ('Reported'/'paper'/
    'published'/'author') plus a reproduction-side column ('Reproduced'/'match'/
    'recovered').  Returns one dict per data row {label, reproduced, reported,
    verdict, match_raw}.  Verdict priority:
      1. an explicit Match?-style column, normalised by ``_match_verdict``
         ('yes'/'exact' -> REPRODUCED, 'no'/'not ...' -> NOT REPRODUCED,
         'close'/'approx'/'partial' -> PARTIAL, 'n/a' -> no verdict);
      2. numeric comparison of the 'Reproduced' and 'Reported' cells with the same
         ~2% / direction / sign-convention rule as the machine recomputation;
      3. '' when nothing is comparable.
    """
    txt, _, _ = read_md(key)
    if not txt:
        return []
    out = []
    for block in re.findall(r"((?:^\|.*\n)+)", txt, re.M):
        lines = [l for l in block.splitlines() if l.strip().startswith("|")]
        if len(lines) < 2:
            continue
        head = [c.strip() for c in lines[0].strip("|").split("|")]
        hl = " ".join(h.lower() for h in head)
        if not (re.search(r"report|paper|published|author", hl)
                and re.search(r"reproduc|match|recover|recalc|result", hl)):
            continue
        if not re.match(r"^\s*\|[\s:\-|]+\|\s*$", lines[1]):
            continue
        vcol = _verdict_col(head)
        rep_i = repd_i = None
        for hi in range(1, len(head)):
            s = head[hi].strip().lower().strip("*").strip()
            if hi == vcol:
                continue
            if rep_i is None and re.search(r"reproduc|recover|recalc", s):
                rep_i = hi
            if repd_i is None and re.search(r"report|paper|publi|author", s):
                repd_i = hi
        for l in lines[2:]:
            cells = [c.strip() for c in l.strip("|").split("|")]
            if len(cells) < len(head):
                cells += [""] * (len(head) - len(cells))
            label = cells[0]
            if not label or label.startswith("**") or "---" in label:
                continue
            # some tables lead with a bare study/experiment number and put the effect
            # name second (e.g. '| Study | Effect | Reproduced | ...'); join them
            if re.fullmatch(r"[\d.\s]+", label) and len(cells) > 1 \
                    and re.search(r"[A-Za-z]", cells[1]):
                label = f"{label.strip()}: {cells[1]}"
            match_raw = str(cells[vcol]).strip() if vcol is not None else ""
            verdict = _match_verdict(match_raw) if vcol is not None else ""
            rep_cell = cells[rep_i] if rep_i is not None else ""
            repd_cell = cells[repd_i] if repd_i is not None else ""
            rep = _first_num(rep_cell)
            repd = _first_num(repd_cell)
            if rep is None and repd is None and not verdict:
                continue
            if not verdict and rep is not None and repd is not None:
                verdict = _pair_verdict(rep, repd) or ""
            out.append({"label": label, "reproduced": rep_cell or "",
                        "reported": repd_cell or "", "verdict": verdict,
                        "match_raw": match_raw})
    return out


def _first_num(cell):
    """First number of a markdown cell, but only when the cell STARTS with one
    (after markdown emphasis, an optional approximation symbol / sign) - so prose
    cells such as 'large (country ratios 0.11-0.53)' or 'I2 = 74.4%' yield None,
    while '**0.154** (se 0.024)', '3.28 / 3.03' and '-0.39' yield their leading value."""
    m = re.match(r"\s*[*_`]*\s*[≈~]?\s*([-−]?\d+(?:\.\d+)?)", str(cell))
    return float(m.group(1).replace("−", "-")) if m else None


# ---------------------------------------------------------------------------
# wrapper report parser  (the 270 repro_lib thin wrappers)
# ---------------------------------------------------------------------------


def parse_wrapper(out):
    """Parse the four-section repro_lib report. Returns a fact dict."""
    f = {"kind": "wrapper", "exclusions": "", "analysis": "", "recalc_rows": [],
         "recalc_note": "", "n_rows": None, "contrasts": [], "summary": {},
         "documented_verdict": "", "documented_reason": "", "n_coded": None,
         "has_data": None}
    if "REPRODUCTION REPORT -" not in out:
        f["error_note"] = "no REPRODUCTION REPORT marker in stdout"
        return f
    f["has_data"] = "no participant data - NOT reproducible" not in out

    # ---------------- exclusion criteria ----------------
    m = re.search(r"EXCLUSION CRITERIA\n(.*?)\n\nANALYSIS APPLIED", out, re.S)
    excl = m.group(1) if m else ""
    if "no participant data: no `" in excl:
        f["exclusions"] = "n/a (no participant data)"
    elif "header-only" in excl:
        f["exclusions"] = "none (exclusion file is header-only)"
    elif "no exclusion file" in excl:
        f["exclusions"] = "none applied"
    else:
        rules = []
        for line in excl.splitlines():
            line = line.strip()
            if not line or line.startswith("flag | gates") or line.startswith("-"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 4 and re.match(r"^[A-Za-z_][\w]*$", parts[0]):
                rules.append(f"{parts[0]} {parts[2]} ({parts[1]})")
        f["exclusions"] = "; ".join(rules) if rules else excl.strip() or "none"

    # ---------------- analysis applied ----------------
    m = re.search(r"ANALYSIS APPLIED\n(.*?)\n\nRECALCULATION", out, re.S)
    ana = m.group(1) if m else ""
    dom_lines = []
    for line in ana.splitlines():
        mm = re.match(r"^\s{4}([\w]+): veracity=\{(.*?)\}\s+timepoint=\{(.*?)\}\s+"
                      r"polarity=\{(.*?)\}$", line)
        if mm:
            dom, ver, tp, pol = mm.groups()
            ver = ver or "(no veracity)"
            tp = tp or "blank"
            pol = pol or "-"
            dom_lines.append(f"{dom} [veracity={ver}; tp={tp}; polarity={pol}]")
    mm = re.search(r"exclusions applied:\s*(.*)", ana)
    excl_applied = mm.group(1).strip() if mm else ""
    mm = re.search(r"estimator:\s*(.*?)(?:\n\s{13}.*)*", ana, re.S)
    est = mm.group(1).strip() if mm else ""
    notes = re.findall(r"NOTE:\s*(.*)", ana)
    f["analysis"] = " | ".join(dom_lines)
    f["estimator"] = re.sub(r"\s+", " ", est)
    f["exclusions_applied"] = excl_applied
    f["notes"] = notes

    # ---------------- recalculation ----------------
    m = re.search(r"RECALCULATION\n(.*?)\n\nREPRODUCED VS NOT", out, re.S)
    rec = m.group(1) if m else ""
    mm = re.search(r"rows analysed:\s*(\d+)", rec)
    f["n_rows"] = int(mm.group(1)) if mm else None
    for line in rec.splitlines():
        mm = re.match(r"^\s{2}([\w]+)\s+(\(blank\)|[\w]+)\s+(\d+)\s+([-\d.]+)\s+"
                      r"(\d+)\s+([-\d.]+)\s+([-\d.]+)$", line)
        if mm:
            dom, tp, nt, mt, nf, mf, d = mm.groups()
            f["recalc_rows"].append({"domain": dom, "timepoint": tp,
                                     "n_true": int(nt), "mean_true": float(mt),
                                     "n_false": int(nf), "mean_false": float(mf),
                                     "d": float(d)})
    if "no participant data" in rec:
        f["recalc_note"] = "no participant data"
        mm = re.search(r"documented verdict \(Reproduction/[^)]*\.md\):\s*(.*)", rec)
        if mm:
            verdict_txt = mm.group(1).strip()
            f["documented_verdict"], _, f["documented_reason"] = _split_verdict(verdict_txt)
        mm = re.search(r"^\s{2}reason:\s*(.*)$", rec, re.M)
        if mm and not f["documented_reason"]:
            f["documented_reason"] = mm.group(1).strip()
    elif "no true/false discrimination computable" in rec:
        f["recalc_note"] = "no true/false discrimination computable (veracity split absent)"

    # ---------------- reproduced vs not ----------------
    m = re.search(r"REPRODUCED VS NOT\n(.*?)(?:\n\s*={10,})", out, re.S)
    rv = m.group(1) if m else out
    mm = re.search(r"documented verdict \(Reproduction/[^)]*\.md\):\s*(.*)", rv)
    if mm:
        f["documented_verdict"], _, f["documented_reason"] = _split_verdict(mm.group(1).strip())
    mm = re.search(r"coded contrasts:\s*(\d+)", rv)
    f["n_coded"] = int(mm.group(1)) if mm else None
    mm = re.search(r"domain \| coded effect \| recalc twin \| verdict", rv)
    cur = None
    if mm:
        for line in rv[mm.end():].splitlines():
            if line.startswith("      contrast:"):
                if cur is not None:
                    cur["contrast"] = line.split(":", 1)[1].strip()
                continue
            if line.startswith("      note:"):
                if cur is not None:
                    cur["note"] = line.split(":", 1)[1].strip()
                continue
            s = line.strip()
            if not s or s.startswith("summary:") or s.startswith("REPRODUCED (within"):
                continue
            if " | " in line:
                left, verdict = line.rsplit(" | ", 1)
                verdict = verdict.strip()
                tok = left.split()
                if not tok:
                    continue
                twin = tok[-1]
                dom = tok[0]
                coded = " ".join(tok[1:-1]).strip()
                cur = {"domain": dom, "coded_effect": coded, "recalc_twin": twin,
                       "verdict": verdict, "contrast": "", "note": ""}
                f["contrasts"].append(cur)
    for line in rv.splitlines():
        s = line.strip()
        for label in ("REPRODUCED (within ~2%, same-scale):",
                      "reproduced in direction (scale-mismatched coded metric):",
                      "NOT REPRODUCED", "NOT COMPARABLE (unsigned coded metric, "
                      "no sign to compare):", "NOT CHECKABLE"):
            if s.startswith(label):
                rest = s[len(label):].strip()
                f["summary"][label] = [x.strip() for x in rest.split(",") if x.strip()] \
                    if rest and rest.lower() != "none" else []
    return f


def _split_verdict(txt):
    """'partial - reason' -> ('partial', '', reason); 'yes' / 'no' -> (value, '', '')."""
    if not txt:
        return "", "", ""
    if txt.lower() in ("yes", "no"):
        return txt.lower(), "", ""
    if " - " in txt:
        v, r = txt.split(" - ", 1)
        return v.strip(), "", r.strip()
    return txt.strip(), "", ""


# ---------------------------------------------------------------------------
# bespoke report analysis (67 hand-written scripts)
# ---------------------------------------------------------------------------


def repro_results_path(key):
    """Locate a study's ``repro_results.csv`` (temp first, then the durable copy).

    Two bespoke scripts write a non-canonical filename (``Martinez2024 - repro_results.csv``,
    ``LR23 - repro_results.csv``), so fall back to the filename literally referenced by the
    study's reproduction script.
    """
    for base in (TMP, os.path.join(DS, key)):
        p = os.path.join(base, key + " - repro_results.csv")
        if os.path.isfile(p):
            return p
    script = os.path.join(DS, key, key + " - reproduction.py")
    if os.path.isfile(script):
        try:
            with open(script, encoding="utf-8", errors="replace") as fh:
                txt = fh.read()
        except OSError:
            return None
        for m in re.finditer(r"([^\"'\s/\\]+ - repro_results\.csv)", txt):
            cand = m.group(1)
            for base in (TMP, os.path.join(DS, key)):
                p = os.path.join(base, cand)
                if os.path.isfile(p):
                    return p
    return None


def csv_columns_for(key):
    p = repro_results_path(key)
    if not p:
        return None, []
    try:
        df = pd.read_csv(p, encoding="utf-8-sig")
    except Exception:
        return None, []
    if df.empty:
        return df, []
    return df, [str(c) for c in df.columns]


def _infer_domain(text):
    # NB a bare string must NOT be iterated character-by-character (that inserts
    # spaces and breaks the substring regexes below).
    if isinstance(text, (list, tuple, set)):
        t = " ".join(str(x) for x in text).lower()
    else:
        t = str(text or "").lower()
    if re.search(r"share|diffus|retweet|forward", t):
        return "sharing_intention"
    if re.search(r"credib|accuracy|true|fake|real|belief|discern|misinform|conspir", t):
        return "credibility"
    if re.search(r"trust", t):
        return "trust"
    if re.search(r"confiden|certain", t):
        return "confidence"
    if re.search(r"attitude|opinion|support|policy", t):
        return "attitude_policy"
    if re.search(r"knowledge|literacy|skill", t):
        return "knowledge"
    if re.search(r"intention|behavior|behaviour|action|vaccinat", t):
        return "intention"
    return "other"


SIGN_CONV = "reproduced (sign-convention)"


def _pair_verdict(a, b):
    """Verdict for one (recalculated, reported) numeric pair on the same scale.

    REPRODUCED within ~2% relative; when the magnitude matches within the same
    tolerance but the sign differs, the pair is a sign-convention flip (e.g. the
    script computes training-vs-control while the paper reports control-vs-training)
    and counts as a match ('reproduced (sign-convention)'); 'reproduced in direction'
    when the signs agree but the magnitude differs by more than 2% (scale/metric
    mismatch); NOT REPRODUCED otherwise.  None when either side is missing/non-finite.
    """
    if a is None or b is None or not math.isfinite(a) or not math.isfinite(b):
        return None
    if b == 0:
        return "REPRODUCED" if abs(a) <= 0.005 else "NOT REPRODUCED"
    if abs(a - b) <= 0.02 * abs(b):
        return "REPRODUCED"
    if abs(a + b) <= 0.02 * abs(b):
        return SIGN_CONV
    if (a > 0) == (b > 0):
        return "reproduced in direction"
    return "NOT REPRODUCED"


def bespoke_csv_facts(key, out, err):
    """Aggregate facts from a bespoke script: csv rows (when written) + stdout scans."""
    f = {"kind": "bespoke", "rows": [], "match_counts": {"match": 0, "direction": 0,
                                                         "nomatch": 0, "nocompare": 0},
         "recalc_vs_reported": "", "domains": []}
    df, _cols = csv_columns_for(key)

    # huge stdout tables (generic): first non-empty lines are the analysis narrative
    narr = []
    for line in out.splitlines():
        s = line.strip()
        if not s:
            continue
        narr.append(s)
        if len(narr) >= 6:
            break
    f["narrative"] = " | ".join(narr[:6])

    if df is not None:
        label_col = next((c for c in ("effect_id", "effect", "experiment", "study",
                                      "dv", "outcome", "label", "contrast", "country",
                                      "condition", "item", "metric", "name", "quantity")
                          if c in df.columns), None)
        match_flags = [c for c in ("sign_match", "magnitude_match", "abs_magnitude_match")
                       if c in df.columns]
        reported_cols = [c for c in df.columns if str(c).startswith("reported")]
        reprod_candidates = [c for c in df.columns
                             if re.match(r"^(reproduced|recovered|recalc|replicated)", str(c))]
        for i, row in df.iterrows():
            label = str(row[label_col]) if label_col else f"row {i + 1}"
            dom = _infer_domain(label + " " + str(row.get("effect", "")) +
                                " " + str(row.get("dv", "")) + " " +
                                str(row.get("experiment", "")))
            note = str(row.get("note", "") or row.get("notes", "") or "")
            verdict = ""
            recalc_num = reported_num = None

            # explicit author match flags take precedence
            if match_flags:
                vals = [str(row[fl]).strip().lower() for fl in match_flags
                        if pd.notna(row[fl])]
                if vals and all(v == "true" for v in vals):
                    verdict = "REPRODUCED"
                elif any(v == "false" for v in vals):
                    verdict = "NOT REPRODUCED"

            # numeric pairs: reported_* vs reproduced_* with matching suffixes
            pairs = []
            for rc in reprod_candidates:
                for pc in reported_cols:
                    rcs, pcs = str(rc), str(pc)
                    if rcs == "reproduced" and pcs == "reported":
                        pairs.append((rcs, pcs))
                    elif rcs.startswith(("reproduced_", "recovered_", "recalc_",
                                         "replicated_")) and \
                            pcs.startswith("reported_") and \
                            rcs.split("_", 1)[1] == pcs.split("_", 1)[1]:
                        pairs.append((rcs, pcs))
            if not pairs and reported_cols and reprod_candidates:
                pairs.append((reprod_candidates[0], reported_cols[0]))

            sub = []          # [(verdict, a, b)] - the metric pair that drove the verdict
            first_pair = [None, None]
            for rc, pc in pairs:
                a, b = _numcell(row[rc]), _numcell(row[pc])
                if a is not None and first_pair[0] is None:
                    first_pair[0] = a
                if b is not None and first_pair[1] is None:
                    first_pair[1] = b
                if verdict == "":
                    v = _pair_verdict(a, b)
                    if v:
                        sub.append((v, a, b))

            # reported numbers embedded in the note (e.g. 'reported t=3.37 p=0.01');
            # p-values are never compared at tolerance (significance classes differ)
            if verdict == "" or sub:
                for met, val in re.findall(
                        r"(?:reported|paper)\s+([A-Za-z][\w.-]*)\s*=\s*(-?\d+(?:\.\d+)?)",
                        note):
                    met = met.strip().lower()
                    if met in ("p", "pval", "p-value", "sig", "significance"):
                        continue
                    col = next((c for c in ("reproduced_" + met, "recovered_" + met,
                                            "recalc_" + met)
                                if c in df.columns), None)
                    if col is None:
                        continue
                    a = _numcell(row[col])
                    if a is None:
                        continue
                    b = float(val)
                    if first_pair[0] is None:
                        first_pair[0] = a
                    if first_pair[1] is None:
                        first_pair[1] = b
                    if verdict == "":
                        v = _pair_verdict(a, b)
                        if v:
                            sub.append((v, a, b))

            if verdict != "":
                recalc_num, reported_num = first_pair
            else:
                chosen = None
                for want in ("NOT REPRODUCED", "REPRODUCED", SIGN_CONV,
                             "reproduced in direction"):
                    for v, a, b in sub:
                        if v == want:
                            verdict, chosen = want, (a, b)
                            break
                    if chosen:
                        break
                if chosen:
                    recalc_num, reported_num = chosen
                else:  # computed/reported but not comparable
                    recalc_num, reported_num = first_pair
            f["rows"].append({"label": label, "domain": dom,
                              "reported": _fmtval(reported_num),
                              "recalculated": _fmtval(recalc_num),
                              "verdict": verdict, "note": note, "csv": True})
        for r in f["rows"]:
            _tally(f["match_counts"], r["verdict"])
        f["recalc_vs_reported"] = _rows_to_text(f["rows"])
    else:
        f["recalc_vs_reported"] = "no repro_results.csv written by script"
    return f


def _numcell(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _fmtval(x):
    return "NA" if x is None else f"{x:.4f}".rstrip("0").rstrip(".")


def _tally(counts, verdict):
    key = {"REPRODUCED": "match", SIGN_CONV: "match",
           "reproduced in direction": "direction",
           "NOT REPRODUCED": "nomatch"}.get(verdict)
    if key:
        counts[key] += 1
    else:
        counts["nocompare"] += 1


def _rows_to_text(rows):
    if not rows:
        return "(no rows)"
    parts = []
    for r in rows[:8]:
        v = r["verdict"] or "no comparison"
        parts.append(f"{r['label']}: recalc={r['recalculated']} reported={r['reported']} [{v}]")
    if len(rows) > 8:
        parts.append(f"... +{len(rows) - 8} more rows")
    return "; ".join(parts)


def bespoke_stdout_match_table(out):
    """Shandler-style stdout table: effect_id, recovered, reported, sign_match, mag_match."""
    rows = []
    pat = re.compile(
        r"^\s*(\S.*?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+"
        r"(True|False)\s+(True|False)\s*$")
    for line in out.splitlines():
        m = pat.match(line)
        if m:
            label, rec, rep, sm, mm = m.groups()
            ok = sm == "True" and mm == "True"
            rows.append({"label": label.strip(),
                         "recalculated": f"{float(rec):.4f}",
                         "reported": f"{float(rep):.4f}",
                         "verdict": "REPRODUCED" if ok else "NOT REPRODUCED",
                         "domain": _infer_domain(label)})
    return rows


# ---------------------------------------------------------------------------
# status classification
# ---------------------------------------------------------------------------


def classify_wrapper_status(f):
    if f.get("has_data") is False:
        return "not reproducible"
    summary = f.get("summary", {})
    nr = len(summary.get("REPRODUCED (within ~2%, same-scale):", []))
    nd = len(summary.get("reproduced in direction (scale-mismatched coded metric):", []))
    nnr = len(summary.get("NOT REPRODUCED", []))
    ncmp = len(summary.get("NOT COMPARABLE (unsigned coded metric, no sign to compare):", []))
    nnc = len(summary.get("NOT CHECKABLE", []))
    f["n_reproduced"], f["n_direction"], f["n_not_reproduced"] = nr, nd, nnr
    f["n_not_comparable"], f["n_not_checkable"] = ncmp, nnc
    if nnr == 0 and (nr + nd) > 0:
        return "reproduced"
    if nnr > 0 and (nr + nd) > 0:
        return "partial"
    if nnr > 0:
        return "not reproduced"
    return "reported_only"


def classify_bespoke_status(f, md_verdict, err):
    counts = f.get("match_counts", {})
    nrows = len(f.get("rows", []))
    mt = len(f.get("stdout_match_rows", []))
    if _crash_evidence(err + " " + f.get("error", ""), 0)["crashed"]:
        return "crashed"
    n_comp = counts["match"] + counts["direction"] + counts["nomatch"]

    def md_fallback():
        if md_verdict:
            v = str(md_verdict).lower()
            if v.startswith("yes") or v in ("1", "true"):
                return "reproduced"
            if "partial" in v:
                return "partial"
            if v.startswith("no"):
                return "not reproduced"
        return "reported_only"

    if nrows > 0:
        if n_comp == 0:
            # nothing machine-comparable against reported values -> documented verdict
            return md_fallback()
        if counts["nomatch"] == 0:
            return "reproduced" if counts["match"] > 0 else "partial"
        return "partial" if (counts["match"] + counts["direction"]) > 0 \
            else "not reproduced"
    if mt > 0:
        ok = sum(1 for r in f["stdout_match_rows"] if r["verdict"] == "REPRODUCED")
        bad = mt - ok
        if bad == 0:
            return "reproduced"
        return "partial" if ok else "not reproduced"
    return md_fallback()


def success_of(status):
    return {"reproduced": "yes", "partial": "partial",
            "not reproduced": "no", "not reproducible": "no"}.get(status, "n/a")


def md_status(verdict):
    """Map a documented ``**reproduced:**`` verdict onto the status vocabulary."""
    if not verdict:
        return ""
    v = str(verdict).strip().lower()
    if v.startswith(("yes", "true", "match", "reproduced")) or v in ("y", "1"):
        return "reproduced"
    if v.startswith("partial") or v.startswith("partly"):
        return "partial"
    if v.startswith(("no", "false", "mismatch", "not reproduced")) or v in ("n", "0"):
        return "not reproduced"
    return ""


def _crash_evidence(err, rc):
    """A script counts as crashed only on hard evidence: nonzero exit, a raised
    subprocess error, or an actual traceback/exception on stderr (harmless warnings
    like pandas PerformanceWarning do NOT count)."""
    txt = str(err or "")
    hard = bool(re.search(
        r"Traceback \(most recent call last\)|(^|\n)[A-Za-z_][\w.]*(Error|Exception)\b|"
        r"Segmentation\s*fault|Killed\b|MemoryError|RecursionError|"
        r"\[run_reproducers:", txt))
    return {"crashed": rc not in (0, None) or hard,
            "error": re.sub(r"\s+", " ", txt).strip()}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default=None, help="only studies whose key contains this "
                                                 "substring (case-insensitive)")
    ap.add_argument("--no-run", action="store_true", help="aggregate from existing logs "
                                                          "in temp/reproduce_logs/")
    ap.add_argument("--timeout", type=int, default=900, help="per-script timeout (s)")
    ap.add_argument("--jobs", type=int, default=1, help="parallel subprocesses (default 1)")
    ap.add_argument("--no-persist", action="store_true",
                    help="do not copy facts/results/logs into Datasets/ and Results/repro_logs/")
    ap.add_argument("--kind", choices=["wrapper", "bespoke"], default=None,
                    help="run only wrapper (repro_lib thin) or only bespoke scripts")
    args = ap.parse_args()

    scripts = enumerate_scripts(args.only, args.kind)
    print(f"run_reproducers: {len(scripts)} reproduction script(s)"
          + (f" (filter: '{args.only}')" if args.only else ""))

    persist_reports = []
    facts = []

    def _persist(key, f):
        if args.no_persist:
            return
        persist_reports.append(persist_study_artifacts(key, f))

    if args.no_run:
        for key, _script, kind in scripts:
            log = load_log(key)
            if log is None:
                f = {"key": key, "script_kind": kind, "error": "",
                     "rc": None, "runtime": float("nan"), "log_path": "",
                     "status": "not run", "success": "n/a", "ran": False,
                     "exclusions": "no log", "analysis": "",
                     "recalc_vs_reported": "", "documented_verdict": "",
                     "documented_reason": "", "domains": []}
                facts.append(f)
                _persist(key, f)
                continue
            out, err, rc, runtime = log
            f = aggregate(key, kind, out, err, rc, runtime, "")
            facts.append(f)
            _persist(key, f)
    else:
        total = len(scripts)
        if args.jobs > 1:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def work(item):
                i, (key, script, _kind) = item
                return run_one(key, script, args.timeout, i, total, args.jobs)

            # Persist each study as soon as it finishes so a long campaign is resumable
            # and temp volatility can never lose the only machine-readable numbers.
            with ThreadPoolExecutor(max_workers=args.jobs) as ex:
                futs = {ex.submit(work, item): item
                        for item in enumerate(scripts, 1)}
                for fut in as_completed(futs):
                    _i, (key, _script, kind) = futs[fut]
                    res = fut.result()
                    out, err, rc, runtime = load_log(key)
                    f = aggregate(key, kind, out or "", err or "", rc,
                                  runtime if runtime == runtime else float("nan"),
                                  res.get("error", ""))
                    facts.append(f)
                    _persist(key, f)
            facts.sort(key=lambda x: x["key"].lower())
        else:
            for i, (key, script, kind) in enumerate(scripts, 1):
                res = run_one(key, script, args.timeout, i, total, args.jobs)
                out, err, rc, runtime = load_log(key)
                f = aggregate(key, kind, out or "", err or "", rc,
                              runtime if runtime == runtime else float("nan"),
                              res.get("error", ""))
                facts.append(f)
                _persist(key, f)
    if persist_reports:
        n_copied = sum(len(r["copied"]) for r in persist_reports)
        n_same = sum(len(r["same"]) for r in persist_reports)
        print(f"persisted: {n_copied} artifact(s) copied, {n_same} already up to date "
              f"-> Datasets/<Key>/ + Results/repro_logs/")

    rep_rows, dom_rows = [], []
    for f in facts:
        rep_rows.append(_repro_sheet_row(f))
        dom_rows.extend(_domain_sheet_rows(f))

    rep_df = pd.DataFrame(rep_rows) if rep_rows else pd.DataFrame(
        columns=_REPRO_COLS)
    dom_df = pd.DataFrame(dom_rows) if dom_rows else pd.DataFrame(
        columns=_DOMAIN_COLS)

    out_xlsx = os.path.join(RESULTS, "reproduction_summary.xlsx")
    with pd.ExcelWriter(out_xlsx, engine="openpyxl") as xw:
        rep_df.to_excel(xw, sheet_name="Reproduction", index=False)
        dom_df.to_excel(xw, sheet_name="Domains", index=False)
        # column widths (rough, for readability)
        for sheet, widths in (("Reproduction", {"key": 34, "exclusions": 60,
                                                "analysis_applied": 60,
                                                "recalc_vs_reported": 80,
                                                "documented_verdict": 30}),
                              ("Domains", {"key": 34, "contrast": 60, "note": 70})):
            ws = xw.sheets[sheet]
            for col, w in widths.items():
                if col in rep_df.columns if sheet == "Reproduction" else col in dom_df.columns:
                    try:
                        ix = (rep_df.columns.get_loc(col) if sheet == "Reproduction"
                              else dom_df.columns.get_loc(col))
                        ws.column_dimensions[ws.cell(row=1, column=ix + 1).column_letter].width = w
                    except Exception:
                        pass
    print(f"\nwrote {out_xlsx}  "
          f"({len(rep_df)} study rows x {len(rep_df.columns)} cols, "
          f"{len(dom_df)} domain rows)")

    json.dump({"reproduction": rep_rows, "domains": dom_rows,
               "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
               "no_run": args.no_run, "only": args.only},
              open(os.path.join(TMP, "reproduce_aggregate.json"), "w", encoding="utf-8"),
              indent=1, ensure_ascii=False)
    print("wrote temp/reproduce_aggregate.json")

    _console_summary(rep_rows)
    return 0


_REPRO_COLS = ["key", "script_kind", "status", "success", "machine_status",
               "documented_status", "machine_disagrees", "has_data", "exclusions",
               "analysis_applied", "recalc_vs_reported", "documented_verdict",
               "documented_reason", "n_contrasts", "n_reproduced", "n_direction",
               "n_not_reproduced", "n_not_comparable", "n_not_checkable", "n_recalc_rows",
               "runtime_s", "returncode", "error",
               # parse_wrapper collects these but they were never surfaced before:
               "error_note", "estimator", "exclusions_applied", "notes", "log_file"]


def _repro_sheet_row(f):
    st = f.get("status", "reported_only")
    doms = f.get("domains", [])
    _notes = f.get("notes", "")
    if isinstance(_notes, (list, tuple)):
        _notes = "; ".join(str(x) for x in _notes)
    return {
        "key": f["key"],
        "script_kind": f.get("script_kind", ""),
        "status": st,
        "success": f.get("success", success_of(st)),
        "machine_status": f.get("machine_status", ""),
        "documented_status": f.get("documented_status", ""),
        "machine_disagrees": f.get("machine_disagrees", ""),
        "has_data": "yes" if f.get("has_data") else ("no" if f.get("has_data") is False else ""),
        "exclusions": f.get("exclusions", ""),
        "analysis_applied": f.get("analysis", ""),
        "recalc_vs_reported": f.get("recalc_vs_reported", ""),
        "documented_verdict": f.get("documented_verdict", ""),
        "documented_reason": (f.get("documented_reason") or "")[:400],
        "n_contrasts": (f.get("n_coded") if f.get("n_coded") is not None
                        else (len(f.get("rows") or f.get("stdout_match_rows") or [])
                              or len(doms))),
        "n_reproduced": f.get("n_reproduced", 0),
        "n_direction": f.get("n_direction", 0),
        "n_not_reproduced": f.get("n_not_reproduced", 0),
        "n_not_comparable": f.get("n_not_comparable", 0),
        "n_not_checkable": f.get("n_not_checkable", 0),
        "n_recalc_rows": f.get("n_rows") if f.get("n_rows") is not None
        else (len([d for d in doms
                   if d.get("recalc_twin") or d.get("recalculated")])),
        "runtime_s": f.get("runtime", ""),
        "returncode": f.get("rc", ""),
        "error": (f.get("error") or "")[:500],
        "error_note": (f.get("error_note") or "")[:400],
        "estimator": f.get("estimator", ""),
        "exclusions_applied": f.get("exclusions_applied", ""),
        "notes": (str(_notes) if _notes else "")[:600],
        "log_file": f.get("log_path", ""),
    }


_DOMAIN_COLS = ["key", "domain", "result", "contrast", "coded_effect", "recalc_twin",
                "note", "source"]


def _domain_sheet_rows(f):
    rows = []
    for d in f.get("domains", []):
        rows.append({
            "key": f["key"],
            "domain": d.get("domain", ""),
            "result": d.get("verdict", ""),
            "contrast": d.get("contrast", d.get("label", "")),
            "coded_effect": d.get("coded_effect", d.get("reported", "")),
            "recalc_twin": d.get("recalc_twin", d.get("recalculated", "")),
            "note": (d.get("note") or "")[:400],
            "source": d.get("source", ""),
        })
    return rows


def aggregate(key, kind, out, err, rc, runtime, run_error):
    """Turn one captured report into the per-study fact dict (wrapper or bespoke)."""
    f = {"key": key, "script_kind": kind, "error": (err or "").strip() or run_error,
         "rc": rc, "runtime": runtime, "domains": [], "has_data": None,
         "log_path": log_path_for(key)}
    _md_txt, md_verdict, md_reason = read_md(key)
    f["documented_verdict"] = md_verdict or ""
    f["documented_reason"] = md_reason or ""

    if not out.strip():
        f["machine_status"] = "crashed" if rc not in (0, None) else "reported_only"
        doc = md_status(md_verdict)
        f["documented_status"] = doc
        f["status"] = (f["machine_status"] if f["machine_status"] == "crashed"
                       else (doc or f["machine_status"]))
        f["success"] = success_of(f["status"])
        f["machine_disagrees"] = ("yes" if doc and f["status"] != f["machine_status"]
                                  else "")
        f["exclusions"] = "n/a (no output)"
        return f

    f["crashed"] = _crash_evidence(f["error"], rc)["crashed"]
    if kind == "wrapper":
        p = parse_wrapper(out)
        f.update(p)
        st = classify_wrapper_status(f)
        if f["crashed"] and st != "not reproducible":
            st = "crashed" if st in ("reproduced", "partial", "not reproduced",
                                     "reported_only") else st
        f["status"] = st
        f["success"] = success_of(st)
        f["has_data"] = p.get("has_data")
        for c in p.get("contrasts", []):
            f["domains"].append({"domain": c["domain"], "verdict": c["verdict"],
                                 "contrast": c.get("contrast", ""),
                                 "coded_effect": c.get("coded_effect", ""),
                                 "recalc_twin": c.get("recalc_twin", ""),
                                 "note": c.get("note", ""), "source": "repro report"})
        rows_txt = "; ".join(
            f"{r['domain']}: d={r['d']:.4f} ({r['timepoint']})" for r in p["recalc_rows"])
        f["recalc_vs_reported"] = ((f"rows analysed={f['n_rows']}; " if f["n_rows"] else "") +
                                   (rows_txt + " | " if rows_txt else "") +
                                   f"{f.get('n_coded', '?')} coded contrasts: "
                                   f"{f.get('n_reproduced', 0)} reproduced, "
                                   f"{f.get('n_direction', 0)} direction-only, "
                                   f"{f.get('n_not_reproduced', 0)} NOT reproduced, "
                                   f"{f.get('n_not_comparable', 0)} not comparable, "
                                   f"{f.get('n_not_checkable', 0)} not checkable")
    else:
        p = bespoke_csv_facts(key, out, err)
        p["stdout_match_rows"] = bespoke_stdout_match_table(out)
        f.update(p)  # before the domain rows: p carries its own (empty) 'domains' key
        # csv rows take precedence; stdout table fills when no csv rows
        rows = p["rows"] or p["stdout_match_rows"]
        for r in rows:
            f["domains"].append({"domain": r.get("domain", "other"),
                                 "verdict": r.get("verdict", ""),
                                 "contrast": r.get("label", ""),
                                 "coded_effect": r.get("reported", ""),
                                 "recalc_twin": r.get("recalculated", ""),
                                 "note": r.get("note", ""),
                                 "source": "repro_results.csv" if p["rows"]
                                 else "stdout table"})
        # md reproduced-vs-reported table as a documented cross-check
        for r in md_repro_table(key):
            note = "documented in Reproduction/" + key + ".md"
            if r.get("match_raw"):
                note += f"; Match?={r['match_raw']}"
            f["domains"].append({"domain": _infer_domain(r["label"]),
                                 "verdict": r["verdict"], "contrast": r["label"],
                                 "coded_effect": r["reported"],
                                 "recalc_twin": r["reproduced"],
                                 "note": note, "source": "md table"})
        st = classify_bespoke_status(p, md_verdict, f["error"])
        if f["crashed"] and st != "not reproducible":
            st = "crashed"
        f["status"] = st
        f["success"] = success_of(st)
        f["has_data"] = True
        if not f["recalc_vs_reported"]:
            rows_txt = "; ".join(
                f"{r['label']}: recalc={r['recalculated']} reported={r['reported']} "
                f"[{r['verdict'] or 'no comparison'}]" for r in rows[:8])
            f["recalc_vs_reported"] = rows_txt or "no structured results parsed from output"
        if md_verdict:
            f["recalc_vs_reported"] += f" | documented: {md_verdict}"

    # ---------------- final status ----------------
    # The machine status comes from the script's own output; the documented verdict in
    # Reproduction/<Key>.md is the human-verified conclusion for the WHOLE study (it
    # also accounts for parts the machine cannot recompute, e.g. missing source data).
    # Precedence: a crash or absent participant data always wins; otherwise the
    # documented verdict is the headline, with the machine status kept alongside.
    f["machine_status"] = f.get("status", "reported_only")
    # read_md is authoritative here: f.update(parse_wrapper(...)) may have carried a
    # truncated/absent copy of the same line out of the printed report.
    f["documented_verdict"] = md_verdict or ""
    if md_reason:
        f["documented_reason"] = md_reason
    doc = md_status(md_verdict)
    f["documented_status"] = doc
    if f["machine_status"] == "crashed":
        final = "crashed"
    elif f.get("has_data") is False:
        final = "not reproducible"
    elif doc:
        final = doc
    else:
        final = f["machine_status"]
    f["machine_disagrees"] = ("yes" if doc and final != f["machine_status"]
                              else "")
    f["status"] = final
    f["success"] = success_of(final)

    if not f.get("recalc_vs_reported"):
        f["recalc_vs_reported"] = "(nothing parsed)"
    return f


def _console_summary(rows):
    from collections import Counter
    cnt = Counter(r["status"] for r in rows)
    print("\n" + "=" * 60)
    print("REPRODUCTION SUMMARY")
    print("=" * 60)
    for s in STATUSES:
        print(f"  {s:<18} {cnt.get(s, 0):>4}")
    n_yes = sum(1 for r in rows if r["success"] == "yes")
    n_part = sum(1 for r in rows if r["success"] == "partial")
    print(f"  {'total':<18} {len(rows):>4}")
    print(f"  success=yes: {n_yes}   success=partial: {n_part}")
    dis = [r for r in rows if r.get("machine_disagrees") == "yes"]
    if dis:
        print(f"\ndocumented verdict overrides the machine status ({len(dis)}):")
        for r in dis[:20]:
            print(f"  - {r['key']}: md={r['status']} vs machine={r['machine_status']}")
        if len(dis) > 20:
            print(f"  ... and {len(dis) - 20} more (see the machine_disagrees column)")
    crashes = [r["key"] for r in rows if r["status"] == "crashed"]
    if crashes:
        print("\ncrashed / failed:")
        for k in crashes:
            print("  -", k)


if __name__ == "__main__":
    sys.exit(main())