"""White labelling: brand colours, tagline and logo, stored per business and applied to the app and to invoices.
Logos are raster images only (PNG, JPEG, GIF, WebP): SVG is refused because it can carry script."""
import base64
import binascii
import re

from ..audit import audit
from ..db import tx
from ..http_util import Response
from ..util import ApiError, clean_flag, clean_text, now_iso

COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_LOGO_BYTES = 400 * 1024

GET_SQL = ("SELECT primary_color, header_color, tagline, show_name, logo_updated_at, logo_type, "
           "(logo IS NOT NULL) AS has_logo FROM branding WHERE id = 1")


def register(router):
    router.add("GET", "/api/branding", get_branding, "authed")          # every signed-in user needs it to theme the app
    router.add("PUT", "/api/branding", put_branding, "business.manage")
    router.add("GET", "/api/branding/logo", get_logo, "authed")
    router.add("POST", "/api/branding/logo", put_logo, "business.manage")
    router.add("DELETE", "/api/branding/logo", delete_logo, "business.manage")


def sniff_image(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _present(db):
    r = db.execute(GET_SQL).fetchone()
    return {"primary_color": r["primary_color"], "header_color": r["header_color"], "tagline": r["tagline"],
            "show_name": bool(r["show_name"]), "has_logo": bool(r["has_logo"]),
            "logo_version": r["logo_updated_at"] if r["has_logo"] else ""}


def _color(body, key):
    value = clean_text(body, key, max_len=7)
    if value and not COLOR_RE.match(value):
        raise ApiError(400, "%s must be a colour like #1a73e8 (or empty for the default)" % key)
    return value.lower()


def get_branding(ctx):
    return _present(ctx.db)


def put_branding(ctx):
    body = ctx.req.json()
    primary, header = _color(body, "primary_color"), _color(body, "header_color")
    tagline = clean_text(body, "tagline", max_len=120)
    show_name = clean_flag(body, "show_name", True)
    with tx(ctx.db):
        ctx.db.execute("UPDATE branding SET primary_color=?, header_color=?, tagline=?, show_name=? WHERE id=1",
                       (primary, header, tagline, show_name))
        audit(ctx.db, ctx.user.id, "settings.branding", "settings", "branding", ip=ctx.req.ip)
    return _present(ctx.db)


def put_logo(ctx):
    raw = ctx.req.json().get("data")
    if not isinstance(raw, str) or not raw:
        raise ApiError(400, "No image received")
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise ApiError(400, "The image could not be read")
    if not 1 <= len(data) <= MAX_LOGO_BYTES:
        raise ApiError(400, "Logo must be smaller than %d KB" % (MAX_LOGO_BYTES // 1024))
    kind = sniff_image(data)
    if kind is None:
        raise ApiError(400, "Logo must be a PNG, JPEG, GIF or WebP image")
    with tx(ctx.db):
        ctx.db.execute("UPDATE branding SET logo=?, logo_type=?, logo_updated_at=? WHERE id=1", (data, kind, now_iso()))
        audit(ctx.db, ctx.user.id, "settings.logo", "settings", "branding", {"bytes": len(data)}, ctx.req.ip)
    return _present(ctx.db)


def delete_logo(ctx):
    with tx(ctx.db):
        ctx.db.execute("UPDATE branding SET logo=NULL, logo_type='', logo_updated_at='' WHERE id=1")
        audit(ctx.db, ctx.user.id, "settings.logo_removed", "settings", "branding", ip=ctx.req.ip)
    return _present(ctx.db)


def get_logo(ctx):
    row = ctx.db.execute("SELECT logo, logo_type FROM branding WHERE id=1").fetchone()
    if row is None or row["logo"] is None:
        raise ApiError(404, "Not found")
    return Response(200, bytes(row["logo"]), row["logo_type"], [("Cache-Control", "private, max-age=86400")])
