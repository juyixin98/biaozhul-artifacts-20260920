"""Process-local registry of simulated clusters and drains."""
from __future__ import annotations

import itertools
import threading

from .executor import Drain, ExecutorError, create_drain
from .models import Snapshot
from .security import new_secret, sha256_fingerprint
from .simulator import Simulator


class Manager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.sims: dict[str, Simulator] = {}
        self.drains: dict[str, Drain] = {}
        self.secrets: dict[str, str] = {}
        self.idempotent: dict[tuple[str, str], dict] = {}
        self._sim_seq = itertools.count(1)
        self._drain_seq = itertools.count(1)

    # ------------------------------------------------------------------ #
    def create_simulation(self, snapshot: Snapshot) -> str:
        with self._lock:
            sim_id = f"sim-{next(self._sim_seq):03d}"
            self.sims[sim_id] = Simulator(snapshot)
            self.secrets[sim_id] = new_secret()
            return sim_id

    def require_sim(self, sim_id: str) -> Simulator:
        try:
            return self.sims[sim_id]
        except KeyError:
            raise ExecutorError(f"unknown simulation {sim_id}", code="unknown_sim", status=404)

    def secret_for(self, sim_id: str) -> str:
        try:
            return self.secrets[sim_id]
        except KeyError:
            raise ExecutorError(f"unknown simulation {sim_id}", code="unknown_sim", status=404)

    # ------------------------------------------------------------------ #
    def create_drain(
        self,
        sim_id: str,
        nodes: list[str],
        ignore_daemon_sets: bool,
        uncordon: bool,
        idempotency_key: str | None,
    ) -> tuple[Drain, str, bool]:
        with self._lock:
            sim = self.require_sim(sim_id)
            if idempotency_key:
                cached = self.idempotent.get((sim_id, idempotency_key))
                if cached is not None:
                    return self.drains[cached["drainId"]], cached["token"], True
            drain_id = f"drain-{next(self._drain_seq):03d}"
            drain = create_drain(
                sim,
                drain_id=drain_id,
                secret=self.secrets[sim_id],
                nodes=nodes,
                ignore_daemon_sets=ignore_daemon_sets,
                uncordon_on_complete=uncordon,
            )
            self.drains[drain_id] = drain
            token = drain.token()
            if idempotency_key:
                self.idempotent[(sim_id, idempotency_key)] = {
                    "drainId": drain_id, "token": token,
                }
            return drain, token, False

    def require_drain(self, sim_id: str, drain_id: str) -> Drain:
        drain = self.drains.get(drain_id)
        if drain is None or drain.sim is not self.require_sim(sim_id):
            raise ExecutorError(
                f"unknown drain {drain_id} in simulation {sim_id}",
                code="unknown_drain", status=404,
            )
        return drain

    def snapshot_etag(self, sim_id: str) -> str:
        sim = self.require_sim(sim_id)
        return '"' + sha256_fingerprint(
            sim.public_snapshot().model_dump(by_alias=True, mode="json")
        )[:32] + '"'
