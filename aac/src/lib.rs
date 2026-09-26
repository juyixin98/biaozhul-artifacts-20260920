//! # aac — adaptive arithmetic coding
//!
//! A pure-Rust, dependency-free streaming binary codec with a self-describing
//! `AC01` container and a JSON control entry point (see the `aac` binary).
//!
//! * [`coder`] — integer-range arithmetic encoder/decoder with fixed E1/E2/E3
//!   renormalization, pending-bit carry handling and an explicit EOF symbol.
//! * [`model`] — adaptive order-0 model (256 byte symbols + EOF) with a
//!   Fenwick tree and a fixed halving rescale rule.
//! * [`bitio`] — MSB-first bit-at-a-time adapters over `Read`/`Write`.
//! * [`container`] — the documented `AC01` envelope with a trailing length
//!   footer and CRC-32 integrity check.
//! * [`jsonutil`] — minimal JSON and base64 support for the control entry.
//!
//! Memory use is bounded independently of input size: all streaming routines
//! work through `Read`/`Write` with fixed-size internal state (the model is a
//! fixed 257-entry table), and decoded output is always length-capped.

pub mod bitio;
pub mod coder;
pub mod constants;
pub mod container;
pub mod error;
pub mod jsonutil;
pub mod model;

pub use coder::{decode_bytes, encode_bytes, Decoder, Encoder, Stats};
pub use constants::DEFAULT_MAX_OUTPUT;
pub use container::{pack, pack_stream, unpack, unpack_stream, Crc32};
pub use error::Error;
pub use error::Result;
pub use model::Model;

/// Read chunk size used by the file-mode helpers (bounded memory).
pub const IO_CHUNK: usize = 64 * 1024;
