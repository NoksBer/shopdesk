"""Equipment / asset records with shop-configurable, profile-driven extra fields."""
import json
import re

from ..audit import audit
from ..db import tx
from ..util import ApiError, clean_date, clean_text, like_pattern, new_id, now_iso

FIELD_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
FIELD_TYPES = ("text", "number", "date")
COLUMN_KEYS = ("serial_number",)       # stored in a real column, not in the JSON `fields`

SELECT_ONE = """
SELECT e.id, e.customer_id, c.name AS customer_name, e.name, e.serial_number, e.description,
       e.purchase_date, e.warranty_expires, e.fields, e.created_at, e.updated_at, e.archived_at
FROM equipment e JOIN customers c ON c.id = e.customer_id WHERE e.id = ?
"""

LIST_SQL = """
SELECT e.id, e.customer_id, c.name AS customer_name, e.name, e.serial_number, e.description,
       e.purchase_date, e.warranty_expires, e.fields, e.created_at, e.updated_at, e.archived_at
FROM equipment e JOIN customers c ON c.id = e.customer_id
WHERE (e.archived_at IS NULL OR :arch = 1)
  AND (:cust = '' OR e.customer_id = :cust)
  AND (:q = '' OR e.name LIKE :pat ESCAPE '\\' OR e.serial_number LIKE :pat ESCAPE '\\'
       OR e.fields LIKE :pat ESCAPE '\\' OR c.name LIKE :pat ESCAPE '\\')
ORDER BY e.name COLLATE NOCASE, e.id
LIMIT :limit OFFSET :offset
"""


def register(router):
    # "/api/equipment/fields" must be registered before "/api/equipment/<id>"
    router.add("GET", "/api/equipment/fields", get_fields, "equipment.read")
    router.add("PUT", "/api/equipment/fields", put_fields, "business.manage")
    router.add("GET", "/api/equipment", list_equipment, "equipment.read")
    router.add("POST", "/api/equipment", create_equipment, "equipment.write")
    router.add("GET", "/api/equipment/<id>", get_equipment, "equipment.read")
    router.add("PUT", "/api/equipment/<id>", update_equipment, "equipment.write")
    router.add("DELETE", "/api/equipment/<id>", archive_equipment, "equipment.write")


def _field_defs(db):
    row = db.execute("SELECT value FROM settings WHERE key='equipment_fields'").fetchone()
    return json.loads(row["value"]) if row else []


def _dynamic_defs(db):
    return [f for f in _field_defs(db) if f["key"] not in COLUMN_KEYS]


def _out(row):
    d = dict(row)
    d["fields"] = json.loads(d["fields"] or "{}")
    return d


def get_fields(ctx):
    term = ctx.db.execute("SELECT value FROM settings WHERE key='terminology'").fetchone()
    return {"fields": _field_defs(ctx.db), "terminology": json.loads(term["value"]) if term else {}}


