"""Test helpers: start a real ShopDesk server on a free port with a throwaway data directory."""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shopdesk import auth  # noqa: E402
from shopdesk.app import App  # noqa: E402
from shopdesk.config import Config  # noqa: E402
from shopdesk.db import connect  # noqa: E402
from shopdesk.httpd import make_server  # noqa: E402
from shopdesk.util import new_id, now_iso  # noqa: E402

PASSWORD = "correct horse battery"


class TestServer:
    def __init__(self, **env):
        self.tmp = tempfile.mkdtemp(prefix="shopdesk-test-")
        e = {"SHOPDESK_HOST": "127.0.0.1", "SHOPDESK_PORT": "0", "SHOPDESK_OPEN_SIGNUP": "1",
             "SHOPDESK_REGISTER_LIMIT": "1000"}
        e.update(env)
        self.config = Config(env=e, data_dir=self.tmp)
        self.app = App(self.config)
        self.server = make_server(self.app, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tenant_db(self, tenant_id):
        return connect(self.config.tenants_dir / tenant_id / "tenant.db")

    def platform_db(self):
        return connect(self.config.platform_db_path)

    def create_platform_admin(self, email="root@example.com", password=PASSWORD):
        conn = self.platform_db()
        try:
            conn.execute("INSERT INTO platform_admins(id, email, name, password_hash, created_at) VALUES(?,?,?,?,?)",
                         (new_id(), email, "Root", auth.hash_password(password), now_iso()))
        finally:
            conn.close()

    def add_user(self, tenant_id, email, role, password=PASSWORD):
        """Insert a staff user directly (the user-management API arrives in a later step)."""
        tdb = self.tenant_db(tenant_id)
        pdb = self.platform_db()
        try:
            role_id = tdb.execute("SELECT id FROM roles WHERE name=?", (role,)).fetchone()["id"]
            uid = new_id()
            tdb.execute("INSERT INTO users(id, email, name, role_id, password_hash, created_at) VALUES(?,?,?,?,?,?)",
                        (uid, email, role.title(), role_id, auth.hash_password(password), now_iso()))
            pdb.execute("INSERT INTO staff_identities(email, tenant_id, user_id, created_at) VALUES(?,?,?,?)",
                        (email, tenant_id, uid, now_iso()))
            return uid
        finally:
            tdb.close()
            pdb.close()


class Resp:
    def __init__(self, status, js, raw, headers):
        self.status, self.json, self.raw, self.headers = status, js, raw, headers

    def set_cookies(self):
        return self.headers.get_all("Set-Cookie") or []


class Client:
    def __init__(self, ts):
        self.ts = ts
        self.cookies = {}
        self.csrf = ""

    def request(self, method, path, body=None, headers=None, use_csrf=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.ts.port, timeout=15)
        h = {}
        if self.cookies:
            h["Cookie"] = "; ".join("%s=%s" % kv for kv in self.cookies.items())
        data = None
        if body is not None:
            data = json.dumps(body)
            h["Content-Type"] = "application/json"
        if method != "GET" and use_csrf and self.csrf:
            h["X-CSRF-Token"] = self.csrf
        h.update(headers or {})
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        raw = r.read()
        for sc in r.msg.get_all("Set-Cookie") or []:
            name, _, rest = sc.partition("=")
            if "Max-Age=0" in sc:
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = rest.split(";")[0]
        try:
            js = json.loads(raw)
        except ValueError:
            js = None
        conn.close()
        return Resp(r.status, js, raw, r.msg)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body if body is not None else {}, **kw)

    def put(self, path, body=None, **kw):
        return self.request("PUT", path, body if body is not None else {}, **kw)

    def register(self, business, email, shop_type="computer", password=PASSWORD, name="Owner"):
        r = self.post("/api/register", {"business_name": business, "shop_type": shop_type, "name": name,
                                        "email": email, "password": password})
        if r.status == 200:
            self.csrf = r.json["csrf"]
        return r

    def login(self, email, password=PASSWORD):
        r = self.post("/api/login", {"email": email, "password": password})
        if r.status == 200:
            self.csrf = r.json["csrf"]
        return r

    @property
    def tenant_id(self):
        return self.cookies["sd_session"].split(".")[1]
