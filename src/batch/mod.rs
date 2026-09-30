//! Typed columnar batches built on arrow2.
//!
//! All execution operators (`crate::exec`) operate on [`Batch`] values: arrow2
//! arrays with a small, explicitly typed schema. Rows are only materialized at
//! the service boundary (JSON in / JSON out).

use arrow2::array::{
    Array, BooleanArray, PrimitiveArray, Utf8Array,
};
use arrow2::chunk::Chunk;
use arrow2::datatypes::DataType as ArrowDataType;
use serde::{Deserialize, Serialize};
use std::hash::{Hash, Hasher};

/// The restricted set of column types supported by the service.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "PascalCase")]
pub enum ColType {
    Int64,
    Float64,
    Boolean,
    Utf8,
}

impl ColType {
    pub fn to_arrow(self) -> ArrowDataType {
        match self {
            ColType::Int64 => ArrowDataType::Int64,
            ColType::Float64 => ArrowDataType::Float64,
            ColType::Boolean => ArrowDataType::Boolean,
            ColType::Utf8 => ArrowDataType::Utf8,
        }
    }
}

/// A field in a typed batch.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Field {
    pub name: String,
    #[serde(rename = "type")]
    pub col_type: ColType,
}

impl Field {
    pub fn new(name: impl Into<String>, col_type: ColType) -> Self {
        Self { name: name.into(), col_type }
    }
}

/// Scalar value. [`f64`] equality/hashing is defined by bit pattern so values
/// can be used as hash-group keys. (NaN groups are an documented limitation;
/// see README.)
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Value {
    Int(i64),
    Float(f64),
    Bool(bool),
}

impl Value {
    pub fn type_of(self) -> ColType {
        match self {
            Value::Int(_) => ColType::Int64,
            Value::Float(_) => ColType::Float64,
            Value::Bool(_) => ColType::Boolean,
        }
    }

    pub fn as_json(self) -> serde_json::Value {
        match self {
            Value::Int(i) => serde_json::Value::Number(serde_json::Number::from(i)),
            Value::Float(f) => serde_json::Number::from_f64(f)
                .map(serde_json::Value::Number)
                .unwrap_or(serde_json::Value::Null),
            Value::Bool(b) => serde_json::Value::Bool(b),
        }
    }
}

/// String values are not part of [`Value`] (which stays `Copy`); strings travel
/// as their own variant in [`Datum`].
#[derive(Debug, Clone)]
pub enum Scalar {
    Val(Value),
    Str(String),
}

impl Scalar {
    pub fn type_of(&self) -> ColType {
        match self {
            Scalar::Val(v) => v.type_of(),
            Scalar::Str(_) => ColType::Utf8,
        }
    }

    pub fn as_json(&self) -> serde_json::Value {
        match self {
            Scalar::Val(v) => v.as_json(),
            Scalar::Str(s) => serde_json::Value::String(s.clone()),
        }
    }
}

/// A possibly-NULL typed cell.
#[derive(Debug, Clone)]
pub struct Datum {
    pub value: Option<Scalar>,
}

impl Datum {
    pub fn null() -> Self {
        Datum { value: None }
    }

    pub fn from_value(v: Value) -> Self {
        Datum { value: Some(Scalar::Val(v)) }
    }

    pub fn int(i: i64) -> Self {
        Self::from_value(Value::Int(i))
    }

    pub fn string(s: impl Into<String>) -> Self {
        Datum { value: Some(Scalar::Str(s.into())) }
    }

    pub fn is_null(&self) -> bool {
        self.value.is_none()
    }
}

/// Structural equality: NULL = NULL here (used for GROUP BY key identity and
/// test oracles). Join *matching* additionally treats NULL keys as
/// non-matching, implemented in the operators.
impl PartialEq for Datum {
    fn eq(&self, other: &Self) -> bool {
        match (&self.value, &other.value) {
            (None, None) => true,
            (Some(Scalar::Str(a)), Some(Scalar::Str(b))) => a == b,
            (Some(Scalar::Val(Value::Float(a))), Some(Scalar::Val(Value::Float(b)))) => {
                a.to_bits() == b.to_bits()
            }
            (Some(Scalar::Val(a)), Some(Scalar::Val(b))) => a == b,
            _ => false,
        }
    }
}

impl Eq for Datum {}

impl Hash for Datum {
    fn hash<H: Hasher>(&self, state: &mut H) {
        match &self.value {
            None => 0u8.hash(state),
            Some(Scalar::Str(s)) => {
                1u8.hash(state);
                s.hash(state);
            }
            Some(Scalar::Val(Value::Int(i))) => {
                2u8.hash(state);
                i.hash(state);
            }
            Some(Scalar::Val(Value::Float(f))) => {
                3u8.hash(state);
                f.to_bits().hash(state);
            }
            Some(Scalar::Val(Value::Bool(b))) => {
                4u8.hash(state);
                b.hash(state);
            }
        }
    }
}

