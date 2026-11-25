"""Core routes: root/config, the static app, papers, extractions, codings, notes, save and the PDF/text viewer."""

from flask import Blueprint, Response, abort, jsonify, request, send_file, send_from_directory
import json
import os
import re
from datetime import datetime
from studio_lib import APP_VERSION, PAGE_SCALE, _MISSING, _PAGE_NUM, _pdf_text, atomic_replace, invalidate_dir, legacy_schema_document, load_extraction, pdf_page_size, pdf_page_spans, pdf_pages, render_page, safe_token, search_pdf, summarise, supplements, validate_records, write_json_atomic
from studio_state import FROZEN, S, STATIC, config_path, configure, has_data, log, paper_keys, refresh_keys, save_config

bp = Blueprint("core", __name__)


def _invalidate_dependants():
    """Tell the other tabs' caches that a coder file changed (best effort)."""
    try:
        from agreement_lib import _invalidate_agreement
        _invalidate_agreement()
    except Exception as exc:  # noqa: BLE001
        log(f"agreement cache invalidation failed: {exc!r}")


def _coding_path(key, coder):
    return os.path.join(S.CODINGS, f"{key} - {safe_token(coder)}.json")


def _mtime_token(path):
    """A file's mtime as an exact string token (ns precision does not fit a JS number)."""
    try:
        return str(os.stat(path).st_mtime_ns)
    except OSError:
        return None


@bp.get("/api/root")
def api_root():
    return jsonify({
        "root": S.ROOT,
        "has_papers": os.path.isdir(S.PAPERS),
        "papers": len(S.KEYS),
        "frozen": FROZEN,
        "version": APP_VERSION,
        "config": config_path(),
        "can_pick": bool(_windows()),
    })

@bp.post("/api/root")
def api_set_root():
    payload = request.get_json(force=True, silent=True) or {}
    path = (payload.get("root") or "").strip().strip('"')
    if not path or not os.path.isdir(path):
        return jsonify({"ok": False, "error": "folder does not exist"}), 400
    # configure() creates Codings/, temp/ and Screening/ in the chosen folder, so
    # a mistyped path (C:\, Desktop, ...) would litter it; insist on a project.
    if not has_data(path) and not payload.get("force"):
        return jsonify({"ok": False, "error": "no Papers/ or Text/ folder in that folder — "
                                              "pick the project root"}), 400
    configure(path)
    save_config({"root": S.ROOT})
    try:
        # a local folder chosen by hand: the next start opens it, not the last
        # online project
        from studio_state import set_last_project_local
        set_last_project_local()
    except Exception:  # noqa: BLE001
        pass
    refresh_keys()
    # The time-based caches of the corpus-wide views are not keyed by project:
    # without this the summary and agreement tabs answered from the previous
    # project for up to 20 s (and the response cache then kept that answer).
    try:
        from studio_lib import _CODING_INDEX
        _CODING_INDEX["data"] = None
        from agreement_lib import _invalidate_agreement
        _invalidate_agreement()
    except Exception:  # noqa: BLE001 - a re-root must never fail over a cache
        pass
    log(f"root changed -> {S.ROOT} ({len(S.KEYS)} papers)")
    return jsonify({"ok": True, "root": S.ROOT, "papers": len(S.KEYS),
                    "has_papers": os.path.isdir(S.PAPERS)})

def _windows():
    try:
        import webview

        return list(getattr(webview, "windows", []) or [])
    except Exception:
        return []

@bp.post("/api/pick-root")
def api_pick_root():
    """Open the native folder chooser (desktop build only)."""
    windows = _windows()
    if not windows:
        return jsonify({"ok": False, "error": "no desktop window — type the folder path instead"}), 400
    try:
        import webview

        chosen = windows[0].create_file_dialog(webview.FOLDER_DIALOG) if hasattr(windows[0], "create_file_dialog") else None
    except BaseException:  # noqa: BLE001
        import traceback

        log("folder dialog failed:\n" + traceback.format_exc())
        return jsonify({"ok": False, "error": "folder chooser unavailable — type the path instead"}), 400
    if not chosen:
        return jsonify({"ok": False, "error": "cancelled"})
    path = chosen[0] if isinstance(chosen, (list, tuple)) else chosen
    return jsonify({"ok": True, "path": str(path)})

