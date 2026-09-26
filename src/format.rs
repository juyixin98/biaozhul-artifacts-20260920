//! Wire format constants and low-level token writing.
//!
//! Format (see README.md for the full specification):
//!
//! Header, 8 bytes:
//!   [0..4]  magic "LZSW"
//!   [4]     version = 1
//!   [5]     flags = 0
//!   [6]     window_log2 = 15 (window = 32768 bytes)
//!   [7]     reserved = 0
//!
//! Token stream (until end of input):
//!   tag < 0x80:  literal run of (tag + 1) raw bytes, 1..=128
//!   tag >= 0x80: match, length = (tag & 0x7f) + MIN_MATCH (3..=130),
//!                followed by a 2-byte big-endian distance (1..=32768)

pub const MAGIC: &[u8; 4] = b"LZSW";
pub const VERSION: u8 = 1;
pub const HEADER_LEN: usize = 8;

pub const WINDOW_LOG2: u8 = 15;
pub const WINDOW_SIZE: usize = 1 << WINDOW_LOG2; // 32768

pub const MIN_MATCH: usize = 3;
pub const MAX_MATCH: usize = 0x7F + MIN_MATCH; // 130
pub const MAX_LITERAL_RUN: usize = 0x80; // 128

pub const MATCH_TAG_BIT: u8 = 0x80;

/// Write the 8-byte stream header.
pub fn write_header(out: &mut Vec<u8>) {
    out.extend_from_slice(MAGIC);
    out.push(VERSION);
    out.push(0); // flags
    out.push(WINDOW_LOG2);
    out.push(0); // reserved
}

/// Append a literal-run token. `bytes.len()` must be 1..=128.
pub fn write_literal_run(out: &mut Vec<u8>, bytes: &[u8]) {
    debug_assert!(!bytes.is_empty() && bytes.len() <= MAX_LITERAL_RUN);
    out.push((bytes.len() - 1) as u8);
    out.extend_from_slice(bytes);
}

/// Append a match token. `len` must be 3..=130, `distance` 1..=32768.
pub fn write_match(out: &mut Vec<u8>, len: usize, distance: usize) {
    debug_assert!((MIN_MATCH..=MAX_MATCH).contains(&len));
    debug_assert!((1..=WINDOW_SIZE).contains(&distance));
    out.push(MATCH_TAG_BIT | (len - MIN_MATCH) as u8);
    out.extend_from_slice(&(distance as u16).to_be_bytes());
}
