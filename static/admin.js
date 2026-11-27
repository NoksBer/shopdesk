"use strict";
// Server administration page (/admin.html): platform administrator sign-in and Software Updates.
// Separate from the shop app on purpose: updating restarts the whole server for every shop, so shop owners never see it.

const $app = document.getElementById("app");
let CSRF = "";

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v); else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) {
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

async function api(method, path, body) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && CSRF) headers["X-CSRF-Token"] = CSRF;
  const res = await fetch(path, { method, headers, credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body) });
  let data = null;
  try { data = await res.json(); } catch (e) { /* not JSON */ }
  if (!res.ok) { const err = new Error((data && data.error) || "Request failed (" + res.status + ")"); err.status = res.status; throw err; }
  return data;
}

const render = (...nodes) => $app.replaceChildren(h("main", null, ...nodes));
const when = (iso) => { const d = iso ? new Date(iso) : null; return d && !isNaN(d) ? d.toLocaleString() : ""; };
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// ------------------------------------------------------------ sign in
function loginView(message) {
  const email = h("input", { type: "email", required: true, autocomplete: "username", maxlength: 254 });
  const pass = h("input", { type: "password", required: true, autocomplete: "current-password", maxlength: 200 });
  const err = h("div", { class: "error", role: "alert" }, message || "");
  const form = h("form", { onsubmit: async (e) => {
    e.preventDefault(); err.textContent = "";
    try { const r = await api("POST", "/api/platform/login", { email: email.value, password: pass.value }); CSRF = r.csrf; start(); }
    catch (ex) { err.textContent = ex.message; }
  } }, h("label", null, "Administrator email", email), h("label", null, "Password", pass),
    h("div", { class: "form-actions" }, h("button", { class: "primary", type: "submit" }, "Sign in")), err);
  $app.replaceChildren(h("div", { class: "auth" }, h("h1", null, "Server administration"),
    h("div", { class: "card" }, form),
    h("p", { class: "muted" }, "This is the server administrator login (created with ‘server.py create-admin’), not a shop login.")));
}

