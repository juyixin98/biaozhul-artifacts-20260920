"""Event-rollback indexer package.

Modules:
    abi       -- Vault contract ABI / artifact loading + event decoding
    storage   -- SQLite-backed block DAG, event log and materialized state
    reducer   -- reversible event -> business-state projection
    indexer   -- chain following, confirmation depth, reorg rollback & catch-up
    onchain   -- Anvil/web3 helpers (deploy, transactions, snapshot/mining RPC)
    main      -- FastAPI app + background poller entrypoint
"""

__all__ = ["abi", "storage", "reducer", "indexer", "onchain", "main"]
