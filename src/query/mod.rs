//! Logical query model: the restricted, explicitly-typed subquery surface,
//! correlation descriptors, error taxonomy and request-side specifications.
//!
//! Everything in this module is planner-facing data. The decorrelation rewrite
//! lives in [`crate::query::rewrite`]; executable plans live in
//! [`crate::query::plan`].

pub mod plan;
pub mod rewrite;

use crate::batch::{ColType, Field};
use serde::{Deserialize, Serialize};
use serde_json::Value;

/// Column reference, optionally qualified by a relation alias.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct ColumnRef {
    #[serde(default)]
    pub relation: Option<String>,
    pub name: String,
}

impl ColumnRef {
    pub fn bare(name: impl Into<String>) -> Self {
        ColumnRef { relation: None, name: name.into() }
    }
}

impl std::fmt::Display for ColumnRef {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match &self.relation {
            Some(r) => write!(f, "{}.{}", r, self.name),
            None => write!(f, "{}", self.name),
        }
    }
}

/// One conjunct of the subquery correlation predicate:
/// `outer_ref = inner_ref` under standard SQL equality (NULL never matches).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Correlation {
    pub outer: ColumnRef,
    pub inner: ColumnRef,
}

/// Aggregate functions supported inside correlated scalar subqueries.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AggFunc {
    /// `COUNT(*)` when the argument is absent; otherwise `COUNT(column)`,
    /// which counts only non-NULL values.
    Count,
    Sum,
}

/// How a subquery is used by the outer query.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SubqueryKind {
    /// `EXISTS (SELECT ... WHERE correlation)` → Boolean
    Exists,
    /// `NOT EXISTS (...)` → Boolean
    NotExists,
    /// Bare scalar subquery `(SELECT col ...)` → value / NULL / cardinality error
    Scalar,
    /// Scalar aggregate `(SELECT COUNT|SUM(...) ...)` → value
    ScalarAgg,
    /// `x NOT IN (SELECT ...)`. Parsed but never executed: its NULL-aware
    /// three-valued logic is not equivalent to NOT EXISTS.
    NotIn,
}

/// Request-side specification of one correlated subquery.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SubquerySpec {
    /// Name of the result column appended to the outer rows.
    pub alias: String,
    #[serde(rename = "type")]
    pub kind: SubqueryKind,
    /// Name of the inner relation in the (request-overlaid) catalog.
    pub input: String,
    /// Conjuncts of the correlation predicate. At least one is required.
    #[serde(default)]
    pub correlations: Vec<Correlation>,
    /// Aggregate function for `scalar_agg`.
    #[serde(default)]
    pub agg_func: Option<AggFunc>,
    /// Aggregate argument column for `scalar_agg`. Absent means `COUNT(*)`.
    #[serde(default)]
    pub agg_argument: Option<ColumnRef>,
    /// Value column projected by a bare `scalar` subquery.
    #[serde(default)]
    pub value: Option<ColumnRef>,
}

/// Inline relation payload (synthetic fixture): typed schema plus JSON rows.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RelationInput {
    pub name: String,
    pub schema: Vec<Field>,
    #[serde(default)]
    pub rows: Vec<serde_json::Map<String, Value>>,
}

/// Error categories. Both execution strategies (naïve row-wise interpreter and
/// the decorrelated plan) must report errors through *the same* category.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorCategory {
    /// Malformed request (missing fields, unknown relation/column, bad JSON).
    Validation,
    /// `NOT IN` requested: NULL semantics make it non-equivalent to NOT EXISTS.
    UnsupportedNotIn,
    /// Construct outside the supported surface (non-equality predicates,
    /// unknown aggregates, expressions, ...).
    UnsupportedForm,
    /// Correlated / scalar columns of incompatible types.
    TypeMismatch,
    /// Scalar subquery returned more than one row for an outer row.
    CardinalityViolation,
    /// Numeric result does not fit the declared column type.
    NumericOverflow,
    /// Fixture data violates its declared schema.
    DataError,
    /// The two execution strategies disagreed. Must never happen; if it does
    /// the response fails closed.
    EquivalenceMismatch,
    /// Internal bug / invariant violation.
    Internal,
}

impl ErrorCategory {
    pub fn http_status(self) -> u16 {
        match self {
            ErrorCategory::Validation
            | ErrorCategory::UnsupportedNotIn
            | ErrorCategory::UnsupportedForm
            | ErrorCategory::TypeMismatch
            | ErrorCategory::DataError => 400,
            ErrorCategory::CardinalityViolation | ErrorCategory::NumericOverflow => 422,
            ErrorCategory::EquivalenceMismatch => 500,
            ErrorCategory::Internal => 500,
        }
    }
}

/// Structured query error carrying the processing step it occurred at.
#[derive(Debug, Clone, Serialize)]
pub struct QueryError {
    pub category: ErrorCategory,
    pub code: &'static str,
    pub message: String,
    /// Pipeline stage that produced the error, e.g. `validate`, `rewrite`,
    /// `exec:decorrelated`, `exec:naive`, `verify:equivalence`.
    pub at_step: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<Value>,
}

