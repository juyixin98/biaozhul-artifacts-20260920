"""SQLite persistence: devices, signed versions with validity intervals, and
an append-only history of every conversion showing which version was used."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path

from .analyzer import AnalysisResult, Segment
from .crypto import Signer

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id   TEXT PRIMARY KEY,
    modulus     REAL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    version_id     TEXT PRIMARY KEY,
    device_id      TEXT NOT NULL REFERENCES devices(device_id),
    segment_id     INTEGER NOT NULL,
    status         TEXT NOT NULL,
    model_json     TEXT NOT NULL,
    valid_from     REAL NOT NULL,
    valid_to       REAL,
    published_at   REAL NOT NULL,
    superseded_by  TEXT,
    envelope_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_versions_device ON versions(device_id);
CREATE TABLE IF NOT EXISTS conversions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id    TEXT NOT NULL,
    version_id   TEXT NOT NULL,
    epoch        INTEGER NOT NULL,
    counter      REAL NOT NULL,
    host_point   REAL,
    host_lower   REAL,
    host_upper   REAL,
    warning      TEXT,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_conversions_device ON conversions(device_id);
"""


class Store:
    def __init__(self, path: str | Path, signer: Signer):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self.signer = signer

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    def upsert_device(self, device_id: str, modulus: float | None, now: float):
        with self._lock:
            row = self._conn.execute(
                "SELECT device_id FROM devices WHERE device_id=?", (device_id,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO devices(device_id,modulus,created_at,updated_at)"
                    " VALUES(?,?,?,?)",
                    (device_id, modulus, now, now),
                )
            else:
                self._conn.execute(
                    "UPDATE devices SET modulus=?, updated_at=? WHERE device_id=?",
                    (modulus, now, device_id),
                )
            self._conn.commit()

    # ------------------------------------------------------------------
    def publish_from_analysis(
        self,
        analysis: AnalysisResult,
        *,
        now: float,
        valid_from: float | None = None,
        valid_to: float | None = None,
        force: bool = False,
    ) -> dict:
        if analysis.recommended_segment_id is None:
            raise ValueError("analysis has no feasible segment to publish")
        seg = next(
            s
            for s in analysis.segments
            if s.segment_id == analysis.recommended_segment_id
        )
        if analysis.status != "ok" and not force:
            raise ValueError(
                f"analysis status is {analysis.status!r}; refusing to publish "
                "a non-confident calibration without force=True"
            )
        vf = now if valid_from is None else valid_from
        if valid_to is not None and valid_to <= vf:
            raise ValueError("valid_to must be later than valid_from")

        model = self._model_from_segment(seg, analysis)
        version_id = uuid.uuid4().hex[:16]
        payload = {
            "version_id": version_id,
            "device_id": analysis.device_id,
            "published_at": now,
            "valid_from": vf,
            "valid_to": valid_to,
            "status": analysis.status,
            "reasons": analysis.reasons,
            "model": model,
            "analysis": _analysis_summary(analysis),
        }
        envelope = self.signer.sign_payload(payload)

        with self._lock:
            # reject overlap with any fixed (closed) interval
            rows = self._conn.execute(
                "SELECT version_id, valid_from, valid_to FROM versions "
                "WHERE device_id=? ORDER BY published_at",
                (analysis.device_id,),
            ).fetchall()
            for r in rows:
                o_from, o_to = r["valid_from"], r["valid_to"]
                end = o_to if o_to is not None else float("inf")
                new_to = valid_to if valid_to is not None else float("inf")
                if vf < end and o_from < new_to:
                    if o_to is None and vf >= o_from:
                        # supersede the currently open version
                        self._conn.execute(
                            "UPDATE versions SET valid_to=?, superseded_by=? "
                            "WHERE version_id=?",
                            (vf, version_id, r["version_id"]),
                        )
                    else:
                        raise ValueError(
                            f"validity interval overlaps version "
                            f"{r['version_id']} [{o_from}, {o_to})"
                        )
            self._conn.execute(
                "INSERT INTO versions(version_id,device_id,segment_id,status,"
                "model_json,valid_from,valid_to,published_at,superseded_by,"
                "envelope_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id,
                    analysis.device_id,
                    seg.segment_id,
                    analysis.status,
                    json.dumps(model),
                    vf,
                    valid_to,
                    now,
                    None,
                    json.dumps(envelope),
                ),
            )
            self._conn.commit()
        return envelope

    @staticmethod
    def _model_from_segment(seg: Segment, analysis: AnalysisResult) -> dict:
        f = seg.fit
        c = f._c.tolist()
        return {
            "epoch": seg.epoch,
            "counter_start": seg.counter_start,
            "modulus": seg.modulus,
            # unwrapped x = unwrap_base + raw_counter - counter_start
            "unwrap_base": float(c[0]),
            "counter_fitted_min": float(c[0]),
            "counter_fitted_max": float(c[-1]),
            "host_valid_from": seg.host_valid_from,
            "host_valid_to": seg.host_valid_to,
            "n_used": f.n_used,
            "alpha_point": float(f.alpha_point),
            "beta_point": float(f.beta_point),
            "beta_lower": float(f.beta_lo),
            "beta_upper": float(f.beta_hi),
            "interval_counters": c,
            "interval_lower": f._lo.tolist(),
            "interval_upper": f._hi.tolist(),
        }

    # ------------------------------------------------------------------
    def list_versions(self, device_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT envelope_json FROM versions WHERE device_id=? "
                "ORDER BY published_at",
                (device_id,),
            ).fetchall()
        return [json.loads(r["envelope_json"]) for r in rows]

    def get_version(self, version_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT envelope_json FROM versions WHERE version_id=?",
                (version_id,),
            ).fetchone()
        return json.loads(row["envelope_json"]) if row else None

    def active_version(
        self, device_id: str, at: float | None = None
    ) -> dict | None:
        """The version whose validity interval contains ``at`` (default: now)."""

        with self._lock:
            rows = self._conn.execute(
                "SELECT envelope_json FROM versions WHERE device_id=? "
                "ORDER BY published_at DESC",
                (device_id,),
            ).fetchall()
        out = None
        for r in rows:
            env = json.loads(r["envelope_json"])
            p = env["payload"]
            vf, vt = p["valid_from"], p["valid_to"]
            if vf <= at and (vt is None or at < vt):
                out = env
                break
        return out

    # ------------------------------------------------------------------
    def record_conversion(self, row: dict, now: float) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO conversions(device_id,version_id,epoch,counter,"
                "host_point,host_lower,host_upper,warning,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    row["device_id"],
                    row["version_id"],
                    row["epoch"],
                    row["counter"],
                    row["host_point"],
                    row["host_lower"],
                    row["host_upper"],
                    row.get("warning"),
                    now,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_conversions(self, device_id: str, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM conversions WHERE device_id=? "
                "ORDER BY id DESC LIMIT ?",
                (device_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def _analysis_summary(a: AnalysisResult) -> dict:
    return {
        "n_input": a.n_input,
        "n_rtt_dropped": a.n_rtt_dropped,
        "median_rtt": a.median_rtt,
        "events": [
            {"type": e.type, "at_sample_index": e.at_sample_index,
             "detail": e.detail}
            for e in a.events
        ],
        "segments": [
            {
                "segment_id": s.segment_id,
                "epoch": s.epoch,
                "n_used": s.fit.n_used,
                "feasible": s.fit.feasible,
            }
            for s in a.segments
        ],
    }
