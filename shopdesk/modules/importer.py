"""CSV data import for customers, inventory items and services (owners and managers: permission data.import).

The browser reads the CSV file and sends rows in small batches, already keyed by ShopDesk field name. Nothing here
connects to QuickBooks or any online service, and the standard library is all that is used. Every value is treated as
plain text: trimmed, stripped of control characters and capped in length, then bound as an SQL parameter. Rows can only
be written to the importing shop's own database (ctx.db).

Deliberately NOT importable: invoices, payments, tickets, ledger entries, and stock quantities (they would change stock
levels and the cash balance). Imported inventory items start with a quantity of 0.
"""
import csv
import io
import re
import sqlite3
from decimal import Decimal

from ..audit import audit
from ..db import tx
from ..http_util import Response
from ..util import EMAIL_RE, MAX_CENTS, ApiError, clean_text, new_id, now_iso

MAX_PREVIEW_ROWS = 50
MAX_BATCH_ROWS = 200
MAX_TOTAL_ROWS = 50000
MODES = ("skip", "update")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# (key, label, required, max length, normalized header names that map to it automatically)
FIELDS = {
    "customers": [
        ("name", "Name / business name", True, 120,
         ["name", "customer", "customername", "company", "companyname", "fullname", "displayname", "customerfullname"]),
        ("contact_name", "Contact person", False, 120, ["contactname", "contact", "contactperson", "primarycontact", "attn"]),
        ("email", "Email", False, 254, ["email", "mainemail", "emailaddress", "e-mail", "email1"]),
        ("phone", "Phone", False, 40, ["phone", "mainphone", "phonenumber", "phonenumbers", "telephone", "mobile", "cell", "phone1"]),
        ("address", "Address", False, 300, ["address", "billto", "billingaddress", "billto1", "streetaddress", "billaddr1"]),
        ("type", "Type (individual or business)", False, 20, ["type", "customertype"]),
        ("notes", "Notes", False, 2000, ["notes", "note", "memo", "comments"]),
    ],
    "inventory": [
        ("name", "Item name", True, 120, ["name", "item", "itemname", "itemnamenumber", "product", "productname", "partname"]),
        ("sku", "Supplier / manufacturer part number", False, 60,
         ["sku", "partnumber", "partno", "mpn", "manufacturerspartnumber", "manufacturerpartnumber", "itemnumber", "supplierpartnumber"]),
        ("description", "Description", False, 300, ["description", "salesdescription", "desc", "details"]),
        ("cost", "Cost", False, 20, ["cost", "purchasecost", "unitcost", "costprice", "buyprice"]),
        ("price", "Sale price", False, 20, ["price", "salesprice", "saleprice", "unitprice", "rate", "sellprice", "retailprice"]),
        ("reorder_level", "Reorder level", False, 10, ["reorderlevel", "reorderpt", "reorderptmin", "reorderpoint", "minimum", "min"]),
        ("taxable", "Taxable (yes or no)", False, 10, ["taxable", "tax", "istaxable", "taxed"]),
    ],
    "services": [
        ("name", "Service name", True, 120, ["name", "item", "itemname", "service", "servicename", "itemnamenumber"]),
        ("description", "Description", False, 300, ["description", "salesdescription", "desc", "details"]),
        ("price", "Price", False, 20, ["price", "salesprice", "rate", "unitprice", "amount", "fee"]),
        ("taxable", "Taxable (yes or no)", False, 10, ["taxable", "tax", "istaxable", "taxed"]),
    ],
}
LABELS = {"customers": "Customers", "inventory": "Inventory items", "services": "Services"}
NOTES = {
    "customers": "Likely duplicates are found by email, phone number or name.",
    "inventory": "Likely duplicates are found by supplier part number or name. Stock quantities are not imported (items "
                 "start at 0 in stock) because that would change stock levels and the cash ledger.",
    "services": "Likely duplicates are found by name.",
}
SAMPLES = {
    "customers": [["Name", "Contact person", "Email", "Phone", "Address", "Type", "Notes"],
                  ["Acme Dental", "Pat Jones", "pat@acme.example", "555-0100", "12 Main St", "business", "Prefers email"],
                  ["Sam Rivera", "", "sam@example.com", "555-0111", "", "individual", ""]],
    "inventory": [["Item name", "Supplier part number", "Description", "Cost", "Sale price", "Reorder level", "Taxable"],
                  ["Laptop battery 6-cell", "BAT-6C-01", "Fits most 15 inch laptops", "24.50", "59.99", "3", "yes"],
                  ["USB-C cable 1m", "", "", "2.10", "9.99", "10", "yes"]],
    "services": [["Service name", "Description", "Price", "Taxable"],
                 ["Diagnostic fee", "Up to 1 hour bench time", "49.00", "no"],
                 ["Screen replacement labour", "", "89.00", "yes"]],
}


