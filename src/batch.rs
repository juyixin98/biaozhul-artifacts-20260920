//! Typed batches: the columnar data boundary of the backend.
//!
//! A [`TypedBatch`] is an ordered collection of named, homogeneously typed
//! columns. Row identity is the **0-based row index inside the batch**; it is
//! stable through sorting, so duplicate values keep distinct identities and
//! join output is a multiset of `(left_row, right_row)` pairs.
//!
//! # Nullability and non-finite floats
//!
//! * Explicit `NULL` values never match any predicate.
//! * `NaN` floats are stored faithfully in arrow2 arrays but, like NULL,
//!   never satisfy any comparison (SQL-like `UNKNOWN` semantics), including
//!   `NaN = NaN`. See `docs/SEMANTICS.md`.

use std::cmp::Ordering;

use arrow2::array::{Array, PrimitiveArray, Utf8Array};
use arrow2::chunk::Chunk;
use arrow2::datatypes::{DataType as ArrowType, Field, Schema};
use serde::{Deserialize, Serialize};

use crate::error::{JoinError, Result};

/// Physical key types supported by the range predicates.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DType {
    Int64,
    Float64,
    Utf8,
}

impl DType {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            DType::Int64 => "int64",
            DType::Float64 => "float64",
            DType::Utf8 => "utf8",
        }
    }

    pub(crate) fn to_arrow(self) -> ArrowType {
        match self {
            DType::Int64 => ArrowType::Int64,
            DType::Float64 => ArrowType::Float64,
            DType::Utf8 => ArrowType::Utf8,
        }
    }

    pub(crate) fn from_arrow(t: &ArrowType) -> Result<Self> {
        match t {
            ArrowType::Int64 => Ok(DType::Int64),
            ArrowType::Float64 => Ok(DType::Float64),
            ArrowType::Utf8 => Ok(DType::Utf8),
            other => Err(JoinError::input(
                "unsupported_arrow_dtype",
                format!("arrow type {other:?} is not supported as a join key"),
            )),
        }
    }
}

/// One borrowed, guaranteed non-NULL (and non-NaN) key value.
#[derive(Debug, Clone, Copy)]
pub(crate) enum Cell<'a> {
    I64(i64),
    F64(f64),
    Str(&'a str),
}

/// Compare two cells of the same physical type. NaN never reaches here.
/// Floats use IEEE total order *except* that `-0.0 == 0.0` (both compare
/// equal), matching `partial_cmp`.
pub(crate) fn cmp_cells(a: Cell<'_>, b: Cell<'_>) -> Ordering {
    try_cmp_cells(a, b).expect("cmp_cells called across dtype families")
}

/// Compare two cells, returning `None` when the dtype families differ.
/// Used on execution paths that did not (or could not) run [`JoinPlan::validate`],
/// so a cross-family comparison is a dynamic compute failure rather than a
/// panic.
pub(crate) fn try_cmp_cells(a: Cell<'_>, b: Cell<'_>) -> Option<Ordering> {
    match (a, b) {
        (Cell::I64(x), Cell::I64(y)) => Some(x.cmp(&y)),
        (Cell::F64(x), Cell::F64(y)) => {
            // Both cells are guaranteed non-NaN, so this is always `Some`.
            x.partial_cmp(&y)
        }
        (Cell::Str(x), Cell::Str(y)) => Some(x.cmp(y)),
        _ => None,
    }
}

#[derive(Debug, Clone)]
pub(crate) enum ColData {
    /// (values, validity) where validity `1` means present.
    I64(Vec<i64>, Vec<u8>),
    F64(Vec<f64>, Vec<u8>),
    Str(Vec<String>, Vec<u8>),
}

/// A single named, nullable, homogeneously typed column.
#[derive(Debug, Clone)]
pub struct Column {
    pub dtype: DType,
    pub(crate) data: ColData,
}

