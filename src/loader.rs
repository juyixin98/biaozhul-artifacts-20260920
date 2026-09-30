//! Local synthetic-fixture loading (CSV).
//!
//! CSV conventions:
//! * first row is a header of column names;
//! * an **empty field is SQL NULL**;
//! * column logical types are declared explicitly by the caller
//!   (`INTEGER`/`TEXT`/`BOOLEAN`) so no type is guessed;
//! * every row must have exactly the declared number of fields.
//!
//! No network or external accounts are involved — fixtures are plain files
//! under `fixtures/`.

use std::path::Path;

use crate::batch::{BatchBuilder, LogicalType, TypedBatch};
use crate::error::{Result, TritmapError};

/// Declared CSV column: header name plus logical type.
#[derive(Debug, Clone)]
pub struct CsvColumn {
    pub name: String,
    pub ty: LogicalType,
}

impl CsvColumn {
    pub fn new(name: impl Into<String>, ty: LogicalType) -> Self {
        Self {
            name: name.into(),
            ty,
        }
    }
}

/// Read a CSV fixture into a [`TypedBatch`].
pub fn load_csv(path: impl AsRef<Path>, columns: &[CsvColumn]) -> Result<TypedBatch> {
    let path = path.as_ref();
    let text = std::fs::read_to_string(path).map_err(|e| {
        TritmapError::Io(std::io::Error::new(
            e.kind(),
            format!("reading fixture {}: {e}", path.display()),
        ))
    })?;
    parse_csv(&text, columns)
}

fn parse_csv(text: &str, columns: &[CsvColumn]) -> Result<TypedBatch> {
    let mut reader = csv::ReaderBuilder::new()
        .has_headers(true)
        .trim(csv::Trim::All)
        .from_reader(text.as_bytes());

    // Validate header against the declared schema (order + names).
    let headers = reader
        .headers()
        .map_err(|e| TritmapError::Csv(format!("header: {e}")))?;
    if headers.len() != columns.len() {
        return Err(TritmapError::SchemaMismatch(format!(
            "CSV has {} header columns, schema declares {}",
            headers.len(),
            columns.len()
        )));
    }
    for (i, c) in columns.iter().enumerate() {
        if headers.get(i) != Some(c.name.as_str()) {
            return Err(TritmapError::SchemaMismatch(format!(
                "column {i}: header '{}' != declared '{}'",
                headers.get(i).unwrap_or(""),
                c.name
            )));
        }
    }

    let n = columns.len();
    let mut int_cols: Vec<Vec<Option<i64>>> = vec![Vec::new(); n];
    let mut txt_cols: Vec<Vec<Option<String>>> = vec![Vec::new(); n];
    let mut bool_cols: Vec<Vec<Option<bool>>> = vec![Vec::new(); n];

    // Row numbering starts at 1: the header was row 0.
    for (row_no, record) in (1usize..).zip(reader.records()) {
        let record = record.map_err(|e| TritmapError::Csv(format!("row {row_no}: {e}")))?;
        if record.len() != n {
            return Err(TritmapError::SchemaMismatch(format!(
                "row {row_no}: {n} fields declared, found {}",
                record.len()
            )));
        }
        for (i, col) in columns.iter().enumerate() {
            let raw = record.get(i).unwrap_or("");
            match col.ty {
                LogicalType::Integer => {
                    let v = if raw.is_empty() {
                        None
                    } else {
                        Some(raw.parse::<i64>().map_err(|e| {
                            TritmapError::Csv(format!(
                                "row {row_no} col '{}': '{raw}' is not INTEGER ({e})",
                                col.name
                            ))
                        })?)
                    };
                    int_cols[i].push(v);
                }
                LogicalType::Text => {
                    txt_cols[i].push(if raw.is_empty() {
                        None
                    } else {
                        Some(raw.to_string())
                    });
                }
                LogicalType::Boolean => {
                    let v = if raw.is_empty() {
                        None
                    } else {
                        Some(match raw.to_ascii_lowercase().as_str() {
                            "true" | "1" | "t" | "yes" => true,
                            "false" | "0" | "f" | "no" => false,
                            other => {
                                return Err(TritmapError::Csv(format!(
                                    "row {row_no} col '{}': '{other}' is not BOOLEAN",
                                    col.name
                                )))
                            }
                        })
                    };
                    bool_cols[i].push(v);
                }
            }
        }
    }

    let mut builder = BatchBuilder::new();
    for (i, col) in columns.iter().enumerate() {
        builder = match col.ty {
            LogicalType::Integer => {
                builder.add_integer(&col.name, std::mem::take(&mut int_cols[i]))
            }
            LogicalType::Text => {
                let strs: Vec<Option<&str>> = txt_cols[i]
                    .iter()
                    .map(|o| o.as_deref())
                    .collect();
                builder.add_text(&col.name, strs)
            }
            LogicalType::Boolean => {
                builder.add_boolean(&col.name, std::mem::take(&mut bool_cols[i]))
            }
        };
    }
    builder.build()
}

#[cfg(test)]
mod tests {
    use super::*;

    const CSV: &str = "id,name,vip\n1,Amy,true\n,,false\n-3,Cee,\n";

    #[test]
    fn parses_types_and_empty_as_null() {
        let cols = [
            CsvColumn::new("id", LogicalType::Integer),
            CsvColumn::new("name", LogicalType::Text),
            CsvColumn::new("vip", LogicalType::Boolean),
        ];
        let b = parse_csv(CSV, &cols).unwrap();
        assert_eq!(b.len(), 3);
        assert_eq!(
            b.column_values("id").unwrap(),
            vec![Some(crate::batch::Scalar::Int(1)), None, Some(crate::batch::Scalar::Int(-3))]
        );
        assert_eq!(b.column_values("vip").unwrap()[2], None);
    }

    #[test]
    fn rejects_header_and_value_errors() {
        let cols = [
            CsvColumn::new("id", LogicalType::Integer),
            CsvColumn::new("name", LogicalType::Text),
        ];
        assert!(matches!(parse_csv(CSV, &cols), Err(TritmapError::SchemaMismatch(_))));

        let bad = "id\nnotanint\n";
        let cols = [CsvColumn::new("id", LogicalType::Integer)];
        assert!(matches!(parse_csv(bad, &cols), Err(TritmapError::Csv(_))));
    }
}
