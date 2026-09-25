"""HTTP/JSON service tests (real socket, stdlib only)."""

import json
import unittest
import urllib.request
import urllib.error

from taintlang.service import create_server, run_analysis_payload, serve_in_thread


def post_json(url, obj):
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class TestPayloadLogic(unittest.TestCase):
    def test_analyze_payload_basic(self):
        result = run_analysis_payload(
            {"source": "func main() { sink(source()); }",
             "config": {"entry_points": ["main"]}})
        self.assertEqual(len(result["findings"]), 1)

    def test_missing_source_field(self):
        with self.assertRaises(ValueError):
            run_analysis_payload({"config": {}})

    def test_body_must_be_object(self):
        with self.assertRaises(ValueError):
            run_analysis_payload(["not", "an", "object"])

    def test_program_error_envelope(self):
        from taintlang.errors import ParseError
        with self.assertRaises(ParseError):
            run_analysis_payload({"source": "func main("})


class TestHTTPService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.thread, cls.host, cls.port = serve_in_thread(
            "127.0.0.1", 0)
        cls.base = f"http://{cls.host}:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_health(self):
        with urllib.request.urlopen(f"{self.base}/health", timeout=5) as r:
            body = json.loads(r.read())
        self.assertEqual(body["status"], "ok")

    def test_analyze_ok(self):
        status, body = post_json(f"{self.base}/analyze", {
            "source": "func main() { sink(source()); }",
            "config": {"entry_points": ["main"], "k": 1},
        })
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["result"]["findings"]), 1)

    def test_analyze_parse_error_is_400(self):
        status, body = post_json(f"{self.base}/analyze",
                                 {"source": "func main("})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["type"], "ParseError")
        self.assertIn("span", body["error"])

    def test_bad_json_is_422(self):
        data = b"{not json"
        req = urllib.request.Request(
            f"{self.base}/analyze", data=data, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 422)
            body = json.loads(exc.read())
            self.assertEqual(body["error"]["type"], "BadJson")

    def test_missing_field_is_422(self):
        status, body = post_json(f"{self.base}/analyze", {"nope": 1})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["type"], "BadRequest")

    def test_unknown_path_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(f"{self.base}/nope", timeout=5)
        self.assertEqual(cm.exception.code, 404)

    def test_include_ir_false_omits_ir(self):
        status, body = post_json(f"{self.base}/analyze", {
            "source": "func main() { sink(source()); }",
            "config": {"entry_points": ["main"]},
            "include_ir": False,
        })
        self.assertEqual(status, 200)
        self.assertNotIn("ir", body["result"])


if __name__ == "__main__":
    unittest.main()
