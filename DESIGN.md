# NoksForge ShopDesk — Foundational Design (for review)

Status: PROPOSAL. No application code has been written. Implementation starts after you approve or amend this.
Based on: `NoksForge_ShopDesk_Product_Brief.txt` v0.1, and the structure of your existing **Plateful** project
(stdlib-only Python, SQLite, static frontend, Proxmox helper script that builds a Debian LXC from a GitHub repo).

---

> Update: the layout below was adjusted to match Plateful (flat `server.py`, `static/`, `install/`, README at root; backup/restore live
> in `install/shopdesk.sh`, so there is no `scripts/` or `docs/` folder). `db/` is a single `shopdesk/db.py`. Platform routes are in
> `modules/platform_admin.py`. See README.md for the real layout.

## 1. Proposed architecture

Follow the Plateful pattern, with more structure because ShopDesk is larger and multi-tenant.

- **Backend:** Python 3.11+ (Debian 12 ships 3.11). Standard library HTTP server (`http.server` with `ThreadingHTTPServer`)
  behind a small in-house router. No framework. Rationale: matches Plateful, zero pip installs on the LXC, tiny attack surface.
  Trade-off: we write routing, cookies, CSRF and multipart parsing ourselves (about 600 lines, all tested).
  Alternative: FastAPI/Starlette in a venv. Faster to build, but adds dependencies the brief says to avoid.
- **Password hashing:** `hashlib.scrypt` (stdlib, modern, memory-hard). Argon2 would need a dependency.
- **DB:** SQLite in WAL mode, `foreign_keys=ON`, `busy_timeout`. All SQL goes through a thin data-access layer using parameterized queries only.
  Plain SQL files for migrations (`migrations/0001_*.sql`) tracked in a `schema_version` table. Keep SQL portable so a PostgreSQL move is feasible.
- **Frontend:** vanilla HTML/CSS/JS single-page app (like Plateful), mobile-first, bottom tab bar on phones. Separate small
  `/portal` app for customers (different cookie, different routes, different JS bundle).
- **PDF:** two options. (a) a small vendored pure-Python PDF writer for estimates/invoices/work orders (no dependency), or
  (b) `reportlab` in a venv. Recommendation: (a) for Phase 2, plus print-friendly HTML as a fallback.
- **Email:** SMTP via `smtplib` with a DB-backed outbox, retry with backoff, error log, per-tenant rate limit. Phase 2.
- **Deploy:** Debian 12 LXC, non-root `shopdesk` user, systemd unit with hardening (`NoNewPrivileges`, `ProtectSystem=strict`,
  `ReadWritePaths=/var/lib/shopdesk`), Cloudflare Tunnel for HTTPS, `/healthz` endpoint.

### Directory structure

```
shopdesk/
  install/shopdesk.sh          # Proxmox host helper (create LXC / update / backup / restore)
  install/lxc-setup.sh         # runs inside the container: user, dirs, systemd, env file
  shopdesk/                    # python package
    app.py  config.py  router.py  http_util.py
    db/            connection.py  migrate.py  tenant_db.py  platform_db.py
    migrations/    platform/0001_init.sql   tenant/0001_init.sql ...
    auth/          passwords.py  sessions.py  csrf.py  ratelimit.py  permissions.py
    modules/       tenants  customers  equipment  tickets  estimates  invoices
                   inventory  pos  employees  portal  notifications  reports  settings
    audit.py  files.py (uploads)  pdf/  templates/ (industry profiles as JSON)
  static/          index.html  app.js  style.css      # staff app
  static/portal/   portal.html portal.js              # customer app
  tests/           test_tenant_isolation.py  test_auth.py  test_tickets.py ...
  docs/            DESIGN.md  CONFIG.md  OPERATIONS.md  SECURITY.md
  scripts/         backup.sh  restore.sh  update.sh
  ROADMAP.md       # running implementation plan, updated as phases finish
```

## 2. Tenant-isolation strategy (the main risk)

**Recommendation: one SQLite file per tenant, plus one small platform DB.**

