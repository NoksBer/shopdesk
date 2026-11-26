"""Manual calendar items: create, list by range, edit, delete, validation, audit and tenant isolation."""
import unittest

from harness import Client, TestServer


class CalendarItems(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = TestServer()
        cls.owner = Client(cls.ts)
        assert cls.owner.register("Cal Shop", "cal-owner@example.com").status == 200
        cls.ts.add_user(cls.owner.tenant_id, "cal-tech@example.com", "technician")
        cls.tech = Client(cls.ts)
        assert cls.tech.login("cal-tech@example.com").status == 200

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def add(self, client=None, **kw):
        body = {"title": "Pick up parts", "item_date": "2031-05-10", "item_time": "14:30", "notes": "Dock 2"}
        body.update(kw)
        return (client or self.owner).post("/api/calendar-items", body)

    def test_create_list_edit_delete(self):
        r = self.add(title="Supplier visit", item_date="2031-06-02", item_time="", notes="")
        self.assertEqual(r.status, 201, r.raw)
        item = r.json["item"]
        self.assertEqual((item["title"], item["item_date"], item["item_time"], item["created_by_name"]),
                         ("Supplier visit", "2031-06-02", "", "Owner"))
        listed = self.owner.get("/api/calendar-items?from=2031-06-01&to=2031-06-30").json["items"]
        self.assertIn(item["id"], [i["id"] for i in listed])
        self.assertNotIn(item["id"], [i["id"] for i in self.owner.get("/api/calendar-items?from=2031-07-01&to=2031-07-31").json["items"]])
        moved = self.owner.put("/api/calendar-items/%s" % item["id"], {"title": "Supplier visit (moved)", "item_date": "2031-06-09",
                                                                          "item_time": "09:00", "notes": "Bring samples"})
        self.assertEqual(moved.status, 200, moved.raw)
        self.assertEqual((moved.json["item"]["item_date"], moved.json["item"]["item_time"]), ("2031-06-09", "09:00"))
        self.assertEqual(self.owner.request("DELETE", "/api/calendar-items/%s" % item["id"]).status, 200)
        self.assertEqual(self.owner.request("DELETE", "/api/calendar-items/%s" % item["id"]).status, 404)
        actions = [e["action"] for e in self.owner.get("/api/audit?limit=200").json["events"]]
        for a in ("calendaritem.created", "calendaritem.updated", "calendaritem.deleted"):
            self.assertIn(a, actions)

    def test_items_are_ordered_by_date_then_time(self):
        a = self.add(title="b", item_date="2032-01-05", item_time="15:00").json["item"]
        b = self.add(title="a", item_date="2032-01-05", item_time="08:00").json["item"]
        c = self.add(title="c", item_date="2032-01-04", item_time="").json["item"]
        got = [i["id"] for i in self.owner.get("/api/calendar-items?from=2032-01-01&to=2032-01-31").json["items"]]
        self.assertEqual(got, [c["id"], b["id"], a["id"]])

    def test_validation(self):
        for body in ({"title": ""}, {"title": "x" * 121}, {"item_date": ""}, {"item_date": "05/10/2031"}, {"item_date": "2031-02-30"},
                     {"item_date": "1990-01-01"}, {"item_time": "25:00"}, {"item_time": "9:5"}, {"notes": "x" * 1001}):
            self.assertEqual(self.add(**body).status, 400, body)
        self.assertEqual(self.owner.get("/api/calendar-items").status, 400)
        self.assertEqual(self.owner.get("/api/calendar-items?from=2031-01-01&to=2031-12-31").status, 400)      # range too long
        self.assertEqual(self.owner.put("/api/calendar-items/unknown", {"title": "x", "item_date": "2031-01-01"}).status, 404)

    def test_technician_can_see_and_add_items(self):
        item = self.add(self.tech, title="Tech reminder", item_date="2033-03-03").json["item"]
        seen = self.owner.get("/api/calendar-items?from=2033-03-01&to=2033-03-31").json["items"]
        self.assertIn(item["id"], [i["id"] for i in seen])
        self.assertEqual(self.tech.get("/api/calendar-items?from=2033-03-01&to=2033-03-31").status, 200)

    def test_sign_in_is_required(self):
        anon = Client(self.ts)
        self.assertEqual(anon.get("/api/calendar-items?from=2031-01-01&to=2031-01-31").status, 401)
        self.assertEqual(anon.post("/api/calendar-items", {"title": "x", "item_date": "2031-01-01"}).status, 401)

    def test_other_shop_cannot_see_or_change_items(self):
        item = self.add(title="Private", item_date="2034-04-04").json["item"]
        other = Client(self.ts)
        self.assertEqual(other.register("Other Cal Shop", "other-cal@example.com").status, 200)
        self.assertEqual(other.get("/api/calendar-items?from=2034-04-01&to=2034-04-30").json["items"], [])
        body = {"title": "Hijack", "item_date": "2034-04-04"}
        self.assertEqual(other.put("/api/calendar-items/%s" % item["id"], body).status, 404)
        self.assertEqual(other.request("DELETE", "/api/calendar-items/%s" % item["id"]).status, 404)

    def test_table_exists_and_is_additive(self):
        db = self.ts.tenant_db(self.owner.tenant_id)
        try:
            cols = {r["name"] for r in db.execute("PRAGMA table_info(calendar_items)")}
            versions = [r[0] for r in db.execute("SELECT version FROM schema_version ORDER BY version")]
        finally:
            db.close()
        self.assertEqual(cols, {"id", "title", "item_date", "item_time", "notes", "created_by", "created_at", "updated_at"})
        self.assertEqual(versions, sorted(versions))
        self.assertIn(11, versions)


if __name__ == "__main__":
    unittest.main()
