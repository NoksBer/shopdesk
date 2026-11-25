"""Append-only audit trail (the table rejects UPDATE and DELETE via triggers)."""
import json

from .util import now_iso


def audit(conn, user_id, action, entity=None, entity_id=None, detail=None, ip=None):
    """Record an event. Never put passwords, tokens or customer PII in `detail`."""
    conn.execute(
        "INSERT INTO audit_log(ts, user_id, action, entity, entity_id, detail, ip) VALUES(?,?,?,?,?,?,?)",
        (now_iso(), user_id, action, entity, entity_id, json.dumps(detail or {}, sort_keys=True), ip),
    )


def platform_audit(conn, actor, action, tenant_id=None, detail=None):
    conn.execute(
        "INSERT INTO platform_audit(ts, actor, action, tenant_id, detail) VALUES(?,?,?,?,?)",
        (now_iso(), actor, action, tenant_id, json.dumps(detail or {}, sort_keys=True)),
    )
