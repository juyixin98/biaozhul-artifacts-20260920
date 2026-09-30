//! Predicate AST: the only way to ask the engine a question.
//!
//! JSON surface (also used by the HTTP API and the config-driven demo):
//!
//! ```json
//! {"cmp": {"column": "grp", "op": "=", "value": 3}}
//! {"is_null": {"column": "tag"}}
//! {"and": [ <pred>, <pred>, ... ]}
//! {"or":  [ <pred>, <pred>, ... ]}
//! {"not": <pred>}
//! ```
//!
//! Parsing is deliberately strict and produces *typed* errors:
//! * an unknown node key or comparison operator is [`ErrorKind::UnknownOperator`];
//! * a structurally broken node (empty `and`, `null` literal, deep nesting,
//!   wrong JSON shape) is [`ErrorKind::InvalidPredicate`].
//!
//! Unknown or malformed input is never coerced into a silently-true predicate.

use serde_json::{json, Map, Value};

use crate::error::{ErrorKind, IndexError, Result};
use crate::scalar::{CompareOp, Scalar};

/// Maximum nesting depth accepted from untrusted JSON. Deeper trees are
/// rejected instead of overflowing the evaluator's recursion.
pub const MAX_DEPTH: usize = 64;

#[derive(Debug, Clone, PartialEq)]
pub enum Predicate {
    /// `column <op> literal` — three-valued at the row level (NULL -> UNKNOWN).
    Compare { column: String, op: CompareOp, value: Scalar },
    /// `column IS NULL`.
    IsNull { column: String },
    And(Vec<Predicate>),
    Or(Vec<Predicate>),
    Not(Box<Predicate>),
}

impl Predicate {
    pub fn compare(column: &str, op: CompareOp, value: Scalar) -> Self {
        Predicate::Compare { column: column.to_string(), op, value }
    }

    pub fn is_null(column: &str) -> Self {
        Predicate::IsNull { column: column.to_string() }
    }

    pub fn and(children: Vec<Predicate>) -> Self {
        Predicate::And(children)
    }

    pub fn or(children: Vec<Predicate>) -> Self {
        Predicate::Or(children)
    }

    pub fn not(child: Predicate) -> Self {
        Predicate::Not(Box::new(child))
    }

    /// Parse from JSON with typed errors. See module docs for the grammar.
    pub fn from_json(value: &Value) -> Result<Self> {
        Self::from_json_at(value, 0)
    }

    fn from_json_at(value: &Value, depth: usize) -> Result<Self> {
        if depth > MAX_DEPTH {
            return Err(IndexError::new(
                ErrorKind::InvalidPredicate,
                format!("predicate nesting exceeds {MAX_DEPTH} levels"),
            ));
        }
        let obj = value.as_object().ok_or_else(|| {
            IndexError::new(
                ErrorKind::InvalidPredicate,
                format!("predicate node must be a JSON object, got {}", json_kind(value)),
            )
        })?;
        if obj.len() != 1 {
            return Err(IndexError::new(
                ErrorKind::InvalidPredicate,
                format!("predicate node must have exactly one operator key, got {}", obj.len()),
            ));
        }
        let (key, body) = obj.iter().next().expect("len checked");
        match key.as_str() {
            "cmp" => Self::parse_cmp(body),
            "is_null" => Self::parse_is_null(body),
            "and" | "or" => {
                let items = body.as_array().ok_or_else(|| {
                    IndexError::new(
                        ErrorKind::InvalidPredicate,
                        format!("{key:?} expects a non-empty array of predicates"),
                    )
                })?;
                if items.is_empty() {
                    return Err(IndexError::new(
                        ErrorKind::InvalidPredicate,
                        format!("{key:?} with zero children is not a valid three-valued predicate"),
                    ));
                }
                let children: Vec<Predicate> =
                    items.iter().map(|c| Self::from_json_at(c, depth + 1)).collect::<Result<_>>()?;
                Ok(if key == "and" { Predicate::And(children) } else { Predicate::Or(children) })
            }
            "not" => Ok(Predicate::Not(Box::new(Self::from_json_at(body, depth + 1)?))),
            other => Err(IndexError::new(
                ErrorKind::UnknownOperator,
                format!("unknown predicate operator {other:?}; expected one of cmp/is_null/and/or/not"),
            )),
        }
    }

