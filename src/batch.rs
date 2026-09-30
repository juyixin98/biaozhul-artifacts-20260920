//! Typed record batches on top of arrow2.
//!
//! A [`TypedBatch`] is a [`BatchSchema`] plus an arrow2 [`Chunk`]. All cells
//! are validated against the declared column types at ingestion: a value of
//! the wrong logical type is an [`ErrorKind::ArrowSchemaMismatch`], never a
//! silent coercion. NULL is represented by arrow validity bits, so "no value"
//! can never be confused with a sentinel value like `0` or `""`.

use std::sync::Arc;

use arrow2::array::{Array, BooleanArray, Float64Array, Int64Array, Utf8Array};
use arrow2::chunk::Chunk;
use arrow2::datatypes::DataType;

use crate::error::{ErrorKind, IndexError, Result};
use crate::scalar::Scalar;

/// Logical column types supported by this engine.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ColumnType {
    Int,
    Float,
    Bool,
    Text,
}

impl ColumnType {
    pub fn as_str(self) -> &'static str {
        match self {
            ColumnType::Int => "int",
            ColumnType::Float => "float",
            ColumnType::Bool => "bool",
            ColumnType::Text => "text",
        }
    }

    pub fn arrow_type(self) -> DataType {
        match self {
            ColumnType::Int => DataType::Int64,
            ColumnType::Float => DataType::Float64,
            ColumnType::Bool => DataType::Boolean,
            ColumnType::Text => DataType::Utf8,
        }
    }

    /// Does a non-NULL cell value belong to this column type?
    pub fn accepts(self, value: &Scalar) -> bool {
        matches!(
            (self, value),
            (ColumnType::Int, Scalar::Int(_))
                | (ColumnType::Float, Scalar::Float(_))
                | (ColumnType::Bool, Scalar::Bool(_))
                | (ColumnType::Text, Scalar::Text(_))
        )
    }
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct ColumnSchema {
    pub name: String,
    #[serde(rename = "type")]
    pub ty: ColumnType,
}

/// Ordered, name-addressable schema shared by every batch of a table.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct BatchSchema {
    pub columns: Vec<ColumnSchema>,
}

impl BatchSchema {
    pub fn new(columns: Vec<ColumnSchema>) -> Result<Self> {
        if columns.is_empty() {
            return Err(IndexError::new(
                ErrorKind::ArrowSchemaMismatch,
                "schema must declare at least one column",
            ));
        }
        for (i, c) in columns.iter().enumerate() {
            if c.name.is_empty() {
                return Err(IndexError::new(
                    ErrorKind::ArrowSchemaMismatch,
                    format!("column {i} has an empty name"),
                ));
            }
            if columns.iter().skip(i + 1).any(|o| o.name == c.name) {
                return Err(IndexError::new(
                    ErrorKind::ArrowSchemaMismatch,
                    format!("duplicate column name {:?}", c.name),
                ));
            }
        }
        Ok(Self { columns })
    }

    pub fn column_index(&self, name: &str) -> Result<usize> {
        self.columns
            .iter()
            .position(|c| c.name == name)
            .ok_or_else(|| {
                IndexError::new(
                    ErrorKind::ColumnNotFound,
                    format!("column {name:?} not in schema [{}]", self.names().join(", ")),
                )
            })
    }

    pub fn column_type(&self, name: &str) -> Result<ColumnType> {
        Ok(self.columns[self.column_index(name)?].ty)
    }

    pub fn names(&self) -> Vec<&str> {
        self.columns.iter().map(|c| c.name.as_str()).collect()
    }
}

/// One typed batch: schema + arrow2 chunk, all rows aligned by position.
#[derive(Debug, Clone)]
pub struct TypedBatch {
    schema: Arc<BatchSchema>,
    chunk: Chunk<Box<dyn Array>>,
}

impl TypedBatch {
    /// Build a batch from column-major typed cells, validating every value
    /// against the declared column type.
    pub fn from_columns(schema: Arc<BatchSchema>, columns: Vec<Vec<Option<Scalar>>>) -> Result<Self> {
        if columns.len() != schema.columns.len() {
            return Err(IndexError::new(
                ErrorKind::ArrowSchemaMismatch,
                format!(
                    "got {} column vectors for schema with {} columns",
                    columns.len(),
                    schema.columns.len()
                ),
            ));
        }
        let row_count = columns.first().map_or(0, Vec::len);
        for (ci, (col, cells)) in schema.columns.iter().zip(&columns).enumerate() {
            if cells.len() != row_count {
                return Err(IndexError::new(
                    ErrorKind::ArrowSchemaMismatch,
                    format!(
                        "column {:?} has {} rows, expected {row_count}",
                        col.name,
                        cells.len()
                    ),
                ));
            }
            for (ri, cell) in cells.iter().enumerate() {
                if let Some(v) = cell {
                    if !col.ty.accepts(v) {
                        return Err(IndexError::new(
                            ErrorKind::ArrowSchemaMismatch,
                            format!(
                                "row {ri} column {:?}: value {v:?} is {}, declared {}",
                                col.name,
                                v.type_name(),
                                col.ty.as_str()
                            ),
                        ));
                    }
                }
            }
            let _ = ci;
        }
        let arrays: Vec<Box<dyn Array>> = schema
            .columns
            .iter()
            .zip(columns)
            .map(|(col, cells)| box_array(col.ty, cells))
            .collect();
        Ok(Self { schema, chunk: Chunk::new(arrays) })
    }

