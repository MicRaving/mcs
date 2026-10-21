"""run_analysis.py - one entry point for the campaign analysis pipeline.

Runs the analysis scripts (now under ``Scripts/analysis/``) in the correct order,
with a shared environment (all intermediates in ``temp/``, every product in
``Results/``).  Nothing is written to the project root.

Steps
  1. harmonize   Scripts/analysis/run_harmonizers.py      -> Results/harmonization_mapping.xlsx
  2. combine     Scripts/combine_harmonized.py            -> Results/combined_harmonized.csv.gz
  3. reproduce   Scripts/analysis/run_reproducers.py      -> Results/reproduction_summary.xlsx
  4. persist     Scripts/persist_repro_artifacts.py       -> Datasets/<Key>/ + Results/repro_logs/
  5. dossier     Scripts/build_repro_dossier.py           -> Results/repro_dossier/ + index
  6. meta        Scripts/analysis/metaanalysis.py         -> Results/metaanalysis.xlsx
  7. sdt         Scripts/analysis/reanalysis.py           -> Results/reanalysis.xlsx
  8. state       Scripts/analysis/state.py                -> Results/state.json
  9. suite       Scripts/analysis_multiverse_meta.py, analysis_multiverse_sdt.py,
                 publication_bias.py, figures.py, build_manuscript_report.py
                 -> Results/ (adds Suite_* sheets, Analysis_Suite_Results.xlsx,
                    figures/, manuscript_tables.xlsx, apa_report_phase4.docx)

Usage
  python run_analysis.py                 # core: meta -> sdt -> state
  python run_analysis.py --harmonize     # also re-run every harmonizer (+ combine)
  python run_analysis.py --reproduce     # also refresh the reproduction summary
  python run_analysis.py --suite         # also run the analysis suite
  python run_analysis.py --all           # everything, in order
  python run_analysis.py --only meta,sdt # explicit subset
  python run_analysis.py --list          # show the steps and exit
"""

import argparse
import os
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(ROOT, "Scripts")
ANALYSIS = os.path.join(SCRIPTS, "analysis")
RESULTS = os.path.join(ROOT, "Results")
TEMP = os.path.join(ROOT, "temp")

# name -> (list of script paths, extra argv)
STEPS = {
    "harmonize": ([os.path.join(ANALYSIS, "run_harmonizers.py")], []),
    "combine": ([os.path.join(SCRIPTS, "combine_harmonized.py")], []),
    "reproduce": ([os.path.join(ANALYSIS, "run_reproducers.py")], []),
    "persist": ([os.path.join(SCRIPTS, "persist_repro_artifacts.py")], []),
    "dossier": ([os.path.join(SCRIPTS, "build_repro_dossier.py")], []),
    "meta": ([os.path.join(ANALYSIS, "metaanalysis.py")], []),
    "sdt": ([os.path.join(ANALYSIS, "reanalysis.py")], ["--no-harmonize"]),
    "state": ([os.path.join(ANALYSIS, "state.py")], []),
    "suite": ([os.path.join(SCRIPTS, "analysis_multiverse_meta.py"),
               os.path.join(SCRIPTS, "analysis_multiverse_sdt.py"),
               os.path.join(SCRIPTS, "publication_bias.py"),
               os.path.join(SCRIPTS, "figures.py"),
               os.path.join(SCRIPTS, "build_manuscript_report.py")], []),
}
# The coverage matrix is authored by workstream W1-B; register it only when present so
# `run_analysis.py` never fails on a missing optional step.
_COVERAGE = os.path.join(SCRIPTS, "harmonization_coverage.py")
if os.path.isfile(_COVERAGE):
    STEPS["coverage"] = ([_COVERAGE], [])

# Report-only audits (registered only when present).  These never abort the run:
# a non-zero exit is printed as a WARNING and the audit products are still written.
_AUDIT_SCRIPTS = [p for p in (
    os.path.join(SCRIPTS, "harmonization_coverage.py"),
    os.path.join(SCRIPTS, "audit_harmonizers.py"),
    os.path.join(SCRIPTS, "audit_polarity.py"),
    os.path.join(SCRIPTS, "audit_assignments.py"),
    os.path.join(SCRIPTS, "validate_repro_dossier.py"),
    os.path.join(SCRIPTS, "parse_reproduction_reports.py"),
    os.path.join(SCRIPTS, "validate_extraction.py"),
    os.path.join(SCRIPTS, "validate_sanity.py"),
) if os.path.isfile(p)]
if _AUDIT_SCRIPTS:
    STEPS["audit"] = (_AUDIT_SCRIPTS, [])

