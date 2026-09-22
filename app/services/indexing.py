"""TF-IDF 倒排索引：代次生命周期、文档写入、finalize、搜索。

并发模型：所有写路径走 IMMEDIATE 事务（SQLite 写串行）。查询只读
``workspaces.active_index_generation_id`` 指向的那一代——切换在单事务内
更新指针 + 旧代置 superseded，读者永远看到完整的同一代，绝不混读。
"""
from __future__ import annotations

import math
from collections import Counter

from sqlalchemy import select, text

from ..db import immediate_transaction, session_scope
from ..models import IndexGeneration, Workspace, utcnow
from ..tokenizer import tokenize


class RebuildInProgress(Exception):
    pass


# --------------------------------------------------------------------------- #
# 代次生命周期
# --------------------------------------------------------------------------- #

def create_generation(workspace_id: int, rule_pack_version: str) -> int:
    """在 IMMEDIATE 事务中创建 building 代次；若已有 building 代则复用并报错由上层决定。"""
    with immediate_transaction() as conn:
        row = conn.execute(
            "SELECT id FROM index_generations WHERE workspace_id=? AND status='building'",
            (workspace_id,),
        ).fetchone()
        if row is not None:
            raise RebuildInProgress(f"工作区 {workspace_id} 已有 building 代 {row['id']}")
        cur = conn.execute(
            "INSERT INTO index_generations(workspace_id, rule_pack_version, status, created_at) "
            "VALUES (?,?, 'building', ?)",
            (workspace_id, rule_pack_version, utcnow()),
        )
        return int(cur.lastrowid)


def get_active_generation_id(workspace_id: int) -> int | None:
    with session_scope() as sess:
        ws = sess.get(Workspace, workspace_id)
        return ws.active_index_generation_id if ws else None


# --------------------------------------------------------------------------- #
# 文档写入
# --------------------------------------------------------------------------- #

def _upsert_postings(conn, generation_id: int, document_id: int, tokens) -> None:
    # postings 去重：同词同位置只保留一行（重复执行幂等）
    seen: set[tuple[str, int]] = set()
    for tok in tokens:
        key = (tok.term, tok.start)
        if key in seen:
            continue
        seen.add(key)
        conn.execute(
            "INSERT INTO postings(generation_id, document_id, term, char_start, char_end) "
            "VALUES (?,?,?,?,?) ON CONFLICT(generation_id, document_id, term, char_start) "
            "DO UPDATE SET char_end=excluded.char_end",
            (generation_id, document_id, tok.term, tok.start, tok.end),
        )
    counts = Counter(t.term for t in tokens)
    for term, tf in counts.items():
        conn.execute(
            "INSERT INTO doc_term_stats(generation_id, document_id, term, tf, weight) "
            "VALUES (?,?,?,?,0.0) ON CONFLICT(generation_id, document_id, term) "
            "DO UPDATE SET tf=excluded.tf",
            (generation_id, document_id, term, tf),
        )


def index_document(
    *, workspace_id: int, document_id: int, content: str, generation_id: int | None = None
) -> int:
    """把一篇文档写进指定代（或当前 active 代）。返回代次 ID；无 active 代则跳过。"""
    tokens = tokenize(content)
    with immediate_transaction() as conn:
        gen_id = generation_id
        if gen_id is None:
            ws = conn.execute(
                "SELECT active_index_generation_id FROM workspaces WHERE id=?",
                (workspace_id,),
            ).fetchone()
            gen_id = ws["active_index_generation_id"] if ws else None
            if gen_id is None:
                return -1
        gen = conn.execute(
            "SELECT status FROM index_generations WHERE id=? AND workspace_id=?",
            (gen_id, workspace_id),
        ).fetchone()
        if gen is None:
            raise ValueError("索引代次不存在或跨工作区")
        if gen["status"] not in ("active", "building"):
            raise ValueError(f"索引代次状态为 {gen['status']}，不可写入")
        _upsert_postings(conn, gen_id, document_id, tokens)
        # active 代是增量路径：立刻重算受影响文档权重与相关词 df，保证可搜。
        if gen["status"] == "active":
            _finalize_docs(conn, gen_id, [document_id])
        return int(gen_id)


