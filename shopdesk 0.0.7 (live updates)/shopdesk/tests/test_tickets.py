import json
import sqlite3
import unittest

from harness import Client, TestServer


def numbers(resp):
    return sorted(t["number"] for t in resp.json["tickets"])


class Tickets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = TestServer()
        cls.owner = Client(cls.ts)
        assert cls.owner.register("Ticket Shop", "ticket-owner@example.com", shop_type="computer").status == 200
        tid = cls.owner.tenant_id
        cls.tech1_id = cls.ts.add_user(tid, "tech1@example.com", "technician")
        cls.tech2_id = cls.ts.add_user(tid, "tech2@example.com", "technician")
        cls.ts.add_user(tid, "desk@example.com", "front_desk")
        cls.ts.add_user(tid, "mgr@example.com", "manager")
        cls.clients = {}
        for email in ("tech1", "tech2", "desk", "mgr"):
            c = Client(cls.ts)
            assert c.login(email + "@example.com").status == 200
            cls.clients[email] = c
        cls.tech1, cls.tech2 = cls.clients["tech1"], cls.clients["tech2"]
        cls.desk, cls.mgr = cls.clients["desk"], cls.clients["mgr"]
        cls.cust = cls.owner.post("/api/customers", {"name": "Ticket Customer", "email": "tc@example.com"}).json
        cls.equip = cls.owner.post("/api/equipment", {"customer_id": cls.cust["id"], "name": "Ticket Laptop",
                                                      "serial_number": "TK-SERIAL-9"}).json
        cls.statuses = {s["name"]: s["id"] for s in cls.owner.get("/api/statuses").json["statuses"]}

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def ticket(self, client=None, **extra):
        body = {"customer_id": self.cust["id"], "title": "Won't boot"}
        body.update(extra)
        r = (client or self.owner).post("/api/tickets", body)
        self.assertEqual(r.status, 201, r.raw)
        return r.json

    # ---------------------------------------------------------------- creation
    def test_create_defaults_numbering_and_history(self):
        t = self.ticket(equipment_ids=[self.equip["id"]], symptoms="Black screen", priority="high")
        self.assertRegex(t["number"], r"^\d+$")
        self.assertEqual(t["status_name"], "New / Checked In")
        self.assertEqual(t["priority"], "high")
        self.assertEqual([e["name"] for e in t["equipment"]], ["Ticket Laptop"])
        self.assertEqual(t["customer_name"], "Ticket Customer")
        self.assertEqual([h["kind"] for h in t["history"]], ["created"])
        t2 = self.ticket()
        self.assertEqual(int(t2["number"]), int(t["number"]) + 1)

    def test_validation(self):
        cid = self.cust["id"]
        for body in ({"customer_id": cid},                                     # no title
                     {"customer_id": cid, "title": "x", "priority": "asap"},
                     {"customer_id": cid, "title": "x", "due_date": "soon"},
                     {"customer_id": cid, "title": "x" * 151},
                     {"customer_id": cid, "title": "x", "equipment_ids": "nope"},
                     {"customer_id": cid, "title": "x", "equipment_ids": ["does-not-exist"]},
                     {"customer_id": cid, "title": "x", "assignee_ids": ["does-not-exist"]}):
            self.assertEqual(self.owner.post("/api/tickets", body).status, 400, body)
        self.assertEqual(self.owner.post("/api/tickets", {"title": "x"}).status, 404)
        self.assertEqual(self.owner.post("/api/tickets", {"customer_id": "nope", "title": "x"}).status, 404)

    def test_equipment_must_belong_to_customer_and_customer_not_archived(self):
        other = self.owner.post("/api/customers", {"name": "Someone Else"}).json
        r = self.owner.post("/api/tickets", {"customer_id": other["id"], "title": "x",
                                             "equipment_ids": [self.equip["id"]]})
        self.assertEqual(r.status, 400)
        self.owner.request("DELETE", "/api/customers/%s" % other["id"])
        self.assertEqual(self.owner.post("/api/tickets", {"customer_id": other["id"], "title": "x"}).status, 400)

    # ---------------------------------------------------------------- status
    def test_status_changes_close_and_reopen(self):
        t = self.ticket()
        done = self.statuses["Completed / Closed"]
        r = self.owner.post("/api/tickets/%s/status" % t["id"], {"status_id": done})
        self.assertEqual(r.status, 200, r.raw)
        self.assertEqual(r.json["status_name"], "Completed / Closed")
        self.assertIsNotNone(r.json["closed_at"])
        self.assertIsNotNone(r.json["closed_by"])
        r = self.owner.post("/api/tickets/%s/status" % t["id"], {"status_id": self.statuses["In Progress"]})
        self.assertIsNone(r.json["closed_at"])
        kinds = [h["kind"] for h in r.json["history"]]
        self.assertEqual(kinds, ["created", "status", "status"])
        self.assertEqual(r.json["history"][1]["detail"], {"from": "New / Checked In", "to": "Completed / Closed"})
        self.assertEqual(self.owner.post("/api/tickets/%s/status" % t["id"],
                                         {"status_id": self.statuses["In Progress"]}).status, 400)   # no-op
        self.assertEqual(self.owner.post("/api/tickets/%s/status" % t["id"], {"status_id": "bogus"}).status, 400)

    # ---------------------------------------------------------------- assignment and visibility
    def test_assignment_and_technician_visibility(self):
        t = self.ticket(title="Assigned to tech1")
        # technician cannot assign
        self.assertEqual(self.tech1.put("/api/tickets/%s/assignees" % t["id"], {"user_ids": [self.tech1_id]}).status, 403)
        r = self.desk.put("/api/tickets/%s/assignees" % t["id"], {"user_ids": [self.tech1_id]})
        self.assertEqual(r.status, 200, r.raw)
        self.assertEqual([a["id"] for a in r.json["assignees"]], [self.tech1_id])
        self.assertIn("assigned", [h["kind"] for h in r.json["history"]])

        self.assertEqual(self.tech1.get("/api/tickets/%s" % t["id"]).status, 200)
        self.assertEqual(self.tech2.get("/api/tickets/%s" % t["id"]).status, 404)           # not assigned to tech2
        self.assertNotIn(t["number"], numbers(self.tech2.get("/api/tickets?limit=200")))
        self.assertIn(t["number"], numbers(self.tech1.get("/api/tickets?limit=200")))
        self.assertIn(t["number"], numbers(self.mgr.get("/api/tickets?limit=200")))          # manager sees all
        self.assertIn(t["number"], numbers(self.tech1.get("/api/tickets?assignee=me&limit=200")))

        ok = self.tech1.post("/api/tickets/%s/status" % t["id"], {"status_id": self.statuses["In Progress"]})
        self.assertEqual(ok.status, 200)
        self.assertEqual(self.tech2.post("/api/tickets/%s/status" % t["id"],
                                         {"status_id": self.statuses["In Progress"]}).status, 404)
        self.assertEqual(self.tech2.put("/api/tickets/%s" % t["id"], {"title": "hijack"}).status, 404)

        r = self.desk.put("/api/tickets/%s/assignees" % t["id"], {"user_ids": [self.tech2_id]})
        self.assertEqual([a["id"] for a in r.json["assignees"]], [self.tech2_id])
        kinds = [h["kind"] for h in r.json["history"]]
        self.assertIn("unassigned", kinds)
        self.assertEqual(self.tech1.get("/api/tickets/%s" % t["id"]).status, 404)

    def test_staff_list_for_assigners_only(self):
        names = [s["name"] for s in self.desk.get("/api/staff").json["staff"]]
        self.assertIn("Technician", names)
        self.assertEqual(self.tech1.get("/api/staff").status, 403)

    def test_technician_created_ticket_is_auto_assigned(self):
        t = self.ticket(client=self.tech1, title="Walk-in handled by tech1")
        self.assertEqual([a["id"] for a in t["assignees"]], [self.tech1_id])
        self.assertEqual(self.tech1.get("/api/tickets/%s" % t["id"]).status, 200)
        r = self.tech1.post("/api/tickets", {"customer_id": self.cust["id"], "title": "x",
                                             "assignee_ids": [self.tech2_id]})
        self.assertEqual(r.status, 403)

    # ---------------------------------------------------------------- edit
    def test_edit_records_history(self):
        t = self.ticket(title="Old title")
        r = self.owner.put("/api/tickets/%s" % t["id"], {"title": "New title", "priority": "urgent",
                                                         "due_date": "2030-01-02", "symptoms": "Beeps"})
        self.assertEqual(r.status, 200, r.raw)
        self.assertEqual(r.json["title"], "New title")
        kinds = [h["kind"] for h in r.json["history"]]
        self.assertEqual(kinds, ["created", "priority", "due_date", "edited"])
        r = self.owner.put("/api/tickets/%s" % t["id"], {"title": "New title", "priority": "urgent",
                                                         "due_date": "2030-01-02", "symptoms": "Beeps",
                                                         "equipment_ids": [self.equip["id"]]})
        self.assertEqual([e["name"] for e in r.json["equipment"]], ["Ticket Laptop"])
        self.assertEqual(r.json["history"][-1]["kind"], "equipment")

    def test_history_is_append_only(self):
        t = self.ticket()
        db = self.ts.tenant_db(self.owner.tenant_id)
        with self.assertRaises(sqlite3.DatabaseError):
            db.execute("UPDATE ticket_history SET kind='x' WHERE ticket_id=?", (t["id"],))
        with self.assertRaises(sqlite3.DatabaseError):
            db.execute("DELETE FROM ticket_history WHERE ticket_id=?", (t["id"],))
        db.close()

    # ---------------------------------------------------------------- list filters
    def test_search_and_filters(self):
        t = self.ticket(title="Zebra-unique-title", equipment_ids=[self.equip["id"]], due_date="2000-01-01",
                        priority="urgent")
        self.assertEqual(numbers(self.owner.get("/api/tickets?q=Zebra-unique")), [t["number"]])
        self.assertEqual(numbers(self.owner.get("/api/tickets?q=TK-SERIAL-9&limit=200")).count(t["number"]), 1)
        self.assertIn(t["number"], numbers(self.owner.get("/api/tickets?q=Ticket%20Customer&limit=200")))
        self.assertIn(t["number"], numbers(self.owner.get("/api/tickets?q=%s" % t["number"])))
        self.assertIn(t["number"], numbers(self.owner.get("/api/tickets?overdue=1&limit=200")))
        self.assertIn(t["number"], numbers(self.owner.get("/api/tickets?priority=urgent&limit=200")))
        self.assertNotIn(t["number"], numbers(self.owner.get("/api/tickets?priority=low&limit=200")))
        self.assertIn(t["number"], numbers(self.owner.get("/api/tickets?open=1&limit=200")))
        self.owner.post("/api/tickets/%s/status" % t["id"], {"status_id": self.statuses["Cancelled"]})
        self.assertNotIn(t["number"], numbers(self.owner.get("/api/tickets?open=1&limit=200")))
        self.assertNotIn(t["number"], numbers(self.owner.get("/api/tickets?overdue=1&limit=200")))   # closed isn't overdue
        self.assertIn(t["number"], numbers(self.owner.get("/api/tickets?open=0&limit=200")))
        self.assertEqual(self.owner.get("/api/tickets?q=%25").json["total"], 0)
        self.assertEqual(self.owner.get("/api/tickets?q=%27%3B%20DROP%20TABLE%20tickets%3B--").json["total"], 0)

    def test_urgent_sorts_first(self):
        self.ticket(title="sort-low", priority="low")
        self.ticket(title="sort-urgent", priority="urgent")
        titles = [t["title"] for t in self.owner.get("/api/tickets?q=sort-&open=1").json["tickets"]]
        self.assertLess(titles.index("sort-urgent"), titles.index("sort-low"))

    # ---------------------------------------------------------------- numbering
    def test_numbering_configurable_and_collision_safe(self):
        self.assertEqual(self.mgr.put("/api/tickets/numbering", {"prefix": "X-", "next_number": 5}).status, 403)
        self.assertEqual(self.owner.put("/api/tickets/numbering", {"prefix": "bad prefix!", "next_number": 5}).status, 400)
        self.assertEqual(self.owner.put("/api/tickets/numbering", {"prefix": "RS-", "next_number": "x"}).status, 400)
        self.assertEqual(self.owner.put("/api/tickets/numbering", {"prefix": "RS-", "next_number": 5000}).status, 200)
        self.assertEqual(self.ticket()["number"], "RS-5000")
        self.owner.put("/api/tickets/numbering", {"prefix": "RS-", "next_number": 5000})   # point at a taken number
        self.assertEqual(self.ticket()["number"], "RS-5001")                              # skipped, no collision
        self.assertEqual(self.owner.get("/api/tickets/numbering").json["next_number"], 5002)
        self.owner.put("/api/tickets/numbering", {"prefix": "", "next_number": 900000})

    # ---------------------------------------------------------------- configurable statuses
    def test_status_configuration(self):
        current = self.owner.get("/api/statuses").json["statuses"]
        self.assertEqual(self.tech1.put("/api/statuses", {"statuses": current}).status, 403)
        self.assertEqual(self.owner.put("/api/statuses", {"statuses": [{"name": "Only open"}]}).status, 400)
        open_only = [{"name": "A"}, {"name": "B"}]
        self.assertEqual(self.owner.put("/api/statuses", {"statuses": open_only}).status, 400)   # needs a closed one
        dup = [{"name": "A"}, {"name": "a", "is_closed": True}]
        self.assertEqual(self.owner.put("/api/statuses", {"statuses": dup}).status, 400)

        t = self.ticket(title="Keeps removed status")
        waiting = self.statuses["Waiting for Parts"]
        self.owner.post("/api/tickets/%s/status" % t["id"], {"status_id": waiting})

        edited = [dict(s, is_closed=bool(s["is_closed"])) for s in current if s["name"] != "Waiting for Parts"]
        edited.insert(0, {"name": "Intake Queue", "customer_label": "Received", "is_closed": False})
        r = self.owner.put("/api/statuses", {"statuses": edited})
        self.assertEqual(r.status, 200, r.raw)
        names = [s["name"] for s in r.json["statuses"]]
        self.assertEqual(names[0], "Intake Queue")
        self.assertNotIn("Waiting for Parts", names)
        kept = self.owner.get("/api/tickets/%s" % t["id"]).json
        self.assertEqual(kept["status_name"], "Waiting for Parts")                  # history survives
        self.assertEqual(self.owner.post("/api/tickets/%s/status" % t["id"], {"status_id": waiting}).status, 400)
        self.assertEqual(self.ticket(title="New default")["status_name"], "Intake Queue")

        # put it back so other tests keep their assumptions
        restored = [dict(s, is_closed=bool(s["is_closed"])) for s in r.json["statuses"] if s["name"] != "Intake Queue"]
        restored.insert(5, {"id": waiting, "name": "Waiting for Parts", "customer_label": "Waiting for parts",
                            "is_closed": False})
        self.assertEqual(self.owner.put("/api/statuses", {"statuses": restored}).status, 200)

    def test_audit_entries(self):
        t = self.ticket(title="Audited ticket")
        self.owner.post("/api/tickets/%s/status" % t["id"], {"status_id": self.statuses["In Progress"]})
        events = self.owner.get("/api/audit?limit=500").json["events"]
        mine = sorted(e["action"] for e in events if e["entity_id"] == t["id"])
        self.assertEqual(mine, ["ticket.created", "ticket.status"])
        self.assertNotIn("Audited ticket", json.dumps(events))


class TicketIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = TestServer()
        cls.a, cls.b = Client(cls.ts), Client(cls.ts)
        assert cls.a.register("Alpha Tickets", "a-tk@example.com").status == 200
        assert cls.b.register("Beta Tickets", "b-tk@example.com").status == 200
        cls.a_cust = cls.a.post("/api/customers", {"name": "Alpha Cust"}).json
        cls.a_equip = cls.a.post("/api/equipment", {"customer_id": cls.a_cust["id"], "name": "Alpha Box"}).json
        cls.a_ticket = cls.a.post("/api/tickets", {"customer_id": cls.a_cust["id"], "title": "Alpha secret job",
                                                   "equipment_ids": [cls.a_equip["id"]]}).json
        cls.a_user = cls.a.get("/api/me").json["user"]["id"]
        cls.b_cust = cls.b.post("/api/customers", {"name": "Beta Cust"}).json
        cls.b_ticket = cls.b.post("/api/tickets", {"customer_id": cls.b_cust["id"], "title": "Beta job"}).json

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def test_other_tenants_ticket_is_404_everywhere(self):
        tid = self.a_ticket["id"]
        a_status = self.a.get("/api/statuses").json["statuses"][1]["id"]
        self.assertEqual(self.b.get("/api/tickets/%s" % tid).status, 404)
        self.assertEqual(self.b.put("/api/tickets/%s" % tid, {"title": "Hijacked"}).status, 404)
        self.assertEqual(self.b.post("/api/tickets/%s/status" % tid, {"status_id": a_status}).status, 404)
        self.assertEqual(self.b.put("/api/tickets/%s/assignees" % tid, {"user_ids": []}).status, 404)
        after = self.a.get("/api/tickets/%s" % tid).json
        self.assertEqual(after["title"], "Alpha secret job")
        self.assertEqual(after["status_name"], "New / Checked In")

    def test_cannot_reference_other_tenants_records(self):
        r = self.b.post("/api/tickets", {"customer_id": self.a_cust["id"], "title": "x"})
        self.assertEqual(r.status, 404)
        r = self.b.post("/api/tickets", {"customer_id": self.b_cust["id"], "title": "x",
                                         "equipment_ids": [self.a_equip["id"]]})
        self.assertEqual(r.status, 400)
        r = self.b.post("/api/tickets", {"customer_id": self.b_cust["id"], "title": "x",
                                         "assignee_ids": [self.a_user]})
        self.assertEqual(r.status, 400)
        a_status = self.a.get("/api/statuses").json["statuses"][1]["id"]
        self.assertEqual(self.b.post("/api/tickets/%s/status" % self.b_ticket["id"], {"status_id": a_status}).status, 400)
        self.assertEqual(self.b.put("/api/tickets/%s/assignees" % self.b_ticket["id"], {"user_ids": [self.a_user]}).status, 400)

    def test_lists_do_not_leak(self):
        self.assertEqual([t["title"] for t in self.b.get("/api/tickets").json["tickets"]], ["Beta job"])
        self.assertEqual(self.b.get("/api/tickets?q=Alpha").json["total"], 0)
        self.assertEqual(self.b.get("/api/tickets?customer_id=%s" % self.a_cust["id"]).json["total"], 0)
        a_ids = {s["id"] for s in self.a.get("/api/statuses").json["statuses"]}
        b_ids = {s["id"] for s in self.b.get("/api/statuses").json["statuses"]}
        self.assertEqual(a_ids & b_ids, set())

    def test_numbering_is_per_tenant(self):
        self.assertEqual(self.a_ticket["number"], self.b_ticket["number"])    # each shop starts at its own 1001


if __name__ == "__main__":
    unittest.main()