| | DB-per-tenant (recommended) | Shared DB with `tenant_id` column |
|---|---|---|
| Cross-tenant leak from a missing `WHERE` | Structurally impossible: the connection only contains one tenant's data | Possible; every query must be right |
| Tenant export / deletion / retention | Copy or delete one file | Filtered dump, careful deletes |
| Dedicated installation later (brief principle 9) | Same code, one tenant file | Needs extraction |
| Backups / restore of one shop | Trivial | Hard |
| Cross-tenant reporting for the platform owner | Not possible by design (matches "admin should not casually access tenant data") | Easy |
| Migrations | Run per file at startup (fine for tens to low hundreds of tenants) | One run |
| Move to PostgreSQL | Schema-per-tenant or add `tenant_id` then | Straightforward |

Plateful already does DB-per-household, so this is proven in your own code.

Enforcement layers:
1. **Platform DB** (`platform.db`): tenants, staff login identities (email to tenant id), platform admins, tenant provisioning state.
   Contains no business data.
2. **Request context:** session cookie resolves to `(tenant_id, user_id, role)`. Handlers receive a `TenantDB` object that is already
   bound to that tenant's file. There is no API to open another tenant's DB from a handler.
3. **File paths:** attachments live under `data/tenants/<tenant_id>/files/`. Downloads go through an authorized handler,
   never a static file route. Filenames on disk are random IDs, not user input.
4. **Customer portal:** portal sessions are scoped to `(tenant_id, customer_id)`; every portal query filters by `customer_id`.
5. **Tests:** two tenants seeded; for every object type and route, tenant A's session requesting tenant B's IDs must get 404/403.
   Tests iterate over the route table so a newly added route without an isolation test fails CI.

## 3. Entity relationship outline

Platform DB: `tenants`, `staff_identities(email, tenant_id, user_id)`, `platform_admins`, `platform_audit`, `rate_limits`.

Tenant DB (per business):
- Org: `business_profile`, `locations`, `users`, `roles`, `role_permissions`, `sessions`, `invites`, `password_resets`, `settings`
- People/assets: `customers`, `customer_contacts`, `equipment`, `equipment_profiles` (field definitions), `equipment_field_values`
- Work: `tickets`, `ticket_statuses` (configurable), `ticket_status_history`, `ticket_assignees`, `ticket_equipment`,
  `ticket_notes` (visibility: internal | customer), `ticket_numbering`, `attachments`
- Money (Phase 2/3): `ticket_lines` (labor/part/fee/discount), `tax_rates`, `estimates`, `estimate_revisions`, `estimate_approvals`,
  `invoices`, `invoice_lines`, `payments`, `payment_provider_refs`
- Inventory (Phase 3): `products`, `categories`, `suppliers`, `stock_movements` (append-only ledger; quantity on hand is derived),
  `purchase_orders`, `po_lines`
- POS (Phase 3): `sales`, `sale_lines`, `register_sessions`, `cash_movements`
- Time (Phase 5): `time_entries` (attendance) and `labor_entries` (billable, linked to ticket), kept separate per the brief
- Cross-cutting: `audit_log` (append-only), `notifications`, `email_outbox`, `schema_version`

Design rules: money as integer cents; timestamps UTC ISO-8601; soft-delete for business records; inventory as a movement ledger with
a unique key per (source document, line) so editing a ticket cannot double-decrement.

## 4. Security checklist (Phase 1 gates)

- [ ] scrypt password hashing, per-user salt, constant-time compare
- [ ] Random 256-bit session tokens, stored hashed; idle + absolute expiry; logout and "log out everywhere"
- [ ] Cookies: `HttpOnly`, `Secure` (when behind HTTPS), `SameSite=Lax`
- [ ] CSRF token on all state-changing requests (double-submit plus Origin check)
- [ ] Parameterized SQL only; automated grep test forbids string-built SQL
- [ ] XSS: frontend builds DOM via `textContent`, never `innerHTML` with data; CSP header without `unsafe-inline` scripts
- [ ] Rate limits: login, password reset, invite acceptance, public lookup
- [ ] Generic login/reset responses (no account enumeration)
- [ ] Role/permission check as a decorator on every route; tests fail if a route lacks one
- [ ] Tenant-isolation test suite (section 2)
- [ ] Audit log for auth events, permission changes, ticket create/status/assign, user changes
- [ ] Upload validation: size cap, extension and magic-byte allowlist, random names, `Content-Disposition`/`nosniff` on download (Phase 2)
- [ ] Invite and reset tokens: single-use, hashed at rest, expiring
- [ ] Logs contain no customer PII or secrets; structured JSON logs
- [ ] Secrets in `/etc/shopdesk/shopdesk.env` (0600), never in the repo
- [ ] Backups use the SQLite online backup API; documented, tested restore
- [ ] Security headers (CSP, `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`)

