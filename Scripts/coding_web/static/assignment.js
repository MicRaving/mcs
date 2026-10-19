/* ============================================================
   Coding Studio — Assignment workspace (Coverage tab)
   The three-pane assignment workspace, rendered inside the Coverage
   view.  Panes: (L) targets — coverage gaps + unmapped raw columns;
   (M) the assignment form with a live decoder preview; (R) raw-variable
   context (value labels, dataset preview, example rows, stats,
   codebook/paper snippets) + the coded Extraction context.

   Saving is one sweep: every edit is staged in memory (the coder field
   is optional and defaults to the neutral label "coder"), and the
   single top-row **Save changes** button posts every pending target to
   `POST /api/assignments/<key>`, which writes the canonical
   `Datasets/<Key>/<Key> - variable_assignments.csv` and regenerates the
   harmonized sidecar/wide on the server (variable_assignments.write_rows
   + apply_files).  Per-target actions (Mark N/A, Split true/false) only
   stage a change; they are applied by the same top-row save.

   Endpoints:
     GET  /api/coverage/study/<key>     targets
     GET  /api/assignments/<key>        applied + drafts
     POST /api/assignments/<key>        save every pending target to the dataset
     GET  /api/rawvar/<key>             raw-variable context
     GET  /api/harmonizer/decode        live decode preview
     GET  /api/harmonizer/<key>         harmonized variables (opts)

   Exposes `window.CSAssign`: open(key, slot), close(), mount(el).
   ============================================================ */
