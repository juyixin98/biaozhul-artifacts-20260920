"""Tests for PCM I/O, the CLI, and the HTTP service."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import numpy as np
import pytest

from dc_blocker import cli, io
from dc_blocker.service import _make_handler, _State


# ----------------------------------------------------------------------
# PCM I/O round-trip
# ----------------------------------------------------------------------
@pytest.mark.parametrize("encoding", io.ENCODINGS)
def test_pcm_roundtrip(tmp_path, encoding):
    x = np.linspace(-0.9, 0.9, 1000)
    path = tmp_path / f"sig.{encoding}"
    io.write_pcm(str(path), x, encoding)
    y = io.read_pcm(str(path), encoding)
    tol = 1e-7 if encoding == "f64le" else (1e-6 if encoding == "f32le" else 1.0 / 32768.0)
    np.testing.assert_allclose(y, x, atol=tol)


def test_pcm_int16_clips_instead_of_wrapping(tmp_path):
    path = tmp_path / "clip.s16le"
    io.write_pcm(str(path), np.array([2.0, -2.0]), "s16le")
    y = io.read_pcm(str(path), "s16le")
    assert y[0] == pytest.approx(32767.0 / 32768.0)
    assert y[1] == pytest.approx(-1.0)


def test_unknown_encoding_rejected(tmp_path):
    with pytest.raises(ValueError):
        io.read_pcm(str(tmp_path / "x.raw"), "u8")


# ----------------------------------------------------------------------
# CLI end-to-end on a synthetic biased sine
# ----------------------------------------------------------------------
def test_cli_synthetic_biased_sine(tmp_path):
    out = tmp_path / "cleaned.s16le"
    metrics_path = tmp_path / "metrics.json"
    metrics = cli.run(
        [
            "--synthetic", "biased-sine",
            "--sample-rate", "48000",
            "--seconds", "3",
            "--freq", "440",
            "--amplitude", "0.5",
            "--bias", "0.3",
            "--cutoff", "5",
            "--block-size", "1024",
            "--output", str(out),
            "--metrics", str(metrics_path),
        ]
    )
    assert out.exists() and out.stat().st_size == 3 * 48000 * 2
    on_disk = json.loads(metrics_path.read_text())
    assert on_disk == metrics
    assert metrics["input_mean"] == pytest.approx(0.3, abs=1e-3)
    # steady-state residual bias well below 1% of the input bias
    assert abs(metrics["output_tail_mean"]) < 0.003


def test_cli_block_size_invariant(tmp_path):
    def run_with_block(size):
        out = tmp_path / f"out_{size}.f64le"
        cli.run(
            [
                "--synthetic", "bias-step",
                "--sample-rate", "48000",
                "--seconds", "1",
                "--encoding", "f64le",
                "--cutoff", "5",
                "--block-size", str(size),
                "--output", str(out),
            ]
        )
        return io.read_pcm(str(out), "f64le")

    np.testing.assert_array_equal(run_with_block(1), run_with_block(4800))


# ----------------------------------------------------------------------
# HTTP service: streaming across requests equals one-shot filtering
# ----------------------------------------------------------------------
@pytest.fixture()
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(_State()))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    thread.join()


def _post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def test_service_streaming_matches_one_shot(server):
    from dc_blocker import DCBlocker

    sid = _post(f"{server}/sessions", {"sample_rate": 48000, "cutoff_hz": 5})["session_id"]
    rng = np.random.default_rng(1)
    x = 0.3 + rng.standard_normal(500) * 0.1
    got = []
    for chunk in (x[:100], x[100:101], x[101:]):  # incl. a 1-sample block
        got.extend(_post(f"{server}/sessions/{sid}/process", {"samples": chunk.tolist()})["samples"])
    expected = DCBlocker(48000, 5).process(x)
    np.testing.assert_allclose(got, expected, rtol=1e-12, atol=1e-15)

    # reset -> filtering the same stream again reproduces the same output
    _post(f"{server}/sessions/{sid}/reset", {})
    again = _post(f"{server}/sessions/{sid}/process", {"samples": x.tolist()})["samples"]
    np.testing.assert_allclose(again, expected, rtol=1e-12, atol=1e-15)

    # sample-rate reconfiguration keeps the cutoff and updates the pole
    resp = _post(f"{server}/sessions/{sid}/sample-rate", {"sample_rate": 16000})
    assert resp["sample_rate"] == 16000.0
    assert resp["R"] == pytest.approx(DCBlocker(16000, 5).coefficient)

    req = urllib.request.Request(f"{server}/sessions/{sid}", method="DELETE")
    with urllib.request.urlopen(req) as resp:
        assert json.loads(resp.read()) == {"deleted": True}


def test_service_rejects_bad_requests(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(f"{server}/sessions", {"sample_rate": -1})
    assert exc.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(f"{server}/sessions/deadbeef/process", {"samples": [1.0]})
    assert exc.value.code == 404
