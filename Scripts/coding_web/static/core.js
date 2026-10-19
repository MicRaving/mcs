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

  /* the active section tab ("coding" has no body class) */
  function currentView() {
    const active = document.querySelector(".toptabs .viewtab.active");
    return active ? active.dataset.view : "coding";
  }

  window.CS = { $, $$, esc, svg, svgEl, api, toast, safeStore, safeGet, currentView };
})();
