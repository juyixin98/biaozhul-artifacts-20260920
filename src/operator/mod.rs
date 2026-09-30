//! Query operators: the range-join logical plan and its two implementations.
//!
//! * [`plan`]      — [`JoinPredicate`], comparison operators and the typed
//!   [`JoinPlan`].
//! * [`reference`] — an independent, obviously-correct nested-loop engine used
//!   as the small-scale oracle in tests. It shares **no code** with the
//!   IEJoin implementation.
//! * [`iejoin`]    — the IEJoin-style batch backend: two sorted
//!   permutations per key, group-atomic candidate-bitmap updates and
//!   candidate-access counters.

pub(crate) mod bitset;
pub(crate) mod iejoin;
pub(crate) mod metrics;
pub(crate) mod plan;
pub(crate) mod reference;
pub(crate) mod sort;

pub use metrics::JoinMetrics;
pub use plan::{CmpOp, JoinPlan, JoinPredicate};
