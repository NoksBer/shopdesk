"""Dashboard summary. Ticket numbers respect visibility: technicians only count tickets assigned to them."""
import json
from datetime import datetime, timedelta, timezone

from .. import books
from .. import timekeeping as tk

_R = "(:restrict = 0 OR EXISTS (SELECT 1 FROM ticket_assignees ra WHERE ra.ticket_id = t.id AND ra.user_id = :uid))"

COUNTS_SQL = ("""
SELECT
  SUM(CASE WHEN s.is_closed = 0 THEN 1 ELSE 0 END) AS open_count,
  SUM(CASE WHEN s.is_closed = 0 AND t.due_date IS NOT NULL AND t.due_date < :today THEN 1 ELSE 0 END) AS overdue,
  SUM(CASE WHEN s.is_closed = 0 AND t.due_date IS NOT NULL AND t.due_date >= :today AND t.due_date <= :week_end
           THEN 1 ELSE 0 END) AS due_week,
  SUM(CASE WHEN s.is_closed = 0 AND NOT EXISTS (SELECT 1 FROM ticket_assignees ua WHERE ua.ticket_id = t.id)
           THEN 1 ELSE 0 END) AS unassigned,
  SUM(CASE WHEN s.is_closed = 1 AND t.closed_at >= :week_ago THEN 1 ELSE 0 END) AS closed_week
FROM tickets t JOIN ticket_statuses s ON s.id = t.status_id
WHERE """ + _R)

BY_STATUS_SQL = ("""
SELECT s.id, s.name, s.is_closed,
       (SELECT COUNT(*) FROM tickets t WHERE t.status_id = s.id AND """ + _R + """) AS n
FROM ticket_statuses s WHERE s.active = 1 ORDER BY s.sort_order, s.name
""")

MINE_SQL = """
SELECT t.id, t.number, t.title, t.priority, t.due_date, c.name AS customer_name, s.name AS status_name
FROM tickets t
JOIN ticket_statuses s ON s.id = t.status_id
JOIN customers c ON c.id = t.customer_id
JOIN ticket_assignees a ON a.ticket_id = t.id AND a.user_id = :uid
WHERE s.is_closed = 0
ORDER BY CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
         COALESCE(t.due_date, '9999-12-31'), t.created_at
LIMIT 10
"""

RECENT_SQL = ("""
SELECT h.id, h.ts, h.kind, h.detail, u.name AS user_name, t.id AS ticket_id, t.number, t.title
FROM ticket_history h
JOIN tickets t ON t.id = h.ticket_id
LEFT JOIN users u ON u.id = h.user_id
WHERE """ + _R + """
ORDER BY h.id DESC LIMIT 10
""")


REV_MONTHS_SQL = """
SELECT substr(paid_at, 1, 7) AS month, COUNT(*) AS n,
       COALESCE(SUM(total_cents), 0) AS total, COALESCE(SUM(tax_cents), 0) AS tax
FROM invoices WHERE status = 'paid' AND paid_at >= :since
GROUP BY substr(paid_at, 1, 7) ORDER BY month
"""

REV_YTD_SQL = ("SELECT COALESCE(SUM(total_cents), 0) AS total, COUNT(*) AS n FROM invoices "
               "WHERE status = 'paid' AND paid_at >= :ystart")

REV_OWED_SQL = """
SELECT COALESCE(SUM(total_cents), 0) AS owed, COUNT(*) AS n,
       COALESCE(SUM(CASE WHEN due_date IS NOT NULL AND due_date < :today THEN total_cents ELSE 0 END), 0) AS overdue
FROM invoices WHERE status = 'issued'
"""

REV_RECENT_SQL = """
SELECT i.id, i.number, c.name AS customer_name, i.total_cents, i.paid_at, i.payment_method
FROM invoices i JOIN customers c ON c.id = i.customer_id
WHERE i.status = 'paid' ORDER BY i.paid_at DESC, i.id LIMIT 5
"""


def register(router):
    router.add("GET", "/api/dashboard", get_dashboard, "authed")


def _month_keys(now, count=12):
    year, month, keys = now.year, now.month, []
    for _ in range(count):
        keys.append("%04d-%02d" % (year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(keys))


def _revenue(db):
    """Money actually collected: invoices marked paid, bucketed by the (UTC) month they were paid.
    Voiding a paid invoice removes it from these figures. Totals include sales tax; tax is reported separately."""
    now = datetime.now(timezone.utc)
    keys = _month_keys(now)
    found = {r["month"]: r for r in db.execute(REV_MONTHS_SQL, {"since": keys[0] + "-01T00:00:00Z"})}
    months = []
    for key in keys:
        r = found.get(key)
        months.append({"month": key, "total_cents": r["total"] if r else 0, "tax_cents": r["tax"] if r else 0,
                       "count": r["n"] if r else 0})
    ytd = db.execute(REV_YTD_SQL, {"ystart": "%04d-01-01T00:00:00Z" % now.year}).fetchone()
    owed = db.execute(REV_OWED_SQL, {"today": now.date().isoformat()}).fetchone()
    return {"months": months, "this_month": months[-1], "last_month": months[-2],
            "ytd_cents": ytd["total"], "ytd_count": ytd["n"],
            "outstanding_cents": owed["owed"], "outstanding_count": owed["n"], "overdue_cents": owed["overdue"],
            "recent": [dict(r) for r in db.execute(REV_RECENT_SQL)]}


def get_dashboard(ctx):
    user = ctx.user
    out = {"tickets": None, "revenue": _revenue(ctx.db) if user.can("invoices.read") else None,
           "ledger": books.summary(ctx.db, recent=5) if user.can("ledger.read") else None,
           "time": _time(ctx)}
    if user.can("tickets.read"):
        out["tickets"] = _tickets(ctx)
    return out


def _time(ctx):
    """Who is clocked in right now (managers) and the viewer's own running timer."""
    user = ctx.user
    if not user.can("time.write"):
        return None
    return {"mine": tk.running_for_user(ctx.db, user.id),
            "clocked_in": tk.clocked_in_now(ctx.db) if user.can("time.manage") else None}


def _tickets(ctx):
    user = ctx.user
    restrict = 0 if user.can("tickets.read_all") else 1
    # "today" is the server's UTC date until per-shop timezones are applied.
    today = datetime.now(timezone.utc).date()
    params = {"restrict": restrict, "uid": user.id, "today": today.isoformat(),
              "week_end": (today + timedelta(days=7)).isoformat(),
              "week_ago": (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    row = ctx.db.execute(COUNTS_SQL, params).fetchone()
    counts = {k: (row[k] or 0) for k in ("open_count", "overdue", "due_week", "unassigned", "closed_week")}
    if restrict:
        counts["unassigned"] = None          # unassigned tickets are invisible to this user
    by_status = [dict(r) for r in ctx.db.execute(BY_STATUS_SQL, params)]
    mine = [dict(r) for r in ctx.db.execute(MINE_SQL, {"uid": user.id})]
    recent = []
    for r in ctx.db.execute(RECENT_SQL, params):
        item = dict(r)
        item["detail"] = json.loads(item["detail"])
        recent.append(item)
    return {"counts": counts, "by_status": by_status, "my_open": mine, "recent": recent}