def _finalize_docs(conn, generation_id: int, doc_ids: list[int]) -> None:
    """增量写入后重算受影响文档的全代 df 与归一化权重。"""
    _recompute_weights(conn, generation_id, doc_ids)


def _recompute_weights(conn, generation_id: int, doc_ids: list[int]) -> None:
    """重算权重。

    对给定文档涉及的每个词，按全代统计重算 df；IDF 受 N/df 影响，因此这些词
    出现过的**所有**文档权重都要刷新（新文档入库会改变共享词的 IDF）。
    """
    if not doc_ids:
        return
    placeholders = ",".join("?" for _ in doc_ids)
    n_row = conn.execute(
        "SELECT COUNT(*) AS c FROM ("
        "SELECT DISTINCT document_id FROM doc_term_stats WHERE generation_id=?)",
        (generation_id,),
    ).fetchone()
    n_total = max(int(n_row["c"]), 1)

    rows = conn.execute(
        f"SELECT DISTINCT term FROM doc_term_stats WHERE generation_id=? "
        f"AND document_id IN ({placeholders})",
        (generation_id, *doc_ids),
    ).fetchall()
    terms = sorted({r["term"] for r in rows})
    df_by_term: dict[str, int] = {}
    for term in terms:
        df_row = conn.execute(
            "SELECT COUNT(DISTINCT document_id) AS c FROM doc_term_stats "
            "WHERE generation_id=? AND term=?",
            (generation_id, term),
        ).fetchone()
        df_by_term[term] = int(df_row["c"])
        conn.execute(
            "INSERT INTO index_df(generation_id, term, df) VALUES (?,?,?) "
            "ON CONFLICT(generation_id, term) DO UPDATE SET df=excluded.df",
            (generation_id, term, int(df_row["c"])),
        )

    # 受影响文档 = 含任一这些词的全部文档（IDF 变了，它们的归一化权重都要刷新）
    if terms:
        tph = ",".join("?" for _ in terms)
        affected_rows = conn.execute(
            f"SELECT DISTINCT document_id FROM doc_term_stats "
            f"WHERE generation_id=? AND term IN ({tph})",
            (generation_id, *terms),
        ).fetchall()
    else:
        affected_rows = []
    affected_docs = sorted({int(r["document_id"]) for r in affected_rows})
    if not affected_docs:
        return
    aff_ph = ",".join("?" for _ in affected_docs)
    doc_rows = conn.execute(
        f"SELECT document_id, term, tf FROM doc_term_stats WHERE generation_id=? "
        f"AND document_id IN ({aff_ph})",
        (generation_id, *affected_docs),
    ).fetchall()
    groups: dict[int, list[tuple[str, float]]] = {}
    for r in doc_rows:
        tf = int(r["tf"])
        sublinear = 1.0 + math.log(tf) if tf > 0 else 0.0
        # 对不在本次词集合的词，沿用其已存 df（index_df 里有）
        if r["term"] in df_by_term:
            df = df_by_term[r["term"]]
        else:
            drow = conn.execute(
                "SELECT df FROM index_df WHERE generation_id=? AND term=?",
                (generation_id, r["term"]),
            ).fetchone()
            df = int(drow["df"]) if drow else 0
        idf = math.log((n_total + 1) / (df + 1)) + 1.0
        groups.setdefault(r["document_id"], []).append((r["term"], sublinear * idf))
    for doc_id, weighted in groups.items():
        norm = math.sqrt(sum(w * w for _, w in weighted)) or 1.0
        for term, w in weighted:
            conn.execute(
                "UPDATE doc_term_stats SET weight=? WHERE generation_id=? AND document_id=? AND term=?",
                (w / norm, generation_id, doc_id, term),
            )


