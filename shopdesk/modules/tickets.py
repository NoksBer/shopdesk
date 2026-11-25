"""Work orders (tickets): numbering, configurable statuses, assignment, and an append-only history."""
import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

from .. import timekeeping as tk
from ..audit import audit
from ..db import tx
from ..util import ApiError, clean_date, clean_text, like_pattern, new_id, now_iso

PRIORITIES = ("low", "normal", "high", "urgent")
PREFIX_RE = re.compile(r"^[A-Za-z0-9-]{0,10}$")

_RESTRICT = "(:restrict = 0 OR EXISTS (SELECT 1 FROM ticket_assignees ra WHERE ra.ticket_id = t.id AND ra.user_id = :uid))"

_FROM = """
FROM tickets t
JOIN customers c ON c.id = t.customer_id
JOIN ticket_statuses s ON s.id = t.status_id
"""

_FILTER = """
WHERE """ + _RESTRICT + """
  AND (:status_id = '' OR t.status_id = :status_id)
  AND (:priority = '' OR t.priority = :priority)
  AND (:cust = '' OR t.customer_id = :cust)
  AND (:open = -1 OR s.is_closed = 1 - :open)
  AND (:overdue = 0 OR (t.due_date IS NOT NULL AND t.due_date < :today AND s.is_closed = 0))
  AND (:assignee = '' OR EXISTS (SELECT 1 FROM ticket_assignees fa WHERE fa.ticket_id = t.id AND fa.user_id = :assignee))
  AND (:q = '' OR t.number LIKE :pat ESCAPE '\\' OR t.title LIKE :pat ESCAPE '\\'
       OR t.symptoms LIKE :pat ESCAPE '\\' OR c.name LIKE :pat ESCAPE '\\'
       OR EXISTS (SELECT 1 FROM ticket_equipment te JOIN equipment e ON e.id = te.equipment_id
                  WHERE te.ticket_id = t.id AND (e.name LIKE :pat ESCAPE '\\' OR e.serial_number LIKE :pat ESCAPE '\\')))
"""

LIST_SQL = ("""
SELECT t.id, t.number, t.title, t.priority, t.due_date, t.customer_id, c.name AS customer_name,
       t.status_id, s.name AS status_name, s.is_closed AS status_closed, t.created_at, t.updated_at, t.closed_at,
       (SELECT group_concat(u.name, ', ') FROM ticket_assignees a JOIN users u ON u.id = a.user_id
        WHERE a.ticket_id = t.id) AS assignee_names
""" + _FROM + _FILTER + """
ORDER BY CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
         COALESCE(t.due_date, '9999-12-31'), t.created_at DESC, t.id
LIMIT :limit OFFSET :offset
""")

COUNT_SQL = "SELECT COUNT(*) " + _FROM + _FILTER

# Kanban: every open ticket plus tickets closed recently, so the Done column stays useful but short.
BOARD_SQL = ("""
SELECT t.id, t.number, t.title, t.priority, t.due_date, t.customer_id, c.name AS customer_name,
       t.status_id, s.name AS status_name, s.is_closed AS status_closed, t.closed_at,
       (SELECT group_concat(u.name, ', ') FROM ticket_assignees a JOIN users u ON u.id = a.user_id
        WHERE a.ticket_id = t.id) AS assignee_names
""" + _FROM + "WHERE " + _RESTRICT + """
  AND (s.is_closed = 0 OR t.closed_at >= :since)
  AND (:priority = '' OR t.priority = :priority)
  AND (:assignee = '' OR EXISTS (SELECT 1 FROM ticket_assignees fa WHERE fa.ticket_id = t.id AND fa.user_id = :assignee))
  AND (:q = '' OR t.number LIKE :pat ESCAPE '\\' OR t.title LIKE :pat ESCAPE '\\' OR c.name LIKE :pat ESCAPE '\\')
ORDER BY CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
         COALESCE(t.due_date, '9999-12-31'), t.created_at DESC, t.id
LIMIT 600
""")

CALENDAR_SQL = ("""
SELECT t.id, t.number, t.title, t.priority, t.due_date, c.name AS customer_name,
       s.name AS status_name, s.is_closed AS status_closed
""" + _FROM + "WHERE " + _RESTRICT + """
  AND t.due_date IS NOT NULL AND t.due_date >= :from_date AND t.due_date <= :to_date
ORDER BY t.due_date, CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, t.id
LIMIT 1000
""")

