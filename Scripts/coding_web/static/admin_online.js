/* Project admin panel for an online (server-backed) project.
 *
 * Opened by the header menu through  window.dispatchEvent(new CustomEvent("cs:online-admin-open")).
 * Owners only: the server answers 403 to everyone else and the panel says so.
 * Plain DOM; every server string is written with textContent, never innerHTML.
 *
 * Tabs: Members (+ server users) · Settings · Assignments · Locks · Audit · Publish.
 * Routes: /api/online/admin/... (Results/INTERFACES_online_projects.md §4), plus
 * GET /api/online/locks for the lock list and GET /api/papers for the paper keys.
 */
(function () {
  "use strict";

  const ROOT_ID = "online-admin-root";
  const API = "api/online/admin";
  const STAGES = ["abstract", "fulltext", "coding"];
  const TASKS = ["abstract", "fulltext", "coding", "settle", "harmonize", "reproduce"];
  const ROLES = ["owner", "coder", "viewer"];
  const TABS = [
    ["members", "Members"],
    ["settings", "Settings"],
    ["assignments", "Assignments"],
    ["locks", "Locks"],
    ["audit", "Audit"],
    ["publish", "Publish"],
  ];
  const POLL_MS = 2000;
  // Publish states that mean "finished". Any other state counts as running,
  // unless the server sends an explicit boolean `running`.
  const DONE_STATES = ["idle", "done", "error", "failed", "complete", "completed", "cancelled", "none"];
  const enc = encodeURIComponent;
  const VIEWS = {};

  let state = null; // non-null while the panel is open

  class AdminError extends Error {
    constructor(code, message, status, data) {
      super(message);
      this.code = code;
      this.status = status || 0;
      this.data = data || null;
    }
  }

  /* ------------------------------------------------------------ DOM helpers */

  // el("tag", {attr: value, text, onclick, ...}, child, [children], ...)
  // false/null/undefined props are skipped. checked/disabled/value are set as
  // properties; other true values become empty attributes (multiple, hidden).
  function el(tag, props, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") n.className = String(v);
      else if (k === "text") n.textContent = String(v);
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (k === "checked" || k === "disabled" || k === "value") n[k] = v;
      else n.setAttribute(k, v === true ? "" : String(v));
    }
    for (const c of kids.flat()) {
      if (c === null || c === undefined || c === false) continue;
      n.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return n;
  }

  const td = (v, cls) => el("td", { class: cls || null, text: v ?? "" });
  const section = (title, ...kids) => el("section", { class: "oa-section" }, el("h3", { text: title }), ...kids);
  const badge = (text, tone) => el("span", { class: "oa-badge", "data-tone": tone || null, text });

  // A control wrapped in its caption. Inputs and selects get a real <label>;
  // composite controls (chip list, checkbox group) get a <div> with a caption.
  function field(label, control) {
    const isCtl = control.matches && control.matches("input, select, textarea");
    const wrap = el(isCtl ? "label" : "div", { class: "oa-field" });
    wrap.append(el("span", { class: "oa-label", text: label }), control);
    return wrap;
  }

  function table(headers, rows, empty) {
    if (!rows.length) return el("p", { class: "oa-muted", text: empty || "Nothing here yet." });
    return el("div", { class: "oa-table" },
      el("table", null,
        el("thead", null, el("tr", null, headers.map((h) => el("th", { scope: "col", text: h })))),
        el("tbody", null, rows)));
  }

  function focusables(root) {
    return [...root.querySelectorAll("button, input, select, textarea, [tabindex='0']")]
      .filter((n) => !n.disabled && n.getClientRects().length > 0);
  }

  /* ------------------------------------------------------------ data helpers */

  // Admin routes answer with an object or a bare list; accept both.
  function listOf(d, key) {
    if (Array.isArray(d)) return d;
    if (d && Array.isArray(d[key])) return d[key];
    return [];
  }
  const uidOf = (m) => m.user_id ?? m.id;
  const nameOf = (m) => m.display_name || m.username || String(uidOf(m));

  function fmtBytes(n) {
    if (!n) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let v = Number(n);
    let i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return `${v.toFixed(i ? 1 : 0)} ${units[i]}`;
  }

  function fmtTime(v) {
    if (v === null || v === undefined || v === "") return "";
    // numbers below 1e12 are epoch seconds
    const d = new Date(typeof v === "number" && v < 1e12 ? v * 1000 : v);
    return Number.isNaN(d.getTime()) ? String(v) : d.toLocaleString();
  }

  // Token list: Enter or comma adds, Backspace on an empty input removes the last.
  function chipInput(initial, label) {
    const items = [...new Set((initial || []).map(String))];
    const input = el("input", { class: "oa-input oa-chip-input", type: "text", autocomplete: "off",
      "aria-label": label, placeholder: "add…" });
    const wrap = el("div", { class: "oa-chips" });

    const draw = () => {
      const chips = items.map((t, i) => el("span", { class: "oa-chip" },
        el("span", { text: t }),
        el("button", { class: "oa-chip-x", type: "button", "aria-label": `Remove ${t}`, text: "×",
          onclick: () => { items.splice(i, 1); draw(); input.focus(); } })));
      wrap.replaceChildren(...chips, input);
    };
    const add = (raw, keepFocus) => {
      for (const t of String(raw).split(",").map((s) => s.trim()).filter(Boolean)) {
        if (!items.includes(t)) items.push(t);
      }
      input.value = "";
      draw();
      if (keepFocus) input.focus();
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === ",") {
        e.preventDefault();
        if (input.value.trim()) add(input.value, true);
      } else if (e.key === "Backspace" && !input.value && items.length) {
        items.pop();
        draw();
        input.focus();
      }
    });
    input.addEventListener("blur", () => { if (input.value.trim()) add(input.value, false); });
    draw();
    return { node: wrap, get: () => items.slice() };
  }

  // Task checkboxes: the six task names.
  function taskBoxes(initial) {
    const have = new Set(initial || []);
    const boxes = TASKS.map((t) => {
      const cb = el("input", { type: "checkbox", checked: have.has(t) });
      return { cb, node: el("label", { class: "oa-check" }, cb, t) };
    });
    return {
      node: el("div", { class: "oa-checks" }, boxes.map((b) => b.node)),
      get: () => TASKS.filter((_, i) => boxes[i].cb.checked),
    };
  }

  function explain(e) {
    if (!e) return "Unknown error.";
    if (e.status === 403) return "Owner access is required for this action.";
    if (e.status === 409 && e.data && e.data.holder) {
      const h = e.data.holder;
      return `Locked by ${h.display_name || h.user || "another user"}.`;
    }
    if (e.code === "network") return "Cannot reach the server.";
    if (e.status === 503) return "The server is unavailable (offline).";
    return e.message || String(e);
  }

  /* ------------------------------------------------------------ API + feedback */

  async function api(path, opts) {
    const o = opts || {};
    const init = { method: o.method || "GET", headers: { Accept: "application/json" }, credentials: "same-origin" };
    if (o.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(o.body);
    }
    let r;
    try {
      r = await fetch(path, init);
    } catch (e) {
      throw new AdminError("network", `Network error: ${e.message || e}`, 0, null);
    }
    const text = await r.text();
    let data = null;
    if (text) {
      try { data = JSON.parse(text); } catch (_) { data = { message: text }; }
    }
    if (!r.ok) {
      const code = (data && (data.error || data.detail)) || `http_${r.status}`;
      const msg = (data && data.message) || `${init.method} ${path} → HTTP ${r.status}`;
      throw new AdminError(String(code), String(msg), r.status, data);
    }
    return data;
  }

  // Inline status line, used when the app's toast is not available.
  function setStatus(msg, kind) {
    if (!state) return;
    state.status.className = `oa-status ${kind || "ok"}`;
    state.status.textContent = msg;
    state.status.hidden = !msg;
  }

  // The app exposes its toast as window.CS.toast(msg, kind, sub).
  function notify(msg, kind) {
    const k = kind === "err" ? "err" : "ok";
    const toast = (window.CS && window.CS.toast) || window.toast;
    if (typeof toast === "function") toast(msg, k);
    else setStatus(msg, k);
  }

  // Wraps a button's action: disables the button while it runs, reports the
  // outcome, and resolves to the result (undefined on failure).
  function action(btn, fn, okMsg) {
    return async () => {
      if (btn) btn.disabled = true;
      try {
        const r = await fn();
        if (okMsg) notify(typeof okMsg === "function" ? okMsg(r) : okMsg, "ok");
        return r;
      } catch (e) {
        notify(explain(e), "err");
        return undefined;
      } finally {
        if (btn) btn.disabled = false;
      }
    };
  }

  /* ------------------------------------------------------------ panel shell */

  function open() {
    if (state) {
      const cur = state.tabs.querySelector('[aria-selected="true"]');
      if (cur) cur.focus();
      return;
    }
    let root = document.getElementById(ROOT_ID);
    if (!root) {
      root = el("div", { id: ROOT_ID });
      document.body.appendChild(root);
    }

    const titleId = "oa-title";
    const tabs = el("div", { class: "oa-tabs", role: "tablist", "aria-label": "Project admin sections" },
      TABS.map(([id, label]) => el("button", {
        class: "oa-tab", type: "button", role: "tab", id: `oa-tab-${id}`,
        "aria-controls": "oa-body", "aria-selected": "false", tabindex: "-1", "data-tab": id, text: label,
      })));
    const status = el("div", { class: "oa-status", role: "status", "aria-live": "polite", hidden: true });
    const body = el("div", { class: "oa-body", id: "oa-body", role: "tabpanel", tabindex: "0" });
    const closeBtn = el("button", { class: "btn ghost icon", type: "button", "aria-label": "Close project admin", text: "×" });
    const head = el("div", { class: "oa-head" }, el("h2", { id: titleId, text: "Project admin" }), closeBtn);
    const panel = el("div", { class: "oa-modal", role: "dialog", "aria-modal": "true", "aria-labelledby": titleId },
      head, tabs, status, body);
    const overlay = el("div", { class: "oa-overlay" }, panel);

    state = {
      root, overlay, panel, tabs, status, body,
      returnFocus: document.activeElement,
      tab: null, token: 0, pollTimer: null, onFocusIn: null,
    };

    closeBtn.addEventListener("click", close);
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) close(); });
    overlay.addEventListener("keydown", onKey);
    tabs.addEventListener("click", (e) => {
      const b = e.target.closest("[data-tab]");
      if (b) selectTab(b.dataset.tab);
    });
    tabs.addEventListener("keydown", (e) => {
      const ids = TABS.map((t) => t[0]);
      const i = ids.indexOf(state.tab);
      let j = null;
      if (e.key === "ArrowRight") j = (i + 1) % ids.length;
      else if (e.key === "ArrowLeft") j = (i - 1 + ids.length) % ids.length;
      else if (e.key === "Home") j = 0;
      else if (e.key === "End") j = ids.length - 1;
      if (j === null) return;
      e.preventDefault();
      selectTab(ids[j]);
      const next = tabs.querySelector(`#oa-tab-${ids[j]}`);
      if (next) next.focus();
    });

    // Focus trap: anything that takes focus outside the dialog is pulled back in.
    state.onFocusIn = (e) => {
      if (!state || panel.contains(e.target)) return;
      const f = focusables(panel);
      if (f.length) f[0].focus();
    };
    document.addEventListener("focusin", state.onFocusIn, true);

    root.replaceChildren(overlay);
    selectTab("members");
    const first = tabs.querySelector('[aria-selected="true"]');
    if (first) first.focus();
  }

  function close() {
    if (!state) return;
    const s = state;
    state = null;
    clearTimeout(s.pollTimer);
    document.removeEventListener("focusin", s.onFocusIn, true);
    s.root.replaceChildren();
    const rf = s.returnFocus;
    if (rf && typeof rf.focus === "function" && document.contains(rf)) rf.focus();
  }

  function onKey(e) {
    if (!state) return;
    if (e.key === "Escape") {
      e.preventDefault();
      close();
      return;
    }
    if (e.key !== "Tab") return;
    const f = focusables(state.panel);
    if (!f.length) return;
    const first = f[0];
    const last = f[f.length - 1];
    const cur = document.activeElement;
    const inside = state.panel.contains(cur);
    if (e.shiftKey && (cur === first || !inside)) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && (cur === last || !inside)) {
      e.preventDefault();
      first.focus();
    }
  }

  function selectTab(id) {
    if (!state) return;
    state.tab = id;
    for (const b of state.tabs.querySelectorAll('[role="tab"]')) {
      const on = b.dataset.tab === id;
      b.setAttribute("aria-selected", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;
      if (on) state.body.setAttribute("aria-labelledby", b.id);
    }
    state.status.hidden = true;
    state.status.textContent = "";
    renderTab();
  }

  // Each render takes a token; a late response from an earlier tab is dropped.
  async function renderTab() {
    if (!state) return;
    const s = state;
    s.token += 1;
    const token = s.token;
    clearTimeout(s.pollTimer);
    s.pollTimer = null;
    s.body.replaceChildren(el("p", { class: "oa-muted", text: "Loading…" }));
    try {
      const view = await VIEWS[s.tab]();
      if (!state || state.token !== token) return;
      state.body.replaceChildren(view);
    } catch (e) {
      if (!state || state.token !== token) return;
      state.body.replaceChildren(el("p", { class: "oa-error", role: "alert", text: explain(e) }));
    }
  }

  /* ------------------------------------------------------------ Members tab */

  VIEWS.members = async function () {
    const [mem, usr] = await Promise.all([api(`${API}/members`), api(`${API}/users`)]);
    const members = listOf(mem, "members");
    const users = listOf(usr, "users");
    const memberIds = new Set(members.map((m) => String(uidOf(m))));
    return el("div", null,
      section("Members", memberTable(members)),
      section("Add member", addMemberForm(users, memberIds)),
      section("Server users", createUserForm(), usersTable(users)));
  };

  function memberTable(members) {
    const rows = members.map((m) => {
      const who = nameOf(m);
      const uid = enc(uidOf(m));
      const role = el("select", { class: "oa-input", "aria-label": `Role of ${who}` },
        ROLES.map((r) => el("option", { value: r, text: r, selected: r === m.role })));
      const tags = chipInput(m.tags, `Tags of ${who}`);
      const tasks = taskBoxes(m.tasks);
      const save = el("button", { class: "btn primary", type: "button", text: "Save" });
      const rm = el("button", { class: "btn danger", type: "button", text: "Remove" });
      save.addEventListener("click", action(save, () => api(`${API}/members/${uid}`, {
        method: "PUT",
        body: { role: role.value, tags: tags.get(), tasks: tasks.get() },
      }), `Saved ${who}`));
      rm.addEventListener("click", () => {
        if (!window.confirm(`Remove ${who} from this project? Their existing work stays in the project.`)) return;
        action(rm, async () => {
          await api(`${API}/members/${uid}`, { method: "DELETE" });
          await renderTab();
        }, `Removed ${who}`)();
      });
      return el("tr", null,
        td(who), td(m.username || ""),
        el("td", null, role),
        el("td", null, tags.node),
        el("td", null, tasks.node),
        el("td", { class: "oa-actions" }, save, rm));
    });
    return table(["Name", "Username", "Role", "Tags", "Tasks", ""], rows, "No members yet. Add one below.");
  }

  function addMemberForm(users, memberIds) {
    const candidates = users.filter((u) => !memberIds.has(String(u.id)) && !u.disabled);
    if (!candidates.length) {
      return el("p", { class: "oa-muted", text: "Every enabled server user is already a member. Create a user below first." });
    }
    const pick = el("select", { class: "oa-input", "aria-label": "Server user to add" },
      candidates.map((u) => el("option", { value: String(u.id), text: `${u.display_name || u.username} (${u.username})` })));
    const role = el("select", { class: "oa-input", "aria-label": "Role for new member" },
      ROLES.map((r) => el("option", { value: r, text: r, selected: r === "coder" })));
    const tags = chipInput([], "Tags for new member");
    const tasks = taskBoxes(["coding"]);
    const btn = el("button", { class: "btn primary", type: "button", text: "Add member" });
    btn.addEventListener("click", action(btn, async () => {
      await api(`${API}/members/${enc(pick.value)}`, {
        method: "PUT",
        body: { role: role.value, tags: tags.get(), tasks: tasks.get() },
      });
      await renderTab();
    }, "Member added"));
    return el("div", null,
      el("div", { class: "oa-form-row" }, field("User", pick), field("Role", role), field("Tags", tags.node)),
      el("div", { class: "oa-form-row" }, field("Tasks", tasks.node)),
      el("div", { class: "oa-actions-row" }, btn));
  }

  function createUserForm() {
    const username = el("input", { class: "oa-input", type: "text", autocomplete: "off" });
    const display = el("input", { class: "oa-input", type: "text", autocomplete: "off" });
    const email = el("input", { class: "oa-input", type: "email", autocomplete: "off" });
    const password = el("input", { class: "oa-input", type: "password", autocomplete: "new-password" });
    const admin = el("input", { type: "checkbox" });
    const submit = el("button", { class: "btn primary", type: "submit", text: "Create user" });
    const form = el("form", { novalidate: true },
      el("h4", { text: "Create user" }),
      el("div", { class: "oa-form-row" },
        field("Username", username), field("Display name", display), field("Email", email)),
      el("div", { class: "oa-form-row" },
        field("Password", password),
        el("label", { class: "oa-check" }, admin, "Server admin")),
      el("div", { class: "oa-actions-row" }, submit));
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      if (!username.value.trim() || !password.value) {
        notify("Username and password are required.", "err");
        return;
      }
      action(submit, async () => {
        await api(`${API}/users`, {
          method: "POST",
          body: {
            username: username.value.trim(),
            display_name: display.value.trim(),
            email: email.value.trim(),
            password: password.value,
            is_admin: admin.checked,
          },
        });
        await renderTab();
      }, "User created")();
    });
    return form;
  }

  function usersTable(users) {
    const rows = users.map((u) => {
      const toggle = el("button", { class: "btn", type: "button", text: u.disabled ? "Enable" : "Disable" });
      toggle.addEventListener("click", action(toggle, async () => {
        await api(`${API}/users/${enc(u.id)}`, { method: "PATCH", body: { disabled: !u.disabled } });
        await renderTab();
      }, u.disabled ? `Enabled ${u.username}` : `Disabled ${u.username}`));
      return el("tr", null,
        td(u.username || ""), td(u.display_name || ""), td(u.email || ""),
        td(u.is_admin ? "yes" : "no"),
        el("td", null, u.disabled ? badge("disabled", "warn") : badge("active", "ok")),
        el("td", { class: "oa-actions" }, toggle));
    });
    return el("div", null, el("h4", { text: "Users" }),
      table(["Username", "Name", "Email", "Admin", "Status", ""], rows, "No server users."));
  }

  /* ------------------------------------------------------------ Settings tab */

  VIEWS.settings = async function () {
    const raw = await api(`${API}/settings`);
    const s = (raw && raw.settings) || raw || {};
    const bl = s.blinding || {};
    const blindSel = {};
    const blindRows = STAGES.map((st) => {
      const sel = el("select", { class: "oa-input", "aria-label": `Blinding for ${st}` },
        [["blinded", "Blinded"], ["visible", "Visible"]].map(([v, t]) =>
          el("option", { value: v, text: t, selected: (bl[st] || "blinded") === v })));
      blindSel[st] = sel;
      return field(`Blinding: ${st}`, sel);
    });
    const lock = el("input", { class: "oa-input", type: "number", min: "0.1", step: "any",
      value: String(s.lock_ttl_h ?? 24) });
    const lease = el("input", { class: "oa-input", type: "number", min: "0.1", step: "any",
      value: String(s.max_offline_lease_h ?? 72) });
    const save = el("button", { class: "btn primary", type: "button", text: "Save settings" });
    save.addEventListener("click", action(save, async () => {
      const lockH = Number(lock.value);
      const leaseH = Number(lease.value);
      if (!(Number.isFinite(lockH) && lockH > 0 && Number.isFinite(leaseH) && leaseH > 0)) {
        throw new AdminError("bad_request", "Lock TTL and offline lease must be positive numbers of hours.");
      }
      const blinding = {};
      for (const st of STAGES) blinding[st] = blindSel[st].value;
      await api(`${API}/settings`, {
        method: "PATCH",
        body: { blinding, lock_ttl_h: lockH, max_offline_lease_h: leaseH },
      });
    }, "Settings saved"));
    return el("div", null,
      section("Blinding",
        el("div", { class: "oa-form-row" }, blindRows),
        el("p", { class: "oa-muted", text: "Blinded: a coder cannot see another tag's work on a paper until they have finished that paper (coding stage). Abstract and fulltext files of other tags stay hidden. Visible: no restriction." })),
      section("Locks and offline leases",
        el("div", { class: "oa-form-row" }, field("Lock TTL (hours)", lock), field("Max offline lease (hours)", lease))),
      el("div", { class: "oa-actions-row" }, save));
  };

  /* ------------------------------------------------------------ Assignments tab */

  VIEWS.assignments = async function () {
    const mem = await api(`${API}/members`);
    const members = listOf(mem, "members");
    const tags = [...new Set(members.flatMap((m) => m.tags || []))].sort();

    // ---- papers
    const stageSel = el("select", { class: "oa-input", "aria-label": "Stage" },
      STAGES.map((s) => el("option", { value: s, text: s })));
    const keysTA = el("textarea", { class: "oa-input oa-keys", rows: "6", "aria-label": "Paper keys, one per line" });
    const keysNote = el("span", { class: "oa-muted" });
    const readKeys = () => [...new Set(keysTA.value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean))];
    const updateKeysNote = () => { keysNote.textContent = `${readKeys().length} keys`; };
    keysTA.addEventListener("input", updateKeysNote);
    const useAll = el("button", { class: "btn", type: "button", text: "Use all papers" });
    useAll.addEventListener("click", action(useAll, async () => {
      const d = await api("api/papers");
      const keys = listOf(d, "papers").map((p) => p.key).filter(Boolean);
      keysTA.value = keys.join("\n");
      updateKeysNote();
      return keys.length;
    }, (n) => `Loaded ${n} paper keys`));

    // ---- mode
    const modeName = "oa-asg-mode";
    const radioAuto = el("input", { type: "radio", name: modeName, value: "auto", checked: true });
    const radioManual = el("input", { type: "radio", name: modeName, value: "manual" });
    const modeGroup = el("div", { class: "oa-radio-group", role: "radiogroup", "aria-label": "Assignment mode" },
      el("label", { class: "oa-check" }, radioAuto, "Automatic"),
      el("label", { class: "oa-check" }, radioManual, "Manual"));

    // ---- automatic: coders are the distinct member tags, each with a share
    const coderRows = tags.map((tag) => ({
      tag,
      share: el("input", { class: "oa-input", type: "number", min: "0", step: "any", value: "1", "aria-label": `Share for ${tag}` }),
    }));
    const coderTable = coderRows.length
      ? table(["Coder (tag)", "Share"], coderRows.map((c) => el("tr", null, td(c.tag), el("td", { class: "oa-num" }, c.share))))
      : el("p", { class: "oa-muted", text: "No member has a tag yet. Give members tags in the Members tab." });
    const overlapPct = el("input", { class: "oa-input", type: "number", min: "0", max: "100", step: "any", value: "0" });
    const overlapMode = el("select", { class: "oa-input", "aria-label": "Overlap mode" },
      [["pairs", "pairs"], ["all", "all"]].map(([v, t]) => el("option", { value: v, text: t })));
    const seedInput = el("input", { class: "oa-input", type: "number", step: "1", placeholder: "random", "aria-label": "Seed (blank = random)" });
    const previewBtn = el("button", { class: "btn", type: "button", text: "Preview" });
    const commitBtn = el("button", { class: "btn primary", type: "button", text: "Commit", disabled: true });
    const resultBox = el("div", { "aria-live": "polite" });
    const autoPanel = el("div", null,
      section("Coders", coderTable),
      el("div", { class: "oa-form-row" },
        field("Overlap %", overlapPct), field("Overlap mode", overlapMode), field("Seed", seedInput)),
      el("div", { class: "oa-actions-row" }, previewBtn, commitBtn),
      resultBox);

    function autoBody() {
      return {
        stage: stageSel.value,
        keys: readKeys(),
        coders: coderRows
          .map((c) => ({ tag: c.tag, share: Number(c.share.value) }))
          .filter((c) => Number.isFinite(c.share) && c.share > 0),
        overlap_pct: Number(overlapPct.value),
        overlap_mode: overlapMode.value,
      };
    }
    function validateAuto(b, seedRaw) {
      if (!b.keys.length) throw new AdminError("bad_request", "Add at least one paper key.");
      if (!b.coders.length) throw new AdminError("bad_request", "Give at least one coder a share above 0.");
      if (!(Number.isFinite(b.overlap_pct) && b.overlap_pct >= 0 && b.overlap_pct <= 100)) {
        throw new AdminError("bad_request", "Overlap must be between 0 and 100 %.");
      }
      if (b.overlap_mode === "pairs" && b.coders.length < 2 && b.overlap_pct > 0) {
        throw new AdminError("bad_request", "Pairs overlap needs at least two coders.");
      }
      if (seedRaw && !Number.isInteger(Number(seedRaw))) {
        throw new AdminError("bad_request", "Seed must be a whole number.");
      }
    }

    function renderPlan(plan, seed, order) {
      const per = plan.per_coder || {};
      const pm = plan.pair_matrix || {};
      const nPapers = Object.keys(plan.assignments || {}).length;
      const perTable = table(["Coder", "Papers"],
        order.map((t) => el("tr", null, td(t), el("td", { class: "oa-num", text: String(per[t] ?? 0) }))),
        "No coders.");
      let matrix;
      if (order.length > 1) {
        const head = el("tr", null, el("th", { text: "" }), order.map((t) => el("th", { scope: "col", text: t })));
        const rows = order.map((a) => el("tr", null,
          el("th", { scope: "row", text: a }),
          order.map((b) => {
            if (a === b) return el("td", { class: "oa-num oa-diag", text: "—" });
            const [x, y] = [a, b].sort();
            return el("td", { class: "oa-num", text: String(pm[`${x}|${y}`] ?? 0) });
          })));
        matrix = el("div", null, el("h4", { text: "Pair matrix (papers both coders get)" }),
          el("div", { class: "oa-table" }, el("table", null, el("thead", null, head), el("tbody", null, rows))));
      } else {
        matrix = el("p", { class: "oa-muted", text: "A pair matrix needs at least two coders." });
      }
      resultBox.replaceChildren(
        el("h4", { text: `Preview: ${nPapers} papers, seed ${seed}` }),
        el("div", { class: "oa-two" },
          el("div", null, el("h4", { text: "Per coder" }), perTable),
          matrix));
    }

    // Commit is allowed only for exactly the inputs the last preview used.
    let previewSig = null;
    const sigOf = (b) => JSON.stringify(b);

    previewBtn.addEventListener("click", action(previewBtn, async () => {
      const seedRaw = seedInput.value.trim();
      const body = autoBody();
      validateAuto(body, seedRaw);
      const req = seedRaw ? { ...body, seed: Number(seedRaw) } : body;
      const plan = await api(`${API}/assignments/preview`, { method: "POST", body: req });
      const seed = (plan.params && plan.params.seed) ?? plan.seed ?? req.seed;
      if (seed === undefined || seed === null) throw new AdminError("bad_request", "The server returned no seed.");
      seedInput.value = String(seed);
      previewSig = sigOf({ ...body, seed: Number(seed) });
      renderPlan(plan, seed, body.coders.map((c) => c.tag));
      commitBtn.disabled = false;
      return plan;
    }));

    commitBtn.addEventListener("click", action(commitBtn, async () => {
      const seedRaw = seedInput.value.trim();
      const body = autoBody();
      validateAuto(body, seedRaw);
      if (!seedRaw) throw new AdminError("bad_request", "Preview first; the preview fills the seed.");
      const req = { ...body, seed: Number(seedRaw) };
      if (sigOf(req) !== previewSig) {
        commitBtn.disabled = true;
        throw new AdminError("bad_request", "The inputs changed since the preview. Preview again.");
      }
      const r = await api(`${API}/assignments/commit`, { method: "POST", body: req });
      previewSig = null;
      commitBtn.disabled = true;
      await refreshRuns();
      return r;
    }, (r) => `Committed run ${(r && (r.run_id ?? r.id)) ?? ""}`.trim()));

    // Any edit after a preview invalidates it.
    const invalidate = () => { commitBtn.disabled = true; };
    autoPanel.addEventListener("input", invalidate);
    autoPanel.addEventListener("change", invalidate);
    stageSel.addEventListener("change", invalidate);
    keysTA.addEventListener("input", invalidate);

    // ---- manual: every picked paper goes to every picked tag
    const tagSel = el("select", { class: "oa-input", multiple: true,
      size: String(Math.min(6, Math.max(2, tags.length))), "aria-label": "Tags to assign" },
      tags.map((t) => el("option", { value: t, text: t })));
    const assignBtn = el("button", { class: "btn primary", type: "button", text: "Assign to tags" });
    assignBtn.addEventListener("click", action(assignBtn, async () => {
      const keys = readKeys();
      const picked = [...tagSel.selectedOptions].map((o) => o.value);
      if (!keys.length) throw new AdminError("bad_request", "Add at least one paper key.");
      if (!picked.length) throw new AdminError("bad_request", "Pick at least one tag.");
      const r = await api(`${API}/assignments/manual`, {
        method: "POST",
        body: { stage: stageSel.value, keys, tags: picked },
      });
      await refreshRuns();
      return r;
    }, (r) => (r && r.run_id ? `Manual run ${r.run_id} saved` : "Manual assignment saved")));
    const manualPanel = el("div", { hidden: true },
      field("Tags", tagSel),
      el("div", { class: "oa-actions-row" }, assignBtn),
      el("p", { class: "oa-muted", text: tags.length ? "Each paper in the list goes to every picked tag." : "No member has a tag yet." }));

    const setMode = () => {
      autoPanel.hidden = !radioAuto.checked;
      manualPanel.hidden = radioAuto.checked;
    };
    radioAuto.addEventListener("change", setMode);
    radioManual.addEventListener("change", setMode);

    // ---- runs
    const runsBox = el("div", { "aria-live": "polite" });
    async function refreshRuns() {
      try {
        const d = await api(`${API}/assignments/runs`);
        const rows = listOf(d, "runs").map((r) => {
          const id = r.run_id ?? r.id;
          const params = (r && r.params) || {};
          const undone = !!(r.undone || r.status === "undone");
          // /api/online/admin/assignments/runs puts mode and n_keys at the top
          // level; the server's own shape keeps them in `params`.
          const mode = r.mode || params.mode || "";
          const nKeys = r.n_keys ?? params.n_keys ?? params.keys ?? r.papers ?? r.count ?? "";
          const undo = el("button", { class: "btn danger", type: "button", text: "Undo" });
          undo.addEventListener("click", () => {
            if (!window.confirm(`Undo run ${id}? Its assignments are removed from the project.`)) return;
            action(undo, async () => {
              await api(`${API}/assignments/runs/${enc(id)}`, { method: "DELETE" });
              await refreshRuns();
            }, `Run ${id} undone`)();
          });
          return el("tr", null,
            td(id), td(r.stage || ""), td(mode),
            td(nKeys, "oa-num"),
            td(fmtTime(r.ts ?? r.created ?? r.created_at)),
            el("td", null, undone ? badge("undone") : badge("active", "ok")),
            el("td", { class: "oa-actions" }, undone ? null : undo));
        });
        runsBox.replaceChildren(table(["Run", "Stage", "Mode", "Papers", "Created", "Status", ""], rows, "No assignment runs yet."));
      } catch (e) {
        runsBox.replaceChildren(el("p", { class: "oa-error", role: "alert", text: explain(e) }));
      }
    }
    const refreshBtn = el("button", { class: "btn ghost", type: "button", text: "Refresh runs" });
    refreshBtn.addEventListener("click", () => refreshRuns());
    // The runs list loads once this view is in the DOM.
    setTimeout(refreshRuns, 0);

    return el("div", null,
      section("Papers",
        el("div", { class: "oa-form-row" }, field("Stage", stageSel)),
        el("div", { class: "oa-form-row" }, field("Paper keys (one per line)", keysTA)),
        el("div", { class: "oa-actions-row" }, useAll, keysNote)),
      section("Mode", modeGroup, autoPanel, manualPanel),
      section("Runs", el("div", { class: "oa-actions-row" }, refreshBtn), runsBox));
  };

  /* ------------------------------------------------------------ Locks tab */

  VIEWS.locks = async function () {
    const d = await api("api/online/locks");
    const rows = listOf(d, "locks").map((l) => {
      const key = l.key;
      const force = el("button", { class: "btn danger", type: "button", text: "Force release" });
      force.addEventListener("click", () => {
        if (!window.confirm(`Force-release the lock on "${key}"? The holder loses it without saving.`)) return;
        action(force, async () => {
          await api(`${API}/locks/${enc(key)}`, { method: "DELETE" });
          await renderTab();
        }, `Released ${key}`)();
      });
      return el("tr", null,
        td(key),
        td(`${l.display_name || l.user || ""}${l.mine === true ? " (you)" : ""}`),
        td(l.client_id ? String(l.client_id).slice(0, 12) : ""),
        td(fmtTime(l.acquired)),
        td(fmtTime(l.expires)),
        el("td", null, l.offline ? badge("offline", "warn") : badge("online", "ok")),
        el("td", null, l.expired ? badge("expired", "warn") : badge("held", "ok")),
        el("td", { class: "oa-actions" }, force));
    });
    const refresh = el("button", { class: "btn ghost", type: "button", text: "Refresh" });
    refresh.addEventListener("click", () => renderTab());
    return el("div", null,
      el("div", { class: "oa-actions-row" }, refresh),
      section("Locks", table(["Key", "Holder", "Client", "Acquired", "Expires", "Mode", "State", ""], rows, "No locks are held.")));
  };

  /* ------------------------------------------------------------ Audit tab */

  VIEWS.audit = async function () {
    const d = await api(`${API}/audit?limit=200`);
    // The server answers {audit: [{id, ts, user_id, username, action, detail}]}
    // (detail is the JSON object the action wrote); older shapes may carry
    // `entries`, `user`/`actor`, `details`.
    const entries = listOf(d, "entries").length ? listOf(d, "entries") : listOf(d, "audit");
    const KNOWN = new Set(["id", "ts", "time", "created_at", "user", "user_id", "username",
      "display_name", "actor", "action", "target", "key", "path", "detail", "details"]);
    const rows = entries.slice(0, 200).map((e) => {
      const rest = {};
      for (const [k, v] of Object.entries(e)) if (!KNOWN.has(k)) rest[k] = v;
      const detail = e.detail !== undefined ? e.detail
        : (e.details !== undefined ? e.details : rest);      let text = "";
      if (detail !== undefined && detail !== null) {
        text = typeof detail === "string" ? detail : JSON.stringify(detail);
        if (text === "{}" || text === "null") text = "";
      }
      return el("tr", null,
        td(fmtTime(e.ts ?? e.time ?? e.created_at)),
        td(e.username ?? e.user ?? e.display_name ?? e.actor ?? ""),
        td(e.action ?? ""),
        td(e.target ?? e.key ?? e.path ?? ""),
        el("td", { class: "oa-clip", title: text, text }));
    });
    const refresh = el("button", { class: "btn ghost", type: "button", text: "Refresh" });
    refresh.addEventListener("click", () => renderTab());
    return el("div", null,
      el("div", { class: "oa-actions-row" }, refresh, el("span", { class: "oa-muted", text: `${rows.length} entries` })),
      section("Audit log", table(["Time", "By", "Action", "Target", "Details"], rows, "No audit entries.")));
  };

  /* ------------------------------------------------------------ Publish tab */

  VIEWS.publish = async function () {
    const token = state.token;
    let cur = null;
    try {
      cur = await api(`${API}/publish`);
    } catch (e) {
      if (e.status !== 404) throw e;
    }

    const srcInput = el("input", { class: "oa-input", type: "text", autocomplete: "off",
      placeholder: "Folder on this computer", "aria-label": "Source folder path" });
    const go = el("button", { class: "btn primary", type: "button", text: "Publish folder" });
    const stateLine = el("p", { class: "oa-muted" });
    const countLine = el("p", { class: "oa-muted" });
    const bar = el("progress", { max: "1", value: "0", "aria-label": "Publish progress" });
    const errBox = el("div", null);

    let wasRunning = false;

    // Shape of GET /api/online/admin/publish: {state, running, done, total,
    // bytes, errors: [..], pushed, skipped, ...}.  `state` is derived by
    // routes_online; the counters are the fallback for an older route.
    function renderProgress(p) {
      const st = String((p && (p.state ?? p.status)) || "idle");
      const running = p && typeof p.running === "boolean" ? p.running : !DONE_STATES.includes(st);
      const done = Number(p && (p.done ?? p.hashed)) || 0;
      const total = Number(p && p.total) || 0;
      const bytes = Number(p && p.bytes) || 0;
      const errList = p && Array.isArray(p.errors) ? p.errors : [];
      const errCount = p && Array.isArray(p.errors) ? p.errors.length : (Number(p && p.errors) || 0);
      bar.max = total || 1;
      bar.value = total ? Math.min(done, total) : 0;
      stateLine.textContent = st === "idle" && !running
        ? "No publish has run yet." : `Status: ${st}${running ? " (running)" : ""}`;
      countLine.textContent = `${done}${total ? ` of ${total}` : ""} files · ${fmtBytes(bytes)}`;
      errBox.replaceChildren(...(errCount ? [
        el("h4", { text: `Errors (${errCount})` }),
        el("ul", { class: "oa-errlist" }, errList.slice(0, 200).map((x) =>
          el("li", { text: typeof x === "string" ? x : JSON.stringify(x) }))),
      ] : []));
      go.disabled = running;
      return { running, errCount };
    }

    async function pollOnce() {
      if (!state || state.token !== token) return;
      let p;
      try {
        p = await api(`${API}/publish`);
      } catch (e) {
        if (state && state.token === token) {
          errBox.replaceChildren(el("p", { class: "oa-error", role: "alert", text: explain(e) }));
          go.disabled = false;
        }
        return;
      }
      if (!state || state.token !== token) return;
      const { running, errCount } = renderProgress(p);
      if (running) {
        wasRunning = true;
        state.pollTimer = setTimeout(pollOnce, POLL_MS);
      } else {
        state.pollTimer = null;
        if (wasRunning) {
          wasRunning = false;
          notify(errCount ? `Publish finished with ${errCount} error(s)` : "Publish finished",
            errCount ? "err" : "ok");
        }
      }
    }

    go.addEventListener("click", async () => {
      const src = srcInput.value.trim();
      if (!src) {
        notify("Enter the source folder path.", "err");
        return;
      }
      go.disabled = true;
      try {
        await api(`${API}/publish`, { method: "POST", body: { source_root: src } });
        notify("Publish started");
        wasRunning = true;
        await pollOnce();
      } catch (e) {
        notify(explain(e), "err");
        go.disabled = false;
      }
    });

    if (cur) {
      const r = renderProgress(cur);
      if (r.running) {
        wasRunning = true;
        state.pollTimer = setTimeout(pollOnce, POLL_MS);
      }
    } else {
      renderProgress(null);
    }

    return el("div", null,
      section("Publish a local folder",
        el("p", { class: "oa-muted", text: "Pushes the folder's files into this server project through the sync protocol. The server rejects files you may not write." }),
        el("div", { class: "oa-form-row" }, field("Source folder path", srcInput)),
        el("div", { class: "oa-actions-row" }, go),
        el("div", { class: "oa-progress", "aria-live": "polite" }, stateLine, bar, countLine, errBox)));
  };

  /* ------------------------------------------------------------ wiring */

  window.addEventListener("cs:online-admin-open", () => open());
  window.csOnlineAdmin = { open, close };
})();