/// A typed columnar batch: arrow2 [`Chunk`] plus its logical field metadata.
#[derive(Debug, Clone)]
pub struct Batch {
    fields: Vec<Field>,
    chunk: Chunk<Box<dyn Array>>,
}

impl Batch {
    pub fn new(fields: Vec<Field>, columns: Vec<Box<dyn Array>>) -> Self {
        assert_eq!(fields.len(), columns.len(), "field/column count mismatch");
        for (f, c) in fields.iter().zip(columns.iter()) {
            assert_eq!(&f.col_type.to_arrow(), c.data_type(), "type mismatch for {}", f.name);
            if !columns.is_empty() {
                assert_eq!(c.len(), columns[0].len(), "column length mismatch for {}", f.name);
            }
        }
        Batch { fields, chunk: Chunk::new(columns) }
    }

    pub fn empty(fields: Vec<Field>) -> Self {
        let columns = fields.iter().map(|f| build_column(f.col_type, &[])).collect();
        Batch { fields, chunk: Chunk::new(columns) }
    }

    pub fn fields(&self) -> &[Field] {
        &self.fields
    }

    pub fn columns(&self) -> &[Box<dyn Array>] {
        self.chunk.columns()
    }

    pub fn len(&self) -> usize {
        self.chunk.len()
    }

    pub fn is_empty(&self) -> bool {
        self.chunk.len() == 0
    }

    pub fn column_index(&self, name: &str) -> Option<usize> {
        self.fields.iter().position(|f| f.name == name)
    }

    /// Read a single cell. Returns [`Datum::null`] for SQL NULL.
    pub fn cell(&self, col: usize, row: usize) -> Datum {
        read_cell(self.columns()[col].as_ref(), row)
    }

    /// Materialize one row as JSON, preserving column order.
    pub fn row_json(&self, row: usize) -> serde_json::Map<String, serde_json::Value> {
        self.fields
            .iter()
            .enumerate()
            .map(|(col, f)| {
                let v = match self.cell(col, row).value {
                    None => serde_json::Value::Null,
                    Some(s) => s.as_json(),
                };
                (f.name.clone(), v)
            })
            .collect()
    }

    pub fn rows_json(&self) -> Vec<serde_json::Map<String, serde_json::Value>> {
        (0..self.len()).map(|r| self.row_json(r)).collect()
    }

    /// Select a subset of columns (with possible duplicates / renames via the
    /// `(new_name, old_index)` pairs).
    pub fn project_named(&self, picks: &[(String, usize)]) -> Batch {
        let fields = picks
            .iter()
            .map(|(name, idx)| Field::new(name.clone(), self.fields[*idx].col_type))
            .collect();
        let columns = picks.iter().map(|(_, idx)| self.columns()[*idx].clone()).collect();
        Batch::new(fields, columns)
    }
}

/// Construct an arrow2 column of `col_type` from typed datums.
pub fn build_column(col_type: ColType, values: &[Datum]) -> Box<dyn Array> {
    match col_type {
        ColType::Int64 => {
            let vec: Vec<Option<i64>> = values
                .iter()
                .map(|d| match &d.value {
                    Some(Scalar::Val(Value::Int(i))) => Some(*i),
                    Some(other) => panic!("expected Int64, got {:?}", other),
                    None => None,
                })
                .collect();
            Box::new(PrimitiveArray::<i64>::from_iter(vec))
        }
        ColType::Float64 => {
            let vec: Vec<Option<f64>> = values
                .iter()
                .map(|d| match &d.value {
                    Some(Scalar::Val(Value::Float(f))) => Some(*f),
                    Some(other) => panic!("expected Float64, got {:?}", other),
                    None => None,
                })
                .collect();
            Box::new(PrimitiveArray::<f64>::from_iter(vec))
        }
        ColType::Boolean => {
            let vec: Vec<Option<bool>> = values
                .iter()
                .map(|d| match &d.value {
                    Some(Scalar::Val(Value::Bool(b))) => Some(*b),
                    Some(other) => panic!("expected Boolean, got {:?}", other),
                    None => None,
                })
                .collect();
            Box::new(BooleanArray::from_iter(vec))
        }
        ColType::Utf8 => {
            let vec: Vec<Option<&str>> = values
                .iter()
                .map(|d| match &d.value {
                    Some(Scalar::Str(s)) => Some(s.as_str()),
                    Some(other) => panic!("expected Utf8, got {:?}", other),
                    None => None,
                })
                .collect();
            Box::new(Utf8Array::<i32>::from_iter(vec))
        }
    }
}

