//! Typed values, null ordering and domain encoding.
//!
//! The join operates on a totally ordered domain. Every distinct [`Datum`] of
//! a variable is mapped to a dense `u32` *code* whose numeric order preserves
//! the [`Datum`] ordering, so the Triejoin only ever compares integers.
//!
//! ## Null policy (explicit and bounded)
//! `NULL` is a first-class domain value with a fixed position in the total
//! order. SQL natural-join semantics are *null-rejecting on join variables*:
//! two `NULL`s do **not** match. We therefore never put a `NULL` on an
//! intersectable (join-key) level under
//! [`NullPolicy::SqlMatchNullsReject`]; rows carrying a null on a join
//! variable are filtered at build time. The alternative
//! [`NullPolicy::MatchNulls`] treats nulls as an ordinary equal value
//! (nested-loop / Datalog semantics). Either way the behaviour is chosen per
//! query, never implicit.

use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet};

use crate::error::Result;

/// A typed scalar. Ordering is total and deterministic:
/// `Null < Int(..) < Text(..)`, ints numeric, text lexicographic (UTF-8 bytes).
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum Datum {
    Null,
    Int(i64),
    Text(String),
}

impl Datum {
    pub fn is_null(&self) -> bool {
        matches!(self, Datum::Null)
    }

    /// Type tag used for schema/type checks (`"null"`, `"int"`, `"text"`).
    pub fn type_tag(&self) -> &'static str {
        match self {
            Datum::Null => "null",
            Datum::Int(_) => "int",
            Datum::Text(_) => "text",
        }
    }
}

impl Ord for Datum {
    fn cmp(&self, other: &Self) -> Ordering {
        match (self, other) {
            (Datum::Null, Datum::Null) => Ordering::Equal,
            (Datum::Null, _) => Ordering::Less,
            (_, Datum::Null) => Ordering::Greater,
            (Datum::Int(a), Datum::Int(b)) => a.cmp(b),
            (Datum::Int(_), Datum::Text(_)) => Ordering::Less,
            (Datum::Text(_), Datum::Int(_)) => Ordering::Greater,
            (Datum::Text(a), Datum::Text(b)) => a.as_bytes().cmp(b.as_bytes()),
        }
    }
}

impl PartialOrd for Datum {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

/// How nulls on join variables are treated.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NullPolicy {
    /// SQL semantics: `NULL = NULL` is unknown, so such rows never join.
    SqlMatchNullsReject,
    /// Datalog/nested-loop semantics: null is an ordinary comparable value and
    /// null matches null.
    MatchNulls,
}

impl NullPolicy {
    pub fn parse(s: &str) -> Result<Self> {
        match s {
            "sql" | "sql_reject" | "match_nulls_reject" => Ok(NullPolicy::SqlMatchNullsReject),
            "match_nulls" | "equal_nulls" => Ok(NullPolicy::MatchNulls),
            other => Err(crate::error::JoinError::new(
                crate::error::ErrorCode::NullPolicyViolation,
                format!("unknown null policy `{other}` (expected sql|match_nulls)"),
            )),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            NullPolicy::SqlMatchNullsReject => "sql",
            NullPolicy::MatchNulls => "match_nulls",
        }
    }
}

impl Default for NullPolicy {
    fn default() -> Self {
        NullPolicy::SqlMatchNullsReject
    }
}

/// Dense, order-preserving codebook for one variable.
///
/// Two-phase use, so codes are never invalidated by a later insert:
/// 1. [`Domain::add`] every datum that may appear (the join does this for all
///    retained rows, across all relations sharing the variable, *before*
///    building any trie);
/// 2. [`Domain::freeze`] once; afterwards codes are dense (`0..len`) and
///    stable in ascending [`Datum`] order.
#[derive(Debug, Clone, Default)]
pub struct Domain {
    pending: BTreeSet<Datum>,
    values: Vec<Datum>,
    by_code: BTreeMap<Datum, u32>,
    frozen: bool,
}

impl Domain {
    pub fn new() -> Self {
        Self::default()
    }

    /// Stage a datum for inclusion. No code is assigned until [`Domain::freeze`].
    pub fn add(&mut self, datum: Datum) {
        debug_assert!(!self.frozen, "add() after freeze()");
        self.pending.insert(datum);
    }

    /// Finalize dense, order-preserving codes. Idempotent.
    pub fn freeze(&mut self) {
        if self.frozen {
            return;
        }
        self.values = self.pending.iter().cloned().collect();
        self.by_code = self
            .values
            .iter()
            .enumerate()
            .map(|(i, d)| (d.clone(), i as u32))
            .collect();
        self.frozen = true;
    }

    pub fn len(&self) -> usize {
        self.values.len()
    }

    pub fn is_empty(&self) -> bool {
        self.values.is_empty()
    }

    pub fn is_frozen(&self) -> bool {
        self.frozen
    }

    /// Stable code of a frozen datum.
    pub fn code_of(&self, datum: &Datum) -> Option<u32> {
        debug_assert!(self.frozen, "code_of() before freeze()");
        self.by_code.get(datum).copied()
    }

    /// Decode a code back to its datum.
    pub fn decode(&self, code: u32) -> Option<&Datum> {
        self.values.get(code as usize)
    }

    pub fn iter(&self) -> impl Iterator<Item = (u32, &Datum)> {
        self.values.iter().enumerate().map(|(i, d)| (i as u32, d))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn datum_total_order_is_stable() {
        let mut sorted = vec![
            Datum::Text("b".into()),
            Datum::Null,
            Datum::Int(2),
            Datum::Int(-9),
            Datum::Text("a".into()),
        ];
        sorted.sort();
        assert_eq!(
            sorted,
            vec![
                Datum::Null,
                Datum::Int(-9),
                Datum::Int(2),
                Datum::Text("a".into()),
                Datum::Text("b".into()),
            ]
        );
    }

    #[test]
    fn domain_codes_are_dense_and_order_preserving() {
        let mut d = Domain::new();
        d.add(Datum::Int(5));
        d.add(Datum::Int(1));
        d.add(Datum::Int(9));
        d.add(Datum::Int(5)); // idempotent
        d.freeze();
        assert_eq!(d.len(), 3);
        let c5 = d.code_of(&Datum::Int(5)).unwrap();
        let c1 = d.code_of(&Datum::Int(1)).unwrap();
        let c9 = d.code_of(&Datum::Int(9)).unwrap();
        assert!(c1 < c5 && c5 < c9);
        assert_eq!(d.decode(c1), Some(&Datum::Int(1)));
    }

    #[test]
    fn null_is_distinct_and_sorts_first() {
        let mut d = Domain::new();
        d.add(Datum::Null);
        d.add(Datum::Int(0));
        d.freeze();
        let cn = d.code_of(&Datum::Null).unwrap();
        let ci = d.code_of(&Datum::Int(0)).unwrap();
        assert!(cn < ci);
    }
}
