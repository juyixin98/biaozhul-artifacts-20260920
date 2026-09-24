//! License constraint propagation service.
//!
//! Pure rule engine: given a dependency graph whose packages declare SPDX
//! license-expression subsets, and a project policy (allow matrix, copyleft
//! classification, exception rules, incompatibility matrix), it
//!
//! 1. parses `AND` / `OR` / `WITH` expressions,
//! 2. propagates copyleft burden across static/dynamic links to a fixed
//!    point (dependency cycles included),
//! 3. picks a satisfying license for every OR-choice (or reports the
//!    unavoidable conflict paths).
//!
//! No legal conclusions are produced — see the disclaimer in every response.

pub mod engine;
pub mod policy;
pub mod spdx;

pub use engine::{
    describe_expression, evaluate, EdgeDetail, EvaluateRequest, EvaluateResponse, Selection,
    Violation,
};
pub use policy::PolicyInput;
