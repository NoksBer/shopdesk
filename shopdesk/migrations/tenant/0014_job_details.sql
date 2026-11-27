-- Optional job details used by shop types that turn them on (auto tinting): where the job takes place and a due time.
-- Additive only: existing tickets get empty values, and shops that do not use them never see the fields.
ALTER TABLE tickets ADD COLUMN job_location TEXT NOT NULL DEFAULT '';
ALTER TABLE tickets ADD COLUMN due_time TEXT NOT NULL DEFAULT '';
