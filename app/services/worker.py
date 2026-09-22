"""本地后台工作器。

- 最多 ``KEX_MAX_WORKERS`` 个租约（跨进程/容器，由认领事务保证）；
- 周期性心跳并回收过期租约，因此进程被杀/重启后 running 作业自动回到 pending；
- 每个作业分阶段执行，失败时记录 stage + doc_id；超过尝试次数置 failed，
  由 POST /api/jobs/<id>/retry 手动重试；
- 重建作业带检查点：已补抽文档集合持久化，重启后继续而不是重头。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid

from ..config import config
from ..db import immediate_transaction
from ..tokenizer import tokenize
from . import extractor, indexing, jobs as jobs_service
from .rule_packs import load_pack

log = logging.getLogger("kex.worker")

MAX_TAIL_PASSES = 64


class StopWorker(Exception):
    pass


class Worker(threading.Thread):
    def __init__(self, worker_id: str | None = None, *, poll_interval: float = 0.25):
        super().__init__(name="kex-worker", daemon=True)
        self.worker_id = worker_id or f"w-{uuid.uuid4().hex[:10]}"
        self.poll_interval = poll_interval
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ #

    def run(self) -> None:
        log.info("worker %s starting (max leases=%s)", self.worker_id, config.max_workers)
        idle_since = 0.0
        while not self._stop.is_set():
            try:
                jobs_service.heartbeat(self.worker_id)
                job = jobs_service.claim_job(self.worker_id)
            except Exception:
                log.exception("claim failed")
                self._wait(0.5)
                continue
            if job is None:
                # 空闲时拉长心跳节奏
                if time.monotonic() - idle_since > 1.0:
                    jobs_service.heartbeat(self.worker_id)
                    idle_since = time.monotonic()
                self._wait(self.poll_interval)
                continue
            idle_since = time.monotonic()
            self._run_one(job)
        log.info("worker %s stopped", self.worker_id)

    def _wait(self, seconds: float) -> None:
        self._stop.wait(seconds)

    # ------------------------------------------------------------------ #

    def _run_one(self, job: dict) -> None:
        jid = job["id"]
        kind = job["kind"]
        try:
            if kind == "extract":
                self._run_extract(job)
            elif kind == "rebuild":
                self._run_rebuild(job)
            else:
                raise RuntimeError(f"未知作业类型 {kind}")
        except StopWorker:
            raise
        except Exception as exc:
            log.exception("job %s failed", jid)
            will_retry = jobs_service.fail_job(
                jid,
                stage=getattr(exc, "stage", kind),
                doc_id=getattr(exc, "doc_id", None),
                message=f"{type(exc).__name__}: {exc}",
            )
            if not will_retry:
                if kind == "rebuild":
                    payload = json.loads(job["payload_json"] or "{}")
                    if payload.get("generation_id"):
                        indexing.fail_generation(payload["generation_id"], str(exc))
        else:
            jobs_service.complete_job(jid)

    def _renew(self, job_id: int) -> None:
        if not jobs_service.renew_lease(job_id, self.worker_id):
            # 租约丢了（通常是被人工重置/过期回收）——立刻停手以免双跑
            raise StopWorker(str(job_id))

    # ------------------------------------------------------------------ #

    def _run_extract(self, job: dict) -> None:
        self._renew(job["id"])
        result = extractor.extract_job(
            document_id=job["document_id"],
            workspace_id=job["workspace_id"],
            rule_pack_version=job["rule_pack_version"],
            doc_sha256=job["doc_sha256"],
        )
        jobs_service.save_checkpoint(job["id"], {"result": result})

    def _run_rebuild(self, job: dict) -> None:
        payload = json.loads(job["payload_json"] or "{}")
        generation_id = int(payload["generation_id"])
        workspace_id = job["workspace_id"]
        version = job["rule_pack_version"]

        checkpoint = job.get("checkpoint_json") and json.loads(job["checkpoint_json"]) or {}
        processed: set[int] = set(checkpoint.get("extracted_docs", []))
        seeded = bool(processed)
        passes = int(checkpoint.get("passes", 0))

        pack = load_pack(version)

        while True:
            self._renew(job["id"])
            with immediate_transaction() as conn:
                gen = conn.execute(
                    "SELECT status FROM index_generations WHERE id=? AND workspace_id=?",
                    (generation_id, workspace_id),
                ).fetchone()
                if gen is None:
                    raise RebuildError("generation_missing", None, f"索引代 {generation_id} 不存在")
                if gen["status"] != "building":
                    raise RebuildError(
                        "generation_state", None, f"索引代状态为 {gen['status']}，停止重建"
                    )

                # 首轮预置：抽取作业（激活时入队）已「在本代索引 + 用本规则抽过实体」
                # 的文档不重复处理。两个条件缺一不可——只在旧代有索引或只有实体都不算完成。
                if not seeded:
                    already = conn.execute(
                        "SELECT DISTINCT dts.document_id FROM doc_term_stats dts "
                        "WHERE dts.generation_id=? AND EXISTS ("
                        "SELECT 1 FROM entities e WHERE e.document_id=dts.document_id "
                        "AND e.rule_pack_version=?)",
                        (generation_id, version),
                    ).fetchall()
                    processed.update(int(r["document_id"]) for r in already)
                    seeded = True

                # 快照：此刻存活文档
                live = [
                    (int(r["id"]), r["doc_sha256"])
                    for r in conn.execute(
                        "SELECT id, doc_sha256 FROM documents "
                        "WHERE workspace_id=? AND deleted_at IS NULL ORDER BY id",
                        (workspace_id,),
                    ).fetchall()
                ]
                todo = [d for d in live if d[0] not in processed]

                if not todo:
                    # 所有存活文档都已抽过：进入最终覆盖校验 + 切换
                    missing = indexing.generation_missing_docs(conn, generation_id, workspace_id)
                    if missing:
                        # 极端竞态：实体有但索引行缺失，直接在本事务补建索引
                        for doc_id in missing:
                            self._index_one_conn(conn, workspace_id, doc_id, generation_id, stage_doc=doc_id)
                            processed.add(doc_id)
                    if indexing.generation_missing_docs(conn, generation_id, workspace_id):
                        raise RebuildError(
                            "coverage", None,
                            f"覆盖校验失败，缺文档 {missing[:10]}",
                        )
                    live_count = len(indexing.list_live_document_ids(conn, workspace_id))
                    indexing.finalize_generation(conn, generation_id, live_count)
                    # 切换
                    self._activate_conn(conn, workspace_id, generation_id)
                    jobs_service.save_checkpoint_conn(
                        conn, job["id"],
                        {"extracted_docs": sorted(processed), "passes": passes, "activated": True},
                    )
                    return

                # 处理一批（每批至多 20 篇，中间可续租、可持久化检查点）
                batch = todo[:20]
                batch_doc_ids: list[int] = []
                for doc_id, sha in batch:
                    ok, content = extractor.get_fresh_blob(
                        conn, workspace_id=workspace_id, document_id=doc_id, expected_sha=sha
                    )
                    if not ok:
                        # 已删除或已改版：跳过（改版文档会有新作业另行处理）
                        processed.add(doc_id)
                        continue
                    try:
                        mentions = extractor.run_extract(content, pack)
                        extractor.persist_entities(
                            conn,
                            workspace_id=workspace_id,
                            document_id=doc_id,
                            rule_pack_version=version,
                            mentions=mentions,
                        )
                        indexing._upsert_postings(conn, generation_id, doc_id, tokenize(content))
                    except Exception as exc:
                        raise RebuildError("extract", doc_id, str(exc)) from exc
                    processed.add(doc_id)
                    batch_doc_ids.append(doc_id)

            passes += 1
            jobs_service.save_checkpoint(
                job["id"], {"extracted_docs": sorted(processed), "passes": passes}
            )
            if passes > MAX_TAIL_PASSES * 50:
                raise RebuildError("coverage", None, "重建轮次超过上限，可能存在持续写入竞争")

    def _index_one_conn(self, conn, workspace_id, doc_id, generation_id, *, stage_doc):
        row = conn.execute(
            "SELECT doc_sha256, blob_sha256 FROM documents WHERE workspace_id=? AND id=? AND deleted_at IS NULL",
            (workspace_id, doc_id),
        ).fetchone()
        if row is None:
            return
        blob = conn.execute(
            "SELECT content FROM blobs WHERE sha256=?", (row["blob_sha256"],)
        ).fetchone()
        if blob is None:
            raise RebuildError("index", stage_doc, f"文档 {doc_id} 的内容缺失")
        indexing._upsert_postings(conn, generation_id, doc_id, tokenize(blob["content"]))

    def _activate_conn(self, conn, workspace_id: int, generation_id: int) -> None:
        from ..models import utcnow

        prev = conn.execute(
            "SELECT active_index_generation_id FROM workspaces WHERE id=?", (workspace_id,)
        ).fetchone()
        prev_id = prev["active_index_generation_id"]
        conn.execute(
            "UPDATE index_generations SET status='superseded' "
            "WHERE workspace_id=? AND status='active'",
            (workspace_id,),
        )
        conn.execute(
            "UPDATE index_generations SET status='active', activated_at=? WHERE id=?",
            (utcnow(), generation_id),
        )
        conn.execute(
            "UPDATE workspaces SET active_index_generation_id=? WHERE id=?",
            (generation_id, workspace_id),
        )
        if prev_id is not None and int(prev_id) != generation_id:
            indexing._delete_generation_data(conn, int(prev_id))
            conn.execute(
                "DELETE FROM index_generations WHERE id=? AND status='superseded'",
                (int(prev_id),),
            )


class RebuildError(Exception):
    def __init__(self, stage: str, doc_id: int | None, message: str):
        super().__init__(message)
        self.stage = stage
        self.doc_id = doc_id


# --------------------------------------------------------------------------- #
# 进程入口
# --------------------------------------------------------------------------- #

def run_forever(num_threads: int | None = None) -> None:  # pragma: no cover
    n = num_threads or config.max_workers
    workers = [Worker(poll_interval=0.2) for _ in range(max(1, min(n, config.max_workers)))]
    for w in workers:
        w.start()
    try:
        while any(w.is_alive() for w in workers):
            time.sleep(0.5)
    except KeyboardInterrupt:
        for w in workers:
            w.stop()
        for w in workers:
            w.join(timeout=5)
