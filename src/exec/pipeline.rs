//! Decorrelated execution: the set-oriented pipeline emitted by the rewrite.
//!
//! Physical shape per subquery:
//!
//! ```text
//! outer (probe driver)          inner
//!     │                            │
//!     │                            ▼
//!     │                     HashBuild on correlation keys
//!     │                            │
//!     ▼                            ▼
//!   LeftOuterHashProbe ── NULL group when unmatched
//!     │
//!     ▼
//!   GroupAggregate / Mark / cardinality check
//!     │
//!     ▼
//!   append typed column
//! ```
//!
//! Unlike the naïve interpreter the inner relation is grouped **once**; each
//! outer row does an O(1) hash probe. The reduction rule itself is the shared
//! [`reduce_group`], so any discrepancy between this pipeline and
//! [`crate::exec::naive`] can only come from group construction/probing —
//! which is exactly what the equivalence verifier guards.

use crate::exec::operators::{reduce_group, InnerGroups};
use crate::exec::{AppendedColumn, StrategyOutput, TraceEvent};
use crate::query::plan::ValidatedQuery;

pub const STRATEGY_NAME: &str = "decorrelated_hash_pipeline";

/// Execute the validated query through the decorrelated operator pipeline.
pub fn execute(q: &ValidatedQuery) -> StrategyOutput {
    let mut trace = vec![TraceEvent {
        step: "scan_outer".into(),
        detail: format!("{} outer rows; {} subqueries", q.outer.len(), q.subqueries.len()),
        rows: q.outer.len(),
    }];

    let mut appended: Vec<AppendedColumn> = Vec::with_capacity(q.subqueries.len());
    for sq in &q.subqueries {
        let inner = q.inputs.get(&sq.input_name).expect("validated input");

        // ── HashBuild -------------------------------------------------------
        let groups = InnerGroups::build(inner, &sq.keys);
        trace.push(TraceEvent {
            step: "hash_build".into(),
            detail: format!(
                "subquery `{}`: grouped {} inner rows into {} correlation group(s); {} row(s) excluded (NULL key)",
                sq.alias,
                inner.len(),
                groups.len(),
                groups.null_key_rows
            ),
            rows: groups.len(),
        });

        // ── LeftOuterHashProbe + reduce per outer row ----------------------
        let mut values: Vec<crate::batch::Datum> = Vec::with_capacity(q.outer.len());
        for outer_row in 0..q.outer.len() {
            let matched = groups.probe(&q.outer, &sq.keys, outer_row);
            match reduce_group(sq, inner, &matched, outer_row) {
                Ok(result) => values.push(into_datum(result)),
                Err(e) => {
                    let mut exec_err = crate::exec::ExecError::from_query_error(&e);
                    exec_err.at_outer_row = Some(outer_row);
                    exec_err.at_step = "exec:decorrelated_hash_pipeline".to_string();
                    trace.push(TraceEvent {
                        step: "error".into(),
                        detail: format!("[{}] {} at outer row #{}", e.code, e.message, outer_row),
                        rows: outer_row,
                    });
                    trace.push(TraceEvent {
                        step: "abort".into(),
                        detail: format!("decorrelated pipeline aborted: [{}] {}", e.code, e.message),
                        rows: outer_row,
                    });
                    return StrategyOutput {
                        strategy: STRATEGY_NAME,
                        appended,
                        trace,
                        error: Some(exec_err),
                    };
                }
            }
        }

        trace.push(TraceEvent {
            step: "left_outer_probe".into(),
            detail: format!("subquery `{}`: probed {} outer row(s); outer multiplicity preserved", sq.alias, q.outer.len()),
            rows: q.outer.len(),
        });
        appended.push(AppendedColumn { field: sq.result_field.clone(), values });
    }

    trace.push(TraceEvent {
        step: "done".into(),
        detail: "decorrelated pipeline finished; outer rows unchanged apart from appended columns".into(),
        rows: q.outer.len(),
    });
    StrategyOutput { strategy: STRATEGY_NAME, appended, trace, error: None }
}

fn into_datum(r: crate::exec::operators::GroupResult) -> crate::batch::Datum {
    use crate::exec::operators::GroupResult::*;
    match r {
        Mark(b) => crate::batch::Datum::from_value(crate::batch::Value::Bool(b)),
        Scalar(d) => d,
    }
}
