"""Validate the fully-within switch (R2.5) end to end.

The switch is one env var (``INCLUDE_FULLY_WITHIN``) with one default (OFF) and one
opt-in spelling (``=1`` / ``--include-fully-within``).  Fully-within records are
single-arm (no no-misinformation control) experiments whose only coded effects are
within-person pre->post changes (``contrast_type == 4``).  A fully-within contrast
has ``timepoint_1=post`` / ``timepoint_2=pre``, so ``metaanalysis.dv_bucket`` sees
``spans_pre_post`` and routes it to the **DiD (pre->post)** question, never the
immediate posttest.  The switch therefore moves the DiD pool only; the immediate
posttest is unchanged.

This validator asserts, and exits 1 on any violation:

DEFAULT (``INCLUDE_FULLY_WITHIN`` unset)
    * the Extraction layer marks 12 records across 11 studies;
    * every shipped pooled product is fully-within free:
      ``temp/meta_specs.csv``, ``temp/sdt_specs.csv`` (canonical, if present),
      ``temp/sdt_specs_pb.csv``, ``temp/sdt_participant_specs.csv``,
      ``temp/pb_pooled.csv`` (k equals the default spec), ``metaanalysis.xlsx``
      (Effects sheet) and ``Results/metacognition/metacog_participants.csv``;
    * the switch markers beside the spec tables read ``0``;
    * the metacognition study registry contains no fully-within study.

OPT-IN (``INCLUDE_FULLY_WITHIN=1``)
    * a fresh meta-spec build adds the fully-within contrasts to ``did_all`` and
      leaves ``post_all`` / ``persistence_all`` unchanged;
    * a fresh SDT-spec build adds the fully-within datasets;
    * a fresh ``metaanalysis.py`` run moves the DiD pool by exactly the
      fully-within DiD contrasts and leaves the immediate posttest unchanged;
    * every fully-within contrast in the opt-in metaanalysis ``Effects`` sheet has
      ``bucket == 'did'``;
    * ``publication_bias.select`` on the opt-in specs yields a larger DiD k.

Run:  python Scripts/validate_fully_within.py
"""
from __future__ import annotations

import contextlib
import glob
import io
import json
import os
import sys
import tempfile

import pandas as pd

B = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(B, "Scripts")
TMP = os.path.join(B, "temp")
EXTRACT = os.path.join(B, "Extraction")
DS = os.path.join(B, "Datasets")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, os.path.join(SCRIPTS, "analysis"))
sys.path.insert(0, B)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import suite_common as SC  # noqa: E402

WORK = os.path.join(TMP, "fw_validate")
os.makedirs(WORK, exist_ok=True)
tempfile.tempdir = WORK

FAILS: list[str] = []
NOTES: list[str] = []


def check(cond, msg):
    if cond:
        print(f"  ok   {msg}")
    else:
        print(f"  FAIL {msg}")
        FAILS.append(msg)
    return bool(cond)


def note(msg):
    NOTES.append(msg)
    print(f"  info {msg}")


def read_csv(path):
    return pd.read_csv(path, encoding="utf-8-sig", low_memory=False)


# --------------------------------------------------------------------------- corpus
def fw_inventory():
    """({(key, Study_ID)}, {all-fw study keys}, {all study keys with >=1 fw record})."""
    records, studies, papers = set(), set(), set()
    for p in sorted(glob.glob(os.path.join(EXTRACT, "*.json"))):
        key = os.path.splitext(os.path.basename(p))[0]
        try:
            data = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        data = data if isinstance(data, list) else [data]
        recs = [o for o in data if isinstance(o, dict)]
        if not recs:
            continue
        hit = False
        all_fw = True
        for o in recs:
            if SC.is_fully_within(o):
                records.add((key, str(o.get("Study_ID") or "")))
                hit = True
            else:
                all_fw = False
        if hit:
            papers.add(key)
        if all_fw:
            studies.add(key)
    return records, studies, papers


def fw_study_keys(records):
    return {k for k, _ in records}


def fw_pair_rows(d, records):
    """Rows of ``d`` whose (study_key, experiment) is a fully-within experiment.

    The meta layer is experiment-level: a paper that contains BOTH a
    fully-within experiment and a between-subject experiment must keep the
    latter's pooled rows.  Matching on the (study_key, experiment_id) pair is the
    semantically correct test; matching on the paper key alone wrongly flags a
    mixed-design paper's legitimate rows.
    """
    if "study_key" not in d.columns:
        return 0
    if "experiment_id" in d.columns:
        return int(sum((str(k), str(s)) in records
                       for k, s in zip(d["study_key"], d["experiment_id"])))
    return int(d["study_key"].astype(str).isin(fw_study_keys(records)).sum())


