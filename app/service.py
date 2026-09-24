"""业务服务层: 注册不可变对象、创建快照、执行尝试、校验后发布索引。

约定:
- 每个数据库操作打开独立连接 (SQLite WAL + busy_timeout 保证线程安全);
- 写操作在事务内完成, 失败回滚;
- 产物先写入内容寻址证据库并重算校验, 全部通过后才在同一事务中
  写索引 (带 HMAC 签名) 并把尝试置为 succeeded —— 校验不过绝不发布;
- running 状态的尝试在服务启动时由 recover_interrupted() 标记为
  interrupted, 模拟崩溃/重启后的真实恢复。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from . import compute as compute_mod
from .canonical import canonical_bytes
from .config import Settings
from .crypto import content_id, hmac_sign, hmac_verify, load_or_create_key
from .db import connect, init_db, transaction
from .storage import EvidenceStore


class ReproConflict(Exception):
    """标记可复现但校验未通过 (HTTP 409)。"""

    def __init__(self, checks: list[dict[str, Any]]) -> None:
        self.checks = checks
        super().__init__("reproducibility checks failed")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Service:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.settings.ensure_dirs()
        self.store = EvidenceStore(self.settings.evidence_dir)
        self.hmac_key = load_or_create_key(self.settings.key_path)
        with closing(connect(self.settings.db_path)) as conn:
            init_db(conn)
        self.recover_interrupted()

    # ---------- 基础工具 ----------

    def _register_doc(
        self, table: str, body: dict[str, Any], prefix: str
    ) -> dict[str, Any]:
        data = canonical_bytes(body)
        cid = content_id(prefix, data)
        with closing(connect(self.settings.db_path)) as conn, transaction(conn):
            conn.execute(
                f"INSERT OR IGNORE INTO {table} (id, body_json, created_at) "
                "VALUES (?, ?, ?)",
                (cid, data.decode("utf-8"), utcnow()),
            )
            row = conn.execute(
                f"SELECT * FROM {table} WHERE id = ?", (cid,)
            ).fetchone()
        return {
            "id": row["id"],
            "sha256": cid.split("-", 1)[1],
            "body": json.loads(row["body_json"]),
            "created_at": row["created_at"],
        }

    # ---------- bag / 参数 / 标定 / 算法 ----------

    def register_bag(self, raw: bytes) -> dict[str, Any]:
        bag = compute_mod.parse_bag(raw)  # 服务端真实解析与校验
        summary = compute_mod.summarize_bag(raw)
        cid = content_id("bag", raw)
        stored = self.store.put(raw, digest=cid.split("-", 1)[1])
        with closing(connect(self.settings.db_path)) as conn, transaction(conn):
            conn.execute(
                "INSERT OR IGNORE INTO bags (id, sha256, size_bytes, summary, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    cid,
                    cid.split("-", 1)[1],
                    len(raw),
                    canonical_bytes(summary).decode("utf-8"),
                    utcnow(),
                ),
            )
            row = conn.execute("SELECT * FROM bags WHERE id = ?", (cid,)).fetchone()
        return {
            "id": row["id"],
            "sha256": row["sha256"],
            "size_bytes": row["size_bytes"],
            "summary": json.loads(row["summary"]),
            "bag_id_in_file": bag["bag_id"],
            "evidence_reused": stored.reused,
            "evidence_repaired": stored.repaired,
        }

    def register_params(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._register_doc("params", body, "params")

    def register_calibration(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._register_doc("calibrations", body, "cal")

    def register_algorithm(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._register_doc("algorithms", body, "algo")

    # ---------- 不可变快照 ----------

    def create_snapshot(self, refs: dict[str, str]) -> dict[str, Any]:
        payload = canonical_bytes(refs)
        cid = content_id("snap", payload)
        with closing(connect(self.settings.db_path)) as conn, transaction(conn):
            for table, key in (
                ("bags", refs["bag_id"]),
                ("params", refs["params_id"]),
                ("calibrations", refs["calibration_id"]),
                ("algorithms", refs["algorithm_id"]),
            ):
                row = conn.execute(
                    f"SELECT 1 FROM {table} WHERE id = ?", (key,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"{table[:-1]} 不存在: {key}")
            conn.execute(
                "INSERT OR IGNORE INTO snapshots "
                "(id, bag_id, params_id, calibration_id, algorithm_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    cid,
                    refs["bag_id"],
                    refs["params_id"],
                    refs["calibration_id"],
                    refs["algorithm_id"],
                    utcnow(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM snapshots WHERE id = ?", (cid,)
            ).fetchone()
        return dict(row)

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        with closing(connect(self.settings.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
        if row is None:
            raise KeyError(snapshot_id)
        return dict(row)

    def list_snapshots(self) -> list[dict[str, Any]]:
        with closing(connect(self.settings.db_path)) as conn:
            rows = conn.execute(
                "SELECT * FROM snapshots ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 运行与尝试 ----------

    def start_run(self, snapshot_id: str, seed: int) -> dict[str, Any]:
        if not (-(2**63) <= seed <= 2**63 - 1):
            raise ValueError("seed 必须是 64 位有符号整数")
        run_id = content_id("run", f"{snapshot_id}:{seed}".encode())
        with closing(connect(self.settings.db_path)) as conn, transaction(conn):
            if conn.execute(
                "SELECT 1 FROM snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone() is None:
                raise KeyError(f"snapshot 不存在: {snapshot_id}")
            conn.execute(
                "INSERT OR IGNORE INTO runs (id, snapshot_id, seed, created_at)"
                " VALUES (?, ?, ?, ?)",
                (run_id, snapshot_id, seed, utcnow()),
            )
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._run_view(dict(row))

    def _run_view(self, run: dict[str, Any]) -> dict[str, Any]:
        with closing(connect(self.settings.db_path)) as conn:
            attempts = conn.execute(
                "SELECT * FROM attempts WHERE run_id = ? ORDER BY attempt_no",
                (run["id"],),
            ).fetchall()
        run["attempts"] = [dict(a) for a in attempts]
        return run

    def get_run(self, run_id: str) -> dict[str, Any]:
        with closing(connect(self.settings.db_path)) as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._run_view(dict(row))

    def submit_attempt(
        self, run_id: str, delay_ms: int = 0, fault: str | None = None
    ) -> dict[str, Any]:
        with closing(connect(self.settings.db_path)) as conn, transaction(conn):
            run = conn.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            row = conn.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) + 1 AS n FROM attempts WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            attempt_no = row["n"]
            attempt_id = content_id(
                "att", canonical_bytes({"run_id": run_id, "attempt_no": attempt_no})
            )
            started = utcnow()
            conn.execute(
                "INSERT INTO attempts (id, run_id, attempt_no, status, started_at)"
                " VALUES (?, ?, ?, 'running', ?)",
                (attempt_id, run_id, attempt_no, started),
            )
        # 事务提交后再启动后台执行: 即使立刻崩溃, running 记录也已持久化。
        thread = threading.Thread(
            target=self._execute_attempt,
            args=(attempt_id, run_id, attempt_no, delay_ms, fault),
            daemon=True,
        )
        thread.start()
        return self.get_attempt(attempt_id)

    def get_attempt(self, attempt_id: str) -> dict[str, Any]:
        with closing(connect(self.settings.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        return dict(row)

    # ---------- 尝试执行 (后台线程) ----------

    def _execute_attempt(
        self,
        attempt_id: str,
        run_id: str,
        attempt_no: int,
        delay_ms: int,
        fault: str | None,
    ) -> None:
        if delay_ms:
            import time

            time.sleep(delay_ms / 1000.0)
        if fault == "crash":
            # 真实模拟运行中进程崩溃: 不清理 running 状态, 直接退出。
            os._exit(2)

        try:
            with closing(connect(self.settings.db_path)) as conn, transaction(conn):
                run = conn.execute(
                    "SELECT * FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
                snap = conn.execute(
                    "SELECT * FROM snapshots WHERE id = ?", (run["snapshot_id"],)
                ).fetchone()
                bag_row = conn.execute(
                    "SELECT * FROM bags WHERE id = ?", (snap["bag_id"],)
                ).fetchone()
                params = json.loads(
                    conn.execute(
                        "SELECT body_json FROM params WHERE id = ?",
                        (snap["params_id"],),
                    ).fetchone()["body_json"]
                )
                calib = json.loads(
                    conn.execute(
                        "SELECT body_json FROM calibrations WHERE id = ?",
                        (snap["calibration_id"],),
                    ).fetchone()["body_json"]
                )
                algo = json.loads(
                    conn.execute(
                        "SELECT body_json FROM algorithms WHERE id = ?",
                        (snap["algorithm_id"],),
                    ).fetchone()["body_json"]
                )

                # 1) 先校验输入证据: 内容必须仍与哈希一致 (测试输入被替换 -> 失败)。
                if not self.store.has(bag_row["sha256"]):
                    self._fail(
                        conn,
                        attempt_id,
                        "input_verification_failed",
                        f"bag 证据缺失: {bag_row['sha256']}",
                    )
                    return
                try:
                    raw = self.store.get(bag_row["sha256"])
                    compute_mod.parse_bag(raw)
                except (ValueError, FileNotFoundError) as exc:
                    self._fail(
                        conn,
                        attempt_id,
                        "input_verification_failed",
                        f"bag 证据校验失败: {exc}",
                    )
                    return

                # 2) 真实计算。
                result = compute_mod.run_experiment(
                    raw,
                    params,
                    calib,
                    algo,
                    snap["id"],
                    bag_row["sha256"],
                    snap["params_id"].split("-", 1)[1],
                    snap["calibration_id"].split("-", 1)[1],
                    snap["algorithm_id"].split("-", 1)[1],
                    run["seed"],
                )

                # 故障注入: 运行结果被改动。
                if fault == "corrupt_result":
                    result["clusters"] = [{"tampered": True, "size": -1}]

                # 3) 发布前校验: 独立重算, 逐字节比对。
                expected = compute_mod.result_bytes(
                    compute_mod.run_experiment(
                        raw,
                        params,
                        calib,
                        algo,
                        snap["id"],
                        bag_row["sha256"],
                        snap["params_id"].split("-", 1)[1],
                        snap["calibration_id"].split("-", 1)[1],
                        snap["algorithm_id"].split("-", 1)[1],
                        run["seed"],
                    )
                )
                if not compute_mod.verify_result(result, expected):
                    self._fail(
                        conn,
                        attempt_id,
                        "output_verification_failed",
                        "产物重算结果与实际输出逐字节不一致, 拒绝发布",
                    )
                    return

                result_bytes = compute_mod.result_bytes(result)

                # 4) 故障注入: 产物在发布前丢失。
                if fault == "lose_artifact":
                    self._fail(
                        conn,
                        attempt_id,
                        "artifact_missing",
                        "产物未能写入证据库 (注入故障), 未发布索引",
                    )
                    return

                # 5) 写入内容寻址证据 (内容相同则复用, 不覆盖)。
                stored = self.store.put(result_bytes)
                artifact_id = content_id("art", result_bytes)
                conn.execute(
                    "INSERT OR IGNORE INTO artifacts (id, sha256, kind, size_bytes, created_at)"
                    " VALUES (?, ?, 'result', ?, ?)",
                    (artifact_id, stored.sha256, stored.size, utcnow()),
                )

                # 6) 校验全部通过 -> 组装并签名索引, 与状态推进同一事务发布。
                payload = {
                    "schema": "experiment-index/v1",
                    "attempt_id": attempt_id,
                    "attempt_no": attempt_no,
                    "run_id": run_id,
                    "snapshot_id": snap["id"],
                    "seed": run["seed"],
                    "result_artifact": {
                        "sha256": stored.sha256,
                        "size_bytes": stored.size,
                    },
                    "inputs": {
                        "bag_sha256": bag_row["sha256"],
                        "params_id": snap["params_id"],
                        "calibration_id": snap["calibration_id"],
                        "algorithm_id": snap["algorithm_id"],
                    },
                    "published_at": utcnow(),
                }
                payload_bytes = canonical_bytes(payload)
                signature = hmac_sign(self.hmac_key, payload_bytes)
                index_id = content_id("idx", payload_bytes)
                conn.execute(
                    "INSERT INTO index_entries (id, attempt_id, payload_json, signature, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (index_id, attempt_id, payload_bytes.decode("utf-8"), signature, utcnow()),
                )
                conn.execute(
                    "UPDATE attempts SET status='succeeded', finished_at=?, "
                    "result_artifact_id=?, index_entry_id=? WHERE id=?",
                    (utcnow(), artifact_id, index_id, attempt_id),
                )
        except Exception as exc:  # 真实失败如实落库, 绝不伪装成功
            self._safe_fail(attempt_id, "runtime_error", repr(exc))

    def _fail(
        self,
        conn: sqlite3.Connection,
        attempt_id: str,
        code: str,
        message: str,
    ) -> None:
        body = {
            "schema": "experiment-error/v1",
            "attempt_id": attempt_id,
            "error_code": code,
            "error_message": message,
            "recorded_at": utcnow(),
        }
        body_bytes = canonical_bytes(body)
        stored = self.store.put(body_bytes)
        artifact_id = content_id("arterr", body_bytes)
        conn.execute(
            "INSERT OR IGNORE INTO artifacts (id, sha256, kind, size_bytes, created_at)"
            " VALUES (?, ?, 'error', ?, ?)",
            (artifact_id, stored.sha256, stored.size, utcnow()),
        )
        conn.execute(
            "UPDATE attempts SET status='failed', error_code=?, error_message=?, "
            "error_artifact_id=?, finished_at=? WHERE id=?",
            (code, message, artifact_id, utcnow(), attempt_id),
        )

    def _safe_fail(self, attempt_id: str, code: str, message: str) -> None:
        try:
            with closing(connect(self.settings.db_path)) as conn, transaction(conn):
                self._fail(conn, attempt_id, code, message)
        except Exception:
            pass  # 失败记录本身再失败时不吞掉进程, 保留 running 由重启恢复处理

    # ---------- 校验 / 标记可复现 ----------

    def _load_attempt_inputs(
        self, conn: sqlite3.Connection, attempt: sqlite3.Row
    ) -> dict[str, Any]:
        run = conn.execute(
            "SELECT * FROM runs WHERE id = ?", (attempt["run_id"],)
        ).fetchone()
        snap = conn.execute(
            "SELECT * FROM snapshots WHERE id = ?", (run["snapshot_id"],)
        ).fetchone()
        bag = conn.execute(
            "SELECT * FROM bags WHERE id = ?", (snap["bag_id"],)
        ).fetchone()
        params = json.loads(
            conn.execute(
                "SELECT body_json FROM params WHERE id=?", (snap["params_id"],)
            ).fetchone()["body_json"]
        )
        calib = json.loads(
            conn.execute(
                "SELECT body_json FROM calibrations WHERE id=?",
                (snap["calibration_id"],),
            ).fetchone()["body_json"]
        )
        algo = json.loads(
            conn.execute(
                "SELECT body_json FROM algorithms WHERE id=?",
                (snap["algorithm_id"],),
            ).fetchone()["body_json"]
        )
        return {
            "run": run,
            "snap": snap,
            "bag": bag,
            "params": params,
            "calib": calib,
            "algo": algo,
        }

    def verify_attempt(self, attempt_id: str) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []

        def add(name: str, ok: bool, detail: str = "") -> None:
            checks.append({"name": name, "ok": ok, "detail": detail})

        with closing(connect(self.settings.db_path)) as conn:
            attempt = conn.execute(
                "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise KeyError(attempt_id)
            ctx = self._load_attempt_inputs(conn, attempt)
            index_row = conn.execute(
                "SELECT * FROM index_entries WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            art_row = None
            if attempt["result_artifact_id"]:
                art_row = conn.execute(
                    "SELECT * FROM artifacts WHERE id = ?",
                    (attempt["result_artifact_id"],),
                ).fetchone()

        add(
            "attempt_succeeded",
            attempt["status"] in ("succeeded", "marked_reproducible"),
            f"status={attempt['status']} error={attempt['error_code'] or '-'}",
        )
        add(
            "seed_recorded",
            ctx["run"]["seed"] is not None,
            f"seed={ctx['run']['seed']}",
        )

        art_ok = bool(art_row) and self.store.has(art_row["sha256"])
        add("artifact_present", art_ok, attempt["result_artifact_id"] or "无产物记录")

        result: dict[str, Any] | None = None
        if art_ok:
            try:
                result = json.loads(self.store.get(art_row["sha256"]))
            except (ValueError, FileNotFoundError) as exc:
                add("artifact_bytes_valid", False, str(exc))
            else:
                add(
                    "artifact_bytes_valid",
                    isinstance(result, dict) and result.get("schema", "").startswith(
                        "robot-experiment-result"
                    ),
                    art_row["sha256"],
                )

        idx_ok = False
        if index_row is not None:
            payload_bytes = index_row["payload_json"].encode("utf-8")
            sig_ok = hmac_verify(self.hmac_key, payload_bytes, index_row["signature"])
            payload = json.loads(index_row["payload_json"])
            consistent = (
                payload.get("attempt_id") == attempt_id
                and payload.get("run_id") == attempt["run_id"]
                and art_row is not None
                and payload["result_artifact"]["sha256"] == art_row["sha256"]
            )
            idx_ok = sig_ok and consistent
            add(
                "index_signature_valid",
                sig_ok,
                "HMAC-SHA256 验签" + ("通过" if sig_ok else "失败"),
            )
            add("index_payload_consistent", consistent, "索引与产物/尝试互相对应")
        else:
            add("index_signature_valid", False, "索引缺失")
            add("index_payload_consistent", False, "索引缺失")

        recompute_ok = False
        if result is not None and art_ok:
            raw = self.store.get(ctx["bag"]["sha256"])
            expected = compute_mod.run_experiment(
                raw,
                ctx["params"],
                ctx["calib"],
                ctx["algo"],
                ctx["snap"]["id"],
                ctx["bag"]["sha256"],
                ctx["snap"]["params_id"].split("-", 1)[1],
                ctx["snap"]["calibration_id"].split("-", 1)[1],
                ctx["snap"]["algorithm_id"].split("-", 1)[1],
                ctx["run"]["seed"],
            )
            recompute_ok = compute_mod.verify_result(
                expected, compute_mod.result_bytes(result)
            )
            add(
                "recomputation_matches",
                recompute_ok,
                "用快照输入与种子独立重算, 与产物逐字节比较",
            )
            input_hashes_ok = (
                result["inputs"]["bag_sha256"] == ctx["bag"]["sha256"]
                and result["inputs"]["params_sha256"]
                == ctx["snap"]["params_id"].split("-", 1)[1]
                and result["inputs"]["calibration_sha256"]
                == ctx["snap"]["calibration_id"].split("-", 1)[1]
                and result["inputs"]["algorithm_sha256"]
                == ctx["snap"]["algorithm_id"].split("-", 1)[1]
            )
            add(
                "result_binds_snapshot_inputs",
                input_hashes_ok,
                "产物记录的四个输入哈希与快照绑定一致",
            )
        else:
            add("recomputation_matches", False, "缺少可重算的产物")
            add("result_binds_snapshot_inputs", False, "缺少产物")

        reproducible = all(c["ok"] for c in checks)
        return {
            "attempt_id": attempt_id,
            "reproducible": reproducible,
            "status": attempt["status"],
            "checks": checks,
        }

    def mark_reproducible(self, attempt_id: str) -> dict[str, Any]:
        report = self.verify_attempt(attempt_id)
        if not report["reproducible"]:
            raise ReproConflict(report["checks"])
        with closing(connect(self.settings.db_path)) as conn, transaction(conn):
            conn.execute(
                "UPDATE attempts SET status='marked_reproducible' "
                "WHERE id=? AND status='succeeded'",
                (attempt_id,),
            )
        return self.verify_attempt(attempt_id)

    # ---------- 证据读取 / 修复 / 崩溃恢复 ----------

    def get_artifact_bytes(self, attempt_id: str, kind: str) -> tuple[bytes, str]:
        with closing(connect(self.settings.db_path)) as conn:
            attempt = conn.execute(
                "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise KeyError(attempt_id)
            col = attempt["result_artifact_id"] if kind == "result" else attempt[
                "error_artifact_id"
            ]
            if not col:
                raise KeyError(f"attempt 没有 {kind} 产物")
            art = conn.execute(
                "SELECT * FROM artifacts WHERE id = ?", (col,)
            ).fetchone()
        return self.store.get(art["sha256"]), art["sha256"]

    def repair_evidence(self, digest: str, raw: bytes) -> dict[str, Any]:
        from .crypto import sha256_bytes

        actual = sha256_bytes(raw)
        if actual != digest:
            raise ValueError(
                f"修复内容的哈希 {actual} 与目标标识 {digest} 不符, 拒绝写入"
            )
        stored = self.store.put(raw, digest=digest)
        return {
            "sha256": digest,
            "repaired": stored.repaired,
            "reused": stored.reused,
            "size_bytes": stored.size,
        }

    def recover_interrupted(self) -> dict[str, Any]:
        """启动时调用: 上次运行中崩溃的尝试标记为 interrupted。"""
        with closing(connect(self.settings.db_path)) as conn, transaction(conn):
            rows = conn.execute(
                "SELECT id FROM attempts WHERE status='running'"
            ).fetchall()
            for r in rows:
                conn.execute(
                    "UPDATE attempts SET status='interrupted', finished_at=?, "
                    "error_code='interrupted_on_restart', "
                    "error_message='服务重启时该尝试仍在运行, 判定为中断' WHERE id=?",
                    (utcnow(), r["id"]),
                )
        return {"recovered": len(rows), "attempt_ids": [r["id"] for r in rows]}
