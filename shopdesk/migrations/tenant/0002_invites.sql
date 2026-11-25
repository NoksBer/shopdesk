-- Staff invitations: single-use, expiring, token stored only as a keyed hash.
CREATE TABLE invites(
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  role_id TEXT NOT NULL REFERENCES roles(id),
  token_hash TEXT NOT NULL UNIQUE,
  invited_by TEXT REFERENCES users(id),
  created_at TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  used_at TEXT,
  revoked_at TEXT
);
CREATE INDEX idx_invites_email ON invites(email);
