"""Append-only SQLite evidence log with a real HMAC-SHA256 hash chain.

For every record *r*::

    body   = "|".join([str(seq), kind, str(epoch), str(params_version),
                       canonical_json(payload)])
    digest = sha256(body).hexdigest()
    mac_0  = HMAC-SHA256(key, digest_hex)
    mac_n  = HMAC-SHA256(key, mac_{n-1} || digest_hex)

A single flipped byte anywhere in a row (or a re-ordered row) breaks both the
row digest and every subsequent MAC, and is detected by :meth:`verify`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .matcher import Outcome
from .model import Record, RecordKind

SCHEMA_VERSION = 1
_GENESIS_MAC = b""  # first row chains over the empty prefix


def canonical_payload(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def signed_body(seq: int, kind: str, epoch: int, params_version: int,
                payload_json: str) -> bytes:
    return "|".join([str(seq), kind, str(epoch), str(params_version),
                     payload_json]).encode("utf-8")


def load_key(path: str | Path) -> bytes:
    """Load an HMAC key from file.

    Accepts either 64 hex characters (as produced by ``cli genkey``) or raw
    high-entropy bytes; exactly one trailing newline is stripped.
    """
    data = Path(path).read_bytes().strip()
    try:
        text = data.decode("ascii")
        if len(text) in (32, 64) and all(c in "0123456789abcdefABCDEF"
                                         for c in text):
            data = bytes.fromhex(text)
    except (UnicodeDecodeError, ValueError):
        pass
    if len(data) < 16:
        raise ValueError("HMAC key too short: require >= 16 bytes (got "
                         f"{len(data)})")
    return data


@dataclass
class VerifyReport:
    ok: bool
    total: int
    first_error: Optional[str] = None
    counts: dict[str, int] = field(default_factory=dict)


class EvidenceStore:
    def __init__(self, db_path: str | Path, key: bytes, *,
                 key_id: str = "default") -> None:
        if len(key) < 16:
            raise ValueError("HMAC key too short: require >= 16 bytes")
        self.db_path = str(db_path)
        self._key = key
        self._key_id = key_id
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        row = self._conn.execute(
            "SELECT MAX(seq) AS s FROM records").fetchone()
        self._next_seq = (row["s"] or 0) + 1
        last = self._conn.execute(
            "SELECT mac FROM records ORDER BY seq DESC LIMIT 1").fetchone()
        self._prev_mac = bytes.fromhex(last["mac"]) if last else _GENESIS_MAC

    # ----------------------------------------------------------- schema

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                name  TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS records (
                seq            INTEGER PRIMARY KEY,
                kind           TEXT NOT NULL,
                epoch          INTEGER NOT NULL,
                params_version INTEGER NOT NULL,
                payload        TEXT NOT NULL,
                digest         TEXT NOT NULL,
                mac            TEXT NOT NULL,
                created_ns     INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_records_kind  ON records(kind);
            CREATE INDEX IF NOT EXISTS idx_records_epoch ON records(epoch);
            """)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(name, value) VALUES('schema', ?)",
            (str(SCHEMA_VERSION),))
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(name, value) VALUES('key_id', ?)",
            (self._key_id,))
        self._conn.commit()

    # ----------------------------------------------------------- write

    def append_outcome(self, outcome: Outcome) -> Record:
        rec = self._build_record(outcome.kind, outcome.epoch,
                                 outcome.params_version, outcome.payload)
        self._insert([rec])
        return rec

    def append_many(self, outcomes: Iterable[Outcome]) -> list[Record]:
        records = [self._build_record(o.kind, o.epoch, o.params_version,
                                      o.payload) for o in outcomes]
        if records:
            self._insert(records)
        return records

    def _build_record(self, kind: RecordKind, epoch: int,
                      params_version: int, payload: dict) -> Record:
        seq = self._next_seq
        pj = canonical_payload(payload)
        body = signed_body(seq, kind.value, epoch, params_version, pj)
        digest = hashlib.sha256(body).hexdigest()
        mac = hmac.new(self._key,
                       self._prev_mac + digest.encode("ascii"),
                       hashlib.sha256).hexdigest()
        self._next_seq += 1
        self._prev_mac = bytes.fromhex(mac)
        return Record(seq=seq, kind=kind, epoch=epoch,
                      params_version=params_version, payload=payload,
                      digest=digest, mac=mac)

    def _insert(self, records: list[Record]) -> None:
        now = time.time_ns()
        with self._conn:
            for r in records:
                pj = canonical_payload(r.payload)
                self._conn.execute(
                    "INSERT INTO records(seq, kind, epoch, params_version, "
                    "payload, digest, mac, created_ns) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (r.seq, r.kind.value, r.epoch, r.params_version, pj,
                     r.digest, r.mac, now))

    # ----------------------------------------------------------- read

    def fetch_records(self, *, kind: Optional[RecordKind] = None
                      ) -> list[sqlite3.Row]:
        if kind is None:
            cur = self._conn.execute(
                "SELECT * FROM records ORDER BY seq")
        else:
            cur = self._conn.execute(
                "SELECT * FROM records WHERE kind=? ORDER BY seq", (kind.value,))
        return cur.fetchall()

    def fetch_pairs(self, *, epoch: Optional[int] = None,
                    status: Optional[str] = None) -> list[dict]:
        rows = self.fetch_records(kind=RecordKind.PAIR)
        out = []
        for r in rows:
            p = json.loads(r["payload"])
            if epoch is not None and r["epoch"] != epoch:
                continue
            if status is not None and p.get("status") != status:
                continue
            out.append({"seq": r["seq"], "epoch": r["epoch"],
                        "params_version": r["params_version"], **p})
        return out

    def fetch_imus(self) -> list[dict]:
        return [{"seq": r["seq"], "epoch": r["epoch"],
                 "params_version": r["params_version"], **json.loads(r["payload"])}
                for r in self.fetch_records(kind=RecordKind.IMU)]

    def export_jsonl(self, path: str | Path) -> int:
        n = 0
        with open(path, "w", encoding="utf-8") as f:
            for r in self.fetch_records():
                f.write(json.dumps({
                    "seq": r["seq"], "kind": r["kind"],
                    "epoch": r["epoch"],
                    "params_version": r["params_version"],
                    "payload": json.loads(r["payload"]),
                    "digest": r["digest"], "mac": r["mac"],
                    "created_ns": r["created_ns"],
                }, ensure_ascii=False) + "\n")
                n += 1
        return n

    @property
    def count(self) -> int:
        return self._next_seq - 1

    # ----------------------------------------------------------- verify

    def verify(self) -> VerifyReport:
        """Recompute every digest and MAC; report first tamper if any."""
        rows = self.fetch_records()
        counts: dict[str, int] = {}
        prev_mac = _GENESIS_MAC
        expected_seq = 1
        for r in rows:
            counts[r["kind"]] = counts.get(r["kind"], 0) + 1
            where = f"seq={r['seq']}"
            if r["seq"] != expected_seq:
                return VerifyReport(False, len(rows),
                                    f"sequence gap at {where}: expected "
                                    f"{expected_seq}", counts)
            pj_stored = r["payload"]
            # Round-trip guarantees the stored TEXT really is canonical JSON.
            try:
                pj = canonical_payload(json.loads(pj_stored))
            except json.JSONDecodeError as e:
                return VerifyReport(False, len(rows),
                                    f"invalid JSON at {where}: {e}", counts)
            if pj != pj_stored:
                return VerifyReport(False, len(rows),
                                    f"non-canonical/altered payload at {where}",
                                    counts)
            body = signed_body(r["seq"], r["kind"], r["epoch"],
                               r["params_version"], pj)
            digest = hashlib.sha256(body).hexdigest()
            if not hmac.compare_digest(digest, r["digest"]):
                return VerifyReport(False, len(rows),
                                    f"digest mismatch at {where}", counts)
            mac = hmac.new(self._key,
                           prev_mac + digest.encode("ascii"),
                           hashlib.sha256).hexdigest()
            if not hmac.compare_digest(mac, r["mac"]):
                return VerifyReport(False, len(rows),
                                    f"HMAC chain broken at {where}", counts)
            prev_mac = bytes.fromhex(mac)
            expected_seq += 1
        return VerifyReport(True, len(rows), None, counts)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "EvidenceStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
