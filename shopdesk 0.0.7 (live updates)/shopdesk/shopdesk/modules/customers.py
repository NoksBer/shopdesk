"""Customer records. Archive instead of delete so history stays intact."""
from ..audit import audit
from ..db import tx
from ..util import ApiError, clean_email, clean_text, like_pattern, new_id, now_iso

CONTACT_PREFS = ("email", "phone", "text", "none")
TYPES = ("individual", "business")

LIST_SQL = """
SELECT c.id, c.type, c.name, c.contact_name, c.email, c.phone, c.address, c.contact_pref, c.notes,
       c.created_at, c.updated_at, c.archived_at,
       (SELECT COUNT(*) FROM equipment e WHERE e.customer_id = c.id AND e.archived_at IS NULL) AS equipment_count
FROM customers c
WHERE (c.archived_at IS NULL OR :arch = 1)
  AND (:q = '' OR c.name LIKE :pat ESCAPE '\\' OR c.contact_name LIKE :pat ESCAPE '\\'
       OR c.email LIKE :pat ESCAPE '\\' OR c.phone LIKE :pat ESCAPE '\\')
ORDER BY c.name COLLATE NOCASE, c.id
LIMIT :limit OFFSET :offset
"""

COUNT_SQL = """
SELECT COUNT(*) FROM customers c
WHERE (c.archived_at IS NULL OR :arch = 1)
  AND (:q = '' OR c.name LIKE :pat ESCAPE '\\' OR c.contact_name LIKE :pat ESCAPE '\\'
       OR c.email LIKE :pat ESCAPE '\\' OR c.phone LIKE :pat ESCAPE '\\')
"""

GET_SQL = """
SELECT c.id, c.type, c.name, c.contact_name, c.email, c.phone, c.address, c.contact_pref, c.notes,
       c.created_at, c.updated_at, c.archived_at,
       (SELECT COUNT(*) FROM equipment e WHERE e.customer_id = c.id AND e.archived_at IS NULL) AS equipment_count
FROM customers c WHERE c.id = ?
"""

EDITABLE = ("type", "name", "contact_name", "email", "phone", "address", "contact_pref", "notes")


def register(router):
    router.add("GET", "/api/customers", list_customers, "customers.read")
    router.add("POST", "/api/customers", create_customer, "customers.write")
    router.add("GET", "/api/customers/<id>", get_customer, "customers.read")
    router.add("PUT", "/api/customers/<id>", update_customer, "customers.write")
    router.add("DELETE", "/api/customers/<id>", archive_customer, "customers.write")


def _parse(body):
    ctype = clean_text(body, "type", max_len=20, default="individual") or "individual"
    if ctype not in TYPES:
        raise ApiError(400, "type must be individual or business")
    pref = clean_text(body, "contact_pref", max_len=20, default="email") or "email"
    if pref not in CONTACT_PREFS:
        raise ApiError(400, "contact_pref must be one of: " + ", ".join(CONTACT_PREFS))
    return {
        "type": ctype,
        "name": clean_text(body, "name", max_len=120, required=True),
        "contact_name": clean_text(body, "contact_name", max_len=120),
        "email": clean_email(body) if body.get("email") else "",
        "phone": clean_text(body, "phone", max_len=40),
        "address": clean_text(body, "address", max_len=300),
        "contact_pref": pref,
        "notes": clean_text(body, "notes", max_len=2000),
    }


def get_customer_row(db, customer_id):
    row = db.execute(GET_SQL, (customer_id,)).fetchone()
    if row is None:
        raise ApiError(404, "Not found")
    return row


def list_customers(ctx):
    req = ctx.req
    q = (req.query.get("q", [""])[0] or "").strip()[:100]
    params = {"arch": 1 if req.query.get("include_archived", ["0"])[0] == "1" else 0,
              "q": q, "pat": like_pattern(q),
              "limit": req.query_int("limit", 50, 1, 200), "offset": req.query_int("offset", 0, 0, 1000000)}
    rows = ctx.db.execute(LIST_SQL, params).fetchall()
    total = ctx.db.execute(COUNT_SQL, {k: params[k] for k in ("arch", "q", "pat")}).fetchone()[0]
    return {"customers": [dict(r) for r in rows], "total": total}


def create_customer(ctx):
    data = _parse(ctx.req.json())
    cid, ts = new_id(), now_iso()
    with tx(ctx.db):
        ctx.db.execute(
            "INSERT INTO customers(id, type, name, contact_name, email, phone, address, contact_pref, notes, "
            "created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, data["type"], data["name"], data["contact_name"], data["email"], data["phone"], data["address"],
             data["contact_pref"], data["notes"], ctx.user.id, ts, ts))
        audit(ctx.db, ctx.user.id, "customer.created", "customer", cid, ip=ctx.req.ip)
    return 201, dict(get_customer_row(ctx.db, cid))


def get_customer(ctx):
    return dict(get_customer_row(ctx.db, ctx.params["id"]))


def update_customer(ctx):
    body = ctx.req.json()
    old = get_customer_row(ctx.db, ctx.params["id"])
    data = _parse(body)
    archived = body.get("archived")
    if archived is not None and not isinstance(archived, bool):
        raise ApiError(400, "archived must be true or false")
    changed = [k for k in EDITABLE if old[k] != data[k]]
    ts = now_iso()
    with tx(ctx.db):
        ctx.db.execute(
            "UPDATE customers SET type=?, name=?, contact_name=?, email=?, phone=?, address=?, contact_pref=?, "
            "notes=?, updated_at=? WHERE id=?",
            (data["type"], data["name"], data["contact_name"], data["email"], data["phone"], data["address"],
             data["contact_pref"], data["notes"], ts, old["id"]))
        if archived is True and old["archived_at"] is None:
            ctx.db.execute("UPDATE customers SET archived_at=? WHERE id=?", (ts, old["id"]))
            changed.append("archived")
        elif archived is False and old["archived_at"] is not None:
            ctx.db.execute("UPDATE customers SET archived_at=NULL WHERE id=?", (old["id"],))
            changed.append("restored")
        audit(ctx.db, ctx.user.id, "customer.updated", "customer", old["id"], {"changed": changed}, ctx.req.ip)
    return dict(get_customer_row(ctx.db, old["id"]))


def archive_customer(ctx):
    old = get_customer_row(ctx.db, ctx.params["id"])
    with tx(ctx.db):
        ctx.db.execute("UPDATE customers SET archived_at=COALESCE(archived_at, ?), updated_at=? WHERE id=?",
                       (now_iso(), now_iso(), old["id"]))
        audit(ctx.db, ctx.user.id, "customer.archived", "customer", old["id"], ip=ctx.req.ip)
    return {"ok": True}
