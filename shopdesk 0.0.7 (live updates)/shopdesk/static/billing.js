"use strict";
// Services, inventory and invoices. Amounts are cents on the wire and in state; text boxes hold dollars.

function parsePct(text) {                        // "7.5" -> 750 (basis points), or null
  const s = String(text).trim().replace(/%$/, "");
  if (!/^\d+(\.\d{1,2})?$/.test(s)) return null;
  const [d, f = ""] = s.split(".");
  const bp = parseInt(d, 10) * 100 + parseInt((f + "00").slice(0, 2), 10);
  return bp <= 10000 ? bp : null;
}
const lineCents = (qtyX100, unit) => Math.floor((qtyX100 * unit + 50) / 100);   // same rounding as the server

function checkRow(label, input) { return h("label", { class: "inline" }, input, " " + label); }

// ------------------------------------------------------------ dashboard revenue
function revenueCard(rev) {
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const label = (key) => MON[parseInt(key.slice(5), 10) - 1] + " " + key.slice(2, 4);
  const stat = (title, cents, note, href, warn) => h(href ? "a" : "div", { class: "tile" + (warn && cents ? " warn" : ""), href: href || null },
    h("span", { class: "big" }, money(cents)), h("span", null, title), note ? h("span", { class: "muted small" }, note) : null);
  const tm = rev.this_month, lm = rev.last_month;
  const change = lm.total_cents ? Math.round((tm.total_cents - lm.total_cents) * 100 / lm.total_cents) : null;
  const peak = Math.max(1, ...rev.months.map(m => m.total_cents));
  const bars = rev.months.map((m, i) => {
    const fill = h("div", { class: "bar-fill" + (i === rev.months.length - 1 ? " current" : "") });
    fill.style.height = (m.total_cents ? Math.max(3, Math.round(m.total_cents * 100 / peak)) : 0) + "%";
    return h("div", { class: "bar", title: label(m.month) + ": " + money(m.total_cents) + " from " + m.count + " invoice" + (m.count === 1 ? "" : "s"),
      role: "img", "aria-label": label(m.month) + " " + money(m.total_cents) },
      h("div", { class: "bar-track" }, fill), h("div", { class: "bar-label" }, label(m.month).slice(0, 3)));
  });
  return h("section", { class: "card" }, h("h2", null, "Revenue"),
    h("p", { class: "muted small" }, "Paid invoices only, by the month they were paid. Totals include sales tax."),
    h("div", { class: "tiles" },
      stat("This month", tm.total_cents, tm.count + " paid" + (tm.tax_cents ? " · " + money(tm.tax_cents) + " tax" : "") +
        (change === null ? "" : " · " + (change >= 0 ? "+" : "") + change + "% vs last month")),
      stat("Last month", lm.total_cents, lm.count + " paid"),
      stat("Year to date", rev.ytd_cents, rev.ytd_count + " paid"),
      stat("Outstanding", rev.outstanding_cents, rev.outstanding_count + " unpaid" + (rev.overdue_cents ? " · " + money(rev.overdue_cents) + " overdue" : ""), "#/invoices", false)),
    h("div", { class: "bars" }, bars),
    rev.recent.length ? h("div", null, h("h3", null, "Latest payments"), h("ul", { class: "history" }, rev.recent.map(p => h("li", null,
      h("strong", null, money(p.total_cents)), " · ", h("a", { href: "#/invoices/" + enc(p.id) }, p.number || "invoice"), " · " + p.customer_name,
      h("span", { class: "muted" }, " · " + (p.paid_at || "").slice(0, 10) + (p.payment_method ? " · " + p.payment_method : "")))))) : null);
}

