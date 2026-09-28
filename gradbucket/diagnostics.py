"""结构化诊断模块。

职责：
- 每一次归约/提交/封存判定都产出一条 :class:`Diagnostic`，带：
  * ``request_id``：请求/记录标识（调用方传入或自动生成）；
  * ``verdict``：ACCEPTED / REJECTED_* / INDETERMINATE_*；
  * 关键状态：轮次、桶、世代、参与样本数、缺失工作者等；
  * 人可读理由（为什么接受、为什么拒绝、为什么无法判定）。
- 敏感数据（梯度张量内容）**绝不进入日志**；只记录形状、范数、样本数等
  脱敏摘要。
- :class:`DiagnosticLog` 是一个带锁的内存记录器，供多进程/多线程安全追加，
  也可把全部记录导出为 JSON 行。
"""

from __future__ import annotations

import json
import math
import threading
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np


class Verdict(str, Enum):
    ACCEPTED = "ACCEPTED"                          # 接受：计入归约
    ACCEPTED_PLACEHOLDER = "ACCEPTED_PLACEHOLDER"  # 接受为显式占位（mask=False）
    REJECTED_STALE_ROUND = "REJECTED_STALE_ROUND"          # 轮次/世代不符
    REJECTED_BUCKET_SHAPE = "REJECTED_BUCKET_SHAPE"        # 桶向量长度/槽位不符
    REJECTED_PARTIAL_SLOT_MASK = "REJECTED_PARTIAL_SLOT_MASK"  # 半截遮罩
    REJECTED_NON_FINITE = "REJECTED_NON_FINITE"            # 梯度含 NaN/inf
    REJECTED_DUPLICATE = "REJECTED_DUPLICATE"              # 同一槽位重复提交
    REJECTED_SAMPLE_COUNT = "REJECTED_SAMPLE_COUNT"        # 样本数非法
    REJECTED_WORKER_LOST = "REJECTED_WORKER_LOST"          # 心跳超时且缺桶
    REJECTED_NO_EVIDENCE = "REJECTED_NO_EVIDENCE"          # 必需槽零证据且无法补提
    REJECTED_SEALED = "REJECTED_SEALED"                    # 轮已封存
    INDETERMINATE_PENDING = "INDETERMINATE_PENDING"        # 缺桶但仍可能到达


def new_request_id(prefix: str = "req") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def tensor_fingerprint(vec: np.ndarray | None) -> dict[str, Any]:
    """张量的脱敏摘要：形状、有限性、范数——不含任何原始数值。"""
    if vec is None:
        return {"present": False}
    arr = np.asarray(vec, dtype=np.float64)
    finite = bool(np.all(np.isfinite(arr)))
    norm = float(np.linalg.norm(arr)) if arr.size and finite else float("nan")
    return {
        "present": True,
        "shape": list(arr.shape),
        "finite": finite,
        "l2_norm": None if math.isnan(norm) else norm,
    }


@dataclass
class Diagnostic:
    request_id: str
    verdict: Verdict
    reason: str
    round_index: int | None = None
    bucket_index: int | None = None
    generation: int | None = None
    worker_id: str | None = None
    key_state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["verdict"] = self.verdict.value
        return d

    def to_jsonl(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


class DiagnosticLog:
    """线程安全的内存诊断记录器。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[Diagnostic] = []

    def record(self, diag: Diagnostic) -> Diagnostic:
        with self._lock:
            self._items.append(diag)
        return diag

    def all(self) -> list[Diagnostic]:
        with self._lock:
            return list(self._items)

    def for_request(self, request_id: str) -> list[Diagnostic]:
        with self._lock:
            return [d for d in self._items if d.request_id == request_id]

    def verdicts(self) -> dict[str, int]:
        with self._lock:
            counts: dict[str, int] = {}
            for d in self._items:
                counts[d.verdict.value] = counts.get(d.verdict.value, 0) + 1
            return counts

    def dump_jsonl(self, path: str | Path) -> None:
        with self._lock:
            lines = [d.to_jsonl() for d in self._items]
        Path(path).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
