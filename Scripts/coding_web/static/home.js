/* ============================================================
   CodingStudio — Home tab
   The landing view: what the app is, one box with the projects, the
   numbers of the open project, and the instructions.

   The back end is built in parallel; this file codes against exactly
   this contract:

     GET  /api/projects
          -> { "current": "<the active project root>",
               "projects": [ { "path": "<abs path>", "name": "<folder name>",
                               "scheme": "<scheme name or ''>",
                               "label": "<scheme label or ''>",
                               "papers": 0, "last_opened": "<ISO or ''>",
                               "exists": true } ] }

     GET  /api/project/summary
          -> { "ok": true, "project": { "path": "...", "name": "..." },
               "metrics": { "papers": 0, "texts": 0, "extractions": 0,
                            "codings": 0, "coders": 0, "screened": 0,
                            "datasets": 0, "harmonized": 0,
                            "reproductions": 0 } }

     POST /api/project/recent   { "path": "<abs path>" }
          -> { "ok": true, "projects": [ ...same shape... ] }

     POST /api/project/new      { "name": "My study", "parent": "<folder>",
                                  "template": "blank" | "copy" }
          -> { "ok": true, "root": "<the new project root>",
               "project": { ...same shape as an entry... } }
          -> 400 { "ok": false, "error": "<why>" }

     GET  /static/docs/index.json   -> { "docs": [ { "id": "...",
                                       "title": "...", "summary": "..." } ] }
     GET  /static/docs/<id>.md      -> the markdown of that instruction

   Switching the open project uses POST /api/root (routes_core.py);
   the "choose a folder" dialog that already exists does the same.
   ============================================================ */
