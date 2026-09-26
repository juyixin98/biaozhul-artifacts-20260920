//! Binary container format, version 1.
//!
//! The container is a self-describing byte stream that carries the coded
//! shards of one input blob. All multi-byte integers are **big-endian**.
//!
//! ```text
//! +------------------+---------------------------------------------+
//! | magic            | 4 bytes: ASCII "ECS1"                       |
//! +------------------+---------------------------------------------+
//! | header_len       | u32: length of header_json in bytes         |
//! | header_json      | header_len bytes, UTF-8 JSON (see below)    |
//! +------------------+---------------------------------------------+
//! | records ...      | zero or more framed records                 |
//! +------------------+---------------------------------------------+
//! | end-of-stream    | exactly one EOS record, always last         |
//! +------------------+---------------------------------------------+
//! ```
//!
//! ## Header JSON
//!
//! ```json
//! {
//!   "format": "ecstripe",
//!   "version": 1,
//!   "data_shards": 3,
//!   "parity_shards": 2,
//!   "stripe_size": 4096,
//!   "total_len": 10000,
//!   "sha256": "9f2f…(64 hex chars, optional)"
//! }
//! ```
//!
//! * `data_shards` (`k`) and `parity_shards` (`m`) satisfy `1 <= k`,
//!   `1 <= m`, `k + m <= 256`.
//! * `stripe_size` (`S`) is the full-stripe shard length in bytes.
//! * `total_len` (`L`) is the exact length of the original input. The number
//!   of stripes is `T = ceil(L / (k * S))` (zero when `L = 0`).
//! * `sha256` is optional. When present it is the lowercase-hex SHA-256 of
//!   the **complete original input**; the decoder verifies it after decoding.
//!   It exists because Reed-Solomon erasure decoding cannot detect silent
//!   corruption on its own.
//!
//! ## Records
//!
//! Every record starts with a one-byte tag:
//!
//! * `0x01` **chunk record** — one contiguous piece of one shard of one
//!   stripe:
//!
//!   ```text
//!   tag        u8   = 0x01
//!   stripe     u32  stripe index, 0-based
//!   shard      u8   shard id, 0..k-1 data, k..k+m-1 parity
//!   chunk_len  u32  number of payload bytes (never exceeds 2^20)
//!   payload    chunk_len bytes
//!   ```
//!
//!   Chunks of one `(stripe, shard)` must appear exactly once, in order and
//!   back-to-back; their concatenation is that shard. Duplicated chunks,
//!   gaps/overlaps and shards exceeding their expected length are rejected.
//!
//! * `0x02` **end-of-stream record** — exactly one, terminates the stream:
//!
//!   ```text
//!   tag           u8  = 0x02
//!   stripe_count  u32 number of stripes (`T`; must match the header)
//!   ```
//!
//! ## Shard lengths
//!
//! In every stripe except the last, all `n` shards are exactly `S` bytes.
//! In the last stripe, data shard `j` is `min(S, L - j*S)` bytes (zero is a
//! valid length and is carried by a single zero-length chunk record so that
//! its presence is explicit); the missing tail is conceptually zero-filled
//! for coding. Parity shards are always emitted with the full `S` bytes of
//! the zero-padded stripe. The decoder strips the padding from reconstructed
//! data shards using `total_len`.
//!
//! ## Limits enforced by the decoder
//!
//! * header length is capped;
//! * chunk payloads are capped at 2^20 bytes;
//! * stripe indices must be `< T` and shard ids `< n`;
//! * working memory and decoded output length are bounded by caller-supplied
//!   limits.

use serde::{Deserialize, Serialize};

use crate::error::{Error, Result};

/// Container magic bytes.
pub const MAGIC: &[u8; 4] = b"ECS1";
/// Format version implemented by this build.
pub const FORMAT_VERSION: u8 = 1;
/// Maximum accepted size of the JSON header (1 MiB).
pub const MAX_HEADER_LEN: u32 = 1 << 20;
/// Maximum payload of a single chunk record (1 MiB).
pub const MAX_CHUNK_LEN: u32 = 1 << 20;

/// Tag byte for chunk records.
pub const TAG_CHUNK: u8 = 0x01;
/// Tag byte for the end-of-stream record.
pub const TAG_EOS: u8 = 0x02;

/// JSON header describing the coding layout of a container.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Header {
    /// Fixed marker string; always `"ecstripe"`.
    pub format: String,
    /// Container format version.
    pub version: u8,
    /// Number `k` of data shards per stripe.
    pub data_shards: u16,
    /// Number `m` of parity shards per stripe.
    pub parity_shards: u16,
    /// Full-stripe shard length `S` in bytes.
    pub stripe_size: u32,
    /// Exact original input length `L` in bytes.
    pub total_len: u64,
    /// Optional lowercase-hex SHA-256 of the original input.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sha256: Option<String>,
}

