//! Execution layer.
//!
//! Two strategies execute the same validated plan:
//!
//! * [`naive`] — the **reference semantics**: a literal per-outer-row
//!   interpreter that scans the inner relation for each outer row and applies
//!   the correlation predicate conjunct by conjunct.
//! * [`pipeline`] — the **decorrelated** execution: hash-build the inner
//!   groups once, then LEFT OUTER hash-probe and reduce per the rewrite.
//!
//! Both share [`operators::reduce_group`] so the aggregation/scalar rule is
//! defined once; they differ only in how the matched set of an outer row is
//! obtained. The service runs both and fails closed if they ever disagree
//! (see `verify`).

pub mod naive;
pub mod operators;
pub mod pipeline;

use crate::batch::{Datum, Field};
use serde::Serialize;

/// One appended result column, positional over outer rows.
#[derive(Debug, Clone)]
pub struct AppendedColumn {
    pub field: Field,
    pub values: Vec<Datum>,
}

/// A single traceable event produced while executing a strategy.
#[derive(Debug, Clone, Serialize)]
pub struct TraceEvent {
    /// Processing step label, e.g. `hash_build`, `probe`, `reduce`, `scan_nested`.
    pub step: String,
    pub detail: String,
    /// Number of rows affected at this step (group count, matched count, ...).
    pub rows: usize,
}

/// Output of one execution strategy. On error the strategy reports the first
/// failing outer row (`at_outer_row`) and may retain columns computed up to
/// that point for diagnostics.
#[derive(Debug, Clone)]
pub struct StrategyOutput {
    pub strategy: &'static str,
    pub appended: Vec<AppendedColumn>,
    pub trace: Vec<TraceEvent>,
    pub error: Option<ExecError>,
}

/// Serializable error view shared with the API response.
#[derive(Debug, Clone, Serialize)]
pub struct ExecError {
    pub category: String,
    pub code: String,
    pub message: String,
    pub at_step: String,
    pub at_outer_row: Option<usize>,
    pub detail: Option<serde_json::Value>,
}

impl ExecError {
    pub fn from_query_error(e: &crate::query::QueryError) -> Self {
        ExecError {
            category: format!("{:?}", e.category),
            code: e.code.to_string(),
            message: e.message.clone(),
            at_step: e.at_step.clone(),
            at_outer_row: e
                .detail
                .as_ref()
                .and_then(|d| d.get("outer_row"))
                .and_then(|v| v.as_u64())
                .map(|n| n as usize),
            detail: e.detail.clone(),
        }
    }
}

impl StrategyOutput {
    pub fn is_ok(&self) -> bool {
        self.error.is_none()
    }
}
