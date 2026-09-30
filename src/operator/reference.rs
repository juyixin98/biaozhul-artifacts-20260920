//! Independent nested-loop reference engine.
//!
//! This is the small-scale **oracle**: it deliberately uses the most
//! straightforward possible algorithm (full Cartesian scan, direct key
//! comparison) and shares no algorithmic code with [`super::iejoin`]. Test
//! expectations for result multisets come from here (and, in
//! `engine_eq.rs`, additionally from hand-written literals).

use crate::batch::{try_cmp_cells, TypedBatch};
use crate::error::{JoinError, Result};
use crate::operator::metrics::JoinMetrics;
use crate::operator::plan::{CmpOp, JoinPlan};

/// Holds `(left, right)` while the predicate is true.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SinkReply {
    Continue,
    Stop,
}

/// Pair sink. Returning [`SinkReply::Stop`] ends execution early; counters
/// then describe only the work done up to that point.
pub type PairSink<'a> = &'a mut dyn FnMut(u32, u32) -> SinkReply;

fn holds(op: CmpOp, ord: std::cmp::Ordering) -> bool {
    match op {
        CmpOp::Lt => ord.is_lt(),
        CmpOp::Le => ord.is_le(),
        CmpOp::Gt => ord.is_gt(),
        CmpOp::Ge => ord.is_ge(),
    }
}

/// Evaluate one predicate directly. NULL / NaN on either side never matches.
fn predicate_holds(
    plan: &JoinPlan,
    which: usize,
    left: &TypedBatch,
    right: &TypedBatch,
    lrow: usize,
    rrow: usize,
    col_cache: &[(usize, usize)],
) -> Result<bool> {
    let (li, ri) = col_cache[which];
    let (Some(lk), Some(rk)) = (
        left.column(li).key_at(lrow),
        right.column(ri).key_at(rrow),
    ) else {
        return Ok(false);
    };
    let ord = try_cmp_cells(lk, rk).ok_or_else(|| {
        JoinError::compute(
            "incomparable_cells",
            "nested-loop reference compared keys from different physical types",
        )
    })?;
    Ok(holds(plan.predicates[which].op, ord))
}

/// Run the reference nested loop. Pairs are emitted in
/// `(left_row asc, right_row asc)` order.
pub fn nested_loop(
    plan: &JoinPlan,
    left: &TypedBatch,
    right: &TypedBatch,
    sink: PairSink<'_>,
) -> Result<JoinMetrics> {
    let col_cache: Vec<(usize, usize)> = plan
        .predicates
        .iter()
        .map(|p| {
            // Already validated by JoinPlan::validate.
            (left.column_index(&p.left_column).unwrap(),
             right.column_index(&p.right_column).unwrap())
        })
        .collect();

    let mut metrics = JoinMetrics::default();
    let nl = left.row_count();
    let nr = right.row_count();
    let mut stopped = false;

    for lrow in 0..nl {
        metrics.left_rows_scanned += 1;
        for rrow in 0..nr {
            metrics.nl_pair_evaluations += 1;
            let p0 = predicate_holds(plan, 0, left, right, lrow, rrow, &col_cache)?;
            let ok = p0
                && predicate_holds(plan, 1, left, right, lrow, rrow, &col_cache)?;
            if ok {
                metrics.pairs_emitted += 1;
                if sink(lrow as u32, rrow as u32) == SinkReply::Stop {
                    stopped = true;
                    break;
                }
            }
        }
        if stopped {
            break;
        }
    }
    Ok(metrics)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::batch::Column;
    use crate::operator::plan::JoinPredicate;

    fn pred(lc: &str, op: CmpOp, rc: &str) -> JoinPredicate {
        JoinPredicate { left_column: lc.to_owned(), op, right_column: rc.to_owned() }
    }

    #[test]
    fn null_never_matches_and_counter_is_cartesian() {
        let l = TypedBatch::new(vec![(
            "x".to_owned(),
            Column::from_i64(vec![Some(1), None]),
        )])
        .unwrap();
        let r = TypedBatch::new(vec![(
            "y".to_owned(),
            Column::from_i64(vec![Some(2), None]),
        )])
        .unwrap();
        let plan = JoinPlan::validate([pred("x", CmpOp::Lt, "y"), pred("x", CmpOp::Lt, "y")], &l, &r)
            .unwrap();
        let mut pairs = Vec::new();
        let m = nested_loop(&plan, &l, &r, &mut |a, b| {
            pairs.push((a, b));
            SinkReply::Continue
        })
        .unwrap();
        assert_eq!(pairs, vec![(0, 0)]);
        assert_eq!(m.nl_pair_evaluations, 4);
        assert_eq!(m.pairs_emitted, 1);
    }
}
