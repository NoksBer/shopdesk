-- Manual calendar items (reminders, appointments) that are not tickets. This migration only ADDS a table; existing data is
-- untouched and no permissions change (seeing follows tickets.read, creating/editing follows tickets.write).
CREATE TABLE calendar_items(
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  item_date TEXT NOT NULL,                 -- plain date, YYYY-MM-DD
  item_time TEXT NOT NULL DEFAULT '',      -- optional HH:MM (24 hour); empty = all day
  notes TEXT NOT NULL DEFAULT '',
  created_by TEXT REFERENCES users(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_calendar_items_date ON calendar_items(item_date);
