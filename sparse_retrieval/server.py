"""本地 HTTP 服务: 稀疏向量入库与余弦 TopK 检索。

仅使用 Python 标准库(http.server)实现, 默认启动时用合成数据建库。

接口
----
- ``GET  /health``             健康检查与索引概况
- ``GET  /stats``              索引与倒排链统计
- ``POST /search``             余弦 TopK 检索(返回候选数等剪枝统计)
- ``POST /index/rebuild``      用显式文档或合成数据重建索引
- ``GET  /``                   接口说明

请求向量支持两种形态::

    {"dim": 1000, "entries": [{"index": 3, "value": -1.5}]}
    {"dim": 1000, "indices": [3, 7], "values": [-1.5, 2.0]}

所有错误返回 HTTP 400 与 ``{"error": "..."}``, 不泄露堆栈。
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import numpy as np

from .data import generate_dataset
from .index import InvertedIndex
from .vector import SparseVector, SparseVectorError, parse_entries

logger = logging.getLogger("sparse_retrieval.server")

_MAX_BODY_BYTES = 8 * 1024 * 1024


class IndexStore:
    """持锁保护的内存索引单例(重建与检索互斥)。"""

    def __init__(self, index: InvertedIndex, build_info: dict[str, Any]) -> None:
        self._lock = threading.Lock()
        self._index = index
        self._build_info = build_info

    @property
    def index(self) -> InvertedIndex:
        return self._index

    def rebuild(self, index: InvertedIndex, build_info: dict[str, Any]) -> None:
        with self._lock:
            self._index = index
            self._build_info = build_info

    def build_info(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._build_info)


def build_synthetic_store(
    seed: int, n_docs: int, dim: int, **kwargs: Any
) -> tuple[InvertedIndex, dict[str, Any]]:
    dataset = generate_dataset(
        seed=seed, n_docs=n_docs, dim=dim, **kwargs
    )
    index = InvertedIndex(dataset.docs)
    info = {
        "source": "synthetic",
        "seed": seed,
        "n_docs": n_docs,
        "dim": dim,
        "params": {k: v for k, v in kwargs.items() if v is not None},
    }
    return index, info


def _vector_from_payload(payload: dict[str, Any], default_dim: int) -> SparseVector:
    indices, values, dim = parse_entries(payload, default_dim=default_dim)
    return SparseVector.create(indices, values, dim)


def rebuild_from_docs(payload: dict[str, Any]) -> tuple[InvertedIndex, dict[str, Any]]:
    if "dim" not in payload or not isinstance(payload["dim"], int):
        raise SparseVectorError("重建索引需要正整数 dim")
    dim = int(payload["dim"])
    raw_docs = payload.get("docs")
    if not isinstance(raw_docs, list) or not raw_docs:
        raise SparseVectorError("docs 必须是非空数组")
    docs: list[SparseVector] = []
    for i, raw in enumerate(raw_docs):
        if not isinstance(raw, dict):
            raise SparseVectorError(f"docs[{i}] 必须是对象")
        try:
            docs.append(_vector_from_payload(raw, dim))
        except SparseVectorError as exc:
            raise SparseVectorError(f"docs[{i}]: {exc}") from exc
    index = InvertedIndex(docs)
    return index, {"source": "explicit", "dim": dim, "n_docs": len(docs)}


class _Handler(BaseHTTPRequestHandler):
    store: IndexStore  # 由 make_server 注入到类属性

    server_version = "SparseRetrieval/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    # -- 基础工具 ------------------------------------------------------ #
    def _send_json(self, status: int, obj: dict[str, Any]) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            raise SparseVectorError("请求体不能为空")
        if length > _MAX_BODY_BYTES:
            raise SparseVectorError("请求体过大(上限 8 MiB)")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SparseVectorError(f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise SparseVectorError("请求体必须是 JSON 对象")
        return payload

    # -- 路由 ---------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802 (stdlib 命名)
        if self.path in ("/health", "/healthz"):
            idx = self.store.index
            self._send_json(
                200,
                {"status": "ok", "dim": idx.dim, "n_docs": idx.n_docs},
            )
        elif self.path == "/stats":
            idx = self.store.index
            chain_lens = [len(d) for d, _ in idx.postings.values()]
            self._send_json(
                200,
                {
                    "build": self.store.build_info(),
                    "dim": idx.dim,
                    "n_docs": idx.n_docs,
                    "n_terms": len(idx.postings),
                    "posting_length_min": min(chain_lens),
                    "posting_length_max": max(chain_lens),
                    "posting_length_mean": float(np.mean(chain_lens)),
                },
            )
        elif self.path == "/":
            self._send_json(200, _API_HELP)
        else:
            self._send_json(404, {"error": f"未知路径: {self.path}"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            payload = self._read_json()
            if self.path == "/search":
                self._handle_search(payload)
            elif self.path == "/index/rebuild":
                self._handle_rebuild(payload)
            else:
                self._send_json(404, {"error": f"未知路径: {self.path}"})
        except (SparseVectorError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception:  # pragma: no cover - 兜底, 不外泄堆栈
            logger.exception("未预期的服务端错误")
            self._send_json(500, {"error": "服务器内部错误"})

    # -- 业务处理 ------------------------------------------------------ #
    def _handle_search(self, payload: dict[str, Any]) -> None:
        k_raw = payload.get("k", 10)
        if not isinstance(k_raw, int) or isinstance(k_raw, bool) or k_raw <= 0:
            raise SparseVectorError("k 必须是正整数")
        query_payload = payload.get("query", payload)
        if not isinstance(query_payload, dict):
            raise SparseVectorError("query 必须是对象")
        idx = self.store.index
        query = _vector_from_payload(query_payload, idx.dim)
        result = idx.search(query, k=k_raw)
        self._send_json(
            200,
            {
                "query_dim": query.dim,
                "query_nnz": query.nnz,
                "k": k_raw,
                "hits": [
                    {"doc_id": h.doc_id, "score": h.score} for h in result.hits
                ],
                "candidates": result.stats_dict(),
            },
        )

    def _handle_rebuild(self, payload: dict[str, Any]) -> None:
        if "synthetic" in payload:
            synth = payload["synthetic"]
            if not isinstance(synth, dict):
                raise SparseVectorError("synthetic 必须是对象")
            seed = int(synth.get("seed", 42))
            n_docs = int(synth.get("n_docs", 2000))
            dim = int(synth.get("dim", 1000))
            extra = {
                key: synth[key]
                for key in (
                    "n_queries",
                    "n_topics",
                    "topic_terms",
                    "doc_avg_nnz",
                    "query_nnz",
                    "negative_prob",
                    "duplicate_prob",
                    "n_zero_docs",
                )
                if key in synth
            }
            index, info = build_synthetic_store(seed, n_docs, dim, **extra)
        else:
            index, info = rebuild_from_docs(payload)
        self.store.rebuild(index, info)
        self._send_json(
            200,
            {"status": "rebuilt", "dim": index.dim, "n_docs": index.n_docs},
        )


_API_HELP = {
    "service": "sparse-vector-cosine-retrieval",
    "endpoints": {
        "GET /health": "健康检查",
        "GET /stats": "索引统计",
        "POST /search": '{"query": <vector>, "k": 10}',
        "POST /index/rebuild": '{"synthetic": {"seed": 42, "n_docs": 2000}} 或 {"dim":..,"docs":[<vector>...]}',
    },
    "vector_formats": [
        '{"dim": 1000, "entries": [{"index": 3, "value": -1.5}]}',
        '{"dim": 1000, "indices": [3, 7], "values": [-1.5, 2.0]}',
    ],
    "semantics": {
        "duplicate_indices": "先求和合并, 抵消为 0 的维度删除",
        "zero_vector": "余弦相似度定义为 0.0",
        "ties": "分数并列时按 doc_id 升序",
        "pruning": "WAND 只跳过可证明进不了 TopK 的文档, 结果与全扫描一致",
    },
}


def make_server(
    host: str,
    port: int,
    store: IndexStore,
) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"store": store})
    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="稀疏向量余弦检索服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--n-docs", type=int, default=2000)
    parser.add_argument("--dim", type=int, default=1000)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    index, info = build_synthetic_store(args.seed, args.n_docs, args.dim)
    store = IndexStore(index, info)
    httpd = make_server(args.host, args.port, store)
    logger.info("服务启动: http://%s:%s (docs=%d, dim=%d)", args.host, args.port,
                index.n_docs, index.dim)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到中断, 关闭服务")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