def put_fields(ctx):
    raw = ctx.req.json().get("fields")
    if not isinstance(raw, list) or len(raw) > 30:
        raise ApiError(400, "fields must be a list of at most 30 items")
    seen, clean = set(), []
    for item in raw:
        if not isinstance(item, dict):
            raise ApiError(400, "each field must be an object")
        key = item.get("key")
        label = clean_text(item, "label", max_len=60, required=True)
        ftype = item.get("type", "text")
        if not isinstance(key, str) or not FIELD_KEY_RE.match(key):
            raise ApiError(400, "field key must be lowercase letters, digits and underscores")
        if key in seen:
            raise ApiError(400, "duplicate field key: " + key)
        if ftype not in FIELD_TYPES:
            raise ApiError(400, "field type must be one of: " + ", ".join(FIELD_TYPES))
        seen.add(key)
        clean.append({"key": key, "label": label, "type": ftype})
    with tx(ctx.db):
        ctx.db.execute("INSERT INTO settings(key, value) VALUES('equipment_fields', ?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(clean),))
        audit(ctx.db, ctx.user.id, "settings.equipment_fields", "settings", "equipment_fields",
              {"count": len(clean)}, ctx.req.ip)
    return {"fields": clean}


def _validate_values(defs, raw):
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ApiError(400, "fields must be an object")
    by_key = {f["key"]: f for f in defs}
    out = {}
    for key, value in raw.items():
        if key not in by_key:
            raise ApiError(400, "Unknown field: " + str(key)[:40])
        if value is None or value == "":
            continue
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise ApiError(400, "%s must be text or a number" % key)
        text = str(value).strip()
        if len(text) > 500:
            raise ApiError(400, "%s is too long" % key)
        kind = by_key[key]["type"]
        if kind == "number":
            try:
                float(text)
            except ValueError:
                raise ApiError(400, "%s must be a number" % key)
        elif kind == "date":
            clean_date({key: text}, key)
        out[key] = text
    return out


def _require_customer(db, customer_id):
    if not isinstance(customer_id, str):
        raise ApiError(400, "customer_id is required")
    row = db.execute("SELECT id, archived_at FROM customers WHERE id=?", (customer_id,)).fetchone()
    if row is None:
        raise ApiError(404, "Customer not found")
    if row["archived_at"] is not None:
        raise ApiError(400, "That customer is archived")
    return row


def _parse(ctx, body):
    return {
        "name": clean_text(body, "name", max_len=120, required=True),
        "serial_number": clean_text(body, "serial_number", max_len=80),
        "description": clean_text(body, "description", max_len=1000),
        "purchase_date": clean_date(body, "purchase_date"),
        "warranty_expires": clean_date(body, "warranty_expires"),
        "fields": _validate_values(_dynamic_defs(ctx.db), body.get("fields")),
    }


def _get_row(db, equipment_id):
    row = db.execute(SELECT_ONE, (equipment_id,)).fetchone()
    if row is None:
        raise ApiError(404, "Not found")
    return row


def list_equipment(ctx):
    req = ctx.req
    q = (req.query.get("q", [""])[0] or "").strip()[:100]
    params = {"arch": 1 if req.query.get("include_archived", ["0"])[0] == "1" else 0,
              "cust": (req.query.get("customer_id", [""])[0] or "")[:64],
              "q": q, "pat": like_pattern(q),
              "limit": req.query_int("limit", 50, 1, 200), "offset": req.query_int("offset", 0, 0, 1000000)}
    rows = ctx.db.execute(LIST_SQL, params).fetchall()
    return {"equipment": [_out(r) for r in rows]}


def create_equipment(ctx):
    body = ctx.req.json()
    _require_customer(ctx.db, body.get("customer_id"))
    data = _parse(ctx, body)
    eid, ts = new_id(), now_iso()
    with tx(ctx.db):
        ctx.db.execute(
            "INSERT INTO equipment(id, customer_id, name, serial_number, description, purchase_date, "
            "warranty_expires, fields, created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (eid, body["customer_id"], data["name"], data["serial_number"], data["description"],
             data["purchase_date"], data["warranty_expires"], json.dumps(data["fields"], sort_keys=True),
             ctx.user.id, ts, ts))
        audit(ctx.db, ctx.user.id, "equipment.created", "equipment", eid,
              {"customer_id": body["customer_id"]}, ctx.req.ip)
    return 201, _out(_get_row(ctx.db, eid))


def get_equipment(ctx):
    return _out(_get_row(ctx.db, ctx.params["id"]))


def update_equipment(ctx):
    body = ctx.req.json()
    old = _get_row(ctx.db, ctx.params["id"])
    customer_id = body.get("customer_id", old["customer_id"])
    if customer_id != old["customer_id"]:
        _require_customer(ctx.db, customer_id)
    data = _parse(ctx, body)
    archived = body.get("archived")
    if archived is not None and not isinstance(archived, bool):
        raise ApiError(400, "archived must be true or false")
    new_fields = json.dumps(data["fields"], sort_keys=True)
    changed = [k for k in ("name", "serial_number", "description", "purchase_date", "warranty_expires")
               if old[k] != data[k]]
    if new_fields != json.dumps(json.loads(old["fields"]), sort_keys=True):
        changed.append("fields")
    if customer_id != old["customer_id"]:
        changed.append("customer_id")
    ts = now_iso()
    with tx(ctx.db):
        ctx.db.execute(
            "UPDATE equipment SET customer_id=?, name=?, serial_number=?, description=?, purchase_date=?, "
            "warranty_expires=?, fields=?, updated_at=? WHERE id=?",
            (customer_id, data["name"], data["serial_number"], data["description"], data["purchase_date"],
             data["warranty_expires"], new_fields, ts, old["id"]))
        if archived is True and old["archived_at"] is None:
            ctx.db.execute("UPDATE equipment SET archived_at=? WHERE id=?", (ts, old["id"]))
            changed.append("archived")
        elif archived is False and old["archived_at"] is not None:
            ctx.db.execute("UPDATE equipment SET archived_at=NULL WHERE id=?", (old["id"],))
            changed.append("restored")
        audit(ctx.db, ctx.user.id, "equipment.updated", "equipment", old["id"], {"changed": changed}, ctx.req.ip)
    return _out(_get_row(ctx.db, old["id"]))


def archive_equipment(ctx):
    old = _get_row(ctx.db, ctx.params["id"])
    with tx(ctx.db):
        ctx.db.execute("UPDATE equipment SET archived_at=COALESCE(archived_at, ?), updated_at=? WHERE id=?",
                       (now_iso(), now_iso(), old["id"]))
        audit(ctx.db, ctx.user.id, "equipment.archived", "equipment", old["id"], ip=ctx.req.ip)
    return {"ok": True}