// ------------------------------------------------------------ services
async function servicesView() {
  await biz();
  const canEdit = can("services.write");
  const msg = h("div", { class: "error", role: "alert" });
  const list = h("div");
  const search = h("input", { type: "search", placeholder: "Search services", maxlength: 100 });
  const inactive = h("input", { type: "checkbox" });
  let editing = null;

  const name = h("input", { required: true, maxlength: 120, placeholder: "e.g. Screen replacement" });
  const desc = h("input", { maxlength: 300, placeholder: "Short description (optional)" });
  const price = h("input", { required: true, inputmode: "decimal", placeholder: "0.00" });
  const taxable = h("input", { type: "checkbox", checked: true });
  const formTitle = h("h2", null, "Add a service");
  const cancel = h("button", { type: "button", onclick: () => reset() }, "Cancel");
  const formMsg = h("span", { class: "error", role: "alert" });

  function reset() {
    editing = null; name.value = desc.value = price.value = ""; taxable.checked = true;
    formTitle.textContent = "Add a service"; cancel.style.display = "none"; formMsg.textContent = "";
  }
  function edit(s) {
    editing = s; name.value = s.name; desc.value = s.description; price.value = centsToInput(s.price_cents);
    taxable.checked = !!s.taxable; formTitle.textContent = "Edit service"; cancel.style.display = ""; name.focus();
    name.scrollIntoView({ block: "center" });
  }

  async function load() {
    const p = new URLSearchParams({ q: search.value.trim() });
    if (inactive.checked) p.set("include_inactive", "1");
    const { services } = await api("GET", "/api/services?" + p.toString());
    list.replaceChildren(services.length ? h("div", { class: "table-wrap" }, h("table", null,
      h("thead", null, h("tr", null, ["Service", "Price", "Tax", canEdit ? "" : null].map(t => t === null ? null : h("th", null, t)))),
      h("tbody", null, services.map(s => h("tr", { class: s.active ? "" : "muted" },
        h("td", null, h("strong", null, s.name), s.active ? "" : " (inactive)", s.description ? h("div", { class: "muted small" }, s.description) : null),
        h("td", null, money(s.price_cents)), h("td", null, s.taxable ? "Yes" : "No"),
        canEdit ? h("td", { class: "actions" },
          h("button", { type: "button", onclick: () => edit(s) }, "Edit"),
          h("button", { type: "button", onclick: async () => {
              try { await api("PUT", "/api/services/" + enc(s.id), { name: s.name, description: s.description, price_cents: s.price_cents, taxable: !!s.taxable, active: !s.active }); await load(); }
              catch (e) { msg.textContent = e.message; } } }, s.active ? "Deactivate" : "Reactivate")) : null)))))
      : h("p", { class: "muted" }, "No services yet."));
  }
  const reload = () => load().catch(showError(msg));
  let timer;
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(reload, 250); });
  inactive.addEventListener("change", reload);

  cancel.style.display = "none";
  const nodes = [];
  if (canEdit) {
    nodes.push(h("section", { class: "card" }, formTitle,
      h("form", { onsubmit: async (e) => {
          e.preventDefault(); formMsg.textContent = "";
          const cents = parseMoney(price.value);
          if (cents === null) { formMsg.textContent = "Enter a price such as 49.99"; return; }
          const body = { name: name.value, description: desc.value, price_cents: cents, taxable: taxable.checked };
          try {
            if (editing) await api("PUT", "/api/services/" + enc(editing.id), { ...body, active: !!editing.active });
            else await api("POST", "/api/services", body);
            reset(); await load();
          } catch (err) { formMsg.textContent = err.message; }
        } },
        field("Name", name), field("Description", desc), field("Price", price), checkRow("Taxable", taxable),
        h("div", { class: "form-actions" }, h("button", { class: "primary", type: "submit" }, "Save service"), cancel, formMsg))));
  }
  await load();
  nodes.push(h("section", { class: "card" }, h("h2", null, "Services and pricing"),
    h("p", { class: "muted small" }, "Services can be added to invoices in one click. Changing a price here does not change invoices already created."),
    search, h("div", { class: "filters" }, checkRow("Show inactive", inactive)), msg, list));
  return nodes;
}

// ------------------------------------------------------------ inventory
// Code 39 barcode (digits) so a part's SKU can be scanned as well as read. Drawn as SVG rectangles.
const CODE39 = { "0": "nnnwwnwnn", "1": "wnnwnnnnw", "2": "nnwwnnnnw", "3": "wnwwnnnnn", "4": "nnnwwnnnw",
  "5": "wnnwwnnnn", "6": "nnwwwnnnn", "7": "nnnwnnwnw", "8": "wnnwnnwnn", "9": "nnwwnnwnn", "*": "nwnnwnwnn" };

function barcodeSvg(text) {
  const NS = "http://www.w3.org/2000/svg";
  const chars = ["*", ...String(text).split(""), "*"];
  let x = 10;                                                  // quiet zone
  const bars = [];
  for (const ch of chars) {
    const pat = CODE39[ch];
    for (let i = 0; i < 9; i++) {
      const w = pat[i] === "w" ? 3 : 1;
      if (i % 2 === 0) bars.push([x, w]);                      // even positions are bars, odd are spaces
      x += w;
    }
    x += 1;                                                    // gap between characters
  }
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 " + (x - 1 + 10) + " 30");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("class", "lbl-barcode");
  svg.setAttribute("aria-hidden", "true");
  for (const [bx, bw] of bars) {
    const r = document.createElementNS(NS, "rect");
    r.setAttribute("x", String(bx)); r.setAttribute("y", "0"); r.setAttribute("width", String(bw)); r.setAttribute("height", "30");
    r.setAttribute("fill", "#000");
    svg.append(r);
  }
  return svg;
}

function labelEl(it, bizName, opts) {
  return h("div", { class: "lbl" },
    h("div", { class: "lbl-top" }, h("span", { class: "lbl-biz" }, bizName), h("span", { class: "lbl-tag" }, "SKU")),
    h("div", { class: "lbl-sku" }, String(it.part_no)),
    h("div", { class: "lbl-name" }, it.name),
    opts.barcode ? barcodeSvg(it.part_no) : null,
    opts.price ? h("div", { class: "lbl-price" }, money(it.price_cents)) : null);
}

// items: inventory rows. opts: { size: s|m|l, paged, price, barcode, copies, perUnit }
async function printLabels(items, opts) {
  const b = await biz();
  const labels = [];
  for (const it of items) {
    const n = opts.perUnit ? it.quantity : opts.copies;
    for (let i = 0; i < n; i++) labels.push(it);
  }
  if (!labels.length) throw new Error(opts.perUnit ? "The selected parts have no stock, so there is nothing to print." : "Nothing to print.");
  if (labels.length > 500) throw new Error("That would print " + labels.length + " labels. Please print 500 or fewer at a time.");
  const target = document.getElementById("print-root");
  target.replaceChildren(h("div", { class: "label-sheet lbl-" + opts.size + (opts.paged ? " paged" : "") },
    labels.map(it => labelEl(it, b.name, opts))));
  window.addEventListener("afterprint", () => target.replaceChildren(), { once: true });
  window.print();
}

