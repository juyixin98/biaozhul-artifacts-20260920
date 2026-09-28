"""Local multi-process runtime: drives rounds across worker subprocesses.

The runtime owns the OS-level orchestration: it starts the FastAPI control
plane in a background thread, spawns one process per worker, and drives
the round loop (begin -> wait for submissions -> commit/abort).  All
correctness decisions live in :mod:`bucket_sync.coordinator`; this module
only moves messages and enforces timeouts.
"""

from __future__ import annotations

import multiprocessing as mp
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import uvicorn

from bucket_sync.api import create_app
from bucket_sync.bucketing import BucketLayout
from bucket_sync.coordinator import Coordinator
from bucket_sync.worker import WorkerConfig, worker_entry


@dataclass(frozen=True)
class WorkerSpec:
    worker_id: str
    x_shard: np.ndarray
    y_shard: np.ndarray
    crash_after_buckets: Optional[int] = None
    hang_after_buckets: Optional[int] = None
    corrupt_base_token: bool = False
    missing_params: Tuple[str, ...] = ()
    submit_order_seed: int = 0


@dataclass
class RoundOutcome:
    round_id: int
    outcome: str  # "committed" | "aborted" | "timeout"
    reason: Optional[str]
    generation_after: Optional[int]


@dataclass
class RunResult:
    final_params: Dict[str, np.ndarray]
    generation: int
    rounds: List[RoundOutcome]
    worker_reports: Dict[str, list]
    diagnostics_events: list


