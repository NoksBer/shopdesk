"use strict";
// ShopDesk staff app. All DOM is built with textContent / createTextNode; never innerHTML with data.

const root = document.getElementById("app");
let csrf = "";
let ME = null;

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

async function api(method, path, body) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (csrf && method !== "GET") headers["X-CSRF-Token"] = csrf;
  const res = await fetch(path, { method, headers, credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body) });
  let data = {};
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok) {
    const err = new Error(data.error || ("Request failed (" + res.status + ")"));
    err.status = res.status;
    throw err;
  }
  return data;
}

const field = (label, input) => h("div", null, h("label", null, label), input);
const render = (...nodes) => root.replaceChildren(...nodes);
const can = (p) => ME.permissions.includes("*") || ME.permissions.includes(p);
const term = (k, fallback) => (ME && ME.terminology && ME.terminology[k]) || fallback;

// ------------------------------------------------------------ signed-out screens
function authScreen(boot) {
  let mode = boot.can_register && location.hash === "#/signup" ? "register" : "login";
  const errorBox = h("div", { class: "error", role: "alert" });

  const email = h("input", { type: "email", autocomplete: "username", required: true });
  const password = h("input", { type: "password", autocomplete: "current-password", required: true });
  const loginForm = h("form", { onsubmit: async (e) => {
      e.preventDefault(); errorBox.textContent = "";
      try { await api("POST", "/api/login", { email: email.value, password: password.value }); await start(); }
      catch (err) { errorBox.textContent = err.message; }
    } },
    field("Email", email), field("Password", password),
    h("div", { class: "form-actions" }, h("button", { class: "primary", type: "submit" }, "Sign in")));

  const bName = h("input", { required: true, maxlength: 120 });
  const bType = h("select", null, boot.shop_types.map(t => h("option", { value: t.key }, t.label)));
  const oName = h("input", { required: true, maxlength: 120, autocomplete: "name" });
  const oEmail = h("input", { type: "email", required: true, autocomplete: "username" });
  const oPass = h("input", { type: "password", required: true, minlength: 10, autocomplete: "new-password" });
  const oPass2 = h("input", { type: "password", required: true, minlength: 10, autocomplete: "new-password" });
  const regForm = h("form", { onsubmit: async (e) => {
      e.preventDefault(); errorBox.textContent = "";
      if (oPass.value !== oPass2.value) { errorBox.textContent = "The two passwords do not match."; return; }
      try {
        await api("POST", "/api/register", { business_name: bName.value, shop_type: bType.value,
          name: oName.value, email: oEmail.value, password: oPass.value });
        await start();
      } catch (err) { errorBox.textContent = err.message; }
    } },
    field("Business name", bName), field("What kind of shop?", bType),
    field("Your name", oName), field("Your email (this is your login)", oEmail), field("Password (10+ characters)", oPass),
    field("Repeat password", oPass2),
    h("div", { class: "form-actions" }, h("button", { class: "primary", type: "submit" }, "Create my shop")));

  function draw() {
    errorBox.textContent = "";
    const tabs = boot.can_register
      ? h("div", { class: "tabs" },
          h("button", { type: "button", class: mode === "login" ? "active" : "", onclick: () => { mode = "login"; draw(); } }, "Sign in"),
          h("button", { type: "button", class: mode === "register" ? "active" : "", onclick: () => { mode = "register"; draw(); } }, "Create your shop"))
      : null;
    render(h("div", { class: "auth" },
      h("div", { class: "brand-lockup" }, h("img", { src: "/logo.svg", alt: "" }), h("h1", null, "ShopDesk")),
      h("p", { class: "muted" }, mode === "register"
        ? "Set up your own shop in a minute. Your business gets its own private database, separate from every other shop, and you become its owner."
        : "Sign in to your shop. New here? Choose “Create your shop”."), tabs,
      h("div", { class: "card" }, mode === "login" ? loginForm : regForm, errorBox),
      boot.can_register ? null : h("p", { class: "muted" }, "New businesses are created by the platform administrator.")));
  }
  draw();
}

function inviteScreen(token) {
  const errorBox = h("div", { class: "error", role: "alert" });
  const name = h("input", { required: true, maxlength: 120, autocomplete: "name" });
  const pass = h("input", { type: "password", required: true, minlength: 10, autocomplete: "new-password" });
  api("POST", "/api/invites/preview", { token }).then((info) => {
    render(h("div", { class: "auth" }, h("h1", null, "Join " + info.business_name),
      h("div", { class: "card" },
        h("p", { class: "muted" }, "You were invited as " + info.role_label + " (" + info.email + ")."),
        h("form", { onsubmit: async (e) => {
            e.preventDefault(); errorBox.textContent = "";
            try {
              await api("POST", "/api/invites/accept", { token, name: name.value, password: pass.value });
              history.replaceState(null, "", location.pathname);
              await start();
            } catch (err) { errorBox.textContent = err.message; }
          } },
          field("Your name", name), field("Choose a password (10+ characters)", pass),
          h("div", { class: "form-actions" }, h("button", { class: "primary", type: "submit" }, "Join")),
          errorBox))));
  }).catch((err) => render(h("p", { class: "center error" }, err.message)));
}

// ------------------------------------------------------------ shell + routing
function versionFooter() {              // small muted "ShopDesk v0.0.10" line at the bottom of every page
  const el = h("footer", { class: "app-version muted small" });
  const show = (v) => { if (v) el.textContent = "ShopDesk v" + v; };
  show(LOADED_VERSION);                 // the version this page was loaded with (from common.js)
  if (!LOADED_VERSION) api("GET", "/api/version").then((r) => show(r.version)).catch(() => {});
  return el;
}

async function dashboard(me) {
  ME = me;
  BIZ = null;
  document.body.classList.toggle("mobile-first", !!(me.ticket_options && me.ticket_options.mobile));   // phone-friendly layout (auto tinting)
  try { setBrand(await api("GET", "/api/branding")); } catch (e) { setBrand(null); }
  document.title = me.business.name;
  const nav = h("nav", { class: "nav", id: "nav" });
  render(
    h("header", { class: "top" }, h("h1", { id: "brand" }, brandMark()),
      h("div", { class: "top-right" }, h("span", { id: "timer-box", class: "timer-box" }),
        h("span", { class: "muted" }, me.user.name + " · " + me.user.role_label + "  "),
        h("button", { onclick: async () => { try { await api("POST", "/api/logout", {}); } finally { csrf = ""; ME = null; start(); } } }, "Sign out"))),
    nav,
    h("main", { id: "content" }),
    versionFooter());
  window.onhashchange = route;
  await route();
}

// Top navigation: single links plus three drop-down groups (Records, Billing, gear). Only entries the user may open show.
function closeMenus() {
  document.querySelectorAll("#nav .menu.open").forEach((m) => {
    m.classList.remove("open");
    m.firstChild.setAttribute("aria-expanded", "false");
  });
}
document.addEventListener("click", closeMenus);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenus(); });

function drawNav(active) {
  const link = ([href, label]) => h("a", { href, class: active === href ? "active" : "", onclick: closeMenus }, label);
  const group = (label, entries, opts = {}) => {
    if (!entries.length) return null;
    const btn = h("button", { type: "button", class: "menu-btn" + (entries.some(([href]) => href === active) ? " active" : ""),
      "aria-haspopup": "true", "aria-expanded": "false", "aria-label": opts.aria, title: opts.aria,
      onclick: (e) => {
        e.stopPropagation();
        const wasOpen = wrap.classList.contains("open");
        closeMenus();
        if (!wasOpen) { wrap.classList.add("open"); btn.setAttribute("aria-expanded", "true"); }
      } }, label, opts.aria ? null : " ▾");
    const wrap = h("div", { class: "menu" + (opts.right ? " right" : "") }, btn,
      h("div", { class: "menu-list" }, entries.map(link)));
    return wrap;
  };
  const only = (perm, entry) => (can(perm) ? [entry] : []);
  const items = [link(["#/dashboard", "Dashboard"])];
  if (can("tickets.read")) items.push(link(["#/board", "Queue"]), link(["#/tickets", jw("Tickets")]), link(["#/calendar", "Calendar"]));
  items.push(group("Records", [...only("customers.read", ["#/customers", "Customers"]), ...only("inventory.read", ["#/inventory", "Inventory"]),
    ...only("services.read", ["#/services", "Services"]), ...only("time.write", ["#/time", "Time"])]));
  items.push(group("Billing", [...only("invoices.read", ["#/invoices", "Invoices"]), ...only("ledger.read", ["#/ledger", "Ledger"])]));
  items.push(group("⚙", [...only("audit.read", ["#/activity", "Activity"]), ["#/settings", "Settings"]],
    { right: true, aria: "Activity and settings" }));
  document.getElementById("nav").replaceChildren(...items.filter(Boolean));
}

