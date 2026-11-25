-- Services catalogue, parts inventory and invoices. Money is stored as integer cents everywhere.

INSERT OR IGNORE INTO role_permissions(role_id, permission)
  SELECT r.id, p.permission FROM roles r,
    (SELECT 'services.read' AS permission UNION ALL SELECT 'services.write' UNION ALL
     SELECT 'inventory.read' UNION ALL SELECT 'inventory.write' UNION ALL
     SELECT 'invoices.read' UNION ALL SELECT 'invoices.write') p
  WHERE r.name IN ('owner', 'manager');
INSERT OR IGNORE INTO role_permissions(role_id, permission)
  SELECT r.id, p.permission FROM roles r,
    (SELECT 'services.read' AS permission UNION ALL SELECT 'inventory.read' UNION ALL
     SELECT 'invoices.read' UNION ALL SELECT 'invoices.write') p
  WHERE r.name = 'front_desk';
INSERT OR IGNORE INTO role_permissions(role_id, permission)
  SELECT r.id, p.permission FROM roles r,
    (SELECT 'services.read' AS permission UNION ALL SELECT 'inventory.read') p
  WHERE r.name = 'technician';

CREATE TABLE services(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  price_cents INTEGER NOT NULL CHECK(price_cents >= 0),
  taxable INTEGER NOT NULL DEFAULT 1,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_services_name ON services(name COLLATE NOCASE);

CREATE TABLE inventory_items(
  id TEXT PRIMARY KEY,
  sku TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  cost_cents INTEGER NOT NULL DEFAULT 0 CHECK(cost_cents >= 0),
  price_cents INTEGER NOT NULL CHECK(price_cents >= 0),
  quantity INTEGER NOT NULL DEFAULT 0 CHECK(quantity >= 0),
  reorder_level INTEGER NOT NULL DEFAULT 0 CHECK(reorder_level >= 0),
  taxable INTEGER NOT NULL DEFAULT 1,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_inventory_sku ON inventory_items(sku COLLATE NOCASE) WHERE sku <> '';
CREATE INDEX idx_inventory_name ON inventory_items(name COLLATE NOCASE);

-- Every stock change is recorded here and the table is append-only.
CREATE TABLE inventory_movements(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id TEXT NOT NULL REFERENCES inventory_items(id),
  ts TEXT NOT NULL,
  user_id TEXT REFERENCES users(id),
  delta INTEGER NOT NULL,
  reason TEXT NOT NULL,
  ref TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_inventory_movements_item ON inventory_movements(item_id, id);
CREATE TRIGGER inventory_movements_no_update BEFORE UPDATE ON inventory_movements
BEGIN SELECT RAISE(ABORT, 'inventory_movements is append-only'); END;
CREATE TRIGGER inventory_movements_no_delete BEFORE DELETE ON inventory_movements
BEGIN SELECT RAISE(ABORT, 'inventory_movements is append-only'); END;

CREATE TABLE invoice_settings(
  id INTEGER PRIMARY KEY CHECK(id = 1),
  prefix TEXT NOT NULL DEFAULT 'INV-',
  next_number INTEGER NOT NULL DEFAULT 1001,
  default_tax_bp INTEGER NOT NULL DEFAULT 0 CHECK(default_tax_bp BETWEEN 0 AND 10000),
  terms_days INTEGER NOT NULL DEFAULT 14 CHECK(terms_days BETWEEN 0 AND 365),
  footer TEXT NOT NULL DEFAULT 'Thank you for your business.'
);
INSERT INTO invoice_settings(id) VALUES(1);

-- Drafts have no number: numbers are assigned when an invoice is issued, so they never have gaps.
CREATE TABLE invoices(
  id TEXT PRIMARY KEY,
  number TEXT UNIQUE,
  customer_id TEXT NOT NULL REFERENCES customers(id),
  ticket_id TEXT REFERENCES tickets(id),
  status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','issued','paid','void')),
  issue_date TEXT,
  due_date TEXT,
  notes TEXT NOT NULL DEFAULT '',
  tax_bp INTEGER NOT NULL DEFAULT 0 CHECK(tax_bp BETWEEN 0 AND 10000),
  subtotal_cents INTEGER NOT NULL DEFAULT 0,
  tax_cents INTEGER NOT NULL DEFAULT 0,
  total_cents INTEGER NOT NULL DEFAULT 0,
  bill_to TEXT NOT NULL DEFAULT '{}',
  paid_at TEXT,
  payment_method TEXT NOT NULL DEFAULT '',
  void_reason TEXT NOT NULL DEFAULT '',
  created_by TEXT REFERENCES users(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_invoices_customer ON invoices(customer_id);
CREATE INDEX idx_invoices_ticket ON invoices(ticket_id);
CREATE INDEX idx_invoices_status ON invoices(status);

CREATE TABLE invoice_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  invoice_id TEXT NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('manual','service','inventory')),
  ref_id TEXT,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  qty_x100 INTEGER NOT NULL CHECK(qty_x100 > 0),
  unit_cents INTEGER NOT NULL CHECK(unit_cents >= 0),
  unit_cost_cents INTEGER NOT NULL DEFAULT 0,
  taxable INTEGER NOT NULL DEFAULT 1,
  line_cents INTEGER NOT NULL
);
CREATE INDEX idx_invoice_items_invoice ON invoice_items(invoice_id, position);
