"""CSV import: field mapping data, validation, duplicates (skip / update), batches, report, history, permissions, isolation."""
import re
import unittest

from harness import Client, TestServer


def rows(*dicts, start=2):
    return [{"line": start + i, "values": d} for i, d in enumerate(dicts)]


class Import(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = TestServer()
        cls.owner = Client(cls.ts)
        assert cls.owner.register("Import Shop", "import-owner@example.com").status == 200
        for email, role in (("imp-mgr@example.com", "manager"), ("imp-tech@example.com", "technician"), ("imp-desk@example.com", "front_desk")):
            cls.ts.add_user(cls.owner.tenant_id, email, role)
        cls.mgr, cls.tech, cls.desk = Client(cls.ts), Client(cls.ts), Client(cls.ts)
        assert cls.mgr.login("imp-mgr@example.com").status == 200
        assert cls.tech.login("imp-tech@example.com").status == 200
        assert cls.desk.login("imp-desk@example.com").status == 200

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def run_import(self, client, kind, data, mode="skip", filename="x.csv"):
        start = client.post("/api/import/%s/start" % kind, {"filename": filename, "mode": mode, "total": len(data)})
        self.assertEqual(start.status, 201, start.raw)
        iid = start.json["import_id"]
        results = []
        for i in range(0, len(data), 3):                                      # small batches on purpose
            r = client.post("/api/import/%s/batch" % kind, {"import_id": iid, "rows": data[i:i + 3]})
            self.assertEqual(r.status, 200, r.raw)
            results += r.json["results"]
        fin = client.post("/api/import/%s/finish" % kind, {"import_id": iid})
        self.assertEqual(fin.status, 200, fin.raw)
        return results, fin.json["import"]

    # ---- access
    def test_only_owner_and_manager_can_import(self):
        for c in (self.owner, self.mgr):
            self.assertEqual(c.get("/api/import/types").status, 200)
        for c in (self.tech, self.desk):
            for path in ("/api/import/types", "/api/import/history", "/api/import/sample/customers"):
                self.assertEqual(c.get(path).status, 403, path)
            self.assertEqual(c.post("/api/import/customers/preview", {"rows": rows({"name": "x"})}).status, 403)
            self.assertEqual(c.post("/api/import/customers/start", {"total": 1}).status, 403)
        anon = Client(self.ts)
        self.assertEqual(anon.get("/api/import/types").status, 401)

    def test_types_and_samples(self):
        types = self.owner.get("/api/import/types").json["types"]
        self.assertEqual([t["key"] for t in types], ["customers", "inventory", "services"])
        cust = next(t for t in types if t["key"] == "customers")
        self.assertIn("customer", next(f for f in cust["fields"] if f["key"] == "name")["aliases"])   # QuickBooks "Customer" column
        for kind in ("customers", "inventory", "services"):
            r = self.owner.get("/api/import/sample/" + kind)
            self.assertEqual(r.status, 200)
            self.assertIn("attachment", r.headers.get("Content-Disposition"))
            self.assertGreaterEqual(len(r.raw.decode().strip().splitlines()), 3)
        self.assertEqual(self.owner.get("/api/import/sample/invoices").status, 404)       # invoices, payments, tickets cannot be imported
        for kind in ("invoices", "tickets", "ledger", "payments"):
            self.assertEqual(self.owner.post("/api/import/%s/preview" % kind, {"rows": rows({"name": "x"})}).status, 404)

    # ---- customers
    def test_customer_import_creates_validates_and_trims(self):
        data = rows({"name": "  Acme  Dental ", "email": "PAT@Acme.example", "phone": "555-0100", "type": "Company", "notes": "n" * 3000},
                    {"name": ""}, {"name": "Bad Email", "email": "nope"}, {"name": "Bad Type", "type": "alien"},
                    {"name": "Ctl\x00Chars\x07"})
        results, imp = self.run_import(self.owner, "customers", data)
        self.assertEqual([r["status"] for r in results], ["created", "error", "error", "error", "created"])
        self.assertEqual([r["line"] for r in results], [2, 3, 4, 5, 6])
        self.assertIn("Name is required", results[1]["message"])
        self.assertEqual((imp["created"], imp["failed"], imp["status"], imp["total_rows"]), (2, 3, "done", 5))
        found = self.owner.get("/api/customers?q=Acme").json["customers"][0]
        self.assertEqual((found["name"], found["email"], found["type"]), ("Acme  Dental", "pat@acme.example", "business"))
        self.assertEqual(len(found["notes"]), 2000)                                       # capped, not rejected
        self.assertEqual(self.owner.get("/api/customers?q=CtlChars").json["total"], 1)    # control characters stripped

    def test_customer_values_are_plain_text_and_bound_as_parameters(self):
        evil = "x'); DROP TABLE customers;--"
        results, _ = self.run_import(self.owner, "customers", rows({"name": evil, "notes": "<script>alert(1)</script>"}))
        self.assertEqual(results[0]["status"], "created")
        c = self.owner.get("/api/customers?q=DROP").json["customers"][0]
        self.assertEqual((c["name"], c["notes"]), (evil, "<script>alert(1)</script>"))
        self.assertEqual(self.owner.get("/api/customers").status, 200)

    def test_customer_duplicates_skip_by_default_and_update_on_request(self):
        self.run_import(self.owner, "customers", rows({"name": "Dup Person", "email": "dup@example.com", "phone": "(555) 010-2000", "address": "1 Old Rd"}))
        # preview says what would happen and writes nothing
        probe = [{"name": "Different Name", "email": "DUP@example.com"}, {"name": "Other", "phone": "555.010.2000"},
                 {"name": "dup person"}, {"name": "Brand New Person"}]
        before = self.owner.get("/api/customers").json["total"]
        pv = self.owner.post("/api/import/customers/preview", {"mode": "skip", "rows": rows(*probe)}).json
        self.assertEqual([r["status"] for r in pv["results"]], ["would skip", "would skip", "would skip", "would create"])
        self.assertIn("email", pv["results"][0]["message"])
        self.assertIn("phone", pv["results"][1]["message"])
        self.assertIn("name", pv["results"][2]["message"])
        self.assertEqual(self.owner.get("/api/customers").json["total"], before)
        # skip mode: nothing changes
        results, imp = self.run_import(self.owner, "customers", rows({"name": "Dup Person", "email": "dup@example.com", "address": "2 New Rd"}))
        self.assertEqual((results[0]["status"], imp["skipped"]), ("skipped", 1))
        self.assertEqual(self.owner.get("/api/customers?q=Dup%20Person").json["customers"][0]["address"], "1 Old Rd")
        # update mode: new values win, empty cells never erase existing data
        results, imp = self.run_import(self.owner, "customers", rows({"name": "Dup Person", "email": "dup@example.com", "address": "2 New Rd", "phone": ""}), mode="update")
        self.assertEqual((results[0]["status"], imp["updated"]), ("updated", 1))
        got = self.owner.get("/api/customers?q=Dup%20Person").json
        self.assertEqual(got["total"], 1)
        self.assertEqual((got["customers"][0]["address"], got["customers"][0]["phone"]), ("2 New Rd", "(555) 010-2000"))

    def test_duplicates_inside_one_file_are_caught(self):
        results, imp = self.run_import(self.owner, "customers", rows({"name": "Twin One", "email": "twin@example.com"}, {"name": "Twin Two", "email": "twin@example.com"}))
        self.assertEqual([r["status"] for r in results], ["created", "skipped"])
        self.assertEqual((imp["created"], imp["skipped"]), (1, 1))

    # ---- inventory and services
    def test_inventory_import_prices_duplicates_and_no_stock_or_ledger(self):
        ledger_before = self.owner.get("/api/ledger/summary").json
        data = rows({"name": "Battery 6-cell", "sku": "BAT-6", "cost": "$24.50", "price": "1,059.99", "reorder_level": "3", "taxable": "no", "quantity": "50"},
                    {"name": "No Price Part"}, {"name": "Bad Money", "price": "12.345"}, {"name": "Negative", "price": "-5"},
                    {"name": "Bad Flag", "price": "1", "taxable": "maybe"}, {"name": "Cable", "price": "9.99"})
        results, imp = self.run_import(self.owner, "inventory", data)
        self.assertEqual([r["status"] for r in results], ["created", "error", "error", "error", "error", "created"])
        item = next(i for i in self.owner.get("/api/inventory?q=Battery").json["items"] if i["name"] == "Battery 6-cell")
        self.assertEqual((item["sku"], item["cost_cents"], item["price_cents"], item["reorder_level"], item["taxable"]), ("BAT-6", 2450, 105999, 3, 0))
        self.assertEqual(item["quantity"], 0)                                            # quantities are never imported
        self.assertTrue(item["part_no"])
        self.assertEqual(self.owner.get("/api/ledger/summary").json, ledger_before)      # no cash movement
        # duplicates by supplier part number and by name
        results, _ = self.run_import(self.owner, "inventory", rows({"name": "Renamed", "sku": "bat-6", "price": "1"}, {"name": "CABLE", "price": "1"}))
        self.assertEqual([r["status"] for r in results], ["skipped", "skipped"])
        results, _ = self.run_import(self.owner, "inventory", rows({"name": "Battery 6-cell", "sku": "BAT-6", "price": "30.00"}), mode="update")
        self.assertEqual(results[0]["status"], "updated")
        item = self.owner.get("/api/inventory/%s" % item["id"]).json
        self.assertEqual((item["price_cents"], item["cost_cents"], item["quantity"]), (3000, 2450, 0))

    def test_service_import(self):
        results, _ = self.run_import(self.owner, "services", rows({"name": "Diagnostic fee", "price": "49", "taxable": "yes"}, {"name": "Free thing"}))
        self.assertEqual([r["status"] for r in results], ["created", "error"])
        results, _ = self.run_import(self.owner, "services", rows({"name": "diagnostic FEE", "price": "10"}))
        self.assertEqual(results[0]["status"], "skipped")
        results, _ = self.run_import(self.owner, "services", rows({"name": "diagnostic FEE", "price": "55"}), mode="update")
        self.assertEqual(results[0]["status"], "updated")
        svc = [s for s in self.owner.get("/api/services").json["services"] if s["name"].lower() == "diagnostic fee"]
        self.assertEqual((len(svc), svc[0]["price_cents"]), (1, 5500))

    # ---- protocol rules, history, audit
    def test_request_validation(self):
        self.assertEqual(self.owner.post("/api/import/customers/preview", {"rows": []}).status, 400)
        self.assertEqual(self.owner.post("/api/import/customers/preview", {"rows": rows(*[{"name": "n%d" % i} for i in range(51)])}).status, 400)
        self.assertEqual(self.owner.post("/api/import/customers/preview", {"mode": "delete", "rows": rows({"name": "x"})}).status, 400)
        self.assertEqual(self.owner.post("/api/import/customers/preview", {"rows": [{"line": 2, "values": {"name": 5}}]}).status, 400)
        self.assertEqual(self.owner.post("/api/import/customers/start", {"total": 0}).status, 400)
        self.assertEqual(self.owner.post("/api/import/customers/batch", {"import_id": "nope", "rows": rows({"name": "x"})}).status, 404)
        iid = self.owner.post("/api/import/customers/start", {"total": 1}).json["import_id"]
        self.assertEqual(self.owner.post("/api/import/services/batch", {"import_id": iid, "rows": rows({"name": "x", "price": "1"})}).status, 404)  # wrong type
        self.assertEqual(self.owner.post("/api/import/customers/batch", {"import_id": iid, "rows": rows(*[{"name": "n%d" % i} for i in range(201)])}).status, 400)
        self.owner.post("/api/import/customers/finish", {"import_id": iid})
        self.assertEqual(self.owner.post("/api/import/customers/batch", {"import_id": iid, "rows": rows({"name": "late"})}).status, 409)

    def test_history_and_audit(self):
        self.run_import(self.mgr, "services", rows({"name": "Audited service", "price": "5"}), filename="svc.csv")
        hist = self.owner.get("/api/import/history").json["imports"]
        mine = next(h for h in hist if h["filename"] == "svc.csv")
        self.assertEqual((mine["kind"], mine["created"], mine["status"], mine["user_name"]), ("services", 1, "done", "Manager"))
        events = self.owner.get("/api/audit?limit=200").json["events"]
        self.assertIn("import.started", [e["action"] for e in events])
        done = next(e for e in events if e["action"] == "import.completed" and e["entity_id"] == mine["id"])
        import json
        detail = done["detail"] if isinstance(done["detail"], dict) else json.loads(done["detail"])
        self.assertEqual(detail["created"], 1)

    # ---- tenant isolation
    def test_import_only_writes_to_the_importing_shop(self):
        other = Client(self.ts)
        self.assertEqual(other.register("Other Import Shop", "other-import@example.com").status, 200)
        before = other.get("/api/customers").json["total"]
        results, _ = self.run_import(self.owner, "customers", rows({"name": "Only In First Shop", "email": "first-only@example.com"}))
        self.assertEqual(results[0]["status"], "created")
        self.assertEqual(other.get("/api/customers").json["total"], before)
        self.assertEqual(other.get("/api/customers?q=Only%20In%20First").json["total"], 0)
        # a duplicate check never sees the other shop's data, and the other shop cannot use this shop's import id
        iid = self.owner.post("/api/import/customers/start", {"total": 1}).json["import_id"]
        self.assertEqual(other.post("/api/import/customers/batch", {"import_id": iid, "rows": rows({"name": "x"})}).status, 404)
        self.assertEqual(other.get("/api/import/history").json["imports"], [])
        res, _ = self.run_import(other, "customers", rows({"name": "Only In First Shop", "email": "first-only@example.com"}))
        self.assertEqual(res[0]["status"], "created")                                      # not a duplicate over there


class CsvBrowserCodeGuards(unittest.TestCase):
    """The CSV parser runs in the browser; guard that the shipped script keeps its safety properties."""

    def test_import_script_never_uses_inner_html_or_eval(self):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent.parent / "static" / "import.js").read_text(encoding="utf-8")
        for bad in ("innerHTML", "eval(", "document.write", "new Function"):
            self.assertNotIn(bad, src)
        self.assertTrue(re.search(r"0xFEFF", src))                                         # byte-order mark tolerated
        self.assertIn("windows-1252", src)                                                 # fallback decoding


if __name__ == "__main__":
    unittest.main()
