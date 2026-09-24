//! Block-level on-demand delta artifact transfer service.
//!
//! Crate layout:
//! - [`checksum`]: weak rolling checksum (rsync-style) and strong BLAKE3 hash.
//! - [`hex`]: lowercase hex helpers.
//! - [`protocol`]: JSON wire types ([`protocol::Signature`], [`protocol::Delta`]).
//! - [`delta`]: signature-indexed delta computation and patch application.
//! - [`store`]: in-memory artifact store.
//! - [`api`]: Axum HTTP routes.

pub mod api;
pub mod checksum;
pub mod delta;
pub mod error;
pub mod hex;
pub mod protocol;
pub mod store;

pub use error::{Error, Result};