impl QueryError {
    pub fn new(
        category: ErrorCategory,
        code: &'static str,
        message: impl Into<String>,
        at_step: impl Into<String>,
    ) -> Self {
        QueryError {
            category,
            code,
            message: message.into(),
            at_step: at_step.into(),
            detail: None,
        }
    }

    pub fn with_detail(mut self, detail: Value) -> Self {
        self.detail = Some(detail);
        self
    }
}

pub type QueryResult<T> = Result<T, QueryError>;

/// Resolve a column against a specific relation: an unqualified reference may
/// match any column; a qualified one must match the bound alias.
pub fn resolve_in(
    schema: &[Field],
    bound_alias: &str,
    col: &ColumnRef,
) -> QueryResult<usize> {
    if let Some(r) = &col.relation {
        if r != bound_alias {
            return Err(QueryError::new(
                ErrorCategory::Validation,
                "alias_mismatch",
                format!("reference {} is not bound to relation {}", col, bound_alias),
                "validate",
            ));
        }
    }
    let candidates: Vec<usize> = schema
        .iter()
        .enumerate()
        .filter(|(_, f)| f.name == col.name)
        .map(|(i, _)| i)
        .collect();
    match candidates.as_slice() {
        [i] => Ok(*i),
        [] => Err(QueryError::new(
            ErrorCategory::Validation,
            "unknown_column",
            format!("column {} not found in relation {} (columns: {})", col, bound_alias,
                schema_names(schema).join(", ")),
            "validate",
        )),
        _ => Err(QueryError::new(
            ErrorCategory::Validation,
            "ambiguous_column",
            format!("column {} is ambiguous in relation {}", col, bound_alias),
            "validate",
        )),
    }
}

fn schema_names(schema: &[Field]) -> Vec<&str> {
    schema.iter().map(|f| f.name.as_str()).collect()
}

/// SQL three-valued equality of two typed datums: NULL on either side yields
/// "not matching". Used identically by the naïve interpreter and the hash
/// join's match test.
pub fn sql_equal(a: &crate::batch::Datum, b: &crate::batch::Datum) -> bool {
    use crate::batch::Scalar;
    match (&a.value, &b.value) {
        (Some(Scalar::Val(x)), Some(Scalar::Val(y))) => match (x, y) {
            (crate::batch::Value::Int(i), crate::batch::Value::Int(j)) => i == j,
            (crate::batch::Value::Float(f), crate::batch::Value::Float(g)) => f == g,
            (crate::batch::Value::Bool(p), crate::batch::Value::Bool(q)) => p == q,
            _ => false,
        },
        (Some(Scalar::Str(s)), Some(Scalar::Str(t))) => s == t,
        _ => false,
    }
}

/// Validate that two columns may be compared in a correlation predicate.
pub fn check_comparable(a: ColType, b: ColType, ctx: &str) -> QueryResult<()> {
    if a == b {
        return Ok(());
    }
    Err(QueryError::new(
        ErrorCategory::TypeMismatch,
        "incomparable_types",
        format!("{}: cannot compare {:?} with {:?} (mixed-type correlation is not supported)", ctx, a, b),
        "validate",
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::batch::{Datum, Value};

    #[test]
    fn sql_equality_is_false_when_either_side_is_null() {
        assert!(!sql_equal(&Datum::null(), &Datum::null()));
        assert!(!sql_equal(&Datum::null(), &Datum::int(1)));
        assert!(!sql_equal(&Datum::int(1), &Datum::null()));
        assert!(sql_equal(&Datum::int(1), &Datum::int(1)));
        assert!(!sql_equal(&Datum::int(1), &Datum::int(2)));
        assert!(sql_equal(&Datum::from_value(Value::Bool(true)), &Datum::from_value(Value::Bool(true))));
        assert!(sql_equal(&Datum::string("a"), &Datum::string("a")));
        assert!(!sql_equal(&Datum::string("a"), &Datum::string("b")));
    }

    #[test]
    fn sql_equality_does_not_coerce_across_types() {
        assert!(!sql_equal(&Datum::int(1), &Datum::from_value(Value::Float(1.0))));
        assert!(!sql_equal(&Datum::int(1), &Datum::string("1")));
        assert!(!sql_equal(&Datum::from_value(Value::Bool(true)), &Datum::int(1)));
    }

    #[test]
    fn comparable_types_require_identical_coltype() {
        assert!(check_comparable(ColType::Int64, ColType::Int64, "x").is_ok());
        assert!(check_comparable(ColType::Int64, ColType::Utf8, "x").is_err());
    }

    #[test]
    fn resolve_in_handles_missing_and_qualified_columns() {
        use crate::batch::Field;
        let schema = vec![Field::new("a", ColType::Int64), Field::new("b", ColType::Utf8)];
        assert_eq!(resolve_in(&schema, "t", &ColumnRef::bare("b")).unwrap(), 1);
        assert!(resolve_in(&schema, "t", &ColumnRef { relation: Some("t".into()), name: "a".into() }).is_ok());
        assert!(resolve_in(&schema, "t", &ColumnRef { relation: Some("other".into()), name: "a".into() }).is_err());
        assert!(resolve_in(&schema, "t", &ColumnRef::bare("missing")).is_err());
    }
}
