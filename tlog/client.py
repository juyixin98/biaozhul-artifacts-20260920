"""轻量 HTTP 客户端与**独立验证器**（验证方视角）。

关键设计：客户端验证逻辑只依赖 :mod:`tlog.merkle` 里的 RFC 9162 验证函数
和 :mod:`tlog.keys` 的签名校验，不信任服务端返回的任何布尔结论——
服务端只给出叶子、路径与 STH，由客户端自己重算根并比对。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64decode

from cryptography.hazmat.primitives.serialization import load_pem_public_key

from . import keys as key_module
from . import merkle
from .hashing import leaf_hash


class ClientError(RuntimeError):
    """HTTP 非 200 或响应畸形。"""


class TransparencyLogClient:
    """最小 JSON API 客户端（标准库 urllib，无第三方依赖）。"""

    def __init__(self, base_url: str = "http://127.0.0.1:8080", timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # type: ignore[name-defined]
            detail = exc.read().decode("utf-8", errors="replace")
            raise ClientError(f"HTTP {exc.code}: {detail}") from exc

    # ------------------------------------------------------------- 服务端操作

    def add_leaf_bytes(self, data: bytes) -> dict:
        import base64

        return self._post(
            "/add", {"data_b64": base64.b64encode(data).decode("ascii")}
        )

    def add_leaf_utf8(self, text: str) -> dict:
        return self._post("/add", {"data_utf8": text})

    def get_sth(self) -> dict:
        return self._get("/sth")

    def get_leaf(self, index: int) -> dict:
        return self._get("/get-leaf", {"index": index})

    def get_inclusion_proof(self, leaf_index: int, tree_size: int | None = None) -> dict:
        params = {"leaf_index": leaf_index}
        if tree_size is not None:
            params["tree_size"] = tree_size
        return self._get("/get-inclusion-proof", params)

    def get_consistency_proof(self, first_size: int) -> dict:
        return self._get("/get-consistency-proof", {"first": first_size})

    # ----------------------------------------------------------- 客户端验证

    @staticmethod
    def verify_inclusion_response(
        leaf_data: bytes,
        proof_response: dict,
        expected_root_hash: bytes,
    ) -> bool:
        """用叶子原始字节 + 服务端给的路径，独立重算根并比对。"""
        return merkle.verify_inclusion(
            leaf_hash_value=leaf_hash(leaf_data),
            leaf_index=int(proof_response["leaf_index"]),
            tree_size=int(proof_response["tree_size"]),
            proof=[bytes.fromhex(p) for p in proof_response["inclusion_path"]],
            root_hash=expected_root_hash,
        )

    @staticmethod
    def verify_consistency_response(resp: dict) -> bool:
        """独立验证一致性证明响应中的两根与路径。"""
        return merkle.verify_consistency(
            first_size=int(resp["first_size"]),
            second_size=int(resp["second_size"]),
            proof=[bytes.fromhex(p) for p in resp["consistency_path"]],
            first_hash=bytes.fromhex(resp["first_root_hash_hex"]),
            second_hash=bytes.fromhex(resp["second_root_hash_hex"]),
        )

    @staticmethod
    def verify_sth(sth: dict, public_key_pem: bytes | None = None) -> bool:
        """校验 STH 签名。可显式传入钉住的公钥 PEM；否则用 STH 自带公钥
        （仅能证明自洽，无法防替换，README 中说明密钥带外分发的重要性）。"""
        if public_key_pem is None:
            public_key_pem = b64decode(sth["public_key_b64"])
        public_key = load_pem_public_key(public_key_pem)
        return key_module.verify_sth_signature(
            public_key,
            int(sth["tree_size"]),
            int(sth["timestamp_ms"]),
            bytes.fromhex(sth["sha256_root_hash"]),
            bytes.fromhex(sth["tree_head_signature"]),
        )
