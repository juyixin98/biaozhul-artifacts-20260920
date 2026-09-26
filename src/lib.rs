//! incutf8 — incremental (streaming) UTF-8 validation and decoding.
//!
//! All core algorithms (UTF-8 state machine, UTF-8 encoder, Base64, JSON)
//! are implemented in this crate with no external dependencies.
//!
//! * [`Decoder`] — feed input in arbitrarily-sized chunks; multi-byte
//!   sequences may straddle chunk boundaries. Finish with
//!   [`Decoder::finish`] to catch truncated final sequences.
//! * [`encode_codepoint`] / [`encode_str`] — the matching encoder.
//! * [`json`] — the JSON subset used by the control protocol.
//! * [`base64`] — the Base64 codec used to carry binary chunks in JSON.

pub mod base64;
pub mod decoder;
pub mod encoder;
pub mod error;
pub mod json;

pub use decoder::{decode_all, is_valid, DecodeReport, Decoder, ErrorPolicy, DEFAULT_MAX_OUTPUT_CODEPOINTS};
pub use encoder::{encode_codepoint, encode_str};
pub use error::{DecodeError, EncodeError, EncodeErrorKind, ErrorKind};
