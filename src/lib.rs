//! Leapfrog Triejoin backend.
//!
//! Crate layout (each module has a real responsibility, see `docs/architecture.md`):
//! - [`types`] / [`batch`]: typed, domain-encoded data and Arrow2 conversion.
//! - [`trie`]: ordered tries built per compatible variable order, with explicit multiplicity.
//! - [`plan`]: query model, variable ordering and validation (the "restricted multi-table" gate).
//! - [`leapfrog`]: the Leapfrog Triejoin operator with instrumented seeks and resumable bounds.
//! - [`naive`]: independent nested-loop reference enumeration (oracle, shared nothing with the core).
//! - [`executor`]: pages, limits, resume cursors, statistics.
//! - [`dataset`]: deterministic local synthetic fixtures + CSV (Arrow2) loading.
//! - [`state`] / [`api`] / [`diagnostics`] / [`config`]: resources, HTTP entry point and diagnostics.

pub mod api;
pub mod batch;
pub mod config;
pub mod dataset;
pub mod diagnostics;
pub mod error;
pub mod executor;
pub mod leapfrog;
pub mod naive;
pub mod plan;
pub mod state;
pub mod trie;
pub mod types;
pub mod verify;

pub use error::{JoinError, Result};
