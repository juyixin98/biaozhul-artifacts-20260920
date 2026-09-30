//! Column-value bitmap indexes with SQL three-valued-logic filtering.
//!
//! Layered architecture (each module is independently testable):
//! * [`bits`] — fixed-universe packed bitmaps with tail-bit hygiene.
//! * [`trivalent`] — TRUE/FALSE/UNKNOWN as three sets over one universe.
//! * [`scalar`] — typed scalar values and SQL comparison semantics.
//! * [`batch`] — typed record batches (arrow2) with a declared schema.
//! * [`index`] — per-column value bitmap indexes and composite indexes.
//! * [`predicate`] — the predicate AST (`cmp` / `is_null` / `and` / `or` / `not`).
//! * [`eval`] — query operators: predicate -> [`trivalent::TriPartition`].
//! * [`table`] — versioned tables: batches + indexes + deletion log.
//! * [`engine`] — catalog/resource state shared by all entry points.
//! * [`fixtures`] — deterministic synthetic data (no external systems).
//! * [`config`] — file/env configuration layer.
//! * [`server`] — axum HTTP validation entry.
//! * [`error`] — stable, machine-readable failure taxonomy.

pub mod batch;
pub mod bits;
pub mod config;
pub mod engine;
pub mod error;
pub mod eval;
pub mod fixtures;
pub mod index;
pub mod predicate;
pub mod scalar;
pub mod server;
pub mod table;
pub mod trivalent;
