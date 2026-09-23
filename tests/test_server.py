"""JSON HTTP 服务测试（真实起停本地端口）。"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.request
import urllib.error

from tinyinfer.server import build_server


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = build_server("127.0.0.1", 0)  # 端口 0 = 操作系统分配
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                     daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def _post(self, body: dict) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/analyze",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def _get(self, path: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{path}", timeout=5
            ) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_health(self) -> None:
        status, body = self._get("/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_infer_identity(self) -> None:
        status, body = self._post({"source": "let id = fun x -> x in id 1"})
        self.assertEqual(status, 200)
        self.assertEqual(body["type"], "int")
        self.assertEqual(body["value"], "1")
        self.assertTrue(any(
            b["name"] == "id" and "forall" in b["scheme"]
            for b in body["bindings"]
        ))

    def test_type_error_returns_400_with_span(self) -> None:
        status, body = self._post({"source": "true + 1"})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        self.assertIsNotNone(body["error"]["span"])
        self.assertEqual(body["error"]["kind"], "UnifyError")

    def test_naive_mode_unsoundness(self) -> None:
        source = "let r = new_ref unit in r <- true; deref r + 1"
        status, body = self._post({"source": source,
                                   "value_restriction": False})
        self.assertEqual(status, 200)
        self.assertEqual(body["type"], "int")
        self.assertIsNotNone(body["eval_error"])

    def test_bad_request(self) -> None:
        status, body = self._post({"nope": 1})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["kind"], "BadRequest")

    def test_404(self) -> None:
        status, _ = self._get("/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
