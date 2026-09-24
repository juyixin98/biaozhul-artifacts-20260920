"""验收测试：溢出、极限环、频响误差、稳定/不稳定反例、服务端到端。

对应验收标准：
1. 冲激及大幅输入下的溢出检查；
2. 极限环检测（定点自持振荡 vs 浮点衰减）；
3. 系数量化前后频响误差；
4. 稳定与不稳定（量化导致）系数反例；
5. 离线服务端到端产出数值与文件。
"""

import json
from pathlib import Path

import numpy as np
import pytest

from fixedpoint_iir import FixedPointSOSFilter, QuantSpec, sos_filter_float
from fixedpoint_iir.analysis import (
    detect_limit_cycle,
    freq_response_error,
    signal_metrics,
)
from fixedpoint_iir.service import run_request
from fixedpoint_iir.signals import gen_impulse, gen_sine
from fixedpoint_iir.stability import quantize_sos_checked

LP_SOS = np.array([[0.0976310729, 0.1952621458, 0.0976310729,
                    1.0, -0.9428090416, 0.3333333333]])
RESONATOR_SOS = np.array([[0.1, 0.0, 0.0, 1.0, -1.99, 0.9961]])

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


class TestImpulseAndOverflow:
    """验收点 1：冲激与大幅输入的溢出行为。"""

    def test_impulse_no_overflow_when_scaled(self):
        x = gen_impulse(256, amplitude=0.9)
        r = FixedPointSOSFilter(LP_SOS, QuantSpec(16, 14), QuantSpec(16, 15)).process(x)
        assert r.total_overflows == 0
        # 冲激响应最终衰减到零（稳定系统）
        assert np.max(np.abs(r.y[-32:])) < 1e-3

    def test_large_input_overflow_detected(self):
        x = gen_sine(512, 500.0, 8000.0, amplitude=1.5)  # 超出 Q1.15
        r = FixedPointSOSFilter(LP_SOS, QuantSpec(16, 14), QuantSpec(16, 15)).process(x)
        assert r.n_input_overflow > 0
        assert r.total_overflows > 0
        # 饱和语义：输出不越界、不发散
        assert np.all(np.abs(r.y) <= 1.0)
        assert np.all(np.isfinite(r.y))

    def test_full_scale_impulse_state_overflow_reported(self):
        # 高增益节 + 满幅冲激，状态写回应出现饱和计数
        hot = np.array([[4.0, 8.0, 4.0, 1.0, -0.5, 0.0]])
        x = gen_impulse(64, amplitude=0.99)
        r = FixedPointSOSFilter(hot, QuantSpec(16, 13), QuantSpec(16, 15)).process(x)
        assert r.total_overflows > 0


class TestLimitCycle:
    """验收点 2：极限环检测。"""

    def test_deadband_limit_cycle_detected(self):
        # y[n] = x[n] + 0.9*y[n-1]，粗量化 Q1.7 + 最近舍入：
        # 浮点冲激响应衰减到 0，定点陷入非零死区（经典递归极限环）
        sos = np.array([[1.0, 0.0, 0.0, 1.0, -0.9, 0.0]])
        x = gen_impulse(512, amplitude=0.5)
        y_ref = sos_filter_float(sos, x)
        r = FixedPointSOSFilter(sos, QuantSpec(16, 14), QuantSpec(8, 7)).process(x)
        assert np.max(np.abs(y_ref[-64:])) < 1e-6          # 浮点已衰减
        lc = detect_limit_cycle(r.y, tail=64)
        assert lc["has_limit_cycle"]
        assert lc["tail_amplitude"] > 0

    def test_no_limit_cycle_below_quantum(self):
        # 该低通在 Q1.15 下存在 1 LSB 死区（真实存在的颗粒极限环），
        # 严格容差下应被检出；以 2 LSB 为容差则视为已衰减
        x = gen_impulse(256, amplitude=0.5)
        state = QuantSpec(16, 15)
        r = FixedPointSOSFilter(LP_SOS, QuantSpec(16, 14), state).process(x)
        strict = detect_limit_cycle(r.y, tail=64)
        assert strict["has_limit_cycle"]
        assert strict["is_deadband_dc"]
        assert strict["tail_amplitude"] == pytest.approx(state.resolution)
        relaxed = detect_limit_cycle(r.y, tail=64, atol=2 * state.resolution)
        assert not relaxed["has_limit_cycle"]


