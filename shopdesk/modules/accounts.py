"""Staff authentication: bootstrap, business registration, login, logout, current user."""
import json

from .. import APP_NAME, __version__, auth, tenants
from ..app import COOKIE_TENANT
from ..audit import audit
from ..db import tx
from ..http_util import Response
from ..profiles import shop_type_list
from ..util import ApiError, clean_email, clean_text, now_iso, now_ts

GENERIC_LOGIN_ERROR = "Invalid email or password"


def register(router):
    router.add("GET", "/api/bootstrap", bootstrap, "public")
    router.add("POST", "/api/register", register_business, "public")
    router.add("POST", "/api/login", login, "public")
    router.add("POST", "/api/logout", logout, "authed")
    router.add("GET", "/api/me", me, "authed")
    router.add("POST", "/api/account/password", change_password, "authed")
    router.add("GET", "/healthz", healthz, "public")


def healthz(ctx):
    ctx.platform().execute("SELECT 1").fetchone()
    return {"ok": True, "version": __version__}     # the updater confirms the new version through this


def bootstrap(ctx):
    return {"app": APP_NAME, "shop_types": shop_type_list(),
            "can_register": tenants.can_register(ctx.platform(), ctx.config)}


def _session_response(ctx, tenant_id, user_id, db):
    token, csrf = auth.create_session(db, "tenant", ctx.app.secret, user_id, ctx.config)
    resp = Response.json({"ok": True, "csrf": csrf})
    resp.cookies.append(ctx.cookie(COOKIE_TENANT, "t.%s.%s" % (tenant_id, token), ctx.config.session_max_seconds))
    return resp


def register_business(ctx):
    app, req = ctx.app, ctx.req
    app.limiter.check("register:" + req.ip, ctx.config.register_limit_per_hour, 3600)
    body = req.json()
    if not tenants.can_register(ctx.platform(), ctx.config):
        raise ApiError(403, "Registration is closed. Ask the platform administrator to enable it.")
    business = clean_text(body, "business_name", max_len=120, required=True)
    shop_type = clean_text(body, "shop_type", max_len=40, required=True)
    name = clean_text(body, "name", max_len=120, required=True)
    email = clean_email(body)
    password = body.get("password")
    err = auth.password_error(password)
    if err:
        raise ApiError(400, err)
    tenant_id, owner_id = tenants.provision_tenant(app, business, shop_type, name, email, password, req.ip)
    db = tenants.open_tenant_db(ctx.config, tenant_id)
    try:
        resp = _session_response(ctx, tenant_id, owner_id, db)
    finally:
        db.close()
    return resp


def login(ctx):
    app, req = ctx.app, ctx.req
    app.limiter.check("login-ip:" + req.ip, 30, 900)
    body = req.json()
    try:
        email = clean_email(body)
    except ApiError:
        raise ApiError(401, GENERIC_LOGIN_ERROR)
    password = body.get("password")
    if not isinstance(password, str) or not password or len(password) > auth.MAX_PASSWORD:
        raise ApiError(401, GENERIC_LOGIN_ERROR)
    app.limiter.check("login-email:" + email, 8, 900)

    plat = ctx.platform()
    ident = plat.execute("SELECT tenant_id, user_id FROM staff_identities WHERE email=?", (email,)).fetchone()
    tenant = None
    if ident:
        tenant = plat.execute("SELECT status FROM tenants WHERE id=?", (ident["tenant_id"],)).fetchone()
    if not ident or tenant is None or tenant["status"] != "active":
        auth.verify_password(password, auth.dummy_hash())     # equalize timing
        raise ApiError(401, GENERIC_LOGIN_ERROR)

    db = tenants.open_tenant_db(ctx.config, ident["tenant_id"])
    try:
        user = db.execute("SELECT id, password_hash, active, must_change_password, temp_password_expires_at, "
                          "temp_password_used FROM users WHERE id=?", (ident["user_id"],)).fetchone()
        stored = user["password_hash"] if user and user["active"] else None
        valid = auth.verify_password(password, stored or auth.dummy_hash())
        if valid and stored and user["must_change_password"]:
            # An administrator-issued temporary password works once, and only within its 24 hours.
            if user["temp_password_used"] or now_ts() >= (user["temp_password_expires_at"] or 0):
                valid = False
        if not (valid and stored):
            if user:
                audit(db, user["id"], "auth.login_failed", "user", user["id"], ip=req.ip)
            raise ApiError(401, GENERIC_LOGIN_ERROR)
        with tx(db):
            if user["must_change_password"]:
                db.execute("UPDATE users SET temp_password_used=1 WHERE id=?", (user["id"],))
            db.execute("UPDATE users SET last_login_at=? WHERE id=?", (now_iso(), user["id"]))
            audit(db, user["id"], "auth.login", "user", user["id"], ip=req.ip)
            return _session_response(ctx, ident["tenant_id"], user["id"], db)
    finally:
        db.close()


def logout(ctx):
    auth.destroy_session(ctx.db, "tenant", ctx.session["hash"])
    audit(ctx.db, ctx.user.id, "auth.logout", "user", ctx.user.id, ip=ctx.req.ip)
    resp = Response.json({"ok": True})
    resp.cookies.append(ctx.cookie(COOKIE_TENANT, "deleted", 0))
    return resp


def change_password(ctx):
    """The signed-in user sets a new password (this is also how a temporary password is replaced).
    Requires the current password; other sessions of the user are signed out."""
    body = ctx.req.json()
    ctx.app.limiter.check("pwchange:" + ctx.user.id, 10, 3600)
    current, new = body.get("current_password"), body.get("new_password")
    if not isinstance(current, str) or not current or len(current) > auth.MAX_PASSWORD:
        raise ApiError(400, "Enter your current password")
    err = auth.password_error(new)
    if err:
        raise ApiError(400, err)
    if new == current:
        raise ApiError(400, "Choose a password different from the current one")
    stored = ctx.db.execute("SELECT password_hash FROM users WHERE id=?", (ctx.user.id,)).fetchone()["password_hash"]
    if not auth.verify_password(current, stored):
        raise ApiError(400, "Your current password is not correct")
    with tx(ctx.db):
        ctx.db.execute("UPDATE users SET password_hash=?, must_change_password=0, temp_password_expires_at=NULL, "
                       "temp_password_used=0 WHERE id=?", (auth.hash_password(new), ctx.user.id))
        ctx.db.execute("DELETE FROM sessions WHERE subject_id=? AND token_hash<>?", (ctx.user.id, ctx.session["hash"]))
        audit(ctx.db, ctx.user.id, "auth.password_changed", "user", ctx.user.id, ip=ctx.req.ip)
    return {"ok": True}


def me(ctx):
    db = ctx.db
    biz = db.execute("SELECT name, shop_type FROM business_profile WHERE id=1").fetchone()
    terminology = db.execute("SELECT value FROM settings WHERE key='terminology'").fetchone()
    presets = db.execute("SELECT value FROM settings WHERE key='touch_base_presets'").fetchone()
    return {
        "touch_base_presets": json.loads(presets["value"]) if presets else [],
        "user": {"id": ctx.user.id, "email": ctx.user.email, "name": ctx.user.name,
                 "role": ctx.user.role, "role_label": ctx.user.role_label,
                 "must_change_password": ctx.user.must_change_password},
        "permissions": sorted(ctx.user.permissions) if ctx.user.role != "owner" else ["*"],
        "business": {"name": biz["name"], "shop_type": biz["shop_type"]},
        "terminology": json.loads(terminology["value"]) if terminology else {},
        "csrf": ctx.session["csrf"],
    }
