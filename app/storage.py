"""SQLite 持久化与哈希链证据。

两张表：
- ``jobs``     每次核验请求（输入摘要、裁决、完整报告 JSON、原始输入落盘路径）；
- ``evidence`` 报告中每条检查一条证据，包含 ``prev_hash/self_hash`` 哈希链。

原始输入（config/compilerOutput/源码包字节/目标字节码）落盘保存，
任何人可用 GET 返回的字段离线复验整条链。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any

from .service import canonical_json

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    contract_source TEXT NOT NULL,
    contract_name TEXT NOT NULL,
    verdict TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    artifact_dir TEXT NOT NULL,
    report_json TEXT NOT NULL,
    report_sha256 TEXT NOT NULL,
    chain_head TEXT NOT NULL,
    chain_valid INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    job_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    code TEXT NOT NULL,
    severity TEXT NOT NULL,
    check_json TEXT NOT NULL,
    check_sha256 TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    self_hash TEXT NOT NULL,
    PRIMARY KEY (job_id, seq),
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);
"""

GENESIS = "0" * 64


class Storage:
    def __init__(self, db_path: str, artifact_root: str):
        self.db_path = db_path
        self.artifact_root = artifact_root
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
        os.makedirs(artifact_root, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ #
    def save_job(
        self,
        *,
        sources: dict[str, bytes],
        config_obj: Any,
        compiler_output_obj: Any,
        target_deployed: str,
        report: dict,
        input_sha256: str,
    ) -> dict:
        """原子落盘：原始输入 + 报告 + 哈希链证据。"""
        import hashlib

        job_id = uuid.uuid4().hex
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        artifact_dir = os.path.join(self.artifact_root, job_id)
        os.makedirs(os.path.join(artifact_dir, "sources"), exist_ok=True)
        for path, data in sources.items():
            dest = os.path.join(artifact_dir, "sources", path.replace("/", "__"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(data)
        with open(os.path.join(artifact_dir, "config.json"), "wb") as fh:
            fh.write(canonical_json(config_obj))
        with open(os.path.join(artifact_dir, "compilerOutput.json"), "wb") as fh:
            fh.write(canonical_json(compiler_output_obj))
        with open(os.path.join(artifact_dir, "target_deployed.hex"), "w", encoding="ascii") as fh:
            fh.write(target_deployed)
        manifest = {
            "job_id": job_id,
            "created_at": created,
            "input_sha256": input_sha256,
            "sources": {
                path: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for path, data in sorted(sources.items())
            },
        }
        with open(os.path.join(artifact_dir, "manifest.json"), "wb") as fh:
            fh.write(canonical_json(manifest))

        report_bytes = canonical_json(report)
        report_sha = hashlib.sha256(report_bytes).hexdigest()

        rows = []
        prev = GENESIS
        for seq, check in enumerate(report["checks"]):
            check_bytes = canonical_json(check)
            self_hash = hashlib.sha256(prev.encode("ascii") + check_bytes).hexdigest()
            rows.append(
                (
                    job_id,
                    seq,
                    check["code"],
                    check["severity"],
                    check_bytes.decode("utf-8"),
                    hashlib.sha256(check_bytes).hexdigest(),
                    prev,
                    self_hash,
                )
            )
            prev = self_hash

        with self._lock:
            self._conn.execute(
                """INSERT INTO jobs(id, created_at, contract_source, contract_name, verdict,
                   input_sha256, artifact_dir, report_json, report_sha256, chain_head, chain_valid)
                   VALUES (?,?,?,?,?,?,?,?,?,?,1)""",
                (
                    job_id,
                    created,
                    report["contract"]["source"],
                    report["contract"]["name"],
                    report["verdict"],
                    input_sha256,
                    artifact_dir,
                    report_bytes.decode("utf-8"),
                    report_sha,
                    prev,
                ),
            )
            self._conn.executemany(
                """INSERT INTO evidence(job_id, seq, code, severity, check_json,
                   check_sha256, prev_hash, self_hash) VALUES (?,?,?,?,?,?,?,?)""",
                rows,
            )
            self._conn.commit()

        return {"job_id": job_id, "created_at": created, "artifact_dir": artifact_dir, "chain_head": prev}

    # ------------------------------------------------------------------ #
    def get_job(self, job_id: str) -> dict | None:
        if not _is_hex_id(job_id):
            return None
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        return dict(row)

    def get_report(self, job_id: str) -> dict | None:
        row = self.get_job(job_id)
        return json.loads(row["report_json"]) if row else None

    def get_evidence(self, job_id: str) -> list[dict] | None:
        if not _is_hex_id(job_id):
            return None
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM evidence WHERE job_id=? ORDER BY seq", (job_id,)
            ).fetchall()
        if not rows:
            return None if self.get_job(job_id) is None else []
        return [dict(r) for r in rows]

    def list_jobs(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, created_at, contract_source, contract_name, verdict,
                          input_sha256, report_sha256, chain_head, chain_valid
                   FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def verify_chain(self, job_id: str) -> dict:
        """从数据库重放哈希链，检测任何事后篡改。"""
        import hashlib

        evidence = self.get_evidence(job_id)
        job = self.get_job(job_id)
        if job is None or evidence is None:
            return {"found": False}
        prev = GENESIS
        bad: list[int] = []
        for e in evidence:
            check_bytes = e["check_json"].encode("utf-8")
            expect = hashlib.sha256(prev.encode("ascii") + check_bytes).hexdigest()
            if expect != e["self_hash"] or e["prev_hash"] != prev:
                bad.append(e["seq"])
            prev = e["self_hash"]
        report_sha = hashlib.sha256(job["report_json"].encode("utf-8")).hexdigest()
        return {
            "found": True,
            "entries": len(evidence),
            "chain_head": prev,
            "stored_head": job["chain_head"],
            "head_matches": prev == job["chain_head"],
            "report_sha256": report_sha,
            "stored_report_sha256": job["report_sha256"],
            "report_matches": report_sha == job["report_sha256"],
            "tampered_at": bad,
            "valid": not bad and prev == job["chain_head"] and report_sha == job["report_sha256"],
        }


def _is_hex_id(job_id: str) -> bool:
    return bool(job_id) and all(c in "0123456789abcdef" for c in job_id) and len(job_id) <= 64
