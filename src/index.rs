//! Column-value bitmap indexes.
//!
//! For each indexed column we keep:
//! * a NULL bitmap (arrow validity = 0),
//! * a `value -> bitmap` dictionary of non-NULL positions.
//!
//! A leaf predicate is evaluated **against an explicit live-domain bitmap**
//! (rows surviving versioned deletion) and produces a [`TritPartition`] on
//! that domain:
//!
//! ```text
//! value = literal matches  -> TRUE
//! value non-NULL, no match -> FALSE
//! value IS NULL            -> UNKNOWN
//! row outside domain       -> in none of the three (deleted)
//! ```
//!
//! `IS NULL` / `IS NOT NULL` never produce UNKNOWN themselves.

use std::collections::BTreeMap;

use crate::batch::{LogicalType, Scalar, TypedBatch};
use crate::bitset::Bitmap;
use crate::error::{Result, TritmapError};
use crate::partition::TritPartition;
use crate::trit::Trit;

/// Supported comparison operators.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CompareOp {
    Eq,
    Neq,
    Lt,
    Lte,
    Gt,
    Gte,
}

impl CompareOp {
    pub fn as_sql(self) -> &'static str {
        match self {
            CompareOp::Eq => "=",
            CompareOp::Neq => "<>",
            CompareOp::Lt => "<",
            CompareOp::Lte => "<=",
            CompareOp::Gt => ">",
            CompareOp::Gte => ">=",
        }
    }

    pub fn applies_to_ordered(self, ty: LogicalType) -> bool {
        matches!(self, CompareOp::Eq | CompareOp::Neq) || ty != LogicalType::Boolean
    }
}

#[derive(Debug, Clone)]
enum TypedIndex {
    Int {
        values: BTreeMap<i64, Bitmap>,
    },
    Text {
        values: BTreeMap<String, Bitmap>,
    },
    Bool {
        /// bitmap of positions equal to TRUE (non-NULL only).
        trues: Bitmap,
    },
}

/// A bitmap index for one column, stamped with the snapshot version at which
/// the underlying batch was built.
#[derive(Debug, Clone)]
pub struct ColumnIndex {
    column: String,
    ty: LogicalType,
    n: usize,
    nulls: Bitmap,
    typed: TypedIndex,
    built_at: u64,
}

impl ColumnIndex {
    /// Build an index from a typed batch column at snapshot version `built_at`.
    pub fn build(batch: &TypedBatch, column: &str, built_at: u64) -> Result<Self> {
        let spec = batch
            .column_spec(column)
            .ok_or_else(|| TritmapError::UnknownColumn(column.to_string()))?;
        let n = batch.len();
        let values = batch.column_values(column)?;
        let mut nulls = Bitmap::new(n);

        fn insert<K: Ord>(map: &mut BTreeMap<K, Bitmap>, key: K, row: usize, n: usize) {
            map.entry(key).or_insert_with(|| Bitmap::new(n)).set(row);
        }

        let typed = match spec.ty {
            LogicalType::Integer => {
                let mut map: BTreeMap<i64, Bitmap> = BTreeMap::new();
                for (row, v) in values.into_iter().enumerate() {
                    match v {
                        None => nulls.set(row),
                        Some(Scalar::Int(i)) => insert(&mut map, i, row, n),
                        Some(other) => {
                            return Err(TritmapError::internal(format!(
                                "extracted {other} from INTEGER column '{column}'"
                            )))
                        }
                    }
                }
                TypedIndex::Int { values: map }
            }
            LogicalType::Text => {
                let mut map: BTreeMap<String, Bitmap> = BTreeMap::new();
                for (row, v) in values.into_iter().enumerate() {
                    match v {
                        None => nulls.set(row),
                        Some(Scalar::Text(s)) => insert(&mut map, s, row, n),
                        Some(other) => {
                            return Err(TritmapError::internal(format!(
                                "extracted {other} from TEXT column '{column}'"
                            )))
                        }
                    }
                }
                TypedIndex::Text { values: map }
            }
            LogicalType::Boolean => {
                let mut trues = Bitmap::new(n);
                for (row, v) in values.into_iter().enumerate() {
                    match v {
                        None => nulls.set(row),
                        Some(Scalar::Bool(true)) => trues.set(row),
                        Some(Scalar::Bool(false)) => {} // represented implicitly
                        Some(other) => {
                            return Err(TritmapError::internal(format!(
                                "extracted {other} from BOOLEAN column '{column}'"
                            )))
                        }
                    }
                }
                TypedIndex::Bool { trues }
            }
        };

        Ok(Self {
            column: column.to_string(),
            ty: spec.ty,
            n,
            nulls,
            typed,
            built_at,
        })
    }

