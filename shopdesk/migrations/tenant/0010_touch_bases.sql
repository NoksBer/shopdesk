-- Customer touch bases: an optional reminder, scheduled when a ticket is completed, that turns into a new low-priority
-- ticket on its due date. This migration only ADDS a table and indexes; existing data is untouched and no permissions
-- change (scheduling follows tickets.write, seeing follows ticket visibility).
CREATE TABLE touch_bases(
  id TEXT PRIMARY KEY,
  ticket_id TEXT NOT NULL REFERENCES tickets(id),          -- the ticket whose completion started this touch base
  customer_id TEXT NOT NULL REFERENCES customers(id),
  due_date TEXT NOT NULL,                                  -- plain date, YYYY-MM-DD (server UTC "today" is the reference)
  notes TEXT NOT NULL DEFAULT '',                          -- "touch base on ..."
  status TEXT NOT NULL DEFAULT 'scheduled' CHECK(status IN ('scheduled','created','cancelled')),
  created_ticket_id TEXT REFERENCES tickets(id),           -- the follow-up ticket, once created
  scheduled_by TEXT REFERENCES users(id),
  scheduled_at TEXT NOT NULL
);
CREATE INDEX idx_touch_bases_due ON touch_bases(status, due_date);
CREATE INDEX idx_touch_bases_ticket ON touch_bases(ticket_id, status);
-- At most one active touch base per ticket, and one follow-up ticket per touch base (backstops for the job's idempotency).
CREATE UNIQUE INDEX idx_touch_bases_one_active ON touch_bases(ticket_id) WHERE status = 'scheduled';
CREATE UNIQUE INDEX idx_touch_bases_created_ticket ON touch_bases(created_ticket_id) WHERE created_ticket_id IS NOT NULL;
