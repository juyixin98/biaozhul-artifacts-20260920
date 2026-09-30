//! Typed in-memory batches backed by [`arrow2`].
//!
//! Only a small, explicit set of logical types is indexable:
//! `INTEGER` (arrow `Int64`), `TEXT` (arrow `Utf8`) and `BOOLEAN`, all with
//! arrow validity buffers to represent SQL `NULL`. Floats are deliberately
//! excluded from value indexes (equality on floats is semantically dubious);
//! see README "Limitations".

use arrow2::array::{Array, BooleanArray, PrimitiveArray, Utf8Array};
use arrow2::chunk::Chunk;
use arrow2::datatypes::{DataType as ArrowDataType, Field, Schema};

use crate::error::TritmapError;

/// Logical, indexable column type.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LogicalType {
    Integer,
    Text,
    Boolean,
}

impl LogicalType {
    pub fn as_str(self) -> &'static str {
        match self {
            LogicalType::Integer => "INTEGER",
            LogicalType::Text => "TEXT",
            LogicalType::Boolean => "BOOLEAN",
        }
    }

    pub fn to_arrow(self) -> ArrowDataType {
        match self {
            LogicalType::Integer => ArrowDataType::Int64,
            LogicalType::Text => ArrowDataType::Utf8,
            LogicalType::Boolean => ArrowDataType::Boolean,
        }
    }

    pub fn from_arrow(dt: &ArrowDataType) -> Result<Self, TritmapError> {
        match dt {
            ArrowDataType::Int64 => Ok(LogicalType::Integer),
            ArrowDataType::Utf8 => Ok(LogicalType::Text),
            ArrowDataType::Boolean => Ok(LogicalType::Boolean),
            other => Err(TritmapError::UnsupportedType(format!("{other:?}"))),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum Scalar {
    Int(i64),
    Text(String),
    Bool(bool),
}

impl std::fmt::Display for Scalar {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Scalar::Int(v) => write!(f, "{v}"),
            Scalar::Text(v) => write!(f, "'{}'", v.replace('\'', "''")),
            Scalar::Bool(v) => write!(f, "{v}"),
        }
    }
}

/// A column declaration: name plus logical type.
#[derive(Debug, Clone)]
pub struct ColumnSpec {
    pub name: String,
    pub ty: LogicalType,
}

/// A typed batch: named, typed columns and an arrow2 [`Chunk`].
#[derive(Debug, Clone)]
pub struct TypedBatch {
    columns: Vec<ColumnSpec>,
    chunk: Chunk<Box<dyn Array>>,
}

impl TypedBatch {
    /// Validate a chunk against declared columns and wrap it.
    pub fn try_new(
        columns: Vec<ColumnSpec>,
        chunk: Chunk<Box<dyn Array>>,
    ) -> Result<Self, TritmapError> {
        if columns.len() != chunk.arrays().len() {
            return Err(TritmapError::SchemaMismatch(format!(
                "declared {} columns but chunk has {}",
                columns.len(),
                chunk.arrays().len()
            )));
        }
        for (spec, arr) in columns.iter().zip(chunk.arrays()) {
            let actual = LogicalType::from_arrow(arr.data_type())?;
            if actual != spec.ty {
                return Err(TritmapError::SchemaMismatch(format!(
                    "column '{}' declared {}, chunk contains {}",
                    spec.name,
                    spec.ty.as_str(),
                    actual.as_str()
                )));
            }
            if arr.len() != chunk.len() {
                return Err(TritmapError::SchemaMismatch(format!(
                    "ragged column '{}': {} rows vs batch {}",
                    spec.name,
                    arr.len(),
                    chunk.len()
                )));
            }
        }
        Ok(Self { columns, chunk })
    }

    pub fn arrow_schema(&self) -> Schema {
        Schema::from(
            self.columns
                .iter()
                .map(|c| Field::new(&c.name, c.ty.to_arrow(), true))
                .collect::<Vec<_>>(),
        )
    }

    pub fn len(&self) -> usize {
        self.chunk.len()
    }

    pub fn is_empty(&self) -> bool {
        self.chunk.len() == 0
    }

    pub fn columns(&self) -> &[ColumnSpec] {
        &self.columns
    }

    pub fn chunk(&self) -> &Chunk<Box<dyn Array>> {
        &self.chunk
    }

    pub fn column_index(&self, name: &str) -> Option<usize> {
        self.columns.iter().position(|c| c.name == name)
    }

    pub fn column_spec(&self, name: &str) -> Option<&ColumnSpec> {
        self.columns.iter().find(|c| c.name == name)
    }

    pub fn array(&self, name: &str) -> Option<&dyn Array> {
        self.column_index(name).map(|i| self.chunk.arrays()[i].as_ref())
    }

