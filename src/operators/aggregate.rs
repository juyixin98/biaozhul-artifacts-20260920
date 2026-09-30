//! Aggregate computation over a correlated group of inner rows.
//!
//! The COUNT/SUM-on-empty-group distinction is the load-bearing semantic here:
//!   * COUNT(*) / COUNT(col) on zero (or all-NULL) rows => 0
//!   * SUM(col) on zero rows                          => NULL
//! The interpreter and decorrelating executor both go through this file.

use crate::ast::ScalarProj;
use crate::batch::Value;
use crate::error::{EngineError, EngineResult};

/// Compute the aggregate over `rows`, where each element is the projected
/// column value (None = NULL) for COUNT(col)/SUM(col); COUNT(*) passes no
/// column and simply receives the row count.
pub fn compute(proj: &ScalarProj, rows: &[Option<Value>]) -> EngineResult<Value> {
    match proj {
        ScalarProj::CountStar => Ok(Value::Int(rows.len() as i64)),
        ScalarProj::CountCol { col } => {
            if col == "__count_null__" {
                // COUNT(NULL) is always 0, including non-empty groups.
                return Ok(Value::Int(0));
            }
            let n = rows.iter().filter(|v| is_present(v)).count();
            Ok(Value::Int(n as i64))
        }
        ScalarProj::SumCol { col, float } => {
            let mut present = 0usize;
            if *float {
                let mut acc = 0.0f64;
                for v in rows {
                    if let Some(Value::Float(x)) = present_value(v) {
                        acc += x;
                        present += 1;
                    }
                }
                if present == 0 {
                    Ok(Value::Null)
                } else {
                    Ok(Value::Float(acc))
                }
            } else {
                let mut acc = 0i64;
                for v in rows {
                    if let Some(Value::Int(x)) = present_value(v) {
                        acc = acc.checked_add(*x).ok_or_else(|| {
                            EngineError::new(
                                crate::error::ErrorKind::LimitExceeded,
                                format!("SUM overflow while summing '{col}'"),
                            )
                        })?;
                        present += 1;
                    }
                }
                if present == 0 {
                    Ok(Value::Null)
                } else {
                    Ok(Value::Int(acc))
                }
            }
        }
    }
}

/// Project one column from a set of rows during execution.
/// Returns None at the slot for a NULL value; `rows` itself may be empty.
pub fn column_values(
    col: &str,
    batch: &crate::batch::Batch,
    row_indices: &[usize],
) -> EngineResult<Vec<Option<Value>>> {
    if col == "__count_null__" {
        // Parser placeholder for COUNT(NULL): every slot is NULL.
        return Ok(vec![None; row_indices.len()]);
    }
    row_indices
        .iter()
        .map(|&r| {
            let v = batch.value(col, r)?;
            Ok(if v.is_null() { None } else { Some(v) })
        })
        .collect()
}

fn is_present(v: &Option<Value>) -> bool {
    present_value(v).is_some()
}

fn present_value(v: &Option<Value>) -> Option<&Value> {
    v.as_ref().filter(|x| !x.is_null())
}
