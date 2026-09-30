//! Typed batches: column-declared rows of [`Scalar`]s plus conversion to and
//! from Arrow2 IPC streams.
//!
//! A batch is the only way data enters the catalog. Columns have explicit
//! names and physical types; values inside a column must share a type (NULL
//! excluded). NULL handling is a declared policy rather than an accident:
//!
//! * [`NullPolicy::Reject`] (default) - a row containing any NULL fails the
//!   whole request with [`ReasonCode::NullRowRejected`].
//! * [`NullPolicy::Drop`] - rows containing NULL are removed before storage
//!   and counted in [`IngestReport::dropped_null_rows`]. NULL never equals
//!   NULL, so dropping is the only coherent join semantics offered.

use serde::{Deserialize, Serialize};

use crate::error::{Error, ReasonCode, Result};

use super::value::{OrderedF64, Scalar};

/// Supported physical column types.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ColumnType {
    Int64,
    Float64,
    Boolean,
    Utf8,
}

impl ColumnType {
    pub fn as_str(self) -> &'static str {
        match self {
            ColumnType::Int64 => "int64",
            ColumnType::Float64 => "float64",
            ColumnType::Boolean => "boolean",
            ColumnType::Utf8 => "utf8",
        }
    }

    fn matches_scalar(self, v: &Scalar) -> bool {
        matches!(
            (self, v),
            (ColumnType::Int64, Scalar::Int(_))
                | (ColumnType::Float64, Scalar::Float(_))
                | (ColumnType::Boolean, Scalar::Bool(_))
                | (ColumnType::Utf8, Scalar::Str(_))
        )
    }
}

/// Declared NULL strategy for an ingestion request.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum NullPolicy {
    /// Reject any row containing NULL (the safe default).
    #[default]
    Reject,
    /// Drop rows containing NULL before they reach the trie.
    Drop,
}

/// Column declaration; a bare string means an untyped (inferred) column.
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(untagged)]
pub enum ColumnSpec {
    Name(String),
    Typed {
        name: String,
        #[serde(rename = "type")]
        data_type: ColumnType,
    },
}

impl ColumnSpec {
    pub fn name(&self) -> &str {
        match self {
            ColumnSpec::Name(n) | ColumnSpec::Typed { name: n, .. } => n,
        }
    }

    pub fn declared_type(&self) -> Option<ColumnType> {
        match self {
            ColumnSpec::Name(_) => None,
            ColumnSpec::Typed { data_type, .. } => Some(*data_type),
        }
    }
}

/// A validated, typed batch in row form (trie building consumes rows; Arrow2
/// conversion is columnar underneath).
#[derive(Debug, Clone)]
pub struct TypedBatch {
    pub columns: Vec<(String, ColumnType)>,
    pub rows: Vec<Vec<Scalar>>,
}

impl TypedBatch {
    pub fn arity(&self) -> usize {
        self.columns.len()
    }

    pub fn column_names(&self) -> Vec<String> {
        self.columns.iter().map(|(n, _)| n.clone()).collect()
    }
}

/// Outcome of validating an ingestion request against its NULL policy.
#[derive(Debug, Clone)]
pub struct IngestReport {
    pub batch: TypedBatch,
    pub null_policy: NullPolicy,
    pub dropped_null_rows: usize,
    /// Whether the caller asked to overwrite an existing relation.
    pub replace: bool,
}

/// JSON body of `POST /relations/:name`.
#[derive(Debug, Clone, Deserialize)]
pub struct LoadRequest {
    #[serde(default)]
    pub null_policy: NullPolicy,
    #[serde(default)]
    pub replace: bool,
    pub columns: Vec<ColumnSpec>,
    pub rows: Vec<Vec<serde_json::Value>>,
}

fn malformed(msg: impl Into<String>) -> Error {
    Error::validation(ReasonCode::MalformedBatch, msg)
}

