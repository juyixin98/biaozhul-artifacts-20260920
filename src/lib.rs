//! Merkle state proof service library crate.
//!
//! * [`core`] — service-side hashing spec & tree construction
//! * [`store`] — RocksDB versioned state, atomic commit/restart recovery
//! * [`proof`] — proof generation
//! * [`verifier`] — independent verifier (only depends on SHA-256 + the proof
//!   bytes + the trusted root)
//! * [`api`] — Axum JSON HTTP layer

pub mod api;
pub mod core;
pub mod error;
pub mod proof;
pub mod store;
pub mod verifier;
