//! Atoms, conjunctive queries and resolved (validated) join plans.
//!
//! A query is a conjunction of atoms `R(var, var, ...)`. Natural-join
//! equality means the same variable name must be bound to the same value in
//! every atom that mentions it.
//!
//! Each relation is physically stored in one fixed (column) order. An atom
//! is *ordered* when its variables already follow the global join variable
//! order; a Leapfrog Triejoin needs exactly that. When every atom is already
//! ordered the stored tries are reused directly. Otherwise the planner
//! materializes a lexicographically permuted *view* of each affected
//! relation; the decision and the work it implies is reported in the plan so
//! diagnostics can show it.

use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};

use crate::error::{Error, ReasonCode, Result};

use super::trie::Trie;
use super::value::Scalar;

/// One atom `relation(variables...)` of a conjunctive query.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Atom {
    pub relation: String,
    #[serde(default)]
    pub vars: Vec<String>,
}

impl Atom {
    pub fn new(relation: impl Into<String>, vars: &[&str]) -> Self {
        Self {
            relation: relation.into(),
            vars: vars.iter().map(|s| (*s).to_string()).collect(),
        }
    }
}

/// A conjunctive query together with an optional caller-supplied global
/// variable order.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct QueryShape {
    pub atoms: Vec<Atom>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub order: Option<Vec<String>>,
}

impl QueryShape {
    pub fn new(atoms: Vec<Atom>) -> Self {
        Self { atoms, order: None }
    }

    /// Stable fingerprint identifying the query shape (used by cursor
    /// validation: a cursor may only resume the query it was issued for).
    pub fn fingerprint(&self) -> String {
        let mut s = String::new();
        for a in &self.atoms {
            s.push_str(&a.relation);
            s.push('(');
            s.push_str(&a.vars.join(","));
            s.push_str(");");
        }
        s.push('|');
        if let Some(order) = &self.order {
            s.push_str(&order.join(","));
        }
        s
    }
}

/// Resolved atom: variables reordered to the global order, plus the column
/// permutation that takes a stored row into view order.
#[derive(Debug, Clone)]
pub struct ResolvedAtom {
    pub relation: String,
    /// Variables of this atom in global order (deduplicated per atom).
    pub ordered_vars: Vec<String>,
    /// For each position in `ordered_vars`, the source column index.
    pub permutation: Vec<usize>,
    /// Whether the stored trie column order already matched.
    pub reused_native_trie: bool,
}

/// What the planner had to do to make every trie iterator share the global
/// variable order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum IndexPolicy {
    /// All atoms already follow the global order; stored tries reused.
    Native,
    /// At least one relation had to be lexicographically re-permuted.
    Reindexed,
}

/// A validated query bound to a specific catalog version.
#[derive(Debug)]
pub struct Plan {
    pub shape: QueryShape,
    pub global_order: Vec<String>,
    pub resolved: Vec<ResolvedAtom>,
    pub index_policy: IndexPolicy,
    /// One view trie per atom, in atom order; permuted views are rebuilt
    /// deterministically, which is what resumable cursors rely on.
    pub views: Vec<Trie>,
    pub catalog_version: u64,
}

impl Plan {
    pub fn var_count(&self) -> usize {
        self.global_order.len()
    }

    pub fn index_of(&self, var: &str) -> Option<usize> {
        self.global_order.iter().position(|v| v == var)
    }

    /// `participants(level)` is the list of atom indices whose view contains
    /// the level's variable.
    pub fn participants(&self, level: usize) -> Vec<usize> {
        let var = &self.global_order[level];
        self.resolved
            .iter()
            .enumerate()
            .filter(|(_, a)| a.ordered_vars.contains(var))
            .map(|(i, _)| i)
            .collect()
    }
}

/// Catalog view required by the planner: relation arity lookup and access to
/// the stored multiset rows (ordered columns).
pub trait SchemaSource {
    fn arity(&self, relation: &str) -> Option<usize>;
    /// Distinct rows with their multiplicity, in stored column order.
    fn multiset_rows(&self, relation: &str) -> Option<Vec<(Vec<Scalar>, u64)>>;
    fn version(&self) -> u64;
}

