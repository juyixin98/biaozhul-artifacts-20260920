"""End-to-end tests of the JSON HTTP service against a live local server."""

import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from interval_ai import service


def _post(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class TestService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), service._Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def test_healthz(self):
        with urllib.request.urlopen(self.base + "/healthz", timeout=5) as r:
            body = json.loads(r.read())
        self.assertEqual(body["status"], "ok")

    def test_analyze_guarded_safe(self):
        code, body = _post(self.base + "/analyze",
                           {"source": "input var k; if(k>0){var z=9/k;}"})
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["alarms"], [])

    def test_analyze_reports_possible_divzero(self):
        code, body = _post(self.base + "/analyze",
                           {"source": "input var k; var z = 9/k;"})
        self.assertEqual(code, 200)
        self.assertEqual(body["alarm_count"], 1)
        self.assertEqual(body["alarms"][0]["kind"], "div_by_zero")
        loc = body["alarms"][0]["location"]
        self.assertIn("line", loc)

    def test_analyze_syntax_error_is_400(self):
        code, body = _post(self.base + "/analyze", {"source": "var ;"})
        self.assertEqual(code, 400)
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["error"], "ParseError")
        self.assertIn("location", body)

    def test_invalid_json(self):
        req = urllib.request.Request(
            self.base + "/analyze", data=b"nope",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_missing_source(self):
        code, body = _post(self.base + "/analyze", {"nope": 1})
        self.assertEqual(code, 400)
        self.assertEqual(body["error"], "InvalidRequest")

    def test_run_ok(self):
        code, body = _post(self.base + "/run",
                           {"source": "input var k; var z=9/k;",
                            "inputs": [3]})
        self.assertEqual(code, 200)
        self.assertEqual(body["variables"]["z"], 3)
        self.assertNotIn("runtime_error", body)

    def test_run_divzero_reported(self):
        code, body = _post(self.base + "/run",
                           {"source": "input var k; var z=9/k;",
                            "inputs": [0]})
        self.assertEqual(code, 200)
        self.assertEqual(body["runtime_error"]["kind"], "div_by_zero")

    def test_run_rejects_non_integer_inputs(self):
        code, body = _post(self.base + "/run",
                           {"source": "var z=1;", "inputs": ["x"]})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
