"""JSON 入口：请求解析、校验与处理。

请求为一个 JSON 对象，``mode`` 取 ``"synthetic"`` 或 ``"messages"``。

1. synthetic 模式::

    {
      "mode": "synthetic",
      "config": { ... run_synthetic_pairing 的配置 ... }
    }

2. messages 模式：直接提供两类传感器消息（时间戳为各自钟面读数），
   可选地给定 B 钟偏移与到达顺序::

    {
      "mode": "messages",
      "tolerance": 0.05,
      "max_buffer_size": 1000,
      "max_out_of_orderness": 0.0,
      "clock_offset_b": -0.2,
      "streams": {
        "a": [{"id": "a1", "timestamp": 0.0, "payload": null}],
        "b": [{"id": "b1", "timestamp": 0.21, "payload": null}]
      },
      "arrival": [["a", "a1"], ["b", "b1"]]
    }

   ``arrival`` 缺省时按先 A 后 B、各自数组顺序推送。响应同时给出在线
   （有界缓存、含拒绝原因）与离线（权威定义）结果及二者是否一致。
"""

import json
from typing import Any

from .clock import corrected_time
from .matcher import MessageMatcher
from .models import Match, Message
from .offline import offline_pair
from .pipeline import run_synthetic_pairing


class RequestError(ValueError):
    """请求内容不合法（边界校验失败）。"""


def _require_number(obj: dict, key: str, default: float | None = None) -> float:
    if key not in obj:
        if default is None:
            raise RequestError(f"缺少必填字段: {key}")
        return float(default)
    value = obj[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RequestError(f"字段 {key} 必须是数字")
    return float(value)


def _parse_message(raw: Any, stream: str) -> Message:
    if not isinstance(raw, dict):
        raise RequestError(f"{stream} 流中的每条消息必须是 JSON 对象")
    if "id" not in raw or "timestamp" not in raw:
        raise RequestError(f"{stream} 流消息缺少 id 或 timestamp")
    if not isinstance(raw["id"], str) or not raw["id"]:
        raise RequestError(f"{stream} 流消息 id 必须是非空字符串")
    ts = raw["timestamp"]
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        raise RequestError(f"{stream} 流消息 {raw['id']} 的 timestamp 必须是数字")
    return Message(id=raw["id"], timestamp=float(ts), payload=raw.get("payload"))


def _process_messages(req: dict) -> dict:
    tolerance = _require_number(req, "tolerance")
    if tolerance < 0:
        raise RequestError("tolerance 不能为负")
    buf = int(_require_number(req, "max_buffer_size", 1000))
    ooo = _require_number(req, "max_out_of_orderness", 0.0)
    offset_b = _require_number(req, "clock_offset_b", 0.0)
    if buf <= 0:
        raise RequestError("max_buffer_size 必须为正整数")
    if ooo < 0:
        raise RequestError("max_out_of_orderness 不能为负")

    streams = req.get("streams")
    if not isinstance(streams, dict):
        raise RequestError("缺少 streams 对象（含 a、b 两个数组）")
    raw_a = streams.get("a", [])
    raw_b = streams.get("b", [])
    if not isinstance(raw_a, list) or not isinstance(raw_b, list):
        raise RequestError("streams.a 与 streams.b 必须是数组")

    msgs_a_raw = [_parse_message(m, "a") for m in raw_a]
    msgs_b_raw = [_parse_message(m, "b") for m in raw_b]
    if len({m.id for m in msgs_a_raw}) != len(msgs_a_raw):
        raise RequestError("streams.a 中存在重复 id")
    if len({m.id for m in msgs_b_raw}) != len(msgs_b_raw):
        raise RequestError("streams.b 中存在重复 id")
    msgs_a = [Message(m.id, m.timestamp, m.payload) for m in msgs_a_raw]
    msgs_b = [
        Message(m.id, corrected_time(m.timestamp, offset_b), m.payload)
        for m in msgs_b_raw
    ]

    # 到达顺序
    arrival = req.get("arrival")
    if arrival is None:
        order = [("a", m.id) for m in msgs_a_raw] + [("b", m.id) for m in msgs_b_raw]
    else:
        if not isinstance(arrival, list):
            raise RequestError("arrival 必须是 [stream, id] 数组")
        order = []
        for item in arrival:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or item[0] not in ("a", "b")
                or not isinstance(item[1], str)
            ):
                raise RequestError("arrival 每项必须是 [\"a\"|\"b\", id]")
            order.append((item[0], item[1]))

    ida = {m.id: m for m in msgs_a}
    idb = {m.id: m for m in msgs_b}
    for stream, mid in order:
        if (mid not in ida) if stream == "a" else (mid not in idb):
            raise RequestError(f"arrival 引用了不存在的消息: {stream}:{mid}")
    if len(order) != len(msgs_a) + len(msgs_b):
        raise RequestError("arrival 必须包含且仅包含所有消息各一次")

    matcher = MessageMatcher(
        tolerance=tolerance,
        max_buffer_size=buf,
        max_out_of_orderness=ooo,
    )
    for stream, mid in order:
        matcher.push(stream, ida[mid] if stream == "a" else idb[mid])
    matcher.flush()

    offline = offline_pair(
        msgs_a,
        msgs_b,
        tolerance,
        raw_a={m.id: m.timestamp for m in msgs_a_raw},
        raw_b={m.id: m.timestamp for m in msgs_b_raw},
    )

    overflowed = any(r.reason == "buffer_overflow" for r in matcher.rejects)
    online_edges = {(m.id_a, m.id_b) for m in matcher.matches}
    offline_edges = {(m.id_a, m.id_b) for m in offline.matches}
    consistent = (not overflowed) and online_edges == offline_edges

    # 回填校正前的原始钟面读数，便于审计时钟校正效果
    raw_ts_a = {m.id: m.timestamp for m in msgs_a_raw}
    raw_ts_b = {m.id: m.timestamp for m in msgs_b_raw}
    online_match_dicts = [
        Match(
            m.id_a,
            m.id_b,
            m.t_a,
            m.t_b,
            raw_ts_a.get(m.id_a, m.raw_a),
            raw_ts_b.get(m.id_b, m.raw_b),
        ).as_dict()
        for m in matcher.matches
    ]

    return {
        "mode": "messages",
        "parameters": {
            "tolerance": tolerance,
            "max_buffer_size": buf,
            "max_out_of_orderness": ooo,
            "clock_offset_b": offset_b,
            "n_arrival_events": len(order),
        },
        "online": {
            "matches": online_match_dicts,
            "rejects": [r.as_dict() for r in matcher.rejects],
        },
        "offline": offline.as_dict(),
        "consistent": consistent,
        "summary": {
            "n_a": len(msgs_a),
            "n_b": len(msgs_b),
            "online_matches": len(matcher.matches),
            "online_rejects": len(matcher.rejects),
            "reject_breakdown": _reject_breakdown(matcher.rejects),
            "offline_matches": len(offline.matches),
        },
    }


