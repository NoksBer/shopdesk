"""Static serving: cache busting (?v=VERSION), cache headers, ETag revalidation."""
import re
import unittest

from harness import Client, TestServer
from shopdesk import __version__


class StaticCache(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = TestServer()

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def get(self, path, **headers):
        return Client(self.ts).get(path, headers=headers)

    def test_html_references_are_versioned_and_html_revalidates(self):
        for page in ("/", "/admin.html"):
            r = self.get(page)
            self.assertEqual(r.status, 200, page)
            self.assertEqual(r.headers["Cache-Control"], "no-cache", page)
            html = r.raw.decode("utf-8")
            refs = re.findall(r'(?:src|href)="(/[^"]+\.(?:js|css|svg))"', html)
            self.assertTrue(refs, page)
            for ref in refs:
                self.assertTrue(ref.endswith("?v=" + __version__), ref)

    def test_versioned_static_url_gets_long_cache_and_etag(self):
        r = self.get("/common.js?v=" + __version__)
        self.assertEqual(r.status, 200)
        self.assertIn("max-age=31536000", r.headers["Cache-Control"])
        self.assertTrue(r.headers["ETag"])

    def test_unversioned_or_wrong_version_static_url_revalidates(self):
        for path in ("/common.js", "/common.js?v=0.0.0-other"):
            r = self.get(path)
            self.assertEqual(r.headers["Cache-Control"], "no-cache", path)
            self.assertTrue(r.headers["ETag"], path)

    def test_etag_revalidation_returns_304(self):
        etag = self.get("/style.css").headers["ETag"]
        r = self.get("/style.css", **{"If-None-Match": etag})
        self.assertEqual(r.status, 304)
        self.assertEqual(r.raw, b"")
        self.assertEqual(self.get("/style.css", **{"If-None-Match": '"stale"'}).status, 200)

    def test_api_responses_revalidate(self):
        self.assertEqual(self.get("/api/version").headers["Cache-Control"], "no-cache")

    def test_logo_is_served_as_svg(self):
        r = self.get("/logo.svg")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.headers["Content-Type"].startswith("image/svg+xml"))
        self.assertNotIn(b"http://", r.raw.replace(b"http://www.w3.org/2000/svg", b""))   # no network references


if __name__ == "__main__":
    unittest.main()
