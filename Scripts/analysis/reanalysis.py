"""Re-analysis of the unified study datasets — signal-detection framework.

Per project decision:
- The study data stays WIDE. Each dataset keeps its own wide `<Study Key> - harmonized.csv`
  (one row per participant/session, one column per variable, covariates included). It is NOT
  melted for the merge.
- Only the SDT analysis melts the `dv_*_01` columns to long, and it EXCLUDES covariates.
  The full union (~19k column names x 2.26M rows) is not materialised; instead one combined
  wide table is written keeping only the columns present in >= 3 studies
  (`combined_harmonized.csv.gz` + `combined_harmonized_variables.csv`; see
  `Scripts/combine_harmonized.py`) and the SDT melt is streamed per study.

Steps
1. Run every `Datasets/<Study Key>/harmonizer.py` (writes the wide file + variable sidecar).
2. Catalogue the wide datasets -> `reanalysis_all.csv` (study, artifact, file, rows, n_dv, n_cov).
2b. Build the combined wide table (columns in >= 3 studies) -> `combined_harmonized.csv.gz`
    (+ `combined_harmonized_variables.csv`); skip with `--no-combine`.
3. Per study, melt `dv_*_01` to long (no covariates), split participants by the harmonized
   `condition` column into the treatment arm and the no-misinformation-intervention control
   arm, and compute per artifact/domain/timepoint/arm an SDT discrimination index
   d' = z(hit rate) - z(false-alarm rate), paired within participant (per-participant d'_i,
   then var(d'_i)/n) when participants are identified.  Domains are resolved via
   Scripts/domain_restriction.py (blank / off-vocabulary labels).
4. Pool the INTERVENTION-vs-CONTROL difference in d' (`d'_treatment - d'_control`) with a
   random-effects (DerSimonian-Laird) model and write `reanalysis.xlsx`
   (sheets: Discrimination = intervention effect, Discrimination_level = arm-pooled level
   auxiliary, MetaResults, MetaResults_restricted, Domain_Counts, RunAudit).  Cells without a
   control arm are excluded from the intervention effect (counted in RunAudit).

TIMEPOINT SEPARATION (2026-09-17, see Scripts/TIMEPOINT_SEPARATION_NOTE.md)
---------------------------------------------------------------------------
The three questions are distinct and are NEVER pooled together:

    posttest (immediate)  -> is the intervention effective?  (timepoint == post / blank)
    follow-up (persistence) -> do effects persist?           (timepoint == followup)
    DiD (pre->post)       -> pretest-corrected change        (post minus pre, per cell)

The raw per-timepoint `Discrimination` sheet is retained (it is informative), but the
`MetaResults` headline is split by question: it contains no timepoint-mixing ALL row.
Pretest/baseline rows are used ONLY as the DiD baseline; they are never outcomes.  The
DiD here is the pretest-corrected change in the intervention effect (post minus pre
intervention-vs-control effect within a study x artifact x domain cell); the arm-pooled
discrimination LEVEL is retained as the auxiliary `Discrimination_level` sheet.
5. The restricted model keeps only the misinformation-susceptibility domains (belief_accuracy,
   credibility, sharing_intention, technique_recognition) and is reported per question in
   `MetaResults_restricted` (separately from the unrestricted `MetaResults`).

Run:  python reanalysis.py                  # runs harmonizers, then re-analyses
      python reanalysis.py --no-harmonize   # re-analyse existing harmonized files only
      python reanalysis.py --no-combine     # skip the combined wide table
      python reanalysis.py --no-exclusions  # skip every <Key> - exclusions.csv rule
"""
import glob
import math
import os
import re
import fnmatch
import subprocess
import sys
import time

import numpy as np
import pandas as pd

import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))     # Scripts/analysis
SCRIPTS = os.path.dirname(_HERE)                       # Scripts
B = os.path.dirname(SCRIPTS)                           # project root
RESULTS = os.path.join(B, "Results")
os.makedirs(RESULTS, exist_ok=True)
_TMP = os.path.join(B, "temp")
os.makedirs(_TMP, exist_ok=True)
tempfile.tempdir = _TMP

DS = os.path.join(B, "Datasets")
OUT = os.path.join(RESULTS, "reanalysis.xlsx")
CATALOG = os.path.join(RESULTS, "reanalysis_all.csv")

# Shared controlled-vocabulary / restriction logic (see Scripts/DOMAIN_RESTRICTION_NOTE.md)
# and non-independent experiment exclusions (see Scripts/FULLTEXT_DUPLICATE_LOG.md).
sys.path.insert(0, SCRIPTS)
import domain_restriction as DR  # noqa: E402
import study_exclusions as SE  # noqa: E402
import suite_common as SC  # noqa: E402  (dependence-aware pooling: 3-level + RVE)
import analysis_log as AL  # noqa: E402  (durable drop log, Results/analysis_log.jsonl)

try:  # console-safe output for study names with non-cp1252 characters
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def dl_random(yi, vi):
    """DerSimonian-Laird pooled estimate (canonical stat_core.dl_random)."""
    return SC.dl_random(yi, vi)


_PROBIT_EPS = SC._PROBIT_EPS


def _probit(p, n=None):
    """Inverse standard-normal CDF (stat_core.probit; D4 log-linear correction).

    ``n`` is the number of trials/items behind a rate; passing it enables the
    Hautus (1995) ``(x*n + 0.5)/(n + 1)`` correction so 0/1 rates are handled.
    """
    return float(SC._probit(p, n=n))


def dprime(hr, cr, n=None, n_fa=None):
    """SDT discrimination ``d' = z(HR) - z(FAR)`` from a hit rate and a
    correct-rejection (true-negative) rate, where ``FAR = 1 - cr``.

    D4: pass ``n`` (the number of trials/items behind the hit rate) and, when it
    differs, ``n_fa`` (trials behind the false-alarm rate) to enable the Hautus
    (1995) log-linear correction.
    """
    return (SC._probit(hr, n=n)
            - SC._probit(1.0 - cr, n=n if n_fa is None else n_fa))


