"""有理比重采样器：多相（polyphase）实现，含边界填充与群延迟定义。

信号流（教科书形式）：

    x[n] --(上采样 L, 零插值)--> 抗混叠低通 FIR h --(下采样 M)--> y[n]

实际计算不真的插零，而是对每个输出样本用对应相位的多相分支做点积：

    t(n)  = n*M + gd            （输出 n 在上采样域的中心位置）
    p     = t mod L             （多相相位）
    kmax  = t // L
    y[n]  = L * sum_{j=0..K-1} h[p + j*L] * x_ext[kmax - j]

其中 K = ceil(num_taps / L) 为每相抽头数，x_ext 为按边界填充策略
扩展后的输入。该定义与分块方式无关：每个输出样本的取值只取决于
输入样本与滤波器系数，因此分块处理与整段处理结果逐位一致。

群延迟
------
FIR 为线性相位（对称），群延迟 gd = (num_taps - 1) / 2 个上采样域
样本 = gd / L 个输入样本 = gd / (L * fs_in) 秒。实现中把 gd 并入
t(n)，即输出样本 n 对齐输入时刻 n*M/L（输入样本单位），输出不含
额外延迟。

边界填充
------
pad_mode = "zero"    ：信号两端之外补零（默认）。
pad_mode = "reflect" ：端点镜像（x[-1]=x[1], x[N]=x[N-2]），可减小
                       边缘瞬态；输入太短不足以镜像时退化为边缘复制。
右端填充只在 flush（已知输入结束）时生效，因此流式处理中末尾若干
输出样本只在 flush() 时产生。
"""

from __future__ import annotations

from math import gcd

import numpy as np

from .filters import design_lowpass, kaiser_num_taps

PAD_MODES = ("zero", "reflect")


