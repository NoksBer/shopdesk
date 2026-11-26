-- Ticket notes (a thread per ticket) with file / screenshot attachments stored in this shop's own database.
-- Notes are append-only. A file is uploaded first (note_id NULL = pending, deletable by its uploader), then attached
-- to a note; once attached it can no longer be changed or deleted. No permission changes: reading needs tickets.read,
-- writing needs tickets.write (and the ticket must be visible to the user).
CREATE TABLE ticket_notes(
  id TEXT PRIMARY KEY,
  ticket_id TEXT NOT NULL REFERENCES tickets(id),
  user_id TEXT REFERENCES users(id),
  body TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX idx_ticket_notes_ticket ON ticket_notes(ticket_id, created_at);
CREATE TRIGGER ticket_notes_no_update BEFORE UPDATE ON ticket_notes
BEGIN SELECT RAISE(ABORT, 'ticket_notes is append-only'); END;
CREATE TRIGGER ticket_notes_no_delete BEFORE DELETE ON ticket_notes
BEGIN SELECT RAISE(ABORT, 'ticket_notes is append-only'); END;

CREATE TABLE ticket_files(
  id TEXT PRIMARY KEY,
  ticket_id TEXT NOT NULL REFERENCES tickets(id),
  note_id TEXT REFERENCES ticket_notes(id),
  user_id TEXT REFERENCES users(id),
  filename TEXT NOT NULL,
  content_type TEXT NOT NULL,
  size INTEGER NOT NULL CHECK(size > 0),
  data BLOB NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_ticket_files_ticket ON ticket_files(ticket_id, note_id);
CREATE TRIGGER ticket_files_attached_no_update BEFORE UPDATE ON ticket_files WHEN OLD.note_id IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'attached files cannot be changed'); END;
CREATE TRIGGER ticket_files_attached_no_delete BEFORE DELETE ON ticket_files WHEN OLD.note_id IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'attached files cannot be deleted'); END;
