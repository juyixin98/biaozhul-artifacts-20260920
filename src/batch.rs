//! Typed columnar batches and conversion to/from Apache Arrow2 arrays.
//!
//! A [`RecordBatch`] is the unit of data crossing the service boundary. Its
//! physical columns are strongly typed (`Int` / `Text` with per-row validity),
//! independent of the join's integer domain codes. Arrow2 is used as the
//! canonical in-memory/over-the-wire representation (CSV is decoded *through*
//! arrow2's CSV reader in [`crate::dataset`]).

use std::sync::Arc;

use arrow2::array::{PrimitiveArray, Utf8Array};
use arrow2::chunk::Chunk;
use arrow2::datatypes::{DataType as ArrowDataType, Field, Schema as ArrowSchema};

use crate::error::{ErrorCode, JoinError, Result};
use crate::types::Datum;

/// Physical column type. `Null` rows are expressed through validity, not here.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ColumnType {
    Int,
    Text,
}

impl ColumnType {
    pub fn as_arrow(self) -> ArrowDataType {
        match self {
            ColumnType::Int => ArrowDataType::Int64,
            ColumnType::Text => ArrowDataType::Utf8,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            ColumnType::Int => "int",
            ColumnType::Text => "text",
        }
    }

    pub fn parse(s: &str) -> Result<Self> {
        match s {
            "int" | "int64" | "integer" => Ok(ColumnType::Int),
            "text" | "string" | "varchar" => Ok(ColumnType::Text),
            other => Err(JoinError::new(
                ErrorCode::SchemaMismatch,
                format!("unsupported column type `{other}` (expected int|text)"),
            )),
        }
    }
}

/// A typed, nullable column.
#[derive(Debug, Clone, PartialEq)]
pub enum Column {
    Int(Vec<Option<i64>>),
    Text(Vec<Option<String>>),
}

impl Column {
    pub fn len(&self) -> usize {
        match self {
            Column::Int(v) => v.len(),
            Column::Text(v) => v.len(),
        }
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    pub fn col_type(&self) -> ColumnType {
        match self {
            Column::Int(_) => ColumnType::Int,
            Column::Text(_) => ColumnType::Text,
        }
    }

    /// Project a column into a [`Datum`] at a row.
    pub fn datum_at(&self, row: usize) -> Datum {
        match self {
            Column::Int(v) => v[row].map_or(Datum::Null, Datum::Int),
            Column::Text(v) => v[row]
                .clone()
                .map_or(Datum::Null, Datum::Text),
        }
    }

    /// Build a typed column from a slice of datums; fails on type mismatch.
    pub fn from_datums(ty: ColumnType, datums: &[Datum]) -> Result<Self> {
        match ty {
            ColumnType::Int => {
                let mut out = Vec::with_capacity(datums.len());
                for d in datums {
                    match d {
                        Datum::Null => out.push(None),
                        Datum::Int(i) => out.push(Some(*i)),
                        other => {
                            return Err(JoinError::new(
                                ErrorCode::SchemaMismatch,
                                format!("expected int, found {}", other.type_tag()),
                            ))
                        }
                    }
                }
                Ok(Column::Int(out))
            }
            ColumnType::Text => {
                let mut out = Vec::with_capacity(datums.len());
                for d in datums {
                    match d {
                        Datum::Null => out.push(None),
                        Datum::Text(s) => out.push(Some(s.clone())),
                        other => {
                            return Err(JoinError::new(
                                ErrorCode::SchemaMismatch,
                                format!("expected text, found {}", other.type_tag()),
                            ))
                        }
                    }
                }
                Ok(Column::Text(out))
            }
        }
    }

    fn to_arrow_field(&self, name: &str) -> Field {
        Field::new(name, self.col_type().as_arrow(), true)
    }

    fn to_arrow_array(&self) -> Box<dyn arrow2::array::Array> {
        match self {
            Column::Int(v) => {
                let arr: PrimitiveArray<i64> = v.iter().collect::<PrimitiveArray<i64>>();
                arr.boxed()
            }
            Column::Text(v) => {
                let arr: Utf8Array<i32> = v
                    .iter()
                    .map(|o| o.as_deref())
                    .collect::<Utf8Array<i32>>();
                arr.boxed()
            }
        }
    }

    /// Decode an arrow2 array into a typed [`Column`].
    fn from_arrow_array(array: &dyn arrow2::array::Array) -> Result<Self> {
        match array.data_type() {
            ArrowDataType::Int64 => {
                let a = array
                    .as_any()
                    .downcast_ref::<PrimitiveArray<i64>>()
                    .ok_or_else(|| {
                        JoinError::internal("int64 array failed downcast")
                    })?;
                Ok(Column::Int(a.iter().map(|v| v.copied()).collect()))
            }
            ArrowDataType::Utf8 => {
                let a = array
                    .as_any()
                    .downcast_ref::<Utf8Array<i32>>()
                    .ok_or_else(|| JoinError::internal("utf8 array failed downcast"))?;
                Ok(Column::Text(
                    a.iter().map(|v| v.map(|s| s.to_string())).collect(),
                ))
            }
            other => Err(JoinError::new(
                ErrorCode::SchemaMismatch,
                format!("unsupported arrow data type in input: {other:?}"),
            )),
        }
    }
}

/// A named, typed column plus its values.
#[derive(Debug, Clone, PartialEq)]
pub struct NamedColumn {
    pub name: String,
    pub column: Column,
}

/// A columnar batch: equally-sized named columns.
#[derive(Debug, Clone, PartialEq)]
pub struct RecordBatch {
    pub columns: Vec<NamedColumn>,
}

impl RecordBatch {
    pub fn new(columns: Vec<NamedColumn>) -> Result<Self> {
        if columns.is_empty() {
            return Err(JoinError::new(
                ErrorCode::SchemaMismatch,
                "record batch must have at least one column",
            ));
        }
        let rows = columns[0].column.len();
        for c in &columns {
            if c.column.len() != rows {
                return Err(JoinError::new(
                    ErrorCode::SchemaMismatch,
                    format!(
                        "column `{}` has {} rows, expected {}",
                        c.name,
                        c.column.len(),
                        rows
                    ),
                ));
            }
        }
        Ok(RecordBatch { columns })
    }

