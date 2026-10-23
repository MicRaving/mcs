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

  /* ---------------------------------------------------------- markdown */
  /* One markdown renderer for the whole app: the paper notes and the Home
     instructions.  It covers what those documents use - headings, lists,
     tables, block quotes, fenced code, rules, inline code, emphasis and
     links - and it escapes the source first, so a document can never inject
     HTML into the page. */
  const escapeHtml = (s) => String(s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function inlineMd(s) {
    return s
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/__([^_]+)__/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/(^|[\s(])_([^_\n]+)_/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  }

  function markdown(src) {
    const lines = escapeHtml(String(src ?? "").replace(/\r\n?/g, "\n")).split("\n");
    const out = [];
    let i = 0;
    const isSep = (t) => /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(t) && t.includes("-");
    const cells = (t) => t.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());
    const isList = (t) => /^\s*([-*+]|\d+\.)\s+/.test(t);
    const isFence = (t) => /^\s*```/.test(t);
    const isSpecial = (t) => /^(#{1,6})\s+/.test(t) || isList(t) || isFence(t)
      || t.trimStart().startsWith(">") || t.trimStart().startsWith("|");

    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i += 1; continue; }
      let m;
      if (isFence(line)) {
        const buf = [];
        i += 1;
        while (i < lines.length && !isFence(lines[i])) { buf.push(lines[i]); i += 1; }
        i += 1;   // the closing fence
        out.push(`<pre><code>${buf.join("\n")}</code></pre>`);
        continue;
      }
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
        /* A list item runs to the next bullet, so an indented line under a
           bullet is the rest of that item, not a paragraph.  Indented bullets
           are a nested list. */
        const items = [];
        while (i < lines.length) {
          const t = lines[i];
          if (isList(t)) {
            items.push({
              text: t.replace(/^\s*([-*+]|\d+\.)\s+/, ""),
              indent: t.match(/^\s*/)[0].replace(/\t/g, "  ").length,
              ord: /^\s*\d+\./.test(t),
            });
            i += 1;
            continue;
          }
          if (t.trim() && /^\s{2,}\S/.test(t) && items.length) {
            items[items.length - 1].text += " " + t.trim();
            i += 1;
            continue;
          }
          if (!t.trim() && items.length) {
            const next = lines[i + 1] || "";
            if (isList(next) || /^\s{2,}\S/.test(next)) { i += 1; continue; }
          }
          break;
        }
        out.push(listHtml(items));
        continue;
      }
      const buf = [];
      while (i < lines.length && lines[i].trim() && !isSpecial(lines[i])) { buf.push(lines[i]); i += 1; }
      out.push(`<p>${inlineMd(buf.join(" "))}</p>`);
    }
    return out.join("\n");
  }

  /* one list tree, built from the flat items the collector produced */
  function listHtml(items) {
    let i = 0;
    const build = (indent) => {
      const tag = items[i].ord ? "ol" : "ul";
      let out = `<${tag}>`;
      while (i < items.length) {
        const it = items[i];
        if (it.indent < indent) break;
        if (it.indent > indent) { out = out.replace(/<\/li>$/, "") + build(it.indent) + "</li>"; continue; }
        out += `<li>${inlineMd(it.text)}`;
        i += 1;
        if (i < items.length && items[i].indent > indent) out += build(items[i].indent);
        out += "</li>";
      }
      return out + `</${tag}>`;
    };
    return items.length ? build(items[0].indent) : "";
  }

  window.CS = { $, $$, esc, svg, svgEl, api, toast, safeStore, safeGet, currentView, fitTopbar, optionText, optionTextOf, markdown };
})();
