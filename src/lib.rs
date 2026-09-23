//! Library crate for the exact-integer multi-hop swap quote router.
//! The `quote-router` binary in `src/main.rs` is a thin wrapper over this.

pub mod amount;
pub mod api;
pub mod db;
pub mod model;
pub mod router;
pub mod swap;
