//! # inc-utf8 — incremental UTF-8 validation and decoding
//!
//! A pure-Rust, dependency-free streaming UTF-8 library:
//!
//! * [`IncrementalDecoder`] validates and decodes UTF-8 delivered in
//!   arbitrary chunks, carrying at most 3 bytes of state across chunk
//!   boundaries. Overlong encodings, UTF-16 surrogates and values above
//!   U+10FFFF are rejected. [`IncrementalDecoder::finish`] reports a
//!   truncated final sequence together with its absolute byte offset in
//!   the original stream.
//! * [`encode_scalar`] / [`encode_all`] encode code points back to UTF-8,
//!   accepting only Unicode scalar values.
//! * [`Limits`] bound total input bytes and total emitted code points so
//!   memory and output size stay under caller control.
//! * [`Recovery`] selects the error strategy: fail-fast or
//!   report-and-resynchronize. Neither strategy ever performs silent
//!   replacement of corrupted bytes — every malformed sequence is
//!   surfaced as a structured [`DecodeError`] with exact offsets.
//!
//! The crate also ships a `utf8ctl` binary exposing the decoder through
//! a newline-delimited JSON control protocol (see `README.md`).

pub mod decoder;
pub mod encoder;
pub mod error;
pub mod hex;
pub mod json;

pub use decoder::{FeedOutcome, IncrementalDecoder, Limits, Recovery};
pub use encoder::{encode_all, encode_scalar};
pub use error::{DecodeError, EncodeError, ErrorKind};
