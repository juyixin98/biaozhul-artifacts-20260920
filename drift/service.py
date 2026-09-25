"""本地 HTTP 服务：基线建档与窗口漂移查询（仅用标准库）。

接口（请求/响应均为 JSON）：

- ``GET  /healthz``：存活检查；
- ``POST /v1/monitor``：一次性传入基线与当前窗口，直接出指标；
- ``POST /v1/baselines``：由基线数据建档，返回 ``baseline_id`` 与各特征桶定义；
- ``POST /v1/drift``：携带 ``baseline_id`` 与当前窗口数据，计算漂移；
- ``GET  /v1/baselines/<id>``：查看已存基线（只含桶定义与计数，不含原始数据）；
- ``POST /v1/demo``：服务端生成可复现合成数据并计算，便于快速自检。

设计约束：

- 纯内存存储，重启即失效（本地基础设施服务，不引入持久化依赖）；
- 不保存原始样本，只保存桶定义与基线桶计数；
- 请求体大小受限，参数在边界处校验，非法输入返回 4xx 且不泄漏内部堆栈。
"""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .binning import BinCounts, FixedBins, assign_counts, fit_fixed_bins
from .metrics import (
    DEFAULT_ALPHA,
    DEFAULT_EPSILON,
    DEFAULT_MIN_SAMPLE,
    SmoothingMethod,
    compute_drift,
)
from .pipeline import monitor_features
from . import synthetic

MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MiB 上限，防止本地服务被超大请求拖垮
MAX_FEATURES = 200
MAX_VALUES_PER_FEATURE = 1_000_000
VALID_SMOOTHING = ("none", "laplace", "floor")
VALID_DEMOS = (
    "same",
    "shifted",
    "scaled",
    "all_missing",
    "small_sample",
    "extremes",
)


class ApiError(Exception):
    """可预期的客户端错误，映射为 4xx。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class BaselineProfile:
    """内存中的基线档案：每特征一个桶定义与基线计数。"""

    baseline_id: str
    features: dict[str, dict[str, Any]]  # name -> {bins: FixedBins, counts: list[int], n_total}

    def to_summary(self) -> dict[str, Any]:
        return {
            "baseline_id": self.baseline_id,
            "features": {
                name: {
                    "bins": item["bins"].to_dict(),
                    "baseline_counts": [int(x) for x in item["counts"]],
                    "n_total": int(item["n_total"]),
                }
                for name, item in self.features.items()
            },
        }


class BaselineStore:
    """线程安全的内存基线存储。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._profiles: dict[str, BaselineProfile] = {}

    def save(self, profile: BaselineProfile) -> None:
        with self._lock:
            self._profiles[profile.baseline_id] = profile

    def get(self, baseline_id: str) -> BaselineProfile:
        with self._lock:
            profile = self._profiles.get(baseline_id)
        if profile is None:
            raise ApiError(HTTPStatus.NOT_FOUND, f"基线不存在: {baseline_id}")
        return profile


def _require_object(body: Any, name: str) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{name} 必须是 JSON 对象")
    return body


def _parse_features(body: dict[str, Any], key: str) -> dict[str, list[float | None]]:
    """解析并校验 ``{"特征名": [数值或 null, ...]}`` 结构。"""
    raw = body.get(key)
    if not isinstance(raw, dict) or not raw:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"'{key}' 必须是非空对象：特征名 -> 数值数组")
    if len(raw) > MAX_FEATURES:
        raise ApiError(
            HTTPStatus.BAD_REQUEST, f"特征数超过上限 {MAX_FEATURES}"
        )
    parsed: dict[str, list[float | None]] = {}
    for name, seq in raw.items():
        if not isinstance(name, str) or not name:
            raise ApiError(HTTPStatus.BAD_REQUEST, "特征名必须是非空字符串")
        if not isinstance(seq, list):
            raise ApiError(
                HTTPStatus.BAD_REQUEST, f"特征 {name!r} 的值必须是数组"
            )
        if len(seq) > MAX_VALUES_PER_FEATURE:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                f"特征 {name!r} 样本数超过上限 {MAX_VALUES_PER_FEATURE}",
            )
        values: list[float | None] = []
        for v in seq:
            if v is None:
                values.append(None)
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    f"特征 {name!r} 含非法元素（只接受数值或 null）: {v!r}",
                )
            values.append(float(v))
        parsed[name] = values
    return parsed