async function inventoryView() {
  await biz();
  const canEdit = can("inventory.write");                 // also the permission that reveals cost
  const msg = h("div", { class: "error", role: "alert" });
  const list = h("div");
  const search = h("input", { type: "search", placeholder: "Search by SKU number, name or supplier part number", maxlength: 100 });
  const low = h("input", { type: "checkbox" });
  const inactive = h("input", { type: "checkbox" });
  const bySku = h("input", { type: "checkbox" });
  const justAdded = h("div");
  let editing = null;
  const selected = new Set();
  const known = new Map();                                // every row seen, so a selection survives changing the filter

  const f = {
    name: h("input", { required: true, maxlength: 120 }), sku: h("input", { maxlength: 60 }),
    desc: h("input", { maxlength: 300 }), cost: h("input", { inputmode: "decimal", placeholder: "0.00" }),
    price: h("input", { required: true, inputmode: "decimal", placeholder: "0.00" }),
    qty: h("input", { type: "number", min: "0", step: "1", value: "0" }),
    reorder: h("input", { type: "number", min: "0", step: "1", value: "0" }), taxable: h("input", { type: "checkbox", checked: true }),
  };
  const qtyField = field("Starting quantity", f.qty);
  const formTitle = h("h2", null, "Add a part");
  const formMsg = h("span", { class: "error", role: "alert" });
  const cancel = h("button", { type: "button", onclick: () => reset() }, "Cancel");
  const adjustBox = h("div");

  // label printing controls
  const lblSize = h("select", { "aria-label": "Label size" }, [["s", "Small 50 × 25 mm"], ["m", "Medium 70 × 40 mm"], ["l", "Large 100 × 60 mm"]]
    .map(([v, l]) => h("option", { value: v, selected: v === "m" }, l)));
  const lblLayout = h("select", { "aria-label": "Layout" }, [["sheet", "Many per page (label sheets or normal paper)"], ["paged", "One per page (label printer)"]]
    .map(([v, l]) => h("option", { value: v }, l)));
  const lblBarcode = h("input", { type: "checkbox", checked: true });
  const lblPrice = h("input", { type: "checkbox" });
  const lblCopies = h("input", { type: "number", min: "1", max: "100", step: "1", value: "1", "aria-label": "Copies" });
  const lblPerUnit = h("input", { type: "checkbox" });
  const labelOpts = () => ({ size: lblSize.value, paged: lblLayout.value === "paged", barcode: lblBarcode.checked, price: lblPrice.checked,
    copies: Math.max(1, Math.min(100, parseInt(lblCopies.value, 10) || 1)), perUnit: lblPerUnit.checked });
  const printSelected = h("button", { type: "button", class: "primary", onclick: () => {
    msg.textContent = "";
    printLabels([...selected].map(id => known.get(id)).filter(Boolean).sort((a, b) => a.part_no - b.part_no), labelOpts()).catch(showError(msg));
  } });
  const refreshSelected = () => {
    printSelected.textContent = "Print labels for " + selected.size + " selected";
    printSelected.disabled = selected.size === 0;
  };

  function reset() {
    editing = null;
    for (const k of ["name", "sku", "desc", "cost", "price"]) f[k].value = "";
    f.qty.value = "0"; f.reorder.value = "0"; f.taxable.checked = true;
    formTitle.textContent = "Add a part"; qtyField.style.display = ""; cancel.style.display = "none"; formMsg.textContent = "";
  }
  function edit(it) {
    editing = it;
    f.name.value = it.name; f.sku.value = it.sku; f.desc.value = it.description; f.cost.value = centsToInput(it.cost_cents || 0);
    f.price.value = centsToInput(it.price_cents); f.reorder.value = String(it.reorder_level); f.taxable.checked = !!it.taxable;
    formTitle.textContent = "Edit SKU " + it.part_no + " (change stock with “Adjust stock”)"; qtyField.style.display = "none"; cancel.style.display = "";
    f.name.focus(); f.name.scrollIntoView({ block: "center" });
  }

  async function openAdjust(it) {
    const d = await api("GET", "/api/inventory/" + enc(it.id));
    const delta = h("input", { type: "number", step: "1", required: true, placeholder: "+5 or -2" });
    const reason = h("input", { required: true, maxlength: 120, placeholder: "e.g. Received from supplier, damaged, count correction" });
    const aMsg = h("span", { class: "error", role: "alert" });
    adjustBox.replaceChildren(h("section", { class: "card" }, h("h2", null, "Adjust stock: SKU " + d.part_no + " · " + d.name + " (now " + d.quantity + ")"),
      h("form", { onsubmit: async (e) => {
          e.preventDefault(); aMsg.textContent = "";
          const n = parseInt(delta.value, 10);
          if (!Number.isInteger(n) || n === 0) { aMsg.textContent = "Enter a whole number other than 0"; return; }
          try { await api("POST", "/api/inventory/" + enc(it.id) + "/adjust", { delta: n, reason: reason.value }); adjustBox.replaceChildren(); await load(); }
          catch (err) { aMsg.textContent = err.message; }
        } },
        field("Change (use a minus to remove)", delta), field("Reason", reason),
        h("div", { class: "form-actions" }, h("button", { class: "primary", type: "submit" }, "Apply"),
          h("button", { type: "button", onclick: () => adjustBox.replaceChildren() }, "Close"), aMsg)),
      d.movements.length ? h("div", null, h("h3", null, "Recent stock changes"), h("ul", { class: "history" }, d.movements.slice(0, 10).map(m =>
        h("li", null, h("span", { class: "muted" }, m.ts.replace("T", " ").replace("Z", "") + " · " + (m.user_name || "system") + " — "),
          (m.delta > 0 ? "+" : "") + m.delta + " (" + m.reason + ")")))) : null));
    adjustBox.scrollIntoView({ block: "center" });
  }

  let shown = [];
  async function load() {
    const p = new URLSearchParams({ q: search.value.trim().replace(/^#/, "") });
    if (low.checked) p.set("low_stock", "1");
    if (inactive.checked) p.set("include_inactive", "1");
    if (bySku.checked) p.set("sort", "sku");
    const { items } = await api("GET", "/api/inventory?" + p.toString());
    shown = items;
    for (const it of items) known.set(it.id, it);
    const heads = ["", "SKU", "Part", "In stock", canEdit ? "Cost" : null, "Price", canEdit ? "Margin" : null, ""].filter(x => x !== null);
    list.replaceChildren(items.length ? h("div", { class: "table-wrap" }, h("table", null,
      h("thead", null, h("tr", null, heads.map(t => h("th", null, t)))),
      h("tbody", null, items.map(it => h("tr", { class: it.active ? "" : "muted" },
        h("td", null, h("input", { type: "checkbox", checked: selected.has(it.id), "aria-label": "Select SKU " + it.part_no, onchange: (e) => {
          if (e.target.checked) selected.add(it.id); else selected.delete(it.id);
          refreshSelected();
        } })),
        h("td", null, h("strong", null, String(it.part_no))),
        h("td", null, h("strong", null, it.name), it.active ? "" : " (inactive)",
          h("div", { class: "muted small" }, (it.sku ? "Supplier # " + it.sku : "") + (it.sku && it.description ? " · " : "") + it.description)),
        h("td", null, String(it.quantity), it.low_stock ? [" ", badge("Low", "badge-high")] : null),
        canEdit ? h("td", null, money(it.cost_cents)) : null,
        h("td", null, money(it.price_cents)),
        canEdit ? h("td", null, it.price_cents ? Math.round((it.price_cents - it.cost_cents) * 100 / it.price_cents) + "%" : "") : null,
        h("td", { class: "actions" },
          h("button", { type: "button", onclick: () => { msg.textContent = ""; printLabels([it], labelOpts()).catch(showError(msg)); } }, "Label"),
          canEdit ? [
            h("button", { type: "button", onclick: () => edit(it) }, "Edit"),
            h("button", { type: "button", onclick: () => openAdjust(it).catch(showError(msg)) }, "Adjust stock"),
            h("button", { type: "button", onclick: async () => {
                try { await api("PUT", "/api/inventory/" + enc(it.id), { sku: it.sku, name: it.name, description: it.description, cost_cents: it.cost_cents, price_cents: it.price_cents, reorder_level: it.reorder_level, taxable: !!it.taxable, active: !it.active }); await load(); }
                catch (e) { msg.textContent = e.message; } } }, it.active ? "Deactivate" : "Reactivate")] : null))))))
      : h("p", { class: "muted" }, "No parts found."));
    refreshSelected();
  }
  const reload = () => load().catch(showError(msg));
  let timer;
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(reload, 250); });
  for (const el of [low, inactive, bySku]) el.addEventListener("change", reload);

  cancel.style.display = "none";
  const nodes = [];
  if (canEdit) {
    nodes.push(h("section", { class: "card" }, formTitle,
      h("p", { class: "muted small" }, "Each new part automatically gets the next SKU number (1, 2, 3 …). Numbers are never reused, even after a part is deactivated or runs out."),
      h("form", { onsubmit: async (e) => {
          e.preventDefault(); formMsg.textContent = ""; justAdded.replaceChildren();
          const cost = f.cost.value.trim() === "" ? 0 : parseMoney(f.cost.value), price = parseMoney(f.price.value);
          if (cost === null || price === null) { formMsg.textContent = "Enter cost and price such as 12.50"; return; }
          const body = { name: f.name.value, sku: f.sku.value, description: f.desc.value, cost_cents: cost, price_cents: price,
            reorder_level: parseInt(f.reorder.value, 10) || 0, taxable: f.taxable.checked };
          try {
            if (editing) await api("PUT", "/api/inventory/" + enc(editing.id), { ...body, active: !!editing.active });
            else {
              const made = await api("POST", "/api/inventory", { ...body, quantity: parseInt(f.qty.value, 10) || 0 });
              known.set(made.id, made);
              justAdded.replaceChildren(h("p", { class: "ok" }, "Added “" + made.name + "” as SKU " + made.part_no + ". ",
                h("button", { type: "button", onclick: () => printLabels([made], labelOpts()).catch(showError(msg)) }, "Print label")));
            }
            reset(); await load();
          } catch (err) { formMsg.textContent = err.message; }
        } },
        field("Part name", f.name), field("Description", f.desc),
        field("Supplier / manufacturer part number (optional)", f.sku),
        field("Cost (what you pay)", f.cost), field("Sale price (what the customer pays)", f.price), qtyField,
        field("Reorder level (flag as low at or below this)", f.reorder), checkRow("Taxable", f.taxable),
        h("div", { class: "form-actions" }, h("button", { class: "primary", type: "submit" }, "Save part"), cancel, formMsg)),
      justAdded));
  }
  await load();
  nodes.push(adjustBox);
  nodes.push(h("section", { class: "card" }, h("h2", null, "Inventory"),
    h("p", { class: "muted small" }, "Parts leave stock when an invoice containing them is issued, and return if that invoice is voided."),
    search, h("div", { class: "filters" }, checkRow("Low stock only", low), checkRow("Show inactive", inactive), checkRow("Sort by SKU", bySku)), msg,
    h("details", { class: "label-opts" }, h("summary", null, "Label printing options"),
      h("div", { class: "grid2" }, field("Label size", lblSize), field("Layout", lblLayout)),
      h("div", { class: "grid2" }, field("Copies of each label", lblCopies), h("div", null, h("label", null, " "), checkRow("One label per unit in stock", lblPerUnit))),
      h("div", { class: "filters" }, checkRow("Barcode", lblBarcode), checkRow("Show price", lblPrice)),
      h("p", { class: "muted small" }, "Choose your paper or label size in the print dialog. Tick parts below, then print.")),
    h("div", { class: "form-actions wrap" }, printSelected,
      h("button", { type: "button", onclick: () => { for (const it of shown) selected.add(it.id); load().catch(showError(msg)); } }, "Select all shown"),
      h("button", { type: "button", onclick: () => { selected.clear(); load().catch(showError(msg)); } }, "Clear selection")),
    list));
  refreshSelected();
  return nodes;
}

// ------------------------------------------------------------ invoices: list and create
const statusBadge = (inv) => {
  const late = inv.status === "issued" && inv.due_date && inv.due_date < localToday();
  return badge(late ? "overdue" : inv.status, "inv-" + (late ? "overdue" : inv.status));
};

async function invoicesView() {
  await biz();
  const msg = h("div", { class: "error", role: "alert" });
  const list = h("div");
  const owed = h("p", { class: "muted" });
  const search = h("input", { type: "search", placeholder: "Search invoice number or customer", maxlength: 100 });
  const status = h("select", null, [["", "All"], ["draft", "Drafts"], ["issued", "Issued (unpaid)"], ["paid", "Paid"], ["void", "Void"]]
    .map(([v, l]) => h("option", { value: v }, l)));
  async function load() {
    const r = await api("GET", "/api/invoices?" + new URLSearchParams({ q: search.value.trim(), status: status.value, limit: "100" }).toString());
    owed.textContent = "Outstanding (issued, unpaid): " + money(r.outstanding_cents);
    list.replaceChildren(r.invoices.length ? h("div", { class: "table-wrap" }, h("table", null,
      h("thead", null, h("tr", null, ["Invoice", "Customer", "Issued", "Due", "Total", "Status"].map(t => h("th", null, t)))),
      h("tbody", null, r.invoices.map(i => h("tr", null,
        h("td", null, h("a", { href: "#/invoices/" + enc(i.id) }, i.number || "Draft"), i.ticket_number ? h("div", { class: "muted small" }, "Ticket #" + i.ticket_number) : null),
        h("td", null, i.customer_name), h("td", null, i.issue_date || ""), h("td", null, i.due_date || ""),
        h("td", null, money(i.total_cents)), h("td", null, statusBadge(i)))))))
      : h("p", { class: "muted" }, "No invoices match."));
  }
  const reload = () => load().catch(showError(msg));
  let timer;
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(reload, 250); });
  status.addEventListener("change", reload);
  await load();
  return [h("section", { class: "card" }, h("h2", null, "Invoices"),
    can("invoices.write") ? h("p", null, h("a", { class: "btn primary", href: "#/invoices/new" }, "+ New invoice")) : null,
    owed, h("div", { class: "filters" }, search, status), msg, list)];
}

