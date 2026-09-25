"""崩溃测试用子进程 worker。

用法: python -m tests._crash_worker <data_dir> <iterations> <out_file>

每轮执行：rotate -> encrypt -> 重新解析信封并（尝试）解密，
成功生成的 (ciphertext_b64, version_id, plaintext) 以 JSONL 追加到 out_file。
记录文件用 O_APPEND 单次 write 追加（单次 write 对管道/常规文件在本内核上
对 <= PIPE_BUF/常规文件写为原子可见）；即使出现半行，父进程也按 JSON
逐行解析并跳过坏行。随时可能被 SIGKILL，不做任何清理。
"""

from __future__ import annotations

import json
import os
import sys

from keyversion.service import KeyService
from keyversion.store import KeyStore


def append_record(path: str, record: dict) -> None:
    line = json.dumps(record, separators=(",", ":")).encode() + b"\n"
    # O_APPEND 单次写：若被杀死，最坏只丢末尾半行
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def main() -> int:
    data_dir, iterations_s, out_file = sys.argv[1], sys.argv[2], sys.argv[3]
    iterations = int(iterations_s)
    store = KeyStore(data_dir).open()
    svc = KeyService(store)
    # 确保至少有一个 active 版本
    if svc.active_version() is None:
        svc.rotate()

    for i in range(iterations):
        svc.rotate()
        plaintext = f"crash-record-{os.getpid()}-{i}".encode()
        envelope, info = svc.encrypt(plaintext)
        # 立刻解密自检，确认刚写入的记录可解
        decoded, used = svc.decrypt(envelope)
        assert decoded == plaintext and used == info.version_id
        append_record(
            out_file,
            {
                "ciphertext": KeyService.b64e(envelope),
                "version_id": info.version_id,
                "plaintext": plaintext.decode(),
                "iter": i,
            },
        )
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