    fn parse_cmp(body: &Value) -> Result<Self> {
        let obj = body.as_object().ok_or_else(|| {
            IndexError::new(ErrorKind::InvalidPredicate, "\"cmp\" expects an object {column, op, value}")
        })?;
        let column = required_string(obj, "column", "cmp")?;
        let op_raw = required_string(obj, "op", "cmp")?;
        let op: CompareOp = serde_json::from_value(Value::String(op_raw.clone())).map_err(|_| {
            IndexError::new(
                ErrorKind::UnknownOperator,
                format!("unknown comparison operator {op_raw:?}; expected =/!=/</<=/>/>= (or eq/ne/lt/le/gt/ge)"),
            )
        })?;
        let value = obj.get("value").ok_or_else(|| {
            IndexError::new(ErrorKind::InvalidPredicate, "\"cmp\" is missing \"value\"")
        })?;
        if value.is_null() {
            return Err(IndexError::new(
                ErrorKind::InvalidPredicate,
                "cmp with a JSON null literal is not a predicate; use {\"is_null\": ...} (SQL NULL never compares)",
            ));
        }
        let scalar: Scalar = serde_json::from_value(value.clone()).map_err(|e| {
            IndexError::new(ErrorKind::InvalidPredicate, format!("cmp literal is not a supported scalar: {e}"))
        })?;
        Ok(Predicate::Compare { column, op, value: scalar })
    }

    fn parse_is_null(body: &Value) -> Result<Self> {
        // Accept both {"is_null": {"column": "c"}} and {"is_null": "c"}.
        let column = match body {
            Value::String(name) => name.clone(),
            Value::Object(obj) => required_string(obj, "column", "is_null")?,
            other => {
                return Err(IndexError::new(
                    ErrorKind::InvalidPredicate,
                    format!("\"is_null\" expects a column name or {{\"column\": name}}, got {}", json_kind(other)),
                ))
            }
        };
        if column.is_empty() {
            return Err(IndexError::new(ErrorKind::InvalidPredicate, "is_null column name is empty"));
        }
        Ok(Predicate::IsNull { column })
    }

    /// Columns referenced anywhere in the tree (deduplicated, first-use order).
    pub fn columns(&self) -> Vec<&str> {
        let mut out: Vec<&str> = Vec::new();
        self.collect_columns(&mut out);
        out
    }

    fn collect_columns<'a>(&'a self, out: &mut Vec<&'a str>) {
        match self {
            Predicate::Compare { column, .. } | Predicate::IsNull { column } => {
                if !out.contains(&column.as_str()) {
                    out.push(column);
                }
            }
            Predicate::And(cs) | Predicate::Or(cs) => {
                for c in cs {
                    c.collect_columns(out);
                }
            }
            Predicate::Not(c) => c.collect_columns(out),
        }
    }

    /// If this predicate is a flat conjunction of equality comparisons on
    /// distinct columns, return the `(column, literal)` pairs in order. This
    /// is the exact shape a composite index can serve.
    pub fn as_eq_conjunction(&self) -> Option<Vec<(&str, &Scalar)>> {
        let children = match self {
            Predicate::And(cs) => cs.as_slice(),
            single => std::slice::from_ref(single),
        };
        let mut out: Vec<(&str, &Scalar)> = Vec::with_capacity(children.len());
        for c in children {
            match c {
                Predicate::Compare { column, op: CompareOp::Eq, value } => {
                    if out.iter().any(|(seen, _)| *seen == column.as_str()) {
                        return None; // duplicate column: not a plain key lookup
                    }
                    out.push((column.as_str(), value));
                }
                _ => return None,
            }
        }
        if out.is_empty() {
            return None;
        }
        Some(out)
    }

    /// Compact, stable JSON rendering used in logs and responses.
    pub fn to_json(&self) -> Value {
        match self {
            Predicate::Compare { column, op, value } => {
                json!({"cmp": {"column": column, "op": op.as_str(), "value": value}})
            }
            Predicate::IsNull { column } => json!({"is_null": {"column": column}}),
            Predicate::And(cs) => json!({"and": cs.iter().map(|c| c.to_json()).collect::<Vec<_>>()}),
            Predicate::Or(cs) => json!({"or": cs.iter().map(|c| c.to_json()).collect::<Vec<_>>()}),
            Predicate::Not(c) => json!({"not": c.to_json()}),
        }
    }

    /// One-line human rendering for step logs.
    pub fn describe(&self) -> String {
        match self {
            Predicate::Compare { column, op, value } => format!("{column} {} {value:?}", op.as_str()),
            Predicate::IsNull { column } => format!("{column} IS NULL"),
            Predicate::And(cs) => {
                let inner: Vec<String> = cs.iter().map(|c| c.describe()).collect();
                format!("({})", inner.join(" AND "))
            }
            Predicate::Or(cs) => {
                let inner: Vec<String> = cs.iter().map(|c| c.describe()).collect();
                format!("({})", inner.join(" OR "))
            }
            Predicate::Not(c) => format!("NOT {}", c.describe()),
        }
    }
}

