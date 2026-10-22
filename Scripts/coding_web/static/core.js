/* ============================================================
   Coding Studio — shared runtime
   One place for the primitives every tab used to re-implement:
   DOM helpers, HTML escaping, the fetch wrapper, icons and toasts,
   plus the shared top-bar refresh dispatcher.

   Exposes `window.CS`; tab scripts alias what they use:
       const { $, $$, esc, api, toast } = window.CS;
   ============================================================ */
(function () {
  "use strict";

  const SVGNS = "http://www.w3.org/2000/svg";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);

  /* inline SVG icon as an HTML string (for templates) */
  const svg = (id, cls = "") =>
    `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" ` +
    `stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><use href="#${id}"/></svg>`;

  /* inline SVG icon as an element (for imperative DOM building) */
  function svgEl(id, size = 15) {
    const s = document.createElementNS(SVGNS, "svg");
    s.setAttribute("viewBox", "0 0 24 24");
    s.setAttribute("width", size);
    s.setAttribute("height", size);
    s.setAttribute("fill", "none");
    s.setAttribute("stroke", "currentColor");
    s.setAttribute("stroke-width", "1.7");
    s.setAttribute("stroke-linecap", "round");
    s.setAttribute("stroke-linejoin", "round");
    const u = document.createElementNS(SVGNS, "use");
    u.setAttribute("href", "#" + id);
    s.appendChild(u);
    return s;
  }

  async function api(path, opts) {
    const r = await fetch(path, opts);
    if (!r.ok) throw new Error(`${path} → HTTP ${r.status}`);
    return r.json();
  }

  /* one toast implementation for every tab (icon + optional sub-line) */
  function toast(msg, kind = "ok", sub = "") {
    const host = document.getElementById("toasts");
    if (!host) return;
    const t = document.createElement("div");
    t.className = "toast " + kind;
    t.appendChild(svgEl(kind === "err" ? "i-alert" : "i-check", 16));
    const wrap = document.createElement("div");
    const main = document.createElement("div");
    main.textContent = String(msg ?? "");
    wrap.appendChild(main);
    if (sub) {
      const s = document.createElement("div");
      s.className = "sub";
      s.textContent = String(sub);
      wrap.appendChild(s);
    }
    t.appendChild(wrap);
    host.appendChild(t);
    setTimeout(() => {
      t.classList.add("hide");
      setTimeout(() => t.remove(), 300);
    }, kind === "err" ? 5200 : 2600);
  }

  const safeStore = (k, v) => { try { localStorage.setItem(k, v); } catch { /* ignore */ } };
  const safeGet = (k) => { try { return localStorage.getItem(k); } catch { return null; } };

  /* ---------------------------------------------------------- option text */
  /* A scheme level carries a number AND a name: "1" and "Lab".  Wherever a
     level is shown the two are merged ("1 - Lab"), so a reader never has to
     guess whether the field wants the number or the word.  The rule lives
     here, once: the scheme editor and the coding form read the same helper,
     so the two can never disagree about what a level looks like.

       optionText(value, label)   - the merged text of one level
       optionTextOf(option)        - the same, for a scheme option
                                      ({value, label}) or a plain string */
  function optionText(value, label) {
    const v = value === null || value === undefined ? "" : String(value);
    const l = label === null || label === undefined ? "" : String(label);
    if (!v) return l;          // a level without a number is just its name
    if (v === l) return l;      // a plain string level is just its name
    return `${v} - ${l}`;
  }

  function optionTextOf(option) {
    if (option === null || option === undefined) return "";
    if (typeof option !== "object") return optionText(option, option);
    return optionText(option.value, option.label);
  }

  /* the active section tab ("coding" has no body class) */
  function currentView() {
    const active = document.querySelector(".toptabs .viewtab.active");
    return active ? active.dataset.view : "coding";
  }

  /* ---------------------------------------------------------- top bar fit */
  /* The top bar carries eight section tabs and the app's own buttons, and
     they do not fit on one row in a 1440px window.  Rather than let the
     buttons run off the right edge - which is what happened, and which is
     why the Scheme entry was invisible for a while - the LABELS go first:
     a button keeps its icon and its tooltip and drops its text.

     The order of sacrifice:
       1. the view-specific toggles (Coder mode, Fast resolve, Blind, Clear)
          - their icons are unambiguous and they only matter in one view;
       2. the tab labels, from the right, so the leftmost ones - and the tab
          in use - keep their names for as long as possible;
       3. Save and Next;
       4. the strip scrolls, as the last resort.

     So the section tabs keep their names for as long as the window allows,
     and the Scheme entry is never the first thing to lose its label. */
  let fitTimer = null;
  let lastFitWidth = -1;
  function doFitTopbar() {
    const bar = $(".topbar");
    const strip = $(".toptabs");
    const actions = $(".actions");
    if (!bar || !strip || !actions) return;
    const tabs = $$(".viewtab", strip);
    const toggles = $$("[data-compact]", actions);
    // Home is always an icon: it is the "go to the start" affordance, like the
    // project and theme buttons, and its label would eat the space the other
    // tabs need.
    const alwaysIcon = (t) => t.dataset.view === "home";
    const reset = () => {
      tabs.forEach((t) => {
        if (!alwaysIcon(t)) t.classList.remove("is-icon");
      });
      actions.classList.remove("is-icon");
      toggles.forEach((t) => t.classList.remove("is-icon"));
    };
    // what the strip may use: the bar, less its padding, the gap and the
    // action buttons as they are right now
    const room = () => bar.clientWidth - 32 - 14 - actions.offsetWidth;
    const fits = () => strip.scrollWidth <= room() + 1;

    reset();
    // 1. the view-specific toggles give up their labels
    toggles.forEach((t) => t.classList.add("is-icon"));
    // 2. the tab labels: ALL of them or NONE of them.  A strip with one tab
    //    labelled and the rest as icons reads as broken, so the strip either
    //    shows every name or it shows icons only.
    if (!fits()) {
      tabs.forEach((t) => t.classList.add("is-icon"));
    }
    // 3. Save, Next and the rest
    if (!fits()) {
      actions.classList.add("is-icon");
      tabs.forEach((t) => t.classList.add("is-icon"));
    }
    // the tab in use must be visible, even when the strip has to scroll
    const active = tabs.find((t) => t.classList.contains("active"));
    if (active && strip.scrollWidth > strip.clientWidth) {
      active.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }

  /* The measurement is done synchronously: requestAnimationFrame is throttled
     in a background tab, and the layout would then be wrong until the tab
     came to the front. */
  function fitTopbar() {
    doFitTopbar();
  }

  function debouncedFitTopbar() {
    if (fitTimer) clearTimeout(fitTimer);
    fitTimer = setTimeout(() => { fitTimer = null; doFitTopbar(); }, 40);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", fitTopbar);
  } else {
    fitTopbar();
  }
  window.addEventListener("resize", debouncedFitTopbar);
  if (window.ResizeObserver) {
    // only a change in WIDTH matters: the strip's room depends on it
    try {
      new ResizeObserver((entries) => {
        const width = Math.round(entries[0].contentRect.width);
        if (width === lastFitWidth) return;
        lastFitWidth = width;
        debouncedFitTopbar();
      }).observe(document.documentElement);
    } catch { /* the layout still works without it */ }
  }

  window.CS = { $, $$, esc, svg, svgEl, api, toast, safeStore, safeGet, currentView, fitTopbar, optionText, optionTextOf };
})();