async function route() {
  const content = document.getElementById("content");
  if (!content || !ME) return;
  const hash = location.hash;
  const path = hash.split("?")[0];
  let active = "#/settings", view;
  if (hash === "" || hash === "#/" || hash === "#/dashboard") { active = "#/dashboard"; view = dashboardView; }
  else if (path === "#/board" && can("tickets.read")) { active = "#/board"; view = boardView; }
  else if (path === "#/calendar" && can("tickets.read")) { active = "#/calendar"; view = calendarView; }
  else if (path === "#/tickets/new" && can("tickets.write") && can("customers.read")) { active = "#/tickets"; view = newTicketView; }
  else if (path === "#/invoices/new" && can("invoices.write")) { active = "#/invoices"; view = invoiceNewView; }
  else if (path.startsWith("#/invoices/") && can("invoices.read")) { active = "#/invoices"; view = () => invoiceDetail(decodeURIComponent(path.slice(11))); }
  else if (path === "#/invoices" && can("invoices.read")) { active = "#/invoices"; view = invoicesView; }
  else if (path === "#/ledger" && can("ledger.read")) { active = "#/ledger"; view = ledgerView; }
  else if (path === "#/time" && can("time.write")) { active = "#/time"; view = timeView; }
  else if (path === "#/inventory" && can("inventory.read")) { active = "#/inventory"; view = inventoryView; }
  else if (path === "#/services" && can("services.read")) { active = "#/services"; view = servicesView; }
  else if (hash.startsWith("#/tickets/") && can("tickets.read")) { active = "#/tickets"; view = () => ticketDetail(decodeURIComponent(hash.slice(10))); }
  else if (hash.startsWith("#/tickets") && can("tickets.read")) { active = "#/tickets"; view = ticketsView; }
  else if (hash.startsWith("#/customers/") && can("customers.read")) { active = "#/customers"; view = () => customerDetail(decodeURIComponent(hash.slice(12))); }
  else if (hash.startsWith("#/customers") && can("customers.read")) { active = "#/customers"; view = customersView; }
  else if (hash === "#/activity" && can("audit.read")) { active = "#/activity"; view = activityView; }
  else if (path === "#/import" && can("data.import")) { active = "#/settings"; view = importView; }
  else view = settingsView;
  drawNav(active);
  paintBrand(BRAND);                                  // drops any unsaved colour preview from the settings page
  content.classList.toggle("wide", active === "#/board" || active === "#/dashboard" || path === "#/tickets");
  content.replaceChildren(h("p", { class: "muted" }, "Loading…"));
  try {
    content.replaceChildren(...await view());
    refreshTimer();                                   // keeps the header clock in step (also after a ticket closes)
  } catch (err) {
    if (err.status === 401) { csrf = ""; ME = null; return start(); }
    content.replaceChildren(h("p", { class: "error" }, err.message));
  }
}

const showError = (box) => (err) => { box.className = "error"; box.textContent = err.message; };

// ------------------------------------------------------------ customers
function customerForm(initial, submitLabel, onSubmit) {
  const msg = h("span", { class: "error", role: "alert" });
  const writable = can("customers.write");
  const f = {
    type: h("select", null, [["individual", "Individual"], ["business", "Business"]].map(([v, l]) => h("option", { value: v, selected: initial.type === v }, l))),
    name: h("input", { value: initial.name || "", required: true, maxlength: 120 }),
    contact_name: h("input", { value: initial.contact_name || "", maxlength: 120 }),
    email: h("input", { type: "email", value: initial.email || "", maxlength: 254 }),
    phone: h("input", { type: "tel", value: initial.phone || "", maxlength: 40 }),
    address: h("input", { value: initial.address || "", maxlength: 300 }),
    contact_pref: h("select", null, [["email", "Email"], ["phone", "Phone call"], ["text", "Text message"], ["none", "Do not contact"]]
      .map(([v, l]) => h("option", { value: v, selected: (initial.contact_pref || "email") === v }, l))),
    notes: h("textarea", { rows: "3", maxlength: 2000 }, initial.notes || ""),
  };
  if (!writable) for (const el of Object.values(f)) el.setAttribute("disabled", "");
  return h("form", { onsubmit: async (e) => {
      e.preventDefault(); msg.textContent = "";
      try { await onSubmit(Object.fromEntries(Object.entries(f).map(([k, el]) => [k, el.value]))); }
      catch (err) { msg.textContent = err.message; }
    } },
    field("Type", f.type), field("Name / business name", f.name), field("Contact person", f.contact_name),
    field("Email", f.email), field("Phone", f.phone), field("Address", f.address),
    field("Preferred contact", f.contact_pref), field("Notes", f.notes),
    writable ? h("div", { class: "form-actions" }, h("button", { class: "primary", type: "submit" }, submitLabel), msg) : null);
}

async function customersView() {
  const list = h("div");
  const errBox = h("div", { class: "error" });
  const search = h("input", { type: "search", placeholder: "Search name, email or phone", maxlength: 100 });
  async function load() {
    const q = search.value.trim();
    const r = await api("GET", "/api/customers?q=" + encodeURIComponent(q));
    list.replaceChildren(r.customers.length
      ? h("div", { class: "table-wrap" }, h("table", null,
          h("thead", null, h("tr", null, ["Name", "Contact", term("items", "Items")].map(t => h("th", null, t)))),
          h("tbody", null, r.customers.map(c => h("tr", null,
            h("td", null, h("a", { href: "#/customers/" + encodeURIComponent(c.id) }, c.name)),
            h("td", null, c.phone || c.email || ""), h("td", null, c.equipment_count))))))
      : h("p", { class: "muted" }, q ? "No matches." : "No customers yet."));
  }
  let timer;
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => load().catch(showError(errBox)), 250); });
  await load();
  const nodes = [h("section", { class: "card" }, h("h2", null, "Customers"), search, errBox, list)];
  if (can("customers.write")) {
    nodes.unshift(h("section", { class: "card" },
      h("details", null, h("summary", null, "New customer"),
        customerForm({}, "Create customer", async (data) => {
          const c = await api("POST", "/api/customers", data);
          location.hash = "#/customers/" + encodeURIComponent(c.id);
        }))));
  }
  return nodes;
}

