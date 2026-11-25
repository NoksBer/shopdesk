"""Tenant registry and provisioning. This is the ONLY place a tenant database path is derived."""
import json
import re
import shutil

from .audit import audit, platform_audit
from .auth import hash_password
from .db import connect, migrate, tx
from .permissions import ROLES
from .profiles import DEFAULT_STATUSES, PROFILES
from .util import ApiError, new_id, now_iso, slugify

TENANT_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def tenant_dir(config, tenant_id):
    if not isinstance(tenant_id, str) or not TENANT_ID_RE.match(tenant_id):
        raise ApiError(404, "Not found")
    return config.tenants_dir / tenant_id


def open_tenant_db(config, tenant_id):
    path = tenant_dir(config, tenant_id) / "tenant.db"
    if not path.exists():
        raise ApiError(404, "Not found")
    return connect(path)


def tenant_count(plat):
    return plat.execute("SELECT COUNT(*) FROM tenants WHERE status != 'provisioning'").fetchone()[0]


def can_register(plat, config):
    return config.open_signup or tenant_count(plat) == 0


def _unique_slug(plat, name):
    base = slugify(name)
    slug, n = base, 1
    while plat.execute("SELECT 1 FROM tenants WHERE slug=?", (slug,)).fetchone():
        n += 1
        slug = "%s-%d" % (base, n)
    return slug


def _seed(conn, tenant_id, business, shop_type, owner_name, owner_email, pw_hash, ip):
    profile = PROFILES[shop_type]
    ts = now_iso()
    owner_id = new_id()
    with tx(conn):
        conn.execute("INSERT INTO business_profile(id, name, shop_type, updated_at) VALUES(1,?,?,?)",
                     (business, shop_type, ts))
        conn.execute("INSERT INTO locations(id, name, is_default, created_at) VALUES(?,?,1,?)",
                     (new_id(), "Main", ts))
        owner_role = None
        for name, label, perms in ROLES:
            rid = new_id()
            conn.execute("INSERT INTO roles(id, name, label, is_system) VALUES(?,?,?,1)", (rid, name, label))
            for perm in perms:
                conn.execute("INSERT INTO role_permissions(role_id, permission) VALUES(?,?)", (rid, perm))
            if name == "owner":
                owner_role = rid
        conn.execute(
            "INSERT INTO users(id, email, name, role_id, password_hash, created_at) VALUES(?,?,?,?,?,?)",
            (owner_id, owner_email, owner_name, owner_role, pw_hash, ts))
        statuses = profile.get("statuses", DEFAULT_STATUSES)
        for i, (name, customer_label, closed) in enumerate(statuses):
            conn.execute(
                "INSERT INTO ticket_statuses(id, name, sort_order, is_closed, customer_label) VALUES(?,?,?,?,?)",
                (new_id(), name, i, closed, customer_label))
        for key in ("terminology", "equipment_fields", "intake_checklist", "service_templates"):
            conn.execute("INSERT INTO settings(key, value) VALUES(?,?)", (key, json.dumps(profile[key])))
        audit(conn, owner_id, "tenant.created", "tenant", tenant_id, {"shop_type": shop_type}, ip)
    return owner_id


def provision_tenant(app, business, shop_type, owner_name, owner_email, password, ip=None):
    """Create a tenant: registry row, database file, migrations, roles, owner, profile defaults.
    Returns (tenant_id, owner_user_id). Cleans up fully if anything fails."""
    cfg = app.config
    if shop_type not in PROFILES:
        raise ApiError(400, "Unknown shop type")
    tenant_id = new_id()
    pw_hash = hash_password(password)
    plat = connect(cfg.platform_db_path)
    try:
        if plat.execute("SELECT 1 FROM staff_identities WHERE email=?", (owner_email,)).fetchone():
            raise ApiError(409, "That email cannot be used to create a new business.")
        with tx(plat):
            plat.execute("INSERT INTO tenants(id, name, slug, shop_type, status, created_at) VALUES(?,?,?,?,?,?)",
                         (tenant_id, business, _unique_slug(plat, business), shop_type, "provisioning", now_iso()))
        directory = tenant_dir(cfg, tenant_id)
        try:
            (directory / "files").mkdir(parents=True, exist_ok=False)
            tconn = connect(directory / "tenant.db")
            try:
                migrate(tconn, cfg.migrations_dir / "tenant")
                owner_id = _seed(tconn, tenant_id, business, shop_type, owner_name, owner_email, pw_hash, ip)
            finally:
                tconn.close()
            with tx(plat):
                plat.execute("INSERT INTO staff_identities(email, tenant_id, user_id, created_at) VALUES(?,?,?,?)",
                             (owner_email, tenant_id, owner_id, now_iso()))
                plat.execute("UPDATE tenants SET status='active' WHERE id=?", (tenant_id,))
                platform_audit(plat, None, "tenant.provisioned", tenant_id, {"shop_type": shop_type})
        except BaseException as exc:
            shutil.rmtree(directory, ignore_errors=True)
            plat.execute("DELETE FROM tenants WHERE id=? AND status='provisioning'", (tenant_id,))
            if isinstance(exc, Exception) and "UNIQUE" in str(exc) and "staff_identities" in str(exc):
                raise ApiError(409, "That email cannot be used to create a new business.")
            raise
        return tenant_id, owner_id
    finally:
        plat.close()


def cleanup_stale_provisioning(config):
    """Startup housekeeping: remove half-created tenants left by a crash."""
    plat = connect(config.platform_db_path)
    try:
        for row in plat.execute("SELECT id FROM tenants WHERE status='provisioning'").fetchall():
            shutil.rmtree(tenant_dir(config, row["id"]), ignore_errors=True)
            plat.execute("DELETE FROM tenants WHERE id=?", (row["id"],))
    finally:
        plat.close()
