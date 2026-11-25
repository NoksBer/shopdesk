"""Automatic, consecutive, never-reused SKU numbers for inventory items."""
import pathlib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from harness import Client, TestServer
from shopdesk.db import connect, migrate

MIGRATIONS = pathlib.Path(__file__).resolve().parent.parent / "shopdesk" / "migrations" / "tenant"


def new_shop(name, email):
    ts = TestServer()
    c = Client(ts)
    assert c.register(name, email).status == 200
    return ts, c


def add(c, name, **kw):
    r = c.post("/api/inventory", dict({"name": name, "price_cents": 100}, **kw))
    assert r.status == 201, r.json
    return r.json


class PartNumbers(unittest.TestCase):
    def test_numbers_start_at_one_and_increase_by_one(self):
        ts, c = new_shop("Num Shop", "num@example.com")
        try:
            self.assertEqual([add(c, "P%d" % i)["part_no"] for i in range(5)], [1, 2, 3, 4, 5])
        finally:
            ts.close()

    def test_each_business_has_its_own_sequence(self):
        ts, a = new_shop("Seq A", "seq-a@example.com")
        try:
            b = Client(ts)
            b.register("Seq B", "seq-b@example.com")
            add(a, "A1"); add(a, "A2")
            self.assertEqual(add(b, "B1")["part_no"], 1)
            self.assertEqual(add(a, "A3")["part_no"], 3)
        finally:
            ts.close()

    def test_a_number_is_never_reused_after_deactivation_or_zero_stock(self):
        ts, c = new_shop("Reuse Shop", "reuse@example.com")
        try:
            a, b, third = add(c, "One", quantity=2), add(c, "Two", quantity=1), add(c, "Three")
            self.assertEqual([a["part_no"], b["part_no"], third["part_no"]], [1, 2, 3])
            c.request("DELETE", "/api/inventory/%s" % b["id"])                       # part leaves the active list
            c.post("/api/inventory/%s/adjust" % a["id"], {"delta": -2, "reason": "sold out"})   # stock reaches zero
            self.assertEqual(add(c, "Four")["part_no"], 4)
            listed = {i["part_no"]: i for i in c.get("/api/inventory?include_inactive=1").json["items"]}
            self.assertEqual(sorted(listed), [1, 2, 3, 4])
            self.assertFalse(listed[2]["active"])
            self.assertEqual(listed[1]["quantity"], 0)
        finally:
            ts.close()

    def test_failed_create_does_not_use_up_a_number(self):
        ts, c = new_shop("Fail Shop", "fail@example.com")
        try:
            add(c, "First", sku="SUP-1")
            self.assertEqual(c.post("/api/inventory", {"name": "Dup", "price_cents": 1, "sku": "sup-1"}).status, 409)
            self.assertEqual(c.post("/api/inventory", {"name": "", "price_cents": 1}).status, 400)
            self.assertEqual(add(c, "Second")["part_no"], 2)                        # no gap
        finally:
            ts.close()

    def test_client_cannot_choose_or_change_the_number(self):
        ts, c = new_shop("Fixed Shop", "fixed@example.com")
        try:
            it = c.post("/api/inventory", {"name": "Chosen", "price_cents": 1, "part_no": 999}).json
            self.assertEqual(it["part_no"], 1)
            r = c.put("/api/inventory/%s" % it["id"], {"name": "Chosen", "price_cents": 1, "part_no": 50})
            self.assertEqual(r.json["part_no"], 1)
            db = ts.tenant_db(c.tenant_id)
            try:
                with self.assertRaises(Exception):
                    db.execute("UPDATE inventory_items SET part_no = 77 WHERE id = ?", (it["id"],))
                with self.assertRaises(Exception):
                    db.execute("DELETE FROM inventory_items WHERE id = ?", (it["id"],))
                self.assertEqual(db.execute("SELECT part_no FROM inventory_items WHERE id=?", (it["id"],)).fetchone()[0], 1)
            finally:
                db.close()
        finally:
            ts.close()

    def test_simultaneous_creates_get_distinct_consecutive_numbers(self):
        ts, c = new_shop("Race Shop", "race@example.com")
        try:
            def make(i):
                cl = Client(ts)
                cl.cookies, cl.csrf = dict(c.cookies), c.csrf
                return cl.post("/api/inventory", {"name": "Race %d" % i, "price_cents": 1})
            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(make, range(12)))
            self.assertTrue(all(r.status == 201 for r in results), [r.status for r in results])
            self.assertEqual(sorted(r.json["part_no"] for r in results), list(range(1, 13)))
        finally:
            ts.close()

    def test_search_by_number_puts_exact_match_first_and_sort_by_sku(self):
        ts, c = new_shop("Find Shop", "find@example.com")
        try:
            for name in ("Mike", "Lima", "Kilo", "Juliet", "India", "Hotel", "Golf", "Foxtrot", "Echo", "Delta", "Charlie", "Bravo"):
                add(c, name)                           # no digits in names; alphabetical order is the reverse of numeric order
            nums = [i["part_no"] for i in c.get("/api/inventory?q=1").json["items"]]
            self.assertEqual(nums[0], 1)               # exact match first, then 10, 11, 12 (contain a 1)
            self.assertEqual(sorted(nums[1:]), [10, 11, 12])
            self.assertEqual([i["part_no"] for i in c.get("/api/inventory?q=12").json["items"]], [12])
            by_sku = [i["part_no"] for i in c.get("/api/inventory?sort=sku").json["items"]]
            self.assertEqual(by_sku, sorted(by_sku))
            by_name = [i["name"] for i in c.get("/api/inventory").json["items"]]
            self.assertEqual(by_name, sorted(by_name))
        finally:
            ts.close()

    def test_technicians_can_see_numbers_to_pull_parts(self):
        ts, c = new_shop("Tech Num Shop", "tech-num@example.com")
        try:
            ts.add_user(c.tenant_id, "tech-num-user@example.com", "technician")
            t = Client(ts)
            t.login("tech-num-user@example.com")
            add(c, "Visible")
            self.assertEqual(t.get("/api/inventory").json["items"][0]["part_no"], 1)
        finally:
            ts.close()

    def test_creation_is_audited_with_the_number(self):
        ts, c = new_shop("Audit Num Shop", "audit-num@example.com")
        try:
            add(c, "Audited")
            ev = [e for e in c.get("/api/audit?action=inventory.created").json["events"]]
            self.assertEqual(len(ev), 1)
            self.assertIn('"sku": 1', ev[0]["detail"] if isinstance(ev[0]["detail"], str) else str(ev[0]["detail"]).replace("'", '"'))
        finally:
            ts.close()


