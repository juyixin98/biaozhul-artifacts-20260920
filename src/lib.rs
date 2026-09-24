//! Disk-backed extendible hash page index with a pluggable hash function.
//!
//! * [`index::Index`] — the storage engine (single file, paged, crash safe).
//! * [`hash::HashKind`] — injectable hash functions, including a constant
//!   hash used to reproduce total collisions.
//! * [`server`] — Axum HTTP wrapper around a shared [`index::Index`].

pub mod error;
pub mod hash;
pub mod header;
pub mod index;
pub mod server;
pub mod store;

pub use error::IndexError;
pub use hash::HashKind;
pub use index::{Config, Index, Stats};
