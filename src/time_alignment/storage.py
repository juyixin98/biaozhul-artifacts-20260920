"""SQLite persistence with a tamper-evident cryptographic hash chain.

Every decision row stores:

    row_hash = HMAC_?(secret, seq || canonical_json(fields...))_or_
               SHA256(seq || canonical_json(...))
    prev_hash = previous row_hash (or "GENESIS")

The chained payload for row N is therefore::

    H( prev_hash(N-1) || canonical JSON of row N's decision fields )

Removing, reordering or altering any row breaks recomputation;
``verify_chain()`` reports exactly the first offending row. With an HMAC
secret configured, the chain additionally authenticates the writer (an
attacker without the secret cannot forge a valid row).

All crypto here is real and executed by :mod:`hashlib` / :mod:`hmac`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from dataclasses import asdict
from typing import Any

from .types import Decision, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS config_versions (
    version            INTEGER PRIMARY KEY,
    created_recv_ns    INTEGER NOT NULL,
    params             TEXT NOT NULL,
    note               TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS epochs (
    epoch_id        INTEGER PRIMARY KEY,
    opened_recv_ns  INTEGER NOT NULL,
    closed_recv_ns  INTEGER,
    reason          TEXT NOT NULL DEFAULT 'init'
);
CREATE TABLE IF NOT EXISTS decisions (
    seq               INTEGER PRIMARY KEY AUTOINCREMENT,
    epoch_id          INTEGER NOT NULL,
    status            TEXT NOT NULL,
    config_version    INTEGER NOT NULL,
    camera_seq        INTEGER,
    camera_stamp_ns   INTEGER,
    imu_seq           INTEGER,
    imu_stamp_ns      INTEGER,
    dt_ns             INTEGER,
    abs_dt_ns         INTEGER,
    tie               INTEGER NOT NULL,
    within_tolerance  INTEGER,
    forced_eviction   INTEGER NOT NULL,
    reason            TEXT NOT NULL,
    candidates        TEXT NOT NULL,
    camera_payload_hash TEXT,
    imu_payload_hash  TEXT,
    event_recv_ns     INTEGER,
    settled_recv_ns   INTEGER NOT NULL,
    prev_hash         TEXT NOT NULL,
    row_hash          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_epoch ON decisions(epoch_id);
CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status);
"""

GENESIS = "GENESIS"


class ChainVerifyError(Exception):
    def __init__(self, index: int, message: str):
        super().__init__(f"chain verification failed at row {index}: {message}")
        self.index = index
        self.message = message


