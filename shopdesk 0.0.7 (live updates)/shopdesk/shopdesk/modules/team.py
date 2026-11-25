"""Team management: roles, expiring staff invitations, role changes and deactivation."""
import secrets
import sqlite3

from .. import auth
from ..audit import audit
from ..db import tx
from ..tenants import TENANT_ID_RE, open_tenant_db
from ..util import ApiError, clean_email, clean_text, new_id, now_iso, now_ts
from .accounts import _session_response
from .business import GET_USER_SQL, _user_dict

INVITE_TTL_SECONDS = 7 * 86400
ASSIGNABLE_ROLES = ("manager", "technician", "front_desk")
INVALID_INVITE = "This invitation is invalid or has expired."


def register(router):
    router.add("GET", "/api/roles", list_roles, "users.read")
    router.add("GET", "/api/invites", list_invites, "users.manage")
    router.add("POST", "/api/invites", create_invite, "users.manage")
    router.add("DELETE", "/api/invites/<id>", revoke_invite, "users.manage")
    router.add("PUT", "/api/users/<id>", update_user, "users.manage")
    router.add("POST", "/api/invites/preview", preview_invite, "public")
    router.add("POST", "/api/invites/accept", accept_invite, "public")


def list_roles(ctx):
    rows = ctx.db.execute("SELECT name, label FROM roles WHERE name IN (?,?,?) ORDER BY label",
                          ASSIGNABLE_ROLES).fetchall()
    return {"roles": [dict(r) for r in rows]}


def list_invites(ctx):
    rows = ctx.db.execute(
        "SELECT i.id, i.email, r.name AS role, r.label AS role_label, i.created_at, i.expires_at "
        "FROM invites i JOIN roles r ON r.id = i.role_id "
        "WHERE i.used_at IS NULL AND i.revoked_at IS NULL AND i.expires_at > ? ORDER BY i.created_at DESC",
        (now_ts(),)).fetchall()
    return {"invites": [dict(r) for r in rows]}


def create_invite(ctx):
    body = ctx.req.json()
    email = clean_email(body)
    role = clean_text(body, "role", max_len=40, required=True)
    if role not in ASSIGNABLE_ROLES:
        raise ApiError(400, "Role must be one of: " + ", ".join(ASSIGNABLE_ROLES))
    if ctx.platform().execute("SELECT 1 FROM staff_identities WHERE email=?", (email,)).fetchone():
        raise ApiError(409, "That email already has an account.")
    role_row = ctx.db.execute("SELECT id FROM roles WHERE name=?", (role,)).fetchone()
    secret = secrets.token_urlsafe(32)
    invite_id = new_id()
    expires = now_ts() + INVITE_TTL_SECONDS
    with tx(ctx.db):
        ctx.db.execute("UPDATE invites SET revoked_at=? WHERE email=? AND used_at IS NULL AND revoked_at IS NULL",
                       (now_iso(), email))
        ctx.db.execute(
            "INSERT INTO invites(id, email, role_id, token_hash, invited_by, created_at, expires_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (invite_id, email, role_row["id"], auth.token_hash(ctx.app.secret, secret), ctx.user.id,
             now_iso(), expires))
        audit(ctx.db, ctx.user.id, "invite.created", "invite", invite_id, {"role": role}, ctx.req.ip)
    # The token is shown once; only its hash is stored. Email delivery arrives in Phase 2.
    return {"invite": {"id": invite_id, "email": email, "role": role, "expires_at": expires},
            "token": "%s.%s" % (ctx.tenant_id, secret)}


def revoke_invite(ctx):
    with tx(ctx.db):
        cur = ctx.db.execute(
            "UPDATE invites SET revoked_at=? WHERE id=? AND used_at IS NULL AND revoked_at IS NULL",
            (now_iso(), ctx.params["id"]))
        if cur.rowcount == 0:
            raise ApiError(404, "Not found")
        audit(ctx.db, ctx.user.id, "invite.revoked", "invite", ctx.params["id"], ip=ctx.req.ip)
    return {"ok": True}