async function customerDetail(id) {
  const [customer, defsRes, equipRes] = await Promise.all([
    api("GET", "/api/customers/" + encodeURIComponent(id)),
    can("equipment.read") ? api("GET", "/api/equipment/fields") : Promise.resolve({ fields: [] }),
    can("equipment.read") ? api("GET", "/api/equipment?customer_id=" + encodeURIComponent(id)) : Promise.resolve({ equipment: [] }),
  ]);
  const msg = h("div", { class: "ok" });
  const nodes = [h("p", null, h("a", { href: "#/customers" }, "← All customers"))];

  const head = [h("h2", null, customer.name + (customer.archived_at ? " (archived)" : "")),
    customerForm(customer, "Save changes", async (data) => {
      await api("PUT", "/api/customers/" + encodeURIComponent(id), data);
      msg.textContent = "Saved"; route();
    })];
  if (can("customers.write")) {
    head.push(h("div", { class: "form-actions" }, h("button", { onclick: async () => {
        try {
          if (customer.archived_at) await api("PUT", "/api/customers/" + encodeURIComponent(id), { ...customer, archived: false });
          else await api("DELETE", "/api/customers/" + encodeURIComponent(id));
          route();
        } catch (err) { msg.className = "error"; msg.textContent = err.message; }
      } }, customer.archived_at ? "Restore customer" : "Archive customer"), msg));
  }
  if (!customer.archived_at) {
    const links = [];
    if (can("tickets.write")) links.push(h("a", { class: "btn", href: "#/tickets/new?customer=" + encodeURIComponent(id) }, jw("+ New ticket")));
    if (can("invoices.write")) links.push(h("a", { class: "btn", href: "#/invoices/new?customer=" + encodeURIComponent(id) }, "+ New invoice"));
    if (links.length) head.push(h("div", { class: "form-actions" }, links));
  }
  nodes.push(h("section", { class: "card" }, head));

  if (can("equipment.read")) nodes.push(equipmentCard(customer, defsRes.fields, equipRes.equipment));
  if (can("tickets.read")) {
    const { tickets } = await api("GET", "/api/tickets?customer_id=" + encodeURIComponent(id) + "&limit=100");
    // Invoices are only fetched (and shown) for people whose role may view invoices.
    let byTicket = null, more = false;
    if (can("invoices.read")) {
      await biz();
      const inv = await api("GET", "/api/invoices?customer_id=" + encodeURIComponent(id) + "&limit=200");
      byTicket = {};
      for (const i of inv.invoices) if (i.ticket_id) (byTicket[i.ticket_id] = byTicket[i.ticket_id] || []).push(i);
      more = inv.total > inv.invoices.length;
    }
    nodes.push(h("section", { class: "card" }, h("h2", null, "Repair history"),
      tickets.length ? ticketTable(tickets, byTicket) : h("p", { class: "muted" }, jw("No tickets yet.")),
      more ? h("p", { class: "muted small" }, jw("Only the 200 most recent invoices are matched to tickets here.")) : null));
  }
  return nodes;
}

// One line per invoice linked to a ticket: number, date, total, status and a link to open it.
function invoiceLines(list) {
  return h("ul", { class: "history ticket-invoices" }, list.map(i => h("li", null, "Invoice ",
    h("a", { href: "#/invoices/" + encodeURIComponent(i.id) }, i.number || "Draft"),
    " · " + (i.issue_date || (i.created_at || "").slice(0, 10)) + " · " + money(i.total_cents) + " · " + i.status)));
}

// ------------------------------------------------------------ equipment
function equipmentForm(defs, initial, submitLabel, onSubmit) {
  const msg = h("span", { class: "error", role: "alert" });
  const base = {
    name: h("input", { value: initial.name || "", required: true, maxlength: 120 }),
    serial_number: h("input", { value: initial.serial_number || "", maxlength: 80 }),
    description: h("textarea", { rows: "2", maxlength: 1000 }, initial.description || ""),
    purchase_date: h("input", { type: "date", value: initial.purchase_date || "" }),
    warranty_expires: h("input", { type: "date", value: initial.warranty_expires || "" }),
  };
  const extra = {};
  for (const d of defs) {
    if (d.key === "serial_number") continue;
    const type = d.type === "number" ? "number" : d.type === "date" ? "date" : "text";
    extra[d.key] = h("input", { type, step: type === "number" ? "any" : null, maxlength: 500,
      value: (initial.fields && initial.fields[d.key]) || "" });
  }
  const writable = can("equipment.write");
  if (!writable) for (const el of [...Object.values(base), ...Object.values(extra)]) el.setAttribute("disabled", "");
  return h("form", { onsubmit: async (e) => {
      e.preventDefault(); msg.textContent = "";
      const body = Object.fromEntries(Object.entries(base).map(([k, el]) => [k, el.value]));
      body.fields = Object.fromEntries(Object.entries(extra).map(([k, el]) => [k, el.value]));
      try { await onSubmit(body); } catch (err) { msg.textContent = err.message; }
    } },
    field(eg("vehicle_name", "Name (e.g. make and model)"), base.name),
    defs.filter(d => d.key !== "serial_number").map(d => field(d.label, extra[d.key])),
    field("Serial number / identifier", base.serial_number),
    field("Description / condition at intake", base.description),
    field("Purchase date", base.purchase_date), field("Warranty expires", base.warranty_expires),
    writable ? h("div", { class: "form-actions" }, h("button", { class: "primary", type: "submit" }, submitLabel), msg) : null);
}

function equipmentCard(customer, defs, items) {
  const item = term("item", "Item");
  const card = h("section", { class: "card" }, h("h2", null, term("items", "Items")));
  const labels = Object.fromEntries(defs.map(d => [d.key, d.label]));
  if (!items.length) card.append(h("p", { class: "muted" }, "No " + term("items", "items").toLowerCase() + " yet."));
  for (const e of items) {
    const summary = Object.entries(e.fields || {}).map(([k, v]) => (labels[k] || k) + ": " + v).join(" · ");
    const body = [h("strong", null, e.name), e.serial_number ? h("span", { class: "muted" }, "  #" + e.serial_number) : null,
      summary ? h("div", { class: "muted" }, summary) : null];
    const box = h("div", { class: "item" }, body);
    if (can("equipment.write")) {
      box.append(h("details", null, h("summary", null, "Edit"),
        equipmentForm(defs, e, "Save", async (data) => { await api("PUT", "/api/equipment/" + encodeURIComponent(e.id), data); route(); }),
        h("div", { class: "form-actions" }, h("button", { onclick: async () => {
            try { await api("DELETE", "/api/equipment/" + encodeURIComponent(e.id)); route(); }
            catch (err) { alert(err.message); }
          } }, "Archive " + item.toLowerCase()))));
    }
    card.append(box);
  }
  if (can("equipment.write") && !customer.archived_at) {
    card.append(h("details", null, h("summary", null, "Add " + item.toLowerCase()),
      equipmentForm(defs, {}, "Add " + item.toLowerCase(), async (data) => {
        await api("POST", "/api/equipment", { ...data, customer_id: customer.id }); route();
      })));
  }
  return card;
}

// ------------------------------------------------------------ dashboard
async function dashboardView() {
  const { tickets, revenue, ledger, time, touch_bases } = await api("GET", "/api/dashboard");
  // Full-width layout: summary tiles on top, then a grid of cards (technician work first, management figures after).
  const nodes = [h("h2", { class: "dash-title" }, "Welcome, " + ME.user.name, h("span", { class: "muted" }, " · " + ME.business.name))];
  const grid = [], mgmt = [];
  if (revenue || ledger) await biz();
  if (ledger) mgmt.push(cashflowCard(ledger));
  if (revenue) mgmt.push(revenueCard(revenue));
  if (time && time.clocked_in) mgmt.push(clockedInCard(time.clocked_in));
  if (!tickets) return [...nodes, h("div", { class: "dash-grid" }, ...mgmt)];

  const c = tickets.counts;
  const tile = (label, value, href, warn) => h("a", { class: "tile" + (warn && value ? " warn" : ""), href: href || "#/tickets" },
    h("span", { class: "big" }, value === null || value === undefined ? "–" : String(value)), h("span", { class: "muted" }, label));
  nodes.push(h("div", { class: "tiles" },
    tile(jw("Open tickets"), c.open_count), tile("Overdue", c.overdue, "#/tickets", true), tile("Due in 7 days", c.due_week),
    ...(c.unassigned === null ? [] : [tile("Unassigned", c.unassigned, "#/tickets", true)]),
    tile("Closed in 7 days", c.closed_week)));

  grid.push(h("section", { class: "card dash-wide" }, h("h2", null, jw("My open tickets")),
    tickets.my_open.length ? ticketTable(tickets.my_open.map(t => ({ ...t, status_closed: 0, assignee_names: "" })))
      : h("p", { class: "muted" }, "Nothing assigned to you.")));

  const open = tickets.by_status.filter(s => !s.is_closed);
  grid.push(h("section", { class: "card" }, h("h2", null, "Open by status"),
    open.length ? h("ul", { class: "history" }, open.map(s => h("li", null, h("strong", null, String(s.n)), "  " + s.name))) : h("p", { class: "muted" }, "No statuses.")));

  if (touch_bases) grid.push(touchBaseCard(touch_bases));

  grid.push(h("section", { class: "card" }, h("h2", null, jw("Recent ticket activity")),
    tickets.recent.length ? h("ul", { class: "history" }, tickets.recent.map(e => h("li", null,
      h("span", { class: "muted" }, e.ts.replace("T", " ").replace("Z", "") + " · " + (e.user_name || "system") + " — "),
      h("a", { href: "#/tickets/" + encodeURIComponent(e.ticket_id) }, "#" + e.number), " " + describeHistory(e))))
      : h("p", { class: "muted" }, "No activity yet.")));
  grid.push(...mgmt);
  return [...nodes, h("div", { class: "dash-grid" }, ...grid)];
}