async function invoiceNewView() {
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  let customer = null, ticket = null;
  if (params.get("ticket")) {
    try {
      const t = await api("GET", "/api/tickets/" + enc(params.get("ticket")));
      ticket = t; customer = { id: t.customer_id, name: t.customer_name, phone: t.customer_phone, email: t.customer_email };
    } catch (e) { ticket = null; }
  } else if (params.get("customer")) {
    try { customer = await api("GET", "/api/customers/" + enc(params.get("customer"))); } catch (e) { customer = null; }
  }
  const msg = h("span", { class: "error", role: "alert" });
  const picker = customerPicker({ initial: customer });
  return [h("p", null, h("a", { href: "#/invoices" }, "← All invoices")),
    h("section", { class: "card" }, h("h2", null, "New invoice"),
      ticket ? h("p", { class: "muted" }, "For ticket #" + ticket.number + " · " + ticket.title) : null,
      h("form", { onsubmit: async (e) => {
          e.preventDefault(); msg.textContent = "";
          const c = picker.value();
          if (!c) { msg.textContent = "Choose a customer first."; return; }
          const body = { customer_id: c.id };
          if (ticket && ticket.customer_id === c.id) body.ticket_id = ticket.id;
          try { const inv = await api("POST", "/api/invoices", body); location.hash = "#/invoices/" + enc(inv.id); }
          catch (err) { msg.textContent = err.message; }
        } },
        h("label", null, "Customer"), picker.el,
        h("div", { class: "form-actions" }, h("button", { class: "primary", type: "submit" }, "Start invoice"), msg)))];
}

