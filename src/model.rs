//! Wire model: simplified blocks, transactions, inputs, outputs.
//!
//! Amounts are **signed on the wire** so a negative amount is a parse-level
//! failure (the whole block is rejected). Internally validated amounts are
//! non-negative `i64`.

use serde::{Deserialize, Serialize};

use crate::error::{Error, Result};
use crate::hash::{hash256, merkle_root, Hash32};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TxIn {
    /// 32-byte hex txid being spent; all-zero for a coinbase.
    pub txid: Hash32,
    pub vout: u32,
    /// Hex-encoded signature/script. Any non-empty bytes are accepted; the
    /// bytes themselves are covered by the txid and stored, not verified
    /// against a public key (the experiment has no key model).
    #[serde(rename = "scriptSig")]
    pub script_sig: String,
}

/// Output. `value` is signed on the wire so JSON can carry a negative number,
/// but [`static_check`] rejects negatives (the whole block is refused).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TxOut {
    pub value: i64,
    pub address: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Transaction {
    pub version: i32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub txid: Option<Hash32>,
    pub vin: Vec<TxIn>,
    pub vout: Vec<TxOut>,
}

impl Transaction {
    pub fn is_coinbase(&self) -> bool {
        self.vin.is_empty()
    }

    /// Canonical serialization used for txid computation. `version=1` only,
    /// little/big-endian fixed widths, length-prefixed strings.
    pub fn canonical_bytes(&self) -> Vec<u8> {
        let mut b = Vec::new();
        b.extend_from_slice(b"utxo-tx:");
        b.extend_from_slice(&self.version.to_be_bytes());
        b.extend_from_slice(&(self.vin.len() as u32).to_be_bytes());
        for i in &self.vin {
            b.extend_from_slice(i.txid.as_bytes());
            b.extend_from_slice(&i.vout.to_be_bytes());
            let sig = i.script_sig.as_bytes();
            b.extend_from_slice(&(sig.len() as u32).to_be_bytes());
            b.extend_from_slice(sig);
        }
        b.extend_from_slice(&(self.vout.len() as u32).to_be_bytes());
        for o in &self.vout {
            b.extend_from_slice(&o.value.to_be_bytes());
            let a = o.address.as_bytes();
            b.extend_from_slice(&(a.len() as u32).to_be_bytes());
            b.extend_from_slice(a);
        }
        b
    }

    pub fn compute_txid(&self) -> Hash32 {
        hash256(&self.canonical_bytes())
    }

    pub fn output_value_sum(&self) -> i64 {
        self.vout.iter().map(|o| o.value).sum()
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Block {
    pub version: i32,
    pub height: i64,
    #[serde(rename = "prevHash")]
    pub prev_hash: Hash32,
    #[serde(default)]
    pub timestamp: i64,
    pub txs: Vec<Transaction>,
}

impl Block {
    pub fn txids(&self) -> Vec<Hash32> {
        self.txs.iter().map(|t| t.compute_txid()).collect()
    }

    pub fn merkle_root(&self) -> Hash32 {
        merkle_root(self.txids())
    }

    pub fn block_hash(&self) -> Hash32 {
        crate::hash::block_hash(self, &self.merkle_root())
    }
}

/// Structural validation shared before and during execution: check the
/// fields that do not need UTXO state. Returns each tx's computed id.
pub fn static_check(block: &Block) -> Result<Vec<Hash32>> {
    if block.version != 1 {
        return Err(Error::BadBlockVersion(block.version));
    }
    if block.height < 1 {
        return Err(Error::Malformed("height must be >= 1".into()));
    }
    if block.txs.is_empty() {
        return Err(Error::EmptyBlock);
    }

    let mut ids = Vec::with_capacity(block.txs.len());
    let mut seen = std::collections::HashSet::new();
    for (idx, tx) in block.txs.iter().enumerate() {
        if tx.version != 1 {
            return Err(Error::BadTxVersion(tx.version));
        }
        if tx.vout.is_empty() {
            return Err(Error::EmptyOutputs);
        }
        for o in &tx.vout {
            if o.value < 0 {
                return Err(Error::NegativeAmount);
            }
            if o.address.is_empty() {
                return Err(Error::EmptyAddress);
            }
            if o.address.len() > 256 {
                return Err(Error::Malformed("address longer than 256 bytes".into()));
            }
        }
        for i in &tx.vin {
            let sig = hex::decode(&i.script_sig).map_err(|_| {
                Error::Malformed("scriptSig must be hex-encoded".into())
            })?;
            if sig.is_empty() {
                return Err(Error::Malformed("empty scriptSig".into()));
            }
        }
        let coinbase = tx.is_coinbase();
        if idx == 0 && !coinbase {
            return Err(Error::CoinbaseFirst);
        }
        if idx > 0 && coinbase {
            return Err(Error::MultipleCoinbase);
        }
        let id = tx.compute_txid();
        if let Some(supplied) = tx.txid {
            if supplied != id {
                return Err(Error::TxIdMismatch {
                    supplied: supplied.to_hex(),
                    computed: id.to_hex(),
                });
            }
        }
        if !seen.insert(id) {
            return Err(Error::DuplicateTx(id.to_hex()));
        }
        ids.push(id);
    }
    Ok(ids)
}
