"""JSON 入口: 从 JSON 请求文件读取两路消息, 输出配对结果 JSON.

用法:
    python -m sensor_pairing.cli request.json [-o result.json]

请求格式:
{
  "tolerance": 0.05,
  "cache_size": 100,
  "clock_offset_b": 0.0,          // 可选, 默认 0
  "estimate_offset": false,        // 可选, true 时忽略 clock_offset_b 并自动估计
  "stream_a": [{"id": "a0", "t": 1.0, "data": {...}}, ...],   // 按到达顺序
  "stream_b": [{"id": "b0", "t": 1.02, "data": {...}}, ...]
}
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from sensor_pairing.clock import estimate_clock_offset
from sensor_pairing.matcher import TimePairingMatcher
from sensor_pairing.models import SensorMessage, StreamId


def _parse_messages(raw: Any, stream_name: str) -> list[SensorMessage]:
    if not isinstance(raw, list):
        raise ValueError(f"{stream_name} 必须是数组")
    messages: list[SensorMessage] = []
    seen_ids: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or "id" not in item or "t" not in item:
            raise ValueError(f"{stream_name}[{i}] 必须包含 id 与 t 字段")
        msg_id = str(item["id"])
        if msg_id in seen_ids:
            raise ValueError(f"{stream_name} 中 id 重复: {msg_id}")
        seen_ids.add(msg_id)
        timestamp = float(item["t"])
        messages.append(
            SensorMessage(msg_id=msg_id, timestamp=timestamp, data=item.get("data"))
        )
    return messages


def run_request(request: dict[str, Any]) -> dict[str, Any]:
    """执行一次配对请求, 返回结果字典."""
    try:
        tolerance = float(request["tolerance"])
        cache_size = int(request["cache_size"])
        stream_a = _parse_messages(request.get("stream_a", []), "stream_a")
        stream_b = _parse_messages(request.get("stream_b", []), "stream_b")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"请求格式错误: {exc}") from exc

    if request.get("estimate_offset"):
        clock_offset_b = estimate_clock_offset(
            [m.timestamp for m in stream_a],
            [m.timestamp for m in stream_b],
        )
    else:
        clock_offset_b = float(request.get("clock_offset_b", 0.0))

    matcher = TimePairingMatcher(
        tolerance=tolerance,
        cache_size=cache_size,
        clock_offset_b=clock_offset_b,
    )
    # 到达顺序: 先按请求数组顺序依次喂入 A 全部、再 B 全部;
    # 若提供 arrival 字段则按显式到达序列处理.
    arrival = request.get("arrival")
    if arrival is None:
        for m in stream_a:
            matcher.add(StreamId.A, m)
        for m in stream_b:
            matcher.add(StreamId.B, m)
    else:
        by_id = {
            ("a", m.msg_id): m for m in stream_a
        } | {("b", m.msg_id): m for m in stream_b}
        for i, ref in enumerate(arrival):
            key = (str(ref["stream"]).lower(), str(ref["id"]))
            if key not in by_id:
                raise ValueError(f"arrival[{i}] 引用了不存在的消息: {key}")
            matcher.add(StreamId(key[0]), by_id[key])

    result = matcher.finish().to_dict()
    result["config"] = {
        "tolerance": tolerance,
        "cache_size": cache_size,
        "clock_offset_b": clock_offset_b,
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="多传感器时间配对(JSON 入口)")
    parser.add_argument("request", help="请求 JSON 文件路径, 或 '-' 表示标准输入")
    parser.add_argument("-o", "--output", help="结果输出路径, 缺省打印到标准输出")
    args = parser.parse_args(argv)

    try:
        if args.request == "-":
            request = json.load(sys.stdin)
        else:
            with open(args.request, encoding="utf-8") as f:
                request = json.load(f)
        result = run_request(request)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
