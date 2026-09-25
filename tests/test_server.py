import json
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

from resflow.server import AnalysisHandler


def make_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), AnalysisHandler)
    server.log_requests = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def post_json(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.thread = make_server()
        host, port = cls.server.server_address
        cls.base = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_health(self):
        with urllib.request.urlopen(self.base + "/health", timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(payload["status"], "ok")

    def test_analyze_ok(self):
        src = 'fun main() { let f = acquire("x"); use(f); release f; }'
        status, payload = post_json(self.base + "/analyze", {"source": src})
        self.assertEqual(status, 200)
        self.assertEqual(payload["counts"]["findings"], 0)
        self.assertEqual(payload["language"], "resflow/1")

    def test_analyze_finding(self):
        src = 'fun main() { let f = acquire("x"); release f; release f; }'
        status, payload = post_json(self.base + "/analyze", {"source": src})
        self.assertEqual(status, 200)
        codes = {f["code"] for f in payload["findings"]}
        self.assertEqual(codes, {"DOUBLE_RELEASE"})

    def test_parse_error_is_400(self):
        status, payload = post_json(self.base + "/analyze", {"source": "fun main() {"})
        self.assertEqual(status, 400)
        self.assertIn("code", payload["error"])
        self.assertEqual(payload["error"]["code"], "E-PARSE")
        self.assertIsNotNone(payload["error"]["span"])

    def test_missing_source_is_400(self):
        status, payload = post_json(self.base + "/analyze", {"sauce": ""})
        self.assertEqual(status, 400)

    def test_invalid_json_is_400(self):
        req = urllib.request.Request(
            self.base + "/analyze", data=b"{not json", headers={"Content-Type": "application/json"}
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_loop_bound_option(self):
        src = "fun main(c) { while (c) {} }"
        status, payload = post_json(
            self.base + "/analyze", {"source": src, "loop_bound": 0}
        )
        self.assertEqual(status, 200)
        # bound 0: only zero-iteration normal path and one divergent cutoff
        kinds = [p["kind"] for p in payload["functions"][0]["paths"]]
        self.assertEqual(kinds, ["normal", "divergent"])

    def test_unknown_route_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/nope", timeout=5)
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
