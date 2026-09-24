//! Block-level on-demand artifact delta transfer.
//!
//! * [`weak`] — rolling weak checksum (locator only)
//! * [`signature`] — per-block (weak, BLAKE3) records and binary encoding
//! * [`delta`] — rolling matcher: weak locate, strong confirm
//! * [`patch`] — patch encoding, application with full verification
//! * [`store`] — in-memory artifact storage
//! * [`api`] — Axum HTTP service

pub mod api;
pub mod delta;
pub mod error;
pub mod patch;
pub mod signature;
pub mod store;
pub mod weak;

pub use delta::{diff, Delta, DeltaStats, Op};
pub use error::{Error, Result};
pub use patch::{apply_patch, Patch};
pub use signature::{default_block_len, strong_hash, Signature};
