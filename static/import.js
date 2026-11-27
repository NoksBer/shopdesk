"use strict";
// Settings > Import data: customers, inventory items and services from CSV files.
// The file is read and parsed here in the browser; rows are sent to the server in small batches.

function parseCsv(text) {
  if (text.charCodeAt(0) === 0xFEFF) text = text.slice(1);                // tolerate a byte-order mark
  const first = text.split(/\r\n|\n|\r/, 1)[0] || "";
  const sep = (first.match(/;/g) || []).length > (first.match(/,/g) || []).length ? ";" : ",";
  const rows = [];
  let row = [], cell = "", quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"') { if (text[i + 1] === '"') { cell += '"'; i++; } else quoted = false; } else cell += c;
    } else if (c === '"') quoted = true;
    else if (c === sep) { row.push(cell); cell = ""; }
    else if (c === "\n" || c === "\r") { if (c === "\r" && text[i + 1] === "\n") i++; row.push(cell); cell = ""; rows.push(row); row = []; }
    else cell += c;
  }
  if (cell !== "" || row.length) { row.push(cell); rows.push(row); }
  return rows.filter(r => r.some(x => x.trim() !== ""));
}

function decodeCsv(buffer) {                                              // UTF-8 first, Windows-1252 as the fallback
  try { return new TextDecoder("utf-8", { fatal: true }).decode(buffer); }
  catch (e) { return new TextDecoder("windows-1252").decode(buffer); }
}

