import sqlite3
import unittest

from harness import PASSWORD, Client, TestServer


class AuthFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = TestServer()

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def test_register_login_logout(self):
        c = Client(self.ts)
        r = c.register("Flow Repair", "flow@example.com")
        self.assertEqual(r.status, 200)
        me = c.get("/api/me")
        self.assertEqual(me.status, 200)
        self.assertEqual(me.json["user"]["role"], "owner")
        self.assertEqual(me.json["business"]["name"], "Flow Repair")
        self.assertEqual(me.json["terminology"]["item"], "Device")
        self.assertEqual(c.post("/api/logout").status, 200)
        self.assertEqual(c.get("/api/me").status, 401)
        c2 = Client(self.ts)
        self.assertEqual(c2.login("flow@example.com").status, 200)
        self.assertEqual(c2.get("/api/me").status, 200)

    def test_logout_invalidates_server_side(self):
        c = Client(self.ts)
        c.register("Logout Repair", "logout@example.com")
        stolen = dict(c.cookies)
        c.post("/api/logout")
        thief = Client(self.ts)
        thief.cookies = stolen
        self.assertEqual(thief.get("/api/me").status, 401)

    def test_bad_login_is_generic(self):
        c = Client(self.ts)
        c.register("Generic Repair", "generic@example.com")
        wrong = Client(self.ts).login("generic@example.com", "wrong password here")
        unknown = Client(self.ts).login("nobody@example.com", "wrong password here")
        self.assertEqual(wrong.status, 401)
        self.assertEqual(unknown.status, 401)
        self.assertEqual(wrong.json, unknown.json)

    def test_password_policy(self):
        r = Client(self.ts).register("Weak Repair", "weak@example.com", password="short")
        self.assertEqual(r.status, 400)

    def test_unknown_shop_type_rejected(self):
        r = Client(self.ts).register("Odd Repair", "odd@example.com", shop_type="nope")
        self.assertEqual(r.status, 400)

    def test_csrf_required_on_state_change(self):
        c = Client(self.ts)
        c.register("Csrf Repair", "csrf@example.com")
        self.assertEqual(c.post("/api/logout", use_csrf=False).status, 403)
        self.assertEqual(c.put("/api/business", {"name": "X"}, headers={"X-CSRF-Token": "wrong"}).status, 403)
        self.assertEqual(c.get("/api/me").status, 200)   # still signed in

    def test_cross_origin_rejected(self):
        c = Client(self.ts)
        r = c.post("/api/login", {"email": "a@example.com", "password": PASSWORD},
                   headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status, 403)

    def test_json_content_type_required(self):
        c = Client(self.ts)
        r = c.post("/api/login", {"email": "a@example.com", "password": PASSWORD},
                   headers={"Content-Type": "text/plain"})
        self.assertEqual(r.status, 415)

    def test_cookie_flags_http(self):
        c = Client(self.ts)
        r = c.register("Cookie Repair", "cookie@example.com")
        cookie = [x for x in r.set_cookies() if x.startswith("sd_session=")][0]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertNotIn("Secure", cookie)           # plain HTTP in this test

    def test_security_headers(self):
        r = Client(self.ts).get("/api/bootstrap")
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.headers["Cache-Control"], "no-store")

    def test_session_idle_expiry(self):
        c = Client(self.ts)
        c.register("Idle Repair", "idle@example.com")
        db = self.ts.tenant_db(c.tenant_id)
        db.execute("UPDATE sessions SET last_seen = 1")
        db.close()
        self.assertEqual(c.get("/api/me").status, 401)

    def test_secrets_not_stored_in_clear(self):
        c = Client(self.ts)
        c.register("Hash Repair", "hash@example.com")
        token = c.cookies["sd_session"].split(".", 2)[2]
        db = self.ts.tenant_db(c.tenant_id)
        pw = db.execute("SELECT password_hash FROM users").fetchone()[0]
        hashes = [r[0] for r in db.execute("SELECT token_hash FROM sessions")]
        db.close()
        self.assertTrue(pw.startswith("scrypt$"))
        self.assertNotIn(PASSWORD, pw)
        self.assertNotIn(token, hashes)

    def test_audit_log_is_append_only(self):
        c = Client(self.ts)
        c.register("Audit Repair", "audit@example.com")
        events = c.get("/api/audit").json["events"]
        self.assertIn("tenant.created", [e["action"] for e in events])
        db = self.ts.tenant_db(c.tenant_id)
        with self.assertRaises(sqlite3.DatabaseError):
            db.execute("UPDATE audit_log SET action='x'")
        with self.assertRaises(sqlite3.DatabaseError):
            db.execute("DELETE FROM audit_log")
        db.close()

    def test_login_failure_and_success_are_audited(self):
        c = Client(self.ts)
        c.register("Audit2 Repair", "audit2@example.com")
        Client(self.ts).login("audit2@example.com", "bad password here")
        c.login("audit2@example.com")
        actions = [e["action"] for e in c.get("/api/audit").json["events"]]
        self.assertIn("auth.login_failed", actions)
        self.assertIn("auth.login", actions)

    def test_static_index_and_traversal(self):
        c = Client(self.ts)
        self.assertEqual(c.get("/").status, 200)
        self.assertEqual(c.get("/app.js").status, 200)
        self.assertEqual(c.get("/../shopdesk/config.py").status, 404)
        self.assertEqual(c.get("/%2e%2e/shopdesk/config.py").status, 404)

    def test_healthz(self):
        self.assertEqual(Client(self.ts).get("/healthz").status, 200)