impl Header {
    /// Construct a validated header for the given layout.
    pub fn new(k: u16, m: u16, stripe_size: u32, total_len: u64) -> Result<Self> {
        let header = Header {
            format: "ecstripe".to_string(),
            version: FORMAT_VERSION,
            data_shards: k,
            parity_shards: m,
            stripe_size,
            total_len,
            sha256: None,
        };
        header.validate()?;
        Ok(header)
    }

    /// Number of data shards as `usize`.
    pub fn k(&self) -> usize {
        self.data_shards as usize
    }

    /// Number of parity shards as `usize`.
    pub fn m(&self) -> usize {
        self.parity_shards as usize
    }

    /// Total shard count `n = k + m`.
    pub fn n(&self) -> usize {
        self.k() + self.m()
    }

    /// Stripe size as `usize`.
    pub fn stripe_size(&self) -> usize {
        self.stripe_size as usize
    }

    /// Number of stripes `T = ceil(L / (k * S))` (0 for an empty input).
    ///
    /// Each stripe packs `k` data shards of `S` bytes, i.e. `k * S` original
    /// bytes (the final stripe may be short).
    pub fn stripe_count(&self) -> u32 {
        let stripe_data = self.k() as u64 * self.stripe_size as u64;
        self.total_len.div_ceil(stripe_data) as u32
    }

    /// Expected on-wire length of shard `shard` in stripe `stripe`.
    ///
    /// Data shard `shard` of stripe `stripe` covers original bytes
    /// `(stripe * k + shard) * S ..`; its length is `min(S, L - offset)`.
    /// Parity shards are always emitted full-length.
    pub fn shard_len(&self, stripe: u32, shard: u8) -> u64 {
        if shard as usize >= self.k() {
            return self.stripe_size as u64;
        }
        let offset = (stripe as u64 * self.k() as u64 + shard as u64) * self.stripe_size as u64;
        (self.stripe_size as u64).min(self.total_len.saturating_sub(offset))
    }

    /// Cross-check field ranges and internal consistency.
    pub fn validate(&self) -> Result<()> {
        if self.format != "ecstripe" {
            return Err(Error::BadHeader(format!(
                "format field is {:?}, expected \"ecstripe\"",
                self.format
            )));
        }
        if self.version != FORMAT_VERSION {
            return Err(Error::UnsupportedVersion(self.version));
        }
        if self.data_shards == 0 {
            return Err(Error::BadHeader("data_shards must be >= 1".into()));
        }
        if self.parity_shards == 0 {
            return Err(Error::BadHeader("parity_shards must be >= 1".into()));
        }
        if self.data_shards as u32 + self.parity_shards as u32 > 256 {
            return Err(Error::BadHeader(
                "data_shards + parity_shards must be <= 256".into(),
            ));
        }
        if self.stripe_size == 0 {
            return Err(Error::BadHeader("stripe_size must be >= 1".into()));
        }
        if let Some(hash) = &self.sha256 {
            if hash.len() != 64 || !hash.bytes().all(|b| b.is_ascii_hexdigit()) {
                return Err(Error::BadHeader(
                    "sha256 must be 64 lowercase hex characters".into(),
                ));
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stripe_count_and_last_shard_lengths() {
        // k=3, S=10 -> each stripe holds 30 original bytes.
        // L=25: single stripe with data-shard lengths 10, 10, 5.
        let h = Header::new(3, 2, 10, 25).unwrap();
        assert_eq!(h.stripe_count(), 1);
        assert_eq!(h.shard_len(0, 0), 10);
        assert_eq!(h.shard_len(0, 1), 10);
        assert_eq!(h.shard_len(0, 2), 5);
        // Parity shards stay full-length even on the last stripe.
        assert_eq!(h.shard_len(0, 3), 10);
        assert_eq!(h.shard_len(0, 4), 10);

        // L=45 -> 2 stripes; second stripe data lengths 10, 5, 0.
        let h = Header::new(3, 2, 10, 45).unwrap();
        assert_eq!(h.stripe_count(), 2);
        assert_eq!(h.shard_len(0, 0), 10);
        assert_eq!(h.shard_len(1, 0), 10);
        assert_eq!(h.shard_len(1, 1), 5);
        assert_eq!(h.shard_len(1, 2), 0);
        assert_eq!(h.shard_len(1, 3), 10);

        // Exact multiple: the last stripe is a full stripe.
        let h = Header::new(3, 2, 10, 60).unwrap();
        assert_eq!(h.stripe_count(), 2);
        assert_eq!(h.shard_len(1, 2), 10);

        // Empty input: no stripes.
        let h = Header::new(3, 2, 10, 0).unwrap();
        assert_eq!(h.stripe_count(), 0);
    }
}
