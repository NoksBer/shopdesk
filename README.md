# ShopDesk (NoksForge)

A self-hosted, multi-tenant repair shop manager. Python standard library only (3.11+). One SQLite database file per shop.

**Status: Phase 1 steps 1 to 4 are written but have NOT been run or tested yet** (no Python was available where they were written). See [ROADMAP.md](ROADMAP.md). [DESIGN.md](DESIGN.md) has the architecture and tenant-isolation strategy.

What exists: shop registration with a shop-type profile (computer, phone, bicycle, small engine, appliance, general, custom), staff sign-in with roles, business profile, team list, audit log, platform-admin tenant list/suspend, and a minimal responsive UI. Also: staff invites, customers and equipment (with shop-type-specific fields). Work tickets with configurable statuses, assignment and history. Technicians only see tickets assigned to them.

Version 3 adds: a drag-and-drop Kanban Queue of tickets (status columns, priority shown on every card), a due-date calendar, a services/pricing catalogue, parts inventory (cost, sale price, stock with an audit trail of every change), and invoices (add services, parts or custom lines; issue, print, mark paid, void). Issuing an invoice takes its parts out of stock; voiding puts them back. Existing businesses get the new permissions automatically on the next start; owners and managers get everything, front desk can create invoices, technicians can look up services and parts.

Version 6 adds: **self-service shop sign-up**, a **cash ledger** and **time tracking**. The ledger (top navigation, plus a Cash flow card on the dashboard) is a running record of money in and out with a running balance and a positive/negative cash-flow reading. Inventory purchases (new stock with a cost, or received stock when you tick "record as purchase") and paid invoices are written to it automatically, each with its date, amount, and SKU or invoice number. You can add other income and expenses by hand, filter by period, download a CSV, and correct mistakes with reversing entries (rows are never edited or deleted; voiding a paid invoice reverses its payment line). Time tracking adds Clock in / Clock out on every ticket with a live clock in the header, manual time entries, per-ticket totals, a Time page (timesheets for managers, "My time" for technicians), and a "Clocked in now" list on the manager dashboard. Closing a ticket stops any running timers on it. Ledger and time permissions are granted to owners and managers (technicians can track their own time).

**Software updates (v6.1):** the platform administrator (not shop owners) can open `/admin.html`, check GitHub Releases for a newer version and click **Update now**; the server backs up, installs the release, restarts and rolls back automatically if the new version does not come up. How to publish a release and how existing installs get the updater: [RELEASING.md](RELEASING.md). The current version is in the `VERSION` file.

**Ticket notes and attachments (v0.0.11):** every ticket has a Notes & attachments thread. Add text, paste a screenshot straight from the clipboard, drop files or use Attach (images, PDF, text/CSV, Word/Excel/PowerPoint, zip; up to 10 MB each, 10 per note). Files are stored in the shop's own database, so they are isolated per shop and included in backups. Notes and attached files cannot be edited or deleted afterwards (a file can be removed only before the note is added). Files are not virus-scanned; everything except images downloads as an attachment.

**Customer touch bases (v0.0.12):** when a ticket is moved to Completed / Closed (not Cancelled), an optional "Customer Touch Base" popup offers to schedule a follow-up in 30/60/90 days, 6 months, 1 year or on a chosen date (MM/DD/YYYY). Skip is the default and never blocks closing. Scheduled touch bases show on the Calendar (purple), on the dashboard ("Upcoming touch bases") and on the original ticket, where they can be rescheduled or cancelled. On the due date a background job inside the app (it runs at startup and about every hour, and catches up after downtime) creates one low-priority "Touch base: <customer> (follow-up to #<number>)" ticket in the first status, assigned to the original ticket's staff if any, and writes an audit entry. Dates use the server's UTC date. Customers that were archived in the meantime are skipped (the touch base is cancelled and audited); suspended shops are skipped until reactivated.

**Ticket page, calendar and dashboard clean-up (v0.0.13):** the ticket details box now shows customer, status, priority, due date, assignees, items, reported symptoms and intake notes as plain text, with "Edit ticket" inside the same box; Notes & attachments come right under it; "Assign to…" opens a popup with a technician dropdown. The Calendar has "+ Add calendar item" for manual reminders and appointments (amber). The dashboard uses the full screen width as a card grid, with the technician's own tickets first.

**Platform admin console, data import and Tickets page (v0.1.0):** `/admin.html` now has a **Shops** section for the platform administrator: every shop with owner, created date, last activity, users, tickets, database size and status, with search and sorting. It shows metadata only, never a shop's customers, tickets, invoices or notes. The administrator can suspend/restore a shop, issue an owner a one-time temporary password (shown once, valid 24 hours, must be changed at sign-in), and delete a shop (type its exact name; its files are moved to `data/trash/` and purged automatically after 30 days; "Recently deleted" can restore it). Every admin action is in the admin audit log on the page. **Settings > Import data** (owners and managers) imports customers, inventory items and services from CSV files, with column matching for common QuickBooks exports, a 20-row preview, duplicate handling (skip or update), an error report and history; invoices, payments, tickets, ledger entries and stock quantities are never imported. The **Tickets** page is now a full-width sortable table with a side panel; the "All statuses" filter bug is fixed. Invoices can be attached to (or removed from) an open ticket of the same customer, and a customer's Repair history shows each ticket's linked invoices to people who may view invoices.
**Auto tinting / window film shop type (v0.1.1):** a new choice on the Create your shop form. A new shop of this type starts with Vehicle records (year, make, model, color, license plate, optional VIN, plus film brand, film type, shade percentage and warranty term for the latest job), tinting ticket statuses (Completed / Closed offers the touch base popup; Cancelled / No-show does not), starter services at 0.00 (set your own prices) and touch base quick buttons (1 week, 6 month, 1 year). It is defined as data in `shopdesk/profiles.py`; starter data is created once when the shop is created, and existing shops and other shop types are unchanged. Not available: per-job window checkboxes (the vehicle record has no checkbox fields).

