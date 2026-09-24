"""真实密码学操作：SHA-256 内容指纹 + HMAC-SHA256 防篡改签名。

签名覆盖规范化后的标定结果载荷（canonical JSON），不含签名字段本身。
密钥来自环境变量 CALIB_HMAC_SECRET；否则在存储目录生成仅当前用户可读的
随机密钥文件（os.urandom，chmod 0600），服务重启后仍可校验旧版本。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any

from .config import settings

_SECRET_CACHE: bytes | None = None
_SECRET_FILE_NAME = ".hmac_secret"


def sha256_bytes(data: bytes) -> str:
    """对原始字节计算 SHA-256 十六进制摘要（真实哈希，非占位）。"""
    return hashlib.sha256(data).hexdigest()


def _load_or_create_secret(storage_dir: str) -> bytes:
    global _SECRET_CACHE
    if _SECRET_CACHE is not None:
        return _SECRET_CACHE

    env_secret = os.getenv(settings.hmac_secret_env)
    if env_secret:
        _SECRET_CACHE = env_secret.encode("utf-8")
        return _SECRET_CACHE

    secret_path = Path(storage_dir) / _SECRET_FILE_NAME
    if secret_path.exists():
        _SECRET_CACHE = secret_path.read_bytes().strip()
    else:
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        # 32 字节随机密钥
        generated = secrets.token_bytes(32).hex().encode("ascii")
        # 先写临时文件再限制权限后原子改名，避免窗口内被其他用户读到
        tmp_path = secret_path.with_suffix(".tmp")
        tmp_path.write_bytes(generated)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, secret_path)
        os.chmod(secret_path, 0o600)
        _SECRET_CACHE = generated
    return _SECRET_CACHE


def canonical_payload(obj: Any) -> bytes:
    """生成确定性 JSON：键排序、无空白、无 ASCII 转义差异，作为签名/哈希输入。"""
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=_json_default
    ).encode("utf-8")


def _json_default(o: Any) -> Any:
    # numpy 标量兜底（正常路径已在序列化前转换为原生类型）
    return o.item() if hasattr(o, "item") else str(o)


def payload_hash(obj: Any) -> str:
    return sha256_bytes(canonical_payload(obj))


def sign_payload(payload: Any, storage_dir: str | None = None) -> str:
    """对标定结果载荷做 HMAC-SHA256，返回十六进制签名。"""
    key = _load_or_create_secret(storage_dir or settings.storage_dir)
    return hmac.new(key, canonical_payload(payload), hashlib.sha256).hexdigest()


def verify_payload(payload: Any, signature: str, storage_dir: str | None = None) -> bool:
    """常量时间比较，校验载荷签名。"""
    expected = sign_payload(payload, storage_dir or settings.storage_dir)
    return hmac.compare_digest(expected, signature or "")
