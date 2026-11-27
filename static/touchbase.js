"use strict";
// Customer touch bases: the optional popup offered when a ticket is completed, reschedule/cancel, calendar detail view,
// dashboard card and the note on the ticket page. Loaded before app.js; uses h(), api(), can(), enc, ymd, route() from it.

const tbPad = (n) => String(n).padStart(2, "0");
const tbUS = (d) => tbPad(d.getMonth() + 1) + "/" + tbPad(d.getDate()) + "/" + d.getFullYear();      // MM/DD/YYYY
const tbFromIso = (iso) => { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d); };
const tbToday = () => { const n = new Date(); return new Date(n.getFullYear(), n.getMonth(), n.getDate()); };
const tbDays = (from, to) => Math.round((to - from) / 86400000);
function tbParseUS(text) {                                           // "MM/DD/YYYY" -> Date, or null
  const m = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(text.trim());
  if (!m) return null;
  const d = new Date(+m[3], +m[1] - 1, +m[2]);
  return d.getMonth() === +m[1] - 1 && d.getDate() === +m[2] ? d : null;
}
function tbAddMonths(d, n) {                                         // keeps the day, clamped to the end of a short month
  const first = new Date(d.getFullYear(), d.getMonth() + n, 1);
  const last = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate();
  return new Date(first.getFullYear(), first.getMonth(), Math.min(d.getDate(), last));
}

