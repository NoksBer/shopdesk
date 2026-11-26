"""Manual calendar items: reminders and appointments that are not tickets. Shared by everyone who can see the calendar
(tickets.read); creating, editing and deleting needs tickets.write."""
import re
from datetime import date

from ..audit import audit
from ..db import tx
from ..util import ApiError, clean_date, clean_text, new_id, now_iso
from .tickets import _query_date

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

COLS = ("SELECT i.id, i.title, i.item_date, i.item_time, i.notes, i.created_by, u.name AS created_by_name, i.created_at "
        "FROM calendar_items i LEFT JOIN users u ON u.id = i.created_by ")
RANGE_SQL = COLS + "WHERE i.item_date >= ? AND i.item_date <= ? ORDER BY i.item_date, i.item_time, i.created_at LIMIT 500"
ONE_SQL = COLS + "WHERE i.id = ?"


def register(router):
    router.add("GET", "/api/calendar-items", list_items, "tickets.read")
    router.add("POST", "/api/calendar-items", create_item, "tickets.write")
    router.add("PUT", "/api/calendar-items/<id>", update_item, "tickets.write")
    router.add("DELETE", "/api/calendar-items/<id>", delete_item, "tickets.write")


def _parse(body):
    title = clean_text(body, "title", max_len=120, required=True)
    day = clean_date(body, "item_date")
    if day is None:
        raise ApiError(400, "Choose a date")
    if not 2000 <= date.fromisoformat(day).year <= 2100:
        raise ApiError(400, "That date is out of range")
    when = clean_text(body, "item_time", max_len=5)
    if when and not TIME_RE.match(when):
        raise ApiError(400, "Time must look like 14:30")
    return title, day, when, clean_text(body, "notes", max_len=1000)


def _one(db, item_id):
    row = db.execute(ONE_SQL, (item_id,)).fetchone()
    if row is None:
        raise ApiError(404, "Not found")
    return row


def list_items(ctx):
    start, end = _query_date(ctx.req, "from"), _query_date(ctx.req, "to")
    if start is None or end is None or end < start:
        raise ApiError(400, "from and to must be dates (YYYY-MM-DD)")
    if (date.fromisoformat(end) - date.fromisoformat(start)).days > 62:
        raise ApiError(400, "Choose a range of at most 62 days")
    return {"items": [dict(r) for r in ctx.db.execute(RANGE_SQL, (start, end))]}


def create_item(ctx):
    title, day, when, notes = _parse(ctx.req.json())
    iid, ts = new_id(), now_iso()
    with tx(ctx.db):
        ctx.db.execute("INSERT INTO calendar_items(id, title, item_date, item_time, notes, created_by, created_at, updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?)", (iid, title, day, when, notes, ctx.user.id, ts, ts))
        audit(ctx.db, ctx.user.id, "calendaritem.created", "calendar_item", iid, {"date": day}, ctx.req.ip)
    return 201, {"item": dict(_one(ctx.db, iid))}


def update_item(ctx):
    title, day, when, notes = _parse(ctx.req.json())
    _one(ctx.db, ctx.params["id"])
    with tx(ctx.db):
        ctx.db.execute("UPDATE calendar_items SET title=?, item_date=?, item_time=?, notes=?, updated_at=? WHERE id=?",
                       (title, day, when, notes, now_iso(), ctx.params["id"]))
        audit(ctx.db, ctx.user.id, "calendaritem.updated", "calendar_item", ctx.params["id"], {"date": day}, ctx.req.ip)
    return {"item": dict(_one(ctx.db, ctx.params["id"]))}


def delete_item(ctx):
    _one(ctx.db, ctx.params["id"])
    with tx(ctx.db):
        ctx.db.execute("DELETE FROM calendar_items WHERE id=?", (ctx.params["id"],))
        audit(ctx.db, ctx.user.id, "calendaritem.deleted", "calendar_item", ctx.params["id"], ip=ctx.req.ip)
    return {"ok": True}
