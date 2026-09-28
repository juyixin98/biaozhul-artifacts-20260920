"""鉴权依赖与权限隔离。

角色（本地合成令牌）：
- ``investigator``：创建研究、登记、查自己可见的分配臂；
- ``auditor``：只读证据接口（均衡、流复核、审计事件、分布诊断），
  **看不到未来分配次序**（开放区组置换不落盘，审计接口也不返回），
  且不能登记/创建研究；
- ``admin``：显式开组、封尾组、封研究、管理性操作。

权限隔离的关键不是"不同 UI"，而是每个路由强制角色断言：auditor 调登记
→ 403 FORBIDDEN_ROLE；任何人都无法通过 HTTP 读取种子明文或未消费的
未来次序。
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, Request

from ..errors import AppError, ErrorCategory


@dataclass(frozen=True)
class Principal:
    token: str
    role: str


def authenticate(request: Request,
                 authorization: str | None = Header(default=None)) -> Principal:
    if not authorization:
        raise AppError(
            ErrorCategory.MISSING_CREDENTIALS, 401,
            "缺少 Authorization 头；本地合成令牌格式为 "
            "'Authorization: Bearer synt-<role>-token'",
        )
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise AppError(
            ErrorCategory.INVALID_TOKEN, 401,
            "Authorization 头格式应为 'Bearer <token>'",
        )
    token = parts[1].strip()
    role = request.app.state.settings.tokens.get(token)
    if role is None:
        raise AppError(
            ErrorCategory.INVALID_TOKEN, 401, "令牌无效或已撤销"
        )
    return Principal(token=token, role=role)
