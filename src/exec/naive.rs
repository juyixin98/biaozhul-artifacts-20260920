//! Naïve **reference** execution: per-outer-row nested-loop interpretation.
//!
//! This is the semantic oracle. It deliberately implements the correlated
//! query literally rather than cleverly: for each outer row it rescans the
//! whole inner relation, evaluates each correlation conjunct with SQL
//! three-valued equality, and reduces the matched row set. It does NOT share
//! any hash/join structure with the decorrelated pipeline — only the reduction
//! rule ([`crate::exec::operators::reduce_group`]), which is the definition
//! both strategies must obey.

use crate::exec::operators::reduce_group;
use crate::exec::{AppendedColumn, StrategyOutput, TraceEvent};
use crate::query::plan::ValidatedQuery;
use crate::query::sql_equal;
use crate::query::QueryError;

pub const STRATEGY_NAME: &str = "naive_rowwise";

/// Execute the validated query row by row.
pub fn execute(q: &ValidatedQuery) -> StrategyOutput {
    let mut trace = vec![TraceEvent {
        step: "scan_outer".into(),
        detail: format!("{} outer rows to evaluate against {} subqueries", q.outer.len(), q.subqueries.len()),
        rows: q.outer.len(),
    }];

    let mut appended: Vec<AppendedColumn> = Vec::with_capacity(q.subqueries.len());
    for sq in &q.subqueries {
        let inner = q.inputs.get(&sq.input_name).expect("validated input");
        let mut values: Vec<crate::batch::Datum> = Vec::with_capacity(q.outer.len());

        for outer_row in 0..q.outer.len() {
            let matched = scan_matched(&q.outer, inner, sq, outer_row);
            match reduce_group(sq, inner, &matched, outer_row) {
                Ok(result) => values.push(into_datum(result)),
                Err(e) => {
                    trace.push(TraceEvent {
                        step: "error".into(),
                        detail: format!("[{}] {} at outer row #{}", e.code, e.message, outer_row),
                        rows: outer_row,
                    });
                    return finish_with_error(appended, trace, e, outer_row);
                }
            }
        }

        trace.push(TraceEvent {
            step: "scan_nested".into(),
            detail: format!(
                "subquery `{}`: rescanned inner ({}) rows for each of {} outer rows ({} comparisons)",
                sq.alias,
                inner.len(),
                q.outer.len(),
                q.outer.len() * inner.len()
            ),
            rows: q.outer.len() * inner.len(),
        });
        appended.push(AppendedColumn { field: sq.result_field.clone(), values });
    }

    trace.push(TraceEvent {
        step: "done".into(),
        detail: "all outer rows evaluated row-by-row".into(),
        rows: q.outer.len(),
    });
    StrategyOutput { strategy: STRATEGY_NAME, appended, trace, error: None }
}

fn finish_with_error(
    appended: Vec<AppendedColumn>,
    mut trace: Vec<TraceEvent>,
    e: QueryError,
    outer_row: usize,
) -> StrategyOutput {
    let mut exec_err = crate::exec::ExecError::from_query_error(&e);
    exec_err.at_outer_row = Some(outer_row);
    exec_err.at_step = "exec:naive_rowwise".to_string();
    trace.push(TraceEvent {
        step: "abort".into(),
        detail: format!("naive execution aborted: [{}] {}", e.code, e.message),
        rows: outer_row,
    });
    StrategyOutput { strategy: STRATEGY_NAME, appended, trace, error: Some(exec_err) }
}

/// Literal nested-loop match test: a row matches iff EVERY conjunct is true
/// under SQL equality (NULL never satisfies equality).
fn scan_matched(
    outer: &crate::batch::Batch,
    inner: &crate::batch::Batch,
    sq: &crate::query::plan::SubqueryPlan,
    outer_row: usize,
) -> Vec<usize> {
    let mut matched = Vec::new();
    for inner_row in 0..inner.len() {
        let mut all_true = true;
        for k in &sq.keys {
            let ov = outer.cell(k.outer_index, outer_row);
            let iv = inner.cell(k.inner_index, inner_row);
            if !sql_equal(&ov, &iv) {
                all_true = false;
                break;
            }
        }
        if all_true {
            matched.push(inner_row);
        }
    }
    matched
}

fn into_datum(r: crate::exec::operators::GroupResult) -> crate::batch::Datum {
    use crate::exec::operators::GroupResult::*;
    match r {
        Mark(b) => crate::batch::Datum::from_value(crate::batch::Value::Bool(b)),
        Scalar(d) => d,
    }
}