def run_harmonizers():
    ran = failed = 0
    failed_keys = []
    for d in sorted(glob.glob(os.path.join(DS, "*"))):
        h = os.path.join(d, "harmonizer.py")
        if os.path.isfile(h):
            proc = subprocess.run([sys.executable, "-X", "utf8", h],
                                  capture_output=True, check=False)
            ran += 1
            if proc.returncode != 0:
                # A crashed harmonizer would otherwise leave a stale CSV that the
                # analysis silently reads as if it were current.
                failed += 1
                failed_keys.append(os.path.basename(d))
                err = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
                tail = err[-1] if err else f"exit {proc.returncode}"
                print(f"  ! harmonizer failed ({os.path.basename(d)}): " + tail)
                # Durable trace + RunAudit: a crashed harmonizer means the analysed CSV
                # may be stale, so it must be visible after the run (audit F3).
                AL.log("harmonizer_failed", study=os.path.basename(d),
                       returncode=proc.returncode,
                       stderr_tail=" | ".join(err[-3:]) if err else f"exit {proc.returncode}")
            # Veracity-dependent polarity (Scripts/DV_POLARITY_FIX.md): re-apply the
            # shared rule to the just-written sidecar so every regeneration path
            # (including harmonizers whose semhom hook runs before main) is covered.
            side = os.path.join(d, os.path.basename(d) + " - harmonized_variables.csv")
            var = os.path.join(d, os.path.basename(d) + " - variables.csv")
            try:
                import dv_polarity as _dvp
                _dvp.normalize_sidecar_file(side)
                _dvp.normalize_variables_file(var, side)
            except Exception:
                pass
            # ---- Track-B runner post-hook: coder variable assignments ----------
            # Coder-owned assignments live in <Key> - variable_assignments.csv and
            # are applied here (beside the polarity rule) so the 264 harmonizers
            # need no changes.  The module is authored by workstream W1-B and may
            # not exist yet, so this is defensive: it runs only when the
            # assignments file exists and the module imports.
            va_path = os.path.join(d, os.path.basename(d) + " - variable_assignments.csv")
            if os.path.isfile(va_path):
                try:
                    import variable_assignments as _va
                    # apply_files() reads the sidecar+wide, applies the coder
                    # assignments and writes them back atomically (apply() alone
                    # only returns the frames).
                    _va.apply_files(os.path.basename(d), d)
                except ImportError:
                    pass
                except Exception as _e:  # never crash the runner on a post-hook
                    AL.log("variable_assignments_failed", study=os.path.basename(d),
                           error=f"{type(_e).__name__}: {_e}")
    print(f"ran harmonizers: {ran}" + (f" ({failed} failed)" if failed else ""))
    return failed_keys


def meta_for(folder):
    p = os.path.join(folder, os.path.basename(folder) + " - harmonized_variables.csv")
    if not os.path.isfile(p):
        return {}
    d = pd.read_csv(p, encoding="utf-8-sig", low_memory=False)
    try:
        # Composite double-weight guard: clear veracity on nested sub-items that
        # duplicate a tagged composite in the same (domain, veracity, timepoint).
        import dv_dedup
        d, _pruned = dv_dedup.prune_sidecar(d)
    except Exception:
        pass
    out = {}
    for _, r in d.iterrows():
        uv = str(r["uniform_variable"])
        out[uv] = (DR.resolve_sidecar_domain(r.get("dv_domain"), uv),
                   str(r.get("veracity", "")), str(r.get("timepoint", "")),
                   str(r.get("dv_polarity", "")))
    return out


_FORCE = False          # set when a just-finished harmonizer run is detected / SUITE_FORCE
_NO_REVERSAL = False    # toggle used by the polarity self-check
# Veracity fallback for BLANK polarity (W6-SDT audit / sensitivity only): when
# True, a false-veracity *endorsement* DV whose sidecar polarity is blank is
# reverse-coded (1 - x) as the veracity-dependent rule intends.  Left False in
# production so the primary result keeps the "ambiguous -> leave as coded"
# policy; `Scripts/audit_sdt_reanalysis.py` toggles it for the sensitivity.
_VERACITY_FALLBACK = False
_FALLBACK_DOMAINS = {"sharing_intention", "engagement_behavior", "credibility"}
# A difference/change-score DV carries no stimulus veracity (contract.dv_veracity
# returns "" for difference/composite scores).  Some sidecars still tag one with
# true/false, which made `_cell_dprime` pair-split a within-person CHANGE score
# (e.g. Maertens et al. (2021) `..._diff_t2_t1`) as a hit / false-alarm
# component and pool it as a posttest OUTCOME.  Such rows are dropped from the
# SDT melt defensively (the harmonizer/sidecar is the source of the tag and is
# not touched here); set False to reproduce the pre-fix behaviour.
_DROP_MISLABELED_DIFF = True
_DIFF_SCORE_RE = re.compile(
    r"(_diff|difference|minus|\bnet\b|\bgap\b|d_prime|dprime|signal_detection)", re.I)
# Drop-logging is active for the primary collect_datasets pass only; the polarity
# self-check re-runs collect_datasets and would otherwise duplicate every record.
_LOG_ACTIVE = True


def _dlog(kind, **fields):
    """AL.log gated on the primary collect_datasets pass (see _LOG_ACTIVE)."""
    if _LOG_ACTIVE:
        AL.log(kind, **fields)


def _fresh_window():
    """Default concurrent-write window (s); override with ``SUITE_FRESH_SECS``."""
    try:
        return float(os.environ.get("SUITE_FRESH_SECS", "120"))
    except (TypeError, ValueError):
        return 120.0


def fresh(path, seconds=None):
    """True if the file was modified within `seconds` (concurrent-session guard).

    The default window is deliberately short (120 s): it protects against a
    genuinely concurrent writer without treating the whole corpus as "fresh" after
    a just-finished harmonizer pass.  ``SUITE_FORCE=1`` (the documented final run)
    or a detected recent ``run_harmonizers.py`` flag disables the guard.
    """
    if _FORCE or os.environ.get("SUITE_FORCE"):
        return False
    if seconds is None:
        seconds = _fresh_window()
    try:
        return (time.time() - os.path.getmtime(path)) < seconds
    except OSError:
        return False


_HARMONIZER_FLAG = os.path.join(_TMP, "RUN_HARMONIZERS_DONE.flag")


def recent_harmonizer_run(minutes=30):
    """Age in seconds of a recent ``run_harmonizers.py`` completion flag, or None."""
    try:
        age = time.time() - os.path.getmtime(_HARMONIZER_FLAG)
    except OSError:
        return None
    return age if 0 <= age < minutes * 60 else None


def time_bucket(tp):
    """Normalise a sidecar `timepoint` to one of pre / post / followup / other.

    Thin adapter over ``suite_common/contract.classify_timepoint`` (the canonical
    classifier): blank is the study's primary (single-timepoint) measurement and
    is treated as an immediate posttest (``post_assumed`` -> ``post``); the
    legacy follow-up tokens (follow / fu / wave / month / week) are preserved so
    the routing is byte-identical to the pre-adapter behaviour.
    """
    t = "" if tp is None else str(tp).strip().lower()
    if t in ("", "nan", "none", "nat"):
        return "post"                       # single-timepoint study -> primary = posttest
    if "follow" in t or t.startswith("fu") or "wave" in t or "month" in t or "week" in t:
        return "followup"
    b = SC.classify_timepoint(tp)
    if b == "post_assumed":
        return "post"
    if b == "other" and ("post" in t or "immediate" in t or "endline" in t):
        return "post"
    return "other" if b == "change" else b


