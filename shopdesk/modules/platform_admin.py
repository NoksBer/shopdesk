"""Platform administration: sign-in and tenant registry (metadata only, never business data)."""
import json
import secrets

from .. import auth, trash, updater
from ..app import COOKIE_PLATFORM
from ..audit import audit, platform_audit
from ..db import tx
from ..http_util import Response
from ..tenants import open_tenant_db, tenant_dir
from ..util import ApiError, clean_email, clean_text, now_iso, now_ts

TEMP_PASSWORD_HOURS = 24
TEMP_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"      # no look-alike characters

GENERIC = "Invalid email or password"


def register(router):
    router.add("POST", "/api/platform/login", login, "public")
    router.add("POST", "/api/platform/logout", logout, "platform")
    router.add("GET", "/api/platform/me", me, "platform")
    router.add("GET", "/api/platform/tenants", list_tenants, "platform")
    router.add("POST", "/api/platform/tenants/<id>/status", set_status, "platform")
    # Shops console. METADATA ONLY: nothing below ever reads or returns customers, tickets, invoices, notes or files.
    router.add("GET", "/api/platform/shops", list_shops, "platform")
    router.add("GET", "/api/platform/shops/<id>/owners", list_owners, "platform")
    router.add("POST", "/api/platform/shops/<id>/reset-owner-password", reset_owner_password, "platform")
    router.add("POST", "/api/platform/shops/<id>/delete", delete_shop, "platform")
    router.add("GET", "/api/platform/trash", list_trash, "platform")
    router.add("POST", "/api/platform/trash/<id>/restore", restore_shop, "platform")
    router.add("GET", "/api/platform/audit", admin_audit, "platform")


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
    if _in_trash(plat, ctx.params["id"]):
        raise ApiError(409, "That shop is pending deletion. Restore it from 'Recently deleted' first.")
    with tx(plat):
        cur = plat.execute("UPDATE tenants SET status=? WHERE id=? AND status != 'provisioning'",
                           (status, ctx.params["id"]))
        if cur.rowcount == 0:
            raise ApiError(404, "Not found")
        platform_audit(plat, ctx.admin["id"], "tenant.status", ctx.params["id"], {"status": status})
    return {"ok": True}


# ---------------------------------------------------------------- shops console (metadata only)
def _in_trash(plat, tenant_id):
    return plat.execute("SELECT 1 FROM tenant_trash WHERE tenant_id=? AND restored_at IS NULL AND purged_at IS NULL",
                        (tenant_id,)).fetchone() is not None


def _owners(db):
    return [dict(r) for r in db.execute(
        "SELECT u.id, u.name, u.email FROM users u JOIN roles r ON r.id = u.role_id "
        "WHERE r.name='owner' AND u.active=1 ORDER BY u.created_at, u.id")]


def _dir_bytes(directory, names):
    total = 0
    for n in names:
        try:
            total += (directory / n).stat().st_size
        except OSError:
            pass
    return total


def _shop_stats(config, tenant_id):
    """Counts and dates only. Deliberately uses aggregate queries: no business rows are ever selected."""
    out = {"owner_name": "", "owner_email": "", "last_activity": None, "user_count": None, "ticket_count": None,
           "db_bytes": None, "db_missing": False}
    directory = tenant_dir(config, tenant_id)
    path = directory / "tenant.db"
    if not path.exists():
        out["db_missing"] = True
        return out
    out["db_bytes"] = _dir_bytes(directory, ("tenant.db", "tenant.db-wal", "tenant.db-shm"))
    db = open_tenant_db(config, tenant_id)
    try:
        owners = _owners(db)
        if owners:
            out["owner_name"], out["owner_email"] = owners[0]["name"], owners[0]["email"]
        out["user_count"] = db.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]
        out["ticket_count"] = db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        stamps = [db.execute("SELECT MAX(last_login_at) FROM users").fetchone()[0],
                  db.execute("SELECT MAX(updated_at) FROM tickets").fetchone()[0],
                  db.execute("SELECT MAX(ts) FROM ticket_history").fetchone()[0]]
        stamps = [s for s in stamps if s]
        out["last_activity"] = max(stamps) if stamps else None
    finally:
        db.close()
    return out


