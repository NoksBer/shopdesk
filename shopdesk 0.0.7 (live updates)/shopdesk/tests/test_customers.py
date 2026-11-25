import json
import unittest

from harness import Client, TestServer


def names(resp, key):
    return sorted(x["name"] for x in resp.json[key])


class CustomersAndEquipment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = TestServer()
        cls.owner = Client(cls.ts)
        assert cls.owner.register("Bench Repair", "bench-owner@example.com", shop_type="computer").status == 200
        tid = cls.owner.tenant_id
        for email, role in (("tech@example.com", "technician"), ("desk@example.com", "front_desk"),
                            ("mgr@example.com", "manager")):
            cls.ts.add_user(tid, email, role)
        cls.tech, cls.desk, cls.mgr = Client(cls.ts), Client(cls.ts), Client(cls.ts)
        assert cls.tech.login("tech@example.com").status == 200
        assert cls.desk.login("desk@example.com").status == 200
        assert cls.mgr.login("mgr@example.com").status == 200

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def customer(self, name, **extra):
        body = {"name": name}
        body.update(extra)
        r = self.owner.post("/api/customers", body)
        self.assertEqual(r.status, 201, r.raw)
        return r.json

    # ---------------------------------------------------------------- customers
    def test_customer_crud(self):
        c = self.customer("Jane Doe", email="Jane@Example.com", phone="555-0101", contact_pref="phone",
                          notes="Prefers morning calls")
        self.assertEqual(c["email"], "jane@example.com")
        self.assertEqual(c["type"], "individual")
        self.assertEqual(c["equipment_count"], 0)
        got = self.owner.get("/api/customers/%s" % c["id"]).json
        self.assertEqual(got["name"], "Jane Doe")
        upd = self.owner.put("/api/customers/%s" % c["id"], {"name": "Jane Q Doe", "phone": "555-0102"})
        self.assertEqual(upd.status, 200)
        self.assertEqual(upd.json["name"], "Jane Q Doe")
        self.assertEqual(upd.json["email"], "")                 # PUT replaces; omitted fields are cleared

    def test_customer_validation(self):
        self.assertEqual(self.owner.post("/api/customers", {}).status, 400)
        self.assertEqual(self.owner.post("/api/customers", {"name": "X", "email": "nope"}).status, 400)
        self.assertEqual(self.owner.post("/api/customers", {"name": "X", "type": "alien"}).status, 400)
        self.assertEqual(self.owner.post("/api/customers", {"name": "X", "contact_pref": "pigeon"}).status, 400)
        self.assertEqual(self.owner.post("/api/customers", {"name": "x" * 121}).status, 400)
        self.assertEqual(self.owner.get("/api/customers/doesnotexist").status, 404)

    def test_search_and_wildcards(self):
        self.customer("Search A_B Corp", type="business", email="sales@abcorp.example")
        self.customer("Search AXB Corp", phone="555-7777")
        r = self.owner.get("/api/customers?q=A_B")
        self.assertEqual(names(r, "customers"), ["Search A_B Corp"])           # '_' is not a wildcard
        self.assertEqual(self.owner.get("/api/customers?q=%25").json["total"], 0)  # '%' is not a wildcard
        self.assertEqual(names(self.owner.get("/api/customers?q=555-7777"), "customers"), ["Search AXB Corp"])
        self.assertEqual(names(self.owner.get("/api/customers?q=abcorp.example"), "customers"), ["Search A_B Corp"])
        self.assertEqual(names(self.owner.get("/api/customers?q=SEARCH%20axb"), "customers"), ["Search AXB Corp"])

    def test_sql_injection_attempt_is_just_text(self):
        self.customer("Bobby Tables")
        r = self.owner.get("/api/customers?q=%27%3B%20DROP%20TABLE%20customers%3B--")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json["total"], 0)
        self.assertEqual(names(self.owner.get("/api/customers?q=Bobby"), "customers"), ["Bobby Tables"])

    def test_archive_and_restore(self):
        c = self.customer("Archive Me")
        self.assertEqual(self.owner.request("DELETE", "/api/customers/%s" % c["id"]).status, 200)
        self.assertNotIn("Archive Me", names(self.owner.get("/api/customers?q=Archive"), "customers"))
        self.assertIn("Archive Me", names(self.owner.get("/api/customers?q=Archive&include_archived=1"), "customers"))
        r = self.owner.put("/api/customers/%s" % c["id"], {"name": "Archive Me", "archived": False})
        self.assertEqual(r.status, 200)
        self.assertIsNone(r.json["archived_at"])
        self.assertIn("Archive Me", names(self.owner.get("/api/customers?q=Archive"), "customers"))

    def test_customer_permissions(self):
        self.assertEqual(self.tech.get("/api/customers").status, 200)
        self.assertEqual(self.tech.post("/api/customers", {"name": "Nope"}).status, 403)
        self.assertEqual(self.desk.post("/api/customers", {"name": "Desk Made"}).status, 201)
        self.assertEqual(self.mgr.post("/api/customers", {"name": "Mgr Made"}).status, 201)

    def test_audit_has_no_customer_pii(self):
        c = self.customer("Zelda Unique-Name", email="zelda@example.com")
        self.owner.put("/api/customers/%s" % c["id"], {"name": "Zelda Unique-Name", "email": "z2@example.com"})
        events = self.owner.get("/api/audit?limit=500").json["events"]
        mine = [e for e in events if e["entity_id"] == c["id"]]
        self.assertEqual(sorted(e["action"] for e in mine), ["customer.created", "customer.updated"])
        blob = json.dumps(mine)
        self.assertNotIn("Zelda", blob)
        self.assertNotIn("example.com", blob)
        upd = [e for e in mine if e["action"] == "customer.updated"][0]
        self.assertEqual(json.loads(upd["detail"]), {"changed": ["email"]})

    # ---------------------------------------------------------------- equipment
    def test_profile_fields_come_from_shop_type(self):
        r = self.owner.get("/api/equipment/fields")
        keys = [f["key"] for f in r.json["fields"]]
        self.assertIn("operating_system", keys)
        self.assertEqual(r.json["terminology"]["item"], "Device")

    def test_equipment_crud_and_profile_validation(self):
        c = self.customer("Equip Owner")
        r = self.owner.post("/api/equipment", {
            "customer_id": c["id"], "name": "Dell Latitude 5420", "serial_number": "SN-12345",
            "description": "Scratched lid", "purchase_date": "2024-03-01", "warranty_expires": "2027-03-01",
            "fields": {"manufacturer": "Dell", "model": "Latitude 5420", "operating_system": "Windows 11"}})
        self.assertEqual(r.status, 201, r.raw)
        e = r.json
        self.assertEqual(e["customer_name"], "Equip Owner")
        self.assertEqual(e["fields"]["operating_system"], "Windows 11")
        self.assertEqual(self.owner.get("/api/customers/%s" % c["id"]).json["equipment_count"], 1)

        self.assertEqual(self.owner.post("/api/equipment", {
            "customer_id": c["id"], "name": "Bad", "fields": {"wheel_size": "29"}}).status, 400)   # not in profile
        self.assertEqual(self.owner.post("/api/equipment", {
            "customer_id": c["id"], "name": "Bad", "purchase_date": "2024-13-45"}).status, 400)
        self.assertEqual(self.owner.post("/api/equipment", {"customer_id": c["id"]}).status, 400)
        self.assertEqual(self.owner.post("/api/equipment", {"name": "No owner"}).status, 400)
        self.assertEqual(self.owner.post("/api/equipment", {"customer_id": "nonexistent", "name": "X"}).status, 404)

        upd = self.owner.put("/api/equipment/%s" % e["id"], {"name": "Dell Latitude 5420", "serial_number": "SN-99999",
                                                              "fields": {"model": "5420"}})
        self.assertEqual(upd.status, 200)
        self.assertEqual(upd.json["serial_number"], "SN-99999")
        self.assertNotIn("operating_system", upd.json["fields"])

    def test_equipment_search_filter_and_reassign(self):
        c1, c2 = self.customer("Owner One"), self.customer("Owner Two")
        a = self.owner.post("/api/equipment", {"customer_id": c1["id"], "name": "Laptop Alpha",
                                               "serial_number": "ZZ-ALPHA-1", "fields": {"manufacturer": "Acme"}}).json
        self.owner.post("/api/equipment", {"customer_id": c2["id"], "name": "Laptop Beta", "serial_number": "ZZ-BETA-2"})
        self.assertEqual(names(self.owner.get("/api/equipment?q=ZZ-ALPHA"), "equipment"), ["Laptop Alpha"])
        self.assertEqual(names(self.owner.get("/api/equipment?q=Acme"), "equipment"), ["Laptop Alpha"])   # in fields
        self.assertEqual(names(self.owner.get("/api/equipment?q=Owner%20Two"), "equipment"), ["Laptop Beta"])
        self.assertEqual(names(self.owner.get("/api/equipment?customer_id=%s" % c1["id"]), "equipment"),
                         ["Laptop Alpha"])
        r = self.owner.put("/api/equipment/%s" % a["id"], {"name": "Laptop Alpha", "customer_id": c2["id"]})
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json["customer_id"], c2["id"])
        self.assertEqual(self.owner.get("/api/customers/%s" % c2["id"]).json["equipment_count"], 2)

    def test_equipment_archive_and_archived_customer(self):
        c = self.customer("Soon Archived")
        e = self.owner.post("/api/equipment", {"customer_id": c["id"], "name": "Thing"}).json
        self.assertEqual(self.owner.request("DELETE", "/api/equipment/%s" % e["id"]).status, 200)
        self.assertEqual(self.owner.get("/api/equipment?customer_id=%s" % c["id"]).json["equipment"], [])
        self.owner.request("DELETE", "/api/customers/%s" % c["id"])
        self.assertEqual(self.owner.post("/api/equipment", {"customer_id": c["id"], "name": "Late"}).status, 400)

    def test_configurable_fields(self):
        r = self.owner.put("/api/equipment/fields", {"fields": [
            {"key": "engine_hours", "label": "Engine hours", "type": "number"},
            {"key": "bought", "label": "Bought on", "type": "date"},
            {"key": "colour", "label": "Colour"}]})
        self.assertEqual(r.status, 200, r.raw)
        c = self.customer("Custom Fields Co")
        ok = self.owner.post("/api/equipment", {"customer_id": c["id"], "name": "Mower",
                                                "fields": {"engine_hours": 12.5, "colour": "red", "bought": "2023-05-05"}})
        self.assertEqual(ok.status, 201, ok.raw)
        self.assertEqual(ok.json["fields"]["engine_hours"], "12.5")
        self.assertEqual(self.owner.post("/api/equipment", {"customer_id": c["id"], "name": "M2",
                                                            "fields": {"engine_hours": "lots"}}).status, 400)
        self.assertEqual(self.owner.post("/api/equipment", {"customer_id": c["id"], "name": "M3",
                                                            "fields": {"bought": "yesterday"}}).status, 400)
        for bad in ([{"key": "Bad Key", "label": "x"}], [{"key": "a", "label": "x"}, {"key": "a", "label": "y"}],
                    [{"key": "a", "label": "x", "type": "blob"}], "nope", [{"label": "no key"}]):
            self.assertEqual(self.owner.put("/api/equipment/fields", {"fields": bad}).status, 400, bad)
        # restore the computer profile fields so other tests keep their assumptions
        from shopdesk.profiles import PROFILES
        self.assertEqual(self.owner.put("/api/equipment/fields",
                                        {"fields": PROFILES["computer"]["equipment_fields"]}).status, 200)

    def test_equipment_permissions(self):
        c = self.customer("Perm Customer")
        self.assertEqual(self.tech.post("/api/equipment", {"customer_id": c["id"], "name": "Tech Added"}).status, 201)
        self.assertEqual(self.tech.put("/api/equipment/fields", {"fields": []}).status, 403)
        self.assertEqual(self.mgr.put("/api/equipment/fields", {"fields": []}).status, 403)


class CustomerIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = TestServer()
        cls.a, cls.b = Client(cls.ts), Client(cls.ts)
        assert cls.a.register("Alpha Shop", "a-iso@example.com").status == 200
        assert cls.b.register("Beta Shop", "b-iso@example.com").status == 200
        cls.a_cust = cls.a.post("/api/customers", {"name": "Alpha Secret Customer", "email": "s@example.com"}).json
        cls.a_equip = cls.a.post("/api/equipment", {"customer_id": cls.a_cust["id"], "name": "Alpha Secret Laptop",
                                                    "serial_number": "ALPHA-SERIAL"}).json

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def test_other_tenant_customer_is_404_everywhere(self):
        cid = self.a_cust["id"]
        self.assertEqual(self.b.get("/api/customers/%s" % cid).status, 404)
        self.assertEqual(self.b.put("/api/customers/%s" % cid, {"name": "Hijacked"}).status, 404)
        self.assertEqual(self.b.request("DELETE", "/api/customers/%s" % cid).status, 404)
        self.assertEqual(self.a.get("/api/customers/%s" % cid).json["name"], "Alpha Secret Customer")
        self.assertIsNone(self.a.get("/api/customers/%s" % cid).json["archived_at"])

    def test_other_tenant_equipment_is_404_everywhere(self):
        eid = self.a_equip["id"]
        self.assertEqual(self.b.get("/api/equipment/%s" % eid).status, 404)
        self.assertEqual(self.b.put("/api/equipment/%s" % eid, {"name": "Hijacked"}).status, 404)
        self.assertEqual(self.b.request("DELETE", "/api/equipment/%s" % eid).status, 404)
        self.assertEqual(self.a.get("/api/equipment/%s" % eid).json["name"], "Alpha Secret Laptop")

    def test_cannot_attach_equipment_to_other_tenants_customer(self):
        r = self.b.post("/api/equipment", {"customer_id": self.a_cust["id"], "name": "Planted"})
        self.assertEqual(r.status, 404)
        self.assertEqual(self.a.get("/api/equipment?customer_id=%s" % self.a_cust["id"]).json["equipment"][0]["name"],
                         "Alpha Secret Laptop")

    def test_lists_and_searches_do_not_leak(self):
        self.assertEqual(self.b.get("/api/customers").json["total"], 0)
        self.assertEqual(self.b.get("/api/customers?q=Alpha").json["customers"], [])
        self.assertEqual(self.b.get("/api/equipment?q=ALPHA-SERIAL").json["equipment"], [])
        self.assertEqual(self.b.get("/api/equipment?customer_id=%s" % self.a_cust["id"]).json["equipment"], [])
        self.assertEqual(self.b.get("/api/customers?include_archived=1").json["total"], 0)

    def test_customer_files_live_in_separate_databases(self):
        db_b = self.ts.tenant_db(self.b.tenant_id)
        count = db_b.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        db_b.close()
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
