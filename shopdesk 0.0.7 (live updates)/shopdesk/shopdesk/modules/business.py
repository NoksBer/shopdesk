"""Business profile, team listing and audit log viewer (all scoped to the caller's own tenant)."""
from ..audit import audit
from ..db import tx
from ..util import ApiError, CURRENCY_RE, TZ_RE, clean_email, clean_text, like_pattern, now_iso

PROFILE_SQL = "SELECT name, shop_type, phone, email, address, timezone, currency, updated_at FROM business_profile WHERE id=1"


def register(router):
    router.add("GET", "/api/business", get_business, "authed")
    router.add("PUT", "/api/business", update_business, "business.manage")
    router.add("GET", "/api/users", list_users, "users.read")
    router.add("GET", "/api/users/<id>", get_user, "users.read")
    router.add("GET", "/api/audit", list_audit, "audit.read")


def _profile(db):
    return dict(db.execute(PROFILE_SQL).fetchone())


def get_business(ctx):
    return _profile(ctx.db)


def update_business(ctx):
    body = ctx.req.json()
    name = clean_text(body, "name", max_len=120, required=True)
    phone = clean_text(body, "phone", max_len=40)
    address = clean_text(body, "address", max_len=300)
    email = clean_email(body) if body.get("email") else ""
    tz = clean_text(body, "timezone", max_len=64, default="UTC") or "UTC"
    currency = clean_text(body, "currency", max_len=3, default="USD").upper() or "USD"
    if not TZ_RE.match(tz):
        raise ApiError(400, "Invalid timezone")
    if not CURRENCY_RE.match(currency):
        raise ApiError(400, "Currency must be a 3-letter code")
    with tx(ctx.db):
        ctx.db.execute(
            "UPDATE business_profile SET name=?, phone=?, email=?, address=?, timezone=?, currency=?, updated_at=? "
            "WHERE id=1", (name, phone, email, address, tz, currency, now_iso()))
        audit(ctx.db, ctx.user.id, "business.updated", "business", "1", {"name": name}, ctx.req.ip)
    plat = ctx.platform()
    plat.execute("UPDATE tenants SET name=? WHERE id=?", (name, ctx.tenant_id))
    return _profile(ctx.db)


def _user_dict(row):
    return {k: row[k] for k in ("id", "email", "name", "role", "role_label", "active", "created_at", "last_login_at")}


LIST_USERS_SQL = ("SELECT u.id, u.email, u.name, r.name AS role, r.label AS role_label, u.active, "
                  "u.created_at, u.last_login_at FROM users u JOIN roles r ON r.id = u.role_id "
                  "ORDER BY u.created_at, u.name")
GET_USER_SQL = ("SELECT u.id, u.email, u.name, r.name AS role, r.label AS role_label, u.active, "
                "u.created_at, u.last_login_at FROM users u JOIN roles r ON r.id = u.role_id WHERE u.id=?")


def list_users(ctx):
    rows = ctx.db.execute(LIST_USERS_SQL).fetchall()
    return {"users": [_user_dict(r) for r in rows]}


def get_user(ctx):
    row = ctx.db.execute(GET_USER_SQL, (ctx.params["id"],)).fetchone()
    if row is None:
        raise ApiError(404, "Not found")
    return _user_dict(row)


AUDIT_SQL = """
SELECT a.id, a.ts, a.user_id, u.name AS user_name, a.action, a.entity, a.entity_id, a.detail
FROM audit_log a LEFT JOIN users u ON u.id = a.user_id
WHERE (:before = 0 OR a.id < :before)
  AND (:action = '' OR a.action LIKE :pat ESCAPE '\\')
  AND (:uid = '' OR a.user_id = :uid)
ORDER BY a.id DESC
LIMIT :limit
"""


def list_audit(ctx):
    """Newest first. Page with `before=<next_before>`; filter with `action` (contains) and `user_id`."""
    req = ctx.req
    action = (req.query.get("action", [""])[0] or "").strip()[:60]
    limit = req.query_int("limit", 100, 1, 500)
    rows = ctx.db.execute(AUDIT_SQL, {
        "before": req.query_int("before", 0, 0, 2 ** 53), "action": action, "pat": like_pattern(action),
        "uid": (req.query.get("user_id", [""])[0] or "")[:64], "limit": limit}).fetchall()
    events = [dict(r) for r in rows]
    return {"events": events, "next_before": events[-1]["id"] if len(events) == limit else None}