// ------------------------------------------------------------ software updates
function updatesCard() {
  const body = h("div", null, h("p", { class: "muted" }, "Loading…"));
  const card = h("section", { class: "card" }, h("h2", null, "Software updates"), body);
  let latest = null;

  function row(label, value) { return h("p", null, h("span", { class: "muted" }, label + " "), h("strong", null, value)); }

  async function check() {
    body.replaceChildren(h("p", { class: "muted" }, "Checking…"));
    try { latest = await api("GET", "/api/update/check"); show(); }
    catch (e) { showBasic(e.message); }
  }

  async function showBasic(error) {
    let v = "";
    try { v = (await api("GET", "/api/version")).version; } catch (e) { /* ignore */ }
    body.replaceChildren(row("Current version:", v || "unknown"),
      error ? h("p", { class: "error", role: "alert" }, error) : null,
      h("div", { class: "form-actions" }, h("button", { type: "button", onclick: check }, "Check for updates")));
  }

  function show() {
    const r = latest;
    const parts = [row("Current version:", r.current), row("Latest release:", r.latest + (r.publishedAt ? " (" + when(r.publishedAt) + ")" : ""))];
    if (r.warning) parts.push(h("p", { class: "muted" }, r.warning + " Showing the last result."));
    if (r.updateAvailable) {
      parts.push(h("p", { class: "ok" }, "Version " + r.latest + " is available."));
      if (r.notes) parts.push(h("h3", null, "Release notes"), h("pre", { class: "release-notes" }, r.notes));
      if (!r.updaterInstalled) parts.push(h("p", { class: "error" }, "The updater is not installed on this server yet. See RELEASING.md, ‘Existing installs’."));
      else if (!r.installable) parts.push(h("p", { class: "error" }, "This release has no installable files yet."));
      parts.push(h("p", { class: "muted" }, "ShopDesk will restart for about a minute for every shop. A backup is taken first and the update rolls back by itself if anything goes wrong."));
      const btn = h("button", { class: "primary", type: "button", disabled: !(r.updaterInstalled && r.installable),
        onclick: () => { if (confirm("Update ShopDesk to " + r.latest + " now? It restarts for all shops.")) apply(); } }, "Update now");
      parts.push(h("div", { class: "form-actions" }, btn, " ", h("button", { type: "button", onclick: check }, "Check again")));
    } else {
      parts.push(h("p", { class: "ok" }, "ShopDesk is up to date."),
        h("div", { class: "form-actions" }, h("button", { type: "button", onclick: check }, "Check for updates")));
    }
    parts.push(h("p", { class: "muted" }, "Last checked " + when(r.checkedAt) + (r.cached ? " (results are cached for 10 minutes)" : "")));
    body.replaceChildren(...parts);
  }

  async function apply() {
    try { await api("POST", "/api/update/apply"); progress(latest.latest); }
    catch (e) { body.prepend(h("p", { class: "error", role: "alert" }, e.message)); }
  }

  // Poll every 2 seconds. The API is briefly unreachable while the service restarts; that is expected.
  async function progress(target) {
    const msg = h("p", null, "Starting…");
    const step = h("p", { class: "muted" });
    body.replaceChildren(h("p", null, h("strong", null, "Updating to " + target)), msg, step,
      h("p", { class: "muted" }, "Keep this page open. It reloads when the update is done."));
    let misses = 0, sawRunning = false;
    for (;;) {
      await sleep(2000);
      let st;
      try { st = await api("GET", "/api/update/status"); misses = 0; }
      catch (e) {
        if (e.status === 401) return location.reload();       // session ended: sign in again
        misses++;                                              // restarting: keep trying
        step.textContent = "ShopDesk is restarting… (" + misses + ")";
        if (misses > 90) { msg.textContent = "The server has not come back after 3 minutes."; msg.className = "error"; return; }
        continue;
      }
      if (st.state === "queued" || st.state === "running") sawRunning = true;
      msg.textContent = st.message || "Working…";
      step.textContent = st.step ? "Step: " + st.step : "";
      if (st.state === "success") {
        const v = (await api("GET", "/api/version").catch(() => ({ version: st.targetVersion }))).version;
        msg.textContent = "Updated to " + v + ". Reloading…";
        return setTimeout(() => location.reload(), 1500);
      }
      if (st.state === "failed") {
        body.replaceChildren(h("p", { class: "error", role: "alert" }, "Update failed. " + (st.rolledBack ? "It was rolled back; your data and the previous version are unchanged." : "")),
          st.error ? h("p", null, st.error) : null, st.message ? h("p", { class: "muted" }, st.message) : null,
          h("div", { class: "form-actions" }, h("button", { type: "button", onclick: check }, "Check for updates")));
        return;
      }
      if (st.state === "idle" && sawRunning) { location.reload(); return; }
    }
  }

  // If an update is already in progress (page reloaded mid-update), pick the progress view back up.
  api("GET", "/api/update/status").then(st => {
    if (st.state === "queued" || st.state === "running") progress(st.targetVersion || "the new version");
    else if (st.state === "failed" && st.error) { showBasic(); body.prepend(h("p", { class: "error" }, "The last update failed" + (st.rolledBack ? " and was rolled back" : "") + ": " + st.error)); }
    else check();
  }).catch(() => check());

  return card;
}

// ------------------------------------------------------------ shops (metadata only: no customers, tickets, invoices or notes)
const bytesText = (n) => n === null || n === undefined ? "" : n < 1024 * 1024 ? Math.max(1, Math.round(n / 1024)) + " KB" : (n / 1048576).toFixed(1) + " MB";
const dayText = (iso) => iso ? iso.slice(0, 10) : "";
const refreshers = [];
const refreshAll = () => refreshers.forEach(fn => fn());

