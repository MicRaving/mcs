/* ============================================================
   Shared in-PDF search: highlight every match and cycle through
   the specific locations.

   The server renders each PDF page to a PNG and returns search hits
   as fractions of the page box ("/api/pdfsearch/<key>?q=...").  This
   helper overlays absolutely-positioned highlight rectangles on the
   rendered pages, so it works at any zoom/fit and in dark mode.

   Usage:
     const find = PdfSearch.create({
       getWrap: () => document.querySelector("#viewer .pdf-scroll"),
       getInput/getCount/getPrev/getNext,
       fetch: (q) => api("/api/pdfsearch/<key>?q=" + encodeURIComponent(q)),
       ensurePage: async (page) => { ... },   // optional (paged viewers)
     });
     find.refresh();   // call after the viewer re-renders its pages
   ============================================================ */
(function () {
  "use strict";

  const instances = [];

  function create(opts) {
    const get = (fn, dflt) => (typeof fn === "function" ? fn() : dflt);
    let hits = [];
    let active = -1;
    let timer = null;
    let token = 0;
    const control = {};

    function wrap() {
      try { return get(opts.getWrap, null); } catch { return null; }
    }

    function clearLayer() {
      const w = wrap();
      if (!w) return;
      const old = w.querySelector(":scope > .pdf-hitlayer");
      if (old) old.remove();
    }

    function ensureLayer() {
      const w = wrap();
      if (!w) return null;
      let layer = w.querySelector(":scope > .pdf-hitlayer");
      if (!layer) {
        layer = document.createElement("div");
        layer.className = "pdf-hitlayer";
        w.appendChild(layer);
      }
      return layer;
    }

    function pageImage(page) {
      const w = wrap();
      if (!w) return null;
      return w.querySelector('.pdf-page[data-page="' + page + '"]');
    }

    function count() {
      const c = get(opts.getCount, null);
      if (!c) return;
      if (!hits.length) { c.textContent = "0"; c.classList.remove("has"); return; }
      c.textContent = (active + 1) + "/" + hits.length;
      c.classList.add("has");
    }

    function paint() {
      const w = wrap();
      if (!w) return;
      clearLayer();
      if (!hits.length) return;
      const layer = ensureLayer();
      if (!layer) return;
      const imgs = {};
      w.querySelectorAll(".pdf-page[data-page]").forEach((img) => { imgs[img.dataset.page] = img; });
      hits.forEach((h, i) => {
        const img = imgs[String(h.page)];
        if (!img || !img.offsetWidth || !img.offsetHeight) return;
        const r = h.rect || [];
        const x0 = r[0] || 0, y0 = r[1] || 0, x1 = r[2] || 0, y1 = r[3] || 0;
        const d = document.createElement("div");
        d.className = "pdf-hit" + (i === active ? " active" : "");
        d.dataset.i = String(i);
        d.style.left = (img.offsetLeft + x0 * img.offsetWidth) + "px";
        d.style.top = (img.offsetTop + y0 * img.offsetHeight) + "px";
        d.style.width = Math.max(3, (x1 - x0) * img.offsetWidth) + "px";
        d.style.height = Math.max(3, (y1 - y0) * img.offsetHeight) + "px";
        layer.appendChild(d);
      });
    }

    function scrollToActive(smooth) {
      const w = wrap();
      if (!w || active < 0) return;
      const el = w.querySelector('.pdf-hit[data-i="' + active + '"]');
      if (el) {
        el.scrollIntoView({ block: "center", inline: "center",
                            behavior: smooth ? "smooth" : "auto" });
        return;
      }
      const h = hits[active];
      const img = h && pageImage(h.page);
      if (img) img.scrollIntoView({ block: "start", behavior: smooth ? "smooth" : "auto" });
    }

    function reset() {
      hits = [];
      active = -1;
      clearLayer();
      count();
    }

    async function go(i, smooth) {
      if (!hits.length) return;
      active = ((i % hits.length) + hits.length) % hits.length;
      const h = hits[active];
      if (!pageImage(h.page) && typeof opts.ensurePage === "function") {
        try { await opts.ensurePage(h.page); } catch { /* ignore */ }
      }
      paint();
      count();
      scrollToActive(smooth);
    }

    async function search(q, smooth = true) {
      q = String(q || "").trim();
      if (q.length < 2) { reset(); return 0; }
      const my = ++token;
      try {
        const res = await opts.fetch(q);
        if (my !== token) return 0;
        hits = (res && res.hits) || [];
        active = hits.length ? 0 : -1;
      } catch {
        if (my !== token) return 0;
        hits = [];
        active = -1;
      }
      count();
      if (active >= 0) {
        const h = hits[0];
        if (!pageImage(h.page) && typeof opts.ensurePage === "function") {
          try { await opts.ensurePage(h.page); } catch { /* ignore */ }
        }
        paint();
        scrollToActive(smooth);
      } else {
        paint();
      }
      return hits.length;
    }

    async function run() {
      const input = get(opts.getInput, null);
      return search(input ? input.value : "");
    }

    function schedule() {
      clearTimeout(timer);
      timer = setTimeout(run, 220);
    }

    function refresh() { paint(); }

    // ---- wiring (idempotent: bind once per DOM element) --------------------
    const input = get(opts.getInput, null);
    const prev = get(opts.getPrev, null);
    const next = get(opts.getNext, null);
    if (input && !input.dataset.pdfbound) {
      input.dataset.pdfbound = "1";
      input.addEventListener("input", schedule);
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          if (hits.length) { if (e.shiftKey) go(active - 1, true); else go(active + 1, true); }
          else run();
        } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          // Up/Down walk through the matches (handy when the input still has
          // focus); prevent the default caret movement.
          e.preventDefault();
          if (hits.length) go(e.key === "ArrowDown" ? active + 1 : active - 1, true);
        } else if (e.key === "Escape") {
          input.value = "";
          reset();
        }
      });
    }
    if (prev && !prev.dataset.pdfbound) {
      prev.dataset.pdfbound = "1";
      prev.addEventListener("click", () => go(active - 1, true));
    }
    if (next && !next.dataset.pdfbound) {
      next.dataset.pdfbound = "1";
      next.addEventListener("click", () => go(active + 1, true));
    }
    control.refresh = refresh;
    control.reset = reset;
    control.run = run;
    control.search = search;
    control.schedule = schedule;
    control.next = () => go(active + 1, true);
    control.prev = () => go(active - 1, true);
    control.hits = () => hits;
    instances.push(control);
    return control;
  }

  /* Collapse the find input behind its icon.  Returns a control whose
     ``open(value)`` reveals the field (optionally pre-filling it) and whose
     ``reset()`` clears and re-hides it. */
  function bindToggle(prefix) {
    const toggle = document.getElementById(prefix + "-toggle");
    const field = document.getElementById(prefix + "-field");
    const count = document.getElementById(prefix + "-count");
    const prev = document.getElementById(prefix + "-prev");
    const next = document.getElementById(prefix + "-next");
    const input = document.getElementById(prefix);
    const setOpen = (open) => {
      if (!field) return;
      field.hidden = !open;
      if (count) count.hidden = !open;
      if (prev) prev.hidden = !open;
      if (next) next.hidden = !open;
      if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
    };
    if (toggle && !toggle.dataset.pdfbound) {
      toggle.dataset.pdfbound = "1";
      toggle.addEventListener("click", () => {
        const open = field && field.hidden;
        setOpen(open);
        if (open && input) input.focus();
      });
    }
    if (input && !input.dataset.pdftoggle) {
      input.dataset.pdftoggle = "1";
      input.addEventListener("keydown", (e) => {
        if (e.key === "Escape") setOpen(false);
      });
    }
    return {
      open: (value) => {
        setOpen(true);
        if (input) { if (value != null) input.value = value; input.focus(); }
      },
      setOpen,
      reset: () => { if (input) input.value = ""; setOpen(false); },
    };
  }

  window.PdfSearch = { create, bindToggle,
                       refreshAll: () => instances.forEach((f) => f.refresh()) };
})();