// ------------------------------------------------------------ invoice sheet (screen preview and print)
function invoiceSheet(inv, b, s) {
  const bt = inv.bill_to || {};
  const q = (n) => String(n / 100);
  const brand = (BRAND && BRAND.primary_color) || "#3b9eff";
  const logo = logoUrl();
  const showName = !logo || !BRAND || BRAND.show_name;
  const tagline = BRAND && BRAND.tagline;
  const kv = (k, v) => (v ? h("div", { class: "sheet-kv" }, h("span", null, k), h("strong", null, v)) : null);
  const contact = [b.address, b.phone, b.email].filter(Boolean);
  const el = h("div", { class: "sheet" },
    h("div", { class: "sheet-bar" }),
    h("div", { class: "sheet-head" },
      h("div", { class: "sheet-brand" },
        logo ? h("img", { class: "sheet-logo", src: logo, alt: "" }) : null,
        h("div", null, showName ? h("div", { class: "sheet-biz" }, b.name) : null,
          tagline ? h("div", { class: "sheet-tagline" }, tagline) : null,
          contact.map(line => h("div", { class: "sheet-contact" }, line)))),
      h("div", { class: "sheet-meta" }, h("div", { class: "sheet-title" }, "INVOICE"),
        h("div", { class: "sheet-number" }, inv.number || "DRAFT"))),
    h("div", { class: "sheet-info" },
      h("div", { class: "sheet-box" }, h("div", { class: "sheet-label" }, "Bill to"),
        h("div", { class: "sheet-strong" }, bt.name || inv.customer_name),
        bt.contact_name ? h("div", null, bt.contact_name) : null, bt.address ? h("div", null, bt.address) : null,
        bt.email ? h("div", null, bt.email) : null, bt.phone ? h("div", null, bt.phone) : null),
      h("div", { class: "sheet-box" }, h("div", { class: "sheet-label" }, "Details"),
        kv("Issued", inv.issue_date), kv("Due", inv.due_date), kv("Ticket", inv.ticket_number ? "#" + inv.ticket_number : ""),
        kv("Status", inv.status === "draft" ? "Draft" : inv.status === "paid" ? "Paid" : inv.status === "void" ? "Void" : "Unpaid"))),
    h("table", { class: "sheet-lines" },
      h("thead", null, h("tr", null, h("th", null, "Description"), h("th", { class: "num" }, "Qty"), h("th", { class: "num" }, "Price"), h("th", { class: "num" }, "Amount"))),
      h("tbody", null, inv.items.map(it => h("tr", null,
        h("td", null, h("div", { class: "sheet-item" }, it.name), it.description ? h("div", { class: "sheet-sub" }, it.description) : null),
        h("td", { class: "num" }, q(it.qty_x100)), h("td", { class: "num" }, money(it.unit_cents)), h("td", { class: "num" }, money(it.line_cents)))))),
    h("table", { class: "sheet-totals" }, h("tbody", null,
      h("tr", null, h("td", null, "Subtotal"), h("td", { class: "num" }, money(inv.subtotal_cents))),
      h("tr", null, h("td", null, "Tax (" + (inv.tax_bp / 100) + "%)"), h("td", { class: "num" }, money(inv.tax_cents))),
      h("tr", { class: "grand" }, h("td", null, inv.status === "paid" ? "Total paid" : "Total due"), h("td", { class: "num" }, money(inv.total_cents))),
      inv.status === "paid" ? h("tr", { class: "paidrow" }, h("td", { colspan: "2" }, "Paid " + (inv.paid_at || "").slice(0, 10) + (inv.payment_method ? " by " + inv.payment_method : ""))) : null)),
    inv.notes ? h("div", { class: "sheet-notes" }, h("div", { class: "sheet-label" }, "Notes"), h("div", null, inv.notes)) : null,
    h("div", { class: "sheet-foot" }, s.footer ? h("div", { class: "sheet-thanks" }, s.footer) : null),
    inv.status === "void" ? h("div", { class: "sheet-void" }, "VOID") : null,
    inv.status === "paid" ? h("div", { class: "sheet-paid" }, "PAID") : null);
  el.style.setProperty("--brand", brand);
  el.style.setProperty("--brand-on", contrastOn(brand));
  return el;
}

