//! Content-addressable block repository — library crate.
//!
//! Module layout:
//! * [`hash`]   — SHA-256 (integrity checks only)
//! * [`json`]   — tiny JSON parser/serializer
//! * [`vfs`]    — injectable I/O layer with fault injection
//! * [`store`]  — file-backed block/root store with mark-sweep GC
//! * [`server`] — minimal HTTP verification entry point

pub mod hash;
pub mod json;
pub mod server;
pub mod store;
pub mod vfs;
