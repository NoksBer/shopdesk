"""Cash ledger helpers shared by the inventory, invoice and ledger modules.
Rows are append-only. Positive amounts are money in, negative amounts are money out.
Callers must already be inside a transaction so the ledger row commits (or rolls back) with the event that caused it."""
from datetime import datetime, timedelta, timezone

from .util import now_iso

INCOME_CATEGORIES = {"sale": "Sale", "deposit": "Owner deposit", "other_income": "Other income"}
EXPENSE_CATEGORIES = {"supplies": "Supplies", "rent": "Rent", "utilities": "Utilities", "payroll": "Payroll",
                      "equipment": "Equipment", "shipping": "Shipping", "marketing": "Marketing", "tax": "Taxes and fees",
                      "other_expense": "Other expense"}
AUTO_CATEGORIES = {"invoice_payment": "Invoice payment", "inventory_purchase": "Inventory purchase", "reversal": "Reversal"}
ALL_CATEGORIES = {**INCOME_CATEGORIES, **EXPENSE_CATEGORIES, **AUTO_CATEGORIES}


def add_entry(db, user_id, amount_cents, category, description, ref_type="", ref_id="", ref_label="",
              entry_date=None, reverses_id=None, ts=None):
    ts = ts or now_iso()
    cur = db.execute(
        "INSERT INTO ledger_entries(entry_date, ts, amount_cents, category, description, ref_type, ref_id, ref_label, "
        "reverses_id, user_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (entry_date or ts[:10], ts, amount_cents, category, description[:300], ref_type, ref_id, ref_label,
         reverses_id, user_id))
    return cur.lastrowid


def money_text(cents):
    return "%.2f" % (cents / 100)


_FILTER = """
WHERE (:date_from = '' OR r.entry_date >= :date_from) AND (:date_to = '' OR r.entry_date <= :date_to)
  AND (:category = '' OR r.category = :category)
  AND (:dir = '' OR (:dir = 'in' AND r.amount_cents > 0) OR (:dir = 'out' AND r.amount_cents < 0))
  AND (:q = '' OR r.description LIKE :pat ESCAPE '\\' OR r.ref_label LIKE :pat ESCAPE '\\')
"""

# The running balance is computed over the WHOLE ledger in date order, then filtered, so a filtered view still shows
# the true balance after each row.
_WITH_BALANCE = """
WITH r AS (
  SELECT e.*, SUM(e.amount_cents) OVER (ORDER BY e.entry_date, e.id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
         AS balance_cents
  FROM ledger_entries e)
SELECT r.id, r.entry_date, r.ts, r.amount_cents, r.category, r.description, r.ref_type, r.ref_id, r.ref_label,
       r.reverses_id, r.balance_cents, u.name AS user_name,
       EXISTS (SELECT 1 FROM ledger_entries x WHERE x.reverses_id = r.id) AS reversed
FROM r LEFT JOIN users u ON u.id = r.user_id
"""

LIST_SQL = _WITH_BALANCE + _FILTER + "ORDER BY r.entry_date DESC, r.id DESC LIMIT :limit OFFSET :offset"
EXPORT_SQL = _WITH_BALANCE + _FILTER + "ORDER BY r.entry_date, r.id LIMIT 5000"
TOTALS_SQL = ("SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN r.amount_cents > 0 THEN r.amount_cents END), 0) AS money_in, "
              "COALESCE(SUM(CASE WHEN r.amount_cents < 0 THEN -r.amount_cents END), 0) AS money_out "
              "FROM ledger_entries r") + _FILTER
RECENT_SQL = _WITH_BALANCE + "ORDER BY r.entry_date DESC, r.id DESC LIMIT :limit"

MONTHS_SQL = """
SELECT substr(entry_date, 1, 7) AS month,
       COALESCE(SUM(CASE WHEN amount_cents > 0 THEN amount_cents END), 0) AS money_in,
       COALESCE(SUM(CASE WHEN amount_cents < 0 THEN -amount_cents END), 0) AS money_out
FROM ledger_entries WHERE entry_date >= :since GROUP BY substr(entry_date, 1, 7)
"""


def month_keys(now, count):
    year, month, keys = now.year, now.month, []
    for _ in range(count):
        keys.append("%04d-%02d" % (year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(keys))


def summary(db, months=6, recent=6):
    """Cash-flow snapshot for the ledger header and the dashboard. Dates are UTC."""
    now = datetime.now(timezone.utc)
    keys = month_keys(now, months)
    found = {r["month"]: r for r in db.execute(MONTHS_SQL, {"since": keys[0] + "-01"})}
    series = []
    for key in keys:
        r = found.get(key)
        money_in, money_out = (r["money_in"], r["money_out"]) if r else (0, 0)
        series.append({"month": key, "money_in": money_in, "money_out": money_out, "net": money_in - money_out})
    balance = db.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM ledger_entries").fetchone()[0]
    rows = [dict(r) for r in db.execute(RECENT_SQL, {"limit": recent})]
    return {"balance_cents": balance, "this_month": series[-1], "last_month": series[-2], "months": series,
            "recent": rows}


def filter_params(req, clean_date_value):
    """Builds the named parameters for the ledger filters from a query string (all optional)."""
    def one(name):
        return (req.query.get(name, [""])[0] or "").strip()
    q = one("q")[:100]
    direction = one("dir")
    if direction not in ("", "in", "out"):
        direction = ""
    category = one("category")[:40]
    from .util import like_pattern
    return {"date_from": clean_date_value(one("from")), "date_to": clean_date_value(one("to")),
            "category": category, "dir": direction, "q": q, "pat": like_pattern(q)}
