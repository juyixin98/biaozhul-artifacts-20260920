"""3. 不同分块大小:输出必须与分块无关(分块不变性)。

同一段处理用各种块长执行,输出彼此逐样本一致,
且与独立逐样本参考实现一致。
"""

import unittest

import numpy as np

import _bootstrap  # noqa: F401
from fir_stream.engine import StreamingFIREngine
from fir_stream.reference import LayeredReferenceFIR


def run_chunks(x, coeffs0, schedule, chunks, capacity=8192, drain=True):
    """按给定块长序列(循环使用)执行带切换计划的引擎。

    与服务层一致:切换事件位置被强制对齐为块边界(必要时截短当前块)。
    """
    eng = StreamingFIREngine(coeffs0, history_capacity=capacity)
    n = x.shape[0]
    events = {int(e["at"]): e for e in schedule}
    outs, pos, ci = [], 0, 0
    while pos < n:
        ev = events.get(pos)
        if ev is not None:
            eng.switch(ev["filter"], channels=ev.get("channels"),
                       fade_len=ev.get("fade_len", 0))
        size = min(chunks[ci % len(chunks)], n - pos)
        ci += 1
        future = [a for a in events if pos < a < pos + size]
        if future:
            size = min(future) - pos
        outs.append(eng.process(x[pos:pos + size]))
        pos += size
    ev = events.get(n)
    if ev is not None:
        eng.switch(ev["filter"], channels=ev.get("channels"),
                   fade_len=ev.get("fade_len", 0))
    if drain:
        tail = eng.drain()
        outs.append(tail if x.ndim == 2 else tail.ravel())
    return np.concatenate(outs, axis=0)


def run_reference(x, coeffs0, schedule):
    ref = LayeredReferenceFIR(coeffs0)
    n = x.shape[0]
    events = {int(e["at"]): e for e in schedule}
    outs, pos = [], 0
    while pos < n:
        ev = events.get(pos)
        if ev is not None:
            ref.switch(ev["filter"], channels=ev.get("channels"),
                       fade_len=ev.get("fade_len", 0))
        outs.append(ref.process(x[pos:pos + 1]))
        pos += 1
    if n in events:
        ref.switch(events[n]["filter"], channels=events[n].get("channels"),
                   fade_len=events[n].get("fade_len", 0))
    tail = ref.drain()
    outs.append(tail if x.ndim == 2 else tail.ravel())
    return np.concatenate(outs, axis=0)


class TestChunkInvariance(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(3)
        self.x = self.rng.standard_normal((2000, 2))
        self.h0 = self.rng.standard_normal(20)
        self.h1 = self.rng.standard_normal(40)
        self.h2 = self.rng.standard_normal(12)
        self.schedule = [
            {"at": 400, "filter": self.h1, "fade_len": 128, "channels": None},
            {"at": 1200, "filter": self.h2, "fade_len": 64, "channels": None},
        ]
        self.ref_y = run_reference(self.x, self.h0, self.schedule)

    def test_chunk_sizes_identical(self):
        chunkings = [
            [1], [2], [7], [128], [512], [2000],
            [1, 1, 1000],
            [333, 333, 333, 333, 333, 335],
            [512, 128, 64, 32, 1024],
            [399, 401, 799, 401],  # 故意包含/避开切换点 400 与 1200
        ]
        first = None
        for chunks in chunkings:
            # run_chunks 会把切换事件位置对齐为块边界(同服务层行为)
            y = run_chunks(self.x, self.h0, self.schedule, chunks)
            if first is None:
                first = y
            self.assertEqual(y.shape, first.shape, f"chunks={chunks}")
            np.testing.assert_allclose(
                y, first, atol=1e-12, err_msg=f"chunks={chunks}")
            np.testing.assert_allclose(
                y, self.ref_y, atol=1e-10, err_msg=f"chunks={chunks}")

    def test_events_aligned_to_boundaries(self):
        # 引擎的 switch 只能在块之间调用;服务层会把事件位置对齐为
        # 块边界。这里手动按事件位置分块,验证与逐样本参考一致。
        eng = StreamingFIREngine(self.h0)
        outs = []
        outs.append(eng.process(self.x[:400]))
        eng.switch(self.h1, fade_len=128)
        outs.append(eng.process(self.x[400:1200]))
        eng.switch(self.h2, fade_len=64)
        outs.append(eng.process(self.x[1200:]))
        y = np.concatenate(outs + [eng.drain()], axis=0)
        np.testing.assert_allclose(y, self.ref_y, atol=1e-10)

    def test_chunk_invariance_no_switch(self):
        x = self.rng.standard_normal(1000)
        h = self.rng.standard_normal(25)
        ys = []
        for chunks in ([1], [13], [512], [300, 400, 300]):
            ys.append(run_chunks(x, h, [], chunks))
        for y in ys[1:]:
            np.testing.assert_allclose(y, ys[0], atol=1e-12)


if __name__ == "__main__":
    unittest.main()
