"""Browser-mode route policy: what a headless worker may still serve.

Browser mode (``Results/PLAN_online_only.md`` §7) runs the desktop backend as a
per-user worker on a server-side mirror.  Everything that needs the desktop's
own machine is refused there, because it would either reach the host
filesystem (file pickers, project-root switching, local-folder/online connect,
executable updates), write the read-only scheme library in the image, or run
user-pushed code / the analysis pipeline on the server (R5).  Reading the
*results* of those desktop-only actions stays allowed where the plan says so
(e.g. ``GET /api/repro/run/status``, ``GET /api/analysis/run/status``,
``GET /api/online/admin/publish`` for progress, ``GET /api/online/cache``).

Every route the app registers is classified here, and the classification is
**total**: :func:`classification` answers ``desktop_only`` for an unknown
endpoint, so a route added later can never slip through unclassified (a test
over ``app.url_map`` fails until it is added below).  The installation is a
single ``before_request`` hook; the app is unchanged when
``app.config["BROWSER_MODE"]`` is false.

Three classes
-------------
``allow``         any role may call it in browser mode;
``owner_only``    only a project owner may; a coder/viewer gets 403;
``desktop_only``  refused for every role (403 ``desktop_only``).

The role vocabulary is the app's own: ``owner`` / ``coder`` / ``viewer``.  Only
the owner-only class distinguishes them here — per-tag, per-task, lock and
"read-only viewer" enforcement stays where it is (the server's
``visibility.can_write`` and the app's ``routes_online.guard_coder_tag``), so a
browser session behaves exactly like a desktop session of the same member.  A
caller may pass the ``/api/auth/me`` docket itself (``{"user": {"role": ...}}``)
or a bare role; a missing role is treated as ``coder`` — never as ``owner``.

Usage (the worker's ``before_request``)::

    from browser_policy import browser_route_allowed, denial
    ok, message = browser_route_allowed(request.endpoint, request.method, role)
    if not ok:
        status, body = denial(request.endpoint, request.method, role)
        return jsonify(body), status

or, as a ready-made hook::

    from browser_policy import install
    install(app)          # does nothing unless app.config["BROWSER_MODE"]
"""

from __future__ import annotations

#: The three policy classes.
ALLOW = "allow"
OWNER_ONLY = "owner_only"
DESKTOP_ONLY = "desktop_only"

#: The roles the policy understands (the app's own project roles).
ROLES = ("owner", "coder", "viewer")

