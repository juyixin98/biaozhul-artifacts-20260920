//! OCI-style multi-architecture manifest selection service.
//!
//! Crate layout:
//! - [`digest`]: OCI digest parsing / SHA-256 verification
//! - [`model`]: OCI manifest / descriptor / platform JSON types
//! - [`store`]: in-memory content-addressed registry (blobs + tags)
//! - [`selector`]: deterministic multi-platform selection over the index tree
//! - [`fixtures`]: built-in demo repositories + on-disk fixture loader
//! - [`api`]: Axum HTTP service
//!
//! The service never contacts the network: every blob is either uploaded by
//! the client or loaded from a local directory. See `README.md`.

pub mod digest;
pub mod fixtures;
pub mod model;
pub mod selector;
pub mod store;

pub mod api;
