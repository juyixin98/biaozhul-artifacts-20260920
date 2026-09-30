//! Query model, typed data carriers and the two join implementations.
//!
//! - [`value`]: typed scalar domain.
//! - [`batch`]: typed ingestion batches and Arrow2 IPC support.
//! - [`trie`]: ordered tries with explicit multiplicity.
//! - [`model`]: atoms, conjunctive queries and resolved plans.
//! - [`oracle`]: independent naive nested-loops reference used only for
//!   verification; it deliberately materializes large intermediates.

pub mod batch;
pub mod model;
pub mod oracle;
pub mod trie;
pub mod value;
