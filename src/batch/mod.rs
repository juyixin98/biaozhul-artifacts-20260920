//! Typed columnar batches built on arrow2.
//!
//! A [`Batch`] is a set of named, typed columns of equal length backed by
//! arrow2 arrays. [`Value`] is the row-level materialization of a single cell
//! used by the interpreter and by hash-key construction during decorrelated
//! execution.

use arrow2::array::{Array, BooleanArray, Float64Array, Int64Array, Utf8Array};
use arrow2::datatypes::DataType;
use std::fmt;
use std::sync::Arc;

use crate::error::{EngineError, ErrorKind};

/// Logical scalar types supported by the engine.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ScalarType {
    Bool,
    Int,
    Float,
    Str,
}

impl ScalarType {
    pub fn to_arrow(self) -> DataType {
        match self {
            ScalarType::Bool => DataType::Boolean,
            ScalarType::Int => DataType::Int64,
            ScalarType::Float => DataType::Float64,
            ScalarType::Str => DataType::Utf8,
        }
    }

    pub fn from_arrow(dt: &DataType) -> Result<Self, EngineError> {
        match dt {
            DataType::Boolean => Ok(ScalarType::Bool),
            DataType::Int64 => Ok(ScalarType::Int),
            DataType::Float64 => Ok(ScalarType::Float),
            DataType::Utf8 => Ok(ScalarType::Str),
            other => Err(EngineError::new(
                ErrorKind::UnsupportedForm,
                format!("unsupported column type: {other:?}"),
            )),
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            ScalarType::Bool => "bool",
            ScalarType::Int => "int",
            ScalarType::Float => "float",
            ScalarType::Str => "str",
        }
    }
}

/// A materialized scalar value. `Null` carries no type; type checks happen
/// against the column schema, not individual values.
#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
}

impl Value {
    pub fn is_null(&self) -> bool {
        matches!(self, Value::Null)
    }

    /// Hash-key encoding. Nulls are excluded from correlation keys upstream;
    /// this encoding only needs to distinguish non-null values across types.
    pub fn key_bytes(&self) -> Vec<u8> {
        match self {
            Value::Null => vec![0u8],
            Value::Bool(b) => [vec![1u8], vec![*b as u8]].concat(),
            Value::Int(i) => [vec![2u8], i.to_le_bytes().to_vec()].concat(),
            Value::Float(f) => [vec![3u8], f.to_bits().to_le_bytes().to_vec()].concat(),
            Value::Str(s) => [vec![4u8], s.as_bytes().to_vec()].concat(),
        }
    }

    pub fn to_json(&self) -> serde_json::Value {
        match self {
            Value::Null => serde_json::Value::Null,
            Value::Bool(b) => serde_json::json!(b),
            Value::Int(i) => serde_json::json!(i),
            Value::Float(f) => serde_json::json!(f),
            Value::Str(s) => serde_json::json!(s),
        }
    }
}

impl fmt::Display for Value {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Value::Null => write!(f, "NULL"),
            Value::Bool(b) => write!(f, "{b}"),
            Value::Int(i) => write!(f, "{i}"),
            Value::Float(x) => write!(f, "{x}"),
            Value::Str(s) => write!(f, "{s}"),
        }
    }
}

/// A named, typed column backed by an arrow2 array.
#[derive(Debug, Clone)]
pub struct Column {
    pub name: String,
    pub ty: ScalarType,
    pub array: Arc<dyn Array>,
}

impl Column {
    pub fn len(&self) -> usize {
        self.array.len()
    }

    pub fn value(&self, row: usize) -> Value {
        if self.array.is_null(row) {
            return Value::Null;
        }
        match self.ty {
            ScalarType::Bool => {
                let a = self.array.as_any().downcast_ref::<BooleanArray>().unwrap();
                Value::Bool(a.value(row))
            }
            ScalarType::Int => {
                let a = self.array.as_any().downcast_ref::<Int64Array>().unwrap();
                Value::Int(a.value(row))
            }
            ScalarType::Float => {
                let a = self.array.as_any().downcast_ref::<Float64Array>().unwrap();
                Value::Float(a.value(row))
            }
            ScalarType::Str => {
                let a = self
                    .array
                    .as_any()
                    .downcast_ref::<Utf8Array<i32>>()
                    .unwrap();
                Value::Str(a.value(row).to_string())
            }
        }
    }
}

/// Builders for columns from Rust vectors (used by fixtures, uploads, tests).
pub struct ColumnBuilder;

impl ColumnBuilder {
    pub fn bools(name: &str, vals: &[Option<bool>]) -> Column {
        Column {
            name: name.to_string(),
            ty: ScalarType::Bool,
            array: Arc::new(BooleanArray::from_iter(vals.iter().copied())),
        }
    }

    pub fn ints(name: &str, vals: &[Option<i64>]) -> Column {
        Column {
            name: name.to_string(),
            ty: ScalarType::Int,
            array: Arc::new(Int64Array::from_iter(vals.iter().copied())),
        }
    }

    pub fn floats(name: &str, vals: &[Option<f64>]) -> Column {
        Column {
            name: name.to_string(),
            ty: ScalarType::Float,
            array: Arc::new(Float64Array::from_iter(vals.iter().copied())),
        }
    }

    pub fn strs(name: &str, vals: &[Option<&str>]) -> Column {
        Column {
            name: name.to_string(),
            ty: ScalarType::Str,
            array: Arc::new(UtfArrayHelper::from(vals)),
        }
    }
}

// Small helper so `ColumnBuilder::strs` stays a one-liner.
struct UtfArrayHelper;

impl UtfArrayHelper {
    fn from(vals: &[Option<&str>]) -> Utf8Array<i32> {
        Utf8Array::<i32>::from_iter(vals.iter().copied())
    }
}

/// A typed record batch: named columns of equal length.
#[derive(Debug, Clone)]
pub struct Batch {
    pub columns: Vec<Column>,
    pub n_rows: usize,
}

impl Batch {
    pub fn new(columns: Vec<Column>) -> Result<Self, EngineError> {
        let n_rows = columns.first().map(|c| c.len()).unwrap_or(0);
        for c in &columns {
            if c.len() != n_rows {
                return Err(EngineError::new(
                    ErrorKind::Internal,
                    format!(
                        "column '{}' has length {} but batch has {} rows",
                        c.name,
                        c.len(),
                        n_rows
                    ),
                ));
            }
        }
        // Output columns are addressed positionally by execution plans;
        // duplicate names are legal SQL (e.g. `SELECT *, k FROM t`).
        Ok(Batch { columns, n_rows })
    }

    pub fn column(&self, name: &str) -> Result<&Column, EngineError> {
        self.columns
            .iter()
            .find(|c| c.name == name)
            .ok_or_else(|| {
                EngineError::new(
                    ErrorKind::Validation,
                    format!("unknown column '{name}'"),
                )
            })
    }

    pub fn value(&self, col: &str, row: usize) -> Result<Value, EngineError> {
        Ok(self.column(col)?.value(row))
    }

    pub fn column_names(&self) -> Vec<String> {
        self.columns.iter().map(|c| c.name.clone()).collect()
    }

    /// Materialize row `row` as a vector of values in column order.
    pub fn row(&self, row: usize) -> Vec<Value> {
        self.columns.iter().map(|c| c.value(row)).collect()
    }

    /// Serialize to JSON rows (array of arrays, column order).
    pub fn to_json_rows(&self) -> Vec<Vec<serde_json::Value>> {
        (0..self.n_rows)
            .map(|r| self.row(r).iter().map(|v| v.to_json()).collect())
            .collect()
    }
}
