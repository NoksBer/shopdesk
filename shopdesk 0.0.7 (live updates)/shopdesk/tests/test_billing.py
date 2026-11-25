"""Services, inventory, invoices, board and calendar."""
import unittest
from datetime import date, timedelta

from harness import Client, TestServer


def make_shop(name, email):
    ts = TestServer()
    owner = Client(ts)
    assert owner.register(name, email).status == 200
    return ts, owner


class Services(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts, cls.owner = make_shop("Svc Shop", "svc@example.com")
        cls.ts.add_user(cls.owner.tenant_id, "svc-tech@example.com", "technician")
        cls.tech = Client(cls.ts)
        cls.tech.login("svc-tech@example.com")

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def test_crud_and_validation(self):
        r = self.owner.post("/api/services", {"name": "Screen replacement", "price_cents": 12900, "description": "Labour"})
        self.assertEqual(r.status, 201)
        sid = r.json["id"]
        self.assertEqual(self.owner.post("/api/services", {"name": "screen REPLACEMENT", "price_cents": 1}).status, 409)
        for bad in (-1, 1.5, "10", True, 10 ** 9):
            self.assertEqual(self.owner.post("/api/services", {"name": "X%s" % bad, "price_cents": bad}).status, 400, bad)
        r = self.owner.put("/api/services/%s" % sid, {"name": "Screen replacement", "price_cents": 14900})
        self.assertEqual(r.json["price_cents"], 14900)
        self.assertEqual(self.owner.request("DELETE", "/api/services/%s" % sid).status, 200)
        names = [s["name"] for s in self.owner.get("/api/services").json["services"]]
        self.assertNotIn("Screen replacement", names)
        names = [s["name"] for s in self.owner.get("/api/services?include_inactive=1").json["services"]]
        self.assertIn("Screen replacement", names)

    def test_technician_can_read_but_not_write(self):
        self.assertEqual(self.tech.get("/api/services").status, 200)
        self.assertEqual(self.tech.post("/api/services", {"name": "Nope", "price_cents": 1}).status, 403)


class Inventory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts, cls.owner = make_shop("Inv Shop", "inv@example.com")
        cls.ts.add_user(cls.owner.tenant_id, "inv-tech@example.com", "technician")
        cls.tech = Client(cls.ts)
        cls.tech.login("inv-tech@example.com")

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def _item(self, name, **kw):
        body = {"name": name, "cost_cents": 500, "price_cents": 1200, "quantity": 5, "reorder_level": 2}
        body.update(kw)
        r = self.owner.post("/api/inventory", body)
        self.assertEqual(r.status, 201, r.json)
        return r.json

    def test_create_adjust_and_movements(self):
        it = self._item("SSD 256GB", sku="SSD-256")
        self.assertEqual(it["quantity"], 5)
        r = self.owner.post("/api/inventory/%s/adjust" % it["id"], {"delta": -3, "reason": "damaged"})
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json["quantity"], 2)
        self.assertTrue(r.json["low_stock"])
        self.assertEqual([m["delta"] for m in r.json["movements"]], [-3, 5])
        self.assertEqual(self.owner.post("/api/inventory/%s/adjust" % it["id"], {"delta": -3, "reason": "x"}).status, 409)
        self.assertEqual(self.owner.post("/api/inventory/%s/adjust" % it["id"], {"delta": 0, "reason": "x"}).status, 400)
        self.assertEqual(self.owner.post("/api/inventory/%s/adjust" % it["id"], {"delta": 1}).status, 400)

    def test_duplicate_sku_rejected(self):
        self._item("Battery A", sku="BAT-1")
        self.assertEqual(self.owner.post("/api/inventory", {"name": "Battery B", "sku": "bat-1", "price_cents": 1}).status, 409)

    def test_cost_is_hidden_from_technicians(self):
        it = self._item("Cost Hidden Part")
        mine = [i for i in self.owner.get("/api/inventory").json["items"] if i["id"] == it["id"]][0]
        self.assertEqual(mine["cost_cents"], 500)
        theirs = [i for i in self.tech.get("/api/inventory").json["items"] if i["id"] == it["id"]][0]
        self.assertNotIn("cost_cents", theirs)
        self.assertNotIn("cost_cents", self.tech.get("/api/inventory/%s" % it["id"]).json)
        self.assertEqual(self.tech.post("/api/inventory", {"name": "Nope", "price_cents": 1}).status, 403)
        self.assertEqual(self.tech.post("/api/inventory/%s/adjust" % it["id"], {"delta": 1, "reason": "x"}).status, 403)

    def test_movements_are_append_only(self):
        it = self._item("Append Only")
        db = self.ts.tenant_db(self.owner.tenant_id)
        try:
            with self.assertRaises(Exception):
                db.execute("DELETE FROM inventory_movements WHERE item_id=?", (it["id"],))
            with self.assertRaises(Exception):
                db.execute("UPDATE inventory_movements SET delta=99 WHERE item_id=?", (it["id"],))
        finally:
            db.close()

    def test_low_stock_filter(self):
        self._item("Plenty", quantity=50, reorder_level=1)
        self._item("Scarce", quantity=1, reorder_level=3)
        names = [i["name"] for i in self.owner.get("/api/inventory?low_stock=1").json["items"]]
        self.assertIn("Scarce", names)
        self.assertNotIn("Plenty", names)


