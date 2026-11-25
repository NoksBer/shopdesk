-- Platform database: tenant registry and platform admins only. No business data lives here.
CREATE TABLE tenants(
  id TEXT PRIMARY KEY CHECK(length(id) = 16),
  name TEXT NOT NULL,
  slug TEXT NOT NULL UNIQUE,
  shop_type TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('provisioning','active','suspended')),
  created_at TEXT NOT NULL
);

-- Maps a staff login email to exactly one tenant (an email can belong to one business).
CREATE TABLE staff_identities(
  email TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  user_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE platform_admins(
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  disabled INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE platform_sessions(
  token_hash TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL REFERENCES platform_admins(id) ON DELETE CASCADE,
  csrf TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  last_seen INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);

CREATE TABLE platform_audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  actor TEXT,
  action TEXT NOT NULL,
  tenant_id TEXT,
  detail TEXT NOT NULL DEFAULT '{}'
);
CREATE TRIGGER platform_audit_no_update BEFORE UPDATE ON platform_audit
BEGIN SELECT RAISE(ABORT, 'platform_audit is append-only'); END;
CREATE TRIGGER platform_audit_no_delete BEFORE DELETE ON platform_audit
BEGIN SELECT RAISE(ABORT, 'platform_audit is append-only'); END;
