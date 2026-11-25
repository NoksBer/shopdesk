"""Running cash ledger: money in and out, with a running balance. Inventory purchases and invoice payments are written
by their own modules; this module adds manual entries, reversals, filters, a summary and a CSV export."""
import csv
import io
from datetime import date, datetime, timedelta, timezone

from .. import books
from ..audit import audit
from ..db import tx
from ..http_util import Response
from ..util import ApiError, clean_int, clean_text, now_iso

MAX_ENTRY_CENTS = 1_000_000_000          # $10,000,000.00


def register(router):
    router.add("GET", "/api/ledger/summary", get_summary, "ledger.read")
    router.add("GET", "/api/ledger/export", export_csv, "ledger.read")
    router.add("GET", "/api/ledger", list_entries, "ledger.read")
    router.add("POST", "/api/ledger", create_entry, "ledger.write")
    router.add("POST", "/api/ledger/<id>/reverse", reverse_entry, "ledger.write")


def _date_or_empty(value):
    if not value:
        return ""
    try:
        if len(value) != 10:
            raise ValueError
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ApiError(400, "Dates must look like 2026-03-31")


def _entry(row):
    out = dict(row)
    out["reversed"] = bool(out["reversed"])
    out["category_label"] = books.ALL_CATEGORIES.get(out["category"], out["category"])
    return out


def get_summary(ctx):
    out = books.summary(ctx.db)
    out["recent"] = [_entry(r) for r in out["recent"]]
    return out


def list_entries(ctx):
    req = ctx.req
    params = books.filter_params(req, _date_or_empty)
    page = dict(params, limit=req.query_int("limit", 100, 1, 500), offset=req.query_int("offset", 0, 0, 1000000))
    rows = [_entry(r) for r in ctx.db.execute(books.LIST_SQL, page).fetchall()]
    t = ctx.db.execute(books.TOTALS_SQL, params).fetchone()
    balance = ctx.db.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM ledger_entries").fetchone()[0]
    return {"entries": rows, "total": t["n"], "money_in": t["money_in"], "money_out": t["money_out"],
            "net": t["money_in"] - t["money_out"], "balance_cents": balance,
            "categories": {"income": books.INCOME_CATEGORIES, "expense": books.EXPENSE_CATEGORIES}}


def create_entry(ctx):
    """A manual entry: cash that did not come from an invoice or an inventory purchase."""
    body = ctx.req.json()
    direction = body.get("direction")
    if direction not in ("in", "out"):
        raise ApiError(400, "direction must be in or out")
    amount = clean_int(body, "amount_cents", 1, MAX_ENTRY_CENTS)
    allowed = books.INCOME_CATEGORIES if direction == "in" else books.EXPENSE_CATEGORIES
    category = clean_text(body, "category", max_len=40, required=True)
    if category not in allowed:
        raise ApiError(400, "Choose a category from the list")
    description = clean_text(body, "description", max_len=200, required=True)
    when = _date_or_empty(clean_text(body, "entry_date", max_len=10)) or now_iso()[:10]
    tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    if when > tomorrow:
        raise ApiError(400, "The date cannot be in the future")
    with tx(ctx.db):
        eid = books.add_entry(ctx.db, ctx.user.id, amount if direction == "in" else -amount, category, description,
                              "manual", "", "", entry_date=when)
        audit(ctx.db, ctx.user.id, "ledger.created", "ledger", str(eid), {"cents": amount if direction == "in" else -amount},
              ctx.req.ip)
    return 201, {"id": eid}


def reverse_entry(ctx):
    """Corrects a mistake without erasing it: adds an opposite entry that points at the original."""
    body = ctx.req.json()
    db = ctx.db
    raw = ctx.params["id"]
    if not raw.isdigit():
        raise ApiError(404, "Not found")
    reason = clean_text(body, "reason", max_len=120, required=True)
    with tx(db):
        old = db.execute("SELECT id, amount_cents, category, description, ref_type, ref_id, ref_label, reverses_id "
                         "FROM ledger_entries WHERE id=?", (int(raw),)).fetchone()
        if old is None:
            raise ApiError(404, "Not found")
        if old["reverses_id"] is not None:
            raise ApiError(409, "That entry is itself a reversal")
        if old["category"] == "invoice_payment":
            raise ApiError(409, "Void the invoice instead. That reverses its payment automatically.")
        if db.execute("SELECT 1 FROM ledger_entries WHERE reverses_id=?", (old["id"],)).fetchone():
            raise ApiError(409, "That entry has already been reversed")
        new_id = books.add_entry(db, ctx.user.id, -old["amount_cents"], "reversal",
                                 "Reversal: %s (%s)" % (old["description"][:150], reason),
                                 old["ref_type"], old["ref_id"], old["ref_label"], reverses_id=old["id"])
        audit(db, ctx.user.id, "ledger.reversed", "ledger", str(new_id), {"reverses": old["id"]}, ctx.req.ip)
    return 201, {"id": new_id}


def _safe(text):
    """Stops spreadsheet programs from running a cell that starts with = + - or @ as a formula."""
    text = str(text)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def export_csv(ctx):
    params = books.filter_params(ctx.req, _date_or_empty)
    rows = ctx.db.execute(books.EXPORT_SQL, params).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Description", "Reference", "Category", "Money in", "Money out", "Balance", "Entered by"])
    for r in rows:
        amount = r["amount_cents"]
        w.writerow([r["entry_date"], _safe(r["description"]), _safe(r["ref_label"]),
                    books.ALL_CATEGORIES.get(r["category"], r["category"]),
                    books.money_text(amount) if amount > 0 else "", books.money_text(-amount) if amount < 0 else "",
                    books.money_text(r["balance_cents"]), _safe(r["user_name"] or "")])
    data = buf.getvalue().encode("utf-8-sig")
    return Response(200, data, "text/csv; charset=utf-8", [("Content-Disposition", 'attachment; filename="ledger.csv"')])