/// Validate and type a JSON load request, applying the NULL policy.
///
/// `relation` is used only in error messages; row contents are never embedded
/// in errors or logs verbatim.
pub fn validate_json_request(relation: &str, req: &LoadRequest) -> Result<IngestReport> {
    if req.columns.is_empty() {
        return Err(malformed("batch must declare at least one column"));
    }
    let mut seen = std::collections::HashSet::new();
    for c in &req.columns {
        if c.name().is_empty() {
            return Err(malformed("column name must not be empty"));
        }
        if !seen.insert(c.name().to_string()) {
            return Err(Error::validation(
                ReasonCode::DuplicateColumn,
                format!("duplicate column in relation {relation}"),
            ));
        }
    }

    let arity = req.columns.len();
    let mut types: Vec<Option<ColumnType>> =
        req.columns.iter().map(ColumnSpec::declared_type).collect();

    let mut scalar_rows: Vec<Vec<Scalar>> = Vec::with_capacity(req.rows.len());
    for (idx, row) in req.rows.iter().enumerate() {
        if row.len() != arity {
            return Err(malformed(format!(
                "row {idx} has {} values, expected {arity}",
                row.len()
            )));
        }
        let mut parsed = Vec::with_capacity(arity);
        for (col, json) in row.iter().enumerate() {
            let v: Scalar = serde_json::from_value(json.clone()).map_err(|e| {
                Error::validation(
                    ReasonCode::UnsupportedType,
                    format!("row {idx} column {col}: {e}"),
                )
            })?;
            if !v.is_null() {
                let inferred = scalar_type(&v);
                if let Some(declared) = types[col] {
                    if !declared.matches_scalar(&v) {
                        return Err(Error::validation(
                            ReasonCode::UnsupportedType,
                            format!(
                                "row {idx} column {}: value is {} but column is declared {}",
                                req.columns[col].name(),
                                v.type_name(),
                                declared.as_str()
                            ),
                        ));
                    }
                } else {
                    match types[col] {
                        None => types[col] = Some(inferred),
                        Some(prev) if prev != inferred => {
                            return Err(Error::validation(
                                ReasonCode::UnsupportedType,
                                format!(
                                    "column {} mixes types {} and {}",
                                    req.columns[col].name(),
                                    prev.as_str(),
                                    inferred.as_str()
                                ),
                            ));
                        }
                        _ => {}
                    }
                }
            }
            parsed.push(v);
        }
        scalar_rows.push(parsed);
    }

    let mut columns = Vec::with_capacity(arity);
    for (col, spec) in req.columns.iter().enumerate() {
        match types[col] {
            Some(t) => columns.push((spec.name().to_string(), t)),
            None => {
                return Err(malformed(format!(
                    "column {} has no non-NULL values; its type cannot be inferred",
                    spec.name()
                )));
            }
        }
    }

    // Apply the NULL policy.
    let mut dropped_null_rows = 0usize;
    let mut kept = Vec::with_capacity(scalar_rows.len());
    for row in scalar_rows {
        let has_null = row.iter().any(Scalar::is_null);
        if has_null {
            match req.null_policy {
                NullPolicy::Reject => {
                    return Err(Error::validation(
                        ReasonCode::NullRowRejected,
                        format!(
                            "relation {relation} contains NULL under null_policy=reject; \
                             resend with null_policy=drop to discard such rows"
                        ),
                    ));
                }
                NullPolicy::Drop => dropped_null_rows += 1,
            }
        } else {
            kept.push(row);
        }
    }

    Ok(IngestReport {
        batch: TypedBatch {
            columns,
            rows: kept,
        },
        null_policy: req.null_policy,
        dropped_null_rows,
        replace: req.replace,
    })
}

fn scalar_type(v: &Scalar) -> ColumnType {
    match v {
        Scalar::Int(_) => ColumnType::Int64,
        Scalar::Float(_) => ColumnType::Float64,
        Scalar::Bool(_) => ColumnType::Boolean,
        Scalar::Str(_) => ColumnType::Utf8,
        Scalar::Null => unreachable!("NULL columns are not typed"),
    }
}

// ---------------------------------------------------------------------------
// Arrow2 conversion
// ---------------------------------------------------------------------------

use arrow2::array::{Array, BooleanArray, Float64Array, PrimitiveArray, Utf8Array};
use arrow2::chunk::Chunk;
use arrow2::datatypes::{DataType, Field, Schema};
use arrow2::io::ipc::read::{read_stream_metadata, StreamReader, StreamState};
use arrow2::io::ipc::write::{StreamWriter, WriteOptions};

