//! The logical range-join plan: two inequality predicates over two tables.

use serde::{Deserialize, Serialize};

use crate::batch::TypedBatch;
use crate::error::{JoinError, Result};

/// One of the four inequality directions.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CmpOp {
    /// `left < right`
    Lt,
    /// `left <= right`
    Le,
    /// `left > right`
    Gt,
    /// `left >= right`
    Ge,
}

impl CmpOp {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            CmpOp::Lt => "<",
            CmpOp::Le => "<=",
            CmpOp::Gt => ">",
            CmpOp::Ge => ">=",
        }
    }

    /// True for the non-strict variants (the equal group is included).
    #[must_use]
    pub fn is_inclusive(self) -> bool {
        matches!(self, CmpOp::Le | CmpOp::Ge)
    }

    /// Swap operands: `a OP b` becomes `b OP' a` with the mirrored operator.
    #[must_use]
    pub fn mirror(self) -> CmpOp {
        match self {
            CmpOp::Lt => CmpOp::Gt,
            CmpOp::Le => CmpOp::Ge,
            CmpOp::Gt => CmpOp::Lt,
            CmpOp::Ge => CmpOp::Le,
        }
    }

    /// Strictly increasing comparison (`<` or `<=`) when read left-to-right.
    #[must_use]
    pub fn is_ascending(self) -> bool {
        matches!(self, CmpOp::Lt | CmpOp::Le)
    }
}

/// A single range predicate: `left.<left_column> OP right.<right_column>`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct JoinPredicate {
    pub left_column: String,
    pub op: CmpOp,
    pub right_column: String,
}

/// A complete two-table, two-range-predicate join plan.
///
/// A pair `(l, r)` is emitted iff predicate 0 **and** predicate 1 hold.
#[derive(Debug, Clone)]
pub struct JoinPlan {
    pub predicates: [JoinPredicate; 2],
}

impl JoinPlan {
    /// Construct and validate a plan against the concrete input batches.
    ///
    /// Both predicates must reference existing columns; the two predicates may
    /// share a column on either side. A predicate comparing different physical
    /// types (e.g. int64 vs utf8) is rejected.
    pub fn validate(
        predicates: [JoinPredicate; 2],
        left: &TypedBatch,
        right: &TypedBatch,
    ) -> Result<Self> {
        for (idx, p) in predicates.iter().enumerate() {
            let li = left.column_index(&p.left_column).map_err(|e| {
                JoinError::input("unknown_left_column", e.message)
                    .with("predicate", idx)
                    .with("column", p.left_column.clone())
            })?;
            let ri = right.column_index(&p.right_column).map_err(|e| {
                JoinError::input("unknown_right_column", e.message)
                    .with("predicate", idx)
                    .with("column", p.right_column.clone())
            })?;
            let lt = left.column(li).dtype;
            let rt = right.column(ri).dtype;
            if lt != rt {
                return Err(JoinError::input(
                    "type_mismatch",
                    format!(
                        "predicate {idx} compares {} ({}) with {} ({})",
                        p.left_column,
                        lt.as_str(),
                        p.right_column,
                        rt.as_str()
                    ),
                )
                .with("predicate", idx)
                .with("left_dtype", lt.as_str())
                .with("right_dtype", rt.as_str()));
            }
        }
        Ok(JoinPlan { predicates })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::batch::{Column, TypedBatch};

    fn batches() -> (TypedBatch, TypedBatch) {
        let l = TypedBatch::new(vec![(
            "a".to_owned(),
            Column::from_i64(vec![Some(1), Some(2)]),
        )])
        .unwrap();
        let r = TypedBatch::new(vec![
            ("b".to_owned(), Column::from_i64(vec![Some(2), Some(3)])),
            ("s".to_owned(), Column::from_utf8(vec![Some("x".into()), Some("y".into())])),
        ])
        .unwrap();
        (l, r)
    }

    fn pred(lc: &str, op: CmpOp, rc: &str) -> JoinPredicate {
        JoinPredicate { left_column: lc.to_owned(), op, right_column: rc.to_owned() }
    }

    #[test]
    fn accepts_valid_plan() {
        let (l, r) = batches();
        let plan =
            JoinPlan::validate([pred("a", CmpOp::Lt, "b"), pred("a", CmpOp::Lt, "b")], &l, &r);
        assert!(plan.is_ok());
    }

    #[test]
    fn rejects_unknown_columns_and_type_mismatch() {
        let (l, r) = batches();
        let err =
            JoinPlan::validate([pred("nope", CmpOp::Lt, "b"), pred("a", CmpOp::Lt, "b")], &l, &r)
                .unwrap_err();
        assert_eq!(err.code, "unknown_left_column");
        assert_eq!(err.category, crate::error::ErrorCategory::Input);

        let err =
            JoinPlan::validate([pred("a", CmpOp::Lt, "b"), pred("a", CmpOp::Lt, "s")], &l, &r)
                .unwrap_err();
        assert_eq!(err.code, "type_mismatch");
    }
}
