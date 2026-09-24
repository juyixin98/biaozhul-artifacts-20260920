"""Reorg-aware event indexer.

Sync model
----------
* The indexer stores a contiguous, hash-linked chain of blocks from
  ``start_block`` (the anchor, applied without events) up to its current
  *target* (chain head minus ``confirmation_depth``).
* Each poll fetches the target block and walks it back via ``parentHash`` until
  it reaches a block hash we already have -- the fork point.
* Forked-out blocks are rolled back newest-first: their events are undone in
  reverse log order (reversible reducer) and their block/event rows deleted, so
  uncle events are removed and cannot contaminate business state.
* The new chain is then applied oldest-first until the target is reached.

Identification of a log is ``(block_hash, tx_hash, log_index)``. Because the
block hash is part of the key, the *same transaction* mined first on an uncle
and later on the canonical chain is tracked separately in each context and
correctly removed/re-added across a reorg.

All structural + state changes for one sync happen in a single SQLite
transaction, so a crash leaves either the old or the new chain indexed, never
a mix.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from web3 import Web3

from . import onchain
from .abi import decode_log
from .reducer import apply_block, revert_block
from .storage import BlockRow, EventRow, Storage, TIP_HASH, TIP_NUMBER

log = logging.getLogger("indexer")


@dataclass
class SyncReport:
    target_number: int = -1
    rolled_back: list[int] = field(default_factory=list)
    applied: list[int] = field(default_factory=list)
    events_undone: int = 0
    events_applied: int = 0
    fork_point: int | None = None

    @property
    def reorged(self) -> bool:
        return bool(self.rolled_back)


class Indexer:
    def __init__(
        self,
        w3: Web3,
        store: Storage,
        vault_address: str,
        start_block: int = 0,
        confirmation_depth: int = 0,
    ):
        self.w3 = w3
        self.store = store
        self.vault_address = Web3.to_checksum_address(vault_address)
        self.start_block = start_block
        self.confirmation_depth = max(0, confirmation_depth)

    # ---- helpers ----------------------------------------------------------
    def _fetch_block(self, number: int) -> BlockRow:
        info = onchain.block_info(self.w3, number)
        return BlockRow(info["number"], info["hash"], info["parent_hash"])

    def _anchor_if_needed(self) -> None:
        """Seed the start block as the root of the indexed hash chain."""
        if self.store.tip() is not None:
            return
        anchor = self._fetch_block(self.start_block)
        self.store.insert_block(anchor)
        self.store.set_meta(TIP_NUMBER, anchor.number)
        self.store.set_meta(TIP_HASH, anchor.hash)
        log.info("anchored at block %d %s", anchor.number, anchor.hash[:10])

    def target_head(self) -> int:
        """Newest block number safe to index given confirmation depth."""
        return max(self.start_block - 1, self.w3.eth.block_number - self.confirmation_depth)

    # ---- core sync --------------------------------------------------------
    def sync_to(self, target_number: int) -> SyncReport:
        """Rollback (if needed) and catch up so the stored tip == target."""
        report = SyncReport(target_number=target_number)
        self._anchor_if_needed()

        if target_number < self.start_block:
            return report  # nothing indexed yet / chain shorter than anchor

        with self.store:
            tip = self.store.tip()
            assert tip is not None  # anchor guarantees it

            target = self._fetch_block(target_number)

            # --- 1. walk the target chain back to a block we already hold ---
            cursor = target
            orphan_hashes: list[str] = []
            while True:
                known = self.store.get_block(cursor.number)
                if known is not None and known.hash == cursor.hash:
                    break  # common ancestor found
                orphan_hashes.append(cursor.hash)
                if cursor.number <= self.start_block:
                    raise RuntimeError(
                        f"reorg below start block {self.start_block}; indexer cannot reconcile"
                    )
                parent = self.w3.eth.get_block(cursor.parent_hash)
                cursor = BlockRow(
                    int(parent["number"]),
                    parent["hash"].to_0x_hex(),
                    parent["parentHash"].to_0x_hex(),
                )

            fork_point = cursor.number
            report.fork_point = fork_point

            # --- 2. roll back forked-out indexed blocks, newest first -------
            current_num = tip.number
            while current_num > fork_point:
                # Ascending rows; revert_block undoes them in descending order.
                evs = self.store.events_on_block(current_num)
                revert_block(self.store, evs)
                report.events_undone += len(evs)
                self.store.delete_events_on_block(current_num)
                self.store.delete_block(current_num)
                report.rolled_back.append(current_num)
                current_num -= 1

            # --- 3. apply the new chain, oldest first -----------------------
            if orphan_hashes:
                # orphan_hashes: target -> ... -> child-of-forkpoint; reverse
                new_hashes = list(reversed(orphan_hashes))
                head_number = fork_point
                for h in new_hashes:
                    head_number += 1
                    b = self.w3.eth.get_block(h)
                    block = BlockRow(
                        int(b["number"]),
                        b["hash"].to_0x_hex(),
                        b["parentHash"].to_0x_hex(),
                    )
                    self.store.insert_block(block)
                    self._ingest_events(block, report)
            else:
                # pure forward extension from the common ancestor / current tip
                head_number = fork_point
                while head_number < target_number:
                    head_number += 1
                    block = self._fetch_block(head_number)
                    self.store.insert_block(block)
                    self._ingest_events(block, report)

            self.store.set_meta(TIP_NUMBER, target.number)
            self.store.set_meta(TIP_HASH, target.hash)

        if report.rolled_back:
            log.warning(
                "REORG: rolled back %s, re-applied %s (fork at %d)",
                report.rolled_back,
                report.applied,
                fork_point,
            )
        else:
            log.info("synced to %d (+%d events)", target_number, report.events_applied)
        return report

    def _ingest_events(self, block: BlockRow, report: SyncReport) -> None:
        rows: list[EventRow] = []
        for raw in onchain.vault_logs(self.w3, self.vault_address, block.number, block.number):
            if raw["blockHash"] != block.hash:
                # get_logs should never return this; guard regardless
                continue
            name, who, amount = decode_log(self.w3, raw)
            rows.append(
                EventRow(
                    block_number=block.number,
                    block_hash=block.hash,
                    tx_hash=raw["transactionHash"],
                    tx_index=raw["transactionIndex"],
                    log_index=raw["logIndex"],
                    event_name=name,
                    who=who,
                    amount=amount,
                )
            )
        rows.sort(key=lambda e: (e.tx_index, e.log_index))
        new_rows = [e for e in rows if self.store.insert_event(e)]
        apply_block(self.store, new_rows)
        report.events_applied += len(new_rows)
        report.applied.append(block.number)

    # ---- polling ----------------------------------------------------------
    def poll_once(self) -> SyncReport | None:
        """One poll cycle honoring confirmation depth.

        None when the chain head is still below the indexed tip (e.g. Anvil
        restarted to an empty state). We still call sync when target == tip so
        a same-height tip replacement (the tip block itself reorged) is caught.
        """
        target = self.target_head()
        tip = self.store.tip()
        if tip is not None and target < tip.number:
            return None
        return self.sync_to(target)