def build_did(disc):
    """Pre->post difference in the per-cell discrimination level.

    Pairs each `post` cell with the `pre` cell of the same study x artifact x domain and
    returns delta = d_post - d_pre.  No participant-level pairing is available at this
    aggregated (per-cell) level, so pre and post are treated as independent samples, which
    gives the conservative (larger) sampling variance.  Baselines enter ONLY here.
    """
    keys = ["study_key", "artifact", "dv_domain"]
    if not len(disc):
        return disc.iloc[0:0]
    _comp_score = [c for c in ("true_score", "fake_score", "criterion_score")
                   if c in disc.columns]
    _comp_var = [c for c in ("true_var", "fake_var", "criterion_var")
                 if c in disc.columns]
    _comp = _comp_score + _comp_var
    pre = disc[disc["bucket"] == "pre"][keys + ["d", "vi"] + _comp].drop_duplicates(keys, keep="first")
    pre = pre.rename(columns={"d": "d_pre", "vi": "vi_pre",
                              **{c: c + "_pre" for c in _comp}})
    # If a cell carries both an explicit `post` row and a blank (post-assumed) one, prefer
    # the explicit posttest so the pre row is not paired twice.
    post = disc[disc["bucket"] == "post"][keys + ["d", "vi", "timepoint"] + _comp].copy()
    post["_post_prio"] = (post["timepoint"].astype(str).str.strip().str.lower() == "post").astype(int)
    post = (post.sort_values("_post_prio", ascending=False)
            .drop_duplicates(keys, keep="first").drop(columns="_post_prio"))
    post = post.rename(columns={"d": "d_post", "vi": "vi_post",
                                **{c: c + "_post" for c in _comp}})
    did = post.merge(pre, on=keys, how="inner")
    if not len(did):
        return did
    did["d"] = did["d_post"] - did["d_pre"]
    did["vi"] = did["vi_post"] + did["vi_pre"]
    for c in _comp_score:
        did[c] = did[c + "_post"] - did[c + "_pre"]
    for c in _comp_var:
        did[c] = did[c + "_post"] + did[c + "_pre"]
    did["timepoint"] = "pre->post"
    did["dv_family"] = SC.DV_FAMILY_DIFFERENCE
    did["cluster_id"] = (did["study_key"].astype(str) + "|" + did["artifact"].astype(str))
    return did[keys + ["timepoint", "d", "vi"] + _comp + ["dv_family", "cluster_id"]]


def _clean_cell(x):
    """Normalize a CSV cell: None / NaN spellings / blank -> '' (never a literal glob).

    pandas renders empty cells as the string ``"nan"``; left as-is, ``applies_to``
    would become a literal glob that matches nothing and ``keep`` an unrecognized
    expression, silently disabling the author's restriction (audit F8).
    """
    if x is None:
        return ""
    s = str(x).strip()
    return "" if s.lower() in ("nan", "none", "nat", "<na>") else s


def load_exclusions(folder):
    """Read `<Key> - exclusions.csv` (flag, keep, applies_to, reason).

    `applies_to` is a ';'-separated list of fnmatch globs on the uniform DV name, `*` for all
    DVs, or `domain:<dv_domain>`. `keep` is a simple condition evaluated on the flag column
    (`!=3`, `==1`, `>=1`, `>0`, `notna`). Every harmonizer must emit this file (empty if the
    study has no exclusions) so the SDT analysis applies the author's sample restrictions.
    """
    p = os.path.join(folder, os.path.basename(folder) + " - exclusions.csv")
    if not os.path.isfile(p):
        return []
    try:
        d = pd.read_csv(p, encoding="utf-8-sig")
    except Exception as e:
        # A corrupt file used to silently disable EVERY author exclusion; record it.
        _dlog("exclusion_rule_problem", study=os.path.basename(folder),
              problem="unreadable_exclusions_file", error=f"{type(e).__name__}: {e}")
        return []
    rules = []
    for _, r in d.iterrows():
        flag = _clean_cell(r.get("flag"))
        keep = _clean_cell(r.get("keep"))
        app_raw = _clean_cell(r.get("applies_to"))
        if not (flag or keep or app_raw):
            continue                       # entirely blank row
        if str(r.get("applies_to")).strip().lower() in ("nan", "none", "nat", "<na>"):
            _dlog("exclusion_rule_problem", study=os.path.basename(folder),
                  problem="nan_applies_to_treated_as_all", flag=flag, keep=keep)
        app = app_raw or "*"
        if flag and keep:
            rules.append({"flag": flag, "keep": keep,
                          "applies_to": [x.strip() for x in app.split(";") if x.strip()]})
        else:
            _dlog("exclusion_rule_problem", study=os.path.basename(folder),
                  problem="incomplete_rule_row", flag=flag, keep=keep,
                  applies_to=app_raw)
    return rules


def keep_mask(s, expr, study=None, flag=None):
    s = pd.to_numeric(s, errors="coerce")
    m = re.match(r"^(==|!=|>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)$", expr)
    if m:
        op, v = m.group(1), float(m.group(2))
        mask = {"==": s == v, "!=": s != v, ">=": s >= v, "<=": s <= v,
                ">": s > v, "<": s < v}[op]
        # Exclude only participants whose flag is DEFINED and fails the condition.
        # A missing flag means the check was not administered (e.g. another
        # artifact in the same study), so the row is retained; otherwise a rule
        # defined for one artifact silently wipes out the others.
        return mask | s.isna()
    if expr.lower() == "notna":
        return s.notna()
    # An unrecognized expression silently keeps every row; record it so the author's
    # restriction is not lost without trace.
    _dlog("exclusion_rule_problem", study=study, flag=flag,
          problem="unrecognized_keep_expression", keep=expr)
    return pd.Series(True, index=s.index)


