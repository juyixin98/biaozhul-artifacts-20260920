//! UTXO rollback validator — library crate.
//!
//! Modules:
//! * [`types`]  — wire/domain types and the fixed subsidy rule
//! * [`crypto`] — real SHA-256 hashing, Merkle/state roots, Ed25519 verify
//! * [`wallet`] — key generation and signing helpers (CLI/tests/examples)
//! * [`storage`]— SQLite state, atomic connect/disconnect and history queries
//! * [`naive`]  — independent HashMap replay used as the cross-check
//! * [`api`]    — Axum handlers and router

pub mod api;
pub mod builder;
pub mod crypto;
pub mod error;
pub mod naive;
pub mod storage;
pub mod types;
pub mod wallet;

pub use error::{AppError, AppResult};
pub use storage::Storage;
