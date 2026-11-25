"""Software updates. Server-wide, so only the PLATFORM administrator can use it (never a shop owner)."""
from .. import __version__, updater
from ..audit import platform_audit
from ..db import tx
from ..util import ApiError


def register(router):
    router.add("GET", "/api/version", version, "public")
    router.add("GET", "/api/update/check", check, "platform")
    router.add("GET", "/api/update/status", status, "platform")
    router.add("POST", "/api/update/apply", apply, "platform")


def version(ctx):
    return {"version": __version__}


def check(ctx):
    try:
        return ctx.app.update_checker.check()
    except updater.UpdateError as e:
        raise ApiError(502, str(e))


def status(ctx):
    return dict(updater.read_status(ctx.config), version=__version__)


def apply(ctx):
    """Queue an update. Takes NO input from the client: the version comes from the (cached) GitHub check of the
    configured repository, and the root-owned updater re-verifies it independently."""
    cfg = ctx.config
    ctx.app.limiter.check("update-apply:" + ctx.req.ip, 30, 600)
    if not cfg.updater_path.exists():
        raise ApiError(409, "The updater is not installed on this server. See RELEASING.md, 'Existing installs'.")
    if updater.is_busy(cfg):
        raise ApiError(409, "An update is already running.")
    try:
        info = ctx.app.update_checker.check()
    except updater.UpdateError as e:
        raise ApiError(502, str(e))
    if not info["updateAvailable"]:
        raise ApiError(409, "ShopDesk is already up to date.")
    if not info["installable"]:
        raise ApiError(409, "That release has no installable files yet (it needs shopdesk-%s.tar.gz and its .sha256)."
                       % info["tag"])
    try:
        updater.request_update(cfg, info["tag"])
    except (OSError, updater.UpdateError):
        raise ApiError(500, "Could not queue the update (is the data folder writable?).")
    plat = ctx.platform()
    with tx(plat):
        platform_audit(plat, ctx.admin["id"], "update.requested", None, {"from": __version__, "to": info["latest"]})
    return {"ok": True, "target": info["latest"]}
