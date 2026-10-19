"""Coding Studio — statistical-core **shim**.

Every estimator used by the app now lives in the shared pipeline module
``Scripts/stat_core.py`` (interface §2 of
``Results/INTERFACES_codingstudio_rework.md``).  This module exists only so the
app's historical import surface keeps working; it defines **no formula** so the
app can never drift from the pipeline again (audit findings H1/H2/H4/M1).

All names below are re-exports of ``stat_core`` (which ``suite_common`` itself
re-exports for the pipeline).  When ``stat_core`` / numpy is unavailable the
import fails loudly — the app must not silently degrade to a second
implementation (plan D6: numpy/scipy are bundled in the packaged app).

Decision record: ``Results/TRACK_A_PIPELINE_CHANGES.md``.
"""

from __future__ import annotations

import os
import sys

# ``stat_core`` lives one level up (Scripts/), not in Scripts/coding_web/.
_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

# Public constants (the app's prior/decorator vocabulary).
from stat_core import (  # noqa: E402,F401
    BAYES_MU_SD,
    BAYES_MU_SD_PRESETS,
    BAYES_TAU_MAX,
    BAYES_TAU_PRESETS,
    BAYES_TAU_PRIOR_KINDS,
    BAYES_TAU_SD,
    _bayes_log_weights,
    _bayes_meta,
    _bayes_pool,
    _bayes_tau_grid,
    _bayes_tau_within,
    _chi2_sf,
    _chol_logdet,
    _dl_pool,
    _dl_tau2,
    _mat_inv,
    _mixture_summary,
    _norm_cdf,
    _probit,
    _dprime,
    _resolve_bayes_priors,
    _study_blocks,
    _study_level,
    _t_ppf,
    _t_sf,
    _tau_log_prior,
    _tau_prior_density0,
    _two_level_components,
    _wls,
    _wls_cluster,
    bayes_meta,
    bayes_pool,
    bayes_tau_within,
    cluster_robust,
    dl_random,
    dprime,
    fe_pool,
    pool_dependence,
    probit,
    rve_intercept,
    three_level_re,
    two_level_re,
    wls,
)