ONE_SQL = ("""
SELECT t.id, t.number, t.title, t.priority, t.due_date, t.symptoms, t.intake_notes, t.customer_id,
       c.name AS customer_name, c.phone AS customer_phone, c.email AS customer_email,
       t.status_id, s.name AS status_name, s.is_closed AS status_closed, t.created_by, t.created_at, t.updated_at,
       t.closed_at, t.closed_by
""" + _FROM + "WHERE t.id = :id AND " + _RESTRICT)


def register(router):
    router.add("GET", "/api/statuses", list_statuses, "tickets.read")
    router.add("PUT", "/api/statuses", put_statuses, "business.manage")
    # numbering must be registered before "/api/tickets/<id>"
    router.add("GET", "/api/tickets/numbering", get_numbering, "tickets.read")
    router.add("PUT", "/api/tickets/numbering", put_numbering, "business.manage")
    router.add("GET", "/api/staff", list_staff, "tickets.assign")
    router.add("GET", "/api/tickets", list_tickets, "tickets.read")
    router.add("POST", "/api/tickets", create_ticket, "tickets.write")
    router.add("GET", "/api/tickets/<id>", get_ticket, "tickets.read")
    router.add("PUT", "/api/tickets/<id>", update_ticket, "tickets.write")
    router.add("POST", "/api/tickets/<id>/status", set_status, "tickets.write")
    router.add("PUT", "/api/tickets/<id>/assignees", set_assignees, "tickets.assign")
    router.add("POST", "/api/tickets/<id>/priority", set_priority, "tickets.write")
    router.add("GET", "/api/board", get_board, "tickets.read")
    router.add("GET", "/api/calendar", get_calendar, "tickets.read")


# ---------------------------------------------------------------- helpers
def _restrict_params(ctx):
    return {"restrict": 0 if ctx.user.can("tickets.read_all") else 1, "uid": ctx.user.id}


def _load(ctx, ticket_id):
    params = {"id": ticket_id}
    params.update(_restrict_params(ctx))
    row = ctx.db.execute(ONE_SQL, params).fetchone()
    if row is None:
        raise ApiError(404, "Not found")      # also returned for tickets the user may not see
    return row


def _history(db, ticket_id, user_id, kind, detail=None):
    db.execute("INSERT INTO ticket_history(ticket_id, ts, user_id, kind, detail) VALUES(?,?,?,?,?)",
               (ticket_id, now_iso(), user_id, kind, json.dumps(detail or {}, sort_keys=True)))


def _id_list(raw, name, limit):
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > limit or not all(isinstance(x, str) for x in raw):
        raise ApiError(400, "%s must be a list of at most %d ids" % (name, limit))
    return list(dict.fromkeys(raw))


def _active_users(db, ids):
    out = []
    for uid in ids:
        row = db.execute("SELECT id, name FROM users WHERE id=? AND active=1", (uid,)).fetchone()
        if row is None:
            raise ApiError(400, "Unknown or inactive user")
        out.append(row)
    return out


def _equipment_for_customer(db, ids, customer_id, already=()):
    for eid in ids:
        row = db.execute("SELECT customer_id, archived_at FROM equipment WHERE id=?", (eid,)).fetchone()
        if row is None or row["customer_id"] != customer_id:
            raise ApiError(400, "Equipment must belong to the ticket's customer")
        if row["archived_at"] is not None and eid not in already:
            raise ApiError(400, "Archived equipment cannot be added")


def _parse(body):
    priority = clean_text(body, "priority", max_len=10, default="normal") or "normal"
    if priority not in PRIORITIES:
        raise ApiError(400, "priority must be one of: " + ", ".join(PRIORITIES))
    return {
        "title": clean_text(body, "title", max_len=150, required=True),
        "priority": priority,
        "due_date": clean_date(body, "due_date"),
        "symptoms": clean_text(body, "symptoms", max_len=2000),
        "intake_notes": clean_text(body, "intake_notes", max_len=2000),
    }