async function printInvoice(inv) {
  const [b, s] = await Promise.all([biz(), api("GET", "/api/invoices/settings")]);
  const target = document.getElementById("print-root");
  target.replaceChildren(invoiceSheet(inv, b, s));
  // make sure the logo has loaded, or it would be missing from the printout
  await Promise.all([...target.querySelectorAll("img")].map(img => (img.decode ? img.decode().catch(() => {}) : Promise.resolve())));
  window.addEventListener("afterprint", () => target.replaceChildren(), { once: true });
  window.print();
}

async function invoiceDetail(id) {
  const [inv, b, s] = await Promise.all([api("GET", "/api/invoices/" + enc(id)), biz(), api("GET", "/api/invoices/settings")]);
  return inv.status === "draft" && can("invoices.write") ? invoiceEditor(inv, b, s) : invoiceReadOnly(inv, b, s);
}

function invoiceReadOnly(inv, b, s) {
  const msg = h("div", { class: "error", role: "alert" });
  const act = (fn) => async () => { msg.textContent = ""; try { await fn(); route(); } catch (e) { msg.textContent = e.message; } };
  const nodes = [h("p", null, h("a", { href: "#/invoices" }, "← All invoices"))];
  const actions = h("div", { class: "form-actions wrap" },
    h("button", { type: "button", class: "primary", onclick: () => printInvoice(inv).catch(showError(msg)) }, "Print"));
  nodes.push(h("section", { class: "card" }, h("h2", null, (inv.number || "Draft invoice") + " ", statusBadge(inv)), actions, msg));
  if (can("invoices.write") && inv.status === "issued") {
    const method = h("select", { "aria-label": "Payment method" }, ["Cash", "Card", "Check", "Bank transfer", "Other"].map(m => h("option", { value: m }, m)));
    actions.append(method, h("button", { type: "button", onclick: act(() => api("POST", "/api/invoices/" + enc(inv.id) + "/pay", { method: method.value })) }, "Mark paid"));
  }
  if (can("invoices.write") && (inv.status === "issued" || inv.status === "paid")) {
    const reason = h("input", { placeholder: "Reason for voiding", maxlength: 200, "aria-label": "Void reason" });
    actions.append(reason, h("button", { type: "button", onclick: () => {
      if (!reason.value.trim()) { msg.textContent = "Enter a reason to void this invoice."; return; }
      if (confirm("Void this invoice? Parts on it go back into stock. This cannot be undone.")) act(() => api("POST", "/api/invoices/" + enc(inv.id) + "/void", { reason: reason.value }))();
    } }, "Void"));
  }
  if (inv.status === "void") nodes[1].append(h("p", { class: "muted" }, "Voided: " + inv.void_reason));
  nodes.push(h("div", { class: "sheet-wrap" }, invoiceSheet(inv, b, s)));
  return nodes;
}