#: Methods that never change anything (used only for the messages).
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# --------------------------------------------------------------------------- #
# The route table.  Keys are Flask endpoint names (``<blueprint>.<view>``), the
# one identity that survives a URL change; values are the policy class.  An
# endpoint with several methods shares one class (the app has no route whose
# GET is allowed and whose POST is not a separate endpoint).
# --------------------------------------------------------------------------- #
CLASSIFICATION: dict[str, str] = {
    # --- agreement: every view reads the mirror and settles decisions in it ---
    "agreement.api_agreement_abstract_conflicts": ALLOW,
    "agreement.api_agreement_abstract_llm_excluded": ALLOW,
    "agreement.api_agreement_abstract_reliability": ALLOW,
    "agreement.api_agreement_abstract_resolve": ALLOW,
    "agreement.api_agreement_conflicts": ALLOW,
    "agreement.api_agreement_masters": ALLOW,
    "agreement.api_agreement_reliability": ALLOW,
    "agreement.api_agreement_resolve": ALLOW,
    "agreement.api_agreement_settle_bulk": ALLOW,
    "agreement.api_agreement_settlements": ALLOW,
    "agreement.api_agreement_settlements_csv": ALLOW,
    "agreement.api_agreement_study_status": ALLOW,

    # --- analysis: reads/computes in the process; running the pipeline is R5 ---
    "analysis.api_analysis_bayes": ALLOW,
    "analysis.api_analysis_export": ALLOW,            # heavy lane, §7
    "analysis.api_analysis_figure": ALLOW,
    "analysis.api_analysis_moderate": ALLOW,
    "analysis.api_analysis_moderators": ALLOW,
    "analysis.api_analysis_note": ALLOW,
    "analysis.api_analysis_overview": ALLOW,
    "analysis.api_analysis_pooled": ALLOW,
    "analysis.api_analysis_run": DESKTOP_ONLY,        # R5: never on the server
    "analysis.api_analysis_run_status": ALLOW,        # reading the result is fine
    "analysis.api_analysis_subgroup": ALLOW,
    "analysis.api_analysis_table": ALLOW,
    "apa.api_analysis_apa": ALLOW,                    # heavy lane, §7
    "apa.api_analysis_prisma_counts": ALLOW,
    "apa.api_analysis_prisma_png": ALLOW,

    # --- core: coding against the mirror is the browser's job; root/pickers ---
    "core.api_coding": ALLOW,
    "core.api_coding_delete": ALLOW,
    "core.api_extraction": ALLOW,
    "core.api_notes": ALLOW,
    "core.api_papers": ALLOW,
    "core.api_papers_progress": ALLOW,
    "core.api_pdfinfo": ALLOW,
    "core.api_pdfsearch": ALLOW,
    "core.api_pdftext": ALLOW,
    "core.api_pick_root": DESKTOP_ONLY,               # native folder dialog
    "core.api_root": ALLOW,                           # GET: which root is open
    "core.api_save": ALLOW,
    "core.api_schema": ALLOW,
    "core.api_set_root": DESKTOP_ONLY,                # fixed root in browser mode
    "core.api_text": ALLOW,
    "core.index": ALLOW,
    "core.page_image": ALLOW,
    "core.paper": ALLOW,
    "core.static_files": ALLOW,
    "core.supplement_file": ALLOW,

    # --- coverage / criteria (criteria validate only reads the payload) -------
    "coverage.api_coverage": ALLOW,
    "coverage.api_coverage_study": ALLOW,
    "coverage.api_gapwalk": ALLOW,
    "criteria.api_criteria_validate": ALLOW,

    # --- full text: decisions in the mirror; the master merge is the owner's --
    "fulltext.api_fulltext_criteria": ALLOW,
    "fulltext.api_fulltext_excluded": ALLOW,
    "fulltext.api_fulltext_get": ALLOW,
    "fulltext.api_fulltext_merge": OWNER_ONLY,        # rewrites the derived master
    "fulltext.api_fulltext_set": ALLOW,

    # --- harmonizer / assignments: coding-layer files in the mirror ----------
    "harmonizer.api_assignments": ALLOW,
    "harmonizer.api_assignments_apply": ALLOW,
    "harmonizer.api_assignments_save": ALLOW,
    "harmonizer.api_harmonizer": ALLOW,
    "harmonizer.api_harmonizer_decode": ALLOW,
    "harmonizer.api_harmonizer_index": ALLOW,
    "harmonizer.api_harmonizer_missing": ALLOW,
    "harmonizer.api_harmonizer_save": ALLOW,
    "harmonizer.api_rawvar": ALLOW,
    "harmonizer_agreement.api_harmonizer_conflicts": ALLOW,
    "harmonizer_agreement.api_harmonizer_reliability": ALLOW,
    "harmonizer_agreement.api_harmonizer_resolve": ALLOW,

    # --- the derived master: compiled inside the mirror ----------------------
    "master.api_master_compile": ALLOW,
    "master.api_master_status": ALLOW,

    # --- online: the gateway owns the session; the syncer stays available ----
    "online.api_admin_assignment_run_delete": ALLOW,
    "online.api_admin_assignment_runs": ALLOW,
    "online.api_admin_assignments": ALLOW,
    "online.api_admin_audit": ALLOW,
    "online.api_admin_lock_force": ALLOW,
    "online.api_admin_member_delete": ALLOW,
    "online.api_admin_member_put": ALLOW,
    "online.api_admin_members": ALLOW,
    "online.api_admin_publish": DESKTOP_ONLY,         # source_root is a server path
    "online.api_admin_publish_cancel": DESKTOP_ONLY,
    "online.api_admin_publish_progress": ALLOW,       # reading progress is fine
    "online.api_admin_settings_get": ALLOW,
    "online.api_admin_settings_patch": ALLOW,
    "online.api_admin_user_create": ALLOW,
    "online.api_admin_user_patch": ALLOW,
    "online.api_admin_users": ALLOW,
    "online.api_assignments": ALLOW,
    "online.api_cache_clear": DESKTOP_ONLY,           # no LRU on the server; symlinks
    "online.api_cache_get": ALLOW,
    "online.api_cache_pin": DESKTOP_ONLY,
    "online.api_cache_preload": DESKTOP_ONLY,
    "online.api_cache_set": DESKTOP_ONLY,
    "online.api_cache_unpin": DESKTOP_ONLY,
    "online.api_close": DESKTOP_ONLY,                 # the gateway owns the project
    "online.api_conflict_detail": ALLOW,
    "online.api_conflict_resolve": ALLOW,
    "online.api_conflicts": ALLOW,
    "online.api_connect": DESKTOP_ONLY,               # local login/session
    "online.api_done_list": ALLOW,
    "online.api_done_mark": ALLOW,
    "online.api_lock_acquire": ALLOW,
    "online.api_lock_release": ALLOW,
    "online.api_locks": ALLOW,
    "online.api_logout": DESKTOP_ONLY,                # the gateway owns the session
    "online.api_open": DESKTOP_ONLY,
    "online.api_presence": ALLOW,
    "online.api_status": ALLOW,
    "online.api_sync": ALLOW,

    # --- project: the recent list is local config; the summary is a read -----
    "project.api_project_new": DESKTOP_ONLY,          # creates folders on the host
    "project.api_project_recent": DESKTOP_ONLY,
    "project.api_project_summary": ALLOW,
    "project.api_projects": DESKTOP_ONLY,             # local project paths

    # --- reproduction: running user-pushed code is desktop-only --------------
    "repro.api_repro_dossier": ALLOW,
    "repro.api_repro_dossier_edit": DESKTOP_ONLY,     # in-place append into Results/
    "repro.api_repro_dossier_files": ALLOW,
    "repro.api_repro_dossier_log": ALLOW,
    "repro.api_repro_overview": ALLOW,
    "repro.api_repro_run": DESKTOP_ONLY,              # executes Datasets/*.py
    "repro.api_repro_run_cancel": DESKTOP_ONLY,       # kills the desktop run's processes
    "repro.api_repro_run_status": ALLOW,              # reading the result is fine
    "repro.api_repro_study": ALLOW,

    # --- scheme: reads are fine; writes the app-dir library (read-only image)
    "scheme.api_criteria": ALLOW,
    "scheme.api_project": ALLOW,
    "scheme.api_put_criteria": DESKTOP_ONLY,
    "scheme.api_put_scheme": DESKTOP_ONLY,
    "scheme.api_scheme": ALLOW,
    "scheme.api_scheme_assign": DESKTOP_ONLY,
    "scheme.api_scheme_blocks": ALLOW,
    "scheme.api_scheme_blocks_edit": DESKTOP_ONLY,
    "scheme.api_scheme_bundle": ALLOW,
    "scheme.api_scheme_field_delete": DESKTOP_ONLY,
    "scheme.api_scheme_fields": DESKTOP_ONLY,
    "scheme.api_scheme_import": DESKTOP_ONLY,
    "scheme.api_scheme_kind_delete": DESKTOP_ONLY,
    "scheme.api_scheme_kinds": DESKTOP_ONLY,
    "scheme.api_scheme_roles": ALLOW,
    "scheme.api_scheme_source": ALLOW,
    "scheme.api_scheme_validate": ALLOW,              # validates a payload, writes nothing
    "scheme.api_scheme_vocabularies": ALLOW,

    # --- screening: the browser's main task; merging is the owner's ----------
    "screening.api_screening_abstract": ALLOW,
    "screening.api_screening_decision": ALLOW,
    "screening.api_screening_list": ALLOW,
    "screening.api_screening_merge": OWNER_ONLY,      # rewrites the derived master
    "screening.api_screening_merge_status": ALLOW,
    "screening.api_screening_meta": ALLOW,

    # --- settings: project settings live in the mirror; exe updates do not ---
    "settings.api_settings_project": ALLOW,
    "settings.api_settings_project_reset": ALLOW,
    "settings.api_settings_project_save": ALLOW,
    "settings.api_update_apply": DESKTOP_ONLY,        # replaces the executable
    "settings.api_update_check": DESKTOP_ONLY,        # queries GitHub for the exe
    "settings.api_update_download": DESKTOP_ONLY,
    "settings.api_update_state": DESKTOP_ONLY,

    # --- summary: reads in the process --------------------------------------
    "summary.api_summary_dimensions": ALLOW,
    "summary.api_summary_levels": ALLOW,
    "summary.api_summary_save": ALLOW,
    "summary.api_summary_variables": ALLOW,
}


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
def classification(endpoint: str, method: str | None = None) -> str:
    """The policy class of one route; ``desktop_only`` when it is unknown.

    *method* is accepted (and reported in messages) though no route needs a
    per-method class; it keeps the call shape of :func:`browser_route_allowed`
    identical to what a ``before_request`` has at hand.
    """
    return CLASSIFICATION.get(str(endpoint or ""), DESKTOP_ONLY)


