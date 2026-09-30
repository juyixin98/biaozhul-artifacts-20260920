//! Typed scalar values and comparison semantics.
//!
//! Only four logical types are supported: 64-bit signed integers, 64-bit
//! floats, booleans and UTF-8 strings. Comparing values of different logical
//! types is always [`ErrorKind::TypeMismatch`] — there is no implicit coercion
//! where a boundary input (`"1"` vs `1`, `true` vs `1`) could silently compare
//! equal.

use std::cmp::Ordering;
use std::hash::{Hash, Hasher};

use serde::{Deserialize, Serialize};

use crate::error::{ErrorKind, IndexError, Result};

/// A non-NULL literal or column value. NULL lives one layer up as `Option<Scalar>`.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Scalar {
    Int(i64),
    Float(f64),
    Bool(bool),
    Text(String),
}

/// Canonical bit pattern for any NaN, so NaNs hash/eq consistently inside the
/// value index while never matching any user-supplied literal.
const CANONICAL_NAN_BITS: u64 = 0x7ff8_0000_0000_0000;

fn float_key(f: f64) -> u64 {
    if f.is_nan() {
        CANONICAL_NAN_BITS
    } else {
        f.to_bits()
    }
}

impl PartialEq for Scalar {
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            (Scalar::Int(a), Scalar::Int(b)) => a == b,
            (Scalar::Float(a), Scalar::Float(b)) => float_key(*a) == float_key(*b),
            (Scalar::Bool(a), Scalar::Bool(b)) => a == b,
            (Scalar::Text(a), Scalar::Text(b)) => a == b,
            _ => false,
        }
    }
}

impl Eq for Scalar {}

impl Hash for Scalar {
    fn hash<H: Hasher>(&self, state: &mut H) {
        std::mem::discriminant(self).hash(state);
        match self {
            Scalar::Int(v) => v.hash(state),
            // NaN never appears as a user literal; canonical bits keep Eq/Hash consistent.
            Scalar::Float(v) => float_key(*v).hash(state),
            Scalar::Bool(v) => v.hash(state),
            Scalar::Text(v) => v.hash(state),
        }
    }
}

impl Scalar {
    pub fn type_name(&self) -> &'static str {
        match self {
            Scalar::Int(_) => "int",
            Scalar::Float(_) => "float",
            Scalar::Bool(_) => "bool",
            Scalar::Text(_) => "text",
        }
    }

    /// SQL equality. Same-type non-NaN values only; cross-type is a hard error.
    pub fn sql_eq(&self, other: &Self) -> Result<bool> {
        match (self, other) {
            (Scalar::Int(a), Scalar::Int(b)) => Ok(a == b),
            (Scalar::Float(a), Scalar::Float(b)) => Ok(*a == *b), // NaN != anything, incl. NaN
            (Scalar::Bool(a), Scalar::Bool(b)) => Ok(a == b),
            (Scalar::Text(a), Scalar::Text(b)) => Ok(a == b),
            _ => Err(type_mismatch(self, other, "=")),
        }
    }

    /// Three-way order for `<`, `<=`, `>`, `>=`. Booleans have no SQL ordering here.
    pub fn sql_cmp(&self, other: &Self, op: CompareOp) -> Result<bool> {
        // NaN is unordered with respect to everything: every ordering test is false.
        if let (Scalar::Float(a), Scalar::Float(b)) = (self, other) {
            if a.is_nan() || b.is_nan() {
                return Ok(false);
            }
        }
        let ord = match (self, other) {
            (Scalar::Int(a), Scalar::Int(b)) => a.cmp(b),
            (Scalar::Float(a), Scalar::Float(b)) => {
                a.partial_cmp(b).expect("non-NaN floats are always comparable")
            }
            (Scalar::Text(a), Scalar::Text(b)) => a.cmp(b),
            (Scalar::Bool(_), Scalar::Bool(_)) => {
                return Err(IndexError::new(
                    ErrorKind::InvalidPredicate,
                    format!("operator {} is not valid for boolean values; use = or !=", op.as_str()),
                ));
            }
            _ => return Err(type_mismatch(self, other, op.as_str())),
        };
        Ok(match op {
            CompareOp::Lt => ord == Ordering::Less,
            CompareOp::Le => ord != Ordering::Greater,
            CompareOp::Gt => ord == Ordering::Greater,
            CompareOp::Ge => ord != Ordering::Less,
            _ => unreachable!("sql_cmp called with equality operator"),
        })
    }
}

