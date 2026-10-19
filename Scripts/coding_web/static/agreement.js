/* ============================================================
   Coding Studio — Agreement tab
   Gwet's AC1/AC2 across deterministic labels (<50 levels) and a
   Master conflict resolver.  Raters = canonical Extraction
   ("LLM/base") + every Codings/<Key> - <coder>.json; they need not
   overlap.

   The resolver has a *fast mode* (like the coding tab's coder mode):
   the next conflict is selected automatically, the PDF follows the
   field/value, pressing a coder's number settles with their value and
   the resolver advances; a dropdown settles with any level when every
   coder is wrong.

   Endpoints: /api/agreement/reliability, /api/agreement/conflicts,
   /api/agreement/resolve (+ /abstract/* and /abstract/llm-excluded).
   ============================================================ */
(() => {
  "use strict";

  const { $, $$, esc, api, toast } = window.CS;
  const icon = window.CS.svg;

  const S = { started: false, master: "", scope: "fulltext", tab: "reliability",
              rel: null, conf: null, field: "", status: "open", busy: false, shown: 0,
              studies: [], study: "", viewer: "",
              exclude: new Set(), fast: false, activeKey: null, viewerStudy: null,
              pages: null, pageSize: null, page: 1, fit: true, zoom: 1, spans: {},
              showSettled: false };

  const acClass = (v) => v === null || v === undefined ? "ac-na"
    : v >= 0.8 ? "ac-good" : v >= 0.6 ? "ac-mid" : "ac-low";
  const fmt = (v) => (v === null || v === undefined) ? "–" : Number(v).toFixed(3);
  const pct = (v) => (v === null || v === undefined) ? "–" : `${Math.round(100 * Number(v))}%`;
  const labelOf = (row, v) => (row.labels && row.labels[v]) || v;

  // ---------------------------------------------------------------- reliability
  function excludeParam() {
    const names = [...S.exclude].filter(Boolean);
    return names.join(",");
  }

  async function loadReliability() {
    const q = new URLSearchParams();
    const ex = excludeParam();
    if (ex) q.set("exclude", ex);
    let d;
    try {
      const base = S.scope === "abstract" ? "/api/agreement/abstract/reliability"
                                           : "/api/agreement/reliability";
      d = await api(base + "?" + q.toString());
    } catch (e) {
      $("#ag-rel-table").innerHTML = `<div class="ag-empty">Could not compute agreement: ${esc(e.message)}</div>`;
      return;
    }
    S.rel = d;
    const o = d.overall || {};
    const kpis = [
      ["Overall AC1", fmt(o.ac1), `${o.n_fields || 0} fields`],
      ["Overall AC2", fmt(o.ac2), "ordered fields"],
      ["Items (≥2 raters)", (o.n_items_total || 0).toLocaleString(), "cells"],
      ["Raters", (d.raters || []).length, ex ? `excluding ${ex}` : "all included"],
    ];
    const da = d.dv_alignment;
    if (da) {
      const total = (da.matched_pairs || 0) + (da.unmatched || 0);
      kpis.splice(2, 0, ["DV alignment", `${da.matched_pairs || 0}/${total}`,
        `${da.studies_passing || 0}/${da.studies || 0} studies · cov ${pct(da.coverage)}`]);
    }
    $("#ag-kpis").innerHTML = kpis.map(([k, v, s]) =>
      `<div class="kpi"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div><div class="s">${esc(s)}</div></div>`).join("");
    renderRaters(d);
    const fields = (d.fields || []).slice().sort((a, b) => {
      const x = a.ac1 === null ? -1 : a.ac1, y = b.ac1 === null ? -1 : b.ac1;
      return y - x || a.label.localeCompare(b.label);
    });
    const rows = fields.map((f) => {
      const align = f.kind === "dv"
        ? `${f.matched_pairs || 0} matched` +
          (f.unmatched ? ` · ${f.unmatched} unmat.` : "") +
          `<br><span class="grp">cov ${pct(f.coverage)}</span>`
        : "–";
      const ac1 = f.insufficient
        ? `<span class="ag-insuff" title="${esc(f.note || "")}">${esc(f.note || "insufficient")}</span>`
        : fmt(f.ac1);
      return `<tr>
      <td>${esc(f.label)}<br><span class="grp">${esc(f.group)}</span></td>
      <td>${f.kind === "dv" ? "DV" : "Exp"}</td>
      <td class="num">${f.n_levels}${f.multi ? "*" : ""}</td>
      <td class="num">${f.n_multi}</td>
      <td class="num">${f.n_raters}</td>
      <td class="num ag-aligncell">${align}</td>
      <td class="num ${acClass(f.ac1)}">${ac1}</td>
      <td class="num ${acClass(f.ac2)}">${fmt(f.ac2)}</td>
    </tr>`;
    }).join("");
    $("#ag-rel-table").innerHTML =
      `<table class="ag-table"><thead><tr><th>Field</th><th>Unit</th><th>Levels</th>` +
      `<th>Items</th><th>Raters</th><th>DV align</th><th>AC1</th><th>AC2</th></tr></thead><tbody>${rows}</tbody></table>`;
    $("#ag-rel-note").textContent =
      "DV rows are aligned by a canonical key (domains · veracity · timepoint · contrast · arm) then by " +
      "dv_name similarity; unmatched rows are reported and excluded. A DV statistic is shown only with " +
      "≥5 matched pairs and ≥60% coverage. AC1 is nominal; AC2 uses linear weights and is shown only for " +
      "integer-ordered fields. Items = cases rated by at least two raters; raters may cover different items. " +
      "Click a rater to include or exclude them from these coefficients.";
    $("#ag-hint").textContent = `${fields.length} fields · ${(d.raters || []).length} raters`;
    renderDvAlignment(d);
  }

  // Raters are toggle buttons: click a name to include/exclude them.
  function renderRaters(d) {
    const host = $("#ag-raters");
    if (!host) return;
    host.innerHTML = (d.rater_summary || []).map((r) => {
      const off = S.exclude.has(r.rater);
      return `<button type="button" class="ag-rater${r.rater === d.reference ? " ref" : ""}${off ? " off" : ""}"` +
        ` data-rater="${esc(r.rater)}" aria-pressed="${off ? "false" : "true"}"` +
        ` title="${off ? "Include" : "Exclude"} ${esc(r.rater)} ${off ? "in" : "from"} the agreement">` +
        `<b>${esc(r.rater)}</b><span class="n">${r.keys} keys · ${r.experiments} exp</span></button>`;
    }).join("");
  }

  // Per-study DV alignment coverage: matched vs unmatched, with the gate state.
  function renderDvAlignment(d) {
    const host = $("#ag-dv-align");
    if (!host) return;
    const da = d && d.dv_alignment;
    const studies = (da && da.by_study) || [];
    if (!studies.length) { host.innerHTML = ""; return; }
    const rows = studies.map((s) => {
      const raters = (s.raters || []).map((r) =>
        `<span class="ag-alignrater" title="${esc(r.rater)}">${esc(r.rater)}` +
        `: ${r.n_dv} DV · ${r.matched} matched${r.unmatched ? ` · ${r.unmatched} unmat.` : ""}</span>`).join("");
      const gate = s.gate
        ? `<span class="ag-gate ok">shown</span>`
        : `<span class="ag-gate off">suppressed</span>`;
      return `<tr>
        <td>${esc(s.key)}</td>
        <td class="num ${s.coverage >= 0.6 ? "ac-good" : "ac-low"}">${pct(s.coverage)}</td>
        <td class="num">${s.matched_pairs}</td>
        <td class="num">${s.unmatched}</td>
        <td>${gate}</td>
        <td class="ag-alignraters">${raters}</td>
      </tr>`;
    }).join("");
    host.innerHTML =
      `<details class="ag-dvalign" open><summary>DV alignment coverage — ` +
      `${da.studies_passing || 0}/${da.studies || 0} studies pass the gate ` +
      `(≥${da.min_matched || 5} matched pairs and ≥${Math.round(100 * (da.min_coverage || 0.6))}% coverage)</summary>` +
      `<div class="an-tablewrap"><table class="ag-table"><thead><tr><th>Study</th><th>Coverage</th>` +
      `<th>Matched</th><th>Unmatched</th><th>Agreement</th><th>Per rater</th></tr></thead>` +
      `<tbody>${rows}</tbody></table></div>` +
      `<p class="an-note">Unmatched DV rows are never silently dropped: they are excluded from the ` +
      `coefficients and listed here. Conflicts are built from the same aligned rows, so two raters ` +
      `listing their DVs in a different order no longer fabricate disagreements.</p></details>`;
  }

  // ---------------------------------------------------------------- conflicts
  async function loadConflicts({ append = false } = {}) {
    if (!append) S.shown = 0;
    const q = new URLSearchParams({ master: S.master, status: S.status,
                                    limit: "400", offset: String(S.shown) });
    if (S.field) q.set("field", S.field);
    if (S.study) q.set("study", S.study);
    let d;
    try {
      const base = S.scope === "abstract" ? "/api/agreement/abstract/conflicts"
                                           : "/api/agreement/conflicts";
      d = await api(base + "?" + q.toString());
    } catch (e) {
      if (!append) $("#ag-conflist").innerHTML = `<div class="ag-empty">Could not load conflicts: ${esc(e.message)}</div>`;
      return;
    }
    S.conf = d;
    S.studies = d.studies || [];
    // keep the selection valid and default to the study with the most open conflicts
    if (!S.study && S.studies.length) { S.study = S.studies[0].key; return loadConflicts(); }
    if (S.study && !S.studies.some((s) => s.key === S.study)) {
      S.study = S.studies.length ? S.studies[0].key : "";
      if (S.study) return loadConflicts();
    }
    if (!append) { renderStudyList(); renderFieldList(d); }
    const html = conflictCardsHtml(d.rows || []);
    const host = $("#ag-conflist");
    if (append && html) host.insertAdjacentHTML("beforeend", html);
    else host.innerHTML = html || `<div class="ag-empty">No ${esc(S.status)} conflicts${S.field ? " for this field" : ""} for this study.</div>`;
    S.shown += (d.rows || []).length;
    renderConflictTail(d);
    $("#ag-confcount").textContent = `${d.total} ${S.status}`;
    $("#ag-hint").textContent = S.field ? `field: ${S.field}` : `${(d.fields || []).length} fields`;
    if (!append) {
      // Only (re)load the paper when the study actually changed, so advancing
      // through conflicts never resets the PDF's scroll position.
      if (S.viewerStudy !== S.study) await loadViewer();
      if (S.fast) await activateCase(agCards()[0]);
    }
  }

  function renderConflictTail(d) {
    const tail = $("#ag-conftail");
    if (!tail) return;
    if (S.shown < d.total) {
      tail.innerHTML = `<button class="btn" id="ag-more">Load more (${S.shown} of ${d.total})</button>`;
      $("#ag-more").addEventListener("click", () => loadConflicts({ append: true }));
    } else {
      tail.innerHTML = d.total ? `<p class="an-note">Showing all ${d.total} conflict(s).</p>` : "";
    }
  }

  function renderFieldList(d) {
    const fields = (d.fields || []).slice().sort((a, b) => (b.open - a.open) || a.label.localeCompare(b.label));
    const totalOpen = fields.reduce((n, f) => n + f.open, 0);
    const totalSettled = fields.reduce((n, f) => n + f.settled, 0);
    const item = (name, label, open, settled, active) =>
      `<button class="ag-fieldbtn${active ? " active" : ""}" data-field="${esc(name)}">
        <span class="grow">${esc(label)}</span>` +
      `<span class="cnt${open ? "" : " zero"}">${open}</span>` +
      (settled ? `<span class="cnt settled">${settled}</span>` : "") + `</button>`;
    let html = item("", "All fields", totalOpen, totalSettled, !S.field);
    const groups = {};
    fields.forEach((f) => { (groups[f.group] = groups[f.group] || []).push(f); });
    Object.keys(groups).sort().forEach((g) => {
      html += `<div class="ag-fieldgrp">${esc(g)}</div>`;
      groups[g].forEach((f) => {
        html += item(f.name, f.label, f.open, f.settled, S.field === f.name);
      });
    });
    $("#ag-fields").innerHTML = html;
    const fc = $("#ag-fieldcount");
    if (fc) fc.textContent = String(fields.length);
  }

  function renderStudyList() {
    const host = $("#ag-studies");
    const count = $("#ag-studycount");
    if (count) count.textContent = String(S.studies.length);
    if (!host) return;
    if (!S.studies.length) {
      host.innerHTML = `<div class="ag-empty">No conflicted studies.</div>`;
      return;
    }
    const studiesToShow = S.showSettled
      ? S.studies.filter((s) => s.n_settled > 0)
      : S.studies;
    if (!studiesToShow.length) {
      host.innerHTML = `<div class="ag-empty">${S.showSettled ? "No studies with settled conflicts." : "No conflicted studies."}</div>`;
      return;
    }
    host.innerHTML = studiesToShow.map((s) => {
      const act = s.key === S.study ? " active" : "";
      return `<button type="button" class="ag-studybtn${act}" data-study="${esc(s.key)}" title="${esc(s.label || s.key)}">` +
        `<span class="t">${esc(s.label || s.key)}</span>` +
        `<span class="n">${s.n_open}<i>open</i></span>` +
        (s.n_settled ? `<span class="n settled">${s.n_settled}<i>settled</i></span>` : "") +
        `</button>`;
    }).join("");
  }

  // ------------------------------------------- LLM excluded, raters would include
  async function loadLlmExcluded() {
    const host = $("#ag-llmexcl");
    if (!host) return;
    if (S.scope !== "abstract") { host.hidden = true; host.innerHTML = ""; return; }
    host.hidden = false;
    host.innerHTML = `<div class="ag-llmexcl"><div class="ag-llmexcl-head">` +
      `<h3>LLM excluded · raters would include</h3></div><p class="an-note">Loading…</p></div>`;
    try {
      renderLlmExcluded(await api("/api/agreement/abstract/llm-excluded"));
    } catch (e) {
      host.innerHTML = `<div class="ag-empty">Could not load LLM/rater disagreements: ${esc(e.message)}</div>`;
    }
  }

  function renderLlmExcluded(d) {
    const host = $("#ag-llmexcl");
    if (!host) return;
    const rows = (d && d.rows) || [];
    const head = `<div class="ag-llmexcl-head"><h3>LLM excluded · raters would include</h3>` +
      `<span class="pill">${rows.length}</span></div>`;
    if (!rows.length) {
      host.innerHTML = `<div class="ag-llmexcl">${head}` +
        `<p class="an-note">No records the LLM excluded that a rater would include.</p></div>`;
      return;
    }
    host.innerHTML = `<div class="ag-llmexcl">${head}` +
      `<p class="an-note">Records the LLM abstract screen excluded (a C1–C4 “No”) but at least one ` +
      `rater would include. Open one to re-screen it in the abstract stage.</p>` +
      `<div class="ag-llmexcl-list">${rows.map((r) => `
        <div class="ag-llmexcl-row">
          <div class="ag-llmexcl-info">
            <div class="t">${esc(r.title || r.key)}</div>
            <div class="m">${esc([r.authors, r.year, r.an ? "an " + r.an : ""].filter(Boolean).join(" · "))}</div>
          </div>
          <div class="ag-llmexcl-raters">${(r.include_raters || []).map((c) =>
            `<span class="ag-chip" title="${esc(c)} would include">` +
            `<span class="who">${esc(c)}</span><b>Include</b></span>`).join("")}</div>
          <button class="btn" data-act="open-screen" data-uid="${esc(r.key)}">` +
            `${icon("i-list")} Open in screening</button>
        </div>`).join("")}</div></div>`;
  }

  function openInScreening(uid) {
    const id = String(uid || "").trim();
    if (!id) return;
    const tab = document.querySelector('.viewtab[data-view="screening"]');
    if (tab && !tab.classList.contains("active")) tab.click();
    window.dispatchEvent(new CustomEvent("cs:screen-open", { detail: { uid: id } }));
  }

  function conflictCardsHtml(rows) {
    if (!rows.length) return "";
    const num = S.fast;
    // Multiple experiments per paper: group the cards by Study_ID so every
    // experiment gets its own section instead of one undifferentiated list.
    const groups = new Map();
    rows.forEach((r) => {
      const sid = r.study_id || r.key;
      if (!groups.has(sid)) groups.set(sid, []);
      groups.get(sid).push(r);
    });
    const cardHtml = (r) => {
      const entries = Object.entries(r.values || {});
      const chips = entries.map(([rater, val], i) =>
        `<button class="ag-chip" data-act="set" data-value="${esc(val)}" title="Settle with ${esc(rater)}'s value">` +
        (num ? `<span class="k">${i + 1}</span>` : "") +
        `<span class="who">${esc(rater)}</span><b>${esc(labelOf(r, val))}</b></button>`).join("");
      const master = r.settled
        ? `<div class="ag-values"><span class="ag-chip master"><span class="who">Master</span><b>${esc(labelOf(r, r.resolution || r.master_value))}</b></span>
             <button class="ag-chip clear" data-act="clear">Clear</button></div>`
        : "";
      let picker = "";
      if (!r.multi && (r.levels || []).length) {
        const opts = r.levels.map((l) => `<option value="${esc(l)}">${esc(labelOf(r, l))}</option>`).join("");
        picker = `<div class="ag-levels"><select class="select ag-levelsel">${opts}</select>` +
                 `<button class="btn ag-levelset" data-act="set">Settle</button></div>`;
      } else {
        picker = `<div class="ag-levels"><input class="input ag-leveltext" placeholder="label + label" spellcheck="false" style="max-width:220px">` +
                 `<button class="btn ag-levelset" data-act="set">Settle</button></div>`;
      }
      const dv = r.dv_name ? ` · ${esc(r.dv_name)}` : (r.dv_index !== null && r.dv_index !== undefined ? ` · DV ${r.dv_index + 1}` : "");
      return `<div class="ag-confcard${r.settled ? " settled" : ""}" data-key="${esc(r.key)}" data-study="${esc(r.study_id)}"
                   data-case="${esc(r.case_id || "")}" data-dv="${r.dv_index === null || r.dv_index === undefined ? "" : r.dv_index}"
                   data-dvname="${esc(r.dv_name || "")}" data-field="${esc(r.field)}" data-label="${esc(r.field_label || r.field)}"
                   data-multi="${r.multi ? "1" : "0"}">
        <div class="ag-confhead">
          <span class="sid">${esc(r.study_id)}</span>
          <span class="fld">${esc(r.field_label)}${dv}</span>
          <span class="meta">${esc(r.key)} · ${r.kind}${r.multi ? " · multi" : ""}</span>
        </div>
        <div class="ag-values">${chips}</div>
        ${master}${picker}
      </div>`;
    };
    return [...groups.entries()].map(([sid, list]) => {
      const nSettled = list.filter((r) => r.settled).length;
      const nOpen = list.length - nSettled;
      return `<div class="ag-exphead"><span>${esc(sid)}</span><span class="cnt">` +
        `${nOpen} open</span>` +
        (nSettled ? `<span class="cnt settled">${nSettled} settled</span>` : "") +
        `</div>` + list.map(cardHtml).join("");
    }).join("");
  }

  // ---------------------------------------------------------------- fast mode
  const agCards = () => $$("#ag-conflist .ag-confcard");

  async function activateCase(card) {
    if (!card) return;
    agCards().forEach((c) => c.classList.toggle("active", c === card));
    S.activeKey = card.dataset.key;
    try { card.scrollIntoView({ block: "center", behavior: "smooth" }); } catch { /* ignore */ }
    if (!S.fast || S.scope !== "fulltext") return;
    await ensurePdfViewer();
    await locateCase(card);
  }

  async function ensurePdfViewer() {
    if (S.scope !== "fulltext" || !S.study) return false;
    if (S.viewer !== "pdf") {
      S.viewer = "pdf";
      syncViewerSeg();
      await loadViewer();
    }
    if (!agWrap() && S.pages == null) await loadViewer();
    return !!agWrap();
  }

  // Terms that locate a field's evidence in the paper (mirrors the coding tab's
  // coder mode).  The rater values are tried after these, then the label.
  const AG_LOCATE = {
    Intervention_Type: ["intervention", "training", "treatment"],
    N_Recruited: ["recruit", "participants", "enrolled"],
    N_Analyzed: ["analy", "final sample", "excluded"],
    N_Reported_Per_Condition: ["participants", "per condition"],
    N_Inference_Method: ["inferred", "estimated"],
    Setting: ["online", "laborator", "classroom", "field"],
    Country: ["countr", "nationwide"],
    Age_Group: ["students", "adults", "age"],
    Age_Reported_As: ["mean age", "years old"],
    Gender_Reported_As: ["female", "male", "gender"],
    control_group_type: ["control group", "control condition", "placebo"],
    within_between: ["between-subjects", "within-subjects", "randomly assigned"],
    unit_of_analysis: ["participants", "items"],
    Modality: ["video", "game"],
    Stimuli: ["headlines", "claims", "stimuli"],
    Synthetic: ["synthetic", "real"],
    Intervention_Duration: ["minutes", "duration", "session"],
    Attention_Cues: ["attention", "warning"],
    Publication: ["published", "journal"],
    dv_domain: ["accuracy", "credibility", "sharing", "belief"],
    veracity: ["true", "false", "fake", "real"],
    contrast_type: ["between-subjects", "within-subjects"],
    dv_polarity: ["reverse", "reverse-coded", "higher"],
    eligible_primary: ["primary outcome", "main outcome", "preregister"],
    score_types: ["accuracy", "belief", "discrimination", "confidence"],
    timepoint: ["posttest", "follow-up", "baseline"],
  };

  async function locateCase(card) {
    if (!agFind || !agWrap()) return;
    // In fast mode, don't open/focus the find bar — just search for highlights.
    // The find bar steals focus (input.focus()), which breaks 1-9 key resolution.
    if (!S.fast && agFindToggle) agFindToggle.open("");
    const field = card.dataset.field || "";
    const terms = [];
    (AG_LOCATE[field] || []).forEach((t) => terms.push(t));
    card.querySelectorAll("[data-act='set'] b").forEach((b) => terms.push(b.textContent));
    if (card.dataset.label) terms.push(card.dataset.label);
    const seen = new Set();
    const inp = $("#ag-find");
    let hit = "";
    for (const raw of terms) {
      const t = String(raw || "").trim();
      if (t.length < 3) continue;
      const k = t.toLowerCase();
      if (seen.has(k)) continue;
      seen.add(k);
      const n = await agFind.search(t, true);
      if (n) { hit = t; break; }
    }
    if (!hit && terms.length) {
      // no term found: still show the first term so the finder state is visible
      hit = String(terms[0]).trim();
      await agFind.search(hit, true);
    }
    if (inp && hit) inp.value = hit;
  }

  function setFast(on) {
    S.fast = !!on;
    const b = $("#ag-fast");
    if (b) b.setAttribute("aria-pressed", S.fast ? "true" : "false");
    document.body.classList.toggle("ag-fast", S.fast);
    if (S.tab !== "conflicts") return;
    const host = $("#ag-conflist");
    if (host) host.innerHTML = conflictCardsHtml((S.conf && S.conf.rows) || []);
    if (S.fast) { ensurePdfViewer().then(() => activateCase(agCards()[0])); }
    else agCards().forEach((c) => c.classList.remove("active"));
  }

  async function fastKeydown(e) {
    if (!S.fast || S.tab !== "conflicts") return;
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
    if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return;
    const card = $("#ag-conflist .ag-confcard.active") || agCards()[0];
    if (!card) return;
    if (/^[1-9]$/.test(e.key)) {
      const chips = card.querySelectorAll("[data-act='set'][data-value]");
      const chip = chips[Number(e.key) - 1];
      if (chip) { e.preventDefault(); resolveCard(card, chip.dataset.value); }
      return;
    }
    if (e.key === "Enter") {
      const sel = card.querySelector(".ag-levelsel");
      if (sel) { e.preventDefault(); resolveCard(card, sel.value); }
    }
  }

  // ---------------------------------------------------------------- viewer
  function syncViewerSeg() {
    $$("#ag-viewerseg button").forEach((b) => {
      const v = b.dataset.agviewer;
      const avail = S.scope === "abstract" ? v === "abstract" : v !== "abstract";
      b.hidden = !avail;
      b.classList.toggle("active", v === S.viewer);
    });
    const link = $("#ag-open-pdf");
    if (link) {
      const ok = S.scope === "fulltext" && !!S.study;
      link.hidden = !ok;
      if (ok) link.href = `/paper/${encodeURIComponent(S.study)}`;
    }
    const findWrap = $("#ag-find-wrap");
    if (findWrap) findWrap.hidden = !(S.scope === "fulltext" && S.viewer === "pdf" && !!S.study);
    agRenderControls();
  }

  /* in-PDF search for the agreement viewer */
  let agFind = null, agFindToggle = null;

  /* make sure a hit page is actually rendered before painting highlights */
  function agEnsurePage(n) {
    const w = agWrap();
    const img = w && w.querySelector(`.pdf-page[data-page="${n}"]`);
    if (img) {
      if (!img.getAttribute("src") && img.dataset.src) img.setAttribute("src", img.dataset.src);
      agEnsureSpans(Number(img.dataset.page));
    }
    return new Promise((resolve) => {
      if (!img) { resolve(); return; }
      if (img.complete && img.naturalWidth) { if (agFind) agFind.refresh(); resolve(); return; }
      let done = false;
      const to = setTimeout(() => { if (!done) { done = true; if (agFind) agFind.refresh(); resolve(); } }, 1500);
      img.addEventListener("load", () => { if (!done) { done = true; clearTimeout(to); if (agFind) agFind.refresh(); resolve(); } }, { once: true });
      img.addEventListener("error", () => { if (!done) { done = true; clearTimeout(to); resolve(); } }, { once: true });
    });
  }

  function initAgFind() {
    if (agFind || !window.PdfSearch) return;
    agFindToggle = window.PdfSearch.bindToggle("ag-find");
    agFind = window.PdfSearch.create({
      getWrap: () => $("#ag-viewer-body .pdf-scroll"),
      getInput: () => $("#ag-find"),
      getCount: () => $("#ag-find-count"),
      getPrev: () => $("#ag-find-prev"),
      getNext: () => $("#ag-find-next"),
      fetch: (q) => api(`/api/pdfsearch/${encodeURIComponent(S.study)}?q=${encodeURIComponent(q)}`),
      ensurePage: (p) => agEnsurePage(p),
    });
  }

  function resetAgFind() {
    const i = $("#ag-find"); if (i) i.value = "";
    if (agFind) agFind.reset();
    if (agFindToggle) agFindToggle.reset();
  }

  /* ---- server-rendered PDF pages, on par with the coding viewer ---- */
  let agObserver = null, agScrollHandler = null, agWheelHandler = null, agScrollTimer = null;

  function agWrap() { return $("#ag-viewer-body .pdf-scroll"); }

  function agTeardownPdf() {
    if (agObserver) { agObserver.disconnect(); agObserver = null; }
    const w = agWrap();
    if (w && agScrollHandler) w.removeEventListener("scroll", agScrollHandler);
    agScrollHandler = null;
    if (w && agWheelHandler) w.removeEventListener("wheel", agWheelHandler);
    agWheelHandler = null;
    clearTimeout(agScrollTimer);
  }

  function agRenderControls() {
    const box = $("#ag-pdf-controls");
    if (!box) return;
    const ok = S.scope === "fulltext" && S.viewer === "pdf" && !!S.study && !!S.pages;
    box.hidden = !ok;
    const pi = $("#ag-page-input");
    if (pi) {
      pi.value = S.page;
      if (S.pages) { pi.max = S.pages; pi.disabled = false; }
      else { pi.removeAttribute("max"); pi.disabled = true; }
    }
    const tot = $("#ag-page-total");
    if (tot) tot.textContent = S.pages ? S.pages : "–";
    const ft = $("#ag-fit-toggle");
    if (ft) ft.setAttribute("aria-pressed", S.fit ? "true" : "false");
  }

  function agScrollToPage(n) {
    const w = agWrap();
    if (!w) return;
    const img = w.querySelector(`.pdf-page[data-page="${n}"]`);
    if (img) w.scrollTop = Math.max(0, img.offsetTop - 6);
  }

  function agApplyFit() {
    const w = agWrap();
    if (!w) return;
    w.classList.toggle("fit", S.fit);
    w.classList.toggle("zoom", !S.fit);
    const s = S.pageSize;
    if (s && s.w) w.style.setProperty("--pdf-100-w", Math.round(s.w * 96 / 72 * (S.zoom || 1)) + "px");
    agRenderControls();
    requestAnimationFrame(() => { if (agFind) agFind.refresh(); agPositionTextLayers(); });
  }

  /* ---- selectable text: a transparent layer over each rendered page ---- */
  function agEnsureSpans(n) {
    if (S.spans[n]) return;
    S.spans[n] = "loading";
    api(`/api/pdftext/${encodeURIComponent(S.study)}/${n}.json`)
      .then((d) => { S.spans[n] = (d && d.spans) || []; agAddTextLayer(n); })
      .catch(() => { S.spans[n] = []; });
  }

  function agAddTextLayer(n) {
    const spans = S.spans[n];
    if (!spans || spans === "loading" || !spans.length) return;
    const w = agWrap();
    if (!w) return;
    const old = w.querySelector(`:scope > .pdf-textlayer[data-page="${n}"]`);
    if (old) old.remove();
    const layer = document.createElement("div");
    layer.className = "pdf-textlayer";
    layer.dataset.page = String(n);
    spans.forEach((s) => {
      const sp = document.createElement("span");
      sp.className = "pdf-word";
      sp.textContent = String(s[4]).replace(/\s+$/, "") + " ";
      sp.style.left = (s[0] * 100) + "%";
      sp.style.top = (s[1] * 100) + "%";
      sp.style.width = Math.max(0.2, (s[2] - s[0]) * 100) + "%";
      sp.style.height = Math.max(0.2, (s[3] - s[1]) * 100) + "%";
      sp.style.fontSize = Math.max(0.004, (s[3] - s[1])) + "em";
      layer.appendChild(sp);
    });
    w.appendChild(layer);
    agPositionTextLayers();
  }

  function agPositionTextLayers() {
    const w = agWrap();
    if (!w) return;
    const imgs = {};
    w.querySelectorAll(".pdf-page[data-page]").forEach((img) => { imgs[img.dataset.page] = img; });
    w.querySelectorAll(":scope > .pdf-textlayer").forEach((layer) => {
      const img = imgs[layer.dataset.page];
      if (!img || !img.offsetWidth || !img.offsetHeight) return;
      layer.style.left = img.offsetLeft + "px";
      layer.style.top = img.offsetTop + "px";
      layer.style.width = img.offsetWidth + "px";
      layer.style.height = img.offsetHeight + "px";
      layer.style.fontSize = img.offsetHeight + "px";
    });
  }

  function agMountPdf() {
    agTeardownPdf();
    const host = $("#ag-viewer-body");
    host.classList.add("pdf-mode");
    const wrap = document.createElement("div");
    wrap.className = "pdf-scroll" + (S.fit ? " fit" : " zoom");
    const s = S.pageSize;
    if (s && s.w) {
      wrap.style.setProperty("--pdf-100-w", Math.round(s.w * 96 / 72 * (S.zoom || 1)) + "px");
      wrap.style.setProperty("--pdf-ratio", `${s.w} / ${s.h}`);
    }
    host.innerHTML = "";
    host.appendChild(wrap);

    agWheelHandler = (e) => {
      if (!e.ctrlKey) return;
      e.preventDefault();
      const step = e.deltaY < 0 ? 0.05 : -0.05;
      if (S.fit) {
        const img = wrap.querySelector(".pdf-page");
        const ss = S.pageSize;
        const base = ss && ss.w ? ss.w * 96 / 72 : 0;
        if (img && base > 0 && img.offsetWidth) S.zoom = img.offsetWidth / base;
        S.fit = false;
      }
      S.zoom = Math.min(3, Math.max(0.5, (S.zoom || 1) + step));
      agApplyFit();
    };
    wrap.addEventListener("wheel", agWheelHandler, { passive: false });

    const io = new IntersectionObserver((entries) => {
      entries.forEach((en) => {
        if (!en.isIntersecting) return;
        const img = en.target;
        const src = img.dataset.src;
        if (src && !img.getAttribute("src")) img.setAttribute("src", src);
        agEnsureSpans(Number(img.dataset.page));
      });
    }, { root: wrap, rootMargin: "1500px 0px" });
    agObserver = io;
    S.spans = {};

    for (let n = 1; n <= S.pages; n += 1) {
      const img = document.createElement("img");
      img.className = "pdf-page";
      img.dataset.page = String(n);
      img.dataset.src = `/pageimg/${encodeURIComponent(S.study)}/${n}.png?scale=2`;
      img.alt = `Page ${n}`;
      img.decoding = "async";
      if (s && s.w) img.style.aspectRatio = `${s.w} / ${s.h}`;
      img.addEventListener("error", () => img.classList.add("page-error"));
      io.observe(img);
      wrap.appendChild(img);
    }

    agScrollHandler = () => {
      const w = agWrap();
      if (!w) return;
      clearTimeout(agScrollTimer);
      agScrollTimer = setTimeout(() => {
        const top = w.scrollTop;
        let current = S.page;
        w.querySelectorAll(".pdf-page").forEach((img) => {
          if (img.offsetTop <= top + 30) current = Number(img.dataset.page);
        });
        if (current !== S.page) { S.page = current; agRenderControls(); }
      }, 120);
    };
    wrap.addEventListener("scroll", agScrollHandler);
    requestAnimationFrame(() => {
      agScrollToPage(S.page);
      if (agFind) agFind.refresh();
      agPositionTextLayers();
    });
  }

  async function loadViewer() {
    const host = $("#ag-viewer-body");
    if (!host) return;
    S.viewerStudy = S.study;
    if (S.scope === "abstract") S.viewer = "abstract";
    else if (!S.viewer || S.viewer === "abstract") S.viewer = "text";
    syncViewerSeg();
    if (!S.study) {
      S.pages = null;
      host.classList.remove("pdf-mode");
      host.innerHTML = `<p class="side-hint" style="padding:16px">Select a study to read it here.</p>`;
      agRenderControls();
      return;
    }
    if (S.scope === "abstract") {
      S.pages = null;
      host.classList.remove("pdf-mode");
      host.innerHTML = `<p class="side-hint" style="padding:16px">Loading abstract…</p>`;
      try {
        const d = await api(`/api/screening/abstract/${encodeURIComponent(S.study)}`);
        const rec = d.record || {};
        host.innerHTML = `<article class="ag-abs">` +
          `<h2 class="ag-abs-title">${esc(rec.title || "(no title)")}</h2>` +
          `<p class="ag-abs-sub">${esc([rec.authors, rec.year, rec.source].filter(Boolean).join(" · ") || "—")}</p>` +
          `<p class="ag-abs-text">${esc(rec.abstract || "(no abstract)")}</p></article>`;
      } catch (e) {
        host.innerHTML = `<div class="ag-empty">Could not load the abstract: ${esc(e.message)}</div>`;
      }
      return;
    }
    if (S.viewer === "pdf") {
      host.classList.add("pdf-mode");
      host.innerHTML = `<p class="side-hint" style="padding:16px">Loading PDF…</p>`;
      try {
        const info = await api(`/api/pdfinfo/${encodeURIComponent(S.study)}`);
        S.pages = info.pages || 0;
        S.pageSize = info.size || null;
        if (!S.pages) {
          host.innerHTML = `<div class="ag-empty">No PDF available.</div>`;
          agRenderControls();
          return;
        }
        agMountPdf();
      } catch (e) {
        S.pages = null;
        host.innerHTML = `<div class="ag-empty">Could not load the PDF: ${esc(e.message)}</div>`;
      }
      agRenderControls();
      return;
    }
    S.pages = null;
    host.classList.remove("pdf-mode");
    host.innerHTML = `<p class="side-hint" style="padding:16px">Loading text…</p>`;
    try {
      const d = await api(`/api/text/${encodeURIComponent(S.study)}`);
      host.innerHTML = d.text
        ? `<pre class="text ag-text">${esc(d.text)}</pre>`
        : `<div class="ag-empty">No extracted text for this study.</div>`;
    } catch (e) {
      host.innerHTML = `<div class="ag-empty">Could not load the text: ${esc(e.message)}</div>`;
    }
  }

  async function resolveCard(card, value) {
    if (S.busy) return;
    const master = (S.master || "").trim();
    if (!master) { toast("Enter the Master name first", "err"); $("#ag-master").focus(); return; }
    S.busy = true;
    try {
      const base = S.scope === "abstract" ? "/api/agreement/abstract/resolve"
                                           : "/api/agreement/resolve";
      await api(base, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          master, key: card.dataset.key, study_id: card.dataset.study,
          field: card.dataset.field,
          case_id: card.dataset.case || "",
          dv_index: card.dataset.dv === "" ? null : Number(card.dataset.dv),
          dv_name: card.dataset.dvname || "",
          value: value,
        }),
      });
      toast(value === null ? "Resolution cleared" : "Conflict settled", "ok", S.master);
      await loadConflicts();
      if (S.fast && S.tab === "conflicts") await activateCase(agCards()[0]);
    } catch (e) {
      toast("Could not settle", "err", String(e.message || e));
    } finally {
      S.busy = false;
    }
  }

  // ---------------------------------------------------------------- wiring
  function setTab(tab) {
    S.tab = tab;
    $$("#ag-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.agtab === tab));
    $("#ag-panel-reliability").hidden = tab !== "reliability";
    $("#ag-panel-conflicts").hidden = tab !== "conflicts";
    if (tab === "reliability") loadReliability();
    else { loadConflicts(); }
  }

  function setScope(scope) {
    S.scope = scope;
    $$("#ag-scope button").forEach((b) => b.classList.toggle("active", b.dataset.agscope === scope));
    S.field = "";
    S.study = "";
    S.viewer = "";
    S.viewerStudy = null;
    S.pages = null;
    S.page = 1;
    $("#ag-confnote").innerHTML = scope === "abstract"
      ? "Click a coder's value to settle the conflict; the Master's file is <code>Screening/&lt;master&gt;.json</code>."
      : "Click a rater's value to settle the conflict with it; the Master's file is <code>Codings/&lt;Key&gt; - &lt;master&gt;.json</code>.";
    if (S.tab === "reliability") loadReliability();
    else loadConflicts();
    loadLlmExcluded();
  }

  function defaultExclude() {
    // The Master is not an independent rater for the coefficients; include them
    // again by clicking their name.
    S.exclude = new Set(S.master ? [S.master] : []);
  }

  async function boot() {
    try { S.master = localStorage.getItem("cs.master") || ""; } catch { /* ignore */ }
    const m = $("#ag-master");
    if (m) m.value = S.master;
    defaultExclude();

    m?.addEventListener("input", () => {
      S.master = m.value;
      try { localStorage.setItem("cs.master", S.master); } catch { /* ignore */ }
    });
    m?.addEventListener("change", () => {
      defaultExclude();
      if (S.tab === "reliability") loadReliability();
      else loadConflicts();
    });
    $$("#ag-tabs button").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.agtab)));
    $$("#ag-scope button").forEach((b) => b.addEventListener("click", () => setScope(b.dataset.agscope)));
    $("#ag-status")?.addEventListener("change", (e) => { S.status = e.target.value; loadConflicts(); });
    $("#ag-fast")?.addEventListener("click", () => setFast(!S.fast));
    $("#ag-show-settled")?.addEventListener("click", () => {
      S.showSettled = !S.showSettled;
      const b = $("#ag-show-settled");
      if (b) b.setAttribute("aria-pressed", S.showSettled ? "true" : "false");
      renderStudyList();
    });
    document.addEventListener("keydown", fastKeydown);

    initAgFind();

    $("#ag-raters")?.addEventListener("click", (e) => {
      const b = e.target.closest(".ag-rater");
      if (!b) return;
      const rater = b.dataset.rater || "";
      if (!rater) return;
      if (S.exclude.has(rater)) S.exclude.delete(rater); else S.exclude.add(rater);
      loadReliability();
    });

    $("#ag-studies")?.addEventListener("click", (e) => {
      const b = e.target.closest(".ag-studybtn");
      if (!b) return;
      S.study = b.dataset.study || "";
      S.shown = 0;
      resetAgFind();
      loadConflicts();
    });

    $$("#ag-viewerseg button").forEach((b) => b.addEventListener("click", () => {
      S.viewer = b.dataset.agviewer;
      if (S.viewer !== "pdf") resetAgFind();
      syncViewerSeg();
      loadViewer();
      if (S.viewer === "pdf") requestAnimationFrame(() => { if (agFind) agFind.refresh(); });
    }));

    $("#ag-fields")?.addEventListener("click", (e) => {
      const b = e.target.closest(".ag-fieldbtn");
      if (!b) return;
      S.field = b.dataset.field || "";
      loadConflicts();
    });

    $("#ag-conflist")?.addEventListener("click", (e) => {
      const card = e.target.closest(".ag-confcard");
      if (!card) return;
      const act = e.target.closest("[data-act]");
      if (!act) { if (S.fast) activateCase(card); return; }
      if (act.classList.contains("ag-levelset")) {
        const sel = card.querySelector(".ag-levelsel");
        const txt = card.querySelector(".ag-leveltext");
        const value = sel ? sel.value : (txt ? txt.value.trim() : "");
        if (!value) return;
        resolveCard(card, value);
        return;
      }
      if (act.dataset.act === "set") resolveCard(card, act.dataset.value);
      else if (act.dataset.act === "clear") resolveCard(card, null);
    });

    // the "all coders are wrong" dropdown settles as soon as a level is chosen
    $("#ag-conflist")?.addEventListener("change", (e) => {
      const sel = e.target.closest(".ag-levelsel");
      if (!sel || !S.fast) return;
      const card = sel.closest(".ag-confcard");
      if (card && sel.value) resolveCard(card, sel.value);
    });

    $("#ag-pdf-controls")?.addEventListener("click", (e) => {
      if (e.target.closest("#ag-fit-toggle")) { S.fit = !S.fit; agApplyFit(); }
    });
    $("#ag-page-input")?.addEventListener("change", (e) => {
      const n = Math.max(1, Math.min(S.pages || 1, Number(e.target.value) || 1));
      S.page = n;
      agScrollToPage(n);
      agRenderControls();
    });

    $("#ag-llmexcl")?.addEventListener("click", (e) => {
      const b = e.target.closest("[data-act='open-screen']");
      if (b) openInScreening(b.dataset.uid);
    });

    setTab("reliability");
    loadLlmExcluded();
  }

  async function ensureStarted() {
    if (S.started) return;
    S.started = true;
    try { await boot(); }
    catch (e) {
      const host = $("#ag-rel-table");
      if (host) host.innerHTML = `<div class="ag-empty">Could not start the agreement tab: ${esc(e.message || e)}</div>`;
    }
  }

  document.addEventListener("cs:view", (e) => { if (e.detail === "agreement") ensureStarted(); });
  function boot2() {
    let saved = "coding";
    try { saved = localStorage.getItem("cs.view") || "coding"; } catch { /* ignore */ }
    if (saved === "agreement") ensureStarted();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot2);
  else boot2();
})();
