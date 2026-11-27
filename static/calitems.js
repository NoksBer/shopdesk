"use strict";
// Manual calendar items (reminders / appointments that are not tickets). Loaded before app.js; uses tbModal() from
// touchbase.js plus h(), api(), can(), enc from app.js and common.js. The calendar screen lives in board.js.

const calTimeText = (t) => {                                        // "14:30" -> "2:30 PM"
  if (!t) return "";
  const [hh, mm] = t.split(":").map(Number);
  return ((hh + 11) % 12 + 1) + ":" + String(mm).padStart(2, "0") + " " + (hh < 12 ? "AM" : "PM");
};

// Add (item = null) or edit one item. `date` pre-fills the date box with the day selected on the calendar.
function calendarItemDialog({ item = null, date = "", onSaved = () => {} }) {
  const title = h("input", { maxlength: 120, required: true, placeholder: eg("calendar_item", "e.g. Pick up parts order"), value: item ? item.title : "" });
  const day = h("input", { type: "date", value: item ? item.item_date : date });
  const when = h("input", { type: "time", value: item ? item.item_time : "" });
  const notes = h("textarea", { rows: "3", maxlength: 1000 }, item ? item.notes : "");
  const err = h("div", { class: "error", role: "alert" });
  const save = h("button", { type: "button", class: "primary" }, item ? "Save" : "Add to calendar");
  const dlg = tbModal(h("h2", null, item ? "Edit calendar item" : "New calendar item"),
    h("label", null, "What"), title, h("label", null, "Date"), day,
    h("label", null, "Time (optional)"), when, h("label", null, "Notes"), notes, err,
    h("div", { class: "form-actions" }, save, " ", h("button", { type: "button", onclick: () => dlg.close() }, "Cancel")));
  save.addEventListener("click", async () => {
    err.textContent = "";
    if (!title.value.trim()) { err.textContent = "Enter a title."; return; }
    if (!day.value) { err.textContent = "Choose a date."; return; }
    save.disabled = true;
    try {
      const body = { title: title.value, item_date: day.value, item_time: when.value, notes: notes.value };
      const r = item ? await api("PUT", "/api/calendar-items/" + enc(item.id), body) : await api("POST", "/api/calendar-items", body);
      dlg.close();
      onSaved(r.item);
    } catch (e) { err.textContent = e.message; save.disabled = false; }
  });
  return dlg;
}

function calendarItemDetail(item, onChange) {
  const dlg = tbModal(h("h2", null, item.title),
    h("p", { class: "muted" }, item.item_date + (item.item_time ? " · " + calTimeText(item.item_time) : " · all day")),
    item.notes ? h("p", { class: "note-body" }, item.notes) : null,
    h("p", { class: "muted small" }, "Added by " + (item.created_by_name || "unknown")),
    h("div", { class: "form-actions" },
      h("button", { type: "button", autofocus: true, onclick: () => dlg.close() }, "Close"),
      can("tickets.write") ? [" ",
        h("button", { type: "button", onclick: () => { dlg.close(); calendarItemDialog({ item, onSaved: onChange }); } }, "Edit"),
        " ", h("button", { type: "button", onclick: async () => {
          if (!confirm("Delete \"" + item.title + "\" from the calendar?")) return;
          try { await api("DELETE", "/api/calendar-items/" + enc(item.id)); dlg.close(); onChange(); } catch (e) { alert(e.message); }
        } }, "Delete")] : null));
  return dlg;
}
