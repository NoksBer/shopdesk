"""Platform administration: sign-in and tenant registry (metadata only, never business data)."""
from .. import auth
from ..app import COOKIE_PLATFORM
from ..audit import platform_audit
from ..db import tx
from ..http_util import Response
from ..util import ApiError, clean_email, clean_text

GENERIC = "Invalid email or password"


def register(router):
    router.add("POST", "/api/platform/login", login, "public")
    router.add("POST", "/api/platform/logout", logout, "platform")
    router.add("GET", "/api/platform/me", me, "platform")
    router.add("GET", "/api/platform/tenants", list_tenants, "platform")
    router.add("POST", "/api/platform/tenants/<id>/status", set_status, "platform")


def login(ctx):
    app, req = ctx.app, ctx.req
    app.limiter.check("plogin-ip:" + req.ip, 20, 900)
    body = req.json()
    try:
        email = clean_email(body)
    except ApiError:
        raise ApiError(401, GENERIC)
    password = body.get("password")
    if not isinstance(password, str) or not password or len(password) > auth.MAX_PASSWORD:
        raise ApiError(401, GENERIC)
    app.limiter.check("plogin-email:" + email, 8, 900)
    plat = ctx.platform()
    row = plat.execute("SELECT id, password_hash, disabled FROM platform_admins WHERE email=?", (email,)).fetchone()
    stored = row["password_hash"] if row and not row["disabled"] else None
    valid = auth.verify_password(password, stored or auth.dummy_hash())
    if not (valid and stored):
        raise ApiError(401, GENERIC)
    with tx(plat):
        token, csrf = auth.create_session(plat, "platform", app.secret, row["id"], ctx.config)
        platform_audit(plat, row["id"], "platform.login")
    resp = Response.json({"ok": True, "csrf": csrf})
    resp.cookies.append(ctx.cookie(COOKIE_PLATFORM, "p." + token, ctx.config.session_max_seconds))
    return resp


def logout(ctx):
    auth.destroy_session(ctx.platform(), "platform", ctx.session["hash"])
    resp = Response.json({"ok": True})
    resp.cookies.append(ctx.cookie(COOKIE_PLATFORM, "deleted", 0))
    return resp


def me(ctx):
    return {"admin": ctx.admin, "csrf": ctx.session["csrf"]}


def list_tenants(ctx):
    rows = ctx.platform().execute(
        "SELECT id, name, slug, shop_type, status, created_at FROM tenants ORDER BY created_at").fetchall()
    return {"tenants": [dict(r) for r in rows]}


def set_status(ctx):
    status = clean_text(ctx.req.json(), "status", max_len=20, required=True)
    if status not in ("active", "suspended"):
        raise ApiError(400, "status must be 'active' or 'suspended'")
    plat = ctx.platform()
    with tx(plat):
        cur = plat.execute("UPDATE tenants SET status=? WHERE id=? AND status != 'provisioning'",
                           (status, ctx.params["id"]))
        if cur.rowcount == 0:
            raise ApiError(404, "Not found")
        platform_audit(plat, ctx.admin["id"], "tenant.status", ctx.params["id"], {"status": status})
    return {"ok": True}
