#!/usr/bin/env bash
# ShopDesk - Proxmox VE helper script.
# Run on the Proxmox NODE shell (as root). Creates a small Debian 12 LXC container, installs
# ShopDesk as a systemd service, and prints the URL.
#
#   From GitHub:   SHOPDESK_REPO=youruser/shopdesk bash -c "$(curl -fsSL https://raw.githubusercontent.com/youruser/shopdesk/main/install/shopdesk.sh)"
#   From a file:   SHOPDESK_TARBALL=/root/shopdesk.tar.gz bash shopdesk.sh
#   Update later:  bash shopdesk.sh update <CTID>          (same env vars as above)
#   Add updater:   bash shopdesk.sh install-updater <CTID> (one-time, for containers installed before the web updater)
#   Backup:        bash shopdesk.sh backup <CTID>          (writes /var/lib/vz/dump/shopdesk-<CTID>-<date>.tar.gz)
#   Restore:       bash shopdesk.sh restore <CTID> <file>
#
# Optional env overrides: CTID CT_NAME CORES RAM DISK BRIDGE STORAGE TEMPLATE_STORAGE PORT BRANCH TRUST_PROXY
set -euo pipefail

APP=shopdesk
CT_NAME="${CT_NAME:-shopdesk}"
CORES="${CORES:-2}"
RAM="${RAM:-1024}"
DISK="${DISK:-8}"
BRIDGE="${BRIDGE:-vmbr0}"
PORT="${PORT:-8080}"
TRUST_PROXY="${TRUST_PROXY:-0}"   # set to 1 only when behind your own HTTPS reverse proxy/tunnel
BRANCH="${BRANCH:-main}"
SHOPDESK_REPO="${SHOPDESK_REPO:-}"
SHOPDESK_TARBALL="${SHOPDESK_TARBALL:-}"
BACKUP_DIR="${BACKUP_DIR:-/var/lib/vz/dump}"

msg()  { echo -e "\e[1;32m[+]\e[0m $*"; }
warn() { echo -e "\e[1;33m[!]\e[0m $*"; }
die()  { echo -e "\e[1;31m[x]\e[0m $*" >&2; exit 1; }

command -v pct >/dev/null   || die "Run this on a Proxmox VE node (pct not found)."
[ "$(id -u)" -eq 0 ]        || die "Run as root."

need_source() {
  [ -n "$SHOPDESK_REPO$SHOPDESK_TARBALL" ] || die "Set SHOPDESK_REPO=user/repo (GitHub) or SHOPDESK_TARBALL=/path/shopdesk.tar.gz"
  [ -z "$SHOPDESK_TARBALL" ] || [ -f "$SHOPDESK_TARBALL" ] || die "Tarball not found: $SHOPDESK_TARBALL"
}

# Online (consistent) backup of every SQLite DB plus uploaded files, inside container $1 -> host file $2.
do_backup() {
  local id="$1" out="$2"
  pct exec "$id" -- bash -c '
    set -e
    rm -rf /tmp/sd-backup && mkdir -p /tmp/sd-backup/data
    python3 - <<PY
import sqlite3, pathlib, shutil
src = pathlib.Path("/var/lib/shopdesk"); dst = pathlib.Path("/tmp/sd-backup/data")
for p in src.rglob("*"):
    rel = p.relative_to(src)
    if rel.parts[0] == "backups": continue          # in-app update backups are not nested into host backups
    if p.is_dir(): (dst/rel).mkdir(parents=True, exist_ok=True)
    elif p.suffix == ".db":
        (dst/rel).parent.mkdir(parents=True, exist_ok=True)
        a = sqlite3.connect(f"file:{p}?mode=ro", uri=True); b = sqlite3.connect(dst/rel)
        a.backup(b); b.close(); a.close()
    elif p.suffix in (".db-wal", ".db-shm"): pass
    else: (dst/rel).parent.mkdir(parents=True, exist_ok=True); shutil.copy2(p, dst/rel)
PY
    tar -czf /tmp/sd-backup.tar.gz -C /tmp/sd-backup data && rm -rf /tmp/sd-backup'
  pct pull "$id" /tmp/sd-backup.tar.gz "$out"
  pct exec "$id" -- rm -f /tmp/sd-backup.tar.gz
  chmod 600 "$out"
}