def list_shops(ctx):
    plat = ctx.platform()
    trashed = {r["tenant_id"]: r for r in plat.execute(
        "SELECT id, tenant_id, owner_name, owner_email, trash_name, deleted_at, purge_after FROM tenant_trash "
        "WHERE restored_at IS NULL AND purged_at IS NULL")}
    shops = []
    for t in plat.execute("SELECT id, name, slug, shop_type, status, created_at FROM tenants "
                          "WHERE status != 'provisioning' ORDER BY created_at, id").fetchall():
        shop = dict(t)
        tr = trashed.get(t["id"])
        if tr is not None:
            shop.update({"status": "pending deletion", "owner_name": tr["owner_name"], "owner_email": tr["owner_email"],
                         "last_activity": None, "user_count": None, "ticket_count": None, "db_missing": False,
                         "deleted_at": tr["deleted_at"], "purge_after": tr["purge_after"], "trash_id": tr["id"],
                         "db_bytes": _dir_bytes(ctx.config.trash_dir / tr["trash_name"], ("tenant.db",))})
        else:
            try:
                shop.update(_shop_stats(ctx.config, t["id"]))
            except Exception:                       # one unreadable shop must not hide all the others
                shop.update({"owner_name": "", "owner_email": "", "last_activity": None, "user_count": None,
                             "ticket_count": None, "db_bytes": None, "db_missing": True})
        shops.append(shop)
    return {"shops": shops}


def _tenant_or_404(plat, tenant_id):
    row = plat.execute("SELECT id, name, status FROM tenants WHERE id=? AND status != 'provisioning'",
                       (tenant_id,)).fetchone()
    if row is None:
        raise ApiError(404, "Not found")
    return row


def list_owners(ctx):
    tenant = _tenant_or_404(ctx.platform(), ctx.params["id"])
    if _in_trash(ctx.platform(), tenant["id"]):
        raise ApiError(409, "That shop is pending deletion")
    db = open_tenant_db(ctx.config, tenant["id"])
    try:
        return {"owners": _owners(db)}
    finally:
        db.close()


def reset_owner_password(ctx):
    """Issue a one-time temporary password for a shop owner. The admin never chooses or sees an existing password; the
    temporary one is shown once in this response, stored only as a scrypt hash, works once and expires after 24 hours."""
    plat = ctx.platform()
    tenant = _tenant_or_404(plat, ctx.params["id"])
    ctx.app.limiter.check("owner-reset-admin:" + ctx.admin["id"], 10, 3600)
    ctx.app.limiter.check("owner-reset-shop:" + tenant["id"], 5, 3600)
    user_id = clean_text(ctx.req.json(), "user_id", max_len=64, required=True)
    if _in_trash(plat, tenant["id"]):
        raise ApiError(409, "That shop is pending deletion")
    db = open_tenant_db(ctx.config, tenant["id"])
    try:
        password = "".join(secrets.choice(TEMP_ALPHABET) for _ in range(16))
        expires = now_ts() + TEMP_PASSWORD_HOURS * 3600
        with tx(db):
            owner = db.execute(
                "SELECT u.id, u.name, u.email FROM users u JOIN roles r ON r.id = u.role_id "
                "WHERE u.id=? AND r.name='owner' AND u.active=1", (user_id,)).fetchone()
            if owner is None:
                raise ApiError(404, "Owner account not found")
            db.execute("UPDATE users SET password_hash=?, must_change_password=1, temp_password_expires_at=?, "
                       "temp_password_used=0 WHERE id=?", (auth.hash_password(password), expires, owner["id"]))
            db.execute("DELETE FROM sessions WHERE subject_id=?", (owner["id"],))
            audit(db, None, "auth.temp_password_issued", "user", owner["id"], {"expires_at": expires, "by": "platform admin"})
    finally:
        db.close()
    with tx(plat):
        platform_audit(plat, ctx.admin["id"], "owner.password_reset", tenant["id"], {"user_id": owner["id"]})
    resp = Response.json({"temporary_password": password, "owner": dict(owner),
                          "expires_hours": TEMP_PASSWORD_HOURS,
                          "message": "Shown once. The owner must change it when they sign in."})
    resp.headers.append(("Cache-Control", "no-store"))
    return resp