fn reject(code: ReasonCode, msg: impl Into<String>) -> Error {
    Error::validation(code, msg)
}

/// Planner knobs; the server configuration is mapped onto this type.
#[derive(Debug, Clone, Copy)]
pub struct PlannerConfig {
    /// Maximum number of distinct join variables a query may introduce.
    pub max_variables: usize,
    /// Whether the planner may materialize column-permuted views when atoms
    /// do not already follow the global variable order.
    pub allow_reindex: bool,
}

impl Default for PlannerConfig {
    fn default() -> Self {
        Self {
            max_variables: 16,
            allow_reindex: true,
        }
    }
}

/// Validate `shape` against `catalog` and build an executable [`Plan`].
///
/// The returned plan owns its view tries and is pinned to
/// [`SchemaSource::version`], which is what resumable cursors use to detect
/// stale catalogs.
pub fn resolve(
    shape: &QueryShape,
    catalog: &dyn SchemaSource,
    cfg: &PlannerConfig,
) -> Result<Plan> {
    if shape.atoms.is_empty() {
        return Err(reject(
            ReasonCode::EmptyQuery,
            "query must contain at least one atom",
        ));
    }

    // Structural validation per atom.
    for atom in &shape.atoms {
        let arity = catalog.arity(&atom.relation).ok_or_else(|| {
            reject(
                ReasonCode::UnknownRelation,
                format!("relation {} is not loaded", atom.relation),
            )
        })?;
        if atom.vars.is_empty() {
            return Err(reject(
                ReasonCode::ArityMismatch,
                format!(
                    "atom {}() binds no variables but the relation has arity {arity}",
                    atom.relation
                ),
            ));
        }
        if atom.vars.len() != arity {
            return Err(reject(
                ReasonCode::ArityMismatch,
                format!(
                    "atom {} binds {} variables but relation {} has arity {arity}",
                    atom.relation,
                    atom.vars.len(),
                    atom.relation
                ),
            ));
        }
        let mut seen = BTreeSet::new();
        for var in &atom.vars {
            if var.is_empty() {
                return Err(reject(
                    ReasonCode::ArityMismatch,
                    format!("atom {} contains an empty variable name", atom.relation),
                ));
            }
            if !seen.insert(var) {
                return Err(reject(
                    ReasonCode::RepeatedVariable,
                    format!(
                        "atom {} repeats variable {var}; an equality inside one \
                         relation must be expressed with two atoms",
                        atom.relation
                    ),
                ));
            }
        }
    }

    // Global variable order: caller hint when present (must be exact),
    // otherwise the order of first appearance across the atoms.
    let mut first_seen: Vec<String> = Vec::new();
    let mut known: BTreeSet<String> = BTreeSet::new();
    for atom in &shape.atoms {
        for var in &atom.vars {
            if known.insert(var.clone()) {
                first_seen.push(var.clone());
            }
        }
    }

    let global_order: Vec<String> = match &shape.order {
        Some(order) => {
            let mut hint = order.clone();
            let mut required = first_seen.clone();
            hint.sort();
            required.sort();
            if hint != required {
                return Err(reject(
                    ReasonCode::BadOrderHint,
                    format!(
                        "order hint must list every query variable exactly once; \
                         required order (first appearance) is [{}]",
                        first_seen.join(", ")
                    ),
                ));
            }
            order.clone()
        }
        None => first_seen,
    };

    if global_order.len() > cfg.max_variables {
        return Err(reject(
            ReasonCode::TooManyVariables,
            format!(
                "query introduces {} variables, configured maximum is {}",
                global_order.len(),
                cfg.max_variables
            ),
        ));
    }

    // Bind each atom's columns to the global order.
    let mut resolved = Vec::with_capacity(shape.atoms.len());
    let mut any_reindexed = false;
    for atom in &shape.atoms {
        let ordered_vars: Vec<String> = global_order
            .iter()
            .filter(|var| atom.vars.contains(var))
            .cloned()
            .collect();
        let permutation: Vec<usize> = ordered_vars
            .iter()
            .map(|var| {
                atom.vars
                    .iter()
                    .position(|v| v == var)
                    .expect("ordered vars come from the atom")
            })
            .collect();
        let reused_native_trie = ordered_vars == atom.vars;
        any_reindexed |= !reused_native_trie;
        resolved.push(ResolvedAtom {
            relation: atom.relation.clone(),
            ordered_vars,
            permutation,
            reused_native_trie,
        });
    }

    if any_reindexed && !cfg.allow_reindex {
        let offenders: Vec<&str> = resolved
            .iter()
            .filter(|r| !r.reused_native_trie)
            .map(|r| r.relation.as_str())
            .collect();
        return Err(reject(
            ReasonCode::IncompatibleOrder,
            format!(
                "atom(s) over relation(s) [{}] contradict the global variable order \
                 and reindexing is disabled on this server",
                offenders.join(", ")
            ),
        ));
    }

    // Materialize one view trie per atom. Native-order atoms reuse their
    // stored rows unchanged; others are lexicographically permuted. Views
    // are deterministic, so a resumable cursor always rebuilds identical
    // tries from the same catalog version.
    let mut views = Vec::with_capacity(shape.atoms.len());
    for resolved_atom in &resolved {
        let rows = catalog
            .multiset_rows(&resolved_atom.relation)
            .ok_or_else(|| {
                reject(
                    ReasonCode::UnknownRelation,
                    format!("relation {} is not loaded", resolved_atom.relation),
                )
            })?;
        let view = if resolved_atom.reused_native_trie {
            Trie::from_distinct_rows(rows, resolved_atom.ordered_vars.len())
        } else {
            let permuted = rows
                .into_iter()
                .map(|(row, mult)| {
                    let view_row: Vec<Scalar> = resolved_atom
                        .permutation
                        .iter()
                        .map(|&src| row[src].clone())
                        .collect();
                    (view_row, mult)
                })
                .collect();
            Trie::from_distinct_rows(permuted, resolved_atom.ordered_vars.len())
        };
        views.push(view);
    }

    Ok(Plan {
        shape: shape.clone(),
        global_order,
        resolved,
        index_policy: if any_reindexed {
            IndexPolicy::Reindexed
        } else {
            IndexPolicy::Native
        },
        views,
        catalog_version: catalog.version(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    fn s(i: i64) -> Scalar {
        Scalar::Int(i)
    }

    struct MockCatalog {
        relations: BTreeMap<String, Trie>,
        version: u64,
    }

    impl MockCatalog {
        fn new(version: u64) -> Self {
            let mut relations = BTreeMap::new();
            relations.insert(
                "R".to_string(),
                Trie::build(vec![vec![s(1), s(2)], vec![s(2), s(3)]]),
            );
            relations.insert(
                "S".to_string(),
                Trie::build(vec![vec![s(2), s(5)], vec![s(3), s(6)]]),
            );
            Self {
                relations,
                version,
            }
        }
    }

    impl SchemaSource for MockCatalog {
        fn arity(&self, relation: &str) -> Option<usize> {
            self.relations.get(relation).map(Trie::arity)
        }
        fn multiset_rows(&self, relation: &str) -> Option<Vec<(Vec<Scalar>, u64)>> {
            self.relations.get(relation).map(Trie::distinct_leaves)
        }
        fn version(&self) -> u64 {
            self.version
        }
    }

    #[test]
    fn native_order_reuses_tries_and_pins_version() {
        let catalog = MockCatalog::new(7);
        let shape = QueryShape::new(vec![Atom::new("R", &["a", "b"]), Atom::new("S", &["b", "c"])]);
        let plan = resolve(&shape, &catalog, &PlannerConfig::default()).unwrap();
        assert_eq!(plan.global_order, vec!["a", "b", "c"]);
        assert_eq!(plan.index_policy, IndexPolicy::Native);
        assert!(plan.resolved.iter().all(|a| a.reused_native_trie));
        assert_eq!(plan.catalog_version, 7);
        assert_eq!(plan.participants(0), vec![0]);
        assert_eq!(plan.participants(1), vec![0, 1]);
        assert_eq!(plan.participants(2), vec![1]);
    }

    #[test]
    fn permuted_atom_is_reindexed_with_correct_view() {
        let catalog = MockCatalog::new(1);
        // S is stored as (b, c); the atom reads its columns as (c, b).
        let shape = QueryShape::new(vec![Atom::new("R", &["a", "b"]), Atom::new("S", &["c", "b"])]);
        let plan = resolve(&shape, &catalog, &PlannerConfig::default()).unwrap();
        assert_eq!(plan.global_order, vec!["a", "b", "c"]);
        assert_eq!(plan.index_policy, IndexPolicy::Reindexed);
        assert!(!plan.resolved[1].reused_native_trie);
        assert_eq!(plan.resolved[1].ordered_vars, vec!["b", "c"]);
        assert_eq!(plan.resolved[1].permutation, vec![1, 0]);
        // Stored S rows (2,5),(3,6) become view rows (b,c) = (5,2),(6,3).
        assert_eq!(plan.views[1].multiplicity_of(&[s(5), s(2)]), 1);
        assert_eq!(plan.views[1].multiplicity_of(&[s(6), s(3)]), 1);
        assert_eq!(plan.views[1].multiplicity_of(&[s(2), s(5)]), 0);
    }

    #[test]
    fn reindex_can_be_disabled() {
        let catalog = MockCatalog::new(1);
        let shape = QueryShape::new(vec![Atom::new("R", &["a", "b"]), Atom::new("S", &["c", "b"])]);
        let cfg = PlannerConfig {
            max_variables: 16,
            allow_reindex: false,
        };
        let err = resolve(&shape, &catalog, &cfg).unwrap_err();
        assert_eq!(err.reason().unwrap().code, ReasonCode::IncompatibleOrder);
    }

    #[test]
    fn explicit_order_hint_must_be_exact() {
        let catalog = MockCatalog::new(1);
        let mut shape = QueryShape::new(vec![Atom::new("R", &["a", "b"]), Atom::new("S", &["b", "c"])]);
        shape.order = Some(vec!["a".into(), "b".into()]);
        let err = resolve(&shape, &catalog, &PlannerConfig::default()).unwrap_err();
        assert_eq!(err.reason().unwrap().code, ReasonCode::BadOrderHint);

        shape.order = Some(vec!["a".into(), "b".into(), "c".into(), "c".into()]);
        let err = resolve(&shape, &catalog, &PlannerConfig::default()).unwrap_err();
        assert_eq!(err.reason().unwrap().code, ReasonCode::BadOrderHint);

        shape.order = Some(vec!["c".into(), "b".into(), "a".into()]);
        let plan = resolve(&shape, &catalog, &PlannerConfig::default()).unwrap();
        assert_eq!(plan.global_order, vec!["c", "b", "a"]);
        assert_eq!(plan.index_policy, IndexPolicy::Reindexed);
    }

    #[test]
    fn structural_rejections_have_specific_codes() {
        let catalog = MockCatalog::new(1);
        let cfg = PlannerConfig::default();
        let cases: Vec<(QueryShape, ReasonCode)> = vec![
            (QueryShape::new(vec![]), ReasonCode::EmptyQuery),
            (
                QueryShape::new(vec![Atom::new("Nope", &["a"])]),
                ReasonCode::UnknownRelation,
            ),
            (
                QueryShape::new(vec![Atom::new("R", &["a", "b", "c"])]),
                ReasonCode::ArityMismatch,
            ),
            (
                QueryShape::new(vec![Atom::new("R", &["a", "a"])]),
                ReasonCode::RepeatedVariable,
            ),
            (
                QueryShape::new(vec![
                    Atom::new("R", &["a", "b"]),
                    Atom::new("S", &["b", "c", "d"]),
                ]),
                ReasonCode::ArityMismatch,
            ),
        ];
        for (shape, code) in cases {
            let err = resolve(&shape, &catalog, &cfg).unwrap_err();
            assert_eq!(err.reason().unwrap().code, code, "shape {:?}", shape);
        }
    }

    #[test]
    fn variable_count_is_limited() {
        let catalog = MockCatalog::new(1);
        let shape = QueryShape::new(vec![Atom::new("R", &["a", "b"]), Atom::new("S", &["b", "c"])]);
        let cfg = PlannerConfig {
            max_variables: 2,
            allow_reindex: true,
        };
        let err = resolve(&shape, &catalog, &cfg).unwrap_err();
        assert_eq!(err.reason().unwrap().code, ReasonCode::TooManyVariables);
    }
}