@bp.get("/")
def index():
    resp = send_from_directory(STATIC, "index.html")
    resp.headers["Cache-Control"] = "no-store"
    return resp

@bp.get("/static/<path:path>")
def static_files(path):
    resp = send_from_directory(STATIC, path)
    resp.headers["Cache-Control"] = "no-store"
    return resp

_SCHEMA_RESP = {"mtime": None, "body": None}


@bp.get("/api/schema")
def api_schema():
    """The coding schema in the legacy v3.1 shape (deprecated).

    The scheme itself is served by ``/api/scheme`` (the ``scheme`` blueprint);
    this route stays because the agent routes and the manuscript report still
    read the v3.1 shape.  A project that carries its own scheme is rendered
    back into that shape, so the response shape never changes.
    """
    doc = legacy_schema_document()
    resp = jsonify(doc)
    resp.headers["Deprecation"] = "true"
    resp.headers["X-Schema-Deprecated"] = (
        "use /api/scheme for the coding scheme; /api/schema renders it in the "
        "legacy v3.1 shape")
    return resp

@bp.get("/api/papers/progress")
def api_papers_progress():
    """Progress of the (first, uncached) papers index build, polled by the
    start-up screen while /api/papers is still running."""
    from studio_cache import papers_progress
    return jsonify(papers_progress())


@bp.get("/api/papers")
def api_papers():
    from fulltext_lib import load_agreed
    from studio_cache import papers_index
    # In an online project a paper can enter the manifest mid-session (a sync
    # tick, a done mark that reveals another coder's file): the key list is
    # refreshed here so /api/papers and the routes that check S.KEYS agree.
    if S.online:
        refresh_keys()
    agreed = load_agreed()
    # The summaries are cached against the files: a study is re-read only when
    # one of its own files changed (on disk or in the app).
    papers = []
    for s in papers_index(paper_keys(), summarise):
        k = s["key"]
        ex = agreed.get(k)
        s["fulltext_excluded"] = bool(ex)
        s["fulltext_criterion"] = (ex or {}).get("criterion_label") or ""
        s["fulltext_coders"] = (ex or {}).get("coders") or []
        # Fully-within (single-arm, no-control) paper: derived from the coded
        # record by studio_lib.summarise (explicit ``Fully_Within`` field, with a
        # documented n_arms/contrast-type fallback).  Excluded from pooled
        # analysis by default; the front end marks it.
        s["fully_within"] = bool(s.get("fully_within"))
        papers.append(s)
    return jsonify({"papers": papers})

@bp.get("/api/extraction/<path:key>")
def api_extraction(key):
    if key not in S.KEYS:
        abort(404)
    records = load_extraction(key)
    if records is None:
        return jsonify({"key": key, "records": None, "exists": False})
    return jsonify({"key": key, "records": records, "exists": True})

@bp.get("/api/coding/<path:key>/<path:coder>")
def api_coding(key, coder):
    if key not in S.KEYS:
        abort(404)
    path = os.path.join(S.CODINGS, f"{key} - {safe_token(coder)}.json")
    # an on-demand AI route coding is fetched here (one explicit request for one
    # study, not a whole-corpus loop); a human coder's file is already local
    path = S.ensure_local(path)
    if not os.path.exists(path):
        abort(404)
    mtime = _mtime_token(path)
    try:
        with open(path, encoding="utf-8") as fh:
            records = json.load(fh)
    except (OSError, ValueError) as exc:
        log(f"coding file unreadable: {os.path.basename(path)}: {exc!r}")
        return jsonify({"ok": False, "key": key, "coder": coder,
                        "error": f"{os.path.basename(path)} cannot be read: {exc}"}), 500
    return jsonify({"key": key, "coder": coder, "records": records, "mtime": mtime})

@bp.get("/api/notes/<path:key>")
def api_notes(key):
    if key not in S.KEYS:
        abort(404)
    path = os.path.join(S.EXTRACTION, key + ".md")
    if not os.path.exists(path):
        return jsonify({"key": key, "notes": None})
    with open(path, encoding="utf-8", errors="replace") as fh:
        return jsonify({"key": key, "notes": fh.read()})

