/* ============================================================
   Coding Studio — scheme editor

   A two-pane modal that lets the user author the project's
   coding scheme without touching a file: blocks (groups), the
   fields inside them, conditional display, user-defined variable
   types ("kinds"), the repeatable metrics block and the agent
   prompt.

   The scheme is read from and written to /api/scheme; the roles
   come from /api/scheme/roles.  Nothing else on the page is
   touched.  Exposes window.CSScheme = { open, close, isOpen }.
   ============================================================ */
(() => {
  "use strict";

  const { $, $$, api, toast } = window.CS;
  const icon = window.CS.svgEl;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  /* ---------------------------------------------------------- vocabulary */
  const FIELD_TYPES = ["string", "integer", "number", "boolean", "list", "object", "date"];
  const EDITORS = ["text", "textarea", "number", "boolean", "select", "multiselect",
    "date", "json", "objectlist", "kv", "chips", "module-select"];
  const SCOPES = ["study", "metric", "module"];
  const OPS = ["in", "not_in", "equals", "not_equals", "not_empty", "empty",
    "gt", "gte", "lt", "lte", "contains"];
  const OP_LABEL = {
    in: "is one of", not_in: "is none of",
    equals: "equals", not_equals: "does not equal",
    not_empty: "is not empty", empty: "is empty",
    gt: ">", gte: "≥", lt: "<", lte: "≤",
    contains: "contains",
  };
  const VALUELESS_OPS = ["not_empty", "empty"];
  const NUMERIC_OPS = ["gt", "gte", "lt", "lte"];
  const LIST_OPS = ["in", "not_in"];

  const FAMILY_ORDER = ["descriptives", "t", "F", "r", "yi", "d_g", "odds_ratio",
    "beta", "eta2p", "chi2"];
  const FAMILY_LABEL = {
    descriptives: "Descriptives (n, M, SD)",
    t: "t statistic",
    F: "F statistic",
    r: "Correlation r",
    yi: "Effect size yi",
    d_g: "Cohen's d / Hedges' g",
    odds_ratio: "Odds ratio / risk ratio",
    beta: "Regression beta",
    eta2p: "Partial eta squared",
    chi2: "Chi-square / p-value",
  };

  /* Standard metric fields per effect-size family (ported from QCnext's
     FAMILY_FIELDS): name, label, type, editor and role. */
  const FAMILY_FIELDS = {
    descriptives: [
      { name: "n1i", label: "N (group 1)", type: "integer", editor: "number", role: "n1" },
      { name: "m1i", label: "Mean (group 1)", type: "number", editor: "number", role: "m1" },
      { name: "sd1i", label: "SD (group 1)", type: "number", editor: "number", role: "sd1" },
      { name: "n2i", label: "N (group 2)", type: "integer", editor: "number", role: "n2" },
      { name: "m2i", label: "Mean (group 2)", type: "number", editor: "number", role: "m2" },
      { name: "sd2i", label: "SD (group 2)", type: "number", editor: "number", role: "sd2" },
    ],
    t: [
      { name: "t_val", label: "t statistic", type: "number", editor: "number", role: "t" },
      { name: "df_den", label: "df (denominator)", type: "number", editor: "number", role: "df_den" },
    ],
    F: [
      { name: "F_val", label: "F statistic", type: "number", editor: "number", role: "F" },
      { name: "df_num", label: "df (numerator)", type: "number", editor: "number", role: "df_num" },
      { name: "df_den", label: "df (denominator)", type: "number", editor: "number", role: "df_den" },
    ],
    r: [
      { name: "r_val", label: "Correlation r", type: "number", editor: "number", role: "r" },
      { name: "n_total", label: "Total N", type: "integer", editor: "number", role: "n_total" },
    ],
    yi: [
      { name: "yi", label: "Effect size yi", type: "number", editor: "number", role: "yi" },
      { name: "vi", label: "Variance vi", type: "number", editor: "number", role: "vi" },
      { name: "sei", label: "Standard error sei", type: "number", editor: "number", role: "sei" },
    ],
    d_g: [
      { name: "d_val", label: "Cohen's d", type: "number", editor: "number", role: "d" },
      { name: "g_val", label: "Hedges' g", type: "number", editor: "number", role: "g" },
      { name: "d_ci_low", label: "d CI low", type: "number", editor: "number", role: "d_ci_low" },
      { name: "d_ci_high", label: "d CI high", type: "number", editor: "number", role: "d_ci_high" },
      { name: "g_ci_low", label: "g CI low", type: "number", editor: "number", role: "g_ci_low" },
      { name: "g_ci_high", label: "g CI high", type: "number", editor: "number", role: "g_ci_high" },
      { name: "d_se", label: "d standard error", type: "number", editor: "number", role: "d_se" },
      { name: "g_se", label: "g standard error", type: "number", editor: "number", role: "g_se" },
    ],
    odds_ratio: [
      { name: "or_val", label: "Odds ratio", type: "number", editor: "number", role: "or" },
      { name: "or_ci_low", label: "OR CI low", type: "number", editor: "number", role: "or_ci_low" },
      { name: "or_ci_high", label: "OR CI high", type: "number", editor: "number", role: "or_ci_high" },
      { name: "log_odds", label: "Log odds", type: "number", editor: "number", role: "log_odds" },
      { name: "rr", label: "Risk ratio", type: "number", editor: "number", role: "rr" },
    ],
    beta: [
      { name: "beta_val", label: "Regression beta", type: "number", editor: "number", role: "beta" },
      { name: "beta_se", label: "Beta standard error", type: "number", editor: "number", role: "beta_se" },
    ],
    eta2p: [
      { name: "eta2p", label: "Partial eta squared", type: "number", editor: "number", role: "eta2p" },
      { name: "eta2p_ci_low", label: "Partial eta² CI low", type: "number", editor: "number", role: "eta2p_ci_low" },
      { name: "eta2p_ci_high", label: "Partial eta² CI high", type: "number", editor: "number", role: "eta2p_ci_high" },
    ],
    chi2: [
      { name: "chi2_val", label: "Chi-square", type: "number", editor: "number", role: "chi2" },
      { name: "p_val", label: "p-value", type: "number", editor: "number", role: "p" },
      { name: "p_censored", label: "p censored (e.g. < .001)", type: "boolean", editor: "boolean", role: "p_censored" },
    ],
  };

  /* ---------------------------------------------------------- state */
  const state = {
    draft: null,
    rev: 0,             // bumped on every edit
    baselineRev: 0,     // the rev of the last loaded/saved scheme
    roles: { study: [], metric: [], all: [] },
    selected: null,      // {scope, name}
    pane: "field",       // "field" | "prompt"
    problems: [],
    loaded: false,
  };

  /* fields whose name was typed by hand (or that carry a legacy name the
     label does not slugify) keep their name when the label changes */
  const nameLocked = new WeakSet();

  /* ---------------------------------------------------------- helpers */
  function slugify(text) {
    const slug = String(text == null ? "" : text).toLowerCase()
      .replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
    if (!slug) return "";
    return /^[0-9]/.test(slug) ? "f_" + slug : slug;
  }

  function optList(raw) {
    if (!Array.isArray(raw)) return [];
    const out = [];
    for (const o of raw) {
      if (o && typeof o === "object") {
        const value = o.value === null || o.value === undefined ? "" : String(o.value);
        const label = o.label === null || o.label === undefined ? value : String(o.label);
        if (!value && !label) continue;
        out.push({ value, label });
      } else if (o === null || o === undefined || o === "") {
        continue;
      } else {
        out.push({ value: String(o), label: String(o) });
      }
    }
    return out;
  }

  function cleanField(raw, scope) {
    const f = raw && typeof raw === "object" ? raw : {};
    const name = String(f.name || "");
    const label = String(f.label || name);
    const type = FIELD_TYPES.indexOf(f.type) >= 0 ? f.type : "string";
    return {
      name,
      label,
      group: String(f.group || ""),
      scope: f.scope || scope,
      type,
      editor: EDITORS.indexOf(f.editor) >= 0 ? f.editor : "text",
      options: optList(f.options),
      vocab: Array.isArray(f.vocab) ? f.vocab.map(String) : [],
      columns: Array.isArray(f.columns)
        ? f.columns.map((c) => ({ name: String(c.name || ""), type: String(c.type || "string") }))
        : [],
      role: f.role || null,
      required: !!f.required,
      derived: !!f.derived,
      moderator: f.moderator === undefined ? true : !!f.moderator,
      description: String(f.description || ""),
      instruction: String(f.instruction || ""),
      show_if: f.show_if || null,
      required_if: f.required_if || null,
      module: f.module || null,
      module_options: f.module_options && typeof f.module_options === "object" ? f.module_options : {},
      kind: f.kind || null,
      source: f.source && typeof f.source === "object" ? f.source : { locate: [] },
      coder: f.coder && typeof f.coder === "object" ? f.coder : { auto: true, hidden: false },
    };
  }

  function normaliseDraft(raw) {
    const d = JSON.parse(JSON.stringify(raw && typeof raw === "object" ? raw : {}));
    d.groups = Array.isArray(d.groups)
      ? d.groups.map((g) => {
        if (typeof g === "string") return { name: g, label: g, description: "", show_if: null };
        const name = String(g.name || "");
        if (!name) return null;
        return {
          name,
          label: String(g.label || name),
          description: String(g.description || ""),
          show_if: g.show_if || null,
        };
      }).filter(Boolean)
      : [];
    d.fields = Array.isArray(d.fields) ? d.fields.map((f) => cleanField(f, "study")) : [];
    d.metrics = d.metrics && typeof d.metrics === "object" ? d.metrics : {};
    d.metrics.enabled = !!d.metrics.enabled;
    d.metrics.group = String(d.metrics.group || "DV_Metrics");
    d.metrics.label = String(d.metrics.label || "Effect sizes");
    d.metrics.fields = Array.isArray(d.metrics.fields)
      ? d.metrics.fields.map((f) => cleanField(f, "metric"))
      : [];
    d.metrics.layout = d.metrics.layout && typeof d.metrics.layout === "object" ? d.metrics.layout : {};
    d.modules = d.modules && typeof d.modules === "object" ? d.modules : {};
    for (const key of Object.keys(d.modules)) {
      const m = d.modules[key] && typeof d.modules[key] === "object" ? d.modules[key] : {};
      m.key = key;
      m.label = String(m.label || key);
      m.fields = Array.isArray(m.fields)
        ? m.fields.map((f) => {
          const x = cleanField(f, "module");
          x.module = x.module || key;
          return x;
        })
        : [];
      d.modules[key] = m;
    }
    d.prompts = d.prompts && typeof d.prompts === "object" ? d.prompts : {};
    d.kinds = Array.isArray(d.kinds)
      ? d.kinds.map((k) => ({
        id: String(k.id || ""),
        label: String(k.label || k.id || ""),
        base_type: FIELD_TYPES.indexOf(k.base_type) >= 0 ? k.base_type : "string",
        editor: EDITORS.indexOf(k.editor) >= 0 ? k.editor : "text",
        description: String(k.description || ""),
        options: optList(k.options),
        columns: Array.isArray(k.columns)
          ? k.columns.map((c) => ({ name: String(c.name || ""), type: String(c.type || "string") }))
          : [],
        role: k.role || null,
      }))
      : [];
    d.effect_families = Array.isArray(d.effect_families) ? d.effect_families.map(String) : [];
    d.vocabularies = d.vocabularies && typeof d.vocabularies === "object" ? d.vocabularies : {};
    d.rules = d.rules && typeof d.rules === "object" ? d.rules : {};
    d.intervention_type_modules = d.intervention_type_modules && typeof d.intervention_type_modules === "object"
      ? d.intervention_type_modules : {};
    d.id = String(d.id || "scheme");
    d.name = String(d.name || "");
    d.label = String(d.label || "");
    d.description = String(d.description || "");
    d.scheme_version = d.scheme_version || 3;
    /* a name that is not the slug of its label was set by hand — keep it */
    for (const f of allFieldsOf(d)) {
      if (f.name && f.name !== slugify(f.label)) nameLocked.add(f);
    }
    return d;
  }

  function allFieldsOf(d) {
    const out = (d.fields || []).slice();
    out.push(...((d.metrics && d.metrics.fields) || []));
    for (const key of Object.keys(d.modules || {})) {
      out.push(...((d.modules[key].fields) || []));
    }
    return out;
  }

  function buildPayload() {
    const d = state.draft || {};
    const outField = (f) => {
      const o = cleanField(f, f.scope || "study");
      o.group = f.group || "";
      return o;
    };
    return {
      scheme_version: d.scheme_version || 3,
      id: d.id || "scheme",
      name: d.name || "",
      label: d.label || "",
      description: d.description || "",
      prompts: d.prompts || {},
      groups: (d.groups || []).map((g) => ({
        name: g.name,
        label: g.label || g.name,
        description: g.description || "",
        show_if: g.show_if || null,
      })),
      fields: (d.fields || []).map(outField),
      metrics: {
        enabled: !!(d.metrics && d.metrics.enabled),
        group: (d.metrics && d.metrics.group) || "DV_Metrics",
        label: (d.metrics && d.metrics.label) || "Effect sizes",
        fields: ((d.metrics && d.metrics.fields) || []).map(outField),
        layout: (d.metrics && d.metrics.layout) || {},
      },
      modules: d.modules || {},
      intervention_type_modules: d.intervention_type_modules || {},
      vocabularies: d.vocabularies || {},
      effect_families: d.effect_families || [],
      kinds: (d.kinds || []).map((k) => ({
        id: k.id,
        label: k.label,
        base_type: k.base_type,
        editor: k.editor,
        description: k.description || "",
        options: k.options || [],
        columns: k.columns || [],
        role: k.role || null,
      })),
      rules: d.rules || {},
    };
  }

  function isDirty() {
    if (!state.draft) return false;
    return state.rev !== state.baselineRev;
  }

  function allFields() {
    return allFieldsOf(state.draft || {});
  }

  function findField(scope, name) {
    if (!state.draft) return null;
    if (scope === "study") return state.draft.fields.find((f) => f.name === name) || null;
    if (scope === "metric") {
      return (state.draft.metrics.fields || []).find((f) => f.name === name) || null;
    }
    for (const key of Object.keys(state.draft.modules)) {
      const f = (state.draft.modules[key].fields || []).find((x) => x.name === name);
      if (f) return f;
    }
    return null;
  }

  function listOf(scope, field) {
    if (scope === "study") return state.draft.fields;
    if (scope === "metric") {
      if (!state.draft.metrics.fields) state.draft.metrics.fields = [];
      return state.draft.metrics.fields;
    }
    const key = field && field.module;
    if (!key || !state.draft.modules[key]) return null;
    return state.draft.modules[key].fields;
  }

  function bucketName(scope, field) {
    if (scope === "study") return field.group;
    if (scope === "metric") return state.draft.metrics.group;
    return field.module || "";
  }

  function buckets() {
    const out = [];
    for (const g of state.draft.groups) {
      out.push({
        id: "group:" + g.name,
        name: g.name,
        label: g.label || g.name,
        scope: "study",
        group: g,
        fields: state.draft.fields.filter((f) => f.group === g.name),
      });
    }
    const known = new Set(state.draft.groups.map((g) => g.name));
    const orphans = state.draft.fields.filter((f) => !known.has(f.group));
    if (orphans.length) {
      out.push({
        id: "group:__unassigned",
        name: "__unassigned",
        label: "Unassigned fields",
        scope: "study",
        group: null,
        fields: orphans,
        orphan: true,
      });
    }
    for (const key of Object.keys(state.draft.modules)) {
      const m = state.draft.modules[key];
      out.push({
        id: "module:" + key,
        name: key,
        label: m.label || key,
        scope: "module",
        group: null,
        module: key,
        fields: (m.fields || []).slice(),
      });
    }
    return out;
  }

  /* ---------------------------------------------------------- conditions */
  function condText(cond) {
    if (!cond || !cond.field) return "";
    const op = OP_LABEL[cond.op] || cond.op;
    const value = cond.value;
    if (VALUELESS_OPS.indexOf(cond.op) >= 0) return `${cond.field} ${op}`;
    const list = Array.isArray(value) ? value : (value === null || value === undefined ? [] : [value]);
    if (!list.length) return `${cond.field} ${op} …`;
    return `${cond.field} ${op} ${list.join(", ")}`;
  }

  /* Which control the condition's value gets.  Pure: it only looks at the
     referenced field and the operator, so it can be reasoned about (and
     tested) on its own.
       "none"          - the operator takes no value
       "options"       - a select of the referenced field's own options
       "options-multi" - the same, but several values can be picked
       "number"        - a numeric input
       "text"          - a free-text input */
  function valueControlKind(refField, op) {
    if (VALUELESS_OPS.indexOf(op) >= 0) return "none";
    const opts = refField ? optList(refField.options) : [];
    const multi = !!refField && (refField.editor === "multiselect" || refField.type === "list");
    if (opts.length && (refField.editor === "select" || refField.editor === "multiselect" || multi)) {
      return multi ? "options-multi" : "options";
    }
    const numeric = NUMERIC_OPS.indexOf(op) >= 0
      || (refField && (refField.type === "integer" || refField.type === "number"));
    return numeric ? "number" : "text";
  }

  function condCandidates(excludeName, excludeBlock) {
    const out = [];
    for (const f of state.draft.fields) {
      if (f.name === excludeName) continue;
      if (excludeBlock && f.group === excludeBlock) continue;
      out.push({ name: f.name, label: f.label || f.name, group: "Study fields", field: f });
    }
    for (const key of Object.keys(state.draft.modules)) {
      for (const f of (state.draft.modules[key].fields || [])) {
        if (f.name === excludeName) continue;
        if (excludeBlock && f.module === excludeBlock) continue;
        out.push({
          name: f.name,
          label: `${f.label || f.name} (module)`,
          group: "Module fields",
          field: f,
        });
      }
    }
    return out;
  }

  let pop = null;         // the open condition popover
  let popAnchor = null;

  function closePop() {
    if (pop) {
      pop.remove();
      pop = null;
      popAnchor = null;
      document.removeEventListener("pointerdown", onDocDown, true);
      document.removeEventListener("keydown", onDocKey, true);
    }
  }

  function onDocDown(e) {
    if (!pop) return;
    if (pop.contains(e.target)) return;
    if (popAnchor && popAnchor.contains(e.target)) return;
    closePop();
  }

  function onDocKey(e) {
    if (pop && e.key === "Escape") {
      e.stopPropagation();
      e.preventDefault();
      closePop();
    }
  }

  function placePop(node, anchor) {
    const r = anchor.getBoundingClientRect();
    const w = node.offsetWidth || 260;
    const h = node.offsetHeight || 160;
    let left = r.left;
    let top = r.bottom + 6;
    if (left + w > window.innerWidth - 10) left = Math.max(10, window.innerWidth - w - 10);
    if (top + h > window.innerHeight - 10) top = Math.max(10, r.top - h - 6);
    node.style.left = `${left}px`;
    node.style.top = `${top}px`;
  }

  /* Open the condition builder under `anchor`.  `current` is the condition
     currently set (or null); `apply(cond|null)` receives every change. */
  function openConditionBuilder(anchor, current, apply, opts) {
    closePop();
    const o = opts || {};
    const work = current
      ? { field: current.field, op: current.op, value: current.value }
      : { field: "", op: "equals", value: "" };

    const box = el("div", "scheme-pop");
    box.appendChild(el("div", "scheme-pop-title", o.title || "Condition"));

    const fieldSel = el("select", "select");
    fieldSel.setAttribute("aria-label", "Field the condition depends on");
    const blank = el("option", "", "— pick a field —");
    blank.value = "";
    fieldSel.appendChild(blank);
    const cands = condCandidates(o.excludeName, o.excludeBlock);
    let lastGroup = null;
    for (const c of cands) {
      let parent = fieldSel;
      if (c.group !== lastGroup) {
        lastGroup = c.group;
        const og = el("optgroup", "", c.group);
        og.label = c.group;
        fieldSel.appendChild(og);
        parent = og;
      }
      const opt = el("option", "", c.label);
      opt.value = c.name;
      parent.appendChild(opt);
    }
    fieldSel.value = work.field || "";
    box.appendChild(fieldSel);

    const opSel = el("select", "select");
    opSel.setAttribute("aria-label", "Operator");
    for (const op of OPS) {
      const opt = el("option", "", OP_LABEL[op] || op);
      opt.value = op;
      opSel.appendChild(opt);
    }
    opSel.value = work.op;
    box.appendChild(opSel);

    const valueHost = el("div", "scheme-pop-value");
    box.appendChild(valueHost);

    const foot = el("div", "scheme-pop-foot");
    const clear = el("button", "btn ghost", "Clear");
    clear.type = "button";
    clear.addEventListener("click", () => {
      apply(null);
      closePop();
    });
    const done = el("button", "btn", "Done");
    done.type = "button";
    done.addEventListener("click", () => closePop());
    foot.appendChild(clear);
    foot.appendChild(el("span", "grow"));
    foot.appendChild(done);
    box.appendChild(foot);

    /* the value control follows the referenced field's own type */
    const refField = () => {
      const name = fieldSel.value;
      for (const c of cands) if (c.name === name) return c.field;
      return null;
    };

    const readValue = () => {
      const ref = refField();
      const op = opSel.value;
      if (VALUELESS_OPS.indexOf(op) >= 0) return null;
      const ctrl = valueHost.firstChild;
      if (!ctrl) return "";
      if (ctrl.multiple) {
        const picked = Array.from(ctrl.selectedOptions).map((x) => x.value);
        return LIST_OPS.indexOf(op) >= 0 ? picked : (picked[0] || "");
      }
      const raw = ctrl.value;
      if (ctrl.type === "number") {
        const n = Number(raw);
        return raw === "" ? null : (Number.isFinite(n) ? n : raw);
      }
      return LIST_OPS.indexOf(op) >= 0 ? (raw === "" ? [] : [raw]) : raw;
    };

    const renderValue = () => {
      valueHost.textContent = "";
      const op = opSel.value;
      const ref = refField();
      const kind = valueControlKind(ref, op);
      if (kind === "none") return;
      if (kind === "options" || kind === "options-multi") {
        const opts = optList(ref.options);
        const sel = el("select", "select");
        sel.multiple = kind === "options-multi";
        sel.size = kind === "options-multi" ? 4 : 1;
        for (const o of opts) {
          const opt = el("option", "", o.label || o.value);
          opt.value = o.value;
          sel.appendChild(opt);
        }
        const wanted = Array.isArray(work.value) ? work.value.map(String)
          : (work.value === null || work.value === undefined ? [] : [String(work.value)]);
        for (const opt of Array.from(sel.options)) opt.selected = wanted.indexOf(opt.value) >= 0;
        sel.addEventListener("change", () => { work.value = readValue(); apply({ ...work }); });
        valueHost.appendChild(sel);
        if (kind === "options-multi") {
          valueHost.appendChild(el("div", "scheme-pop-hint",
            "Several values can be picked; the condition tests whether any of them matches."));
        }
        return;
      }
      const ctrl = el("input", "input");
      ctrl.type = kind === "number" ? "number" : "text";
      const multi = ref && (ref.editor === "multiselect" || ref.type === "list");
      ctrl.placeholder = multi ? "value, value, …" : "value";
      const wanted = Array.isArray(work.value) ? work.value.join(", ") : (work.value == null ? "" : String(work.value));
      ctrl.value = wanted;
      ctrl.addEventListener("input", () => { work.value = readValue(); apply({ ...work }); });
      valueHost.appendChild(ctrl);
      if (multi) {
        valueHost.appendChild(el("div", "scheme-pop-hint",
          "This field holds several values — separate them with commas."));
      }
    };

    fieldSel.addEventListener("change", () => {
      work.field = fieldSel.value;
      work.value = "";
      renderValue();
      apply(work.field ? { ...work } : null);
    });
    opSel.addEventListener("change", () => {
      work.op = opSel.value;
      renderValue();
      apply(work.field ? { ...work } : null);
    });

    renderValue();

    const host = $("#scheme-dialog") || document.body;
    host.appendChild(box);
    placePop(box, anchor);
    pop = box;
    popAnchor = anchor;
    document.addEventListener("pointerdown", onDocDown, true);
    document.addEventListener("keydown", onDocKey, true);
  }

  /* ---------------------------------------------------------- small parts */
  function iconBtn(cls, title, iconId, onClick) {
    const b = el("button", cls);
    b.type = "button";
    b.title = title;
    b.setAttribute("aria-label", title);
    const svg = icon(iconId, 13);
    b.appendChild(svg);
    if (onClick) b.addEventListener("click", onClick);
    return b;
  }

  function chevron(dir, title, onClick) {
    const b = iconBtn("btn ghost icon", title, "i-caret", onClick);
    b.firstChild.style.transform = dir === "up" ? "rotate(-90deg)" : "rotate(90deg)";
    return b;
  }

  function textInput(value, onInput, attrs) {
    const i = el("input", "input");
    i.type = "text";
    i.value = value == null ? "" : value;
    i.spellcheck = false;
    if (attrs) for (const k of Object.keys(attrs)) i.setAttribute(k, attrs[k]);
    if (onInput) i.addEventListener("input", () => onInput(i.value, i));
    return i;
  }

  function selectBox(options, value, onChange) {
    const s = el("select", "select");
    for (const opt of options) {
      const o = el("option", "", opt.label);
      o.value = opt.value;
      s.appendChild(o);
    }
    s.value = value == null ? "" : value;
    s.addEventListener("change", () => onChange(s.value));
    return s;
  }

  function labelled(text, control) {
    const wrap = el("label", "scheme-field");
    wrap.appendChild(el("span", "scheme-field-label", text));
    wrap.appendChild(control);
    return wrap;
  }

  function checkbox(text, checked, onChange) {
    const lab = el("label", "scheme-check");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = !!checked;
    cb.addEventListener("change", () => onChange(cb.checked));
    lab.appendChild(cb);
    lab.appendChild(document.createTextNode(" " + text));
    return lab;
  }

  /* ---------------------------------------------------------- left pane */
  function renderLeft() {
    const host = $("#scheme-left");
    if (!host) return;
    host.textContent = "";
    host.appendChild(metaSection());
    host.appendChild(blocksSection());
    host.appendChild(fieldsSection());
    host.appendChild(metricsSection());
    host.appendChild(kindsSection());
  }

  function metaSection() {
    const sec = el("div", "scheme-section");
    const head = el("div", "scheme-section-head");
    head.appendChild(el("h4", "", "Scheme"));
    sec.appendChild(head);
    const stack = el("div", "scheme-stack");
    stack.appendChild(labelled("Name", textInput(state.draft.name, (v) => {
      state.draft.name = v;
      changed();
    })));
    stack.appendChild(labelled("Label", textInput(state.draft.label, (v) => {
      state.draft.label = v;
      changed();
    })));
    stack.appendChild(labelled("Description", textInput(state.draft.description, (v) => {
      state.draft.description = v;
      changed();
    }, { placeholder: "What this scheme is for" })));
    sec.appendChild(stack);
    return sec;
  }

  function blocksSection() {
    const sec = el("div", "scheme-section");
    const head = el("div", "scheme-section-head");
    head.appendChild(el("h4", "", "Blocks"));
    sec.appendChild(head);

    const list = el("div", "scheme-block-list");
    state.draft.groups.forEach((g, index) => {
      const row = el("div", "scheme-block-row");
      const input = textInput(g.name, (v) => {
        const old = g.name;
        g.name = v;
        if (g.label === old) g.label = v;
        for (const f of state.draft.fields) if (f.group === old) f.group = v;
        changed();
      }, { "aria-label": "Block name" });
      input.addEventListener("change", () => renderLeft());
      row.appendChild(input);

      row.appendChild(chevron("up", "Move block up", () => {
        if (index === 0) return;
        const gs = state.draft.groups;
        [gs[index - 1], gs[index]] = [gs[index], gs[index - 1]];
        changed();
        renderLeft();
      }));
      row.appendChild(chevron("down", "Move block down", () => {
        if (index >= state.draft.groups.length - 1) return;
        const gs = state.draft.groups;
        [gs[index + 1], gs[index]] = [gs[index], gs[index + 1]];
        changed();
        renderLeft();
      }));

      const condBtn = iconBtn("btn ghost icon scheme-block-cond" + (g.show_if ? " set" : ""),
        g.show_if ? `Condition: ${condText(g.show_if)}` : "Set a condition for this block",
        "i-filter", (e) => {
          openConditionBuilder(e.currentTarget, g.show_if, (cond) => {
            g.show_if = cond;
            changed();
            renderLeft();
          }, { title: "Block condition", excludeBlock: g.name });
        });
      row.appendChild(condBtn);

      const inUse = state.draft.fields.some((f) => f.group === g.name);
      const del = iconBtn("btn ghost icon", inUse
        ? "This block still holds fields — move or delete them first"
        : "Delete block", "i-trash", () => {
        if (inUse) return;
        state.draft.groups = state.draft.groups.filter((x) => x !== g);
        changed();
        renderLeft();
      });
      del.disabled = inUse;
      row.appendChild(del);
      list.appendChild(row);
    });
    sec.appendChild(list);

    const add = el("div", "scheme-btn-row");
    const name = textInput("", () => {}, { placeholder: "New block name" });
    const addBtn = el("button", "btn", "Add block");
    addBtn.type = "button";
    const addBlock = () => {
      const v = name.value.trim();
      if (!v) return;
      if (state.draft.groups.some((g) => g.name === v)) {
        toast("That block already exists", "err");
        return;
      }
      state.draft.groups.push({ name: v, label: v, description: "", show_if: null });
      changed();
      renderLeft();
    };
    addBtn.addEventListener("click", addBlock);
    name.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); addBlock(); }
    });
    add.appendChild(name);
    add.appendChild(addBtn);
    sec.appendChild(add);
    return sec;
  }

  function fieldsSection() {
    const sec = el("div", "scheme-section");
    const head = el("div", "scheme-section-head");
    head.appendChild(el("h4", "", "Fields"));
    sec.appendChild(head);

    for (const b of buckets()) {
      if (b.orphan) continue;
      const wrap = el("div", "scheme-bucket");
      const bh = el("div", "scheme-bucket-head");
      bh.appendChild(el("span", "scheme-bucket-name", b.label));
      bh.appendChild(el("span", "grow"));
      bh.appendChild(iconBtn("btn ghost icon", "Add a field to this block", "i-plus", () => {
        addField(b.scope, b.scope === "study" ? b.name : b.name, b.scope === "module" ? b.name : null);
      }));
      wrap.appendChild(bh);

      const ul = el("ul", "scheme-list");
      if (!b.fields.length) {
        ul.appendChild(el("li", "scheme-empty", "No fields yet."));
      }
      for (const f of b.fields) {
        ul.appendChild(fieldRow(f, b.scope));
      }
      wrap.appendChild(ul);
      sec.appendChild(wrap);
    }

    const add = el("button", "btn scheme-btn-row", "Add field");
    add.type = "button";
    add.addEventListener("click", () => {
      const first = state.draft.groups[0];
      if (!first) {
        state.draft.groups.push({ name: "Study", label: "Study", description: "", show_if: null });
      }
      addField("study", state.draft.groups[0].name, null);
    });
    add.style.width = "100%";
    add.style.justifyContent = "center";
    sec.appendChild(add);
    return sec;
  }

  function fieldRow(f, scope) {
    const li = el("li");
    const btn = el("button", "scheme-item" + (state.selected && state.selected.name === f.name ? " active" : ""));
    btn.type = "button";
    btn.dataset.name = f.name;
    btn.dataset.scope = scope;
    btn.appendChild(el("span", "label", f.label || f.name || "(unnamed)"));
    if (scope === "metric" && f.role) {
      btn.appendChild(el("span", "scheme-badge role", f.role));
    } else {
      btn.appendChild(el("span", "scheme-badge", f.type || "string"));
    }
    btn.addEventListener("click", () => {
      state.selected = { scope, name: f.name };
      state.pane = "field";
      renderAll();
    });
    li.appendChild(btn);
    return li;
  }

  /* keep the field row in step with a rename/relabel, without a full re-render */
  function syncFieldRow(oldName, f) {
    for (const row of $$(".scheme-item")) {
      if (row.dataset.name !== oldName) continue;
      row.dataset.name = f.name;
      const lab = row.querySelector(".label");
      if (lab) lab.textContent = f.label || f.name || "(unnamed)";
      const badge = row.querySelector(".scheme-badge");
      if (badge && !badge.classList.contains("role")) badge.textContent = f.type || "string";
    }
  }

  function addField(scope, group, moduleKey) {
    const f = cleanField({
      name: "",
      label: "",
      group: group || "",
      scope,
      type: "string",
      editor: "text",
      options: [],
      columns: [],
      role: null,
      required: false,
      moderator: true,
      module: moduleKey || null,
    }, scope);
    const list = scope === "study" ? state.draft.fields
      : scope === "metric" ? state.draft.metrics.fields
        : (state.draft.modules[moduleKey].fields = state.draft.modules[moduleKey].fields || []);
    list.push(f);
    changed();
    state.selected = { scope, name: f.name };
    state.pane = "field";
    renderAll();
  }

  function metricsSection() {
    const card = el("div", "scheme-card");
    const head = el("div", "scheme-card-head");
    head.appendChild(el("h4", "scheme-card-title", "Metrics"));
    head.appendChild(el("span", "grow"));
    head.appendChild(checkbox("Enabled", state.draft.metrics.enabled, (v) => {
      state.draft.metrics.enabled = v;
      changed();
      renderLeft();
    }));
    card.appendChild(head);

    const grid = el("div", "scheme-grid2");
    grid.appendChild(labelled("Group", textInput(state.draft.metrics.group, (v) => {
      state.draft.metrics.group = v;
      for (const f of state.draft.metrics.fields) f.group = v;
      changed();
    })));
    grid.appendChild(labelled("Label", textInput(state.draft.metrics.label, (v) => {
      state.draft.metrics.label = v;
      changed();
    })));
    card.appendChild(grid);

    const ul = el("ul", "scheme-list");
    if (!state.draft.metrics.fields.length) {
      ul.appendChild(el("li", "scheme-empty", "No metric fields yet."));
    }
    for (const f of state.draft.metrics.fields) ul.appendChild(fieldRow(f, "metric"));
    card.appendChild(ul);

    const add = el("button", "btn scheme-btn-row", "Add metric field");
    add.type = "button";
    add.style.width = "100%";
    add.style.justifyContent = "center";
    add.addEventListener("click", () => addField("metric", state.draft.metrics.group, null));
    card.appendChild(add);

    /* effect families */
    card.appendChild(el("div", "scheme-card-sub", "Effect-size families"));
    const families = FAMILY_ORDER.slice();
    for (const f of state.draft.effect_families) if (families.indexOf(f) < 0) families.push(f);
    const checks = el("div", "scheme-checks");
    for (const fam of families) {
      const on = state.draft.effect_families.indexOf(fam) >= 0;
      checks.appendChild(checkbox(FAMILY_LABEL[fam] || fam, on, (v) => {
        if (v) {
          if (state.draft.effect_families.indexOf(fam) < 0) state.draft.effect_families.push(fam);
        } else {
          state.draft.effect_families = state.draft.effect_families.filter((x) => x !== fam);
        }
        changed();
        renderLeft();
      }));
    }
    card.appendChild(checks);

    const presets = el("button", "btn scheme-btn-row", "Add standard effect-size fields");
    presets.type = "button";
    presets.style.width = "100%";
    presets.style.justifyContent = "center";
    presets.addEventListener("click", () => {
      const ticked = state.draft.effect_families.filter((f) => FAMILY_FIELDS[f]);
      if (!ticked.length) {
        toast("Tick at least one effect-size family first", "err");
        return;
      }
      const existing = new Set(state.draft.metrics.fields.map((f) => f.name));
      let added = 0;
      for (const fam of ticked) {
        for (const preset of FAMILY_FIELDS[fam]) {
          if (existing.has(preset.name)) continue;
          existing.add(preset.name);
          state.draft.metrics.fields.push(cleanField({
            ...preset,
            group: state.draft.metrics.group,
            scope: "metric",
            options: [],
            columns: [],
            required: false,
            moderator: true,
          }, "metric"));
          added += 1;
        }
      }
      state.draft.metrics.enabled = true;
      changed();
      renderLeft();
      toast(added ? `Added ${added} standard metric fields` : "Every standard field is already there");
    });
    card.appendChild(presets);
    return card;
  }

  function kindsSection() {
    const card = el("div", "scheme-card");
    const head = el("div", "scheme-card-head");
    head.appendChild(el("h4", "scheme-card-title", "Kinds"));
    head.appendChild(el("span", "grow"));
    const add = el("button", "btn", "Add kind");
    add.type = "button";
    add.addEventListener("click", () => {
      let n = 1;
      while (state.draft.kinds.some((k) => k.id === `kind_${n}`)) n += 1;
      state.draft.kinds.push({
        id: `kind_${n}`,
        label: `Kind ${n}`,
        base_type: "string",
        editor: "text",
        description: "",
        options: [],
        columns: [],
        role: null,
      });
      changed();
      renderLeft();
    });
    head.appendChild(add);
    card.appendChild(head);

    if (!state.draft.kinds.length) {
      card.appendChild(el("div", "scheme-empty", "No kinds yet — a kind is a reusable field template."));
    }
    for (const k of state.draft.kinds) card.appendChild(kindCard(k));
    return card;
  }

  function kindCard(k) {
    const wrap = el("div", "scheme-kind");
    const head = el("div", "scheme-kind-head");
    head.appendChild(textInput(k.id, (v) => {
      k.id = v;
      changed();
    }, { "aria-label": "Kind id" }));
    head.appendChild(textInput(k.label, (v) => {
      k.label = v;
      changed();
    }, { "aria-label": "Kind label" }));
    head.appendChild(iconBtn("btn ghost icon", "Delete kind", "i-trash", () => {
      state.draft.kinds = state.draft.kinds.filter((x) => x !== k);
      changed();
      renderLeft();
    }));
    wrap.appendChild(head);

    const grid = el("div", "scheme-grid2");
    grid.appendChild(labelled("Base type", selectBox(
      FIELD_TYPES.map((t) => ({ value: t, label: t })), k.base_type,
      (v) => { k.base_type = v; changed(); renderLeft(); })));
    grid.appendChild(labelled("Editor", selectBox(
      EDITORS.map((t) => ({ value: t, label: t })), k.editor,
      (v) => { k.editor = v; changed(); renderLeft(); })));
    wrap.appendChild(grid);

    wrap.appendChild(el("div", "scheme-card-sub", "Options"));
    const list = el("div", "scheme-editlist");
    k.options.forEach((o, i) => {
      const row = el("div", "scheme-editrow");
      row.appendChild(textInput(o.value, (v) => {
        const old = o.value;
        o.value = v;
        if (o.label === old) o.label = v;
        changed();
      }, { "aria-label": "Option" }));
      row.appendChild(iconBtn("btn ghost icon", "Remove option", "i-trash", () => {
        k.options.splice(i, 1);
        changed();
        renderLeft();
      }));
      list.appendChild(row);
    });
    const addOpt = el("button", "btn", "Add option");
    addOpt.type = "button";
    addOpt.addEventListener("click", () => {
      k.options.push({ value: "", label: "" });
      changed();
      renderLeft();
    });
    list.appendChild(addOpt);
    wrap.appendChild(list);
    return wrap;
  }

  /* ---------------------------------------------------------- right pane */
  function renderRight() {
    const tabs = $$("#scheme-tabs button");
    tabs.forEach((b) => b.classList.toggle("active", b.dataset.spane === state.pane));
    const host = $("#scheme-right-body");
    if (!host) return;
    host.textContent = "";
    if (state.pane === "prompt") {
      host.appendChild(promptSection());
      return;
    }
    const f = state.selected ? findField(state.selected.scope, state.selected.name) : null;
    if (!f) {
      const box = el("div", "scheme-load");
      box.appendChild(el("p", "", "Select a field on the left to edit it."));
      host.appendChild(box);
      return;
    }
    host.appendChild(fieldDetails(f, state.selected.scope));
  }

  function fieldDetails(f, scope) {
    const wrap = el("div");
    const grid = el("div", "scheme-grid");

    const nameInput = textInput(f.name, (v) => {
      const old = f.name;
      f.name = v;
      nameLocked.add(f);
      if (state.selected) state.selected.name = v;
      syncFieldRow(old, f);
      changed();
    }, { "aria-label": "Field name" });

    grid.appendChild(labelled("Label", textInput(f.label, (v) => {
      const old = f.name;
      f.label = v;
      if (!nameLocked.has(f)) {
        f.name = slugify(v);
        nameInput.value = f.name;
        if (state.selected) state.selected.name = f.name;
      }
      syncFieldRow(old, f);
      changed();
    })));
    grid.appendChild(labelled("Name", nameInput));

    /* block */
    const blockOptions = [];
    if (scope === "study") {
      for (const g of state.draft.groups) blockOptions.push({ value: g.name, label: g.label || g.name });
    } else if (scope === "metric") {
      blockOptions.push({ value: state.draft.metrics.group, label: state.draft.metrics.group });
    } else {
      for (const key of Object.keys(state.draft.modules)) {
        blockOptions.push({ value: key, label: state.draft.modules[key].label || key });
      }
    }
    grid.appendChild(labelled("Block", selectBox(blockOptions, bucketName(scope, f), (v) => {
      if (scope === "study") {
        f.group = v;
      } else if (scope === "metric") {
        state.draft.metrics.group = v;
        for (const x of state.draft.metrics.fields) x.group = v;
      } else {
        moveModuleField(f, v);
      }
      changed();
      renderAll();
    })));

    /* scope */
    const scopeOptions = SCOPES.map((s) => ({ value: s, label: s }));
    const scopeSel = selectBox(scopeOptions, scope, (v) => {
      moveScope(f, scope, v);
      changed();
      renderAll();
    });
    if (!Object.keys(state.draft.modules).length) {
      const modOpt = Array.from(scopeSel.options).find((o) => o.value === "module");
      if (modOpt) modOpt.disabled = true;
    }
    grid.appendChild(labelled("Scope", scopeSel));

    grid.appendChild(labelled("Type", selectBox(
      FIELD_TYPES.map((t) => ({ value: t, label: t })), f.type, (v) => {
        f.type = v;
        changed();
        renderRight();
      })));

    grid.appendChild(labelled("Editor", selectBox(
      EDITORS.map((t) => ({ value: t, label: t })), f.editor, (v) => {
        f.editor = v;
        changed();
        renderRight();
      })));

    /* role */
    const roleSel = el("select", "select");
    const none = el("option", "", "— no role —");
    none.value = "";
    roleSel.appendChild(none);
    const studyRoles = state.roles.study.length ? state.roles.study : [];
    const metricRoles = state.roles.metric.length ? state.roles.metric : [];
    const allRoles = state.roles.all.length
      ? state.roles.all : studyRoles.concat(metricRoles);
    if (studyRoles.length) {
      const og = el("optgroup", "", "Study roles");
      og.label = "Study roles";
      for (const r of studyRoles) {
        const o = el("option", "", r);
        o.value = r;
        og.appendChild(o);
      }
      roleSel.appendChild(og);
    }
    if (metricRoles.length) {
      const og = el("optgroup", "", "Metric roles");
      og.label = "Metric roles";
      for (const r of metricRoles) {
        const o = el("option", "", r);
        o.value = r;
        og.appendChild(o);
      }
      roleSel.appendChild(og);
    }
    for (const r of allRoles) {
      if (studyRoles.indexOf(r) >= 0 || metricRoles.indexOf(r) >= 0) continue;
      const o = el("option", "", r);
      o.value = r;
      roleSel.appendChild(o);
    }
    /* a role the vocabulary does not know is still shown, so the editor never
       silently drops it */
    if (f.role && allRoles.indexOf(f.role) < 0 && studyRoles.indexOf(f.role) < 0
      && metricRoles.indexOf(f.role) < 0) {
      const o = el("option", "", `${f.role} (not in the vocabulary)`);
      o.value = f.role;
      roleSel.appendChild(o);
    }
    roleSel.value = f.role || "";
    roleSel.addEventListener("change", () => {
      f.role = roleSel.value || null;
      changed();
      renderLeft();
    });
    grid.appendChild(labelled("Role", roleSel));

    grid.appendChild(labelled("Required", checkbox("Required", f.required, (v) => {
      f.required = v;
      changed();
    })));

    grid.appendChild(labelled("Use as moderator", checkbox("Use as moderator", f.moderator, (v) => {
      f.moderator = v;
      changed();
    })));

    /* required if / show if */
    const reqCond = el("div", "scheme-cond");
    reqCond.appendChild(el("span", "scheme-cond-text" + (f.required_if ? "" : " none"),
      f.required_if ? condText(f.required_if) : "Always required when the field is shown"));
    const reqBtn = el("button", "btn ghost", f.required_if ? "Edit" : "Add");
    reqBtn.type = "button";
    reqBtn.addEventListener("click", (e) => {
      openConditionBuilder(e.currentTarget, f.required_if, (cond) => {
        f.required_if = cond;
        changed();
        renderRight();
      }, { title: "Required if", excludeName: f.name });
    });
    reqCond.appendChild(reqBtn);
    if (f.required_if) {
      const clr = el("button", "btn ghost", "Clear");
      clr.type = "button";
      clr.addEventListener("click", () => {
        f.required_if = null;
        changed();
        renderRight();
      });
      reqCond.appendChild(clr);
    }
    const reqWrap = el("div", "scheme-field wide");
    reqWrap.appendChild(el("span", "scheme-field-label", "Required if"));
    reqWrap.appendChild(reqCond);
    grid.appendChild(reqWrap);

    const showCond = el("div", "scheme-cond");
    showCond.appendChild(el("span", "scheme-cond-text" + (f.show_if ? "" : " none"),
      f.show_if ? condText(f.show_if) : "Always shown"));
    const showBtn = el("button", "btn ghost", f.show_if ? "Edit" : "Add");
    showBtn.type = "button";
    showBtn.addEventListener("click", (e) => {
      openConditionBuilder(e.currentTarget, f.show_if, (cond) => {
        f.show_if = cond;
        changed();
        renderRight();
      }, { title: "Show if", excludeName: f.name });
    });
    showCond.appendChild(showBtn);
    if (f.show_if) {
      const clr = el("button", "btn ghost", "Clear");
      clr.type = "button";
      clr.addEventListener("click", () => {
        f.show_if = null;
        changed();
        renderRight();
      });
      showCond.appendChild(clr);
    }
    const showWrap = el("div", "scheme-field wide");
    showWrap.appendChild(el("span", "scheme-field-label", "Show if"));
    showWrap.appendChild(showCond);
    grid.appendChild(showWrap);

    const descWrap = el("div", "scheme-field wide");
    descWrap.appendChild(el("span", "scheme-field-label", "Description"));
    const desc = el("textarea", "textarea");
    desc.rows = 2;
    desc.value = f.description || "";
    desc.addEventListener("input", () => { f.description = desc.value; changed(); });
    descWrap.appendChild(desc);
    grid.appendChild(descWrap);

    const instWrap = el("div", "scheme-field wide");
    instWrap.appendChild(el("span", "scheme-field-label", "Instruction"));
    const inst = el("textarea", "textarea");
    inst.rows = 2;
    inst.value = f.instruction || "";
    inst.addEventListener("input", () => { f.instruction = inst.value; changed(); });
    instWrap.appendChild(inst);
    grid.appendChild(instWrap);

    wrap.appendChild(grid);

    /* kind picker */
    if (state.draft.kinds.length) {
      wrap.appendChild(el("div", "scheme-sub", "Kind"));
      const kindSel = el("select", "select");
      const noneK = el("option", "", "— no kind —");
      noneK.value = "";
      kindSel.appendChild(noneK);
      for (const k of state.draft.kinds) {
        const o = el("option", "", k.label || k.id);
        o.value = k.id;
        kindSel.appendChild(o);
      }
      kindSel.value = f.kind || "";
      kindSel.addEventListener("change", () => {
        f.kind = kindSel.value || null;
        const k = state.draft.kinds.find((x) => x.id === f.kind);
        if (k) {
          f.type = k.base_type;
          f.editor = k.editor;
          if (k.options && k.options.length) f.options = k.options.map((o) => ({ ...o }));
          if (!f.role && k.role) f.role = k.role;
        }
        changed();
        renderAll();
      });
      const kw = el("div", "scheme-field");
      kw.appendChild(el("span", "scheme-field-label", "Kind"));
      kw.appendChild(kindSel);
      wrap.appendChild(kw);
    }

    /* options editor */
    const showOptions = f.editor === "select" || f.editor === "multiselect" || f.type === "list";
    if (showOptions) {
      wrap.appendChild(el("div", "scheme-sub", "Options"));
      wrap.appendChild(optionsEditor(f));
    }

    /* columns editor */
    if (f.editor === "objectlist") {
      wrap.appendChild(el("div", "scheme-sub", "Columns"));
      wrap.appendChild(columnsEditor(f));
    }

    /* footer */
    const foot = el("div", "scheme-fieldfoot");
    foot.appendChild(chevron("up", "Move field up", () => moveField(scope, f, -1)));
    foot.appendChild(chevron("down", "Move field down", () => moveField(scope, f, 1)));
    foot.appendChild(el("span", "grow"));
    const del = el("button", "btn danger", "Delete field");
    del.type = "button";
    del.addEventListener("click", () => {
      const list = listOf(scope, f);
      if (list) {
        const i = list.indexOf(f);
        if (i >= 0) list.splice(i, 1);
      }
      state.selected = null;
      changed();
      renderAll();
    });
    foot.appendChild(del);
    wrap.appendChild(foot);
    return wrap;
  }

  function optionsEditor(f) {
    const list = el("div", "scheme-editlist");
    f.options.forEach((o, i) => {
      const row = el("div", "scheme-editrow");
      row.appendChild(textInput(o.value, (v) => {
        const old = o.value;
        o.value = v;
        if (o.label === old) o.label = v;
        changed();
      }, { "aria-label": "Option value" }));
      row.appendChild(iconBtn("btn ghost icon", "Remove option", "i-trash", () => {
        f.options.splice(i, 1);
        changed();
        renderRight();
      }));
      list.appendChild(row);
    });
    const add = el("button", "btn", "Add option");
    add.type = "button";
    add.addEventListener("click", () => {
      f.options.push({ value: "", label: "" });
      changed();
      renderRight();
    });
    list.appendChild(add);
    return list;
  }

  function columnsEditor(f) {
    const list = el("div", "scheme-editlist");
    f.columns.forEach((c, i) => {
      const row = el("div", "scheme-editrow");
      row.appendChild(textInput(c.name, (v) => { c.name = v; changed(); },
        { "aria-label": "Column name" }));
      row.appendChild(selectBox(FIELD_TYPES.map((t) => ({ value: t, label: t })), c.type,
        (v) => { c.type = v; changed(); }));
      row.appendChild(iconBtn("btn ghost icon", "Remove column", "i-trash", () => {
        f.columns.splice(i, 1);
        changed();
        renderRight();
      }));
      list.appendChild(row);
    });
    const add = el("button", "btn", "Add column");
    add.type = "button";
    add.addEventListener("click", () => {
      f.columns.push({ name: "", type: "string" });
      changed();
      renderRight();
    });
    list.appendChild(add);
    return list;
  }

  function moveField(scope, f, delta) {
    const list = listOf(scope, f);
    if (!list) return;
    const i = list.indexOf(f);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= list.length) return;
    [list[i], list[j]] = [list[j], list[i]];
    changed();
    renderAll();
  }

  function moveModuleField(f, moduleKey) {
    if (!state.draft.modules[moduleKey]) return;
    const old = listOf("module", f);
    if (old) {
      const i = old.indexOf(f);
      if (i >= 0) old.splice(i, 1);
    }
    f.module = moduleKey;
    f.group = moduleKey;
    state.draft.modules[moduleKey].fields = state.draft.modules[moduleKey].fields || [];
    state.draft.modules[moduleKey].fields.push(f);
  }

  function moveScope(f, from, to) {
    if (from === to) return;
    if (to === "module" && !Object.keys(state.draft.modules).length) return;
    const list = listOf(from, f);
    if (list) {
      const i = list.indexOf(f);
      if (i >= 0) list.splice(i, 1);
    }
    f.scope = to;
    if (to === "study") {
      f.group = state.draft.groups[0] ? state.draft.groups[0].name : "";
      f.module = null;
      state.draft.fields.push(f);
    } else if (to === "metric") {
      f.group = state.draft.metrics.group;
      f.module = null;
      state.draft.metrics.fields = state.draft.metrics.fields || [];
      state.draft.metrics.fields.push(f);
    } else {
      const key = Object.keys(state.draft.modules)[0];
      if (!key) return;
      f.group = key;
      f.module = key;
      state.draft.modules[key].fields = state.draft.modules[key].fields || [];
      state.draft.modules[key].fields.push(f);
    }
  }

  /* ---------------------------------------------------------- prompt tab */
  function fieldLine(f) {
    const parts = [];
    parts.push(`* **${f.name || "(unnamed)"}**`);
    if (f.label && f.label !== f.name) parts.push(` — ${f.label}`);
    if (f.role) parts.push(` [role: ${f.role}]`);
    if (f.required) parts.push(" (required)");
    parts.push(`: ${f.description || ""}`);
    if (f.instruction) parts.push(` ${f.instruction}`);
    if (f.show_if) parts.push(` Code only if ${condText(f.show_if)}.`);
    const opts = (f.options || []).map((o) => o.label || o.value).filter(Boolean);
    if (opts.length) parts.push(` Options: ${opts.join(", ")}.`);
    return parts.join("");
  }

  function generatePrompt() {
    const d = state.draft;
    const lines = [];
    lines.push("You are an expert academic research assistant and data coder. "
      + "Read the paper thoroughly and code each independent experiment.");
    lines.push('Output the extracted data strictly as a JSON array of objects, one per experiment: '
      + '[{"study": {...}, "metrics": [...]}].');
    lines.push("");
    lines.push("Study fields:");
    let any = false;
    for (const g of d.groups) {
      const members = d.fields.filter((f) => f.group === g.name);
      if (!members.length) continue;
      any = true;
      lines.push(`### ${g.label || g.name}`);
      if (g.show_if) lines.push(`(this block applies only when ${condText(g.show_if)})`);
      for (const f of members) lines.push(fieldLine(f));
    }
    if (!any) lines.push("* (add fields to generate this section)");
    const required = d.fields.filter((f) => f.required).map((f) => f.name);
    if (required.length) lines.push(`Required study fields: ${required.join(", ")}.`);
    if (d.metrics.enabled) {
      lines.push("");
      lines.push(`${d.metrics.label || "Effect sizes"} — one entry per inferential DV/contrast:`);
      for (const f of d.metrics.fields) lines.push(fieldLine(f));
      const roles = d.metrics.fields.map((f) => f.role).filter(Boolean);
      if (roles.length) lines.push(`Metric roles in use: ${Array.from(new Set(roles)).join(", ")}.`);
      if (d.effect_families.length) {
        lines.push(`Preferred effect-size families: ${d.effect_families.join(", ")}.`);
      }
      lines.push("For every inferential DV and every reported group contrast add a metrics entry with "
        + "at least one complete set of: full descriptives (roles n1, m1, sd1, n2, m2, sd2), "
        + "(role t with n1, n2), (role F with df_num, df_den, n1, n2), (role r with n_total), "
        + "(role yi with vi or sei), (role d or g with n1, n2).");
      lines.push("Never leave a contrast without a complete metric set; leave unavailable numeric "
        + "fields null and record why in the **missing_metrics_reason** field.");
    } else {
      lines.push("");
      lines.push("Effect sizes: for every inferential dependent variable (DV) and every reported "
        + "group contrast add a metrics array entry with at least one complete set of full "
        + "descriptives or a documented fallback.");
    }
    return lines.join("\n");
  }

  function promptSection() {
    const wrap = el("div");
    const sysWrap = el("div", "scheme-field");
    sysWrap.appendChild(el("span", "scheme-field-label", "prompts.system"));
    const ta = el("textarea", "textarea");
    ta.rows = 6;
    ta.value = state.draft.prompts.system || "";
    ta.placeholder = "The system prompt the coding agents receive.";
    ta.addEventListener("input", () => {
      state.draft.prompts.system = ta.value;
      changed();
    });
    sysWrap.appendChild(ta);
    wrap.appendChild(sysWrap);

    wrap.appendChild(el("div", "scheme-sub", "Generated prompt (read-only)"));
    const actions = el("div", "scheme-prompt-actions");
    const copy = el("button", "btn", "Copy prompt");
    copy.type = "button";
    copy.addEventListener("click", () => copyText(generatePrompt()));
    const refresh = el("button", "btn ghost", "Refresh preview");
    refresh.type = "button";
    refresh.addEventListener("click", () => renderRight());
    actions.appendChild(copy);
    actions.appendChild(refresh);
    actions.appendChild(el("span", "grow"));
    wrap.appendChild(actions);

    const pre = el("pre", "scheme-prompt", generatePrompt());
    wrap.appendChild(pre);
    return wrap;
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast("Prompt copied");
    } catch (err) {
      const ta = el("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
        toast("Prompt copied");
      } catch (e) {
        toast("Could not copy the prompt", "err");
      }
      ta.remove();
    }
  }

  /* ---------------------------------------------------------- save / load */
  /* `changed()` marks an edit; `markDirty()` only refreshes the pill, so a
     re-render never looks like an unsaved change. */
  function changed() {
    state.rev += 1;
    markDirty();
  }

  function markDirty() {
    const pill = $("#scheme-state");
    if (pill) {
      const dirty = isDirty();
      pill.textContent = dirty ? "unsaved changes" : "saved";
      pill.className = "pill" + (dirty ? " warn" : "");
    }
  }

  function showProblems(problems) {
    const host = $("#scheme-problems");
    if (!host) return;
    host.textContent = "";
    const list = Array.isArray(problems) ? problems : [];
    state.problems = list;
    if (!list.length) {
      host.hidden = true;
      return;
    }
    const hasError = list.some((p) => (p.level || "error") === "error");
    host.hidden = false;
    host.classList.toggle("warn", !hasError);
    host.appendChild(el("h4", "", hasError ? "The scheme was not saved" : "Saved with warnings"));
    for (const p of list) {
      const row = el("div", "scheme-problem");
      row.appendChild(el("span", "lvl", p.level || "error"));
      row.appendChild(el("span", "", p.message || String(p)));
      const where = p.where || p.path;
      if (where) row.appendChild(el("span", "path", where));
      host.appendChild(row);
    }
  }

  async function save() {
    const btn = $("#scheme-save");
    if (btn) btn.disabled = true;
    showProblems([]);
    try {
      const res = await fetch("/api/scheme", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(buildPayload()),
      });
      let data = null;
      try {
        data = await res.json();
      } catch (err) {
        data = null;
      }
      const problems = (data && data.problems) || [];
      if (!res.ok || (data && data.ok === false)) {
        showProblems(problems.length ? problems
          : [{ level: "error", message: `The server refused the scheme (HTTP ${res.status}).` }]);
        toast("The scheme was not saved", "err");
        return;
      }
      if (data && data.scheme) {
        state.draft = normaliseDraft(data.scheme);
      }
      state.baselineRev = state.rev;
      showProblems(problems);
      renderAll();
      toast("Scheme saved");
      window.dispatchEvent(new CustomEvent("cs:scheme-saved"));
    } catch (err) {
      showProblems([{ level: "error", message: String((err && err.message) || err) }]);
      toast("The scheme could not be saved", "err");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function loadRoles() {
    try {
      const r = await api("/api/scheme/roles");
      const study = Array.isArray(r.study) ? r.study : [];
      const metric = Array.isArray(r.metric) ? r.metric : [];
      const all = Array.isArray(r.all) ? r.all : study.concat(metric);
      state.roles = { study, metric, all };
    } catch (err) {
      const used = [];
      for (const f of allFields()) if (f.role && used.indexOf(f.role) < 0) used.push(f.role);
      state.roles = { study: [], metric: [], all: used };
    }
  }

  function renderLoading() {
    const host = $("#scheme-right-body");
    const left = $("#scheme-left");
    if (left) left.textContent = "";
    if (!host) return;
    host.textContent = "";
    const box = el("div", "scheme-load");
    box.appendChild(el("b", "", "Loading the scheme…"));
    host.appendChild(box);
  }

  function renderLoadError(message, detail) {
    const host = $("#scheme-right-body");
    const left = $("#scheme-left");
    if (left) left.textContent = "";
    if (!host) return;
    host.textContent = "";
    const box = el("div", "scheme-load");
    box.appendChild(el("b", "", message));
    if (detail) box.appendChild(el("p", "", detail));
    const again = el("button", "btn", "Try again");
    again.type = "button";
    again.addEventListener("click", () => load());
    box.appendChild(again);
    host.appendChild(box);
    const note = $("#scheme-note");
    if (note) note.textContent = "The scheme could not be loaded.";
  }

  async function load() {
    const note = $("#scheme-note");
    if (note) note.textContent = "Loading the scheme…";
    let raw = null;
    try {
      raw = await api("/api/scheme");
    } catch (err) {
      renderLoadError("The scheme could not be loaded",
        "The /api/scheme endpoint did not answer. The scheme editor needs the scheme API.");
      return;
    }
    const doc = (raw && typeof raw === "object" && raw.scheme && typeof raw.scheme === "object")
      ? raw.scheme : raw;
    if (!doc || typeof doc !== "object") {
      renderLoadError("The scheme could not be loaded",
        "The /api/scheme endpoint did not return a scheme document.");
      return;
    }
    state.draft = normaliseDraft(doc);
    state.rev = 0;
    state.baselineRev = 0;
    state.selected = null;
    state.pane = "field";
    state.loaded = true;
    showProblems([]);
    await loadRoles();
    renderAll();
    if (note) note.textContent = "";
  }

  /* ---------------------------------------------------------- rendering */
  function renderAll() {
    renderLeft();
    renderRight();
    markDirty();
  }

  /* ---------------------------------------------------------- confirm */
  /* The app's own confirm sheet (the one the coding tab uses) — a native
     window.confirm() would block the whole page behind the modal. */
  function confirmDialog(title, text, okLabel) {
    return new Promise((resolve) => {
      const dlg = $("#dialog");
      const ok = $("#dialog-ok");
      const cancel = $("#dialog-cancel");
      if (!dlg || !ok || !cancel) {
        resolve(window.confirm(text));
        return;
      }
      $("#dialog-title").textContent = title;
      $("#dialog-text").textContent = text;
      ok.textContent = okLabel || "Discard changes";
      ok.className = "btn danger";
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
        if (dlg.open) dlg.close();
        resolve(v);
      };
      const onOk = () => done(true);
      const onCancel = () => done(false);
      const onClose = () => done(false);
      ok.addEventListener("click", onOk);
      cancel.addEventListener("click", onCancel);
      dlg.addEventListener("close", onClose);
      dlg.showModal();
    });
  }

  /* ---------------------------------------------------------- public API */
  function isOpen() {
    const dlg = $("#scheme-dialog");
    return !!(dlg && dlg.open);
  }

  function open() {
    const dlg = $("#scheme-dialog");
    if (!dlg) return;
    if (dlg.open) return;          // already open — never reload over unsaved work
    dlg.showModal();
    state.problems = [];
    renderLoading();
    load();
  }

  async function close() {
    if (isDirty()) {
      const ok = await confirmDialog("Discard the scheme changes?",
        "The scheme has unsaved changes. Closing the editor discards them.", "Discard changes");
      if (!ok) return;
    }
    closePop();
    const dlg = $("#scheme-dialog");
    if (dlg && dlg.open) dlg.close();
  }

  /* ---------------------------------------------------------- wiring */
  function wire() {
    const btn = $("#schema-btn");
    if (btn) btn.addEventListener("click", () => open());
    const cancel = $("#scheme-cancel");
    if (cancel) cancel.addEventListener("click", () => close());
    const saveBtn = $("#scheme-save");
    if (saveBtn) saveBtn.addEventListener("click", () => save());
    const tabs = $$("#scheme-tabs button");
    tabs.forEach((b) => b.addEventListener("click", () => {
      state.pane = b.dataset.spane;
      renderRight();
    }));
    const dlg = $("#scheme-dialog");
    if (dlg) {
      dlg.addEventListener("cancel", (e) => {
        e.preventDefault();
        close();
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }

  window.CSScheme = { open, close, isOpen };
})();