// ------------------------------------------------------------ invoice editor (drafts)
async function invoiceEditor(inv, b, s) {
  const msg = h("div", { role: "alert", class: "error" });
  const services = can("services.read") ? (await api("GET", "/api/services")).services : [];
  let lines = inv.items.map(it => ({ kind: it.kind, ref_id: it.ref_id, name: it.name, description: it.description,
    qty: String(it.quantity), price: centsToInput(it.unit_cents), taxable: it.taxable }));

  const dueIn = h("input", { type: "date", value: inv.due_date || "" });
  const taxIn = h("input", { inputmode: "decimal", value: String(inv.tax_bp / 100), "aria-label": "Tax rate percent" });
  const notesIn = h("textarea", { rows: "2", maxlength: 2000 }, inv.notes);
  const linesBox = h("div", { class: "lines" });
  const totalsBox = h("div", { class: "totals-box" });

  function calc() {
    let sub = 0, taxBase = 0, ok = true;
    for (const l of lines) {
      const q = parseQty(l.qty), p = parseMoney(l.price);
      if (q === null || p === null) { ok = false; continue; }
      const c = lineCents(q, p); sub += c; if (l.taxable) taxBase += c;
    }
    const bp = parsePct(taxIn.value);
    if (bp === null) ok = false;
    const tax = bp === null ? 0 : Math.floor((taxBase * bp + 5000) / 10000);
    return { sub, tax, total: sub + tax, ok };
  }
  function drawTotals() {
    const t = calc();
    totalsBox.replaceChildren(h("div", null, "Subtotal: " + money(t.sub)), h("div", null, "Tax: " + money(t.tax)),
      h("div", { class: "grand" }, "Total: " + money(t.total)), t.ok ? null : h("div", { class: "muted small" }, "Fix the highlighted values to see exact totals."));
  }

  function lineEl(l, i) {
    const total = h("div", { class: "line-total" });
    const refresh = () => {
      const q = parseQty(l.qty), p = parseMoney(l.price);
      total.textContent = q !== null && p !== null ? money(lineCents(q, p)) : "—";
      qty.classList.toggle("bad", q === null); price.classList.toggle("bad", p === null); name.classList.toggle("bad", !l.name.trim());
      drawTotals();
    };
    const bind = (el, key) => el.addEventListener("input", () => { l[key] = el.value; refresh(); });
    const name = h("input", { value: l.name, maxlength: 120, placeholder: "Item name", "aria-label": "Item name" });
    const desc = h("input", { value: l.description, maxlength: 300, placeholder: "Short description (optional)", "aria-label": "Description" });
    const qty = h("input", { value: l.qty, inputmode: "decimal", "aria-label": "Quantity" });
    const price = h("input", { value: l.price, inputmode: "decimal", "aria-label": "Unit price" });
    const tax = h("input", { type: "checkbox", checked: l.taxable, "aria-label": "Taxable" });
    bind(name, "name"); bind(desc, "description"); bind(qty, "qty"); bind(price, "price");
    tax.addEventListener("change", () => { l.taxable = tax.checked; refresh(); });
    const el = h("div", { class: "line" },
      h("div", { class: "line-kind" }, badge(l.kind === "service" ? "Service" : l.kind === "inventory" ? "Part" : "Custom")),
      h("div", { class: "line-name" }, name), h("div", { class: "line-desc" }, desc),
      h("div", { class: "line-qty" }, h("label", null, "Qty"), qty), h("div", { class: "line-price" }, h("label", null, "Price"), price),
      h("label", { class: "inline line-tax" }, tax, " Tax"), total,
      h("button", { type: "button", class: "line-del", "aria-label": "Remove line", onclick: () => { lines.splice(i, 1); drawLines(); } }, "✕"));
    refresh();
    return el;
  }
  function drawLines() {
    linesBox.replaceChildren(...(lines.length ? lines.map(lineEl) : [h("p", { class: "muted" }, "No lines yet. Add a service, a part, or a custom line below.")]));
    drawTotals();
  }
  taxIn.addEventListener("input", drawTotals);

  // add controls
  const svcSel = h("select", { "aria-label": "Service" }, h("option", { value: "" }, "Choose a service…"),
    services.map(sv => h("option", { value: sv.id }, sv.name + " — " + money(sv.price_cents))));
  const addSvc = h("button", { type: "button", onclick: () => {
    const sv = services.find(x => x.id === svcSel.value); if (!sv) return;
    lines.push({ kind: "service", ref_id: sv.id, name: sv.name, description: sv.description, qty: "1", price: centsToInput(sv.price_cents), taxable: !!sv.taxable });
    svcSel.value = ""; drawLines();
  } }, "Add service");
  const partSearch = h("input", { type: "search", placeholder: "Search parts to add", maxlength: 100 });
  const partResults = h("div", { class: "picker-results" });
  let ptimer;
  let foundParts = [];
  const addPart = (it) => {
    lines.push({ kind: "inventory", ref_id: it.id, name: it.name, description: it.description, qty: "1", price: centsToInput(it.price_cents), taxable: !!it.taxable });
    drawLines();
  };
  async function findParts() {
    try {
      const { items } = await api("GET", "/api/inventory?limit=8&q=" + enc(partSearch.value.trim().replace(/^#/, "")));
      foundParts = items;
      partResults.replaceChildren(...(items.length ? items.map(it => h("button", { type: "button", class: "picker-item", onclick: () => addPart(it) },
        h("strong", null, "SKU " + it.part_no + " · " + it.name), " · " + money(it.price_cents) + " · " + it.quantity + " in stock"))
        : [h("p", { class: "muted" }, "No parts found.")]));
    } catch (e) { msg.textContent = e.message; }
  }
  partSearch.addEventListener("input", () => { clearTimeout(ptimer); ptimer = setTimeout(findParts, 200); });
  partSearch.addEventListener("keydown", async (e) => {         // typing or scanning a SKU number and pressing Enter adds that part
    if (e.key !== "Enter") return;
    e.preventDefault();
    clearTimeout(ptimer);
    await findParts();
    const wanted = partSearch.value.trim().replace(/^#/, "");
    const exact = foundParts.find(it => String(it.part_no) === wanted);
    if (exact) { addPart(exact); partSearch.value = ""; partResults.replaceChildren(); }
  });
  const addManual = h("button", { type: "button", onclick: () => { lines.push({ kind: "manual", ref_id: null, name: "", description: "", qty: "1", price: "", taxable: true }); drawLines(); } }, "+ Custom line");

  function collect() {
    const items = lines.map((l, i) => {
      const q = parseQty(l.qty), p = parseMoney(l.price);
      if (!l.name.trim()) throw new Error("Line " + (i + 1) + ": enter an item name");
      if (q === null) throw new Error("Line " + (i + 1) + ": quantity must be a number such as 1 or 1.5");
      if (p === null) throw new Error("Line " + (i + 1) + ": enter a price such as 49.99");
      return { kind: l.kind, ref_id: l.ref_id, name: l.name.trim(), description: l.description.trim(), quantity: q / 100, unit_price_cents: p, taxable: l.taxable };
    });
    const bp = parsePct(taxIn.value);
    if (bp === null) throw new Error("Tax rate must be a percentage between 0 and 100");
    return { tax_bp: bp, due_date: dueIn.value || null, notes: notesIn.value, items };
  }
  async function save() { inv = await api("PUT", "/api/invoices/" + enc(inv.id), collect()); }
  const run = (fn, done) => async () => {
    msg.className = "error"; msg.textContent = "";
    try { await fn(); if (done) done(); } catch (e) { msg.textContent = e.message; }
  };

  const actions = h("div", { class: "form-actions wrap" },
    h("button", { type: "button", class: "primary", onclick: run(save, () => { msg.className = "ok"; msg.textContent = "Draft saved"; }) }, "Save draft"),
    h("button", { type: "button", onclick: run(async () => { await save(); await printInvoice(inv); }) }, "Print"),
    h("button", { type: "button", onclick: run(async () => {
        if (!confirm("Issue this invoice? It gets its number, the lines lock, and parts are taken out of stock.")) return;
        await save(); await api("POST", "/api/invoices/" + enc(inv.id) + "/issue", {}); route();
      }) }, "Issue invoice"),
    h("button", { type: "button", onclick: run(async () => {
        if (!confirm("Delete this draft? This cannot be undone.")) return;
        await api("DELETE", "/api/invoices/" + enc(inv.id)); location.hash = "#/invoices";
      }) }, "Delete draft"));

  drawLines();
  return [h("p", null, h("a", { href: "#/invoices" }, "← All invoices")),
    h("section", { class: "card" }, h("h2", null, "Draft invoice " , badge("draft", "inv-draft")),
      h("p", null, h("strong", null, inv.customer_name), inv.ticket_number ? "  ·  Ticket #" + inv.ticket_number : ""),
      h("div", { class: "grid2" }, field("Due date (blank = " + s.terms_days + " days after issue)", dueIn), field("Tax rate (%)", taxIn)),
      field("Notes shown on the invoice", notesIn)),
    h("section", { class: "card" }, h("h2", null, "Line items"), linesBox,
      h("h3", null, "Add to invoice"),
      services.length ? h("div", { class: "add-row" }, svcSel, addSvc) : null,
      can("inventory.read") ? h("div", null, partSearch, partResults) : null,
      h("div", { class: "form-actions" }, addManual)),
    h("section", { class: "card" }, totalsBox, actions, msg)];
}

// ------------------------------------------------------------ settings card
async function invoiceSettingsCard() {
  const cur = await api("GET", "/api/invoices/settings");
  const prefix = h("input", { value: cur.prefix, maxlength: 10 });
  const next = h("input", { type: "number", min: "1", value: String(cur.next_number) });
  const tax = h("input", { inputmode: "decimal", value: String(cur.default_tax_bp / 100) });
  const terms = h("input", { type: "number", min: "0", max: "365", value: String(cur.terms_days) });
  const footer = h("input", { value: cur.footer, maxlength: 300 });
  const msg = h("span", { class: "muted" });
  return h("section", { class: "card" }, h("h2", null, "Invoicing"),
    h("form", { onsubmit: async (e) => {
        e.preventDefault(); msg.className = "muted"; msg.textContent = "Saving…";
        const bp = parsePct(tax.value);
        if (bp === null) { msg.className = "error"; msg.textContent = "Tax rate must be 0 to 100"; return; }
        try {
          await api("PUT", "/api/invoices/settings", { prefix: prefix.value, next_number: parseInt(next.value, 10), default_tax_bp: bp,
            terms_days: parseInt(terms.value, 10), footer: footer.value });
          msg.className = "ok"; msg.textContent = "Saved";
        } catch (err) { msg.className = "error"; msg.textContent = err.message; }
      } },
      field("Invoice number prefix", prefix), field("Next invoice number", next), field("Default sales tax rate (%)", tax),
      field("Payment terms (days until due)", terms), field("Footer text on printed invoices", footer),
      h("div", { class: "form-actions" }, h("button", { class: "primary", type: "submit" }, "Save"), msg)));
}
