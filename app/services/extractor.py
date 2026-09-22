"""抽取执行与实体持久化（幂等）。"""
from __future__ import annotations

from collections import defaultdict

from ..db import immediate_transaction
from ..rules_engine import CompiledPack, RawMention, extract as run_extract
from ..tokenizer import tokenize
from . import rule_packs as pack_service


def extract_text(text: str, pack: CompiledPack) -> list[RawMention]:
    """纯函数：跑规则。测试与离线演示也用它。"""
    return run_extract(text, pack)


def get_fresh_blob(
    conn, *, workspace_id: int, document_id: int, expected_sha: str
) -> tuple[bool, str | None]:
    """在 IMMEDIATE 事务中校验作业仍新鲜，返回内容。

    返回 (ok, content)：
    - 文档已删除 / 不存在 → (False, None)
    - 文档内容哈希已变（新版本）→ (False, None)（旧作业迟到结果丢弃）
    """
    row = conn.execute(
        "SELECT id, doc_sha256, blob_sha256, deleted_at FROM documents "
        "WHERE workspace_id=? AND id=?",
        (workspace_id, document_id),
    ).fetchone()
    if row is None or row["deleted_at"] is not None or row["doc_sha256"] != expected_sha:
        return False, None
    blob = conn.execute(
        "SELECT content FROM blobs WHERE sha256=?", (row["blob_sha256"],)
    ).fetchone()
    if blob is None:  # 理论上不会发生（引用计数保护）
        return False, None
    return True, blob["content"]


def persist_entities(
    conn,
    *,
    workspace_id: int,
    document_id: int,
    rule_pack_version: str,
    mentions: list[RawMention],
) -> int:
    """在已有 IMMEDIATE 连接上写实体/提及。全部 upsert + 唯一约束，重复执行幂等。

    返回规范实体数。
    """
    # 1) 实体行（同文档/版本/类型/规范名唯一）
    grouped: dict[tuple[str, str], RawMention] = {}
    mentions_by_key: dict[tuple[str, str], list[RawMention]] = defaultdict(list)
    for m in mentions:
        key = (m.entity_type, m.canonical)
        grouped.setdefault(key, m)
        mentions_by_key[key].append(m)

    entity_ids: dict[tuple[str, str], int] = {}
    for (etype, canonical), first in grouped.items():
        row = conn.execute(
            "SELECT id FROM entities WHERE document_id=? AND rule_pack_version=? "
            "AND entity_type=? AND canonical=?",
            (document_id, rule_pack_version, etype, canonical),
        ).fetchone()
        if row is not None:
            entity_ids[(etype, canonical)] = int(row["id"])
            continue
        cur = conn.execute(
            "INSERT INTO entities(workspace_id, document_id, rule_pack_version, "
            "entity_type, canonical, first_seen_rule_id, created_at) "
            "VALUES (?,?,?,?,?,?,datetime('now'))",
            (workspace_id, document_id, rule_pack_version, etype, canonical, first.rule_id),
        )
        entity_ids[(etype, canonical)] = int(cur.lastrowid)

    # 2) 提及行（同实体同位置同原文唯一）；原始提及一个不丢
    for m in mentions:
        rid = entity_ids[(m.entity_type, m.canonical)]
        conn.execute(
            "INSERT INTO mentions(entity_id, alias, matched_text, char_start, char_end, "
            "rule_id, created_at) VALUES (?,?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(entity_id, char_start, char_end, matched_text) DO UPDATE SET "
            "alias=alias",
            (rid, m.alias, m.matched_text, m.start, m.end, m.rule_id),
        )
    return len(grouped)


def extract_job(document_id: int, workspace_id: int, rule_pack_version: str, doc_sha256: str) -> dict:
    """抽取作业的完整事务：校验新鲜 → 读内容 → 跑规则 → 落实体 → 写索引。

    索引目标：
    - 若存在同规则版本的 building 代，写入该代（重建期间上传的新文档由此不漏）；
    - 同时若存在 active 代，也写入 active 代并做增量权重收敛（立即可搜）；
    - 全部 upsert，重复执行幂等。
    返回统计。
    """
    pack = pack_service.load_pack(rule_pack_version)
    from . import indexing

    with immediate_transaction() as conn:
        ok, content = get_fresh_blob(
            conn, workspace_id=workspace_id, document_id=document_id, expected_sha=doc_sha256
        )
        if not ok:
            return {"skipped": "stale_or_deleted"}
        mentions = run_extract(content, pack)
        n_entities = persist_entities(
            conn,
            workspace_id=workspace_id,
            document_id=document_id,
            rule_pack_version=rule_pack_version,
            mentions=mentions,
        )

        tokens = tokenize(content)
        indexed_gens: list[int] = []

        building = conn.execute(
            "SELECT id FROM index_generations WHERE workspace_id=? "
            "AND rule_pack_version=? AND status='building' ORDER BY id",
            (workspace_id, rule_pack_version),
        ).fetchall()
        for g in building:
            indexing._upsert_postings(conn, int(g["id"]), document_id, tokens)
            indexed_gens.append(int(g["id"]))

        ws = conn.execute(
            "SELECT active_index_generation_id FROM workspaces WHERE id=?", (workspace_id,)
        ).fetchone()
        active_id = ws["active_index_generation_id"] if ws else None
        if active_id is not None and int(active_id) not in indexed_gens:
            gen = conn.execute(
                "SELECT status, rule_pack_version FROM index_generations WHERE id=?",
                (int(active_id),),
            ).fetchone()
            # 只在 active 代与作业规则版本一致时写入：版本切换后、新代切换前，
            # 不把新文档污染进旧代（查询必须始终看到同一代完整索引）。
            if gen is not None and gen["status"] == "active" and gen["rule_pack_version"] == rule_pack_version:
                indexing._upsert_postings(conn, int(active_id), document_id, tokens)
                indexing._recompute_weights(conn, int(active_id), [document_id])
                indexed_gens.append(int(active_id))

        return {
            "mentions": len(mentions),
            "entities": n_entities,
            "doc_sha256": doc_sha256,
            "indexed_generations": indexed_gens,
        }
