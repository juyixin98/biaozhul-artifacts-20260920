//! Independent naive nested-loop reference engine.
//!
//! Verification compares the Leapfrog engine against this implementation. It
//! deliberately follows the textbook nested-loops strategy: every partial
//! assignment is materialized into a growing table, and each new atom scans
//! its whole relation for *every* partial row. That is exactly the behaviour
//! the Leapfrog engine must avoid, so besides cross-checking correctness the
//! oracle reports:
//!
//! * `row_visits` - total inner relation rows scanned, and
//! * `max_intermediate` - the largest partial-assignment table built on the
//!   way to the answer (evidence of intermediate Cartesian blow-up).
//!
//! When `max_intermediate` would exceed a safety cap the oracle fails with
//! [`crate::error::Error::Budget`]: for verification purposes that *is* the
//! expected result on an explosive plan - it shows the naive strategy is
//! unusable while the Leapfrog engine still answers.

use std::collections::{BTreeSet, HashMap};

use crate::error::{Error, ReasonCode, Result};

use super::model::{QueryShape, SchemaSource};
use super::trie::lex_cmp;
use super::value::Scalar;

/// Default safety cap on materialized partial assignments.
pub const DEFAULT_INTERMEDIATE_CAP: usize = 2_000_000;

/// Result of a naive enumeration.
#[derive(Debug, Clone)]
pub struct OracleOutcome {
    /// Variables in output (global) order.
    pub vars: Vec<String>,
    /// Sorted distinct answer tuples with their multiset multiplicity.
    pub rows: Vec<(Vec<Scalar>, u64)>,
    /// Total inner relation rows scanned by nested loops.
    pub row_visits: u64,
    /// Largest partial-assignment table materialized mid-query.
    pub max_intermediate: usize,
}

