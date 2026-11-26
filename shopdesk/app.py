"""Application core: request dispatch, authentication, CSRF, authorization, tenant scoping."""
import hashlib
import hmac
import json
import re
import sys
import traceback
from urllib.parse import quote, urlsplit

from . import APP_NAME, __version__, auth
from .config import Config
from .db import connect, migrate
from .http_util import Request, Response, Router
from .tenants import TENANT_ID_RE, cleanup_stale_provisioning, open_tenant_db
from .util import ApiError

# Local script/style/icon references in HTML get "?v=<VERSION>" appended (cache busting; no build step).
ASSET_RE = re.compile(rb'((?:src|href)=")(/[^"?#]+\.(?:js|css|svg))(")')
ASSET_VERSION = quote(__version__, safe="").encode("ascii")

COOKIE_TENANT = "sd_session"
COOKIE_PLATFORM = "sd_platform"

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
    ".ico": "image/x-icon", ".json": "application/json; charset=utf-8",
}


class User:
    def __init__(self, row, permissions):
        self.id = row["id"]
        self.email = row["email"]
        self.name = row["name"]
        self.role = row["role_name"]
        self.role_label = row["role_label"]
        self.permissions = permissions

    def can(self, permission):
        return self.role == "owner" or permission in self.permissions


class Context:
    """Per-request state. `db` is bound to ONE tenant's database file; handlers have no way to open another."""

    def __init__(self, app, req, params):
        self.app = app
        self.req = req
        self.params = params
        self.tenant_id = None
        self.db = None
        self.user = None
        self.admin = None
        self.session = None
        self._plat = None

    @property
    def config(self):
        return self.app.config

    def platform(self):
        if self._plat is None:
            self._plat = connect(self.config.platform_db_path)
        return self._plat

    def cookie(self, name, value, max_age):
        parts = ["%s=%s" % (name, value), "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=%d" % max_age]
        if self.req.https or self.config.force_secure_cookies:
            parts.append("Secure")
        return "; ".join(parts)

    def close(self):
        for conn in (self.db, self._plat):
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