const csvEscape = (v) => /[",\r\n]/.test(v) ? '"' + String(v).replace(/"/g, '""') + '"' : String(v);
const normHeader = (s) => String(s).toLowerCase().replace(/[^a-z0-9]/g, "");

function autoMap(fields, headers) {                                        // field key -> column index (or -1)
  const norm = headers.map(normHeader), used = new Set(), map = {};
  for (const f of fields) {
    const names = [normHeader(f.key), normHeader(f.label), ...f.aliases.map(normHeader)];
    let idx = -1;
    for (const n of names) { const i = norm.findIndex((x, k) => x === n && !used.has(k)); if (i >= 0) { idx = i; break; } }
    map[f.key] = idx;
    if (idx >= 0) used.add(idx);
  }
  return map;
}

async function importView() {
  const [{ types, max_batch_rows, max_preview_rows, max_total_rows }, hist] =
    await Promise.all([api("GET", "/api/import/types"), api("GET", "/api/import/history")]);
  const msg = h("div", { class: "error", role: "alert" });
  const stage = h("div");
  const state = { kind: types[0].key, mode: "skip", headers: [], rows: [], map: {}, filename: "" };

  const typeSel = h("select", { "aria-label": "What to import" }, types.map(t => h("option", { value: t.key }, t.label)));
  const modeSel = h("select", { "aria-label": "Duplicates" },
    h("option", { value: "skip" }, "Skip rows that already exist (recommended)"),
    h("option", { value: "update" }, "Update the existing record when a row matches"));
  const fileIn = h("input", { type: "file", accept: ".csv,text/csv", "aria-label": "CSV file" });
  const typeInfo = h("p", { class: "muted" });
  const sampleLink = h("a", { class: "btn", href: "#", download: "" }, "Download sample CSV");
  const cur = () => types.find(t => t.key === state.kind);

  function syncType() {
    state.kind = typeSel.value;
    typeInfo.textContent = cur().note;
    sampleLink.setAttribute("href", "/api/import/sample/" + state.kind);
    sampleLink.setAttribute("download", "shopdesk-" + state.kind + "-sample.csv");
  }
  typeSel.addEventListener("change", () => { syncType(); if (state.rows.length) drawMapping(); });
  modeSel.addEventListener("change", () => { state.mode = modeSel.value; if (state.rows.length) drawMapping(); });
  fileIn.addEventListener("change", async () => {
    msg.textContent = ""; stage.replaceChildren();
    const file = fileIn.files[0];
    if (!file) return;
    if (file.size > 10 * 1024 * 1024) { msg.textContent = "That file is larger than 10 MB."; return; }
    const all = parseCsv(decodeCsv(await file.arrayBuffer()));
    if (all.length < 2) { msg.textContent = "The file needs a header row and at least one data row."; return; }
    if (all.length - 1 > max_total_rows) { msg.textContent = "Import at most " + max_total_rows + " rows at a time."; return; }
    state.filename = file.name; state.headers = all[0].map(x => x.trim()); state.rows = all.slice(1);
    drawMapping();
  });

  const valuesOf = (row) => { const v = {}; for (const f of cur().fields) { const i = state.map[f.key]; v[f.key] = i >= 0 ? (row[i] ?? "") : ""; } return v; };
  const pack = (row, n) => ({ line: n + 2, values: valuesOf(state.rows[n]) });

  function drawMapping() {
    state.map = autoMap(cur().fields, state.headers);
    const selects = {};
    const rowsEls = cur().fields.map(f => {
      const sel = h("select", { "aria-label": "Column for " + f.label }, h("option", { value: "-1" }, "(not in my file)"),
        state.headers.map((hd, i) => h("option", { value: String(i), selected: state.map[f.key] === i }, hd || "(column " + (i + 1) + ")")));
      sel.addEventListener("change", () => { state.map[f.key] = parseInt(sel.value, 10); });
      selects[f.key] = sel;
      return h("tr", null, h("td", null, f.label + (f.required ? " *" : "")), h("td", null, sel));
    });
    stage.replaceChildren(h("section", { class: "card" }, h("h2", null, "2. Match your columns"),
      h("p", { class: "muted" }, state.rows.length + " rows found in " + state.filename + ". Columns that look familiar were matched for you; check them and change any that are wrong."),
      h("div", { class: "table-wrap" }, h("table", null, h("thead", null, h("tr", null, h("th", null, "ShopDesk field"), h("th", null, "Column in your file"))), h("tbody", null, rowsEls))),
      h("div", { class: "form-actions" }, h("button", { class: "primary", type: "button", onclick: () => preview().catch(e => { msg.textContent = e.message; }) }, "Preview first " + Math.min(max_preview_rows, 20) + " rows"))));
  }

  async function preview() {
    msg.textContent = "";
    const req = cur().fields.find(f => f.required);
    if (state.map[req.key] < 0) { msg.textContent = "Match the " + req.label + " column first."; return; }
    const n = Math.min(20, state.rows.length);
    const r = await api("POST", "/api/import/" + state.kind + "/preview", { mode: state.mode, rows: state.rows.slice(0, n).map((row, i) => pack(row, i)) });
    const fields = cur().fields.filter(f => state.map[f.key] >= 0);
    const verb = state.mode === "skip" ? "rows that already exist are skipped" : "rows that already exist are updated (empty cells never erase existing data)";
    stage.replaceChildren(h("section", { class: "card" }, h("h2", null, "3. Preview and import"),
      h("p", null, h("strong", null, "What will happen: "), "all " + state.rows.length + " rows are checked; new records are created, " + verb + ", and rows with problems are reported and left out. Nothing is deleted. Invoices, payments, tickets, ledger entries and stock quantities are never imported."),
      h("p", { class: "muted" }, "The first " + n + " rows: " + Object.entries(r.summary).map(([k, v]) => v + " " + k).join(", ")),
      h("div", { class: "table-wrap" }, h("table", null,
        h("thead", null, h("tr", null, h("th", null, "Row"), h("th", null, "Result"), ...fields.map(f => h("th", null, f.label)))),
        h("tbody", null, r.results.map((res, i) => h("tr", { class: res.status === "error" ? "row-error" : "" },
          h("td", null, String(res.line)), h("td", null, res.status + (res.message ? " – " + res.message : "")),
          ...fields.map(f => h("td", null, valuesOf(state.rows[i])[f.key]))))))),
      h("div", { class: "form-actions" },
        h("button", { class: "primary", type: "button", onclick: () => { if (confirm("Import " + state.rows.length + " rows into " + cur().label.toLowerCase() + " now?")) run().catch(e => { msg.textContent = e.message; }); } }, "Import all " + state.rows.length + " rows"),
        h("button", { type: "button", onclick: drawMapping }, "Back to column matching"))));
  }

  async function run() {
    msg.textContent = "";
    const progress = h("p", null, "Starting…");
    stage.replaceChildren(h("section", { class: "card" }, h("h2", null, "Importing…"), progress, h("p", { class: "muted" }, "Keep this page open until it finishes.")));
    const kind = state.kind;
    const { import_id } = await api("POST", "/api/import/" + kind + "/start", { filename: state.filename, mode: state.mode, total: state.rows.length });
    const results = [];
    for (let at = 0; at < state.rows.length; at += Math.min(100, max_batch_rows)) {
      const slice = state.rows.slice(at, at + Math.min(100, max_batch_rows));
      const r = await api("POST", "/api/import/" + kind + "/batch", { import_id, rows: slice.map((row, i) => ({ line: at + i + 2, values: valuesOf(row) })) });
      results.push(...r.results);
      progress.textContent = results.length + " of " + state.rows.length + " rows done";
    }
    await api("POST", "/api/import/" + kind + "/finish", { import_id });
    report(results);
  }

  function report(results) {
    const count = (s) => results.filter(r => r.status === s).length;
    const bad = results.filter(r => r.status === "error");
    const parts = [h("h2", null, "Import finished"),
      h("p", null, h("strong", null, count("created") + " created, " + count("updated") + " updated, " + count("skipped") + " skipped, " + bad.length + " with errors."))];
    if (bad.length) {
      parts.push(h("div", { class: "table-wrap" }, h("table", null, h("thead", null, h("tr", null, h("th", null, "Row"), h("th", null, "Problem"))),
        h("tbody", null, bad.slice(0, 200).map(r => h("tr", null, h("td", null, String(r.line)), h("td", null, r.message)))))),
        bad.length > 200 ? h("p", { class: "muted" }, "Showing the first 200 problems; the download has all of them.") : null,
        h("div", { class: "form-actions" }, h("button", { type: "button", onclick: () => {
          const lines = [[...state.headers, "Row", "Problem"].map(csvEscape).join(",")];
          for (const r of bad) lines.push([...state.rows[r.line - 2], String(r.line), r.message].map(csvEscape).join(","));
          const url = URL.createObjectURL(new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv" }));
          const a = h("a", { href: url, download: "import-failed-rows.csv" }); document.body.append(a); a.click(); a.remove(); URL.revokeObjectURL(url);
        } }, "Download failed rows (CSV)")));
    }
    parts.push(h("div", { class: "form-actions" }, h("a", { class: "btn", href: "#/customers" }, "View customers"), h("button", { type: "button", onclick: () => route() }, "Import another file")));
    stage.replaceChildren(h("section", { class: "card" }, parts));
  }

  syncType();
  const histCard = h("section", { class: "card" }, h("h2", null, "Import history"),
    hist.imports.length ? h("div", { class: "table-wrap" }, h("table", null,
      h("thead", null, h("tr", null, ["When", "Who", "What", "File", "Created", "Updated", "Skipped", "Errors", "State"].map(t => h("th", null, t)))),
      h("tbody", null, hist.imports.map(i => h("tr", null, h("td", null, (i.created_at || "").replace("T", " ").replace("Z", "")), h("td", null, i.user_name || ""),
        h("td", null, i.kind), h("td", null, i.filename), h("td", null, String(i.created)), h("td", null, String(i.updated)), h("td", null, String(i.skipped)),
        h("td", null, String(i.failed)), h("td", null, i.status === "done" ? "done" : "not finished")))))) : h("p", { class: "muted" }, "No imports yet."));
  return [h("p", null, h("a", { href: "#/settings" }, "← Settings")),
    h("section", { class: "card" }, h("h2", null, "Import data"),
      h("p", { class: "muted" }, "Bring in customers, inventory items and services from a CSV file (for example exported from QuickBooks). Nothing connects to QuickBooks or any online service."),
      h("p", null, h("strong", null, "1. Choose what to import and a CSV file")),
      field("What to import", typeSel), typeInfo, field("If a row matches an existing record", modeSel),
      h("div", { class: "form-actions" }, sampleLink), field("CSV file (UTF-8 or Windows-1252)", fileIn), msg),
    stage, histCard];
}
