"""JSON service tests: request handling, error payloads, HTTP endpoint."""
from __future__ import annotations

import json
import threading
import unittest
import urllib.request
from http.server import HTTPServer

from ssa_toolchain.service import handle_request, make_http_handler

from .helpers import load_example


class HandleRequestTests(unittest.TestCase):
    def test_compile_and_execute(self):
        req = {"source": load_example("diamond.mini"),
               "inputs": [8]}
        resp = handle_request(req)
        self.assertTrue(resp["ok"], resp)
        result = resp["result"]
        self.assertIn("ir_ssa", result["functions"]["main"])
        ex = result["executions"]["main"]
        self.assertTrue(ex["agree"])
        self.assertEqual(ex["flat"]["return"], 9)

    def test_single_definition_notes_present(self):
        resp = handle_request({"source": load_example("sum_loop.mini"),
                               "inputs": [3]})
        self.assertTrue(resp["ok"])
        verify = resp["result"]["functions"]["main"]["verify"]
        self.assertIn("single-definition ok", verify)

    def test_unreachable_reported(self):
        resp = handle_request({"source": load_example("unreachable.mini"),
                               "inputs": [0]})
        self.assertTrue(resp["ok"], resp)
        info = resp["result"]["functions"]["main"]
        self.assertIn("b.if1.else",
                      info["unreachable_blocks_pruned"])

    def test_syntax_error_payload(self):
        resp = handle_request({"source": "func main( { return 1; }"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["kind"], "ParseError")
        self.assertIsNotNone(resp["error"]["loc"])

    def test_use_of_undeclared(self):
        resp = handle_request(
            {"source": "func main() { return y; }"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["kind"], "LoweringError")

    def test_missing_source(self):
        resp = handle_request({})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["kind"], "BadRequest")

    def test_omit_ir(self):
        resp = handle_request({"source": load_example("diamond.mini"),
                               "inputs": [1], "include_ir": False})
        self.assertNotIn("ir_ssa", resp["result"]["functions"]["main"])


class HttpServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), make_http_handler())
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=2)

    def _post(self, payload):
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/compile", data=data,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())

    def test_health(self):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/health", timeout=5) as r:
            body = json.loads(r.read().decode())
        self.assertTrue(body["ok"])

    def test_compile_over_http(self):
        body = self._post({"source": load_example("swap_loop.mini"),
                           "inputs": [2]})
        self.assertTrue(body["ok"], body)
        self.assertTrue(body["result"]["executions"]["main"]["agree"])

    def test_bad_json(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/compile", data=b"{not json",
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)


if __name__ == "__main__":
    unittest.main()