class BackfillMigration(unittest.TestCase):
    def test_existing_items_are_numbered_in_creation_order_and_counter_continues(self):
        with tempfile.TemporaryDirectory() as d:
            old = pathlib.Path(d) / "old"
            old.mkdir()
            for f in sorted(MIGRATIONS.glob("*.sql")):
                if f.name < "0007":
                    (old / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
            conn = connect(pathlib.Path(d) / "t.db")
            migrate(conn, old)
            for iid, name, ts in (("c", "Third", "2024-03-01T00:00:00Z"), ("a", "First", "2024-01-01T00:00:00Z"),
                                  ("b", "Second", "2024-02-01T00:00:00Z"), ("d", "Same time", "2024-03-01T00:00:00Z")):
                conn.execute("INSERT INTO inventory_items(id, name, price_cents, created_at, updated_at) VALUES(?,?,?,?,?)",
                             (iid, name, 100, ts, ts))
            migrate(conn, MIGRATIONS)
            nums = {r["id"]: r["part_no"] for r in conn.execute("SELECT id, part_no FROM inventory_items")}
            self.assertEqual((nums["a"], nums["b"], nums["c"], nums["d"]), (1, 2, 3, 4))
            self.assertEqual(conn.execute("SELECT next_number FROM inventory_counter").fetchone()[0], 5)
            conn.close()

    def test_empty_database_counter_starts_at_one(self):
        with tempfile.TemporaryDirectory() as d:
            conn = connect(pathlib.Path(d) / "t.db")
            migrate(conn, MIGRATIONS)
            self.assertEqual(conn.execute("SELECT next_number FROM inventory_counter").fetchone()[0], 1)
            conn.close()


if __name__ == "__main__":
    unittest.main()
