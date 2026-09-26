//! On-disk format constants. All multi-byte integers are little-endian.
//!
//! Stream header (28 bytes):
//!
//! | offset | size | field        |
//! |--------|------|--------------|
//! | 0      | 4    | magic "BPB1" |
//! | 4      | 1    | version (=1) |
//! | 5      | 1    | flags (=0)   |
//! | 6      | 2    | reserved (=0)|
//! | 8      | 4    | block_count  |
//! | 12     | 8    | total_values |
//! | 20     | 8    | index_offset |
//!
//! Block (at offsets recorded in the index):
//!
//! | size              | field                                  |
//! |-------------------|----------------------------------------|
//! | 4                 | count (1..=MAX_BLOCK_VALUES)           |
//! | 8                 | base (i64)                             |
//! | 1                 | bit_width (0..=64)                     |
//! | 4                 | escape_count (0..=count)               |
//! | ceil(count*bw/8)  | packed zigzag deltas (LSB-first)       |
//! | 12 * escape_count | escape entries (index u32, value i64)  |
//!
//! Index (at `index_offset`, `block_count` entries of 20 bytes):
//!
//! | size | field                              |
//! |------|------------------------------------|
//! | 8    | absolute offset of the block       |
//! | 8    | first_value: cumulative value index|
//! | 4    | count of values in the block       |

pub const MAGIC: &[u8; 4] = b"BPB1";
pub const VERSION: u8 = 1;
pub const HEADER_LEN: usize = 28;
pub const INDEX_ENTRY_LEN: usize = 20;
pub const BLOCK_HEADER_LEN: usize = 17;
pub const ESCAPE_ENTRY_LEN: usize = 12;
/// Hard upper bound for values per block, enforced by both encoder and decoder.
pub const MAX_BLOCK_VALUES: u32 = 256;
/// Default block size used by the encoder.
pub const DEFAULT_BLOCK_SIZE: u32 = 128;