    pub fn column(&self) -> &str {
        &self.column
    }

    pub fn logical_type(&self) -> LogicalType {
        self.ty
    }

    pub fn row_space_len(&self) -> usize {
        self.n
    }

    pub fn built_at(&self) -> u64 {
        self.built_at
    }

    pub fn nulls(&self) -> &Bitmap {
        &self.nulls
    }

    fn check_literal(&self, literal: &Scalar) -> Result<()> {
        let ok = matches!(
            (self.ty, literal),
            (LogicalType::Integer, Scalar::Int(_))
                | (LogicalType::Text, Scalar::Text(_))
                | (LogicalType::Boolean, Scalar::Bool(_))
        );
        if !ok {
            return Err(TritmapError::TypeMismatch {
                column: self.column.clone(),
                actual: self.ty.as_str().to_string(),
                literal: literal.to_string(),
            });
        }
        Ok(())
    }

    /// Bitmap of non-NULL rows satisfying `value op literal` over the whole
    /// row space.
    fn matched_bitmap(&self, op: CompareOp, literal: &Scalar) -> Result<Bitmap> {
        self.check_literal(literal)?;
        if !op.applies_to_ordered(self.ty) {
            return Err(TritmapError::TypeMismatch {
                column: self.column.clone(),
                actual: self.ty.as_str().to_string(),
                literal: format!("{}{}", op.as_sql(), literal),
            });
        }
        let mut matched = Bitmap::new(self.n);
        match (&self.typed, literal) {
            (TypedIndex::Int { values }, Scalar::Int(k)) => match op {
                CompareOp::Neq => {
                    let eq = values.get(k).cloned().unwrap_or_else(|| Bitmap::new(self.n));
                    // non-NULL rows not equal = (all non-NULL) ∖ eq
                    matched = self.nulls.complement().difference(&eq)?;
                }
                _ => {
                    for (_v, bm) in values.range(ordered_bounds(op, *k)) {
                        matched = matched.union(bm)?;
                    }
                }
            },
            (TypedIndex::Text { values }, Scalar::Text(k)) => match op {
                CompareOp::Neq => {
                    let eq = values.get(k).cloned().unwrap_or_else(|| Bitmap::new(self.n));
                    matched = self.nulls.complement().difference(&eq)?;
                }
                _ => {
                    for (_v, bm) in values.range(ordered_bounds(op, k.clone())) {
                        matched = matched.union(bm)?;
                    }
                }
            },
            (TypedIndex::Bool { trues }, Scalar::Bool(k)) => {
                // Booleans support only = / <>, both via literal membership.
                let eq_true = *k;
                let eq_matches = if eq_true {
                    trues.clone()
                } else {
                    // non-NULL falses = non-NULL universe minus trues
                    self.nulls.complement().difference(trues)?
                };
                matched = match op {
                    CompareOp::Eq => eq_matches,
                    CompareOp::Neq => eq_matches.complement().difference(&self.nulls)?,
                    _ => unreachable!("rejected above"),
                };
            }
            _ => unreachable!("type/literal checked"),
        }
        Ok(matched)
    }

