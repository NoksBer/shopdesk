"use strict";
// Shared helpers for the board, calendar and billing screens. Loaded before app.js; uses h(), api(), can() from it.

let BIZ = null;                       // cached business profile (currency, name, address)
async function biz() {
  if (!BIZ) BIZ = await api("GET", "/api/business");
  return BIZ;
}

function money(cents) {
  const cur = (BIZ && BIZ.currency) || "USD";
  try { return new Intl.NumberFormat(undefined, { style: "currency", currency: cur }).format(cents / 100); }
  catch (e) { return (cents / 100).toFixed(2) + " " + cur; }
}

// "12.5" / "$1,200" -> cents, or null when it is not a valid non-negative amount with at most 2 decimals.
function parseMoney(text) {
  const s = String(text).trim().replace(/^\$/, "").replace(/,/g, "");
  if (!/^\d+(\.\d{1,2})?$/.test(s)) return null;
  const [d, f = ""] = s.split(".");
  return parseInt(d, 10) * 100 + parseInt((f + "00").slice(0, 2), 10);
}
const centsToInput = (c) => (c / 100).toFixed(2);

// Quantity text -> hundredths (integer), or null. Allows up to 2 decimals.
function parseQty(text) {
  const s = String(text).trim();
  if (!/^\d+(\.\d{1,2})?$/.test(s)) return null;
  const [d, f = ""] = s.split(".");
  const q = parseInt(d, 10) * 100 + parseInt((f + "00").slice(0, 2), 10);
  return q >= 1 && q <= 10000000 ? q : null;
}

const enc = encodeURIComponent;
const ymd = (d) => d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
const localToday = () => ymd(new Date());

// Type-ahead customer chooser. Returns { el, value() }. Shows matches as you type (and a starter list at once).
function customerPicker({ initial = null, onChange = () => {} } = {}) {
  let selected = initial, timer;
  const input = h("input", { type: "search", placeholder: "Type a name, email or phone number", maxlength: 100, autocomplete: "off" });
  const results = h("div", { class: "picker-results" });
  const chosen = h("div", { class: "picker-chosen" });
  const err = h("div", { class: "error", role: "alert" });
  const el = h("div", { class: "picker" }, chosen, input, results, err);

  function pick(c) { selected = c; draw(); onChange(c); }
  function draw() {
    if (selected) {
      chosen.replaceChildren(h("span", null, h("strong", null, selected.name),
        selected.phone ? " · " + selected.phone : "", selected.email ? " · " + selected.email : ""), " ",
        h("button", { type: "button", onclick: () => { selected = null; draw(); onChange(null); search(); } }, "Change"));
      input.style.display = "none"; results.style.display = "none";
    } else {
      chosen.replaceChildren(); input.style.display = ""; results.style.display = "";
    }
  }
  async function search() {
    try {
      const r = await api("GET", "/api/customers?limit=8&q=" + enc(input.value.trim()));
      err.textContent = "";
      results.replaceChildren(...(r.customers.length
        ? r.customers.map(c => h("button", { type: "button", class: "picker-item", onclick: () => pick(c) },
            h("strong", null, c.name), (c.phone ? " · " + c.phone : "") + (c.email ? " · " + c.email : "")))
        : [h("p", { class: "muted" }, input.value.trim() ? "No customers match. " : "No customers yet. ",
            h("a", { href: "#/customers" }, "Add one on the Customers page"), ".")]));
    } catch (e) { err.textContent = e.message; }
  }
  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(search, 200); });
  input.addEventListener("keydown", (e) => {            // Enter picks the first match instead of submitting the form
    if (e.key === "Enter") { e.preventDefault(); const first = results.querySelector("button"); if (first) first.click(); }
  });
  draw();
  if (!selected) search();
  return { el, value: () => selected };
}

// ------------------------------------------------------------ white labelling
let BRAND = null;                       // saved branding: { primary_color, header_color, tagline, show_name, has_logo, logo_version }

function contrastOn(hex) {              // readable text colour (dark or white) for a given background
  const n = parseInt(hex.slice(1), 16);
  const lin = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
  const lum = 0.2126 * lin((n >> 16) & 255) + 0.7152 * lin((n >> 8) & 255) + 0.0722 * lin(n & 255);
  return lum > 0.4 ? "#0b1520" : "#ffffff";
}
function paintBrand(b) {                // CSS only (so settings can preview unsaved colours)
  const st = document.documentElement.style;
  const set = (name, value) => { if (value) st.setProperty(name, value); else st.removeProperty(name); };
  const accent = (b && b.primary_color) || "", head = (b && b.header_color) || "";
  set("--accent", accent); set("--on-accent", accent ? contrastOn(accent) : "");
  set("--header-bg", head); set("--header-fg", head ? contrastOn(head) : "");
}
function setBrand(b) { BRAND = b; paintBrand(b); }
const logoUrl = () => (BRAND && BRAND.has_logo ? "/api/branding/logo?v=" + enc(BRAND.logo_version) : null);

function brandMark() {                  // logo and/or business name for the top bar
  const url = logoUrl();
  const showName = !url || !BRAND || BRAND.show_name;
  return h("span", { class: "brandmark" }, url ? h("img", { class: "brand-logo", src: url, alt: "" }) : null,
    showName ? h("span", null, ME.business.name) : null);
}
function refreshBrandHeader() {
  const el = document.getElementById("brand");
  if (el) el.replaceChildren(brandMark());
}

function badge(text, cls) { return h("span", { class: "badge " + (cls || "") }, text); }
