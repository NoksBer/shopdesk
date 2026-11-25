"use strict";
// Running cash ledger screen and the dashboard cash-flow card. Positive = money in, negative = money out.

const signedMoney = (c) => (c > 0 ? "+" : c < 0 ? "−" : "") + money(Math.abs(c));
const flowClass = (c) => (c < 0 ? "neg" : c > 0 ? "pos" : "");

function ledgerRef(e) {
  if (e.ref_type === "invoice" && e.ref_id && can("invoices.read")) return h("a", { href: "#/invoices/" + enc(e.ref_id) }, e.ref_label || "invoice");
  if (e.ref_type === "inventory" && can("inventory.read")) return h("a", { href: "#/inventory" }, e.ref_label || "part");
  return e.ref_label ? h("span", null, e.ref_label) : null;
}

function flowTile(label, cents, note, signed) {
  return h("div", { class: "tile " + flowClass(cents) },
    h("span", { class: "big" }, signed ? signedMoney(cents) : money(cents)), h("span", null, label),
    note ? h("span", { class: "muted small" }, note) : null);
}

function flowSentence(net, what) {
  return net > 0 ? "Cash flow is positive " + what + "." : net < 0 ? "Cash flow is negative " + what + "." : "Cash flow is break-even " + what + ".";
}

// ------------------------------------------------------------ dashboard card
function cashflowCard(led) {
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const tm = led.this_month;
  const peak = Math.max(1, ...led.months.map(m => Math.max(m.money_in, m.money_out)));
  const pct = (c) => (c ? Math.max(3, Math.round(c * 100 / peak)) : 0) + "%";
  const bars = led.months.map(m => {
    const inFill = h("div", { class: "bar-fill in" }), outFill = h("div", { class: "bar-fill out" });
    inFill.style.height = pct(m.money_in); outFill.style.height = pct(m.money_out);
    return h("div", { class: "bar wide", role: "img", title: MON[parseInt(m.month.slice(5), 10) - 1] + ": in " + money(m.money_in) + ", out " + money(m.money_out),
      "aria-label": MON[parseInt(m.month.slice(5), 10) - 1] + " net " + signedMoney(m.net) },
      h("div", { class: "bar-track pair" }, inFill, outFill), h("div", { class: "bar-label" }, MON[parseInt(m.month.slice(5), 10) - 1]));
  });
  return h("section", { class: "card" }, h("h2", null, "Cash flow"),
    h("p", { class: "muted small" }, "Every payment received and every cost recorded in the ledger. " + flowSentence(led.balance_cents, "overall")),
    h("div", { class: "tiles" },
      flowTile("Balance", led.balance_cents, led.balance_cents < 0 ? "Negative" : "Positive", true),
      flowTile("Money in (this month)", tm.money_in), flowTile("Money out (this month)", tm.money_out),
      flowTile("Net (this month)", tm.net, null, true)),
    h("div", { class: "bars six" }, bars),
    h("div", { class: "legend small muted" }, h("span", { class: "dot in" }), " in  ", h("span", { class: "dot out" }), " out"),
    led.recent.length ? h("ul", { class: "history" }, led.recent.map(e => h("li", null,
      h("span", { class: "muted" }, e.entry_date + " · "), e.description + " ", ledgerRef(e),
      h("strong", { class: "amt " + flowClass(e.amount_cents) }, "  " + signedMoney(e.amount_cents))))) : h("p", { class: "muted" }, "Nothing in the ledger yet."),
    h("a", { class: "btn", href: "#/ledger" }, "Open the ledger"));
}

// ------------------------------------------------------------ ledger screen
function rangeFor(preset) {
  const n = new Date(), y = n.getFullYear(), m = n.getMonth();
  if (preset === "month") return [ymd(new Date(y, m, 1)), ymd(new Date(y, m + 1, 0))];
  if (preset === "last_month") return [ymd(new Date(y, m - 1, 1)), ymd(new Date(y, m, 0))];
  if (preset === "30") return [ymd(new Date(y, m, n.getDate() - 29)), ymd(n)];
  if (preset === "year") return [ymd(new Date(y, 0, 1)), ymd(new Date(y, 11, 31))];
  return ["", ""];
}

