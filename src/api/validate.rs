//! Validation entry point.
//!
//! Turns an untrusted [`QueryRequest`](crate::api::types::QueryRequest) into a
//! fully resolved [`ValidatedQuery`]: relations are resolved against the
//! catalog with per-request overlays, every column name is resolved to a
//! positional index, correlation types are checked, and unsupported forms
//! (notably `NOT IN`) are rejected with a categorized error before anything
//! executes.

use crate::api::types::{QueryRequest, RelationSpec};
use crate::batch::ColType;
use crate::catalog::{resolve_relation, rows_to_batch, Catalog};
use crate::query::plan::{AggSpec, KeyPair, SubqueryPlan, ValidatedQuery};
use crate::query::{
    check_comparable, resolve_in, AggFunc, ErrorCategory, QueryError, QueryResult, SubqueryKind,
    SubquerySpec,
};
use std::collections::{HashMap, HashSet};

/// Validate and prepare the request. All returned plans use positional indices
/// only.
pub fn validate(
    request: &QueryRequest,
    request_id: &str,
    catalog: &Catalog,
) -> QueryResult<ValidatedQuery> {
    // ── 1. Per-request inline overlays (typed, validated, never persisted) ──
    let mut overlays: HashMap<String, crate::batch::Batch> = HashMap::new();
    for rel in &request.relations {
        if overlays.contains_key(&rel.name) {
            return Err(QueryError::new(
                ErrorCategory::Validation,
                "duplicate_inline_relation",
                format!("inline relation `{}` declared twice", rel.name),
                "validate",
            ));
        }
        let batch = rows_to_batch(&rel.schema, &rel.rows).map_err(|msg| {
            QueryError::new(
                ErrorCategory::DataError,
                "bad_inline_relation",
                format!("inline relation `{}`: {}", rel.name, msg),
                "validate",
            )
        })?;
        overlays.insert(rel.name.clone(), batch);
    }

    // ── 2. Resolve the outer relation ──────────────────────────────────────
    let outer_name = request.outer.display_name().to_string();
    if let RelationSpec::Inline(inline) = &request.outer {
        if inline.name != outer_name {
            return Err(QueryError::new(
                ErrorCategory::Validation,
                "outer_name_mismatch",
                "inline outer relation name inconsistent",
                "validate",
            ));
        }
        let batch = rows_to_batch(&inline.schema, &inline.rows).map_err(|msg| {
            QueryError::new(
                ErrorCategory::DataError,
                "bad_inline_relation",
                format!("outer relation `{}`: {}", inline.name, msg),
                "validate",
            )
        })?;
        overlays.insert(inline.name.clone(), batch);
    }
    // Clone the outer batch now (it is the driving materialized relation).
    let outer = resolve_relation(catalog, &overlays, &outer_name)?.clone();

    // ── 3. Validate each subquery ──────────────────────────────────────────
    if request.subqueries.is_empty() {
        return Err(QueryError::new(
            ErrorCategory::Validation,
            "no_subqueries",
            "at least one correlated subquery is required",
            "validate",
        ));
    }

    let mut aliases = HashSet::new();
    let outer_column_names: HashSet<&str> = outer.fields().iter().map(|f| f.name.as_str()).collect();
    let mut subqueries = Vec::with_capacity(request.subqueries.len());

    for spec in &request.subqueries {
        let plan = validate_subquery(
            spec,
            &outer_name,
            &outer,
            catalog,
            &overlays,
            &mut aliases,
            &outer_column_names,
        )?;
        subqueries.push(plan);
    }

    // ── 4. Inner inputs referenced by the validated plans ──────────────────
    let mut inputs: HashMap<String, crate::batch::Batch> = HashMap::new();
    for sq in &subqueries {
        inputs.entry(sq.input_name.clone()).or_insert_with(|| {
            resolve_relation(catalog, &overlays, &sq.input_name)
                .expect("validated at subquery stage")
                .clone()
        });
    }

    Ok(ValidatedQuery {
        request_id: request_id.to_string(),
        outer_name,
        outer,
        subqueries,
        inputs,
    })
}