def finalize_generation(conn, generation_id: int, doc_count: int) -> None:
    """全代终态：重算所有文档权重（df 全量），更新 doc_count。"""
    ids = [
        int(r["document_id"])
        for r in conn.execute(
            "SELECT DISTINCT document_id FROM doc_term_stats WHERE generation_id=?",
            (generation_id,),
        ).fetchall()
    ]
    _recompute_weights(conn, generation_id, ids)
    conn.execute(
        "UPDATE index_generations SET doc_count=? WHERE id=?", (doc_count, generation_id)
    )


# --------------------------------------------------------------------------- #
# 覆盖校验与切换
# --------------------------------------------------------------------------- #

def list_live_document_ids(conn, workspace_id: int) -> list[int]:
    return [
        int(r["id"])
        for r in conn.execute(
            "SELECT id FROM documents WHERE workspace_id=? AND deleted_at IS NULL ORDER BY id",
            (workspace_id,),
        ).fetchall()
    ]


def generation_missing_docs(conn, generation_id: int, workspace_id: int) -> list[int]:
    live = set(list_live_document_ids(conn, workspace_id))
    indexed = {
        int(r["document_id"])
        for r in conn.execute(
            "SELECT DISTINCT document_id FROM doc_term_stats WHERE generation_id=?",
            (generation_id,),
        ).fetchall()
    }
    return sorted(live - indexed)


def activate_generation(workspace_id: int, generation_id: int) -> dict:
    """覆盖校验通过后，单事务切换 active。返回 {switched, missing}。"""
    with immediate_transaction() as conn:
        gen = conn.execute(
            "SELECT * FROM index_generations WHERE id=? AND workspace_id=?",
            (generation_id, workspace_id),
        ).fetchone()
        if gen is None:
            raise ValueError("索引代次不存在或跨工作区")
        missing = generation_missing_docs(conn, generation_id, workspace_id)
        if missing:
            return {"switched": False, "missing": missing}
        live_count = len(list_live_document_ids(conn, workspace_id))
        # 终态权重全量重算（building 期间增量文档的 df 也在这最终对齐）
        finalize_generation(conn, generation_id, live_count)
        prev = conn.execute(
            "SELECT active_index_generation_id FROM workspaces WHERE id=?",
            (workspace_id,),
        ).fetchone()
        prev_id = prev["active_index_generation_id"]
        conn.execute(
            "UPDATE index_generations SET status='superseded', "
            "activated_at=activated_at WHERE workspace_id=? AND status='active'",
            (workspace_id,),
        )
        conn.execute(
            "UPDATE index_generations SET status='active', activated_at=?, doc_count=? WHERE id=?",
            (utcnow(), live_count, generation_id),
        )
        conn.execute(
            "UPDATE workspaces SET active_index_generation_id=? WHERE id=?",
            (generation_id, workspace_id),
        )
        if prev_id is not None and int(prev_id) != generation_id:
            # 旧代物理清理（superseded 代的数据不再保留，防止混读与存储泄漏）
            _delete_generation_data(conn, int(prev_id))
            conn.execute(
                "DELETE FROM index_generations WHERE id=? AND status='superseded'",
                (int(prev_id),),
            )
        return {"switched": True, "missing": [], "previous_generation_id": prev_id}


def fail_generation(generation_id: int, error: str) -> None:
    with immediate_transaction() as conn:
        conn.execute(
            "UPDATE index_generations SET status='failed', error=? WHERE id=? AND status='building'",
            (error[:4000], generation_id),
        )


