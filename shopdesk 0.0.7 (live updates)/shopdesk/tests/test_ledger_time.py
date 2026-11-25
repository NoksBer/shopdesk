"""Public sign-up, the cash ledger, and ticket time tracking."""
import pathlib
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

from harness import Client, TestServer
from shopdesk.db import connect, migrate

MIGRATIONS = pathlib.Path(__file__).resolve().parent.parent / "shopdesk" / "migrations" / "tenant"
TODAY = lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d")


def shop(name, email):
    ts = TestServer()
    c = Client(ts)
    assert c.register(name, email).status == 200
    return ts, c


def entries(c, query=""):
    return c.get("/api/ledger?limit=500" + query).json["entries"]


def paid_invoice(c, cust_id, net=10000, tax_bp=0, pay=True, method="Cash"):
    inv = c.post("/api/invoices", {"customer_id": cust_id, "tax_bp": tax_bp, "items": [
        {"kind": "manual", "name": "Job", "quantity": 1, "unit_price_cents": net, "taxable": True}]}).json
    assert c.post("/api/invoices/%s/issue" % inv["id"]).status == 200
    if pay:
        assert c.post("/api/invoices/%s/pay" % inv["id"], {"method": method}).status == 200
    return c.get("/api/invoices/%s" % inv["id"]).json


class SignUp(unittest.TestCase):
    def test_default_configuration_lets_new_shops_register(self):
        from shopdesk.config import Config
        self.assertTrue(Config(env={}, data_dir=tempfile.gettempdir()).open_signup)
        self.assertFalse(Config(env={"SHOPDESK_OPEN_SIGNUP": "0"}, data_dir=tempfile.gettempdir()).open_signup)

    def test_many_shops_can_register_and_each_gets_its_own_database_file(self):
        ts = TestServer()
        try:
            clients = []
            for i in range(3):
                c = Client(ts)
                self.assertTrue(c.get("/api/bootstrap").json["can_register"])
                self.assertEqual(c.register("Shop %d" % i, "owner%d@example.com" % i).status, 200)
                clients.append(c)
            dbs = {c.tenant_id for c in clients}
            self.assertEqual(len(dbs), 3)
            for c in clients:
                self.assertTrue((ts.config.tenants_dir / c.tenant_id / "tenant.db").exists())
            clients[0].post("/api/customers", {"name": "Only in shop 0"})
            self.assertEqual(clients[1].get("/api/customers").json["customers"], [])
            self.assertEqual(clients[2].get("/api/customers").json["customers"], [])
        finally:
            ts.close()


