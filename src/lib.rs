//! IEJoin-style batch join backend.
//!
//! Crate layout (each boundary from the task is its own module):
//!
//! * [`types`] — typed columnar batches ([`types::batch::Batch`]) plus the
//!   Arrow2 IPC bridge ([`types::arrow`]);
//! * [`operator`] — the query operators: a small nested-loop reference
//!   ([`operator::nested_loop`]) and the two-predicate IEJoin engine
//!   ([`operator::iejoin`]) with its cost counters and shared predicates
//!   ([`operator::predicate`]);
//! * [`exec`] — budgets, run/job state, the join service, replay logging and
//!   deterministic synthetic fixtures;
//! * [`api`] — the Axum HTTP boundary with serialisable request/response DTOs.

pub mod api;
pub mod error;
pub mod exec;
pub mod operator;
pub mod types;

pub use error::{Error, ErrorKind, Result};
