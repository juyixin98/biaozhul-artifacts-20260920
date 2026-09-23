//! Wire types and domain types for the simplified chain.
//!
//! Every protocol hash is a real cryptographic computation:
//! - txids are double-SHA256 of the canonical unsigned serialization
//! - block hashes are double-SHA256 of the canonical header serialization
//! - signatures are real Ed25519 signatures over the txid
//!
//! Amounts are signed on the wire (`i64`) so a negative amount is a parse
//! error the validator reports explicitly instead of silently wrapping.

use serde::{Deserialize, Serialize};

/// Block subsidy at genesis, in whole integer coins.
pub const GENESIS_SUBSIDY: u64 = 50;
/// Subsidy halves every `SUBSIDY_HALVING_INTERVAL` blocks.
pub const SUBSIDY_HALVING_INTERVAL: u64 = 100;

/// Block subsidy (coinbase reward) for a block at `height`.
///
/// Fixed deterministic rule: 50 coins, halving every 100 blocks by integer
/// shift, so it eventually reaches 0.
pub fn block_subsidy(height: u64) -> u64 {
    GENESIS_SUBSIDY >> (height / SUBSIDY_HALVING_INTERVAL).min(63)
}

/// Reference to a transaction output: `(txid, output index)`.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
pub struct OutPoint {
    /// Hex-encoded double-SHA256 txid of the transaction being spent.
    pub txid: String,
    /// Zero-based index into that transaction's `outputs`.
    pub vout: u32,
}

/// Transaction input. Coinbase inputs carry `prev: None`; every other input
/// must reference an existing, unspent output.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TxIn {
    /// `None` for a coinbase input; otherwise the outpoint being spent.
    #[serde(default)]
    pub prev: Option<OutPoint>,
    /// Hex-encoded tag included in the signed serialization *only* for
    /// coinbase inputs. The block builder fills it with the block height
    /// (8-byte LE), which guarantees distinct coinbase txids per height
    /// (mirroring the Bitcoin height-in-coinbase rule).
    #[serde(default)]
    pub coinbase_tag: String,
    /// Hex-encoded 32-byte Ed25519 public key proving authority to spend.
    #[serde(default)]
    pub pubkey: String,
    /// Hex-encoded 64-byte Ed25519 signature over the txid of this tx.
    #[serde(default)]
    pub signature: String,
}

/// Transaction output. `address` is the hex-encoded 32-byte Ed25519 public key
/// that is allowed to spend this output later.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TxOut {
    /// Non-negative integer value.
    pub value: u64,
    /// Hex-encoded 32-byte Ed25519 public key owning this output.
    pub address: String,
}

/// A simplified transaction.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Transaction {
    #[serde(default)]
    pub inputs: Vec<TxIn>,
    pub outputs: Vec<TxOut>,
}

/// A simplified block. The `merkle_root` is supplied by the producer and must
/// match the real Merkle root of the block's txids, or the block is rejected.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Block {
    pub height: u64,
    /// Hex-encoded 32-byte parent block hash; all-zero at height 0.
    pub prev_hash: String,
    pub timestamp: i64,
    pub txs: Vec<Transaction>,
    #[serde(default)]
    pub merkle_root: String,
}

/// Stored block metadata (the block itself is stored as raw JSON too).
#[derive(Debug, Clone, Serialize)]
pub struct BlockInfo {
    pub height: u64,
    pub hash: String,
    pub prev_hash: String,
    pub timestamp: i64,
    pub tx_count: usize,
    pub merkle_root: String,
    pub state_root: String,
    pub fee_total: u64,
}

/// Current chain tip summary.
#[derive(Debug, Clone, Serialize)]
pub struct TipInfo {
    pub height: u64,
    pub hash: String,
    pub state_root: String,
    pub utxo_count: i64,
}

/// One UTXO as returned by history queries. `spent_in`/`created_in` are always
/// the *active-chain* heights the caller asked for.
#[derive(Debug, Clone, Serialize)]
pub struct UtxoJson {
    pub txid: String,
    pub vout: u32,
    pub value: u64,
    pub address: String,
    pub created_in: u64,
}

/// Result of connecting a block.
#[derive(Debug, Clone, Serialize)]
pub struct ConnectResult {
    pub accepted: bool,
    pub height: u64,
    pub hash: String,
    pub state_root: String,
    pub reorged: bool,
    /// Heights rolled back when this block replaced a longer chain tip.
    pub disconnected_heights: Vec<u64>,
    pub fee_total: u64,
}

/// Result of disconnecting the tip (explicit rollback).
#[derive(Debug, Clone, Serialize)]
pub struct DisconnectResult {
    pub disconnected: String,
    pub height: u64,
    pub new_tip_height: Option<u64>,
    pub new_tip_hash: String,
    pub state_root: String,
}
