//! Signature of an *old* artifact: one (weak, strong) record per fixed-size
//! block, plus a trailing short block if the basis length is not a multiple.
//!
//! Wire format (little-endian, deterministic):
//!
//! ```text
//! magic     4 bytes  b"ADLS"
//! version   u16      = 1
//! block_len u32      block size S (1..=65536)
//! count     u32      number of block records
//! per record:
//!   index      u32
//!   length     u32   (S for all blocks, smaller only for the last one)
//!   weak       u32   rolling checksum
//!   strong     32 bytes BLAKE3 of the block
//! ```

use crate::error::{Error, Result};
use crate::weak::Rollsum;
use std::collections::HashMap;

pub const MAGIC: &[u8; 4] = b"ADLS";
pub const VERSION: u16 = 1;
pub const HEADER_LEN: usize = 4 + 2 + 4 + 4;
pub const RECORD_LEN: usize = 4 + 4 + 4 + 32;
pub const MAX_BLOCK_SIZE: u32 = 65536;
pub const MIN_BLOCK_SIZE: u32 = 1;

/// Strong digest over a byte slice — BLAKE3, 32 bytes. Content equality is
/// decided ONLY by this digest, never by the weak rolling checksum.
#[inline]
pub fn strong_hash(data: &[u8]) -> [u8; 32] {
    *blake3::hash(data).as_bytes()
}

/// One block record as produced from the old artifact.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BlockSig {
    pub index: u32,
    pub length: u32,
    pub weak: u32,
    pub strong: [u8; 32],
}

/// Decoded signature of an old artifact.
#[derive(Debug, Clone)]
pub struct Signature {
    pub block_len: u32,
    pub blocks: Vec<BlockSig>,
}

/// Choose a sensible default block size, like rsync:
/// `clamp(ceil(len / 10000), 64, 65536)`, with a minimum of 64.
pub fn default_block_len(basis_len: u64) -> u32 {
    let s = basis_len.div_ceil(10_000).clamp(64, MAX_BLOCK_SIZE as u64);
    s as u32
}

impl Signature {
    /// Build the signature of `basis` using fixed block size `block_len`.
    pub fn build(basis: &[u8], block_len: u32) -> Result<Signature> {
        if !(MIN_BLOCK_SIZE..=MAX_BLOCK_SIZE).contains(&block_len) {
            return Err(Error::BadBlockSize(block_len));
        }
        let s = block_len as usize;
        let mut blocks = Vec::with_capacity(basis.len().div_ceil(s).max(1));
        for (index, chunk) in basis.chunks(s).enumerate() {
            blocks.push(BlockSig {
                index: index as u32,
                length: chunk.len() as u32,
                weak: Rollsum::checksum(chunk),
                strong: strong_hash(chunk),
            });
        }
        Ok(Signature { block_len, blocks })
    }

    /// Serialize to the compact binary wire format.
    pub fn encode(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(HEADER_LEN + self.blocks.len() * RECORD_LEN);
        out.extend_from_slice(MAGIC);
        out.extend_from_slice(&VERSION.to_le_bytes());
        out.extend_from_slice(&self.block_len.to_le_bytes());
        out.extend_from_slice(&(self.blocks.len() as u32).to_le_bytes());
        for b in &self.blocks {
            out.extend_from_slice(&b.index.to_le_bytes());
            out.extend_from_slice(&b.length.to_le_bytes());
            out.extend_from_slice(&b.weak.to_le_bytes());
            out.extend_from_slice(&b.strong);
        }
        out
    }

    /// Parse and strictly validate a signature byte stream.
    pub fn decode(buf: &[u8]) -> Result<Signature> {
        if buf.len() < HEADER_LEN || &buf[..4] != MAGIC {
            return Err(Error::Malformed("bad signature magic"));
        }
        let version = u16::from_le_bytes([buf[4], buf[5]]);
        if version != VERSION {
            return Err(Error::Malformed("unsupported signature version"));
        }
        let block_len = u32::from_le_bytes(buf[6..10].try_into().unwrap());
        if !(MIN_BLOCK_SIZE..=MAX_BLOCK_SIZE).contains(&block_len) {
            return Err(Error::BadBlockSize(block_len));
        }
        let count = u32::from_le_bytes(buf[10..14].try_into().unwrap()) as usize;
        let need = HEADER_LEN
            .checked_add(count.checked_mul(RECORD_LEN).ok_or(Error::Malformed("count overflow"))?)
            .ok_or(Error::Malformed("length overflow"))?;
        if buf.len() != need {
            return Err(Error::Malformed("signature length does not match record count"));
        }
        let mut blocks = Vec::with_capacity(count);
        let mut pos = HEADER_LEN;
        for expected in 0..count {
            let index = u32::from_le_bytes(buf[pos..pos + 4].try_into().unwrap());
            if index as usize != expected {
                return Err(Error::Malformed("block index must be contiguous from 0"));
            }
            let length = u32::from_le_bytes(buf[pos + 4..pos + 8].try_into().unwrap());
            if length == 0 || length > block_len {
                return Err(Error::Malformed("block length out of range"));
            }
            // Only the final block may be short.
            if length < block_len && expected + 1 != count {
                return Err(Error::Malformed("non-final block must be full length"));
            }
            let weak = u32::from_le_bytes(buf[pos + 8..pos + 12].try_into().unwrap());
            let mut strong = [0u8; 32];
            strong.copy_from_slice(&buf[pos + 12..pos + 44]);
            blocks.push(BlockSig { index, length, weak, strong });
            pos += RECORD_LEN;
        }
        Ok(Signature { block_len, blocks })
    }
}

/// Index built over a signature for the rolling matcher.
///
/// Duplicate weak checksums (repeated blocks in the old artifact) keep ALL
/// candidates — the correct one is picked only after comparing strong digests.
#[derive(Debug, Clone, Default)]
pub struct SigIndex {
    pub block_len: usize,
    pub by_weak: HashMap<u32, Vec<u32>>,
    pub blocks: Vec<BlockSig>,
}

impl SigIndex {
    pub fn new(sig: Signature) -> Self {
        let mut by_weak: HashMap<u32, Vec<u32>> = HashMap::with_capacity(sig.blocks.len());
        for b in &sig.blocks {
            by_weak.entry(b.weak).or_default().push(b.index);
        }
        SigIndex { block_len: sig.block_len as usize, by_weak, blocks: sig.blocks }
    }

    /// Candidates sharing a weak checksum (possibly several — duplicates or
    /// collisions). Strong confirmation is still required by the caller.
    #[inline]
    pub fn weak_hits(&self, weak: u32) -> &[u32] {
        self.by_weak
            .get(&weak)
            .map(|v| v.as_slice())
            .unwrap_or(&[])
    }
}
