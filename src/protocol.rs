//! Wire protocol types: block signatures and delta operations.
//!
//! JSON is the on-wire format. Literal block payloads are base64-encoded.
//! Strong hashes are lowercase hex-encoded BLAKE3 digests.

use serde::{Deserialize, Serialize};

use crate::checksum;

/// Signature of one block of the *basis* (old) artifact.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BlockSig {
    /// 32-bit weak rolling checksum of the block.
    pub weak: u32,
    /// BLAKE3 strong hash of the block (64 hex chars).
    pub strong_hex: String,
}

/// The signature of a basis artifact, produced by `POST /v1/signatures`.
///
/// The client passes this back unchanged to `POST /v1/deltas`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Signature {
    /// Fixed block size in bytes (last block may be shorter).
    pub block_size: usize,
    /// Total length of the basis artifact in bytes.
    pub basis_len: usize,
    /// Per-block signatures, in basis order.
    pub blocks: Vec<BlockSig>,
}

impl Signature {
    /// Build the signature of `basis` with the given block size.
    pub fn build(basis: &[u8], block_size: usize) -> Self {
        let blocks = basis
            .chunks(block_size)
            .map(|chunk| BlockSig {
                weak: checksum::weak(chunk),
                strong_hex: crate::hex::encode(&checksum::strong(chunk)),
            })
            .collect();
        Signature {
            block_size,
            basis_len: basis.len(),
            blocks,
        }
    }
}

/// One element of a delta: either raw new bytes or a copy of a basis block.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum Op {
    /// Insert raw bytes (base64-encoded `data`).
    Literal {
        #[serde(rename = "data_b64")]
        data_b64: String,
    },
    /// Copy basis block `block_index` verbatim into the output at this point.
    Copy { block_index: usize },
}

/// A delta that transforms the basis artifact into the target artifact.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Delta {
    pub block_size: usize,
    pub basis_len: usize,
    pub ops: Vec<Op>,
}

impl Delta {
    /// Size in bytes of the raw literal payload (decoded), i.e. genuinely new
    /// content that had to travel. This does not include base64/JSON overhead.
    pub fn literal_bytes(&self) -> usize {
        self.ops
            .iter()
            .map(|op| match op {
                Op::Literal { data_b64 } => {
                    // Decoded length of base64.
                    let s = data_b64.trim_end_matches('=');
                    s.len() * 3 / 4
                }
                Op::Copy { .. } => 0,
            })
            .sum()
    }

    /// Number of COPY ops (matched blocks).
    pub fn copy_count(&self) -> usize {
        self.ops
            .iter()
            .filter(|op| matches!(op, Op::Copy { .. }))
            .count()
    }
}

/// Request body of `POST /v1/patch`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PatchRequest {
    pub delta: Delta,
    /// Expected BLAKE3 hex digest of the reconstructed target. The service
    /// checks the result against it and fails loudly on mismatch.
    pub expected_blake3_hex: String,
}
