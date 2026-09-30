//! Row-by-row interpreter — the semantic reference implementation.
//!
//! For every outer row (outer duplicate rows kept, never deduplicated) each
//! subquery is re-evaluated by a full inner-table scan with the outer row
//! bound. Slow by construction; its only job is to be obviously correct so
//! the decorrelating executor can be differential-tested against it.

use std::collections::HashMap;
use std::sync::Arc;

use crate::batch::{Batch, Value};
use crate::error::{EngineError, EngineResult, ErrorKind};
use crate::operators::aggregate;
use crate::operators::predicate::{all_pass, RowEnv};
use crate::operators::runner;
use crate::semantic::{ValidatedQuery, ValidatedSubKind, ValidatedSubquery};
use crate::state::{Catalog, Table};

pub fn execute(q: &ValidatedQuery, catalog: &Catalog) -> EngineResult<Batch> {
    let mut inner_cache: HashMap<String, Arc<Table>> = HashMap::new();
    for s in &q.subqueries {
        inner_cache.insert(s.inner_table.clone(), catalog.get(&s.inner_table)?);
    }

    runner::run(q, catalog, |index, outer_batch, outer_row| {
        let sq = &q.subqueries[index];
        eval_subquery(sq, outer_batch, outer_row, &inner_cache)
    })
}

fn eval_subquery(
    sq: &ValidatedSubquery,
    outer_batch: &Batch,
    outer_row: usize,
    inner_cache: &HashMap<String, Arc<Table>>,
) -> EngineResult<Value> {
    let inner = inner_cache
        .get(&sq.inner_table)
        .ok_or_else(|| EngineError::internal("inner table missing from cache"))?;
    let inner_batch = &inner.batch;

    let mut matching: Vec<usize> = Vec::new();
    for inner_row in 0..inner_batch.n_rows {
        let env = RowEnv {
            outer: outer_batch,
            outer_row,
            inner: inner_batch,
            inner_row,
        };
        if all_pass(&sq.predicates, &|r| env.lookup(r))? {
            matching.push(inner_row);
        }
    }

    finish_subquery(sq, inner_batch, &matching)
}

/// The post-scan tail shared with the decorrelating executor: given the set of
/// inner row indices that satisfy the subquery predicates, produce the value
/// or raise the cardinality error. Same code path for both strategies.
pub fn finish_subquery(
    sq: &ValidatedSubquery,
    inner_batch: &Batch,
    matching: &[usize],
) -> EngineResult<Value> {
    match &sq.kind {
        ValidatedSubKind::Exists => Ok(Value::Bool(!matching.is_empty())),
        ValidatedSubKind::NotExists => Ok(Value::Bool(matching.is_empty())),
        ValidatedSubKind::ScalarAgg { proj, .. } => {
            let values = match proj {
                crate::ast::ScalarProj::CountStar => vec![None; matching.len()],
                crate::ast::ScalarProj::CountCol { col }
                | crate::ast::ScalarProj::SumCol { col, .. } => {
                    aggregate::column_values(col, inner_batch, matching)?
                }
            };
            aggregate::compute(proj, &values)
        }
        ValidatedSubKind::ScalarColumn { col, .. } => match matching {
            [] => Ok(Value::Null),
            [only] => Ok(inner_batch.value(col, *only)?),
            _ => Err(cardinality_error(col, matching.len())),
        },
    }
}

pub(crate) fn cardinality_error(col: &str, n: usize) -> EngineError {
    EngineError::new(
        ErrorKind::CardinalityViolation,
        format!(
            "scalar subquery on column '{col}' returned {n} rows; at most 1 is allowed"
        ),
    )
}
