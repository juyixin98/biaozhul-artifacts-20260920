"""End-to-end tests for the JSON HTTP service (stdlib server on an ephemeral port)."""

import json
import threading
import unittest
import urllib.request
import urllib.error

from miniml.service import make_server


def _post(url: str, payload: dict):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class TestService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = make_server("127.0.0.1", 0, log_requests=False)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def test_health(self):
        with urllib.request.urlopen(f"{self.base}/health", timeout=5) as r:
            body = json.loads(r.read())
        self.assertTrue(body["ok"])

    def test_infer_identity(self):
        status, body = _post(f"{self.base}/infer", {"source": "let id = fun x -> x ;;"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["bindings"][0]["type"], "forall a. a -> a")
        self.assertIn("trace", body)

    def test_infer_error_is_located_json(self):
        status, body = _post(f"{self.base}/infer", {"source": "1 + true"})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        err = body["error"]
        self.assertEqual(err["code"], "E003")
        self.assertEqual(err["location"]["start"]["line"], 1)
        self.assertEqual(err["location"]["start"]["column"], 5)
        self.assertIn("expected int", err["rendered"])

    def test_occurs_error_code(self):
        status, body = _post(f"{self.base}/infer", {"source": "fun x -> x x"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "E004")

    def test_value_restriction_flag(self):
        src = (
            "let r = ref (fun x -> x) in "
            "r := (fun n -> n + 1); let f = !r in f true"
        )
        status_on, body_on = _post(
            f"{self.base}/infer", {"source": src, "value_restriction": True}
        )
        status_off, body_off = _post(
            f"{self.base}/infer", {"source": src, "value_restriction": False}
        )
        self.assertEqual(status_on, 400)
        self.assertEqual(status_off, 200)
        self.assertTrue(body_off["ok"])

    def test_eval_endpoint_runs_program(self):
        status, body = _post(f"{self.base}/eval", {"source": "let rec fact = fun n -> if n <= 1 then 1 else n * fact (n-1) in fact 5"})
        self.assertEqual(status, 200)
        self.assertEqual(body["final_value"], "120")
        self.assertIn("eval_output", body)

    def test_missing_source_field(self):
        status, body = _post(f"{self.base}/infer", {})
        self.assertEqual(status, 400)
        self.assertIn("source", body["error"]["message"])

    def test_invalid_json(self):
        req = urllib.request.Request(
            f"{self.base}/infer",
            data=b"{not json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 400)

    def test_trace_can_be_turned_off(self):
        status, body = _post(
            f"{self.base}/infer",
            {"source": "let id = fun x -> x ;;", "trace": False},
        )
        self.assertEqual(status, 200)
        self.assertNotIn("trace", body)


if __name__ == "__main__":
    unittest.main()
