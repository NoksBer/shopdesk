"""Clock in / clock out and manual time on tickets. One running timer per person: starting another stops the first."""
import json
from datetime import date, datetime, timedelta, timezone

from .. import timekeeping as tk
from ..audit import audit
from ..db import tx
from ..util import ApiError, clean_date, clean_flag, clean_int, clean_text, new_id, now_iso
from .tickets import _history, _load

TICKET_ENTRIES_SQL = tk.ENTRY_SQL + "WHERE e.ticket_id = ? ORDER BY e.work_date DESC, COALESCE(e.started_at, e.created_at) DESC"

RANGE_SQL = tk.ENTRY_SQL + """
WHERE e.work_date >= :date_from AND e.work_date <= :date_to
  AND (:uid = '' OR e.user_id = :uid)
  AND (:ticket = '' OR e.ticket_id = :ticket)
ORDER BY e.work_date DESC, COALESCE(e.started_at, e.created_at) DESC
LIMIT 1000
"""


def register(router):
    router.add("GET", "/api/time/running", get_running, "time.write")
    router.add("POST", "/api/time/clock-out", clock_out, "time.write")
    router.add("GET", "/api/time/entries", list_entries, "time.write")
    router.add("PUT", "/api/time/<id>", update_entry, "time.write")
    router.add("DELETE", "/api/time/<id>", delete_entry, "time.write")
    router.add("POST", "/api/tickets/<id>/clock-in", clock_in, "time.write")
    router.add("GET", "/api/tickets/<id>/time", ticket_time, "tickets.read")
    router.add("POST", "/api/tickets/<id>/time", add_manual, "time.write")


def _entry(db, entry_id):
    row = db.execute(tk.ENTRY_BY_ID_SQL, (entry_id,)).fetchone()
    return tk.present(row, now_iso()) if row else None


def _check_date(value):
    tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    if value > tomorrow:
        raise ApiError(400, "The date cannot be in the future")
    return value


def _totals(entries):
    by_user = {}
    total = billable = 0
    for e in entries:
        total += e["seconds"]
        if e["billable"]:
            billable += e["seconds"]
        u = by_user.setdefault(e["user_id"], {"user_id": e["user_id"], "user_name": e["user_name"], "seconds": 0, "billable_seconds": 0})
        u["seconds"] += e["seconds"]
        if e["billable"]:
            u["billable_seconds"] += e["seconds"]
    return {"total_seconds": total, "billable_seconds": billable,
            "by_user": sorted(by_user.values(), key=lambda u: -u["seconds"])}


def get_running(ctx):
    now = now_iso()
    return {"entry": tk.running_for_user(ctx.db, ctx.user.id), "server_now": now}


def clock_in(ctx):
    body = ctx.req.json()
    db = ctx.db
    ticket = _load(ctx, ctx.params["id"])
    if ticket["status_closed"]:
        raise ApiError(409, "This ticket is closed. Reopen it to track time.")
    note = clean_text(body, "note", max_len=200)
    ts = now_iso()
    switched = None
    with tx(db):
        running = db.execute("SELECT id, ticket_id, started_at FROM time_entries "
                             "WHERE user_id=? AND source='timer' AND ended_at IS NULL", (ctx.user.id,)).fetchone()
        if running:
            if running["ticket_id"] == ticket["id"]:
                raise ApiError(409, "You are already clocked in on this ticket")
            other = db.execute("SELECT number FROM tickets WHERE id=?", (running["ticket_id"],)).fetchone()
            switched = other["number"] if other else None
            tk.stop_entry(db, running, ts, ctx.user.id)
        eid = new_id()
        db.execute("INSERT INTO time_entries(id, ticket_id, user_id, work_date, started_at, note, billable, source, "
                   "created_at, updated_at) VALUES(?,?,?,?,?,?,1,'timer',?,?)",
                   (eid, ticket["id"], ctx.user.id, ts[:10], ts, note, ts, ts))
        _history(db, ticket["id"], ctx.user.id, "time_in")
        audit(db, ctx.user.id, "time.clock_in", "ticket", ticket["id"], ip=ctx.req.ip)
    return {"entry": _entry(db, eid), "switched_from": switched, "server_now": now_iso()}


def clock_out(ctx):
    body = ctx.req.json()
    db = ctx.db
    note = clean_text(body, "note", max_len=200)
    ts = now_iso()
    with tx(db):
        running = db.execute("SELECT id, ticket_id, started_at FROM time_entries "
                             "WHERE user_id=? AND source='timer' AND ended_at IS NULL", (ctx.user.id,)).fetchone()
        if running is None:
            raise ApiError(409, "You are not clocked in")
        seconds = tk.stop_entry(db, running, ts, ctx.user.id, note or None)
        audit(db, ctx.user.id, "time.clock_out", "ticket", running["ticket_id"], {"seconds": seconds}, ctx.req.ip)
    return {"entry": _entry(db, running["id"]), "server_now": now_iso()}


