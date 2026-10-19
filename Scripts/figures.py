"""Graphics for the analysis suite (``Scripts/ANALYSIS_SUITE_SPEC.md`` section E).

Produces, for both layers (``meta`` reported-effects layer and ``sdt``
signal-detection layer), and for every specification (``did_all``, ``did_cred``,
``post_all``, ``post_cred``, exclusions ON unless stated):

1. ``forest_<layer>_<spec>``        overall + per-domain forest
2. ``forest_four_specs_<layer>``    side-by-side forest of the four specs
3. ``funnel_<layer>_<spec>``        funnel with pooled estimate and Egger line
4. ``loo_<layer>_<spec>``           leave-one-out (study-level) forest
5. ``moderator_<layer>_<spec>``     data_available vs paper_only forest
6. ``petpeese_<layer>_<spec>``      PET / PEESE fitted lines (meta layer only;
                                    the SDT layer is replaced by
                                    ``sdtcomponents_sdt_<spec>``: true score,
                                    fake score and response bias)
7. ``exclusions_<layer>``           exclusions ON vs OFF forest
8. ``domain_<layer>_<spec>``        forest for the four susceptibility domains

Every figure is saved as 300-dpi PNG and PDF in ``figures/`` and is labelled
with the layer, spec, k and the pooled estimate.  Polarities are already
applied in the specs (higher = more desirable) and the domain restriction is
applied to the susceptibility domains.

Run:  python Scripts/figures.py [--specs post_all] [--layers meta sdt]
"""
import argparse
import math
import os
import sys

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import suite_common as SC  # noqa: E402
import publication_bias as PB  # noqa: E402

OUT = SC.FIGDIR
os.makedirs(OUT, exist_ok=True)

LAYERS = ["meta", "sdt"]
SPECS = ["did_all", "did_cred", "post_all", "post_cred",
         "persistence_all", "persistence_cred",
         "didpost_all", "didpost_cred", "fupost_all", "fupost_cred"]
SUSC = list(SC.DR.SUSCEPTIBILITY_DOMAINS)
# Paper-ready labels (no internal spec/layer/domain codes).
SPEC_LABEL = {
    "did_all": "Pretest-corrected difference (DiD), all outcomes",
    "did_cred": "Pretest-corrected difference (DiD), credibility",
    "post_all": "Posttest, all outcomes",
    "post_cred": "Posttest, credibility",
    "persistence_all": "Follow-up (persistence), all outcomes",
    "persistence_cred": "Follow-up (persistence), credibility",
    "didpost_all": "Pretest-corrected where possible, all outcomes",
    "didpost_cred": "Pretest-corrected where possible, credibility",
    "fupost_all": "Follow-up where available, all outcomes",
    "fupost_cred": "Follow-up where available, credibility",
    "fudidpost_all": "Follow-up, else pretest-corrected, all outcomes",
    "fudidpost_cred": "Follow-up, else pretest-corrected, credibility",
}
SPEC_SHORT = {
    "did_all": "DiD - all outcomes",
    "did_cred": "DiD - credibility",
    "post_all": "Posttest - all outcomes",
    "post_cred": "Posttest - credibility",
    "persistence_all": "Follow-up - all outcomes",
    "persistence_cred": "Follow-up - credibility",
    "fudidpost_all": "Follow-up else DiD - all outcomes",
    "fudidpost_cred": "Follow-up else DiD - credibility",
}
# W6-AN: the app's DEFAULT selection (pretest = all, follow-up = all) resolves to
# the derived ``fudidpost_*`` specification, which the pipeline does not
# materialise.  Its forest figures are built here from the already-materialised
# didpost/fupost rows, with the same priority rule as the coding app
# (follow-up > pretest-corrected > immediate posttest).
FUDIDPOST_SPECS = ["fudidpost_all", "fudidpost_cred"]
# spec -> (pretest-corrected base, follow-up base)
FUDIDPOST_BASES = {
    "fudidpost_all": ("didpost_all", "persistence_all"),
    "fudidpost_cred": ("didpost_cred", "persistence_cred"),
}
LAYER_LABEL = {"meta": "Reported effects", "sdt": "Signal detection (discrimination)"}
DOMAIN_LABEL = {
    # 2026-09-19: `belief_accuracy` merged into `credibility` (PI decision), so
    # the retired label is intentionally absent from figures.
    "credibility": "Credibility",
    "sharing_intention": "Sharing intention",
    "technique_recognition": "Technique recognition",
    "attitude_policy": "Attitude / policy",
    "search_behavior": "Search behavior",
    "trust": "Trust",
    "media_literacy_attitudes": "Media-literacy attitudes",
    "critical_thinking": "Critical thinking",
    "other": "Other",
}
C_GREY = "#4d4d4d"
C_ACCENT = "#1f5fa8"
C_WARN = "#b2182b"