    pub fn row_count(&self) -> usize {
        self.chunk.len()
    }

    pub fn schema(&self) -> &Arc<BatchSchema> {
        &self.schema
    }

    pub fn chunk(&self) -> &Chunk<Box<dyn Array>> {
        &self.chunk
    }

    /// Typed cell access. `None` = SQL NULL.
    pub fn cell(&self, row: usize, col: usize) -> Result<Option<Scalar>> {
        if row >= self.row_count() {
            return Err(IndexError::new(
                ErrorKind::RowIndexOutOfBounds,
                format!("cell: row {row} out of bounds for {} rows", self.row_count()),
            ));
        }
        let array = &self.chunk.arrays()[col];
        if array.is_null(row) {
            return Ok(None);
        }
        let scalar = match self.schema.columns[col].ty {
            ColumnType::Int => {
                let a = array.as_any().downcast_ref::<Int64Array>().expect("schema/array type match");
                Scalar::Int(a.value(row))
            }
            ColumnType::Float => {
                let a = array.as_any().downcast_ref::<Float64Array>().expect("schema/array type match");
                Scalar::Float(a.value(row))
            }
            ColumnType::Bool => {
                let a = array.as_any().downcast_ref::<BooleanArray>().expect("schema/array type match");
                Scalar::Bool(a.value(row))
            }
            ColumnType::Text => {
                let a = array.as_any().downcast_ref::<Utf8Array<i32>>().expect("schema/array type match");
                Scalar::Text(a.value(row).to_string())
            }
        };
        Ok(Some(scalar))
    }

    /// Materialize one column as typed cells (used by index builders and the
    /// independent test oracle).
    pub fn column_cells(&self, col: usize) -> Result<Vec<Option<Scalar>>> {
        (0..self.row_count()).map(|r| self.cell(r, col)).collect()
    }
}

fn box_array(ty: ColumnType, cells: Vec<Option<Scalar>>) -> Box<dyn Array> {
    match ty {
        ColumnType::Int => Box::new(Int64Array::from_trusted_len_iter(cells.into_iter().map(|c| match c {
            Some(Scalar::Int(v)) => Some(v),
            None => None,
            Some(other) => unreachable!("validated at ingestion: {other:?}"),
        }))),
        ColumnType::Float => Box::new(Float64Array::from_trusted_len_iter(cells.into_iter().map(|c| match c {
            Some(Scalar::Float(v)) => Some(v),
            None => None,
            Some(other) => unreachable!("validated at ingestion: {other:?}"),
        }))),
        ColumnType::Bool => Box::new(BooleanArray::from_trusted_len_iter(cells.into_iter().map(|c| match c {
            Some(Scalar::Bool(v)) => Some(v),
            None => None,
            Some(other) => unreachable!("validated at ingestion: {other:?}"),
        }))),
        ColumnType::Text => Box::new(Utf8Array::<i32>::from_trusted_len_iter(cells.into_iter().map(|c| match c {
            Some(Scalar::Text(v)) => Some(v),
            None => None,
            Some(other) => unreachable!("validated at ingestion: {other:?}"),
        }))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn schema() -> Arc<BatchSchema> {
        Arc::new(
            BatchSchema::new(vec![
                ColumnSchema { name: "id".into(), ty: ColumnType::Int },
                ColumnSchema { name: "tag".into(), ty: ColumnType::Text },
            ])
            .unwrap(),
        )
    }

    #[test]
    fn nulls_roundtrip_as_null_not_sentinel() {
        let batch = TypedBatch::from_columns(
            schema(),
            vec![
                vec![Some(Scalar::Int(1)), None, Some(Scalar::Int(3))],
                vec![None, Some(Scalar::Text("x".into())), Some(Scalar::Text("".into()))],
            ],
        )
        .unwrap();
        assert_eq!(batch.row_count(), 3);
        assert_eq!(batch.cell(1, 0).unwrap(), None, "NULL stays NULL");
        assert_eq!(batch.cell(0, 1).unwrap(), None);
        // Empty string is a real value, distinct from NULL.
        assert_eq!(batch.cell(2, 1).unwrap(), Some(Scalar::Text("".into())));
        assert_eq!(batch.cell(1, 1).unwrap(), Some(Scalar::Text("x".into())));
    }

    #[test]
    fn wrong_type_cell_is_rejected() {
        let err = TypedBatch::from_columns(
            schema(),
            vec![
                vec![Some(Scalar::Int(1)), Some(Scalar::Text("not-an-int".into()))],
                vec![Some(Scalar::Text("a".into())), Some(Scalar::Text("b".into()))],
            ],
        )
        .unwrap_err();
        assert_eq!(err.kind, ErrorKind::ArrowSchemaMismatch);
    }

    #[test]
    fn ragged_columns_are_rejected() {
        let err = TypedBatch::from_columns(
            schema(),
            vec![vec![Some(Scalar::Int(1))], vec![Some(Scalar::Text("a".into())), None]],
        )
        .unwrap_err();
        assert_eq!(err.kind, ErrorKind::ArrowSchemaMismatch);
    }

    #[test]
    fn unknown_column_is_typed_error() {
        let s = schema();
        assert_eq!(s.column_index("nope").unwrap_err().kind, ErrorKind::ColumnNotFound);
    }
}