    /// Evaluate `column op literal` on an explicit live domain. NULL rows in
    /// the domain are UNKNOWN; deleted rows belong to nothing.
    pub fn evaluate_compare(
        &self,
        op: CompareOp,
        literal: &Scalar,
        domain: &Bitmap,
    ) -> Result<TritPartition> {
        if domain.len() != self.n {
            return Err(TritmapError::Partition(
                crate::partition::PartitionError::UniverseMismatch {
                    which: Trit::Unknown,
                    expected: self.n,
                    got: domain.len(),
                },
            ));
        }
        let matched = self.matched_bitmap(op, literal)?;
        let t = matched.intersection(domain)?;
        let u = self.nulls.intersection(domain)?;
        let f = domain.difference(&t.union(&u)?)?;
        TritPartition::with_domain(domain.clone(), t, f, u).map_err(Into::into)
    }

    /// Evaluate `column IS NULL` (or `IS NOT NULL`). These predicates never
    /// produce UNKNOWN themselves.
    pub fn evaluate_is_null(&self, negated: bool, domain: &Bitmap) -> Result<TritPartition> {
        if domain.len() != self.n {
            return Err(TritmapError::Partition(
                crate::partition::PartitionError::UniverseMismatch {
                    which: Trit::Unknown,
                    expected: self.n,
                    got: domain.len(),
                },
            ));
        }
        let is_null = self.nulls.intersection(domain)?;
        let is_not_null = domain.difference(&is_null)?;
        let (t, f) = if negated {
            (is_not_null, is_null)
        } else {
            (is_null, is_not_null)
        };
        let u = Bitmap::new(self.n);
        TritPartition::with_domain(domain.clone(), t, f, u).map_err(Into::into)
    }
}