def dlabel(dom):
    return DOMAIN_LABEL.get(str(dom), str(dom).replace("_", " ").capitalize())


def save(fig, name):
    for ext in ("png", "pdf"):
        fig.savefig(os.path.join(OUT, f"{name}.{ext}"), dpi=300, bbox_inches="tight")
    plt.close(fig)
    print("  figure:", os.path.join(OUT, name + ".png"))


def caption(layer, spec, k, est, lo=None, hi=None, extra=""):
    ci = f", 95% CI [{lo:.3f}, {hi:.3f}]" if (lo is not None and np.isfinite(lo)) else ""
    return (f"{LAYER_LABEL[layer]} | {SPEC_LABEL.get(spec, spec)} | k={k} | "
            f"pooled={est:.3f}{ci}" + (f" | {extra}" if extra else ""))


def _forest_panel(ax, items, overall, xlabel, title):
    """items: list of (label, est, lo, hi, k); overall: dict or None."""
    n = len(items)
    ys = np.arange(n)[::-1]
    est = np.array([it[1] for it in items], float)
    lo = np.array([it[2] for it in items], float)
    hi = np.array([it[3] for it in items], float)
    ax.errorbar(est, ys, xerr=[est - lo, hi - est], fmt="o", color=C_ACCENT,
                ecolor=C_GREY, ms=5, lw=1.2, capsize=2)
    labels = [f"{it[0]}  (k={it[4]})" for it in items]
    ax.set_yticks(ys)
    ax.set_yticklabels(labels, fontsize=7)
    ax.axvline(0.0, color="black", lw=0.8, ls="--")
    if overall is not None and n:
        y0 = -1.4
        ax.errorbar([overall["est"]], [y0],
                    xerr=[[overall["est"] - overall["lo"]], [overall["hi"] - overall["est"]]],
                    fmt="D", color=C_WARN, ecolor=C_WARN, ms=7, lw=1.6, capsize=3)
        ax.set_ylim(-2.4, n - 0.3)
        ax.set_yticks(list(ys) + [y0])
        ax.set_yticklabels(labels + [f"OVERALL  (k={overall['k']})"], fontsize=7)
    ax.set_xlabel(xlabel, fontsize=8)
    ax.set_title(title, fontsize=9)
    ax.tick_params(axis="x", labelsize=7)


def data_for(specs, layer, spec, excl="on"):
    d = PB.select(specs, layer, spec, excl)
    yi = d["effect"].to_numpy(float)
    vi = d["var"].to_numpy(float)
    return d, yi, vi


def _fudidpost_key(layer):
    """Outcome identity used to decide which pretest-corrected rows a follow-up
    row supersedes (mirrors the coding app's ``_derived_rows`` fudidpost branch)."""
    if layer == "sdt":
        return lambda r: (str(r["study_key"]), str(r["experiment_id"]),
                          str(r.get("dv_domain") or ""))
    return lambda r: (str(r["study_key"]), str(r["experiment_id"]),
                      str(r.get("dv_name") or r.get("dv_domain") or ""))