class Storage:
    """Single-writer SQLite storage. One thread at a time (the engine)."""

    def __init__(self, path: str, hmac_secret: bytes | str | None = None):
        # ``:memory:`` is shared via a single connection held open.
        self._conn = sqlite3.connect(
            path if path != ":memory:" else "",
            uri=False,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        if isinstance(hmac_secret, str):
            hmac_secret = hmac_secret.encode("utf-8")
        self._secret = hmac_secret
        self._path = path
        self._set_meta("format_version", "1")
        self._set_meta(
            "chain_alg",
            "HMAC-SHA256" if hmac_secret is not None else "SHA256",
        )

    # ---------------------------------------------------------------- meta
    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._conn.commit()

    def get_meta(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self._conn.execute("SELECT key,value FROM meta")}

    # ------------------------------------------------------------- epochs
    def open_epoch(self, epoch_id: int, recv_ns: int, reason: str) -> None:
        self._conn.execute(
            "INSERT INTO epochs(epoch_id,opened_recv_ns,reason) VALUES(?,?,?)",
            (epoch_id, recv_ns, reason),
        )
        self._conn.commit()

    def close_epoch(self, epoch_id: int, recv_ns: int) -> None:
        self._conn.execute(
            "UPDATE epochs SET closed_recv_ns=? WHERE epoch_id=?",
            (recv_ns, epoch_id),
        )
        self._conn.commit()

    def list_epochs(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM epochs ORDER BY epoch_id")]

    # ------------------------------------------------------------ configs
    def record_config(self, version: int, recv_ns: int, params: dict[str, Any], note: str) -> None:
        self._conn.execute(
            "INSERT INTO config_versions(version,created_recv_ns,params,note) VALUES(?,?,?,?)",
            (version, recv_ns, json.dumps(params, sort_keys=True, separators=(",", ":")), note),
        )
        self._conn.commit()

    def list_configs(self) -> list[dict[str, Any]]:
        rows = [dict(r) for r in self._conn.execute(
            "SELECT * FROM config_versions ORDER BY version")]
        for r in rows:
            r["params"] = json.loads(r["params"])
        return rows

    # ------------------------------------------------------------- crypto
    @staticmethod
    def _canonical(seq: int, d: dict[str, Any]) -> bytes:
        """Canonical serialization of one row: seq + decision fields,
        sorted keys, no whitespace. Stable across Python invocations."""
        payload = {"seq": seq}
        payload.update(d)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")

    def _digest(self, prev_hash: str, seq: int, fields: dict[str, Any]) -> str:
        buf = prev_hash.encode("ascii") + b"\0" + self._canonical(seq, fields)
        if self._secret is not None:
            return hmac.new(self._secret, buf, hashlib.sha256).hexdigest()
        return hashlib.sha256(buf).hexdigest()

    # ------------------------------------------------------------ records
    @staticmethod
    def _decision_fields(d: Decision) -> dict[str, Any]:
        out = asdict(d)
        out["status"] = d.status.value if isinstance(d.status, Status) else d.status
        out["candidates"] = json.dumps(d.candidates, sort_keys=True,
                                       separators=(",", ":"))
        out["tie"] = 1 if d.tie else 0
        out["within_tolerance"] = (None if d.within_tolerance is None
                                   else 1 if d.within_tolerance else 0)
        out["forced_eviction"] = 1 if d.forced_eviction else 0
        return out

    def record_decision(self, d: Decision) -> int:
        """Append one decision, extending the hash chain atomically.
        Returns the row sequence number."""
        fields = self._decision_fields(d)
        cur = self._conn.execute("SELECT seq,row_hash FROM decisions ORDER BY seq DESC LIMIT 1")
        last = cur.fetchone()
        if last is None:
            seq, prev_hash = 1, GENESIS
        else:
            seq, prev_hash = last["seq"] + 1, last["row_hash"]
        row_hash = self._digest(prev_hash, seq, fields)
        self._conn.execute(
            """INSERT INTO decisions(
                 seq,epoch_id,status,config_version,camera_seq,camera_stamp_ns,
                 imu_seq,imu_stamp_ns,dt_ns,abs_dt_ns,tie,within_tolerance,
                 forced_eviction,reason,candidates,camera_payload_hash,
                 imu_payload_hash,event_recv_ns,settled_recv_ns,prev_hash,row_hash)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                seq, d.epoch_id,
                fields["status"], d.config_version,
                d.camera_seq, d.camera_stamp_ns, d.imu_seq, d.imu_stamp_ns,
                d.dt_ns, d.abs_dt_ns, fields["tie"], fields["within_tolerance"],
                fields["forced_eviction"], d.reason, fields["candidates"],
                d.camera_payload_hash, d.imu_payload_hash,
                d.event_recv_ns, d.settled_recv_ns, prev_hash, row_hash,
            ),
        )
        self._conn.commit()
        return seq

    def all_decisions(self) -> list[dict[str, Any]]:
        rows = [dict(r) for r in self._conn.execute("SELECT * FROM decisions ORDER BY seq")]
        for r in rows:
            r["tie"] = bool(r["tie"])
            r["forced_eviction"] = bool(r["forced_eviction"])
            if r["within_tolerance"] is not None:
                r["within_tolerance"] = bool(r["within_tolerance"])
            r["candidates"] = json.loads(r["candidates"])
        return rows

    def count_decisions(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) c FROM decisions").fetchone()["c"])

    # ------------------------------------------------------------ verify
    def verify_chain(self) -> dict[str, Any]:
        """Recompute the whole chain and check invariants.

        Raises ChainVerifyError on the first broken row. Returns a summary
        dict when everything is valid.
        """
        alg = "HMAC-SHA256" if self._secret is not None else "SHA256"
        meta = self.get_meta()
        if meta.get("chain_alg") != alg:
            raise ChainVerifyError(0, f"chain_alg meta {meta.get('chain_alg')!r} "
                                     f"does not match opener {alg!r} (wrong secret?)")
        rows = list(self._conn.execute("SELECT * FROM decisions ORDER BY seq"))
        prev_hash = GENESIS
        for i, row in enumerate(rows, start=1):
            if row["seq"] != i:
                raise ChainVerifyError(i, f"gap or reorder: seq={row['seq']} expected {i}")
            if row["prev_hash"] != prev_hash:
                raise ChainVerifyError(i, "prev_hash link broken (row deleted or reordered?)")
            d = dict(row)
            stored_hash = d.pop("row_hash")
            d.pop("prev_hash")
            d.pop("seq")
            d["tie"] = 1 if d["tie"] else 0
            d["forced_eviction"] = 1 if d["forced_eviction"] else 0
            if d["within_tolerance"] is not None:
                d["within_tolerance"] = 1 if d["within_tolerance"] else 0
            calc = self._digest(prev_hash, i, d)
            if not hmac.compare_digest(calc, stored_hash):
                raise ChainVerifyError(i, "row_hash mismatch (payload tampered or "
                                          "wrong HMAC secret)")
            prev_hash = stored_hash
        # Exclusive-policy invariant (checked from data only): a single IMU
        # must never appear matched to more than one camera.
        seen: dict[int, int] = {}
        for row in rows:
            if row["status"] == Status.MATCHED.value and row["imu_seq"] is not None:
                first = seen.get(row["imu_seq"])
                if first is not None:
                    raise ChainVerifyError(
                        row["seq"],
                        f"imu_seq {row['imu_seq']} matched twice (rows {first},{row['seq']}); "
                        "exclusive policy violated")
                seen[row["imu_seq"]] = row["seq"]
        return {"ok": True, "rows": len(rows), "algorithm": alg, "tail": prev_hash}

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def open_default(path: str) -> Storage:
    """Open storage using the HMAC secret from the environment if present."""
    secret = os.environ.get("TIME_ALIGNMENT_HMAC_SECRET")
    return Storage(path, hmac_secret=secret)