async function ledgerView() {
  await biz();
  const canWrite = can("ledger.write");
  const msg = h("div", { class: "error", role: "alert" });
  const tiles = h("div", { class: "tiles" });
  const sentence = h("p", { class: "muted small" });
  const body = h("tbody");
  const more = h("button", { type: "button" }, "Load more");
  const empty = h("p", { class: "muted" });
  const exportLink = h("a", { class: "btn", href: "#", download: "ledger.csv" }, "Download CSV");
  let rows = [], total = 0, categories = { income: {}, expense: {} };

  const preset = h("select", { "aria-label": "Period" }, [["month", "This month"], ["last_month", "Last month"], ["30", "Last 30 days"],
    ["year", "This year"], ["all", "All time"], ["custom", "Custom dates"]].map(([v, l]) => h("option", { value: v }, l)));
  const [f0, t0] = rangeFor("month");
  const from = h("input", { type: "date", value: f0, "aria-label": "From" });
  const to = h("input", { type: "date", value: t0, "aria-label": "To" });
  const dir = h("select", { "aria-label": "Direction" }, [["", "In and out"], ["in", "Money in"], ["out", "Money out"]].map(([v, l]) => h("option", { value: v }, l)));
  const cat = h("select", { "aria-label": "Category" }, h("option", { value: "" }, "All categories"));
  const search = h("input", { type: "search", placeholder: "Search description or invoice / SKU", maxlength: 100 });

  const params = (offset) => {
    const p = new URLSearchParams({ from: from.value, to: to.value, dir: dir.value, category: cat.value, q: search.value.trim() });
    if (offset !== undefined) { p.set("limit", "100"); p.set("offset", String(offset)); }
    return p.toString();
  };
  function addRows(list) {
    for (const e of list) {
      const canReverse = canWrite && !e.reversed && !e.reverses_id && e.category !== "invoice_payment";
      body.append(h("tr", { class: e.reverses_id || e.reversed ? "muted" : "" },
        h("td", null, e.entry_date),
        h("td", null, e.description, " ", ledgerRef(e), e.reversed ? [" ", badge("reversed")] : null, e.reverses_id ? [" ", badge("reversal")] : null,
          h("div", { class: "muted small" }, (e.user_name ? e.user_name + " · " : "") + e.category_label)),
        h("td", { class: "num amt pos" }, e.amount_cents > 0 ? money(e.amount_cents) : ""),
        h("td", { class: "num amt neg" }, e.amount_cents < 0 ? money(-e.amount_cents) : ""),
        h("td", { class: "num amt " + flowClass(e.balance_cents) }, signedMoney(e.balance_cents).replace("+", "")),
        h("td", { class: "actions" }, canReverse ? h("button", { type: "button", onclick: async () => {
          const reason = prompt("Why are you reversing this entry? (it stays in the ledger, with an opposite entry added)");
          if (!reason || !reason.trim()) return;
          try { await api("POST", "/api/ledger/" + enc(e.id) + "/reverse", { reason: reason.trim() }); await load(); }
          catch (err) { msg.textContent = err.message; }
        } }, "Reverse") : null)));
    }
  }
  async function load() {
    msg.textContent = "";
    const r = await api("GET", "/api/ledger?" + params(0));
    categories = r.categories;
    rows = r.entries; total = r.total;
    body.replaceChildren(); addRows(rows);
    tiles.replaceChildren(flowTile("Balance now", r.balance_cents, r.balance_cents < 0 ? "Negative" : "Positive", true),
      flowTile("Money in", r.money_in), flowTile("Money out", r.money_out), flowTile("Net for this view", r.net, null, true));
    sentence.textContent = r.total === 0 && r.balance_cents === 0 ? "No money has moved yet." : flowSentence(r.net, "in this view") + " " + flowSentence(r.balance_cents, "overall");
    empty.textContent = rows.length ? "" : "No ledger entries match.";
    more.style.display = rows.length < total ? "" : "none";
    exportLink.href = "/api/ledger/export?" + params();
    fillCategories();
    refreshAddCategories();
  }
  function fillCategories() {
    if (cat.options.length > 1) return;
    const all = { ...categories.income, ...categories.expense, invoice_payment: "Invoice payment", inventory_purchase: "Inventory purchase", reversal: "Reversal" };
    for (const [k, l] of Object.entries(all)) cat.append(h("option", { value: k }, l));
  }
  const reload = () => load().catch(showError(msg));
  let timer;
  const later = () => { clearTimeout(timer); timer = setTimeout(reload, 250); };
  preset.addEventListener("change", () => { if (preset.value !== "custom") { const [a, b] = rangeFor(preset.value); from.value = a; to.value = b; } reload(); });
  for (const el of [from, to]) el.addEventListener("change", () => { preset.value = "custom"; reload(); });
  for (const el of [dir, cat]) el.addEventListener("change", reload);
  search.addEventListener("input", later);
  more.addEventListener("click", async () => {
    try { const r = await api("GET", "/api/ledger?" + params(rows.length)); rows = rows.concat(r.entries); addRows(r.entries); more.style.display = rows.length < total ? "" : "none"; }
    catch (e) { msg.textContent = e.message; }
  });

  // manual entry form
  const aDir = h("select", { "aria-label": "Money in or out" }, h("option", { value: "in" }, "Money in"), h("option", { value: "out", selected: true }, "Money out"));
  const aCat = h("select", { "aria-label": "Category" });
  const aAmount = h("input", { required: true, inputmode: "decimal", placeholder: "0.00" });
  const aDesc = h("input", { required: true, maxlength: 200, placeholder: "e.g. Shop rent for March" });
  const aDate = h("input", { type: "date", value: localToday() });
  const aMsg = h("span", { class: "error", role: "alert" });
  function refreshAddCategories() {
    const set = aDir.value === "in" ? categories.income : categories.expense;
    const keep = aCat.value;
    aCat.replaceChildren(...Object.entries(set).map(([k, l]) => h("option", { value: k, selected: k === keep }, l)));
  }
  aDir.addEventListener("change", refreshAddCategories);

  const nodes = [];
  nodes.push(h("section", { class: "card" }, h("h2", null, "Ledger"),
    h("p", { class: "muted small" }, "A running record of cash in and out. Inventory purchases and invoice payments are added automatically; use “Add entry” for everything else. Entries are never edited or deleted. Mistakes are corrected with a reversing entry."),
    tiles, sentence));
  if (canWrite) {
    nodes.push(h("section", { class: "card" }, h("details", null, h("summary", null, "Add entry"),
      h("form", { onsubmit: async (e) => {
          e.preventDefault(); aMsg.textContent = "";
          const cents = parseMoney(aAmount.value);
          if (cents === null || cents === 0) { aMsg.textContent = "Enter an amount such as 125.00"; return; }
          try {
            await api("POST", "/api/ledger", { direction: aDir.value, amount_cents: cents, category: aCat.value, description: aDesc.value, entry_date: aDate.value });
            aAmount.value = ""; aDesc.value = ""; await load();
          } catch (err) { aMsg.textContent = err.message; }
        } },
        h("div", { class: "grid2" }, field("Type", aDir), field("Category", aCat)),
        h("div", { class: "grid2" }, field("Amount", aAmount), field("Date", aDate)),
        field("Description", aDesc),
        h("div", { class: "form-actions" }, h("button", { class: "primary", type: "submit" }, "Add to ledger"), aMsg)))));
  }
  await load();
  nodes.push(h("section", { class: "card" },
    h("div", { class: "filters" }, preset, from, to), h("div", { class: "filters" }, dir, cat), search,
    msg, h("div", { class: "table-wrap" }, h("table", null,
      h("thead", null, h("tr", null, ["Date", "Description", "In", "Out", "Balance", ""].map((t, i) => h("th", { class: i >= 2 && i <= 4 ? "num" : "" }, t)))), body)),
    empty, h("div", { class: "form-actions wrap" }, more, exportLink)));
  return nodes;
}
