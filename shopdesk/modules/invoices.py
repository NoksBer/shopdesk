"""Invoices. Drafts are freely editable; issuing assigns the number, locks the lines and takes parts out of stock.
Money is integer cents. Quantities are stored in hundredths so labour can be billed as e.g. 1.5 hours."""
import json
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from .. import books
from ..audit import audit
from ..db import tx
from ..util import (MAX_CENTS, ApiError, clean_date, clean_flag, clean_int, clean_text, like_pattern, new_id,
                    now_iso)

KINDS = ("manual", "service", "inventory")
PREFIX_RE = re.compile(r"^[A-Za-z0-9-]{0,10}$")
MAX_LINES = 100

LIST_SQL = """
SELECT i.id, i.number, i.status, i.customer_id, c.name AS customer_name, i.ticket_id, t.number AS ticket_number,
       i.issue_date, i.due_date, i.total_cents, i.paid_at, i.created_at
FROM invoices i
JOIN customers c ON c.id = i.customer_id
LEFT JOIN tickets t ON t.id = i.ticket_id
WHERE (:status = '' OR i.status = :status)
  AND (:cust = '' OR i.customer_id = :cust)
  AND (:ticket = '' OR i.ticket_id = :ticket)
  AND (:q = '' OR i.number LIKE :pat ESCAPE '\\' OR c.name LIKE :pat ESCAPE '\\')
ORDER BY CASE i.status WHEN 'draft' THEN 0 ELSE 1 END, COALESCE(i.issue_date, i.created_at) DESC, i.id
LIMIT :limit OFFSET :offset
"""

COUNT_SQL = """
SELECT COUNT(*) FROM invoices i JOIN customers c ON c.id = i.customer_id
WHERE (:status = '' OR i.status = :status)
  AND (:cust = '' OR i.customer_id = :cust)
  AND (:ticket = '' OR i.ticket_id = :ticket)
  AND (:q = '' OR i.number LIKE :pat ESCAPE '\\' OR c.name LIKE :pat ESCAPE '\\')
"""

ONE_SQL = """
SELECT i.id, i.number, i.status, i.customer_id, c.name AS customer_name, i.ticket_id, t.number AS ticket_number,
       i.issue_date, i.due_date, i.notes, i.tax_bp, i.subtotal_cents, i.tax_cents, i.total_cents, i.bill_to,
       i.paid_at, i.payment_method, i.void_reason, i.created_at, i.updated_at
FROM invoices i
JOIN customers c ON c.id = i.customer_id
LEFT JOIN tickets t ON t.id = i.ticket_id
WHERE i.id = ?
"""


def register(router):
    # "settings" must be registered before "<id>"
    router.add("GET", "/api/invoices/settings", get_settings, "invoices.read")
    router.add("PUT", "/api/invoices/settings", put_settings, "business.manage")
    router.add("GET", "/api/invoices", list_invoices, "invoices.read")
    router.add("POST", "/api/invoices", create_invoice, "invoices.write")
    router.add("GET", "/api/invoices/<id>", get_invoice, "invoices.read")
    router.add("PUT", "/api/invoices/<id>", update_invoice, "invoices.write")
    router.add("DELETE", "/api/invoices/<id>", delete_invoice, "invoices.write")
    router.add("POST", "/api/invoices/<id>/issue", issue_invoice, "invoices.write")
    router.add("POST", "/api/invoices/<id>/pay", pay_invoice, "invoices.write")
    router.add("POST", "/api/invoices/<id>/void", void_invoice, "invoices.write")


# ---------------------------------------------------------------- money
def line_total(qty_x100, unit_cents):
    return (qty_x100 * unit_cents + 50) // 100


def totals(items, tax_bp):
    """items: dicts with qty_x100, unit_cents, taxable. Returns (subtotal, tax, total) in cents."""
    subtotal = taxable = 0
    for it in items:
        line = line_total(it["qty_x100"], it["unit_cents"])
        subtotal += line
        if it["taxable"]:
            taxable += line
    tax = (taxable * tax_bp + 5000) // 10000
    return subtotal, tax, subtotal + tax


