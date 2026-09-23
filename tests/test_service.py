"""End-to-end tests for the JSON HTTP service."""

import json
import threading
import unittest
import urllib.request
import urllib.error

from slang.service import create_server


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = create_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=2)
        cls.server.server_close()

    def post(self, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_health(self):
        status, body = self.get("/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_parse_returns_tokens_ast_and_locations(self):
        status, body = self.post("/parse", {"source": "let x = 1;"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        toks = body["result"]["tokens"]
        self.assertEqual(toks[0]["kind"], "KEYWORD:let")
        self.assertIn("loc", toks[0])
        self.assertEqual(body["result"]["ast"]["type"], "Program")

    def test_analyze(self):
        status, body = self.post("/analyze", {"source": "print(1);"})
        self.assertEqual(status, 200)
        self.assertIn("frames", body["result"]["analysis"])

    def test_lower(self):
        src = "fn f(){ let x = 0; return fn(){x = x+1; return x;}; }"
        status, body = self.post("/lower", {"source": src})
        self.assertEqual(status, 200)
        mod = body["result"]["module"]
        self.assertIn("main", mod)
        self.assertIn("funcs", mod)

    def test_run_ir(self):
        status, body = self.post("/run/ir", {"source": "print(1 + 2);"})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["trace"], ["3"])

    def test_run_source(self):
        status, body = self.post("/run/source", {"source": 'print("hi");'})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["trace"], ["hi"])

    def test_compare_agrees(self):
        src = """
fn pair() { let n = 0;
  let inc = fn() { n = n + 1; return n; };
  let dec = fn() { n = n - 1; return n; };
  return fn(op) { if (op==0){return inc();} return dec(); };
}
let p = pair();
print(p(0));
print(p(0));
print(p(1));
"""
        status, body = self.post("/compare", {"source": src})
        self.assertEqual(status, 200)
        r = body["result"]
        self.assertTrue(r["agree"])
        self.assertEqual(r["ir_trace"], ["1", "2", "1"])

    def test_error_has_location(self):
        status, body = self.post("/run/ir", {"source": "print(missing);"})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        err = body["error"]
        self.assertEqual(err["phase"], "compile")
        self.assertIn("loc", err)
        self.assertEqual(err["loc"]["line"], 1)

    def test_runtime_error(self):
        status, body = self.post("/run/ir", {"source": "print(1/0);"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["phase"], "runtime")

    def test_parse_error(self):
        status, body = self.post("/parse", {"source": "let = 1;"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["phase"], "parse")

    def test_bad_json(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/run/ir",
            data=b"{not json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req)
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_unknown_endpoint(self):
        status, _ = self.post("/nope", {"source": ""})
        self.assertEqual(status, 404)

    def test_missing_source(self):
        status, body = self.post("/run/ir", {})
        self.assertEqual(status, 400)
        self.assertIn("source", body["error"]["message"])


if __name__ == "__main__":
    unittest.main()
