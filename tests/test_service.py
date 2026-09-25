import json
import subprocess
import sys
import unittest

from lattlang.service import handle


class TestService(unittest.TestCase):
    def test_analyze_structure(self):
        resp = handle({"action": "analyze",
                       "source": "if 1 { print 1; } else { print 2; }"})
        self.assertIn("reachable_blocks", resp)
        self.assertIn("executable_edges", resp)
        self.assertIn("lattice_summary", resp)
        self.assertEqual(set(resp["lattice_summary"]),
                         {"top", "const", "bottom"})
        # else block not reachable
        self.assertFalse(any("else" in b for b in resp["reachable_blocks"]))
        for block in resp["blocks"]:
            for ins in block["instructions"]:
                self.assertIn("span", ins)

    def test_optimize_returns_ir_and_changes(self):
        resp = handle({"action": "optimize", "source": "x := 1; print x;"})
        self.assertIn("optimized_ir", resp)
        self.assertIn("ssa_ir", resp)
        kinds = {c["kind"] for c in resp["changes"]}
        self.assertIn("constant_fold", kinds)

    def test_run_modes(self):
        src = "print 1 + 2;"
        for mode in ("ast", "ir", "optimized"):
            resp = handle({"action": "run", "source": src, "mode": mode})
            self.assertEqual(resp["output"], ["3"], mode)
            self.assertIsNone(resp["error"])

    def test_run_divzero_error_payload(self):
        resp = handle({"action": "run", "source": "print 1 / 0;",
                       "mode": "optimized"})
        self.assertIsNotNone(resp["error"])
        self.assertEqual(resp["error"]["stage"], "runtime")
        self.assertIn("span", resp["error"])

    def test_check_equivalent(self):
        resp = handle({"action": "check",
                       "source": "i:=0; while i<3 { print i; i:=i+1; }"})
        self.assertTrue(resp["equivalent"])
        self.assertEqual(resp["signatures"]["ast"]["output"],
                         ["0", "1", "2"])

    def test_unknown_action(self):
        from lattlang.errors import LangError
        with self.assertRaises(LangError):
            handle({"action": "nope", "source": ""})

    def test_parse_error_payload(self):
        from lattlang.errors import LangError
        with self.assertRaises(LangError) as cm:
            handle({"action": "run", "source": "x :="})
        self.assertEqual(cm.exception.stage, "parse")


class TestServiceCLI(unittest.TestCase):
    def test_stdin_roundtrip(self):
        req = json.dumps({"action": "run",
                          "source": "print 6 * 7;", "mode": "optimized"})
        proc = subprocess.run(
            [sys.executable, "-m", "lattlang.cli", "serve"],
            input=req, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        resp = json.loads(proc.stdout)
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["output"], ["42"])

    def test_error_exit_code(self):
        req = json.dumps({"action": "run", "source": "print 1/0;"})
        proc = subprocess.run(
            [sys.executable, "-m", "lattlang.cli", "serve"],
            input=req, capture_output=True, text=True, timeout=30)
        resp = json.loads(proc.stdout)
        self.assertTrue(resp["ok"])  # run succeeds; error is in payload
        self.assertIsNotNone(resp["error"])


if __name__ == "__main__":
    unittest.main()