# Gets the source archive into container $1 (from the tarball or GitHub) and unpacks it to /tmp/sd.
fetch_archive() {
  local id="$1"
  if [ -n "$SHOPDESK_TARBALL" ]; then
    pct push "$id" "$SHOPDESK_TARBALL" /tmp/shopdesk.tar.gz
  else
    pct exec "$id" -- bash -c "curl -fsSL https://github.com/${SHOPDESK_REPO}/archive/refs/heads/${BRANCH}.tar.gz -o /tmp/shopdesk.tar.gz"
  fi
  pct exec "$id" -- bash -c '
    set -e
    rm -rf /tmp/sd && mkdir -p /tmp/sd
    tar -xzf /tmp/shopdesk.tar.gz -C /tmp/sd
    SRC=$(find /tmp/sd -name server.py -not -path "*/node_modules/*" | head -n1 | xargs -r dirname)
    [ -n "$SRC" ] || { echo "server.py not found in archive"; exit 1; }
    echo "$SRC" > /tmp/sd/.srcdir'
}

# Copies the app code into container $1 (never touches /var/lib/shopdesk, where data lives). Needs fetch_archive first.
deploy_app() {
  local id="$1"
  pct exec "$id" -- bash -c '
    set -e
    SRC=$(cat /tmp/sd/.srcdir)
    rm -rf /opt/shopdesk.new && mkdir -p /opt/shopdesk.new
    cp -r "$SRC"/server.py "$SRC"/static /opt/shopdesk.new/
    [ -f "$SRC/VERSION" ] && cp "$SRC/VERSION" /opt/shopdesk.new/ || true
    [ -d "$SRC/shopdesk" ] && cp -r "$SRC/shopdesk" /opt/shopdesk.new/
    rm -rf /opt/shopdesk.prev
    [ -d /opt/shopdesk ] && mv /opt/shopdesk /opt/shopdesk.prev
    mv /opt/shopdesk.new /opt/shopdesk
    chown -R root:shopdesk /opt/shopdesk && chmod -R g+rX,go-w /opt/shopdesk'
}

