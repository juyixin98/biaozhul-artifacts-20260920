//! `bitpack` — a bit-packed integer block stream codec.
//!
//! A stream of `i64` values is split into fixed-size blocks. Each block
//! stores a base value plus bit-packed zigzag-encoded deltas; deltas that
//! do not fit the block's bit width escape into an exception table. A
//! trailing index allows decoding any single block directly. All
//! multi-byte integers are little-endian. See FORMAT.md for the byte-level
//! specification.

pub mod bitio;
pub mod block;
pub mod error;
pub mod limits;
pub mod stream;
pub mod zigzag;

pub use error::{Error, Result};
pub use limits::Limits;
pub use stream::{
    decode_block_at, decode_stream, encode_stream, inspect, IndexEntry, StreamHeader, StreamInfo,
};
