//! Query model, global variable ordering and validation.
//!
//! This module is the gate-keeper for **restricted multi-table natural joins**.
//! It resolves relations against the catalog, type-checks shared variables,
//! decides which variables are join variables (occur in at least two
//! relations), and enforces the restrictions before any trie is built:
//!
//! * at least two relations and at least one equality (join variable);
//! * the join hypergraph is connected (no implicit cartesian product);
//! * shared variables have a consistent physical type.
//!
//! It also fixes a single *global variable order*. Every relation trie is built
//! with its variables projected into that global order, which is the
//! compatibility precondition Leapfrog Triejoin requires.

use std::collections::{BTreeMap, BTreeSet};

use crate::batch::{ColumnType, RecordBatch};
use crate::error::{ErrorCode, JoinError, Result};
use crate::types::NullPolicy;

/// One relation occurrence in a query: a catalog relation name plus the query
/// variable bound to each of its columns, positionally.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RelationOccurrence {
    pub relation: String,
    /// One variable per catalog column, in column order.
    pub variables: Vec<String>,
}

/// A user-supplied join request (before resolution/validation).
#[derive(Debug, Clone)]
pub struct JoinRequest {
    pub relations: Vec<RelationOccurrence>,
    pub null_policy: NullPolicy,
    /// Variables to return; `None` means all variables in global order.
    pub projection: Option<Vec<String>>,
    pub limit: Option<u64>,
}

/// A resolved relation with its catalog batch and derived positions.
#[derive(Debug, Clone)]
pub struct PlannedRelation {
    pub name: String,
    /// This relation's variables in **global** order (its trie level order).
    pub trie_vars: Vec<String>,
    /// For each trie level, the source catalog column index.
    pub source_columns: Vec<usize>,
}

/// A validated, executable plan.
#[derive(Debug, Clone)]
pub struct JoinPlan {
    /// Global variable order: join variables first, then free variables.
    pub ordered_vars: Vec<String>,
    /// Prefix of `ordered_vars` that are join variables.
    pub join_var_count: usize,
    pub relations: Vec<PlannedRelation>,
    /// Physical type of each global variable.
    pub types: Vec<ColumnType>,
    /// For each global level, the relation indices containing that variable.
    pub level_relations: Vec<Vec<usize>>,
    /// Projection as indices into `ordered_vars`.
    pub projection: Vec<usize>,
    pub null_policy: NullPolicy,
    pub limit: Option<u64>,
}

impl JoinPlan {
    pub fn output_vars(&self) -> Vec<&str> {
        self.projection
            .iter()
            .map(|&i| self.ordered_vars[i].as_str())
            .collect()
    }
}

/// Per-variable accumulated catalog info used while planning.
#[derive(Debug, Default)]
struct VarInfo {
    typ: Option<ColumnType>,
    relations: BTreeSet<usize>,
}

