"""合成测试信号生成（正弦加偏置、偏置跳变、直流常量）。"""

from __future__ import annotations

import numpy as np


def generate(spec: dict, sample_rate: float) -> np.ndarray:
    """按规格字典生成合成信号，返回 float64 一维数组。

    支持的 type：
      sine     : frequency, amplitude, dc_offset, duration_s
      dc_step  : initial_dc, final_dc, step_time_s, duration_s
      constant : value, duration_s
    """
    kind = spec.get("type")
    duration_s = float(spec.get("duration_s", 1.0))
    if duration_s <= 0:
        raise ValueError(f"duration_s 必须为正数，得到 {duration_s!r}")
    n = int(round(duration_s * sample_rate))

    if kind == "sine":
        freq = float(spec["frequency"])
        amp = float(spec.get("amplitude", 1.0))
        dc = float(spec.get("dc_offset", 0.0))
        t = np.arange(n, dtype=np.float64) / sample_rate
        return amp * np.sin(2.0 * np.pi * freq * t) + dc

    if kind == "dc_step":
        initial = float(spec.get("initial_dc", 0.0))
        final = float(spec["final_dc"])
        step_time = float(spec.get("step_time_s", duration_s / 2.0))
        out = np.full(n, initial, dtype=np.float64)
        out[int(round(step_time * sample_rate)) :] = final
        return out

    if kind == "constant":
        return np.full(n, float(spec["value"]), dtype=np.float64)

    raise ValueError(f"未知的合成信号类型 {kind!r}（支持 sine / dc_step / constant）")
