"""Time-tracking helpers shared by the time and ticket modules. Call inside a transaction."""
import json
from datetime import datetime, timezone

from .util import now_iso

FMT = "%Y-%m-%dT%H:%M:%SZ"


def parse_ts(value):
    return datetime.strptime(value, FMT).replace(tzinfo=timezone.utc)


def elapsed_seconds(started_at, now_ts=None):
    return max(0, int((parse_ts(now_ts or now_iso()) - parse_ts(started_at)).total_seconds()))


def stop_entry(db, row, ts, actor_id, note=None):
    """Stops a running timer row (needs id, ticket_id, started_at). Returns the recorded seconds."""
    seconds = elapsed_seconds(row["started_at"], ts)
    if note:
        db.execute("UPDATE time_entries SET ended_at=?, seconds=?, note=?, updated_at=? WHERE id=?",
                   (ts, seconds, note, ts, row["id"]))
    else:
        db.execute("UPDATE time_entries SET ended_at=?, seconds=?, updated_at=? WHERE id=?", (ts, seconds, ts, row["id"]))
    db.execute("INSERT INTO ticket_history(ticket_id, ts, user_id, kind, detail) VALUES(?,?,?,?,?)",
               (row["ticket_id"], ts, actor_id, "time_out", json.dumps({"seconds": seconds})))
    return seconds


def stop_all_on_ticket(db, ticket_id, ts, actor_id):
    """Used when a ticket is closed: nobody should keep billing time to a finished job."""
    for row in db.execute("SELECT id, ticket_id, started_at FROM time_entries "
                          "WHERE ticket_id=? AND source='timer' AND ended_at IS NULL", (ticket_id,)).fetchall():
        stop_entry(db, row, ts, actor_id)


ENTRY_SQL = """
SELECT e.id, e.ticket_id, t.number AS ticket_number, t.title AS ticket_title, e.user_id, u.name AS user_name,
       e.work_date, e.started_at, e.ended_at, e.seconds, e.note, e.billable, e.source, e.created_at,
       (e.source = 'timer' AND e.ended_at IS NULL) AS running
FROM time_entries e
JOIN tickets t ON t.id = e.ticket_id
JOIN users u ON u.id = e.user_id
"""
ENTRY_BY_ID_SQL = ENTRY_SQL + "WHERE e.id = ?"
RUNNING_FOR_USER_SQL = ENTRY_SQL + "WHERE e.user_id = ? AND e.source = 'timer' AND e.ended_at IS NULL"
CLOCKED_IN_SQL = ENTRY_SQL + "WHERE e.source = 'timer' AND e.ended_at IS NULL ORDER BY e.started_at"


def present(row, now_ts):
    out = dict(row)
    out["running"] = bool(out["running"])
    out["billable"] = bool(out["billable"])
    if out["running"]:
        out["seconds"] = elapsed_seconds(out["started_at"], now_ts)
    return out


def running_for_user(db, user_id):
    row = db.execute(RUNNING_FOR_USER_SQL, (user_id,)).fetchone()
    return present(row, now_iso()) if row else None


def clocked_in_now(db):
    now = now_iso()
    return [present(r, now) for r in db.execute(CLOCKED_IN_SQL)]
