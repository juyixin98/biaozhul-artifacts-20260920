//! manifest-selector library crate.
//!
//! Offline OCI-style multi-architecture manifest selection. See `main.rs`
//! for the executable entry point and `README.md` for the HTTP contract.

pub mod api;
pub mod digest;
pub mod error;
pub mod loader;
pub mod model;
pub mod reference;
pub mod select;
pub mod store;