def _join_lines(text):
    """One line break = ignore, so wrapped lines become a single paragraph."""
    text = text.replace("\u00ad ", "").replace("\u00ad\n", "-\n")
    text = text.replace("\u00ad", "-").replace("\u2010", "-").replace("\u2011", "-")
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    out = ""
    for ln in lines:
        if not ln:
            continue
        if not out:
            out = ln
        elif out.endswith("-") and re.match(r"^[a-zà-ÿ]", ln):
            out = out[:-1] + ln
        else:
            out = out + " " + ln
    out = re.sub(r"([A-Za-z])-\s+([a-zà-ÿ])", r"\1\2", out)
    return re.sub(r"[ \t]{2,}", " ", out).strip()

def clean_text(raw):
    """Fallback for pre-extracted ``.txt``: two or more line breaks start a new
    paragraph; page-number-only lines are dropped."""
    if not raw:
        return raw
    raw = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    raw = re.sub(r"[ \t]+\n", "\n", raw)
    paras = []
    for chunk in re.split(r"\n{2,}", raw):
        lines = [ln for ln in chunk.split("\n") if not _PAGE_NUM.match(ln)]
        text = _join_lines("\n".join(lines))
        if text:
            paras.append(text)
    return "\n\n".join(paras)

_HEADER_RE = re.compile(
    r"^(downloaded from|this article|copyright|all rights reserved|https?://|doi:|cite this article|"
    r"see discussions,? stats|received:|accepted:|published|vol\.?\s*\d|volume\s*\d|issue\s*\d|"
    r"www\.|page\s+\d+|preprint|supplementary|creative commons|licen[cs]e)",
    re.IGNORECASE,
)

_BOILER_RE = re.compile(
    r"(the author\(s\)|under exclusive licen|all rights reserved|distributed under the terms|"
    r"creative commons|downloaded from|this article is|terms and conditions|springer nature|"
    r"elsevier|taylor\s*&\s*francis|wiley periodicals|sage publications|oxford university press|"
    r"informa uk limited|licen[cs]ed under)",
    re.IGNORECASE,
)

_MASTHEAD = {
    "originalarticle", "originalresearch", "reviewarticle", "researcharticle",
    "systematicreview", "briefreport", "empiricalarticle", "researchpaper",
    "fulllengtharticle", "originalpaper", "research",
}

def _norm_line(t):
    return re.sub(r"\s+", " ", t).strip().lower()

def extract_pdf_text(path):
    """Paragraph-aware extraction straight from the PDF.

    Lines are grouped into paragraphs by vertical gaps within a detected
    column; running heads/footers (lines repeating across pages) and common
    publisher boilerplate are dropped.
    """
    stamp = (path, os.path.getmtime(path))
    cached = _pdf_text.get(stamp, _MISSING)
    if cached is not _MISSING:
        return cached
    text = ""
    try:
        import pymupdf

        with pymupdf.open(path) as doc:
            pages = []
            for page in doc:
                width = page.rect.width
                lines = []
                for block in page.get_text("dict").get("blocks", []):
                    if block.get("type") != 0:
                        continue
                    for line in block.get("lines", []):
                        raw = "".join(s.get("text", "") for s in (line.get("spans") or []))
                        if not raw.strip():
                            continue
                        x0, y0, x1, y1 = line["bbox"]
                        lines.append({"t": raw, "x0": x0, "x1": x1, "y0": y0, "y1": y1, "mid": width / 2})
                pages.append(lines)

            n_pages = max(len(pages), 1)
            counts = {}
            for lines in pages:
                for l in lines:
                    k = _norm_line(l["t"])
                    if len(k) >= 6 and not _PAGE_NUM.match(l["t"]):
                        counts[k] = counts.get(k, 0) + 1
            repeat_min = max(2, int(n_pages * 0.4))
            repeated = {k for k, c in counts.items() if c >= repeat_min}

            paras = []
            for lines in pages:
                keep = []
                for l in lines:
                    k = _norm_line(l["t"])
                    if _PAGE_NUM.match(l["t"]):
                        continue
                    if re.sub(r"[^a-z]", "", k) in _MASTHEAD:
                        continue
                    if len(l["t"]) < 220 and _BOILER_RE.search(l["t"]):
                        continue
                    if k in repeated or _HEADER_RE.search(l["t"].strip()):
                        continue
                    keep.append(l)
                if not keep:
                    continue
                col = lambda l: 0 if l["x0"] < l["mid"] else 1
                keep.sort(key=lambda l: (col(l), round(l["y0"], 1), l["x0"]))
                buf, prev = [], None
                for l in keep:
                    if buf and prev is not None:
                        height = max(prev["y1"] - prev["y0"], 1.0)
                        if col(l) != col(prev) or (l["y0"] - prev["y1"]) > height * 1.25:
                            paras.append(buf)
                            buf = []
                    buf.append(l["t"].strip())
                    prev = l
                if buf:
                    paras.append(buf)

            out = []
            for buf in paras:
                para = _join_lines("\n".join(buf))
                if para:
                    out.append(para)
            text = "\n\n".join(out)
    except Exception as exc:  # noqa: BLE001 - fall back to Text/<key>.txt
        log(f"extract_pdf_text failed for {os.path.basename(path)}: {exc!r}")
        text = ""
    _pdf_text[stamp] = text
    return text