def fudidpost_frame(specs, layer):
    """Materialise the app-only ``fudidpost_all`` / ``fudidpost_cred`` rows.

    The coding app composes the derived fudidpost specification on the fly as
    follow-up > pretest-corrected (DiD) > immediate posttest.  The pipeline's
    ``derive_specs`` only materialises didpost/fupost, so there is no
    ``fudidpost_*`` row set for the figure writer.  This builds it from the
    already-selected didpost and persistence rows (both pass through the shared
    selector first, so the derived frame inherits the eligible/exclusions/|DiD|
    quality guard) and is restricted to the didpost study population, exactly as
    the app does.
    """
    out = []
    for spec, (inner_spec, fu_spec) in FUDIDPOST_BASES.items():
        for excl in ("on", "off"):
            inner = PB.select(specs, layer, inner_spec, excl)
            if not len(inner):
                continue
            population = set(inner["study_key"].astype(str))
            fu = PB.select(specs, layer, fu_spec, excl)
            if len(fu):
                fu = fu[fu["study_key"].astype(str).isin(population)]
            if len(fu):
                key = _fudidpost_key(layer)
                superseded = {key(r) for _, r in fu.iterrows()}
                rest = inner[[key(r) not in superseded for _, r in inner.iterrows()]]
                d = pd.concat([fu, rest], ignore_index=True)
            else:
                d = inner.copy()
            d["spec"] = spec
            out.append(d)
    if not out:
        return specs.iloc[0:0].copy()
    return pd.concat(out, ignore_index=True)


def domain_items(d, domains=None, min_k=1):
    items = []
    for dom, g in d.groupby("dv_domain"):
        if domains is not None and dom not in domains:
            continue
        if len(g) < min_k:
            continue
        r = SC.dl_random(g["effect"], g["var"])
        items.append((dlabel(dom), r["est"], r["lo"], r["hi"], r["k"]))
    items.sort(key=lambda t: t[1])
    return items


# ---------------------------------------------------------------------------
# figures
# ---------------------------------------------------------------------------


def fig_forest(specs, layer, spec):
    d, yi, vi = data_for(specs, layer, spec)
    if not len(d):
        return None
    overall = SC.dl_random(yi, vi)
    items = domain_items(d)
    fig, ax = plt.subplots(figsize=(7.2, max(3.0, 0.32 * len(items) + 1.4)))
    _forest_panel(ax, items, overall, "effect (higher = more desirable)",
                  "Per-domain effects")
    fig.suptitle(caption(layer, spec, overall["k"], overall["est"], overall["lo"], overall["hi"]),
                 fontsize=9, y=1.02)
    fig.tight_layout()
    return save(fig, f"forest_{layer}_{spec}")


