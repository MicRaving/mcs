/* ============================================================
   Coding Studio — Analysis tab
   Publication-bias tables + figures, driven by the Flask
   endpoints under /api/analysis/*.
   ============================================================ */
(() => {
  "use strict";

  const { $, $$, esc, toast, api } = window.CS;

  const state = {
    overview: null,
    layer: "meta",
    spec: "post_all",
    exclusions: "on",
    pre: "all",
    fu: "all",
    cred: false,
    fullyWithin: false,   // include fully-within (single-arm, no-control) studies
    model: "freq",
    muSd: 10,
    tauSd: 0.5,
    tauPrior: "half-normal",
    filterName: "",
    filterValue: "",
    table: "robustness",
    figure: null,
    figLayer: "all",
    figSpec: "all",
    figKind: "all",
    polling: null,
    runActive: false,     // a backend analysis run is in progress
    modFitting: false,    // a /api/analysis/moderate request is in flight
    mods: null,          // moderator catalogue
    selMods: new Set(),  // selected moderator names
    interactions: [],    // [ [a,b], ... ]
    modResult: null,
    sideFigure: null,
    modFigures: [],       // one figure per selected moderator (right rail)
    modComponents: null,
    modPanelInited: false,
  };

  const SPEC_ORDER = ["did_all", "did_cred", "post_all", "post_cred",
                      "persistence_all", "persistence_cred",
                      "didpost_all", "didpost_cred", "fupost_all", "fupost_cred",
                      "fudidpost_all", "fudidpost_cred"];
  const KIND_ORDER = ["forest", "forest_four_specs", "funnel", "loo", "moderator",
                      "petpeese", "sdtcomponents", "domain", "intervention_types", "exclusions"];
  const KIND_LABEL = {
    forest: "Forest (overall + domains)", funnel: "Funnel (with Egger line)",
    loo: "Leave-one-out", moderator: "Data-availability moderator", petpeese: "PET-PEESE",
    sdtcomponents: "SDT components (true / fake / response bias)",
    domain: "Susceptibility domains", forest_four_specs: "Four specifications",
    intervention_types: "By intervention type", exclusions: "Exclusions sensitivity",
  };
  // Paper-ready labels (no internal codes).
  const LAYER_LABEL = { meta: "Meta-analysis (reported effects)",
                        sdt: "Re-analysis (signal detection)" };
  const SPEC_LABEL = {
    did_all: "Pretest-corrected difference (DiD), all outcomes",
    did_cred: "Pretest-corrected difference (DiD), credibility",
    post_all: "Posttest, all outcomes",
    post_cred: "Posttest, credibility",
    persistence_all: "Follow-up (persistence), all outcomes",
    persistence_cred: "Follow-up (persistence), credibility",
    didpost_all: "Pretest-corrected where possible, all outcomes",
    didpost_cred: "Pretest-corrected where possible, credibility",
    fupost_all: "Follow-up where available, all outcomes",
    fupost_cred: "Follow-up where available, credibility",
    fudidpost_all: "Follow-up, else pretest-corrected, all outcomes",
    fudidpost_cred: "Follow-up, else pretest-corrected, credibility",
  };
  const SPEC_SHORT = {
    did_all: "DiD - all outcomes", did_cred: "DiD - credibility",
    post_all: "Posttest - all outcomes", post_cred: "Posttest - credibility",
    persistence_all: "Follow-up - all outcomes", persistence_cred: "Follow-up - credibility",
    didpost_all: "DiD where possible - all outcomes", didpost_cred: "DiD where possible - credibility",
    fupost_all: "Follow-up where available - all outcomes",
    fupost_cred: "Follow-up where available - credibility",
    fudidpost_all: "Follow-up else DiD - all outcomes",
    fudidpost_cred: "Follow-up else DiD - credibility",
  };
  const EXCL_LABEL = { on: "Author exclusions applied", off: "Author exclusions ignored" };
  const COLUMN_LABEL = {
    layer: "Layer", spec: "Specification", exclusions: "Exclusions",
    study_key: "Study", experiment_id: "Experiment", k: "k",
    n_studies: "Studies", n_quality_excluded: "Excluded (quality guard)",
    n_data_available: "With primary data", n_paper_only: "Paper only",
    n_fallback_sharing: "Sharing fallback",
    dl_est: "Effect (DL, single-level)", est: "Effect", dl_se: "SE", se: "SE",
    three_est: "Effect (multilevel)", three_lo: "95% CI low (multilevel)",
    three_hi: "95% CI high (multilevel)", two_est: "Effect (two-level)",
    clus_est: "Effect (cluster-robust)", clus_se: "Cluster-robust SE",
    dl_lo: "95% CI low", lo: "95% CI low", dl_hi: "95% CI high", hi: "95% CI high",
    dl_tau2: "Between-study variance", tau2: "Between-study variance", dl_i2: "I-squared (%)",
    i2: "I-squared (%)", two_est: "Two-level random effects", clus_est: "Cluster-robust",
    clus_se: "Cluster-robust SE", n_clusters: "Clusters",
    egger_intercept: "PET (Egger intercept)", egger_se: "PET SE", egger_t: "PET t",
    egger_p: "PET p", egger_slope: "Egger's test slope", egger_slope_se: "Egger slope SE",
    egger_slope_t: "Egger slope t", egger_slope_p: "Egger's test p",
    egger_status: "Egger's test",
    pet: "PET", pet_p: "PET p", peese: "PEESE", peese_p: "PEESE p",
    pet_peese_prefer: "Preferred estimator", pet_peese_est: "PET-PEESE estimate",
    tf_k0: "Trim-and-fill studies added", tf_side: "Trim-and-fill side",
    tf_obs_est: "Observed effect", tf_adj_est: "Adjusted effect", tf_adj_se: "Adjusted SE",
    tf_status: "Trim-and-fill",
    pc_n_sig: "p-curve n (p < .05)", pc_n_below_025: "p < .025",
    pc_prop_below_025: "Proportion p < .025", pc_p_right_skew: "Right-skew p",
    p_uniform_delta: "p-uniform estimate", pcurve_status: "p-curve",
    moderator: "Moderator", level_label: "Level", level: "Level", p: "p",
    moderator_status: "Moderator test",
    dv_domain: "Outcome domain", metric: "Effect metric",
    intervention_type: "Intervention type",
    on_k: "k (applied)", on_est: "Effect (applied)", off_k: "k (ignored)",
    off_est: "Effect (ignored)", delta_off_minus_on: "Change (ignored - applied)",
    unit_type: "Unit", omitted: "Omitted", k_omitted: "k (remaining)",
    full_est: "Full estimate", delta: "Change", influential: "Influential",
    study: "Study", role: "Role", ambiguous: "Ambiguous", n: "n",
    effect_raw: "Effect (unoriented)", effect: "Effect", source: "Source",
    artifact: "Experiment", domain: "Outcome domain", timepoint: "Timepoint",
    veracity: "Veracity", fallback: "Fallback", did_basis: "DiD basis",
    exclusions_embedded: "Exclusions embedded", cond_ambiguous: "Condition ambiguous",
    oriented: "Polarity applied", reported_domain: "Susceptibility domain",
    d_ig: "Discrimination (intervention)", d_cg: "Discrimination (control)",
  };

  function pretty(c) {
    if (COLUMN_LABEL[c]) return COLUMN_LABEL[c];
    const s = String(c).replace(/_/g, " ").trim();
    if (s.length <= 3) return s === "p" ? "p" : s.toUpperCase();
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  function prettyTitle(parts) {
    const kind = KIND_LABEL[parts[0]] || pretty(parts[0]);
    const layer = LAYER_LABEL[parts[1]] || "";
    const spec = SPEC_SHORT[parts[2]] || "";
    return [kind, layer, spec].filter(Boolean).join(" \u00b7 ");
  }

  const METRIC_LABEL = {
    g: "Standardized mean difference", d: "Standardized mean difference",
    z: "Fisher z", beta: "Regression coefficient", yi: "Reported effect",
    reported: "Reported effect", "d_sdt": "Discrimination (d')",
    "d_sdt_did": "Change in discrimination",
  };
  const LEVEL_LABEL = {
    data_available: "Primary data available", paper_only: "Paper only",
    slope: "Difference (meta-regression)",
  };
  // 2026-09-19: belief_accuracy merged into credibility (retired label omitted).
  const DOMAIN_LABEL = {
    credibility: "Credibility",
    sharing_intention: "Sharing intention", technique_recognition: "Technique recognition",
    attitude_policy: "Attitude / policy",
    search_behavior: "Search behavior", trust: "Trust",
    media_literacy_attitudes: "Media-literacy attitudes",
    critical_thinking: "Critical thinking", information_overload: "Information overload",
    echo_chamber: "Echo chamber", general_transfer: "General transfer", other: "Other",
  };

  function valueLabel(col, v) {
    if (v === null || v === undefined || v === "") return v ?? "";
    if (col === "spec") return SPEC_SHORT[v] || v;
    if (col === "layer") return LAYER_LABEL[v] || v;
    if (col === "exclusions") return EXCL_LABEL[v] || v;
    if (col === "level_label" || col === "moderator") return LEVEL_LABEL[v] || v;
    if (col === "metric") return METRIC_LABEL[v] || v;
    if (col === "dv_domain" || col === "domain") return DOMAIN_LABEL[v] || pretty(v);
    if (col === "veracity") return v === "true_vs_false" ? "True vs. false items" : v;
    return v;
  }

  // ---------------------------------------------------------------- helpers
  const num = (v) => {
    if (v === null || v === undefined || v === "" || v === "nan" || v === "NaN") return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  };
  const fmt = (v, d = 2) => {
    const n = num(v);
    if (n === null) return v === "" || v === null || v === undefined ? "–" : String(v);
    if (n !== 0 && Math.abs(n) < 0.001) return n.toExponential(2);
    return n.toFixed(d);
  };


  async function fetchTable(name) {
    const p = new URLSearchParams({ name, limit: "3000" });
    if (state.layer) p.set("layer", state.layer);
    if (state.spec) p.set("spec", state.spec);
    p.set("exclusions", state.exclusions);
    p.set("fully_within", state.fullyWithin ? "1" : "0");
    return api(`/api/analysis/table?${p}`);
  }

  function renderTable(columns, rows, { numeric = "auto" } = {}) {
    if (!rows || !rows.length) return `<div class="an-empty">No rows for this selection.</div>`;
    const isNum = {};
    const isInt = {};
    columns.forEach((c) => {
      if (numeric === "all") { isNum[c] = true; return; }
      let ok = 0, seen = 0, allInt = true;
      for (const r of rows) {
        const v = r[c];
        if (v === "" || v === null || v === undefined) continue;
        seen++;
        const n = num(v);
        if (n !== null) {
          ok++;
          if (Math.abs(n - Math.round(n)) > 1e-9) allInt = false;
        }
      }
      isNum[c] = seen > 0 && ok / seen > 0.7;
      isInt[c] = isNum[c] && allInt;
    });
    const isBool = (c) => /influential|flag|oriented|_embedded|ambiguous|^is_|restricted/i.test(c) &&
      rows.some((r) => ["0", "1"].includes(String(r[c]))) &&
      rows.every((r) => r[c] === "" || r[c] === null || ["0", "1"].includes(String(r[c])));
    const head = columns.map((c) => `<th>${esc(pretty(c))}</th>`).join("");
    const isPCol = (c) => /(^|_)p(_|$)|_p$|pval|p_value/i.test(c);
    const body = rows.map((r) => "<tr>" + columns.map((c) => {
      const v = r[c];
      if (isBool(c) && ["0", "1"].includes(String(v))) {
        const on = String(v) === "1";
        return `<td class="bool-${on ? "true" : "false"}">${on ? "yes" : "no"}</td>`;
      }
      if (isPCol(c) && num(v) !== null) return `<td class="num">${esc(fmtP(v))}</td>`;
      if (isNum[c]) return `<td class="num">${esc(fmt(v, isInt[c] ? 0 : 2))}</td>`;
      return `<td>${esc(valueLabel(c, v))}</td>`;
    }).join("") + "</tr>").join("");
    return `<table class="an-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  }

  function kpi(label, value, sub = "", cls = "", title = "") {
    return `<div class="kpi ${cls}"${title ? ` title="${esc(title)}"` : ""}>` +
      `<div class="k">${esc(label)}</div>` +
      `<div class="v">${value}</div>${sub ? `<div class="s">${sub}</div>` : ""}</div>`;
  }

  // ---------------------------------------------------------------- markdown
  function inline(md) {
    return esc(md)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  }

  function renderMarkdown(md) {
    const lines = String(md || "").replace(/\r\n/g, "\n").split("\n");
    const out = [];
    let i = 0, list = null;
    const flushList = () => { if (list) { out.push(`<${list.tag}>${list.items.join("")}</${list.tag}>`); list = null; } };
    while (i < lines.length) {
      const line = lines[i];
      if (/^```/.test(line)) {
        flushList();
        const buf = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
        i++;
        out.push(`<pre><code>${esc(buf.join("\n"))}</code></pre>`);
        continue;
      }
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
        flushList();
        const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
        const head = cells(line);
        i += 2;
        const rows = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(cells(lines[i++]));
        out.push("<table><thead><tr>" + head.map((c) => `<th>${inline(c)}</th>`).join("") +
          "</tr></thead><tbody>" + rows.map((r) => "<tr>" + r.map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>").join("") +
          "</tbody></table>");
        continue;
      }
      const h = line.match(/^(#{1,6})\s+(.*)$/);
      if (h) { flushList(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); i++; continue; }
      const ul = line.match(/^\s*[-*]\s+(.*)$/);
      const ol = line.match(/^\s*\d+\.\s+(.*)$/);
      if (ul || ol) {
        const tag = ul ? "ul" : "ol";
        if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
        list.items.push(`<li>${inline((ul || ol)[1])}</li>`);
        i++; continue;
      }
      if (/^\s*>\s?/.test(line)) {
        flushList();
        out.push(`<blockquote>${inline(line.replace(/^\s*>\s?/, ""))}</blockquote>`);
        i++; continue;
      }
      if (!line.trim()) { flushList(); i++; continue; }
      flushList();
      out.push(`<p>${inline(line)}</p>`);
      i++;
    }
    flushList();
    return out.join("\n");
  }

  // ---------------------------------------------------------------- view toggle
  function showView(view) {
    const views = { coding: $("#view-coding"), screening: $("#view-screening"),
                    agreement: $("#view-agreement"),
                    coverage: $("#view-coverage"),
                    summary: $("#view-summary"), analysis: $("#view-analysis"),
                    reproduction: $("#view-reproduction"),
                    scheme: $("#view-scheme") };
    Object.entries(views).forEach(([name, el]) => { if (el) el.hidden = name !== view; });
    $$(".viewtab").forEach((b) => {
      const active = b.dataset.view === view;
      b.classList.toggle("active", active);
      b.setAttribute("aria-selected", String(active));
    });
    try { localStorage.setItem("cs.view", view); } catch { /* ignore */ }
    document.body.classList.toggle("view-analysis", view === "analysis");
    document.body.classList.toggle("view-screening", view === "screening");
    document.body.classList.toggle("view-agreement", view === "agreement");
    document.body.classList.toggle("view-summary", view === "summary");
    document.body.classList.toggle("view-reproduction", view === "reproduction");
    document.body.classList.toggle("view-scheme", view === "scheme");
    document.dispatchEvent(new CustomEvent("cs:view", { detail: view }));
    if (view === "analysis" && !state.overview) refresh();
    if (view === "analysis") window.dispatchEvent(new Event("resize"));
  }

  // The scheme editor is a view of its own, and it needs to come back to
  // whatever view the user came from.
  window.CS.showView = showView;

  // ---------------------------------------------------------------- selects
  function fillSelect(el, values, current, labeler = (v) => v) {
    el.innerHTML = values.map((v) =>
      `<option value="${esc(v)}"${String(v) === String(current) ? " selected" : ""}>${esc(labeler(v))}</option>`).join("");
  }

  function specFromControls() {
    const cred = state.cred ? "cred" : "all";
    let base;
    if (state.pre === "only") base = "did";               // pretest-only (DiD) wins
    else if (state.fu === "delayed") base = "persistence"; // follow-up only
    else if (state.fu === "all") base = (state.pre === "all") ? "fudidpost" : "fupost";
    else if (state.pre === "all") base = "didpost";        // immediate / no follow-up
    else base = "post";
    return `${base}_${cred}`;
  }

  // Recover the two timepoint controls from a specification code.
  function controlsFromSpec(spec) {
    const m = /^(post|did|didpost|fudidpost|persistence|fupost)_(all|cred)$/.exec(String(spec || ""));
    if (!m) return;
    const base = m[1];
    state.cred = m[2] === "cred";
    if (base === "did") { state.pre = "only"; state.fu = "none"; }
    else if (base === "didpost") { state.pre = "all"; state.fu = "none"; }
    else if (base === "fudidpost") { state.pre = "all"; state.fu = "all"; }
    else if (base === "persistence") { state.pre = "-"; state.fu = "delayed"; }
    else if (base === "fupost") { state.pre = "-"; state.fu = "all"; }
    else { state.pre = "-"; state.fu = "immediate"; }
  }

  function syncControls() {
    $$("#an-layer-switch button").forEach((b) =>
      b.classList.toggle("active", b.dataset.layer === state.layer));
    $$("#an-model-switch button").forEach((b) =>
      b.classList.toggle("active", b.dataset.model === state.model));
    const priors = $("#an-priors"); if (priors) priors.hidden = state.model !== "bayes";
    const mu = $("#an-mu-prior"); if (mu) mu.value = String(state.muSd);
    const tp = $("#an-tau-prior"); if (tp) tp.value = `${state.tauPrior}|${state.tauSd}`;
    const pre = $("#an-pre"); if (pre) pre.value = state.pre || "-";
    const fu = $("#an-fu"); if (fu) fu.value = state.fu || "none";
    const cred = $("#an-cred"); if (cred) cred.checked = !!state.cred;
    const ex = $("#an-excl-on"); if (ex) ex.checked = state.exclusions !== "off";
    const fw = $("#an-fully-within"); if (fw) fw.checked = !!state.fullyWithin;
  }

  function applyOverviewFilters() {
    state.exclusions = state.exclusions === "off" ? "off" : "on";
    state.spec = specFromControls();
    const specs = state.overview?.specs || [];
    let row = specs.find((s) => s.layer === state.layer && s.spec === state.spec
      && s.exclusions === state.exclusions);
    state.specFallback = false;
    if (!row && specs.length) {
      const alt = specs.find((s) => s.layer === state.layer && s.spec === state.spec)
        || specs.find((s) => s.layer === state.layer) || specs[0];
      state.specFallback = alt.spec !== state.spec;
      state.wantedSpec = state.spec;
      state.layer = alt.layer;
      controlsFromSpec(alt.spec);
      state.exclusions = alt.exclusions;
      state.spec = alt.spec;
    }
    syncControls();
  }

  // ---------------------------------------------------------------- render
  function currentSpecRow() {
    return (state.overview?.specs || []).find((s) =>
      s.layer === state.layer && s.spec === state.spec && s.exclusions === state.exclusions) || null;
  }

  // Bayes-factor evidence labels (Jeffreys / Lee & Wagenmakers).
  function bfLabel(bf) {
    if (bf === null || bf === undefined || !isFinite(bf)) return "not estimated";
    if (bf >= 100) return "extreme evidence";
    if (bf >= 30) return "very strong";
    if (bf >= 10) return "strong";
    if (bf >= 3) return "moderate";
    if (bf > 1) return "anecdotal";
    return "favours the null";
  }

  function bfFmt(v) {
    const n = num(v);
    if (n === null) return "–";
    if (!isFinite(n)) return ">1,000,000";
    if (n >= 1e6) return ">1,000,000";
    if (n > 1000) return ">1,000";
    if (n <= 1 / 1e6) return "<1/1,000,000";
    if (n < 1 / 1000) return "<1/1,000";
    return n.toFixed(3);
  }

  // W6-AN: the normal-normal Bayes factor over thousands of contrasts can
  // overflow to ~1e200.  The backend sends a guarded presentation
  // (`*_present`: {display, log10, capped, note}); fall back to a local cap when
  // an older backend omits it.  The posterior mean and P(effect > 0) are the
  // headline; the BF is never rendered raw.
  function bfPresent(present, rawValue) {
    if (present && typeof present === "object" && present.display) {
      return { display: present.display, log10: num(present.log10),
               capped: !!present.capped, note: present.note || "" };
    }
    const n = num(rawValue);
    const capped = n !== null && (!isFinite(n) || n >= 1e6 || n <= 1 / 1e6);
    return { display: bfFmt(rawValue), log10: (n !== null && isFinite(n) && n > 0) ? Math.log10(n) : null,
             capped, note: capped ? "raw Bayes factor is numerically extreme" : "" };
  }

  function bfSub(present, rawValue) {
    const bits = [];
    bits.push(present.capped ? "effectively certain" : bfLabel(num(rawValue)));
    if (present.log10 !== null) bits.push(`log\u2081\u2080 BF = ${present.log10.toFixed(1)}`);
    return bits.join(" \u00b7 ");
  }

  function renderKpis(pb, pool, bayes, sdtc, estimand = "") {
    const row = (pool.rows || []).find((r) => r.layer === state.layer) || {};
    const pbr = (pb.rows || [])[0] || {};
    const k = row.k ?? currentSpecRow()?.k ?? "–";
    // Headline = multilevel estimate (three-level, then two-level), with DL as
    // the single-level comparison rather than the primary model.
    const est = num(row.three_est) ?? num(row.two_est) ?? num(row.dl_est);
    const lo = num(row.three_lo) ?? num(row.two_lo) ?? num(row.dl_lo);
    const hi = num(row.three_hi) ?? num(row.two_hi) ?? num(row.dl_hi);
    const eggerP = num(pbr.egger_slope_p);   // Egger's asymmetry test = slope on SE
    const tfK0 = num(pbr.tf_k0);
    const tfa = num(pbr.tf_adj_est);
    const i2 = num(row.dl_i2), tau2 = num(row.dl_tau2);
    const stamp = (state.overview?.tables || []).find((t) => t.key === "publication_bias")?.modified;
    const gen = state.overview?.xlsx
      ? (stamp ? stamp.replace("T", " ") : "no tables yet")
      : "no results file";
    $("#an-gen").textContent = gen + (state.model === "bayes" ? " · Bayes" : "");
    const comp = {};
    ((sdtc && sdtc.rows) || []).forEach((r) => { comp[r.component] = r; });
    const compCard = (name, label) => comp[name]
      ? kpi(label || name, fmt(comp[name].est),
            `95% CI [${fmt(comp[name].lo)}, ${fmt(comp[name].hi)}]`)
      : kpi(label || name, "–", "not estimated");
    const bias = state.layer === "sdt"
      ? [compCard("True score"), compCard("Fake score"),
         compCard("Response bias (criterion c)", "Response bias (c)")]
      : [
        kpi("Egger's test",
          num(pbr.egger_slope) !== null ? fmt(pbr.egger_slope, 3) : "–",
          eggerP !== null ? `p = ${fmtP(eggerP)}` : "not assessed",
          eggerP !== null && eggerP < 0.05 ? "warn" : ""),
        kpi("PET", num(pbr.egger_intercept) !== null ? fmt(pbr.egger_intercept) : "–",
          num(pbr.egger_p) !== null ? `p = ${fmtP(num(pbr.egger_p))}` : "not assessed"),
        kpi("PET-PEESE", pbr.pet_peese_prefer ? fmt(pbr.pet_peese_est) : "–",
          pbr.pet_peese_prefer ? `prefer ${pbr.pet_peese_prefer}` : "not assessed"),
        kpi("Trim-and-fill", tfK0 !== null ? `${tfK0}` : "–",
          tfa !== null ? `adjusted ${fmt(tfa)}` : "not assessed"),
      ];
    let cards;
    if (state.model === "bayes" && bayes && bayes.ok && bayes.pooled) {
      const bp = bayes.pooled;
      const bf10p = bfPresent(bp.bf10_present, bp.bf10);
      const bf01p = bfPresent(bp.bf01_tau0_present, bp.bf01_tau0);
      const bb = bayes.bias || null;
      const bayesBias = bb ? [
        kpi("PET (Bayes)", fmt(bb.pet), `95% CrI [${fmt(bb.pet_lo)}, ${fmt(bb.pet_hi)}]`,
            "", "Bayesian SE-regression intercept (bias-adjusted effect)."),
        kpi("Egger slope (Bayes)", fmt(bb.egger_slope),
            `95% CrI [${fmt(bb.egger_slope_lo)}, ${fmt(bb.egger_slope_hi)}]`,
            bb.egger_p_pos !== null && bb.egger_p_pos !== undefined && bb.egger_p_pos > 0.95 ? "warn" : "",
            "Bayesian small-study asymmetry: posterior slope of the effect on its standard error."),
      ] : [kpi("Bayes bias", "–", "not assessed")];
      cards = [
        kpi("Contrasts (k)", esc(bp.k), `${esc(bp.n_studies ?? "–")} studies`),
        kpi("Pooled effect (Bayes)", fmt(bp.est), `95% CrI [${fmt(bp.lo)}, ${fmt(bp.hi)}]`,
            "", "Posterior mean of the pooled effect (the intended headline)."),
        kpi("P(effect > 0)", fmt(bp.p_gt0, 3), "posterior", bp.p_gt0 >= 0.95 ? "" : "warn",
            "Posterior probability that the pooled effect is positive."),
        kpi("τ posterior", fmt(bp.tau, 3),
          `95% CrI [${fmt(bp.tau_lo, 3)}, ${fmt(bp.tau_hi, 3)}]`),
        kpi("BF₁₀ (effect)", esc(bf10p.display),
          bfSub(bf10p, bp.bf10) +
          (bp.p !== null && bp.p !== undefined ? ` · p = ${fmtP(bp.p)}` : ""),
          "", bf10p.note),
        kpi("BF₀₁ (τ = 0)", esc(bf01p.display), "heterogeneity", "", bf01p.note),
        ...bayesBias,
        ...bias,
      ];
    } else if (state.model === "bayes") {
      cards = [
        kpi("Contrasts (k)", esc(k), `${esc(row.n_studies ?? "–")} studies`),
        kpi("Pooled effect (Bayes)", "–", "not estimated"),
        ...bias,
      ];
    } else {
      cards = [
        kpi("Contrasts (k)", esc(k), `${esc(row.n_studies ?? "–")} studies`),
        kpi("Pooled effect (multilevel)", esc(est !== null ? fmt(est) : "–"),
          est !== null ? `95% CI [${fmt(lo)}, ${fmt(hi)}]` : "not estimated"),
        kpi("Heterogeneity", i2 !== null ? `${fmt(i2, 0)}%` : "–",
          tau2 !== null ? `I² · τ² = ${fmt(tau2, 2)}` : "not estimated",
          i2 !== null && i2 >= 75 ? "warn" : ""),
        ...bias,
      ];
    }
    $("#an-kpis").innerHTML = cards.join("") +
      (estimand ? `<p class="an-note an-estimand" title="${esc(estimand)}">` +
        `Estimand: ${esc(estimand)}</p>` : "");
  }

  function summaryBlock(title, columns, rows, opts) {
    if (!rows || !rows.length) return "";
    return `<div><h3>${esc(title)}</h3>${renderTable(columns, rows, opts)}</div>`;
  }

  function filterParams(p) {
    if (state.filterName && state.filterValue) {
      p.set("filter_name", state.filterName);
      p.set("filter_value", state.filterValue);
    }
    p.set("fully_within", state.fullyWithin ? "1" : "0");
    return p;
  }

  async function fetchSubgroup(by, bayes = false) {
    const p = new URLSearchParams({ by, layer: state.layer, spec: state.spec,
                                    exclusions: state.exclusions });
    if (bayes) p.set("model", "bayes");
    filterParams(p);
    try { return await api(`/api/analysis/subgroup?${p}`); } catch { return { rows: [] }; }
  }

  async function fetchPooled() {
    const p = new URLSearchParams({ layer: state.layer, spec: state.spec,
                                    exclusions: state.exclusions,
                                    scope: (state.cred ? "restricted" : "unrestricted") });
    filterParams(p);
    try { return await api(`/api/analysis/pooled?${p}`); } catch { return null; }
  }

  async function fetchBayes(sensitivity = false) {
    const p = new URLSearchParams({ layer: state.layer, spec: state.spec,
                                    exclusions: state.exclusions,
                                    scope: (state.cred ? "restricted" : "unrestricted"),
                                    mu_sd: String(state.muSd),
                                    tau_sd: String(state.tauSd),
                                    tau_prior: state.tauPrior });
    filterParams(p);
    if (sensitivity) p.set("sensitivity", "1");
    try { return await api(`/api/analysis/bayes?${p}`); } catch { return null; }
  }

  async function showSensitivity() {
    const host = $("#an-sens-panel");
    if (!host) return;
    host.hidden = false;
    host.innerHTML = `<div class="an-empty">Computing prior sensitivity…</div>`;
    const d = await fetchBayes(true);
    if (!d || !d.ok || !d.sensitivity) {
      host.innerHTML = `<div class="an-empty">Prior sensitivity unavailable for this selection.</div>`;
      return;
    }
    const muRows = (d.sensitivity.mu || []).map((r) => `<tr>
      <td>N(0, ${esc(r.mu_sd)}²)</td><td class="num">${esc(bfFmt(r.bf10))}</td>
      <td class="num">${esc(fmt(r.est))}</td><td class="num">${esc(fmt(r.lo))}, ${esc(fmt(r.hi))}</td></tr>`).join("");
    const tauRows = (d.sensitivity.tau || []).map((r) => `<tr>
      <td>${esc(r.tau_prior)}(${esc(r.tau_sd)})</td><td class="num">${esc(bfFmt(r.bf01_tau0))}</td>
      <td class="num">${esc(fmt(r.tau, 3))}</td><td class="num">${esc(bfFmt(r.bf10))}</td></tr>`).join("");
    host.innerHTML =
      `<p class="an-note">Prior sensitivity — the posterior mean/CrI are stable, but Bayes factors move ` +
      `with the prior. Selected: μ ~ N(0, ${esc(d.prior.mu_sd)}²), τ ~ ${esc(d.prior.tau_prior)}(${esc(d.prior.tau_sd)}).</p>` +
      `<div class="an-sens-grid">` +
      `<div><h4>Effect prior (μ)</h4><table class="an-table"><thead><tr><th>Prior</th><th>BF₁₀</th><th>Mean</th><th>95% CrI</th></tr></thead><tbody>${muRows}</tbody></table></div>` +
      `<div><h4>Heterogeneity prior (τ)</h4><table class="an-table"><thead><tr><th>Prior</th><th>BF₀₁(τ=0)</th><th>τ</th><th>BF₁₀</th></tr></thead><tbody>${tauRows}</tbody></table></div>` +
      `</div>`;
  }

  async function loadSummary() {
    const bayesOn = state.model === "bayes";
    const filtered = !!(state.filterName && state.filterValue);
    const [pb, pool, mod, dom, ex, age, itype, bayes, fpool, sdtc] = await Promise.all([
      fetchTable("publication_bias"), fetchTable("pooled"), fetchTable("moderator"),
      fetchTable("domain"), fetchTable("exclusions_onoff"),
      fetchSubgroup("Age_Group", bayesOn), fetchSubgroup("Intervention_Type", bayesOn),
      bayesOn ? fetchBayes() : Promise.resolve(null),
      fetchPooled(),
      state.layer === "sdt" ? fetchTable("sdt_components") : Promise.resolve({ rows: [], columns: [] }),
    ]);
    let poolUse = pool;
    if (filtered && fpool && fpool.ok) {
      const dl = fpool.dl || {}, two = fpool.two_level || {}, cl = fpool.cluster || {};
      poolUse = {
        rows: [{ layer: state.layer, spec: state.spec, exclusions: state.exclusions,
                  k: fpool.k, n_studies: fpool.n_studies,
                  dl_est: dl.est, dl_lo: dl.lo, dl_hi: dl.hi, dl_tau2: dl.tau2, dl_i2: dl.i2,
                  two_est: two.est, clus_est: cl.est, clus_se: cl.se }],
      };
    } else if (filtered && fpool && !fpool.ok) {
      poolUse = { rows: [] };
    }
    renderKpis(pb, poolUse, bayes, sdtc, (fpool && fpool.estimand) || "");
    const pbCols = ["k", "n_studies", "n_quality_excluded",
      "egger_slope", "egger_slope_p", "egger_intercept", "egger_p",
      "pet", "peese", "pet_peese_prefer", "pet_peese_est", "tf_k0", "tf_adj_est",
      "pc_n_sig", "pcurve_status"];
    const compCols = ["component", "k", "n_studies", "est", "lo", "hi", "tau2", "i2"];
    const poolCols = ["k", "n_studies", "three_est", "three_lo", "three_hi",
      "dl_est", "dl_lo", "dl_hi", "dl_tau2", "dl_i2", "two_est", "clus_est", "clus_se"];
    const modCols = ["moderator", "level_label", "k", "est", "lo", "hi", "p", "moderator_status"];
    const exCols = ["layer", "spec", "on_k", "on_est", "off_k", "off_est", "delta_off_minus_on"];
    const domCols = ["dv_domain", "k", "n_studies", "est", "lo", "hi", "i2"];
    const subCols = ["label", "k", "n_studies", "est", "lo", "hi", "i2"];
    const bSubCols = ["label", "k", "n_studies", "est", "lo", "hi", "tau", "p_gt0"];
    const bDomCols = ["dv_domain", "k", "n_studies", "est", "lo", "hi", "tau", "p_gt0"];
    const fnote = filtered
      ? `<p class="an-note">Subgroup filter: <b>${esc(filterLabel())}</b> — the overall estimate is ` +
        `restricted to this subgroup; the publication-bias tests remain on the full set.</p>`
      : "";
    const biasBlock = state.layer === "sdt"
      ? summaryBlock("SDT components — true score · fake score · response bias",
                     compCols, (sdtc.rows || []))
      : summaryBlock("Publication-bias battery",
                     pbCols.filter((c) => c in ((pb.rows || [])[0] || {})), pb.rows || []);
    const two = (fpool && fpool.ok && fpool.two_level && fpool.two_level.k) ? fpool.two_level : null;
    const varBlock = two
      ? summaryBlock("Variance decomposition (three-level model)",
          ["Level", "Component", "Variance", "Share of variance (%)"],
          [
            { Level: "Level 1", Component: "Sampling variance", Variance: fmt(two.vbar, 4),
              "Share of variance (%)": fmt(two.i2_sampling, 1) },
            { Level: "Level 2", Component: "τ² (between effects within studies)",
              Variance: fmt(two.tau2_within, 4),
              "Share of variance (%)": fmt(two.i2_within_alloc, 1) },
            { Level: "Level 3", Component: "σ² (between studies)",
              Variance: fmt(two.tau2_between, 4),
              "Share of variance (%)": fmt(two.i2_between_alloc, 1) },
          ])
      : "";
    let html;
    if (bayesOn && bayes && bayes.ok && bayes.pooled) {
      const bp = bayes.pooled;
      html = [
        summaryBlock("Pooled model — Bayesian random-effects (posterior mean · 95% credible interval)",
          ["k", "n_studies", "est", "lo", "hi", "tau", "bf10", "p", "bf01_tau0", "p_gt0"],
          [{ k: bp.k, n_studies: bp.n_studies, est: bp.est, lo: bp.lo, hi: bp.hi,
             tau: bp.tau, bf10: bfPresent(bp.bf10_present, bp.bf10).display, p: bp.p,
             bf01_tau0: bfPresent(bp.bf01_tau0_present, bp.bf01_tau0).display, p_gt0: bp.p_gt0 }]),
        (bayes.bias ? summaryBlock("Bayesian small-study-effect analysis (SE regression)",
          ["Term", "Estimate", "95% CrI"],
          [{ Term: "PET (SE-regression intercept)", Estimate: fmt(bayes.bias.pet),
             "95% CrI": `[${fmt(bayes.bias.pet_lo)}, ${fmt(bayes.bias.pet_hi)}]` },
           { Term: "Egger asymmetry (slope on SE)", Estimate: fmt(bayes.bias.egger_slope),
             "95% CrI": `[${fmt(bayes.bias.egger_slope_lo)}, ${fmt(bayes.bias.egger_slope_hi)}]` }]) : ""),
        summaryBlock("Effects by age (Bayes)", bSubCols, bayes.age),
        summaryBlock("Effects by intervention type (Bayes)", bSubCols, bayes.intervention),
        summaryBlock("By primary-data availability (Bayes)", bSubCols, bayes.data_available),
        summaryBlock("Per-domain estimates (Bayes)", bDomCols, (bayes.domain || []).slice(0, 20)),
        varBlock,
        biasBlock,
        summaryBlock("Exclusions ON vs OFF", exCols, ex.rows),
      ].join("");
    } else {
      html = [
        summaryBlock("Pooled models (DL · two-level · cluster-robust)", poolCols, poolUse.rows),
        varBlock,
        summaryBlock("Effects by age", subCols, age.rows),
        summaryBlock("Effects by intervention type", subCols, itype.rows),
        biasBlock,
        summaryBlock("Moderator: primary data availability vs paper only", modCols, mod.rows),
        summaryBlock("Exclusions ON vs OFF", exCols, ex.rows),
        summaryBlock("Per-domain estimates (this spec)", domCols, (dom.rows || []).slice(0, 20)),
      ].join("");
    }
    $("#an-summary").innerHTML = fnote + html ||
      `<div class="an-empty">No results yet. Click <b>Analyze</b> to generate them.</div>`;
  }

  function filterLabel() {
    const sel = $("#an-filter");
    if (!sel || !sel.selectedOptions || !sel.selectedOptions.length) return "";
    return sel.selectedOptions[0].textContent.trim();
  }

  let filterLoaded = false;
  async function loadFilterOptions() {
    if (filterLoaded) return;
    const sel = $("#an-filter");
    if (!sel) return;
    try {
      const d = await api("/api/analysis/moderators");
      let html = `<option value="">All studies</option>`;
      (d.featured || []).forEach((m) => {
        const levels = m.levels || [];
        if (!levels.length) return;
        html += `<optgroup label="${esc(m.label)}">`;
        levels.forEach((lv) => {
          html += `<option value="${esc(m.name)}|${esc(lv.value)}">` +
                  `${esc(m.label)}: ${esc(lv.label)} (${lv.n_studies})</option>`;
        });
        html += `</optgroup>`;
      });
      sel.innerHTML = html;
      filterLoaded = true;
    } catch { /* keep "All studies" */ }
  }

  // ---------------------------------------------------------------- quality
  function qualityVisible() {
    const p = $("#an-panel-tables");
    return p && !p.hidden;
  }

  function refreshQuality() {
    if (qualityVisible()) loadQuality();
  }

  async function loadQuality() {
    const host = $("#an-quality");
    if (!host) return;
    host.innerHTML = `<div class="an-empty">Loading quality checks…</div>`;
    const [rob, loo, inv, exc, aud] = await Promise.all([
      fetchTable("robustness"), fetchTable("loo_summary"), fetchTable("spec_inventory"),
      fetchTable("specs_excluded"), fetchTable("condition_audit"),
    ]);
    const blocks = [];
    const note = (t) => (t ? `<p class="an-note">${t}</p>` : "");
    blocks.push(summaryBlock(
      state.layer === "sdt"
        ? "Robustness & bias — SDT components (true score · fake score · response bias)"
        : "Robustness & bias — publication bias, exclusions, effect-size metric",
      rob.columns, rob.rows) ||
      `<div class="an-empty">No robustness rows for this selection.</div>`);
    blocks.push(summaryBlock("Leave-one-out — mean · min · max and outlier studies",
      loo.columns, loo.rows));
    blocks.push(summaryBlock("Specification inventory", inv.columns, (inv.rows || []).slice(0, 40)));
    if (exc && exc.rows && exc.rows.length) {
      blocks.push(summaryBlock("Excluded by the quality guard", exc.columns, exc.rows));
      blocks.push(note(exc.note ? "Quality-guard criterion: " + esc(exc.note) : ""));
    }
    if (aud && aud.rows && aud.rows.length) {
      blocks.push(summaryBlock("SDT condition audit — ambiguous arm roles only",
        aud.columns, aud.rows));
      blocks.push(note(aud.note ? esc(aud.note) : ""));
    }
    host.innerHTML = blocks.join("") ||
      `<div class="an-empty">No quality tables yet. Click <b>Recompute tables</b>.</div>`;
    const f = state.filterName && state.filterValue ? ` · subgroup filter: ${filterLabel()}` : "";
    $("#an-quality-note").textContent =
      `${state.layer === "sdt" ? "SDT" : "Meta"} layer · ${SPEC_SHORT[state.spec] || state.spec}` +
      ` · exclusions ${state.exclusions}${f} · the robustness components follow the selection.`;
  }

  // ---------------------------------------------------------------- figures
  function figCaption(f) {
    const parts = (f.stem || "").split("_");
    const title = prettyTitle(parts);
    const layer = parts[1], spec = parts[2] || "";
    const row = (state.overview?.specs || []).find((s) =>
      s.layer === layer && s.spec === spec && s.exclusions === state.exclusions);
    const est = row ? ` \u00b7 k=${row.k}, pooled=${fmt(row.est)}` : "";
    return title + est;
  }

  function figSpecRank(f) {
    const i = SPEC_ORDER.indexOf(f.spec || "");
    return i < 0 ? SPEC_ORDER.length : i;
  }

  function figureSort(a, b) {
    const rank = (f) => (f.layer === "meta" ? 0 : f.layer === "sdt" ? 1 : 2);
    return (rank(a) - rank(b)) || (figSpecRank(a) - figSpecRank(b))
      || String(a.stem).localeCompare(String(b.stem));
  }

  function renderFigureFilters() {
    const figs = (state.overview?.figures || []).filter((f) => f.ext === ".png");
    const kinds = [...new Set(figs.map((f) => f.kind))];
    const specs = SPEC_ORDER.filter((s) => figs.some((f) => f.spec === s));
    const host = $("#an-fig-filters");
    if (!host) return;
    const sel = (id, label, opts, val) =>
      `<label class="field-inline"><span>${label}</span><select class="select" id="${id}">` +
      opts.map(([v, t]) => `<option value="${esc(v)}"${v === val ? " selected" : ""}>${esc(t)}</option>`).join("") +
      `</select></label>`;
    host.innerHTML =
      sel("an-fig-layer", "Layer",
          [["all", "All layers"], ["meta", "Meta"], ["sdt", "SDT"], ["both", "Legacy / untagged"]],
          state.figLayer) +
      sel("an-fig-spec", "Spec",
          [["all", "All specs"]].concat(specs.map((s) => [s, SPEC_LABEL[s] || s])), state.figSpec) +
      sel("an-fig-kind", "Type",
          [["all", "All types"]].concat(
            KIND_ORDER.filter((k) => kinds.includes(k))
              .concat(kinds.filter((k) => !KIND_ORDER.includes(k)))
              .map((k) => [k, KIND_LABEL[k] || prettyTitle(String(k).split("_"))])),
          state.figKind);
    $("#an-fig-layer")?.addEventListener("change", (e) => { state.figLayer = e.target.value; renderFigureList(); });
    $("#an-fig-spec")?.addEventListener("change", (e) => { state.figSpec = e.target.value; renderFigureList(); });
    $("#an-fig-kind")?.addEventListener("change", (e) => { state.figKind = e.target.value; renderFigureList(); });
  }

  function renderFigureList() {
    renderFigureFilters();
    const all = (state.overview?.figures || []).filter((f) => f.ext === ".png");
    const stems = [];
    const seen = new Set();
    all.forEach((f) => { if (!seen.has(f.stem)) { seen.add(f.stem); stems.push(f); } });
    const figs = stems.filter((f) =>
      (state.figLayer === "all" || f.layer === state.figLayer) &&
      (state.figSpec === "all" || f.spec === state.figSpec) &&
      (state.figKind === "all" || f.kind === state.figKind));
    const host = $("#an-fig-list");
    if (!figs.length) {
      host.innerHTML = `<div class="an-empty">No figures match these filters.</div>`;
      return;
    }
    const byKind = {};
    figs.forEach((f) => { (byKind[f.kind] = byKind[f.kind] || []).push(f); });
    const kinds = KIND_ORDER.filter((k) => byKind[k])
      .concat(Object.keys(byKind).filter((k) => !KIND_ORDER.includes(k)));
    host.innerHTML = kinds.map((k) => {
      const items = byKind[k].slice().sort(figureSort);
      const groupLabel = KIND_LABEL[k] || prettyTitle(String(k).split("_"));
      return `<div class="an-figgroup">${esc(groupLabel)} <span class="an-figcount">${items.length}</span></div>` +
        items.map((f) =>
          `<button class="an-figbtn${f.stem === state.figure ? " active" : ""}" data-stem="${esc(f.stem)}" title="${esc(f.stem)}">` +
          `${esc(prettyTitle(f.stem.split("_")))}` +
          `${f.layer === "both" ? `<span class="an-legacy">legacy</span>` : ""}</button>`).join("");
    }).join("");
    $$("#an-fig-list .an-figbtn").forEach((b) =>
      b.addEventListener("click", () => selectFigure(b.dataset.stem)));
    if (!figs.some((f) => f.stem === state.figure)) state.figure = figs[0].stem;
    if (state.figure) selectFigure(state.figure, false);
  }

  function selectFigure(stem, rerender = true) {
    state.figure = stem;
    const f = (state.overview?.figures || []).find((x) => x.stem === stem && x.ext === ".png");
    if (!f) return;
    if (rerender) {
      $$("#an-fig-list .an-figbtn").forEach((b) => b.classList.toggle("active", b.dataset.stem === stem));
    }
    // Do not pull the (large) image until the Figures tab is actually open.
    if ($("#an-panel-figures")?.hidden) return;
    const img = $("#an-fig-img");
    img.src = `/api/analysis/figure/${encodeURIComponent(stem)}.png?t=${Date.now()}`;
    img.hidden = false;
    $("#an-fig-cap").innerHTML = `${esc(figCaption(f))}<br><span class="an-figlinks">` +
      `<a href="/api/analysis/figure/${encodeURIComponent(stem)}.png" target="_blank" rel="noopener">PNG</a>` +
      (f.pdf ? ` · <a href="/api/analysis/figure/${encodeURIComponent(stem)}.pdf" target="_blank" rel="noopener">PDF</a>` : "") +
      `</span>`;
  }

  // ---------------------------------------------------------------- effects figure (right)
  // Closest-available fallback order per specification base.  The app must never
  // leave the Effects panel blank just because the exact (layer, spec) forest is
  // missing; it shows the nearest available one and labels the substitution.
  const FOREST_FALLBACK = {
    fudidpost: ["didpost", "fupost", "post", "did", "persistence"],
    fupost: ["post", "persistence", "didpost", "did"],
    didpost: ["did", "post", "fupost"],
    persistence: ["post", "fupost", "did"],
    did: ["post", "didpost"],
    post: ["didpost", "fupost", "persistence", "did"],
  };

  function forestFallbackSpecs(spec) {
    const m = /^(.*)_(all|cred)$/.exec(String(spec || ""));
    const base = m ? m[1] : String(spec || "");
    const suffix = m ? "_" + m[2] : "";
    const order = (FOREST_FALLBACK[base] || []).map((b) => b + suffix);
    SPEC_ORDER.forEach((s) => order.push(s));
    return order.filter((s, i) => s !== spec && order.indexOf(s) === i);
  }

  // Returns [{fig, exact}] in preference order: exact (layer, spec) first, then
  // the closest same-layer specs, then the legacy/untagged layer, then anything.
  function forestCandidates() {
    const figs = (state.overview?.figures || []).filter((f) => f.ext === ".png" && f.kind === "forest");
    const out = [];
    const push = (f, exact) => {
      if (f && !out.some((c) => c.fig.stem === f.stem)) out.push({ fig: f, exact });
    };
    figs.filter((f) => f.layer === state.layer && f.spec === state.spec)
        .forEach((f) => push(f, true));
    forestFallbackSpecs(state.spec).forEach((s) =>
      figs.filter((f) => f.layer === state.layer && f.spec === s).forEach((f) => push(f, false)));
    figs.filter((f) => f.layer === "both" && f.spec === state.spec).forEach((f) => push(f, false));
    forestFallbackSpecs(state.spec).forEach((s) =>
      figs.filter((f) => f.layer === "both" && f.spec === s).forEach((f) => push(f, false)));
    figs.filter((f) => f.layer === state.layer).forEach((f) => push(f, false));
    figs.forEach((f) => push(f, false));
    return out;
  }

  function renderEffectsFigure() {
    renderModFigures();
    const img = $("#an-side-img"), cap = $("#an-side-cap");
    const none = $("#an-side-none"), png = $("#an-side-png"), pdf = $("#an-side-pdf");
    const fallbackNote = $("#an-side-fallback");
    if (!img) return;
    let stem = null, isMod = false, fallbackStem = null;
    if (state.sideFigure) { stem = String(state.sideFigure).replace(/\.png$/, ""); isMod = true; }
    else {
      const chosen = forestCandidates()[0];
      if (chosen) {
        stem = chosen.fig.stem;
        if (!chosen.exact) fallbackStem = chosen.fig.stem;
      }
    }
    if (!stem) {
      img.hidden = true;
      if (none) none.hidden = false;
      if (png) png.hidden = true;
      if (pdf) pdf.hidden = true;
      if (cap) cap.textContent = "";
      if (fallbackNote) { fallbackNote.hidden = true; fallbackNote.textContent = ""; }
      return;
    }
    img.hidden = false;
    img.src = `/api/analysis/figure/${encodeURIComponent(stem)}.png?t=${Date.now()}`;
    if (none) none.hidden = true;
    const url = `/api/analysis/figure/${encodeURIComponent(stem)}`;
    if (png) { png.href = url + ".png"; png.hidden = false; }
    if (pdf) { pdf.href = url + ".pdf"; pdf.hidden = isMod; }
    if (fallbackNote) {
      fallbackNote.hidden = !fallbackStem;
      fallbackNote.textContent = fallbackStem
        ? `No exact figure for this specification; showing ${fallbackStem}.`
        : "";
    }
    if (cap) {
      if (isMod) {
        const mods = (state.modResult?.moderators || []).map((n) =>
          ((state.mods || []).find((m) => m.name === n) || { label: n }).label).join(" × ");
        cap.textContent = `Moderator model · ${mods}`;
      } else {
        // The caption describes the figure actually shown (which may be the
        // nearest fallback), so the k/estimate can never be misattributed.
        const f = (state.overview?.figures || []).find((x) => x.stem === stem);
        const fl = f ? f.layer : state.layer, fs = f ? f.spec : state.spec;
        const row = (state.overview?.specs || []).find((s) => s.layer === fl
          && s.spec === fs && s.exclusions === state.exclusions);
        cap.textContent = `${LAYER_LABEL[fl] || fl} · ${SPEC_SHORT[fs] || fs}` +
          (row ? ` · k=${row.k}` : "");
        cap.title = fallbackStem ? "Showing the closest available figure, not the exact selected specification" : "";
      }
    }
  }

  // ---------------------------------------------------------------- moderator panel
  /* One figure per selected moderator, stacked in the right rail.  These are the
     per-moderator level/association plots (independent of the fitted model), so
     every selected moderator always has its own picture even before/without a
     successful meta-regression fit. */
  function renderModFigures() {
    const host = $("#an-side-figs");
    const wrap = $("#an-side-figwrap");
    if (!host) return;
    const figs = state.modFigures || [];
    if (!figs.length) { host.innerHTML = ""; if (wrap) wrap.hidden = true; return; }
    if (wrap) wrap.hidden = false;
    host.innerHTML = figs.map((f) => {
      const stem = String(f.file || "").replace(/\.png$/, "");
      if (!stem) return "";
      const url = `/api/analysis/figure/${encodeURIComponent(stem)}`;
      const label = esc(f.label || f.name);
      return `<figure class="an-sidefig"><figcaption>${label}</figcaption>` +
        `<img src="${url}.png?t=${Date.now()}" alt="${label}">` +
        `<div class="an-figlinks"><a href="${url}.png" target="_blank" rel="noopener">PNG</a>` +
        `<a href="${url}.pdf" target="_blank" rel="noopener">PDF</a></div></figure>`;
    }).join("");
  }

  const modKindLabel = (kind) => kind === "continuous" ? "continuous"
    : kind === "binary" ? "yes/no" : "categorical";

  async function loadModerators(force = false) {
    if (state.mods && !force) { renderModerators(); return; }
    const groupsHost = $("#an-mod-groups");
    if (groupsHost) groupsHost.innerHTML = `<div class="an-empty">Loading moderators…</div>`;
    try {
      const d = await api("/api/analysis/moderators");
      state.mods = d.moderators || [];
      state.featured = d.featured || [];
      state.modGroups = d.groups || [];
      state.modModuleGroups = d.module_groups || [];
    } catch (e) {
      if (groupsHost) groupsHost.innerHTML = `<div class="an-empty">Could not load moderators: ${esc(e.message)}</div>`;
      return;
    }
    renderModerators();
  }

  function modItem(m) {
    const warn = m.module
      ? `<span class="an-modwarn" title="Intervention-module field (${esc(m.module_label || m.module)}). Only studies coded for this module have a value, so selecting it restricts the model to those studies.">⚠</span>`
      : "";
    return `<label class="an-moditem"><input type="checkbox" data-mod="${esc(m.name)}"` +
      (state.selMods.has(m.name) ? " checked" : "") + `>` +
      `<span>${warn}${esc(m.label)}</span><span class="kind">${esc(modKindLabel(m.kind))}` +
      `${m.kind !== "continuous" && m.levels ? " · " + m.levels.length + " lv" : ""}</span></label>`;
  }

  function syncModChecks() {
    $$('.an-moditem input[type=checkbox]').forEach((c) => {
      c.checked = state.selMods.has(c.dataset.mod);
    });
  }

  function renderModerators() {
    const feat = $("#an-mod-featured"), groups = $("#an-mod-groups");
    const keep = (m) => !m.sdt_only || state.layer === "sdt";
    if (feat) feat.innerHTML = (state.featured || []).filter(keep).map(modItem).join("");
    let html = (state.modGroups || []).map((g) => {
      const mods = (g.moderators || []).filter(keep);
      if (!mods.length) return "";
      return `<details class="an-modcat"><summary>${esc(g.group)}` +
        `<span class="cnt">${mods.length}</span></summary>` +
        `<div class="an-modlist">${mods.map(modItem).join("")}</div></details>`;
    }).join("");
    const mg = (state.modModuleGroups || []).map((g) => ({
      ...g, moderators: (g.moderators || []).filter(keep) })).filter((g) => g.moderators.length);
    if (mg.length) {
      const n = mg.reduce((a, g) => a + g.moderators.length, 0);
      html += `<details class="an-modcat an-modcat-modules"><summary>` +
        `<span>Intervention-specific variables</span><span class="cnt">${n}</span></summary>` +
        `<div class="an-modmodules">` + mg.map((g) =>
          `<h5 class="an-modsubhead">${esc(g.label)}<span class="cnt">${g.moderators.length}</span></h5>` +
          `<div class="an-modlist">${g.moderators.map(modItem).join("")}</div>`).join("") +
        `</div></details>`;
    }
    if (groups) groups.innerHTML = html;
    $$('.an-moditem input[type=checkbox]').forEach((c) => c.addEventListener("change", () => {
      if (c.checked) state.selMods.add(c.dataset.mod); else state.selMods.delete(c.dataset.mod);
      state.interactions = state.interactions.filter(([a, b]) => state.selMods.has(a) && state.selMods.has(b));
      syncModChecks();
      renderInteractionSelects(); renderInteractions();
      scheduleModeratorFit();
    }));
    renderInteractionSelects(); renderInteractions();
  }

  function renderInteractionSelects() {
    const opts = Array.from(state.selMods).map((n) => {
      const m = (state.mods || []).find((x) => x.name === n) || { label: n };
      return `<option value="${esc(n)}">${esc(m.label)}</option>`;
    }).join("");
    const a = $("#an-int-a"), b = $("#an-int-b");
    if (a) a.innerHTML = opts || `<option value="">—</option>`;
    if (b) b.innerHTML = opts || `<option value="">—</option>`;
  }

  function renderInteractions() {
    const host = $("#an-int-list");
    if (!host) return;
    host.innerHTML = state.interactions.map(([a, b], i) => {
      const ma = (state.mods || []).find((x) => x.name === a) || { label: a };
      const mb = (state.mods || []).find((x) => x.name === b) || { label: b };
      return `<span class="an-intchip">${esc(ma.label)} × ${esc(mb.label)}` +
        `<button data-i="${i}" title="Remove">✕</button></span>`;
    }).join("");
    $$("#an-int-list button").forEach((btn) => btn.addEventListener("click", () => {
      state.interactions.splice(Number(btn.dataset.i), 1);
      renderInteractions();
      scheduleModeratorFit();
    }));
  }

  function fmtP(p) {
    const n = Number(p);
    if (p === null || p === undefined || Number.isNaN(n)) return "–";
    return n.toFixed(3).replace(/^0/, "");
  }

  function humanTerm(term) {
    // Humanise the moderator variable names in a term label (backend keeps the
    // raw names; only the display is humanised).
    let out = String(term == null ? "" : term);
    const labels = {};
    (state.mods || []).forEach((m) => { if (m && m.name) labels[m.name] = m.label || m.name; });
    Object.keys(labels).sort((a, b) => b.length - a.length).forEach((name) => {
      out = out.split(name).join(labels[name]);
    });
    return out;
  }

  function modHeading(bayes) {
    return bayes
      ? `<tr><th>Term</th><th>k</th><th>Post. mean</th><th>Post. SD</th><th>95% CrI</th><th>BF₁₀</th><th>P(&gt;0)</th></tr>`
      : `<tr><th>Term</th><th>k</th><th>Estimate</th><th>SE</th><th>95% CI</th><th>z</th><th>p</th></tr>`;
  }

  function modRow(t, bayes) {
    const est = esc(fmt(t.estimate, 2));
    const se = esc(fmt(t.se, 2));
    const kk = (t.k === null || t.k === undefined || t.k === "") ? "–" : esc(t.k);
    const ci = `${esc(fmt(t.lo, 2))}, ${esc(fmt(t.hi, 2))}`;
    if (bayes) {
      const bf = num(t.bf10);
      const sig = (bf !== null && (bf > 3 || bf < 1 / 3)) ? ' class="num p-sig"' : ' class="num"';
      return `<tr><td>${esc(humanTerm(t.term))}</td><td class="num">${kk}</td>` +
        `<td class="num">${est}</td><td class="num">${se}</td>` +
        `<td class="num">${ci}</td><td${sig}>${esc(bfFmt(t.bf10))}</td>` +
        `<td class="num">${esc(fmt(t.prob_gt0, 3))}</td></tr>`;
    }
    const sig = Number(t.p) < 0.05 ? ' class="num p-sig"' : ' class="num"';
    return `<tr><td>${esc(humanTerm(t.term))}</td><td class="num">${kk}</td>` +
      `<td class="num">${est}</td><td class="num">${se}</td>` +
      `<td class="num">${ci}</td><td class="num">${esc(Number(t.z).toFixed(2))}</td>` +
      `<td${sig}>${esc(fmtP(t.p))}</td></tr>`;
  }

  const SDT_COMPONENTS = [
    ["true_score", "True score"],
    ["fake_score", "Fake score"],
    ["criterion", "Response bias (criterion c)"],
  ];

  function renderModComponents() {
    if (state.layer !== "sdt" || !state.modComponents) return "";
    const bayes = state.model === "bayes";
    const blocks = SDT_COMPONENTS.map(([key, label]) => {
      const cr = state.modComponents[key];
      const head = `<h4 class="an-subhead">${esc(label)} — moderation</h4>`;
      if (!cr) return head + `<div class="an-empty">not estimated</div>`;
      if (!cr.ok) return head + `<div class="an-empty">${esc(cr.error || "not estimated")}</div>`;
      const body = (cr.terms || []).map((t) => modRow(t, cr.model === "bayes")).join("");
      return head + `<div class="an-tablewrap"><table class="an-table">` +
        `<thead>${modHeading(cr.model === "bayes")}</thead><tbody>${body}</tbody></table></div>` +
        `<p class="an-note">k = ${esc(cr.k)} · ${esc(cr.n_studies)} studies · ` +
        `τ between = ${cr.tau_between !== undefined && cr.tau_between !== null ? Number(cr.tau_between).toFixed(3) : "–"}</p>`;
    }).join("");
    return `<h3 class="an-subhead" style="font-size:12.5px">SDT decomposition — moderation of true score, fake score and response bias</h3>` + blocks;
  }

  function renderModResults() {
    const host = $("#an-mod-results");
    const kpis = $("#an-mod-kpis");
    if (!host) return;
    const r = state.modResult;
    if (kpis) kpis.innerHTML = "";
    if (!r) { host.innerHTML = ""; return; }
    if (!r.ok) { host.innerHTML = `<div class="an-empty">${esc(r.error || "Model failed")}</div>`; return; }
    const bayes = r.model === "bayes";
    const tauB = r.tau_between !== undefined ? r.tau_between : r.tau;
    const tauW = r.tau_within;
    const i2B = r.i2_between, i2W = r.i2_within;
    const r2tau = (r.r2_tau !== null && r.r2_tau !== undefined && isFinite(r.r2_tau))
      ? Number(r.r2_tau).toFixed(2) : null;
    if (kpis) {
      kpis.innerHTML = [
        kpi("Moderator k", esc(r.k), `${esc(r.n_studies)} studies`),
        kpi("R² explained",
          (r.r2 !== null && r.r2 !== undefined && isFinite(r.r2)) ? Number(r.r2).toFixed(2) : "–",
          bayes ? "posterior fit" : ""),
        kpi("R² among τ", r2tau === null ? "–" : r2tau, "between-study variance"),
        kpi("τ between", (tauB !== null && tauB !== undefined) ? Number(tauB).toFixed(3) : "–",
          (i2B !== null && i2B !== undefined) ? `I² ${fmt(i2B, 0)}%` : ""),
        kpi("τ within", (tauW !== null && tauW !== undefined) ? Number(tauW).toFixed(3) : "–",
          (i2W !== null && i2W !== undefined) ? `I² ${fmt(i2W, 0)}%` : ""),
        bayes
          ? kpi("Model", "Bayes · two-level", "posterior mean / 95% CrI")
          : kpi("Q_M", (r.qm === null || r.qm === undefined) ? "–" : Number(r.qm).toFixed(2),
                (r.qm_p !== null && r.qm_p !== undefined) ? `p = ${fmtP(r.qm_p)}` : ""),
      ].join("");
    }
    const rows = (r.terms || []).map((t) => modRow(t, bayes)).join("");
    const heading = modHeading(bayes);
    const note = (bayes
      ? `<p class="an-note">Bayesian two-level random-effects meta-regression: effects are nested in studies (weak Normal prior on the coefficients, half-normal priors on both τ levels). BF₁₀ is the Savage–Dickey Bayes factor for the coefficient.</p>`
      : `<p class="an-note">Two-level random-effects meta-regression: stage 1 combines the effects within each study, stage 2 regresses the study estimates on the moderators (between-study τ in the weights, CR1 SEs); within-study τ is reported in the indicator strip above.</p>`) +
      (r2tau !== null
        ? `<p class="an-note">R² among τ = ${esc(r2tau)} — the proportional reduction in between-study variance relative to an intercept-only model.</p>`
        : "");
    const lv = r.levels || {};
    const levelBlocks = Object.keys(lv).map((name) => {
      const entries = lv[name] || [];
      if (!entries.length) return "";
      const m = (state.mods || []).find((x) => x.name === name) || { label: name };
      const body = entries.map((l) => {
        const tau = (l.tau === null || l.tau === undefined) ? "–" : fmt(l.tau, 2);
        const i2 = (l.i2 === null || l.i2 === undefined) ? "–" : `${fmt(l.i2, 0)}%`;
        return `<tr><td>${esc(l.label)}</td><td class="num">${esc(l.k)}</td>` +
          `<td class="num">${esc(l.n_studies)}</td><td class="num">${esc(fmt(l.estimate, 2))}</td>` +
          `<td class="num">${esc(fmt(l.lo, 2))}, ${esc(fmt(l.hi, 2))}</td>` +
          `<td class="num">${esc(tau)}</td><td class="num">${esc(i2)}</td></tr>`;
      }).join("");
      return `<h4 class="an-subhead">${esc(m.label)} — all levels</h4>` +
        `<div class="an-tablewrap"><table class="an-table"><thead><tr><th>Level</th><th>k</th>` +
        `<th>Studies</th><th>${bayes ? "Post. mean" : "Estimate"}</th>` +
        `<th>95% ${bayes ? "CrI" : "CI"}</th><th>τ</th><th>I²</th></tr></thead>` +
        `<tbody>${body}</tbody></table></div>`;
    }).join("");
    const modWarn = (r.moderators || [])
      .map((n) => (state.mods || []).find((x) => x.name === n))
      .filter((m) => m && m.module)
      .map((m) => `${m.label} (${m.module_label || m.module})`);
    const warnNote = modWarn.length
      ? `<p class="an-warn">⚠ Module-specific moderator${modWarn.length > 1 ? "s" : ""}: ` +
        `${esc(modWarn.join(", "))}. Only studies coded for ` +
        `${modWarn.length > 1 ? "those intervention modules" : "that intervention module"} enter this model` +
        `${r.dropped ? ` (${esc(r.dropped)} contrast(s) dropped)` : ""}.</p>`
      : "";
    const subNote = (r.substitutions || []).length
      ? `<p class="an-note">Multi-valued moderator substitution: ` +
        (r.substitutions || []).map((s) => {
          const m = (state.mods || []).find((x) => x.name === s.moderator);
          const lab = m ? m.label : s.moderator;
          return `${esc(lab)} (${esc(s.n_studies)} stud${Number(s.n_studies) === 1 ? "y" : "ies"})`;
        }).join("; ") +
        " — multi-type studies assigned their first intervention label " +
        "(primary/first value) instead of being dropped.</p>"
      : "";
    host.innerHTML = note + warnNote + subNote +
      (r.dropped_terms && r.dropped_terms.length
        ? `<p class="an-note">Dropped collinear term(s): ${esc(r.dropped_terms.map(humanTerm).join("; "))}</p>` : "") +
      `<div class="an-tablewrap"><table class="an-table"><thead>${heading}</thead>` +
      `<tbody>${rows}</tbody></table></div>` +
      renderModComponents() +
      (levelBlocks
        ? `<p class="an-note">All moderator levels: an intercept-only random-effects model is fitted to each ` +
          `level (the easy equivalent of Patzl et al.'s separate per-level models), so every level is disclosed — ` +
          `not just the non-reference dummy coefficients above.</p>${levelBlocks}`
        : "");
  }

  let modTimer = null;
  let modToken = 0;          // identifies the newest moderator fit request
  let modPending = false;    // a selection changed while a fit was in flight

  function clearModeratorModel() {
    state.modResult = null;
    state.modComponents = null;
    state.sideFigure = null;
    state.modFigures = [];
    renderModResults();
    renderEffectsFigure();
  }

  function scheduleModeratorFit() {
    // Moderator models are refit automatically whenever the selected moderators,
    // interactions, model, priors or subgroup filter change — no run button.
    // A change made while a fit is already in flight must not be dropped: mark it
    // pending and refit as soon as the in-flight request settles.
    clearTimeout(modTimer);
    if (state.modFitting) { modPending = true; return; }
    modTimer = setTimeout(() => {
      const panel = $("#an-panel-moderator");
      if (!panel || panel.hidden) return;
      if (!state.selMods.size) { clearModeratorModel(); return; }
      runModerator();
    }, 350);
  }

  let compToken = 0;

  async function fetchModeratorComponents() {
    const token = ++compToken;
    const base = { layer: state.layer, spec: state.spec, exclusions: state.exclusions,
                   scope: (state.cred ? "restricted" : "unrestricted"),
                   moderators: Array.from(state.selMods), interactions: state.interactions,
                   model: state.model, mu_sd: state.muSd, tau_sd: state.tauSd,
                   tau_prior: state.tauPrior,
                   filter_name: state.filterName, filter_value: state.filterValue,
                   fully_within: state.fullyWithin };
    const out = {};
    for (const oc of ["true_score", "fake_score", "criterion"]) {
      try {
        out[oc] = await api("/api/analysis/moderate", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(Object.assign({}, base, { outcome: oc })),
        });
      } catch (e) {
        out[oc] = { ok: false, error: String(e.message || e) };
      }
    }
    if (token !== compToken) return;
    state.modComponents = out;
    renderModResults();
  }

  async function runModerator() {
    if (!state.selMods.size) { clearModeratorModel(); return; }
    const token = ++modToken;
    state.modComponents = null;
    state.modFitting = true;
    syncModWait();
    try {
      const r = await api("/api/analysis/moderate", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ layer: state.layer, spec: state.spec, exclusions: state.exclusions,
                               scope: (state.cred ? "restricted" : "unrestricted"),
                               moderators: Array.from(state.selMods),
                               interactions: state.interactions,
                               model: state.model,
                               mu_sd: state.muSd, tau_sd: state.tauSd, tau_prior: state.tauPrior,
                               filter_name: state.filterName, filter_value: state.filterValue,
                               fully_within: state.fullyWithin }),
      });
      if (token !== modToken) return;   // superseded by a newer selection
      state.modResult = r;
      renderModResults();
      state.modFigures = r.figures || [];
      // Per-moderator figures only ("one with each level"): the fitted-model
      // coefficient forest ("the differences") is no longer produced, so the
      // Effects rail keeps the specification's forest plot.
      state.sideFigure = null;
      renderEffectsFigure();
      if (r.ok && state.layer === "sdt") fetchModeratorComponents();
    } catch (e) {
      if (token !== modToken) return;
      state.modResult = { ok: false, error: String(e.message || e) };
      state.modFigures = [];
      renderModResults();
      renderEffectsFigure();
    } finally {
      if (token === modToken) {
        state.modFitting = false;
        syncModWait();
      }
      if (modPending) { modPending = false; scheduleModeratorFit(); }
    }
  }

  // ---------------------------------------------------------------- methods
  async function loadMethods() {
    const host = $("#an-methods");
    try {
      const n = await api("/api/analysis/note");
      if (!n.available) {
        host.innerHTML = `<div class="an-empty">No methods note found (<code>Scripts/ANALYSIS_SUITE_NOTE.md</code>).</div>`;
        return;
      }
      const secs = n.sections || [];
      const stale = n.stale_sections || 0;
      const banner = `<div class="an-method-banner">`
        + `Methods note · ${secs.length} section${secs.length === 1 ? "" : "s"}`
        + (n.tables_at ? ` · tables updated ${esc(n.tables_at)}` : "")
        + (stale
            ? ` · <b class="an-stale">${stale} stale</b>`
            : ` · <b class="an-fresh">up to date</b>`)
        + `</div>`;
      const staleIdx = secs.map((s, i) => (s.stale ? i : -1)).filter((i) => i >= 0);
      const bar = secs.length > 1
        ? `<div class="an-methodbar">`
          + `<label class="an-jump"><span>Jump to section</span>`
          + `<select id="an-method-jump"><option value="">Choose…</option>`
          + secs.map((s, i) => `<option value="${i}">${esc(s.title)}</option>`).join("")
          + `</select></label>`
          + (staleIdx.length
              ? `<span class="an-hint warn">${staleIdx.length} stale</span>` : "")
          + `<span class="grow"></span>`
          + `<button class="btn ghost" id="an-method-expand">Expand all</button>`
          + `<button class="btn ghost" id="an-method-collapse">Collapse all</button>`
          + `</div>`
        : "";
      const blocks = secs.map((s, i) => {
        const badge = s.generated
          ? `<span class="an-badge ${s.stale ? "warn" : "ok"}">generated ${esc(s.generated)}`
            + `${s.writer ? " · " + esc(s.writer) : ""}</span>`
          : `<span class="an-badge">hand-written</span>`;
        return `<details class="section an-methodsec" data-mi="${i}" ${i === 0 ? "open" : ""}>`
          + `<summary><span class="an-methodtitle">${esc(s.title)}</span>${badge}</summary>`
          + `<div class="group-body md">${renderMarkdown(s.markdown)}</div></details>`;
      }).join("");
      host.innerHTML = banner + bar + blocks;
      const secEls = $$("#an-methods .an-methodsec");
      const jump = $("#an-method-jump");
      if (jump) jump.addEventListener("change", () => {
        const t = secEls[Number(jump.value)];
        if (t) { t.open = true; t.scrollIntoView({ block: "start", behavior: "smooth" }); }
        jump.value = "";
      });
      const exp = $("#an-method-expand");
      if (exp) exp.addEventListener("click", () => secEls.forEach((d) => { d.open = true; }));
      const col = $("#an-method-collapse");
      if (col) col.addEventListener("click", () => secEls.forEach((d) => { d.open = false; }));
    } catch {
      host.innerHTML = `<div class="an-empty">Methods note unavailable.</div>`;
    }
  }

  // ---------------------------------------------------------------- run
  // A single place decides whether the "Fitting moderator model…" indicator is
  // active: only while a backend run is in progress, a moderator fit is being
  // requested, or a fit has been queued behind the run.  Idle otherwise.
  function syncModWait() {
    const wait = $("#an-mod-wait");
    if (wait) wait.hidden = !(state.runActive || state.modFitting);
  }

  function setRunning(on, kind) {
    state.runActive = !!on;
    const b = $("#an-full-top"); if (b) b.disabled = on;
    if (on) {
      $("#an-runlog").hidden = false;
      $("#an-runlog-title").textContent = "Recomputing tables and figures…";
    }
    syncModWait();
  }

  // The top-bar hint doubles as the startup progress indicator: while the
  // prerequisite tables are being recomputed it shows a progress bar and a
  // status line instead of the plain "N tables · M figures" summary.
  function setProgress(text, busy = false) {
    const host = $("#an-hint"), txt = $("#an-hint-text"), bar = $("#an-hint-bar");
    if (txt) txt.textContent = text || "";
    if (host) host.classList.toggle("warn", false);
    if (bar) bar.hidden = !busy;
  }

  function clearProgress() {
    const bar = $("#an-hint-bar");
    if (bar) bar.hidden = true;
  }

  async function pollRun() {
    try {
      const s = await api("/api/analysis/run/status");
      $("#an-runlog-body").textContent = s.log || "(starting…)";
      $("#an-runlog-body").scrollTop = $("#an-runlog-body").scrollHeight;
      setProgress("Recomputing prerequisite tables and figures…", true);
      if (s.finished || !s.running) {
        clearInterval(state.polling); state.polling = null;
        setRunning(false);
        clearProgress();
        $("#an-runlog-title").textContent = s.ok === false ? "Run failed" : "Run complete";
        await refresh();
        return;
      }
    } catch { /* keep polling */ }
  }

  async function run(kind) {
    try {
      const res = await api("/api/analysis/run", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind, fully_within: state.fullyWithin }),
      });
      if (res && res.cached) {
        await refresh();
        return;
      }
      setRunning(true, kind);
      setProgress("Recomputing prerequisite tables and figures…", true);
      $("#an-runlog-body").textContent = "starting…";
      state.polling = setInterval(pollRun, 1500);
    } catch (e) {
      state.runActive = false;
      syncModWait();
      toast("Could not start the run", "err", String(e.message || e));
    }
  }

  // Analyze/Full regenerate the pipeline tables and figures.  Moderator models
  // are refit automatically and are not part of this run.
  function analyze(full = false) {
    run(full ? "full" : "essential");
  }

  // ---------------------------------------------------------------- refresh
  async function refresh() {
    try {
      state.overview = await api(`/api/analysis/overview?fully_within=${state.fullyWithin ? "1" : "0"}`);
    } catch (e) {
      $("#an-summary").innerHTML = `<div class="an-empty">Could not load the analysis overview: ${esc(e.message)}</div>`;
      return;
    }
    // Reconcile the client flags with the backend _analysis_run state so the
    // indicator reflects a run started elsewhere (or before reload): on while
    // it is running, off once it has stopped.
    const backendRunning = !!(state.overview.run && state.overview.run.running);
    if (backendRunning) {
      state.runActive = true;
      setProgress("Recomputing prerequisite tables and figures…", true);
      if (!state.polling) state.polling = setInterval(pollRun, 1500);
    } else if (!state.polling) {
      state.runActive = false;
    }
    syncModWait();
    const first = !state._inited;
    if (first) {
      // Preset: Pretest = all, Follow-up = all (spec = fupost_all).  Keep the
      // preset when that specification exists; otherwise fall back to the first
      // available one.  Follow-up = all takes precedence, as documented.
      const desired = specFromControls();
      const have = (state.overview.specs || []).some((s) =>
        s.layer === state.layer && s.spec === desired && s.exclusions === state.exclusions);
      if (!have) {
        const firstSpec = (state.overview.specs || []).find((s) => s.exclusions === "on")
          || (state.overview.specs || [])[0];
        if (firstSpec) {
          state.layer = firstSpec.layer;
          controlsFromSpec(firstSpec.spec);
          state.exclusions = firstSpec.exclusions;
        }
      }
      state._inited = true;
    }
    applyOverviewFilters();
    // Auto-recompute stale tables/effects once per input fingerprint and cache
    // the result for next time (the backend skips scripts already computed).
    if (state.overview.stale && state.overview.scripts_available && !state.runActive
        && state.overview.stale_token
        && state._autoRanToken !== state.overview.stale_token) {
      state._autoRanToken = state.overview.stale_token;
      setProgress("Recomputing prerequisite tables and figures…", true);
      run("essential");
    } else if (!state.runActive) {
      clearProgress();
    }
    loadFilterOptions();
    renderFigureList();
    renderEffectsFigure();
    await loadSummary();
    if (!$("#an-panel-tables").hidden) await loadQuality();
    const s = state.overview;
    if (!s.available) {
      $("#an-summary").innerHTML = `<div class="an-empty">No analysis tables yet.` +
        (s.scripts_available ? " Click <b>Analyze</b> to compute them." :
          " Run <code>python Scripts/publication_bias.py</code> from the project.") + `</div>`;
    }
    updateHint();
  }

  function updateHint() {
    const s = state.overview || {};
    const busy = !!s.specs_stale || state.runActive;
    if (!s.available) {
      setProgress(busy ? "Recomputing prerequisite tables and figures…" : "", busy);
      return;
    }
    let hint = `${(s.tables || []).length} tables · ${s.n_figures} figures` +
      (state.model === "bayes"
        ? ` · Bayes (μ N(0,${state.muSd}²), τ ${state.tauPrior}(${state.tauSd}))`
        : "");
    if (state.filterName && state.filterValue) hint += ` · ${filterLabel()}`;
    if (state.specFallback) {
      hint = `${SPEC_SHORT[state.wantedSpec] || state.wantedSpec} not generated — ` +
        `showing ${SPEC_SHORT[state.spec] || state.spec}. Run Analyze.`;
    }
    if (busy) hint = "Recomputing prerequisite tables and figures…";
    setProgress(hint, busy);
  }

  // ---------------------------------------------------------------- wiring
  function tabPanel(name) {
    ["summary", "tables", "moderator", "figures", "methods", "apa"].forEach((t) => {
      $(`#an-panel-${t}`).hidden = t !== name;
    });
    $$("#an-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    if (name === "tables") loadQuality();
    if (name === "moderator") loadModerators();
    if (name === "figures") renderFigureList();
    if (name === "methods") loadMethods();
    if (name === "apa") { renderApaControls(); updateApaModNote(); }
  }

  // ---------------------------------------------------------------- APA7 report
  const APA_DESC = [["sample", "Sample sizes"], ["age", "Age group"], ["setting", "Setting"],
                    ["design", "Design"], ["control", "Control type"], ["intervention", "Intervention type"],
                    ["domains", "Outcome domains"], ["year", "Publication year"],
                    ["wereid", "Western sample"]];
  const APA_AN = [["pooled_freq", "Frequentist pooled (DL + CR1 + two-level)"],
                  ["heterogeneity", "Heterogeneity (τ², I²)"],
                  ["pooled_bayes", "Bayesian pooled (+ BF)"],
                  ["domain", "By outcome domain"],
                  ["intervention", "By intervention type"], ["data_available", "By data availability"],
                  ["metric", "By metric family"], ["r_sensitivity", "r sensitivity"],
                  ["exclusions", "Exclusions sensitivity"],
                  ["leave_one_out", "Leave-one-out influence"],
                  ["bias", "Publication bias"],
                  ["reliability", "Inter-rater reliability"],
                  ["figures", "Embed figures (forest / moderator)"]];
  const APA_AN_DEFAULT = APA_AN.map((x) => x[0]);
  let apaInited = false;

  function apaChecks(host, items, checked) {
    const el = $(host);
    if (!el) return;
    el.innerHTML = items.map(([v, label]) =>
      `<label><input type="checkbox" value="${esc(v)}"${checked.includes(v) ? " checked" : ""}> ` +
      `${esc(label)}</label>`).join("");
  }

  function apaChecked(host) {
    return $$(`${host} input:checked`).map((c) => c.value);
  }

  function renderApaControls() {
    apaChecks("#apa-desc", APA_DESC,
              apaChecked("#apa-desc").length ? apaChecked("#apa-desc") : APA_DESC.map((x) => x[0]));
    apaChecks("#apa-an", APA_AN,
              apaChecked("#apa-an").length ? apaChecked("#apa-an") : APA_AN_DEFAULT);
    renderApaEcho();
    updateApaModNote();
    updateApaStale();
  }

  function renderApaEcho() {
    const host = $("#apa-echo");
    if (!host) return;
    const nd = apaChecked("#apa-desc").length, na = apaChecked("#apa-an").length;
    const scope = state.cred ? "restricted (susceptibility)" : "unrestricted";
    const filt = (state.filterName && state.filterValue) ? ` · filter: ${esc(filterLabel())}` : "";
    host.innerHTML = `Layer <b>${esc(LAYER_LABEL[state.layer] || state.layer)}</b> · ` +
      `spec <b>${esc(SPEC_LABEL[state.spec] || state.spec)}</b> · exclusions <b>${esc(state.exclusions)}</b> · ` +
      `scope <b>${esc(scope)}</b> · model <b>${esc(state.model)}</b>${filt} · ` +
      `<b>${na}</b> analys${na === 1 ? "is" : "es"} + <b>${nd}</b> descriptives selected.`;
  }

  function updateApaStale() {
    const el = $("#apa-stale");
    if (!el) return;
    const s = state.overview || {};
    if (s.specs_stale || s.stale) {
      el.hidden = false;
      el.textContent = "⚠ The prerequisite tables are stale — run Analyze before exporting so the " +
        "report embeds current numbers.";
    } else {
      el.hidden = true;
    }
  }

  function setApaAll(on) {
    $$("#apa-desc input, #apa-an input").forEach((c) => { c.checked = on; });
    renderApaEcho();
  }

  function updateApaModNote() {
    const note = $("#apa-mod-note");
    if (!note) return;
    const mods = (state.mods || []).filter((m) => state.selMods.has(m.name))
      .map((m) => m.label);
    const inter = state.interactions.map(([a, b]) => {
      const la = ((state.mods || []).find((m) => m.name === a) || { label: a }).label;
      const lb = ((state.mods || []).find((m) => m.name === b) || { label: b }).label;
      return `${la} × ${lb}`;
    });
    note.innerHTML = mods.length
      ? `Taken from the <b>Moderator</b> tab: ${mods.map(esc).join(", ")} — each fitted separately, ` +
        `plus one combined model for all${inter.length ? ` with interactions ${inter.map(esc).join(", ")}` : ""}.`
      : "No moderators selected in the <b>Moderator</b> tab; only the pooled model will be reported.";
  }

  function previewPrisma() {
    const img = $("#apa-prisma-img");
    if (!img) return;
    img.hidden = false;
    img.src = `/api/analysis/prisma.png?t=${Date.now()}`;
  }

  async function downloadApa() {
    const fmt = $("#apa-fmt")?.value || "docx";
    const body = {
      layer: state.layer, spec: state.spec, exclusions: state.exclusions,
      scope: (state.cred ? "restricted" : "unrestricted"),
      model: state.model,
      priors: { mu_sd: state.muSd, tau_sd: state.tauSd, tau_prior: state.tauPrior },
      descriptives: apaChecked("#apa-desc"),
      analyses: apaChecked("#apa-an"),
      moderators: Array.from(state.selMods),
      interactions: state.interactions,
      filter_name: state.filterName || "", filter_value: state.filterValue || "",
      fully_within: state.fullyWithin,
      format: fmt,
    };
    if (!body.analyses.length) { alert("Select at least one analysis."); return; }
    toast("Building the report…", "ok", "This can take a few seconds.");
    try {
      const r = await fetch("/api/analysis/apa", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        let msg = `${r.status} ${r.statusText}`;
        try { const j = await r.json(); if (j && j.error) msg = j.error; } catch { /* non-JSON */ }
        throw new Error(msg);
      }
      const blob = await r.blob();
      const cd = r.headers.get("Content-Disposition") || "";
      const m = cd.match(/filename\*?=(?:UTF-8''|")?([^";]+)/i);
      const name = m ? decodeURIComponent(m[1].replace(/"/g, "")) : `analysis_report.${fmt}`;
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(a.href);
      toast("Report downloaded", "ok", name);
    } catch (e) {
      toast("Report failed", "err", String(e.message || e));
    }
  }

  // ---------------------------------------------------------------- export
  function exportResults() {
    const fmt = $("#an-export-fmt")?.value || "xlsx";
    const p = new URLSearchParams({ fmt, layer: state.layer, spec: state.spec,
                                    exclusions: state.exclusions,
                                    scope: (state.cred ? "restricted" : "unrestricted") });
    p.set("fully_within", state.fullyWithin ? "1" : "0");
    const url = `/api/analysis/export?${p}`;
    const a = document.createElement("a");
    a.href = url;
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function init() {
    $$(".viewtab").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
    $$("#an-layer-switch button").forEach((b) => b.addEventListener("click", async () => {
      state.layer = b.dataset.layer; state.sideFigure = null;
      // participant-level moderators are SDT-only: drop them when leaving SDT
      if (state.layer !== "sdt") {
        Array.from(state.selMods).forEach((n) => {
          const m = (state.mods || []).find((x) => x.name === n);
          if (m && m.sdt_only) state.selMods.delete(n);
        });
      }
      renderModerators();
      applyOverviewFilters(); await loadSummary(); refreshQuality();
      renderFigureList(); renderEffectsFigure();
      scheduleModeratorFit();
      updateApaStale(); renderApaEcho();
    }));
    $$("#an-model-switch button").forEach((b) => b.addEventListener("click", async () => {
      state.model = b.dataset.model;
      state.sideFigure = null;
      syncControls();
      await loadSummary();
      if (!$("#an-panel-tables").hidden) loadQuality();
      if (!$("#an-panel-moderator").hidden && state.selMods.size) await runModerator();
      updateHint();
      updateApaStale(); renderApaEcho();
    }));
    $("#an-mu-prior")?.addEventListener("change", async (e) => {
      state.muSd = Number(e.target.value) || 10;
      state.sideFigure = null;
      await loadSummary();
      refreshQuality();
      if (!$("#an-panel-moderator").hidden && state.selMods.size) await runModerator();
      updateHint();
    });
    $("#an-tau-prior")?.addEventListener("change", async (e) => {
      const [kind, sd] = String(e.target.value).split("|");
      state.tauPrior = kind || "half-normal";
      state.tauSd = Number(sd) || 0.5;
      state.sideFigure = null;
      await loadSummary();
      refreshQuality();
      if (!$("#an-panel-moderator").hidden && state.selMods.size) await runModerator();
      updateHint();
    });
    $("#an-sens")?.addEventListener("click", showSensitivity);
    $("#an-pre")?.addEventListener("change", async (e) => {
      state.pre = e.target.value; state.sideFigure = null;
      applyOverviewFilters(); await loadSummary(); refreshQuality();
      renderFigureList(); renderEffectsFigure();
      scheduleModeratorFit();
    });
    $("#an-fu")?.addEventListener("change", async (e) => {
      state.fu = e.target.value; state.sideFigure = null;
      applyOverviewFilters(); await loadSummary(); refreshQuality();
      renderFigureList(); renderEffectsFigure();
      scheduleModeratorFit();
    });
    $("#an-cred")?.addEventListener("change", async (e) => {
      state.cred = e.target.checked; state.sideFigure = null;
      applyOverviewFilters(); await loadSummary(); refreshQuality();
      renderFigureList(); renderEffectsFigure();
      scheduleModeratorFit();
    });
    $("#an-excl-on")?.addEventListener("change", async (e) => {
      state.exclusions = e.target.checked ? "on" : "off"; state.sideFigure = null;
      applyOverviewFilters(); await loadSummary(); refreshQuality();
      renderFigureList(); renderEffectsFigure();
      scheduleModeratorFit();
    });
    $("#an-fully-within")?.addEventListener("change", async (e) => {
      // Toggling changes which contrasts the specs contain, so the tables must be
      // rebuilt under the new switch (the in-process views also filter on it).
      state.fullyWithin = e.target.checked; state.sideFigure = null;
      syncControls();
      run("essential");
    });
    $("#an-filter")?.addEventListener("change", async (e) => {
      const v = e.target.value;
      if (v) {
        const parts = v.split("|");
        state.filterName = parts[0];
        state.filterValue = parts.slice(1).join("|");
      } else {
        state.filterName = "";
        state.filterValue = "";
      }
      state.sideFigure = null;
      await loadSummary();
      refreshQuality();
      renderFigureList();
      renderEffectsFigure();
      scheduleModeratorFit();
      updateHint();
    });
    $("#an-full-top")?.addEventListener("click", () => analyze(true));
    $("#an-export").addEventListener("click", exportResults);
    $("#apa-preview")?.addEventListener("click", previewPrisma);
    $("#apa-download")?.addEventListener("click", downloadApa);
    $("#apa-all")?.addEventListener("click", () => setApaAll(true));
    $("#apa-none")?.addEventListener("click", () => setApaAll(false));
    $("#apa-fmt")?.addEventListener("change", renderApaEcho);
    $("#apa-an")?.addEventListener("change", renderApaEcho);
    $("#apa-desc")?.addEventListener("change", renderApaEcho);
    $("#an-runlog-close").addEventListener("click", () => { $("#an-runlog").hidden = true; });
    $$("#an-tabs button").forEach((b) => b.addEventListener("click", () => tabPanel(b.dataset.tab)));
    $("#an-int-add")?.addEventListener("click", () => {
      const a = $("#an-int-a")?.value, b = $("#an-int-b")?.value;
      if (!a || !b || a === b) return;
      if (state.interactions.some(([x, y]) => (x === a && y === b) || (x === b && y === a))) return;
      state.interactions.push([a, b]);
      renderInteractions();
      scheduleModeratorFit();
    });
    let saved = "coding";
    try { saved = localStorage.getItem("cs.view") || "coding"; } catch { /* ignore */ }
    if (saved === "harmonizer") saved = "coding";   // retired tab -> fall back
    showView(saved);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
