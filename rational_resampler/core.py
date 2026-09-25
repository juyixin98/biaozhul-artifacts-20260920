"""多相有理比重采样核心。

数学定义（零相位对齐约定）：

    y[m] = sum_i x[i] * h[m*M + D - i*L],   m = 0 .. n_out-1

其中 h 为长度 N 的对称抗混叠 FIR（支撑区间 [0, N-1]），D = (N-1)/2 为其
群延迟（上采样域采样点），x 超出 [0, n_in-1] 的部分按填充模式取值。
输出时刻 t = m*M/L（输入采样点单位）：延迟项 D 使滤波器中心对准输出
瞬间，群延迟被索引方式补偿，输出与输入时间轴对齐（非因果对称窗，
边界由填充处理）。

输出长度约定：n_out = ceil(n_in * L / M)。

输出长度约定：n_out = ceil(n_in * L / M)。

PolyphaseResampler 是无填充的纯状态机；StreamingResampler 在其上实现
边界填充与裁剪。整段 resample() 就是单块的 StreamingResampler，
因此分块与整段结果逐样本一致。
"""

from __future__ import annotations

import math

import numpy as np

from .filter_design import (
    DEFAULT_ATTENUATION_DB,
    DEFAULT_TRANSITION_RATIO,
    design_anti_alias_fir,
)

PAD_MODES = ("zero", "edge", "reflect", "none")


class PolyphaseResampler:
    """无填充的 L/M 多相重采样状态机（输入界外隐含补零）。"""

    def __init__(self, h: np.ndarray, up: int, down: int):
        h = np.asarray(h, dtype=np.float64)
        if h.ndim != 1 or h.size < 1:
            raise ValueError("h must be a non-empty 1-D array")
        if up < 1 or down < 1:
            raise ValueError("up and down must be positive integers")
        self.h = h
        self.up = int(up)
        self.down = int(down)
        # 群延迟（上采样域采样点）。假设 h 为对称滤波器；奇数长时 D 为整数，
        # 对齐精确；偶数长时存在半样本残差（文档化行为，建议用奇数长）。
        self._delay = (h.size - 1) // 2
        # 按输出相位预分解多相分支：phase = (m*M + D) % L，系数逆序以便与
        # 输入历史切片做点积。不同 m 只会有 L/gcd(L,M) 种相位。
        self._branches = [h[p :: self.up][::-1].copy() for p in range(self.up)]
        self._hist = np.zeros(0, dtype=np.float64)  # 输入历史（全局索引自 _hist_start 起）
        self._hist_start = 0
        self._keep = (h.size - 1) // self.up + 2  # 需要保留的输入样本数
        self._n_in = 0  # 已接收输入样本总数
        self._m_next = 0  # 下一个待发射的输出索引

    @property
    def n_in(self) -> int:
        return self._n_in

    @property
    def n_out_emitted(self) -> int:
        return self._m_next

    def _output_at(self, m: int) -> float:
        """按定义式计算单个输出，历史界外视为零。"""
        up = self.up
        mm = m * self.down + self._delay
        branch = self._branches[mm % up]
        i_hi = mm // up
        i_lo = i_hi - branch.size + 1
        lo = max(i_lo, self._hist_start)
        hi = min(i_hi, self._n_in - 1)
        if lo > hi:
            return 0.0
        seg = self._hist[lo - self._hist_start : hi - self._hist_start + 1]
        taps = branch[lo - i_lo : branch.size - (i_hi - hi)]
        return float(np.dot(taps, seg))

    def _emit_until(self, m_max: int) -> np.ndarray:
        """发射索引 [m_next, m_max] 的输出。"""
        if m_max < self._m_next:
            return np.zeros(0, dtype=np.float64)
        out = np.array(
            [self._output_at(m) for m in range(self._m_next, m_max + 1)],
            dtype=np.float64,
        )
        self._m_next = m_max + 1
        return out

    def process(self, block: np.ndarray) -> np.ndarray:
        """送入一段输入，返回当前可确定的输出（尾部暂不冲刷）。"""
        block = np.ascontiguousarray(block, dtype=np.float64).ravel()
        if block.size:
            self._hist = np.concatenate([self._hist, block])
            self._n_in += block.size
        if self._n_in == 0:
            return np.zeros(0, dtype=np.float64)
        # 输出 m 需要输入到 floor((m*M + D)/L)，故 m*M + D <= (n_in-1)*L 时可发射
        m_max = ((self._n_in - 1) * self.up - self._delay) // self.down
        out = self._emit_until(m_max)
        # 裁剪历史：保留末尾 _keep 个样本即可满足未来所有输出
        if self._hist.size > self._keep:
            drop = self._hist.size - self._keep
            self._hist = self._hist[drop:]
            self._hist_start += drop
        return out

    def flush(self) -> np.ndarray:
        """冲刷滤波器尾部（输入界外补零），返回剩余全部输出。"""
        if self._n_in == 0:
            return np.zeros(0, dtype=np.float64)
        # 最后一个仍有非零输入贡献的输出：m*M + D - (N-1) <= (n_in-1)*L
        m_max = ((self._n_in - 1) * self.up - self._delay + self.h.size - 1) // self.down
        return self._emit_until(m_max)


