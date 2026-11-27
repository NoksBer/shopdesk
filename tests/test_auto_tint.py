"""Shop type "Auto tinting / window film": starter fields, statuses, services and touch base presets, seeded once at creation."""
import json
import unittest

from harness import Client, TestServer
from shopdesk.app import App
from shopdesk.profiles import DEFAULT_STATUSES, PROFILES

TINT = "auto_tint"
SERVICES = ["Full vehicle tint", "Front two windows", "Rear windows", "Windshield strip", "Full windshield", "Sunroof", "Old tint removal"]
STATUSES = [("Quote Requested", 0), ("Scheduled", 0), ("Checked In", 0), ("In Progress", 0), ("Quality Check", 0),
            ("Ready for Pickup", 0), ("Completed / Closed", 1), ("Cancelled / No-show", 1)]
FIELDS = ["year", "make", "model", "color", "license_plate", "vin", "film_brand", "film_type", "shade_percent", "warranty_term"]
OTHER_TYPES = [k for k in PROFILES if k != TINT]


def snapshot(ts, tenant_id):
    """Everything the seeding could touch, in a comparable form."""
    db = ts.tenant_db(tenant_id)
    try:
        return {
            "services": [tuple(r) for r in db.execute("SELECT name, price_cents FROM services ORDER BY name")],
            "statuses": [tuple(r) for r in db.execute("SELECT name, is_closed, sort_order FROM ticket_statuses ORDER BY sort_order")],
            "settings": [tuple(r) for r in db.execute("SELECT key, value FROM settings ORDER BY key")],
            "counts": [db.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in ("customers", "equipment", "tickets", "invoices", "inventory_items", "users")],
        }
    finally:
        db.close()


