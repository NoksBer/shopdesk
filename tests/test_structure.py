"""Static guards and migration behavior."""
import pathlib
import re
import sqlite3
import tempfile
import unittest

from harness import TestServer
from shopdesk.app import App
from shopdesk.db import connect, migrate

PKG = pathlib.Path(__file__).resolve().parent.parent / "shopdesk"

# execute( f"..."  |  execute("..." % x)  |  execute("..." + x)  |  execute(name + ...)
UNSAFE_SQL = re.compile(r"""\.execute\(\s*(f["']|["'][^"']*["']\s*[%+]|[A-Za-z_][\w.\[\]"']*\s*\+)""")


class SqlSafety(unittest.TestCase):
    def test_no_string_built_sql(self):
        offenders = []
        for path in PKG.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for m in UNSAFE_SQL.finditer(text):
                offenders.append("%s: %s" % (path.name, m.group(0)))
        self.assertEqual(offenders, [])


class Migrations(unittest.TestCase):
    def test_repeatable_and_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            conn = connect(pathlib.Path(d) / "t.db")
            mdir = PKG / "migrations" / "tenant"
            migrate(conn, mdir)
            migrate(conn, mdir)
            count = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
            conn.close()
            self.assertEqual(count, len(list(mdir.glob("*.sql"))))

    def test_failed_migration_rolls_back(self):
        with tempfile.TemporaryDirectory() as d:
            mdir = pathlib.Path(d) / "m"
            mdir.mkdir()
            (mdir / "0001_bad.sql").write_text("CREATE TABLE a(x); CREATE TABLE a(y);")
            conn = connect(pathlib.Path(d) / "t.db")
            with self.assertRaises(sqlite3.DatabaseError):
                migrate(conn, mdir)
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE name='a'")]
            applied = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
            conn.close()
            self.assertEqual(tables, [])
            self.assertEqual(applied, 0)

    def test_restart_keeps_data(self):
        ts = TestServer()
        try:
            from harness import Client
            c = Client(ts)
            self.assertEqual(c.register("Persist Repair", "persist@example.com").status, 200)
            App(ts.config)       # simulates a restart: migrates again over the same data
            self.assertEqual(c.get("/api/business").json["name"], "Persist Repair")
        finally:
            ts.close()


class Routes(unittest.TestCase):
    def test_every_route_declares_access(self):
        ts = TestServer()
        try:
            for r in ts.app.router.routes:
                self.assertTrue(r.access, (r.method, r.path))
        finally:
            ts.close()


if __name__ == "__main__":
    unittest.main()
