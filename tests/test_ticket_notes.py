"""Ticket notes and file attachments: validation, visibility, immutability, tenant isolation."""
import http.client
import json
import sqlite3
import unittest
from urllib.parse import quote

from harness import Client, Resp, TestServer

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n%fake\n"
OLE = bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 32


def upload(ts, client, ticket_id, name, data, csrf=True):
    conn = http.client.HTTPConnection("127.0.0.1", ts.port, timeout=30)
    headers = {"Cookie": "; ".join("%s=%s" % kv for kv in client.cookies.items()),
               "X-Filename": quote(name), "Content-Type": "application/octet-stream"}
    if csrf:
        headers["X-CSRF-Token"] = client.csrf
    conn.request("POST", "/api/tickets/%s/files" % ticket_id, body=data, headers=headers)
    r = conn.getresponse()
    raw = r.read()
    try:
        js = json.loads(raw)
    except ValueError:
        js = None
    conn.close()
    return Resp(r.status, js, raw, r.msg)


class TicketNotes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = TestServer()
        cls.owner = Client(cls.ts)
        assert cls.owner.register("Notes Repair", "notes-owner@example.com").status == 200
        cls.tech_id = cls.ts.add_user(cls.owner.tenant_id, "notes-tech@example.com", "technician")
        cls.tech = Client(cls.ts)
        assert cls.tech.login("notes-tech@example.com").status == 200
        cls.cust = cls.owner.post("/api/customers", {"name": "Notes Customer"}).json

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def ticket(self, **extra):
        body = {"customer_id": self.cust["id"], "title": "Notes ticket"}
        body.update(extra)
        r = self.owner.post("/api/tickets", body)
        self.assertEqual(r.status, 200, r.raw)
        return r.json["id"]

    def up(self, tid, name, data, client=None):
        return upload(self.ts, client or self.owner, tid, name, data)

    # ---------------------------------------------------------------- notes + files
    def test_note_with_screenshot_and_files_round_trip(self):
        tid = self.ticket()
        png = self.up(tid, "screen.png", PNG)
        pdf = self.up(tid, "quote.pdf", PDF)
        self.assertEqual((png.status, pdf.status), (200, 200), (png.raw, pdf.raw))
        self.assertTrue(png.json["is_image"] and not pdf.json["is_image"])
        r = self.owner.post("/api/tickets/%s/notes" % tid, {"body": "Cracked screen, see photo", "file_ids": [png.json["id"], pdf.json["id"]]})
        self.assertEqual(r.status, 200, r.raw)
        notes = self.owner.get("/api/tickets/%s/notes" % tid).json["notes"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["body"], "Cracked screen, see photo")
        self.assertEqual(sorted(f["filename"] for f in notes[0]["files"]), ["quote.pdf", "screen.png"])
        img = self.owner.get("/api/tickets/%s/files/%s" % (tid, png.json["id"]))
        self.assertEqual((img.status, img.raw), (200, PNG))
        self.assertEqual(img.headers["Content-Type"], "image/png")
        self.assertTrue(img.headers["Content-Disposition"].startswith("inline"))
        self.assertEqual(img.headers["X-Content-Type-Options"], "nosniff")
        forced = self.owner.get("/api/tickets/%s/files/%s?download=1" % (tid, png.json["id"]))
        self.assertTrue(forced.headers["Content-Disposition"].startswith("attachment"))
        doc = self.owner.get("/api/tickets/%s/files/%s" % (tid, pdf.json["id"]))
        self.assertTrue(doc.headers["Content-Disposition"].startswith("attachment"))
        kinds = [h["kind"] for h in self.owner.get("/api/tickets/%s" % tid).json["history"]]
        self.assertIn("note", kinds)

    def test_text_only_note_and_empty_note(self):
        tid = self.ticket()
        self.assertEqual(self.owner.post("/api/tickets/%s/notes" % tid, {"body": "  "}).status, 400)
        self.assertEqual(self.owner.post("/api/tickets/%s/notes" % tid, {"body": "Called customer"}).status, 200)
        self.assertEqual(self.owner.post("/api/tickets/%s/notes" % tid, {"body": "x" * 4001}).status, 400)

    def test_allowed_types_need_matching_contents(self):
        tid = self.ticket()
        self.assertEqual(self.up(tid, "a.txt", b"hello\nworld").status, 200)
        self.assertEqual(self.up(tid, "a.csv", b"a,b\n1,2\n").status, 200)
        self.assertEqual(self.up(tid, "a.docx", b"PK\x03\x04" + b"\x00" * 20).status, 200)
        self.assertEqual(self.up(tid, "a.xls", OLE).status, 200)
        self.assertEqual(self.up(tid, "fake.png", b"just text").status, 400)       # extension lies about the contents
        self.assertEqual(self.up(tid, "fake.pdf", b"nope").status, 400)
        self.assertEqual(self.up(tid, "bin.txt", b"abc\x00def").status, 400)

    def test_dangerous_or_unknown_types_are_refused(self):
        tid = self.ticket()
        for name in ("run.exe", "page.html", "image.svg", "script.js", "macro.docm", "noext", "evil.php", ".png.", "x.bat"):
            self.assertEqual(self.up(tid, name, PNG).status, 400, name)

    def test_filename_is_sanitised(self):
        tid = self.ticket()
        r = self.up(tid, "../../etc/evil.png", PNG)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json["filename"], "evil.png")

    def test_size_limits(self):
        tid = self.ticket()
        self.assertEqual(self.up(tid, "empty.txt", b"").status, 400)
        self.assertEqual(self.up(tid, "big.txt", b"a" * (10 * 1024 * 1024)).status, 200)
        self.assertEqual(self.up(tid, "toobig.txt", b"a" * (10 * 1024 * 1024 + 1)).status, 413)

    def test_at_most_ten_pending_files(self):
        tid = self.ticket()
        for i in range(10):
            self.assertEqual(self.up(tid, "f%d.txt" % i, b"x").status, 200)
        self.assertEqual(self.up(tid, "f10.txt", b"x").status, 409)

    def test_upload_needs_csrf_token_and_sign_in(self):
        tid = self.ticket()
        self.assertEqual(upload(self.ts, self.owner, tid, "a.txt", b"x", csrf=False).status, 403)
        self.assertEqual(upload(self.ts, Client(self.ts), tid, "a.txt", b"x").status, 401)

    # ---------------------------------------------------------------- pending files and immutability
    def test_pending_file_can_be_removed_and_only_by_its_uploader(self):
        tid = self.ticket(assignee_ids=[self.tech_id])
        f = self.up(tid, "mine.txt", b"x").json
        self.assertEqual(self.tech.request("DELETE", "/api/tickets/%s/files/%s" % (tid, f["id"])).status, 404)
        self.assertEqual(self.owner.request("DELETE", "/api/tickets/%s/files/%s" % (tid, f["id"])).status, 200)
        r = self.owner.post("/api/tickets/%s/notes" % tid, {"body": "x", "file_ids": [f["id"]]})
        self.assertEqual(r.status, 400)                                              # it is gone

    def test_cannot_attach_someone_elses_or_another_tickets_file(self):
        t1, t2 = self.ticket(assignee_ids=[self.tech_id]), self.ticket()
        theirs = self.up(t1, "tech.txt", b"x", client=self.tech).json
        self.assertEqual(self.owner.post("/api/tickets/%s/notes" % t1, {"body": "x", "file_ids": [theirs["id"]]}).status, 400)
        mine = self.up(t1, "mine.txt", b"x").json
        self.assertEqual(self.owner.post("/api/tickets/%s/notes" % t2, {"body": "x", "file_ids": [mine["id"]]}).status, 400)
        self.assertEqual(self.owner.post("/api/tickets/%s/notes" % t1, {"body": "x", "file_ids": [mine["id"], mine["id"]]}).status, 200)

    def test_pending_file_is_not_downloadable_by_others(self):
        tid = self.ticket(assignee_ids=[self.tech_id])
        f = self.up(tid, "private.txt", b"secret").json
        self.assertEqual(self.tech.get("/api/tickets/%s/files/%s" % (tid, f["id"])).status, 404)
        self.assertEqual(self.owner.get("/api/tickets/%s/files/%s" % (tid, f["id"])).status, 200)

    def test_attached_files_and_notes_are_immutable(self):
        tid = self.ticket()
        f = self.up(tid, "keep.txt", b"evidence").json
        self.assertEqual(self.owner.post("/api/tickets/%s/notes" % tid, {"body": "kept", "file_ids": [f["id"]]}).status, 200)
        self.assertEqual(self.owner.request("DELETE", "/api/tickets/%s/files/%s" % (tid, f["id"])).status, 404)
        db = self.ts.tenant_db(self.owner.tenant_id)
        try:
            for sql in ("DELETE FROM ticket_files WHERE id='%s'" % f["id"], "UPDATE ticket_files SET filename='x'",
                        "UPDATE ticket_notes SET body='changed'", "DELETE FROM ticket_notes"):
                with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                    db.execute(sql)
        finally:
            db.close()

    # ---------------------------------------------------------------- visibility and tenants
    def test_technician_only_reaches_notes_of_assigned_tickets(self):
        mine, other = self.ticket(assignee_ids=[self.tech_id]), self.ticket()
        f = self.up(other, "o.txt", b"x").json
        self.owner.post("/api/tickets/%s/notes" % other, {"body": "hidden", "file_ids": [f["id"]]})
        self.assertEqual(self.tech.get("/api/tickets/%s/notes" % other).status, 404)
        self.assertEqual(self.tech.post("/api/tickets/%s/notes" % other, {"body": "x"}).status, 404)
        self.assertEqual(self.tech.get("/api/tickets/%s/files/%s" % (other, f["id"])).status, 404)
        self.assertEqual(self.up(other, "t.txt", b"x", client=self.tech).status, 404)
        self.assertEqual(self.tech.post("/api/tickets/%s/notes" % mine, {"body": "On it"}).status, 200)
        self.assertEqual(self.owner.get("/api/tickets/%s/notes" % mine).json["notes"][0]["user_name"], "Technician")

    def test_other_shop_cannot_read_notes_or_files(self):
        tid = self.ticket()
        f = self.up(tid, "x.txt", b"x").json
        self.owner.post("/api/tickets/%s/notes" % tid, {"body": "mine", "file_ids": [f["id"]]})
        other = Client(self.ts)
        self.assertEqual(other.register("Other Notes Shop", "other-notes@example.com").status, 200)
        self.assertEqual(other.get("/api/tickets/%s/notes" % tid).status, 404)
        self.assertEqual(other.post("/api/tickets/%s/notes" % tid, {"body": "x"}).status, 404)
        self.assertEqual(other.get("/api/tickets/%s/files/%s" % (tid, f["id"])).status, 404)
        self.assertEqual(upload(self.ts, other, tid, "x.txt", b"x").status, 404)


if __name__ == "__main__":
    unittest.main()