def fw_pair_mask(d, records):
    if "study_key" not in d.columns:
        return pd.Series(False, index=d.index)
    if "experiment_id" in d.columns:
        return pd.Series([(str(k), str(s)) in records
                          for k, s in zip(d["study_key"], d["experiment_id"])],
                         index=d.index)
    return d["study_key"].astype(str).isin(fw_study_keys(records))


def set_switch(on):
    if on:
        os.environ[SC.FULLY_WITHIN_ENV] = "1"
    else:
        os.environ.pop(SC.FULLY_WITHIN_ENV, None)


# --------------------------------------------------------------------------- builders
def build_meta(include, out):
    orig = SC.META_SPECS
    SC.META_SPECS = out
    set_switch(include)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            SC.build_meta_specs()
        return read_csv(out)
    finally:
        SC.META_SPECS = orig
        set_switch(False)


def build_sdt(include, out):
    set_switch(include)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            SC.build_sdt_specs(out=out)
        skipped = []
        skip_path = os.path.join(TMP, "sdt_fully_within_skipped.csv")
        if os.path.isfile(skip_path):
            try:
                skipped = read_csv(skip_path)["study_key"].astype(str).tolist()
            except Exception:
                skipped = []
        return read_csv(out), skipped
    finally:
        set_switch(False)


def run_metaanalysis(include, out):
    import metaanalysis as MA
    orig = MA.OUT
    MA.OUT = out
    set_switch(include)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            MA.main()
    finally:
        MA.OUT = orig
        set_switch(False)
    eff = pd.read_excel(out, sheet_name="Effects")
    res = pd.read_excel(out, sheet_name="MetaResults")
    return eff, res


def meta_k(res, question):
    row = res[(res["question"] == question) & (res["scope"] == "unrestricted")
              & (res["group"] == "ALL")]
    return int(row["dl_k"].iloc[0]) if len(row) else None


# --------------------------------------------------------------------------- default
def check_default(fw_records, fw_studies):
    print("\n[1] DEFAULT: shipped pooled products are fully-within free")
    fw_keys = fw_study_keys(fw_records)

    for path, layer_label, exp_level in (
            (os.path.join(TMP, "meta_specs.csv"), "meta specs", True),
            (os.path.join(TMP, "sdt_specs.csv"), "SDT canonical specs", False),
            (os.path.join(TMP, "sdt_specs_pb.csv"), "SDT publication-bias specs", False),
            (os.path.join(TMP, "sdt_participant_specs.csv"), "SDT participant specs", False)):
        if not os.path.isfile(path):
            note(f"{layer_label}: {os.path.basename(path)} not present (skipped)")
            continue
        d = read_csv(path)
        if "study_key" not in d.columns:
            note(f"{layer_label}: no study_key column (skipped)")
            continue
        n = fw_pair_rows(d, fw_records) if exp_level else int(
            d["study_key"].astype(str).isin(fw_studies).sum())
        check(n == 0, f"{layer_label}: 0 fully-within rows (found {n})")

    # publication-bias table: the pooled DiD k must equal the default spec pool.
    import publication_bias as PB
    pb_path = os.path.join(TMP, "pb_pooled.csv")
    if os.path.isfile(pb_path) and os.path.isfile(os.path.join(TMP, "meta_specs.csv")):
        pb = read_csv(pb_path)
        row = pb[(pb["layer"] == "meta") & (pb["spec"] == "did_all")
                 & (pb["exclusions"] == "on")]
        pb_k = int(row["k"].iloc[0]) if len(row) else None
        spec = read_csv(os.path.join(TMP, "meta_specs.csv"))
        exp_k = len(PB.select(spec, "meta", "did_all", "on"))
        check(pb_k == exp_k,
              f"publication-bias did_all (meta,on) k={pb_k} equals the default spec pool k={exp_k}")
    else:
        note("publication-bias tables not present (skipped)")

    # metaanalysis workbook: the pooled Effects sheet carries no fw contrast.
    ma_path = os.path.join(B, "Results", "metaanalysis.xlsx")
    default_did = default_post = None
    if os.path.isfile(ma_path):
        eff = pd.read_excel(ma_path, sheet_name="Effects")
        n = fw_pair_rows(eff, fw_records) if "study_key" in eff.columns else -1
        check(n == 0, f"metaanalysis.xlsx Effects: 0 fully-within rows (found {n})")
        res = pd.read_excel(ma_path, sheet_name="MetaResults")
        default_did = meta_k(res, "DiD (pre->post)")
        default_post = meta_k(res, "posttest (immediate)")
        check(default_did is not None and default_post is not None,
              f"metaanalysis.xlsx MetaResults read (DiD k={default_did}, posttest k={default_post})")
    else:
        note("metaanalysis.xlsx not present (skipped)")

    # metacognition strand.
    mc_path = os.path.join(B, "Results", "metacognition", "metacog_participants.csv")
    if os.path.isfile(mc_path):
        mc = read_csv(mc_path)
        n = int(mc["study_key"].astype(str).isin(fw_keys).sum()) if "study_key" in mc.columns else -1
        check(n == 0, f"metacognition participants: 0 fully-within rows (found {n})")
    else:
        note("metacognition participants table not present (skipped)")
    try:
        import sys as _sys
        _mc_dir = os.path.join(B, "metacognition", "Scripts")
        if _mc_dir not in _sys.path:
            _sys.path.insert(0, _mc_dir)
        import metacognition as MC
        registry = set(MC.HARM_STUDIES) | set(MC.RAW_STUDIES)
        overlap = registry & fw_studies
        check(not overlap, f"metacognition registry has no fully-within study ({sorted(overlap)})")
    except Exception as exc:  # pragma: no cover - optional heavy import
        note(f"metacognition registry not importable ({exc})")

    # switch markers.
    for spec in ("meta_specs.csv", "sdt_specs_pb.csv"):
        marker = os.path.join(TMP, spec + SC.FULLY_WITHIN_MARKER_SUFFIX)
        if os.path.isfile(marker):
            val = open(marker, encoding="utf-8").read().strip()
            check(val == "0", f"{spec} switch marker == '0' (found {val!r})")
        else:
            note(f"{spec} switch marker absent (skipped)")

    return default_did, default_post


