-- Work orders. Technicians without tickets.read_all only see tickets assigned to them.
INSERT OR IGNORE INTO role_permissions(role_id, permission)
  SELECT id, 'tickets.read_all' FROM roles WHERE name IN ('owner', 'manager', 'front_desk');

CREATE TABLE ticket_numbering(
  id INTEGER PRIMARY KEY CHECK(id = 1),
  prefix TEXT NOT NULL DEFAULT '',
  next_number INTEGER NOT NULL DEFAULT 1001
);
INSERT INTO ticket_numbering(id, prefix, next_number) VALUES(1, '', 1001);

CREATE TABLE tickets(
  id TEXT PRIMARY KEY,
  number TEXT NOT NULL UNIQUE,
  customer_id TEXT NOT NULL REFERENCES customers(id),
  status_id TEXT NOT NULL REFERENCES ticket_statuses(id),
  title TEXT NOT NULL,
  priority TEXT NOT NULL DEFAULT 'normal' CHECK(priority IN ('low','normal','high','urgent')),
  due_date TEXT,
  symptoms TEXT NOT NULL DEFAULT '',
  intake_notes TEXT NOT NULL DEFAULT '',
  created_by TEXT REFERENCES users(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  closed_at TEXT,
  closed_by TEXT REFERENCES users(id)
);
CREATE INDEX idx_tickets_status ON tickets(status_id);
CREATE INDEX idx_tickets_customer ON tickets(customer_id);
CREATE INDEX idx_tickets_created ON tickets(created_at);

CREATE TABLE ticket_equipment(
  ticket_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  equipment_id TEXT NOT NULL REFERENCES equipment(id),
  PRIMARY KEY(ticket_id, equipment_id)
);

CREATE TABLE ticket_assignees(
  ticket_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(id),
  assigned_at TEXT NOT NULL,
  assigned_by TEXT REFERENCES users(id),
  PRIMARY KEY(ticket_id, user_id)
);
CREATE INDEX idx_ticket_assignees_user ON ticket_assignees(user_id);

-- Append-only timeline: created, status changes, assignment, edits.
CREATE TABLE ticket_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id TEXT NOT NULL REFERENCES tickets(id),
  ts TEXT NOT NULL,
  user_id TEXT REFERENCES users(id),
  kind TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_ticket_history_ticket ON ticket_history(ticket_id, id);
CREATE TRIGGER ticket_history_no_update BEFORE UPDATE ON ticket_history
BEGIN SELECT RAISE(ABORT, 'ticket_history is append-only'); END;
CREATE TRIGGER ticket_history_no_delete BEFORE DELETE ON ticket_history
BEGIN SELECT RAISE(ABORT, 'ticket_history is append-only'); END;
