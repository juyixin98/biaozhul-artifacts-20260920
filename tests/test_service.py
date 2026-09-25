"""HTTP JSON service tests (in-process, no network sockets exposed)."""

import json
import threading
import unittest
import urllib.request
import urllib.error

from resflow.service import build_server


class ServerHarness:
    def __init__(self):
        self.httpd = build_server("127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def post_analyze(self, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url("/analyze"), data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def get(self, path):
        with urllib.request.urlopen(self.url(path), timeout=5) as resp:
            return resp.status, json.loads(resp.read())


CLEAN = "fn f(){ acquire(a); release(a); return; }"
LEAK = "fn f(){ acquire(a); return; }"
BAD = "fn f(){ acquire( }"


class TestService(unittest.TestCase):

    def test_health(self):
        with ServerHarness() as h:
            status, body = h.get("/health")
            self.assertEqual(status, 200)
            self.assertEqual(body["status"], "ok")

    def test_version(self):
        with ServerHarness() as h:
            status, body = h.get("/version")
            self.assertEqual(status, 200)
            self.assertIn("language_version", body)

    def test_analyze_clean(self):
        with ServerHarness() as h:
            status, body = h.post_analyze({"source": CLEAN})
            self.assertEqual(status, 200)
            self.assertEqual(body["functions"][0]["summary"]
                             ["diagnostic_count"], 0)

    def test_analyze_leak_reports_diagnostic(self):
        with ServerHarness() as h:
            status, body = h.post_analyze({"source": LEAK})
            self.assertEqual(status, 200)
            codes = [d["code"]
                     for d in body["functions"][0]["diagnostics"]]
            self.assertIn("resource_leak", codes)

    def test_parse_error_is_400_with_location(self):
        with ServerHarness() as h:
            status, body = h.post_analyze({"source": BAD})
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"],
                             "analysis_frontend_error")
            self.assertIn("location", body["error"])
            self.assertIn("line", body["error"]["location"])

    def test_missing_source_is_400(self):
        with ServerHarness() as h:
            status, body = h.post_analyze({})
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "missing_source")

    def test_invalid_json_is_400(self):
        with ServerHarness() as h:
            data = b"{not json"
            req = urllib.request.Request(
                h.url("/analyze"), data=data,
                headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req)
                self.fail("expected HTTPError")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 400)
                body = json.loads(e.read())
                self.assertEqual(body["error"]["code"], "invalid_json")

    def test_source_must_be_string(self):
        with ServerHarness() as h:
            status, body = h.post_analyze({"source": 123})
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "invalid_source")

    def test_invalid_loop_bound(self):
        with ServerHarness() as h:
            status, body = h.post_analyze(
                {"source": CLEAN, "loop_bound": -1})
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "invalid_option")

    def test_loop_bound_affects_result(self):
        src = "fn f(){ let c; while(c){acquire(a);release(a);} return; }"
        with ServerHarness() as h:
            _, b1 = h.post_analyze({"source": src, "loop_bound": 1})
            _, b5 = h.post_analyze({"source": src, "loop_bound": 5})
            self.assertLess(
                b1["functions"][0]["summary"]["path_count"],
                b5["functions"][0]["summary"]["path_count"])

    def test_unknown_route_404(self):
        with ServerHarness() as h:
            try:
                h.get("/nope")
                self.fail("expected 404")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 404)


if __name__ == "__main__":
    unittest.main()