impl Column {
    #[must_use]
    pub fn len(&self) -> usize {
        match &self.data {
            ColData::I64(_, v) | ColData::F64(_, v) | ColData::Str(_, v) => v.len(),
        }
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// True for explicit SQL NULL.
    #[must_use]
    pub fn is_null(&self, row: usize) -> bool {
        let valid = match &self.data {
            ColData::I64(_, v) | ColData::F64(_, v) | ColData::Str(_, v) => &v[row],
        };
        *valid == 0
    }

    /// Returns the matchable key at `row`, or `None` for NULL / NaN.
    pub(crate) fn key_at(&self, row: usize) -> Option<Cell<'_>> {
        if self.is_null(row) {
            return None;
        }
        match &self.data {
            ColData::I64(vals, _) => Some(Cell::I64(vals[row])),
            ColData::F64(vals, _) => {
                let v = vals[row];
                if v.is_nan() {
                    None
                } else {
                    Some(Cell::F64(v))
                }
            }
            ColData::Str(vals, _) => Some(Cell::Str(&vals[row])),
        }
    }

    /// Typed public accessors. Return `None` for SQL NULL, for float NaN, or
    /// when the column's physical type does not match the accessor. These keep
    /// the internal [`Cell`] enum private while letting API clients read keys.
    #[must_use]
    pub fn i64_at(&self, row: usize) -> Option<i64> {
        match self.key_at(row) {
            Some(Cell::I64(v)) => Some(v),
            _ => None,
        }
    }

    #[must_use]
    pub fn f64_at(&self, row: usize) -> Option<f64> {
        match self.key_at(row) {
            Some(Cell::F64(v)) => Some(v),
            _ => None,
        }
    }

    #[must_use]
    pub fn utf8_at(&self, row: usize) -> Option<&str> {
        match self.key_at(row) {
            Some(Cell::Str(v)) => Some(v),
            _ => None,
        }
    }

    pub fn from_i64(values: Vec<Option<i64>>) -> Self {
        let mut raw = Vec::with_capacity(values.len());
        let valid = values
            .iter()
            .map(|v| {
                raw.push(v.unwrap_or_default());
                u8::from(v.is_some())
            })
            .collect();
        Column { dtype: DType::Int64, data: ColData::I64(raw, valid) }
    }

    pub fn from_f64(values: Vec<Option<f64>>) -> Self {
        let mut raw = Vec::with_capacity(values.len());
        let valid = values
            .iter()
            .map(|v| {
                raw.push(v.unwrap_or(f64::NAN));
                u8::from(v.is_some())
            })
            .collect();
        Column { dtype: DType::Float64, data: ColData::F64(raw, valid) }
    }

    pub fn from_utf8(values: Vec<Option<String>>) -> Self {
        let mut raw = Vec::with_capacity(values.len());
        let valid = values
            .iter()
            .map(|v| {
                raw.push(v.clone().unwrap_or_default());
                u8::from(v.is_some())
            })
            .collect();
        Column { dtype: DType::Utf8, data: ColData::Str(raw, valid) }
    }

    pub(crate) fn to_arrow_array(&self) -> Box<dyn Array> {
        match &self.data {
            ColData::I64(vals, valid) => {
                let iter = vals.iter().zip(valid).map(|(v, ok)| (*ok == 1).then_some(*v));
                PrimitiveArray::<i64>::from_iter(iter).boxed()
            }
            ColData::F64(vals, valid) => {
                let iter = vals.iter().zip(valid).map(|(v, ok)| (*ok == 1).then_some(*v));
                PrimitiveArray::<f64>::from_iter(iter).boxed()
            }
            ColData::Str(vals, valid) => {
                let iter = vals
                    .iter()
                    .zip(valid)
                    .map(|(v, ok)| (*ok == 1).then(|| v.as_str()));
                Utf8Array::<i32>::from_iter(iter).boxed()
            }
        }
    }

    pub(crate) fn from_arrow_array(array: &dyn Array) -> Result<Self> {
        let dtype = DType::from_arrow(array.data_type())?;
        match dtype {
            DType::Int64 => {
                let a = array
                    .as_any()
                    .downcast_ref::<PrimitiveArray<i64>>()
                    .ok_or_else(|| {
                        JoinError::compute("arrow_downcast_failed", "expected Int64 array")
                    })?;
                let vals = (0..a.len())
                    .map(|i| if a.is_null(i) { None } else { Some(a.value(i)) })
                    .collect();
                Ok(Column::from_i64(vals))
            }
            DType::Float64 => {
                let a = array
                    .as_any()
                    .downcast_ref::<PrimitiveArray<f64>>()
                    .ok_or_else(|| {
                        JoinError::compute("arrow_downcast_failed", "expected Float64 array")
                    })?;
                let vals = (0..a.len())
                    .map(|i| if a.is_null(i) { None } else { Some(a.value(i)) })
                    .collect();
                Ok(Column::from_f64(vals))
            }
            DType::Utf8 => {
                let a =
                    array.as_any().downcast_ref::<Utf8Array<i32>>().ok_or_else(|| {
                        JoinError::compute("arrow_downcast_failed", "expected Utf8 array")
                    })?;
                let vals = (0..a.len())
                    .map(|i| if a.is_null(i) { None } else { Some(a.value(i).to_owned()) })
                    .collect();
                Ok(Column::from_utf8(vals))
            }
        }
    }

