"use strict";
// Time tracking: header clock, the Time card on a ticket, the timesheet page, and "clocked in now" on the dashboard.

const pad2 = (n) => String(n).padStart(2, "0");
const fmtDur = (s) => Math.floor(s / 3600) + ":" + pad2(Math.floor(s / 60) % 60) + ":" + pad2(s % 60);
function fmtHM(s) {
  const m = Math.round(s / 60);
  if (m < 1) return s > 0 ? "<1m" : "0m";
  return m >= 60 ? Math.floor(m / 60) + "h " + pad2(m % 60) + "m" : m + "m";
}
const fmtClock = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "");

// ------------------------------------------------------------ the running timer (header)
let TIMER = null;                // { entry, t0, base }  base = seconds already elapsed when we last asked the server
let timerTick = null;

function setTimer(entry) {
  TIMER = entry ? { entry, t0: Date.now(), base: entry.seconds } : null;
  drawTimer();
}
const timerElapsed = () => (TIMER ? TIMER.base + Math.floor((Date.now() - TIMER.t0) / 1000) : 0);

async function refreshTimer() {
  if (!ME || !can("time.write")) { setTimer(null); return; }
  try { setTimer((await api("GET", "/api/time/running")).entry); } catch (e) { /* header clock is a convenience */ }
}

function drawTimer() {
  const box = document.getElementById("timer-box");
  clearInterval(timerTick);
  if (!box) return;
  if (!TIMER) { box.replaceChildren(); return; }
  const e = TIMER.entry;
  const clock = h("span", { class: "timer-clock" }, fmtDur(timerElapsed()));
  box.replaceChildren(
    h("a", { class: "timer-pill", href: "#/tickets/" + enc(e.ticket_id), title: "Clocked in on #" + e.ticket_number + " " + e.ticket_title },
      h("span", { class: "rec-dot" }), " #" + e.ticket_number + " ", clock),
    h("button", { type: "button", class: "mini", onclick: async () => {
      try { await api("POST", "/api/time/clock-out", {}); setTimer(null); route(); } catch (err) { alert(err.message); }
    } }, "Clock out"));
  timerTick = setInterval(() => {
    if (!document.body.contains(clock)) { clearInterval(timerTick); return; }
    clock.textContent = fmtDur(timerElapsed());
  }, 1000);
}

