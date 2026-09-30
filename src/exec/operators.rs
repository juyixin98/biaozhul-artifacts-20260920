//! Set-oriented physical operators used by the decorrelated pipeline.
//!
//! The key operator is a hash group table on the inner relation's correlation
//! columns, probed with `LEFT OUTER` semantics. All operators are deliberately
//! small and typed; they work on [`Batch`] and positional indices from the
//! validated plan.

use crate::batch::{Batch, Datum, Scalar, Value};
use crate::query::plan::{AggSpec, KeyPair, SubqueryPlan};
use crate::query::{ErrorCategory, QueryError, QueryResult};
use std::collections::HashMap;

/// Inner rows grouped by their full correlation-key tuple. Inner rows whose
/// key tuple contains any SQL NULL are collected separately (`null_singletons`)
/// because no outer row can ever match them under SQL equality — they are
/// excluded from every group aggregate.
#[derive(Debug, Default)]
pub struct InnerGroups {
    /// Key (NULL = NULL here, structural identity) -> inner row indices in
    /// ascending insertion order.
    groups: HashMap<Vec<Datum>, Vec<usize>>,
    pub null_key_rows: usize,
}

impl InnerGroups {
    /// Build groups from the inner batch for one subquery.
    pub fn build(inner: &Batch, keys: &[KeyPair]) -> Self {
        let mut table: HashMap<Vec<Datum>, Vec<usize>> = HashMap::new();
        let mut null_key_rows = 0usize;
        for row in 0..inner.len() {
            let mut key = Vec::with_capacity(keys.len());
            let mut any_null = false;
            for k in keys {
                let d = inner.cell(k.inner_index, row);
                if d.is_null() {
                    any_null = true;
                }
                key.push(d);
            }
            if any_null {
                null_key_rows += 1;
            } else {
                table.entry(key).or_default().push(row);
            }
        }
        InnerGroups { groups: table, null_key_rows }
    }

    pub fn len(&self) -> usize {
        self.groups.len()
    }

    pub fn is_empty(&self) -> bool {
        self.groups.is_empty()
    }

    /// Probe with an outer row's key. Returns the matched inner rows (always in
    /// ascending inner-row order). A NULL outer key, or no matching group,
    /// yields no rows — the LEFT OUTER NULL group.
    pub fn probe(&self, outer: &Batch, keys: &[KeyPair], outer_row: usize) -> Vec<usize> {
        let mut key = Vec::with_capacity(keys.len());
        for k in keys {
            let d = outer.cell(k.outer_index, outer_row);
            if d.is_null() {
                return Vec::new();
            }
            key.push(d);
        }
        self.groups.get(&key).cloned().unwrap_or_default()
    }
}

/// Result of reducing one correlated group.
#[derive(Debug, Clone)]
pub enum GroupResult {
    /// Boolean mark (EXISTS / NOT EXISTS).
    Mark(bool),
    /// Scalar (aggregate or bare scalar); NULL = SQL NULL.
    Scalar(Datum),
}

/// Reduce the matched inner rows of one outer row.
///
/// Shared by both execution strategies so the aggregation *rule* (including
/// COUNT-vs-SUM on an empty or all-NULL group) exists in exactly one place;
/// the strategies differ only in how the matched row set is obtained
/// (nested-loop probe vs hash probe).
pub fn reduce_group(
    sq: &SubqueryPlan,
    inner: &Batch,
    matched: &[usize],
    outer_row: usize,
) -> QueryResult<GroupResult> {
    use crate::query::SubqueryKind::*;
    match sq.kind {
        Exists => Ok(GroupResult::Mark(!matched.is_empty())),
        NotExists => Ok(GroupResult::Mark(matched.is_empty())),
        ScalarAgg => Ok(GroupResult::Scalar(aggregate(&sq.agg, inner, matched, outer_row)?)),
        Scalar => {
            let col = sq.value_inner_index.ok_or_else(|| {
                QueryError::new(
                    ErrorCategory::Internal,
                    "missing_scalar_value",
                    "validated scalar subquery has no value column",
                    "exec",
                )
            })?;
            if matched.len() > 1 {
                return Err(cardinality_error(outer_row, matched.len()));
            }
            match matched.first() {
                None => Ok(GroupResult::Scalar(Datum::null())),
                Some(&r) => Ok(GroupResult::Scalar(inner.cell(col, r))),
            }
        }
        NotIn => Err(QueryError::new(
            ErrorCategory::UnsupportedNotIn,
            "not_in_unsupported",
            "NOT IN must be rejected before execution",
            "exec",
        )),
    }
}

fn cardinality_error(outer_row: usize, matched: usize) -> QueryError {
    QueryError::new(
        ErrorCategory::CardinalityViolation,
        "cardinality_violation",
        format!(
            "scalar subquery returned {} rows for outer row #{}; at most 1 is allowed",
            matched, outer_row
        ),
        "exec",
    )
    .with_detail(serde_json::json!({ "outer_row": outer_row, "matched_rows": matched }))
}

fn aggregate(spec: &AggSpec, inner: &Batch, rows: &[usize], _outer_row: usize) -> QueryResult<Datum> {
    match spec {
        AggSpec::None => Err(QueryError::new(
            ErrorCategory::Internal,
            "bad_agg_spec",
            "aggregate called with AggSpec::None",
            "exec",
        )),
        AggSpec::CountStar => Ok(Datum::int(rows.len() as i64)),
        AggSpec::CountColumn(c) => {
            let n = rows.iter().filter(|&&r| !inner.cell(*c, r).is_null()).count() as i64;
            Ok(Datum::int(n))
        }
        AggSpec::Sum(c) => {
            // SQL: skip NULL inputs. Empty set AND all-NULL set both yield NULL.
            let mut acc: Option<Datum> = None;
            for &r in rows {
                let d = inner.cell(*c, r);
                match d.value {
                    None => continue,
                    Some(Scalar::Val(Value::Int(i))) => {
                        acc = Some(match acc {
                            None => Datum::int(i),
                            Some(prev) => match prev.value {
                                Some(Scalar::Val(Value::Int(p))) => Datum::int(checked_add_i64(p, i)?),
                                _ => unreachable!("type-checked at validation"),
                            },
                        });
                    }
                    Some(Scalar::Val(Value::Float(f))) => {
                        acc = Some(match acc {
                            None => Datum::from_value(Value::Float(f)),
                            Some(prev) => match prev.value {
                                Some(Scalar::Val(Value::Float(p))) => {
                                    Datum::from_value(Value::Float(p + f))
                                }
                                _ => unreachable!("type-checked at validation"),
                            },
                        });
                    }
                    other => {
                        return Err(QueryError::new(
                            ErrorCategory::TypeMismatch,
                            "sum_non_numeric",
                            format!("SUM argument must be numeric, got {:?}", other.map(|_| "…")),
                            "exec",
                        ));
                    }
                }
            }
            Ok(acc.unwrap_or_else(Datum::null))
        }
    }
}

fn checked_add_i64(a: i64, b: i64) -> QueryResult<i64> {
    a.checked_add(b).ok_or_else(|| {
        QueryError::new(
            ErrorCategory::NumericOverflow,
            "sum_overflow",
            format!("Int64 SUM overflow adding {} + {}", a, b),
            "exec",
        )
    })
}
