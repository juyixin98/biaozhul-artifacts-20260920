"""Reorg-aware synchronization engine.

Per ``sync_once`` tick:

1. Read the chain head.
2. Find the fork point: walk downward from ``min(stored_tip, head)`` while the
   stored canonical block hash differs from the on-chain hash.
3. Detach canonical blocks above the fork point (tip first), applying inverse
   event effects. Detached blocks stay on record as orphans with their event
   rows removed.
4. Catch up: ingest blocks from fork+1 to head in order, applying forward
   effects. Previously detached blocks that become canonical again are
   re-adopted rather than duplicated.

Confirmed tip = ``head_number - (confirmations - 1)``. Confirmations only
model soft finality for the read API; the rollback path works at any depth.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from .chain import ChainClient
from .store import IndexStore

logger = logging.getLogger("indexer")


@dataclass
class SyncReport:
    head_before: tuple[int, str] | None = None
    head_after: tuple[int, str] | None = None
    fork_point: int | None = None
    detached_blocks: list[str] = field(default_factory=list)
    detached_events: int = 0
    ingested_blocks: list[str] = field(default_factory=list)
    ingested_events: int = 0
    reorg: bool = False

    def as_dict(self) -> dict:
        return {
            "head_before": _fmt(self.head_before),
            "head_after": _fmt(self.head_after),
            "fork_point": self.fork_point,
            "reorg": self.reorg,
            "detached_block_hashes": self.detached_blocks,
            "detached_events": self.detached_events,
            "ingested_block_hashes": self.ingested_blocks,
            "ingested_events": self.ingested_events,
        }


def _fmt(tip: tuple[int, str] | None) -> dict | None:
    if tip is None:
        return None
    return {"number": tip[0], "hash": tip[1]}


class Indexer:
    def __init__(
        self,
        client: ChainClient,
        store: IndexStore,
        start_block: int = 0,
        confirmations: int = 2,
        log_chunk_size: int = 100,
    ):
        self.client = client
        self.store = store
        self.start_block = start_block
        self.confirmations = max(1, confirmations)
        self.chunk_size = log_chunk_size

    @property
    def tip(self) -> tuple[int, str] | None:
        return self.store.tip()

    def confirmed_tip_number(self) -> int | None:
        tip = self.store.tip()
        if tip is None:
            return None
        confirmed = tip[0] - (self.confirmations - 1)
        return confirmed if confirmed >= 0 else None

    # ------------------------------------------------------------ the tick

    def sync_once(self) -> SyncReport:
        report = SyncReport(head_before=self.store.tip())
        head = self.client.head()
        stored = self.store.tip()

        fork_point = self._find_fork_point(head, stored)
        report.fork_point = fork_point

        if stored is not None and stored[0] > fork_point:
            self._rollback(stored[0], fork_point, report)
            report.reorg = True

        self._catch_up(fork_point + 1, head, report)

        self.store.set_tip(head.number, head.hash)
        report.head_after = (head.number, head.hash)
        logger.info(
            "sync done: head=%s detached=%s ingested=%s reorg=%s",
            head.number,
            len(report.detached_blocks),
            len(report.ingested_blocks),
            report.reorg,
        )
        return report

    # ----------------------------------------------------------- internals

    def _find_fork_point(
        self, head, stored: tuple[int, str] | None
    ) -> int:
        """Highest common height where stored and on-chain hashes agree.

        Returns -1 when even the genesis differs (e.g. the node was reset to
        a fork with a replaced genesis): the caller rolls back every block
        and re-indexes from height 0.
        """
        if stored is None:
            # Nothing indexed yet: begin one block before start_block.
            return self.start_block - 1

        n = min(stored[0], head.number)
        chain_hash = self.client.get_header(n).hash
        stored_hash = self.store.canonical_hash_at(n)
        while stored_hash != chain_hash:
            if n <= 0:
                if self.start_block == 0:
                    return -1
                raise RuntimeError(
                    "reorg diverges below INDEXER_START_BLOCK; cannot roll back"
                )
            n -= 1
            chain_hash = self.client.get_header(n).hash
            stored_hash = self.store.canonical_hash_at(n)
        return n

    def _rollback(
        self, from_number: int, fork_point: int, report: SyncReport
    ) -> None:
        n = from_number
        while n > fork_point:
            block_hash = self.store.canonical_hash_at(n)
            if block_hash is None:
                # Canonical chain had a gap (can happen with start_block>0):
                # simply continue downward.
                n -= 1
                continue
            detached = self.store.detach_block(block_hash)
            report.detached_blocks.append(block_hash)
            report.detached_events += len(detached)
            parent = self.store.get_block(block_hash)["parent_hash"]
            self.store.set_tip(n - 1, parent)
            n -= 1

    def _catch_up(self, from_block: int, head, report: SyncReport) -> None:
        if from_block > head.number:
            return
        logs = self.client.fetch_logs_chunked(
            from_block, head.number, self.chunk_size
        )
        by_block: dict[str, list[dict]] = defaultdict(list)
        for event in logs:
            by_block[event["block_hash"]].append(event)

        for number in range(from_block, head.number + 1):
            header = self.client.get_header(number)
            events = by_block.get(header.hash, [])
            self._ingest(header.number, header.hash, header.parent_hash, events)
            report.ingested_blocks.append(header.hash)
            report.ingested_events += len(events)

    def _ingest(
        self, number: int, block_hash: str, parent_hash: str, events: list[dict]
    ) -> None:
        events = sorted(events, key=lambda e: e["log_index"])
        if self.store.has_block(block_hash):
            self.store.readopt_block(number, block_hash, parent_hash, events)
        else:
            self.store.ingest_block(number, block_hash, parent_hash, events)
