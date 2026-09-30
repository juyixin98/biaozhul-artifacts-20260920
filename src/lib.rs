//! Leapfrog Triejoin backend.
//!
//! Crate layout, each module carries real responsibility:
//!
//! - [`query::value`] / [`query::batch`] - the typed scalar domain and typed
//!   batches, including Arrow2 IPC conversion.
//! - [`query::trie`] - ordered tries with explicit row multiplicity.
//! - [`query::model`] - atoms, conjunctive queries and validated plans.
//! - [`query::oracle`] - an independent naive nested-loop reference engine used
//!   only for verification; it deliberately materializes intermediates.
//! - [`leapfrog`] - the Leapfrog Triejoin operator, resumable cursors and
//!   access accounting.
//! - [`state`] - the in-memory catalog, versions and server configuration.
//! - [`api`] - the Axum HTTP validation and execution entry points with
//!   request-scoped diagnostics.

pub mod api;
pub mod error;
pub mod fixtures;
pub mod leapfrog;
pub mod query;
pub mod state;
