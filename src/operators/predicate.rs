//! Predicate evaluation with SQL three-valued logic (TRUE / FALSE / UNKNOWN).
//! A row passes a predicate only when the result is TRUE.

use crate::ast::{ColumnRef, Predicate, Scope};
use crate::batch::{Batch, Value};
use crate::error::{EngineError, EngineResult};

/// Evaluate a conjunction tree. Returns the tri-state result.
/// `Ok(None)` means UNKNOWN. Errors are reserved for unresolved names, which
/// validation should have ruled out (defense in depth).
pub fn eval<L>(p: &Predicate, lookup: &L) -> EngineResult<Option<bool>>
where
    L: Fn(&ColumnRef) -> EngineResult<Value>,
{
    match p {
        Predicate::And(a, b) => match (eval(a, lookup)?, eval(b, lookup)?) {
            // SQL AND truth table over {T,F,U}: F dominates, then U.
            (Some(false), _) | (_, Some(false)) => Ok(Some(false)),
            (None, _) | (_, None) => Ok(None),
            (Some(true), Some(true)) => Ok(Some(true)),
        },
        Predicate::NullTest { col, expect_null } => {
            let v = lookup(col)?;
            Ok(Some(v.is_null() == *expect_null))
        }
        Predicate::LitCmp { col, op, value } => {
            let v = lookup(col)?;
            Ok(op.eval(&v, value))
        }
        Predicate::ColCmp { left, op, right } => {
            let l = lookup(left)?;
            let r = lookup(right)?;
            Ok(op.eval(&l, &r))
        }
    }
}

/// True iff the predicate evaluates to TRUE (UNKNOWN/FALSE both reject).
pub fn passes<L>(p: &Predicate, lookup: &L) -> EngineResult<bool>
where
    L: Fn(&ColumnRef) -> EngineResult<Value>,
{
    Ok(eval(p, lookup)? == Some(true))
}

/// Evaluate a conjunction vector (empty => TRUE).
pub fn all_pass<L>(preds: &[Predicate], lookup: &L) -> EngineResult<bool>
where
    L: Fn(&ColumnRef) -> EngineResult<Value>,
{
    for p in preds {
        if !passes(p, lookup)? {
            return Ok(false);
        }
    }
    Ok(true)
}

/// Row binding over two batches: the outer row and an inner row.
pub struct RowEnv<'a> {
    pub outer: &'a Batch,
    pub outer_row: usize,
    pub inner: &'a Batch,
    pub inner_row: usize,
}

impl<'a> RowEnv<'a> {
    pub fn lookup(&self, r: &ColumnRef) -> EngineResult<Value> {
        match r.scope {
            Scope::Outer => Ok(self.outer.value(&r.name, self.outer_row)?),
            Scope::Inner => Ok(self.inner.value(&r.name, self.inner_row)?),
        }
    }
}

/// Binding with only the outer row present (outer WHERE evaluation).
pub struct OuterEnv<'a> {
    pub outer: &'a Batch,
    pub outer_row: usize,
}

impl<'a> OuterEnv<'a> {
    pub fn lookup(&self, r: &ColumnRef) -> EngineResult<Value> {
        match r.scope {
            Scope::Outer => Ok(self.outer.value(&r.name, self.outer_row)?),
            Scope::Inner => Err(EngineError::internal(
                "inner column referenced while evaluating outer-only predicate",
            )),
        }
    }
}
