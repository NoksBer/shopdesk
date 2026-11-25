"""Configuration from environment variables (see README for the reference)."""
import os
import pathlib
import secrets

PKG_DIR = pathlib.Path(__file__).resolve().parent
ROOT_DIR = PKG_DIR.parent

# GitHub "owner/repo" that publishes ShopDesk releases. CHANGE THE REPO NAME if yours differs.
UPDATE_REPO = "NoksBer/ShopDesk-Live"


def _bool(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self, env=None, data_dir=None):
        e = os.environ if env is None else env
        self.host = e.get("SHOPDESK_HOST", "127.0.0.1")
        self.port = int(e.get("SHOPDESK_PORT", "8080"))
        self.data_dir = pathlib.Path(data_dir or e.get("SHOPDESK_DATA_DIR", "./data")).resolve()
        self.secret = e.get("SHOPDESK_SECRET", "")
        self.trust_proxy = _bool(e.get("SHOPDESK_TRUST_PROXY", "0"))
        # Anyone can create their own business (each gets a separate, isolated database). Set to 0 to close
        # registration; the very first business can always be created.
        self.open_signup = _bool(e.get("SHOPDESK_OPEN_SIGNUP", "1"))
        # Cookies get the Secure flag over HTTPS (detected via the trusted proxy) or when forced.
        self.force_secure_cookies = _bool(e.get("SHOPDESK_SECURE_COOKIES", "0"))
        self.session_idle_seconds = int(e.get("SHOPDESK_SESSION_IDLE_SECONDS", str(12 * 3600)))
        self.session_max_seconds = int(e.get("SHOPDESK_SESSION_MAX_SECONDS", str(7 * 86400)))
        self.register_limit_per_hour = int(e.get("SHOPDESK_REGISTER_LIMIT", "5"))   # per client IP
        self.static_dir = ROOT_DIR / "static"
        self.migrations_dir = PKG_DIR / "migrations"
        self.max_body_bytes = 1024 * 1024
        # Software updates. The repository is fixed here and is NEVER taken from a request or the browser.
        # (The root-owned updater reads its own copy from /etc/shopdesk/update.conf, written by the installer.)
        self.update_repo = UPDATE_REPO
        self.updater_path = pathlib.Path("/usr/local/bin/shopdesk-update")   # present only when the updater is installed

    @property
    def platform_db_path(self):
        return self.data_dir / "platform.db"

    @property
    def tenants_dir(self):
        return self.data_dir / "tenants"

    @property
    def update_request_path(self):      # the app drops a file here; a root systemd .path unit starts the updater
        return self.data_dir / "update-request"

    @property
    def update_status_path(self):
        return self.data_dir / "update-status.json"

    @property
    def update_lock_path(self):
        return self.data_dir / "update.lock"

    def ensure_dirs(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.tenants_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError:
            pass

    def load_secret(self):
        """Server secret used to key session-token hashes. Env wins; else a persisted random file."""
        if self.secret:
            return self.secret.encode()
        path = self.data_dir / ".secret"
        if path.exists():
            return path.read_text().strip().encode()
        value = secrets.token_hex(32)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(value)
        return value.encode()