def register(router):
    router.add("GET", "/api/import/types", list_types, "data.import")
    router.add("GET", "/api/import/history", history, "data.import")
    router.add("GET", "/api/import/sample/<kind>", sample, "data.import")
    router.add("POST", "/api/import/<kind>/preview", preview, "data.import")
    router.add("POST", "/api/import/<kind>/start", start, "data.import")
    router.add("POST", "/api/import/<kind>/batch", batch, "data.import")
    router.add("POST", "/api/import/<kind>/finish", finish, "data.import")


# ---------------------------------------------------------------- catalogue
def _kind(ctx):
    kind = ctx.params["kind"]
    if kind not in FIELDS:
        raise ApiError(404, "Not found")
    return kind


def list_types(ctx):
    return {"types": [{"key": k, "label": LABELS[k], "note": NOTES[k],
                       "fields": [{"key": f[0], "label": f[1], "required": f[2], "aliases": f[4]} for f in FIELDS[k]]}
                      for k in ("customers", "inventory", "services")],
            "max_batch_rows": MAX_BATCH_ROWS, "max_preview_rows": MAX_PREVIEW_ROWS, "max_total_rows": MAX_TOTAL_ROWS}


def sample(ctx):
    kind = _kind(ctx)
    out = io.StringIO()
    csv.writer(out, lineterminator="\r\n").writerows(SAMPLES[kind])
    return Response(200, out.getvalue().encode("utf-8"), "text/csv; charset=utf-8",
                    [("Content-Disposition", 'attachment; filename="shopdesk-%s-sample.csv"' % kind)])


# ---------------------------------------------------------------- value cleaning
class RowError(Exception):
    pass


def _text(values, key, limit):
    raw = values.get(key)
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise RowError("%s must be text" % key)
    value = CONTROL_RE.sub("", raw).strip()
    return value[:limit]                                   # capped, never an error: long notes just get cut


def _money(text, label):
    if text == "":
        return None
    t = text.replace("$", "").replace(",", "").replace(" ", "")
    if not re.fullmatch(r"\d+(\.\d{1,2})?|\.\d{1,2}", t):
        raise RowError("%s is not a valid amount (use a number such as 49.99)" % label)
    cents = int(Decimal(t) * 100)
    if cents > MAX_CENTS:
        raise RowError("%s is too large" % label)
    return cents


def _flag(text, label):
    if text == "":
        return None
    low = text.lower()
    if low in ("yes", "y", "true", "t", "1", "x", "taxable"):
        return 1
    if low in ("no", "n", "false", "f", "0", "non", "nontaxable", "non-taxable"):
        return 0
    raise RowError("%s must be yes or no" % label)


def _int(text, label, maximum):
    if text == "":
        return None
    if not re.fullmatch(r"\d{1,9}", text.split(".")[0]) or ("." in text and text.split(".", 1)[1].strip("0")):
        raise RowError("%s must be a whole number" % label)
    n = int(text.split(".")[0])
    if n > maximum:
        raise RowError("%s is too large" % label)
    return n


def _norm_phone(text):
    digits = re.sub(r"\D", "", text or "")
    return digits[-10:] if len(digits) >= 7 else ""