class TestFreqResponseError:
    """验收点 3：系数量化前后频响误差。"""

    def test_fine_quantization_small_error(self):
        sos_q, _ = quantize_sos_checked(LP_SOS, QuantSpec(16, 14))
        err = freq_response_error(LP_SOS, sos_q)
        assert err["max_db_error"] < 0.5
        assert err["mean_db_error"] < 0.05

    def test_coarse_quantization_larger_error(self):
        sos_q, _ = quantize_sos_checked(LP_SOS, QuantSpec(8, 6))
        err = freq_response_error(LP_SOS, sos_q)
        assert err["mean_db_error"] > 0.1


class TestCounterExamples:
    """验收点 4：稳定与不稳定系数反例。"""

    def test_stable_counterexample(self):
        x = gen_impulse(512, amplitude=0.5)
        r = FixedPointSOSFilter(RESONATOR_SOS, QuantSpec(16, 14),
                                QuantSpec(16, 15)).process(x)
        # Q2.14 量化下仍稳定：响应最终衰减
        assert np.max(np.abs(r.y[-64:])) < np.max(np.abs(r.y[:64]))

    def test_unstable_counterexample_sustains(self):
        x = gen_impulse(512, amplitude=0.5)
        r = FixedPointSOSFilter(RESONATOR_SOS, QuantSpec(8, 6),
                                QuantSpec(16, 15)).process(x)
        y_ref = sos_filter_float(RESONATOR_SOS, x)
        # 浮点参考在衰减，定点（量化后 a2=1.0）尾部仍维持振荡
        tail_fixed = np.max(np.abs(r.y[-64:]))
        assert tail_fixed > 0.1
        assert np.max(np.abs(y_ref[-64:])) < np.max(np.abs(y_ref[:64]))


class TestServiceEndToEnd:
    """验收点 5：离线服务端到端。"""

    @pytest.mark.parametrize("req_name", [
        "request_stable_impulse.json",
        "request_unstable_quantization.json",
        "request_overflow_large_input.json",
    ])
    def test_example_requests(self, req_name, tmp_path):
        with open(EXAMPLES / req_name, encoding="utf-8") as f:
            request = json.load(f)
        report = run_request(request, tmp_path)
        # 文件产出齐全
        for name in ("input.npy", "output_float.npy", "output_fixed.npy",
                     "output_fixed.pcm", "report.json"):
            assert (tmp_path / name).exists()
        # 报告结构完整
        for key in ("stability", "overflow", "freq_response_error",
                    "time_domain_metrics", "limit_cycle"):
            assert key in report
        # 数值文件长度一致
        x = np.load(tmp_path / "input.npy")
        assert np.load(tmp_path / "output_fixed.npy").shape == x.shape

    def test_stable_request_reports_stable(self, tmp_path):
        with open(EXAMPLES / "request_stable_impulse.json", encoding="utf-8") as f:
            report = run_request(json.load(f), tmp_path)
        assert report["stability"]["stable"] is True
        assert report["overflow"]["total_overflows"] == 0
        assert report["freq_response_error"]["max_db_error"] < 0.5
        assert report["time_domain_metrics"]["snr_db"] > 60.0

    def test_unstable_request_reports_instability(self, tmp_path):
        with open(EXAMPLES / "request_unstable_quantization.json", encoding="utf-8") as f:
            report = run_request(json.load(f), tmp_path)
        assert report["stability"]["stable"] is False
        assert len(report["stability"]["warnings"]) > 0

    def test_overflow_request_counts_overflows(self, tmp_path):
        with open(EXAMPLES / "request_overflow_large_input.json", encoding="utf-8") as f:
            report = run_request(json.load(f), tmp_path)
        assert report["overflow"]["input_overflows"] > 0
        assert report["overflow"]["total_overflows"] > 0

    def test_pcm_roundtrip_request(self, tmp_path):
        # 生成 PCM 输入 -> 跑服务 -> 验证输出
        from fixedpoint_iir.signals import gen_multi_sine, write_pcm
        pcm_in = tmp_path / "in.pcm"
        write_pcm(str(pcm_in), gen_multi_sine(512, [1000.0], 8000.0, [0.5]))
        request = {
            "name": "pcm_e2e",
            "signal": {"type": "pcm_file", "path": str(pcm_in), "fmt": "s16le"},
            "sos": LP_SOS.tolist(),
        }
        report = run_request(request, tmp_path / "out")
        assert report["config"]["n_samples"] == 512
        m = report["time_domain_metrics"]
        assert m["max_abs_error"] < 1e-2
