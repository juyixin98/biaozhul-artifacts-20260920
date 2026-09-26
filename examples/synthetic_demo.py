"""合成数据端到端演示: 不同频率 + 乱序 + 时钟偏移 + 有限缓存.

运行: python3 examples/synthetic_demo.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sensor_pairing.clock import estimate_offset_from_data
from sensor_pairing.matcher import TimePairingMatcher
from sensor_pairing.models import StreamId, UnmatchedReason
from sensor_pairing.synthetic import (
    generate_synthetic_streams,
    interleave_by_arrival,
)


def main() -> None:
    streams = generate_synthetic_streams(
        duration=10.0,
        rate_a_hz=50.0,
        rate_b_hz=30.0,
        clock_offset_b=0.1,
        noise_std=0.005,
        time_jitter_std=0.0005,
        disorder_window=3,
        seed=9,
    )
    print(f"流 A: {len(streams.stream_a)} 条 (50Hz), 流 B: {len(streams.stream_b)} 条 (30Hz)")
    print(f"真实时钟偏移 clock_offset_b = {streams.clock_offset_b}")

    offset = estimate_offset_from_data(
        [m.timestamp for m in streams.stream_a],
        [[m.data["x"], m.data["y"]] for m in streams.stream_a],
        [m.timestamp for m in streams.stream_b],
        [[m.data["x"], m.data["y"]] for m in streams.stream_b],
    )
    print(f"数据辅助估计偏移 = {offset:.4f}s")

    matcher = TimePairingMatcher(tolerance=0.02, cache_size=64, clock_offset_b=offset)
    for stream, m in interleave_by_arrival(streams.stream_a, streams.stream_b, seed=4):
        matcher.add(StreamId(stream), m)
    result = matcher.finish()

    print(f"配对成功: {len(result.pairs)} 对")
    for reason in UnmatchedReason:
        n = sum(1 for u in result.unmatched if u.reason is reason)
        print(f"未配对({reason.value}): {n} 条")


if __name__ == "__main__":
    main()
