"""Ticket notes with screenshots and files. Files live in the shop's own database (BLOBs), so they are isolated per
business and included in every backup. Visibility follows the ticket: if you cannot see the ticket you get a 404.

Safety: the stored content type comes from an allow-list keyed by file extension (never from the client), the bytes must
match that type, and everything except raster images is downloaded as an attachment, never rendered in the browser."""
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote

from ..db import tx
from ..http_util import Response
from ..util import ApiError, clean_text, new_id, now_iso
from .branding import sniff_image
from .tickets import _history, _load

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_NOTE_FILES = 10
PENDING_HOURS = 24
OLE_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")

EXT_TYPES = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp",
    "pdf": "application/pdf", "txt": "text/plain", "log": "text/plain", "csv": "text/csv", "md": "text/plain",
    "doc": "application/msword", "xls": "application/vnd.ms-excel", "ppt": "application/vnd.ms-powerpoint",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "zip": "application/zip",
}
ZIP_LIKE = ("zip", "docx", "xlsx", "pptx")
OLE_LIKE = ("doc", "xls", "ppt")
TEXT_LIKE = ("txt", "log", "csv", "md")

NOTES_SQL = ("SELECT n.id, n.body, n.created_at, n.user_id, u.name AS user_name FROM ticket_notes n "
             "LEFT JOIN users u ON u.id = n.user_id WHERE n.ticket_id = ? ORDER BY n.created_at DESC, n.rowid DESC")
FILES_SQL = ("SELECT id, note_id, filename, content_type, size FROM ticket_files "
             "WHERE ticket_id = ? AND note_id IS NOT NULL ORDER BY created_at, rowid")


def register(router):
    router.add("GET", "/api/tickets/<id>/notes", list_notes, "tickets.read")
    router.add("POST", "/api/tickets/<id>/notes", add_note, "tickets.write")
    router.add("POST", "/api/tickets/<id>/files", upload_file, "tickets.write")
    router.add("GET", "/api/tickets/<id>/files/<fid>", get_file, "tickets.read")
    router.add("DELETE", "/api/tickets/<id>/files/<fid>", delete_pending, "tickets.write")


def _file_meta(r):
    return {"id": r["id"], "filename": r["filename"], "content_type": r["content_type"], "size": r["size"],
            "is_image": r["content_type"].startswith("image/")}


def _clean_name(raw):
    name = unquote(raw or "")[:300].replace("\\", "/").split("/")[-1]
    name = "".join(c for c in name if ord(c) >= 32 and c not in '<>:"|?*\x7f').strip(" .")[:120]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in EXT_TYPES:
        raise ApiError(400, "That file type is not allowed. Allowed: images, PDF, text/CSV, Word/Excel/PowerPoint, zip.")
    return name, ext


def _content_ok(ext, data):
    if EXT_TYPES[ext].startswith("image/"):
        return sniff_image(data) == EXT_TYPES[ext]
    if ext == "pdf":
        return data.startswith(b"%PDF-")
    if ext in ZIP_LIKE:
        return data[:4] in (b"PK\x03\x04", b"PK\x05\x06")
    if ext in OLE_LIKE:
        return data.startswith(OLE_MAGIC)
    if ext in TEXT_LIKE:
        return b"\x00" not in data[:8192]
    return False


def list_notes(ctx):
    ticket = _load(ctx, ctx.params["id"])
    files = {}
    for r in ctx.db.execute(FILES_SQL, (ticket["id"],)):
        files.setdefault(r["note_id"], []).append(_file_meta(r))
    notes = [{"id": r["id"], "body": r["body"], "created_at": r["created_at"], "user_id": r["user_id"],
              "user_name": r["user_name"], "files": files.get(r["id"], [])}
             for r in ctx.db.execute(NOTES_SQL, (ticket["id"],))]
    return {"notes": notes}