class RationalResampler:
    """L/M 有理比重采样器（多相实现）。"""

    def __init__(
        self,
        up: int,
        down: int,
        atten_db: float = 80.0,
        transition: float = 0.1,
        num_taps: int | None = None,
        pad_mode: str = "zero",
    ):
        up = int(up)
        down = int(down)
        if up < 1 or down < 1:
            raise ValueError("up/down 必须为正整数")
        g = gcd(up, down)
        up //= g
        down //= g
        if pad_mode not in PAD_MODES:
            raise ValueError(f"pad_mode 必须是 {PAD_MODES} 之一")
        if not 0.0 < transition < 1.0:
            raise ValueError("transition 必须在 (0, 1) 内")

        self.up = up
        self.down = down
        self.pad_mode = pad_mode
        self.atten_db = float(atten_db)
        self.transition = float(transition)

        # 抗混叠截止：上采样域中 min(输入奈奎斯特, 输出奈奎斯特)
        edge = 0.5 / max(up, down)  # cycles/sample（上采样域）
        self.cutoff = edge * (1.0 - self.transition)
        tw = edge * self.transition

        if num_taps is None:
            num_taps = kaiser_num_taps(self.atten_db, tw)
        else:
            num_taps = int(num_taps)
            if num_taps % 2 == 0:
                num_taps += 1  # 保持奇数阶，群延迟为整数样本
        self.num_taps = num_taps

        self.h = design_lowpass(num_taps, self.cutoff, self.atten_db)
        self.taps_per_phase = -(-num_taps // up)  # ceil
        # 群延迟（上采样域样本数），奇数阶保证为整数
        self.group_delay_up = (num_taps - 1) // 2
        # 把 h 补零到 L 的整数倍，便于按相位等长切片
        self._hp = np.zeros(self.taps_per_phase * up)
        self._hp[:num_taps] = self.h

    # ------------------------------------------------------------------
    # 基本属性
    # ------------------------------------------------------------------
    @property
    def group_delay_in(self) -> float:
        """群延迟，输入样本单位。"""
        return self.group_delay_up / self.up

    def group_delay_seconds(self, fs_in: float) -> float:
        """群延迟，秒。"""
        return self.group_delay_in / float(fs_in)

    def output_length(self, n_in: int) -> int:
        """输入 n_in 个样本时的输出样本数：ceil(n_in * L / M)。"""
        n_in = int(n_in)
        if n_in < 0:
            raise ValueError("n_in 必须非负")
        return -(-n_in * self.up // self.down)

    # ------------------------------------------------------------------
    # 边界填充
    # ------------------------------------------------------------------
    def _pad_left(self, x: np.ndarray) -> np.ndarray:
        k = self.taps_per_phase
        if self.pad_mode == "reflect":
            if len(x) > k:
                return x[1 : k + 1][::-1]
            # 输入太短无法镜像：退化为边缘复制
            return np.full(k, x[0] if len(x) else 0.0)
        return np.zeros(k)

    def _pad_right(self, x: np.ndarray) -> np.ndarray:
        k = self.taps_per_phase
        if self.pad_mode == "reflect":
            if len(x) > k:
                return x[-k - 1 : -1][::-1]
            return np.full(k, x[-1] if len(x) else 0.0)
        return np.zeros(k)

    # ------------------------------------------------------------------
    # 核心计算（对给定输出下标集合求值）
    # ------------------------------------------------------------------
    def _compute(self, xp: np.ndarray, n_indices: np.ndarray) -> np.ndarray:
        """xp 为左端已填充的输入（x[i] 位于 xp[i + K]），计算输出下标集合。"""
        L, M = self.up, self.down
        gd, K = self.group_delay_up, self.taps_per_phase
        n = np.asarray(n_indices, dtype=np.int64)
        t = n * M + gd
        phase = t % L
        kmax = t // L
        y = np.empty(len(n), dtype=np.float64)
        j = np.arange(K)
        for p in range(L):
            mask = phase == p
            if not mask.any():
                continue
            km = kmax[mask]
            pos = km[:, None] - j[None, :] + K
            y[mask] = L * (xp[pos] @ self._hp[p::L])
        return y

    # ------------------------------------------------------------------
    # 整段处理
    # ------------------------------------------------------------------
    def process(self, x) -> np.ndarray:
        """整段重采样，返回 float64 数组，长度 output_length(len(x))。"""
        x = np.asarray(x, dtype=np.float64)
        n_out = self.output_length(len(x))
        if n_out == 0:
            return np.empty(0)
        xp = np.concatenate([self._pad_left(x), x, self._pad_right(x)])
        return self._compute(xp, np.arange(n_out))

    # ------------------------------------------------------------------
    # 分块（流式）处理
    # ------------------------------------------------------------------
    def block_processor(self) -> "BlockProcessor":
        return BlockProcessor(self)

    def process_blocks(self, x, block_size: int) -> np.ndarray:
        """按 block_size 分块处理并拼接，结果与 process() 一致。"""
        x = np.asarray(x, dtype=np.float64)
        bp = self.block_processor()
        outs = [
            bp.push(x[i : i + block_size])
            for i in range(0, len(x), block_size)
        ]
        outs.append(bp.flush())
        return np.concatenate(outs) if outs else np.empty(0)


class BlockProcessor:
    """有状态的分块处理器：push() 追加输入，flush() 收尾。

    push() 返回已经确定的输出样本（其所需输入窗口已全部到达）；
    依赖右端填充的末尾样本在 flush() 时返回。拼接 push*/flush 的
    全部返回值即得到与整段 process() 相同的结果。
    """

    def __init__(self, rs: RationalResampler):
        self._rs = rs
        self._chunks: list[np.ndarray] = []
        self._n_in = 0
        self._n_next = 0  # 下一个待发射的输出下标
        self._flushed = False

    def _buffer(self) -> np.ndarray:
        return np.concatenate(self._chunks) if self._chunks else np.empty(0)

    def push(self, chunk) -> np.ndarray:
        if self._flushed:
            raise RuntimeError("flush 之后不能再 push")
        rs = self._rs
        chunk = np.asarray(chunk, dtype=np.float64)
        self._chunks.append(chunk)
        self._n_in += len(chunk)
        if rs.pad_mode == "reflect" and self._n_in <= rs.taps_per_phase:
            return np.empty(0)  # 样本不足以构造左端镜像，暂缓
        # 输出 n 可确定  <=>  kmax(n) <= n_in - 1
        #                <=>  n*M + gd <= n_in*L - 1
        n_hi = -(-(self._n_in * rs.up - rs.group_delay_up) // rs.down)
        n_hi = max(n_hi, self._n_next)
        if n_hi <= self._n_next:
            return np.empty(0)
        x = self._buffer()
        xp = np.concatenate([rs._pad_left(x), x])
        idx = np.arange(self._n_next, n_hi)
        self._n_next = n_hi
        return rs._compute(xp, idx)

    def flush(self) -> np.ndarray:
        if self._flushed:
            raise RuntimeError("重复 flush")
        self._flushed = True
        rs = self._rs
        n_out = rs.output_length(self._n_in)
        if n_out <= self._n_next:
            return np.empty(0)
        x = self._buffer()
        xp = np.concatenate([rs._pad_left(x), x, rs._pad_right(x)])
        idx = np.arange(self._n_next, n_out)
        self._n_next = n_out
        return rs._compute(xp, idx)
