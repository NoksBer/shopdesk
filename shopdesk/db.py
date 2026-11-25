"""SQLite helpers: connections, explicit transactions, and the versioned migration runner.

All SQL in this project uses bound parameters. Never build SQL with string formatting.
"""
import sqlite3
from contextlib import contextmanager

from .util import now_iso


def connect(path):
    # isolation_level=None -> autocommit; transactions are explicit via tx().
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def tx(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def migrate(conn, directory):
    """Apply NNNN_name.sql files in order, each in its own transaction. Safe to run repeatedly."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
    for path in sorted(directory.glob("*.sql")):
        version = int(path.name.split("_", 1)[0])
        if version in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        try:
            conn.executescript("BEGIN IMMEDIATE;\n" + sql)
            conn.execute(
                "INSERT INTO schema_version(version, name, applied_at) VALUES(?,?,?)",
                (version, path.name, now_iso()),
            )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
