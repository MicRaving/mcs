/* ============================================================
   Coding Studio — Summary tab (paper coding)
   Counts every level of the categorical coding fields across the
   coded papers.  Pick a field, click a level, and get the list of
   studies/experiments (or DV metrics) with their context.  The
   value that defines the list is editable inline and saved to your
   own Codings file; each study links into the coding tab.

   Driven by /api/summary/dimensions, /api/summary/variables and
   /api/summary/save.
   ============================================================ */
(() => {
  "use strict";

  const { $, $$, esc, api, optionText } = window.CS;
  const icon = window.CS.svg;

  // read-only context shown next to the editable dimension
  const CTX_EXP = [
    ["experiment_id", "Experiment"],
    ["intervention_types", "Intervention"],
    ["Setting", "Setting"],
    ["Age_Group", "Age"],
    ["n_dv", "#DV"],
    ["n_missing", "#missing"],
  ];
  const CTX_DV = [
    ["dv_name", "DV"],
    ["dv_domain", "Domain"],
    ["contrast_type", "Contrast"],
    ["eligible_primary", "Eligible"],
    ["dv_polarity", "Direction"],
  ];

  const state = {
    dims: [], byName: {}, groupOrder: [], total: {}, selDim: "", selKind: "",
    selValue: null, rows: [], totalRows: 0, nStudies: 0, truncated: false,
    groups: {}, order: [], rendered: new Set(), levelSearch: "", levelCache: {},
    edits: {}, coder: "", search: "", started: false, loaded: false, request: 0,
  };

  // Free-text / identifier fields (Study_ID, Title, dv_name, …) can have
  // hundreds or thousands of distinct values: a level filter is meaningless and
  // unusably large, so no level-selection UI is offered above this many levels.
  const MAX_LEVELS = 100;

  const toast = window.CS.toast;

  const isDirty = () => Object.values(state.edits).some((u) => Object.keys(u).length);
  function setDirty() {
    const d = isDirty();
    const pill = $("#sm-dirty");
    if (pill) { pill.textContent = d ? "unsaved changes" : "saved"; pill.className = "pill " + (d ? "warn" : "ok"); }
  }

  function labelOf(dim, value) {
    const levels = state.levelCache[dim] || [];
    const lv = levels.find((l) => String(l.value) === String(value));
    if (lv && lv.label) return optionText(lv.value, lv.label);
    return value === "" ? "—" : value;
  }

  // A field offers level selection only when it has a manageable number of levels.
  function levelSelectable(dim) {
    return ((state.byName[dim] || {}).n_levels ?? 0) <= MAX_LEVELS;
  }

  // ---------------------------------------------------------------- counts
  function renderKpis() {
    const kpi = (k, v, s) => `<div class="kpi"><div class="k">${esc(k)}</div>` +
      `<div class="v">${esc(v)}</div><div class="s">${esc(s || "")}</div></div>`;
    const t = state.total;
    $("#sm-kpis").innerHTML =
      kpi("Experiments", t.experiments, "coded experiment records") +
      kpi("DV metrics", t.dvs, "coded contrasts") +
      kpi("Studies", t.studies, "papers with coding") +
      kpi("Fields", t.fields, "coding variables");
    const sub = $("#sm-sub");
    if (sub) sub.textContent = `${t.fields} coding fields · ${t.experiments} experiments · ` +
      `${t.dvs} DV metrics. Pick a field, click a level to list the studies.`;
  }

  function renderDims() {
    const order = (state.groupOrder && state.groupOrder.length)
      ? state.groupOrder : Array.from(new Set(state.dims.map((d) => d.group)));
    $("#sm-levels").innerHTML = order.map((g) => {
      const items = state.dims.filter((d) => d.group === g);
      if (!items.length) return "";
      const chips = items.map((d) =>
        `<button class="sm-dimchip${state.selDim === d.dim ? " active" : ""}" data-dim="${esc(d.dim)}" ` +
        `data-kind="${esc(d.kind)}" title="${esc((d.kind === "dv" ? "DV metric · " : "") + d.label)}">` +
        `${esc(d.label)}<b>${d.n_levels}</b></button>`).join("");
      return `<details class="sm-dimgroup">` +
        `<summary>${icon("i-caret", "caret")}<h3>${esc(g)}</h3>` +
        `<span class="cnt">${items.length} fields</span></summary>` +
        `<div class="sm-chips">${chips}</div></details>`;
    }).join("");
    $$("#sm-levels .sm-dimchip").forEach((b) =>
      b.addEventListener("click", () => selectDim(b.dataset.dim)));
  }

  function levelChipHTML(l) {
    const active = state.selValue !== null && String(state.selValue) === String(l.value) ? " active" : "";
    const lab = l.value === "" ? `<span class="lv blank">(blank)</span>`
      : `<span class="lv">${esc(optionText(l.value, l.label || l.value))}</span>`;
    return `<button class="sm-chip${active}" data-dim="${esc(state.selDim)}" data-value="${esc(l.value)}">` +
      `${lab} <b>${l.n_records}</b> <i>${l.n_studies} ${l.n_studies === 1 ? "study" : "studies"}</i></button>`;
  }

  function renderLevelList() {
    const list = $("#sm-levelchips-list");
    if (!list) return;
    const cached = state.levelCache[state.selDim];
    if (cached === undefined) {
      list.innerHTML = `<p class="sm-hint">Loading levels…</p>`;
      return;
    }
    const q = state.levelSearch.trim().toLowerCase();
    const levels = (cached || []).filter((l) => !q
      || String(l.value).toLowerCase().includes(q)
      || String(l.label || "").toLowerCase().includes(q));
    list.innerHTML = levels.length ? levels.map(levelChipHTML).join("")
      : `<p class="sm-hint">No level matches “${esc(state.levelSearch)}”.</p>`;
    $$("#sm-levelchips-list .sm-chip").forEach((b) =>
      b.addEventListener("click", () => selectLevel(b.dataset.dim, b.dataset.value)));
  }

  function renderLevelChips() {
    const host = $("#sm-levelchips");
    if (!state.selDim) {
      host.innerHTML = `<p class="sm-hint">Pick a field above to see its levels.</p>`;
      return;
    }
    const f = state.byName[state.selDim] || { label: state.selDim, n_levels: 0 };
    if (!levelSelectable(state.selDim)) {
      host.innerHTML = `<div class="sm-levelhead"><span class="sm-fieldname">${esc(f.label)}</span>` +
        `<span class="sm-hint">${f.n_levels} levels</span></div>` +
        `<p class="sm-hint">Too many levels to filter — level selection is hidden for this variable. ` +
        `All records are listed below.</p>`;
      return;
    }
    const search = `<div class="search-wrap" style="width:220px">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><use href="#i-search"/></svg>
      <input class="input" id="sm-levelsearch" placeholder="Filter ${esc(f.label)} levels…" value="${esc(state.levelSearch)}" autocomplete="off" spellcheck="false">
    </div>`;
    host.innerHTML = `<div class="sm-levelhead"><span class="sm-fieldname">${esc(f.label)}</span>` +
      `<span class="sm-hint">${f.n_levels} levels</span>` +
      `<span class="grow"></span>${search}` +
      `<button class="sm-chip" id="sm-showall" data-dim="" data-value="">Show all</button></div>` +
      `<div class="sm-chips" id="sm-levelchips-list"></div>`;
    renderLevelList();
    $("#sm-showall")?.addEventListener("click", () => selectLevel("", ""));
    const li = $("#sm-levelsearch");
    if (li) li.addEventListener("input", () => { state.levelSearch = li.value; renderLevelList(); });
  }

  async function loadLevels(dim) {
    if (state.levelCache[dim] !== undefined) return;
    if (!levelSelectable(dim)) { state.levelCache[dim] = []; return; }
    try {
      const d = await api("/api/summary/levels?dim=" + encodeURIComponent(dim));
      state.levelCache[dim] = d.levels || [];
      if (state.byName[dim] && d.n_levels != null) state.byName[dim].n_levels = d.n_levels;
    } catch { state.levelCache[dim] = []; }
    if (state.selDim === dim) renderLevelChips();
  }

  function selectDim(dim) {
    state.selDim = dim;
    state.selKind = (state.byName[dim] || {}).kind || "";
    state.selValue = null;
    state.levelSearch = "";
    renderDims();
    renderLevelChips();
    if (levelSelectable(dim)) loadLevels(dim);
    loadRows();
  }

  function selectLevel(dim, value) {
    if (dim === "") {
      state.selDim = ""; state.selKind = ""; state.selValue = null;
      renderDims(); renderLevelChips(); loadRows(); return;
    }
    if (state.selDim === dim && state.selValue !== null && String(state.selValue) === String(value)) {
      state.selValue = null;                       // toggle the level filter off (show all)
    } else {
      state.selDim = dim; state.selKind = (state.byName[dim] || {}).kind || "";
      state.selValue = value;
    }
    renderDims(); renderLevelChips(); loadRows();
  }

  // ---------------------------------------------------------------- rows
  async function loadRows() {
    const p = new URLSearchParams({ limit: "3000" });
    if (state.selDim) { p.set("dim", state.selDim); if (state.selValue !== null) p.set("value", state.selValue); }
    if (state.search) p.set("q", state.search);
    const req = ++state.request;
    let d;
    try {
      d = await api("/api/summary/variables?" + p.toString());
    } catch (e) {
      $("#sm-table").innerHTML = `<div class="sm-empty">Could not load the list: ${esc(e.message)}</div>`;
      return;
    }
    if (req !== state.request) return;
    state.rows = d.rows || [];
    state.totalRows = d.total || 0;
    state.nStudies = d.n_studies || 0;
    state.truncated = !!d.truncated;
    state.kind = d.kind || "experiment";
    state.fieldsMeta = d.fields || [];
    const fm = state.fieldsMeta.find((f) => f.name === state.selDim);
    state.fieldOptions = (state.selDim && !levelSelectable(state.selDim))
      ? []
      : (fm && fm.options && fm.options.length
        ? fm.options
        : ((state.byName[state.selDim] || {}).levels || []).map((l) => ({ value: l.value, label: l.label })));
    state.selFieldIsList = state.selDim ? state.rows.some((r) => Array.isArray(r[state.selDim])) : false;
    buildGroups();
    renderTable();
  }

  function buildGroups() {
    state.groups = {};
    for (const r of state.rows) (state.groups[r.study_key] = state.groups[r.study_key] || []).push(r);
    state.order = Object.keys(state.groups).sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
    state.rendered = new Set();
  }

  function rowId(row) {
    return String(row.experiment_id) + (state.kind === "dv" ? "#" + row.dv_index : "");
  }
  const editVal = (study, id, field, base) => {
    const st = state.edits[study] && state.edits[study][String(id)];
    return st && field in st ? st[field] : (base || "");
  };
  function setEdit(row, field, value) {
    const study = row.study_key, ex = String(row.experiment_id);
    state.edits[study] = state.edits[study] || {};
    const rec = state.edits[study][ex] = state.edits[study][ex] || {};
    if (state.kind === "dv") {
      rec.__dvs = rec.__dvs || {};
      rec.__dvs[row.dv_index] = rec.__dvs[row.dv_index] || {};
      rec.__dvs[row.dv_index][field] = value;
    } else {
      rec[field] = value;
    }
  }

  function studyBody(study, rows) {
    const ctx = state.kind === "dv" ? CTX_DV : CTX_EXP;
    const editCol = !!state.selDim;
    const tooMany = editCol && !levelSelectable(state.selDim);
    const head = `<div class="sm-row head"><div><span>${esc(study)}</span></div>` +
      (editCol ? `<div>${esc(state.byName[state.selDim]?.label || state.selDim)}</div>` : "") +
      ctx.map(([, lab]) => `<div>${esc(lab)}</div>`).join("") + `</div>`;
    const body = rows.map((row) => {
      const id = rowId(row);
      const edited = state.edits[study] && state.edits[study][String(row.experiment_id)] &&
        (state.kind === "dv"
          ? (state.edits[study][String(row.experiment_id)].__dvs || {})[row.dv_index] &&
            state.selDim in (state.edits[study][String(row.experiment_id)].__dvs[row.dv_index] || {})
          : state.selDim in state.edits[study][String(row.experiment_id)]);
      let editCell = "";
      if (editCol && state.selFieldIsList) {
        const cur = row[state.selDim];
        const joined = Array.isArray(cur) ? cur.join(", ") : (cur === null || cur === undefined ? "" : cur);
        editCell = `<div><span class="sm-val">${esc(joined === "" ? "—" : joined)}</span></div>`;
      } else if (tooMany) {
        // Free-text field: no level list, edit as plain text.
        const cur = editVal(study, id, state.selDim, row[state.selDim]);
        const cv = cur === null || cur === undefined ? "" : String(cur);
        editCell = `<div><input class="input sm-edit${edited ? " overridden" : ""}" ` +
          `data-study="${esc(study)}" data-exp="${esc(String(row.experiment_id))}" ` +
          `data-dv="${state.kind === "dv" ? row.dv_index : ""}" data-field="${esc(state.selDim)}" ` +
          `value="${esc(cv)}" autocomplete="off" spellcheck="false"></div>`;
      } else if (editCol) {
        const opts = state.fieldOptions || [];
        const cur = editVal(study, id, state.selDim, row[state.selDim]);
        const cv = cur === null || cur === undefined ? "" : String(cur);
        if (opts.length) {
          let list = opts.slice();
          if (cv !== "" && !list.some((o) => String(o.value) === cv)) {
            list = [{ value: cv, label: labelOf(state.selDim, cv) }].concat(list);
          }
          editCell = `<div><select class="select${edited ? " overridden" : ""}" ` +
            `data-study="${esc(study)}" data-exp="${esc(String(row.experiment_id))}" ` +
            `data-dv="${state.kind === "dv" ? row.dv_index : ""}" data-field="${esc(state.selDim)}">` +
            list.map((o) => `<option value="${esc(o.value)}"${String(o.value) === cv ? " selected" : ""}>` +
              `${esc(o.value === "" ? "—" : (o.label || o.value))}</option>`).join("") + `</select></div>`;
        } else {
          let list = (state.levelCache[state.selDim] || []).map((l) => ({ value: l.value, label: l.label }));
          if (cv !== "" && !list.some((o) => String(o.value) === cv)) list = [{ value: cv, label: labelOf(state.selDim, cv) }].concat(list);
          editCell = `<div><select class="select${edited ? " overridden" : ""}" ` +
            `data-study="${esc(study)}" data-exp="${esc(String(row.experiment_id))}" ` +
            `data-dv="${state.kind === "dv" ? row.dv_index : ""}" data-field="${esc(state.selDim)}">` +
            list.map((o) => `<option value="${esc(o.value)}"${String(o.value) === cv ? " selected" : ""}>` +
              `${esc(o.value === "" ? "—" : (o.label || o.value))}</option>`).join("") + `</select></div>`;
        }
      }
      const cells = ctx.map(([f]) => {
        let v = row[f];
        if (Array.isArray(v)) v = v.join(", ");
        return `<div class="ctx" title="${esc(v ?? "")}">${esc(v === null || v === undefined || v === "" ? "—" : v)}</div>`;
      }).join("");
      return `<div class="sm-row${edited ? " pending" : ""}"><div class="rec">${esc(id)}</div>` +
        editCell + cells + `</div>`;
    }).join("");
    return head + body;
  }

  function renderTable() {
    const wrap = $("#sm-table");
    const title = $("#sm-listtitle");
    if (title) {
      if (!state.selDim) title.textContent = "All experiments";
      else {
        const f = state.byName[state.selDim];
        const val = state.selValue === null ? "all levels"
          : (state.selValue === "" ? "(blank)" : esc(labelOf(state.selDim, state.selValue)));
        title.innerHTML = `${esc(f ? f.label : state.selDim)} = ${val} · ` +
          `${state.totalRows} rows · ${state.nStudies} studies`;
      }
    }
    const note = $("#sm-note");
    if (note) {
      note.textContent = state.rows.length
        ? `Showing ${state.rows.length} of ${state.totalRows} rows across ${state.nStudies} studies` +
          (state.truncated ? " · truncated (narrow the filter)" : "") +
          " · the highlighted field is editable; Save writes to your coder file."
        : "";
    }
    if (!state.rows.length) {
      wrap.innerHTML = `<div class="sm-empty">No rows for this selection.</div>`;
      return;
    }
    wrap.innerHTML = state.order.map((study) => {
      const rows = state.groups[study];
      const edited = state.edits[study] && Object.keys(state.edits[study]).length;
      return `<details class="sm-study" data-study="${esc(study)}">` +
        `<summary class="sm-study-head">${icon("i-caret", "caret")}` +
        `<span class="key">${esc(study)}</span>` +
        `<span class="n">${rows.length} ${state.kind === "dv" ? "DV" : "experiment"}${rows.length === 1 ? "" : "s"}</span>` +
        `${edited ? `<span class="sm-edited">edited</span>` : ""}` +
        `<span class="grow"></span>` +
        `<button type="button" class="sm-open" data-open="${esc(study)}">Open in coding</button>` +
        `</summary><div class="sm-study-body"></div></details>`;
    }).join("");
    $$("#sm-table .sm-study").forEach((d) => d.addEventListener("toggle", onStudyToggle, true));
    $$("#sm-table .sm-open").forEach((b) => b.addEventListener("click", (e) => {
      e.preventDefault(); e.stopPropagation();
      openInCoding(b.dataset.open);
    }));
  }

  function onStudyToggle(e) {
    const d = e.target;
    if (!d.classList || !d.classList.contains("sm-study")) return;
    if (!d.open || state.rendered.has(d.dataset.study)) return;
    const rows = state.groups[d.dataset.study] || [];
    d.querySelector(".sm-study-body").innerHTML = studyBody(d.dataset.study, rows);
    state.rendered.add(d.dataset.study);
  }

  // ---------------------------------------------------------------- editing
  function onSelectChange(e) {
    const s = e.target;
    if (!s || s.tagName !== "SELECT") return;
    const study = s.dataset.study, exp = s.dataset.exp, field = s.dataset.field;
    if (!study || !exp || !field) return;
    const row = { study_key: study, experiment_id: exp, dv_index: s.dataset.dv };
    setEdit(row, field, s.value);
    s.classList.add("overridden");
    const r = s.closest(".sm-row"); if (r) r.classList.add("pending");
    setDirty(); renderKpis();
  }

  function onEditInput(e) {
    const s = e.target;
    if (!s || s.tagName !== "INPUT" || !s.classList.contains("sm-edit")) return;
    const study = s.dataset.study, exp = s.dataset.exp, field = s.dataset.field;
    if (!study || !exp || !field) return;
    const row = { study_key: study, experiment_id: exp, dv_index: s.dataset.dv };
    setEdit(row, field, s.value);
    s.classList.add("overridden");
    const r = s.closest(".sm-row"); if (r) r.classList.add("pending");
    setDirty();
  }

  function summaryVisible() {
    const v = $("#view-summary");
    return !!v && !v.hidden;
  }

  async function save() {
    if (!summaryVisible()) return;   // the shared Save button also serves the Coding tab
    if (!isDirty()) { toast("Nothing to save", "ok"); return; }
    const coderEl = $("#coder-input");
    const coder = (coderEl ? coderEl.value : state.coder || "").trim();
    if (!coder) { toast("Enter your coder name before saving", "err"); if (coderEl) coderEl.focus(); return; }
    state.coder = coder;
    try {
      const r = await api("/api/summary/save", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ coder, edits: state.edits }),
      });
      state.edits = {}; setDirty();
      await loadDimensions(); await loadRows();
      toast("Coding edits saved", "ok", `${r.studies} study file(s) · ${r.fields} field(s)`);
    } catch (e) {
      toast("Save failed", "err", String(e.message || e));
    }
  }

  async function refreshSummary() {
    if (!summaryVisible()) return;   // the shared Refresh button also serves the Coding tab
    state.loaded = false;
    await loadDimensions();
    await loadRows();
  }

  function openInCoding(key) {
    const tab = document.querySelector('.viewtab[data-view="coding"]');
    if (tab) tab.click();
    window.dispatchEvent(new CustomEvent("cs:open-paper", { detail: { key } }));
  }

  // ---------------------------------------------------------------- load
  async function loadDimensions() {
    try {
      const d = await api("/api/summary/dimensions");
      state.dims = d.dimensions || [];
      state.groupOrder = d.group_order || [];
      state.byName = {};
      state.levelCache = {};
      state.dims.forEach((x) => { state.byName[x.dim] = x; });
      state.total = { experiments: d.total_experiments, dvs: d.total_dvs,
                      studies: d.total_studies, fields: d.n_fields };
      state.loaded = true;
      // ensure the selected field still exists
      if (state.selDim && !state.byName[state.selDim]) { state.selDim = ""; state.selKind = ""; state.selValue = null; }
      renderKpis(); renderDims(); renderLevelChips();
      if (state.selDim) loadLevels(state.selDim);
    } catch (e) {
      $("#sm-levels").innerHTML = `<div class="sm-empty">Could not load the summary: ${esc(e.message)}</div>`;
    }
  }

  async function ensureStarted() {
    if (!state.loaded) await loadDimensions();
    if (!state.rows.length && !state.selDim && !state.search) await loadRows();
  }

  // ---------------------------------------------------------------- wiring
  function init() {
    if (state.started) return;
    state.started = true;
    try { state.coder = localStorage.getItem("cs.coder") || ""; } catch { /* ignore */ }
    const coder = $("#coder-input");
    if (coder) {
      state.coder = coder.value || state.coder;
      coder.addEventListener("input", () => { state.coder = coder.value.trim(); });
    }
    // The Summary reuses the app top bar: Save lives there.
    $("#save-btn")?.addEventListener("click", save);
    $("#sm-clearfilter")?.addEventListener("click", () => {
      state.selDim = ""; state.selKind = ""; state.selValue = null;
      renderDims(); renderLevelChips(); loadRows();
    });
    const search = $("#sm-search");
    if (search) {
      let t = null;
      search.addEventListener("input", () => {
        clearTimeout(t);
        t = setTimeout(() => { state.search = search.value.trim(); loadRows(); }, 200);
      });
    }
    const table = $("#sm-table");
    if (table) {
      table.addEventListener("change", onSelectChange);
      table.addEventListener("input", onEditInput);
    }
    document.addEventListener("cs:view", (e) => { if (e.detail === "summary") ensureStarted(); });
    window.addEventListener("keydown", (e) => {
      const v = $("#view-summary");
      if (!v || v.hidden) return;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
        e.preventDefault(); e.stopImmediatePropagation(); save();
      }
    }, true);
    let saved = "coding";
    try { saved = localStorage.getItem("cs.view") || "coding"; } catch { /* ignore */ }
    if (saved === "summary") ensureStarted();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