def _clean_row(kind, values):
    """Validate one row. Returns a dict of cleaned values (None means 'not provided'); raises RowError."""
    if not isinstance(values, dict):
        raise RowError("row is not valid")
    t = {f[0]: _text(values, f[0], f[3]) for f in FIELDS[kind]}
    if not t["name"]:
        raise RowError("Name is required")
    if kind == "customers":
        if t["email"] and not EMAIL_RE.match(t["email"]):
            raise RowError("Email is not a valid address")
        t["email"] = t["email"].lower()
        low = t["type"].lower()
        if low in ("business", "company", "organization", "organisation", "b"):
            t["type"] = "business"
        elif low in ("individual", "person", "personal", "residential", "i"):
            t["type"] = "individual"
        elif low == "":
            t["type"] = None
        else:
            raise RowError("Type must be individual or business")
        return t
    t["price"] = _money(t["price"], "Price")
    t["taxable"] = _flag(t["taxable"], "Taxable")
    if kind == "inventory":
        t["cost"] = _money(t["cost"], "Cost")
        t["reorder_level"] = _int(t["reorder_level"], "Reorder level", 1000000)
    return t


# ---------------------------------------------------------------- the row engine
def _maps(db, kind):
    if kind == "customers":
        m = {"email": {}, "phone": {}, "name": {}}
        for r in db.execute("SELECT id, name, email, phone FROM customers ORDER BY created_at, id"):
            if r["email"]:
                m["email"].setdefault(r["email"].lower(), r["id"])
            if _norm_phone(r["phone"]):
                m["phone"].setdefault(_norm_phone(r["phone"]), r["id"])
            m["name"].setdefault(r["name"].lower(), r["id"])
        return m
    if kind == "inventory":
        m = {"sku": {}, "name": {}}
        for r in db.execute("SELECT id, sku, name FROM inventory_items ORDER BY created_at, id"):
            if r["sku"]:
                m["sku"].setdefault(r["sku"].lower(), r["id"])
            m["name"].setdefault(r["name"].lower(), r["id"])
        return m
    return {"name": {r["name"].lower(): r["id"] for r in db.execute("SELECT id, name FROM services")}}


def _find(kind, maps, t):
    """Returns (existing id, matched-by) or (None, None)."""
    if kind == "customers":
        if t["email"] and t["email"] in maps["email"]:
            return maps["email"][t["email"]], "email"
        if _norm_phone(t["phone"]) and _norm_phone(t["phone"]) in maps["phone"]:
            return maps["phone"][_norm_phone(t["phone"])], "phone number"
        if t["name"].lower() in maps["name"]:
            return maps["name"][t["name"].lower()], "name"
    elif kind == "inventory":
        if t["sku"] and t["sku"].lower() in maps["sku"]:
            return maps["sku"][t["sku"].lower()], "supplier part number"
        if t["name"].lower() in maps["name"]:
            return maps["name"][t["name"].lower()], "name"
    elif t["name"].lower() in maps["name"]:
        return maps["name"][t["name"].lower()], "name"
    return None, None


def _remember(kind, maps, t, row_id):
    if kind == "customers":
        if t["email"]:
            maps["email"].setdefault(t["email"], row_id)
        if _norm_phone(t["phone"]):
            maps["phone"].setdefault(_norm_phone(t["phone"]), row_id)
    elif kind == "inventory" and t["sku"]:
        maps["sku"].setdefault(t["sku"].lower(), row_id)
    maps["name"].setdefault(t["name"].lower(), row_id)


def _keep(new, old):
    """On update an empty cell never wipes out what the shop already has."""
    return old if new is None or new == "" else new


def _create(db, kind, t, user_id, ts):
    rid = new_id()
    if kind == "customers":
        db.execute("INSERT INTO customers(id, type, name, contact_name, email, phone, address, contact_pref, notes, "
                   "created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (rid, t["type"] or "individual", t["name"], t["contact_name"], t["email"], t["phone"], t["address"],
                    "email", t["notes"], user_id, ts, ts))
    elif kind == "inventory":
        if t["price"] is None:
            raise RowError("Sale price is required for a new item")
        part_no = db.execute("SELECT next_number FROM inventory_counter WHERE id=1").fetchone()[0]
        db.execute("UPDATE inventory_counter SET next_number = ? WHERE id=1", (part_no + 1,))
        db.execute("INSERT INTO inventory_items(id, part_no, sku, name, description, cost_cents, price_cents, quantity, "
                   "reorder_level, taxable, created_at, updated_at) VALUES(?,?,?,?,?,?,?,0,?,?,?,?)",
                   (rid, part_no, t["sku"], t["name"], t["description"], t["cost"] or 0, t["price"],
                    t["reorder_level"] or 0, 1 if t["taxable"] is None else t["taxable"], ts, ts))
    else:
        if t["price"] is None:
            raise RowError("Price is required for a new service")
        db.execute("INSERT INTO services(id, name, description, price_cents, taxable, created_at, updated_at) "
                   "VALUES(?,?,?,?,?,?,?)",
                   (rid, t["name"], t["description"], t["price"], 1 if t["taxable"] is None else t["taxable"], ts, ts))
    return rid


