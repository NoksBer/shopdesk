"""Customer touch bases: schedule, reschedule, cancel and list. The follow-up ticket itself is created by the background
job in touchjob.py. Visibility follows the originating ticket (technicians only see touch bases of tickets assigned to
them); scheduling follows tickets.write, the permission needed to close a ticket."""
import sqlite3
from datetime import date, datetime, timedelta, timezone

from ..audit import audit
from ..db import tx
from ..util import ApiError, clean_date, clean_text, new_id, now_iso
from .tickets import _RESTRICT, _completed, _history, _load, _query_date, _restrict_params

SELECT_COLS = """
SELECT b.id, b.ticket_id, t.number AS ticket_number, t.title AS ticket_title, b.customer_id, c.name AS customer_name,
       b.due_date, b.notes, b.status, b.created_ticket_id, b.scheduled_by, u.name AS scheduled_by_name, b.scheduled_at
FROM touch_bases b
JOIN tickets t ON t.id = b.ticket_id
JOIN customers c ON c.id = b.customer_id
LEFT JOIN users u ON u.id = b.scheduled_by
"""

RANGE_SQL = SELECT_COLS + "WHERE b.status = 'scheduled' AND " + _RESTRICT + """
  AND b.due_date >= :from_date AND b.due_date <= :to_date
ORDER BY b.due_date, b.id
LIMIT 500
"""
ONE_SQL = SELECT_COLS + "WHERE b.id = :id AND " + _RESTRICT
FOR_TICKET_SQL = SELECT_COLS + "WHERE b.ticket_id = :ticket AND b.status = 'scheduled' AND " + _RESTRICT


def register(router):
    router.add("GET", "/api/touch-bases", list_range, "tickets.read")
    router.add("PUT", "/api/touch-bases/<id>", reschedule, "tickets.write")
    router.add("POST", "/api/touch-bases/<id>/cancel", cancel, "tickets.write")
    router.add("GET", "/api/tickets/<id>/touch-base", for_ticket, "tickets.read")
    router.add("POST", "/api/tickets/<id>/touch-base", create, "tickets.write")


def _today():
    return datetime.now(timezone.utc).date()


def _due(body):
    value = clean_date(body, "due_date")
    if value is None:
        raise ApiError(400, "Choose the date for the touch base")
    d, today = date.fromisoformat(value), _today()
    if d < today - timedelta(days=1):                  # one day of slack for time zones
        raise ApiError(400, "The touch base date cannot be in the past")
    if d > today + timedelta(days=3650):
        raise ApiError(400, "The touch base date is too far away")
    return value


def _one(ctx, touch_base_id):
    params = {"id": touch_base_id}
    params.update(_restrict_params(ctx))
    row = ctx.db.execute(ONE_SQL, params).fetchone()
    if row is None:
        raise ApiError(404, "Not found")               # also for touch bases of tickets the user may not see
    return row


def list_range(ctx):
    start, end = _query_date(ctx.req, "from"), _query_date(ctx.req, "to")
    if start is None or end is None or end < start:
        raise ApiError(400, "from and to must be dates (YYYY-MM-DD)")
    if (date.fromisoformat(end) - date.fromisoformat(start)).days > 62:
        raise ApiError(400, "Choose a range of at most 62 days")
    params = {"from_date": start, "to_date": end}
    params.update(_restrict_params(ctx))
    return {"touch_bases": [dict(r) for r in ctx.db.execute(RANGE_SQL, params)]}


def upcoming(ctx):
    """Dashboard: scheduled touch bases that are overdue or due within 14 days, oldest first."""
    params = {"from_date": "0000-01-01", "to_date": (_today() + timedelta(days=14)).isoformat()}
    params.update(_restrict_params(ctx))
    today = _today().isoformat()
    rows = [dict(r, overdue=r["due_date"] < today) for r in ctx.db.execute(RANGE_SQL, params)]
    return rows[:30]


def for_ticket(ctx):
    ticket = _load(ctx, ctx.params["id"])
    params = {"ticket": ticket["id"]}
    params.update(_restrict_params(ctx))
    row = ctx.db.execute(FOR_TICKET_SQL, params).fetchone()
    return {"touch_base": dict(row) if row else None}


def create(ctx):
    body = ctx.req.json()
    db = ctx.db
    ticket = _load(ctx, ctx.params["id"])
    if not _completed(ticket["status_closed"], ticket["status_name"]):
        raise ApiError(409, "A touch base can be scheduled once the ticket is completed")
    due = _due(body)
    notes = clean_text(body, "notes", max_len=1000)
    cust = db.execute("SELECT archived_at FROM customers WHERE id=?", (ticket["customer_id"],)).fetchone()
    if cust is None or cust["archived_at"] is not None:
        raise ApiError(400, "That customer is archived")
    tbid = new_id()
    try:
        with tx(db):
            db.execute("INSERT INTO touch_bases(id, ticket_id, customer_id, due_date, notes, status, scheduled_by, "
                       "scheduled_at) VALUES(?,?,?,?,?,'scheduled',?,?)",
                       (tbid, ticket["id"], ticket["customer_id"], due, notes, ctx.user.id, now_iso()))
            _history(db, ticket["id"], ctx.user.id, "touch_base", {"action": "scheduled", "due_date": due})
            audit(db, ctx.user.id, "touchbase.scheduled", "ticket", ticket["id"],
                  {"touch_base_id": tbid, "due_date": due}, ctx.req.ip)
    except sqlite3.IntegrityError:
        raise ApiError(409, "This ticket already has a scheduled touch base")
    return 201, {"touch_base": dict(_one(ctx, tbid))}


def reschedule(ctx):
    body = ctx.req.json()
    db = ctx.db
    tb = _one(ctx, ctx.params["id"])
    due = _due(body)
    notes = clean_text(body, "notes", max_len=1000) if "notes" in body else tb["notes"]
    with tx(db):
        cur = db.execute("UPDATE touch_bases SET due_date=?, notes=? WHERE id=? AND status='scheduled'",
                         (due, notes, tb["id"]))
        if cur.rowcount == 0:
            raise ApiError(409, "This touch base is no longer scheduled")
        _history(db, tb["ticket_id"], ctx.user.id, "touch_base", {"action": "rescheduled", "due_date": due})
        audit(db, ctx.user.id, "touchbase.rescheduled", "ticket", tb["ticket_id"],
              {"touch_base_id": tb["id"], "due_date": due}, ctx.req.ip)
    return {"touch_base": dict(_one(ctx, tb["id"]))}


def cancel(ctx):
    db = ctx.db
    tb = _one(ctx, ctx.params["id"])
    with tx(db):
        cur = db.execute("UPDATE touch_bases SET status='cancelled' WHERE id=? AND status='scheduled'", (tb["id"],))
        if cur.rowcount == 0:
            raise ApiError(409, "This touch base is no longer scheduled")
        _history(db, tb["ticket_id"], ctx.user.id, "touch_base", {"action": "cancelled"})
        audit(db, ctx.user.id, "touchbase.cancelled", "ticket", tb["ticket_id"], {"touch_base_id": tb["id"]}, ctx.req.ip)
    return {"ok": True}