class App:
    def __init__(self, config):
        self.config = config
        config.ensure_dirs()
        self.secret = config.load_secret()
        self.limiter = auth.RateLimiter()
        self.router = Router()
        from .modules import (accounts, branding, business, customers, dashboard, equipment, inventory, invoices,
                              platform_admin, services, team, tickets)
        from .modules import ledger, ticketnotes, timetracking, updates
        from .updater import UpdateChecker
        self.update_checker = UpdateChecker(config)
        updates.register(self.router)
        branding.register(self.router)
        ledger.register(self.router)
        timetracking.register(self.router)
        ticketnotes.register(self.router)
        accounts.register(self.router)
        dashboard.register(self.router)
        business.register(self.router)
        customers.register(self.router)
        equipment.register(self.router)
        tickets.register(self.router)
        services.register(self.router)
        inventory.register(self.router)
        invoices.register(self.router)
        team.register(self.router)
        platform_admin.register(self.router)
        self.migrate_all()

    # ------------------------------------------------------------ startup
    def migrate_all(self):
        cfg = self.config
        plat = connect(cfg.platform_db_path)
        try:
            migrate(plat, cfg.migrations_dir / "platform")
        finally:
            plat.close()
        cleanup_stale_provisioning(cfg)
        plat = connect(cfg.platform_db_path)
        try:
            ids = [r["id"] for r in plat.execute("SELECT id FROM tenants").fetchall()]
        finally:
            plat.close()
        for tid in ids:
            conn = open_tenant_db(cfg, tid)
            try:
                migrate(conn, cfg.migrations_dir / "tenant")
            finally:
                conn.close()

    # ------------------------------------------------------------ dispatch
    def handle(self, req):
        try:
            resp = self._dispatch(req)
        except ApiError as e:
            resp = Response.json({"error": e.message}, e.status)
        except Exception:
            # Log the traceback only (no request data) and return a generic error.
            sys.stderr.write(json.dumps({"level": "error", "msg": "unhandled exception",
                                         "trace": traceback.format_exc()}) + "\n")
            resp = Response.json({"error": "Internal server error"}, 500)
        self._decorate(resp, req)
        return resp

    def _decorate(self, resp, req):
        resp.headers += [
            ("Content-Security-Policy", CSP),
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
        ]
        if req.https:
            resp.headers.append(("Strict-Transport-Security", "max-age=31536000"))
        has_cache_header = any(k.lower() == "cache-control" for k, _ in resp.headers)
        if (req.path.startswith("/api/") or req.path == "/healthz") and not has_cache_header:
            resp.headers.append(("Cache-Control", "no-cache"))

    def _dispatch(self, req):
        if req.method in ("GET", "HEAD") and not req.path.startswith("/api/") and req.path != "/healthz":
            return self._static(req)
        self._check_origin(req)
        status, route, params = self.router.match(req.method, req.path)
        if status == "none":
            raise ApiError(404, "Not found")
        if status == "method":
            raise ApiError(405, "Method not allowed")
        ctx = Context(self, req, params)
        try:
            self._authenticate(ctx, route)
            return Response.from_result(route.handler(ctx))
        finally:
            ctx.close()

    # ------------------------------------------------------------ security checks
    def _check_origin(self, req):
        """Reject cross-site state-changing requests (defense in depth beside the CSRF token)."""
        if req.method in ("GET", "HEAD"):
            return
        origin = req.headers.get("Origin")
        if origin is None:
            return
        host = urlsplit(origin).netloc.lower() if origin != "null" else ""
        allowed = {(req.headers.get("Host") or "").lower()}
        if self.config.trust_proxy and req.headers.get("X-Forwarded-Host"):
            allowed.add(req.headers.get("X-Forwarded-Host").split(",")[0].strip().lower())
        if not host or host not in allowed:
            raise ApiError(403, "Cross-site request rejected")

    def _check_csrf(self, req, expected):
        if req.method in ("GET", "HEAD"):
            return
        given = req.headers.get("X-CSRF-Token") or ""
        if not hmac.compare_digest(given.encode(), expected.encode()):
            raise ApiError(403, "Missing or invalid CSRF token")

    def _authenticate(self, ctx, route):
        access = route.access
        if access == "public":
            return
        req = ctx.req
        if access == "platform":
            return self._auth_platform(ctx)

        raw = req.cookies.get(COOKIE_TENANT, "")
        parts = raw.split(".", 2)
        if len(parts) != 3 or parts[0] != "t" or not TENANT_ID_RE.match(parts[1]) or not parts[2]:
            raise ApiError(401, "Not signed in")
        tenant_id, token = parts[1], parts[2]
        plat = ctx.platform()
        trow = plat.execute("SELECT status FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        if trow is None or trow["status"] != "active":
            raise ApiError(401, "Not signed in")
        ctx.tenant_id = tenant_id
        ctx.db = open_tenant_db(self.config, tenant_id)
        sess = auth.lookup_session(ctx.db, "tenant", self.secret, token, self.config)
        if sess is None:
            raise ApiError(401, "Not signed in")
        row = ctx.db.execute(
            "SELECT u.id, u.email, u.name, u.active, r.id AS role_id, r.name AS role_name, r.label AS role_label "
            "FROM users u JOIN roles r ON r.id = u.role_id WHERE u.id=?", (sess["subject_id"],)).fetchone()
        if row is None or not row["active"]:
            raise ApiError(401, "Not signed in")
        self._check_csrf(req, sess["csrf"])
        perms = {r["permission"] for r in ctx.db.execute(
            "SELECT permission FROM role_permissions WHERE role_id=?", (row["role_id"],))}
        ctx.user = User(row, perms)
        ctx.session = sess
        if access != "authed" and not ctx.user.can(access):
            raise ApiError(403, "You do not have permission to do that")

    def _auth_platform(self, ctx):
        raw = ctx.req.cookies.get(COOKIE_PLATFORM, "")
        parts = raw.split(".", 1)
        if len(parts) != 2 or parts[0] != "p" or not parts[1]:
            raise ApiError(401, "Not signed in")
        plat = ctx.platform()
        sess = auth.lookup_session(plat, "platform", self.secret, parts[1], self.config)
        if sess is None:
            raise ApiError(401, "Not signed in")
        row = plat.execute("SELECT id, email, name FROM platform_admins WHERE id=? AND disabled=0",
                           (sess["subject_id"],)).fetchone()
        if row is None:
            raise ApiError(401, "Not signed in")
        self._check_csrf(ctx.req, sess["csrf"])
        ctx.admin = dict(row)
        ctx.session = sess

    # ------------------------------------------------------------ static files
    def _static(self, req):
        root = self.config.static_dir.resolve()
        from urllib.parse import unquote
        try:
            rel = unquote(req.path).lstrip("/") or "index.html"
            target = (root / rel).resolve()
            if target.is_dir():
                target = target / "index.html"
            if (root not in target.parents) or not target.is_file():
                raise ApiError(404, "Not found")
            data = target.read_bytes()
        except (ValueError, OSError):
            raise ApiError(404, "Not found")
        ctype = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        if target.suffix.lower() == ".html":
            # HTML always revalidates, and its script/style/icon URLs carry ?v=<VERSION> so a new release is picked up.
            resp = Response(200, ASSET_RE.sub(rb"\1\2?v=" + ASSET_VERSION + rb"\3", data), ctype)
            resp.headers.append(("Cache-Control", "no-cache"))
            return resp
        etag = '"%s"' % hashlib.sha256(data).hexdigest()[:32]
        # Only a URL carrying the CURRENT version may be cached for a long time; anything else revalidates via ETag.
        versioned = req.query.get("v") == [__version__]
        headers = [("ETag", etag),
                   ("Cache-Control", "public, max-age=31536000, immutable" if versioned else "no-cache")]
        sent = [t.strip() for t in (req.headers.get("If-None-Match") or "").split(",")]
        if etag in sent or "*" in sent:
            return Response(304, b"", ctype, headers)
        return Response(200, data, ctype, headers)


def main(argv):
    config = Config()
    if argv and argv[0] == "create-admin":
        return _create_admin(config, argv[1:])
    from .httpd import serve
    serve(App(config))


def _create_admin(config, args):
    import getpass
    from .util import clean_email, new_id, now_iso
    if not args:
        sys.exit("Usage: server.py create-admin EMAIL")
    email = clean_email({"email": args[0]})
    config.ensure_dirs()
    plat = connect(config.platform_db_path)
    try:
        migrate(plat, config.migrations_dir / "platform")
        if plat.execute("SELECT 1 FROM platform_admins WHERE email=?", (email,)).fetchone():
            sys.exit("An administrator with that email already exists.")
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repeat password: "):
            sys.exit("Passwords do not match.")
        err = auth.password_error(password)
        if err:
            sys.exit(err)
        plat.execute("INSERT INTO platform_admins(id, email, name, password_hash, created_at) VALUES(?,?,?,?,?)",
                     (new_id(), email, "Platform administrator", auth.hash_password(password), now_iso()))
        print("Platform administrator created: %s" % email)
    finally:
        plat.close()
