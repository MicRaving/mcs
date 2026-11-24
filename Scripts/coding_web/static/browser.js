/* Browser mode (WP5 of PLAN_online_only): the studio served through the web
   gateway under /app/p/<pid>/.  When GET api/online/status says "browser": true
   this hides the controls that only make sense on the desktop (the project
   folder picker, the connect/close online dialog, the pipeline and reproducer
   runs, the scheme editor, the exe update) and adds a user menu with a log out.
   On the desktop (browser missing or false) nothing changes.

   Every URL here is relative, so it resolves against the page URL: "/" on the
   desktop, "/app/p/<pid>/" in the browser. */
(function () {
  "use strict";

  // the gateway's own logout route is outside the /app/p/<pid>/ prefix
  const LOGOUT_URL = window.CS_BROWSER_LOGOUT_URL || "/app/logout"; // abs-url-ok: gateway route outside the project prefix

  const DESKTOP_ONLY = [
    "#project-btn",            // project folder picker
    "#project-dialog",
    "#project-online",         // "Open online project" from the folder dialog
    "#online-close-project",   // "Close online project"
    "#home-open-btn",          // choose a folder on disk
    "#home-new-toggle",        // create a folder on disk
    "#home-new-form",
    "#an-full-top",            // run the analysis pipeline
    "#rp-runbar",              // generate / harmonize / reproduce
    "#rp-runlog",
    "[data-view=\"scheme\"]",  // scheme editor entry
    "#view-scheme",
    "[data-section=\"updates\"]", // exe update (Settings)
    "#st-panel-updates",
  ];

  function injectStyle() {
    const style = document.createElement("style");
    style.id = "cs-browser-style";
    const sel = DESKTOP_ONLY.map((s) => "body.cs-browser " + s).join(",\n");
    style.textContent =
      sel + " { display: none !important; }\n" +
      ".cs-user { position: relative; }\n" +
      ".cs-user-menu { position: absolute; right: 0; top: calc(100% + 4px); min-width: 200px;" +
      " padding: .5rem; z-index: 50; background: var(--card, #fff); color: var(--card-foreground, inherit);" +
      " border: 1px solid var(--border, #ccc); border-radius: var(--radius, 6px);" +
      " box-shadow: 0 8px 24px rgba(0, 0, 0, .18); }\n" +
      ".cs-user-menu[hidden] { display: none; }\n" +
      ".cs-user-menu .cs-user-name { display: block; padding: .25rem .5rem .5rem; opacity: .75; overflow-wrap: anywhere; }\n" +
      ".cs-user-menu .cs-user-logout { width: 100%; justify-content: flex-start; }\n";
    document.head.appendChild(style);
  }

  function buildUserMenu(name) {
    const wrap = document.createElement("div");
    wrap.className = "cs-user";

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn ghost cs-user-btn";
    btn.setAttribute("aria-haspopup", "menu");
    btn.setAttribute("aria-expanded", "false");
    btn.title = "Account";
    btn.textContent = name || "Account";

    const menu = document.createElement("div");
    menu.className = "cs-user-menu";
    menu.setAttribute("role", "menu");
    menu.hidden = true;

    const who = document.createElement("span");
    who.className = "cs-user-name";
    who.textContent = name || "";

    const logout = document.createElement("button");
    logout.type = "button";
    logout.className = "btn ghost cs-user-logout";
    logout.setAttribute("role", "menuitem");
    logout.textContent = "Log out";

    menu.append(who, logout);
    wrap.append(btn, menu);

    function setOpen(open) {
      menu.hidden = !open;
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    }
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      setOpen(menu.hidden);
    });
    document.addEventListener("click", (ev) => {
      if (!wrap.contains(ev.target)) setOpen(false);
    });
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") setOpen(false);
    });
    logout.addEventListener("click", async () => {
      logout.disabled = true;
      try {
        await fetch(LOGOUT_URL, { method: "POST", credentials: "same-origin" });
      } catch { /* the reload below shows the login page either way */ }
      location.reload();
    });
    return wrap;
  }

  async function init() {
    let st = null;
    try {
      const res = await fetch("api/online/status", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!res.ok) return;
      st = await res.json();
    } catch {
      return; // no gateway: the desktop app, nothing to change
    }
    if (!st || st.browser !== true) return;

    document.body.classList.add("cs-browser");
    injectStyle();
    const name = String(st.display_name || st.user || (st.me && st.me.name) || "");
    const bar = document.querySelector("header.topbar");
    if (bar) bar.insertBefore(buildUserMenu(name), document.getElementById("theme-btn") || null);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