fn arrow_dtype(t: ColumnType) -> DataType {
    match t {
        ColumnType::Int64 => DataType::Int64,
        ColumnType::Float64 => DataType::Float64,
        ColumnType::Boolean => DataType::Boolean,
        ColumnType::Utf8 => DataType::Utf8,
    }
}

/// Build an Arrow2 [`Chunk`] with one nullable column per declared column.
pub fn to_arrow_chunk(batch: &TypedBatch) -> Chunk<Box<dyn Array>> {
    let n = batch.rows.len();
    let mut arrays: Vec<Box<dyn Array>> = Vec::with_capacity(batch.arity());
    for (col, (_, t)) in batch.columns.iter().enumerate() {
        let col_values: Vec<&Scalar> = batch.rows.iter().map(|r| &r[col]).collect();
        let array: Box<dyn Array> = match t {
            ColumnType::Int64 => {
                let vals: Vec<Option<i64>> = col_values
                    .iter()
                    .map(|v| match v {
                        Scalar::Int(i) => Some(*i),
                        _ => None,
                    })
                    .collect();
                Box::new(PrimitiveArray::<i64>::from(vals).to(DataType::Int64))
            }
            ColumnType::Float64 => {
                let vals: Vec<Option<f64>> = col_values
                    .iter()
                    .map(|v| match v {
                        Scalar::Float(OrderedF64(f)) => Some(*f),
                        _ => None,
                    })
                    .collect();
                Box::new(Float64Array::from(vals))
            }
            ColumnType::Boolean => {
                let vals: Vec<Option<bool>> = col_values
                    .iter()
                    .map(|v| match v {
                        Scalar::Bool(b) => Some(*b),
                        _ => None,
                    })
                    .collect();
                Box::new(BooleanArray::from(vals))
            }
            ColumnType::Utf8 => {
                let vals: Vec<Option<&str>> = col_values
                    .iter()
                    .map(|v| match v {
                        Scalar::Str(s) => Some(s.as_str()),
                        _ => None,
                    })
                    .collect();
                Box::new(Utf8Array::<i32>::from(vals))
            }
        };
        debug_assert_eq!(array.len(), n);
        arrays.push(array);
    }
    Chunk::new(arrays)
}

/// Serialize a batch as an Arrow IPC stream: schema message + one record
/// batch + end-of-stream marker.
pub fn encode_ipc_stream(batch: &TypedBatch) -> Result<Vec<u8>> {
    let fields: Vec<Field> = batch
        .columns
        .iter()
        .map(|(name, t)| Field::new(name.clone(), arrow_dtype(*t), true))
        .collect();
    let schema = Schema::from(fields);
    let chunk = to_arrow_chunk(batch);

    let mut buf = Vec::new();
    {
        let mut writer = StreamWriter::new(&mut buf, WriteOptions { compression: None });
        writer
            .start(&schema, None)
            .map_err(|e| Error::Arrow(e.to_string()))?;
        writer
            .write(&chunk, None)
            .map_err(|e| Error::Arrow(e.to_string()))?;
        writer
            .finish()
            .map_err(|e| Error::Arrow(e.to_string()))?;
    }
    Ok(buf)
}

fn dtype_to_column(t: &DataType) -> Option<ColumnType> {
    match t {
        DataType::Int64 => Some(ColumnType::Int64),
        DataType::Float64 => Some(ColumnType::Float64),
        DataType::Boolean => Some(ColumnType::Boolean),
        DataType::Utf8 | DataType::LargeUtf8 => Some(ColumnType::Utf8),
        _ => None,
    }
}

