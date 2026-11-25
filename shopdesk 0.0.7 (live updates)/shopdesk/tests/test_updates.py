"""In-app software updates: version, release check, apply (request file), status. GitHub is faked; the root-owned
updater script (install/shopdesk-update) itself is not exercised here (it needs a real container)."""
import json
import os
import pathlib
import time
import unittest

from harness import PASSWORD, Client, TestServer
from shopdesk import __version__, updater
from shopdesk.updater import UpdateError, parse_semver


def release(tag, assets=True, body="Fixes and improvements"):
    names = ["shopdesk-%s.tar.gz" % tag, "shopdesk-%s.tar.gz.sha256" % tag] if assets else []
    return {"tag_name": tag, "body": body, "published_at": "2030-01-01T00:00:00Z",
            "assets": [{"name": n} for n in names]}


class Semver(unittest.TestCase):
    def test_parse_and_order(self):
        self.assertEqual(parse_semver("v6.1.0"), (6, 1, 0))
        self.assertEqual(parse_semver("6.10.2"), (6, 10, 2))
        self.assertGreater(parse_semver("v6.10.0"), parse_semver("v6.9.9"))     # numeric, not text, comparison
        for bad in ("6.1", "v1.2.3-beta", "latest", "", None, "v1.2.3; rm -rf /", "1.2.3.4"):
            self.assertIsNone(parse_semver(bad), bad)

    def test_version_file_is_the_source(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        self.assertEqual((root / "VERSION").read_text().strip(), __version__)
        self.assertIsNotNone(parse_semver(__version__))

    def test_repo_must_look_like_owner_slash_name(self):
        with self.assertRaises(UpdateError):
            updater.fetch_latest_release("evil.com/x?y=z")
        with self.assertRaises(UpdateError):
            updater.fetch_latest_release("../etc/passwd")


class Updates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = TestServer()
        cls.ts.create_platform_admin()
        cls.admin = Client(cls.ts)
        r = cls.admin.post("/api/platform/login", {"email": "root@example.com", "password": PASSWORD})
        assert r.status == 200
        cls.admin.csrf = r.json["csrf"]
        cls.owner = Client(cls.ts)
        assert cls.owner.register("Update Repair", "upd-owner@example.com").status == 200
        cls.cfg = cls.ts.config
        cls.updater_file = pathlib.Path(cls.ts.tmp) / "fake-updater"
        cls.updater_file.write_text("#!/bin/sh\n")

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def setUp(self):
        self.calls = []
        self.answer = release("v99.0.0")
        checker = self.ts.app.update_checker
        checker.forget()
        checker.fetch = self._fetch
        self.cfg.updater_path = self.updater_file
        for p in (self.cfg.update_request_path, self.cfg.update_status_path):
            if p.exists():
                p.unlink()

    def _fetch(self, repo):
        self.calls.append(repo)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    # ---------------------------------------------------------------- access
    def test_version_is_public_and_healthz_reports_it(self):
        self.assertEqual(Client(self.ts).get("/api/version").json, {"version": __version__})
        self.assertEqual(Client(self.ts).get("/healthz").json["version"], __version__)

    def test_only_platform_admin_can_use_update_routes(self):
        for method, path in (("GET", "/api/update/check"), ("GET", "/api/update/status"), ("POST", "/api/update/apply")):
            anon = Client(self.ts).request(method, path, {} if method == "POST" else None)
            self.assertEqual(anon.status, 401, path)
            shop_owner = self.owner.request(method, path, {} if method == "POST" else None)     # a shop OWNER is not enough
            self.assertEqual(shop_owner.status, 401, path)
        self.assertEqual(self.calls, [])

    def test_apply_needs_csrf_token(self):
        r = self.admin.post("/api/update/apply", use_csrf=False)
        self.assertEqual(r.status, 403)
        self.assertFalse(self.cfg.update_request_path.exists())

    # ---------------------------------------------------------------- check
    def test_check_reports_newer_release(self):
        r = self.admin.get("/api/update/check")
        self.assertEqual(r.status, 200)
        j = r.json
        self.assertEqual((j["current"], j["latest"], j["tag"]), (__version__, "99.0.0", "v99.0.0"))
        self.assertTrue(j["updateAvailable"] and j["installable"] and j["updaterInstalled"])
        self.assertEqual(j["notes"], "Fixes and improvements")
        self.assertEqual(j["publishedAt"], "2030-01-01T00:00:00Z")
        self.assertEqual(self.calls, [self.cfg.update_repo])        # the repo comes from config only

    def test_check_ignores_any_client_supplied_repo_or_url(self):
        self.admin.get("/api/update/check?repo=evil/evil&url=http://evil.example/x")
        self.assertEqual(self.calls, [self.cfg.update_repo])

    def test_same_or_older_release_is_not_an_update(self):
        for tag in ("v" + __version__, "v0.0.1"):
            self.ts.app.update_checker.forget()
            self.answer = release(tag)
            self.assertFalse(self.admin.get("/api/update/check").json["updateAvailable"], tag)

    def test_result_is_cached_for_ten_minutes(self):
        self.admin.get("/api/update/check")
        second = self.admin.get("/api/update/check").json
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(second["cached"])

    def test_failure_without_cache_is_a_clean_502(self):
        self.answer = UpdateError("Could not reach GitHub. Check the server's internet connection.")
        r = self.admin.get("/api/update/check")
        self.assertEqual(r.status, 502)
        self.assertIn("GitHub", r.json["error"])

    def test_failure_with_cache_serves_last_good_result(self):
        self.admin.get("/api/update/check")
        self.ts.app.update_checker._cached = (time.monotonic() - 10_000, self.ts.app.update_checker._cached[1])   # expire it
        self.answer = UpdateError("GitHub is rate limiting update checks. Try again in a few minutes.")
        r = self.admin.get("/api/update/check")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.json["stale"])
        self.assertIn("rate limiting", r.json["warning"])

    def test_non_version_tag_is_rejected(self):
        self.answer = release("nightly")
        self.assertEqual(self.admin.get("/api/update/check").status, 502)

    def test_release_without_files_is_not_installable(self):
        self.answer = release("v99.0.0", assets=False)
        self.assertFalse(self.admin.get("/api/update/check").json["installable"])
        r = self.admin.post("/api/update/apply")
        self.assertEqual(r.status, 409)
        self.assertFalse(self.cfg.update_request_path.exists())

    # ---------------------------------------------------------------- apply / status
    def test_apply_queues_request_for_the_checked_version_only(self):
        r = self.admin.post("/api/update/apply", {"tag": "v1.0.0", "url": "http://evil.example/x.tgz", "cmd": "id"})
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json["target"], "99.0.0")
        self.assertEqual(self.cfg.update_request_path.read_text().strip(), "v99.0.0")   # body was ignored entirely
        st = self.admin.get("/api/update/status").json
        self.assertEqual(st["state"], "queued")
        self.assertTrue(st["running"])
        db = self.ts.platform_db()
        actions = [row[0] for row in db.execute("SELECT action FROM platform_audit")]
        db.close()
        self.assertIn("update.requested", actions)

    def test_second_apply_is_rejected_while_one_is_pending(self):
        self.assertEqual(self.admin.post("/api/update/apply").status, 200)
        self.assertEqual(self.admin.post("/api/update/apply").status, 409)

    def test_apply_refused_when_already_up_to_date(self):
        self.answer = release("v" + __version__)
        self.assertEqual(self.admin.post("/api/update/apply").status, 409)

    def test_apply_refused_when_updater_not_installed(self):
        self.cfg.updater_path = pathlib.Path(self.ts.tmp) / "does-not-exist"
        r = self.admin.post("/api/update/apply")
        self.assertEqual(r.status, 409)
        self.assertIn("not installed", r.json["error"])
        self.assertFalse(self.cfg.update_request_path.exists())

    def test_status_defaults_to_idle(self):
        self.assertEqual(self.admin.get("/api/update/status").json["state"], "idle")

    def test_status_shows_what_the_updater_wrote(self):
        self.cfg.update_status_path.write_text(json.dumps({
            "state": "failed", "step": "rollback", "message": "rolled back", "error": "health check timed out",
            "rolledBack": True, "evil": "<script>"}))
        st = self.admin.get("/api/update/status").json
        self.assertEqual((st["state"], st["rolledBack"], st["error"]), ("failed", True, "health check timed out"))
        self.assertNotIn("evil", st)                                  # unknown keys are dropped
        self.assertFalse(st["running"])

    def test_corrupt_status_file_is_treated_as_idle(self):
        self.cfg.update_status_path.write_text("{not json")
        self.assertEqual(self.admin.get("/api/update/status").json["state"], "idle")
        self.cfg.update_status_path.write_text(json.dumps({"state": "exploded"}))
        self.assertEqual(self.admin.get("/api/update/status").json["state"], "idle")

    def test_request_nobody_picked_up_is_reported_and_cleared(self):
        self.assertEqual(self.admin.post("/api/update/apply").status, 200)
        stale = json.loads(self.cfg.update_status_path.read_text())
        stale["queuedAt"] = time.time() - 600
        self.cfg.update_status_path.write_text(json.dumps(stale))
        st = self.admin.get("/api/update/status").json
        self.assertEqual(st["state"], "failed")
        self.assertIn("did not start", st["error"])
        self.assertFalse(self.cfg.update_request_path.exists())
        self.assertEqual(self.admin.post("/api/update/apply").status, 200)       # can try again


class UpdaterScriptStatic(unittest.TestCase):
    """Cheap guards on the root script and installer (they cannot be run here)."""

    ROOT = pathlib.Path(__file__).resolve().parent.parent

    def test_updater_script_compiles_and_never_uses_a_shell(self):
        src = (self.ROOT / "install" / "shopdesk-update").read_text(encoding="utf-8")
        compile(src, "shopdesk-update", "exec")
        self.assertNotIn("shell=True", src)
        self.assertNotIn("os.system", src)
        self.assertNotIn("os.popen", src)

    def test_installer_installs_path_unit_and_no_sudo(self):
        sh = (self.ROOT / "install" / "shopdesk.sh").read_text(encoding="utf-8")
        self.assertIn("PathExists=/var/lib/shopdesk/update-request", sh)
        self.assertIn("shopdesk-update.path", sh)
        self.assertNotIn("sudoers", sh)
        self.assertIn("NoNewPrivileges=true", sh)         # the app service keeps its hardening


if __name__ == "__main__":
    unittest.main()
