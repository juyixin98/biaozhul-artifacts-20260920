"""状态存储：内存态 + JSON 文件持久化（原子写、线程锁）。

数据全部为十六进制字符串等可 JSON 序列化的简单类型，方便直接查看 ``state.json``。
这是教学级实现：适用于单实例服务，不做并发多实例协调。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from copy import deepcopy
from typing import Any


def empty_state() -> dict[str, Any]:
    """返回一份全新的空状态。"""

    return {
        # 当前信任根；未初始化时为 None
        "root": None,
        # 已登记制品：key = f"{artifact_type}@{version}"
        "artifacts": {},
        # 每个制品类型已见的最高版本，用于回退检测
        "highest_versions": {},
        # 已消费的 nonce 集合（值恒为 1，仅用于 O(1) 查重）
        "used_nonces": {},
        # 历代根归档（审计用），最新的在末尾
        "root_history": [],
    }


class Store:
    """带 RLock 的 JSON 存储；所有修改在锁内完成并落盘。"""

    def __init__(self, path: str | None = None) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._state = empty_state()
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            # 合并而非盲取，避免旧文件缺字段时 KeyError
            state = empty_state()
            state.update(loaded)
            self._state = state

    @property
    def path(self) -> str | None:
        return self._path

    def snapshot(self) -> dict[str, Any]:
        """返回状态的深拷贝（供只读业务逻辑使用）。"""

        with self._lock:
            return deepcopy(self._state)

    def mutate(self, fn):
        """在锁内执行 ``fn(state)``，fn 返回值即本方法返回值；落盘在锁内完成。"""

        with self._lock:
            result = fn(self._state)
            self._persist_locked()
            return result

    def _persist_locked(self) -> None:
        if not self._path:
            return
        directory = os.path.dirname(os.path.abspath(self._path))
        os.makedirs(directory, exist_ok=True)
        # 原子替换：同目录临时文件 + os.replace
        fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._state, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