/// BTreeMap range bounds for the four ordered comparisons plus equality.
/// `NEQ` is handled by the caller because it is not a contiguous range.
fn ordered_bounds<K: Ord + Clone>(
    op: CompareOp,
    k: K,
) -> (std::ops::Bound<K>, std::ops::Bound<K>) {
    use std::ops::Bound::{Excluded, Included, Unbounded};
    match op {
        CompareOp::Eq => (Included(k.clone()), Included(k)),
        CompareOp::Lt => (Unbounded, Excluded(k)),
        CompareOp::Lte => (Unbounded, Included(k)),
        CompareOp::Gt => (Excluded(k.clone()), Unbounded),
        CompareOp::Gte => (Included(k), Unbounded),
        CompareOp::Neq => unreachable!("NEQ handled by caller"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::batch::BatchBuilder;

    fn sample() -> TypedBatch {
        // 9 rows: non-machine-word tail; deliberate NULL crossings.
        BatchBuilder::new()
            .add_integer(
                "age",
                vec![Some(10), None, Some(30), Some(10), None, Some(30), Some(42), None, Some(5)],
            )
            .add_text(
                "city",
                vec![
                    Some("NY"),
                    Some("NY"),
                    None,
                    Some("LA"),
                    None,
                    Some("LA"),
                    Some("SF"),
                    None,
                    Some("NY"),
                ],
            )
            .add_boolean(
                "vip",
                vec![Some(true), None, Some(false), Some(false), None, Some(true), None, Some(true), Some(false)],
            )
            .build()
            .unwrap()
    }

    #[test]
    fn equality_partitions_true_false_unknown() {
        let b = sample();
        let idx = ColumnIndex::build(&b, "age", 1).unwrap();
        let d = Bitmap::all(b.len());
        let p = idx
            .evaluate_compare(CompareOp::Eq, &Scalar::Int(10), &d)
            .unwrap();
        assert_eq!(p.true_set().iter_ones().collect::<Vec<_>>(), vec![0, 3]);
        assert_eq!(p.set(Trit::Unknown).iter_ones().collect::<Vec<_>>(), vec![1, 4, 7]);
        // every other live row is FALSE
        assert_eq!(
            p.set(Trit::False).iter_ones().collect::<Vec<_>>(),
            vec![2, 5, 6, 8]
        );
        p.validate().unwrap();
    }

    #[test]
    fn ordered_comparisons_on_index_ranges() {
        let b = sample();
        let idx = ColumnIndex::build(&b, "age", 1).unwrap();
        let d = Bitmap::all(b.len());
        let cases: &[(CompareOp, i64, Vec<usize>)] = &[
            (CompareOp::Lt, 30, vec![0, 3, 8]),
            (CompareOp::Lte, 30, vec![0, 2, 3, 5, 8]),
            (CompareOp::Gt, 10, vec![2, 5, 6]),
            (CompareOp::Gte, 30, vec![2, 5, 6]),
            (CompareOp::Neq, 10, vec![2, 5, 6, 8]),
            (CompareOp::Eq, 99, vec![]),
        ];
        for (op, k, want) in cases {
            let p = idx.evaluate_compare(*op, &Scalar::Int(*k), &d).unwrap();
            let mut got: Vec<usize> = p.true_set().iter_ones().collect();
            got.sort();
            assert_eq!(got, *want, "age {} {} (TRUE set)", op.as_sql(), k);
            // NULLs must stay UNKNOWN for every comparison.
            assert_eq!(
                p.set(Trit::Unknown).iter_ones().collect::<Vec<_>>(),
                vec![1, 4, 7],
                "age {} {} (UNKNOWN set)",
                op.as_sql(),
                k
            );
            p.validate().unwrap();
        }
    }

    #[test]
    fn text_ordering_and_equality() {
        let b = sample();
        let idx = ColumnIndex::build(&b, "city", 1).unwrap();
        let d = Bitmap::all(b.len());
        let p = idx
            .evaluate_compare(CompareOp::Eq, &Scalar::Text("NY".into()), &d)
            .unwrap();
        assert_eq!(p.true_set().iter_ones().collect::<Vec<_>>(), vec![0, 1, 8]);
        assert_eq!(p.set(Trit::Unknown).iter_ones().collect::<Vec<_>>(), vec![2, 4, 7]);
        let p2 = idx
            .evaluate_compare(CompareOp::Lt, &Scalar::Text("SF".into()), &d)
            .unwrap();
        // LA < NY < SF lexicographically: LA=3,5 ; NY=0,1,8
        assert_eq!(p2.true_set().iter_ones().collect::<Vec<_>>(), vec![0, 1, 3, 5, 8]);
    }

    #[test]
    fn is_null_is_data_not_unknown() {
        let b = sample();
        let idx = ColumnIndex::build(&b, "age", 1).unwrap();
        let d = Bitmap::all(b.len());
        let p = idx.evaluate_is_null(false, &d).unwrap();
        assert_eq!(p.true_set().iter_ones().collect::<Vec<_>>(), vec![1, 4, 7]);
        assert_eq!(p.set(Trit::Unknown).count_ones(), 0);
        let pn = idx.evaluate_is_null(true, &d).unwrap();
        assert_eq!(pn.set(Trit::False).iter_ones().collect::<Vec<_>>(), vec![1, 4, 7]);
        assert_eq!(pn.true_set().count_ones(), 6);
    }

    #[test]
    fn deleted_rows_in_no_set_for_leaf() {
        let b = sample(); // 9 rows
        let idx = ColumnIndex::build(&b, "age", 2).unwrap();
        // rows 1 (a NULL row) and 5 deleted.
        let domain = Bitmap::from_indices(9, [0, 2, 3, 4, 6, 7, 8]);
        let p = idx
            .evaluate_compare(CompareOp::Eq, &Scalar::Int(30), &domain)
            .unwrap();
        assert_eq!(p.true_set().iter_ones().collect::<Vec<_>>(), vec![2]);
        assert!(!p.set(Trit::Unknown).get(1), "deleted NULL row must not be UNKNOWN");
        assert_eq!(p.set(Trit::Unknown).iter_ones().collect::<Vec<_>>(), vec![4, 7]);
        for t in Trit::ALL {
            assert!(!p.set(t).get(5));
        }
        p.validate().unwrap();
    }

    #[test]
    fn type_mismatches_are_errors() {
        let b = sample();
        let idx = ColumnIndex::build(&b, "age", 1).unwrap();
        let d = Bitmap::all(9);
        assert!(matches!(
            idx.evaluate_compare(CompareOp::Eq, &Scalar::Text("x".into()), &d),
            Err(TritmapError::TypeMismatch { .. })
        ));
        assert!(matches!(
            idx.evaluate_compare(CompareOp::Lt, &Scalar::Bool(true), &d),
            Err(TritmapError::TypeMismatch { .. })
        ));
    }
}