    /// Append the rows of `other` to this column (same physical dtype).
    /// Used by the stateful run registry when appending an input batch.
    pub(crate) fn append(&mut self, other: &Column) -> Result<()> {
        if self.dtype != other.dtype {
            return Err(JoinError::state(
                "schema_drift",
                format!(
                    "cannot append {} column into {} column",
                    other.dtype.as_str(),
                    self.dtype.as_str()
                ),
            ));
        }
        match (&mut self.data, &other.data) {
            (ColData::I64(a, av), ColData::I64(b, bv)) => {
                a.extend_from_slice(b);
                av.extend_from_slice(bv);
            }
            (ColData::F64(a, av), ColData::F64(b, bv)) => {
                a.extend_from_slice(b);
                av.extend_from_slice(bv);
            }
            (ColData::Str(a, av), ColData::Str(b, bv)) => {
                a.extend(b.iter().cloned());
                av.extend_from_slice(bv);
            }
            // Unreachable: the dtype equality check above guarantees equal
            // variants. Kept explicit so a future dtype addition fails safely.
            _ => {
                return Err(JoinError::compute(
                    "column_variant_drift",
                    "columns share a dtype tag but use different physical representations",
                ))
            }
        }
        Ok(())
    }
}

/// An ordered, named set of equal-length columns.
#[derive(Debug, Clone)]
pub struct TypedBatch {
    columns: Vec<(String, Column)>,
}

impl TypedBatch {
    /// Construct a batch, validating name uniqueness and equal lengths.
    pub fn new(columns: Vec<(String, Column)>) -> Result<Self> {
        let mut row_count: Option<usize> = None;
        for (name, col) in &columns {
            if name.is_empty() {
                return Err(JoinError::input("empty_column_name", "column names must be non-empty"));
            }
            if let Some(n) = row_count {
                if n != col.len() {
                    return Err(JoinError::input(
                        "ragged_batch",
                        format!("column {name} has {} rows, expected {n}", col.len()),
                    )
                    .with("column", name.as_str()));
                }
            } else {
                row_count = Some(col.len());
            }
        }
        for i in 1..columns.len() {
            for j in 0..i {
                if columns[i].0 == columns[j].0 {
                    return Err(JoinError::input(
                        "duplicate_column",
                        format!("duplicate column name {}", columns[i].0),
                    )
                    .with("column", columns[i].0.clone()));
                }
            }
        }
        Ok(TypedBatch { columns })
    }

    #[must_use]
    pub fn row_count(&self) -> usize {
        self.columns.first().map_or(0, |(_, c)| c.len())
    }

    #[must_use]
    pub fn columns(&self) -> &[(String, Column)] {
        &self.columns
    }

    /// Resolve a column name.
    pub fn column_index(&self, name: &str) -> Result<usize> {
        self.columns
            .iter()
            .position(|(n, _)| n == name)
            .ok_or_else(|| {
                JoinError::input("unknown_column", format!("no column named {name:?}"))
                    .with("column", name)
            })
    }

    #[must_use]
    pub fn column(&self, idx: usize) -> &Column {
        &self.columns[idx].1
    }

    /// arrow2 schema of the batch (all fields nullable).
    #[must_use]
    pub fn arrow_schema(&self) -> Schema {
        Schema::from(
            self.columns
                .iter()
                .map(|(name, col)| Field::new(name.clone(), col.dtype.to_arrow(), true))
                .collect::<Vec<_>>(),
        )
    }

    /// Encode as an arrow2 `Chunk`.
    #[must_use]
    pub fn to_arrow_chunk(&self) -> Chunk<Box<dyn Array>> {
        Chunk::new(self.columns.iter().map(|(_, c)| c.to_arrow_array()).collect())
    }