class Ledger(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts, cls.owner = shop("Ledger Shop", "ledger@example.com")
        for email, role in (("led-mgr@example.com", "manager"), ("led-desk@example.com", "front_desk"), ("led-tech@example.com", "technician")):
            cls.ts.add_user(cls.owner.tenant_id, email, role)
        cls.mgr, cls.desk, cls.tech = Client(cls.ts), Client(cls.ts), Client(cls.ts)
        cls.mgr.login("led-mgr@example.com")
        cls.desk.login("led-desk@example.com")
        cls.tech.login("led-tech@example.com")
        cls.cust = cls.owner.post("/api/customers", {"name": "Ledger Customer"}).json

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def _balance(self):
        return self.owner.get("/api/ledger").json["balance_cents"]

    def _part(self, **kw):
        body = {"name": "Ledger part", "price_cents": 1500, "cost_cents": 400, "quantity": 5}
        body.update(kw)
        r = self.owner.post("/api/inventory", body)
        self.assertEqual(r.status, 201, r.json)
        return r.json

    # ---- automatic entries
    def test_adding_stock_records_the_purchase(self):
        before = self._balance()
        part = self._part(name="Screen 15in")
        e = [x for x in entries(self.owner) if x["ref_id"] == part["id"]]
        self.assertEqual(len(e), 1)
        self.assertEqual(e[0]["amount_cents"], -5 * 400)
        self.assertEqual(e[0]["category"], "inventory_purchase")
        self.assertEqual(e[0]["ref_label"], "SKU %d" % part["part_no"])
        self.assertEqual(e[0]["entry_date"], TODAY())
        self.assertIn("Screen 15in", e[0]["description"])
        self.assertEqual(self._balance(), before - 2000)

    def test_no_entry_when_nothing_was_bought(self):
        before = len(entries(self.owner))
        no_stock = self._part(quantity=0)
        free = self._part(cost_cents=0)
        self.assertEqual(len(entries(self.owner)), before)
        self.assertNotIn(no_stock["id"], [x["ref_id"] for x in entries(self.owner)])
        self.assertNotIn(free["id"], [x["ref_id"] for x in entries(self.owner)])

    def test_received_stock_is_recorded_only_when_asked(self):
        part = self._part(quantity=1)
        base = self._balance()
        self.owner.post("/api/inventory/%s/adjust" % part["id"], {"delta": 2, "reason": "count correction"})
        self.assertEqual(self._balance(), base)
        self.owner.post("/api/inventory/%s/adjust" % part["id"], {"delta": 3, "reason": "supplier delivery", "record_purchase": True})
        self.assertEqual(self._balance(), base - 3 * 400)
        self.owner.post("/api/inventory/%s/adjust" % part["id"], {"delta": 2, "reason": "pricey batch", "record_purchase": True, "unit_cost_cents": 1000})
        self.assertEqual(self._balance(), base - 1200 - 2000)
        self.assertEqual(self.owner.post("/api/inventory/%s/adjust" % part["id"], {"delta": -1, "reason": "x", "record_purchase": True}).status, 400)
        self.owner.post("/api/inventory/%s/adjust" % part["id"], {"delta": -1, "reason": "broken"})
        self.assertEqual(self._balance(), base - 3200)                     # removing stock moves no cash

    def test_paid_invoice_adds_its_own_line_with_the_invoice_number(self):
        before = self._balance()
        inv = paid_invoice(self.owner, self.cust["id"], net=10000, tax_bp=1000)          # 110.00 with tax
        lines = [x for x in entries(self.owner) if x["ref_id"] == inv["id"]]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["amount_cents"], 11000)
        self.assertEqual(lines[0]["ref_label"], inv["number"])
        self.assertIn(inv["number"], lines[0]["description"])
        self.assertIn("Cash", lines[0]["description"])
        self.assertEqual(lines[0]["category"], "invoice_payment")
        self.assertEqual(self._balance(), before + 11000)

    def test_unpaid_issued_and_draft_invoices_move_no_cash(self):
        before = self._balance()
        paid_invoice(self.owner, self.cust["id"], net=5000, pay=False)
        self.owner.post("/api/invoices", {"customer_id": self.cust["id"], "items": [
            {"kind": "manual", "name": "Draft", "quantity": 1, "unit_price_cents": 999}]})
        self.assertEqual(self._balance(), before)

    def test_voiding_a_paid_invoice_reverses_its_payment_once(self):
        inv = paid_invoice(self.owner, self.cust["id"], net=7000)
        base = self._balance()
        self.assertEqual(self.owner.post("/api/invoices/%s/void" % inv["id"], {"reason": "refunded"}).status, 200)
        lines = [x for x in entries(self.owner) if x["ref_id"] == inv["id"]]
        self.assertEqual(sorted(x["amount_cents"] for x in lines), [-7000, 7000])
        rev = [x for x in lines if x["reverses_id"]][0]
        pay = [x for x in lines if not x["reverses_id"]][0]
        self.assertEqual(rev["reverses_id"], pay["id"])
        self.assertEqual(self._balance(), base - 7000)
        self.assertEqual(self.owner.post("/api/invoices/%s/void" % inv["id"], {"reason": "again"}).status, 409)
        self.assertEqual(self._balance(), base - 7000)

    def test_voiding_an_unpaid_invoice_does_not_touch_the_ledger(self):
        inv = paid_invoice(self.owner, self.cust["id"], net=3000, pay=False)
        base = self._balance()
        self.owner.post("/api/invoices/%s/void" % inv["id"], {"reason": "mistake"})
        self.assertEqual(self._balance(), base)

    # ---- running balance and filters
    def test_running_balance_follows_date_order_not_entry_order(self):
        ts, c = shop("Balance Shop", "balance@example.com")
        try:
            add = lambda d, direction, cents, cat: c.post("/api/ledger", {"direction": direction, "amount_cents": cents, "category": cat,
                                                                           "description": "e %s %s" % (d, cents), "entry_date": d})
            self.assertEqual(add("2026-02-01", "in", 10000, "sale").status, 201)       # entered first, happened later
            self.assertEqual(add("2026-01-10", "out", 2500, "rent").status, 201)
            self.assertEqual(add("2026-03-05", "out", 1000, "supplies").status, 201)
            rows = entries(c)
            self.assertEqual([r["entry_date"] for r in rows], ["2026-03-05", "2026-02-01", "2026-01-10"])
            self.assertEqual([r["balance_cents"] for r in rows], [6500, 7500, -2500])
            sub = c.get("/api/ledger?from=2026-02-01&to=2026-02-28").json
            self.assertEqual([r["balance_cents"] for r in sub["entries"]], [7500])            # true balance even when filtered
            self.assertEqual((sub["money_in"], sub["money_out"], sub["net"]), (10000, 0, 10000))
            self.assertEqual(sub["balance_cents"], 6500)
        finally:
            ts.close()

    def test_filters(self):
        ts, c = shop("Filter Ledger", "filter-led@example.com")
        try:
            post = lambda direction, cents, cat, desc: c.post("/api/ledger", {"direction": direction, "amount_cents": cents, "category": cat, "description": desc})
            post("in", 5000, "sale", "Cash sale of cables"); post("out", 1200, "rent", "March rent"); post("out", 300, "supplies", "Tape")
            self.assertEqual(len(entries(c, "&dir=in")), 1)
            self.assertEqual(len(entries(c, "&dir=out")), 2)
            self.assertEqual([r["description"] for r in entries(c, "&category=rent")], ["March rent"])
            self.assertEqual([r["description"] for r in entries(c, "&q=cables")], ["Cash sale of cables"])
            self.assertEqual(entries(c, "&q=%25"), [])                                      # '%' is literal
            self.assertEqual(c.get("/api/ledger?from=bad").status, 400)
            r = c.get("/api/ledger").json
            self.assertEqual((r["money_in"], r["money_out"], r["net"]), (5000, 1500, 3500))
            self.assertEqual(r["total"], 3)
        finally:
            ts.close()

    def test_paging(self):
        ts, c = shop("Paging Ledger", "paging-led@example.com")
        try:
            for i in range(5):
                c.post("/api/ledger", {"direction": "in", "amount_cents": 100 + i, "category": "sale", "description": "p%d" % i})
            first = c.get("/api/ledger?limit=2&offset=0").json
            second = c.get("/api/ledger?limit=2&offset=2").json
            self.assertEqual(first["total"], 5)
            ids = [e["id"] for e in first["entries"] + second["entries"]]
            self.assertEqual(len(set(ids)), 4)
            self.assertEqual(ids, sorted(ids, reverse=True))
        finally:
            ts.close()

    # ---- manual entries and reversals
    def test_manual_entry_validation(self):
        good = {"direction": "out", "amount_cents": 500, "category": "supplies", "description": "Tape"}
        self.assertEqual(self.owner.post("/api/ledger", good).status, 201)
        for field, bad in (("direction", "sideways"), ("amount_cents", 0), ("amount_cents", -5), ("amount_cents", 1.5),
                           ("amount_cents", "5"), ("amount_cents", True), ("category", "sale"), ("category", "nonsense"),
                           ("description", ""), ("entry_date", "2026-13-40"), ("entry_date", "yesterday")):
            self.assertEqual(self.owner.post("/api/ledger", dict(good, **{field: bad})).status, 400, (field, bad))
        future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
        self.assertEqual(self.owner.post("/api/ledger", dict(good, entry_date=future)).status, 400)
        self.assertEqual(self.owner.post("/api/ledger", {"direction": "in", "amount_cents": 100, "category": "rent", "description": "x"}).status, 400)

    def test_reversal(self):
        base = self._balance()
        eid = self.owner.post("/api/ledger", {"direction": "out", "amount_cents": 4000, "category": "rent", "description": "Oops rent"}).json["id"]
        self.assertEqual(self._balance(), base - 4000)
        self.assertEqual(self.owner.post("/api/ledger/%s/reverse" % eid, {}).status, 400)               # reason required
        r = self.owner.post("/api/ledger/%s/reverse" % eid, {"reason": "entered twice"})
        self.assertEqual(r.status, 201)
        self.assertEqual(self._balance(), base)
        rows = {e["id"]: e for e in entries(self.owner)}
        self.assertTrue(rows[eid]["reversed"])
        self.assertEqual(rows[r.json["id"]]["reverses_id"], eid)
        self.assertEqual(self.owner.post("/api/ledger/%s/reverse" % eid, {"reason": "again"}).status, 409)
        self.assertEqual(self.owner.post("/api/ledger/%s/reverse" % r.json["id"], {"reason": "undo undo"}).status, 409)
        self.assertEqual(self.owner.post("/api/ledger/999999/reverse", {"reason": "x"}).status, 404)
        self.assertEqual(self.owner.post("/api/ledger/abc/reverse", {"reason": "x"}).status, 404)

    def test_payment_lines_cannot_be_reversed_directly(self):
        inv = paid_invoice(self.owner, self.cust["id"], net=1000)
        line = [x for x in entries(self.owner) if x["ref_id"] == inv["id"]][0]
        self.assertEqual(self.owner.post("/api/ledger/%s/reverse" % line["id"], {"reason": "x"}).status, 409)

    def test_a_wrong_stock_purchase_can_be_reversed(self):
        part = self._part(name="Reverse me")
        line = [x for x in entries(self.owner) if x["ref_id"] == part["id"]][0]
        base = self._balance()
        self.assertEqual(self.owner.post("/api/ledger/%s/reverse" % line["id"], {"reason": "wrong quantity"}).status, 201)
        self.assertEqual(self._balance(), base + 2000)

    def test_ledger_rows_are_append_only_in_the_database(self):
        self.owner.post("/api/ledger", {"direction": "in", "amount_cents": 100, "category": "sale", "description": "x"})
        db = self.ts.tenant_db(self.owner.tenant_id)
        try:
            with self.assertRaises(Exception):
                db.execute("UPDATE ledger_entries SET amount_cents = 1")
            with self.assertRaises(Exception):
                db.execute("DELETE FROM ledger_entries")
            with self.assertRaises(Exception):
                db.execute("INSERT INTO ledger_entries(entry_date, ts, amount_cents, category, description) VALUES('2026-01-01','x',0,'sale','zero')")
        finally:
            db.close()

    # ---- permissions, summary, export, dashboard
    def test_permissions(self):
        for who in (self.desk, self.tech):
            self.assertEqual(who.get("/api/ledger").status, 403)
            self.assertEqual(who.get("/api/ledger/summary").status, 403)
            self.assertEqual(who.get("/api/ledger/export").status, 403)
            self.assertEqual(who.post("/api/ledger", {"direction": "in", "amount_cents": 1, "category": "sale", "description": "x"}).status, 403)
        self.assertEqual(self.mgr.get("/api/ledger").status, 200)
        self.assertEqual(self.mgr.post("/api/ledger", {"direction": "in", "amount_cents": 1, "category": "sale", "description": "mgr"}).status, 201)
        self.assertEqual(Client(self.ts).get("/api/ledger").status, 401)

    def test_summary_and_dashboard(self):
        ts, c = shop("Summary Shop", "summary@example.com")
        try:
            ts.add_user(c.tenant_id, "summary-tech@example.com", "technician")
            tech = Client(ts)
            tech.login("summary-tech@example.com")
            c.post("/api/ledger", {"direction": "in", "amount_cents": 9000, "category": "sale", "description": "in"})
            c.post("/api/ledger", {"direction": "out", "amount_cents": 4000, "category": "rent", "description": "out"})
            s = c.get("/api/ledger/summary").json
            self.assertEqual(s["balance_cents"], 5000)
            self.assertEqual((s["this_month"]["money_in"], s["this_month"]["money_out"], s["this_month"]["net"]), (9000, 4000, 5000))
            self.assertEqual(len(s["months"]), 6)
            self.assertEqual(s["months"][-1]["month"], datetime.now(timezone.utc).strftime("%Y-%m"))
            d = c.get("/api/dashboard").json["ledger"]
            self.assertEqual(d["balance_cents"], 5000)
            self.assertEqual(len(d["recent"]), 2)
            self.assertIsNone(tech.get("/api/dashboard").json["ledger"])
        finally:
            ts.close()

    def test_negative_balance_is_reported_as_negative(self):
        ts, c = shop("Negative Shop", "negative@example.com")
        try:
            c.post("/api/ledger", {"direction": "out", "amount_cents": 2500, "category": "rent", "description": "rent"})
            self.assertEqual(c.get("/api/ledger/summary").json["balance_cents"], -2500)
            self.assertEqual(c.get("/api/ledger").json["net"], -2500)
        finally:
            ts.close()

    def test_csv_export(self):
        ts, c = shop("Csv Shop", "csv@example.com")
        try:
            c.post("/api/ledger", {"direction": "in", "amount_cents": 12345, "category": "sale", "description": "=HYPERLINK(\"http://x\")", "entry_date": "2026-01-02"})
            c.post("/api/ledger", {"direction": "out", "amount_cents": 500, "category": "supplies", "description": "Tape, \"strong\"", "entry_date": "2026-01-03"})
            r = c.get("/api/ledger/export")
            self.assertEqual(r.status, 200)
            self.assertIn("text/csv", r.headers.get("Content-Type"))
            self.assertIn("attachment", r.headers.get("Content-Disposition"))
            text = r.raw.decode("utf-8-sig")
            lines = text.strip().splitlines()
            self.assertEqual(lines[0], "Date,Description,Reference,Category,Money in,Money out,Balance,Entered by")
            self.assertIn("2026-01-02", lines[1])                       # oldest first
            self.assertIn("123.45", lines[1])
            self.assertIn("'=HYPERLINK", lines[1])                      # formulas are neutralised
            self.assertNotIn(",=HYPERLINK", text)
            self.assertIn("5.00", lines[2])
            self.assertIn("118.45", lines[2])                           # running balance
        finally:
            ts.close()

    def test_ledger_is_per_business(self):
        ts, a = shop("Iso Led A", "iso-led-a@example.com")
        try:
            b = Client(ts)
            b.register("Iso Led B", "iso-led-b@example.com")
            eid = a.post("/api/ledger", {"direction": "in", "amount_cents": 777, "category": "sale", "description": "A only"}).json["id"]
            self.assertEqual(b.get("/api/ledger").json["entries"], [])
            self.assertEqual(b.get("/api/ledger/summary").json["balance_cents"], 0)
            self.assertEqual(b.post("/api/ledger/%s/reverse" % eid, {"reason": "x"}).status, 404)
            self.assertEqual(a.get("/api/ledger").json["balance_cents"], 777)
        finally:
            ts.close()


