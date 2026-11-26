"""Customer touch bases: scheduling API, the close-ticket offer, calendar/dashboard feeds and the background job."""
import pathlib
import shutil
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone

from harness import Client, TestServer
from shopdesk import touchjob


def iso(days=0):
    return (datetime.now(timezone.utc).date() + timedelta(days=days)).isoformat()


class Base(unittest.TestCase):
    def make_shop(self, name="Touch Shop", email="touch-owner@example.com"):
        owner = Client(self.ts)
        self.assertEqual(owner.register(name, email).status, 200)
        cust = owner.post("/api/customers", {"name": "Pat Customer"}).json
        return owner, cust

    def status_id(self, client, needle):
        return [s for s in client.get("/api/statuses").json["statuses"] if needle in s["name"]][0]["id"]

    def ticket(self, owner, cust, title="Fix laptop", **extra):
        body = {"customer_id": cust["id"], "title": title}
        body.update(extra)
        r = owner.post("/api/tickets", body)
        self.assertEqual(r.status, 201, r.raw)
        return r.json

    def move(self, client, ticket_id, needle):
        r = client.post("/api/tickets/%s/status" % ticket_id, {"status_id": self.status_id(client, needle)})
        self.assertEqual(r.status, 200, r.raw)
        return r.json

    def schedule(self, client, ticket_id, due=None, notes="Check the repair is holding up"):
        return client.post("/api/tickets/%s/touch-base" % ticket_id, {"due_date": due or iso(30), "notes": notes})

    def completed_ticket(self, owner, cust, **extra):
        t = self.ticket(owner, cust, **extra)
        self.move(owner, t["id"], "Completed")
        return t


