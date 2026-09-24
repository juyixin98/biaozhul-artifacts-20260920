"""stability 模块单元测试：稳定 / 量化失稳 / 越界三类情形。"""

import numpy as np

from fixedpoint_iir import QuantSpec, check_sos_stability
from fixedpoint_iir.stability import quantize_sos_checked, sos_poles

LP_SOS = np.array([[0.0976310729, 0.1952621458, 0.0976310729,
                    1.0, -0.9428090416, 0.3333333333]])

# 浮点稳定（极点半径 sqrt(0.9961)≈0.998）但 Q2.6 量化后 a2->1.0 失稳
RESONATOR_SOS = np.array([[0.1, 0.0, 0.0, 1.0, -1.99, 0.9961]])


class TestPoleComputation:
    def test_lowpass_poles_inside_unit_circle(self):
        poles = sos_poles(LP_SOS)
        assert np.max(np.abs(poles[0])) < 1.0

    def test_known_pole_locations(self):
        # a=[1, -1, 0.5] -> 极点 0.5 ± 0.5j，半径 sqrt(0.5)
        poles = sos_poles(np.array([[1.0, 0, 0, 1.0, -1.0, 0.5]]))
        assert abs(np.abs(poles[0][0]) - np.sqrt(0.5)) < 1e-12


class TestStableCase:
    def test_stable_report_clean(self):
        report = check_sos_stability(LP_SOS, QuantSpec(16, 14))
        assert report.stable
        assert not report.has_risk
        assert report.warnings == []
        assert report.coef_out_of_range == []

    def test_report_serializable(self):
        d = check_sos_stability(LP_SOS, QuantSpec(16, 14)).to_dict()
        assert d["stable"] is True
        assert isinstance(d["sections"][0]["poles"][0], list)


class TestQuantizationInducedInstability:
    def test_unstable_after_quantization(self):
        report = check_sos_stability(RESONATOR_SOS, QuantSpec(8, 6))
        assert not report.stable
        assert report.has_risk
        assert any("失稳" in w for w in report.warnings)
        # 量化前稳定
        assert report.sections[0].ref_max_radius < 1.0
        # 量化后极点落在单位圆上
        assert report.sections[0].max_radius >= 1.0

    def test_fine_quantization_keeps_stable(self):
        # 同一谐振器用 Q2.14 量化仍稳定，但应给出临界稳定风险提示
        report = check_sos_stability(RESONATOR_SOS, QuantSpec(16, 14))
        assert report.stable
        assert report.has_risk  # 极点距单位圆 < 1e-3，触发 margin 告警


class TestOutOfRangeCoefficients:
    def test_coef_beyond_range_detected(self):
        sos = np.array([[1.0, 0.0, 0.0, 1.0, -2.5, 0.0]])
        sos_q, oor = quantize_sos_checked(sos, QuantSpec(16, 14))
        assert (0, 4, -2.5) in oor
        assert sos_q[0, 4] == -2.0  # 饱和到 Q2.14 下界

    def test_out_of_range_raises_warning(self):
        sos = np.array([[1.0, 0.0, 0.0, 1.0, -2.5, 0.0]])
        report = check_sos_stability(sos, QuantSpec(16, 14))
        assert report.has_risk
        assert any("饱和" in w for w in report.warnings)
