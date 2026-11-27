"""Background job: turn due touch bases into follow-up tickets.

Runs once at startup and then about every hour on a daemon thread, visiting every ACTIVE shop's database. For each
scheduled touch base due today or earlier (so a server that was down on the due date catches up) it creates exactly one
ticket. Idempotent: the check-and-create happens inside one BEGIN IMMEDIATE transaction that re-reads the touch base's
status, and two unique indexes back it up, so restarts and overlapping runs can never make two tickets. Errors are
logged (no customer data) and retried on the next run; the job never raises into the web server.
"""
import json
import sys
import threading
from datetime import datetime, timezone

from .audit import audit
from .db import connect, tx
from .modules.tickets import _allocate_number, _history
from .tenants import open_tenant_db
from .trash import purge_expired
from .util import new_id, now_iso

INTERVAL_SECONDS = 3600
BATCH = 200
_run_lock = threading.Lock()


def _log(level, msg, **fields):
    sys.stderr.write(json.dumps(dict({"level": level, "msg": msg}, **fields)) + "\n")


def _clip(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _description(db, orig, customer_name, tb):
    equipment = [r["name"] for r in db.execute(
        "SELECT e.name FROM ticket_equipment te JOIN equipment e ON e.id = te.equipment_id WHERE te.ticket_id=? "
        "ORDER BY e.name", (orig["id"],))]
    lines = ["Customer: %s" % customer_name,
             "Follow-up to ticket #%s (%s). Open it at #/tickets/%s" % (orig["number"], _clip(orig["title"], 80), orig["id"]),
             "Touch base notes: %s" % (tb["notes"] or "(none)"), "",
             "Original ticket: status %s, opened %s%s" % (orig["status_name"], (orig["created_at"] or "")[:10],
                                                          (", closed " + orig["closed_at"][:10]) if orig["closed_at"] else "")]
    if equipment:
        lines.append("Equipment: %s" % _clip(", ".join(equipment), 150))
    if orig["symptoms"]:
        lines.append("Reported problem: %s" % _clip(orig["symptoms"], 300))
    if orig["intake_notes"]:
        lines.append("Intake notes: %s" % _clip(orig["intake_notes"], 300))
    return "\n".join(lines)[:2000]


def create_followup(db, touch_base_id):
    """Returns the new ticket id, or None when nothing was created (already handled, or skipped)."""
    with tx(db):
        tb = db.execute("SELECT * FROM touch_bases WHERE id=? AND status='scheduled'", (touch_base_id,)).fetchone()
        if tb is None:
            return None                                   # another run (or a person) already dealt with it
        cust = db.execute("SELECT id, name, archived_at FROM customers WHERE id=?", (tb["customer_id"],)).fetchone()
        orig = db.execute(
            "SELECT t.id, t.number, t.title, t.symptoms, t.intake_notes, t.created_at, t.closed_at, s.name AS status_name "
            "FROM tickets t JOIN ticket_statuses s ON s.id = t.status_id WHERE t.id=?", (tb["ticket_id"],)).fetchone()
        status = db.execute("SELECT id, name FROM ticket_statuses WHERE active=1 ORDER BY sort_order LIMIT 1").fetchone()
        if cust is None or cust["archived_at"] is not None or orig is None or status is None:
            db.execute("UPDATE touch_bases SET status='cancelled' WHERE id=?", (tb["id"],))
            audit(db, None, "touchbase.skipped", "ticket", tb["ticket_id"],
                  {"touch_base_id": tb["id"], "reason": "customer unavailable"})
            return None
        tid, ts = new_id(), now_iso()
        number = _allocate_number(db)
        title = ("Touch base: %s (follow-up to #%s)" % (cust["name"], orig["number"]))[:150]
        db.execute(
            "INSERT INTO tickets(id, number, customer_id, status_id, title, priority, due_date, symptoms, intake_notes, "
            "created_by, created_at, updated_at) VALUES(?,?,?,?,?,'low',NULL,'',?,NULL,?,?)",
            (tid, number, cust["id"], status["id"], title, _description(db, orig, cust["name"], tb), ts, ts))
        techs = db.execute("SELECT u.id, u.name FROM ticket_assignees a JOIN users u ON u.id = a.user_id "
                           "WHERE a.ticket_id=? AND u.active=1", (orig["id"],)).fetchall()
        _history(db, tid, None, "created", {"number": number, "status": status["name"]})
        for u in techs:
            db.execute("INSERT INTO ticket_assignees(ticket_id, user_id, assigned_at, assigned_by) VALUES(?,?,?,NULL)",
                       (tid, u["id"], ts))
            _history(db, tid, None, "assigned", {"user_id": u["id"], "name": u["name"]})
        db.execute("UPDATE touch_bases SET status='created', created_ticket_id=? WHERE id=?", (tid, tb["id"]))
        _history(db, orig["id"], None, "touch_base", {"action": "created", "ticket_id": tid, "number": number})
        audit(db, None, "touchbase.created", "ticket", tid,
              {"touch_base_id": tb["id"], "from_ticket": orig["number"], "number": number})
        return tid


def process_shop(config, tenant_id, today):
    db = open_tenant_db(config, tenant_id)
    try:
        due = [r["id"] for r in db.execute(
            "SELECT id FROM touch_bases WHERE status='scheduled' AND due_date <= ? ORDER BY due_date, id LIMIT ?",
            (today, BATCH))]
        made = 0
        for tb_id in due:
            try:
                if create_followup(db, tb_id):
                    made += 1
            except Exception as e:                        # one bad touch base must not block the others
                _log("error", "touch base failed", tenant=tenant_id, touch_base=tb_id, error=type(e).__name__ + ": " + str(e))
        return made
    finally:
        db.close()


def run_once(config, today=None):
    """One pass over every active shop. Returns the number of tickets created. Never raises."""
    if not _run_lock.acquire(blocking=False):
        return 0                                          # a pass is already running in this process
    try:
        today = today or datetime.now(timezone.utc).date().isoformat()
        try:
            plat = connect(config.platform_db_path)
            try:
                shops = [r["id"] for r in plat.execute("SELECT id FROM tenants WHERE status='active'")]
            finally:
                plat.close()
        except Exception as e:
            _log("error", "touch base job cannot list shops", error=type(e).__name__ + ": " + str(e))
            return 0
        total = 0
        for tid in shops:                                 # suspended shops are skipped and catch up when reactivated
            try:
                total += process_shop(config, tid, today)
            except Exception as e:                        # missing database file, locked database, bad data ...
                _log("error", "touch base job failed for a shop", tenant=tid, error=type(e).__name__ + ": " + str(e))
        if total:
            _log("info", "touch base follow-up tickets created", count=total)
        return total
    finally:
        _run_lock.release()


def start(config, interval=INTERVAL_SECONDS):
    """Start the hourly loop (first pass immediately). Returns an Event that stops it."""
    stop = threading.Event()

    def loop():
        while True:
            run_once(config)
            purge_expired(config)                             # deleted shops older than 30 days leave the trash folder
            if stop.wait(interval):
                return

    threading.Thread(target=loop, name="touch-base-job", daemon=True).start()
    return stop
