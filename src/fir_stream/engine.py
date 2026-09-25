"""多通道流式 FIR 引擎:运行中滤波器切换 + 明确交叉渐变。

设计要点
--------
1. 每通道状态完全隔离:每通道持有独立的输入历史与滤波层列表,
   切换只作用于目标通道。
2. 交叉渐变(crossfade)通过“滤波层”实现:
   - 每层是一个以当前输入历史 prime 过的 StreamingFIR(因此新滤波器
     从切换点起就拥有正确的延迟线,无预热瞬态);
   - 每层带一个分段线性增益包络。切换时,所有现存层的目标增益置 0
     (在 fade_len 个样本内线性衰减),新层目标增益为 1(同样长度内
     线性上升)。任意时刻所有层增益之和恒为 1(线性插值保持该不变量),
     因此切换点不产生突跳。
   - 切换中再切换:对现存所有层(包括尚未淡入完成的层)再次执行
     “全部淡出 + 新层淡入”,不变量依然成立,层数自然收敛。
3. 空块(长度 0 的输入)是严格无操作:不推进包络、不更新历史、
   不创建通道。
4. 通道数变化:输入块通道数可逐块变化。新通道以零历史即时创建;
   消失的通道其状态被冻结保留,再次出现时继续。
"""

from __future__ import annotations

import numpy as np

from .core import StreamingFIR

#: 输入历史的默认容量(样本数)。切换时用历史 prime 新层,
#: 因此容量应不小于任何将使用的滤波器阶数(M-1)。
DEFAULT_HISTORY_CAPACITY = 8192


class _Layer:
    """一个滤波层:prime 过的 StreamingFIR + 分段线性增益包络。"""

    __slots__ = ("fir", "gain", "target", "remaining")

    def __init__(self, fir: StreamingFIR, gain: float, target: float, remaining: int):
        self.fir = fir
        self.gain = float(gain)      # 当前块起点的增益
        self.target = float(target)  # 渐变结束时的目标增益
        self.remaining = int(remaining)  # 剩余渐变样本数;0 表示增益恒定

    def start_fade(self, target: float, fade_len: int) -> None:
        self.target = float(target)
        self.remaining = int(fade_len)

    def block_gains(self, n: int) -> np.ndarray:
        """返回本块 n 个样本的增益包络,并推进内部渐变状态。"""
        if self.remaining <= 0:
            return np.full(n, self.gain, dtype=np.float64)
        k = min(n, self.remaining)
        j = np.arange(k, dtype=np.float64)
        step = (self.target - self.gain) / self.remaining
        g = np.empty(n, dtype=np.float64)
        g[:k] = self.gain + step * (j + 1.0)
        if k < n:
            g[k:] = self.target
        self.gain = self.target if k == self.remaining else float(g[k - 1])
        self.remaining -= k
        return g

    @property
    def exhausted(self) -> bool:
        """渐变完成且目标增益为 0 —— 该层可移除。"""
        return self.remaining == 0 and self.target == 0.0


class _ChannelState:
    __slots__ = ("layers", "history", "_capacity")

    def __init__(self, coeffs, history_capacity: int):
        self.layers = [_Layer(StreamingFIR(coeffs), gain=1.0, target=1.0, remaining=0)]
        self.history = np.zeros(0, dtype=np.float64)
        self._capacity = history_capacity

    def append_history(self, x: np.ndarray) -> None:
        if x.size == 0:
            return
        h = np.concatenate([self.history, x])
        if h.size > self._capacity:
            h = h[h.size - self._capacity:]
        self.history = h


