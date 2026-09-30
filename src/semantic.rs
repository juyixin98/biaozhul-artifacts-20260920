//! Semantic validation and name/type resolution against a [`Catalog`].
//!
//! Output is a [`ValidatedQuery`]: the raw [`ast::Query`] plus resolved output
//! schema, per-subquery correlation-key classification, and resolved SUM
//! types. Both the row-by-row interpreter and the decorrelating compiler
//! consume this single resolved object.

use crate::ast::*;
use crate::batch::ScalarType;
use crate::error::{EngineError, EngineResult};
use crate::state::Catalog;

/// A resolved output column of the query.
#[derive(Debug, Clone)]
pub struct ResolvedOutput {
    pub name: String,
    pub ty: ScalarType,
    /// Index into the final row: outer projected columns then subquery outputs.
    pub source: OutputSource,
}

#[derive(Debug, Clone)]
pub enum OutputSource {
    /// Plain outer column.
    OuterColumn { col: String },
    /// Result of subquery at this index in `ValidatedQuery.subqueries`.
    Subquery { index: usize },
}

/// A predicate after validation: still the AST shape, but every column ref is
/// known-good and typed.
#[derive(Debug, Clone)]
pub struct TypedPredicate {
    pub raw: Predicate,
}

/// How a subquery correlates with the outer row.
#[derive(Debug, Clone)]
pub struct Correlation {
    /// `(outer_col, inner_col)` equi-correlations. This is the join key used
    /// by the decorrelating hash aggregation.
    pub eq_keys: Vec<(String, String)>,
    /// Types shared by both sides of each key (must match).
    pub key_types: Vec<ScalarType>,
}

#[derive(Debug, Clone)]
pub struct ValidatedSubquery {
    pub inner_table: String,
    pub kind: ValidatedSubKind,
    pub predicates: Vec<Predicate>,
    pub correlation: Correlation,
    pub output_name: String,
    pub output_type: ScalarType,
}

#[derive(Debug, Clone)]
pub enum ValidatedSubKind {
    Exists,
    NotExists,
    ScalarAgg {
        proj: ScalarProj,
        /// Type of the aggregated value column, if any.
        col_type: Option<ScalarType>,
    },
    ScalarColumn {
        col: String,
        col_type: ScalarType,
    },
}

#[derive(Debug, Clone)]
pub struct ValidatedQuery {
    pub outer_table: String,
    /// Ordered outer column names selected before subqueries are appended.
    pub outer_projected: Vec<String>,
    pub outputs: Vec<ResolvedOutput>,
    pub subqueries: Vec<ValidatedSubquery>,
    pub filters: Vec<Predicate>,
}

pub fn validate(q: &Query, catalog: &Catalog) -> EngineResult<ValidatedQuery> {
    let outer = catalog.get(&q.outer_table)?;

    // Outer WHERE must reference only the outer scope.
    let mut filters = Vec::new();
    for p in &q.filters {
        check_predicate_scopes(p, AllowScopes::OuterOnly)?;
        type_predicate(p, catalog, &q.outer_table, None)?;
        filters.push(p.clone());
    }

    // Expand projection: wildcard plus explicit items, preserving order.
    let mut outputs = Vec::new();
    let mut outer_projected: Vec<String> = Vec::new();
    let mut subqueries = Vec::new();
    let mut auto_idx = 0usize;

    for item in &q.items {
        match item {
            OuterItem::Column { name, alias } if name == "*" => {
                if alias.is_some() {
                    return Err(EngineError::validation(
                        "wildcard * cannot carry an alias",
                    ));
                }
                for c in &outer.batch.columns {
                    outer_projected.push(c.name.clone());
                    outputs.push(ResolvedOutput {
                        name: c.name.clone(),
                        ty: c.ty,
                        source: OutputSource::OuterColumn {
                            col: c.name.clone(),
                        },
                    });
                }
            }
            OuterItem::Column { name, alias } => {
                let ty = outer.batch.column(name)?.ty;
                outer_projected.push(name.clone());
                outputs.push(ResolvedOutput {
                    name: alias.clone().unwrap_or_else(|| name.clone()),
                    ty,
                    source: OutputSource::OuterColumn { col: name.clone() },
                });
            }
            OuterItem::Subquery(sq) => {
                let index = subqueries.len();
                let validated = validate_subquery(sq, catalog, &q.outer_table, auto_idx)?;
                auto_idx += 1;
                outputs.push(ResolvedOutput {
                    name: validated.output_name.clone(),
                    ty: validated.output_type,
                    source: OutputSource::Subquery { index },
                });
                subqueries.push(validated);
            }
        }
    }

    if outputs.is_empty() {
        return Err(EngineError::validation("query projects no columns"));
    }

    Ok(ValidatedQuery {
        outer_table: q.outer_table.clone(),
        outer_projected,
        outputs,
        subqueries,
        filters,
    })
}

#[derive(Clone, Copy)]
enum AllowScopes {
    OuterOnly,
    Any,
}