fn array_to_scalars(
    array: &dyn Array,
    col_type: ColumnType,
) -> Result<Vec<Scalar>> {
    use arrow2::array::{AsArray, PrimitiveArray as PA};
    let len = array.len();
    let mut out = Vec::with_capacity(len);
    match col_type {
        ColumnType::Int64 => {
            let a: &PA<i64> = array
                .as_any()
                .downcast_ref::<PA<i64>>()
                .ok_or_else(|| Error::Arrow("int64 array cast failed".into()))?;
            for i in 0..len {
                out.push(match a.get(i) {
                    Some(v) => Scalar::Int(v),
                    None => Scalar::Null,
                });
            }
        }
        ColumnType::Float64 => {
            let a: &PA<f64> = array
                .as_any()
                .downcast_ref::<PA<f64>>()
                .ok_or_else(|| Error::Arrow("float64 array cast failed".into()))?;
            for i in 0..len {
                out.push(match a.get(i) {
                    Some(v) => Scalar::Float(OrderedF64(v)),
                    None => Scalar::Null,
                });
            }
        }
        ColumnType::Boolean => {
            let a: &BooleanArray = array
                .as_any()
                .downcast_ref::<BooleanArray>()
                .ok_or_else(|| Error::Arrow("boolean array cast failed".into()))?;
            for i in 0..len {
                out.push(match a.get(i) {
                    Some(v) => Scalar::Bool(v),
                    None => Scalar::Null,
                });
            }
        }
        ColumnType::Utf8 => {
            let a = array.as_utf8::<i32>();
            for i in 0..len {
                out.push(match a.value(i) {
                    Some(v) => Scalar::Str(v.to_string()),
                    None => Scalar::Null,
                });
            }
        }
    }
    Ok(out)
}

/// Parse one Arrow IPC stream into a [`TypedBatch`]. Every schema field must
/// have a supported type. NULL values follow the supplied [`NullPolicy`].
pub fn decode_ipc_stream(bytes: &[u8], policy: NullPolicy) -> Result<IngestReport> {
    let mut cursor = std::io::Cursor::new(bytes);
    let metadata = read_stream_metadata(&mut cursor).map_err(|e| {
        Error::validation(
            ReasonCode::MalformedBatch,
            format!("invalid Arrow IPC stream: {e}"),
        )
    })?;
    let schema = metadata.schema.clone();

    let mut columns_meta: Vec<(String, ColumnType)> = Vec::new();
    for f in &schema.fields {
        match dtype_to_column(&f.data_type) {
            Some(t) => columns_meta.push((f.name.clone(), t)),
            None => {
                return Err(Error::validation(
                    ReasonCode::UnsupportedType,
                    format!(
                        "Arrow field {} has unsupported type {:?}",
                        f.name, f.data_type
                    ),
                ));
            }
        }
    }
    if columns_meta.is_empty() {
        return Err(malformed("Arrow schema has no columns"));
    }

    let stream = StreamReader::new(cursor, metadata, None);
    let mut rows: Vec<Vec<Scalar>> = Vec::new();
    for state in stream {
        let chunk = state.map_err(|e| {
            Error::validation(
                ReasonCode::MalformedBatch,
                format!("invalid Arrow IPC record batch: {e}"),
            )
        })?;
        if let StreamState::Some(chunk) = chunk {
            if chunk.arrays().len() != columns_meta.len() {
                return Err(malformed("record batch arity differs from schema"));
            }
            let n = chunk.len();
            let mut columns_scalars: Vec<Vec<Scalar>> = Vec::with_capacity(columns_meta.len());
            for (array, (_, t)) in chunk.arrays().iter().zip(columns_meta.iter()) {
                if array.len() != n {
                    return Err(malformed("jagged Arrow columns in record batch"));
                }
                columns_scalars.push(array_to_scalars(array.as_ref(), *t)?);
            }
            for i in 0..n {
                rows.push(columns_scalars.iter().map(|c| c[i].clone()).collect());
            }
        }
    }

    let raw = TypedBatch {
        columns: columns_meta,
        rows,
    };
    apply_null_policy(raw, policy)
}

/// Apply a NULL policy to an already-typed batch (shared by JSON and Arrow
/// ingest paths).
pub fn apply_null_policy(batch: TypedBatch, policy: NullPolicy) -> Result<IngestReport> {
    let mut dropped_null_rows = 0usize;
    let mut kept = Vec::with_capacity(batch.rows.len());
    for row in batch.rows {
        if row.iter().any(Scalar::is_null) {
            match policy {
                NullPolicy::Reject => {
                    return Err(Error::validation(
                        ReasonCode::NullRowRejected,
                        "batch contains NULL under null_policy=reject; use null_policy=drop"
                            .to_string(),
                    ));
                }
                NullPolicy::Drop => dropped_null_rows += 1,
            }
        } else {
            kept.push(row);
        }
    }
    Ok(IngestReport {
        batch: TypedBatch {
            columns: batch.columns,
            rows: kept,
        },
        null_policy: policy,
        dropped_null_rows,
        replace: false,
    })
}

