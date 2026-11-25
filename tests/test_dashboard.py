import json
import unittest
from datetime import datetime, timedelta, timezone

from harness import Client, TestServer


class Dashboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = TestServer()
        cls.owner = Client(cls.ts)
        assert cls.owner.register("Dash Shop", "dash-owner@example.com").status == 200
        tid = cls.owner.tenant_id
        cls.tech_id = cls.ts.add_user(tid, "dash-tech@example.com", "technician")
        cls.tech = Client(cls.ts)
        assert cls.tech.login("dash-tech@example.com").status == 200
        cust = cls.owner.post("/api/customers", {"name": "Dash Customer"}).json
        today = datetime.now(timezone.utc).date()
        mk = lambda **kw: cls.owner.post("/api/tickets", dict({"customer_id": cust["id"]}, **kw)).json
        cls.a = mk(title="A overdue", due_date="2000-01-01")
        cls.b = mk(title="B assigned", assignee_ids=[cls.tech_id], priority="urgent")
        cls.c = mk(title="C due soon", due_date=(today + timedelta(days=3)).isoformat())
        cls.d = mk(title="D closed")
        statuses = {s["name"]: s["id"] for s in cls.owner.get("/api/statuses").json["statuses"]}
        assert cls.owner.post("/api/tickets/%s/status" % cls.d["id"],
                              {"status_id": statuses["Completed / Closed"]}).status == 200

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def test_owner_sees_everything(self):
        d = self.owner.get("/api/dashboard").json["tickets"]
        self.assertEqual(d["counts"], {"open_count": 3, "overdue": 1, "due_week": 1, "unassigned": 2, "closed_week": 1})
        by = {s["name"]: s["n"] for s in d["by_status"]}
        self.assertEqual(by["New / Checked In"], 3)
        self.assertEqual(by["Completed / Closed"], 1)
        self.assertEqual(by["In Progress"], 0)
        self.assertEqual(d["my_open"], [])                       # owner has nothing assigned
        self.assertEqual(d["recent"][0]["kind"], "status")       # newest event first: D was closed
        self.assertEqual(d["recent"][0]["number"], self.d["number"])
        self.assertLessEqual(len(d["recent"]), 10)

    def test_technician_sees_only_assigned_work(self):
        d = self.tech.get("/api/dashboard").json["tickets"]
        self.assertEqual(d["counts"]["open_count"], 1)
        self.assertEqual(d["counts"]["overdue"], 0)
        self.assertIsNone(d["counts"]["unassigned"])
        self.assertEqual([t["number"] for t in d["my_open"]], [self.b["number"]])
        self.assertEqual({e["number"] for e in d["recent"]}, {self.b["number"]})
        self.assertEqual({s["name"]: s["n"] for s in d["by_status"]}["New / Checked In"], 1)

    def test_dashboard_requires_sign_in(self):
        self.assertEqual(Client(self.ts).get("/api/dashboard").status, 401)


class DashboardIsolation(unittest.TestCase):
    def test_other_shop_dashboard_is_empty(self):
        ts = TestServer()
        try:
            a, b = Client(ts), Client(ts)
            a.register("Alpha D", "alpha-d@example.com")
            b.register("Beta D", "beta-d@example.com")
            cust = a.post("/api/customers", {"name": "C"}).json
            a.post("/api/tickets", {"customer_id": cust["id"], "title": "Alpha only"})
            d = b.get("/api/dashboard").json["tickets"]
            self.assertEqual(d["counts"]["open_count"], 0)
            self.assertEqual(d["recent"], [])
            self.assertEqual(d["my_open"], [])
            self.assertEqual(a.get("/api/dashboard").json["tickets"]["counts"]["open_count"], 1)
        finally:
            ts.close()


class AuditViewer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = TestServer()
        cls.owner = Client(cls.ts)
        assert cls.owner.register("Audit View", "av-owner@example.com").status == 200
        cls.ts.add_user(cls.owner.tenant_id, "av-tech@example.com", "technician")
        cls.mgr_id = cls.ts.add_user(cls.owner.tenant_id, "av-mgr@example.com", "manager")
        for n in range(3):
            cls.owner.post("/api/customers", {"name": "Audit Customer %d" % n})
        cls.owner.post("/api/invites", {"email": "newbie@example.com", "role": "technician"})

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def test_filter_by_action(self):
        r = self.owner.get("/api/audit?action=customer.&limit=100").json
        self.assertEqual(len(r["events"]), 3)
        self.assertTrue(all(e["action"].startswith("customer.") for e in r["events"]))
        self.assertTrue(all(e["user_name"] == "Owner" for e in r["events"]))

    def test_paging(self):
        first = self.owner.get("/api/audit?action=customer.&limit=2").json
        self.assertEqual(len(first["events"]), 2)
        self.assertIsNotNone(first["next_before"])
        second = self.owner.get("/api/audit?action=customer.&limit=2&before=%d" % first["next_before"]).json
        self.assertEqual(len(second["events"]), 1)
        self.assertIsNone(second["next_before"])
        ids = [e["id"] for e in first["events"] + second["events"]]
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual(len(set(ids)), 3)

    def test_filter_by_user_and_wildcards(self):
        owner_id = self.owner.get("/api/me").json["user"]["id"]
        mine = self.owner.get("/api/audit?user_id=%s&limit=500" % owner_id).json["events"]
        self.assertTrue(mine and all(e["user_id"] == owner_id for e in mine))
        self.assertEqual(self.owner.get("/api/audit?user_id=%s" % self.mgr_id).json["events"], [])
        self.assertEqual(self.owner.get("/api/audit?action=%25").json["events"], [])      # '%' is literal

    def test_audit_is_permission_protected(self):
        tech = Client(self.ts)
        tech.login("av-tech@example.com")
        self.assertEqual(tech.get("/api/audit").status, 403)

    def test_events_contain_no_pii(self):
        blob = json.dumps(self.owner.get("/api/audit?limit=500").json)
        self.assertNotIn("Audit Customer", blob)
        self.assertNotIn("newbie@example.com", blob)


if __name__ == "__main__":
    unittest.main()
