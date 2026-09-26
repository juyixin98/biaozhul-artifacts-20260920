//! `canohuff` — canonical Huffman coding as a streaming library plus a
//! JSON-controlled CLI (`chf`). Pure backend, zero external dependencies;
//! the core algorithm lives in [`table`].
//!
//! Stream format `CHF1` (all integers little-endian, codes MSB-first):
//!
//! ```text
//! offset  size        field
//! 0       4           magic "CHF1"
//! 4       8           original_len: u64, uncompressed size in bytes
//! 12      2           symbol_count: u16, number K of table entries
//! 14      2*K         entries: (symbol: u8, code_len: u8), sorted by symbol
//! 14+2K   ...         bitstream of canonical codes; final byte zero-padded
//! ```
//!
//! Canonical codes are derived from the length table alone: symbols sorted
//! by `(length, symbol)` receive consecutive codes per length class.

pub mod bitio;
pub mod decode;
pub mod encode;
pub mod error;
pub mod json;
pub mod table;

pub use decode::{decompress_slice, DecodeOptions, IncompletePolicy, StreamDecoder};
pub use encode::{compress_slice, count_frequencies, StreamEncoder};
pub use error::{Error, Result};
pub use table::{build_lengths, CodeTable, MAX_CODE_LEN};
