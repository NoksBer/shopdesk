"""Auto tinting profile, v0.1.2: "Job" wording, job location, due time, vehicle "e.g." examples, phone layout. Other types unchanged."""
import pathlib
import re
import unittest

from harness import Client, TestServer
from shopdesk.profiles import PROFILES

TINT = "auto_tint"
STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"
OTHER_TYPES = [k for k in PROFILES if k != TINT]


def read(name):
    return (STATIC / name).read_text(encoding="utf-8")


class JobSettings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = TestServer()
        cls.tint = Client(cls.ts)
        assert cls.tint.register("Job Tint Shop", "job-tint@example.com", shop_type=TINT).status == 200
        cls.others = {}
        for i, key in enumerate(OTHER_TYPES):
            c = Client(cls.ts)
            assert c.register("Job Other %s" % key, "job-other-%d@example.com" % i, shop_type=key).status == 200
            cls.others[key] = c
        cls.cust = cls.tint.post("/api/customers", {"name": "Job Customer"}).json["id"]

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def job(self, client=None, **kw):
        body = {"customer_id": self.cust, "title": "Ceramic tint"}
        body.update(kw)
        return (client or self.tint).post("/api/tickets", body)

    # ---- wording and options come from the profile
    def test_tint_profile_renames_tickets_to_jobs_and_sets_examples(self):
        me = self.tint.get("/api/me").json
        t = me["terminology"]
        self.assertEqual((t["ticket"], t["tickets"], t["item"]), ("Job", "Jobs", "Vehicle"))
        self.assertEqual(me["ticket_options"], {"location_label": "Where will job take place", "due_time": True, "mobile": True})
        eg = [k for k in t if k.startswith("eg_")]
        self.assertGreaterEqual(len(eg), 9)
        for k in eg:
            self.assertIn("e.g.", t[k], k)
            self.assertNotRegex(t[k].lower(), r"laptop|battery|screen|phone|repair|rent", k)       # vehicle-related, not the old repair examples

    def test_other_types_get_no_job_wording_no_options_no_examples(self):
        for key, c in self.others.items():
            me = c.get("/api/me").json
            self.assertFalse([k for k in me["terminology"] if k in ("ticket", "tickets") or k.startswith("eg_")], key)
            self.assertEqual(me["ticket_options"], {}, key)
        for key in OTHER_TYPES:
            self.assertEqual(PROFILES[key]["terminology"].keys() & {"ticket", "tickets"}, set())
            self.assertNotIn("ticket_options", PROFILES[key])

    def test_a_tint_shop_created_before_this_version_gets_the_wording_too(self):
        shop = Client(self.ts)
        self.assertEqual(shop.register("Old Tint Shop", "old-tint@example.com", shop_type=TINT).status, 200)
        db = self.ts.tenant_db(shop.tenant_id)
        db.execute("UPDATE settings SET value=? WHERE key='terminology'", ('{"item": "Vehicle", "items": "Vehicles"}',))
        db.close()
        t = shop.get("/api/me").json["terminology"]
        self.assertEqual((t["ticket"], t["tickets"], t["item"]), ("Job", "Jobs", "Vehicle"))
        db = self.ts.tenant_db(shop.tenant_id)
        self.assertEqual(db.execute("SELECT value FROM settings WHERE key='terminology'").fetchone()[0], '{"item": "Vehicle", "items": "Vehicles"}')
        db.close()                                                              # nothing was rewritten in the shop's database

    # ---- location and due time
    def test_job_location_and_due_time_round_trip(self):
        r = self.job(job_location="Customer's driveway, 12 Oak St", due_date="2031-05-10", due_time="14:30")
        self.assertEqual(r.status, 201, r.raw)
        t = r.json
        self.assertEqual((t["job_location"], t["due_date"], t["due_time"]), ("Customer's driveway, 12 Oak St", "2031-05-10", "14:30"))
        listed = next(x for x in self.tint.get("/api/tickets?status_id=").json["tickets"] if x["id"] == t["id"])
        self.assertEqual((listed["job_location"], listed["due_time"]), ("Customer's driveway, 12 Oak St", "14:30"))
        u = self.tint.put("/api/tickets/%s" % t["id"], {"title": "Ceramic tint", "due_date": "2031-05-11", "due_time": "09:05", "job_location": "Shop bay 2"})
        self.assertEqual((u.status, u.json["job_location"], u.json["due_time"], u.json["due_date"]), (200, "Shop bay 2", "09:05", "2031-05-11"))
        edited = [h for h in u.json["history"] if h["kind"] == "edited"][-1]["detail"]["changed"]
        self.assertIn("job_location", edited)
        self.assertIn("due_time", edited)
        cleared = self.tint.put("/api/tickets/%s" % t["id"], {"title": "Ceramic tint", "due_date": "", "due_time": ""}).json
        self.assertEqual((cleared["due_date"], cleared["due_time"]), (None, ""))

    def test_due_time_validation(self):
        for bad in ("25:00", "9:5", "noon", "12:60"):
            self.assertEqual(self.job(due_date="2031-05-10", due_time=bad).status, 400, bad)
        self.assertEqual(self.job(due_time="10:00").status, 400)                   # a time needs a date
        self.assertEqual(self.job(job_location="x" * 201).status, 400)
        self.assertEqual(self.job(due_date="2031-05-10").status, 201)

    def test_new_columns_are_additive_and_default_empty(self):
        for key, c in self.others.items():
            t = c.post("/api/customers", {"name": "Plain"}).json
            j = c.post("/api/tickets", {"customer_id": t["id"], "title": "Plain job", "due_date": "2031-01-01"}).json
            self.assertEqual((j["job_location"], j["due_time"]), ("", ""), key)
        db = self.ts.tenant_db(self.tint.tenant_id)
        cols = {r["name"]: r for r in db.execute("PRAGMA table_info(tickets)")}
        versions = [r[0] for r in db.execute("SELECT version FROM schema_version")]
        db.close()
        self.assertIn("job_location", cols)
        self.assertIn("due_time", cols)
        self.assertIn(14, versions)

    def test_tickets_of_one_shop_never_show_in_another(self):
        mine = self.job(job_location="Only here").json
        for c in self.others.values():
            self.assertEqual(c.get("/api/tickets/%s" % mine["id"]).status, 404)

    # ---- what the browser scripts do (guards; the screens themselves are not run by these tests)
    def test_scripts_use_the_wording_and_example_helpers(self):
        common = read("common.js")
        for name in ("function jw(", "function eg(", "const fmtTime"):
            self.assertIn(name, common)
        src = {n: read(n) for n in ("app.js", "billing.js", "board.js", "timer.js", "touchbase.js", "branding.js", "calitems.js", "ledger.js")}
        for n, text in src.items():
            self.assertNotIn('jw("ticket")', text, n)                                # entity names / keys must never be reworded
            for i, line in enumerate(text.splitlines(), 1):
                if "e.g." in line and not line.lstrip().startswith("//"):
                    self.assertIn("eg(", line, "%s:%d has an unwrapped example" % (n, i))
        for label in ('jw("+ New ticket")', 'jw("Create ticket")', 'jw("Edit ticket")', 'jw("Tickets")'):
            self.assertIn(label, src["app.js"])
        self.assertIn("jobExtraInputs", src["app.js"])

    def test_phone_layout_is_scoped_to_the_profile_flag(self):
        css = read("style.css")
        block = css[css.index("v0.1.2: phone-friendly layout"):]
        selectors = re.findall(r"^\s*(body[^{]+)\{", block, re.M)
        self.assertTrue(selectors)
        self.assertTrue(all(s.strip().startswith("body.mobile-first") or "body.mobile-first" in s for s in selectors))
        self.assertIn("@media (max-width: 700px)", block)
        self.assertIn("mobile-first", read("app.js"))
        self.assertEqual(self.tint.get("/api/me").json["ticket_options"]["mobile"], True)


if __name__ == "__main__":
    unittest.main()