(() => {
  "use strict";

  const { $, esc, api, toast } = window.CS;

  const DEFAULT_OPTIONS = {
    role: ["dv", "covariate", ""],
    dv_domain: ["technique_recognition", "credibility", "sharing_intention",
      "search_behavior", "attitude_policy", "trust",
      "media_literacy_attitudes", "critical_thinking", "other", ""],
    veracity: ["true", "false", ""],
    timepoint: ["pre", "post", "followup", ""],
    dv_polarity: ["higher_is_better", "higher_is_worse", "ambiguous", ""],
  };

  const A_SELECT = ["role", "dv_domain", "veracity", "timepoint", "dv_polarity"];
  const A_LABEL = {
    role: "Role", dv_domain: "Domain", veracity: "Veracity",
    timepoint: "Timepoint", dv_polarity: "Direction",
  };
  const A_SLOT_FIELD = {
    S5: "dv_domain", S6: "veracity", S7: "timepoint", S8: "dv_polarity",
  };
  const enc = encodeURIComponent;

  const state = {
    key: null, coder: "", host: null, token: 0, mounted: false,
    byVar: {}, options: DEFAULT_OPTIONS, form: null,
    ws: {
      targets: [], selected: null, applied: [], context: null, decode: null,
      contextLoading: false, pending: {},
      filter: "gaps", search: "", focusSlot: "", open: false, busy: false,
    },
  };

  // ---------------------------------------------------------------- coder
  function readCoder() {
    try { return (localStorage.getItem("cs.coder") || "").trim(); } catch { return ""; }
  }

  // ---------------------------------------------------------------- keys
  function normArtifact(a) {
    return (!a || a === "*") ? "*" : String(a);
  }

  function draftKey(r) {
    // "" and "*" both mean the wildcard artifact -> one pending slot per target.
    return [normArtifact(r.artifact), r.original_variable || "", r.item_scope || ""].join("\u0001");
  }

  function sameKey(a, b) {
    return normArtifact(a.artifact) === normArtifact(b.artifact) &&
      (a.original_variable || "") === (b.original_variable || "") &&
      (a.item_scope || "") === (b.item_scope || "");
  }

  function findApplied(row) {
    return (state.ws.applied || []).find((a) => sameKey(a, row)) || null;
  }

  function stagedRows() {
    return Object.values(state.ws.pending || {});
  }

  function isStaged(row) {
    return Object.prototype.hasOwnProperty.call(state.ws.pending || {}, draftKey(row));
  }

  // ---------------------------------------------------------------- targets
  function filteredTargets() {
    const ws = state.ws;
    const q = ws.search.trim().toLowerCase();
    return (ws.targets || []).filter((t) => {
      if (ws.filter === "gaps" && t.kind !== "coverage") return false;
      if (ws.filter === "raw" && t.kind !== "raw") return false;
      if (ws.filter === "study" && !(t.kind === "coverage" && t.scope === "study")) return false;
      if (!q) return true;
      return (t.target + " " + (t.uniform_variable || "") + " " + (t.detail || ""))
        .toLowerCase().includes(q);
    });
  }

  function targetKind(t) {
    if (t.kind === "raw") return "raw";
    if (t.scope === "study") return "study";
    return "gap";
  }

  function targetKey(t) {
    return {
      artifact: t.artifact || "", original_variable: t.original_variable || "",
      item_scope: t.item_scope || "",
    };
  }

  function targetListHtml() {
    const list = filteredTargets();
    if (!list.length) return `<p class="assign-hint">No targets match this filter.</p>`;
    return list.map((t, i) => {
      const main = t.original_variable || t.uniform_variable || t.target;
      const sub = [t.slot, t.status, t.uniform_variable && t.uniform_variable !== main ? t.uniform_variable : ""]
        .filter(Boolean).join(" · ");
      const sel = (state.ws.selected === t) ? " selected" : "";
      const staged = t.original_variable && isStaged(targetKey(t))
        ? `<span class="at-staged" title="Staged — saved by the top-row Save changes">●</span>` : "";
      return `<button type="button" class="assign-target${sel}" data-idx="${i}">` +
        `<span class="at-kind ${targetKind(t)}">${esc(targetKind(t))}</span>` +
        `<span class="at-main" title="${esc(main)}">${esc(main)}${staged}</span>` +
        `<span class="at-sub">${esc(sub)}</span></button>`;
    }).join("");
  }

  function renderTargetList() {
    const host = state.host && state.host.querySelector(".assign-tlist");
    if (host) host.innerHTML = targetListHtml();
  }

  // ---------------------------------------------------------------- panel
  function updateHeader() {
    const host = state.host;
    if (!host) return;
    const count = host.querySelector("#assign-count");
    if (count) {
      const nT = (state.ws.targets || []).length;
      const nA = (state.ws.applied || []).length;
      count.textContent = `${nT} target${nT === 1 ? "" : "s"} · ${nA} applied`;
    }
    const btn = host.querySelector("#assign-save-all");
    const n = stagedRows().length;
    if (btn) {
      btn.disabled = n === 0 || state.ws.busy;
      btn.textContent = n ? `Save changes (${n})` : "Save changes";
    }
    const dirty = host.querySelector("#assign-dirty");
    if (dirty) {
      dirty.hidden = n === 0;
      dirty.textContent = n ? `${n} unsaved` : "";
    }
  }

  function workspacePanel() {
    const ws = state.ws;
    const head = `<header class="assign-head" id="assign-head">` +
      `<b>Assignment workspace</b>` +
      `<span class="pill">${esc(state.key || "")}</span>` +
      `<span class="pill" id="assign-count">–</span>` +
      `<span class="assign-dirty" id="assign-dirty" hidden></span>` +
      `<span class="grow"></span>` +
      `<button type="button" class="btn primary" id="assign-save-all" disabled ` +
      `title="Write every staged change to the dataset and regenerate the harmonized sidecar/wide">` +
      `Save changes</button>` +
      `<button type="button" class="btn ghost" id="assign-close">Close</button>` +
      `</header>`;
    const chips = [["gaps", "Coverage gaps"], ["raw", "Unmapped raw"], ["study", "Study slots"], ["all", "All"]]
      .map(([v, l]) => `<button type="button" class="chip${ws.filter === v ? " active" : ""}" data-wsfilter="${v}">${esc(l)}</button>`).join("");
    return `<section class="assign-ws" id="assign-ws">${head}` +
      `<div class="assign-grid">` +
      `<div class="assign-pane assign-targets"><div class="assign-pane-h">Targets</div>` +
      `<div class="assign-tfilter">${chips}</div>` +
      `<div class="search-wrap assign-tsearch"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><use href="#i-search"/></svg>` +
      `<input class="input" id="assign-tsearch" placeholder="Filter targets…" value="${esc(ws.search)}" autocomplete="off" spellcheck="false"></div>` +
      `<div class="assign-tlist">${targetListHtml()}</div>` +
      `<div id="assign-pending">${pendingListHtml()}</div>` +
      `<div id="assign-applied">${appliedListHtml()}</div></div>` +
      `<div class="assign-pane assign-form" id="assign-form">${renderForm()}</div>` +
      `<div class="assign-pane assign-context" id="assign-context">${renderContext()}</div>` +
      `</div></section>`;
  }

  function pendingListHtml() {
    const rows = stagedRows();
    if (!rows.length) return "";
    return `<div class="assign-applied assign-pending"><div class="assign-pane-h">Pending changes ` +
      `<span class="pill">${rows.length}</span></div>` +
      rows.slice(0, 60).map((r) => {
        const label = r.original_variable || r.uniform_variable || "";
        const flds = ["role", "dv_domain", "veracity", "timepoint", "dv_polarity"]
          .filter((f) => r[f]).map((f) => `${A_LABEL[f] || f}=${r[f]}`).join(", ");
        return `<div class="assign-applied-row">` +
          `<span class="aa-orig">${esc(label)}</span>` +
          `<span class="aa-uv" title="${esc(flds)}">${esc(r.uniform_variable || flds)}</span>` +
          `<span class="aa-st ${esc(r.status || "")}">${esc(r.status || "")}</span>` +
          `<button type="button" class="aa-x" data-unstage="${esc(draftKey(r))}" title="Discard this staged change">✕</button></div>`;
      }).join("") + `</div>`;
  }

  function appliedListHtml() {
    const rows = state.ws.applied || [];
    if (!rows.length) return "";
    return `<div class="assign-applied"><div class="assign-pane-h">Applied assignments</div>` +
      rows.slice(0, 40).map((r) =>
        `<div class="assign-applied-row" title="${esc(r.evidence || r.note || "")}">` +
        `<span class="aa-orig">${esc(r.original_variable || "")}</span>` +
        `<span class="aa-uv">${esc(r.uniform_variable || "")}</span>` +
        `<span class="aa-st ${esc(r.status || "")}">${esc(r.status || "")}</span></div>`).join("") +
      `</div>`;
  }

  function renderPendingList() {
    const host = state.host && state.host.querySelector("#assign-pending");
    if (host) host.innerHTML = pendingListHtml();
  }

  function renderAppliedList() {
    const host = state.host && state.host.querySelector("#assign-applied");
    if (host) host.innerHTML = appliedListHtml();
  }

  function optSel(field, value) {
    const opts = state.options[field] || DEFAULT_OPTIONS[field] || [""];
    let list = opts.slice();
    const v = value == null ? "" : String(value);
    if (v !== "" && !list.includes(v)) list = [v].concat(list);
    return list.map((o) =>
      `<option value="${esc(o)}"${String(o) === v ? " selected" : ""}>${esc(o === "" ? "—" : o)}</option>`).join("");
  }

  function renderForm() {
    const f = state.form;
    if (!f) {
      return `<div class="assign-pane-h">Assignment form</div>` +
        `<p class="assign-hint">Select a target on the left to label the variable. The form shows the raw variable, the uniform name with a live decode preview, and the fields the harmonizer consumes.</p>`;
    }
    const focus = state.ws.focusSlot && A_SLOT_FIELD[state.ws.focusSlot];
    const fieldCls = (field) => "assign-field" + (field === focus ? " focus" : "");
    const selects = A_SELECT.map((field) =>
      `<div class="${fieldCls(field)}"><label>${esc(A_LABEL[field])}</label>` +
      `<select class="select" data-afield="${field}">${optSel(field, f[field])}</select></div>`).join("");
    const keyed = !!f.original_variable;
    return `<div class="assign-pane-h">Assignment form` +
      (state.ws.focusSlot ? ` <span class="pill">slot ${esc(state.ws.focusSlot)}</span>` : "") +
      (keyed && isStaged(f) ? ` <span class="pill staged-pill">staged</span>` : "") + `</div>` +
      `<div class="assign-orig"><label>Original variable</label>` +
      `<input class="input mono-val" value="${esc(f.original_variable || "")}" readonly>` +
      (f.artifact ? `<span class="assign-tag">artifact ${esc(f.artifact)}</span>` : "") +
      (f.item_scope ? `<span class="assign-tag">item ${esc(f.item_scope)}</span>` : "") + `</div>` +
      (keyed ? "" : `<p class="assign-hint assign-nokey">This is a study-level slot with no raw variable to key on. ` +
        `Pick a variable-scope gap (S4–S8) or an unmapped raw column to record an assignment for it.</p>`) +
      `<div class="assign-field"><label>Uniform variable</label>` +
      `<input class="input mono-val" id="assign-uv" data-ainput="uniform_variable" value="${esc(f.uniform_variable || "")}" placeholder="dv_<domain>[_<veracity>][_<time>]" autocomplete="off" spellcheck="false"></div>` +
      `<div class="assign-decode" id="assign-decode">${decodeHtml()}</div>` +
      `<div class="assign-grid2">${selects}</div>` +
      `<div class="assign-field"><label>Evidence</label>` +
      `<input class="input" data-ainput="evidence" value="${esc(f.evidence || "")}" placeholder="variables.csv row, codebook p.12, wide value labels…" autocomplete="off" spellcheck="false"></div>` +
      `<div class="assign-field"><label>Note</label>` +
      `<textarea class="textarea" rows="2" data-ainput="note" placeholder="Why this label; N/A reason; split spec…">${esc(f.note || "")}</textarea></div>` +
      `<div class="assign-field"><label>Decision</label>` +
      `<select class="select" data-afield="status">` +
      ["proposed", "confirmed"].map((s) => `<option value="${s}"${f.status === s ? " selected" : ""}>${s}</option>`).join("") +
      `</select></div>` +
      `<div class="assign-actions">` +
      `<button type="button" class="btn" id="assign-stage" ${keyed ? "" : "disabled"} title="Stage this target for the top-row Save changes">Stage change</button>` +
      `<button type="button" class="btn ghost" id="assign-discard" title="Discard the staged change for this target">Discard</button>` +
      `<button type="button" class="btn ghost" id="assign-na" ${keyed ? "" : "disabled"} title="Mark this variable not applicable with a reason">Mark N/A</button>` +
      `<button type="button" class="btn ghost" id="assign-split" ${keyed ? "" : "disabled"} title="Split a composite into true/false columns by item list">Split true/false</button>` +
      `</div>` +
      `<div class="assign-splitform" id="assign-splitform" hidden>` +
      `<div class="assign-grid2">` +
      `<div class="assign-field"><label>pid column</label><input class="input" id="split-pid" value="pid"></div>` +
      `<div class="assign-field"><label>true values</label><input class="input" id="split-true" placeholder="1,true,real"></div>` +
      `<div class="assign-field"><label>false values</label><input class="input" id="split-false" placeholder="0,-1,false,fake"></div>` +
      `<div class="assign-field"><label>source file</label><input class="input" id="split-file" placeholder="${esc(state.key || "")} - prepared.csv"></div>` +
      `</div><button type="button" class="btn" id="split-add">Add split assignment</button></div>` +
      `<div class="assign-status" id="assign-status"></div>`;
  }

  function decodeHtml() {
    const d = state.ws.decode;
    if (!d) return `<span class="decode-hint">Live decode preview appears as you type the uniform name.</span>`;
    const rows = [
      ["domain", d.domain, false],
      ["veracity", d.veracity, d.veracity_agreement === false],
      ["timepoint", d.timepoint, false],
      ["polarity", d.polarity, false],
    ].map(([k, v, warn]) =>
      `<span class="decode-cell${warn ? " warn" : ""}"><span>${esc(k)}</span>` +
      `<b>${esc(v || "—")}</b></span>`).join("");
    const note = (d.veracity_agreement === false)
      ? `<span class="decode-note warn">decoders disagree: template=${esc(d.veracity_template || "—")} · contract=${esc(d.veracity_contract || "—")}</span>`
      : (d.errors && d.errors.length ? `<span class="decode-note">${esc(d.errors[0])}</span>` : "");
    return `<div class="decode-row">${rows}</div>${note}`;
  }

  function renderContext() {
    const c = state.ws.context;
    const head = `<div class="assign-pane-h">Context</div>`;
    if (state.ws.contextLoading) return head + `<p class="assign-hint"><span class="spinner"></span> Loading raw context…</p>`;
    if (!c) return head + `<p class="assign-hint">Select a target to load value labels, a dataset preview, example rows and codebook text. The context degrades gracefully — a missing source never blocks an assignment.</p>`;
    const parts = [];
    if (c.source) parts.push(`<div class="ctx-src">source: <b>${esc(c.source.file)}</b> (${esc(c.source.kind)})` +
      (c.matched_name ? ` · matched <code>${esc(c.matched_name)}</code>` : "") + `</div>`);
    if (c.variable_label) parts.push(`<div class="ctx-vlabel">${esc(c.variable_label)}</div>`);
    if (c.value_labels && c.value_labels.length) {
      parts.push(`<div class="ctx-block"><div class="ctx-h">Value labels</div><div class="ctx-labels">` +
        c.value_labels.slice(0, 60).map((l) =>
          `<span class="ctx-label"><b>${esc(l.value)}</b> ${esc(l.label)}</span>`).join("") + `</div></div>`);
    }
    if (c.stats) {
      const s = c.stats;
      parts.push(`<div class="ctx-block"><div class="ctx-h">Distribution</div><div class="ctx-stats">` +
        `<span>n <b>${esc(s.n)}</b></span><span>missing <b>${esc(s.missing)}</b></span>` +
        `<span>unique <b>${esc(s.n_unique)}</b></span>` +
        (s.numeric ? `<span>min <b>${esc(s.min)}</b></span><span>max <b>${esc(s.max)}</b></span><span>mean <b>${esc(s.mean)}</b></span>` : "") +
        `</div></div>`);
    }
    if (c.top_values && c.top_values.length) {
      parts.push(`<div class="ctx-block"><div class="ctx-h">Top values</div><div class="ctx-labels">` +
        c.top_values.map((t) => `<span class="ctx-label"><b>${esc(t.value)}</b> ×${esc(t.n)}</span>`).join("") + `</div></div>`);
    }
    if (c.dataset_preview) {
      const dp = c.dataset_preview;
      const isHl = (col) => col && col === dp.highlight;
      const head2 = `<thead><tr>` +
        (dp.columns || []).map((col) => `<th${isHl(col) ? ' class="hl"' : ""}>${esc(col)}</th>`).join("") +
        `</tr></thead>`;
      const body = (dp.rows || []).map((row) => `<tr>` +
        (dp.columns || []).map((col, i) => `<td${isHl(col) ? ' class="hl"' : ""}>${esc(row[i] == null ? "" : row[i])}</td>`).join("") +
        `</tr>`).join("");
      parts.push(`<div class="ctx-block"><div class="ctx-h">Dataset preview ` +
        `<span class="ctx-dp-file">${esc(dp.file || "")} (${esc(dp.n_rows)} rows × ${esc(dp.n_cols)} cols)</span></div>` +
        `<div class="ctx-tablewrap"><table class="ctx-table ctx-dp">${head2}<tbody>${body}</tbody></table></div></div>`);
    }
    if (c.examples && c.examples.length) {
      const cols = c.example_cols || Object.keys(c.examples[0] || {});
      parts.push(`<div class="ctx-block"><div class="ctx-h">Example rows</div>` +
        `<div class="ctx-tablewrap"><table class="ctx-table"><thead><tr>` +
        cols.map((x) => `<th>${esc(x)}</th>`).join("") + `</tr></thead><tbody>` +
        c.examples.map((row) => `<tr>` + cols.map((x) => `<td>${esc(row[x] == null ? "" : row[x])}</td>`).join("") + `</tr>`).join("") +
        `</tbody></table></div></div>`);
    }
    if (c.original_values || c.mapped_values) {
      parts.push(`<div class="ctx-block"><div class="ctx-h">variables.csv</div>` +
        (c.original_values ? `<div class="ctx-kv"><span>original_values</span><pre>${esc(c.original_values)}</pre></div>` : "") +
        (c.mapped_values ? `<div class="ctx-kv"><span>mapped_values</span><pre>${esc(c.mapped_values)}</pre></div>` : "") +
        `</div>`);
    }
    if (c.snippets && c.snippets.length) {
      parts.push(`<div class="ctx-block"><div class="ctx-h">Paper / notes snippets</div>` +
        c.snippets.map((s) => `<div class="ctx-snip"><span class="ctx-snip-src">${esc(s.source)}</span>${esc(s.text)}</div>`).join("") + `</div>`);
    }
    if (c.codebook_files && c.codebook_files.length) {
      parts.push(`<div class="ctx-block"><div class="ctx-h">Codebook files</div><div class="ctx-labels">` +
        c.codebook_files.map((f) => `<span class="ctx-label">${esc(f)}</span>`).join("") + `</div></div>`);
    }
    if (c.degraded && c.degraded.length) {
      parts.push(`<div class="ctx-block"><div class="ctx-h">Graceful degradation</div>` +
        c.degraded.map((d) => `<div class="ctx-deg">${esc(d)}</div>`).join("") + `</div>`);
    }
    if (!parts.length) parts.push(`<p class="assign-hint">No context found for this variable — you can still assign it.</p>`);
    parts.push(`<div class="ctx-foot"><button type="button" class="btn ghost" id="assign-open-coding">Open coding tab</button></div>`);
    return head + parts.join("");
  }

  function renderDecodeBox() {
    const box = state.host && state.host.querySelector("#assign-decode");
    if (box) box.innerHTML = decodeHtml();
  }

  function renderContextPane() {
    const host = state.host && state.host.querySelector("#assign-context");
    if (host) host.innerHTML = renderContext();
  }

  function renderFormPane() {
    const host = state.host && state.host.querySelector("#assign-form");
    if (host) host.innerHTML = renderForm();
  }

  function skeletonHtml() {
    return `<section class="assign-ws" id="assign-ws"><header class="assign-head">` +
      `<b>Assignment workspace</b><span class="pill">loading…</span></header>` +
      `<div class="assign-grid"><div class="assign-pane"><div class="skeleton">` +
      `<div class="sk" style="width:70%"></div><div class="sk" style="width:90%"></div>` +
      `<div class="sk" style="width:60%"></div></div></div></div></section>`;
  }

  function renderHost() {
    const host = state.host;
    if (!host) return;
    if (!state.ws.open || !state.key) { host.hidden = true; host.innerHTML = ""; return; }
    host.hidden = false;
    host.innerHTML = workspacePanel();
    updateHeader();
  }

  // ---------------------------------------------------------------- load
  async function loadStudy(key, slot) {
    state.ws.targets = [];
    state.ws.selected = null;
    state.ws.context = null;
    state.ws.decode = null;
    state.form = null;
    state.ws.focusSlot = slot || "";
    const [cov, asg, harm] = await Promise.all([
      api("/api/coverage/study/" + enc(key)).catch(() => ({ targets: [] })),
      api("/api/assignments/" + enc(key)).catch(() => ({ drafts: [], applied: [] })),
      api("/api/harmonizer/" + enc(key)).catch(() => null),
    ]);
    state.ws.targets = cov.targets || [];
    state.ws.applied = asg.applied || [];
    state.byVar = {};
    if (harm) {
      (harm.variables || []).forEach((v) => { state.byVar[v.uniform_variable] = v; });
      state.options = Object.assign({}, DEFAULT_OPTIONS, harm.options || {});
    }
  }

  async function open(key, slot) {
    if (!key) return;
    if (!state.host) mount(document.getElementById("cv-assign-host"));
    if (!state.host) return;
    const token = ++state.token;
    state.key = key;
    state.coder = readCoder();
    state.ws.open = true;
    state.ws.focusSlot = slot || "";
    state.ws.selected = null;
    state.ws.context = null;
    state.ws.decode = null;
    state.form = null;
    state.ws.pending = {};
    state.host.hidden = false;
    state.host.innerHTML = skeletonHtml();
    try {
      await loadStudy(key, slot);
    } catch (e) {
      if (token !== state.token) return;
      state.host.innerHTML = `<section class="assign-ws" id="assign-ws">` +
        `<header class="assign-head"><b>Assignment workspace</b>` +
        `<span class="grow"></span><button type="button" class="btn ghost" id="assign-close">Close</button></header>` +
        `<div class="assign-pane"><p class="assign-hint">Could not load targets: ${esc(e.message || e)}</p></div></section>`;
      return;
    }
    if (token !== state.token) return;    // a newer open() superseded this one
    renderHost();
    focusFirstSlot(slot || "");
    try { state.host.scrollIntoView({ block: "nearest" }); } catch { /* ignore */ }
  }

  function close() {
    if (stagedRows().length && !window.confirm("Discard unsaved assignment changes?")) return;
    state.token++;
    state.ws.open = false;
    state.ws.selected = null;
    state.ws.context = null;
    state.ws.decode = null;
    state.form = null;
    state.ws.pending = {};
    if (state.host) { state.host.hidden = true; state.host.innerHTML = ""; }
  }

  function focusFirstSlot(slot) {
    state.ws.focusSlot = slot || "";
    const list = filteredTargets();
    let idx = list.findIndex((t) => t.slot === slot);
    if (idx < 0 && state.ws.targets.length) {
      idx = list.findIndex((t) => t.kind === "coverage");
    }
    if (idx < 0) idx = 0;
    if (list[idx]) selectTarget(idx);
  }

  function selectTarget(idx) {
    const list = filteredTargets();
    const t = list[idx];
    if (!t) return;
    state.ws.selected = t;
    state.ws.context = null;
    state.ws.decode = null;
    const v = t.kind === "coverage" ? (state.byVar[t.uniform_variable] || {}) : {};
    const base = {
      artifact: t.artifact || v.artifact || "",
      original_variable: t.original_variable || v.original_variable || "",
      item_scope: t.item_scope || v.item_scope || "",
      uniform_variable: t.uniform_variable || "",
      role: t.kind === "coverage" ? (v.role || "dv") : "",
      dv_domain: v.dv_domain || "",
      veracity: v.veracity || "",
      timepoint: v.timepoint || "",
      dv_polarity: v.dv_polarity || "",
      slot_fix: (t.kind === "coverage" && t.scope === "study") ? (t.slot || "") : "",
      source_file: "", evidence: t.evidence || "", note: "", status: "proposed",
    };
    // Prefer the already-applied assignment for this target, then any staged
    // change, so the form shows what the dataset (or the pending save) holds.
    const f = Object.assign({}, base);
    const staged = state.ws.pending[draftKey(base)];
    const applied = appliedFor(base);
    const src = staged || applied;
    if (src) {
      for (const fld of ["artifact", "original_variable", "item_scope", "uniform_variable",
        "role", "dv_domain", "veracity", "timepoint", "dv_polarity", "slot_fix",
        "source_file", "evidence", "note", "status"]) {
        if (src[fld] !== undefined && src[fld] !== null && String(src[fld]) !== "") f[fld] = src[fld];
      }
    }
    state.form = f;
    state.ws.focusSlot = t.slot || "";
    renderHost();
    fetchContext();
    decodePreview();
  }

  function appliedFor(row) {
    return findApplied(row);
  }

  function fetchContext() {
    const f = state.form;
    if (!f || !state.key) return;
    state.ws.contextLoading = true;
    renderContextPane();
    const qs = new URLSearchParams({
      var: f.original_variable || "", uniform: f.uniform_variable || "",
      artifact: f.artifact || "",
    });
    const token = state.token;
    api("/api/rawvar/" + enc(state.key) + "?" + qs.toString())
      .then((d) => { if (token === state.token) state.ws.context = d; })
      .catch((e) => { if (token === state.token) state.ws.context = { degraded: ["context unavailable: " + String(e.message || e)] }; })
      .finally(() => {
        if (token !== state.token) return;
        state.ws.contextLoading = false;
        renderContextPane();
      });
  }

  let decodeTimer = null;
  function decodePreview() {
    const f = state.form;
    if (!f) return;
    clearTimeout(decodeTimer);
    const token = state.token;
    decodeTimer = setTimeout(() => {
      const qs = new URLSearchParams({
        name: f.uniform_variable || "", notes: f.note || "",
        original: f.original_variable || "",
      });
      api("/api/harmonizer/decode?" + qs.toString())
        .then((d) => { if (token === state.token) state.ws.decode = d; })
        .catch((e) => { if (token === state.token) state.ws.decode = { errors: [String(e.message || e)] }; })
        .finally(() => { if (token === state.token) renderDecodeBox(); });
    }, 220);
  }

  function pickRow(f) {
    return {
      artifact: f.artifact || "", original_variable: f.original_variable || "",
      source_file: f.source_file || "", item_scope: f.item_scope || "",
      uniform_variable: f.uniform_variable || "", role: f.role || "",
      dv_domain: f.dv_domain || "", veracity: f.veracity || "",
      timepoint: f.timepoint || "", dv_polarity: f.dv_polarity || "",
      slot_fix: f.slot_fix || "", evidence: f.evidence || "", note: f.note || "",
      status: f.status || "proposed",
    };
  }

  function setStatus(msg, kind = "") {
    const el = state.host && state.host.querySelector("#assign-status");
    if (el) { el.textContent = msg || ""; el.className = "assign-status " + kind; }
  }

  // ---------------------------------------------------------------- staging
  // Every edit stages the current target in memory. Nothing is written until the
  // top-row Save changes button applies the whole batch in one request.
  function stageForm({ quiet = false, rerender = true } = {}) {
    const f = state.form;
    if (!f) return false;
    if (!f.original_variable) {
      if (!quiet) setStatus("This target has no raw variable to key the assignment on — pick a variable-scope target.", "err");
      return false;
    }
    const row = pickRow(f);
    state.ws.pending[draftKey(row)] = row;
    updateHeader();
    renderTargetList();
    renderPendingList();
    if (rerender) renderFormPane();
    if (!quiet) setStatus("Staged — press “Save changes” to write to the dataset.", "ok");
    return true;
  }

  function unstage(key) {
    if (!Object.prototype.hasOwnProperty.call(state.ws.pending, key)) return;
    delete state.ws.pending[key];
    updateHeader();
    renderTargetList();
    renderPendingList();
    if (state.ws.selected) {
      const list = filteredTargets();
      const idx = list.indexOf(state.ws.selected);
      if (idx >= 0) selectTarget(idx);
    }
  }

  function discardCurrent() {
    const f = state.form;
    if (!f) return;
    unstage(draftKey(f));
    setStatus("Staged change discarded.", "");
  }

  async function saveAll() {
    const rows = stagedRows();
    if (!rows.length) { toast("Nothing staged to save", "ok"); return; }
    state.ws.busy = true;
    updateHeader();
    setStatus("Saving…");
    const token = state.token;
    try {
      const r = await api("/api/assignments/" + enc(state.key), {
        method: "POST", headers: { "Content-Type": "application/json" },
        // The coder is optional; a blank name is stored as the neutral "coder".
        body: JSON.stringify({ coder: readCoder(), assignments: rows }),
      });
      if (token !== state.token) return;
      state.ws.pending = {};
      const changes = (r.records || []).map((x) =>
        `${x.action} ${x.field || ""} ${x.old || ""}→${x.new || ""}`).join("; ");
      setStatus(`Saved ${r.count} target(s) · ${r.changes} dataset change(s). ${changes}`, "ok");
      toast("Assignments saved", "ok", `${r.changes} dataset change(s)`);
      // refresh applied list + targets + sidecar-derived variables
      await loadStudy(state.key, state.ws.focusSlot);
      if (token !== state.token) return;
      renderHost();
      // reload the Coverage counts when the host tab exposes a reload hook
      if (window.CSCoverage && typeof window.CSCoverage.reload === "function") {
        try { window.CSCoverage.reload(); } catch { /* ignore */ }
      }
    } catch (e) {
      if (token !== state.token) return;
      setStatus("Save failed: " + String(e.message || e), "err");
      toast("Save failed", "err", String(e.message || e));
    }
    state.ws.busy = false;
    updateHeader();
  }

  function markNA() {
    const f = state.form;
    if (!f) return;
    const reason = window.prompt("Reason for N/A (e.g. single-posttest design, composite only):", f.note || "");
    if (reason === null) return;
    f.role = "na";
    f.status = "confirmed";
    f.note = reason || "not applicable";
    renderFormPane();
    if (stageForm({ quiet: true, rerender: false })) setStatus("Staged N/A — press “Save changes” to write it.", "ok");
  }

  function addSplit() {
    const f = state.form;
    if (!f) return;
    const pid = ($("#split-pid") || {}).value || "pid";
    const t = ($("#split-true") || {}).value || "";
    const fl = ($("#split-false") || {}).value || "";
    const file = ($("#split-file") || {}).value || "";
    if (!t || !fl) { toast("Enter the true and false value lists", "err"); return; }
    f.slot_fix = "veracity_split";
    f.role = "";
    f.source_file = file;
    f.evidence = JSON.stringify({ pid_col: pid, true_values: t, false_values: fl });
    f.note = "split into true/false by item list";
    renderFormPane();
    if (stageForm({ quiet: true, rerender: false })) setStatus("Staged split — press “Save changes” to write it.", "ok");
  }

  // ---------------------------------------------------------------- wiring
  function attachHost() {
    const host = state.host;
    if (!host || host.__csAssignWired) return;
    host.__csAssignWired = true;
    host.addEventListener("change", (e) => {
      const s = e.target;
      if (!s) return;
      if (s.dataset && s.dataset.afield) {
        state.form = state.form || {};
        state.form[s.dataset.afield] = s.value;
        if (s.dataset.afield !== "status") decodePreview();
        stageForm({ quiet: true, rerender: false });
      }
    });
    let stageTimer = null;
    host.addEventListener("input", (e) => {
      const t = e.target;
      if (!t || !t.dataset) return;
      if (t.id === "assign-tsearch") {
        state.ws.search = t.value;
        renderTargetList();
        return;
      }
      if (!t.dataset.ainput) return;
      state.form = state.form || {};
      state.form[t.dataset.ainput] = t.value;
      if (t.dataset.ainput === "uniform_variable" || t.dataset.ainput === "note") decodePreview();
      // stage on a short debounce so a text field does not re-render per keystroke
      clearTimeout(stageTimer);
      stageTimer = setTimeout(() => stageForm({ quiet: true, rerender: false }), 300);
    });
    host.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      if (b.id === "assign-close") { close(); return; }
      if (b.id === "assign-save-all") { saveAll(); return; }
      if (b.id === "assign-stage") { stageForm(); return; }
      if (b.id === "assign-discard") { discardCurrent(); return; }
      if (b.dataset.unstage !== undefined) { unstage(b.dataset.unstage); return; }
      if (b.dataset.wsfilter) { state.ws.filter = b.dataset.wsfilter; state.ws.search = ""; renderHost(); return; }
      if (b.dataset.idx !== undefined) { selectTarget(parseInt(b.dataset.idx, 10)); return; }
      if (b.id === "assign-na") { markNA(); return; }
      if (b.id === "assign-split") { const sf = $("#assign-splitform"); if (sf) sf.hidden = !sf.hidden; return; }
      if (b.id === "split-add") { addSplit(); return; }
      if (b.id === "assign-open-coding") {
        const tab = document.querySelector('.viewtab[data-view="coding"]');
        if (tab) tab.click();
        window.dispatchEvent(new CustomEvent("cs:open-paper", { detail: { key: state.key } }));
        return;
      }
    });
  }

  function mount(el) {
    if (!el) return;
    state.host = el;
    if (!state.mounted) {
      state.mounted = true;
      state.coder = readCoder();
      window.addEventListener("cs:coder", (e) => { state.coder = String(e.detail || "").trim(); });
    }
    attachHost();
  }

  window.CSAssign = { open, close, mount };
})();