/// Run the independent nested-loop reference over `shape`.
///
/// The oracle performs its own validation (unknown relation, arity, bad
/// order hint); it never calls the planner, so a match is real cross-checking
/// rather than self-confirmation.
pub fn run_naive(
    shape: &QueryShape,
    catalog: &dyn SchemaSource,
    intermediate_cap: usize,
) -> Result<OracleOutcome> {
    if shape.atoms.is_empty() {
        return Err(Error::validation(
            ReasonCode::EmptyQuery,
            "oracle: query must contain at least one atom",
        ));
    }

    // Global order (same rule as the planner, checked locally).
    let mut first_seen: Vec<String> = Vec::new();
    let mut known: BTreeSet<String> = BTreeSet::new();
    for atom in &shape.atoms {
        let arity = catalog.arity(&atom.relation).ok_or_else(|| {
            Error::validation(
                ReasonCode::UnknownRelation,
                format!("oracle: relation {} is not loaded", atom.relation),
            )
        })?;
        if atom.vars.len() != arity {
            return Err(Error::validation(
                ReasonCode::ArityMismatch,
                format!(
                    "oracle: atom {} binds {} variables, relation has arity {arity}",
                    atom.relation,
                    atom.vars.len()
                ),
            ));
        }
        for var in &atom.vars {
            if known.insert(var.clone()) {
                first_seen.push(var.clone());
            }
        }
    }
    let global_order = match &shape.order {
        Some(order) => {
            let mut hint = order.clone();
            let mut required = first_seen.clone();
            hint.sort();
            required.sort();
            if hint != required {
                return Err(Error::validation(
                    ReasonCode::BadOrderHint,
                    "oracle: order hint is not a permutation of the query variables",
                ));
            }
            order.clone()
        }
        None => first_seen,
    };
    let var_index: HashMap<&str, usize> = global_order
        .iter()
        .enumerate()
        .map(|(i, v)| (v.as_str(), i))
        .collect();
    let nvars = global_order.len();

    // A partial assignment is a slot per variable (None = unbound) plus the
    // multiplicity of the input-row combination that produced it.
    let mut partials: Vec<(Vec<Option<Scalar>>, u64)> = vec![(vec![None; nvars], 1)];
    let mut row_visits = 0u64;
    let mut max_intermediate = 1usize;

    for atom in &shape.atoms {
        let rows = catalog
            .multiset_rows(&atom.relation)
            .expect("existence and arity validated above");
        let slots: Vec<usize> = atom
            .vars
            .iter()
            .map(|v| *var_index.get(v.as_str()).expect("variable known"))
            .collect();

        let mut next: Vec<(Vec<Option<Scalar>>, u64)> = Vec::new();
        for (partial, partial_mult) in &partials {
            for (row, row_mult) in &rows {
                row_visits += 1;
                // Consistency with the outer partial assignment, including
                // repeated variables inside one atom.
                let mut consistent = true;
                for (col, &slot) in slots.iter().enumerate() {
                    if let Some(prev) = &partial[slot] {
                        if *prev != row[col] {
                            consistent = false;
                            break;
                        }
                    }
                    for earlier in 0..col {
                        if slots[earlier] == slot && row[earlier] != row[col] {
                            consistent = false;
                            break;
                        }
                    }
                    if !consistent {
                        break;
                    }
                }
                if !consistent {
                    continue;
                }
                let mut extended = partial.clone();
                for (col, &slot) in slots.iter().enumerate() {
                    if extended[slot].is_none() {
                        extended[slot] = Some(row[col].clone());
                    }
                }
                next.push((extended, partial_mult.saturating_mul(*row_mult)));
            }
        }
        max_intermediate = max_intermediate.max(next.len());
        if next.len() > intermediate_cap {
            return Err(Error::Budget(format!(
                "naive nested-loop materialized {} partial assignments after atom {} \
                 (cap {intermediate_cap}); this is the intermediate-blow-up case",
                next.len(),
                atom.relation
            )));
        }
        partials = next;
    }

    // Every variable appears in at least one atom, so all slots are bound.
    let mut answer: Vec<(Vec<Scalar>, u64)> = partials
        .into_iter()
        .map(|(partial, mult)| {
            (
                partial
                    .into_iter()
                    .map(|v| v.expect("every join variable is bound"))
                    .collect(),
                mult,
            )
        })
        .collect();
    answer.sort_by(|a, b| lex_cmp(&a.0, &b.0));

    // Defensive merge: two different row combinations should never produce
    // the same full tuple because input rows are distinct, but the oracle is
    // verification code - make multiset aggregation explicit.
    let mut merged: Vec<(Vec<Scalar>, u64)> = Vec::with_capacity(answer.len());
    for (tuple, mult) in answer {
        match merged.last_mut() {
            Some((prev, prev_mult)) if *prev == tuple => *prev_mult += mult,
            _ => merged.push((tuple, mult)),
        }
    }

    Ok(OracleOutcome {
        vars: global_order,
        rows: merged,
        row_visits,
        max_intermediate,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::query::model::Atom;
    use crate::query::trie::Trie;

    fn s(i: i64) -> Scalar {
        Scalar::Int(i)
    }

    struct Catalog(BTreeMap<String, Trie>);

    impl SchemaSource for Catalog {
        fn arity(&self, relation: &str) -> Option<usize> {
            self.0.get(relation).map(Trie::arity)
        }
        fn multiset_rows(&self, relation: &str) -> Option<Vec<(Vec<Scalar>, u64)>> {
            self.0.get(relation).map(Trie::distinct_leaves)
        }
        fn version(&self) -> u64 {
            1
        }
    }

    #[test]
    fn naive_triangle_with_multiplicity() {
        let mut relations = BTreeMap::new();
        relations.insert(
            "R".to_string(),
            Trie::build(vec![vec![s(1), s(2)], vec![s(1), s(2)], vec![s(2), s(3)]]),
        );
        relations.insert(
            "S".to_string(),
            Trie::build(vec![vec![s(2), s(3)], vec![s(3), s(4)]]),
        );
        relations.insert(
            "T".to_string(),
            Trie::build(vec![vec![s(1), s(3)], vec![s(2), s(4)]]),
        );
        let catalog = Catalog(relations);
        let shape = QueryShape::new(vec![
            Atom::new("R", &["a", "b"]),
            Atom::new("S", &["b", "c"]),
            Atom::new("T", &["a", "c"]),
        ]);
        let outcome = run_naive(&shape, &catalog, DEFAULT_INTERMEDIATE_CAP).unwrap();
        assert_eq!(
            outcome.rows,
            vec![
                (vec![s(1), s(2), s(3)], 2),
                (vec![s(2), s(3), s(4)], 1),
            ]
        );
        assert_eq!(outcome.max_intermediate, 3);
    }

    #[test]
    fn empty_query_is_rejected_independently() {
        let err = run_naive(
            &QueryShape::new(vec![]),
            &Catalog(BTreeMap::new()),
            DEFAULT_INTERMEDIATE_CAP,
        )
        .unwrap_err();
        assert_eq!(err.reason().unwrap().code, ReasonCode::EmptyQuery);
    }
}
