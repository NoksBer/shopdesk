"""Deleted shops: files are MOVED to <data dir>/trash/ (never removed straight away), kept 30 days, restorable.

Everything here works from a tenant id that matches TENANT_ID_RE and from folder names this module generated itself
(checked again with TRASH_NAME_RE); no path ever comes from a client. No shell commands are run.
"""
import json
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone

from .audit import platform_audit
from .db import connect, tx
from .tenants import tenant_dir
from .util import ApiError, now_iso

RETENTION_DAYS = 30
TRASH_NAME_RE = re.compile(r"^[0-9a-f]{16}-\d{8}T\d{6}Z$")


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _trash_path(config, trash_name):
    if not TRASH_NAME_RE.match(trash_name or ""):
        raise ApiError(400, "Invalid trash entry")
    root = config.trash_dir.resolve()
    path = (root / trash_name).resolve()
    if path.parent != root:
        raise ApiError(400, "Invalid trash entry")
    return path


def move_to_trash(config, tenant_id, now=None):
    """Move the shop's whole folder into the trash. Returns (trash_name, purge_after_iso)."""
    now = now or datetime.now(timezone.utc)
    src = tenant_dir(config, tenant_id)
    if not src.is_dir():
        raise ApiError(404, "Not found")
    name = "%s-%s" % (tenant_id, now.strftime("%Y%m%dT%H%M%SZ"))
    config.trash_dir.mkdir(parents=True, exist_ok=True)
    dest = _trash_path(config, name)
    if dest.exists():
        raise ApiError(409, "That shop was just deleted; try again in a moment")
    shutil.move(str(src), str(dest))
    return name, _iso(now + timedelta(days=RETENTION_DAYS))


def move_back(config, tenant_id, trash_name):
    src = _trash_path(config, trash_name)
    dest = tenant_dir(config, tenant_id)
    if not src.is_dir():
        raise ApiError(409, "The deleted files are no longer in the trash")
    if dest.exists():
        raise ApiError(409, "A folder for this shop already exists")
    shutil.move(str(src), str(dest))


def purge_expired(config, now=None):
    """Permanently remove trash entries older than 30 days. Idempotent and safe to run any time or concurrently:
    it touches only folders inside the trash directory, and each entry is finished inside one database transaction.
    Returns the number of entries purged. Never raises."""
    now_text = _iso(now or datetime.now(timezone.utc))
    done = 0
    try:
        plat = connect(config.platform_db_path)
    except Exception as e:
        sys.stderr.write(json.dumps({"level": "error", "msg": "trash purge cannot open platform db", "error": str(e)}) + "\n")
        return 0
    try:
        rows = plat.execute("SELECT id, tenant_id, trash_name FROM tenant_trash WHERE restored_at IS NULL "
                            "AND purged_at IS NULL AND purge_after <= ? ORDER BY id", (now_text,)).fetchall()
        for row in rows:
            try:
                path = _trash_path(config, row["trash_name"])
                with tx(plat):
                    cur = plat.execute("UPDATE tenant_trash SET purged_at=? WHERE id=? AND restored_at IS NULL AND purged_at IS NULL",
                                       (now_text, row["id"]))
                    if cur.rowcount != 1:
                        continue                                  # another run already handled it
                    if path.exists():
                        shutil.rmtree(path)                       # the trash folder only
                    plat.execute("DELETE FROM staff_identities WHERE tenant_id=?", (row["tenant_id"],))
                    plat.execute("DELETE FROM tenants WHERE id=? AND status='suspended'", (row["tenant_id"],))
                    platform_audit(plat, None, "shop.purged", row["tenant_id"], {"trash": row["trash_name"]})
                done += 1
            except Exception as e:                                # keep going; it is retried on the next run
                sys.stderr.write(json.dumps({"level": "error", "msg": "trash purge failed", "entry": row["id"],
                                             "error": type(e).__name__ + ": " + str(e)}) + "\n")
    finally:
        plat.close()
    return done
