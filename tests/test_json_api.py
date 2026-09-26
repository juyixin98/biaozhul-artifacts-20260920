"""JSON 入口测试：两种模式、时钟校正、乱序 arrival、校验失败。"""

import json

import pytest

from sensor_matcher.json_api import RequestError, process_request


@pytest.mark.unit
def test_messages_mode_basic() -> None:
    req = json.dumps(
        {
            "mode": "messages",
            "tolerance": 0.05,
            "streams": {
                "a": [
                    {"id": "a1", "timestamp": 0.0},
                    {"id": "a2", "timestamp": 1.0},
                ],
                "b": [
                    {"id": "b1", "timestamp": 0.02},
                    {"id": "b2", "timestamp": 1.01},
                ],
            },
        }
    )
    resp = process_request(req)
    assert resp["consistent"] is True
    pairs = {(m["id_a"], m["id_b"]) for m in resp["online"]["matches"]}
    assert pairs == {("a1", "b1"), ("a2", "b2")}
    assert resp["summary"]["online_matches"] == 2


@pytest.mark.unit
def test_messages_mode_clock_offset_correction() -> None:
    # B 钟慢 0.2：b1 读数 0.21，校正后 0.01，与 a1@0.0 可配
    req = json.dumps(
        {
            "mode": "messages",
            "tolerance": 0.05,
            "clock_offset_b": -0.2,
            "streams": {
                "a": [{"id": "a1", "timestamp": 0.0}],
                "b": [{"id": "b1", "timestamp": 0.21}],
            },
        }
    )
    resp = process_request(req)
    assert len(resp["online"]["matches"]) == 1
    match = resp["online"]["matches"][0]
    assert match["dt_raw"] == pytest.approx(0.21)
    assert match["dt_corrected"] == pytest.approx(0.01, abs=1e-9)


@pytest.mark.unit
def test_messages_mode_out_of_order_arrival() -> None:
    req = json.dumps(
        {
            "mode": "messages",
            "tolerance": 0.05,
            "max_out_of_orderness": 1.0,
            "streams": {
                "a": [{"id": "a1", "timestamp": 0.0}, {"id": "a2", "timestamp": 1.0}],
                "b": [{"id": "b1", "timestamp": 0.02}, {"id": "b2", "timestamp": 1.01}],
            },
            # 晚时间戳先到
            "arrival": [["b", "b2"], ["a", "a2"], ["b", "b1"], ["a", "a1"]],
        }
    )
    resp = process_request(req)
    assert resp["consistent"] is True
    pairs = {(m["id_a"], m["id_b"]) for m in resp["online"]["matches"]}
    assert pairs == {("a1", "b1"), ("a2", "b2")}


@pytest.mark.unit
def test_messages_mode_buffer_overflow() -> None:
    req = json.dumps(
        {
            "mode": "messages",
            "tolerance": 0.05,
            "max_buffer_size": 1,
            "streams": {
                "a": [{"id": "a1", "timestamp": 0.0}, {"id": "a2", "timestamp": 1.0}],
                "b": [],
            },
        }
    )
    resp = process_request(req)
    breakdown = resp["summary"]["reject_breakdown"]
    # a2 到达时 a1 仍占缓存（B 从未来过，水位线 -inf）
    assert breakdown.get("buffer_overflow") == 1


@pytest.mark.unit
def test_synthetic_mode_consistent() -> None:
    req = json.dumps(
        {
            "mode": "synthetic",
            "config": {
                "tolerance": 0.08,
                "duration": 8.0,
                "sensor_a": {"rate_hz": 10.0, "delay_jitter": 0.05, "seed": 1},
                "sensor_b": {
                    "rate_hz": 7.0,
                    "clock_offset": -0.2,
                    "delay_jitter": 0.05,
                    "seed": 2,
                },
            },
        }
    )
    resp = process_request(req)
    assert resp["mode"] == "synthetic"
    assert resp["consistent"] is True
    assert resp["summary"]["online_matches"] > 10


@pytest.mark.unit
@pytest.mark.parametrize(
    "request_text",
    [
        "{not json",
        "[]",
        json.dumps({"mode": "nope"}),
        json.dumps({"mode": "messages"}),  # 缺 tolerance/streams
        json.dumps(
            {"mode": "messages", "tolerance": -1, "streams": {"a": [], "b": []}}
        ),
        json.dumps(
            {
                "mode": "messages",
                "tolerance": 0.05,
                "streams": {"a": [{"timestamp": 0.0}], "b": []},
            }
        ),
        json.dumps(
            {
                "mode": "messages",
                "tolerance": 0.05,
                "streams": {"a": [{"id": "a1", "timestamp": 0.0}], "b": []},
                "arrival": [["a", "a1"], ["a", "ghost"]],
            }
        ),
        json.dumps(
            {
                "mode": "messages",
                "tolerance": 0.05,
                "streams": {"a": [{"id": "a1", "timestamp": 0.0}], "b": []},
                "arrival": [["a", "a1"], ["a", "a1"]],
            }
        ),
        json.dumps({"mode": "synthetic", "config": []}),
        # 数字字段类型错误
        json.dumps({"mode": "messages", "tolerance": "0.05", "streams": {}}),
        # 非法缓存/乱序参数
        json.dumps(
            {"mode": "messages", "tolerance": 0.05, "max_buffer_size": 0,
             "streams": {"a": [], "b": []}}
        ),
        json.dumps(
            {"mode": "messages", "tolerance": 0.05, "max_out_of_orderness": -1,
             "streams": {"a": [], "b": []}}
        ),
        # streams 结构错误
        json.dumps({"mode": "messages", "tolerance": 0.05, "streams": []}),
        json.dumps(
            {"mode": "messages", "tolerance": 0.05,
             "streams": {"a": "not-list", "b": []}}
        ),
        # 消息字段错误
        json.dumps(
            {"mode": "messages", "tolerance": 0.05,
             "streams": {"a": ["x"], "b": []}}
        ),
        json.dumps(
            {"mode": "messages", "tolerance": 0.05,
             "streams": {"a": [{"id": "", "timestamp": 0.0}], "b": []}}
        ),
        json.dumps(
            {"mode": "messages", "tolerance": 0.05,
             "streams": {"a": [{"id": "a1", "timestamp": "x"}], "b": []}}
        ),
        # 流内重复 id
        json.dumps(
            {"mode": "messages", "tolerance": 0.05,
             "streams": {"a": [{"id": "a1", "timestamp": 0.0},
                               {"id": "a1", "timestamp": 1.0}], "b": []}}
        ),
        # arrival 结构错误
        json.dumps(
            {"mode": "messages", "tolerance": 0.05,
             "streams": {"a": [{"id": "a1", "timestamp": 0.0}], "b": []},
             "arrival": "a1"}
        ),
        json.dumps(
            {"mode": "messages", "tolerance": 0.05,
             "streams": {"a": [{"id": "a1", "timestamp": 0.0}], "b": []},
             "arrival": [["c", "a1"]]}
        ),
    ],
)
def test_invalid_requests_rejected(request_text: str) -> None:
    with pytest.raises(RequestError):
        process_request(request_text)