fn check_predicate_scopes(p: &Predicate, allow: AllowScopes) -> EngineResult<()> {
    match p {
        Predicate::And(a, b) => {
            check_predicate_scopes(a, allow)?;
            check_predicate_scopes(b, allow)
        }
        Predicate::NullTest { col, .. } | Predicate::LitCmp { col, .. } => {
            if matches!(allow, AllowScopes::OuterOnly) && col.scope == Scope::Inner {
                Err(EngineError::validation(
                    "outer WHERE cannot reference inner-table columns",
                ))
            } else {
                Ok(())
            }
        }
        Predicate::ColCmp { left, right, .. } => {
            for r in [&left.scope, &right.scope] {
                if matches!(allow, AllowScopes::OuterOnly) && *r == Scope::Inner {
                    return Err(EngineError::validation(
                        "outer WHERE cannot reference inner-table columns",
                    ));
                }
            }
            Ok(())
        }
    }
}

fn type_predicate(
    p: &Predicate,
    catalog: &Catalog,
    outer_table: &str,
    inner_table: Option<&str>,
) -> EngineResult<()> {
    match p {
        Predicate::And(a, b) => {
            type_predicate(a, catalog, outer_table, inner_table)?;
            type_predicate(b, catalog, outer_table, inner_table)
        }
        Predicate::NullTest { col, .. } => {
            resolve_type(col, catalog, outer_table, inner_table)?;
            Ok(())
        }
        Predicate::LitCmp { col, value, .. } => {
            let ty = resolve_type(col, catalog, outer_table, inner_table)?;
            if !value.is_null() && !value_matches(ty, value) {
                return Err(EngineError::validation(format!(
                    "column '{}' is {} but compared against {}",
                    col.name,
                    ty.name(),
                    value_name(value)
                )));
            }
            Ok(())
        }
        Predicate::ColCmp { left, right, .. } => {
            let lt = resolve_type(left, catalog, outer_table, inner_table)?;
            let rt = resolve_type(right, catalog, outer_table, inner_table)?;
            if lt != rt {
                return Err(EngineError::validation(format!(
                    "cannot compare '{}' ({}) with '{}' ({})",
                    left.name,
                    lt.name(),
                    right.name,
                    rt.name()
                )));
            }
            Ok(())
        }
    }
}

fn resolve_type(
    r: &ColumnRef,
    catalog: &Catalog,
    outer_table: &str,
    inner_table: Option<&str>,
) -> EngineResult<ScalarType> {
    let table_name = match r.scope {
        Scope::Outer => {
            if let Some(t) = &r.table {
                if t != outer_table {
                    return Err(EngineError::validation(format!(
                        "qualifier '{t}' is not the outer table '{outer_table}'"
                    )));
                }
            }
            outer_table.to_string()
        }
        Scope::Inner => {
            let t = inner_table.ok_or_else(|| {
                EngineError::internal("inner-scoped ref without an inner table")
            })?;
            if let Some(q) = &r.table {
                if q != t {
                    return Err(EngineError::validation(format!(
                        "qualifier '{q}' is not the inner table '{t}'"
                    )));
                }
            }
            t.to_string()
        }
    };
    Ok(catalog.get(&table_name)?.batch.column(&r.name)?.ty)
}

fn value_matches(ty: ScalarType, v: &crate::batch::Value) -> bool {
    use crate::batch::Value;
    match (ty, v) {
        (ScalarType::Bool, Value::Bool(_)) => true,
        (ScalarType::Str, Value::Str(_)) => true,
        // Numeric literals coerce across int/float columns; evaluation widens
        // to f64 (see CmpOp::eval).
        (ScalarType::Int | ScalarType::Float, Value::Int(_) | Value::Float(_)) => true,
        _ => false,
    }
}

fn value_name(v: &crate::batch::Value) -> &'static str {
    match v {
        crate::batch::Value::Null => "null",
        crate::batch::Value::Bool(_) => "bool",
        crate::batch::Value::Int(_) => "int",
        crate::batch::Value::Float(_) => "float",
        crate::batch::Value::Str(_) => "str",
    }
}

