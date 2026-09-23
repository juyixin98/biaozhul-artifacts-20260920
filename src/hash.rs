//! Real cryptographic hashing (SHA-256, Bitcoin-style double SHA-256).
//!
//! Every identifier in the protocol — transaction ids, block hashes, the
//! Merkle root and the UTXO state root — is produced by actually executing
//! `sha256(sha256(...))` over a canonical byte encoding. Nothing is stubbed.

use sha2::{Digest, Sha256};

use crate::model::Block;

/// A 32-byte cryptographic hash, displayed as lowercase hex.
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Hash32(pub [u8; 32]);

impl Hash32 {
    pub const ZERO: Hash32 = Hash32([0u8; 32]);

    pub fn from_hex(s: &str) -> Result<Self, hex::FromHexError> {
        let v = hex::decode(s)?;
        let a: [u8; 32] = v
            .try_into()
            .map_err(|_| hex::FromHexError::InvalidStringLength)?;
        Ok(Hash32(a))
    }

    pub fn to_hex(&self) -> String {
        hex::encode(self.0)
    }

    pub fn as_bytes(&self) -> &[u8; 32] {
        &self.0
    }
}

impl std::fmt::Debug for Hash32 {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Hash32({})", self.to_hex())
    }
}

impl std::fmt::Display for Hash32 {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.to_hex())
    }
}

impl serde::Serialize for Hash32 {
    fn serialize<S: serde::Serializer>(&self, s: S) -> std::result::Result<S::Ok, S::Error> {
        s.serialize_str(&self.to_hex())
    }
}

impl<'de> serde::Deserialize<'de> for Hash32 {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> std::result::Result<Self, D::Error> {
        let s = String::deserialize(d)?;
        Hash32::from_hex(&s).map_err(serde::de::Error::custom)
    }
}

/// Double SHA-256, the hash used everywhere in this protocol.
pub fn hash256(bytes: &[u8]) -> Hash32 {
    let first = Sha256::digest(bytes);
    let second = Sha256::digest(first);
    let mut out = [0u8; 32];
    out.copy_from_slice(&second);
    Hash32(out)
}

/// Single SHA-256 (exposed for tests / inspection).
pub fn sha256(bytes: &[u8]) -> Hash32 {
    let d = Sha256::digest(bytes);
    let mut out = [0u8; 32];
    out.copy_from_slice(&d);
    Hash32(out)
}

/// Bitcoin-style Merkle root over double-SHA256 leaf hashes.
///
/// * no leaves  -> `hash256(b"merkle:")` (well-defined empty root)
/// * odd node   -> duplicated before pairing
/// * one leaf   -> that leaf itself
pub fn merkle_root(mut level: Vec<Hash32>) -> Hash32 {
    match level.len() {
        0 => hash256(b"merkle:"),
        1 => level.remove(0),
        _ => {
            while level.len() > 1 {
                if level.len() % 2 == 1 {
                    let last = *level.last().unwrap();
                    level.push(last);
                }
                let mut next = Vec::with_capacity(level.len() / 2);
                let mut i = 0;
                while i < level.len() {
                    let mut buf = [0u8; 64];
                    buf[..32].copy_from_slice(level[i].as_bytes());
                    buf[32..].copy_from_slice(level[i + 1].as_bytes());
                    next.push(hash256(&buf));
                    i += 2;
                }
                level = next;
            }
            level.remove(0)
        }
    }
}

/// Deterministic hash of a block header; identifies a block on the chain.
pub fn block_hash(block: &Block, merkle: &Hash32) -> Hash32 {
    // Hand-rolled canonical encoding with fixed 4/8-byte integers and
    // length-prefixed fields, so the hash never depends on serde formatting.
    let mut buf = Vec::with_capacity(128);
    buf.extend_from_slice(b"utxo-block:");
    buf.extend_from_slice(&block.version.to_be_bytes());
    buf.extend_from_slice(&block.height.to_be_bytes());
    buf.extend_from_slice(block.prev_hash.as_bytes());
    buf.extend_from_slice(merkle.as_bytes());
    buf.extend_from_slice(&block.timestamp.to_be_bytes());
    hash256(&buf)
}