    /// Decode an arrow2 chunk, taking field names from `schema`.
    pub fn from_arrow_chunk(schema: &Schema, chunk: &Chunk<Box<dyn Array>>) -> Result<Self> {
        if schema.fields.len() != chunk.columns().len() {
            return Err(JoinError::input(
                "arrow_schema_chunk_mismatch",
                "schema field count differs from chunk column count",
            ));
        }
        let mut columns = Vec::with_capacity(chunk.columns().len());
        for (field, array) in schema.fields.iter().zip(chunk.columns()) {
            columns.push((field.name.clone(), Column::from_arrow_array(array.as_ref())?));
        }
        TypedBatch::new(columns)
    }

    /// Append every row of `other` to this batch in place.
    ///
    /// Schemas must match exactly: same column names in the same order with
    /// the same physical types. Row identities of previously appended rows are
    /// preserved because append never reorders existing rows.
    pub fn concat_in_place(&mut self, other: &TypedBatch) -> Result<()> {
        if self.columns.len() != other.columns.len() {
            return Err(JoinError::state(
                "schema_drift",
                format!(
                    "append has {} columns, run expects {}",
                    other.columns.len(),
                    self.columns.len()
                ),
            ));
        }
        for ((name, col), (other_name, other_col)) in
            self.columns.iter_mut().zip(other.columns.iter())
        {
            if name != other_name {
                return Err(JoinError::state(
                    "schema_drift",
                    format!("column {name:?} expected, append has {other_name:?}"),
                ));
            }
            col.append(other_col)?;
        }
        Ok(())
    }
}

/// The canonical output schema: pair of stable row identities.
#[must_use]
pub fn output_arrow_schema() -> Schema {
    Schema::from(vec![
        Field::new("left_row", ArrowType::UInt32, false),
        Field::new("right_row", ArrowType::UInt32, false),
    ])
}

/// Encode `(left_row, right_row)` pairs as an arrow2 chunk.
#[must_use]
pub fn pairs_to_arrow_chunk(pairs: &[(u32, u32)]) -> Chunk<Box<dyn Array>> {
    let lhs: PrimitiveArray<u32> = PrimitiveArray::from_values(pairs.iter().map(|p| p.0));
    let rhs: PrimitiveArray<u32> = PrimitiveArray::from_values(pairs.iter().map(|p| p.1));
    Chunk::new(vec![lhs.boxed(), rhs.boxed()])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn null_and_nan_have_no_key() {
        let c = Column::from_i64(vec![Some(1), None, Some(3)]);
        assert!(c.key_at(0).is_some());
        assert!(c.key_at(1).is_none());
        let f = Column::from_f64(vec![Some(1.0), Some(f64::NAN), None]);
        assert!(f.key_at(0).is_some());
        assert!(f.key_at(1).is_none(), "NaN must not match");
        assert!(f.key_at(2).is_none());
    }

    #[test]
    fn rejects_ragged_and_duplicate_and_unnamed() {
        let err = TypedBatch::new(vec![
            ("a".to_owned(), Column::from_i64(vec![Some(1)])),
            ("b".to_owned(), Column::from_i64(vec![])),
        ])
        .unwrap_err();
        assert_eq!(err.code, "ragged_batch");

        let err = TypedBatch::new(vec![
            ("a".to_owned(), Column::from_i64(vec![Some(1)])),
            ("a".to_owned(), Column::from_i64(vec![Some(2)])),
        ])
        .unwrap_err();
        assert_eq!(err.code, "duplicate_column");

        let err = TypedBatch::new(vec![(
            String::new(),
            Column::from_i64(vec![Some(1)]),
        )])
        .unwrap_err();
        assert_eq!(err.code, "empty_column_name");
    }

    #[test]
    fn arrow_roundtrip_preserves_nulls_and_order() {
        let batch = TypedBatch::new(vec![
            ("a".to_owned(), Column::from_i64(vec![Some(5), None, Some(-2)])),
            (
                "s".to_owned(),
                Column::from_utf8(vec![Some("x".into()), None, Some("z".into())]),
            ),
        ])
        .unwrap();
        let schema = batch.arrow_schema();
        let chunk = batch.to_arrow_chunk();
        let back = TypedBatch::from_arrow_chunk(&schema, &chunk).unwrap();
        assert_eq!(back.row_count(), 3);
        assert!(back.column(0).is_null(1));
        assert_eq!(
            back.column(0).key_at(2).map(|c| matches!(c, Cell::I64(-2))),
            Some(true)
        );
    }
}