fn type_mismatch(a: &Scalar, b: &Scalar, op: &str) -> IndexError {
    IndexError::new(
        ErrorKind::TypeMismatch,
        format!("cannot compare {a:?} ({}) with {b:?} ({}) using {op}", a.type_name(), b.type_name()),
    )
}

/// Leaf comparison operators.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CompareOp {
    #[serde(alias = "=", alias = "eq")]
    Eq,
    #[serde(alias = "!=", alias = "<>", alias = "ne")]
    Ne,
    #[serde(alias = "<", alias = "lt")]
    Lt,
    #[serde(alias = "<=", alias = "le")]
    Le,
    #[serde(alias = ">", alias = "gt")]
    Gt,
    #[serde(alias = ">=", alias = "ge")]
    Ge,
}

impl CompareOp {
    pub fn as_str(self) -> &'static str {
        match self {
            CompareOp::Eq => "=",
            CompareOp::Ne => "!=",
            CompareOp::Lt => "<",
            CompareOp::Le => "<=",
            CompareOp::Gt => ">",
            CompareOp::Ge => ">=",
        }
    }

    pub fn is_ordering(self) -> bool {
        !matches!(self, CompareOp::Eq | CompareOp::Ne)
    }

    /// Evaluate a comparison between two present (non-NULL) typed values.
    pub fn compare(self, left: &Scalar, right: &Scalar) -> Result<bool> {
        match self {
            CompareOp::Eq => left.sql_eq(right),
            CompareOp::Ne => Ok(!left.sql_eq(right)?),
            ord => left.sql_cmp(right, ord),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cross_type_comparison_is_typed_error() {
        let cases = [
            (Scalar::Int(1), Scalar::Float(1.0), "="),
            (Scalar::Int(1), Scalar::Text("1".into()), "="),
            (Scalar::Bool(true), Scalar::Int(1), "="),
            (Scalar::Text("a".into(), ), Scalar::Int(1), "<"),
        ];
        for (a, b, _) in cases {
            assert_eq!(CompareOp::Eq.compare(&a, &b).unwrap_err().kind, ErrorKind::TypeMismatch);
        }
    }

    #[test]
    fn nan_never_compares_equal_or_ordered() {
        let nan = Scalar::Float(f64::NAN);
        let one = Scalar::Float(1.0);
        for op in [CompareOp::Eq, CompareOp::Ne, CompareOp::Lt, CompareOp::Le, CompareOp::Gt, CompareOp::Ge] {
            let v = op.compare(&nan, &one).unwrap();
            assert_eq!(v, matches!(op, CompareOp::Ne), "NaN {} 1.0 -> {v}", op.as_str());
            let v = op.compare(&one, &nan).unwrap();
            assert_eq!(v, matches!(op, CompareOp::Ne), "1.0 {} NaN -> {v}", op.as_str());
        }
    }

    #[test]
    fn scalar_hash_key_is_stable() {
        use std::collections::hash_map::DefaultHasher;
        let mut h1 = DefaultHasher::new();
        Scalar::Float(f64::NAN).hash(&mut h1);
        let mut h2 = DefaultHasher::new();
        Scalar::Float(f64::NAN).hash(&mut h2);
        assert_eq!(h1.finish(), h2.finish());
        assert_eq!(Scalar::Float(f64::NAN), Scalar::Float(f64::NAN));
    }
}