#[allow(clippy::too_many_arguments)]
fn validate_subquery(
    spec: &SubquerySpec,
    outer_name: &str,
    outer: &crate::batch::Batch,
    catalog: &Catalog,
    overlays: &HashMap<String, crate::batch::Batch>,
    aliases: &mut HashSet<String>,
    outer_column_names: &HashSet<&str>,
) -> QueryResult<SubqueryPlan> {
    // ── Hard gate: NOT IN is never supported ───────────────────────────────
    if matches!(spec.kind, SubqueryKind::NotIn) {
        return Err(QueryError::new(
            ErrorCategory::UnsupportedNotIn,
            "not_in_unsupported",
            "NOT IN (subquery) is rejected: if the subquery contains any NULL, \
             `x NOT IN (SELECT ...)` is UNKNOWN for every outer row, while NOT EXISTS \
             uses plain correlation equality; the NULL semantics differ and no safe \
             rewrite exists on the supported surface.",
            "validate",
        ));
    }

    // ── Alias checks ───────────────────────────────────────────────────────
    if spec.alias.is_empty() {
        return Err(QueryError::new(
            ErrorCategory::Validation,
            "empty_alias",
            "subquery alias must be a non-empty column name",
            "validate",
        ));
    }
    if !aliases.insert(spec.alias.clone()) {
        return Err(QueryError::new(
            ErrorCategory::Validation,
            "duplicate_alias",
            format!("subquery alias `{}` used more than once", spec.alias),
            "validate",
        ));
    }
    if outer_column_names.contains(spec.alias.as_str()) {
        return Err(QueryError::new(
            ErrorCategory::Validation,
            "alias_shadows_outer_column",
            format!("subquery alias `{}` collides with an outer column", spec.alias),
            "validate",
        ));
    }

    // ── Inner relation resolution ──────────────────────────────────────────
    let inner = resolve_relation(catalog, overlays, &spec.input)?;
    let inner_schema = inner.fields().to_vec();

    // ── Correlation resolution and type checks ─────────────────────────────
    if spec.correlations.is_empty() {
        return Err(QueryError::new(
            ErrorCategory::UnsupportedForm,
            "correlation_required",
            format!(
                "subquery `{}`: at least one `outer_col = inner_col` correlation is required; \
                 uncorrelated subqueries are outside the supported surface",
                spec.alias
            ),
            "validate",
        ));
    }

    let mut keys = Vec::with_capacity(spec.correlations.len());
    let mut seen_pairs = HashSet::new();
    for corr in &spec.correlations {
        let outer_index = resolve_in(outer.fields(), outer_name, &corr.outer).map_err(|mut e| {
            e.message = format!("subquery `{}`: {}", spec.alias, e.message);
            e
        })?;
        let inner_index = resolve_in(&inner_schema, &spec.input, &corr.inner).map_err(|mut e| {
            e.message = format!("subquery `{}`: {}", spec.alias, e.message);
            e
        })?;
        let outer_type = outer.fields()[outer_index].col_type;
        let inner_type = inner_schema[inner_index].col_type;
        check_comparable(
            outer_type,
            inner_type,
            &format!("subquery `{}` correlation {}", spec.alias, corr.outer),
        )?;
        if !seen_pairs.insert((outer_index, inner_index)) {
            return Err(QueryError::new(
                ErrorCategory::Validation,
                "duplicate_correlation",
                format!("subquery `{}`: repeated correlation pair", spec.alias),
                "validate",
            ));
        }
        keys.push(KeyPair { outer_index, inner_index, col_type: outer_type });
    }

    // ── Kind-specific shape ────────────────────────────────────────────────
    let (agg, value_inner_index, result_col_type) = match spec.kind {
        SubqueryKind::Exists | SubqueryKind::NotExists => {
            if spec.agg_func.is_some() || spec.value.is_some() || spec.agg_argument.is_some() {
                return Err(QueryError::new(
                    ErrorCategory::Validation,
                    "bad_exists_shape",
                    format!("subquery `{}`: EXISTS/NOT EXISTS takes only correlations", spec.alias),
                    "validate",
                ));
            }
            (AggSpec::None, None, ColType::Boolean)
        }
        SubqueryKind::ScalarAgg => {
            let func = spec.agg_func.ok_or_else(|| {
                QueryError::new(
                    ErrorCategory::Validation,
                    "agg_func_required",
                    format!("subquery `{}`: scalar_agg requires `agg_func`", spec.alias),
                    "validate",
                )
            })?;
            let agg = match (func, &spec.agg_argument) {
                (AggFunc::Count, None) => AggSpec::CountStar,
                (AggFunc::Count, Some(col)) => AggSpec::CountColumn(resolve_agg_arg(col, &spec.input, &inner_schema, spec)?),
                (AggFunc::Sum, None) => {
                    return Err(QueryError::new(
                        ErrorCategory::Validation,
                        "sum_requires_argument",
                        format!("subquery `{}`: SUM requires a numeric argument column", spec.alias),
                        "validate",
                    ));
                }
                (AggFunc::Sum, Some(col)) => {
                    let idx = resolve_agg_arg(col, &spec.input, &inner_schema, spec)?;
                    let ty = inner_schema[idx].col_type;
                    if !matches!(ty, ColType::Int64 | ColType::Float64) {
                        return Err(QueryError::new(
                            ErrorCategory::TypeMismatch,
                            "sum_non_numeric",
                            format!(
                                "subquery `{}`: SUM argument `{}` is {:?}, only Int64/Float64 are supported",
                                spec.alias, col.name, ty
                            ),
                            "validate",
                        ));
                    }
                    AggSpec::Sum(idx)
                }
            };
            let result_type = match &agg {
                AggSpec::Sum(_) => match &spec.agg_argument {
                    Some(col) => inner_schema[resolve_agg_arg(col, &spec.input, &inner_schema, spec)?].col_type,
                    None => unreachable!(),
                },
                _ => ColType::Int64,
            };
            if spec.value.is_some() {
                return Err(QueryError::new(
                    ErrorCategory::Validation,
                    "bad_agg_shape",
                    format!("subquery `{}`: `value` is only valid for bare scalar subqueries", spec.alias),
                    "validate",
                ));
            }
            (agg, None, result_type)
        }
        SubqueryKind::Scalar => {
            if spec.agg_func.is_some() || spec.agg_argument.is_some() {
                return Err(QueryError::new(
                    ErrorCategory::Validation,
                    "bad_scalar_shape",
                    format!("subquery `{}`: bare scalar subquery uses `value`, not aggregates", spec.alias),
                    "validate",
                ));
            }
            let value_col = spec.value.as_ref().ok_or_else(|| {
                QueryError::new(
                    ErrorCategory::Validation,
                    "value_required",
                    format!("subquery `{}`: scalar subquery requires `value`", spec.alias),
                    "validate",
                )
            })?;
            let idx = resolve_agg_arg(value_col, &spec.input, &inner_schema, spec)?;
            (AggSpec::None, Some(idx), inner_schema[idx].col_type)
        }
        SubqueryKind::NotIn => unreachable!("rejected above"),
    };

    Ok(SubqueryPlan {
        alias: spec.alias.clone(),
        kind: spec.kind,
        input_name: spec.input.clone(),
        keys,
        agg,
        value_inner_index,
        result_field: crate::batch::Field::new(spec.alias.clone(), result_col_type),
    })
}

fn resolve_agg_arg(
    col: &crate::query::ColumnRef,
    input_name: &str,
    schema: &[crate::batch::Field],
    spec: &SubquerySpec,
) -> QueryResult<usize> {
    resolve_in(schema, input_name, col).map_err(|mut e| {
        e.message = format!("subquery `{}`: {}", spec.alias, e.message);
        e
    })
}
