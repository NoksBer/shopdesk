"""Request/Response value objects (transport independent) and the router."""
import json
import re
from http.cookies import SimpleCookie

from .permissions import ACCESS_LEVELS, PERMISSIONS
from .util import ApiError


class Request:
    def __init__(self, method, path, query, headers, body, ip, https):
        self.method = method
        self.path = path
        self.query = query          # dict of str -> list[str]
        self.headers = headers      # case-insensitive mapping
        self.body = body            # bytes
        self.ip = ip
        self.https = https
        self._cookies = None

    @property
    def cookies(self):
        if self._cookies is None:
            jar = SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie", ""))
            except Exception:
                jar = SimpleCookie()
            self._cookies = {k: m.value for k, m in jar.items()}
        return self._cookies

    def json(self):
        if not self.body:
            return {}
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(415, "Content-Type must be application/json")
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "Invalid JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "JSON body must be an object")
        return data

    def query_int(self, name, default, minimum, maximum):
        try:
            value = int(self.query.get(name, [default])[0])
        except (ValueError, TypeError):
            value = default
        return max(minimum, min(maximum, value))


class Response:
    def __init__(self, status=200, body=b"", content_type="application/json", headers=None):
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = list(headers or [])
        self.cookies = []           # full Set-Cookie header values

    @classmethod
    def json(cls, data, status=200):
        return cls(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    @classmethod
    def from_result(cls, result):
        if isinstance(result, Response):
            return result
        if isinstance(result, tuple):
            return cls.json(result[1], result[0])
        return cls.json(result if result is not None else {"ok": True})


# ---------------------------------------------------------------- router
class Route:
    def __init__(self, method, path, handler, access):
        if access not in ACCESS_LEVELS and access not in PERMISSIONS:
            raise ValueError("route %s %s declares unknown access %r" % (method, path, access))
        self.method = method
        self.path = path
        self.handler = handler
        self.access = access
        pattern = re.sub(r"<(\w+)>", r"(?P<\1>[A-Za-z0-9_-]{1,64})", path)
        self.regex = re.compile("^" + pattern + "$")


class Router:
    def __init__(self):
        self.routes = []

    def add(self, method, path, handler, access):
        """Every route MUST declare its access level: public, authed, platform, or a permission."""
        self.routes.append(Route(method, path, handler, access))

    def match(self, method, path):
        path_matched = False
        for route in self.routes:
            m = route.regex.match(path)
            if not m:
                continue
            path_matched = True
            if route.method == method:
                return "ok", route, m.groupdict()
        return ("method" if path_matched else "none"), None, None
