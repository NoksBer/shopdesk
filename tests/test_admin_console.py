"""Platform admin console: shop list (metadata only), suspend/restore, owner password reset, delete/trash/restore/purge, audit."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from harness import PASSWORD, Client, TestServer
from shopdesk import trash
from shopdesk.app import App

ALLOWED_SHOP_KEYS = {"id", "name", "slug", "shop_type", "status", "created_at", "owner_name", "owner_email", "last_activity",
                     "user_count", "ticket_count", "db_bytes", "db_missing", "deleted_at", "purge_after", "trash_id"}


def admin_client(ts):
    ts.create_platform_admin()
    c = Client(ts)
    r = c.post("/api/platform/login", {"email": "root@example.com", "password": PASSWORD})
    assert r.status == 200, r.raw
    c.csrf = r.json["csrf"]
    return c


def make_shop(ts, name, email):
    owner = Client(ts)
    assert owner.register(name, email).status == 200
    return owner


class AdminConsole(unittest.TestCase):
    def setUp(self):
        self.ts = TestServer()
        self.admin = admin_client(self.ts)
        self.owner = make_shop(self.ts, "Console Repair", "console-owner@example.com")
        self.tid = self.owner.tenant_id

    def tearDown(self):
        self.ts.close()

    def shop(self):
        return next(s for s in self.admin.get("/api/platform/shops").json["shops"] if s["id"] == self.tid)

    # ---- access and metadata only
    def test_requires_platform_admin_and_csrf(self):
        for path in ("/api/platform/shops", "/api/platform/trash", "/api/platform/audit", "/api/platform/shops/%s/owners" % self.tid):
            self.assertEqual(Client(self.ts).get(path).status, 401, path)
            self.assertEqual(self.owner.get(path).status, 401, "a shop owner is not a platform admin: " + path)
        self.assertEqual(Client(self.ts).post("/api/platform/shops/%s/delete" % self.tid, {"confirm_name": "x"}).status, 401)
        self.assertEqual(self.admin.post("/api/platform/tenants/%s/status" % self.tid, {"status": "suspended"}, use_csrf=False).status, 403)
        self.assertEqual(self.admin.post("/api/platform/shops/%s/reset-owner-password" % self.tid, {"user_id": "x"}, use_csrf=False).status, 403)
        self.assertEqual(self.admin.post("/api/platform/shops/%s/delete" % self.tid, {"confirm_name": "Console Repair"}, use_csrf=False).status, 403)

    def test_list_returns_metadata_only(self):
        c = self.owner.post("/api/customers", {"name": "ZZ SECRET CUSTOMER", "email": "secret@example.com"}).json
        t = self.owner.post("/api/tickets", {"customer_id": c["id"], "title": "ZZ SECRET TICKET", "symptoms": "ZZ SECRET SYMPTOM"})
        self.assertEqual(t.status, 201)
        self.owner.post("/api/tickets/%s/notes" % t.json["id"], {"body": "ZZ SECRET NOTE"})
        r = self.admin.get("/api/platform/shops")
        self.assertEqual(r.status, 200)
        for secret in (b"ZZ SECRET", b"secret@example.com"):
            self.assertNotIn(secret, r.raw)
        shop = self.shop()
        self.assertLessEqual(set(shop), ALLOWED_SHOP_KEYS)
        self.assertEqual((shop["name"], shop["owner_name"], shop["owner_email"], shop["status"]),
                         ("Console Repair", "Owner", "console-owner@example.com", "active"))
        self.assertEqual((shop["user_count"], shop["ticket_count"]), (1, 1))
        self.assertGreater(shop["db_bytes"], 0)
        self.assertTrue(shop["last_activity"])
        # no admin endpoint exposes business data
        for path in ("/api/platform/shops", "/api/platform/trash", "/api/platform/audit", "/api/platform/shops/%s/owners" % self.tid):
            self.assertNotIn(b"ZZ SECRET", self.admin.get(path).raw, path)
        for forbidden in ("/api/customers", "/api/tickets", "/api/invoices"):
            self.assertEqual(self.admin.get(forbidden).status, 401, "platform session must not open shop data: " + forbidden)

    def test_suspend_and_restore(self):
        self.assertEqual(self.admin.post("/api/platform/tenants/%s/status" % self.tid, {"status": "suspended"}).status, 200)
        self.assertEqual(self.shop()["status"], "suspended")
        self.assertEqual(Client(self.ts).login("console-owner@example.com").status, 401)
        self.assertEqual(self.admin.post("/api/platform/tenants/%s/status" % self.tid, {"status": "active"}).status, 200)
        self.assertEqual(Client(self.ts).login("console-owner@example.com").status, 200)

    # ---- owner password reset
    def owner_id(self):
        return self.admin.get("/api/platform/shops/%s/owners" % self.tid).json["owners"][0]["id"]

    def reset(self, **extra):
        body = {"user_id": self.owner_id()}
        body.update(extra)
        return self.admin.post("/api/platform/shops/%s/reset-owner-password" % self.tid, body)

    def test_reset_is_one_time_and_forces_change(self):
        r = self.reset(password="admin-chosen-1")                       # a chosen password is ignored
        self.assertEqual(r.status, 200, r.raw)
        temp = r.json["temporary_password"]
        self.assertNotEqual(temp, "admin-chosen-1")
        self.assertEqual(len(temp), 16)
        self.assertIn("no-store", r.headers.get("Cache-Control"))
        db = self.ts.tenant_db(self.tid)
        row = db.execute("SELECT password_hash, must_change_password FROM users WHERE email='console-owner@example.com'").fetchone()
        db.close()
        self.assertTrue(row["password_hash"].startswith("scrypt$"))
        self.assertNotIn(temp, row["password_hash"])
        self.assertEqual(row["must_change_password"], 1)
        # the old session and the old password are gone
        self.assertEqual(self.owner.get("/api/me").status, 401)
        self.assertEqual(Client(self.ts).login("console-owner@example.com", PASSWORD).status, 401)
        self.assertEqual(Client(self.ts).login("console-owner@example.com", "admin-chosen-1").status, 401)
        # first use works, but only the password change is allowed
        user = Client(self.ts)
        self.assertEqual(user.login("console-owner@example.com", temp).status, 200)
        self.assertTrue(user.get("/api/me").json["user"]["must_change_password"])
        self.assertEqual(user.get("/api/customers").status, 403)
        self.assertEqual(user.get("/api/business").status, 403)
        # the temporary password cannot be used a second time
        self.assertEqual(Client(self.ts).login("console-owner@example.com", temp).status, 401)
        # changing the password ends the restriction
        self.assertEqual(user.post("/api/account/password", {"current_password": temp, "new_password": "brand new secret 1"}).status, 200)
        self.assertEqual(user.get("/api/customers").status, 200)
        self.assertFalse(user.get("/api/me").json["user"]["must_change_password"])
        self.assertEqual(Client(self.ts).login("console-owner@example.com", "brand new secret 1").status, 200)
        self.assertEqual(Client(self.ts).login("console-owner@example.com", temp).status, 401)

    def test_reset_expires_after_24_hours(self):
        temp = self.reset().json["temporary_password"]
        db = self.ts.tenant_db(self.tid)
        row = db.execute("SELECT temp_password_expires_at FROM users WHERE email='console-owner@example.com'").fetchone()
        import time
        self.assertTrue(23 * 3600 < row[0] - time.time() <= 24 * 3600 + 5)
        db.execute("UPDATE users SET temp_password_expires_at=? WHERE email='console-owner@example.com'", (int(time.time()) - 1,))
        db.close()
        self.assertEqual(Client(self.ts).login("console-owner@example.com", temp).status, 401)

    def test_reset_only_for_an_owner_of_that_shop(self):
        tech = self.ts.add_user(self.tid, "console-tech@example.com", "technician")
        self.assertEqual(self.admin.post("/api/platform/shops/%s/reset-owner-password" % self.tid, {"user_id": tech}).status, 404)
        other = make_shop(self.ts, "Other Repair", "other-owner@example.com")
        other_owner = self.admin.get("/api/platform/shops/%s/owners" % other.tenant_id).json["owners"][0]["id"]
        self.assertEqual(self.admin.post("/api/platform/shops/%s/reset-owner-password" % self.tid, {"user_id": other_owner}).status, 404)
        self.assertEqual(self.admin.post("/api/platform/shops/%s/reset-owner-password" % ("0" * 16), {"user_id": other_owner}).status, 404)

    def test_reset_is_rate_limited(self):
        statuses = [self.reset().status for _ in range(6)]
        self.assertEqual(statuses[:5], [200] * 5)
        self.assertEqual(statuses[5], 429)

    # ---- delete, trash, restore, purge
    def test_delete_requires_exact_name_then_moves_to_trash_and_restores(self):
        url = "/api/platform/shops/%s/delete" % self.tid
        self.assertEqual(self.admin.post(url, {"confirm_name": "console repair"}).status, 400)
        self.assertEqual(self.admin.post(url, {}).status, 400)
        self.assertTrue((self.ts.config.tenants_dir / self.tid / "tenant.db").exists())
        sentinel = self.ts.config.data_dir / "keep-me.txt"
        sentinel.write_text("x")
        r = self.admin.post(url, {"confirm_name": "Console Repair"})
        self.assertEqual(r.status, 200, r.raw)
        self.assertFalse((self.ts.config.tenants_dir / self.tid).exists())                     # moved, not left behind
        moved = [p.name for p in self.ts.config.trash_dir.iterdir()]
        self.assertEqual(len(moved), 1)
        self.assertTrue(moved[0].startswith(self.tid + "-"))
        self.assertTrue((self.ts.config.trash_dir / moved[0] / "tenant.db").exists())          # nothing deleted outright
        self.assertTrue(sentinel.exists())
        self.assertEqual(self.shop()["status"], "pending deletion")
        self.assertEqual(Client(self.ts).login("console-owner@example.com").status, 401)
        listed = self.admin.get("/api/platform/trash").json["deleted"]
        self.assertEqual([d["name"] for d in listed], ["Console Repair"])
        self.assertEqual(self.admin.post("/api/platform/tenants/%s/status" % self.tid, {"status": "active"}).status, 409)
        App(self.ts.config)                                                                    # a restart with a shop in the trash must work
        # restore
        self.assertEqual(self.admin.post("/api/platform/trash/%s/restore" % listed[0]["id"], {}).status, 200)
        self.assertTrue((self.ts.config.tenants_dir / self.tid / "tenant.db").exists())
        self.assertEqual(self.admin.get("/api/platform/trash").json["deleted"], [])
        self.assertEqual(self.shop()["status"], "suspended")
        self.assertEqual(Client(self.ts).login("console-owner@example.com").status, 401)
        self.assertEqual(self.admin.post("/api/platform/tenants/%s/status" % self.tid, {"status": "active"}).status, 200)
        self.assertEqual(Client(self.ts).login("console-owner@example.com").status, 200)
        self.assertEqual(self.admin.post("/api/platform/trash/%s/restore" % listed[0]["id"], {}).status, 404)

    def test_purge_after_30_days_is_idempotent_and_touches_only_the_trash(self):
        other = make_shop(self.ts, "Survivor Repair", "survivor@example.com")
        sentinel = self.ts.config.data_dir / "keep-me.txt"
        sentinel.write_text("x")
        self.assertEqual(self.admin.post("/api/platform/shops/%s/delete" % self.tid, {"confirm_name": "Console Repair"}).status, 200)
        self.assertEqual(trash.purge_expired(self.ts.config), 0)                               # not old enough yet
        self.assertEqual(len(list(self.ts.config.trash_dir.iterdir())), 1)
        row = self.admin.get("/api/platform/trash").json["deleted"][0]
        self.assertLessEqual(datetime.fromisoformat(row["purge_after"].replace("Z", "+00:00")) - datetime.now(timezone.utc),
                             timedelta(days=30, minutes=1))
        later = datetime.now(timezone.utc) + timedelta(days=31)
        self.assertEqual(trash.purge_expired(self.ts.config, now=later), 1)
        self.assertEqual(list(self.ts.config.trash_dir.iterdir()), [])
        self.assertEqual(trash.purge_expired(self.ts.config, now=later), 0)                    # running again does nothing
        self.assertTrue(sentinel.exists())
        self.assertTrue((self.ts.config.tenants_dir / other.tenant_id / "tenant.db").exists())
        self.assertEqual(Client(self.ts).login("survivor@example.com").status, 200)
        self.assertNotIn(self.tid, [s["id"] for s in self.admin.get("/api/platform/shops").json["shops"]])
        self.assertEqual(self.admin.get("/api/platform/trash").json["deleted"], [])
        # the owner's email is free again once the shop is gone for good
        self.assertEqual(Client(self.ts).register("Reborn Repair", "console-owner@example.com").status, 200)

    def test_purge_ignores_forged_trash_names(self):
        victim = self.ts.config.data_dir / "do-not-touch"
        victim.mkdir()
        db = self.ts.platform_db()
        db.execute("INSERT INTO tenant_trash(tenant_id, name, trash_name, deleted_at, purge_after) VALUES(?,?,?,?,?)",
                   ("f" * 16, "Forged", "../do-not-touch", "2000-01-01T00:00:00Z", "2000-01-31T00:00:00Z"))
        db.close()
        self.assertEqual(trash.purge_expired(self.ts.config), 0)
        self.assertTrue(victim.exists())

    def test_delete_blocked_while_an_update_is_running(self):
        with mock.patch("shopdesk.updater.is_busy", return_value=True):
            r = self.admin.post("/api/platform/shops/%s/delete" % self.tid, {"confirm_name": "Console Repair"})
        self.assertEqual(r.status, 409)
        self.assertTrue((self.ts.config.tenants_dir / self.tid / "tenant.db").exists())
        self.assertEqual(self.shop()["status"], "active")

    def test_admin_actions_are_in_the_admin_audit_log(self):
        self.admin.post("/api/platform/tenants/%s/status" % self.tid, {"status": "suspended"})
        self.admin.post("/api/platform/tenants/%s/status" % self.tid, {"status": "active"})
        self.reset()
        self.admin.post("/api/platform/shops/%s/delete" % self.tid, {"confirm_name": "Console Repair"})
        events = self.admin.get("/api/platform/audit").json["events"]
        actions = [e["action"] for e in events]
        for a in ("platform.login", "tenant.status", "owner.password_reset", "shop.deleted"):
            self.assertIn(a, actions)
        deleted = next(e for e in events if e["action"] == "shop.deleted")
        self.assertEqual((deleted["actor_email"], deleted["shop_name"], deleted["tenant_id"]), ("root@example.com", "Console Repair", self.tid))
        self.assertNotIn("temporary_password", self.admin.get("/api/platform/audit").raw.decode())


if __name__ == "__main__":
    unittest.main()
