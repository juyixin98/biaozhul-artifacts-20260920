//! Shared, hand-built fixtures and oracle helpers for the integration suite.
//!
//! Everything in this file is **independent of the IEJoin engine**: expected
//! answer multisets are literal constants or come from a tiny brute-force
//! checker written directly on `Option<i64>` values, which shares no code
//! with either join implementation.

#![allow(dead_code)]

use iejoin_backend::batch::{Column, TypedBatch};
use iejoin_backend::error::Result;
use iejoin_backend::operator::plan::{CmpOp, JoinPredicate};

pub fn i64col(name: &str, v: Vec<Option<i64>>) -> (String, Column) {
    (name.to_owned(), Column::from_i64(v))
}

/// Hand-built small dataset with duplicates, NULLs, ties and gaps.
///
/// ```text
/// row    a1   a2          b1   b2
///  0     2    3            2    1
///  1     2    1            4    2
///  2   NULL  9            2    3
///  3     0  NULL        NULL   8
///  4     5    0            9  NULL
///  5     2    5            1    1
///  6                        7    0
/// ```
pub fn mixed() -> Result<(TypedBatch, TypedBatch)> {
    let left = TypedBatch::new(vec![
        i64col("a1", vec![Some(2), Some(2), None, Some(0), Some(5), Some(2)]),
        i64col("a2", vec![Some(3), Some(1), Some(9), None, Some(0), Some(5)]),
    ])?;
    let right = TypedBatch::new(vec![
        i64col("b1", vec![Some(2), Some(4), Some(2), None, Some(9), Some(1), Some(7)]),
        i64col("b2", vec![Some(1), Some(2), Some(3), Some(8), None, Some(1), Some(0)]),
    ])?;
    Ok((left, right))
}

pub fn pred(op1: CmpOp, op2: CmpOp) -> [JoinPredicate; 2] {
    [
        JoinPredicate { left_column: "a1".into(), op: op1, right_column: "b1".into() },
        JoinPredicate { left_column: "a2".into(), op: op2, right_column: "b2".into() },
    ]
}

pub fn validate(
    op1: CmpOp,
    op2: CmpOp,
    left: &TypedBatch,
    right: &TypedBatch,
) -> Result<iejoin_backend::JoinPlan> {
    iejoin_backend::JoinPlan::validate(pred(op1, op2), left, right)
}

/// Collect IEJoin output pairs (sorted for multiset comparison).
pub fn ie_pairs(
    plan: &iejoin_backend::JoinPlan,
    left: &TypedBatch,
    right: &TypedBatch,
) -> Result<(Vec<(u32, u32)>, iejoin_backend::JoinMetrics)> {
    let mut pairs = Vec::new();
    let metrics = iejoin_backend::iejoin(plan, left, right, &mut |l, r| {
        pairs.push((l, r));
        iejoin_backend::SinkReply::Continue
    })?;
    pairs.sort_unstable();
    Ok((pairs, metrics))
}

/// Collect nested-loop oracle pairs.
pub fn nl_pairs(
    plan: &iejoin_backend::JoinPlan,
    left: &TypedBatch,
    right: &TypedBatch,
) -> Result<(Vec<(u32, u32)>, iejoin_backend::JoinMetrics)> {
    let mut pairs = Vec::new();
    let metrics = iejoin_backend::nested_loop(plan, left, right, &mut |l, r| {
        pairs.push((l, r));
        iejoin_backend::SinkReply::Continue
    })?;
    pairs.sort_unstable();
    Ok((pairs, metrics))
}

fn holds(op: CmpOp, a: i64, b: i64) -> bool {
    match op {
        CmpOp::Lt => a < b,
        CmpOp::Le => a <= b,
        CmpOp::Gt => a > b,
        CmpOp::Ge => a >= b,
    }
}

fn i64vals(batch: &TypedBatch, col: &str) -> Vec<Option<i64>> {
    let idx = batch.column_index(col).unwrap();
    let column = batch.column(idx);
    (0..batch.row_count()).map(|r| column.i64_at(r)).collect()
}

/// Brute-force answer written directly on `Option<i64>` values.
/// NULL never matches. Shares no code with either engine.
pub fn brute_force(
    op1: CmpOp,
    op2: CmpOp,
    left: &TypedBatch,
    right: &TypedBatch,
) -> Vec<(u32, u32)> {
    let a1 = i64vals(left, "a1");
    let a2 = i64vals(left, "a2");
    let b1 = i64vals(right, "b1");
    let b2 = i64vals(right, "b2");
    let mut out = Vec::new();
    for (li, (x1, x2)) in a1.iter().zip(&a2).enumerate() {
        for (ri, (y1, y2)) in b1.iter().zip(&b2).enumerate() {
            if let (Some(x1), Some(x2), Some(y1), Some(y2)) = (x1, x2, y1, y2) {
                if holds(op1, *x1, *y1) && holds(op2, *x2, *y2) {
                    out.push((li as u32, ri as u32));
                }
            }
        }
    }
    out
}