# Hard gates: a failure here means the pooled products must not be trusted.  Each
# gate has its own argv, so the step is special-cased in ``run_step``.
GATES = [(label, script, argv) for label, script, argv in (
    ("polarity", os.path.join(SCRIPTS, "validate_polarity.py"), []),
    ("fully-within", os.path.join(SCRIPTS, "validate_fully_within.py"), []),
    ("B_validate", os.path.join(SCRIPTS, "B_validate.py"), ["--dir", "Extraction"]),
    ("parity", os.path.join(SCRIPTS, "parity_check.py"), []),
    ("scheme", os.path.join(SCRIPTS, "validate_scheme.py"), ["--no-corpus"]),
) if os.path.isfile(script)]
if GATES:
    STEPS["gate"] = ([], [])  # sentinel; run_step() dispatches to GATES

DEFAULT = ["meta", "sdt", "state"]
# Canonical execution order for every step (documented in the module docstring).
# reproduce comes BEFORE meta/sdt so the reproduction summary is refreshed from the
# same data the pools use; persist+dossier come right after reproduce; the audits and
# the hard gates run last.
CANON_ORDER = ["harmonize", "combine", "reproduce", "persist", "dossier", "coverage",
               "meta", "sdt", "state", "suite", "audit", "gate"]

# Steps whose non-zero exit is a WARNING, not an abort.
TOLERANT = {"audit"}



def _env():
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPYCACHEPREFIX"] = os.path.join(TEMP, "pycache")
    # The documented final run: bypass the concurrent-writer freshness guard so a
    # just-finished harmonizer pass can never silently truncate the re-analysis.
    env["SUITE_FORCE"] = "1"
    return env


def run_step(name, env):
    if name == "gate":
        rc = 0
        for label, script, argv in GATES:
            cmd = [sys.executable, "-X", "utf8", script, *argv]
            print(f"\n=== gate/{label}: {' '.join(os.path.relpath(c, ROOT) for c in cmd[3:])} ===")
            proc = subprocess.run(cmd, cwd=ROOT, env=env)
            if proc.returncode != 0:
                print(f"ERROR: gate {label} failed (exit {proc.returncode})")
                rc = proc.returncode
        return rc
    scripts, extra = STEPS[name]
    for script in scripts:
        if not os.path.isfile(script):
            print(f"ERROR: {name}: missing {os.path.relpath(script, ROOT)}")
            return 1
        cmd = [sys.executable, "-X", "utf8", script, *extra]
        print(f"\n=== {name}: {' '.join(os.path.relpath(c, ROOT) for c in cmd[3:])} ===")
        proc = subprocess.run(cmd, cwd=ROOT, env=env)
        if proc.returncode != 0:
            if name in TOLERANT:
                print(f"WARNING: {name} reported problems (exit {proc.returncode}); "
                      f"products were still written, see Results/")
                continue
            print(f"ERROR: {name} failed (exit {proc.returncode})")
            return proc.returncode
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--harmonize", action="store_true",
                    help="run every harmonizer first (implies --combine)")
    ap.add_argument("--combine", action="store_true",
                    help="rebuild the combined wide dataset")
    ap.add_argument("--reproduce", action="store_true",
                    help="refresh the reproduction summary")
    ap.add_argument("--suite", action="store_true",
                    help="also run the analysis suite (multiverse + publication bias + figures)")
    ap.add_argument("--all", action="store_true", help="every step, in order")
    ap.add_argument("--validate", action="store_true",
                    help="also run the report-only audits and the hard gates")
    ap.add_argument("--only", default=None,
                    help="comma-separated subset of: " + ", ".join(STEPS))
    ap.add_argument("--list", action="store_true", help="list the steps and exit")
    args = ap.parse_args()

    if args.list:
        for n in CANON_ORDER:
            if n not in STEPS:
                continue
            if n == "gate":
                print(f"  {'gate':10s} " + ", ".join(lbl for lbl, _s, _a in GATES))
                continue
            print(f"  {n:10s} {', '.join(os.path.relpath(s, ROOT) for s in STEPS[n][0])}")
        return 0

    if args.only:
        steps = [s.strip() for s in args.only.split(",") if s.strip()]
        unknown = [s for s in steps if s not in STEPS]
        if unknown:
            print("unknown step(s):", ", ".join(unknown))
            return 2
    else:
        # Build the requested set, then emit it in the canonical order so the
        # documented sequence (harmonize, combine, reproduce, meta, sdt, state) is
        # always respected, whichever flags are given.
        wanted = set(DEFAULT)
        if args.harmonize or args.all:
            wanted |= {"harmonize", "combine"}
        if args.combine:
            wanted |= {"combine"}
        if args.reproduce or args.all:
            wanted |= {"reproduce", "persist", "dossier"}
        if args.suite or args.all:
            wanted |= {"suite"}
        if args.all or args.validate:
            wanted |= {"audit", "gate"}
        steps = [s for s in CANON_ORDER if s in wanted]

    os.makedirs(RESULTS, exist_ok=True)
    os.makedirs(TEMP, exist_ok=True)
    env = _env()
    print(f"root:    {ROOT}")
    print(f"results: {RESULTS}")
    print(f"steps:   {', '.join(steps)}")
    for name in steps:
        rc = run_step(name, env)
        if rc:
            return rc
    print(f"\ndone. products in {os.path.relpath(RESULTS, ROOT)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
