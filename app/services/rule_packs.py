"""规则包发布、加载与编译缓存。规则包只追加，永不修改。"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from sqlalchemy import select

from ..config import config
from ..db import session_scope
from ..models import RulePack
from ..rules_engine import CompiledPack, RulePackError, compile_pack_json, pack_content_sha

_cache_lock = threading.Lock()
_compiled_cache: dict[str, CompiledPack] = {}


def publish_pack(content: str) -> tuple[CompiledPack, bool]:
    """校验并落库规则包。返回 (编译包, 是否新建)。内容相同则复用旧版本。"""
    compiled = compile_pack_json(content)
    sha = pack_content_sha(content)
    with session_scope() as sess:
        existing = sess.execute(
            select(RulePack).where(RulePack.content_sha256 == sha)
        ).scalar_one_or_none()
        if existing is not None:
            return _get_compiled(existing.version, existing.content_json), False
        pack = RulePack(
            version=compiled.version,
            content_json=content,
            content_sha256=sha,
        )
        sess.add(pack)
    with _cache_lock:
        _compiled_cache[compiled.version] = compiled
    return compiled, True


def load_pack(version: str) -> CompiledPack:
    cached = _compiled_cache.get(version)
    if cached is not None:
        return cached
    return _get_compiled(version, None)


def _get_compiled(version: str, content_json: str | None) -> CompiledPack:
    with _cache_lock:
        cached = _compiled_cache.get(version)
        if cached is not None:
            return cached
    if content_json is None:
        with session_scope() as sess:
            pack = sess.get(RulePack, version)
            if pack is None:
                raise KeyError(f"规则包不存在: {version}")
            content_json = pack.content_json
    compiled = compile_pack_json(content_json)
    if compiled.version != version:
        raise RulePackError(f"规则包内容哈希与版本不符: {version}")
    with _cache_lock:
        _compiled_cache.setdefault(version, compiled)
    return compiled


def load_builtin_packs() -> list[tuple[CompiledPack, bool]]:
    """从 rules/ 目录载入全部 *.json 内置包（幂等）。"""
    results: list[tuple[CompiledPack, bool]] = []
    rules_dir = Path(config.builtin_rules_dir)
    if not rules_dir.exists():
        return results
    for path in sorted(rules_dir.glob("*.json")):
        content = path.read_text(encoding="utf-8")
        results.append(publish_pack(content))
    return results


def list_packs() -> list[dict]:
    with session_scope() as sess:
        rows = sess.execute(select(RulePack).order_by(RulePack.created_at, RulePack.version)).scalars()
        return [
            {
                "version": r.version,
                "content_sha256": r.content_sha256,
                "created_at": r.created_at.isoformat() + "Z",
                "pack": json.loads(r.content_json),
            }
            for r in rows
        ]