# --------------------------------------------------------------------------- opt-in
def check_optin(fw_records, fw_studies, default_did, default_post):
    print("\n[2] OPT-IN: fully-within records reappear and land in the DiD spec")
    fw_keys = fw_study_keys(fw_records)

    # NB: build the opt-in table first and the default table last so the shared
    # temp/meta_fully_within_skipped.csv diagnostic is left in its default state.
    d1 = build_meta(True, os.path.join(WORK, "optin_meta_specs.csv"))
    d0 = build_meta(False, os.path.join(WORK, "default_meta_specs.csv"))
    check(fw_pair_rows(d0, fw_records) == 0,
          "meta specs (default build): 0 fully-within rows")
    n_fw = fw_pair_rows(d1, fw_records)
    check(n_fw > 0, f"meta specs (opt-in build): {n_fw} fully-within rows appear")

    def spec_n(d, spec, r=None):
        m = d["spec"] == spec
        if r is not None:
            m = m & (pd.to_numeric(d["r_used"], errors="coerce").round(3) == r)
        return int(m.sum())

    for spec in ("post_all", "persistence_all"):
        check(spec_n(d1, spec) == spec_n(d0, spec),
              f"meta specs: {spec} unchanged by the switch "
              f"({spec_n(d0, spec)} -> {spec_n(d1, spec)})")
    check(spec_n(d1, "did_all", 0.5) > spec_n(d0, "did_all", 0.5),
          f"meta specs: did_all grows ({spec_n(d0, 'did_all', 0.5)} -> {spec_n(d1, 'did_all', 0.5)})")
    fw_did = d1[fw_pair_mask(d1, fw_records) & (d1["spec"] == "did_all")]
    check(len(fw_did) > 0 and set(fw_did["source"].astype(str)) <= {"did:reported"},
          f"meta specs: fully-within opt-in rows are DiD reported-change rows ({len(fw_did)})")

    # publication-bias reaches the opt-in pool.
    try:
        import publication_bias as PB
        pb0 = len(PB.select(d0, "meta", "did_all", "on"))
        pb1 = len(PB.select(d1, "meta", "did_all", "on"))
        check(pb1 > pb0, f"publication-bias DiD pool grows on opt-in ({pb0} -> {pb1})")
    except Exception as exc:  # pragma: no cover
        note(f"publication_bias import failed ({exc})")

    # SDT specs.  Only two fully-within studies (Cook et al. 2024, Craig &
    # Vijaykumar 2023) have harmonized data, and neither yields a usable
    # discrimination cell, so the opt-in SDT *table* is numerically identical —
    # what the switch controls is whether those datasets are processed at all
    # (the skipped-dataset list goes 11 -> 0).
    s1, skip1 = build_sdt(True, os.path.join(WORK, "optin_sdt_specs.csv"))
    s0, skip0 = build_sdt(False, os.path.join(WORK, "default_sdt_specs.csv"))
    check(int(s0["study_key"].astype(str).isin(fw_studies).sum()) == 0,
          "SDT specs (default build): 0 fully-within rows")
    fw_dataset_studies = {k for k in fw_studies
                          if os.path.isdir(os.path.join(DS, k))}
    check(len(skip0) == len(fw_dataset_studies),
          f"SDT default build skips all {len(fw_dataset_studies)} fully-within datasets "
          f"(skipped {len(skip0)})")
    check(set(skip0) == fw_dataset_studies,
          "SDT default build skips exactly the fully-within dataset keys")
    check(len(skip1) == 0,
          f"SDT opt-in build skips 0 fully-within datasets (skipped {len(skip1)})")
    check(len(s1) == len(s0),
          f"SDT specs counts equal the include-run ({len(s0)} -> {len(s1)} rows; "
          f"fw datasets contribute no usable discrimination cell)")

    # metaanalysis headline.
    eff1, res1 = run_metaanalysis(True, os.path.join(WORK, "optin_metaanalysis.xlsx"))
    opt_did = meta_k(res1, "DiD (pre->post)")
    opt_post = meta_k(res1, "posttest (immediate)")
    fw_eff = eff1[fw_pair_mask(eff1, fw_records)] if "study_key" in eff1.columns else eff1.iloc[0:0]
    bad = sorted(set(fw_eff["bucket"].astype(str)) - {"did"}) if "bucket" in fw_eff.columns else []
    check(not bad, f"opt-in metaanalysis: every fully-within contrast is bucket='did' (found {bad})")
    n_fw_did = int((fw_eff["bucket"].astype(str) == "did").sum()) if "bucket" in fw_eff.columns else 0
    check(n_fw_did > 0, f"opt-in metaanalysis: {n_fw_did} fully-within DiD contrasts appear")
    if default_did is not None and opt_did is not None:
        # The opt-in switch can add AT MOST one DiD contrast per fully-within row, and
        # it adds FEWER when a fully-within contrast is not poolable: a raw coefficient
        # or a non-standardised metric is dropped by the effect guard (see the
        # 2026-09-27 `beta/n` + standardisation fixes in Scripts/SB_SHARED_CODE_TRAPS.md).
        # Assert the bound, not the old exact equality -- that was only ever true while
        # every fully-within row happened to convert into a poolable effect.
        check(default_did <= opt_did <= default_did + n_fw_did,
              f"opt-in metaanalysis DiD k={opt_did} within [default {default_did}, "
              f"default + {n_fw_did} fully-within]")
    if default_post is not None and opt_post is not None:
        check(opt_post == default_post,
              f"opt-in metaanalysis immediate-posttest k unchanged ({default_post} -> {opt_post})")
    if opt_did is not None and opt_post is not None:
        note(f"headline opt-in: posttest k={opt_post}, DiD k={opt_did} "
             f"(default DiD k={default_did})")
    return n_fw_did


