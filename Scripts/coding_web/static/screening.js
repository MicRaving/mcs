/* ============================================================
   Coding Studio — Abstract screening tab
   Human abstract screening against the inclusion criteria.

   The criteria carry an **auto-approval rule set**: a dictionary of
   named regular expressions that approve the criterion when the
   abstract text matches.  A rule that matches pre-fills the
   criterion's first pass value and the card says WHICH rule matched;
   a criterion with no rules is never pre-filled.  The rules come from
   the scheme's criteria (/api/criteria) - nothing is hardcoded here
   any more.

   Inclusion is driven by the Tab key: each Tab accepts the focused
   criterion (leaving an existing verdict as-is) and advances; on the
   last criterion the next Tab saves the record and loads the next
   abstract in the same place.
   N = No, U = Unsure, Shift+Tab = back.  Every completed abstract is
   written to Screening/<coder>.json immediately; "Merge to master"
   writes the coder's decisions into master_dataset.xlsx.

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
    dec: {},            // human decisions already on disk for this coder
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
    coderMode: false,   // mirrors the coding tab's coder mode: auto-advance to the next unfilled
    shuffleOn: false,
    viewer: "abstract",
    saving: false,
    timer: null,
    mergePoll: null,
    bootPromise: null,
    booted: false,
    scrollGuard: false,
    scrollTimer: null,
  };

  // ---------------------------------------------------------------- helpers
  const currentCriterion = () => S.criteria[S.critIdx] || null;

  const blankValues = () => {
    const v = {};
    S.criteria.forEach((c) => { v[c.label] = { verdict: "", certainty: "", rationale: "" }; });
    return v;
  };

  function currentDecision() {
    const verdicts = ["C1", "C2", "C3", "C4"].map((l) => (S.values[l] || {}).verdict || "");
    if (verdicts.some((v) => v === "No")) return "Exclude";
    if (verdicts.every((v) => v === "Yes")) return "Include";
    return "";
  }

  function decisionPill(value) {
    const el = $("#screen-decision");
    if (!el) return;
    if (value === "Include") { el.textContent = "Include"; el.className = "pill inc"; }
    else if (value === "Exclude") { el.textContent = "Exclude"; el.className = "pill exc"; }
    else { el.textContent = "Incomplete"; el.className = "pill warn"; }
  }

  function coderName() {
    return (S.coder || "").trim();
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
  async function loadMeta() {
    S.meta = await api(`/api/screening/meta?coder=${encodeURIComponent(coderName())}`);
    S.criteria = S.meta.criteria || [];
    await loadCriteria();
  }

  /* The criteria of the active scheme: they carry the auto-approval
     rules the pre-fill is driven by.  A failure here must not break the
     tab - the criteria simply carry no rules then. */
  async function loadCriteria() {
    try {
      const doc = await api("/api/criteria");
      const list = (doc && Array.isArray(doc.abstract)) ? doc.abstract : [];
      const byKey = {};
      list.forEach((c) => { if (c && c.key) byKey[c.key] = c; });
      S.criteriaByKey = byKey;
    } catch (e) {
      S.criteriaByKey = {};
    }
  }

  async function loadList({ keepPos = false } = {}) {
    const q = new URLSearchParams({ coder: coderName(), filter: S.filter, search: S.search });
    const d = await api(`/api/screening/list?${q.toString()}`);
    S.rows = d.rows || [];
    S.byIdMeta = {};
    S.rows.forEach((r) => { S.byIdMeta[r.i] = r; });
    S.keys = S.rows.map((r) => r.i);
    if (!keepPos) S.pos = 0;
    if (S.pos < 0 || S.pos >= S.keys.length) S.pos = S.keys.length ? 0 : -1;
    updateProgress();
    renderList();
    if (S.pos >= 0) await selectPos(S.pos, { force: true });
    else { S.cur = null; renderForm(); renderViewer(); updateStatus(); }
  }

  async function selectPos(i, { force = false } = {}) {
    if (!S.keys.length) return;
    S.pos = Math.max(0, Math.min(S.keys.length - 1, i));
    const uid = S.keys[S.pos];
    if (!uid) return;
    if (!force && S.cur && S.cur.id === uid) { renderList(); return; }
    try {
      const d = await api(`/api/screening/abstract/${encodeURIComponent(uid)}?coder=${encodeURIComponent(coderName())}`);
      S.cur = d.record;
      S.ai = d.record.ai || {};
      S.values = blankValues();
      const human = d.human;
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
      if (!S.values.C3.verdict && looksEnglish(S.cur)) {
        S.values.C3.verdict = "Yes";
        S.c3Auto = true;
      }
      // Pre-fill from the criteria's auto-approval rules: a rule that
      // matches the title/abstract approves the criterion, so its first
      // pass value is pre-filled and the card says which rule matched.  A
      // criterion with rules but no match is pre-selected "Unsure" (the
      // text gives no evidence for it); a criterion WITHOUT rules is not
      // pre-filled at all.  Only blank criteria are touched: an
      // already-coded verdict is never changed.  C3 is handled by the
      // English detector above.
      S.autoUnsure = new Set();
      S.autoRules = new Map();
      const text = abstractText();
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
      S.critIdx = 0;
      renderForm();
      renderViewer();
      updateStatus();
      renderList();
      setSaved(!!human);
      const body = $("#screen-body"); if (body) body.scrollTop = 0;
      const viewer = $("#screen-viewer"); if (viewer) viewer.scrollTop = 0;
      focusCurrent();
    } catch (e) {
      toast("Could not load abstract", "err", String(e.message || e));
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
      const autoNote = autoYes
        ? `<div class="crit-auto">Pre-filled <b>Yes</b> — English words detected in the abstract</div>`
        : (autoRule.length
          ? `<div class="crit-auto">Pre-filled <b>${esc(v.verdict || "")}</b> — the rule <b>${esc(autoRule[0])}</b> matched the title/abstract</div>`
          : (autoUnsure
            ? `<div class="crit-auto">Pre-filled <b>Unsure</b> — no auto-approval rule matched the title/abstract</div>`
            : ""));
      const verd = String(v.verdict || "").trim().toLowerCase();
      return `<div class="screen-crit crit-${esc(String(c.label).toLowerCase())}${verd ? " v-" + esc(verd) : ""}${(autoYes || autoUnsure || autoRule.length) ? " auto" : ""}" data-label="${esc(c.label)}" data-i="${i}">
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
    host.className = S.blind ? "blind" : "";
    host.innerHTML = `
      <div class="screen-head">
        <div class="screen-title">${esc(rec.title || "(no title)")}</div>
        <div class="screen-sub">${esc(sub || "—")}</div>
      </div>
      ${cards}`;
    paintFocus();
    decisionPill(currentDecision());
  }

  function setVal(label, value) {
    if (!S.values[label]) S.values[label] = { verdict: "", certainty: "", rationale: "" };
    S.values[label].verdict = value;
    if (label === "C3") S.c3Auto = false;   // a manual C3 answer is not skipped
    if (S.autoUnsure) S.autoUnsure.delete(label);   // a manual choice replaces the pre-fill
    const btn = $(`.crit-opt[data-label="${cssEsc(label)}"][data-value="${cssEsc(value)}"]`);
    $$(`.crit-opt[data-label="${cssEsc(label)}"]`).forEach((b) => b.classList.remove("active", "v-yes", "v-no", "v-unsure", "v-inoculation", "v-sourcing", "v-generic", "v-none"));
    if (btn) btn.classList.add("active", "v-" + value.toLowerCase());
    const card = $(`.screen-crit[data-label="${cssEsc(label)}"]`);
    if (card) {
      card.classList.remove("v-yes", "v-no", "v-unsure", "v-inoculation", "v-sourcing", "v-generic", "v-none");
      card.classList.add("v-" + String(value).toLowerCase());
    }
    // the verdict is now the coder's own: drop any "pre-filled" note/marker
    const stillAuto = (label === "C3" && S.c3Auto)
      || (S.autoUnsure && S.autoUnsure.has(label))
      || (S.autoRules && S.autoRules.has(label));
    if (card && !stillAuto) {
      card.classList.remove("auto");
      const note = card.querySelector(".crit-auto");
      if (note) note.remove();
    }
    decisionPill(currentDecision());
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
  async function saveCurrent({ advance: doAdvance = false } = {}) {
    if (!S.cur) return false;
    const coder = coderName();
    if (!coder) {
      toast("Enter your coder name before saving", "err");
      const el = $("#coder-input"); if (el) el.focus();
      return false;
    }
    if (S.saving) return false;
    S.saving = true;
    try {
      const r = await api("/api/screening/decision", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ coder, id: S.cur.id, criteria: S.values }),
      });
      S.dec[S.cur.id] = { criteria: JSON.parse(JSON.stringify(S.values)), decision: r.decision };
      if (S.meta) S.meta.screened = r.screened;
      const meta = S.byIdMeta[S.cur.id];
      if (meta) { meta.h = r.decision; }
      updateProgress();
      renderList();
      setSaved(true);
      decisionPill(r.decision);
      if (doAdvance) {
        const next = nextPos();
        if (next >= 0) await selectPos(next, { force: true });
        else toast("End of the list", "ok", "No more abstracts in this view.");
      }
      return true;
    } catch (e) {
      toast("Save failed", "err", String(e.message || e));
      return false;
    } finally {
      S.saving = false;
    }
  }

  function completeCurrent() { return saveCurrent({ advance: true }); }

  function setSaved(on) {
    const btn = $("#save-btn");
    if (!btn) return;
    btn.classList.toggle("save-pending", !on);
    btn.classList.toggle("save-saved", on);
  }

  // ---------------------------------------------------------------- left list
  function updateProgress() {
    const el = $("#screen-progress");
    if (!el) return;
    const total = S.meta ? S.meta.total : 0;
    const done = S.meta ? S.meta.screened : 0;
    const pct = total ? Math.round((done / total) * 1000) / 10 : 0;
    el.innerHTML = `${done.toLocaleString()} / ${total.toLocaleString()} screened` +
      `<div class="screen-bar"><i style="width:${pct}%"></i></div>`;
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
      const human = (S.dec[uid] && S.dec[uid].decision) || m.h || "";
      const badges = [];
      if (human) badges.push(`<span class="s-badge human ${human === "Include" ? "inc" : "exc"}">you: ${esc(human)}</span>`);
      if (m.ai && !S.blind) badges.push(`<span class="s-badge ai ${m.ai === "Include" ? "inc" : (m.ai === "Exclude" ? "exc" : "")}">AI: ${esc(m.ai)}</span>`);
      parts.push(`<button type="button" class="paper-item screen-item${i === S.pos ? " active" : ""}" data-i="${i}">
        <span class="dot${human ? " coded" : ""}"></span>
        <div class="meta">
          <div class="k">${esc(m.t || uid)}</div>
          <div class="m"><span class="idx">${i + 1}</span><span>${esc([m.a, m.y].filter(Boolean).join(" · "))}</span></div>
          <div class="s-badges">${badges.join("")}</div>
        </div>
      </button>`);
    }
    if (end < S.keys.length) parts.push(`<div class="screen-progress">… ${S.keys.length - end} more</div>`);
    host.innerHTML = parts.join("");
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
    const head = `<div class="screen-head"><div class="screen-title">${highlightRules(rec.title || "(no title)")}</div>` +
      `<div class="screen-sub">${esc([rec.authors, rec.year, rec.source].filter(Boolean).join(" · ") || "—")}</div></div>`;
    if (S.viewer === "meta") {
      const rows = [["id", rec.an || rec.id], ["title", rec.title], ["authors", rec.authors],
                    ["year", rec.year], ["source", rec.source], ["doi", rec.doi],
                    ["fulltext verdict", rec.fulltext || "—"]];
      host.innerHTML = head + `<dl class="screen-metagrid">` +
        rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v || "—")}</dd>`).join("") + `</dl>`;
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
      `<div class="screen-abstract"><span class="screen-abs-label">Abstract</span>${highlightRules(rec.abstract || "(no abstract)")}</div>`;
  }

  function updateStatus() {
    const pos = $("#screen-pos");
    const total = S.meta ? S.meta.total : 0;
    const done = S.meta ? S.meta.screened : 0;
    if (pos) pos.textContent = `${done.toLocaleString()} / ${total.toLocaleString()} coded`;
    const hint = $("#screen-hint");
    if (hint) hint.textContent = S.shuffleOn ? `shuffle on · ${S.keys.length} in view` : `${S.keys.length} in view`;
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
      const pool = S.keys.map((uid, i) => i)
        .filter((i) => i !== S.pos && !S.dec[S.keys[i]]);
      if (!pool.length) return -1;
      return pool[Math.floor(Math.random() * pool.length)];
    }
    return S.pos < S.keys.length - 1 ? S.pos + 1 : -1;
  }

  // ---------------------------------------------------------------- merge
  async function mergeToMaster() {
    const coder = coderName();
    if (!coder) { toast("Enter your coder name first", "err"); return; }
    const n = S.meta ? S.meta.screened : 0;
    if (!n) { toast("Nothing to merge yet", "err", "Screen at least one abstract."); return; }
    const ok = window.confirm(
      `Merge ${n} screened abstract(s) into master_dataset.xlsx?\n\n` +
      "For each screened row this overwrites the C1-C4 verdict/certainty/rationale " +
      "and records Screened_By / Screened_At. A backup is written under archive/. " +
      "This cannot be undone from the app.");
    if (!ok) return;
    try {
      const r = await api("/api/screening/merge", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ coder }),
      });
      toast("Merge started", "ok", `${n} abstract(s)`);
      const btn = $("#merge-btn"); if (btn) btn.disabled = true;
      pollMerge();
    } catch (e) {
      toast("Merge could not start", "err", String(e.message || e));
    }
  }

  function pollMerge() {
    if (S.mergePoll) clearInterval(S.mergePoll);
    S.mergePoll = setInterval(async () => {
      let s;
      try { s = await api("/api/screening/merge/status"); } catch { return; }
      if (s.running) return;
      clearInterval(S.mergePoll); S.mergePoll = null;
      const btn = $("#merge-btn"); if (btn) btn.disabled = false;
      if (s.ok === false) toast("Merge failed", "err", s.error || "");
      else toast("Merged to master", "ok", `${s.matched} row(s) · backup ${s.backup || "written"}`);
      // master changed: reload metadata (server rebuilds its index)
      try { await loadMeta(); await loadList({ keepPos: true }); } catch { /* ignore */ }
    }, 1500);
  }

  // ---------------------------------------------------------------- wiring
  async function boot() {
    try { S.coder = localStorage.getItem("cs.coder") || ""; } catch { /* ignore */ }
    try { S.coderMode = localStorage.getItem("cs.coderMode") === "1"; } catch { /* ignore */ }
    const coderEl = $("#coder-input");
    if (coderEl && !coderEl.value) coderEl.value = S.coder;

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
    window.addEventListener("cs:coder", (e) => {
      S.coder = String(e.detail || "");
      try { localStorage.setItem("cs.coder", S.coder); } catch { /* ignore */ }
      clearTimeout(S.timer);
      S.timer = setTimeout(() => loadList({ keepPos: true }).catch(() => {}), 350);
    });

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
        const s = e.target.closest(".crit-cert");
        if (!s) return;
        const v = S.values[s.dataset.label] || (S.values[s.dataset.label] = {});
        v.certainty = s.value;
        setSaved(false);
      });
      form.addEventListener("input", (e) => {
        const t = e.target.closest(".crit-ratio");
        if (!t) return;
        const v = S.values[t.dataset.label] || (S.values[t.dataset.label] = {});
        v.rationale = t.value;
        setSaved(false);
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
    window.addEventListener("cs:screening-blind", () => { S.blind = !S.blind; applyBlind(); });

    // keyboard (capture so the browser's default Tab is replaced by our walk)
    window.addEventListener("keydown", async (e) => {
      const view = $("#view-screening");
      if (!view || view.hidden) return;
      const t = e.target;
      const meta = e.ctrlKey || e.metaKey;
      if (meta) {
        const k = e.key.toLowerCase();
        if (k === "s") { e.preventDefault(); e.stopImmediatePropagation(); saveCurrent({ advance: false }); }
        else if (k === "b") { e.preventDefault(); e.stopImmediatePropagation(); S.blind = !S.blind; applyBlind(); }
        else if (k === "k") {
          e.preventDefault(); e.stopImmediatePropagation();
          const s = $("#screen-search"); if (s) { s.focus(); s.select(); }
        }
        return;
      }
      const inField = t && /^(input|textarea|select)$/i.test(t.tagName);
      const keepNativeTab = inField && !t.classList.contains("crit-ratio") && !t.classList.contains("crit-cert");
      if (e.key === "Tab") {
        if (keepNativeTab) return;              // normal focus movement in search/coder/count
        e.preventDefault();
        e.stopImmediatePropagation();
        if (e.shiftKey) {
          if (S.critIdx > 0) { S.critIdx -= 1; focusCurrent(); }
          else if (S.pos > 0) { await selectPos(S.pos - 1, { force: true }); }
          else { toast("First abstract", "ok"); }
        } else { acceptCurrent(); if (S.coderMode) advanceUnfilled(); else advance(); }
        return;
      }
      if (inField) return;
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
      });
    return S.bootPromise;
  }

  // Open a specific record in abstract screening (driven from the Agreement tab).
  async function openUid(uid) {
    const id = String(uid || "").trim();
    if (!id) return;
    await ensureStarted();
    if (!S.booted) return;
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
    openUid(uid).catch(() => {});
  });

  document.addEventListener("cs:view", (e) => {
    if (e.detail !== "screening") return;
    try { S.coderMode = localStorage.getItem("cs.coderMode") === "1"; } catch { /* ignore */ }
    ensureStarted().then(() => { if (S.started) { applyBlind(); syncShuffle(); if (S.cur) renderForm(); } });
  });
  function boot2() {
    let saved = "coding";
    try { saved = localStorage.getItem("cs.view") || "coding"; } catch { /* ignore */ }
    if (saved === "screening") ensureStarted();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot2);
  else boot2();
})();
