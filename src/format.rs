//! Binary container format `ECSR` (version 1).
//!
//! All integers are little-endian. The layout is:
//!
//! ```text
//! Header (20 bytes):
//!   magic         [u8; 4]   ASCII "ECSR"
//!   version       u8        currently 1
//!   data_shards   u8        k, number of systematic (data) shards, 1..=255
//!   parity_shards u8        m, number of parity shards, 1..=255
//!   flags         u8        reserved, must be 0
//!   stripe_size   u32       bytes per shard block per stripe, 1..=MAX_STRIPE_SIZE
//!   original_len  u64       length of the original input in bytes
//!
//! Body: exactly num_stripes = ceil(original_len / (k * stripe_size)) stripes
//!       (0 stripes when original_len == 0). Per stripe, k+m blocks in shard
//!       index order (0..k are data, k..k+m are parity):
//!   block_len     u32       must equal stripe_size
//!   block         [u8; block_len]
//! ```
//!
//! The last stripe's data region is zero-padded up to `k * stripe_size`
//! before encoding; decoders MUST truncate the output to `original_len`.
//!
//! The format carries NO integrity protection: a silently corrupted shard
//! decodes to silently wrong output. Integrity must be checked externally
//! (e.g. the `expected_sha256` field of the JSON decode request).

use crate::error::{Error, Result};
use std::io::{Read, Write};

pub const MAGIC: &[u8; 4] = b"ECSR";
pub const VERSION: u8 = 1;
pub const HEADER_LEN: usize = 20;

/// Hard limits enforced on BOTH encode and decode paths. Decoding re-checks
/// them because a container is untrusted input.
pub const MAX_DATA_SHARDS: u32 = 255;
pub const MAX_PARITY_SHARDS: u32 = 255;
pub const MAX_TOTAL_SHARDS: u32 = 255;
pub const MAX_STRIPE_SIZE: u64 = 1 << 24; // 16 MiB per shard block
/// Default cap on bytes held in memory per stripe: (k+m) * stripe_size.
pub const DEFAULT_MAX_STRIPE_MEMORY: u64 = 1 << 26; // 64 MiB
/// Default cap on decoded output length.
pub const DEFAULT_MAX_OUTPUT_BYTES: u64 = 1 << 30; // 1 GiB
/// Hard ceiling for the caller-tunable decoded output cap.
pub const HARD_MAX_OUTPUT_BYTES: u64 = 1 << 34; // 16 GiB

/// Parsed container header.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Header {
    pub data_shards: u8,
    pub parity_shards: u8,
    pub stripe_size: u32,
    pub original_len: u64,
}

impl Header {
    pub fn total_shards(&self) -> u32 {
        self.data_shards as u32 + self.parity_shards as u32
    }

    /// Bytes of original data covered by one stripe.
    pub fn stripe_data_bytes(&self) -> u64 {
        self.data_shards as u64 * self.stripe_size as u64
    }

    pub fn num_stripes(&self) -> u64 {
        let per = self.stripe_data_bytes();
        self.original_len.div_ceil(per)
    }

    /// Validate header fields against the hard limits. `max_stripe_memory`
    /// caps (k+m) * stripe_size so one stripe always fits a bounded buffer.
    pub fn validate(&self, max_stripe_memory: u64) -> Result<()> {
        let k = self.data_shards as u32;
        let m = self.parity_shards as u32;
        if k == 0 || k > MAX_DATA_SHARDS {
            return Err(Error::InvalidParams(format!(
                "data_shards must be 1..={MAX_DATA_SHARDS}, got {k}"
            )));
        }
        if m == 0 || m > MAX_PARITY_SHARDS {
            return Err(Error::InvalidParams(format!(
                "parity_shards must be 1..={MAX_PARITY_SHARDS}, got {m}"
            )));
        }
        if k + m > MAX_TOTAL_SHARDS {
            return Err(Error::InvalidParams(format!(
                "data_shards + parity_shards must be <= {MAX_TOTAL_SHARDS}, got {}",
                k + m
            )));
        }
        if self.stripe_size == 0 || self.stripe_size as u64 > MAX_STRIPE_SIZE {
            return Err(Error::InvalidParams(format!(
                "stripe_size must be 1..={MAX_STRIPE_SIZE}, got {}",
                self.stripe_size
            )));
        }
        let stripe_mem = (k + m) as u64 * self.stripe_size as u64;
        if stripe_mem > max_stripe_memory {
            return Err(Error::InvalidParams(format!(
                "per-stripe memory (k+m)*stripe_size = {stripe_mem} exceeds limit {max_stripe_memory}"
            )));
        }
        Ok(())
    }

    pub fn write_to<W: Write>(&self, mut w: W) -> Result<()> {
        let mut buf = [0u8; HEADER_LEN];
        buf[0..4].copy_from_slice(MAGIC);
        buf[4] = VERSION;
        buf[5] = self.data_shards;
        buf[6] = self.parity_shards;
        buf[7] = 0; // flags
        buf[8..12].copy_from_slice(&self.stripe_size.to_le_bytes());
        buf[12..20].copy_from_slice(&self.original_len.to_le_bytes());
        w.write_all(&buf)?;
        Ok(())
    }

    pub fn read_from<R: Read>(mut r: R) -> Result<Header> {
        let mut buf = [0u8; HEADER_LEN];
        r.read_exact(&mut buf).map_err(|e| match e.kind() {
            std::io::ErrorKind::UnexpectedEof => {
                Error::CorruptFormat("truncated header".to_string())
            }
            _ => Error::Io(e),
        })?;
        if &buf[0..4] != MAGIC {
            return Err(Error::BadMagic);
        }
        if buf[4] != VERSION {
            return Err(Error::UnsupportedVersion(buf[4]));
        }
        if buf[7] != 0 {
            return Err(Error::CorruptFormat("flags byte must be 0".to_string()));
        }
        Ok(Header {
            data_shards: buf[5],
            parity_shards: buf[6],
            stripe_size: u32::from_le_bytes(buf[8..12].try_into().unwrap()),
            original_len: u64::from_le_bytes(buf[12..20].try_into().unwrap()),
        })
    }
}

/// Write one length-prefixed block.
pub fn write_block<W: Write>(mut w: W, block: &[u8]) -> Result<()> {
    w.write_all(&(block.len() as u32).to_le_bytes())?;
    w.write_all(block)?;
    Ok(())
}

/// Read one length-prefixed block, checking the prefix against the header's
/// stripe size. Returns `None` on clean EOF before the prefix (used to detect
/// the exact end of the body); a prefix that is cut short is an error.
pub fn read_block<R: Read>(mut r: R, stripe_size: u32, buf: &mut [u8]) -> Result<bool> {
    let mut len_buf = [0u8; 4];
    match r.read_exact(&mut len_buf) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => {
            return Err(Error::CorruptFormat(
                "unexpected end of stream inside block table".to_string(),
            ));
        }
        Err(e) => return Err(Error::Io(e)),
    }
    let block_len = u32::from_le_bytes(len_buf);
    if block_len != stripe_size {
        return Err(Error::LengthMismatch {
            expected: stripe_size,
            actual: block_len,
        });
    }
    debug_assert_eq!(buf.len(), stripe_size as usize);
    r.read_exact(buf).map_err(|e| match e.kind() {
        std::io::ErrorKind::UnexpectedEof => {
            Error::CorruptFormat("truncated block body".to_string())
        }
        _ => Error::Io(e),
    })?;
    Ok(true)
}
