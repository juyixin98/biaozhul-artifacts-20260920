import json
import threading
import unittest
import urllib.request
import urllib.error

import server


def post(url, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def get(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


class ServerTests(unittest.TestCase):
    httpd = None
    thread = None
    base = ""

    @classmethod
    def setUpClass(cls):
        cls.httpd = server.serve("127.0.0.1", 0)
        port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def test_parse_endpoint(self):
        status, body = post(self.base + "/parse", {"text": "let x = 1 + 2;"})
        self.assertEqual(status, 200)
        self.assertEqual(body["tree"]["kind"], "program")
        self.assertEqual(body["diagnostics"], [])
        let = body["tree"]["children"][0]
        self.assertEqual(let["kind"], "let")
        self.assertEqual(let["span"]["start_line"], 0)

    def test_parse_reports_error_positions(self):
        status, body = post(self.base + "/parse",
                            {"text": "let x = (1;"})
        self.assertEqual(status, 200)
        self.assertTrue(body["diagnostics"])
        d = body["diagnostics"][0]
        self.assertEqual(d["message"], "unclosed '('")
        self.assertEqual((d["start"], d["end"]), (8, 9))
        self.assertEqual((d["start_line"], d["start_col"]), (0, 8))

    def test_document_edit_roundtrip_and_stats(self):
        status, body = post(self.base + "/documents",
                            {"text": "let a = 1;\nlet b = 2;"})
        self.assertEqual(status, 201)
        doc_id = body["id"]

        # insert a blank line between the two declarations
        status, body = post(
            self.base + f"/documents/{doc_id}/edit",
            {"start": 10, "delete": 0, "insert": "\n"},
        )
        self.assertEqual(status, 200)
        self.assertIn("let b = 2;", body["text"])
        # both declarations untouched textually -> both reused
        self.assertEqual(body["stats"]["reused"], 2)
        self.assertEqual(body["stats"]["parsed"], 0)
        # spans shifted by one on the second declaration
        second = body["tree"]["children"][1]
        self.assertEqual(second["span"]["start"], 12)

        status, fetched = get(self.base + f"/documents/{doc_id}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["text"], body["text"])

    def test_bad_edit_returns_400(self):
        _, body = post(self.base + "/documents", {"text": "let a = 1;"})
        doc_id = body["id"]
        try:
            post(self.base + f"/documents/{doc_id}/edit",
                 {"start": 100, "delete": 0, "insert": "x"})
            self.fail("expected HTTP 400")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)
            payload = json.loads(e.read().decode("utf-8"))
            self.assertIn("outside", payload["error"])

    def test_unknown_document_404(self):
        try:
            get(self.base + "/documents/nope")
            self.fail("expected HTTP 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)


if __name__ == "__main__":
    unittest.main()
