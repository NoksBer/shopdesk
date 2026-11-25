"use strict";
// Kanban board and due-date calendar. Cards drag with mouse or touch (grab the handle on touch screens).

const PRIORITIES = ["low", "normal", "high", "urgent"];

async function boardView() {
  const search = h("input", { type: "search", placeholder: "Filter by number, title or customer", maxlength: 100 });
  const prio = h("select", null, h("option", { value: "" }, "All priorities"), PRIORITIES.map(p => h("option", { value: p }, p)));
  const mine = h("input", { type: "checkbox" });
  const errBox = h("div", { class: "error", role: "alert" });
  const wrap = h("div", { class: "board" });
  const writable = can("tickets.write");
  let data = { statuses: [], tickets: [] };

  async function load() {
    const p = new URLSearchParams({ q: search.value.trim(), priority: prio.value });
    if (mine.checked) p.set("assignee", "me");
    data = await api("GET", "/api/board?" + p.toString());
    draw();
  }
  const reload = () => load().catch(showError(errBox));

  async function moveTicket(id, statusId) {
    const t = data.tickets.find(x => x.id === id);
    const st = data.statuses.find(s => s.id === statusId);
    if (!t || !st || t.status_id === statusId) return;
    const prev = { ...t };
    Object.assign(t, { status_id: st.id, status_name: st.name, status_closed: st.is_closed });
    draw();                                                   // optimistic; undone if the server refuses
    try { await api("POST", "/api/tickets/" + enc(id) + "/status", { status_id: statusId }); errBox.textContent = ""; }
    catch (e) { Object.assign(t, prev); draw(); errBox.textContent = "Could not move #" + t.number + ": " + e.message; }
  }

  async function setPriority(t, priority) {
    const prev = t.priority;
    t.priority = priority; draw();
    try { await api("POST", "/api/tickets/" + enc(t.id) + "/priority", { priority }); errBox.textContent = ""; }
    catch (e) { t.priority = prev; draw(); errBox.textContent = "Could not change priority: " + e.message; }
  }

  function card(t, cols, colIndex) {
    const overdue = t.due_date && t.due_date < localToday() && !t.status_closed;
    const arrow = (label, target) => h("button", { type: "button", class: "mini", "aria-label": label + " (" + (target ? target.name : "") + ")",
      disabled: !target || target.retired, onclick: () => moveTicket(t.id, target.id) }, label === "Move left" ? "◀" : "▶");
    const el = h("div", { class: "kcard prio-b-" + t.priority, "data-id": t.id },
      h("div", { class: "kcard-top" },
        h("a", { href: "#/tickets/" + enc(t.id), class: "kcard-num" }, "#" + t.number),
        badge(t.priority, "badge-" + t.priority),
        writable ? h("span", { class: "grip", title: "Drag to another column", "aria-hidden": "true" }, "⠿") : null),
      h("a", { href: "#/tickets/" + enc(t.id), class: "kcard-title" }, t.title),
      h("div", { class: "muted small" }, t.customer_name),
      h("div", { class: "kcard-meta small" },
        t.due_date ? h("span", { class: overdue ? "overdue" : "muted" }, "Due " + t.due_date) : null,
        t.assignee_names ? h("span", { class: "muted" }, t.assignee_names) : null),
      writable ? h("div", { class: "kcard-foot" },
        arrow("Move left", cols[colIndex - 1]),
        h("select", { class: "mini-select", "aria-label": "Priority", onchange: (e) => setPriority(t, e.target.value) },
          PRIORITIES.map(p => h("option", { value: p, selected: p === t.priority }, p))),
        arrow("Move right", cols[colIndex + 1])) : null);
    if (writable) enableDrag(el, t.id);
    return el;
  }

  function enableDrag(el, id) {
    el.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || e.target.closest("a,button,select,input")) return;
      if (e.pointerType !== "mouse" && !e.target.closest(".grip")) return;     // touch: only the handle drags, so the page can still scroll
      const rect = el.getBoundingClientRect();
      const ox = e.clientX - rect.left, oy = e.clientY - rect.top, sx = e.clientX, sy = e.clientY;
      let dragging = false, ghost = null, over = null;
      const move = (ev) => {
        if (!dragging) {
          if (Math.hypot(ev.clientX - sx, ev.clientY - sy) < 6) return;
          dragging = true;
          ghost = el.cloneNode(true);
          ghost.classList.add("ghost");
          ghost.style.width = rect.width + "px";
          document.body.append(ghost);
          el.classList.add("dragging");
        }
        ghost.style.left = (ev.clientX - ox) + "px";
        ghost.style.top = (ev.clientY - oy) + "px";
        const wr = wrap.getBoundingClientRect();                                // scroll sideways near the edges
        if (ev.clientX < wr.left + 40) wrap.scrollLeft -= 14;
        else if (ev.clientX > wr.right - 40) wrap.scrollLeft += 14;
        const under = document.elementFromPoint(ev.clientX, ev.clientY);
        const col = under && under.closest(".kcol[data-drop='1']");
        if (col !== over) { if (over) over.classList.remove("over"); if (col) col.classList.add("over"); over = col; }
        ev.preventDefault();
      };
      const end = (ev) => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", end);
        window.removeEventListener("pointercancel", end);
        if (ghost) ghost.remove();
        el.classList.remove("dragging");
        if (over) over.classList.remove("over");
        if (dragging && over && ev.type === "pointerup") moveTicket(id, over.dataset.id);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", end);
      window.addEventListener("pointercancel", end);
    });
  }

  function draw() {
    const cols = data.statuses.map(s => ({ id: s.id, name: s.name, closed: !!s.is_closed, tickets: [] }));
    const byId = new Map(cols.map(c => [c.id, c]));
    for (const t of data.tickets) {
      let c = byId.get(t.status_id);
      if (!c) {                                                                 // ticket sits in a status that was removed from the workflow
        c = { id: t.status_id, name: t.status_name + " (retired)", closed: !!t.status_closed, tickets: [], retired: true };
        byId.set(c.id, c); cols.push(c);
      }
      c.tickets.push(t);
    }
    wrap.style.setProperty("--cols", String(Math.max(1, cols.length)));        // every status fits on screen side by side
    wrap.replaceChildren(...cols.map((c, i) => h("section", { class: "kcol" + (c.closed ? " closed" : ""), "data-id": c.id, "data-drop": c.retired ? "0" : "1" },
      h("header", null, h("strong", null, c.name), h("span", { class: "count" }, String(c.tickets.length))),
      h("div", { class: "kcol-body" }, c.tickets.length ? c.tickets.map(t => card(t, cols, i)) : h("p", { class: "muted small" }, "Nothing here")))));
  }

  let timer;
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(reload, 250); });
  prio.addEventListener("change", reload);
  mine.addEventListener("change", reload);
  await load();
  const bar = h("div", { class: "filters" }, search, prio, h("label", { class: "inline" }, mine, " Mine"),
    can("tickets.write") && can("customers.read") ? h("a", { class: "btn primary", href: "#/tickets/new" }, "+ New ticket") : null);
  const intro = (writable ? "Drag a card to another column to change its status (on a phone, hold ⠿ or use the arrows). " : "") +
    "Closed tickets stay for " + data.closed_days + " days.";
  return [h("div", { class: "board-bar" }, h("h2", null, "Board"), bar, h("span", { class: "muted small" }, intro)), errBox, wrap];
}