class LedgerBackfill(unittest.TestCase):
    def test_existing_paid_invoices_and_opening_stock_appear_after_upgrade(self):
        with tempfile.TemporaryDirectory() as d:
            old = pathlib.Path(d) / "old"
            old.mkdir()
            for f in sorted(MIGRATIONS.glob("*.sql")):
                if f.name < "0008":
                    (old / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
            conn = connect(pathlib.Path(d) / "t.db")
            migrate(conn, old)
            conn.execute("INSERT INTO customers(id, name, created_at, updated_at) VALUES('c1','Cust','2024-01-01T00:00:00Z','2024-01-01T00:00:00Z')")
            for iid, num, status, paid in (("i1", "INV-1001", "paid", "2024-05-05T10:00:00Z"), ("i2", "INV-1002", "issued", None), ("i3", "INV-1003", "void", None)):
                conn.execute("INSERT INTO invoices(id, number, customer_id, status, total_cents, paid_at, payment_method, created_at, updated_at) "
                             "VALUES(?,?,?,?,?,?,?,?,?)", (iid, num, "c1", status, 5000, paid, "Cash", "2024-05-01T00:00:00Z", "2024-05-01T00:00:00Z"))
            for pid, cost, qty in (("p1", 400, 3), ("p2", 0, 2)):
                conn.execute("INSERT INTO inventory_items(id, name, price_cents, cost_cents, quantity, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                             (pid, "Part " + pid, 1000, cost, qty, "2024-04-01T00:00:00Z", "2024-04-01T00:00:00Z"))
                conn.execute("INSERT INTO inventory_movements(item_id, ts, delta, reason) VALUES(?,?,?,?)", (pid, "2024-04-01T00:00:00Z", qty, "opening stock"))
            migrate(conn, MIGRATIONS)
            rows = conn.execute("SELECT entry_date, amount_cents, category, ref_label FROM ledger_entries ORDER BY id").fetchall()
            got = [(r["category"], r["amount_cents"], r["entry_date"], r["ref_label"]) for r in rows]
            self.assertIn(("invoice_payment", 5000, "2024-05-05", "INV-1001"), got)
            self.assertIn(("inventory_purchase", -1200, "2024-04-01", "SKU 0"), got)
            self.assertEqual(len(got), 2)                                # unpaid, void and free parts add nothing
            conn.close()


class TimeTracking(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts, cls.owner = shop("Time Shop", "time@example.com")
        cls.tech_id = cls.ts.add_user(cls.owner.tenant_id, "time-tech@example.com", "technician")
        cls.tech2_id = cls.ts.add_user(cls.owner.tenant_id, "time-tech2@example.com", "technician")
        cls.desk_id = cls.ts.add_user(cls.owner.tenant_id, "time-desk@example.com", "front_desk")
        cls.tech, cls.tech2, cls.desk = Client(cls.ts), Client(cls.ts), Client(cls.ts)
        cls.tech.login("time-tech@example.com")
        cls.tech2.login("time-tech2@example.com")
        cls.desk.login("time-desk@example.com")
        cls.cust = cls.owner.post("/api/customers", {"name": "Time Customer"}).json

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def _ticket(self, title="Timed job", assign=None, **kw):
        body = {"customer_id": self.cust["id"], "title": title}
        if assign:
            body["assignee_ids"] = assign
        body.update(kw)
        r = self.owner.post("/api/tickets", body)
        self.assertEqual(r.status, 201, r.json)
        return r.json

    def _stop_any(self, c):
        c.post("/api/time/clock-out", {})

    def test_clock_in_and_out_records_time(self):
        t = self._ticket(assign=[self.tech_id])
        r = self.tech.post("/api/tickets/%s/clock-in" % t["id"], {"note": "diagnosing"})
        self.assertEqual(r.status, 200, r.json)
        self.assertTrue(r.json["entry"]["running"])
        self.assertEqual(self.tech.get("/api/time/running").json["entry"]["ticket_id"], t["id"])
        self.assertEqual(self.tech.post("/api/tickets/%s/clock-in" % t["id"], {}).status, 409)          # already clocked in
        # age the timer by an hour so the duration is predictable
        db = self.ts.tenant_db(self.owner.tenant_id)
        try:
            hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            db.execute("UPDATE time_entries SET started_at=? WHERE id=?", (hour_ago, r.json["entry"]["id"]))
        finally:
            db.close()
        out = self.tech.post("/api/time/clock-out", {"note": "replaced the screen"})
        self.assertEqual(out.status, 200)
        e = out.json["entry"]
        self.assertFalse(e["running"])
        self.assertTrue(3599 <= e["seconds"] <= 3605, e["seconds"])
        self.assertEqual(e["note"], "replaced the screen")
        self.assertIsNone(self.tech.get("/api/time/running").json["entry"])
        self.assertEqual(self.tech.post("/api/time/clock-out", {}).status, 409)                         # nothing running
        kinds = [h["kind"] for h in self.owner.get("/api/tickets/%s" % t["id"]).json["history"]]
        self.assertIn("time_in", kinds)
        self.assertIn("time_out", kinds)

    def test_starting_a_second_timer_switches_from_the_first(self):
        a, b = self._ticket("Switch A", assign=[self.tech2_id]), self._ticket("Switch B", assign=[self.tech2_id])
        self.tech2.post("/api/tickets/%s/clock-in" % a["id"], {})
        r = self.tech2.post("/api/tickets/%s/clock-in" % b["id"], {})
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json["switched_from"], a["number"])
        self.assertEqual(self.tech2.get("/api/time/running").json["entry"]["ticket_id"], b["id"])
        first = self.tech2.get("/api/tickets/%s/time" % a["id"]).json["entries"][0]
        self.assertFalse(first["running"])
        db = self.ts.tenant_db(self.owner.tenant_id)
        try:
            running = db.execute("SELECT COUNT(*) FROM time_entries WHERE user_id=? AND ended_at IS NULL AND source='timer'", (self.tech2_id,)).fetchone()[0]
        finally:
            db.close()
        self.assertEqual(running, 1)
        self._stop_any(self.tech2)

    def test_database_allows_only_one_running_timer_per_person(self):
        t = self._ticket("One running", assign=[self.tech_id])
        self.tech.post("/api/tickets/%s/clock-in" % t["id"], {})
        db = self.ts.tenant_db(self.owner.tenant_id)
        try:
            with self.assertRaises(Exception):
                db.execute("INSERT INTO time_entries(id, ticket_id, user_id, work_date, started_at, source, created_at, updated_at) "
                           "VALUES('dup', ?, ?, '2026-01-01', '2026-01-01T00:00:00Z', 'timer', 'x', 'x')", (t["id"], self.tech_id))
        finally:
            db.close()
        self._stop_any(self.tech)

    def test_closing_a_ticket_stops_running_timers(self):
        t = self._ticket("Close me", assign=[self.tech_id])
        self.tech.post("/api/tickets/%s/clock-in" % t["id"], {})
        closed = [s for s in self.owner.get("/api/statuses").json["statuses"] if s["is_closed"]][0]
        self.assertEqual(self.owner.post("/api/tickets/%s/status" % t["id"], {"status_id": closed["id"]}).status, 200)
        self.assertIsNone(self.tech.get("/api/time/running").json["entry"])
        self.assertFalse(self.owner.get("/api/tickets/%s/time" % t["id"]).json["entries"][0]["running"])
        self.assertEqual(self.tech.post("/api/tickets/%s/clock-in" % t["id"], {}).status, 409)          # closed tickets reject timers

    def test_technician_cannot_clock_in_on_a_ticket_they_cannot_see(self):
        t = self._ticket("Not yours")
        self.assertEqual(self.tech.post("/api/tickets/%s/clock-in" % t["id"], {}).status, 404)
        self.assertEqual(self.tech.get("/api/tickets/%s/time" % t["id"]).status, 404)
        self.assertEqual(self.tech.post("/api/tickets/%s/time" % t["id"], {"minutes": 5}).status, 404)

    def test_front_desk_can_read_time_but_not_track_it(self):
        t = self._ticket("Desk view")
        self.assertEqual(self.desk.get("/api/tickets/%s/time" % t["id"]).status, 200)
        self.assertEqual(self.desk.post("/api/tickets/%s/clock-in" % t["id"], {}).status, 403)
        self.assertEqual(self.desk.post("/api/tickets/%s/time" % t["id"], {"minutes": 5}).status, 403)
        self.assertEqual(self.desk.get("/api/time/running").status, 403)
        self.assertEqual(self.desk.get("/api/time/entries").status, 403)

    def test_manual_entries_and_validation(self):
        t = self._ticket("Manual", assign=[self.tech_id])
        r = self.tech.post("/api/tickets/%s/time" % t["id"], {"work_date": "2026-02-03", "minutes": 90, "note": "phone call", "billable": False})
        self.assertEqual(r.status, 201, r.json)
        e = r.json["entry"]
        self.assertEqual((e["seconds"], e["work_date"], e["billable"], e["source"]), (5400, "2026-02-03", False, "manual"))
        for bad in ({"minutes": 0}, {"minutes": 1441}, {"minutes": 1.5}, {"minutes": "5"}, {}, {"minutes": 5, "work_date": "nope"},
                    {"minutes": 5, "work_date": (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")}):
            self.assertEqual(self.tech.post("/api/tickets/%s/time" % t["id"], bad).status, 400, bad)
        d = self.tech.get("/api/tickets/%s/time" % t["id"]).json
        self.assertEqual((d["total_seconds"], d["billable_seconds"]), (5400, 0))

    def test_totals_by_person(self):
        t = self._ticket("Totals", assign=[self.tech_id, self.tech2_id])
        self.tech.post("/api/tickets/%s/time" % t["id"], {"minutes": 60})
        self.tech2.post("/api/tickets/%s/time" % t["id"], {"minutes": 30})
        self.tech2.post("/api/tickets/%s/time" % t["id"], {"minutes": 15, "billable": False})
        d = self.owner.get("/api/tickets/%s/time" % t["id"]).json
        self.assertEqual((d["total_seconds"], d["billable_seconds"]), (6300, 5400))
        self.assertEqual(sorted(u["seconds"] for u in d["by_user"]), [2700, 3600])                       # one row per person
        self.assertEqual(len(d["entries"]), 3)

    def test_edit_and_delete_rules(self):
        t = self._ticket("Edit rules", assign=[self.tech_id, self.tech2_id])
        mine = self.tech.post("/api/tickets/%s/time" % t["id"], {"minutes": 20}).json["entry"]
        theirs = self.tech2.post("/api/tickets/%s/time" % t["id"], {"minutes": 40}).json["entry"]
        r = self.tech.put("/api/time/%s" % mine["id"], {"minutes": 45, "note": "fixed", "billable": False})
        self.assertEqual((r.json["entry"]["seconds"], r.json["entry"]["note"], r.json["entry"]["billable"]), (2700, "fixed", False))
        self.assertEqual(self.tech.put("/api/time/%s" % theirs["id"], {"minutes": 1}).status, 404)       # not yours
        self.assertEqual(self.tech.request("DELETE", "/api/time/%s" % theirs["id"]).status, 404)
        self.assertEqual(self.owner.put("/api/time/%s" % theirs["id"], {"minutes": 50}).json["entry"]["seconds"], 3000)   # managers can
        self.assertEqual(self.tech.request("DELETE", "/api/time/%s" % mine["id"]).status, 200)
        self.assertEqual(self.owner.request("DELETE", "/api/time/%s" % theirs["id"]).status, 200)
        self.assertEqual(self.owner.get("/api/tickets/%s/time" % t["id"]).json["entries"], [])
        kinds = [h["kind"] for h in self.owner.get("/api/tickets/%s" % t["id"]).json["history"]]
        self.assertIn("time_removed", kinds)

    def test_running_entry_duration_cannot_be_edited(self):
        t = self._ticket("Running edit", assign=[self.tech_id])
        e = self.tech.post("/api/tickets/%s/clock-in" % t["id"], {}).json["entry"]
        self.assertEqual(self.tech.put("/api/time/%s" % e["id"], {"minutes": 5}).status, 409)
        self.assertEqual(self.tech.put("/api/time/%s" % e["id"], {"note": "still working"}).status, 200)
        self._stop_any(self.tech)

    def test_timesheet_range_and_visibility(self):
        t = self._ticket("Sheet", assign=[self.tech_id, self.tech2_id])
        self.tech.post("/api/tickets/%s/time" % t["id"], {"work_date": "2026-01-05", "minutes": 60})
        self.tech2.post("/api/tickets/%s/time" % t["id"], {"work_date": "2026-01-06", "minutes": 120})
        q = "?from=2026-01-01&to=2026-01-31"
        everyone = self.owner.get("/api/time/entries" + q).json
        self.assertEqual(everyone["total_seconds"], 10800)
        mine = self.tech.get("/api/time/entries" + q).json
        self.assertEqual(mine["total_seconds"], 3600)                                   # technicians only ever see themselves
        spoof = self.tech.get("/api/time/entries%s&user_id=%s" % (q, self.tech2_id)).json
        self.assertEqual(spoof["total_seconds"], 3600)
        one = self.owner.get("/api/time/entries%s&user_id=%s" % (q, self.tech2_id)).json
        self.assertEqual(one["total_seconds"], 7200)
        for bad in ("?from=x&to=2026-01-02", "?from=2026-02-01&to=2026-01-01", "?from=2020-01-01&to=2026-01-01"):
            self.assertEqual(self.owner.get("/api/time/entries" + bad).status, 400, bad)

    def test_dashboard_shows_who_is_clocked_in(self):
        t = self._ticket("Dash timer", assign=[self.tech_id])
        self.tech.post("/api/tickets/%s/clock-in" % t["id"], {})
        owner_view = self.owner.get("/api/dashboard").json["time"]
        self.assertIn(t["id"], [e["ticket_id"] for e in owner_view["clocked_in"]])
        tech_view = self.tech.get("/api/dashboard").json["time"]
        self.assertEqual(tech_view["mine"]["ticket_id"], t["id"])
        self.assertIsNone(tech_view["clocked_in"])
        self.assertIsNone(self.desk.get("/api/dashboard").json["time"])
        self._stop_any(self.tech)

    def test_time_is_per_business(self):
        ts, a = shop("Time Iso A", "time-iso-a@example.com")
        try:
            b = Client(ts)
            b.register("Time Iso B", "time-iso-b@example.com")
            cust = a.post("/api/customers", {"name": "C"}).json
            t = a.post("/api/tickets", {"customer_id": cust["id"], "title": "A job"}).json
            e = a.post("/api/tickets/%s/time" % t["id"], {"minutes": 10}).json["entry"]
            self.assertEqual(b.get("/api/tickets/%s/time" % t["id"]).status, 404)
            self.assertEqual(b.post("/api/tickets/%s/clock-in" % t["id"], {}).status, 404)
            self.assertEqual(b.put("/api/time/%s" % e["id"], {"minutes": 1}).status, 404)
            self.assertEqual(b.request("DELETE", "/api/time/%s" % e["id"]).status, 404)
            self.assertEqual(b.get("/api/time/entries?from=2020-01-01&to=2030-01-01").json["entries"], [])
        finally:
            ts.close()


if __name__ == "__main__":
    unittest.main()