// ------------------------------------------------------------ activity (audit log)
const AUDIT_AREAS = [["", "Everything"], ["ticket.", "Tickets"], ["customer.", "Customers"], ["equipment.", "Equipment"],
  ["invoice.", "Invoices"], ["inventory.", "Inventory"], ["service.", "Services"],
  ["user.", "Team changes"], ["invite.", "Invites"], ["auth.", "Sign-ins"], ["business.", "Business profile"], ["settings.", "Settings"]];

function auditLink(e) {
  if (e.entity === "ticket" && e.entity_id) return h("a", { href: "#/tickets/" + encodeURIComponent(e.entity_id) }, "ticket");
  if (e.entity === "customer" && e.entity_id) return h("a", { href: "#/customers/" + encodeURIComponent(e.entity_id) }, "customer");
  if (e.entity === "invoice" && e.entity_id && can("invoices.read")) return h("a", { href: "#/invoices/" + encodeURIComponent(e.entity_id) }, "invoice");
  return e.entity || "";
}

async function activityView() {
  const { users } = can("users.read") ? await api("GET", "/api/users") : { users: [] };
  const area = h("select", null, AUDIT_AREAS.map(([v, l]) => h("option", { value: v }, jw(l))));
  const who = h("select", null, h("option", { value: "" }, "Everyone"), users.map(u => h("option", { value: u.id }, u.name)));
  const body = h("tbody");
  const more = h("button", { type: "button" }, "Load more");
  const errBox = h("div", { class: "error" });
  let nextBefore = null;

  async function load(append) {
    const p = new URLSearchParams({ limit: "50", action: area.value, user_id: who.value });
    if (append && nextBefore) p.set("before", String(nextBefore));
    const r = await api("GET", "/api/audit?" + p.toString());
    if (!append) body.replaceChildren();
    for (const e of r.events) {
      const detail = Object.entries(JSON.parse(e.detail || "{}")).map(([k, v]) => k + ": " + (Array.isArray(v) ? v.join(", ") : v)).join(" · ");
      body.append(h("tr", null, h("td", null, e.ts.replace("T", " ").replace("Z", "")), h("td", null, e.user_name || "system"),
        h("td", null, e.action), h("td", null, auditLink(e)), h("td", { class: "muted" }, detail)));
    }
    nextBefore = r.next_before;
    more.style.display = nextBefore ? "" : "none";
    if (!append && !r.events.length) body.append(h("tr", null, h("td", { colspan: "5", class: "muted" }, "No events.")));
  }
  const reload = () => load(false).catch(showError(errBox));
  area.addEventListener("change", reload);
  who.addEventListener("change", reload);
  more.addEventListener("click", () => load(true).catch(showError(errBox)));
  await load(false);
  return [h("section", { class: "card" }, h("h2", null, "Activity log"),
    h("div", { class: "filters" }, area, who), errBox,
    h("div", { class: "table-wrap" }, h("table", null,
      h("thead", null, h("tr", null, ["When (UTC)", "Who", "Action", "Item", "Details"].map(t => h("th", null, t)))), body)),
    h("div", { class: "form-actions" }, more))];
}

// ------------------------------------------------------------ tickets
const today = () => new Date().toISOString().slice(0, 10);

// invoicesByTicket (optional): { ticketId: [invoice, ...] } adds the linked invoices under each ticket (customer page).
function ticketTable(tickets, invoicesByTicket) {
  const rows = [];
  for (const t of tickets) {
    rows.push(h("tr", null,
      h("td", null, h("a", { href: "#/tickets/" + encodeURIComponent(t.id) }, t.number)),
      h("td", null, t.title), h("td", null, t.customer_name), h("td", null, t.status_name),
      h("td", { class: "prio-" + t.priority }, t.priority),
      h("td", { class: t.due_date && t.due_date < today() && !t.status_closed ? "overdue" : "" }, t.due_date || ""),
      h("td", null, t.assignee_names || "")));
    const inv = invoicesByTicket && invoicesByTicket[t.id];
    if (inv && inv.length) rows.push(h("tr", { class: "sub-row" }, h("td", null), h("td", { colspan: "6" }, invoiceLines(inv))));
  }
  return h("div", { class: "table-wrap" }, h("table", null,
    h("thead", null, h("tr", null, ["#", "Title", "Customer", "Status", "Priority", "Due", "Assigned"].map(t => h("th", null, t)))),
    h("tbody", null, rows)));
}

// Filter state survives re-renders (the page redraws after any action in the side panel).
const TK = { q: "", status: "open", priority: "", assignee: "", mine: false, overdue: false, sort: "", dir: "asc", shown: 0, scroll: 0 };
const TK_PAGE = 100;

