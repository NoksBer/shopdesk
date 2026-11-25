"""Service catalogue: named jobs with a price, used to fill invoice lines. Deactivate instead of delete."""
import sqlite3

from ..audit import audit
from ..db import tx
from ..util import (MAX_CENTS, ApiError, clean_flag, clean_int, clean_text, like_pattern, new_id, now_iso)

LIST_SQL = """
SELECT id, name, description, price_cents, taxable, active, created_at, updated_at
FROM services
WHERE (active = 1 OR :inactive = 1)
  AND (:q = '' OR name LIKE :pat ESCAPE '\\' OR description LIKE :pat ESCAPE '\\')
ORDER BY active DESC, name COLLATE NOCASE
LIMIT 500
"""

GET_SQL = "SELECT id, name, description, price_cents, taxable, active, created_at, updated_at FROM services WHERE id = ?"


def register(router):
    router.add("GET", "/api/services", list_services, "services.read")
    router.add("POST", "/api/services", create_service, "services.write")
    router.add("PUT", "/api/services/<id>", update_service, "services.write")
    router.add("DELETE", "/api/services/<id>", deactivate_service, "services.write")


def _parse(body):
    return {
        "name": clean_text(body, "name", max_len=120, required=True),
        "description": clean_text(body, "description", max_len=300),
        "price_cents": clean_int(body, "price_cents", 0, MAX_CENTS),
        "taxable": clean_flag(body, "taxable", True),
    }


def _row(db, sid):
    row = db.execute(GET_SQL, (sid,)).fetchone()
    if row is None:
        raise ApiError(404, "Not found")
    return row


def list_services(ctx):
    q = (ctx.req.query.get("q", [""])[0] or "").strip()[:100]
    inactive = 1 if ctx.req.query.get("include_inactive", ["0"])[0] == "1" else 0
    rows = ctx.db.execute(LIST_SQL, {"q": q, "pat": like_pattern(q), "inactive": inactive}).fetchall()
    return {"services": [dict(r) for r in rows]}


def create_service(ctx):
    data = _parse(ctx.req.json())
    sid, ts = new_id(), now_iso()
    try:
        with tx(ctx.db):
            ctx.db.execute(
                "INSERT INTO services(id, name, description, price_cents, taxable, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (sid, data["name"], data["description"], data["price_cents"], data["taxable"], ts, ts))
            audit(ctx.db, ctx.user.id, "service.created", "service", sid, ip=ctx.req.ip)
    except sqlite3.IntegrityError:
        raise ApiError(409, "A service with that name already exists")
    return 201, dict(_row(ctx.db, sid))


def update_service(ctx):
    body = ctx.req.json()
    old = _row(ctx.db, ctx.params["id"])
    data = _parse(body)
    active = clean_flag(body, "active", bool(old["active"]))
    try:
        with tx(ctx.db):
            ctx.db.execute(
                "UPDATE services SET name=?, description=?, price_cents=?, taxable=?, active=?, updated_at=? WHERE id=?",
                (data["name"], data["description"], data["price_cents"], data["taxable"], active, now_iso(), old["id"]))
            audit(ctx.db, ctx.user.id, "service.updated", "service", old["id"],
                  {"price_changed": old["price_cents"] != data["price_cents"]}, ctx.req.ip)
    except sqlite3.IntegrityError:
        raise ApiError(409, "A service with that name already exists")
    return dict(_row(ctx.db, old["id"]))


def deactivate_service(ctx):
    old = _row(ctx.db, ctx.params["id"])
    with tx(ctx.db):
        ctx.db.execute("UPDATE services SET active=0, updated_at=? WHERE id=?", (now_iso(), old["id"]))
        audit(ctx.db, ctx.user.id, "service.deactivated", "service", old["id"], ip=ctx.req.ip)
    return {"ok": True}
