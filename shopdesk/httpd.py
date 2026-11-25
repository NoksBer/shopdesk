"""HTTP transport: turns sockets into Request objects and Response objects back into bytes."""
import ipaddress
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .http_util import Request


def _client_ip(handler, trust_proxy):
    peer = handler.client_address[0]
    if trust_proxy:
        candidates = [handler.headers.get("CF-Connecting-IP")]
        xff = handler.headers.get("X-Forwarded-For")
        if xff:
            candidates.append(xff.split(",")[-1].strip())      # last hop = added by our proxy
        for c in candidates:
            if c:
                try:
                    return str(ipaddress.ip_address(c.strip()))
                except ValueError:
                    continue
    return peer


def make_handler(app):
    cfg = app.config

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "ShopDesk"
        sys_version = ""

        def log_message(self, *args):   # replaced by structured log below
            pass

        def _send(self, status, body, content_type, headers=(), cookies=(), close=False):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for k, v in headers:
                self.send_header(k, v)
            for c in cookies:
                self.send_header("Set-Cookie", c)
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _handle(self):
            start = time.time()
            raw_target = self.path
            if raw_target.startswith("//"):          # avoid "//host/x" being parsed as a netloc
                raw_target = "/" + raw_target.lstrip("/")
            parts = urlsplit(raw_target)
            body = b""
            if self.headers.get("Transfer-Encoding"):
                return self._send(411, b'{"error":"Length required"}', "application/json", close=True)
            length = self.headers.get("Content-Length")
            if length:
                try:
                    n = int(length)
                except ValueError:
                    return self._send(400, b'{"error":"Bad Content-Length"}', "application/json", close=True)
                if n < 0 or n > cfg.max_body_bytes:
                    return self._send(413, b'{"error":"Request too large"}', "application/json", close=True)
                body = self.rfile.read(n)
            https = cfg.trust_proxy and self.headers.get("X-Forwarded-Proto", "").lower() == "https"
            req = Request(self.command, parts.path, parse_qs(parts.query), self.headers, body,
                          _client_ip(self, cfg.trust_proxy), https)
            resp = app.handle(req)
            self._send(resp.status, resp.body, resp.content_type, resp.headers, resp.cookies)
            # Structured log: no query string, no body, no cookies.
            sys.stderr.write(json.dumps({"ts": int(start), "method": req.method, "path": req.path,
                                         "status": resp.status, "ms": int((time.time() - start) * 1000),
                                         "ip": req.ip}) + "\n")

        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = _handle

    return Handler


def make_server(app, host=None, port=None):
    return ThreadingHTTPServer((host or app.config.host, app.config.port if port is None else port),
                               make_handler(app))


def serve(app):
    server = make_server(app)
    sys.stderr.write(json.dumps({"level": "info", "msg": "listening",
                                 "host": app.config.host, "port": app.config.port}) + "\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
