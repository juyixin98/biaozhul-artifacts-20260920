//! Arrow2 IPC bridge for the typed batch layer.
//!
//! [`Batch`](crate::types::batch::Batch) is the in-memory contract between
//! modules; this module is the *file/stream* contract. A batch is written as
//! a single-chunk Arrow IPC stream (`Int64` nullable columns) and read back
//! with full validation, so a corrupt or mistyped file surfaces as a
//! classified [`ErrorKind::Input`](crate::error::ErrorKind) error rather than
//! a panic.

use std::io::{Read, Write};

use arrow2::array::{Array, Int64Array};
use arrow2::chunk::Chunk;
use arrow2::datatypes::{DataType as ArrowType, Field, Schema};
use arrow2::io::ipc::read::{read_stream_metadata, StreamReader, StreamState};
use arrow2::io::ipc::write::{StreamWriter, WriteOptions};

use crate::error::{Error, Result};
use crate::types::batch::{Batch, Column, DataType};

/// Serialise a batch as an Arrow IPC stream (schema + one chunk).
pub fn write_ipc_stream(batch: &Batch, mut writer: impl Write) -> Result<()> {
    batch.validate()?;
    let schema = schema_of(batch);
    let chunk = chunk_of(batch);
    let options = WriteOptions { compression: None };
    let mut stream = StreamWriter::new(&mut writer, options);
    stream
        .start(&schema, None)
        .map_err(|e| Error::compute(format!("arrow ipc start failed: {e}")))?;
    stream
        .write(&chunk, None)
        .map_err(|e| Error::compute(format!("arrow ipc write failed: {e}")))?;
    stream
        .finish()
        .map_err(|e| Error::compute(format!("arrow ipc finish failed: {e}")))?;
    Ok(())
}

/// Deserialise a batch from an Arrow IPC stream.
///
/// Every column must be nullable `Int64`; anything else is an input error
/// naming the offending column.
pub fn read_ipc_stream(mut reader: impl Read) -> Result<Batch> {
    let metadata = read_stream_metadata(&mut reader)
        .map_err(|e| Error::input("arrow_ipc_parse", format!("invalid ipc stream: {e}")))?;
    let schema = metadata.schema.clone();
    for field in &schema.fields {
        if field.data_type() != &ArrowType::Int64 {
            return Err(Error::input(
                "unsupported_datatype",
                format!(
                    "column '{}' has arrow type {:?}, only int64 is supported",
                    field.name(),
                    field.data_type()
                ),
            ));
        }
    }
    let mut columns: Vec<Column> = schema
        .fields
        .iter()
        .map(|f| Column::new(f.name().clone(), Vec::new()))
        .collect();
    let mut stream = StreamReader::new(reader, metadata, None);
    while let Some(state) = stream.next() {
        let state = state
            .map_err(|e| Error::input("arrow_ipc_parse", format!("invalid ipc message: {e}")))?;
        let chunk = match state {
            StreamState::Some(chunk) => chunk,
            StreamState::Waiting => continue,
        };
        if chunk.arrays().len() != columns.len() {
            return Err(Error::input(
                "arrow_ipc_parse",
                "ipc chunk column count does not match stream schema",
            ));
        }
        for (col, arr) in columns.iter_mut().zip(chunk.arrays()) {
            let ints = arr.as_any().downcast_ref::<Int64Array>().ok_or_else(|| {
                Error::input(
                    "unsupported_datatype",
                    format!("column '{}' chunk is not int64", col.name),
                )
            })?;
            col.values.extend(ints.iter().map(|v| v.copied()));
        }
    }
    Batch::new(columns)
}

fn schema_of(batch: &Batch) -> Schema {
    Schema {
        fields: batch
            .columns
            .iter()
            .map(|c| Field::new(c.name.clone(), ArrowType::Int64, true))
            .collect(),
        metadata: Default::default(),
    }
}

fn chunk_of(batch: &Batch) -> Chunk<Box<dyn Array>> {
    let arrays: Vec<Box<dyn Array>> = batch
        .columns
        .iter()
        .map(|c| {
            let arr = Int64Array::from_trusted_len_iter(c.values.iter().copied());
            Box::new(arr) as Box<dyn Array>
        })
        .collect();
    Chunk::new(arrays)
}

/// In-memory convenience wrapper around [`write_ipc_stream`].
pub fn to_ipc_bytes(batch: &Batch) -> Result<Vec<u8>> {
    let mut buf = Vec::new();
    write_ipc_stream(batch, &mut buf)?;
    Ok(buf)
}

/// In-memory convenience wrapper around [`read_ipc_stream`].
pub fn from_ipc_bytes(bytes: &[u8]) -> Result<Batch> {
    read_ipc_stream(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_preserves_values_and_nulls() {
        let batch = Batch::new(vec![
            Column::new("a", vec![Some(1), None, Some(-3)]),
            Column::new("b", vec![None, None, Some(7)]),
        ])
        .unwrap();
        let bytes = to_ipc_bytes(&batch).unwrap();
        let back = from_ipc_bytes(&bytes).unwrap();
        assert_eq!(batch, back);
        assert_eq!(back.columns[0].data_type, DataType::Int64);
    }

    #[test]
    fn garbage_bytes_are_input_error() {
        let err = from_ipc_bytes(b"not an arrow stream").unwrap_err();
        assert_eq!(err.kind, crate::error::ErrorKind::Input);
    }
}
