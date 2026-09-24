"""内容寻址证据库。

所有证据 (bag 原始字节、产物、错误记录) 都以其 SHA-256 哈希为路径存放:
evidence/<前2字符>/<完整哈希>。

- 写入是幂等的: 相同内容解析到同一路径, 天然去重, 重复执行不会覆盖证据。
- 不同内容必然得到不同路径: 证据永远不会被另一次尝试覆盖。
- 发现路径上的字节与哈希不符 (被外部替换) 时, 用正确内容修复并返回
  repaired=True —— 因为身份由内容哈希定义, 同一路径只允许存放对应内容。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .crypto import sha256_bytes, sha256_file


@dataclass
class StoredEvidence:
    sha256: str
    path: Path
    size: int
    reused: bool
    repaired: bool


class EvidenceStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, digest: str) -> Path:
        return self.root / digest[:2] / digest

    def put(self, data: bytes, digest: str | None = None) -> StoredEvidence:
        actual = sha256_bytes(data)
        if digest is not None and actual != digest:
            raise ValueError(f"内容哈希不匹配: 期望 {digest}, 实际 {actual}")
        path = self.path_for(actual)
        reused = path.exists()
        repaired = False
        if reused:
            if sha256_file(path) != actual:
                # 文件被替换成了别的内容: 按内容寻址身份恢复正确字节。
                path.write_bytes(data)
                repaired = True
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return StoredEvidence(actual, path, len(data), reused, repaired)

    def get(self, digest: str) -> bytes:
        path = self.path_for(digest)
        if not path.exists():
            raise FileNotFoundError(digest)
        data = path.read_bytes()
        if sha256_bytes(data) != digest:
            raise ValueError(f"证据内容与标识不符, 可能被篡改: {digest}")
        return data

    def has(self, digest: str) -> bool:
        path = self.path_for(digest)
        return path.exists() and sha256_file(path) == digest