function shopsCard() {
  const notice = h("div");
  const err = h("div", { class: "error", role: "alert" });
  const body = h("div", null, h("p", { class: "muted" }, "Loading…"));
  const search = h("input", { type: "search", placeholder: "Search shop, owner or email", maxlength: 100, "aria-label": "Search shops" });
  let shops = [], sortKey = "created_at", dir = 1;
  const card = h("section", { class: "card" }, h("h2", null, "Shops"),
    h("p", { class: "muted" }, "Shop details only. This console never shows a shop's customers, tickets, invoices or notes."), search, notice, err, body);

  async function act(fn) { err.textContent = ""; try { await fn(); } catch (e) { err.textContent = e.message; } }
  async function setStatus(s, status) {
    if (!confirm((status === "suspended" ? "Suspend " : "Restore ") + s.name + "? " + (status === "suspended" ? "Nobody in that shop can sign in until it is restored." : ""))) return;
    await act(async () => { await api("POST", "/api/platform/tenants/" + s.id + "/status", { status }); load(); refreshAll(); });
  }
  async function resetPassword(s) {
    await act(async () => {
      const { owners } = await api("GET", "/api/platform/shops/" + s.id + "/owners");
      if (!owners.length) throw new Error("This shop has no active owner account.");
      let owner = owners[0];
      if (owners.length > 1) {
        const pick = prompt("Which owner? Type the number:\n" + owners.map((o, i) => (i + 1) + ". " + o.name + " <" + o.email + ">").join("\n"));
        owner = owners[parseInt(pick, 10) - 1];
        if (!owner) return;
      }
      if (!confirm("Create a one-time temporary password for " + owner.name + " (" + owner.email + ")? Their current password stops working and they are signed out.")) return;
      const r = await api("POST", "/api/platform/shops/" + s.id + "/reset-owner-password", { user_id: owner.id });
      notice.replaceChildren(h("div", { class: "card notice" }, h("h3", null, "Temporary password for " + r.owner.email),
        h("p", null, h("code", { class: "temp-password" }, r.temporary_password)),
        h("p", { class: "muted" }, "Shown once: copy it now and give it to the owner. It works for one sign-in within " + r.expires_hours + " hours; they must then choose their own password."),
        h("button", { type: "button", onclick: () => notice.replaceChildren() }, "I have copied it")));
      refreshAll();
    });
  }
  async function remove(s) {
    const typed = prompt("This suspends " + s.name + " and moves its database to the trash for 30 days (it can be restored until then).\n\nType the shop's exact name to confirm:");
    if (typed === null) return;
    await act(async () => { await api("POST", "/api/platform/shops/" + s.id + "/delete", { confirm_name: typed }); load(); refreshAll(); });
  }

  function draw() {
    const q = search.value.trim().toLowerCase();
    const list = shops.filter(s => !q || [s.name, s.owner_name, s.owner_email].join(" ").toLowerCase().includes(q));
    list.sort((a, b) => { const x = a[sortKey], y = b[sortKey]; if (x === y) return 0; if (x === null || x === undefined || x === "") return 1; if (y === null || y === undefined || y === "") return -1; return (x < y ? -1 : 1) * dir; });
    const heads = [["name", "Shop"], ["owner_name", "Owner"], ["created_at", "Created"], ["last_activity", "Last activity"], ["user_count", "Users"],
      ["ticket_count", "Tickets"], ["db_bytes", "Database"], ["status", "Status"]];
    body.replaceChildren(h("p", { class: "muted" }, list.length + " of " + shops.length + " shops"),
      h("div", { class: "table-wrap" }, h("table", null,
        h("thead", null, h("tr", null, heads.map(([k, l]) => h("th", { "aria-sort": sortKey === k ? (dir > 0 ? "ascending" : "descending") : "none" },
          h("button", { type: "button", class: "sort-btn", onclick: () => { if (sortKey === k) dir = -dir; else { sortKey = k; dir = 1; } draw(); } }, l + (sortKey === k ? (dir > 0 ? " ▲" : " ▼") : "")))),
          h("th", null, "Actions"))),
        h("tbody", null, list.map(s => h("tr", null,
          h("td", null, s.name), h("td", null, s.owner_name, h("div", { class: "muted small" }, s.owner_email)),
          h("td", null, dayText(s.created_at)), h("td", null, s.db_missing ? "database missing" : when(s.last_activity)),
          h("td", null, s.user_count === null ? "" : String(s.user_count)), h("td", null, s.ticket_count === null ? "" : String(s.ticket_count)),
          h("td", null, bytesText(s.db_bytes)), h("td", null, s.status),
          h("td", null, s.status === "pending deletion" ? h("span", { class: "muted" }, "Restore from “Recently deleted”") : h("div", { class: "form-actions wrap" },
            s.status === "active" ? h("button", { type: "button", onclick: () => setStatus(s, "suspended") }, "Suspend") : h("button", { type: "button", onclick: () => setStatus(s, "active") }, "Restore"),
            h("button", { type: "button", onclick: () => resetPassword(s) }, "Reset owner password"),
            h("button", { type: "button", onclick: () => remove(s) }, "Delete…")))))))));
  }
  async function load() { await act(async () => { shops = (await api("GET", "/api/platform/shops")).shops; draw(); }); }
  search.addEventListener("input", draw);
  refreshers.push(load);
  load();
  return card;
}

