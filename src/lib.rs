//! UTXO rollback validator — library surface.
//!
//! Modules:
//! * [`model`] — simplified block/tx wire types and real txid hashing
//! * [`hash`]  — double-SHA256, Merkle root, block hash
//! * [`db`]    — SQLite storage (blocks, UTXOs, undo records)
//! * [`chain`] — ordered validation, connect/disconnect, naive replay
//! * [`api`]   — Axum HTTP layer

pub mod api;
pub mod chain;
pub mod db;
pub mod error;
pub mod hash;
pub mod model;

use hash::Hash32;
use model::{Block, Transaction, TxIn, TxOut};

// ---------------------------------------------------------------------------
// Builders used by tests and the fixture generator. They construct
// protocol-valid objects with txids actually computed over canonical bytes.
// ---------------------------------------------------------------------------

pub fn coinbase_tx(address: &str, value: i64) -> Transaction {
    let mut tx = Transaction {
        version: 1,
        txid: None,
        vin: vec![],
        vout: vec![TxOut {
            value,
            address: address.to_string(),
        }],
    };
    tx.txid = Some(tx.compute_txid());
    tx
}

pub fn spending_tx(
    vin: Vec<(Hash32, u32, &str)>,
    vout: Vec<(i64, &str)>,
) -> Transaction {
    let mut tx = Transaction {
        version: 1,
        txid: None,
        vin: vin
            .into_iter()
            .map(|(txid, vout, sig)| TxIn {
                txid,
                vout,
                script_sig: hex::encode(sig),
            })
            .collect(),
        vout: vout
            .into_iter()
            .map(|(value, address)| TxOut {
                value,
                address: address.to_string(),
            })
            .collect(),
    };
    tx.txid = Some(tx.compute_txid());
    tx
}

pub fn block(height: i64, prev_hash: hash::Hash32, txs: Vec<Transaction>, timestamp: i64) -> Block {
    Block {
        version: 1,
        height,
        prev_hash,
        timestamp,
        txs,
    }
}