def _update(db, kind, rid, t, ts):
    if kind == "customers":
        o = db.execute("SELECT * FROM customers WHERE id=?", (rid,)).fetchone()
        db.execute("UPDATE customers SET type=?, name=?, contact_name=?, email=?, phone=?, address=?, notes=?, updated_at=? "
                   "WHERE id=?", (_keep(t["type"], o["type"]), t["name"], _keep(t["contact_name"], o["contact_name"]),
                                  _keep(t["email"], o["email"]), _keep(t["phone"], o["phone"]),
                                  _keep(t["address"], o["address"]), _keep(t["notes"], o["notes"]), ts, rid))
    elif kind == "inventory":
        o = db.execute("SELECT * FROM inventory_items WHERE id=?", (rid,)).fetchone()
        db.execute("UPDATE inventory_items SET sku=?, name=?, description=?, cost_cents=?, price_cents=?, reorder_level=?, "
                   "taxable=?, updated_at=? WHERE id=?",
                   (_keep(t["sku"], o["sku"]), t["name"], _keep(t["description"], o["description"]),
                    _keep(t["cost"], o["cost_cents"]), _keep(t["price"], o["price_cents"]),
                    _keep(t["reorder_level"], o["reorder_level"]), _keep(t["taxable"], o["taxable"]), ts, rid))
    else:
        o = db.execute("SELECT * FROM services WHERE id=?", (rid,)).fetchone()
        db.execute("UPDATE services SET name=?, description=?, price_cents=?, taxable=?, updated_at=? WHERE id=?",
                   (t["name"], _keep(t["description"], o["description"]), _keep(t["price"], o["price_cents"]),
                    _keep(t["taxable"], o["taxable"]), ts, rid))


def _process(db, kind, mode, rows, user_id, dry):
    """Run rows through validation, duplicate detection and (unless dry) the writes. Returns result dicts.
    The caller wraps real runs in one transaction; each row has its own savepoint so one bad row never blocks the rest."""
    maps = _maps(db, kind)
    ts = now_iso()
    out = []
    for item in rows:
        line = item.get("line") if isinstance(item, dict) else None
        res = {"line": line if isinstance(line, int) and not isinstance(line, bool) else None, "status": "error", "message": ""}
        out.append(res)
        try:
            t = _clean_row(kind, item.get("values") if isinstance(item, dict) else None)
            existing, by = _find(kind, maps, t)
            if existing is not None and mode == "skip":
                res.update(status="would skip" if dry else "skipped", message="Already exists (matched by %s)" % by)
                continue
            if dry:
                if existing is None:
                    if kind != "customers" and t["price"] is None:
                        raise RowError("Sale price is required for a new item" if kind == "inventory"
                                       else "Price is required for a new service")
                    _remember(kind, maps, t, "new")
                    res.update(status="would create", message="New record")
                else:
                    res.update(status="would update", message="Updates the existing record (matched by %s)" % by)
                continue
            db.execute("SAVEPOINT import_row")
            try:
                if existing is None:
                    rid = _create(db, kind, t, user_id, ts)
                    _remember(kind, maps, t, rid)
                    res.update(status="created", message="Created")
                else:
                    _update(db, kind, existing, t, ts)
                    res.update(status="updated", message="Updated the existing record (matched by %s)" % by)
                db.execute("RELEASE import_row")
            except (RowError, sqlite3.IntegrityError) as e:
                db.execute("ROLLBACK TO import_row")
                db.execute("RELEASE import_row")
                if isinstance(e, sqlite3.IntegrityError):
                    raise RowError("Conflicts with an existing record (for example a duplicate supplier part number)")
                raise
        except RowError as e:
            res.update(status="error", message=str(e))
    return out