/// Resolve and validate a request against a catalog of named batches.
pub fn plan(request: &JoinRequest, catalog: &BTreeMap<String, RecordBatch>) -> Result<JoinPlan> {
    if request.relations.len() < 2 {
        return Err(JoinError::new(
            ErrorCode::NotANaturalJoin,
            "a natural join requires at least two relations",
        ));
    }

    // Detect repeated relation occurrences by name? Occurrences may legitimately
    // repeat (self-join); we index them positionally and allow it.
    let n_rel = request.relations.len();

    // 1. resolve schemas + per-variable info.
    let mut var_info: BTreeMap<String, VarInfo> = BTreeMap::new();

    for (ri, occ) in request.relations.iter().enumerate() {
        let batch = catalog
            .get(&occ.relation)
            .ok_or_else(|| JoinError::unknown_relation(occ.relation.clone()))?;
        let mut seen: BTreeSet<&str> = BTreeSet::new();
        for v in &occ.variables {
            if !seen.insert(v.as_str()) {
                return Err(JoinError::new(
                    ErrorCode::DuplicateVariable,
                    format!("relation `{}` binds variable `{v}` twice", occ.relation),
                ));
            }
        }
        if occ.variables.len() != batch.n_cols() {
            return Err(JoinError::new(
                ErrorCode::UnknownVariable,
                format!(
                    "relation `{}` binds {} variables but has {} columns",
                    occ.relation,
                    occ.variables.len(),
                    batch.n_cols()
                ),
            ));
        }
        let types: Vec<ColumnType> = batch.columns.iter().map(|c| c.column.col_type()).collect();
        for (vi, v) in occ.variables.iter().enumerate() {
            let info = var_info.entry(v.clone()).or_default();
            match info.typ {
                None => info.typ = Some(types[vi]),
                Some(t) if t == types[vi] => {}
                Some(t) => {
                    return Err(JoinError::new(
                        ErrorCode::SchemaMismatch,
                        format!(
                            "variable `{v}` has conflicting types {} and {} in `{}`",
                            t.as_str(),
                            types[vi].as_str(),
                            occ.relation
                        ),
                    ))
                }
            }
            info.relations.insert(ri);
        }
    }

    // 2. join vs free variables.
    let mut join_vars: Vec<&String> = var_info
        .iter()
        .filter(|(_, i)| i.relations.len() >= 2)
        .map(|(v, _)| v)
        .collect();
    if join_vars.is_empty() {
        return Err(JoinError::new(
            ErrorCode::NotANaturalJoin,
            "no variable is shared by two relations; this would be a cross product",
        ));
    }

    // 3. connectivity on the join hypergraph.
    let mut dsu = Dsu::new(n_rel);
    for v in var_info.values() {
        if v.relations.len() >= 2 {
            let mut it = v.relations.iter();
            let first = *it.next().unwrap();
            for &r in it {
                dsu.union(first, r);
            }
        }
    }
    let root = dsu.find(0);
    for r in 1..n_rel {
        if dsu.find(r) != root {
            return Err(JoinError::new(
                ErrorCode::DisconnectedQuery,
                "join hypergraph is disconnected; relation group has no shared key",
            ));
        }
    }

    // 4. global variable order.
    // Join variables first, ordered by increasing estimated domain size (a
    // deterministic Leapfrog-friendly heuristic), ties by name.
    join_vars.sort_by(|a, b| {
        est_domain_size(request, catalog, a)
            .cmp(&est_domain_size(request, catalog, b))
            .then_with(|| a.cmp(b))
    });
    let mut ordered_vars: Vec<String> = join_vars.iter().map(|v| (*v).clone()).collect();
    let join_var_count = ordered_vars.len();

    // Free variables afterwards, grouped deterministically by occurrence.
    for (ri, occ) in request.relations.iter().enumerate() {
        for v in &occ.variables {
            if var_info[v].relations.len() == 1 && !ordered_vars.contains(v) {
                ordered_vars.push(v.clone());
                // record owning relation implicitly via level_relations later
                let _ = ri;
            }
        }
    }

    let position: BTreeMap<&str, usize> = ordered_vars
        .iter()
        .map(|v| v.as_str())
        .enumerate()
        .map(|(i, v)| (v, i))
        .collect();

    // 5. per-relation trie layout in global order.
    let mut relations = Vec::with_capacity(n_rel);
    for occ in request.relations.iter() {
        // (global position, source column) for each of this relation's vars.
        let mut laid: Vec<(usize, usize)> = occ
            .variables
            .iter()
            .enumerate()
            .map(|(col, v)| (position[v.as_str()], col))
            .collect();
        laid.sort_by_key(|(gp, _)| *gp);
        let trie_vars = laid
            .iter()
            .map(|(gp, _)| ordered_vars[*gp].clone())
            .collect();
        let source_columns = laid.iter().map(|(_, col)| *col).collect();
        relations.push(PlannedRelation {
            name: occ.relation.clone(),
            trie_vars,
            source_columns,
        });
    }

    // 6. types per global variable.
    let types: Vec<ColumnType> = ordered_vars
        .iter()
        .map(|v| var_info[v].typ.expect("type resolved"))
        .collect();

    // 7. level -> relation indices.
    let mut level_relations = vec![Vec::new(); ordered_vars.len()];
    for (ri, occ) in request.relations.iter().enumerate() {
        for v in &occ.variables {
            level_relations[position[v.as_str()]].push(ri);
        }
    }

    // 8. projection.
    let projection = match &request.projection {
        None => (0..ordered_vars.len()).collect(),
        Some(vars) => {
            let mut out = Vec::with_capacity(vars.len());
            for v in vars {
                let p = position.get(v.as_str()).ok_or_else(|| {
                    JoinError::new(
                        ErrorCode::UnknownVariable,
                        format!("projection variable `{v}` is not bound by the query"),
                    )
                })?;
                out.push(*p);
            }
            out
        }
    };

    Ok(JoinPlan {
        ordered_vars,
        join_var_count,
        relations,
        types,
        level_relations,
        projection,
        null_policy: request.null_policy,
        limit: request.limit,
    })
}

/// Distinct-value estimate for a variable, scanning the columns where it occurs.
fn est_domain_size(
    request: &JoinRequest,
    catalog: &BTreeMap<String, RecordBatch>,
    var: &str,
) -> usize {
    use std::collections::HashSet;
    let mut distinct: HashSet<crate::types::Datum> = HashSet::new();
    for occ in &request.relations {
        if let Some(col) = occ.variables.iter().position(|v| v == var) {
            let batch = &catalog[&occ.relation];
            for row in 0..batch.n_rows() {
                distinct.insert(batch.columns[col].column.datum_at(row));
            }
        }
    }
    distinct.len()
}