def upload_file(ctx):
    """Raw body = the file; X-Filename = percent-encoded name. Creates a pending file to attach to the next note."""
    ctx.app.limiter.check("upload:" + ctx.user.id, 120, 600)
    ticket = _load(ctx, ctx.params["id"])
    data = ctx.req.body
    if not data:
        raise ApiError(400, "No file received")
    if len(data) > MAX_FILE_BYTES:
        raise ApiError(413, "Files can be at most %d MB" % (MAX_FILE_BYTES // (1024 * 1024)))
    name, ext = _clean_name(ctx.req.headers.get("X-Filename"))
    if not _content_ok(ext, data):
        raise ApiError(400, "The file's contents do not match its type (.%s)" % ext)
    db = ctx.db
    fid = new_id()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=PENDING_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with tx(db):
        db.execute("DELETE FROM ticket_files WHERE note_id IS NULL AND created_at < ?", (cutoff,))   # abandoned uploads
        pending = db.execute("SELECT COUNT(*) FROM ticket_files WHERE ticket_id=? AND user_id=? AND note_id IS NULL",
                             (ticket["id"], ctx.user.id)).fetchone()[0]
        if pending >= MAX_NOTE_FILES:
            raise ApiError(409, "At most %d files per note" % MAX_NOTE_FILES)
        db.execute("INSERT INTO ticket_files(id, ticket_id, note_id, user_id, filename, content_type, size, data, created_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (fid, ticket["id"], None, ctx.user.id, name, EXT_TYPES[ext], len(data), data, now_iso()))
    return {"id": fid, "filename": name, "content_type": EXT_TYPES[ext], "size": len(data),
            "is_image": EXT_TYPES[ext].startswith("image/")}


def delete_pending(ctx):
    ticket = _load(ctx, ctx.params["id"])
    with tx(ctx.db):
        cur = ctx.db.execute("DELETE FROM ticket_files WHERE id=? AND ticket_id=? AND user_id=? AND note_id IS NULL",
                             (ctx.params["fid"], ticket["id"], ctx.user.id))
        if cur.rowcount == 0:
            raise ApiError(404, "Not found")
    return {"ok": True}


def add_note(ctx):
    body = ctx.req.json()
    ticket = _load(ctx, ctx.params["id"])
    text = clean_text(body, "body", max_len=4000)
    ids = body.get("file_ids", [])
    if not isinstance(ids, list) or len(ids) > MAX_NOTE_FILES or not all(isinstance(x, str) for x in ids):
        raise ApiError(400, "file_ids must be a list of at most %d ids" % MAX_NOTE_FILES)
    ids = list(dict.fromkeys(ids))
    if not text and not ids:
        raise ApiError(400, "Write a note or attach a file")
    db = ctx.db
    nid, ts = new_id(), now_iso()
    with tx(db):
        for fid in ids:
            ok = db.execute("SELECT 1 FROM ticket_files WHERE id=? AND ticket_id=? AND user_id=? AND note_id IS NULL",
                            (fid, ticket["id"], ctx.user.id)).fetchone()
            if ok is None:
                raise ApiError(400, "An attached file is missing or expired. Attach it again.")
        db.execute("INSERT INTO ticket_notes(id, ticket_id, user_id, body, created_at) VALUES(?,?,?,?,?)",
                   (nid, ticket["id"], ctx.user.id, text, ts))
        for fid in ids:
            db.execute("UPDATE ticket_files SET note_id=? WHERE id=?", (nid, fid))
        _history(db, ticket["id"], ctx.user.id, "note", {"files": len(ids)})
    files = [_file_meta(r) for r in db.execute(FILES_SQL, (ticket["id"],)) if r["note_id"] == nid]
    return {"id": nid, "body": text, "created_at": ts, "user_id": ctx.user.id, "user_name": ctx.user.name, "files": files}


def get_file(ctx):
    ticket = _load(ctx, ctx.params["id"])
    row = ctx.db.execute("SELECT filename, content_type, data FROM ticket_files WHERE id=? AND ticket_id=? "
                         "AND (note_id IS NOT NULL OR user_id=?)", (ctx.params["fid"], ticket["id"], ctx.user.id)).fetchone()
    if row is None:
        raise ApiError(404, "Not found")
    inline = row["content_type"].startswith("image/") and "download" not in ctx.req.query
    fallback = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in row["filename"])
    disposition = "%s; filename=\"%s\"; filename*=UTF-8''%s" % ("inline" if inline else "attachment", fallback,
                                                               quote(row["filename"]))
    return Response(200, bytes(row["data"]), row["content_type"],
                    [("Content-Disposition", disposition), ("Cache-Control", "private, max-age=3600")])