def apply_exclusions(long, rules, study=None):
    """Drop long-format rows failing any exclusion rule that applies to their DV.

    The flag columns must already be part of `long` (added to the melt id_vars).
    """
    for r in rules:
        if r["flag"] not in long.columns:
            # A misspelled / absent flag column silently skipped the rule.
            _dlog("exclusion_rule_problem", study=study, flag=r["flag"],
                  problem="flag_column_missing")
            continue
        row_keep = keep_mask(long[r["flag"]], r["keep"], study=study,
                             flag=r["flag"]).to_numpy(dtype=bool)
        applies = long["dv"].map(
            lambda v: any(p == "*" or fnmatch.fnmatch(str(v), p)
                          for p in r["applies_to"] if not p.startswith("domain:")))
        for p in r["applies_to"]:
            if p.startswith("domain:"):
                applies = applies | (long["dv_domain"].astype(str) == p.split(":", 1)[1])
        applies = applies.to_numpy(dtype=bool)
        long = long[~(applies & (~row_keep))]
    return long


def _make_disc(disc_rows):
    disc = pd.DataFrame(disc_rows)
    if len(disc):
        disc["bucket"] = disc["timepoint"].map(time_bucket)
    else:
        disc["bucket"] = pd.Series(dtype=str)
    return disc


def run_polarity_check(tag):
    """Run Scripts/validate_polarity.py; return 0 pass / 1 fail / None error."""
    try:
        import validate_polarity as VP
        rc = int(VP.main(["--quiet"]))
    except Exception as e:
        print(f"  polarity invariant ({tag}): ERROR ({type(e).__name__}: {e})")
        return None
    print(f"  polarity invariant ({tag}): {'PASS' if rc == 0 else 'FAIL'}")
    return rc


def _component_stats(tv, fv):
    """Arm-level SDT components: mean true score, fake score and response bias c.

    ``tv`` are the (oriented, higher = correct) hit-rate values, ``fv`` the
    correct-rejection values (FAR = 1 - fv).  Returns the mean and the
    variance-of-the-mean of each, so an arm contrast can be formed as a difference
    with a summed variance.  ``criterion c = -0.5 [z(HR) + z(FAR)]`` (positive =
    conservative), computed per unit then averaged, matching the participant-level
    SDT suite.
    """
    out = {"true_score": np.nan, "true_var": np.nan,
           "fake_score": np.nan, "fake_var": np.nan,
           "criterion_score": np.nan, "criterion_var": np.nan}
    tv = np.asarray(tv, float); fv = np.asarray(fv, float)
    tv = tv[np.isfinite(tv)]; fv = fv[np.isfinite(fv)]
    if len(tv):
        out["true_score"] = float(tv.mean())
        out["true_var"] = float(tv.var(ddof=1) / len(tv)) if len(tv) > 1 else np.nan
    if len(fv):
        out["fake_score"] = float(fv.mean())
        out["fake_var"] = float(fv.var(ddof=1) / len(fv)) if len(fv) > 1 else np.nan
    n = min(len(tv), len(fv))
    if n:
        cs = np.array([-0.5 * (_probit(float(tv[i])) + _probit(1.0 - float(fv[i])))
                       for i in range(n)], float)
        cs = cs[np.isfinite(cs)]
        if len(cs):
            out["criterion_score"] = float(cs.mean())
            out["criterion_var"] = float(cs.var(ddof=1) / len(cs)) if len(cs) > 1 else np.nan
    return out


def _cell_dprime(g):
    """Per-cell discrimination d' and its variance, or None.

    Uses per-participant d'_i = z(HR_i) - z(FAR_i) (paired; var(d'_i)/n_pid) when
    participants are identified, else the aggregate d' with a delta-method
    variance.  Returns dict(d, vi, n_true, n_false, n_pid) plus the arm-level SDT
    components (true score, fake score, response bias c and their variances).

    D4: the Hautus (1995) log-linear correction is enabled by passing the number
    of trials/items behind each rate (the count of true/false DV values averaged
    into the rate), so 0/1 rates are not mapped to a fixed z.
    """
    ver = g["veracity"].astype(str).str.strip().str.lower()
    t = g.loc[ver == "true", "value_0_1"]
    fa = g.loc[ver == "false", "value_0_1"]
    if not (len(t) and len(fa)):
        return None
    dd = var = None
    n_pid = 0
    comp = _component_stats(t.to_numpy(float), fa.to_numpy(float))
    if "pid" in g.columns:
        tv = g.loc[ver == "true", ["pid", "value_0_1"]].dropna()
        fv = g.loc[ver == "false", ["pid", "value_0_1"]].dropna()
        if len(tv) and len(fv):
            tm = tv.groupby("pid")["value_0_1"].mean()
            fm = fv.groupby("pid")["value_0_1"].mean()
            nt = tv.groupby("pid").size()
            nf = fv.groupby("pid").size()
            common = tm.index.intersection(fm.index)
            if len(common) >= 2:
                dps = np.array([dprime(tm[p], fm[p], n=int(nt.get(p, 1)),
                                       n_fa=int(nf.get(p, 1))) for p in common], float)
                dps = dps[np.isfinite(dps)]
                if len(dps) >= 2:
                    dd = float(np.mean(dps))
                    var = float(np.var(dps, ddof=1) / len(dps))
                    n_pid = len(dps)
                # Paired participant-level components (more precise than the
                # aggregate when participants are identified).
                comp = _component_stats(
                    np.array([tm[p] for p in common], float),
                    np.array([fm[p] for p in common], float))
    if dd is None:
        hr, cr = float(t.mean()), float(fa.mean())
        dd = dprime(hr, cr, n=len(t), n_fa=len(fa))
        zhr, zfar = _probit(hr), _probit(1.0 - cr)
        phi = lambda z: math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
        v_hr = float(t.var(ddof=1)) / max(len(t), 1)
        v_cr = float(fa.var(ddof=1)) / max(len(fa), 1)
        var = v_hr / (phi(zhr) ** 2) + v_cr / (phi(zfar) ** 2)
        n_pid = int(min(len(t), len(fa)))
    if np.isfinite(dd) and np.isfinite(var) and var > 0:
        out = dict(d=dd, vi=var, n_true=len(t), n_false=len(fa), n_pid=n_pid)
        out.update(comp)
        return out
    return None


