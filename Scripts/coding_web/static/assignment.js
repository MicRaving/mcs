/* ============================================================
   Coding Studio — Gap workspace (Harmonization tab, redesigned)

   The guided queue for ONE study: it walks the study's open gaps ONE
   CARD AT A TIME — one plain question per card, a progress bar ("9
   gaps left, then this study joins the pools"), previous answers as
   collapsed cards — in a three-column layout:

     (a) "What this study is"   facts from the coding tab + notes +
                                 the full paper one click away
     (b) "Gaps to label"        the current card with evidence blocks
                                 (PAPER / CODING / HARMONIZER GUESS),
                                 coding pre-fills that just need a
                                 CONFIRM, candidates for column
                                 questions, and the renamed actions
                                 (Confirm / Undo / Mark N-A /
                                 Split into two variables / Decode)
     (c) "Raw data + what this unlocks"
                                 the column preview that highlights
                                 whatever is typed in the answer box,
                                 the eleven-question checklist and
                                 the three pool pills

   Saving is one sweep, exactly as before: every Confirm stages an
   assignment row in memory and the top-row **Save & apply** posts all
   of them to POST /api/assignments/<key>, which writes the canonical
   `Datasets/<Key>/<Key> - variable_assignments.csv` and regenerates
   the harmonized sidecar/wide (variable_assignments.write_rows +
   apply_files).  No data contracts changed.

   Endpoints:
     GET  /api/gapwalk/<key>          the guided queue (questions,
                                      evidence, pre-fills, candidates)
     GET  /api/assignments/<key>      applied rows + drafts
     POST /api/assignments/<key>      save every staged row to the dataset
     GET  /api/rawvar/<key>           column context + dataset preview
     GET  /api/harmonizer/decode      live decode preview
     GET  /api/pdfsearch/<key>        page lookup for a paper quote

   Exposes `window.CSAssign`: open(key, slot), close(), mount(el).
   ============================================================ */
