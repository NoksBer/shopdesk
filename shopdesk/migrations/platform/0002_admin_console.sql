-- Platform admin console: shops that were deleted by the administrator. The shop's files are MOVED to data/trash/ and kept
-- for 30 days (restorable); this table records where they went. Additive only.
CREATE TABLE tenant_trash(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL,
  name TEXT NOT NULL,
  owner_name TEXT NOT NULL DEFAULT '',
  owner_email TEXT NOT NULL DEFAULT '',
  trash_name TEXT NOT NULL,
  deleted_at TEXT NOT NULL,
  deleted_by TEXT,
  purge_after TEXT NOT NULL,
  restored_at TEXT,
  purged_at TEXT
);
CREATE UNIQUE INDEX idx_tenant_trash_open ON tenant_trash(tenant_id) WHERE restored_at IS NULL AND purged_at IS NULL;