class StreamingFIREngine:
    """多通道流式 FIR 引擎。

    参数
    ----
    default_coeffs : 初始(及新建通道默认使用的)FIR 系数。
    history_capacity : 每通道输入历史容量,需 >= 最大滤波器阶数。
    """

    def __init__(self, default_coeffs, history_capacity: int = DEFAULT_HISTORY_CAPACITY):
        h = np.asarray(default_coeffs, dtype=np.float64).ravel()
        if h.size == 0:
            raise ValueError("默认系数不能为空")
        if h.size - 1 > history_capacity:
            raise ValueError(
                f"history_capacity={history_capacity} 小于默认滤波器阶数 {h.size - 1}"
            )
        self._default_coeffs = h
        self._capacity = int(history_capacity)
        self._channels: dict[int, _ChannelState] = {}
        self._pending: dict[int, np.ndarray] = {}

    # ------------------------------------------------------------------ API

    @property
    def num_channels(self) -> int:
        """当前持有状态的通道数(含被冻结的通道)。"""
        return len(self._channels)

    def channel_ids(self) -> list[int]:
        return sorted(self._channels)

    def active_layer_count(self, ch: int) -> int:
        """通道当前的活动滤波层数(>1 表示正在交叉渐变)。"""
        return len(self._channels[ch].layers)

    def layer_gains(self, ch: int) -> list[float]:
        """通道各层当前增益(测试与诊断用)。"""
        return [ly.gain for ly in self._channels[ch].layers]

    def switch(self, coeffs, channels=None, fade_len: int = 0) -> None:
        """运行中切换滤波器系数。

        参数
        ----
        coeffs   : 新滤波器系数(长度可与旧滤波器不同)。
        channels : 目标通道 id 列表;None 表示“当前已知的所有通道”。
                   若当前还没有任何通道(尚未处理过输入),则记为
                   pending,之后新建的每个通道都使用该系数。
        fade_len : 交叉渐变长度(样本数)。0 表示硬切换(立即替换,
                   不保证无突跳);>0 时所有现存层线性淡出、新层线性
                   淡入,增益之和恒为 1,切换点无突跳。
        """
        h = np.asarray(coeffs, dtype=np.float64).ravel()
        if h.size == 0:
            raise ValueError("系数不能为空")
        if h.size - 1 > self._capacity:
            raise ValueError(
                f"滤波器阶数 {h.size - 1} 超过 history_capacity={self._capacity};"
                "请在构造引擎时增大 history_capacity"
            )
        fade_len = int(fade_len)
        if fade_len < 0:
            raise ValueError("fade_len 不能为负")

        targets = list(self._channels) if channels is None else [int(c) for c in channels]
        if channels is None:
            # 之后新建的通道也使用新系数
            self._default_coeffs = h
            self._pending.clear()
        if not targets:
            # 尚无通道:记录为 pending,作用于未来所有新通道
            self._pending[-1] = h
            return

        for ch in targets:
            st = self._channels.get(ch)
            if st is None:
                # 通道尚未出现:记录 pending,创建时生效
                self._pending[ch] = h
                continue
            if fade_len == 0:
                st.layers = [
                    _Layer(self._primed_fir(h, st.history), 1.0, 1.0, 0)
                ]
            else:
                for ly in st.layers:
                    ly.start_fade(0.0, fade_len)
                st.layers.append(
                    _Layer(self._primed_fir(h, st.history), 0.0, 1.0, fade_len)
                )

    def process(self, block) -> np.ndarray:
        """处理一块多通道输入。

        输入形状 (n,) 视为单通道,(n, C) 为多通道;返回形状与输入一致。
        空块(n=0)原样返回,不改变任何状态。
        """
        arr = np.asarray(block, dtype=np.float64)
        one_d = arr.ndim == 1
        if one_d:
            arr2 = arr.reshape(-1, 1)
        elif arr.ndim == 2:
            arr2 = arr
        else:
            raise ValueError("输入必须是一维或二维数组")
        n, c = arr2.shape
        if n == 0:
            return arr.copy()

        out = np.empty((n, c), dtype=np.float64)
        for ch in range(c):
            st = self._channels.get(ch)
            if st is None:
                st = self._new_channel(ch)
            x = arr2[:, ch]
            acc = np.zeros(n, dtype=np.float64)
            for ly in st.layers:
                acc += ly.block_gains(n) * ly.fir.process(x)
            st.layers = [ly for ly in st.layers if not ly.exhausted]
            st.append_history(x)
            out[:, ch] = acc
        return out.ravel() if one_d else out

    def drain(self) -> np.ndarray:
        """以零输入冲刷所有现存通道,返回各通道滤波器尾部。

        返回形状 (max_order, C),C 为当前最大通道 id + 1;
        不存在的通道列输出 0。与 process 的输出拼接后,即得到与
        np.convolve(x, h, 'full') 等价的完整长度结果。
        """
        if not self._channels:
            return np.zeros((0, 0), dtype=np.float64)
        c = max(self._channels) + 1
        tail = max(
            (ly.fir.order for st in self._channels.values() for ly in st.layers),
            default=0,
        )
        if tail == 0:
            return np.zeros((0, c), dtype=np.float64)
        return self.process(np.zeros((tail, c), dtype=np.float64))

    # ------------------------------------------------------------- internal

    def _primed_fir(self, coeffs, history: np.ndarray) -> StreamingFIR:
        fir = StreamingFIR(coeffs)
        fir.prime(history)
        return fir

    def _new_channel(self, ch: int) -> _ChannelState:
        coeffs = self._pending.pop(ch, None)
        if coeffs is None:
            coeffs = self._pending.get(-1, self._default_coeffs)
        st = _ChannelState(coeffs, self._capacity)
        self._channels[ch] = st
        return st
