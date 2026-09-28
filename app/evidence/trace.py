"""可解释追踪：把一次请求关联到身份、关键步骤、版本与随机流来源。

输出结构刻意把三类信息分开：
- ``steps``：按顺序的关键处理步骤及处理位置（代码模块）；
- ``failure``：失败原因（稳定类别 + 人话）；
- ``uncertainties``：结构性不确定结论（如尾组未排满、统计未检出≠证明）。

不允许把不确定结论混进成功信息里。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Trace:
    request_id: str
    study_id: str | None = None
    subject_id: str | None = None
    actor_role: str | None = None
    versions: dict[str, str] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    failure: dict[str, Any] | None = None
    uncertainties: list[dict[str, Any]] = field(default_factory=list)
    random_sources: list[dict[str, Any]] = field(default_factory=list)

    def step(self, name: str, *, handler: str, detail: dict | None = None,
             status: str = "ok") -> None:
        self.steps.append({
            "step": name,
            "status": status,
            "handler": handler,
            **({"detail": detail} if detail else {}),
        })

    def fail(self, category: str, message: str, http_status: int,
             detail: dict | None = None) -> None:
        self.failure = {
            "category": category,
            "message": message,
            "http_status": http_status,
            **({"detail": detail} if detail else {}),
        }
        self.steps.append({
            "step": "failure", "status": "error",
            "handler": "app.errors", "category": category,
        })

    def uncertain(self, category: str, message: str,
                  detail: dict | None = None) -> None:
        self.uncertainties.append({
            "category": category,
            "message": message,
            **({"detail": detail} if detail else {}),
        })

    def random_source(self, info: dict[str, Any]) -> None:
        self.random_sources.append(info)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "study_id": self.study_id,
            "subject_id": self.subject_id,
            "actor_role": self.actor_role,
            "versions": self.versions,
            "steps": self.steps,
            "random_sources": self.random_sources,
            "failure": self.failure,
            "uncertainties": self.uncertainties,
        }
