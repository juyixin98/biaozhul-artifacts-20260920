//! Real cryptographic and protocol hashing.
//!
//! Nothing here is mocked:
//! * all 32-byte hashes are SHA-256 (Bitcoin-style double-SHA256 for
//!   identity hashes, so they are visibly distinct from a raw digest);
//! * state roots and Merkle roots are real Merkle trees over the full
//!   ordered leaves with odd-node duplication;
//! * spend authorization is a real Ed25519 verify against the output owner.

use ed25519_dalek::{VerifyingKey, Signature, Verifier};
use sha2::{Digest, Sha256};

use crate::error::AppError;
use crate::types::{Block, OutPoint, Transaction};

pub const HASH_LEN: usize = 32;

/// Hex-decode exactly `expect` bytes.
pub fn decode_hex(name: &str, s: &str, expect: usize) -> Result<Vec<u8>, AppError> {
    let v = hex::decode(s)
        .map_err(|e| AppError::bad_request(format!("field {name} is not valid hex: {e}")))?;
    if v.len() != expect {
        return Err(AppError::bad_request(format!(
            "field {name} must be {expect} bytes, got {}",
            v.len()
        )));
    }
    Ok(v)
}

/// Bitcoin-style double SHA-256.
pub fn hash256(bytes: &[u8]) -> [u8; HASH_LEN] {
    let first = Sha256::digest(bytes);
    Sha256::digest(first).into()
}

/// Real Ed25519 verification: the signature must be over `message` by the key
/// that owns the spent output.
pub fn verify_signature(pubkey: &[u8], message: &[u8], sig: &[u8]) -> bool {
    let key_bytes: &[u8; 32] = match pubkey.try_into() {
        Ok(b) => b,
        Err(_) => return false,
    };
    let (Ok(key), Ok(signature)) = (
        VerifyingKey::from_bytes(key_bytes),
        Signature::from_slice(sig),
    ) else {
        return false;
    };
    key.verify(message, &signature).is_ok()
}

// ---------------------------------------------------------------------------
// Canonical serialization
// ---------------------------------------------------------------------------

fn push_u64(out: &mut Vec<u8>, v: u64) {
    out.extend_from_slice(&v.to_le_bytes());
}

fn push_i64(out: &mut Vec<u8>, v: i64) {
    out.extend_from_slice(&v.to_le_bytes());
}

fn push_u32(out: &mut Vec<u8>, v: u32) {
    out.extend_from_slice(&v.to_le_bytes());
}

fn push_bytes(out: &mut Vec<u8>, b: &[u8]) {
    push_u64(out, b.len() as u64);
    out.extend_from_slice(b);
}

/// Canonical serialization of an outpoint: txid(32) || vout(u32 LE).
fn serialize_outpoint(op: &OutPoint) -> Result<Vec<u8>, AppError> {
    let mut out = Vec::with_capacity(36);
    let txid = decode_hex("txid", &op.txid, 32)?;
    out.extend_from_slice(&txid);
    push_u32(&mut out, op.vout);
    Ok(out)
}

/// Canonical serialization of a transaction for signing / identity hashing.
/// Inputs are encoded as:
/// * coinbase: `0u8`
/// * spend:    `1u8 || outpoint(36)`
/// Outputs are: `value(u64 LE) || address(32 raw)`.
/// Signatures and pubkeys are deliberately excluded from the signed message:
/// the signature authorizes the transaction identified by this serialization.
pub fn serialize_transaction_unsigned(tx: &Transaction) -> Result<Vec<u8>, AppError> {
    let mut out = Vec::new();
    push_u64(&mut out, tx.inputs.len() as u64);
    for input in &tx.inputs {
        match &input.prev {
            None => {
                out.push(0u8);
                let tag = if input.coinbase_tag.is_empty() {
                    Vec::new()
                } else {
                    decode_hex("coinbase_tag", &input.coinbase_tag, 8)?
                };
                push_bytes(&mut out, &tag);
            }
            Some(op) => {
                out.push(1u8);
                out.extend_from_slice(&serialize_outpoint(op)?);
            }
        }
    }
    push_u64(&mut out, tx.outputs.len() as u64);
    for o in &tx.outputs {
        push_u64(&mut out, o.value);
        let addr = decode_hex("address", &o.address, 32)?;
        out.extend_from_slice(&addr);
    }
    Ok(out)
}

