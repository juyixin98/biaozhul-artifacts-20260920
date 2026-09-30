//! Shared outer-row driver. Both execution strategies iterate outer rows,
//! apply outer filters, evaluate each output expression, and emit rows in
//! exactly the same way — the only difference is how a subquery value is
//! produced (full scan vs. pre-aggregated hash lookup).

use crate::batch::{Batch, Value};
use crate::error::EngineResult;
use crate::operators::emit::Emitter;
use crate::operators::predicate::{all_pass, OuterEnv};
use crate::semantic::{OutputSource, ValidatedQuery};
use crate::state::Catalog;

/// Run the outer query. `eval_sub(index, outer_batch, outer_row)` returns the
/// subquery value. Errors raised here (e.g. cardinality violations) propagate
/// identically from either execution strategy.
pub fn run<F>(
    q: &ValidatedQuery,
    catalog: &Catalog,
    mut eval_sub: F,
) -> EngineResult<Batch>
where
    F: FnMut(usize, &Batch, usize) -> EngineResult<Value>,
{
    let outer = catalog.get(&q.outer_table)?;
    let n_out = q.outputs.len();
    let mut emitter = Emitter::new(&q.outputs);

    for outer_row in 0..outer.batch.n_rows {
        if !all_pass(&q.filters, &|r| {
            OuterEnv {
                outer: &outer.batch,
                outer_row,
            }
            .lookup(r)
        })? {
            continue;
        }
        let mut out_row: Vec<Value> = Vec::with_capacity(n_out);
        for out in &q.outputs {
            let value = match &out.source {
                OutputSource::OuterColumn { col } => outer.batch.value(col, outer_row)?,
                OutputSource::Subquery { index } => eval_sub(*index, &outer.batch, outer_row)?,
            };
            out_row.push(value);
        }
        emitter.push_row(&out_row)?;
    }
    emitter.into_batch()
}