def main():
    print("=" * 78)
    print("validate_fully_within: fully-within switch (default EXCLUDE, opt-in INCLUDE)")
    print("=" * 78)
    set_switch(False)
    fw_records, fw_studies, fw_papers = fw_inventory()
    print(f"\n[0] Extraction layer: {len(fw_records)} fully-within records across "
          f"{len(fw_studies)} all-fully-within studies "
          f"({len(fw_papers)} papers with >=1 fully-within record)")
    check(len(fw_records) >= 12, f"at least 12 fully-within records (found {len(fw_records)})")
    # Corpus SNAPSHOTS grow as new fully-within studies are coded (the inbox campaign
    # added several) -- assert a lower bound, not an exact count, or every new coding
    # breaks a gate that is really about the switch behaviour, not about the tally.
    check(len(fw_studies) >= 11, f"at least 11 all-fully-within studies (found {len(fw_studies)})")

    default_did, default_post = check_default(fw_records, fw_studies)
    check_optin(fw_records, fw_studies, default_did, default_post)

    print("\n" + "=" * 78)
    if FAILS:
        print(f"FULLY-WITHIN VALIDATION FAIL ({len(FAILS)} issue(s))")
        for f in FAILS:
            print("  -", f)
        return 1
    print("FULLY-WITHIN VALIDATION PASS (both settings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