// ------------------------------------------------------------ ticket "Time" card
async function ticketTimeCard(t) {
  const eid = enc(t.id);
  const d = await api("GET", "/api/tickets/" + eid + "/time");
  const canTrack = can("time.write");
  const msg = h("div", { class: "error", role: "alert" });
  const card = h("section", { class: "card" }, h("h2", null, "Time"));
  const act = (fn) => async () => { msg.textContent = ""; try { await fn(); await refreshTimer(); route(); } catch (e) { msg.textContent = e.message; } };

  // controls
  const mine = d.entries.find(e => e.running && e.user_id === d.me);
  const note = h("input", { maxlength: 200, placeholder: "What are you working on? (optional)", "aria-label": "Note" });
  if (canTrack && !d.ticket_closed) {
    if (mine) {
      const clock = h("span", { class: "big-clock" }, fmtDur(mine.seconds));
      const t0 = Date.now(), base = mine.seconds;
      const tick = setInterval(() => { if (!document.body.contains(clock)) clearInterval(tick); else clock.textContent = fmtDur(base + Math.floor((Date.now() - t0) / 1000)); }, 1000);
      card.append(h("div", { class: "clock-row" }, h("span", { class: "rec-dot" }), clock, h("span", { class: "muted" }, "since " + fmtClock(mine.started_at))),
        field("Note for this session", note),
        h("div", { class: "form-actions" }, h("button", { type: "button", class: "primary", onclick: act(() => api("POST", "/api/time/clock-out", { note: note.value })) }, "Clock out")));
    } else {
      const elsewhere = TIMER && TIMER.entry.ticket_id !== t.id ? TIMER.entry : null;
      card.append(field("Note (optional)", note),
        h("div", { class: "form-actions wrap" },
          h("button", { type: "button", class: "primary", onclick: act(() => api("POST", "/api/tickets/" + eid + "/clock-in", { note: note.value })) },
            elsewhere ? "Switch timer to this ticket" : "Clock in"),
          elsewhere ? h("span", { class: "muted small" }, "You are currently clocked in on #" + elsewhere.ticket_number + ". Switching stops that timer.") : null));
    }
  } else if (d.ticket_closed) {
    card.append(h("p", { class: "muted small" }, "This ticket is closed. Reopen it to clock in. You can still add time below."));
  }

  // totals
  card.append(h("p", null, h("strong", null, "Total " + fmtHM(d.total_seconds)), h("span", { class: "muted" }, " · billable " + fmtHM(d.billable_seconds) +
    (d.by_user.length > 1 ? " · " + d.by_user.map(u => u.user_name + " " + fmtHM(u.seconds)).join(", ") : ""))));

  // entries
  const rows = d.entries.map(e => {
    const allowed = e.user_id === d.me || d.can_manage;
    const when = e.source === "timer" ? fmtClock(e.started_at) + (e.ended_at ? " – " + fmtClock(e.ended_at) : " – now") : "added by hand";
    return h("tr", null, h("td", null, e.work_date), h("td", null, e.user_name),
      h("td", null, when, e.running ? [" ", badge("running", "badge-high")] : null),
      h("td", { class: "num" }, e.running ? fmtDur(e.seconds) : fmtHM(e.seconds)),
      h("td", null, e.note, e.billable ? "" : [" ", badge("non-billable")]),
      h("td", { class: "actions" }, allowed ? [
        h("button", { type: "button", onclick: () => editTimeEntry(e, card, act) }, "Edit"),
        h("button", { type: "button", onclick: act(async () => { if (confirm("Remove this time entry?")) await api("DELETE", "/api/time/" + enc(e.id)); }) }, "Remove")] : null));
  });
  card.append(rows.length ? h("div", { class: "table-wrap" }, h("table", null,
    h("thead", null, h("tr", null, ["Date", "Who", "When", "Time", "Note", ""].map((x, i) => h("th", { class: i === 3 ? "num" : "" }, x)))),
    h("tbody", null, rows))) : h("p", { class: "muted" }, "No time logged yet."));

  // manual entry
  if (canTrack) {
    const date = h("input", { type: "date", value: localToday() });
    const hours = h("input", { type: "number", min: "0", max: "24", step: "1", value: "0", "aria-label": "Hours" });
    const mins = h("input", { type: "number", min: "0", max: "59", step: "1", value: "30", "aria-label": "Minutes" });
    const mnote = h("input", { maxlength: 200, placeholder: "What was done?" });
    const bill = h("input", { type: "checkbox", checked: true });
    card.append(h("details", null, h("summary", null, "Add time by hand"),
      h("div", { class: "grid2" }, field("Date", date), h("div", { class: "grid2" }, field("Hours", hours), field("Minutes", mins))),
      field("Note", mnote), checkRow("Billable", bill),
      h("div", { class: "form-actions" }, h("button", { type: "button", onclick: act(async () => {
        const total = (parseInt(hours.value, 10) || 0) * 60 + (parseInt(mins.value, 10) || 0);
        if (total < 1) throw new Error("Enter at least one minute.");
        await api("POST", "/api/tickets/" + eid + "/time", { work_date: date.value, minutes: total, note: mnote.value, billable: bill.checked });
      }) }, "Add time"))));
  }
  card.append(msg);
  return card;
}

function editTimeEntry(e, card, act) {
  const dlg = h("div", { class: "inline-edit" });
  const date = h("input", { type: "date", value: e.work_date, disabled: e.running });
  const hours = h("input", { type: "number", min: "0", max: "24", step: "1", value: String(Math.floor(e.seconds / 3600)), disabled: e.running, "aria-label": "Hours" });
  const mins = h("input", { type: "number", min: "0", max: "59", step: "1", value: String(Math.floor(e.seconds / 60) % 60), disabled: e.running, "aria-label": "Minutes" });
  const note = h("input", { value: e.note, maxlength: 200 });
  const bill = h("input", { type: "checkbox", checked: e.billable });
  dlg.append(h("h3", null, "Edit time entry" + (e.running ? " (clock out to change the duration)" : "")),
    h("div", { class: "grid2" }, field("Date", date), h("div", { class: "grid2" }, field("Hours", hours), field("Minutes", mins))),
    field("Note", note), checkRow("Billable", bill),
    h("div", { class: "form-actions" },
      h("button", { type: "button", class: "primary", onclick: act(async () => {
        const body = { note: note.value, billable: bill.checked };
        if (!e.running) {
          const total = (parseInt(hours.value, 10) || 0) * 60 + (parseInt(mins.value, 10) || 0);
          if (total < 1) throw new Error("Enter at least one minute.");
          body.minutes = total; body.work_date = date.value;
        }
        await api("PUT", "/api/time/" + enc(e.id), body);
      }) }, "Save"),
      h("button", { type: "button", onclick: () => dlg.remove() }, "Cancel")));
  card.append(dlg);
  dlg.scrollIntoView({ block: "center" });
}

