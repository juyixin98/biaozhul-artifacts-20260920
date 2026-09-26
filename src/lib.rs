//! # rbitmap — run-length / hybrid-container u32 integer sets
//!
//! A pure-Rust, zero-dependency implementation of the hybrid
//! sparse-array + bitmap container scheme (the Roaring Bitmap family) for
//! sets of 32-bit unsigned integers, plus a streaming `RBM1` binary codec
//! with explicit memory/output limits and a stateless JSON control entry.
//!
//! * [`container`] — the two container representations and their switching.
//! * [`bitmap`] — the top-level set, set algebra, and binary serialization.
//! * [`codec`] — bounded varint / fixed-width readers and writers.
//! * [`api`] — the JSON request/response entry point.

pub mod api;
mod base64;
pub mod bitmap;
pub mod codec;
pub mod container;
pub mod error;
mod json;

pub use bitmap::{Bitmap, Limits};
pub use container::Container;
pub use error::{RbError, RbResult};