def _detail(ctx, row):
    db = ctx.db
    tid = row["id"]
    equipment = [dict(r) for r in db.execute(
        "SELECT e.id, e.name, e.serial_number FROM ticket_equipment te JOIN equipment e ON e.id = te.equipment_id "
        "WHERE te.ticket_id=? ORDER BY e.name", (tid,))]
    assignees = [dict(r) for r in db.execute(
        "SELECT u.id, u.name FROM ticket_assignees a JOIN users u ON u.id = a.user_id WHERE a.ticket_id=? "
        "ORDER BY u.name", (tid,))]
    history = []
    for r in db.execute(
            "SELECT h.id, h.ts, h.user_id, u.name AS user_name, h.kind, h.detail FROM ticket_history h "
            "LEFT JOIN users u ON u.id = h.user_id WHERE h.ticket_id=? ORDER BY h.id", (tid,)):
        item = dict(r)
        item["detail"] = json.loads(item["detail"])
        history.append(item)
    out = dict(row)
    out.update({"equipment": equipment, "assignees": assignees, "history": history})
    return out


# ---------------------------------------------------------------- statuses and numbering
def _status_rows(db):
    return [dict(r) for r in db.execute(
        "SELECT id, name, customer_label, is_closed, sort_order FROM ticket_statuses WHERE active=1 "
        "ORDER BY sort_order, name")]


def list_statuses(ctx):
    return {"statuses": _status_rows(ctx.db)}


def put_statuses(ctx):
    raw = ctx.req.json().get("statuses")
    if not isinstance(raw, list) or not 2 <= len(raw) <= 30:
        raise ApiError(400, "statuses must be a list of 2 to 30 items")
    items, seen_names = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise ApiError(400, "each status must be an object")
        name = clean_text(item, "name", max_len=60, required=True)
        label = clean_text(item, "customer_label", max_len=80)
        closed = item.get("is_closed", False)
        sid = item.get("id")
        if not isinstance(closed, bool) or (sid is not None and not isinstance(sid, str)):
            raise ApiError(400, "invalid status entry")
        if name.lower() in seen_names:
            raise ApiError(400, "duplicate status name: " + name)
        seen_names.add(name.lower())
        items.append({"id": sid, "name": name, "label": label, "closed": 1 if closed else 0})
    if not any(i["closed"] for i in items) or all(i["closed"] for i in items):
        raise ApiError(400, "Keep at least one open and one closed status")
    try:
        with tx(ctx.db):
            existing = {r["id"] for r in ctx.db.execute("SELECT id FROM ticket_statuses")}
            keep = set()
            for order, item in enumerate(items):
                if item["id"] is not None:
                    if item["id"] not in existing:
                        raise ApiError(400, "Unknown status id")
                    ctx.db.execute(
                        "UPDATE ticket_statuses SET name=?, customer_label=?, is_closed=?, sort_order=?, active=1 "
                        "WHERE id=?", (item["name"], item["label"], item["closed"], order, item["id"]))
                    keep.add(item["id"])
                else:
                    new = new_id()
                    ctx.db.execute(
                        "INSERT INTO ticket_statuses(id, name, sort_order, is_closed, customer_label) VALUES(?,?,?,?,?)",
                        (new, item["name"], order, item["closed"], item["label"]))
                    keep.add(new)
            for sid in existing - keep:
                ctx.db.execute("UPDATE ticket_statuses SET active=0 WHERE id=?", (sid,))
            audit(ctx.db, ctx.user.id, "settings.statuses", "settings", "ticket_statuses",
                  {"count": len(items)}, ctx.req.ip)
    except sqlite3.IntegrityError:
        raise ApiError(409, "A status with that name already exists (deactivated statuses keep their names).")
    return {"statuses": _status_rows(ctx.db)}


def get_numbering(ctx):
    row = ctx.db.execute("SELECT prefix, next_number FROM ticket_numbering WHERE id=1").fetchone()
    return dict(row)