// ------------------------------------------------------------ dashboard card
function clockedInCard(list) {
  return h("section", { class: "card" }, h("h2", null, "Clocked in now"),
    list.length ? h("ul", { class: "history" }, list.map(e => h("li", null, h("span", { class: "rec-dot" }), " ", h("strong", null, e.user_name),
      " on ", h("a", { href: "#/tickets/" + enc(e.ticket_id) }, "#" + e.ticket_number), " " + e.ticket_title,
      h("span", { class: "muted" }, " · " + fmtHM(e.seconds) + (e.seconds > 12 * 3600 ? " (forgot to clock out?)" : "")))))
      : h("p", { class: "muted" }, "Nobody is clocked in."),
    h("a", { class: "btn", href: "#/time" }, "Open timesheets"));
}

// ------------------------------------------------------------ timesheet page
function weekRange(offsetWeeks) {
  const n = new Date();
  const monday = new Date(n.getFullYear(), n.getMonth(), n.getDate() - ((n.getDay() + 6) % 7) + offsetWeeks * 7);
  return [ymd(monday), ymd(new Date(monday.getFullYear(), monday.getMonth(), monday.getDate() + 6))];
}

async function timeView() {
  const manage = can("time.manage");
  const msg = h("div", { class: "error", role: "alert" });
  const out = h("div");
  const preset = h("select", { "aria-label": "Period" }, [["week", "This week"], ["last_week", "Last week"], ["month", "This month"], ["custom", "Custom dates"]]
    .map(([v, l]) => h("option", { value: v }, l)));
  const [a0, b0] = weekRange(0);
  const from = h("input", { type: "date", value: a0, "aria-label": "From" });
  const to = h("input", { type: "date", value: b0, "aria-label": "To" });
  let people = [];
  if (manage && can("users.read")) { try { people = (await api("GET", "/api/users")).users; } catch (e) { people = []; } }
  const who = h("select", { "aria-label": "Person" }, h("option", { value: "" }, "Everyone"), people.map(u => h("option", { value: u.id }, u.name)));

  async function load() {
    msg.textContent = "";
    const p = new URLSearchParams({ from: from.value, to: to.value });
    if (manage && who.value) p.set("user_id", who.value);
    const r = await api("GET", "/api/time/entries?" + p.toString());
    out.replaceChildren(
      h("p", null, h("strong", null, "Total " + fmtHM(r.total_seconds)), h("span", { class: "muted" }, " · billable " + fmtHM(r.billable_seconds))),
      r.by_user.length > 1 || manage ? h("div", { class: "table-wrap" }, h("table", null,
        h("thead", null, h("tr", null, ["Person", "Time", "Billable"].map((x, i) => h("th", { class: i ? "num" : "" }, x)))),
        h("tbody", null, r.by_user.map(u => h("tr", null, h("td", null, u.user_name), h("td", { class: "num" }, fmtHM(u.seconds)), h("td", { class: "num" }, fmtHM(u.billable_seconds))))))) : null,
      r.entries.length ? h("div", { class: "table-wrap" }, h("table", null,
        h("thead", null, h("tr", null, ["Date", "Who", "Ticket", "Note", "Time"].map((x, i) => h("th", { class: i === 4 ? "num" : "" }, x)))),
        h("tbody", null, r.entries.map(e => h("tr", null, h("td", null, e.work_date), h("td", null, e.user_name),
          h("td", null, h("a", { href: "#/tickets/" + enc(e.ticket_id) }, "#" + e.ticket_number), " " + e.ticket_title),
          h("td", null, e.note, e.billable ? "" : [" ", badge("non-billable")], e.running ? [" ", badge("running", "badge-high")] : null),
          h("td", { class: "num" }, e.running ? fmtDur(e.seconds) : fmtHM(e.seconds))))))) : h("p", { class: "muted" }, "No time logged in this period."));
  }
  const reload = () => load().catch(showError(msg));
  preset.addEventListener("change", () => {
    let r = null;
    if (preset.value === "week") r = weekRange(0);
    else if (preset.value === "last_week") r = weekRange(-1);
    else if (preset.value === "month") { const n = new Date(); r = [ymd(new Date(n.getFullYear(), n.getMonth(), 1)), ymd(new Date(n.getFullYear(), n.getMonth() + 1, 0))]; }
    if (r) { from.value = r[0]; to.value = r[1]; }
    reload();
  });
  for (const el of [from, to]) el.addEventListener("change", () => { preset.value = "custom"; reload(); });
  who.addEventListener("change", reload);
  await load();
  return [h("section", { class: "card" }, h("h2", null, manage ? "Timesheets" : "My time"),
    h("p", { class: "muted small" }, "Clock in and out from a ticket page. Time per person is below" + (manage ? "; use the ticket to edit an entry." : ".")),
    h("div", { class: "filters" }, preset, from, to, manage ? who : null), msg, out)];
}