/// Read a typed cell out of an arrow2 array.
pub fn read_cell(array: &dyn Array, row: usize) -> Datum {
    if !array.is_valid(row) {
        return Datum::null();
    }
    match array.data_type() {
        ArrowDataType::Int64 => {
            let a = array.as_any().downcast_ref::<PrimitiveArray<i64>>().unwrap();
            Datum::int(a.value(row))
        }
        ArrowDataType::Float64 => {
            let a = array.as_any().downcast_ref::<PrimitiveArray<f64>>().unwrap();
            Datum::from_value(Value::Float(a.value(row)))
        }
        ArrowDataType::Boolean => {
            let a = array.as_any().downcast_ref::<BooleanArray>().unwrap();
            Datum::from_value(Value::Bool(a.value(row)))
        }
        ArrowDataType::Utf8 => {
            let a = array.as_any().downcast_ref::<Utf8Array<i32>>().unwrap();
            Datum::string(a.value(row))
        }
        other => panic!("unsupported arrow type {:?}", other),
    }
}

/// Convert a JSON value of the declared column type into a (possibly NULL) datum.
pub fn datum_from_json(col_type: ColType, json: &serde_json::Value) -> Result<Datum, String> {
    if json.is_null() {
        return Ok(Datum::null());
    }
    match (col_type, json) {
        (ColType::Int64, serde_json::Value::Number(n)) => n
            .as_i64()
            .map(Datum::int)
            .ok_or_else(|| format!("value {} is not an Int64", n)),
        (ColType::Float64, serde_json::Value::Number(n)) => n
            .as_f64()
            .map(|f| Datum::from_value(Value::Float(f)))
            .ok_or_else(|| format!("value {} is not a Float64", n)),
        (ColType::Boolean, serde_json::Value::Bool(b)) => {
            Ok(Datum::from_value(Value::Bool(*b)))
        }
        (ColType::Utf8, serde_json::Value::String(s)) => Ok(Datum::string(s.clone())),
        (t, v) => Err(format!("expected {:?}, got JSON {}", t, v)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn null_round_trips_through_arrow_arrays_for_every_type() {
        for col_type in [ColType::Int64, ColType::Float64, ColType::Boolean, ColType::Utf8] {
            let (datums, expected_null): (Vec<Datum>, Vec<bool>) = match col_type {
                ColType::Int64 => (vec![Datum::int(1), Datum::null(), Datum::int(-3)], vec![false, true, false]),
                ColType::Float64 => (
                    vec![Datum::from_value(Value::Float(1.5)), Datum::null()],
                    vec![false, true],
                ),
                ColType::Boolean => (
                    vec![Datum::from_value(Value::Bool(true)), Datum::null(), Datum::from_value(Value::Bool(false))],
                    vec![false, true, false],
                ),
                ColType::Utf8 => (vec![Datum::string("x"), Datum::null(), Datum::string("")], vec![false, true, false]),
            };
            let array = build_column(col_type, &datums);
            for (i, &is_null) in expected_null.iter().enumerate() {
                assert_eq!(array.is_valid(i), !is_null, "{:?} validity at {}", col_type, i);
                assert_eq!(read_cell(array.as_ref(), i), datums[i], "{:?} round-trip at {}", col_type, i);
            }
        }
    }

    #[test]
    fn structural_datum_equality_treats_null_equal_to_null() {
        assert_eq!(Datum::null(), Datum::null());
        assert_ne!(Datum::null(), Datum::int(0));
        assert_ne!(Datum::int(0), Datum::null());
        assert_eq!(Datum::int(7), Datum::int(7));
        assert_ne!(Datum::int(7), Datum::string("7"));
    }

    #[test]
    fn json_coercion_rules_are_strict_at_the_boundary() {
        assert!(datum_from_json(ColType::Int64, &serde_json::json!(1.5)).is_err());
        assert!(datum_from_json(ColType::Int64, &serde_json::json!("1")).is_err());
        assert!(datum_from_json(ColType::Boolean, &serde_json::json!(1)).is_err());
        assert!(datum_from_json(ColType::Utf8, &serde_json::json!(1)).is_err());
        assert!(datum_from_json(ColType::Int64, &serde_json::Value::Null).unwrap().is_null());
    }

    #[test]
    fn projection_preserves_column_order_and_duplicates() {
        let fields = vec![Field::new("a", ColType::Int64), Field::new("b", ColType::Int64)];
        let cols = vec![build_column(ColType::Int64, &[Datum::int(1)]), build_column(ColType::Int64, &[Datum::int(2)])];
        let batch = Batch::new(fields, cols);
        let p = batch.project_named(&[("b2".to_string(), 1), ("a2".to_string(), 0), ("b3".to_string(), 1)]);
        assert_eq!(p.fields().iter().map(|f| f.name.as_str()).collect::<Vec<_>>(), vec!["b2", "a2", "b3"]);
        let row = p.row_json(0);
        assert_eq!(row["b2"], serde_json::json!(2));
        assert_eq!(row["a2"], serde_json::json!(1));
        assert_eq!(row["b3"], serde_json::json!(2));
    }
}