def _rows(body, limit):
    rows = body.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ApiError(400, "rows must be a non-empty list")
    if len(rows) > limit:
        raise ApiError(400, "Send at most %d rows at a time" % limit)
    for r in rows:
        if not isinstance(r, dict) or not isinstance(r.get("values"), dict):
            raise ApiError(400, "each row needs a values object")
        for k, v in r["values"].items():
            if not isinstance(k, str) or len(k) > 40 or (v is not None and (not isinstance(v, str) or len(v) > 5000)):
                raise ApiError(400, "row values must be text of at most 5000 characters")
    return rows


def _mode(body):
    mode = body.get("mode", "skip")
    if mode not in MODES:
        raise ApiError(400, "mode must be skip or update")
    return mode


def _summary(results):
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return counts


# ---------------------------------------------------------------- endpoints
def preview(ctx):
    """Validate up to 50 rows and say what WOULD happen. Writes nothing."""
    kind = _kind(ctx)
    body = ctx.req.json()
    results = _process(ctx.db, kind, _mode(body), _rows(body, MAX_PREVIEW_ROWS), ctx.user.id, True)
    return {"results": results, "summary": _summary(results)}


def start(ctx):
    kind = _kind(ctx)
    body = ctx.req.json()
    mode = _mode(body)
    total = body.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or not 1 <= total <= MAX_TOTAL_ROWS:
        raise ApiError(400, "total must be between 1 and %d rows" % MAX_TOTAL_ROWS)
    filename = clean_text(body, "filename", max_len=200)
    iid, ts = new_id(), now_iso()
    with tx(ctx.db):
        ctx.db.execute("INSERT INTO import_history(id, kind, filename, mode, total_rows, created_by, created_at) "
                       "VALUES(?,?,?,?,?,?,?)", (iid, kind, filename, mode, total, ctx.user.id, ts))
        audit(ctx.db, ctx.user.id, "import.started", "import", iid, {"kind": kind, "mode": mode, "rows": total}, ctx.req.ip)
    return 201, {"import_id": iid}


def _load_import(ctx, kind, body):
    iid = body.get("import_id")
    row = ctx.db.execute("SELECT * FROM import_history WHERE id=? AND kind=?", (iid if isinstance(iid, str) else "", kind)).fetchone()
    if row is None:
        raise ApiError(404, "Import not found")
    return row


def batch(ctx):
    kind = _kind(ctx)
    body = ctx.req.json()
    ctx.app.limiter.check("import-batch:" + ctx.user.id, 600, 3600)
    imp = _load_import(ctx, kind, body)
    if imp["status"] != "running":
        raise ApiError(409, "This import has already finished")
    rows = _rows(body, MAX_BATCH_ROWS)
    with tx(ctx.db):
        results = _process(ctx.db, kind, imp["mode"], rows, ctx.user.id, False)
        c = _summary(results)
        ctx.db.execute("UPDATE import_history SET created=created+?, updated=updated+?, skipped=skipped+?, failed=failed+? "
                       "WHERE id=?", (c.get("created", 0), c.get("updated", 0), c.get("skipped", 0), c.get("error", 0), imp["id"]))
    return {"results": results, "summary": c}


def finish(ctx):
    kind = _kind(ctx)
    imp = _load_import(ctx, kind, ctx.req.json())
    with tx(ctx.db):
        ctx.db.execute("UPDATE import_history SET status='done', finished_at=? WHERE id=? AND status='running'",
                       (now_iso(), imp["id"]))
        fresh = ctx.db.execute("SELECT * FROM import_history WHERE id=?", (imp["id"],)).fetchone()
        audit(ctx.db, ctx.user.id, "import.completed", "import", imp["id"],
              {"kind": kind, "created": fresh["created"], "updated": fresh["updated"], "skipped": fresh["skipped"],
               "failed": fresh["failed"]}, ctx.req.ip)
    return {"import": dict(fresh)}


def history(ctx):
    rows = ctx.db.execute(
        "SELECT h.id, h.kind, h.filename, h.mode, h.total_rows, h.created, h.updated, h.skipped, h.failed, h.status, "
        "h.created_at, h.finished_at, u.name AS user_name FROM import_history h LEFT JOIN users u ON u.id = h.created_by "
        "ORDER BY h.created_at DESC, h.id DESC LIMIT 50").fetchall()
    return {"imports": [dict(r) for r in rows]}