    pub fn n_rows(&self) -> usize {
        self.columns.first().map(|c| c.column.len()).unwrap_or(0)
    }

    pub fn n_cols(&self) -> usize {
        self.columns.len()
    }

    pub fn column_index(&self, name: &str) -> Option<usize> {
        self.columns.iter().position(|c| c.name == name)
    }

    /// Read a row as datums in column order.
    pub fn row_datums(&self, row: usize) -> Vec<Datum> {
        self.columns.iter().map(|c| c.column.datum_at(row)).collect()
    }

    /// Project/reorder columns to `names`; error if any is absent.
    pub fn project(&self, names: &[String]) -> Result<RecordBatch> {
        let mut cols = Vec::with_capacity(names.len());
        for n in names {
            let idx = self
                .column_index(n)
                .ok_or_else(|| JoinError::unknown_relation(n.clone()))?;
            cols.push(self.columns[idx].clone());
        }
        RecordBatch::new(cols)
    }

    /// Arrow schema for this batch.
    pub fn arrow_schema(&self) -> Arc<ArrowSchema> {
        Arc::new(ArrowSchema::from(
            self.columns
                .iter()
                .map(|c| c.column.to_arrow_field(&c.name))
                .collect::<Vec<_>>(),
        ))
    }

    /// Convert into an arrow2 [`Chunk`] plus its schema (arrow2 keeps schema
    /// separate from data).
    pub fn to_arrow(
        &self,
    ) -> Result<(
        Arc<ArrowSchema>,
        Chunk<Box<dyn arrow2::array::Array>>,
    )> {
        let schema = self.arrow_schema();
        let arrays = self.columns.iter().map(|c| c.column.to_arrow_array()).collect();
        let chunk = Chunk::try_new(arrays).map_err(|e| {
            JoinError::new(
                ErrorCode::Internal,
                format!("arrow chunk construction failed: {e}"),
            )
        })?;
        Ok((schema, chunk))
    }

    /// Convert from an arrow2 [`Chunk`] and an externally supplied schema.
    pub fn from_arrow(
        schema: &ArrowSchema,
        chunk: &Chunk<Box<dyn arrow2::array::Array>>,
    ) -> Result<Self> {
        if schema.fields.len() != chunk.arrays().len() {
            return Err(JoinError::new(
                ErrorCode::SchemaMismatch,
                "arrow schema/column count mismatch",
            ));
        }
        let mut columns = Vec::with_capacity(chunk.arrays().len());
        for (field, array) in schema.fields.iter().zip(chunk.arrays()) {
            let column = Column::from_arrow_array(array.as_ref())?;
            columns.push(NamedColumn {
                name: field.name.clone(),
                column,
            });
        }
        RecordBatch::new(columns)
    }

    /// Build a batch from parallel names/types and row-major datums.
    pub fn from_rows(
        names: &[String],
        types: &[ColumnType],
        rows: Vec<Vec<Datum>>,
    ) -> Result<Self> {
        if names.len() != types.len() {
            return Err(JoinError::new(
                ErrorCode::SchemaMismatch,
                "names/types length mismatch",
            ));
        }
        let mut datum_cols: Vec<Vec<Datum>> = vec![Vec::with_capacity(rows.len()); names.len()];
        for r in rows {
            if r.len() != names.len() {
                return Err(JoinError::new(
                    ErrorCode::SchemaMismatch,
                    format!("row arity {} != schema {}", r.len(), names.len()),
                ));
            }
            for (i, d) in r.into_iter().enumerate() {
                datum_cols[i].push(d);
            }
        }
        let mut columns = Vec::with_capacity(names.len());
        for (i, name) in names.iter().enumerate() {
            columns.push(NamedColumn {
                name: name.clone(),
                column: Column::from_datums(types[i], &datum_cols[i])?,
            });
        }
        RecordBatch::new(columns)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_through_arrow_preserving_nulls() {
        let batch = RecordBatch::new(vec![
            NamedColumn {
                name: "a".into(),
                column: Column::Int(vec![Some(1), None, Some(3)]),
            },
            NamedColumn {
                name: "b".into(),
                column: Column::Text(vec![Some("x".into()), None, Some("z".into())]),
            },
        ])
        .unwrap();
        let (schema, chunk) = batch.to_arrow().unwrap();
        assert_eq!(chunk.len(), 3);
        let back = RecordBatch::from_arrow(&schema, &chunk).unwrap();
        assert_eq!(back, batch);
    }

    #[test]
    fn rejects_ragged_columns() {
        let err = RecordBatch::new(vec![
            NamedColumn {
                name: "a".into(),
                column: Column::Int(vec![Some(1)]),
            },
            NamedColumn {
                name: "b".into(),
                column: Column::Text(vec![]),
            },
        ]);
        assert_eq!(err.unwrap_err().code, ErrorCode::SchemaMismatch);
    }
}