def _qty_x100(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError(400, "quantity must be a number")
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        raise ApiError(400, "quantity must be a number")
    if not d.is_finite():
        raise ApiError(400, "quantity must be a number")
    scaled = d * 100
    if scaled != scaled.to_integral_value():
        raise ApiError(400, "quantity can have at most 2 decimal places")
    q = int(scaled)
    if not 1 <= q <= 10_000_000:
        raise ApiError(400, "quantity must be between 0.01 and 100000")
    return q


# ---------------------------------------------------------------- parsing and loading
def _settings(db):
    return db.execute("SELECT prefix, next_number, default_tax_bp, terms_days, footer FROM invoice_settings WHERE id=1"
                      ).fetchone()


def _parse_items(db, raw):
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_LINES:
        raise ApiError(400, "items must be a list of at most %d lines" % MAX_LINES)
    out = []
    for n, it in enumerate(raw, 1):
        if not isinstance(it, dict):
            raise ApiError(400, "line %d is invalid" % n)
        kind = it.get("kind")
        if kind not in KINDS:
            raise ApiError(400, "line %d: kind must be manual, service or inventory" % n)
        qty = _qty_x100(it.get("quantity"))
        cat = None
        ref_id = None
        if kind != "manual":
            ref_id = it.get("ref_id")
            if not isinstance(ref_id, str):
                raise ApiError(400, "line %d: choose an item" % n)
            if kind == "service":
                cat = db.execute("SELECT name, description, price_cents, taxable, 0 AS cost_cents FROM services "
                                 "WHERE id=?", (ref_id,)).fetchone()
            else:
                cat = db.execute("SELECT name, description, price_cents, taxable, cost_cents FROM inventory_items "
                                 "WHERE id=?", (ref_id,)).fetchone()
            if cat is None:
                raise ApiError(400, "line %d: unknown %s" % (n, "service" if kind == "service" else "inventory item"))
            if kind == "inventory" and qty % 100:
                raise ApiError(400, "line %d: parts are sold in whole units" % n)
        name = clean_text(it, "name", max_len=120) or (cat["name"] if cat else "")
        if not name:
            raise ApiError(400, "line %d: item name is required" % n)
        description = clean_text(it, "description", max_len=300) if "description" in it else (cat["description"] if cat else "")
        if "unit_price_cents" in it:
            unit = clean_int(it, "unit_price_cents", 0, MAX_CENTS)
        elif cat is not None:
            unit = cat["price_cents"]
        else:
            raise ApiError(400, "line %d: price is required" % n)
        taxable = clean_flag(it, "taxable", bool(cat["taxable"]) if cat else True)
        out.append({"kind": kind, "ref_id": ref_id, "name": name, "description": description, "qty_x100": qty,
                    "unit_cents": unit, "unit_cost_cents": cat["cost_cents"] if cat else 0, "taxable": taxable})
    return out


def _bill_to(db, customer_id):
    c = db.execute("SELECT name, contact_name, email, phone, address FROM customers WHERE id=?",
                   (customer_id,)).fetchone()
    return json.dumps(dict(c), sort_keys=True)


def _header(body, cfg, current=None):
    tax_bp = clean_int(body, "tax_bp", 0, 10000, current["tax_bp"] if current else cfg["default_tax_bp"])
    due = clean_date(body, "due_date") if "due_date" in body else (current["due_date"] if current else None)
    notes = clean_text(body, "notes", max_len=2000) if "notes" in body else (current["notes"] if current else "")
    return tax_bp, due, notes


def _items_of(db, invoice_id):
    rows = db.execute("SELECT id, position, kind, ref_id, name, description, qty_x100, unit_cents, taxable, line_cents "
                      "FROM invoice_items WHERE invoice_id=? ORDER BY position", (invoice_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["taxable"] = bool(d["taxable"])
        d["quantity"] = d["qty_x100"] / 100
        out.append(d)
    return out


def _load(db, invoice_id):
    row = db.execute(ONE_SQL, (invoice_id if isinstance(invoice_id, str) else "",)).fetchone()
    if row is None:
        raise ApiError(404, "Not found")
    out = dict(row)
    out["bill_to"] = json.loads(out["bill_to"])
    out["items"] = _items_of(db, row["id"])
    return out


def _write_items(db, invoice_id, items):
    db.execute("DELETE FROM invoice_items WHERE invoice_id=?", (invoice_id,))
    for pos, it in enumerate(items):
        db.execute(
            "INSERT INTO invoice_items(invoice_id, position, kind, ref_id, name, description, qty_x100, unit_cents, "
            "unit_cost_cents, taxable, line_cents) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (invoice_id, pos, it["kind"], it["ref_id"], it["name"], it["description"], it["qty_x100"],
             it["unit_cents"], it["unit_cost_cents"], it["taxable"], line_total(it["qty_x100"], it["unit_cents"])))


def _status_of(db, invoice_id):
    row = db.execute("SELECT status FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if row is None:
        raise ApiError(404, "Not found")
    return row["status"]


# ---------------------------------------------------------------- settings
def get_settings(ctx):
    return dict(_settings(ctx.db))


def put_settings(ctx):
    body = ctx.req.json()
    prefix = clean_text(body, "prefix", max_len=10)
    if not PREFIX_RE.match(prefix):
        raise ApiError(400, "Prefix may contain letters, digits and dashes (max 10)")
    nxt = clean_int(body, "next_number", 1, 999999999)
    tax_bp = clean_int(body, "default_tax_bp", 0, 10000)
    terms = clean_int(body, "terms_days", 0, 365)
    footer = clean_text(body, "footer", max_len=300)
    with tx(ctx.db):
        ctx.db.execute("UPDATE invoice_settings SET prefix=?, next_number=?, default_tax_bp=?, terms_days=?, footer=? "
                       "WHERE id=1", (prefix, nxt, tax_bp, terms, footer))
        audit(ctx.db, ctx.user.id, "settings.invoices", "settings", "invoice_settings", ip=ctx.req.ip)
    return dict(_settings(ctx.db))


# ---------------------------------------------------------------- list / read
def list_invoices(ctx):
    req = ctx.req

    def one(name):
        return (req.query.get(name, [""])[0] or "").strip()

    q = one("q")[:100]
    status = one("status")
    if status not in ("", "draft", "issued", "paid", "void"):
        raise ApiError(400, "Unknown status")
    params = {"status": status, "cust": one("customer_id")[:64], "ticket": one("ticket_id")[:64],
              "q": q, "pat": like_pattern(q)}
    page = dict(params, limit=req.query_int("limit", 50, 1, 200), offset=req.query_int("offset", 0, 0, 1000000))
    rows = ctx.db.execute(LIST_SQL, page).fetchall()
    total = ctx.db.execute(COUNT_SQL, params).fetchone()[0]
    owed = ctx.db.execute("SELECT COALESCE(SUM(total_cents), 0) FROM invoices WHERE status='issued'").fetchone()[0]
    return {"invoices": [dict(r) for r in rows], "total": total, "outstanding_cents": owed}


def get_invoice(ctx):
    return _load(ctx.db, ctx.params["id"])


# ---------------------------------------------------------------- create / edit
def create_invoice(ctx):
    body = ctx.req.json()
    db = ctx.db
    customer_id = body.get("customer_id")
    cust = db.execute("SELECT id, archived_at FROM customers WHERE id=?",
                      (customer_id if isinstance(customer_id, str) else "",)).fetchone()
    if cust is None:
        raise ApiError(404, "Customer not found")
    if cust["archived_at"] is not None:
        raise ApiError(400, "That customer is archived")
    ticket_id = body.get("ticket_id") or None
    if ticket_id is not None:
        t = db.execute("SELECT customer_id FROM tickets WHERE id=?", (ticket_id if isinstance(ticket_id, str) else "",)).fetchone()
        if t is None or t["customer_id"] != customer_id:
            raise ApiError(400, "The ticket must belong to the same customer")
    tax_bp, due, notes = _header(body, _settings(db))
    items = _parse_items(db, body.get("items"))
    sub, tax, total = totals(items, tax_bp)
    iid, ts = new_id(), now_iso()
    with tx(db):
        db.execute(
            "INSERT INTO invoices(id, customer_id, ticket_id, due_date, notes, tax_bp, subtotal_cents, tax_cents, "
            "total_cents, bill_to, created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, customer_id, ticket_id, due, notes, tax_bp, sub, tax, total, _bill_to(db, customer_id),
             ctx.user.id, ts, ts))
        _write_items(db, iid, items)
        audit(db, ctx.user.id, "invoice.created", "invoice", iid, ip=ctx.req.ip)
    return 201, _load(db, iid)


def update_invoice(ctx):
    body = ctx.req.json()
    db = ctx.db
    old = _load(db, ctx.params["id"])
    tax_bp, due, notes = _header(body, _settings(db), old)
    items = _parse_items(db, body["items"]) if "items" in body else None
    with tx(db):
        if _status_of(db, old["id"]) != "draft":
            raise ApiError(409, "Only draft invoices can be edited")
        if items is not None:
            _write_items(db, old["id"], items)
        stored = db.execute("SELECT qty_x100, unit_cents, taxable FROM invoice_items WHERE invoice_id=?",
                            (old["id"],)).fetchall()
        sub, tax, total = totals([dict(r) for r in stored], tax_bp)
        db.execute("UPDATE invoices SET due_date=?, notes=?, tax_bp=?, subtotal_cents=?, tax_cents=?, total_cents=?, "
                   "bill_to=?, updated_at=? WHERE id=?",
                   (due, notes, tax_bp, sub, tax, total, _bill_to(db, old["customer_id"]), now_iso(), old["id"]))
        audit(db, ctx.user.id, "invoice.updated", "invoice", old["id"], ip=ctx.req.ip)
    return _load(db, old["id"])


def delete_invoice(ctx):
    db = ctx.db
    old = _load(db, ctx.params["id"])
    with tx(db):
        if _status_of(db, old["id"]) != "draft":
            raise ApiError(409, "Only draft invoices can be deleted. Void an issued invoice instead.")
        db.execute("DELETE FROM invoices WHERE id=?", (old["id"],))
        audit(db, ctx.user.id, "invoice.deleted", "invoice", old["id"], ip=ctx.req.ip)
    return {"ok": True}


# ---------------------------------------------------------------- lifecycle
def _allocate_number(db):
    row = _settings(db)
    n = row["next_number"]
    while db.execute("SELECT 1 FROM invoices WHERE number=?", (row["prefix"] + str(n),)).fetchone():
        n += 1
    db.execute("UPDATE invoice_settings SET next_number=? WHERE id=1", (n + 1,))
    return row["prefix"] + str(n)


def issue_invoice(ctx):
    db = ctx.db
    old = _load(db, ctx.params["id"])
    ts = now_iso()
    with tx(db):
        if _status_of(db, old["id"]) != "draft":
            raise ApiError(409, "This invoice has already been issued")
        lines = db.execute("SELECT kind, ref_id, name, qty_x100, unit_cents, taxable FROM invoice_items "
                           "WHERE invoice_id=? ORDER BY position", (old["id"],)).fetchall()
        if not lines:
            raise ApiError(400, "Add at least one line before issuing")
        need = {}
        for ln in lines:
            if ln["kind"] == "inventory":
                need[ln["ref_id"]] = need.get(ln["ref_id"], 0) + ln["qty_x100"] // 100
        for item_id, qty in need.items():
            cur = db.execute("UPDATE inventory_items SET quantity = quantity - ?, updated_at = ? "
                             "WHERE id = ? AND quantity >= ?", (qty, ts, item_id, qty))
            if cur.rowcount != 1:
                have = db.execute("SELECT name, quantity FROM inventory_items WHERE id=?", (item_id,)).fetchone()
                raise ApiError(409, "Not enough stock for %s (have %d, need %d)" % (have["name"], have["quantity"], qty))
            db.execute("INSERT INTO inventory_movements(item_id, ts, user_id, delta, reason, ref) VALUES(?,?,?,?,?,?)",
                       (item_id, ts, ctx.user.id, -qty, "invoice", old["id"]))
        sub, tax, total = totals([dict(r) for r in lines], old["tax_bp"])
        number = _allocate_number(db)
        today = date.today()
        due = old["due_date"] or (today + timedelta(days=_settings(db)["terms_days"])).isoformat()
        db.execute("UPDATE invoices SET status='issued', number=?, issue_date=?, due_date=?, subtotal_cents=?, "
                   "tax_cents=?, total_cents=?, bill_to=?, updated_at=? WHERE id=?",
                   (number, today.isoformat(), due, sub, tax, total, _bill_to(db, old["customer_id"]), ts, old["id"]))
        audit(db, ctx.user.id, "invoice.issued", "invoice", old["id"], {"number": number}, ctx.req.ip)
    return _load(db, old["id"])


def pay_invoice(ctx):
    body = ctx.req.json()
    db = ctx.db
    old = _load(db, ctx.params["id"])
    method = clean_text(body, "method", max_len=40)
    ts = now_iso()
    with tx(db):
        if _status_of(db, old["id"]) != "issued":
            raise ApiError(409, "Only an issued invoice can be marked paid")
        db.execute("UPDATE invoices SET status='paid', paid_at=?, payment_method=?, updated_at=? WHERE id=?",
                   (ts, method, ts, old["id"]))
        if old["total_cents"] > 0:                            # cash in: its own ledger line carrying the invoice number
            books.add_entry(db, ctx.user.id, old["total_cents"], "invoice_payment",
                            "Invoice %s paid%s" % (old["number"], " (%s)" % method if method else ""),
                            "invoice", old["id"], old["number"], ts=ts)
        audit(db, ctx.user.id, "invoice.paid", "invoice", old["id"], ip=ctx.req.ip)
    return _load(db, old["id"])


def void_invoice(ctx):
    body = ctx.req.json()
    db = ctx.db
    old = _load(db, ctx.params["id"])
    reason = clean_text(body, "reason", max_len=200, required=True)
    ts = now_iso()
    with tx(db):
        status_before = _status_of(db, old["id"])
        if status_before not in ("issued", "paid"):
            raise ApiError(409, "Only an issued or paid invoice can be voided")
        if status_before == "paid" and old["total_cents"] > 0:     # the money is given back: reverse the payment line
            paid_line = db.execute("SELECT id FROM ledger_entries WHERE ref_type='invoice' AND ref_id=? "
                                   "AND category='invoice_payment' ORDER BY id DESC LIMIT 1", (old["id"],)).fetchone()
            books.add_entry(db, ctx.user.id, -old["total_cents"], "reversal",
                            "Invoice %s voided, payment reversed (%s)" % (old["number"], reason),
                            "invoice", old["id"], old["number"], reverses_id=paid_line["id"] if paid_line else None, ts=ts)
        for ln in db.execute("SELECT ref_id, qty_x100 FROM invoice_items WHERE invoice_id=? AND kind='inventory'",
                             (old["id"],)).fetchall():
            qty = ln["qty_x100"] // 100
            db.execute("UPDATE inventory_items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
                       (qty, ts, ln["ref_id"]))
            db.execute("INSERT INTO inventory_movements(item_id, ts, user_id, delta, reason, ref) VALUES(?,?,?,?,?,?)",
                       (ln["ref_id"], ts, ctx.user.id, qty, "invoice voided", old["id"]))
        db.execute("UPDATE invoices SET status='void', void_reason=?, updated_at=? WHERE id=?", (reason, ts, old["id"]))
        audit(db, ctx.user.id, "invoice.voided", "invoice", old["id"], ip=ctx.req.ip)
    return _load(db, old["id"])