class TouchBaseApi(Base):
    @classmethod
    def setUpClass(cls):
        cls.ts = TestServer()
        cls.owner, cls.cust = cls._shop()
        cls.tech_id = cls.ts.add_user(cls.owner.tenant_id, "tb-tech@example.com", "technician")
        cls.tech = Client(cls.ts)
        assert cls.tech.login("tb-tech@example.com").status == 200

    @classmethod
    def _shop(cls):
        owner = Client(cls.ts)
        assert owner.register("Touch API Shop", "touch-api@example.com").status == 200
        return owner, owner.post("/api/customers", {"name": "Pat Customer"}).json

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    # ---------------------------------------------------------------- table
    def test_table_has_the_requested_columns(self):
        db = self.ts.tenant_db(self.owner.tenant_id)
        try:
            cols = {r["name"] for r in db.execute("PRAGMA table_info(touch_bases)")}
        finally:
            db.close()
        self.assertEqual(cols, {"id", "ticket_id", "customer_id", "due_date", "notes", "status", "created_ticket_id",
                                "scheduled_by", "scheduled_at"})

    # ---------------------------------------------------------------- the offer when a ticket is closed
    def test_offer_only_when_moved_to_completed(self):
        t = self.ticket(self.owner, self.cust)
        done = self.move(self.owner, t["id"], "Completed")
        self.assertTrue(done["touch_base_offer"])
        reopened = self.move(self.owner, t["id"], "New")
        self.assertFalse(reopened["touch_base_offer"])
        cancelled = self.ticket(self.owner, self.cust, "Cancelled one")
        self.assertFalse(self.move(self.owner, cancelled["id"], "Cancelled")["touch_base_offer"])

    def test_not_offered_again_while_one_is_scheduled_but_offered_after_cancel(self):
        t = self.completed_ticket(self.owner, self.cust)
        r = self.schedule(self.owner, t["id"])
        self.assertEqual(r.status, 201, r.raw)
        self.move(self.owner, t["id"], "New")
        self.assertFalse(self.move(self.owner, t["id"], "Completed")["touch_base_offer"])          # still scheduled
        tb = r.json["touch_base"]
        self.assertEqual(self.owner.post("/api/touch-bases/%s/cancel" % tb["id"]).status, 200)
        self.move(self.owner, t["id"], "New")
        self.assertTrue(self.move(self.owner, t["id"], "Completed")["touch_base_offer"])           # cancelled -> offer again

    def test_closing_is_never_blocked_by_touch_bases(self):
        t = self.ticket(self.owner, self.cust)
        r = self.owner.post("/api/tickets/%s/status" % t["id"], {"status_id": self.status_id(self.owner, "Completed")})
        self.assertEqual(r.status, 200)
        self.assertTrue(r.json["status_closed"])

    # ---------------------------------------------------------------- scheduling
    def test_schedule_show_reschedule_cancel(self):
        t = self.completed_ticket(self.owner, self.cust)
        r = self.schedule(self.owner, t["id"], iso(60), "Ask about the battery")
        self.assertEqual(r.status, 201, r.raw)
        tb = r.json["touch_base"]
        self.assertEqual((tb["status"], tb["due_date"], tb["notes"], tb["customer_name"], tb["ticket_number"]),
                         ("scheduled", iso(60), "Ask about the battery", "Pat Customer", t["number"]))
        self.assertEqual(tb["scheduled_by_name"], "Owner")
        shown = self.owner.get("/api/tickets/%s/touch-base" % t["id"]).json["touch_base"]
        self.assertEqual(shown["id"], tb["id"])
        moved = self.owner.put("/api/touch-bases/%s" % tb["id"], {"due_date": iso(90), "notes": "New notes"})
        self.assertEqual(moved.status, 200, moved.raw)
        self.assertEqual((moved.json["touch_base"]["due_date"], moved.json["touch_base"]["notes"]), (iso(90), "New notes"))
        self.assertEqual(self.owner.post("/api/touch-bases/%s/cancel" % tb["id"]).status, 200)
        self.assertIsNone(self.owner.get("/api/tickets/%s/touch-base" % t["id"]).json["touch_base"])
        self.assertEqual(self.owner.post("/api/touch-bases/%s/cancel" % tb["id"]).status, 409)       # already cancelled
        self.assertEqual(self.owner.put("/api/touch-bases/%s" % tb["id"], {"due_date": iso(40)}).status, 409)
        history = [h for h in self.owner.get("/api/tickets/%s" % t["id"]).json["history"] if h["kind"] == "touch_base"]
        self.assertEqual([h["detail"]["action"] for h in history], ["scheduled", "rescheduled", "cancelled"])
        actions = [e["action"] for e in self.owner.get("/api/audit?limit=200").json["events"]]
        for a in ("touchbase.scheduled", "touchbase.rescheduled", "touchbase.cancelled"):
            self.assertIn(a, actions)

    def test_only_one_active_touch_base_per_ticket(self):
        t = self.completed_ticket(self.owner, self.cust)
        self.assertEqual(self.schedule(self.owner, t["id"]).status, 201)
        self.assertEqual(self.schedule(self.owner, t["id"]).status, 409)

    def test_only_completed_tickets_can_get_one(self):
        open_t = self.ticket(self.owner, self.cust)
        self.assertEqual(self.schedule(self.owner, open_t["id"]).status, 409)
        cancelled = self.ticket(self.owner, self.cust, "Cancelled")
        self.move(self.owner, cancelled["id"], "Cancelled")
        self.assertEqual(self.schedule(self.owner, cancelled["id"]).status, 409)

    def test_validation(self):
        t = self.completed_ticket(self.owner, self.cust)
        url = "/api/tickets/%s/touch-base" % t["id"]
        for body in ({}, {"due_date": ""}, {"due_date": "06/01/2031"}, {"due_date": "2031-13-45"}, {"due_date": iso(-10)},
                     {"due_date": iso(5000)}, {"due_date": iso(30), "notes": "x" * 1001}, {"due_date": 5}):
            self.assertEqual(self.owner.post(url, body).status, 400, body)
        self.assertEqual(self.owner.post(url, {"due_date": iso(0)}).status, 201)             # today is allowed

    # ---------------------------------------------------------------- calendar and dashboard feeds
    def test_calendar_feed_and_dashboard_card(self):
        owner, cust = self._fresh_shop("Feed Shop", "feed@example.com")
        a = self.completed_ticket(owner, cust, title="A")
        b = self.completed_ticket(owner, cust, title="B")
        c = self.completed_ticket(owner, cust, title="C")
        self.schedule(owner, a["id"], iso(0))
        self.schedule(owner, b["id"], iso(10))
        self.schedule(owner, c["id"], iso(40))
        db = self.ts.tenant_db(owner.tenant_id)
        late = self.completed_ticket(owner, cust, title="Late")
        self.schedule(owner, late["id"], iso(1))
        db.execute("UPDATE touch_bases SET due_date=? WHERE ticket_id=?", (iso(-5), late["id"]))       # overdue
        db.close()
        feed = owner.get("/api/touch-bases?from=%s&to=%s" % (iso(-10), iso(60))).json["touch_bases"]
        self.assertEqual([x["ticket_id"] for x in feed], [late["id"], a["id"], b["id"], c["id"]])
        self.assertEqual(owner.get("/api/touch-bases?from=%s&to=%s" % (iso(5), iso(20))).json["touch_bases"][0]["ticket_id"], b["id"])
        self.assertEqual(owner.get("/api/touch-bases?from=%s&to=%s" % (iso(0), iso(200))).status, 400)       # range too long
        self.assertEqual(owner.get("/api/touch-bases").status, 400)
        card = owner.get("/api/dashboard").json["touch_bases"]
        self.assertEqual([x["ticket_id"] for x in card], [late["id"], a["id"], b["id"]])                  # overdue + next 14 days
        self.assertEqual([x["overdue"] for x in card], [True, False, False])

    def _fresh_shop(self, name, email):
        owner = Client(self.ts)
        self.assertEqual(owner.register(name, email).status, 200)
        return owner, owner.post("/api/customers", {"name": "Pat Customer"}).json

    # ---------------------------------------------------------------- permissions and visibility
    def test_technician_only_sees_and_schedules_for_assigned_tickets(self):
        owner, cust = self._fresh_shop("Vis Shop", "vis@example.com")
        tech_id = self.ts.add_user(owner.tenant_id, "vis-tech@example.com", "technician")
        tech = Client(self.ts)
        self.assertEqual(tech.login("vis-tech@example.com").status, 200)
        mine = self.completed_ticket(owner, cust, title="Mine", assignee_ids=[tech_id])
        other = self.completed_ticket(owner, cust, title="Other")
        self.schedule(owner, other["id"], iso(3))
        self.assertEqual(tech.get("/api/tickets/%s/touch-base" % other["id"]).status, 404)
        self.assertEqual(self.schedule(tech, other["id"]).status, 404)
        r = self.schedule(tech, mine["id"], iso(4))
        self.assertEqual(r.status, 201, r.raw)
        visible = tech.get("/api/touch-bases?from=%s&to=%s" % (iso(0), iso(30))).json["touch_bases"]
        self.assertEqual([x["ticket_id"] for x in visible], [mine["id"]])
        dash = tech.get("/api/dashboard").json["touch_bases"]
        self.assertEqual([x["ticket_id"] for x in dash], [mine["id"]])
        other_tb = owner.get("/api/tickets/%s/touch-base" % other["id"]).json["touch_base"]
        self.assertEqual(tech.put("/api/touch-bases/%s" % other_tb["id"], {"due_date": iso(9)}).status, 404)
        self.assertEqual(tech.post("/api/touch-bases/%s/cancel" % other_tb["id"]).status, 404)

    def test_other_shop_cannot_touch_these(self):
        owner, cust = self._fresh_shop("Iso Shop A", "iso-a@example.com")
        t = self.completed_ticket(owner, cust)
        tb = self.schedule(owner, t["id"]).json["touch_base"]
        other = Client(self.ts)
        self.assertEqual(other.register("Iso Shop B", "iso-b@example.com").status, 200)
        self.assertEqual(other.get("/api/tickets/%s/touch-base" % t["id"]).status, 404)
        self.assertEqual(self.schedule(other, t["id"]).status, 404)
        self.assertEqual(other.put("/api/touch-bases/%s" % tb["id"], {"due_date": iso(9)}).status, 404)
        self.assertEqual(other.post("/api/touch-bases/%s/cancel" % tb["id"]).status, 404)
        self.assertEqual(other.get("/api/touch-bases?from=%s&to=%s" % (iso(0), iso(30))).json["touch_bases"], [])