def _reflect_left(s: np.ndarray, p: int) -> np.ndarray:
    if p == 0:
        return np.zeros(0, dtype=np.float64)
    return np.pad(s[: p + 1], (p, 0), mode="reflect")[:p]


def _reflect_right(s: np.ndarray, p: int) -> np.ndarray:
    if p == 0:
        return np.zeros(0, dtype=np.float64)
    return np.pad(s[-(p + 1) :], (0, p), mode="reflect")[-p:]


class StreamingResampler:
    """带边界填充与输出裁剪的分块重采样器。

    填充在输入域进行，长度默认取群延迟对应的输入采样数
    D_in = (N-1)/(2L)，并向上取整为 M/gcd(L,M) 的整数倍，
    使输出域裁剪量 pad*L/M 为精确整数。
    """

    def __init__(
        self,
        up: int,
        down: int,
        h: np.ndarray | None = None,
        pad_mode: str = "edge",
        attenuation_db: float = DEFAULT_ATTENUATION_DB,
        transition_ratio: float = DEFAULT_TRANSITION_RATIO,
        pad_left: int | None = None,
        pad_right: int | None = None,
    ):
        if pad_mode not in PAD_MODES:
            raise ValueError(f"pad_mode must be one of {PAD_MODES}")
        if h is None:
            h = design_anti_alias_fir(up, down, attenuation_db, transition_ratio)
        self.h = np.asarray(h, dtype=np.float64)
        self.up = int(up)
        self.down = int(down)
        self.pad_mode = pad_mode

        step = self.down // math.gcd(self.up, self.down)
        delay_in = (self.h.size - 1) / (2.0 * self.up)  # 群延迟（输入采样点）

        def _resolve(p: int | None) -> int:
            if pad_mode == "none":
                return 0
            v = delay_in if p is None else float(p)
            return int(math.ceil(v / step)) * step

        self.pad_left = _resolve(pad_left)
        self.pad_right = _resolve(pad_right)
        # 输出域丢弃数（精确整数）
        self._drop = self.pad_left * self.up // self.down

        self._core = PolyphaseResampler(self.h, self.up, self.down)
        self._pending: list[np.ndarray] | None = []  # 启动前缓冲的首批输入
        self._n_in = 0
        self._emitted = 0  # 裁剪后已发射输出数
        self._dropped = 0
        self._tail = np.zeros(0, dtype=np.float64)  # 末尾真实输入（构造右填充用）
        self._finished = False

    # ---- 内部 ----

    def _left_pad(self, s: np.ndarray) -> np.ndarray:
        p = self.pad_left
        if p == 0:
            return np.zeros(0, dtype=np.float64)
        if self.pad_mode == "zero" or s.size == 0:
            return np.zeros(p, dtype=np.float64)
        if self.pad_mode == "edge":
            return np.full(p, s[0], dtype=np.float64)
        return _reflect_left(s, p)

    def _right_pad(self) -> np.ndarray:
        p = self.pad_right
        if p == 0:
            return np.zeros(0, dtype=np.float64)
        if self.pad_mode == "zero" or self._tail.size == 0:
            return np.zeros(p, dtype=np.float64)
        if self.pad_mode == "edge":
            return np.full(p, self._tail[-1], dtype=np.float64)
        return _reflect_right(self._tail, p)

    def _route(self, chunk: np.ndarray) -> np.ndarray:
        """丢弃开头 _drop 个输出（左填充产生），其余放行。"""
        if self._dropped < self._drop:
            take = min(self._drop - self._dropped, chunk.size)
            chunk = chunk[take:]
            self._dropped += take
        self._emitted += chunk.size
        return chunk

    def _start(self) -> np.ndarray:
        """用缓冲的首批输入构造左填充并启动核心状态机，返回已有输出。"""
        s = np.concatenate(self._pending) if self._pending else np.zeros(0)
        self._pending = None
        parts = [self._route(self._core.process(self._left_pad(s)))]
        if s.size:
            parts.append(self._feed(s))
        return np.concatenate(parts)

    def _feed(self, block: np.ndarray) -> np.ndarray:
        out = self._route(self._core.process(block))
        keep = self.pad_right + 1
        self._tail = np.concatenate([self._tail, block])[-keep:]
        return out

    # ---- 公开接口 ----

    def process(self, block: np.ndarray) -> np.ndarray:
        """送入一块输入，返回本次产生的输出（可能为空）。"""
        if self._finished:
            raise RuntimeError("process() called after finish()")
        block = np.ascontiguousarray(block, dtype=np.float64).ravel()
        self._n_in += block.size  # 接收即计数（与是否还在缓冲首批输入无关）
        if self._pending is not None:
            self._pending.append(block)
            need = self.pad_left + 1 if self.pad_mode == "reflect" else 0
            have = sum(b.size for b in self._pending)
            if have < need:
                return np.zeros(0, dtype=np.float64)
            return self._start()
        if block.size == 0:
            return np.zeros(0, dtype=np.float64)
        return self._feed(block)

    def finish(self) -> np.ndarray:
        """结束输入，返回剩余输出；总输出长度恰为 ceil(n_in*L/M)。"""
        if self._finished:
            raise RuntimeError("finish() called twice")
        self._finished = True
        # 裁剪到约定长度 ceil(n_in*L/M)；n_in 在 process() 接收时已计数，
        # 且剩余量须在任何 _route 计数之前确定
        n_out = -(-self._n_in * self.up // self.down) if self._n_in else 0
        remain = max(n_out - self._emitted, 0)
        parts = []
        if self._pending is not None:
            parts.append(self._start())
        parts.append(self._route(self._core.process(self._right_pad())))
        parts.append(self._route(self._core.flush()))
        out = np.concatenate(parts)
        return out[:remain]


def resample(
    x: np.ndarray,
    up: int,
    down: int,
    h: np.ndarray | None = None,
    pad_mode: str = "edge",
    **kwargs,
) -> np.ndarray:
    """整段重采样：等价于单块的 StreamingResampler。"""
    r = StreamingResampler(up, down, h=h, pad_mode=pad_mode, **kwargs)
    parts = [r.process(np.asarray(x, dtype=np.float64)), r.finish()]
    nonempty = [p for p in parts if p.size]
    return np.concatenate(nonempty) if nonempty else np.zeros(0)


def filter_info(h: np.ndarray, up: int, down: int) -> dict:
    """返回滤波器与延迟的定义性参数（文档与报告用）。"""
    n = int(np.asarray(h).size)
    return {
        "filter_length": n,
        "up": int(up),
        "down": int(down),
        "group_delay_upsampled_samples": (n - 1) / 2.0,
        "group_delay_input_samples": (n - 1) / (2.0 * up),
        "group_delay_output_samples": (n - 1) / (2.0 * down),
        "alignment": "zero-phase (delay compensated by indexing)",
    }