class LocalRuntime:
    """Runs a coordinator + N worker processes on localhost."""

    def __init__(
        self,
        coordinator: Coordinator,
        layout: BucketLayout,
        worker_specs: Sequence[WorkerSpec],
        *,
        host: str = "127.0.0.1",
        port: int = 0,  # 0 = pick a free port
        round_timeout_s: float = 20.0,
    ) -> None:
        self._coordinator = coordinator
        self._layout = layout
        self._specs = list(worker_specs)
        self._host = host
        self._port = port
        self._round_timeout_s = round_timeout_s
        self._server: Optional[uvicorn.Server] = None
        self._server_thread: Optional[threading.Thread] = None
        self._ctx = mp.get_context("spawn")

    # ---- server lifecycle ------------------------------------------

    def _pick_port(self) -> int:
        import socket

        with socket.socket() as s:
            s.bind((self._host, 0))
            return s.getsockname()[1]

    def start_server(self) -> str:
        if self._port == 0:
            self._port = self._pick_port()
        app = create_app(self._coordinator)
        config = uvicorn.Config(
            app, host=self._host, port=self._port, log_level="warning"
        )
        self._server = uvicorn.Server(config)
        self._server_thread = threading.Thread(
            target=self._server.run, daemon=True, name="bucket-sync-server"
        )
        self._server_thread.start()
        deadline = time.monotonic() + 10.0
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("server failed to start within 10s")
            time.sleep(0.02)
        return self.base_url

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    def stop_server(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._server_thread is not None:
            self._server_thread.join(timeout=5.0)

    # ---- workers ----------------------------------------------------

    def _spawn_workers(self, rounds: int) -> Tuple[Dict[str, object], object]:
        queue = self._ctx.Queue()
        procs: Dict[str, object] = {}
        for spec in self._specs:
            cfg = WorkerConfig(
                worker_id=spec.worker_id,
                server_url=self.base_url,
                layout=self._layout,
                x_shard=spec.x_shard,
                y_shard=spec.y_shard,
                submit_order_seed=spec.submit_order_seed,
                crash_after_buckets=spec.crash_after_buckets,
                hang_after_buckets=spec.hang_after_buckets,
                corrupt_base_token=spec.corrupt_base_token,
                missing_params=tuple(spec.missing_params),
            )
            proc = self._ctx.Process(
                target=worker_entry,
                args=(cfg, rounds, queue),
                name=f"worker-{spec.worker_id}",
                daemon=True,
            )
            proc.start()
            procs[spec.worker_id] = proc
        return procs, queue

    # ---- round driving ----------------------------------------------

    def run(self, rounds: int) -> RunResult:
        """Run ``rounds`` synchronous rounds; returns full evidence."""
        self.start_server()
        all_ids = [s.worker_id for s in self._specs]
        procs_by_id, queue = self._spawn_workers(rounds)
        procs = list(procs_by_id.values())
        outcomes: List[RoundOutcome] = []
        try:
            self._wait_for_registration(all_ids)
            for _ in range(rounds):
                outcome = self._drive_one_round(all_ids, procs_by_id)
                outcomes.append(outcome)
            worker_reports = self._collect_reports(queue, procs_by_id)
        finally:
            for proc in procs:
                if proc.is_alive():
                    proc.terminate()
                proc.join(timeout=5.0)
            self.stop_server()
        return RunResult(
            final_params=self._coordinator.snapshot_params(),
            generation=self._coordinator.current_generation(),
            rounds=outcomes,
            worker_reports=worker_reports,
            diagnostics_events=[
                {
                    "record_id": e.record_id,
                    "round_id": e.round_id,
                    "worker_id": e.worker_id,
                    "outcome": e.outcome,
                    "reason": e.reason,
                    "detail": e.detail,
                }
                for e in self._coordinator.diagnostics.events()
            ],
        )

    def _wait_for_registration(self, all_ids: List[str]) -> None:
        deadline = time.monotonic() + self._round_timeout_s
        while time.monotonic() < deadline:
            if set(all_ids) <= set(self._coordinator.alive_worker_ids()):
                return
            time.sleep(0.02)
        raise RuntimeError(
            f"workers never registered: "
            f"{sorted(set(all_ids) - set(self._coordinator.alive_worker_ids()))}"
        )

    def _drive_one_round(
        self,
        all_ids: List[str],
        procs_by_id: Dict[str, object],
    ) -> RoundOutcome:
        # Only currently-alive workers participate; a worker that crashed in
        # an earlier round is excluded rather than silently dropped.
        participant_ids = [
            wid for wid in all_ids if self._coordinator.is_alive(wid)
        ]
        desc = self._coordinator.begin_round(participant_ids)
        round_id = desc["round_id"]
        deadline = time.monotonic() + self._round_timeout_s
        while time.monotonic() < deadline:
            # Process death is failure, not absence: report it immediately.
            for wid, proc in procs_by_id.items():
                if self._coordinator.is_alive(wid) and not proc.is_alive():
                    self._coordinator.mark_worker_lost(wid, reason="process_exited")
            self._coordinator.check_liveness()
            desc_now = self._coordinator.round_descriptor()
            if desc_now is None or desc_now["status"] != "open":
                # Surface the specific abort cause (e.g. worker_lost), not a
                # generic "aborted".
                if desc_now is not None and desc_now["status"] == "aborted":
                    return RoundOutcome(
                        round_id, "aborted", desc_now.get("abort_reason"), None
                    )
                return RoundOutcome(round_id, "aborted", "round_aborted", None)
            report = self._coordinator.commit_round()
            if report.outcome == "committed":
                return RoundOutcome(
                    round_id, "committed", None, report.generation_after
                )
            if report.outcome == "rejected":
                return RoundOutcome(round_id, "aborted", report.reason, None)
            time.sleep(0.02)  # undecided: submissions still in flight
        self._coordinator.abort_round("round_timeout")
        return RoundOutcome(round_id, "timeout", "round_timeout", None)

    def _collect_reports(self, queue, procs) -> Dict[str, list]:
        """Collect reports opportunistically.

        Crashed workers send a summary before exiting; hung workers never do,
        so missing reports are recorded as such rather than waited upon.
        """
        reports: Dict[str, list] = {}
        deadline = time.monotonic() + 2.0
        pending = set(procs)
        while pending and time.monotonic() < deadline:
            try:
                msg = queue.get(timeout=0.2)
            except Exception:
                continue
            wid = msg["worker_id"]
            pending.discard(wid)
            reports[wid] = msg.get("summaries", msg)
        for wid in pending:
            reports[wid] = {"ok": False, "error": "no report: worker still running or hung"}
        return reports