async function ticketsView() {
  const statuses = (await api("GET", "/api/statuses")).statuses;
  const staff = can("tickets.assign") ? (await api("GET", "/api/staff")).staff : [];
  const openId = new URLSearchParams(location.hash.split("?")[1] || "").get("t");
  const errBox = h("div", { class: "error", role: "alert" });
  const count = h("span", { class: "muted tk-count", "aria-live": "polite" });
  const list = h("div", { class: "tk-scroll" });
  const panel = h("aside", { class: "tk-panel", "aria-label": jw("Ticket details") });
  const more = h("button", { type: "button" }, "Load more");
  const search = h("input", { type: "search", placeholder: "Search number, title, customer, serial", maxlength: 100, value: TK.q, "aria-label": jw("Search tickets") });
  const statusSel = h("select", { "aria-label": "Status" },
    [["", "All statuses"], ["open", "Open statuses"], ["closed", "Closed / cancelled statuses"]].map(([v, l]) => h("option", { value: v, selected: TK.status === v }, l)),
    statuses.map(s => h("option", { value: s.id, selected: TK.status === s.id }, s.name)));
  const prioSel = h("select", { "aria-label": "Priority" }, h("option", { value: "" }, "Any priority"),
    ["urgent", "high", "normal", "low"].map(p => h("option", { value: p, selected: TK.priority === p }, p)));
  const whoSel = staff.length ? h("select", { "aria-label": "Assigned to" }, h("option", { value: "" }, "Anyone"),
    staff.map(u => h("option", { value: u.id, selected: TK.assignee === u.id }, u.name))) : null;
  const mine = h("input", { type: "checkbox", checked: TK.mine });
  const overdue = h("input", { type: "checkbox", checked: TK.overdue });
  let tickets = [], total = 0, selected = openId, token = 0;

  async function load(append) {
    const p = new URLSearchParams({ q: TK.q.trim(), status_id: TK.status,
      limit: String(append ? TK_PAGE : Math.min(200, Math.max(TK_PAGE, TK.shown))), offset: String(append ? tickets.length : 0) });
    if (TK.mine) p.set("assignee", "me"); else if (TK.assignee) p.set("assignee", TK.assignee);
    if (TK.priority) p.set("priority", TK.priority);
    if (TK.overdue) p.set("overdue", "1");
    if (TK.sort) { p.set("sort", TK.sort); p.set("dir", TK.dir); }
    const r = await api("GET", "/api/tickets?" + p.toString());
    tickets = append ? tickets.concat(r.tickets) : r.tickets;
    total = r.total; TK.shown = tickets.length;
    draw();
  }
  const reload = () => load(false).catch(showError(errBox));

  function sortHead(key, label) {
    const on = TK.sort === key;
    return h("th", { "aria-sort": on ? (TK.dir === "asc" ? "ascending" : "descending") : "none" },
      h("button", { type: "button", class: "sort-btn", onclick: () => { if (on) TK.dir = TK.dir === "asc" ? "desc" : "asc"; else { TK.sort = key; TK.dir = "asc"; } reload(); } },
        label + (on ? (TK.dir === "asc" ? " ▲" : " ▼") : "")));
  }
  function draw() {
    count.textContent = total + (total === 1 ? jw(" ticket") : jw(" tickets")) + (tickets.length < total ? " (showing " + tickets.length + ")" : "");
    more.hidden = tickets.length >= total;
    if (!tickets.length) {
      list.replaceChildren(h("p", { class: "muted tk-empty" }, jw("No tickets match these filters.") + (TK.status !== "" ? jw(" Try “All statuses” to include closed and cancelled tickets.") : "")));
      return;
    }
    const rows = tickets.map(t => {
      const link = h("a", { href: "#/tickets/" + encodeURIComponent(t.id), onclick: (e) => {
        if (e.ctrlKey || e.metaKey || e.shiftKey || e.button) return;     // modified clicks keep the normal link behaviour
        if (window.matchMedia("(max-width: 900px)").matches) return;      // small screens: the full ticket page, as before
        e.preventDefault(); openPanel(t.id);
      } }, t.number);
      return h("tr", { class: "tk-row" + (t.id === selected ? " selected" : ""), "data-id": t.id,
        onclick: (e) => { if (!e.target.closest("a")) link.click(); } },
        h("td", null, link), h("td", { class: "tk-title" }, t.title, t.job_location ? h("div", { class: "muted small" }, "📍 " + t.job_location) : null), h("td", null, t.status_name),
        h("td", { class: "prio-" + t.priority }, t.priority), h("td", null, t.customer_name), h("td", null, t.equipment_names || ""),
        h("td", null, t.assignee_names || ""), h("td", null, (t.created_at || "").slice(0, 10)), h("td", null, (t.updated_at || "").slice(0, 10)),
        h("td", { class: t.due_date && t.due_date < today() && !t.status_closed ? "overdue" : "" }, (t.due_date || "") + (t.due_date && t.due_time ? " " + fmtTime(t.due_time) : "")));
    });
    list.replaceChildren(h("table", { class: "tk-table" },
      h("thead", null, h("tr", null, sortHead("number", "#"), h("th", null, "Title"), sortHead("status", "Status"), sortHead("priority", "Priority"),
        sortHead("customer", "Customer"), sortHead("item", "Item / device"), sortHead("assigned", "Assigned to"),
        sortHead("created", "Created"), sortHead("updated", "Updated"), sortHead("due", "Due"))),
      h("tbody", null, rows)));
  }

  function closePanel() {
    selected = null; token++;
    history.replaceState(null, "", "#/tickets");
    layout.classList.remove("has-panel"); panel.replaceChildren();
    list.querySelectorAll("tr.selected").forEach(r => r.classList.remove("selected"));
  }
  async function openPanel(id) {
    if (window.matchMedia("(max-width: 900px)").matches) { location.hash = "#/tickets/" + encodeURIComponent(id); return; }
    const mine_ = ++token;
    selected = id;
    history.replaceState(null, "", "#/tickets?t=" + encodeURIComponent(id));
    layout.classList.add("has-panel");
    list.querySelectorAll("tr.tk-row").forEach(r => r.classList.toggle("selected", r.dataset.id === id));
    const bar = h("div", { class: "tk-panel-bar" },
      h("a", { href: "#/tickets/" + encodeURIComponent(id) }, "Open full page"),
      h("button", { type: "button", onclick: closePanel, "aria-label": jw("Close ticket panel") }, "✕ Close"));
    panel.replaceChildren(bar, h("p", { class: "muted" }, "Loading…"));
    try {
      const nodes = await ticketDetail(id, true);
      if (mine_ === token) panel.replaceChildren(bar, ...nodes);
    } catch (err) { if (mine_ === token) panel.replaceChildren(bar, h("p", { class: "error" }, err.message)); }
  }

  let timer;
  search.addEventListener("input", () => { TK.q = search.value; clearTimeout(timer); timer = setTimeout(reload, 250); });
  statusSel.addEventListener("change", () => { TK.status = statusSel.value; reload(); });
  prioSel.addEventListener("change", () => { TK.priority = prioSel.value; reload(); });
  if (whoSel) whoSel.addEventListener("change", () => { TK.assignee = whoSel.value; reload(); });
  mine.addEventListener("change", () => { TK.mine = mine.checked; if (whoSel) whoSel.disabled = mine.checked; reload(); });
  overdue.addEventListener("change", () => { TK.overdue = overdue.checked; reload(); });
  more.addEventListener("click", () => load(true).catch(showError(errBox)));
  list.addEventListener("scroll", () => { TK.scroll = list.scrollTop; });
  if (whoSel) whoSel.disabled = TK.mine;

  const check = (label, input) => h("label", { class: "inline" }, input, " " + label);
  const newBtn = can("tickets.write") && can("customers.read") ? h("a", { class: "btn primary", href: "#/tickets/new" }, jw("+ New ticket")) : null;
  const layout = h("div", { class: "tk-layout" },
    h("section", { class: "card tk-main" },
      h("div", { class: "tk-head" }, h("h2", null, jw("Tickets")), count, newBtn),
      h("div", { class: "filters tk-filters" }, search, statusSel, prioSel, whoSel, check("Mine", mine), check("Overdue", overdue)),
      errBox, list, h("div", { class: "form-actions" }, more)),
    panel);
  layout.addEventListener("keydown", (e) => { if (e.key === "Escape" && selected) closePanel(); });
  await load(false);
  list.scrollTop = TK.scroll;
  if (openId) setTimeout(() => openPanel(openId), 0);
  return [layout];
}
async function newTicketView() {
  const pre = new URLSearchParams(location.hash.split("?")[1] || "").get("customer");
  let customer = null;
  if (pre) { try { customer = await api("GET", "/api/customers/" + encodeURIComponent(pre)); } catch (e) { customer = null; } }
  return [h("p", null, h("a", { href: "#/tickets" }, jw("← All tickets"))), await newTicketCard(customer)];
}

