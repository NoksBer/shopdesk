-- CSV data import (customers, inventory items, services): permission and import history. Additive only.
INSERT OR IGNORE INTO role_permissions(role_id, permission)
  SELECT id, 'data.import' FROM roles WHERE name IN ('owner', 'manager');

CREATE TABLE import_history(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  filename TEXT NOT NULL DEFAULT '',
  mode TEXT NOT NULL,
  total_rows INTEGER NOT NULL DEFAULT 0,
  created INTEGER NOT NULL DEFAULT 0,
  updated INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'running',
  created_by TEXT REFERENCES users(id),
  created_at TEXT NOT NULL,
  finished_at TEXT
);