class Invoices(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts, cls.owner = make_shop("Bill Shop", "bill@example.com")
        cls.ts.add_user(cls.owner.tenant_id, "bill-desk@example.com", "front_desk")
        cls.ts.add_user(cls.owner.tenant_id, "bill-tech@example.com", "technician")
        cls.desk, cls.tech = Client(cls.ts), Client(cls.ts)
        cls.desk.login("bill-desk@example.com")
        cls.tech.login("bill-tech@example.com")
        cls.cust = cls.owner.post("/api/customers", {"name": "Bill Customer", "email": "c@example.com"}).json
        cls.svc = cls.owner.post("/api/services", {"name": "Diagnostic", "price_cents": 5000}).json
        cls.owner.put("/api/invoices/settings", {"prefix": "INV-", "next_number": 1001, "default_tax_bp": 700,
                                                 "terms_days": 14, "footer": "Thanks"})

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def _part(self, qty=5, price=1000, cost=400, name="Part"):
        return self.owner.post("/api/inventory", {"name": "%s %d" % (name, len(self.owner.get("/api/inventory").json["items"])),
                                                  "price_cents": price, "cost_cents": cost, "quantity": qty}).json

    def _draft(self, items=None, **kw):
        body = {"customer_id": self.cust["id"]}
        if items is not None:
            body["items"] = items
        body.update(kw)
        r = self.owner.post("/api/invoices", body)
        self.assertEqual(r.status, 201, r.json)
        return r.json

    def test_totals_and_rounding(self):
        inv = self._draft([
            {"kind": "manual", "name": "Cable", "quantity": 3, "unit_price_cents": 333, "taxable": True},
            {"kind": "manual", "name": "Labour", "quantity": 1.5, "unit_price_cents": 6000, "taxable": False},
            {"kind": "service", "ref_id": self.svc["id"], "quantity": 1},
        ])
        # 3*3.33 = 9.99 taxable, 1.5*60 = 90.00 not taxable, service 50.00 taxable (default taxable)
        self.assertEqual(inv["subtotal_cents"], 999 + 9000 + 5000)
        self.assertEqual(inv["tax_bp"], 700)                          # default from invoicing settings
        self.assertEqual(inv["tax_cents"], (5999 * 700 + 5000) // 10000)
        self.assertEqual(inv["total_cents"], inv["subtotal_cents"] + inv["tax_cents"])
        self.assertIsNone(inv["number"])
        self.assertEqual(inv["status"], "draft")

    def test_service_line_takes_catalog_defaults_and_can_be_overridden(self):
        inv = self._draft([{"kind": "service", "ref_id": self.svc["id"], "quantity": 2},
                           {"kind": "service", "ref_id": self.svc["id"], "quantity": 1, "unit_price_cents": 2500, "name": "Discounted diag"}])
        self.assertEqual(inv["items"][0]["name"], "Diagnostic")
        self.assertEqual(inv["items"][0]["line_cents"], 10000)
        self.assertEqual(inv["items"][1]["name"], "Discounted diag")
        self.assertEqual(inv["items"][1]["unit_cents"], 2500)

    def test_line_validation(self):
        bad_lines = [
            {"kind": "manual", "name": "", "quantity": 1, "unit_price_cents": 1},
            {"kind": "manual", "name": "X", "quantity": 0, "unit_price_cents": 1},
            {"kind": "manual", "name": "X", "quantity": 1.005, "unit_price_cents": 1},
            {"kind": "manual", "name": "X", "quantity": "1", "unit_price_cents": 1},
            {"kind": "manual", "name": "X", "quantity": 1},
            {"kind": "manual", "name": "X", "quantity": 1, "unit_price_cents": -5},
            {"kind": "manual", "name": "X", "quantity": 1, "unit_price_cents": 1.5},
            {"kind": "weird", "name": "X", "quantity": 1, "unit_price_cents": 1},
            {"kind": "service", "ref_id": "nope", "quantity": 1},
            {"kind": "inventory", "ref_id": "nope", "quantity": 1},
        ]
        for line in bad_lines:
            r = self.owner.post("/api/invoices", {"customer_id": self.cust["id"], "items": [line]})
            self.assertEqual(r.status, 400, line)
        part = self._part()
        r = self.owner.post("/api/invoices", {"customer_id": self.cust["id"],
                                              "items": [{"kind": "inventory", "ref_id": part["id"], "quantity": 1.5}]})
        self.assertEqual(r.status, 400)                               # parts are whole units

    def test_edit_draft_replaces_lines_and_recomputes(self):
        inv = self._draft([{"kind": "manual", "name": "A", "quantity": 1, "unit_price_cents": 1000}])
        r = self.owner.put("/api/invoices/%s" % inv["id"], {"tax_bp": 0, "notes": "hi", "due_date": "2030-01-31",
                                                           "items": [{"kind": "manual", "name": "B", "quantity": 2, "unit_price_cents": 250}]})
        self.assertEqual(r.status, 200)
        self.assertEqual([i["name"] for i in r.json["items"]], ["B"])
        self.assertEqual((r.json["subtotal_cents"], r.json["tax_cents"], r.json["total_cents"]), (500, 0, 500))
        self.assertEqual(r.json["notes"], "hi")
        self.assertEqual(r.json["due_date"], "2030-01-31")
        self.assertEqual(self.owner.put("/api/invoices/%s" % inv["id"], {"notes": "only notes"}).json["items"][0]["name"], "B")

    def test_issue_numbers_locks_and_takes_stock(self):
        part = self._part(qty=5, price=1000, cost=400)
        inv = self._draft([{"kind": "inventory", "ref_id": part["id"], "quantity": 2},
                           {"kind": "manual", "name": "Labour", "quantity": 1, "unit_price_cents": 3000}])
        r = self.owner.post("/api/invoices/%s/issue" % inv["id"])
        self.assertEqual(r.status, 200, r.json)
        self.assertEqual(r.json["status"], "issued")
        self.assertTrue(r.json["number"].startswith("INV-"))
        self.assertEqual(r.json["issue_date"], date.today().isoformat())
        self.assertEqual(r.json["due_date"], (date.today() + timedelta(days=14)).isoformat())
        self.assertEqual(r.json["bill_to"]["name"], "Bill Customer")
        self.assertEqual(self.owner.get("/api/inventory/%s" % part["id"]).json["quantity"], 3)
        # locked
        self.assertEqual(self.owner.put("/api/invoices/%s" % inv["id"], {"notes": "x"}).status, 409)
        self.assertEqual(self.owner.request("DELETE", "/api/invoices/%s" % inv["id"]).status, 409)
        # cannot issue twice, and stock is not taken twice
        self.assertEqual(self.owner.post("/api/invoices/%s/issue" % inv["id"]).status, 409)
        self.assertEqual(self.owner.get("/api/inventory/%s" % part["id"]).json["quantity"], 3)

    def test_unit_cost_is_snapshotted_from_inventory_not_client(self):
        part = self._part(cost=777)
        inv = self._draft([{"kind": "inventory", "ref_id": part["id"], "quantity": 1, "unit_cost_cents": 1}])
        db = self.ts.tenant_db(self.owner.tenant_id)
        try:
            cost = db.execute("SELECT unit_cost_cents FROM invoice_items WHERE invoice_id=?", (inv["id"],)).fetchone()[0]
        finally:
            db.close()
        self.assertEqual(cost, 777)

    def test_insufficient_stock_blocks_issue_and_changes_nothing(self):
        a, b = self._part(qty=5), self._part(qty=1)
        inv = self._draft([{"kind": "inventory", "ref_id": a["id"], "quantity": 2},
                           {"kind": "inventory", "ref_id": b["id"], "quantity": 2}])
        r = self.owner.post("/api/invoices/%s/issue" % inv["id"])
        self.assertEqual(r.status, 409)
        self.assertIn("Not enough stock", r.json["error"])
        self.assertEqual(self.owner.get("/api/inventory/%s" % a["id"]).json["quantity"], 5)     # rolled back
        self.assertEqual(self.owner.get("/api/invoices/%s" % inv["id"]).json["status"], "draft")

    def test_same_part_on_two_lines_is_summed(self):
        part = self._part(qty=3)
        inv = self._draft([{"kind": "inventory", "ref_id": part["id"], "quantity": 2},
                           {"kind": "inventory", "ref_id": part["id"], "quantity": 2}])
        self.assertEqual(self.owner.post("/api/invoices/%s/issue" % inv["id"]).status, 409)

    def test_cannot_issue_empty_invoice(self):
        self.assertEqual(self.owner.post("/api/invoices/%s/issue" % self._draft()["id"]).status, 400)

    def test_numbers_are_sequential_without_gaps(self):
        nums = []
        for _ in range(3):
            inv = self._draft([{"kind": "manual", "name": "Seq", "quantity": 1, "unit_price_cents": 100}])
            nums.append(int(self.owner.post("/api/invoices/%s/issue" % inv["id"]).json["number"][4:]))
        self.assertEqual(nums, [nums[0], nums[0] + 1, nums[0] + 2])
        junk = self._draft([{"kind": "manual", "name": "Gap?", "quantity": 1, "unit_price_cents": 100}])
        self.assertEqual(self.owner.request("DELETE", "/api/invoices/%s" % junk["id"]).status, 200)   # deleting a draft leaves no gap
        inv = self._draft([{"kind": "manual", "name": "Next", "quantity": 1, "unit_price_cents": 100}])
        self.assertEqual(int(self.owner.post("/api/invoices/%s/issue" % inv["id"]).json["number"][4:]), nums[-1] + 1)

    def test_pay_and_void_restores_stock_once(self):
        part = self._part(qty=4)
        inv = self._draft([{"kind": "inventory", "ref_id": part["id"], "quantity": 3}])
        self.owner.post("/api/invoices/%s/issue" % inv["id"])
        self.assertEqual(self.owner.get("/api/inventory/%s" % part["id"]).json["quantity"], 1)
        self.assertEqual(self.owner.post("/api/invoices/%s/pay" % inv["id"], {"method": "Cash"}).json["status"], "paid")
        self.assertEqual(self.owner.post("/api/invoices/%s/pay" % inv["id"], {"method": "Cash"}).status, 409)
        self.assertEqual(self.owner.post("/api/invoices/%s/void" % inv["id"], {}).status, 400)            # reason required
        r = self.owner.post("/api/invoices/%s/void" % inv["id"], {"reason": "customer cancelled"})
        self.assertEqual(r.json["status"], "void")
        self.assertEqual(self.owner.get("/api/inventory/%s" % part["id"]).json["quantity"], 4)
        self.assertEqual(self.owner.post("/api/invoices/%s/void" % inv["id"], {"reason": "again"}).status, 409)
        self.assertEqual(self.owner.get("/api/inventory/%s" % part["id"]).json["quantity"], 4)

    def test_draft_cannot_be_paid_or_voided(self):
        inv = self._draft([{"kind": "manual", "name": "X", "quantity": 1, "unit_price_cents": 100}])
        self.assertEqual(self.owner.post("/api/invoices/%s/pay" % inv["id"], {}).status, 409)
        self.assertEqual(self.owner.post("/api/invoices/%s/void" % inv["id"], {"reason": "x"}).status, 409)

    def test_ticket_link_must_match_customer(self):
        other = self.owner.post("/api/customers", {"name": "Other Customer"}).json
        t = self.owner.post("/api/tickets", {"customer_id": other["id"], "title": "Other job"}).json
        r = self.owner.post("/api/invoices", {"customer_id": self.cust["id"], "ticket_id": t["id"]})
        self.assertEqual(r.status, 400)
        r = self.owner.post("/api/invoices", {"customer_id": other["id"], "ticket_id": t["id"]})
        self.assertEqual(r.status, 201)
        self.assertEqual(r.json["ticket_number"], t["number"])
        listed = self.owner.get("/api/invoices?ticket_id=%s" % t["id"]).json
        self.assertEqual(listed["total"], 1)

    def test_list_filters_and_outstanding(self):
        c = self.owner.post("/api/customers", {"name": "Filter Fiona"}).json
        inv = self.owner.post("/api/invoices", {"customer_id": c["id"], "items": [
            {"kind": "manual", "name": "F", "quantity": 1, "unit_price_cents": 10000, "taxable": False}], "tax_bp": 0}).json
        self.owner.post("/api/invoices/%s/issue" % inv["id"])
        r = self.owner.get("/api/invoices?q=Fiona&status=issued").json
        self.assertEqual([i["id"] for i in r["invoices"]], [inv["id"]])
        self.assertGreaterEqual(r["outstanding_cents"], 10000)
        self.assertEqual(self.owner.get("/api/invoices?status=bogus").status, 400)
        self.assertEqual(self.owner.get("/api/invoices?q=%25").json["invoices"], [])        # '%' is literal

    def test_permissions(self):
        inv = self._draft([{"kind": "manual", "name": "P", "quantity": 1, "unit_price_cents": 100}])
        self.assertEqual(self.tech.get("/api/invoices").status, 403)
        self.assertEqual(self.tech.get("/api/invoices/%s" % inv["id"]).status, 403)
        self.assertEqual(self.tech.post("/api/invoices", {"customer_id": self.cust["id"]}).status, 403)
        self.assertEqual(self.desk.get("/api/invoices/%s" % inv["id"]).status, 200)
        self.assertEqual(self.desk.post("/api/invoices", {"customer_id": self.cust["id"]}).status, 201)
        self.assertEqual(self.desk.put("/api/invoices/settings", {"prefix": "X", "next_number": 1, "default_tax_bp": 0,
                                                                  "terms_days": 1, "footer": ""}).status, 403)

    def test_archived_customer_cannot_be_invoiced(self):
        c = self.owner.post("/api/customers", {"name": "Archived Al"}).json
        self.owner.request("DELETE", "/api/customers/%s" % c["id"])
        self.assertEqual(self.owner.post("/api/invoices", {"customer_id": c["id"]}).status, 400)

    def test_settings_validation(self):
        ok = {"prefix": "INV-", "next_number": 1001, "default_tax_bp": 700, "terms_days": 14, "footer": "Thanks"}
        for key, bad in (("prefix", "bad prefix!"), ("next_number", 0), ("default_tax_bp", 10001), ("terms_days", 400)):
            self.assertEqual(self.owner.put("/api/invoices/settings", dict(ok, **{key: bad})).status, 400, key)
        self.assertEqual(self.owner.put("/api/invoices/settings", ok).status, 200)

    def test_actions_are_audited(self):
        inv = self._draft([{"kind": "manual", "name": "Aud", "quantity": 1, "unit_price_cents": 100}])
        self.owner.post("/api/invoices/%s/issue" % inv["id"])
        actions = [e["action"] for e in self.owner.get("/api/audit?action=invoice.&limit=100").json["events"]]
        self.assertIn("invoice.created", actions)
        self.assertIn("invoice.issued", actions)


class BoardAndCalendar(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts, cls.owner = make_shop("Board Shop", "board@example.com")
        cls.tech_id = cls.ts.add_user(cls.owner.tenant_id, "board-tech@example.com", "technician")
        cls.tech = Client(cls.ts)
        cls.tech.login("board-tech@example.com")
        cls.cust = cls.owner.post("/api/customers", {"name": "Board Customer"}).json
        cls.statuses = cls.owner.get("/api/statuses").json["statuses"]

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def _ticket(self, title, **kw):
        r = self.owner.post("/api/tickets", dict({"customer_id": self.cust["id"], "title": title}, **kw))
        self.assertEqual(r.status, 201, r.json)
        return r.json

    def test_board_has_columns_and_tickets_sorted_by_priority(self):
        low = self._ticket("Board low", priority="low")
        urgent = self._ticket("Board urgent", priority="urgent")
        r = self.owner.get("/api/board").json
        self.assertEqual([s["id"] for s in r["statuses"]], [s["id"] for s in self.statuses])
        ids = [t["id"] for t in r["tickets"]]
        self.assertLess(ids.index(urgent["id"]), ids.index(low["id"]))

    def test_dragging_is_a_status_change_with_history(self):
        t = self._ticket("Board drag")
        target = [s for s in self.statuses if s["id"] != t["status_id"] and not s["is_closed"]][0]
        self.assertEqual(self.owner.post("/api/tickets/%s/status" % t["id"], {"status_id": target["id"]}).status, 200)
        moved = [x for x in self.owner.get("/api/board").json["tickets"] if x["id"] == t["id"]][0]
        self.assertEqual(moved["status_id"], target["id"])
        kinds = [h["kind"] for h in self.owner.get("/api/tickets/%s" % t["id"]).json["history"]]
        self.assertIn("status", kinds)

    def test_closed_tickets_appear_only_when_recent(self):
        t = self._ticket("Board closed")
        closed = [s for s in self.statuses if s["is_closed"]][0]
        self.owner.post("/api/tickets/%s/status" % t["id"], {"status_id": closed["id"]})
        self.assertIn(t["id"], [x["id"] for x in self.owner.get("/api/board").json["tickets"]])
        db = self.ts.tenant_db(self.owner.tenant_id)
        try:
            db.execute("UPDATE tickets SET closed_at='2001-01-01T00:00:00Z' WHERE id=?", (t["id"],))
        finally:
            db.close()
        self.assertNotIn(t["id"], [x["id"] for x in self.owner.get("/api/board").json["tickets"]])

    def test_board_filters(self):
        t = self._ticket("Board filter unique", priority="high")
        self.assertEqual([x["id"] for x in self.owner.get("/api/board?q=filter+unique").json["tickets"]], [t["id"]])
        self.assertTrue(all(x["priority"] == "high" for x in self.owner.get("/api/board?priority=high").json["tickets"]))
        self.assertEqual(self.owner.get("/api/board?priority=nope").status, 400)

    def test_technician_board_shows_only_assigned(self):
        mine = self._ticket("Board tech mine", assignee_ids=[self.tech_id])
        other = self._ticket("Board tech other")
        ids = [x["id"] for x in self.tech.get("/api/board").json["tickets"]]
        self.assertIn(mine["id"], ids)
        self.assertNotIn(other["id"], ids)

    def test_priority_endpoint(self):
        t = self._ticket("Board prio")
        r = self.owner.post("/api/tickets/%s/priority" % t["id"], {"priority": "urgent"})
        self.assertEqual(r.json["priority"], "urgent")
        self.assertIn("priority", [h["kind"] for h in r.json["history"]])
        self.assertEqual(self.owner.post("/api/tickets/%s/priority" % t["id"], {"priority": "extreme"}).status, 400)
        self.assertEqual(self.tech.post("/api/tickets/%s/priority" % t["id"], {"priority": "low"}).status, 404)   # not assigned to them

    def test_calendar_range_and_restrictions(self):
        a = self._ticket("Cal A", due_date="2031-05-10")
        b = self._ticket("Cal B", due_date="2031-05-20", assignee_ids=[self.tech_id])
        self._ticket("Cal outside", due_date="2031-07-01")
        r = self.owner.get("/api/calendar?from=2031-05-01&to=2031-05-31").json
        self.assertEqual({t["id"] for t in r["tickets"]}, {a["id"], b["id"]})
        self.assertEqual([t["due_date"] for t in r["tickets"]], ["2031-05-10", "2031-05-20"])
        r = self.tech.get("/api/calendar?from=2031-05-01&to=2031-05-31").json
        self.assertEqual({t["id"] for t in r["tickets"]}, {b["id"]})

    def test_calendar_validation(self):
        for q in ("", "from=2031-05-01", "from=bad&to=2031-05-02", "from=2031-05-10&to=2031-05-01",
                  "from=2031-01-01&to=2031-12-31", "from=2031-02-30&to=2031-03-01"):
            self.assertEqual(self.owner.get("/api/calendar?" + q).status, 400, q)


class BillingIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = TestServer()
        cls.a, cls.b = Client(cls.ts), Client(cls.ts)
        assert cls.a.register("Iso Alpha", "iso-a@example.com").status == 200
        assert cls.b.register("Iso Beta", "iso-b@example.com").status == 200
        cust = cls.a.post("/api/customers", {"name": "Alpha Cust"}).json
        cls.svc = cls.a.post("/api/services", {"name": "Alpha only", "price_cents": 100}).json
        cls.item = cls.a.post("/api/inventory", {"name": "Alpha part", "price_cents": 100, "quantity": 3}).json
        cls.inv = cls.a.post("/api/invoices", {"customer_id": cust["id"], "items": [
            {"kind": "inventory", "ref_id": cls.item["id"], "quantity": 1}]}).json

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def test_other_tenant_cannot_see_or_touch_records(self):
        b = self.b
        self.assertEqual(b.get("/api/services").json["services"], [])
        self.assertEqual(b.get("/api/inventory").json["items"], [])
        self.assertEqual(b.get("/api/invoices").json["invoices"], [])
        self.assertEqual(b.put("/api/services/%s" % self.svc["id"], {"name": "x", "price_cents": 1}).status, 404)
        self.assertEqual(b.request("DELETE", "/api/services/%s" % self.svc["id"]).status, 404)
        self.assertEqual(b.get("/api/inventory/%s" % self.item["id"]).status, 404)
        self.assertEqual(b.put("/api/inventory/%s" % self.item["id"], {"name": "x", "price_cents": 1}).status, 404)
        self.assertEqual(b.post("/api/inventory/%s/adjust" % self.item["id"], {"delta": 5, "reason": "x"}).status, 404)
        for path, body in (("", None), ("/issue", {}), ("/pay", {}), ("/void", {"reason": "x"})):
            if path == "":
                self.assertEqual(b.get("/api/invoices/%s" % self.inv["id"]).status, 404)
                self.assertEqual(b.put("/api/invoices/%s" % self.inv["id"], {"notes": "x"}).status, 404)
                self.assertEqual(b.request("DELETE", "/api/invoices/%s" % self.inv["id"]).status, 404)
            else:
                self.assertEqual(b.post("/api/invoices/%s%s" % (self.inv["id"], path), body).status, 404)
        self.assertEqual(self.a.get("/api/inventory/%s" % self.item["id"]).json["quantity"], 3)

    def test_cannot_put_another_tenants_ids_on_an_invoice(self):
        b_cust = self.b.post("/api/customers", {"name": "Beta Cust"}).json
        r = self.b.post("/api/invoices", {"customer_id": b_cust["id"], "items": [
            {"kind": "inventory", "ref_id": self.item["id"], "quantity": 1}]})
        self.assertEqual(r.status, 400)
        r = self.b.post("/api/invoices", {"customer_id": b_cust["id"], "items": [
            {"kind": "service", "ref_id": self.svc["id"], "quantity": 1}]})
        self.assertEqual(r.status, 400)
        a_cust = self.a.post("/api/customers", {"name": "Alpha Two"}).json
        self.assertEqual(self.b.post("/api/invoices", {"customer_id": a_cust["id"]}).status, 404)

    def test_board_and_calendar_are_per_tenant(self):
        cust = self.a.post("/api/customers", {"name": "Board Alpha"}).json
        self.a.post("/api/tickets", {"customer_id": cust["id"], "title": "Alpha board", "due_date": "2032-03-03"})
        self.assertEqual(self.b.get("/api/board").json["tickets"], [])
        self.assertEqual(self.b.get("/api/calendar?from=2032-03-01&to=2032-03-31").json["tickets"], [])
        self.assertEqual(len(self.a.get("/api/calendar?from=2032-03-01&to=2032-03-31").json["tickets"]), 1)


class MigrationForExistingShops(unittest.TestCase):
    def test_existing_roles_gain_new_permissions(self):
        """A business created before this release gets sensible grants when migration 0005 runs."""
        import pathlib
        import tempfile
        from shopdesk.db import connect, migrate
        mdir = pathlib.Path(__file__).resolve().parent.parent / "shopdesk" / "migrations" / "tenant"
        with tempfile.TemporaryDirectory() as d:
            conn = connect(pathlib.Path(d) / "t.db")
            old = pathlib.Path(d) / "old"
            old.mkdir()
            for f in sorted(mdir.glob("*.sql")):
                if f.name < "0005":
                    (old / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
            migrate(conn, old)
            for name in ("owner", "manager", "technician", "front_desk"):
                conn.execute("INSERT INTO roles(id, name, label, is_system) VALUES(?,?,?,1)", (name, name, name))
            migrate(conn, mdir)
            grants = {}
            for r in conn.execute("SELECT r.name, p.permission FROM roles r JOIN role_permissions p ON p.role_id = r.id"):
                grants.setdefault(r[0], set()).add(r[1])
            conn.close()
        self.assertTrue({"invoices.write", "inventory.write", "services.write"} <= grants["owner"])
        self.assertTrue({"invoices.write", "inventory.write", "services.write"} <= grants["manager"])
        self.assertTrue({"invoices.read", "invoices.write", "services.read", "inventory.read"} <= grants["front_desk"])
        self.assertNotIn("inventory.write", grants["front_desk"])
        self.assertEqual(grants["technician"], {"services.read", "inventory.read", "time.write"})


if __name__ == "__main__":
    unittest.main()
