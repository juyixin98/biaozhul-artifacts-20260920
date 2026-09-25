"""JSON 服务：真实起线程 HTTP 服务，用 urllib 打各端点。"""

import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from ssa_tool.service import Handler, ROUTES


DIAMOND = {"source": "func main(a){ var x; if(a>0){x=10;}else{x=20;} return x; }",
           "args": [4]}


class TestService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def post(self, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())

    def test_health(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
        self.assertTrue(body["ok"])
        for r in ("/parse", "/ssa", "/pipeline"):
            self.assertIn(r, body["routes"])

    def test_routes_complete(self):
        self.assertEqual(set(ROUTES),
                         {"/health", "/parse", "/build_ir", "/ssa",
                          "/eliminate", "/interpret", "/pipeline"})

    def test_parse(self):
        _, body = self.post("/parse", DIAMOND)
        self.assertTrue(body["ok"])
        self.assertEqual(body["program"]["node"], "Program")
        self.assertEqual(body["program"]["functions"][0]["name"], "main")

    def test_build_ir(self):
        _, body = self.post("/build_ir", DIAMOND)
        self.assertTrue(body["ok"])
        self.assertEqual(body["ir"]["flavor"], "raw")
        self.assertIn("br", body["text"])

    def test_ssa_route(self):
        _, body = self.post("/ssa", DIAMOND)
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["ir"]["flavor"], "ssa")
        self.assertEqual(body["violations"], [])
        # φ 确实出现
        self.assertIn("phi", body["text"])

    def test_eliminate(self):
        _, body = self.post("/eliminate", DIAMOND)
        self.assertTrue(body["ok"])
        self.assertEqual(body["ir"]["flavor"], "exec")
        self.assertNotIn("phi", body["text"])

    def test_interpret_flavors(self):
        for flavor, want in (("raw", 10), ("ssa", 10), ("exec", 10)):
            _, body = self.post("/interpret",
                                {"source": DIAMOND["source"],
                                 "args": [4], "flavor": flavor})
            self.assertTrue(body["ok"], body)
            self.assertEqual(body["value"], want)

    def test_pipeline_equivalence(self):
        _, body = self.post("/pipeline", DIAMOND)
        self.assertTrue(body["ok"], body)
        ex = body["execution"]
        self.assertEqual(ex["raw"]["value"], ex["ssa"]["value"])
        self.assertEqual(ex["ssa"]["value"], ex["exec"]["value"])
        self.assertTrue(ex["equivalent"])
        self.assertEqual(body["ssa_violations"], [])

    def test_pipeline_span_preserved(self):
        _, body = self.post("/pipeline", DIAMOND)
        # AST 中位置保留
        ast = body["ast"]
        self.assertIn("span", ast)
        # IR 指令也带 span（ret 来自源码）
        raw = body["raw_ir"]["json"]
        spans = [i["span"] for b in raw["blocks"] for i in b["instrs"]
                 if i["span"]]
        self.assertTrue(spans)

    def test_bad_source_400(self):
        import urllib.error
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/parse",
            data=json.dumps({"source": "func main({ var; }"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("应返回 400")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)
            body = json.loads(e.read())
            self.assertFalse(body["ok"])
            self.assertIn("message", body)

    def test_unknown_route(self):
        import urllib.error
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/nope",
            data=b"{}", headers={"Content-Type": "application/json"},
            method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail()
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)


if __name__ == "__main__":
    unittest.main()