@bp.get("/api/text/<path:key>")
def api_text(key):
    if key not in S.KEYS:
        abort(404)
    pdf = resolve_pdf(key)
    if pdf:
        text = extract_pdf_text(pdf)
        if text:
            return jsonify({"key": key, "text": text, "source": "pdf"})
    path = S.ensure_local(os.path.join(S.TEXT, key + ".txt"))
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            return jsonify({"key": key, "text": clean_text(fh.read()), "source": "txt"})
    return jsonify({"key": key, "text": None, "source": None})

def _supp_path(sp):
    folder = S.SUPPLEMENTS if sp.get("folder") == "Supplements" else S.PAPERS
    return os.path.join(folder, sp["file"])

def resolve_pdf(key, supp=None):
    if key not in S.KEYS:
        return None
    supps = supplements(key)
    if supp:
        s = str(supp)
        for sp in supps:
            if sp.get("name") == s and sp.get("kind") == "pdf":
                return _supp_path(sp)
        try:
            want = int(s)
        except ValueError:
            want = None
        for sp in supps:
            if sp.get("kind") == "pdf" and sp.get("n") == want:
                return _supp_path(sp)
        return None
    path = os.path.join(S.PAPERS, key + ".pdf")
    # An online project holds Papers/ in the cache tier: fetch the PDF the
    # moment a route needs it (audit §2, R9-R14; R6).
    path = S.ensure_local(path)
    return path if os.path.exists(path) else None

@bp.get("/supplement/<path:key>/<path:name>")
def supplement_file(key, name):
    """Serve a supplementary file (PDF inline, anything else as a download)."""
    if key not in S.KEYS:
        abort(404)
    for sp in supplements(key):
        if sp["file"] == name:
            path = S.ensure_local(_supp_path(sp))
            if os.path.exists(path):
                is_pdf = sp.get("kind") == "pdf"
                return send_file(path,
                                 mimetype="application/pdf" if is_pdf
                                 else "application/octet-stream",
                                 as_attachment=not is_pdf, download_name=sp["file"])
    abort(404)

@bp.get("/paper/<path:key>")
def paper(key):
    path = resolve_pdf(key, request.args.get("supp"))
    if not path:
        abort(404)
    return send_file(path, mimetype="application/pdf", conditional=True)

@bp.get("/api/pdfinfo/<path:key>")
def api_pdfinfo(key):
    if key not in S.KEYS:
        abort(404)
    path = resolve_pdf(key, request.args.get("supp"))
    return jsonify({"key": key, "pages": pdf_pages(path), "size": pdf_page_size(path),
                    "supplements": supplements(key)})