class TouchBaseJob(Base):
    """A fresh server per test so every run starts from a known state."""

    def setUp(self):
        self.ts = TestServer()
        self.owner, self.cust = self.make_shop()
        self.tech_id = self.ts.add_user(self.owner.tenant_id, "job-tech@example.com", "technician")
        self.cfg = self.ts.config

    def tearDown(self):
        self.ts.close()

    def db(self):
        return self.ts.tenant_db(self.owner.tenant_id)

    def tickets(self):
        db = self.db()
        try:
            return [dict(r) for r in db.execute("SELECT * FROM tickets ORDER BY created_at, number")]
        finally:
            db.close()

    def followups(self):
        return [t for t in self.tickets() if t["title"].startswith("Touch base:")]

    def due_touch_base(self, **ticket_extra):
        t = self.completed_ticket(self.owner, self.cust, **ticket_extra)
        r = self.schedule(self.owner, t["id"], iso(0), "Ask how the new SSD is performing")
        self.assertEqual(r.status, 201, r.raw)
        return t, r.json["touch_base"]

    def test_creates_one_followup_with_the_requested_details(self):
        t, tb = self.due_touch_base(assignee_ids=[self.tech_id], symptoms="Will not boot", intake_notes="Customer left charger")
        self.assertEqual(touchjob.run_once(self.cfg), 1)
        made = self.followups()
        self.assertEqual(len(made), 1)
        f = made[0]
        self.assertEqual(f["title"], "Touch base: Pat Customer (follow-up to #%s)" % t["number"])
        self.assertEqual(f["priority"], "low")
        first = self.owner.get("/api/statuses").json["statuses"][0]
        self.assertEqual(f["status_id"], first["id"])
        self.assertIn("New", first["name"])
        for part in ("Pat Customer", "#%s" % t["number"], t["id"], "Ask how the new SSD is performing", "Will not boot",
                     "Customer left charger", "Fix laptop"):
            self.assertIn(part, f["intake_notes"])
        detail = self.owner.get("/api/tickets/%s" % f["id"]).json
        self.assertEqual([a["id"] for a in detail["assignees"]], [self.tech_id])
        db = self.db()
        try:
            row = db.execute("SELECT status, created_ticket_id FROM touch_bases WHERE id=?", (tb["id"],)).fetchone()
            audit = [r["action"] for r in db.execute("SELECT action FROM audit_log")]
        finally:
            db.close()
        self.assertEqual((row["status"], row["created_ticket_id"]), ("created", f["id"]))
        self.assertIn("touchbase.created", audit)
        kinds = [h["kind"] for h in self.owner.get("/api/tickets/%s" % t["id"]).json["history"]]
        self.assertIn("touch_base", kinds)

    def test_unassigned_when_the_original_had_nobody(self):
        self.due_touch_base()
        touchjob.run_once(self.cfg)
        f = self.followups()[0]
        self.assertEqual(self.owner.get("/api/tickets/%s" % f["id"]).json["assignees"], [])

    def test_is_idempotent_across_runs(self):
        self.due_touch_base()
        self.assertEqual(touchjob.run_once(self.cfg), 1)
        self.assertEqual(touchjob.run_once(self.cfg), 0)
        self.assertEqual(touchjob.run_once(self.cfg), 0)
        self.assertEqual(len(self.followups()), 1)

    def test_overlapping_creators_make_only_one_ticket(self):
        _, tb = self.due_touch_base()
        results, barrier = [], threading.Barrier(4)

        def worker():
            db = self.db()
            try:
                barrier.wait()
                results.append(touchjob.create_followup(db, tb["id"]))
            finally:
                db.close()
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(len([r for r in results if r]), 1)
        self.assertEqual(len(self.followups()), 1)

    def test_future_touch_bases_wait_and_old_ones_catch_up(self):
        t = self.completed_ticket(self.owner, self.cust)
        future = self.schedule(self.owner, t["id"], iso(30)).json["touch_base"]
        self.assertEqual(touchjob.run_once(self.cfg), 0)
        self.assertEqual(touchjob.run_once(self.cfg, today=iso(29)), 0)
        self.assertEqual(touchjob.run_once(self.cfg, today=iso(30)), 1)                  # due today
        t2 = self.completed_ticket(self.owner, self.cust, title="Older")
        old = self.schedule(self.owner, t2["id"], iso(1)).json["touch_base"]
        db = self.db()
        db.execute("UPDATE touch_bases SET due_date='2001-01-01' WHERE id=?", (old["id"],))   # server was down for years
        db.close()
        self.assertEqual(touchjob.run_once(self.cfg), 1)
        self.assertEqual(len(self.followups()), 2)
        self.assertIsNotNone(future)

    def test_cancelled_touch_bases_never_become_tickets(self):
        _, tb = self.due_touch_base()
        self.owner.post("/api/touch-bases/%s/cancel" % tb["id"])
        self.assertEqual(touchjob.run_once(self.cfg), 0)
        self.assertEqual(self.followups(), [])

    def test_archived_customer_is_skipped_with_an_audit_entry(self):
        _, tb = self.due_touch_base()
        db = self.db()
        db.execute("UPDATE customers SET archived_at='2030-01-01T00:00:00Z' WHERE id=?", (self.cust["id"],))
        db.close()
        self.assertEqual(touchjob.run_once(self.cfg), 0)
        self.assertEqual(self.followups(), [])
        db = self.db()
        try:
            status = db.execute("SELECT status FROM touch_bases WHERE id=?", (tb["id"],)).fetchone()[0]
            audit = [r["action"] for r in db.execute("SELECT action FROM audit_log")]
        finally:
            db.close()
        self.assertEqual(status, "cancelled")
        self.assertIn("touchbase.skipped", audit)

    def test_suspended_shop_is_skipped_then_caught_up_when_reactivated(self):
        self.due_touch_base()
        plat = self.ts.platform_db()
        plat.execute("UPDATE tenants SET status='suspended' WHERE id=?", (self.owner.tenant_id,))
        self.assertEqual(touchjob.run_once(self.cfg), 0)
        self.assertEqual(self.followups(), [])
        plat.execute("UPDATE tenants SET status='active' WHERE id=?", (self.owner.tenant_id,))
        plat.close()
        self.assertEqual(touchjob.run_once(self.cfg), 1)

    def test_missing_shop_database_does_not_stop_other_shops(self):
        self.due_touch_base()
        gone = Client(self.ts)
        self.assertEqual(gone.register("Vanishing Shop", "vanish@example.com").status, 200)
        shutil.rmtree(self.cfg.tenants_dir / gone.tenant_id)
        self.assertEqual(touchjob.run_once(self.cfg), 1)                                  # no exception, the other shop is served

    def test_offered_again_after_the_touch_base_became_a_ticket(self):
        t, _ = self.due_touch_base()
        self.assertEqual(touchjob.run_once(self.cfg), 1)
        self.move(self.owner, t["id"], "New")
        self.assertTrue(self.move(self.owner, t["id"], "Completed")["touch_base_offer"])

    def test_an_unreachable_platform_database_does_not_raise(self):
        broken = types.SimpleNamespace(platform_db_path=pathlib.Path(self.ts.tmp) / "no-such-folder" / "platform.db")
        self.assertEqual(touchjob.run_once(broken), 0)

    def test_new_ticket_numbers_do_not_collide(self):
        self.due_touch_base()
        touchjob.run_once(self.cfg)
        numbers = [t["number"] for t in self.tickets()]
        self.assertEqual(len(numbers), len(set(numbers)))


if __name__ == "__main__":
    unittest.main()