async function newTicketCard(initialCustomer) {
  const msg = h("span", { class: "error", role: "alert" });
  const equipBox = h("div");
  const title = h("input", { required: true, maxlength: 150, placeholder: eg("job_title", "e.g. Laptop won't boot") });
  const priority = h("select", null, ["low", "normal", "high", "urgent"].map(p => h("option", { value: p, selected: p === "normal" }, p)));
  const due = h("input", { type: "date" });
  const extra = jobExtraInputs(null);
  const symptoms = h("textarea", { rows: "2", maxlength: 2000 });
  const notes = h("textarea", { rows: "2", maxlength: 2000 });
  const staffBox = h("div");
  let equipChecks = [], staffChecks = [];

  if (can("tickets.assign")) {
    const { staff } = await api("GET", "/api/staff");
    staffChecks = staff.map(s => ({ id: s.id, el: h("input", { type: "checkbox" }) }));
    staffBox.append(h("label", null, "Assign to"), ...staff.map((s, i) => h("label", { class: "inline" }, staffChecks[i].el, " " + s.name)));
  }

  async function loadEquipment(customer) {
    equipChecks = []; equipBox.replaceChildren();
    if (!customer || !can("equipment.read")) return;
    try {
      const { equipment } = await api("GET", "/api/equipment?customer_id=" + encodeURIComponent(customer.id));
      equipChecks = equipment.map(e => ({ id: e.id, el: h("input", { type: "checkbox" }) }));
      if (equipment.length) equipBox.append(h("label", null, term("items", "Items") + " for this repair"),
        ...equipment.map((e, i) => h("label", { class: "inline" }, equipChecks[i].el, " " + e.name + (e.serial_number ? " #" + e.serial_number : ""))));
      else equipBox.append(h("p", { class: "muted" }, "No " + term("items", "items").toLowerCase() + " on file. Add one from the customer page."));
    } catch (err) { msg.textContent = err.message; }
  }
  const picker = customerPicker({ initial: initialCustomer, onChange: loadEquipment });
  if (initialCustomer) loadEquipment(initialCustomer);

  return h("section", { class: "card" }, h("h2", null, jw("New ticket")),
    h("form", { onsubmit: async (e) => {
        e.preventDefault(); msg.textContent = "";
        if (!picker.value()) { msg.textContent = "Choose a customer first."; return; }
        try {
          const t = await api("POST", "/api/tickets", {
            customer_id: picker.value().id, title: title.value, priority: priority.value, due_date: due.value, ...extra.values(due),
            symptoms: symptoms.value, intake_notes: notes.value,
            equipment_ids: equipChecks.filter(c => c.el.checked).map(c => c.id),
            assignee_ids: staffChecks.filter(c => c.el.checked).map(c => c.id) });
          location.hash = "#/tickets/" + encodeURIComponent(t.id);
        } catch (err) { msg.textContent = err.message; }
      } },
      h("label", null, "Customer"), picker.el, equipBox,
      field("Title", title), field("Priority", priority), field("Due date", due), ...extra.fields,
      field("Customer-reported symptoms", symptoms), field("Intake notes", notes), staffBox,
      h("div", { class: "form-actions" }, h("button", { class: "primary", type: "submit" }, jw("Create ticket")), msg)));
}

// Extra job fields some shop types turn on (profile option ticket_options): "where" and a due time. Nothing for other types.
function jobExtraInputs(t) {
  const o = (ME && ME.ticket_options) || {};
  const where = o.location_label ? h("input", { maxlength: 200, value: (t && t.job_location) || "", placeholder: "Address, shop bay or parking spot", "aria-label": o.location_label }) : null;
  const time = o.due_time ? h("input", { type: "time", value: (t && t.due_time) || "", "aria-label": "Due time" }) : null;
  return {
    fields: [where ? field(o.location_label, where) : null, time ? field("Due time", time) : null].filter(Boolean),
    values: (dueInput) => Object.assign({}, where ? { job_location: where.value } : {}, time ? { due_time: dueInput.value ? time.value : "" } : {}),
  };
}

function describeHistory(e) {
  const d = e.detail || {};
  switch (e.kind) {
    case "created": return jw("Ticket ") + (d.number || "") + " created (" + (d.status || "") + ")";
    case "status": return "Status: " + d.from + " → " + d.to;
    case "assigned": return "Assigned to " + (d.name || "");
    case "unassigned": return "Unassigned " + (d.name || "");
    case "priority": return "Priority: " + d.from + " → " + d.to;
    case "due_date": return "Due date: " + (d.from || "none") + " → " + (d.to || "none");
    case "equipment": return "Equipment changed (" + (d.added || []).length + " added, " + (d.removed || []).length + " removed)";
    case "edited": return "Edited: " + (d.changed || []).join(", ");
    case "time_in": return "Clocked in";
    case "time_out": return "Clocked out (" + fmtHM(d.seconds || 0) + ")";
    case "time_added": return "Added " + fmtHM(d.seconds || 0) + " of time";
    case "time_removed": return "Removed " + fmtHM(d.seconds || 0) + " of time";
    case "touch_base": return d.action === "scheduled" ? "Scheduled a customer touch base for " + d.due_date
      : d.action === "rescheduled" ? "Rescheduled the touch base to " + d.due_date
      : d.action === "cancelled" ? "Cancelled the touch base" : jw("Touch base follow-up ticket #") + (d.number || "") + " created";
    case "note": return "Added a note" + (d.files ? " with " + d.files + (d.files === 1 ? " file" : " files") : "");
    default: return e.kind;
  }
}

// "Assign to…" popup: current staff on the ticket with a remove button, and a dropdown of technicians to add.
async function assignDialog(t, onDone) {
  let staff;
  try { staff = (await api("GET", "/api/staff")).staff; } catch (e) { alert(e.message); return; }
  let ids = t.assignees.map(a => a.id);
  const nameOf = (id) => (staff.find(s => s.id === id) || t.assignees.find(a => a.id === id) || { name: "Unknown" }).name;
  const list = h("div", { class: "assigned-list" });
  const pick = h("select", { "aria-label": "Technician" });
  const err = h("div", { class: "error", role: "alert" });
  const save = async (next) => {
    err.textContent = "";
    try { await api("PUT", "/api/tickets/" + encodeURIComponent(t.id) + "/assignees", { user_ids: next }); ids = next; draw(); }
    catch (e) { err.textContent = e.message; }
  };
  function draw() {
    list.replaceChildren(...(ids.length ? ids.map(id => h("span", { class: "file-chip" }, nameOf(id) + " ",
      h("button", { type: "button", "aria-label": "Remove " + nameOf(id), onclick: () => save(ids.filter(x => x !== id)) }, "×")))
      : [h("span", { class: "muted" }, "Nobody assigned")]));
    pick.replaceChildren(h("option", { value: "" }, "Choose a technician…"),
      ...staff.filter(s => !ids.includes(s.id)).map(s => h("option", { value: s.id }, s.name + " (" + s.role_label + ")")));
  }
  draw();
  const dlg = tbModal(h("h2", null, "Assign to"), h("p", { class: "muted small" }, "#" + t.number + " · " + t.title),
    list, h("label", null, "Add a technician"), pick,
    h("div", { class: "form-actions" }, h("button", { type: "button", class: "primary", onclick: () => { if (pick.value) save([...ids, pick.value]); } }, "Add"),
      " ", h("button", { type: "button", autofocus: true, onclick: () => dlg.close() }, "Done")), err);
  dlg.addEventListener("close", onDone);
}