## Layout (same as Plateful)

```
install/shopdesk.sh   Proxmox helper script
server.py             entry point
shopdesk/             python package (app, auth, tenants, modules/, migrations/)
static/               frontend (index.html, app.js, style.css)
tests/                unittest suite
Dockerfile  DESIGN.md  ROADMAP.md  README.md
```

## Run locally

```bash
python3 server.py                            # http://127.0.0.1:8080, data in ./data
python3 server.py create-admin you@example.com   # optional platform administrator
python3 -m unittest discover -s tests -v     # run the tests
```

The sign-in page has two tabs: **Sign in** and **Create your shop**. Anyone can create their own shop: each business gets its own SQLite database file (`data/tenants/<id>/tenant.db`), so businesses are separate from each other, and the person who signs up becomes that shop's owner. Set `SHOPDESK_OPEN_SIGNUP=0` to close public sign-up (the very first business can always be created). Because sign-up is open to anyone who can reach the server, put it behind HTTPS and consider the per-IP limit below. There is no email verification or password reset yet.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `SHOPDESK_HOST` / `SHOPDESK_PORT` | `127.0.0.1` / `8080` | Bind address (the installer sets `0.0.0.0`) |
| `SHOPDESK_DATA_DIR` | `./data` | Databases and uploads (`platform.db`, `tenants/<id>/tenant.db`) |
| `SHOPDESK_SECRET` | generated file `data/.secret` | Key for session-token hashes |
| `SHOPDESK_TRUST_PROXY` | `0` | `1` only behind your HTTPS proxy/tunnel: trusts `X-Forwarded-Proto/For`, `CF-Connecting-IP` |
| `SHOPDESK_OPEN_SIGNUP` | `1` | `1` lets anyone create their own business; `0` closes sign-up |
| `SHOPDESK_SECURE_COOKIES` | `0` | Force the Secure cookie flag (it is automatic behind an HTTPS proxy) |
| `SHOPDESK_SESSION_IDLE_SECONDS` / `_MAX_SECONDS` | `43200` / `604800` | Session idle and absolute lifetime |
| `SHOPDESK_REGISTER_LIMIT` | `5` | Business registrations per IP per hour |

## Install on Proxmox (helper script)

Run on the **Proxmox node shell**. Creates a Debian 12 LXC (2 cores, 1 GB RAM, 8 GB disk), installs ShopDesk as a systemd service under a non-root `shopdesk` user, and prints the URL. **Not yet run on a real Proxmox host.**

**Option A: from GitHub.** Push this folder to a repo, then run:

```bash
SHOPDESK_REPO=youruser/shopdesk bash -c "$(curl -fsSL https://raw.githubusercontent.com/youruser/shopdesk/main/install/shopdesk.sh)"
```

**Option B: no GitHub.** Copy `shopdesk.tar.gz` and `install/shopdesk.sh` to the node, then run:

```bash
SHOPDESK_TARBALL=/root/shopdesk.tar.gz bash /root/shopdesk.sh
```

Other modes (take the container ID):

```bash
bash shopdesk.sh update <CTID>             # takes a pre-update backup, keeps data, keeps previous code in /opt/shopdesk.prev
bash shopdesk.sh backup <CTID>             # consistent SQLite backup + uploads -> /var/lib/vz/dump
bash shopdesk.sh restore <CTID> <file>     # replaces data; old data folder is kept inside the container
```

Overrides: `CTID`, `CT_NAME`, `CORES`, `RAM`, `DISK`, `BRIDGE`, `STORAGE`, `TEMPLATE_STORAGE`, `PORT`, `BRANCH`, `TRUST_PROXY`, `BACKUP_DIR`.

Data lives in `/var/lib/shopdesk`. Config and secrets are in `/etc/shopdesk/shopdesk.env` (mode 640). Do not expose the plain HTTP port; use Cloudflare Tunnel and re-run with `TRUST_PROXY=1`.

## Known limitations

- Ledger: it records cash movements only (invoice payments and stock purchases, plus manual entries); it is not full accounting. Invoice payments are recorded for the full invoice total (including tax). Parts already in inventory before this version were added to the ledger at their current cost, labelled "valued at current cost". Dates use the server's UTC date.
- Time tracking: hours are not billed automatically; there is no hourly rate or "add time to invoice" yet. A timer someone forgets to stop keeps running until they (or a manager) clock out or close the ticket; the dashboard flags timers running over 12 hours.

- Part labels: the barcode is Code 39 drawn by the browser. It has not been tried with real barcode scanners or label printers, so test one label before printing a batch. Choose your paper or label size in the browser's print dialog.

- Branding applies after sign-in only: the sign-in page itself shows the default ShopDesk look because the business isn't known yet. Logos are raster images (SVG is refused on purpose).
- Revenue months use the UTC date a payment was recorded, not the shop's timezone.

- Invoices: one tax rate per invoice, no partial payments, no emailing, no credit notes; "Mark paid" only records the date and method. Invoice and calendar dates use the server's date, not the shop's timezone.
- Dragging on the Queue page uses pointer events (mouse and touch). On phones, hold the ⠿ handle or use the arrow buttons on each card.

- An email address can belong to one business only (staff login is by email alone).
- Rate limits are in memory (reset on restart; single process).
- Staff are added with invite links the owner copies and sends by hand (email delivery comes in Phase 2). There is no password reset yet.
- Admin-created tenants are not built; new businesses come from the registration form.