## 5. Phase 1 task breakdown

1. Repo skeleton, config loader, logging, router, test harness
2. Migration runner (platform and tenant), initial schemas
3. Auth: scrypt, sessions, CSRF, rate limiter, permissions decorator
4. Tenant provisioning: registration, platform admin bootstrap, onboarding wizard (shop type applies terminology, fields, statuses)
5. Users/roles: owner, manager, technician, front desk; invites with expiring tokens
6. Customers and equipment (profile-driven fields)
7. Tickets: CRUD, search/filter, configurable statuses, priority, due date, multi-assignee, status history, numbering
8. Audit log (writes and viewer)
9. Staff UI: responsive dashboard, tickets, customers, equipment, settings, users
10. Tenant-isolation tests across every route
11. `install/shopdesk.sh` (create/update/backup/restore), systemd unit, healthz, CONFIG/OPERATIONS docs
12. Backup/restore scripts and a restore test on a clean container

Estimated at roughly 4 to 6 working sessions with review points after steps 4, 7 and 11.

## 6. Installer behavior (Plateful-style)

Run on the Proxmox host shell:

```bash
SHOPDESK_REPO=<github-user>/shopdesk bash -c "$(curl -fsSL https://raw.githubusercontent.com/<github-user>/shopdesk/main/install/shopdesk.sh)"
```

Same modes as Plateful: create (new Debian 12 LXC), `update <CTID>` (code only, DB and uploads untouched, migrations run on restart,
automatic pre-update backup), plus `backup <CTID>` and `restore <CTID> <file>`. Same env overrides (`CTID`, `RAM`, `DISK`, `BRIDGE`,
`STORAGE`, `PORT`, `BRANCH`). Defaults for ShopDesk: 2 cores, 1 GB RAM, 8 GB disk (photos and PDFs need more than Plateful).
Non-destructive: refuses to overwrite an existing CTID or data directory.

Note: while reading `plateful/install/plateful.sh` I found a bug on line 84: `--hostname "$CT_NAME"--cores "$CORES"` is missing a space,
so `pct create` receives a malformed argument. I will not carry that into ShopDesk. Plateful's copy is unchanged; tell me if you want it fixed.

## 7. Recommendations on the brief's open decisions

| Decision | Recommendation | Why |
|---|---|---|
| A. Target market | Universal data model, but ship and test with **one** profile first (computer/phone), others as JSON profiles | Configurable fields are cheap; polishing every industry is not |
| B. Hosting | Shared multi-tenant first, DB-per-tenant makes dedicated installs a config change | See section 2 |
| C. Delivery | Smaller MVP to one real shop quickly, then harden | Real feedback beats speculation |
| D. Name | Keep "NoksForge ShopDesk" working name; keep it in one config constant | Cheap to rename |
| E. Payments | Defer to Phase 3. Likely Stripe (hosted Checkout, test mode, Terminal for POS) or Square (strong POS) | Decide after Phase 2 |
| F. Business model | Private homelab pilot with 1 to 3 shops, then decide on commercial SaaS | Commercial needs policies, insurance, support |

## 8. Known risks

- SQLite write concurrency: fine for a pilot (WAL, short transactions). With per-tenant files, contention is per shop.
- Hand-rolled HTTP and auth code needs careful tests; this is why security tests are Phase 1 gates, not polish.
- Email deliverability from a homelab: use an SMTP relay, not direct send.
- Cloudflare Tunnel terminates TLS; the app must trust `X-Forwarded-*` only when `SHOPDESK_TRUST_PROXY=1`.
- OneDrive-synced working folder can corrupt SQLite/`.git`; develop in a non-synced path or keep the DB out of the repo folder.
