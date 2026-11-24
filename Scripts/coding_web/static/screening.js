/* ============================================================
   Coding Studio — Abstract screening tab
   Human abstract screening against the inclusion criteria.

   The criteria carry an **auto-approval rule set**: a dictionary of
   named regular expressions that approve the criterion when the
   abstract text matches.  A rule that matches pre-fills the
   criterion's first pass value and the card says WHICH rule matched;
   a criterion with no rules is never pre-filled.  The rules come from
   the scheme's criteria (/api/criteria) - nothing is hardcoded here
   any more.  Before the rules, and only outside blind mode and only
   when the source is the master workbook (never the decision-free
   records file), a blank gating criterion is pre-filled from the AI
   screening verdict; C3 is pre-filled "Yes" by the English detector.

   Outcome (the same rule as screening_lib.screen_outcome): one failing
   gating criterion -> Exclude; every gating criterion passing ->
   Include; every one answered, none failing, some "Unsure" -> Unsure
   (goes to full text); otherwise incomplete.

   Inclusion is driven by the Tab key: each Tab accepts the focused
   criterion (leaving an existing verdict as-is) and advances; on the
   last criterion the next Tab saves the record and loads the next
   abstract in the same place.  Y / N / U = verdict, Shift+Tab = back,
   Ctrl+Z = undo, Alt+Down / Alt+Up = next / previous abstract, Ctrl+J =
   next unscreened.  Every completed abstract is written to
   Screening/<coder>.json immediately; leaving an abstract with unsaved
   changes saves it first.

   Endpoints: /api/screening/{meta,list,abstract/<id>,decision,merge,merge/status}.
   ============================================================ */
