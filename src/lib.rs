//! bpb — bit-packed integer block stream codec.
//!
//! Layout of a stream (all integers little-endian, see README.md for the full
//! format specification):
//!
//! ```text
//! header | block 0 | block 1 | ... | block N-1 | index
//! ```
//!
//! Each block stores `count` values as `base + delta`, where the signed delta
//! is zigzag-encoded and bit-packed with the block's bit width. Values whose
//! delta does not fit the bit width are stored in the block's escape table.

pub mod base64;
pub mod bitio;
pub mod decode;
pub mod encode;
pub mod error;
pub mod format;
pub mod json_io;

pub use decode::{decode_all, decode_block, inspect, BlockMeta, IndexEntry, Limits, StreamInfo};
pub use encode::{encode, EncodeOptions};
pub use error::Error;

/// Zigzag-encode a signed 64-bit integer into an unsigned 64-bit integer.
pub(crate) fn zigzag_encode(x: i64) -> u64 {
    ((x << 1) ^ (x >> 63)) as u64
}

/// Inverse of [`zigzag_encode`].
pub(crate) fn zigzag_decode(z: u64) -> i64 {
    ((z >> 1) as i64) ^ -((z & 1) as i64)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn zigzag_roundtrip_boundaries() {
        for v in [0, -1, 1, i64::MIN, i64::MAX, i64::MIN + 1, i64::MAX - 1, 42, -42] {
            assert_eq!(zigzag_decode(zigzag_encode(v)), v);
        }
        assert_eq!(zigzag_encode(0), 0);
        assert_eq!(zigzag_encode(-1), 1);
        assert_eq!(zigzag_encode(1), 2);
        assert_eq!(zigzag_encode(i64::MIN), u64::MAX);
        assert_eq!(zigzag_encode(i64::MAX), u64::MAX - 1);
    }
}