async function ticketDetail(id, inPanel) {
  const eid = encodeURIComponent(id);
  const [t, st] = await Promise.all([api("GET", "/api/tickets/" + eid), api("GET", "/api/statuses")]);
  const msg = h("div", { class: "error", role: "alert" });
  const nodes = inPanel ? [] : [h("p", null, h("a", { href: "#/tickets" }, jw("← All tickets")))];
  const run = (fn) => async () => { msg.textContent = ""; try { await fn(); route(); } catch (err) { msg.textContent = err.message; } };

  // header + status
  const options = st.statuses.map(s => h("option", { value: s.id, selected: s.id === t.status_id }, s.name));
  if (!st.statuses.some(s => s.id === t.status_id)) options.unshift(h("option", { value: t.status_id, selected: true, disabled: true }, t.status_name));
  const statusSel = h("select", { onchange: (e) => run(async () => maybeOfferTouchBase(await api("POST", "/api/tickets/" + eid + "/status", { status_id: e.target.value })))() }, options);
  if (!can("tickets.write")) statusSel.setAttribute("disabled", "");
  const fact = (label, value, wide) => h("div", { class: "fact" + (wide ? " wide" : "") },
    h("span", { class: "fact-label muted" }, label), h("div", { class: "fact-value" }, value));
  const assignedRow = h("span", null, t.assignees.length ? t.assignees.map(a => a.name).join(", ") : "Nobody assigned",
    can("tickets.assign") ? [" ", h("button", { type: "button", class: "mini", onclick: () => assignDialog(t, route) }, "Assign to…")] : null);
  const head = h("section", { class: "card" },
    h("h2", null, "#" + t.number + " · " + t.title),
    h("div", { class: "ticket-facts" },
      fact("Customer", h("span", null, h("a", { href: "#/customers/" + encodeURIComponent(t.customer_id) }, t.customer_name),
        t.customer_phone ? " · " + t.customer_phone : "", t.customer_email ? " · " + t.customer_email : "")),
      fact("Status", statusSel),
      fact("Priority", t.priority),
      fact("Due date", t.due_date ? t.due_date + (t.due_time ? " at " + fmtTime(t.due_time) : "") : "Not set"),
      ME.ticket_options && ME.ticket_options.location_label ? fact(ME.ticket_options.location_label, t.job_location || "Not set") : null,
      fact("Assigned to", assignedRow),
      fact(term("items", "Items"), t.equipment.length ? t.equipment.map(e => e.name + (e.serial_number ? " #" + e.serial_number : "")).join(", ") : "None"),
      t.closed_at ? fact("Closed", t.closed_at.replace("T", " ").replace("Z", "")) : null,
      fact("Customer-reported symptoms", t.symptoms || "None entered", true),
      fact("Intake notes", t.intake_notes || "None entered", true)),
    msg);
  nodes.push(head);
  const tbNote = await touchBaseNote(t);               // "Customer touch base scheduled for ..." with reschedule / cancel
  if (tbNote) nodes.push(tbNote);
  nodes.push(await ticketNotesCard(t));                // notes and attachments sit right under the details

  // edit (lives inside the details card)
  if (can("tickets.write")) {
    const title = h("input", { value: t.title, required: true, maxlength: 150 });
    const priority = h("select", null, ["low", "normal", "high", "urgent"].map(p => h("option", { value: p, selected: p === t.priority }, p)));
    const due = h("input", { type: "date", value: t.due_date || "" });
    const extra = jobExtraInputs(t);
    const symptoms = h("textarea", { rows: "2", maxlength: 2000 }, t.symptoms);
    const notes = h("textarea", { rows: "2", maxlength: 2000 }, t.intake_notes);
    let checks = [];
    const equipBox = h("div");
    if (can("equipment.read")) {
      const { equipment } = await api("GET", "/api/equipment?customer_id=" + encodeURIComponent(t.customer_id));
      const linked = new Set(t.equipment.map(e => e.id));
      checks = equipment.map(e => ({ id: e.id, el: h("input", { type: "checkbox", checked: linked.has(e.id) }) }));
      equipBox.append(h("label", null, term("items", "Items") + jw(" on this ticket")),
        ...equipment.map((e, i) => h("label", { class: "inline" }, checks[i].el, " " + e.name)));
    }
    head.append(h("details", { class: "ticket-edit" }, h("summary", null, jw("Edit ticket")),
      h("form", { onsubmit: (e) => { e.preventDefault(); run(() => {
          const body = { title: title.value, priority: priority.value, due_date: due.value, ...extra.values(due), symptoms: symptoms.value, intake_notes: notes.value };
          if (can("equipment.read")) body.equipment_ids = checks.filter(c => c.el.checked).map(c => c.id);
          return api("PUT", "/api/tickets/" + eid, body);
        })(); } },
        field("Title", title), field("Priority", priority), field("Due date", due), ...extra.fields,
        field("Customer-reported symptoms", symptoms), field("Intake notes", notes), equipBox,
        h("div", { class: "form-actions" }, h("button", { class: "primary", type: "submit" }, "Save changes")))));
  }

  // time tracking
  if (can("tickets.read")) nodes.push(await ticketTimeCard(t));

  // invoices for this ticket
  if (can("invoices.read")) {
    await biz();
    const { invoices } = await api("GET", "/api/invoices?ticket_id=" + eid + "&limit=50");
    nodes.push(h("section", { class: "card" }, h("h2", null, "Invoices"),
      invoices.length ? h("ul", { class: "history" }, invoices.map(i => h("li", null,
        h("a", { href: "#/invoices/" + encodeURIComponent(i.id) }, i.number || "Draft"),
        " · " + i.status + " · " + money(i.total_cents)))) : h("p", { class: "muted" }, "No invoices yet."),
      can("invoices.write") ? h("a", { class: "btn", href: "#/invoices/new?ticket=" + eid }, jw("+ Create invoice for this ticket")) : null));
  }

  // history
  nodes.push(h("section", { class: "card" }, h("h2", null, "History"),
    h("ul", { class: "history" }, [...t.history].reverse().map(e =>
      h("li", null, h("span", { class: "muted" }, e.ts.replace("T", " ").replace("Z", "") + " · " + (e.user_name || "system") + " — "), describeHistory(e))))));
  return nodes;
}

// ------------------------------------------------------------ settings (business profile + team)
async function settingsView() {
  const biz = await api("GET", "/api/business");
  const nodes = [];
  const msg = h("span", { class: "muted" });
  const f = {
    name: h("input", { value: biz.name, maxlength: 120, required: true }),
    phone: h("input", { value: biz.phone, maxlength: 40, type: "tel" }),
    email: h("input", { value: biz.email, maxlength: 254, type: "email" }),
    address: h("input", { value: biz.address, maxlength: 300 }),
    timezone: h("input", { value: biz.timezone, maxlength: 64 }),
    currency: h("input", { value: biz.currency, maxlength: 3 }),
  };
  const editable = can("business.manage");
  for (const input of Object.values(f)) if (!editable) input.setAttribute("disabled", "");
  const profile = h("form", { onsubmit: async (e) => {
      e.preventDefault(); msg.className = "muted"; msg.textContent = "Saving…";
      try {
        await api("PUT", "/api/business", Object.fromEntries(Object.entries(f).map(([k, v]) => [k, v.value])));
        msg.className = "ok"; msg.textContent = "Saved";
      } catch (err) { msg.className = "error"; msg.textContent = err.message; }
    } },
    field("Business name", f.name), field("Phone", f.phone), field("Email", f.email),
    field("Address", f.address), field("Timezone", f.timezone), field("Currency", f.currency),
    editable ? h("div", { class: "form-actions" }, h("button", { class: "primary", type: "submit" }, "Save"), msg) : null);
  nodes.push(h("section", { class: "card" }, h("h2", null, "Business profile"), profile));
  if (can("business.manage")) { nodes.push(brandingCard()); nodes.push(await statusesCard()); nodes.push(await numberingCard()); nodes.push(await invoiceSettingsCard()); }
  if (can("data.import")) nodes.push(h("section", { class: "card" }, h("h2", null, "Import data"),
    h("p", { class: "muted" }, "Bring in customers, inventory items and services from CSV files (for example exported from QuickBooks)."),
    h("a", { class: "btn", href: "#/import" }, "Open the importer")));
  if (can("users.read")) nodes.push(await teamCard(ME));
  return nodes;
}

