"""Software-update support inside the app: release check (GitHub), update status and the update request.

The app never installs anything itself. "Apply" only drops a small request file; a root-owned systemd .path unit
notices it and starts /usr/local/bin/shopdesk-update, which does the real work (see install/shopdesk-update).
Nothing here accepts a repository, URL or command from a client.
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

from . import __version__

try:                                   # not available on Windows (only matters for local development)
    import fcntl
except ImportError:                    # pragma: no cover
    fcntl = None

SEMVER_RE = re.compile(r"^v?(\d{1,6})\.(\d{1,6})\.(\d{1,6})$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
CACHE_SECONDS = 600
MAX_NOTES = 20000
QUEUE_TIMEOUT_SECONDS = 45             # request file still unread after this long: the .path unit is not working
STATUS_KEYS = ("state", "step", "message", "startedAt", "finishedAt", "error", "fromVersion", "targetVersion",
               "rolledBack", "backup", "queuedAt")


class UpdateError(Exception):
    """A problem worth showing to the administrator (network, rate limit, bad release data)."""


def parse_semver(text):
    m = SEMVER_RE.match(text.strip()) if isinstance(text, str) else None
    return tuple(int(x) for x in m.groups()) if m else None


def asset_names(tag):
    base = "shopdesk-%s.tar.gz" % tag
    return base, base + ".sha256"


def fetch_latest_release(repo):
    """GET the latest published (non-draft, non-prerelease) release of `repo` from the GitHub API."""
    if not REPO_RE.match(repo):
        raise UpdateError("The update repository is not configured correctly.")
    req = urllib.request.Request(
        "https://api.github.com/repos/%s/releases/latest" % repo,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "shopdesk-update-check",
                 "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read(2_000_000)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("No release has been published yet.")
        if e.code in (403, 429):
            raise UpdateError("GitHub is rate limiting update checks. Try again in a few minutes.")
        raise UpdateError("GitHub answered with an error (%d)." % e.code)
    except (urllib.error.URLError, OSError, TimeoutError):
        raise UpdateError("Could not reach GitHub. Check the server's internet connection.")
    try:
        data = json.loads(raw)
    except ValueError:
        raise UpdateError("GitHub sent an unreadable answer.")
    if not isinstance(data, dict):
        raise UpdateError("GitHub sent an unexpected answer.")
    return data


class UpdateChecker:
    """Compares the running version with the latest release. Result cached ~10 minutes; failures fall back to it."""

    def __init__(self, config, fetch=None):
        self.config = config
        self.fetch = fetch or fetch_latest_release
        self._lock = threading.Lock()
        self._cached = None            # (monotonic time, result)

    def check(self):
        with self._lock:
            now = time.monotonic()
            if self._cached and now - self._cached[0] < CACHE_SECONDS:
                return dict(self._cached[1], cached=True)
            try:
                result = self._build(self.fetch(self.config.update_repo))
            except UpdateError as e:
                if self._cached:       # keep working off the last good answer
                    return dict(self._cached[1], cached=True, stale=True, warning=str(e))
                raise
            self._cached = (now, result)
            return dict(result, cached=False)

    def forget(self):
        with self._lock:
            self._cached = None

    def _build(self, data):
        tag = data.get("tag_name")
        latest = parse_semver(tag)
        if latest is None:
            raise UpdateError("The latest release tag is not a version number (expected e.g. v6.1.0).")
        current = parse_semver(__version__)
        names = {a.get("name") for a in (data.get("assets") or []) if isinstance(a, dict)}
        tarball, checksum = asset_names("v%d.%d.%d" % latest)
        notes = data.get("body")
        return {
            "current": __version__,
            "latest": "%d.%d.%d" % latest,
            "tag": "v%d.%d.%d" % latest,
            "updateAvailable": current is not None and latest > current,
            "installable": tarball in names and checksum in names,   # release must carry the tarball + .sha256
            "notes": notes[:MAX_NOTES] if isinstance(notes, str) else "",
            "publishedAt": data.get("published_at") if isinstance(data.get("published_at"), str) else None,
            "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "updaterInstalled": self.config.updater_path.exists(),
        }


# ---------------------------------------------------------------- status / lock / request
def is_locked(config):
    """True while the updater process holds its lock (it takes an exclusive flock for its whole run)."""
    if fcntl is None or not config.update_lock_path.exists():
        return None                    # cannot tell
    try:
        with open(config.update_lock_path, "rb") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(f, fcntl.LOCK_UN)
            return False
    except OSError:
        return None


def _read_status_file(config):
    try:
        with open(config.update_status_path, "rb") as f:
            data = json.loads(f.read(100_000))
    except (OSError, ValueError):
        return {"state": "idle"}
    if not isinstance(data, dict) or data.get("state") not in ("idle", "queued", "running", "success", "failed"):
        return {"state": "idle"}
    return {k: data[k] for k in STATUS_KEYS if k in data}


def _write_status(config, status):
    tmp = str(config.update_status_path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(status, f)
    os.replace(tmp, config.update_status_path)


def read_status(config):
    """Current update status for the UI. Also repairs two stuck cases: a 'running' status left by a crashed updater
    and a request the .path unit never picked up."""
    status = _read_status_file(config)
    locked = is_locked(config)
    if status["state"] == "running" and locked is False:
        status.update(state="failed", error="The updater stopped unexpectedly. Check the server log "
                      "(journalctl -u shopdesk-update).")
    elif status["state"] == "queued":
        queued_at = status.get("queuedAt") or 0
        expired = time.time() - queued_at > QUEUE_TIMEOUT_SECONDS
        if not config.update_request_path.exists():
            if expired and locked is False:      # picked up, but the updater died before reporting
                status = {"state": "failed", "error": "The updater stopped unexpectedly. Check the server log "
                          "(journalctl -u shopdesk-update)."}
                try:
                    _write_status(config, status)
                except OSError:
                    pass
        elif expired:
            try:
                config.update_request_path.unlink()
            except OSError:
                pass
            status = {"state": "failed", "error": "The update did not start. The updater is not listening "
                      "(is shopdesk-update.path enabled? See RELEASING.md)."}
            try:
                _write_status(config, status)
            except OSError:
                pass
    status["running"] = bool(locked) or status["state"] in ("queued", "running")
    return status


def is_busy(config):
    """True when an update is queued or running (reject a second one)."""
    if config.update_request_path.exists():
        return True
    locked = is_locked(config)
    if locked:
        return True
    state = _read_status_file(config)["state"]
    if locked is None:
        return state in ("queued", "running")
    return state == "queued"           # request picked up but the updater has not taken its lock yet


def request_update(config, tag):
    """Queue an update to `tag` (already validated). The root-owned updater re-checks everything itself."""
    if parse_semver(tag) is None:
        raise UpdateError("Invalid version.")
    tmp = str(config.update_request_path) + ".tmp"
    with open(tmp, "w", encoding="ascii") as f:
        f.write(tag + "\n")
    _write_status(config, {"state": "queued", "step": "queued", "message": "Waiting for the updater to start…",
                           "targetVersion": tag.lstrip("v"), "fromVersion": __version__,
                           "queuedAt": time.time()})
    os.replace(tmp, config.update_request_path)     # the .path unit fires on this file appearing
