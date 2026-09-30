//! IEJoin-style batch range-join backend.
//!
//! Crate layout (data and error contracts cross module boundaries explicitly):
//!
//! * [`batch`]      — typed columnar batches, NULL/NaN semantics, arrow2 IO.
//! * [`error`]      — four-category structured error taxonomy.
//! * [`operator`]   — query plan, nested-loop oracle, sort permutations,
//!                    candidate bitset, IEJoin engine and deterministic metrics.
//! * [`resource`]   — budgets, output truncation, controlled paging.
//! * [`state`]      — stateful run registry (appends, cursor conflicts).
//! * [`fixtures`]   — deterministic synthetic data generators.
//! * [`http`]       — Axum JSON validation entry.

pub mod batch;
pub mod error;
pub mod fixtures;
pub mod http;
pub mod operator;
pub mod resource;
pub mod state;

pub use operator::iejoin::{explain, iejoin, JoinExplain, KeyState};
pub use operator::metrics::JoinMetrics;
pub use operator::plan::{CmpOp, JoinPlan, JoinPredicate};
pub use operator::reference::{nested_loop, SinkReply};
pub use resource::{
    execute_page, result_size, CompletionReason, Engine, PageOutcome, PageRequest,
    ResourceBudget,
};
pub use state::{Run, RunRegistry, RunStatus, Side};
