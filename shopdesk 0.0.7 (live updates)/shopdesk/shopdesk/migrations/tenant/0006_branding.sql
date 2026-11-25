-- White labelling: each business picks its own colours and logo. Empty colour = ShopDesk default.
CREATE TABLE branding(
  id INTEGER PRIMARY KEY CHECK(id = 1),
  primary_color TEXT NOT NULL DEFAULT '',
  header_color TEXT NOT NULL DEFAULT '',
  tagline TEXT NOT NULL DEFAULT '',
  show_name INTEGER NOT NULL DEFAULT 1,
  logo BLOB,
  logo_type TEXT NOT NULL DEFAULT '',
  logo_updated_at TEXT NOT NULL DEFAULT ''
);
INSERT INTO branding(id) VALUES(1);