@bp.get("/api/pdfsearch/<path:key>")
def api_pdfsearch(key):
    """Highlight anchors for a text search in the viewer PDF (fractions of page)."""
    if key not in S.KEYS:
        abort(404)
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"key": key, "query": q, "count": 0, "hits": []})
    path = resolve_pdf(key, request.args.get("supp"))
    if not path:
        return jsonify({"key": key, "query": q, "count": 0, "hits": [],
                        "error": "no pdf"})
    res = search_pdf(path, q)
    if res is None:
        return jsonify({"key": key, "query": q, "count": 0, "hits": [],
                        "error": "search unavailable (PyMuPDF missing?)"})
    res["key"] = key
    return jsonify(res)

@bp.get("/api/pdftext/<path:key>/<int:page>.json")
def api_pdftext(key, page):
    """Selectable text spans for one page, overlaid on the rendered image."""
    if key not in S.KEYS:
        abort(404)
    path = resolve_pdf(key, request.args.get("supp"))
    data = pdf_page_spans(path, page)
    if data is None:
        return jsonify({"key": key, "page": page, "spans": []})
    data["key"] = key
    return jsonify(data)

@bp.get("/pageimg/<path:key>/<int:page>.png")
def page_image(key, page):
    """One PDF page rendered to PNG so the viewer can be themed (dark mode)."""
    if key not in S.KEYS:
        abort(404)
    path = resolve_pdf(key, request.args.get("supp"))
    if not path:
        abort(404)
    try:
        scale = float(request.args.get("scale", PAGE_SCALE))
    except (TypeError, ValueError):
        scale = PAGE_SCALE
    scale = max(1.0, min(3.0, scale))
    out = render_page(path, page, scale)
    if not out:
        abort(404)
    return send_file(out, mimetype="image/png", conditional=True, etag=True)

def _normalise_per_intervention(records):
    """Keep the per-intervention model intact on the way to disk.

    ``Interventions`` is not a schema field: it and the DV ``Intervention_ID``
    binding must survive a save/load cycle.  Records without the key are left
    without it, so a pre-existing record behaves exactly as before.  Only the
    new keys are touched; every other field (including unknown ones) is written
    back verbatim.
    """
    for rec in records:
        if not isinstance(rec, dict):
            continue
        ivs = rec.get("Interventions")
        if isinstance(ivs, list):
            for it in ivs:
                if not isinstance(it, dict):
                    continue
                it.setdefault("label", "")
                if not isinstance(it.get("overrides"), dict):
                    it["overrides"] = {}
        for dv in rec.get("DV_Metrics") or []:
            if isinstance(dv, dict) and "Intervention_ID" in dv and not dv["Intervention_ID"]:
                dv.pop("Intervention_ID", None)  # blank = shared by all interventions
    return records


