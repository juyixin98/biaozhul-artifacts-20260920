"""JSON HTTP 服务的端到端测试（标准库 urllib，线程内起服务）。"""

import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from byteverifier.service import _Handler


class _Server:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()

    def post(self, path: str, payload: dict):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())

    def get(self, path: str):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}{path}", timeout=5
        ) as resp:
            return resp.status, json.loads(resp.read().decode())


class TestService(unittest.TestCase):
    def test_health(self):
        with _Server() as s:
            code, body = s.get("/health")
            self.assertEqual(code, 200)
            self.assertTrue(body["ok"])

    def test_run_ok(self):
        with _Server() as s:
            _, body = s.post("/run", {
                "source": "void main() { print_int(1+2); }"
            })
            self.assertTrue(body["ok"], body)
            self.assertEqual(body["output"].strip(), "3")

    def test_verify_error_has_path(self):
        with _Server() as s:
            _, comp = s.post("/compile", {
                "source": "void main() { int x; print_int(x); }"
            })
            _, body = s.post("/verify", {"module_hex": comp["module_hex"]})
            self.assertFalse(body["ok"])
            self.assertEqual(body["error"]["kind"], "local.uninit")
            self.assertIn("shortest_path", body["error"])

    def test_mutate_oob(self):
        with _Server() as s:
            src = """
            void main() {
                int i = 0;
                while (i < 3) { print_int(i); i = i + 1; }
            }
            """
            _, comp = s.post("/compile", {"source": src})
            # 找到 while 回边并越界：直接对若干字节尝试，至少一次被拒
            rejected = False
            for byte in range(comp["module_size"]):
                _, body = s.post("/mutate", {
                    "module_hex": comp["module_hex"],
                    "byte": byte, "value": 255,
                })
                err = body.get("error") or {}
                if err.get("kind") in ("jump.oob", "jump.misaligned"):
                    rejected = True
                    break
            self.assertTrue(rejected, "至少一个变异应触发跳转边界错误")

    def test_bad_json_400(self):
        with _Server() as s:
            req = urllib.request.Request(
                f"http://127.0.0.1:{s.port}/run",
                data=b"not-json",
                headers={"Content-Type": "application/json"},
            )
            try:
                urllib.request.urlopen(req, timeout=5)
                self.fail()
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 400)

    def test_404(self):
        with _Server() as s:
            try:
                s.get("/nope")
                self.fail()
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 404)


if __name__ == "__main__":
    unittest.main()