def _parse_options(body: dict[str, Any]) -> dict[str, Any]:
    n_bins = body.get("n_bins", 10)
    if not isinstance(n_bins, int) or isinstance(n_bins, bool) or n_bins < 1:
        raise ApiError(HTTPStatus.BAD_REQUEST, "n_bins 必须是 >= 1 的整数")
    smoothing = body.get("smoothing", "laplace")
    if smoothing not in VALID_SMOOTHING:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"smoothing 必须是 {VALID_SMOOTHING} 之一",
        )
    alpha = body.get("alpha", DEFAULT_ALPHA)
    if not isinstance(alpha, (int, float)) or isinstance(alpha, bool) or alpha <= 0:
        raise ApiError(HTTPStatus.BAD_REQUEST, "alpha 必须是正数")
    epsilon = body.get("epsilon", DEFAULT_EPSILON)
    if (
        not isinstance(epsilon, (int, float))
        or isinstance(epsilon, bool)
        or not 0 < epsilon < 1
    ):
        raise ApiError(HTTPStatus.BAD_REQUEST, "epsilon 必须在 (0,1) 内")
    min_sample = body.get("min_sample", DEFAULT_MIN_SAMPLE)
    if (
        not isinstance(min_sample, int)
        or isinstance(min_sample, bool)
        or min_sample < 0
    ):
        raise ApiError(HTTPStatus.BAD_REQUEST, "min_sample 必须是非负整数")
    return {
        "n_bins": n_bins,
        "smoothing": smoothing,
        "alpha": float(alpha),
        "epsilon": float(epsilon),
        "min_sample": min_sample,
    }


def _build_profile(
    features: dict[str, list[float | None]],
    n_bins: int,
) -> BaselineProfile:
    saved: dict[str, dict[str, Any]] = {}
    for name, values in features.items():
        try:
            bins = fit_fixed_bins(values, n_bins=n_bins)
        except ValueError as exc:
            raise ApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                f"特征 {name!r} 无法建档: {exc}",
            ) from exc
        counts = assign_counts(bins, values)
        saved[name] = {
            "bins": bins,
            "counts": counts.counts,
            "n_total": counts.n_total,
        }
    return BaselineProfile(
        baseline_id=uuid.uuid4().hex,
        features=saved,
    )


