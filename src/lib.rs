//! IFIX — Immutable File Index format library.
//!
//! A pure-backend, streaming binary codec for a read-only tree index with a
//! multi-layer offset table (see `docs/FORMAT.md`). All offsets, counts and
//! ranges declared by an input file are validated before use; declared
//! sizes never drive allocation. The only third-party dependency is
//! `serde_json`; the binary format, CRC-32, base64 and index algorithms are
//! implemented in this crate.

pub mod base64;
pub mod control;
pub mod crc;
pub mod error;
pub mod format;
pub mod model;
pub mod reader;
pub mod storage;
pub mod toc;
pub mod validate;
pub mod writer;

pub use error::{IfixError, Result};
pub use format::Limits;