# Installs the in-app updater into container $1: /usr/local/bin/shopdesk-update (root), a oneshot systemd unit, and a
# .path unit that starts it when the app drops /var/lib/shopdesk/update-request. No sudo/polkit and no change to the
# app service's hardening. Needs fetch_archive first (the script ships in the archive under install/).
install_updater() {
  local id="$1" tmp
  local repo="${SHOPDESK_UPDATE_REPO:-${SHOPDESK_REPO:-}}"
  [ -z "$repo" ] || [[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "SHOPDESK_UPDATE_REPO must look like owner/repo"
  tmp="$(mktemp -d)"
  cat > "$tmp/shopdesk-update.service" <<'EOF'
[Unit]
Description=ShopDesk updater (runs once per update request)
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/shopdesk-update
TimeoutStartSec=1200
EOF
  cat > "$tmp/shopdesk-update.path" <<'EOF'
[Unit]
Description=Start the ShopDesk updater when an update is requested from the web UI

[Path]
PathExists=/var/lib/shopdesk/update-request
Unit=shopdesk-update.service

[Install]
WantedBy=multi-user.target
EOF
  pct push "$id" "$tmp/shopdesk-update.service" /etc/systemd/system/shopdesk-update.service
  pct push "$id" "$tmp/shopdesk-update.path" /etc/systemd/system/shopdesk-update.path
  if [ -n "$repo" ]; then
    printf 'SHOPDESK_UPDATE_REPO=%s\n' "$repo" > "$tmp/update.conf"
    pct push "$id" "$tmp/update.conf" /etc/shopdesk/update.conf.new
  fi
  rm -rf "$tmp"
  pct exec "$id" -- bash -c '
    set -e
    SRC=$(cat /tmp/sd/.srcdir)
    [ -f "$SRC/install/shopdesk-update" ] || { echo "install/shopdesk-update is not in the archive"; exit 1; }
    install -m 0755 -o root -g root "$SRC/install/shopdesk-update" /usr/local/bin/shopdesk-update
    if [ -f /etc/shopdesk/update.conf.new ]; then mv /etc/shopdesk/update.conf.new /etc/shopdesk/update.conf; fi
    [ -f /etc/shopdesk/update.conf ] || echo "SHOPDESK_UPDATE_REPO=NoksBer/ShopDesk-Live" > /etc/shopdesk/update.conf
    chown root:root /etc/shopdesk/update.conf /etc/systemd/system/shopdesk-update.service /etc/systemd/system/shopdesk-update.path
    chmod 644 /etc/shopdesk/update.conf /etc/systemd/system/shopdesk-update.service /etc/systemd/system/shopdesk-update.path
    systemctl daemon-reload
    systemctl enable --now shopdesk-update.path'
}

cleanup_archive() {
  pct exec "$1" -- rm -rf /tmp/sd /tmp/shopdesk.tar.gz
}

# ------------------------------------------------------------------ update / backup / restore modes
case "${1:-}" in
  update)
    id="${2:-}"; [ -n "$id" ] || die "Usage: shopdesk.sh update <CTID>"
    need_source
    mkdir -p "$BACKUP_DIR"
    pre="$BACKUP_DIR/shopdesk-$id-preupdate-$(date +%Y%m%d-%H%M%S).tar.gz"
    msg "Taking pre-update backup: $pre"
    do_backup "$id" "$pre"
    msg "Updating ShopDesk in container $id"
    fetch_archive "$id"
    deploy_app "$id"
    install_updater "$id"       # also refreshes the in-app updater itself
    cleanup_archive "$id"
    pct exec "$id" -- systemctl restart shopdesk
    msg "Updated. Data kept. Previous code is in /opt/shopdesk.prev inside the container (rollback: swap the folders and restart)."
    exit 0 ;;
  install-updater)
    # One-time step for containers installed before the in-app updater existed (also refreshes it later).
    id="${2:-}"; [ -n "$id" ] || die "Usage: shopdesk.sh install-updater <CTID>   (with SHOPDESK_REPO or SHOPDESK_TARBALL set)"
    need_source
    msg "Installing the in-app updater into container $id (the app code is not changed)"
    fetch_archive "$id"
    install_updater "$id"
    cleanup_archive "$id"
    msg "Done. A platform administrator can now use Software updates at /admin.html (create one: server.py create-admin)."
    exit 0 ;;
  backup)
    id="${2:-}"; [ -n "$id" ] || die "Usage: shopdesk.sh backup <CTID>"
    mkdir -p "$BACKUP_DIR"
    out="$BACKUP_DIR/shopdesk-$id-$(date +%Y%m%d-%H%M%S).tar.gz"
    do_backup "$id" "$out"; msg "Backup written: $out"; exit 0 ;;
  restore)
    id="${2:-}"; file="${3:-}"
    [ -n "$id" ] && [ -f "$file" ] || die "Usage: shopdesk.sh restore <CTID> <backup.tar.gz>"
    warn "This REPLACES all ShopDesk data in container $id with the backup."
    pct push "$id" "$file" /tmp/sd-restore.tar.gz
    pct exec "$id" -- bash -c '
      set -e
      systemctl stop shopdesk
      mv /var/lib/shopdesk "/var/lib/shopdesk.before-restore.$(date +%s)"
      mkdir -p /tmp/sd-restore && tar -xzf /tmp/sd-restore.tar.gz -C /tmp/sd-restore
      mv /tmp/sd-restore/data /var/lib/shopdesk
      chown -R shopdesk:shopdesk /var/lib/shopdesk && chmod 700 /var/lib/shopdesk
      rm -rf /tmp/sd-restore /tmp/sd-restore.tar.gz
      systemctl start shopdesk'
    msg "Restored. The old data folder was kept inside the container as /var/lib/shopdesk.before-restore.*"
    exit 0 ;;
esac

# ------------------------------------------------------------------ create container
need_source
CTID="${CTID:-$(pvesh get /cluster/nextid)}"
pct status "$CTID" >/dev/null 2>&1 && die "Container $CTID already exists. Use 'update $CTID' or set a different CTID."
STORAGE="${STORAGE:-$(pvesm status -content rootdir | awk 'NR>1{print $1; exit}')}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-$(pvesm status -content vztmpl | awk 'NR>1{print $1; exit}')}"
[ -n "$STORAGE" ] || die "No storage with 'rootdir' content found. Set STORAGE=<name>."
[ -n "$TEMPLATE_STORAGE" ] || die "No storage with 'vztmpl' content found. Set TEMPLATE_STORAGE=<name>."

msg "Container ID: $CTID | storage: $STORAGE | templates: $TEMPLATE_STORAGE | bridge: $BRIDGE"

