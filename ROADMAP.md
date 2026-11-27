# ShopDesk implementation plan

Legend: [x] implemented AND tested (tests run and passing), [~] written, NOT yet run, [ ] not started.

## Phase 1 - Core application / MVP foundation

- [~] 1. Skeleton: config, router, HTTP layer, test harness
- [~] 2. Migration runner (platform + tenant DBs), initial schemas
- [~] 3. Auth: scrypt, sessions (hashed tokens), CSRF + Origin check, rate limiting, permission-checked routes
- [~] 4. Tenant provisioning, registration, shop-type profiles (terminology, statuses, equipment fields)
- [~] Minimal UI: register / sign in / business profile / team list
- [~] Platform admin: CLI `create-admin`, sign in, list / suspend / reactivate tenants (metadata only)
- [~] 5. Staff invites (single-use 7-day link, token hashed at rest), role changes, deactivate (kills sessions). Invite links are
      copied by the owner; emailing them arrives with Phase 2 email. Password reset flow is NOT done yet.
- [~] 6. Customers and equipment: search, archive/restore, profile-driven equipment fields (owner can reconfigure via
      `PUT /api/equipment/fields`; there is no UI for that yet), audit without customer PII. Photos wait for Phase 2 uploads.
- [~] 7. Tickets: create/edit/search/filter, configurable statuses (editor in Settings), priority, due date, multi-assignee,
      append-only history, configurable numbering, per-technician visibility (`tickets.read_all`). Tickets cannot be deleted
      (use the Cancelled status). Due dates compare against the server's UTC date, not the shop's timezone yet.
- [~] 8. Activity log screen (filter by area and person, paging). Only users with `audit.read` (owner, manager) see it.
- [~] 9. Dashboard: open / overdue / due in 7 days / unassigned / closed in 7 days, open by status, my open tickets,
      recent activity. Technicians see only their own assigned tickets. "Today" is the server's UTC date.
- [~] v3-a. Fix: customer search in the new-ticket form (array passed to `replaceChildren` produced no options). Replaced by a
      type-ahead customer picker also used for invoices; `#/tickets/new?customer=<id>` pre-selects.
- [~] v3-b. Kanban Queue (shown as "Queue"; the URL is still `#/board`): one column per status, cards show priority and due date, drag with mouse or touch handle,
      arrow buttons, quick priority change, filters. Closed tickets stay 14 days. `GET /api/board`, `POST /api/tickets/<id>/priority`.
- [~] v3-c. Calendar (`#/calendar`): month grid of ticket due dates with a day agenda. `GET /api/calendar`.
- [~] v3-d. Services / pricing catalogue (`#/services`).
- [~] v3-e. Inventory (`#/inventory`): cost, sale price, stock, reorder level, append-only stock movements, adjust stock.
      Cost is visible only with `inventory.write`.
- [~] v3-f. Invoices (`#/invoices`): drafts with service / part / custom lines, tax, notes; issue (assigns number, locks lines,
      takes parts out of stock), mark paid, void (restores stock), print. Invoicing settings (prefix, tax, terms, footer) in Settings.
      Not included: partial payments, card payments, emailing invoices, credit notes, discounts as negative lines.
- [~] v4-a. Dashboard revenue: this month / last month / year to date / outstanding, 12-month bar chart, latest payments.
      Paid invoices only, bucketed by UTC payment month; visible with `invoices.read`.
- [~] v4-b. Queue uses the full screen width: every status fits side by side (no horizontal scrolling on a normal monitor).
- [~] v4-c. White labelling (Settings > Branding): brand colour, top-bar colour, logo (resized to PNG in the browser, stored in the
      tenant database, served only to signed-in users), tagline, show/hide name. Applied across the app.
- [~] v4-d. Redesigned invoice print-out with logo, brand colour bands, tagline, PAID / VOID stamps.
- [~] v5-a. Inventory SKUs: every part gets the next number automatically (1, 2, 3 ...), per business, from a forward-only counter
      taken in the same transaction as the insert. Numbers are never reused (deactivating or selling out keeps the number), cannot be
      edited, and items cannot be deleted (database triggers). The old free-text SKU is now "supplier / manufacturer part number".
      Existing parts were numbered in creation order by migration 0007.
- [~] v5-b. Label printing from Inventory: per part or for a selection, copies or one per unit in stock, three sizes, sheet or
      label-printer layout, optional Code 39 barcode and price. Typing/scanning a SKU in the invoice part search and pressing Enter adds it.
      Barcode scanning has not been tested with real scanners or label printers.
- [~] v6-a. Self-service sign-up: public "Create your shop" tab next to "Sign in" (default sign-up is now open; env
      `SHOPDESK_OPEN_SIGNUP=0` closes it). Each business has its own database file. No email verification yet.
- [~] v6-b. Cash ledger (`#/ledger`, dashboard Cash flow card): append-only, running balance, positive/negative reading, filters,
      CSV export, manual entries, reversals. Auto entries: stock purchases (SKU) and paid invoices (invoice number); voiding a paid
      invoice reverses its line. Existing paid invoices and opening stock are backfilled by migration 0008.
- [~] v6-c. Time tracking: clock in/out on tickets (one running timer per person, switching stops the other), header clock, manual
      entries, edit/remove, per-ticket totals, Time page, "Clocked in now", timers stop when a ticket closes.
- [~] v6.1. In-app software updates (platform administrator only, `/admin.html`): VERSION file, `GET /api/version`, release check
      against the hardcoded GitHub repo (cached 10 min), "Update now" drops a request file that a root systemd `.path` unit turns
      into `shopdesk-update` (SHA-256 verified release tarball, backup, swap, restart, health check, automatic rollback).
      Installer: `install-updater` mode for existing containers. See RELEASING.md. Never run on a real container.
- [~] v0.0.12. Customer touch bases (migration 0010): optional popup on completing a ticket, calendar + dashboard + ticket note,
      reschedule/cancel, hourly in-app job that creates the follow-up ticket (idempotent, catches up). Dashboard "Money out" tile red.
- [~] v0.0.13. Ticket page tidy-up (details + edit in one box, notes first, Assign-to popup), manual calendar items
      (migration 0011, `/api/calendar-items`), full-width dashboard grid.
- [~] v0.1.0. Platform admin Shops console (platform migration 0002; tenant 0012 temporary passwords), CSV data import (tenant 0013, permission `data.import`),
      full-width Tickets table with side panel, tickets status-filter fix, invoice-to-ticket link, customer history with invoices.
- [~] v0.1.1. Shop type 'Auto tinting / window film' as profile data (vehicle fields, statuses, starter services at 0.00, touch base presets), seeded once at shop creation.
- [ ] 10. Tenant-isolation tests for every new route (existing suite covers steps 1-4 routes)
- [~] 11. Installer (`install/shopdesk.sh`): written, never run on Proxmox
- [ ] 12. Restore test on a clean container

## Status note

No Python interpreter was available on the development PC, so NOTHING in this repo has been executed.
First action on any machine with Python 3.11+: `python -m unittest discover -s tests -v`, fix failures, then flip [~] to [x].

## Later phases

See `DESIGN.md` and the product brief (phases 2 to 5).
