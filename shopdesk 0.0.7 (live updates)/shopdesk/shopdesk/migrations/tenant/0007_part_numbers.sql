-- Every inventory item gets a system-assigned SKU: 1, 2, 3 ... in order. Numbers are never reused, even when a part
-- is deactivated or its stock reaches zero, because the counter below only ever moves forward.
ALTER TABLE inventory_items ADD COLUMN part_no INTEGER;

-- Existing items are numbered in the order they were created.
UPDATE inventory_items SET part_no = (
  SELECT COUNT(*) FROM inventory_items b
  WHERE b.created_at < inventory_items.created_at
     OR (b.created_at = inventory_items.created_at AND b.id <= inventory_items.id));

CREATE UNIQUE INDEX idx_inventory_part_no ON inventory_items(part_no);

CREATE TABLE inventory_counter(
  id INTEGER PRIMARY KEY CHECK(id = 1),
  next_number INTEGER NOT NULL CHECK(next_number >= 1)
);
INSERT INTO inventory_counter(id, next_number) SELECT 1, COALESCE(MAX(part_no), 0) + 1 FROM inventory_items;

-- A SKU can never change and an item can never be removed (deactivate it instead).
CREATE TRIGGER inventory_part_no_immutable BEFORE UPDATE OF part_no ON inventory_items
WHEN OLD.part_no IS NOT NULL AND NEW.part_no IS NOT OLD.part_no
BEGIN SELECT RAISE(ABORT, 'part_no cannot change'); END;
CREATE TRIGGER inventory_items_no_delete BEFORE DELETE ON inventory_items
BEGIN SELECT RAISE(ABORT, 'inventory items cannot be deleted; deactivate them instead'); END;