def put_numbering(ctx):
    body = ctx.req.json()
    prefix = clean_text(body, "prefix", max_len=10)
    nxt = body.get("next_number")
    if not PREFIX_RE.match(prefix):
        raise ApiError(400, "Prefix may contain letters, digits and dashes (max 10)")
    if not isinstance(nxt, int) or isinstance(nxt, bool) or not 1 <= nxt <= 999999999:
        raise ApiError(400, "next_number must be a positive whole number")
    with tx(ctx.db):
        ctx.db.execute("UPDATE ticket_numbering SET prefix=?, next_number=? WHERE id=1", (prefix, nxt))
        audit(ctx.db, ctx.user.id, "settings.ticket_numbering", "settings", "ticket_numbering", ip=ctx.req.ip)
    return get_numbering(ctx)


def _allocate_number(db):
    """Call inside a transaction. Skips numbers already taken so numbering can never collide."""
    row = db.execute("SELECT prefix, next_number FROM ticket_numbering WHERE id=1").fetchone()
    n = row["next_number"]
    while db.execute("SELECT 1 FROM tickets WHERE number=?", (row["prefix"] + str(n),)).fetchone():
        n += 1
    db.execute("UPDATE ticket_numbering SET next_number=? WHERE id=1", (n + 1,))
    return row["prefix"] + str(n)


# ---------------------------------------------------------------- tickets
def list_staff(ctx):
    """Active staff that can be assigned to tickets (available to anyone allowed to assign)."""
    rows = ctx.db.execute("SELECT u.id, u.name, r.label AS role_label FROM users u JOIN roles r ON r.id = u.role_id "
                          "WHERE u.active=1 ORDER BY u.name").fetchall()
    return {"staff": [dict(r) for r in rows]}


def list_tickets(ctx):
    req = ctx.req

    def one(name, default=""):
        return (req.query.get(name, [default])[0] or default).strip()

    q = one("q")[:100]
    open_filter = {"1": 1, "0": 0}.get(one("open"), -1)
    assignee = one("assignee")[:64]
    if assignee == "me":
        assignee = ctx.user.id
    params = {
        "status_id": one("status_id")[:64], "priority": one("priority")[:10], "cust": one("customer_id")[:64],
        "open": open_filter, "overdue": 1 if one("overdue") == "1" else 0, "today": date.today().isoformat(),
        "assignee": assignee, "q": q, "pat": like_pattern(q),
    }
    params.update(_restrict_params(ctx))
    page = dict(params, limit=req.query_int("limit", 50, 1, 200), offset=req.query_int("offset", 0, 0, 1000000))
    rows = ctx.db.execute(LIST_SQL, page).fetchall()
    total = ctx.db.execute(COUNT_SQL, params).fetchone()[0]
    return {"tickets": [dict(r) for r in rows], "total": total}


def create_ticket(ctx):
    body = ctx.req.json()
    db = ctx.db
    customer_id = body.get("customer_id")
    cust = db.execute("SELECT id, archived_at FROM customers WHERE id=?",
                      (customer_id if isinstance(customer_id, str) else "",)).fetchone()
    if cust is None:
        raise ApiError(404, "Customer not found")
    if cust["archived_at"] is not None:
        raise ApiError(400, "That customer is archived")
    data = _parse(body)
    equipment_ids = _id_list(body.get("equipment_ids"), "equipment_ids", 20)
    _equipment_for_customer(db, equipment_ids, customer_id)
    assignee_ids = _id_list(body.get("assignee_ids"), "assignee_ids", 10)
    if assignee_ids and not ctx.user.can("tickets.assign"):
        raise ApiError(403, "You do not have permission to assign technicians")
    if not ctx.user.can("tickets.read_all") and ctx.user.id not in assignee_ids:
        assignee_ids.append(ctx.user.id)        # so the creator can still see their own ticket
    users = _active_users(db, assignee_ids)
    status = db.execute("SELECT id, name FROM ticket_statuses WHERE active=1 ORDER BY sort_order LIMIT 1").fetchone()
    tid, ts = new_id(), now_iso()
    with tx(db):
        number = _allocate_number(db)
        db.execute(
            "INSERT INTO tickets(id, number, customer_id, status_id, title, priority, due_date, symptoms, intake_notes, "
            "created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, number, customer_id, status["id"], data["title"], data["priority"], data["due_date"],
             data["symptoms"], data["intake_notes"], ctx.user.id, ts, ts))
        for eid in equipment_ids:
            db.execute("INSERT INTO ticket_equipment(ticket_id, equipment_id) VALUES(?,?)", (tid, eid))
        for u in users:
            db.execute("INSERT INTO ticket_assignees(ticket_id, user_id, assigned_at, assigned_by) VALUES(?,?,?,?)",
                       (tid, u["id"], ts, ctx.user.id))
        _history(db, tid, ctx.user.id, "created", {"number": number, "status": status["name"]})
        for u in users:
            _history(db, tid, ctx.user.id, "assigned", {"user_id": u["id"], "name": u["name"]})
        audit(db, ctx.user.id, "ticket.created", "ticket", tid, {"number": number}, ctx.req.ip)
    return 201, _detail(ctx, _load(ctx, tid))