def fig_four_specs(specs, layer):
    n = len(SPECS)
    nrow = (n + 1) // 2
    fig, axes = plt.subplots(nrow, 2, figsize=(10.5, 3.5 * nrow))
    axes = np.atleast_1d(axes).ravel()
    for ax, spec in zip(axes, SPECS):
        d, yi, vi = data_for(specs, layer, spec)
        if not len(d):
            ax.axis("off")
            continue
        overall = SC.dl_random(yi, vi)
        items = domain_items(d)
        _forest_panel(ax, items, overall,
                      "effect", SPEC_LABEL.get(spec, spec))
        ax.set_title(f"{SPEC_SHORT.get(spec, spec)} | k={overall['k']} | pooled={overall['est']:.3f} "
                     f"[{overall['lo']:.3f}, {overall['hi']:.3f}]", fontsize=8)
    for ax in axes[n:]:
        ax.axis("off")
    fig.suptitle(f"{LAYER_LABEL[layer]} - all specifications (exclusions ON)", fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    return save(fig, f"forest_four_specs_{layer}")


def fig_funnel(specs, layer, spec):
    d, yi, vi = data_for(specs, layer, spec)
    if len(d) < 3:
        return None
    se = np.sqrt(vi)
    pooled = SC.dl_random(yi, vi)
    eg = SC.egger_pet(yi, vi)
    fig, ax = plt.subplots(figsize=(6.4, 5.2))
    ax.scatter(yi, se, s=14, color=C_ACCENT, alpha=0.65, edgecolor="none")
    ymax = float(np.nanmax(se)) * 1.05
    yy = np.linspace(0, ymax, 100)
    ax.plot([pooled["est"], pooled["est"]], [0, ymax], color=C_WARN, lw=1.4,
            label=f"pooled = {pooled['est']:.3f}")
    ax.plot(pooled["est"] - 1.96 * yy, yy, color=C_GREY, ls="--", lw=0.9,
            label="pseudo 95% funnel")
    ax.plot(pooled["est"] + 1.96 * yy, yy, color=C_GREY, ls="--", lw=0.9)
    if np.isfinite(eg["slope_se"]):
        ax.plot(eg["intercept"] + eg["slope_se"] * yy, yy, color="black", lw=1.2,
                label=(f"Egger slope {eg['slope_se']:.3f} "
                       f"(p={eg.get('slope_p', eg['p']):.3g})"))
    ax.invert_yaxis()
    # keep the x-axis focused on the bulk of the data so a few extreme coded
    # coefficients cannot compress the funnel (the Egger line is clipped).
    xlo, xhi = np.nanpercentile(yi, [1, 99])
    pad = 0.15 * (xhi - xlo + 1e-9)
    ax.set_xlim(xlo - pad, xhi + pad)
    for ln in ax.lines:
        ln.set_clip_on(True)
    ax.set_xlabel("effect (higher = more desirable)", fontsize=9)
    ax.set_ylabel("standard error", fontsize=9)
    ax.set_title(caption(layer, spec, pooled["k"], pooled["est"], pooled["lo"], pooled["hi"],
                         (f"Egger slope p={eg['slope_p']:.3g}"
                          if np.isfinite(eg.get("slope_p", np.nan)) else "")),
                 fontsize=8)
    ax.legend(fontsize=7, loc="best")
    fig.tight_layout()
    return save(fig, f"funnel_{layer}_{spec}")


def fig_loo(specs, layer, spec, max_show=45):
    d, yi, vi = data_for(specs, layer, spec)
    if len(d) < 4:
        return None
    full, loo = SC.leave_one_out(yi, vi, d["study_key"].astype(str).to_numpy(), is_study=True)
    if not len(loo):
        return None
    loo = loo.copy()
    n_total = len(loo)
    if len(loo) > max_show:
        infl = loo[loo["influential"].astype(bool)]
        rest = loo[~loo["influential"].astype(bool)].reindex(
            loo.loc[~loo["influential"].astype(bool), "delta"].abs().sort_values(ascending=False).index)
        shown = pd.concat([infl, rest]).drop_duplicates("omitted").head(max_show)
        note = f"showing {len(shown)} of {n_total} study-LOO estimates (influential + largest |delta|)"
    else:
        shown = loo.sort_values("est")
        note = ""
    shown = shown.sort_values("est")
    items = [(o, e, l, h, int(k)) for o, e, l, h, k in
             zip(shown["omitted"], shown["est"], shown["lo"], shown["hi"], shown["k_omitted"])]
    fig, ax = plt.subplots(figsize=(7.0, max(3.0, 0.3 * len(items) + 1.2)))
    est = np.array([it[1] for it in items], float)
    lo = np.array([it[2] for it in items], float)
    hi = np.array([it[3] for it in items], float)
    ys = np.arange(len(items))[::-1]
    infl_flags = shown["influential"].to_numpy(bool)
    colors = [C_WARN if b else C_ACCENT for b in infl_flags]
    ax.errorbar(est, ys, xerr=[est - lo, hi - est], fmt="none", ecolor=C_GREY, lw=0.9, capsize=2)
    ax.scatter(est, ys, c=colors, s=16, zorder=3)
    ax.axvline(full["est"], color=C_WARN, lw=1.3, ls="--",
               label=f"full pooled = {full['est']:.3f}")
    ax.set_yticks(ys)
    ax.set_yticklabels([f"omit {it[0]}  (k={it[4]})" for it in items], fontsize=6.5)
    ax.set_xlabel("leave-one-study-out pooled estimate", fontsize=9)
    ax.set_title(caption(layer, spec, full["k"], full["est"], full["lo"], full["hi"],
                         (note + " | " if note else "") +
                         f"{int(infl_flags.sum())} influential (red)"), fontsize=8)
    ax.legend(fontsize=7)
    fig.tight_layout()
    return save(fig, f"loo_{layer}_{spec}")


def fig_moderator(specs, layer, spec):
    d, yi, vi = data_for(specs, layer, spec)
    if not len(d):
        return None
    mod = pd.to_numeric(d["data_available"], errors="coerce").fillna(0).astype(int)
    levels = sorted(set(mod))
    items = []
    for lev in levels:
        g = d[mod == lev]
        r = SC.dl_random(g["effect"], g["var"])
        items.append(("Primary data available" if lev == 1 else "Paper only",
                      r["est"], r["lo"], r["hi"], r["k"]))
    overall = SC.dl_random(yi, vi)
    fig, ax = plt.subplots(figsize=(6.6, 3.2))
    _forest_panel(ax, items, overall, "effect", "Data-availability moderator")
    if len(levels) > 1:
        res = SC._wls(yi, mod.to_numpy(float), 1 / vi)
        extra = f"meta-reg slope={res['b']:.3f} (p={res['p_b']:.3g})"
    else:
        extra = f"only one level present (data_available={levels[0] if levels else 'n/a'}); no moderator contrast"
    fig.suptitle(caption(layer, spec, overall["k"], overall["est"], overall["lo"], overall["hi"], extra),
                 fontsize=9, y=1.04)
    fig.tight_layout()
    return save(fig, f"moderator_{layer}_{spec}")


def fig_petpeese(specs, layer, spec):
    d, yi, vi = data_for(specs, layer, spec)
    if len(d) < 4:
        return None
    se = np.sqrt(vi)
    pt = SC.pet_peese(yi, vi)
    fig, ax = plt.subplots(figsize=(6.4, 5.0))
    ax.scatter(se, yi, s=14, color=C_ACCENT, alpha=0.6, edgecolor="none", label="contrasts")
    xmax = float(np.nanmax(se)) * 1.05
    xx = np.linspace(0, xmax, 100)
    pet = pt["pet"]
    peese = pt["peese"]
    ax.plot(xx, pet["intercept"] + pet["slope_se"] * xx, color="black", lw=1.3,
            label=f"PET: {pet['intercept']:.3f} (p={pet['p']:.3g})")
    ax.plot(xx, peese["intercept"] + peese["slope_var"] * xx ** 2, color=C_WARN, lw=1.3,
            ls="--", label=f"PEESE: {peese['intercept']:.3f} (p={peese['p']:.3g})")
    ax.axhline(0, color="gray", lw=0.8, ls=":")
    ylo, yhi = np.nanpercentile(yi, [1, 99])
    pad = 0.15 * (yhi - ylo + 1e-9)
    ax.set_ylim(ylo - pad, yhi + pad)
    for ln in ax.lines:
        ln.set_clip_on(True)
    ax.set_xlabel("standard error", fontsize=9)
    ax.set_ylabel("effect", fontsize=9)
    ax.set_title(caption(layer, spec, int(pt["pet"]["k"]), pt["estimate"], pt["lo"], pt["hi"],
                         f"prefer {pt['prefer']}"), fontsize=8)
    ax.legend(fontsize=7)
    fig.tight_layout()
    return save(fig, f"petpeese_{layer}_{spec}")


def fig_exclusions(specs, layer):
    fig, ax = plt.subplots(figsize=(7.0, 4.6))
    items = []
    for spec in SPECS:
        for excl, tag in (("on", "ON"), ("off", "OFF")):
            d, yi, vi = data_for(specs, layer, spec, excl)
            if not len(d):
                continue
            r = SC.dl_random(yi, vi)
            items.append((f"{SPEC_SHORT.get(spec, spec)} - exclusions {tag}",
                          r["est"], r["lo"], r["hi"], r["k"]))
    if not items:
        plt.close(fig)
        return None
    items.sort(key=lambda t: t[1])
    _forest_panel(ax, items, None, "effect", "Exclusions ON vs OFF")
    fig.suptitle(f"{LAYER_LABEL[layer]} - exclusions ON vs OFF (all four specs)", fontsize=10, y=1.02)
    fig.tight_layout()
    return save(fig, f"exclusions_{layer}")


def fig_domain(specs, layer, spec):
    d, yi, vi = data_for(specs, layer, spec)
    if not len(d):
        return None
    items = domain_items(d, domains=SUSC, min_k=1)
    if not items:
        return None
    overall = SC.dl_random(yi, vi)
    fig, ax = plt.subplots(figsize=(6.8, max(2.6, 0.6 * len(items) + 1.4)))
    _forest_panel(ax, items, None, "effect (higher = more desirable)",
                  "Susceptibility domains")
    fig.suptitle(caption(layer, spec, overall["k"], overall["est"], overall["lo"], overall["hi"],
                         "susceptibility domains only"), fontsize=9, y=1.03)
    fig.tight_layout()
    return save(fig, f"domain_{layer}_{spec}")


def fig_intervention_types(specs, layer, spec="post_all"):
    """Effects by intervention type (one diagram, reported-effects layer)."""
    if layer != "meta":
        return None
    d = data_for(specs, layer, spec)[0]
    if len(d) < 4:
        return None
    d = SC.attach_intervention_type(d, layer)
    items = []
    for itype, g in d.groupby("intervention_type"):
        if len(g) < 1:
            continue
        r = SC.dl_random(g["effect"], g["var"])
        items.append((str(itype), r["est"], r["lo"], r["hi"], r["k"]))
    if len(items) < 2:
        return None
    items.sort(key=lambda t: t[1])
    overall = SC.dl_random(d["effect"], d["var"])
    fig, ax = plt.subplots(figsize=(7.4, max(3.4, 0.34 * len(items) + 1.6)))
    _forest_panel(ax, items, overall, "effect (higher = more desirable)",
                  "Pooled effect by intervention type")
    fig.suptitle(caption(layer, spec, overall["k"], overall["est"], overall["lo"], overall["hi"],
                         "exclusions ON"), fontsize=9, y=1.02)
    return save(fig, f"intervention_types_{layer}")


def fig_sdt_components(specs, layer, spec):
    """SDT layer: true score, fake score and response bias (replaces PET-PEESE)."""
    if layer != "sdt":
        return None
    rows = PB.sdt_component_rows(specs, layer, spec, "on")
    items = [(r["component"], r["est"], r["lo"], r["hi"], r["k"])
             for r in rows
             if np.isfinite(r["est"]) and np.isfinite(r["lo"]) and np.isfinite(r["hi"])]
    if not items:
        return None
    d = PB.select(specs, layer, spec, "on")
    overall = SC.dl_random(d["effect"], d["var"]) if len(d) else None
    fig, ax = plt.subplots(figsize=(7.0, 2.6))
    _forest_panel(ax, items, None, "mean endorsement (0–1) · criterion c",
                  "True score, fake score and response bias (criterion c)")
    if overall and np.isfinite(overall["est"]) and np.isfinite(overall["lo"]):
        fig.suptitle(caption(layer, spec, overall["k"], overall["est"],
                             overall["lo"], overall["hi"]), fontsize=9, y=1.04)
    return save(fig, f"sdtcomponents_{layer}_{spec}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--specs", nargs="+", default=SPECS)
    ap.add_argument("--layers", nargs="+", default=LAYERS)
    ap.add_argument("--include-fully-within", action="store_true",
                    help="Include fully-within (single-arm, no-control) studies "
                         "(default: excluded).")
    args = ap.parse_args()
    if args.include_fully_within:
        os.environ["INCLUDE_FULLY_WITHIN"] = "1"
    SC.announce_fully_within("figures")
    SC.ensure_specs()
    specs = SC.load_specs()
    specs["study_key"] = specs["study_key"].astype(str)
    # W6-AN: the app's default selection (pretest=all, follow-up=all) resolves to
    # the derived fudidpost specification, which the pipeline never materialises.
    # Append its rows so the same forest machinery can draw it for both layers.
    specs = pd.concat([specs, fudidpost_frame(specs, "meta"),
                       fudidpost_frame(specs, "sdt")], ignore_index=True)
    print("figures ->", OUT)
    for layer in args.layers:
        for spec in args.specs:
            fig_forest(specs, layer, spec)
            fig_funnel(specs, layer, spec)
            fig_loo(specs, layer, spec)
            fig_moderator(specs, layer, spec)
            if layer == "sdt":
                fig_sdt_components(specs, layer, spec)
            else:
                fig_petpeese(specs, layer, spec)
            fig_domain(specs, layer, spec)
        # The derived follow-up-else-DiD specification: forest only (that is the
        # figure the Effects panel selects for the default controls).
        for spec in FUDIDPOST_SPECS:
            fig_forest(specs, layer, spec)
        fig_four_specs(specs, layer)
        fig_exclusions(specs, layer)
        fig_intervention_types(specs, layer)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
