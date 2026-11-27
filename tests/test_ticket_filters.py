"""Tickets list: every status filter option, counts, paging, sorting and technician visibility."""
import unittest

from harness import Client, TestServer


class TicketFilters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = TestServer()
        cls.owner = Client(cls.ts)
        assert cls.owner.register("Filter Shop", "filter-owner@example.com").status == 200
        cls.tech_id = cls.ts.add_user(cls.owner.tenant_id, "filter-tech@example.com", "technician")
        cls.tech = Client(cls.ts)
        assert cls.tech.login("filter-tech@example.com").status == 200
        cls.statuses = cls.owner.get("/api/statuses").json["statuses"]
        cls.open_statuses = [s for s in cls.statuses if not s["is_closed"]]
        cls.closed_statuses = [s for s in cls.statuses if s["is_closed"]]
        assert len(cls.open_statuses) >= 2 and len(cls.closed_statuses) >= 1
        cls.cust = cls.owner.post("/api/customers", {"name": "Filter Customer"}).json["id"]
        cls.by_status = {}
        # one ticket in every status (so "Completed" and "Cancelled" style statuses are included)
        for i, s in enumerate(cls.statuses):
            t = cls.owner.post("/api/tickets", {"customer_id": cls.cust, "title": "Ticket in %s" % s["name"],
                                                "priority": ["low", "normal", "high", "urgent"][i % 4]}).json
            if s["id"] != t["status_id"]:
                r = cls.owner.post("/api/tickets/%s/status" % t["id"], {"status_id": s["id"]})
                assert r.status == 200, r.raw
            cls.by_status[s["id"]] = t["id"]
        # the technician is assigned to exactly one open ticket
        mine = cls.by_status[cls.open_statuses[0]["id"]]
        assert cls.owner.put("/api/tickets/%s/assignees" % mine, {"user_ids": [cls.tech_id]}).status == 200
        cls.mine = mine

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def ids(self, client, query=""):
        r = client.get("/api/tickets?limit=200" + query)
        self.assertEqual(r.status, 200, r.raw)
        return [t["id"] for t in r.json["tickets"]], r.json["total"]

    def test_all_statuses_returns_every_ticket_including_closed_and_cancelled(self):
        ids, total = self.ids(self.owner, "&status_id=")
        self.assertEqual(sorted(ids), sorted(self.by_status.values()))
        self.assertEqual(total, len(self.statuses))
        for s in self.closed_statuses:
            self.assertIn(self.by_status[s["id"]], ids, s["name"])

    def test_open_and_closed_groups(self):
        ids, total = self.ids(self.owner, "&status_id=open")
        self.assertEqual(sorted(ids), sorted(self.by_status[s["id"]] for s in self.open_statuses))
        self.assertEqual(total, len(self.open_statuses))
        ids, total = self.ids(self.owner, "&status_id=closed")
        self.assertEqual(sorted(ids), sorted(self.by_status[s["id"]] for s in self.closed_statuses))

    def test_every_individual_status_option_works(self):
        for s in self.statuses:
            ids, total = self.ids(self.owner, "&status_id=" + s["id"])
            self.assertEqual((ids, total), ([self.by_status[s["id"]]], 1), s["name"])

    def test_a_closed_status_is_not_cancelled_out_by_the_open_flag(self):
        """The original bug: the page always sent open=1, so choosing a closed status (or relying on 'all') hid tickets."""
        closed = self.closed_statuses[0]
        ids, _ = self.ids(self.owner, "&open=1&status_id=" + closed["id"])
        self.assertEqual(ids, [self.by_status[closed["id"]]])

    def test_empty_result_has_total_zero(self):
        ids, total = self.ids(self.owner, "&q=no-such-ticket-anywhere")
        self.assertEqual((ids, total), ([], 0))

    def test_technician_only_sees_assigned_tickets_for_every_filter(self):
        for q in ("&status_id=", "&status_id=open", "&status_id=closed", "&status_id=" + self.closed_statuses[0]["id"]):
            ids, total = self.ids(self.tech, q)
            self.assertTrue(set(ids) <= {self.mine}, q)
            self.assertEqual(total, len(ids), q)
        ids, total = self.ids(self.tech, "&status_id=")
        self.assertEqual((ids, total), ([self.mine], 1))
        self.assertEqual(self.ids(self.tech, "&status_id=" + self.closed_statuses[0]["id"]), ([], 0))
        self.assertEqual(self.ids(self.tech, "&assignee=me&status_id=")[0], [self.mine])

    def test_paging_and_total(self):
        r = self.owner.get("/api/tickets?status_id=&limit=2&offset=0").json
        r2 = self.owner.get("/api/tickets?status_id=&limit=2&offset=2").json
        self.assertEqual((len(r["tickets"]), r["total"], r["offset"]), (2, len(self.statuses), 0))
        self.assertEqual(r2["offset"], 2)
        self.assertFalse({t["id"] for t in r["tickets"]} & {t["id"] for t in r2["tickets"]})

    def test_sorting_is_whitelisted_and_stable(self):
        for key in ("number", "status", "priority", "customer", "item", "assigned", "created", "updated", "due"):
            for d in ("asc", "desc"):
                r = self.owner.get("/api/tickets?status_id=&sort=%s&dir=%s" % (key, d))
                self.assertEqual(r.status, 200, (key, d, r.raw))
                self.assertEqual(r.json["total"], len(self.statuses))
        asc = [t["number"] for t in self.owner.get("/api/tickets?status_id=&sort=number&dir=asc").json["tickets"]]
        desc = [t["number"] for t in self.owner.get("/api/tickets?status_id=&sort=number&dir=desc").json["tickets"]]
        self.assertEqual(asc, list(reversed(desc)))
        # an unknown sort key (or an injection attempt) falls back to the default order instead of reaching SQL
        r = self.owner.get("/api/tickets?status_id=&sort=" + "t.id;DROP%20TABLE%20tickets")
        self.assertEqual((r.status, r.json["total"]), (200, len(self.statuses)))

    def test_list_rows_carry_the_table_columns(self):
        t = self.owner.get("/api/tickets?status_id=").json["tickets"][0]
        for key in ("number", "status_name", "priority", "customer_name", "equipment_names", "assignee_names", "created_at",
                    "updated_at", "due_date"):
            self.assertIn(key, t)


if __name__ == "__main__":
    unittest.main()
