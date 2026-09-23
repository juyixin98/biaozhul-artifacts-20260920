"""JSON 服务的端到端测试：在真实 HTTP 端口上打请求。"""

import json
import threading
import unittest
import urllib.request
import urllib.error

from dl.server import serve


def _request(method, url, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = serve("127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_health(self):
        status, body = _request("GET", f"{self.base}/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_parse_one_shot(self):
        status, body = _request("POST", f"{self.base}/parse",
                                {"source": "var x = 1 + 2;"})
        self.assertEqual(status, 200)
        self.assertEqual(body["tree"]["kind"], "Program")
        self.assertEqual(body["errors"], [])

    def test_parse_reports_unclosed_paren_position(self):
        status, body = _request("POST", f"{self.base}/parse",
                                {"source": "x = (1;"})
        self.assertEqual(status, 200)
        msgs = [e["message"] for e in body["errors"]]
        self.assertTrue(any("未闭合的括号" in m for m in msgs))
        # 错误位置是半开区间
        err = next(e for e in body["errors"] if "未闭合的括号" in e["message"])
        self.assertEqual(err["end"] - err["start"], 1)

    def test_document_lifecycle_and_incremental_edit(self):
        # 创建
        status, body = _request("POST", f"{self.base}/documents",
                                {"source": "var a = 1;\nvar b = 2;"})
        self.assertEqual(status, 201)
        doc_id = body["id"]
        # 编辑：前面插入一条声明，后面的声明应被复用
        status, body = _request(
            "POST", f"{self.base}/documents/{doc_id}/edits",
            {"edits": [{"start": 0, "end": 0, "text": "var z = 0;\n"}]})
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], 1)
        self.assertGreater(body["reuse_events"], 0)
        self.assertEqual(len(body["tree"]["children"]), 3)
        # GET
        status, body = _request("GET", f"{self.base}/documents/{doc_id}")
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], 1)
        # 删除
        status, body = _request("DELETE",
                                f"{self.base}/documents/{doc_id}")
        self.assertEqual(status, 200)
        status, body = _request("GET", f"{self.base}/documents/{doc_id}")
        self.assertEqual(status, 404)

    def test_sequential_edits_match_full_parse(self):
        status, body = _request("POST", f"{self.base}/documents",
                                {"source": "var x = (1 + 2);"})
        doc_id = body["id"]
        # 删除 ')' 制造未闭合错误
        p = body["source"].index(")")
        _request("POST", f"{self.base}/documents/{doc_id}/edits",
                 {"start": p, "end": p + 1, "text": ""})
        _, broken = _request("GET", f"{self.base}/documents/{doc_id}")
        self.assertTrue(any("未闭合的括号" in e["message"]
                            for e in broken["errors"]))
        # 与一次性全量解析结果比较错误集合
        _, full = _request("POST", f"{self.base}/parse",
                           {"source": broken["source"]})
        self.assertEqual(
            sorted((e["message"], e["start"], e["end"])
                   for e in broken["errors"]),
            sorted((e["message"], e["start"], e["end"])
                   for e in full["errors"]))

    def test_bad_json(self):
        status, body = _request("POST", f"{self.base}/parse", None)
        # 空 body -> 当作 {} 处理（source 默认空串），不应 500
        self.assertEqual(status, 200)

    def test_single_edit_shorthand(self):
        _, created = _request("POST", f"{self.base}/documents",
                              {"source": "var a = 1;"})
        doc_id = created["id"]
        status, body = _request(
            "POST", f"{self.base}/documents/{doc_id}/edits",
            {"start": 0, "end": 0, "text": "x;"})
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], 1)


if __name__ == "__main__":
    unittest.main()