(() => {
  "use strict";

  const { $, $$, esc, api, toast, safeGet } = window.CS;
  const icon = window.CS.svg;

  /* ------------------------------------------------------------ the tagline */
  /* One pun per visit, drawn at random, never the same one twice in a row. */
  const TAGLINES = [
    "Because one study is an anecdote in a trench coat.",
    "Pooling evidence, not vibes.",
    "All your effect sizes in a row. Finally.",
    "We put the meta in meticulous.",
    "Where k is never just one.",
    "Small effects, big picture.",
    "Turning PDFs into forest plots, one variable at a time.",
    "Heterogeneity is temporary; coding is forever.",
    "Ten studies walk into a random-effects model.",
    "Your forest plot's favourite forest.",
    "Making sure the evidence actually adds up.",
    "Because science is a group project.",
    "So many papers, so few degrees of freedom.",
    "The funnel plot is mightier than the pen.",
    "Every paper deserves its fifteen minutes of coding.",
    "Pulling studies together so you don't have to pull your hair out.",
    "Double-coded, double-checked, single spreadsheet.",
    "Strictly significant since the first screening.",
  ];

  function pickTagline() {
    let last = -1;
    try { last = Number(localStorage.getItem("cs.home.tagline") ?? -1); } catch { /* ignore */ }
    let i = Math.floor(Math.random() * TAGLINES.length);
    if (TAGLINES.length > 1 && i === last) i = (i + 1) % TAGLINES.length;
    try { localStorage.setItem("cs.home.tagline", String(i)); } catch { /* ignore */ }
    return TAGLINES[i];
  }

  /* ------------------------------------------------------------ metrics */
  /* The numbers of the project, as cards.  An empty project, or no project at
     all, shows a dash rather than a zero: "0" reads as a measured nothing,
     "-" reads as "nothing to measure yet". */
  const METRICS = [
    { key: "papers", label: "Papers",
      sub: (m) => `${num(m.texts)} with text extracted` },
    { key: "extractions", label: "Reference codings",
      sub: () => "the canonical coding" },
    { key: "codings", label: "Coder files",
      sub: (m) => `from ${num(m.coders)} coder${m.coders === 1 ? "" : "s"}` },
    { key: "screened", label: "Abstracts screened",
      sub: () => "with at least one verdict" },
    { key: "harmonized", label: "Harmonized datasets",
      sub: (m) => `of ${num(m.datasets)} with public data` },
    { key: "reproductions", label: "Reproduction reports",
      sub: () => "one per study checked" },
  ];

  function num(n) {
    const v = Number(n);
    if (!Number.isFinite(v)) return "-";
    return v.toLocaleString();
  }

  const state = {
    projects: [],   // the recent projects, as the back end reports them
    current: "",    // the active project root
    available: true, // false when the back end has no /api/projects yet
    loading: false,  // a request is in flight (the buttons wait)
    fetching: false, // the project list is being fetched
    dialogIsMine: false,  // the app opened no dialog, so this view owns it
    metrics: null,   // /api/project/summary's metrics, null until loaded
    metricsFor: "",  // the root the metrics belong to
    metricsProject: false,  // whether that root is a project (has Papers/)
    docs: [],        // the instruction index
    docsError: "",
    docId: "",       // the instruction on screen
    docText: {},     // id -> rendered markdown
  };

  /* ------------------------------------------------------------ helpers */

  /* CS.api throws on a non-2xx answer, but /api/project/new answers a 400
     with a readable reason, so the body has to be read either way. */
  async function postJson(path, body) {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    let data = {};
    try { data = await r.json(); } catch { /* no body */ }
    return { ok: r.ok, status: r.status, data: data || {} };
  }

  /* the folder that holds <path> */
  function parentOf(path) {
    const p = String(path || "").replace(/[\\/]+$/, "");
    const i = Math.max(p.lastIndexOf("\\"), p.lastIndexOf("/"));
    if (i < 0) return "";
    if (i === 2 && p[1] === ":") return p.slice(0, 3);   // C:\Users -> C:\
    return p.slice(0, i);
  }

  /* the last folder of a path */
  function nameOf(path) {
    const p = String(path || "").replace(/[\\/]+$/, "");
    const i = Math.max(p.lastIndexOf("\\"), p.lastIndexOf("/"));
    return i >= 0 ? p.slice(i + 1) : p;
  }

  /* Windows paths are case-insensitive, and the back end may answer with a
     different spelling of the same folder. */
  function samePath(a, b) {
    const norm = (p) => String(p || "").replace(/[\\/]+$/, "").toLowerCase();
    return !!a && !!b && norm(a) === norm(b);
  }

  function formatWhen(iso) {
    const s = String(iso || "").trim();
    if (!s) return "";
    const d = new Date(s);
    if (Number.isNaN(d.getTime())) return s;
    return d.toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  }

  function flash(text) {
    try { sessionStorage.setItem("cs.home.flash", String(text || "")); } catch { /* ignore */ }
  }

  function takeFlash() {
    try {
      const t = sessionStorage.getItem("cs.home.flash");
      if (t) sessionStorage.removeItem("cs.home.flash");
      return t || "";
    } catch {
      return "";
    }
  }

  function setError(text) {
    const el = $("#home-new-error");
    if (!el) return;
    el.textContent = String(text || "");
    el.hidden = !text;
    const ok = $("#home-new-ok");
    if (ok && text) ok.hidden = true;
  }

  function setOk(text) {
    const el = $("#home-new-ok");
    if (!el) return;
    el.textContent = String(text || "");
    el.hidden = !text;
  }

  function setBusy(on) {
    state.loading = !!on;
    const page = $(".home-page");
    if (page) page.classList.toggle("home-busy", !!on);
    ["#home-open-btn", "#home-refresh", "#home-new-toggle", "#home-new-create",
     "#home-new-name", "#home-new-parent", "#home-new-template"].forEach((sel) => {
      const el = $(sel);
      if (el) el.disabled = !!on;
    });
  }

  /* ------------------------------------------------------------ the view */

  function homeView() { return $("#view-home"); }

  function setHomeVisible(on) {
    const v = homeView();
    if (v) v.hidden = !on;
    document.body.classList.toggle("view-home", !!on);
    if (on) ensureStarted();
  }

  /* the app opens the last used view; the Home view only has to appear when
     the view switcher is not there at all. */
  function manualSwitchHome() {
    $$("main.workspace").forEach((m) => { m.hidden = m.id !== "view-home"; });
    const tab = $('.viewtab[data-view="home"]');
    if (tab) {
      tab.classList.add("active");
      tab.setAttribute("aria-selected", "true");
    }
    try { localStorage.setItem("cs.view", "home"); } catch { /* ignore */ }
  }

  function showHome() {
    const sv = window.CS && typeof window.CS.showView === "function" ? window.CS.showView : null;
    if (sv) sv("home");            // hides the other views and marks the tab
    else manualSwitchHome();
    setHomeVisible(true);
  }

  function syncVisibility() {
    const cur = window.CS && typeof window.CS.currentView === "function"
      ? window.CS.currentView() : "";
    setHomeVisible(cur === "home");
  }

  /* ------------------------------------------------------------ rendering */

  function renderTagline() {
    const el = $("#home-tagline");
    if (!el) return;
    if (!el.textContent.trim()) el.textContent = pickTagline();
  }

  function projectRow(p) {
    const path = p.path || "";
    const name = p.name || nameOf(path);
    const label = p.label || p.scheme || "";
    const papers = Number(p.papers || 0);
    const missing = p.exists === false;
    const isCurrent = samePath(path, state.current);
    const tags = [];
    if (isCurrent) tags.push(`<span class="pill accent">open</span>`);
    if (label) tags.push(`<span class="pill">${esc(label)}</span>`);
    tags.push(`<span class="pill">${papers} paper${papers === 1 ? "" : "s"}</span>`);
    if (p.last_opened) tags.push(`<span class="pill">${esc(formatWhen(p.last_opened))}</span>`);
    if (missing) tags.push(`<span class="pill danger">missing</span>`);
    return `<button class="home-proj${isCurrent ? " current" : ""}${missing ? " missing" : ""}"`
      + ` data-path="${esc(path)}"${missing ? " disabled" : ""}`
      + ` title="${esc(path || name)}">`
      + `<span class="home-proj-name">${esc(name)}</span>`
      + `<span class="home-proj-path">${esc(path)}</span>`
      + `<span class="home-proj-tags">${tags.join("")}</span>`
      + `</button>`;
  }

  function renderProjects() {
    const list = $("#home-projects");
    if (!list) return;
    if (!state.available) {
      list.innerHTML = `<p class="home-note">The project list is not available — `
        + `<code>GET /api/projects</code> did not answer. You can still open a folder.</p>`;
      return;
    }
    if (!state.projects.length) {
      list.innerHTML = `<p class="home-note">No project has been opened yet. `
        + `Open a folder, or create a project.</p>`;
      return;
    }
    list.innerHTML = state.projects.map(projectRow).join("");
  }

  function metricCard(m, value, sub) {
    const empty = value === null || value === undefined;
    return `<div class="home-metric${empty ? " is-empty" : ""}">`
      + `<span class="home-metric-label">${esc(m.label)}</span>`
      + `<span class="home-metric-value">${empty ? "-" : num(value)}</span>`
      + `<span class="home-metric-sub">${empty ? "-" : esc(sub)}</span>`
      + `</div>`;
  }

  function renderMetrics() {
    const grid = $("#home-metric-grid");
    const title = $("#home-metrics-title");
    const note = $("#home-metrics-note");
    if (!grid) return;

    const root = state.current || "";
    const usable = state.metricsFor && samePath(state.metricsFor, root) && state.metricsProject;
    const m = usable ? state.metrics : null;
    if (title) title.textContent = root ? "This project" : "No project open";
    if (note) note.textContent = root ? root : "-";

    grid.innerHTML = METRICS.map((spec) => {
      if (!m) return metricCard(spec, null, "");
      return metricCard(spec, m[spec.key], spec.sub ? spec.sub(m) : "");
    }).join("");
  }

  /* ------------------------------------------------------------ instructions */

  function renderDocsNav() {
    const host = $("#home-docs-list");
    if (!host) return;
    if (state.docsError) {
      host.innerHTML = `<p class="home-note">${esc(state.docsError)}</p>`;
      return;
    }
    host.innerHTML = state.docs.map((d) => `
      <button class="home-doc${d.id === state.docId ? " active" : ""}"
              data-doc="${esc(d.id)}" aria-pressed="${d.id === state.docId}">
        <span class="home-doc-title">${esc(d.title)}</span>
        <span class="home-doc-sum">${esc(d.summary || "")}</span>
      </button>`).join("");
  }

  function renderDoc() {
    const host = $("#home-docs-text");
    if (!host) return;
    host.innerHTML = state.docText[state.docId] || "";
  }

  async function openDoc(id) {
    if (!id || id === state.docId) return;
    state.docId = id;
    try { localStorage.setItem("cs.home.doc", id); } catch { /* ignore */ }
    renderDocsNav();
    const host = $("#home-docs-text");
    if (!state.docText[id]) {
      if (host) host.innerHTML = `<p class="home-note">Loading…</p>`;
      try {
        const r = await fetch(`/static/docs/${encodeURIComponent(id)}.md`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        state.docText[id] = window.CS.markdown(await r.text());
      } catch (err) {
        state.docText[id] = `<p class="home-note">This instruction could not be `
          + `loaded (${esc(String(err && err.message || err))}).</p>`;
      }
    }
    renderDoc();
    const body = $(".home-docs-body");
    if (body) body.scrollTop = 0;
  }

  async function loadDocs() {
    if (state.docs.length) return;
    try {
      const r = await fetch("/static/docs/index.json");
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      state.docs = Array.isArray(data.docs) ? data.docs : [];
    } catch (err) {
      state.docsError = "The instructions could not be loaded.";
      renderDocsNav();
      return;
    }
    const wanted = (() => {
      try { return localStorage.getItem("cs.home.doc") || ""; } catch { return ""; }
    })();
    const id = state.docs.some((d) => d.id === wanted)
      ? wanted
      : (state.docs[0] ? state.docs[0].id : "");
    state.docId = "";
    renderDocsNav();
    if (id) openDoc(id);
  }

  /* ------------------------------------------------------------ loading */

  async function loadProjects() {
    if (state.fetching) return;
    state.fetching = true;
    try {
      const data = await api("/api/projects");
      state.projects = Array.isArray(data.projects) ? data.projects : [];
      state.current = data.current || "";
      state.available = true;
    } catch (err) {
      // a back end without /api/projects must not break the view
      state.projects = [];
      state.available = false;
      // the active project is still known, even without the list
      try {
        const info = await api("/api/root");
        state.current = (info && info.root) || "";
      } catch { /* ignore */ }
    } finally {
      state.fetching = false;
    }
    renderProjects();
    renderMetrics();
    loadMetrics();
  }

  /* The numbers come from their own endpoint: the Home view must not wait for
     the paper list, which takes a while on a large project. */
  async function loadMetrics() {
    const root = state.current || "";
    try {
      const data = await api("/api/project/summary");
      state.metrics = data.metrics || null;
      state.metricsFor = (data.project && data.project.path) || root;
      state.metricsProject = !!(data.project && data.project.is_project);
    } catch {
      state.metrics = null;
      state.metricsFor = "";
      state.metricsProject = false;
    }
    renderMetrics();
  }

  /* the parent folder of the new project: the folder the current project
     sits in. */
  async function prefillParent() {
    const input = $("#home-new-parent");
    if (!input) return;
    if (input.value.trim()) return;          // the user typed one already
    let root = state.current || "";
    if (!root) {
      try {
        const info = await api("/api/root");
        root = (info && info.root) || "";
      } catch { /* ignore */ }
    }
    const parent = parentOf(root);
    if (parent) input.value = parent;
  }

  function ensureStarted() {
    loadProjects();
    prefillParent();
    loadDocs();
  }

  /* ------------------------------------------------------------ actions */

  function showNewProject(on) {
    const form = $("#home-new-form");
    const btn = $("#home-new-toggle");
    const open = !!on;
    if (form) form.hidden = !open;
    if (btn) {
      btn.setAttribute("aria-pressed", String(open));
      btn.classList.toggle("active", open);
    }
    if (open) {
      prefillParent();
      setTimeout(() => $("#home-new-name")?.focus({ preventScroll: true }), 0);
    } else {
      setError("");
      setOk("");
    }
  }

  /* The dialog that already exists (the "project folder" button in the top
     bar opens it): the app owns it, so the app's own button is clicked.
     The app wires that button while it is still starting up (the paper list
     of a big project takes a while), so a click that arrives too early finds
     no handler - the very same dialog is then opened here, with the same
     fields, and the folder is set from here too, because the app's own
     "Use this folder" button is not wired yet either. */
  function openProjectDialog() {
    const btn = $("#project-btn");
    const dlg = $("#project-dialog");
    if (!btn || !dlg) {
      toast("The project dialog is not available", "err");
      return;
    }
    if (dlg.open) return;
    btn.click();
    setTimeout(() => openDialogDirectly(dlg), 700);
  }

  /* the fallback: fill the app's dialog the way the app does and show it. */
  async function openDialogDirectly(dlg) {
    if (dlg.open) return;
    let info = {};
    try { info = await api("/api/root"); } catch { /* ignore */ }
    if (dlg.open) return;                       // the app opened it meanwhile
    const input = $("#project-input");
    const status = $("#project-status");
    if (input) input.value = (info && info.root) || state.current || "";
    if (status) {
      status.textContent = info && info.papers != null ? `${info.papers} papers found` : "";
    }
    try {
      dlg.showModal();
    } catch {
      /* the app opened it in the meantime */
      return;
    }
    // the app is still starting: its own "Use this folder" button is not
    // wired, so this view sets the folder itself.
    state.dialogIsMine = true;
  }

  /* open a project by path and start it as the active one */
  async function openProject(path) {
    if (!path) return;
    setBusy(true);
    try {
      const r = await postJson("/api/root", { root: path });
      if (!r.ok) {
        const why = (r.data && r.data.error) || "Could not open that folder.";
        toast("Could not open that project", "err", why);
        setError(why);
        return;
      }
      // the back end keeps the recent list
      try { await postJson("/api/project/recent", { path }); } catch { /* not fatal */ }
      const root = r.data.root || path;
      flash(`Project opened — ${root}`);
      // the paper list belongs to the project, so the app is reloaded
      location.reload();
    } catch (err) {
      const why = String((err && err.message) || err);
      toast("Could not open that project", "err", why);
      setError(why);
    } finally {
      setBusy(false);
    }
  }

  async function createProject(ev) {
    if (ev) ev.preventDefault();
    const nameEl = $("#home-new-name");
    const parentEl = $("#home-new-parent");
    const tmplEl = $("#home-new-template");
    const name = nameEl ? nameEl.value.trim() : "";
    const parent = parentEl ? parentEl.value.trim() : "";
    const template = tmplEl ? tmplEl.value : "blank";
    setError("");
    setOk("");
    if (!name) { setError("Give the project a name."); return; }
    if (!parent) { setError("Give the folder the new project is created in."); return; }
    setBusy(true);
    try {
      const r = await postJson("/api/project/new", { name, parent, template });
      if (!r.ok) {
        setError((r.data && r.data.error) || "The project could not be created.");
        return;
      }
      const root = r.data.root || "";
      setOk(`Created ${root}`);
      // open it
      const o = await postJson("/api/root", { root });
      if (!o.ok) {
        setError((o.data && o.data.error) || "The project was created but could not be opened.");
        return;
      }
      try { await postJson("/api/project/recent", { path: root }); } catch { /* not fatal */ }
      flash(`Project created and opened — ${root}`);
      location.reload();
    } catch (err) {
      setError(String((err && err.message) || err));
    } finally {
      setBusy(false);
    }
  }

  /* the project dialog changes the project too: it is recorded here, so the
     recent list follows the app. */
  async function noteDialogSwitch() {
    let root = "";
    try {
      const info = await api("/api/root");
      root = (info && info.root) || "";
    } catch { return; }
    if (!root || samePath(root, state.current)) return;
    try {
      const r = await postJson("/api/project/recent", { path: root });
      if (r.ok && Array.isArray(r.data.projects)) {
        state.projects = r.data.projects;
        state.current = r.data.current || root;
      }
    } catch { /* not fatal */ }
    renderProjects();
    renderMetrics();
    loadMetrics();
    prefillParent();
  }

  /* ------------------------------------------------------------ wiring */

  function wireEvents() {
    $("#home-open-btn")?.addEventListener("click", openProjectDialog);
    $("#home-refresh")?.addEventListener("click", () => {
      state.fetching = false;
      loadProjects();
      loadMetrics();
    });
    $("#home-new-toggle")?.addEventListener("click", () => {
      const form = $("#home-new-form");
      showNewProject(form ? form.hidden : true);
    });
    $("#home-new-cancel")?.addEventListener("click", () => showNewProject(false));
    $("#home-new-form")?.addEventListener("submit", createProject);

    const list = $("#home-projects");
    if (list) {
      list.addEventListener("click", (e) => {
        const row = e.target.closest(".home-proj");
        if (!row || row.disabled) return;
        openProject(row.getAttribute("data-path") || "");
      });
    }

    const docs = $("#home-docs-list");
    if (docs) {
      docs.addEventListener("click", (e) => {
        const btn = e.target.closest(".home-doc");
        if (!btn) return;
        openDoc(btn.dataset.doc || "");
      });
    }

    // the project dialog is the app's own: it switches the project too
    $("#project-dialog")?.addEventListener("close", () => {
      state.dialogIsMine = false;
      noteDialogSwitch();
    });

    // When the app was still starting and the dialog was opened from here,
    // the app's own "Use this folder" button is not wired yet: the folder is
    // set from here.
    $("#project-save")?.addEventListener("click", () => {
      if (!state.dialogIsMine) return;        // the app's own handler does it
      state.dialogIsMine = false;
      const input = $("#project-input");
      const path = input ? input.value.trim() : "";
      if (!path) return;
      $("#project-dialog")?.close();
      openProject(path);
    });

    // the view switcher of the app knows the other views; the Home tab falls
    // back to its own handler when there is none.
    const tab = $('.viewtab[data-view="home"]');
    if (tab) {
      tab.addEventListener("click", () => {
        const v = homeView();
        if (v && !v.hidden) return;      // the app's own handler did it
        showHome();
      });
    }

    document.addEventListener("cs:view", (e) => {
      const v = e && e.detail ? String(e.detail) : "";
      setHomeVisible(v === "home");
    });
  }

  /* The app opens the last used view, and that must not change.  Only a
     start without a project lands on the Home view. */
  async function maybeLandOnHome() {
    if (safeGet("cs.view")) return;
    let info = null;
    try { info = await api("/api/root"); } catch { return; }
    if (!info) return;
    if (info.has_papers || Number(info.papers || 0) > 0) return;
    showHome();
  }

  function init() {
    renderTagline();
    renderProjects();
    renderMetrics();
    wireEvents();
    // the view switcher may have run before this file was ready
    syncVisibility();
    showFlash();
    maybeLandOnHome();
  }

  function showFlash() {
    const el = $("#home-flash");
    if (!el) return;
    const text = takeFlash();
    if (!text) return;
    el.textContent = text;
    el.hidden = false;
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
