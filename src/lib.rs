//! # decorrelate
//!
//! Decorrelation compilation and execution service for a restricted set of
//! correlated subqueries: `EXISTS`, `NOT EXISTS` and scalar aggregate
//! subqueries (`COUNT`, `SUM`) plus bare scalar subqueries.
//!
//! Module layout (each carries real work; no hardcoded demo):
//!
//! * [`batch`]    — typed columnar batches over arrow2
//! * [`query`]    — logical model, validated plans, decorrelation rewrite
//! * [`catalog`]  — resource/state: typed relation catalog + synthetic fixtures
//! * [`exec`]     — naïve reference interpreter and the hash-pipeline
//! * [`api`]      — validation entry point, orchestration, HTTP, verification
//!
//! Every request is executed **twice** — by a literal per-row interpreter and
//! by the decorrelated set-oriented pipeline — and the results (or the error
//! category) must agree before anything is returned. See
//! `README.md` for the surface, semantics and limitations.

pub mod api;
pub mod batch;
pub mod catalog;
pub mod exec;
pub mod query;

pub use config::ServerConfig;

/// Process configuration (see `config/default.json`).
pub mod config;
