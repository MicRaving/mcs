/* ============================================================
   Coding Studio — Harmonization tab (coverage triage, redesigned)

   The study list is a WORK QUEUE ordered most-pools-unblocked first
   (the backend's poolability-weighted missingness score, ascending;
   finished studies sink to the end).  Everything is named in plain
   words: the readiness sentence spells out the three pool counts, the
   audit chips read as human sentences and click through to the
   disagreement, the slot chips carry the eleven questions with the
   S-codes in tooltips only, and each row shows the three pool pills
   ("Joins the signal-detection pool" / "Joins the pre-post pool" /
   "Author exclusions apply") with a hover that defines the pool and
   names what is still missing.

   Driven by GET /api/coverage; the workspace (static/assignment.js)
   walks the queue one gap at a time.
   ============================================================ */
(() => {
  "use strict";

  const { $, $$, esc, api, toast } = window.CS;

  const state = {
    data: null,
    // fewest open gaps first = most pools unblocked first (the backend sort,
    // ascending); the stable partition below only demotes finished studies
    sort: "missingness_score", dir: "asc", filter: "all", q: "",
    started: false, loading: false,
  };

  const FILTER_LABEL = {
    all: "All studies",
    blocks_sdt: "Can't join the signal-detection pool",
    blocks_did: "Can't join the pre-post pool",
    blocks_exclusions_on: "Exclusions not encoded",
    needs_na_verdict: "Needs a true/false verdict",
    has_dataset: "Has a dataset",
    paper_only: "No public dataset",
  };
  const FILTER_HINT = {
    all: "Every study in the harmonization coverage products.",
    blocks_sdt: "Studies that cannot enter the signal-detection (d') pool yet: "
      + "missing arm roles, a true/false pair, or an outcome's 0-1 twin.",
    blocks_did: "Studies that cannot enter the pre-post (DiD) pool yet: "
      + "missing the participant id, the group column, or an outcome's 0-1 twin.",
    blocks_exclusions_on: "Studies with author attention/comprehension checks "
      + "(flag_* columns) but no encoded drop rules.",
    needs_na_verdict: "Studies whose composite outcomes carry no stimulus "
      + "veracity: confirm the study truly has no true/false split, or label "
      + "the pair.",
    has_dataset: "Studies whose public dataset is acquired and harmonized.",
    paper_only: "Included papers without a public dataset - nothing to label here.",
  };

  const CHIP_STATE = {
    filled: ["ok", "answered"], confirmed: ["ok", "answered"],
    na: ["na", "not applicable"], missing: ["bad", "still to answer"],
    ambiguous: ["warn", "needs your verdict"], partial: ["warn", "partly answered"],
    "": ["na", "unknown"],
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
      toast("Could not load the harmonization queue", "err", String(e.message || e));
      state.data = { studies: [], funnel: {}, counts: {}, audit: { checks: [], findings: [] }, slots: [] };
    }
    state.loading = false;
    render();
  }

  // ------------------------------------------------------- ordered queue
  function orderedStudies() {
    // keep the backend's missingness order, but finished studies (no open
    // gaps) sink below the ones that still need work
    const studies = (state.data.studies || []).slice();
    const open = studies.filter((s) => (s.open_count || s.open_slots?.length || 0) > 0);
    const done = studies.filter((s) => !(s.open_count || s.open_slots?.length || 0));
    return open.concat(done);
  }

  // ------------------------------------------------------- readiness line
  function renderReadiness() {
    const host = $("#cv-funnel");
    if (!host) return;
    const f = (state.data.funnel || {}).pools || {};
    const any = f.any ?? "–";
    const sentence =
      `<span class="cv-readiness-main"><b>${esc(any)}</b> studies join a pool today:</span>` +
      `<span class="cv-read-item ok" title="The signal-detection pool: the intervention-vs-control effect on discrimination (d' = z(hit rate) - z(false-alarm rate)), pooled across studies. A study joins when its arm roles, its true/false pair and every outcome's 0-1 twin are labelled."><b>${esc(f.sdt ?? "–")}</b> join the signal-detection pool</span>` +
      `<span class="cv-read-item ok" title="The pre-post pool: the pretest-to-posttest (difference-in-differences) effect, pooled across studies that measured the outcome twice. A study joins when its participant id, its group column and every outcome's 0-1 twin are labelled."><b>${esc(f.did ?? "–")}</b> join the pre-post pool</span>` +
      `<span class="cv-read-item warn" title="These studies have author attention/comprehension checks (flag_* columns) but no encoded drop rules - answer 'Which rows should be dropped?' for each."><b>${esc(f.exclusions_on ?? "–")}</b> have author exclusions waiting to be encoded</span>` +
      `<span class="cv-grow"></span>` +
      `<span class="cv-read-item muted" title="Studies with a public dataset that has been harmonized.">${esc(f.datasets ?? "–")} datasets · ${esc((state.data.funnel || {}).harmonized ?? "–")} rows in the products</span>`;
    host.innerHTML = sentence;
  }

  // ------------------------------------------------------------ audit
  function renderAudit() {
    const host = $("#cv-audit");
    if (!host) return;
    const a = state.data.audit || {};
    const parity = a.parity || { ok: 0, bad: 0, n: 0 };
    const tot = parity.ok + parity.bad;
    const parts = [`<span class="cv-audit-t">Assignment audit</span>`];

    // parity: every confirmed assignment is reflected in the harmonized labels
    const parityBad = parity.bad ? parity.bad : 0;
    parts.push(
      `<button type="button" class="cv-sentence ${parityBad ? "warn" : "ok"}" ` +
      `title="Parity check: every confirmed assignment row must be reflected in the harmonized sidecar. ${esc(parityBad)} row(s) disagree - click to see which study.">` +
      (parityBad
        ? `<b>${esc(parityBad)}</b> confirmed label${parityBad === 1 ? "" : "s"} not yet reflected in the harmonized data`
        : `All <b>${esc(parity.ok)}</b> confirmed label${parity.ok === 1 ? "" : "s"} are reflected in the harmonized data`) +
      `</button>`);

    // conflicts: assignment rows stamped 'conflict' (two labels disagreed)
    const nConf = a.conflicts || 0;
    const confStudies = a.conflict_studies || [];
    parts.push(
      `<button type="button" class="cv-sentence ${nConf ? "bad" : "ok"}" ` +
      `title="A conflict is stamped when a coder's proposed label disagrees with a non-blank label that is already in the sidecar - the existing label is never silently overwritten. Click to open the most-affected study.">` +
      (nConf
        ? `<b>${esc(nConf)}</b> variable label${nConf === 1 ? "" : "s"} where a coder disagreed with the existing label` +
          (confStudies.length ? ` (${esc(confStudies.map((s) => s.study_key).slice(0, 2).join(", "))}${confStudies.length > 2 ? "…" : ""})` : "")
        : `No labelling disagreements recorded`) +
      `</button>`);

    // needs veracity
    const nVer = a.needs_veracity || 0;
    parts.push(
      `<button type="button" class="cv-sentence ${nVer ? "warn" : "ok"}" ` +
      `title="Studies whose composite outcomes carry no stimulus veracity yet. Confirm the study truly has no true/false split (N/A) or label the true/false pair - the verdict removes it from the queue.">` +
      (nVer
        ? `<b>${esc(nVer)}</b> stud${nVer === 1 ? "y" : "ies"} still need a true/false verdict`
        : `Every study's true/false verdict is in`) +
      `</button>`);

    // click-throughs
    const findings = (a.findings || []).filter((f) => f.check === "parity");
    host.innerHTML = parts.join("");
    const btns = $$("#cv-audit .cv-sentence");
    if (btns[0] && findings.length) {
      btns[0].addEventListener("click", () => openAssignment(findings[0].study_key, ""));
    }
    if (btns[1] && confStudies.length) {
      btns[1].addEventListener("click", () => openAssignment(confStudies[0].study_key, ""));
    } else if (btns[1]) {
      btns[1].disabled = true;
    }
    if (btns[2]) {
      btns[2].addEventListener("click", () => {
        const first = orderedStudies().find((s) => s.needs_na_verdict === 1);
        if (first) openAssignment(first.study_key, "S6cell");
        else toast("No study needs a verdict right now", "ok");
      });
    }

    // non-parity findings keep a compact expandable list
    const other = (a.findings || []).filter((f) => f.check !== "parity");
    if (other.length) {
      const wrap = document.createElement("details");
      wrap.className = "cv-findings";
      wrap.innerHTML = `<summary>${other.length} other finding(s) — click to review</summary>` +
        `<div class="cv-findlist">` + other.map((f) =>
          `<button type="button" class="cv-finding" data-key="${esc(f.study_key)}">` +
          `<b>${esc(f.study_key)}</b><span class="cv-findchk">${esc(f.check)}</span>` +
          `<span class="cv-finddet">${esc(f.detail || "")}</span></button>`).join("") +
        `</div>`;
      host.appendChild(wrap);
      $$(".cv-finding", wrap).forEach((b) =>
        b.addEventListener("click", () => openAssignment(b.dataset.key, "")));
    }
  }

  // ------------------------------------------------------------ filters
  function renderFilters() {
    const host = $("#cv-filters");
    if (!host) return;
    const filters = state.data.filters || Object.keys(FILTER_LABEL);
    host.innerHTML = filters.map((f) =>
      `<button class="chip${f === state.filter ? " active" : ""}" data-cfilter="${esc(f)}" ` +
      `title="${esc(FILTER_HINT[f] || f)}">${esc(FILTER_LABEL[f] || f)}</button>`).join("");
    $$("#cv-filters .chip").forEach((c) => c.addEventListener("click", () => {
      state.filter = c.dataset.cfilter;
      load(true);
    }));
  }

  // ------------------------------------------------------- pool pills
  function poolPills(s) {
    if (!s.has_dataset) return `<span class="cv-tag">no public dataset — code it in the Coding tab</span>`;
    const pools = s.pools || {};
    const miss = (list) => (list && list.length
      ? ` Still missing: ${list.join("; ")}.` : "");
    const sdt = pools.sdt || {};
    const did = pools.did || {};
    const excl = pools.excl || {};
    const pills = [];
    pills.push(
      `<button type="button" class="cv-pool ${sdt.joins ? "ok" : "bad"}" data-pool="sdt" data-key="${esc(s.study_key)}" ` +
      `title="${esc((sdt.define || "The signal-detection (d') pool.") + (sdt.joins ? " This study joins it." : miss(sdt.missing)))}">` +
      (sdt.joins ? "✓ signal-detection pool" : "✗ signal-detection pool") + `</button>`);
    pills.push(
      `<button type="button" class="cv-pool ${did.joins ? "ok" : "bad"}" data-pool="did" data-key="${esc(s.study_key)}" ` +
      `title="${esc((did.define || "The pre-post (DiD) pool.") + (did.joins ? " This study joins it." : miss(did.missing)))}">` +
      (did.joins ? "✓ pre-post pool" : "✗ pre-post pool") + `</button>`);
    const exState = excl.state || "";
    const exCls = exState === "applied" ? "ok" : (exState === "rules-missing" ? "warn" : "na");
    pills.push(
      `<button type="button" class="cv-pool ${exCls}" data-pool="excl" data-key="${esc(s.study_key)}" ` +
      `title="${esc((excl.define || "The authors' own attention/comprehension checks.") + " " +
        (exState === "applied" ? "Drop rules are encoded (exclusions.csv)."
          : exState === "none" ? "This study has no author exclusion checks — nothing to encode."
          : "flag_* columns exist but no drop rules are encoded yet." + miss(excl.missing)))}">` +
      (exState === "applied" ? "author exclusions applied"
        : exState === "none" ? "no author exclusions"
        : "author exclusions apply") + `</button>`);
    if (s.needs_na_verdict === 1) {
      pills.push(
        `<button type="button" class="cv-pool warn" data-pool="na" data-key="${esc(s.study_key)}" ` +
        `title="This study's composite outcomes carry no true/false side. Confirm the study truly has no true/false split (an N/A verdict with a one-line reason) — confirming removes the study from the queue. Click to answer.">` +
        `confirm: no true/false split?</button>`);
    }
    return pills.join("");
  }

  // ------------------------------------------------------- slot chips
  function slotMeta() {
    const byCode = {};
    (state.data.slots || []).forEach((sl) => { byCode[sl.code] = sl; });
    return byCode;
  }

  function slotChip(study, slot, meta) {
    const status = study[slot] || "";
    const counts = (study.slot_counts || {})[slot] || {};
    const tot = Object.values(counts).reduce((a, b) => a + (b || 0), 0);
    const res = (counts.filled || 0) + (counts.confirmed || 0) + (counts.na || 0);
    const miss = study[slot + "_missing"] || 0;
    const m = meta[slot] || { short: slot, question: "", hint: "" };
    const [cls, stateLabel] = CHIP_STATE[status] || CHIP_STATE[""];
    let cnt = "";
    if (tot > 1) cnt = `${res}/${tot}`;
    else if (status === "na") cnt = "n/a";
    else cnt = status === "filled" || status === "confirmed" ? "✓" : "–";
    // the S-code lives in the tooltip only: the chip shows the plain question
    const title = `${slot} · ${m.question || m.short} — ${stateLabel}` +
      (miss ? ` (${miss} to answer)` : "") +
      (tot > 1 ? ` · ${res}/${tot} answered` : "") +
      (m.hint ? `\n${m.hint}` : "");
    return `<button type="button" class="cv-slot ${cls}" data-key="${esc(study.study_key)}" ` +
      `data-slot="${esc(slot)}" title="${esc(title)}">` +
      `<span class="cv-slot-s">${esc(m.short || slot)}</span>` +
      `<span class="cv-slot-c">${esc(cnt)}</span></button>`;
  }

  // ------------------------------------------------------------ table
  function renderTable() {
    const host = $("#cv-table");
    if (!host) return;
    const meta = slotMeta();
    const studies = orderedStudies();
    if (!studies.length) {
      host.innerHTML = `<div class="empty"><b>No studies match</b><p>Try another filter or search.</p></div>`;
      return;
    }
    const sortBtn = (key, label, cls = "") => {
      const active = state.sort === key ? " active" : "";
      const arrow = state.sort === key ? (state.dir === "desc" ? " ↓" : " ↑") : "";
      return `<button class="cv-th${cls}${active}" data-sort="${esc(key)}" ` +
        `title="${esc(key === "missingness_score"
          ? "The coverage pipeline's poolability-weighted missingness score: how much still blocks the pools. Ascending = the most pools already unblocked first."
          : "Alphabetical study order.")}">${esc(label)}${arrow}</button>`;
    };
    const slotCodes = (state.data.slots || []).map((s) => s.code);
    const head = `<div class="cv-row cv-head">` +
      `<span class="cv-study">${sortBtn("study_key", "Study")}</span>` +
      `<span class="cv-score" title="How many questions this study still has to answer (the coverage pipeline's weighted score in the tooltip)">${sortBtn("missingness_score", "Gaps")}</span>` +
      `<span class="cv-flags" title="Which pooled analyses this study can join today - hover each pill for the definition and what is still missing">Pools</span>` +
      `<span class="cv-slots" title="The eleven harmonization questions - hover a chip for the question and its state">` +
      slotCodes.map((s) => `<span class="cv-slothead" title="${esc((meta[s] || {}).question || s)}">${esc((meta[s] || {}).short || s)}</span>`).join("") +
      `</span><span class="cv-open"></span></div>`;
    const rows = studies.map((s) => {
      const score = s.missingness_score == null ? "–" : s.missingness_score;
      const nOpen = s.open_count != null ? s.open_count : (s.open_slots || []).length;
      const first = (s.first_open_slot || {}).slot || "";
      const openBtn = !s.has_dataset
        ? `<button class="btn ghost cv-openbtn" data-key="${esc(s.study_key)}" data-nodata="1" ` +
          `title="No public dataset for this study - open it in the Coding tab instead">Coding tab</button>`
        : `<button class="btn ghost cv-openbtn" data-key="${esc(s.study_key)}" data-slot="${esc(first)}" ` +
          `title="${nOpen ? `Answer the ${nOpen} open question${nOpen === 1 ? "" : "s"} one at a time` : "Nothing to label - every question is answered"}">` +
          `${nOpen ? `Answer ${nOpen}` : "done"}</button>`;
      return `<div class="cv-row${s.has_dataset ? "" : " nodata"}">` +
        `<span class="cv-study"><button class="cv-link" data-key="${esc(s.study_key)}" data-slot="${esc(first)}" ` +
        `title="Open the guided gap queue for this study">${esc(s.study_key)}</button>` +
        (s.has_dataset ? "" : ' <span class="cv-tag">paper only</span>') + `</span>` +
        `<span class="cv-score" title="poolability-weighted missingness score ${esc(score)}">${esc(nOpen)}</span>` +
        `<span class="cv-flags">${poolPills(s)}</span>` +
        `<span class="cv-slots">${slotCodes.map((sl) => slotChip(s, sl, meta)).join("")}</span>` +
        `<span class="cv-open">${openBtn}</span>` +
        `</div>`;
    }).join("");
    host.innerHTML = head + rows;

    $$("#cv-table [data-sort]").forEach((b) => b.addEventListener("click", () => {
      const key = b.dataset.sort;
      if (state.sort === key) {
        state.dir = state.dir === "desc" ? "asc" : "desc";  // second click flips
      } else {
        state.sort = key;
        // "Gaps" is a work queue: fewest first; study names read A->Z
        state.dir = key === "missingness_score" ? "asc" : "asc";
      }
      load(true);
    }));
    $$("#cv-table .cv-link, #cv-table .cv-openbtn[data-slot]").forEach((b) =>
      b.addEventListener("click", () => openAssignment(b.dataset.key, b.dataset.slot)));
    $$("#cv-table .cv-openbtn[data-nodata]").forEach((b) =>
      b.addEventListener("click", () => {
        window.dispatchEvent(new CustomEvent("cs:open-paper", { detail: { key: b.dataset.key } }));
      }));
    $$("#cv-table .cv-slot").forEach((b) =>
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        openAssignment(b.dataset.key, b.dataset.slot);
      }));
    $$("#cv-table .cv-pool").forEach((b) =>
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        openAssignment(b.dataset.key, b.dataset.pool === "na" ? "S6cell" : "");
      }));
  }

  // ------------------------------------------------------------ render
  function render() {
    if (!state.data) return;
    renderReadiness();
    renderAudit();
    renderFilters();
    renderTable();
    const note = $("#cv-note");
    if (note) {
      const c = state.data.counts || {};
      note.textContent =
        `${c.total ?? 0} studies · queue ordered most-pools-unblocked first ` +
        `(coverage pipeline missingness score, ascending; finished studies at the end) · ` +
        `coverage generated ${state.data.generated_at || "–"} · click a study to answer its open questions one at a time`;
    }
  }

  function openAssignment(key, slot) {
    if (!key) return;
    // The workspace lives inside this view — open it inline and keep the
    // table (filter/sort/scroll state) exactly as it is.
    if (window.CSAssign) window.CSAssign.open(key, slot || "");
  }

  // ------------------------------------------------------------ wiring
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
