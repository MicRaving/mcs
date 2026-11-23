/* ============================================================
   Coding Studio — online projects (desktop client UI)

   Everything here is inert for a local folder project: the badge stays hidden,
   no polling happens, the coder field is the app's own picker and the shared
   editors take no locks.  What the layer adds in an online project:

     - the header badge + sync panel (GET /api/online/status every 5 s;
       GET /api/online/presence is the 15 s heartbeat),
     - "Open online project…" in the project dialog
       (POST /api/online/connect | /open | /close),
     - edit locks for the shared files of one study key
       (POST|DELETE /api/online/locks/<key>), renewed every 10 min,
     - the conflict list and its side-by-side resolve view
       (GET /api/online/conflicts, POST …/<id>/resolve),
     - the cache panel and "take papers offline"
       (GET|POST /api/online/cache, /cache/{pin,unpin,clear,preload}).

   Route shapes are those of Results/INTERFACES_online_projects.md §4.  The
   admin panel is a separate module (admin_online.js) that mounts into
   #online-admin-root when the header entry dispatches cs:online-admin-open.
   ============================================================ */
(() => {
  "use strict";

  const { $, $$, esc, toast } = window.CS;

  const STATUS_MS = 5000;        // badge + mode poll
  const PRESENCE_MS = 15000;     // heartbeat (the route itself is presence)
  const RENEW_MS = 10 * 60 * 1000;

  const S = {
    status: null,          // last GET /api/online/status payload
    active: false,
    mode: "local",
    others: [],            // presence rows of other members
    locks: [],             // GET /api/online/locks
    conflicts: [],         // GET /api/online/conflicts
    conflictCount: 0,
    conflictsLoaded: false,
    conflictsError: "",
    cache: null,           // GET /api/online/cache
    projects: [],          // connect result
    held: new Map(),       // key -> {task, timer, lock}
    blocked: new Map(),    // key -> holder object (someone else holds it)
    statusTimer: null,
    presenceTimer: null,
    lockPollTimer: null,
    lastPresence: "",
    routeMissing: false,
    booted: false,
    dialog: null,
  };

  /* ---------------------------------------------------------------- helpers */
  function h(tag, attrs, ...kids) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (v === null || v === undefined || v === false) return;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "html") node.innerHTML = v;
      else if (k === "dataset") Object.entries(v).forEach(([d, dv]) => { node.dataset[d] = dv; });
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : String(v));
    });
    (kids.flat ? kids.flat(Infinity) : kids).forEach((kid) => {
      if (kid === null || kid === undefined || kid === false) return;
      node.appendChild(typeof kid === "string" || typeof kid === "number"
        ? document.createTextNode(String(kid)) : kid);
    });
    return node;
  }

  const SVGNS = "http://www.w3.org/2000/svg";
  function icon(id, size = 14) {
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

  function when(iso) {
    if (!iso) return "never";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    const secs = Math.round((Date.now() - d.getTime()) / 1000);
    if (secs >= 0 && secs < 60) return "just now";
    if (secs >= 60 && secs < 3600) return `${Math.round(secs / 60)} min ago`;
    return d.toLocaleString();
  }

  const mb = (n) => `${Math.round(Number(n) || 0)} MB`;

  /* ---------------------------------------------------------------- transport */
  /* core.js's api() drops the response body, but this layer needs the error
     codes (locked + holder, offline, forbidden), so it has its own wrapper. */
  async function call(path, opts) {
    let res;
    try {
      res = await fetch(path, opts);
    } catch (err) {
      const e = new Error("The studio server cannot be reached");
      e.status = 0; e.code = "offline"; e.cause = err;
      throw e;
    }
    let data = null;
    try { data = await res.json(); } catch { data = null; }
    if (!res.ok) {
      const e = new Error((data && (data.message || data.error || data.detail))
        || `HTTP ${res.status}`);
      e.status = res.status;
      e.code = (data && (data.error || data.detail)) || "";
      e.holder = (data && data.holder) || null;
      e.data = data;
      throw e;
    }
    return data || {};
  }

  const GET = (p) => call(p);
  const POST = (p, body) => call(p, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const DELETE = (p) => call(p, { method: "DELETE" });

  /* ---------------------------------------------------------------- state read */
  function active() { return !!S.active; }
  function status() { return S.status; }
  function me() { return (S.status && S.status.me) || {}; }
  function isOwner() { return String(me().role || "") === "owner"; }
  function settings() { return (S.status && S.status.settings) || {}; }
  function myTags() { return Array.isArray(me().tags) ? me().tags : []; }

  /* ---------------------------------------------------------------- status */
  function applyStatus(st) {
    const wasActive = S.active;
    S.status = st || null;
    S.active = !!(st && st.active);
    S.mode = (st && st.mode) || (S.active ? "online" : "local");
    S.conflictCount = Number(st && st.conflicts) || 0;
    renderBadge();
    renderCoderField();
    renderAdminEntry();
    if (S.active && !wasActive) {
      startPresence();
      startLockPoll();
      if (window.CS.app) {
        window.CS.app.refreshDone({ force: true })
          .then(() => window.CS.app.renderPaperList())
          .catch(() => {});
      }
    }
    if (!S.active && wasActive) {
      stopPresence();
      stopLockPoll();
      releaseAll();
    }
    document.body.classList.toggle("cs-online-active", S.active);
    document.dispatchEvent(new CustomEvent("cs:online-status", { detail: S.status }));
  }

  async function refreshStatus() {
    if (S.routeMissing) return;
    try {
      const st = await GET("/api/online/status");
      applyStatus(st);
    } catch (err) {
      if (err.status === 404) {
        // this build has no online layer: stay a local app, stop asking
        S.routeMissing = true;
        stopTimers();
        applyStatus({ active: false, mode: "local" });
        return;
      }
      // an unreachable server is the offline mode, not a UI error
      if (S.status && S.active) {
        applyStatus(Object.assign({}, S.status, { mode: "offline" }));
      }
    }
  }

  function startStatus() {
    refreshStatus();
    if (!S.statusTimer) S.statusTimer = setInterval(refreshStatus, STATUS_MS);
  }

  function stopTimers() {
    if (S.statusTimer) { clearInterval(S.statusTimer); S.statusTimer = null; }
    stopPresence();
    stopLockPoll();
  }

  /* ---------------------------------------------------------------- badge */
  function renderBadge() {
    const badge = $("#online-badge");
    if (!badge) return;
    const text = $("#online-badge-text");
    const flag = $("#online-badge-flag");
    const st = S.status || {};
    if (!S.active) {
      badge.hidden = true;
      document.title = document.title.replace(/^\(\d+\+?\) /, "");
      return;
    }
    badge.hidden = false;
    badge.dataset.mode = S.mode;
    let label = "Online";
    if (S.mode === "syncing") label = "Syncing…";
    else if (S.mode === "offline") label = `offline — ${Number(st.pending) || 0} pending`;
    else if (S.mode === "error") label = "Sync error";
    if (text) text.textContent = label;
    if (flag) {
      const n = S.conflictCount;
      const pending = Number(st.pending) || 0;
      // the top bar drops the labels on a narrow window, so the count that
      // matters has to survive in the flag: conflicts first, then pending
      // changes while offline
      const showPending = !n && S.mode === "offline" && pending > 0;
      flag.hidden = !n && !showPending;
      flag.textContent = n ? String(n) : (showPending ? String(pending) : "");
      flag.dataset.kind = n ? "conflict" : "pending";
      flag.title = n ? `${n} conflict${n === 1 ? "" : "s"}`
        : (showPending ? `${pending} change${pending === 1 ? "" : "s"} pending` : "");
    }
    const bits = [];
    if (st.project_name) bits.push(st.project_name);
    if (st.server) bits.push(st.server);
    if (st.user) bits.push(st.user);
    bits.push(`last sync ${when(st.last_sync)}`);
    if (st.pending) bits.push(`${st.pending} change${st.pending === 1 ? "" : "s"} pending`);
    if (S.conflictCount) bits.push(`${S.conflictCount} conflict${S.conflictCount === 1 ? "" : "s"}`);
    if (st.last_error) bits.push(`last error: ${st.last_error}`);
    badge.title = bits.join(" · ") + "\nClick for sync, conflicts, presence, locks and cache.";
    document.title = (S.conflictCount || st.pending)
      ? `(${S.conflictCount || st.pending}${S.conflictCount ? "!" : ""}) ${document.title.replace(/^\(\d+\+?!?\) /, "")}`
      : document.title.replace(/^\(\d+\+?!?\) /, "");
  }

  /* ---------------------------------------------------------------- admin entry */
  function renderAdminEntry() {
    const btn = $("#online-admin-btn");
    if (!btn) return;
    btn.hidden = !(S.active && isOwner());
  }

  /* ---------------------------------------------------------------- coder tags */
  /* In an online project the coder is not free text: it is one of the member's
     tags (INTERFACES §4, "coder identity").  The owner keeps the free field —
     they may code under any tag.  The visible control is a <select> next to the
     app's own (hidden) input, so every place that reads
     $("#coder-input").value keeps working. */
  function renderCoderField() {
    const inp = $("#coder-input");
    const pick = $("#coder-pick");
    if (!inp || !pick) return;
    const tags = myTags();
    const free = !S.active || isOwner() || !tags.length;
    let sel = $("#online-coder-select");
    if (free) {
      pick.classList.remove("online-tags");
      if (sel) sel.remove();
      return;
    }
    if (!sel) {
      sel = h("select", { class: "input select online-coder", id: "online-coder-select",
        title: "Your coder tag in this project (one of your member tags)" });
      sel.addEventListener("change", () => {
        const v = sel.value;
        inp.value = v;
        if (window.CS.app && window.CS.app.setCoder) window.CS.app.setCoder(v);
      });
      pick.insertBefore(sel, inp);
    }
    pick.classList.add("online-tags");
    const cur = String(inp.value || "").trim();
    const value = tags.includes(cur) ? cur : tags[0];
    sel.innerHTML = tags.map((t) =>
      `<option value="${esc(t)}"${t === value ? " selected" : ""}>${esc(t)}</option>`).join("");
    const appState = window.CS.app ? window.CS.app.state() : {};
    if (cur !== value && !appState.dirty) {
      inp.value = value;
      if (window.CS.app && window.CS.app.setCoder) window.CS.app.setCoder(value);
    }
  }

  /* ---------------------------------------------------------------- presence */
  function sendPresence() {
    if (!S.active) return;
    const st = window.CS.app ? window.CS.app.state() : { key: "", view: "coding" };
    const key = st.key || "";
    const tab = (window.CS.currentView ? window.CS.currentView() : "coding");
    const sig = `${key}|${tab}`;
    if (sig === S.lastPresence) return;   // still inside the TTL window
    S.lastPresence = sig;
    GET(`/api/online/presence?key=${encodeURIComponent(key)}&tab=${encodeURIComponent(tab)}`)
      .then((d) => { S.others = (d && d.others) || []; renderPresenceIfOpen(); })
      .catch(() => { /* heartbeat failures are the offline mode's business */ });
  }

  function startPresence() {
    if (S.presenceTimer) return;
    S.lastPresence = "";
    sendPresence();
    S.presenceTimer = setInterval(() => { S.lastPresence = ""; sendPresence(); }, PRESENCE_MS);
  }

  function stopPresence() {
    if (S.presenceTimer) { clearInterval(S.presenceTimer); S.presenceTimer = null; }
  }

  /* ---------------------------------------------------------------- locks */
  async function fetchLocks() {
    if (!S.active) { S.locks = []; return S.locks; }
    try {
      const d = await GET("/api/online/locks");
      S.locks = (d && d.locks) || [];
    } catch { S.locks = S.locks || []; }
    return S.locks;
  }

  function startLockPoll() {
    if (S.lockPollTimer) return;
    fetchLocks().then(() => window.CS.app && window.CS.app.renderPaperList());
    S.lockPollTimer = setInterval(() => {
      fetchLocks().then(() => {
        if (window.CS.app) window.CS.app.renderPaperList();
        renderLocksIfOpen();
      });
    }, PRESENCE_MS);
  }

  function stopLockPoll() {
    if (S.lockPollTimer) { clearInterval(S.lockPollTimer); S.lockPollTimer = null; }
  }

  /* A lock held by somebody else, unexpired, on this key. */
  function holderFor(key) {
    if (!key || !S.active) return null;
    if (S.held.has(key)) return null;            // ours: never shown as foreign
    const mine = String((S.status && S.status.user) || "");
    const row = (S.locks || []).find((l) => l && l.key === key && !l.expired);
    if (!row) return null;
    if (row.user && mine && String(row.user) === mine) return null;
    if (row.display_name && mine && row.display_name === mine) return null;
    return row;
  }

  /* Acquire (or renew) the edit lock of one study key.
     Returns {ok, held, holder, error}.  Never throws: an editor must still open
     when the lock cannot be taken — it opens read-only instead. */
  async function acquire(key, task, extra) {
    if (!S.active || !key) return { ok: true, held: false };
    if (S.held.has(key)) { renew(key); return { ok: true, held: true, lock: S.held.get(key).lock }; }
    try {
      const body = { task: task || "", key };
      Object.assign(body, extra || {});
      const d = await POST(`/api/online/locks/${encodeURIComponent(key)}`, body);
      const lock = d && d.lock;
      const timer = setInterval(() => renew(key), RENEW_MS);
      S.held.set(key, { task, timer, lock });
      S.blocked.delete(key);
      return { ok: true, held: true, lock };
    } catch (err) {
      if (err.status === 409) {
        const holder = err.holder || (err.data && err.data.holder) || {};
        S.blocked.set(key, holder);
        return { ok: false, held: false, holder, error: err };
      }
      // 403/404/503: no lock layer for this key (or offline) — do not block
      return { ok: true, held: false, error: err };
    }
  }

  async function renew(key) {
    if (!S.active || !S.held.has(key)) return;
    try {
      await POST(`/api/online/locks/${encodeURIComponent(key)}`, { task: S.held.get(key).task || "" });
    } catch (err) {
      if (err.status === 409) {
        // someone force-released it and took it: stop renewing silently
        const entry = S.held.get(key);
        if (entry && entry.timer) clearInterval(entry.timer);
        S.held.delete(key);
        S.blocked.set(key, err.holder || {});
      }
    }
  }

  async function release(key) {
    const entry = S.held.get(key);
    if (entry && entry.timer) clearInterval(entry.timer);
    S.held.delete(key);
    S.blocked.delete(key);
    if (!S.active || !key) return;
    try { await DELETE(`/api/online/locks/${encodeURIComponent(key)}`); } catch { /* TTL covers it */ }
  }

  function releaseAll() {
    Array.from(S.held.keys()).forEach((k) => release(k));
    S.blocked.clear();
  }

  /* Read-only presentation of a lock held by someone else, inside an editor.
     `host` is where the note goes; `disable` is a selector of the editor's save
     controls.  Returns {ok, holder}; the caller keeps its own guard so a
     keyboard path cannot bypass the disabled buttons. */
  function notice(host, key, holder, disable) {
    const who = (holder && (holder.display_name || holder.user)) || "another coder";
    const until = holder && holder.expires
      ? String(holder.expires).replace("T", " ").slice(0, 16) : "";
    const text = `Locked by ${who}${until ? ` until ${until}` : ""} — this study is read-only for you.`;
    let note = host ? host.querySelector(":scope > .cs-lock-note") : null;
    if (!note && host) {
      note = h("p", { class: "cs-lock-note" }, icon("i-lock", 13), h("span", { text }));
      host.insertBefore(note, host.firstChild);
    } else if (note) {
      note.querySelector("span").textContent = text;
    }
    if (disable) $$(disable).forEach((n) => { n.disabled = true; });
    return { ok: false, holder: holder || {}, who };
  }

  function clearNotice(host, disable) {
    if (host) Array.from(host.querySelectorAll(":scope > .cs-lock-note")).forEach((n) => n.remove());
    if (disable) $$(disable).forEach((n) => { n.disabled = false; });
  }

  /* Begin an edit session of a shared-file key: acquire, and if somebody else
     holds it show the note and disable the save controls. */
  async function begin(key, { task, host, disable } = {}) {
    if (!S.active || !key) return { ok: true, held: false };
    const r = await acquire(key, task);
    if (!r.ok) return notice(host, key, r.holder, disable);
    clearNotice(host, disable);
    if (r.error) return { ok: true, held: false, error: r.error };
    return { ok: true, held: true, lock: r.lock };
  }

  function end(key, { host, disable } = {}) {
    clearNotice(host, disable);
    if (S.active && key) release(key);
  }

  const locks = {
    acquire, renew, release, releaseAll, begin, end, notice, clearNotice,
    holderFor, isHeld: (k) => S.held.has(k), list: () => S.locks, refresh: fetchLocks,
  };

  /* ---------------------------------------------------------------- dialogs */
  function ensureDialog(id, cls) {
    let d = document.getElementById(id);
    if (d) return d;
    d = h("dialog", { class: `sheet online-sheet ${cls || ""}`.trim(), id });
    document.body.appendChild(d);
    return d;
  }

  function openDialog(d) {
    if (!d) return;
    if (d.open) return;
    d.showModal();
  }

  /* ---------------------------------------------------------------- connect */
  function buildConnectDialog() {
    const d = ensureDialog("online-connect-dialog", "wide");
    d.innerHTML = "";
    const body = h("div", { class: "sheet-body" });
    const head = h("h3", { text: "Open online project" });
    const sub = h("p", { class: "online-sub" });
    body.append(head, sub);

    const current = h("div", { class: "online-current", id: "online-current" });
    body.appendChild(current);

    const form = h("form", { class: "online-form", id: "online-form", autocomplete: "off" });
    const field = (label, input) => h("label", { class: "online-field" }, h("span", { text: label }), input);
    const server = h("input", { class: "input", id: "online-server", placeholder: "https://studio.example.org", spellcheck: "false" });
    const user = h("input", { class: "input", id: "online-username", placeholder: "username", autocomplete: "username", spellcheck: "false" });
    const pass = h("input", { class: "input", id: "online-password", type: "password", placeholder: "password", autocomplete: "current-password" });
    form.append(
      field("Server", server),
      h("div", { class: "online-row" }, field("Username", user), field("Password", pass)),
    );
    const status = h("p", { class: "online-status", id: "online-connect-status" });
    const list = h("div", { class: "online-projects", id: "online-projects" });
    form.append(status, list);
    body.appendChild(form);

    const foot = h("div", { class: "sheet-foot" });
    const closeBtn = h("button", { class: "btn ghost", id: "online-connect-close", type: "button", text: "Close" });
    const offBtn = h("button", { class: "btn ghost danger", id: "online-close-project", type: "button", hidden: true, text: "Close online project" });
    const connectBtn = h("button", { class: "btn primary", id: "online-connect-btn", type: "submit", form: "online-form", text: "Connect" });
    foot.append(offBtn, h("span", { style: "flex:1" }), closeBtn, connectBtn);
    d.append(body, foot);

    closeBtn.addEventListener("click", () => d.close());
    offBtn.addEventListener("click", closeOnlineProject);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      await connect();
    });
    return d;
  }

  function renderConnectDialog() {
    const d = buildConnectDialog();
    const cur = $("#online-current", d);
    const form = $("#online-form", d);
    const offBtn = $("#online-close-project", d);
    const connectBtn = $("#online-connect-btn", d);
    const sub = d.querySelector(".online-sub");
    const st = S.status || {};
    if (S.active) {
      sub.textContent = "You are in an online project. Opening another one or closing this one switches the app's root.";
      form.hidden = true;
      offBtn.hidden = false;
      if (connectBtn) connectBtn.hidden = true;
      cur.hidden = false;
      cur.innerHTML = "";
      cur.append(
        h("div", { class: "online-card" },
          h("b", { text: st.project_name || "Online project" }),
          h("div", { class: "online-kv" },
            h("span", { text: "Server" }), h("b", { text: st.server || "—" }),
            h("span", { text: "User" }), h("b", { text: st.user || "—" }),
            h("span", { text: "Role" }), h("b", { text: (me().role || "—") }),
            h("span", { text: "Mode" }), h("b", { text: S.mode })),
        ),
      );
      return d;
    }
    sub.textContent = "Sign in to a studio server and open one of its projects. The project is mirrored into the app's cache; the password is used for this connection only and is never stored.";
    form.hidden = false;
    offBtn.hidden = true;
    if (connectBtn) connectBtn.hidden = false;
    cur.hidden = true;
    return d;
  }

  async function connect() {
    const d = $("#online-connect-dialog");
    const st = $("#online-connect-status", d);
    const server = ($("#online-server", d).value || "").trim();
    const username = ($("#online-username", d).value || "").trim();
    const password = $("#online-password", d).value || "";
    if (!server || !username || !password) {
      if (st) { st.textContent = "Server, username and password are all needed."; st.className = "online-status err"; }
      return;
    }
    if (st) { st.textContent = "Connecting…"; st.className = "online-status"; }
    try {
      const r = await POST("/api/online/connect", { server, username, password });
      $("#online-password", d).value = "";        // never kept, not even in the DOM
      S.projects = (r && r.projects) || [];
      renderProjectList();
      if (st) {
        st.textContent = S.projects.length
          ? `${S.projects.length} project${S.projects.length === 1 ? "" : "s"} available.`
          : "This account is not a member of any project.";
        st.className = "online-status";
      }
    } catch (err) {
      if (st) {
        st.textContent = err.status === 401 ? "Wrong username or password."
          : err.status === 0 ? "The server cannot be reached."
          : String(err.message || err);
        st.className = "online-status err";
      }
    }
  }

  function renderProjectList() {
    const host = $("#online-projects");
    if (!host) return;
    host.innerHTML = "";
    if (!S.projects.length) return;
    S.projects.forEach((p) => {
      const row = h("div", { class: "online-project-row" },
        h("div", { class: "online-project-name" },
          h("b", { text: p.name || p.id }),
          h("span", { class: "online-role", text: p.role || "" })),
        h("button", {
          class: "btn primary", type: "button", text: "Open",
          onclick: () => openProject(p.id, p.name),
        }));
      host.appendChild(row);
    });
  }

  async function openProject(projectId, name) {
    const d = $("#online-connect-dialog");
    const st = $("#online-connect-status", d);
    if (st) { st.textContent = `Opening ${name || projectId}…`; st.className = "online-status"; }
    try {
      const r = await POST("/api/online/open", { project_id: projectId });
      d.close();
      await refreshStatus();
      if (window.CS.app && window.CS.app.applyRootSwitch) {
        await window.CS.app.applyRootSwitch({ root: (r && r.root) || name || "online project" });
      }
      toast("Online project opened", "ok", `${name || projectId} · ${(r && r.root) || ""}`);
    } catch (err) {
      if (st) {
        st.textContent = err.status === 503 ? "The project is not available offline yet — connect once while online."
          : String(err.message || err);
        st.className = "online-status err";
      }
    }
  }

  async function closeOnlineProject() {
    try {
      const r = await POST("/api/online/close", {});
      const d = $("#online-connect-dialog");
      if (d) d.close();
      releaseAll();
      await refreshStatus();
      if (window.CS.app && window.CS.app.applyRootSwitch) {
        await window.CS.app.applyRootSwitch({ root: (r && r.root) || "local project" });
      }
      toast("Back to the local project", "ok", (r && r.root) || "");
    } catch (err) {
      toast("Could not close the online project", "err", String(err.message || err));
    }
  }

  function openConnectDialog() {
    renderConnectDialog();
    const d = $("#online-connect-dialog");
    openDialog(d);
    const s = $("#online-server");
    if (s) setTimeout(() => s.focus(), 30);
  }

  /* ---------------------------------------------------------------- sync panel */
  function buildSyncDialog() {
    const d = ensureDialog("online-sync-dialog", "wide online-sync");
    d.innerHTML = "";
    const body = h("div", { class: "sheet-body" });
    const head = h("header", { class: "online-head" },
      h("div", {},
        h("h3", { text: "Online project" }),
        h("p", { id: "online-sync-sub", class: "online-sub", text: "" })),
      h("span", { class: "online-grow" }),
      h("button", { class: "btn primary", id: "online-sync-now", type: "button", text: "Sync now" }),
    );
    body.appendChild(head);

    const grid = h("div", { class: "online-grid" });
    grid.append(
      h("section", { class: "online-card", id: "online-card-state" }),
      h("section", { class: "online-card", id: "online-card-cache" }),
    );
    body.append(grid,
      h("section", { class: "online-card", id: "online-card-conflicts" }),
      h("section", { class: "online-card", id: "online-card-presence" }),
      h("section", { class: "online-card", id: "online-card-locks" }),
      h("section", { class: "online-card", id: "online-card-offline" }),
    );

    const foot = h("div", { class: "sheet-foot" },
      h("button", { class: "btn ghost", id: "online-sync-close", type: "button", text: "Close" }));
    d.append(body, foot);

    $("#online-sync-close", d).addEventListener("click", () => d.close());
    $("#online-sync-now", d).addEventListener("click", syncNow);
    return d;
  }

  async function syncNow() {
    const btn = $("#online-sync-now");
    if (btn) { btn.disabled = true; btn.textContent = "Syncing…"; }
    try {
      const r = await POST("/api/online/sync", {});
      await refreshStatus();
      await loadPanelData();
      const mode = S.mode;
      if (mode === "offline") toast("Still offline", "err", "The server cannot be reached; changes stay queued.");
      else toast("Synced", "ok", r && r.pushed != null ? `${r.pushed} pushed` : "");
    } catch (err) {
      toast("Sync failed", "err", String(err.message || err));
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "Sync now"; }
    }
  }

  async function loadPanelData() {
    if (!S.active) return;
    S.lastPresence = "";        // the panel always asks for a fresh heartbeat
    const jobs = [
      GET("/api/online/conflicts").then((d) => {
        S.conflicts = (d && d.conflicts) || [];
        S.conflictsLoaded = true;
        S.conflictsError = "";
      }).catch((err) => {
        S.conflictsLoaded = false;
        S.conflictsError = String(err.message || err);
      }),
      GET("/api/online/locks").then((d) => { S.locks = (d && d.locks) || []; }).catch(() => {}),
      GET("/api/online/cache").then((d) => { S.cache = d || null; }).catch(() => {}),
      Promise.resolve(sendPresence()),
    ];
    await Promise.all(jobs);
    renderStateCard();
    renderCacheCard();
    renderConflictsCard();
    renderPresenceIfOpen();
    renderLocksIfOpen();
    renderOfflineCard();
  }

  function card(id, title, ...kids) {
    const host = $("#" + id);
    if (!host) return null;
    host.innerHTML = "";
    host.appendChild(h("h4", { text: title }));
    kids.forEach((k) => k && host.appendChild(k));
    return host;
  }

  function kv(pairs) {
    const box = h("div", { class: "online-kv" });
    pairs.forEach(([k, v]) => {
      box.append(h("span", { text: k }), h("b", { text: v === null || v === undefined || v === "" ? "—" : String(v) }));
    });
    return box;
  }

  function renderStateCard() {
    const st = S.status || {};
    const box = card("online-card-state", "Connection",
      kv([
        ["Project", st.project_name],
        ["Server", st.server],
        ["User", st.user],
        ["Role", me().role],
        ["Mode", S.mode + (S.mode === "offline" && st.pending ? ` — ${st.pending} pending` : "")],
        ["Last sync", when(st.last_sync)],
        ["Changes pending", st.pending],
        ["Conflicts", S.conflictCount],
        ["Head seq", st.seq],
        ["Next retry", st.next_retry_in != null ? `${Math.round(Number(st.next_retry_in))} s` : ""],
      ]));
    if (!box) return;
    if (st.last_error) {
      box.appendChild(h("p", { class: "online-warn", text: String(st.last_error) }));
    }
    box.appendChild(h("div", { class: "online-actions" },
      h("button", { class: "btn", type: "button", text: "Open online project…",
        onclick: () => { const d = $("#online-sync-dialog"); if (d && d.open) d.close(); openConnectDialog(); } }),
      h("button", { class: "btn ghost danger", type: "button", text: "Close online project",
        onclick: () => { const d = $("#online-sync-dialog"); if (d && d.open) d.close(); closeOnlineProject(); } })));
  }

  function renderCacheCard() {
    const c = S.cache || {};
    const limit = Number(c.limit_mb) || 1000;
    const used = Number(c.used_mb) || 0;
    const pinned = Array.isArray(c.pinned) ? c.pinned : [];
    const save = async (patch) => {
      try {
        S.cache = await POST("/api/online/cache", patch);
        renderCacheCard();
      } catch (err) { toast("Cache setting not saved", "err", String(err.message || err)); }
    };
    const range = h("input", { type: "range", min: "200", max: "20000", step: "100", value: String(limit), class: "online-range" });
    const num = h("input", { type: "number", min: "200", max: "20000", step: "100", value: String(limit), class: "input online-num" });
    range.addEventListener("input", () => { num.value = range.value; });
    range.addEventListener("change", () => save({ limit_mb: Number(range.value) }));
    num.addEventListener("change", () => save({ limit_mb: Number(num.value) || 1000 }));
    const supp = h("input", { type: "checkbox", class: "online-check", checked: !!c.supplements });
    supp.addEventListener("change", () => save({ supplements: supp.checked }));

    const bar = h("div", { class: "online-bar" },
      h("i", { style: `width:${limit ? Math.min(100, Math.round(used / limit * 100)) : 0}%` }));

    const box = card("online-card-cache", "Cache",
      kv([["Used", mb(used)], ["Limit", `${limit} MB`], ["Pinned keys", pinned.length]]),
      bar,
      h("label", { class: "online-field inline" }, h("span", { text: "Limit" }), range, num, h("span", { class: "online-muted", text: "MB" })),
      h("label", { class: "online-field inline" }, h("span", { text: "Supplements" }), supp,
        h("span", { class: "online-muted", text: "download supplements too" })),
      h("div", { class: "online-actions" },
        h("button", { class: "btn", type: "button", text: "Pre-load my assignments", onclick: preloadMine }),
        h("button", { class: "btn ghost danger", type: "button", text: "Clear cache", onclick: clearCache })));
    if (!box) return;
    if (pinned.length) {
      const chips = h("div", { class: "online-chips" });
      pinned.forEach((k) => {
        const chip = h("span", { class: "online-chip" }, h("span", { text: k }));
        chip.appendChild(h("button", {
          class: "online-chip-x", type: "button", title: `Unpin ${k}`, text: "×",
          onclick: () => unpin([k]),
        }));
        chips.appendChild(chip);
      });
      box.appendChild(chips);
    } else {
      box.appendChild(h("p", { class: "online-muted", text: "Nothing pinned yet." }));
    }
  }

  async function myAssignmentKeys() {
    try {
      const d = await GET("/api/online/assignments?stage=coding");
      const my = new Set(myTags().map((t) => String(t).toLowerCase()));
      const rows = (d && d.assignments) || [];
      const keys = rows
        .filter((r) => r && r.key && (!my.size || !r.tag || my.has(String(r.tag).toLowerCase())))
        .map((r) => r.key);
      return Array.from(new Set(keys));
    } catch { return []; }
  }

  async function preloadMine() {
    try {
      const keys = await myAssignmentKeys();
      await POST("/api/online/cache/preload", { keys });
      S.cache = await GET("/api/online/cache").catch(() => S.cache);
      renderCacheCard();
      toast("Pre-loading your assignments", "ok", keys.length ? `${keys.length} papers` : "nothing assigned");
    } catch (err) {
      toast("Pre-load failed", "err", String(err.message || err));
    }
  }

  async function unpin(keys) {
    try {
      await POST("/api/online/cache/unpin", { keys });
      S.cache = await GET("/api/online/cache").catch(() => S.cache);
      renderCacheCard();
    } catch (err) { toast("Unpin failed", "err", String(err.message || err)); }
  }

  async function clearCache() {
    try {
      await POST("/api/online/cache/clear", {});
      S.cache = await GET("/api/online/cache").catch(() => S.cache);
      renderCacheCard();
      toast("Cache cleared", "ok", "Pinned papers and your core files stay.");
    } catch (err) { toast("Could not clear the cache", "err", String(err.message || err)); }
  }

  function renderConflictsCard() {
    const rows = S.conflicts || [];
    const total = S.conflictCount || (S.conflictsLoaded ? rows.length : 0);
    const box = card("online-card-conflicts", `Conflicts (${total})`);
    if (!box) return;
    if (!S.conflictsLoaded) {
      box.appendChild(h("p", { class: "online-muted",
        text: S.conflictsError ? `The conflict list could not be loaded: ${S.conflictsError}`
          : "Loading conflicts…" }));
      return;
    }
    if (!rows.length) {
      box.appendChild(h("p", { class: "online-muted", text: "No conflicts — your edits merged cleanly." }));
      return;
    }
    const list = h("div", { class: "online-list" });
    rows.forEach((c) => {
      list.appendChild(h("button", {
        class: "online-row-item", type: "button",
        onclick: () => openConflict(c.id),
      },
        icon("i-alert", 13),
        h("span", { class: "online-grow", text: c.path || `conflict ${c.id}` }),
        h("span", { class: "online-muted", text: when(c.ts) }),
      ));
    });
    box.appendChild(list);
  }

  function renderPresenceIfOpen() {
    if (!document.getElementById("online-card-presence")) return;
    const others = S.others || [];
    const box = $("#online-card-presence");
    if (!box || !S.active) return;
    box.innerHTML = "";
    box.appendChild(h("h4", { text: `Who is working on what (${others.length})` }));
    if (!others.length) {
      box.appendChild(h("p", { class: "online-muted", text: "Nobody else is in this project right now." }));
      return;
    }
    const list = h("div", { class: "online-list" });
    others.forEach((o) => {
      list.appendChild(h("div", { class: "online-row-item static" },
        h("span", { class: "online-dot-sm" }),
        h("span", { class: "online-grow" },
          h("b", { text: o.display_name || o.user || "?" }),
          h("span", { class: "online-muted", text: o.key ? ` · ${o.key}` : "" }),
          h("span", { class: "online-muted", text: o.tab ? ` · ${o.tab}` : "" })),
        h("span", { class: "online-muted", text: when(o.ts) }),
      ));
    });
    box.appendChild(list);
  }

  function renderLocksIfOpen() {
    if (!document.getElementById("online-card-locks")) return;
    if (!S.active) return;
    const rows = S.locks || [];
    const box = $("#online-card-locks");
    box.innerHTML = "";
    box.appendChild(h("h4", { text: `Locks (${rows.length})` }));
    if (!rows.length) {
      box.appendChild(h("p", { class: "online-muted", text: "No study is locked." }));
      return;
    }
    const list = h("div", { class: "online-list" });
    rows.forEach((l) => {
      const who = l.display_name || l.user || "?";
      const until = l.expires ? String(l.expires).replace("T", " ").slice(0, 16) : "";
      const row = h("div", { class: "online-row-item static" },
        icon("i-lock", 13),
        h("span", { class: "online-grow" },
          h("b", { text: l.key }),
          h("span", { class: "online-muted", text: ` · ${who}` }),
          h("span", { class: "online-muted", text: until ? ` · until ${until}` : "" }),
          h("span", { class: "online-muted", text: l.offline ? " · offline lease" : "" }),
          h("span", { class: "online-muted", text: l.expired ? " · expired" : "" })),
      );
      if (isOwner()) {
        row.appendChild(h("button", {
          class: "btn ghost danger", type: "button", text: "Force release",
          onclick: () => forceRelease(l.key),
        }));
      }
      list.appendChild(row);
    });
    box.appendChild(list);
  }

  async function forceRelease(key) {
    try {
      await DELETE(`/api/online/locks/${encodeURIComponent(key)}?force=1`);
      await fetchLocks();
      renderLocksIfOpen();
      if (window.CS.app) window.CS.app.renderPaperList();
      toast("Lock released", "ok", key);
    } catch (err) {
      toast("Could not release the lock", "err", String(err.message || err));
    }
  }

  function renderOfflineCard() {
    const box = $("#online-card-offline");
    if (!box) return;
    box.innerHTML = "";
    box.appendChild(h("h4", { text: "Take papers offline" }));
    box.appendChild(h("p", { class: "online-muted",
      text: "Choose papers from your assignments, then take an offline lease (a lock that survives without the server) and pin their files — so you can keep coding on a train." }));

    const maxH = Number(settings().max_offline_lease_h) || 72;
    const hours = h("input", { class: "input online-num", id: "online-offline-hours", type: "number", min: "1", max: String(maxH), value: "72" });
    const list = h("div", { class: "online-list online-scroll", id: "online-offline-list" }, h("p", { class: "online-muted", text: "Loading your assignments…" }));
    const go = h("button", { class: "btn primary", type: "button", text: "Take offline" });
    box.append(
      h("label", { class: "online-field inline" }, h("span", { text: "Lease" }), hours,
        h("span", { class: "online-muted", text: `hours (max ${maxH})` })),
      list,
      h("div", { class: "online-actions" }, go));

    go.addEventListener("click", () => takeOffline(list, hours));

    // Fill the key list from the coder's assignments.
    GET("/api/online/assignments?stage=coding").then((d) => {
      const my = new Set(myTags().map((t) => String(t).toLowerCase()));
      const rows = ((d && d.assignments) || [])
        .filter((r) => r && r.key && (!my.size || !r.tag || my.has(String(r.tag).toLowerCase())));
      const keys = Array.from(new Set(rows.map((r) => r.key)));
      list.innerHTML = "";
      if (!keys.length) {
        list.appendChild(h("p", { class: "online-muted", text: "No assigned papers — nothing to take offline." }));
        return;
      }
      keys.forEach((k) => {
        const cb = h("input", { type: "checkbox", class: "online-check", value: k });
        list.appendChild(h("label", { class: "online-check-row" }, cb, h("span", { text: k })));
      });
    }).catch(() => {
      list.innerHTML = "";
      list.appendChild(h("p", { class: "online-muted", text: "Assignments are not available." }));
    });
  }

  async function takeOffline(list, hoursEl) {
    const keys = $$("input[type=checkbox]:checked", list).map((c) => c.value);
    if (!keys.length) { toast("Pick at least one paper", "err"); return; }
    const maxH = Number(settings().max_offline_lease_h) || 72;
    const hours = Math.min(maxH, Math.max(1, Number(hoursEl && hoursEl.value) || 72));
    let leased = 0;
    for (const key of keys) {
      try {
        await POST(`/api/online/locks/${encodeURIComponent(key)}`, { offline_hours: hours });
        leased += 1;
      } catch (err) {
        // 409: somebody else holds it — the pin below is still worth doing
        if (err.status !== 409) continue;
      }
    }
    try {
      await POST("/api/online/cache/pin", { keys });
      S.cache = await GET("/api/online/cache").catch(() => S.cache);
      renderCacheCard();
    } catch (err) {
      toast("Pinning failed", "err", String(err.message || err));
    }
    await fetchLocks();
    renderLocksIfOpen();
    toast("Taken offline", "ok", `${keys.length} pinned · ${leased} leased for ${hours} h`);
  }

  function openSyncPanel() {
    if (!S.active) { openConnectDialog(); return; }
    buildSyncDialog();
    const sub = $("#online-sync-sub");
    if (sub) {
      sub.textContent = [S.status && S.status.project_name, S.status && S.status.server,
        S.status && S.status.user].filter(Boolean).join(" · ");
    }
    renderStateCard();
    renderCacheCard();
    renderOfflineCard();
    loadPanelData();
    openDialog($("#online-sync-dialog"));
  }

  /* ---------------------------------------------------------------- conflicts */
  function buildConflictDialog() {
    const d = ensureDialog("online-conflict-dialog", "wide online-conflict");
    d.innerHTML = "";
    const body = h("div", { class: "sheet-body" });
    body.appendChild(h("header", { class: "online-head" },
      h("div", {},
        h("h3", { text: "Conflict" }),
        h("p", { class: "online-sub", id: "online-conflict-path", text: "" })),
      h("span", { class: "online-grow" })));
    body.appendChild(h("div", { class: "online-compare", id: "online-compare" }));
    body.appendChild(h("div", { class: "online-merged", id: "online-merged", hidden: true },
      h("label", { class: "online-field" },
        h("span", { text: "Merged text (starts from yours)" }),
        h("textarea", { class: "textarea online-merge-text", id: "online-merge-text", spellcheck: "false" }))));
    const foot = h("div", { class: "sheet-foot" },
      h("button", { class: "btn ghost", id: "online-conflict-close", type: "button", text: "Close" }),
      h("span", { style: "flex:1" }),
      h("button", { class: "btn", id: "online-keep-theirs", type: "button", text: "Keep theirs" }),
      h("button", { class: "btn", id: "online-keep-mine", type: "button", text: "Keep mine" }),
      h("button", { class: "btn primary", id: "online-edit-merged", type: "button", text: "Edit merged" }),
      h("button", { class: "btn primary", id: "online-save-merged", type: "button", text: "Save merged", hidden: true }));
    d.append(body, foot);
    $("#online-conflict-close", d).addEventListener("click", () => d.close());
    return d;
  }

  async function openConflict(id, preloaded) {
    const d = buildConflictDialog();
    const cmp = $("#online-compare", d);
    const pathEl = $("#online-conflict-path", d);
    cmp.innerHTML = "";
    cmp.appendChild(h("p", { class: "online-muted", text: "Loading…" }));
    openDialog(d);
    let c = preloaded || null;
    if (!c) {
      try {
        c = await GET(`/api/online/conflicts/${encodeURIComponent(id)}`);
      } catch (err) {
        cmp.innerHTML = "";
        cmp.appendChild(h("p", { class: "online-warn", text: String(err.message || err) }));
        return;
      }
    }
    if (pathEl) pathEl.textContent = c.path || "";
    cmp.innerHTML = "";
    cmp.append(
      h("div", { class: "online-pane" },
        h("h5", { text: "Mine" }),
        h("pre", { class: "online-pre", text: c.mine_text || "(empty)" })),
      h("div", { class: "online-pane" },
        h("h5", { text: "Theirs" }),
        h("pre", { class: "online-pre", text: c.theirs_text || "(empty)" })),
    );
    const merged = $("#online-merged", d);
    const area = $("#online-merge-text", d);
    const mine = $("#online-keep-mine", d);
    const theirs = $("#online-keep-theirs", d);
    const editBtn = $("#online-edit-merged", d);
    const saveBtn = $("#online-save-merged", d);

    const resolve = async (resolution, text) => {
      [mine, theirs, editBtn, saveBtn].forEach((b) => { if (b) b.disabled = true; });
      if (c.__preview) {
        d.close();
        toast("Conflict resolved", "ok", "preview only — nothing was sent");
        return;
      }
      try {
        await POST(`/api/online/conflicts/${encodeURIComponent(id)}/resolve`,
          { resolution, merged_text: text });
        await POST("/api/online/sync", {}).catch(() => {});
        await refreshStatus();
        d.close();
        toast("Conflict resolved", "ok", resolution === "merged" ? "your merged version was kept" : `kept ${resolution === "mine" ? "yours" : "theirs"}`);
        await loadPanelData();
      } catch (err) {
        toast("Could not resolve the conflict", "err", String(err.message || err));
        [mine, theirs, editBtn, saveBtn].forEach((b) => { if (b) b.disabled = false; });
      }
    };
    mine.onclick = () => resolve("mine", c.mine_text || "");
    theirs.onclick = () => resolve("theirs", null);
    editBtn.onclick = () => {
      merged.hidden = false;
      area.value = c.mine_text || "";
      editBtn.hidden = true;
      saveBtn.hidden = false;
      setTimeout(() => area.focus(), 30);
    };
    saveBtn.onclick = () => resolve("merged", area.value);
  }

  /* ---------------------------------------------------------------- offline errors */
  function offlineNotice(err) {
    if (!S.active || !err) return false;
    const offline = err.status === 503 || err.code === "offline"
      || /offline/i.test(String(err.code || ""));
    if (!offline) return false;
    toast("Not available offline — pin it next time", "err",
      "This paper is not in the cache yet. Pin it (online badge → Take papers offline) and it opens without the server.");
    return true;
  }

  /* ---------------------------------------------------------------- boot */
  /* The admin panel is a module of its own (admin_online.js/.css) that mounts
     into #online-admin-root on cs:online-admin-open.  It is pulled in here, and
     only when the page does not carry its tags already, so the two sessions
     cannot include it twice. */
  function ensureAdminAssets() {
    if (!document.getElementById("online-admin-root")) return;
    if (!document.querySelector('script[src="/static/admin_online.js"]')) {
      const s = document.createElement("script");
      s.src = "/static/admin_online.js";
      s.defer = true;
      document.body.appendChild(s);
    }
    if (!document.querySelector('link[href="/static/admin_online.css"]')) {
      const l = document.createElement("link");
      l.rel = "stylesheet";
      l.href = "/static/admin_online.css";
      document.head.appendChild(l);
    }
  }

  function boot() {
    if (S.booted) return;
    S.booted = true;

    ensureAdminAssets();
    const badge = $("#online-badge");
    if (badge) badge.addEventListener("click", () => openSyncPanel());
    const adminBtn = $("#online-admin-btn");
    if (adminBtn) {
      adminBtn.addEventListener("click", () => {
        window.dispatchEvent(new CustomEvent("cs:online-admin-open"));
      });
    }
    const projectOnline = $("#project-online");
    if (projectOnline) {
      projectOnline.addEventListener("click", () => {
        const pd = $("#project-dialog");
        if (pd && pd.open) pd.close();
        openConnectDialog();
      });
    }

    // the heartbeat follows the open paper and the active tab
    document.addEventListener("cs:view", () => { S.lastPresence = ""; sendPresence(); });
    window.addEventListener("cs:paper", () => { S.lastPresence = ""; sendPresence(); });
    window.addEventListener("beforeunload", () => { releaseAll(); });

    /* Design review without a studio server: ?online=preview paints the badge
       and the sync panel from a synthetic status and starts no polling.  It is
       the only way the online chrome can be looked at on a local folder. */
    try {
      const q = new URLSearchParams(location.search);
      if (q.get("online") === "preview") {
        window.CSOnline._preview({
          active: true,
          mode: q.get("mode") || "online",
          pending: Number(q.get("pending") || 2),
          conflicts: Number(q.get("conflicts") || 1),
          last_sync: new Date(Date.now() - 4 * 60 * 1000).toISOString(),
          server: "https://studio.example.org",
          user: "anna",
          project_name: "Misinformation interventions (preview)",
          seq: 1421,
          me: { role: q.get("role") || "coder", tags: ["AN", "AN2"], tasks: ["coding", "settle", "harmonize"] },
          settings: { blinding: { coding: "blinded" }, lock_ttl_h: 24, max_offline_lease_h: 72 },
        });
        if (q.get("conflict") === "1") {
          openConflict("preview", {
            __preview: true,
            id: "preview",
            path: "Extraction/Abascal (2023).json",
            mine_text: '{\n  "Study_ID": "ABASCAL2023",\n  "N_Analyzed": 412,\n  "Setting": "online",\n  "Intervention_Type": ["accuracy prompt"],\n  "DV_Metrics": [\n    { "dv_name": "sharing_intention_false", "n1i": 205, "m1i": 2.31 }\n  ]\n}',
            theirs_text: '{\n  "Study_ID": "ABASCAL2023",\n  "N_Analyzed": 402,\n  "Setting": "online",\n  "Intervention_Type": ["accuracy prompt", "warning label"],\n  "DV_Metrics": [\n    { "dv_name": "sharing_intention_false", "n1i": 205, "m1i": 2.28 }\n  ]\n}',
            base_text: '{\n  "Study_ID": "ABASCAL2023",\n  "N_Analyzed": 402,\n  "Setting": "online",\n  "Intervention_Type": ["accuracy prompt"],\n  "DV_Metrics": [\n    { "dv_name": "sharing_intention_false", "n1i": 205, "m1i": 2.28 }\n  ]\n}',
          });
        }
        return;
      }
    } catch { /* ignore */ }

    startStatus();
  }

  /* ---------------------------------------------------------------- public API */
  window.CSOnline = {
    active,
    status,
    mode: () => S.mode,
    me,
    settings,
    locks,
    offlineNotice,
    openSyncPanel,
    openConnectDialog,
    conflicts: () => S.conflicts.slice(),
    presence: () => S.others.slice(),
    /* design/QA hook: paint the badge from a synthetic status object without a
       server (window.CSOnline._preview({...})) */
    _preview: (st) => applyStatus(st || { active: true, mode: "online", project_name: "Preview", server: "https://preview", user: "anna", me: { role: "coder", tags: ["AN"], tasks: ["coding", "settle"] }, settings: { lock_ttl_h: 24, max_offline_lease_h: 72 } }),
    _state: S,
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
