"""JSON 服务测试：直接测试 run_request 的请求/响应契约。"""

import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from renfa.service import _Handler, run_request


class RunRequestTests(unittest.TestCase):
    def test_search_success(self):
        resp, code = run_request({"pattern": "a{1,2}", "text": "xaa",
                                  "op": "search"})
        self.assertEqual(code, 200)
        self.assertTrue(resp["ok"])
        self.assertTrue(resp["matched"])
        self.assertEqual(resp["match"], {"start": 1, "end": 3, "text": "aa"})

    def test_findall(self):
        resp, code = run_request({"pattern": "a|b", "text": "ab",
                                  "op": "findall"})
        self.assertEqual(code, 200)
        self.assertEqual([m["text"] for m in resp["matches"]], ["a", "b"])

    def test_fullmatch_no_match(self):
        resp, code = run_request({"pattern": "^abc$", "text": "abcd",
                                  "op": "fullmatch"})
        self.assertEqual(code, 200)
        self.assertFalse(resp["matched"])
        self.assertIsNone(resp["match"])

    def test_unicode_match_offsets(self):
        resp, code = run_request({"pattern": "中+", "text": "x中中",
                                  "op": "search"})
        self.assertEqual(resp["match"], {"start": 1, "end": 3, "text": "中中"})

    def test_syntax_error_400_with_location(self):
        resp, code = run_request({"pattern": "a(", "text": "a"})
        self.assertEqual(code, 400)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["type"], "syntax")
        self.assertIn("message", resp["error"])
        self.assertIsNotNone(resp["error"]["location"])

    def test_unsupported_feature_error(self):
        resp, code = run_request({"pattern": r"\d+", "text": "1"})
        self.assertEqual(code, 400)
        self.assertEqual(resp["error"]["type"], "syntax")

    def test_compile_limit_error(self):
        resp, code = run_request({"pattern": "a{99999}", "text": ""})
        self.assertEqual(code, 400)
        self.assertEqual(resp["error"]["type"], "compile")

    def test_bad_request_shapes(self):
        self.assertEqual(run_request("notdict")[1], 400)
        self.assertEqual(run_request({"text": "x"})[1], 400)
        self.assertEqual(run_request({"pattern": 1})[1], 400)
        self.assertEqual(run_request({"pattern": "a", "text": 1})[1], 400)
        self.assertEqual(run_request({"pattern": "a", "op": "bogus"})[1], 400)

    def test_debug_payload(self):
        resp, code = run_request({"pattern": "ab|c", "text": "ab",
                                  "op": "fullmatch", "debug": True})
        self.assertEqual(code, 200)
        self.assertIn("tokens", resp)
        self.assertIn("ast", resp)
        self.assertIn("nfa", resp)
        self.assertEqual(resp["ast"]["type"], "Alt")
        self.assertGreater(len(resp["nfa"]["states"]), 2)


class HttpServerTests(unittest.TestCase):
    """真正起一个线程内 HTTP 服务，走一次网络往返。"""

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.httpd.verbose = False
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def _post(self, obj, raw=None):
        url = f"http://127.0.0.1:{self.port}/match"
        data = raw if raw is not None else json.dumps(obj).encode("utf-8")
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_healthz(self):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/healthz") as r:
            body = json.loads(r.read())
            self.assertEqual(r.status, 200)
            self.assertTrue(body["ok"])

    def test_post_roundtrip(self):
        code, body = self._post({"pattern": "(ab)+c", "text": "ababc",
                                 "op": "fullmatch"})
        self.assertEqual(code, 200)
        self.assertTrue(body["matched"])

    def test_malformed_json(self):
        code, body = self._post(None, raw=b"{not json")
        self.assertEqual(code, 400)
        self.assertEqual(body["error"]["type"], "request")

    def test_unknown_route(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/nope", method="GET")
        try:
            urllib.request.urlopen(req)
            self.fail("应返回 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)


if __name__ == "__main__":
    unittest.main()
