/* ============================================================
   Coding Studio — Reproduction tab (reproduction dossier)
   The per-study dossier screen: file inventory · contrast-level
   original | reproduced | Δ | without-exclusions · per-rule exclusion
   provenance · run card + log · preparations/failures/gaps · the
   rendered Reproduction report.

   Endpoints: /api/repro/overview,
              /api/repro/study/<key>/dossier,
              /api/repro/dossier/files/<key>,
              /api/repro/dossier/log/<key>,
              /api/repro/dossier/edit,
              /api/repro/run, /api/repro/run/status.
   ============================================================ */
(() => {
  "use strict";

  const { $, $$, esc, api, toast } = window.CS;

  const S = {
    started: false,
    overview: null,
    filter: "all",
    q: "",
    sortKey: "study_key",
    sortDir: 1,
    study: "",
    dossier: null,
    files: [],
    filesKey: "",
    log: null,
    dtab: "results",
    polling: null,
    runActive: false,
    varq: "",
    fileq: "",
    expanded: {},
    reportOpen: false,
  };

  const STATUS = {
    reproduced:       { label: "Reproduced",      cls: "ok" },
    partial:          { label: "Partial",         cls: "warn" },
    not_reproduced:   { label: "Not reproduced",  cls: "danger" },
    reported_only:    { label: "Reported only",   cls: "" },
    not_reproducible: { label: "Not reproducible", cls: "" },
    crashed:          { label: "Crashed",         cls: "danger" },
    not_run:          { label: "Not run",         cls: "" },
    unknown:          { label: "Unknown",         cls: "" },
  };
  const FILTERS = ["all", "reproduced", "partial", "not_reproduced",
                   "reported_only", "not_reproducible", "crashed", "not_run"];
  const KIND_LABEL = { generate: "Generating scripts…", harmonizers: "Running harmonizers…",
                       reproducers: "Running reproducers…" };
  const DTABS = [
    ["results", "Results"],
    ["files", "File inventory"],
    ["exclusions", "Exclusion provenance"],
    ["run", "Run & evidence"],
    ["notes", "Preparations & gaps"],
  ];
  const VERDICT_OPTIONS = ["", "REPRODUCED", "reproduced in direction", "PARTIAL",
                           "NOT REPRODUCED", "NOT COMPARABLE", "NOT CHECKABLE"];

  const statusMeta = (st) => STATUS[st] || { label: st || "–", cls: "" };
  const statusPill = (st) => {
    const m = statusMeta(st);
    return `<span class="pill ${m.cls}">${esc(m.label)}</span>`;
  };
  const fwChip = (on) => on
    ? `<span class="fw-chip" title="fully-within (single-arm, no control); excluded from the pooled analysis by default">FW</span>`
    : "";
  const staleChip = (on, title) => on
    ? `<span class="rp-stale" title="${esc(title || "the dossier is older than the newest dataset file")}">stale</span>`
    : "";
  const fmtInt = (v) => (v === null || v === undefined || v === "") ? "–" : Number(v).toLocaleString();
  const fmtStamp = (v) => {
    if (!v) return "–";
    const d = new Date(v);
    return isNaN(d.getTime()) ? String(v) : d.toLocaleString();
  };
  function fmtNum(v, d = 4) {
    if (v === null || v === undefined || v === "") return "–";
    const n = Number(v);
    if (!isFinite(n)) return String(v);
    if (n !== 0 && Math.abs(n) < 1e-4) return n.toExponential(2);
    if (Math.abs(n) >= 10000) return n.toFixed(0);
    return String(parseFloat(n.toFixed(d)));
  }
  const signNum = (v, d = 4) => (v === null || v === undefined || v === "" ? "–"
    : (Number(v) > 0 ? "+" : "") + fmtNum(v, d));
  function fmtBytes(n) {
    if (n === null || n === undefined || n === "") return "–";
    n = Number(n);
    if (!isFinite(n)) return "–";
    const u = ["B", "KB", "MB", "GB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n.toFixed(0)) + " " + u[i];
  }
  const shorten = (s, n) => {
    s = String(s || "");
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  };
  function jparse(s, fb) {
    try { return s ? JSON.parse(s) : fb; } catch { return fb; }
  }
  function estLabel(m) {
    const s = String(m || "");
    if (/d['\u2032]|dprime|discrimination/i.test(s)) return "d′ (treatment − control)";
    return s;
  }

  function verdictPill(v) {
    const raw = String(v || "");
    const s = raw.toUpperCase();
    let cls = "", label = raw || "–";
    if (s === "REPRODUCED") cls = "ok";
    else if (s.includes("DIRECTION") || s.includes("PARTIAL")) cls = "warn";
    else if (s.includes("NOT REPRODUCED")) cls = "danger";
    return `<span class="pill ${cls}">${esc(label)}</span>`;
  }

  // ---------------------------------------------------------------- run
  function setRunning(on, title) {
    S.runActive = !!on;
    const pill = $("#rp-status-pill");
    if (pill) {
      pill.className = "pill " + (on ? "accent" : "");
      pill.textContent = on ? (title || "Running…") : "Idle";
    }
    $$("#rp-runbar .btn").forEach((b) => { b.disabled = on; });
    if (on) {
      const log = $("#rp-runlog");
      if (log) log.hidden = false;
      const t = $("#rp-runlog-title");
      if (t) t.textContent = title || "Run";
    }
  }

  async function pollRun() {
    let s;
    try { s = await api("/api/repro/run/status"); } catch { return; }
    const body = $("#rp-runlog-body");
    if (body) {
      body.textContent = s.log || "(starting…)";
      body.scrollTop = body.scrollHeight;
    }
    if (s.finished || !s.running) {
      if (S.polling) { clearInterval(S.polling); S.polling = null; }
      setRunning(false);
      const t = $("#rp-runlog-title");
      if (t) t.textContent = s.ok === false ? "Run failed" : "Run complete";
      const pill = $("#rp-status-pill");
      if (pill) {
        pill.className = "pill " + (s.ok === false ? "danger" : "ok");
        pill.textContent = s.ok === false ? "Run failed" : "Run complete";
      }
      toast(s.ok === false ? "Run failed" : "Run complete",
            s.ok === false ? "err" : "ok",
            s.error || (s.kind ? String(s.kind) : ""));
      try { await loadOverview(); } catch { /* keep the last view */ }
      if (S.study) openStudy(S.study, true);
      return;
    }
  }

  async function startRun(kind) {
    const onlyEl = $("#rp-only");
    const only = onlyEl ? onlyEl.value.trim() : "";
    let res;
    try {
      res = await api("/api/repro/run", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind, only }),
      });
    } catch (e) {
      toast("Could not start the run", "err", String(e.message || e));
      return;
    }
    if (res && res.ok === false) { toast("Could not start the run", "err", res.error || ""); return; }
    setRunning(true, KIND_LABEL[kind] || "Running…");
    const body = $("#rp-runlog-body");
    if (body) body.textContent = "starting…";
    S.polling = setInterval(pollRun, 1500);
    pollRun();
  }

  // ---------------------------------------------------------------- overview
  function renderKpis() {
    const c = (S.overview && S.overview.counts) || {};
    const dj = (S.overview && S.overview.dossier) || {};
    const v = dj.validation || {};
    const kpis = [
      ["Studies", c.studies, "in the reproduction campaign"],
      ["Dossiers", c.dossiers, "per-study dossier JSON"],
      ["Reproduced", c.reproduced, "every checked contrast"],
      ["Partial", c.partial, "some contrasts failed"],
      ["Not reproduced", c.not_reproduced, "data present, checks failed"],
      ["Not reproducible", c.not_reproducible, "no participant data"],
      ["Durable logs", c.logs, "Results/repro_logs/"],
      ["Validator", v.n_problems, (v.n_error || 0) + " error · " + (v.n_warning || 0) + " warning"],
    ];
    const host = $("#rp-kpis");
    if (!host) return;
    host.innerHTML = kpis.map(([k, val, s]) =>
      `<div class="kpi"><div class="k">${esc(k)}</div><div class="v">${esc(fmtInt(val))}</div>` +
      `<div class="s">${esc(s)}</div></div>`).join("");
  }

  function renderFilters() {
    const c = (S.overview && S.overview.counts) || {};
    const n = (f) => f === "all" ? (c.studies || 0) : (c[f] || 0);
    const host = $("#rp-filters");
    if (!host) return;
    host.innerHTML = FILTERS.map((f) =>
      `<button class="chip${S.filter === f ? " active" : ""}" data-rfilter="${f}">` +
      `${esc(f === "all" ? "All" : statusMeta(f).label)} <span class="cnt">${n(f)}</span></button>`).join("");
    $$("#rp-filters .chip").forEach((b) => b.addEventListener("click", () => {
      S.filter = b.dataset.rfilter;
      renderFilters();
      renderTable();
    }));
  }

  function filtered() {
    const rows = (S.overview && S.overview.studies) || [];
    const q = S.q.toLowerCase();
    return rows.filter((r) => {
      if (S.filter !== "all" && r.status !== S.filter) return false;
      if (q && !String(r.study_key).toLowerCase().includes(q)) return false;
      return true;
    });
  }

  function sortRows(rows) {
    const k = S.sortKey, dir = S.sortDir;
    return rows.slice().sort((a, b) => {
      let x = a[k], y = b[k];
      if (typeof x === "boolean" || typeof y === "boolean") { x = x ? 1 : 0; y = y ? 1 : 0; }
      if (x === null || x === undefined) x = "";
      if (y === null || y === undefined) y = "";
      if (typeof x === "number" && typeof y === "number") return (x - y) * dir;
      return String(x).toLowerCase().localeCompare(String(y).toLowerCase()) * dir
             || String(a.study_key).localeCompare(String(b.study_key));
    });
  }

  function fileCounts(r) {
    const seg = (n, cls, t) =>
      `<span class="rp-fc ${cls}" title="${esc(t)}">${esc(fmtInt(n))}</span>`;
    return `<span class="rp-fcs">${seg(r.n_files_raw, "raw", "raw data files")}` +
      `${seg(r.n_files_scripts, "scr", "scripts (harmonizer / reproduction / prepare)")}` +
      `${seg(r.n_files_products, "prd", "derived products + sidecars")}</span>`;
  }

  function renderTable() {
    const rows = sortRows(filtered());
    const host = $("#rp-table");
    if (!host) return;
    const th = (key, label, num) =>
      `<th class="rp-sort${num ? " num" : ""}" data-sort="${key}" ` +
      `title="Sort by ${esc(label)}">${esc(label)}${S.sortKey === key ? (S.sortDir > 0 ? " \u25b4" : " \u25be") : ""}</th>`;
    const body = rows.map((r) => {
      const st = statusPill(r.status);
      const sel = r.study_key === S.study ? " class=\"sel\"" : "";
      const log = r.log_present
        ? `<span class="rp-flag on" title="durable run log present">✓</span>`
        : `<span class="rp-flag" title="no durable run log">–</span>`;
      return `<tr data-key="${esc(r.study_key)}"${sel}>` +
        `<td class="rp-key">${esc(r.study_key)}${fwChip(r.fully_within)}</td>` +
        `<td>${st}${r.dossier_present ? "" : ` <span class="rp-muted" title="no dossier JSON — a v0 is synthesized on demand">no dossier</span>`}</td>` +
        `<td class="rp-files">${fileCounts(r)}</td>` +
        `<td class="num">${esc(fmtInt(r.n_contrasts_dossier))}</td>` +
        `<td class="num">${esc(fmtInt(r.n_reproduced_dossier))}</td>` +
        `<td class="num">${esc(fmtInt(r.n_gaps_dossier))}</td>` +
        `<td class="rp-log">${log}</td>` +
        `<td class="rp-when">${esc(fmtStamp(r.last_run))}</td>` +
        `</tr>`;
    }).join("");
    host.innerHTML =
      `<table class="an-table rp-table"><thead><tr>` +
      th("study_key", "Study") +
      th("status", "Reproduction") +
      `<th title="raw / scripts / products">Files <span class="rp-fcs"><span class="rp-fc raw">R</span><span class="rp-fc scr">S</span><span class="rp-fc prd">P</span></span></th>` +
      th("n_contrasts_dossier", "k", true) +
      th("n_reproduced_dossier", "Repro.", true) +
      th("n_gaps_dossier", "Gaps", true) +
      `<th title="durable run log present">Log</th>` +
      th("last_run", "Last run") +
      `</tr></thead><tbody>${body}</tbody></table>`;
    $$("#rp-table th.rp-sort").forEach((h) => h.addEventListener("click", () => {
      const k = h.dataset.sort;
      if (S.sortKey === k) S.sortDir = -S.sortDir;
      else { S.sortKey = k; S.sortDir = 1; }
      renderTable();
    }));
    $$("#rp-table tbody tr").forEach((tr) => tr.addEventListener("click", () => {
      openStudy(tr.dataset.key);
    }));
    const note = $("#rp-note");
    if (note) note.textContent = `${rows.length} of ${((S.overview && S.overview.studies) || []).length} studies` +
      (S.filter !== "all" ? ` · filter: ${statusMeta(S.filter).label}` : "") +
      ` · click a row for its reproduction dossier`;
  }

  // ---------------------------------------------------------------- detail
  function renderDetail() {
    const d = S.dossier;
    const empty = $("#rp-detail-empty");
    const body = $("#rp-detail-body");
    if (!empty || !body) return;
    if (!d) { empty.hidden = false; body.hidden = true; return; }
    empty.hidden = true;
    body.hidden = false;

    const st = (d.staleness || {});
    const val = (d.validation || {});
    const valRows = d.validation_rows || [];
    const head =
      `<div class="rp-detail-head">` +
      `<div class="rp-detail-title"><h2>${esc(d.study_key)}</h2>${fwChip(d.fully_within)}` +
      `${staleChip(st.stale, "dossier generated " + (st.generated_at || "?") + " but " + (st.newest_source || "a dataset file") + " changed " + (st.newest_source_mtime || ""))}` +
      `</div>` +
      `<div class="rp-detail-pills">${statusPill(d.status)}` +
      `<span class="pill" title="machine status from the reproducer">machine: ${esc(d.machine_status || "–")}</span>` +
      `<span class="pill" title="documented status parsed from the report">documented: ${esc(d.documented_status || "–")}</span>` +
      (d.synthesized ? `<span class="pill warn" title="${esc(d.note || "")}">synthesized v0</span>` : "") +
      (val.n_problems ? `<span class="pill danger" title="validate_repro_dossier rows for this study">validation: ${val.n_problems}</span>`
                      : `<span class="pill ok" title="validate_repro_dossier has no problems">validation ✓</span>`) +
      `<span class="pill">generated ${esc(fmtStamp(d.generated_at))}</span>` +
      `</div></div>`;

    const tabs =
      `<div class="an-tabbar">` +
      `<div class="seg" id="rp-dtabs">` +
      DTABS.map(([t, label]) =>
        `<button class="${S.dtab === t ? "active" : ""}" data-rdtab="${t}">${esc(label)}` +
        (t === "results" ? ` <span class="cnt">${(d.results || []).length}</span>` : "") +
        (t === "files" ? ` <span class="cnt">${(S.files || d.file_inventory || []).length}</span>` : "") +
        `</button>`).join("") +
      `</div><span class="grow"></span>` +
      `<button class="btn ghost" id="rp-report-btn"${d.report_available ? "" : " disabled"}>` +
      `Report${d.report_available ? " ▸" : " — none"}</button>` +
      `</div>`;

    let panel;
    if (S.dtab === "results") panel = resultsPanel(d);
    else if (S.dtab === "files") panel = filesPanel(d);
    else if (S.dtab === "exclusions") panel = exclusionsPanel(d);
    else if (S.dtab === "run") panel = runPanel(d);
    else panel = notesPanel(d);

    const drawer =
      `<div class="rp-drawer" id="rp-drawer"${S.reportOpen ? "" : " hidden"}>` +
      `<div class="rp-drawer-backdrop" data-rpclose="1"></div>` +
      `<div class="rp-drawer-panel">` +
      `<div class="rp-drawer-head"><span>Reproduction report — <span class="mono">${esc(d.study_key)}</span></span>` +
      `<span class="grow"></span>` +
      `<span class="rp-muted mono">${esc(d.report_path || "")}</span>` +
      `<button class="btn ghost icon" data-rpclose="1" title="Close">✕</button></div>` +
      `<div class="rp-drawer-body md">${renderMarkdown(d.report_markdown || "")}</div>` +
      `</div></div>`;

    body.innerHTML = head + `<div class="an-card rp-detail-card">${tabs}${panel}</div>` + drawer;
    $$("#rp-dtabs button").forEach((b) => b.addEventListener("click", () => {
      S.dtab = b.dataset.rdtab;
      renderDetail();
    }));
    const rb = $("#rp-report-btn");
    if (rb) rb.addEventListener("click", () => { S.reportOpen = true; renderDetail(); });
    $$("[data-rpclose]").forEach((el) => el.addEventListener("click", () => {
      S.reportOpen = false; renderDetail();
    }));
    wireDetail();
  }

  function rowMeta(r) {
    const chips = [];
    if (r.study_id) chips.push(`<span class="rp-tag">${esc(r.study_id)}</span>`);
    if (r.dv_domain) chips.push(`<span class="rp-tag dom">${esc(r.dv_domain)}</span>`);
    if (r.contrast_type !== null && r.contrast_type !== undefined && r.contrast_type !== "")
      chips.push(`<span class="rp-tag">ct ${esc(r.contrast_type)}</span>`);
    if (Number(r.eligible_primary) === 1) chips.push(`<span class="rp-tag elig">primary</span>`);
    return chips.join("");
  }

  function renderOriginal(r) {
    if (r.original_value === null || r.original_value === undefined) {
      return `<span class="rp-muted">no coded original</span>`;
    }
    let ci = "";
    if (r.original_ci_low != null && r.original_ci_high != null)
      ci = `<div class="rp-muted">95% CI [${fmtNum(r.original_ci_low)}, ${fmtNum(r.original_ci_high)}]</div>`;
    const p = (r.original_p != null) ? `<div class="rp-muted">p = ${fmtNum(r.original_p)}</div>` : "";
    const n = (r.original_n1 != null || r.original_n2 != null)
      ? `<div class="rp-muted">n ${fmtInt(r.original_n1)} / ${fmtInt(r.original_n2)}</div>` : "";
    const loc = r.source_location
      ? `<div class="rp-src">${esc(r.source_location)}</div>`
      : `<div class="rp-src rp-missing">source location missing</div>`;
    const quote = r.source_quote
      ? `<div class="rp-quote">“${esc(r.source_quote)}”</div>` : "";
    return `<div class="rp-val">${fmtNum(r.original_value)} ` +
      `<span class="rp-metric-tag">${esc(r.original_metric || "")}</span></div>` +
      ci + p + n + loc + quote;
  }

  function renderReproduced(r) {
    const v = r.reproduced_value;
    const est = r.reproduced_estimand || r.reproduced_metric || "";
    const main = (v === null || v === undefined)
      ? `<div class="rp-muted">not reproduced numerically</div>`
      : `<div class="rp-val">${fmtNum(v)}</div>`;
    const estChip = est ? `<div class="rp-est">${esc(estLabel(est))}</div>` : "";
    const n = r.reproduced_n_used != null
      ? `<div class="rp-muted">n used ${fmtInt(r.reproduced_n_used)}</div>` : "";
    const dr = r.domain_recalc_d != null
      ? `<div class="rp-domrecalc" title="${esc(r.domain_recalc_metric || "domain-level mean difference, not the coded effect")}">` +
        `domain mean-diff ${fmtNum(r.domain_recalc_d)}` +
        (r.domain_recalc_n != null ? ` (n ${fmtInt(r.domain_recalc_n)})` : "") + `</div>`
      : "";
    return main + estChip + n + dr;
  }

  function renderDelta(r, gaps) {
    if (r.delta_abs === null || r.delta_abs === undefined) {
      if ((gaps || []).includes("different_estimand_not_differenced"))
        return `<span class="rp-muted" title="the reproduced value is a different estimand and is not differenced against the coded effect">not differenced</span>`;
      return `<span class="rp-muted">–</span>`;
    }
    const rel = (r.delta_rel === null || r.delta_rel === undefined) ? ""
      : `<div class="rp-muted">${(r.delta_rel * 100).toFixed(1)}%</div>`;
    const cls = Math.abs(r.delta_abs) < 1e-9 ? "rp-zero"
      : (r.delta_abs > 0 ? "rp-pos" : "rp-neg");
    return `<span class="${cls}">${signNum(r.delta_abs)}</span>${rel}`;
  }

  function renderWoe(r) {
    const est = String(r.without_exclusions_estimand || "");
    const on = r.woe_d_on, off = r.woe_d_off;
    if (on == null && off == null) {
      return `<div class="rp-muted">${esc(est || "not computed")}</div>`;
    }
    const nOn = `${fmtInt(r.woe_n_treat_on)}/${fmtInt(r.woe_n_control_on)}`;
    const nOff = `${fmtInt(r.woe_n_treat_off)}/${fmtInt(r.woe_n_control_off)}`;
    return `<div class="rp-est">${esc(estLabel(est))}</div>` +
      `<div class="rp-line"><span class="rp-k">ON</span> ${fmtNum(on)} ` +
      `<span class="rp-arrow">→</span> <span class="rp-k">OFF</span> ${fmtNum(off)}</div>` +
      `<div class="rp-line">Δ ${signNum(r.woe_delta)} ` +
      `<span class="rp-muted">n ${nOn} → ${nOff}</span></div>`;
  }

  function renderVerdict(r) {
    if (r.verdict_override) {
      return `<div>${verdictPill(r.verdict_override)} <span class="rp-coder">coder</span></div>` +
        `<div class="rp-muted">machine: ${esc(r.verdict || "–")}</div>` +
        (r.verdict_override_reason ? `<div class="rp-muted">${esc(r.verdict_override_reason)}</div>` : "");
    }
    return verdictPill(r.verdict) +
      (r.verdict_basis ? `<div class="rp-muted">${esc(r.verdict_basis)}</div>` : "");
  }

  function renderHow(r, how, ex) {
    const est = how.estimator || r.reproduced_estimand || r.reproduced_metric || "";
    const rules = ex.rules || [];
    let excl;
    if (!rules.length) {
      excl = `<div class="rp-muted">${esc(ex.note || "no exclusion rules")}</div>`;
    } else {
      const applied = rules.filter((x) => x.gates_this_domain);
      excl = applied.map((x) => `<code class="rp-code">${esc(x.flag)}${esc(x.keep || "")}</code>`).join(" ") +
        `<div class="rp-muted">${applied.length}/${rules.length} rules gate this domain</div>`;
    }
    const src = how.reproduced
      ? `<div class="rp-muted" title="${esc(how.reproduced)}">src: ${esc(shorten(how.reproduced, 46))}</div>` : "";
    return `<div class="rp-est">${esc(estLabel(est))}</div>` + excl + src;
  }

  function resultsPanel(d) {
    const rows = d.results || [];
    const q = S.varq.toLowerCase();
    const shown = rows.filter((r) => !q ||
      String(r.dv_name).toLowerCase().includes(q) ||
      String(r.dv_domain).toLowerCase().includes(q) ||
      String(r.verdict).toLowerCase().includes(q) ||
      String(r.contrast_id).toLowerCase().includes(q));
    const body = shown.map((r, i) => {
      const gaps = jparse(r.gaps_json, []);
      const how = jparse(r.how, {});
      const ex = jparse(r.exclusions_applied_json, {});
      const cid = String(r.contrast_id || "");
      const idx = rows.indexOf(r);
      const open = !!S.expanded[cid];
      const gapBadge = gaps.length
        ? `<span class="rp-gapcount" title="${esc(gaps.join(", "))}">${gaps.length} gap${gaps.length > 1 ? "s" : ""}</span>` : "";
      return `<tr class="rp-res${open ? " open" : ""}" data-idx="${idx}" data-cid="${esc(cid)}">` +
        `<td class="rp-contrast"><div class="rp-dvname">${esc(r.dv_name || "(orphan reproduced row)")}</div>` +
        `<div class="rp-meta">${rowMeta(r)}${gapBadge}</div></td>` +
        `<td class="rp-metric mono">${esc(r.original_metric || "–")}</td>` +
        `<td class="rp-orig">${renderOriginal(r)}</td>` +
        `<td class="rp-repro">${renderReproduced(r)}</td>` +
        `<td class="rp-delta">${renderDelta(r, gaps)}</td>` +
        `<td class="rp-woe">${renderWoe(r)}</td>` +
        `<td class="rp-verdict">${renderVerdict(r)}</td>` +
        `<td class="rp-how">${renderHow(r, how, ex)}</td>` +
        `<td class="rp-caret">${open ? "▾" : "▸"}</td>` +
        `</tr>` +
        `<tr class="rp-exp"${open ? "" : " hidden"}><td colspan="9">${expandedRow(r, gaps, how, ex)}</td></tr>`;
    }).join("");
    return `<div class="an-panel">` +
      `<div class="rp-mini-kpis">` +
      `<span class="pill">contrasts ${fmtInt(rows.length)}</span>` +
      `<span class="pill ok">reproduced ${fmtInt((d.index || {}).n_reproduced)}</span>` +
      `<span class="pill danger">not reproduced ${fmtInt((d.index || {}).n_not_reproduced)}</span>` +
      `<span class="pill warn">gaps ${fmtInt((d.index || {}).n_gap_rows)}</span>` +
      `<input class="input rp-varsearch" id="rp-varsearch" placeholder="Filter contrasts…" value="${esc(S.varq)}">` +
      `</div>` +
      `<div class="an-tablewrap rp-reswrap"><table class="an-table rp-res-table"><thead><tr>` +
      `<th>Contrast</th><th>Metric</th><th>Original (paper)</th><th>Reproduced</th>` +
      `<th>Δ</th><th>W/o exclusions</th><th>Verdict</th><th>How</th><th></th>` +
      `</tr></thead><tbody>${body || `<tr><td colspan="9" class="rp-none">No coded contrast for this study.</td></tr>`}</tbody>` +
      `</table></div>` +
      `<p class="an-note">Original = the coded paper statistic; Reproduced = the recomputed value with its own ` +
      `estimand label; <b>domain mean-diff</b> is a domain-level mean(true)−mean(false) recalculation and is ` +
      `NOT the coded effect; Δ is only shown when the two sides are the same estimand. ` +
      `W/o exclusions is the intervention-vs-control d′ ON→OFF.</p>` +
      `</div>`;
  }

  function expandedRow(r, gaps, how, ex) {
    const cid = String(r.contrast_id || "");
    const rules = ex.rules || [];
    const applied = rules.filter((x) => x.gates_this_domain);
    const ruleLines = rules.length
      ? applied.map((x) => `<li><code class="rp-code">${esc(x.flag)}${esc(x.keep || "")}</code> ` +
          `applies_to ${esc((x.applies_to || []).join(", "))} ${x.gates_this_domain ? "" : "(not this domain)"}</li>`).join("")
      : `<li class="rp-muted">${esc(ex.note || "no exclusion rules")}</li>`;
    const gapChips = gaps.length
      ? gaps.map((g) => `<span class="rp-gap">${esc(g)}</span>`).join(" ")
      : `<span class="rp-muted">no gap recorded</span>`;
    const locVal = esc(r.source_location || "");
    const quoteVal = esc(r.source_quote || "");
    const overrideOpts = VERDICT_OPTIONS.map((v) =>
      `<option value="${esc(v)}"${String(r.verdict_override || "") === v ? " selected" : ""}>${esc(v || "— no override —")}</option>`).join("");
    return `<div class="rp-expand">` +
      `<div class="rp-expand-cols">` +
      `<div class="rp-expand-col">` +
      `<h4>Provenance</h4>` +
      `<div class="rp-kv"><span>extraction</span><code>${esc(r.extraction_ref || "–")}</code></div>` +
      `<div class="rp-kv"><span>reproduced source</span><code>${esc(r.reproduced_source || "–")}</code></div>` +
      `<div class="rp-kv"><span>without-exclusions</span><code>${esc(how.without_exclusions || "–")}</code></div>` +
      `<div class="rp-kv"><span>estimator</span><code>${esc(how.estimator || "–")}</code></div>` +
      `<h4>Exclusion rules gating this contrast</h4><ul class="rp-rules">${ruleLines}</ul>` +
      `<h4>Gaps / reasons</h4><div class="rp-gaps">${gapChips}</div>` +
      `</div>` +
      `<div class="rp-expand-col rp-edit">` +
      `<h4>Coder edits <span class="rp-coder-note">(written to the per-study results CSV, audited)</span></h4>` +
      `<label class="rp-fld"><span>Source location</span>` +
      `<input class="input" data-ef="source_location" value="${locVal}" placeholder="e.g. Table 2, p. 7"></label>` +
      `<label class="rp-fld"><span>Source quote</span>` +
      `<textarea class="input" data-ef="source_quote" rows="2" placeholder="verbatim statistic from the paper">${quoteVal}</textarea></label>` +
      `<label class="rp-fld"><span>Verdict override</span>` +
      `<select class="input" data-ef="verdict_override">${overrideOpts}</select></label>` +
      `<label class="rp-fld"><span>Override reason</span>` +
      `<input class="input" data-ef="verdict_override_reason" value="${esc(r.verdict_override_reason || "")}"></label>` +
      `<label class="rp-fld"><span>Gap / failure note</span>` +
      `<textarea class="input" data-ef="gap_note" rows="2">${esc(r.coder_gap_note || "")}</textarea></label>` +
      `<div class="rp-edit-actions">` +
      `<button class="btn primary" data-save="${esc(cid)}">Save coder edits</button>` +
      `<span class="rp-muted" data-savemsg="${esc(cid)}">${r.coder_edit_at ? "last edit " + esc(fmtStamp(r.coder_edit_at)) : ""}</span>` +
      `</div>` +
      `</div></div></div>`;
  }

  function filesPanel(d) {
    const rows = (S.files && S.files.length ? S.files : (d.file_inventory || []));
    const q = S.fileq.toLowerCase();
    const shown = rows.filter((f) => !q || String(f.file).toLowerCase().includes(q) ||
      String(f.role).toLowerCase().includes(q));
    const body = shown.map((f) => `<tr>` +
      `<td class="mono rp-file">${esc(f.file)}</td>` +
      `<td><span class="rp-role">${esc(f.role || "other")}</span></td>` +
      `<td class="num">${esc(fmtBytes(f.size_bytes))}</td>` +
      `<td class="mono">${esc(f.provenance || "")}</td>` +
      `<td class="mono rp-readby">${esc(f.read_by || "")}</td>` +
      `</tr>`).join("");
    const src = (S.files && S.files.length) ? "live file scan" : "dossier snapshot";
    return `<div class="an-panel rp-stack">` +
      `<div class="rp-mini-kpis">` +
      `<span class="pill">${esc(fmtInt(rows.length))} files</span>` +
      `<span class="pill">${esc(src)}</span>` +
      `<input class="input rp-varsearch" id="rp-fileq" placeholder="Filter files…" value="${esc(S.fileq)}">` +
      `</div>` +
      `<div class="an-tablewrap rp-fileswrap"><table class="an-table"><thead><tr>` +
      `<th>File</th><th>Role</th><th class="num">Size</th><th>Provenance</th><th>Read by</th>` +
      `</tr></thead><tbody>${body || `<tr><td colspan="5" class="rp-none">No dataset files (no Datasets/&lt;Key&gt;/ folder).</td></tr>`}</tbody>` +
      `</table></div>` +
      `<p class="an-note">Raw sources, codebooks, author scripts and our prepare/harmonizer/reproduction scripts ` +
      `and products. “Read by” is the script that references the file.</p>` +
      `</div>`;
  }

  function exclusionsPanel(d) {
    const prov = d.exclusion_provenance || {};
    const rules = d.exclusion_rules || [];
    if (!rules.length) {
      return `<div class="an-panel rp-stack"><h3 class="rp-h">Exclusion provenance</h3>` +
        `<p class="an-note">No author exclusion / attention-check rule for this study` +
        (prov.reason ? ` (${esc(prov.reason)})` : "") + `.</p></div>`;
    }
    const cumByFlag = {};
    (prov.cumulative || []).forEach((e) => { cumByFlag[(e.rule || {}).flag] = e; });
    const gated = {};
    (d.results || []).forEach((r) => {
      const ex = jparse(r.exclusions_applied_json, {});
      (ex.rules || []).forEach((x) => {
        if (x.gates_this_domain) (gated[x.flag] = gated[x.flag] || []).push(r);
      });
    });
    const body = rules.map((r) => {
      const flag = r.flag;
      const cum = cumByFlag[flag] || {};
      const overall = cum.n_participants_removed;
      const inCell = (cum.cells || []).reduce((a, c) => a + (Number(c.n_removed) || 0), 0);
      const g = gated[flag] || [];
      const gatedTxt = g.length
        ? `<span class="rp-gated" title="${esc(g.map((x) => x.dv_name).join(" · "))}">${g.length} contrast${g.length > 1 ? "s" : ""}</span>`
        : `<span class="rp-muted">none</span>`;
      return `<tr>` +
        `<td class="mono">${esc(flag)}</td>` +
        `<td class="mono">${esc(r.keep || "")}</td>` +
        `<td>${esc((r.applies_to || []).join(", "))}</td>` +
        `<td>${esc(r.reason || "")}</td>` +
        `<td class="num">${esc(fmtInt(overall))}</td>` +
        `<td class="num">${esc(fmtInt(inCell))}</td>` +
        `<td>${gatedTxt}</td>` +
        `</tr>`;
    }).join("");
    const meta = `<div class="rp-mini-kpis">` +
      `<span class="pill">${esc(fmtInt(rules.length))} rules</span>` +
      `<span class="pill ${prov.computed ? "ok" : "warn"}">${prov.computed ? "provenance computed" : "not computed"}</span>` +
      `<span class="pill" title="${esc(prov.source || "")}">source: ${esc(shorten(prov.source || "–", 40))}</span>` +
      (prov.reason ? `<span class="pill warn">${esc(prov.reason)}</span>` : "") +
      `</div>`;
    return `<div class="an-panel rp-stack">${meta}` +
      `<div class="an-tablewrap rp-exclwrap"><table class="an-table"><thead><tr>` +
      `<th>Flag</th><th>Keep</th><th>Applies to</th><th>Reason</th>` +
      `<th class="num" title="participants removed overall">Removed</th>` +
      `<th class="num" title="rows removed inside the analysed cells">In-cell</th>` +
      `<th>Gated results</th></tr></thead><tbody>${body}</tbody></table></div>` +
      `<p class="an-note">Per-rule drop counts come from the re-analysis long frame ` +
      `(cumulative and leave-one-rule-out); “gated results” are the dossier contrasts whose ` +
      `DV domain the rule applies to.</p>` +
      `</div>`;
  }

  function runPanel(d) {
    const run = d.run || {};
    const log = S.log;
    const kv = (k, v, mono) => `<div class="rp-kv"><span>${esc(k)}</span>` +
      `<code class="${mono ? "mono" : ""}">${esc(v || "–")}</code></div>`;
    const logBlock = log
      ? (log.exists
        ? `<pre class="rp-logtail">${esc(log.tail || "(empty log)")}</pre>`
        : `<p class="an-note">${esc(log.reason || "no log")}</p>`)
      : `<button class="btn" id="rp-load-log">Load log tail</button>`;
    return `<div class="an-panel rp-stack">` +
      `<h3 class="rp-h">Run card</h3>` +
      kv("command", run.command) +
      kv("batch command", run.batch_command) +
      kv("script", run.script) +
      kv("exit code", run.exit_code === null || run.exit_code === undefined ? "" : String(run.exit_code)) +
      kv("runtime (s)", run.runtime_s === null || run.runtime_s === undefined ? "" : String(run.runtime_s)) +
      kv("participants", run.n_rows === null || run.n_rows === undefined ? "" : String(run.n_rows)) +
      kv("status", run.status) +
      kv("log path", run.log_path) +
      `<div class="rp-logwrap">${logBlock}</div>` +
      `</div>`;
  }

  function notesPanel(d) {
    const idx = d.index || {};
    const prep = String(d.preparations || "");
    const failures = String(d.failures || "").split(" | ").map((s) => s.trim()).filter(Boolean);
    const gaps = d.study_gaps || [];
    const gapCounts = {};
    (d.results || []).forEach((r) => {
      jparse(r.gaps_json, []).forEach((g) => { gapCounts[g] = (gapCounts[g] || 0) + 1; });
    });
    const reportBtn = d.report_available
      ? `<button class="btn ghost" data-open-report="1">Open report ▸</button>`
      : `<span class="rp-muted">no Reproduction/&lt;Key&gt;.md</span>`;
    const failList = failures.length
      ? `<ul class="rp-bullets">${failures.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>`
      : `<p class="an-note">No discrepancy recorded.</p>`;
    const gapChips = Object.keys(gapCounts).sort().map((g) =>
      `<span class="rp-gap">${esc(g)} <b>${gapCounts[g]}</b></span>`).join(" ");
    return `<div class="an-panel rp-stack">` +
      `<h3 class="rp-h">Preparations (d)</h3>` +
      `<div class="rp-mini-kpis">` +
      `<span class="pill ${prep.includes("yes") ? "ok" : "warn"}">${esc(prep || "unknown")}</span>` +
      `<span class="pill" title="report path">${esc(idx.report_path || d.report_path || "–")}</span>` +
      reportBtn + `</div>` +
      `<h3 class="rp-h">Failures / discrepancies (e)</h3>` +
      `<div class="rp-mini-kpis"><span class="pill ${failures.length ? "warn" : ""}">${esc(fmtInt(d.discrepancy_count || failures.length))} recorded</span></div>` +
      failList +
      (gaps.length ? `<h3 class="rp-h">Study gaps</h3><ul class="rp-bullets">${gaps.map((g) => `<li><code>${esc(g)}</code></li>`).join("")}</ul>` : "") +
      `<h3 class="rp-h">Result-level gap reasons</h3>` +
      `<div class="rp-gaps">${gapChips || `<span class="rp-muted">no gap reasons</span>`}</div>` +
      (d.validation_rows && d.validation_rows.length
        ? `<h3 class="rp-h">Validator findings</h3><ul class="rp-bullets">` +
          d.validation_rows.map((x) => `<li><b>${esc(x.check)}</b> ${esc(x.detail || "")} <span class="rp-muted">${esc(x.contrast_id || "")}</span></li>`).join("") +
          `</ul>`
        : "") +
      `</div>`;
  }

  // ---------------------------------------------------------------- markdown
  function inlineMd(s) {
    return s
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/__([^_]+)__/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/(^|[\s(])_([^_\n]+)_/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  }

  function renderMarkdown(src) {
    const lines = esc(String(src).replace(/\r\n?/g, "\n")).split("\n");
    const out = [];
    let i = 0;
    const isSep = (t) => /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(t) && t.includes("-");
    const cells = (t) => t.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());
    const isList = (t) => /^\s*([-*+]|\d+\.)\s+/.test(t);
    const isSpecial = (t) => /^(#{1,6})\s+/.test(t) || isList(t) ||
      t.trimStart().startsWith(">") || t.trimStart().startsWith("|");
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i += 1; continue; }
      let m;
      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
        out.push(`<h${m[1].length}>${inlineMd(m[2])}</h${m[1].length}>`);
        i += 1; continue;
      }
      if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { out.push("<hr>"); i += 1; continue; }
      if (line.trimStart().startsWith(">")) {
        const buf = [];
        while (i < lines.length && lines[i].trimStart().startsWith(">")) {
          buf.push(lines[i].replace(/^\s*>\s?/, "")); i += 1;
        }
        out.push(`<blockquote>${inlineMd(buf.join(" "))}</blockquote>`); continue;
      }
      if (line.trimStart().startsWith("|")) {
        const tbl = [];
        while (i < lines.length && lines[i].trimStart().startsWith("|")) { tbl.push(lines[i]); i += 1; }
        if (tbl.length > 1 && isSep(tbl[1])) {
          const head = cells(tbl[0]);
          const rows = tbl.slice(2).map(cells).filter((r) => r.some((c) => c !== ""));
          out.push("<table><thead><tr>" + head.map((h) => `<th>${inlineMd(h)}</th>`).join("") +
            "</tr></thead><tbody>" + rows.map((r) =>
              "<tr>" + r.map((c) => `<td>${inlineMd(c)}</td>`).join("") + "</tr>").join("") +
            "</tbody></table>");
        } else {
          out.push(`<p>${inlineMd(tbl.join(" "))}</p>`);
        }
        continue;
      }
      if (isList(line)) {
        const ordered = /^\s*\d+\./.test(line);
        const items = [];
        while (i < lines.length && isList(lines[i])) {
          items.push(lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, "")); i += 1;
        }
        out.push(`<${ordered ? "ol" : "ul"}>` +
          items.map((t) => `<li>${inlineMd(t)}</li>`).join("") + `</${ordered ? "ol" : "ul"}>`);
        continue;
      }
      const buf = [];
      while (i < lines.length && lines[i].trim() && !isSpecial(lines[i])) { buf.push(lines[i]); i += 1; }
      out.push(`<p>${inlineMd(buf.join(" "))}</p>`);
    }
    return out.join("\n");
  }

  // ---------------------------------------------------------------- wiring
  function wireDetail() {
    const vs = $("#rp-varsearch");
    if (vs) {
      vs.addEventListener("input", () => {
        S.varq = vs.value;
        renderDetail();
        const el = $("#rp-varsearch");
        if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); }
      });
    }
    const fq = $("#rp-fileq");
    if (fq) {
      fq.addEventListener("input", () => {
        S.fileq = fq.value;
        renderDetail();
        const el = $("#rp-fileq");
        if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); }
      });
    }
    const ll = $("#rp-load-log");
    if (ll) ll.addEventListener("click", loadLog);
    $$("[data-open-report]").forEach((b) => b.addEventListener("click", () => {
      S.reportOpen = true; renderDetail();
    }));
    $$("#rp-detail-body tr.rp-res").forEach((tr) => tr.addEventListener("click", (ev) => {
      if (ev.target.closest(".rp-edit")) return;
      const cid = tr.dataset.cid;
      S.expanded[cid] = !S.expanded[cid];
      renderDetail();
    }));
    $$("[data-save]").forEach((b) => b.addEventListener("click", (ev) => {
      ev.stopPropagation();
      saveEdits(b.dataset.save, b);
    }));
  }

  function editPayload(cid) {
    const row = $$("#rp-detail-body tr.rp-res").find((tr) => tr.dataset.cid === cid);
    const host = row ? row.nextElementSibling : null;
    if (!host) return null;
    const val = (f) => {
      const el = host.querySelector(`[data-ef="${f}"]`);
      return el ? el.value : "";
    };
    return {
      key: S.study,
      contrast_id: cid,
      source_location: val("source_location"),
      source_quote: val("source_quote"),
      verdict_override: val("verdict_override"),
      verdict_override_reason: val("verdict_override_reason"),
      gap_note: val("gap_note"),
      coder: localStorage.getItem("cs.coder") || "coder",
    };
  }

  async function saveEdits(cid, btn) {
    const payload = editPayload(cid);
    if (!payload) return;
    btn.disabled = true;
    let res;
    try {
      res = await api("/api/repro/dossier/edit", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } catch (e) {
      btn.disabled = false;
      toast("Could not save the edit", "err", String(e.message || e));
      return;
    }
    btn.disabled = false;
    if (res && res.ok === false) { toast("Could not save the edit", "err", res.error || ""); return; }
    const msg = $$("#rp-detail-body [data-savemsg]").find((el) => el.dataset.savemsg === cid);
    if (msg) msg.textContent = "saved " + (res.changed || 0) + " field(s)";
    toast("Saved coder edits", "ok", (res.changed || 0) + " field(s) written to the results CSV");
    // refresh the merged row so the table shows the override
    if (S.dossier) {
      const r = (S.dossier.results || []).find((x) => String(x.contrast_id) === cid);
      if (r && res.row) {
        ["source_location", "source_quote", "verdict_override", "verdict_override_reason",
         "coder_gap_note", "coder_edit_at", "coder"].forEach((f) => {
          if (res.row[f] !== undefined) r[f] = res.row[f];
        });
      }
    }
  }

  async function loadLog() {
    if (!S.study) return;
    const wrap = document.querySelector(".rp-logwrap");
    if (wrap) wrap.innerHTML = `<span class="rp-muted">loading log…</span>`;
    try {
      S.log = await api("/api/repro/dossier/log/" + encodeURIComponent(S.study));
    } catch (e) {
      if (wrap) wrap.innerHTML = `<span class="rp-muted">could not load the log: ${esc(String(e.message || e))}</span>`;
      return;
    }
    renderDetail();
  }

  async function openStudy(key, quiet) {
    S.study = key;
    S.dtab = "results";
    S.varq = "";
    S.fileq = "";
    S.expanded = {};
    S.log = null;
    S.reportOpen = false;
    const empty = $("#rp-detail-empty");
    if (empty) empty.textContent = `Loading ${key}…`;
    let dossier, files;
    try {
      [dossier, files] = await Promise.all([
        api("/api/repro/study/" + encodeURIComponent(key) + "/dossier"),
        api("/api/repro/dossier/files/" + encodeURIComponent(key)).catch(() => null),
      ]);
    } catch (e) {
      if (empty) empty.textContent = `Could not load ${key}: ${String(e.message || e)}`;
      S.dossier = null;
      return;
    }
    S.dossier = dossier;
    S.files = (files && files.files) || dossier.file_inventory || [];
    S.filesKey = key;
    renderDetail();
    if (!quiet) {
      const el = $("#rp-detail");
      if (el && el.scrollIntoView) el.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  async function loadOverview() {
    const d = await api("/api/repro/overview");
    S.overview = d;
    // Say where the verdicts come from.  The workbook is the campaign's own
    // summary; without it the tab reads each study's repro_facts.json, and a
    // reader should not have to guess that from the numbers.
    const sub = $("#rp-sub");
    if (sub) {
      if (!sub.dataset.base) sub.dataset.base = sub.innerHTML;
      sub.innerHTML = sub.dataset.base + (d.reproduction_available ? "" :
        " <b>reproduction_summary.xlsx</b> is not in this project, so the verdicts " +
        "are read from each study's <code>repro_facts.json</code> and its report.");
    }
    renderKpis();
    renderFilters();
    renderTable();
    const runbar = $("#rp-runbar");
    const sa = d.scripts_available || {};
    const gb = $("#rp-gen");
    if (gb) {
      gb.disabled = !d.generator_available;
      gb.title = d.generator_available
        ? "Generate the thin per-study reproduction wrappers (idempotent)"
        : "Generator not available — expected temp/generate_repro_wrappers.py";
    }
    const hb = $("#rp-harm");
    if (hb) hb.disabled = !sa.harmonizers;
    const rb = $("#rp-repro");
    if (rb) rb.disabled = !sa.reproducers;
    if (runbar) runbar.title = `dossier index: ${(d.dossier || {}).index_present ? "yes" : "no"} · mapping: ${d.mapping_xlsx ? "yes" : "no"} · summary: ${d.reproduction_xlsx ? "yes" : "no"}`;
    if (d.run && d.run.running && !S.polling) {
      setRunning(true, KIND_LABEL[d.run.kind] || "Running…");
      S.polling = setInterval(pollRun, 1500);
    }
  }

  function wire() {
    const search = $("#rp-search");
    if (search) {
      let t = null;
      search.addEventListener("input", () => {
        clearTimeout(t);
        t = setTimeout(() => { S.q = search.value.trim(); renderTable(); }, 150);
      });
    }
    const close = $("#rp-runlog-close");
    if (close) close.addEventListener("click", () => { $("#rp-runlog").hidden = true; });
    const only = $("#rp-only");
    if (only) only.addEventListener("keydown", (e) => { if (e.key === "Enter") startRun("reproducers"); });
    mkRunButtons();
  }

  function mkRunButtons() {
    const g = $("#rp-gen"); if (g) g.onclick = () => startRun("generate");
    const h = $("#rp-harm"); if (h) h.onclick = () => startRun("harmonizers");
    const r = $("#rp-repro"); if (r) r.onclick = () => startRun("reproducers");
  }

  async function ensureStarted() {
    if (S.started) return;
    S.started = true;
    try { await loadOverview(); }
    catch (e) {
      const host = $("#rp-table");
      if (host) host.innerHTML = `<div class="an-empty">Could not load the reproduction overview: ${esc(e.message || e)}</div>`;
    }
  }

  function boot() {
    wire();
    document.addEventListener("cs:view", (e) => { if (e.detail === "reproduction") ensureStarted(); });
    let saved = "coding";
    try { saved = localStorage.getItem("cs.view") || "coding"; } catch { /* ignore */ }
    if (saved === "reproduction") ensureStarted();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