def _results_against_profile(
    profile: BaselineProfile,
    current: dict[str, list[float | None]],
    options: dict[str, Any],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, item in profile.features.items():
        bins: FixedBins = item["bins"]
        base = BinCounts(bins=bins, counts=item["counts"])
        cur = assign_counts(bins, current.get(name, []))
        result = compute_drift(
            base,
            cur,
            feature=name,
            smoothing=options["smoothing"],
            alpha=options["alpha"],
            epsilon=options["epsilon"],
            min_sample=options["min_sample"],
        )
        out[name] = result.to_dict()
    return out


def _demo_payload(scenario: str, options: dict[str, Any]) -> dict[str, Any]:
    """生成合成场景并直接计算（用于无数据时的快速自检）。"""
    if scenario == "same":
        baseline, current = synthetic.same_distribution()
    elif scenario == "shifted":
        baseline, current = synthetic.shifted_distribution()
    elif scenario == "scaled":
        baseline, current = synthetic.scaled_distribution()
    elif scenario == "all_missing":
        baseline, current = synthetic.all_missing_current()
    elif scenario == "small_sample":
        baseline, current = synthetic.small_sample(10)
    elif scenario == "extremes":
        b0, c0 = synthetic.same_distribution()
        baseline, current = b0, synthetic.inject_extremes(c0, 0.05, seed=1)
    else:  # pragma: no cover - 入口已校验
        raise ApiError(HTTPStatus.BAD_REQUEST, f"未知场景 {scenario!r}")

    result = monitor_features(
        {"value": baseline.tolist()},
        {"value": current.tolist()},
        n_bins=options["n_bins"],
        smoothing=options["smoothing"],
        alpha=options["alpha"],
        epsilon=options["epsilon"],
        min_sample=options["min_sample"],
    )["value"]
    return {
        "scenario": scenario,
        "disclaimer": (
            "PSI 阈值打标（0.1/0.25）仅为工程经验规则，"
            "不是分布异同的统计证明；小样本场景尤其如此。"
        ),
        "result": result.to_dict(),
    }


def create_handler(store: BaselineStore) -> type[BaseHTTPRequestHandler]:
    """生成绑定了存储的 handler 类。"""

    class DriftHandler(BaseHTTPRequestHandler):
        server_version = "FeatureDrift/1.0"

        # -- 低层工具 -------------------------------------------------
        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError) as exc:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST, "Content-Length 头非法"
                ) from exc
            if length <= 0:
                raise ApiError(HTTPStatus.BAD_REQUEST, "请求体必须是 JSON")
            if length > MAX_BODY_BYTES:
                raise ApiError(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    f"请求体超过 {MAX_BODY_BYTES} 字节上限",
                )
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, f"JSON 解析失败: {exc}") from exc
            return _require_object(body, "请求体")

        def log_message(self, fmt: str, *args: Any) -> None:
            # 访问日志写到 stderr
            import sys

            sys.stderr.write(
                "[%s] %s\n" % (self.log_date_time_string(), fmt % args)
            )

        # -- 路由 -----------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            try:
                if self.path == "/healthz":
                    self._send_json(
                        HTTPStatus.OK,
                        {"status": "ok", "service": "feature-drift"},
                    )
                    return
                if self.path.startswith("/v1/baselines/"):
                    baseline_id = self.path.rsplit("/", 1)[-1]
                    self._send_json(
                        HTTPStatus.OK, store.get(baseline_id).to_summary()
                    )
                    return
                raise ApiError(HTTPStatus.NOT_FOUND, f"未知路径: {self.path}")
            except ApiError as exc:
                self._send_json(exc.status, {"error": exc.message})

        def do_POST(self) -> None:  # noqa: N802
            try:
                if self.path == "/v1/monitor":
                    self._handle_monitor()
                elif self.path == "/v1/baselines":
                    self._handle_create_baseline()
                elif self.path == "/v1/drift":
                    self._handle_drift()
                elif self.path == "/v1/demo":
                    self._handle_demo()
                else:
                    raise ApiError(HTTPStatus.NOT_FOUND, f"未知路径: {self.path}")
            except ApiError as exc:
                self._send_json(exc.status, {"error": exc.message})
            except Exception as exc:  # 防御性兜底：不外泄堆栈
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": f"内部错误: {type(exc).__name__}"},
                )

        # -- 端点实现 -------------------------------------------------
        def _handle_monitor(self) -> None:
            body = self._read_json()
            options = _parse_options(body)
            baseline = _parse_features(body, "baseline")
            current = _parse_features(body, "current")
            if set(baseline) != set(current):
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "monitor 端点要求基线与当前窗口特征集合完全一致",
                )
            results = monitor_features(
                baseline,
                current,
                n_bins=options["n_bins"],
                smoothing=options["smoothing"],
                alpha=options["alpha"],
                epsilon=options["epsilon"],
                min_sample=options["min_sample"],
            )
            self._send_json(
                HTTPStatus.OK,
                {
                    "disclaimer": (
                        "PSI 阈值打标（0.1/0.25）仅为工程经验规则，"
                        "不是分布异同的统计证明。"
                    ),
                    "results": {k: v.to_dict() for k, v in results.items()},
                },
            )

        def _handle_create_baseline(self) -> None:
            body = self._read_json()
            options = _parse_options(body)
            features = _parse_features(body, "features")
            profile = _build_profile(features, options["n_bins"])
            store.save(profile)
            self._send_json(HTTPStatus.CREATED, profile.to_summary())

        def _handle_drift(self) -> None:
            body = self._read_json()
            options = _parse_options(body)
            baseline_id = body.get("baseline_id")
            if not isinstance(baseline_id, str) or not baseline_id:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST, "baseline_id 必须是非空字符串"
                )
            profile = store.get(baseline_id)
            current = _parse_features(body, "current")
            unknown = sorted(set(current) - set(profile.features))
            if unknown:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    f"当前窗口包含基线中不存在的特征: {unknown}",
                )
            results = _results_against_profile(profile, current, options)
            self._send_json(
                HTTPStatus.OK,
                {
                    "baseline_id": baseline_id,
                    "disclaimer": (
                        "PSI 阈值打标（0.1/0.25）仅为工程经验规则，"
                        "不是分布异同的统计证明。"
                    ),
                    "results": results,
                },
            )

        def _handle_demo(self) -> None:
            body = self._read_json()
            options = _parse_options(body)
            scenario = body.get("scenario", "same")
            if scenario not in VALID_DEMOS:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    f"scenario 必须是 {VALID_DEMOS} 之一",
                )
            self._send_json(HTTPStatus.OK, _demo_payload(scenario, options))

    return DriftHandler


def run_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    """启动阻塞式 HTTP 服务（线程池处理并发请求）。"""
    store = BaselineStore()
    handler = create_handler(store)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"特征漂移监测服务已启动: http://{host}:{port}", flush=True)
    print("按 Ctrl+C 停止", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭服务 ...", flush=True)
    finally:
        server.server_close()