@bp.post("/api/save")
def api_save():
    payload = request.get_json(force=True, silent=True) or {}
    key = payload.get("key")
    coder = payload.get("coder")
    records = payload.get("records")
    if key not in S.KEYS:
        return jsonify({"ok": False, "error": "unknown paper key"}), 400
    if not coder or not str(coder).strip():
        return jsonify({"ok": False, "error": "coder name is required"}), 400
    # The original (reference) coding is Extraction/<Key>.json and is read-only
    # from the app; a coder file named after it would shadow it in every
    # comparison, so the reserved names are refused here and not only in the UI.
    if str(coder).strip().lower() in ("og", "original (og)", "original"):
        return jsonify({"ok": False, "error": "the original coding is read-only; "
                        "save under your own coder name"}), 400
    # "Consensus" is the settlement route's name for Extraction/<Key>.json and a
    # leading underscore / "harmonizer" marks store and override files: none of
    # them may become a coder, or the agreement tab would score it as a rater.
    _c = str(coder).strip().lower()
    if _c == "consensus" or _c.startswith("_") or _c.startswith("harmonizer"):
        return jsonify({"ok": False, "error": "this coder name is reserved; "
                        "save under your own coder name"}), 400
    if not isinstance(records, list) or not records:
        return jsonify({"ok": False, "error": "no coding records to save"}), 400

    if str(coder).strip().lower() in ("og", "original (og)"):
        return jsonify({"ok": False, "error": "the original extraction is read-only — "
                                              "save under your own coder name"}), 400
    if not all(isinstance(r, dict) for r in records):
        return jsonify({"ok": False, "error": "every coding record must be an object"}), 400

    records = _normalise_per_intervention(records)
    target = _coding_path(key, coder)
    existed = os.path.exists(target)
    # Optimistic concurrency: the client sends the mtime of the file it loaded
    # (null = it loaded no file of this coder).  If the file changed since -
    # the Summary tab, a second window or a script wrote it - refuse instead of
    # silently overwriting that work; the client asks and may resend with force.
    if "expected_mtime" in payload and not payload.get("force"):
        expected = payload.get("expected_mtime")
        current = _mtime_token(target) if existed else None
        if (expected or None) != current:
            if payload.get("unload"):
                # Closing the window: nobody can answer a question now, so keep
                # the edits in a recovery copy instead of losing or forcing them.
                rescue = _write_recovery(key, coder, records)
                log(f"save conflict on unload for {os.path.basename(target)}; edits kept in {rescue}")
            return jsonify({"ok": False, "conflict": True,
                            "error": f"{os.path.basename(target)} was changed outside this "
                                     "window since you opened it",
                            "mtime": current}), 409
    # M3 (audit): warn (never block) on codings that would fail B_validate.  The
    # check uses the shared completeness rule (contract.dv_has_effect).
    try:
        warnings = validate_records(records)
    except Exception as exc:  # never fail a save because the validator broke
        log(f"validate_records failed for {key}: {exc!r}")
        warnings = [f"validator unavailable: {exc}"]
    try:
        write_json_atomic(target, records)
    except Exception as exc:  # noqa: BLE001 - report it to the coder, never a bare 500
        log(f"save failed for {os.path.basename(target)}: {exc!r}")
        return jsonify({"ok": False, "error": f"could not write {os.path.basename(target)}: {exc}"}), 500
    _invalidate_dependants()
    return jsonify(
        {
            "ok": True,
            "path": os.path.relpath(target, S.ROOT).replace("\\", "/"),
            "overwrote": existed,
            "records": len(records),
            "dv_rows": sum(len(r.get("DV_Metrics") or []) for r in records),
            "warnings": warnings,
            "mtime": _mtime_token(target),
            "file": os.path.basename(target),
            "coder": os.path.basename(target)[len(key) + 3:-len(".json")],
        }
    )


def _write_recovery(key, coder, records):
    folder = os.path.join(S.TEMP, "codingstudio_recovery")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(folder, f"{key} - {safe_token(coder)} - {stamp}.json")
    try:
        write_json_atomic(path, records)
    except Exception as exc:  # noqa: BLE001
        log(f"recovery copy failed for {key}: {exc!r}")
        return None
    return os.path.relpath(path, S.ROOT)


@bp.post("/api/coding/delete")
def api_coding_delete():
    """Archive one coder's coding of one study (never a hard delete).

    Unwanted codings are moved to ``archive/codings_deleted/<stamp>/`` so a
    mistake is always recoverable; the canonical Extraction is untouched.
    """
    payload = request.get_json(force=True, silent=True) or {}
    key = payload.get("key")
    coder = (payload.get("coder") or "").strip()
    if key not in S.KEYS:
        return jsonify({"ok": False, "error": "unknown paper key"}), 400
    if not coder:
        return jsonify({"ok": False, "error": "coder name is required"}), 400
    target = os.path.join(S.CODINGS, f"{key} - {safe_token(coder)}.json")
    target = S.ensure_local(target)      # an on-demand AI route coding is fetched first
    if not os.path.exists(target):
        return jsonify({"ok": False, "error": "no coding of this study by that coder"}), 404
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest_dir = os.path.join(S.ROOT, "archive", "codings_deleted", stamp)
    try:
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, os.path.basename(target))
        atomic_replace(target, dest)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500
    try:
        from agreement_lib import _invalidate_agreement
        _invalidate_agreement()
    except Exception:
        pass
    return jsonify({"ok": True, "key": key, "coder": coder,
                    "archived": os.path.relpath(dest, S.ROOT).replace("\\", "/")})
