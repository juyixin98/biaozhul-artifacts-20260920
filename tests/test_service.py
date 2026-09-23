"""JSON 服务的端到端测试：直接调用处理函数 + 起真实 HTTP socket 各测一遍。"""

import json
import threading
import unittest
import urllib.request

from slang import service


SRC_SUM = """
fn sumto(int n): int {
    var i = 1; var acc = 0;
    while (i <= n) { acc = acc + i; i = i + 1; }
    return acc;
}
fn main() { print sumto(10); }
"""


class TestServiceFunctions(unittest.TestCase):
    def test_compile_verify_run_pipeline(self):
        c = service.op_compile({"source": SRC_SUM, "encoding": "hex"})
        self.assertTrue(c["ok"], c)
        binary = c["module"]["binary"]
        self.assertIn("disasm", c["module"])

        v = service.op_verify({"module": {"binary": binary, "encoding": "hex"}})
        self.assertTrue(v["ok"], v)

        r = service.op_run({"module": {"binary": binary, "encoding": "hex"}})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["printed"], ["55"])

    def test_compile_error_has_position(self):
        r = service.op_compile({"source": "fn main( { }"})
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "compile")
        self.assertTrue(r["errors"])
        self.assertIsNotNone(r["errors"][0]["line"])

    def test_verify_reports_structured_path(self):
        c = service.op_compile({"source": SRC_SUM})
        binary = c["module"]["binary"]
        # 破坏一个跳转操作数字节（找 JUMP 0x15 并把偏移高位改 0x7F）
        raw = bytearray.fromhex(binary)
        idx = raw.find(b"\x15")
        self.assertGreater(idx, 0)
        raw[idx + 1] = 0x7F
        raw[idx + 2] = 0xFF
        r = service.op_verify(
            {"module": {"binary": raw.hex(), "encoding": "hex"}})
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "verify")
        err = r["errors"][0]
        self.assertIn(err["code"],
                      ("JUMP_OUT_OF_BOUNDS", "JUMP_UNALIGNED", "DECODE_ERROR"))
        self.assertIn("shortest_error_path", err)

    def test_mutate_endpoint(self):
        c = service.op_compile({"source": "fn main() { print 1; }"})
        binary = c["module"]["binary"]
        r = service.op_mutate(
            {"module": {"binary": binary}, "strategy": "targeted",
             "limit": 10})
        self.assertTrue(r["ok"], r)
        self.assertLessEqual(r["count"], 10)
        self.assertTrue(r["mutants"])

    def test_base64_roundtrip(self):
        c = service.op_compile({"source": SRC_SUM, "encoding": "base64"})
        self.assertTrue(c["ok"])
        v = service.op_verify({"module": {
            "binary": c["module"]["binary"], "encoding": "base64"}})
        self.assertTrue(v["ok"])

    def test_bad_hex(self):
        r = service.op_verify({"module": {"binary": "zzz", "encoding": "hex"}})
        self.assertFalse(r["ok"])


class TestHTTPServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = service.serve("127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def post(self, path, obj):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())

    def test_health(self):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/health", timeout=5) as r:
            self.assertTrue(json.loads(r.read())["ok"])

    def test_compile_and_run_over_http(self):
        c = self.post("/compile", {"source": "fn main() { print 6 * 7; }"})
        self.assertTrue(c["ok"])
        r = self.post("/run", {"module": c["module"]})
        self.assertEqual(r["printed"], ["42"])

    def test_404(self):
        import urllib.error
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/nope", timeout=5)
            self.fail("应当 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)


if __name__ == "__main__":
    unittest.main()