function trashCard() {
  const body = h("div");
  const err = h("div", { class: "error", role: "alert" });
  async function load() {
    try {
      const r = await api("GET", "/api/platform/trash");
      body.replaceChildren(r.deleted.length ? h("div", { class: "table-wrap" }, h("table", null,
        h("thead", null, h("tr", null, ["Shop", "Owner", "Deleted", "Purged automatically", ""].map(t => h("th", null, t)))),
        h("tbody", null, r.deleted.map(d => h("tr", null, h("td", null, d.name), h("td", null, d.owner_email), h("td", null, when(d.deleted_at)), h("td", null, dayText(d.purge_after)),
          h("td", null, h("button", { type: "button", onclick: async () => {
            err.textContent = "";
            try { await api("POST", "/api/platform/trash/" + d.id + "/restore", {}); refreshAll(); } catch (e) { err.textContent = e.message; }
          } }, "Restore"))))))) : h("p", { class: "muted" }, "Nothing has been deleted recently."));
    } catch (e) { err.textContent = e.message; }
  }
  refreshers.push(load);
  load();
  return h("section", { class: "card" }, h("h2", null, "Recently deleted"),
    h("p", { class: "muted" }, "Deleted shops stay here for 30 days and are then removed for good. Restoring brings the shop back suspended; use Restore on the Shops list to let its staff sign in again."), body, err);
}

function auditCard() {
  const body = h("div");
  async function load() {
    try {
      const { events } = await api("GET", "/api/platform/audit?limit=100");
      body.replaceChildren(h("div", { class: "table-wrap" }, h("table", null,
        h("thead", null, h("tr", null, ["When", "Who", "Action", "Shop", "Details"].map(t => h("th", null, t)))),
        h("tbody", null, events.map(e => h("tr", null, h("td", null, when(e.ts)), h("td", null, e.actor_email || "system"), h("td", null, e.action),
          h("td", null, e.shop_name || ""), h("td", null, Object.keys(e.detail).length ? JSON.stringify(e.detail) : "")))))));
    } catch (e) { body.replaceChildren(h("p", { class: "error" }, e.message)); }
  }
  refreshers.push(load);
  load();
  return h("section", { class: "card" }, h("h2", null, "Admin audit log"), h("p", { class: "muted" }, "Every administrator action: who, what, which shop, when (latest 100)."), body);
}
// ------------------------------------------------------------ page
async function start() {
  try {
    const me = await api("GET", "/api/platform/me");
    CSRF = me.csrf;
    refreshers.length = 0;
    render(h("div", { class: "form-actions" }, h("h1", null, "Server administration"), " ",
      h("button", { type: "button", onclick: async () => { try { await api("POST", "/api/platform/logout", {}); } catch (e) { /* ignore */ } loginView(); } }, "Sign out")),
      h("p", { class: "muted" }, "Signed in as " + me.admin.email), updatesCard(), shopsCard(), trashCard(), auditCard());
  } catch (e) { loginView(); }
}

start();