(() => {
  "use strict";

  const { $, $$, esc, api, toast } = window.CS;
  const enc = encodeURIComponent;

  const DEFAULT_OPTIONS = {
    role: ["dv", "covariate", ""],
    dv_domain: ["credibility", "sharing_intention", "belief", "trust",
      "media_literacy_attitudes", "technique_recognition", "other", ""],
    veracity: ["true", "false", ""],
    timepoint: ["pre", "post", "followup", ""],
    dv_polarity: ["higher_is_better", "higher_is_worse", "ambiguous", ""],
  };

  const ROLE_LABEL = {
    dv_domain: "What it measures", veracity: "True or false stimuli",
    timepoint: "When measured", dv_polarity: "Is a higher number better?",
    role: "Kind of variable", uniform_variable: "Uniform name",
  };
  const SLOT_FIELD = { S5: "dv_domain", S6: "veracity", S7: "timepoint", S8: "dv_polarity" };
  const ARM_ROLES = ["intervention", "control", "active_control", "other"];
  const ARM_ROLE_HINT = {
    intervention: "this arm receives the misinformation intervention",
    control: "a no-misinformation-intervention comparison arm",
    active_control: "an arm that itself delivers a competing intervention",
    other: "neither (e.g. a within-study shorthand the paper calls control)",
  };

  const state = {
    key: null, coder: "", host: null, token: 0, mounted: false,
    data: null, options: DEFAULT_OPTIONS,
    queue: [], rawCards: [], idx: 0,
    session: {}, pending: {}, applied: [],
    context: null, contextLoading: false, decode: null,
    open: false, busy: false, startedAt: 0,
    /* online edit lock of the open study (its assignments/harmonized files are
       shared): held while the workspace is open, renewed by online.js */
    lockKey: "", locked: null,
  };

  // --------------------------------------------------------------- coder
  function readCoder() {
    try { return (localStorage.getItem("cs.coder") || "").trim(); } catch { return ""; }
  }

  // ----------------------------------------------------------- row keys
  function normArtifact(a) { return (!a || a === "*") ? "*" : String(a); }
  function draftKey(r) {
    return [normArtifact(r.artifact), r.original_variable || "", r.item_scope || ""].join("\u0001");
  }
  function stagedRows() { return Object.values(state.pending || {}); }
  function isStaged(key) {
    return Object.prototype.hasOwnProperty.call(state.pending, key);
  }

  // -------------------------------------------------------- card list
  function answeredSessionCards() {
    return state.queue.filter((c) => state.session[c.id]);
  }
  function pendingCards() {
    return state.queue.filter((c) => !state.session[c.id]);
  }
  function currentCard() {
    const pend = pendingCards();
    if (!pend.length) return null;
    return pend[Math.min(state.idx, pend.length - 1)] || pend[0];
  }
  function firstCardOfSlot(slot) {
    const i = pendingCards().findIndex((c) => c.slot === slot);
    return i < 0 ? null : i;
  }

  // =============================================================== panes
  function progressHtml() {
    const left = pendingCards().length;
    const done = answeredSessionCards().length + (state.data.counts.answered || 0);
    if (state.data.warning) {
      // the warning banner tells the story; no progress bar for it
      return "";
    }
    if (!state.queue.length && !(state.data.counts.answered || 0)) {
      return `<div class="gw-progress"><span class="gw-progress-note">` +
        `Nothing to label — every question for this study is already answered.</span></div>`;
    }
    const total = left + done || 1;
    const pct = Math.round((done / total) * 100);
    return `<div class="gw-progress">` +
      `<div class="gw-progress-head">` +
      `<b>${esc(left)} gap${left === 1 ? "" : "s"} left</b>, then this study joins the pools` +
      `<span class="gw-progress-done">${esc(done)} answered</span></div>` +
      `<div class="gw-bar" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100">` +
      `<div class="gw-bar-fill" style="width:${pct}%"></div></div>` +
      `<span class="gw-progress-note">answers are applied to the dataset on save; ` +
      `the queue re-ranks the next time the coverage pipeline runs</span>` +
      `</div>`;
  }

  // --------------------------------------------------- pane (a): study
  function studyPaneHtml() {
    const d = state.data;
    const facts = (d.facts || [])[0] || {};
    const parts = [];
    parts.push(`<div class="gw-pane-h">What this study is</div>`);
    if (facts.title) parts.push(`<div class="gw-fact-t">${esc(facts.title)}</div>`);
    const meta = [
      facts.authors && `${facts.authors}${facts.year ? " (" + facts.year + ")" : ""}`,
      facts.n_analyzed != null && facts.n_analyzed !== "" && `N analyzed ${facts.n_analyzed}`,
      facts.n_arms && `${facts.n_arms} arm${facts.n_arms === 1 ? "" : "s"}`,
      facts.control_group_label,
      facts.intervention_types && `intervention: ${facts.intervention_types}`,
      facts.setting && `setting: ${facts.setting}`,
    ].filter(Boolean);
    if (meta.length) {
      parts.push(`<div class="gw-fact-meta">${meta.map((m) => `<span>${esc(m)}</span>`).join("")}</div>`);
    }
    if (facts.description) {
      parts.push(`<div class="gw-fact-desc">${esc(facts.description)}</div>`);
    }
    // the coded outcomes: the DV rows a coder already answered in the Coding tab
    const dvs = (facts.dvs || []).slice(0, 24);
    if (dvs.length) {
      parts.push(`<div class="ctx-block"><div class="ctx-h" ` +
        `title="What the Coding tab already records for this study's outcome measures">` +
        `Coded outcomes (${esc(dvs.length)})</div>` +
        dvs.map((dv) =>
          `<div class="gw-dv-row" title="${esc(dv.name)}">` +
          `<span class="gw-dv-name">${esc(dv.name)}</span>` +
          `<span class="gw-dv-tags">` +
          (dv.domain ? `<span class="gw-dv-tag">${esc(dv.domain)}</span>` : "") +
          (dv.timepoint_1 ? `<span class="gw-dv-tag">${esc(dv.timepoint_1)}</span>` : "") +
          (dv.polarity !== null && dv.polarity !== "" && dv.polarity !== undefined
            ? `<span class="gw-dv-tag">${esc(dv.polarity === 1 || dv.polarity === "1" ? "higher = better" : "higher = worse")}</span>` : "") +
          `</span></div>`).join("") + `</div>`);
    }
    if (d.notes) {
      parts.push(`<details class="gw-notes"><summary>Coding notes</summary>` +
        `<pre class="gw-notes-pre">${esc(d.notes.slice(0, 2400))}</pre></details>`);
    }
    parts.push(`<div class="gw-paper-actions">` +
      `<button type="button" class="btn ghost" id="gw-open-pdf" ` +
      `title="Open the paper in the built-in PDF viewer (the quote's page is located automatically)">` +
      `Open the paper (PDF)</button>` +
      `<button type="button" class="btn ghost" id="gw-open-coding" ` +
      `title="Open this study in the Coding tab">Open coding tab</button></div>`);
    return parts.join("");
  }

  // ------------------------------------------- evidence blocks on cards
  function quoteHtml(card) {
    const quotes = card.paper_quotes || [];
    if (!quotes.length) return "";
    return `<div class="gw-evidence gw-ev-paper">` +
      `<span class="gw-ev-tag" title="A passage the server found in the paper's extracted text (Text/&lt;Key&gt;.txt) using this question's search terms">PAPER</span>` +
      quotes.map((q) =>
        `<div class="gw-quote">` +
        `<span class="gw-quote-line">line ${esc(q.line)}</span>` +
        `<span class="gw-quote-text">${esc(q.text)}</span>` +
        `<button type="button" class="gw-quote-pdf" data-quote="${esc(q.text)}" ` +
        `title="Locate this passage in the PDF and open the viewer at that page">show in PDF</button>` +
        `</div>`).join("") + `</div>`;
  }

  function prefillHtml(card) {
    const p = card.prefill;
    if (!p) return "";
    const isHint = !p.value;
    return `<div class="gw-evidence gw-ev-coding${isHint ? " hint" : ""}">` +
      `<span class="gw-ev-tag" title="The Coding tab's own answer for this study - pre-filled here so you only need to confirm it">CODING</span>` +
      `<span class="gw-ev-detail">${esc(p.detail || "")}</span>` +
      (isHint ? "" :
        `<button type="button" class="btn small" data-use-prefill="1" ` +
        `title="Pre-select the coding tab's answer in the box below - you still confirm it">` +
        `use this answer</button>`) +
      `</div>`;
  }

  function guessHtml(card) {
    const g = card.harmonizer_guess;
    if (!g) {
      const det = card.auto_detail || "";
      if (!det) return "";
      return `<div class="gw-evidence gw-ev-guess"><span class="gw-ev-tag" ` +
        `title="What the harmonizer itself assumed when it built the wide table">HARMONIZER GUESS</span>` +
        `<span class="gw-ev-detail">${esc(det)}</span></div>`;
    }
    return `<div class="gw-evidence gw-ev-guess"><span class="gw-ev-tag" ` +
      `title="What the harmonizer itself decoded when it built the wide table">HARMONIZER GUESS</span>` +
      `<span class="gw-ev-detail"><b>${esc(g.value)}</b> — ${esc(g.detail || "")}</span></div>`;
  }

  // -------------------------------------------------- pane (b): cards
  function cardHeaderHtml(card) {
    return `<div class="gw-card-q">` +
      `<span class="gw-card-slot" title="${esc(card.hint || card.question)}">${esc(card.short)}</span>` +
      `<b>${esc(card.question)}</b>` +
      (card.sub ? `<span class="gw-card-sub">${esc(card.sub)}</span>` : "") +
      `</div>` +
      `<div class="gw-card-writes" title="Exactly what your confirmed answer writes, and where">` +
      `Your confirmed answer ${esc(card.writes || "records a decision row in the study's assignment file")}</div>`;
  }

  function selectAnswerHtml(card) {
    const field = SLOT_FIELD[card.slot] || "";
    const opts = (card.answer.options || []).slice();
    const cur = (card.prefill && card.prefill.value) || "";
    if (cur && !opts.includes(cur)) opts.unshift(cur);
    return `<div class="gw-answer">` +
      `<label class="gw-answer-l">${esc(ROLE_LABEL[field] || field)}</label>` +
      `<select class="select gw-answer-select" data-field="${esc(field)}">` +
      `<option value="">— pick an answer —</option>` +
      opts.map((o) => `<option value="${esc(o)}"${o === cur ? " selected" : ""}>${esc(o)}</option>`).join("") +
      `</select>` +
      (card.prefill && card.prefill.value
        ? `<span class="gw-answer-prefilled" title="${esc(card.prefill.detail || "")}">pre-filled from the coding tab (${esc(card.prefill.confidence || "medium")} confidence)</span>`
        : "") +
      `</div>`;
  }

  function columnAnswerHtml(card) {
    const cands = card.answer.candidates || [];
    const guess = card.answer.guess || "";
    return `<div class="gw-answer">` +
      `<label class="gw-answer-l">${esc(card.question)}</label>` +
      `<input class="input mono-val gw-answer-input" id="gw-answer-input" ` +
      `placeholder="column name…" value="${esc(guess)}" autocomplete="off" spellcheck="false">` +
      (guess ? `<span class="gw-answer-prefilled" ` +
        `title="The harmonizer's own name-based guess - click a candidate or type your own">` +
        `harmonizer's guess pre-filled</span>` : "") +
      (cands.length ? `<div class="gw-cands">` +
        cands.slice(0, 24).map((c) => {
          const stats = c.numeric
            ? `mean ${c.mean} · range ${c.min}–${c.max}`
            : (c.top || []).slice(0, 3).map((v) => `"${v}"`).join(", ");
          return `<button type="button" class="gw-cand${c.guess ? " guess" : ""}" data-cand="${esc(c.name)}" ` +
            `title="numeric: ${esc(c.numeric ? "yes" : "no")}${c.missing ? ` · ${c.missing} missing in the first rows` : ""}">` +
            `<span class="gw-cand-name">${esc(c.name)}</span>` +
            `<span class="gw-cand-stats">${esc(stats)}</span>` +
            `</button>`;
        }).join("") +
        (cands.length > 24 ? `<span class="gw-cands-more">+${cands.length - 24} more columns — type to filter in the preview</span>` : "") +
        `</div>` : `<p class="assign-hint">No readable column list for this study — type the column name.</p>`) +
      `</div>`;
  }

  function pairAnswerHtml(card) {
    const cols = (card.answer.dv_columns || []).filter((c) => !c.veracity);
    const withVer = (card.answer.dv_columns || []).filter((c) => c.veracity);
    const opt = (list, sel) => list.map((c) =>
      `<option value="${esc(c.name)}"${c.name === sel ? " selected" : ""}>${esc(c.name)}</option>`).join("");
    const naTargets = card.answer.confirm_na_targets || [];
    return `<div class="gw-answer gw-answer-pair">` +
      `<div class="gw-pair-col"><label>TRUE-stimuli column</label>` +
      `<select class="select" data-pair="true"><option value="">— pick the true-side column —</option>` +
      opt(cols) + `</select></div>` +
      `<div class="gw-pair-col"><label>FALSE-stimuli column</label>` +
      `<select class="select" data-pair="false"><option value="">— pick the false-side column —</option>` +
      opt(cols) + `</select></div>` +
      (withVer.length ? `<span class="gw-answer-prefilled">already labelled: ` +
        withVer.slice(0, 6).map((c) => `${esc(c.name)} (${esc(c.veracity)})`).join(" · ") + `</span>` : "") +
      `<div class="gw-na-confirm">` +
      `<button type="button" class="btn ghost" id="gw-confirm-na" ` +
      `title="${esc(card.answer.na_hint || "Confirming the absence records an N/A verdict and removes the study from this queue.")}">` +
      `${esc(card.answer.na_label || "Confirm: this study truly has no true/false split")}</button>` +
      (naTargets.length ? `<span class="gw-na-note">records an N/A verdict for ${esc(naTargets.length)} unlabelled outcome column(s)</span>` : "") +
      `</div></div>`;
  }

  function rolesAnswerHtml(card) {
    const obs = card.answer.observed || [];
    const pre = card.prefill && card.prefill.detail ? card.prefill.detail : "";
    return `<div class="gw-answer">` +
      (pre ? `<div class="gw-roles-prefill" title="The coding tab's own description of the arms">${esc(pre)}</div>` : "") +
      (!obs.length ? `<p class="assign-hint">No condition values could be read for this study — type the role map as a note below.</p>` : "") +
      (obs.length ? `<div class="gw-roles">` + obs.map((o) =>
        `<div class="gw-role-row">` +
        `<span class="gw-role-code" title="${esc(o.label || o.arm || "")}">${esc(o.code)}</span>` +
        (o.role ? `<span class="gw-role-cur" title="the role already recorded in conditions.csv">${esc(o.role)}</span>` : "") +
        `<select class="select" data-role-code="${esc(o.code)}">` +
        `<option value="">— role? —</option>` +
        ARM_ROLES.map((r) => `<option value="${esc(r)}" title="${esc(ARM_ROLE_HINT[r])}">${esc(r)}</option>`).join("") +
        `</select></div>`).join("") + `</div>` : "") +
      `</div>`;
  }

  function exclusionsAnswerHtml(card) {
    const flags = card.answer.flags || [];
    return `<div class="gw-answer">` +
      (!flags.length ? `<p class="assign-hint">No flag_* columns found in the wide table — nothing to encode.</p>` :
        `<div class="gw-roles">` + flags.map((f) =>
          `<div class="gw-role-row">` +
          `<span class="gw-role-code" title="the author check column">${esc(f.name)}</span>` +
          `<span class="gw-role-cur">values: ${(f.top || []).join(" | ") || "?"}</span>` +
          `<select class="select" data-flag="${esc(f.name)}">` +
          `<option value="">— what does it gate? —</option>` +
          `<option value="keep0">keep rows where it is 0 (drop failed checks)</option>` +
          `<option value="keep1">keep rows where it is 1</option>` +
          `<option value="na">not an exclusion — ignore</option>` +
          `</select></div>`).join("") + `</div>`) +
      `<div class="gw-answer-note"><label>One-line reason (recorded with the decision)</label>` +
      `<input class="input" id="gw-note-input" placeholder="e.g. drop participants who failed the attention check (all outcomes)"></div>` +
      `</div>`;
  }

  function renameAnswerHtml(card) {
    return `<div class="gw-answer">` +
      `<label class="gw-answer-l">Uniform name for this column</label>` +
      `<input class="input mono-val gw-answer-input" id="gw-answer-input" value="${esc(card.uniform_variable || "")}" ` +
      `autocomplete="off" spellcheck="false">` +
      `<span class="gw-answer-prefilled">${esc(card.answer.hint || "")}</span>` +
      `</div>`;
  }

  function noteAnswerHtml(card) {
    return `<div class="gw-answer">` +
      `<label class="gw-answer-l">Your decision / note</label>` +
      `<textarea class="textarea" id="gw-note-area" rows="2" ` +
      `placeholder="One line the harmonizer owner can act on"></textarea>` +
      `</div>`;
  }

  function actionsHtml(card) {
    const keyed = !!(card.original_variable || card.kind === "column");
    return `<div class="gw-actions">` +
      `<button type="button" class="btn primary" id="gw-confirm" ` +
      `title="Stage this answer for the top-row Save & apply — it is written to the study's variable_assignments.csv and applied to the harmonized sidecar">` +
      `Confirm<span class="gw-btn-sub">stage this answer</span></button>` +
      `<button type="button" class="btn ghost" id="gw-undo" ` +
      `title="Undo the staged answer for this card (nothing has been written yet)">` +
      `Undo<span class="gw-btn-sub">unstage this card</span></button>` +
      `<button type="button" class="btn ghost" id="gw-na" ${keyed ? "" : "disabled"} ` +
      `title="Record that this question does not apply to this variable, with a one-line reason">` +
      `Mark N/A<span class="gw-btn-sub">not applicable, asks a reason</span></button>` +
      (card.slot === "S6" || card.slot === "S6cell" || card.kind === "raw" ?
        `<button type="button" class="btn ghost" id="gw-split" ` +
        `title="Turn one composite column into two (true-side / false-side) by an item list — stages a veracity split">` +
        `Split into two variables<span class="gw-btn-sub">true/false split by item list</span></button>` : "") +
      `<button type="button" class="btn ghost" id="gw-decode" ` +
      `title="Run the harmonizer's own name decoders on the current answer (what the uniform name implies about domain, veracity, timing, direction)">` +
      `Decode values<span class="gw-btn-sub">what the name implies</span></button>` +
      `</div>` +
      // the one-line N/A reason form (Mark N/A and the pair's confirm-absence
      // button both open it; the reason stays visible in the card)
      `<div class="gw-naform" id="gw-naform" hidden>` +
      `<label class="gw-answer-l" id="gw-na-label">One-line reason this does not apply</label>` +
      `<input class="input" id="gw-na-reason" placeholder="e.g. composite score across true and false items - no true/false split" autocomplete="off">` +
      `<div class="gw-naform-actions">` +
      `<button type="button" class="btn" id="gw-na-go" ` +
      `title="Stage the N/A verdict with this reason - the confirmed row removes the study from the queue once saved">` +
      `Record the N/A verdict</button>` +
      `<button type="button" class="btn ghost" id="gw-na-cancel">Cancel</button>` +
      `</div></div>` +
      `<div class="assign-decode" id="assign-decode">${decodeHtml()}</div>` +
      `<div class="gw-splitform" id="assign-splitform" hidden>` +
      `<div class="assign-grid2">` +
      `<div class="assign-field"><label>pid column</label><input class="input" id="split-pid" value="pid"></div>` +
      `<div class="assign-field"><label>true values</label><input class="input" id="split-true" placeholder="1,true,real"></div>` +
      `<div class="assign-field"><label>false values</label><input class="input" id="split-false" placeholder="0,-1,false,fake"></div>` +
      `<div class="assign-field"><label>source file</label><input class="input" id="split-file" placeholder="${esc(state.key || "")} - prepared.csv"></div>` +
      `</div><button type="button" class="btn" id="split-add">Stage the split</button></div>` +
      `<div class="assign-status" id="assign-status"></div>`;
  }

  function rawCardHtml(card) {
    // leftover capability: an unmapped raw column becomes an optional card
    return cardHeaderHtml({ ...card, question: "This raw column is not mapped to a uniform variable yet — what is it?", short: "Leftover column", sub: card.target, writes: "adds a sidecar row for this raw column via the study's variable_assignments.csv" }) +
      `<div class="gw-answer">` +
      `<div class="assign-grid2">` +
      `<div class="assign-field"><label>Kind of variable</label>` +
      `<select class="select" data-afield="role">` +
      ["dv", "covariate", "flag", "condition", "id", "other", ""].map((r) => `<option value="${esc(r)}">${esc(r || "—")}</option>`).join("") +
      `</select></div>` +
      `<div class="assign-field"><label>Uniform name</label>` +
      `<input class="input mono-val" id="gw-raw-uv" placeholder="dv_<domain>[_<veracity>][_<time>]" autocomplete="off" spellcheck="false"></div>` +
      `</div></div>` + actionsHtml(card);
  }

  function cardHtml(card) {
    if (card.kind === "raw") return rawCardHtml(card);
    let answer = "";
    if (card.kind === "select") answer = selectAnswerHtml(card);
    else if (card.kind === "column") answer = columnAnswerHtml(card);
    else if (card.kind === "pair") answer = pairAnswerHtml(card);
    else if (card.kind === "roles") answer = rolesAnswerHtml(card);
    else if (card.kind === "exclusions") answer = exclusionsAnswerHtml(card);
    else if (card.kind === "rename") answer = renameAnswerHtml(card);
    else answer = noteAnswerHtml(card);
    return cardHeaderHtml(card) + quoteHtml(card) + prefillHtml(card) +
      guessHtml(card) + answer + actionsHtml(card);
  }

  function answeredCardHtml(card, summary) {
    const who = summary.coder ? ` by ${summary.coder}` : "";
    return `<div class="gw-card answered" data-card-id="${esc(card.id)}">` +
      `<div class="gw-card-answered-head">` +
      `<span class="gw-card-slot">${esc(card.short)}</span>` +
      `<b>${esc(card.question)}</b>` +
      `<span class="gw-card-ans">${esc(summary.value || summary.note || "answered")}</span>` +
      `<span class="gw-card-by">${esc(who)}</span>` +
      `</div>` +
      `<div class="gw-card-answered-sub">${esc(card.sub || card.target || "")}</div>` +
      (summary.staged ? `<button type="button" class="gw-unstage" data-unstage-card="${esc(card.id)}" ` +
        `title="Undo this staged answer (nothing saved yet)">Undo</button>` : "") +
      `</div>`;
  }

  function queuePaneHtml() {
    const parts = [`<div class="gw-pane-h">Gaps to label</div>`, progressHtml()];
    if (state.data.warning) {
      parts.push(`<div class="gw-warning">${esc(state.data.warning)}</div>`);
    }
    // cards answered in a previous session (from the coverage products)
    const preAnswered = (state.data.answered || []).slice(0, 40);
    if (preAnswered.length) {
      parts.push(`<details class="gw-answered-prev"><summary>` +
        `${preAnswered.length} answered earlier — click to review</summary>` +
        preAnswered.map((a) =>
          `<div class="gw-card answered static">` +
          `<div class="gw-card-answered-head">` +
          `<span class="gw-card-slot" title="${esc(a.question || "")}">${esc(a.short)}</span>` +
          `<b>${esc(a.target)}</b>` +
          `<span class="gw-card-ans">${esc(a.status === "na" ? "N/A" : a.status)}</span>` +
          (a.coder ? `<span class="gw-card-by">by ${esc(a.coder)}</span>` : "") +
          `</div></div>`).join("") + `</details>`);
    }
    // this session's answers, collapsed
    answeredSessionCards().forEach((c) => {
      parts.push(answeredCardHtml(c, state.session[c.id]));
    });
    const cur = currentCard();
    if (cur) {
      parts.push(`<div class="gw-card current" id="gw-current-card">${cardHtml(cur)}</div>`);
    } else if (state.queue.length || preAnswered.length) {
      parts.push(`<div class="gw-done-note">All ${esc(state.queue.length)} open question(s) for this study are answered.` +
        `${stagedRows().length ? " Press Save & apply to write them to the dataset." : ""}</div>`);
    } else {
      parts.push(`<div class="gw-done-note">This study has no open gaps in the coverage products.</div>`);
    }
    // optional leftover raw columns (never part of the counted queue)
    if ((state.rawCards || []).length) {
      parts.push(`<details class="gw-rawcards"><summary>` +
        `${state.rawCards.length} unmapped raw column(s) — optional</summary>` +
        state.rawCards.slice(0, 12).map((c, i) =>
          `<button type="button" class="gw-rawcard-btn" data-rawidx="${i}">${esc(c.target)}</button>`
        ).join("") + `</details>`);
    }
    return parts.join("");
  }

  // ------------------------------------ pane (c): data + what it unlocks
  function checklistHtml() {
    const items = state.data.checklist || [];
    const pools = state.data.pools || {};
    return `<div class="gw-pane-h">What this unlocks</div>` +
      `<div class="gw-pools">` +
      [["sdt", "signal-detection pool"], ["did", "pre-post pool"]].map(([k, label]) => {
        const p = pools[k] || {};
        const tip = (p.define || "") + (p.joins ? " This study joins it." :
          (p.missing && p.missing.length ? " Still missing: " + p.missing.join("; ") + "." : ""));
        return `<span class="cv-pool ${p.joins ? "ok" : "bad"}" title="${esc(tip)}">` +
          `${p.joins ? "✓" : "✗"} ${esc(label)}</span>`;
      }).join("") +
      (() => {
        const p = pools.excl || {};
        const tip = (p.define || "") + " " + (p.state === "applied" ? "Drop rules are encoded."
          : p.state === "none" ? "No author exclusion checks."
          : "Drop rules not encoded yet.");
        const cls = p.state === "applied" ? "ok" : (p.state === "rules-missing" ? "warn" : "na");
        const lbl = p.state === "applied" ? "author exclusions applied"
          : p.state === "none" ? "no author exclusions" : "author exclusions apply";
        return `<span class="cv-pool ${cls}" title="${esc(tip)}">${esc(lbl)}</span>`;
      })() +
      `</div>` +
      `<div class="gw-checklist">` +
      items.map((it) => {
        const cls = it.status === "filled" || it.status === "confirmed" || it.status === "na"
          ? "ok" : (it.status === "missing" ? "bad" : "warn");
        const tip = `${it.code} · ${it.question}\n${it.hint || ""}\nState: ${it.state}` +
          (it.open ? ` (${it.open} to answer)` : "");
        const nOpen = pendingCards().filter((c) => c.slot === it.code).length;
        return `<button type="button" class="gw-check ${cls}" data-check-slot="${esc(it.code)}" title="${esc(tip)}">` +
          `<span class="gw-check-dot"></span>` +
          `<span class="gw-check-q">${esc(it.short)}</span>` +
          `<span class="gw-check-n">${nOpen ? esc(nOpen) : (it.status === "na" ? "n/a" : "✓")}</span>` +
          `</button>`;
      }).join("") + `</div>`;
  }

  function previewHtml() {
    const c = state.context;
    const head = `<div class="ctx-h" title="The first rows of the study's data — the column you typed in the answer box is highlighted">Raw data preview</div>`;
    if (state.contextLoading) return head + `<p class="assign-hint"><span class="spinner"></span> loading…</p>`;
    if (!c || !c.dataset_preview) {
      return head + `<p class="assign-hint">No readable data preview for this study.</p>`;
    }
    const dp = c.dataset_preview;
    const isHl = (col) => col && col === dp.highlight;
    const th = (dp.columns || []).map((col) =>
      `<th${isHl(col) ? ' class="hl"' : ""}>${esc(col)}</th>`).join("");
    const body = (dp.rows || []).map((row) =>
      `<tr>` + (dp.columns || []).map((col, i) =>
        `<td${isHl(col) ? ' class="hl"' : ""}>${esc(row[i] == null ? "" : row[i])}</td>`).join("") + `</tr>`).join("");
    return head +
      `<div class="ctx-dp-file">${esc(dp.file || "")} (${esc(dp.n_rows)} rows × ${esc(dp.n_cols)} cols)</div>` +
      `<div class="ctx-tablewrap"><table class="ctx-table ctx-dp"><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table></div>`;
  }

  function unlockPaneHtml() {
    return checklistHtml() + `<div class="ctx-block" id="gw-preview">${previewHtml()}</div>`;
  }

  // ------------------------------------------------------------- chrome
  function workspacePanel() {
    const n = stagedRows().length;
    const head = `<header class="assign-head" id="assign-head">` +
      `<b>Gap workspace</b>` +
      `<span class="pill">${esc(state.key || "")}</span>` +
      `<span class="assign-dirty" id="assign-dirty" ${n ? "" : "hidden"}>${n} unsaved</span>` +
      `<span class="grow"></span>` +
      `<button type="button" class="btn primary" id="assign-save-all" ${n ? "" : "disabled"} ` +
      `title="Write every staged answer to the study's variable_assignments.csv and regenerate the harmonized sidecar/wide (variable_assignments.write_rows + apply_files)">` +
      `Save & apply${n ? ` (${n})` : ""}</button>` +
      `<button type="button" class="btn ghost" id="assign-close">Close</button>` +
      `</header>`;
    return `<section class="assign-ws gw-ws" id="assign-ws">${head}` +
      `<div class="gw-grid">` +
      `<div class="gw-pane gw-study" id="gw-study">${studyPaneHtml()}</div>` +
      `<div class="gw-pane gw-queue" id="gw-queue">${queuePaneHtml()}</div>` +
      `<div class="gw-pane gw-unlock" id="gw-unlock">${unlockPaneHtml()}</div>` +
      `</div></section>`;
  }

  function skeletonHtml() {
    return `<section class="assign-ws gw-ws" id="assign-ws"><header class="assign-head">` +
      `<b>Gap workspace</b><span class="pill">loading…</span></header>` +
      `<div class="gw-grid"><div class="gw-pane"><div class="skeleton">` +
      `<div class="sk" style="width:70%"></div><div class="sk" style="width:90%"></div>` +
      `<div class="sk" style="width:60%"></div></div></div></div></section>`;
  }

  function renderHost() {
    const host = state.host;
    if (!host) return;
    if (!state.open || !state.key || !state.data) {
      host.hidden = true;
      host.innerHTML = "";
      return;
    }
    host.hidden = false;
    host.innerHTML = workspacePanel();
    paintLock();
  }

  function rerenderQueue() {
    const el = state.host && state.host.querySelector("#gw-queue");
    if (el) el.innerHTML = queuePaneHtml();
  }
  function rerenderUnlock() {
    const el = state.host && state.host.querySelector("#gw-unlock");
    if (el) el.innerHTML = unlockPaneHtml();
  }
  function rerenderStudy() {
    const el = state.host && state.host.querySelector("#gw-study");
    if (el) el.innerHTML = studyPaneHtml();
  }

  function setStatus(msg, kind = "") {
    const el = state.host && state.host.querySelector("#assign-status");
    if (el) { el.textContent = msg || ""; el.className = "assign-status " + kind; }
  }

  function updateHeader() {
    const host = state.host;
    if (!host) return;
    const n = stagedRows().length;
    const btn = host.querySelector("#assign-save-all");
    if (btn) {
      btn.disabled = n === 0 || state.busy || !!state.locked;
      btn.textContent = n ? `Save & apply (${n})` : "Save & apply";
    }
    const dirty = host.querySelector("#assign-dirty");
    if (dirty) {
      dirty.hidden = n === 0;
      dirty.textContent = n ? `${n} unsaved` : "";
    }
  }

  // -------------------------------------------------------------- load
  async function loadStudy(key, slot) {
    const token = state.token;
    const [walk, asg] = await Promise.all([
      api("api/gapwalk/" + enc(key)).catch(() => null),
      api("api/assignments/" + enc(key)).catch(() => ({ applied: [] })),
    ]);
    if (token !== state.token) return;
    const fallback = {
      key, counts: { open: 0, answered: 0 }, queue: [], answered: [],
      checklist: [], pools: {}, facts: [], paper: {}, notes: "",
      options: DEFAULT_OPTIONS, profile: [], raw_targets: [],
      wide_columns: [],
    };
    if (!walk) {
      // the fetch failed (server or network): show the workspace with the
      // reason instead of closing it silently
      state.data = Object.assign(fallback, {
        warning: "Could not load the gap queue for this study — try again.",
      });
    } else if (walk.exists === false) {
      state.data = Object.assign(fallback, walk, {
        warning: walk.warning ||
          "No dataset folder for this study — open it in the Coding tab instead.",
      });
    } else {
      state.data = walk;
    }
    state.queue = (state.data.queue || []).slice();
    state.rawCards = (state.data.raw_targets || []).map((t) => ({
      id: "raw|" + t.target, slot: "", kind: "raw", scope: "variable",
      target: t.target, question: "", short: "Leftover column",
      sub: t.target, status: "unmapped", auto_detail: t.detail || "",
      evidence: t.evidence || "", hint: "",
      uniform_variable: t.uniform_variable || "", original_variable: t.original_variable || t.target,
      artifact: t.artifact || "", item_scope: t.item_scope || "",
      prefill: null, harmonizer_guess: null, paper_quotes: [],
      writes: "adds a sidecar row for this raw column via the study's variable_assignments.csv",
    }));
    state.appliedRows = asg.applied || [];
    state.options = Object.assign({}, DEFAULT_OPTIONS, state.data.options || {});
    // jump to the requested slot if the caller asked for one
    if (slot) {
      const i = firstCardOfSlot(slot);
      if (i != null) state.idx = i;
    }
  }

  async function open(key, slot) {
    if (!key) return;
    if (!state.host) mount(document.getElementById("cv-assign-host"));
    if (!state.host) return;
    const token = ++state.token;
    state.key = key;
    state.coder = readCoder();
    state.open = true;
    state.idx = 0;
    state.session = {};
    state.pending = {};
    state.context = null;
    state.decode = null;
    state.data = null;
    state.host.hidden = false;
    state.host.innerHTML = skeletonHtml();
    try {
      await loadStudy(key, slot || "");
    } catch (e) {
      if (token !== state.token) return;
      state.host.innerHTML = `<section class="assign-ws gw-ws" id="assign-ws">` +
        `<header class="assign-head"><b>Gap workspace</b>` +
        `<span class="grow"></span><button type="button" class="btn ghost" id="assign-close">Close</button></header>` +
        `<div class="gw-pane"><p class="assign-hint">Could not load the gap queue: ${esc(e.message || e)}</p></div></section>`;
      return;
    }
    if (token !== state.token) return;    // a newer open() superseded this one
    renderHost();
    fetchContext();
    holdStudyLock(key);
    try { state.host.scrollIntoView({ block: "nearest" }); } catch { /* ignore */ }
  }

  function close() {
    if (stagedRows().length && !window.confirm("Discard unsaved answers?")) return;
    releaseStudyLock();
    state.token++;
    state.open = false;
    state.data = null;
    state.queue = [];
    state.session = {};
    state.pending = {};
    state.context = null;
    state.decode = null;
    if (state.host) { state.host.hidden = true; state.host.innerHTML = ""; }
  }

  // ---------------------------------------------------- row construction
  function baseRow(card) {
    return {
      artifact: card.artifact || "*", original_variable: card.original_variable || "",
      item_scope: card.item_scope || "", uniform_variable: card.uniform_variable || "",
      role: "", dv_domain: "", veracity: "", timepoint: "", dv_polarity: "",
      slot_fix: "", source_file: "", evidence: "", note: "", status: "confirmed",
    };
  }

  function stageRows(card, rows, summary) {
    // stage rows under their draft keys and remember them on the card's
    // session entry, so Undo removes exactly what Confirm added
    const keys = rows.map((r) => { state.pending[draftKey(r)] = r; return draftKey(r); });
    state.session[card.id] = { ...summary, staged: true, coder: readCoder(), keys };
    updateHeader();
    rerenderQueue();
  }

  function advance() {
    const pend = pendingCards();
    state.idx = Math.min(state.idx, Math.max(0, pend.length - 1));
  }

  function collectAnswer(card) {
    // returns {rows: [...], summary: "..."} from the current card's inputs
    const host = state.host;
    const field = SLOT_FIELD[card.slot] || "";
    if (card.kind === "select") {
      const sel = host.querySelector(".gw-answer-select");
      const value = sel ? sel.value : "";
      if (!value) return null;
      const row = baseRow(card);
      row[field] = value;
      row.status = "confirmed";
      row.note = (card.prefill && card.prefill.value === value)
        ? "confirmed coding pre-fill: " + (card.prefill.detail || "")
        : "";
      return { rows: [row], summary: { value: `${field}=${value}` } };
    }
    if (card.kind === "column") {
      const inp = host.querySelector("#gw-answer-input");
      const col = inp ? inp.value.trim() : "";
      if (!col) return null;
      const row = baseRow(card);
      if (card.slot === "S1") { row.uniform_variable = "pid"; row.role = "id"; }
      else if (card.slot === "S2") { row.uniform_variable = "condition"; row.role = "condition"; }
      else {
        row.uniform_variable = "cov_" + col.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 40);
        row.role = "covariate";
      }
      row.original_variable = col;
      row.artifact = card.artifact || "*";
      row.item_scope = "";
      row.status = "confirmed";
      row.note = `S${card.slot === "S10" ? "10" : card.slot.slice(1)}: picked from the column list`;
      return { rows: [row], summary: { value: `${col} → ${row.uniform_variable}` } };
    }
    if (card.kind === "rename") {
      const inp = host.querySelector("#gw-answer-input");
      const name = inp ? inp.value.trim() : "";
      if (name && name !== card.uniform_variable) {
        const row = baseRow(card);
        row.uniform_variable = name;
        row.status = "confirmed";
        row.note = "rename: " + card.uniform_variable + " -> " + name;
        return { rows: [row], summary: { value: `renamed to ${name}` } };
      }
      const note = (host.querySelector("#gw-note-area") || {}).value || "";
      if (!note) return null;
      const row = baseRow(card);
      row.note = note;
      row.status = "confirmed";
      return { rows: [row], summary: { value: "note: " + note.slice(0, 80) } };
    }
    if (card.kind === "pair") {
      const t = (host.querySelector('[data-pair="true"]') || {}).value || "";
      const f = (host.querySelector('[data-pair="false"]') || {}).value || "";
      const cols = card.answer.dv_columns || [];
      const rows = [];
      [[t, "true"], [f, "false"]].forEach(([name, ver]) => {
        if (!name) return;
        const meta = cols.find((c) => c.name === name) || {};
        const row = baseRow({
          ...card, uniform_variable: name,
          original_variable: meta.original_variable || name,
          artifact: meta.artifact || "*", item_scope: meta.item_scope || "",
        });
        row.veracity = ver;
        row.status = "confirmed";
        row.note = `S6cell: the ${ver}-side column of the discernment pair`;
        rows.push(row);
      });
      if (!rows.length) return null;
      return { rows, summary: { value: `true=${t || "?"} · false=${f || "?"}` } };
    }
    if (card.kind === "roles") {
      const sels = Array.from(host.querySelectorAll("[data-role-code]"));
      const picked = sels.map((s) => ({ code: s.dataset.roleCode, role: s.value }))
        .filter((x) => x.role);
      if (!picked.length) {
        const note = (host.querySelector("#gw-note-area") || {}).value || "";
        if (!note) return null;
        const row = baseRow(card);
        row.original_variable = condColumn();
        row.item_scope = "S3";
        row.note = note;
        row.evidence = "S3 role note";
        return { rows: [row], summary: { value: "note: " + note.slice(0, 80) } };
      }
      const rows = picked.map((p) => {
        const row = baseRow(card);
        row.original_variable = condColumn();
        row.item_scope = "S3:" + p.code;
        row.evidence = JSON.stringify({ code: p.code, role: p.role });
        row.note = `S3: condition "${p.code}" is ${p.role}`;
        return row;
      });
      return { rows, summary: { value: picked.map((p) => `${p.code}=${p.role}`).join(" · ") } };
    }
    if (card.kind === "exclusions") {
      const sels = Array.from(host.querySelectorAll("[data-flag]"));
      const reason = (host.querySelector("#gw-note-input") || {}).value || "";
      const picked = sels.map((s) => ({ flag: s.dataset.flag, keep: s.value }))
        .filter((x) => x.keep);
      if (!picked.length) return null;
      const rows = picked.map((p) => {
        const row = baseRow(card);
        row.original_variable = p.flag;
        row.item_scope = "S9";
        row.evidence = JSON.stringify({ flag: p.flag, rule: p.keep,
          keep: p.keep === "keep0" ? "0" : (p.keep === "keep1" ? "1" : ""),
          applies_to: "*", reason: reason || "author exclusion check" });
        row.note = `S9: ${p.flag} -> ${p.keep}`;
        return row;
      });
      return { rows, summary: { value: picked.map((p) => `${p.flag}: ${p.keep}`).join(" · ") } };
    }
    // note kinds (S11 and friends) and raw leftover columns
    if (card.kind === "raw") {
      const host2 = state.host;
      const roleSel = host2.querySelector('[data-afield="role"]');
      const uvInp = host2.querySelector("#gw-raw-uv");
      const role = roleSel ? roleSel.value : "";
      const uv = uvInp ? uvInp.value.trim() : "";
      if (!role || (role !== "other" && !uv)) return null;
      const row = baseRow(card);
      row.role = role;
      row.uniform_variable = uv;
      row.status = "confirmed";
      row.note = "mapped leftover raw column";
      return { rows: [row], summary: { value: `${card.target} → ${uv || role}` } };
    }
    const note = (host.querySelector("#gw-note-area") || {}).value ||
      (host.querySelector("#gw-note-input") || {}).value || "";
    if (!note) return null;
    const row = baseRow(card);
    row.original_variable = card.original_variable || condColumn() || firstDvColumn();
    row.item_scope = card.slot || "note";
    row.note = note;
    row.evidence = card.slot || "";
    return { rows: [row], summary: { value: "note: " + note.slice(0, 80) } };
  }

  function condColumn() {
    const cols = state.data.wide_columns || [];
    return cols.includes("condition") ? "condition"
      : (cols.includes("condition_raw") ? "condition_raw" : "");
  }
  function firstDvColumn() {
    return (state.data.wide_columns || []).find((c) => c.startsWith("dv_")) || "";
  }

  // ------------------------------------------------------------- staging
  function confirmCard({ quiet = false } = {}) {
    const card = currentCard();
    if (!card) return false;
    const got = collectAnswer(card);
    if (!got) {
      if (!quiet) setStatus("Pick an answer first — nothing is staged.", "err");
      return false;
    }
    stageRows(card, got.rows, got.summary);
    advance();
    if (!quiet) setStatus(`Staged — press “Save & apply” to write ${got.rows.length} row(s) to the dataset.`, "ok");
    return true;
  }

  function undoCard(cardId) {
    const card = state.queue.find((c) => c.id === cardId);
    if (!card) return;
    const sum = state.session[card.id];
    if (!sum) return;
    // remove exactly the rows this card staged (tracked at stage time)
    (sum.keys || []).forEach((k) => { delete state.pending[k]; });
    delete state.session[card.id];
    updateHeader();
    rerenderQueue();
    setStatus("Staged answer discarded — nothing was written.", "");
  }

  function openNaForm(label) {
    const form = state.host.querySelector("#gw-naform");
    if (!form) return;
    form.hidden = false;
    const lab = state.host.querySelector("#gw-na-label");
    if (lab && label) lab.textContent = label;
    const inp = state.host.querySelector("#gw-na-reason");
    if (inp) { inp.value = ""; inp.focus(); }
  }

  function markNA() {
    const card = currentCard();
    if (!card) return;
    if (card.kind === "pair") {
      // the confirm-absence button on the pair card is the N/A path; use it
      const btn = state.host.querySelector("#gw-confirm-na");
      if (btn) { btn.click(); return; }
    }
    openNaForm("One-line reason this does not apply");
  }

  function recordNa() {
    const card = currentCard();
    if (!card) return;
    const reasonInput = state.host.querySelector("#gw-na-reason");
    const reason = reasonInput ? reasonInput.value.trim() : "";
    const pairConfirm = card.kind === "pair";
    const rows = [];
    if (pairConfirm) {
      const targets = card.answer.confirm_na_targets || [];
      const cols = card.answer.dv_columns || [];
      targets.forEach((uv) => {
        const meta = cols.find((c) => c.name === uv) || {};
        const row = baseRow({
          ...card, uniform_variable: uv,
          original_variable: meta.original_variable || uv,
          artifact: meta.artifact || "*", item_scope: meta.item_scope || "",
        });
        row.role = "na";
        row.status = "confirmed";
        row.note = reason || "no true/false split";
        rows.push(row);
      });
      if (!rows.length) {
        setStatus("No unlabelled outcome columns to mark N/A for this study.", "err");
        return;
      }
      stageRows(card, rows, {
        value: "N/A confirmed: " + (reason || "no true/false split") +
          ` (${rows.length} outcome column(s))`,
      });
      setStatus(`Staged the N/A verdict for ${rows.length} column(s) — press “Save & apply”.`, "ok");
    } else {
      const row = baseRow(card);
      row.role = "na";
      row.status = "confirmed";
      row.note = reason || "not applicable";
      if (!row.original_variable && (card.kind === "roles" || card.kind === "exclusions")) {
        row.original_variable = condColumn() || firstDvColumn();
        row.item_scope = card.slot;
      }
      rows.push(row);
      stageRows(card, rows, { value: "N/A: " + (reason || "not applicable") });
      setStatus("Staged N/A — press “Save & apply” to record the verdict.", "ok");
    }
    const form = state.host.querySelector("#gw-naform");
    if (form) form.hidden = true;
    advance();
  }

  function confirmPairNA() {
    openNaForm("Confirm: this study truly has no true/false split — one-line reason");
  }

  function addSplit() {
    const card = currentCard();
    if (!card) return;
    const pid = ($("#split-pid") || {}).value || "pid";
    const t = ($("#split-true") || {}).value || "";
    const fl = ($("#split-false") || {}).value || "";
    const file = ($("#split-file") || {}).value || "";
    if (!t || !fl) { toast("Enter the true and false value lists", "err"); return; }
    const row = baseRow(card);
    row.slot_fix = "veracity_split";
    row.role = "";
    row.source_file = file;
    row.evidence = JSON.stringify({ pid_col: pid, true_values: t, false_values: fl });
    row.note = "split into two variables (true/false) by item list";
    row.status = "confirmed";
    stageRows(card, [row], { value: `split staged (${t} vs ${fl})` });
    advance();
    setStatus("Staged the split — press “Save & apply” to write it.", "ok");
  }

  // ------------------------------------------------------------- online lock
  /* The workspace writes the study's shared files (Datasets/<Key>/…), so an
     online project holds an edit lock while it is open.  Local projects have no
     lock layer and every helper below returns at once.  The note has to survive
     renderHost()'s innerHTML rewrite, so paintLock() re-inserts it after each
     render. */
  const LOCK_OFF = "#assign-save-all";
  function lockHost() { return state.host && state.host.querySelector("#assign-ws"); }

  function paintLock() {
    const C = window.CSOnline;
    const host = lockHost();
    if (!host) return;
    if (!C || !C.active() || !state.locked) {
      if (C) C.locks.clearNotice(host, LOCK_OFF);
      return;
    }
    C.locks.notice(host, state.key || "", state.locked.holder || {}, LOCK_OFF);
    const btn = host.querySelector("#assign-save-all");
    if (btn) btn.disabled = true;
  }

  async function holdStudyLock(key) {
    const C = window.CSOnline;
    if (!C || !C.active() || !key) { state.lockKey = ""; state.locked = null; return; }
    if (state.lockKey === key) return;
    if (state.lockKey) C.locks.end(state.lockKey, {});
    state.lockKey = key;
    const r = await C.locks.begin(key, { task: "harmonize", host: lockHost(), disable: LOCK_OFF });
    state.locked = r.ok ? null : r;
    paintLock();
    updateHeader();
  }

  function releaseStudyLock() {
    const C = window.CSOnline;
    if (C && state.lockKey) C.locks.end(state.lockKey, { host: lockHost(), disable: LOCK_OFF });
    state.lockKey = "";
    state.locked = null;
  }

  function lockedNow() {
    if (!state.locked) return false;
    toast("This study is locked", "err",
      `Locked by ${state.locked.who || "another coder"} — answers cannot be saved until the lock is released.`);
    return true;
  }

  // ------------------------------------------------------------- save
  async function saveAll() {
    const rows = stagedRows();
    if (!rows.length) { toast("Nothing staged to save", "ok"); return; }
    if (lockedNow()) return;
    state.busy = true;
    updateHeader();
    setStatus("Saving…");
    const token = state.token;
    try {
      const r = await api("api/assignments/" + enc(state.key), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ coder: readCoder(), assignments: rows }),
      });
      if (token !== state.token) return;
      state.pending = {};
      for (const id of Object.keys(state.session)) state.session[id].staged = false;
      const changes = (r.records || []).map((x) =>
        `${x.action} ${x.field || ""} ${x.old || ""}→${x.new || ""}`).join("; ");
      setStatus(`Saved ${r.count} row(s) · ${r.changes} dataset change(s). ${changes}`, "ok");
      toast("Answers saved", "ok", `${r.changes} dataset change(s)`);
      await loadStudy(state.key, "");
      if (token !== state.token) return;
      renderHost();
      if (window.CSCoverage && typeof window.CSCoverage.reload === "function") {
        try { window.CSCoverage.reload(); } catch { /* ignore */ }
      }
    } catch (e) {
      if (token !== state.token) return;
      setStatus("Save failed: " + String(e.message || e), "err");
      toast("Save failed", "err", String(e.message || e));
    }
    state.busy = false;
    updateHeader();
  }

  // ------------------------------------------------------------- context
  function fetchContext(highlight) {
    const card = currentCard();
    const key = state.key;
    if (!key) return;
    state.contextLoading = true;
    const el = state.host && state.host.querySelector("#gw-preview");
    if (el) el.innerHTML = previewHtml();
    const varName = highlight || (card && card.original_variable) || "";
    const uniform = (card && card.uniform_variable) || "";
    const qs = new URLSearchParams({ var: varName, uniform, artifact: (card && card.artifact) || "" });
    const token = state.token;
    api("api/rawvar/" + enc(key) + "?" + qs.toString())
      .then((d) => { if (token === state.token) state.context = d; })
      .catch((e) => { if (token === state.token) state.context = { degraded: ["context unavailable: " + String(e.message || e)] }; })
      .finally(() => {
        if (token !== state.token) return;
        state.contextLoading = false;
        const el2 = state.host && state.host.querySelector("#gw-preview");
        if (el2) el2.innerHTML = previewHtml();
      });
  }

  let decodeTimer = null;
  function decodePreview(nameOverride) {
    const card = currentCard();
    if (!card) return;
    clearTimeout(decodeTimer);
    const token = state.token;
    decodeTimer = setTimeout(() => {
      const name = nameOverride != null ? nameOverride
        : card.uniform_variable || "";
      const qs = new URLSearchParams({ name, notes: card.auto_detail || "", original: card.original_variable || "" });
      api("api/harmonizer/decode?" + qs.toString())
        .then((d) => { if (token === state.token) state.decode = d; })
        .catch((e) => { if (token === state.token) state.decode = { errors: [String(e.message || e)] }; })
        .finally(() => { if (token === state.token) renderDecodeBox(); });
    }, 180);
  }

  function decodeHtml() {
    const d = state.decode;
    if (!d) return `<span class="decode-hint" title="Run Decode values, or just start typing a uniform name — shows what the name implies (domain / veracity / timing / direction) according to the harmonizer's own decoders">Decode values shows what the uniform name implies.</span>`;
    const rows = [
      ["domain", d.domain], ["veracity", d.veracity], ["timepoint", d.timepoint],
      ["polarity", d.polarity],
    ].map(([k, v]) =>
      `<span class="decode-cell"><span>${esc(k)}</span><b>${esc(v || "—")}</b></span>`).join("");
    const note = (d.veracity_agreement === false)
      ? `<span class="decode-note warn">decoders disagree: template=${esc(d.veracity_template || "—")} · contract=${esc(d.veracity_contract || "—")}</span>`
      : (d.errors && d.errors.length ? `<span class="decode-note">${esc(d.errors[0])}</span>` : "");
    return `<div class="decode-row">${rows}</div>${note}`;
  }

  function renderDecodeBox() {
    const box = state.host && state.host.querySelector("#assign-decode");
    if (box) box.innerHTML = decodeHtml();
  }

  // --------------------------------------------------------- pdf viewer
  function openPdfAtQuote(quoteText) {
    const key = state.key;
    // switch to the coding view exactly as the Summary tab does (its viewer
    // owns the rendered PDF), then land on the quote's page
    const tab = document.querySelector('.viewtab[data-view="coding"]');
    if (tab) tab.click();
    const go = (page) => {
      window.dispatchEvent(new CustomEvent("cs:open-paper", {
        detail: { key, pdf: true, page: page || 1 },
      }));
    };
    // a long phrase often spans a PDF line break and finds nothing, so try
    // progressively shorter distinctive phrases from the quote
    const words = (quoteText || "").replace(/[^\w\s']/g, " ").split(/\s+/)
      .filter((w) => w.length > 3);
    const phrases = [words.slice(0, 5).join(" "), words.slice(0, 3).join(" "),
      words.slice(-3).join(" ")].filter(Boolean);
    const tryAt = (i) => {
      if (i >= phrases.length) { go(1); return; }
      api("api/pdfsearch/" + enc(key) + "?q=" + enc(phrases[i]))
        .then((d) => {
          if (d && d.hits && d.hits.length) go(d.hits[0].page);
          else tryAt(i + 1);
        })
        .catch(() => tryAt(i + 1));
    };
    tryAt(0);
  }

  // ------------------------------------------------------------- wiring
  function attachHost() {
    const host = state.host;
    if (!host || host.__csAssignWired) return;
    host.__csAssignWired = true;

    host.addEventListener("change", (e) => {
      const s = e.target;
      if (!s || !s.dataset) return;
      if (s.classList.contains("gw-answer-select")) decodePreview();
    });

    let ctxTimer = null;
    host.addEventListener("input", (e) => {
      const t = e.target;
      if (!t || !t.id) return;
      if (t.id === "gw-answer-input" || t.id === "gw-raw-uv") {
        // the raw-data preview highlights whatever is typed in the answer box
        clearTimeout(ctxTimer);
        ctxTimer = setTimeout(() => fetchContext(t.value.trim()), 400);
        decodePreview(t.value.trim());
      }
    });

    host.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      if (b.id === "assign-close") { close(); return; }
      if (b.id === "assign-save-all") { saveAll(); return; }
      if (b.id === "gw-confirm") { confirmCard(); return; }
      if (b.id === "gw-undo") { undoCard(currentCard() && currentCard().id); return; }
      if (b.dataset.unstageCard) { undoCard(b.dataset.unstageCard); return; }
      if (b.id === "gw-na") { markNA(); return; }
      if (b.id === "gw-na-go") { recordNa(); return; }
      if (b.id === "gw-na-cancel") {
        const form = host.querySelector("#gw-naform");
        if (form) form.hidden = true;
        return;
      }
      if (b.id === "gw-confirm-na") { confirmPairNA(); return; }
      if (b.id === "gw-split") {
        const sf = $("#assign-splitform");
        if (sf) sf.hidden = !sf.hidden;
        return;
      }
      if (b.id === "split-add") { addSplit(); return; }
      if (b.id === "gw-decode") { decodePreview(); renderDecodeBox(); return; }
      if (b.dataset.usePrefill) {
        const card = currentCard();
        if (card && card.prefill) {
          const sel = host.querySelector(".gw-answer-select");
          if (sel && card.prefill.value) sel.value = card.prefill.value;
        }
        return;
      }
      if (b.dataset.cand) {
        const inp = host.querySelector("#gw-answer-input");
        if (inp) { inp.value = b.dataset.cand; fetchContext(b.dataset.cand); }
        return;
      }
      if (b.dataset.quote) { openPdfAtQuote(b.dataset.quote); return; }
      if (b.dataset.checkSlot) {
        const i = firstCardOfSlot(b.dataset.checkSlot);
        if (i == null) {
          toast("No open card for that question", "ok",
            "every card of this question is already answered");
          return;
        }
        state.idx = i;
        rerenderQueue();
        fetchContext();
        decodePreview();
        return;
      }
      if (b.dataset.rawidx != null) {
        const c = state.rawCards[parseInt(b.dataset.rawidx, 10)];
        if (c) {
          const pend = pendingCards();
          state.rawCards.splice(parseInt(b.dataset.rawidx, 10), 1);
          state.queue.push(c);
          state.idx = Math.max(0, pend.length - 1);
          rerenderQueue();
        }
        return;
      }
      if (b.id === "gw-open-pdf") {
        window.dispatchEvent(new CustomEvent("cs:open-paper", { detail: { key: state.key, pdf: true } }));
        return;
      }
      if (b.id === "gw-open-coding") {
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

  // The Harmonization tab owns the workspace's lifetime: it tells the workspace
  // when it is on screen, so the study's edit lock is held only while it is.
  function onView(on) {
    if (!on) { releaseStudyLock(); return; }
    if (state.open && state.key) holdStudyLock(state.key).then(() => renderHost());
  }

  window.CSAssign = { open, close, mount, onView };
})();
