"""Attach / change / remove an invoice's ticket link, and the customer's repair history with linked invoices."""
import json
import unittest

from harness import Client, TestServer


class InvoiceTicketLink(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = TestServer()
        cls.owner = Client(cls.ts)
        assert cls.owner.register("Link Shop", "link-owner@example.com").status == 200
        cls.ts.add_user(cls.owner.tenant_id, "link-tech@example.com", "technician")
        cls.ts.add_user(cls.owner.tenant_id, "link-desk@example.com", "front_desk")
        cls.tech, cls.desk = Client(cls.ts), Client(cls.ts)
        assert cls.tech.login("link-tech@example.com").status == 200
        assert cls.desk.login("link-desk@example.com").status == 200
        sts = cls.owner.get("/api/statuses").json["statuses"]
        cls.closed = next(s for s in sts if s["is_closed"])
        cls.cancelled = next((s for s in sts if s["is_closed"] and "cancel" in s["name"].lower()), None)

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def customer(self, name):
        return self.owner.post("/api/customers", {"name": name}).json["id"]

    def ticket(self, cust, title="Job"):
        r = self.owner.post("/api/tickets", {"customer_id": cust, "title": title})
        self.assertEqual(r.status, 201, r.raw)
        return r.json

    def invoice(self, cust, ticket_id=None, issue=False):
        body = {"customer_id": cust, "items": [{"kind": "manual", "name": "Labour", "quantity": 2, "unit_price_cents": 5000, "taxable": False}]}
        if ticket_id:
            body["ticket_id"] = ticket_id
        r = self.owner.post("/api/invoices", body)
        self.assertEqual(r.status, 201, r.raw)
        if issue:
            r = self.owner.post("/api/invoices/%s/issue" % r.json["id"], {})
            self.assertEqual(r.status, 200, r.raw)
        return r.json

    def link(self, invoice, ticket_id, client=None):
        return (client or self.owner).put("/api/invoices/%s/ticket" % invoice["id"], {"ticket_id": ticket_id})

    # ---- attach, change, remove
    def test_attach_change_remove_and_audit(self):
        cust = self.customer("Attach Customer")
        t1, t2 = self.ticket(cust, "First"), self.ticket(cust, "Second")
        inv = self.invoice(cust)
        self.assertIsNone(inv["ticket_id"])
        r = self.link(inv, t1["id"])
        self.assertEqual(r.status, 200, r.raw)
        self.assertEqual((r.json["ticket_id"], r.json["ticket_number"]), (t1["id"], t1["number"]))
        r = self.link(inv, t2["id"])
        self.assertEqual(r.json["ticket_id"], t2["id"])
        r = self.link(inv, None)
        self.assertEqual(r.status, 200)
        self.assertIsNone(r.json["ticket_id"])
        self.assertEqual(self.owner.get("/api/invoices?ticket_id=%s" % t1["id"]).json["total"], 0)
        events = [e for e in self.owner.get("/api/audit?limit=300").json["events"] if e["action"].startswith("invoice.ticket_")]
        self.assertEqual(sorted(e["action"] for e in events if e["entity_id"] == inv["id"]),
                         ["invoice.ticket_attached", "invoice.ticket_changed", "invoice.ticket_removed"])
        changed = next(e for e in events if e["action"] == "invoice.ticket_changed")
        detail = changed["detail"] if isinstance(changed["detail"], dict) else json.loads(changed["detail"])
        self.assertEqual((detail["from_ticket"], detail["to_ticket"]), (t1["number"], t2["number"]))

    def test_linking_never_changes_amounts_stock_or_ledger(self):
        cust = self.customer("Money Customer")
        t = self.ticket(cust)
        part = self.owner.post("/api/inventory", {"name": "Link part", "price_cents": 1000, "cost_cents": 400, "quantity": 5}).json
        inv = self.owner.post("/api/invoices", {"customer_id": cust, "items": [{"kind": "inventory", "ref_id": part["id"], "quantity": 2}]}).json
        issued = self.owner.post("/api/invoices/%s/issue" % inv["id"], {}).json
        ledger = self.owner.get("/api/ledger?limit=200").json
        stock = self.owner.get("/api/inventory/%s" % part["id"]).json["quantity"]
        r = self.link(issued, t["id"])
        self.assertEqual(r.status, 200, r.raw)
        for key in ("subtotal_cents", "tax_cents", "total_cents", "status", "number", "issue_date"):
            self.assertEqual(r.json[key], issued[key], key)
        self.assertEqual(self.owner.get("/api/ledger?limit=200").json, ledger)
        self.assertEqual(self.owner.get("/api/inventory/%s" % part["id"]).json["quantity"], stock)
        self.assertEqual(self.link(r.json, None).json["total_cents"], issued["total_cents"])
        paid = self.owner.post("/api/invoices/%s/pay" % issued["id"], {"method": "Cash"}).json
        self.assertEqual(self.link(paid, t["id"]).status, 200)                      # paid invoices can still be linked

    def test_only_open_tickets_can_be_linked(self):
        cust = self.customer("Open Rule Customer")
        t = self.ticket(cust)
        inv = self.invoice(cust)
        self.assertEqual(self.owner.post("/api/tickets/%s/status" % t["id"], {"status_id": self.closed["id"]}).status, 200)
        r = self.link(inv, t["id"])
        self.assertEqual(r.status, 400, r.raw)
        self.assertIn("open", r.json["error"].lower())
        if self.cancelled:
            t2 = self.ticket(cust)
            self.owner.post("/api/tickets/%s/status" % t2["id"], {"status_id": self.cancelled["id"]})
            self.assertEqual(self.link(inv, t2["id"]).status, 400)
        # reopening makes it eligible again
        first = self.owner.get("/api/statuses").json["statuses"][0]
        self.owner.post("/api/tickets/%s/status" % t["id"], {"status_id": first["id"]})
        self.assertEqual(self.link(inv, t["id"]).status, 200)
        # an already linked invoice can still be unlinked after its ticket closes
        self.owner.post("/api/tickets/%s/status" % t["id"], {"status_id": self.closed["id"]})
        self.assertEqual(self.link(inv, None).status, 200)

    def test_ticket_must_belong_to_the_invoices_customer(self):
        a, b = self.customer("Customer A"), self.customer("Customer B")
        ticket_b = self.ticket(b)
        inv = self.invoice(a)
        r = self.link(inv, ticket_b["id"])
        self.assertEqual(r.status, 400, r.raw)
        self.assertIn("same customer", r.json["error"])

    def test_void_invoices_cannot_be_linked_changed_or_removed(self):
        cust = self.customer("Void Customer")
        t = self.ticket(cust)
        inv = self.invoice(cust, ticket_id=t["id"], issue=True)
        self.assertEqual(self.owner.post("/api/invoices/%s/void" % inv["id"], {"reason": "mistake"}).status, 200)
        self.assertEqual(self.link(inv, None).status, 409)
        self.assertEqual(self.link(inv, self.ticket(cust)["id"]).status, 409)
        self.assertEqual(self.owner.get("/api/invoices/%s" % inv["id"]).json["ticket_id"], t["id"])

    def test_validation_and_unknown_ids(self):
        cust = self.customer("Validation Customer")
        inv = self.invoice(cust)
        self.assertEqual(self.owner.put("/api/invoices/%s/ticket" % inv["id"], {}).status, 400)
        self.assertEqual(self.owner.put("/api/invoices/%s/ticket" % inv["id"], {"ticket_id": 5}).status, 400)
        self.assertEqual(self.link(inv, "nonexistent").status, 404)
        self.assertEqual(self.owner.put("/api/invoices/nonexistent/ticket", {"ticket_id": None}).status, 404)

    def test_permissions_and_other_shops(self):
        cust = self.customer("Perm Link Customer")
        t = self.ticket(cust)
        inv = self.invoice(cust)
        self.assertEqual(self.link(inv, t["id"], self.tech).status, 403)             # technicians cannot edit invoices
        self.assertEqual(self.link(inv, t["id"], self.desk).status, 200)             # front desk can
        self.assertEqual(Client(self.ts).put("/api/invoices/%s/ticket" % inv["id"], {"ticket_id": None}).status, 401)
        other = Client(self.ts)
        self.assertEqual(other.register("Other Link Shop", "other-link@example.com").status, 200)
        self.assertEqual(other.put("/api/invoices/%s/ticket" % inv["id"], {"ticket_id": t["id"]}).status, 404)
        self.assertEqual(self.owner.get("/api/invoices/%s" % inv["id"]).json["ticket_id"], t["id"])

    def test_linked_invoices_show_on_the_ticket_listing(self):
        cust = self.customer("Ticket Page Customer")
        t = self.ticket(cust)
        inv = self.invoice(cust)
        self.link(inv, t["id"])
        listed = self.owner.get("/api/invoices?ticket_id=%s" % t["id"]).json["invoices"]
        self.assertEqual([i["id"] for i in listed], [inv["id"]])

    # ---- customer repair history
    def test_customer_history_lists_invoices_per_ticket_only_with_invoice_permission(self):
        cust = self.customer("History Customer")
        t1, t2 = self.ticket(cust, "Has invoices"), self.ticket(cust, "No invoices")
        draft = self.invoice(cust, ticket_id=t1["id"])
        issued = self.invoice(cust, ticket_id=t1["id"], issue=True)
        self.invoice(cust)                                                         # not linked to any ticket
        other_customer_inv = self.invoice(self.customer("Someone else"))
        # users who may view invoices get number, date, total, status and an id to link to, per ticket
        for client in (self.owner, self.desk):
            r = client.get("/api/invoices?customer_id=%s&limit=200" % cust)
            self.assertEqual(r.status, 200)
            by_ticket = {}
            for i in r.json["invoices"]:
                if i["ticket_id"]:
                    by_ticket.setdefault(i["ticket_id"], []).append(i)
            self.assertEqual(sorted(i["id"] for i in by_ticket[t1["id"]]), sorted([draft["id"], issued["id"]]))
            self.assertNotIn(t2["id"], by_ticket)
            self.assertNotIn(other_customer_inv["id"], [i["id"] for i in r.json["invoices"]])
            shown = next(i for i in by_ticket[t1["id"]] if i["id"] == issued["id"])
            for key in ("id", "number", "status", "total_cents", "issue_date", "ticket_id", "created_at"):
                self.assertIn(key, shown)
            self.assertEqual((shown["status"], shown["total_cents"]), ("issued", 10000))
        # a technician sees the repair history but is refused the invoices
        self.assertEqual(self.tech.get("/api/invoices?customer_id=%s&limit=200" % cust).status, 403)
        self.assertEqual(self.tech.get("/api/customers/%s" % cust).status, 200)
        self.assertEqual(self.owner.get("/api/tickets?customer_id=%s&limit=100&status_id=" % cust).json["total"], 2)

    def test_history_script_only_asks_for_invoices_when_allowed(self):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
        i = src.index("async function customerDetail")
        block = src[i:src.index("function invoiceLines", i)]
        self.assertIn('can("invoices.read")', block)
        self.assertLess(block.index('can("invoices.read")'), block.index("/api/invoices?customer_id="))


if __name__ == "__main__":
    unittest.main()
