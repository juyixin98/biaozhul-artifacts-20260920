//! lzsw — streaming LZ77 sliding-window codec.
//!
//! - 32 KiB window, match length 3..=130, literal runs 1..=128.
//! - Decoder enforces a hard output budget and validates every back
//!   reference, so hostile input cannot expand without bound or read
//!   outside already-produced output.
//! - Wire format documented in README.md and [`format`].

pub mod base64;
pub mod decoder;
pub mod encoder;
pub mod error;
pub mod format;
pub mod json;

pub use decoder::{decompress, Decoder};
pub use encoder::{compress, Encoder};
pub use error::Error;