class RateLimit(unittest.TestCase):
    def test_login_rate_limited_per_email(self):
        ts = TestServer()
        try:
            c = Client(ts)
            c.register("Rate Repair", "rate@example.com")
            codes = [Client(ts).login("rate@example.com", "wrong password").status for _ in range(9)]
            self.assertEqual(codes[:8], [401] * 8)
            self.assertEqual(codes[8], 429)
            # even the right password is blocked while limited
            self.assertEqual(Client(ts).login("rate@example.com").status, 429)
        finally:
            ts.close()


class ProxyCookies(unittest.TestCase):
    def test_secure_cookie_and_hsts_behind_https_proxy(self):
        ts = TestServer(SHOPDESK_TRUST_PROXY="1")
        try:
            c = Client(ts)
            r = c.post("/api/register", {"business_name": "Proxy Repair", "shop_type": "general", "name": "O",
                                         "email": "proxy@example.com", "password": PASSWORD},
                       headers={"X-Forwarded-Proto": "https"})
            self.assertEqual(r.status, 200)
            cookie = [x for x in r.set_cookies() if x.startswith("sd_session=")][0]
            self.assertIn("Secure", cookie)
            self.assertIn("Strict-Transport-Security", r.headers)
        finally:
            ts.close()


class SignupPolicy(unittest.TestCase):
    def test_registration_closes_after_first_business(self):
        ts = TestServer(SHOPDESK_OPEN_SIGNUP="0")
        try:
            self.assertTrue(Client(ts).get("/api/bootstrap").json["can_register"])
            self.assertEqual(Client(ts).register("First", "first@example.com").status, 200)
            self.assertFalse(Client(ts).get("/api/bootstrap").json["can_register"])
            self.assertEqual(Client(ts).register("Second", "second@example.com").status, 403)
        finally:
            ts.close()


class PlatformAdmin(unittest.TestCase):
    def test_suspend_and_reactivate(self):
        ts = TestServer()
        try:
            ts.create_platform_admin()
            staff = Client(ts)
            staff.register("Susp Repair", "susp@example.com")
            tid = staff.tenant_id

            self.assertEqual(staff.get("/api/platform/tenants").status, 401)   # staff cookie is not an admin session

            admin = Client(ts)
            r = admin.post("/api/platform/login", {"email": "root@example.com", "password": PASSWORD})
            self.assertEqual(r.status, 200)
            admin.csrf = r.json["csrf"]
            listing = admin.get("/api/platform/tenants").json["tenants"]
            self.assertEqual([t["id"] for t in listing], [tid])
            self.assertEqual(set(listing[0]), {"id", "name", "slug", "shop_type", "status", "created_at"})

            self.assertEqual(admin.post("/api/platform/tenants/%s/status" % tid, {"status": "suspended"}).status, 200)
            self.assertEqual(staff.get("/api/me").status, 401)
            self.assertEqual(Client(ts).login("susp@example.com").status, 401)

            self.assertEqual(admin.post("/api/platform/tenants/%s/status" % tid, {"status": "active"}).status, 200)
            self.assertEqual(Client(ts).login("susp@example.com").status, 200)

            # admin session cannot read tenant data routes
            self.assertEqual(admin.get("/api/business").status, 401)
        finally:
            ts.close()

    def test_bad_admin_login(self):
        ts = TestServer()
        try:
            ts.create_platform_admin()
            r = Client(ts).post("/api/platform/login", {"email": "root@example.com", "password": "nope nope nope"})
            self.assertEqual(r.status, 401)
        finally:
            ts.close()


if __name__ == "__main__":
    unittest.main()
