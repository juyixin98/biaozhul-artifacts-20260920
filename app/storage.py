"""SQLite-backed storage plus the immutable evidence store on disk.

Evidence layout (nothing is ever overwritten)::

    <root>/
      signer_key.pem
      state.db
      evidence/
        bags/<sha256>.bin                # content-addressed input bags
        jobs/<job_id>/result.json        # one fresh directory per attempt
        indexes/index-<n>.json           # append-only published indices

Concurrency: a single SQLite connection guarded by an ``RLock``. All API work
that mutates state takes the lock only for the short database transaction; the
numeric pipeline runs outside the lock.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .compute import (
    render_artifact,
    run_pipeline,
    write_artifact_atomic,
)
from .crypto import Signer, canonical_json, digest_json, sha256_bytes, sha256_file


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class StorageError(Exception):
    pass


class NotFound(StorageError):
    pass


class Conflict(StorageError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS bags (
    digest TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    summary_json TEXT NOT NULL,
    uploaded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calibrations (
    id TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    calibration_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    bag_digest TEXT NOT NULL,
    bag_summary_json TEXT NOT NULL,
    params_json TEXT NOT NULL,
    calibration_id TEXT NOT NULL,
    calibration_json TEXT NOT NULL,
    calibration_digest TEXT NOT NULL,
    algorithm_name TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    signature TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    attempt_index INTEGER NOT NULL,
    status TEXT NOT NULL,
    seed INTEGER NOT NULL,
    input_digest TEXT,
    input_digest_end TEXT,
    output_path TEXT,
    output_digest TEXT,
    results_json TEXT,
    params_digest_start TEXT,
    params_digest_end TEXT,
    reproducible INTEGER NOT NULL DEFAULT 0,
    failures_json TEXT NOT NULL DEFAULT '[]',
    tamper_json TEXT NOT NULL DEFAULT '[]',
    error TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(snapshot_id, attempt_index)
);
CREATE TABLE IF NOT EXISTS indexes (
    id TEXT PRIMARY KEY,
    index_number INTEGER NOT NULL UNIQUE,
    published_at TEXT NOT NULL,
    path TEXT NOT NULL,
    digest TEXT NOT NULL,
    signature TEXT NOT NULL,
    entry_count INTEGER NOT NULL
);
"""