/// Convert validated rows back into a [`LoadRequest`]-shaped batch for trie
/// building convenience (used by tests and fixture loading).
pub fn batch_from_rows(
    columns: Vec<(String, ColumnType)>,
    rows: Vec<Vec<Scalar>>,
) -> TypedBatch {
    TypedBatch { columns, rows }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn req(json: serde_json::Value) -> LoadRequest {
        serde_json::from_value(json).unwrap()
    }

    #[test]
    fn rejects_jagged_rows() {
        let r = req(serde_json::json!({
            "columns": ["a", "b"],
            "rows": [[1, 2], [3]],
        }));
        let err = validate_json_request("R", &r).unwrap_err();
        assert_eq!(err.reason().unwrap().code, ReasonCode::MalformedBatch);
    }

    #[test]
    fn infers_and_checks_column_types() {
        let r = req(serde_json::json!({
            "columns": ["a", "b"],
            "rows": [[1, "x"], [2, "y"]],
        }));
        let rep = validate_json_request("R", &r).unwrap();
        assert_eq!(
            rep.batch.columns,
            vec![
                ("a".to_string(), ColumnType::Int64),
                ("b".to_string(), ColumnType::Utf8),
            ]
        );

        let mixed = req(serde_json::json!({
            "columns": ["a"],
            "rows": [[1], ["no"]],
        }));
        let err = validate_json_request("R", &mixed).unwrap_err();
        assert_eq!(err.reason().unwrap().code, ReasonCode::UnsupportedType);
    }

    #[test]
    fn null_policies_are_explicit() {
        let body = serde_json::json!({
            "columns": ["a"],
            "rows": [[1], [null]],
        });
        let reject = req(body.clone());
        let err = validate_json_request("R", &reject).unwrap_err();
        assert_eq!(err.reason().unwrap().code, ReasonCode::NullRowRejected);

        let mut drop_body = body.as_object().unwrap().clone();
        drop_body.insert("null_policy".into(), serde_json::json!("drop"));
        let drop = req(serde_json::Value::Object(drop_body));
        let rep = validate_json_request("R", &drop).unwrap();
        assert_eq!(rep.dropped_null_rows, 1);
        assert_eq!(rep.batch.rows.len(), 1);
    }

    #[test]
    fn arrow_ipc_round_trip() {
        let batch = batch_from_rows(
            vec![
                ("a".to_string(), ColumnType::Int64),
                ("b".to_string(), ColumnType::Utf8),
            ],
            vec![
                vec![Scalar::Int(1), Scalar::Str("x".into())],
                vec![Scalar::Int(2), Scalar::Str("y".into())],
            ],
        );
        let bytes = encode_ipc_stream(&batch).unwrap();
        let back = decode_ipc_stream(&bytes, NullPolicy::Reject).unwrap();
        assert_eq!(back.batch.column_names(), vec!["a", "b"]);
        assert_eq!(back.batch.rows, batch.rows);
    }

    #[test]
    fn arrow_ipc_drop_null_round_trip() {
        // Build an Arrow stream with a null.
        let chunk: Chunk<Box<dyn Array>> = Chunk::new(vec![Box::new(
            PrimitiveArray::<i64>::from(vec![Some(1), None, Some(3)]).to(DataType::Int64),
        )]);
        let schema = Schema::from(vec![Field::new("a", DataType::Int64, true)]);
        let mut buf = Vec::new();
        {
            let mut w = StreamWriter::new(&mut buf, WriteOptions { compression: None });
            w.start(&schema, None).unwrap();
            w.write(&chunk, None).unwrap();
            w.finish().unwrap();
        }
        let rep = decode_ipc_stream(&buf, NullPolicy::Drop).unwrap();
        assert_eq!(rep.dropped_null_rows, 1);
        assert_eq!(rep.batch.rows.len(), 2);

        let err = decode_ipc_stream(&buf, NullPolicy::Reject).unwrap_err();
        assert_eq!(err.reason().unwrap().code, ReasonCode::NullRowRejected);
    }
}