// ------------------------------------------------------------ calendar
let calState = null;

async function calendarView() {
  const now = new Date();
  if (!calState) calState = { y: now.getFullYear(), m: now.getMonth(), selected: localToday() };
  const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  const title = h("h2");
  const grid = h("div", { class: "cal-grid" });
  const agenda = h("div", { class: "cal-agenda" });
  const errBox = h("div", { class: "error", role: "alert" });
  let byDay = {};

  async function load() {
    const first = new Date(calState.y, calState.m, 1);
    const start = new Date(calState.y, calState.m, 1 - first.getDay());
    const days = [];
    for (let i = 0; i < 42; i++) days.push(new Date(start.getFullYear(), start.getMonth(), start.getDate() + i));
    const r = await api("GET", "/api/calendar?from=" + ymd(days[0]) + "&to=" + ymd(days[41]));
    byDay = {};
    for (const t of r.tickets) (byDay[t.due_date] = byDay[t.due_date] || []).push(t);
    title.textContent = MONTHS[calState.m] + " " + calState.y;
    const todayStr = localToday();
    grid.replaceChildren(
      ...["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].map(d => h("div", { class: "cal-head" }, d)),
      ...days.map(d => {
        const key = ymd(d), list = byDay[key] || [];
        const open = list.filter(t => !t.status_closed).length;
        return h("div", { class: "cal-day" + (d.getMonth() !== calState.m ? " other" : "") + (key === todayStr ? " today" : "") +
            (key === calState.selected ? " selected" : ""), role: "button", tabindex: "0", "aria-label": key + ", " + list.length + " due",
          onclick: () => { calState.selected = key; drawSelection(); },
          onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); calState.selected = key; drawSelection(); } } },
          h("div", { class: "cal-num" }, String(d.getDate())),
          list.slice(0, 3).map(t => h("a", { class: "cal-chip prio-b-" + t.priority + (t.status_closed ? " done" : (key < todayStr ? " late" : "")),
            href: "#/tickets/" + enc(t.id), title: "#" + t.number + " " + t.title + " (" + t.customer_name + ")",
            onclick: (e) => e.stopPropagation() }, "#" + t.number + " " + t.title)),
          list.length > 3 ? h("div", { class: "cal-more" }, "+" + (list.length - 3) + " more") : null,
          list.length ? h("div", { class: "cal-count" + (open && key < todayStr ? " late" : "") }, String(list.length)) : null);
      }));
    drawSelection();
  }

  function drawSelection() {
    for (const el of grid.querySelectorAll(".cal-day")) el.classList.toggle("selected", el.getAttribute("aria-label").startsWith(calState.selected));
    const list = byDay[calState.selected] || [];
    agenda.replaceChildren(h("h3", null, calState.selected + (calState.selected === localToday() ? " (today)" : "")),
      list.length ? h("div", { class: "table-wrap" }, h("table", null,
        h("thead", null, h("tr", null, ["#", "Title", "Customer", "Status", "Priority"].map(t => h("th", null, t)))),
        h("tbody", null, list.map(t => h("tr", null,
          h("td", null, h("a", { href: "#/tickets/" + enc(t.id) }, t.number)), h("td", null, t.title), h("td", null, t.customer_name),
          h("td", null, t.status_name), h("td", { class: "prio-" + t.priority }, t.priority))))))
        : h("p", { class: "muted" }, "No jobs due this day."));
  }

  const go = (delta) => { const d = new Date(calState.y, calState.m + delta, 1); calState.y = d.getFullYear(); calState.m = d.getMonth(); load().catch(showError(errBox)); };
  const bar = h("div", { class: "cal-bar" },
    h("button", { type: "button", onclick: () => go(-1), "aria-label": "Previous month" }, "‹"),
    title,
    h("button", { type: "button", onclick: () => go(1), "aria-label": "Next month" }, "›"),
    h("button", { type: "button", onclick: () => { const n = new Date(); calState = { y: n.getFullYear(), m: n.getMonth(), selected: localToday() }; load().catch(showError(errBox)); } }, "Today"));
  await load();
  return [h("section", { class: "card" }, bar, errBox, h("p", { class: "muted small" }, "Job due dates. Red means overdue and still open."), grid), h("section", { class: "card" }, agenda)];
}
