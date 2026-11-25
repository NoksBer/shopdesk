"""Parts inventory: cost, sale price, stock level. Stock only changes through recorded movements."""
import sqlite3

from .. import books
from ..audit import audit
from ..db import tx
from ..util import (MAX_CENTS, ApiError, clean_flag, clean_int, clean_text, like_pattern, new_id, now_iso)

# part_no is the automatic SKU (1, 2, 3 ...). "sku" is an optional free-text supplier / manufacturer part number.
COLS = ("id, part_no, sku, name, description, cost_cents, price_cents, quantity, reorder_level, taxable, active, "
        "created_at, updated_at")

LIST_SQL = ("SELECT " + COLS + """ FROM inventory_items
WHERE (active = 1 OR :inactive = 1)
  AND (:low = 0 OR (active = 1 AND quantity <= reorder_level))
  AND (:q = '' OR name LIKE :pat ESCAPE '\\' OR sku LIKE :pat ESCAPE '\\' OR description LIKE :pat ESCAPE '\\'
       OR CAST(part_no AS TEXT) LIKE :pat ESCAPE '\\')
ORDER BY CASE WHEN CAST(part_no AS TEXT) = :q THEN 0 ELSE 1 END,      -- an exact SKU match always comes first
         active DESC,
         CASE WHEN :sort = 'sku' THEN part_no END,
         name COLLATE NOCASE
LIMIT :limit""")

GET_SQL = "SELECT " + COLS + " FROM inventory_items WHERE id = ?"


def register(router):
    router.add("GET", "/api/inventory", list_items, "inventory.read")
    router.add("POST", "/api/inventory", create_item, "inventory.write")
    router.add("GET", "/api/inventory/<id>", get_item, "inventory.read")
    router.add("PUT", "/api/inventory/<id>", update_item, "inventory.write")
    router.add("DELETE", "/api/inventory/<id>", deactivate_item, "inventory.write")
    router.add("POST", "/api/inventory/<id>/adjust", adjust_item, "inventory.write")


def _present(ctx, row):
    out = dict(row)
    if not ctx.user.can("inventory.write"):
        out.pop("cost_cents", None)          # what the shop paid is only for people who manage stock
    out["low_stock"] = bool(row["active"] and row["quantity"] <= row["reorder_level"])
    return out


def _row(db, item_id):
    row = db.execute(GET_SQL, (item_id,)).fetchone()
    if row is None:
        raise ApiError(404, "Not found")
    return row


def _parse(body):
    return {
        "sku": clean_text(body, "sku", max_len=60),
        "name": clean_text(body, "name", max_len=120, required=True),
        "description": clean_text(body, "description", max_len=300),
        "cost_cents": clean_int(body, "cost_cents", 0, MAX_CENTS, 0),
        "price_cents": clean_int(body, "price_cents", 0, MAX_CENTS),
        "reorder_level": clean_int(body, "reorder_level", 0, 1000000, 0),
        "taxable": clean_flag(body, "taxable", True),
    }


def list_items(ctx):
    req = ctx.req
    q = (req.query.get("q", [""])[0] or "").strip()[:100]
    params = {"q": q, "pat": like_pattern(q),
              "inactive": 1 if req.query.get("include_inactive", ["0"])[0] == "1" else 0,
              "low": 1 if req.query.get("low_stock", ["0"])[0] == "1" else 0,
              "sort": "sku" if req.query.get("sort", [""])[0] == "sku" else "name",
              "limit": req.query_int("limit", 200, 1, 500)}
    return {"items": [_present(ctx, r) for r in ctx.db.execute(LIST_SQL, params).fetchall()]}


def get_item(ctx):
    row = _row(ctx.db, ctx.params["id"])
    out = _present(ctx, row)
    moves = ctx.db.execute(
        "SELECT m.id, m.ts, m.delta, m.reason, m.ref, u.name AS user_name FROM inventory_movements m "
        "LEFT JOIN users u ON u.id = m.user_id WHERE m.item_id = ? ORDER BY m.id DESC LIMIT 50", (row["id"],))
    out["movements"] = [dict(m) for m in moves]
    return out


