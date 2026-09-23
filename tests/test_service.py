"""JSON HTTP 服务测试（直接调用 handle_request，并起真实 socket 自测）。"""

import json
import threading
import unittest
import urllib.request

from constprop.service import handle_request, make_server


PROG = "x = 40;\nx = x + 2;\nif (1) { print x; } else { print 9; }\n"


class TestHandler(unittest.TestCase):
    def test_health(self):
        r = handle_request({"endpoint": "health"})
        self.assertTrue(r["ok"])

    def test_parse_returns_ast_with_spans(self):
        r = handle_request({"endpoint": "parse", "source": "x = 1;"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["ast"]["type"], "Program")
        self.assertIn("span", r["ast"])

    def test_analyze_reports_constants(self):
        r = handle_request({"endpoint": "analyze", "source": PROG})
        self.assertTrue(r["ok"])
        consts = r["sccp"]["constants"]
        # 某个 x 版本应为 42
        self.assertIn(42, consts.values())
        # else 分支不可达
        reachable = r["sccp"]["reachable_blocks"]
        self.assertFalse(any(b.startswith("else") for b in reachable))

    def test_optimize_equivalent(self):
        r = handle_request({"endpoint": "optimize", "source": PROG})
        self.assertTrue(r["ok"])
        self.assertTrue(r["equivalent"])
        self.assertEqual(r["run_after_ir"]["output"], [42])
        self.assertIn("ir_after_text", r)

    def test_run_reports_divzero_structured(self):
        r = handle_request({
            "endpoint": "run",
            "source": "print 1;\nz = 5/0;\nprint 2;\n",
        })
        self.assertTrue(r["ok"])  # 传输成功
        run = r["run"]
        self.assertFalse(run["ok"])  # 程序运行期错误
        self.assertEqual(run["error_code"], "division-by-zero")
        self.assertEqual(run["output"], [1])

    def test_parse_error_is_structured_200(self):
        r = handle_request({"endpoint": "parse", "source": "x = ;"})
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["error"], "parse error")
        self.assertIsNotNone(r["error"]["location"])

    def test_bad_source_type(self):
        r = handle_request({"endpoint": "run", "source": 123})
        self.assertFalse(r["ok"])

    def test_unknown_endpoint(self):
        r = handle_request({"endpoint": "nope", "source": ""})
        self.assertFalse(r["ok"])


class TestHTTPServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _post(self, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            return json.loads(resp.read().decode("utf-8"))

    def test_get_health(self):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/health", timeout=5) as r:
            self.assertTrue(json.loads(r.read())["ok"])

    def test_post_optimize_over_http(self):
        r = self._post("/optimize", {"source": PROG})
        self.assertTrue(r["ok"])
        self.assertTrue(r["equivalent"])

    def test_invalid_json_body_is_400(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/run",
            data=b"not json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected HTTP 400")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)


if __name__ == "__main__":
    unittest.main()