def collect_datasets(no_excl, write_catalog=True):
    """Per-study SDT melt.  Returns the fact dict including dataset skip counts.

    The orientation reversal (``higher_is_worse`` -> ``1 - x``) honours the
    module-level ``_NO_REVERSAL`` toggle used by the polarity self-check.
    """
    cat, disc_rows, level_rows, skipped_fresh, fw_skipped = [], [], [], [], []
    n_total = n_analysed = 0
    no_control_cells = 0
    no_control_studies = set()
    n_no_condition = 0
    read_failed = []
    for folder in sorted(glob.glob(os.path.join(DS, "*"))):
        f = os.path.join(folder, os.path.basename(folder) + " - harmonized.csv")
        side = os.path.join(folder, os.path.basename(folder) + " - harmonized_variables.csv")
        if not os.path.isfile(f):
            continue
        # Whole-study full-text exclusion agreed by coders in Coding Studio.
        if SE.is_fulltext_excluded(os.path.basename(folder)):
            continue
        # Fully-within (single-arm, no-control) datasets: excluded by default.
        if not SC.include_fully_within() and SC.study_fully_within(os.path.basename(folder)):
            fw_skipped.append(os.path.basename(folder))
            continue
        n_total += 1
        if fresh(f) or fresh(side):
            skipped_fresh.append(os.path.basename(folder))
            continue
        meta = meta_for(folder)
        try:
            d = pd.read_csv(f, encoding="utf-8-sig", low_memory=False)
        except Exception as e:
            # A silently skipped dataset would make the run partial; record it.
            read_failed.append((os.path.basename(folder), str(e)))
            _dlog("dataset_read_failed", study=os.path.basename(folder),
                  error=f"{type(e).__name__}: {e}")
            continue
        n_analysed += 1
        # The canonical arm column is `condition`; some harmonizers emitted only the raw
        # label (`condition_raw`).  Accept the raw column as a fallback so a study with a
        # real control arm is not silently dropped from the intervention effect.  The SDT
        # analysis suite already reads both names (`condition`/`condition_raw`).
        if "condition" not in d.columns and "condition_raw" in d.columns:
            d = d.rename(columns={"condition_raw": "condition"})
        dvcols = [c for c in d.columns if str(c).startswith("dv_") and str(c).endswith("_01")]
        # Confidence/certainty DVs are NOT outcomes (separate metacognition strand); drop them
        # from the catalogue and from the melt so they can never be pooled, not even as `other`.
        dvcols = [c for c in dvcols
                  if not (DR.is_non_outcome(meta.get(str(c)[:-3], ("", "", "", ""))[0])
                          or SC.is_metacognition(meta.get(str(c)[:-3], ("", "", "", ""))[0],
                                                 str(c)[:-3]))]
        covcols = [c for c in d.columns if str(c).startswith("cov_")]
        cat.append({"study_key": os.path.basename(folder), "rows": len(d),
                    "cols": d.shape[1], "n_dv": len(dvcols), "n_cov": len(covcols),
                    "file": os.path.relpath(f, B)})
        idcols = [c for c in ("study_key", "artifact", "pid", "condition") if c in d.columns]
        # carry exclusion flags through the melt so the author sample restrictions can be applied
        flagcols = [c for c in d.columns if str(c).startswith("flag_")]
        idcols = idcols + [c for c in flagcols if c not in idcols]
        if not dvcols:
            continue
        long = d.melt(id_vars=idcols, value_vars=dvcols, var_name="dv_01", value_name="value_0_1")
        long = long.dropna(subset=["value_0_1"])
        if not len(long):
            continue
        if "artifact" not in long.columns:
            long["artifact"] = ""          # the groupby below always needs the key
        long["dv"] = long["dv_01"].str[:-3]
        long["dv_domain"] = long["dv"].map(lambda v: meta.get(v, ("", "", "", ""))[0])
        long["veracity"] = long["dv"].map(lambda v: meta.get(v, ("", "", "", ""))[1])
        long["timepoint"] = long["dv"].map(lambda v: meta.get(v, ("", "", "", ""))[2])
        long["polarity"] = long["dv"].map(lambda v: meta.get(v, ("", "", "", ""))[3])
        # Difference/change-score DVs must not carry a veracity (contract.dv_veracity);
        # if a sidecar tags one anyway, drop it so it is never pair-split as a hit /
        # false-alarm component and pooled as an outcome (see _DROP_MISLABELED_DIFF).
        if _DROP_MISLABELED_DIFF:
            _ver_tf = long["veracity"].astype(str).str.strip().str.lower().isin(["true", "false"])
            _diff = long["dv"].astype(str).map(lambda v: bool(_DIFF_SCORE_RE.search(v)))
            if (_ver_tf & _diff).any():
                long = long[~(_ver_tf & _diff)]
                if not len(long):
                    continue
        # Confidence/certainty DVs are NOT outcomes: they belong to the separate confidence &
        # metacognition strand (metacog_* namespace) and are dropped here -- never pooled, and
        # never folded into `other`.
        long = long[~long["dv_domain"].isin(DR.NON_OUTCOME_DOMAINS)]
        if not len(long):
            continue
        # Drop non-independent experiments (same sample reported by another included source).
        ex_labels = SE.excluded_labels(os.path.basename(folder))
        if ex_labels and "artifact" in long.columns:
            long = long[~long["artifact"].astype(str).isin(ex_labels)]
            if not len(long):
                continue
        # Orientation: every DV must read "higher = more desirable". Reverse-code where the
        # sidecar records a higher_is_worse orientation (0-1 rescale -> 1 - x). Positive
        # discrimination then always means better processing of true vs false material.
        rev = long["polarity"].astype(str).str.strip().str.lower().isin(
            ["higher_is_worse", "lower_is_better", "reverse", "reversed", "negative"])
        if _VERACITY_FALLBACK:
            # Blank-polarity false-veracity endorsement DVs are un-reversed in the
            # primary pass (the rule declines "ambiguous"); for the sensitivity,
            # treat them as the rule intends (false endorsement -> higher_is_worse).
            try:
                import dv_polarity as _dvp
                _is_comp = long["dv"].astype(str).map(_dvp.is_composite)
            except Exception:
                _is_comp = pd.Series(False, index=long.index)
            _blank_pol = long["polarity"].astype(str).str.strip().str.lower().isin(
                ["", "nan", "none", "nat"])
            _ver_f = long["veracity"].astype(str).str.strip().str.lower() == "false"
            _endorse = long["dv_domain"].astype(str).isin(_FALLBACK_DOMAINS)
            rev = rev | (_blank_pol & _ver_f & _endorse & (~_is_comp))
        if not _NO_REVERSAL:
            long.loc[rev, "value_0_1"] = 1.0 - long.loc[rev, "value_0_1"]
        # Apply the study's exclusion flags (author sample restrictions) before pooling.
        # `--no-exclusions` skips every rule for the exclusions-off robustness run.
        if not no_excl:
            long = apply_exclusions(long, load_exclusions(folder),
                                    study=os.path.basename(folder))
        # Timepoints that route to no pool are invisible otherwise: record the distinct
        # (study, artifact, timepoint) values whose `time_bucket` is ``other``.
        if _LOG_ACTIVE and len(long):
            _bad_tp = [t for t in long["timepoint"].dropna().unique()
                       if time_bucket(t) == "other"]
            if _bad_tp:
                _other = long.loc[long["timepoint"].isin(_bad_tp),
                                  ["study_key", "artifact", "timepoint"]].drop_duplicates()
                for _r in _other.to_dict("records"):
                    _dlog("timepoint_unrouted", study=_r["study_key"],
                          artifact=_r["artifact"], variable="timepoint",
                          timepoint=_r["timepoint"])
        # ---- condition -> arm role (PI decision 2026-09-21) ------------------
        # The pooled SDT quantity is now the INTERVENTION-vs-CONTROL difference in
        # discrimination, so each participant is assigned to the treatment or the
        # no-misinformation-intervention control arm from the harmonized
        # `condition` column (using the study's conditions.csv mapping when it
        # exists, else the documented numeric/name heuristic).
        if "condition" in long.columns:
            _role_cache = {}

            def _cond_role(c, a):
                kk = (str(a), str(c))
                if kk not in _role_cache:
                    try:
                        _role_cache[kk] = SC.classify_condition(
                            c, os.path.basename(folder), str(a), folder=folder)[0]
                    except Exception as e:
                        _role_cache[kk] = "unknown"
                        _dlog("condition_role_unknown", study=os.path.basename(folder),
                              variable="condition", condition=str(c),
                              artifact=str(a), error=f"{type(e).__name__}: {e}")
                return _role_cache[kk]

            art_series = (long["artifact"] if "artifact" in long.columns
                          else pd.Series("", index=long.index))
            long = long.copy()
            long["_role"] = [_cond_role(c, a)
                             for c, a in zip(long["condition"], art_series)]
        else:
            long = long.copy()
            long["_role"] = "missing"
            n_no_condition += 1
        for (key, art, dom, tp), g in long.groupby(["study_key", "artifact", "dv_domain", "timepoint"],
                                                   dropna=False):
            lvl = _cell_dprime(g)
            if lvl is not None:
                level_rows.append({"study_key": key, "artifact": art, "dv_domain": dom,
                                   "timepoint": tp, "n_true": lvl["n_true"],
                                   "n_false": lvl["n_false"], "d": lvl["d"],
                                   "vi": lvl["vi"],
                                   "dv_family": SC.DV_FAMILY_DIFFERENCE,
                                   "cluster_id": SC.sdt_cluster_id(key, art)})
            gi = g[g["_role"] == "intervention"]
            gc = g[g["_role"].isin(SC.COND_CONTROL_ROLES)]
            ri = _cell_dprime(gi)
            rc = _cell_dprime(gc)
            if ri is None or rc is None:
                if lvl is not None:
                    no_control_cells += 1
                    no_control_studies.add(key)
                continue
            # Arm contrast of the SDT components (true score, fake score, response
            # bias c): difference of the arm means, variance = sum of the
            # variance-of-the-mean.  Auxiliary; never enters the pooled effect.
            _row = {"study_key": key, "artifact": art, "dv_domain": dom,
                    "timepoint": tp, "n_true": ri["n_true"],
                    "n_false": ri["n_false"], "n_treat": ri["n_pid"],
                    "n_control": rc["n_pid"],
                    "d": ri["d"] - rc["d"], "vi": ri["vi"] + rc["vi"],
                    "dv_family": SC.DV_FAMILY_DIFFERENCE,
                    "cluster_id": SC.sdt_cluster_id(key, art)}
            for _c in ("true_score", "fake_score", "criterion_score"):
                _a, _b = ri.get(_c), rc.get(_c)
                _row[_c] = (float(_a) - float(_b)
                            if (_a is not None and _b is not None
                                and np.isfinite(_a) and np.isfinite(_b)) else np.nan)
            for _c in ("true_var", "fake_var", "criterion_var"):
                _a, _b = ri.get(_c), rc.get(_c)
                _row[_c] = (float(_a) + float(_b)
                            if (_a is not None and _b is not None
                                and np.isfinite(_a) and np.isfinite(_b)) else np.nan)
            disc_rows.append(_row)
    if write_catalog:
        pd.DataFrame(cat).to_csv(CATALOG, index=False, encoding="utf-8-sig")
        pd.DataFrame({"study_key": skipped_fresh}).to_csv(
            os.path.join(_TMP, "reanalysis_skipped_fresh.csv"),
            index=False, encoding="utf-8-sig")
    return dict(cat=cat, disc_rows=disc_rows, level_rows=level_rows,
                skipped_fresh=skipped_fresh, fw_skipped=fw_skipped,
                n_total=n_total, n_analysed=n_analysed,
                no_control_cells=no_control_cells,
                no_control_studies=sorted(no_control_studies),
                n_no_condition=n_no_condition, read_failed=read_failed)


