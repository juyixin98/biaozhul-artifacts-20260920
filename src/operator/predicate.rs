//! Predicates, comparison operators and shared join descriptions.
//!
//! A [`JoinSpec`] joins two relations `L` ("left") and `R` ("right") with
//! exactly two range predicates. Every predicate names one column on each
//! side plus one of four inequality directions:
//!
//! | code | meaning (left value `op` right value) |
//! |------|---------------------------------------|
//! | `lt` | `L.a <  R.b`  (strict)                |
//! | `le` | `L.a <= R.b`  (non-strict)            |
//! | `gt` | `L.a >  R.b`  (strict, mirrored `lt`) |
//! | `ge` | `L.a >= R.b`  (non-strict, mirrored `le`) |
//!
//! NULL never satisfies any inequality, in any direction: if either side of
//! either predicate is NULL the row pair is excluded.

use std::fmt;

use serde::{Deserialize, Serialize};

use crate::error::Error;
use crate::types::batch::Batch;

/// One of the four inequality directions.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Comparator {
    /// `<`
    Lt,
    /// `<=`
    Le,
    /// `>`
    Gt,
    /// `>=`
    Ge,
}

impl Comparator {
    pub fn parse(code: &str) -> Option<Self> {
        match code {
            "lt" | "<" => Some(Comparator::Lt),
            "le" | "<=" => Some(Comparator::Le),
            "gt" | ">" => Some(Comparator::Gt),
            "ge" | ">=" => Some(Comparator::Ge),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Comparator::Lt => "lt",
            Comparator::Le => "le",
            Comparator::Gt => "gt",
            Comparator::Ge => "ge",
        }
    }

    /// Evaluate `left op right`. NULL inputs never compare true.
    #[inline]
    pub fn eval(self, left: Option<i64>, right: Option<i64>) -> bool {
        match (left, right) {
            (Some(l), Some(r)) => match self {
                Comparator::Lt => l < r,
                Comparator::Le => l <= r,
                Comparator::Gt => l > r,
                Comparator::Ge => l >= r,
            },
            _ => false,
        }
    }

    /// `true` for non-strict inequalities (`<=`, `>=`). Drives the distinct
    /// equal-value boundary behaviour of strict vs non-strict predicates.
    pub fn is_inclusive(self) -> bool {
        matches!(self, Comparator::Le | Comparator::Ge)
    }

    /// `true` when values on the *left* are ordered above the right values,
    /// i.e. the predicate scans high-to-low (`>`, `>=`).
    pub fn is_descending(self) -> bool {
        matches!(self, Comparator::Gt | Comparator::Ge)
    }
}

impl fmt::Display for Comparator {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// One range predicate: `left_column op right_column`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Predicate {
    pub left_column: String,
    #[serde(rename = "op")]
    pub comparator: Comparator,
    pub right_column: String,
}

impl Predicate {
    pub fn new(
        left_column: impl Into<String>,
        comparator: Comparator,
        right_column: impl Into<String>,
    ) -> Self {
        Self {
            left_column: left_column.into(),
            comparator,
            right_column: right_column.into(),
        }
    }
}

/// Complete description of a two-predicate join.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct JoinSpec {
    /// Predicate used to drive the merge/insertion scan ("p1").
    pub p1: Predicate,
    /// Predicate tested through the sorted bitmap window ("p2").
    pub p2: Predicate,
}

impl JoinSpec {
    pub fn new(p1: Predicate, p2: Predicate) -> Self {
        Self { p1, p2 }
    }

    /// Structural validation: columns must exist in the supplied batches and
    /// the two predicates must not be textually identical.
    pub fn validate_against(&self, left: &Batch, right: &Batch) -> Result<(), Error> {
        self.validate_one(&self.p1, left, right)?;
        self.validate_one(&self.p2, left, right)?;
        if self.p1.left_column == self.p2.left_column
            && self.p1.right_column == self.p2.right_column
            && self.p1.comparator == self.p2.comparator
        {
            return Err(Error::input(
                "duplicate_predicate",
                "p1 and p2 must be distinct predicates",
            ));
        }
        Ok(())
    }

    fn validate_one(
        predicate: &Predicate,
        left: &Batch,
        right: &Batch,
    ) -> Result<(), Error> {
        left.column(&predicate.left_column).map(|_| ())?;
        right.column(&predicate.right_column).map(|_| ())?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nulls_never_compare() {
        for op in [Comparator::Lt, Comparator::Le, Comparator::Gt, Comparator::Ge] {
            assert!(!op.eval(None, Some(1)));
            assert!(!op.eval(Some(1), None));
            assert!(!op.eval(None, None));
        }
    }

    #[test]
    fn strict_vs_inclusive_boundary() {
        assert!(!Comparator::Lt.eval(Some(1), Some(1)));
        assert!(Comparator::Le.eval(Some(1), Some(1)));
        assert!(!Comparator::Gt.eval(Some(1), Some(1)));
        assert!(Comparator::Ge.eval(Some(1), Some(1)));
        assert!(!Comparator::Lt.is_inclusive());
        assert!(Comparator::Le.is_inclusive());
        assert!(Comparator::Gt.is_descending());
        assert!(!Comparator::Lt.is_descending());
    }
}