    /// Extract a column as nullable scalars. Returns a typed error for unknown
    /// columns or unsupported physical types — never an empty/garbage vector.
    pub fn column_values(&self, name: &str) -> Result<Vec<Option<Scalar>>, TritmapError> {
        let idx = self
            .column_index(name)
            .ok_or_else(|| TritmapError::UnknownColumn(name.to_string()))?;
        let spec = &self.columns[idx];
        let arr = &self.chunk.arrays()[idx];
        extract_scalars(arr.as_ref(), spec.ty)
    }
}

fn extract_scalars(arr: &dyn Array, ty: LogicalType) -> Result<Vec<Option<Scalar>>, TritmapError> {
    let n = arr.len();
    let mut out = Vec::with_capacity(n);
    match ty {
        LogicalType::Integer => {
            let a = arr
                .as_any()
                .downcast_ref::<PrimitiveArray<i64>>()
                .ok_or_else(|| TritmapError::internal("Int64 downcast failed"))?;
            for i in 0..n {
                // Value only read when the validity bit says non-null.
                out.push(if a.is_null(i) {
                    None
                } else {
                    Some(Scalar::Int(a.value(i)))
                });
            }
        }
        LogicalType::Text => {
            let a = arr
                .as_any()
                .downcast_ref::<Utf8Array<i32>>()
                .ok_or_else(|| TritmapError::internal("Utf8 downcast failed"))?;
            for i in 0..n {
                out.push(if a.is_null(i) {
                    None
                } else {
                    Some(Scalar::Text(a.value(i).to_string()))
                });
            }
        }
        LogicalType::Boolean => {
            let a = arr
                .as_any()
                .downcast_ref::<BooleanArray>()
                .ok_or_else(|| TritmapError::internal("Boolean downcast failed"))?;
            for i in 0..n {
                out.push(if a.is_null(i) {
                    None
                } else {
                    Some(Scalar::Bool(a.value(i)))
                });
            }
        }
    }
    Ok(out)
}

/// Builder for small synthetic / fixture batches. Each `Vec` may contain
/// `NULL`; all columns must have equal length.
#[derive(Default)]
pub struct BatchBuilder {
    columns: Vec<ColumnSpec>,
    arrays: Vec<Box<dyn Array>>,
}

impl BatchBuilder {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn add_integer(mut self, name: &str, values: Vec<Option<i64>>) -> Self {
        self.columns.push(ColumnSpec {
            name: name.to_string(),
            ty: LogicalType::Integer,
        });
        self.arrays
            .push(Box::new(PrimitiveArray::<i64>::from(values)));
        self
    }

    pub fn add_text(mut self, name: &str, values: Vec<Option<&str>>) -> Self {
        self.columns.push(ColumnSpec {
            name: name.to_string(),
            ty: LogicalType::Text,
        });
        self.arrays
            .push(Box::new(Utf8Array::<i32>::from(values)));
        self
    }

    pub fn add_boolean(mut self, name: &str, values: Vec<Option<bool>>) -> Self {
        self.columns.push(ColumnSpec {
            name: name.to_string(),
            ty: LogicalType::Boolean,
        });
        self.arrays.push(Box::new(BooleanArray::from(values)));
        self
    }

    pub fn build(self) -> Result<TypedBatch, TritmapError> {
        let n = self.arrays.first().map(|a| a.len()).unwrap_or(0);
        for a in &self.arrays {
            if a.len() != n {
                return Err(TritmapError::SchemaMismatch(format!(
                    "ragged fixture columns: {n} vs {}",
                    a.len()
                )));
            }
        }
        let chunk = Chunk::new(self.arrays);
        TypedBatch::try_new(self.columns, chunk)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nulls_round_trip_through_arrow_validity() {
        let b = BatchBuilder::new()
            .add_integer("id", vec![Some(1), None, Some(-7), None])
            .add_text("name", vec![Some("a"), None, Some("c"), Some("d")])
            .add_boolean("flag", vec![Some(true), Some(false), None, None])
            .build()
            .unwrap();
        assert_eq!(b.len(), 4);
        let ids = b.column_values("id").unwrap();
        assert_eq!(ids, vec![Some(Scalar::Int(1)), None, Some(Scalar::Int(-7)), None]);
        assert_eq!(
            b.column_values("flag").unwrap()[2],
            None,
            "validity bit must surface as NULL, not a zero/false value"
        );
        assert!(matches!(
            b.column_values("missing"),
            Err(TritmapError::UnknownColumn(_))
        ));
    }

    #[test]
    fn type_mismatch_is_rejected() {
        let cols = vec![ColumnSpec {
            name: "x".into(),
            ty: LogicalType::Text,
        }];
        let chunk = Chunk::new(vec![Box::new(PrimitiveArray::<i64>::from(vec![
            Some(1i64),
        ])) as Box<dyn Array>]);
        assert!(matches!(
            TypedBatch::try_new(cols, chunk),
            Err(TritmapError::SchemaMismatch(_))
        ));
    }
}