class Storage:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.evidence = self.root / "evidence"
        self.bags_dir = self.evidence / "bags"
        self.jobs_dir = self.evidence / "jobs"
        self.indexes_dir = self.evidence / "indexes"
        for d in (self.bags_dir, self.jobs_dir, self.indexes_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "state.db"
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.execute("PRAGMA synchronous=FULL;")
        self._conn.executescript(SCHEMA)
        self.signer = Signer(self.root / "signer_key.pem")
        self.signer.load_or_create()

    # ---------------------------------------------------------------- helpers
    def _bag_path(self, digest: str) -> Path:
        return self.bags_dir / f"{digest}.bin"

    def _fetchone(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()

    @staticmethod
    def _job_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "snapshot_id": row["snapshot_id"],
            "attempt_index": row["attempt_index"],
            "status": row["status"],
            "seed": row["seed"],
            "input_digest": row["input_digest"],
            "input_digest_end": row["input_digest_end"],
            "output_path": row["output_path"],
            "output_digest": row["output_digest"],
            "results_summary": json.loads(row["results_json"]) if row["results_json"] else None,
            "params_digest_start": row["params_digest_start"],
            "params_digest_end": row["params_digest_end"],
            "reproducible": bool(row["reproducible"]),
            "reproducibility_failures": json.loads(row["failures_json"]),
            "tamper_events": json.loads(row["tamper_json"]),
            "error": row["error"],
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
        }

    # ------------------------------------------------------------- lifecycle
    def recover_after_restart(self) -> list[str]:
        """Mark jobs interrupted mid-run by a restart as failed (non-reproducible).

        A 'running' job never produced a validated output, so it cannot be
        reproducible. Returns recovered job ids.
        """
        recovered: list[str] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, tamper_json FROM jobs WHERE status='running'"
            ).fetchall()
            for r in rows:
                events = json.loads(r["tamper_json"])
                events.append(
                    f"{utcnow()} process_restart: job was running when service restarted; "
                    "no validated output"
                )
                self._conn.execute(
                    """UPDATE jobs SET status='failed', reproducible=0,
                       failures_json=?, tamper_json=?,
                       error='interrupted by service restart before output validation',
                       completed_at=? WHERE id=?""",
                    (
                        json.dumps(["job_not_completed", "recovered_after_restart"]),
                        json.dumps(events),
                        utcnow(),
                        r["id"],
                    ),
                )
                recovered.append(r["id"])
        return recovered

    # ------------------------------------------------------------------ bags
    def add_bag(self, data: bytes, summary: dict[str, Any]) -> dict[str, Any]:
        digest = sha256_bytes(data)
        path = self._bag_path(digest)
        with self._lock:
            existing = self._fetchone("SELECT digest, size FROM bags WHERE digest=?", (digest,))
            if existing:
                # Content addressing: same digest must mean identical bytes.
                if not path.exists() or sha256_file(path) != digest:
                    raise Conflict("stored bag with same digest has different content")
                size = existing["size"]
            else:
                tmp = path.with_name(f".{digest}.bin.tmp-{os.getpid()}")
                with open(tmp, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
                self._conn.execute(
                    "INSERT INTO bags(digest,size,summary_json,uploaded_at) VALUES(?,?,?,?)",
                    (digest, len(data), canonical_json(summary).decode(), utcnow()),
                )
                size = len(data)
        return {"digest": digest, "size": size, "summary": summary}

    def list_bags(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT digest, size, summary_json, uploaded_at FROM bags ORDER BY uploaded_at"
        ).fetchall()
        return [
            {"digest": r["digest"], "size": r["size"],
             "summary": json.loads(r["summary_json"]), "uploaded_at": r["uploaded_at"]}
            for r in rows
        ]

    def get_bag_bytes(self, digest: str) -> bytes:
        path = self._bag_path(digest)
        row = self._fetchone("SELECT digest FROM bags WHERE digest=?", (digest,))
        if not row:
            raise NotFound(f"unknown bag {digest}")
        if not path.exists():
            raise StorageError(f"bag evidence missing on disk: {path}")
        return path.read_bytes()

    # ----------------------------------------------------------- calibrations
    def add_calibration(self, calib: dict[str, Any]) -> dict[str, Any]:
        digest = digest_json(calib)
        with self._lock:
            row = self._fetchone("SELECT id, created_at FROM calibrations WHERE id=?", (digest,))
            if row:
                created_at = row["created_at"]
            else:
                created_at = utcnow()
                self._conn.execute(
                    "INSERT INTO calibrations(id,digest,calibration_json,created_at) VALUES(?,?,?,?)",
                    (digest, digest, canonical_json(calib).decode(), created_at),
                )
        return {"id": digest, "digest": digest, "calibration": calib,
                "created_at": created_at}

    def list_calibrations(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, digest, calibration_json, created_at FROM calibrations ORDER BY created_at"
        ).fetchall()
        return [
            {"id": r["id"], "digest": r["digest"],
             "calibration": json.loads(r["calibration_json"]), "created_at": r["created_at"]}
            for r in rows
        ]

    def get_calibration(self, calib_id: str) -> dict[str, Any]:
        r = self._fetchone(
            "SELECT id, digest, calibration_json, created_at FROM calibrations WHERE id=?",
            (calib_id,),
        )
        if not r:
            raise NotFound(f"unknown calibration {calib_id}")
        return {"id": r["id"], "digest": r["digest"],
                "calibration": json.loads(r["calibration_json"]), "created_at": r["created_at"]}

    # -------------------------------------------------------------- snapshots
    def create_snapshot(self, req: dict[str, Any]) -> dict[str, Any]:
        bag_digest = req["bag_digest"]
        bag_row = self._fetchone(
            "SELECT summary_json FROM bags WHERE digest=?", (bag_digest,)
        )
        if not bag_row:
            raise NotFound(f"unknown bag {bag_digest}; upload it first")
        bag_summary = json.loads(bag_row["summary_json"])
        calib = self.get_calibration(req["calibration_id"])

        manifest = {
            "bag_digest": bag_digest,
            "bag_summary": bag_summary,
            "params": req["params"],
            "calibration_digest": calib["digest"],
            "calibration": calib["calibration"],
            "algorithm": {
                "name": req["algorithm_name"],
                "version": req["algorithm_version"],
            },
        }
        snap_id = digest_json(manifest)
        signature = self.signer.sign_json(manifest)
        created_at = utcnow()
        with self._lock:
            existing = self._fetchone("SELECT id FROM snapshots WHERE id=?", (snap_id,))
            if not existing:
                self._conn.execute(
                    """INSERT INTO snapshots(id,created_at,bag_digest,bag_summary_json,params_json,
                       calibration_id,calibration_json,calibration_digest,algorithm_name,
                       algorithm_version,manifest_json,signature)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        snap_id, created_at, bag_digest, canonical_json(bag_summary).decode(),
                        canonical_json(req["params"]).decode(), calib["id"],
                        canonical_json(calib["calibration"]).decode(), calib["digest"],
                        req["algorithm_name"], req["algorithm_version"],
                        canonical_json(manifest).decode(), signature,
                    ),
                )
        return self.get_snapshot(snap_id)

    def _snapshot_row(self, snap_id: str) -> sqlite3.Row:
        r = self._fetchone("SELECT * FROM snapshots WHERE id=?", (snap_id,))
        if not r:
            raise NotFound(f"unknown snapshot {snap_id}")
        return r

    def get_snapshot(self, snap_id: str) -> dict[str, Any]:
        r = self._snapshot_row(snap_id)
        return {
            "id": r["id"],
            "created_at": r["created_at"],
            "bag_digest": r["bag_digest"],
            "bag_summary": json.loads(r["bag_summary_json"]),
            "params": json.loads(r["params_json"]),
            "calibration": json.loads(r["calibration_json"]),
            "calibration_digest": r["calibration_digest"],
            "algorithm": {"name": r["algorithm_name"], "version": r["algorithm_version"]},
            "manifest": json.loads(r["manifest_json"]),
            "signature": r["signature"],
        }

    def list_snapshots(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT id FROM snapshots ORDER BY created_at").fetchall()
        return [self.get_snapshot(r["id"]) for r in rows]

    def snapshot_signature_valid(self, snap: dict[str, Any]) -> bool:
        return self.signer.verify_json_signature(snap["manifest"], snap["signature"])

    # ------------------------------------------------------------------- jobs
    @staticmethod
    def _compute_input_digest(
        *, snapshot_id: str, bag_digest: str, params_digest: str,
        calibration_digest: str, algorithm: dict[str, str], seed: int,
    ) -> str:
        return digest_json({
            "kind": "experiment_input",
            "snapshot_id": snapshot_id,
            "bag_digest": bag_digest,
            "params_digest": params_digest,
            "calibration_digest": calibration_digest,
            "algorithm": algorithm,
            "seed": seed,
        })

    def _insert_running_job(self, req: dict[str, Any]) -> dict[str, Any]:
        """Validate the snapshot and insert a job in 'running' state."""
        snap = self.get_snapshot(req["snapshot_id"])
        if not self.snapshot_signature_valid(snap):
            raise Conflict("snapshot signature is invalid; refusing to run")

        job_id = uuid.uuid4().hex
        created_at = utcnow()
        params_digest_start = digest_json(snap["params"])
        input_digest = self._compute_input_digest(
            snapshot_id=snap["id"], bag_digest=snap["bag_digest"],
            params_digest=params_digest_start,
            calibration_digest=snap["calibration_digest"],
            algorithm=snap["algorithm"], seed=req["seed"],
        )
        with self._lock:
            n = self._conn.execute(
                "SELECT COUNT(*) AS c FROM jobs WHERE snapshot_id=?", (snap["id"],)
            ).fetchone()["c"]
            attempt_index = n + 1
            self._conn.execute(
                """INSERT INTO jobs(id,snapshot_id,attempt_index,status,seed,input_digest,
                   params_digest_start,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (job_id, snap["id"], attempt_index, "running", req["seed"],
                 input_digest, params_digest_start, created_at),
            )
        return self.get_job(job_id)

    def create_job(self, req: dict[str, Any]) -> dict[str, Any]:
        """Insert a job and execute it synchronously in this call."""
        view = self._insert_running_job(req)
        self._execute_job(view["id"], req)
        return self.get_job(view["id"])

    def start_job(self, req: dict[str, Any]) -> dict[str, Any]:
        """Insert a job and execute it in a daemon thread (returns immediately)."""
        view = self._insert_running_job(req)
        thread = threading.Thread(
            target=self._execute_job, args=(view["id"], req), daemon=True
        )
        thread.start()
        return self.get_job(view["id"])

    def _execute_job(self, job_id: str, req: dict[str, Any]) -> None:
        """Numeric pipeline + evidence write. Runs against the pinned snapshot."""
        job = self.get_job(job_id)
        snap = self.get_snapshot(job["snapshot_id"])
        try:
            bag_bytes = self.get_bag_bytes(snap["bag_digest"])
            if req.get("hold_sec"):
                time.sleep(float(req["hold_sec"]))
            results = run_pipeline(
                bag_bytes, snap["params"],
                snap["calibration"]["transform"], job["seed"],
            )

            if req.get("simulate_param_change"):
                # Simulate someone editing parameters mid-run: the end-of-run
                # digest is computed over a changed parameter set.
                tampered_params = dict(snap["params"])
                tampered_params["__changed_during_run__"] = True
                params_digest_end = digest_json(tampered_params)
            else:
                params_digest_end = digest_json(snap["params"])

            input_digest_end = self._compute_input_digest(
                snapshot_id=snap["id"], bag_digest=snap["bag_digest"],
                params_digest=params_digest_end,
                calibration_digest=snap["calibration_digest"],
                algorithm=snap["algorithm"], seed=job["seed"],
            )

            payload = render_artifact(
                snapshot_id=snap["id"], seed=job["seed"],
                bag_digest=snap["bag_digest"], params_digest=params_digest_end,
                calibration_digest=snap["calibration_digest"], results=results,
                input_digest=input_digest_end,
            )
            job_dir = self.jobs_dir / job_id
            out_path = write_artifact_atomic(job_dir, "result.json", payload)
            output_digest = sha256_file(out_path)
            completed_at = utcnow()

            failures: list[str] = []
            if params_digest_end != job["params_digest_start"]:
                failures.append("parameters_changed_during_run")
            if input_digest_end != job["input_digest"]:
                failures.append("input_digest_mismatch_start_vs_end")
            reproducible = not failures

            with self._lock:
                cur = self._conn.execute(
                    """UPDATE jobs SET status='completed', input_digest_end=?, output_path=?,
                       output_digest=?, results_json=?, params_digest_end=?, reproducible=?,
                       failures_json=?, completed_at=? WHERE id=? AND status='running'""",
                    (input_digest_end, str(out_path), output_digest,
                     canonical_json(results).decode(), params_digest_end,
                     1 if reproducible else 0, json.dumps(failures),
                     completed_at, job_id),
                )
                if cur.rowcount == 0:
                    # A restart-recovery (or another actor) already settled this job.
                    os.replace(out_path, out_path.with_name(
                        f"result.json.abandoned-{uuid.uuid4().hex[:8]}"))
        except Exception as exc:  # honest failure reporting
            with self._lock:
                self._conn.execute(
                    """UPDATE jobs SET status='failed', reproducible=0, error=?,
                       failures_json=?, completed_at=? WHERE id=? AND status='running'""",
                    (str(exc), json.dumps(["execution_error"]), utcnow(), job_id),
                )

    def get_job(self, job_id: str) -> dict[str, Any]:
        r = self._fetchone("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not r:
            raise NotFound(f"unknown job {job_id}")
        return self._job_view(r)

    def list_jobs(self, snapshot_id: str | None = None) -> list[dict[str, Any]]:
        if snapshot_id:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE snapshot_id=? ORDER BY attempt_index", (snapshot_id,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY created_at, attempt_index"
            ).fetchall()
        return [self._job_view(r) for r in rows]

    def _append_tamper_event(self, job_id: str, event: str) -> None:
        with self._lock:
            r = self._fetchone("SELECT tamper_json FROM jobs WHERE id=?", (job_id,))
            if not r:
                raise NotFound(f"unknown job {job_id}")
            events = json.loads(r["tamper_json"])
            events.append(f"{utcnow()} {event}")
            self._conn.execute(
                "UPDATE jobs SET tamper_json=? WHERE id=?",
                (json.dumps(events), job_id),
            )

    def simulate_tamper_output(self, job_id: str, mode: str) -> dict[str, Any]:
        """Fault injection for the acceptance demo: delete or swap the artifact.

        The original evidence is never destroyed: on swap it is renamed aside.
        """
        job = self.get_job(job_id)
        if job["status"] != "completed" or not job["output_path"]:
            raise Conflict("only completed jobs with an output can be tampered with")
        out = Path(job["output_path"])
        if not out.exists() and mode != "missing":
            raise Conflict("output already missing on disk")

        if mode == "missing":
            if out.exists():
                removed = out.with_name("result.json.removed-evidence")
                out.rename(removed)
            self._append_tamper_event(job_id, "fault_injection: output artifact deleted from evidence store")
        elif mode == "swap":
            kept = out.with_name("result.json.original-evidence")
            out.rename(kept)
            out.write_bytes(b'{"tampered": true, "note": "replaced test input/evidence"}\n')
            self._append_tamper_event(job_id, "fault_injection: output artifact content replaced")
        else:
            raise ValueError("mode must be 'missing' or 'swap'")
        return self.revalidate_job(job_id)

    def _verify_job_evidence(self, job: dict[str, Any], snap: dict[str, Any]) -> list[str]:
        """Full re-verification against current disk state. Returns failure codes."""
        failures: list[str] = []

        if job["status"] == "running":
            failures.append("job_still_running")
            return failures
        if job["status"] != "completed":
            failures.append("job_not_completed")
            return failures

        # 1. snapshot integrity
        if not self.snapshot_signature_valid(snap):
            failures.append("snapshot_signature_invalid")

        # 2. seed/input recorded
        if job["seed"] is None or job["input_digest"] is None:
            failures.append("missing_seed_or_input_record")

        # 3. start/end input + parameter consistency
        if job["input_digest"] != job["input_digest_end"]:
            failures.append("input_digest_mismatch_start_vs_end")
        if job["params_digest_start"] != job["params_digest_end"]:
            failures.append("parameters_changed_during_run")

        # 4. output exists and digest matches the recorded evidence
        out = Path(job["output_path"]) if job["output_path"] else None
        if not out or not out.exists():
            failures.append("output_artifact_missing")
            return failures  # nothing further to check
        actual_digest = sha256_file(out)
        if actual_digest != job["output_digest"]:
            failures.append("output_digest_mismatch")
            return failures

        # 5. artifact parses and records the same input + seed
        try:
            artifact = json.loads(out.read_bytes())
        except (json.JSONDecodeError, OSError):
            failures.append("output_unparseable")
            return failures
        embedded = artifact.get("input", {})
        if embedded.get("input_digest") != job["input_digest"]:
            failures.append("artifact_input_record_mismatch")
        if embedded.get("seed") != job["seed"]:
            failures.append("artifact_seed_mismatch")

        # 6. real recomputation: rerun the deterministic pipeline now and compare
        bag_path = self._bag_path(snap["bag_digest"])
        if not bag_path.exists():
            failures.append("input_bag_missing")
        elif sha256_file(bag_path) != snap["bag_digest"]:
            failures.append("input_bag_digest_mismatch")
        try:
            bag_bytes = self.get_bag_bytes(snap["bag_digest"])
            fresh = run_pipeline(
                bag_bytes, snap["params"],
                snap["calibration"]["transform"], job["seed"],
            )
            if canonical_json(fresh) != canonical_json(artifact.get("results")):
                failures.append("nondeterministic_rerun_result")
        except Exception as exc:
            failures.append(f"recomputation_error:{type(exc).__name__}")

        return failures

    def revalidate_job(self, job_id: str) -> dict[str, Any]:
        """Re-run all evidence checks and update the reproducibility verdict."""
        job = self.get_job(job_id)
        snap = self.get_snapshot(job["snapshot_id"])
        failures = self._verify_job_evidence(job, snap)
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET reproducible=?, failures_json=? WHERE id=?",
                (1 if not failures else 0, json.dumps(failures), job_id),
            )
        return self.get_job(job_id)

    # ---------------------------------------------------------------- indexes
    def publish_index(self, job_ids: list[str]) -> dict[str, Any]:
        # ---------- phase 1: validate everything BEFORE publishing ----------
        if len(set(job_ids)) != len(job_ids):
            raise Conflict("duplicate job ids in publish request")
        jobs = [self.get_job(j) for j in job_ids]  # raises NotFound if missing
        entries: list[dict[str, Any]] = []
        for job in jobs:
            snap = self.get_snapshot(job["snapshot_id"])
            failures = self._verify_job_evidence(job, snap)
            if failures:
                raise Conflict(
                    f"job {job['id']} failed validation: {', '.join(failures)}; index NOT published"
                )
            out_rel = os.path.relpath(job["output_path"], self.root)
            entries.append({
                "job_id": job["id"],
                "attempt_index": job["attempt_index"],
                "snapshot_id": job["snapshot_id"],
                "seed": job["seed"],
                "input_digest": job["input_digest"],
                "output_path": out_rel,
                "output_digest": job["output_digest"],
                "results_digest": digest_json(job["results_summary"]),
            })

        # ---------- phase 2: build, sign and atomically write the index ----------
        with self._lock:
            number = (self._conn.execute(
                "SELECT COALESCE(MAX(index_number),0)+1 AS n FROM indexes"
            ).fetchone())["n"]
        index_id = uuid.uuid4().hex
        unsigned = {
            "index_type": "robot_experiment_publication",
            "index_id": index_id,
            "index_number": number,
            "published_at": utcnow(),
            "entries": entries,
        }
        digest = digest_json(unsigned)
        signed = dict(unsigned)
        signed["digest"] = digest
        signature = self.signer.sign_json(signed)
        body = dict(signed)
        body["signature"] = signature
        payload = canonical_json(body) + b"\n"
        index_path = write_artifact_atomic(
            self.indexes_dir, f"index-{number:04d}.json", payload
        )
        # Read the file back and verify it before recording the publication.
        written = json.loads(index_path.read_bytes())
        w_unsigned = {k: v for k, v in written.items() if k not in ("digest", "signature")}
        if digest_json(w_unsigned) != digest:
            raise StorageError("index file content digest mismatch after write; refusing to publish")
        if not self.signer.verify_json_signature(
            {k: v for k, v in written.items() if k != "signature"}, written["signature"]
        ):
            raise StorageError("index signature self-check failed after write; refusing to publish")
        with self._lock:
            self._conn.execute(
                """INSERT INTO indexes(id,index_number,published_at,path,digest,signature,entry_count)
                   VALUES(?,?,?,?,?,?,?)""",
                (index_id, number, body["published_at"], str(index_path),
                 body["digest"], body["signature"], len(entries)),
            )
        return self.get_index(index_id)

    def _index_row(self, index_id: str) -> sqlite3.Row:
        r = self._fetchone("SELECT * FROM indexes WHERE id=?", (index_id,))
        if not r:
            raise NotFound(f"unknown index {index_id}")
        return r

    def get_index(self, index_id: str) -> dict[str, Any]:
        r = self._index_row(index_id)
        body = json.loads(Path(r["path"]).read_bytes())
        return {
            "id": r["id"],
            "index_number": r["index_number"],
            "published_at": r["published_at"],
            "path": r["path"],
            "digest": r["digest"],
            "signature": r["signature"],
            "entry_count": r["entry_count"],
            "entries": body["entries"],
        }

    def latest_index(self) -> dict[str, Any] | None:
        r = self._fetchone("SELECT id FROM indexes ORDER BY index_number DESC LIMIT 1")
        return self.get_index(r["id"]) if r else None

    def list_indexes(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT id FROM indexes ORDER BY index_number").fetchall()
        return [self.get_index(r["id"]) for r in rows]

    def verify_index(self, index_id: str) -> dict[str, Any]:
        """Independent verification of a published index: signature + every entry."""
        r = self._index_row(index_id)
        path = Path(r["path"])
        checks: dict[str, Any] = {"index_id": index_id, "ok": True, "problems": []}
        if not path.exists():
            checks["ok"] = False
            checks["problems"].append("index_file_missing")
            return checks
        body = json.loads(path.read_bytes())
        unsigned = {k: v for k, v in body.items() if k not in ("digest", "signature")}
        if digest_json(unsigned) != r["digest"]:
            checks["ok"] = False
            checks["problems"].append("index_content_digest_mismatch")
        if not self.signer.verify_json_signature(
            {k: v for k, v in body.items() if k != "signature"}, body["signature"]
        ):
            checks["ok"] = False
            checks["problems"].append("index_signature_invalid")
        for e in body["entries"]:
            out = Path(self.root) / e["output_path"]
            if not out.exists():
                checks["ok"] = False
                checks["problems"].append(f"entry {e['job_id']}: output_missing")
            elif sha256_file(out) != e["output_digest"]:
                checks["ok"] = False
                checks["problems"].append(f"entry {e['job_id']}: output_digest_mismatch")
        checks["entries_checked"] = len(body["entries"])
        return checks