(() => {
  "use strict";

  const { $, $$, esc, api, toast } = window.CS;
  const icon = window.CS.svg;

  const S = {
    started: false,
    meta: null,
    criteria: [],
    criteriaByKey: {},     // the scheme's criteria, by key
    rows: [],           // metadata for the current server-side filter
    byIdMeta: {},
    dec: {},            // decisions saved in this session for this coder
    coder: "",
    blind: false,
    filter: "all",
    search: "",
    keys: [],           // ids in working order (filtered list or random sample)
    pos: -1,
    cur: null,
    ai: {},
    values: {},
    critIdx: 0,
    c3Auto: false,      // C3 pre-filled "Yes" from the English-word detector
    autoUnsure: new Set(),  // criteria pre-filled "Unsure" (no rule matched)
    autoRules: new Map(),   // criteria pre-filled by a rule -> the rule names
    aiPrefill: new Set(),   // criteria pre-filled from the master dataset's AI verdict
    aiPrefillOn: true,  // pre-fill from the AI verdict (never in blind mode)
    stale: null,        // a saved decision that no longer matches this row's title
    coderMode: false,   // mirrors the coding tab's coder mode: auto-advance to the next unfilled
    shuffleOn: false,
    viewer: "abstract",
    saving: false,
    dirty: false,       // the shown abstract has unsaved changes
    undo: [],           // verdict changes on the shown abstract, newest last
    fieldEdits: 0,      // certainty/rationale edits on the shown abstract
    cleanDepth: 0,      // undo.length + fieldEdits at the last load/save
    cleanSaved: false,  // whether that clean state is on disk
    savePromise: null,
    wired: false,
    loadSeq: 0,         // guards against out-of-order abstract responses
    listSeq: 0,
    timer: null,
    mergePoll: null,
    bootPromise: null,
    booted: false,
    scrollGuard: false,
    scrollTimer: null,
  };

  // ---------------------------------------------------------------- helpers
  // call() (defined under "loading") surfaces the server's {"error": ...} text
  const postJson = (path, data) => call(path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
  });

  const currentCriterion = () => S.criteria[S.critIdx] || null;

  const blankValues = () => {
    const v = {};
    S.criteria.forEach((c) => { v[c.label] = { verdict: "", certainty: "", rationale: "" }; });
    return v;
  };

  /* The outcome of a set of verdicts, by the rule the server stores
     (screening_lib.screen_outcome): over the GATING criteria, a verdict
     that is neither a pass value nor "Unsure" fails and excludes; all
     passing -> Include; all answered with some "Unsure" -> Unsure (goes on
     to full text); anything blank -> "" (incomplete). */
  const gatingCriteria = () => S.criteria.filter((c) => c.gating !== false && c.kind !== "classify");
  const passes = (c, v) => (c.pass_values && c.pass_values.length ? c.pass_values : ["Yes"])
    .some((p) => String(p).trim().toLowerCase() === String(v).trim().toLowerCase());

  function outcomeOf(verdictOf) {
    const gating = gatingCriteria();
    if (!gating.length) return "";
    let blank = false, unsure = false;
    for (const c of gating) {
      const v = String(verdictOf(c) || "").trim();
      if (!v) { blank = true; continue; }
      if (passes(c, v)) continue;
      if (v.toLowerCase() === "unsure") { unsure = true; continue; }
      return "Exclude";
    }
    if (blank) return "";
    return unsure ? "Unsure" : "Include";
  }

  function currentDecision() {
    return outcomeOf((c) => (S.values[c.label] || {}).verdict);
  }

  function decisionPill(value) {
    const el = $("#screen-decision");
    if (!el) return;
    if (value === "Include") { el.textContent = "Include"; el.className = "pill inc"; }
    else if (value === "Exclude") { el.textContent = "Exclude"; el.className = "pill exc"; }
    else if (value === "Unsure") { el.textContent = "Unsure → full text"; el.className = "pill unsure"; }
    else { el.textContent = "Incomplete"; el.className = "pill warn"; }
  }

  function coderName() {
    return (S.coder || "").trim();
  }

  /* abstracts carry inline markup from the databases (<i>OR</i>); show it as text */
  function plain(s) {
    return String(s ?? "")
      .replace(/<br\s*\/?>/gi, "\n")
      .replace(/<\/?(?:i|b|em|strong|sup|sub|u|span|p)(?:\s[^<>]*)?>/gi, "");
  }

  // ---------------------------------------------------------------- language
  // The C3 (English) verdict is pre-filled "Yes" when the abstract looks
  // English.  We count how many word-like tokens are frequent English words:
  // real English prose clears 20% easily, most other languages do not.  The
  // list mixes function words with words common to abstract/social-science text.
  const EN_WORDS = new Set((
    "the be to of and a in that have i it for not on with he as you do at this but " +
    "his by from they we say her she or an will my one all would there their what so " +
    "up out if about who get which go me when make can like time no just him know take " +
    "people into year your good some could them see other than then now look only come " +
    "its over think also back after use two how our work first well way even new want " +
    "because any these give day most us is are was were been has had did does doing done " +
    "being am were between within during before above below more less many much such same " +
    "another each every both few several own here where why while again once very " +
    "should shall may might must study studies participants results research effect " +
    "effects group groups experiment experimental control condition conditions information " +
    "social media news false misinformation disinformation students data analysis test " +
    "tests tested testing found using used use users online survey surveys sample " +
    "significant differences participants reported report reports condition article " +
    "articles journal authors paper papers present presents presented show shows shown " +
    "suggest suggests suggest that increase decrease higher lower compared comparison " +
    "accuracy accurate political science climate health vaccination covid intervention"
  ).split(/\s+/).filter(Boolean));

  function englishShare(text) {
    const toks = String(text || "").toLowerCase().match(/[a-z][a-z'-]*/g) || [];
    if (toks.length < 12) return 0;
    let hit = 0;
    for (const t of toks) if (EN_WORDS.has(t)) hit += 1;
    return hit / toks.length;
  }

  function looksEnglish(rec) {
    return englishShare(`${(rec && rec.title) || ""} ${(rec && rec.abstract) || ""}`) > 0.20;
  }

  // ---------------------------------------------------------------- rules
  // A criterion carries an auto-approval rule set: a dictionary of named
  // regular expressions.  A rule approves the criterion when its
  // expression matches anywhere in the title/abstract.  The matching is
  // case-insensitive unless the expression starts with "(?-i)" - the same
  // rule the criteria library (criteria_lib.py) applies, so the editor
  // and the screening tab can never disagree.
  function compileRule(pattern) {
    const text = String(pattern == null ? "" : pattern);
    let body = text;
    let flags = "i";
    if (body.startsWith("(?-i)")) { body = body.slice(4); flags = ""; }
    if (!body.trim()) return { ok: false, error: "the expression is empty" };
    try {
      return { ok: true, regex: new RegExp(body, flags) };
    } catch (err) {
      return { ok: false, error: String((err && err.message) || err) };
    }
  }

  /* the scheme's criterion that goes with a screening criterion (the
     screening tab keys its criteria by their key, "C1" ...) */
  function schemeCriterion(criterion) {
    const key = criterion && (criterion.key || criterion.label);
    return (key && S.criteriaByKey[key]) || null;
  }

  /* the rule set of a screening criterion: {} when it has none */
  function ruleSetFor(criterion) {
    const c = schemeCriterion(criterion);
    const rules = c && c.auto_approve;
    return (rules && typeof rules === "object") ? rules : {};
  }

  /* the value a criterion passes with: its first pass value */
  function firstPassValue(criterion) {
    const c = schemeCriterion(criterion);
    const values = (c && Array.isArray(c.pass_values)) ? c.pass_values : [];
    return values.length ? String(values[0]) : "Yes";
  }

  /* the text the rules are matched against */
  function abstractText() {
    const rec = S.cur;
    if (!rec) return "";
    return [rec.title, rec.abstract].filter(Boolean).join("\n");
  }

  /* the rules that match the text (a broken rule matches nothing) */
  function matchRules(rules, text) {
    const hits = [];
    for (const name of Object.keys(rules || {})) {
      const compiled = compileRule(rules[name]);
      if (!compiled.ok) continue;
      if (compiled.regex.test(text)) hits.push(name);
    }
    return hits;
  }

  /* does the criterion have evidence?  A criterion without rules has
     nothing to check (so it is never held back), a criterion with rules
     has evidence when one of them matches. */
  function criterionHasRuleEvidence(criterion) {
    const rules = ruleSetFor(criterion);
    if (!Object.keys(rules).length) return true;
    return matchRules(rules, abstractText()).length > 0;
  }

  // ---------------------------------------------------------------- loading
  /* fetch JSON and surface the server's own error text (the shared api()
     only says "HTTP 400", which tells the coder nothing) */
  async function call(path, opts) {
    const r = await fetch(path, opts);
    let body = null;
    try { body = await r.json(); } catch { body = null; }
    if (!r.ok || (body && body.ok === false)) {
      const msg = (body && body.error) || `${path} → HTTP ${r.status}`;
      throw new Error(msg);
    }
    return body;
  }

  /* A missing workbook or a missing title column used to look like an empty
     list ("No abstracts match").  Say what is wrong and where. */
  function workbookProblem() {
    const m = S.meta;
    if (!m) return "";
    if (!m.available) return m.error || `${m.master || "master_dataset.xlsx"} was not found in the project folder.`;
    if (m.error) return m.error;
    if (!m.total) return `${m.master} has no data rows${m.sheet ? ` (sheet '${m.sheet}')` : ""}.`;
    return "";
  }

  function renderProblem(text) {
    const host = $("#screen-list");
    if (host) host.innerHTML = `<div class="empty">${icon("i-alert")}<b>Cannot load the screening workbook</b><p>${esc(text)}</p></div>`;
    const form = $("#screen-form");
    if (form) form.innerHTML = `<p class="side-hint">${esc(text)}</p>`;
  }

  async function loadMeta() {
    S.meta = await call(`api/screening/meta?coder=${encodeURIComponent(coderName())}`);
    S.criteria = S.meta.criteria || [];
    if (S.meta.available === false) {
      toast("Screening workbook unavailable", "err", S.meta.error || "master_dataset.xlsx");
    }
    if (S.meta.decisions_error) toast("Your screening file cannot be read", "err", S.meta.decisions_error);
    const c = S.meta.counts || {};
    if (c.stale) {
      toast(`${c.stale} saved decision(s) no longer match their row`, "err",
            "The workbook was re-ordered since they were saved; those rows show as unscreened.");
    }
    await loadCriteria();
    const rec = S.meta.missing_recommended || [];
    if (S.meta.available && !S.meta.error && rec.length) {
      toast("Screening workbook: column(s) not found", "err",
        `${rec.join(", ")} — ` + (rec.includes("id")
          ? "records are identified by their row number, so do not re-sort the workbook."
          : "the abstract text will be empty."));
    }
  }

  /* The criteria of the active scheme: they carry the auto-approval
     rules the pre-fill is driven by.  A failure here must not break the
     tab - the criteria simply carry no rules then. */
  async function loadCriteria() {
    try {
      const doc = await api("api/criteria");
      const list = (doc && Array.isArray(doc.abstract)) ? doc.abstract : [];
      const byKey = {};
      list.forEach((c) => { if (c && c.key) byKey[c.key] = c; });
      S.criteriaByKey = byKey;
    } catch (e) {
      S.criteriaByKey = {};
    }
  }

  async function loadList({ keepPos = false } = {}) {
    if (!(await flushDirty())) return;
    const seq = ++S.listSeq;
    const keepId = keepPos && S.cur ? S.cur.id : null;
    const problem = workbookProblem();
    if (problem) { S.rows = []; S.keys = []; S.cur = null; renderProblem(problem); return; }
    const host = $("#screen-list");
    if (host && !S.keys.length) host.innerHTML = `<div class="empty"><b>Loading abstracts…</b><p>${esc(String((S.meta && S.meta.total) || ""))} records in ${esc((S.meta && S.meta.master) || "the workbook")}</p></div>`;
    const q = new URLSearchParams({ coder: coderName(), filter: S.filter, search: S.search });
    const d = await call(`api/screening/list?${q.toString()}`);
    if (seq !== S.listSeq) return;           // a newer filter/search superseded this one
    S.rows = d.rows || [];
    S.byIdMeta = {};
    S.rows.forEach((r) => { S.byIdMeta[r.i] = r; });
    S.keys = S.rows.map((r) => r.i);
    if (keepId && S.keys.includes(keepId)) S.pos = S.keys.indexOf(keepId);
    else if (!keepPos) S.pos = 0;
    if (S.pos < 0 || S.pos >= S.keys.length) S.pos = S.keys.length ? 0 : -1;
    updateProgress();
    renderList();
    if (S.pos >= 0) await selectPos(S.pos, { force: true, skipFlush: true });
    else { S.cur = null; renderForm(); renderViewer(); updateStatus(); }
  }

  /* Save the shown abstract before leaving it, so a quick click elsewhere never
     drops verdicts.  Returns false when the coder chose to stay. */
  async function flushDirty(coder) {
    if (S.savePromise) await S.savePromise.catch(() => {});   // a save already on its way
    if (!S.dirty || !S.cur) return true;
    const ok = await saveCurrent({ coder, quiet: true });
    if (ok) return true;
    return window.confirm("This abstract has unsaved changes that could not be saved.\n\nDiscard them and continue?");
  }

  function prefillFor(rec, human) {
    S.values = blankValues();
    if (human && human.criteria) {
      S.criteria.forEach((c) => {
        const h = human.criteria[c.label] || {};
        S.values[c.label] = {
          verdict: h.verdict || "",
          certainty: h.certainty || "",
          rationale: h.rationale || "",
        };
      });
    }
    // Pre-fill C3 "Yes" when the abstract is detected as English, unless the
    // coder already answered it.  C3 is then skipped when moving past C2.
    S.c3Auto = false;
    if (S.values.C3 && !S.values.C3.verdict && looksEnglish(rec)) {
      S.values.C3.verdict = "Yes";
      S.c3Auto = true;
    }
    // Pre-fill the other gating criteria (C1, C2, C4) from the master
    // dataset's AI verdict, so the coder starts from the AI's suggestion.
    // Never in blind mode (the coder must not see the AI) and never when the
    // source is the decision-free records file (a blinded mirror: no AI there
    // by construction); only with a valid option (the workbook also holds
    // "Unscreened"), and only blank criteria are touched.
    S.aiPrefill = new Set();
    if (aiPrefillAllowed()) {
      S.criteria.forEach((c) => {
        if (c.label === "C3" || c.kind === "classify" || c.gating === false) return;
        const v = S.values[c.label];
        if (!v || v.verdict) return;
        const aiV = String(((rec && rec.ai && rec.ai[c.label]) || {}).verdict || "").trim();
        const opt = (c.options || []).find((o) => o.toLowerCase() === aiV.toLowerCase());
        if (opt) {
          v.verdict = opt;
          S.aiPrefill.add(c.label);
        }
      });
    }
    // Then the criteria's auto-approval rules: a rule that matches the
    // title/abstract approves the criterion (its first pass value is
    // pre-filled and the card says which rule matched); a criterion with rules
    // but no match is pre-selected "Unsure" (the text gives no evidence for
    // it); a criterion WITHOUT rules is not pre-filled.  Only blank criteria
    // are touched: an already-coded or AI-prefilled verdict is never changed.
    S.autoUnsure = new Set();
    S.autoRules = new Map();
    const text = [rec && rec.title, rec && rec.abstract].filter(Boolean).join("\n");
    S.criteria.forEach((c) => {
      if (c.kind === "classify") return;
      const v = S.values[c.label] || (S.values[c.label] = { verdict: "", certainty: "", rationale: "" });
      if (v.verdict) return;
      const rules = ruleSetFor(c);
      if (!Object.keys(rules).length) return;
      const hits = matchRules(rules, text);
      if (hits.length) {
        v.verdict = firstPassValue(c);
        S.autoRules.set(c.label, hits);
      } else {
        v.verdict = "Unsure";
        S.autoUnsure.add(c.label);
      }
    });
  }

  /* AI pre-fill: outside blind mode, with the switch on, and only when the
     tab reads the master workbook (the records file carries no AI verdict,
     and a blinded coder's mirror only has the records file). */
  function aiPrefillAllowed() {
    return !!(S.aiPrefillOn && !S.blind && S.meta && S.meta.source === "master");
  }

  async function selectPos(i, { force = false, skipFlush = false } = {}) {
    if (!S.keys.length) return;
    const pos = Math.max(0, Math.min(S.keys.length - 1, i));
    const uid = S.keys[pos];
    if (!uid) return;
    if (!force && S.cur && S.cur.id === uid) { S.pos = pos; renderList(); return; }
    if (!skipFlush && S.cur && S.cur.id !== uid && !(await flushDirty())) return;
    const seq = ++S.loadSeq;
    S.pos = pos;
    renderList();
    const viewer = $("#screen-viewer");
    if (viewer && (!S.cur || S.cur.id !== uid)) viewer.classList.add("loading");
    try {
      const d = await call(`api/screening/abstract/${encodeURIComponent(uid)}?coder=${encodeURIComponent(coderName())}`);
      if (seq !== S.loadSeq) return;         // the coder already moved on
      S.cur = d.record;
      S.ai = d.record.ai || {};
      S.stale = d.human_stale || null;
      const human = d.human;
      prefillFor(S.cur, human);
      S.dirty = false;
      S.undo = [];
      S.fieldEdits = 0;
      S.cleanDepth = 0;
      S.cleanSaved = !!human;
      S.critIdx = 0;
      renderForm();
      renderViewer();
      updateStatus();
      renderList();
      setSaved(!!human);
      const body = $("#screen-body"); if (body) body.scrollTop = 0;
      if (viewer) viewer.scrollTop = 0;
      focusCurrent();
    } catch (e) {
      if (seq === S.loadSeq) toast("Could not load abstract", "err", String(e.message || e));
    } finally {
      if (viewer && seq === S.loadSeq) viewer.classList.remove("loading");
    }
  }

  // ---------------------------------------------------------------- form
  function renderForm() {
    const host = $("#screen-form");
    if (!host) return;
    if (!S.cur) {
      host.innerHTML = `<p class="side-hint">Select an abstract to screen.</p>`;
      return;
    }
    const rec = S.cur;
    const sub = [rec.authors, rec.year, rec.source, rec.doi].filter(Boolean).join(" · ");
    const cards = S.criteria.map((c, i) => {
      const v = S.values[c.label] || {};
      const opts = c.options.map((o) => {
        const cls = "crit-opt" + (v.verdict === o ? " active v-" + o.toLowerCase() : "");
        return `<button type="button" class="${cls}" data-label="${esc(c.label)}" data-value="${esc(o)}">${esc(o)}</button>`;
      }).join("");
      const certs = ["", "High", "Medium", "Low"].map((o) =>
        `<option value="${o}"${v.certainty === o ? " selected" : ""}>${o || "certainty…"}</option>`).join("");
      const ai = S.ai[c.label] || {};
      const aiLine = (ai.verdict || ai.rationale)
        ? `<div class="crit-ai">AI reference: <b>${esc(ai.verdict || "–")}</b>` +
          `${ai.certainty ? ` (${esc(ai.certainty)})` : ""}` +
          `${ai.rationale ? ` — ${esc(ai.rationale)}` : ""}</div>`
        : "";
      const autoYes = c.label === "C3" && S.c3Auto;
      const autoUnsure = !!(S.autoUnsure && S.autoUnsure.has(c.label));
      const autoRule = (S.autoRules && S.autoRules.get(c.label)) || [];
      const autoAI = S.aiPrefill.has(c.label);
      const autoNote = autoYes
        ? `<div class="crit-auto">Pre-filled <b>Yes</b> - English words detected in the abstract</div>`
        : (autoAI
          ? `<div class="crit-auto">Pre-filled <b>${esc(v.verdict || "")}</b> - from the AI screening verdict</div>`
          : (autoRule.length
            ? `<div class="crit-auto">Pre-filled <b>${esc(v.verdict || "")}</b> - the rule <b>${esc(autoRule[0])}</b> matched the title/abstract</div>`
            : (autoUnsure
              ? `<div class="crit-auto">Pre-filled <b>Unsure</b> - no auto-approval rule matched the title/abstract</div>`
              : "")));
      const verd = String(v.verdict || "").trim().toLowerCase();
      return `<div class="screen-crit crit-${esc(String(c.label).toLowerCase())}${verd ? " v-" + esc(verd) : ""}${(autoYes || autoUnsure || autoRule.length || autoAI) ? " auto" : ""}" data-label="${esc(c.label)}" data-i="${i}">
        <div class="crit-head">
          <span class="crit-tag">${esc(c.label)}</span>
          <span class="crit-title">${esc(c.title)}</span>
          <span class="crit-step">${i + 1} / ${S.criteria.length}</span>
        </div>
        <p class="crit-inst">${esc(c.instruction)}</p>
        <div class="crit-opts">${opts}</div>
        <div class="crit-fields">
          <select class="select crit-cert" data-label="${esc(c.label)}" title="Certainty">${certs}</select>
          <input class="input crit-ratio" data-label="${esc(c.label)}" placeholder="Rationale (optional, under 25 words)" value="${esc(v.rationale || "")}" spellcheck="false">
        </div>
        ${autoNote}
        ${aiLine}
      </div>`;
    }).join("");
    const stale = S.stale
      ? `<div class="screen-warn">A decision saved for this row id (${esc(S.stale.saved_at || "")}) was made on a different title — the workbook was re-ordered since. It is not shown; screen this abstract again.</div>`
      : "";
    const prefillOpt = (S.blind || !S.meta || S.meta.source !== "master") ? "" :
      `<label class="screen-opt" title="Pre-fill blank C1/C2/C4 verdicts from the AI screening verdict (never in blind mode)">` +
      `<input type="checkbox" id="screen-ai-prefill"${S.aiPrefillOn ? " checked" : ""}> pre-fill from AI</label>`;
    host.className = S.blind ? "blind" : "";
    host.innerHTML = `
      <div class="screen-head">
        <div class="screen-title">${esc(plain(rec.title) || "(no title)")}</div>
        <div class="screen-sub">${esc(sub || "—")}${prefillOpt}</div>
      </div>
      ${stale}
      ${cards}`;
    paintFocus();
    decisionPill(currentDecision());
  }

  const V_CLASSES = ["v-yes", "v-no", "v-unsure", "v-inoculation", "v-sourcing", "v-generic", "v-none"];

  function paintVerdict(label) {
    const value = (S.values[label] || {}).verdict || "";
    $$(`.crit-opt[data-label="${cssEsc(label)}"]`).forEach((b) => {
      b.classList.remove("active", ...V_CLASSES);
      if (value && b.dataset.value === value) b.classList.add("active", "v-" + value.toLowerCase());
    });
    const card = $(`.screen-crit[data-label="${cssEsc(label)}"]`);
    if (card) {
      card.classList.remove(...V_CLASSES);
      if (value) card.classList.add("v-" + String(value).toLowerCase());
      const stillAuto = (label === "C3" && S.c3Auto) || S.autoUnsure.has(label)
        || S.aiPrefill.has(label) || S.autoRules.has(label);
      if (!stillAuto) {
        card.classList.remove("auto");
        const note = card.querySelector(".crit-auto");
        if (note) note.remove();
      }
    }
    decisionPill(currentDecision());
  }

  function setVal(label, value) {
    if (!S.values[label]) S.values[label] = { verdict: "", certainty: "", rationale: "" };
    S.undo.push({
      label, verdict: S.values[label].verdict, critIdx: S.critIdx,
      c3Auto: S.c3Auto, autoUnsure: S.autoUnsure.has(label), aiPrefill: S.aiPrefill.has(label),
      autoRules: S.autoRules.get(label) || null,
    });
    if (S.undo.length > 200) S.undo.shift();
    S.values[label].verdict = value;
    if (label === "C3") S.c3Auto = false;   // a manual C3 answer is not skipped
    S.autoUnsure.delete(label);              // a manual choice replaces the pre-fill
    S.aiPrefill.delete(label);
    S.autoRules.delete(label);
    paintVerdict(label);
    markDirty();
  }

  function undoLast() {
    const u = S.undo.pop();
    if (!u) { toast("Nothing to undo", "ok"); return; }
    S.values[u.label].verdict = u.verdict;
    S.c3Auto = u.c3Auto;
    if (u.autoUnsure) S.autoUnsure.add(u.label);
    if (u.aiPrefill) S.aiPrefill.add(u.label);
    if (u.autoRules) S.autoRules.set(u.label, u.autoRules);
    S.critIdx = u.critIdx;
    renderForm();
    focusCurrent();
    // back at the state last loaded or saved: nothing left to save
    if (S.undo.length + S.fieldEdits === S.cleanDepth) { S.dirty = false; setSaved(S.cleanSaved); }
    else markDirty();
  }

  function markDirty({ field = false } = {}) {
    if (field) S.fieldEdits += 1;
    S.dirty = true;
    setSaved(false);
  }

  function cssEsc(s) {
    return String(s).replace(/["\\]/g, "\\$&");
  }

  function paintFocus() {
    $$(".screen-crit").forEach((c) => c.classList.toggle("focus", Number(c.dataset.i) === S.critIdx));
  }

  function focusCurrent() {
    const c = currentCriterion();
    if (!c) return;
    const el = $(`.screen-crit[data-label="${cssEsc(c.label)}"]`);
    S.scrollGuard = true;                    // keep the scroll handler from hijacking this
    clearTimeout(S.scrollTimer);
    S.scrollTimer = setTimeout(() => { S.scrollGuard = false; }, 450);
    if (el) el.scrollIntoView({ block: "nearest" });
    paintFocus();
    if (S.viewer === "abstract") renderViewer();
  }

  // Make the criterion currently being read the active one, so the keyword
  // highlighting follows it (on click, focus or scroll).
  function activateCriterion(i) {
    if (!Number.isFinite(i) || i < 0 || i >= S.criteria.length) return;
    if (S.critIdx === i) return;
    S.critIdx = i;
    paintFocus();
    if (S.viewer === "abstract") renderViewer();
  }

  function acceptCurrent() {
    const c = currentCriterion();
    if (!c) return;
    // Never overwrite a verdict the coder already set: Tab only fills criteria
    // that are still blank (e.g. Shift+Tab back and Tab again, or re-walking an
    // abstract that was screened earlier, must not flip "No"/"Unsure" to "Yes").
    if ((S.values[c.label] || {}).verdict) return;
    if (c.kind === "classify") {
      setVal(c.label, "generic");
    } else if (c.label !== "C3" && !criterionHasRuleEvidence(c)) {
      // Fallback for a blank criterion the load-time pre-selection missed (e.g.
      // its text was not available when the abstract opened): mark it "Unsure"
      // rather than assume the criterion passes.  C3 is exempt (pre-filled
      // "Yes" by the English detector and skipped in the walk).
      setVal(c.label, "Unsure");
    } else {
      setVal(c.label, firstPassValue(c));
    }
  }

  function advance() {
    let next = S.critIdx + 1;
    // C3 is pre-filled from the language detector: after C2 go straight to C4.
    if (S.c3Auto && next < S.criteria.length && S.criteria[next].label === "C3") next += 1;
    if (next <= S.criteria.length - 1) {
      S.critIdx = next;
      focusCurrent();
    } else {
      completeCurrent();
    }
  }

  // Coder mode: after a verdict, jump to the next criterion that is still
  // blank (wrapping around); when none is left, save and load the next
  // abstract.  Mirrors the coding tab's coder-mode auto-advance.
  function nextUnfilled(from) {
    const n = S.criteria.length;
    for (let step = 1; step <= n; step += 1) {
      const i = (((from + step) % n) + n) % n;
      const c = S.criteria[i];
      if (c && !(S.values[c.label] || {}).verdict) return i;
    }
    return -1;
  }

  function advanceUnfilled() {
    const i = nextUnfilled(S.critIdx);
    if (i >= 0) { S.critIdx = i; focusCurrent(); }
    else completeCurrent();   // every criterion is filled: next abstract
  }

  function setAndAdvance(value) {
    const c = currentCriterion();
    if (!c) return;
    setVal(c.label, value);
    if (S.coderMode) advanceUnfilled(); else advance();
  }

  // ---------------------------------------------------------------- save
  function prefilledMap() {
    const out = {};
    S.criteria.forEach((c) => {
      if (c.label === "C3" && S.c3Auto) out.C3 = "language";
      else if (S.aiPrefill.has(c.label)) out[c.label] = "ai";
      else if (S.autoRules.has(c.label)) out[c.label] = "rule";
      else if (S.autoUnsure.has(c.label)) out[c.label] = "keyword";
    });
    return out;
  }

  async function saveCurrent({ advance: doAdvance = false, coder: coderOverride, quiet = false } = {}) {
    if (!S.cur) return false;
    const coder = (coderOverride ?? coderName()).trim();
    if (!coder) {
      toast("Enter your coder name before saving", "err");
      const el = $("#coder-input"); if (el) el.focus();
      return false;
    }
    if (S.saving) return false;
    S.saving = true;
    // capture everything now: the coder may move to another abstract while the
    // request is in flight, and the reply must update THIS record only
    const uid = S.cur.id;
    const values = JSON.parse(JSON.stringify(S.values));
    const editDepth = S.undo.length + S.fieldEdits;
    const persist = async () => {
      const r = await postJson("api/screening/decision", {
        coder, id: uid, criteria: values, prefilled: prefilledMap(),
      });
      if (coder === coderName()) {
        S.dec[uid] = { criteria: values, decision: r.decision, outcome: r.outcome };
        if (S.meta) S.meta.screened = r.screened;
        const meta = S.byIdMeta[uid];
        if (meta) { meta.h = r.outcome; meta.d = 1; }
      }
      const same = !!(S.cur && S.cur.id === uid);
      if (same) {
        // only clean when nothing was edited while the request was in flight
        S.cleanSaved = true;
        S.cleanDepth = editDepth;
        if (S.undo.length + S.fieldEdits === editDepth) { S.dirty = false; setSaved(true); }
        S.stale = null;
        decisionPill(currentDecision());
      }
      updateProgress();
      updateStatus();
      renderList();
      if (quiet) toast("Saved", "ok", plain((S.byIdMeta[uid] || {}).t || uid).slice(0, 80));
      return same;
    };
    let same = false;
    S.savePromise = persist();
    try {
      same = await S.savePromise;
    } catch (e) {
      toast("Save failed", "err", String(e.message || e));
      return false;
    } finally {
      S.saving = false;
      S.savePromise = null;
    }
    if (doAdvance && same) {
      const next = nextPos();
      if (next >= 0) await selectPos(next, { force: true });
      else toast("End of the list", "ok", "No more abstracts in this view.");
    }
    return true;
  }

  function completeCurrent() { return saveCurrent({ advance: true }); }

  function setSaved(on) {
    const btn = $("#save-btn");
    if (!btn) return;
    btn.classList.toggle("save-pending", !on);
    btn.classList.toggle("save-saved", on);
  }

  // ---------------------------------------------------------------- left list
  const isScreened = (uid) => !!(S.dec[uid] || (S.byIdMeta[uid] || {}).d);

  function updateProgress() {
    const el = $("#screen-progress");
    if (!el) return;
    const total = S.meta ? S.meta.total : 0;
    const done = S.meta ? S.meta.screened : 0;
    const pct = total ? Math.min(100, Math.round((done / total) * 1000) / 10) : 0;
    let left = 0;
    S.keys.forEach((uid) => { if (!isScreened(uid)) left += 1; });
    el.innerHTML = `${done.toLocaleString()} / ${total.toLocaleString()} screened (${pct}%)` +
      ` · ${left.toLocaleString()} to screen in view` +
      `<div class="screen-bar"><i style="width:${pct}%"></i></div>`;
  }

  function badgeClass(v) {
    return v === "Include" ? "inc" : (v === "Exclude" ? "exc" : (v === "Unsure" ? "unsure" : ""));
  }

  function renderList() {
    const host = $("#screen-list");
    if (!host) return;
    if (!S.keys.length) {
      host.innerHTML = `<div class="empty">${icon("i-search")}<b>No abstracts match</b><p>Try a different search or filter.</p></div>`;
      return;
    }
    const start = Math.max(0, S.pos - 80);
    const end = Math.min(S.keys.length, start + 220);
    const parts = [];
    if (start > 0) parts.push(`<div class="screen-progress">… ${start} earlier</div>`);
    for (let i = start; i < end; i += 1) {
      const uid = S.keys[i];
      const m = S.byIdMeta[uid] || {};
      const own = S.dec[uid];
      const human = own ? (own.outcome || own.decision || "") : (m.h || "");
      const screened = isScreened(uid);
      const badges = [];
      if (screened) badges.push(`<span class="s-badge human ${badgeClass(human)}">you: ${esc(human || "incomplete")}</span>`);
      if (m.ai && !S.blind) badges.push(`<span class="s-badge ai ${badgeClass(m.ai)}">AI: ${esc(m.ai)}</span>`);
      parts.push(`<button type="button" class="paper-item screen-item${i === S.pos ? " active" : ""}" data-i="${i}">
        <span class="dot${screened ? " coded" : ""}"></span>
        <div class="meta">
          <div class="k">${esc(plain(m.t) || uid)}</div>
          <div class="m"><span class="idx">${i + 1}</span><span>${esc([m.a, m.y].filter(Boolean).join(" · "))}</span></div>
          <div class="s-badges">${badges.join("")}</div>
        </div>
      </button>`);
    }
    if (end < S.keys.length) parts.push(`<div class="screen-progress">… ${S.keys.length - end} more</div>`);
    host.innerHTML = parts.join("");
    const active = host.querySelector(".screen-item.active");
    if (active) active.scrollIntoView({ block: "nearest" });
  }

  // ---------------------------------------------------------------- rule highlighting
  // The focused criterion's auto-approval rules are highlighted in the
  // title and the abstract, so the coder sees WHY the criterion was
  // approved (or which text the rule looked for and did not find).
  // A rule that does not compile highlights nothing - it approves
  // nothing either.
  function ruleSpans(rules, text) {
    const spans = [];
    for (const name of Object.keys(rules || {})) {
      const compiled = compileRule(rules[name]);
      if (!compiled.ok) continue;
      const flags = compiled.regex.flags.indexOf("g") >= 0
        ? compiled.regex.flags : compiled.regex.flags + "g";
      const re = new RegExp(compiled.regex.source, flags);
      let m = re.exec(text);
      while (m !== null) {
        if (!m[0].length) { re.lastIndex += 1; }
        else { spans.push([m.index, m.index + m[0].length]); }
        if (spans.length > 300) break;
        m = re.exec(text);
      }
    }
    return spans;
  }

  function highlightRules(raw) {
    const text = String(raw == null ? "" : raw);
    const c = currentCriterion();
    const rules = ruleSetFor(c);
    if (!text || !Object.keys(rules).length) return esc(text);
    const spans = ruleSpans(rules, text);
    if (!spans.length) return esc(text);
    spans.sort((a, b) => a[0] - b[0]);
    const merged = [];
    for (const span of spans) {
      const last = merged[merged.length - 1];
      if (last && span[0] <= last[1]) last[1] = Math.max(last[1], span[1]);
      else merged.push([span[0], span[1]]);
    }
    let out = "";
    let pos = 0;
    for (const [from, to] of merged) {
      out += esc(text.slice(pos, from));
      out += `<mark class="kw">${esc(text.slice(from, to))}</mark>`;
      pos = to;
    }
    out += esc(text.slice(pos));
    return out;
  }

  // ---------------------------------------------------------------- right viewer
  function renderViewer() {
    const host = $("#screen-viewer");
    if (!host) return;
    if (!S.cur) { host.innerHTML = `<p class="side-hint" style="padding:16px">Select an abstract.</p>`; return; }
    const rec = S.cur;
    const head = `<div class="screen-head"><div class="screen-title">${highlightRules(plain(rec.title) || "(no title)")}</div>` +
      `<div class="screen-sub">${esc([rec.authors, rec.year, rec.source].filter(Boolean).join(" · ") || "—")}</div></div>`;
    if (S.viewer === "meta") {
      const doi = String(rec.doi || "").trim();
      const doiLink = /^10\.\S+$/.test(doi)
        ? `<a href="https://doi.org/${esc(encodeURI(doi))}" target="_blank" rel="noopener noreferrer">${esc(doi)}</a>`
        : esc(doi || "—");
      const rows = [["id", esc(rec.an || rec.id)], ["title", esc(plain(rec.title) || "—")],
                    ["authors", esc(rec.authors || "—")], ["year", esc(rec.year || "—")],
                    ["source", esc(rec.source || "—")], ["doi", doiLink],
                    ["fulltext verdict", esc(rec.fulltext || "—")]];
      host.innerHTML = head + `<dl class="screen-metagrid">` +
        rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("") + `</dl>`;
      return;
    }
    if (S.viewer === "ai") {
      if (S.blind) { host.innerHTML = head + `<div class="screen-blind-note">Blind mode is on — the AI reference is hidden.</div>`; return; }
      const rows = S.criteria.map((c) => {
        const a = S.ai[c.label] || {};
        return `<tr><td>${esc(c.label)} — ${esc(c.title)}</td><td>${esc(a.verdict || "–")}</td>` +
               `<td>${esc(a.certainty || "–")}</td><td>${esc(a.rationale || "")}</td></tr>`;
      }).join("");
      host.innerHTML = head +
        `<p class="screen-ai-note">AI screening reference (read-only). Verdicts are hidden while blind mode is on.</p>` +
        `<table class="an-table"><thead><tr><th>Criterion</th><th>Verdict</th><th>Certainty</th><th>Rationale</th></tr></thead>` +
        `<tbody>${rows}</tbody></table>`;
      return;
    }
    host.innerHTML = head +
      `<div class="screen-abstract"><span class="screen-abs-label">Abstract</span>${highlightRules(plain(rec.abstract) || "(no abstract)")}</div>`;
  }

  function updateStatus() {
    const pos = $("#screen-pos");
    const total = S.meta ? S.meta.total : 0;
    const done = S.meta ? S.meta.screened : 0;
    if (pos) pos.textContent = `${done.toLocaleString()} / ${total.toLocaleString()} coded`;
    const hint = $("#screen-hint");
    if (hint) {
      const at = S.pos >= 0 && S.keys.length ? `${(S.pos + 1).toLocaleString()} of ${S.keys.length.toLocaleString()} in view` : `${S.keys.length} in view`;
      hint.textContent = S.shuffleOn ? `shuffle on · ${at}` : at;
    }
    if (S.cur) decisionPill(currentDecision());
  }

  function setViewer(tab) {
    S.viewer = tab;
    $$("#screen-viewer-seg button").forEach((b) => b.classList.toggle("active", b.dataset.sview === tab));
    renderViewer();
  }

  // ---------------------------------------------------------------- blind
  function applyBlind() {
    const btn = $("#blind-btn");
    if (btn) {
      btn.setAttribute("aria-pressed", S.blind ? "true" : "false");
      btn.classList.toggle("blind-on", S.blind);
    }
    const label = $("#blind-label"); if (label) label.textContent = S.blind ? "Blind on" : "Blind";
    const ic = $("#blind-icon"); if (ic) ic.setAttribute("href", S.blind ? "#i-eye-off" : "#i-eye");
    const form = $("#screen-form"); if (form) form.classList.toggle("blind", S.blind);
    renderList();
    if (S.viewer === "ai") renderViewer();
  }

  function toggleBlind() {
    S.blind = !S.blind;
    // switching blind on must also drop AI pre-fills the coder has not confirmed
    if (S.blind && S.cur && S.aiPrefill.size) {
      S.aiPrefill.forEach((lab) => { if (S.values[lab]) S.values[lab].verdict = ""; });
      S.aiPrefill = new Set();
      // the freed criteria get the rule-based pre-fill they would have had
      const text = abstractText();
      S.criteria.forEach((c) => {
        const v = S.values[c.label];
        if (c.kind === "classify" || !v || v.verdict) return;
        const rules = ruleSetFor(c);
        if (!Object.keys(rules).length) return;
        const hits = matchRules(rules, text);
        if (hits.length) { v.verdict = firstPassValue(c); S.autoRules.set(c.label, hits); }
        else { v.verdict = "Unsure"; S.autoUnsure.add(c.label); }
      });
    }
    applyBlind();
    if (S.cur) renderForm();
  }

  // ---------------------------------------------------------------- shuffle
  function syncShuffle() {
    const btn = $("#shuffle-btn");
    if (btn) {
      btn.setAttribute("aria-pressed", S.shuffleOn ? "true" : "false");
      btn.classList.toggle("primary", S.shuffleOn);
    }
    updateStatus();
  }

  function nextPos() {
    if (S.shuffleOn) {
      const pool = [];
      S.keys.forEach((uid, i) => { if (i !== S.pos && !isScreened(uid)) pool.push(i); });
      if (!pool.length) return -1;
      return pool[Math.floor(Math.random() * pool.length)];
    }
    return S.pos < S.keys.length - 1 ? S.pos + 1 : -1;
  }

  /* the next abstract in the view the coder has not screened (wrapping) */
  function nextUnscreenedPos() {
    const n = S.keys.length;
    for (let step = 1; step <= n; step += 1) {
      const i = (S.pos + step) % n;
      if (!isScreened(S.keys[i])) return i;
    }
    return -1;
  }

  async function gotoNextUnscreened() {
    const i = nextUnscreenedPos();
    if (i < 0) { toast("Everything in this view is screened", "ok"); return; }
    await selectPos(i, { force: true });
  }

  // ---------------------------------------------------------------- merge
  async function mergeToMaster() {
    const coder = coderName();
    if (!coder) { toast("Enter your coder name first", "err"); return; }
    if (!(await flushDirty())) return;
    const n = S.meta ? S.meta.screened : 0;
    if (!n) { toast("Nothing to merge yet", "err", "Screen at least one abstract."); return; }
    const ok = window.confirm(
      `Merge ${n} screened abstract(s) into master_dataset.xlsx?\n\n` +
      "For each screened row this overwrites the C1-C4 verdict/certainty/rationale " +
      "and records Screened_By / Screened_At. A backup is written under archive/. " +
      "(In a project whose master is derived from the decision files, this schedules " +
      "a recompile instead.)");
    if (!ok) return;
    try {
      const r = await postJson("api/screening/merge", { coder });
      if (r && r.compiled) {
        toast("Recompile scheduled", "ok", r.message || "the master is derived from the decision files");
        return;
      }
      toast("Merge started", "ok", `${n} abstract(s)`);
      const btn = $("#merge-btn"); if (btn) btn.disabled = true;
      pollMerge();
    } catch (e) {
      toast("Merge could not start", "err", String(e.message || e));
    }
  }

  function pollMerge() {
    if (S.mergePoll) clearInterval(S.mergePoll);
    let failures = 0;
    const stop = () => {
      clearInterval(S.mergePoll); S.mergePoll = null;
      const btn = $("#merge-btn"); if (btn) btn.disabled = false;
    };
    S.mergePoll = setInterval(async () => {
      let s;
      try { s = await call("api/screening/merge/status"); failures = 0; } catch (e) {
        failures += 1;
        if (failures >= 5) { stop(); toast("Lost track of the merge", "err", String(e.message || e)); }
        return;
      }
      if (s.running) return;
      stop();
      if (s.ok === false) toast("Merge failed", "err", s.error || "");
      else toast("Merged to master", "ok", `${s.matched} row(s) · backup ${s.backup || "written"}`);
      // master changed: reload metadata (server rebuilds its index)
      try { await loadMeta(); await loadList({ keepPos: true }); } catch { /* ignore */ }
    }, 1500);
  }

  // ---------------------------------------------------------------- wiring
  const KEY_HELP = "Tab = accept & next criterion · Y / N / U = Yes / No / Unsure · Shift+Tab = back · " +
    "Ctrl+Z = undo · Alt+↓ / Alt+↑ = next / previous abstract · Ctrl+J = next unscreened · " +
    "Ctrl+S = save · Ctrl+K = search · the last Tab saves and loads the next abstract; " +
    "leaving an abstract with changes saves it";

  async function onCoderChange(name) {
    const prev = S.coder;
    if (S.dirty && S.cur && prev.trim()) {
      // the edits belong to the coder who made them, not the name being typed
      const ok = await saveCurrent({ coder: prev, quiet: true });
      if (!ok) S.dirty = false;
    }
    S.coder = name;
    S.dec = {};
    try { localStorage.setItem("cs.coder", S.coder); } catch { /* ignore */ }
    clearTimeout(S.timer);
    S.timer = setTimeout(async () => {
      try { await loadMeta(); await loadList({ keepPos: true }); }
      catch (e) { toast("Could not load screening for this coder", "err", String(e.message || e)); }
    }, 350);
  }

  function wire() {
    try { S.coder = localStorage.getItem("cs.coder") || ""; } catch { /* ignore */ }
    try { S.coderMode = localStorage.getItem("cs.coderMode") === "1"; } catch { /* ignore */ }
    try { S.aiPrefillOn = localStorage.getItem("cs.screenAiPrefill") !== "0"; } catch { /* ignore */ }
    const coderEl = $("#coder-input");
    if (coderEl && !coderEl.value) coderEl.value = S.coder;
    const keys = $("#screen-body .screen-keys");
    if (keys) keys.textContent = "Criteria whose auto-approval rules match are pre-filled; with rules but no match, Unsure · " + KEY_HELP;

    // viewer tabs
    $$("#screen-viewer-seg button").forEach((b) =>
      b.addEventListener("click", () => setViewer(b.dataset.sview)));

    // filter chips
    $$("#screen-filters .chip").forEach((b) => b.addEventListener("click", () => {
      S.filter = b.dataset.sfilter;
      $$("#screen-filters .chip").forEach((x) => x.classList.toggle("active", x === b));
      loadList().catch((e) => toast("Load failed", "err", String(e.message || e)));
    }));

    // search
    const search = $("#screen-search");
    if (search) search.addEventListener("input", () => {
      clearTimeout(S.timer);
      S.timer = setTimeout(() => {
        S.search = search.value.trim();
        loadList().catch((e) => toast("Load failed", "err", String(e.message || e)));
      }, 250);
    });

    // coder mode (shared top-bar toggle): re-render so the coder hint appears;
    // entry then auto-advances to the next unfilled criterion
    window.addEventListener("cs:codermode", (e) => {
      S.coderMode = !!e.detail;
      if (S.cur) renderForm();
    });

    // coder (shared top-bar field)
    window.addEventListener("cs:coder", (e) => { onCoderChange(String(e.detail || "")); });

    // shuffle (top-bar toggle): when on, Next/Tab advances to a random unscreened abstract
    window.addEventListener("cs:screening-shuffle", (e) => {
      S.shuffleOn = !!(e.detail);
      syncShuffle();
      toast(S.shuffleOn ? "Shuffle on — next is random" : "Shuffle off — next is sequential", "ok");
    });

    // list clicks (delegated)
    $("#screen-list")?.addEventListener("click", (e) => {
      const item = e.target.closest(".screen-item");
      if (item) selectPos(Number(item.dataset.i));
    });

    // form interactions (delegated)
    const form = $("#screen-form");
    if (form) {
      form.addEventListener("click", (e) => {
        const card = e.target.closest(".screen-crit");
        if (card) activateCriterion(Number(card.dataset.i));
        const b = e.target.closest(".crit-opt");
        if (!b) return;
        setVal(b.dataset.label, b.dataset.value);
        const i = S.criteria.findIndex((c) => c.label === b.dataset.label);
        if (i >= 0) S.critIdx = i;
        if (S.coderMode) {
          // coder mode: entering a verdict moves on to the next blank criterion
          advanceUnfilled();
        } else {
          // annotating C2 jumps past the auto-filled C3 straight to C4
          if (i >= 0 && S.c3Auto && b.dataset.label === "C2") {
            const c4 = S.criteria.findIndex((c) => c.label === "C4");
            if (c4 >= 0) S.critIdx = c4;
          }
          if (i >= 0) focusCurrent();
        }
      });
      form.addEventListener("focusin", (e) => {
        const card = e.target.closest(".screen-crit");
        if (card) activateCriterion(Number(card.dataset.i));
      });
      form.addEventListener("change", (e) => {
        if (e.target.id === "screen-ai-prefill") {
          S.aiPrefillOn = !!e.target.checked;
          try { localStorage.setItem("cs.screenAiPrefill", S.aiPrefillOn ? "1" : "0"); } catch { /* ignore */ }
          toast(S.aiPrefillOn ? "AI pre-fill on (from the next abstract)" : "AI pre-fill off (from the next abstract)", "ok");
          return;
        }
        const s = e.target.closest(".crit-cert");
        if (!s) return;
        const v = S.values[s.dataset.label] || (S.values[s.dataset.label] = {});
        v.certainty = s.value;
        markDirty({ field: true });
      });
      form.addEventListener("input", (e) => {
        const t = e.target.closest(".crit-ratio");
        if (!t) return;
        const v = S.values[t.dataset.label] || (S.values[t.dataset.label] = {});
        v.rationale = t.value;
        markDirty({ field: true });
      });
    }

    // follow the criterion currently in view so the keyword highlighting follows
    const body = $("#screen-body");
    if (body) {
      let raf = 0;
      body.addEventListener("scroll", () => {
        if (S.scrollGuard || raf) return;
        raf = requestAnimationFrame(() => {
          raf = 0;
          if (S.scrollGuard) return;
          const cards = $$(".screen-crit", body);
          if (!cards.length) return;
          const top = body.getBoundingClientRect().top + 12;
          let best = null, bestD = Infinity;
          cards.forEach((el) => {
            const d = Math.abs(el.getBoundingClientRect().top - top);
            if (d < bestD) { bestD = d; best = el; }
          });
          if (best) activateCriterion(Number(best.dataset.i));
        });
      }, { passive: true });
    }

    // actions (driven from the shared top bar)
    window.addEventListener("cs:screening-save", () => saveCurrent({ advance: false }));
    window.addEventListener("cs:screening-merge", () => mergeToMaster());
    window.addEventListener("cs:screening-blind", () => toggleBlind());

    // keyboard (capture so the browser's default Tab is replaced by our walk)
    window.addEventListener("keydown", async (e) => {
      const view = $("#view-screening");
      if (!view || view.hidden) return;
      const t = e.target;
      const inField = t && (/^(input|textarea|select)$/i.test(t.tagName) || t.isContentEditable);
      const meta = e.ctrlKey || e.metaKey;
      if (meta) {
        const k = e.key.toLowerCase();
        if (k === "s") { e.preventDefault(); e.stopImmediatePropagation(); saveCurrent({ advance: false }); }
        else if (k === "b") { e.preventDefault(); e.stopImmediatePropagation(); toggleBlind(); }
        else if (k === "k") {
          e.preventDefault(); e.stopImmediatePropagation();
          const s = $("#screen-search"); if (s) { s.focus(); s.select(); }
        } else if (k === "j") {
          e.preventDefault(); e.stopImmediatePropagation(); gotoNextUnscreened();
        } else if (k === "z" && !e.shiftKey && !inField) {
          e.preventDefault(); e.stopImmediatePropagation(); undoLast();
        }
        return;
      }
      if (e.altKey && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
        e.preventDefault(); e.stopImmediatePropagation();
        if (e.repeat) return;
        const next = S.pos + (e.key === "ArrowDown" ? 1 : -1);
        if (next >= 0 && next < S.keys.length) await selectPos(next);
        return;
      }
      const keepNativeTab = inField && !t.classList.contains("crit-ratio") && !t.classList.contains("crit-cert");
      if (e.key === "Tab") {
        if (keepNativeTab) return;              // normal focus movement in search/coder/count
        e.preventDefault();
        e.stopImmediatePropagation();
        // a held-down Tab would walk through and save abstract after abstract
        if (e.repeat) return;
        if (e.shiftKey) {
          if (S.critIdx > 0) { S.critIdx -= 1; focusCurrent(); }
          else if (S.pos > 0) { await selectPos(S.pos - 1, { force: true }); }
          else { toast("First abstract", "ok"); }
        } else { acceptCurrent(); if (S.coderMode) advanceUnfilled(); else advance(); }
        return;
      }
      if (inField || e.altKey || e.repeat) return;
      if (e.key === "y" || e.key === "Y") { e.preventDefault(); setAndAdvance("Yes"); }
      else if (e.key === "n" || e.key === "N") { e.preventDefault(); setAndAdvance("No"); }
      else if (e.key === "u" || e.key === "U") { e.preventDefault(); setAndAdvance("Unsure"); }
      else if (e.key >= "1" && e.key <= "4") {
        const c = currentCriterion();
        if (c && c.kind === "classify" && c.options[Number(e.key) - 1]) {
          e.preventDefault(); setAndAdvance(c.options[Number(e.key) - 1]);
        }
      }
    }, true);
  }

  async function boot() {
    if (!S.wired) { S.wired = true; wire(); }
    await loadMeta();
    await loadList();
    applyBlind();
    syncShuffle();
  }

  async function ensureStarted() {
    if (S.bootPromise) return S.bootPromise;
    S.started = true;
    S.bootPromise = boot()
      .then(() => { S.booted = true; })
      .catch((e) => {
        const host = $("#screen-list");
        if (host) host.innerHTML = `<div class="empty">${icon("i-alert")}<b>Could not load screening</b><p>${esc(e.message || e)}</p></div>`;
        S.bootPromise = null;                // let the next visit retry
      });
    return S.bootPromise;
  }

  // Open a specific record in abstract screening (driven from the Agreement tab).
  async function openUid(uid) {
    const id = String(uid || "").trim();
    if (!id) return;
    await ensureStarted();
    if (!S.booted) return;
    if (!(await flushDirty())) return;
    // the record may be hidden behind the active filter/search: reset to "all"
    S.filter = "all";
    S.search = "";
    const search = $("#screen-search"); if (search) search.value = "";
    $$("#screen-filters .chip").forEach((x) =>
      x.classList.toggle("active", x.dataset.sfilter === "all"));
    await loadList();
    const i = S.keys.indexOf(id);
    if (i >= 0) await selectPos(i, { force: true });
    else toast("Study not found in screening", "err", id);
  }

  window.addEventListener("cs:screen-open", (e) => {
    const uid = e.detail && (e.detail.uid || e.detail.id);
    const tab = document.querySelector('.viewtab[data-view="screening"]');
    if (tab && !tab.classList.contains("active")) tab.click();
    openUid(uid).catch((err) => toast("Could not open the abstract", "err", String(err.message || err)));
  });

  document.addEventListener("cs:view", (e) => {
    if (e.detail !== "screening") {
      // leaving the tab: keep what was entered
      if (S.dirty && S.cur && coderName()) saveCurrent({ quiet: true });
      return;
    }
    try { S.coderMode = localStorage.getItem("cs.coderMode") === "1"; } catch { /* ignore */ }
    ensureStarted().then(() => { if (S.booted) { applyBlind(); syncShuffle(); if (S.cur) renderForm(); } });
  });
  function boot2() {
    let saved = "coding";
    try { saved = localStorage.getItem("cs.view") || "coding"; } catch { /* ignore */ }
    if (saved === "screening") ensureStarted();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot2);
  else boot2();
})();
