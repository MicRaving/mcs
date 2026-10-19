/* ============================================================
   Coding Studio — Coverage triage tab
   Ranks every harmonized study by the poolability-weighted
   missingness of the 11 essential harmonization slots, shows a
   per-slot chip with counts (e.g. veracity 53/74), the readiness
   funnel and the assignment-audit counters, and click-throughs
   into the assignment workspace at the first open slot.

   Driven by GET /api/coverage.
   ============================================================ */
(() => {
  "use strict";

  const { $, $$, esc, api, toast } = window.CS;

  const SLOT_LABEL = {
    S1: "id", S2: "condition", S3: "arm roles", S4: "dv pairs",
    S5: "domain", S6: "veracity", S6cell: "veracity cell",
    S7: "timepoint", S8: "polarity", S9: "exclusions",
    S10: "covariates", S11: "provenance",
  };

  const state = {
    data: null, sort: "missingness_score", dir: "desc", filter: "all", q: "",
    started: false, loading: false,
  };

  const FILTER_LABEL = {
    all: "All",
    blocks_sdt: "Blocks SDT",
    blocks_did: "Blocks DiD",
    blocks_exclusions_on: "Blocks exclusions-on",
    needs_na_verdict: "Needs N/A verdict",
    has_dataset: "Has dataset",
    paper_only: "Paper only",
  };

  const rollupClass = (s) => {
    if (s === "filled" || s === "confirmed") return "ok";
    if (s === "missing") return "bad";
    if (s === "ambiguous") return "warn";
    if (s === "na") return "na";
    if (s === "partial") return "warn";
    return "na";
  };

  const resolvedCount = (counts) => {
    if (!counts) return 0;
    return (counts.filled || 0) + (counts.confirmed || 0) + (counts.na || 0);
  };
  const totalCount = (counts) => {
    if (!counts) return 0;
    return Object.values(counts).reduce((a, b) => a + (b || 0), 0);
  };

  // ---------------------------------------------------------------- loading
  async function load(force = false) {
    if (state.data && !force) return;
    state.loading = true;
    try {
      const qs = new URLSearchParams({ sort: state.sort, dir: state.dir, filter: state.filter });
      if (state.q) qs.set("q", state.q);
      state.data = await api("/api/coverage?" + qs.toString());
    } catch (e) {
      toast("Could not load coverage", "err", String(e.message || e));
      state.data = { studies: [], funnel: {}, counts: {}, audit: { checks: [], findings: [] } };
    }
    state.loading = false;
    render();
  }

  // ---------------------------------------------------------------- render
  function renderKpis() {
    const host = $("#cv-kpis");
    if (!host) return;
    const c = state.data.counts || {};
    const kpis = [
      ["Harmonized studies", c.has_dataset ?? "–", "studies with a wide table"],
      ["Block SDT", c.blocks_sdt ?? "–", "missing arm roles / veracity / dv pairs"],
      ["Block DiD", c.blocks_did ?? "–", "missing pid / condition / dv pairs"],
      ["Block exclusions-on", c.blocks_exclusions_on ?? "–", "flag_* columns but no rules"],
      ["Needs N/A verdict", c.needs_na_verdict ?? "–", "blank timepoint/veracity on a composite"],
    ];
    host.innerHTML = kpis.map(([label, val, sub]) =>
      `<div class="cv-kpi"><span class="cv-kpi-v">${esc(val)}</span>` +
      `<span class="cv-kpi-l">${esc(label)}</span><span class="cv-kpi-s">${esc(sub)}</span></div>`
    ).join("");
  }

  function renderFunnel() {
    const host = $("#cv-funnel");
    if (!host) return;
    const f = state.data.funnel || {};
    host.innerHTML =
      `<span class="cv-funnel-t">Readiness funnel</span>` +
      `<span class="cv-step"><b>${esc(f.true_false_cell ?? "–")}</b> true+false cell</span>` +
      `<span class="cv-arrow">→</span>` +
      `<span class="cv-step"><b>${esc(f.with_condition ?? "–")}</b> + condition column</span>` +
      `<span class="cv-arrow">→</span>` +
      `<span class="cv-step"><b>${esc(f.explicit_roles ?? "–")}</b> + explicit roles</span>` +
      `<span class="cv-grow"></span>` +
      `<span class="cv-step muted">${esc(f.harmonized ?? "–")} harmonized · ` +
      `${esc(f.papers ?? "–")} papers</span>`;
  }

  function renderAudit() {
    const host = $("#cv-audit");
    if (!host) return;
    const a = state.data.audit || {};
    const parity = a.parity || { ok: 0, bad: 0 };
    const parts = [
      `<span class="cv-audit-t">Assignment audit</span>`,
      `<span class="cv-audit-chip ${parity.bad ? "bad" : "ok"}">parity ${parity.ok}/${parity.ok + parity.bad}</span>`,
      `<span class="cv-audit-chip ${a.conflicts ? "bad" : "ok"}">conflicts ${a.conflicts || 0}</span>`,
      `<span class="cv-audit-chip ${a.needs_veracity ? "warn" : "ok"}">needs veracity ${a.needs_veracity || 0}</span>`,
    ];
    const findings = (a.findings || []);
    if (findings.length) {
      parts.push(`<details class="cv-findings"><summary>${findings.length} non-parity finding(s)</summary>`);
      parts.push(`<div class="cv-findlist">` + findings.map((f) =>
        `<button type="button" class="cv-finding" data-key="${esc(f.study_key)}" data-slot="">` +
        `<b>${esc(f.study_key)}</b><span class="cv-findchk">${esc(f.check)}</span>` +
        `<span class="cv-finddet">${esc(f.detail || "")}</span></button>`).join("") +
        `</div></details>`);
    }
    host.innerHTML = parts.join("");
    $$("#cv-audit .cv-finding").forEach((b) =>
      b.addEventListener("click", () => openAssignment(b.dataset.key, b.dataset.slot)));
  }

  function renderFilters() {
    const host = $("#cv-filters");
    if (!host) return;
    const filters = state.data.filters || Object.keys(FILTER_LABEL);
    host.innerHTML = filters.map((f) =>
      `<button class="chip${f === state.filter ? " active" : ""}" data-cfilter="${esc(f)}">` +
      `${esc(FILTER_LABEL[f] || f)}</button>`).join("");
    $$("#cv-filters .chip").forEach((c) => c.addEventListener("click", () => {
      state.filter = c.dataset.cfilter;
      load(true);
    }));
  }

  function slotChip(study, slot) {
    const status = study[slot] || "";
    const counts = (study.slot_counts || {})[slot] || {};
    const tot = totalCount(counts);
    const res = resolvedCount(counts);
    const miss = study[slot + "_missing"] || 0;
    let cnt = "";
    if (tot > 1) cnt = `${res}/${tot}`;
    else if (slot === "S6cell" || tot === 1) cnt = status === "filled" ? "✓" : (status === "na" ? "n/a" : "–");
    const cls = rollupClass(status);
    const title = `${SLOT_LABEL[slot] || slot}: ${status || "n/a"}` +
      (miss ? ` · ${miss} missing` : "") + (tot ? ` · ${res}/${tot} resolved` : "");
    return `<button type="button" class="cv-slot ${cls}" data-key="${esc(study.study_key)}" ` +
      `data-slot="${esc(slot)}" title="${esc(title)}">` +
      `<span class="cv-slot-s">${esc(slot)}</span>` +
      `<span class="cv-slot-c">${esc(cnt)}</span></button>`;
  }

  function renderTable() {
    const host = $("#cv-table");
    if (!host) return;
    const studies = state.data.studies || [];
    if (!studies.length) {
      host.innerHTML = `<div class="empty"><b>No studies match</b><p>Try another filter or search.</p></div>`;
      return;
    }
    const sortBtn = (key, label, cls = "") => {
      const active = state.sort === key ? " active" : "";
      const arrow = state.sort === key ? (state.dir === "desc" ? " ↓" : " ↑") : "";
      return `<button class="cv-th${cls}${active}" data-sort="${esc(key)}">${esc(label)}${arrow}</button>`;
    };
    const head = `<div class="cv-row cv-head">` +
      `<span class="cv-study">${sortBtn("study_key", "Study")}</span>` +
      `<span class="cv-score">${sortBtn("missingness_score", "Score")}</span>` +
      `<span class="cv-flags">Flags</span>` +
      `<span class="cv-slots">` +
      ["S1", "S2", "S3", "S4", "S5", "S6", "S6cell", "S7", "S8", "S9", "S10", "S11"]
        .map((s) => `<span class="cv-slothead" title="${esc(SLOT_LABEL[s])}">${esc(s)}</span>`).join("") +
      `</span><span class="cv-open"></span></div>`;
    const rows = studies.map((s) => {
      const flags = [
        s.blocks_sdt ? `<span class="cv-flag bad" title="blocks SDT">SDT</span>` : "",
        s.blocks_did ? `<span class="cv-flag bad" title="blocks DiD">DiD</span>` : "",
        s.blocks_exclusions_on ? `<span class="cv-flag warn" title="blocks exclusions-on">Excl</span>` : "",
        s.needs_na_verdict ? `<span class="cv-flag warn" title="needs a coder N/A verdict">N/A?</span>` : "",
      ].join("");
      const score = s.missingness_score == null ? "–" : s.missingness_score;
      const first = (s.first_open_slot || {}).slot || "";
      return `<div class="cv-row${s.has_dataset ? "" : " nodata"}">` +
        `<span class="cv-study"><button class="cv-link" data-key="${esc(s.study_key)}" data-slot="${esc(first)}">` +
        `${esc(s.study_key)}</button>${s.has_dataset ? "" : ' <span class="cv-tag">paper only</span>'}</span>` +
        `<span class="cv-score">${esc(score)}</span>` +
        `<span class="cv-flags">${flags}</span>` +
        `<span class="cv-slots">${["S1", "S2", "S3", "S4", "S5", "S6", "S6cell", "S7", "S8", "S9", "S10", "S11"].map((sl) => slotChip(s, sl)).join("")}</span>` +
        `<span class="cv-open"><button class="btn ghost cv-openbtn" data-key="${esc(s.study_key)}" data-slot="${esc(first)}">Open</button></span>` +
        `</div>`;
    }).join("");
    host.innerHTML = head + rows;

    $$("#cv-table [data-sort]").forEach((b) => b.addEventListener("click", () => {
      const key = b.dataset.sort;
      if (state.sort === key) {
        state.dir = state.dir === "desc" ? "asc" : "desc";  // second click flips
      } else {
        state.sort = key;
        state.dir = ["missingness_score", "blocks_sdt", "blocks_did"].includes(key) ? "desc" : "asc";
      }
      load(true);
    }));
    $$("#cv-table .cv-link, #cv-table .cv-openbtn").forEach((b) =>
      b.addEventListener("click", () => openAssignment(b.dataset.key, b.dataset.slot)));
    $$("#cv-table .cv-slot").forEach((b) =>
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        openAssignment(b.dataset.key, b.dataset.slot);
      }));
  }

  function render() {
    if (!state.data) return;
    renderKpis();
    renderFunnel();
    renderAudit();
    renderFilters();
    renderTable();
    const note = $("#cv-note");
    if (note) {
      note.textContent = `${state.data.counts?.total ?? 0} study rows · coverage ` +
        `generated ${state.data.generated_at || "–"} · click a study or a slot chip to assign ` +
        `the missing variable with raw context`;
    }
  }

  function openAssignment(key, slot) {
    if (!key) return;
    // The workspace lives inside the Coverage view — open it inline and keep the
    // table (filter/sort/scroll state) exactly as it is.
    if (window.CSAssign) window.CSAssign.open(key, slot || "");
  }

  // ---------------------------------------------------------------- wiring
  function init() {
    if (state.started) return;
    state.started = true;
    if (window.CSAssign) window.CSAssign.mount($("#cv-assign-host"));
    const search = $("#cv-search");
    if (search) {
      let t = null;
      search.addEventListener("input", () => {
        clearTimeout(t);
        t = setTimeout(() => { state.q = search.value.trim(); load(true); }, 200);
      });
    }
    document.addEventListener("cs:view", (e) => {
      const view = $("#view-coverage");
      if (!view) return;
      const on = e.detail === "coverage";
      view.hidden = !on;
      document.body.classList.toggle("view-coverage", on);
      if (on) load();
    });
    // own tab click (analysis.js's showView fires cs:view which shows us)
    const tab = document.querySelector('.viewtab[data-view="coverage"]');
    if (tab) tab.addEventListener("click", () => { const v = $("#view-coverage"); if (v) v.hidden = false; load(); });
    let saved = "coding";
    try { saved = localStorage.getItem("cs.view") || "coding"; } catch { /* ignore */ }
    if (saved === "coverage") { const v = $("#view-coverage"); if (v) v.hidden = false; load(); }
  }

  // Let the assignment workspace refresh the coverage counts after a save.
  window.CSCoverage = { reload: () => load(true) };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
