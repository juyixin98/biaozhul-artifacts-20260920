//! # serialframe
//!
//! Custom binary serial-protocol frame library plus Axum HTTP replay service.
//!
//! * [`parser`] — incremental, memory-bounded frame parser and the real encoder.
//! * [`crc`]    — CRC-32/IEEE used by the frame trailer.
//! * [`server`]  — HTTP replay front-end (enabled by the crate's default deps).
//!
//! ## Frame layout
//!
//! see [`parser`] module documentation.

pub mod crc;
pub mod parser;

pub mod server;

pub use crc::Crc32;
pub use parser::{
    encode_frame, EncodeError, Event, FeedError, Frame, Parser, DEFAULT_MAX_PAYLOAD, HEADER_LEN,
    MAGIC0, MAGIC1, TRAILER_LEN,
};