fn required_string(obj: &Map<String, Value>, field: &str, node: &str) -> Result<String> {
    match obj.get(field) {
        Some(Value::String(s)) if !s.is_empty() => Ok(s.clone()),
        Some(Value::String(_)) => Err(IndexError::new(
            ErrorKind::InvalidPredicate,
            format!("{node:?} field {field:?} is an empty string"),
        )),
        Some(other) => Err(IndexError::new(
            ErrorKind::InvalidPredicate,
            format!("{node:?} field {field:?} must be a string, got {}", json_kind(other)),
        )),
        None => Err(IndexError::new(
            ErrorKind::InvalidPredicate,
            format!("{node:?} is missing required field {field:?}"),
        )),
    }
}

fn json_kind(v: &Value) -> &'static str {
    match v {
        Value::Null => "null",
        Value::Bool(_) => "a boolean",
        Value::Number(_) => "a number",
        Value::String(_) => "a string",
        Value::Array(_) => "an array",
        Value::Object(_) => "an object",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_all_node_kinds() {
        let p = Predicate::from_json(&json!({
            "and": [
                {"cmp": {"column": "a", "op": ">=", "value": 10}},
                {"or": [
                    {"is_null": {"column": "b"}},
                    {"not": {"cmp": {"column": "c", "op": "eq", "value": "x"}}}
                ]}
            ]
        }))
        .unwrap();
        assert_eq!(p.columns(), vec!["a", "b", "c"]);
        // Round-trip: to_json output parses back to the same tree.
        let again = Predicate::from_json(&p.to_json()).unwrap();
        assert_eq!(again, p);
    }

    #[test]
    fn unknown_operator_is_typed() {
        let err = Predicate::from_json(&json!({"between": [1, 2]})).unwrap_err();
        assert_eq!(err.kind, ErrorKind::UnknownOperator);
        let err = Predicate::from_json(&json!({"cmp": {"column": "a", "op": "~=", "value": 1}})).unwrap_err();
        assert_eq!(err.kind, ErrorKind::UnknownOperator);
    }

    #[test]
    fn structural_errors_are_typed() {
        let cases = [
            json!({"and": []}),
            json!({"or": []}),
            json!({"cmp": {"column": "a", "op": "=", "value": null}}),
            json!({"cmp": {"column": "a", "op": "="}}),
            json!({"cmp": {"op": "=", "value": 1}}),
            json!({"is_null": 7}),
            json!([]),
            json!({"and": [{"cmp": {"column": "a", "op": "=", "value": 1}}], "extra": 1}),
        ];
        for (i, c) in cases.iter().enumerate() {
            let err = Predicate::from_json(c).unwrap_err();
            assert_eq!(err.kind, ErrorKind::InvalidPredicate, "case {i}: {c}");
        }
    }

    #[test]
    fn excessive_depth_is_rejected_not_crashing() {
        let mut v = json!({"cmp": {"column": "a", "op": "=", "value": 1}});
        for _ in 0..(MAX_DEPTH + 5) {
            v = json!({"not": v});
        }
        let err = Predicate::from_json(&v).unwrap_err();
        assert_eq!(err.kind, ErrorKind::InvalidPredicate);
    }

    #[test]
    fn eq_conjunction_detection() {
        let p = Predicate::from_json(&json!({
            "and": [
                {"cmp": {"column": "a", "op": "=", "value": 1}},
                {"cmp": {"column": "b", "op": "=", "value": "x"}}
            ]
        }))
        .unwrap();
        let pairs = p.as_eq_conjunction().unwrap();
        assert_eq!(pairs.len(), 2);

        let not_conj = Predicate::from_json(&json!({
            "and": [
                {"cmp": {"column": "a", "op": "=", "value": 1}},
                {"cmp": {"column": "a", "op": ">", "value": 2}}
            ]
        }))
        .unwrap();
        assert!(not_conj.as_eq_conjunction().is_none());
    }
}
