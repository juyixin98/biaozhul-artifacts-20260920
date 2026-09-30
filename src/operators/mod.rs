//! Query operators: predicate evaluation under SQL three-valued logic and
//! per-correlated-group aggregate computation.
//!
//! These operators are shared by BOTH execution strategies:
//!   * [`crate::interpreter`] binds one outer row and scans the inner table;
//!   * [`crate::decorrel`] binds one outer row and the group of inner rows the
//!     decorrelation hash table produced for its correlation key.
//! Same operator code => any discrepancy is a compiler bug, not an eval bug.

pub mod aggregate;
pub mod emit;
pub mod predicate;
pub mod runner;