def _pool_est(df):
    if df is None or not len(df):
        return np.nan, 0
    r = SC.pool_dependence(df["d"], df["vi"], df["cluster_id"], df["study_key"])
    est = r.get("three_est")
    if est is None or not np.isfinite(est):
        est = r.get("dl_est")
    return est, r.get("dl_k", 0)


def main():
    global _FORCE, _NO_REVERSAL, _LOG_ACTIVE
    SC.include_fully_within_cli()
    SC.announce_fully_within("reanalysis")
    AL.reset("reanalysis")
    print("QUANTITY (PI decision 2026-09-21): the SDT headline is the INTERVENTION-vs-"
          "CONTROL\n  effect on discrimination: participants are split by the harmonized "
          "`condition` column,\n  d' = z(HR) - z(FAR) is computed per arm, and the pooled "
          "effect is\n  d'_treatment - d'_control.  The arm-pooled discrimination LEVEL is "
          "kept as the\n  auxiliary `Discrimination_level` sheet.  Studies/cells without a "
          "control arm are\n  excluded from the intervention effect (see RunAudit).")
    force = bool(os.environ.get("SUITE_FORCE"))
    harmonizer_failed = []
    if "--no-harmonize" in sys.argv:
        print("skipping harmonizers (--no-harmonize)")
    else:
        harmonizer_failed = run_harmonizers()
        force = True
        print("  ! reanalysis ran the harmonizers itself: freshness guard relaxed for this run")
        # Also materialise the single combined wide table (columns present in
        # >= 3 studies; see Scripts/combine_harmonized.py).  Skipped with --no-combine.
        if "--no-combine" in sys.argv:
            print("skipping combined harmonized dataset (--no-combine)")
        else:
            try:
                import combine_harmonized
                combine_harmonized.build_if_stale()
            except Exception as e:
                print(f"  ! combined harmonized dataset failed: {e}")
    if not force:
        age = recent_harmonizer_run()
        if age is not None:
            force = True
            print(f"  ! WARNING: run_harmonizers.py finished {age / 60:.1f} min ago; "
                  f"freshness guard relaxed for this run.  For the documented final run "
                  f"use SUITE_FORCE=1.")
    _FORCE = force
    no_excl = "--no-exclusions" in sys.argv
    if no_excl:
        print("skipping every <Key> - exclusions.csv rule (--no-exclusions)")

    # ---- polarity precondition (PI addendum b) ------------------------------
    pre_pol = run_polarity_check("precondition")
    if pre_pol == 1:
        print("ERROR: polarity invariant FAILED before pooling; refusing to pool.")
        return 3

    _LOG_ACTIVE = True          # primary pass: every drop is logged
    d1 = collect_datasets(no_excl, write_catalog=True)
    n_total, n_analysed = d1["n_total"], d1["n_analysed"]
    skipped_fresh, fw_skipped, cat = d1["skipped_fresh"], d1["fw_skipped"], d1["cat"]
    no_control_cells = d1["no_control_cells"]
    no_control_studies = d1["no_control_studies"]
    n_no_condition = d1["n_no_condition"]
    read_failed = d1["read_failed"]

    # ---- completeness guard (PI addendum a): never write a silent partial run
    if skipped_fresh and not force:
        print(f"ERROR: {len(skipped_fresh)} dataset(s) were skipped by the freshness guard "
              f"(modified within {_fresh_window():.0f}s).  Refusing to write a partial "
              f"re-analysis.  Wait for the concurrent writer to finish, or re-run with "
              f"SUITE_FORCE=1 (the documented final run).")
        print("  skipped: " + ", ".join(skipped_fresh[:12])
              + (" ..." if len(skipped_fresh) > 12 else ""))
        return 2

    disc = _make_disc(d1["disc_rows"])              # intervention effects
    level = _make_disc(d1["level_rows"])            # arm-pooled levels (auxiliary)

    # ---- polarity self-check (PI addendum b): the pooled effect MUST change when
    # the higher_is_worse reversal is toggled (unless no reversed DV is contributed).
    _NO_REVERSAL = True
    _LOG_ACTIVE = False         # self-check re-runs the melt; do not duplicate drop records
    d2 = collect_datasets(no_excl, write_catalog=False)
    _NO_REVERSAL = False
    _LOG_ACTIVE = True
    disc_no_rev = _make_disc(d2["disc_rows"])
    post_on = disc[disc["bucket"] == "post"] if len(disc) else disc
    post_off = disc_no_rev[disc_no_rev["bucket"] == "post"] if len(disc_no_rev) else disc_no_rev
    est_on, k_on = _pool_est(post_on)
    est_off, k_off = _pool_est(post_off)
    level_post = level[level["bucket"] == "post"] if len(level) else level
    level_est, level_k = _pool_est(level_post)
    changed = (not (np.isfinite(est_on) and np.isfinite(est_off))) or abs(est_on - est_off) > 1e-9
    print(f"  polarity self-check (posttest intervention effect): reversal ON "
          f"d'={est_on:.4f} (k={k_on}) vs OFF d'={est_off:.4f} (k={k_off}) "
          f"-> {'CHANGES (reversal is applied)' if changed else 'UNCHANGED'}")
    print(f"  auxiliary arm-pooled posttest LEVEL (not the headline): "
          f"d'={level_est:.4f} (k={level_k})")
    if not changed:
        print("  ! WARNING: toggling higher_is_worse does NOT change the pooled posttest "
              "effect -- either no reversed DV contributes, or the reversal is silently "
              "unapplied.")

    # ------------------------------------------------------------------ timepoint-separated pools
    # The three questions are pooled separately and are NEVER mixed.  Pretest rows are not
    # outcomes: they enter only through `build_did` (post - pre within a cell).
    post = post_on
    follow = disc[disc["bucket"] == "followup"] if len(disc) else disc
    did = build_did(disc)
    questions = [("posttest (immediate)", post),
                 ("follow-up (persistence)", follow),
                 ("DiD (pre->post)", did)]

    def pool_question(df, question, scope, restricted_only=False):
        rows = []
        if not len(df):
            return rows
        if restricted_only:
            df = df[df["dv_domain"].isin(DR.SUSCEPTIBILITY_DOMAINS)]
        if not len(df):
            return rows
        rows.append({"question": question, "scope": scope, "group": "ALL",
                     **SC.pool_dependence(df["d"], df["vi"], df["cluster_id"],
                                         df["study_key"])})
        for dom, g in df.groupby("dv_domain"):
            if len(g):
                rows.append({"question": question, "scope": scope, "group": f"domain: {dom}",
                             **SC.pool_dependence(g["d"], g["vi"], g["cluster_id"],
                                                 g["study_key"])})
        return rows

    res_rows = []
    for question, df in questions:
        res_rows += pool_question(df, question, "unrestricted")
    res = pd.DataFrame(res_rows)

    # Restricted model kept separate: misinformation-susceptibility domains only, per question.
    res_r_rows = []
    for question, df in questions:
        res_r_rows += pool_question(df, question, "restricted", restricted_only=True)
    res_restricted = pd.DataFrame(res_r_rows)

    # Domain table: per question x controlled domain, discrimination contrasts and pooled d,
    # with the DL comparison plus the dependence-aware (three-level / RVE) estimates.
    EMPTY_DEP = dict(dl_k=0, dl_est=np.nan, dl_lo=np.nan, dl_hi=np.nan, dl_tau2=np.nan,
                     dl_i2=np.nan, three_est=np.nan, three_lo=np.nan, three_hi=np.nan,
                     rve_est=np.nan, rve_lo=np.nan, rve_hi=np.nan, n_clusters=0,
                     max_weight_share_dl=np.nan, max_weight_share_rve=np.nan)

    def _dep(g):
        if not len(g):
            return EMPTY_DEP
        return SC.pool_dependence(g["d"], g["vi"], g["cluster_id"], g["study_key"])

    dom_rows = []
    for question, df in questions:
        if not len(df) or "dv_domain" not in df.columns:
            continue
        for dom in DR.DV_DOMAINS:
            g = df[df["dv_domain"] == dom]
            r = _dep(g) if len(g) else EMPTY_DEP
            dom_rows.append({"question": question, "dv_domain": dom,
                             "restricted": int(dom in DR.SUSCEPTIBILITY_DOMAINS),
                             "n_contrasts": len(g),
                             "n_studies": g["study_key"].nunique() if len(g) else 0,
                             "k": r["dl_k"], "est": r["dl_est"], "lo": r["dl_lo"],
                             "hi": r["dl_hi"], "tau2": r["dl_tau2"], "i2": r["dl_i2"],
                             # multilevel (three-level) headline; DL is the comparison
                             "est_multilevel": r["three_est"], "lo_multilevel": r["three_lo"],
                             "hi_multilevel": r["three_hi"],
                             "est_rve": r["rve_est"], "lo_rve": r["rve_lo"], "hi_rve": r["rve_hi"],
                             "three_est": r["three_est"], "rve_est": r["rve_est"],
                             "rve_lo": r["rve_lo"], "rve_hi": r["rve_hi"],
                             "n_clusters": r["n_clusters"],
                             "max_weight_share_dl": r["max_weight_share_dl"],
                             "max_weight_share_rve": r["max_weight_share_rve"]})
        for label in ("ALL_DOMAINS", "RESTRICTED_SUSCEPTIBILITY"):
            sel = df if label == "ALL_DOMAINS" else (
                df[df["dv_domain"].isin(DR.SUSCEPTIBILITY_DOMAINS)] if len(df) else df)
            r = _dep(sel) if len(sel) else EMPTY_DEP
            dom_rows.append({"question": question, "dv_domain": label,
                             "restricted": int(label == "RESTRICTED_SUSCEPTIBILITY"),
                             "n_contrasts": len(sel),
                             "n_studies": sel["study_key"].nunique() if len(sel) else 0,
                             "k": r["dl_k"], "est": r["dl_est"], "lo": r["dl_lo"],
                             "hi": r["dl_hi"], "tau2": r["dl_tau2"], "i2": r["dl_i2"],
                             "est_multilevel": r["three_est"], "lo_multilevel": r["three_lo"],
                             "hi_multilevel": r["three_hi"],
                             "est_rve": r["rve_est"], "lo_rve": r["rve_lo"], "hi_rve": r["rve_hi"],
                             "three_est": r["three_est"], "rve_est": r["rve_est"],
                             "rve_lo": r["rve_lo"], "rve_hi": r["rve_hi"],
                             "n_clusters": r["n_clusters"],
                             "max_weight_share_dl": r["max_weight_share_dl"],
                             "max_weight_share_rve": r["max_weight_share_rve"]})
    domain_counts = pd.DataFrame(dom_rows)

    post_pol = run_polarity_check("postcondition")
    audit = pd.DataFrame([
        {"item": "quantity_measured", "value": "INTERVENTION-vs-CONTROL effect on "
                                               "discrimination, d'_treatment - d'_control "
                                               "(condition split from `condition` column)"},
        {"item": "auxiliary_sheet", "value": "Discrimination_level = arm-pooled "
                                             "discrimination level (not the headline)"},
        {"item": "datasets_eligible", "value": n_total},
        {"item": "datasets_analysed", "value": n_analysed},
        {"item": "datasets_skipped_fresh", "value": len(skipped_fresh)},
        {"item": "fully_within_skipped", "value": len(fw_skipped)},
        {"item": "datasets_without_condition_column", "value": n_no_condition},
        {"item": "datasets_read_failed", "value": len(read_failed)},
        {"item": "datasets_read_failed_detail", "value": "; ".join(f"{k}: {e}"
                                                                    for k, e in read_failed[:8])},
        # Crashed harmonizers leave a possibly stale CSV that is still pooled: make them
        # visible in the audit (full stderr tails are in Results/analysis_log.jsonl).
        {"item": "harmonizers_failed", "value": len(harmonizer_failed)},
        {"item": "harmonizers_failed_detail", "value": "; ".join(harmonizer_failed)},
        {"item": "drop_log", "value": os.path.relpath(AL.LOG_PATH, B)},
        {"item": "cells_without_control_arm", "value": no_control_cells},
        {"item": "studies_without_control_arm", "value": len(no_control_studies)},
        {"item": "wide_datasets_catalogued", "value": len(cat)},
        {"item": "intervention_contrasts", "value": len(disc)},
        {"item": "level_contrasts_auxiliary", "value": len(level)},
        {"item": "posttest_intervention_effect_reversal_on", "value": est_on},
        {"item": "posttest_intervention_effect_reversal_off", "value": est_off},
        {"item": "posttest_level_auxiliary", "value": level_est},
        {"item": "reversal_toggle_changes_effect", "value": bool(changed)},
        {"item": "polarity_precondition_rc", "value": pre_pol},
        {"item": "polarity_postcondition_rc", "value": post_pol},
        {"item": "freshness_guard_forced", "value": bool(force)},
        {"item": "freshness_window_seconds", "value": _fresh_window()},
    ])
    with pd.ExcelWriter(OUT, engine="openpyxl") as xl:
        disc.to_excel(xl, sheet_name="Discrimination", index=False)
        level.to_excel(xl, sheet_name="Discrimination_level", index=False)
        res.to_excel(xl, sheet_name="MetaResults", index=False)
        res_restricted.to_excel(xl, sheet_name="MetaResults_restricted", index=False)
        domain_counts.to_excel(xl, sheet_name="Domain_Counts", index=False)
        audit.to_excel(xl, sheet_name="RunAudit", index=False)
    print(f"wide datasets catalogued: {len(cat)} | intervention contrasts: {len(disc)} | "
          f"auxiliary level contrasts: {len(level)}")
    print(f"completeness: analysed {n_analysed}/{n_total} eligible dataset(s); "
          f"skipped-fresh {len(skipped_fresh)}; fully-within skipped {len(fw_skipped)}")
    if no_control_studies:
        print(f"  no control arm (excluded from the intervention effect): "
              f"{len(no_control_studies)} study/studies, {no_control_cells} cell(s)")
    if read_failed:
        print(f"  ! {len(read_failed)} harmonized dataset(s) could not be read and were "
              f"skipped: {', '.join(k for k, _ in read_failed[:8])}"
              + (" ..." if len(read_failed) > 8 else ""))
    print(SC.fully_within_report(len(fw_skipped), "dataset(s)"))
    if skipped_fresh:
        print(f"  (freshness-guard forced, so these were still analysed): "
              f"{', '.join(skipped_fresh[:8])}"
              + (" ..." if len(skipped_fresh) > 8 else ""))
    print(f"  timepoint buckets: {disc['bucket'].value_counts().to_dict() if len(disc) else {}}")
    if len(res):
        print(res.to_string(index=False))
    if len(res_restricted):
        print(res_restricted.to_string(index=False))
    print("wrote", OUT, "and", CATALOG)


if __name__ == "__main__":
    sys.exit(main())
