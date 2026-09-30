//! Independent row-by-row scalar oracle.
//!
//! This is the reference answer used to cross-check the bitmap engine. It is
//! deliberately written on completely different primitives:
//!
//! * inputs are plain `Vec<Option<Scalar>>` pulled from the arrow batch —
//!   NOT [`crate::bitset::Bitmap`] / [`crate::partition::TritPartition`];
//! * every row is evaluated independently with scalar Kleene logic
//!   ([`crate::trit::Trit`]);
//! * deleted rows are represented by an explicit hash-set of dead row ids and
//!   surface as `None` (outside the live universe) — never as `Unknown`.
//!
//! Because the bitmap engine's answers cannot influence this code path, an
//! agreement over thousands of randomized rows is a genuine differential test.

use std::collections::HashSet;

use crate::batch::{Scalar, TypedBatch};
use crate::error::Result;
use crate::index::CompareOp;
use crate::query::Expr;
use crate::trit::Trit;

/// Scalar comparison with SQL NULL propagation.
///
/// Returns `None` when either operand is NULL (the comparison is UNKNOWN);
/// otherwise the boolean result. Ordering follows the logical type: integers
/// numerically, text lexicographically, booleans by equality only.
pub fn scalar_compare(op: CompareOp, value: Option<&Scalar>, literal: &Scalar) -> Option<bool> {
    let v = value?;
    let ord = match (v, literal) {
        (Scalar::Int(a), Scalar::Int(b)) => a.cmp(b),
        (Scalar::Text(a), Scalar::Text(b)) => a.cmp(b),
        (Scalar::Bool(a), Scalar::Bool(b)) => a.cmp(b),
        _ => return None, // type mismatch: treated as UNKNOWN here; typed layer errors first
    };
    use std::cmp::Ordering::*;
    let result = match op {
        CompareOp::Eq => ord == Equal,
        CompareOp::Neq => ord != Equal,
        CompareOp::Lt => ord == Less,
        CompareOp::Lte => ord != Greater,
        CompareOp::Gt => ord == Greater,
        CompareOp::Gte => ord != Less,
    };
    Some(result)
}

/// One row's truth value for an expression. `dead` rows return `None`.
fn eval_row(
    expr: &Expr,
    columns: &[(String, Vec<Option<Scalar>>)],
    dead: &HashSet<usize>,
    row: usize,
) -> Result<Option<Trit>> {
    if dead.contains(&row) {
        return Ok(None);
    }
    Ok(Some(match expr {
        Expr::Compare {
            column,
            op,
            literal,
        } => {
            let col = columns
                .iter()
                .find(|(name, _)| name == column)
                .map(|(_, v)| &v[row]);
            match col {
                Some(v) => match scalar_compare(*op, v.as_ref(), literal) {
                    Some(true) => Trit::True,
                    Some(false) => Trit::False,
                    None => Trit::Unknown,
                },
                None => return Err(crate::error::TritmapError::UnknownColumn(column.clone())),
            }
        }
        Expr::IsNull { column, negated } => {
            let col = columns
                .iter()
                .find(|(name, _)| name == column)
                .map(|(_, v)| &v[row]);
            match col {
                Some(v) => {
                    let is_null = v.is_none();
                    let matched = is_null ^ *negated;
                    if matched {
                        Trit::True
                    } else {
                        Trit::False
                    }
                }
                None => return Err(crate::error::TritmapError::UnknownColumn(column.clone())),
            }
        }
        Expr::And(a, b) => {
            let x = eval_row(a, columns, dead, row)?.expect("live row checked");
            let y = eval_row(b, columns, dead, row)?.expect("live row checked");
            x.and(y)
        }
        Expr::Or(a, b) => {
            let x = eval_row(a, columns, dead, row)?.expect("live row checked");
            let y = eval_row(b, columns, dead, row)?.expect("live row checked");
            x.or(y)
        }
        Expr::Not(a) => !eval_row(a, columns, dead, row)?.expect("live row checked"),
    }))
}

/// Full oracle result: one `Option<Trit>` per row id; `None` means deleted.
pub fn oracle_eval(
    batch: &TypedBatch,
    expr: &Expr,
    dead_rows: &HashSet<usize>,
) -> Result<Vec<Option<Trit>>> {
    let mut columns = Vec::new();
    for spec in batch.columns() {
        columns.push((spec.name.clone(), batch.column_values(&spec.name)?));
    }
    let n = batch.len();
    let mut out = Vec::with_capacity(n);
    for row in 0..n {
        out.push(eval_row(expr, &columns, dead_rows, row)?);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::batch::BatchBuilder;
    use crate::query::parse_predicate;

    #[test]
    fn scalar_null_rules() {
        // NULL compared to anything -> None (UNKNOWN)
        assert_eq!(
            scalar_compare(CompareOp::Eq, None, &Scalar::Int(1)),
            None
        );
        // The decisive Kleene rows at scalar level:
        assert_eq!(
            scalar_compare(CompareOp::Gt, Some(&Scalar::Int(5)), &Scalar::Int(1)),
            Some(true)
        );
        assert_eq!(
            scalar_compare(
                CompareOp::Lte,
                Some(&Scalar::Text("a".to_string())),
                &Scalar::Text("a".to_string()),
            ),
            Some(true)
        );
    }

    #[test]
    fn deleted_rows_are_none_not_unknown() {
        let b = BatchBuilder::new()
            .add_integer("a", vec![None, Some(2), Some(3)])
            .build()
            .unwrap();
        let expr = parse_predicate("a = 2").unwrap();
        let mut dead = HashSet::new();
        dead.insert(0); // a NULL row is deleted -> must be None, not Unknown
        let got = oracle_eval(&b, &expr, &dead).unwrap();
        assert_eq!(got, vec![None, Some(Trit::True), Some(Trit::False)]);
    }
}