/// Double-SHA256 of the canonical unsigned transaction.
pub fn txid(tx: &Transaction) -> Result<[u8; 32], AppError> {
    Ok(hash256(&serialize_transaction_unsigned(tx)?))
}

/// Canonical header serialization:
/// `version(u32=1) || height(u64) || prev_hash(32) || timestamp(i64) ||
///  merkle_root(32)`.
pub fn serialize_header(
    height: u64,
    prev_hash: &[u8],
    timestamp: i64,
    merkle_root: &[u8],
) -> Vec<u8> {
    let mut out = Vec::with_capacity(4 + 8 + 32 + 8 + 32);
    push_u32(&mut out, 1);
    push_u64(&mut out, height);
    out.extend_from_slice(prev_hash);
    push_i64(&mut out, timestamp);
    out.extend_from_slice(merkle_root);
    out
}

/// Double-SHA256 of the canonical block header.
pub fn block_hash(block: &Block, merkle_root: &[u8]) -> Result<[u8; 32], AppError> {
    let prev = decode_hex("prev_hash", &block.prev_hash, 32)?;
    Ok(hash256(&serialize_header(
        block.height,
        &prev,
        block.timestamp,
        merkle_root,
    )))
}

// ---------------------------------------------------------------------------
// Merkle trees
// ---------------------------------------------------------------------------

/// Real Bitcoin-style Merkle root: pair-wise double-SHA256 over ordered
/// leaves, with the last node duplicated when a level has odd length.
/// Empty input is an error at the call sites (blocks must have txs).
pub fn merkle_root(leaves: &[[u8; 32]]) -> [u8; 32] {
    assert!(!leaves.is_empty(), "merkle tree requires leaves");
    let mut level: Vec<[u8; 32]> = leaves.to_vec();
    while level.len() > 1 {
        let mut next = Vec::with_capacity(level.len().div_ceil(2));
        let mut i = 0;
        while i < level.len() {
            let left = &level[i];
            // Odd node: duplicate it.
            let right = if i + 1 < level.len() { &level[i + 1] } else { &level[i] };
            let mut buf = [0u8; 64];
            buf[..32].copy_from_slice(left);
            buf[32..].copy_from_slice(right);
            next.push(hash256(&buf));
            i += 2;
        }
        level = next;
    }
    level[0]
}

/// Ordered txids of a block.
pub fn block_txids(block: &Block) -> Result<Vec<[u8; 32]>, AppError> {
    block.txs.iter().map(txid).collect()
}

/// UTXO leaf in the state-root tree, domain-separated by tag `0x02` so it
/// cannot be confused with a Merkle node or a txid:
/// `tag || outpoint(36) || value(u64 LE) || address(32) || created_in(u64 LE)`.
pub fn state_leaf(
    txid: &[u8],
    vout: u32,
    value: u64,
    address: &[u8],
    created_in: u64,
) -> [u8; 32] {
    let mut buf = Vec::with_capacity(1 + 36 + 8 + 32 + 8);
    buf.push(0x02);
    buf.extend_from_slice(txid);
    push_u32(&mut buf, vout);
    push_u64(&mut buf, value);
    buf.extend_from_slice(address);
    push_u64(&mut buf, created_in);
    hash256(&buf)
}

/// Compute the UTXO state root from rows `(txid, vout, value, address, height)`.
/// Rows are sorted by `(txid, vout)` first so the root is order-independent of
/// storage iteration. The empty set hashes a single domain tag `0x03`.
pub fn state_root_from_rows(mut rows: Vec<([u8; 32], u32, u64, [u8; 32], u64)>) -> [u8; 32] {
    rows.sort_by(|a, b| a.0.cmp(&b.0).then(a.1.cmp(&b.1)));
    if rows.is_empty() {
        return hash256(&[0x03]);
    }
    let leaves: Vec<[u8; 32]> = rows
        .iter()
        .map(|(t, v, val, addr, h)| state_leaf(t, *v, *val, addr, *h))
        .collect();
    merkle_root(&leaves)
}
