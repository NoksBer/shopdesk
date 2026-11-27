-- Temporary passwords issued by the platform administrator. The temporary password itself is stored hashed in
-- users.password_hash like any other password; these columns only say it is temporary, when it expires (epoch seconds)
-- and whether it has already been used to sign in. Additive only: existing rows get the defaults.
ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN temp_password_expires_at INTEGER;
ALTER TABLE users ADD COLUMN temp_password_used INTEGER NOT NULL DEFAULT 0;