msg "Finding Debian 12 template"
pveam update >/dev/null 2>&1 || warn "pveam update failed (continuing with cached list)"
TEMPLATE="$(pveam list "$TEMPLATE_STORAGE" | awk '/debian-12-standard/{print $1}' | sed 's|.*/||' | sort -V | tail -n1)"
if [ -z "$TEMPLATE" ]; then
  TEMPLATE="$(pveam available --section system | awk '/debian-12-standard/{print $2}' | sort -V | tail -n1)"
  [ -n "$TEMPLATE" ] || die "Could not find a Debian 12 template via pveam."
  msg "Downloading $TEMPLATE"
  pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
fi

msg "Creating LXC $CTID ($CT_NAME)"
# NOTE: the space before --cores matters (fixes the missing-space bug present in plateful.sh line 84).
pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE" \
  --hostname "$CT_NAME" --cores "$CORES" --memory "$RAM" --swap 512 \
  --rootfs "$STORAGE:$DISK" --net0 "name=eth0,bridge=$BRIDGE,ip=dhcp" \
  --unprivileged 1 --features nesting=1 --onboot 1 --ostype debian \
  --description "ShopDesk repair shop manager (http port $PORT)"
pct start "$CTID"

msg "Waiting for network"
IP=""
for _ in $(seq 1 40); do
  IP="$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}' || true)"
  [ -n "$IP" ] && break
  sleep 2
done
[ -n "$IP" ] || die "Container got no IP. Check bridge '$BRIDGE' and DHCP."

msg "Installing Python and creating service account"
pct exec "$CTID" -- bash -c "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq python3 curl ca-certificates >/dev/null
id shopdesk >/dev/null 2>&1 || useradd --system --home-dir /var/lib/shopdesk --shell /usr/sbin/nologin shopdesk
mkdir -p /var/lib/shopdesk /etc/shopdesk && chown shopdesk:shopdesk /var/lib/shopdesk && chmod 700 /var/lib/shopdesk"

msg "Installing ShopDesk"
fetch_archive "$CTID"
deploy_app "$CTID"

pct exec "$CTID" -- bash -c "
umask 077
[ -f /etc/shopdesk/shopdesk.env ] || cat > /etc/shopdesk/shopdesk.env <<EOF
SHOPDESK_PORT=$PORT
SHOPDESK_HOST=0.0.0.0
SHOPDESK_TRUST_PROXY=$TRUST_PROXY
SHOPDESK_DATA_DIR=/var/lib/shopdesk
SHOPDESK_SECRET=\$(python3 -c 'import secrets;print(secrets.token_hex(32))')
EOF
chown root:shopdesk /etc/shopdesk/shopdesk.env && chmod 640 /etc/shopdesk/shopdesk.env
cat > /etc/systemd/system/shopdesk.service <<EOF
[Unit]
Description=ShopDesk repair shop manager
After=network.target

[Service]
User=shopdesk
Group=shopdesk
WorkingDirectory=/opt/shopdesk
EnvironmentFile=/etc/shopdesk/shopdesk.env
ExecStart=/usr/bin/python3 /opt/shopdesk/server.py
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/shopdesk

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable --now shopdesk"

msg "Installing the in-app updater"
install_updater "$CTID"
cleanup_archive "$CTID"

sleep 2
if pct exec "$CTID" -- curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null; then
  msg "ShopDesk is running."
else
  warn "Service did not answer yet - check: pct exec $CTID -- journalctl -u shopdesk -n 50"
fi
echo
echo -e "  \e[1mOpen:\e[0m  http://$IP:$PORT"
echo "  Tip: set a DHCP reservation for $IP on your router so the address never changes."
echo "  HTTPS: do not expose the plain HTTP port. Use Cloudflare Tunnel to http://$IP:$PORT and re-run with TRUST_PROXY=1."
echo "  Server admin:  pct enter $CTID, then: runuser -u shopdesk -- env SHOPDESK_DATA_DIR=/var/lib/shopdesk python3 /opt/shopdesk/server.py create-admin you@example.com"
echo "                 then open http://$IP:$PORT/admin.html for Software updates"
echo "  Update later:  bash shopdesk.sh update $CTID   (or from the web UI, see RELEASING.md)"
echo "  Backup:        bash shopdesk.sh backup $CTID"
