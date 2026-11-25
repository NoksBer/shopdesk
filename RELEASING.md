# Releasing ShopDesk

Updates are pulled from **GitHub Releases** of the repository named in `UPDATE_REPO` in `shopdesk/config.py`
(`NoksBer/ShopDesk-Live`; if you ever rename or move the repo, change it there and set `SHOPDESK_UPDATE_REPO`
for the installer, see below). The server never uses `git`.

## Cut a release

1. Make sure the tests pass: `python -m unittest discover -s tests -v`.
2. Bump the version in the **`VERSION`** file (e.g. `6.1.0`, plain semver `MAJOR.MINOR.PATCH`). Commit and push.
3. Tag and push: `git tag v6.1.0 && git push origin v6.1.0` (the tag must be `v` + the VERSION value).
4. Build the two release files from the tagged commit (run in the repo root):

   ```bash
   git archive --format=tar.gz --prefix=shopdesk-v6.1.0/ -o shopdesk-v6.1.0.tar.gz v6.1.0
   sha256sum shopdesk-v6.1.0.tar.gz > shopdesk-v6.1.0.tar.gz.sha256
   ```

   (PowerShell: `git archive ...` is the same; for the checksum use
   `(Get-FileHash shopdesk-v6.1.0.tar.gz -Algorithm SHA256).Hash.ToLower() | Set-Content -NoNewline shopdesk-v6.1.0.tar.gz.sha256`.)
5. On GitHub: **Releases → Draft a new release**, choose the tag, write the release notes (they are shown in the
   app), **attach both files** (`shopdesk-v6.1.0.tar.gz` and `shopdesk-v6.1.0.tar.gz.sha256`) and click **Publish**.
   Drafts and pre-releases are ignored by the updater.

A release without both attached files shows in the app as "no installable files yet" and cannot be applied.
The file names must match exactly. The tarball's `VERSION` must equal the tag.

## What users do

A **platform administrator** (server-level login, not a shop owner) opens `http://<server>/admin.html`,
**Software updates → Check for updates → Update now**. Shop owners never see this because an update restarts
the server for every shop. Create the administrator once, inside the container:

```bash
runuser -u shopdesk -- env SHOPDESK_DATA_DIR=/var/lib/shopdesk python3 /opt/shopdesk/server.py create-admin you@example.com
```

## What the update does

`shopdesk-update` (root, `/usr/local/bin`) is started by `shopdesk-update.path` when the app writes
`/var/lib/shopdesk/update-request`. It:

1. takes a lock, re-asks GitHub for the latest release and refuses unless it is the version the admin approved;
2. downloads only the two release files from `github.com/<repo>/releases/download/<tag>/`, verifies SHA-256,
   unpacks safely, checks the new code (syntax, required files, VERSION = tag) and stages it;
3. stops ShopDesk (this is the "maintenance" window, usually a few seconds), backs up all databases, data files and
   the current code to `/var/lib/shopdesk/backups/<timestamp>-v<old>/` (last 3 kept);
4. swaps the code in (the old code stays in `/opt/shopdesk.prev`), starts the service (database migrations run at
   app start), and polls `/healthz` for up to 60 s until it reports the new version;
5. on any failure restores the code and databases from the backup, restarts, and reports "failed … rolled back".

There is no build or dependency step (the app uses only the Python standard library). Progress is in
`/var/lib/shopdesk/update-status.json` and `journalctl -u shopdesk-update`. Data (databases, uploads, secret) lives
in `/var/lib/shopdesk`, outside the code folder, so replacing code never touches it.

## Existing installs (one-time step)

Containers installed before this feature have no updater. From the Proxmox node shell, with the new code pushed to
GitHub (or a tarball):

```bash
SHOPDESK_REPO=NoksBer/ShopDesk-Live bash shopdesk.sh update <CTID>          # updates the app code and installs the updater
# or only the updater, leaving the app code alone:
SHOPDESK_REPO=NoksBer/ShopDesk-Live bash shopdesk.sh install-updater <CTID>
```

`SHOPDESK_UPDATE_REPO=owner/repo` overrides the repository the updater trusts (default: `SHOPDESK_REPO`, then
`NoksBer/ShopDesk-Live`); it is stored root-owned in `/etc/shopdesk/update.conf`. Later changes to the updater script itself
need `install-updater` again (the web updater replaces the app code, not the root-owned script).

## Manual rollback

```bash
systemctl stop shopdesk
rm -rf /opt/shopdesk && cp -a /var/lib/shopdesk/backups/<stamp>/code /opt/shopdesk
# restore databases only if needed: copy files from /var/lib/shopdesk/backups/<stamp>/data over /var/lib/shopdesk
find /var/lib/shopdesk -path /var/lib/shopdesk/backups -prune -o -exec chown shopdesk:shopdesk {} +
systemctl start shopdesk
```

## Not tested yet

The updater has never run on a real Proxmox container. Try it first on a throwaway container with a test release
(e.g. v6.0.1) before relying on it.
