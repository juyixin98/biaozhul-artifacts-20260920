"""End-to-end tests for the JSON HTTP service (starts a real server)."""
import json
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

from sclang.service import Handler, handle_request


COUNTER_SRC = (
    "fn counter(){ let c=0;"
    " fn inc(){c=c+1; return c;}"
    " return inc; }"
    "let f=counter(); print(f()); print(f());"
)


def _post(path, payload):
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


PORT = 0
server = None
thread = None


def setUpModule():
    global server, thread, PORT
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    PORT = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()


def tearDownModule():
    server.shutdown()
    server.server_close()


class TestService(unittest.TestCase):
    def test_health(self):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/healthz", timeout=5) as r:
            self.assertEqual(json.loads(r.read())["ok"], True)

    def test_eval_agreement(self):
        status, body = _post("/eval", {"source": COUNTER_SRC})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["output"], ["1", "2"])
        self.assertTrue(body["outputs_match"])

    def test_analyze_reports_boxing(self):
        status, body = _post("/analyze", {"source": COUNTER_SRC})
        self.assertEqual(status, 200)
        inc = [f for f in body["functions"] if f["name"] == "inc"][0]
        self.assertEqual(inc["free"][0]["name"], "c")
        self.assertTrue(inc["free"][0]["boxed"])

    def test_compile_returns_module_and_disassembly(self):
        status, body = _post("/compile", {"source": "fn f(){return 1;} f();"})
        self.assertEqual(status, 200)
        self.assertIn("functions", body["module"])
        self.assertIn("MAKE_CLOSURE", body["disassembly"])
        self.assertIn("CONST", body["disassembly"])

    def test_lex_positions(self):
        status, body = _post("/lex", {"source": "  abc"})
        self.assertEqual(status, 200)
        ident = [t for t in body["tokens"] if t["kind"] == "IDENT"][0]
        self.assertEqual(ident["span"][3], 3)  # 1-based column

    def test_parse_ast_has_spans(self):
        status, body = _post("/parse", {"source": "print(1);"})
        self.assertEqual(status, 200)
        self.assertEqual(body["ast"]["type"], "Program")
        self.assertTrue(body["ast"]["span"])

    def test_compile_error_has_span_and_snippet(self):
        status, body = _post("/eval", {"source": "print(missing);"})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        self.assertEqual(body["stage"], "compile")
        self.assertIsNotNone(body["span"])
        self.assertIn("missing", body["snippet"])

    def test_syntax_error(self):
        status, body = _post("/parse", {"source": "let ;"})
        self.assertEqual(status, 400)
        self.assertIn("span", body)

    def test_runtime_error_status(self):
        status, body = _post("/eval", {"source": "print(1/0);"})
        self.assertEqual(status, 422)
        self.assertEqual(body["stage"], "runtime")

    def test_bad_json(self):
        url = f"http://127.0.0.1:{PORT}/eval"
        req = urllib.request.Request(url, data=b"not json",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_unknown_route(self):
        status, body = _post("/nope", {"source": ""})
        self.assertEqual(status, 404)

    def test_source_must_be_string(self):
        status, body = _post("/eval", {"source": 123})
        self.assertEqual(status, 400)


class TestHandlerDirect(unittest.TestCase):
    """No network: exercise the pure request handler function."""

    def test_shadowing_program_matches(self):
        src = ("let x=1; {let x=2; print(x);} print(x);")
        status, body = handle_request("/eval", {"source": src})
        self.assertEqual(status, 200)
        self.assertEqual(body["output"], ["2", "1"])
        self.assertTrue(body["outputs_match"])


if __name__ == "__main__":
    unittest.main()
