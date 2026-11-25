CREATE TABLE customers(
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL DEFAULT 'individual' CHECK(type IN ('individual','business')),
  name TEXT NOT NULL,
  contact_name TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL DEFAULT '',
  phone TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL DEFAULT '',
  contact_pref TEXT NOT NULL DEFAULT 'email' CHECK(contact_pref IN ('email','phone','text','none')),
  notes TEXT NOT NULL DEFAULT '',
  created_by TEXT REFERENCES users(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  archived_at TEXT
);
CREATE INDEX idx_customers_name ON customers(name COLLATE NOCASE);

-- Core columns are shared by every industry; industry-specific values live in `fields` (JSON),
-- validated against the shop's configurable `equipment_fields` setting.
CREATE TABLE equipment(
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL REFERENCES customers(id),
  name TEXT NOT NULL,
  serial_number TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  purchase_date TEXT,
  warranty_expires TEXT,
  fields TEXT NOT NULL DEFAULT '{}',
  created_by TEXT REFERENCES users(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  archived_at TEXT
);
CREATE INDEX idx_equipment_customer ON equipment(customer_id);
CREATE INDEX idx_equipment_serial ON equipment(serial_number);
