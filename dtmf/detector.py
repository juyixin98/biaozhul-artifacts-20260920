"""双音检测器：分帧 Goertzel 分析 + 门限判决 + 时序合并。

判决流程（每帧）：
1. 能量门限：RMS 低于 noise 门限的帧判为静音/噪声；
2. Goertzel 求 8 个频点功率，行/列组内分别取最强与次强，
   要求组内主次比 >= group_ratio_db，否则判为含糊（拒识）；
3. twist 检查：行/列主频电平差（幅度比）不得超过 twist_db；
4. 信噪比门限：双音估计功率与残余功率之比需 >= min_tone_snr。

时序合并：连续相同键的帧合并为一个音段，持续时长 >= min_tone_ms 才接受；
相邻不同键直接切换（无静音间隔）时，各自满足时长即分别接受。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .goertzel import goertzel_power_batch
from .tones import ALL_FREQS, COL_FREQS, KEYPAD, ROW_FREQS

# 拒识原因码
REASON_LOW_ENERGY = "low_energy"        # 能量低于噪声门限
REASON_ROW_AMBIGUOUS = "row_ambiguous"  # 行组主次比不足
REASON_COL_AMBIGUOUS = "col_ambiguous"  # 列组主次比不足
REASON_TWIST = "twist_exceeded"         # 幅度比（twist）超限
REASON_LOW_SNR = "low_tone_snr"         # 双音功率占比不足
REASON_TOO_SHORT = "too_short"          # 持续时长不足


@dataclass
class DetectorConfig:
    sample_rate: int = 8000
    frame_ms: float = 40.0        # 分析帧长
    hop_ms: float = 20.0          # 帧移
    min_tone_ms: float = 40.0     # 最短有效音长
    energy_threshold: float = 500.0   # RMS 噪声门限（int16 幅度刻度）
    group_ratio_db: float = 6.0   # 组内主次功率比下限（dB）
    twist_db: float = 8.0         # 行/列电平差上限（dB，绝对值）
    min_tone_snr: float = 1.5     # 双音功率 / 残余功率 下限（线性比）
    freq_search_pct: float = 2.0  # 频偏搜索范围（%）：在标称频率 ±该范围内取最大功率


@dataclass
class ToneEvent:
    key: str
    start_ms: float
    end_ms: float
    frames: int


@dataclass
class FrameDecision:
    key: str | None
    reason: str | None          # 拒识原因；key 有效时为 None
    row_freq: float | None = None
    col_freq: float | None = None
    snr_est: float = 0.0


@dataclass
class DecodeResult:
    digits: str
    events: list[ToneEvent]
    rejected_frames: int
    total_frames: int
    short_segments: int          # 因时长不足被丢弃的音段数

    def to_dict(self) -> dict:
        return {
            "digits": self.digits,
            "events": [
                {"key": e.key, "start_ms": round(e.start_ms, 1),
                 "end_ms": round(e.end_ms, 1), "frames": e.frames}
                for e in self.events
            ],
            "rejected_frames": self.rejected_frames,
            "total_frames": self.total_frames,
            "short_segments": self.short_segments,
        }


def _db(power_ratio: float) -> float:
    return 10.0 * math.log10(max(power_ratio, 1e-12))


class DtmfDetector:
    def __init__(self, config: DetectorConfig | None = None):
        self.cfg = config or DetectorConfig()
        self.frame_len = int(round(self.cfg.frame_ms * self.cfg.sample_rate / 1000.0))
        self.hop = int(round(self.cfg.hop_ms * self.cfg.sample_rate / 1000.0))
        if self.frame_len <= 0 or self.hop <= 0:
            raise ValueError("frame_ms/hop_ms 与采样率组合非法")
        self._window = np.hamming(self.frame_len)
        self._window_sum = float(np.sum(self._window))
        # 频偏搜索：每个标称频点在 ±freq_search_pct 内取 3 个评估点，取最大功率，
        # 以容忍合成信号的频率偏移（scalloping loss 不再被误判为残余噪声）。
        d = self.cfg.freq_search_pct / 100.0
        offsets = np.array([1.0 - d, 1.0, 1.0 + d]) if d > 0 else np.array([1.0])
        self._search_freqs = np.outer(np.asarray(ALL_FREQS), offsets).ravel()
        self._n_offsets = len(offsets)

    # ---- 单帧分析 ------------------------------------------------------

    def analyze_frame(self, frame: np.ndarray) -> FrameDecision:
        cfg = self.cfg
        x = np.asarray(frame, dtype=np.float64)
        n = x.shape[0]
        rms = math.sqrt(float(np.mean(x * x))) if n else 0.0
        if rms < cfg.energy_threshold:
            return FrameDecision(None, REASON_LOW_ENERGY)

        w = self._window[:n] if n == self.frame_len else np.hamming(n)
        wsum = float(np.sum(w))
        xw = x * w
        powers = goertzel_power_batch(xw, self._search_freqs, cfg.sample_rate)
        powers = powers.reshape(len(ALL_FREQS), self._n_offsets).max(axis=1)
        row_p, col_p = powers[: len(ROW_FREQS)], powers[len(ROW_FREQS):]

        def top2(p: np.ndarray) -> tuple[int, float, float]:
            order = np.argsort(p)[::-1]
            return int(order[0]), float(p[order[0]]), float(p[order[1]])

        ri, r1, r2 = top2(row_p)
        ci, c1, c2 = top2(col_p)
        if _db(r1 / max(r2, 1e-12)) < cfg.group_ratio_db:
            return FrameDecision(None, REASON_ROW_AMBIGUOUS)
        if _db(c1 / max(c2, 1e-12)) < cfg.group_ratio_db:
            return FrameDecision(None, REASON_COL_AMBIGUOUS)

        twist = abs(_db(r1 / max(c1, 1e-12)))
        if twist > cfg.twist_db:
            return FrameDecision(None, REASON_TWIST)

        # 由 Goertzel 功率估计双音幅度（考虑窗的相干增益），再算残余功率
        amp_row = 2.0 * math.sqrt(r1) / wsum
        amp_col = 2.0 * math.sqrt(c1) / wsum
        tone_ms_power = (amp_row ** 2 + amp_col ** 2) / 2.0
        residual = max(float(np.mean(xw * xw)) / max(float(np.mean(w * w)), 1e-12)
                       - tone_ms_power, 1e-9)
        snr_est = tone_ms_power / residual
        if snr_est < cfg.min_tone_snr:
            return FrameDecision(None, REASON_LOW_SNR, snr_est=snr_est)

        key = KEYPAD[ri][ci]
        return FrameDecision(key, None, ROW_FREQS[ri], COL_FREQS[ci], snr_est)

    # ---- 整段解码 ------------------------------------------------------

    def decode(self, samples: np.ndarray) -> DecodeResult:
        x = np.asarray(samples, dtype=np.float64)
        if x.size < self.frame_len:
            x = np.pad(x, (0, self.frame_len - x.size))
        starts = range(0, x.size - self.frame_len + 1, self.hop)
        decisions = [self.analyze_frame(x[s:s + self.frame_len]) for s in starts]

        events: list[ToneEvent] = []
        short_segments = 0
        i = 0
        n_frames = len(decisions)
        min_frames = max(1, math.ceil(self.cfg.min_tone_ms / self.cfg.hop_ms))
        while i < n_frames:
            d = decisions[i]
            if d.key is None:
                i += 1
                continue
            j = i
            while j + 1 < n_frames and decisions[j + 1].key == d.key:
                j += 1
            seg_frames = j - i + 1
            if seg_frames >= min_frames:
                events.append(ToneEvent(
                    key=d.key,
                    start_ms=i * self.cfg.hop_ms,
                    end_ms=j * self.cfg.hop_ms + self.cfg.frame_ms,
                    frames=seg_frames,
                ))
            else:
                short_segments += 1
            i = j + 1

        rejected = sum(1 for d in decisions if d.key is None)
        return DecodeResult(
            digits="".join(e.key for e in events),
            events=events,
            rejected_frames=rejected,
            total_frames=n_frames,
            short_segments=short_segments,
        )