def get_ticket(ctx):
    return _detail(ctx, _load(ctx, ctx.params["id"]))


def update_ticket(ctx):
    body = ctx.req.json()
    db = ctx.db
    old = _load(ctx, ctx.params["id"])
    data = _parse(body)
    current = [r["equipment_id"] for r in db.execute(
        "SELECT equipment_id FROM ticket_equipment WHERE ticket_id=?", (old["id"],))]
    wanted = _id_list(body["equipment_ids"], "equipment_ids", 20) if "equipment_ids" in body else current
    _equipment_for_customer(db, wanted, old["customer_id"], already=current)
    ts = now_iso()
    changed = [k for k in ("title", "symptoms", "intake_notes") if old[k] != data[k]]
    with tx(db):
        db.execute("UPDATE tickets SET title=?, priority=?, due_date=?, symptoms=?, intake_notes=?, updated_at=? "
                   "WHERE id=?", (data["title"], data["priority"], data["due_date"], data["symptoms"],
                                  data["intake_notes"], ts, old["id"]))
        if old["priority"] != data["priority"]:
            _history(db, old["id"], ctx.user.id, "priority", {"from": old["priority"], "to": data["priority"]})
        if old["due_date"] != data["due_date"]:
            _history(db, old["id"], ctx.user.id, "due_date", {"from": old["due_date"], "to": data["due_date"]})
        added, removed = [e for e in wanted if e not in current], [e for e in current if e not in wanted]
        for eid in added:
            db.execute("INSERT INTO ticket_equipment(ticket_id, equipment_id) VALUES(?,?)", (old["id"], eid))
        for eid in removed:
            db.execute("DELETE FROM ticket_equipment WHERE ticket_id=? AND equipment_id=?", (old["id"], eid))
        if added or removed:
            _history(db, old["id"], ctx.user.id, "equipment", {"added": added, "removed": removed})
        if changed:
            _history(db, old["id"], ctx.user.id, "edited", {"changed": changed})
        audit(db, ctx.user.id, "ticket.updated", "ticket", old["id"], ip=ctx.req.ip)
    return _detail(ctx, _load(ctx, old["id"]))


def set_status(ctx):
    body = ctx.req.json()
    db = ctx.db
    old = _load(ctx, ctx.params["id"])
    sid = body.get("status_id")
    new = db.execute("SELECT id, name, is_closed, active FROM ticket_statuses WHERE id=?",
                     (sid if isinstance(sid, str) else "",)).fetchone()
    if new is None or not new["active"]:
        raise ApiError(400, "Unknown status")
    if new["id"] == old["status_id"]:
        raise ApiError(400, "The ticket is already in that status")
    ts = now_iso()
    was_closed = bool(old["status_closed"])
    with tx(db):
        if new["is_closed"] and not was_closed:
            db.execute("UPDATE tickets SET status_id=?, updated_at=?, closed_at=?, closed_by=? WHERE id=?",
                       (new["id"], ts, ts, ctx.user.id, old["id"]))
        elif not new["is_closed"]:
            db.execute("UPDATE tickets SET status_id=?, updated_at=?, closed_at=NULL, closed_by=NULL WHERE id=?",
                       (new["id"], ts, old["id"]))
        else:
            db.execute("UPDATE tickets SET status_id=?, updated_at=? WHERE id=?", (new["id"], ts, old["id"]))
        _history(db, old["id"], ctx.user.id, "status", {"from": old["status_name"], "to": new["name"]})
        if new["is_closed"] and not was_closed:
            tk.stop_all_on_ticket(db, old["id"], ts, ctx.user.id)      # nobody keeps clocking time on a finished job
        audit(db, ctx.user.id, "ticket.status", "ticket", old["id"], {"to": new["name"]}, ctx.req.ip)
    return _detail(ctx, _load(ctx, old["id"]))


