"""pcm.cli:命令行/JSON 请求分发与退出码测试。"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from pcmval import cli


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_synth_then_validate_roundtrip(self):
        wav = os.path.join(self.dir, "fs.wav")
        code, out, _ = self._run([
            "synth", "--kind", "fullscale", "--bits", "24",
            "--sample-rate", "1000", "--duration", "0.008", "-o", wav,
        ])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["sample_stats"]["int_min"] == -8388608)

        code, out, _ = self._run(["validate", wav])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["frames"], 8)

        code, out, _ = self._run([
            "roundtrip", "--kind", "fullscale", "--bits", "16",
            "--sample-rate", "1000", "--duration", "0.008",
        ])
        self.assertEqual(code, 0)
        checks = json.loads(out)["checks"]
        self.assertTrue(checks["payload_bytes_identical"])
        self.assertTrue(checks["amplitude_roundtrip_identical"])

    def test_rejected_wav_exit_2(self):
        bad = os.path.join(self.dir, "bad.wav")
        with open(bad, "wb") as fh:
            fh.write(b"RF64" + b"\x00" * 30)
        code, _, err = self._run(["validate", bad])
        self.assertEqual(code, 2)
        self.assertIn("not a RIFF", err)

    def test_missing_file_exit_2(self):
        code, _, err = self._run(["validate", os.path.join(self.dir, "nope.wav")])
        self.assertEqual(code, 2)
        self.assertIn("file not found", err)

    def test_json_request_batch_partial_failure_exit_1(self):
        wav = os.path.join(self.dir, "ok.wav")
        req = os.path.join(self.dir, "req.json")
        with open(req, "w", encoding="utf-8") as fh:
            json.dump([
                {"action": "synthesize", "kind": "silence", "sample_rate": 8000,
                 "duration": 0.001, "bits": 16, "out": wav},
                {"action": "validate", "path": os.path.join(self.dir, "x.wav")},
            ], fh)
        code, out, _ = self._run(["request", req])
        self.assertEqual(code, 1)
        results = json.loads(out)
        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[1]["ok"])

    def test_json_request_stdin(self):
        wav = os.path.join(self.dir, "s.wav")
        payload = json.dumps({
            "action": "synthesize", "kind": "silence", "sample_rate": 8000,
            "duration": 0.001, "bits": 16, "out": wav,
        })
        out, err = io.StringIO(), io.StringIO()
        import sys
        old_stdin = sys.stdin
        try:
            sys.stdin = io.StringIO(payload)
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["request", "-"])
        finally:
            sys.stdin = old_stdin
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