def ticket_time(ctx):
    ticket = _load(ctx, ctx.params["id"])            # also enforces who may see this ticket
    now = now_iso()
    entries = [tk.present(r, now) for r in ctx.db.execute(TICKET_ENTRIES_SQL, (ticket["id"],))]
    out = _totals(entries)
    out.update({"entries": entries, "server_now": now, "can_manage": ctx.user.can("time.manage"), "me": ctx.user.id,
                "ticket_closed": bool(ticket["status_closed"])})
    return out


def add_manual(ctx):
    """Time worked without the timer (forgot to clock in, phone call, work done away from the desk)."""
    body = ctx.req.json()
    db = ctx.db
    ticket = _load(ctx, ctx.params["id"])
    work_date = _check_date(clean_date(body, "work_date") or "") if body.get("work_date") else now_iso()[:10]
    minutes = clean_int(body, "minutes", 1, 1440)
    note = clean_text(body, "note", max_len=200)
    billable = clean_flag(body, "billable", True)
    ts = now_iso()
    eid = new_id()
    with tx(db):
        db.execute("INSERT INTO time_entries(id, ticket_id, user_id, work_date, seconds, note, billable, source, "
                   "created_at, updated_at) VALUES(?,?,?,?,?,?,?,'manual',?,?)",
                   (eid, ticket["id"], ctx.user.id, work_date, minutes * 60, note, billable, ts, ts))
        _history(db, ticket["id"], ctx.user.id, "time_added", {"seconds": minutes * 60})
        audit(db, ctx.user.id, "time.added", "ticket", ticket["id"], {"seconds": minutes * 60}, ctx.req.ip)
    return 201, {"entry": _entry(db, eid)}


def _own_or_manage(ctx, entry_id):
    row = ctx.db.execute("SELECT id, ticket_id, user_id, source, ended_at, seconds, work_date FROM time_entries WHERE id=?",
                         (entry_id,)).fetchone()
    if row is None or (row["user_id"] != ctx.user.id and not ctx.user.can("time.manage")):
        raise ApiError(404, "Not found")
    return row


def update_entry(ctx):
    body = ctx.req.json()
    db = ctx.db
    old = _own_or_manage(ctx, ctx.params["id"])
    running = old["source"] == "timer" and old["ended_at"] is None
    note = clean_text(body, "note", max_len=200) if "note" in body else None
    billable = clean_flag(body, "billable", True) if "billable" in body else None
    seconds = old["seconds"]
    work_date = old["work_date"]
    if not running:
        if "minutes" in body:
            seconds = clean_int(body, "minutes", 1, 1440) * 60
        if body.get("work_date"):
            work_date = _check_date(clean_date(body, "work_date"))
    elif "minutes" in body or "work_date" in body:
        raise ApiError(409, "Clock out first, then adjust the time")
    ts = now_iso()
    with tx(db):
        db.execute("UPDATE time_entries SET seconds=?, work_date=?, note=COALESCE(?, note), billable=COALESCE(?, billable), "
                   "updated_at=? WHERE id=?", (seconds, work_date, note, billable, ts, old["id"]))
        audit(db, ctx.user.id, "time.edited", "ticket", old["ticket_id"],
              {"entry": old["id"], "from": old["seconds"], "to": seconds}, ctx.req.ip)
    return {"entry": _entry(db, old["id"])}


def delete_entry(ctx):
    db = ctx.db
    old = _own_or_manage(ctx, ctx.params["id"])
    with tx(db):
        db.execute("DELETE FROM time_entries WHERE id=?", (old["id"],))
        _history(db, old["ticket_id"], ctx.user.id, "time_removed", {"seconds": old["seconds"]})
        audit(db, ctx.user.id, "time.removed", "ticket", old["ticket_id"], {"seconds": old["seconds"]}, ctx.req.ip)
    return {"ok": True}


def list_entries(ctx):
    """Timesheet for a date range. People without time.manage only ever see their own time."""
    req = ctx.req

    def one(name):
        return (req.query.get(name, [""])[0] or "").strip()

    today = datetime.now(timezone.utc).date()
    start = one("from") or (today - timedelta(days=6)).isoformat()
    end = one("to") or today.isoformat()
    for v in (start, end):
        try:
            if len(v) != 10:
                raise ValueError
            date.fromisoformat(v)
        except ValueError:
            raise ApiError(400, "Dates must look like 2026-03-31")
    if end < start:
        raise ApiError(400, "The end date is before the start date")
    if (date.fromisoformat(end) - date.fromisoformat(start)).days > 366:
        raise ApiError(400, "Choose a range of at most a year")
    uid = one("user_id")[:64] if ctx.user.can("time.manage") else ctx.user.id
    now = now_iso()
    entries = [tk.present(r, now) for r in ctx.db.execute(
        RANGE_SQL, {"date_from": start, "date_to": end, "uid": uid, "ticket": one("ticket_id")[:64]})]
    out = _totals(entries)
    out.update({"entries": entries, "from": start, "to": end, "can_manage": ctx.user.can("time.manage"), "server_now": now})
    return out
