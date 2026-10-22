/* ============================================================
   Coding Studio — client
   Loads the coding scheme, renders an editable form for every
   schema field, and saves schema-compatible JSON per coder.
   ============================================================ */
(() => {
  "use strict";

  const { $, $$, esc, api, optionText } = window.CS;
  const icon = window.CS.svgEl;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  const isNum = (v) => v !== null && v !== undefined && v !== "" && !isNaN(Number(v));

  /* ---------------------------------------------------------- state */
  const state = {
    schema: null,
    papers: [],
    byKey: {},
    key: null,
    coder: "",
    coderLoaded: "",   // coder whose working copy is currently loaded
    blind: false,
    source: "original",
    compareCoder: "",   // coder (or "OG") shown in the compare gutter; "" = none
    compare: null,      // that coder's records for the current paper (read-only)
    records: [],
    current: 0,
    selIntervention: -1,
    showAllDvs: false,   // DV list scoped to the selected intervention unless toggled
    dirty: false,
    filter: "all",
    showExcluded: false,
    search: "",
    shuffleOn: false,
    shuffleRecent: [],   // keys picked by recent shuffles (avoid immediate repeats)
    fieldSearch: "",
    openDv: new Set(),
    openSections: new Set(),
    ftOpen: false,
    statChoice: {},
    missingsOnly: false,
    view: "pdf",
    supp: "",
    page: 1,
    fit: true,
    zoom: 1,             // relative zoom for the image PDF viewer (Ctrl+wheel)
    pageSpans: {},       // page -> text spans for the selectable text layer
    pages: null,
    pageSize: null,
    pdfFailed: false,
    textCache: {},
    notesCache: {},
    facets: { module: new Set(), type: new Set(), age: new Set(), setting: new Set(), design: new Set(), availability: new Set() },
    busy: false,
    coderMode: false,
    coderField: null,
    coderEl: null,
    coderDone: new Set(),
    coderDv: null,        // DV index whose source span is being marked / located
    coderTarget: null,    // {kind:"dv",index} | {kind:"field",name} for source marking
    markWarnTimer: null,
    fulltext: { criteria: [], mine: null, agreed: null, agreeMin: 2 },
  };

  const FACET_DIMS = ["module", "type", "age", "setting", "design", "availability"];
  const FACET_LABELS = {
    module: "Intervention module",
    type: "Intervention type",
    age: "Age group",
    setting: "Setting",
    design: "Design (within/between)",
    availability: "Data availability",
  };
  const FACET_FIELD = { age: "Age_Group", setting: "Setting", design: "within_between", availability: "dataset_availability_status" };

  const RAIL = 42;
  const SNAP = 150;
  const WIDE = 1341;
  const fieldIndex = new Map();
  // Per-DV effect fields filled automatically in coder mode, so a manual entry
  // is never overwritten and a later manual effect can clear the derived ones.
  const dvAutoFill = new Map();   // "recIdx:dvIdx" -> Set(fieldName)

  const DESCRIPTIVE_GROUPS = [
    { title: "Descriptives — tested timepoint", names: ["n1i", "m1i", "sd1i", "n2i", "m2i", "sd2i"] },
    { title: "Descriptives — pre", names: ["n1i_pre", "m1i_pre", "sd1i_pre", "n2i_pre", "m2i_pre", "sd2i_pre"] },
    { title: "Descriptives — post", names: ["n1i_post", "m1i_post", "sd1i_post", "n2i_post", "m2i_post", "sd2i_post"] },
  ];

  const STAT_FAMILIES = {
    descriptives: { label: "Descriptives — n, M, SD", groups: DESCRIPTIVE_GROUPS },
    t: { label: "t statistic", groups: [{ title: "t test", names: ["t_val", "df_den", "n1i", "n2i"] }] },
    F: { label: "F statistic", groups: [{ title: "F test", names: ["F_val", "df_num", "df_den", "n1i", "n2i"] }] },
    r: { label: "Correlation r", groups: [{ title: "Correlation", names: ["r_val", "n_total"] }] },
    yi: { label: "Effect size yi + variance", groups: [{ title: "Effect size (yi)", names: ["yi", "vi", "sei"] }] },
    dg: {
      label: "Cohen's d / Hedges' g",
      groups: [{ title: "Standardised mean difference", names: ["d_val", "g_val", "d_ci_low", "d_ci_high", "g_ci_low", "g_ci_high", "d_se", "g_se", "n1i", "n2i"] }],
    },
    or: {
      label: "Odds ratio / log-odds",
      groups: [{ title: "Odds & risk", names: ["log_odds", "odds_ratio", "or_ci_low", "or_ci_high", "risk_ratio", "n1i", "n2i"] }],
    },
    beta: { label: "Regression beta (+ SE)", groups: [{ title: "Regression", names: ["beta_val", "beta_se", "z_val", "n_total"] }] },
    eta: { label: "Partial eta squared", groups: [{ title: "ANOVA effect size", names: ["eta2_p", "eta2_p_ci_low", "eta2_p_ci_high", "F_val", "df_num", "df_den"] }] },
    p: { label: "p-value / chi-square", groups: [{ title: "Significance", names: ["chi2_val", "p_val", "p_censored"] }] },
    all: {
      label: "All metrics (show everything)",
      groups: [
        ...DESCRIPTIVE_GROUPS,
        { title: "t statistic", names: ["t_val", "df_den"] },
        { title: "F statistic", names: ["F_val", "df_num", "df_den"] },
        { title: "Correlation", names: ["r_val", "n_total"] },
        { title: "Odds & risk", names: ["log_odds", "odds_ratio", "or_ci_low", "or_ci_high", "risk_ratio"] },
        { title: "Regression", names: ["beta_val", "beta_se", "z_val"] },
        { title: "ANOVA effect size", names: ["eta2_p", "eta2_p_ci_low", "eta2_p_ci_high"] },
        { title: "Standardised mean difference", names: ["d_val", "g_val", "d_ci_low", "d_ci_high", "g_ci_low", "g_ci_high", "d_se", "g_se"] },
        { title: "Effect size (yi)", names: ["yi", "vi", "sei"] },
        { title: "Significance", names: ["chi2_val", "p_val", "p_censored"] },
      ],
    },
  };

  const DV_HEAD_GROUPS = [
    { title: "Completeness", names: ["missing_metrics_reason"] },
    {
      title: "Variable",
      names: ["dv_name", "dv_domain", "score_types", "discernment_defined_as", "dv_polarity", "scale_type", "scale_points", "scale_min", "scale_max"],
    },
    {
      title: "Contrast & groups",
      names: ["comparison_label", "group1_label", "group2_label", "contrast_type", "eligible_primary", "timepoint_1", "timepoint_2", "posttest_type", "delay_weeks"],
    },
  ];

  const DV_EXTRA_GROUPS = [
    { title: "Effect size metadata", names: ["effect_size_type_reported", "effect_favors", "effect_direction_meaning", "conversion_method"] },
    { title: "Conversion detail", names: ["conversion_inputs", "cell_descriptives"] },
  ];

  /* ---------------------------------------------------------- toast + dialog */
  const toast = window.CS.toast;

  function confirmDialog(title, text, okLabel = "Confirm", danger = false) {
    return new Promise((resolve) => {
      const dlg = $("#dialog");
      const ok = $("#dialog-ok");
      const cancel = $("#dialog-cancel");
      $("#dialog-title").textContent = title;
      $("#dialog-text").textContent = text;
      ok.textContent = okLabel;
      ok.className = "btn " + (danger ? "danger" : "primary");
      let settled = false;
      const cleanup = () => {
        ok.removeEventListener("click", onOk);
        cancel.removeEventListener("click", onCancel);
        dlg.removeEventListener("close", onClose);
      };
      const done = (v) => {
        if (settled) return;
        settled = true;
        cleanup();
        if (dlg.open) dlg.close();      // fires "close"; onClose is already detached
        resolve(v);
      };
      const onOk = () => done(true);
      const onCancel = () => done(false);
      // Esc / backdrop / any other native close must also settle the promise,
      // otherwise the awaiting flow (e.g. selectPaper) hangs forever.
      const onClose = () => done(false);
      ok.addEventListener("click", onOk);
      cancel.addEventListener("click", onCancel);
      dlg.addEventListener("close", onClose);
      dlg.showModal();
    });
  }

  function safeStore(k, v) {
    try { localStorage.setItem(k, v); } catch { /* ignore */ }
  }

  /* ---------------------------------------------------------- key helpers */
  function parseKey(key) {
    const m = key.match(/^(.*?)\s*\((\d{4})([a-z]?)\)\s*$/);
    const authorPart = m ? m[1].trim() : key;
    const year = m ? m[2] : "";
    let first = authorPart.replace(/\s+et al\.?$/i, "").split(" & ")[0].trim();
    first = first.replace(/\s*\(.*\)$/, "").trim();
    return { authorPart, year, first, token: first.replace(/[^\w-]/g, "") };
  }

  function moduleLabel(name) {
    const m = state.schema && state.schema.modules[name];
    return m ? (m.label || name) : (name || "—");
  }

  /* ---------------------------------------------------------- blank records */
  function blankValue(def) {
    if (def.name === "Gender_Ratio" || def.name === "conversion_inputs") return null;
    if (def.name === "Covariates" || def.name === "factor_levels" || def.name === "cell_descriptives") return [];
    if ((def.type || "").startsWith("list")) return [];
    if (def.base_type === "integer" || def.base_type === "number") return null;
    if ((def.type || "").includes("|null")) return null;
    return "";
  }

  function blankDv(prev) {
    const dv = {};
    state.schema.dv_fields.forEach((f) => { dv[f.name] = blankValue(f); });
    dv.cell_descriptives = [];
    dv.conversion_inputs = null;
    if (state.coderMode) {
      // coder mode assumes a pre -> post measurement per contrast; the coder
      // fills one pre/post matrix per DV.
      dv.timepoint_1 = "pre";
      dv.timepoint_2 = "post";
      // carry the shared per-contrast metadata from the previous DV so only the
      // numbers have to be typed for each additional contrast.
      if (prev) {
        ["dv_domain", "contrast_type", "eligible_primary", "group1_label",
         "group2_label", "scale_type", "scale_points", "timepoint_1", "timepoint_2"]
          .forEach((k) => {
            if (prev[k] != null && prev[k] !== "") dv[k] = JSON.parse(JSON.stringify(prev[k]));
          });
      }
    }
    return dv;
  }

  function blankRecord(key, expNo = 1) {
    const rec = {};
    state.schema.common_fields.forEach((f) => { rec[f.name] = blankValue(f); });
    rec.DV_Metrics = [];
    if (key) {
      const p = parseKey(key);
      rec.Year = p.year ? Number(p.year) : null;
      rec.Authors = p.authorPart;
      rec.Study_ID = `${p.token}${p.year}_Exp${expNo}`;
      rec.Rationale_Notes = "";
    }
    return rec;
  }

  /* Blind coding hides every value but keeps the original's STRUCTURE so the new
     coder's file stays compatible: the experiment tabs (with their titles), the
     intervention arms (with their labels) and one blank DV row per original
     contrast (name + domain + arm binding).  All numeric / coded values are blank. */
  const BLIND_META_SKIP = new Set(["Rationale_Notes", "Study_Description"]);
  function blindSeedRecords(records) {
    const metaNames = (state.schema.common_fields || [])
      .filter((f) => f.group === "Metadata & Identification" && !BLIND_META_SKIP.has(f.name))
      .map((f) => f.name);
    return (records || []).map((src, i) => {
      const rec = blankRecord(state.key, i + 1);
      metaNames.forEach((k) => {
        if (src && src[k] !== undefined && src[k] !== null && src[k] !== "") {
          rec[k] = JSON.parse(JSON.stringify(src[k]));
        }
      });
      if (src && Array.isArray(src.Interventions) && src.Interventions.length) {
        rec.Interventions = src.Interventions.map((iv) => {
          const out = {};
          ["Intervention_ID", "Intervention_Type", "label"].forEach((k) => {
            if (iv && iv[k] !== undefined && iv[k] !== null) out[k] = JSON.parse(JSON.stringify(iv[k]));
          });
          return out;
        });
      }
      const dvs = (src && Array.isArray(src.DV_Metrics)) ? src.DV_Metrics : [];
      rec.DV_Metrics = dvs.map((dv) => {
        const nd = blankDv();
        ["dv_name", "dv_domain", "Intervention_ID"].forEach((k) => {
          if (dv && dv[k] !== undefined && dv[k] !== null && dv[k] !== "") nd[k] = dv[k];
        });
        return nd;
      });
      // Sample detail: keep the covariate labels (which variables were controlled
      // for, and against which DV) but hide the correlation values.
      if (src && Array.isArray(src.Covariates) && src.Covariates.length) {
        rec.Covariates = src.Covariates.map((c) => {
          const out = {};
          if (c && c.covariate !== undefined && c.covariate !== null && c.covariate !== "") out.covariate = c.covariate;
          if (c && c.dv_name !== undefined && c.dv_name !== null && c.dv_name !== "") out.dv_name = c.dv_name;
          return out;
        }).filter((c) => Object.keys(c).length);
      }
      return rec;
    });
  }

  // Module key(s) implied by a record's list-valued Intervention_Type.
  function recordModules(rec) {
    const schema = state.schema || {};
    const modules = schema.modules || {};
    const map = schema.intervention_type_modules || {};
    const types = Array.isArray(rec && rec.Intervention_Type)
      ? rec.Intervention_Type.map((t) => String(t))
      : (rec && rec.Intervention_Type ? [String(rec.Intervention_Type)] : []);
    const out = [];
    types.forEach((t) => (map[t] || []).forEach((m) => {
      if (modules[m] && !out.includes(m)) out.push(m);
    }));
    Object.keys(modules).forEach((name) => {
      const mts = modules[name].intervention_types || [];
      if (!out.includes(name) && types.some((t) => mts.includes(t))) out.push(name);
    });
    return out;
  }

  function ensureModuleFields(rec) {
    recordModules(rec).forEach((name) => {
      const mod = state.schema.modules[name];
      if (!mod) return;
      mod.fields.forEach((f) => {
        if (!(f.name in rec)) rec[f.name] = blankValue(f);
      });
    });
  }

  /* ---------------------------------------------------- per-intervention model
     An experiment record is the parent / shared coding.  It may also carry
     `Interventions`: one entry per arm, each with a single Intervention_Type and
     an `overrides` map holding ONLY the fields that differ from the parent.
     The effective coding of an intervention is the parent overlaid with its
     overrides.  A record without `Interventions` behaves exactly as before. */
  function interventionTypes() {
    const f = (state.schema && state.schema.common_fields || []).find((x) => x.name === "Intervention_Type");
    return (f && f.vocab) || [];
  }

  function recordInterventions(rec) {
    return Array.isArray(rec && rec.Interventions) ? rec.Interventions : [];
  }

  function selectedIntervention(rec) {
    const list = recordInterventions(rec);
    const i = state.selIntervention;
    return Number.isInteger(i) && i >= 0 && i < list.length ? list[i] : null;
  }

  function hasOverride(inter, name) {
    return !!inter && inter.overrides && Object.prototype.hasOwnProperty.call(inter.overrides, name);
  }

  function nextInterventionId(rec) {
    const used = new Set(recordInterventions(rec).map((iv) => iv && iv.Intervention_ID));
    const base = String((rec && rec.Study_ID) || "Study").replace(/[^\w-]/g, "") || "Study";
    let n = 1;
    while (used.has(`${base}_I${n}`)) n += 1;
    return `${base}_I${n}`;
  }

  /* The object the form renders: the parent record, or the effective coding when
     an intervention is selected.  Inherited structured fields are cloned so an
     in-place editor can never mutate the parent. */
  function effectiveView(rec) {
    const inter = selectedIntervention(rec);
    if (!inter) return { obj: rec, inter: null, write: null };
    if (!inter.overrides || typeof inter.overrides !== "object" || Array.isArray(inter.overrides)) {
      inter.overrides = {};
    }
    const overrides = inter.overrides;
    const obj = Object.assign({}, rec, overrides);
    // the intervention's single type (not the parent's list) drives which module
    // fields the effective coding shows
    obj.Intervention_Type = inter.Intervention_Type || "";
    delete obj.Interventions;
    delete obj.DV_Metrics;
    Object.keys(obj).forEach((k) => {
      if (hasOverride(inter, k)) return;
      if (obj[k] && typeof obj[k] === "object") obj[k] = JSON.parse(JSON.stringify(obj[k]));
    });
    return { obj, inter, write: (name, v) => writeEffective(inter, name, v) };
  }

  function writeEffective(inter, name, value) {
    if (name === "Intervention_Type") {
      inter.Intervention_Type = Array.isArray(value)
        ? (value[0] != null ? value[0] : "")
        : (value == null ? "" : value);
      return;
    }
    if (!inter.overrides || typeof inter.overrides !== "object" || Array.isArray(inter.overrides)) {
      inter.overrides = {};
    }
    inter.overrides[name] = value;
  }

  function resetOverride(rec, name) {
    const inter = selectedIntervention(rec);
    if (!inter || !inter.overrides || typeof inter.overrides !== "object") return;
    delete inter.overrides[name];
    touch();
    renderForm();
  }

  // The Intervention_Type row becomes a single-choice select while an
  // intervention is selected (its type lives on the intervention, not overrides).
  function interventionTypeField(f) {
    const vocab = (f.vocab && f.vocab.length) ? f.vocab : interventionTypes();
    return Object.assign({}, f, {
      editor: "select",
      type: "string",
      base_type: "string",
      options: vocab.map((v) => ({ value: v, label: v })),
    });
  }

  function addIntervention(rec) {
    if (!Array.isArray(rec.Interventions)) rec.Interventions = [];
    const parentTypes = Array.isArray(rec.Intervention_Type)
      ? rec.Intervention_Type.slice()
      : (rec.Intervention_Type ? [rec.Intervention_Type] : []);
    const vocab = interventionTypes();
    const inter = {
      Intervention_ID: nextInterventionId(rec),
      Intervention_Type: parentTypes[0] || vocab[0] || "",
      label: "",
      overrides: {},
    };
    rec.Interventions.push(inter);
    state.selIntervention = rec.Interventions.length - 1;
    touch();
    renderForm();
  }

  function duplicateIntervention(rec, i) {
    const list = recordInterventions(rec);
    if (!list[i]) return;
    const copy = JSON.parse(JSON.stringify(list[i]));
    copy.Intervention_ID = nextInterventionId(rec);
    copy.label = (copy.label || "") + " copy";
    list.splice(i + 1, 0, copy);
    state.selIntervention = i + 1;
    touch();
    renderForm();
  }

  async function removeIntervention(rec, i) {
    const list = recordInterventions(rec);
    const inter = list[i];
    if (!inter) return;
    const name = inter.label || inter.Intervention_ID || `#${i + 1}`;
    const ok = await confirmDialog(
      "Delete intervention",
      `Delete “${name}”? Variables it overrides revert to the shared parent coding, and DVs bound to it become shared.`,
      "Delete", true
    );
    if (!ok) return;
    (rec.DV_Metrics || []).forEach((dv) => {
      if (dv && dv.Intervention_ID === inter.Intervention_ID) delete dv.Intervention_ID;
    });
    list.splice(i, 1);
    state.selIntervention = -1;
    touch();
    renderForm();
  }

  function refreshOverrideCounts(rec) {
    const inter = selectedIntervention(rec);
    if (!inter) return;
    const n = inter.overrides && typeof inter.overrides === "object" ? Object.keys(inter.overrides).length : 0;
    $$("#interventions-panel .iv-count").forEach((c) => {
      c.textContent = `${n} override${n === 1 ? "" : "s"}`;
      c.classList.toggle("on", n > 0);
    });
  }

  function renderInterventionsPanel(rec) {
    const tabs = $("#interventions-tabs");
    if (tabs) {
      tabs.innerHTML = "";
      const mk = (label, title, active, onClick) => {
        const b = el("button", "cb-num" + (active ? " active" : ""));
        b.type = "button";
        b.title = title;
        b.appendChild(el("span", "cb-num-v", label));
        b.addEventListener("click", onClick);
        tabs.appendChild(b);
      };
      mk("S", "Shared (parent) coding", !selectedIntervention(rec),
         () => { state.selIntervention = -1; renderForm(); });
      recordInterventions(rec).forEach((inter, i) => {
        mk(String(i + 1),
           [inter.Intervention_ID, inter.label, inter.Intervention_Type].filter(Boolean).join(" · ") || `Intervention ${i + 1}`,
           selectedIntervention(rec) === inter,
           () => { state.selIntervention = i; renderForm(); });
      });
      const add = el("button", "cb-num add");
      add.type = "button";
      add.title = "Add an intervention (a new arm that inherits the shared coding)";
      add.appendChild(icon("i-plus", 12));
      add.addEventListener("click", () => addIntervention(rec));
      tabs.appendChild(add);
    }
    const panel = $("#interventions-panel");
    if (!panel) return;
    panel.innerHTML = "";
    const inter = selectedIntervention(rec);
    if (!inter) { panel.hidden = true; return; }
    panel.hidden = false;
    panel.appendChild(interventionEditor(rec, inter, recordInterventions(rec).indexOf(inter)));
  }

  /* Compact editor for the selected intervention, shown directly beneath the
     Experiment / Interventions boxes. */
  function interventionEditor(rec, inter, i) {
    const row = el("div", "iv-editor");

    const typeSel = el("select", "select iv-type");
    const cur = inter.Intervention_Type || "";
    const vocab = interventionTypes();
    const opts = vocab.includes(cur) || !cur ? vocab : vocab.concat([cur]);
    if (!cur) typeSel.appendChild(new Option("— choose type —", ""));
    opts.forEach((v) => typeSel.appendChild(new Option(v, v)));
    typeSel.value = cur;
    typeSel.title = "Intervention type";
    typeSel.addEventListener("change", () => {
      inter.Intervention_Type = typeSel.value;
      touch();
      // Defer the rebuild to the next frame: destroying the <select> while its
      // native popup is still closing orphans the popup/arrow mid-animation.
      requestAnimationFrame(() => renderForm());
    });
    row.appendChild(typeSel);

    row.appendChild(el("span", "iv-id mono", inter.Intervention_ID || ""));

    const label = el("input", "input iv-label");
    label.type = "text";
    label.placeholder = "label (optional)";
    label.value = inter.label || "";
    label.addEventListener("input", () => { inter.label = label.value; touch(); });
    row.appendChild(label);

    const n = inter.overrides && typeof inter.overrides === "object" ? Object.keys(inter.overrides).length : 0;
    const meta = el("div", "iv-meta");
    meta.appendChild(el("span", "iv-count" + (n ? " on" : ""), `${n} override${n === 1 ? "" : "s"}`));
    row.appendChild(meta);

    const actions = el("div", "iv-actions");
    const dup = el("button", "btn ghost icon");
    dup.type = "button";
    dup.tabIndex = -1;
    dup.title = "Duplicate intervention";
    dup.appendChild(icon("i-copy", 13));
    dup.addEventListener("click", (e) => { e.stopPropagation(); duplicateIntervention(rec, i); });
    const rm = el("button", "btn ghost icon danger");
    rm.type = "button";
    rm.tabIndex = -1;
    rm.title = "Delete intervention";
    rm.appendChild(icon("i-trash", 13));
    rm.addEventListener("click", (e) => { e.stopPropagation(); removeIntervention(rec, i); });
    actions.append(dup, rm);
    row.appendChild(actions);

    return row;
  }

  function recordHasContent(rec) {
    const blank = blankRecord(state.key, 1);
    blank.Intervention_Type = rec.Intervention_Type || [];
    ensureModuleFields(blank);
    if ((rec.DV_Metrics || []).length) return true;
    const keys = new Set([...Object.keys(rec), ...Object.keys(blank)]);
    for (const k of keys) {
      if (k === "DV_Metrics") continue;
      if (JSON.stringify(rec[k] ?? null) !== JSON.stringify(blank[k] ?? null)) return true;
    }
    return false;
  }

  function payloadHasContent() {
    return state.records.some((r) => recordHasContent(r));
  }

  // A blind coding should persist only what the coder actually entered, so the
  // file does not carry the hidden source values.  Empty scalars, empty lists,
  // empty objects and all-empty DV rows are dropped.
  function isEmptyValue(v) {
    if (v === null || v === undefined) return true;
    if (typeof v === "string") return v.trim() === "";
    if (Array.isArray(v)) return v.length === 0;
    if (typeof v === "object") return Object.keys(v).length === 0;
    return false;   // numbers (including 0) and booleans count as entered
  }

  function stripEmptyForBlind(records) {
    return (records || []).map((rec) => {
      const out = {};
      Object.keys(rec || {}).forEach((k) => {
        if (k === "DV_Metrics" || k === "Interventions") return;
        if (!isEmptyValue(rec[k])) out[k] = rec[k];
      });
      // Interventions keep their identity/label but never carry coded overrides.
      const keptIvs = (Array.isArray(rec.Interventions) ? rec.Interventions : []).map((iv) => {
        const o = {};
        Object.keys(iv || {}).forEach((k) => {
          if (k === "overrides") {
            if (iv.overrides && typeof iv.overrides === "object" && Object.keys(iv.overrides).length) o.overrides = iv.overrides;
            return;
          }
          if (!isEmptyValue(iv[k])) o[k] = iv[k];
        });
        return o;
      }).filter((o) => Object.keys(o).length);
      if (keptIvs.length) out.Interventions = keptIvs;
      const keptDvs = (Array.isArray(rec.DV_Metrics) ? rec.DV_Metrics : []).map((dv) => {
        const d = {};
        Object.keys(dv || {}).forEach((k) => { if (!isEmptyValue(dv[k])) d[k] = dv[k]; });
        return d;
      }).filter((d) => Object.keys(d).length);
      if (keptDvs.length) out.DV_Metrics = keptDvs;
      return out;
    });
  }

  async function loadSource(kind) {
    const key = state.key;
    if (!key) return;
    if (kind === "blank") {
      state.records = [blankRecord(key, 1)];
      return;
    }
    let records = null;
    if (kind === "original") {
      const d = await api(`/api/extraction/${encodeURIComponent(key)}`);
      records = d.records;
    } else if (kind.startsWith("coding:")) {
      const coder = kind.slice(7);
      const d = await api(`/api/coding/${encodeURIComponent(key)}/${encodeURIComponent(coder)}`);
      records = d.records;
    }
    if (!records || !records.length) {
      state.records = [blankRecord(key, 1)];
      return;
    }
    state.records = JSON.parse(JSON.stringify(records));
  }

  /* ================================================================ facets */
  function codedBy(paper, coder) {
    if (!coder) return false;
    return (paper.codings || []).some((c) => c.coder.toLowerCase() === coder.toLowerCase());
  }

  function facetValueLabel(dim, value) {
    if (dim === "module") return moduleLabel(value);
    const fname = FACET_FIELD[dim];
    if (fname) {
      const f = state.schema.common_fields.find((x) => x.name === fname);
      const opt = f && (f.options || []).find((o) => String(o.value) === String(value));
      if (opt) return CS.optionText(opt.value, opt.label);
    }
    return String(value);
  }

  function facetOptions(dim) {
    const counts = new Map();
    state.papers.forEach((p) => {
      ((p.facets && p.facets[dim]) || []).forEach((v) => counts.set(String(v), (counts.get(String(v)) || 0) + 1));
    });
    const rows = [...counts.entries()];
    if (FACET_FIELD[dim]) rows.sort((a, b) => Number(a[0]) - Number(b[0]));
    else rows.sort((a, b) => facetValueLabel(dim, a[0]).localeCompare(facetValueLabel(dim, b[0])));
    return rows;
  }

  function passesFacets(p) {
    for (const dim of FACET_DIMS) {
      const set = state.facets[dim];
      if (!set || !set.size) continue;
      const vals = (p.facets && p.facets[dim]) || [];
      if (!vals.some((v) => set.has(String(v)))) return false;
    }
    return true;
  }

  function facetCount() {
    return FACET_DIMS.reduce((n, d) => n + state.facets[d].size, 0);
  }

  function updateFacetBadge() {
    const n = facetCount();
    const badge = $("#facet-count");
    badge.textContent = n;
    badge.hidden = n === 0;
  }

  function openFacetDialog() {
    const body = $("#facet-body");
    body.innerHTML = "";
    FACET_DIMS.forEach((dim) => {
      const opts = facetOptions(dim);
      if (!opts.length) return;
      const g = el("div", "facet-group");
      g.appendChild(el("h4", null, FACET_LABELS[dim]));
      opts.forEach(([val, count]) => {
        const lab = el("label");
        const cb = el("input");
        cb.type = "checkbox";
        cb.value = val;
        cb.dataset.dim = dim;
        cb.checked = state.facets[dim].has(val);
        lab.append(cb, el("span", null, facetValueLabel(dim, val)), el("span", "n", String(count)));
        g.appendChild(lab);
      });
      body.appendChild(g);
    });
    if (!body.children.length) body.appendChild(el("p", null, "No study characteristics available yet."));
    $("#facet-dialog").showModal();
  }

  function applyFacets() {
    FACET_DIMS.forEach((dim) => {
      state.facets[dim] = new Set($$(`#facet-body input[data-dim="${dim}"]:checked`).map((c) => c.value));
    });
    updateFacetBadge();
    renderPaperList();
    $("#facet-dialog").close();
  }

  /* ================================================================ paper list */
  function filterPapers(base) {
    const q = state.search.trim().toLowerCase();
    return base.filter((p) => {
      if (!passesFacets(p)) return false;
      // Full-text-excluded studies are hidden unless the eye toggle is on.
      if (p.fulltext_excluded && !state.showExcluded) return false;
      if (q && !p.key.toLowerCase().includes(q)) return false;
      if (state.filter === "coded" && !codedBy(p, state.coder)) return false;
      if (state.filter === "uncoded" && codedBy(p, state.coder)) return false;
      if (state.filter === "missing" && !(p.missing_metrics > 0)) return false;
      return true;
    });
  }

  /* Shuffle: jump to a random study the current coder has not coded yet. */
  function visiblePapers() {
    return filterPapers(state.papers);
  }

  /* Keep a small recency buffer so a shuffle does not immediately re-pick the
     study the user just left or keeps revisiting. */
  function pushShuffleRecent(key) {
    state.shuffleRecent = state.shuffleRecent.filter((k) => k !== key);
    state.shuffleRecent.push(key);
    if (state.shuffleRecent.length > 8) state.shuffleRecent.shift();
  }

  /* Recency-aware random pick from a pool of uncoded studies. */
  function pickUncoded(pool) {
    let pickPool = pool.filter((p) => !state.shuffleRecent.includes(p.key));
    if (!pickPool.length) pickPool = pool;
    const pick = pickPool[Math.floor(Math.random() * pickPool.length)];
    if (pick) pushShuffleRecent(pick.key);
    return pick;
  }

  /* Toast sub-line showing how many coders have already coded the study. */
  function coderCountSub(p) {
    const n = p.n_coders || 0;
    return `${p.key} · ${n === 0 ? "uncoded" : n + " coder" + (n === 1 ? "" : "s") + " coded"}`;
  }

  async function shuffleUncoded() {
    const coder = state.coder.trim();
    if (!coder) { toast("Enter your coder name before shuffling", "err"); $("#coder-input").focus(); return; }
    if (state.dirty) {
      const ok = await save({ quiet: true });
      if (!ok) return;
    }
    const pool = state.papers.filter((p) => !codedBy(p, coder) && p.key !== state.key);
    if (!pool.length) { toast("Nothing left to code", "ok", `Every paper is coded by ${coder}.`); return; }
    const pick = pickUncoded(pool);
    await selectPaper(pick.key, { force: true });
    toast("Random uncoded study", "ok", coderCountSub(pick));
  }

  /* The current coder's own coding of a study, or null. */
  function coderHasCoding(p) {
    const coder = (state.coder || "").trim().toLowerCase();
    if (!coder) return false;
    return (p.codings || []).some((c) => (c.coder || "").toLowerCase() === coder);
  }

  /* Archive one coder's coding of one study (recoverable, never a hard delete). */
  async function deleteCoding(key) {
    const coder = (state.coder || "").trim();
    if (!coder) { toast("Enter your coder name first", "err"); $("#coder-input").focus(); return; }
    const name = (state.byKey[key] && state.byKey[key].name) || key;
    const ok = await confirmDialog("Delete this coding?",
      `Archive ${coder}'s coding of “${name}”? The study stays and the file is moved ` +
      `to archive/codings_deleted/ (you can restore it later).`,
      "Delete coding", true);
    if (!ok) return;
    try {
      await api("/api/coding/delete", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key, coder }),
      });
      toast("Coding deleted", "ok", `${coder} · ${key}`);
      const fresh = await api("/api/papers");
      state.papers = fresh.papers;
      state.byKey = {};
      state.papers.forEach((p) => { state.byKey[p.key] = p; });
      renderPaperList();
      renderCoderOptions();
      if (state.key === key) {
        state.dirty = false;
        await selectPaper(key, { force: true });
      }
    } catch (e) {
      toast("Could not delete the coding", "err", String(e.message || e));
    }
  }

  function renderPaperList() {
    const list = $("#paper-list");
    list.innerHTML = "";
    const filtered = visiblePapers();
    if (!filtered.length) {
      const empty = el("div", "empty");
      empty.appendChild(icon("i-search", 30));
      empty.appendChild(el("b", null, "No papers match"));
      empty.appendChild(el("p", null, "Try a different search or filter."));
      list.appendChild(empty);
      return;
    }
    filtered.forEach((p, i) => {
      const hasCoding = coderHasCoding(p);
      // Mirror the Screening leftbar exactly (`.paper-item screen-item`, `.dot`,
      // `.meta > .k` + `.m`, `.s-badges`).  The `.k` line carries the study name
      // (title, falling back to the key); the coding-specific signals stay as
      // screening-style badges below it.
      const btn = el("button", "paper-item screen-item" + (p.key === state.key ? " active" : "")
        + (p.fulltext_excluded ? " excluded" : ""));
      btn.type = "button";
      const dot = el("span", "dot" + (codedBy(p, state.coder) ? " coded" : "") + (p.key === state.key && state.dirty ? " dirty" : ""));
      const meta = el("div", "meta");
      const krow = el("div", "k");
      krow.appendChild(el("span", null, p.name || p.key));
      if (p.fulltext_excluded) krow.appendChild(el("span", "ft-badge", "excluded"));
      if (p.fully_within) {
        const fw = el("span", "fw-chip", "FW");
        fw.title = "fully-within (no control); excluded from pooled analysis by default";
        krow.appendChild(fw);
      }
      meta.appendChild(krow);
      const m = el("div", "m");
      m.appendChild(el("span", "idx", String(i + 1)));
      const biblio = [p.authors, p.year].filter(Boolean).join(" · ");
      m.appendChild(el("span", null, biblio || p.key));
      meta.appendChild(m);
      const badges = el("div", "s-badges");
      const coders = p.n_coders || 0;
      const ctag = el("span", "s-badge" + (coders ? " coded" : ""));
      ctag.appendChild(icon("i-users", 9));
      ctag.appendChild(el("span", null, coders === 0 ? "uncoded" : `${coders} coder${coders === 1 ? "" : "s"}`));
      badges.appendChild(ctag);
      const ivs = p.n_interventions || 0;
      if (p.n_records > 0 || ivs > 0) {
        badges.appendChild(el("span", "s-badge",
          `${p.n_records || 0} E${ivs > 0 ? `, ${ivs} I` : ""}`));
      }
      if (p.fulltext_excluded && p.fulltext_criterion) {
        badges.appendChild(el("span", "s-badge warn", p.fulltext_criterion));
      } else if (p.missing_metrics > 0) {
        badges.appendChild(el("span", "s-badge warn", `${p.missing_metrics} missing`));
      }
      meta.appendChild(badges);
      btn.appendChild(dot);
      btn.appendChild(meta);
      if (hasCoding) {
        const del = el("button", "paper-del");
        del.type = "button";
        del.title = `Delete ${state.coder.trim()}'s coding of this study`;
        del.setAttribute("aria-label", del.title);
        del.appendChild(icon("i-trash", 12));
        del.addEventListener("click", (e) => {
          e.stopPropagation();
          e.preventDefault();
          deleteCoding(p.key);
        });
        btn.appendChild(del);
      }
      btn.addEventListener("click", () => selectPaper(p.key));
      list.appendChild(btn);
    });
  }

  /* ================================================================ record tabs */
  function renderTabs() {
    const wrap = $("#rec-tabs");
    if (!wrap) return;
    wrap.innerHTML = "";
    state.records.forEach((rec, i) => {
      const tab = el("button", "cb-num" + (i === state.current ? " active" : ""));
      tab.type = "button";
      tab.title = [rec.Study_ID, rec.Title].filter(Boolean).join(" · ") || `Experiment ${i + 1}`;
      tab.appendChild(el("span", "cb-num-v", String(i + 1)));
      if (state.records.length > 1) {
        const x = el("span", "cb-x");
        x.appendChild(icon("i-x", 9));
        x.title = "Remove this experiment";
        x.addEventListener("click", (e) => {
          e.stopPropagation();
          removeRecord(i);
        });
        tab.appendChild(x);
      }
      tab.addEventListener("click", () => {
        state.current = i;
        state.selIntervention = -1;
        state.openDv = new Set();
        syncMissingOpen();
        renderTabs();
        renderForm();
        syncRationale();
      });
      wrap.appendChild(tab);
    });
    const add = el("button", "cb-num add");
    add.type = "button";
    add.title = "Add experiment";
    add.appendChild(icon("i-plus", 12));
    add.addEventListener("click", () => addRecord());
    wrap.appendChild(add);
  }

  function addRecord() {
    const rec = blankRecord(state.key, state.records.length + 1);
    const prev = state.records[state.current];
    if (prev) {
      rec.Authors = prev.Authors;
      rec.Year = prev.Year;
      rec.DOI = prev.DOI;
      rec.Title = prev.Title;
      rec.Intervention_Type = Array.isArray(prev.Intervention_Type)
        ? prev.Intervention_Type.slice() : (prev.Intervention_Type ? [prev.Intervention_Type] : []);
      ensureModuleFields(rec);
    }
    state.records.push(rec);
    state.current = state.records.length - 1;
    state.selIntervention = -1;
    touch();
    renderTabs();
    renderForm();
    syncRationale();
  }

  async function removeRecord(i) {
    if (state.records.length === 1) {
      toast("A paper needs at least one experiment record", "err");
      return;
    }
    const ok = await confirmDialog("Remove experiment", `Remove record #${i + 1} from the working copy?`, "Remove", true);
    if (!ok) return;
    state.records.splice(i, 1);
    state.current = Math.max(0, Math.min(state.current, state.records.length - 1));
    state.selIntervention = -1;
    touch();
    renderTabs();
    renderForm();
    syncRationale();
  }

  /* ================================================================ field controls */
  const emptyAsNull = (def) => (def.type || "").includes("|null") && def.base_type === "string";

  function fieldRow(def, obj, opts = {}) {
    const row = el("div", "field");
    row.dataset.field = def.name;
    row.dataset.editor = def.editor || "";
    const lab = el("div", "field-label");
    lab.appendChild(el("span", null, def.label || def.name));
    if (def.required) {
      const r = el("span", "req", "*");
      r.title = "required";
      lab.appendChild(r);
    }
    if (opts.showName) lab.appendChild(el("span", "name", def.name));
    if (def.description) {
      lab.dataset.help = def.description;
      lab.classList.add("has-help");
    }

    let resetBtn = null;
    const paintOverridden = (on) => {
      row.classList.toggle("overridden", on);
      let tag = lab.querySelector(".ovr-tag");
      if (on && !tag) {
        tag = el("span", "ovr-tag", "overridden");
        tag.title = "This field differs from the shared parent coding";
        lab.appendChild(tag);
      } else if (!on && tag) {
        tag.remove();
      }
      if (on && !resetBtn && opts.onReset) {
        resetBtn = el("button", "btn ghost override-reset");
        resetBtn.type = "button";
        resetBtn.tabIndex = -1;
        resetBtn.title = "Reset to the inherited (parent) value";
        resetBtn.appendChild(icon("i-undo", 12));
        resetBtn.appendChild(el("span", null, "reset to inherited"));
        resetBtn.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); opts.onReset(); });
        control.appendChild(resetBtn);
      } else if (!on && resetBtn) {
        resetBtn.remove();
        resetBtn = null;
      }
    };
    row.appendChild(lab);

    const control = el("div", "control");
    const inner = Object.assign({}, opts);
    if (opts.liveOverride) inner.onCommit = () => { paintOverridden(true); if (opts.onOverride) opts.onOverride(); };
    control.appendChild(controlFor(def, obj, obj[def.name], inner));
    paintOverridden(!!opts.overridden);
    if (def.editor === "number" && def.hint_levels) {
      const hint = Object.entries(def.hint_levels).map(([k, v]) => `${k}=${v}`).join(" · ");
      control.appendChild(el("div", "field-hint", hint));
    }
    row.appendChild(control);
    return row;
  }

  function controlFor(def, obj, value, opts) {
    const write = opts.write || ((name, v) => { obj[name] = v; });
    const commit = (v) => {
      write(def.name, v);
      touch();
      if (opts.onChange) opts.onChange(v);
      if (opts.onCommit) opts.onCommit(v);
    };
    switch (def.editor) {
      case "module-select": {
        const sel = el("select", "select");
        sel.appendChild(new Option("— choose module —", ""));
        (state.schema.vocabularies.modules || []).forEach((m) =>
          sel.appendChild(new Option(moduleLabel(m), m)));
        if (value && !(state.schema.modules[value] || state.schema.vocabularies.modules.includes(value))) {
          sel.appendChild(new Option(`${value} (unlisted)`, value));
        }
        sel.value = value || "";
        sel.addEventListener("change", () => {
          commit(sel.value);
          requestAnimationFrame(() => renderForm());
        });
        return sel;
      }
      case "select":
        return isDichotomous(def) ? checkControl(def, value, commit) : selectControl(def, value, commit);
      case "number": {
        const inp = el("input", "input mono-val");
        inp.type = "number";
        inp.step = "any";
        inp.value = value === null || value === undefined ? "" : value;
        inp.placeholder = "—";
        inp.addEventListener("input", () => commit(inp.value === "" ? null : Number(inp.value)));
        return inp;
      }
      case "textarea": {
        const ta = el("textarea", "textarea");
        ta.rows = def.name === "Study_Description" ? 3 : 2;
        ta.value = value == null ? "" : String(value);
        ta.placeholder = "—";
        ta.addEventListener("input", () => commit(emptyAsNull(def) ? (ta.value.trim() || null) : ta.value));
        return ta;
      }
      case "chips":
      case "multiselect":
        return chipsControl(def, value, commit);
      case "objectlist":
        return objectListControl(def, value, commit);
      case "kv":
        return kvControl(value, commit);
      case "json":
        return jsonControl(value, commit);
      default: {
        const inp = el("input", "input");
        inp.type = "text";
        inp.value = value == null ? "" : String(value);
        inp.placeholder = "—";
        inp.addEventListener("input", () => commit(emptyAsNull(def) ? (inp.value.trim() || null) : inp.value));
        return inp;
      }
    }
  }

  function isDichotomous(def) {
    return def.editor === "select" && Array.isArray(def.options) && def.options.length === 2;
  }

  /* A two-level variable is a checkbox: unchecked = first level, checked = second.
     A never-touched field stays null (indeterminate) and is only committed on change. */
  function checkControl(def, value, commit) {
    const [off, on] = def.options;
    const isString = typeof off.value === "string";
    const blank = value === null || value === undefined || value === "";
    const known = blank || String(value) === String(off.value) || String(value) === String(on.value);
    if (!known) return selectControl(def, value, commit);   // keep unlisted legacy values safe

    const wrap = el("div");
    const lab = el("label", "check");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = !blank && String(value) === String(on.value);
    cb.indeterminate = blank;
    cb.title = `${CS.optionText(on.value, on.label)} (checked) / `
      + `${CS.optionText(off.value, off.label)} (unchecked)`;
    cb.addEventListener("change", () => {
      cb.indeterminate = false;
      const v = cb.checked ? on.value : off.value;
      commit(isString ? String(v) : Number(v));
    });
    lab.append(cb, el("span", null, on.label));
    wrap.appendChild(lab);
    wrap.appendChild(el("div", "field-hint", `checked = ${on.label} · unchecked = ${off.label}`));
    return wrap;
  }

  function selectControl(def, value, commit) {
    const wrap = el("div", "control");
    const sel = el("select", "select");
    const options = def.options || [];
    const isString = !!(options[0] && typeof options[0].value === "string");
    sel.appendChild(new Option("— not set —", "__none__"));
    let matched = false;
    options.forEach((o) => {
      /* a level shows its number and its name ("1 - Lab") - the same rule
         the scheme editor uses, so the two can never disagree */
      const text = optionText(o.value, o.label);
      const opt = new Option(text, String(o.value));
      if (value !== null && value !== undefined && String(value) === String(o.value)) {
        opt.selected = true;
        matched = true;
      }
      sel.appendChild(opt);
    });
    if (value !== null && value !== undefined && value !== "" && !matched) {
      const opt = new Option(`${value} — (unlisted)`, String(value));
      opt.selected = true;
      sel.appendChild(opt);
    }
    if (value === null || value === undefined || value === "") sel.value = "__none__";
    sel.addEventListener("change", () => {
      const v = sel.value;
      if (v === "__none__") commit(null);
      else commit(isString ? v : Number(v));
    });
    wrap.appendChild(sel);
    return wrap;
  }

  function miniBtn(iconId, title, onClick) {
    const b = el("button", "rm");
    b.type = "button";
    b.tabIndex = -1;
    b.title = title;
    b.appendChild(icon(iconId, 14));
    b.addEventListener("click", onClick);
    return b;
  }

  function chipsControl(def, value, commit) {
    const arr = Array.isArray(value) ? value.slice() : [];
    const wrap = el("div");
    const box = el("div", "chips");
    const input = el("input");
    input.type = "text";
    input.placeholder = "type and press Enter…";

    const paint = () => {
      $$(".chip-token", box).forEach((n) => n.remove());
      arr.forEach((v, i) => {
        const t = el("span", "chip-token");
        t.appendChild(el("span", null, String(v)));
        const b = el("button");
        b.type = "button";
        b.tabIndex = -1;
        b.title = "Remove";
        b.appendChild(icon("i-x", 10));
        b.addEventListener("click", () => {
          arr.splice(i, 1);
          commit(arr.slice());
          paint();
        });
        t.appendChild(b);
        box.insertBefore(t, input);
      });
    };
    const addValue = (raw) => {
      const v = raw.trim();
      if (!v) return;
      if (!arr.includes(v)) {
        arr.push(v);
        commit(arr.slice());
        paint();
      }
      input.value = "";
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === ",") {
        e.preventDefault();
        addValue(input.value);
      } else if (e.key === "Backspace" && !input.value && arr.length) {
        arr.pop();
        commit(arr.slice());
        paint();
      }
    });
    input.addEventListener("blur", () => addValue(input.value));
    box.appendChild(input);
    wrap.appendChild(box);
    paint();

    const sugg = (def.vocab || []).filter((v) => !arr.includes(v));
    if (sugg.length) {
      const s = el("div", "chip-suggest");
      sugg.slice(0, 24).forEach((v) => {
        const b = el("button", "s");
        b.type = "button";
        b.tabIndex = -1;
        b.textContent = v;
        b.addEventListener("click", () => addValue(v));
        s.appendChild(b);
      });
      wrap.appendChild(s);
    }
    return wrap;
  }

  function objectListControl(def, value, commit) {
    const arr = Array.isArray(value) ? value : [];
    // a field with the objectlist editor but no columns yet (the scheme
    // editor can leave it empty) must not throw
    const columns = Array.isArray(def.columns) ? def.columns : [];
    const wrap = el("div", "objlist");
    const table = el("table");
    const thead = el("thead");
    const hr = el("tr");
    columns.forEach((c) => hr.appendChild(el("th", null, c.name)));
    hr.appendChild(el("th"));
    thead.appendChild(hr);
    table.appendChild(thead);
    const tbody = el("tbody");

    const paint = () => {
      tbody.innerHTML = "";
      arr.forEach((rowObj, i) => {
        const tr = el("tr");
        if (!rowObj) arr[i] = rowObj = {};
        columns.forEach((c) => {
          const td = el("td");
          const cur = rowObj[c.name];
          if (c.type === "list") {
            const inp = el("input", "input");
            inp.type = "text";
            inp.placeholder = "comma-separated";
            inp.value = Array.isArray(cur) ? cur.join(", ") : (cur || "");
            inp.addEventListener("input", () => {
              arr[i][c.name] = inp.value.split(",").map((s) => s.trim()).filter(Boolean);
              commit(arr);
            });
            td.appendChild(inp);
          } else {
            const inp = el("input", "input" + (c.type === "number" ? " mono-val" : ""));
            inp.type = c.type === "number" ? "number" : "text";
            if (c.type === "number") inp.step = "any";
            inp.value = cur == null ? "" : cur;
            inp.placeholder = "—";
            inp.addEventListener("input", () => {
              arr[i][c.name] = c.type === "number" ? (inp.value === "" ? null : Number(inp.value)) : inp.value;
              commit(arr);
            });
            td.appendChild(inp);
          }
          tr.appendChild(td);
        });
        const td = el("td");
        td.appendChild(miniBtn("i-trash", "Remove row", () => {
          arr.splice(i, 1);
          commit(arr.slice());
          paint();
        }));
        tr.appendChild(td);
        tbody.appendChild(tr);
      });
    };
    paint();
    table.appendChild(tbody);
    wrap.appendChild(table);
    const foot = el("div", "objlist-foot");
    const add = el("button", "btn ghost");
    add.type = "button";
    add.tabIndex = -1;
    add.appendChild(icon("i-plus", 13));
    add.appendChild(el("span", null, "Add row"));
    add.addEventListener("click", () => {
      const rowObj = {};
      columns.forEach((c) => { rowObj[c.name] = c.type === "list" ? [] : null; });
      arr.push(rowObj);
      commit(arr.slice());
      paint();
    });
    foot.appendChild(add);
    wrap.appendChild(foot);
    return wrap;
  }

  function kvControl(value, commit) {
    const map = value && typeof value === "object" && !Array.isArray(value) ? Object.assign({}, value) : {};
    const entries = Object.entries(map);
    const wrap = el("div", "objlist");
    const table = el("table");
    const thead = el("thead");
    const hr = el("tr");
    hr.appendChild(el("th", null, "key"));
    hr.appendChild(el("th", null, "value"));
    hr.appendChild(el("th"));
    thead.appendChild(hr);
    table.appendChild(thead);
    const tbody = el("tbody");
    const write = () => {
      const out = {};
      entries.forEach(([k, v]) => { if (k !== "") out[k] = v; });
      commit(Object.keys(out).length ? out : null);
    };
    const paint = () => {
      tbody.innerHTML = "";
      entries.forEach((pair, i) => {
        const tr = el("tr");
        const kd = el("td");
        const ki = el("input", "input");
        ki.value = pair[0];
        ki.placeholder = "label";
        ki.addEventListener("input", () => { entries[i][0] = ki.value; write(); });
        kd.appendChild(ki);
        const vd = el("td");
        const vi = el("input", "input mono-val");
        vi.type = "number";
        vi.step = "any";
        vi.value = pair[1] == null ? "" : pair[1];
        vi.addEventListener("input", () => { entries[i][1] = vi.value === "" ? null : Number(vi.value); write(); });
        vd.appendChild(vi);
        const ad = el("td");
        ad.appendChild(miniBtn("i-trash", "Remove entry", () => { entries.splice(i, 1); write(); paint(); }));
        tr.append(kd, vd, ad);
        tbody.appendChild(tr);
      });
    };
    paint();
    table.appendChild(tbody);
    wrap.appendChild(table);
    const foot = el("div", "objlist-foot");
    const add = el("button", "btn ghost");
    add.type = "button";
    add.tabIndex = -1;
    add.appendChild(icon("i-plus", 13));
    add.appendChild(el("span", null, "Add entry"));
    add.addEventListener("click", () => { entries.push(["", null]); paint(); });
    foot.appendChild(add);
    wrap.appendChild(foot);
    return wrap;
  }

  function jsonControl(value, commit) {
    const ta = el("textarea", "textarea mono-val");
    ta.rows = 3;
    ta.value = value == null ? "" : JSON.stringify(value, null, 1);
    ta.placeholder = "{}";
    ta.addEventListener("input", () => {
      const raw = ta.value.trim();
      if (!raw) {
        ta.classList.remove("json-invalid");
        commit(null);
        return;
      }
      try {
        commit(JSON.parse(raw));
        ta.classList.remove("json-invalid");
      } catch {
        ta.classList.add("json-invalid");
      }
    });
    return ta;
  }

  /* ================================================================ form */
  function sectionCount(fields, obj) {
    let n = 0;
    fields.forEach((f) => {
      const v = obj[f.name];
      if (v === null || v === undefined || v === "") return;
      if (Array.isArray(v) && !v.length) return;
      n += 1;
    });
    return n;
  }

  /* ---------------------------------------------------------- variable search */
  function currentFieldQuery() {
    return state.fieldSearch.trim().toLowerCase();
  }

  function fieldMatches(def, q) {
    if (!q || !def) return true;
    return `${def.name} ${def.label || ""} ${def.group || ""}`.toLowerCase().includes(q);
  }

  function dvMatchText(dv) {
    return `${dv.dv_name || ""} ${dv.comparison_label || ""} ${dv.group1_label || ""} ${dv.group2_label || ""}`;
  }

  /* Every DV field that can appear on a card, regardless of the selected
     statistics family - used so the variable search can reach any field. */
  function allDvFields() {
    const names = [];
    const seen = new Set();
    const add = (n) => { if (!seen.has(n)) { seen.add(n); names.push(n); } };
    DV_HEAD_GROUPS.forEach((g) => g.names.forEach(add));
    Object.values(STAT_FAMILIES).forEach((f) => f.groups.forEach((g) => g.names.forEach(add)));
    DV_EXTRA_GROUPS.forEach((g) => g.names.forEach(add));
    return names.map((n) => state.schema.dv_fields.find((f) => f.name === n)).filter(Boolean);
  }

  function dvMatches(dv, q) {
    if (!q) return true;
    if (dvMatchText(dv).toLowerCase().includes(q)) return true;
    return allDvFields().some((f) => fieldMatches(f, q));
  }

  function makeSection(title, subtitle) {
    const sec = el("details", "section");
    const key = title;
    sec.open = !state.openSections.has("closed:" + key);
    const sum = el("summary");
    const c = icon("i-caret", 14);
    c.classList.add("caret");
    sum.appendChild(c);
    sum.appendChild(el("span", null, title));
    if (subtitle) sum.appendChild(el("span", "count", subtitle));
    sec.appendChild(sum);
    sec.addEventListener("toggle", () => {
      if (sec.open) state.openSections.delete("closed:" + key);
      else state.openSections.add("closed:" + key);
    });
    // In coder mode the native open/close toggle is replaced: clicking a category
    // makes it the single active one (CSS hides the others) and focuses + locates
    // its first variable, so the paper follows.
    sum.addEventListener("click", (e) => {
      if (!state.coderMode) return;
      e.preventDefault();
      coderActivateSection(sec);
    });
    const body = el("div", "group-body grid-fields");
    sec.appendChild(body);
    return { sec, body, sum };
  }

  function renderForm() {
    const form = $("#form");
    const scroller = $(".pane.mid .pane-body");
    const keepTop = scroller ? scroller.scrollTop : 0;
    if (state._srcRec !== state.current || state._srcKey !== state.key) {
      state._srcRec = state.current;
      state._srcKey = state.key;
      state.coderDv = null;
      state.coderTarget = null;
      state.lastMark = null;
      clearPdfMarks();
    }
    form.innerHTML = "";
    const rec = state.records[state.current];
    if (!rec) return;
    const q = currentFieldQuery();

    // Fully-within (single-arm, no-control) study: surface the marker in the
    // coding header so the coder knows the within-person effects are excluded
    // from pooled analysis by default.
    const fwMeta = state.byKey[state.key];
    if (fwMeta && fwMeta.fully_within) {
      const banner = el("div", "fw-banner");
      const chip = el("span", "fw-chip", "FW");
      chip.title = "fully-within (no control); excluded from pooled analysis by default";
      banner.appendChild(chip);
      banner.appendChild(el("span", "fw-banner-text",
        "Fully-within study (no control) — its within-person pre→post effects are " +
        "excluded from the pooled analysis by default."));
      form.appendChild(banner);
    }

    renderInterventionsPanel(rec);
    prefillControlGroups(rec);
    const view = effectiveView(rec);
    ensureModuleFields(view.obj);
    const fieldOpts = (f) => (view.inter ? {
      write: view.write,
      overridden: hasOverride(view.inter, f.name),
      onReset: () => resetOverride(rec, f.name),
      liveOverride: f.name !== "Intervention_Type",
      onOverride: () => refreshOverrideCounts(rec),
    } : {});

    const groups = state.schema.groups.filter((g) => g !== "DV");
    groups.forEach((g) => {
      let fields = state.schema.common_fields.filter((f) => f.group === g);
      if (q) fields = fields.filter((f) => fieldMatches(f, q));
      if (!fields.length) return;
      const subtitle = q
        ? `${fields.length} match${fields.length === 1 ? "" : "es"}`
        : `${sectionCount(fields, view.obj)}/${fields.length} set`;
      const { sec, body } = makeSection(g, subtitle);
      if (state.missingsOnly && !q) sec.open = false;
      if (q) sec.open = true;
      fields.forEach((f) => {
        const def = view.inter && f.name === "Intervention_Type" ? interventionTypeField(f) : f;
        body.appendChild(fieldRow(def, view.obj, fieldOpts(f)));
      });
      form.appendChild(sec);
    });

    const modSec = moduleSection(view.obj, q, fieldOpts);
    if (modSec) form.appendChild(modSec);

    const modNames = new Set();
    recordModules(view.obj).forEach((n) => {
      const m = state.schema.modules[n];
      if (m) m.fields.forEach((f) => modNames.add(f.name));
    });
    const commonNames = new Set(state.schema.common_fields.map((f) => f.name));
    const otherModule = Object.keys(view.obj).filter(
      (k) => k !== "DV_Metrics" && k !== "Interventions" && !commonNames.has(k) && !modNames.has(k) && fieldIndex.has(k)
        && fieldMatches(fieldIndex.get(k), q)
    );
    if (otherModule.length) {
      const { sec, body } = makeSection("Fields from other modules", `${otherModule.length} kept`);
      if (state.missingsOnly && !q) sec.open = false;
      if (q) sec.open = true;
      otherModule.forEach((k) => body.appendChild(fieldRow(fieldIndex.get(k), view.obj,
        Object.assign({ showName: true }, fieldOpts(fieldIndex.get(k))))));
      form.appendChild(sec);
    }

    const extras = Object.keys(view.obj).filter(
      (k) => k !== "DV_Metrics" && k !== "Interventions" && !fieldIndex.has(k) && !commonNames.has(k)
        && fieldMatches(guessDef(k, view.obj[k]), q)
    );
    if (extras.length) {
      const { sec, body } = makeSection("Additional fields (not in schema)", `${extras.length}`);
      if (state.missingsOnly && !q) sec.open = false;
      if (q) sec.open = true;
      extras.forEach((k) => {
        const def = guessDef(k, view.obj[k]);
        body.appendChild(fieldRow(def, view.obj, Object.assign({ showName: true }, fieldOpts(def))));
      });
      form.appendChild(sec);
    }

    const dvSec = dvSection(rec, q);
    if (dvSec) form.appendChild(dvSec);

    if (q && !form.querySelector(".field")) {
      const empty = el("div", "empty");
      empty.appendChild(icon("i-search", 28));
      empty.appendChild(el("b", null, "No variables match"));
      empty.appendChild(el("p", null, `Nothing in this experiment matches “${state.fieldSearch.trim()}”.`));
      form.appendChild(empty);
    }
    if (scroller) scroller.scrollTop = keepTop;
    renderGroupSizes(rec);
    renderFulltext();
    if (state.coderMode) decorateCoderMode();
    renderCompareGutter();
  }

  function moduleSection(obj, q, fieldOpts) {
    const names = recordModules(obj);
    const mods = names.map((n) => state.schema.modules[n]).filter(Boolean);
    const seen = new Set();
    let fields = [];
    mods.forEach((mod) => (mod.fields || []).forEach((f) => {
      if (!seen.has(f.name)) { seen.add(f.name); fields.push(f); }
    }));
    if (q) fields = fields.filter((f) => fieldMatches(f, q));
    if (q && !fields.length) return null;
    const title = mods.length
      ? `Intervention module — ${mods.map((m) => m.label || "").filter(Boolean).join(" + ")}`
      : "Intervention module";
    const { sec, body } = makeSection(
      title,
      mods.length ? `${sectionCount(fields, obj)}/${fields.length} set` : ""
    );
    if (state.missingsOnly && !q) sec.open = false;
    if (q) sec.open = true;
    if (!mods.length) {
      const empty = el("div", "empty");
      empty.appendChild(icon("i-alert", 26));
      empty.appendChild(el("b", null, "No intervention type selected"));
      empty.appendChild(el("p", null, "Set Intervention Type above (Intervention core) to reveal the module-specific coding fields."));
      body.appendChild(empty);
      return sec;
    }
    mods.forEach((mod) => {
      if (mod.description) {
        const d = el("p", "field-hint", mod.description);
        d.style.margin = "2px 8px 8px";
        body.appendChild(d);
      }
    });
    fields.forEach((f) => body.appendChild(fieldRow(f, obj, fieldOpts ? fieldOpts(f) : {})));
    return sec;
  }

  function guessDef(name, value) {
    const existing = fieldIndex.get(name);
    if (existing) return existing;
    let base = "string";
    if (typeof value === "number") base = "number";
    else if (Array.isArray(value)) base = "list";
    else if (value && typeof value === "object") base = "object";
    const def = { name, label: name, type: base, base_type: base, description: "", required: false };
    def.editor = base === "number" ? "number" : base === "list" ? "chips" : base === "object" ? "json" : "text";
    return def;
  }

  /* ---------------------------------------------------------- missing-only */
  function missingIndexes(rec) {
    return rec.DV_Metrics.map((dv, i) => (dvStatus(dv).cls !== "ok" ? i : -1)).filter((i) => i >= 0);
  }

  function syncMissingOpen() {
    const rec = state.records[state.current];
    if (!rec) return;
    if (state.missingsOnly) state.openDv = new Set(missingIndexes(rec));
  }

  function toggleMissings() {
    state.missingsOnly = !state.missingsOnly;
    safeStore("cs.missingsOnly", state.missingsOnly ? "1" : "0");
    $("#missings-only").setAttribute("aria-pressed", state.missingsOnly ? "true" : "false");
    state.openDv = new Set();
    syncMissingOpen();
    renderForm();
  }

  /* ---------------------------------------------------------- DV section */
  /* The DV list is scoped to the selected intervention: a contrast bound to an
     intervention belongs to that arm only; an unbound contrast is shared by all.
     ``showAllDvs`` reveals every arm's contrasts for review. */
  function dvScope(rec) {
    const all = (rec && rec.DV_Metrics) || [];
    const arm = selectedIntervention(rec);
    if (!arm || state.showAllDvs) {
      return { arm, indexes: new Set(all.map((_, i) => i)), hidden: 0, total: all.length };
    }
    const idx = new Set();
    let hidden = 0;
    all.forEach((dv, i) => {
      if (!dv.Intervention_ID || dv.Intervention_ID === arm.Intervention_ID) idx.add(i);
      else hidden += 1;
    });
    return { arm, indexes: idx, hidden, total: all.length };
  }

  function armTitle(arm) {
    if (!arm) return "";
    return arm.label || arm.Intervention_Type || arm.Intervention_ID || "intervention";
  }

  function dvSection(rec, q) {
    const scope = dvScope(rec);
    // A shared contrast with a coded effect is applied to every arm; if its effect
    // is arm-specific that double-counts it, so warn the coder to bind it.
    const sharedEffects = recordInterventions(rec).length > 1
      ? rec.DV_Metrics.filter((dv) => !dv.Intervention_ID && dv.eligible_primary === 1
          && ["yi", "g_val", "d_val", "t_val", "r_val", "beta_val", "m1i", "m1i_post"]
            .some((k) => isNum(dv[k]))).length
      : 0;
    const allRows = rec.DV_Metrics.map((dv, i) => ({ dv, i }))
      .filter(({ i }) => scope.indexes.has(i));
    let rows = q ? allRows.filter((r) => dvMatches(r.dv, q)) : allRows;
    if (q && !rows.length) return null;
    const wrapped = el("div", "section dv-section");
    wrapped.open = true;
    const sum = el("summary");
    const c = icon("i-caret", 14);
    c.classList.add("caret");
    sum.appendChild(c);
    sum.appendChild(el("span", null, "DV metrics (contrasts)"));
    const total = scope.indexes.size;
    const missing = rec.DV_Metrics
      .map((dv, i) => (scope.indexes.has(i) && dvStatus(dv).cls !== "ok" ? i : -1))
      .filter((i) => i >= 0).length;
    const countText = q
      ? `${rows.length} matching row${rows.length === 1 ? "" : "s"}`
      : (state.missingsOnly ? `${missing} missing of ${total}` : `${total} row${total === 1 ? "" : "s"}`);
    sum.appendChild(el("span", "count", countText));
    if (scope.arm) {
      const sc = el("span", "dv-scope",
        state.showAllDvs
          ? `all interventions · ${scope.total}`
          : `${armTitle(scope.arm)} · ${total}/${scope.total}` +
            (scope.hidden ? ` (${scope.hidden} other arm${scope.hidden === 1 ? "" : "s"} hidden)` : ""));
      sc.title = "The DV list is scoped to the selected intervention; unbound contrasts are shared.";
      sum.appendChild(sc);
      const toggle = el("button", "btn ghost");
      toggle.type = "button";
      toggle.tabIndex = -1;
      toggle.style.marginLeft = "8px";
      toggle.textContent = state.showAllDvs ? "Only this arm" : "Show all arms";
      toggle.addEventListener("click", (e) => {
        e.preventDefault(); e.stopPropagation();
        state.showAllDvs = !state.showAllDvs;
        renderForm();
      });
      sum.appendChild(toggle);
    }
    const add = el("button", "btn ghost");
    add.type = "button";
    add.tabIndex = -1;
    add.style.marginLeft = "10px";
    add.appendChild(icon("i-plus", 13));
    add.appendChild(el("span", null, "Add DV"));
    add.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const prev = rec.DV_Metrics[rec.DV_Metrics.length - 1];
      const nd = blankDv(state.coderMode ? prev : undefined);
      // A new contrast is coded for the arm currently being edited.
      if (scope.arm) nd.Intervention_ID = scope.arm.Intervention_ID;
      else delete nd.Intervention_ID;
      rec.DV_Metrics.push(nd);
      state.openDv.add(rec.DV_Metrics.length - 1);
      dvAutoFill.clear();
      touch();
      renderForm();
    });
    sum.appendChild(add);
    wrapped.appendChild(sum);
    // The DV section is a plain div (not a native <details>), so it needs the
    // same coder-mode activation as the other categories or it can never be
    // opened while coding.  This is what made the DV category unreachable.
    sum.addEventListener("click", (e) => {
      if (!state.coderMode) return;
      e.preventDefault();
      coderActivateSection(wrapped);
    });

    const body = el("div", "group-body");
    const shown = q ? rows : (state.missingsOnly ? rows.filter((r) => dvStatus(r.dv).cls !== "ok") : rows);

    if (!q && sharedEffects) {
      const warn = el("p", "dv-warn",
        `${sharedEffects} shared contrast${sharedEffects === 1 ? "" : "s"} with a coded effect ` +
        `apply to every intervention. If the effect is arm-specific, open the contrast and ` +
        `set its Intervention binding.`);
      body.appendChild(warn);
    }

    if (!rec.DV_Metrics.length) {
      const empty = el("div", "empty");
      empty.appendChild(icon("i-list", 28));
      empty.appendChild(el("b", null, "No contrasts yet"));
      empty.appendChild(el("p", null, "Add one DV row per group comparison per dependent variable, including non-primary contrasts."));
      body.appendChild(empty);
    } else if (!shown.length && scope.arm && !state.showAllDvs) {
      const empty = el("div", "empty");
      empty.appendChild(icon("i-plus", 28));
      empty.appendChild(el("b", null, "No contrasts for this intervention"));
      empty.appendChild(el("p", null, `This arm has no bound contrast yet; unbound contrasts are shared. “Add DV” binds a new row to ${armTitle(scope.arm)}, or use “Show all arms”.`));
      body.appendChild(empty);
    } else if (!shown.length) {
      const empty = el("div", "empty");
      empty.appendChild(icon("i-check", 28));
      empty.appendChild(el("b", null, "No missing metrics"));
      empty.appendChild(el("p", null, "Every contrast in this experiment has complete descriptives or a valid fallback."));
      body.appendChild(empty);
    }
    shown.forEach(({ dv, i }) => body.appendChild(dvCard(rec, dv, i, q)));
    wrapped.appendChild(body);
    return wrapped;
  }

  function inferFamily(dv) {
    const has = (k) => isNum(dv[k]);
    const all = (...ks) => ks.every(has);
    if (all("n1i", "m1i", "sd1i", "n2i", "m2i", "sd2i")) return "descriptives";
    if (all("odds_ratio", "or_ci_low", "or_ci_high")) return "or";
    if (has("log_odds") || has("risk_ratio")) return "or";
    if (has("yi")) return "yi";
    if (has("d_val") || has("g_val")) return "dg";
    if (all("t_val", "n1i", "n2i")) return "t";
    if (all("F_val", "df_num", "df_den")) return "F";
    if (has("r_val")) return "r";
    if (has("beta_val") || has("beta_se")) return "beta";
    if (has("eta2_p")) return "eta";
    if (has("chi2_val")) return "p";
    if (all("n1i", "m1i", "n2i", "m2i")) return "descriptives";
    return "descriptives";
  }

  function statChoice(dv, i) {
    const k = `${state.current}:${i}`;
    if (!(k in state.statChoice)) state.statChoice[k] = inferFamily(dv);
    return state.statChoice[k];
  }

  /* Optional per-DV binding to one intervention; blank = shared by all. */
  function dvInterventionBinding(rec, dv) {
    const list = recordInterventions(rec);
    if (!list.length) return null;
    const sub = el("div", "dv-sub dv-bind");
    sub.appendChild(el("h4", null, "Intervention binding"));
    const row = el("div", "field");
    row.dataset.field = "Intervention_ID";
    const lab = el("div", "field-label");
    lab.appendChild(el("span", null, "Intervention"));
    row.appendChild(lab);
    const control = el("div", "control");
    const sel = el("select", "select");
    sel.appendChild(new Option("Shared by all interventions (blank)", ""));
    list.forEach((iv) => {
      const text = [iv.Intervention_ID, iv.label, iv.Intervention_Type].filter(Boolean).join(" · ");
      sel.appendChild(new Option(text, iv.Intervention_ID));
    });
    sel.value = dv.Intervention_ID || "";
    if (dv.Intervention_ID && sel.value !== dv.Intervention_ID) {
      sel.appendChild(new Option(`${dv.Intervention_ID} (missing)`, dv.Intervention_ID));
      sel.value = dv.Intervention_ID;
    }
    sel.addEventListener("change", () => {
      if (sel.value) dv.Intervention_ID = sel.value;
      else delete dv.Intervention_ID;
      touch();
    });
    control.appendChild(sel);
    control.appendChild(el("div", "field-hint", "Blank = this contrast belongs to every intervention of the experiment."));
    row.appendChild(control);
    sub.appendChild(row);
    return sub;
  }

  function dvCard(rec, dv, i, q) {
    const dvNameMatch = !q || dvMatchText(dv).toLowerCase().includes(q);
    const open = state.openDv.has(i) || state.missingsOnly || (!!q && dvNameMatch);
    const card = el("div", "dv-card" + (open ? " open" : ""));
    card.dataset.dv = String(i);
    const head = el("div", "dv-head");
    const caret = icon("i-caret", 14);
    caret.classList.add("caret");
    head.appendChild(caret);
    head.appendChild(el("span", "dv-index", `#${i + 1}`));

    const titlecol = el("div", "dv-titlecol");
    const title = el("span", "dv-title", dv.dv_name || "Untitled DV");
    const status = dvStatus(dv);
    const reasonLine = el("div", "dv-reason");
    const paintReason = () => {
      const st = dvStatus(dv);
      reasonLine.textContent = "";
      reasonLine.className = "dv-reason";
      if (st.cls === "ok") return;
      const r = (dv.missing_metrics_reason || "").trim();
      if (r) reasonLine.textContent = "Reason: " + r;
      else {
        reasonLine.textContent = "Missing metrics — add a reason below";
        reasonLine.classList.add("blank");
      }
    };
    paintReason();
    titlecol.append(title, reasonLine);
    head.appendChild(titlecol);
    head.appendChild(el("span", "spacer"));

    const st = el("span", "pill " + status.cls);
    st.appendChild(icon(status.cls === "ok" ? "i-check" : "i-alert", 12));
    st.appendChild(el("span", null, status.text));
    head.appendChild(st);

    const elig = el("span", "pill " + (dv.eligible_primary === 1 ? "ok" : ""));
    elig.textContent = dv.eligible_primary === 1 ? "eligible" : "not primary";
    head.appendChild(elig);

    // Which arm this contrast belongs to (only meaningful when the experiment
    // actually has several interventions).
    if (recordInterventions(rec).length) {
      const bound = dv.Intervention_ID
        ? recordInterventions(rec).find((iv) => iv && iv.Intervention_ID === dv.Intervention_ID)
        : null;
      const b = el("span", "pill " + (bound ? "accent" : ""));
      b.textContent = bound ? armTitle(bound) : "shared";
      b.title = bound ? `Bound to ${bound.Intervention_ID}` : "Shared by all interventions";
      head.appendChild(b);
    }

    const actions = el("span", "dv-actions-inline");
    const dup = el("button", "btn ghost");
    dup.type = "button";
    dup.tabIndex = -1;
    dup.title = "Duplicate this contrast";
    dup.appendChild(icon("i-copy", 13));
    dup.addEventListener("click", (e) => {
      e.stopPropagation();
      rec.DV_Metrics.splice(i + 1, 0, JSON.parse(JSON.stringify(dv)));
      dvAutoFill.clear();
      touch();
      renderForm();
    });
    const mark = el("button", "btn ghost dv-mark-btn" + (dv.source_span ? " marked" : ""));
    mark.type = "button";
    mark.tabIndex = -1;
    mark.title = dv.source_span
      ? "Jump to this variable's marked source (or re-mark a new selection)"
      : "Mark the selected PDF text as this variable's source";
    mark.appendChild(icon("i-target", 13));
    mark.addEventListener("click", (e) => {
      e.stopPropagation();
      setActiveDv(i);
      state.coderTarget = { kind: "dv", index: i };
      const sel = captureSelectionSpan();
      if (sel) { markSelectionFor({ kind: "dv", index: i }); return; }
      if (dv.source_span) { pdfGotoSpan(dv.source_span); return; }
      toast("Select the supporting text in the PDF, then click Mark source", "ok");
    });
    const rm = el("button", "btn ghost danger");
    rm.type = "button";
    rm.tabIndex = -1;
    rm.title = "Remove this contrast";
    rm.appendChild(icon("i-trash", 13));
    rm.addEventListener("click", async (e) => {
      e.stopPropagation();
      const ok = await confirmDialog("Remove DV row", `Remove contrast #${i + 1}${dv.dv_name ? ` (${dv.dv_name})` : ""}?`, "Remove", true);
      if (!ok) return;
      rec.DV_Metrics.splice(i, 1);
      state.openDv = new Set();
      dvAutoFill.clear();
      touch();
      renderForm();
    });
    actions.append(dup, mark, rm);
    head.appendChild(actions);

    head.addEventListener("click", () => {
      if (card.classList.contains("open")) {
        card.classList.remove("open");
        state.openDv.delete(i);
      } else {
        card.classList.add("open");
        state.openDv.add(i);
      }
      setActiveDv(i);
      state.coderTarget = { kind: "dv", index: i };
      // selecting a DV jumps the paper to its marked source, else its effect-size number
      if (dv.source_span) pdfGotoSpan(dv.source_span);
      else coderLocateDvCard(dv);
    });
    card.appendChild(head);

    const body = el("div", "dv-body");

    /* statistics-type selector first */
    const statRow = el("div", "dv-stat");
    const statLabel = el("label", null, "Statistics to enter");
    const statSel = el("select", "select");
    Object.entries(STAT_FAMILIES).forEach(([k, v]) => statSel.appendChild(new Option(v.label, k)));
    statSel.value = statChoice(dv, i);
    statSel.addEventListener("change", () => {
      state.statChoice[`${state.current}:${i}`] = statSel.value;
      requestAnimationFrame(() => renderForm());
    });
    statRow.append(statLabel, statSel);
    body.appendChild(statRow);

    const bindSub = dvInterventionBinding(rec, dv);
    if (bindSub) body.appendChild(bindSub);

    const fieldOnChange = (f) => (v) => {
      if (f.name === "dv_name") title.textContent = v || "Untitled DV";
      if (f.name === "missing_metrics_reason") paintReason();
    };

    if (q && !dvNameMatch) {
      const fields = allDvFields().filter((f) => fieldMatches(f, q));
      if (fields.length) {
        const sub = el("div", "dv-sub");
        sub.appendChild(el("h4", null, "Matching fields"));
        fields.forEach((f) => sub.appendChild(fieldRow(f, dv, { onChange: fieldOnChange(f) })));
        body.appendChild(sub);
      }
    } else {
      const fam = statChoice(dv, i);
      const renderGroups = (groups) => groups.forEach((grp) => {
        const fields = grp.names.map((n) => state.schema.dv_fields.find((f) => f.name === n)).filter(Boolean);
        if (!fields.length) return;
        const sub = el("div", "dv-sub");
        sub.appendChild(el("h4", null, grp.title));
        fields.forEach((f) => sub.appendChild(fieldRow(f, dv, { onChange: fieldOnChange(f) })));
        body.appendChild(sub);
      });
      if (state.coderMode) {
        // Variable + contrast metadata stays; the effect entry is a single
        // pre/post matrix so only the numbers change from contrast to contrast.
        renderGroups(DV_HEAD_GROUPS);
        if (fam === "descriptives" || fam === "all") {
          body.appendChild(dvCoderGrid(rec, dv, i));
          body.appendChild(dvDerivedSummary(dv));
        } else {
          renderGroups(STAT_FAMILIES[fam].groups);
        }
      } else {
        renderGroups([...DV_HEAD_GROUPS, ...STAT_FAMILIES[fam].groups, ...DV_EXTRA_GROUPS]);
      }
    }
    card.appendChild(body);
    return card;
  }

  function dvStatus(dv) {
    const need = ["n1i", "m1i", "sd1i", "n2i", "m2i", "sd2i"];
    if (need.every((k) => isNum(dv[k]))) return { cls: "ok", text: "complete · descriptives" };
    const fb = [];
    if (isNum(dv.t_val) && isNum(dv.n1i) && isNum(dv.n2i)) fb.push("t + n");
    if (isNum(dv.F_val) && isNum(dv.df_num) && isNum(dv.df_den) && isNum(dv.n1i) && isNum(dv.n2i)) fb.push("F + df + n");
    if (isNum(dv.r_val) && isNum(dv.n_total)) fb.push("r + N");
    if (isNum(dv.yi) && (isNum(dv.vi) || isNum(dv.sei))) fb.push("yi + vi/sei");
    if ((isNum(dv.d_val) || isNum(dv.g_val)) && isNum(dv.n1i) && isNum(dv.n2i)) fb.push("d/g + n");
    if (isNum(dv.odds_ratio) && isNum(dv.or_ci_low) && isNum(dv.or_ci_high)) fb.push("OR + CI");
    if (fb.length) return { cls: "ok", text: "fallback · " + fb[0] };
    if ((dv.missing_metrics_reason || "").trim()) return { cls: "danger", text: "no metrics · reason given" };
    return { cls: "warn", text: "needs metrics or reason" };
  }

  /* ================================================================ markdown */
  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

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
    const lines = escapeHtml(String(src).replace(/\r\n?/g, "\n")).split("\n");
    const out = [];
    let i = 0;
    const isSep = (t) => /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(t) && t.includes("-");
    const cells = (t) => t.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());
    const isList = (t) => /^\s*([-*+]|\d+\.)\s+/.test(t);
    const isSpecial = (t) => /^(#{1,6})\s+/.test(t) || isList(t) || t.trimStart().startsWith(">") || t.trimStart().startsWith("|");

    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i += 1; continue; }
      let m;
      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
        const n = m[1].length;
        out.push(`<h${n}>${inlineMd(m[2])}</h${n}>`);
        i += 1;
        continue;
      }
      if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { out.push("<hr>"); i += 1; continue; }
      if (line.trimStart().startsWith(">")) {
        const buf = [];
        while (i < lines.length && lines[i].trimStart().startsWith(">")) {
          buf.push(lines[i].replace(/^\s*>\s?/, ""));
          i += 1;
        }
        out.push(`<blockquote>${inlineMd(buf.join(" "))}</blockquote>`);
        continue;
      }
      if (line.trimStart().startsWith("|")) {
        const tbl = [];
        while (i < lines.length && lines[i].trimStart().startsWith("|")) { tbl.push(lines[i]); i += 1; }
        if (tbl.length > 1 && isSep(tbl[1])) {
          const head = cells(tbl[0]);
          const rows = tbl.slice(2).map(cells).filter((r) => r.some((c) => c !== ""));
          out.push(
            "<table><thead><tr>" + head.map((h) => `<th>${inlineMd(h)}</th>`).join("") + "</tr></thead><tbody>" +
            rows.map((r) => "<tr>" + r.map((c) => `<td>${inlineMd(c)}</td>`).join("") + "</tr>").join("") +
            "</tbody></table>"
          );
        } else {
          out.push(`<p>${inlineMd(tbl.join(" "))}</p>`);
        }
        continue;
      }
      if (isList(line)) {
        const ordered = /^\s*\d+\./.test(line);
        const items = [];
        while (i < lines.length && isList(lines[i])) {
          items.push(lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, ""));
          i += 1;
        }
        out.push(`<${ordered ? "ol" : "ul"}>` + items.map((t) => `<li>${inlineMd(t)}</li>`).join("") + `</${ordered ? "ol" : "ul"}>`);
        continue;
      }
      const buf = [];
      while (i < lines.length && lines[i].trim() && !isSpecial(lines[i])) { buf.push(lines[i]); i += 1; }
      out.push(`<p>${inlineMd(buf.join(" "))}</p>`);
    }
    return out.join("\n");
  }

  /* ================================================================ rationale */
  function syncRationale() {
    const rec = state.records[state.current];
    $("#rationale-input").value = rec && rec.Rationale_Notes != null ? rec.Rationale_Notes : "";
  }

  async function loadNotes() {
    const det = $("#notes-details");
    const pre = $("#notes-pre");
    const md = $("#notes-md");
    if (!state.key) return;
    // Blind coding hides every other coding, including the original coder's notes.
    if (state.blind) { det.hidden = true; return; }
    const paint = (text) => {
      det.hidden = !text;
      pre.textContent = text || "";
      md.innerHTML = text ? renderMarkdown(text) : "";
    };
    if (state.notesCache[state.key] !== undefined) {
      paint(state.notesCache[state.key]);
      return;
    }
    det.hidden = true;
    try {
      const d = await api(`/api/notes/${encodeURIComponent(state.key)}`);
      state.notesCache[state.key] = d.notes;
      paint(d.notes);
    } catch {
      state.notesCache[state.key] = null;
    }
  }

  function applyRationaleCollapsed(on) {
    $("#rationale-block").classList.toggle("collapsed", on);
    document.body.classList.toggle("rationale-collapsed", on);
    $("#rationale-toggle").setAttribute("aria-expanded", on ? "false" : "true");
    safeStore("cs.rationaleCollapsed", on ? "1" : "0");
  }

  /* ================================================================ viewer */
  function emptyState(iconId, title, text) {
    const e = el("div", "empty");
    e.appendChild(icon(iconId, 30));
    e.appendChild(el("b", null, title));
    e.appendChild(el("p", null, text));
    return e;
  }

  /* ================================================================ viewer
     PDF pages are rendered server-side to PNGs so the viewer is fully themed:
     dark background, dark scrollbar and inverted pages in dark mode. */
  const PDF_RENDER_SCALE = 2;
  let pdfObserver = null;
  let pdfScrollHandler = null;
  let pdfWheelHandler = null;
  let pdfScrollTimer = null;

  function pdfFileUrl() {
    const key = state.key;
    let url = `/paper/${encodeURIComponent(key)}`;
    if (state.supp) url += `?supp=${encodeURIComponent(state.supp)}`;
    return url;
  }

  function pageImageUrl(n) {
    let url = `/pageimg/${encodeURIComponent(state.key)}/${n}.png?scale=${PDF_RENDER_SCALE}`;
    if (state.supp) url += `&supp=${encodeURIComponent(state.supp)}`;
    return url;
  }

  function pageTextUrl(n) {
    let url = `/api/pdftext/${encodeURIComponent(state.key)}/${n}.json`;
    if (state.supp) url += `?supp=${encodeURIComponent(state.supp)}`;
    return url;
  }

  function teardownPdf() {
    if (pdfObserver) { pdfObserver.disconnect(); pdfObserver = null; }
    const wrap = $(".pdf-scroll");
    if (wrap && pdfScrollHandler) wrap.removeEventListener("scroll", pdfScrollHandler);
    pdfScrollHandler = null;
    if (wrap && pdfWheelHandler) wrap.removeEventListener("wheel", pdfWheelHandler);
    pdfWheelHandler = null;
    clearTimeout(pdfScrollTimer);
    pdfScrollTimer = null;
  }

  /* Vertical anchor (page + fraction) at the top of the viewport, so a zoom or
     fit change keeps the reader looking at the same place in the paper. */
  function pdfAnchor(wrap) {
    const top = wrap.scrollTop;
    let best = null;
    wrap.querySelectorAll(".pdf-page[data-page]").forEach((img) => {
      if (img.offsetTop <= top && img.offsetTop + img.offsetHeight > top) best = img;
    });
    if (!best) {
      wrap.querySelectorAll(".pdf-page[data-page]").forEach((img) => {
        if (!best && img.offsetTop >= top) best = img;
      });
    }
    if (!best) return null;
    const h = best.offsetHeight || 1;
    return { page: Number(best.dataset.page),
             frac: Math.max(0, Math.min(1, (top - best.offsetTop) / h)) };
  }

  function restorePdfAnchor(wrap, a) {
    const img = a && wrap.querySelector(`.pdf-page[data-page="${a.page}"]`);
    if (img) wrap.scrollTop = Math.max(0, img.offsetTop + a.frac * img.offsetHeight);
  }

  function applyPdfFit({ anchor = false } = {}) {
    const wrap = $(".pdf-scroll");
    if (!wrap) return;
    const before = anchor ? pdfAnchor(wrap) : null;
    wrap.classList.toggle("fit", state.fit);
    wrap.classList.toggle("zoom", !state.fit);
    const s = state.pageSize;
    if (s && s.w) wrap.style.setProperty("--pdf-100-w", Math.round(s.w * 96 / 72 * (state.zoom || 1)) + "px");
    $("#fit-toggle").setAttribute("aria-pressed", state.fit ? "true" : "false");
    requestAnimationFrame(() => {
      if (before) restorePdfAnchor(wrap, before);
      if (pdfFind) pdfFind.refresh();
      positionPdfTextLayers();
    });
  }

  function scrollPdfToPage(n) {
    const wrap = $(".pdf-scroll");
    if (!wrap) return;
    const img = wrap.querySelector(`.pdf-page[data-page="${n}"]`);
    if (img) wrap.scrollTop = Math.max(0, img.offsetTop - 6);
  }

  /* ============================================================ selectable text
     The page itself is a server-rendered PNG, so a transparent text layer is
     laid over it: one fraction box per PDF text span (same coordinate space as
     the search hits).  The glyphs are invisible but selectable, so the paper's
     text can be selected and copied natively. */
  function ensurePageSpans(n) {
    if (state.pageSpans[n]) return;
    state.pageSpans[n] = "loading";
    api(pageTextUrl(n))
      .then((d) => {
        state.pageSpans[n] = (d && d.spans) || [];
        addPdfTextLayer(n);
      })
      .catch(() => { state.pageSpans[n] = []; });
  }

  function addPdfTextLayer(n) {
    const spans = state.pageSpans[n];
    if (!spans || spans === "loading" || !spans.length) return;
    const w = $(".pdf-scroll");
    if (!w) return;
    const old = w.querySelector(`:scope > .pdf-textlayer[data-page="${n}"]`);
    if (old) old.remove();
    const layer = el("div", "pdf-textlayer");
    layer.dataset.page = String(n);
    spans.forEach((s) => {
      const sp = el("span", "pdf-word", String(s[4]).replace(/\s+$/, "") + " ");
      sp.style.left = (s[0] * 100) + "%";
      sp.style.top = (s[1] * 100) + "%";
      sp.style.width = Math.max(0.2, (s[2] - s[0]) * 100) + "%";
      sp.style.height = Math.max(0.2, (s[3] - s[1]) * 100) + "%";
      sp.style.fontSize = Math.max(0.004, (s[3] - s[1])) + "em";
      layer.appendChild(sp);
    });
    w.appendChild(layer);
    positionPdfTextLayers();
  }

  /* Keep every text layer aligned with its page image.  The spans are placed
     with percentage coordinates, so only the layer's box and em base change
     when the page is resized (fit / zoom). */
  function positionPdfTextLayers() {
    const w = $(".pdf-scroll");
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

  /* ============================================================ source marking
     Select text in the PDF to record the location that supports a variable.  The
     selection is stored on the DV as ``source_span``
     ({page, rects: [[x0,y0,x1,y1], ...], text}) in the coding JSON; the coder
     view then jumps to that span instead of running a text search, and only
     falls back (with a warning) when the variable has no marked location. */
  function dvAt(i) {
    const rec = state.records[state.current];
    const dvs = (rec && rec.DV_Metrics) || [];
    return (i === null || i === undefined) ? null : dvs[Number(i)];
  }

  function setActiveDv(i) {
    state.coderDv = (i === null || i === undefined) ? null : Number(i);
    $$(".dv-card.active-src").forEach((c) => c.classList.remove("active-src"));
    const card = state.coderDv === null ? null : $(`.dv-card[data-dv="${state.coderDv}"]`);
    if (card) card.classList.add("active-src");
  }

  function ensurePdfPage(n) {
    const w = $(".pdf-scroll");
    const img = w && w.querySelector(`.pdf-page[data-page="${n}"]`);
    if (!img) return Promise.resolve(null);
    if (!img.getAttribute("src") && img.dataset.src) img.setAttribute("src", img.dataset.src);
    ensurePageSpans(Number(img.dataset.page));
    return new Promise((resolve) => {
      if (img.complete && img.naturalWidth) { resolve(img); return; }
      let done = false;
      const to = setTimeout(() => { if (!done) { done = true; resolve(img); } }, 1500);
      img.addEventListener("load", () => { if (!done) { done = true; clearTimeout(to); resolve(img); } }, { once: true });
      img.addEventListener("error", () => { if (!done) { done = true; clearTimeout(to); resolve(img); } }, { once: true });
    });
  }

  function clearPdfMarks() {
    const w = $(".pdf-scroll");
    if (w) w.querySelectorAll(":scope > .pdf-marklayer").forEach((n) => n.remove());
  }

  function paintPdfMark(span) {
    const w = $(".pdf-scroll");
    if (!w || !span || !span.page) return;
    state.lastMark = span;
    clearPdfMarks();
    const img = w.querySelector(`.pdf-page[data-page="${span.page}"]`);
    if (!img) return;
    const layer = el("div", "pdf-marklayer");
    w.appendChild(layer);
    (span.rects || []).forEach((r) => {
      if (!Array.isArray(r) || r.length < 4) return;
      const d = el("div", "pdf-mark active");
      d.style.left = (img.offsetLeft + r[0] * img.offsetWidth) + "px";
      d.style.top = (img.offsetTop + r[1] * img.offsetHeight) + "px";
      d.style.width = Math.max(3, (r[2] - r[0]) * img.offsetWidth) + "px";
      d.style.height = Math.max(3, (r[3] - r[1]) * img.offsetHeight) + "px";
      layer.appendChild(d);
    });
  }

  async function pdfGotoSpan(span) {
    if (!span || !span.page) return false;
    if (state.view !== "pdf") {
      toast("Switch to the PDF view to see the marked source", "err");
      return false;
    }
    const img = await ensurePdfPage(span.page);
    if (!img) return false;
    state.lastMark = span;
    const w = $(".pdf-scroll");
    const first = (span.rects && span.rects[0]) || [0, 0, 0, 0];
    w.scrollTop = Math.max(0, img.offsetTop + first[1] * img.offsetHeight - 90);
    state.page = span.page;
    const pi = $("#page-input");
    if (pi) pi.value = span.page;
    paintPdfMark(span);
    setTimeout(() => paintPdfMark(span), 80);
    if (pdfFind) pdfFind.refresh();
    return true;
  }

  /* The current browser selection as a page-local span (or null). */
  function captureSelectionSpan() {
    const sel = window.getSelection();
    if (!sel || !sel.rangeCount || sel.isCollapsed) return null;
    const range = sel.getRangeAt(0);
    const wrap = $(".pdf-scroll");
    if (!wrap) return null;
    let layer = null;
    wrap.querySelectorAll(":scope > .pdf-textlayer").forEach((l) => {
      if (l.contains(range.startContainer) || l.contains(range.commonAncestorContainer)) layer = l;
    });
    if (!layer) return null;
    const page = Number(layer.dataset.page);
    const rects = [];
    layer.querySelectorAll(".pdf-word").forEach((sp) => {
      let hit = false;
      try { hit = range.intersectsNode(sp); } catch { hit = false; }
      if (!hit) return;
      const l = parseFloat(sp.style.left) / 100;
      const t = parseFloat(sp.style.top) / 100;
      const wd = parseFloat(sp.style.width) / 100;
      const h = parseFloat(sp.style.height) / 100;
      if (![l, t, wd, h].every(Number.isFinite)) return;
      rects.push([l, t, l + wd, t + h]);
    });
    if (!rects.length) return null;
    const text = String(sel.toString() || "").replace(/\s+/g, " ").trim();
    if (!text) return null;
    return { page, rects, text };
  }

  function showMarkWarn(msg) {
    const w = $("#mark-warn");
    if (!w) return;
    w.textContent = msg;
    w.hidden = false;
    clearTimeout(state.markWarnTimer);
    state.markWarnTimer = setTimeout(() => {
      const e = $("#mark-warn");
      if (e) e.hidden = true;
    }, 6000);
  }

  function updateMarkButton() {
    const b = $("#mark-btn");
    if (!b) return;
    const t = state.coderTarget;
    const hasTarget = !!t && ((t.kind === "dv" && !!dvAt(t.index))
                               || (t.kind === "field" && !!t.name));
    const ready = state.view === "pdf" && !!captureSelectionSpan() && hasTarget;
    b.setAttribute("aria-pressed", ready ? "true" : "false");
    b.classList.toggle("primary", ready);
  }

  /* ``target`` is {kind:"dv", index} or {kind:"field", name}. */
  async function markSelectionFor(target) {
    const rec = state.records[state.current];
    if (!rec) return;
    const t = target || state.coderTarget;
    if (!t) {
      toast("Focus a variable first", "err",
        "Click a field or a contrast (DV), then select text in the PDF.");
      return;
    }
    const span = captureSelectionSpan();
    if (!span) {
      toast("Select text in the PDF first", "err",
        "Drag over the sentence that supports this variable, then click Mark source.");
      return;
    }
    let label = "";
    if (t.kind === "dv") {
      const dv = dvAt(t.index);
      if (!dv) { toast("Focus a variable first", "err"); return; }
      dv.source_span = { page: span.page, rects: span.rects, text: span.text };
      if (!String(dv.source_quote || "").trim()) dv.source_quote = span.text;
      if (!String(dv.source_location || "").trim()) dv.source_location = `PDF p. ${span.page}`;
      label = dv.dv_name || "Variable";
      const card = $(`.dv-card[data-dv="${t.index}"]`);
      const mb = card && card.querySelector(".dv-mark-btn");
      if (mb) mb.classList.add("marked");
    } else if (t.kind === "field" && t.name) {
      rec.source_spans = rec.source_spans || {};
      rec.source_spans[t.name] = { page: span.page, rects: span.rects, text: span.text };
      label = t.name;
    } else {
      toast("Focus a variable first", "err");
      return;
    }
    touch();
    paintPdfMark(span);
    let saved = false;
    try { saved = await save({ quiet: true }); } catch { saved = false; }
    toast("Source marked", "ok",
      `${label} · p. ${span.page}` +
      (saved ? "" : " (not saved — enter your coder name and Save)"));
    updateMarkButton();
  }

  /* Lite builds without PyMuPDF cannot render page images: fall back to the
     browser's embedded viewer. */
  function mountPdfFallback() {
    teardownPdf();
    const v = $("#viewer");
    v.innerHTML = "";
    $("#open-pdf").href = pdfFileUrl() + "#view=FitH";
    const f = document.createElement("iframe");
    f.className = "pdf-native";
    const frag = ["toolbar=0", "navpanes=0"];
    frag.push(state.fit ? "view=FitH" : "zoom=100");
    if (state.page > 1) frag.push(`page=${state.page}`);
    f.src = pdfFileUrl() + "#" + frag.join("&");
    f.title = state.key + " PDF";
    v.appendChild(f);
    $("#fit-toggle").setAttribute("aria-pressed", state.fit ? "true" : "false");
  }

  function mountPdf() {
    if (state.pdfFailed) {
      mountPdfFallback();
      return;
    }
    teardownPdf();
    initPdfFind();
    const v = $("#viewer");
    v.innerHTML = "";
    $("#open-pdf").href = pdfFileUrl() + "#view=FitH";
    const wrap = el("div", "pdf-scroll" + (state.fit ? " fit" : " zoom"));
    const s = state.pageSize;
    if (s && s.w) {
      wrap.style.setProperty("--pdf-100-w", Math.round(s.w * 96 / 72 * (state.zoom || 1)) + "px");
      wrap.style.setProperty("--pdf-ratio", `${s.w} / ${s.h}`);
    }
    v.appendChild(wrap);

    /* Ctrl+wheel zooms the rendered pages (browser page-zoom would fight the
       app layout); each step is 5% of the 100% page width, clamped 0.5-3x.
       Starting from Fit the zoom is seeded from the size currently on screen,
       so the first step continues from what the reader sees instead of jumping
       to 100%; the viewport anchor keeps the reading position. */
    pdfWheelHandler = (e) => {
      if (!e.ctrlKey) return;
      e.preventDefault();
      const step = e.deltaY < 0 ? 0.05 : -0.05;
      if (state.fit) {
        const img = wrap.querySelector(".pdf-page");
        const s = state.pageSize;
        const base = s && s.w ? s.w * 96 / 72 : 0;
        if (img && base > 0 && img.offsetWidth) state.zoom = img.offsetWidth / base;
        state.fit = false;
      }
      state.zoom = Math.min(3, Math.max(0.5, (state.zoom || 1) + step));
      applyPdfFit({ anchor: true });
    };
    wrap.addEventListener("wheel", pdfWheelHandler, { passive: false });

    if (!state.pages) {
      const loading = el("div", "empty");
      loading.appendChild(icon("i-book", 30));
      loading.appendChild(el("b", null, "Loading pages…"));
      loading.appendChild(el("p", null, "Rendering the PDF for the viewer."));
      wrap.appendChild(loading);
      return;
    }

    const io = new IntersectionObserver((entries) => {
      entries.forEach((en) => {
        if (!en.isIntersecting) return;
        const img = en.target;
        const src = img.dataset.src;
        if (src && !img.getAttribute("src")) img.setAttribute("src", src);
        ensurePageSpans(Number(img.dataset.page));
      });
    }, { root: wrap, rootMargin: "1500px 0px" });
    pdfObserver = io;

    for (let n = 1; n <= state.pages; n += 1) {
      const img = document.createElement("img");
      img.className = "pdf-page";
      img.dataset.page = String(n);
      img.dataset.src = pageImageUrl(n);
      img.alt = `Page ${n}`;
      img.decoding = "async";
      if (s && s.w) img.style.aspectRatio = `${s.w} / ${s.h}`;
      img.addEventListener("error", () => img.classList.add("page-error"));
      io.observe(img);
      wrap.appendChild(img);
    }

    /* keep the page box in sync and drop far-away bitmaps to bound memory */
    pdfScrollHandler = () => {
      const w = $(".pdf-scroll");
      if (!w) return;
      clearTimeout(pdfScrollTimer);
      pdfScrollTimer = setTimeout(() => {
        const top = w.scrollTop;
        const bottom = top + w.clientHeight;
        let current = state.page;
        w.querySelectorAll(".pdf-page").forEach((img) => {
          const o = img.offsetTop;
          const far = o > bottom + 6000 || (o + img.offsetHeight) < top - 4000;
          if (far && img.getAttribute("src")) img.removeAttribute("src");
          if (o <= top + 30) current = Number(img.dataset.page);
        });
        if (current !== state.page) {
          state.page = current;
          $("#page-input").value = current;
        }
        positionPdfTextLayers();
      }, 120);
    };
    wrap.addEventListener("scroll", pdfScrollHandler);
    requestAnimationFrame(() => {
      scrollPdfToPage(state.page);
      if (pdfFind) pdfFind.refresh();
      positionPdfTextLayers();
      if (state.lastMark) paintPdfMark(state.lastMark);
    });
  }

  function renderControls() {
    const p = state.byKey[state.key];
    const supps = (p && p.supplements) || [];
    const sel = $("#supp-select");
    sel.hidden = supps.length === 0;
    sel.innerHTML = "";
    sel.appendChild(new Option("Main PDF", ""));
    supps.forEach((s) => {
      const tag = s.kind === "pdf" ? "" : ` (${String(s.ext || "").replace(".", "")})`;
      sel.appendChild(new Option(`Supplement ${s.n || "?"}${tag}`, s.file));
    });
    sel.value = state.supp;
    const controls = $("#pdf-controls");
    controls.hidden = !(state.view === "pdf" && p && p.has_pdf);
    const pi = $("#page-input");
    pi.value = state.page;
    if (state.pages) {
      pi.max = state.pages;
      $("#page-total").textContent = state.pages;
      pi.disabled = false;
    } else {
      pi.removeAttribute("max");
      $("#page-total").textContent = "–";
      pi.disabled = !(p && p.has_pdf);
    }
    $("#fit-toggle").setAttribute("aria-pressed", state.fit ? "true" : "false");
  }

  async function loadPdfInfo() {
    state.pages = null;
    state.pageSize = null;
    state.pdfFailed = false;
    state.zoom = 1;          // fresh zoom per paper (Ctrl+wheel changes it)
    state.pageSpans = {};    // fresh selectable-text cache per paper
    initPdfFind();
    resetPdfFind();
    renderControls();
    if (!state.key) return;
    if (state.view === "pdf") renderViewer();
    try {
      const q = state.supp ? `?supp=${encodeURIComponent(state.supp)}` : "";
      const d = await api(`/api/pdfinfo/${encodeURIComponent(state.key)}${q}`);
      state.pages = d.pages;
      state.pageSize = d.size || null;
      /* no page count (e.g. Lite build without PyMuPDF) -> embedded viewer */
      state.pdfFailed = !state.pages;
    } catch {
      state.pages = null;
      state.pageSize = null;
      state.pdfFailed = true;
    }
    renderControls();
    if (state.view === "pdf") renderViewer();
  }

  function renderViewer() {
    const v = $("#viewer");
    v.innerHTML = "";
    const key = state.key;
    const p = state.byKey[key];
    if (!key) {
      v.appendChild(emptyState("i-book", "No paper selected", "Pick a study from the list on the left."));
      renderControls();
      return;
    }
    if (state.view === "pdf" && p && p.has_pdf) {
      mountPdf();
      renderControls();
      return;
    }
    if (p && p.has_text) {
      const wrap = el("div", "textwrap");
      const cached = state.textCache[key];
      const pre = el("pre", "text", cached != null ? cached : "Loading full text…");
      wrap.appendChild(pre);
      v.appendChild(wrap);
      if (cached == null) {
        api(`/api/text/${encodeURIComponent(key)}`).then((d) => {
          state.textCache[key] = d.text || "(no extracted text)";
          pre.textContent = state.textCache[key];
          if (state.coderMode) coderRelocateSoon(60);
        });
      }
      renderControls();
      return;
    }
    v.appendChild(emptyState("i-alert", "No paper file", "Neither a PDF nor extracted text was found for this study."));
    renderControls();
  }

  /* in-PDF search: highlight + cycle through matches (shared helper) */
  let pdfFind = null;

  let pdfFindToggle = null;

  function initPdfFind() {
    if (pdfFind || !window.PdfSearch) return;
    pdfFindToggle = window.PdfSearch.bindToggle("pdf-find");
    pdfFind = window.PdfSearch.create({
      getWrap: () => $("#viewer .pdf-scroll"),
      getInput: () => $("#pdf-find"),
      getCount: () => $("#pdf-find-count"),
      getPrev: () => $("#pdf-find-prev"),
      getNext: () => $("#pdf-find-next"),
      fetch: (q) => {
        const p = new URLSearchParams({ q });
        if (state.supp) p.set("supp", state.supp);
        return api(`/api/pdfsearch/${encodeURIComponent(state.key)}?${p.toString()}`);
      },
    });
  }

  function resetPdfFind() {
    const inp = $("#pdf-find");
    if (inp) inp.value = "";
    if (pdfFind) pdfFind.reset();
    if (pdfFindToggle) pdfFindToggle.reset();
  }

  /* ---- coder dropdown + compare gutter (unifies the old initials + source) ---- */
  function readStore(k, dflt) {
    try { const v = localStorage.getItem(k); return v == null ? dflt : v; } catch { return dflt; }
  }

  /* The canonical extraction is selectable as a read-only "coder" so it can be
     viewed / compared against; it must never be saved over. */
  const OG_CODER = "Original (OG)";

  function isOriginalSource() {
    const c = String(state.coder || "").trim().toLowerCase();
    return c === "og" || c === OG_CODER.toLowerCase();
  }

  function knownCoders() {
    const set = new Set();
    state.papers.forEach((p) => (p.codings || []).forEach((c) => c && c.coder && set.add(c.coder)));
    let stored = [];
    try { stored = JSON.parse(readStore("cs.coders", "[]")); } catch { stored = []; }
    const present = new Set([...set].map((c) => String(c).toLowerCase()));
    // Prune remembered names that no longer exist in any coding file, so a
    // renamed coder (e.g. ten Minimax-XX files unified to "MM") stops appearing
    // in the dropdown after a reload.
    const storedArr = Array.isArray(stored) ? stored : [];
    const fresh = storedArr.filter((c) => c && present.has(String(c).toLowerCase()));
    fresh.forEach((c) => set.add(c));
    if (fresh.length !== storedArr.length) {
      try { safeStore("cs.coders", JSON.stringify(fresh)); } catch { /* ignore */ }
    }
    if (state.coder && !isOriginalSource()) set.add(state.coder);
    return [...set].sort((a, b) => String(a).localeCompare(String(b)));
  }

  function rememberCoder(name) {
    if (!name) return;
    if (String(name).trim().toLowerCase() === OG_CODER.toLowerCase()) return;  // OG is fixed, never a coder file
    let stored = [];
    try { stored = JSON.parse(readStore("cs.coders", "[]")); } catch { stored = []; }
    if (!Array.isArray(stored)) stored = [];
    if (!stored.some((c) => String(c).toLowerCase() === String(name).toLowerCase())) stored.push(name);
    safeStore("cs.coders", JSON.stringify(stored));
  }

  function renderCoderOptions() {
    const dl = $("#coder-options");
    if (dl) {
      dl.innerHTML = [`<option value="${esc(OG_CODER)}"></option>`]
        .concat(knownCoders().map((c) => `<option value="${esc(c)}"></option>`))
        .join("");
    }
    const inp = $("#coder-input");
    if (inp) {
      const og = isOriginalSource();
      inp.classList.toggle("og-mode", og);
      inp.title = og
        ? "Original extraction (read-only reference) — enter your own initials to code and save"
        : "Your coder name — pick an existing one or type a new name to add yourself";
    }
  }

  /* Working copy for the selected coder: their saved coding, else the canonical
     extraction, else blank.  "Original (OG)" always shows the extraction.  In
     blind mode the structure is seeded but every value stays hidden. */
  async function loadWorkingCopyForCoder() {
    state.coderLoaded = state.coder;
    const p = state.byKey[state.key] || {};
    if (state.blind) {
      state.source = "blind";
      let records = null;
      if (p.has_extraction) {
        try { const d = await api(`/api/extraction/${encodeURIComponent(state.key)}`); records = d.records; }
        catch { records = null; }
      }
      state.records = (records && records.length) ? blindSeedRecords(records) : [blankRecord(state.key, 1)];
      return;
    }
    if (isOriginalSource()) {
      if (p.has_extraction) { state.source = "original"; return loadSource("original"); }
      state.source = "blank";
      return loadSource("blank");
    }
    const mine = (p.codings || []).find(
      (c) => c && c.coder && c.coder.toLowerCase() === state.coder.toLowerCase());
    if (mine) { state.source = "coding:" + mine.coder; return loadSource(state.source); }
    if (p.has_extraction) { state.source = "original"; return loadSource("original"); }
    state.source = "blank";
    return loadSource("blank");
  }

  /* Compare dropdown: another coder's coding, shown in a read-only side column. */
  function renderCompareSelect() {
    const sel = $("#compare-select");
    if (!sel) return;
    if (state.blind) {
      sel.innerHTML = "";
      sel.appendChild(new Option("— blind —", ""));
      sel.disabled = true;
      return;
    }
    sel.disabled = false;
    sel.innerHTML = "";
    sel.appendChild(new Option("— none —", ""));
    const p = state.byKey[state.key];
    if (p && p.has_extraction && !isOriginalSource()) sel.appendChild(new Option("Original (OG)", "original"));
    ((p && p.codings) || []).forEach((c) => {
      if (!c || !c.coder) return;
      if (c.coder.toLowerCase() === (state.coder || "").toLowerCase()) return; // yourself
      sel.appendChild(new Option(c.coder, "coding:" + c.coder));
    });
    sel.value = state.compareCoder
      ? (state.compareCoder === "OG" ? "original" : "coding:" + state.compareCoder)
      : "";
    if (!sel.value) sel.selectedIndex = 0;
  }

  async function loadCompare(kind) {
    state.compare = null;
    state.compareCoder = "";
    if (kind === "original") state.compareCoder = "OG";
    else if (kind && kind.startsWith("coding:")) state.compareCoder = kind.slice(7);
    if (state.compareCoder && state.key) {
      try {
        const d = state.compareCoder === "OG"
          ? await api(`/api/extraction/${encodeURIComponent(state.key)}`)
          : await api(`/api/coding/${encodeURIComponent(state.key)}/${encodeURIComponent(state.compareCoder)}`);
        state.compare = (d && d.records && d.records.length) ? d.records : null;
      } catch { state.compare = null; }
    }
    renderCompareGutter();
  }

  function compareKind() {
    return state.compareCoder
      ? (state.compareCoder === "OG" ? "original" : "coding:" + state.compareCoder)
      : "";
  }

  async function reloadCompare() { return loadCompare(compareKind()); }

  function fmtCompareVal(v) {
    if (v === null || v === undefined || v === "") return "–";
    if (typeof v === "boolean") return v ? "yes" : "no";
    if (Array.isArray(v)) return v.map((x) => String(x)).join("; ");
    if (typeof v === "object") return "{…}";
    return String(v);
  }

  function fieldLabelOf(row, name) {
    const lab = row.querySelector(":scope > label, :scope > .field-label, label");
    if (lab && lab.textContent.trim()) return lab.textContent.trim();
    const def = fieldIndex.get(name);
    return (def && def.label) || name;
  }

  /* Value normalisation for the match/mismatch colouring: numbers compare
     numerically, multi-value lists as unordered sets, strings trimmed and
     case-insensitively. */
  function normCompareVal(v) {
    if (v === null || v === undefined) return "";
    if (typeof v === "boolean") return v ? "1" : "0";
    if (Array.isArray(v)) {
      return v.map((x) => String(x).trim().toLowerCase()).filter(Boolean).sort().join("|");
    }
    const s = String(v).trim();
    if (s !== "" && !isNaN(Number(s))) return String(Number(s));
    return s.toLowerCase();
  }

  function valuesEqual(a, b) { return normCompareVal(a) === normCompareVal(b); }

  /* The compare values: the comparison coder's answer inline, just LEFT of each
     control, tinted GREEN when it matches the coder's own value and RED when it
     differs.  Nothing else about the field layout changes. */
  function renderCompareGutter() {
    $$("#form .compare-val").forEach((e) => e.remove());
    $$("#form .has-compare").forEach((e) => e.classList.remove("has-compare"));
    const form = $("#form");
    if (form) form.classList.toggle("comparing", !!state.compare);
    const theirsRec = state.compare && state.compare[state.current];
    if (!state.compare || !theirsRec) return;
    const mineRec = state.records[state.current];

    const pick = (record, elm, name) => {
      if (!record) return undefined;
      const card = elm.closest(".dv-card");
      const src = card ? (record.DV_Metrics || [])[Number(card.dataset.dv)] : record;
      return src ? src[name] : undefined;
    };
    const shown = (v) => v !== null && v !== undefined && v !== "" &&
      !(Array.isArray(v) && !v.length);

    // returns a badge for the comparison value, or null when they left it blank
    const badge = (elm, name, title) => {
      const theirs = pick(theirsRec, elm, name);
      if (!shown(theirs)) return null;
      const same = valuesEqual(pick(mineRec, elm, name), theirs);
      const s = el("span", "compare-val " + (same ? "match" : "mismatch"), fmtCompareVal(theirs));
      s.title = "Compare (" + (state.compareCoder || "") + ")" +
        (title ? ": " + title : "") + (same ? " — matches your value" : " — differs from your value");
      return s;
    };

    // schema fields: right edge of the label column = directly left of the control
    $$("#form .field[data-field]").forEach((row) => {
      const b = badge(row, row.dataset.field, fieldLabelOf(row, row.dataset.field));
      if (!b) return;
      const lab = row.querySelector(":scope > .field-label");
      (lab || row).appendChild(b);
    });
    // DV metric cells: left of the input
    $$("#form .dv-grid-input[data-field]").forEach((inp) => {
      const b = badge(inp, inp.dataset.field, inp.dataset.field);
      if (!b) return;
      if (inp.parentElement) inp.parentElement.classList.add("has-compare");
      if (inp.parentNode) inp.parentNode.insertBefore(b, inp);
    });
    // DV timepoint inputs (flex row)
    $$("#form .dv-tp[data-field]").forEach((inp) => {
      const b = badge(inp, inp.dataset.field, inp.dataset.field);
      if (b && inp.parentNode) inp.parentNode.insertBefore(b, inp);
    });
  }


  /* ================================================================ full-text exclusion */
  function updateFtCount() {
    const n = state.papers.filter((p) => p.fulltext_excluded).length;
    const c = $("#ft-count");
    if (!c) return;
    c.textContent = String(n);
    c.hidden = n === 0;
  }

  function applyShowExcluded() {
    const b = $("#show-excluded");
    if (!b) return;
    b.setAttribute("aria-pressed", state.showExcluded ? "true" : "false");
    b.classList.toggle("active", state.showExcluded);
  }

  function renderFulltext() {
    const form = $("#form");
    if (!form) return;
    let sec = $("#ft-section");
    if (!state.key) { if (sec) sec.remove(); return; }
    if (!sec) {
      // Built as a normal collapsible schema section so it scrolls with the form
      // instead of being a permanently visible bar.  Closed by default.
      sec = el("details", "section ft-section");
      sec.id = "ft-section";
      sec.open = state.ftOpen === true;
      const sum = el("summary");
      const c = icon("i-caret", 14);
      c.classList.add("caret");
      sum.appendChild(c);
      sum.appendChild(el("span", null, "Full-text exclusion"));
      const badge = el("span", "count");
      badge.id = "ft-count-badge";
      sum.appendChild(badge);
      const body = el("div", "group-body ft-body");

      const statusRow = el("div", "ft-status-row");
      const dot = el("span", "ft-dot");
      dot.id = "ft-dot";
      const status = el("span", "ft-status");
      status.id = "ft-status";
      statusRow.append(dot, status);

      const reason = el("label", "field-inline");
      reason.appendChild(el("span", null, "Reason"));
      const crit = el("select", "select");
      crit.id = "ft-criterion";
      reason.appendChild(crit);

      const note = el("input", "input");
      note.id = "ft-note";
      note.placeholder = "Exclusion note (optional)";
      note.autocomplete = "off";

      const btn = el("button", "btn");
      btn.id = "ft-exclude";
      btn.type = "button";
      btn.setAttribute("aria-pressed", "false");

      const agree = el("span", "ft-agree");
      agree.id = "ft-agree";

      body.append(statusRow, reason, note, btn, agree);
      sec.appendChild(sum);
      sec.appendChild(body);
      sec.addEventListener("toggle", () => { state.ftOpen = sec.open; });
      form.insertBefore(sec, form.firstChild);
    }
    const mine = state.fulltext.mine || null;
    const agreed = state.fulltext.agreed || null;
    const excluded = !!(mine && mine.exclude);
    sec.classList.toggle("excluded", excluded);
    sec.classList.toggle("agreed", !!agreed);
    const status = $("#ft-status");
    if (status) {
      status.textContent = excluded
        ? `Full text: you excluded — ${mine.criterion_label || mine.criterion || "excluded"}`
        : (mine && !mine.exclude ? "Full text: you marked include" : "Full text: not reviewed");
    }
    const crit = $("#ft-criterion");
    if (crit) {
      if (crit.options.length <= 1) {
        crit.innerHTML = "";
        crit.appendChild(new Option("— reason —", ""));
        (state.fulltext.criteria || []).forEach((c) => crit.appendChild(new Option(c.label, c.value)));
      }
      crit.value = (mine && mine.criterion) || "";
    }
    const note = $("#ft-note");
    if (note) note.value = (mine && mine.note) || "";
    const btn = $("#ft-exclude");
    if (btn) {
      btn.setAttribute("aria-pressed", excluded ? "true" : "false");
      btn.textContent = excluded ? "Retract exclusion" : "Exclude full text";
    }
    const ag = $("#ft-agree");
    if (ag) {
      ag.textContent = agreed
        ? `Agreed by ${agreed.n_exclude} coders: ${agreed.criterion_label || agreed.criterion}`
        : (state.fulltext.agreeMin ? `Needs ${state.fulltext.agreeMin} coders to exclude` : "");
    }
    const badge = $("#ft-count-badge");
    if (badge) badge.textContent = excluded ? "excluded" : (agreed ? "agreed exclusion" : "not reviewed");
  }

  async function loadFulltext(key) {
    state.fulltext.mine = null;
    state.fulltext.agreed = null;
    renderFulltext();
    if (!key) return;
    try {
      const q = state.coder ? `?coder=${encodeURIComponent(state.coder)}` : "";
      const d = await api(`/api/fulltext/${encodeURIComponent(key)}${q}`);
      state.fulltext.criteria = d.criteria || state.fulltext.criteria;
      state.fulltext.mine = d.mine || null;
      state.fulltext.agreed = d.agreed || null;
      state.fulltext.agreeMin = d.agree_min || 2;
    } catch { /* keep the empty bar */ }
    renderFulltext();
  }

  async function saveFulltext(exclude) {
    const key = state.key;
    if (!key) return;
    const coder = state.coder.trim();
    if (!coder) {
      toast("Enter your coder name first", "err", "The full-text decision is saved per coder.");
      $("#coder-input").focus();
      return;
    }
    const crit = $("#ft-criterion") ? $("#ft-criterion").value : "";
    const note = $("#ft-note") ? $("#ft-note").value : "";
    if (exclude && !crit) { toast("Pick an exclusion reason", "err"); $("#ft-criterion").focus(); return; }
    try {
      const d = await api(`/api/fulltext/${encodeURIComponent(key)}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ coder, exclude, criterion: crit || "other", note }),
      });
      state.fulltext.mine = d.mine || null;
      state.fulltext.agreed = d.agreed || null;
      renderFulltext();
      toast(exclude ? "Full text excluded" : "Exclusion retracted", "ok", coder);
      await refreshExcluded();
      renderPaperList();
    } catch (e) {
      toast("Could not save the full-text decision", "err", String(e.message || e));
    }
  }

  async function refreshExcluded() {
    try {
      const a = await api("/api/fulltext/excluded");
      const agreed = a.agreed || {};
      state.papers.forEach((p) => {
        const ex = agreed[p.key];
        p.fulltext_excluded = !!ex;
        p.fulltext_criterion = ex ? (ex.criterion_label || ex.criterion || "") : "";
      });
      updateFtCount();
    } catch { /* ignore */ }
  }

  /* ================================================================ coder mode */
  const CODER_MANUAL_EDITORS = new Set(["textarea", "objectlist", "json", "kv"]);
  const CODER_MANUAL_FIELDS = new Set([
    "Study_ID", "Parent_Study_ID", "DOI", "Authors", "Title", "Study_Description",
    "Rationale_Notes", "Sample_Region", "Intervention_Topic", "Stimuli_Topic",
    "Theory_Basis", "Booster_Content", "Intervention_Type_Detail", "comparison_label",
    "discernment_defined_as", "conversion_method", "effect_direction_meaning",
    "missing_metrics_reason",
    // publication / venue metadata and derived availability flags: not part of
    // the streamlined coder pass, so cut them from coder mode as well.
    "Publication", "Year", "N_Reported_Per_Condition", "N_Inference_Method",
    "International_Sample", "WEIRD_Western", "WEIRD_Educated", "WEIRD_Industrialized",
    "WEIRD_Rich", "WEIRD_Democratic", "dataset_availability_status", "data_available",
    "code_available", "preregistration_status", "ethics", "consent_reported",
  ]);
  /* Per-field search hints used by coder mode to locate a field in the paper.
     Terms are SPECIFIC stems/prefixes, never generic words: the old list led
     "N Recruited" with "participants", so it marked every "participant" instead
     of "recruit*".  Order matters: the first term that hits wins. */
  const CODER_LOCATE = {
    N_Recruited: ["recruit", "enrolled", "sampled"],
    N_Analyzed: ["analy", "final sample", "excluded"],
    Age_Mean: ["mean age", "years old", "age"],
    Age_Group: ["students", "adults", "age"],
    Gender_Ratio: ["female", "male", "gender"],
    Country: ["country", "nationwide"],
    Setting: ["online", "laboratory", "classroom", "field"],
    Intervention_Type: ["intervention", "training", "treatment"],
    Intervention_Duration: ["minutes", "duration", "session"],
    Modality: ["video", "game"],
    Stimuli: ["headlines", "claims", "stimuli"],
    Synthetic: ["synthet", "real"],
    control_group_type: ["control group", "placebo", "comparison"],
    within_between: ["between-subjects", "within-subjects"],
    unit_of_analysis: ["participants", "items"],
    Attention_Cues: ["attention", "warning"],
  };

  /* Generic words that produce excessive hits; dropped whenever a more specific
     term exists for the field. */
  const CODER_TERM_GENERIC = new Set([
    "participants", "participant", "sample", "samples", "people", "subjects",
    "items", "item", "group", "groups", "study", "studies", "data", "number",
    "count", "total", "mean", "average", "value", "values", "variable",
  ]);

  /* Significant words of a schema label, minus leading "N"/"Number of"/... */
  const CODER_LABEL_STOP = /^(n|no|num|number|of|the|a|an|is|are|was|were|do|does|did|how|many|much|which|what|mean|median|ratio|total|count|percent|percentage|proportion|sum)$/i;

  function labelStems(label) {
    const words = String(label || "")
      .replace(/\([^)]*\)/g, " ")
      .split(/[^A-Za-z0-9]+/)
      .filter(Boolean)
      .filter((w) => !CODER_LABEL_STOP.test(w));
    if (!words.length) return [];
    if (words.length === 1) return [words[0]];
    // phrase first, then its leading/trailing significant word
    return [words.join(" "), words[0], words[words.length - 1]];
  }


  function isManualField(def) {
    if (!def) return true;
    if (CODER_MANUAL_EDITORS.has(def.editor)) return true;
    if (def.editor === "text") return true;
    return CODER_MANUAL_FIELDS.has(def.name);
  }

  /* ---- coder mode: trim DV metadata and auto-deduce effect sizes ---- */
  // DV fields coder mode hides outright (the numeric effect/descriptive fields
  // stay available so the coder can still enter what the paper reports).
  const CODER_DV_HIDE = new Set([
    "score_types", "discernment_defined_as", "comparison_label", "timepoint_1",
    "timepoint_2", "posttest_type", "delay_weeks", "scale_type", "scale_points",
    "scale_min", "scale_max", "p_censored", "effect_size_type_reported",
    "effect_favors", "effect_direction_meaning", "conversion_method",
  ]);
  const CODER_DV_KEEP = new Set(["missing_metrics_reason"]);
  const DV_DERIVED = ["d_val", "d_se", "d_ci_low", "d_ci_high", "g_val", "g_se",
                      "g_ci_low", "g_ci_high", "yi", "vi", "sei"];
  const DV_REPORTED_EFFECTS = ["t_val", "F_val", "df_num", "df_den", "z_val",
    "chi2_val", "r_val", "beta_val", "beta_se", "log_odds", "odds_ratio",
    "or_ci_low", "or_ci_high", "risk_ratio", "eta2_p", "p_val"];

  function isManualDvField(def) {
    if (!def) return true;
    if (CODER_DV_KEEP.has(def.name)) return false;
    if (CODER_DV_HIDE.has(def.name)) return true;
    if (["objectlist", "json", "kv", "textarea"].includes(def.editor)) return true;
    return false;
  }

  function dvDescriptives(dv) {
    const pick = (sfx) => {
      const n1 = dv["n1i" + sfx], m1 = dv["m1i" + sfx], sd1 = dv["sd1i" + sfx];
      const n2 = dv["n2i" + sfx], m2 = dv["m2i" + sfx], sd2 = dv["sd2i" + sfx];
      if ([n1, m1, sd1, n2, m2, sd2].every(isNum) && n1 + n2 > 2 && sd1 > 0 && sd2 > 0) {
        return { n1, m1, sd1, n2, m2, sd2 };
      }
      return null;
    };
    return pick("") || pick("_post") || pick("_pre");
  }

  /* Cohen's d / Hedges' g and the matching yi/vi, using the same formulas as the
     meta-analysis backend (pooled SD, Hedges' correction J, g-based variance). */
  function dvDeriveFromDescriptives(dv) {
    const d = dvDescriptives(dv);
    if (!d) return null;
    const { n1, m1, sd1, n2, m2, sd2 } = d;
    const sp = Math.sqrt(((n1 - 1) * sd1 * sd1 + (n2 - 1) * sd2 * sd2) / (n1 + n2 - 2));
    if (!(sp > 0)) return null;
    const cohend = (m1 - m2) / sp;
    const varD = (n1 + n2) / (n1 * n2) + (cohend * cohend) / (2 * (n1 + n2));
    const seD = Math.sqrt(varD);
    const J = 1 - 3 / (4 * (n1 + n2) - 9);
    const g = cohend * J;
    // match the backend `effect()` g variance exactly:
    //   vi = (n1+n2)/(n1*n2) + g^2 / (2(n1+n2))
    const varG = (n1 + n2) / (n1 * n2) + (g * g) / (2 * (n1 + n2));
    const seG = Math.sqrt(varG);
    const r6 = (x) => Math.round(x * 1e6) / 1e6;
    return {
      d_val: r6(cohend), d_se: r6(seD),
      d_ci_low: r6(cohend - 1.96 * seD), d_ci_high: r6(cohend + 1.96 * seD),
      g_val: r6(g), g_se: r6(seG),
      g_ci_low: r6(g - 1.96 * seG), g_ci_high: r6(g + 1.96 * seG),
      yi: r6(g), vi: r6(varG), sei: r6(seG),
    };
  }

  function dvHasManualEffect(dv, autoSet) {
    return DV_REPORTED_EFFECTS.concat(["d_val", "g_val", "yi", "vi", "sei"])
      .some((k) => isNum(dv[k]) && !autoSet.has(k));
  }

  /* Fill the derived effect sizes from the group descriptives.  Only writes a
     field that is empty or was filled by us before, so a reported effect the
     coder typed by hand is never overwritten; a reported effect also clears any
     derived values we had filled. */
  function recomputeDvEffects(dvIdx) {
    if (!state.coderMode) return;
    const rec = state.records[state.current];
    const dv = rec && (rec.DV_Metrics || [])[dvIdx];
    if (!dv) return;
    const key = `${state.current}:${dvIdx}`;
    const autoSet = dvAutoFill.get(key) || new Set();
    const clearAuto = () => {
      autoSet.forEach((f) => { delete dv[f]; });
      autoSet.clear();
      dvAutoFill.set(key, autoSet);
    };
    if (dvHasManualEffect(dv, autoSet)) { clearAuto(); return; }
    const derived = dvDeriveFromDescriptives(dv);
    if (!derived) { clearAuto(); return; }
    Object.entries(derived).forEach(([f, v]) => {
      if (!isNum(dv[f]) || autoSet.has(f)) { dv[f] = v; autoSet.add(f); }
    });
    dvAutoFill.set(key, autoSet);
  }

  /* Reflect auto-computed values in the open DV card without re-rendering. */
  function syncDvCardInputs(dvIdx) {
    const rec = state.records[state.current];
    const dv = rec && (rec.DV_Metrics || [])[dvIdx];
    const card = $(`.dv-card[data-dv="${dvIdx}"]`);
    if (!dv || !card) return;
    $$("[data-field]", card).forEach((node) => {
      const f = node.dataset.field;
      if (!(f in dv)) return;
      const inp = node.matches("input, select") ? node : node.querySelector("input, select");
      if (!inp || inp.type === "checkbox") return;
      inp.value = dv[f] == null ? "" : dv[f];
    });
  }

  /* ---- coder mode: pre/post grid, group sizes and arm focus ---- */
  const DV_POST_TO_BASE = { m1i_post: "m1i", sd1i_post: "sd1i", m2i_post: "m2i", sd2i_post: "sd2i" };

  function dvN(dv, g) {
    const names = g === 1 ? ["n1i", "n1i_post", "n1i_pre"] : ["n2i", "n2i_post", "n2i_pre"];
    for (const k of names) if (isNum(dv[k])) return Number(dv[k]);
    return null;
  }

  function dvsForArm(rec, arm) {
    const all = (rec && rec.DV_Metrics) || [];
    if (!arm) {
      const shared = all.filter((dv) => !dv.Intervention_ID);
      return shared.length ? shared : all;
    }
    const bound = all.filter((dv) => dv.Intervention_ID === arm.Intervention_ID);
    return bound.length ? bound : all;
  }

  function firstControlN(rec, arm) {
    const dvs = dvsForArm(rec, arm);
    for (const dv of dvs) { const n = dvN(dv, 2); if (n != null) return n; }
    return null;
  }

  function contextGroupSizes(rec) {
    const arm = selectedIntervention(rec);
    const dvs = dvsForArm(rec, arm);
    let n1 = null, n2 = null;
    dvs.forEach((dv) => { if (n1 == null) n1 = dvN(dv, 1); if (n2 == null) n2 = dvN(dv, 2); });
    return { arm, dvs, n1, n2 };
  }

  function applyGroupSizes(rec, n1, n2) {
    const { arm, dvs } = contextGroupSizes(rec);
    const arms = recordInterventions(rec);
    const isFirstArm = !!arm && arms.indexOf(arm) === 0;
    const oldSharedN2 = isFirstArm ? firstControlN(rec, arm) : null;
    dvs.forEach((dv) => {
      if (n1 != null) { dv.n1i = n1; dv.n1i_pre = n1; dv.n1i_post = n1; }
      if (n2 != null) { dv.n2i = n2; dv.n2i_pre = n2; dv.n2i_post = n2; }
      const a = isNum(dv.n1i) ? Number(dv.n1i) : n1;
      const b = isNum(dv.n2i) ? Number(dv.n2i) : n2;
      if (a != null && b != null) dv.n_total = a + b;
    });
    // The control group is shared: the first arm's control N prefills every other
    // arm, unless that arm has its own (overridden, differing) control N.
    if (isFirstArm && n2 != null) {
      arms.slice(1).forEach((other) => {
        dvsForArm(rec, other).forEach((dv) => {
          const cur = dvN(dv, 2);
          if (cur == null || cur === oldSharedN2) {
            dv.n2i = n2; dv.n2i_pre = n2; dv.n2i_post = n2;
            const a = dvN(dv, 1);
            if (a != null) dv.n_total = a + n2;
          }
        });
      });
    }
  }

  function groupSizesRow(rec) {
    const row = el("div", "field group-sizes-field");
    row.dataset.field = "__group_sizes__";
    const lab = el("div", "field-label");
    lab.appendChild(el("span", null, "Group sizes"));
    lab.appendChild(el("span", "name", "N intervention / control"));
    row.appendChild(lab);
    const control = el("div", "control split");
    const mk = (id, title, val) => {
      const wrap = el("span", "gs-wrap");
      const inp = el("input", "input gs-input");
      inp.type = "number"; inp.min = "0"; inp.step = "1"; inp.inputMode = "numeric";
      inp.id = id; inp.title = title;
      inp.value = val == null ? "" : String(val);
      inp.addEventListener("input", onGroupSizeInput);
      wrap.appendChild(inp);
      return wrap;
    };
    const arms = recordInterventions(rec);
    const { arm, n1, n2 } = contextGroupSizes(rec);
    // an arm with no control N of its own shows the shared (first-arm) control N
    const sharedN2 = (arm && arms.length && arms.indexOf(arm) > 0) ? firstControlN(rec, arms[0]) : null;
    const n2Shown = n2 != null ? n2 : sharedN2;
    control.appendChild(mk("n1-input", "Intervention-group n — written to every contrast of this arm", n1));
    control.appendChild(mk("n2-input", "Control-group n — shared by every arm unless overridden", n2Shown));
    row.appendChild(control);
    return row;
  }

  /* Group sizes live inside the coding scheme (top of the Sample category),
     not in the top bar. */
  function renderGroupSizes(rec) {
    const form = $("#form");
    if (!form) return;
    const old = form.querySelector(".group-sizes-field");
    if (old) old.remove();
    if (!state.key || !rec) return;
    const sample = $$("#form .section").find((s) => {
      const t = s.querySelector("summary > span");
      return t && (t.textContent || "").trim() === "Sample";
    });
    const host = sample ? (sample.querySelector(".group-body") || sample) : form;
    const row = groupSizesRow(rec);
    host.insertBefore(row, host.firstChild);
  }

  /* The control group is shared across arms: when the first arm's control N is
     known, prefill every other arm that has no control N of its own. */
  function prefillControlGroups(rec) {
    if (!state.coderMode || !rec) return;
    const arms = recordInterventions(rec);
    if (arms.length < 2) return;
    const shared = firstControlN(rec, arms[0]);
    if (shared == null) return;
    arms.slice(1).forEach((arm) => {
      if (firstControlN(rec, arm) != null) return;
      dvsForArm(rec, arm).forEach((dv) => {
        dv.n2i = shared; dv.n2i_pre = shared; dv.n2i_post = shared;
        const a = dvN(dv, 1);
        if (a != null) dv.n_total = a + shared;
      });
    });
  }

  function onGroupSizeInput() {
    const rec = state.records[state.current];
    if (!rec) return;
    const raw1 = $("#n1-input") ? $("#n1-input").value.trim() : "";
    const raw2 = $("#n2-input") ? $("#n2-input").value.trim() : "";
    const n1 = raw1 === "" ? null : Number(raw1);
    const n2 = raw2 === "" ? null : Number(raw2);
    applyGroupSizes(rec, Number.isFinite(n1) ? n1 : null, Number.isFinite(n2) ? n2 : null);
    touch();
    (rec.DV_Metrics || []).forEach((_dv, idx) => syncDvCardInputs(idx));
  }

  function mirrorDvPrePost(dv, field) {
    if (!state.coderMode) return;
    if (DV_POST_TO_BASE[field]) dv[DV_POST_TO_BASE[field]] = dv[field];
    if (!dv.timepoint_1) dv.timepoint_1 = "pre";
    if (!dv.timepoint_2) dv.timepoint_2 = "post";
    const n1 = dvN(dv, 1), n2 = dvN(dv, 2);
    if (n1 != null) { dv.n1i = n1; dv.n1i_pre = n1; dv.n1i_post = n1; }
    if (n2 != null) { dv.n2i = n2; dv.n2i_pre = n2; dv.n2i_post = n2; }
    if (n1 != null && n2 != null) dv.n_total = n1 + n2;
  }

  function dvGridInput(parent, field, dv) {
    const inp = el("input", "input dv-grid-input");
    inp.type = "number";
    inp.step = "any";
    inp.inputMode = "decimal";
    inp.dataset.field = field;
    inp.value = dv[field] == null ? "" : dv[field];
    parent.appendChild(inp);
    return inp;
  }

  /* One pre/post matrix per contrast: M and SD for both groups at both times.
     Group sizes are shared, so only the four means and SDs are typed here. */
  function dvCoderGrid(rec, dv, i) {
    const sub = el("div", "dv-sub dv-prepost");
    const head = el("h4", null, "Pre / post means & SDs");
    sub.appendChild(head);

    const tpRow = el("div", "dv-tp-row");
    const tp1 = el("input", "input dv-tp");
    tp1.type = "text"; tp1.dataset.field = "timepoint_1";
    tp1.value = dv.timepoint_1 || "pre"; tp1.title = "First measurement time";
    const tp2 = el("input", "input dv-tp");
    tp2.type = "text"; tp2.dataset.field = "timepoint_2";
    tp2.value = dv.timepoint_2 || "post"; tp2.title = "Second measurement time";
    tpRow.append(el("span", "dv-tp-lab", "Times"), tp1, el("span", "dv-tp-sep", "→"), tp2);
    sub.appendChild(tpRow);

    const table = el("table", "dv-grid");
    const thead = el("thead");
    const hr = el("tr");
    hr.appendChild(el("th", null, ""));
    hr.appendChild(el("th", null, dv.group1_label || "Group 1"));
    hr.appendChild(el("th", null, dv.group2_label || "Group 2"));
    thead.appendChild(hr);
    table.appendChild(thead);
    const tbody = el("tbody");
    [["Pre — M", "m1i_pre", "m2i_pre"],
     ["Pre — SD", "sd1i_pre", "sd2i_pre"],
     ["Post — M", "m1i_post", "m2i_post"],
     ["Post — SD", "sd1i_post", "sd2i_post"]].forEach(([label, f1, f2]) => {
      const tr = el("tr");
      tr.appendChild(el("th", null, label));
      [f1, f2].forEach((f) => { const td = el("td"); dvGridInput(td, f, dv); tr.appendChild(td); });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    sub.appendChild(table);
    return sub;
  }

  function dvDerivedSummary(dv) {
    const line = el("div", "dv-derived");
    const d = dvDeriveFromDescriptives(dv);
    if (!d) {
      line.textContent = "Auto effect: enter post M/SD to derive d / g / yi.";
      line.classList.add("muted");
    } else {
      line.textContent = `Auto effect: d = ${d.d_val} · g = ${d.g_val} · yi = ${d.yi} · vi = ${d.vi}`;
    }
    return line;
  }

  function isArmFocusAllowedField(def) {
    if (!def) return false;
    if (def.module) return true;                       // intervention-module fields
    if (def.group === "Intervention") return true;     // intervention block
    return def.name === "N_Recruited" || def.name === "N_Analyzed";
  }

  function coderTerms(def) {
    const explicit = (CODER_LOCATE[def.name] || []).slice();
    // Auto-derived label words drop generic noise; curated hints are kept as-is
    // (e.g. unit_of_analysis legitimately searches "participants"/"items").
    const fromLabel = labelStems(def.label)
      .filter((s) => !CODER_TERM_GENERIC.has(String(s).toLowerCase()));
    const cleaned = [...new Set([...explicit, ...fromLabel]
      .map((s) => String(s).trim()).filter((s) => s.length >= 3))];
    if (cleaned.length) return cleaned.slice(0, 6);
    return (def.options || []).map((o) => String(o.label || "").trim())
      .filter((s) => s && s.length >= 3 && !/^(yes|no|other|not set)$/i.test(s)).slice(0, 4);
  }

  function coderHighlightText(terms) {
    const pre = $("#viewer .textwrap pre.text");
    if (!pre) return;
    const plain = pre.dataset.plain != null ? pre.dataset.plain : pre.textContent;
    pre.dataset.plain = plain;
    let html = esc(plain);
    terms.forEach((term) => {
      const safe = String(term).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      try { html = html.replace(new RegExp("(" + safe + ")", "gi"), '<mark class="coder-kw">$1</mark>'); } catch { /* ignore */ }
    });
    pre.innerHTML = html;
    const first = pre.querySelector("mark.coder-kw");
    if (first) first.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  let coderLocateToken = 0;

  /* Search the paper for the first term that matches; PDF view highlights and
     scrolls, the text view marks the matched strings. */
  async function coderSearchTerms(terms) {
    terms = [...new Set((terms || []).map((s) => String(s).trim()).filter(Boolean))];
    if (!terms.length) return;
    if (state.view === "pdf") {
      const my = ++coderLocateToken;
      if (!pdfFind) initPdfFind();
      if (pdfFindToggle) pdfFindToggle.open("");
      const inp = $("#pdf-find");
      let hit = "";
      for (const term of terms) {
        const n = pdfFind ? await pdfFind.search(term) : 0;
        if (my !== coderLocateToken) return;   // a newer locate took over
        if (n > 0) { hit = term; break; }
      }
      if (my !== coderLocateToken) return;
      if (!hit) {
        hit = terms[0];                        // no term found: show the first anyway
        if (pdfFind) await pdfFind.search(hit);
      }
      if (inp) inp.value = hit;
    } else {
      coderHighlightText(terms);
    }
  }

  async function coderLocate(def) {
    if (!state.coderMode || !def) return;
    const terms = coderTerms(def);
    if (!terms.length) return;
    return coderSearchTerms(terms);
  }

  /* Number formats a value may take in the PDF (raw, thousands-separated,
     trailing-zero trimmed / expanded). */
  function numberVariants(v) {
    if (typeof v !== "number" || !isFinite(v)) return [];
    const a = Math.abs(v);
    const out = [String(v)];
    if (Number.isInteger(v)) {
      out.push(v.toLocaleString("en-US"), String(a), a.toLocaleString("en-US"));
    } else {
      out.push(v.toFixed(2), String(a), a.toFixed(2));
    }
    return [...new Set(out)].filter((s) => s.replace(/[^0-9]/g, "").length >= 2);
  }

  /* Candidate PDF search numbers for a DV.  A focused metric searches only its
     OWN value, so an empty field never highlights an unrelated number (this is
     what made "202" appear for every field without a specific rule).  The
     generic effect-size fallback is opt-in, for the explicit "jump to this DV"
     action on the card header. */
  const DV_EFFECT_ORDER = ["n1i", "n2i", "m1i", "m2i", "d_val", "g_val", "yi",
                           "odds_ratio", "t_val", "F_val", "r_val", "n_total"];

  function dvNumberTerms(dv, name, fallback = false) {
    if (!dv) return [];
    let terms = [];
    if (name && name in dv) {
      const v = dv[name];
      let n = null;
      if (typeof v === "number") n = v;
      else if (typeof v === "string" && /^\s*-?\d/.test(v)) n = Number(v);
      if (n !== null && isFinite(n)) terms = numberVariants(n);
    }
    if (!terms.length && fallback) {
      DV_EFFECT_ORDER.forEach((k) => {
        if (terms.length < 6) terms = terms.concat(numberVariants(dv[k]));
      });
    }
    return [...new Set(terms)].slice(0, 6);
  }

  /* DV metrics: selecting/entering a numeric metric searches the paper for it. */
  function coderLocateDv(row) {
    const card = row && row.closest(".dv-card");
    if (!card) return;
    const rec = state.records[state.current];
    const dv = rec && (rec.DV_Metrics || [])[Number(card.dataset.dv)];
    if (!dv) return;
    const terms = dvNumberTerms(dv, row.dataset.field);
    if (!terms.length) return;
    return coderSearchTerms(terms);
  }

  function coderLocateDvCard(dv) {
    if (dv && dv.source_span) return pdfGotoSpan(dv.source_span);
    const terms = dvNumberTerms(dv, null, true);
    if (!terms.length) return;
    return coderSearchTerms(terms);
  }

  let dvLocateTimer = null;

  // Accepts a schema/number .field row or a DV grid input.
  function scheduleDvLocate(row) {
    if (!row) return;
    const isGrid = row.classList && row.classList.contains("dv-grid-input");
    if (!isGrid && row.dataset.editor !== "number") return;
    clearTimeout(dvLocateTimer);
    dvLocateTimer = setTimeout(() => coderLocateDv(row), 400);
  }

  function coderRelocateSoon(delay = 350) {
    if (!state.coderMode || !state.coderField) return;
    const def = fieldIndex.get(state.coderField);
    if (def) setTimeout(() => coderLocate(def), delay);
  }

  function coderVisibleFields() {
    return $$("#form .field:not(.manual), #form .dv-grid-input, #form .dv-tp")
      .filter((r) => r.dataset.field && fieldIndex.get(r.dataset.field));
  }

  function coderSetFocus(row) {
    if (!row) return;
    $$("#form .coder-focus").forEach((r) => r.classList.remove("coder-focus"));
    row.classList.add("coder-focus");
    state.coderField = row.dataset.field;
    state.coderEl = row;
    coderShowSectionFor(row);
    const ctl = row.matches("input, select") ? row
      : row.querySelector("select, input[type=checkbox], input[type=number], input[type=text]");
    if (ctl && typeof ctl.focus === "function") {
      try { ctl.focus({ preventScroll: true }); } catch { ctl.focus(); }
    }
    row.scrollIntoView({ block: "center", behavior: "smooth" });
    // A DV field/marked variable: jump to its saved source span, else fall back
    // to the value/text search and say so.
    const card = row.closest(".dv-card");
    if (card) {
      const i = Number(card.dataset.dv);
      setActiveDv(i);
      state.coderTarget = { kind: "dv", index: i };
      const rec = state.records[state.current];
      const dv = rec && (rec.DV_Metrics || [])[i];
      if (dv && dv.source_span) {
        pdfGotoSpan(dv.source_span);
        return;
      }
      showMarkWarn("No marked source location for this variable — used a text search.");
      if (row.classList.contains("dv-grid-input")) {
        const terms = dvNumberTerms(dv, row.dataset.field);
        if (terms.length) coderSearchTerms(terms);
      } else {
        coderSearchTerms(dvNumberTerms(dv, row.dataset.field) ||
          (dv ? [String(dv.dv_name || "").trim()].filter(Boolean) : []));
      }
      return;
    }
    // A record-level field (N, age, gender, control group, intervention, …).
    const fname = row.dataset.field;
    const rec = state.records[state.current];
    state.coderTarget = fname ? { kind: "field", name: fname } : null;
    const fspan = rec && rec.source_spans && fname ? rec.source_spans[fname] : null;
    if (fspan) {
      pdfGotoSpan(fspan);
      return;
    }
    showMarkWarn("No marked source location for this field — used a text search.");
    coderLocate(fieldIndex.get(state.coderField));
  }

  function coderMove(delta) {
    const rows = coderVisibleFields();
    if (!rows.length) return;
    let i = rows.indexOf(state.coderEl);
    if (i < 0) i = delta > 0 ? -1 : 0;
    i = Math.max(0, Math.min(rows.length - 1, i + delta));
    coderSetFocus(rows[i]);
  }

  function coderAdvance() {
    const rows = coderVisibleFields();
    const i = rows.indexOf(state.coderEl);
    if (i < 0) { coderMove(1); return; }
    if (rows[i]) coderCommitField(rows[i]);
    if (i + 1 < rows.length) coderSetFocus(rows[i + 1]);
  }

  function coderRefreshChoice(row) {
    if (!row) return;
    const def = fieldIndex.get(row.dataset.field);
    if (!def || !def.options) return;
    const sel = row.querySelector("select");
    const cb = row.querySelector('input[type="checkbox"]');
    const cur = sel ? String(sel.value)
      : (cb ? (cb.indeterminate ? "" : (cb.checked ? String(def.options[1].value) : String(def.options[0].value))) : "");
    $$(".num-opt", row).forEach((b, i) => {
      const o = def.options[i];
      b.classList.toggle("active", o && cur === String(o.value));
    });
  }

  function coderApplyNumber(n) {
    const name = state.coderField;
    if (!name || !fieldIndex.get(name)) return false;
    const row = $(`#form .field[data-field="${name.replace(/"/g, '\\"')}"]`);
    if (!row) return false;
    const def = fieldIndex.get(name);
    const sugg = $$(".chip-suggest button", row);
    if (sugg.length && n >= 1 && n <= Math.min(9, sugg.length)) { sugg[n - 1].click(); return true; }
    const opts = def.options || [];
    if (n >= 1 && n <= Math.min(9, opts.length)) return coderSelectRow(row, opts[n - 1].value, true);
    return false;
  }

  function coderSelectRow(row, val, advance) {
    let ok = false;
    const sel = row.querySelector("select");
    if (sel) {
      sel.value = String(val);
      sel.dispatchEvent(new Event("change", { bubbles: true }));
      ok = true;
    } else {
      const cb = row.querySelector('input[type="checkbox"]');
      if (cb) {
        cb.checked = !!val;
        cb.indeterminate = false;
        cb.dispatchEvent(new Event("change", { bubbles: true }));
        ok = true;
      }
    }
    if (ok && advance) {
      try { coderAdvance(); } catch (err) { if (window.console) console.error("coderAdvance", err); }
    }
    return ok;
  }

  function coderCommitField(row) {
    if (!row) return;
    // grid / timepoint inputs are bare <input>s: nothing to commit and nowhere
    // safe to append the "needs a number" hint.
    if (row.matches && row.matches("input")) return;
    const def = fieldIndex.get(row.dataset.field);
    const pend = row.querySelector(".coder-pending");
    if (pend) pend.remove();
    if (!def) return;
    if (def.options && def.options.length) {
      const sel = row.querySelector("select");
      if (sel && sel.value === "__none__") { coderSelectRow(row, def.options[0].value); return; }
      const cb = row.querySelector('input[type="checkbox"]');
      if (cb && cb.indeterminate) { coderSelectRow(row, false); return; }
    } else if (def.editor === "number") {
      const inp = row.querySelector("input[type='number']");
      if (inp && String(inp.value || "").trim() === "") {
        const host = row.querySelector(".control") || row;
        host.appendChild(el("span", "coder-pending", "needs a number"));
      }
    }
  }

  function coderActivateSection(sec) {
    if (!sec) return;
    $$("#form .section").forEach((s) => s.classList.toggle("coder-open", s === sec));
    // a closed <details> would keep its group-body hidden even while coder-open
    if (sec.open === false) sec.open = true;
    const first = $$(".field:not(.manual)", sec)
      .find((r) => r.dataset.field && fieldIndex.get(r.dataset.field));
    if (first) coderSetFocus(first);
  }

  function coderShowSectionFor(row) {
    const sec = row && row.closest(".section");
    if (!sec) return;
    $$("#form .section").forEach((s) => s.classList.toggle("coder-open", s === sec));
    if (sec.open === false) sec.open = true;
    // expand a DV card too, if the focused field lives inside one
    const card = row.closest(".dv-card");
    if (card) card.classList.add("coder-open");
  }

  function coderChoiceFor(row, def) {
    const host = row.querySelector(".control") || row;
    const box = el("div", "coder-choice");
    const sel = row.querySelector("select");
    const cb = row.querySelector('input[type="checkbox"]');
    def.options.slice(0, 9).forEach((o, i) => {
      const b = el("button", "num-opt");
      b.type = "button";
      b.appendChild(el("span", "k", String(i + 1)));
      b.appendChild(el("span", null, CS.optionText(o.value, o.label)));
      const cur = sel ? String(sel.value) : (cb ? (cb.checked ? String(o.value) : "") : "");
      if (cur === String(o.value)) b.classList.add("active");
      b.addEventListener("click", (e) => {
        e.preventDefault();
        coderSelectRow(row, o.value, true);
        coderRefreshChoice(row);
      });
      box.appendChild(b);
    });
    host.appendChild(box);
    if (def.options.length > 9) {
      host.appendChild(el("div", "coder-more", `${def.options.length - 9} more levels — use the dropdown`));
    }
  }

  function decorateCoderMode() {
    const on = state.coderMode;
    document.body.classList.toggle("coder-mode", on);
    renderGroupSizes(state.records[state.current]);
    const btn = $("#coder-mode");
    if (btn) btn.setAttribute("aria-pressed", on ? "true" : "false");
    const rows = $$("#form .field");
    rows.forEach((r) => r.classList.remove("manual", "coder-focus"));
    $$("#form .coder-hint, #form .coder-opt-hint, #form .coder-choice, #form .coder-more, #form .coder-pending")
      .forEach((n) => n.remove());

    // When a specific intervention arm is coded, keep only the arm-relevant
    // categories: the sample Ns, the intervention block, its module and the DVs.
    const rec = state.records[state.current];
    const armFocus = on && !!(rec && selectedIntervention(rec));
    document.body.classList.toggle("arm-focus", armFocus);
    $$("#form .section").forEach((sec) => {
      let keep = false;
      if (armFocus) {
        const titleEl = sec.querySelector("summary > span");
        const title = (titleEl && titleEl.textContent || "").trim();
        keep = sec.classList.contains("dv-section")
          || title === "Sample"
          || title === "Intervention"
          || title.startsWith("Intervention module");
      }
      sec.classList.toggle("arm-keep", keep);
    });

    // Mark the coder-hidden rows (and build the option buttons) BEFORE choosing
    // the active category, so a category whose fields are all coder-hidden can be
    // skipped instead of opening a blank pane.
    if (on) {
      rows.forEach((r) => {
        const def = fieldIndex.get(r.dataset.field);
        if (!def) { r.classList.remove("manual"); return; }  // non-schema rows (e.g. group sizes)
        const inDv = !!r.closest(".dv-card");
        const armGate = armFocus && !inDv && !isArmFocusAllowedField(def);
        const manual = armGate || (inDv ? isManualDvField(def) : isManualField(def));
        r.classList.toggle("manual", manual);
        // a coder-choice button row replaces the native dropdown for short option lists
        if (def && !manual && def.options && def.options.length >= 2 && def.options.length <= 9
            && !/^(text|textarea)$/i.test(def.editor || "")) {
          coderChoiceFor(r, def);
        }
        // multiselect/chips: label the suggestion tokens with their number key
        $$(".chip-suggest button", r).forEach((b, i) => {
          if (i < 9 && b.dataset.k == null) { b.dataset.k = "1"; b.textContent = `${i + 1} ${b.textContent}`; }
        });
      });
    }

    // Coder-visible fields of one category ("anchor" fields exclude the full-text
    // criterion, which lives in the otherwise-empty Metadata category and would
    // otherwise make that category look like it has something to code).
    const visibleRows = (sec) => sec
      ? $$(".field:not(.manual), .dv-grid-input, .dv-tp", sec)
        .filter((r) => r.dataset.field && fieldIndex.get(r.dataset.field))
      : [];
    const anchorRows = (sec) => visibleRows(sec).filter((r) => r.dataset.field !== "Fulltext_Exclusion_Criterion");
    const openable = (sec) => !!sec && (!armFocus || sec.classList.contains("arm-keep"))
      && (!on || visibleRows(sec).length > 0);
    const anchorable = (sec) => !!sec && (!armFocus || sec.classList.contains("arm-keep"))
      && (!on || anchorRows(sec).length > 0);

    // Keep the currently open category if it still shows a field; otherwise open
    // the first category that has a codable (anchor) field — so entering coder
    // mode never opens a category whose fields are all hidden.
    let active = $("#form .section.coder-open:not(.ft-section)");
    if (!openable(active)) {
      active = $$("#form .section:not(.ft-section)").find(anchorable)
        || $("#form .section.arm-keep") || $("#form .section:not(.ft-section)");
    }
    $$("#form .section").forEach((s) => s.classList.toggle("coder-open", s === active));
    // an <details> has to be open for its group-body to render at all
    if (active && active.open === false) active.open = true;
    if (!on) return;

    const form = $("#form");
    if (form) {
      const h = el("div", "coder-hint",
        "Coder mode · 1–9 choose an option · Tab / Shift+Tab move between variables · ↑/↓ move · the paper follows the field.");
      form.insertBefore(h, form.firstChild);
    }
    // Focus a field inside the active category (never the first field anywhere) so
    // focusing cannot re-open a different, empty category.
    const first = anchorRows(active)[0] || visibleRows(active)[0] || coderVisibleFields()[0];
    if (first) coderSetFocus(first);
  }

  function toggleCoderMode(force) {
    state.coderMode = force === undefined ? !state.coderMode : !!force;
    safeStore("cs.coderMode", state.coderMode ? "1" : "0");
    // re-render so the DV cards switch between the full family layout and the
    // coder pre/post grid, and the arm-focus / group-size controls appear.
    if (state.records.length) renderForm();
    // always sync the body classes / hidden fields, including when turning off
    decorateCoderMode();
    // the screening tab keeps its own form: tell it so it can re-decorate
    window.dispatchEvent(new CustomEvent("cs:codermode", { detail: state.coderMode }));
  }

  /* ================================================================ paper selection */
  async function selectPaper(key, opts = {}) {
    if (state.dirty && !opts.force) {
      const ok = await confirmDialog("Discard unsaved changes?", `The working copy for ${state.key} has unsaved edits. Open ${key} anyway?`, "Discard & open", true);
      if (!ok) return;
    }
    state.key = key;
    state.current = 0;
    state.selIntervention = -1;
    state.openDv = new Set();
    state.statChoice = {};
    state.coderField = null;
    state.coderEl = null;
    state.coderDone = new Set();
    dvAutoFill.clear();
    state.dirty = false;
    state.supp = "";
    state.page = 1;
    state.fit = true;
    state.pages = null;
    state.pageSize = null;
    state.pdfFailed = false;
    await loadWorkingCopyForCoder();
    state.records.forEach(ensureModuleFields);
    syncMissingOpen();
    renderPaperList();
    renderTabs();
    renderForm();
    syncRationale();
    renderCompareSelect();
    await reloadCompare();
    renderViewer();
    loadNotes();
    loadPdfInfo();
    loadFulltext(key);
    coderRelocateSoon(500);
    updateDirty();
  }

  /* ================================================================ dirty */
  function touch() {
    state.dirty = true;
    updateDirty();
    const item = document.querySelector(".paper-item.active .dot");
    if (item) item.classList.add("dirty");
  }

  function updateDirty() {
    const btn = $("#save-btn");
    if (btn) {
      btn.classList.toggle("save-pending", state.dirty);
      btn.classList.toggle("save-saved", !state.dirty);
    }
    document.title = `${state.dirty ? "• " : ""}${state.key || "Coding Studio"}`;
  }

  /* ================================================================ actions */
  async function save({ quiet = false } = {}) {
    if (document.body.classList.contains("view-summary")) return false;  // Summary handles its own save
    if (document.body.classList.contains("view-screening")) return false; // Screening handles its own save
    if (document.body.classList.contains("view-agreement")) return false; // Agreement handles its own save
    if (document.body.classList.contains("view-analysis")) return false; // Analysis owns its own outputs
    if (document.body.classList.contains("view-reproduction")) return false; // Reproduction owns its own outputs
    if (state.busy) return false;
    const coder = state.coder.trim();
    if (!coder) {
      toast("Enter your coder name before saving", "err");
      $("#coder-input").focus();
      return false;
    }
    if (isOriginalSource()) {
      toast("Original is read-only", "err",
        "Pick your own coder initials to save a coding — the original extraction is never overwritten.");
      $("#coder-input").focus();
      return false;
    }
    if (!state.key) {
      toast("Select a paper first", "err");
      return false;
    }
    if (state.blind && !payloadHasContent()) {
      if (!quiet) toast("Nothing entered yet", "ok", "Blind coding is only saved once you enter variables.");
      return true;
    }
    if ($(".json-invalid", $("#form"))) {
      toast("Fix invalid JSON before saving", "err", "A conversion_inputs / object field could not be parsed.");
      return false;
    }
    const missing = [];
    state.records.forEach((rec, i) => {
      ["Study_ID", "Year", "Authors", "Title", "Study_Description"].forEach((k) => {
        if (rec[k] === null || rec[k] === undefined || rec[k] === "") missing.push(`${k} (exp ${i + 1})`);
      });
    });
    if (missing.length && !quiet) toast("Saving with required fields still empty", "err", missing.slice(0, 4).join(", "));

    state.busy = true;
    try {
      const recordsOut = state.blind ? stripEmptyForBlind(state.records) : state.records;
      const res = await fetch("/api/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: state.key, coder, records: recordsOut, blind: state.blind }),
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || "save failed");
      state.dirty = false;
      updateDirty();
      const meta = state.byKey[state.key];
      if (meta && !meta.codings.some((c) => c.coder.toLowerCase() === coder.toLowerCase())) {
        meta.codings.push({ coder, file: "", modified: new Date().toISOString() });
        meta.n_coders = meta.codings.length;
      }
      renderPaperList();
      renderCompareSelect();
      renderCoderOptions();
      if (!quiet) toast(`Saved ${data.records} record${data.records === 1 ? "" : "s"}`, "ok", data.path);
      return true;
    } catch (err) {
      toast("Save failed", "err", String(err.message || err));
      return false;
    } finally {
      state.busy = false;
    }
  }

  async function goNext() {
    if (state.dirty && !isOriginalSource()) {
      const ok = await save({ quiet: true });
      if (!ok) return;
    }
    const list = visiblePapers();
    if (!list.length) { toast("No papers in the current view", "err", "Clear the search."); return; }
    if (state.shuffleOn) {
      const pool = list.filter((p) => p.key !== state.key && !codedBy(p, state.coder));
      if (pool.length) {
        const pick = pickUncoded(pool);
        await selectPaper(pick.key, { force: true });
        toast("Random uncoded study", "ok", coderCountSub(pick));
        return;
      }
      toast("Nothing left to code", "ok", `Every paper is coded by ${state.coder || "you"}.`);
      return;
    }
    const idx = list.findIndex((p) => p.key === state.key);
    for (let step = 1; step <= list.length; step += 1) {
      const cand = list[(idx + step) % list.length];
      if (cand.key === state.key) continue;
      if (!codedBy(cand, state.coder)) {
        await selectPaper(cand.key, { force: true });
        return;
      }
    }
    toast("Every paper is coded by " + (state.coder || "you"), "ok", "Nice work.");
  }

  async function clearCoding() {
    const ok = await confirmDialog(
      "Clear manual coding?",
      "This resets the working copy for this paper to blank fields. The original extraction and any saved coder files are not touched.",
      "Clear fields", true
    );
    if (!ok) return;
    if (state.blind) {
      // blind coding keeps the original's structure, so "clear" re-seeds it
      await loadWorkingCopyForCoder();
    } else {
      state.records = [blankRecord(state.key, 1)];
      state.source = "blank";
    }
    state.current = 0;
    state.selIntervention = -1;
    state.openDv = new Set();
    state.statChoice = {};
    touch();
    renderTabs();
    renderForm();
    syncRationale();
    renderCompareSelect();
    toast("Fields cleared", "ok", "Start fresh; Save writes a new coder file.");
  }

  async function toggleBlind() {
    if (!state.blind && state.dirty) {
      const ok = await confirmDialog("Start blind coding?", "Unsaved edits will be discarded and the structure will be shown with all values hidden.", "Start blind", true);
      if (!ok) return;
    }
    state.blind = !state.blind;
    applyBlind();
    // Blind coding hides every other coding, so the compare view is turned off.
    if (state.blind) { state.compare = null; state.compareCoder = ""; }
    if (state.key) {
      await loadWorkingCopyForCoder();
      state.records.forEach(ensureModuleFields);
      state.statChoice = {};
      state.selIntervention = -1;
      state.dirty = false;
      syncMissingOpen();
      renderTabs();
      renderForm();
      syncRationale();
      renderCompareSelect();
      loadNotes();
      updateDirty();
    }
    if (state.blind) toast("Blind coding on", "ok", "Values hidden — the original's experiments, arms and DV names are kept for compatibility.");
  }

  function applyBlind() {
    const btn = $("#blind-btn");
    btn.setAttribute("aria-pressed", state.blind ? "true" : "false");
    btn.classList.toggle("blind-on", state.blind);
    $("#blind-label").textContent = "Blind";
    $("#blind-icon").setAttribute("href", state.blind ? "#i-eye-off" : "#i-eye");
    const cmp = $("#compare-select");
    if (cmp) cmp.disabled = !!state.blind;
  }

  /* ---------------------------------------------------------- panes */
  function setLeftCollapsed(on) {
    document.body.classList.toggle("left-collapsed", on);
    $("#left-pane").classList.toggle("collapsed", on);
    safeStore("cs.leftCollapsed", on ? "1" : "0");
  }

  function setRightCollapsed(on) {
    document.body.classList.toggle("right-collapsed", on);
    $("#paper-pane").classList.toggle("collapsed", on);
    safeStore("cs.rightCollapsed", on ? "1" : "0");
  }

  function expandLeft() {
    setLeftCollapsed(false);
    document.documentElement.style.setProperty("--w-left", "306px");
  }

  function expandRight() {
    setRightCollapsed(false);
    document.documentElement.style.setProperty("--w-mid", "46%");
  }

  function syncWideLayout() {
    const left = localStorage.getItem("cs.leftCollapsed") === "1";
    const right = localStorage.getItem("cs.rightCollapsed") === "1";
    document.body.classList.toggle("left-collapsed", left);
    document.body.classList.toggle("right-collapsed", right);
    $("#left-pane").classList.toggle("collapsed", left);
    $("#paper-pane").classList.toggle("collapsed", right);
  }

  /* ---------------------------------------------------------- project folder */
  async function openProjectDialog() {
    let info = { root: "", can_pick: false };
    try {
      info = await api("/api/root");
    } catch { /* ignore */ }
    $("#project-input").value = info.root || "";
    $("#project-status").textContent = info.papers != null ? `${info.papers} papers found` : "";
    const desktop = !!(window.pywebview && window.pywebview.api && window.pywebview.api.choose_folder);
    $("#project-browse").hidden = !(info.can_pick || desktop);
    $("#project-dialog").showModal();
  }

  async function pickProjectFolder() {
    const status = $("#project-status");
    status.textContent = "Waiting for the folder chooser…";
    /* desktop window: the native dialog runs inside pywebview on its GUI thread */
    const desktopApi = window.pywebview && window.pywebview.api;
    if (desktopApi && typeof desktopApi.choose_folder === "function") {
      let path = "";
      try {
        path = await desktopApi.choose_folder();
      } catch {
        path = "";
      }
      if (path) {
        $("#project-input").value = path;
        status.textContent = "Folder selected — press “Use this folder”.";
        return;
      }
      status.textContent = "";
    }
    try {
      const r = await fetch("/api/pick-root", { method: "POST" }).then((x) => x.json());
      if (r.ok && r.path) {
        $("#project-input").value = r.path;
        status.textContent = "Folder selected — press “Use this folder”.";
      } else if (r.error !== "cancelled") {
        status.textContent = r.error || "Folder chooser unavailable — type the path instead.";
      } else {
        status.textContent = "";
      }
    } catch {
      status.textContent = "Folder chooser unavailable — type the path instead.";
    }
  }

  async function saveProjectRoot() {
    const path = $("#project-input").value.trim();
    if (!path) return;
    $("#project-status").textContent = "Switching project…";
    try {
      const r = await fetch("/api/root", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ root: path }),
      }).then((x) => x.json());
      if (!r.ok) {
        $("#project-status").textContent = r.error || "Could not use that folder.";
        return;
      }
      $("#project-dialog").close();
      const fresh = await api("/api/papers");
      state.papers = fresh.papers;
      state.byKey = {};
      state.papers.forEach((p) => { state.byKey[p.key] = p; });
      state.key = null;
      state.records = [];
      state.current = 0;
      state.selIntervention = -1;
      state.openDv = new Set();
      state.textCache = {};
      state.notesCache = {};
      state.dirty = false;
      renderPaperList();
      renderCoderOptions();
      renderTabs();
      $("#form").innerHTML = "";
      const ivPanel = $("#interventions-panel");
      if (ivPanel) ivPanel.innerHTML = "";
      $("#form").appendChild(emptyState("i-search", "Choose a paper to begin", `${state.papers.length} papers in ${r.root}`));
      renderViewer();
      updateDirty();
      toast("Project folder set", "ok", `${r.papers} papers · ${r.root}`);
    } catch (err) {
      $("#project-status").textContent = String(err.message || err);
    }
  }

  /* ================================================================ boot */
  async function boot() {
    const savedTheme = localStorage.getItem("cs.theme") || "dark";
    document.documentElement.dataset.theme = savedTheme;

    state.coder = localStorage.getItem("cs.coder") || "";
    $("#coder-input").value = state.coder;
    // Optional deep link: ?compare=<coder> preselects the compare column.
    try {
      const cmp = new URLSearchParams(location.search).get("compare");
      if (cmp) state.compareCoder = cmp === "OG" ? "OG" : cmp;
    } catch { /* ignore */ }
    state.missingsOnly = localStorage.getItem("cs.missingsOnly") === "1";
    $("#missings-only").setAttribute("aria-pressed", state.missingsOnly ? "true" : "false");
    state.showExcluded = localStorage.getItem("cs.showExcluded") === "1";
    applyShowExcluded();
    state.coderMode = localStorage.getItem("cs.coderMode") === "1";
    $("#coder-mode").setAttribute("aria-pressed", state.coderMode ? "true" : "false");
    applyRationaleCollapsed(localStorage.getItem("cs.rationaleCollapsed") === "1");

    const [schema, papers] = await Promise.all([api("/api/schema"), api("/api/papers")]);
    state.schema = schema;
    state.papers = papers.papers;
    state.byKey = {};
    state.papers.forEach((p) => { state.byKey[p.key] = p; });
    renderCoderOptions();
    updateFtCount();
    try { state.fulltext.criteria = (await api("/api/fulltext/criteria")).criteria || []; } catch { /* ignore */ }

    schema.common_fields.forEach((f) => fieldIndex.set(f.name, f));
    Object.values(schema.modules).forEach((m) => m.fields.forEach((f) => { if (!fieldIndex.has(f.name)) fieldIndex.set(f.name, f); }));

    renderPaperList();
    renderTabs();
    $("#form").innerHTML = "";
    const ivPanelBoot = $("#interventions-panel");
    if (ivPanelBoot) ivPanelBoot.innerHTML = "";
    $("#form").appendChild(emptyState("i-search", "Choose a paper to begin", "Search or pick a study from the left. Existing codings load for correction; Blind mode hides them."));

    wireEvents();
    applyBlind();
    updateFacetBadge();
    syncWideLayout();
    updateDirty();
  }

  function wireEvents() {
    $("#search-input").addEventListener("input", (e) => {
      state.search = e.target.value;
      renderPaperList();
    });
    $("#field-search").addEventListener("input", (e) => {
      state.fieldSearch = e.target.value;
      if (state.records.length) renderForm();
    });
    $("#field-search").addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        e.target.value = "";
        state.fieldSearch = "";
        if (state.records.length) renderForm();
      }
    });
    $("#filters").addEventListener("click", (e) => {
      const chip = e.target.closest(".chip");
      if (!chip) return;
      state.filter = chip.dataset.filter;
      $$("#filters .chip").forEach((c) => c.classList.toggle("active", c === chip));
      renderPaperList();
    });
    $("#show-excluded")?.addEventListener("click", () => {
      state.showExcluded = !state.showExcluded;
      safeStore("cs.showExcluded", state.showExcluded ? "1" : "0");
      applyShowExcluded();
      renderPaperList();
    });
    const screeningView = () => document.body.classList.contains("view-screening");
    $("#shuffle-btn")?.addEventListener("click", () => {
      const btn = $("#shuffle-btn");
      const on = btn.getAttribute("aria-pressed") !== "true";
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      btn.classList.toggle("primary", on);
      if (screeningView()) {
        window.dispatchEvent(new CustomEvent("cs:screening-shuffle", { detail: on }));
      } else {
        state.shuffleOn = on;
        if (on) shuffleUncoded();
      }
    });
    $("#merge-btn")?.addEventListener("click", () => {
      if (screeningView()) window.dispatchEvent(new CustomEvent("cs:screening-merge"));
    });
    $("#coder-input").addEventListener("input", (e) => {
      state.coder = e.target.value;
      safeStore("cs.coder", state.coder);
      window.dispatchEvent(new CustomEvent("cs:coder", { detail: state.coder }));
      if (!screeningView()) renderPaperList();
    });
    // Committing a coder (pick from the dropdown or type a new name) sets the
    // initials AND loads that coder's working copy — the unified replacement for
    // the old separate initials + source controls.
    $("#coder-input").addEventListener("change", async (e) => {
      const name = (e.target.value || "").trim();
      if (name === state.coderLoaded) { renderCoderOptions(); return; }
      if (state.dirty) {
        const ok = await confirmDialog("Discard unsaved changes?",
          "Switching coder replaces the working copy with that coder's coding.",
          "Discard & switch", true);
        if (!ok) { e.target.value = state.coder; return; }
      }
      state.coder = name;
      safeStore("cs.coder", name);
      if (name) rememberCoder(name);
      renderCoderOptions();
      window.dispatchEvent(new CustomEvent("cs:coder", { detail: name }));
      if (!state.key || window.CS.currentView() !== "coding") { renderCompareSelect(); return; }
      await loadWorkingCopyForCoder();
      state.records.forEach(ensureModuleFields);
      state.current = 0;
      state.selIntervention = -1;
      state.openDv = new Set();
      state.statChoice = {};
      dvAutoFill.clear();
      state.dirty = false;
      syncMissingOpen();
      renderTabs();
      renderForm();
      syncRationale();
      renderCompareSelect();
      updateDirty();
    });
    $("#save-btn").addEventListener("click", () => {
      if (screeningView()) window.dispatchEvent(new CustomEvent("cs:screening-save"));
      else save();
    });
    $("#next-btn").addEventListener("click", () => goNext());
    $("#clear-btn").addEventListener("click", () => clearCoding());
    $("#blind-btn").addEventListener("click", () => {
      if (screeningView()) window.dispatchEvent(new CustomEvent("cs:screening-blind"));
      else toggleBlind();
    });
    $("#missings-only").addEventListener("click", toggleMissings);
    $("#coder-mode")?.addEventListener("click", () => toggleCoderMode());
    $("#ft-exclude")?.addEventListener("click", () =>
      saveFulltext($("#ft-exclude").getAttribute("aria-pressed") !== "true"));
    $("#ft-criterion")?.addEventListener("change", () => {
      const mine = state.fulltext.mine;
      if (mine && mine.exclude) saveFulltext(true);
    });
    const formEl = $("#form");
    // Keep the compare badges' match/mismatch colouring in step with edits.
    let compareRefreshTimer = null;
    const scheduleCompareRefresh = () => {
      if (!state.compare) return;
      clearTimeout(compareRefreshTimer);
      compareRefreshTimer = setTimeout(renderCompareGutter, 250);
    };
    formEl.addEventListener("input", scheduleCompareRefresh);
    formEl.addEventListener("change", scheduleCompareRefresh);
    formEl.addEventListener("focusin", (e) => {
      const gridInput = e.target.matches && e.target.matches(".dv-grid-input, .dv-tp") ? e.target : null;
      const row = e.target.closest(".field");
      // DV metrics: focusing a value searches the paper for that number.
      const locateTarget = gridInput || (row && row.dataset.field ? row : null);
      if (locateTarget) scheduleDvLocate(locateTarget);
      if (!state.coderMode) return;
      const fieldEl = gridInput || row;
      if (!fieldEl || !fieldEl.dataset.field) return;
      if (row && row.classList.contains("manual")) return;
      if (fieldEl.dataset.field !== state.coderField || state.coderEl !== fieldEl) {
        $$("#form .coder-focus").forEach((r) => r.classList.remove("coder-focus"));
        fieldEl.classList.add("coder-focus");
        state.coderField = fieldEl.dataset.field;
        state.coderEl = fieldEl;
        coderShowSectionFor(fieldEl);
        if (!gridInput) coderLocate(fieldIndex.get(state.coderField));
      }
    });
    formEl.addEventListener("change", (e) => {
      // Full-text exclusion criterion (the section is rebuilt on each render,
      // so its controls are handled by delegation).
      if (e.target.id === "ft-criterion") {
        const mine = state.fulltext.mine;
        if (mine && mine.exclude) saveFulltext(true);
        return;
      }
      // DV metrics: committing a value searches the paper for that number and,
      // in coder mode, mirrors post -> base and re-derives the effect sizes.
      const dvCard = e.target.closest(".dv-card");
      const dvFieldRow = e.target.closest(".dv-card .field");
      const dvGridInput = e.target.classList && e.target.classList.contains("dv-grid-input") ? e.target : null;
      const dvField = (e.target.dataset && e.target.dataset.field)
        || (dvFieldRow && dvFieldRow.dataset.field) || "";
      if (dvFieldRow || dvGridInput) scheduleDvLocate(dvFieldRow || dvGridInput);
      if (dvCard && dvField) {
        const idx = Number(dvCard.dataset.dv);
        if (Number.isInteger(idx) && idx >= 0 && state.coderMode) {
          const rec = state.records[state.current];
          const dv = rec && (rec.DV_Metrics || [])[idx];
          if (dv) {
            // a manual edit of a derived field opts it out of auto-fill
            const autoSet = dvAutoFill.get(`${state.current}:${idx}`);
            if (autoSet && DV_DERIVED.includes(dvField)) autoSet.delete(dvField);
            mirrorDvPrePost(dv, dvField);
            recomputeDvEffects(idx);
            syncDvCardInputs(idx);
            const line = dvCard.querySelector(".dv-derived");
            if (line) {
              const d = dvDeriveFromDescriptives(dv);
              line.classList.toggle("muted", !d);
              line.textContent = d
                ? `Auto effect: d = ${d.d_val} · g = ${d.g_val} · yi = ${d.yi} · vi = ${d.vi}`
                : "Auto effect: enter post M/SD to derive d / g / yi.";
            }
          }
        }
      }
      if (!state.coderMode) return;
      const row = e.target.closest(".field");
      coderRefreshChoice(row);
      // only a user-driven change (isTrusted) auto-advances; programmatic ones do not
      if (e.isTrusted && (e.target.tagName === "SELECT" || e.target.type === "checkbox")) {
        setTimeout(coderAdvance, 40);
      }
    });
    formEl.addEventListener("click", (e) => {
      if (e.target.closest("#ft-exclude")) {
        saveFulltext($("#ft-exclude").getAttribute("aria-pressed") !== "true");
      }
    });
    document.addEventListener("keydown", (e) => {
      if (!state.coderMode || e.ctrlKey || e.metaKey || e.altKey) return;
      const view = document.querySelector(".viewtab.active");
      if (view && view.dataset.view !== "coding") return;
      const tag = (e.target.tagName || "").toLowerCase();
      if (tag === "textarea") return;
      // Tab / Shift+Tab step to the next / previous variable (like the paper screen);
      // number keys pick a level, arrows move, Shift+Tab goes back.
      if (e.key === "Tab") { e.preventDefault(); if (e.shiftKey) coderMove(-1); else coderAdvance(); return; }
      if (e.key === "ArrowDown" || e.key === "ArrowRight") { e.preventDefault(); coderMove(1); return; }
      if (e.key === "ArrowUp" || e.key === "ArrowLeft") { e.preventDefault(); coderMove(-1); return; }
      if (/^[1-9]$/.test(e.key)) {
        if (tag === "input" && ["text", "number"].includes(e.target.type)) return;
        if (coderApplyNumber(Number(e.key))) e.preventDefault();
      }
    });
    const fsToggle = $("#field-search-toggle");
    if (fsToggle) {
      fsToggle.addEventListener("click", () => {
        const field = $("#field-search-field");
        const opening = field.hidden;
        field.hidden = !opening;
        fsToggle.setAttribute("aria-expanded", opening ? "true" : "false");
        if (opening) $("#field-search").focus();
        else { state.fieldSearch = ""; $("#field-search").value = ""; if (state.records.length) renderForm(); }
      });
    }
    $("#rail-left").addEventListener("click", expandLeft);
    $("#rail-right").addEventListener("click", expandRight);
    $("#min-left").addEventListener("click", () => setLeftCollapsed(true));
    $("#min-right").addEventListener("click", () => setRightCollapsed(true));
    $("#rationale-toggle").addEventListener("click", () => {
      const on = !$("#rationale-block").classList.contains("collapsed");
      applyRationaleCollapsed(on);
    });
    $("#theme-btn").addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      localStorage.setItem("cs.theme", next);
      $("#theme-icon").setAttribute("href", next === "dark" ? "#i-sun" : "#i-moon");
    });
    $("#theme-icon").setAttribute("href", document.documentElement.dataset.theme === "dark" ? "#i-sun" : "#i-moon");

    $("#rationale-input").addEventListener("input", (e) => {
      const rec = state.records[state.current];
      if (rec) {
        rec.Rationale_Notes = e.target.value;
        touch();
      }
    });

    $("#notes-raw-toggle").addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const pre = $("#notes-pre");
      const md = $("#notes-md");
      const raw = pre.hidden;
      pre.hidden = !raw;
      md.hidden = raw;
      e.currentTarget.textContent = raw ? "rendered" : "raw";
    });

    $("#compare-select").addEventListener("change", async (e) => {
      await loadCompare(e.target.value);
    });

    $("#viewer-seg").addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      state.view = b.dataset.view;
      $$("#viewer-seg button").forEach((x) => x.classList.toggle("active", x === b));
      renderViewer();
      coderRelocateSoon(600);
    });
    $("#supp-select").addEventListener("change", (e) => {
      const val = e.target.value;
      const p = state.byKey[state.key] || {};
      const sp = (p.supplements || []).find((s) => s.file === val);
      if (sp && sp.kind !== "pdf") {
        window.open(`/supplement/${encodeURIComponent(state.key)}/` +
                    `${encodeURIComponent(sp.file)}`, "_blank");
        e.target.value = state.supp;
        return;
      }
      state.supp = val;
      state.page = 1;
      renderViewer();
      loadPdfInfo();
    });
    $("#page-input").addEventListener("change", (e) => {
      let n = parseInt(e.target.value, 10);
      if (isNaN(n) || n < 1) n = 1;
      if (state.pages && n > state.pages) n = state.pages;
      state.page = n;
      if (state.pdfFailed) renderViewer();
      else scrollPdfToPage(n);
    });
    $("#fit-toggle").addEventListener("click", () => {
      state.fit = !state.fit;
      if (state.pdfFailed) renderViewer();
      else applyPdfFit({ anchor: true });
    });
    $("#mark-btn").addEventListener("click", () => {
      if (state.view !== "pdf") {
        toast("Switch to the PDF view to mark a source", "err");
        return;
      }
      markSelectionFor(state.coderTarget);
    });
    document.addEventListener("selectionchange", () => {
      if (state.view === "pdf") updateMarkButton();
    });
    $("#viewer").addEventListener("mouseup", () => {
      if (state.view === "pdf") setTimeout(updateMarkButton, 0);
    });
    $("#form").addEventListener("focusin", (e) => {
      const card = e.target && e.target.closest ? e.target.closest(".dv-card") : null;
      if (card) {
        const i = Number(card.dataset.dv);
        setActiveDv(i);
        state.coderTarget = { kind: "dv", index: i };
      } else if (e.target && e.target.dataset && e.target.dataset.field) {
        state.coderTarget = { kind: "field", name: e.target.dataset.field };
      }
    });

    $("#facet-btn").addEventListener("click", openFacetDialog);
    $("#facet-apply").addEventListener("click", applyFacets);
    $("#facet-cancel").addEventListener("click", () => $("#facet-dialog").close());
    $("#facet-clear").addEventListener("click", () => {
      $$("#facet-body input[type=checkbox]").forEach((c) => { c.checked = false; });
      applyFacets();
    });

    /* project folder */
    $("#project-btn").addEventListener("click", openProjectDialog);
    $("#project-cancel").addEventListener("click", () => $("#project-dialog").close());
    $("#project-browse").addEventListener("click", pickProjectFolder);
    $("#project-save").addEventListener("click", saveProjectRoot);

    /* gutters: drag below the snap threshold minimises the pane to an edge rail */
    document.querySelectorAll(".gutter").forEach((g) => {
      g.addEventListener("pointerdown", (e) => {
        e.preventDefault();
        g.setPointerCapture(e.pointerId);
        g.classList.add("dragging");
        const root = document.documentElement;
        const which = g.dataset.resize;
        const move = (ev) => {
          const rect = $(".workspace").getBoundingClientRect();
          if (which === "left") {
            const w = ev.clientX - rect.left - 10;
            if (w < SNAP) {
              setLeftCollapsed(true);
            } else {
              setLeftCollapsed(false);
              root.style.setProperty("--w-left", Math.round(Math.min(560, Math.max(200, w))) + "px");
            }
          } else {
            const leftW = $("#left-pane").getBoundingClientRect().width;
            const rightW = rect.right - ev.clientX;
            if (rightW < SNAP) {
              setRightCollapsed(true);
            } else {
              setRightCollapsed(false);
              const avail = rect.width - leftW - 24;
              const px = Math.round(Math.max(240, Math.min(avail - 240, ev.clientX - rect.left - leftW - 12)));
              root.style.setProperty("--w-mid", px + "px");
            }
          }
        };
        const up = () => {
          g.classList.remove("dragging");
          window.removeEventListener("pointermove", move);
          window.removeEventListener("pointerup", up);
        };
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", up);
      });
    });

    /* rationale resize */
    const rg = $("#rationale-gutter");
    rg.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      rg.setPointerCapture(e.pointerId);
      rg.classList.add("dragging");
      const pane = $(".pane.left");
      const move = (ev) => {
        const rect = pane.getBoundingClientRect();
        const h = Math.max(150, Math.min(rect.height - 180, rect.bottom - ev.clientY));
        document.documentElement.style.setProperty("--rationale-h", h + "px");
      };
      const up = () => {
        rg.classList.remove("dragging");
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    });

    /* shortcuts */
    window.addEventListener("keydown", (e) => {
      // Only the coding view owns these global shortcuts.  Every other tab has
      // its own key handling, so it must not trigger coding actions (Ctrl+S
      // saving a coding file, Insert/Ctrl+Enter advancing the paper, Ctrl+B
      // toggling blind, Ctrl+K focusing a hidden search box).
      if (window.CS.currentView() !== "coding") return;
      if (e.key === "Insert") {
        e.preventDefault();
        goNext();
        return;
      }
      const meta = e.ctrlKey || e.metaKey;
      if (!meta) return;
      if (e.key === "s") { e.preventDefault(); save(); }
      else if (e.key === "Enter") { e.preventDefault(); goNext(); }
      else if (e.key.toLowerCase() === "b") { e.preventDefault(); toggleBlind(); }
      else if (e.key.toLowerCase() === "k") { e.preventDefault(); $("#search-input").focus(); $("#search-input").select(); }
    });

    window.addEventListener("resize", syncWideLayout);

    window.addEventListener("beforeunload", () => {
      // Auto-save any open edits when the app is closed.
      if (!state.dirty) return;
      if (document.body.classList.contains("view-screening")) return;
      if (document.body.classList.contains("view-reproduction")) return;
      const coder = state.coder.trim();
      if (!coder || !state.key) return;
      if (state.blind && !payloadHasContent()) return;   // nothing entered in blind mode
      try {
        const recordsOut = state.blind ? stripEmptyForBlind(state.records) : state.records;
        const blob = new Blob([JSON.stringify({ key: state.key, coder, records: recordsOut })],
                              { type: "application/json" });
        navigator.sendBeacon("/api/save", blob);
        state.dirty = false;
      } catch { /* ignore */ }
    });
    // Open a paper from another tab (Summary/Coverage) in the coding view.
    window.addEventListener("cs:open-paper", (e) => {
      const k = e.detail && e.detail.key;
      if (k) selectPaper(k);
    });
    // Keep the shared top-bar actions in sync with the active view.
    document.addEventListener("cs:view", (e) => {
      const lbl = $("#clear-label");
      if (lbl) lbl.textContent = "Clear";
      if (e.detail !== "coding") return;
      const btn = $("#shuffle-btn");
      if (btn) {
        btn.setAttribute("aria-pressed", state.shuffleOn ? "true" : "false");
        btn.classList.toggle("primary", state.shuffleOn);
      }
    });

  }

  boot().catch((err) => {
    console.error(err);
    $("#form").innerHTML = "";
    const e = el("div", "empty");
    e.appendChild(icon("i-alert", 30));
    e.appendChild(el("b", null, "Could not start"));
    e.appendChild(el("p", null, String(err.message || err)));
    $("#form").appendChild(e);
  });
})();
