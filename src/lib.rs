//! Bounded-memory external sort — library crate.
//!
//! Module map:
//! - [`crc32`]      — CRC-32 used by every integrity check
//! - [`fs`]         — injectable storage layer (`Fs`/`RFile`/`WFile`, faults)
//! - [`format`]     — on-disk segment format (frames + CRC + trailer)
//! - [`lreader`]    — streaming line reader with unbounded single-line growth
//! - [`manifest`]   — journal/recovery state
//! - [`key`]        — composite key spec and stable comparator
//! - [`engine`]     — run formation, multi-way merge, recovery
//! - [`server`]     — local HTTP validation endpoint

pub mod crc32;
pub mod engine;
pub mod error;
pub mod format;
pub mod fs;
pub mod key;
pub mod lreader;
pub mod manifest;
pub mod server;

pub use engine::{Engine, SortConfig, SortStats};
pub use error::{Error, Result};
pub use key::{Direction, KeyPart, KeySpec};