function tbModal(...content) {                                        // native <dialog>: Esc closes it (same as Skip)
  const dlg = h("dialog", { class: "modal" }, ...content);
  dlg.addEventListener("close", () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();
  return dlg;
}

// ticket: { id, number, customer_name }; existing: a scheduled touch base to reschedule (omit to schedule a new one).
function touchBaseDialog({ ticket, existing = null, onSaved = null }) {
  const today = tbToday();
  const days = h("input", { type: "number", min: "0", max: "3650", inputmode: "numeric", "aria-label": "Days from today" });
  const date = h("input", { type: "text", placeholder: "MM/DD/YYYY", maxlength: 10, inputmode: "numeric", "aria-label": "Calendar date" });
  const notes = h("textarea", { rows: "3", maxlength: 1000, placeholder: "What to check on, e.g. is the battery replacement holding up?" }, existing ? existing.notes : "");
  const err = h("div", { class: "error", role: "alert" });
  const skip = h("button", { type: "button", autofocus: true }, existing ? "Close" : "Skip");
  const save = h("button", { type: "button", class: "primary" }, existing ? "Save" : "Schedule");

  const setDate = (d) => { date.value = tbUS(d); days.value = String(Math.max(0, tbDays(today, d))); err.textContent = ""; };
  days.addEventListener("input", () => {                              // days -> date
    const n = parseInt(days.value, 10);
    if (Number.isInteger(n) && n >= 0 && n <= 3650) date.value = tbUS(new Date(today.getFullYear(), today.getMonth(), today.getDate() + n));
  });
  date.addEventListener("input", () => {                              // date -> days
    const d = tbParseUS(date.value);
    if (d && d >= today) days.value = String(tbDays(today, d));
  });
  const quick = (label, make) => h("button", { type: "button", class: "mini", onclick: () => setDate(make()) }, label);
  const plus = (n) => () => new Date(today.getFullYear(), today.getMonth(), today.getDate() + n);
  if (existing) setDate(tbFromIso(existing.due_date));
  // Per-shop-type presets (e.g. window tinting): set the date and prefill the note. Shops without presets see none.
  let lastPreset = "";
  const presetButtons = ((ME && ME.touch_base_presets) || []).map((p) => h("button", { type: "button", class: "mini", onclick: () => {
    setDate(p.months ? tbAddMonths(today, p.months) : plus(p.days || 0)());
    if (!notes.value.trim() || notes.value === lastPreset) { notes.value = p.note || ""; lastPreset = notes.value; }
  } }, p.label));

  const dlg = tbModal(
    h("h2", null, existing ? "Reschedule touch base" : "Customer Touch Base"),
    h("p", { class: "muted small" }, "Ticket #" + ticket.number + " · " + ticket.customer_name +
      (existing ? "" : " is complete. Optional: add a reminder to follow up with this customer.")),
    h("div", { class: "tb-row" }, h("label", null, "Touch base in"), days, h("span", null, "days")),
    h("div", { class: "tb-row" }, h("label", null, "Create calendar item for"), date),
    h("div", { class: "tb-quick" }, quick("30 days", plus(30)), quick("60 days", plus(60)), quick("90 days", plus(90)),
      quick("6 months", () => tbAddMonths(today, 6)), quick("1 year", () => tbAddMonths(today, 12))),
    presetButtons.length ? h("div", { class: "tb-quick" }, presetButtons) : null,
    h("label", null, "Touch base on"), notes, err,
    h("div", { class: "form-actions" }, skip, " ", save));

  skip.addEventListener("click", () => dlg.close());
  save.addEventListener("click", async () => {
    err.textContent = "";
    const d = tbParseUS(date.value);
    if (!d) { err.textContent = "Enter the date as MM/DD/YYYY, or choose Skip."; return; }
    if (d < today) { err.textContent = "The date cannot be in the past."; return; }
    save.disabled = true;
    try {
      const body = { due_date: ymd(d), notes: notes.value };
      const r = existing ? await api("PUT", "/api/touch-bases/" + enc(existing.id), body)
        : await api("POST", "/api/tickets/" + enc(ticket.id) + "/touch-base", body);
      dlg.close();
      if (onSaved) onSaved(r.touch_base);
    } catch (e) { err.textContent = e.message; save.disabled = false; }
  });
  return dlg;
}

// Called with the response of POST /api/tickets/<id>/status. Never blocks or undoes the status change.
function maybeOfferTouchBase(ticket) {
  if (!ticket || !ticket.touch_base_offer || !can("tickets.write")) return;
  touchBaseDialog({ ticket, onSaved: () => { if (location.hash.startsWith("#/tickets/")) route(); } });
}

async function cancelTouchBase(tb, done) {
  if (!confirm("Cancel the touch base for " + tb.customer_name + " (" + tbUS(tbFromIso(tb.due_date)) + ")?")) return;
  try { await api("POST", "/api/touch-bases/" + enc(tb.id) + "/cancel", {}); done(); }
  catch (e) { alert(e.message); }
}

// Small detail view used from the calendar.
function touchBaseDetail(tb, onChange) {
  const dlg = tbModal(
    h("h2", null, "Customer touch base"),
    h("p", null, h("strong", null, tb.customer_name), " · " + tbUS(tbFromIso(tb.due_date))),
    tb.notes ? h("p", { class: "note-body" }, tb.notes) : h("p", { class: "muted" }, "No notes."),
    h("p", { class: "muted small" }, "From ticket ", h("a", { href: "#/tickets/" + enc(tb.ticket_id), onclick: () => dlg.close() }, "#" + tb.ticket_number),
      " · scheduled by " + (tb.scheduled_by_name || "unknown") + ". A follow-up ticket is created automatically on the due date."),
    h("div", { class: "form-actions" },
      h("button", { type: "button", autofocus: true, onclick: () => dlg.close() }, "Close"),
      can("tickets.write") ? [" ",
        h("button", { type: "button", onclick: () => { dlg.close();
          touchBaseDialog({ ticket: { id: tb.ticket_id, number: tb.ticket_number, customer_name: tb.customer_name }, existing: tb, onSaved: onChange }); } }, "Reschedule"),
        " ", h("button", { type: "button", onclick: () => { dlg.close(); cancelTouchBase(tb, onChange); } }, "Cancel touch base")] : null));
  return dlg;
}

// Note on the original ticket's page while a touch base is scheduled (null when there is none).
async function touchBaseNote(t) {
  let tb;
  try { tb = (await api("GET", "/api/tickets/" + enc(t.id) + "/touch-base")).touch_base; } catch (e) { return null; }
  if (!tb) return null;
  return h("section", { class: "card tb-note" },
    h("strong", null, "Customer touch base scheduled for " + tbUS(tbFromIso(tb.due_date))),
    tb.notes ? h("p", { class: "note-body" }, tb.notes) : null,
    can("tickets.write") ? h("div", { class: "form-actions" },
      h("button", { type: "button", onclick: () => touchBaseDialog({ ticket: t, existing: tb, onSaved: route }) }, "Reschedule"), " ",
      h("button", { type: "button", onclick: () => cancelTouchBase(tb, route) }, "Cancel")) : null);
}

// Dashboard card: overdue first, then the next 14 days.
function touchBaseCard(list) {
  const today = ymd(new Date());
  return h("section", { class: "card" }, h("h2", null, "Upcoming touch bases"),
    list.length ? h("ul", { class: "history" }, list.map((tb) => h("li", null,
      h("span", { class: tb.due_date < today ? "overdue" : "tb-date" }, tbUS(tbFromIso(tb.due_date)) + (tb.due_date < today ? " (overdue)" : "")),
      " · " + tb.customer_name + " · follow-up to ", h("a", { href: "#/tickets/" + enc(tb.ticket_id) }, "#" + tb.ticket_number),
      tb.notes ? h("div", { class: "muted small" }, tb.notes.length > 100 ? tb.notes.slice(0, 99) + "…" : tb.notes) : null)))
      : h("p", { class: "muted" }, "No touch bases due in the next 14 days."),
    h("a", { class: "btn", href: "#/calendar" }, "Open the calendar"));
}