def set_priority(ctx):
    body = ctx.req.json()
    db = ctx.db
    old = _load(ctx, ctx.params["id"])
    priority = body.get("priority")
    if priority not in PRIORITIES:
        raise ApiError(400, "priority must be one of: " + ", ".join(PRIORITIES))
    if priority != old["priority"]:
        with tx(db):
            db.execute("UPDATE tickets SET priority=?, updated_at=? WHERE id=?", (priority, now_iso(), old["id"]))
            _history(db, old["id"], ctx.user.id, "priority", {"from": old["priority"], "to": priority})
            audit(db, ctx.user.id, "ticket.updated", "ticket", old["id"], ip=ctx.req.ip)
    return _detail(ctx, _load(ctx, old["id"]))


def get_board(ctx):
    req = ctx.req

    def one(name):
        return (req.query.get(name, [""])[0] or "").strip()

    q = one("q")[:100]
    priority = one("priority")[:10]
    if priority and priority not in PRIORITIES:
        raise ApiError(400, "Unknown priority")
    assignee = one("assignee")[:64]
    if assignee == "me":
        assignee = ctx.user.id
    since = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {"since": since, "priority": priority, "assignee": assignee, "q": q, "pat": like_pattern(q)}
    params.update(_restrict_params(ctx))
    rows = ctx.db.execute(BOARD_SQL, params).fetchall()
    return {"statuses": _status_rows(ctx.db), "tickets": [dict(r) for r in rows], "closed_days": 14}


def _query_date(req, name):
    value = (req.query.get(name, [""])[0] or "").strip()
    try:
        return date.fromisoformat(value).isoformat() if len(value) == 10 else None
    except ValueError:
        return None


def get_calendar(ctx):
    start, end = _query_date(ctx.req, "from"), _query_date(ctx.req, "to")
    if start is None or end is None or end < start:
        raise ApiError(400, "from and to must be dates (YYYY-MM-DD)")
    if (date.fromisoformat(end) - date.fromisoformat(start)).days > 62:
        raise ApiError(400, "Choose a range of at most 62 days")
    params = {"from_date": start, "to_date": end}
    params.update(_restrict_params(ctx))
    rows = ctx.db.execute(CALENDAR_SQL, params).fetchall()
    return {"tickets": [dict(r) for r in rows], "from": start, "to": end}


def set_assignees(ctx):
    body = ctx.req.json()
    db = ctx.db
    old = _load(ctx, ctx.params["id"])
    wanted = _id_list(body.get("user_ids"), "user_ids", 10)
    users = {u["id"]: u for u in _active_users(db, wanted)}
    current = {r["user_id"]: r["name"] for r in db.execute(
        "SELECT a.user_id, u.name FROM ticket_assignees a JOIN users u ON u.id = a.user_id WHERE a.ticket_id=?",
        (old["id"],))}
    ts = now_iso()
    with tx(db):
        for uid in wanted:
            if uid not in current:
                db.execute("INSERT INTO ticket_assignees(ticket_id, user_id, assigned_at, assigned_by) VALUES(?,?,?,?)",
                           (old["id"], uid, ts, ctx.user.id))
                _history(db, old["id"], ctx.user.id, "assigned", {"user_id": uid, "name": users[uid]["name"]})
        for uid, name in current.items():
            if uid not in users:
                db.execute("DELETE FROM ticket_assignees WHERE ticket_id=? AND user_id=?", (old["id"], uid))
                _history(db, old["id"], ctx.user.id, "unassigned", {"user_id": uid, "name": name})
        db.execute("UPDATE tickets SET updated_at=? WHERE id=?", (ts, old["id"]))
        audit(db, ctx.user.id, "ticket.assigned", "ticket", old["id"], {"count": len(users)}, ctx.req.ip)
    try:
        return _detail(ctx, _load(ctx, old["id"]))
    except ApiError:       # the caller unassigned themselves from a ticket they could only see via assignment
        return {"ok": True}