async function statusesCard() {
  let rows = (await api("GET", "/api/statuses")).statuses.map(s => ({ id: s.id, name: s.name, customer_label: s.customer_label, is_closed: !!s.is_closed }));
  const card = h("section", { class: "card" });
  const msg = h("span", { class: "muted" });
  function draw() {
    const move = (i, d) => { const j = i + d; if (j < 0 || j >= rows.length) return; [rows[i], rows[j]] = [rows[j], rows[i]]; draw(); };
    card.replaceChildren(h("h2", null, jw("Ticket statuses")),
      h("p", { class: "muted" }, jw("Order here is the order of the workflow. The first status is used for new tickets. Removed statuses stay on old tickets.")),
      ...rows.map((r, i) => h("div", { class: "status-row" },
        h("input", { value: r.name, maxlength: 60, "aria-label": "Status name", oninput: (e) => { r.name = e.target.value; } }),
        h("input", { value: r.customer_label, maxlength: 80, placeholder: "Customer sees…", "aria-label": "Customer label", oninput: (e) => { r.customer_label = e.target.value; } }),
        h("label", { class: "inline" }, h("input", { type: "checkbox", checked: r.is_closed, onchange: (e) => { r.is_closed = e.target.checked; } }), jw(" Closes ticket")),
        h("button", { type: "button", onclick: () => move(i, -1), "aria-label": "Move up" }, "↑"),
        h("button", { type: "button", onclick: () => move(i, 1), "aria-label": "Move down" }, "↓"),
        h("button", { type: "button", onclick: () => { rows.splice(i, 1); draw(); } }, "Remove"))),
      h("div", { class: "form-actions" },
        h("button", { type: "button", onclick: () => { rows.push({ id: null, name: "", customer_label: "", is_closed: false }); draw(); } }, "Add status"),
        h("button", { type: "button", class: "primary", onclick: async () => {
            msg.className = "muted"; msg.textContent = "Saving…";
            try {
              const res = await api("PUT", "/api/statuses", { statuses: rows.map(r => ({ ...(r.id ? { id: r.id } : {}), name: r.name, customer_label: r.customer_label, is_closed: r.is_closed })) });
              rows = res.statuses.map(s => ({ id: s.id, name: s.name, customer_label: s.customer_label, is_closed: !!s.is_closed }));
              draw(); msg.className = "ok"; msg.textContent = "Saved";
            } catch (err) { msg.className = "error"; msg.textContent = err.message; }
          } }, "Save statuses"), msg));
  }
  draw();
  return card;
}

async function numberingCard() {
  const cur = await api("GET", "/api/tickets/numbering");
  const prefix = h("input", { value: cur.prefix, maxlength: 10 });
  const next = h("input", { type: "number", min: "1", value: String(cur.next_number) });
  const msg = h("span", { class: "muted" });
  return h("section", { class: "card" }, h("h2", null, jw("Ticket numbering")),
    h("form", { onsubmit: async (e) => {
        e.preventDefault(); msg.className = "muted"; msg.textContent = "Saving…";
        try {
          const r = await api("PUT", "/api/tickets/numbering", { prefix: prefix.value, next_number: parseInt(next.value, 10) });
          next.value = String(r.next_number); msg.className = "ok"; msg.textContent = "Saved";
        } catch (err) { msg.className = "error"; msg.textContent = err.message; }
      } },
      field(eg("prefix", "Prefix (optional, e.g. RS-)"), prefix), field(jw("Next ticket number"), next),
      h("div", { class: "form-actions" }, h("button", { class: "primary", type: "submit" }, "Save"), msg)));
}

async function teamCard(me) {
  const manage = can("users.manage");
  const card = h("section", { class: "card" }, h("h2", null, "Team"));
  const msg = h("div", { class: "error", role: "alert" });

  async function act(fn) {
    msg.className = "error"; msg.textContent = "";
    try { await fn(); await load(); } catch (err) { msg.textContent = err.message; }
  }

  async function load() {
    const { users } = await api("GET", "/api/users");
    const roles = manage ? (await api("GET", "/api/roles")).roles : [];
    const kids = [h("h2", null, "Team")];

    const rows = users.map(u => {
      const editable = manage && u.role !== "owner" && u.id !== me.user.id;
      const roleCell = editable
        ? h("select", { onchange: (e) => act(() => api("PUT", "/api/users/" + u.id, { role: e.target.value })) },
            roles.map(r => h("option", { value: r.name, selected: r.name === u.role }, r.label)))
        : u.role_label;
      const statusCell = editable
        ? h("button", { onclick: () => act(() => api("PUT", "/api/users/" + u.id, { active: !u.active })) },
            u.active ? "Deactivate" : "Activate")
        : (u.active ? "Active" : "Inactive");
      return h("tr", null, h("td", null, u.name), h("td", null, u.email), h("td", null, roleCell), h("td", null, statusCell));
    });
    kids.push(h("div", { class: "table-wrap" }, h("table", null,
      h("thead", null, h("tr", null, ["Name", "Email", "Role", "Status"].map(t => h("th", null, t)))),
      h("tbody", null, rows))));

    if (manage) {
      const linkBox = h("div");
      const pendingBox = h("div");
      const email = h("input", { type: "email", required: true, maxlength: 254, autocomplete: "off" });
      const role = h("select", null, roles.map(r => h("option", { value: r.name }, r.label)));
      async function refreshInvites() {
        const list = (await api("GET", "/api/invites")).invites;
        pendingBox.replaceChildren(list.length ? h("div", null, h("h3", null, "Pending invites"),
          h("div", { class: "table-wrap" }, h("table", null,
            h("tbody", null, list.map(i => h("tr", null, h("td", null, i.email), h("td", null, i.role_label),
              h("td", null, h("button", { onclick: () => act(() => api("DELETE", "/api/invites/" + i.id)) }, "Revoke")))))))) : "");
      }
      kids.push(h("h3", null, "Invite someone"),
        h("form", { onsubmit: async (e) => {
            e.preventDefault();
            msg.textContent = "";
            try {
              const res = await api("POST", "/api/invites", { email: email.value, role: role.value });
              const box = h("input", { value: location.origin + "/#/invite/" + res.token, readonly: true });
              box.addEventListener("focus", () => box.select());
              linkBox.replaceChildren(
                h("p", { class: "ok" }, "Invite created for " + res.invite.email + ". Send them this link. It is shown only once and expires in 7 days."),
                box);
              email.value = "";
              await refreshInvites();
            } catch (err) { msg.textContent = err.message; }
          } },
          field("Email", email), field("Role", role),
          h("div", { class: "form-actions" }, h("button", { class: "primary", type: "submit" }, "Create invite link"))),
        linkBox, pendingBox);
      await refreshInvites();
    }
    kids.push(msg);
    card.replaceChildren(...kids);
  }

  await load();
  return card;
}

// Shown after signing in with an administrator-issued temporary password: nothing else works until it is replaced.
function passwordScreen() {
  const err = h("div", { class: "error", role: "alert" });
  const cur = h("input", { type: "password", required: true, autocomplete: "current-password", maxlength: 200 });
  const p1 = h("input", { type: "password", required: true, minlength: 10, autocomplete: "new-password", maxlength: 200 });
  const p2 = h("input", { type: "password", required: true, minlength: 10, autocomplete: "new-password", maxlength: 200 });
  render(h("div", { class: "auth" }, h("h1", null, "Choose a new password"),
    h("div", { class: "card" }, h("p", { class: "muted" }, "You signed in with a temporary password. Choose your own (10+ characters) to continue."),
      h("form", { onsubmit: async (e) => {
          e.preventDefault(); err.textContent = "";
          if (p1.value !== p2.value) { err.textContent = "The two passwords do not match."; return; }
          try { await api("POST", "/api/account/password", { current_password: cur.value, new_password: p1.value }); await start(); }
          catch (ex) { err.textContent = ex.message; }
        } },
        field("Temporary password", cur), field("New password", p1), field("Repeat new password", p2),
        h("div", { class: "form-actions" }, h("button", { class: "primary", type: "submit" }, "Save new password"),
          h("button", { type: "button", onclick: async () => { try { await api("POST", "/api/logout", {}); } finally { csrf = ""; start(); } } }, "Sign out")), err))));
}

// ------------------------------------------------------------ boot
async function start() {
  if (location.hash.startsWith("#/invite/")) return inviteScreen(location.hash.slice("#/invite/".length));
  try {
    const me = await api("GET", "/api/me");
    csrf = me.csrf;
    if (me.user.must_change_password) return passwordScreen();
    await dashboard(me);
  } catch (err) {
    csrf = ""; ME = null; window.onhashchange = null; document.body.classList.remove("mobile-first"); setBrand(null); document.title = "ShopDesk";
    if (err.status === 401) {
      try { authScreen(await api("GET", "/api/bootstrap")); }
      catch (e2) { render(h("p", { class: "center error" }, e2.message)); }
    } else {
      render(h("p", { class: "center error" }, err.message));
    }
  }
}

start();
