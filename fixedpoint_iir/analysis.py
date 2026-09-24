"""滤波结果分析：频响误差、溢出统计与极限环检测。"""

from __future__ import annotations

import numpy as np

from .sos_filter import normalize_sos


def sos_freq_response(sos: np.ndarray, n_points: int = 512) -> tuple[np.ndarray, np.ndarray]:
    """计算 SOS 级联的复频响，返回 (角频率 ω∈[0,π], H(e^jω))。"""
    norm = normalize_sos(sos)
    w = np.linspace(0.0, np.pi, n_points)
    z1 = np.exp(-1j * w)
    z2 = z1 * z1
    h = np.ones(n_points, dtype=np.complex128)
    for k in range(norm.shape[0]):
        b0, b1, b2, _, a1, a2 = norm[k]
        num = b0 + b1 * z1 + b2 * z2
        den = 1.0 + a1 * z1 + a2 * z2
        h *= num / den
    return w, h


def freq_response_error(sos_ref: np.ndarray, sos_q: np.ndarray,
                        n_points: int = 512, floor_rel: float = 1e-3) -> dict:
    """量化前后频响误差（幅度 dB 与相位）。

    深阻带频点（|H| 低于峰值 floor_rel 倍，默认 -60 dB）的 dB 误差
    受零点量化敏感性主导、工程意义有限，予以剔除并报告有效点数。

    Returns:
        dict 含 max_db_error / mean_db_error / max_phase_error_deg。
    """
    _, h_ref = sos_freq_response(sos_ref, n_points)
    _, h_q = sos_freq_response(sos_q, n_points)
    mag_ref = np.abs(h_ref)
    mag_q = np.abs(h_q)
    floor = max(mag_ref.max(), 1e-12) * floor_rel
    valid = (mag_ref > floor) & (mag_q > floor)
    if not np.any(valid):
        raise ValueError("频响在所有频点均接近零，无法计算误差")
    db_err = 20.0 * np.abs(np.log10(mag_q[valid] / mag_ref[valid]))
    phase_err = np.abs(np.angle(h_q[valid] / h_ref[valid]))
    return {
        "n_points": int(n_points),
        "n_valid_points": int(np.count_nonzero(valid)),
        "max_db_error": float(db_err.max()),
        "mean_db_error": float(db_err.mean()),
        "max_phase_error_deg": float(np.degrees(phase_err.max())),
    }


def detect_limit_cycle(y: np.ndarray, tail: int = 64, max_period: int = 32,
                       atol: float = 1e-12) -> dict:
    """检测输出尾段是否存在极限环（零输入下的自持振荡 / 死区直流）。

    Args:
        y: 输出序列（通常为零输入响应，即冲激后的自由衰减段）。
        tail: 取末尾多少样本做周期检测。
        max_period: 最大检测周期。
        atol: 判定“相等”的绝对容差。

    Returns:
        dict: has_limit_cycle / period / tail_amplitude / is_deadband_dc。
    """
    y = np.asarray(y, dtype=np.float64)
    seg = y[-tail:] if y.shape[0] >= tail else y
    amp = float(np.max(np.abs(seg))) if seg.size else 0.0
    result = {
        "has_limit_cycle": False,
        "period": 0,
        "tail_amplitude": amp,
        "is_deadband_dc": False,
        "tail_samples": int(seg.size),
    }
    if seg.size < 4 or amp <= atol:
        return result  # 尾部已衰减到零，无极限环

    # 直流死区：尾段恒定非零
    if np.max(np.abs(seg - seg[0])) <= atol:
        result.update(has_limit_cycle=True, period=1, is_deadband_dc=True)
        return result

    for period in range(1, min(max_period, seg.size // 2) + 1):
        diff = seg[period:] - seg[:-period]
        if np.max(np.abs(diff)) <= atol:
            result.update(has_limit_cycle=True, period=period)
            return result
    # 未找到精确周期但尾部仍有显著能量：标记为疑似（非周期振荡）
    result["has_limit_cycle"] = True
    result["period"] = -1
    return result


def summarize_overflows(result) -> dict:
    """把 FixedPointResult 的溢出统计整理为可序列化字典。"""
    per_section = [
        {
            "section": k,
            "state_overflows": s.n_state_overflow,
            "output_overflows": s.n_output_overflow,
            "max_abs_state": s.max_abs_state,
        }
        for k, s in enumerate(result.section_stats)
    ]
    return {
        "input_overflows": result.n_input_overflow,
        "total_overflows": result.total_overflows,
        "coef_saturated_count": int(sum(result.coef_overflow_flags)),
        "per_section": per_section,
    }


def signal_metrics(y_ref: np.ndarray, y_fix: np.ndarray) -> dict:
    """定点输出相对浮点参考的时域误差指标。"""
    err = np.asarray(y_fix) - np.asarray(y_ref)
    ref_pow = float(np.mean(np.asarray(y_ref) ** 2))
    err_pow = float(np.mean(err ** 2))
    return {
        "max_abs_error": float(np.max(np.abs(err))) if err.size else 0.0,
        "rms_error": float(np.sqrt(err_pow)),
        "snr_db": float(10.0 * np.log10(ref_pow / err_pow)) if err_pow > 0 and ref_pow > 0 else float("inf"),
    }