def abandon_building(workspace_id: int, generation_id: int) -> None:
    with immediate_transaction() as conn:
        gen = conn.execute(
            "SELECT status FROM index_generations WHERE id=? AND workspace_id=?",
            (generation_id, workspace_id),
        ).fetchone()
        if gen and gen["status"] == "building":
            _delete_generation_data(conn, generation_id)
            conn.execute("DELETE FROM index_generations WHERE id=?", (generation_id,))


def _delete_generation_data(conn, generation_id: int) -> None:
    for table in ("postings", "doc_term_stats", "index_df"):
        conn.execute(f"DELETE FROM {table} WHERE generation_id=?", (generation_id,))


# --------------------------------------------------------------------------- #
# 删除文档时的索引维护（全代清理 + active 代 df/权重收敛）
# --------------------------------------------------------------------------- #

def purge_document_from_indexes(conn, workspace_id: int, document_id: int) -> None:
    """在删除事务内调用：从该工作区所有代清掉文档，并收敛 active 代的 df/权重。"""
    gens = [
        int(r["id"])
        for r in conn.execute(
            "SELECT id FROM index_generations WHERE workspace_id=?", (workspace_id,)
        ).fetchall()
    ]
    active_id = None
    ws = conn.execute(
        "SELECT active_index_generation_id FROM workspaces WHERE id=?", (workspace_id,)
    ).fetchone()
    if ws:
        active_id = ws["active_index_generation_id"]
    for gen_id in gens:
        conn.execute(
            "DELETE FROM postings WHERE generation_id=? AND document_id=?",
            (gen_id, document_id),
        )
        conn.execute(
            "DELETE FROM doc_term_stats WHERE generation_id=? AND document_id=?",
            (gen_id, document_id),
        )
        if gen_id == active_id:
            # 删除后受影响词的 df 重算，归零的词从 index_df 移除（防泄漏）
            terms = [
                r["term"]
                for r in conn.execute(
                    "SELECT term FROM index_df WHERE generation_id=?", (gen_id,)
                ).fetchall()
            ]
            remaining = {
                r["term"]: int(r["c"])
                for r in conn.execute(
                    "SELECT term, COUNT(DISTINCT document_id) AS c FROM doc_term_stats "
                    "WHERE generation_id=? GROUP BY term",
                    (gen_id,),
                ).fetchall()
            }
            for term in terms:
                df = remaining.get(term, 0)
                if df == 0:
                    conn.execute(
                        "DELETE FROM index_df WHERE generation_id=? AND term=?",
                        (gen_id, term),
                    )
                else:
                    conn.execute(
                        "UPDATE index_df SET df=? WHERE generation_id=? AND term=?",
                        (df, gen_id, term),
                    )
            # 全代权重重算（N 变了）
            ids = [
                int(r["document_id"])
                for r in conn.execute(
                    "SELECT DISTINCT document_id FROM doc_term_stats WHERE generation_id=?",
                    (gen_id,),
                ).fetchall()
            ]
            _recompute_weights(conn, gen_id, ids)
            conn.execute(
                "UPDATE index_generations SET doc_count=? WHERE id=?",
                (len(ids), gen_id),
            )


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #

def _query_terms(q: str) -> list[str]:
    seen: set[str] = set()
    terms: list[str] = []
    for tok in tokenize(q):
        if tok.term not in seen:
            seen.add(tok.term)
            terms.append(tok.term)
    return terms


