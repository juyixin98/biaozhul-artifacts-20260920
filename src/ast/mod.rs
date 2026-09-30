//! Normalized, restricted logical representation of supported queries.
//!
//! The parser (sqlparser-based) lowers SQL into this IR, the validator checks
//! it against catalogs, and both the interpreter and the decorrelating compiler
//! consume the *same* [`Query`]. This is what makes differential testing
//! meaningful: the two execution strategies never see different inputs.

use crate::batch::ScalarType;

/// Binary comparison operators allowed in WHERE clauses.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CmpOp {
    Eq,
    Neq,
    Lt,
    Lte,
    Gt,
    Gte,
}

impl CmpOp {
    /// Reverse operand order: `lit OP col` becomes `col OP.flip() lit`.
    pub fn flip(self) -> CmpOp {
        match self {
            CmpOp::Eq => CmpOp::Eq,
            CmpOp::Neq => CmpOp::Neq,
            CmpOp::Lt => CmpOp::Gt,
            CmpOp::Lte => CmpOp::Gte,
            CmpOp::Gt => CmpOp::Lt,
            CmpOp::Gte => CmpOp::Lte,
        }
    }

    pub fn eval(&self, a: &crate::batch::Value, b: &crate::batch::Value) -> Option<bool> {
        use crate::batch::Value;
        if matches!(a, Value::Null) || matches!(b, Value::Null) {
            return None; // SQL three-valued logic
        }
        let ord = match (a, b) {
            (Value::Int(x), Value::Int(y)) => x.cmp(y),
            (Value::Float(x), Value::Float(y)) => x.partial_cmp(y)?,
            // Mixed numeric comparisons widen the int side to f64, matching
            // SQL numeric coercion (documented: precision loss above 2^53).
            (Value::Int(x), Value::Float(y)) => (*x as f64).partial_cmp(y)?,
            (Value::Float(x), Value::Int(y)) => x.partial_cmp(&(*y as f64))?,
            (Value::Str(x), Value::Str(y)) => x.cmp(y),
            (Value::Bool(x), Value::Bool(y)) => x.cmp(y),
            _ => return None,
        };
        use std::cmp::Ordering::*;
        Some(match self {
            CmpOp::Eq => ord == Equal,
            CmpOp::Neq => ord != Equal,
            CmpOp::Lt => ord == Less,
            CmpOp::Lte => ord != Greater,
            CmpOp::Gt => ord == Greater,
            CmpOp::Gte => ord != Less,
        })
    }
}

/// A column reference qualified by an alias scope: Outer or the subquery.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Scope {
    Outer,
    Inner,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ColumnRef {
    pub scope: Scope,
    pub table: Option<String>,
    pub name: String,
}

/// A single predicate. The supported predicate shapes are deliberately small:
/// column-to-column comparisons and column-to-literal comparisons, combined
/// with AND. That covers equi-correlation (`o.k = i.k`) plus local filters.
#[derive(Debug, Clone)]
pub enum Predicate {
    /// Column compared to another column.
    ColCmp {
        left: ColumnRef,
        op: CmpOp,
        right: ColumnRef,
    },
    /// Column compared to a literal (typed value; `Null` => IS [NOT] NULL style).
    LitCmp {
        col: ColumnRef,
        op: CmpOp,
        value: crate::batch::Value,
    },
    /// col IS NULL / col IS NOT NULL
    NullTest {
        col: ColumnRef,
        expect_null: bool,
    },
    And(Box<Predicate>, Box<Predicate>),
}

impl Predicate {
    pub fn and(a: Predicate, b: Predicate) -> Predicate {
        Predicate::And(Box::new(a), Box::new(b))
    }
}

/// A scalar aggregate projection: `SELECT COUNT(*)` or `SELECT SUM(col)`.
/// COUNT(*) and COUNT(col) both return 0 on an empty group; SUM returns NULL.
#[derive(Debug, Clone)]
pub enum ScalarProj {
    /// COUNT(*) — 0 on an empty group.
    CountStar,
    /// COUNT(col) — 0 on an empty group; ignores NULL values.
    CountCol { col: String },
    /// SUM(col) — NULL on an empty group.
    SumCol { col: String, float: bool },
}

/// What appears in the outer SELECT list.
#[derive(Debug, Clone)]
pub enum OuterItem {
    /// A plain outer column, optionally renamed (`k AS id`). `name == "*"`
    /// denotes the unqualified wildcard.
    Column { name: String, alias: Option<String> },
    /// A correlated subquery used as a value/boolean expression.
    Subquery(Subquery),
}

/// The kind of correlated subquery supported.
#[derive(Debug, Clone)]
pub enum SubKind {
    /// EXISTS (SELECT 1 FROM inner WHERE correlation + filters)
    Exists,
    /// NOT EXISTS (...)
    NotExists,
    /// (SELECT agg FROM inner WHERE ...) used as a scalar expression.
    ScalarAgg { proj: ScalarProj },
    /// (SELECT col FROM inner WHERE ...): cardinality must be exactly 0 or 1.
    /// Zero rows yields NULL (standard scalar-subquery semantics); more than
    /// one row is a runtime cardinality violation.
    ScalarColumn { col: String },
}

#[derive(Debug, Clone)]
pub struct Subquery {
    pub kind: SubKind,
    pub inner_table: String,
    /// Conjunctive predicates; may mix correlated (Outer vs Inner) and local.
    pub predicates: Vec<Predicate>,
    /// User-facing output column name (`AS alias`), assigned by validator if absent.
    pub output_name: Option<String>,
}

/// A fully parsed, restricted query:
/// `SELECT <items> FROM <outer> [WHERE <filters>]`.
#[derive(Debug, Clone)]
pub struct Query {
    pub outer_table: String,
    pub items: Vec<OuterItem>,
    /// Outer-only filters (no Inner references allowed here).
    pub filters: Vec<Predicate>,
}

/// Output schema of a compiled/executed query.
#[derive(Debug, Clone)]
pub struct OutputColumn {
    pub name: String,
    pub ty: ScalarType,
}
