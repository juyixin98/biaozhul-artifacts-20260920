"""JSON 入口测试."""

import json

import pytest

from sensor_pairing.cli import main, run_request


def sample_request() -> dict:
    return {
        "tolerance": 0.05,
        "cache_size": 10,
        "stream_a": [
            {"id": "a1", "t": 1.00, "data": {"x": 1.0}},
            {"id": "a2", "t": 2.00},
        ],
        "stream_b": [
            {"id": "b1", "t": 1.02},
            {"id": "b2", "t": 5.00},
        ],
        "arrival": [
            {"stream": "a", "id": "a1"},
            {"stream": "b", "id": "b1"},
            {"stream": "a", "id": "a2"},
            {"stream": "b", "id": "b2"},
        ],
    }


class TestRunRequest:
    def test_pairs_and_unmatched_with_reasons(self):
        result = run_request(sample_request())
        assert result["stats"]["num_pairs"] == 1
        assert result["pairs"][0]["a"]["id"] == "a1"
        assert result["pairs"][0]["b"]["id"] == "b1"
        unmatched = {(u["message"]["id"], u["reason"]) for u in result["unmatched"]}
        assert unmatched == {
            ("a2", "no_match_within_tolerance"),
            ("b2", "no_match_within_tolerance"),
        }

    def test_default_arrival_is_stream_a_then_b(self):
        request = sample_request()
        del request["arrival"]
        result = run_request(request)
        assert result["stats"]["num_pairs"] == 1

    def test_estimate_offset(self):
        request = {
            "tolerance": 0.05,
            "cache_size": 10,
            "estimate_offset": True,
            "stream_a": [{"id": f"a{i}", "t": float(i)} for i in range(5)],
            "stream_b": [{"id": f"b{i}", "t": float(i) + 0.3} for i in range(5)],
        }
        result = run_request(request)
        assert result["config"]["clock_offset_b"] == pytest.approx(0.3)
        assert result["stats"]["num_pairs"] == 5

    def test_duplicate_id_rejected(self):
        request = sample_request()
        request["stream_a"].append({"id": "a1", "t": 9.0})
        with pytest.raises(ValueError, match="id 重复"):
            run_request(request)

    def test_missing_field_rejected(self):
        request = sample_request()
        request["stream_b"] = [{"id": "b1"}]
        with pytest.raises(ValueError, match="id 与 t"):
            run_request(request)

    def test_arrival_unknown_reference_rejected(self):
        request = sample_request()
        request["arrival"] = [{"stream": "a", "id": "nope"}]
        with pytest.raises(ValueError, match="不存在的消息"):
            run_request(request)


class TestMain:
    def test_cli_roundtrip(self, tmp_path):
        req_path = tmp_path / "request.json"
        out_path = tmp_path / "result.json"
        req_path.write_text(json.dumps(sample_request()), encoding="utf-8")
        assert main([str(req_path), "-o", str(out_path)]) == 0
        result = json.loads(out_path.read_text(encoding="utf-8"))
        assert result["stats"]["num_pairs"] == 1

    def test_cli_bad_request_returns_1(self, tmp_path, capsys):
        req_path = tmp_path / "bad.json"
        req_path.write_text('{"tolerance": 0.05}', encoding="utf-8")
        assert main([str(req_path)]) == 1
        assert "错误" in capsys.readouterr().err