def search(workspace_id: int, query: str, top_k: int = 10) -> dict:
    gen_id = get_active_generation_id(workspace_id)
    terms = _query_terms(query)
    if gen_id is None or not terms:
        return {"query": query, "generation_id": gen_id, "terms": terms, "results": []}
    with session_scope() as sess:
        # AND 语义：每个查询词都要在该文档出现；多字中文由此自然要求逐字都在。
        # 打分：归一化文档向量与等权查询向量点积，再乘 1/sqrt(n)（对排序无影响但可读）。
        params: dict = {"ws": workspace_id, "gen": gen_id}
        term_clauses = []
        for i, term in enumerate(terms):
            key = f"t{i}"
            params[key] = term
            term_clauses.append(
                f"EXISTS (SELECT 1 FROM doc_term_stats dts{i} "
                f"JOIN documents dd{i} ON dd{i}.id=dts{i}.document_id "
                f"WHERE dts{i}.generation_id=:gen AND dts{i}.document_id=d.id "
                f"AND dd{i}.deleted_at IS NULL AND dts{i}.term=:t{i})"
            )
        and_sql = " AND ".join(term_clauses)
        weight_sum = "+".join(
            f"COALESCE((SELECT w{i}.weight FROM doc_term_stats w{i} "
            f"WHERE w{i}.generation_id=:gen AND w{i}.document_id=d.id AND w{i}.term=:t{i}),0)"
            for i in range(len(terms))
        )
        sql = text(
            f"SELECT d.id AS doc_id, d.name AS name, ({weight_sum}) AS score "
            f"FROM documents d WHERE d.workspace_id=:ws AND d.deleted_at IS NULL AND {and_sql} "
            f"ORDER BY score DESC, d.id ASC LIMIT :k"
        )
        params["k"] = top_k
        rows = sess.execute(sql, params).mappings().all()

        # 稳定排序第三键：首个命中位置（在 score、doc_id 相同时）
        results = []
        qnorm = 1.0 / math.sqrt(len(terms))
        for r in rows:
            first_pos = None
            positions: dict[str, list[list[int]]] = {}
            for i, term in enumerate(terms):
                prows = sess.execute(
                    text(
                        "SELECT char_start, char_end FROM postings "
                        "WHERE generation_id=:gen AND document_id=:doc AND term=:term "
                        "ORDER BY char_start"
                    ),
                    {"gen": gen_id, "doc": r["doc_id"], "term": term},
                ).all()
                positions[term] = [[p[0], p[1]] for p in prows]
                if prows and (first_pos is None or prows[0][0] < first_pos):
                    first_pos = prows[0][0]
            results.append(
                {
                    "doc_id": r["doc_id"],
                    "name": r["name"],
                    "score": round(float(r["score"]) * qnorm, 8),
                    "first_char_start": first_pos if first_pos is not None else 0,
                    "positions": positions,
                }
            )
        results.sort(key=lambda x: (-x["score"], x["doc_id"], x["first_char_start"]))
        return {
            "query": query,
            "generation_id": gen_id,
            "rule_pack_version": sess.get(IndexGeneration, gen_id).rule_pack_version,
            "terms": terms,
            "result_count": len(results),
            "results": results,
        }


def document_keywords(workspace_id: int, document_id: int, top_k: int = 10) -> dict:
    """单文档关键词：active 代下按 TF-IDF 权重取 top k，稳定次序 weight DESC, term ASC。"""
    gen_id = get_active_generation_id(workspace_id)
    if gen_id is None:
        return {"document_id": document_id, "generation_id": None, "keywords": []}
    with session_scope() as sess:
        doc = sess.execute(
            select(IndexGeneration).where(IndexGeneration.id == gen_id)
        ).scalar_one()
        rows = sess.execute(
            text(
                "SELECT dts.term, dts.tf, dts.weight FROM doc_term_stats dts "
                "JOIN documents d ON d.id=dts.document_id "
                "WHERE dts.generation_id=:gen AND dts.document_id=:doc "
                "AND d.workspace_id=:ws AND d.deleted_at IS NULL "
                "ORDER BY dts.weight DESC, dts.term ASC LIMIT :k"
            ),
            {"gen": gen_id, "doc": document_id, "ws": workspace_id, "k": top_k},
        ).all()
        return {
            "document_id": document_id,
            "generation_id": gen_id,
            "rule_pack_version": doc.rule_pack_version,
            "keywords": [
                {"term": r[0], "tf": r[1], "weight": round(float(r[2]), 8)} for r in rows
            ],
        }