fn validate_subquery(
    sq: &Subquery,
    catalog: &Catalog,
    outer_table: &str,
    auto_idx: usize,
) -> EngineResult<ValidatedSubquery> {
    let inner = catalog.get(&sq.inner_table)?;

    for p in &sq.predicates {
        check_predicate_scopes(p, AllowScopes::Any)?;
        type_predicate(p, catalog, outer_table, Some(&sq.inner_table))?;
        // Bare inner columns resolve fine; an Outer-scope bare column is a
        // parser artifact — require qualification for outer refs inside sub.
        for_each_ref(p, &mut |r: &ColumnRef| {
            if r.scope == Scope::Outer && r.table.is_none() {
                return Err(EngineError::validation(format!(
                    "outer column '{}' inside subquery must be qualified with the outer table name",
                    r.name
                )));
            }
            Ok(())
        })?;
    }

    // Classify equi-correlation keys: Outer.k OP Inner.k where OP is Eq.
    let mut eq_keys: Vec<(String, String)> = Vec::new();
    let mut key_types: Vec<ScalarType> = Vec::new();
    for p in &sq.predicates {
        if let Predicate::ColCmp { left, op, right } = p {
            if !matches!(op, CmpOp::Eq) {
                continue;
            }
            match (&left.scope, &right.scope) {
                (Scope::Outer, Scope::Inner) => {
                    let t = resolve_type(left, catalog, outer_table, Some(&sq.inner_table))?;
                    eq_keys.push((left.name.clone(), right.name.clone()));
                    key_types.push(t);
                }
                (Scope::Inner, Scope::Outer) => {
                    let t = resolve_type(right, catalog, outer_table, Some(&sq.inner_table))?;
                    eq_keys.push((right.name.clone(), left.name.clone()));
                    key_types.push(t);
                }
                _ => {}
            }
        }
    }
    // Deterministic order for proof/explain (sort by outer name).
    let mut paired: Vec<_> = eq_keys.into_iter().zip(key_types).collect();
    paired.sort_by(|a, b| a.0 .0.cmp(&b.0 .0));
    let (eq_keys, key_types): (Vec<_>, Vec<_>) = paired.into_iter().unzip();

    // Resolve projection kind, types, output name.
    let (kind, output_type) = match &sq.kind {
        SubKind::Exists => (ValidatedSubKind::Exists, ScalarType::Bool),
        SubKind::NotExists => (ValidatedSubKind::NotExists, ScalarType::Bool),
        SubKind::ScalarAgg { proj } => {
            let (resolved, col_ty) = resolve_agg(proj, &sq.inner_table, catalog)?;
            let out_ty = match &resolved {
                ScalarProj::CountStar | ScalarProj::CountCol { .. } => ScalarType::Int,
                ScalarProj::SumCol { float, .. } => {
                    if *float {
                        ScalarType::Float
                    } else {
                        ScalarType::Int
                    }
                }
            };
            (
                ValidatedSubKind::ScalarAgg {
                    proj: resolved,
                    col_type: Some(col_ty),
                },
                out_ty,
            )
        }
        SubKind::ScalarColumn { col } => {
            let t = inner.batch.column(col)?.ty;
            (
                ValidatedSubKind::ScalarColumn {
                    col: col.clone(),
                    col_type: t,
                },
                t,
            )
        }
    };

    let output_name = sq.output_name.clone().unwrap_or_else(|| default_name(&sq.kind, auto_idx));

    Ok(ValidatedSubquery {
        inner_table: sq.inner_table.clone(),
        kind,
        predicates: sq.predicates.clone(),
        correlation: Correlation { eq_keys, key_types },
        output_name,
        output_type,
    })
}

fn resolve_agg(
    proj: &ScalarProj,
    inner_table: &str,
    catalog: &Catalog,
) -> EngineResult<(ScalarProj, ScalarType)> {
    match proj {
        ScalarProj::CountStar => Ok((proj.clone(), ScalarType::Int)),
        ScalarProj::CountCol { col } => {
            if col == "__count_null__" {
                return Ok((proj.clone(), ScalarType::Int));
            }
            let ty = catalog.get(inner_table)?.batch.column(col)?.ty;
            Ok((proj.clone(), ty))
        }
        ScalarProj::SumCol { col, float: _ } => {
            let ty = catalog.get(inner_table)?.batch.column(col)?.ty;
            match ty {
                ScalarType::Int => Ok((ScalarProj::SumCol { col: col.clone(), float: false }, ty)),
                ScalarType::Float => Ok((ScalarProj::SumCol { col: col.clone(), float: true }, ty)),
                other => Err(EngineError::validation(format!(
                    "SUM over {} column '{col}' is not supported",
                    other.name()
                ))),
            }
        }
    }
}

fn default_name(kind: &SubKind, idx: usize) -> String {
    match kind {
        SubKind::Exists => format!("exists_{idx}"),
        SubKind::NotExists => format!("not_exists_{idx}"),
        SubKind::ScalarAgg { proj } => match proj {
            ScalarProj::CountStar | ScalarProj::CountCol { .. } => format!("count_{idx}"),
            ScalarProj::SumCol { .. } => format!("sum_{idx}"),
        },
        SubKind::ScalarColumn { .. } => format!("scalar_{idx}"),
    }
}

fn for_each_ref<F: FnMut(&ColumnRef) -> EngineResult<()>>(
    p: &Predicate,
    f: &mut F,
) -> EngineResult<()> {
    match p {
        Predicate::And(a, b) => {
            for_each_ref(a, f)?;
            for_each_ref(b, f)
        }
        Predicate::NullTest { col, .. } | Predicate::LitCmp { col, .. } => f(col),
        Predicate::ColCmp { left, right, .. } => {
            f(left)?;
            f(right)
        }
    }
}
