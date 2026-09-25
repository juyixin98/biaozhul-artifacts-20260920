"""本地 HTTP 服务：把 :class:`tlog.log.Log` 暴露为 JSON API（纯后端，无前端）。

仅监听 127.0.0.1，供本机测试使用。所有响应均为 JSON；
成功状态码 200，参数错误 400，方法不允许 405，路由不存在 404。

路由
----
``POST /add``
    请求 JSON：``{"data_b64": "<base64 叶子>"}`` 或
    ``{"data_utf8": "<UTF-8 字符串叶子>"}``
    响应：``{"leaf_index", "tree_size", "leaf_hash_hex"}``

``GET /sth``
    返回签名树头（见 :meth:`tlog.log.Log.get_sth`）。

``GET /get-inclusion-proof?leaf_index=<m>&tree_size=<n 可选>``
    响应：``{"leaf_index", "tree_size", "leaf_hash_hex",
    "inclusion_path": [hex...], "root_hash_hex"}``

``GET /get-consistency-proof?first=<m>``
    响应：``{"first_size", "second_size", "first_root_hash_hex",
    "second_root_hash_hex", "consistency_path": [hex...]}``

``GET /get-leaf?index=<i>``
    响应：``{"index", "data_b64", "data_utf8(若可解码)", "leaf_hash_hex"}``

``GET /health`` -> ``{"status": "ok", "tree_size": n}``
"""

from __future__ import annotations

import json
from base64 import b64decode, b64encode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .hashing import leaf_hash
from .log import CorruptLogError, Log


class Handler(BaseHTTPRequestHandler):
    # 由 make_server 注入到 server 属性上：self.server.log
    server_version = "tlog/1.0"

    # ------------------------------------------------------------ 工具方法

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def _query(self) -> dict[str, str]:
        q = parse_qs(urlparse(self.path).query)
        return {k: v[0] for k, v in q.items()}

    def log_message(self, fmt: str, *args) -> None:
        # 落到默认访问日志格式，便于 RUNLOG 记录。
        super().log_message(fmt, *args)

    # ------------------------------------------------------------ GET 路由

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        log: Log = self.server.log  # type: ignore[attr-defined]
        route = urlparse(self.path).path
        try:
            if route == "/health":
                self._send_json(200, {"status": "ok", "tree_size": log.size})
                return

            if route == "/sth":
                self._send_json(200, log.get_sth())
                return

            if route == "/get-leaf":
                q = self._query()
                if "index" not in q:
                    self._error(400, "缺少查询参数 index")
                    return
                try:
                    index = int(q["index"])
                    data = log.get_leaf(index)
                except (ValueError, IndexError):
                    self._error(400, f"index 非法或越界：{q.get('index')}")
                    return
                payload = {
                    "index": index,
                    "data_b64": b64encode(data).decode("ascii"),
                    "leaf_hash_hex": leaf_hash(data).hex(),
                }
                try:
                    payload["data_utf8"] = data.decode("utf-8")
                except UnicodeDecodeError:
                    pass
                self._send_json(200, payload)
                return

            if route == "/get-inclusion-proof":
                q = self._query()
                if "leaf_index" not in q:
                    self._error(400, "缺少查询参数 leaf_index")
                    return
                try:
                    leaf_index = int(q["leaf_index"])
                    tree_size = int(q["tree_size"]) if q.get("tree_size") else None
                except ValueError:
                    self._error(400, "leaf_index/tree_size 必须是整数")
                    return
                try:
                    lh, proof, root, n = log.inclusion_proof(leaf_index, tree_size)
                except IndexError as exc:
                    self._error(400, str(exc))
                    return
                self._send_json(
                    200,
                    {
                        "leaf_index": leaf_index,
                        "tree_size": n,
                        "leaf_hash_hex": lh.hex(),
                        "root_hash_hex": root.hex(),
                        "inclusion_path": [p.hex() for p in proof],
                    },
                )
                return

            if route == "/get-consistency-proof":
                q = self._query()
                if "first" not in q:
                    self._error(400, "缺少查询参数 first")
                    return
                try:
                    first = int(q["first"])
                except ValueError:
                    self._error(400, "first 必须是整数")
                    return
                try:
                    old_root, new_root, proof, first_n, new_n = (
                        log.consistency_proof(first)
                    )
                except (ValueError, IndexError) as exc:
                    self._error(400, str(exc))
                    return
                self._send_json(
                    200,
                    {
                        "first_size": first_n,
                        "second_size": new_n,
                        "first_root_hash_hex": old_root.hex(),
                        "second_root_hash_hex": new_root.hex(),
                        "consistency_path": [p.hex() for p in proof],
                    },
                )
                return

            self._error(404, f"路由不存在：{route}")
        except CorruptLogError as exc:
            self._error(500, f"日志存储已损坏，拒绝服务：{exc}")

    # ------------------------------------------------------------ POST 路由

    def do_POST(self) -> None:  # noqa: N802
        log: Log = self.server.log  # type: ignore[attr-defined]
        route = urlparse(self.path).path
        if route != "/add":
            self._error(404, f"路由不存在：{route}")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            req = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._error(400, "请求体必须是 UTF-8 JSON")
            return

        if not isinstance(req, dict):
            self._error(400, "请求 JSON 必须是对象")
            return

        if "data_b64" in req:
            try:
                data = b64decode(req["data_b64"], validate=True)
            except (ValueError, TypeError):
                self._error(400, "data_b64 不是合法 base64")
                return
        elif "data_utf8" in req:
            if not isinstance(req["data_utf8"], str):
                self._error(400, "data_utf8 必须是字符串")
                return
            data = req["data_utf8"].encode("utf-8")
        else:
            self._error(400, "必须提供 data_b64 或 data_utf8")
            return

        try:
            index = log.append(data)
        except (TypeError, CorruptLogError) as exc:
            self._error(400 if isinstance(exc, TypeError) else 500, str(exc))
            return

        self._send_json(
            200,
            {
                "leaf_index": index,
                "tree_size": index + 1,
                "leaf_hash_hex": leaf_hash(data).hex(),
            },
        )


def make_server(host: str, port: int, data_dir: str) -> ThreadingHTTPServer:
    """构造并返回一个绑定日志实例的 HTTP 服务（尚未开始 serve）。"""
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.log = Log(data_dir)  # type: ignore[attr-defined]
    return httpd


def serve(host: str = "127.0.0.1", port: int = 8080, data_dir: str = "log_data") -> None:
    """阻塞式启动服务。"""
    httpd = make_server(host, port, data_dir)
    actual_host, actual_port = httpd.server_address[:2]
    print(f"透明日志服务已启动：http://{actual_host}:{actual_port}")
    print(f"数据目录：{data_dir}（叶子只增存储 leaves.jsonl）")
    print("仅监听本地地址，仅供测试；Ctrl+C 停止。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务……")
    finally:
        httpd.server_close()
