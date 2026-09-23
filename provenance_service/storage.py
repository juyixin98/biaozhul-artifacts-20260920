"""SQLite-backed append-only evidence store with Ed25519 receipt signatures."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager

from .crypto import (
    canonical_json,
    generate_private_key,
    json_digest,
    load_private_key,
    public_key_hex,
    save_private_key,
    sign_message,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    status          TEXT NOT NULL,
    verdict         TEXT NOT NULL,
    created_at      REAL NOT NULL,
    config_json     TEXT NOT NULL,
    package_zip     BLOB NOT NULL,
    compiler_output TEXT NOT NULL,
    expected_rt     TEXT,
    result_json     TEXT NOT NULL,
    payload_digest  TEXT NOT NULL,
    chain_head      TEXT,
    signature       TEXT NOT NULL,
    public_key      TEXT NOT NULL,
    prev_chain      TEXT
);
CREATE TABLE IF NOT EXISTS findings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL REFERENCES jobs(job_id),
    contract    TEXT,
    code        TEXT NOT NULL,
    severity    TEXT NOT NULL,
    message     TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chain_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    head TEXT
);
"""


class Store:
    def __init__(self, db_path: str, key_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

        if os.path.exists(key_path):
            self.key = load_private_key(key_path)
        else:
            os.makedirs(os.path.dirname(os.path.abspath(key_path)), exist_ok=True)
            self.key = generate_private_key()
            save_private_key(self.key, key_path)
            os.chmod(key_path, 0o600)
        self.public_key = public_key_hex(self.key)

    @contextmanager
    def tx(self):
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def save_job(self, *, package_bytes: bytes, config: dict, compiler_output: dict,
                 expected_rt: dict | None, result: dict) -> dict:
        payload_digest = result["evidence"]["payload_digest_keccak256"]
        prev = self._get_head()
        receipt = {
            "job_id": result["job_id"],
            "payload_digest": payload_digest,
            "prev_chain": prev,
            "public_key": self.public_key,
        }
        signature = sign_message(self.key, canonical_json(receipt))
        receipt["signature"] = signature.hex()
        new_head = json_digest(receipt)

        with self.tx() as conn:
            conn.execute(
                """INSERT INTO jobs(job_id,status,verdict,created_at,config_json,
                       package_zip,compiler_output,expected_rt,result_json,
                       payload_digest,chain_head,signature,public_key,prev_chain)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (result["job_id"], result["status"], result["verdict"],
                 result["submitted_at"], json.dumps(config, sort_keys=True),
                 package_bytes, json.dumps(compiler_output, sort_keys=True),
                 json.dumps(expected_rt, sort_keys=True) if expected_rt else None,
                 json.dumps(result, sort_keys=True), payload_digest, new_head,
                 signature.hex(), self.public_key, prev),
            )
            for c in result.get("contracts", []):
                for f in c.get("findings", []):
                    conn.execute(
                        """INSERT INTO findings(job_id,contract,code,severity,
                               message,detail_json) VALUES(?,?,?,?,?,?)""",
                        (result["job_id"], c.get("fqn"), f["code"],
                         f["severity"], f["message"],
                         json.dumps(f.get("detail", {}), sort_keys=True)),
                    )
            for w in result.get("warnings", []):
                conn.execute(
                    """INSERT INTO findings(job_id,contract,code,severity,
                           message,detail_json) VALUES(?,?,?,?,?,?)""",
                    (result["job_id"], None, w["code"], w["severity"],
                     w["message"], json.dumps(w.get("detail", {}), sort_keys=True)),
                )
            for f in result.get("package_findings", []):
                conn.execute(
                    """INSERT INTO findings(job_id,contract,code,severity,
                           message,detail_json) VALUES(?,?,?,?,?,?)""",
                    (result["job_id"], None, f["code"], f["severity"],
                     f["message"], json.dumps(f.get("detail", {}), sort_keys=True)),
                )
            conn.execute("INSERT OR REPLACE INTO chain_meta(id,head) VALUES(1,?)",
                         (new_head,))
        result["receipt"] = receipt
        result["evidence"]["receipt_chain_head"] = new_head
        return result

    def _get_head(self) -> str | None:
        row = self._conn.execute("SELECT head FROM chain_meta WHERE id=1").fetchone()
        return row["head"] if row else None

    def get_job(self, job_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return None
        return json.loads(row["result_json"])

    def get_receipt(self, job_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT job_id,payload_digest,prev_chain,chain_head,signature,"
            "public_key,status,verdict FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return None
        return dict(row)

    def list_jobs(self, limit: int = 100) -> list[dict]:
        rows = self._conn.execute(
            "SELECT job_id,status,verdict,created_at,payload_digest,chain_head "
            "FROM jobs ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def list_findings(self, job_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT contract,code,severity,message,detail_json FROM findings "
            "WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
        return [{"contract": r["contract"], "code": r["code"],
                 "severity": r["severity"], "message": r["message"],
                 "detail": json.loads(r["detail_json"])} for r in rows]

    def chain_head(self) -> str | None:
        return self._get_head()