def delete_shop(ctx):
    plat, cfg = ctx.platform(), ctx.config
    tenant = _tenant_or_404(plat, ctx.params["id"])
    confirm = clean_text(ctx.req.json(), "confirm_name", max_len=120)
    if _in_trash(plat, tenant["id"]):
        raise ApiError(409, "That shop is already pending deletion")
    if confirm != tenant["name"]:
        raise ApiError(400, "The name you typed does not match the shop's name exactly")
    if updater.is_busy(cfg):
        raise ApiError(409, "A software update is running. Try again when it has finished.")
    try:
        snap = _shop_stats(cfg, tenant["id"])
        owner_name, owner_email = snap["owner_name"], snap["owner_email"]
    except Exception:
        owner_name, owner_email = "", ""
    with tx(plat):                                          # first stop everyone signing in
        plat.execute("UPDATE tenants SET status='suspended' WHERE id=?", (tenant["id"],))
    try:
        trash_name, purge_after = trash.move_to_trash(cfg, tenant["id"])
    except ApiError:
        raise
    except OSError:
        raise ApiError(500, "Could not move the shop's files to the trash. The shop is suspended and nothing was deleted.")
    try:
        with tx(plat):
            plat.execute("INSERT INTO tenant_trash(tenant_id, name, owner_name, owner_email, trash_name, deleted_at, "
                         "deleted_by, purge_after) VALUES(?,?,?,?,?,?,?,?)",
                         (tenant["id"], tenant["name"], owner_name, owner_email, trash_name, now_iso(),
                          ctx.admin["id"], purge_after))
            platform_audit(plat, ctx.admin["id"], "shop.deleted", tenant["id"], {"purge_after": purge_after})
    except Exception:
        trash.move_back(cfg, tenant["id"], trash_name)      # keep files and registry in step
        raise
    return {"ok": True, "purge_after": purge_after}


def list_trash(ctx):
    rows = ctx.platform().execute(
        "SELECT id, tenant_id, name, owner_name, owner_email, deleted_at, purge_after FROM tenant_trash "
        "WHERE restored_at IS NULL AND purged_at IS NULL ORDER BY deleted_at DESC, id DESC").fetchall()
    return {"deleted": [dict(r) for r in rows], "retention_days": trash.RETENTION_DAYS}


def restore_shop(ctx):
    from ..db import migrate
    plat, cfg = ctx.platform(), ctx.config
    try:
        trash_id = int(ctx.params["id"])
    except ValueError:
        raise ApiError(404, "Not found")
    row = plat.execute("SELECT id, tenant_id, name, trash_name FROM tenant_trash WHERE id=? AND restored_at IS NULL "
                       "AND purged_at IS NULL", (trash_id,)).fetchone()
    if row is None:
        raise ApiError(404, "Not found")
    trash.move_back(cfg, row["tenant_id"], row["trash_name"])
    db = open_tenant_db(cfg, row["tenant_id"])
    try:
        migrate(db, cfg.migrations_dir / "tenant")          # the app may have been updated while it sat in the trash
    finally:
        db.close()
    with tx(plat):
        plat.execute("UPDATE tenant_trash SET restored_at=? WHERE id=?", (now_iso(), row["id"]))
        platform_audit(plat, ctx.admin["id"], "shop.restored", row["tenant_id"], {})
    return {"ok": True, "status": "suspended"}       # it stays suspended until the administrator re-activates it


def admin_audit(ctx):
    limit = ctx.req.query_int("limit", 100, 1, 500)
    offset = ctx.req.query_int("offset", 0, 0, 1000000)
    rows = ctx.platform().execute(
        "SELECT a.id, a.ts, a.actor, pa.email AS actor_email, a.action, a.tenant_id, a.detail, "
        "COALESCE(t.name, (SELECT x.name FROM tenant_trash x WHERE x.tenant_id = a.tenant_id ORDER BY x.id DESC LIMIT 1)) "
        "AS shop_name FROM platform_audit a LEFT JOIN platform_admins pa ON pa.id = a.actor "
        "LEFT JOIN tenants t ON t.id = a.tenant_id ORDER BY a.id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    events = []
    for r in rows:
        e = dict(r)
        e["detail"] = json.loads(e["detail"] or "{}")
        events.append(e)
    return {"events": events}