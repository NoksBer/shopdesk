-- Cash ledger and ticket time tracking.

INSERT OR IGNORE INTO role_permissions(role_id, permission)
  SELECT r.id, p.permission FROM roles r,
    (SELECT 'ledger.read' AS permission UNION ALL SELECT 'ledger.write' UNION ALL
     SELECT 'time.write' UNION ALL SELECT 'time.manage') p
  WHERE r.name IN ('owner', 'manager');
INSERT OR IGNORE INTO role_permissions(role_id, permission)
  SELECT id, 'time.write' FROM roles WHERE name = 'technician';

-- One row per money movement. Positive = money in, negative = money out. Rows are never edited or deleted:
-- a mistake is corrected by a reversing row that points back at the original (reverses_id).
CREATE TABLE ledger_entries(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_date TEXT NOT NULL,
  ts TEXT NOT NULL,
  amount_cents INTEGER NOT NULL CHECK(amount_cents <> 0),
  category TEXT NOT NULL,
  description TEXT NOT NULL,
  ref_type TEXT NOT NULL DEFAULT '',
  ref_id TEXT NOT NULL DEFAULT '',
  ref_label TEXT NOT NULL DEFAULT '',
  reverses_id INTEGER UNIQUE REFERENCES ledger_entries(id),
  user_id TEXT REFERENCES users(id)
);
CREATE INDEX idx_ledger_date ON ledger_entries(entry_date, id);
CREATE INDEX idx_ledger_ref ON ledger_entries(ref_type, ref_id);

-- History for businesses that already have data: invoices that are already paid, and the opening stock of parts
-- (valued at the part's current cost, which is the best information available).
INSERT INTO ledger_entries(entry_date, ts, amount_cents, category, description, ref_type, ref_id, ref_label)
  SELECT substr(i.paid_at, 1, 10), i.paid_at, i.total_cents, 'invoice_payment',
         'Invoice ' || i.number || ' paid' || CASE WHEN i.payment_method <> '' THEN ' (' || i.payment_method || ')' ELSE '' END,
         'invoice', i.id, i.number
  FROM invoices i WHERE i.status = 'paid' AND i.paid_at IS NOT NULL AND i.total_cents > 0 AND i.number IS NOT NULL;
INSERT INTO ledger_entries(entry_date, ts, amount_cents, category, description, ref_type, ref_id, ref_label, user_id)
  SELECT substr(m.ts, 1, 10), m.ts, -(m.delta * it.cost_cents), 'inventory_purchase',
         'Stock purchase: SKU ' || COALESCE(it.part_no, 0) || ' · ' || it.name || ' × ' || m.delta || ' (valued at current cost)',
         'inventory', it.id, 'SKU ' || COALESCE(it.part_no, 0), m.user_id
  FROM inventory_movements m JOIN inventory_items it ON it.id = m.item_id
  WHERE m.reason = 'opening stock' AND m.delta > 0 AND it.cost_cents > 0;

CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger_entries
BEGIN SELECT RAISE(ABORT, 'ledger_entries is append-only'); END;
CREATE TRIGGER ledger_no_delete BEFORE DELETE ON ledger_entries
BEGIN SELECT RAISE(ABORT, 'ledger_entries is append-only'); END;

-- Time on tickets. A running timer has source='timer' and no ended_at; each person can have only one running.
CREATE TABLE time_entries(
  id TEXT PRIMARY KEY,
  ticket_id TEXT NOT NULL REFERENCES tickets(id),
  user_id TEXT NOT NULL REFERENCES users(id),
  work_date TEXT NOT NULL,
  started_at TEXT,
  ended_at TEXT,
  seconds INTEGER NOT NULL DEFAULT 0 CHECK(seconds >= 0),
  note TEXT NOT NULL DEFAULT '',
  billable INTEGER NOT NULL DEFAULT 1,
  source TEXT NOT NULL CHECK(source IN ('timer', 'manual')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_time_running ON time_entries(user_id) WHERE source = 'timer' AND ended_at IS NULL;
CREATE INDEX idx_time_ticket ON time_entries(ticket_id, work_date);
CREATE INDEX idx_time_user_date ON time_entries(user_id, work_date);
