"""CLI 冒烟测试：run 子命令的退出码、输出文件与拒绝覆盖。"""

import json
import subprocess
import sys

from delay_correlator.signals import SyntheticSpec, make_delayed_pair
from delay_correlator.signals import save_raw_pcm


def _run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "delay_correlator", *args],
        capture_output=True, text=True, cwd=__import__("pathlib").Path(__file__).resolve().parent.parent,
    )


def test_cli_version():
    p = _run_cli("--version")
    assert p.returncode == 0
    assert "delay_correlator" in p.stdout


def test_cli_run_synthetic(tmp_path):
    request = {
        "source": {
            "mode": "synthetic", "kind": "noise", "duration": 0.5,
            "sample_rate": 8000, "delay_samples": 37, "seed": 11,
        },
        "estimator": {"max_lag": 128, "min_peak": 0.6},
    }
    req = tmp_path / "req.json"
    req.write_text(json.dumps(request), encoding="utf-8")
    out = tmp_path / "out"

    p = _run_cli("run", str(req), "-o", str(out))
    assert p.returncode == 0, p.stderr
    assert "平均延迟" in p.stdout
    assert (out / "results.json").is_file()

    # 第二次运行同目录必须失败，防止误覆盖
    p2 = _run_cli("run", str(req), "-o", str(out))
    assert p2.returncode == 1
    assert "拒绝覆盖" in p2.stderr


def test_cli_run_pcm_and_bad_request(tmp_path):
    spec = SyntheticSpec(kind="noise", duration=0.5, sample_rate=8000,
                         delay_samples=-12, seed=3)
    ref, chan = make_delayed_pair(spec)
    save_raw_pcm(str(tmp_path / "pair.pcm"), ref, chan,
                 dtype="s16", interleaved=True)
    request = {
        "source": {
            "mode": "file", "kind": "raw_pcm", "path": "pair.pcm",
            "dtype": "s16", "channels": 2, "ref_channel": 0,
            "channel": 1, "sample_rate": 8000,
        },
        "estimator": {"max_lag": 128},
    }
    req = tmp_path / "req.json"
    req.write_text(json.dumps(request), encoding="utf-8")
    p = _run_cli("run", str(req), "-o", str(tmp_path / "out"))
    assert p.returncode == 0, p.stderr

    missing = tmp_path / "nope.json"
    p2 = _run_cli("run", str(missing))
    assert p2.returncode == 2
