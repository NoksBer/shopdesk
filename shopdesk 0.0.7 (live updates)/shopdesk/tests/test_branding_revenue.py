"""White-label branding and the dashboard revenue section."""
import base64
import unittest
from datetime import datetime, timedelta, timezone

from harness import Client, TestServer

# 1x1 transparent PNG
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
b64 = lambda raw: base64.b64encode(raw).decode()


class Branding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = TestServer()
        cls.owner = Client(cls.ts)
        assert cls.owner.register("Brand Shop", "brand@example.com").status == 200
        cls.ts.add_user(cls.owner.tenant_id, "brand-tech@example.com", "technician")
        cls.tech = Client(cls.ts)
        cls.tech.login("brand-tech@example.com")

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def test_defaults_then_update(self):
        d = self.owner.get("/api/branding").json
        self.assertEqual((d["primary_color"], d["header_color"], d["has_logo"]), ("", "", False))
        r = self.owner.put("/api/branding", {"primary_color": "#E4572E", "header_color": "#1A1A2E",
                                             "tagline": "Fast repairs", "show_name": False})
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json["primary_color"], "#e4572e")            # normalised to lower case
        self.assertFalse(r.json["show_name"])
        self.assertEqual(self.tech.get("/api/branding").json["tagline"], "Fast repairs")     # everyone sees the brand
        cleared = self.owner.put("/api/branding", {"primary_color": "", "header_color": "", "tagline": "", "show_name": True})
        self.assertEqual(cleared.json["primary_color"], "")

    def test_colour_validation(self):
        for bad in ("red", "#fff", "#GGGGGG", "#1234567", "url(x)", "javascript:alert(1)", "#12345", 123):
            r = self.owner.put("/api/branding", {"primary_color": bad})
            self.assertEqual(r.status, 400, bad)
        self.assertEqual(self.owner.put("/api/branding", {"tagline": "x" * 121}).status, 400)

    def test_only_business_managers_can_change_branding(self):
        self.assertEqual(self.tech.put("/api/branding", {"primary_color": "#000000"}).status, 403)
        self.assertEqual(self.tech.post("/api/branding/logo", {"data": b64(PNG)}).status, 403)
        self.assertEqual(self.tech.request("DELETE", "/api/branding/logo").status, 403)
        self.assertEqual(Client(self.ts).get("/api/branding").status, 401)

    def test_logo_round_trip(self):
        self.assertEqual(self.owner.get("/api/branding/logo").status, 404)
        r = self.owner.post("/api/branding/logo", {"data": b64(PNG)})
        self.assertEqual(r.status, 200, r.json)
        self.assertTrue(r.json["has_logo"])
        self.assertTrue(r.json["logo_version"])
        got = self.tech.get("/api/branding/logo")                        # any signed-in teammate can load it
        self.assertEqual(got.status, 200)
        self.assertEqual(got.raw, PNG)
        self.assertEqual(got.headers.get("Content-Type"), "image/png")
        self.assertEqual(got.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIn("private", got.headers.get("Cache-Control"))
        self.assertEqual(Client(self.ts).get("/api/branding/logo").status, 401)
        d = self.owner.request("DELETE", "/api/branding/logo")
        self.assertFalse(d.json["has_logo"])
        self.assertEqual(self.owner.get("/api/branding/logo").status, 404)

    def test_logo_rejects_dangerous_or_invalid_files(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        html = b"<html><script>alert(1)</script></html>"
        for name, raw in (("svg", svg), ("html", html), ("text", b"hello"), ("empty-ish", b"\x00")):
            self.assertEqual(self.owner.post("/api/branding/logo", {"data": b64(raw)}).status, 400, name)
        self.assertEqual(self.owner.post("/api/branding/logo", {"data": b64(PNG[:4] + b"x" * 10)}).status, 400)   # bad magic
        self.assertEqual(self.owner.post("/api/branding/logo", {"data": "%%%not base64%%%"}).status, 400)
        self.assertEqual(self.owner.post("/api/branding/logo", {"data": ""}).status, 400)
        self.assertEqual(self.owner.post("/api/branding/logo", {"data": 5}).status, 400)
        self.assertEqual(self.owner.post("/api/branding/logo", {}).status, 400)
        big = PNG + b"\x00" * (400 * 1024)
        self.assertEqual(self.owner.post("/api/branding/logo", {"data": b64(big)}).status, 400)

    def test_other_image_types_accepted(self):
        for name, raw, ctype in (("jpeg", b"\xff\xd8\xff\xe0" + b"\x00" * 20, "image/jpeg"),
                                 ("gif", b"GIF89a" + b"\x00" * 20, "image/gif"),
                                 ("webp", b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 8, "image/webp")):
            self.assertEqual(self.owner.post("/api/branding/logo", {"data": b64(raw)}).status, 200, name)
            self.assertEqual(self.owner.get("/api/branding/logo").headers.get("Content-Type"), ctype)
        self.owner.request("DELETE", "/api/branding/logo")

    def test_branding_is_per_tenant(self):
        other = Client(self.ts)
        self.assertEqual(other.register("Other Brand", "other-brand@example.com").status, 200)
        self.owner.put("/api/branding", {"primary_color": "#112233"})
        self.owner.post("/api/branding/logo", {"data": b64(PNG)})
        d = other.get("/api/branding").json
        self.assertEqual((d["primary_color"], d["has_logo"]), ("", False))
        self.assertEqual(other.get("/api/branding/logo").status, 404)
        self.owner.request("DELETE", "/api/branding/logo")
        self.owner.put("/api/branding", {})                              # back to defaults for the other tests

    def test_changes_are_audited_without_the_image(self):
        self.owner.post("/api/branding/logo", {"data": b64(PNG)})
        events = self.owner.get("/api/audit?action=settings.&limit=50").json["events"]
        self.assertIn("settings.logo", [e["action"] for e in events])
        self.assertNotIn(b64(PNG), str(events))
        self.owner.request("DELETE", "/api/branding/logo")


class Revenue(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = TestServer()
        cls.owner = Client(cls.ts)
        assert cls.owner.register("Rev Shop", "rev@example.com").status == 200
        cls.ts.add_user(cls.owner.tenant_id, "rev-tech@example.com", "technician")
        cls.ts.add_user(cls.owner.tenant_id, "rev-desk@example.com", "front_desk")
        cls.tech, cls.desk = Client(cls.ts), Client(cls.ts)
        cls.tech.login("rev-tech@example.com")
        cls.desk.login("rev-desk@example.com")
        cls.cust = cls.owner.post("/api/customers", {"name": "Rev Customer"}).json

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def _paid(self, net_cents, tax_bp=1000, pay=True):
        inv = self.owner.post("/api/invoices", {"customer_id": self.cust["id"], "tax_bp": tax_bp, "items": [
            {"kind": "manual", "name": "Job", "quantity": 1, "unit_price_cents": net_cents, "taxable": True}]}).json
        self.assertEqual(self.owner.post("/api/invoices/%s/issue" % inv["id"]).status, 200)
        if pay:
            self.assertEqual(self.owner.post("/api/invoices/%s/pay" % inv["id"], {"method": "Cash"}).status, 200)
        return inv

    def _rev(self, client=None):
        return (client or self.owner).get("/api/dashboard").json["revenue"]

    def test_empty_shop_has_zeroes_and_twelve_months(self):
        ts = TestServer()
        try:
            c = Client(ts)
            c.register("Empty Rev", "empty-rev@example.com")
            r = c.get("/api/dashboard").json["revenue"]
            self.assertEqual(len(r["months"]), 12)
            self.assertEqual(r["months"][-1]["month"], datetime.now(timezone.utc).strftime("%Y-%m"))
            self.assertEqual((r["this_month"]["total_cents"], r["ytd_cents"], r["outstanding_cents"], r["recent"]), (0, 0, 0, []))
        finally:
            ts.close()

    def test_paid_invoices_are_counted_with_tax_split(self):
        before = self._rev()
        self._paid(10000)                                           # 100.00 + 10% tax = 110.00
        after = self._rev()
        self.assertEqual(after["this_month"]["total_cents"] - before["this_month"]["total_cents"], 11000)
        self.assertEqual(after["this_month"]["tax_cents"] - before["this_month"]["tax_cents"], 1000)
        self.assertEqual(after["this_month"]["count"] - before["this_month"]["count"], 1)
        self.assertEqual(after["ytd_cents"] - before["ytd_cents"], 11000)
        self.assertEqual(after["months"][-1]["total_cents"], after["this_month"]["total_cents"])
        self.assertEqual(after["recent"][0]["total_cents"], 11000)
        self.assertEqual(after["recent"][0]["payment_method"], "Cash")

    def test_unpaid_issued_invoices_count_as_outstanding_not_revenue(self):
        before = self._rev()
        self._paid(5000, tax_bp=0, pay=False)
        after = self._rev()
        self.assertEqual(after["outstanding_cents"] - before["outstanding_cents"], 5000)
        self.assertEqual(after["outstanding_count"] - before["outstanding_count"], 1)
        self.assertEqual(after["this_month"]["total_cents"], before["this_month"]["total_cents"])

    def test_drafts_do_not_count(self):
        before = self._rev()
        self.owner.post("/api/invoices", {"customer_id": self.cust["id"], "items": [
            {"kind": "manual", "name": "Draft", "quantity": 1, "unit_price_cents": 99999}]})
        after = self._rev()
        self.assertEqual(before["this_month"], after["this_month"])
        self.assertEqual(before["outstanding_cents"], after["outstanding_cents"])

    def test_voiding_a_paid_invoice_removes_it(self):
        inv = self._paid(7000, tax_bp=0)
        mid = self._rev()
        self.owner.post("/api/invoices/%s/void" % inv["id"], {"reason": "refunded"})
        after = self._rev()
        self.assertEqual(mid["this_month"]["total_cents"] - after["this_month"]["total_cents"], 7000)

    def test_previous_month_payments_land_in_last_month(self):
        inv = self._paid(3000, tax_bp=0)
        before = self._rev()
        last = (datetime.now(timezone.utc).replace(day=1) - timedelta(days=1))
        db = self.ts.tenant_db(self.owner.tenant_id)
        try:
            db.execute("UPDATE invoices SET paid_at=? WHERE id=?", (last.strftime("%Y-%m-15T12:00:00Z"), inv["id"]))
        finally:
            db.close()
        after = self._rev()
        self.assertEqual(after["last_month"]["month"], last.strftime("%Y-%m"))
        self.assertEqual(after["last_month"]["total_cents"] - before["last_month"]["total_cents"], 3000)
        self.assertEqual(before["this_month"]["total_cents"] - after["this_month"]["total_cents"], 3000)

    def test_old_payments_stay_out_of_the_twelve_month_chart(self):
        inv = self._paid(1234, tax_bp=0)
        before = self._rev()
        db = self.ts.tenant_db(self.owner.tenant_id)
        try:
            db.execute("UPDATE invoices SET paid_at='2001-05-05T00:00:00Z' WHERE id=?", (inv["id"],))
        finally:
            db.close()
        after = self._rev()
        self.assertEqual(sum(m["total_cents"] for m in before["months"]) - sum(m["total_cents"] for m in after["months"]), 1234)
        self.assertEqual(before["ytd_cents"] - after["ytd_cents"], 1234)

    def test_only_people_who_can_read_invoices_see_revenue(self):
        self.assertIsNone(self.tech.get("/api/dashboard").json["revenue"])
        self.assertIsNotNone(self._rev(self.desk))
        self.assertIsNotNone(self.tech.get("/api/dashboard").json["tickets"])         # tickets part still works

    def test_revenue_is_per_tenant(self):
        self._paid(8888, tax_bp=0)
        ts = TestServer()
        try:
            other = Client(ts)
            other.register("Other Rev", "other-rev@example.com")
            r = other.get("/api/dashboard").json["revenue"]
            self.assertEqual((r["this_month"]["total_cents"], r["recent"]), (0, []))
        finally:
            ts.close()


if __name__ == "__main__":
    unittest.main()