def _reject_breakdown(rejects) -> dict:
    counts: dict[str, int] = {}
    for r in rejects:
        counts[r.reason] = counts.get(r.reason, 0) + 1
    return counts


def _process_synthetic(req: dict) -> dict:
    cfg = req.get("config", {})
    if not isinstance(cfg, dict):
        raise RequestError("synthetic 模式需要 config 对象")
    out = run_synthetic_pairing(cfg)
    body = out.as_dict()
    body["mode"] = "synthetic"
    body["summary"] = {
        "online_matches": len(out.online_matches),
        "online_rejects": len(out.online_rejects),
        "reject_breakdown": _reject_breakdown(out.online_rejects),
        "offline_matches": len(out.offline_matches),
        "offline_unmatched_a": len(out.offline_unmatched_a),
        "offline_unmatched_b": len(out.offline_unmatched_b),
    }
    return body


def process_request(request_text: str) -> dict:
    """解析并处理一个 JSON 请求字符串，返回可序列化的响应字典。"""
    try:
        req = json.loads(request_text)
    except json.JSONDecodeError as exc:
        raise RequestError(f"JSON 解析失败: {exc.msg} (行 {exc.lineno} 列 {exc.colno})")
    if not isinstance(req, dict):
        raise RequestError("请求顶层必须是 JSON 对象")
    mode = req.get("mode")
    if mode == "synthetic":
        return _process_synthetic(req)
    if mode == "messages":
        return _process_messages(req)
    raise RequestError("mode 必须是 'synthetic' 或 'messages'")
