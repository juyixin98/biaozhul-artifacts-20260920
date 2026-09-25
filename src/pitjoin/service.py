"""零依赖 HTTP 服务（标准库 http.server）。

提供本地"特征时间点连接"基础设施接口，默认使用内置可复现合成数据，
也允许请求体内自带特征记录。仅监听 127.0.0.1，无任何外部网络调用。

接口
----
GET  /health                         健康检查
POST /join                           批量时间点连接
POST /explain                        单点选择并返回完整依据

请求体见 examples/ 下的样例。时间字段一律 ISO-8601（按 UTC 解析）。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .engine import Event, FeatureRecord, FeatureStore, JoinConfig, pit_join
from .serialize import evidence_to_dict, frame_to_dict
from .synthetic import build_canonical_dataset, build_generated_dataset
from .times import parse_iso

_MAX_BODY_BYTES = 1 << 20  # 1 MiB 上限，防止无界请求体
_GENERATED_LOCK = threading.Lock()
_GENERATED_CACHE: dict[str, Any] = {}


def _get_store(spec: dict[str, Any]) -> FeatureStore:
    """从请求构造（或取缓存的）特征库。

    请求体内若给了 ``records`` 则即时构建；否则按 ``dataset`` 取内置数据：
    ``canonical``（默认，每次重建，零状态）或 ``generated``（缓存复用）。
    """
    if "records" in spec:
        raw_records = spec["records"]
        if not isinstance(raw_records, list) or not raw_records:
            raise ValueError("records 必须是非空数组")
        records = [_parse_record(r) for r in raw_records]
        return FeatureStore(records)

    dataset = spec.get("dataset", "canonical")
    if dataset == "canonical":
        return build_canonical_dataset().store
    if dataset == "generated":
        with _GENERATED_LOCK:
            if "g" not in _GENERATED_CACHE:
                _GENERATED_CACHE["g"] = build_generated_dataset()
            return _GENERATED_CACHE["g"].store
    raise ValueError(f"未知 dataset: {dataset!r}（支持 canonical / generated）")


def _parse_record(r: Any) -> FeatureRecord:
    if not isinstance(r, dict):
        raise ValueError("每条 record 必须是对象")
    required = ("entity_id", "feature_name", "value", "effective_time", "ingest_time")
    missing = [k for k in required if k not in r]
    if missing:
        raise ValueError(f"record 缺少字段: {missing}")
    if not isinstance(r["entity_id"], str) or not isinstance(r["feature_name"], str):
        raise ValueError("entity_id / feature_name 必须是字符串")
    return FeatureRecord(
        entity_id=r["entity_id"],
        feature_name=r["feature_name"],
        value=float(r["value"]),
        effective_time=parse_iso(str(r["effective_time"])),
        ingest_time=parse_iso(str(r["ingest_time"])),
        version=int(r.get("version", 1)),
        record_id=(str(r["record_id"]) if r.get("record_id") is not None else None),
    )


def _parse_events(raw: Any) -> list[Event]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("events 必须是非空数组")
    events: list[Event] = []
    for e in raw:
        if not isinstance(e, dict) or "entity_id" not in e or "event_time" not in e:
            raise ValueError("每个事件需要 entity_id 与 event_time")
        label = e.get("label")
        events.append(
            Event(
                entity_id=str(e["entity_id"]),
                event_time=parse_iso(str(e["event_time"])),
                label=None if label is None else float(label),
            )
        )
    return events


def handle_join(body: dict[str, Any]) -> dict[str, Any]:
    store = _get_store(body)
    events = _parse_events(body.get("events"))
    features = body.get("features")
    if features is not None:
        if not isinstance(features, list) or not all(isinstance(f, str) for f in features):
            raise ValueError("features 必须是字符串数组")
        features = tuple(features)
    config = JoinConfig(
        use_event_as_of=bool(body.get("use_event_as_of", True)),
        features=features,
    )
    return frame_to_dict(pit_join(events, store, config))


def handle_explain(body: dict[str, Any]) -> dict[str, Any]:
    store = _get_store(body)
    for key in ("entity_id", "event_time", "feature_name"):
        if key not in body:
            raise ValueError(f"缺少字段: {key}")
    ev = store.select(
        str(body["entity_id"]),
        str(body["feature_name"]),
        parse_iso(str(body["event_time"])),
        bool(body.get("use_event_as_of", True)),
    )
    return evidence_to_dict(ev)


class _Handler(BaseHTTPRequestHandler):
    server_version = "pitjoin/0.1"

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 (http.server 命名)
        if self.path.split("?")[0] == "/health":
            self._send_json(200, {"status": "ok", "service": "pitjoin"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path not in ("/join", "/explain"):
            self._send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > _MAX_BODY_BYTES:
                raise ValueError("请求体为空或超过 1MiB 上限")
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("请求体必须是 JSON 对象")
            payload = handle_join(body) if path == "/join" else handle_explain(body)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(200, payload)

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
        return


def create_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    """构造（但不启动）HTTP 服务，便于测试与外部生命周期管理。"""
    return ThreadingHTTPServer((host, port), _Handler)


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    httpd = create_server(host, port)
    print(f"pitjoin 服务已启动: http://{host}:{port}  (Ctrl+C 停止)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