class AutoTint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = TestServer()
        cls.tint = Client(cls.ts)
        assert cls.tint.register("Tint Shop", "tint-owner@example.com", shop_type=TINT).status == 200
        cls.others = {}
        for i, key in enumerate(OTHER_TYPES):
            c = Client(cls.ts)
            assert c.register("Other %s" % key, "other-%d@example.com" % i, shop_type=key).status == 200, key
            cls.others[key] = c

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    # ---- the type is data in the profile system
    def test_type_is_offered_on_the_create_shop_form(self):
        types = Client(self.ts).get("/api/bootstrap").json["shop_types"]
        by_key = {t["key"]: t["label"] for t in types}
        self.assertEqual(by_key[TINT], "Auto tinting / window film")
        self.assertEqual(types[-1]["key"], "custom")                      # "Other / custom" stays last
        self.assertEqual([t["key"] for t in types], list(PROFILES))      # the list is just the profile table

    # ---- what a new tint shop starts with
    def test_vehicle_fields_and_label(self):
        r = self.tint.get("/api/equipment/fields").json
        self.assertEqual([f["key"] for f in r["fields"]], FIELDS)
        labels = {f["key"]: f["label"] for f in r["fields"]}
        self.assertEqual((labels["year"], labels["license_plate"], labels["vin"]), ("Year", "License plate", "VIN (optional)"))
        self.assertEqual((r["terminology"]["item"], r["terminology"]["items"]), ("Vehicle", "Vehicles"))
        self.assertEqual(self.tint.get("/api/me").json["terminology"]["item"], "Vehicle")

    def test_vehicle_record_can_be_saved_with_and_without_vin(self):
        cust = self.tint.post("/api/customers", {"name": "Tint Customer"}).json["id"]
        r = self.tint.post("/api/equipment", {"customer_id": cust, "name": "2019 Honda Civic", "fields": {
            "year": "2019", "make": "Honda", "model": "Civic", "color": "Blue", "license_plate": "ABC1234",
            "film_brand": "Acme", "film_type": "Ceramic", "shade_percent": "35", "warranty_term": "Lifetime"}})
        self.assertEqual(r.status, 201, r.raw)
        self.assertEqual(r.json["fields"]["make"], "Honda")
        self.assertNotIn("vin", r.json["fields"])                         # VIN is optional
        r = self.tint.post("/api/equipment", {"customer_id": cust, "name": "Truck", "fields": {"vin": "1HGCM82633A004352"}})
        self.assertEqual(r.status, 201)
        self.assertEqual(self.tint.post("/api/equipment", {"customer_id": cust, "name": "Bad", "fields": {"year": "twenty"}}).status, 400)

    def test_starter_statuses_and_touch_base_rules(self):
        sts = self.tint.get("/api/statuses").json["statuses"]
        self.assertEqual([(s["name"], s["is_closed"]) for s in sts], STATUSES)
        by_name = {s["name"]: s["id"] for s in sts}
        cust = self.tint.post("/api/customers", {"name": "Status Customer"}).json["id"]
        done = self.tint.post("/api/tickets", {"customer_id": cust, "title": "Tint job"}).json
        self.assertEqual(done["status_name"], "Quote Requested")          # new tickets start at the first status
        r = self.tint.post("/api/tickets/%s/status" % done["id"], {"status_id": by_name["Completed / Closed"]}).json
        self.assertTrue(r["touch_base_offer"])                            # Completed / Closed still offers a touch base
        other = self.tint.post("/api/tickets", {"customer_id": cust, "title": "No show"}).json
        r = self.tint.post("/api/tickets/%s/status" % other["id"], {"status_id": by_name["Cancelled / No-show"]}).json
        self.assertFalse(r["touch_base_offer"])                           # Cancelled / No-show does not

    def test_starter_services_have_no_prices(self):
        svcs = self.tint.get("/api/services?include_inactive=1").json["services"]
        self.assertEqual(sorted(s["name"] for s in svcs), sorted(SERVICES))
        self.assertEqual({s["price_cents"] for s in svcs}, {0})
        self.assertTrue(all(s["active"] for s in svcs))

    def test_touch_base_presets_reach_the_popup(self):
        presets = self.tint.get("/api/me").json["touch_base_presets"]
        self.assertEqual([p["label"] for p in presets], ["1 week check-in", "6 month check-in", "1 year check-in"])
        self.assertEqual((presets[0].get("days"), presets[1].get("months"), presets[2].get("months")), (7, 6, 12))
        self.assertTrue(all(p["note"] for p in presets))
        # the popup code reads them from /api/me and adds nothing for shops without presets
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent.parent / "static" / "touchbase.js").read_text(encoding="utf-8")
        self.assertIn("touch_base_presets", src)

    # ---- the owner can change everything that was seeded
    def test_owner_can_edit_and_delete_everything_seeded(self):
        shop = Client(self.ts)
        self.assertEqual(shop.register("Editable Tint Shop", "editable-tint@example.com", shop_type=TINT).status, 200)
        svcs = shop.get("/api/services").json["services"]
        s = next(x for x in svcs if x["name"] == "Sunroof")
        r = shop.put("/api/services/%s" % s["id"], {"name": "Sunroof tint", "price_cents": 7500})
        self.assertEqual((r.status, r.json["price_cents"]), (200, 7500))
        self.assertEqual(shop.request("DELETE", "/api/services/%s" % svcs[0]["id"]).status, 200)
        sts = shop.get("/api/statuses").json["statuses"]
        edited = [{"id": x["id"], "name": x["name"] + " *", "customer_label": x["customer_label"], "is_closed": bool(x["is_closed"])} for x in sts]
        self.assertEqual(shop.put("/api/statuses", {"statuses": edited[:-1] + [{"name": "Brand new", "is_closed": True}]}).status, 200)
        self.assertEqual(shop.put("/api/equipment/fields", {"fields": [{"key": "plate", "label": "Plate", "type": "text"}]}).status, 200)
        self.assertEqual([f["key"] for f in shop.get("/api/equipment/fields").json["fields"]], ["plate"])

    # ---- other types are exactly as before
    def test_other_types_are_unchanged(self):
        for key, client in self.others.items():
            prof = PROFILES[key]
            self.assertEqual([f["key"] for f in client.get("/api/equipment/fields").json["fields"]], [f["key"] for f in prof["equipment_fields"]], key)
            self.assertEqual(client.get("/api/equipment/fields").json["terminology"], prof["terminology"], key)
            self.assertEqual([(s["name"], s["is_closed"]) for s in client.get("/api/statuses").json["statuses"]],
                             [(n, c) for n, _l, c in DEFAULT_STATUSES], key)
            self.assertEqual(client.get("/api/services?include_inactive=1").json["services"], [], key)
            self.assertEqual(client.get("/api/me").json["touch_base_presets"], [], key)
            db = self.ts.tenant_db(client.tenant_id)
            self.assertIsNone(db.execute("SELECT 1 FROM settings WHERE key='touch_base_presets'").fetchone(), key)
            db.close()
        for key in OTHER_TYPES:
            for forbidden in ("starter_services", "touch_base_presets"):
                self.assertNotIn(forbidden, PROFILES[key])

    # ---- seeding happens once, at creation
    def test_existing_shops_and_restarts_change_nothing(self):
        before = {k: snapshot(self.ts, c.tenant_id) for k, c in self.others.items()}
        before_tint = snapshot(self.ts, self.tint.tenant_id)
        App(self.ts.config)                                               # restart: migrations run again on every shop
        App(self.ts.config)
        for k, c in self.others.items():
            self.assertEqual(snapshot(self.ts, c.tenant_id), before[k], k)
        self.assertEqual(snapshot(self.ts, self.tint.tenant_id), before_tint)

    def test_seeding_never_repeats_even_after_the_owner_removes_things(self):
        shop = Client(self.ts)
        self.assertEqual(shop.register("Once Tint Shop", "once-tint@example.com", shop_type=TINT).status, 200)
        for s in shop.get("/api/services").json["services"]:
            shop.request("DELETE", "/api/services/%s" % s["id"])
        shop.put("/api/equipment/fields", {"fields": []})
        App(self.ts.config)
        self.assertEqual(shop.get("/api/services").json["services"], [])                       # nothing re-added
        self.assertEqual(len(shop.get("/api/services?include_inactive=1").json["services"]), 7)  # and no duplicates
        self.assertEqual(shop.get("/api/equipment/fields").json["fields"], [])
        db = self.ts.tenant_db(shop.tenant_id)
        n = db.execute("SELECT COUNT(*) FROM services").fetchone()[0]
        db.close()
        self.assertEqual(n, 7)

    def test_each_shop_gets_its_own_records(self):
        second = Client(self.ts)
        self.assertEqual(second.register("Second Tint Shop", "second-tint@example.com", shop_type=TINT).status, 200)
        ids = lambda c: {s["id"] for s in c.get("/api/services?include_inactive=1").json["services"]}   # noqa: E731
        a, b = ids(self.tint), ids(second)
        self.assertEqual((len(a), len(b)), (7, 7))
        self.assertFalse(a & b)
        # editing one shop's catalogue does not show up in the other
        s = second.get("/api/services").json["services"][0]
        second.put("/api/services/%s" % s["id"], {"name": s["name"], "price_cents": 12345})
        self.assertEqual({x["price_cents"] for x in self.tint.get("/api/services").json["services"]}, {0})
        # a non-tint shop never sees the tint shop's services or presets
        computer = self.others["computer"]
        self.assertNotIn("Full vehicle tint", [x["name"] for x in computer.get("/api/services").json["services"]])
        self.assertEqual(computer.get("/api/me").json["touch_base_presets"], [])
        self.assertEqual(computer.get("/api/services/%s" % s["id"]).status, 405)    # ids from another shop lead nowhere


class ProfileShape(unittest.TestCase):
    def test_optional_keys_are_well_formed(self):
        prof = PROFILES[TINT]
        self.assertEqual([n for n, _l, _c in prof["statuses"]], [n for n, _ in STATUSES])
        self.assertEqual(prof["starter_services"], SERVICES)
        for p in prof["touch_base_presets"]:
            self.assertTrue(p["label"] and p["note"] and (p.get("days") or p.get("months")))
        json.dumps(prof["touch_base_presets"])
        self.assertTrue(any("cancel" in n.lower() for n, _l, c in prof["statuses"] if c))
        self.assertTrue(any("cancel" not in n.lower() for n, _l, c in prof["statuses"] if c))


if __name__ == "__main__":
    unittest.main()