def _desktop_message(endpoint, method):
    return (f"{endpoint or 'this route'} ({method}) is desktop-only: a browser "
            "session runs on the project's server-side mirror and cannot switch "
            "the project root, pick local folders, connect a local/online "
            "account, edit the scheme library, run the analysis or reproduction "
            "pipeline, update the executable, or reach the host filesystem. "
            "Use the CodingStudio desktop app for this.")


def _unknown_message(endpoint, method):
    return (f"{endpoint or 'this route'} ({method}) is not classified for browser "
            "mode and is refused by default.")


def _role_name(role) -> str:
    """The role of one call: a bare role, the ``/api/auth/me`` docket, or none.

    A worker reads the member's role from the store's ``me()`` answer, whose
    shape is ``{"user": {"role": ...}, "projects": [...]}``; accepting the
    docket keeps that call correct.  An unresolved role is the least-privileged
    member role (``coder``), never ``owner``.
    """
    if isinstance(role, dict):
        user = role.get("user") if isinstance(role.get("user"), dict) else {}
        role = user.get("role") or role.get("role")
    name = str(role or "").strip().lower()
    return name or "coder"


def browser_route_allowed(endpoint: str, method: str, role) -> tuple[bool, str]:
    """Whether *role* may call *endpoint* with *method* in browser mode.

    *endpoint* is the Flask endpoint name (``request.endpoint``), *role* one of
    ``owner`` / ``coder`` / ``viewer`` (or the ``/api/auth/me`` docket carrying
    it; a missing role counts as ``coder``).  Returns ``(True, "")`` when the
    call is allowed, else ``(False, message)`` where *message* is the text of
    the ``desktop_only`` / ``forbidden`` refusal (see :func:`denial`).  An
    unknown endpoint or an unknown explicit role is refused (default deny).
    """
    method = str(method or "GET").strip().upper()
    role = _role_name(role)
    endpoint = str(endpoint or "")
    if role not in ROLES:
        return False, (f"unknown role {role!r}: browser mode only serves the "
                       f"roles {', '.join(ROLES)}")
    if not endpoint:
        return False, _unknown_message(endpoint, method)
    cls = classification(endpoint, method)
    if cls == ALLOW:
        return True, ""
    if cls == OWNER_ONLY and role == "owner":
        return True, ""
    if cls == OWNER_ONLY:
        return False, (f"{endpoint} ({method}) is owner-only in browser mode; "
                       f"this session is a {role}.")
    if endpoint in CLASSIFICATION:
        return False, _desktop_message(endpoint, method)
    return False, _unknown_message(endpoint, method)