def update_user(ctx):
    body = ctx.req.json()
    target = ctx.db.execute(
        "SELECT u.id, r.name AS role_name FROM users u JOIN roles r ON r.id = u.role_id WHERE u.id=?",
        (ctx.params["id"],)).fetchone()
    if target is None:
        raise ApiError(404, "Not found")
    if target["role_name"] == "owner":
        raise ApiError(400, "The owner account cannot be changed here.")
    if target["id"] == ctx.user.id:
        raise ApiError(400, "You cannot change your own account here.")
    role = body.get("role")
    active = body.get("active")
    if role is None and active is None:
        raise ApiError(400, "Nothing to change")
    detail = {}
    with tx(ctx.db):
        if role is not None:
            if role not in ASSIGNABLE_ROLES:
                raise ApiError(400, "Role must be one of: " + ", ".join(ASSIGNABLE_ROLES))
            role_row = ctx.db.execute("SELECT id FROM roles WHERE name=?", (role,)).fetchone()
            ctx.db.execute("UPDATE users SET role_id=? WHERE id=?", (role_row["id"], target["id"]))
            detail["role"] = role
        if active is not None:
            if not isinstance(active, bool):
                raise ApiError(400, "active must be true or false")
            ctx.db.execute("UPDATE users SET active=? WHERE id=?", (1 if active else 0, target["id"]))
            if not active:
                ctx.db.execute("DELETE FROM sessions WHERE subject_id=?", (target["id"],))
            detail["active"] = active
        audit(ctx.db, ctx.user.id, "user.updated", "user", target["id"], detail, ctx.req.ip)
    return _user_dict(ctx.db.execute(GET_USER_SQL, (target["id"],)).fetchone())


# ---------------------------------------------------------------- public: accept an invitation
def _load_invite(ctx, token):
    """Returns (tenant_db, invite_row). Caller must close the db. Any problem -> one generic error."""
    if not isinstance(token, str) or token.count(".") != 1:
        raise ApiError(400, INVALID_INVITE)
    tenant_id, secret = token.split(".")
    if not TENANT_ID_RE.match(tenant_id) or not secret or len(secret) > 100:
        raise ApiError(400, INVALID_INVITE)
    trow = ctx.platform().execute("SELECT status FROM tenants WHERE id=?", (tenant_id,)).fetchone()
    if trow is None or trow["status"] != "active":
        raise ApiError(400, INVALID_INVITE)
    db = open_tenant_db(ctx.config, tenant_id)
    row = db.execute(
        "SELECT i.id, i.email, i.role_id, r.label AS role_label, "
        "(SELECT name FROM business_profile WHERE id=1) AS business_name "
        "FROM invites i JOIN roles r ON r.id = i.role_id "
        "WHERE i.token_hash=? AND i.used_at IS NULL AND i.revoked_at IS NULL AND i.expires_at > ?",
        (auth.token_hash(ctx.app.secret, secret), now_ts())).fetchone()
    if row is None:
        db.close()
        raise ApiError(400, INVALID_INVITE)
    return tenant_id, db, row


def preview_invite(ctx):
    ctx.app.limiter.check("invite:" + ctx.req.ip, 30, 900)
    tenant_id, db, row = _load_invite(ctx, ctx.req.json().get("token"))
    db.close()
    return {"email": row["email"], "role_label": row["role_label"], "business_name": row["business_name"]}


def accept_invite(ctx):
    ctx.app.limiter.check("invite:" + ctx.req.ip, 30, 900)
    body = ctx.req.json()
    name = clean_text(body, "name", max_len=120, required=True)
    password = body.get("password")
    err = auth.password_error(password)
    if err:
        raise ApiError(400, err)
    tenant_id, db, inv = _load_invite(ctx, body.get("token"))
    plat = ctx.platform()
    user_id = new_id()
    try:
        try:
            plat.execute("INSERT INTO staff_identities(email, tenant_id, user_id, created_at) VALUES(?,?,?,?)",
                         (inv["email"], tenant_id, user_id, now_iso()))
        except sqlite3.IntegrityError:
            raise ApiError(409, "That email already has an account.")
        try:
            with tx(db):
                cur = db.execute("UPDATE invites SET used_at=? WHERE id=? AND used_at IS NULL AND revoked_at IS NULL",
                                 (now_iso(), inv["id"]))
                if cur.rowcount != 1:
                    raise ApiError(400, INVALID_INVITE)
                db.execute(
                    "INSERT INTO users(id, email, name, role_id, password_hash, created_at) VALUES(?,?,?,?,?,?)",
                    (user_id, inv["email"], name, inv["role_id"], auth.hash_password(password), now_iso()))
                audit(db, user_id, "user.joined", "user", user_id, {"invite": inv["id"]}, ctx.req.ip)
        except BaseException:
            plat.execute("DELETE FROM staff_identities WHERE email=? AND user_id=?", (inv["email"], user_id))
            raise
        return _session_response(ctx, tenant_id, user_id, db)
    finally:
        db.close()