/// Minimal disjoint-set for the connectivity check.
struct Dsu {
    parent: Vec<usize>,
}

impl Dsu {
    fn new(n: usize) -> Self {
        Dsu {
            parent: (0..n).collect(),
        }
    }

    fn find(&mut self, x: usize) -> usize {
        let mut root = x;
        while self.parent[root] != root {
            root = self.parent[root];
        }
        let mut cur = x;
        while self.parent[cur] != root {
            let next = self.parent[cur];
            self.parent[cur] = root;
            cur = next;
        }
        root
    }

    fn union(&mut self, a: usize, b: usize) {
        let ra = self.find(a);
        let rb = self.find(b);
        if ra != rb {
            self.parent[rb] = ra;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::batch::{Column, NamedColumn};

    fn catalog3() -> BTreeMap<String, RecordBatch> {
        let mut m = BTreeMap::new();
        m.insert(
            "r".into(),
            RecordBatch::new(vec![NamedColumn {
                name: "a".into(),
                column: Column::Int(vec![Some(1), Some(2)]),
            }])
            .unwrap(),
        );
        m.insert(
            "s".into(),
            RecordBatch::new(vec![NamedColumn {
                name: "a".into(),
                column: Column::Int(vec![Some(1)]),
            }])
            .unwrap(),
        );
        m
    }

    fn req(rels: Vec<(&str, Vec<&str>)>) -> JoinRequest {
        JoinRequest {
            relations: rels
                .into_iter()
                .map(|(r, vs)| RelationOccurrence {
                    relation: r.into(),
                    variables: vs.into_iter().map(String::from).collect(),
                })
                .collect(),
            null_policy: NullPolicy::SqlMatchNullsReject,
            projection: None,
            limit: None,
        }
    }

    #[test]
    fn plans_a_simple_two_way_join() {
        let cat = catalog3();
        let plan = plan(&req(vec![("r", vec!["a"]), ("s", vec!["a"])]), &cat).unwrap();
        assert_eq!(plan.ordered_vars, vec!["a".to_string()]);
        assert_eq!(plan.join_var_count, 1);
        assert_eq!(plan.level_relations[0], vec![0, 1]);
    }

    #[test]
    fn rejects_cross_product_and_disconnected_query() {
        let cat = catalog3();
        let err = plan(
            &req(vec![("r", vec!["a"]), ("s", vec!["b"])]),
            &cat,
        )
        .unwrap_err();
        assert_eq!(err.code, ErrorCode::NotANaturalJoin);

        // r(a,b), s(b,c), t(d) : t shares no variable -> disconnected.
        let mut cat3 = BTreeMap::new();
        cat3.insert(
            "r".into(),
            RecordBatch::new(vec![
                NamedColumn {
                    name: "a".into(),
                    column: Column::Int(vec![Some(1)]),
                },
                NamedColumn {
                    name: "b".into(),
                    column: Column::Int(vec![Some(2)]),
                },
            ])
            .unwrap(),
        );
        cat3.insert(
            "s".into(),
            RecordBatch::new(vec![
                NamedColumn {
                    name: "b".into(),
                    column: Column::Int(vec![Some(2)]),
                },
                NamedColumn {
                    name: "c".into(),
                    column: Column::Int(vec![Some(3)]),
                },
            ])
            .unwrap(),
        );
        cat3.insert(
            "t".into(),
            RecordBatch::new(vec![NamedColumn {
                name: "d".into(),
                column: Column::Int(vec![Some(9)]),
            }])
            .unwrap(),
        );
        let err = plan(
            &req(vec![
                ("r", vec!["a", "b"]),
                ("s", vec!["b", "c"]),
                ("t", vec!["d"]),
            ]),
            &cat3,
        )
        .unwrap_err();
        assert_eq!(err.code, ErrorCode::DisconnectedQuery);
    }

    #[test]
    fn rejects_unknown_relation_duplicate_var_and_bad_arity() {
        let cat = catalog3();
        assert_eq!(
            plan(&req(vec![("nope", vec!["a"]), ("s", vec!["a"])]), &cat)
                .unwrap_err()
                .code,
            ErrorCode::UnknownRelation
        );
        assert_eq!(
            plan(&req(vec![("r", vec!["a", "a"]), ("s", vec!["a"])]), &cat)
                .unwrap_err()
                .code,
            ErrorCode::DuplicateVariable
        );
        assert_eq!(
            plan(&req(vec![("r", vec!["a", "x"]), ("s", vec!["a"])]), &cat)
                .unwrap_err()
                .code,
            ErrorCode::UnknownVariable
        );
    }
}
