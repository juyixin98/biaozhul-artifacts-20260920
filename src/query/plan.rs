//! Validated, name-resolved logical plans.
//!
//! A [`ValidatedQuery`] is produced by the validation entry point
//! ([`crate::api::service`]) and consumed by both execution strategies and by
//! the decorrelation rewriter. Name resolution and type checks have already
//! happened, so operators only see positional indices and concrete types.

use crate::batch::{Batch, ColType, Field};
use crate::query::{AggFunc, SubqueryKind};
use std::collections::HashMap;

/// One resolved correlation conjunct `outer_key = inner_key`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KeyPair {
    pub outer_index: usize,
    pub inner_index: usize,
    pub col_type: ColType,
}

/// Aggregate computed over the rows of one correlated group.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AggSpec {
    /// No aggregate (EXISTS / NOT EXISTS / bare scalar).
    None,
    /// `COUNT(*)`
    CountStar,
    /// `COUNT(inner_column)` — counts non-NULL values.
    CountColumn(usize),
    /// `SUM(inner_column)` — NULL and empty-group aware.
    Sum(usize),
}

/// One fully validated subquery.
#[derive(Debug, Clone)]
pub struct SubqueryPlan {
    pub alias: String,
    pub kind: SubqueryKind,
    /// Relation name the inner references were bound against (for diagnostics).
    pub input_name: String,
    pub keys: Vec<KeyPair>,
    pub agg: AggSpec,
    /// Inner value column for a bare scalar subquery.
    pub value_inner_index: Option<usize>,
    /// Type/name of the column appended to outer rows.
    pub result_field: Field,
}

impl SubqueryPlan {
    /// Whether an empty correlated group yields SQL NULL (vs a default such as
    /// 0 or false). Preserving this distinction is a core requirement:
    /// `COUNT` over an empty group is 0, `SUM` is NULL.
    pub fn empty_group_is_null(&self) -> bool {
        match self.kind {
            SubqueryKind::ScalarAgg => matches!(self.agg, AggSpec::Sum(_)),
            SubqueryKind::Scalar => true,
            _ => false,
        }
    }

    pub fn agg_func_label(&self) -> Option<(AggFunc, bool)> {
        match self.agg {
            AggSpec::None | AggSpec::Sum(_) => None,
            AggSpec::CountStar => Some((AggFunc::Count, true)),
            AggSpec::CountColumn(_) => Some((AggFunc::Count, false)),
        }
    }
}

/// The complete validated query: materialized typed batches plus plans.
#[derive(Debug)]
pub struct ValidatedQuery {
    pub request_id: String,
    pub outer_name: String,
    pub outer: Batch,
    pub subqueries: Vec<SubqueryPlan>,
    /// Inner relation name -> typed batch (catalog or inline overlay).
    pub inputs: HashMap<String, Batch>,
}
