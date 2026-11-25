"""Password hashing (scrypt), session tokens, and the in-memory rate limiter."""
import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import deque

from .util import ApiError, now_ts

# ---------------------------------------------------------------- passwords
_N, _R, _P = 2 ** 14, 8, 1
MIN_PASSWORD = 10
MAX_PASSWORD = 200


def _b64(raw):
    return base64.b64encode(raw).decode("ascii")


def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P,
                        maxmem=64 * 1024 * 1024, dklen=32)
    return "scrypt$%d$%d$%d$%s$%s" % (_N, _R, _P, _b64(salt), _b64(dk))


def verify_password(password, stored):
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r),
                            p=int(p), maxmem=64 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(dk, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


_dummy = None


def dummy_hash():
    """A throwaway hash so unknown-account logins cost the same as real ones (timing)."""
    global _dummy
    if _dummy is None:
        _dummy = hash_password("not-a-real-password-for-timing")
    return _dummy


def password_error(password):
    if not isinstance(password, str):
        return "Password is required"
    if len(password) < MIN_PASSWORD:
        return "Password must be at least %d characters" % MIN_PASSWORD
    if len(password) > MAX_PASSWORD:
        return "Password is too long"
    return None


# ---------------------------------------------------------------- sessions
def token_hash(secret, token):
    return hmac.new(secret, token.encode("utf-8"), hashlib.sha256).hexdigest()


# kind -> literal SQL (tables differ; no SQL is ever built from input)
_SQL = {
    "tenant": {
        "insert": "INSERT INTO sessions(token_hash, subject_id, csrf, created_at, last_seen, expires_at) VALUES(?,?,?,?,?,?)",
        "get": "SELECT subject_id, csrf, last_seen, expires_at FROM sessions WHERE token_hash=?",
        "touch": "UPDATE sessions SET last_seen=? WHERE token_hash=?",
        "delete": "DELETE FROM sessions WHERE token_hash=?",
        "purge": "DELETE FROM sessions WHERE expires_at<? OR last_seen<?",
    },
    "platform": {
        "insert": "INSERT INTO platform_sessions(token_hash, subject_id, csrf, created_at, last_seen, expires_at) VALUES(?,?,?,?,?,?)",
        "get": "SELECT subject_id, csrf, last_seen, expires_at FROM platform_sessions WHERE token_hash=?",
        "touch": "UPDATE platform_sessions SET last_seen=? WHERE token_hash=?",
        "delete": "DELETE FROM platform_sessions WHERE token_hash=?",
        "purge": "DELETE FROM platform_sessions WHERE expires_at<? OR last_seen<?",
    },
}


def create_session(conn, kind, secret, subject_id, config):
    """Returns (raw_token, csrf_token). Only a keyed hash of the token is stored."""
    sql = _SQL[kind]
    now = now_ts()
    conn.execute(sql["purge"], (now, now - config.session_idle_seconds))
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    conn.execute(sql["insert"], (token_hash(secret, token), subject_id, csrf, now, now,
                                 now + config.session_max_seconds))
    return token, csrf


def lookup_session(conn, kind, secret, token, config):
    """Returns {'subject_id','csrf','hash'} or None (expired sessions are deleted)."""
    sql = _SQL[kind]
    h = token_hash(secret, token)
    row = conn.execute(sql["get"], (h,)).fetchone()
    if row is None:
        return None
    now = now_ts()
    if now >= row["expires_at"] or now - row["last_seen"] > config.session_idle_seconds:
        conn.execute(sql["delete"], (h,))
        return None
    if now - row["last_seen"] > 60:
        conn.execute(sql["touch"], (now, h))
    return {"subject_id": row["subject_id"], "csrf": row["csrf"], "hash": h}


def destroy_session(conn, kind, token_hash_value):
    conn.execute(_SQL[kind]["delete"], (token_hash_value,))


# ---------------------------------------------------------------- rate limiting
class RateLimiter:
    """Sliding-window limiter, in memory (single process). Raises 429 when exceeded."""

    def __init__(self):
        self._hits = {}
        self._lock = threading.Lock()

    def check(self, key, limit, window_seconds):
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] > window_seconds:
                q.popleft()
            if len(q) >= limit:
                raise ApiError(429, "Too many attempts. Please wait a few minutes and try again.")
            q.append(now)
            if len(self._hits) > 20000:  # bound memory
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] > window_seconds]:
                    del self._hits[k]
