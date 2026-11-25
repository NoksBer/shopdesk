"""Cross-tenant access must be impossible. These tests are Phase 1 acceptance gates."""
import unittest

from harness import PASSWORD, Client, TestServer

# Every tenant-scoped route must be listed here, so adding a route without thinking about isolation
# makes test_every_route_is_accounted_for fail.
SELF_SCOPED = {            # no object ids in the URL: results come only from the caller's own database
    ("POST", "/api/logout"), ("GET", "/api/me"), ("GET", "/api/business"), ("PUT", "/api/business"),
    ("GET", "/api/users"), ("GET", "/api/audit"), ("GET", "/api/roles"),
    ("GET", "/api/invites"), ("POST", "/api/invites"),
    ("GET", "/api/customers"), ("POST", "/api/customers"),
    ("GET", "/api/equipment"), ("POST", "/api/equipment"),
    ("GET", "/api/equipment/fields"), ("PUT", "/api/equipment/fields"),
    ("GET", "/api/statuses"), ("PUT", "/api/statuses"), ("GET", "/api/staff"), ("GET", "/api/dashboard"),
    ("GET", "/api/tickets"), ("POST", "/api/tickets"),
    ("GET", "/api/tickets/numbering"), ("PUT", "/api/tickets/numbering"),
    ("GET", "/api/board"), ("GET", "/api/calendar"),
    ("GET", "/api/ledger"), ("POST", "/api/ledger"), ("GET", "/api/ledger/summary"), ("GET", "/api/ledger/export"),
    ("GET", "/api/time/running"), ("POST", "/api/time/clock-out"), ("GET", "/api/time/entries"),
    ("GET", "/api/branding"), ("PUT", "/api/branding"),
    ("GET", "/api/branding/logo"), ("POST", "/api/branding/logo"), ("DELETE", "/api/branding/logo"),
    ("GET", "/api/services"), ("POST", "/api/services"),
    ("GET", "/api/inventory"), ("POST", "/api/inventory"),
    ("GET", "/api/invoices"), ("POST", "/api/invoices"),
    ("GET", "/api/invoices/settings"), ("PUT", "/api/invoices/settings"),
}
ID_ROUTES_TESTED = {       # routes taking an id; each has an explicit cross-tenant test (here, test_team.py, test_customers.py)
    ("GET", "/api/users/<id>"), ("PUT", "/api/users/<id>"), ("DELETE", "/api/invites/<id>"),
    ("GET", "/api/customers/<id>"), ("PUT", "/api/customers/<id>"), ("DELETE", "/api/customers/<id>"),
    ("GET", "/api/equipment/<id>"), ("PUT", "/api/equipment/<id>"), ("DELETE", "/api/equipment/<id>"),
    ("GET", "/api/tickets/<id>"), ("PUT", "/api/tickets/<id>"), ("POST", "/api/tickets/<id>/status"),
    ("PUT", "/api/tickets/<id>/assignees"), ("POST", "/api/tickets/<id>/priority"),
    ("PUT", "/api/services/<id>"), ("DELETE", "/api/services/<id>"),
    ("GET", "/api/inventory/<id>"), ("PUT", "/api/inventory/<id>"), ("DELETE", "/api/inventory/<id>"),
    ("POST", "/api/inventory/<id>/adjust"),
    ("POST", "/api/ledger/<id>/reverse"),
    ("POST", "/api/tickets/<id>/clock-in"), ("GET", "/api/tickets/<id>/time"), ("POST", "/api/tickets/<id>/time"),
    ("PUT", "/api/time/<id>"), ("DELETE", "/api/time/<id>"),
    ("GET", "/api/invoices/<id>"), ("PUT", "/api/invoices/<id>"), ("DELETE", "/api/invoices/<id>"),
    ("POST", "/api/invoices/<id>/issue"), ("POST", "/api/invoices/<id>/pay"), ("POST", "/api/invoices/<id>/void"),
}


class TenantIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = TestServer()
        cls.a = Client(cls.ts)
        cls.b = Client(cls.ts)
        assert cls.a.register("Alpha Repair", "alpha@example.com").status == 200
        assert cls.b.register("Beta Repair", "beta@example.com").status == 200
        cls.a_user = cls.a.get("/api/me").json["user"]["id"]
        cls.b_user = cls.b.get("/api/me").json["user"]["id"]

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def test_every_route_is_accounted_for(self):
        tenant_routes = {(r.method, r.path) for r in self.ts.app.router.routes
                         if r.access not in ("public", "platform")}
        self.assertEqual(tenant_routes - SELF_SCOPED - ID_ROUTES_TESTED, set(),
                         "new tenant route without an isolation test")

    def test_each_tenant_has_its_own_database_file(self):
        self.assertNotEqual(self.a.tenant_id, self.b.tenant_id)
        for client, email in ((self.a, "alpha@example.com"), (self.b, "beta@example.com")):
            db = self.ts.tenant_db(client.tenant_id)
            emails = [r[0] for r in db.execute("SELECT email FROM users")]
            db.close()
            self.assertEqual(emails, [email])

    def test_user_by_id_across_tenants_is_404(self):
        self.assertEqual(self.a.get("/api/users/%s" % self.b_user).status, 404)
        self.assertEqual(self.b.get("/api/users/%s" % self.a_user).status, 404)
        self.assertEqual(self.a.get("/api/users/%s" % self.a_user).status, 200)

    def test_listings_only_show_own_data(self):
        a_users = [u["email"] for u in self.a.get("/api/users").json["users"]]
        b_users = [u["email"] for u in self.b.get("/api/users").json["users"]]
        self.assertEqual(a_users, ["alpha@example.com"])
        self.assertEqual(b_users, ["beta@example.com"])
        a_events = self.a.get("/api/audit").json["events"]
        self.assertTrue(a_events)
        self.assertNotIn(self.b.tenant_id, [e["entity_id"] for e in a_events])

    def test_business_profile_is_separate(self):
        self.assertEqual(self.a.get("/api/business").json["name"], "Alpha Repair")
        self.assertEqual(self.b.get("/api/business").json["name"], "Beta Repair")
        r = self.a.put("/api/business", {"name": "Alpha Repair", "phone": "555-0100"})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.b.get("/api/business").json["phone"], "")

    def test_forged_cookie_with_other_tenant_id_is_rejected(self):
        token = self.a.cookies["sd_session"].split(".", 2)[2]
        forged = Client(self.ts)
        forged.cookies = {"sd_session": "t.%s.%s" % (self.b.tenant_id, token)}
        self.assertEqual(forged.get("/api/me").status, 401)

    def test_malformed_tenant_ids_in_cookie_are_rejected(self):
        token = self.a.cookies["sd_session"].split(".", 2)[2]
        for bad in ("../../platform", "..%2f..", "0" * 15, "G" * 16, "", "a" * 17):
            c = Client(self.ts)
            c.cookies = {"sd_session": "t.%s.%s" % (bad, token)}
            self.assertEqual(c.get("/api/me").status, 401, bad)

    def test_email_cannot_be_reused_to_create_another_business(self):
        r = Client(self.ts).register("Gamma Repair", "alpha@example.com")
        self.assertEqual(r.status, 409)
        # and no half-created tenant is left behind
        db = self.ts.platform_db()
        names = [row[0] for row in db.execute("SELECT name FROM tenants")]
        db.close()
        self.assertNotIn("Gamma Repair", names)
        dirs = [p.name for p in self.ts.config.tenants_dir.iterdir()]
        self.assertEqual(sorted(dirs), sorted([self.a.tenant_id, self.b.tenant_id]))


class Permissions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = TestServer()
        cls.owner = Client(cls.ts)
        assert cls.owner.register("Perm Repair", "perm-owner@example.com").status == 200
        cls.ts.add_user(cls.owner.tenant_id, "tech@example.com", "technician")
        cls.ts.add_user(cls.owner.tenant_id, "desk@example.com", "front_desk")
        cls.ts.add_user(cls.owner.tenant_id, "mgr@example.com", "manager")

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def _login(self, email):
        c = Client(self.ts)
        self.assertEqual(c.login(email).status, 200)
        return c

    def test_technician_is_least_privilege(self):
        tech = self._login("tech@example.com")
        self.assertEqual(tech.get("/api/business").status, 200)
        self.assertEqual(tech.put("/api/business", {"name": "Hacked"}).status, 403)
        self.assertEqual(tech.get("/api/users").status, 403)
        self.assertEqual(tech.get("/api/audit").status, 403)
        self.assertEqual(self.owner.get("/api/business").json["name"], "Perm Repair")

    def test_front_desk_cannot_manage_business(self):
        desk = self._login("desk@example.com")
        self.assertEqual(desk.put("/api/business", {"name": "Nope"}).status, 403)

    def test_manager_can_read_team_and_audit_but_not_edit_business(self):
        mgr = self._login("mgr@example.com")
        self.assertEqual(mgr.get("/api/users").status, 200)
        self.assertEqual(mgr.get("/api/audit").status, 200)
        self.assertEqual(mgr.put("/api/business", {"name": "Nope"}).status, 403)

    def test_owner_can_edit_business_and_it_is_audited(self):
        r = self.owner.put("/api/business", {"name": "Perm Repair", "phone": "555-0199"})
        self.assertEqual(r.status, 200)
        actions = [e["action"] for e in self.owner.get("/api/audit").json["events"]]
        self.assertIn("business.updated", actions)

    def test_deactivated_user_is_locked_out(self):
        self.ts.add_user(self.owner.tenant_id, "gone@example.com", "technician")
        gone = self._login("gone@example.com")
        db = self.ts.tenant_db(self.owner.tenant_id)
        db.execute("UPDATE users SET active=0 WHERE email='gone@example.com'")
        db.close()
        self.assertEqual(gone.get("/api/me").status, 401)
        self.assertEqual(Client(self.ts).login("gone@example.com").status, 401)


if __name__ == "__main__":
    unittest.main()
