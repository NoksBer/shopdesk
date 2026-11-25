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

// ------------------------------------------------------------ page
async function start() {
  try {
    const me = await api("GET", "/api/platform/me");
    CSRF = me.csrf;
    render(h("div", { class: "form-actions" }, h("h1", null, "Server administration"), " ",
      h("button", { type: "button", onclick: async () => { try { await api("POST", "/api/platform/logout", {}); } catch (e) { /* ignore */ } loginView(); } }, "Sign out")),
      h("p", { class: "muted" }, "Signed in as " + me.admin.email), updatesCard());
  } catch (e) { loginView(); }
}

start();
