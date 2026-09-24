"""Runtime configuration, sourced from environment variables.

All defaults target a local Anvil node and its well-known test key #0;
nothing here points at a public network.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_ANVIL_RPC = "http://127.0.0.1:8545"
# Anvil deterministic test account #0 (public, ephemeral, local chains only).
DEFAULT_TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


@dataclass(frozen=True)
class Settings:
    rpc_url: str = os.getenv("INDEXER_RPC_URL", DEFAULT_ANVIL_RPC)
    ledger_address: str | None = os.getenv("LEDGER_ADDRESS") or None
    db_path: str = os.getenv("INDEXER_DB_PATH", "data/indexer.db")
    start_block: int = int(os.getenv("INDEXER_START_BLOCK", "0"))
    # A block at height h is "confirmed" once the chain head reaches
    # h + confirmations - 1. Reorg handling itself is not limited by this
    # depth: a reorg crossing the confirmation boundary is still rolled back
    # correctly (defensive); the flag models soft finality for API consumers.
    confirmations: int = int(os.getenv("INDEXER_CONFIRMATIONS", "2"))
    poll_interval: float = float(os.getenv("INDEXER_POLL_INTERVAL", "1.0"))
    log_chunk_size: int = int(os.getenv("INDEXER_LOG_CHUNK", "100"))
    auto_start: bool = os.getenv("INDEXER_AUTO_START", "1") != "0"
    deployer_key: str = os.getenv("DEPLOYER_KEY", DEFAULT_TEST_KEY)
