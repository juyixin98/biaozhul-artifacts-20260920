"""Tests for the JSON service (stdio + HTTP)."""

import io
import json
import threading
import unittest
import urllib.request

from intervalai.service import handle, run_http, run_stdio


SRC = "input d;\nvar q = 0;\nif (d > 0) { q = 10 / d; }\n"


class TestService(unittest.TestCase):
    def test_analyze_ok(self):
        resp = handle({"op": "analyze", "source": SRC,
                       "input_bounds": {"d": {"lo": "-2", "hi": "3"}},
                       "include_points": False})
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(resp["alarms"], [])

    def test_analyze_big_integer_bound_as_string(self):
        resp = handle({"op": "analyze",
                       "source": "input x;\nvar y = x * 2;\n",
                       "input_bounds": {"x": {"lo": "1", "hi": "100000000000000000000"}}})
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(resp["exit_state"]["scalars"]["y"]["hi"],
                         "200000000000000000000")

    def test_execute(self):
        resp = handle({"op": "execute", "source": SRC, "inputs": {"d": 4}})
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(resp["scalars"]["q"], 2)

    def test_execute_fault(self):
        resp = handle({"op": "execute",
                       "source": "var z = 0;\nvar q = 1/z;\n",
                       "inputs": {}})
        self.assertEqual(resp["status"], "error")
        self.assertEqual(resp["error"], "execution_error")
        self.assertEqual(resp["kind"], "div_by_zero")
        self.assertEqual(resp["loc"]["line"], 2)

    def test_parse_error_structured(self):
        resp = handle({"op": "analyze", "source": "var x = ;\n"})
        self.assertEqual(resp["status"], "error")
        self.assertIn(resp["error"], ("parse_error", "lex_error"))

    def test_bad_bound(self):
        resp = handle({"op": "analyze", "source": SRC,
                       "input_bounds": {"d": {"lo": "5", "hi": "1"}}})
        self.assertEqual(resp["status"], "error")

    def test_parse_op_includes_cfg(self):
        resp = handle({"op": "parse", "source": SRC, "include_cfg": True})
        self.assertEqual(resp["status"], "ok")
        self.assertIn("blocks", resp["cfg"])
        self.assertIn("entry", resp["cfg"])

    def test_stdio_roundtrip(self):
        req = json.dumps({"op": "execute", "source": SRC,
                          "inputs": {"d": 5}}) + "\n"
        out = io.StringIO()
        run_stdio(io.StringIO(req), out)
        resp = json.loads(out.getvalue())
        self.assertEqual(resp["scalars"]["q"], 2)

    def test_http_roundtrip(self):
        server, port = _start_server()
        try:
            body = json.dumps({"op": "analyze", "source": SRC,
                               "input_bounds": {"d": {"lo": 1, "hi": 3}},
                               "include_points": False}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/analyze", data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                resp = json.loads(r.read())
            self.assertEqual(resp["status"], "ok")
            self.assertEqual(resp["alarms"], [])
        finally:
            server.shutdown()
            server.server_close()


def _start_server():
    import socket
    from http.server import HTTPServer
    # pick a free port
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    from intervalai.service import HTTPHandler as Handler
    server = HTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


if __name__ == "__main__":
    unittest.main()