def denial(endpoint: str, method: str, role: str) -> tuple[int, dict]:
    """The HTTP refusal for one call: ``(403, {"error", "message"})``.

    ``error`` is ``desktop_only`` for a desktop-only (or unknown) route and
    ``forbidden`` for a role refusal — the shapes the app's own client expects.
    """
    ok, message = browser_route_allowed(endpoint, method, role)
    if ok:
        return 200, {}
    cls = classification(endpoint, method)
    desktop = cls == DESKTOP_ONLY or str(endpoint or "") not in CLASSIFICATION
    return 403, {"error": "desktop_only" if desktop else "forbidden",
                 "message": message}


def summary() -> dict:
    """Counts per class and the desktop-only list (for reports and audits)."""
    counts = {ALLOW: 0, OWNER_ONLY: 0, DESKTOP_ONLY: 0}
    for cls in CLASSIFICATION.values():
        counts[cls] = counts.get(cls, 0) + 1
    return {
        "endpoints": len(CLASSIFICATION),
        "counts": counts,
        "owner_only": sorted(e for e, c in CLASSIFICATION.items() if c == OWNER_ONLY),
        "desktop_only": sorted(e for e, c in CLASSIFICATION.items() if c == DESKTOP_ONLY),
    }


# --------------------------------------------------------------------------- #
# Installation
# --------------------------------------------------------------------------- #
def install(app):
    """Register the browser-mode guard on *app* (idempotent).

    The guard runs only while ``app.config["BROWSER_MODE"]`` is true; the role
    comes from ``app.config["BROWSER_ROLE"]`` (default ``coder``).  A worker
    that resolves the role per request should call :func:`browser_route_allowed`
    itself instead; this hook is the ready-made form.
    """
    if app.extensions.get("browser_policy"):
        return app
    app.extensions["browser_policy"] = True

    from flask import jsonify, request

    def _browser_mode_guard():
        if not app.config.get("BROWSER_MODE"):
            return None
        role = app.config.get("BROWSER_ROLE") or "coder"
        status, body = denial(request.endpoint, request.method, role)
        if status == 200:
            return None
        resp = jsonify(body)
        resp.status_code = status
        return resp

    # Registered on the hook list directly rather than through the decorator:
    # Flask refuses ``before_request`` once the app has handled a request, and
    # the worker may install the guard after its own health check has run.  The
    # list is the same place the decorator writes to.
    app.before_request_funcs.setdefault(None, []).append(_browser_mode_guard)
    return app
