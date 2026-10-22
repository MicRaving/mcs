/* ============================================================
   CodingStudio — Settings

   Two sections for now: the project's AGENTS.md (the documentation an
   agent reads when it opens the folder) and the application's updates.
   The page is a list plus one panel so later sections need no redesign.

   Contract with the back end (routes_settings.py):

     GET  /api/settings/project   -> { root, path, exists, text, template }
     POST /api/settings/project   { text }        -> { ok, path, bytes }
     POST /api/settings/project/reset              -> { ok, path, text }

     GET  /api/update/state       -> { current, latest, available, ... }
     POST /api/update/check       -> the same state, freshly checked
     POST /api/update/download    -> { ok, started }
     POST /api/update/apply       -> { ok, restarting, message }
   ============================================================ */
(() => {
  "use strict";

  const { $, $$, api, toast } = window.CS;
  const confirmDialog = (...args) => window.CS.confirmDialog(...args);

  const state = {
    section: "project",   // project | updates
    doc: "",              // the AGENTS.md on screen
    savedDoc: "",         // the last saved copy, for the dirty marker
    update: null,         // the last /api/update/state answer
    poll: 0,              // the download poll timer
    busy: false,
  };

  /* ------------------------------------------------------------ sections */

  function showSection(id) {
    state.section = id === "updates" ? "updates" : "project";
    $$("#st-nav .st-nav-item").forEach((b) => {
      const on = b.dataset.section === state.section;
      b.classList.toggle("active", on);
      b.setAttribute("aria-pressed", String(on));
    });
    const project = $("#st-panel-project");
    const updates = $("#st-panel-updates");
    if (project) project.hidden = state.section !== "project";
    if (updates) updates.hidden = state.section !== "updates";
    if (state.section === "updates") loadUpdateState();
  }

  /* ------------------------------------------------------------ the AGENTS.md */

  function setDocStatus(text, kind = "") {
    const el = $("#st-doc-status");
    if (!el) return;
    el.textContent = text || "";
    el.className = "st-status" + (kind ? " " + kind : "");
  }

  function renderDocMeta(info) {
    const path = $("#st-doc-path");
    if (!path) return;
    if (!info || !info.root) {
      path.textContent = "No project is open.";
      return;
    }
    path.textContent = (info.exists ? "" : "(not created yet) ") + info.path;
  }

  async function loadProjectDoc() {
    let info = null;
    try {
      info = await api("/api/settings/project");
    } catch (err) {
      setDocStatus("The file could not be read: " + (err.message || err), "err");
      return;
    }
    state.doc = info.text || "";
    state.savedDoc = state.doc;
    const area = $("#st-doc");
    if (area) {
      area.value = state.doc;
      area.disabled = !info.root;
    }
    renderDocMeta(info);
    setDocStatus(info.exists ? "" : "This project has no AGENTS.md yet — Save creates it.");
  }

  async function saveDoc() {
    const area = $("#st-doc");
    if (!area || state.busy) return;
    state.busy = true;
    setDocStatus("Saving…");
    try {
      const r = await fetch("/api/settings/project", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: area.value }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok || !data.ok) throw new Error(data.error || `HTTP ${r.status}`);
      state.savedDoc = area.value;
      state.doc = area.value;
      renderDocMeta({ root: true, path: data.path, exists: true });
      setDocStatus(`Saved (${data.bytes} bytes)`, "ok");
      toast("Project documentation saved", "ok", "AGENTS.md");
    } catch (err) {
      setDocStatus("Not saved: " + (err.message || err), "err");
      toast("The documentation could not be saved", "err", String(err.message || err));
    } finally {
      state.busy = false;
    }
  }

  async function resetDoc() {
    if (state.busy) return;
    const ok = await confirmDialog(
      "Restore the template?",
      "The current AGENTS.md is replaced by a fresh template for this project. "
      + "Anything you wrote in it is lost.", "Restore template");
    if (!ok) return;
    state.busy = true;
    try {
      const r = await fetch("/api/settings/project/reset", { method: "POST" });
      const data = await r.json().catch(() => ({}));
      if (!r.ok || !data.ok) throw new Error(data.error || `HTTP ${r.status}`);
      state.doc = data.text || "";
      state.savedDoc = state.doc;
      const area = $("#st-doc");
      if (area) area.value = state.doc;
      setDocStatus("Template restored", "ok");
    } catch (err) {
      setDocStatus("Not restored: " + (err.message || err), "err");
    } finally {
      state.busy = false;
    }
  }

  /* ------------------------------------------------------------ updates */

  function fmtBytes(n) {
    const v = Number(n) || 0;
    if (v >= 1024 * 1024) return (v / (1024 * 1024)).toFixed(1) + " MB";
    if (v >= 1024) return Math.round(v / 1024) + " KB";
    return v + " B";
  }

  function setUpdateStatus(text, kind = "") {
    const el = $("#st-update-status");
    if (!el) return;
    el.textContent = text || "";
    el.className = "st-status" + (kind ? " " + kind : "");
  }

  function renderUpdate(s) {
    if (!s) return;
    state.update = s;
    const current = $("#st-current");
    const latest = $("#st-latest");
    if (current) current.textContent = s.current || "-";
    if (latest) {
      latest.textContent = s.available ? s.latest
        : s.error ? "-"
        : (s.latest || "up to date");
    }

    // the note explains why the app cannot install an update itself; it only
    // matters when there is one to install
    const note = $("#st-update-note");
    if (note) {
      const text = s.available ? (s.note || "") : "";
      note.textContent = text;
      note.hidden = !text;
    }

    const install = $("#st-install");
    if (install) {
      const dl = (s.download || {});
      const ready = dl.status === "ready";
      install.hidden = !(s.available && s.asset && s.can_self_update);
      install.disabled = !s.can_self_update || dl.status === "downloading";
      install.textContent = ready ? "Install and restart"
        : dl.status === "downloading" ? "Downloading…"
        : "Download and install";
    }

    const page = $("#st-release-page");
    if (page) {
      page.hidden = !s.available;
      if (s.page_url) page.href = s.page_url;
    }

    const notes = $("#st-notes");
    if (notes) {
      const text = s.available ? (s.notes || "") : "";
      notes.hidden = !text;
      if (text) notes.innerHTML = window.CS.markdown(text);
    }

    const bar = $("#st-progress");
    const fill = $("#st-progress-bar");
    const dl = (s.download || {});
    if (bar && fill) {
      const show = dl.status === "downloading" || dl.status === "ready";
      bar.hidden = !show;
      const total = Number(dl.total) || 0;
      const pct = dl.status === "ready" ? 100
        : total > 0 ? Math.min(100, Math.round((Number(dl.received) || 0) / total * 100)) : 0;
      fill.style.width = pct + "%";
    }

    if (dl.status === "downloading") {
      setUpdateStatus(`${fmtBytes(dl.received)} of ${fmtBytes(dl.total)}`);
    } else if (dl.status === "ready") {
      setUpdateStatus("Downloaded — install it to restart into the new version", "ok");
    } else if (dl.status === "error") {
      setUpdateStatus("The download failed: " + (dl.error || ""), "err");
    } else if (s.checking) {
      setUpdateStatus("Checking…");
    } else if (s.available) {
      setUpdateStatus(`Version ${s.latest} is available`, "ok");
    } else if (!s.error) {
      setUpdateStatus("You are on the latest version");
    } else {
      setUpdateStatus(s.error);
    }
  }

  async function loadUpdateState() {
    try {
      renderUpdate(await api("/api/update/state"));
    } catch (err) {
      setUpdateStatus("The update state could not be read: " + (err.message || err), "err");
    }
    schedulePoll();
  }

  function schedulePoll() {
    if (state.poll) clearTimeout(state.poll);
    const dl = (state.update && state.update.download) || {};
    if (dl.status !== "downloading") return;
    state.poll = setTimeout(async () => {
      try {
        renderUpdate(await api("/api/update/state"));
      } catch { /* keep the last state */ }
      schedulePoll();
    }, 700);
  }

  async function checkUpdates() {
    const btn = $("#st-check");
    if (btn) btn.disabled = true;
    setUpdateStatus("Checking…");
    try {
      renderUpdate(await api("/api/update/check", { method: "POST" }));
    } catch (err) {
      setUpdateStatus("Could not check: " + (err.message || err), "err");
    } finally {
      if (btn) btn.disabled = false;
      schedulePoll();
    }
  }

  async function installUpdate() {
    const s = state.update;
    if (!s) return;
    const dl = s.download || {};
    if (dl.status !== "ready") {
      setUpdateStatus("Downloading…");
      try {
        const r = await api("/api/update/download", { method: "POST" });
        if (!r.ok) setUpdateStatus(r.error || "The download could not start", "err");
      } catch (err) {
        setUpdateStatus("The download could not start: " + (err.message || err), "err");
        return;
      }
      schedulePoll();
      return;
    }
    const ok = await confirmDialog(
      "Install and restart?",
      `CodingStudio ${s.latest} replaces this copy and starts again. The app closes ` +
      "for a moment; nothing in the project folder is touched.", "Install and restart");
    if (!ok) return;
    setUpdateStatus("Installing…");
    try {
      const r = await api("/api/update/apply", { method: "POST" });
      if (!r.ok) {
        setUpdateStatus(r.message || "The update could not be installed", "err");
        return;
      }
      setUpdateStatus(r.message || "Restarting…", "ok");
    } catch (err) {
      setUpdateStatus("The update could not be installed: " + (err.message || err), "err");
    }
  }

  /* ------------------------------------------------------------ wiring */

  function wire() {
    $("#st-nav")?.addEventListener("click", (e) => {
      const btn = e.target.closest(".st-nav-item");
      if (btn) showSection(btn.dataset.section);
    });
    $("#st-doc-save")?.addEventListener("click", saveDoc);
    $("#st-doc-reset")?.addEventListener("click", resetDoc);
    $("#st-doc")?.addEventListener("input", () => {
      const area = $("#st-doc");
      if (!area) return;
      setDocStatus(area.value === state.savedDoc ? "" : "Unsaved changes");
    });
    $("#st-check")?.addEventListener("click", checkUpdates);
    $("#st-install")?.addEventListener("click", installUpdate);

    document.addEventListener("cs:view", (e) => {
      if (String(e && e.detail) === "settings") {
        loadProjectDoc();
        if (state.section === "updates") loadUpdateState();
      } else if (state.poll) {
        clearTimeout(state.poll);
        state.poll = 0;
      }
    });
  }

  function init() {
    wire();
    showSection("project");
    // the view may already be the one on screen (a reload that lands here)
    if (window.CS.currentView() === "settings") loadProjectDoc();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
