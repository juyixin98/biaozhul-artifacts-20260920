"""End-to-end tests for the JSON HTTP service (stdlib only)."""
import json
import threading
import unittest
import urllib.request
import urllib.error

from regex_automata.service import make_server


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _post(self, path, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _get(self, path):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}{path}", timeout=5
        ) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_health(self):
        status, body = self._get("/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("version", body)

    def test_match_search(self):
        status, body = self._post("/match", {
            "pattern": r"(ab|a)*b", "text": "aab", "mode": "search"})
        self.assertEqual(status, 200)
        self.assertTrue(body["matched"])
        self.assertEqual(body["match"], {"start": 0, "end": 3, "matched": "aab"})
        self.assertGreater(body["nfa_states"], 2)

    def test_match_fullmatch_and_no_match(self):
        status, body = self._post("/match",
                                  {"pattern": "abc", "text": "abc", "mode": "fullmatch"})
        self.assertEqual(status, 200)
        self.assertTrue(body["matched"])

        status, body = self._post("/match",
                                  {"pattern": "abc", "text": "xabc", "mode": "fullmatch"})
        self.assertTrue(status == 200 and body["matched"] is False and body["match"] is None)

    def test_match_default_mode_is_search(self):
        status, body = self._post("/match", {"pattern": "b", "text": "abc"})
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], "search")
        self.assertEqual(body["match"]["start"], 1)

    def test_unicode_roundtrip(self):
        status, body = self._post("/match", {
            "pattern": r"\w+", "text": "日本語 é", "mode": "prefix"})
        self.assertEqual(status, 200)
        self.assertEqual(body["match"]["matched"], "日本語")

    def test_compile_returns_ast_and_nfa(self):
        status, body = self._post("/compile", {"pattern": "a|b"})
        self.assertEqual(status, 200)
        self.assertEqual(body["ast"]["kind"], "alt")
        self.assertIn("states", body["nfa"])
        self.assertIn("start", body["nfa"])
        self.assertIn("accept", body["nfa"])

    def test_invalid_pattern_reports_position(self):
        status, body = self._post("/compile", {"pattern": "  (ab"})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["type"], "ParseError")
        self.assertEqual(body["error"]["line"], 1)
        self.assertEqual(body["error"]["column"], 3)
        self.assertEqual(body["error"]["span"], [2, 3])

    def test_bad_requests(self):
        status, body = self._post("/match", {"pattern": 123})
        self.assertEqual(status, 400)
        self.assertIn("pattern", body["error"]["message"])

        status, body = self._post("/match", {"pattern": "a", "mode": "bogus"})
        self.assertEqual(status, 400)

        status, body = self._post("/compile", {"pattern": "a{3,2}"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["type"], "LexError")

    def test_malformed_json(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/match",
            data=b"{not json", headers={"Content-Type": "application/json"},
            method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_unknown_route(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/nope", timeout=5)
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