def create_item(ctx):
    body = ctx.req.json()
    data = _parse(body)
    qty = clean_int(body, "quantity", 0, 10000000, 0)
    iid, ts = new_id(), now_iso()
    try:
        with tx(ctx.db):
            # The next SKU is taken inside the same transaction as the insert: if anything fails, the number is
            # not consumed, and two people adding parts at once can never receive the same number.
            part_no = ctx.db.execute("SELECT next_number FROM inventory_counter WHERE id=1").fetchone()[0]
            ctx.db.execute("UPDATE inventory_counter SET next_number = ? WHERE id=1", (part_no + 1,))
            ctx.db.execute(
                "INSERT INTO inventory_items(id, part_no, sku, name, description, cost_cents, price_cents, quantity, "
                "reorder_level, taxable, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (iid, part_no, data["sku"], data["name"], data["description"], data["cost_cents"], data["price_cents"],
                 qty, data["reorder_level"], data["taxable"], ts, ts))
            if qty:
                ctx.db.execute("INSERT INTO inventory_movements(item_id, ts, user_id, delta, reason) VALUES(?,?,?,?,?)",
                               (iid, ts, ctx.user.id, qty, "opening stock"))
                if data["cost_cents"]:                       # stock bought = cash out, recorded in the ledger
                    books.add_entry(ctx.db, ctx.user.id, -qty * data["cost_cents"], "inventory_purchase",
                                    "Stock purchase: SKU %d · %s × %d @ %s each" % (
                                        part_no, data["name"], qty, books.money_text(data["cost_cents"])),
                                    "inventory", iid, "SKU %d" % part_no, ts=ts)
            audit(ctx.db, ctx.user.id, "inventory.created", "inventory", iid, {"sku": part_no}, ctx.req.ip)
    except sqlite3.IntegrityError:
        raise ApiError(409, "An item with that supplier part number already exists")
    return 201, _present(ctx, _row(ctx.db, iid))


def update_item(ctx):
    """Edits details only. Quantity changes go through /adjust so they are recorded."""
    body = ctx.req.json()
    old = _row(ctx.db, ctx.params["id"])
    data = _parse(body)
    active = clean_flag(body, "active", bool(old["active"]))
    try:
        with tx(ctx.db):
            ctx.db.execute(
                "UPDATE inventory_items SET sku=?, name=?, description=?, cost_cents=?, price_cents=?, "
                "reorder_level=?, taxable=?, active=?, updated_at=? WHERE id=?",
                (data["sku"], data["name"], data["description"], data["cost_cents"], data["price_cents"],
                 data["reorder_level"], data["taxable"], active, now_iso(), old["id"]))
            audit(ctx.db, ctx.user.id, "inventory.updated", "inventory", old["id"], ip=ctx.req.ip)
    except sqlite3.IntegrityError:
        raise ApiError(409, "An item with that supplier part number already exists")
    return _present(ctx, _row(ctx.db, old["id"]))


def deactivate_item(ctx):
    old = _row(ctx.db, ctx.params["id"])
    with tx(ctx.db):
        ctx.db.execute("UPDATE inventory_items SET active=0, updated_at=? WHERE id=?", (now_iso(), old["id"]))
        audit(ctx.db, ctx.user.id, "inventory.deactivated", "inventory", old["id"], ip=ctx.req.ip)
    return {"ok": True}


def adjust_item(ctx):
    """Add or remove stock (receiving parts, counting errors, damage). Never drops below zero."""
    body = ctx.req.json()
    old = _row(ctx.db, ctx.params["id"])
    delta = clean_int(body, "delta", -10000000, 10000000)
    if delta == 0:
        raise ApiError(400, "delta cannot be zero")
    reason = clean_text(body, "reason", max_len=120, required=True)
    record = clean_flag(body, "record_purchase", False)
    if record and delta < 0:
        raise ApiError(400, "Only added stock can be recorded as a purchase")
    unit_cost = clean_int(body, "unit_cost_cents", 0, MAX_CENTS, old["cost_cents"]) if record else 0
    ts = now_iso()
    with tx(ctx.db):
        cur = ctx.db.execute("UPDATE inventory_items SET quantity = quantity + ?, updated_at = ? "
                             "WHERE id = ? AND quantity + ? >= 0", (delta, ts, old["id"], delta))
        if cur.rowcount != 1:
            raise ApiError(409, "Stock cannot go below zero (currently %d)" % old["quantity"])
        ctx.db.execute("INSERT INTO inventory_movements(item_id, ts, user_id, delta, reason) VALUES(?,?,?,?,?)",
                       (old["id"], ts, ctx.user.id, delta, reason))
        if record and unit_cost:                              # bought more stock = cash out
            books.add_entry(ctx.db, ctx.user.id, -delta * unit_cost, "inventory_purchase",
                            "Stock purchase: SKU %d · %s × %d @ %s each (%s)" % (
                                old["part_no"], old["name"], delta, books.money_text(unit_cost), reason),
                            "inventory", old["id"], "SKU %d" % old["part_no"], ts=ts)
        audit(ctx.db, ctx.user.id, "inventory.adjusted", "inventory", old["id"], {"delta": delta}, ctx.req.ip)
    return get_item(ctx)
