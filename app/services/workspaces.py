"""工作区创建、认证与当前规则/索引指针读取。"""
from __future__ import annotations

import secrets

from sqlalchemy import select

from ..db import session_scope
from ..models import Workspace
from .rule_packs import load_builtin_packs


class AuthError(Exception):
    pass


def create_workspace(name: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("工作区名称不能为空")
    builtin = load_builtin_packs()
    if not builtin:
        raise RuntimeError("没有可用规则包（内置规则缺失且未手工发布）")
    default_version = builtin[0][0].version
    api_key = "wk_" + secrets.token_urlsafe(32)
    with session_scope() as sess:
        ws = Workspace(name=name[:200], api_key=api_key, active_rule_pack_version=default_version)
        sess.add(ws)
        sess.flush()
        return {
            "id": ws.id,
            "name": ws.name,
            "api_key": api_key,
            "active_rule_pack_version": default_version,
            "created_at": ws.created_at.isoformat() + "Z",
        }


def authenticate(api_key: str) -> Workspace:
    if not api_key:
        raise AuthError("缺少 X-Workspace-Key")
    with session_scope() as sess:
        ws = sess.execute(
            select(Workspace).where(Workspace.api_key == api_key)
        ).scalar_one_or_none()
        if ws is None:
            raise AuthError("工作区密钥无效")
        sess.expunge(ws)
        return ws


def get_workspace(workspace_id: int) -> Workspace:
    with session_scope() as sess:
        ws = sess.get(Workspace, workspace_id)
        if ws is None:
            raise AuthError("工作区不存在")
        sess.expunge(ws)
        return ws
